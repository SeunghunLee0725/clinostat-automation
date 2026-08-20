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

### Task 4: Deploy the camera and stream-lifecycle fix as one incident unit

**Files:**
- Deploy: `integration/clinostat_extension/work/camera.py` to `/home/aiworker-1/clinostat/camera.py`
- Deploy: `integration/clinostat_extension/work/server.py` to `/home/aiworker-1/clinostat/server.py`
- Backup: remote timestamped `camera.py.bak-<timestamp>` and `server.py.bak-<timestamp>`

This is a two-file clinostat incident deployment. Never run
`integration/deploy/deploy_to_pi.sh`: that script also deploys and restarts the
SpaceBio gateway. Do not upload, copy, signal, or restart any gateway file or
process. Reading the gateway PID before and after the incident is the only
gateway operation allowed.

- [ ] **Step 1: Run pre-deployment verification**

From the incident worktree, compile both deployable modules and run the focused
camera/server tests plus the complete integration suite:

```bash
set -euo pipefail
cd /Users/lee_ai_labtop/.config/superpowers/worktrees/clinostat-automation/camera-stream-recovery/integration
INCIDENT_PY='/Users/lee_ai_labtop/Desktop/과제수행_KIMS/(25-27)우주바이오/1_research_results/clinostat-automation/integration/.venv/bin/python'

"$INCIDENT_PY" -m py_compile \
  clinostat_extension/work/camera.py \
  clinostat_extension/work/server.py
"$INCIDENT_PY" -m pytest -q \
  clinostat_extension/tests/test_camera.py \
  clinostat_extension/tests/test_existing_contract.py \
  clinostat_extension/tests/test_spacebio_routes.py \
  clinostat_extension/tests/test_proxy_sessions.py
"$INCIDENT_PY" -m pytest -q
git -C .. diff --check
test -z "$(git -C .. status --porcelain=v1)"
```

Stop if any command fails or the worktree is not clean.

- [ ] **Step 2: Re-check operational safety**

Run the rest of this task in the same local shell. Resolve the exact target,
require strict host-key checking, verify control and experiment are stopped,
and record both service PIDs and both original file hashes:

```bash
set -euo pipefail
INCIDENT_PI_HOST='100.77.1.42'
INCIDENT_PI_USER='aiworker-1'
INCIDENT_TARGET="${INCIDENT_PI_USER}@${INCIDENT_PI_HOST}"
INCIDENT_REMOTE_ROOT='/home/aiworker-1/clinostat'
INCIDENT_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
INCIDENT_LOG_SINCE="$(date -u +'%Y-%m-%d %H:%M:%S UTC')"
INCIDENT_SSH=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=5
  -o ServerAliveInterval=2
  -o ServerAliveCountMax=2
)
test "$INCIDENT_TARGET" = 'aiworker-1@100.77.1.42'

ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 5 http://127.0.0.1:8000/api/control/status \
   | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get(\"running\") is False, d'"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 5 http://127.0.0.1:8000/api/experiment/status \
   | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get(\"running\") is False, d'"

INCIDENT_CLINOSTAT_PID_BEFORE="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'systemctl show clinostat.service -p MainPID --value')"
INCIDENT_GATEWAY_PID_BEFORE="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'systemctl show spacebio-gateway.service -p MainPID --value')"
test "$INCIDENT_CLINOSTAT_PID_BEFORE" -gt 0
test "$INCIDENT_GATEWAY_PID_BEFORE" -gt 0

INCIDENT_OLD_CAMERA_SHA="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/camera.py' | awk '{print \$1}'")"
INCIDENT_OLD_SERVER_SHA="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/server.py' | awk '{print \$1}'")"
test "${#INCIDENT_OLD_CAMERA_SHA}" -eq 64
test "${#INCIDENT_OLD_SERVER_SHA}" -eq 64
printf 'incident=%s clinostat PID=%s gateway PID=%s camera old=%s server old=%s\n' \
  "$INCIDENT_STAMP" "$INCIDENT_CLINOSTAT_PID_BEFORE" \
  "$INCIDENT_GATEWAY_PID_BEFORE" "$INCIDENT_OLD_CAMERA_SHA" \
  "$INCIDENT_OLD_SERVER_SHA"
```

Do not continue if either status is running, either PID is zero, or either hash
is missing.

- [ ] **Step 3: Create and validate timestamped backups of both files**

Create adjacent, timestamped backups and prove that each backup hash matches the
recorded original before uploading anything:

```bash
INCIDENT_CAMERA_BACKUP="$INCIDENT_REMOTE_ROOT/camera.py.bak-$INCIDENT_STAMP"
INCIDENT_SERVER_BACKUP="$INCIDENT_REMOTE_ROOT/server.py.bak-$INCIDENT_STAMP"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "set -eu
   test ! -e '$INCIDENT_CAMERA_BACKUP'
   test ! -e '$INCIDENT_SERVER_BACKUP'
   cp -a '$INCIDENT_REMOTE_ROOT/camera.py' '$INCIDENT_CAMERA_BACKUP'
   cp -a '$INCIDENT_REMOTE_ROOT/server.py' '$INCIDENT_SERVER_BACKUP'
   test -s '$INCIDENT_CAMERA_BACKUP'
   test -s '$INCIDENT_SERVER_BACKUP'"

INCIDENT_BACKUP_CAMERA_SHA="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_CAMERA_BACKUP' | awk '{print \$1}'")"
INCIDENT_BACKUP_SERVER_SHA="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_SERVER_BACKUP' | awk '{print \$1}'")"
test "$INCIDENT_BACKUP_CAMERA_SHA" = "$INCIDENT_OLD_CAMERA_SHA"
test "$INCIDENT_BACKUP_SERVER_SHA" = "$INCIDENT_OLD_SERVER_SHA"
printf 'camera backup=%s server backup=%s\n' \
  "$INCIDENT_CAMERA_BACKUP" "$INCIDENT_SERVER_BACKUP"
```

- [ ] **Step 4: Stage, compile, hash, and commit both files together**

Upload into one timestamped staging directory on the same filesystem as the
targets. Compile and hash both staged files before changing either live path:

```bash
INCIDENT_LOCAL_CAMERA="$PWD/clinostat_extension/work/camera.py"
INCIDENT_LOCAL_SERVER="$PWD/clinostat_extension/work/server.py"
INCIDENT_NEW_CAMERA_SHA="$(shasum -a 256 "$INCIDENT_LOCAL_CAMERA" | awk '{print $1}')"
INCIDENT_NEW_SERVER_SHA="$(shasum -a 256 "$INCIDENT_LOCAL_SERVER" | awk '{print $1}')"
printf 'camera new=%s server new=%s\n' \
  "$INCIDENT_NEW_CAMERA_SHA" "$INCIDENT_NEW_SERVER_SHA"
INCIDENT_REMOTE_STAGE="$INCIDENT_REMOTE_ROOT/.camera-stream-recovery-$INCIDENT_STAMP"
case "$INCIDENT_REMOTE_STAGE" in
  "$INCIDENT_REMOTE_ROOT"/.camera-stream-recovery-*) ;;
  *) exit 1 ;;
esac

ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "set -eu; umask 077; test ! -e '$INCIDENT_REMOTE_STAGE'; mkdir '$INCIDENT_REMOTE_STAGE'"
scp "${INCIDENT_SSH[@]}" "$INCIDENT_LOCAL_CAMERA" \
  "$INCIDENT_TARGET:$INCIDENT_REMOTE_STAGE/camera.py"
scp "${INCIDENT_SSH[@]}" "$INCIDENT_LOCAL_SERVER" \
  "$INCIDENT_TARGET:$INCIDENT_REMOTE_STAGE/server.py"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "set -eu
   python3 -m py_compile \
     '$INCIDENT_REMOTE_STAGE/camera.py' \
     '$INCIDENT_REMOTE_STAGE/server.py'
   test \"\$(sha256sum '$INCIDENT_REMOTE_STAGE/camera.py' | awk '{print \$1}')\" = '$INCIDENT_NEW_CAMERA_SHA'
   test \"\$(sha256sum '$INCIDENT_REMOTE_STAGE/server.py' | awk '{print \$1}')\" = '$INCIDENT_NEW_SERVER_SHA'"
```

Before committing either live file, define and arm the local fail-safe below.
The function uses the still-live `INCIDENT_*` values and is deliberately
self-contained: it restores a reviewed new/new pair or a known old/new partial
pair, skips file mutation for an already restored old/old pair, and refuses any
unknown third-party hash. It then performs an exact-target clinostat-only
handoff and verifies rollback. Its traps disable themselves before rollback to
prevent recursion.

```bash
incident_rollback_pair() (
  set -euo pipefail
  printf 'automatic rollback: restoring camera.py and server.py\n' >&2

  test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    "sha256sum '$INCIDENT_CAMERA_BACKUP' | awk '{print \$1}'")" = \
    "$INCIDENT_OLD_CAMERA_SHA"
  test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    "sha256sum '$INCIDENT_SERVER_BACKUP' | awk '{print \$1}'")" = \
    "$INCIDENT_OLD_SERVER_SHA"

  live_camera_sha=''
  live_server_sha=''
  for _attempt in $(seq 1 20); do
    live_camera_sha=''
    live_server_sha=''
    if live_camera_sha="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
         "sha256sum '$INCIDENT_REMOTE_ROOT/camera.py' | awk '{print \$1}'")" \
       && live_server_sha="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
         "sha256sum '$INCIDENT_REMOTE_ROOT/server.py' | awk '{print \$1}'")"; then
      if {
           [ "$live_camera_sha" = "$INCIDENT_NEW_CAMERA_SHA" ] \
             && [ "$live_server_sha" = "$INCIDENT_NEW_SERVER_SHA" ]
         } || {
           [ "$live_camera_sha" = "$INCIDENT_OLD_CAMERA_SHA" ] \
             && [ "$live_server_sha" = "$INCIDENT_OLD_SERVER_SHA" ]
         }; then
        break
      fi
    fi
    sleep 0.1
  done

  if [ "$live_camera_sha" = "$INCIDENT_OLD_CAMERA_SHA" ] \
     && [ "$live_server_sha" = "$INCIDENT_OLD_SERVER_SHA" ]; then
    printf 'automatic rollback: old file pair already present; resuming handoff\n' >&2
  elif {
         [ "$live_camera_sha" = "$INCIDENT_OLD_CAMERA_SHA" ] \
           || [ "$live_camera_sha" = "$INCIDENT_NEW_CAMERA_SHA" ]
       } && {
         [ "$live_server_sha" = "$INCIDENT_OLD_SERVER_SHA" ] \
           || [ "$live_server_sha" = "$INCIDENT_NEW_SERVER_SHA" ]
       }; then
    ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" bash -s -- \
      "$INCIDENT_REMOTE_ROOT" "$INCIDENT_CAMERA_BACKUP" \
      "$INCIDENT_SERVER_BACKUP" "$INCIDENT_OLD_CAMERA_SHA" \
      "$INCIDENT_OLD_SERVER_SHA" "$INCIDENT_NEW_CAMERA_SHA" \
      "$INCIDENT_NEW_SERVER_SHA" "$INCIDENT_STAMP" \
      <<'REMOTE_AUTOMATIC_ROLLBACK'
set -euo pipefail
root=$1
camera_backup=$2
server_backup=$3
camera_old_sha=$4
server_old_sha=$5
camera_new_sha=$6
server_new_sha=$7
stamp=$8
committed=0

restore_pre_pair() {
  mv -f -- "$root/.camera.py.auto-pre-rollback-$stamp" "$root/camera.py"
  mv -f -- "$root/.server.py.auto-pre-rollback-$stamp" "$root/server.py"
}
finish_rollback() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$committed" -ne 1 ]; then
    restore_pre_pair
  fi
  exit "$status"
}

test "$(sha256sum "$camera_backup" | awk '{print $1}')" = "$camera_old_sha"
test "$(sha256sum "$server_backup" | awk '{print $1}')" = "$server_old_sha"
camera_live_sha="$(sha256sum "$root/camera.py" | awk '{print $1}')"
server_live_sha="$(sha256sum "$root/server.py" | awk '{print $1}')"
case "$camera_live_sha" in
  "$camera_old_sha"|"$camera_new_sha") ;;
  *) exit 1 ;;
esac
case "$server_live_sha" in
  "$server_old_sha"|"$server_new_sha") ;;
  *) exit 1 ;;
esac
test ! -e "$root/.camera.py.auto-pre-rollback-$stamp"
test ! -e "$root/.server.py.auto-pre-rollback-$stamp"
cp -a "$root/camera.py" "$root/.camera.py.auto-pre-rollback-$stamp"
cp -a "$root/server.py" "$root/.server.py.auto-pre-rollback-$stamp"
cp -a "$camera_backup" "$root/.camera.py.auto-rollback-$stamp"
cp -a "$server_backup" "$root/.server.py.auto-rollback-$stamp"
trap finish_rollback EXIT
trap 'exit 1' HUP INT TERM
mv -f -- "$root/.camera.py.auto-rollback-$stamp" "$root/camera.py"
mv -f -- "$root/.server.py.auto-rollback-$stamp" "$root/server.py"
test "$(sha256sum "$root/camera.py" | awk '{print $1}')" = "$camera_old_sha"
test "$(sha256sum "$root/server.py" | awk '{print $1}')" = "$server_old_sha"
committed=1
trap - EXIT HUP INT TERM
rm -f -- \
  "$root/.camera.py.auto-pre-rollback-$stamp" \
  "$root/.server.py.auto-pre-rollback-$stamp"
REMOTE_AUTOMATIC_ROLLBACK
  else
    printf 'automatic rollback refused: live file pair contains an unknown hash\n' >&2
    exit 1
  fi

  test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    'systemctl show clinostat.service -p Restart --value')" = 'always'
  INCIDENT_ROLLBACK_PID_BEFORE=''
  for _attempt in $(seq 1 60); do
    INCIDENT_ROLLBACK_PID_BEFORE="$(
      ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
        'systemctl show clinostat.service -p MainPID --value'
    )"
    if [ "$INCIDENT_ROLLBACK_PID_BEFORE" -gt 0 ] \
       && ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
         'systemctl is-active --quiet clinostat.service'; then
      break
    fi
    sleep 0.25
  done
  test "$INCIDENT_ROLLBACK_PID_BEFORE" -gt 0

  ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" bash -s -- \
    "$INCIDENT_ROLLBACK_PID_BEFORE" "$INCIDENT_GATEWAY_PID_BEFORE" \
    <<'REMOTE_AUTOMATIC_HANDOFF'
set -euo pipefail
expected_pid=$1
expected_gateway_pid=$2
curl -fsS --max-time 5 http://127.0.0.1:8000/api/control/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("running") is False, d'
curl -fsS --max-time 5 http://127.0.0.1:8000/api/experiment/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("running") is False, d'
systemctl is-active --quiet clinostat.service
test "$(systemctl show clinostat.service -p Restart --value)" = 'always'
test "$(systemctl show spacebio-gateway.service -p MainPID --value)" = \
  "$expected_gateway_pid"
test "$(systemctl show clinostat.service -p User --value)" = 'aiworker-1'
current_pid="$(systemctl show clinostat.service -p MainPID --value)"
test "$current_pid" = "$expected_pid"
test "$current_pid" -gt 0
process_uid="$(awk '/^Uid:/{print $2; exit}' "/proc/$current_pid/status")"
test "$process_uid" = "$(id -u aiworker-1)"
python3 - "$current_pid" <<'PROCESS_CHECK'
import pathlib
import sys

pid = sys.argv[1]
argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
assert argv == [b"/usr/bin/python3", b"/home/aiworker-1/clinostat/server.py"], argv
PROCESS_CHECK
kill -KILL -- "$current_pid"
REMOTE_AUTOMATIC_HANDOFF

  INCIDENT_ROLLBACK_PID_AFTER=''
  for _attempt in $(seq 1 60); do
    INCIDENT_ROLLBACK_PID_AFTER="$(
      ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
        'systemctl show clinostat.service -p MainPID --value'
    )"
    if [ "$INCIDENT_ROLLBACK_PID_AFTER" -gt 0 ] \
       && [ "$INCIDENT_ROLLBACK_PID_AFTER" != "$INCIDENT_ROLLBACK_PID_BEFORE" ] \
       && ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
         'systemctl is-active --quiet clinostat.service'; then
      break
    fi
    sleep 0.25
  done
  test "$INCIDENT_ROLLBACK_PID_AFTER" -gt 0
  test "$INCIDENT_ROLLBACK_PID_AFTER" != "$INCIDENT_ROLLBACK_PID_BEFORE"
  test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    "sha256sum '$INCIDENT_REMOTE_ROOT/camera.py' | awk '{print \$1}'")" = \
    "$INCIDENT_OLD_CAMERA_SHA"
  test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    "sha256sum '$INCIDENT_REMOTE_ROOT/server.py' | awk '{print \$1}'")" = \
    "$INCIDENT_OLD_SERVER_SHA"
  test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    'systemctl show spacebio-gateway.service -p MainPID --value')" = \
    "$INCIDENT_GATEWAY_PID_BEFORE"
  ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    'curl -fsS --max-time 5 http://127.0.0.1:8000/ -o /dev/null'
  ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    "curl -fsS --max-time 5 http://127.0.0.1:8000/api/control/status \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get(\"running\") is False, d'"
  ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    "curl -fsS --max-time 5 http://127.0.0.1:8000/api/experiment/status \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get(\"running\") is False, d'"
  printf 'automatic rollback complete: old hashes restored, clinostat PID %s -> %s\n' \
    "$INCIDENT_ROLLBACK_PID_BEFORE" "$INCIDENT_ROLLBACK_PID_AFTER" >&2
)

INCIDENT_ROLLBACK_REQUIRED=1
incident_rollback_guard() {
  local failure_status=${1:-1}
  local rollback_status
  trap - ERR EXIT
  trap '' HUP INT TERM
  set +e
  incident_rollback_pair
  rollback_status=$?
  if [ "$rollback_status" -ne 0 ]; then
    printf 'AUTOMATIC ROLLBACK FAILED (status %s); stop and escalate\n' \
      "$rollback_status" >&2
  fi
  exit "$failure_status"
}
incident_exit_guard() {
  local exit_status=$?
  trap - EXIT
  if [ "${INCIDENT_ROLLBACK_REQUIRED:-0}" -eq 1 ]; then
    if [ "$exit_status" -eq 0 ]; then
      exit_status=1
    fi
    incident_rollback_guard "$exit_status"
  fi
  exit "$exit_status"
}
trap 'incident_rollback_guard $?' ERR
trap incident_exit_guard EXIT
trap 'incident_rollback_guard 129' HUP
trap 'incident_rollback_guard 130' INT
trap 'incident_rollback_guard 143' TERM
```

The guard is now active. Commit the two files with two atomic renames inside one
remote shell. A POSIX filesystem cannot atomically rename two paths as a single
primitive, so the remote shell installs both before any restart and restores
both validated backups on any error or signal. The running process continues
using its already-imported modules until the pair is complete and verified.

```bash
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" bash -s -- \
  "$INCIDENT_REMOTE_ROOT" "$INCIDENT_REMOTE_STAGE" \
  "$INCIDENT_CAMERA_BACKUP" "$INCIDENT_SERVER_BACKUP" \
  "$INCIDENT_NEW_CAMERA_SHA" "$INCIDENT_NEW_SERVER_SHA" \
  "$INCIDENT_OLD_CAMERA_SHA" "$INCIDENT_OLD_SERVER_SHA" \
  "$INCIDENT_STAMP" <<'REMOTE_INSTALL'
set -euo pipefail
root=$1
stage=$2
camera_backup=$3
server_backup=$4
camera_new_sha=$5
server_new_sha=$6
camera_old_sha=$7
server_old_sha=$8
stamp=$9
committed=0

restore_pair() {
  cp -a "$camera_backup" "$root/.camera.py.restore-$stamp"
  cp -a "$server_backup" "$root/.server.py.restore-$stamp"
  mv -f -- "$root/.camera.py.restore-$stamp" "$root/camera.py"
  mv -f -- "$root/.server.py.restore-$stamp" "$root/server.py"
}
finish_install() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$committed" -ne 1 ]; then
    restore_pair
  fi
  exit "$status"
}
test "$(sha256sum "$stage/camera.py" | awk '{print $1}')" = "$camera_new_sha"
test "$(sha256sum "$stage/server.py" | awk '{print $1}')" = "$server_new_sha"
test "$(sha256sum "$root/camera.py" | awk '{print $1}')" = "$camera_old_sha"
test "$(sha256sum "$root/server.py" | awk '{print $1}')" = "$server_old_sha"
test "$(sha256sum "$camera_backup" | awk '{print $1}')" = "$camera_old_sha"
test "$(sha256sum "$server_backup" | awk '{print $1}')" = "$server_old_sha"
trap finish_install EXIT
trap 'exit 1' HUP INT TERM
mv -f -- "$stage/camera.py" "$root/camera.py"
mv -f -- "$stage/server.py" "$root/server.py"
test "$(sha256sum "$root/camera.py" | awk '{print $1}')" = "$camera_new_sha"
test "$(sha256sum "$root/server.py" | awk '{print $1}')" = "$server_new_sha"
committed=1
trap - EXIT HUP INT TERM
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$stage" || true
REMOTE_INSTALL
```

If the remote transaction fails, its own trap restores the old pair and the
already-armed local handler briefly waits for a stable old/old or new/new
state. If a known old/new partial pair remains, it safely restores both
validated backups; any unknown hash causes drift escalation without overwrite.
From guard activation through the end of live validation, any unhandled
nonzero criterion, signal, or premature shell exit invokes
`incident_rollback_pair` before the incident shell exits nonzero. Do not run
the install or live checks in a different shell.

Confirm both final hashes once more while the automatic rollback is armed:

```bash
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/camera.py' | awk '{print \$1}'")" = "$INCIDENT_NEW_CAMERA_SHA"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/server.py' | awk '{print \$1}'")" = "$INCIDENT_NEW_SERVER_SHA"
```

- [ ] **Step 5: Load the pair with an exact validated clinostat SIGKILL**

The rolled-back production code has already hung during graceful SIGTERM, so
the initial load uses the explicitly authorized exact MainPID SIGKILL. In the
same remote shell, re-check stopped state, gateway identity, MainPID, service
user, and the exact process argv before sending any signal:

```bash
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" bash -s -- \
  "$INCIDENT_CLINOSTAT_PID_BEFORE" "$INCIDENT_GATEWAY_PID_BEFORE" \
  <<'REMOTE_FORCE_LOAD'
set -euo pipefail
expected_pid=$1
expected_gateway_pid=$2
curl -fsS --max-time 5 http://127.0.0.1:8000/api/control/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("running") is False, d'
curl -fsS --max-time 5 http://127.0.0.1:8000/api/experiment/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("running") is False, d'
systemctl is-active --quiet clinostat.service
test "$(systemctl show clinostat.service -p Restart --value)" = 'always'
test "$(systemctl show spacebio-gateway.service -p MainPID --value)" = \
  "$expected_gateway_pid"
test "$(systemctl show clinostat.service -p User --value)" = 'aiworker-1'
current_pid="$(systemctl show clinostat.service -p MainPID --value)"
test "$current_pid" = "$expected_pid"
test "$current_pid" -gt 0
process_uid="$(awk '/^Uid:/{print $2; exit}' "/proc/$current_pid/status")"
test "$process_uid" = "$(id -u aiworker-1)"
python3 - "$current_pid" <<'PROCESS_CHECK'
import pathlib
import sys

pid = sys.argv[1]
argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
assert argv == [b"/usr/bin/python3", b"/home/aiworker-1/clinostat/server.py"], argv
PROCESS_CHECK
kill -KILL -- "$current_pid"
REMOTE_FORCE_LOAD
INCIDENT_CLINOSTAT_PID_AFTER=''
for _attempt in $(seq 1 60); do
  INCIDENT_CLINOSTAT_PID_AFTER="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    'systemctl show clinostat.service -p MainPID --value')"
  if [ "$INCIDENT_CLINOSTAT_PID_AFTER" -gt 0 ] \
     && [ "$INCIDENT_CLINOSTAT_PID_AFTER" != "$INCIDENT_CLINOSTAT_PID_BEFORE" ] \
     && ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
       'systemctl is-active --quiet clinostat.service'; then
    break
  fi
  sleep 0.25
done
test "$INCIDENT_CLINOSTAT_PID_AFTER" -gt 0
test "$INCIDENT_CLINOSTAT_PID_AFTER" != "$INCIDENT_CLINOSTAT_PID_BEFORE"
INCIDENT_GATEWAY_PID_AFTER="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'systemctl show spacebio-gateway.service -p MainPID --value')"
test "$INCIDENT_GATEWAY_PID_AFTER" = "$INCIDENT_GATEWAY_PID_BEFORE"
```

- [ ] **Step 6: Verify live recovery criteria**

Use one bounded localhost MJPEG client, sample status while that client owns the
camera, and retain the response files until every assertion passes:

```bash
INCIDENT_VERIFY_DIR="/tmp/clinostat-camera-verify-$INCIDENT_STAMP"
case "$INCIDENT_VERIFY_DIR" in
  /tmp/clinostat-camera-verify-*) ;;
  *) exit 1 ;;
esac
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "set -eu; umask 077; test ! -e '$INCIDENT_VERIFY_DIR'; mkdir '$INCIDENT_VERIFY_DIR'"

INCIDENT_VIEWERS_BEFORE="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 5 http://127.0.0.1:8000/api/camera/status \
   | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"viewers\"])'")"
test "$INCIDENT_VIEWERS_BEFORE" -eq 0

ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "set -eu
   stream_rc=0
   curl -fsS --max-time 3 \
     http://127.0.0.1:8000/api/camera/stream \
     > '$INCIDENT_VERIFY_DIR/stream.mjpeg' || stream_rc=\$?
   test \"\$stream_rc\" -eq 28" &
INCIDENT_STREAM_JOB=$!

INCIDENT_FRAME_SEQ_ONE=''
for _attempt in $(seq 1 50); do
  if INCIDENT_FRAME_SEQ_ONE="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    "curl -fsS --max-time 2 http://127.0.0.1:8000/api/camera/status \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"healthy\"] is True and d[\"error\"] is None, d; print(d[\"frame_sequence\"])'")"; then
    break
  fi
  sleep 0.1
done
test -n "$INCIDENT_FRAME_SEQ_ONE"
sleep 0.25
INCIDENT_FRAME_SEQ_TWO="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 2 http://127.0.0.1:8000/api/camera/status \
   | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"healthy\"] is True and d[\"error\"] is None, d; print(d[\"frame_sequence\"])'")"
test "$INCIDENT_FRAME_SEQ_TWO" -gt "$INCIDENT_FRAME_SEQ_ONE"
wait "$INCIDENT_STREAM_JOB"

INCIDENT_BOUNDARIES="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_bytes().count(b\"--frame\"))' \
   '$INCIDENT_VERIFY_DIR/stream.mjpeg'")"
test "$INCIDENT_BOUNDARIES" -ge 3

INCIDENT_VIEWERS_AFTER=''
for _attempt in $(seq 1 50); do
  INCIDENT_VIEWERS_AFTER="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    "curl -fsS --max-time 2 http://127.0.0.1:8000/api/camera/status \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"viewers\"])'")"
  if [ "$INCIDENT_VIEWERS_AFTER" = "$INCIDENT_VIEWERS_BEFORE" ]; then
    break
  fi
  sleep 0.1
done
test "$INCIDENT_VIEWERS_AFTER" -eq 0

ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  bash -s -- "$INCIDENT_VERIFY_DIR" <<'REMOTE_SNAPSHOT_CHECK'
set -euo pipefail
verify_dir=$1
frame_sequence() {
  curl -fsS --max-time 5 http://127.0.0.1:8000/api/camera/status \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["frame_sequence"])'
}

before="$(frame_sequence)"
curl -fsS --max-time 5 http://127.0.0.1:8000/api/camera/snapshot \
  > "$verify_dir/snapshot-1.jpg"
after_one="$(frame_sequence)"
test "$after_one" -gt "$before"
curl -fsS --max-time 5 http://127.0.0.1:8000/api/camera/snapshot \
  > "$verify_dir/snapshot-2.jpg"
after_two="$(frame_sequence)"
test "$after_two" -gt "$after_one"
python3 -c 'import pathlib,sys
for name in sys.argv[1:]:
    data=pathlib.Path(name).read_bytes()
    assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"), name' \
  "$verify_dir/snapshot-1.jpg" \
  "$verify_dir/snapshot-2.jpg"
snapshot_one_sha="$(sha256sum "$verify_dir/snapshot-1.jpg" | awk '{print $1}')"
snapshot_two_sha="$(sha256sum "$verify_dir/snapshot-2.jpg" | awk '{print $1}')"
test "$snapshot_one_sha" != "$snapshot_two_sha"
REMOTE_SNAPSHOT_CHECK

ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 5 http://127.0.0.1:8000/api/control/status \
   | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get(\"running\") is False, d'"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 5 http://127.0.0.1:8000/api/experiment/status \
   | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get(\"running\") is False, d'"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'systemctl show spacebio-gateway.service -p MainPID --value')" = "$INCIDENT_GATEWAY_PID_BEFORE"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/camera.py' | awk '{print \$1}'")" = "$INCIDENT_NEW_CAMERA_SHA"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/server.py' | awk '{print \$1}'")" = "$INCIDENT_NEW_SERVER_SHA"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" bash -s -- \
  "$INCIDENT_LOG_SINCE" <<'REMOTE_LOG_CHECK'
set -euo pipefail
since=$1
logs="$(journalctl -u clinostat.service --since "$since" --no-pager)"
if grep -E 'Traceback|camera.*(failed|error)' <<<"$logs"; then
  exit 1
fi
REMOTE_LOG_CHECK
```

After these checks pass with viewers at zero, require the new code to complete
one graceful SIGTERM handoff. Revalidate the exact unit, PID, user, argv,
stopped state, and gateway PID in the same shell before signaling:

```bash
INCIDENT_GRACEFUL_PID_BEFORE="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'systemctl show clinostat.service -p MainPID --value')"
test "$INCIDENT_GRACEFUL_PID_BEFORE" = "$INCIDENT_CLINOSTAT_PID_AFTER"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" bash -s -- \
  "$INCIDENT_GRACEFUL_PID_BEFORE" "$INCIDENT_GATEWAY_PID_BEFORE" \
  <<'REMOTE_GRACEFUL_CHECK'
set -euo pipefail
expected_pid=$1
expected_gateway_pid=$2
curl -fsS --max-time 5 http://127.0.0.1:8000/api/control/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("running") is False, d'
curl -fsS --max-time 5 http://127.0.0.1:8000/api/experiment/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("running") is False, d'
curl -fsS --max-time 5 http://127.0.0.1:8000/api/camera/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["viewers"] == 0, d'
systemctl is-active --quiet clinostat.service
test "$(systemctl show clinostat.service -p Restart --value)" = 'always'
test "$(systemctl show spacebio-gateway.service -p MainPID --value)" = \
  "$expected_gateway_pid"
test "$(systemctl show clinostat.service -p User --value)" = 'aiworker-1'
current_pid="$(systemctl show clinostat.service -p MainPID --value)"
test "$current_pid" = "$expected_pid"
test "$current_pid" -gt 0
process_uid="$(awk '/^Uid:/{print $2; exit}' "/proc/$current_pid/status")"
test "$process_uid" = "$(id -u aiworker-1)"
python3 - "$current_pid" <<'PROCESS_CHECK'
import pathlib
import sys

pid = sys.argv[1]
argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
assert argv == [b"/usr/bin/python3", b"/home/aiworker-1/clinostat/server.py"], argv
PROCESS_CHECK
kill -TERM -- "$current_pid"
REMOTE_GRACEFUL_CHECK

INCIDENT_GRACEFUL_PID_AFTER=''
for _attempt in $(seq 1 60); do
  INCIDENT_GRACEFUL_PID_AFTER="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    'systemctl show clinostat.service -p MainPID --value')"
  if [ "$INCIDENT_GRACEFUL_PID_AFTER" -gt 0 ] \
     && [ "$INCIDENT_GRACEFUL_PID_AFTER" != "$INCIDENT_GRACEFUL_PID_BEFORE" ] \
     && ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
       'systemctl is-active --quiet clinostat.service'; then
    break
  fi
  sleep 0.25
done
test "$INCIDENT_GRACEFUL_PID_AFTER" -gt 0
test "$INCIDENT_GRACEFUL_PID_AFTER" != "$INCIDENT_GRACEFUL_PID_BEFORE"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'systemctl show spacebio-gateway.service -p MainPID --value')" = "$INCIDENT_GATEWAY_PID_BEFORE"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 5 http://127.0.0.1:8000/api/camera/status \
   | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"viewers\"] == 0, d'"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "python3 -c 'import pathlib,shutil,sys
path=pathlib.Path(sys.argv[1])
assert str(path).startswith(\"/tmp/clinostat-camera-verify-\")
shutil.rmtree(path)' '$INCIDENT_VERIFY_DIR'"
INCIDENT_ROLLBACK_REQUIRED=0
trap - ERR EXIT HUP INT TERM
```

On failure, retain the exact verification directory for the rollback report.
Do not use a broad /tmp wildcard. Reaching the trap-disarm lines is the only
successful end state. Any earlier failure invokes the automatic paired
rollback and exits nonzero.

- [ ] **Step 7 (failure only): Retain the manual paired rollback as an audited fallback**

The armed handler above is the primary failure path and runs before the
incident shell can lose its unexported state. Keep the commands below as a
line-by-line audited fallback for an operator who has reconstructed the exact
printed `INCIDENT_*` values after diagnosing a failed automatic rollback. Do
not execute this step after Step 6 disarms the trap successfully, and do not
stack another fix. Validate both backup hashes and classify the live pair
first: old/old skips file mutation and resumes handoff; any pair composed only
of the recorded old/new hashes restores both backups; any unknown hash stops
for drift escalation. Verify both old hashes before repeating the
clinostat-only PID handoff. Never restart or signal the gateway.

```bash
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_CAMERA_BACKUP' | awk '{print \$1}'")" = "$INCIDENT_OLD_CAMERA_SHA"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_SERVER_BACKUP' | awk '{print \$1}'")" = "$INCIDENT_OLD_SERVER_SHA"

INCIDENT_FALLBACK_CAMERA_SHA="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/camera.py' | awk '{print \$1}'")"
INCIDENT_FALLBACK_SERVER_SHA="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/server.py' | awk '{print \$1}'")"

if [ "$INCIDENT_FALLBACK_CAMERA_SHA" = "$INCIDENT_OLD_CAMERA_SHA" ] \
   && [ "$INCIDENT_FALLBACK_SERVER_SHA" = "$INCIDENT_OLD_SERVER_SHA" ]; then
  printf 'manual fallback: old file pair already present; resuming handoff\n'
elif {
       [ "$INCIDENT_FALLBACK_CAMERA_SHA" = "$INCIDENT_OLD_CAMERA_SHA" ] \
         || [ "$INCIDENT_FALLBACK_CAMERA_SHA" = "$INCIDENT_NEW_CAMERA_SHA" ]
     } && {
       [ "$INCIDENT_FALLBACK_SERVER_SHA" = "$INCIDENT_OLD_SERVER_SHA" ] \
         || [ "$INCIDENT_FALLBACK_SERVER_SHA" = "$INCIDENT_NEW_SERVER_SHA" ]
     }; then
  ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" bash -s -- \
    "$INCIDENT_REMOTE_ROOT" "$INCIDENT_CAMERA_BACKUP" \
    "$INCIDENT_SERVER_BACKUP" "$INCIDENT_OLD_CAMERA_SHA" \
    "$INCIDENT_OLD_SERVER_SHA" "$INCIDENT_NEW_CAMERA_SHA" \
    "$INCIDENT_NEW_SERVER_SHA" "$INCIDENT_STAMP" <<'REMOTE_ROLLBACK'
set -euo pipefail
root=$1
camera_backup=$2
server_backup=$3
camera_old_sha=$4
server_old_sha=$5
camera_new_sha=$6
server_new_sha=$7
stamp=$8
committed=0

restore_pre_pair() {
  mv -f -- "$root/.camera.py.pre-rollback-$stamp" "$root/camera.py"
  mv -f -- "$root/.server.py.pre-rollback-$stamp" "$root/server.py"
}
finish_rollback() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$committed" -ne 1 ]; then
    restore_pre_pair
  fi
  exit "$status"
}

test "$(sha256sum "$camera_backup" | awk '{print $1}')" = "$camera_old_sha"
test "$(sha256sum "$server_backup" | awk '{print $1}')" = "$server_old_sha"
camera_live_sha="$(sha256sum "$root/camera.py" | awk '{print $1}')"
server_live_sha="$(sha256sum "$root/server.py" | awk '{print $1}')"
case "$camera_live_sha" in
  "$camera_old_sha"|"$camera_new_sha") ;;
  *) exit 1 ;;
esac
case "$server_live_sha" in
  "$server_old_sha"|"$server_new_sha") ;;
  *) exit 1 ;;
esac
cp -a "$root/camera.py" "$root/.camera.py.pre-rollback-$stamp"
cp -a "$root/server.py" "$root/.server.py.pre-rollback-$stamp"
cp -a "$camera_backup" "$root/.camera.py.rollback-$stamp"
cp -a "$server_backup" "$root/.server.py.rollback-$stamp"
trap finish_rollback EXIT
trap 'exit 1' HUP INT TERM
mv -f -- "$root/.camera.py.rollback-$stamp" "$root/camera.py"
mv -f -- "$root/.server.py.rollback-$stamp" "$root/server.py"
test "$(sha256sum "$root/camera.py" | awk '{print $1}')" = "$camera_old_sha"
test "$(sha256sum "$root/server.py" | awk '{print $1}')" = "$server_old_sha"
committed=1
trap - EXIT HUP INT TERM
rm -f -- \
  "$root/.camera.py.pre-rollback-$stamp" \
  "$root/.server.py.pre-rollback-$stamp"
REMOTE_ROLLBACK
else
  printf 'manual fallback refused: live file pair contains an unknown hash\n' >&2
  exit 1
fi
```

Because rollback may be recovering from a failed graceful-shutdown criterion,
use the exact validated clinostat MainPID SIGKILL for the rollback handoff.
Re-check stopped state, gateway PID, unit user, process user, and exact argv in
the same remote shell before signaling:

```bash
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'systemctl show clinostat.service -p Restart --value')" = 'always'
INCIDENT_ROLLBACK_PID_BEFORE=''
for _attempt in $(seq 1 60); do
  INCIDENT_ROLLBACK_PID_BEFORE="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    'systemctl show clinostat.service -p MainPID --value')"
  if [ "$INCIDENT_ROLLBACK_PID_BEFORE" -gt 0 ] \
     && ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
       'systemctl is-active --quiet clinostat.service'; then
    break
  fi
  sleep 0.25
done
test "$INCIDENT_ROLLBACK_PID_BEFORE" -gt 0
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" bash -s -- \
  "$INCIDENT_ROLLBACK_PID_BEFORE" "$INCIDENT_GATEWAY_PID_BEFORE" \
  <<'REMOTE_ROLLBACK_RESTART'
set -euo pipefail
expected_pid=$1
expected_gateway_pid=$2
curl -fsS --max-time 5 http://127.0.0.1:8000/api/control/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("running") is False, d'
curl -fsS --max-time 5 http://127.0.0.1:8000/api/experiment/status \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("running") is False, d'
systemctl is-active --quiet clinostat.service
test "$(systemctl show clinostat.service -p Restart --value)" = 'always'
test "$(systemctl show spacebio-gateway.service -p MainPID --value)" = \
  "$expected_gateway_pid"
test "$(systemctl show clinostat.service -p User --value)" = 'aiworker-1'
current_pid="$(systemctl show clinostat.service -p MainPID --value)"
test "$current_pid" = "$expected_pid"
test "$current_pid" -gt 0
process_uid="$(awk '/^Uid:/{print $2; exit}' "/proc/$current_pid/status")"
test "$process_uid" = "$(id -u aiworker-1)"
python3 - "$current_pid" <<'PROCESS_CHECK'
import pathlib
import sys

pid = sys.argv[1]
argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
assert argv == [b"/usr/bin/python3", b"/home/aiworker-1/clinostat/server.py"], argv
PROCESS_CHECK
kill -KILL -- "$current_pid"
REMOTE_ROLLBACK_RESTART

INCIDENT_ROLLBACK_PID_AFTER=''
for _attempt in $(seq 1 60); do
  INCIDENT_ROLLBACK_PID_AFTER="$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
    'systemctl show clinostat.service -p MainPID --value')"
  if [ "$INCIDENT_ROLLBACK_PID_AFTER" -gt 0 ] \
     && [ "$INCIDENT_ROLLBACK_PID_AFTER" != "$INCIDENT_ROLLBACK_PID_BEFORE" ] \
     && ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
       'systemctl is-active --quiet clinostat.service'; then
    break
  fi
  sleep 0.25
done
test "$INCIDENT_ROLLBACK_PID_AFTER" -gt 0
test "$INCIDENT_ROLLBACK_PID_AFTER" != "$INCIDENT_ROLLBACK_PID_BEFORE"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/camera.py' | awk '{print \$1}'")" = "$INCIDENT_OLD_CAMERA_SHA"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "sha256sum '$INCIDENT_REMOTE_ROOT/server.py' | awk '{print \$1}'")" = "$INCIDENT_OLD_SERVER_SHA"
test "$(ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'systemctl show spacebio-gateway.service -p MainPID --value')" = "$INCIDENT_GATEWAY_PID_BEFORE"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  'curl -fsS --max-time 5 http://127.0.0.1:8000/ -o /dev/null'
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 5 http://127.0.0.1:8000/api/control/status \
   | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get(\"running\") is False, d'"
ssh "${INCIDENT_SSH[@]}" "$INCIDENT_TARGET" \
  "curl -fsS --max-time 5 http://127.0.0.1:8000/api/experiment/status \
   | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get(\"running\") is False, d'"
```

Report the failed criterion, both restored hashes, both rollback clinostat PIDs,
and the unchanged gateway PID.

- [ ] **Step 8: Commit any verification-only documentation adjustment**

Do not commit runtime artifacts or remote backups. Leave the branch clean.
