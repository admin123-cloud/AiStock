# -*- coding: utf-8 -*-
"""AI 策略挖掘工坊 —— 双引擎规则挖掘器（浅层决策树规则提纯 + AI 逻辑假设族）。

严格防止信息泄漏：
- 决策树分裂与候选条件搜索 **只看 IS 训练集**；
- 出场参数（hold_days / stop_loss / take_profit）优选 **只看 Val 验证集**；
- 最终排行榜与 5 道硬门禁在 **OOS 盲测样本外** 一次性验收。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

from services.mining import MINING_TEMPLATES
from services.mining.backtest_engine import (
    RuleSpec,
    apply_entry_conditions,
    evaluate_rule,
    simulate_trade_returns,
    split_trade_dates_for_regimes,
)
from services.mining.feature_store import MINING_FEATURE_META, build_or_load_feature_panel
from services.mining.quality_gates import evaluate_mining_quality_gates
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))

# 用于纯 NumPy 决策树提纯的核心特征白名单（按模板聚焦，避免噪声特征乱分裂）
_TEMPLATE_TREE_FEATURES: dict[str, list[str]] = {
    "mainwave_offense": [
        "rs_20d_vs_hs300",
        "rs_5d_vs_hs300",
        "ma_bull_align",
        "ma20_bias_pct",
        "pos_20d",
        "pos_60d",
        "dist_to_hi20_pct",
        "vol_ratio_5d",
        "vol_ratio_20d",
        "close_pos_in_day",
        "upper_shadow_pct",
        "industry_ret_5d_rank",
        "industry_above_ma20_pct",
        "atr_14_pct",
        "ret_5d_pct",
        "ret_20d_pct",
    ],
    "panic_rebound": [
        "ret_1d_pct",
        "ret_3d_pct",
        "ret_5d_pct",
        "ret_10d_pct",
        "ma5_bias_pct",
        "ma20_bias_pct",
        "pos_20d",
        "vol_ratio_5d",
        "close_pos_in_day",
        "market_above_ma20_pct",
        "atr_14_pct",
        "industry_ret_5d_rank",
    ],
    "sideways_swing": [
        "ret_3d_pct",
        "ret_5d_pct",
        "ma10_bias_pct",
        "ma20_bias_pct",
        "pos_20d",
        "vol_ratio_5d",
        "vol_ratio_20d",
        "close_pos_in_day",
        "industry_ret_5d_rank",
        "rs_20d_vs_hs300",
        "atr_14_pct",
    ],
    "sector_rotation": [
        "industry_ret_5d_rank",
        "industry_above_ma20_pct",
        "rs_5d_vs_hs300",
        "rs_20d_vs_hs300",
        "ma_bull_align",
        "vol_ratio_5d",
        "pos_20d",
        "close_pos_in_day",
        "ret_5d_pct",
        "dist_to_hi20_pct",
    ],
}


def _runs_dir() -> Path:
    d = runtime_path("mining_runs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_template_hypotheses(template_id: str) -> list[RuleSpec]:
    """返回各周期模板内置的、有明确 A 股交易逻辑的候选假设族。"""
    tpl = MINING_TEMPLATES.get(template_id) or MINING_TEMPLATES["mainwave_offense"]
    regimes = list(tpl["target_regimes"])

    if template_id == "mainwave_offense":
        return [
            RuleSpec(
                rule_id="mw_hyp_01_trend_breakout_sector",
                name="均线多头+行业领涨+放量近新高",
                template_id=template_id,
                target_regimes=regimes,
                entry_conditions=[
                    {"feature": "ma_bull_align", "op": "==", "value": 1.0},
                    {"feature": "industry_ret_5d_rank", "op": ">=", "value": 0.70},
                    {"feature": "dist_to_hi20_pct", "op": ">=", "value": -4.0},
                    {"feature": "vol_ratio_5d", "op": ">=", "value": 1.25},
                ],
                exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.15},
                rank_by="rs_20d_vs_hs300",
                source="ai_hypothesis",
                thesis="主升期顺势抓最强主线行业中均线多头且放量突破20日新高的趋势龙头",
            ),
            RuleSpec(
                rule_id="mw_hyp_02_strong_pullback_low_shadow",
                name="强势趋势缩量回踩MA10企稳",
                template_id=template_id,
                target_regimes=regimes,
                entry_conditions=[
                    {"feature": "ma_bull_align", "op": "==", "value": 1.0},
                    {"feature": "rs_20d_vs_hs300", "op": ">=", "value": 8.0},
                    {"feature": "ma10_bias_pct", "op": "<=", "value": 2.5},
                    {"feature": "vol_ratio_5d", "op": "<=", "value": 0.90},
                    {"feature": "close_pos_in_day", "op": ">=", "value": 0.55},
                ],
                exit_policy={"hold_days": 5, "stop_loss_pct": -0.06, "take_profit_pct": 0.14},
                rank_by="industry_ret_5d_rank",
                source="ai_hypothesis",
                thesis="主升强势股不追高，等缩量回踩MA10附近且收实体阳线时低吸接力",
            ),
            RuleSpec(
                rule_id="mw_hyp_03_sector_diffusion_laggard",
                name="主线行业扩散+个股温和补涨",
                template_id=template_id,
                target_regimes=regimes,
                entry_conditions=[
                    {"feature": "industry_ret_5d_rank", "op": ">=", "value": 0.80},
                    {"feature": "industry_above_ma20_pct", "op": ">=", "value": 65.0},
                    {"feature": "ret_5d_pct", "op": "<=", "value": 6.0},
                    {"feature": "ma20_bias_pct", "op": ">=", "value": 0.0},
                    {"feature": "vol_ratio_5d", "op": ">=", "value": 1.20},
                ],
                exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.14},
                rank_by="vol_ratio_5d",
                source="ai_hypothesis",
                thesis="板块整体强势扩散时，埋伏板块内尚未大涨但当日开始放量启动的补涨龙二",
            ),
            RuleSpec(
                rule_id="mw_hyp_04_rs_momentum_clean_candle",
                name="20日高相对强度+实体强收盘+控波动",
                template_id=template_id,
                target_regimes=regimes,
                entry_conditions=[
                    {"feature": "rs_20d_vs_hs300", "op": ">=", "value": 10.0},
                    {"feature": "close_pos_in_day", "op": ">=", "value": 0.70},
                    {"feature": "upper_shadow_pct", "op": "<=", "value": 1.8},
                    {"feature": "atr_14_pct", "op": "<=", "value": 6.5},
                ],
                exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.16},
                rank_by="rs_20d_vs_hs300",
                source="ai_hypothesis",
                thesis="筛选相对沪深300持续走强、收盘实体饱满无长上影、且波动率受控的机构型主升趋势股",
            ),
            RuleSpec(
                rule_id="mw_hyp_05_mid_pos_vol_ignition",
                name="60日中位启动+放量突破+行业共振",
                template_id=template_id,
                target_regimes=regimes,
                entry_conditions=[
                    {"feature": "pos_60d", "op": ">=", "value": 0.55},
                    {"feature": "pos_60d", "op": "<=", "value": 0.90},
                    {"feature": "vol_ratio_20d", "op": ">=", "value": 1.50},
                    {"feature": "ret_1d_pct", "op": ">=", "value": 2.0},
                    {"feature": "industry_ret_5d_rank", "op": ">=", "value": 0.65},
                ],
                exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.15},
                rank_by="vol_ratio_20d",
                source="ai_hypothesis",
                thesis="避开高位透支，抓60日区间中高位刚刚放量点火且有行业共振的趋势起爆点",
            ),
        ]

    if template_id == "panic_rebound":
        return [
            RuleSpec(
                rule_id="pr_hyp_01_deep_oversold_rebound",
                name="冰点连续杀跌+长下影放量承接",
                template_id=template_id,
                target_regimes=regimes,
                entry_conditions=[
                    {"feature": "ret_5d_pct", "op": "<=", "value": -10.0},
                    {"feature": "vol_ratio_5d", "op": ">=", "value": 1.25},
                    {"feature": "close_pos_in_day", "op": ">=", "value": 0.60},
                ],
                exit_policy={"hold_days": 3, "stop_loss_pct": -0.05, "take_profit_pct": 0.08},
                rank_by="vol_ratio_5d",
                source="ai_hypothesis",
                thesis="防守/冰点期博弈5日急跌后放量收下影承接的1~3日超跌修复",
            ),
            RuleSpec(
                rule_id="pr_hyp_02_ma20_extreme_deviate",
                name="深度负乖离MA20+流动性大票反抽",
                template_id=template_id,
                target_regimes=regimes,
                entry_conditions=[
                    {"feature": "ma20_bias_pct", "op": "<=", "value": -10.0},
                    {"feature": "amt_20d_yi", "op": ">=", "value": 3.0},
                    {"feature": "ret_1d_pct", "op": "<=", "value": -3.0},
                ],
                exit_policy={"hold_days": 3, "stop_loss_pct": -0.05, "take_profit_pct": 0.08},
                rank_by="ma20_bias_pct",
                rank_ascending=True,
                source="ai_hypothesis",
                thesis="大流动性标的跌破MA20超过10%后的均值回归反抽",
            ),
        ]

    if template_id == "sideways_swing":
        return [
            RuleSpec(
                rule_id="sw_hyp_01_shrink_dip_support",
                name="横盘缩量回踩20日低位+实体企稳",
                template_id=template_id,
                target_regimes=regimes,
                entry_conditions=[
                    {"feature": "pos_20d", "op": "<=", "value": 0.25},
                    {"feature": "vol_ratio_5d", "op": "<=", "value": 0.85},
                    {"feature": "close_pos_in_day", "op": ">=", "value": 0.60},
                    {"feature": "ma60_bias_pct", "op": ">=", "value": -5.0},
                ],
                exit_policy={"hold_days": 4, "stop_loss_pct": -0.06, "take_profit_pct": 0.10},
                rank_by="industry_ret_5d_rank",
                source="ai_hypothesis",
                thesis="震荡市在中期未破位前提下，低吸20日箱体下沿缩量企稳的波段买点",
            ),
        ]

    # sector_rotation
    return [
        RuleSpec(
            rule_id="sr_hyp_01_top_sector_early_break",
            name="Top20%领涨行业+个股站上MA20放量",
            template_id=template_id,
            target_regimes=regimes,
            entry_conditions=[
                {"feature": "industry_ret_5d_rank", "op": ">=", "value": 0.80},
                {"feature": "ma20_bias_pct", "op": ">=", "value": 0.0},
                {"feature": "ma20_bias_pct", "op": "<=", "value": 8.0},
                {"feature": "vol_ratio_5d", "op": ">=", "value": 1.30},
            ],
            exit_policy={"hold_days": 5, "stop_loss_pct": -0.07, "take_profit_pct": 0.14},
            rank_by="industry_ret_5d_rank",
            source="ai_hypothesis",
            thesis="聚焦全市场5日涨幅前20%主线行业，买入刚站稳MA20且放量的轮动标的",
        ),
    ]


def mine_tree_rules_on_is(
    panel_df: pd.DataFrame,
    *,
    template_id: str,
    target_regimes: Sequence[str],
    is_range: tuple[str, str],
    hold_days: int = 5,
    max_rules: int = 6,
) -> list[RuleSpec]:
    """Pure-NumPy 浅层二元决策树 + 束搜索规则提纯器（严格只在 IS 训练集上拟合，绝不触碰 Val/OOS）。"""
    tpl = MINING_TEMPLATES.get(template_id) or MINING_TEMPLATES["mainwave_offense"]
    feats = [f for f in _TEMPLATE_TREE_FEATURES.get(template_id, list(MINING_FEATURE_META.keys())) if f in panel_df.columns]

    d0, d1 = is_range
    is_mask = (
        (panel_df["trade_date"] >= d0)
        & (panel_df["trade_date"] <= d1)
        & panel_df["entry_tradable"]
        & panel_df["market_regime"].isin(list(target_regimes))
    )
    is_df = panel_df.loc[is_mask].copy()
    if len(is_df) < 500:
        return []

    # 用扣费后的标准持有期净收益作为分裂目标（严格基于 IS 区间内）
    y_ret, _ = simulate_trade_returns(
        is_df,
        hold_days=hold_days,
        stop_loss_pct=float(tpl["default_stop_loss"]),
        take_profit_pct=float(tpl["default_take_profit"]),
    )
    valid_y = np.isfinite(y_ret)
    is_df = is_df.loc[valid_y].reset_index(drop=True)
    y_ret = y_ret[valid_y]
    if len(is_df) < 500:
        return []

    base_mean = float(y_ret.mean())
    min_leaf_n = max(200, int(len(is_df) * 0.015))

    # 预构建单特征分位分裂候选集（20%, 35%, 50%, 65%, 80%）
    feat_arrays: dict[str, np.ndarray] = {}
    cand_splits: list[tuple[str, str, float, np.ndarray]] = []
    for f in feats:
        arr = is_df[f].to_numpy(dtype=np.float64)
        feat_arrays[f] = arr
        fin = arr[np.isfinite(arr)]
        if len(fin) < min_leaf_n:
            continue
        if f == "ma_bull_align":
            qs = [1.0]
            ops = ["=="]
        else:
            qs = sorted({round(float(np.quantile(fin, q)), 3) for q in (0.20, 0.35, 0.50, 0.65, 0.80)})
            ops = [">=", "<="]
        for qv in qs:
            for op in ops:
                if op == "==":
                    m = np.isfinite(arr) & (arr == qv)
                elif op == ">=":
                    m = np.isfinite(arr) & (arr >= qv)
                else:
                    m = np.isfinite(arr) & (arr <= qv)
                cnt = int(m.sum())
                if cnt >= min_leaf_n and cnt <= int(len(is_df) * 0.85):
                    cand_splits.append((f, op, float(qv), m))

    # 束搜索（Beam Search）深度 1 -> 2 -> 3，每层保留 IS t-stat 最强且特征不重复的规则路径
    def score_mask(m: np.ndarray) -> tuple[float, float, int]:
        n = int(m.sum())
        if n < min_leaf_n:
            return -1e9, 0.0, n
        sub = y_ret[m]
        mu = float(sub.mean())
        sd = float(sub.std(ddof=1)) if n > 1 else 10.0
        t_stat = (mu - base_mean) / (sd / np.sqrt(n) + 1e-6)
        return float(t_stat), mu, n

    # 第 1 层
    beam: list[tuple[float, list[dict[str, Any]], np.ndarray]] = []
    for f, op, qv, m in cand_splits:
        sc, mu, n = score_mask(m)
        if mu > base_mean + 0.10:
            beam.append((sc, [{"feature": f, "op": op, "value": qv}], m))
    beam.sort(key=lambda x: x[0], reverse=True)
    # 同一特征只留最强 2 个，保证规则多样性
    top_l1: list[tuple[float, list[dict[str, Any]], np.ndarray]] = []
    seen_f: dict[str, int] = {}
    for item in beam:
        f = item[1][0]["feature"]
        if seen_f.get(f, 0) < 2:
            top_l1.append(item)
            seen_f[f] = seen_f.get(f, 0) + 1
        if len(top_l1) >= 18:
            break

    # 第 2 层与第 3 层扩展
    discovered: list[tuple[float, list[dict[str, Any]]]] = []
    cur_beam = top_l1
    for depth in (2, 3):
        next_beam: list[tuple[float, list[dict[str, Any]], np.ndarray]] = []
        min_leaf_depth = max(120, int(min_leaf_n * (0.65 if depth == 2 else 0.45)))
        for prev_sc, prev_conds, prev_m in cur_beam:
            used_feats = {c["feature"] for c in prev_conds}
            for f, op, qv, m in cand_splits:
                if f in used_feats:
                    continue
                comb = prev_m & m
                n = int(comb.sum())
                if n < min_leaf_depth:
                    continue
                sub = y_ret[comb]
                mu = float(sub.mean())
                if mu <= base_mean + 0.25:
                    continue
                sd = float(sub.std(ddof=1)) if n > 1 else 10.0
                t_stat = (mu - base_mean) / (sd / np.sqrt(n) + 1e-6)
                if t_stat > prev_sc * 0.95:
                    next_beam.append((float(t_stat), prev_conds + [{"feature": f, "op": op, "value": qv}], comb))
        next_beam.sort(key=lambda x: x[0], reverse=True)
        # 按特征集合去重
        dedup_next: list[tuple[float, list[dict[str, Any]], np.ndarray]] = []
        seen_sig: set[frozenset[str]] = set()
        for sc, conds, m in next_beam:
            sig = frozenset(c["feature"] for c in conds)
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            dedup_next.append((sc, conds, m))
            discovered.append((sc, conds))
            if len(dedup_next) >= 12:
                break
        cur_beam = dedup_next

    discovered.sort(key=lambda x: x[0], reverse=True)
    rules: list[RuleSpec] = []
    seen_sigs: set[frozenset[str]] = set()
    for idx, (_, conds) in enumerate(discovered, start=1):
        sig = frozenset(f"{c['feature']}{c['op']}" for c in conds)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        human_parts = [
            f"{MINING_FEATURE_META.get(c['feature'], {}).get('label', c['feature'])}{c['op']}{c['value']}"
            for c in conds
        ]
        rules.append(
            RuleSpec(
                rule_id=f"{template_id}_tree_{len(rules) + 1:02d}",
                name=f"树模型提纯规则 #{len(rules) + 1}",
                template_id=template_id,
                target_regimes=list(target_regimes),
                entry_conditions=conds,
                exit_policy={
                    "hold_days": int(tpl["default_hold_days"]),
                    "stop_loss_pct": float(tpl["default_stop_loss"]),
                    "take_profit_pct": float(tpl["default_take_profit"]),
                },
                rank_by=str(tpl["default_rank_by"]),
                source="tree_miner",
                thesis="IS 训练集自动提纯：" + " 且 ".join(human_parts),
            )
        )
        if len(rules) >= max_rules:
            break
    return rules


def _tune_exit_on_val(
    panel_df: pd.DataFrame,
    bench_df: pd.DataFrame,
    rule: RuleSpec,
    splits: dict[str, tuple[str, str]],
) -> RuleSpec:
    """仅在 Val 验证集上轻量择优出场参数（绝不触碰 OOS），防止出场参数拍脑袋。"""
    val_d0, val_d1 = splits["val"]
    mask = apply_entry_conditions(panel_df, rule.entry_conditions, target_regimes=rule.target_regimes)
    cand = panel_df.loc[mask & (panel_df["trade_date"] >= val_d0) & (panel_df["trade_date"] <= val_d1)].copy()
    if len(cand) < 20:
        return rule

    rank_col = rule.rank_by if rule.rank_by in cand.columns else "rs_20d_vs_hs300"
    cand.sort_values(["trade_date", rank_col], ascending=[True, bool(rule.rank_ascending)], inplace=True)
    cand = cand.groupby("trade_date", sort=False).head(max(1, int(rule.top_k_per_day))).copy()

    best_score = -1e9
    best_exit = dict(rule.exit_policy)
    for hd in (3, 5, 7):
        for sl in (-0.05, -0.07):
            for tp in (0.10, 0.15):
                rets, _ = simulate_trade_returns(cand, hold_days=hd, stop_loss_pct=sl, take_profit_pct=tp)
                if len(rets) < 10:
                    continue
                mu = float(rets.mean())
                sd = float(rets.std(ddof=1)) if len(rets) > 1 else 5.0
                sc = mu / (sd + 1.0)
                if sc > best_score:
                    best_score = sc
                    best_exit = {"hold_days": hd, "stop_loss_pct": sl, "take_profit_pct": tp}

    return RuleSpec(
        rule_id=rule.rule_id,
        name=rule.name,
        template_id=rule.template_id,
        target_regimes=list(rule.target_regimes),
        entry_conditions=[dict(c) for c in rule.entry_conditions],
        exit_policy=best_exit,
        top_k_per_day=rule.top_k_per_day,
        rank_by=rule.rank_by,
        rank_ascending=rule.rank_ascending,
        source=rule.source,
        thesis=rule.thesis,
    )


def run_mining_experiment(
    *,
    template_id: str = "mainwave_offense",
    start_date: str = "2023-01-01",
    end_date: Optional[str] = None,
    min_amt_20d: float = 50_000_000.0,
    custom_rules: Optional[Sequence[RuleSpec]] = None,
    enable_tree_miner: bool = True,
    tune_exit_on_val: bool = True,
    force_rebuild_cache: bool = False,
) -> dict[str, Any]:
    """一键运行完整策略挖掘闭环：特征加载 → 双引擎候选生成 → Val 出场择优 → OOS 盲测与 5 道门禁 → 落盘报告。"""
    tpl = MINING_TEMPLATES.get(template_id) or MINING_TEMPLATES["mainwave_offense"]
    now_bj = datetime.now(BJ_TZ)
    run_id = f"mine_{template_id}_{now_bj.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    panel_df, bench_df = build_or_load_feature_panel(
        start_date=start_date,
        end_date=end_date,
        min_amt_20d=min_amt_20d,
        force_rebuild=force_rebuild_cache,
    )
    target_regs = (
        list(custom_rules[0].target_regimes)
        if custom_rules and custom_rules[0].target_regimes
        else list(tpl["target_regimes"])
    )
    splits = split_trade_dates_for_regimes(bench_df, target_regimes=target_regs)

    candidates: list[RuleSpec] = []
    if custom_rules:
        candidates.extend(custom_rules)
    else:
        candidates.extend(build_template_hypotheses(template_id))

    if enable_tree_miner and template_id != "custom_hypothesis":
        tree_rules = mine_tree_rules_on_is(
            panel_df,
            template_id=template_id,
            target_regimes=tpl["target_regimes"],
            is_range=splits["is"],
            hold_days=int(tpl["default_hold_days"]),
            max_rules=6,
        )
        candidates.extend(tree_rules)

    evaluated: list[dict[str, Any]] = []
    dd_limit = float(tpl.get("max_drawdown_limit", 0.22)) * 100.0

    for cand in candidates:
        tuned = _tune_exit_on_val(panel_df, bench_df, cand, splits) if tune_exit_on_val else cand
        ev = evaluate_rule(panel_df, bench_df, tuned, splits=splits)
        gate = evaluate_mining_quality_gates(ev, max_drawdown_limit_pct=dd_limit)
        evaluated.append({**ev, "quality_gates": gate})

    # 按「全部通过门禁优先 → mining_score → OOS 期望收益」降序排列
    evaluated.sort(
        key=lambda x: (
            1 if x["quality_gates"]["all_passed"] else 0,
            x["quality_gates"]["mining_score"],
            x["segments"]["oos"]["avg_ret_pct"],
        ),
        reverse=True,
    )

    passed_list = [x for x in evaluated if x["quality_gates"]["all_passed"]]
    report = {
        "schema": "g3_mining_run/1",
        "run_id": run_id,
        "created_at": now_bj.isoformat(timespec="seconds"),
        "template_id": template_id,
        "template_name": tpl["name"],
        "universe": {
            "start_date": start_date,
            "end_date": str(bench_df["trade_date"].iloc[-1]) if not bench_df.empty else end_date,
            "min_amt_20d_wan": round(min_amt_20d / 1e4, 0),
            "panel_rows": int(len(panel_df)),
            "unique_stocks": int(panel_df["code"].nunique()) if not panel_df.empty else 0,
            "trade_days": int(bench_df["trade_date"].nunique()) if not bench_df.empty else 0,
        },
        "splits": {k: {"from": v[0], "to": v[1]} for k, v in splits.items()},
        "summary": {
            "candidates_evaluated": len(evaluated),
            "passed_all_gates": len(passed_list),
            "best_rule_id": evaluated[0]["rule"]["rule_id"] if evaluated else None,
            "best_mining_score": evaluated[0]["quality_gates"]["mining_score"] if evaluated else 0.0,
        },
        "results": evaluated,
    }

    out_path = _runs_dir() / f"{run_id}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["artifact_path"] = str(out_path)
    return report
