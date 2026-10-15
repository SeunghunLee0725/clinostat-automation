#!/usr/bin/env python3
"""펌프 2대 브링업 — USB 시리얼로 보드에 명령을 보내 두 엔진이 따로 도는지 확인한다.

브로커·게이트웨이 없이 보드만 꽂으면 된다(펌웨어는 오프라인에서도 시리얼 명령을 받는다).
파이에서:   ~/spacebio-gateway/.venv/bin/python bringup_2pump.py
맥에서:     python3 bringup_2pump.py            (pyserial 필요)

USB 없이 MQTT 로도 같은 절차를 돌릴 수 있다(펌웨어가 cmd 토픽을 시리얼과 같은 파서로 처리).
보드를 파이 USB 에 꽂으면 Pi 4 가 저전압 재부팅하는 경우(2026-10-15) 이 경로를 쓴다:
파이에서:   ~/spacebio-gateway/.venv/bin/python bringup_2pump.py --mqtt
            (브로커 주소·계정은 --config 의 mqtt: 절에서 읽는다. 기본 ~/spacebio-gateway/config.yaml)

기본 순서:
  help → 2펌프 펌웨어인지 확인 (p1:/p2: 안내가 있어야 한다)
  wiring/status → 두 펌프의 핀과 pos 기준값
  p1:hz:H:S → 펌프 1 만 pos 증가, 펌프 2 는 그대로
  p2:hz:H:S → 펌프 2 만 pos 증가
  p1:hz:H:S + p2:hz:2H:S 동시 → 둘 다 증가, 펌프 2 가 약 2배
  stopall → 둘 다 run=off

⚠ pos 가 늘어난 것은 MCU 가 펄스를 냈다는 뜻이지 모터가 돌았다는 증거가 아니다.
  각 단계에서 눈으로 어느 모터가 도는지 확인하고, 안 돌면 그 펌프에 `pN:sweep` 을 돌린다.

  --cmd "p2:sweep"        명령 하나를 보내고 --listen 초 동안 응답을 흘려 보여준다
  --status-only           구동 없이 wiring/status 만
"""

from __future__ import annotations

import argparse
import glob
import os
import queue
import sys
import time
from typing import Union

BAUD = 115200
PORT_PATTERNS = (
    "/dev/serial/by-id/*Espressif*",   # 파이 — ACM 번호는 매번 바뀌므로 by-id 로 찾는다
    "/dev/cu.usbmodem*",               # 맥
)


def find_port() -> str:
    for pattern in PORT_PATTERNS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    sys.exit("ESP32 시리얼 포트를 찾지 못했다. --port 로 지정하거나 USB 연결을 확인하라.")


def parse_status(lines: list[str]) -> dict[int, dict[str, str]]:
    """`status` 응답(pump=N,...) 을 펌프별 dict 로."""
    out: dict[int, dict[str, str]] = {}
    for line in lines:
        if not line.startswith("pump="):
            continue
        fields = dict(kv.split("=", 1) for kv in line.split(",") if "=" in kv)
        out[int(fields["pump"])] = fields
    return out


class SerialBoard:
    def __init__(self, port: str) -> None:
        import serial  # pyserial

        self.ser = serial.Serial(port, BAUD, timeout=0.2)
        time.sleep(0.5)
        self.drain(1.0)                 # 부팅 로그·이전 출력 버리기

    def drain(self, seconds: float) -> list[str]:
        end = time.monotonic() + seconds
        lines: list[str] = []
        while time.monotonic() < end:
            raw = self.ser.readline()
            if raw:
                lines.append(raw.decode("utf-8", "replace").rstrip())
        return lines

    def send(self, cmd: str, listen: float = 0.8, echo: bool = True) -> list[str]:
        self.ser.write((cmd + "\n").encode())
        self.ser.flush()
        lines = self.drain(listen)
        if echo:
            print(f"> {cmd}")
            for line in lines:
                print(f"  {line}")
        return lines

    def status(self) -> dict[int, dict[str, str]]:
        return parse_status(self.send("status", echo=False))


MQTT_CMD_TOPIC = "s25007/board1/cmd"        # p1:/p2: 접두어를 펌웨어가 해석하므로 토픽 하나로 충분
MQTT_REPLY_TOPIC = "s25007/board1/reply"
DEFAULT_CONFIG = os.path.expanduser("~/spacebio-gateway/config.yaml")


def load_mqtt_config(path: str) -> dict[str, object]:
    import yaml

    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    mqtt = raw.get("mqtt") or {}
    return {
        "host": str(mqtt.get("host", "127.0.0.1")),
        "port": int(mqtt.get("port", 1883)),
        "username": mqtt.get("username"),
        "password": mqtt.get("password"),
    }


class MqttBoard:
    """시리얼 대신 브로커를 거쳐 보드에 명령을 보낸다. 응답은 reply 토픽으로 돌아온다."""

    def __init__(self, cfg: dict[str, object]) -> None:
        import paho.mqtt.client as mqtt

        self._rx: queue.Queue[str] = queue.Queue()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bringup-2pump")
        if cfg["username"]:
            self._client.username_pw_set(str(cfg["username"]), str(cfg["password"] or ""))
        self._client.on_message = lambda _c, _u, msg: self._rx.put(msg.payload.decode("utf-8", "replace"))
        self._client.connect(str(cfg["host"]), int(cfg["port"]), keepalive=30)
        self._client.subscribe(MQTT_REPLY_TOPIC)
        self._client.loop_start()
        time.sleep(0.5)
        self.drain(0.5)

    def drain(self, seconds: float) -> list[str]:
        end = time.monotonic() + seconds
        lines: list[str] = []
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return lines
            try:
                lines.append(self._rx.get(timeout=remaining).rstrip())
            except queue.Empty:
                return lines

    def send(self, cmd: str, listen: float = 0.8, echo: bool = True) -> list[str]:
        info = self._client.publish(MQTT_CMD_TOPIC, cmd, qos=1)
        info.wait_for_publish(timeout=3)
        lines = self.drain(max(listen, 1.5))     # 무선 왕복은 시리얼보다 느리다
        if echo:
            print(f"> {cmd}")
            for line in lines:
                print(f"  {line}")
        return lines

    def status(self) -> dict[int, dict[str, str]]:
        return parse_status(self.send("status", echo=False))


Board = Union[SerialBoard, MqttBoard]


def expected_pulses(hz: float, sec: float) -> float:
    """펌웨어 램프업(1 s, 100 Hz→hz) 을 반영한 이론 펄스 수. 60 Hz 이하는 램프 없음."""
    if hz <= 60:
        return hz * sec
    return (100 + hz) / 2.0 + hz * (sec - 1)


def within(actual: float, expected: float, tol: float) -> bool:
    return expected > 0 and abs(actual - expected) / expected <= tol


def run_sequence(board: Board, hz: float, sec: float) -> int:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}\n")

    help_lines = board.send("help", listen=1.0)
    record("2펌프 펌웨어", any("p2:" in l for l in help_lines),
           "help 에 p1:/p2: 안내가 있어야 2026-10-15 이후 펌웨어다")

    board.send("wiring", listen=1.0)
    base = board.status()
    record("두 펌프 status", set(base) == {1, 2},
           f"status 가 펌프 {sorted(base)} 를 보고 (1,2 여야 한다)")
    if set(base) != {1, 2}:
        return summarize(results)

    pos = lambda st, n: int(st[n]["pos"])
    exp = expected_pulses(hz, sec)

    # ── 펌프 1 단독 ──
    board.send(f"p1:hz:{hz:.0f}:{sec:.0f}")
    print(f"  … {sec + 2:.0f} s 대기 — 모터 1 만 도는지 눈으로 확인")
    time.sleep(sec + 2)
    st = board.status()
    d1, d2 = pos(st, 1) - pos(base, 1), pos(st, 2) - pos(base, 2)
    record("펌프 1 단독 구동", within(d1, exp, 0.05) and d2 == 0,
           f"Δpos1={d1} (이론 {exp:.0f}) Δpos2={d2} (0 이어야)")
    base = st

    # ── 펌프 2 단독 ──
    board.send(f"p2:hz:{hz:.0f}:{sec:.0f}")
    print(f"  … {sec + 2:.0f} s 대기 — 모터 2 만 도는지 눈으로 확인")
    time.sleep(sec + 2)
    st = board.status()
    d1, d2 = pos(st, 1) - pos(base, 1), pos(st, 2) - pos(base, 2)
    record("펌프 2 단독 구동", within(d2, exp, 0.05) and d1 == 0,
           f"Δpos1={d1} (0 이어야) Δpos2={d2} (이론 {exp:.0f})")
    base = st

    # ── 동시 구동, 서로 다른 속도 ──
    exp1, exp2 = exp, expected_pulses(hz * 2, sec)
    board.send(f"p1:hz:{hz:.0f}:{sec:.0f}", listen=0.3)
    board.send(f"p2:hz:{hz * 2:.0f}:{sec:.0f}", listen=0.3)
    print(f"  … {sec + 2:.0f} s 대기 — 둘 다 돌고 모터 2 가 더 빠른지 확인")
    time.sleep(sec + 2)
    st = board.status()
    d1, d2 = pos(st, 1) - pos(base, 1), pos(st, 2) - pos(base, 2)
    record("동시 구동 (독립 속도)", within(d1, exp1, 0.08) and within(d2, exp2, 0.08),
           f"Δpos1={d1} (이론 {exp1:.0f}) Δpos2={d2} (이론 {exp2:.0f})")

    # ── 전체 정지 ──
    board.send("stopall")
    st = board.status()
    record("stopall", all(st[n]["run"] == "off" for n in (1, 2)),
           "run=" + ",".join(st[n]["run"] for n in (1, 2)))
    return summarize(results)


def summarize(results: list[tuple[str, bool, str]]) -> int:
    failed = [r for r in results if not r[1]]
    print("=" * 60)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL':4}  {name}")
    print("=" * 60)
    if failed:
        print(f"{len(failed)} 개 실패. 안 도는 펌프는 `pN:sweep` 으로 STEP 핀을 찾아 "
              "`pN:pin:step:<gpio>` 로 고정한 뒤 다시 돌린다.")
    else:
        print("전부 통과. 눈으로 본 회전이 pos 와 일치했는지 함께 기록할 것.")
    return 1 if failed else 0


def open_board(args: argparse.Namespace) -> Board:
    if args.mqtt:
        cfg = load_mqtt_config(args.config)
        print(f"브로커: {cfg['host']}:{cfg['port']} (계정 {cfg['username'] or '없음'})")
        board = MqttBoard(cfg)
        if not board.send("help", listen=2.0, echo=False):
            sys.exit("보드가 reply 토픽에 응답하지 않는다. 보드 WiFi/브로커 접속을 확인하라 "
                     "(mosquitto_sub -t 's25007/board1/#' 로 status 가 2 초마다 오는지).")
        return board
    port = args.port or find_port()
    print(f"포트: {port}")
    return SerialBoard(port)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="시리얼 포트 (기본: by-id 의 Espressif 링크 자동 탐색)")
    ap.add_argument("--hz", type=float, default=1500, help="단독 구동 펄스율 (기본 1500 = 1/16 µstep 기준 약 0.47 rev/s)")
    ap.add_argument("--sec", type=float, default=5, help="각 구동 시간 (기본 5 s)")
    ap.add_argument("--cmd", help="명령 하나만 보내고 응답을 보여준다 (예: 'p2:sweep')")
    ap.add_argument("--listen", type=float, default=3.0, help="--cmd 응답을 들을 시간 (sweep 은 50 초 이상)")
    ap.add_argument("--status-only", action="store_true", help="구동 없이 wiring/status 만")
    ap.add_argument("--mqtt", action="store_true", help="USB 대신 MQTT 브로커 경유 (보드가 WiFi 로 붙어 있어야 한다)")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="--mqtt 때 브로커 주소·계정을 읽을 게이트웨이 config.yaml")
    args = ap.parse_args()

    board = open_board(args)

    if args.cmd:
        board.send(args.cmd, listen=args.listen)
        return 0
    if args.status_only:
        board.send("wiring", listen=1.0)
        board.send("status", listen=1.0)
        return 0
    return run_sequence(board, args.hz, args.sec)


if __name__ == "__main__":
    sys.exit(main())
