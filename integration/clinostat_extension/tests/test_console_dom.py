"""통합 관제 콘솔(work/static/console.html) 계약.

/console 은 기존 / 화면과 **별개 파일**이다. 기존 화면의 baseline 보존 계약은 그대로 두고,
여기서는 새 콘솔이 (1) 같은 서버 API 만 쓰고, (2) 음성 명령을 확인 없이 실행하지 않으며,
(3) 클리노스텟 기동에 기존 화면과 같은 안전 확인 토큰을 쓰는지 못박는다.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "work" / "static" / "console.html"
INDEX = ROOT / "work" / "static" / "index.html"

_ID_RE = re.compile(r'\bid="([^"]+)"')


@pytest.fixture(scope="module")
def page() -> str:
    return CONSOLE.read_text(encoding="utf-8")


def _ids(html: str) -> Counter:
    return Counter(_ID_RE.findall(html))


def _block(page: str, start: str, end: str = "\nfunction ") -> str:
    body = page[page.index(start):]
    return body[:body.index(end, 1)] if end in body[1:] else body


# ─────────────────────────── DOM ───────────────────────────

REQUIRED_IDS = [
    # 상단
    "csClock", "csChipPi", "csChipGateway", "csChipBoard", "csChipBle", "csChipEstim", "csChipLlm",
    "csStopAll",
    # 개요 띠
    "csOvStrategy", "csOvStrategyState", "csOvSensor", "csOvSensorState", "csOvPumps", "csOvPumpsState",
    "csOvVoice", "csOvVoiceState",
    # 클리노스텟
    "csClinoElapsed", "csClinoOuter", "csClinoInner", "csClinoTasmg", "csClinoStrategy",
    "csClinoParams", "csStrategySelect", "csSupervisionAck", "csClinoStart", "csClinoStop",
    # 저항센서
    "csSensorDelta", "csSensorOhm", "csSensorTemp", "csSensorAdc", "csSensorBattery",
    "csSensorState", "csSensorRref", "csSensorPath", "csSensorFill", "csExperimentName",
    "csSensorStart", "csSensorStop",
    # 펌프 · 비상정지
    "csPumpCards", "csPumpCardTemplate", "csPumpEstop", "csEstopAck", "csPumpEstopReset",
    "csEstopSummary",
    # 전기신호(빈 카드)
    "csEstimCard", "csEstimChannel", "csEstimWave", "csEstimAmp", "csEstimHz", "csEstimApply",
    "csEstimStop",
    # 음성 비서
    "csMicBtn", "csMicState", "csMicHint", "csVoiceBars", "csTranscript", "csConfirmBar",
    "csConfirmRun", "csConfirmCancel", "csSummaryBtn", "csTtsToggle", "csTextInput", "csTextSend",
    # 세션 · 로그
    "csEventLog", "csSessionId", "csSessionState", "csSessionFinish", "csSessionsLink",
    "csGatewayError",
]


def test_console_file_exists():
    assert CONSOLE.exists()


@pytest.mark.parametrize("element_id", REQUIRED_IDS)
def test_required_id_exists_exactly_once(page, element_id):
    assert _ids(page)[element_id] == 1, f"{element_id} 가 없거나 중복이다"


def test_no_duplicate_ids_at_all(page):
    dupes = [i for i, n in _ids(page).items() if n > 1]
    assert not dupes, f"중복 id: {dupes}"


PUMP_CARD_FIELDS = ["pumpTitle", "pumpState", "pumpMode", "pumpPosition", "pumpLast",
                    "pumpRunHz", "pumpRunSec", "pumpRunBtn", "pumpStop", "pumpLatched"]


def _pump_template(page: str) -> str:
    start = page.index('<template id="csPumpCardTemplate">') + len('<template id="csPumpCardTemplate">')
    return page[start:page.index("</template>", start)]


@pytest.mark.parametrize("field", PUMP_CARD_FIELDS)
def test_pump_card_template_has_each_field_once(page, field):
    assert _pump_template(page).count(f'data-f="{field}"') == 1


def test_pump_card_template_has_no_ids(page):
    assert 'id="' not in _pump_template(page)


def test_pump_hz_input_defaults_and_bounds(page):
    tpl = _pump_template(page)
    hz = tpl[tpl.index('data-f="pumpRunHz"'):][:160]
    assert 'value="600"' in hz and 'min="1"' in hz and 'max="1000"' in hz
    assert "const PUMP_RUN_HZ_MAX = 1000" in page


def test_estim_card_is_a_disabled_placeholder(page):
    card = page[page.index('id="csEstimCard"'):]
    card = card[:card.index("</section>")]
    for field_id in ("csEstimChannel", "csEstimWave", "csEstimAmp", "csEstimHz", "csEstimApply", "csEstimStop"):
        tag = card[card.index(f'id="{field_id}"'):][:200]
        assert "disabled" in tag, f"{field_id} 는 장치 연결 전까지 비활성이어야 한다"
    assert "/api/spacebio/estim" not in page      # 아직 없는 API 를 부르지 않는다


# ─────────────────────────── API 경로 ───────────────────────────

@pytest.mark.parametrize("path", [
    "/api/assistant/command", "/api/assistant/summary", "/api/assistant/status",
    "/api/control/start", "/api/control/stop", "/api/strategies", "/api/strategy",
    "/api/spacebio/status", "/api/spacebio/pump/step", "/api/spacebio/pump/stop",
    "/api/spacebio/pump/emergency-stop", "/api/spacebio/pump/reset-emergency-stop",
    "/api/spacebio/sensor/configure", "/api/spacebio/sensor/start", "/api/spacebio/sensor/stop",
    "/api/spacebio/session/start", "/api/spacebio/session/finish",
])
def test_console_calls_only_server_routes(page, path):
    assert path in page


def test_console_never_talks_to_gateway_or_llm_directly(page):
    assert "8010" not in page and "8081" not in page
    assert "127.0.0.1" not in page and "100.90." not in page and "100.85." not in page


def test_console_uses_both_websockets(page):
    assert "'/ws'" in page or '"/ws"' in page or "/ws`" in page
    assert "/ws/spacebio" in page


# ─────────────────────────── 음성 · 확인 절차 ───────────────────────────

def test_speech_recognition_is_korean_and_tts_is_optional(page):
    assert "webkitSpeechRecognition" in page
    assert "'ko-KR'" in page
    assert "speechSynthesis" in page
    assert "csTtsToggle" in _block(page, "function speak(")


def test_voice_actions_wait_for_confirmation_when_the_server_says_so(page):
    handle = _block(page, "async function handleAssistantReply(")
    assert "requires_confirm" in handle
    assert "csConfirmBar" in handle or "showConfirm(" in handle
    assert "executeAction(" in handle


@pytest.mark.parametrize("kind", [
    "pump_run", "pump_stop", "clinostat_stop", "stop_all", "estop",
    "sensor_start", "sensor_stop", "clinostat_start", "status",
])
def test_every_rule_action_kind_has_an_executor(page, kind):
    assert f"'{kind}'" in _block(page, "async function executeAction(")


def test_pump_run_executor_uses_step_endpoint_like_the_card(page):
    run = _block(page, "async function runPumpHz(")
    assert "/api/spacebio/pump/step" in run
    assert "hz * sec" in run and "hz * 60" in run and "PUMP_RUN_HZ_MAX" in run


def test_emergency_stop_targets_every_pump(page):
    estop = _block(page, "async function pumpEmergencyStop(")
    assert "/api/spacebio/pump/emergency-stop" in estop and "pump_id" not in estop


def test_clinostat_start_reuses_the_existing_supervision_tokens(page):
    index = INDEX.read_text(encoding="utf-8")
    for token in ("HUMAN_SUPERVISED_LIVE_MOTION", "HUMAN_CONFIRMED_SAMPLE_PLATFORM"):
        assert token in index and token in page
    start = _block(page, "async function clinostatStart(")
    assert "csSupervisionAck" in start and "supervision_token" in start
    assert "/api/control/start" in start


def test_stop_all_stops_clinostat_first_then_pumps(page):
    stop = _block(page, "async function stopAll(")
    assert stop.index("/api/control/stop") < stop.index("/api/spacebio/pump/stop")


def test_llm_reply_is_never_executed(page):
    """서버 응답의 reply(글자)는 말풍선·TTS 로만 간다. 실행은 action 객체로만."""
    handle = _block(page, "async function handleAssistantReply(")
    assert "executeAction(reply" not in handle
    assert "executeAction(data.action" in handle or "executeAction(action" in handle
