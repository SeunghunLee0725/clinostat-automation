"""근육 응답 전압 캡처 수신기 (2026-10-30) — 대시보드 카드용.

펌프 보드가 `emg:cap:<ms>` 를 받으면 근육 전용 ADS1115(0x49) AIN0−AIN1 을
캡처한 뒤 `s25007/board1/emg` 로 조각을 보낸다(firmware sensor_muscle.h). 여기서는
조각을 모아 **마지막 캡처 하나**만 들고 있다가 `/api/emg/latest` 로 내준다.
기록·세션 저장은 하지 않는다(실측 단계 도구) — CSV 가 필요하면 `gateway.emg_capture`.

연속 측정(`emg:stream:on`): 보드는 keepalive 가 20 s 없으면 스스로 멈춘다(저항센서는
근육 전용 칩과 달라 그동안에도 돈다). 그래서 keepalive 는 **화면이 물을 때만** 대신 보낸다 — 화면을 닫으면 멈춘다.
오래 묻지 않다가 다시 물으면 되살리지 않고 꺼진 것으로 본다.

paho 는 자체 스레드에서 콜백을 부르므로 상태는 잠금 아래에서 바꾸고 읽는다.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from gateway.emg_filter import notch_series
from gateway.emg_packet import (
    EMG_STREAM_TOPIC,
    EMG_TOPIC,
    EmgAssembler,
    EmgCapture,
    EmgPacketError,
    parse_emg_stream_chunk,
)
from gateway.emg_stream import EmgStreamWindow

logger = logging.getLogger("gateway.emg_monitor")

CMD_TOPIC = "s25007/board1/cmd"
REPLY_TOPIC = "s25007/board1/reply"
EMG_MS_MIN, EMG_MS_MAX = 10, 2000
EMG_PGA_MV = (4096, 2048, 1024, 512, 256)
#: 보드 자동 종료(20 s)보다 넉넉히 짧게.
STREAM_KEEPALIVE_S = 5.0
#: 화면이 이만큼 묻지 않으면 연속 측정을 버린 것으로 본다(보드는 이미 멈췄다).
STREAM_ABANDON_S = 15.0
#: 화면 한 번 폴링에 내주는 구간. 화면은 400 ms 마다 묻고 겹치는 부분은 버린다.
STREAM_READ_WINDOW_S = 1.5


class EmgConflictError(Exception):
    """보드·브로커가 없어 캡처를 걸 수 없다 — 409."""

    def __init__(self, message: str, *, code: str = "emg_offline") -> None:
        super().__init__(message)
        self.code = code


def _span(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min_mv": 0.0, "max_mv": 0.0, "pp_mv": 0.0}
    lo, hi = min(values), max(values)
    return {"min_mv": lo, "max_mv": hi, "pp_mv": hi - lo}


def _capture_view(cap: EmgCapture, received_at: float) -> dict[str, Any]:
    t_ms = [t / 1000.0 for t, _ in cap.samples_raw]
    mv = [v for _, v in cap.samples_mv]
    notched = notch_series(t_ms, mv)
    return {
        "capture_id": cap.capture_id,
        "pga_mv": cap.pga_mv,
        "received_at": received_at,
        "t_ms": [round(t, 3) for t in t_ms],
        "mv": [round(v, 4) for v in mv],
        "mv_notch": [round(v, 4) for v in notched],
        "summary": cap.summary(),
        "summary_notch": _span(notched),
    }


class EmgMonitor:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_factory: Optional[Callable[[], Any]] = None,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._client_factory = client_factory
        self._client: Any = None
        self._guard = threading.Lock()
        self._assembler = EmgAssembler()
        self._latest: Optional[dict[str, Any]] = None
        self._board_reply: Optional[str] = None
        self._pending = False
        self._clock = clock
        self._stream = EmgStreamWindow(clock=clock)
        self._stream_requested = False
        self._last_keepalive = 0.0
        self._last_poll = 0.0

    # ─────────────────────────── 연결 ───────────────────────────

    def connect(self) -> None:
        client = self._make_client()
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        try:
            client.connect(self._host, self._port)
            client.loop_start()
        except Exception:  # noqa: BLE001 — 브로커 부재가 게이트웨이 기동을 막으면 안 된다
            logger.exception("emg monitor connect failed")
            return
        self._client = client

    def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            logger.warning("emg monitor disconnect failed")
        self._client = None

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self._username is not None:
            client.username_pw_set(self._username, self._password)
        return client

    def _on_connect(self, client, _userdata, _flags, _reason_code, *_args) -> None:
        # 재접속 때마다 다시 건다 — clean session 이면 브로커가 구독을 버린다(mqtt_pump 와 같은 이유).
        client.subscribe(EMG_TOPIC)
        client.subscribe(REPLY_TOPIC)
        client.subscribe(EMG_STREAM_TOPIC)

    def _on_message(self, _client, _userdata, message) -> None:
        if message.topic == REPLY_TOPIC:
            line = message.payload.decode("utf-8", errors="replace")
            if line.startswith(("emg ", "err emg", "ok emg")):
                with self._guard:
                    self._board_reply = line
                    if line.startswith("err emg"):
                        self._pending = False
            return
        try:
            if message.topic == EMG_STREAM_TOPIC:
                chunk = parse_emg_stream_chunk(message.payload)
                with self._guard:
                    self._stream.feed(chunk)
                return
            with self._guard:
                cap = self._assembler.feed(message.payload)
                if cap is not None:
                    self._latest = _capture_view(cap, time.time())
                    self._pending = False
        except EmgPacketError as exc:
            logger.warning("emg chunk ignored: %s", exc)

    # ─────────────────────────── 명령·조회 ───────────────────────────

    def request_capture(self, ms: int, pga_mv: Optional[int]) -> dict[str, Any]:
        if self._client is None:
            raise EmgConflictError("펌프 보드 MQTT 연결이 없어 캡처를 걸 수 없다")
        if pga_mv is not None:
            self._client.publish(CMD_TOPIC, f"emg:pga:{pga_mv}")
        self._client.publish(CMD_TOPIC, f"emg:cap:{ms}")
        with self._guard:
            self._pending = True
        return self.latest()

    def set_stream(self, on: bool, pga_mv: Optional[int]) -> dict[str, Any]:
        if self._client is None:
            raise EmgConflictError("펌프 보드 MQTT 연결이 없어 연속 측정을 켤 수 없다")
        if on and pga_mv is not None:
            self._client.publish(CMD_TOPIC, f"emg:pga:{pga_mv}")
        self._client.publish(CMD_TOPIC, "emg:stream:on" if on else "emg:stream:off")
        now = self._clock()
        with self._guard:
            self._stream_requested = on
            self._last_keepalive = self._last_poll = now
        return self.stream_snapshot()

    def stream_snapshot(self, window_s: float = STREAM_READ_WINDOW_S) -> dict[str, Any]:
        now = self._clock()
        with self._guard:
            if self._stream_requested and now - self._last_poll > STREAM_ABANDON_S:
                self._stream_requested = False
            self._last_poll = now
            keepalive = self._stream_requested and now - self._last_keepalive >= STREAM_KEEPALIVE_S
            if keepalive:
                self._last_keepalive = now
            snap = self._stream.snapshot(window_s=window_s)
            requested, reply = self._stream_requested, self._board_reply
        if keepalive and self._client is not None:
            self._client.publish(CMD_TOPIC, "emg:stream:on")
        return {**snap, "online": self._client is not None, "requested": requested,
                "board_reply": reply}

    def latest(self) -> dict[str, Any]:
        with self._guard:
            return {
                "online": self._client is not None,
                "pending": self._pending,
                "board_reply": self._board_reply,
                "capture": self._latest,
            }
