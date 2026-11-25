# -*- coding: utf-8 -*-
"""数据中心统一健康服务单测（覆盖 G3 核心跨资产 ETF / 国债 / 宏观 / 南向 / 国家队份额与 ETF 单标的体检）。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.ops import data_center_service as dcs  # noqa: E402


def test_normalize_code_handles_sh_etf_and_bond_etf():
    assert dcs._normalize_code("510880") == "510880.SH"
    assert dcs._normalize_code("511260") == "511260.SH"
    assert dcs._normalize_code("511090") == "511090.SH"
    assert dcs._normalize_code("588000") == "588000.SH"
    assert dcs._normalize_code("159915") == "159915.SZ"
    assert dcs._normalize_code("002015") == "002015.SZ"
    assert dcs._normalize_code("603881") == "603881.SH"
    assert dcs._normalize_code("899050") == "899050.BJ"


def test_health_matrix_includes_g3_strategy_datasets_and_missing_items(monkeypatch):
    dates = [("2026-11-24",), ("2026-11-23",)]
    core_etfs = sorted(dcs.CORE_CROSS_ASSET_ETFS.keys())
    nt_etfs = sorted(dcs.AR4_NATIONAL_TEAM_ETFS.keys())
    # 模拟 11-24 缺失 510300.SH 与 511260.SH（国债ETF）
    present_1124 = [c for c in core_etfs if c not in ("510300.SH", "511260.SH")]

    def fake_query(sql: str, params=None):
        s = " ".join(sql.split())
        if "FROM stock.trade_calendar" in s:
            return dates
        if "FROM stock.kline_daily" in s and "IN (SELECT code FROM stock.stocks" in s:
            return [("2026-11-23", 5560), ("2026-11-24", 5565)]
        if "FROM stock.kline_minute_" in s and "IN (SELECT code FROM stock.stocks" in s:
            return [("2026-11-23", 5560), ("2026-11-24", 5565)]
        if "FROM stock.stocks FINAL WHERE type = 'stock'" in s:
            return [(5570,)]
        if "FROM stock.kline_minute_" in s:
            return [("2026-11-23", list(dcs.CORE_INDICES)), ("2026-11-24", list(dcs.CORE_INDICES))]
        if "FROM stock.kline_daily" in s:
            return [("2026-11-23", list(dcs.CORE_INDICES)), ("2026-11-24", list(dcs.CORE_INDICES))]
        if "FROM stock.sectors FINAL" in s:
            return [(604,)]
        if "FROM stock.sector_kline_daily" in s:
            return [("2026-11-23", 595), ("2026-11-24", 595)]
        if "SELECT code, any(name), any(asset_class) FROM stock.etf_kline_daily" in s:
            return [(c, n, "bond" if c.startswith("511") else "cn_broad") for c, n in dcs.CORE_CROSS_ASSET_ETFS.items()]
        if "FROM stock.etf_kline_daily FINAL" in s and "asset_class IN" in s:
            return [("2026-11-23", core_etfs), ("2026-11-24", present_1124)]
        if "FROM stock.etf_kline_daily FINAL" in s:
            return [("2026-11-23", core_etfs), ("2026-11-24", present_1124)]
        if "FROM stock.ar4_etf_share_daily FINAL" in s:
            return [("2026-11-23", nt_etfs)]
        if "FROM stock.ar4_macro_daily FINAL" in s:
            return [("2026-11-23", 1), ("2026-11-24", 1)]
        if "FROM stock.ar4_southbound_daily FINAL" in s:
            return [("2026-11-23", 1), ("2026-11-24", 1)]
        raise AssertionError(f"unexpected SQL: {s}")

    monkeypatch.setattr(dcs, "_query", fake_query)
    dcs.invalidate_data_center_cache()
    mx = dcs.get_health_matrix(days=5, force_refresh=True)
    ds_map = {d["id"]: d for d in mx["datasets"]}

    for req_id in ("etf_core_daily", "etf_all_daily", "ar4_etf_share_daily", "ar4_macro_daily", "ar4_southbound_daily"):
        assert req_id in ds_map
        assert ds_map[req_id]["group"] == "g3_strategy"

    core_1124 = next(c for c in ds_map["etf_core_daily"]["cells"] if c["date"] == "2026-11-24")
    assert core_1124["status"] == "missing"
    assert core_1124["actual"] == 12
    assert core_1124["expected"] == 14
    assert "510300.SH 沪深300ETF" in core_1124["missing_items"]
    assert "511260.SH 十年国债ETF" in core_1124["missing_items"]

    # Runtime summary 必须捕捉到核心 ETF 与国家队份额滞后，绝不报假全绿
    summary = dcs.get_runtime_health_summary()
    assert summary["strategy_actionable"] is False
    bad_names = {c["name"] for c in summary["components"] if c["status"] != "healthy"}
    assert "etf_core_daily" in bad_names
    assert "ar4_etf_share_daily" in bad_names


def test_inspect_kline_supports_bond_etf_without_false_minute_missing(monkeypatch):
    def fake_query(sql: str, params=None):
        s = " ".join(sql.split())
        if "FROM stock.stocks FINAL WHERE code" in s:
            return []
        if "SELECT anyLast(name), anyLast(asset_class), count() FROM stock.etf_kline_daily" in s:
            return [("十年国债ETF", "bond", 1089)]
        if "FROM stock.trade_calendar" in s:
            return [("2026-11-24",), ("2026-11-23",)]
        if "SELECT uniqExact(trade_date)" in s and "etf_kline_daily" in s:
            return [(1089, "2022-06-01", "2026-11-24")]
        if "GROUP BY d" in s and "etf_kline_daily" in s:
            return [("2026-11-23", 1), ("2026-11-24", 1)]
        if "SELECT uniqExact(trade_date)" in s and "ar4_etf_share_daily" in s:
            return [(1426, "2021-01-04", "2026-11-24")]
        if "GROUP BY d" in s and "ar4_etf_share_daily" in s:
            return [("2026-11-23", 1), ("2026-11-24", 1)]
        raise AssertionError(f"unexpected SQL: {s}")

    monkeypatch.setattr(dcs, "_query", fake_query)
    res = dcs.inspect_kline("511260", days=5)
    assert res["code"] == "511260.SH"
    assert res["name"] == "十年国债ETF"
    assert res["is_etf"] is True
    assert res["has_share"] is True
    assert "利率债" in res["type"]
    assert all(p["complete"] for p in res["periods"])
    assert all(r["all_ok"] for r in res["daily_rows"])
