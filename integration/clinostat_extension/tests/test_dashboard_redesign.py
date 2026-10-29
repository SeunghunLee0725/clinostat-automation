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
    assert work.count('<details class="sb-drop"') >= 2       # 센서 · 펌프 템플릿
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


def test_estop_card_is_gone_but_a_latch_shows_the_reset(work):
    """비상정지 카드는 없앴다(2026-10-30). 래치가 남아 있으면 풀 길이 있어야 하므로
    해제 줄은 평소 숨겨 두고 래치됐을 때만 보인다."""
    assert "<h3>펌프 비상정지</h3>" not in work
    banner = _between(work, 'id="pumpLatchBanner"', "</div>\n    </div>")
    assert banner.startswith('id="pumpLatchBanner" hidden')
    assert 'id="pumpResetAcknowledgement"' in banner and 'id="pumpResetEmergencyStop"' in banner
    render = _between(work, "function spacebioRenderStatus", "\n}")
    assert "sbEl('pumpLatchBanner').hidden = !anyLatched" in render


def test_resistance_chart_lives_inside_the_sensor_card(work):
    card = _sensor_card(work)
    assert 'id="resistanceChart"' in card
    assert "<h3>저항 추이</h3>" not in work


def test_sensor_card_spans_the_row_and_pumps_share_the_next(work):
    assert "#spacebioPanel .ui-device-grid > .sb-sensor-card { grid-column: 1 / -1; }" in _theme(work)
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in _tile_css(work)


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


# ───────────── 직사각형 타일 배치 (2026-10-29 2차) ─────────────
#
# 설정을 펼치면 같은 줄 카드가 함께 늘어나 열 높이가 1,620 / 920 / 2,509px 로
# 들쭉날쭉했다. 넓은 화면에서는 세 열을 화면 높이에 고정하고, 카드 칸 크기를
# 일정하게 둔 채 펼친 설정은 카드 안에서 스크롤한다.

def _tile_css(work: str) -> str:
    block = _between(work, '<style id="uiTheme">', "</style>")
    return block[block.index("@media (min-width: 1600px)"):]


def test_wide_layout_pins_the_three_columns_to_the_viewport(work):
    css = _tile_css(work)
    assert "height: 100vh" in css
    assert "align-items: stretch !important" in css, "앞선 align-items:start 를 이겨야 한다"
    assert css.count("overflow-y: auto") >= 3


def test_device_cards_share_equal_cells_and_scroll_inside(work):
    css = _tile_css(work)
    assert "grid-template-rows: minmax(0, 1fr) minmax(0, 1fr)" in css
    assert "#spacebioPanel .ui-device-grid > .sb-card" in css


def test_stop_all_stays_pinned_at_the_bottom_of_the_left_column(work):
    css = _tile_css(work)
    stop = css[css.index(".btn-stop"):][:160]
    assert "position: sticky" in stop and "bottom: 0" in stop


def test_tile_layout_regroups_cards_at_runtime(work):
    fn = _between(work, "function uiApplyTileLayout", "\n}")
    assert "if (!panel) return;" in fn, "배치 실패가 기능을 막으면 안 된다"
    for cls in ("ui-device-grid", "ui-row-records"):
        assert cls in fn
    assert "spacebioArchive" in fn and "spacebioEvents" in fn
    assert "uiApplyTileLayout();" in work


def test_opened_settings_scroll_into_view_inside_the_card(work):
    fn = _between(work, "function uiApplyTileLayout", "\n}")
    assert "'toggle'" in fn and "scrollIntoView" in fn


# ───────────── 검은 배경 · 흰 글자 · 흰 그래프선 (2026-10-29 3차) ─────────────

import re


def _theme(work: str) -> str:
    return _between(work, '<style id="uiTheme">', "</style>")


def test_theme_is_black_background_with_white_text(work):
    root = _between(_theme(work), ":root {", "}")
    assert "--bg: #000000;" in root
    assert "--text: #ffffff;" in root
    assert "--on-primary: #000000;" in root, "흰 주 버튼 위 글자는 검정이어야 읽힌다"


def test_theme_has_no_light_surfaces_left(work):
    """밝은 테마에서 박아 둔 흰 배경·옅은 색이 남으면 검은 화면에서 흰 덩어리로 뜬다."""
    theme = _theme(work)
    for light in ("background: #fff", "#fef2f2", "#fef3c7", "#dcfce7", "#f9fafb"):
        assert light not in theme, f"{light} 가 남아 있다"


def test_primary_buttons_are_white_on_black(work):
    theme = _theme(work)
    for sel in (".btn-connect, .btn-start", "#spacebioPanel button.sb-primary {"):
        rule = theme[theme.index(sel):][:200]
        assert "var(--primary)" in rule and "var(--on-primary)" in rule


def test_chart_lines_are_white_on_black(work):
    fn = _between(work, "function uiApplyChartTheme", "\n}")
    assert "'#4cc9f0': '#ffffff'" in work, "주 계열선은 흰색"
    assert re.search(r"paper_bgcolor: '#000000'", fn)
    assert "marker" in fn, "중력벡터 점도 흰색이어야 한다"


def test_panel_title_and_subtitle_are_gone(work):
    """2026-10-30: 'SpaceBio Integration' 제목과 부제를 지웠다."""
    assert "<h2>SpaceBio Integration</h2>" not in work
    assert 'id="spacebioSubtitle"' not in work
