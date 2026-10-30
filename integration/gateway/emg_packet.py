"""근육 응답 전압 캡처 패킷 파서 (2026-10-30).

펌프 보드(ESP32-C6)가 근육 전용 ADS1115(0x49) AIN0−AIN1 차동 입력을 단발 변환으로 연속 읽어
(`emg:cap:<ms>`) 캡처 하나를 조각으로 나눠 `s25007/board1/emg` 로 발행한다
(firmware/clinostat_cube_esp32c6/sensor_muscle.h). **펌웨어 struct 가 바뀌면 같이 고친다.**

리틀엔디안 조각:
    헤더  uint32 capture_id · uint16 chunk_index · uint16 chunk_count · uint16 n · uint16 pga_mv
    본문  n × (uint32 t_us · int16 raw)      t_us = 캡처 시작부터 그 변환을 시작한 시각

연속 측정(`emg:stream:on`)은 같은 형식을 `s25007/board1/emg/stream` 으로 보낸다.
이때 capture_id = 스트림 번호, chunk_index = 일련번호(uint16, 넘치면 0 으로), chunk_count = 0.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

EMG_TOPIC = "s25007/board1/emg"
EMG_STREAM_TOPIC = "s25007/board1/emg/stream"
EMG_HEADER_FORMAT: str = "<IHHHH"
EMG_HEADER_SIZE: int = struct.calcsize(EMG_HEADER_FORMAT)  # 12
EMG_SAMPLE_FORMAT: str = "<Ih"
EMG_SAMPLE_SIZE: int = struct.calcsize(EMG_SAMPLE_FORMAT)  # 6

_RAW_MAX, _RAW_MIN = 32767, -32768


class EmgPacketError(ValueError):
    """조각 길이·번호가 헤더와 맞지 않는다."""


def raw_to_mv(raw: int, pga_mv: int) -> float:
    return raw * pga_mv / 32768.0


@dataclass(frozen=True)
class EmgChunk:
    capture_id: int
    index: int
    count: int
    pga_mv: int
    samples: Tuple[Tuple[int, int], ...]


@dataclass(frozen=True)
class EmgCapture:
    capture_id: int
    pga_mv: int
    samples_raw: Tuple[Tuple[int, int], ...]

    @property
    def samples_mv(self) -> Tuple[Tuple[int, float], ...]:
        return tuple((t, raw_to_mv(r, self.pga_mv)) for t, r in self.samples_raw)

    def summary(self) -> Dict[str, float]:
        raws = [r for _, r in self.samples_raw]
        if not raws:
            return {"n": 0, "min_mv": 0.0, "max_mv": 0.0, "pp_mv": 0.0, "saturated": 0}
        lo, hi = raw_to_mv(min(raws), self.pga_mv), raw_to_mv(max(raws), self.pga_mv)
        return {
            "n": len(raws),
            "min_mv": lo,
            "max_mv": hi,
            "pp_mv": hi - lo,
            "saturated": sum(1 for r in raws if r >= _RAW_MAX or r <= _RAW_MIN),
        }


def parse_emg_chunk(data: bytes) -> EmgChunk:
    return _parse(data, stream=False)


def parse_emg_stream_chunk(data: bytes) -> EmgChunk:
    return _parse(data, stream=True)


def _parse(data: bytes, *, stream: bool) -> EmgChunk:
    if len(data) < EMG_HEADER_SIZE:
        raise EmgPacketError(f"헤더보다 짧다: {len(data)} B")
    cap_id, idx, count, n, pga_mv = struct.unpack_from(EMG_HEADER_FORMAT, data, 0)
    expected = EMG_HEADER_SIZE + n * EMG_SAMPLE_SIZE
    if len(data) != expected:
        raise EmgPacketError(f"길이 {len(data)} B ≠ 헤더 기준 {expected} B")
    if stream and count != 0:
        raise EmgPacketError(f"연속 측정 조각인데 조각 수 {count}")
    if not stream and (count == 0 or idx >= count):
        raise EmgPacketError(f"조각 번호 {idx}/{count}")
    samples = tuple(
        struct.unpack_from(EMG_SAMPLE_FORMAT, data, EMG_HEADER_SIZE + i * EMG_SAMPLE_SIZE)
        for i in range(n)
    )
    return EmgChunk(cap_id, idx, count, pga_mv, samples)


class EmgAssembler:
    """조각을 모아 캡처 하나로 합친다. 새 capture_id 가 오면 덜 모인 이전 캡처는 버린다."""

    def __init__(self) -> None:
        self._capture_id: Optional[int] = None
        self._chunks: Dict[int, EmgChunk] = {}
        self.dropped_captures = 0

    def feed(self, data: bytes) -> Optional[EmgCapture]:
        chunk = parse_emg_chunk(data)
        if chunk.capture_id != self._capture_id:
            if self._chunks:
                self.dropped_captures += 1
            self._capture_id, self._chunks = chunk.capture_id, {}
        self._chunks = {**self._chunks, chunk.index: chunk}
        if len(self._chunks) < chunk.count:
            return None
        ordered = [self._chunks[i] for i in range(chunk.count)]
        self._capture_id, self._chunks = None, {}
        samples = tuple(s for c in ordered for s in c.samples)
        return EmgCapture(chunk.capture_id, chunk.pga_mv, samples)
