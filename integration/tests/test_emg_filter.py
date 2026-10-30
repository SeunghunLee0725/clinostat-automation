"""근육 응답 전압 60 Hz 노치 (2026-10-30).

보드 샘플 간격은 1.6~3.1 ms 로 들쭉날쭉하다. 균일 격자로 다시 찍으면 보간 오차가
험을 광대역 잡음으로 바꾼다. 그래서 **실제 샘플 시각에서** 60·120·180 Hz 사인·코사인과
직류를 최소제곱으로 맞추고 사인 성분만 뺀다(0.5 s 창 — 전원 주파수 미세 변동을 따라간다).
"""

from __future__ import annotations

import math
import random

import pytest

from gateway.emg_filter import StreamNotch, notch_series


def _jittered_times(seconds: float, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    t, out = 0.0, []
    while t < seconds * 1000.0:
        out.append(t)
        t += 1.6 + rng.random() * 0.4 if rng.random() > 0.02 else 3.1
    return out


def _slow(t_ms: float) -> float:
    return 100.0 * math.sin(2 * math.pi * 10 * t_ms / 1000.0)


def _signal(t_ms: float, hum_mv: float = 1000.0, mains_hz: float = 60.0) -> float:
    s = t_ms / 1000.0
    return (_slow(t_ms) + 14.0
            + hum_mv * math.sin(2 * math.pi * mains_hz * s)
            + 0.3 * hum_mv * math.sin(2 * math.pi * 2 * mains_hz * s + 0.4)
            + 0.1 * hum_mv * math.sin(2 * math.pi * 3 * mains_hz * s + 1.1))


def _max_err(t, y):
    return max(abs(b - (_slow(a) + 14.0)) for a, b in zip(t, y))


def test_capture_hum_and_harmonics_are_removed():
    t = _jittered_times(2.0)
    out = notch_series(t, [_signal(x) for x in t])
    assert len(out) == len(t)
    assert _max_err(t, out) < 15.0, "험 1000 mV 가 15 mV 아래로 줄어야 한다"


def test_small_mains_drift_is_tolerated():
    t = _jittered_times(2.0)
    out = notch_series(t, [_signal(x, mains_hz=60.03) for x in t])
    assert _max_err(t, out) < 40.0


def test_dc_offset_is_kept():
    t = _jittered_times(0.3)
    assert notch_series(t, [14.0] * len(t)) == pytest.approx([14.0] * len(t))


def test_too_few_samples_pass_through_unchanged():
    assert notch_series([0.0, 2.0, 4.0], [1.0, 5.0, 2.0]) == [1.0, 5.0, 2.0]


def test_stream_removes_hum_after_the_first_window():
    t = _jittered_times(2.0)
    v = [_signal(x) for x in t]
    sn = StreamNotch()
    out_t, out_v = [], []
    for i in range(0, len(t), 64):
        out_v.extend(sn.feed(t[i:i + 64], v[i:i + 64]))
        out_t.extend(t[i:i + 64])
    assert len(out_v) == len(t)
    settled = [(a, b) for a, b in zip(out_t, out_v) if a > 500.0]
    assert _max_err(*zip(*settled)) < 15.0


def test_stream_forgets_samples_older_than_the_window():
    sn = StreamNotch()
    sn.feed(_jittered_times(0.3), [500.0] * len(_jittered_times(0.3)))
    later = [5000.0 + x for x in _jittered_times(0.2)]
    out = sn.feed(later, [9.0] * len(later))
    assert out == pytest.approx([9.0] * len(later))


def test_stream_resets_when_time_goes_backwards():
    sn = StreamNotch()
    t = _jittered_times(0.3)
    sn.feed([1000.0 + x for x in t], [_signal(x) for x in t])
    out = sn.feed(t, [7.0] * len(t))
    assert out == pytest.approx([7.0] * len(t))
