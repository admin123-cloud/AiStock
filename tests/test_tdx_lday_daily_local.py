# -*- coding: utf-8 -*-
"""B 部分（Owner 2026-11-13 裁决：日线走 TDX 客户端本地 lday）单元门禁。

覆盖：
1. lday 解析（构造样本字节 → 记录往返）
2. 时序门禁拒绝（mtime < 16:00 / 无当日记录 → 不产行；fresh 体量不足 → 拒写）
3. 幂等重写（--source local --formal-write 跑两遍 → 第二遍 0 缺失键、不重复插）
"""

from __future__ import annotations

import json
import os
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import daily_kline_pipeline as pipeline
from scripts import tdx_lday_loader as lday
from tests.test_daily_kline_pipeline import FakeCHClient

TRADE_DATE = "2026-11-13"
TRADE_YMD = 20261113
PREV_DATE = "2026-11-12"
PREV_YMD = 20261112
BEIJING = timezone(timedelta(hours=8))


# ── 构造样本字节 ─────────────────────────────────────────────────────

def pack_lday(records) -> bytes:
    """records: (ymd, open, high, low, close, amount, vol[, reserved]) → lday 字节。"""
    out = bytearray()
    for rec in records:
        ymd, o, h, l, c, amount, vol = rec[:7]
        reserved = rec[7] if len(rec) > 7 else 0
        out += struct.pack("<Iiiiifii", ymd, round(o * 100), round(h * 100),
                           round(l * 100), round(c * 100), amount, vol, reserved)
    return bytes(out)


def write_lday_file(vipdoc: Path, name: str, records, *, mtime: datetime) -> Path:
    market = name[:2]
    directory = vipdoc / market / "lday"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.day"
    path.write_bytes(pack_lday(records))
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def fresh_time(hour=16, minute=30) -> datetime:
    day = datetime.strptime(TRADE_DATE, "%Y-%m-%d")
    return day.replace(hour=hour, minute=minute, tzinfo=BEIJING)


def stale_time() -> datetime:
    return fresh_time(15, 0)


# 样本行情（vwap=amount/vol 须落回 [low, high]：量纲=股）
SH600000 = (TRADE_YMD, 10.19, 10.44, 10.12, 10.40, 786507840.0, 76157312)
SH600000_PREV = (PREV_YMD, 10.10, 10.30, 10.05, 10.15, 508447360.0, 50081746)
SZ000001 = (TRADE_YMD, 11.76, 12.10, 11.70, 12.07, 2624482048.0, 218942992)
SH688111 = (TRADE_YMD, 217.11, 219.78, 216.30, 217.21, 774995776.0, 3558749)
SH000001_IDX = (TRADE_YMD, 3658.72, 3669.44, 3623.84, 3634.35, 770614165504.0, 472293657)


def build_vipdoc(tmp_path: Path) -> Path:
    """构造 vipdoc 树：3 fresh 可写 + 1 stale + 1 无当日记录 + 2 宇宙外。"""
    vipdoc = tmp_path / "vipdoc"
    write_lday_file(vipdoc, "sh600000", [SH600000_PREV, SH600000], mtime=fresh_time())
    write_lday_file(vipdoc, "sz000001", [SZ000001], mtime=fresh_time(16, 1))
    write_lday_file(vipdoc, "sh688111", [SH688111], mtime=fresh_time(17, 2))
    write_lday_file(vipdoc, "sh000001", [SH000001_IDX], mtime=fresh_time(16, 3))
    # 有当日记录但 mtime 15:00（盘中/未完成下载证据）→ 时序门禁逐文件拒绝
    write_lday_file(vipdoc, "sz300750",
                    [(TRADE_YMD, 260.0, 261.0, 259.0, 260.5, 2600000000.0, 10000000)],
                    mtime=stale_time())
    # 只有昨日记录（停牌）→ 无当日行
    write_lday_file(vipdoc, "sh601398", [(PREV_YMD, 7.0, 7.1, 6.9, 7.05, 70000000.0, 9000000)],
                    mtime=fresh_time())
    # 宇宙外：ETF / 非冻结指数
    write_lday_file(vipdoc, "sh510300",
                    [(TRADE_YMD, 4.0, 4.1, 3.9, 4.05, 40000000.0, 9000000)],
                    mtime=fresh_time())
    write_lday_file(vipdoc, "sz399005",
                    [(TRADE_YMD, 6000.0, 6100.0, 5900.0, 6050.0, 60000000000.0, 400000000)],
                    mtime=fresh_time())
    # lc5 覆盖对齐用（同名宇宙文件）
    for market, code in (("sh", "sh600000"), ("sz", "sz000001"), ("sh", "sh688111"),
                         ("sh", "sh000001"), ("sh", "sh601398")):
        fz = vipdoc / market / "fzline"
        fz.mkdir(parents=True, exist_ok=True)
        (fz / f"{code}.lc5").write_bytes(b"\0" * 32)
    return vipdoc


# ── 1) lday 解析：构造样本字节往返 ──────────────────────────────────

def test_parse_lday_bytes_roundtrip():
    blob = pack_lday([SH600000_PREV, SH600000, SH000001_IDX])
    records = lday.parse_lday_bytes(blob)
    assert len(records) == 3
    today = records[-1]
    assert today["date"] == TRADE_YMD
    assert today["open"] == pytest.approx(3658.72)
    assert today["high"] == pytest.approx(3669.44)
    assert today["low"] == pytest.approx(3623.84)
    assert today["close"] == pytest.approx(3634.35)
    assert today["amount"] == pytest.approx(770614165504.0, rel=1e-6)   # float32 精度
    assert today["vol"] == 472293657


def test_parse_lday_bytes_rejects_bad_length():
    with pytest.raises(ValueError, match="32 的倍数"):
        lday.parse_lday_bytes(b"\0" * 33)


def test_read_day_file_prev_close():
    # 直接用临时文件验证 prev_close 取前一条 close
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = write_lday_file(Path(td), "sh600000", [SH600000_PREV, SH600000],
                               mtime=fresh_time())
        rec = lday.read_day_file(path, TRADE_DATE)
        assert rec is not None
        assert rec["prev_close"] == pytest.approx(10.15)
        assert lday.read_day_file(path, "2026-11-11") is None   # 无该日记录 → None


# ── 2) 时序门禁拒绝 ─────────────────────────────────────────────────

def test_scan_excludes_stale_and_no_today(tmp_path):
    vipdoc = build_vipdoc(tmp_path)
    scan = lday.scan_lday_universe(vipdoc, TRADE_DATE)
    rows = {row["code"]: row for row in scan["rows"]}
    # 只有 fresh 且含当日记录的宇宙文件产行
    assert set(rows) == {"600000.SH", "000001.SZ", "688111.SH", "000001.SH"}
    timing = scan["timing"]
    assert timing["stale_mtime_excluded"] == 1          # sz300750：15:00 mtime 被拒
    assert timing["no_today_record"] == 1               # sh601398：停牌无当日行
    assert timing["excluded_non_universe"] == 2         # ETF + 非冻结指数
    assert timing["fresh"] == 4
    # 量纲：个股 股÷100→手；冻结指数原值
    assert rows["600000.SH"]["volume"] == pytest.approx(761573.12)
    assert rows["600000.SH"]["volume_raw_unit"] == "shares"
    assert rows["000001.SH"]["volume"] == pytest.approx(472293657.0)
    assert rows["000001.SH"]["volume_raw_unit"] == "lots"
    assert rows["000001.SH"]["is_index"] is True


def test_pipeline_timing_gate_rejects_write(tmp_path):
    vipdoc = build_vipdoc(tmp_path)
    client = FakeCHClient(calendar_sessions=[PREV_DATE, TRADE_DATE],
                          formal_latest=PREV_DATE,
                          prev_day_codes=["600000.SH", "688111.SH", "000001.SZ"])
    result = pipeline.run_pipeline({
        "source": "local", "trade_date": TRADE_DATE, "tdx_root": str(vipdoc),
        "min_fresh_files": 10,                        # fresh=4 < 10 → 体量证据不足
        "crosscheck_sample": 0, "formal_write": True,
        "output": tmp_path / "r.json", "runtime_dir": tmp_path / "rt",
        "client_factory": lambda: client,
    })
    report = result["report"]
    assert result["exit_code"] == 1
    assert report["gates"]["lday_timing"]["status"] == "fail"
    assert "fail-closed 拒绝写入" in report["gates"]["lday_timing"]["reason"]
    assert report["formal_write"]["performed"] is False
    assert client.formal_rows == {}                   # 一行都没写
    assert client.inserts == []


# ── 3) 幂等重写 ─────────────────────────────────────────────────────

def _local_options(tmp_path, vipdoc, client, **extra):
    options = {
        "source": "local", "trade_date": TRADE_DATE, "tdx_root": str(vipdoc),
        "min_fresh_files": 1, "coverage_min": 1,
        "crosscheck_sample": 0, "formal_write": True,
        "output": tmp_path / "r1.json", "runtime_dir": tmp_path / "rt",
        "client_factory": lambda: client,
    }
    options.update(extra)
    return options


def test_local_formal_write_then_idempotent_rerun(tmp_path):
    vipdoc = build_vipdoc(tmp_path)
    client = FakeCHClient(calendar_sessions=[PREV_DATE, TRADE_DATE],
                          formal_latest=PREV_DATE,
                          prev_day_codes=["600000.SH", "688111.SH", "000001.SZ",
                                          "000001.SH"])
    first = pipeline.run_pipeline(_local_options(tmp_path, vipdoc, client))
    assert first["exit_code"] == 0, first["report"]["failed_gates"]
    fw1 = first["report"]["formal_write"]
    assert fw1["performed"] is True
    assert fw1["inserted_rows"] == 4                       # 4 行全新写入
    assert fw1["readback"]["ok"] and fw1["readback"]["row_count"] == 4
    assert first["report"]["gates"]["postwrite_coverage"]["status"] == "pass"
    written_ids = {row["id"] for row in client.formal_rows.values()}

    # 第二遍：重叠一致性 + 反连接 → 0 缺失键 → 不插任何行（幂等）
    second = pipeline.run_pipeline(
        _local_options(tmp_path, vipdoc, client, output=tmp_path / "r2.json"))
    assert second["exit_code"] == 0, second["report"]["failed_gates"]
    fw2 = second["report"]["formal_write"]
    assert fw2["performed"] is True
    assert fw2["inserted_rows"] == 0
    assert fw2["missing_key_count"] == 0
    assert fw2["overlap_key_count"] == 4
    assert fw2["readback"]["ok"] and fw2["readback"]["row_count"] == 4
    # 行集与 id 完全不变（exact-key 幂等）
    assert len(client.formal_rows) == 4
    assert {row["id"] for row in client.formal_rows.values()} == written_ids
    # B5 对照报告与 B4 覆盖缺口报告落盘
    rt = tmp_path / "rt"
    assert (rt / "local_vs_pkg_20261113.json").is_file()
    coverage = json.loads((rt / "coverage_gap_20261113.json").read_text(encoding="utf-8"))
    assert coverage["postwrite_row_count"] == 4
    assert "601398.SH" in coverage["lc5_not_in_daily"]      # 停牌缺口如实报告


def test_local_vwap_gate_fails_on_wrong_unit(tmp_path):
    # 量纲陷阱样本：vol 若按「手」解释 vwap 会出界 → 门禁必须 fail（不得猜换算）
    vipdoc = tmp_path / "vipdoc"
    bad = (TRADE_YMD, 10.0, 10.5, 9.8, 10.2, 1000000.0, 1000)   # vwap_shares=1000 出界
    write_lday_file(vipdoc, "sh600000", [bad], mtime=fresh_time())
    client = FakeCHClient(calendar_sessions=[PREV_DATE, TRADE_DATE],
                          formal_latest=PREV_DATE, prev_day_codes=["600000.SH"])
    result = pipeline.run_pipeline(_local_options(tmp_path, vipdoc, client))
    assert result["exit_code"] == 1
    assert result["report"]["gates"]["lday_vwap_unit"]["status"] == "fail"
    assert result["report"]["formal_write"]["performed"] is False
    assert client.inserts == []
