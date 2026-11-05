"""기존 대시보드(/)에 덧붙인 음성 비서 위젯 (2026-11-05).

위젯은 `static/voice_widget.js` 한 파일이고, index.html 은 그 스크립트와 실행기 묶음을 **덧붙이기만** 한다
(베이스라인 가드는 test_existing_contract 가 본다). 여기서는 (1) 서버 경로가 콘솔과 같고, (2) 비서의 글이
실행되지 않으며, (3) 모든 action 종류에 이 화면의 기존 함수로 만든 실행기가 있는지를 본다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "work" / "static" / "index.html"
WIDGET = ROOT / "work" / "static" / "voice_widget.js"
CONSOLE = ROOT / "work" / "static" / "console.html"

ACTION_KINDS = ["pump_run", "pump_stop", "clinostat_stop", "clinostat_start", "stop_all", "estop",
                "sensor_start", "sensor_stop", "experiment_run", "experiment_stop", "strategy_set"]


@pytest.fixture(scope="module")
def index() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def widget() -> str:
    return WIDGET.read_text(encoding="utf-8")


def _block(text: str, start: str, end: str = "\n  }") -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _executors(index: str) -> str:
    i = index.index("const executors = {")
    return index[i:index.index("\n  };", i)]


# ─────────────────────────── 위젯 ───────────────────────────

def test_widget_is_korean_speech_with_optional_tts(widget):
    assert "webkitSpeechRecognition" in widget and "'ko-KR'" in widget
    assert "speechSynthesis" in widget
    assert "vaTts" in _block(widget, "function speak(")


def test_widget_uses_the_same_server_routes_as_the_console(widget):
    send = _block(widget, "async function sendCommand(")
    assert "/api/assistant/chat" in send and "session_id" in send
    assert "/api/assistant/command" in send
    assert "/api/assistant/status" in _block(widget, "async function probe(")
    assert "/api/experiment/status" in _block(widget, "async function pollExperiment(")
    assert "8010" not in widget and "8081" not in widget and "100.85." not in widget


def test_widget_waits_for_confirmation_and_never_executes_the_reply(widget):
    handle = _block(widget, "async function handleReply(")
    assert "requires_confirm" in handle and "showConfirm(action)" in handle
    assert "executeAction(action)" in handle and "executeAction(data.reply" not in handle
    execute = _block(widget, "async function executeAction(")
    assert "state.executors[action.kind]" in execute      # 페이지가 준 실행기로만
    assert "fetch(" not in execute                          # 위젯 자체는 장치 API 를 부르지 않는다
    assert "reportResult(" in execute


def test_widget_reports_results_and_auto_reports_are_opt_in(widget):
    assert "[실행 결과]" in _block(widget, "async function reportResult(")
    assert 'id="vaAutoReport"' in widget
    assert "vaAutoReport" in _block(widget, "function autoReportDue(")


def test_widget_lists_every_action_kind(widget):
    kinds = re.search(r"const KINDS = \[(.*?)\];", widget, re.S).group(1)
    for kind in ACTION_KINDS + ["status"]:
        assert f"'{kind}'" in kinds


# ─────────────────────────── index.html 덧붙임 ───────────────────────────

def test_index_loads_the_widget_after_its_own_scripts(index):
    assert '<script src="/static/voice_widget.js?v=' in index
    assert index.index("voice_widget.js") > index.index("uiWatchImuConnection();")
    assert "VoiceAssistant.mount(" in index


@pytest.mark.parametrize("kind", ACTION_KINDS)
def test_index_has_an_executor_for_every_action_kind(index, kind):
    assert f"{kind}: async" in _executors(index)


def test_index_executors_reuse_the_existing_page_functions(index):
    ex = _executors(index)
    assert "/api/spacebio/pump/step" in ex and "p.hz * p.sec" in ex and "p.hz * 60" in ex
    assert "/api/experiment/run" in ex and "duration_s" in ex
    assert "withSupervisionAck(" in ex and "/api/control/start" in ex
    assert "stopAllMotion()" in ex
    assert "/api/spacebio/pump/emergency-stop" in ex and "pump_id" not in _block(ex, "estop: async", "\n    },")
    assert "spacebioStartSession()" in ex and "spacebioFinishSession()" in ex
    assert "spacebioLog" in ex and "alert(" not in ex


def test_console_is_unchanged_by_the_widget(index):
    """콘솔은 자체 구현을 유지한다 — 위젯은 기존 화면 전용이다."""
    assert "voice_widget.js" not in CONSOLE.read_text(encoding="utf-8")
