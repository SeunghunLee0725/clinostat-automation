"""근육 응답 전압 60 Hz 노치 — 실제 샘플 시각에서 최소제곱으로 험을 빼낸다 (2026-10-30).

보드 샘플 간격은 1.6~3.1 ms 로 들쭉날쭉하다(단발 변환 + I²C 폴링). 균일 격자로 보간한 뒤
biquad 노치를 걸면 보간 오차가 1 V 험을 ±50 mV 광대역 잡음으로 바꾼다. 대신 각 샘플
시각에서 [1, sin/cos 60·120·180 Hz] 를 맞추고 **사인 성분만** 뺀다 — 직류와 그 밖의
주파수(근육 신호)는 남는다. 창을 0.5 s 로 짧게 두고, 각 고조파에 τ·sin·τ·cos(τ = 창 가운데
기준 시간) 항을 더해 전원 주파수가 60 Hz 에서 조금 벗어나도(±0.05 Hz) 따라간다 —
그 항이 없으면 +0.03 Hz 에서 1 V 험이 46 mV 남았다.

⚠ 근육 신호의 60·120·180 Hz 성분도 함께 빠진다(노치의 본성). 포화된 샘플이 섞이면
  맞춤이 틀어지므로 화면은 포화가 있을 때 원신호를 같이 본다.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from typing import Deque, List, Optional, Sequence, Tuple

HARMONICS_HZ = (60.0, 120.0, 180.0)
WINDOW_MS = 500.0
BLOCK_SAMPLES = 64
_N_PARAMS = 1 + 4 * len(HARMONICS_HZ)
#: 이보다 적으면 맞춤이 불안정하다 — 그대로 돌려준다.
MIN_FIT_SAMPLES = 2 * _N_PARAMS


def _basis(t_ms: float, center_ms: float) -> List[float]:
    s = t_ms / 1000.0
    tau = (t_ms - center_ms) / 1000.0
    row = [1.0]
    for f in HARMONICS_HZ:
        w = 2.0 * math.pi * f * s
        sn, cs = math.sin(w), math.cos(w)
        row.extend((sn, cs, tau * sn, tau * cs))
    return row


def _solve(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """부분 피벗 가우스 소거. 특이하면 None."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-9:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            k = m[r][col] / m[col][col]
            if k:
                m[r] = [x - k * y for x, y in zip(m[r], m[col])]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def _fit(ts: Sequence[float], vs: Sequence[float], center_ms: float) -> Optional[List[float]]:
    if len(ts) < MIN_FIT_SAMPLES:
        return None
    ata = [[0.0] * _N_PARAMS for _ in range(_N_PARAMS)]
    atb = [0.0] * _N_PARAMS
    for t, v in zip(ts, vs):
        row = _basis(t, center_ms)
        for i in range(_N_PARAMS):
            atb[i] += row[i] * v
            for j in range(i, _N_PARAMS):
                ata[i][j] += row[i] * row[j]
    for i in range(_N_PARAMS):
        for j in range(i):
            ata[i][j] = ata[j][i]
    return _solve(ata, atb)


def _remove(ts: Sequence[float], vs: Sequence[float], w: Optional[List[float]],
            center_ms: float) -> List[float]:
    if w is None:
        return list(vs)
    return [v - sum(c * b for c, b in zip(w[1:], _basis(t, center_ms)[1:])) for t, v in zip(ts, vs)]


def notch_series(t_ms: Sequence[float], mv: Sequence[float]) -> List[float]:
    """캡처 한 개. 64샘플 묶음마다 그 묶음을 가운데 둔 0.5 s 창으로 맞춘다."""
    out: List[float] = []
    for start in range(0, len(t_ms), BLOCK_SAMPLES):
        end = min(start + BLOCK_SAMPLES, len(t_ms))
        mid = (t_ms[start] + t_ms[end - 1]) / 2.0
        lo = bisect.bisect_left(t_ms, mid - WINDOW_MS / 2.0)
        hi = bisect.bisect_right(t_ms, mid + WINDOW_MS / 2.0)
        w = _fit(t_ms[lo:hi], mv[lo:hi], mid)
        out.extend(_remove(t_ms[start:end], mv[start:end], w, mid))
    return out


class StreamNotch:
    """연속 측정용. 들어온 조각을 포함한 최근 0.5 s 로 맞춰 그 조각만 거른다."""

    def __init__(self) -> None:
        self._buf: Deque[Tuple[float, float]] = deque()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, t_ms: Sequence[float], mv: Sequence[float]) -> List[float]:
        if not t_ms:
            return []
        if self._buf and t_ms[0] < self._buf[-1][0]:
            self._buf.clear()           # 새 스트림이거나 보드 시계가 한 바퀴 돌았다
        self._buf.extend(zip(t_ms, mv))
        oldest = t_ms[-1] - WINDOW_MS
        while self._buf and self._buf[0][0] < oldest:
            self._buf.popleft()
        bt, bv = zip(*self._buf)
        center = (bt[0] + bt[-1]) / 2.0
        return _remove(t_ms, mv, _fit(bt, bv, center), center)
