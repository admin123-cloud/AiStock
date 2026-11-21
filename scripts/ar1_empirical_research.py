# -*- coding: utf-8 -*-
"""AR1（AI Research #1）全市场大样本实证回测引擎。

严格基于全市场 4,995 只流动性达标股（>=5000万）近 4 年（2023-01-03 ~ 2026-11-20，940 交易日）
Parquet 特征宽表，分四项子课题执行无未来函数、扣真实摩擦的样本外实证：
  - AR1-1: 大盘择时状态机对比（旧 R51 vs 纯宽度 vs AR1 复合五态）
  - AR1-2: 动态仓位水位矩阵 × 减仓步长寻优（IS / Val / OOS 三段卡玛比率裁决）
  - AR1-3: AR1 分周期冠军策略全天候组合资金曲线 vs 沪深300
  - AR1-4: 硬止损 / 止盈参数对组合尾部风险与期望收益的敏感性实证
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.mining.backtest_engine import (  # noqa: E402
    RuleSpec,
    apply_entry_conditions,
    simulate_trade_returns,
    split_trade_dates,
)
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402


def _eval_nav_metrics(daily_ret: pd.Series) -> dict[str, float]:
    n = max(1, len(daily_ret))
    nav = (1.0 + daily_ret.fillna(0.0)).cumprod()
    cum = float((nav.iloc[-1] - 1.0) * 100.0)
    years = max(0.15, n / 242.0)
    ann = float(((nav.iloc[-1]) ** (1.0 / years) - 1.0) * 100.0) if nav.iloc[-1] > 0 else -100.0
    dd = float(abs(((nav / nav.cummax()) - 1.0).min() * 100.0))
    sd = float(daily_ret.std(ddof=1)) if n > 1 else 0.0
    sharpe = float((daily_ret.mean() / sd) * math.sqrt(242.0)) if sd > 1e-8 else 0.0
    calmar = float(ann / dd) if dd > 0.5 else (ann / 0.5 if ann > 0 else 0.0)
    return {
        "cum_ret_pct": round(cum, 2),
        "ann_ret_pct": round(ann, 2),
        "max_dd_pct": round(dd, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
    }


def _simulate_waterline_curve(
    day_df: pd.DataFrame,
    regime_col: str,
    target_map: dict[str, float],
    max_step: float,
    cost_one_way: float = 0.0013,
) -> pd.Series:
    """严格 T 日收盘判定目标水位 -> T+1 开盘/盘中执行调仓（扣单边 0.13% 摩擦）-> 承受 T+1 日收益。"""
    pos = 0.50
    rets: list[float] = []
    # 使用 T-1 日判定出的目标水位决定 T 日持仓
    prev_targets = day_df[regime_col].map(lambda r: target_map.get(str(r), 0.25)).shift(1).fillna(0.50).to_numpy()
    mkt_rets = day_df["eq_ret_1d"].fillna(0.0).to_numpy() / 100.0

    for tgt, r_day in zip(prev_targets, mkt_rets):
        diff = float(tgt) - pos
        if abs(diff) > 1e-6:
            step = math.copysign(min(abs(diff), float(max_step)), diff)
            turnover = abs(step)
            pos = max(0.0, min(1.0, pos + step))
        else:
            turnover = 0.0
        net_r = pos * float(r_day) - turnover * cost_one_way
        rets.append(net_r)
    return pd.Series(rets, index=day_df["trade_date"].values, dtype=np.float64)


def run_ar1_research() -> dict[str, Any]:
    panel_df, bench_df = build_or_load_feature_panel(start_date="2023-01-01", min_amt_20d=50_000_000.0)
    sp = split_trade_dates(bench_df["trade_date"].astype(str).tolist())

    # 构建全市场日频汇总表（严格基于全市场 4,995 股真实截面）
    mkt_daily = (
        panel_df.groupby("trade_date", as_index=False)
        .agg(
            eq_ret_1d=("ret_1d_pct", "mean"),
            med_ret_1d=("ret_1d_pct", "median"),
            up_ratio=("ret_1d_pct", lambda s: float((s > 0).mean() * 100.0)),
            total_amt=("amount", "sum"),
        )
        .sort_values("trade_date")
    )
    day_df = bench_df.merge(mkt_daily, on="trade_date", how="inner").sort_values("trade_date").reset_index(drop=True)
    day_df["hs300_ret_1d"] = day_df["hs300_close"].pct_change() * 100.0
    day_df["amt_ma5"] = day_df["total_amt"].rolling(5, min_periods=3).mean()
    day_df["amt_ratio_5d"] = day_df["total_amt"] / (day_df["amt_ma5"] + 1.0)

    # 未来 5 日 / 10 日全市场等权累计收益（仅用于评估择时准确度，绝不进入信号）
    eq_nav = (1.0 + day_df["eq_ret_1d"].fillna(0.0) / 100.0).cumprod()
    day_df["fwd_eq_5d"] = (eq_nav.shift(-5) / eq_nav - 1.0) * 100.0
    day_df["fwd_eq_10d"] = (eq_nav.shift(-10) / eq_nav - 1.0) * 100.0
    day_df["fwd_hs_5d"] = (day_df["hs300_close"].shift(-5) / day_df["hs300_close"] - 1.0) * 100.0

    # --- 构造 3 套大盘择时状态机（严格 T 日收盘已知） ---
    close_sh = day_df["idx_close"].astype(float)
    ma20_sh = close_sh.rolling(20, min_periods=10).mean()
    mom20 = day_df["idx_mom20_pct"].fillna(0.0)
    mom60 = day_df["idx_mom60_pct"].fillna(0.0)
    breadth = day_df["market_above_ma20_pct"].fillna(50.0)
    vol_r = day_df["amt_ratio_5d"].fillna(1.0)

    # Model A: 旧 R51 单指数均线 N=3
    raw_up_a = (close_sh > ma20_sh) & (mom60 > 0)
    raw_dn_a = (close_sh < ma20_sh) & (mom60 < -1.0)
    up3_a = raw_up_a.astype(int).rolling(3, min_periods=3).sum() == 3
    dn3_a = raw_dn_a.astype(int).rolling(3, min_periods=3).sum() == 3
    day_df["regime_r51_old"] = np.where(dn3_a, "defensive", np.where(up3_a, "offensive", "balanced"))

    # Model B: 纯全市场宽度模型
    day_df["regime_breadth_only"] = np.where(
        breadth < 28.0, "defensive", np.where(breadth >= 55.0, "offensive", "balanced")
    )

    # Model C: AR1 复合五态状态机（全市场宽度 × 上证趋势动量 × 成交额量比，N=2 快速防守 / N=3 稳健进攻）
    # 防守宁快勿慢，进攻宁稳勿急
    raw_def_c = (breadth < 30.0) | ((close_sh < ma20_sh) & (mom20 < -2.0))
    def2_c = raw_def_c.astype(int).rolling(2, min_periods=2).sum() == 2
    raw_off_c = (breadth >= 52.0) & (close_sh > ma20_sh) & (mom20 > 1.0)
    off3_c = raw_off_c.astype(int).rolling(3, min_periods=3).sum() == 3

    ar1_regimes: list[str] = []
    for d2, o3, b, m20, vr in zip(def2_c, off3_c, breadth, mom20, vol_r):
        if d2 and b <= 18.0:
            ar1_regimes.append("extreme_panic")
        elif d2 or b < 25.0:
            ar1_regimes.append("defensive")
        elif o3 and b >= 75.0 and m20 >= 6.0 and vr >= 1.05:
            ar1_regimes.append("extreme_offensive")
        elif o3:
            ar1_regimes.append("offensive")
        else:
            ar1_regimes.append("balanced")
    day_df["regime_ar1"] = ar1_regimes
    # 归并三态便于横向对比区分度
    day_df["regime_ar1_3"] = day_df["regime_ar1"].replace(
        {"extreme_panic": "defensive", "extreme_offensive": "offensive"}
    )

    # =========================================================================
    # AR1-1: 大盘择时状态机区分度实证（全样本 + OOS）
    # =========================================================================
    ar1_1: dict[str, Any] = {}
    for m_name, col in [
        ("Model_A_Old_R51", "regime_r51_old"),
        ("Model_B_Breadth_Only", "regime_breadth_only"),
        ("Model_C_AR1_Composite", "regime_ar1_3"),
    ]:
        rows = []
        for state in ("offensive", "balanced", "defensive"):
            sub = day_df[day_df[col] == state]
            rows.append(
                {
                    "state": state,
                    "days": int(len(sub)),
                    "fwd_eq_5d_avg": round(float(sub["fwd_eq_5d"].mean()), 2) if len(sub) else 0.0,
                    "fwd_eq_5d_win_pct": round(float((sub["fwd_eq_5d"] > 0).mean() * 100.0), 1) if len(sub) else 0.0,
                    "fwd_eq_10d_avg": round(float(sub["fwd_eq_10d"].mean()), 2) if len(sub) else 0.0,
                    "fwd_hs_5d_avg": round(float(sub["fwd_hs_5d"].mean()), 2) if len(sub) else 0.0,
                }
            )
        # 计算进攻 vs 防守的 5 日收益剪刀差（越大说明择时区分度越强）
        off_v = next((x["fwd_eq_5d_avg"] for x in rows if x["state"] == "offensive"), 0.0)
        def_v = next((x["fwd_eq_5d_avg"] for x in rows if x["state"] == "defensive"), 0.0)
        # 计算简单择时（进攻100% / 平衡50% / 防守0%）的组合卡玛
        curve = _simulate_waterline_curve(
            day_df, col, {"offensive": 1.0, "balanced": 0.5, "defensive": 0.0}, max_step=1.0
        )
        oos_d0, oos_d1 = sp["oos"]
        oos_mask = (day_df["trade_date"] >= oos_d0) & (day_df["trade_date"] <= oos_d1)
        ar1_1[m_name] = {
            "states": rows,
            "off_minus_def_5d_spread": round(off_v - def_v, 2),
            "full_curve": _eval_nav_metrics(curve),
            "oos_curve": _eval_nav_metrics(curve[oos_mask.values]),
        }

    # 同时输出 AR1 五态细分布
    ar1_5_states = []
    for st in ("extreme_offensive", "offensive", "balanced", "defensive", "extreme_panic"):
        sub = day_df[day_df["regime_ar1"] == st]
        ar1_5_states.append(
            {
                "state": st,
                "days": int(len(sub)),
                "fwd_eq_5d_avg": round(float(sub["fwd_eq_5d"].mean()), 2) if len(sub) else 0.0,
                "fwd_eq_5d_win_pct": round(float((sub["fwd_eq_5d"] > 0).mean() * 100.0), 1) if len(sub) else 0.0,
                "fwd_eq_10d_avg": round(float(sub["fwd_eq_10d"].mean()), 2) if len(sub) else 0.0,
            }
        )

    # =========================================================================
    # AR1-2: 仓位水位矩阵 × 减仓步长寻优（全样本 + IS/Val/OOS）
    # =========================================================================
    waterline_grids = [
        ("BuyHold_100", {"extreme_offensive": 1.0, "offensive": 1.0, "balanced": 1.0, "defensive": 1.0, "extreme_panic": 1.0}, 1.0),
        ("Old_R51_25_50_75_step15", {"extreme_offensive": 0.75, "offensive": 0.75, "balanced": 0.50, "defensive": 0.25, "extreme_panic": 0.25}, 0.15),
        ("AR1_0_40_80_step15", {"extreme_offensive": 0.85, "offensive": 0.80, "balanced": 0.40, "defensive": 0.0, "extreme_panic": 0.0}, 0.15),
        ("AR1_0_40_80_step30", {"extreme_offensive": 0.85, "offensive": 0.80, "balanced": 0.40, "defensive": 0.0, "extreme_panic": 0.0}, 0.30),
        ("AR1_0_40_80_step100", {"extreme_offensive": 0.85, "offensive": 0.80, "balanced": 0.40, "defensive": 0.0, "extreme_panic": 0.0}, 1.00),
        ("AR1_10_45_85_step30", {"extreme_offensive": 0.90, "offensive": 0.85, "balanced": 0.45, "defensive": 0.10, "extreme_panic": 0.0}, 0.30),
        ("AR1_0_30_80_step100", {"extreme_offensive": 0.85, "offensive": 0.80, "balanced": 0.30, "defensive": 0.0, "extreme_panic": 0.0}, 1.00),
    ]
    ar1_2: list[dict[str, Any]] = []
    for name, wmap, step in waterline_grids:
        c = _simulate_waterline_curve(day_df, "regime_ar1", wmap, max_step=step)
        seg_m = {}
        for sk, (d0, d1) in sp.items():
            m = (day_df["trade_date"] >= d0) & (day_df["trade_date"] <= d1)
            seg_m[sk] = _eval_nav_metrics(c[m.values])
        ar1_2.append(
            {
                "config": name,
                "step": step,
                "full": _eval_nav_metrics(c),
                **seg_m,
            }
        )
    ar1_2.sort(key=lambda x: x["full"]["calmar"], reverse=True)

    # =========================================================================
    # AR1-3: 分周期冠军策略全天候组合资金曲线 vs 沪深300
    # =========================================================================
    # 将 AR1 复合周期映射到 panel_df
    reg_map = dict(zip(day_df["trade_date"], day_df["regime_ar1"]))
    panel_ar1 = panel_df.copy()
    panel_ar1["ar1_regime"] = panel_ar1["trade_date"].map(reg_map)

    champ_rules = {
        "offensive": RuleSpec(
            rule_id="ar1_offense_mw04",
            name="AR1进攻主升龙头",
            template_id="mainwave_offense",
            target_regimes=["offensive"],
            entry_conditions=[
                {"feature": "rs_20d_vs_hs300", "op": ">=", "value": 10.0},
                {"feature": "close_pos_in_day", "op": ">=", "value": 0.70},
                {"feature": "upper_shadow_pct", "op": "<=", "value": 1.8},
                {"feature": "atr_14_pct", "op": "<=", "value": 6.5},
            ],
            exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.15},
            top_k_per_day=3,
            rank_by="rs_20d_vs_hs300",
        ),
        "extreme_offensive": RuleSpec(
            rule_id="ar1_extreme_offense_mw01",
            name="AR1高潮突破龙头",
            template_id="mainwave_offense",
            target_regimes=["extreme_offensive"],
            entry_conditions=[
                {"feature": "ma_bull_align", "op": "==", "value": 1.0},
                {"feature": "industry_ret_5d_rank", "op": ">=", "value": 0.70},
                {"feature": "dist_to_hi20_pct", "op": ">=", "value": -4.0},
                {"feature": "vol_ratio_5d", "op": ">=", "value": 1.25},
            ],
            exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.15},
            top_k_per_day=3,
            rank_by="rs_20d_vs_hs300",
        ),
        "balanced": RuleSpec(
            rule_id="ar1_balanced_rot05",
            name="AR1平衡主线补涨",
            template_id="sector_rotation",
            target_regimes=["balanced"],
            entry_conditions=[
                {"feature": "industry_above_ma20_pct", "op": ">=", "value": 72.65},
                {"feature": "rs_5d_vs_hs300", "op": "<=", "value": 0.12},
                {"feature": "pos_20d", "op": ">=", "value": 0.25},
            ],
            exit_policy={"hold_days": 7, "stop_loss_pct": -0.07, "take_profit_pct": 0.10},
            top_k_per_day=3,
            rank_by="industry_ret_5d_rank",
        ),
    }

    # 收集全天候组合所有触发交易，并按周期配额加权（进攻80% / 高潮85% / 平衡40% / 防守0%）
    weight_by_reg = {"offensive": 0.80, "extreme_offensive": 0.85, "balanced": 0.40}
    daily_port_ret = pd.Series(0.0, index=day_df["trade_date"].values, dtype=np.float64)
    all_trades_list: list[pd.DataFrame] = []

    for rg, rspec in champ_rules.items():
        sub_p = panel_ar1[panel_ar1["ar1_regime"] == rg]
        mask = apply_entry_conditions(sub_p, rspec.entry_conditions, target_regimes=None)
        cand = sub_p.loc[mask].copy()
        if cand.empty:
            continue
        cand.sort_values(["trade_date", rspec.rank_by], ascending=[True, False], inplace=True)
        cand = cand.groupby("trade_date", sort=False).head(rspec.top_k_per_day).copy()
        hd = int(rspec.exit_policy["hold_days"])
        rets, _ = simulate_trade_returns(
            cand,
            hold_days=hd,
            stop_loss_pct=float(rspec.exit_policy["stop_loss_pct"]),
            take_profit_pct=float(rspec.exit_policy["take_profit_pct"]),
        )
        cand["net_ret_pct"] = rets
        cand["ar1_source_regime"] = rg
        all_trades_list.append(cand)
        w = weight_by_reg[rg]
        day_mean = cand.groupby("trade_date")["net_ret_pct"].mean() * (w / float(hd)) / 100.0
        for dt, val in day_mean.items():
            if dt in daily_port_ret.index:
                daily_port_ret.loc[dt] += float(val)

    hs_daily_ret = pd.Series(day_df["hs300_ret_1d"].fillna(0.0).values / 100.0, index=day_df["trade_date"].values)
    ar1_3: dict[str, Any] = {
        "full": {
            "ar1_portfolio": _eval_nav_metrics(daily_port_ret),
            "hs300_benchmark": _eval_nav_metrics(hs_daily_ret),
        },
        "segments": {},
    }
    for sk, (d0, d1) in sp.items():
        m = (day_df["trade_date"] >= d0) & (day_df["trade_date"] <= d1)
        ar1_3["segments"][sk] = {
            "from": d0,
            "to": d1,
            "ar1_portfolio": _eval_nav_metrics(daily_port_ret[m.values]),
            "hs300_benchmark": _eval_nav_metrics(hs_daily_ret[m.values]),
        }

    # =========================================================================
    # AR1-4: 硬止损与止盈参数敏感性实证（在全天候候选交易集上）
    # =========================================================================
    ar1_4: list[dict[str, Any]] = []
    if all_trades_list:
        union_trades = pd.concat(all_trades_list, ignore_index=True)
        for sl in (-0.05, -0.07, -0.10, -0.30):
            for tp in (0.10, 0.15, 0.25):
                rets, _ = simulate_trade_returns(union_trades, hold_days=5, stop_loss_pct=sl, take_profit_pct=tp)
                wins = rets[rets > 0]
                losses = rets[rets <= 0]
                wr = float(len(wins) / len(rets) * 100.0) if len(rets) else 0.0
                plr = float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else 0.0
                p5 = float(np.percentile(rets, 5)) if len(rets) else 0.0
                ar1_4.append(
                    {
                        "stop_loss_pct": int(sl * 100),
                        "take_profit_pct": int(tp * 100),
                        "trades": int(len(rets)),
                        "avg_ret_pct": round(float(rets.mean()), 3),
                        "win_rate_pct": round(wr, 1),
                        "pl_ratio": round(plr, 2),
                        "p5_tail_loss_pct": round(p5, 2),
                    }
                )
        ar1_4.sort(key=lambda x: x["avg_ret_pct"], reverse=True)

    result = {
        "schema": "ar1_empirical_research/1",
        "splits": sp,
        "ar1_1_timing_models": ar1_1,
        "ar1_1_five_states": ar1_5_states,
        "ar1_2_waterline_and_step": ar1_2,
        "ar1_3_full_portfolio": ar1_3,
        "ar1_4_stop_tp_matrix": ar1_4[:8],
    }

    out_path = runtime_path("mining_runs") / "ar1_empirical_research_v1.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    res = run_ar1_research()
    print("=== AR1-1 大盘择时状态机对比（进攻/平衡/防守 5日剪刀差 & OOS 曲线） ===")
    for k, v in res["ar1_1_timing_models"].items():
        print(f"  {k:24s} | 剪刀差={v['off_minus_def_5d_spread']:+.2f}% | 全样本={v['full_curve']} | OOS={v['oos_curve']}")
    print("=== AR1 五态细分布 ===")
    for s in res["ar1_1_five_states"]:
        print(f"  {s['state']:18s} | {s['days']:3d}天 | 未来5日均={s['fwd_eq_5d_avg']:+.2f}% (胜率{s['fwd_eq_5d_win_pct']}%) | 未来10日均={s['fwd_eq_10d_avg']:+.2f}%")
    print("=== AR1-2 水位矩阵 × 减仓步长排行（按全样本卡玛比率） ===")
    for r in res["ar1_2_waterline_and_step"]:
        print(f"  {r['config']:26s} | Full={r['full']} | OOS={r['oos']}")
    print("=== AR1-3 分周期冠军全天候组合 vs 沪深300 ===")
    print("  FULL:", res["ar1_3_full_portfolio"]["full"])
    for sk, sv in res["ar1_3_full_portfolio"]["segments"].items():
        print(f"  {sk.upper():4s} [{sv['from']}~{sv['to']}]: AR1={sv['ar1_portfolio']} | HS300={sv['hs300_benchmark']}")
    print("=== AR1-4 止损/止盈敏感性 Top 6 ===")
    for r in res["ar1_4_stop_tp_matrix"][:6]:
        print(f"  SL={r['stop_loss_pct']:3d}% TP=+{r['take_profit_pct']:2d}% | 均={r['avg_ret_pct']:+.3f}% 胜率={r['win_rate_pct']}% 盈亏比={r['pl_ratio']} P5尾部={r['p5_tail_loss_pct']:+.2f}%")
