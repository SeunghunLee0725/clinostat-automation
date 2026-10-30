"""근육 응답 전압 캡처 패킷 (2026-10-30, firmware sensor_muscle.h 의 `s25007/board1/emg`).

보드는 캡처 하나를 조각(chunk) 여러 개로 나눠 보낸다. 조각 하나만 풀 수 있어야 하고,
모두 모이면 시간순 샘플(µs, mV)로 합쳐져야 한다. 빠진 조각은 조용히 넘기지 않는다.
"""

from __future__ import annotations

import struct

import pytest

from gateway.emg_packet import (
    EMG_HEADER_FORMAT,
    EMG_SAMPLE_FORMAT,
    EmgAssembler,
    EmgPacketError,
    parse_emg_chunk,
    raw_to_mv,
)


def _chunk(cap_id, idx, count, samples, pga_mv=2048):
    head = struct.pack(EMG_HEADER_FORMAT, cap_id, idx, count, len(samples), pga_mv)
    body = b"".join(struct.pack(EMG_SAMPLE_FORMAT, t, raw) for t, raw in samples)
    return head + body


def test_raw_to_mv_uses_pga_full_scale():
    assert raw_to_mv(16384, 2048) == pytest.approx(1024.0)
    assert raw_to_mv(-32768, 256) == pytest.approx(-256.0)


def test_parse_single_chunk():
    c = parse_emg_chunk(_chunk(7, 0, 1, [(0, 100), (1163, -200)], pga_mv=512))
    assert (c.capture_id, c.index, c.count, c.pga_mv) == (7, 0, 1, 512)
    assert c.samples == ((0, 100), (1163, -200))


def test_parse_rejects_truncated_body():
    data = _chunk(1, 0, 1, [(0, 1), (10, 2)])[:-1]
    with pytest.raises(EmgPacketError):
        parse_emg_chunk(data)


def test_parse_rejects_short_header():
    with pytest.raises(EmgPacketError):
        parse_emg_chunk(b"\x00" * 5)


def test_parse_rejects_bad_index():
    with pytest.raises(EmgPacketError):
        parse_emg_chunk(_chunk(1, 2, 2, [(0, 1)]))


def test_assembler_returns_capture_when_all_chunks_arrive_out_of_order():
    asm = EmgAssembler()
    assert asm.feed(_chunk(9, 1, 2, [(2000, 300)])) is None
    cap = asm.feed(_chunk(9, 0, 2, [(0, 100), (1000, 200)]))
    assert cap is not None
    assert cap.capture_id == 9
    assert [t for t, _ in cap.samples_mv] == [0, 1000, 2000]
    assert cap.samples_mv[2][1] == pytest.approx(raw_to_mv(300, 2048))


def test_assembler_drops_incomplete_capture_when_new_one_starts():
    asm = EmgAssembler()
    asm.feed(_chunk(1, 0, 3, [(0, 1)]))
    cap = asm.feed(_chunk(2, 0, 1, [(0, 5)]))
    assert cap is not None and cap.capture_id == 2
    assert asm.dropped_captures == 1


def test_summary_reports_peak_to_peak_and_saturation():
    asm = EmgAssembler()
    cap = asm.feed(_chunk(3, 0, 1, [(0, -16384), (1, 16384), (2, 32767)], pga_mv=2048))
    s = cap.summary()
    assert s["n"] == 3
    assert s["min_mv"] == pytest.approx(-1024.0)
    assert s["pp_mv"] == pytest.approx(raw_to_mv(32767, 2048) + 1024.0)
    assert s["saturated"] == 1


def test_write_csv_has_time_raw_and_mv(tmp_path):
    from gateway.emg_capture import write_csv

    cap = EmgAssembler().feed(_chunk(4, 0, 1, [(0, 16384), (1163, -16384)], pga_mv=2048))
    out = tmp_path / "emg.csv"
    write_csv(cap, out)
    assert out.read_text().splitlines() == ["t_us,raw,mv", "0,16384,1024.0000", "1163,-16384,-1024.0000"]


def test_cli_rejects_out_of_range_duration():
    from gateway.emg_capture import _parse_args

    with pytest.raises(SystemExit):
        _parse_args(["--ms", "5000"])
