"""무선 실기 펌프 백엔드 (MQTT) — 브로커 없이 가짜 클라이언트로 검증."""

from __future__ import annotations

import asyncio

import pytest

from gateway.api_models import (
    PumpEmergencyStopRequest,
    PumpResetEmergencyStopRequest,
    PumpState,
    PumpStepRequest,
    PumpStopRequest,
)
from gateway.mqtt_pump import (
    STATUS_TOPIC,
    MqttPump,
    PumpConflictError,
    parse_board_status,
)


class FakeClient:
    """paho 클라이언트 대역 — publish·subscribe를 기록만 한다."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.subscribed: list[str] = []
        self.on_message = None
        self.on_connect = None

    def username_pw_set(self, *a, **k): ...
    def connect(self, *a, **k): ...
    def loop_start(self): ...
    def loop_stop(self): ...
    def disconnect(self): ...

    def subscribe(self, topic, *a, **k):
        self.subscribed.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def fire_connect(self):
        """브로커가 CONNACK을 돌려준 상황을 재현한다."""
        assert self.on_connect is not None, "on_connect 핸들러가 등록되지 않았다"
        self.on_connect(self, None, {}, 0)


BOARD_LINE = ("ip=125.135.141.9,rssi=-48,run=off,mode=IDLE,current_spm=0,"
              "spin_rem=0,manual_rem=0,spinsteps=0,spinspm=2400,spm=1200,pos=782")


def _pump():
    client = FakeClient()
    pump = MqttPump(client_factory=lambda: client)
    pump.connect()
    return pump, client


# ─────────────────────────── 상태 파싱 ───────────────────────────

def test_parse_board_status_reads_pos_and_run():
    board = parse_board_status(BOARD_LINE)
    assert board.position_steps == 782
    assert board.run is False
    assert board.spm == 1200


def test_parse_board_status_rejects_incomplete_line():
    assert parse_board_status("garbage") is None
    assert parse_board_status("ip=1.2.3.4,rssi=-40") is None  # pos/run 없음


# ─────────────────────────── 명령 발행 ───────────────────────────

@pytest.mark.asyncio
async def test_step_publishes_spm_then_manualstep():
    pump, client = _pump()
    await pump.step(PumpStepRequest(request_id="r", steps=200, spm=600))
    assert client.published == [
        ("s25007/board1/cmd", "spm:600"),
        ("s25007/board1/cmd", "manualstep:200"),
    ]


@pytest.mark.asyncio
async def test_status_reflects_board_telemetry():
    pump, _ = _pump()
    pump.feed_status(BOARD_LINE)
    status = await pump.status()
    assert status.mode == "WIRELESS"
    assert status.position_steps == 782
    assert status.spm == 1200
    assert status.state is PumpState.IDLE


@pytest.mark.asyncio
async def test_running_board_maps_to_running_state():
    pump, _ = _pump()
    pump.feed_status(BOARD_LINE.replace("run=off", "run=on"))
    assert (await pump.status()).state is PumpState.RUNNING


# ─────────────────────────── 안전: 소프트웨어 estop 래치 ───────────────────────────

@pytest.mark.asyncio
async def test_emergency_stop_latches_and_blocks_step():
    pump, client = _pump()
    await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e"))
    assert ("s25007/board1/cmd", "stop") in client.published  # 하드웨어 정지 먼저
    status = await pump.status()
    assert status.estop_latched is True
    assert status.state is PumpState.EMERGENCY_STOPPED
    with pytest.raises(PumpConflictError):
        await pump.step(PumpStepRequest(request_id="r", steps=10, spm=600))


@pytest.mark.asyncio
async def test_stop_is_allowed_even_while_latched():
    pump, client = _pump()
    await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e"))
    await pump.stop(PumpStopRequest(request_id="s"))          # 예외 없이 정지 명령
    assert client.published.count(("s25007/board1/cmd", "stop")) >= 2


@pytest.mark.asyncio
async def test_reset_clears_latch():
    pump, _ = _pump()
    await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e"))
    result = await pump.reset_emergency_stop(
        PumpResetEmergencyStopRequest(request_id="x",
                                      acknowledgement="RESET_SIMULATED_PUMP_ESTOP"))
    assert result.accepted is True
    assert result.estop_latched is False
    assert (await pump.status()).estop_latched is False


@pytest.mark.asyncio
async def test_offline_publish_raises_conflict_not_crash():
    pump = MqttPump()          # connect 안 함 → client None
    with pytest.raises(PumpConflictError):
        await pump.step(PumpStepRequest(request_id="r", steps=10, spm=600))


# ─────────────────────────── 구독 (재접속 포함) ───────────────────────────
#
# 구독을 connect() 직후에 명령형으로 부르면 두 가지가 깨진다.
#   ① paho 의 connect() 는 비블로킹이라 CONNACK 전에 SUBSCRIBE 가 나가 유실될 수 있다.
#   ② clean_session 기본값에서 재접속하면 브로커가 구독을 버리는데, 다시 걸어줄
#      경로가 없어 **영구히 상태를 못 받는다.** 명령(publish)은 구독과 무관하게
#      계속 되므로 "명령은 가는데 position_steps 가 null" 인 상태로 조용히 남는다.
#      (2026-08-04 실기에서 실제로 이 상태였다.)
# 그래서 구독은 반드시 on_connect 안에서, 접속할 때마다 이뤄져야 한다.

def test_subscribe_happens_on_connack_not_before():
    _, client = _pump()
    assert client.subscribed == [], "CONNACK 전에 구독하면 유실될 수 있다"
    client.fire_connect()
    assert client.subscribed == [STATUS_TOPIC]


def test_resubscribes_on_every_reconnect():
    _, client = _pump()
    client.fire_connect()
    client.fire_connect()      # 브로커 재시작·네트워크 끊김 후 자동 재접속
    assert client.subscribed == [STATUS_TOPIC, STATUS_TOPIC]


@pytest.mark.asyncio
async def test_status_flows_after_reconnect():
    pump, client = _pump()
    client.fire_connect()
    pump.feed_status(BOARD_LINE)
    assert (await pump.status()).position_steps == 782
