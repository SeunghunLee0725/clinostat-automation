"""B안 연구실 노트 테마 (2026-11-05) — uiTheme 블록이 종이 톤 변수를 정의하고 차트 색 함수는 하나뿐이다.

처음엔 검은 테마 뒤에 덮어쓰기 블록을 덧붙였다가, 같은 날 검은 테마 변수를 B안 값으로 바꿔 한 블록으로 합쳤다
(uiTheme 블록은 baseline 줄이 아니라 고쳐도 된다). 높이 상한(uiHeightCaps)·배율(uiScale)은 별도 블록 그대로.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "work" / "static"
INDEX = STATIC / "index.html"
WIDGET = STATIC / "voice_widget.js"
CONSOLE = STATIC / "console.html"

WARM = {"--bg": "#F3F1EC", "--surface": "#FFFDF9", "--text": "#2A2A28", "--primary": "#0F766E", "--accent": "#B91C1C",
        "--line": "#E4DFD5", "--text-dim": "#6B6760", "--green": "#0F766E"}


@pytest.fixture(scope="module")
def page() -> str:
    return INDEX.read_text(encoding="utf-8")


def _theme(page: str) -> str:
    i = page.index('<style id="uiTheme">')
    return page[i:page.index("</style>", i)]


def test_single_theme_block_no_leftover_override_block(page):
    assert page.count('<style id="uiTheme">') == 1
    assert 'uiThemeWarmLab' not in page
    assert page.index('<style id="uiHeightCaps">') > page.index('<style id="uiTheme">')


@pytest.mark.parametrize("var,value", list(WARM.items()))
def test_warm_root_variables(page, var, value):
    root = re.search(r":root \{(.*?)\}", _theme(page), re.S).group(1)
    assert re.search(re.escape(var) + r":\s*" + re.escape(value) + r";", root), var


def test_no_black_theme_values_remain_in_theme_block(page):
    theme = _theme(page)
    for black in ("#000000", "#0a0a0a", "--text: #ffffff", "#a3a3a3", "rgba(255,255,255"):
        assert black not in theme, black
    assert ".outer-color { color: var(--slate); }" in theme
    assert ".inner-color { color: var(--green); }" in theme
    assert "Noto Sans KR" in theme and "IBM Plex Mono" in theme


def test_run_button_is_primary_not_test_styled(page):
    assert 'button.btn-test[onclick="applyOperationCondition(true)"] { background: var(--primary);' in _theme(page)


def test_chart_theme_function_is_declared_once_with_warm_colors(page):
    assert page.count("function uiApplyChartTheme()") == 1
    i = page.index("function uiApplyChartTheme()")
    script = page[page.rindex("<script>", 0, i):page.index("</script>", i)]
    assert "UI_WARM_SERIES" in script and "'#334155'" in script and "'#0F766E'" in script
    assert "UI_SERIES_COLORS" not in page and "'#000000'" not in script


def test_chart_theme_call_sites_resolve_by_name(page):
    assert "setInterval(uiApplyChartTheme" not in page
    assert "setTimeout(uiApplyChartTheme, 800)" in page


def test_voice_widget_follows_page_variables():
    css = WIDGET.read_text(encoding="utf-8")
    for token in ("var(--surface,", "var(--primary,", "var(--text,", "var(--line,", "var(--bg,"):
        assert token in css
    assert "#16213e)" in css        # 변수가 없는 페이지에서는 예전 어두운 색으로 되돌아간다


def test_console_gets_the_same_palette():
    console = CONSOLE.read_text(encoding="utf-8")
    assert '<style id="csThemeWarmLab">' in console
    block = console[console.index('<style id="csThemeWarmLab">'):]
    block = block[:block.index("</style>")]
    for token in ("--bg:#F3F1EC", "--accent:#0F766E", "--text:#2A2A28", "--ok-fg:#0F766E"):
        assert token in block
    assert console.index('<style id="csThemeWarmLab">') > console.index("--bg:#0A0F1C")
