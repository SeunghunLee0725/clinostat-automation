"""근육 응답 전압 연속 측정 (2026-10-30).

보드는 `emg:stream:on` 을 받으면 저항센서를 멈추고 64샘플 조각을 끝없이
`s25007/board1/emg/stream` 으로 보낸다(조각 수 칸 = 0, 조각 번호 = 일련번호).
보드는 20 s 동안 keepalive 가 없으면 스스로 멈춘다 — 화면이 닫히면 저항센서가 돌아오게.
게이트웨이는 최근 구간만 들고 있다가 화면이 물을 때 keepalive 를 대신 보낸다.
"""

from __future__ import annotations

import struct

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.emg_monitor import CMD_TOPIC, EmgMonitor
from gateway.emg_packet import (
    EMG_HEADER_FORMAT,
    EMG_SAMPLE_FORMAT,
    EMG_STREAM_TOPIC,
    EmgPacketError,
    parse_emg_chunk,
    parse_emg_stream_chunk,
)
from gateway.emg_stream import EmgStreamWindow
from tests.conftest import make_gw_config
from tests.test_emg_monitor import FakePaho


def _stream_chunk(stream_id, seq, samples, pga_mv=2048):
    head = struct.pack(EMG_HEADER_FORMAT, stream_id, seq, 0, len(samples), pga_mv)
    return head + b"".join(struct.pack(EMG_SAMPLE_FORMAT, t, r) for t, r in samples)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# ─────────────── 패킷 ───────────────

def test_stream_chunk_parses_with_zero_count():
    c = parse_emg_stream_chunk(_stream_chunk(3, 7, [(1000, 5)]))
    assert (c.capture_id, c.index, c.count) == (3, 7, 0)


def test_stream_and_capture_parsers_reject_each_other():
    with pytest.raises(EmgPacketError):
        parse_emg_chunk(_stream_chunk(3, 7, [(0, 5)]))
    capture = struct.pack(EMG_HEADER_FORMAT, 1, 0, 1, 0, 2048)
    with pytest.raises(EmgPacketError):
        parse_emg_stream_chunk(capture)


# ─────────────── 최근 구간 ───────────────

def _feed(win, stream_id, seq, samples, pga_mv=2048):
    win.feed(parse_emg_stream_chunk(_stream_chunk(stream_id, seq, samples, pga_mv)))


def test_window_keeps_only_recent_stream_time():
    win = EmgStreamWindow(keep_s=1.0, clock=Clock())
    _feed(win, 1, 0, [(0, 0), (500_000, 0)])
    _feed(win, 1, 1, [(1_600_000, 0)])
    snap = win.snapshot(window_s=10.0)
    assert snap["t_ms"] == [1600.0]


def test_snapshot_window_and_summary():
    win = EmgStreamWindow(keep_s=10.0, clock=Clock())
    _feed(win, 1, 0, [(0, 32767), (1_000_000, 16384), (2_000_000, -16384)])
    snap = win.snapshot(window_s=1.5)
    assert snap["t_ms"] == [1000.0, 2000.0]
    assert snap["summary"]["pp_mv"] == pytest.approx(2048.0)
    assert snap["summary"]["saturated"] == 0


def test_new_stream_id_resets_the_window():
    win = EmgStreamWindow(keep_s=10.0, clock=Clock())
    _feed(win, 1, 0, [(5_000_000, 1)])
    _feed(win, 2, 0, [(0, 2)])
    snap = win.snapshot(window_s=10.0)
    assert snap["stream_id"] == 2 and snap["t_ms"] == [0.0]


def test_lost_chunks_are_counted_across_wrap():
    win = EmgStreamWindow(keep_s=10.0, clock=Clock())
    _feed(win, 1, 65534, [(0, 1)])
    _feed(win, 1, 1, [(1000, 1)])        # 65535, 0 빠짐
    assert win.snapshot(window_s=10.0)["lost_chunks"] == 2


def test_receiving_turns_off_when_chunks_stop():
    clock = Clock()
    win = EmgStreamWindow(keep_s=10.0, clock=clock)
    _feed(win, 1, 0, [(0, 1)])
    assert win.snapshot(window_s=1.0)["receiving"] is True
    clock.now += 3.0
    assert win.snapshot(window_s=1.0)["receiving"] is False


# ─────────────── 수신기: 켜기·끄기·keepalive ───────────────

@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake():
    return FakePaho()


@pytest.fixture
def monitor(fake, clock):
    m = EmgMonitor(host="h", port=1, client_factory=lambda: fake, clock=clock)
    m.connect()
    return m


def test_subscribes_to_stream_topic(monitor, fake):
    assert EMG_STREAM_TOPIC in fake.subscribed


def test_stream_on_sets_pga_then_starts(monitor, fake):
    monitor.set_stream(True, 512)
    assert fake.published == [(CMD_TOPIC, "emg:pga:512"), (CMD_TOPIC, "emg:stream:on")]
    assert monitor.stream_snapshot()["requested"] is True


def test_stream_off(monitor, fake):
    monitor.set_stream(True, None)
    monitor.set_stream(False, None)
    assert fake.published[-1] == (CMD_TOPIC, "emg:stream:off")
    assert monitor.stream_snapshot()["requested"] is False


def test_polling_sends_keepalive_every_few_seconds(monitor, fake, clock):
    monitor.set_stream(True, None)
    clock.now += 2.0
    monitor.stream_snapshot()
    assert fake.published.count((CMD_TOPIC, "emg:stream:on")) == 1
    clock.now += 4.0
    monitor.stream_snapshot()
    assert fake.published.count((CMD_TOPIC, "emg:stream:on")) == 2


def test_abandoned_stream_is_not_revived_by_a_late_poll(monitor, fake, clock):
    """화면을 닫아 보드가 스스로 멈춘 뒤, 나중에 연 화면이 연속 측정을 되살리면 안 된다."""
    monitor.set_stream(True, None)
    clock.now += 60.0
    snap = monitor.stream_snapshot()
    assert snap["requested"] is False
    assert fake.published.count((CMD_TOPIC, "emg:stream:on")) == 1


def test_stream_chunks_reach_the_snapshot(monitor, fake):
    monitor.set_stream(True, None)
    fake.deliver(EMG_STREAM_TOPIC, _stream_chunk(4, 0, [(0, 100), (1800, 200)]))
    snap = monitor.stream_snapshot()
    assert snap["stream_id"] == 4 and len(snap["mv"]) == 2


# ─────────────── REST ───────────────

def _client(tmp_path, fake, clock):
    app = create_app(make_gw_config(tmp_path))
    app.state.emg = EmgMonitor(host="h", port=1, client_factory=lambda: fake, clock=clock)
    return app, TestClient(app)


def test_api_stream_on_and_read(tmp_path, fake, clock):
    _app, client = _client(tmp_path, fake, clock)
    with client:
        r = client.post("/api/emg/stream", json={"on": True, "pga_mv": 1024})
        assert r.status_code == 200, r.text
        fake.deliver(EMG_STREAM_TOPIC, _stream_chunk(1, 0, [(0, 10)], pga_mv=1024))
        data = client.get("/api/emg/stream").json()["data"]
        assert data["requested"] is True and data["mv"]


def test_api_stream_runs_alongside_resistance_measurement(tmp_path, fake, clock):
    """근육은 전용 칩(0x49)으로 옮겼다 — 저항 측정 중에도 연속 측정을 켤 수 있다."""
    app, client = _client(tmp_path, fake, clock)
    with client:
        assert not hasattr(app.state.runtime, "sensor_running")
        assert client.post("/api/emg/stream", json={"on": True}).status_code == 200


def test_board_clock_wrap_restarts_the_window():
    """보드 µs 시계(uint32)는 약 71분에 0 으로 돌아온다 — 뒤섞이지 않게 새로 시작한다."""
    win = EmgStreamWindow(keep_s=10.0, clock=Clock())
    _feed(win, 1, 0, [(4_294_000_000, 1)])
    _feed(win, 1, 1, [(500, 2)])
    assert win.snapshot(window_s=10.0)["t_ms"] == [0.5]


def test_stream_snapshot_carries_a_notched_series():
    win = EmgStreamWindow(keep_s=10.0, clock=Clock())
    for seq in range(4):
        _feed(win, 1, seq, [((seq * 64 + i) * 1800, 100 + i) for i in range(64)])
    snap = win.snapshot(window_s=10.0)
    assert len(snap["mv_notch"]) == len(snap["mv"])
    assert "pp_mv" in snap["summary_notch"]
