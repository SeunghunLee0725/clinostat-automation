"""타일 배치에서 남는 높이를 흡수하는 줄에는 상한이 있어야 한다 (2026-11-05).

큰 모니터(5120×2588 실측)에서 기록 줄이 1,682px, 펌프 칸이 922px 로 늘어나 기록 관리 목록이 97행 노출됐다.
베이스라인 줄은 못 고치므로 뒤에 덧붙인 `#uiHeightCaps` 스타일이 상한을 건다. 이 테스트는 그 상한이 남아 있고,
타일 블록보다 **뒤에** 있어 같은 우선순위에서 이기는지를 본다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "work" / "static" / "index.html"


@pytest.fixture(scope="module")
def page() -> str:
    return INDEX.read_text(encoding="utf-8")


def _caps(page: str) -> str:
    i = page.index('<style id="uiHeightCaps">')
    return page[i:page.index("</style>", i)]


def test_caps_block_comes_after_the_tile_layout_block(page):
    tile = page.index("#spacebioPanel .ui-row-records { flex: 1 1 0; height: auto; min-height: 150px; }")
    assert page.index('<style id="uiHeightCaps">') > tile


@pytest.mark.parametrize("selector", [
    "#spacebioPanel .ui-row-records",
    "#spacebioArchive .sb-arch-list",
    ".monitor #pumpDock",
    ".monitor .metrics-grid",
])
def test_every_growing_row_has_a_max_height_in_tile_mode(page, selector):
    tile_part = _caps(page)
    tile_part = tile_part[tile_part.index("@media (min-width: 1600px)"):]
    rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", tile_part)
    assert rule, selector
    cap = re.search(r"max-height:\s*(\d+)px", rule.group(1))
    assert cap and 200 <= int(cap.group(1)) <= 600, selector


def test_archive_list_scrolls_inside_its_card_everywhere(page):
    caps = _caps(page)
    assert "#spacebioArchive .sb-arch-list { max-height: 360px; overflow-y: auto; }" in caps
    # 비타일에서도 상한 — 미디어 쿼리 밖에 한 번 더
    outside = caps[caps.rindex("}") :] if "@media" not in caps[caps.rindex("}"):] else ""
    assert re.search(r"\n#spacebioArchive \.sb-arch-list \{ max-height: 360px; \}", caps)


# ─────────────────────────── 큰 화면 배율 (2026-11-05) ───────────────────────────

def _scale(page: str) -> str:
    i = page.index('<style id="uiScale">')
    return page[i:page.index("</style>", i)]


def test_scale_block_comes_after_tile_and_caps_blocks(page):
    assert page.index('<style id="uiScale">') > page.index('<style id="uiHeightCaps">')


@pytest.mark.parametrize("min_width,zoom", [(2400, "1.5"), (3600, "1.75"), (4800, "2")])
def test_each_width_step_zooms_body_and_compensates_viewport_height(page, min_width, zoom):
    block = _scale(page)
    i = block.index(f"@media (min-width: {min_width}px)")
    rule = block[i:block.index("}", block.index("body {", i))]
    assert f"zoom: {zoom};" in rule
    assert f"height: calc(100vh / {zoom});" in rule and f"min-height: calc(100vh / {zoom});" in rule


def test_no_scaling_below_2400px(page):
    block = _scale(page)
    widths = [int(w) for w in re.findall(r"min-width: (\d+)px", block)]
    assert min(widths) >= 2400
