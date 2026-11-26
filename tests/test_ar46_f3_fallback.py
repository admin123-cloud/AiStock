# -*- coding: utf-8 -*-
"""AR4.6 F3 分层兜底单元测试：全灭时 511260 20d>0 用国债，<=0 切现金/逆回购。"""
from __future__ import annotations

import pandas as pd

from services.mining import ar3_dual_core_live as live


def _make_etf_df(bond_prices: list[float], div_prices: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range("2026-10-01", periods=len(bond_prices)).strftime("%Y-%m-%d").tolist()
    rows = []
    for dt, bp, dp in zip(dates, bond_prices, div_prices):
        rows.append({
            "code": "511260.SH",
            "name": "十年国债ETF",
            "asset_class": "bond",
            "trade_date": dt,
            "open": bp,
            "close": bp,
        })
        rows.append({
            "code": "512890.SH",
            "name": "红利低波ETF",
            "asset_class": "cn_dividend",
            "trade_date": dt,
            "open": dp,
            "close": dp,
        })
    return pd.DataFrame(rows)


def test_f3_fallback_bond_when_511260_ret20_positive(monkeypatch) -> None:
    """全池均破 MA20，但 511260 20 日动量仍 > 0 -> 维持十年国债兜底。"""
    # 前 20 天稳步上涨到 137，最后 2 天小幅回踩到 135.8（低于 MA20 但仍高于 20 天前 134.0）
    bond_px = [134.0 + i * 0.15 for i in range(23)] + [135.8, 135.7]
    # 红利低波一路下跌，破 MA20 且 ret20 < 0
    div_px = [1.35 - i * 0.005 for i in range(25)]
    df = _make_etf_df(bond_px, div_px)
    monkeypatch.setattr(live, "_ch_query_df", lambda *a, **kw: df.copy())

    res = live._build_etf_radar(
        regime="defensive",
        etf_core_mode="regime_focus",
        stock_budget_cap_pct=0,
    )
    assert res["available"] is True
    assert res["fallback_mode"] == "bond_fallback"
    assert res["cash_defense_pct"] == 0
    assert len(res["targets"]) == 1
    assert res["targets"][0]["ts_code"] == "511260.SH"


def test_f3_fallback_cash_when_511260_ret20_non_positive(monkeypatch) -> None:
    """全池破 MA20 且 511260 20 日动量 <= 0 -> 触发 F3 现金/逆回购避险，不推破位国债。"""
    bond_px = [137.0 - i * 0.08 for i in range(25)]
    div_px = [1.35 - i * 0.005 for i in range(25)]
    df = _make_etf_df(bond_px, div_px)
    monkeypatch.setattr(live, "_ch_query_df", lambda *a, **kw: df.copy())

    res = live._build_etf_radar(
        regime="defensive",
        etf_core_mode="regime_focus",
        stock_budget_cap_pct=0,
    )
    assert res["available"] is True
    assert res["fallback_mode"] == "cash_defense"
    assert res["cash_defense_pct"] == 100
    assert res["targets"] == []
    assert "F3 分层避险" in res["headline"]


def test_f3_fallback_cash_in_panic_mode_keeps_50pct_budget(monkeypatch) -> None:
    """冰点 30m 触发但全部避险 ETF + 国债均走弱 -> 50% 避险预算保持现金。"""
    bond_px = [137.0 - i * 0.08 for i in range(25)]
    div_px = [1.35 - i * 0.005 for i in range(25)]
    df = _make_etf_df(bond_px, div_px)
    monkeypatch.setattr(live, "_ch_query_df", lambda *a, **kw: df.copy())

    res = live._build_etf_radar(
        regime="defensive",
        etf_core_mode="regime_focus",
        stock_budget_cap_pct=0,
        panic_signal={"triggered": True},
    )
    assert res["available"] is True
    assert res["fallback_mode"] == "cash_defense"
    assert res["cash_defense_pct"] == 50
    assert res["targets"] == []
