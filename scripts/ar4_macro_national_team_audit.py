# -*- coding: utf-8 -*-
"""AR4 宏观资金面 × 国家队持仓代理 × 股债性价比 ERP × 两融杠杆 实证回测验真脚本。

严格防幻觉验真内容：
1. 数据审计：抽检 2021~2026 关键节点（2022熊市、2024-02 汇金救市、2024-09 924反转、2025-04 关税冲击、2026-11 本周），
   确认 300ETF 份额异动是真实申购而非拆分/脏数据；
2. 独立复现验证（验证网传“国家队持仓250日分位高位买入并持有一年超额10%”结论）：
   - 分别计算不同 250日分位区间（0~20%、20~50%、50~80%、80~100%）买入沪深300未来 20d/60d/120d/250d 的真实收益与超额；
   - 检验「原版250日分位」vs「去趋势纯份额5日/20日净申购脉冲」vs「股债性价比 ERP」vs「两融/拥挤度」的前瞻预测力（IC / 胜率 / 收益差）；
3. AR3 v3.0 物理单账户零杠杆消融对照实验（2022-05 ~ 2026-11 全样本 + 2025~2026 样本外）：
   - Baseline: 现役 AR3 v3.0（50% AB混合 F3A+F2A + #01 RegimeFocus_Top1 + AR2_2A）
   - Exp 1: + 国家队250日分位动态仓位调节（高位加仓个股至 60% / 低位降仓至 35%）
   - Exp 2: + 汇金救市脉冲（5日宽基净申购 >= 200亿）强化 AR2_2A 冰点反抽（放宽右侧门槛或提仓至 80%）
   - Exp 3: + 汇金救市脉冲独立作为「政策底大底反抽触发器」（不拘泥于宽度<=18%，只要防守态+汇金5日净买>=300亿+右侧阳线即反抽）
   - Exp 4: + 股债性价比 ERP 高估/低估仓位缩放（ERP >= 6.0% 进攻加仓至 60%；ERP <= 4.5% 降仓至 30%）
   - Exp 5: + 抱团拥挤度顶部风控（Top5% 成交占比极端过热时收紧追踪止盈或降股仓）
   - Exp 6: + 两融过热减速门禁
   - Exp 7: 择优共振组合（仅叠加消融中真正有效的正向信号）
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ar1_alpha_experiment import BUY_COST, SELL_COST, _build_ar1_regimes, _eval_nav  # noqa: E402
from scripts.ar3_cross_asset_experiment import ETF_ONE_WAY_COST, SAFE_BOND_CODE, _load_etf_panels  # noqa: E402
from scripts.ar3f_fundamental_trend_experiment import (  # noqa: E402
    StockPos,
    _load_dividend_lookup,
    _load_full_price_lookup,
    _pack_metrics,
    attach_pit_fundamentals,
)
from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402


def _sel_f2a_canslim_core(df: pd.DataFrame) -> pd.DataFrame:
    m = (
        (~df["has_earnings_mine"])
        & (df["pit_days_since_notice"] <= 185)
        & (df["net_profit_yoy_pct"] >= 30.0)
        & (df["revenue_yoy_pct"] >= 15.0)
        & (df["roe_pct"] >= 6.0)
        & (df["net_profit"] >= 50_000_000.0)
        & (df["amt_20d_yi"] >= 2.0)
        & (df["above_ma20"] > 0)
        & (df["ma_bull_align"] > 0)
        & (df["ret_20d_pct"].between(3.0, 35.0))
    )
    sub = df.loc[m].copy()
    if sub.empty:
        return sub
    sub["score"] = 0.55 * sub["rs20_rank"].fillna(0.5) + 0.45 * sub["np_yoy_rank"].fillna(0.5)
    return sub.sort_values("score", ascending=False)


def _sel_f3a_growth_pullback(df: pd.DataFrame) -> pd.DataFrame:
    m = (
        (~df["has_earnings_mine"])
        & (df["pit_days_since_notice"] <= 185)
        & (df["net_profit_yoy_pct"] >= 30.0)
        & (df["revenue_yoy_pct"] >= 15.0)
        & (df["roe_pct"] >= 8.0)
        & (df["net_profit"] >= 50_000_000.0)
        & (df["amt_20d_yi"] >= 2.0)
        & (df["ma20_bias_pct"] < df["ma60_bias_pct"])
        & (df["ma10_bias_pct"].between(-3.0, 2.0))
        & (df["ret_1d_pct"] > 0.5)
        & (df["vol_ratio_20d"] <= 1.3)
    )
    sub = df.loc[m].copy()
    if sub.empty:
        return sub
    sub["score"] = 0.5 * sub["np_yoy_rank"].fillna(0.5) + 0.5 * sub["rs20_rank"].fillna(0.5)
    return sub.sort_values("score", ascending=False)


def _sel_ab_blend(df: pd.DataFrame) -> pd.DataFrame:
    a = _sel_f3a_growth_pullback(df)
    b = _sel_f2a_canslim_core(df)
    if a.empty:
        return b
    if b.empty:
        return a
    rows = []
    seen: set[str] = set()
    ia = a.reset_index(drop=True)
    ib = b.reset_index(drop=True)
    for k in range(max(len(ia), len(ib))):
        for src in (ia, ib):
            if k < len(src):
                c = str(src.loc[k, "code"])
                if c not in seen:
                    seen.add(c)
                    rows.append(src.loc[k])
    return pd.DataFrame(rows) if rows else a.iloc[0:0]


def _ch_query_df(sql: str, timeout: int = 60) -> pd.DataFrame:
    req = urllib.request.Request(
        "http://127.0.0.1:8123/",
        data=(sql.strip() + "\nFORMAT Parquet").encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return pq.read_table(io.BytesIO(r.read())).to_pandas()


def audit_and_test_standalone_signals(macro: pd.DataFrame) -> dict[str, Any]:
    """独立检验国家队持仓分位、净申购脉冲、ERP、两融与拥挤度对沪深300未来收益的真实预测力。"""
    df = macro.copy().sort_values("trade_date").reset_index(drop=True)
    # 未来 20d / 60d / 120d / 250d 沪深300 真实收益（T日收盘买 -> T+H日收盘卖）
    for h in (20, 60, 120, 250):
        df[f"fwd_{h}d"] = (df["hs300_close"].shift(-h) / df["hs300_close"] - 1.0) * 100.0

    # 1. 复现网传：按国家队 250 日分位分组（2022-01 之后，确保 250 日窗口已满）
    sub = df[df["trade_date"] >= "2022-01-01"].copy()
    sub["nt_bucket"] = pd.cut(
        sub["nt_ratio_pctile_250d"],
        bins=[-1, 20, 50, 80, 101],
        labels=["低位(0~20%)", "中低(20~50%)", "中高(50~80%)", "高位(80~100%)"],
    )
    nt_table = sub.groupby("nt_bucket", observed=False)[["fwd_20d", "fwd_60d", "fwd_120d", "fwd_250d"]].agg(["count", "mean"])

    # 2. 按年份拆解「高位(>=80%) vs 低位(<50%)」的 60d / 250d 表现（防单一年份幸存者偏差）
    sub["year"] = sub["trade_date"].str[:4]
    yearly_nt = []
    for yr, g in sub.groupby("year"):
        hi = g[g["nt_ratio_pctile_250d"] >= 80.0]
        lo = g[g["nt_ratio_pctile_250d"] < 50.0]
        yearly_nt.append({
            "year": yr,
            "days_total": int(len(g)),
            "days_hi80": int(len(hi)),
            "hi80_fwd60": round(float(hi["fwd_60d"].mean()), 2) if len(hi) and hi["fwd_60d"].notna().any() else None,
            "hi80_fwd250": round(float(hi["fwd_250d"].mean()), 2) if len(hi) and hi["fwd_250d"].notna().any() else None,
            "all_fwd60": round(float(g["fwd_60d"].mean()), 2) if g["fwd_60d"].notna().any() else None,
            "all_fwd250": round(float(g["fwd_250d"].mean()), 2) if g["fwd_250d"].notna().any() else None,
        })

    # 3. 检验「汇金救市脉冲」（broad6_net_inflow_5d_yi >= 200亿 / >= 400亿）
    pulse_stats = []
    for thr in (100.0, 200.0, 300.0, 500.0):
        g = sub[sub["broad6_net_inflow_5d_yi"] >= thr]
        pulse_stats.append({
            "threshold_yi": thr,
            "days": int(len(g)),
            "fwd20_avg": round(float(g["fwd_20d"].mean()), 2) if len(g) and g["fwd_20d"].notna().any() else None,
            "fwd20_win": round(float((g["fwd_20d"] > 0).mean() * 100.0), 1) if len(g) and g["fwd_20d"].notna().any() else None,
            "fwd60_avg": round(float(g["fwd_60d"].mean()), 2) if len(g) and g["fwd_60d"].notna().any() else None,
            "fwd60_win": round(float((g["fwd_60d"] > 0).mean() * 100.0), 1) if len(g) and g["fwd_60d"].notna().any() else None,
            "fwd250_avg": round(float(g["fwd_250d"].mean()), 2) if len(g) and g["fwd_250d"].notna().any() else None,
        })

    # 4. 检验「股债性价比 ERP」分组（<5%, 5~6%, >=6%）
    sub["erp_bucket"] = pd.cut(sub["erp_pct"], bins=[-10, 5.0, 6.0, 20.0], labels=["偏贵(<5%)", "中性(5~6%)", "便宜(>=6%)"])
    erp_table = sub.groupby("erp_bucket", observed=False)[["fwd_20d", "fwd_60d", "fwd_250d"]].agg(["count", "mean"])

    # 5. 检验最近 2026-11 本周大幅净申购的明细（反幻觉抽检：确认究竟哪只 ETF 涨份额了）
    recent_shares = _ch_query_df(
        """
        SELECT toString(trade_date) dt, code, round(shares_wan/1e4, 2) sh_yi, round(close, 3) px, round(scale_yi, 1) scale_yi
        FROM stock.ar4_etf_share_daily FINAL
        WHERE trade_date >= '2026-11-12' AND bucket IN ('hs300_core', 'sz50_core', 'zz500_core', 'zz1000_core')
        ORDER BY code, trade_date
        """
    )

    # 6. 历史最高 5 日净申购 Top 8 交易日（看是不是精准踩中 2024-02、2025-04、2026-11 等真实救市节点）
    top_pulses = (
        sub[["trade_date", "hs300_close", "broad6_net_inflow_5d_yi", "nt_share_chg_5d_pct", "nt_ratio_pctile_250d", "erp_pct", "fwd_20d", "fwd_60d"]]
        .sort_values("broad6_net_inflow_5d_yi", ascending=False)
        .head(10)
        .to_dict(orient="records")
    )

    print("\n" + "=" * 92)
    print("【第一部分】网传「国家队持仓250日分位」独立实证复现（2022-01 ~ 2026-11 真实日频数据）")
    print("=" * 92)
    print(nt_table.to_string())
    print("\n按年份拆解（高位 >=80% 分位 vs 全年均值，防单年幻觉）：")
    print(pd.DataFrame(yearly_nt).to_string(index=False))

    print("\n【去趋势脉冲】国家队6大宽基5日净申购资金阈值 vs 沪深300未来收益：")
    print(pd.DataFrame(pulse_stats).to_string(index=False))

    print("\n【宏观估值锚】沪深300 股债性价比 ERP 分组 vs 未来收益：")
    print(erp_table.to_string())

    print("\n【历史最大国家队净申购脉冲 Top 10 交易日（反幻觉检验）】：")
    print(pd.DataFrame(top_pulses).to_string(index=False))

    print("\n【本周 2026-11-12 ~ 11-20 国家队6大宽基份额明细（确认真实申购非拆分）】：")
    piv_r = recent_shares.pivot(index="dt", columns="code", values="sh_yi")
    print(piv_r.to_string())

    return {
        "yearly_nt": yearly_nt,
        "pulse_stats": pulse_stats,
        "top_pulses": top_pulses,
    }


def simulate_ar4_physical_dual_core(
    day_df: pd.DataFrame,
    macro_map: dict[str, dict[str, float]],
    by_date_rows: dict[str, pd.DataFrame],
    px_lookup: dict[tuple[str, str], tuple[float, float, float, float, float]],
    div_lookup: dict[tuple[str, str], tuple[float, float]],
    piv_o: pd.DataFrame,
    piv_c: pd.DataFrame,
    code_to_class: dict[str, str],
    selector_fn: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    name: str,
    stock_cap_fn: Callable[[str, str, dict[str, float]], float],
    panic_trigger_fn: Callable[[str, str, float, float, dict[str, float]], tuple[bool, float]] | None = None,
    trail_stop_fn: Callable[[dict[str, float]], float] | None = None,
    max_slots: int = 4,
    hard_stop_pct: float = -0.10,
    max_hold_days: int = 60,
    rebal_every: int = 5,
) -> tuple[pd.Series, dict[str, Any]]:
    """支持 AR4 宏观/国家队动态信号钩子的 100% 物理单账户零杠杆联合撮合引擎（严格 T-1 可见）。"""
    dates = day_df["trade_date"].astype(str).tolist()
    reg_map = dict(zip(dates, day_df["ar1_regime"].tolist()))
    breadth_map = dict(zip(dates, day_df["market_above_ma20_pct"].fillna(50.0).tolist()))

    ret20 = piv_c.pct_change(20, fill_method=None)
    ma20 = piv_c.rolling(20).mean()
    above_ma20 = piv_c > ma20
    vol20 = piv_c.pct_change(fill_method=None).rolling(20).std() * np.sqrt(242.0)
    risk_adj_mom = ret20 / (vol20 + 0.05)
    panic_pair_cc = piv_c[["159915.SZ", "512100.SH"]].pct_change(fill_method=None).mean(axis=1)

    all_etfs = list(piv_c.columns)
    non_cn_broad = [c for c in all_etfs if code_to_class.get(c) != "cn_broad"]
    cn_broad_codes = [c for c in all_etfs if code_to_class.get(c) == "cn_broad"]

    def _pick_etf(prev_dt: str, pool: list[str], score_df: pd.DataFrame, k: int) -> list[str]:
        if prev_dt not in score_df.index:
            return []
        s_row = score_df.loc[prev_dt]
        a_row = above_ma20.loc[prev_dt]
        r20_row = ret20.loc[prev_dt]
        cands = []
        for c in pool:
            if c not in s_row.index or pd.isna(s_row[c]) or pd.isna(r20_row.get(c)):
                continue
            if bool(a_row.get(c, False)) and float(r20_row[c]) > 0.0:
                cands.append((c, float(s_row[c])))
        cands.sort(key=lambda x: x[1], reverse=True)
        return [c for c, _ in cands[:k]]

    cash = 1.0
    stock_pos: dict[str, StockPos] = {}
    etf_shares: dict[str, float] = {}
    pending_stock_buys: list[dict[str, Any]] = []
    etf_target_norm: dict[str, float] = {}

    days_since_rebal = rebal_every
    panic_active = False
    panic_days = 0
    panic_cum = 1.0
    panic_cd = 0
    panic_weight_pair = 0.60
    panic_trigger_cnt = 0

    nav_list: list[float] = [1.0]
    stock_trades = 0

    for i in range(1, len(dates)):
        prev_dt = dates[i - 1]
        dt = dates[i]
        reg_prev = reg_map.get(prev_dt, "balanced")
        reg_today = reg_map.get(dt, "balanced")
        b_prev = breadth_map.get(prev_dt, 50.0)
        pair_r_prev = float(panic_pair_cc.get(prev_dt, 0.0)) if prev_dt in panic_pair_cc.index else 0.0
        mac_prev = macro_map.get(prev_dt, {})
        mac_today = macro_map.get(dt, {})
        day_tbl = by_date_rows.get(dt)

        # A. 除权除息
        for code, pos in stock_pos.items():
            ev = div_lookup.get((code, dt))
            if ev is not None:
                b10, c10 = ev
                cash_ps = c10 / 10.0
                mult = 1.0 + b10 / 10.0
                if cash_ps > 0:
                    cash += pos.shares * cash_ps
                if mult > 1.0:
                    pos.shares *= mult
                pos.entry_px = max(0.01, (pos.entry_px - cash_ps) / mult)
                pos.peak_close = max(0.01, (pos.peak_close - cash_ps) / mult)
                pos.last_close = max(0.01, (pos.last_close - cash_ps) / mult)

        # B. 冰点反抽 / 汇金救市脉冲状态机（严格基于 prev_dt 收盘）
        panic_changed = False
        if panic_active:
            if panic_days >= 5 or (panic_cum - 1.0) <= -0.06 or (panic_cum - 1.0) >= 0.12 or b_prev >= 38.0:
                panic_active = False
                panic_days = 0
                panic_cd = 2
                panic_changed = True
        else:
            if panic_cd > 0:
                panic_cd -= 1
            else:
                if panic_trigger_fn is not None:
                    trig, w_pair = panic_trigger_fn(prev_dt, reg_prev, b_prev, pair_r_prev, mac_prev)
                else:
                    trig, w_pair = (b_prev <= 18.0 and pair_r_prev >= 0.005), 0.60
                if trig:
                    panic_active = True
                    panic_days = 0
                    panic_cum = 1.0
                    panic_weight_pair = w_pair
                    panic_trigger_cnt += 1
                    panic_changed = True

        days_since_rebal += 1
        need_etf_eval = panic_changed or (not panic_active and (days_since_rebal >= rebal_every or not etf_target_norm))
        if i >= 2 and reg_prev != reg_map.get(dates[i - 2], reg_prev):
            need_etf_eval = True
        if need_etf_eval:
            if panic_active:
                safe_top = _pick_etf(prev_dt, non_cn_broad, risk_adj_mom, 1)
                safe_code = safe_top[0] if safe_top else SAFE_BOND_CODE
                w_half = panic_weight_pair / 2.0
                etf_target_norm = {"159915.SZ": w_half, "512100.SH": w_half, safe_code: max(0.0, 1.0 - panic_weight_pair)}
            else:
                if reg_prev in ("offensive", "extreme_offensive"):
                    picks = _pick_etf(prev_dt, cn_broad_codes, ret20, 2)
                    if not picks:
                        picks = _pick_etf(prev_dt, non_cn_broad, ret20, 1)
                else:
                    picks = _pick_etf(prev_dt, non_cn_broad, ret20, 1)
                if not picks:
                    etf_target_norm = {SAFE_BOND_CODE: 1.0}
                else:
                    etf_target_norm = {c: 1.0 / len(picks) for c in picks}
            days_since_rebal = 0

        # C. 开盘先卖后买（物理单一现金池）
        def _etf_open(c: str) -> float:
            if dt in piv_o.index and c in piv_o.columns and pd.notna(piv_o.loc[dt, c]) and float(piv_o.loc[dt, c]) > 0:
                return float(piv_o.loc[dt, c])
            if dt in piv_c.index and c in piv_c.columns and pd.notna(piv_c.loc[dt, c]) and float(piv_c.loc[dt, c]) > 0:
                return float(piv_c.loc[dt, c])
            return 0.0

        def _etf_close(c: str) -> float:
            if dt in piv_c.index and c in piv_c.columns and pd.notna(piv_c.loc[dt, c]) and float(piv_c.loc[dt, c]) > 0:
                return float(piv_c.loc[dt, c])
            return _etf_open(c)

        # C1. 股票退出
        trail_pct = trail_stop_fn(mac_prev) if trail_stop_fn is not None else 0.10
        to_sell_stocks: list[str] = []
        for code, pos in stock_pos.items():
            px_t = px_lookup.get((code, dt))
            if px_t is None:
                continue
            op, _hi, _lo, _cl, prev_cl = px_t
            if op <= 0:
                continue
            if prev_cl > 0 and op <= prev_cl * 0.905:
                continue
            ret_prev = (pos.last_close / pos.entry_px) - 1.0
            dd_peak = (pos.last_close / pos.peak_close) - 1.0
            if (
                pos.days_held >= max_hold_days
                or ret_prev <= hard_stop_pct
                or dd_peak <= -trail_pct
                or reg_prev in ("defensive", "extreme_panic")
            ):
                to_sell_stocks.append(code)

        for code in to_sell_stocks:
            pos = stock_pos.pop(code)
            op = px_lookup[(code, dt)][0]
            cash += pos.shares * op * (1.0 - SELL_COST)

        # 计算开盘总权益
        cur_stock_val_open = sum(
            pos.shares * (px_lookup[(c, dt)][0] if (c, dt) in px_lookup and px_lookup[(c, dt)][0] > 0 else pos.last_close)
            for c, pos in stock_pos.items()
        )
        cur_etf_val_open = sum(sh * _etf_open(c) for c, sh in etf_shares.items() if sh > 1e-12)
        total_eq_open = cash + cur_stock_val_open + cur_etf_val_open

        dyn_stock_cap = stock_cap_fn(prev_dt, reg_prev, mac_prev)
        slot_w = dyn_stock_cap / max(1, max_slots)

        # C2. 执行股票开仓（缺钱时先卖 ETF 腾现金）
        if pending_stock_buys and reg_prev in ("offensive", "extreme_offensive") and dyn_stock_cap > 0.0:
            for cand in pending_stock_buys:
                code = cand["code"]
                if code in stock_pos or len(stock_pos) >= max_slots:
                    continue
                if cand["ind"] and any(p.industry == cand["ind"] for p in stock_pos.values()):
                    continue
                px_t = px_lookup.get((code, dt))
                if px_t is None:
                    continue
                op, _hi, _lo, _cl, prev_cl = px_t
                if op <= 0:
                    continue
                if prev_cl > 0 and op >= prev_cl * 1.095:
                    continue

                cur_stock_val_now = sum(
                    pos.shares * (px_lookup[(c, dt)][0] if (c, dt) in px_lookup and px_lookup[(c, dt)][0] > 0 else pos.last_close)
                    for c, pos in stock_pos.items()
                )
                rem_budget = max(0.0, total_eq_open * dyn_stock_cap - cur_stock_val_now)
                target_notional = min(total_eq_open * slot_w, rem_budget)
                if target_notional <= 1e-4:
                    break

                if cash < target_notional and etf_shares:
                    need = (target_notional - cash) / (1.0 - ETF_ONE_WAY_COST)
                    cur_e_val = sum(sh * _etf_open(c) for c, sh in etf_shares.items() if sh > 1e-12)
                    if cur_e_val > 1e-9:
                        frac = min(1.0, need / cur_e_val)
                        for ec in list(etf_shares.keys()):
                            e_op = _etf_open(ec)
                            if e_op <= 0 or etf_shares[ec] <= 1e-12:
                                continue
                            sell_sh = etf_shares[ec] * frac
                            etf_shares[ec] -= sell_sh
                            cash += sell_sh * e_op * (1.0 - ETF_ONE_WAY_COST)
                            if etf_shares[ec] <= 1e-12:
                                del etf_shares[ec]

                spend = min(target_notional, cash)
                if spend <= 1e-4:
                    break
                shares = (spend * (1.0 - BUY_COST)) / op
                cash -= spend
                stock_pos[code] = StockPos(
                    code=code,
                    name=cand["name"],
                    industry=cand["ind"] or "未分类",
                    shares=shares,
                    entry_px=op,
                    entry_date=dt,
                    signal_date=prev_dt,
                    notice_date=cand.get("notice_date", ""),
                    report_date=cand.get("report_date", ""),
                    pit_np_yoy=float(cand.get("pit_np_yoy") or 0.0),
                    peak_close=op,
                    last_close=op,
                )
                stock_trades += 1
        pending_stock_buys = []

        # C3. 全部剩余非股票资金无缝配置 ETF
        cur_etf_val_after_stock = sum(sh * _etf_open(c) for c, sh in etf_shares.items() if sh > 1e-12)
        etf_pool_budget = cash + cur_etf_val_after_stock

        cur_etf_norm = {c: (sh * _etf_open(c)) / etf_pool_budget for c, sh in etf_shares.items() if etf_pool_budget > 1e-9 and sh > 1e-12}
        drift = sum(abs(cur_etf_norm.get(c, 0.0) - etf_target_norm.get(c, 0.0)) for c in set(cur_etf_norm) | set(etf_target_norm))
        need_trade_etf = need_etf_eval or (cash > total_eq_open * 0.03) or (drift > 0.10)

        if need_trade_etf and etf_pool_budget > 1e-6 and etf_target_norm:
            for ec in list(etf_shares.keys()):
                e_op = _etf_open(ec)
                if e_op <= 0:
                    continue
                cur_val = etf_shares[ec] * e_op
                tgt_val = etf_pool_budget * etf_target_norm.get(ec, 0.0)
                if cur_val > tgt_val + 1e-6:
                    sell_val = cur_val - tgt_val
                    sell_sh = min(etf_shares[ec], sell_val / e_op)
                    etf_shares[ec] -= sell_sh
                    cash += sell_sh * e_op * (1.0 - ETF_ONE_WAY_COST)
                    if etf_shares[ec] <= 1e-12:
                        del etf_shares[ec]
            total_buy_need = 0.0
            buy_needs: dict[str, float] = {}
            for ec, w in etf_target_norm.items():
                e_op = _etf_open(ec)
                if e_op <= 0 or w <= 0:
                    continue
                cur_val = etf_shares.get(ec, 0.0) * e_op
                tgt_val = etf_pool_budget * w
                if tgt_val > cur_val + 1e-6:
                    buy_needs[ec] = tgt_val - cur_val
                    total_buy_need += tgt_val - cur_val
            if total_buy_need > 1e-6 and cash > 1e-6:
                scale = min(1.0, cash / total_buy_need)
                for ec, bn in buy_needs.items():
                    e_op = _etf_open(ec)
                    spend = min(cash, bn * scale)
                    if spend <= 1e-9 or e_op <= 0:
                        continue
                    etf_shares[ec] = etf_shares.get(ec, 0.0) + (spend * (1.0 - ETF_ONE_WAY_COST)) / e_op
                    cash -= spend

        # D. 收盘市值与冰点累计
        if panic_active:
            panic_days += 1
            r_pair = float(panic_pair_cc.get(dt, 0.0)) if dt in panic_pair_cc.index and pd.notna(panic_pair_cc.get(dt)) else 0.0
            panic_cum *= (1.0 + r_pair)

        s_val_close = 0.0
        for code, pos in stock_pos.items():
            px_t = px_lookup.get((code, dt))
            if px_t is not None and px_t[3] > 0:
                pos.last_close = px_t[3]
                if pos.last_close > pos.peak_close:
                    pos.peak_close = pos.last_close
            pos.days_held += 1
            s_val_close += pos.shares * pos.last_close

        e_val_close = sum(sh * _etf_close(c) for c, sh in etf_shares.items() if sh > 1e-12)
        total_eq_close = cash + s_val_close + e_val_close
        nav_list.append(total_eq_close)

        # E. T日收盘选股供 T+1 开盘买入
        today_cap = stock_cap_fn(dt, reg_today, mac_today)
        if day_tbl is not None and not day_tbl.empty and reg_today in ("offensive", "extreme_offensive") and today_cap > 0:
            ranked = selector_fn(day_tbl)
            if not ranked.empty:
                for _, row in ranked.head(max_slots * 3).iterrows():
                    pending_stock_buys.append({
                        "code": str(row["code"]),
                        "name": str(row.get("name") or row["code"]),
                        "ind": str(row.get("industry") or ""),
                        "notice_date": str(row.get("pit_notice_date") or ""),
                        "report_date": str(row.get("pit_report_date") or ""),
                        "pit_np_yoy": float(row.get("net_profit_yoy_pct") or 0.0),
                    })

    nav = pd.Series(nav_list, index=dates, dtype=float)
    return nav, {"name": name, "stock_trades": stock_trades, "panic_triggers": panic_trigger_cnt}


def main() -> int:
    t0 = time.time()
    # 1. 加载 AR4 宏观 & 国家队数据
    macro_df = _ch_query_df(
        """
        SELECT
            toString(trade_date) AS trade_date,
            hs300_close, hs300_pe_ttm, cn_bond_10y, us_bond_10y, erp_pct,
            margin_balance_yi, hs300_top3_shares_yi, hs300_top3_scale_yi,
            broad6_shares_yi, broad6_scale_yi, market_total_amt_yi, top5pct_amt_share,
            nt_ratio_proxy, nt_ratio_pctile_250d, nt_share_chg_5d_pct,
            nt_share_chg_20d_pct, broad6_net_inflow_5d_yi
        FROM stock.ar4_macro_daily FINAL
        ORDER BY trade_date
        """
    )
    # 预计算宏观滚动派生字段（严格向后看，零未来函数）
    macro_df["margin_pctile_250d"] = (
        macro_df["margin_balance_yi"].rolling(250, min_periods=60).apply(lambda w: float(np.mean(w <= w.iloc[-1]) * 100.0), raw=False)
    ).fillna(50.0)
    macro_df["crowd_pctile_250d"] = (
        macro_df["top5pct_amt_share"].rolling(250, min_periods=60).apply(lambda w: float(np.mean(w <= w.iloc[-1]) * 100.0), raw=False)
    ).fillna(50.0)
    macro_df["erp_pctile_250d"] = (
        macro_df["erp_pct"].rolling(250, min_periods=60).apply(lambda w: float(np.mean(w <= w.iloc[-1]) * 100.0), raw=False)
    ).fillna(50.0)

    standalone = audit_and_test_standalone_signals(macro_df)

    # 2. 加载 AR3 v3.0 物理单账户全量面板
    print("\n" + "=" * 92)
    print("【第二部分】AR3 v3.0 物理单账户零杠杆基准 × AR4 宏观/国家队信号 消融对照实验")
    print("=" * 92)
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
    piv_o, piv_c, code_to_class, _ = _load_etf_panels()
    macro_map = {str(r["trade_date"])[:10]: r for r in macro_df.to_dict(orient="records")}

    ab_sel = _sel_ab_blend

    experiments: list[dict[str, Any]] = [
        {
            "name": "00_Baseline_AR3_v3.0 (50% AB + #01 + AR2_2A)",
            "stock_cap_fn": lambda dt, reg, m: 0.50,
            "panic_trigger_fn": None,
            "trail_stop_fn": None,
        },
        {
            "name": "01_+国家队250日分位动态股仓(>=70%加至65%, <30%降至35%)",
            "stock_cap_fn": lambda dt, reg, m: 0.65 if m.get("nt_ratio_pctile_250d", 50.0) >= 70.0 else (0.35 if m.get("nt_ratio_pctile_250d", 50.0) < 30.0 else 0.50),
            "panic_trigger_fn": None,
            "trail_stop_fn": None,
        },
        {
            "name": "02_+国家队250日分位保守版(>=80%加至60%, <40%降至30%)",
            "stock_cap_fn": lambda dt, reg, m: 0.60 if m.get("nt_ratio_pctile_250d", 50.0) >= 80.0 else (0.30 if m.get("nt_ratio_pctile_250d", 50.0) < 40.0 else 0.50),
            "panic_trigger_fn": None,
            "trail_stop_fn": None,
        },
        {
            "name": "03_+汇金救市脉冲共振(冰点+5日净买>=150亿反抽提仓至80%)",
            "stock_cap_fn": lambda dt, reg, m: 0.50,
            "panic_trigger_fn": lambda dt, reg, b, pr, m: (
                (True, 0.80 if m.get("broad6_net_inflow_5d_yi", 0.0) >= 150.0 else 0.60)
                if (b <= 18.0 and pr >= 0.005)
                else (False, 0.60)
            ),
            "trail_stop_fn": None,
        },
        {
            "name": "04_+汇金救市政策底触发器(防守+5日净买>=250亿+右侧阳线)",
            "stock_cap_fn": lambda dt, reg, m: 0.50,
            "panic_trigger_fn": lambda dt, reg, b, pr, m: (
                (True, 0.70)
                if ((b <= 18.0 and pr >= 0.005) or (reg in ("defensive", "extreme_panic") and m.get("broad6_net_inflow_5d_yi", 0.0) >= 250.0 and pr >= 0.005))
                else (False, 0.60)
            ),
            "trail_stop_fn": None,
        },
        {
            "name": "05_+股债性价比ERP动态股仓(>=6%加至65%, <4.8%降至30%)",
            "stock_cap_fn": lambda dt, reg, m: 0.65 if m.get("erp_pct", 5.5) >= 6.0 else (0.30 if m.get("erp_pct", 5.5) < 4.8 else 0.50),
            "panic_trigger_fn": None,
            "trail_stop_fn": None,
        },
        {
            "name": "06_+国家队分位×ERP双估值底加仓(双高加至70%, 双低降至30%)",
            "stock_cap_fn": lambda dt, reg, m: (
                0.70 if (m.get("nt_ratio_pctile_250d", 50.0) >= 65.0 and m.get("erp_pct", 5.5) >= 5.8)
                else (0.30 if (m.get("nt_ratio_pctile_250d", 50.0) < 35.0 and m.get("erp_pct", 5.5) < 5.2) else 0.50)
            ),
            "panic_trigger_fn": None,
            "trail_stop_fn": None,
        },
        {
            "name": "07_+拥挤度/两融过热减速(两融>=90%且拥挤>=85%降仓40%+止盈7%)",
            "stock_cap_fn": lambda dt, reg, m: 0.40 if (m.get("margin_pctile_250d", 50.0) >= 90.0 and m.get("crowd_pctile_250d", 50.0) >= 85.0) else 0.50,
            "panic_trigger_fn": None,
            "trail_stop_fn": lambda m: 0.07 if (m.get("margin_pctile_250d", 50.0) >= 90.0 and m.get("crowd_pctile_250d", 50.0) >= 85.0) else 0.10,
        },
        {
            "name": "08_+ERP精准风控(ERP>=6.0%加至65%, ERP<5.5%降至35%, <5.0%锁0%)",
            "stock_cap_fn": lambda dt, reg, m: (
                0.0 if m.get("erp_pct", 5.5) < 5.0
                else (0.35 if m.get("erp_pct", 5.5) < 5.5 else (0.65 if m.get("erp_pct", 5.5) >= 6.0 else 0.50))
            ),
            "panic_trigger_fn": None,
            "trail_stop_fn": None,
        },
        {
            "name": "09_纯加仓对照(固定65% AB + #01，检验是否纯靠加仓而非择时)",
            "stock_cap_fn": lambda dt, reg, m: 0.65,
            "panic_trigger_fn": None,
            "trail_stop_fn": None,
        },
    ]

    summary_rows = []
    for exp in experiments:
        nav, meta = simulate_ar4_physical_dual_core(
            day_df, macro_map, by_date_rows, px_lookup, div_lookup, piv_o, piv_c, code_to_class,
            ab_sel,
            name=exp["name"],
            stock_cap_fn=exp["stock_cap_fn"],
            panic_trigger_fn=exp["panic_trigger_fn"],
            trail_stop_fn=exp["trail_stop_fn"],
        )
        assert meta["stock_trades"] > 20, f"异常：个股成交笔数过低 ({meta['stock_trades']})，请检查日期键对齐！"
        m_pack = _pack_metrics(nav, splits)
        yr = {
            y: _eval_nav(nav.loc[[d for d in nav.index if d.startswith(y)]])["cum_ret_pct"]
            for y in ("2023", "2024", "2025", "2026")
        }
        summary_rows.append({
            "strategy": exp["name"],
            "full_ret%": m_pack["full"]["cum_ret_pct"],
            "ann%": m_pack["full"]["ann_ret_pct"],
            "max_dd%": m_pack["full"]["max_dd_pct"],
            "sharpe": m_pack["full"]["sharpe"],
            "calmar": m_pack["full"]["calmar"],
            "oos_ret%": m_pack["oos"]["cum_ret_pct"],
            "oos_dd%": m_pack["oos"]["max_dd_pct"],
            "stk_trd": meta["stock_trades"],
            "y23": yr.get("2023"),
            "y24": yr.get("2024"),
            "y25": yr.get("2025"),
            "y26": yr.get("2026"),
        })

    res_df = pd.DataFrame(summary_rows)
    print(res_df.to_string(index=False))

    out_path = runtime_path("mining_runs") / "ar4_macro_national_team_audit_latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"standalone": standalone, "ablation": summary_rows, "elapsed_sec": round(time.time() - t0, 1)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[AR4-Audit] 全部消融验真完成，耗时 {time.time() - t0:.1f}s，报告落盘: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
