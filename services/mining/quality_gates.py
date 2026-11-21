# -*- coding: utf-8 -*-
"""AI 策略挖掘工坊 —— 五道防过拟合硬门禁与失败归因引擎。"""
from __future__ import annotations

from typing import Any, Mapping


def evaluate_mining_quality_gates(
    eval_result: Mapping[str, Any],
    *,
    max_drawdown_limit_pct: float = 22.0,
    min_oos_trades: int = 30,
    min_oos_avg_ret_pct: float = 0.25,
    min_decay_ratio: float = 0.45,
) -> dict[str, Any]:
    """对单条候选策略执行五道防过拟合硬门禁打分，并生成失败归因诊断。"""
    rule = eval_result.get("rule") or {}
    segs = eval_result.get("segments") or {}
    is_m = segs.get("is") or {}
    val_m = segs.get("val") or {}
    oos_m = segs.get("oos") or {}

    conds = rule.get("entry_conditions") or []

    # Gate 1: 三段结构完整且验证集未崩塌
    g1_ok = (
        int(is_m.get("trades_count") or 0) >= 60
        and int(val_m.get("trades_count") or 0) >= 20
        and float(val_m.get("avg_ret_pct") or 0.0) > 0.0
    )
    g1_detail = (
        f"IS={is_m.get('trades_count', 0)}笔(均{is_m.get('avg_ret_pct', 0):+.2f}%) · "
        f"Val={val_m.get('trades_count', 0)}笔(均{val_m.get('avg_ret_pct', 0):+.2f}%)"
    )

    # Gate 2: A 股真实摩擦（已内建扣 0.26% 往返成本 + 涨跌停不可成交过滤），校验扣费后 OOS 期望为正
    oos_avg = float(oos_m.get("avg_ret_pct") or 0.0)
    g2_ok = oos_avg >= float(min_oos_avg_ret_pct)
    g2_detail = f"OOS 扣费后单笔期望 {oos_avg:+.3f}%（门槛 ≥ +{min_oos_avg_ret_pct:.2f}%）"

    # Gate 3: 样本充分性与分布稳健（OOS 笔数 ≥ 30 且剔除最大盈利月仍为正）
    oos_n = int(oos_m.get("trades_count") or 0)
    ex_best = float(oos_m.get("ex_best_month_cum_ret_pct") or 0.0)
    g3_ok = (oos_n >= int(min_oos_trades)) and (ex_best > 0.0)
    g3_detail = f"OOS 独立交易 {oos_n} 笔(≥{min_oos_trades}) · 剔除最佳单月累计 {ex_best:+.2f}%(>0)"

    # Gate 4: 风险收益与防过拟合衰减比
    is_avg = float(is_m.get("avg_ret_pct") or 0.0)
    decay_ratio = (oos_avg / is_avg) if is_avg > 1e-6 else (1.0 if oos_avg > 0 else 0.0)
    oos_dd = float(oos_m.get("max_drawdown_pct") or 0.0)
    oos_sharpe = float(oos_m.get("sharpe") or 0.0)
    oos_excess = float(oos_m.get("excess_ret_pct") or 0.0)
    g4_ok = (
        oos_dd <= float(max_drawdown_limit_pct)
        and (oos_sharpe >= 0.8 or oos_excess > 0.0)
        and decay_ratio >= float(min_decay_ratio)
    )
    g4_detail = (
        f"OOS回撤 {oos_dd:.1f}%(≤{max_drawdown_limit_pct:.0f}%) · "
        f"夏普 {oos_sharpe:.2f} · 超额 {oos_excess:+.2f}% · 衰减比 {decay_ratio:.2f}(≥{min_decay_ratio:.2f})"
    )

    # Gate 5: 规则简洁可解释（1~5 条明文条件，防止过度堆砌参数）
    g5_ok = 1 <= len(conds) <= 5
    g5_detail = f"明文条件 {len(conds)} 条（允许 1~5 条，可直译为标准策略合约）"

    gates = [
        {"gate": "G1_three_way_split", "label": "三段切分与验证集一致性", "passed": g1_ok, "detail": g1_detail},
        {"gate": "G2_friction_expectancy", "label": "扣费后样本外正期望", "passed": g2_ok, "detail": g2_detail},
        {"gate": "G3_sample_robustness", "label": "样本充分性与单月稳健", "passed": g3_ok, "detail": g3_detail},
        {"gate": "G4_risk_and_decay", "label": "回撤/夏普/防过拟合衰减", "passed": g4_ok, "detail": g4_detail},
        {"gate": "G5_interpretability", "label": "明文规则简洁可解释", "passed": g5_ok, "detail": g5_detail},
    ]

    passed_count = sum(1 for g in gates if g["passed"])
    all_passed = passed_count == len(gates)

    # 失败归因（供 AI 下一轮自动迭代优化使用）
    failure_reasons: list[str] = []
    if not g1_ok:
        failure_reasons.append("验证集收益转负或交易过少，规则可能仅适配早期行情")
    if not g2_ok:
        failure_reasons.append(f"OOS 扣费后期望偏低({oos_avg:+.2f}%)，需提升入场筛选强度或优化止盈止损")
    if not g3_ok:
        if oos_n < min_oos_trades:
            failure_reasons.append(f"OOS 样本不足({oos_n}<{min_oos_trades})，条件过严，建议放宽阈值")
        if ex_best <= 0:
            failure_reasons.append(f"剔除最大盈利月后 OOS 收益转负({ex_best:+.2f}%)，收益过度集中于单月")
    if not g4_ok:
        if decay_ratio < min_decay_ratio:
            failure_reasons.append(f"存在明显过拟合衰减(OOS/IS={decay_ratio:.2f})")
        if oos_dd > max_drawdown_limit_pct:
            failure_reasons.append(f"OOS 最大回撤超标({oos_dd:.1f}%>{max_drawdown_limit_pct:.0f}%)")
    if not g5_ok:
        failure_reasons.append("规则条件过多，存在过拟合复杂度风险")

    # 综合评分（0~100）：以 OOS 表现为核心，叠加门禁通过数
    score = 0.0
    score += passed_count * 12.0  # 最多 60 分
    score += max(-15.0, min(20.0, oos_avg * 10.0))  # 期望收益贡献
    score += max(-10.0, min(15.0, oos_sharpe * 6.0))  # 夏普贡献
    score += 5.0 if oos_excess > 0 else 0.0
    mining_score = round(max(0.0, min(100.0, score)), 1)

    return {
        "all_passed": all_passed,
        "passed_count": passed_count,
        "total_gates": len(gates),
        "mining_score": mining_score,
        "decay_ratio": round(decay_ratio, 3),
        "gates": gates,
        "failure_reasons": failure_reasons,
    }
