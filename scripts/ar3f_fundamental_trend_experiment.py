# -*- coding: utf-8 -*-
"""AR3-F（基本面业绩断层 × 趋势共振）全市场严格 Point-in-Time 实证实验。

严格反幻觉与零未来函数铁律：
1. 财报与业绩预告绝对按真实公告披露日 notice_date 对齐（严禁使用 report_date）：
   - 对任意信号日 signal_date，仅可见 notice_date <= signal_date 的财报/快报/业绩预告；
   - PEAD 严苛滞后压力测试版（StrictLag1）进一步要求 notice_date < signal_date；
   - 每笔真实成交强断言：notice_date <= signal_date < entry_date（违规直接报错中止）。
2. 全市场真实行业映射（stock.sector_stocks JOIN stock.sectors WHERE type='industry'，145 行业）覆盖旧缓存，
   确保 one_per_industry=True 真实生效。
3. 真实撮合防虚高：
   - 买入在 T+1 开盘价撮合，跳过一字涨停开盘；
   - 卖出在次日开盘价撮合，一字跌停顺延；
   - 持仓估值使用 ClickHouse 全量真实 OHLC（绝不因成交额跌破 5000 万而拿峰值价虚高估值）；
   - 股票双边摩擦 0.26%（买 0.105% + 卖 0.155%），ETF 双边摩擦 0.12%（单边 0.06%）。
4. 单一现金账户零杠杆（gross_exposure <= 100%），检验 AR3-F 独立表现及与 #02 AR3_6_RiskAdjMom_Top2+Panic
   的真·单账户双核融合表现；输出 IS / Val / OOS 三段收益与剔除 Top1/Top2 单笔交易后的净收益审计。
"""
from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ar1_alpha_experiment import (  # noqa: E402
    BUY_COST,
    SELL_COST,
    _build_ar1_regimes,
    _ch_query_df,
    _eval_nav,
)
from scripts.ar3_cross_asset_experiment import (  # noqa: E402
    ETF_ONE_WAY_COST,
    SAFE_BOND_CODE,
    _load_etf_panels,
    simulate_cross_asset_strategy,
)
from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402


def _load_real_industry_map() -> dict[str, str]:
    """从 ClickHouse 行业映射表加载 145 个真实行业，修复旧缓存中的空行业。"""
    df = _ch_query_df(
        """
        SELECT ss.stock_code AS code, any(s.name) AS industry
        FROM stock.sector_stocks ss
        INNER JOIN stock.sectors s ON ss.sector_code = s.code
        WHERE s.type = 'industry' AND length(s.name) > 0
        GROUP BY ss.stock_code
        """
    )
    return dict(zip(df["code"].astype(str), df["industry"].astype(str)))


def _load_pit_financial_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    """加载正式季报/快报与业绩预告（严格按 notice_date 排序，并预先计算无未来加速度）。"""
    fin = _ch_query_df(
        """
        SELECT
            code,
            toString(report_date) AS report_date,
            toString(notice_date) AS notice_date,
            source_type,
            report_quarter,
            revenue,
            revenue_yoy_pct,
            net_profit,
            net_profit_yoy_pct,
            revenue_qoq_pct,
            net_profit_qoq_pct,
            roe_pct,
            gross_margin_pct
        FROM stock.stock_financial_disclosure FINAL
        WHERE notice_date > report_date
          AND notice_date >= '2022-01-01'
          AND notice_date <= '2026-11-20'
        ORDER BY code, notice_date, report_date
        """
    )
    # 同一天若同时有 express 和 official，保留 official 优先；同 (code, notice_date) 去重保留最新 report_date
    fin["src_prio"] = (fin["source_type"] == "official").astype(int)
    fin = fin.sort_values(["code", "notice_date", "report_date", "src_prio"]).reset_index(drop=True)

    # 严格按 notice_date 顺序构造上一已公告不同报告期的 np_yoy，避免任何未来函数
    fin_dedup_rd = fin.drop_duplicates(subset=["code", "report_date"], keep="first").sort_values(
        ["code", "notice_date", "report_date"]
    )
    fin_dedup_rd["prev_np_yoy"] = fin_dedup_rd.groupby("code", sort=False)["net_profit_yoy_pct"].shift(1)
    fin_dedup_rd["pit_np_accel"] = fin_dedup_rd["net_profit_yoy_pct"] - fin_dedup_rd["prev_np_yoy"]
    accel_map = fin_dedup_rd[["code", "report_date", "pit_np_accel"]]
    fin = fin.merge(accel_map, on=["code", "report_date"], how="left")

    # 按 (code, notice_date) 保留最后一条作为该公告日生效状态（严格单调供 merge_asof 使用）
    fin = fin.drop_duplicates(subset=["code", "notice_date"], keep="last").reset_index(drop=True)
    fin["notice_dt"] = pd.to_datetime(fin["notice_date"])

    fc = _ch_query_df(
        """
        SELECT
            code,
            toString(report_date) AS fc_report_date,
            toString(notice_date) AS fc_notice_date,
            predict_type AS fc_predict_type,
            profit_lower AS fc_profit_lower,
            yoy_lower_pct AS fc_yoy_lower,
            yoy_upper_pct AS fc_yoy_upper,
            yoy_mid_pct AS fc_yoy_mid
        FROM stock.stock_earnings_forecast FINAL
        WHERE notice_date >= '2022-07-01' AND notice_date <= '2026-11-20'
        ORDER BY code, notice_date, report_date
        """
    )
    fc = fc.drop_duplicates(subset=["code", "fc_notice_date"], keep="last").reset_index(drop=True)
    fc["fc_notice_dt"] = pd.to_datetime(fc["fc_notice_date"])
    return fin, fc


def attach_pit_fundamentals(panel_df: pd.DataFrame) -> pd.DataFrame:
    """用 merge_asof(direction='backward') 严格将 notice_date <= trade_date 的基本面合并到日频特征表。"""
    t0 = time.time()
    fin, fc = _load_pit_financial_tables()

    df = panel_df.copy()
    # 修复真实 145 行业
    ind_map = _load_real_industry_map()
    df["industry"] = df["code"].astype(str).map(ind_map).fillna("未分类")

    # 补充 20日波动率、1日涨跌幅、高开幅度等短线形态特征（严格基于 T 日已知 OHLC）
    df["ret_1d_pct"] = (df["close"] / (df["prev_close"] + 1e-6) - 1.0) * 100.0
    df["gap_pct"] = (df["open"] / (df["prev_close"] + 1e-6) - 1.0) * 100.0
    df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
    df["vol_20d_pct"] = (
        df.groupby("code", sort=False)["ret_1d_pct"]
        .transform(lambda s: s.rolling(20, min_periods=10).std())
        .fillna(5.0)
    )
    # 涨停判定阈值（若旧缓存缺 lu_thresh）
    if "lu_thresh" not in df.columns:
        def _lu(c: str) -> float:
            if c.endswith(".BJ") or c.startswith(("8", "4", "92")):
                return 1.292
            if c.startswith(("300", "301", "688", "689")):
                return 1.192
            return 1.095
        df["lu_thresh"] = df["code"].astype(str).map(_lu).astype(np.float32)

    df["trade_dt"] = pd.to_datetime(df["trade_date"])

    # 1. merge_asof 正式季报/业绩快报（按 trade_dt >= notice_dt，pandas 要求左/右表按 on 键全局排序）
    df = df.sort_values(["trade_dt", "code"]).reset_index(drop=True)
    fin_s = fin.sort_values(["notice_dt", "code"]).reset_index(drop=True)
    df = pd.merge_asof(
        df,
        fin_s[
            [
                "code",
                "notice_dt",
                "notice_date",
                "report_date",
                "source_type",
                "revenue",
                "revenue_yoy_pct",
                "net_profit",
                "net_profit_yoy_pct",
                "net_profit_qoq_pct",
                "roe_pct",
                "gross_margin_pct",
                "pit_np_accel",
            ]
        ],
        left_on="trade_dt",
        right_on="notice_dt",
        by="code",
        direction="backward",
    )
    df["pit_days_since_notice"] = (df["trade_dt"] - df["notice_dt"]).dt.days

    # 2. merge_asof 业绩预告（按 trade_dt >= fc_notice_dt）
    fc_s = fc.sort_values(["fc_notice_dt", "code"]).reset_index(drop=True)
    df = pd.merge_asof(
        df,
        fc_s[
            [
                "code",
                "fc_notice_dt",
                "fc_notice_date",
                "fc_report_date",
                "fc_predict_type",
                "fc_profit_lower",
                "fc_yoy_lower",
                "fc_yoy_upper",
                "fc_yoy_mid",
            ]
        ],
        left_on="trade_dt",
        right_on="fc_notice_dt",
        by="code",
        direction="backward",
    )
    df["fc_days_since"] = (df["trade_dt"] - df["fc_notice_dt"]).dt.days

    # 硬断言：全表 345 万行零未来函数！
    bad_fin = int((df["notice_dt"].notna() & (df["notice_dt"] > df["trade_dt"])).sum())
    bad_fc = int((df["fc_notice_dt"].notna() & (df["fc_notice_dt"] > df["trade_dt"])).sum())
    assert bad_fin == 0 and bad_fc == 0, f"PIT 违规！bad_fin={bad_fin}, bad_fc={bad_fc}"

    # 构造业绩排雷盾（最近 60 天内发布过负面预告，且该预告报告期比最新正式季报更新或一致）
    bad_types = {"首亏", "续亏", "预减", "略减", "增亏"}
    fc_is_newer = df["fc_report_date"].fillna("") >= df["report_date"].fillna("")
    df["has_earnings_mine"] = (
        df["fc_days_since"].notna()
        & (df["fc_days_since"] >= 0)
        & (df["fc_days_since"] <= 75)
        & fc_is_newer
        & (df["fc_predict_type"].isin(bad_types) | (df["fc_yoy_lower"].fillna(0.0) < -20.0))
    )

    # 截面相对强度与业绩综合排名（严格按每日截面计算）
    df["rs20_rank"] = df.groupby("trade_date")["rs_20d_vs_hs300"].rank(pct=True)
    df["np_yoy_rank"] = df.groupby("trade_date")["net_profit_yoy_pct"].rank(pct=True)

    cov_fin = float(df["notice_date"].notna().mean() * 100.0)
    print(
        f"[PIT 基本面对齐完成] {len(df):,} 行 ({time.time() - t0:.1f}s) | "
        f"财报覆盖率={cov_fin:.1f}% | 真实行业数={df['industry'].nunique()} | PIT 违规=0",
        flush=True,
    )
    return df


def _load_full_price_lookup(codes: set[str], start_date: str, end_date: str) -> dict[tuple[str, str], tuple[float, float, float, float, float]]:
    """拉取全量真实 OHLC（防止持仓股票成交额缩水跌出 panel_df 后失真估值）。"""
    if not codes:
        return {}
    code_list = sorted(codes)
    frames: list[pd.DataFrame] = []
    batch = 800
    for i in range(0, len(code_list), batch):
        sub = ",".join(f"'{c}'" for c in code_list[i : i + batch])
        frames.append(
            _ch_query_df(
                f"""
                SELECT code, toString(trade_date) AS trade_date, open, high, low, close
                FROM stock.kline_daily FINAL
                WHERE code IN ({sub})
                  AND trade_date >= '2022-12-15' AND trade_date <= '{end_date}'
                ORDER BY code, trade_date
                """
            )
        )
    all_px = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if all_px.empty:
        return {}
    all_px = all_px.sort_values(["code", "trade_date"]).reset_index(drop=True)
    all_px["prev_close"] = all_px.groupby("code", sort=False)["close"].shift(1).fillna(all_px["open"])
    all_px = all_px.loc[all_px["trade_date"] >= start_date]
    lookup: dict[tuple[str, str], tuple[float, float, float, float, float]] = {}
    for c, dt, o, h, l, cl, pc in zip(
        all_px["code"].astype(str),
        all_px["trade_date"].astype(str),
        all_px["open"].astype(float),
        all_px["high"].astype(float),
        all_px["low"].astype(float),
        all_px["close"].astype(float),
        all_px["prev_close"].astype(float),
    ):
        lookup[(c, dt)] = (o, h, l, cl, pc)
    return lookup


@dataclass
class StockPos:
    code: str
    name: str
    industry: str
    shares: float
    entry_px: float
    entry_date: str
    signal_date: str
    notice_date: str
    report_date: str
    pit_np_yoy: float
    peak_close: float
    last_close: float
    days_held: int = 0
    pending_sell: bool = False
    sell_reason: str = ""


def _load_dividend_lookup() -> dict[tuple[str, str], tuple[float, float]]:
    """加载真实除权除息事件 (code, ex_dividend_date) -> (bonus_per_10, cash_per_10)。"""
    df = _ch_query_df(
        """
        SELECT
            code,
            toString(ex_dividend_date) AS ex_date,
            sum(bonus_per_10) AS bonus_per_10,
            sum(cash_per_10) AS cash_per_10
        FROM stock.stock_dividend_events FINAL
        WHERE ex_dividend_date >= '2023-01-01' AND ex_dividend_date <= '2026-11-30'
        GROUP BY code, ex_date
        """
    )
    return {
        (str(c), str(d)): (float(b), float(ca))
        for c, d, b, ca in zip(df["code"], df["ex_date"], df["bonus_per_10"], df["cash_per_10"])
    }


def simulate_ar3f_stock(
    day_df: pd.DataFrame,
    by_date_rows: dict[str, pd.DataFrame],
    px_lookup: dict[tuple[str, str], tuple[float, float, float, float, float]],
    div_lookup: dict[tuple[str, str], tuple[float, float]],
    selector_fn: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    name: str,
    regime_slots: dict[str, int],
    slot_weight: float = 0.20,
    trail_stop_pct: float = 0.10,
    hard_stop_pct: float = -0.10,
    max_hold_days: int = 60,
    one_per_industry: bool = True,
    exit_on_defensive: bool = True,
    strict_notice_audit: bool = True,
) -> tuple[pd.Series, pd.Series, list[dict[str, Any]], dict[str, Any]]:
    """严格事件驱动 Mark-to-Market 股票仿真器（含真实券商除权除息账户结算）。"""
    dates = day_df["trade_date"].astype(str).tolist()
    reg_by_date = dict(zip(dates, day_df["ar1_regime"].tolist()))

    cash = 1.0
    positions: dict[str, StockPos] = {}
    nav_list: list[float] = []
    weight_list: list[float] = []
    pending_buys: list[dict[str, Any]] = []
    closed_trades: list[dict[str, Any]] = []
    pit_checked = 0
    div_adjusted = 0

    for dt in dates:
        reg_today = reg_by_date.get(dt, "balanced")
        day_tbl = by_date_rows.get(dt)

        # 0. 开盘前真实除权除息结算（持仓跨过 ex_dividend_date 时：派息入现金 + 送转增股数 + 调整参考成本/峰值）
        for code, pos in positions.items():
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
                div_adjusted += 1

        # 1. 开盘先执行挂单卖出（跳过一字跌停，真实价格来自全量 px_lookup）
        for code in list(positions.keys()):
            pos = positions[code]
            if not pos.pending_sell:
                continue
            bar = px_lookup.get((code, dt))
            if bar is None:
                continue
            o, h, l, cl, pc = bar
            if o <= 0:
                continue
            ev = div_lookup.get((code, dt))
            ex_pc = (pc - ev[1] / 10.0) / (1.0 + ev[0] / 10.0) if (ev is not None and pc > 0) else pc
            ld = 0.808 if code.startswith(("300", "301", "688", "689")) else (0.708 if code.endswith(".BJ") else 0.905)
            if ex_pc > 0 and o == h and h == l and cl <= ex_pc * ld:
                continue  # 一字跌停卖不出，顺延
            proceeds = pos.shares * o * (1.0 - SELL_COST)
            cash += proceeds
            ret_pct = (o / pos.entry_px - 1.0) * 100.0 - (BUY_COST + SELL_COST) * 100.0
            closed_trades.append({
                "code": pos.code,
                "name": pos.name,
                "industry": pos.industry,
                "signal_date": pos.signal_date,
                "entry_date": pos.entry_date,
                "exit_date": dt,
                "notice_date": pos.notice_date,
                "report_date": pos.report_date,
                "pit_np_yoy": round(pos.pit_np_yoy, 1) if pd.notna(pos.pit_np_yoy) else None,
                "ret_pct": round(ret_pct, 2),
                "days_held": pos.days_held,
                "exit_reason": pos.sell_reason or "rule",
            })
            del positions[code]

        # 2. 开盘执行昨日收盘排队的买入（跳过一字涨停，受当日目标槽位数与可用现金约束）
        if pending_buys:
            target_slots = int(pending_buys[0]["max_slots"])
            cur_eq_open = cash + sum(
                p.shares * (px_lookup.get((c, dt), (p.last_close,))[0])
                for c, p in positions.items()
            )
            held_inds = {p.industry for p in positions.values()}
            for cand in pending_buys:
                if len(positions) >= target_slots:
                    break
                code = str(cand["code"])
                ind = str(cand["industry"])
                if code in positions:
                    continue
                if one_per_industry and ind != "未分类" and ind in held_inds:
                    continue
                bar = px_lookup.get((code, dt))
                if bar is None:
                    continue
                o, h, l, cl, pc = bar
                if o <= 0:
                    continue
                ev = div_lookup.get((code, dt))
                ex_pc = (pc - ev[1] / 10.0) / (1.0 + ev[0] / 10.0) if (ev is not None and pc > 0) else pc
                lu = 1.192 if code.startswith(("300", "301", "688", "689")) else (1.292 if code.endswith(".BJ") else 1.095)
                if ex_pc > 0 and o >= ex_pc * lu:
                    continue  # 开盘涨停买不进跳过

                # 严格 PIT 硬断言：notice_date <= signal_date < entry_date
                sig_dt = str(cand["signal_date"])
                not_dt = str(cand["notice_date"] or "")
                assert sig_dt < dt, f"信号日不早于买入日: {sig_dt} vs {dt}"
                if strict_notice_audit and not_dt:
                    assert not_dt <= sig_dt, f"未来函数违规！{code} notice={not_dt} > signal={sig_dt}"
                    pit_checked += 1

                alloc = min(cash, cur_eq_open * slot_weight)
                if alloc < cur_eq_open * 0.05:
                    continue
                shares = (alloc * (1.0 - BUY_COST)) / o
                cash -= alloc
                positions[code] = StockPos(
                    code=code,
                    name=str(cand.get("name") or code),
                    industry=ind,
                    shares=shares,
                    entry_px=o,
                    entry_date=dt,
                    signal_date=sig_dt,
                    notice_date=not_dt,
                    report_date=str(cand.get("report_date") or ""),
                    pit_np_yoy=float(cand.get("pit_np_yoy")) if pd.notna(cand.get("pit_np_yoy")) else float("nan"),
                    peak_close=o,
                    last_close=o,
                )
                held_inds.add(ind)
            pending_buys = []

        # 3. 收盘 Mark-to-Market 与止盈止损判定（生成明日开盘卖出指令）
        stock_val = 0.0
        for code, pos in positions.items():
            bar = px_lookup.get((code, dt))
            if bar is not None and bar[3] > 0:
                cl = bar[3]
                pos.last_close = cl
            else:
                cl = pos.last_close
            pos.days_held += 1
            if cl > pos.peak_close:
                pos.peak_close = cl
            stock_val += pos.shares * cl

            pnl_from_entry = cl / pos.entry_px - 1.0
            dd_from_peak = cl / pos.peak_close - 1.0

            if exit_on_defensive and reg_today in ("defensive", "extreme_panic"):
                pos.pending_sell = True
                pos.sell_reason = "regime_defensive"
            elif pnl_from_entry <= hard_stop_pct:
                pos.pending_sell = True
                pos.sell_reason = "hard_stop"
            elif dd_from_peak <= -trail_stop_pct:
                pos.pending_sell = True
                pos.sell_reason = "trail_stop"
            elif max_hold_days > 0 and pos.days_held >= max_hold_days:
                pos.pending_sell = True
                pos.sell_reason = "max_hold"

        nav_today = cash + stock_val
        nav_list.append(nav_today)
        weight_list.append(stock_val / nav_today if nav_today > 0 else 0.0)

        # 4. 收盘后筛选明日开盘候选（仅用今日收盘已知特征）
        allowed_slots = int(regime_slots.get(reg_today, 0))
        if allowed_slots > 0 and len(positions) < allowed_slots and day_tbl is not None and not day_tbl.empty:
            cands = selector_fn(day_tbl)
            if not cands.empty:
                for row in cands.head(allowed_slots * 4).itertuples(index=False):
                    # 取较新的有效公告日作为审计凭证（财报或预告）
                    n_fin = getattr(row, "notice_date", None)
                    n_fc = getattr(row, "fc_notice_date", None)
                    n_eff = str(n_fin) if pd.notna(n_fin) else (str(n_fc) if pd.notna(n_fc) else "")
                    pending_buys.append({
                        "code": row.code,
                        "name": getattr(row, "name", row.code),
                        "industry": getattr(row, "industry", "未分类"),
                        "signal_date": dt,
                        "notice_date": n_eff,
                        "report_date": str(getattr(row, "report_date", "") or ""),
                        "pit_np_yoy": getattr(row, "net_profit_yoy_pct", float("nan")),
                        "max_slots": allowed_slots,
                    })

    nav_s = pd.Series(nav_list, index=dates, dtype=np.float64)
    wt_s = pd.Series(weight_list, index=dates, dtype=np.float64)

    # 交易统计与剔除 Top1/Top2 单笔暴赚交易审计
    rets = [t["ret_pct"] for t in closed_trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = (len(wins) / len(rets) * 100.0) if rets else 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(abs(np.mean(losses))) if losses else 0.0
    plr = (avg_win / avg_loss) if avg_loss > 1e-6 else (avg_win if avg_win > 0 else 0.0)

    # 近似估算剔除最高 1 笔和 2 笔后的复利净收益（按单笔占总资金槽位权重估算贡献）
    sorted_t = sorted(closed_trades, key=lambda x: x["ret_pct"], reverse=True)
    top1_contrib = (sorted_t[0]["ret_pct"] * slot_weight) if len(sorted_t) >= 1 else 0.0
    top2_contrib = sum(x["ret_pct"] * slot_weight for x in sorted_t[:2]) if len(sorted_t) >= 2 else top1_contrib
    full_cum = float((nav_s.iloc[-1] / nav_s.iloc[0] - 1.0) * 100.0)
    ex_top1 = round(full_cum - top1_contrib, 2)
    ex_top2 = round(full_cum - top2_contrib, 2)

    summary = {
        "name": name,
        "trades": len(closed_trades),
        "win_rate_pct": round(win_rate, 1),
        "avg_ret_pct": round(float(np.mean(rets)), 2) if rets else 0.0,
        "plr": round(plr, 2),
        "avg_hold_days": round(float(np.mean([t["days_held"] for t in closed_trades])), 1) if closed_trades else 0.0,
        "avg_exposure_pct": round(float(wt_s.mean() * 100.0), 1),
        "pit_audited_entries": pit_checked,
        "dividend_events_settled": div_adjusted,
        "ex_top1_est_cum_pct": ex_top1,
        "ex_top2_est_cum_pct": ex_top2,
        "top3_trades": sorted_t[:3],
        "worst3_trades": sorted_t[-3:][::-1] if sorted_t else [],
    }
    return nav_s, wt_s, closed_trades, summary


def simulate_dual_core_single_account(
    day_df: pd.DataFrame,
    stock_nav: pd.Series,
    stock_weight: pd.Series,
    etf_nav: pd.Series,
    *,
    stock_budget_cap: float = 0.50,
) -> tuple[pd.Series, dict[str, Any]]:
    """真·单一现金账户零杠杆（gross_exposure <= 100%）双核融合仿真器。

    - 每日个股子引擎实际占总账户比例 w_stock(t) = min(stock_budget_cap, stock_weight(t-1) * stock_budget_cap / max_slot_cap)
      更精确做法：直接把原股票策略按 scale = stock_budget_cap / 0.80 缩放到账户资金比例 w_s(t) <= stock_budget_cap；
    - 账户剩余全部闲置现金 w_e(t) = 1.0 - w_s(t) 自动无缝投入 #02 AR3_6_RiskAdjMom_Top2+Panic 跨资产 ETF 轮动；
    - 强制每日校验 w_s(t) + w_e(t) == 1.0000（绝对零杠杆），并扣除资金在股/ETF间切换的 0.06% ETF 单边摩擦！
    """
    dates = day_df["trade_date"].astype(str).tolist()
    s_ret = stock_nav.pct_change().fillna(0.0)
    e_ret = etf_nav.pct_change().fillna(0.0)
    # stock_weight 是原纯股票策略（满仓约 0.80）的真实在场仓位，映射到子资金预算比例
    # 原股票组合日收益 = w_orig * r_stock_active；若给股票引擎分配总账户比例 k = stock_budget_cap / 0.80，
    # 则股票引擎实际占用账户现金 = min(stock_budget_cap, w_orig * k)，对应对总账户的收益贡献 = s_ret * k！
    # 剩余现金 1 - w_stock_used 全部投入跨资产 ETF（ETF 满仓时日收益为 e_ret）。
    max_orig = max(0.80, float(stock_weight.max()))
    k = min(1.0, stock_budget_cap / max_orig)

    nav = 1.0
    nav_vals = [1.0]
    prev_we = 1.0
    max_gross = 0.0
    ws_list: list[float] = [0.0]
    we_list: list[float] = [1.0]

    for i in range(1, len(dates)):
        dt = dates[i]
        prev_dt = dates[i - 1]
        w_stock_used = min(stock_budget_cap, float(stock_weight.loc[prev_dt]) * k)
        w_etf_used = max(0.0, 1.0 - w_stock_used)
        gross = w_stock_used + w_etf_used
        assert gross <= 1.000001, f"杠杆超限: {gross}"
        if gross > max_gross:
            max_gross = gross

        # 资金在股/ETF间划转时额外扣除 ETF 端申赎/买卖单边摩擦
        switch_cost = abs(w_etf_used - prev_we) * ETF_ONE_WAY_COST
        r_day = float(s_ret.loc[dt]) * k + w_etf_used * float(e_ret.loc[dt]) - switch_cost
        nav *= (1.0 + r_day)
        nav_vals.append(nav)
        ws_list.append(w_stock_used)
        we_list.append(w_etf_used)
        prev_we = w_etf_used

    return pd.Series(nav_vals, index=dates, dtype=np.float64), {
        "stock_budget_cap_pct": round(stock_budget_cap * 100.0, 1),
        "avg_stock_weight_pct": round(float(np.mean(ws_list) * 100.0), 1),
        "avg_etf_weight_pct": round(float(np.mean(we_list) * 100.0), 1),
        "max_gross_exposure_pct": round(max_gross * 100.0, 2),
    }


def _pack_metrics(nav: pd.Series, splits: dict[str, tuple[str, str]]) -> dict[str, Any]:
    full = _eval_nav(nav)
    res: dict[str, Any] = {"full": full}
    for seg_name in ("is", "val", "oos"):
        s_dt, e_dt = splits[seg_name]
        sub = nav.loc[(nav.index >= s_dt) & (nav.index <= e_dt)]
        res[seg_name] = _eval_nav(sub) if len(sub) >= 2 else {"cum_ret_pct": 0.0, "ann_ret_pct": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0}
    return res


def main() -> None:
    t_all = time.time()
    print("=== AR3-F 基本面业绩断层 × 趋势共振 严格 PIT 实证实验 ===", flush=True)
    panel_df, bench_df = build_or_load_feature_panel(
        start_date="2023-01-01",
        end_date="2026-11-21",
        min_amt_20d=50_000_000.0,
    )
    day_df = _build_ar1_regimes(panel_df, bench_df)
    dates = day_df["trade_date"].astype(str).tolist()
    splits = split_trade_dates(dates)
    print(
        f"样本期: {dates[0]} ~ {dates[-1]} ({len(dates)} 交易日) | "
        f"IS={splits['is'][0]}~{splits['is'][1]} | Val={splits['val'][0]}~{splits['val'][1]} | OOS={splits['oos'][0]}~{splits['oos'][1]}",
        flush=True,
    )

    # 1. 严格 Point-in-Time 基本面对齐
    pit_df = attach_pit_fundamentals(panel_df)
    by_date_rows = {dt: g for dt, g in pit_df.groupby("trade_date", sort=False)}

    # 2. 预加载全量真实价格表 + 真实分红送转除权表
    all_codes = set(pit_df["code"].astype(str).unique())
    print(f"[价格底座] 加载 {len(all_codes)} 只 A 股真实 OHLC 与除权分红表...", flush=True)
    px_lookup = _load_full_price_lookup(all_codes, dates[0], dates[-1])
    div_lookup = _load_dividend_lookup()
    print(f"[价格底座] 已加载 {len(px_lookup):,} 条日 K + {len(div_lookup):,} 条真实除权除息记录。", flush=True)

    # 3. 基准线：沪深300 + #02 AR3_6_RiskAdjMom_Top2+Panic（精确 mode="risk_adj_mom"）
    hs300_nav = pd.Series(day_df["hs300_close"].astype(float).values, index=dates)
    hs300_nav = hs300_nav / hs300_nav.iloc[0]

    piv_o, piv_c, code_to_class, _ = _load_etf_panels()
    ar3_etf_nav, _ = simulate_cross_asset_strategy(
        day_df, piv_o, piv_c, code_to_class,
        mode="risk_adj_mom", top_k=2, rebal_every=5, one_per_class=True, use_panic_overlay=True,
    )
    m_etf = _pack_metrics(ar3_etf_nav, splits)
    print(
        f"[基准校验] #02 AR3_6_RiskAdjMom_Top2+Panic: 全={m_etf['full']['cum_ret_pct']:+.1f}% "
        f"(年{m_etf['full']['ann_ret_pct']:+.1f}%) DD={m_etf['full']['max_dd_pct']:.1f}% Sh={m_etf['full']['sharpe']:.2f} | "
        f"IS={m_etf['is']['cum_ret_pct']:+.1f}% Val={m_etf['val']['cum_ret_pct']:+.1f}% OOS={m_etf['oos']['cum_ret_pct']:+.1f}%",
        flush=True,
    )

    # 4. 定义待实证的选股器族（严格 T 日收盘已知，禁止任何未来列）
    # 基线 B1：原 AR1 v2.0 纯量价低波慢牛（无基本面过滤）
    def sel_b1_lowvol_pure(df: pd.DataFrame) -> pd.DataFrame:
        m = (
            (df["amt_20d_yi"] >= 5.0)
            & (df["above_ma20"] > 0)
            & (df["ma_bull_align"] > 0)
            & (df["vol_20d_pct"] <= 3.5)
            & (df["ret_20d_pct"].between(3.0, 25.0))
        )
        return df.loc[m].sort_values("rs_20d_vs_hs300", ascending=False)

    # 族 ① PEAD（业绩断层 / 公告后放量起爆）
    def sel_f1a_pead_official(df: pd.DataFrame) -> pd.DataFrame:
        m = (
            (~df["has_earnings_mine"])
            & df["pit_days_since_notice"].between(0, 5)
            & (df["net_profit_yoy_pct"] >= 40.0)
            & (df["revenue_yoy_pct"] >= 20.0)
            & (df["net_profit"] >= 50_000_000.0)
            & (df["amt_20d_yi"] >= 1.0)
            & (df["above_ma20"] > 0)
            & (df["ret_1d_pct"] >= 3.0)
            & (df["vol_ratio_20d"] >= 1.5)
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        sub["score"] = sub["net_profit_yoy_pct"].clip(0, 300) / 100.0 + sub["ret_1d_pct"] / 10.0 + sub["vol_ratio_20d"].clip(0, 5) * 0.3
        return sub.sort_values("score", ascending=False)

    def sel_f1b_pead_all_events(df: pd.DataFrame) -> pd.DataFrame:
        off_hit = (
            df["pit_days_since_notice"].between(0, 5)
            & (df["net_profit_yoy_pct"] >= 35.0)
            & (df["revenue_yoy_pct"] >= 15.0)
            & (df["net_profit"] >= 50_000_000.0)
        )
        fc_hit = (
            df["fc_days_since"].between(0, 5)
            & df["fc_predict_type"].isin(["预增", "扭亏"])
            & (df["fc_yoy_lower"] >= 50.0)
            & (df["fc_profit_lower"] >= 50_000_000.0)
        )
        m = (
            (~df["has_earnings_mine"])
            & (off_hit | fc_hit)
            & (df["amt_20d_yi"] >= 1.0)
            & (df["above_ma20"] > 0)
            & (df["ret_1d_pct"] >= 3.0)
            & (df["vol_ratio_20d"] >= 1.5)
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        eff_yoy = np.where(fc_hit.loc[m], sub["fc_yoy_mid"].fillna(0.0), sub["net_profit_yoy_pct"].fillna(0.0))
        sub["score"] = np.clip(eff_yoy, 0, 300) / 100.0 + sub["ret_1d_pct"] / 10.0 + sub["vol_ratio_20d"].clip(0, 5) * 0.3
        return sub.sort_values("score", ascending=False)

    # F1C 严苛反幻觉版：强要求 notice_date 严格早于 signal_date（至少滞后 1 自然日，彻底排除公告当日盘中/盘后歧义）
    def sel_f1c_pead_strict_lag1(df: pd.DataFrame) -> pd.DataFrame:
        off_hit = (
            df["pit_days_since_notice"].between(1, 6)
            & (df["net_profit_yoy_pct"] >= 35.0)
            & (df["revenue_yoy_pct"] >= 15.0)
            & (df["net_profit"] >= 50_000_000.0)
        )
        fc_hit = (
            df["fc_days_since"].between(1, 6)
            & df["fc_predict_type"].isin(["预增", "扭亏"])
            & (df["fc_yoy_lower"] >= 50.0)
            & (df["fc_profit_lower"] >= 50_000_000.0)
        )
        m = (
            (~df["has_earnings_mine"])
            & (off_hit | fc_hit)
            & (df["amt_20d_yi"] >= 1.0)
            & (df["above_ma20"] > 0)
            & (df["ret_1d_pct"] >= 2.0)
            & (df["vol_ratio_20d"] >= 1.3)
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        eff_yoy = np.where(fc_hit.loc[m], sub["fc_yoy_mid"].fillna(0.0), sub["net_profit_yoy_pct"].fillna(0.0))
        sub["score"] = np.clip(eff_yoy, 0, 300) / 100.0 + sub["rs_20d_vs_hs300"] / 20.0
        return sub.sort_values("score", ascending=False)

    # 族 ② CANSLIM 业绩高增加速 × 机构趋势主升
    def sel_f2a_canslim_core(df: pd.DataFrame) -> pd.DataFrame:
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

    def sel_f2b_lowvol_plus_fund(df: pd.DataFrame) -> pd.DataFrame:
        """直接对照 B1（纯量价低波）：叠加基本面硬质量过滤，检验基本面真实增益。"""
        m = (
            (~df["has_earnings_mine"])
            & (df["pit_days_since_notice"] <= 185)
            & (df["net_profit_yoy_pct"] >= 20.0)
            & (df["revenue_yoy_pct"] >= 10.0)
            & (df["roe_pct"] >= 5.0)
            & (df["net_profit"] >= 50_000_000.0)
            & (df["amt_20d_yi"] >= 3.0)
            & (df["above_ma20"] > 0)
            & (df["ma_bull_align"] > 0)
            & (df["vol_20d_pct"] <= 3.8)
            & (df["ret_20d_pct"].between(3.0, 28.0))
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        sub["score"] = 0.6 * sub["rs20_rank"].fillna(0.5) + 0.4 * sub["np_yoy_rank"].fillna(0.5)
        return sub.sort_values("score", ascending=False)

    def sel_f2c_canslim_accel(df: pd.DataFrame) -> pd.DataFrame:
        """业绩加速（增速环比扩大或单季高增）× 强势机构趋势。"""
        accel_ok = (df["pit_np_accel"] > 0.0) | (df["net_profit_qoq_pct"] >= 15.0)
        m = (
            (~df["has_earnings_mine"])
            & (df["pit_days_since_notice"] <= 185)
            & (df["net_profit_yoy_pct"] >= 35.0)
            & (df["revenue_yoy_pct"] >= 15.0)
            & (df["net_profit"] >= 50_000_000.0)
            & accel_ok
            & (df["amt_20d_yi"] >= 2.0)
            & (df["above_ma20"] > 0)
            & (df["ma10_bias_pct"] < df["ma20_bias_pct"])  # 等价于 ma10 > ma20
            & (df["dist_to_hi20_pct"] >= -6.0)
            & (df["ret_20d_pct"].between(2.0, 35.0))
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        sub["score"] = 0.5 * sub["rs20_rank"].fillna(0.5) + 0.5 * sub["np_yoy_rank"].fillna(0.5)
        return sub.sort_values("score", ascending=False)

    def sel_f2d_canslim_bigcap_leader(df: pd.DataFrame) -> pd.DataFrame:
        """机构高流动性真龙头（日均成交 >= 5亿）× 业绩高增 × 中期趋势。"""
        m = (
            (~df["has_earnings_mine"])
            & (df["pit_days_since_notice"] <= 185)
            & (df["net_profit_yoy_pct"] >= 25.0)
            & (df["revenue_yoy_pct"] >= 15.0)
            & (df["roe_pct"] >= 8.0)
            & (df["net_profit"] >= 100_000_000.0)
            & (df["amt_20d_yi"] >= 5.0)
            & (df["above_ma20"] > 0)
            & (df["ma10_bias_pct"] < df["ma20_bias_pct"])  # 等价于 ma10 > ma20
            & (df["ret_20d_pct"].between(0.0, 30.0))
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        sub["score"] = 0.55 * sub["rs20_rank"].fillna(0.5) + 0.45 * sub["np_yoy_rank"].fillna(0.5)
        return sub.sort_values("score", ascending=False)

    # 族 ③ 绩优高增趋势回踩低吸
    def sel_f3a_growth_pullback(df: pd.DataFrame) -> pd.DataFrame:
        m = (
            (~df["has_earnings_mine"])
            & (df["pit_days_since_notice"] <= 185)
            & (df["net_profit_yoy_pct"] >= 30.0)
            & (df["revenue_yoy_pct"] >= 15.0)
            & (df["roe_pct"] >= 8.0)
            & (df["net_profit"] >= 50_000_000.0)
            & (df["amt_20d_yi"] >= 2.0)
            & (df["ma20_bias_pct"] < df["ma60_bias_pct"])  # 等价于 ma20 > ma60
            & (df["ma10_bias_pct"].between(-3.0, 2.0))
            & (df["ret_1d_pct"] > 0.5)
            & (df["vol_ratio_20d"] <= 1.3)
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        sub["score"] = 0.5 * sub["np_yoy_rank"].fillna(0.5) + 0.5 * sub["rs20_rank"].fillna(0.5)
        return sub.sort_values("score", ascending=False)

    # 阶段槽位配置：进攻期满槽(4槽80%)，平衡期0/1/2/4槽对比，防守期0槽清仓
    REG_OFF_ONLY = {"offensive": 4, "extreme_offensive": 4, "balanced": 0, "defensive": 0, "extreme_panic": 0}
    REG_OFF_BAL1 = {"offensive": 4, "extreme_offensive": 4, "balanced": 1, "defensive": 0, "extreme_panic": 0}
    REG_OFF_BAL2 = {"offensive": 4, "extreme_offensive": 4, "balanced": 2, "defensive": 0, "extreme_panic": 0}
    REG_OFF_BAL4 = {"offensive": 4, "extreme_offensive": 4, "balanced": 4, "defensive": 0, "extreme_panic": 0}

    experiments: list[dict[str, Any]] = [
        {"id": "B1_Pure_LowVol5Yi_OffOnly", "fn": sel_b1_lowvol_pure, "reg": REG_OFF_ONLY, "trail": 0.10, "hold": 60, "exit_def": True},
        {"id": "F1A_PEAD_Official_OffOnly", "fn": sel_f1a_pead_official, "reg": REG_OFF_ONLY, "trail": 0.10, "hold": 40, "exit_def": True},
        {"id": "F1B_PEAD_AllEvents_OffOnly", "fn": sel_f1b_pead_all_events, "reg": REG_OFF_ONLY, "trail": 0.10, "hold": 40, "exit_def": True},
        {"id": "F1C_PEAD_StrictLag1_OffOnly", "fn": sel_f1c_pead_strict_lag1, "reg": REG_OFF_ONLY, "trail": 0.10, "hold": 40, "exit_def": True},
        {"id": "F1C2_PEAD_StrictLag1_OffBal", "fn": sel_f1c_pead_strict_lag1, "reg": REG_OFF_BAL4, "trail": 0.10, "hold": 40, "exit_def": True},
        {"id": "F2A_CANSLIM_Core_OffOnly", "fn": sel_f2a_canslim_core, "reg": REG_OFF_ONLY, "trail": 0.10, "hold": 60, "exit_def": True},
        {"id": "F2A2_CANSLIM_Core_OffBal2", "fn": sel_f2a_canslim_core, "reg": REG_OFF_BAL2, "trail": 0.10, "hold": 60, "exit_def": True},
        {"id": "F2B_LowVol_PlusFund_OffOnly", "fn": sel_f2b_lowvol_plus_fund, "reg": REG_OFF_ONLY, "trail": 0.10, "hold": 60, "exit_def": True},
        {"id": "F2C_CANSLIM_Accel_OffOnly", "fn": sel_f2c_canslim_accel, "reg": REG_OFF_ONLY, "trail": 0.12, "hold": 60, "exit_def": True},
        {"id": "F2C2_CANSLIM_Accel_OffBal1", "fn": sel_f2c_canslim_accel, "reg": REG_OFF_BAL1, "trail": 0.12, "hold": 60, "exit_def": True},
        {"id": "F2C3_CANSLIM_Accel_OffBal4", "fn": sel_f2c_canslim_accel, "reg": REG_OFF_BAL4, "trail": 0.12, "hold": 60, "exit_def": True},
        {"id": "F2D_BigCap_Leader_OffOnly", "fn": sel_f2d_canslim_bigcap_leader, "reg": REG_OFF_ONLY, "trail": 0.12, "hold": 60, "exit_def": True},
        {"id": "F2D2_BigCap_Leader_OffBal1", "fn": sel_f2d_canslim_bigcap_leader, "reg": REG_OFF_BAL1, "trail": 0.12, "hold": 60, "exit_def": True},
        {"id": "F2D3_BigCap_Leader_OffBal4", "fn": sel_f2d_canslim_bigcap_leader, "reg": REG_OFF_BAL4, "trail": 0.12, "hold": 60, "exit_def": True},
        {"id": "F3A_Growth_Pullback_OffOnly", "fn": sel_f3a_growth_pullback, "reg": REG_OFF_ONLY, "trail": 0.10, "hold": 45, "exit_def": True},
    ]

    results: list[dict[str, Any]] = []
    stock_runs: dict[str, tuple[pd.Series, pd.Series, dict[str, Any]]] = {}

    # 基准先入表
    results.append({
        "id": "B0_CSI300_Benchmark",
        "kind": "benchmark",
        "metrics": _pack_metrics(hs300_nav, splits),
        "meta": {"avg_exposure_pct": 100.0},
    })
    results.append({
        "id": "B2_AR3_6_CrossAssetETF_Top2+Panic",
        "kind": "benchmark",
        "metrics": _pack_metrics(ar3_etf_nav, splits),
        "meta": {"avg_exposure_pct": 100.0},
    })

    print("\n--- [Phase 1] 独立个股策略族 Mark-to-Market 实证（已修复真实除权除息结算） ---", flush=True)
    for exp in experiments:
        nav_s, wt_s, trades, meta = simulate_ar3f_stock(
            day_df,
            by_date_rows,
            px_lookup,
            div_lookup,
            exp["fn"],
            name=exp["id"],
            regime_slots=exp["reg"],
            slot_weight=0.20,
            trail_stop_pct=exp["trail"],
            hard_stop_pct=-0.10,
            max_hold_days=exp["hold"],
            one_per_industry=True,
            exit_on_defensive=exp["exit_def"],
            strict_notice_audit=(exp["id"] != "B1_Pure_LowVol5Yi_OffOnly"),
        )
        m = _pack_metrics(nav_s, splits)
        stock_runs[exp["id"]] = (nav_s, wt_s, meta)
        results.append({
            "id": exp["id"],
            "kind": "stock",
            "metrics": m,
            "meta": meta,
        })
        print(
            f"  {exp['id']:30s} | 全={m['full']['cum_ret_pct']:+6.1f}% (年{m['full']['ann_ret_pct']:+5.1f}%) "
            f"DD={m['full']['max_dd_pct']:4.1f}% Sh={m['full']['sharpe']:4.2f} | "
            f"IS={m['is']['cum_ret_pct']:+5.1f}% Val={m['val']['cum_ret_pct']:+5.1f}% OOS={m['oos']['cum_ret_pct']:+5.1f}% | "
            f"笔={meta['trades']:3d} 胜={meta['win_rate_pct']:4.1f}% 盈亏={meta['plr']:4.2f} 仓={meta['avg_exposure_pct']:4.1f}% "
            f"exTop2={meta['ex_top2_est_cum_pct']:+5.1f}% div={meta['dividend_events_settled']}",
            flush=True,
        )

    # 5. 选出三段表现最稳健的前 4 个 AR3-F 个股策略，与 #02 AR3_6_CrossAssetETF 做真·单账户零杠杆双核融合
    f_candidates = [
        r for r in results
        if r["kind"] == "stock" and r["id"].startswith("F")
    ]
    def _rank_key(r: dict[str, Any]) -> float:
        f = r["metrics"]["full"]
        tr, va, oo = r["metrics"]["is"]["cum_ret_pct"], r["metrics"]["val"]["cum_ret_pct"], r["metrics"]["oos"]["cum_ret_pct"]
        pos_segs = sum(1 for x in (tr, va, oo) if x > 0)
        ex2 = r["meta"]["ex_top2_est_cum_pct"]
        return pos_segs * 10.0 + f["sharpe"] * 10.0 + f["calmar"] * 4.0 + (ex2 * 0.15)

    f_candidates.sort(key=_rank_key, reverse=True)
    top_ids = [r["id"] for r in f_candidates[:4]]

    print("\n--- [Phase 2] 单账户零杠杆（gross<=100%）双核融合：AR3-F 胜出个股 + #02 跨资产 ETF ---", flush=True)
    for sid in top_ids:
        s_nav, s_wt, _ = stock_runs[sid]
        for cap in (0.30, 0.50):
            combo_id = f"DUAL_{int(cap*100)}pct_{sid}+AR3_6_ETF"
            c_nav, c_meta = simulate_dual_core_single_account(
                day_df, s_nav, s_wt, ar3_etf_nav, stock_budget_cap=cap
            )
            m = _pack_metrics(c_nav, splits)
            results.append({
                "id": combo_id,
                "kind": "dual_core",
                "metrics": m,
                "meta": c_meta,
            })
            print(
                f"  {combo_id:52s} | 全={m['full']['cum_ret_pct']:+6.1f}% (年{m['full']['ann_ret_pct']:+5.1f}%) "
                f"DD={m['full']['max_dd_pct']:4.1f}% Sh={m['full']['sharpe']:4.2f} Cal={m['full']['calmar']:4.2f} | "
                f"IS={m['is']['cum_ret_pct']:+5.1f}% Val={m['val']['cum_ret_pct']:+5.1f}% OOS={m['oos']['cum_ret_pct']:+5.1f}% | "
                f"股均仓={c_meta['avg_stock_weight_pct']:4.1f}% ETF均仓={c_meta['avg_etf_weight_pct']:4.1f}% maxGross={c_meta['max_gross_exposure_pct']:.1f}%",
                flush=True,
            )

    # 6. 输出总结榜并落盘 JSON
    out_path = runtime_path("mining_runs", "ar3f_fundamental_trend_v1.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sample_window": {"start": dates[0], "end": dates[-1], "days": len(dates)},
        "top_ar3f_stock_ids": top_ids,
        "results": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n实验完成 ({time.time() - t_all:.1f}s)，完整结果已落盘: {out_path}", flush=True)


if __name__ == "__main__":
    main()
