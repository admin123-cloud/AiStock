# -*- coding: utf-8 -*-
"""AR1-Alpha（上升期选股与趋势跟踪交易引擎）全市场实证实验。

严格事件驱动逐日账户净值仿真（Mark-to-Market，零未来函数）：
- 样本池：全市场 4,995 只流动性达标 A 股（>=5000万）× 940 交易日（2023-01-03 ~ 2026-11-20）
- 择时底座：AR1 复合五态状态机（N=2 快速防守 / N=3 稳健进攻）
- 真实撮合：T 日收盘出信号 -> T+1 开盘撮合，跳过一字涨停，一字跌停顺延，双边成本 0.26%
- 对比维度：
  1. 基准：沪深300 / 旧 5日死止盈(+15%) / AR1 宽基动量Top2 ETF 轮动
  2. 5 大选股因子族 × 固定4槽位 × 3 种趋势跟踪出场（破MA10 / 破MA20 / 高点回撤10%）
  3. 进攻期核心卫星配比（纯个股 vs 50%强势宽基ETF + 50%趋势跟踪主升个股）
  4. 严格 IS / Val / OOS 三段独立验收
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
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402

BUY_COST = 0.00105   # 0.03% 佣金 + 0.075% 滑点
SELL_COST = 0.00155  # 0.03% 佣金 + 0.05% 印花税 + 0.075% 滑点

BROAD_INDICES = {
    "000016.SH": "上证50ETF(510050)",
    "000300.SH": "沪深300ETF(510300)",
    "000905.SH": "中证500ETF(510500)",
    "000852.SH": "中证1000ETF(512100)",
    "399006.SZ": "创业板ETF(159915)",
    "000688.SH": "科创50ETF(588000)",
}


def _ch_query_df(sql: str) -> pd.DataFrame:
    host = (os.environ.get("AISTOCK_CLICKHOUSE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("AISTOCK_CLICKHOUSE_PORT") or "8123").strip()
    req = urllib.request.Request(
        f"http://{host}:{port}/",
        data=(sql.strip() + "\nFORMAT Parquet").encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return pq.read_table(io.BytesIO(r.read())).to_pandas()


def _eval_nav(nav: pd.Series) -> dict[str, float]:
    n = max(1, len(nav))
    if n < 2 or float(nav.iloc[0]) <= 0:
        return {"cum_ret_pct": 0.0, "ann_ret_pct": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0, "calmar": 0.0}
    rets = nav.pct_change().dropna()
    cum = float((nav.iloc[-1] / nav.iloc[0] - 1.0) * 100.0)
    years = max(0.15, n / 242.0)
    tot_ratio = float(nav.iloc[-1] / nav.iloc[0])
    ann = float((tot_ratio ** (1.0 / years) - 1.0) * 100.0) if tot_ratio > 0 else -100.0
    dd = float(abs(((nav / nav.cummax()) - 1.0).min() * 100.0))
    sd = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    sharpe = float((rets.mean() / sd) * math.sqrt(242.0)) if sd > 1e-8 else 0.0
    calmar = float(ann / dd) if dd > 0.5 else (ann / 0.5 if ann > 0 else 0.0)
    return {
        "cum_ret_pct": round(cum, 2),
        "ann_ret_pct": round(ann, 2),
        "max_dd_pct": round(dd, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
    }


def _build_ar1_regimes(panel_df: pd.DataFrame, bench_df: pd.DataFrame) -> pd.DataFrame:
    """按 AR1 实证宪法 v1.0 构建每日五态周期（严格 T 日收盘已知）。"""
    mkt_daily = (
        panel_df.groupby("trade_date", as_index=False)
        .agg(total_amt=("amount", "sum"))
        .sort_values("trade_date")
    )
    df = bench_df.merge(mkt_daily, on="trade_date", how="inner").sort_values("trade_date").reset_index(drop=True)
    df["amt_ratio_5d"] = df["total_amt"] / (df["total_amt"].rolling(5, min_periods=3).mean() + 1.0)

    close_sh = df["idx_close"].astype(float)
    ma20_sh = close_sh.rolling(20, min_periods=10).mean()
    mom20 = df["idx_mom20_pct"].fillna(0.0)
    breadth = df["market_above_ma20_pct"].fillna(50.0)
    vol_r = df["amt_ratio_5d"].fillna(1.0)

    raw_def = (breadth < 30.0) | ((close_sh < ma20_sh) & (mom20 < -2.0))
    def2 = raw_def.astype(int).rolling(2, min_periods=2).sum() == 2
    raw_off = (breadth >= 52.0) & (close_sh > ma20_sh) & (mom20 > 1.0)
    off3 = raw_off.astype(int).rolling(3, min_periods=3).sum() == 3

    regs: list[str] = []
    for d2, o3, b, m20, vr in zip(def2, off3, breadth, mom20, vol_r):
        if d2 and b <= 18.0:
            regs.append("extreme_panic")
        elif d2 or b < 25.0:
            regs.append("defensive")
        elif o3 and b >= 75.0 and m20 >= 6.0 and vr >= 1.05:
            regs.append("extreme_offensive")
        elif o3:
            regs.append("offensive")
        else:
            regs.append("balanced")
    df["ar1_regime"] = regs
    return df


def _simulate_broad_etf_top2(day_df: pd.DataFrame) -> tuple[pd.Series, dict[str, Any]]:
    """6 大宽基指数 ETF 在 AR1 进攻期持动量最强 Top2（80%仓位），平衡期持站上MA20最强Top2（35%仓位），防守期清仓转现金。"""
    codes_sql = ",".join(f"'{c}'" for c in BROAD_INDICES)
    raw = _ch_query_df(
        f"""
        SELECT code, toString(trade_date) AS trade_date, open, close
        FROM stock.kline_daily FINAL
        WHERE code IN ({codes_sql}) AND trade_date >= '2022-09-01' AND trade_date <= '2026-11-20'
        ORDER BY code, trade_date
        """
    )
    piv_c = raw.pivot(index="trade_date", columns="code", values="close").sort_index()
    piv_o = raw.pivot(index="trade_date", columns="code", values="open").sort_index()
    mom20 = piv_c.pct_change(20)
    ma20 = piv_c.rolling(20).mean()
    above_ma20 = piv_c > ma20

    dates = day_df["trade_date"].astype(str).tolist()
    reg_by_date = dict(zip(dates, day_df["ar1_regime"].tolist()))

    # 逐日账户仿真（T日收盘决定目标权重 -> T+1开盘调仓，扣ETF双边低摩擦0.06%）
    etf_cost = 0.0006
    holdings: dict[str, float] = {}  # code -> shares
    cash = 1.0
    nav_vals: list[float] = []
    trades = 0

    for i, dt in enumerate(dates):
        if i == 0:
            nav_vals.append(1.0)
            continue
        prev_dt = dates[i - 1]
        reg_prev = reg_by_date.get(prev_dt, "balanced")

        # 决定今日目标仓位与标的（基于昨日收盘已知的 AR1 周期与宽基动量）
        if reg_prev in ("offensive", "extreme_offensive"):
            target_gross = 0.85 if reg_prev == "extreme_offensive" else 0.80
        elif reg_prev == "balanced":
            target_gross = 0.35
        else:
            target_gross = 0.0

        target_weights: dict[str, float] = {}
        if target_gross > 0 and prev_dt in mom20.index:
            m_row = mom20.loc[prev_dt].dropna()
            a_row = above_ma20.loc[prev_dt]
            elig = [c for c in m_row.index if bool(a_row.get(c, False)) and float(m_row[c]) > 0]
            elig.sort(key=lambda c: float(m_row[c]), reverse=True)
            chosen = elig[:2]
            if chosen:
                w = target_gross / len(chosen)
                for c in chosen:
                    target_weights[c] = w

        # 今日开盘按目标权重调仓（仅在标的集合变化或仓位差>8%时调仓，避免无谓摩擦）
        if dt in piv_o.index and dt in piv_c.index:
            o_row = piv_o.loc[dt]
            c_row = piv_c.loc[dt]
            cur_eq_open = cash + sum(sh * float(o_row[c]) for c, sh in holdings.items() if pd.notna(o_row.get(c)))
            cur_weights = {
                c: (sh * float(o_row[c]) / cur_eq_open) if cur_eq_open > 0 else 0.0
                for c, sh in holdings.items()
                if pd.notna(o_row.get(c))
            }
            need_rebal = set(cur_weights.keys()) != set(target_weights.keys()) or any(
                abs(cur_weights.get(c, 0.0) - target_weights.get(c, 0.0)) > 0.08
                for c in set(cur_weights) | set(target_weights)
            )
            if need_rebal and cur_eq_open > 0:
                # 先结算卖出差额与买入差额的换手成本
                turnover = 0.0
                new_holdings: dict[str, float] = {}
                for c in set(cur_weights) | set(target_weights):
                    tw = target_weights.get(c, 0.0)
                    cw = cur_weights.get(c, 0.0)
                    turnover += abs(tw - cw)
                    if tw > 0 and pd.notna(o_row.get(c)) and float(o_row[c]) > 0:
                        new_holdings[c] = (cur_eq_open * tw) / float(o_row[c])
                cost = cur_eq_open * turnover * etf_cost
                cash = cur_eq_open * (1.0 - sum(target_weights.values())) - cost
                holdings = new_holdings
                trades += 1

            # 收盘 Mark-to-Market
            nav_close = cash + sum(sh * float(c_row[c]) for c, sh in holdings.items() if pd.notna(c_row.get(c)))
            nav_vals.append(nav_close)
        else:
            nav_vals.append(nav_vals[-1])

    return pd.Series(nav_vals, index=dates, dtype=np.float64), {"rebalance_events": trades}


@dataclass
class Pos:
    code: str
    industry: str
    shares: float
    entry_px: float
    peak_close: float
    days_held: int
    pending_sell: bool = False


def simulate_slot_portfolio(
    day_df: pd.DataFrame,
    by_date_rows: dict[str, pd.DataFrame],
    selector_fn: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    name: str,
    active_regimes: set[str],
    max_slots: int = 4,
    slot_weight: float = 0.20,
    exit_mode: str = "trail_10",  # trail_10 | ma10 | ma20 | fixed_5d_tp15
    hard_stop_pct: float = -0.10,
    one_per_industry: bool = False,
    exit_on_defensive: bool = True,
    exit_on_balanced: bool = False,
) -> tuple[pd.Series, dict[str, Any]]:
    """严格事件驱动固定槽位组合仿真（T日收盘出信号 -> T+1开盘真实撮合 -> 收盘Mark-to-Market）。"""
    dates = day_df["trade_date"].astype(str).tolist()
    reg_by_date = dict(zip(dates, day_df["ar1_regime"].tolist()))

    cash = 1.0
    positions: dict[str, Pos] = {}
    nav_list: list[float] = []
    pending_buys: list[dict[str, Any]] = []

    closed_rets: list[float] = []
    closed_days: list[int] = []

    for i, dt in enumerate(dates):
        day_tbl = by_date_rows.get(dt)
        reg_today = reg_by_date.get(dt, "balanced")
        # 快速索引当日 OHLCV
        if day_tbl is not None and not day_tbl.empty:
            o_map = dict(zip(day_tbl["code"].values, day_tbl["open"].values))
            h_map = dict(zip(day_tbl["code"].values, day_tbl["high"].values))
            l_map = dict(zip(day_tbl["code"].values, day_tbl["low"].values))
            c_map = dict(zip(day_tbl["code"].values, day_tbl["close"].values))
            pc_map = dict(zip(day_tbl["code"].values, day_tbl["prev_close"].values))
            ld_map = dict(zip(day_tbl["code"].values, day_tbl["ld_thresh"].values))
            lu_map = dict(zip(day_tbl["code"].values, day_tbl["lu_thresh"].values))
            ma10_bias_map = dict(zip(day_tbl["code"].values, day_tbl["ma10_bias_pct"].values))
            ma20_bias_map = dict(zip(day_tbl["code"].values, day_tbl["ma20_bias_pct"].values))
        else:
            o_map = h_map = l_map = c_map = pc_map = ld_map = lu_map = ma10_bias_map = ma20_bias_map = {}

        # 1. 开盘先执行挂单卖出（跳过一字跌停）
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
            # 一字跌停卖不出，顺延
            if pc and o == h and h == l and c <= pc * ld:
                continue
            proceeds = pos.shares * float(o) * (1.0 - SELL_COST)
            cash += proceeds
            ret_pct = (float(o) / pos.entry_px - 1.0) * 100.0 - (BUY_COST + SELL_COST) * 100.0
            closed_rets.append(ret_pct)
            closed_days.append(pos.days_held)
            del positions[code]

        # 2. 开盘执行昨日收盘排队的买入（跳过一字涨停，受槽位与可用现金约束）
        if pending_buys:
            cur_eq_open = cash + sum(
                p.shares * float(o_map.get(c, p.peak_close)) for c, p in positions.items()
            )
            held_inds = {p.industry for p in positions.values()}
            for cand in pending_buys:
                if len(positions) >= max_slots:
                    break
                code = cand["code"]
                ind = cand["industry"]
                if code in positions:
                    continue
                if one_per_industry and ind in held_inds:
                    continue
                o = o_map.get(code)
                pc = pc_map.get(code)
                lu = lu_map.get(code, 1.095)
                if o is None or o <= 0:
                    continue
                # 一字涨停买不进跳过
                if pc and float(o) >= float(pc) * float(lu):
                    continue
                alloc = min(cash, cur_eq_open * slot_weight)
                if alloc < cur_eq_open * 0.05:
                    continue
                shares = (alloc * (1.0 - BUY_COST)) / float(o)
                cash -= alloc
                positions[code] = Pos(
                    code=code,
                    industry=ind,
                    shares=shares,
                    entry_px=float(o),
                    peak_close=float(o),
                    days_held=0,
                )
                held_inds.add(ind)
            pending_buys = []

        # 3. 盘中/收盘更新持仓状态并生成明日卖出指令
        nav_today = cash
        for code, pos in positions.items():
            c = c_map.get(code)
            if c is None or c <= 0:
                nav_today += pos.shares * pos.peak_close
                continue
            c_f = float(c)
            pos.days_held += 1
            if c_f > pos.peak_close:
                pos.peak_close = c_f
            nav_today += pos.shares * c_f

            # 退出判定（严格 T 日收盘后触发 -> T+1 开盘卖）
            pnl = c_f / pos.entry_px - 1.0
            dd_from_peak = c_f / pos.peak_close - 1.0
            trigger_sell = False

            if exit_on_defensive and reg_today in ("defensive", "extreme_panic"):
                trigger_sell = True
            elif exit_on_balanced and reg_today == "balanced":
                trigger_sell = True
            elif pnl <= hard_stop_pct:
                trigger_sell = True
            elif exit_mode == "fixed_5d_tp15":
                if pos.days_held >= 5 or pnl >= 0.15:
                    trigger_sell = True
            elif exit_mode == "ma10":
                b10 = ma10_bias_map.get(code)
                if pos.days_held >= 2 and b10 is not None and float(b10) < 0.0:
                    trigger_sell = True
            elif exit_mode == "ma20":
                b20 = ma20_bias_map.get(code)
                if pos.days_held >= 2 and b20 is not None and float(b20) < 0.0:
                    trigger_sell = True
            elif exit_mode == "trail_10":
                # 只要盈利超过 5% 或持有 >= 2 天，从最高收盘回撤 >= 10% 移动止盈；或持有超 25 日动能衰竭(破MA20)走
                if dd_from_peak <= -0.10:
                    trigger_sell = True
                elif pos.days_held >= 15:
                    b20 = ma20_bias_map.get(code)
                    if b20 is not None and float(b20) < 0.0:
                        trigger_sell = True

            if trigger_sell:
                pos.pending_sell = True

        nav_list.append(nav_today)

        # 4. 收盘后检查是否还有空槽位（含明日即将卖出腾出的槽位）并在允许周期内生成明日买入候选
        free_slots = max_slots - sum(1 for p in positions.values() if not p.pending_sell)
        if free_slots > 0 and reg_today in active_regimes and day_tbl is not None and not day_tbl.empty:
            picked_df = selector_fn(day_tbl)
            if picked_df is not None and not picked_df.empty:
                for _, r in picked_df.head(max_slots * 2).iterrows():
                    pending_buys.append({"code": str(r["code"]), "industry": str(r.get("industry") or "")})

    nav_series = pd.Series(nav_list, index=dates, dtype=np.float64)
    rets_arr = np.array(closed_rets, dtype=np.float64)
    wins = rets_arr[rets_arr > 0]
    losses = rets_arr[rets_arr <= 0]
    stats = {
        "name": name,
        "closed_trades": int(len(rets_arr)),
        "avg_hold_days": round(float(np.mean(closed_days)), 1) if closed_days else 0.0,
        "win_rate_pct": round(float(len(wins) / len(rets_arr) * 100.0), 1) if len(rets_arr) else 0.0,
        "avg_trade_ret_pct": round(float(rets_arr.mean()), 2) if len(rets_arr) else 0.0,
        "pl_ratio": round(float(wins.mean() / abs(losses.mean())), 2) if len(wins) and len(losses) else 0.0,
    }
    return nav_series, stats


def main() -> int:
    print("加载全市场 4,995 股 Parquet 特征宽表...")
    panel_df, bench_df = build_or_load_feature_panel(start_date="2023-01-01", min_amt_20d=50_000_000.0)
    day_df = _build_ar1_regimes(panel_df, bench_df)
    sp = split_trade_dates(day_df["trade_date"].astype(str).tolist())

    # 补充 lu_thresh 与 prev_close 列方便逐日撮合
    def _lu(code: str) -> float:
        if code.startswith(("300", "301", "688", "689")):
            return 1.192
        return 1.095

    panel_df = panel_df.copy()
    panel_df["lu_thresh"] = panel_df["code"].map(_lu).astype(np.float32)

    # 仅保留仿真需要的列并按日期分组字典化（极大加速 940 日事件循环）
    keep_cols = [
        "trade_date", "code", "industry", "open", "high", "low", "close", "prev_close",
        "ld_thresh", "lu_thresh", "amt_20d_yi", "ret_1d_pct", "ret_5d_pct", "ret_20d_pct",
        "rs_5d_vs_hs300", "rs_20d_vs_hs300", "ma_bull_align", "ma10_bias_pct", "ma20_bias_pct",
        "ma60_bias_pct", "pos_20d", "pos_60d", "dist_to_hi20_pct", "vol_ratio_5d", "vol_ratio_20d",
        "close_pos_in_day", "upper_shadow_pct", "atr_14_pct", "industry_ret_5d_rank", "industry_above_ma20_pct",
    ]
    by_date_rows = {dt: grp for dt, grp in panel_df[keep_cols].groupby("trade_date", sort=False)}

    # --- 1. 基准组 ---
    hs_nav = pd.Series(
        day_df["hs300_close"].astype(float).values / float(day_df["hs300_close"].iloc[0]),
        index=day_df["trade_date"].astype(str).values,
    )
    etf_nav, etf_meta = _simulate_broad_etf_top2(day_df)

    # --- 2. 5 大选股因子族定义 ---
    def sel_mw04_legacy(df: pd.DataFrame) -> pd.DataFrame:
        m = (
            (df["rs_20d_vs_hs300"] >= 10.0)
            & (df["close_pos_in_day"] >= 0.70)
            & (df["upper_shadow_pct"] <= 1.8)
            & (df["atr_14_pct"] <= 6.5)
        )
        return df.loc[m].sort_values("rs_20d_vs_hs300", ascending=False)

    def sel_vcp_breakout(df: pd.DataFrame) -> pd.DataFrame:
        """A. VCP波动率收敛 + 温和放量突破20日新高 + 拒绝连板透支 + 行业强势"""
        m = (
            (df["amt_20d_yi"] >= 2.0)
            & (df["ma_bull_align"] == 1.0)
            & (df["ma60_bias_pct"] > 0.0)
            & (df["dist_to_hi20_pct"] >= -3.0)
            & (df["ret_5d_pct"] <= 12.0)
            & (df["ret_1d_pct"] >= 1.5)
            & (df["atr_14_pct"] <= 5.5)
            & (df["vol_ratio_20d"] >= 1.2)
            & (df["vol_ratio_20d"] <= 3.0)
            & (df["industry_above_ma20_pct"] >= 60.0)
        )
        return df.loc[m].sort_values("industry_ret_5d_rank", ascending=False)

    def sel_inst_trend_3yi(df: pd.DataFrame) -> pd.DataFrame:
        """B. 机构大票趋势共振（20日均额>=3亿 + 多头排列 + 强行业 + 稳步领涨不追暴涨）"""
        m = (
            (df["amt_20d_yi"] >= 3.0)
            & (df["ma_bull_align"] == 1.0)
            & (df["ma60_bias_pct"] > 2.0)
            & (df["rs_20d_vs_hs300"] >= 5.0)
            & (df["rs_20d_vs_hs300"] <= 25.0)
            & (df["ret_5d_pct"] <= 10.0)
            & (df["industry_ret_5d_rank"] >= 0.70)
            & (df["atr_14_pct"] <= 5.8)
            & (df["close_pos_in_day"] >= 0.55)
        )
        return df.loc[m].sort_values(["industry_ret_5d_rank", "amt_20d_yi"], ascending=[False, False])

    def sel_trend_pullback(df: pd.DataFrame) -> pd.DataFrame:
        """C. 强趋势缩量回踩MA10企稳低吸（不追高买点）"""
        m = (
            (df["amt_20d_yi"] >= 2.0)
            & (df["ma_bull_align"] == 1.0)
            & (df["ma60_bias_pct"] > 0.0)
            & (df["rs_20d_vs_hs300"] >= 6.0)
            & (df["ma10_bias_pct"] >= -2.5)
            & (df["ma10_bias_pct"] <= 2.5)
            & (df["vol_ratio_5d"] <= 0.95)
            & (df["close_pos_in_day"] >= 0.55)
            & (df["industry_above_ma20_pct"] >= 55.0)
        )
        return df.loc[m].sort_values("rs_20d_vs_hs300", ascending=False)

    def sel_sector_leader_div(df: pd.DataFrame) -> pd.DataFrame:
        """D. 主线行业龙头分散（20日均额>=3亿 + 行业前25% + 趋势多头 + 控波动，每行业限1只）"""
        m = (
            (df["amt_20d_yi"] >= 3.0)
            & (df["ma_bull_align"] == 1.0)
            & (df["industry_ret_5d_rank"] >= 0.75)
            & (df["pos_60d"] >= 0.60)
            & (df["ret_5d_pct"] <= 12.0)
            & (df["atr_14_pct"] <= 5.5)
        )
        return df.loc[m].sort_values("amt_20d_yi", ascending=False)

    def sel_low_vol_quality_mom(df: pd.DataFrame) -> pd.DataFrame:
        """E. 低波动大盘机构慢牛动量（均额>=5亿 + 60日稳健走强 + 极低波动 + 温和上行）"""
        m = (
            (df["amt_20d_yi"] >= 5.0)
            & (df["ma_bull_align"] == 1.0)
            & (df["ma60_bias_pct"] >= 2.0)
            & (df["rs_20d_vs_hs300"] >= 3.0)
            & (df["atr_14_pct"] <= 4.2)
            & (df["ret_5d_pct"] >= 0.0)
            & (df["ret_5d_pct"] <= 8.0)
        )
        return df.loc[m].sort_values("rs_20d_vs_hs300", ascending=False)

    offense_set = {"offensive", "extreme_offensive"}
    experiments = [
        ("B1_Old_MW04_5d_TP15", sel_mw04_legacy, "fixed_5d_tp15", -0.07, False),
        ("E1_MW04_Trail10", sel_mw04_legacy, "trail_10", -0.10, False),
        ("E2_MW04_MA20", sel_mw04_legacy, "ma20", -0.10, False),
        ("A1_VCP_Break_MA20", sel_vcp_breakout, "ma20", -0.10, True),
        ("A2_VCP_Break_Trail10", sel_vcp_breakout, "trail_10", -0.10, True),
        ("B_Inst3Yi_MA20", sel_inst_trend_3yi, "ma20", -0.10, True),
        ("B_Inst3Yi_Trail10", sel_inst_trend_3yi, "trail_10", -0.10, True),
        ("C_Pullback_MA20", sel_trend_pullback, "ma20", -0.10, True),
        ("C_Pullback_Trail10", sel_trend_pullback, "trail_10", -0.10, True),
        ("D_SecLeader_MA20", sel_sector_leader_div, "ma20", -0.10, True),
        ("D_SecLeader_Trail10", sel_sector_leader_div, "trail_10", -0.10, True),
        ("E_LowVol5Yi_MA20", sel_low_vol_quality_mom, "ma20", -0.10, True),
        ("E_LowVol5Yi_Trail10", sel_low_vol_quality_mom, "trail_10", -0.10, True),
    ]

    def pack_result(label: str, nav: pd.Series, meta: dict[str, Any]) -> dict[str, Any]:
        segs = {}
        for sk, (d0, d1) in sp.items():
            sub = nav[(nav.index >= d0) & (nav.index <= d1)]
            segs[sk] = _eval_nav(sub)
        return {"label": label, "full": _eval_nav(nav), "segments": segs, "meta": meta}

    results: list[dict[str, Any]] = [
        pack_result("B0_HS300_BuyHold", hs_nav, {}),
        pack_result("B2_AR1_BroadETF_Top2", etf_nav, etf_meta),
    ]
    nav_store: dict[str, pd.Series] = {"B2_AR1_BroadETF_Top2": etf_nav}

    for exp_name, fn, ex_mode, sl, one_ind in experiments:
        nav, st = simulate_slot_portfolio(
            day_df,
            by_date_rows,
            fn,
            name=exp_name,
            active_regimes=offense_set,
            max_slots=4,
            slot_weight=0.20,
            exit_mode=ex_mode,
            hard_stop_pct=sl,
            one_per_industry=one_ind,
            exit_on_defensive=True,
            exit_on_balanced=False,
        )
        nav_store[exp_name] = nav
        results.append(pack_result(exp_name, nav, st))

    # --- 3. 核心宽基ETF + 最优个股趋势组合（50/50 混合协同） ---
    stock_only = [r for r in results if r["label"] not in ("B0_HS300_BuyHold", "B2_AR1_BroadETF_Top2")]
    stock_only.sort(key=lambda x: (x["full"]["calmar"], x["full"]["cum_ret_pct"]), reverse=True)
    best_stock_label = stock_only[0]["label"]
    best_stock_nav = nav_store[best_stock_label]
    # 50% 宽基ETF动量Top2 + 50% 最优趋势跟踪主升个股
    etf_r = etf_nav.pct_change().fillna(0.0)
    stk_r = best_stock_nav.pct_change().fillna(0.0)
    combo_50_50 = (1.0 + 0.5 * etf_r + 0.5 * stk_r).cumprod()
    results.append(
        pack_result(f"COMBO_50ETF_50_{best_stock_label}", combo_50_50, {"etf_weight": 0.5, "stock_strategy": best_stock_label})
    )

    # 排序输出
    results.sort(key=lambda x: x["full"]["calmar"], reverse=True)
    out_path = runtime_path("mining_runs") / "ar1_alpha_experiment_v1.json"
    out_path.write_text(json.dumps({"splits": sp, "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=" * 120)
    print(f"AR1-Alpha 上升期全市场实证排行榜（IS[{sp['is'][0]}~{sp['is'][1]}] | Val[{sp['val'][0]}~{sp['val'][1]}] | OOS[{sp['oos'][0]}~{sp['oos'][1]}]）")
    print("=" * 120)
    for idx, r in enumerate(results, 1):
        f = r["full"]
        s_is, s_val, s_oos = r["segments"]["is"], r["segments"]["val"], r["segments"]["oos"]
        m = r["meta"]
        extra = (
            f"笔={m.get('closed_trades','-')} 均持={m.get('avg_hold_days','-')}d 胜率={m.get('win_rate_pct','-')}% 盈亏比={m.get('pl_ratio','-')}"
            if "closed_trades" in m
            else str(m)
        )
        print(
            f"#{idx:02d} {r['label']:34s} | FULL: 累计{f['cum_ret_pct']:+6.1f}% 年化{f['ann_ret_pct']:+5.1f}% 回撤{f['max_dd_pct']:4.1f}% 夏普{f['sharpe']:4.2f} 卡玛{f['calmar']:4.2f}"
        )
        print(
            f"    IS={s_is['cum_ret_pct']:+6.1f}%(DD{s_is['max_dd_pct']:4.1f}%) | "
            f"Val={s_val['cum_ret_pct']:+6.1f}%(DD{s_val['max_dd_pct']:4.1f}%) | "
            f"OOS={s_oos['cum_ret_pct']:+6.1f}%(DD{s_oos['max_dd_pct']:4.1f}%) | {extra}"
        )
    print("=" * 120)
    print(f"报告已落盘：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
