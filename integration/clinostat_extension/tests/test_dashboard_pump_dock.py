"""펌프 1·2 카드를 가운데 열 논문 메트릭(극관 집중도·위도 밴드 밀도) 아래로 (2026-10-30).

펌프 카드는 SpaceBio 패널(#spacebioPanel) 안에서 만들어지고 스타일도 그 아래로 한정돼 있다.
카드를 가운데 열의 #pumpDock 으로 옮기고, 패널 한정 스타일은 로드 뒤 #pumpDock 용으로
복제한다. 넓은 화면에서는 세 열이 화면 높이를 채우는 직사각형을 유지한다.
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


def _tile_css(work: str) -> str:
    block = _between(work, '<style id="uiTheme">', "</style>")
    return block[block.index("@media (min-width: 1600px)"):]


def test_pumps_move_under_the_paper_metrics(work):
    fn = _between(work, "function uiApplyTileLayout", "\n}")
    assert "pumpDock" in fn and ".metrics-grid" in fn
    assert "insertBefore(dock, metrics.nextSibling)" in fn
    for el in ("pumpCards", "pumpLatchBanner"):
        assert f"getElementById('{el}')" in _between(fn, "dock.id = 'pumpDock'", "insertBefore(dock")


def test_device_grid_keeps_only_the_sensor_card(work):
    fn = _between(work, "function uiApplyTileLayout", "\n}")
    grid = _between(fn, "devices.className = 'ui-device-grid'", "grid.parentNode.insertBefore(devices")
    assert "resistanceModeBadge" in grid and "pumpCards" not in grid


def test_panel_scoped_styles_are_mirrored_to_the_dock(work):
    fn = _between(work, "function uiMirrorPanelStyles", "\n}")
    assert "cssRules" in fn and "insertRule" in fn and "CSSMediaRule" in fn
    assert "'#pumpDock'" in fn
    assert "try" in fn, "교차 출처 스타일시트 접근 실패가 화면을 막으면 안 된다"
    assert "uiMirrorPanelStyles();" in _between(work, "function uiApplyTileLayout", "\n}")


def test_stale_gateway_also_locks_the_dock(work):
    fn = _between(work, "function spacebioSetStale", "\n}")
    assert "pumpDock" in fn


def test_wide_layout_splits_the_middle_column_and_stays_rectangular(work):
    css = _tile_css(work)
    assert ".monitor #pumpDock" in css
    dock = _between(css, ".monitor #pumpDock {", "}")
    assert "flex: 1 1 0" in dock and "grid-template-columns: repeat(2, minmax(0, 1fr))" in dock
    assert ".monitor .metrics-grid { flex: 1 1 0;" in css
    assert "#spacebioPanel .ui-device-grid { grid-template-rows: minmax(0, 1fr);" in css


# ───────────── 저항센서·근육 카드 같은 높이 (2026-10-30) ─────────────
# 설정을 펼친 높이(실측 저항 452 px·근육 409 px)에 맞추되 화면(1010 px)에 들어가게,
# 저항 설정 칸을 한 줄로 모으고 두 카드를 같은 높이로 고정, 기록 줄이 나머지를 쓴다.

def test_sensor_and_emg_cards_share_one_fixed_height(work):
    css = _tile_css(work)
    assert "--ui-device-card-h: 400px;" in css
    dev = _between(css, "#spacebioPanel .ui-device-grid { flex: none;", "}")
    emg = _between(css, "#spacebioPanel .ui-row-emg { flex: none;", "}")
    assert "height: var(--ui-device-card-h)" in dev and "height: var(--ui-device-card-h)" in emg
    rec = _between(css, "#spacebioPanel .ui-row-records { flex: 1 1 0;", "}")
    assert "min-height: 150px" in rec


def test_sensor_settings_fit_on_one_row(work):
    block = _between(work, '<style id="uiTheme">', "</style>")
    assert "#spacebioPanel .sb-sensor-card .sb-drop-body .sb-fields { display: contents; }" in block
    for el in ('label[for="spacebioExperimentName"]', "#spacebioExperimentName",
               'label[for="resistanceRref"]', "#resistanceRref"):
        assert el in block
