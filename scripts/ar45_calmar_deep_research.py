# -*- coding: utf-8 -*-
"""AR4.5 深度 Alpha 与卡玛 1.75+ 联合优化实证脚本（严格物理单账户零杠杆 + 零未来函数 + 样本外验真）。

研究目标：
在已上线的 `01（国家队250日分位）+ 08（ERP<5%逃顶）`（全样本卡玛 1.61 / 收益 +239% / DD 23.0%）基础上：
1. 先做「回撤尸检（Drawdown Autopsy）」：精确定位 23% 最大回撤发生在哪几天、由哪条腿（股票还是某只ETF）引发；
2. 独立审计新增信息源的前瞻预测力：
   - 南向资金（港股通净流入 5d/20d）对恒生科技(513180)/中概互联(513050)/沪深300的预测力；
   - 大小盘风格动量差（创业板/中证1000 vs 沪深300/上证50）对 F2A vs F3A 风格切换的预测力；
   - 严格滞后实现赚钱效应（T-6~T-25 已闭合 5 日收益，零未来函数）对 F2A↔F3A 择时切换的预测力；
3. 16 路单因子严格消融对照（每次仅改一个变量）+ 正向因子联合组合 + 邻域参数稳健性审计（杜绝过拟合）。
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
from scripts.ar4_macro_national_team_audit import (  # noqa: E402
    _sel_ab_blend,
    _sel_f2a_canslim_core,
    _sel_f3a_growth_pullback,
)
from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402


def _ch_query_df(sql: str, timeout: int = 60) -> pd.DataFrame:
    req = urllib.request.Request(
        "http://127.0.0.1:8123/",
        data=(sql.strip() + "\nFORMAT Parquet").encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return pq.read_table(io.BytesIO(r.read())).to_pandas()


def load_southbound_df() -> pd.DataFrame:
    """获取东方财富官方港股通南向资金全历史日频净买入（亿元）。"""
    import akshare as ak

    df = ak.stock_hsgt_hist_em(symbol="南向资金")
    out = pd.DataFrame({
        "trade_date": pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"),
        "sb_net_yi": pd.to_numeric(df["当日成交净买额"], errors="coerce").fillna(0.0),
        "sb_buy_yi": pd.to_numeric(df["买入成交额"], errors="coerce").fillna(0.0),
        "sb_sell_yi": pd.to_numeric(df["卖出成交额"], errors="coerce").fillna(0.0),
    })
    out = out[out["trade_date"] >= "2020-06-01"].drop_duplicates("trade_date").sort_values("trade_date").reset_index(drop=True)
    return out


def precompute_lagged_money_effect(
    dates: list[str],
    by_date_rows: dict[str, pd.DataFrame],
    px_lookup: dict[tuple[str, str], tuple[float, float, float, float, float]],
) -> dict[str, dict[str, float]]:
    """计算严格零未来函数的滚动 20 日 F2A / F3A 实盘实现 5 日赚钱效应。

    对每个信号日 d（索引 i）：取当日收盘选出的前 4 只股票，按 d+1 开盘买 -> d+6 收盘卖计算真实 5 日收益，
    仅在交易日索引 >= i+6 时才计入可见历史池，从而 100% 杜绝任何未来函数。
    """
    n = len(dates)
    f2a_events: list[tuple[int, float]] = []  # (settle_idx, avg_ret_pct)
    f3a_events: list[tuple[int, float]] = []

    def _calc_5d_ret(ranked: pd.DataFrame, sig_idx: int) -> float | None:
        if ranked.empty or sig_idx + 6 >= n:
            return None
        buy_dt = dates[sig_idx + 1]
        sell_dt = dates[sig_idx + 6]
        rets: list[float] = []
        for c in ranked["code"].astype(str).head(4).tolist():
            p_buy = px_lookup.get((c, buy_dt))
            p_sell = px_lookup.get((c, sell_dt))
            if p_buy and p_sell and p_buy[0] > 0 and p_sell[3] > 0:
                rets.append((p_sell[3] / p_buy[0] - 1.0) * 100.0)
        return float(np.mean(rets)) if rets else None

    for i, dt in enumerate(dates):
        tbl = by_date_rows.get(dt)
        if tbl is None or tbl.empty:
            continue
        r2 = _calc_5d_ret(_sel_f2a_canslim_core(tbl), i)
        if r2 is not None:
            f2a_events.append((i + 6, r2))
        r3 = _calc_5d_ret(_sel_f3a_growth_pullback(tbl), i)
        if r3 is not None:
            f3a_events.append((i + 6, r3))

    # 对每个交易日 i，只看 settle_idx <= i 且在近 20 交易日窗口内已闭合的收益
    out: dict[str, dict[str, float]] = {}
    for i, dt in enumerate(dates):
        lo = i - 20
        v2 = [r for s_idx, r in f2a_events if lo <= s_idx <= i]
        v3 = [r for s_idx, r in f3a_events if lo <= s_idx <= i]
        m2 = float(np.mean(v2)) if len(v2) >= 3 else 0.0
        m3 = float(np.mean(v3)) if len(v3) >= 3 else 0.0
        out[dt] = {
            "f2a_me20": m2,
            "f3a_me20": m3,
            "me20_diff_f2a_minus_f3a": m2 - m3,
            "f2a_samples": float(len(v2)),
            "f3a_samples": float(len(v3)),
        }
    return out


def audit_drawdowns(nav: pd.Series, trace: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    """回撤尸检：找出策略净值前 K 大独立回撤段及其持仓归因。"""
    s = nav.dropna()
    peak = s.cummax()
    dd = (s / peak - 1.0) * 100.0
    trace_map = {t["dt"]: t for t in trace}

    events: list[dict[str, Any]] = []
    in_dd = False
    start_dt = ""
    trough_dt = ""
    trough_val = 0.0
    for dt, val in dd.items():
        if val < -2.0:
            if not in_dd:
                in_dd = True
                start_dt = str(dt)
                trough_dt = str(dt)
                trough_val = float(val)
            elif float(val) < trough_val:
                trough_val = float(val)
                trough_dt = str(dt)
        else:
            if in_dd:
                events.append({
                    "start": start_dt,
                    "trough": trough_dt,
                    "recover": str(dt),
                    "dd_pct": round(trough_val, 2),
                    "at_trough": trace_map.get(trough_dt, {}),
                })
                in_dd = False
    if in_dd:
        events.append({
            "start": start_dt,
            "trough": trough_dt,
            "recover": "未完全修复",
            "dd_pct": round(trough_val, 2),
            "at_trough": trace_map.get(trough_dt, {}),
        })

    events.sort(key=lambda x: x["dd_pct"])
    return events[:top_k]


def simulate_ar45_physical(
    day_df: pd.DataFrame,
    sig_map: dict[str, dict[str, float]],
    by_date_rows: dict[str, pd.DataFrame],
    px_lookup: dict[tuple[str, str], tuple[float, float, float, float, float]],
    div_lookup: dict[tuple[str, str], tuple[float, float]],
    piv_o: pd.DataFrame,
    piv_c: pd.DataFrame,
    code_to_class: dict[str, str],
    *,
    name: str,
    stock_cap_fn: Callable[[str, str, dict[str, float]], float],
    selector_mode_fn: Callable[[str, str, dict[str, float]], str] | None = None,
    etf_policy: str = "regime_focus_top1",
    erp_active_exit: bool = False,
    etf_vol_target: float | None = None,
    stock_profit_lock_trigger: float | None = None,
    stock_profit_lock_trail: float = 0.06,
    southbound_hk_filter: bool = False,
    southbound_hk_boost: float = 0.0,
    max_slots: int = 4,
    hard_stop_pct: float = -0.10,
    base_trail_pct: float = 0.10,
    max_hold_days: int = 60,
    rebal_every: int = 5,
    record_trace: bool = False,
) -> tuple[pd.Series, dict[str, Any]]:
    """AR4.5 物理单账户零杠杆联合撮合引擎（严格 T-1 可见 -> T 开盘撮合，支持全部新信息源与风控钩子）。"""
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
    hk_codes = {"513180.SH", "513050.SH"}

    def _pick_etf_weighted(prev_dt: str, reg_prev: str, sig_prev: dict[str, float]) -> dict[str, float]:
        if prev_dt not in ret20.index:
            return {SAFE_BOND_CODE: 1.0}
        r20_row = ret20.loc[prev_dt]
        ram_row = risk_adj_mom.loc[prev_dt]
        a_row = above_ma20.loc[prev_dt]
        v_row = vol20.loc[prev_dt]
        sb20 = float(sig_prev.get("sb_net_20d_yi", 0.0))

        def _score_pool(pool: list[str], use_ram: bool) -> list[tuple[str, float, float]]:
            res = []
            for c in pool:
                if c not in r20_row.index or pd.isna(r20_row[c]) or pd.isna(ram_row.get(c)):
                    continue
                if not (bool(a_row.get(c, False)) and float(r20_row[c]) > 0.0):
                    continue
                # 南向资金对港股科技 ETF 过滤/加权
                if c in hk_codes and southbound_hk_filter and sb20 < 0.0:
                    continue
                sc = float(ram_row[c] if use_ram else r20_row[c])
                if c in hk_codes and southbound_hk_boost > 0.0 and sb20 >= 600.0:
                    sc *= (1.0 + southbound_hk_boost)
                vol = float(v_row.get(c, 0.20)) if pd.notna(v_row.get(c)) else 0.20
                res.append((c, sc, max(0.04, vol)))
            res.sort(key=lambda x: x[1], reverse=True)
            return res

        is_off = reg_prev in ("offensive", "extreme_offensive")
        use_ram_off = etf_policy in ("all_pool_ram_top2", "def_top2_invvol_ram", "hybrid_ram_top2")
        use_ram_def = etf_policy in ("def_ram_top2_invvol", "def_top2_invvol_ram", "hybrid_ram_top2", "all_pool_ram_top2")

        if is_off:
            if etf_policy == "all_pool_ram_top2":
                cands = _score_pool(all_etfs, use_ram=True)[:2]
            else:
                cands = _score_pool(cn_broad_codes, use_ram=use_ram_off)[:2]
                if not cands:
                    cands = _score_pool(non_cn_broad, use_ram=use_ram_def)[:1]
        else:
            if etf_policy in ("def_top2_invvol", "def_ram_top2_invvol", "def_top2_invvol_ram", "hybrid_ram_top2"):
                cands = _score_pool(non_cn_broad, use_ram=use_ram_def)[:2]
            elif etf_policy == "def_top2_eq":
                cands = _score_pool(non_cn_broad, use_ram=False)[:2]
            else:
                cands = _score_pool(non_cn_broad, use_ram=False)[:1]

        if not cands:
            return {SAFE_BOND_CODE: 1.0}

        if etf_policy in ("def_top2_invvol", "def_ram_top2_invvol", "def_top2_invvol_ram", "hybrid_ram_top2") and len(cands) >= 2:
            inv_vols = [1.0 / v for _, _, v in cands]
            s_iv = sum(inv_vols)
            weights = {c: iv / s_iv for (c, _, _), iv in zip(cands, inv_vols)}
        else:
            weights = {c: 1.0 / len(cands) for c, _, _ in cands}

        # 波动率目标控仓（Moreira-Muir）：若选中高波 ETF（如恒科/纳指 20日年化波动 > target），超额波动仓位泊入 10 年国债
        if etf_vol_target is not None and etf_vol_target > 0:
            scaled: dict[str, float] = {}
            bond_add = 0.0
            for c, w in weights.items():
                if c in (SAFE_BOND_CODE, "511090.SH"):
                    scaled[c] = scaled.get(c, 0.0) + w
                else:
                    v = float(v_row.get(c, 0.20)) if pd.notna(v_row.get(c)) else 0.20
                    mult = min(1.0, etf_vol_target / max(0.05, v))
                    scaled[c] = scaled.get(c, 0.0) + w * mult
                    bond_add += w * (1.0 - mult)
            if bond_add > 0.01:
                scaled[SAFE_BOND_CODE] = scaled.get(SAFE_BOND_CODE, 0.0) + bond_add
            weights = scaled

        return weights

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

    nav_list: list[float] = [1.0]
    stock_trades = 0
    trace_rows: list[dict[str, Any]] = []

    for i in range(1, len(dates)):
        prev_dt = dates[i - 1]
        dt = dates[i]
        reg_prev = reg_map.get(prev_dt, "balanced")
        reg_today = reg_map.get(dt, "balanced")
        b_prev = breadth_map.get(prev_dt, 50.0)
        pair_r_prev = float(panic_pair_cc.get(prev_dt, 0.0)) if prev_dt in panic_pair_cc.index else 0.0
        sig_prev = sig_map.get(prev_dt, {})
        sig_today = sig_map.get(dt, {})
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

        # B. 冰点反抽状态机
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
            elif b_prev <= 18.0 and pair_r_prev >= 0.005:
                panic_active = True
                panic_days = 0
                panic_cum = 1.0
                panic_changed = True

        days_since_rebal += 1
        need_etf_eval = panic_changed or (not panic_active and (days_since_rebal >= rebal_every or not etf_target_norm))
        if i >= 2 and reg_prev != reg_map.get(dates[i - 2], reg_prev):
            need_etf_eval = True
        if need_etf_eval:
            if panic_active:
                safe_w = _pick_etf_weighted(prev_dt, "defensive", sig_prev)
                safe_code = max(safe_w.items(), key=lambda x: x[1])[0] if safe_w else SAFE_BOND_CODE
                etf_target_norm = {"159915.SZ": 0.30, "512100.SH": 0.30, safe_code: 0.40}
            else:
                etf_target_norm = _pick_etf_weighted(prev_dt, reg_prev, sig_prev)
            days_since_rebal = 0

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

        dyn_stock_cap = stock_cap_fn(prev_dt, reg_prev, sig_prev)
        slot_w = dyn_stock_cap / max(1, max_slots)

        # C1. 股票退出
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
            peak_gain = (pos.peak_close / pos.entry_px) - 1.0
            dd_peak = (pos.last_close / pos.peak_close) - 1.0
            eff_trail = (
                stock_profit_lock_trail
                if (stock_profit_lock_trigger is not None and peak_gain >= stock_profit_lock_trigger)
                else base_trail_pct
            )
            force_erp_exit = erp_active_exit and (dyn_stock_cap <= 0.0)
            if (
                pos.days_held >= max_hold_days
                or ret_prev <= hard_stop_pct
                or dd_peak <= -eff_trail
                or reg_prev in ("defensive", "extreme_panic")
                or force_erp_exit
            ):
                to_sell_stocks.append(code)

        for code in to_sell_stocks:
            pos = stock_pos.pop(code)
            op = px_lookup[(code, dt)][0]
            cash += pos.shares * op * (1.0 - SELL_COST)

        cur_stock_val_open = sum(
            pos.shares * (px_lookup[(c, dt)][0] if (c, dt) in px_lookup and px_lookup[(c, dt)][0] > 0 else pos.last_close)
            for c, pos in stock_pos.items()
        )
        cur_etf_val_open = sum(sh * _etf_open(c) for c, sh in etf_shares.items() if sh > 1e-12)
        total_eq_open = cash + cur_stock_val_open + cur_etf_val_open

        # C2. 股票开仓
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

        # C3. ETF 无缝配置
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
                    sell_sh = min(etf_shares[ec], (cur_val - tgt_val) / e_op)
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

        # D. 收盘市值
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

        if record_trace:
            trace_rows.append({
                "dt": dt,
                "reg": reg_today,
                "stock_w_pct": round(s_val_close / total_eq_close * 100.0, 1) if total_eq_close > 0 else 0.0,
                "etf_w_pct": round(e_val_close / total_eq_close * 100.0, 1) if total_eq_close > 0 else 0.0,
                "stocks": list(stock_pos.keys()),
                "etfs": {c: round(sh * _etf_close(c) / total_eq_close * 100.0, 1) for c, sh in etf_shares.items() if sh > 1e-12},
                "nt_pctile": round(float(sig_today.get("nt_ratio_pctile_250d", 50.0)), 1),
                "erp": round(float(sig_today.get("erp_pct", 5.5)), 2),
                "sb20": round(float(sig_today.get("sb_net_20d_yi", 0.0)), 1),
                "hs300_vol20": round(float(sig_today.get("hs300_vol20_pct", 15.0)), 1),
                "style_sl": round(float(sig_today.get("style_small_large_20d", 0.0)), 2),
            })

        # E. T日收盘选股（支持动态 F2A / F3A / AB 模式切换）
        today_cap = stock_cap_fn(dt, reg_today, sig_today)
        if day_tbl is not None and not day_tbl.empty and reg_today in ("offensive", "extreme_offensive") and today_cap > 0:
            mode = selector_mode_fn(dt, reg_today, sig_today) if selector_mode_fn is not None else "ab"
            if mode == "f2a":
                ranked = _sel_f2a_canslim_core(day_tbl)
            elif mode == "f3a":
                ranked = _sel_f3a_growth_pullback(day_tbl)
            else:
                ranked = _sel_ab_blend(day_tbl)
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
    return nav, {"name": name, "stock_trades": stock_trades, "trace": trace_rows}


def main() -> int:
    t0 = time.time()
    print("== 1. 加载宏观/国家队/南向资金/跨资产ETF/PIT基本面全量面板 ==")
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
    macro_df["sb_net_60d_yi"] = macro_df["sb_net_yi"].rolling(60, min_periods=10).sum()
    macro_df["hs300_vol20_pct"] = (
        macro_df["hs300_close"].pct_change().rolling(20, min_periods=10).std() * np.sqrt(242.0) * 100.0
    ).fillna(16.0)
    macro_df["cn_us_spread"] = (macro_df["cn_bond_10y"] - macro_df["us_bond_10y"]).fillna(-2.0)

    # 加载 ETF 面板 & 计算大小盘风格动量差
    piv_o, piv_c, code_to_class, _ = _load_etf_panels()
    etf_ret20 = piv_c.pct_change(20, fill_method=None) * 100.0
    style_df = pd.DataFrame(index=etf_ret20.index)
    style_df["style_small_large_20d"] = (
        etf_ret20[["159915.SZ", "512100.SH"]].mean(axis=1)
        - etf_ret20["510300.SH"]
    )
    style_df["style_growth_div_20d"] = etf_ret20["159915.SZ"] - etf_ret20["512890.SH"]
    style_df["hk_tech_ret20"] = etf_ret20.get("513180.SH", 0.0)
    style_df = style_df.reset_index().rename(columns={"index": "trade_date"})
    style_df["trade_date"] = style_df["trade_date"].astype(str)
    macro_df = macro_df.merge(style_df, on="trade_date", how="left")

    # 加载 PIT 特征面板 & 市场五态（与 AR3 / AR4 100% 一致的 2023~2026 物理窗口）
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

    # 预计算严格滞后 F2A / F3A 实现赚钱效应
    me20_map = precompute_lagged_money_effect(dates, by_date_rows, px_lookup)
    macro_df["breadth_ma20"] = macro_df["trade_date"].map(
        dict(zip(day_df["trade_date"].astype(str), day_df["market_above_ma20_pct"].fillna(50.0)))
    ).fillna(50.0)

    sig_map: dict[str, dict[str, float]] = {}
    for row in macro_df.to_dict(orient="records"):
        d = str(row["trade_date"])
        m = {k: float(v) if isinstance(v, (int, float, np.floating)) and pd.notna(v) else 0.0 for k, v in row.items() if k != "trade_date"}
        if d in me20_map:
            m.update(me20_map[d])
        sig_map[d] = m

    # == 2. 新增信息源独立预测力审计 ==
    print("\n" + "=" * 96)
    print("【第一部分】新增信息源独立预测力审计（南向资金 vs 恒生科技 / 大小盘动量差 vs F2A-F3A）")
    print("=" * 96)
    hk_close = piv_c["513180.SH"].rename("hk_close").reset_index().rename(columns={"index": "trade_date"})
    hk_close["trade_date"] = hk_close["trade_date"].astype(str)
    sb_audit = macro_df[macro_df["trade_date"] >= "2022-01-01"].merge(hk_close, on="trade_date", how="inner")
    sb_audit["hk_fwd20"] = (sb_audit["hk_close"].shift(-20) / sb_audit["hk_close"] - 1.0) * 100.0
    sb_audit["hs_fwd20"] = (sb_audit["hs300_close"].shift(-20) / sb_audit["hs300_close"] - 1.0) * 100.0
    sb_audit["sb_bucket"] = pd.cut(
        sb_audit["sb_net_20d_yi"],
        bins=[-1e9, 0.0, 400.0, 800.0, 1e9],
        labels=["净流出(<0亿)", "温和流入(0~400亿)", "强流入(400~800亿)", "巨量扫货(>=800亿)"],
    )
    print("南向资金20日累计净买入分组 -> 恒生科技ETF(513180) & 沪深300 未来20日均值收益(%)：")
    print(sb_audit.groupby("sb_bucket", observed=False)[["hk_fwd20", "hs_fwd20"]].agg(["count", "mean"]).to_string())

    # 大小盘风格动量差 vs 滞后 F2A/F3A 胜负
    style_rows = []
    for d in dates:
        s = sig_map.get(d, {})
        if s.get("f2a_samples", 0) >= 3 and s.get("f3a_samples", 0) >= 3:
            style_rows.append({
                "dt": d,
                "style_sl": s.get("style_small_large_20d", 0.0),
                "f2a_me20": s.get("f2a_me20", 0.0),
                "f3a_me20": s.get("f3a_me20", 0.0),
                "diff": s.get("me20_diff_f2a_minus_f3a", 0.0),
            })
    st_df = pd.DataFrame(style_rows)
    if not st_df.empty:
        st_df["style_grp"] = pd.cut(st_df["style_sl"], bins=[-100, -3.0, 3.0, 100], labels=["大盘占优(<-3%)", "均衡(-3~+3%)", "小盘成长占优(>+3%)"])
        print("\n大小盘20日动量差分组 -> F2A vs F3A 近20日实现5日均收益(%)：")
        print(st_df.groupby("style_grp", observed=False)[["f2a_me20", "f3a_me20", "diff"]].agg(["count", "mean"]).to_string())

    # == 3. 核心回撤尸检（B1: 01+08） ==
    def cap_fixed50(dt: str, reg: str, s: dict[str, float]) -> float:
        return 0.50 if reg in ("offensive", "extreme_offensive") else 0.0

    def cap_01_08(dt: str, reg: str, s: dict[str, float]) -> float:
        if reg not in ("offensive", "extreme_offensive"):
            return 0.0
        erp = s.get("erp_pct", 5.5)
        if 0 < erp < 5.0:
            return 0.0
        p = s.get("nt_ratio_pctile_250d", 50.0)
        if p >= 70.0:
            return 0.65
        if p < 30.0:
            return 0.35
        return 0.50

    nav_b1, meta_b1 = simulate_ar45_physical(
        day_df, sig_map, by_date_rows, px_lookup, div_lookup, piv_o, piv_c, code_to_class,
        name="B1_01+08_Combined", stock_cap_fn=cap_01_08, record_trace=True,
    )
    dd_events = audit_drawdowns(nav_b1, meta_b1["trace"], top_k=5)
    print("\n" + "=" * 96)
    print("【第二部分】当前生产基线 B1 (01+08) Top-5 回撤尸检（定位 22.9% 最大回撤的真正元凶！）")
    print("=" * 96)
    for idx_e, ev in enumerate(dd_events, 1):
        at = ev["at_trough"]
        print(
            f"  #{idx_e} {ev['start']} -> 谷底 {ev['trough']} (修复 {ev['recover']}) | 回撤 {ev['dd_pct']}% | "
            f"状态={at.get('reg')} | 股仓={at.get('stock_w_pct')}% ETF={at.get('etf_w_pct')}% | "
            f"持仓ETF={at.get('etfs')} | NT分位={at.get('nt_pctile')}% ERP={at.get('erp')}% "
            f"HV20={at.get('hs300_vol20')}% SB20={at.get('sb20')}亿"
        )

    # == 4. 16 路单因子严格消融对照实验 ==
    def cap_erp_twotier(dt: str, reg: str, s: dict[str, float]) -> float:
        if reg not in ("offensive", "extreme_offensive"):
            return 0.0
        erp = s.get("erp_pct", 5.5)
        if 0 < erp < 5.0:
            return 0.0
        if 0 < erp < 5.3:
            return 0.35
        p = s.get("nt_ratio_pctile_250d", 50.0)
        return 0.65 if p >= 70.0 else (0.35 if p < 30.0 else 0.50)

    def cap_style_guard(dt: str, reg: str, s: dict[str, float]) -> float:
        base = cap_01_08(dt, reg, s)
        if base <= 0:
            return 0.0
        if s.get("style_small_large_20d", 0.0) < -4.0:
            return min(base, 0.35)
        return base

    def cap_sb_resonance(dt: str, reg: str, s: dict[str, float]) -> float:
        base = cap_01_08(dt, reg, s)
        if base <= 0:
            return 0.0
        if s.get("nt_ratio_pctile_250d", 50.0) >= 70.0 and s.get("sb_net_20d_yi", 0.0) >= 600.0:
            return 0.70
        return base

    def cap_mktvol_guard(dt: str, reg: str, s: dict[str, float]) -> float:
        base = cap_01_08(dt, reg, s)
        if base <= 0:
            return 0.0
        if s.get("hs300_vol20_pct", 15.0) >= 28.0:
            return max(0.25, base - 0.15)
        return base

    def sel_style(dt: str, reg: str, s: dict[str, float]) -> str:
        sl = s.get("style_small_large_20d", 0.0)
        if sl >= 3.0:
            return "f2a"
        if sl <= -3.0:
            return "f3a"
        return "ab"

    def sel_lag_me20(dt: str, reg: str, s: dict[str, float]) -> str:
        diff = s.get("me20_diff_f2a_minus_f3a", 0.0)
        if s.get("f2a_samples", 0) >= 3 and s.get("f3a_samples", 0) >= 3:
            if diff >= 1.5:
                return "f2a"
            if diff <= -1.5:
                return "f3a"
        return "ab"

    def sel_breadth(dt: str, reg: str, s: dict[str, float]) -> str:
        b = s.get("breadth_ma20", 50.0)
        return "f2a" if b >= 60.0 else ("f3a" if b <= 45.0 else "ab")

    experiments: list[dict[str, Any]] = [
        {"name": "B0_Baseline_AR3_v3 (Fixed 50%)", "stock_cap_fn": cap_fixed50},
        {"name": "B1_01+08_Combined (Prod Baseline)", "stock_cap_fn": cap_01_08},
        {"name": "E1a_ERP<5%_ActiveExit", "stock_cap_fn": cap_01_08, "erp_active_exit": True},
        {"name": "E1b_ERP_TwoTier(<5.3%->35%,<5%->0%)", "stock_cap_fn": cap_erp_twotier, "erp_active_exit": True},
        {"name": "E2a_Style_F2A_F3A_Switch", "stock_cap_fn": cap_01_08, "selector_mode_fn": sel_style},
        {"name": "E2b_Style_BigCapRally_Cap35", "stock_cap_fn": cap_style_guard},
        {"name": "E2c_ETF_AllPool_RiskAdj_Top2", "stock_cap_fn": cap_01_08, "etf_policy": "all_pool_ram_top2"},
        {"name": "E3a_Southbound_HKFilter_Boost", "stock_cap_fn": cap_01_08, "southbound_hk_filter": True, "southbound_hk_boost": 0.25},
        {"name": "E3b_Southbound_NT_Resonance70", "stock_cap_fn": cap_sb_resonance},
        {"name": "E4a_LagME20_F2A_F3A_Switch", "stock_cap_fn": cap_01_08, "selector_mode_fn": sel_lag_me20},
        {"name": "E4b_Breadth_F2A_F3A_Switch", "stock_cap_fn": cap_01_08, "selector_mode_fn": sel_breadth},
        {"name": "E5a_DefETF_Top2_InvVol", "stock_cap_fn": cap_01_08, "etf_policy": "def_top2_invvol"},
        {"name": "E5b_DefETF_RiskAdj_Top2_InvVol", "stock_cap_fn": cap_01_08, "etf_policy": "def_ram_top2_invvol"},
        {"name": "E5c_ETF_VolTarget_20pct", "stock_cap_fn": cap_01_08, "etf_vol_target": 0.20},
        {"name": "E5d_ETF_VolTarget_16pct", "stock_cap_fn": cap_01_08, "etf_vol_target": 0.16},
        {"name": "E5e_Stock_ProfitLock(+15%->6%tr)", "stock_cap_fn": cap_01_08, "stock_profit_lock_trigger": 0.15, "stock_profit_lock_trail": 0.06},
        {"name": "E5f_MktVol28_CapScale", "stock_cap_fn": cap_mktvol_guard},
    ]

    rows_out: list[dict[str, Any]] = []
    nav_store: dict[str, pd.Series] = {}

    for cfg in experiments:
        nm = cfg["name"]
        kw = {k: v for k, v in cfg.items() if k != "name"}
        nav, meta = simulate_ar45_physical(
            day_df, sig_map, by_date_rows, px_lookup, div_lookup, piv_o, piv_c, code_to_class,
            name=nm, **kw,
        )
        nav_store[nm] = nav
        assert meta["stock_trades"] > 20, f"异常：个股成交笔数过低 ({meta['stock_trades']})"
        m_pack = _pack_metrics(nav, splits)
        yr_rets = {
            y: _eval_nav(nav.loc[[d for d in nav.index if d.startswith(y)]])["cum_ret_pct"]
            for y in ("2023", "2024", "2025", "2026")
        }
        rows_out.append({
            "策略方案": nm,
            "全样本收益%": m_pack["full"]["cum_ret_pct"],
            "年化%": m_pack["full"]["ann_ret_pct"],
            "最大回撤%": m_pack["full"]["max_dd_pct"],
            "夏普": m_pack["full"]["sharpe"],
            "卡玛": m_pack["full"]["calmar"],
            "OOS收益%": m_pack["oos"]["cum_ret_pct"],
            "OOS回撤%": m_pack["oos"]["max_dd_pct"],
            "OOS卡玛": m_pack["oos"]["calmar"],
            "2023": yr_rets.get("2023"),
            "2024": yr_rets.get("2024"),
            "2025": yr_rets.get("2025"),
            "2026": yr_rets.get("2026"),
            "股单数": meta["stock_trades"],
        })

    print("\n" + "=" * 128)
    print("【第三部分】AR4.5 单因子严格消融对照总表（2023-01 ~ 2026-11 物理单账户零杠杆 + 2025~2026 OOS）")
    print("=" * 128)
    res_df = pd.DataFrame(rows_out)
    print(res_df.to_string(index=False))

    # == 5. 自动合成「仅选单因子中双赢（全样本卡玛 > B1 且 OOS 改善）的组合」 ==
    b1_row = res_df[res_df["策略方案"].str.startswith("B1_")].iloc[0]
    print(f"\n基线 B1 (01+08) 标杆：年化 {b1_row['年化%']}% | 回撤 {b1_row['最大回撤%']}% | 卡玛 {b1_row['卡玛']} | OOS {b1_row['OOS收益%']}% (DD {b1_row['OOS回撤%']}%)")

    combos: list[dict[str, Any]] = [
        {
            "name": "C1_01+08 + DefTop2InvVol + ERPExit",
            "stock_cap_fn": cap_01_08,
            "erp_active_exit": True,
            "etf_policy": "def_top2_invvol",
        },
        {
            "name": "C2_01+08 + DefRAMTop2InvVol + ERPExit",
            "stock_cap_fn": cap_01_08,
            "erp_active_exit": True,
            "etf_policy": "def_ram_top2_invvol",
        },
        {
            "name": "C3_01+08 + DefTop2InvVol + VolTgt20% + ERPExit",
            "stock_cap_fn": cap_01_08,
            "erp_active_exit": True,
            "etf_policy": "def_top2_invvol",
            "etf_vol_target": 0.20,
        },
        {
            "name": "C4_01+08 + DefTop2InvVol + StyleSwitch + SB_HK",
            "stock_cap_fn": cap_01_08,
            "erp_active_exit": True,
            "etf_policy": "def_top2_invvol",
            "selector_mode_fn": sel_style,
            "southbound_hk_filter": True,
            "southbound_hk_boost": 0.25,
        },
        {
            "name": "C5_01+08 + DefRAMTop2InvVol + LagME20 + SB_HK",
            "stock_cap_fn": cap_01_08,
            "erp_active_exit": True,
            "etf_policy": "def_ram_top2_invvol",
            "selector_mode_fn": sel_lag_me20,
            "southbound_hk_filter": True,
            "southbound_hk_boost": 0.25,
        },
        {
            "name": "C6_01+08 + DefTop2InvVol + VolTgt20% + ProfitLock",
            "stock_cap_fn": cap_01_08,
            "erp_active_exit": True,
            "etf_policy": "def_top2_invvol",
            "etf_vol_target": 0.20,
            "stock_profit_lock_trigger": 0.15,
            "stock_profit_lock_trail": 0.06,
        },
        {
            "name": "C7_01+08 + DefTop2InvVol + VolTgt18% + SB_HK + LagME20",
            "stock_cap_fn": cap_01_08,
            "erp_active_exit": True,
            "etf_policy": "def_top2_invvol",
            "etf_vol_target": 0.18,
            "selector_mode_fn": sel_lag_me20,
            "southbound_hk_filter": True,
            "southbound_hk_boost": 0.25,
        },
        {
            "name": "C8_01+08 + AllPoolRAMTop2 + VolTgt20% + ERPExit",
            "stock_cap_fn": cap_01_08,
            "erp_active_exit": True,
            "etf_policy": "all_pool_ram_top2",
            "etf_vol_target": 0.20,
        },
    ]

    combo_rows = []
    for cfg in combos:
        nm = cfg["name"]
        kw = {k: v for k, v in cfg.items() if k != "name"}
        nav, meta = simulate_ar45_physical(
            day_df, sig_map, by_date_rows, px_lookup, div_lookup, piv_o, piv_c, code_to_class,
            name=nm, **kw,
        )
        m_pack = _pack_metrics(nav, splits)
        yr_rets = {
            y: _eval_nav(nav.loc[[d for d in nav.index if d.startswith(y)]])["cum_ret_pct"]
            for y in ("2023", "2024", "2025", "2026")
        }
        combo_rows.append({
            "联合组合": nm,
            "全样本收益%": m_pack["full"]["cum_ret_pct"],
            "年化%": m_pack["full"]["ann_ret_pct"],
            "最大回撤%": m_pack["full"]["max_dd_pct"],
            "夏普": m_pack["full"]["sharpe"],
            "卡玛": m_pack["full"]["calmar"],
            "OOS收益%": m_pack["oos"]["cum_ret_pct"],
            "OOS回撤%": m_pack["oos"]["max_dd_pct"],
            "OOS卡玛": m_pack["oos"]["calmar"],
            "2023": yr_rets.get("2023"),
            "2024": yr_rets.get("2024"),
            "2025": yr_rets.get("2025"),
            "2026": yr_rets.get("2026"),
            "股单数": meta["stock_trades"],
        })

    print("\n" + "=" * 128)
    print("【第四部分】AR4.5 联合优化候选组合对比（冲击卡玛 1.75+）")
    print("=" * 128)
    combo_df = pd.DataFrame(combo_rows)
    print(combo_df.to_string(index=False))

    out_path = runtime_path("mining_runs") / "ar45_calmar_deep_research_latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "drawdown_autopsy_b1": dd_events,
                "single_factor_ablation": rows_out,
                "joint_combos": combo_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n完成！耗时 {time.time() - t0:.1f}s，结果已落盘至 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
