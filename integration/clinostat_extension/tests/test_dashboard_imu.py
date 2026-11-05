"""IMU 값 줄은 센서가 연결됐을 때만 보인다 (2026-10-30).

IMU 선을 연결하지 않은 구성에서 0.00 만 찍힌 9칸이 화면 위쪽을 차지했다.
연결 표시(dotSensor 의 'on')가 이미 연결·해제·초기 상태를 모두 반영하므로,
baseline 줄을 고치지 않고 그 점을 지켜보며 `.sensor-grid` 를 숨긴다.
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


def test_imu_row_follows_the_sensor_connection_dot(work):
    fn = _between(work, "function uiSyncImuRow", "\n}")
    assert "dotSensor" in fn and "classList.contains('on')" in fn
    assert ".sensor-grid" in fn and ".hidden" in fn


def test_dot_changes_are_watched_not_polled(work):
    setup = _between(work, "function uiWatchImuConnection", "\n}")
    assert "MutationObserver" in setup and "attributeFilter: ['class']" in setup
    assert "uiWatchImuConnection();" in work


def test_hidden_beats_the_grid_display_rule(work):
    """`.sensor-grid { display: grid }` 가 hidden 속성을 이기므로 따로 막는다."""
    assert ".sensor-grid[hidden] { display: none !important; }" in work


def test_hidden_until_connected(work):
    fn = _between(work, "function uiSyncImuRow", "\n}")
    assert "!connected" in fn
