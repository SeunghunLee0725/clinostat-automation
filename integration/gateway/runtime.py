"""Gateway 런타임 — 센서 소스·모의 펌프·세션 저장소를 하나로 묶는다 (설계 스펙 6장).

FastAPI 핸들러(`gateway/app.py`, `gateway/routes.py`)는 검증·위임·envelope 포장만
하고, 실제 상태 전이는 전부 여기서 일어난다. 소스·펌프·저장소 변경은 단일
`asyncio.Lock`으로 직렬화한다 — 센서 재설정이 진행 중인 세션 기록과 겹치거나,
세션 시작/종료가 센서 tick 루프와 겹치는 것을 막기 위해서다.

⚠ 안전 핵심: `startup()`은 `recover_session()`으로 메타데이터만 복원한다.
센서·펌프 실행은 절대 자동 재개하지 않는다 (`start_sensor`/pump dispense를
호출하지 않는다). 비상정지 래치는 `SimulatedPump` 자신의
`FileEstopLatchPersistence`가 생성 시점에 독립적으로 복원한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from pathlib import Path
from typing import IO, Any, AsyncIterator, Callable, Optional, Union

from gateway.api_models import (
    GatewayComponent,
    GatewayState,
    GatewayStatus,
    PumpCalibrationRequest,
    PumpDispenseRequest,
    PumpEmergencyStopRequest,
    PumpEvent,
    PumpResetEmergencyStopRequest,
    PumpState,
    PumpStatus,
    PumpStopRequest,
    PumpStepRequest,
    SensorConfigureBleLiveRequest,
    SensorConfigureCsvRequest,
    SensorConfigureSerialLiveRequest,
    SensorConfigureSyntheticRequest,
    SensorMode,
    SensorSample,
    SensorState,
    SensorStatus,
    SessionFinishRequest,
    SessionStartRequest,
    SessionState,
    SessionStatus,
    SessionUpdateRequest,
    server_time,
)
from gateway.csv_replay import load_csv_replay_source, load_manifest
from gateway.idempotency import IdempotencyCache
from gateway.recovery import RecoveryResult, recover_session
from gateway.session_store import (
    InsufficientSpaceError,
    SessionConflictError,
    SessionIoError,
    SessionNotActiveError,
    SessionSnapshot,
    SessionStore,
)
from gateway.session_archive import (
    ArchivedSession,
    delete_session,
    list_sessions,
    open_archive,
)
from gateway.runtime_config import GatewayConfig, load_config
from gateway.mqtt_pump import MqttPump
from gateway.mqtt_pump import PumpConflictError as MqttPumpConflictError
from gateway.ble_sensor import BleSensorSource, BleTarget
from gateway.sensor_source import SensorSourceError
from gateway.serial_sensor import SerialSensorSource
from gateway.mqtt_sensor import MqttSensorClient
from gateway.simulated_pump import FileEstopLatchPersistence, SimulatedPump
from gateway.synthetic_sensor import SyntheticSensorSource

logger = logging.getLogger("gateway.runtime")

#: WebSocket/센서 상태 발행 상한 (설계 스펙 6장) — config가 이보다 높은 값을
#: 줘도 여기서 clamp한다.
MAX_PUBLISH_HZ = 10.0

_ESTOP_LATCH_FILENAME = "estop_latch.json"

#: 한 폴링 tick에서 소스로부터 꺼낼 샘플 수 상한. 소스가 폴링보다 빠를 때
#: (BLE 실측 ~30Hz vs 폴링 10Hz) 밀린 것을 따라잡되, 한 tick이 lock을 오래
#: 붙잡지 않도록 묶는다. 10Hz × 64 = 초당 640샘플까지 감당한다.
MAX_SAMPLES_PER_TICK = 64

SensorConfigCommand = Union[
    SensorConfigureCsvRequest,
    SensorConfigureSyntheticRequest,
    SensorConfigureSerialLiveRequest,
    SensorConfigureBleLiveRequest,
]
PumpCommand = Union[
    PumpDispenseRequest, PumpStopRequest, PumpEmergencyStopRequest,
    PumpResetEmergencyStopRequest,
]
SessionCommand = Union[SessionStartRequest, SessionUpdateRequest, SessionFinishRequest]


class SensorConflictError(Exception):
    """센서 상태 전이 오류 — 409에 대응 (미설정 상태 시작, 실행 중 재설정 등)."""

    def __init__(self, message: str, *, code: str = "sensor_conflict") -> None:
        super().__init__(message)
        self.code = code


def ble_client_factory(config: GatewayConfig) -> Optional[Callable[[], MqttSensorClient]]:
    """BLE_LIVE 센서의 수신 클라이언트. bleak 이면 None(BleSensorSource 기본 = bleak)."""
    if config.ble_transport != "mqtt":
        return None
    return lambda: MqttSensorClient(
        host=config.mqtt_host, port=config.mqtt_port,
        username=config.mqtt_username, password=config.mqtt_password,
    )


def _find_latest_session_dir(sessions_root: Path) -> Optional[Path]:
    """가장 최근에 수정된 세션 디렉터리를 찾는다 — 재시작 시 reconciliation 대상."""
    if not sessions_root.exists():
        return None
    candidates = [d for d in sessions_root.iterdir() if d.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


class GatewayRuntime:
    """소스·펌프·저장소를 소유하고 변경을 단일 lock으로 직렬화하는 오케스트레이터."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._started_monotonic = time.monotonic()
        self._lock = asyncio.Lock()
        self._sequence = itertools.count(1)

        self._sensor_state = SensorState.IDLE
        self._sensor_mode: Optional[SensorMode] = None
        self._sensor_source: Optional[Any] = None
        self._sensor_sample: Optional[SensorSample] = None
        #: BLE 모드에서 실제로 적용 중인 설정 (화면 표시용)
        self._sensor_rref_ohm: Optional[float] = None
        self._sensor_avg_factor: Optional[int] = None
        self._sensor_task: Optional[asyncio.Task[None]] = None
        #: 진행 중인 하드웨어 해제. 정지 응답을 막지 않으려고 백그라운드로 돌리고,
        #: 다음 start/configure가 이것을 기다린다.
        self._sensor_release_task: Optional[asyncio.Task[None]] = None

        self._session_cache = IdempotencyCache()
        self._active_store: Optional[SessionStore] = None
        self._current_session_status = SessionStatus()
        self.recovery: Optional[RecoveryResult] = None

        # 펌프마다 백엔드 인스턴스 하나. 번호는 1부터, 순서는 번호 순이다.
        # pump.backend == "wireless"면 실기 무선 펌프(MQTT), 아니면 모의 펌프.
        # 실기 펌프는 하드웨어 estop이 없어 게이트웨이 측 소프트웨어 래치를 쓴다.
        self._pumps: dict[int, Any] = {
            pid: self._make_pump(pid) for pid in range(1, self._config.pump_count + 1)
        }

    def _make_pump(self, pump_id: int) -> Any:
        if self._config.pump_backend == "wireless":
            return MqttPump(
                pump_id=pump_id,
                host=self._config.mqtt_host, port=self._config.mqtt_port,
                username=self._config.mqtt_username, password=self._config.mqtt_password,
                ul_per_step=self._config.pump_ul_per_step,
            )
        # 펌프 1 의 래치 파일명은 단일 펌프 시절 그대로 — 재시작 후 래치 복원이 이어진다.
        suffix = "" if pump_id == 1 else f"_p{pump_id}"
        estop_path = self._config.state_root / _ESTOP_LATCH_FILENAME.replace(
            ".json", f"{suffix}.json")
        return SimulatedPump(
            event_sink=lambda event, pid=pump_id: self._dispatch_pump_event(
                event.model_copy(update={"pump_id": pid})),
            estop_persistence=FileEstopLatchPersistence(estop_path),
        )

    def _pump_for(self, pump_id: int) -> Any:
        """번호로 펌프 백엔드를 고른다. 설정에 없는 번호는 409 (`pump_unknown`)."""
        pump = self._pumps.get(pump_id)
        if pump is None:
            raise MqttPumpConflictError(
                f"pump {pump_id} is not configured (pump.count={len(self._pumps)})",
                code="pump_unknown",
            )
        return pump

    def _pump_targets(self, pump_id: Optional[int]) -> list[tuple[int, Any]]:
        """브로드캐스트 명령의 대상. None 이면 전부."""
        if pump_id is None:
            return list(self._pumps.items())
        return [(pump_id, self._pump_for(pump_id))]

    @staticmethod
    def _stamp(status: PumpStatus, pump_id: int) -> PumpStatus:
        """백엔드가 자기 번호를 모르면(모의 펌프) 런타임이 찍는다."""
        return status if status.pump_id == pump_id else status.model_copy(
            update={"pump_id": pump_id})

    async def _pump_status(self, pump_id: int, pump: Any) -> PumpStatus:
        return self._stamp(await pump.status(), pump_id)

    # ─────────────────────────── 라이프사이클 ───────────────────────────

    async def startup(self) -> None:
        """`recover_session()`으로 메타데이터만 복원한다 — 실행은 재개하지 않는다."""
        for pump in self._pumps.values():
            if isinstance(pump, MqttPump):
                pump.connect()            # 브로커 부재해도 예외를 삼킨다
        sessions_root = self._config.data_root / "sessions"
        latest = _find_latest_session_dir(sessions_root)
        if latest is None:
            return
        try:
            self.recovery = recover_session(latest)
        except OSError:
            logger.exception("session recovery failed for %s", latest)
            return
        try:
            state = SessionState(self.recovery.status)
        except ValueError:
            state = SessionState.FAILED
        self._current_session_status = SessionStatus(
            state=state, session_id=self.recovery.session_id, experiment_name=None,
            data_dir=self._session_data_dir(self.recovery.session_id),
        )

    async def shutdown(self) -> None:
        """백그라운드 센서 tick task를 cancel하고 await한다."""
        task = self._sensor_task
        self._sensor_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for pump in self._pumps.values():
            if isinstance(pump, MqttPump):
                pump.disconnect()

    def uptime_s(self) -> float:
        return time.monotonic() - self._started_monotonic

    def next_sequence(self) -> int:
        """WS 상태 메시지 sequence — 프로세스 수명 동안 단조 증가(연결마다 리셋 아님)."""
        return next(self._sequence)

    # ─────────────────────────── 조회 ───────────────────────────

    async def status(self) -> GatewayStatus:
        async with self._lock:
            return await self._status_locked()

    async def _status_locked(self) -> GatewayStatus:
        pump_statuses = tuple([
            await self._pump_status(pid, pump) for pid, pump in self._pumps.items()
        ])
        gateway_state = (
            GatewayState.DEGRADED
            if any(st.state == PumpState.FAULT for st in pump_statuses)
            else GatewayState.ONLINE
        )
        return GatewayStatus(
            gateway=GatewayComponent(state=gateway_state, last_seen_at=server_time()),
            sensor=self._sensor_status_locked(),
            pump=pump_statuses[0],
            pumps=pump_statuses,
            session=self._current_session_status,
        )

    async def subscribe(self) -> AsyncIterator[GatewayStatus]:
        """최대 `MAX_PUBLISH_HZ`로 상태를 계속 발행한다. 첫 값은 즉시 낸다."""
        interval = 1.0 / min(self._config.sensor_publish_hz, MAX_PUBLISH_HZ)
        while True:
            yield await self.status()
            await asyncio.sleep(interval)

    async def session_status_for_request(
        self, *, session_id: Optional[str], request_id: Optional[str],
    ) -> SessionStatus:
        """reconciliation 조회 — 같은 request_id 재요청이면 그때 응답을 그대로 낸다.

        모르는 세션을 조회하면 타임아웃만 보고 "시작 안 됐다"고 넘겨짚지 않도록
        명시적으로 빈 상태(state=idle, session_id=None)를 낸다 — 현재/최근 세션과
        다른 `session_id`를 물으면서 그 세션의 상태를 받은 것으로 착각하면 안
        된다(스펙 6.6).
        """
        async with self._lock:
            if request_id is not None:
                cached = self._session_cache.get(request_id)
                if cached is not None:
                    return cached
            if session_id is not None and session_id != self._current_session_status.session_id:
                return SessionStatus()
            return self._current_session_status

    # ─────────────────────────── 센서 ───────────────────────────

    def _sensor_status_locked(self) -> SensorStatus:
        ble = self._sensor_mode == SensorMode.BLE_LIVE
        return SensorStatus(
            state=self._sensor_state, mode=self._sensor_mode, sample=self._sensor_sample,
            rref_ohm=self._sensor_rref_ohm if ble else None,
            avg_factor=self._sensor_avg_factor if ble else None,
        )

    def sensor_running(self) -> bool:
        """근육 연속 측정은 같은 ADS1115 를 쓰므로 저항 측정 중엔 켜지 않는다(emg_routes)."""
        return self._sensor_state == SensorState.RUNNING

    def list_datasets(self) -> list[dict[str, Any]]:
        """CSV 재생용 dataset 목록. 경로·파일명은 노출하지 않는다(화면 드롭다운용)."""
        manifest_path = self._config.datasets_dir / "manifest.json"
        if not manifest_path.exists():
            return []
        entries = load_manifest(manifest_path)
        return [
            {"dataset_id": e.dataset_id, "sample_count": e.sample_count,
             "provenance": e.provenance}
            for e in entries.values()
        ]

    async def configure_sensor(self, request: SensorConfigCommand) -> SensorStatus:
        await self._await_sensor_release()   # 이전 해제가 끝난 뒤에 새 소스를 만든다
        async with self._lock:
            if self._sensor_state == SensorState.RUNNING:
                raise SensorConflictError("sensor is running; stop before reconfiguring")

            if isinstance(request, SensorConfigureCsvRequest):
                manifest_path = self._config.datasets_dir / "manifest.json"
                source: Any = load_csv_replay_source(
                    request.dataset_id,
                    datasets_dir=self._config.datasets_dir,
                    manifest_path=manifest_path,
                    replay_speed=request.replay_speed,
                    loop=request.loop,
                )
                mode = SensorMode.CSV_REPLAY
            elif isinstance(request, SensorConfigureSerialLiveRequest):
                source = SerialSensorSource(
                    port=self._config.sensor_serial_port,
                    baudrate=self._config.sensor_serial_baudrate,
                )
                mode = SensorMode.SERIAL_LIVE
            elif isinstance(request, SensorConfigureBleLiveRequest):
                source = BleSensorSource(
                    target=BleTarget(
                        address=request.address or self._config.ble_address,
                        name=request.device_name or self._config.ble_device_name,
                    ),
                    scan_timeout_s=request.scan_timeout_s,
                    client_factory=ble_client_factory(self._config),
                    # 요청에 없으면 config.yaml 값을 쓴다. Rref 가 끝내 None 이면
                    # 장치 설정을 건드리지 않는다(보드의 기존 값 유지).
                    rref_ohm=(request.rref_ohm if request.rref_ohm is not None
                              else self._config.ble_rref_ohm),
                    avg_factor=(request.avg_factor if request.avg_factor is not None
                                else self._config.ble_avg_factor),
                )
                self._sensor_rref_ohm = (request.rref_ohm if request.rref_ohm is not None
                                         else self._config.ble_rref_ohm)
                self._sensor_avg_factor = (request.avg_factor if request.avg_factor is not None
                                           else self._config.ble_avg_factor)
                mode = SensorMode.BLE_LIVE
            else:
                source = SyntheticSensorSource(
                    baseline_resistance_ohm=request.baseline_resistance_ohm,
                    amplitude_ohm=request.amplitude_ohm,
                    period_s=request.period_s,
                    noise_std_ohm=request.noise_std_ohm,
                    seed=request.seed,
                    temperature_c=request.temperature_c,
                    battery_pct=request.battery_pct,
                    reference_resistor_ohm=self._config.reference_resistor_ohm,
                    adc_full_scale=self._config.adc_full_scale,
                )
                mode = SensorMode.SYNTHETIC

            # 이전 소스를 반드시 닫는다. 안 닫으면 시리얼 포트가 잡혀 있거나
            # BLE 연결이 남아 **재설정 후 다시 붙지 못한다.**
            await self._release_sensor_source()

            self._sensor_source = source
            self._sensor_mode = mode
            self._sensor_state = SensorState.IDLE
            self._sensor_sample = None
            # 세션이 먼저 열렸다면 store 는 기본값(CSV_REPLAY)으로 만들어져 있다.
            # 그대로 두면 실측 데이터가 재생 데이터로 기록된다 — 실제 모드를 알린다.
            if self._active_store is not None:
                self._active_store.set_sensor_mode(mode)
            return self._sensor_status_locked()

    async def start_sensor(self, request_id: str) -> SensorStatus:
        del request_id  # 센서 시작은 멱등이 요구되지 않는다 — 재시작 요청은 409.
        await self._await_sensor_release()   # 직전 정지의 하드웨어 해제를 먼저 끝낸다
        async with self._lock:
            if self._sensor_source is None:
                raise SensorConflictError("sensor is not configured")
            if self._sensor_state == SensorState.RUNNING:
                raise SensorConflictError("sensor is already running")
            self._sensor_source.start()
            self._sensor_state = SensorState.RUNNING
            self._sensor_sample = None
            self._sensor_task = asyncio.create_task(self._sensor_poll_loop())
            return self._sensor_status_locked()

    async def stop_sensor(self, request_id: str) -> SensorStatus:
        """멱등 — 이미 멈춰 있으면 에러 없이 현재 상태를 낸다."""
        del request_id
        task: Optional[asyncio.Task[None]] = None
        async with self._lock:
            if self._sensor_state == SensorState.RUNNING:
                self._sensor_state = SensorState.STOPPED
                task = self._sensor_task
                self._sensor_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        async with self._lock:
            # 실기 소스는 하드웨어 자원을 잡고 있다 — 정지 시 반드시 놓아준다.
            # (BLE는 연결이 남으면 다음 start에서 스캔조차 실패한다)
            #
            # ⚠ 다만 **응답을 기다리게 하지 않는다.** BLE 해제는 수 초가 걸리는데
            #   프록시의 변경요청 예산은 1초라, 정지는 실제로 성공했는데 화면에는
            #   504로 보였다(2026-07-29 실기 재현). 해제는 뒤에서 진행하고,
            #   다음 start/configure가 그것을 기다린다.
            self._schedule_sensor_release()
            return self._sensor_status_locked()

    def _schedule_sensor_release(self) -> None:
        """소스 자원 해제를 백그라운드로 돌린다. 이미 돌고 있으면 그대로 둔다."""
        if self._sensor_release_task is not None and not self._sensor_release_task.done():
            return
        self._sensor_release_task = asyncio.create_task(self._release_sensor_source())

    async def _await_sensor_release(self) -> None:
        """진행 중인 해제가 끝나기를 기다린다.

        해제가 끝나기 전에 새로 start/configure 하면 BLE가 아직 붙어 있어
        스캔조차 실패한다. 시작하는 쪽이 값을 치른다 — 정지는 빨라야 한다.
        """
        task, self._sensor_release_task = self._sensor_release_task, None
        if task is None:
            return
        with contextlib.suppress(Exception):
            await task

    async def _release_sensor_source(self) -> None:
        """센서 소스가 잡은 자원(시리얼 포트/BLE 연결)을 놓아준다.

        소스마다 정리 방식이 다르다 — 비동기 `aclose()`가 있으면 그것을,
        없으면 동기 `close()`를. CSV/합성 소스는 둘 다 없어 아무것도 하지 않는다.

        ⚠ 소스 **객체는 남긴다.** 여기서 None으로 지우면 stop 뒤 start가
        "sensor is not configured"로 막힌다. 시리얼·BLE 모두 close 후
        start에서 자원을 다시 잡을 수 있게 되어 있다.
        """
        source = self._sensor_source
        if source is None:
            return
        aclose = getattr(source, "aclose", None)
        close = getattr(source, "close", None)
        try:
            if aclose is not None:
                await aclose()
            elif close is not None:
                close()
        except Exception:  # noqa: BLE001 — 정리 실패가 API 호출을 깨뜨리면 안 된다
            logger.warning("failed to release sensor source", exc_info=True)

    async def _sensor_poll_loop(self) -> None:
        interval = 1.0 / self._config.sensor_publish_hz
        try:
            while True:
                await asyncio.sleep(interval)
                async with self._lock:
                    if self._sensor_source is None:
                        return
                    # 소스가 폴링 주기보다 빠를 수 있다(BLE 실측 ~30Hz vs 폴링 10Hz).
                    # tick()은 호출당 하나만 주므로 하나씩 꺼내면 버퍼가 계속 쌓여
                    # 표시값이 벽시계보다 뒤처지고(실측 0.39배) 결국 오래된 샘플이
                    # 버려진다. 밀린 것을 모두 꺼내 **전부 저장**하고 화면에는 가장
                    # 최근 것을 쓴다. 한 tick의 작업량은 상한으로 묶는다.
                    latest: Optional[SensorSample] = None
                    drained = 0
                    while drained < MAX_SAMPLES_PER_TICK:
                        try:
                            sample = self._sensor_source.tick()
                        except SensorSourceError:
                            # 실기 소스가 자원을 못 잡은 경우(BLE 스캔 실패, 포트 소실 등).
                            # 예전엔 이 예외가 루프를 조용히 죽여 상태만 running으로 남았고
                            # 운영자가 이유를 알 수 없었다 — FAULT로 올리고 로그를 남긴다.
                            self._sensor_state = SensorState.FAULT
                            self._sensor_task = None
                            logger.exception("sensor source failed; marking sensor FAULT")
                            return
                        if sample is None:
                            break
                        drained += 1
                        latest = sample
                        if self._active_store is not None:
                            try:
                                self._active_store.append_sensor(sample)
                            except SessionIoError:
                                logger.exception(
                                    "failed to append sensor sample to session store")
                    if latest is None:
                        continue
                    if drained == MAX_SAMPLES_PER_TICK:
                        logger.warning(
                            "센서 샘플이 폴링보다 빠르다 — 한 tick에서 %d개를 처리했다. "
                            "지속되면 버퍼가 밀린다", drained)
                    self._sensor_sample = latest
        except asyncio.CancelledError:
            raise

    # ─────────────────────────── 펌프 ───────────────────────────

    def _dispatch_pump_event(self, event: PumpEvent) -> None:
        """활성 세션이 있으면 그대로 저장소로 전달한다 — 변환 없이 직접 연결."""
        if self._active_store is not None:
            self._active_store.append_pump_event(event)

    async def pump_command(self, command: PumpCommand) -> Union[PumpStatus, dict[str, Any]]:
        async with self._lock:
            if isinstance(command, PumpDispenseRequest):
                pump = self._pump_for(command.pump_id)
                # 무선 실기 펌프는 µL 보정 상수가 없어 dispense 를 구현하지 않는다.
                # 조건 없이 부르면 AttributeError → 500 이다. 지원하지 않는 것과
                # 고장난 것은 다르게 보여야 하므로 409 로 거절한다.
                # ⚠ 코드는 스텝 쪽(pump_backend_unsupported)과 달라야 한다. 패널은
                # 코드로만 문구를 고르므로, 같은 코드를 쓰면 주입 실패에 "모의 펌프
                # 구성입니다" 라는 사실과 다른 안내가 나간다. 백엔드는 무선이 맞고,
                # 막힌 이유는 µL 보정 상수가 없다는 것이다.
                dispense = getattr(pump, "dispense", None)
                if dispense is None:
                    raise MqttPumpConflictError(
                        "volume dispense needs µL calibration; use step commands",
                        code="pump_volume_uncalibrated",
                    )
                return self._stamp(await dispense(command), command.pump_id)
            if isinstance(command, PumpStopRequest):
                return await self._broadcast_locked(
                    command.pump_id, lambda pump: pump.stop(command))
            if isinstance(command, PumpEmergencyStopRequest):
                return await self._broadcast_locked(
                    command.pump_id, lambda pump: pump.emergency_stop(command))
            if isinstance(command, PumpResetEmergencyStopRequest):
                return await self._broadcast_locked(
                    command.pump_id,
                    lambda pump: self._reset_emergency_stop_one(pump, command))
            raise TypeError(f"unsupported pump command: {type(command)!r}")

    async def _broadcast_locked(
        self, pump_id: Optional[int], action: Any,
    ) -> Union[PumpStatus, dict[str, Any]]:
        """정지·비상정지·해제를 대상 펌프 전부에 차례로 적용한다.

        대상이 하나면 그 펌프의 결과를 그대로 낸다(단일 펌프 시절 응답 형태).
        전체면 펌프 1 의 결과에 `pumps`(전부의 상태)를 덧붙인다 — 기존 호출부는
        `state` 만 보고, 새 화면은 `pumps` 로 각 카드를 갱신한다.
        비상정지는 어느 하나가 실패해도 나머지를 계속 멈춘다.
        """
        targets = self._pump_targets(pump_id)
        results: list[Union[PumpStatus, dict[str, Any]]] = []
        first_error: Optional[BaseException] = None
        for pid, pump in targets:
            try:
                result = await action(pump)
            except Exception as exc:  # noqa: BLE001 — 한 펌프의 실패가 다른 펌프 정지를 막으면 안 된다
                if first_error is None:
                    first_error = exc
                continue
            results.append(self._stamp_result(result, pid))
        if first_error is not None:
            raise first_error
        if len(targets) == 1:
            return results[0]
        first = results[0]
        first_dict = first if isinstance(first, dict) else first.model_dump(mode="json")
        return {
            **first_dict,
            "pumps": [
                r if isinstance(r, dict) else r.model_dump(mode="json") for r in results
            ],
        }

    def _stamp_result(
        self, result: Union[PumpStatus, dict[str, Any]], pump_id: int,
    ) -> Union[PumpStatus, dict[str, Any]]:
        if isinstance(result, dict):
            return {**result, "pump_id": pump_id}
        return self._stamp(result, pump_id)

    async def pump_step(self, request: PumpStepRequest) -> PumpStatus:
        """실기 무선 펌프의 스텝 명령. 모의 펌프에는 스텝 개념이 없어 409.

        ⚠ 펌프 오류를 SensorConflictError 로 감싸지 마라. 코드가 `sensor_conflict`
        가 되어 패널이 '측정 정지를 먼저 누르세요' 로 안내하는데, 센서를 안 쓰는
        구성에서는 실행할 수 없는 지시이고 진짜 해법('비상정지 해제')은 드러나지
        않는다. MqttPumpConflictError 는 app.py 에 핸들러가 등록돼 있으므로 그대로
        올려보내면 `pump_estop_latched` 같은 정확한 코드가 나간다.
        """
        async with self._lock:
            pump = self._pump_for(request.pump_id)
            step = getattr(pump, "step", None)
            if step is None:
                raise MqttPumpConflictError(
                    "step commands require the wireless pump backend",
                    code="pump_backend_unsupported",
                )
            return self._stamp(await step(request), request.pump_id)

    async def set_pump_calibration(
        self, request: PumpCalibrationRequest,
    ) -> PumpStatus:
        """스텝당 토출량을 넣는다 — 이 값이 있어야 부피 주입을 환산할 수 있다.

        연동식 펌프는 튜브 규격마다 값이 달라 펌웨어 상수로 둘 수 없다. 실측으로
        구해 운영자가 화면에서 넣는다.
        """
        async with self._lock:
            pump = self._pump_for(request.pump_id)
            setter = getattr(pump, "set_calibration", None)
            if setter is None:
                raise MqttPumpConflictError(
                    "this pump backend has no step-based calibration",
                    code="pump_backend_unsupported",
                )
            setter(request.ul_per_step)
            return await self._pump_status(request.pump_id, pump)

    async def _reset_emergency_stop_one(
        self, pump: Any, command: PumpResetEmergencyStopRequest,
    ) -> dict[str, Any]:
        """PumpStatus 전체 + `previous_state`/`accepted`(스펙 6.5)를 합쳐 낸다."""
        result = await pump.reset_emergency_stop(command)
        status = await pump.status()
        return {
            **status.model_dump(mode="json"),
            "previous_state": result.previous_state.value,
            "accepted": result.accepted,
        }

    # ─────────────────────────── 기록 관리 ───────────────────────────

    def _sessions_root(self) -> Path:
        return self._config.data_root / "sessions"

    def _active_session_id(self) -> Optional[str]:
        """기록 중인 세션. 삭제를 막는 근거이므로 store를 직접 본다."""
        if self._active_store is None:
            return None
        return self._active_store.snapshot().session_id

    def list_archived_sessions(self) -> list[ArchivedSession]:
        return list_sessions(self._sessions_root(), active_session_id=self._active_session_id())

    def open_session_archive(self, session_id: str) -> tuple[IO[bytes], int]:
        """호출자가 스트림을 닫아야 한다."""
        return open_archive(self._sessions_root(), session_id)

    def delete_archived_session(self, session_id: str) -> None:
        delete_session(
            self._sessions_root(), session_id, active_session_id=self._active_session_id(),
        )

    # ─────────────────────────── 세션 ───────────────────────────

    def _session_data_dir(self, session_id: Optional[str]) -> Optional[str]:
        """세션 데이터가 쌓이는 실제 경로. session_store의 배치와 같아야 한다."""
        if not session_id:
            return None
        return str(self._config.data_root / "sessions" / session_id)

    def _status_from_snapshot(self, snapshot: SessionSnapshot) -> SessionStatus:
        return SessionStatus(
            state=SessionState(snapshot.status), session_id=snapshot.session_id,
            experiment_name=snapshot.experiment_name,
            data_dir=self._session_data_dir(snapshot.session_id),
        )

    async def session_command(self, command: SessionCommand) -> SessionStatus:
        async with self._lock:
            cached = self._session_cache.get(command.request_id)
            if cached is not None:
                return cached

            if isinstance(command, SessionStartRequest):
                status = await self._session_start_locked(command)
            elif isinstance(command, SessionUpdateRequest):
                status = self._session_update_locked(command)
            elif isinstance(command, SessionFinishRequest):
                status = await self._session_finish_locked(command)
            else:
                raise TypeError(f"unsupported session command: {type(command)!r}")

            self._session_cache.set(command.request_id, status)
            self._current_session_status = status
            return status

    async def _session_start_locked(self, command: SessionStartRequest) -> SessionStatus:
        if self._active_store is not None:
            raise SessionConflictError(
                f"a session is already active: {self._active_store.snapshot().session_id}"
            )
        mode = self._sensor_mode or SensorMode.CSV_REPLAY
        store = SessionStore(
            self._config.data_root, sensor_mode=mode, min_free_bytes=self._config.min_free_bytes,
        )
        snapshot = store.start(command)
        self._active_store = store
        for pump in self._pumps.values():
            await pump.begin_session()
        return self._status_from_snapshot(snapshot)

    def _session_update_locked(self, command: SessionUpdateRequest) -> SessionStatus:
        if self._active_store is None:
            raise SessionNotActiveError("no session is currently active")
        snapshot = self._active_store.update_run_id(command)
        return self._status_from_snapshot(snapshot)

    async def _session_finish_locked(self, command: SessionFinishRequest) -> SessionStatus:
        """정지·종료는 멱등이어야 한다 — 같은 request_id 재시도는 위 캐시가 처리한다."""
        if self._active_store is None:
            raise SessionNotActiveError("no session is currently active")
        snapshot = self._active_store.finish(command)
        self._active_store = None
        for pump in self._pumps.values():
            await pump.end_session()
        return self._status_from_snapshot(snapshot)


__all__ = [
    "GatewayConfig",
    "GatewayRuntime",
    "SensorConflictError",
    "ble_client_factory",
    "load_config",
]
