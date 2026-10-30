"""근육 응답 전압 연속 측정의 최근 구간 (2026-10-30).

보드가 보내는 연속 조각을 시간(보드 기준 µs → ms) 순으로 쌓고 `keep_s` 보다 오래된 것은
버린다. 화면은 짧은 구간만 자주 가져가 자기 쪽에서 이어 붙인다 — 매번 전부 보내면
Tailscale 너머에서 무겁다. 구간 통계(pp·포화)는 가져간 구간 기준이다.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable, Deque, Optional, Tuple

from gateway.emg_filter import StreamNotch
from gateway.emg_packet import EmgChunk, raw_to_mv

_RAW_MAX, _RAW_MIN = 32767, -32768
#: 이 시간 안에 조각이 왔으면 "받는 중" — 조각은 64샘플(≈115 ms)마다 온다.
RECEIVING_WITHIN_S = 1.5


class EmgStreamWindow:
    def __init__(self, *, keep_s: float = 10.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._keep_ms = keep_s * 1000.0
        self._clock = clock
        #: (t_ms, mV, 포화, 노치 거친 mV)
        self._samples: Deque[Tuple[float, float, bool, float]] = deque()
        self._notch = StreamNotch()
        self._stream_id: Optional[int] = None
        self._pga_mv: Optional[int] = None
        self._next_seq: Optional[int] = None
        self._lost = 0
        self._last_rx: Optional[float] = None

    def feed(self, chunk: EmgChunk) -> None:
        if chunk.capture_id != self._stream_id:
            self._samples.clear()
            self._notch.reset()
            self._stream_id, self._next_seq, self._lost = chunk.capture_id, None, 0
        if self._next_seq is not None and chunk.index != self._next_seq:
            self._lost += (chunk.index - self._next_seq) & 0xFFFF
        self._next_seq = (chunk.index + 1) & 0xFFFF
        self._pga_mv = chunk.pga_mv
        if self._samples and chunk.samples and chunk.samples[0][0] / 1000.0 < self._samples[-1][0] - 1000.0:
            self._samples.clear()          # 보드 µs 시계가 한 바퀴 돌았다(≈71분) — 노치는 스스로 비운다
        t_ms = [t_us / 1000.0 for t_us, _ in chunk.samples]
        mv = [raw_to_mv(raw, chunk.pga_mv) for _, raw in chunk.samples]
        notched = self._notch.feed(t_ms, mv)
        for t, v, (_, raw), vn in zip(t_ms, mv, chunk.samples, notched):
            self._samples.append((t, v, raw >= _RAW_MAX or raw <= _RAW_MIN, vn))
        if self._samples:
            oldest = self._samples[-1][0] - self._keep_ms
            while self._samples and self._samples[0][0] < oldest:
                self._samples.popleft()
        self._last_rx = self._clock()

    def snapshot(self, *, window_s: float) -> dict[str, Any]:
        recent = list(self._samples)
        if recent:
            since = recent[-1][0] - window_s * 1000.0
            recent = [s for s in recent if s[0] >= since]
        mv = [round(v, 4) for _, v, _, _ in recent]
        mv_notch = [round(v, 4) for _, _, _, v in recent]
        return {
            "stream_id": self._stream_id,
            "pga_mv": self._pga_mv,
            "receiving": self._last_rx is not None and self._clock() - self._last_rx <= RECEIVING_WITHIN_S,
            "lost_chunks": self._lost,
            "t_ms": [round(t, 3) for t, _, _, _ in recent],
            "mv": mv,
            "mv_notch": mv_notch,
            "summary_notch": {
                "min_mv": min(mv_notch) if mv_notch else 0.0,
                "max_mv": max(mv_notch) if mv_notch else 0.0,
                "pp_mv": (max(mv_notch) - min(mv_notch)) if mv_notch else 0.0,
            },
            "summary": {
                "n": len(mv),
                "min_mv": min(mv) if mv else 0.0,
                "max_mv": max(mv) if mv else 0.0,
                "pp_mv": (max(mv) - min(mv)) if mv else 0.0,
                "saturated": sum(1 for _, _, sat, _ in recent if sat),
            },
        }
