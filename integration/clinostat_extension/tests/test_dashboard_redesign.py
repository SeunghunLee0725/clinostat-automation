"""대시보드 개편 (2026-10-29, A안).

펌프·저항센서 카드는 현재값과 실행 버튼만 보이고, 설정 입력은 [설정]을 **클릭**하면
카드 안에서 펼쳐진다(드래그 아님). 테마는 밝은 계측기 톤으로 바꾸되, baseline 줄을
지우지 않도록 맨 뒤에 덮어쓰는 스타일·스크립트로만 적용한다.
"""
from pathlib import Path

import pytest

WORK = Path(__file__).resolve().parents[1] / "work" / "static" / "index.html"


@pytest.fixture(scope="module")
def work() -> str:
    return WORK.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _pump_template(work: str) -> str:
    return _between(work, '<template id="pumpCardTemplate">', "</template>")


def _drop(block: str) -> str:
    """카드 안 설정 드롭(<details class="sb-drop">) 부분."""
    return _between(block, '<details class="sb-drop"', "</details>")


def _sensor_card(work: str) -> str:
    return _between(work, 'id="resistanceModeBadge"', '<h3>기록</h3>')


# ───────────── 실행 버튼은 밖, 설정은 클릭 드롭 안 ─────────────

@pytest.mark.parametrize("field", ["pumpRunHzBtn", "pumpDispense", "pumpStop"])
def test_pump_run_buttons_stay_outside_the_drop(work, field):
    tpl = _pump_template(work)
    assert f'data-f="{field}"' in tpl
    assert f'data-f="{field}"' not in _drop(tpl), f"{field} 는 바로 눌러야 한다"


@pytest.mark.parametrize("field", [
    "pumpRunHz", "pumpRunSec", "pumpRate", "pumpTargetVolume",
    "pumpUlPerStep", "pumpApplyCalibration", "pumpStepCount", "pumpStepSpm", "pumpSendSteps",
])
def test_pump_settings_live_in_the_drop(work, field):
    assert f'data-f="{field}"' in _drop(_pump_template(work))


@pytest.mark.parametrize("element_id", ["resistanceStart", "resistanceStop"])
def test_sensor_run_buttons_stay_outside_the_drop(work, element_id):
    card = _sensor_card(work)
    assert f'id="{element_id}"' in card
    assert f'id="{element_id}"' not in _drop(card)


@pytest.mark.parametrize("element_id", [
    "spacebioExperimentName", "resistanceRref", "resistanceAvgFactor",
])
def test_sensor_settings_live_in_the_drop(work, element_id):
    assert f'id="{element_id}"' in _drop(_sensor_card(work))


def test_drop_opens_on_click_not_drag(work):
    """<details>/<summary> 는 클릭으로 열린다. 드래그 핸들러를 두지 않는다."""
    assert work.count('<details class="sb-drop"') >= 3       # 센서 · 펌프 템플릿 · 비상정지 해제
    assert '<summary class="sb-drop-head">' in work
    for drag in ("draggable=", "ondragstart", "'dragstart'", "pointerdown"):
        assert drag not in work


def test_pump_drop_summary_shows_what_the_run_button_will_do(work):
    """설정이 접혀 있어도 [구동]이 무엇을 할지 보여야 한다."""
    tpl = _pump_template(work)
    assert tpl.count('data-f="pumpRunSummary"') == 1
    assert 'data-f="pumpRunSummary"' not in _drop(tpl)
    build = _between(work, "function spacebioBuildPumpCard", "\nfunction ")
    assert "pumpRunSummary" in build and "oninput" in build


def test_sensor_drop_summary_shows_applied_settings(work):
    """요약은 접혀도 보이는 머리줄(<summary>)에 있어야 한다."""
    head = _between(_sensor_card(work), '<summary class="sb-drop-head">', "</summary>")
    assert 'id="resistanceSettingSummary"' in head
    assert "resistanceSettingSummary" in _between(work, "function spacebioRenderStatus", "\n}")


def test_estop_button_is_always_visible_and_reset_is_folded(work):
    estop = _between(work, "<h3>펌프 비상정지</h3>", '<div class="sb-card sb-chart-card"')
    assert 'id="pumpEmergencyStop"' not in _drop(estop)
    assert 'id="pumpResetAcknowledgement"' in _drop(estop)


def test_pump_cards_are_not_folded_away(work):
    """구동 버튼이 접힌 영역 안에 숨으면 안 된다 — 기록 카드만 접는다."""
    layout = _between(work, "function spacebioApplyOneScreenLayout", "\n}")
    assert "/기록/.test(t)" in layout
    assert "펌프/.test" not in layout


# ───────────── 밝은 테마 ─────────────

def test_light_theme_overrides_come_after_every_other_style(work):
    theme = work.index('<style id="uiTheme">')
    assert theme > work.rindex("<style>"), "다른 스타일보다 뒤에 있어야 덮어쓴다"
    block = _between(work, '<style id="uiTheme">', "</style>")
    for token in ("--bg:", "--surface:", "--text:", "--text-dim:"):
        assert token in block


def test_charts_are_rethemed_at_runtime(work):
    """차트 색은 baseline 줄에 박혀 있어 고칠 수 없다 — 로드 뒤 옵션을 바꿔 다시 그린다."""
    assert "function uiApplyChartTheme" in work
    fn = _between(work, "function uiApplyChartTheme", "\n}")
    assert "Chart.getChart" in fn and "update(" in fn
    assert "sphereLayout" in fn and "Plotly.relayout" in fn
    assert "try" in fn, "테마 실패가 화면을 막으면 안 된다"
