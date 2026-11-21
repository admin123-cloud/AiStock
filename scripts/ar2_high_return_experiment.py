# -*- coding: utf-8 -*-
"""AR2（高收益与高资金利用率增强引擎）全市场 4,995 股逐日 Mark-to-Market 实证实验。

直击 AR1 v2.0 年化 5.5% 的三大瓶颈：
1. AR2-1: 全市场行业主线动量轮动（横盘+进攻双周期运行，吃透全年 67% 时间的行业结构性主升）
2. AR2-2: 极端恐慌冰点（宽度 <= 18%）高弹性宽基 ETF（创业板/中证1000）反抽引擎（吃透 104 天冰点高胜率红利）
3. AR2-3: 高资金利用率版机构低波慢牛（多槽满载 + 平衡期精选，把资金利用率从 18% 拉至 55%~75%）
4. AR2-4: 终极全天候多引擎无缝叠加（时间天然互补：进攻/横盘引擎 + 冰点反抽引擎，资金复用零冲突）
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ar1_alpha_experiment import (  # noqa: E402
    BUY_COST,
    SELL_COST,
    _build_ar1_regimes,
    _ch_query_df,
    _eval_nav,
    _simulate_broad_etf_top2,
)
from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402


def _simulate_sector_etf_rotation(
    panel_df: pd.DataFrame,
    day_df: pd.DataFrame,
    *,
    top_k_sectors: int = 2,
    rebal_every: int = 5,
    off_weight: float = 0.90,
    bal_weight: float = 0.60,
    def_weight: float = 0.0,
    mom_window: int = 20,
) -> tuple[pd.Series, dict[str, Any]]:
    """AR2-1A：基于全市场 4,995 股合成有效行业等权指数，在进攻+横盘期轮动动量最强且站上MA20的 Top K 主线行业（模拟行业ETF）。"""
    # 过滤有效行业（平均每日成分股 >= 10 只，排除“未分类”）
    ind_cnt = panel_df.groupby(["trade_date", "industry"])["code"].count().groupby("industry").mean()
    valid_inds = set(ind_cnt[ind_cnt >= 10].index) - {"未分类", "", "未知"}

    sub = panel_df[panel_df["industry"].isin(valid_inds)]
    ind_ret = sub.groupby(["trade_date", "industry"])["ret_1d_pct"].mean().unstack("industry").sort_index().fillna(0.0)
    ind_nav = (1.0 + ind_ret / 100.0).cumprod()
    ind_mom = ind_nav.pct_change(mom_window)
    ind_ma20 = ind_nav.rolling(20).mean()
    ind_above = ind_nav > ind_ma20

    # 行业站上MA20宽度
    ind_breadth = sub.groupby(["trade_date", "industry"])["industry_above_ma20_pct"].first().unstack("industry").sort_index()

    dates = day_df["trade_date"].astype(str).tolist()
    reg_map = dict(zip(dates, day_df["ar1_regime"].tolist()))

    etf_cost = 0.0008  # 行业 ETF 单边摩擦（含滑点）
    cur_weights: dict[str, float] = {}
    nav = 1.0
    nav_list = [1.0]
    days_since_rebal = rebal_every
    in_market_days = 0
    rebal_cnt = 0

    for i in range(1, len(dates)):
        prev_dt = dates[i - 1]
        dt = dates[i]
        reg_prev = reg_map.get(prev_dt, "balanced")

        if reg_prev in ("offensive", "extreme_offensive"):
            tgt_gross = off_weight
        elif reg_prev == "balanced":
            tgt_gross = bal_weight
        else:
            tgt_gross = def_weight

        # 触发调仓条件：转防守立即清仓；或者达到调仓周期；或者当前空仓但周期允许进场
        must_exit = tgt_gross == 0.0 and len(cur_weights) > 0
        want_enter = tgt_gross > 0.0 and len(cur_weights) == 0
        days_since_rebal += 1

        if must_exit or want_enter or (tgt_gross > 0 and days_since_rebal >= rebal_every):
            new_w: dict[str, float] = {}
            if tgt_gross > 0 and prev_dt in ind_mom.index:
                m_row = ind_mom.loc[prev_dt].dropna()
                a_row = ind_above.loc[prev_dt]
                b_row = ind_breadth.loc[prev_dt] if prev_dt in ind_breadth.index else pd.Series(dtype=float)
                elig = [
                    ind
                    for ind in m_row.index
                    if bool(a_row.get(ind, False))
                    and float(m_row[ind]) > 0.01
                    and float(b_row.get(ind, 50.0)) >= 55.0
                ]
                elig.sort(key=lambda x: float(m_row[x]), reverse=True)
                chosen = elig[:top_k_sectors]
                if chosen:
                    w = tgt_gross / len(chosen)
                    for c in chosen:
                        new_w[c] = w

            turnover = sum(abs(new_w.get(k, 0.0) - cur_weights.get(k, 0.0)) for k in set(new_w) | set(cur_weights))
            if turnover > 1e-6:
                nav *= 1.0 - turnover * etf_cost
                cur_weights = new_w
                rebal_cnt += 1
            days_since_rebal = 0

        # 计算当日组合收益
        day_r = 0.0
        if cur_weights and dt in ind_ret.index:
            in_market_days += 1
            r_row = ind_ret.loc[dt]
            for ind, w in cur_weights.items():
                day_r += w * float(r_row.get(ind, 0.0)) / 100.0
        nav *= 1.0 + day_r
        nav_list.append(nav)

    util_pct = round(in_market_days / max(1, len(dates) - 1) * 100.0, 1)
    return pd.Series(nav_list, index=dates, dtype=np.float64), {
        "rebal_cnt": rebal_cnt,
        "utilization_pct": util_pct,
    }


def _simulate_panic_rebound_etf(
    day_df: pd.DataFrame,
    *,
    mode: str = "reversal",  # direct_15 | reversal_18
    weight: float = 0.60,
    hold_days: int = 5,
    stop_loss: float = -0.06,
    take_profit: float = 0.12,
) -> tuple[pd.Series, dict[str, Any]]:
    """AR2-2：极端恐慌冰点（宽度 <= 15% / 18%）高弹性宽基 ETF（创业板 399006 + 中证1000 000852）反抽引擎。"""
    raw = _ch_query_df(
        """
        SELECT code, toString(trade_date) AS trade_date, open, high, low, close
        FROM stock.kline_daily FINAL
        WHERE code IN ('399006.SZ', '000852.SH') AND trade_date >= '2022-12-01' AND trade_date <= '2026-11-20'
        ORDER BY code, trade_date
        """
    )
    piv_o = raw.pivot(index="trade_date", columns="code", values="open").sort_index()
    piv_c = raw.pivot(index="trade_date", columns="code", values="close").sort_index()
    # 等权组合（创业板 + 中证1000）日收益率与开盘/收盘合成
    ret_o_to_c = ((piv_c / piv_o) - 1.0).mean(axis=1)
    ret_c_to_c = piv_c.pct_change().mean(axis=1)
    ret_c_to_o = ((piv_o / piv_c.shift(1)) - 1.0).mean(axis=1)

    dates = day_df["trade_date"].astype(str).tolist()
    breadth_map = dict(zip(dates, day_df["market_above_ma20_pct"].fillna(50.0).tolist()))

    etf_cost = 0.0006
    in_pos = False
    entry_nav_ref = 1.0
    held_days = 0
    pending_buy = False
    pending_sell = False

    nav = 1.0
    nav_list = [1.0]
    trades_rets: list[float] = []
    in_market_days = 0
    cooldown = 0

    for i in range(1, len(dates)):
        dt = dates[i]
        b_today = breadth_map.get(dt, 50.0)
        r_cc = float(ret_c_to_c.get(dt, 0.0)) if dt in ret_c_to_c.index else 0.0
        r_oc = float(ret_o_to_c.get(dt, 0.0)) if dt in ret_o_to_c.index else 0.0
        r_co = float(ret_c_to_o.get(dt, 0.0)) if dt in ret_c_to_o.index else 0.0

        # 1. 开盘卖出
        if in_pos and pending_sell:
            # 承担昨收到今开跳空
            nav *= 1.0 + weight * r_co - weight * etf_cost
            trade_r = ((entry_nav_ref * (1.0 + r_co)) - 1.0) * 100.0 - 0.12
            trades_rets.append(trade_r)
            in_pos = False
            pending_sell = False
            held_days = 0
            cooldown = 2  # 避免连续两天反复止损

        # 2. 开盘买入
        if not in_pos and pending_buy:
            nav *= 1.0 - weight * etf_cost
            # 买入日承担今开到今收
            nav *= 1.0 + weight * r_oc
            entry_nav_ref = 1.0 + r_oc
            in_pos = True
            pending_buy = False
            held_days = 1
            in_market_days += 1
        elif in_pos:
            nav *= 1.0 + weight * r_cc
            entry_nav_ref *= 1.0 + r_cc
            held_days += 1
            in_market_days += 1

        # 3. 收盘判定明日动作
        if in_pos:
            pnl = entry_nav_ref - 1.0
            if held_days >= hold_days or pnl <= stop_loss or pnl >= take_profit or b_today >= 38.0:
                pending_sell = True
        else:
            if cooldown > 0:
                cooldown -= 1
            else:
                if mode == "direct_15":
                    if b_today <= 15.0:
                        pending_buy = True
                elif mode == "reversal_18":
                    # 极端冰点（宽度 <= 18%）且高弹性宽基当日收红企稳（>= +0.5%）
                    if b_today <= 18.0 and r_cc >= 0.005:
                        pending_buy = True
                elif mode == "direct_12":
                    if b_today <= 12.0:
                        pending_buy = True

        nav_list.append(nav)

    arr = np.array(trades_rets, dtype=np.float64)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    return pd.Series(nav_list, index=dates, dtype=np.float64), {
        "trades": int(len(arr)),
        "win_rate_pct": round(float(len(wins) / len(arr) * 100.0), 1) if len(arr) else 0.0,
        "avg_ret_pct": round(float(arr.mean()), 2) if len(arr) else 0.0,
        "pl_ratio": round(float(wins.mean() / abs(losses.mean())), 2) if len(wins) and len(losses) else 0.0,
        "utilization_pct": round(in_market_days / max(1, len(dates) - 1) * 100.0, 1),
    }


@dataclass
class DynamicPos:
    code: str
    industry: str
    shares: float
    entry_px: float
    peak_close: float
    days_held: int
    pending_sell: bool = False


def _simulate_dynamic_stock_engine(
    day_df: pd.DataFrame,
    by_date_rows: dict[str, pd.DataFrame],
    selector_fn: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    name: str,
    off_slots: int = 5,
    bal_slots: int = 3,
    slot_weight: float = 0.20,
    exit_mode: str = "trail_10",
    hard_stop_pct: float = -0.10,
    one_per_industry: bool = True,
) -> tuple[pd.Series, dict[str, Any]]:
    """AR2-3 / AR2-1B：动态槽位（进攻满载 / 横盘精选 / 防守清仓）高利用率趋势跟踪股票组合仿真。"""
    dates = day_df["trade_date"].astype(str).tolist()
    reg_by_date = dict(zip(dates, day_df["ar1_regime"].tolist()))

    cash = 1.0
    positions: dict[str, DynamicPos] = {}
    nav_list: list[float] = []
    pending_buys: list[dict[str, Any]] = []
    closed_rets: list[float] = []
    closed_days: list[int] = []
    daily_gross_exposure: list[float] = []

    for dt in dates:
        day_tbl = by_date_rows.get(dt)
        reg_today = reg_by_date.get(dt, "balanced")
        if day_tbl is not None and not day_tbl.empty:
            o_map = dict(zip(day_tbl["code"].values, day_tbl["open"].values))
            h_map = dict(zip(day_tbl["code"].values, day_tbl["high"].values))
            l_map = dict(zip(day_tbl["code"].values, day_tbl["low"].values))
            c_map = dict(zip(day_tbl["code"].values, day_tbl["close"].values))
            pc_map = dict(zip(day_tbl["code"].values, day_tbl["prev_close"].values))
            ld_map = dict(zip(day_tbl["code"].values, day_tbl["ld_thresh"].values))
            lu_map = dict(zip(day_tbl["code"].values, day_tbl["lu_thresh"].values))
            ma20_bias_map = dict(zip(day_tbl["code"].values, day_tbl["ma20_bias_pct"].values))
            ma10_bias_map = dict(zip(day_tbl["code"].values, day_tbl["ma10_bias_pct"].values))
        else:
            o_map = h_map = l_map = c_map = pc_map = ld_map = lu_map = ma20_bias_map = ma10_bias_map = {}

        # 1. 开盘卖出
        for code in list(positions.keys()):
            pos = positions[code]
            if not pos.pending_sell:
                continue
            o = o_map.get(code)
            h = h_map.get(code)
            l = l_map.get(code)
            c = c_map.get(code)
            pc = pc_map.get(code)
            ld = ld_map.get(code, 0.905)
            if o is None or c is None or o <= 0:
                continue
            if pc and o == h and h == l and c <= pc * ld:
                continue
            cash += pos.shares * float(o) * (1.0 - SELL_COST)
            ret_pct = (float(o) / pos.entry_px - 1.0) * 100.0 - (BUY_COST + SELL_COST) * 100.0
            closed_rets.append(ret_pct)
            closed_days.append(pos.days_held)
            del positions[code]

        # 2. 开盘买入
        if pending_buys:
            cur_eq_open = cash + sum(p.shares * float(o_map.get(c, p.peak_close)) for c, p in positions.items())
            held_inds = {p.industry for p in positions.values()}
            for cand in pending_buys:
                max_s = cand["max_slots"]
                if len(positions) >= max_s:
                    break
                code = cand["code"]
                ind = cand["industry"]
                if code in positions or (one_per_industry and ind in held_inds):
                    continue
                o = o_map.get(code)
                pc = pc_map.get(code)
                lu = lu_map.get(code, 1.095)
                if o is None or o <= 0:
                    continue
                if pc and float(o) >= float(pc) * float(lu):
                    continue
                alloc = min(cash, cur_eq_open * slot_weight)
                if alloc < cur_eq_open * 0.05:
                    continue
                shares = (alloc * (1.0 - BUY_COST)) / float(o)
                cash -= alloc
                positions[code] = DynamicPos(code=code, industry=ind, shares=shares, entry_px=float(o), peak_close=float(o), days_held=0)
                held_inds.add(ind)
            pending_buys = []

        # 3. 收盘估净值与出场判定
        mv_today = 0.0
        for code, pos in positions.items():
            c = c_map.get(code)
            c_f = float(c) if (c is not None and c > 0) else pos.peak_close
            pos.days_held += 1
            if c_f > pos.peak_close:
                pos.peak_close = c_f
            mv_today += pos.shares * c_f

            pnl = c_f / pos.entry_px - 1.0
            dd_peak = c_f / pos.peak_close - 1.0
            trigger_sell = False
            if reg_today in ("defensive", "extreme_panic"):
                trigger_sell = True
            elif pnl <= hard_stop_pct:
                trigger_sell = True
            elif exit_mode == "trail_10":
                if dd_peak <= -0.10:
                    trigger_sell = True
                elif pos.days_held >= 15:
                    b20 = ma20_bias_map.get(code)
                    if b20 is not None and float(b20) < 0.0:
                        trigger_sell = True
            elif exit_mode == "trail_12":
                if dd_peak <= -0.12:
                    trigger_sell = True
            elif exit_mode == "ma20":
                b20 = ma20_bias_map.get(code)
                if pos.days_held >= 2 and b20 is not None and float(b20) < 0.0:
                    trigger_sell = True
            elif exit_mode == "ma10":
                b10 = ma10_bias_map.get(code)
                if pos.days_held >= 2 and b10 is not None and float(b10) < 0.0:
                    trigger_sell = True

            if trigger_sell:
                pos.pending_sell = True

        nav_today = cash + mv_today
        nav_list.append(nav_today)
        daily_gross_exposure.append(mv_today / nav_today if nav_today > 0 else 0.0)

        # 4. 收盘后按明日目标槽位数生成候选
        target_slots = off_slots if reg_today in ("offensive", "extreme_offensive") else (bal_slots if reg_today == "balanced" else 0)
        live_cnt = sum(1 for p in positions.values() if not p.pending_sell)
        if target_slots > live_cnt and day_tbl is not None and not day_tbl.empty:
            picked_df = selector_fn(day_tbl)
            if picked_df is not None and not picked_df.empty:
                for _, r in picked_df.head(target_slots * 3).iterrows():
                    pending_buys.append({"code": str(r["code"]), "industry": str(r.get("industry") or ""), "max_slots": target_slots})

    arr = np.array(closed_rets, dtype=np.float64)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    return pd.Series(nav_list, index=dates, dtype=np.float64), {
        "trades": int(len(arr)),
        "avg_hold": round(float(np.mean(closed_days)), 1) if closed_days else 0.0,
        "win_rate_pct": round(float(len(wins) / len(arr) * 100.0), 1) if len(arr) else 0.0,
        "pl_ratio": round(float(wins.mean() / abs(losses.mean())), 2) if len(wins) and len(losses) else 0.0,
        "avg_exposure_pct": round(float(np.mean(daily_gross_exposure) * 100.0), 1),
    }


def main() -> int:
    print("加载全市场 4,995 股 Parquet 特征宽表...")
    panel_df, bench_df = build_or_load_feature_panel(start_date="2023-01-01", min_amt_20d=50_000_000.0)
    day_df = _build_ar1_regimes(panel_df, bench_df)
    sp = split_trade_dates(day_df["trade_date"].astype(str).tolist())

    panel_df = panel_df.copy()
    # 补齐来自 stock.sector_stocks + stock.sectors 的真实 145 行业归属并重算截面行业强弱
    sec_map_df = _ch_query_df(
        """
        SELECT ss.stock_code AS code, any(s.name) AS ind_name
        FROM stock.sector_stocks ss
        INNER JOIN stock.sectors s ON ss.sector_code = s.code
        WHERE s.type = 'industry' AND length(trim(s.name)) > 0
        GROUP BY ss.stock_code
        """
    )
    real_ind_map = dict(zip(sec_map_df["code"].astype(str), sec_map_df["ind_name"].astype(str)))
    panel_df["industry"] = panel_df["code"].map(real_ind_map).fillna("未分类")
    panel_df["above_ma20_tmp"] = (panel_df["ma20_bias_pct"] > 0.0).astype(np.float32)
    ind_daily = (
        panel_df.groupby(["trade_date", "industry"], as_index=False)
        .agg(
            ind_ret_5d=("ret_5d_pct", "mean"),
            industry_above_ma20_pct=("above_ma20_tmp", "mean"),
        )
    )
    ind_daily["industry_above_ma20_pct"] = (ind_daily["industry_above_ma20_pct"] * 100.0).astype(np.float32)
    ind_daily["industry_ret_5d_rank"] = ind_daily.groupby("trade_date")["ind_ret_5d"].rank(pct=True).astype(np.float32)
    panel_df.drop(columns=["industry_ret_5d_rank", "industry_above_ma20_pct", "above_ma20_tmp"], inplace=True, errors="ignore")
    panel_df = panel_df.merge(
        ind_daily[["trade_date", "industry", "industry_ret_5d_rank", "industry_above_ma20_pct"]],
        on=["trade_date", "industry"],
        how="left",
    )
    panel_df["lu_thresh"] = panel_df["code"].map(lambda c: 1.192 if c.startswith(("300", "301", "688", "689")) else 1.095).astype(np.float32)

    keep_cols = [
        "trade_date", "code", "industry", "open", "high", "low", "close", "prev_close",
        "ld_thresh", "lu_thresh", "amt_20d_yi", "ret_1d_pct", "ret_5d_pct", "ret_20d_pct",
        "rs_5d_vs_hs300", "rs_20d_vs_hs300", "ma_bull_align", "ma10_bias_pct", "ma20_bias_pct",
        "ma60_bias_pct", "pos_20d", "pos_60d", "dist_to_hi20_pct", "vol_ratio_5d", "vol_ratio_20d",
        "close_pos_in_day", "upper_shadow_pct", "atr_14_pct", "industry_ret_5d_rank", "industry_above_ma20_pct",
    ]
    by_date_rows = {dt: grp for dt, grp in panel_df[keep_cols].groupby("trade_date", sort=False)}

    def pack(label: str, nav: pd.Series, meta: dict[str, Any]) -> dict[str, Any]:
        segs = {sk: _eval_nav(nav[(nav.index >= d0) & (nav.index <= d1)]) for sk, (d0, d1) in sp.items()}
        return {"label": label, "full": _eval_nav(nav), "segments": segs, "meta": meta}

    hs_nav = pd.Series(
        day_df["hs300_close"].astype(float).values / float(day_df["hs300_close"].iloc[0]),
        index=day_df["trade_date"].astype(str).values,
    )
    etf_nav, etf_m = _simulate_broad_etf_top2(day_df)

    # --- AR2-1A: 行业主线 ETF 轮动（进攻+横盘双吃） ---
    sec_etf_20, sec_m20 = _simulate_sector_etf_rotation(panel_df, day_df, top_k_sectors=2, rebal_every=5, off_weight=0.90, bal_weight=0.60, mom_window=20)
    sec_etf_10, sec_m10 = _simulate_sector_etf_rotation(panel_df, day_df, top_k_sectors=2, rebal_every=5, off_weight=0.90, bal_weight=0.60, mom_window=10)
    sec_etf_all, sec_mall = _simulate_sector_etf_rotation(panel_df, day_df, top_k_sectors=3, rebal_every=5, off_weight=0.95, bal_weight=0.70, mom_window=20)

    # --- AR2-2: 极端冰点高弹性宽基 ETF 反抽 ---
    panic_rev18, p_m1 = _simulate_panic_rebound_etf(day_df, mode="reversal_18", weight=0.60, hold_days=5)
    panic_dir15, p_m2 = _simulate_panic_rebound_etf(day_df, mode="direct_15", weight=0.60, hold_days=5)
    panic_dir12, p_m3 = _simulate_panic_rebound_etf(day_df, mode="direct_12", weight=0.70, hold_days=5)

    # --- AR2-3 & AR2-1B: 高资金利用率动态个股引擎（进攻满载 + 横盘精选） ---
    def sel_ar2_lowvol_wide(df: pd.DataFrame) -> pd.DataFrame:
        """高利用率低波机构慢牛：均额>=2.5亿 + 均线多头 + 站稳MA60 + 振幅<=4.8% + 5日未暴涨"""
        m = (
            (df["amt_20d_yi"] >= 2.5)
            & (df["ma_bull_align"] == 1.0)
            & (df["ma60_bias_pct"] >= 1.0)
            & (df["rs_20d_vs_hs300"] >= 3.0)
            & (df["atr_14_pct"] <= 4.8)
            & (df["ret_5d_pct"] >= -2.0)
            & (df["ret_5d_pct"] <= 10.0)
        )
        return df.loc[m].sort_values(["industry_ret_5d_rank", "rs_20d_vs_hs300"], ascending=[False, False])

    def sel_ar2_sector_lowvol(df: pd.DataFrame) -> pd.DataFrame:
        """主线强势行业 + 低波机构中军（行业前25%且行业站上MA20>=60% + 个股均额>=2.5亿 + 振幅<=5.0%）"""
        m = (
            (df["industry_ret_5d_rank"] >= 0.75)
            & (df["industry_above_ma20_pct"] >= 60.0)
            & (df["amt_20d_yi"] >= 2.5)
            & (df["ma_bull_align"] == 1.0)
            & (df["ma60_bias_pct"] >= 0.0)
            & (df["atr_14_pct"] <= 5.0)
            & (df["ret_5d_pct"] <= 10.0)
        )
        return df.loc[m].sort_values(["industry_ret_5d_rank", "rs_20d_vs_hs300"], ascending=[False, False])

    def sel_ar2_sector_elastic(df: pd.DataFrame) -> pd.DataFrame:
        """主线强势行业 + 弹性趋势龙头（行业前20% + 均额>=3亿 + 20日超额>=5% + 放量收强）"""
        m = (
            (df["industry_ret_5d_rank"] >= 0.80)
            & (df["industry_above_ma20_pct"] >= 65.0)
            & (df["amt_20d_yi"] >= 3.0)
            & (df["ma_bull_align"] == 1.0)
            & (df["rs_20d_vs_hs300"] >= 5.0)
            & (df["ret_5d_pct"] <= 12.0)
            & (df["close_pos_in_day"] >= 0.60)
        )
        return df.loc[m].sort_values("rs_20d_vs_hs300", ascending=False)

    stk_lowvol_t10, s_m1 = _simulate_dynamic_stock_engine(day_df, by_date_rows, sel_ar2_lowvol_wide, name="AR2_3A_LowVolWide_5s_T10", off_slots=5, bal_slots=3, slot_weight=0.20, exit_mode="trail_10")
    stk_lowvol_ma20, s_m2 = _simulate_dynamic_stock_engine(day_df, by_date_rows, sel_ar2_lowvol_wide, name="AR2_3B_LowVolWide_5s_MA20", off_slots=5, bal_slots=3, slot_weight=0.20, exit_mode="ma20")
    stk_sec_lv_t10, s_m3 = _simulate_dynamic_stock_engine(day_df, by_date_rows, sel_ar2_sector_lowvol, name="AR2_1B_SecLowVol_5s_T10", off_slots=5, bal_slots=3, slot_weight=0.20, exit_mode="trail_10")
    stk_sec_lv_ma20, s_m4 = _simulate_dynamic_stock_engine(day_df, by_date_rows, sel_ar2_sector_lowvol, name="AR2_1B_SecLowVol_5s_MA20", off_slots=5, bal_slots=3, slot_weight=0.20, exit_mode="ma20")
    stk_sec_el_t12, s_m5 = _simulate_dynamic_stock_engine(day_df, by_date_rows, sel_ar2_sector_elastic, name="AR2_1C_SecElastic_5s_T12", off_slots=5, bal_slots=2, slot_weight=0.20, exit_mode="trail_12")

    # --- AR2-4: 终极全天候多引擎无缝叠加（时间天然互补：进攻/横盘引擎 + 冰点反抽引擎） ---
    # 找出最强的进攻/横盘引擎和最强的冰点反抽引擎
    off_bal_pool = {
        "AR2_1A_SecETF_Top2_M20": sec_etf_20,
        "AR2_1A_SecETF_Top2_M10": sec_etf_10,
        "AR2_1A_SecETF_Top3_M20": sec_etf_all,
        "AR2_3A_LowVolWide_5s_T10": stk_lowvol_t10,
        "AR2_3B_LowVolWide_5s_MA20": stk_lowvol_ma20,
        "AR2_1B_SecLowVol_5s_T10": stk_sec_lv_t10,
        "AR2_1B_SecLowVol_5s_MA20": stk_sec_lv_ma20,
        "AR2_1C_SecElastic_5s_T12": stk_sec_el_t12,
    }
    panic_pool = {
        "AR2_2A_Panic_Rev18": panic_rev18,
        "AR2_2B_Panic_Dir15": panic_dir15,
        "AR2_2C_Panic_Dir12": panic_dir12,
    }

    best_ob_name = max(off_bal_pool, key=lambda k: _eval_nav(off_bal_pool[k])["cum_ret_pct"])
    best_p_name = max(panic_pool, key=lambda k: _eval_nav(panic_pool[k])["cum_ret_pct"])

    # 由于进攻/横盘引擎在防守/冰点期空仓（0%暴露），而冰点反抽引擎只在冰点期出击（进攻/横盘0%暴露），
    # 两者日收益率可直接 1:1 资金复用叠加（总仓位绝不超过 100%！）
    r_ob = off_bal_pool[best_ob_name].pct_change().fillna(0.0)
    r_sec = sec_etf_20.pct_change().fillna(0.0)
    r_lv = stk_lowvol_t10.pct_change().fillna(0.0)
    r_p = panic_pool[best_p_name].pct_change().fillna(0.0)

    combo_best_plus_panic = (1.0 + r_ob + r_p).cumprod()
    combo_mix_plus_panic = (1.0 + 0.5 * r_sec + 0.5 * r_lv + r_p).cumprod()

    all_results = [
        pack("B0_HS300_BuyHold", hs_nav, {}),
        pack("AR1_v2_BroadETF_Top2", etf_nav, etf_m),
        pack("AR2_1A_SecETF_Top2_M20", sec_etf_20, sec_m20),
        pack("AR2_1A_SecETF_Top2_M10", sec_etf_10, sec_m10),
        pack("AR2_1A_SecETF_Top3_M20", sec_etf_all, sec_mall),
        pack("AR2_2A_Panic_Rev18", panic_rev18, p_m1),
        pack("AR2_2B_Panic_Dir15", panic_dir15, p_m2),
        pack("AR2_2C_Panic_Dir12", panic_dir12, p_m3),
        pack("AR2_3A_LowVolWide_5s_T10", stk_lowvol_t10, s_m1),
        pack("AR2_3B_LowVolWide_5s_MA20", stk_lowvol_ma20, s_m2),
        pack("AR2_1B_SecLowVol_5s_T10", stk_sec_lv_t10, s_m3),
        pack("AR2_1B_SecLowVol_5s_MA20", stk_sec_lv_ma20, s_m4),
        pack("AR2_1C_SecElastic_5s_T12", stk_sec_el_t12, s_m5),
        pack(f"AR2_4_ALLWEATHER_{best_ob_name}+{best_p_name}", combo_best_plus_panic, {"ob": best_ob_name, "panic": best_p_name}),
        pack(f"AR2_4_MIX_SecETF_LowVol+{best_p_name}", combo_mix_plus_panic, {"ob": "50%SecETF+50%LowVol", "panic": best_p_name}),
    ]

    all_results.sort(key=lambda x: x["full"]["ann_ret_pct"], reverse=True)
    out_path = runtime_path("mining_runs") / "ar2_high_return_experiment_v1.json"
    out_path.write_text(json.dumps({"splits": sp, "results": all_results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=" * 124)
    print(f"AR2 高收益增强实证排行榜（按全样本年化降序 | IS[{sp['is'][0]}~{sp['is'][1]}] | Val[{sp['val'][0]}~{sp['val'][1]}] | OOS[{sp['oos'][0]}~{sp['oos'][1]}]）")
    print("=" * 124)
    for idx, r in enumerate(all_results, 1):
        f = r["full"]
        s_is, s_val, s_oos = r["segments"]["is"], r["segments"]["val"], r["segments"]["oos"]
        print(
            f"#{idx:02d} {r['label']:46s} | FULL: 累计{f['cum_ret_pct']:+6.1f}% 年化{f['ann_ret_pct']:+5.1f}% 回撤{f['max_dd_pct']:4.1f}% 夏普{f['sharpe']:4.2f} 卡玛{f['calmar']:4.2f}"
        )
        print(
            f"    IS={s_is['cum_ret_pct']:+6.1f}%(DD{s_is['max_dd_pct']:4.1f}%) | "
            f"Val={s_val['cum_ret_pct']:+6.1f}%(DD{s_val['max_dd_pct']:4.1f}%) | "
            f"OOS={s_oos['cum_ret_pct']:+6.1f}%(DD{s_oos['max_dd_pct']:4.1f}%) | {r['meta']}"
        )
    print("=" * 124)
    print(f"报告已落盘：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
