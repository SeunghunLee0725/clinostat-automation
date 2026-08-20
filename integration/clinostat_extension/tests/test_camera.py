import ast
import asyncio
import importlib.util
import sys
import threading
import types
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORK_CAMERA = ROOT / 'work' / 'camera.py'
WORK_SERVER = ROOT / 'work' / 'server.py'
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
        self.enter_count = 0
        self.notify_count = 0

    def __enter__(self):
        self.enter_count += 1
        return super().__enter__()

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


class StreamWaitCondition(threading.Condition):
    def __init__(self):
        super().__init__()
        self.wait_started = threading.Event()

    def wait(self, timeout=None):
        self.wait_started.set()
        return super().wait(timeout)


class AttemptRecordingCondition(threading.Condition):
    def __init__(self):
        super().__init__()
        self.enter_attempted = threading.Event()

    def __enter__(self):
        self.enter_attempted.set()
        return super().__enter__()


class AttemptRecordingLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.enter_attempted = threading.Event()
        self.progress = threading.Event()

    def acquire(self):
        return self._lock.acquire()

    def release(self):
        return self._lock.release()

    def __enter__(self):
        self.enter_attempted.set()
        self.progress.set()
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


class TeardownGapLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.pause_thread = None
        self.paused = threading.Event()
        self.resume = threading.Event()
        self.timed_out = threading.Event()
        self._did_pause = False

    def acquire(self):
        return self._lock.acquire()

    def release(self):
        return self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()
        if threading.current_thread() is self.pause_thread and not self._did_pause:
            self._did_pause = True
            self.paused.set()
            if not self.resume.wait(timeout=1.0):
                self.timed_out.set()


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


def test_camera_snapshot_route_is_synchronous():
    tree = ast.parse(WORK_SERVER.read_text(encoding='utf-8'))
    route = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == 'camera_snapshot'
    )

    assert type(route) is ast.FunctionDef


class TrackingCloseIterator:
    def __init__(self, iterator, close_error=None):
        self.iterator = iterator
        self.close_error = close_error
        self.close_calls = []

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.iterator)

    def close(self):
        self.close_calls.append(threading.get_ident())
        self.iterator.close()
        if self.close_error is not None:
            raise self.close_error


class StreamCamera:
    def __init__(self, stream, camera=None):
        self.stream = stream
        self.camera = camera

    def mjpeg_generator(self):
        return self.stream

    @property
    def viewer_count(self):
        if self.camera is None:
            return None
        return self.camera.viewer_count


class RouteApp:
    def get(self, *_args, **_kwargs):
        return lambda route: route


def load_camera_stream_route(stream_camera):
    import anyio
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import StreamingResponse

    tree = ast.parse(WORK_SERVER.read_text(encoding='utf-8'))
    selected = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.ClassDef)
            and node.name == 'CloseableStreamingResponse'
        )
        or (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == 'camera_stream'
        )
    ]
    namespace = {
        'anyio': anyio,
        'StreamingResponse': StreamingResponse,
        'run_in_threadpool': run_in_threadpool,
        'camera': stream_camera,
        'app': RouteApp(),
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
            WORK_SERVER,
            'exec',
        ),
        namespace,
    )
    return namespace['camera_stream']


def http_scope(spec_version):
    return {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': spec_version},
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': '/api/camera/stream',
        'raw_path': b'/api/camera/stream',
        'query_string': b'',
        'root_path': '',
        'headers': [],
        'client': ('127.0.0.1', 12345),
        'server': ('127.0.0.1', 8000),
    }


def test_camera_stream_disconnect_closes_generator_and_releases_viewer_promptly(
    camera_module
):
    cam = camera_module.Camera(stream_stall_timeout_s=1.0)
    cam._ensure_running = lambda: None
    underlying = cam.mjpeg_generator()
    tracked = TrackingCloseIterator(underlying)
    route = load_camera_stream_route(StreamCamera(tracked, camera=cam))

    async def scenario():
        disconnect = asyncio.Event()
        messages = []
        loop_thread = threading.get_ident()

        async def receive():
            await disconnect.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            messages.append(message)
            if (
                message['type'] == 'http.response.body'
                and message.get('body')
            ):
                disconnect.set()

        async def publish_after_viewer_enters():
            deadline = asyncio.get_running_loop().time() + 0.5
            while cam.viewer_count != 1:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0)
            with cam._frame_cond:
                cam._latest_jpeg = b'fresh-frame'
                cam._frame_seq += 1
                cam._frame_cond.notify_all()

        response = await route()
        await asyncio.wait_for(
            asyncio.gather(
                response(http_scope('2.3'), receive, send),
                publish_after_viewer_enters(),
            ),
            timeout=1.0,
        )
        return messages, loop_thread, cam.viewer_count, list(tracked.close_calls)

    try:
        messages, loop_thread, viewer_count, close_calls = asyncio.run(scenario())
    finally:
        underlying.close()

    assert any(
        message['type'] == 'http.response.body' and message.get('body')
        for message in messages
    )
    assert viewer_count == 0
    assert len(close_calls) == 1
    assert close_calls[0] != loop_thread


def test_camera_stream_natural_completion_closes_exactly_once_off_event_loop():
    finalized = []

    def finite_stream():
        try:
            yield b'one-frame'
        finally:
            finalized.append('finalized')

    underlying = finite_stream()
    tracked = TrackingCloseIterator(underlying)
    route = load_camera_stream_route(StreamCamera(tracked))

    async def scenario():
        messages = []
        loop_thread = threading.get_ident()

        async def receive():
            raise AssertionError('ASGI 2.4 response must not poll receive')

        async def send(message):
            messages.append(message)

        response = await route()
        await asyncio.wait_for(
            response(http_scope('2.4'), receive, send),
            timeout=1.0,
        )
        await asyncio.wait_for(
            response(http_scope('2.4'), receive, send),
            timeout=1.0,
        )
        return messages, loop_thread

    messages, loop_thread = asyncio.run(scenario())

    assert finalized == ['finalized']
    assert len(tracked.close_calls) == 1
    assert tracked.close_calls[0] != loop_thread
    assert sum(
        message['type'] == 'http.response.body'
        and message.get('more_body') is False
        for message in messages
    ) == 2


def test_camera_stream_send_error_closes_and_preserves_client_disconnect():
    from starlette.requests import ClientDisconnect

    finalized = []

    def endless_stream():
        try:
            while True:
                yield b'frame'
        finally:
            finalized.append('finalized')

    underlying = endless_stream()
    tracked = TrackingCloseIterator(
        underlying,
        close_error=RuntimeError('close also failed'),
    )
    route = load_camera_stream_route(StreamCamera(tracked))

    async def scenario():
        loop_thread = threading.get_ident()

        async def receive():
            raise AssertionError('ASGI 2.4 response must not poll receive')

        async def send(message):
            if (
                message['type'] == 'http.response.body'
                and message.get('body')
            ):
                raise OSError('client send failed')

        response = await route()
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(
                response(http_scope('2.4'), receive, send),
                timeout=1.0,
            )
        return loop_thread

    try:
        loop_thread = asyncio.run(scenario())
    finally:
        underlying.close()

    assert finalized == ['finalized']
    assert len(tracked.close_calls) == 1
    assert tracked.close_calls[0] != loop_thread


def test_camera_stream_task_group_cancellation_shields_iterator_close():
    import anyio

    finalized = []

    def endless_stream():
        try:
            while True:
                yield b'frame'
        finally:
            finalized.append('finalized')

    underlying = endless_stream()
    tracked = TrackingCloseIterator(
        underlying,
        close_error=RuntimeError('close during cancellation failed'),
    )
    route = load_camera_stream_route(StreamCamera(tracked))

    async def scenario():
        body_sent = anyio.Event()
        loop_thread = threading.get_ident()

        async def receive():
            raise AssertionError('ASGI 2.4 response must not poll receive')

        async def send(message):
            if (
                message['type'] == 'http.response.body'
                and message.get('body')
            ):
                body_sent.set()
                await anyio.sleep_forever()

        response = await route()
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                response,
                http_scope('2.4'),
                receive,
                send,
            )
            await body_sent.wait()
            task_group.cancel_scope.cancel()
        return loop_thread

    try:
        loop_thread = anyio.run(scenario)
    finally:
        underlying.close()

    assert finalized == ['finalized']
    assert len(tracked.close_calls) == 1
    assert tracked.close_calls[0] != loop_thread


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


def test_unicam_detection_uses_sysfs_name_and_missing_device_is_safe(
    camera_module, tmp_path
):
    sysfs_root = tmp_path / 'video4linux'
    video0 = sysfs_root / 'video0'
    video0.mkdir(parents=True)
    (video0 / 'name').write_text('unicam-image\n', encoding='utf-8')
    video1 = sysfs_root / 'video1'
    (video1 / 'device').mkdir(parents=True)
    (video1 / 'name').write_text('camera capture\n', encoding='utf-8')
    unicam_driver = tmp_path / 'drivers' / 'unicam'
    unicam_driver.mkdir(parents=True)
    (video1 / 'device' / 'driver').symlink_to(
        unicam_driver,
        target_is_directory=True,
    )

    assert camera_module._is_unicam_device(
        '/dev/video0',
        sysfs_root=sysfs_root,
    ) is True
    assert camera_module._is_unicam_device(
        '/dev/video1',
        sysfs_root=sysfs_root,
    ) is True
    assert camera_module._is_unicam_device(
        '/dev/video9',
        sysfs_root=sysfs_root,
    ) is False


def test_viewer_entry_and_disconnect_do_not_wait_for_blocked_open(camera_module):
    open_started = threading.Event()
    release_open = threading.Event()
    viewer_entered = threading.Event()
    allow_disconnect = threading.Event()
    viewer_finished = threading.Event()
    cam = camera_module.Camera(idle_close_s=-1.0)

    def blocked_open():
        open_started.set()
        assert release_open.wait(timeout=1.0)
        return False

    def view_camera():
        with cam.viewer():
            viewer_entered.set()
            assert allow_disconnect.wait(timeout=1.0)
        viewer_finished.set()

    cam._open = blocked_open
    viewer_thread = threading.Thread(target=view_camera, daemon=True)
    viewer_thread.start()
    assert open_started.wait(timeout=0.5)
    entered_while_open_blocked = viewer_entered.wait(timeout=0.3)
    allow_disconnect.set()
    disconnected_while_open_blocked = viewer_finished.wait(timeout=0.3)
    viewer_count_after_disconnect = cam.viewer_count

    release_open.set()
    viewer_thread.join(timeout=0.5)
    cam.stop()

    assert entered_while_open_blocked is True
    assert disconnected_while_open_blocked is True
    assert viewer_count_after_disconnect == 0
    assert viewer_thread.is_alive() is False


def test_status_remains_responsive_while_worker_open_is_blocked(camera_module):
    open_started = threading.Event()
    release_open = threading.Event()
    viewer_entered = threading.Event()
    release_viewer = threading.Event()
    status_finished = threading.Event()
    statuses = []
    cam = camera_module.Camera()

    def blocked_open():
        open_started.set()
        assert release_open.wait(timeout=1.0)
        return False

    def view_camera():
        with cam.viewer():
            viewer_entered.set()
            release_viewer.wait(timeout=1.0)

    def read_status():
        statuses.append(cam.status())
        status_finished.set()

    cam._open = blocked_open
    viewer_thread = threading.Thread(target=view_camera, daemon=True)
    viewer_thread.start()
    assert open_started.wait(timeout=0.5)
    status_thread = threading.Thread(target=read_status, daemon=True)
    status_thread.start()
    status_returned_while_open_blocked = status_finished.wait(timeout=0.3)

    release_open.set()
    release_viewer.set()
    viewer_thread.join(timeout=0.5)
    status_thread.join(timeout=0.5)
    cam.stop()

    assert status_returned_while_open_blocked is True
    assert status_thread.is_alive() is False
    assert statuses and statuses[0]['open'] is False


def test_disconnected_viewer_causes_inflight_candidate_to_be_discarded(
    camera_module, monkeypatch
):
    start_blocked = threading.Event()
    release_start = threading.Event()
    candidate_closed = threading.Event()
    capture_called = threading.Event()
    release_capture = threading.Event()

    class SlowStartingPicamera:
        def create_video_configuration(self, **_kwargs):
            return object()

        def configure(self, _config):
            pass

        def start(self):
            start_blocked.set()
            assert release_start.wait(timeout=1.0)

        def capture_array(self, *, wait):
            capture_called.set()
            assert release_capture.wait(timeout=1.0)
            raise TimeoutError('test cleanup')

        def cancel_all_and_flush(self):
            release_capture.set()

        def stop(self):
            pass

        def close(self):
            candidate_closed.set()

    monkeypatch.setattr(camera_module, '_Picamera2', SlowStartingPicamera)
    cam = camera_module.Camera(prefer_picamera2=True)
    viewer = cam.viewer()
    viewer.__enter__()
    assert start_blocked.wait(timeout=0.5)
    viewer.__exit__(None, None, None)
    assert cam.viewer_count == 0

    release_start.set()
    discarded_before_forced_stop = candidate_closed.wait(timeout=0.3)
    capture_started_before_forced_stop = capture_called.is_set()
    try:
        assert discarded_before_forced_stop is True
        assert capture_started_before_forced_stop is False
        assert cam._backend == 'none'
        assert cam._picam is None
    finally:
        cam.stop()


def test_stale_candidate_failure_cannot_mutate_replacement_or_try_fallback(
    camera_module, monkeypatch
):
    start_blocked = threading.Event()
    release_start = threading.Event()
    candidate_closed = threading.Event()
    cv2_calls = []

    class FailingPicamera:
        def create_video_configuration(self, **_kwargs):
            return object()

        def configure(self, _config):
            pass

        def start(self):
            start_blocked.set()
            assert release_start.wait(timeout=1.0)
            raise RuntimeError('stale candidate failed')

        def close(self):
            candidate_closed.set()

    def create_cv2(*_args):
        cv2_calls.append('constructed')
        return StubVideoCapture()

    monkeypatch.setattr(camera_module, '_Picamera2', FailingPicamera)
    monkeypatch.setattr(camera_module.cv2, 'VideoCapture', create_cv2)
    cam = camera_module.Camera(prefer_picamera2=True)
    viewer = cam.viewer()
    viewer.__enter__()
    assert start_blocked.wait(timeout=0.5)
    old_worker = cam._capture_thread
    assert old_worker is not None
    replacement = object()
    replacement_owner = object()
    with cam._lock:
        cam._backend = 'picamera2'
        cam._picam = replacement
        cam._capture_thread = replacement_owner
        with cam._frame_cond:
            cam._latest_jpeg = b'replacement-frame'
            cam._frame_seq = 17
            cam._last_frame_t = 456.0
            cam._last_error = 'replacement still starting'

    release_start.set()
    assert candidate_closed.wait(timeout=0.5)
    old_worker.join(timeout=0.5)
    viewer.__exit__(None, None, None)

    assert old_worker.is_alive() is False
    assert cv2_calls == []
    assert cam._backend == 'picamera2'
    assert cam._picam is replacement
    assert cam._capture_thread is replacement_owner
    with cam._frame_cond:
        assert cam._latest_jpeg == b'replacement-frame'
        assert cam._frame_seq == 17
        assert cam._last_frame_t == 456.0
        assert cam._last_error == 'replacement still starting'


def test_unicam_preference_opens_picamera_first_without_initial_backoff(
    camera_module, monkeypatch
):
    calls = []

    class ImmediatePicamera:
        def __init__(self):
            calls.append('picamera-construct')

        def create_video_configuration(self, **_kwargs):
            return object()

        def configure(self, _config):
            calls.append('picamera-configure')

        def start(self):
            calls.append('picamera-start')

        def capture_array(self, *, wait):
            calls.append(('picamera-capture', wait))
            return RGBFrame()

        def cancel_all_and_flush(self):
            calls.append('picamera-cancel')

        def stop(self):
            calls.append('picamera-stop')

        def close(self):
            calls.append('picamera-close')

    def unexpected_cv2(*_args):
        calls.append('cv2-construct')
        return StubVideoCapture()

    monkeypatch.setattr(camera_module, '_Picamera2', ImmediatePicamera)
    monkeypatch.setattr(camera_module.cv2, 'VideoCapture', unexpected_cv2)
    cam = camera_module.Camera(
        prefer_picamera2=True,
        recovery_backoff_s=1.0,
    )
    try:
        frame = cam.snapshot(timeout=0.5)
    finally:
        cam.stop()

    assert frame == b'jpeg'
    assert calls[0] == 'picamera-construct'
    assert 'cv2-construct' not in calls
    assert cam.viewer_count == 0


def test_unknown_device_retains_cv2_first_open_order(camera_module, monkeypatch):
    calls = []

    class WorkingUsbCapture:
        def isOpened(self):
            return True

        def set(self, _prop, _value):
            return True

        def read(self):
            calls.append('cv2-read')
            return True, object()

        def release(self):
            calls.append('cv2-release')

    def create_cv2(*_args):
        calls.append('cv2-construct')
        return WorkingUsbCapture()

    def unexpected_picamera():
        calls.append('picamera-construct')
        return StubPicamera2()

    monkeypatch.setattr(camera_module.cv2, 'VideoCapture', create_cv2)
    monkeypatch.setattr(camera_module, '_Picamera2', unexpected_picamera)
    cam = camera_module.Camera(
        prefer_picamera2=False,
        recovery_backoff_s=1.0,
    )
    try:
        frame = cam.snapshot(timeout=0.5)
    finally:
        cam.stop()

    assert frame == b'jpeg'
    assert calls[0] == 'cv2-construct'
    assert 'picamera-construct' not in calls
    assert cam.viewer_count == 0


def test_picamera_read_has_numeric_timeout(camera_module):
    fake = RecordingPicamera(array=RGBFrame())
    cam = camera_module.Camera(capture_timeout_s=2.0)
    cam._backend = 'picamera2'
    cam._picam = fake

    ok, frame = cam._read_frame()

    assert ok is True
    assert frame is not None
    assert fake.wait_values == [2.0]


@pytest.mark.parametrize('failure_stage', ('constructor', 'isOpened', 'set', 'read'))
def test_cv2_open_exception_releases_temporary_handle_and_reports_error(
    camera_module, monkeypatch, failure_stage
):
    release_calls = []

    class ExplodingVideoCapture:
        def isOpened(self):
            if failure_stage == 'isOpened':
                raise RuntimeError('isOpened failed')
            return True

        def set(self, _prop, _value):
            if failure_stage == 'set':
                raise RuntimeError('set failed')
            return True

        def read(self):
            if failure_stage == 'read':
                raise RuntimeError('read failed')
            return True, object()

        def release(self):
            release_calls.append('release')

    if failure_stage == 'constructor':
        def create_capture(*_args):
            raise RuntimeError('constructor failed')
    else:
        def create_capture(*_args):
            return ExplodingVideoCapture()

    monkeypatch.setattr(camera_module.cv2, 'VideoCapture', create_capture)
    cam = camera_module.Camera()
    cam._frame_cond = RecordingCondition()

    assert cam._open_cv2() is False
    assert release_calls == ([] if failure_stage == 'constructor' else ['release'])
    assert cam._cap is None
    assert cam._backend == 'none'
    assert cam._last_error == (
        f'cv2 open failed: RuntimeError: {failure_stage} failed'
    )
    assert cam._frame_cond.notify_count == 1


def test_cv2_optional_buffer_size_failure_does_not_abort_open(
    camera_module, monkeypatch
):
    class BufferSizeUnsupportedCapture:
        def __init__(self):
            self.released = False
            self.set_calls = []

        def isOpened(self):
            return True

        def set(self, prop, value):
            self.set_calls.append((prop, value))
            if prop == camera_module.cv2.CAP_PROP_BUFFERSIZE:
                raise RuntimeError('buffer size unsupported')
            return True

        def read(self):
            return True, object()

        def release(self):
            self.released = True

    temporary = BufferSizeUnsupportedCapture()
    monkeypatch.setattr(
        camera_module.cv2,
        'VideoCapture',
        lambda *_args: temporary,
    )
    cam = camera_module.Camera()

    assert cam._open_cv2() is True
    assert cam._backend == 'cv2'
    assert cam._cap is temporary
    assert temporary.released is False
    assert temporary.set_calls == [
        (camera_module.cv2.CAP_PROP_FRAME_WIDTH, cam.width),
        (camera_module.cv2.CAP_PROP_FRAME_HEIGHT, cam.height),
        (camera_module.cv2.CAP_PROP_FPS, cam.fps),
        (camera_module.cv2.CAP_PROP_BUFFERSIZE, 1),
    ]


def test_viewer_preserves_detailed_cv2_open_error_without_fallback(
    camera_module, monkeypatch
):
    def create_capture(*_args):
        raise OSError('video device unavailable')

    monkeypatch.setattr(camera_module.cv2, 'VideoCapture', create_capture)
    monkeypatch.setattr(camera_module, '_HAS_PICAMERA2', False)
    cam = camera_module.Camera(recovery_backoff_s=1.0)
    try:
        with cam.viewer():
            with cam._frame_cond:
                assert cam._frame_cond.wait_for(
                    lambda: cam._last_error is not None,
                    timeout=0.5,
                )
                assert cam._last_error == (
                    'cv2 open failed: OSError: video device unavailable'
                )
            assert cam._capture_thread is not None
            assert cam._capture_thread.is_alive() is True
    finally:
        cam.stop()


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


def test_status_waits_for_a_concurrent_close_transaction(camera_module):
    fake_clock = FakeClock(now=10.0)
    cam = camera_module.Camera(clock=fake_clock)
    cam._backend = 'picamera2'
    cam._picam = object()
    cam._viewers = 1
    cam._last_frame_t = 9.0
    cam._frame_seq = 3
    cam._last_error = None
    cam._lock = AttemptRecordingLock()
    finished = threading.Event()
    result = []

    def read_status():
        result.append(cam.status())
        finished.set()
        cam._lock.progress.set()

    cam._lock.acquire()
    reader = threading.Thread(target=read_status, daemon=True)
    try:
        reader.start()
        assert cam._lock.progress.wait(timeout=1.0)
        cam._close()
    finally:
        cam._lock.release()
    reader.join(timeout=1.0)

    assert cam._lock.enter_attempted.is_set() is True
    assert finished.is_set() is True
    assert result[0]['open'] is False
    assert result[0]['healthy'] is False
    assert result[0]['backend'] == 'none'
    assert result[0]['viewers'] == 1
    assert result[0]['frame_sequence'] == 3
    assert result[0]['last_frame_age_s'] == 1.0


def test_idle_exit_and_worker_clear_are_atomic_for_restart(
    camera_module, monkeypatch
):
    cam = camera_module.Camera(idle_close_s=-1.0)
    cam._backend = 'picamera2'
    cam._picam = object()
    gap_lock = TeardownGapLock()
    cam._lock = gap_lock
    close_calls = 0
    original_close = cam._close

    def recording_close():
        nonlocal close_calls
        close_calls += 1
        original_close()

    cam._close = recording_close
    worker = threading.Thread(target=cam._capture_loop, daemon=True)
    gap_lock.pause_thread = worker
    cam._capture_thread = worker
    worker.start()
    assert gap_lock.paused.wait(timeout=1.0)

    class ReplacementThread:
        instances = []

        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon
            self.started = False
            type(self).instances.append(self)

        def start(self):
            self.viewer_count_at_start = cam._viewers
            self.started = True

        def is_alive(self):
            return self.started

    monkeypatch.setattr(camera_module.threading, 'Thread', ReplacementThread)
    viewer = cam.viewer()
    viewer.__enter__()
    try:
        gap_lock.resume.set()
        worker.join(timeout=1.0)

        assert gap_lock.timed_out.is_set() is False
        assert worker.is_alive() is False
        assert close_calls == 1
        assert len(ReplacementThread.instances) == 1
        replacement = ReplacementThread.instances[0]
        assert replacement.started is True
        assert replacement.viewer_count_at_start == 1
        assert cam._capture_thread is replacement
        assert cam._backend == 'none'
        assert cam._picam is None
    finally:
        viewer.__exit__(None, None, None)


def test_close_clears_cached_frame_under_condition_and_notifies(camera_module):
    cam = camera_module.Camera()
    cam._backend = 'picamera2'
    cam._picam = object()
    cam._latest_jpeg = b'cached'
    cam._frame_cond = RecordingCondition()

    with cam._lock:
        cam._close()

    assert cam._latest_jpeg is None
    assert cam._frame_cond.enter_count == 1
    assert cam._frame_cond.notify_count == 1


def test_read_failure_updates_error_under_condition_and_notifies(
    camera_module, monkeypatch
):
    cam = camera_module.Camera()
    cam._backend = 'picamera2'
    cam._picam = object()
    cam._viewers = 1
    cam._frame_cond = RecordingCondition()
    cam._close = lambda: None
    cam._capture_thread = threading.current_thread()

    def failed_read(*_args):
        cam._stop.set()
        return False, None

    cam._read_frame = failed_read
    monkeypatch.setattr(camera_module.time, 'sleep', lambda _seconds: None)

    cam._capture_loop()

    assert cam._last_error == 'read failed'
    assert cam._frame_cond.enter_count == 1
    assert cam._frame_cond.notify_count == 1


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
    cam._close = lambda: None
    cam._capture_thread = threading.current_thread()
    read_count = 0

    def read_frame(*_args):
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


def test_picamera_timeout_recovers_and_clears_error_only_after_publication(
    camera_module, monkeypatch
):
    reopened = threading.Event()
    release_replacement_frame = threading.Event()
    failed_calls = []
    replacement_calls = []

    class FailedPicamera:
        def create_video_configuration(self, **_kwargs):
            return object()

        def configure(self, _config):
            failed_calls.append('configure')

        def start(self):
            failed_calls.append('start')

        def capture_array(self, *, wait):
            assert wait == 0.02
            raise TimeoutError('capture deadline expired')

        def cancel_all_and_flush(self):
            failed_calls.append('cancel_all_and_flush')

        def stop(self):
            failed_calls.append('stop')

        def close(self):
            failed_calls.append('close')

    class ReplacementPicamera:
        def create_video_configuration(self, **_kwargs):
            return object()

        def configure(self, _config):
            replacement_calls.append('configure')

        def start(self):
            replacement_calls.append('start')
            reopened.set()

        def capture_array(self, *, wait):
            assert wait == 0.02
            assert release_replacement_frame.wait(timeout=1.0)
            return RGBFrame()

        def stop(self):
            replacement_calls.append('stop')

        def close(self):
            replacement_calls.append('close')

    backends = iter((FailedPicamera(), ReplacementPicamera()))
    monkeypatch.setattr(camera_module, '_Picamera2', lambda: next(backends))
    monkeypatch.setattr(StubVideoCapture, 'opened', False)
    cam = camera_module.Camera(
        capture_timeout_s=0.02,
        recovery_backoff_s=0.01,
    )
    wait_timeouts = []
    original_wait = cam._stop.wait

    def recording_wait(timeout=None):
        wait_timeouts.append(timeout)
        return original_wait(timeout)

    monkeypatch.setattr(cam._stop, 'wait', recording_wait)

    with cam.viewer():
        assert reopened.wait(timeout=1.0)
        expected_error = (
            'picamera2 capture failed: TimeoutError: '
            'capture deadline expired'
        )
        assert cam.status()['error'] == expected_error
        assert failed_calls == [
            'configure',
            'start',
            'cancel_all_and_flush',
            'stop',
            'close',
        ]
        assert cam._frame_seq == 0
        assert cam.recovery_backoff_s in wait_timeouts

        release_replacement_frame.set()
        with cam._frame_cond:
            assert cam._frame_cond.wait_for(
                lambda: cam._frame_seq > 0,
                timeout=1.0,
            )
            assert cam._last_error is None

    cam.stop()


def test_stop_cancels_blocked_picamera_then_worker_closes_backend(camera_module):
    capture_started = threading.Event()
    capture_unblock = threading.Event()
    cancel_observed = threading.Event()
    operations = []

    class BlockingPicamera:
        def capture_array(self, *, wait):
            assert wait == 0.05
            operations.append(('capture-start', threading.current_thread().name))
            capture_started.set()
            assert capture_unblock.wait(timeout=1.0)
            operations.append(('capture-unblocked', threading.current_thread().name))
            raise TimeoutError('capture cancelled')

        def cancel_all_and_flush(self):
            operations.append(('cancel', threading.current_thread().name))
            cancel_observed.set()
            capture_unblock.set()

        def stop(self):
            operations.append(('stop', threading.current_thread().name))

        def close(self):
            operations.append(('close', threading.current_thread().name))

    cam = camera_module.Camera(capture_timeout_s=0.05)
    cam._backend = 'picamera2'
    cam._picam = BlockingPicamera()
    cam._viewers = 1
    worker = threading.Thread(
        target=cam._capture_loop,
        daemon=True,
        name='camera-capture-worker',
    )
    cam._capture_thread = worker
    join_timeouts = []
    original_join = worker.join

    def recording_join(timeout=None):
        join_timeouts.append(timeout)
        return original_join(timeout)

    worker.join = recording_join
    worker.start()
    assert capture_started.wait(timeout=0.5)

    stopper = threading.Thread(target=cam.stop, name='camera-stopper')
    stopper.start()
    cancelled_before_forced_cleanup = cancel_observed.wait(timeout=0.3)
    if not cancelled_before_forced_cleanup:
        capture_unblock.set()
    stopper.join(timeout=0.75)
    original_join(timeout=0.5)

    assert cancelled_before_forced_cleanup is True
    assert stopper.is_alive() is False
    assert worker.is_alive() is False
    assert join_timeouts and join_timeouts[0] > cam.capture_timeout_s
    first_cancel = operations.index(('cancel', 'camera-stopper'))
    unblocked = operations.index(('capture-unblocked', 'camera-capture-worker'))
    stopped = operations.index(('stop', 'camera-capture-worker'))
    closed = operations.index(('close', 'camera-capture-worker'))
    assert first_cancel < unblocked < stopped < closed
    assert cam._capture_thread is None
    assert cam._backend == 'none'
    assert cam._picam is None


def test_stop_does_not_close_backend_while_capture_worker_is_alive(camera_module):
    capture_started = threading.Event()
    release_capture = threading.Event()
    calls = []

    class StillBlockedPicamera:
        def capture_array(self, *, wait):
            assert wait == 0.05
            capture_started.set()
            assert release_capture.wait(timeout=1.0)
            raise TimeoutError('released after stop returned')

        def cancel_all_and_flush(self):
            calls.append(('cancel', threading.current_thread().name))

        def stop(self):
            calls.append(('stop', threading.current_thread().name))

        def close(self):
            calls.append(('close', threading.current_thread().name))

    cam = camera_module.Camera(capture_timeout_s=0.05)
    backend = StillBlockedPicamera()
    cam._backend = 'picamera2'
    cam._picam = backend
    cam._viewers = 1
    worker = threading.Thread(
        target=cam._capture_loop,
        daemon=True,
        name='camera-capture-worker',
    )
    cam._capture_thread = worker
    original_join = worker.join
    join_timeouts = []

    def incomplete_join(timeout=None):
        join_timeouts.append(timeout)

    worker.join = incomplete_join
    worker.start()
    assert capture_started.wait(timeout=0.5)

    cam.stop()

    assert join_timeouts and join_timeouts[0] > cam.capture_timeout_s
    assert worker.is_alive() is True
    assert calls == [('cancel', threading.current_thread().name)]
    assert cam._backend == 'picamera2'
    assert cam._picam is backend
    assert cam._capture_thread is worker

    release_capture.set()
    original_join(timeout=0.5)
    assert worker.is_alive() is False
    assert calls[-2:] == [
        ('stop', 'camera-capture-worker'),
        ('close', 'camera-capture-worker'),
    ]


def test_stale_failed_read_does_not_close_replacement_backend(camera_module):
    read_started = threading.Event()
    release_failed_read = threading.Event()
    old_calls = []
    replacement_calls = []

    class OldPicamera:
        def capture_array(self, *, wait):
            assert wait == 2.0
            read_started.set()
            assert release_failed_read.wait(timeout=1.0)
            raise TimeoutError('old read timed out')

        def cancel_all_and_flush(self):
            old_calls.append('cancel_all_and_flush')

        def stop(self):
            old_calls.append('stop')

        def close(self):
            old_calls.append('close')

    class ReplacementPicamera:
        def cancel_all_and_flush(self):
            replacement_calls.append('cancel_all_and_flush')

        def stop(self):
            replacement_calls.append('stop')

        def close(self):
            replacement_calls.append('close')

    old = OldPicamera()
    replacement = ReplacementPicamera()
    cam = camera_module.Camera()
    cam._backend = 'picamera2'
    cam._picam = old
    cam._viewers = 1
    worker = threading.Thread(target=cam._capture_loop, daemon=True)
    cam._capture_thread = worker
    worker.start()
    assert read_started.wait(timeout=0.5)

    with cam._lock:
        cam._close()
        cam._backend = 'picamera2'
        cam._picam = replacement
        with cam._frame_cond:
            cam._last_error = 'replacement still starting'
    cam._stop.set()
    release_failed_read.set()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert old_calls == ['stop', 'close']
    assert replacement_calls == []
    assert cam._backend == 'picamera2'
    assert cam._picam is replacement
    assert cam._last_error == 'replacement still starting'


def test_stale_worker_cannot_adopt_backend_owned_by_replacement_worker(
    camera_module
):
    old_read_started = threading.Event()
    release_old_read = threading.Event()
    replacement_read_started = threading.Event()
    release_replacement_read = threading.Event()
    replacement_calls = []

    class OldPicamera:
        def capture_array(self, *, wait):
            assert wait == 2.0
            old_read_started.set()
            assert release_old_read.wait(timeout=1.0)
            raise TimeoutError('stale timeout')

        def stop(self):
            pass

        def close(self):
            pass

    class ReplacementPicamera:
        def capture_array(self, *, wait):
            assert wait == 2.0
            replacement_read_started.set()
            assert release_replacement_read.wait(timeout=1.0)
            return RGBFrame()

        def cancel_all_and_flush(self):
            replacement_calls.append('cancel_all_and_flush')

        def stop(self):
            replacement_calls.append('stop')

        def close(self):
            replacement_calls.append('close')

    cam = camera_module.Camera()
    cam._backend = 'picamera2'
    cam._picam = OldPicamera()
    cam._viewers = 1
    old_worker = threading.Thread(target=cam._capture_loop, daemon=True)
    cam._capture_thread = old_worker
    old_worker.start()
    assert old_read_started.wait(timeout=0.5)

    replacement = ReplacementPicamera()
    replacement_owner = object()
    with cam._lock:
        cam._backend = 'picamera2'
        cam._picam = replacement
        cam._capture_thread = replacement_owner
        with cam._frame_cond:
            cam._latest_jpeg = b'replacement-cached-frame'
            cam._frame_seq = 11
            cam._last_frame_t = 123.0
            cam._last_error = 'replacement still starting'
    release_old_read.set()
    old_worker.join(timeout=0.5)
    exited_without_forced_stop = not old_worker.is_alive()
    if old_worker.is_alive():
        cam._stop.set()
        release_replacement_read.set()
        old_worker.join(timeout=0.5)

    assert exited_without_forced_stop is True
    assert old_worker.is_alive() is False
    assert replacement_read_started.is_set() is False
    assert replacement_calls == []
    assert cam._backend == 'picamera2'
    assert cam._picam is replacement
    assert cam._capture_thread is replacement_owner
    with cam._frame_cond:
        assert cam._latest_jpeg == b'replacement-cached-frame'
        assert cam._frame_seq == 11
        assert cam._last_frame_t == 123.0
        assert cam._last_error == 'replacement still starting'


def test_stale_worker_reads_snapshotted_handle_not_replacement(camera_module):
    old_reads = []
    replacement_reads = []

    class OldPicamera:
        def capture_array(self, *, wait):
            old_reads.append(wait)
            return RGBFrame()

    class ReplacementPicamera:
        def capture_array(self, *, wait):
            replacement_reads.append(wait)
            return RGBFrame()

    cam = camera_module.Camera()
    old = OldPicamera()
    replacement = ReplacementPicamera()
    cam._backend = 'picamera2'
    cam._picam = old
    cam._viewers = 1
    gap_lock = TeardownGapLock()
    cam._lock = gap_lock
    old_worker = threading.Thread(target=cam._capture_loop, daemon=True)
    replacement_owner = object()
    gap_lock.pause_thread = old_worker
    cam._capture_thread = old_worker
    old_worker.start()
    assert gap_lock.paused.wait(timeout=0.5)

    with cam._lock:
        cam._backend = 'picamera2'
        cam._picam = replacement
        cam._capture_thread = replacement_owner
    gap_lock.resume.set()
    old_worker.join(timeout=0.5)

    assert gap_lock.timed_out.is_set() is False
    assert old_worker.is_alive() is False
    assert old_reads == [2.0]
    assert replacement_reads == []
    assert cam._backend == 'picamera2'
    assert cam._picam is replacement
    assert cam._capture_thread is replacement_owner


def test_initial_open_failure_keeps_viewer_owned_worker_retrying(
    camera_module, monkeypatch
):
    cam = camera_module.Camera(recovery_backoff_s=0.01)
    retried = threading.Event()
    attempts = []
    retry_workers = []
    wait_timeouts = []
    original_wait = cam._stop.wait

    def recording_wait(timeout=None):
        wait_timeouts.append(timeout)
        return original_wait(timeout)

    monkeypatch.setattr(cam._stop, 'wait', recording_wait)

    def open_camera():
        attempts.append(cam._viewers)
        if len(attempts) == 1:
            return False
        cam._backend = 'picamera2'
        cam._picam = object()
        retry_workers.append(cam._capture_thread)
        retried.set()
        cam._stop.set()
        return True

    cam._open = open_camera
    cam._close = lambda: None

    with cam.viewer():
        assert retried.wait(timeout=1.0)
        worker = retry_workers[0]
        assert worker is not None
        worker.join(timeout=1.0)
        assert worker.is_alive() is False

    assert attempts == [1, 1]
    assert cam.recovery_backoff_s in wait_timeouts
    assert cam.viewer_count == 0


def test_initial_open_exception_still_starts_worker_and_recovers(
    camera_module
):
    reopened = threading.Event()
    allow_frame = threading.Event()
    attempts = []

    class RecoveredPicamera:
        def capture_array(self, *, wait):
            assert wait == 0.02
            assert allow_frame.wait(timeout=1.0)
            return RGBFrame()

        def cancel_all_and_flush(self):
            allow_frame.set()

        def stop(self):
            pass

        def close(self):
            pass

    cam = camera_module.Camera(
        capture_timeout_s=0.02,
        recovery_backoff_s=0.01,
    )

    def open_camera():
        attempts.append(cam._viewers)
        if len(attempts) == 1:
            raise RuntimeError('initial open exploded')
        cam._backend = 'picamera2'
        cam._picam = RecoveredPicamera()
        reopened.set()
        return True

    cam._open = open_camera
    try:
        with cam.viewer():
            assert reopened.wait(timeout=0.5)
            assert cam._last_error == (
                'camera open failed: RuntimeError: initial open exploded'
            )
            allow_frame.set()
            with cam._frame_cond:
                assert cam._frame_cond.wait_for(
                    lambda: cam._frame_seq > 0,
                    timeout=0.5,
                )
                assert cam._last_error is None
    finally:
        cam.stop()

    assert attempts == [1, 1]


def test_retry_open_exception_keeps_worker_alive_until_later_success(
    camera_module
):
    reopened = threading.Event()
    allow_frame = threading.Event()
    attempts = []

    class RecoveredPicamera:
        def capture_array(self, *, wait):
            assert wait == 0.02
            assert allow_frame.wait(timeout=1.0)
            return RGBFrame()

        def cancel_all_and_flush(self):
            allow_frame.set()

        def stop(self):
            pass

        def close(self):
            pass

    cam = camera_module.Camera(
        capture_timeout_s=0.02,
        recovery_backoff_s=0.01,
    )

    def open_camera():
        attempts.append(cam._viewers)
        if len(attempts) == 1:
            return False
        if len(attempts) == 2:
            raise RuntimeError('retry open exploded')
        cam._backend = 'picamera2'
        cam._picam = RecoveredPicamera()
        reopened.set()
        return True

    cam._open = open_camera
    try:
        with cam.viewer():
            assert reopened.wait(timeout=0.5)
            worker = cam._capture_thread
            assert worker is not None
            assert worker.is_alive() is True
            assert cam._last_error == (
                'camera open failed: RuntimeError: retry open exploded'
            )
            allow_frame.set()
            with cam._frame_cond:
                assert cam._frame_cond.wait_for(
                    lambda: cam._frame_seq > 0,
                    timeout=0.5,
                )
                assert cam._last_error is None
    finally:
        cam.stop()

    assert attempts == [1, 1, 1]


@pytest.mark.parametrize(
    'failure_stage',
    ('create_video_configuration', 'configure', 'start'),
)
def test_partial_picamera_startup_failure_closes_temporary_camera_and_reports_error(
    camera_module, monkeypatch, failure_stage
):
    calls = []

    class PartiallyStartedPicamera:
        def create_video_configuration(self, **_kwargs):
            calls.append('create_video_configuration')
            if failure_stage == 'create_video_configuration':
                raise RuntimeError('create_video_configuration failed')
            return object()

        def configure(self, _config):
            calls.append('configure')
            if failure_stage == 'configure':
                raise RuntimeError('configure failed')

        def start(self):
            calls.append('start')
            if failure_stage == 'start':
                raise RuntimeError('start failed')

        def close(self):
            calls.append('close')

    temporary = PartiallyStartedPicamera()
    monkeypatch.setattr(camera_module, '_Picamera2', lambda: temporary)
    cam = camera_module.Camera()

    assert cam._open_picamera2() is False
    assert calls[-1] == 'close'
    assert cam._picam is None
    assert cam._backend == 'none'
    assert cam.status()['error'] == f'picamera2: {failure_stage} failed'


@pytest.mark.parametrize('backend', ('cv2', 'picamera2'))
def test_successful_reopen_preserves_recovery_error_until_frame_publication(
    camera_module, monkeypatch, backend
):
    class OpenPicamera:
        def create_video_configuration(self, **_kwargs):
            return object()

        def configure(self, _config):
            pass

        def start(self):
            pass

    cam = camera_module.Camera()
    cam._last_error = 'picamera2 capture failed: TimeoutError: stalled'
    if backend == 'cv2':
        monkeypatch.setattr(StubVideoCapture, 'opened', True)
        monkeypatch.setattr(StubVideoCapture, 'read_result', (True, object()))
        opened = cam._open_cv2()
    else:
        monkeypatch.setattr(camera_module, '_Picamera2', OpenPicamera)
        opened = cam._open_picamera2()

    assert opened is True
    assert cam.status()['error'] == (
        'picamera2 capture failed: TimeoutError: stalled'
    )

    with cam._frame_cond:
        cam._latest_jpeg = b'published'
        cam._frame_seq += 1
        cam._last_frame_t = cam._clock()
        cam._last_error = None
        cam._frame_cond.notify_all()

    assert cam.status()['error'] is None


def test_mjpeg_generator_uses_frame_sequence_for_identical_bytes(
    camera_module
):
    cam = camera_module.Camera(stream_stall_timeout_s=0.5)
    cam._ensure_running = lambda: None
    cam._frame_cond = StreamWaitCondition()
    shared_jpeg = b'same-jpeg-object'
    cam._latest_jpeg = shared_jpeg
    cam._frame_seq = 7
    generator = cam.mjpeg_generator()
    chunks = []
    errors = []

    def consume_one():
        try:
            chunks.append(next(generator))
        except BaseException as exc:
            errors.append(exc)

    for sequence in (8, 9):
        cam._frame_cond.wait_started.clear()
        consumer = threading.Thread(target=consume_one, daemon=True)
        consumer.start()
        assert cam._frame_cond.wait_started.wait(timeout=0.5)
        with cam._frame_cond:
            cam._latest_jpeg = shared_jpeg
            cam._frame_seq = sequence
            cam._frame_cond.notify_all()
        consumer.join(timeout=0.5)
        if consumer.is_alive():
            cam._stop.set()
            with cam._frame_cond:
                cam._frame_cond.notify_all()
            consumer.join(timeout=0.5)
        assert consumer.is_alive() is False

    generator.close()
    expected = (
        b'--frame\r\nContent-Type: image/jpeg\r\n'
        b'Content-Length: 16\r\n\r\nsame-jpeg-object\r\n'
    )
    assert errors == []
    assert chunks == [expected, expected]
    assert cam.viewer_count == 0


def test_mjpeg_generator_stall_releases_viewer_and_consumer_thread(
    camera_module
):
    cam = camera_module.Camera(stream_stall_timeout_s=0.03)
    cam._ensure_running = lambda: None
    generator = cam.mjpeg_generator()
    stopped = []

    def consume():
        try:
            next(generator)
        except StopIteration:
            stopped.append(True)

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    consumer.join(timeout=0.5)
    exited_before_forced_cleanup = not consumer.is_alive()
    if consumer.is_alive():
        cam._stop.set()
        with cam._frame_cond:
            cam._frame_cond.notify_all()
        consumer.join(timeout=0.5)

    assert exited_before_forced_cleanup is True
    assert consumer.is_alive() is False
    assert stopped == [True]
    assert cam.viewer_count == 0
    assert cam._capture_thread is None


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
