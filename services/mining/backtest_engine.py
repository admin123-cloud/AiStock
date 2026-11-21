# -*- coding: utf-8 -*-
"""AI 策略挖掘工坊 —— 严格向量化日频组合回测引擎。

特性：
1. 强制三段严格时间切分（IS 训练 60% / Val 验证 20% / OOS 盲测样本外 20%）；
2. 真实 A 股摩擦：双边佣金 0.03% × 2 + 卖出印花税 0.05% + 冲击滑点 0.15% = 0.26% 往返；
3. 严格 T+1 开盘撮合，跳过一字涨停买不进，卖出日一字跌停顺延至下一可成交日；
4. 每日等权槽位上限（top_k），避免牛市单日触发数百股造成收益失真；
5. 完整统计胜率、盈亏比、期望收益、组合年化、相对沪深300超额、最大回撤、夏普、剔除最大盈利月 OOS 收益。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

# A 股真实往返交易成本（佣金 0.03%×2 + 印花税 0.05% + 滑点 0.15%）
ROUND_TRIP_COST_PCT = 0.26


@dataclass
class RuleSpec:
    rule_id: str
    name: str
    template_id: str
    target_regimes: list[str]
    entry_conditions: list[dict[str, Any]]
    exit_policy: dict[str, Any] = field(
        default_factory=lambda: {"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.15}
    )
    top_k_per_day: int = 3
    rank_by: str = "rs_20d_vs_hs300"
    rank_ascending: bool = False
    source: str = "tree_miner"
    thesis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "template_id": self.template_id,
            "target_regimes": list(self.target_regimes),
            "entry_conditions": [dict(c) for c in self.entry_conditions],
            "exit_policy": dict(self.exit_policy),
            "top_k_per_day": int(self.top_k_per_day),
            "rank_by": self.rank_by,
            "rank_ascending": bool(self.rank_ascending),
            "source": self.source,
            "thesis": self.thesis,
        }


def split_trade_dates(
    trade_dates: Sequence[str],
    *,
    is_ratio: float = 0.60,
    val_ratio: float = 0.20,
) -> dict[str, tuple[str, str]]:
    """按严格时间顺序切分 IS / Val / OOS 三个互不重叠的区间。"""
    uniq = sorted({str(d) for d in trade_dates})
    n = len(uniq)
    if n < 30:
        raise ValueError(f"有效交易日不足（仅 {n} 日），无法执行三段切分")
    i1 = max(10, int(n * is_ratio))
    i2 = max(i1 + 5, int(n * (is_ratio + val_ratio)))
    i2 = min(i2, n - 5)
    return {
        "is": (uniq[0], uniq[i1 - 1]),
        "val": (uniq[i1], uniq[i2 - 1]),
        "oos": (uniq[i2], uniq[-1]),
    }


def apply_entry_conditions(
    df: pd.DataFrame,
    conditions: Sequence[Mapping[str, Any]],
    target_regimes: Optional[Sequence[str]] = None,
) -> np.ndarray:
    """将明文 DSL 入场条件向量化编译为布尔掩码。"""
    mask = df["entry_tradable"].to_numpy(dtype=bool, copy=True)
    if target_regimes:
        reg_set = {str(r) for r in target_regimes}
        mask &= df["market_regime"].isin(reg_set).to_numpy(dtype=bool)

    for cond in conditions:
        feat = str(cond.get("feature") or "")
        op = str(cond.get("op") or ">=").strip()
        val = float(cond.get("value", 0.0))
        if feat not in df.columns:
            return np.zeros(len(df), dtype=bool)
        col = df[feat].to_numpy(dtype=np.float64)
        valid = np.isfinite(col)
        if op == ">=":
            mask &= valid & (col >= val)
        elif op == ">":
            mask &= valid & (col > val)
        elif op == "<=":
            mask &= valid & (col <= val)
        elif op == "<":
            mask &= valid & (col < val)
        elif op == "==":
            mask &= valid & (col == val)
        else:
            raise ValueError(f"不支持的运算符: {op}")
    return mask


def simulate_trade_returns(
    sub_df: pd.DataFrame,
    *,
    hold_days: int = 5,
    stop_loss_pct: float = -0.07,
    take_profit_pct: float = 0.15,
    round_trip_cost_pct: float = ROUND_TRIP_COST_PCT,
) -> tuple[np.ndarray, np.ndarray]:
    """对候选交易行逐日模拟止损/止盈与跌停顺延，返回 (净收益率%, 实际持有天数)。

    - 买入价：T+1 日开盘价 `next_open`
    - A 股 T+1 制度：T+1 当日不可卖出，从 T+2（d=2）开始检查止损/止盈；
    - 若某日盘中最低跌破止损价（且非一字跌停），按止损价（或当日开盘价若低开破位）成交；
    - 若某日盘中最高触及止盈价，按止盈价（或当日开盘价若高开越过）成交；
    - 到 `hold_days` 当日收盘按收盘价平仓（若一字跌停则顺延至下一可成交日）。
    """
    n = len(sub_df)
    if n == 0:
        return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.int32)

    hold_days = max(2, min(10, int(hold_days)))
    entry_px = sub_df["next_open"].to_numpy(dtype=np.float64)
    ld_thresh = sub_df["ld_thresh"].to_numpy(dtype=np.float64)

    stop_px = entry_px * (1.0 + float(stop_loss_pct))
    tp_px = entry_px * (1.0 + float(take_profit_pct))

    exit_px = np.full(n, np.nan, dtype=np.float64)
    exit_day = np.full(n, hold_days, dtype=np.int32)
    exited = np.zeros(n, dtype=bool)

    prev_close_arr = sub_df["next_close"].to_numpy(dtype=np.float64)

    for d in range(2, 11):
        o = sub_df[f"fwd_open_{d}"].to_numpy(dtype=np.float64)
        h = sub_df[f"fwd_high_{d}"].to_numpy(dtype=np.float64)
        l = sub_df[f"fwd_low_{d}"].to_numpy(dtype=np.float64)
        c = sub_df[f"fwd_close_{d}"].to_numpy(dtype=np.float64)

        valid_bar = np.isfinite(o) & np.isfinite(c) & (o > 0) & (c > 0)
        # 一字跌停判断：开盘==最高==最低 且 <= 昨收 * 跌停阈值
        is_locked_down = valid_bar & (o == h) & (h == l) & (c <= prev_close_arr * ld_thresh)
        can_sell = (~exited) & valid_bar & (~is_locked_down)

        # 1. 止损检查（保守优先：若同日既触止损又触止盈，按先止损处理）
        hit_sl = can_sell & (l <= stop_px)
        if np.any(hit_sl):
            fill_sl = np.minimum(o[hit_sl], stop_px[hit_sl])
            exit_px[hit_sl] = fill_sl
            exit_day[hit_sl] = d
            exited[hit_sl] = True

        # 2. 止盈检查
        can_sell2 = (~exited) & valid_bar & (~is_locked_down)
        hit_tp = can_sell2 & (h >= tp_px)
        if np.any(hit_tp):
            fill_tp = np.maximum(o[hit_tp], tp_px[hit_tp])
            exit_px[hit_tp] = fill_tp
            exit_day[hit_tp] = d
            exited[hit_tp] = True

        # 3. 到达目标持有期（或跌停顺延后首个可卖出日）按收盘价平仓
        if d >= hold_days:
            due = (~exited) & valid_bar & (~is_locked_down)
            if np.any(due):
                exit_px[due] = c[due]
                exit_day[due] = d
                exited[due] = True

        prev_close_arr = np.where(valid_bar, c, prev_close_arr)

    # 极端情况（末尾不足 hold_days 交易日）：回退到最近有效收盘价
    still_open = ~exited
    if np.any(still_open):
        fallback = prev_close_arr[still_open]
        exit_px[still_open] = np.where(np.isfinite(fallback) & (fallback > 0), fallback, entry_px[still_open])

    gross_ret_pct = (exit_px / entry_px - 1.0) * 100.0
    net_ret_pct = gross_ret_pct - float(round_trip_cost_pct)
    return net_ret_pct, exit_day


def _compute_segment_metrics(
    trades_df: pd.DataFrame,
    bench_seg: pd.DataFrame,
    hold_days: int,
) -> dict[str, Any]:
    """计算单个时间段（IS / Val / OOS）的组合与交易级统计指标。"""
    bench_dates = sorted(bench_seg["trade_date"].astype(str).unique())
    n_days = max(1, len(bench_dates))
    bench_cum_pct = 0.0
    if len(bench_seg) >= 2:
        c0 = float(bench_seg.iloc[0]["hs300_close"])
        c1 = float(bench_seg.iloc[-1]["hs300_close"])
        if c0 > 0:
            bench_cum_pct = round((c1 / c0 - 1.0) * 100.0, 2)

    if trades_df.empty:
        return {
            "trades_count": 0,
            "active_days": 0,
            "win_rate_pct": 0.0,
            "avg_ret_pct": 0.0,
            "profit_loss_ratio": 0.0,
            "cum_ret_pct": 0.0,
            "ann_ret_pct": 0.0,
            "bench_cum_ret_pct": bench_cum_pct,
            "excess_ret_pct": round(-bench_cum_pct, 2),
            "max_drawdown_pct": 0.0,
            "sharpe": 0.0,
            "calmar": 0.0,
            "ex_best_month_cum_ret_pct": 0.0,
            "monthly_returns": [],
        }

    rets = trades_df["net_ret_pct"].to_numpy(dtype=np.float64)
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    win_rate = float(len(wins) / len(rets) * 100.0)
    avg_win = float(wins.mean()) if len(wins) > 0 else 0.0
    avg_loss = float(abs(losses.mean())) if len(losses) > 0 else 0.0
    pl_ratio = round(avg_win / avg_loss, 2) if avg_loss > 1e-6 else (9.99 if avg_win > 0 else 0.0)

    # 组合日频净值近似：将每日新开仓的等权篮子收益分摊到资金池（假设最多同时持有 hold_days 个批次，每批 1/hold_days 仓位）
    daily_basket = trades_df.groupby("trade_date")["net_ret_pct"].mean() / float(max(1, hold_days))
    daily_series = pd.Series(0.0, index=bench_dates, dtype=np.float64)
    for dt, val in daily_basket.items():
        if dt in daily_series.index:
            daily_series.loc[dt] = float(val) / 100.0

    nav = (1.0 + daily_series).cumprod()
    cum_ret_pct = float((nav.iloc[-1] - 1.0) * 100.0)
    years = max(0.15, n_days / 242.0)
    ann_ret_pct = float(((nav.iloc[-1]) ** (1.0 / years) - 1.0) * 100.0) if nav.iloc[-1] > 0 else -100.0

    peak = nav.cummax()
    dd = (nav / peak - 1.0) * 100.0
    max_dd_pct = float(abs(dd.min()))

    std_d = float(daily_series.std(ddof=1)) if len(daily_series) > 1 else 0.0
    sharpe = float((daily_series.mean() / std_d) * math.sqrt(242.0)) if std_d > 1e-8 else 0.0
    calmar = float(ann_ret_pct / max_dd_pct) if max_dd_pct > 0.5 else (ann_ret_pct / 0.5 if ann_ret_pct > 0 else 0.0)

    # 按月聚合（检查是否全靠单月暴赚）
    monthly_df = pd.DataFrame({"date": daily_series.index, "r": daily_series.values})
    monthly_df["ym"] = monthly_df["date"].str.slice(0, 7)
    m_ret = monthly_df.groupby("ym")["r"].apply(lambda s: float(((1.0 + s).prod() - 1.0) * 100.0))
    monthly_rows = [{"ym": str(k), "ret_pct": round(float(v), 2)} for k, v in m_ret.items()]

    if len(m_ret) >= 2:
        best_ym = m_ret.idxmax()
        ex_best = m_ret.drop(index=best_ym)
        ex_best_cum = float(((1.0 + ex_best / 100.0).prod() - 1.0) * 100.0)
    else:
        ex_best_cum = cum_ret_pct

    return {
        "trades_count": int(len(rets)),
        "active_days": int(trades_df["trade_date"].nunique()),
        "win_rate_pct": round(win_rate, 2),
        "avg_ret_pct": round(float(rets.mean()), 3),
        "profit_loss_ratio": round(pl_ratio, 2),
        "cum_ret_pct": round(cum_ret_pct, 2),
        "ann_ret_pct": round(ann_ret_pct, 2),
        "bench_cum_ret_pct": bench_cum_pct,
        "excess_ret_pct": round(cum_ret_pct - bench_cum_pct, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "ex_best_month_cum_ret_pct": round(ex_best_cum, 2),
        "monthly_returns": monthly_rows,
    }


def evaluate_rule(
    panel_df: pd.DataFrame,
    bench_df: pd.DataFrame,
    rule: RuleSpec,
    *,
    splits: Optional[dict[str, tuple[str, str]]] = None,
) -> dict[str, Any]:
    """在 IS / Val / OOS 三段上完整评估单条明文规则。"""
    sp = splits or split_trade_dates(bench_df["trade_date"].astype(str).tolist())
    mask = apply_entry_conditions(panel_df, rule.entry_conditions, target_regimes=rule.target_regimes)
    cand = panel_df.loc[mask].copy()

    hold_days = int(rule.exit_policy.get("hold_days", 5))
    stop_loss = float(rule.exit_policy.get("stop_loss_pct", -0.07))
    take_profit = float(rule.exit_policy.get("take_profit_pct", 0.15))

    if not cand.empty:
        rank_col = rule.rank_by if rule.rank_by in cand.columns else "rs_20d_vs_hs300"
        cand.sort_values(
            ["trade_date", rank_col],
            ascending=[True, bool(rule.rank_ascending)],
            inplace=True,
        )
        # 每日等权槽位上限
        cand = cand.groupby("trade_date", sort=False).head(max(1, int(rule.top_k_per_day))).copy()
        net_rets, exit_days = simulate_trade_returns(
            cand,
            hold_days=hold_days,
            stop_loss_pct=stop_loss,
            take_profit_pct=take_profit,
        )
        cand["net_ret_pct"] = net_rets
        cand["exit_day"] = exit_days

    seg_results: dict[str, dict[str, Any]] = {}
    for seg_name in ("is", "val", "oos"):
        d0, d1 = sp[seg_name]
        b_seg = bench_df[(bench_df["trade_date"] >= d0) & (bench_df["trade_date"] <= d1)]
        if cand.empty:
            t_seg = cand
        else:
            t_seg = cand[(cand["trade_date"] >= d0) & (cand["trade_date"] <= d1)]
        seg_results[seg_name] = {
            "from_date": d0,
            "to_date": d1,
            **_compute_segment_metrics(t_seg, b_seg, hold_days=hold_days),
        }

    # 提取 OOS 最近 10 笔代表性成交样本供前端/报告审阅
    sample_trades: list[dict[str, Any]] = []
    if not cand.empty:
        oos_d0, oos_d1 = sp["oos"]
        oos_t = cand[(cand["trade_date"] >= oos_d0) & (cand["trade_date"] <= oos_d1)].tail(10)
        for _, r in oos_t.iterrows():
            sample_trades.append(
                {
                    "trade_date": str(r["trade_date"]),
                    "code": str(r["code"]),
                    "name": str(r.get("name") or r["code"]),
                    "industry": str(r.get("industry") or ""),
                    "entry_open": round(float(r["next_open"]), 2),
                    "net_ret_pct": round(float(r["net_ret_pct"]), 2),
                    "exit_day": int(r["exit_day"]),
                }
            )

    return {
        "rule": rule.to_dict(),
        "segments": seg_results,
        "oos_sample_trades": sample_trades,
    }
