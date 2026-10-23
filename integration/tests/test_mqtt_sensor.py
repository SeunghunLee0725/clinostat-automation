"""저항센서 MQTT 경로 (2026-10-22, 펌프 보드 ESP32-C6 가 ADS1115 를 직접 읽음).

BLE 경로의 BleSensorSource 를 그대로 쓰고 클라이언트만 MQTT 로 바꾼다 — 브로커 없이
가짜 paho 클라이언트로 검증한다.
"""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import pytest

from gateway.ble_packet import SENSOR_PACKET_FORMAT
from gateway.ble_sensor import BleSensorSource, BleTarget
from gateway.mqtt_sensor import SENSOR_TOPIC, MqttSensorClient


def _packet(ts: int, raw: int = 1000, r: float = 50_000.0) -> bytes:
    return struct.pack(SENSOR_PACKET_FORMAT, ts, raw, r, 0.0, 25.0, 0)


class FakePaho:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.subscribed: list[str] = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.stopped = False
        self.credentials = None

    def username_pw_set(self, user, password):
        self.credentials = (user, password)

    def connect(self, host, port, *a, **k):
        self.host, self.port = host, port

    def loop_start(self):
        if self.accept:
            self.on_connect(self, None, {}, 0)

    def loop_stop(self):
        self.stopped = True

    def disconnect(self): ...

    def subscribe(self, topic, *a, **k):
        self.subscribed.append(topic)

    def deliver(self, payload: bytes) -> None:
        self.on_message(self, None, SimpleNamespace(topic=SENSOR_TOPIC, payload=payload))

    def drop(self) -> None:
        self.on_disconnect(self, None, {}, 7)


def _client(fake: FakePaho, **kw) -> MqttSensorClient:
    return MqttSensorClient(host="127.0.0.1", port=1883, client_factory=lambda: fake, **kw)


@pytest.mark.asyncio
async def test_connect_subscribes_to_sensor_topic():
    fake = FakePaho()
    assert await _client(fake).connect(BleTarget(), timeout=1.0)
    assert fake.subscribed == [SENSOR_TOPIC]


@pytest.mark.asyncio
async def test_connect_returns_false_without_connack():
    fake = FakePaho(accept=False)
    assert not await _client(fake).connect(BleTarget(), timeout=0.05)


@pytest.mark.asyncio
async def test_batched_payload_is_split_into_packets():
    fake = FakePaho()
    client = _client(fake)
    await client.connect(BleTarget(), timeout=1.0)
    fake.deliver(_packet(1) + _packet(2) + _packet(3))
    got = []
    async for chunk in client.stream():
        got.append(chunk)
        if len(got) == 3:
            break
    assert [struct.unpack(SENSOR_PACKET_FORMAT, c)[0] for c in got] == [1, 2, 3]


@pytest.mark.asyncio
async def test_stream_ends_on_broker_disconnect():
    fake = FakePaho()
    client = _client(fake)
    await client.connect(BleTarget(), timeout=1.0)
    fake.drop()
    got = [c async for c in client.stream()]
    assert got == []


@pytest.mark.asyncio
async def test_stream_ends_when_board_goes_silent():
    """보드가 멈추면(센서 빠짐·전원) 스트림을 끝내 재연결·경고 경로로 넘긴다."""
    fake = FakePaho()
    client = _client(fake, stale_timeout_s=0.05)
    await client.connect(BleTarget(), timeout=1.0)
    got = [c async for c in client.stream()]
    assert got == []


@pytest.mark.asyncio
async def test_disconnect_stops_network_loop():
    fake = FakePaho()
    client = _client(fake)
    await client.connect(BleTarget(), timeout=1.0)
    await client.disconnect()
    assert fake.stopped


@pytest.mark.asyncio
async def test_credentials_are_passed_to_paho():
    fake = FakePaho()
    client = MqttSensorClient(host="h", port=1, username="u", password="p",
                              client_factory=lambda: fake)
    await client.connect(BleTarget(), timeout=1.0)
    assert fake.credentials == ("u", "p")


@pytest.mark.asyncio
async def test_ble_source_runs_unchanged_over_mqtt_client():
    """저항 재계산·경과시간 등 BLE 소스 로직이 MQTT 클라이언트에서도 그대로 돈다."""
    fake = FakePaho()
    src = BleSensorSource(client_factory=lambda: _client(fake), rref_ohm=81_000.0)
    src.start()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if fake.subscribed:
            break
    fake.deliver(_packet(5_000, raw=0) + _packet(5_020, raw=0))
    samples = []
    for _ in range(50):
        await asyncio.sleep(0.01)
        sample = src.tick()
        if sample is not None:
            samples.append(sample)
        if len(samples) == 2:
            break
    await src.aclose()
    assert [s.session_elapsed_ms for s in samples] == [0, 20]
    # raw 0 → Vmid = Vref(1.579) → R = 81k·(3.2−1.579)/1.579
    assert samples[0].resistance_ohm == pytest.approx(81_000 * (3.2 - 1.579) / 1.579, rel=1e-6)


# ── 설정: ble.transport ─────────────────────────────────────────────

from pathlib import Path  # noqa: E402

from gateway.runtime import ble_client_factory, load_config  # noqa: E402
from tests.conftest import make_gw_config  # noqa: E402


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_transport_defaults_to_bleak(tmp_path: Path):
    assert load_config(_write(tmp_path, "ble: {}\n")).ble_transport == "bleak"


def test_transport_mqtt_is_read(tmp_path: Path):
    assert load_config(_write(tmp_path, "ble:\n  transport: mqtt\n")).ble_transport == "mqtt"


def test_unknown_transport_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="ble.transport"):
        load_config(_write(tmp_path, "ble:\n  transport: wifi\n"))


def test_bleak_transport_keeps_default_ble_client(tmp_path: Path):
    assert ble_client_factory(make_gw_config(tmp_path)) is None


def test_mqtt_transport_builds_client_with_broker_settings(tmp_path: Path):
    cfg = make_gw_config(tmp_path, ble_transport="mqtt", mqtt_host="10.0.0.5",
                         mqtt_port=1884, mqtt_username="u", mqtt_password="p")
    factory = ble_client_factory(cfg)
    client = factory()
    assert isinstance(client, MqttSensorClient)
    assert (client._host, client._port, client._username, client._password) == (
        "10.0.0.5", 1884, "u", "p")
