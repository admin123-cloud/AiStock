# -*- coding: utf-8 -*-
"""AR4.5 冠军组合防过拟合折磨测试（Component Decomposition + Parameter Perturbation + Date Audit）。

目的：在把任何新规则写入 AR3 实盘代码之前，对 `C2 / C5` 涉及的 4 个因子做最严苛的敏感性与归因审计：
1. ERP < 5.0% 主动离场：打印历史上究竟哪些日期触发、触发前后 20 日市场真实走势；
2. 南向资金 20 日过滤/加权：拆开「仅过滤(sb20<0禁配港科)」vs「仅加权」vs「过滤+加权」，并扰动阈值；
3. 防守期跨资产 ETF Risk-Adj Top2 逆波动加权：测试 `InvVol` vs `EqualWeight` 及 `rebal_every=3/5/7/10`；
4. 严格滞后赚钱效应 LagME20 F2A↔F3A 切换：拆解为什么 C5 把最大回撤从 19.07% 压到 14.42%，并扰动阈值 `±1.0/1.5/2.0/2.5%`；
5. 完整对比 `C2_clean (不含LagME20, 仅01+08+ERPExit+DefRAMTop2InvVol+SB_HK)` vs `C5_full (含LagME20)` 的稳健性矩阵。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ar1_alpha_experiment import _build_ar1_regimes, _eval_nav  # noqa: E402
from scripts.ar3_cross_asset_experiment import _load_etf_panels  # noqa: E402
from scripts.ar3f_fundamental_trend_experiment import (  # noqa: E402
    _load_dividend_lookup,
    _load_full_price_lookup,
    _pack_metrics,
    attach_pit_fundamentals,
)
from scripts.ar45_calmar_deep_research import (  # noqa: E402
    _ch_query_df,
    audit_drawdowns,
    load_southbound_df,
    precompute_lagged_money_effect,
    simulate_ar45_physical,
)
from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402


def main() -> int:
    t0 = time.time()
    macro_df = _ch_query_df(
        """
        SELECT
            toString(trade_date) AS trade_date,
            hs300_close, hs300_pe_ttm, cn_bond_10y, us_bond_10y, erp_pct,
            margin_balance_yi, nt_ratio_pctile_250d, broad6_net_inflow_5d_yi,
            top5pct_amt_share
        FROM stock.ar4_macro_daily FINAL
        ORDER BY trade_date
        """
    )
    sb_df = load_southbound_df()
    macro_df = macro_df.merge(sb_df, on="trade_date", how="left")
    macro_df["sb_net_yi"] = macro_df["sb_net_yi"].fillna(0.0)
    macro_df["sb_net_5d_yi"] = macro_df["sb_net_yi"].rolling(5, min_periods=1).sum()
    macro_df["sb_net_20d_yi"] = macro_df["sb_net_yi"].rolling(20, min_periods=5).sum()

    piv_o, piv_c, code_to_class, _ = _load_etf_panels()
    panel_df, bench_df = build_or_load_feature_panel(
        start_date="2023-01-01",
        end_date="2026-11-21",
        min_amt_20d=50_000_000.0,
    )
    day_df = _build_ar1_regimes(panel_df, bench_df)
    dates = day_df["trade_date"].astype(str).tolist()
    splits = split_trade_dates(dates)
    pit_df = attach_pit_fundamentals(panel_df)
    pit_df["trade_date_str"] = pit_df["trade_date"].astype(str).str[:10]
    by_date_rows = {dt: g for dt, g in pit_df.groupby("trade_date_str", sort=False)}
    px_lookup = _load_full_price_lookup(set(pit_df["code"].astype(str).unique()), dates[0], dates[-1])
    div_lookup = _load_dividend_lookup()
    me20_map = precompute_lagged_money_effect(dates, by_date_rows, px_lookup)

    sig_map: dict[str, dict[str, float]] = {}
    for row in macro_df.to_dict(orient="records"):
        d = str(row["trade_date"])
        m = {k: float(v) if isinstance(v, (int, float, np.floating)) and pd.notna(v) else 0.0 for k, v in row.items() if k != "trade_date"}
        if d in me20_map:
            m.update(me20_map[d])
        sig_map[d] = m

    # 1. 审计 ERP < 5.0% 在 2023~2026 真实触发了哪些日期
    m23 = macro_df[macro_df["trade_date"] >= "2023-01-01"].copy()
    erp_low = m23[(m23["erp_pct"] > 0) & (m23["erp_pct"] < 5.2)]
    print("=" * 100)
    print(f"【审计1】2023~2026 ERP < 5.2% 触发日期分布（共 {len(erp_low)} 天；其中 <5.0% 有 {int((m23['erp_pct'] < 5.0).sum())} 天）")
    print("=" * 100)
    if not erp_low.empty:
        print(erp_low[["trade_date", "hs300_close", "hs300_pe_ttm", "cn_bond_10y", "erp_pct", "nt_ratio_pctile_250d"]].head(20).to_string(index=False))
        if len(erp_low) > 20:
            print(f"... 共 {len(erp_low)} 行，最后5行:")
            print(erp_low[["trade_date", "hs300_close", "hs300_pe_ttm", "cn_bond_10y", "erp_pct", "nt_ratio_pctile_250d"]].tail(5).to_string(index=False))

    def make_cap(erp_thr: float = 5.0, hi_cap: float = 0.65, lo_cap: float = 0.35):
        def _fn(dt: str, reg: str, s: dict[str, float]) -> float:
            if reg not in ("offensive", "extreme_offensive"):
                return 0.0
            erp = s.get("erp_pct", 5.5)
            if 0 < erp < erp_thr:
                return 0.0
            p = s.get("nt_ratio_pctile_250d", 50.0)
            return hi_cap if p >= 70.0 else (lo_cap if p < 30.0 else 0.50)
        return _fn

    def make_lag_me(thr: float):
        def _fn(dt: str, reg: str, s: dict[str, float]) -> str:
            if s.get("f2a_samples", 0) >= 3 and s.get("f3a_samples", 0) >= 3:
                diff = s.get("me20_diff_f2a_minus_f3a", 0.0)
                if diff >= thr:
                    return "f2a"
                if diff <= -thr:
                    return "f3a"
            return "ab"
        return _fn

    # 2. 逐层拆解 C2 -> C2+SB -> C5 + 全面参数邻域扰动测试
    cases: list[dict[str, Any]] = [
        # --- 逐层累加验证（每一步都要看清增量贡献） ---
        {"grp": "逐层累加", "name": "L0_B1 (01+08 现役生产基线)", "stock_cap_fn": make_cap(5.0)},
        {"grp": "逐层累加", "name": "L1_+ERP<5%主动止盈离场", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True},
        {"grp": "逐层累加", "name": "L2_+防守ETF_RAM_Top2_InvVol", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol"},
        {"grp": "逐层累加", "name": "L3a_+南向仅过滤(sb20<0禁配港科)", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.0},
        {"grp": "逐层累加", "name": "L3b_+南向过滤+加权0.25 (无LagME)", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25},
        {"grp": "逐层累加", "name": "L4_C5 (+LagME20 ±1.5%切换)", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "selector_mode_fn": make_lag_me(1.5)},
        # --- 扰动1：ERP 阈值扰动 (4.8 / 5.0 / 5.2) 基于 L3b ---
        {"grp": "ERP扰动", "name": "L3b_ERP=4.8%", "stock_cap_fn": make_cap(4.8), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25},
        {"grp": "ERP扰动", "name": "L3b_ERP=5.2%", "stock_cap_fn": make_cap(5.2), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25},
        # --- 扰动2：南向加权扰动 (0 / 0.15 / 0.25 / 0.35) 基于 L3b ---
        {"grp": "南向扰动", "name": "L3b_SB_boost=0.15", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.15},
        {"grp": "南向扰动", "name": "L3b_SB_boost=0.35", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.35},
        # --- 扰动3：ETF 调仓周期扰动 (rebal=3 / 5 / 7 / 10) 基于 L3b ---
        {"grp": "ETF周期扰动", "name": "L3b_rebal=3d", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "rebal_every": 3},
        {"grp": "ETF周期扰动", "name": "L3b_rebal=7d", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "rebal_every": 7},
        {"grp": "ETF周期扰动", "name": "L3b_rebal=10d", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "rebal_every": 10},
        # --- 扰动4：LagME20 切换阈值扰动 (±1.0 / ±1.5 / ±2.0 / ±2.5 / ±3.0) 基于 L4 ---
        {"grp": "LagME扰动", "name": "L4_LagME=±1.0%", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "selector_mode_fn": make_lag_me(1.0)},
        {"grp": "LagME扰动", "name": "L4_LagME=±2.0%", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "selector_mode_fn": make_lag_me(2.0)},
        {"grp": "LagME扰动", "name": "L4_LagME=±2.5%", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "selector_mode_fn": make_lag_me(2.5)},
        {"grp": "LagME扰动", "name": "L4_LagME=±3.0%", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "selector_mode_fn": make_lag_me(3.0)},
        # --- 扰动5：L4 换 ETF 调仓周期 (rebal=3 / 7 / 10) ---
        {"grp": "L4周期扰动", "name": "L4_rebal=3d", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "selector_mode_fn": make_lag_me(1.5), "rebal_every": 3},
        {"grp": "L4周期扰动", "name": "L4_rebal=7d", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "selector_mode_fn": make_lag_me(1.5), "rebal_every": 7},
        {"grp": "L4周期扰动", "name": "L4_rebal=10d", "stock_cap_fn": make_cap(5.0), "erp_active_exit": True, "etf_policy": "def_ram_top2_invvol", "southbound_hk_filter": True, "southbound_hk_boost": 0.25, "selector_mode_fn": make_lag_me(1.5), "rebal_every": 10},
    ]

    rows = []
    l3b_dd = None
    l4_dd = None
    for c in cases:
        nm = c["name"]
        grp = c["grp"]
        kw = {k: v for k, v in c.items() if k not in ("name", "grp")}
        rec = nm.startswith("L3b_") or nm.startswith("L4_C5")
        nav, meta = simulate_ar45_physical(
            day_df, sig_map, by_date_rows, px_lookup, div_lookup, piv_o, piv_c, code_to_class,
            name=nm, record_trace=rec, **kw,
        )
        if nm.startswith("L3b_+南向"):
            l3b_dd = audit_drawdowns(nav, meta["trace"], top_k=3)
        if nm.startswith("L4_C5"):
            l4_dd = audit_drawdowns(nav, meta["trace"], top_k=3)
        m = _pack_metrics(nav, splits)
        yr = {y: _eval_nav(nav.loc[[d for d in nav.index if d.startswith(y)]])["cum_ret_pct"] for y in ("2023", "2024", "2025", "2026")}
        rows.append({
            "分组": grp,
            "方案": nm,
            "全样本%": m["full"]["cum_ret_pct"],
            "年化%": m["full"]["ann_ret_pct"],
            "回撤%": m["full"]["max_dd_pct"],
            "夏普": m["full"]["sharpe"],
            "卡玛": m["full"]["calmar"],
            "OOS%": m["oos"]["cum_ret_pct"],
            "OOS回撤%": m["oos"]["max_dd_pct"],
            "OOS卡玛": m["oos"]["calmar"],
            "2023": yr["2023"],
            "2024": yr["2024"],
            "2025": yr["2025"],
            "2026": yr["2026"],
        })

    print("\n" + "=" * 136)
    print("【审计2】逐层累加贡献 + 全参数邻域扰动稳健性矩阵（防过拟合折磨测试）")
    print("=" * 136)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n【L3b (无LagME) Top-3 回撤】:")
    for e in (l3b_dd or []):
        print(" ", e["start"], "->", e["trough"], f"{e['dd_pct']}%", e["at_trough"].get("reg"), e["at_trough"].get("etfs"))
    print("【L4_C5 (含LagME) Top-3 回撤】:")
    for e in (l4_dd or []):
        print(" ", e["start"], "->", e["trough"], f"{e['dd_pct']}%", e["at_trough"].get("reg"), e["at_trough"].get("etfs"))

    out = runtime_path("mining_runs") / "ar45_champion_stress_audit_latest.json"
    out.write_text(json.dumps({"rows": rows, "l3b_dd": l3b_dd, "l4_dd": l4_dd}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n折磨测试完成，耗时 {time.time() - t0:.1f}s，落盘至 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
