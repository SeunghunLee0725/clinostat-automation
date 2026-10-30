"""근육 응답 전압 카드 (2026-10-30).

펌프 보드 ADS1115 AIN2−AIN3 캡처를 대시보드에서 걸고 마지막 결과(값·파형)를 본다.
다른 카드와 같은 규칙: 실행 버튼은 밖, 설정은 클릭 드롭 안, 차트는 흰 선, 프록시 경로만.
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


def _card(work: str) -> str:
    return _between(work, '<div class="sb-card sb-emg-card">', "<!-- /emg card -->")


@pytest.mark.parametrize("element_id", [
    "emgBadge", "emgPp", "emgRange", "emgSat", "emgCount", "emgTime",
    "emgCapture", "emgHint", "emgChart", "emgMs", "emgPga", "emgSettingSummary",
])
def test_card_has_each_element_once(work, element_id):
    assert work.count(f'id="{element_id}"') == 1
    assert f'id="{element_id}"' in _card(work)


def test_capture_button_is_outside_the_drop_and_settings_inside(work):
    card = _card(work)
    drop = _between(card, '<details class="sb-drop"', "</details>")
    assert 'id="emgCapture"' not in drop
    for field in ("emgMs", "emgPga"):
        assert f'id="{field}"' in drop


def test_duration_and_range_bounds_match_the_firmware(work):
    card = _card(work)
    ms = _between(card, 'id="emgMs"', ">")
    assert 'min="10"' in ms and 'max="2000"' in ms
    for mv in ("4096", "2048", "1024", "512", "256"):
        assert f'value="{mv}"' in card


def test_card_only_calls_proxied_emg_routes(work):
    assert "'/api/spacebio/emg/latest'" in work
    assert "'/api/spacebio/emg/capture'" in work
    assert "/api/emg/" not in work.replace("/api/spacebio/emg/", "")


def test_capture_polls_until_the_board_answers_with_a_deadline(work):
    fn = _between(work, "async function emgCaptureNow", "\n}")
    assert "pending" in fn and "Date.now()" in fn, "끝없이 폴링하면 안 된다"


def test_saturation_is_called_out(work):
    fn = _between(work, "function emgRender", "\n}")
    assert "sat" in fn and "범위" in fn


def test_chart_is_white_line_on_black(work):
    fn = _between(work, "function emgInitChart", "\n}")
    assert "'#ffffff'" in fn and "parsing: false" in fn


def test_tile_layout_gives_the_card_its_own_row_between_devices_and_records(work):
    fn = _between(work, "function uiApplyTileLayout", "\n}")
    assert "ui-row-emg" in fn and "card('emgBadge')" in fn
    assert "insertBefore(emgRow, recordsRow)" in fn
    css = _between(work, '<style id="uiTheme">', "</style>")
    assert "#spacebioPanel .ui-row-emg" in css


# ───────────── 연속 측정 (2026-10-30) ─────────────

def test_stream_toggle_sits_next_to_capture(work):
    card = _card(work)
    assert card.count('id="emgStream"') == 1
    run = _between(card, '<div class="sb-actions sb-runbar">', "</div>")
    assert 'id="emgCapture"' in run and 'id="emgStream"' in run


def test_stream_polls_the_proxied_route_and_keeps_a_rolling_window(work):
    assert "'/api/spacebio/emg/stream'" in work
    fn = _between(work, "function emgMergeStream", "\n}")
    assert "EMG_STREAM_KEEP_MS" in fn and "stream_id" in fn


def test_stream_stops_before_resistance_measurement_starts(work):
    """같은 ADS1115 를 쓴다 — 저항 측정 시작을 누르면 연속 측정부터 끈다."""
    hook = _between(work, "sbEl('resistanceStart').addEventListener", "{ capture: true });")
    assert "emgStreamSet(false)" in hook


def test_capture_is_disabled_while_streaming(work):
    fn = _between(work, "function emgSetStreamingUi", "\n}")
    assert "emgCapture" in fn and "disabled" in fn


# ───────────── 60 Hz 노치 (2026-10-30) ─────────────

def test_notch_toggle_lives_in_the_drop_and_is_on_by_default(work):
    drop = _between(_card(work), '<details class="sb-drop"', "</details>")
    box = _between(drop, 'id="emgNotch"', ">")
    assert 'type="checkbox"' in box and "checked" in box


def test_chart_draws_notched_white_over_raw_grey(work):
    fn = _between(work, "function emgInitChart", "\n}")
    assert fn.count("borderColor") == 2 and "'#525252'" in fn


def test_capture_and_stream_use_the_notched_series_when_on(work):
    for name in ("function emgRender(", "function emgRenderStream("):
        fn = _between(work, name, "\n}")
        assert "summary_notch" in fn, name
    assert "mv_notch" in _between(work, "function emgMergeStream", "\n}")
    draw = _between(work, "function emgDraw", "\n}")
    assert "emgNotchOn()" in draw


def test_notch_choice_is_remembered_safely(work):
    fn = _between(work, "function emgNotchOn", "\n}")
    assert "checked" in fn
    assert "try" in _between(work, "function emgSaveNotch", "\n}")
