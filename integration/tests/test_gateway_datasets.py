"""dataset 목록 엔드포인트 (화면의 CSV 모드가 이것 없이는 동작하지 않는다).

패널의 데이터셋 드롭다운은 이 목록으로 채워진다. 목록이 없으면 운영자가
dataset_id를 알 방법이 없어 CSV 재생을 아예 시작할 수 없다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

WORK = Path(__file__).resolve().parents[1] / "clinostat_extension" / "work"


def test_gateway_exposes_dataset_list(client):
    body = client.get("/api/sensor/datasets").json()
    assert body["schema_version"] == 1
    datasets = body["data"]["datasets"]
    assert datasets, "등록된 dataset이 하나도 없다"
    entry = datasets[0]
    assert {"dataset_id", "sample_count", "provenance"} <= set(entry)


def test_dataset_list_matches_the_manifest(client):
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "datasets" / "manifest.json")
        .read_text(encoding="utf-8")
    )
    expected = {d["dataset_id"] for d in manifest["datasets"]}
    live = {d["dataset_id"] for d in client.get("/api/sensor/datasets").json()["data"]["datasets"]}
    assert live == expected


def test_dataset_list_never_leaks_filesystem_paths(client):
    """dataset_id는 allowlist 키다. 경로를 노출하면 traversal 표면이 생긴다."""
    raw = client.get("/api/sensor/datasets").text
    assert "/home/" not in raw
    assert "datasets_dir" not in raw
    for entry in client.get("/api/sensor/datasets").json()["data"]["datasets"]:
        assert "filename" not in entry
        assert "path" not in entry


def test_proxy_allowlists_the_dataset_route():
    import sys
    sys.path.insert(0, str(WORK))
    import spacebio_proxy

    assert "/api/spacebio/sensor/datasets" in spacebio_proxy.ROUTE_MAP
    assert spacebio_proxy.ROUTE_MAP["/api/spacebio/sensor/datasets"] == "/api/sensor/datasets"


def test_proxy_allowlists_the_calibration_route():
    """ROUTE_MAP 은 허용목록이다 — 등록하지 않으면 화면에서 404 가 난다."""
    import sys
    sys.path.insert(0, str(WORK))
    import spacebio_proxy

    assert spacebio_proxy.ROUTE_MAP["/api/spacebio/pump/calibration"] == \
        "/api/pump/calibration"


def test_clinostat_registers_the_calibration_proxy_route():
    """등록 지점이 둘이다 — ROUTE_MAP 만 고치면 :8000 에 라우트가 안 생긴다."""
    source = (WORK / "server.py").read_text(encoding="utf-8")
    assert '"/api/spacebio/pump/calibration"' in source


def test_clinostat_registers_the_dataset_proxy_route():
    source = (WORK / "server.py").read_text(encoding="utf-8")
    assert '"/api/spacebio/sensor/datasets"' in source


def test_panel_no_longer_exposes_the_dataset_dropdown():
    """센서 패널은 실기 BLE 전용이 됐다 — CSV 재생 UI는 화면에서 제거했다.

    `/api/spacebio/sensor/datasets` 라우트 자체는 남아 있고(위 테스트들이 검증한다)
    필요하면 API로 직접 CSV 모드를 설정할 수 있다.
    """
    html = (WORK / "static" / "index.html").read_text(encoding="utf-8")
    assert "spacebioLoadDatasets" not in html
    assert 'id="resistanceDataset"' not in html


# ─────────────────────────── 실기 배선 (2026-07-25) ───────────────────────────

def test_serial_live_configure_is_accepted(client, monkeypatch):
    """SERIAL_LIVE 설정이 판별 유니온을 통과한다(포트 없이도 configure까지는 됨)."""
    import gateway.runtime as rt

    class _FakeSerialSource:
        def __init__(self, *a, **k): ...
        def start(self): ...
        def tick(self): return None
    monkeypatch.setattr(rt, "SerialSensorSource", _FakeSerialSource)
    data = client.post("/api/sensor/configure", json={"mode": "SERIAL_LIVE"}).json()["data"]
    assert data["mode"] == "SERIAL_LIVE"


def test_pump_step_on_simulated_backend_is_409(client):
    """모의 펌프에는 스텝 개념이 없다 — 실기 백엔드에서만 동작."""
    r = client.post("/api/pump/step", json={"steps": 200, "spm": 600})
    assert r.status_code == 409


def test_pump_step_out_of_range_is_422(client):
    assert client.post("/api/pump/step", json={"steps": 200, "spm": 9999}).status_code == 422


def test_pump_dispense_on_wireless_backend_is_409_not_500(tmp_path):
    """무선 펌프에 부피 명령이 오면 409로 거절해야 한다.

    µL 보정 상수가 없어 MqttPump 는 dispense 를 일부러 구현하지 않는데,
    runtime 이 조건 없이 호출해 **AttributeError → 500** 으로 터졌다
    (2026-08-04 실기 로그에 실제로 찍혔다). 지원하지 않는 것과 고장난 것은
    다르게 보여야 한다.

    ⚠ 코드는 스텝 쪽(`pump_backend_unsupported`)과 **달라야 한다.** 패널은 코드로만
    문구를 고르므로 같은 코드를 쓰면 주입 실패에 "스텝 명령은 무선 펌프에서만
    동작합니다. 현재는 모의 펌프 구성입니다" 라는 **사실과 다른 안내**가 나온다
    (지금은 무선 실기 펌프가 맞고, 막힌 이유는 µL 보정이 없어서다).
    """
    from fastapi.testclient import TestClient
    from gateway.app import create_app
    from tests.conftest import make_gw_config
    import gateway.runtime as rt

    class FakeClient:
        def __init__(self): self.published = []; self.on_message = None; self.on_connect = None
        def username_pw_set(self, *a, **k): ...
        def connect(self, *a, **k): ...
        def subscribe(self, *a, **k): ...
        def loop_start(self): ...
        def loop_stop(self): ...
        def disconnect(self): ...
        def publish(self, t, p): self.published.append((t, p))

    fake = FakeClient()
    real_init = rt.MqttPump.__init__

    def patched_init(self, **kwargs):
        kwargs["client_factory"] = lambda: fake
        real_init(self, **kwargs)
    rt.MqttPump.__init__ = patched_init
    try:
        cfg = make_gw_config(tmp_path, pump_backend="wireless")
        app = create_app(cfg)
        with TestClient(app) as c:
            r = c.post("/api/pump/dispense",
                       json={"rate_ul_s": 10.0, "target_volume_ul": 100.0})
            assert r.status_code == 409, f"500이 아니라 409여야 한다 (실제 {r.status_code})"
            assert r.json()["error"]["code"] == "pump_volume_uncalibrated"
    finally:
        rt.MqttPump.__init__ = real_init


def test_pump_calibration_enables_volume_dispense(tmp_path):
    """µL 보정을 넣으면 부피 주입이 스텝으로 환산돼 발행된다."""
    from fastapi.testclient import TestClient
    from gateway.app import create_app
    from tests.conftest import make_gw_config
    import gateway.runtime as rt

    class FakeClient:
        def __init__(self): self.published = []; self.on_message = None; self.on_connect = None
        def username_pw_set(self, *a, **k): ...
        def connect(self, *a, **k): ...
        def subscribe(self, *a, **k): ...
        def loop_start(self): ...
        def loop_stop(self): ...
        def disconnect(self): ...
        def publish(self, t, p): self.published.append((t, p))

    fake = FakeClient()
    real_init = rt.MqttPump.__init__

    def patched_init(self, **kwargs):
        kwargs["client_factory"] = lambda: fake
        real_init(self, **kwargs)
    rt.MqttPump.__init__ = patched_init
    try:
        cfg = make_gw_config(tmp_path, pump_backend="wireless")
        app = create_app(cfg)
        with TestClient(app) as c:
            # 보정 전에는 거절
            assert c.post("/api/pump/dispense",
                          json={"rate_ul_s": 10.0, "target_volume_ul": 120.0}
                          ).status_code == 409

            r = c.post("/api/pump/calibration", json={"ul_per_step": 0.5})
            assert r.status_code == 200
            assert r.json()["data"]["ul_per_step"] == 0.5

            r = c.post("/api/pump/dispense",
                       json={"rate_ul_s": 10.0, "target_volume_ul": 120.0})
            assert r.status_code == 200, r.text
        assert ("s25007/board1/cmd", "manualstep:240") in fake.published
        assert ("s25007/board1/cmd", "spm:1200") in fake.published
    finally:
        rt.MqttPump.__init__ = real_init


def test_pump_calibration_rejects_non_positive(tmp_path):
    from fastapi.testclient import TestClient
    from gateway.app import create_app
    from tests.conftest import make_gw_config

    app = create_app(make_gw_config(tmp_path))
    with TestClient(app) as c:
        assert c.post("/api/pump/calibration", json={"ul_per_step": 0}).status_code == 422
        assert c.post("/api/pump/calibration", json={"ul_per_step": -1}).status_code == 422


def test_pump_step_while_latched_reports_pump_code_not_sensor(tmp_path):
    """래치 중 스텝은 **펌프** 오류로 나가야 한다.

    runtime 이 MqttPumpConflictError 를 SensorConflictError 로 재포장하면 코드가
    `sensor_conflict` 가 되고, 패널은 그걸 '측정 정지를 먼저 누르세요' 로 옮긴다.
    센서를 안 쓰는 구성에서는 **실행할 수 없는 안내**이고, 진짜 해법인 '비상정지
    해제' 는 어디에도 안 나온다 — 2026-08-04 실기에서 실제로 막혔던 지점이다.
    """
    from fastapi.testclient import TestClient
    from gateway.app import create_app
    from tests.conftest import make_gw_config
    import gateway.runtime as rt

    class FakeClient:
        def __init__(self): self.published = []; self.on_message = None; self.on_connect = None
        def username_pw_set(self, *a, **k): ...
        def connect(self, *a, **k): ...
        def subscribe(self, *a, **k): ...
        def loop_start(self): ...
        def loop_stop(self): ...
        def disconnect(self): ...
        def publish(self, t, p): self.published.append((t, p))

    fake = FakeClient()
    real_init = rt.MqttPump.__init__

    def patched_init(self, **kwargs):
        kwargs["client_factory"] = lambda: fake
        real_init(self, **kwargs)
    rt.MqttPump.__init__ = patched_init
    try:
        cfg = make_gw_config(tmp_path, pump_backend="wireless")
        app = create_app(cfg)
        with TestClient(app) as c:
            assert c.post("/api/pump/emergency-stop", json={}).status_code == 200
            r = c.post("/api/pump/step", json={"steps": 200, "spm": 600})
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "pump_estop_latched"
    finally:
        rt.MqttPump.__init__ = real_init


def test_pump_step_works_on_wireless_backend(tmp_path):
    """실기 백엔드면 스텝이 MQTT로 발행되고 상태에 스텝 텔레메트리가 실린다."""
    from fastapi.testclient import TestClient
    from gateway.app import create_app
    from tests.conftest import make_gw_config
    import gateway.runtime as rt

    class FakeClient:
        def __init__(self): self.published = []; self.on_message = None
        def username_pw_set(self, *a, **k): ...
        def connect(self, *a, **k): ...
        def subscribe(self, *a, **k): ...
        def loop_start(self): ...
        def loop_stop(self): ...
        def disconnect(self): ...
        def publish(self, t, p): self.published.append((t, p))

    fake = FakeClient()
    real_init = rt.MqttPump.__init__

    def patched_init(self, **kwargs):
        kwargs["client_factory"] = lambda: fake
        real_init(self, **kwargs)
    rt.MqttPump.__init__ = patched_init
    try:
        cfg = make_gw_config(tmp_path, pump_backend="wireless")
        app = create_app(cfg)
        with TestClient(app) as c:
            data = c.post("/api/pump/step", json={"steps": 200, "spm": 600}).json()["data"]
            assert data["mode"] == "WIRELESS"
        assert ("s25007/board1/cmd", "manualstep:200") in fake.published
    finally:
        rt.MqttPump.__init__ = real_init
