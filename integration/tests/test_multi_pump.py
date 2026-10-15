"""펌프 2대 — `pump_id` 라우팅 (2026-10-15).

ESP32 보드 하나가 DRV8825 두 개를 몬다. 게이트웨이는 펌프마다 백엔드 인스턴스를
하나씩 두고, 요청 본문의 `pump_id`(1부터)로 대상을 고른다. 정지·비상정지·해제는
`pump_id` 를 생략하면 **모든 펌프**에 적용된다 — STOP ALL 경로가 그대로 두 펌프를
멈춰야 하기 때문이다. `pump` 필드는 펌프 1 로 남겨 기존 화면·프록시와 호환한다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.api_models import PumpStepRequest
from gateway.app import create_app
from gateway.mqtt_pump import MqttPump, cmd_topic, status_topic
from gateway.runtime import load_config
from tests.conftest import make_gw_config
from tests.test_mqtt_pump import BOARD_LINE, FakeClient


# ─────────────────────────── MQTT 백엔드: 펌프별 토픽 ───────────────────────────

def test_pump_one_keeps_the_manual_topics_and_pump_two_gets_p2():
    assert cmd_topic(1) == "s25007/board1/cmd"
    assert status_topic(1) == "s25007/board1/status"
    assert cmd_topic(2) == "s25007/board1/p2/cmd"
    assert status_topic(2) == "s25007/board1/p2/status"


@pytest.mark.asyncio
async def test_pump_two_publishes_and_subscribes_on_its_own_topics():
    client = FakeClient()
    pump = MqttPump(pump_id=2, client_factory=lambda: client)
    pump.connect()
    client.fire_connect()
    assert client.subscribed == ["s25007/board1/p2/status"]

    await pump.step(PumpStepRequest(request_id="r", steps=50, spm=600))
    assert client.published == [
        ("s25007/board1/p2/cmd", "spm:600"),
        ("s25007/board1/p2/cmd", "manualstep:50"),
    ]


@pytest.mark.asyncio
async def test_status_carries_pump_id_and_ignores_the_boards_pump_field():
    client = FakeClient()
    pump = MqttPump(pump_id=2, client_factory=lambda: client)
    pump.connect()
    pump.feed_status("pump=2," + BOARD_LINE)       # 펌웨어가 앞에 pump= 를 붙인다
    status = await pump.status()
    assert status.pump_id == 2
    assert status.position_steps == 782


# ─────────────────────────── 런타임/API: 모의 펌프 2대 ───────────────────────────

@pytest.fixture
def two_pump_client(tmp_path: Path):
    app = create_app(make_gw_config(tmp_path, pump_count=2))
    with TestClient(app) as test_client:
        yield test_client


def _dispense(client: TestClient, request_id: str, pump_id: int | None = None):
    body = {"request_id": request_id, "rate_ul_s": 200.0, "target_volume_ul": 1.0}
    if pump_id is not None:
        body["pump_id"] = pump_id
    return client.post("/api/pump/dispense", json=body)


def test_status_lists_every_pump_and_pump_stays_pump_one(two_pump_client: TestClient):
    full = two_pump_client.get("/api/status").json()["data"]
    assert [p["pump_id"] for p in full["pumps"]] == [1, 2]
    assert full["pump"] == full["pumps"][0]
    assert two_pump_client.get("/api/pump/status").json()["data"] == full["pumps"][0]


def test_single_pump_config_lists_one_pump(client: TestClient):
    full = client.get("/api/status").json()["data"]
    assert [p["pump_id"] for p in full["pumps"]] == [1]


def test_dispense_targets_only_the_requested_pump(two_pump_client: TestClient):
    resp = _dispense(two_pump_client, "d2", pump_id=2)
    assert resp.status_code == 200
    assert resp.json()["data"]["pump_id"] == 2
    assert resp.json()["data"]["state"] == "running"

    pumps = two_pump_client.get("/api/status").json()["data"]["pumps"]
    assert pumps[0]["state"] == "idle"
    assert pumps[1]["state"] == "running"


def test_dispense_without_pump_id_goes_to_pump_one(two_pump_client: TestClient):
    assert _dispense(two_pump_client, "d1").json()["data"]["pump_id"] == 1


def test_stop_without_pump_id_stops_every_pump(two_pump_client: TestClient):
    _dispense(two_pump_client, "d1", pump_id=1)
    _dispense(two_pump_client, "d2", pump_id=2)
    resp = two_pump_client.post("/api/pump/stop", json={"request_id": "s-all"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["state"] == "stopped"                       # 펌프 1 (호환 형태)
    assert [p["state"] for p in data["pumps"]] == ["stopped", "stopped"]


def test_stop_with_pump_id_leaves_the_other_running(two_pump_client: TestClient):
    _dispense(two_pump_client, "d1", pump_id=1)
    _dispense(two_pump_client, "d2", pump_id=2)
    resp = two_pump_client.post("/api/pump/stop", json={"request_id": "s-2", "pump_id": 2})
    assert resp.json()["data"]["pump_id"] == 2
    assert "pumps" not in resp.json()["data"]

    pumps = two_pump_client.get("/api/status").json()["data"]["pumps"]
    assert pumps[0]["state"] == "running"
    assert pumps[1]["state"] == "stopped"


def test_emergency_stop_latches_every_pump_and_reset_clears_all(two_pump_client: TestClient):
    estop = two_pump_client.post("/api/pump/emergency-stop", json={"request_id": "e"})
    assert [p["estop_latched"] for p in estop.json()["data"]["pumps"]] == [True, True]
    assert _dispense(two_pump_client, "d2", pump_id=2).status_code == 409

    reset = two_pump_client.post(
        "/api/pump/reset-emergency-stop",
        json={"request_id": "r", "acknowledgement": "RESET_SIMULATED_PUMP_ESTOP"},
    )
    assert reset.status_code == 200
    data = reset.json()["data"]
    assert data["accepted"] is True
    assert [p["estop_latched"] for p in data["pumps"]] == [False, False]
    assert _dispense(two_pump_client, "d2b", pump_id=2).status_code == 200


def test_pump_id_beyond_configured_count_is_409(client: TestClient):
    resp = _dispense(client, "d", pump_id=2)          # 기본 설정은 펌프 1대
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "pump_unknown"


def test_pump_id_outside_model_range_is_422(two_pump_client: TestClient):
    assert _dispense(two_pump_client, "d", pump_id=3).status_code == 422
    assert _dispense(two_pump_client, "d", pump_id=0).status_code == 422


def test_pump_events_record_which_pump_moved(tmp_path: Path):
    config = make_gw_config(tmp_path, pump_count=2)
    session_id = "spacebio_20261015_150000_twopump"
    with TestClient(create_app(config)) as c:
        c.post("/api/session/start", json={
            "request_id": "ss", "session_id": session_id,
            "experiment_name": "two-pump", "started_at": "2026-10-15T15:00:00+09:00",
        })
        _dispense(c, "d2", pump_id=2)
        time.sleep(0.05)
        c.post("/api/pump/stop", json={"request_id": "s", "pump_id": 2})
        c.post("/api/session/finish", json={
            "request_id": "sf", "session_id": session_id,
            "finished_at": "2026-10-15T15:01:00+09:00",
        })
    lines = (config.data_root / "sessions" / session_id / "pump_events.jsonl") \
        .read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert events and all(e["pump_id"] == 2 for e in events)


# ─────────────────────────── 설정 ───────────────────────────

def test_load_config_reads_pump_count_and_defaults_to_one(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("pump:\n  backend: null\n  count: 2\n", encoding="utf-8")
    assert load_config(path).pump_count == 2
    path.write_text("pump:\n  backend: null\n", encoding="utf-8")
    assert load_config(path).pump_count == 1


def test_load_config_rejects_pump_count_outside_supported_range(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("pump:\n  count: 3\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
