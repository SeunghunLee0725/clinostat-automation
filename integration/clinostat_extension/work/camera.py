"""
Camera capture + JPEG streaming for the dashboard.

Backends (tried in order):
  1. cv2 + V4L2  — USB webcam or Pi CSI via unicam (/dev/video0)
  2. picamera2   — Pi Camera Module via libcamera (fallback when V4L2 fails)

A single capture instance is shared by all HTTP clients via reference counting:
the camera opens on the first viewer and closes again ~3 s after the last
viewer disconnects.

Endpoints (in server.py):
    GET /api/camera/snapshot   →  one JPEG
    GET /api/camera/stream     →  multipart/x-mixed-replace MJPEG stream
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import cv2

try:
    from picamera2 import Picamera2 as _Picamera2
    import numpy as _np
    _HAS_PICAMERA2 = True
except ImportError:
    _Picamera2 = None
    _np = None
    _HAS_PICAMERA2 = False


class Camera:
    def __init__(
        self,
        device: str = "/dev/video0",
        width: int = 640,
        height: int = 480,
        fps: int = 15,
        jpeg_quality: int = 75,
        idle_close_s: float = 3.0,
        capture_timeout_s: float = 2.0,
        unhealthy_after_s: float = 4.0,
        recovery_backoff_s: float = 0.5,
        stream_stall_timeout_s: float = 10.0,
        clock=time.monotonic,
    ):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.idle_close_s = idle_close_s
        self.capture_timeout_s = capture_timeout_s
        self.unhealthy_after_s = unhealthy_after_s
        self.recovery_backoff_s = recovery_backoff_s
        self.stream_stall_timeout_s = stream_stall_timeout_s
        self._clock = clock

        self._lock = threading.Lock()
        self._cap: cv2.VideoCapture | None = None
        self._picam: object | None = None
        self._backend: str = "none"
        self._capture_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._latest_jpeg: bytes | None = None
        self._frame_cond = threading.Condition()
        self._frame_seq = 0
        self._last_frame_t: float | None = None
        self._viewers = 0
        self._last_viewer_t = time.monotonic()  # avoid immediate idle-close on start
        self._last_error: str | None = None

    # ─────────────────────────────────────────────────────────────────────
    @property
    def is_open(self) -> bool:
        if self._backend == "cv2":
            return self._cap is not None and self._cap.isOpened()
        if self._backend == "picamera2":
            return self._picam is not None
        return False

    @property
    def viewer_count(self) -> int:
        return self._viewers

    def status(self) -> dict:
        is_open = self.is_open
        with self._frame_cond:
            last_frame_t = self._last_frame_t
            frame_sequence = self._frame_seq
            last_error = self._last_error
        age = (
            None
            if last_frame_t is None
            else max(0.0, self._clock() - last_frame_t)
        )
        healthy = (
            is_open
            and age is not None
            and age <= self.unhealthy_after_s
            and last_error is None
        )
        return {
            "open": is_open,
            "healthy": healthy,
            "viewers": self._viewers,
            "device": self.device,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "backend": self._backend,
            "error": last_error,
            "frame_sequence": frame_sequence,
            "last_frame_age_s": age,
        }

    # ─────────────────────────────────────────────────────────────────────
    def _open_cv2(self) -> bool:
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return False
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        # Verify we can actually read a frame (unicam may open but not deliver)
        ok, _ = cap.read()
        if not ok:
            cap.release()
            return False
        self._cap = cap
        self._backend = "cv2"
        self._last_error = None
        return True

    def _open_picamera2(self) -> bool:
        if not _HAS_PICAMERA2:
            return False
        try:
            picam = _Picamera2()
            frame_us = int(1_000_000 / max(1, self.fps))
            config = picam.create_video_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                controls={"FrameDurationLimits": (frame_us, frame_us)},
            )
            picam.configure(config)
            picam.start()
            self._picam = picam
            self._backend = "picamera2"
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = f"picamera2: {exc}"
            return False

    def _open(self) -> bool:
        if self._open_cv2():
            return True
        if self._open_picamera2():
            return True
        self._last_error = f"cannot open {self.device} (tried cv2+V4L2 and picamera2)"
        self._backend = "none"
        return False

    def _close(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None
        if self._picam is not None:
            try:
                self._picam.stop()
                self._picam.close()
            except Exception:
                pass
        self._picam = None
        self._backend = "none"
        self._latest_jpeg = None

    def _read_frame(self) -> tuple[bool, object]:
        if self._backend == "cv2" and self._cap is not None:
            return self._cap.read()
        if self._backend == "picamera2" and self._picam is not None:
            rgb = self._picam.capture_array(wait=self.capture_timeout_s)
            bgr = _np.ascontiguousarray(rgb[:, :, ::-1])
            return True, bgr
        return False, None

    def _capture_loop(self) -> None:
        period = 1.0 / max(1, self.fps)
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        while not self._stop.is_set():
            with self._lock:
                if self._viewers == 0:
                    if time.monotonic() - self._last_viewer_t > self.idle_close_s:
                        self._close()
                        break
                backend = self._backend
            if backend == "none":
                time.sleep(0.05)
                continue
            ok, frame = self._read_frame()
            if not ok or frame is None:
                self._last_error = "read failed"
                time.sleep(0.1)
                continue
            ok2, jpeg = cv2.imencode(".jpg", frame, encode_param)
            if not ok2:
                time.sleep(0.05)
                continue
            with self._frame_cond:
                self._latest_jpeg = jpeg.tobytes()
                self._frame_seq += 1
                self._last_frame_t = self._clock()
                self._last_error = None
                self._frame_cond.notify_all()
            time.sleep(period)
        with self._lock:
            self._close()
        self._capture_thread = None

    # ─────────────────────────────────────────────────────────────────────
    def _ensure_running(self) -> None:
        """Open the camera + start the capture thread if not already running."""
        with self._lock:
            if not self.is_open:
                if not self._open():
                    return
            if self._capture_thread is None or not self._capture_thread.is_alive():
                self._stop.clear()
                self._capture_thread = threading.Thread(
                    target=self._capture_loop, daemon=True
                )
                self._capture_thread.start()

    @contextmanager
    def viewer(self):
        """Use as `with camera.viewer():` while serving a stream. Reference
        counts the active viewer; on exit, schedules idle close."""
        self._ensure_running()
        with self._lock:
            self._viewers += 1
        try:
            yield self
        finally:
            with self._lock:
                self._viewers = max(0, self._viewers - 1)
                self._last_viewer_t = time.monotonic()

    def get_frame(self, timeout: float = 2.0) -> bytes | None:
        """Block until the next JPEG frame is available; None on timeout."""
        with self._frame_cond:
            starting_seq = self._frame_seq
            if not self._frame_cond.wait_for(
                lambda: self._frame_seq > starting_seq,
                timeout=timeout,
            ):
                return None
            return self._latest_jpeg

    def snapshot(self, timeout: float = 2.0) -> bytes | None:
        """Single-shot JPEG (opens camera if needed, but does not start a
        long-lived stream — caller counts as viewer for one frame)."""
        with self.viewer():
            return self.get_frame(timeout=timeout)

    def mjpeg_generator(self):
        """Generator producing multipart/x-mixed-replace MJPEG payload."""
        boundary = b"--frame"
        # Drop the very first frame so a fresh client never sees a stale one
        prev = id(None)
        with self.viewer():
            while not self._stop.is_set():
                with self._frame_cond:
                    self._frame_cond.wait(timeout=2.0)
                    frame = self._latest_jpeg
                if not frame:
                    continue
                if id(frame) == prev:
                    continue
                prev = id(frame)
                yield (
                    boundary
                    + b"\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    + frame
                    + b"\r\n"
                )

    def stop(self) -> None:
        """Force-stop the capture thread (used at process shutdown)."""
        self._stop.set()
        with self._frame_cond:
            self._frame_cond.notify_all()
        t = self._capture_thread
        if t is not None:
            t.join(timeout=1.0)
        with self._lock:
            self._close()


# Process-wide singleton
camera = Camera()
