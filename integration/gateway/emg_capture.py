"""근육 응답 전압 캡처 CLI (2026-10-30) — 보드에 `emg:cap:<ms>` 를 걸고 결과를 CSV 로 저장한다.

    python -m gateway.emg_capture --host <브로커> --ms 500 [--pga 2048] [--out emg.csv]

브로커 계정이 있으면 환경변수 MQTT_USER / MQTT_PASS 로 준다. 보드 응답(요약 줄)은
`s25007/board1/reply` 에서 받아 그대로 출력한다. 대시보드 연동 전 실측용 도구다.
"""

from __future__ import annotations

import argparse
import csv
import os
import queue
import sys
import time
from pathlib import Path
from typing import Optional

from gateway.emg_packet import EMG_TOPIC, EmgAssembler, EmgCapture, EmgPacketError

CMD_TOPIC = "s25007/board1/cmd"
REPLY_TOPIC = "s25007/board1/reply"
VALID_PGA_MV = (4096, 2048, 1024, 512, 256)


def write_csv(capture: EmgCapture, path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_us", "raw", "mv"])
        for (t, raw), (_, mv) in zip(capture.samples_raw, capture.samples_mv):
            w.writerow([t, raw, f"{mv:.4f}"])


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="근육 응답 전압 캡처(ADS1115 AIN2-AIN3)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--ms", type=int, default=500, help="캡처 길이 10~2000 ms")
    p.add_argument("--pga", type=int, choices=VALID_PGA_MV, help="풀스케일 mV (보드 NVS 에 저장됨)")
    p.add_argument("--out", type=Path, default=None, help="CSV 경로(기본 emg_<시각>.csv)")
    args = p.parse_args(argv)
    if not 10 <= args.ms <= 2000:
        p.error("--ms 는 10~2000")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    import paho.mqtt.client as mqtt

    args = _parse_args(argv)
    inbox: "queue.Queue[tuple[str, bytes]]" = queue.Queue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"emg-capture-{os.getpid()}")
    if os.environ.get("MQTT_USER"):
        client.username_pw_set(os.environ["MQTT_USER"], os.environ.get("MQTT_PASS"))
    client.on_message = lambda _c, _u, m: inbox.put((m.topic, m.payload))
    try:
        client.connect(args.host, args.port, keepalive=30)
    except OSError as e:
        print(f"브로커 접속 실패 {args.host}:{args.port} — {e}", file=sys.stderr)
        return 2
    client.subscribe([(EMG_TOPIC, 0), (REPLY_TOPIC, 0)])
    client.loop_start()
    try:
        time.sleep(0.5)  # SUBACK 대기 — 발행보다 구독이 늦으면 첫 조각을 놓친다
        if args.pga:
            client.publish(CMD_TOPIC, f"emg:pga:{args.pga}")
        client.publish(CMD_TOPIC, f"emg:cap:{args.ms}")
        capture = _wait_capture(inbox, deadline=time.monotonic() + args.ms / 1000 + 10)
    finally:
        client.loop_stop()
        client.disconnect()
    if capture is None:
        print("캡처를 받지 못했다(보드 응답·MQTT 연결 확인)", file=sys.stderr)
        return 1
    out = args.out or Path(time.strftime("emg_%Y%m%d_%H%M%S.csv"))
    write_csv(capture, out)
    s = capture.summary()
    print(f"저장 {out}  n={s['n']}  min={s['min_mv']:.2f} max={s['max_mv']:.2f} "
          f"pp={s['pp_mv']:.2f} mV  포화={s['saturated']}")
    return 0


def _wait_capture(inbox: "queue.Queue[tuple[str, bytes]]", deadline: float) -> Optional[EmgCapture]:
    asm = EmgAssembler()
    while (left := deadline - time.monotonic()) > 0:
        try:
            topic, payload = inbox.get(timeout=left)
        except queue.Empty:
            break
        if topic == REPLY_TOPIC:
            print("보드:", payload.decode("utf-8", "replace"))
            continue
        try:
            capture = asm.feed(payload)
        except EmgPacketError as e:
            print(f"조각 무시: {e}", file=sys.stderr)
            continue
        if capture is not None:
            return capture
    return None


if __name__ == "__main__":
    sys.exit(main())
