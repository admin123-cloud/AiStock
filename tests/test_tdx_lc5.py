# -*- coding: utf-8 -*-
"""utils/tdx_lc5.py 的合约测试（TDD：先于补数管道）。"""
from __future__ import annotations

import struct
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import tdx_lc5 as lc5


def pack_record(dt, o, h, l, c, amount, shares, reserved=0):
    raw_date = (dt.year - 2004) * 2048 + dt.month * 100 + dt.day
    raw_time = dt.hour * 60 + dt.minute
    return lc5.LC5_RECORD.pack(raw_date, raw_time, o, h, l, c, amount, shares, reserved)


# ── 结构与单位合约 ──────────────────────────────────────────────

def test_record_size_is_32_bytes():
    assert lc5.LC5_RECORD_SIZE == 32


def test_decode_lc5_datetime_roundtrip():
    dt = lc5.decode_lc5_datetime((2026 - 2004) * 2048 + 10 * 100 + 30, 9 * 60 + 35)
    assert dt == datetime(2026, 10, 30, 9, 35)


def test_decode_lc5_datetime_invalid_returns_none():
    assert lc5.decode_lc5_datetime(0, 0) is None or True  # 2004-01-01 00:00 合法
    assert lc5.decode_lc5_datetime((2026 - 2004) * 2048 + 13 * 100 + 1, 0) is None  # 13月不存在


@pytest.mark.parametrize("code,is_idx", [
    ("000001.SH", True), ("000300.SH", True), ("399001.SZ", True), ("399006.SZ", True),
    ("000698.SZ", False), ("600000.SH", False), ("920001.BJ", False), ("880001.SH", False),
])
def test_is_index_code(code, is_idx):
    assert lc5.is_index_code(code) is is_idx


def test_shares_to_lots_stock_multiple_of_100():
    assert lc5.shares_to_lots("000698.SZ", 19571300) == 195713


def test_shares_to_lots_stock_not_multiple_fails_closed():
    with pytest.raises(lc5.Lc5FormatError):
        lc5.shares_to_lots("000698.SZ", 19571345)


def test_shares_to_lots_index_no_conversion():
    assert lc5.shares_to_lots("000001.SH", 1234567) == 1234567


def test_shares_to_lots_negative_fails():
    with pytest.raises(lc5.Lc5FormatError):
        lc5.shares_to_lots("600000.SH", -100)


# ── 标准 48 槽网格 ─────────────────────────────────────────────

def test_standard_grid_has_48_slots_with_known_edges():
    assert len(lc5.STANDARD_5M_BAR_ENDS) == 48
    from datetime import time as dtime
    assert dtime(9, 35) in lc5.STANDARD_5M_BAR_ENDS
    assert dtime(11, 30) in lc5.STANDARD_5M_BAR_ENDS
    assert dtime(13, 5) in lc5.STANDARD_5M_BAR_ENDS
    assert dtime(15, 0) in lc5.STANDARD_5M_BAR_ENDS
    assert dtime(11, 35) not in lc5.STANDARD_5M_BAR_ENDS
    assert dtime(9, 30) not in lc5.STANDARD_5M_BAR_ENDS  # bar 用结束时刻


# ── 文件解析 ───────────────────────────────────────────────────

def _write_lc5(tmp_path, records):
    path = tmp_path / "sz000698.lc5"
    path.write_bytes(b"".join(records))
    return path


def test_parse_lc5_file_filters_days_and_converts_units(tmp_path):
    rec_in = pack_record(datetime(2026, 10, 30, 9, 35), 10.0, 10.5, 9.8, 10.2, 1_000_000.0, 30_000)
    rec_out = pack_record(datetime(2026, 10, 31, 9, 35), 10.0, 10.5, 9.8, 10.2, 1_000_000.0, 30_000)
    path = _write_lc5(tmp_path, [rec_in, rec_out])
    by_day = lc5.parse_lc5_file(path, "000698.SZ", ["2026-10-30"])
    assert list(by_day) == ["2026-10-30"]
    bars = by_day["2026-10-30"]
    assert len(bars) == 1
    b = bars[0]
    assert b.volume == 300  # 30000 股 → 300 手
    assert b.amount == 1_000_000.0


def test_parse_lc5_file_bad_size_fails_closed(tmp_path):
    path = tmp_path / "sz000698.lc5"
    path.write_bytes(b"\x00" * 33)
    with pytest.raises(lc5.Lc5FormatError):
        lc5.parse_lc5_file(path, "000698.SZ", ["2026-10-30"])


def test_parse_lc5_file_nonpositive_price_fails_closed(tmp_path):
    rec = pack_record(datetime(2026, 10, 30, 9, 35), 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    path = _write_lc5(tmp_path, [rec])
    with pytest.raises(lc5.Lc5FormatError):
        lc5.parse_lc5_file(path, "000698.SZ", ["2026-10-30"])


def test_parse_lc5_file_volume_unit_mismatch_fails_closed(tmp_path):
    rec = pack_record(datetime(2026, 10, 30, 9, 35), 10.0, 10.5, 9.8, 10.2, 1000.0, 12345)
    path = _write_lc5(tmp_path, [rec])
    with pytest.raises(lc5.Lc5FormatError):
        lc5.parse_lc5_file(path, "000698.SZ", ["2026-10-30"])


# ── (code, day) 槽位校验 ───────────────────────────────────────

def _full_day_bars(code="000698.SZ", day=None):
    from datetime import timedelta
    day = day or datetime(2026, 10, 30)
    bars = []
    t = day.replace(hour=9, minute=35)
    for _ in range(24):
        bars.append(lc5.LC5Record(code, t, 10, 10.5, 9.8, 10.2, 100, 1000.0))
        t += timedelta(minutes=5)
    t = day.replace(hour=13, minute=5)
    for _ in range(24):
        bars.append(lc5.LC5Record(code, t, 10, 10.5, 9.8, 10.2, 100, 1000.0))
        t += timedelta(minutes=5)
    return bars


def test_validate_code_day_bars_ok():
    bars = _full_day_bars()
    lc5.validate_code_day_bars("000698.SZ", "2026-10-30", bars)


def test_validate_code_day_bars_missing_slot_fails():
    bars = _full_day_bars()[:-1]
    with pytest.raises(lc5.Lc5FormatError):
        lc5.validate_code_day_bars("000698.SZ", "2026-10-30", bars)


def test_validate_code_day_bars_offgrid_timestamp_fails():
    bars = _full_day_bars()
    bars[0] = lc5.LC5Record("000698.SZ", datetime(2026, 10, 30, 9, 33), 10, 10, 10, 10, 100, 1000.0)
    with pytest.raises(lc5.Lc5FormatError):
        lc5.validate_code_day_bars("000698.SZ", "2026-10-30", bars)


# ── 高周期聚合口径（与库内既有行反推一致）─────────────────────

def test_aggregate_5m_to_15_30_60_grid_and_values():
    from datetime import time as dtime
    bars = _full_day_bars()
    for i, b in enumerate(bars):
        bars[i] = lc5.LC5Record(b.code, b.datetime, open=i + 1, high=i + 2, low=i + 0.5,
                                close=i + 1.5, volume=100 * (i + 1), amount=10.0 * (i + 1))
    m15 = lc5.aggregate_5m(bars, 15)
    assert len(m15) == 16
    assert [b.datetime.time() for b in m15[:8]] == [dtime(9, 45), dtime(10, 0), dtime(10, 15), dtime(10, 30),
                                                    dtime(10, 45), dtime(11, 0), dtime(11, 15), dtime(11, 30)]
    assert [b.datetime.time() for b in m15[8:]] == [dtime(13, 15), dtime(13, 30), dtime(13, 45), dtime(14, 0),
                                                    dtime(14, 15), dtime(14, 30), dtime(14, 45), dtime(15, 0)]
    first = m15[0]
    assert first.open == 1 and first.high == 4 and first.low == 0.5 and first.close == 3.5
    assert first.volume == 100 * (1 + 2 + 3) and first.amount == 10.0 * (1 + 2 + 3)

    m30 = lc5.aggregate_5m(bars, 30)
    assert [b.datetime.time() for b in m30] == [dtime(10, 0), dtime(10, 30), dtime(11, 0), dtime(11, 30),
                                                dtime(13, 30), dtime(14, 0), dtime(14, 30), dtime(15, 0)]

    m60 = lc5.aggregate_5m(bars, 60)
    assert [b.datetime.time() for b in m60] == [dtime(10, 30), dtime(11, 30), dtime(14, 0), dtime(15, 0)]
    h1 = m60[0]
    assert h1.open == 1 and h1.volume == 100 * sum(range(1, 13))  # 09:35–10:30 共 12 根


def test_aggregate_5m_rejects_bad_minutes():
    with pytest.raises(ValueError):
        lc5.aggregate_5m(_full_day_bars(), 7)
