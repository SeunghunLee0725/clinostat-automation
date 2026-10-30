"""근육 응답 전압 캡처 — 게이트웨이 수신기와 REST (2026-10-30).

보드 없이 가짜 paho 클라이언트로 조각·응답 줄을 흘려 넣고, 대시보드가 쓸
`/api/emg/latest`·`/api/emg/capture` 계약을 확인한다.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.emg_monitor import CMD_TOPIC, REPLY_TOPIC, EmgConflictError, EmgMonitor
from gateway.emg_packet import EMG_HEADER_FORMAT, EMG_SAMPLE_FORMAT, EMG_TOPIC
from tests.conftest import make_gw_config


def _chunk(cap_id, idx, count, samples, pga_mv=2048):
    head = struct.pack(EMG_HEADER_FORMAT, cap_id, idx, count, len(samples), pga_mv)
    return head + b"".join(struct.pack(EMG_SAMPLE_FORMAT, t, r) for t, r in samples)


class FakePaho:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.subscribed: list[str] = []
        self.on_connect = self.on_message = None

    def connect(self, host, port, *a, **k):
        pass

    def loop_start(self):
        self.on_connect(self, None, None, 0, None)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, *a, **k):
        self.subscribed.append(topic)

    def publish(self, topic, payload, *a, **k):
        self.published.append((topic, payload))

    def deliver(self, topic, payload):
        data = payload.encode() if isinstance(payload, str) else payload
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=data))


@pytest.fixture
def fake():
    return FakePaho()


@pytest.fixture
def monitor(fake):
    m = EmgMonitor(host="h", port=1, client_factory=lambda: fake)
    m.connect()
    return m


def test_subscribes_to_emg_and_reply_on_connect(monitor, fake):
    assert {EMG_TOPIC, REPLY_TOPIC} <= set(fake.subscribed)


def test_latest_is_empty_before_any_capture(monitor):
    assert monitor.latest()["capture"] is None


def test_assembled_capture_is_exposed_as_ms_and_mv(monitor, fake):
    fake.deliver(EMG_TOPIC, _chunk(5, 0, 1, [(0, 16384), (2000, -16384)], pga_mv=2048))
    cap = monitor.latest()["capture"]
    assert cap["capture_id"] == 5
    assert cap["t_ms"] == [0.0, 2.0]
    assert cap["mv"] == [1024.0, -1024.0]
    assert cap["summary"]["pp_mv"] == pytest.approx(2048.0)


def test_board_reply_lines_are_kept_only_for_emg(monitor, fake):
    fake.deliver(REPLY_TOPIC, "pump=1,run=off")
    fake.deliver(REPLY_TOPIC, "emg id=5 n=2 sps=500 pga=2048mV")
    assert monitor.latest()["board_reply"].startswith("emg id=5")
    fake.deliver(REPLY_TOPIC, "err emg 캡처 중단: 샘플 없음 (n=0)")
    assert monitor.latest()["board_reply"].startswith("err emg")


def test_request_capture_sets_pga_then_starts(monitor, fake):
    monitor.request_capture(500, 512)
    assert fake.published == [(CMD_TOPIC, "emg:pga:512"), (CMD_TOPIC, "emg:cap:500")]
    assert monitor.latest()["pending"] is True


def test_completed_capture_clears_pending(monitor, fake):
    monitor.request_capture(100, None)
    fake.deliver(EMG_TOPIC, _chunk(1, 0, 1, [(0, 1)]))
    assert monitor.latest()["pending"] is False


def test_request_capture_without_broker_is_conflict():
    m = EmgMonitor(host="h", port=1, client_factory=None)
    with pytest.raises(EmgConflictError):
        m.request_capture(100, None)


def _client_with(tmp_path, fake):
    app = create_app(make_gw_config(tmp_path))
    app.state.emg = EmgMonitor(host="h", port=1, client_factory=lambda: fake)
    return TestClient(app)


def test_api_capture_and_latest(tmp_path, fake):
    with _client_with(tmp_path, fake) as client:
        r = client.post("/api/emg/capture", json={"ms": 300, "pga_mv": 1024})
        assert r.status_code == 200, r.text
        assert fake.published[-1] == (CMD_TOPIC, "emg:cap:300")
        fake.deliver(EMG_TOPIC, _chunk(2, 0, 1, [(0, 100)], pga_mv=1024))
        data = client.get("/api/emg/latest").json()["data"]
        assert data["capture"]["capture_id"] == 2


@pytest.mark.parametrize("body", [{"ms": 5}, {"ms": 3000}, {"ms": 100, "pga_mv": 300}])
def test_api_capture_rejects_bad_input(tmp_path, fake, body):
    with _client_with(tmp_path, fake) as client:
        assert client.post("/api/emg/capture", json=body).status_code == 422
    assert not fake.published


def test_api_capture_is_409_without_wireless_board(tmp_path):
    with TestClient(create_app(make_gw_config(tmp_path))) as client:
        assert client.post("/api/emg/capture", json={"ms": 100}).status_code == 409
        assert client.get("/api/emg/latest").json()["data"]["capture"] is None


def test_capture_view_carries_a_notched_series(monitor, fake):
    samples = [(i * 1800, 1000 + (i % 7)) for i in range(200)]
    fake.deliver(EMG_TOPIC, _chunk(6, 0, 1, samples))
    cap = monitor.latest()["capture"]
    assert len(cap["mv_notch"]) == len(cap["mv"])
    assert set(cap["summary_notch"]) >= {"pp_mv", "min_mv", "max_mv"}
