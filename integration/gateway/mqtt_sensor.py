"""저항센서 MQTT 클라이언트 — 펌프 보드(ESP32-C6)가 ADS1115 를 직접 읽는 경로 (2026-10-22).

그 전에는 Nano 33 BLE 가 센서를 읽어 BLE notify 로 보냈다. 두 보드가 큐브에 같이
올라가므로 ESP32-C6 하나로 합치고, 보드는 **BLE 와 같은 21바이트 패킷**을 10개씩 묶어
`s25007/board1/sensor` 로 발행한다(firmware/clinostat_cube_esp32c6/sensor_ads1115.h).

이 클래스는 `ble_sensor.BleClient` 프로토콜을 구현한다. 그래서 `BleSensorSource` 의
저항 재계산·평균·경과시간·재연결 로직을 수정 없이 그대로 쓴다. 고르는 곳은
config.yaml `ble.transport` (bleak | mqtt) — bleak 로 되돌리면 Nano 경로로 복구된다.

paho 는 자체 스레드에서 콜백을 부르므로 `call_soon_threadsafe` 로 이벤트 루프에 넘긴다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator, Callable, Optional

from gateway.ble_packet import SENSOR_PACKET_SIZE

logger = logging.getLogger("gateway.mqtt_sensor")

SENSOR_TOPIC = "s25007/board1/sensor"

#: 보드는 200 ms 마다 묶음을 낸다. 이만큼 조용하면 끊긴 것으로 보고 스트림을 끝낸다
#: — BleSensorSource 가 재연결을 시도하며 경고를 남긴다(센서 빠짐·보드 전원·WiFi).
DEFAULT_STALE_TIMEOUT_S = 10.0


class MqttSensorClient:
    """`BleClient` 와 같은 모양: connect → stream → disconnect."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        topic: str = SENSOR_TOPIC,
        stale_timeout_s: float = DEFAULT_STALE_TIMEOUT_S,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._topic = topic
        self._stale_timeout_s = stale_timeout_s
        self._client_factory = client_factory
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._connected = asyncio.Event()
        self._disconnected = asyncio.Event()

    async def connect(self, target: Any, timeout: float) -> bool:
        """브로커에 붙어 센서 토픽을 구독한다. CONNACK 이 timeout 안에 없으면 False."""
        del target  # BLE 탐색 대상 — MQTT 에서는 토픽이 대상을 정한다
        await self.disconnect()
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._connected = asyncio.Event()
        self._disconnected = asyncio.Event()
        client = self._make_client()
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        self._client = client
        try:
            client.connect(self._host, self._port)
            client.loop_start()
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            logger.warning("센서 MQTT 브로커 응답 없음 (%s:%d)", self._host, self._port)
            await self.disconnect()
            return False
        except OSError as exc:
            logger.warning("센서 MQTT 접속 실패 (%s:%d): %s", self._host, self._port, exc)
            await self.disconnect()
            return False
        return True

    async def stream(self) -> AsyncIterator[bytes]:
        """21바이트 패킷을 하나씩 낸다. 브로커가 끊기거나 보드가 조용해지면 끝난다."""
        while not self._disconnected.is_set():
            getter = asyncio.ensure_future(self._queue.get())
            waiter = asyncio.ensure_future(self._disconnected.wait())
            done, pending = await asyncio.wait(
                {getter, waiter}, timeout=self._stale_timeout_s,
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if getter not in done:
                if not done:
                    logger.warning("센서 데이터가 %.0f초 동안 없음 — 보드·ADS1115 확인",
                                   self._stale_timeout_s)
                return
            payload = getter.result()
            for offset in range(0, len(payload), SENSOR_PACKET_SIZE):
                yield payload[offset:offset + SENSOR_PACKET_SIZE]

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        with contextlib.suppress(Exception):
            client.loop_stop()
        with contextlib.suppress(Exception):
            client.disconnect()

    # ── paho 스레드 콜백 ────────────────────────────────────────────

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any,
                    reason_code: Any, *_args: Any) -> None:
        if _is_failure(reason_code):
            logger.warning("센서 MQTT 접속 거부: %s", reason_code)
            return
        client.subscribe(self._topic)
        self._threadsafe(self._connected.set)

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        self._threadsafe(self._queue.put_nowait, bytes(message.payload))

    def _on_disconnect(self, *_args: Any) -> None:
        self._threadsafe(self._disconnected.set)

    def _threadsafe(self, fn: Callable[..., Any], *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            if loop is _running_loop():
                fn(*args)
            else:
                loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # 루프가 닫히는 중 — 종료 경로라 버린다
            pass

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            client = self._client_factory()
        else:
            import paho.mqtt.client as mqtt

            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            except (AttributeError, TypeError):  # pragma: no cover — paho v1 호환
                client = mqtt.Client()
        if self._username is not None:
            client.username_pw_set(self._username, self._password)
        return client


def _is_failure(reason_code: Any) -> bool:
    """paho v1 은 int rc, v2 는 ReasonCode(.is_failure). 0 이 성공."""
    flag = getattr(reason_code, "is_failure", None)
    if flag is not None:
        return bool(flag)
    return reason_code not in (0, None)


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
