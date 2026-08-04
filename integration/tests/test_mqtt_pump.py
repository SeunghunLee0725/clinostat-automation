"""무선 실기 펌프 백엔드 (MQTT) — 브로커 없이 가짜 클라이언트로 검증."""

from __future__ import annotations

import asyncio

import pytest

from gateway.api_models import (
    PumpDispenseRequest,
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


# ─────────────────────────── 부피 주입 (µL 보정) ───────────────────────────
#
# 펌웨어는 스텝으로만 돈다. µL/스텝 상수를 알면 부피 명령을 스텝으로 환산할 수 있다.
#   steps = target_volume_ul / ul_per_step
#   spm   = (rate_ul_s / ul_per_step) * 60
# 상수는 튜브 규격마다 다르므로 런타임에 넣는다. 없으면 추측하지 않고 거절한다.

@pytest.mark.asyncio
async def test_dispense_without_calibration_is_rejected():
    pump, _ = _pump()
    with pytest.raises(PumpConflictError) as exc:
        await pump.dispense(PumpDispenseRequest(
            request_id="d", rate_ul_s=10.0, target_volume_ul=120.0))
    assert exc.value.code == "pump_volume_uncalibrated"


@pytest.mark.asyncio
async def test_dispense_converts_volume_to_steps_and_rate_to_spm():
    pump, client = _pump()
    pump.set_calibration(0.5)          # 0.5 µL/스텝
    await pump.dispense(PumpDispenseRequest(
        request_id="d", rate_ul_s=10.0, target_volume_ul=120.0))
    # 120 µL / 0.5 = 240 스텝,  10 µL/s / 0.5 = 20 스텝/s → 1200 spm
    assert client.published == [
        ("s25007/board1/cmd", "spm:1200"),
        ("s25007/board1/cmd", "manualstep:240"),
    ]


@pytest.mark.asyncio
async def test_dispense_clamps_spm_to_firmware_range():
    """환산값이 펌웨어 상한(2400)을 넘으면 잘라낸다 — 거절하면 쓸 수 없다."""
    pump, client = _pump()
    pump.set_calibration(0.5)
    await pump.dispense(PumpDispenseRequest(
        request_id="d", rate_ul_s=100.0, target_volume_ul=120.0))
    assert ("s25007/board1/cmd", "spm:2400") in client.published


@pytest.mark.asyncio
async def test_dispense_rejects_volume_smaller_than_one_step():
    pump, _ = _pump()
    pump.set_calibration(50.0)         # 1스텝 = 50 µL
    with pytest.raises(PumpConflictError) as exc:
        await pump.dispense(PumpDispenseRequest(
            request_id="d", rate_ul_s=10.0, target_volume_ul=20.0))  # 0.4 스텝
    assert exc.value.code == "pump_volume_too_small"


@pytest.mark.asyncio
async def test_dispense_while_latched_is_rejected():
    pump, _ = _pump()
    pump.set_calibration(0.5)
    await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e"))
    with pytest.raises(PumpConflictError) as exc:
        await pump.dispense(PumpDispenseRequest(
            request_id="d", rate_ul_s=10.0, target_volume_ul=120.0))
    assert exc.value.code == "pump_estop_latched"


@pytest.mark.asyncio
async def test_status_reports_calibration_and_delivered_volume():
    pump, _ = _pump()
    pump.set_calibration(0.5)
    pump.feed_status(BOARD_LINE)                       # pos=782
    await pump.dispense(PumpDispenseRequest(
        request_id="d", rate_ul_s=10.0, target_volume_ul=120.0))
    pump.feed_status(BOARD_LINE.replace("pos=782", "pos=882"))   # 100 스텝 진행
    status = await pump.status()
    assert status.ul_per_step == 0.5
    assert status.delivered_volume_ul == 50.0          # 100 스텝 × 0.5
    assert status.target_volume_ul == 120.0


@pytest.mark.asyncio
async def test_delivered_volume_does_not_exceed_target():
    """보드가 목표를 넘겨 돌아도 전달량은 목표에서 멈춘다 — 거짓 보고 방지."""
    pump, _ = _pump()
    pump.set_calibration(0.5)
    pump.feed_status(BOARD_LINE)
    await pump.dispense(PumpDispenseRequest(
        request_id="d", rate_ul_s=10.0, target_volume_ul=120.0))
    pump.feed_status(BOARD_LINE.replace("pos=782", "pos=1782"))  # 1000 스텝
    assert (await pump.status()).delivered_volume_ul == 120.0


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
