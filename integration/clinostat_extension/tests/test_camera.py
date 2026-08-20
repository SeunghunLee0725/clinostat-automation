import importlib.util
import sys
import threading
import types
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORK_CAMERA = ROOT / 'work' / 'camera.py'
DEPLOY_SCRIPT = ROOT.parents[0] / 'deploy' / 'deploy_to_pi.sh'


class EncodedJpeg:
    def __init__(self, value=b'jpeg'):
        self.value = value

    def tobytes(self):
        return self.value


class RGBFrame:
    def __getitem__(self, key):
        return ('rgb-slice', key)


class StubVideoCapture:
    instances = []
    opened = False
    read_result = (False, None)

    def __init__(self, *args):
        self.args = args
        self.set_calls = []
        self.released = False
        type(self).instances.append(self)

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.set_calls.append((prop, value))
        return True

    def read(self):
        return self.read_result

    def release(self):
        self.released = True


class StubPicamera2:
    pass


@pytest.fixture
def camera_module(monkeypatch):
    cv2 = types.ModuleType('cv2')
    cv2.CAP_V4L2 = 1
    cv2.CAP_PROP_FRAME_WIDTH = 2
    cv2.CAP_PROP_FRAME_HEIGHT = 3
    cv2.CAP_PROP_FPS = 4
    cv2.CAP_PROP_BUFFERSIZE = 5
    cv2.IMWRITE_JPEG_QUALITY = 6
    cv2.VideoCapture = StubVideoCapture
    cv2.imencode = lambda *_args: (True, EncodedJpeg())

    picamera2 = types.ModuleType('picamera2')
    picamera2.Picamera2 = StubPicamera2

    numpy = types.ModuleType('numpy')
    numpy.ascontiguousarray = lambda value: value

    monkeypatch.setitem(sys.modules, 'cv2', cv2)
    monkeypatch.setitem(sys.modules, 'picamera2', picamera2)
    monkeypatch.setitem(sys.modules, 'numpy', numpy)

    module_name = f'_camera_under_test_{uuid.uuid4().hex}'
    spec = importlib.util.spec_from_file_location(module_name, WORK_CAMERA)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


class RecordingPicamera:
    def __init__(self, array):
        self.array = array
        self.wait_values = []

    def capture_array(self, *, wait):
        self.wait_values.append(wait)
        return self.array


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class RecordingCondition(threading.Condition):
    def __init__(self):
        super().__init__()
        self.notify_count = 0

    def notify_all(self):
        self.notify_count += 1
        super().notify_all()


class WaitRecordingCondition(threading.Condition):
    def __init__(self):
        super().__init__()
        self.wait_started = threading.Event()

    def wait_for(self, predicate, timeout=None):
        self.wait_started.set()
        return super().wait_for(predicate, timeout)


class AttemptRecordingCondition(threading.Condition):
    def __init__(self):
        super().__init__()
        self.enter_attempted = threading.Event()

    def __enter__(self):
        self.enter_attempted.set()
        return super().__enter__()


def test_camera_module_is_canonical_and_deployed():
    assert WORK_CAMERA.is_file()

    deploy_script = DEPLOY_SCRIPT.read_text(encoding='utf-8')
    assert 'clinostat_extension/work/camera.py:$CLINOSTAT_ROOT/camera.py' in deploy_script
    assert (
        'cp -a $BACKUP_ROOT/clinostat/camera.py $CLINOSTAT_ROOT/camera.py'
        in deploy_script
    )


def test_camera_backup_is_required_without_changing_existing_backups():
    deploy_script = DEPLOY_SCRIPT.read_text(encoding='utf-8')
    backup_section = deploy_script[
        deploy_script.index('say "백업: $BACKUP_ROOT"'):
        deploy_script.index('say "게이트웨이 전송"')
    ]

    assert (
        '  else\n'
        '    run_remote "test -f \'$remote\' && cp -a \'$remote\' '
        '\'$BACKUP_ROOT/clinostat/${remote#$CLINOSTAT_ROOT/}\' || true"\n'
        '  fi'
        in backup_section
    )
    assert (
        '  if [ "$remote" = "$CLINOSTAT_ROOT/camera.py" ]; then\n'
        '    run_remote "test -f \'$remote\' && cp -a \'$remote\' '
        '\'$BACKUP_ROOT/clinostat/${remote#$CLINOSTAT_ROOT/}\'" '
        '|| die "camera.py 백업 실패"\n'
        in backup_section
    )


def test_constructor_accepts_camera_recovery_timings_and_clock(camera_module):
    default = camera_module.Camera()
    assert default.capture_timeout_s == 2.0
    assert default.unhealthy_after_s == 4.0
    assert default.recovery_backoff_s == 0.5
    assert default.stream_stall_timeout_s == 10.0
    assert default._clock is camera_module.time.monotonic

    fake_clock = FakeClock()
    configured = camera_module.Camera(
        capture_timeout_s=2.5,
        unhealthy_after_s=5.0,
        recovery_backoff_s=0.75,
        stream_stall_timeout_s=12.0,
        clock=fake_clock,
    )
    assert configured.capture_timeout_s == 2.5
    assert configured.unhealthy_after_s == 5.0
    assert configured.recovery_backoff_s == 0.75
    assert configured.stream_stall_timeout_s == 12.0
    assert configured._clock is fake_clock


def test_picamera_read_has_numeric_timeout(camera_module):
    fake = RecordingPicamera(array=RGBFrame())
    cam = camera_module.Camera(capture_timeout_s=2.0)
    cam._backend = 'picamera2'
    cam._picam = fake

    ok, frame = cam._read_frame()

    assert ok is True
    assert frame is not None
    assert fake.wait_values == [2.0]


def test_status_is_unhealthy_before_first_frame(camera_module):
    cam = camera_module.Camera()
    cam._backend = 'picamera2'
    cam._picam = object()

    status = cam.status()

    assert status['open'] is True
    assert status['last_frame_age_s'] is None
    assert status['healthy'] is False
    assert status['frame_sequence'] == 0


def test_open_but_stale_camera_is_unhealthy(camera_module):
    fake_clock = FakeClock(now=10.0)
    cam = camera_module.Camera(unhealthy_after_s=4.0, clock=fake_clock)
    cam._backend = 'picamera2'
    cam._picam = object()
    cam._last_frame_t = fake_clock() - 5.0

    status = cam.status()

    assert status['open'] is True
    assert status['healthy'] is False
    assert status['last_frame_age_s'] == 5.0
    assert status['frame_sequence'] == 0


def test_recent_open_camera_without_error_is_healthy(camera_module):
    fake_clock = FakeClock(now=10.0)
    cam = camera_module.Camera(unhealthy_after_s=4.0, clock=fake_clock)
    cam._backend = 'picamera2'
    cam._picam = object()
    cam._last_frame_t = fake_clock() - 1.0
    cam._frame_seq = 3
    cam._last_error = None

    status = cam.status()

    assert status['healthy'] is True
    assert status['last_frame_age_s'] == 1.0
    assert status['frame_sequence'] == 3


def test_status_waits_for_a_coherent_frame_publication(camera_module):
    fake_clock = FakeClock(now=10.0)
    cam = camera_module.Camera(clock=fake_clock)
    cam._backend = 'picamera2'
    cam._picam = object()
    cam._last_error = 'old error'
    cam._frame_cond = AttemptRecordingCondition()
    finished = threading.Event()
    result = []

    def read_status():
        result.append(cam.status())
        finished.set()

    cam._frame_cond.acquire()
    reader = threading.Thread(target=read_status, daemon=True)
    try:
        reader.start()
        assert cam._frame_cond.enter_attempted.wait(timeout=0.5)
        assert finished.is_set() is False
        cam._latest_jpeg = b'fresh'
        cam._frame_seq = 1
        cam._last_frame_t = fake_clock()
        cam._last_error = None
    finally:
        cam._frame_cond.release()
    reader.join(timeout=1.0)

    assert finished.is_set() is True
    assert result == [
        {
            'open': True,
            'healthy': True,
            'viewers': 0,
            'device': '/dev/video0',
            'width': 640,
            'height': 480,
            'fps': 15,
            'backend': 'picamera2',
            'error': None,
            'frame_sequence': 1,
            'last_frame_age_s': 0.0,
        }
    ]


def test_publishing_encoded_frames_updates_health_state_and_notifies(
    camera_module, monkeypatch
):
    fake_clock = FakeClock(now=100.0)
    cam = camera_module.Camera(clock=fake_clock)
    cam._backend = 'picamera2'
    cam._picam = object()
    cam._viewers = 1
    cam._last_error = 'prior read failed'
    cam._frame_cond = RecordingCondition()
    read_count = 0

    def read_frame():
        nonlocal read_count
        read_count += 1
        fake_clock.now = 100.0 + read_count
        if read_count == 2:
            cam._stop.set()
        return True, object()

    encoded_values = iter((EncodedJpeg(b'first'), EncodedJpeg(b'second')))
    cam._read_frame = read_frame
    monkeypatch.setattr(
        camera_module.cv2,
        'imencode',
        lambda *_args: (True, next(encoded_values)),
    )
    monkeypatch.setattr(camera_module.time, 'sleep', lambda _seconds: None)

    cam._capture_loop()
    status = cam.status()

    assert status['frame_sequence'] == 2
    assert status['error'] is None
    assert status['last_frame_age_s'] == 0.0
    assert cam._last_frame_t == 102.0
    assert cam._frame_cond.notify_count == 2


def test_snapshot_does_not_return_cached_frame(camera_module):
    cam = camera_module.Camera()
    cam._ensure_running = lambda: None
    cam._latest_jpeg = b'stale'
    cam._frame_seq = 7

    assert cam.snapshot(timeout=0.01) is None


def test_snapshot_returns_frame_published_after_wait_begins(camera_module):
    cam = camera_module.Camera()
    cam._ensure_running = lambda: None
    cam._latest_jpeg = b'stale'
    cam._frame_seq = 7
    cam._frame_cond = WaitRecordingCondition()

    def publish_fresh_frame():
        if not cam._frame_cond.wait_started.wait(timeout=1.0):
            return
        with cam._frame_cond:
            cam._latest_jpeg = b'fresh'
            cam._frame_seq += 1
            cam._frame_cond.notify_all()

    publisher = threading.Thread(target=publish_fresh_frame, daemon=True)
    publisher.start()
    result = cam.snapshot(timeout=0.5)
    publisher.join(timeout=1.0)

    assert result == b'fresh'
