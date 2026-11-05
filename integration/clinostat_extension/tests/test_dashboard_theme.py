"""B안 연구실 노트 테마 (2026-11-05) — 변수 덮어쓰기 블록과 차트 색 함수가 남아 있고 순서가 맞는지.

검은 배경 테마(uiTheme)는 baseline 이 아니라 지워도 되지만, 덧붙이는 방식이 이 저장소의 관례라 뒤에 두고 이긴다.
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
        "--line": "#E4DFD5", "--text-dim": "#6B6760"}


@pytest.fixture(scope="module")
def page() -> str:
    return INDEX.read_text(encoding="utf-8")


def _theme(page: str) -> str:
    i = page.index('<style id="uiThemeWarmLab">')
    return page[i:page.index("</style>", i)]


def test_warm_theme_block_comes_after_black_theme_and_scale(page):
    assert page.index('<style id="uiThemeWarmLab">') > page.index('<style id="uiTheme">')
    assert page.index('<style id="uiThemeWarmLab">') > page.index('<style id="uiScale">')


@pytest.mark.parametrize("var,value", list(WARM.items()))
def test_warm_root_variables(page, var, value):
    root = re.search(r":root \{(.*?)\}", _theme(page), re.S).group(1)
    assert re.search(re.escape(var) + r":\s*" + re.escape(value) + r";", root), var


def test_hardcoded_black_theme_lines_are_overridden(page):
    theme = _theme(page)
    assert ".outer-color { color: var(--slate); }" in theme
    assert ".inner-color { color: var(--green); }" in theme
    assert "#spacebioPanel .sb-badge.sb-live" in theme


def test_chart_theme_function_is_redeclared_with_warm_colors(page):
    first = page.index("function uiApplyChartTheme()")
    second = page.index("function uiApplyChartTheme()", first + 1)
    assert second > page.index('<style id="uiThemeWarmLab">')
    warm = page[page.rindex("<script>", 0, second):page.index("</script>", second)]
    assert "UI_WARM_SERIES" in warm and "'#334155'" in warm and "'#0F766E'" in warm
    assert "'#000000'" not in warm and "'#ffffff'" not in warm.replace("'#ffffff': '#0F766E'", "")


def test_chart_theme_call_sites_resolve_at_runtime(page):
    """호출은 앞 스크립트에 있지만 전역 이름으로 부르므로 나중 선언이 쓰인다 — 직접 참조(setInterval(fn))가 없어야 한다."""
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
