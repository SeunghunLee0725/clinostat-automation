# Camera Stream Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the deployed Picamera2 MJPEG stream and make future capture stalls recover automatically without leaking viewers.

**Architecture:** Preserve the existing process-wide `Camera` singleton and capture thread. Bound Picamera2 reads, record frame sequence/freshness, recover a timed-out backend through Picamera2's cancel/close/reopen path, and make stalled stream iterators terminate so their viewer contexts unwind.

**Tech Stack:** Python 3.13 on Raspberry Pi, Python 3.14 test environment, Picamera2, OpenCV, FastAPI/Starlette, pytest, systemd.

---

## File map

- Create `integration/clinostat_extension/work/camera.py`: canonical editable copy of the deployed camera module plus bounded capture and recovery logic.
- Create `integration/clinostat_extension/tests/test_camera.py`: fake-backend regression tests that do not require cv2, Picamera2, or camera hardware.
- Modify `integration/deploy/deploy_to_pi.sh`: include `camera.py` in future full clinostat deployments; do not run this general-purpose script for the incident deployment.

### Task 1: Canonicalize the deployed camera module

**Files:**
- Create: `integration/clinostat_extension/tests/test_camera.py`
- Create: `integration/clinostat_extension/work/camera.py`
- Modify: `integration/deploy/deploy_to_pi.sh:32-36`

- [ ] **Step 1: Write the failing source/deployment contract test**

Create the test module with path constants and this first regression:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK_CAMERA = ROOT / "work" / "camera.py"
DEPLOY_SCRIPT = ROOT.parents[0] / "deploy" / "deploy_to_pi.sh"


def test_camera_module_is_canonical_and_deployed():
    assert WORK_CAMERA.is_file(), "camera.py is missing from editable work sources"
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "clinostat_extension/work/camera.py:$CLINOSTAT_ROOT/camera.py" in deploy
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd integration
'/Users/lee_ai_labtop/Desktop/과제수행_KIMS/(25-27)우주바이오/1_research_results/clinostat-automation/integration/.venv/bin/python' -m pytest \
  clinostat_extension/tests/test_camera.py::test_camera_module_is_canonical_and_deployed -q
```

Expected: FAIL because `work/camera.py` does not exist and the deploy manifest omits it.

- [ ] **Step 3: Add the audited production baseline and deployment entry**

Fetch `/home/aiworker-1/clinostat/camera.py` to a temporary path, verify its SHA-256 is `2bb253b194215994c3c7d30a2a5e9c7f723de2e629bc008efe22f5158a0698ad`, and add the same content to `work/camera.py` using `apply_patch`.

Add this pair to `EXTENSION_PAIRS`:

```bash
"clinostat_extension/work/camera.py:$CLINOSTAT_ROOT/camera.py"
```

Extend the script's printed rollback command so it also restores
`$BACKUP_ROOT/clinostat/camera.py` to `$CLINOSTAT_ROOT/camera.py`.

- [ ] **Step 4: Run the contract test and verify GREEN**

Expected: PASS.

- [ ] **Step 5: Commit the canonical source**

```bash
git add integration/clinostat_extension/work/camera.py \
  integration/clinostat_extension/tests/test_camera.py \
  integration/deploy/deploy_to_pi.sh
git commit -m "test: track deployed camera module"
```

### Task 2: Bound Picamera2 reads and report real frame health

**Files:**
- Modify: `integration/clinostat_extension/tests/test_camera.py`
- Modify: `integration/clinostat_extension/work/camera.py`

- [ ] **Step 1: Add an import fixture with fake hardware modules**

Before importing `work/camera.py`, install small `cv2`, `picamera2`, and `numpy`
module stubs in `sys.modules`. The cv2 stub must expose the constants used by
`Camera`, a controllable `VideoCapture`, and `imencode`; the Picamera2 stub
exposes a replaceable `Picamera2` class; the NumPy stub provides
`ascontiguousarray`. Load the module through
`importlib.util.spec_from_file_location` under a unique module name per test.

- [ ] **Step 2: Write failing tests for timeout, health, and fresh snapshots**

Add these behaviors:

```python
def test_picamera_read_has_numeric_timeout(camera_module):
    fake = RecordingPicamera(array=RGB_FRAME)
    cam = camera_module.Camera(capture_timeout_s=2.0)
    cam._backend = "picamera2"
    cam._picam = fake
    ok, _ = cam._read_frame()
    assert ok is True
    assert fake.wait_values == [2.0]


def test_open_but_stale_camera_is_unhealthy(camera_module, fake_clock):
    cam = camera_module.Camera(unhealthy_after_s=4.0, clock=fake_clock)
    cam._backend = "picamera2"
    cam._picam = object()
    cam._last_frame_t = fake_clock() - 5.0
    status = cam.status()
    assert status["open"] is True
    assert status["healthy"] is False
    assert status["last_frame_age_s"] == 5.0
    assert status["frame_sequence"] == 0


def test_snapshot_does_not_return_cached_frame(camera_module):
    cam = camera_module.Camera()
    cam._ensure_running = lambda: None
    cam._latest_jpeg = b"stale"
    cam._frame_seq = 7
    assert cam.snapshot(timeout=0.01) is None
```

Also assert `last_frame_age_s is None` before the first frame, that a recent
successful frame with no error is healthy, and that every published frame is
visible through the public `status()["frame_sequence"]` field.

- [ ] **Step 3: Run the focused tests and verify RED**

Expected failures: constructor arguments do not exist, Picamera2 receives no `wait`, status lacks freshness fields, and snapshot returns the cached JPEG.

- [ ] **Step 4: Implement the minimal freshness contract**

Add constructor parameters with production defaults:

```python
capture_timeout_s=2.0,
unhealthy_after_s=4.0,
recovery_backoff_s=0.5,
stream_stall_timeout_s=10.0,
clock=time.monotonic,
```

Track `_frame_seq`, `_last_frame_t`, and `_clock`, and expose `_frame_seq` as
`status()["frame_sequence"]`. Call:

```python
rgb = self._picam.capture_array(wait=self.capture_timeout_s)
```

On each successfully encoded frame, update JPEG, increment sequence, set the timestamp, clear the prior error, and notify clients while holding `_frame_cond`.

Implement health as:

```python
age = None if self._last_frame_t is None else max(0.0, self._clock() - self._last_frame_t)
healthy = self.is_open and age is not None and age <= self.unhealthy_after_s and self._last_error is None
```

Implement fresh-frame waiting by recording the starting sequence and using `Condition.wait_for` until `_frame_seq` advances or the timeout expires. `snapshot()` must call this path inside `viewer()`.

- [ ] **Step 5: Run focused and full tests and verify GREEN**

Run the camera test file, then the complete integration suite. Expected: all tests pass.

- [ ] **Step 6: Commit frame freshness**

```bash
git add integration/clinostat_extension/work/camera.py \
  integration/clinostat_extension/tests/test_camera.py
git commit -m "fix: bound camera reads and report freshness"
```

### Task 3: Recover timed-out capture and release stalled viewers

**Files:**
- Modify: `integration/clinostat_extension/tests/test_camera.py`
- Modify: `integration/clinostat_extension/work/camera.py`

- [ ] **Step 1: Write failing recovery and stream lifecycle tests**

Cover these behaviors with fake backends and short injected timings:

```python
def test_capture_timeout_cancels_closes_and_reopens(camera_module):
    # First read raises TimeoutError; the fake replacement returns one frame.
    # Assert cancel_all_and_flush(), stop(), and close() were called on the
    # failed backend, _open() was retried while a viewer existed, and sequence
    # eventually advanced with error cleared.


def test_stream_uses_sequence_even_for_same_bytes_object(camera_module):
    # Notify the generator twice with the identical bytes instance while
    # incrementing _frame_seq; assert it emits two multipart frames.


def test_stalled_stream_exits_and_releases_viewer(camera_module):
    cam = camera_module.Camera(stream_stall_timeout_s=0.03)
    cam._ensure_running = lambda: None
    assert list(cam.mjpeg_generator()) == []
    assert cam.viewer_count == 0
```

Also test that a partial Picamera2 startup failure closes the temporary object.

- [ ] **Step 2: Run the new tests and verify RED**

Expected failures: the capture exception escapes or kills the thread, no Picamera2 cancellation occurs, stream delivery is based on `id(frame)`, and a stalled generator never exits.

- [ ] **Step 3: Implement bounded recovery**

On any capture exception:

```python
self._last_error = f"{backend} capture failed: {type(exc).__name__}: {exc}"
if backend == "picamera2" and self._picam is not None:
    self._picam.cancel_all_and_flush()
self._close()
```

Wake `_frame_cond`, wait with `self._stop.wait(self.recovery_backoff_s)`, and let the capture loop call `_open()` again while `_viewers > 0`. The loop must also retry initial open failures while viewers exist. Increment the viewer count before `_ensure_running()` so a failed initial open still has a live retry owner.

Retain `_last_error` through reopen and clear it only when a newly encoded frame is published. Close partially initialized Picamera2 instances in `_open_picamera2()` exceptions.

- [ ] **Step 4: Replace object identity with sequence delivery**

At generator entry, capture `previous_seq`. Wait until `_frame_seq > previous_seq`; emit the corresponding JPEG and update `previous_seq`. Reset the 10-second stall deadline after each emitted frame. If the deadline expires without a new sequence, return from the generator so the `viewer()` context decrements the count.

- [ ] **Step 5: Run focused and full tests and verify GREEN**

Expected: camera regressions pass and the full suite remains green.

- [ ] **Step 6: Commit recovery behavior**

```bash
git add integration/clinostat_extension/work/camera.py \
  integration/clinostat_extension/tests/test_camera.py
git commit -m "fix: recover stalled Picamera2 streams"
```

### Task 4: Deploy only the camera fix and verify production

**Files:**
- Deploy: `integration/clinostat_extension/work/camera.py` to `/home/aiworker-1/clinostat/camera.py`
- Backup: remote timestamped `camera.py.bak-<timestamp>`

- [ ] **Step 1: Run pre-deployment verification**

Run the focused camera tests, complete integration suite, `git diff --check`, and `python -m py_compile` on the camera module. Confirm zero failures.

- [ ] **Step 2: Re-check operational safety**

Query `/api/control/status` and `/api/experiment/status`. Require both `running=false`. Record the current clinostat PID and SpaceBio gateway PID.

- [ ] **Step 3: Back up and deploy only `camera.py`**

Create a timestamped remote backup with `cp -a`, upload the verified file with `scp`, and verify the local and remote SHA-256 hashes match. Do not invoke `deploy_to_pi.sh` and do not touch gateway files.

- [ ] **Step 4: Restart only clinostat**

Send `SIGTERM` to the user-owned clinostat MainPID. Poll `systemctl show clinostat -p MainPID` until a different nonzero PID is active. Confirm the recorded SpaceBio gateway PID did not change.

- [ ] **Step 5: Verify live recovery criteria**

Require all of the following:

- camera status reaches `healthy=true` with `error=null`;
- frame sequence increases across two status samples;
- two fresh snapshots are valid JPEGs and have different hashes;
- a 3-second MJPEG request contains at least three `--frame` boundaries;
- viewer count returns to its pre-request value after the client disconnects;
- control and experiment remain stopped;
- recent service logs contain no traceback or camera recovery error.

- [ ] **Step 6: Roll back on any failed production criterion**

If verification fails, restore the timestamped backup, restart only clinostat by the same PID handoff, and report both the failed check and rollback result. Do not stack speculative fixes in production.

- [ ] **Step 7: Commit any verification-only documentation adjustment**

Do not commit runtime artifacts or remote backups. Leave the branch clean.
