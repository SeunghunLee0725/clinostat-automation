# Camera Stream Recovery Design

## Context

The Raspberry Pi dashboard at `http://100.77.1.42:8000/` serves a Picamera2
MJPEG stream. The process remained alive and reported `open=true`, but the
capture thread stopped producing frames. Every new client received one cached
JPEG and then waited forever. Disconnected clients were not released, so the
viewer count reached 26 and the existing idle-close behavior could never reopen
the camera.

Runtime evidence isolated the failure to the Picamera2 capture path:

- Tailscale and the FastAPI application remained reachable.
- A 15-second stream request returned exactly one JPEG frame.
- snapshots taken seconds apart had identical content hashes.
- the capture thread remained in a futex wait with no increase in CPU time.
- Picamera2's installed API supports a numeric `capture_array(wait=...)`
  timeout plus `cancel_all_and_flush()` for recovery after a timed-out job.

## Goals

1. Restore the live stream on the deployed Raspberry Pi.
2. Bound Picamera2 frame acquisition so a stalled camera cannot block forever.
3. Automatically close and reopen the camera after a capture timeout while
   viewers remain connected.
4. Make health reporting reflect frame freshness, not merely object existence.
5. Ensure stalled or disconnected MJPEG viewers are eventually released.
6. Preserve the existing camera routes and browser contract.

## Non-goals

- Replacing Picamera2 or changing camera resolution, frame rate, or JPEG quality.
- Refactoring unrelated motor, sensor, SpaceBio, or dashboard behavior.
- Restarting the SpaceBio gateway.
- Adding a browser UI redesign or new operator controls.

## Chosen approach

Keep the current single shared `Camera` object and capture thread. Add bounded
Picamera2 reads, explicit frame sequence/freshness state, and in-process recovery.
This is smaller and safer than moving camera capture to a subprocess, while
directly using the recovery primitives supplied by the installed Picamera2
version.

Two alternatives were rejected:

- A service restart alone restores the stream but leaves the same recurrence
  path.
- A separate camera subprocess provides a stronger crash boundary but is an
  unnecessary architectural expansion for this incident.

## Source layout

- Add `integration/clinostat_extension/work/camera.py` as the canonical editable
  copy of the deployed `/home/aiworker-1/clinostat/camera.py`.
- Add `integration/clinostat_extension/tests/test_camera.py` for camera lifecycle,
  timeout, status, and stream cleanup regressions.
- Keep `integration/clinostat_extension/work/server.py` as the canonical server
  source and explicitly close its synchronous MJPEG iterator when the Starlette
  response lifecycle ends.
- Update `integration/deploy/deploy_to_pi.sh` so future deployments include
  `camera.py` with the other clinostat extension files.

## Camera state and health

The camera tracks:

- a monotonically increasing frame sequence,
- the monotonic time of the last successfully encoded frame,
- the latest JPEG,
- the most recent capture/recovery error,
- active viewer count.

`status()` retains all existing fields and adds `healthy`, `frame_sequence`, and
`last_frame_age_s`. `open` continues to mean that a backend object is open;
`healthy` means a recent frame has actually arrived. Existing consumers remain
compatible because no current field is removed or renamed.

The timing contract is fixed for this deployment:

- `capture_timeout_s = 2.0`,
- `unhealthy_after_s = 4.0`,
- `recovery_backoff_s = 0.5`,
- `stream_stall_timeout_s = 10.0`.

The required ordering is `capture_timeout_s < unhealthy_after_s <
stream_stall_timeout_s`, leaving time for at least one close/reopen attempt
before a stream is terminated. `last_frame_age_s` is `null` before the first
successful frame. `healthy` is true only when the backend is open, at least one
frame has arrived, the latest frame is no older than `unhealthy_after_s`, and
there is no current capture/recovery error.

## Capture and recovery flow

For Picamera2, frame reads call `capture_array(wait=2.0)` instead of waiting
without a bound. A successful frame is encoded, stored, assigned the next
sequence number, and broadcast to waiting stream clients.

On timeout or another capture exception:

1. record a diagnostic error and wake waiting clients;
2. call `cancel_all_and_flush()` for the timed-out Picamera2 job;
3. close the current backend;
4. wait the 0.5-second recovery backoff;
5. reopen the camera if at least one viewer remains;
6. clear the error only after the backend reopens and a fresh frame arrives.

The capture loop must catch errors rather than terminating silently. If reopening
fails, it repeats with the same bounded backoff while viewers exist. Once no
viewers remain for the existing idle period, it closes normally.

If Picamera2 construction or startup fails partway through, the temporary
Picamera2 object is closed before returning failure so device handles are not
leaked.

## Frame delivery and viewer cleanup

Freshness uses the frame sequence rather than Python object identity.
`snapshot()` waits for a sequence newer than the one present when the request
started, so it cannot return an indefinitely cached frame as if it were live.

The MJPEG generator waits for a sequence change. A normal recovery keeps the
same stream open and resumes when a fresh frame arrives. If no fresh frame is
produced for 10 seconds, the generator exits, its `viewer()` context decrements
the count, and the server can release a dead TCP client. This also guarantees
that a permanently unavailable camera cannot accumulate viewer threads forever.

Starlette iterates the synchronous generator in its thread pool. The camera
stream route therefore uses a closeable streaming response that closes the
exact iterator, off the event loop, when the ASGI response completes, its send
fails, or the client disconnects. If a synchronous `next()` is already running,
cleanup waits for that bounded call to return before closing, avoiding a
concurrent `generator.close()` race.

## Deployment and rollback

Before deployment:

- re-check that clinostat control and experiments are stopped;
- run the focused camera/server tests and the full integration suite;
- save and hash timestamped copies of the remote `camera.py` and `server.py`.

The incident deployment does **not** invoke `deploy/deploy_to_pi.sh`, because
that general-purpose script also deploys gateway files and restarts
`spacebio-gateway.service`. Instead, treat `camera.py` and `server.py` as one
incident unit: stage, compile, hash, and install both before loading either,
then restart only `clinostat.service`. Every SSH operation has finite connect
and keepalive limits. Because the operator host login shell is zsh, the entire
Task 4 procedure runs in one explicitly verified persistent Bash process; each
command block checks the recorded Bash PID before proceeding. The service is
configured with `Restart=always`; because direct `systemctl restart` is
unavailable to the SSH user, validate its exact MainPID, unit user, process
UID, and argv before signaling that PID and letting systemd recreate it. Do
not upload, signal, or restart any SpaceBio gateway file or process.

Adding `camera.py` beside the existing `server.py` entry in `EXTENSION_PAIRS`
only makes both canonical sources part of later full-system deployments; the
modified general-purpose script is tested but is not executed during this
incident.

Before the first live handoff, arm a local failure handler while all recorded
hashes, paths, and PIDs are still in scope. Any failed live criterion
automatically disables recursive traps, restores both validated backups with
live-hash guards, performs an exact-target clinostat-only handoff, verifies
both old hashes, stopped state, root response, and unchanged gateway PID, then
exits nonzero. Before calling rollback, the handler clears its armed-state flag
so shell-specific function trap scope cannot invoke rollback twice. Old/old
resumes handoff without rewriting; a pair composed only of the recorded
old/new hashes restores both backups; any unknown hash aborts without
overwrite. The manual rollback commands use the same classification as an
auditable fallback, not the primary failure path.

After restart, verify:

- the service has a new PID and is active;
- control and experiment state remain stopped;
- camera status becomes healthy with an advancing sequence;
- multiple snapshots advance frame sequence and have different content hashes;
- an MJPEG request receives multiple boundaries within a few seconds;
- a disconnected diagnostic stream does not leave the viewer count elevated.

Rollback restores the timestamped `camera.py` and `server.py` backups as a
validated pair, restarts only `clinostat.service`, and verifies the restored
hashes and stopped state. It never changes or restarts the gateway.

## Tests

Tests use fake cv2/Picamera2 backends and short injected timeout values; no real
camera is needed.

Required regressions:

1. Picamera2 reads receive the configured numeric timeout.
2. A capture timeout cancels pending Picamera2 jobs, closes the backend, records
   an error, and attempts reopen while a viewer exists.
3. A later successful frame clears the error and advances health/sequence.
4. Status reports an open-but-stale backend as unhealthy.
5. Snapshot refuses a cached frame and waits for a newer sequence.
6. MJPEG delivery is keyed by sequence, not object identity.
7. A stalled MJPEG generator exits and decrements its viewer count.
8. Existing tests remain green, and the deployment manifest adds `camera.py`
   alongside its existing `server.py` entry.
9. Real Starlette response-lifecycle tests close the exact synchronous stream
   iterator once, off the event loop, after disconnect, send failure, or normal
   completion, including an in-flight bounded `next()`.

## Success criteria

The production endpoint delivers more than one distinct JPEG frame, frame
sequence advances continuously during viewing, status reports healthy, and the
ASGI response lifecycle closes a disconnected diagnostic stream so the viewer
count returns to its pre-request value. The deployed `camera.py` and `server.py`
hashes match the reviewed incident pair, while the SpaceBio gateway PID is
unchanged. The complete integration test suite passes before deployment.
