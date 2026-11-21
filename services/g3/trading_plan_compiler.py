# -*- coding: utf-8 -*-
"""G3 唯一权威「今日交易执行指令单」编译器。

将大盘五态环境、周期自适应策略路由、R51 水位控制、当前持仓逐股处置（该留还是该减多少股）
以及新开仓/绿灯储备池/红灯雷区统一编译为单一真值结构，彻底消除作战台多卡片口径割裂。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence


def _floor_lot(shares: float) -> int:
    if shares < 100:
        return 0
    return int(math.floor(shares / 100.0) * 100)


def compile_daily_trading_command(
    *,
    market_gate: Mapping[str, Any],
    strategy_router: Optional[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]],
    accounts_summary: Mapping[str, Any],
    mainwave_ai_summary: Optional[Mapping[str, Any]] = None,
    mainwave_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """编译唯一权威的「今日交易执行指令单」。"""
    regime = str(market_gate.get("regime") or "defensive")
    regime_label = str(market_gate.get("regime_label") or "防守模式")
    allow_new = bool(market_gate.get("allow_new_mainwave", False))
    cap_pct = float(market_gate.get("target_position_cap_pct") or 25.0)
    wl = market_gate.get("r51_waterline") if isinstance(market_gate.get("r51_waterline"), Mapping) else {}
    actual_pct = wl.get("actual_pct")
    gap_pct = float(wl.get("gap_pct") or 0.0)
    max_step_pct = float(wl.get("max_step_pct") or 15.0)
    is_defensive = regime in ("defensive", "extreme_panic") or not allow_new

    sr = dict(strategy_router or {})
    router_mode = str(sr.get("mode") or ("cash_defense" if is_defensive else "active_strategy"))
    active_strategy_name = str(
        sr.get("active_strategy_name") or ("现金防守 / 严禁开新仓" if is_defensive else "周期生效策略")
    )

    # 1. 允许 / 禁止交易模式
    if is_defensive:
        allowed_modes = [
            "R51 水位减仓单（base_rebalance，优先降超配水位）",
            "已有持仓反T降本（t_trade，严格先卖后买，接不回顺势减仓）",
        ]
        forbidden_modes = [
            "严禁开新主升仓",
            "严禁正T低吸（先买后卖）",
            "严禁逆势抄底补仓",
        ]
    elif regime == "balanced":
        allowed_modes = [
            f"周期轮动补涨买单（{active_strategy_name}，受 R51 水位上限 ≤{cap_pct:.0f}% 约束）",
            "已有持仓双向做T（t_trade，优先反T + 低位支撑正T）",
        ]
        forbidden_modes = [
            "严禁追高涨幅已大的强势股",
            "严禁买 AI 排雷红灯标的",
        ]
    else:
        allowed_modes = [
            f"主升进攻龙头买单（{active_strategy_name}，受水位上限 ≤{cap_pct:.0f}% 约束）",
            "已有持仓顺势做T（t_trade，均线支撑低吸高抛）",
        ]
        forbidden_modes = [
            "严禁买 AI 排雷红灯标的",
            "严禁无止损重仓单押",
        ]

    # 2. 现有持仓逐股硬核处置计划（精确算出“该不该留、建议减多少股、原因”）
    total_assets = float(accounts_summary.get("total_assets") or 0.0)
    # 若超配，今日目标减仓金额 = 总资产 * min(超配比例, 单日步长 15%)
    need_reduce_pct = max(0.0, min(gap_pct, max_step_pct)) if (actual_pct is not None and gap_pct > 1.0) else 0.0
    remaining_reduce_yuan = total_assets * (need_reduce_pct / 100.0) if total_assets > 0 else 0.0

    holding_cards = [c for c in cards if c.get("in_holdings") and int(c.get("total_shares") or 0) > 0]

    # 按风险严重度排序：先处置（破止损 / 下降周期 / AI红黄灯 / 浮亏重）的票
    def _risk_priority(c: Mapping[str, Any]) -> tuple[int, float]:
        sen = c.get("ai_sentinel") or {}
        light = str(sen.get("risk_light") or "unknown")
        reg = str(c.get("regime") or "unknown")
        px = float((c.get("live_quote") or {}).get("price") or (c.get("holdings") or [{}])[0].get("current_price") or 0)
        stop = (c.get("effective_params") or {}).get("stop")
        broken_stop = bool(stop and px > 0 and px < float(stop))
        score = 0
        if light == "red":
            score += 100
        if broken_stop:
            score += 60
        if reg == "down":
            score += 40
        if light == "yellow":
            score += 25
        mv = sum(float(h.get("market_value") or 0) for h in (c.get("holdings") or []))
        return (-score, -mv)

    holding_cards.sort(key=_risk_priority)

    holding_actions: list[dict[str, Any]] = []
    for c in holding_cards:
        code = str(c.get("code") or "")
        name = str(c.get("name") or code)
        total_shares = int(c.get("total_shares") or 0)
        avail_shares = int(c.get("available_shares") or 0)
        h_list = c.get("holdings") or []
        h0 = h_list[0] if h_list else {}
        cost_px = h0.get("cost_price")
        px = float((c.get("live_quote") or {}).get("price") or h0.get("current_price") or 0.0)
        mv = sum(float(h.get("market_value") or 0) for h in h_list) or (px * total_shares)
        pnl_pct = h0.get("profit_pct")
        reg = str(c.get("regime") or "unknown")
        stop_px = (c.get("effective_params") or {}).get("stop")
        broken_stop = bool(stop_px and px > 0 and px < float(stop_px))

        sen = c.get("ai_sentinel") or {}
        ai_light = str(sen.get("risk_light") or "unknown")
        ai_cat = sen.get("catalyst_score")
        ai_flags = list(sen.get("risk_flags") or [])
        ai_thesis = str(sen.get("one_line_thesis") or "")

        reasons: list[str] = []
        if ai_light == "red":
            reasons.append("AI排雷红灯（重大基本面/减持风险）")
        if broken_stop:
            reasons.append(f"现价 {px:.2f} 已跌破硬止损位 {float(stop_px):.2f}")
        if reg == "down":
            reasons.append("个股处于下降周期（严禁正T低吸）")
        if ai_light == "yellow":
            reasons.append("AI哨兵黄灯（" + (ai_flags[0] if ai_flags else "存在减持或业绩压力") + "）")
        if need_reduce_pct > 0:
            reasons.append(f"双账号水位 {actual_pct:.1f}% 超配 {gap_pct:.1f}%，须执行 R51 分步减仓")

        suggested_reduce_shares = 0
        verdict = "HOLD_T"
        verdict_label = "可持有做T"
        verdict_tone = "emerald"
        order_type = "t_trade"

        if total_shares < 300:
            # 零股持仓（如 100 股）
            if ai_light == "red" or (is_defensive and reg == "down" and avail_shares >= 100):
                verdict = "SHOULD_EXIT"
                verdict_label = "建议择机清仓"
                verdict_tone = "red"
                suggested_reduce_shares = _floor_lot(avail_shares)
                order_type = "base_rebalance"
            else:
                verdict = "HOLD_ODD_LOT"
                verdict_label = "零股暂观察（不动）"
                verdict_tone = "zinc"
                if not reasons:
                    reasons.append("不足 300 股不可做T，基本面无雷暂观察")
        elif ai_light == "red" or broken_stop or (is_defensive and (reg == "down" or remaining_reduce_yuan > 0)):
            order_type = "base_rebalance"
            if ai_light == "red":
                verdict = "SHOULD_EXIT"
                verdict_label = "红灯建议清退"
                verdict_tone = "red"
                suggested_reduce_shares = _floor_lot(avail_shares)
            else:
                verdict = "MUST_REDUCE"
                verdict_label = "应分步减仓（降水位/止损）"
                verdict_tone = "red" if (broken_stop or reg == "down") else "amber"
                # 按剩余需减仓金额计算整百股数（至少减 1 份 T 仓或 20% 可用股，且不超过可用股）
                if remaining_reduce_yuan > 0 and px > 0:
                    target_sh = _floor_lot(min(avail_shares, remaining_reduce_yuan / px))
                    # 防止单只一次性卖空过多，单日按不超过可用仓位的 35%（但至少 200 股若可用充足）
                    step_cap = _floor_lot(max(200, avail_shares * 0.35)) if avail_shares >= 300 else _floor_lot(avail_shares)
                    suggested_reduce_shares = min(avail_shares, max(100, min(target_sh, step_cap))) if avail_shares >= 100 else 0
                    remaining_reduce_yuan = max(0.0, remaining_reduce_yuan - suggested_reduce_shares * px)
                elif avail_shares >= 100:
                    suggested_reduce_shares = _floor_lot(max(100, avail_shares * 0.25))
        elif is_defensive:
            verdict = "REVERSE_T_ONLY"
            verdict_label = "防守期只做反T"
            verdict_tone = "amber"
            suggested_reduce_shares = _floor_lot(min(avail_shares, total_shares / 3.0))
            order_type = "t_trade"

        holding_actions.append(
            {
                "code": code,
                "name": name,
                "total_shares": total_shares,
                "available_shares": avail_shares,
                "cost_price": cost_px,
                "current_price": round(px, 2) if px > 0 else None,
                "market_value": round(mv, 0),
                "profit_pct": pnl_pct,
                "regime": reg,
                "stop_price": stop_px,
                "broken_stop": broken_stop,
                "ai_risk_light": ai_light,
                "ai_catalyst_score": ai_cat,
                "ai_thesis": ai_thesis,
                "verdict": verdict,
                "verdict_label": verdict_label,
                "verdict_tone": verdict_tone,
                "suggested_side": "SELL" if suggested_reduce_shares > 0 else "HOLD",
                "suggested_order_type": order_type,
                "suggested_shares": suggested_reduce_shares,
                "reasons": reasons,
            }
        )

    # 3. 新开仓与候选储备池（含评分与红灯排雷）
    ai_sum = dict(mainwave_ai_summary or {})
    reserve_top_picks = list(ai_sum.get("top_picks") or [])[:5]
    red_blocked: list[dict[str, Any]] = []
    for cand in mainwave_candidates or []:
        ai = cand.get("ai_screening") or {}
        if ai.get("risk_light") == "red":
            red_blocked.append(
                {
                    "code": str(cand.get("code") or "").split(".")[0],
                    "name": str(cand.get("name") or ""),
                    "industry": str(cand.get("industry") or ""),
                    "score": cand.get("score"),
                    "risk_flags": ai.get("risk_flags") or [],
                    "one_line_thesis": ai.get("one_line_thesis") or "",
                }
            )

    return {
        "schema": "g3_daily_trading_command/1",
        "environment": {
            "regime": regime,
            "regime_label": regime_label,
            "is_defensive": is_defensive,
            "actual_position_pct": actual_pct,
            "target_cap_pct": cap_pct,
            "overweight_pct": round(max(0.0, gap_pct), 1),
            "today_reduce_step_pct": round(need_reduce_pct, 1),
            "today_reduce_target_yuan": round(total_assets * (need_reduce_pct / 100.0), 0) if total_assets > 0 else 0,
        },
        "strategy": {
            "mode": router_mode,
            "active_strategy_id": sr.get("active_strategy_id") or "cash_defense",
            "active_strategy_name": active_strategy_name,
            "allowed_modes": allowed_modes,
            "forbidden_modes": forbidden_modes,
            "summary": str(market_gate.get("action_summary") or ""),
        },
        "holding_actions": holding_actions,
        "new_entries": {
            "can_open_new": (not is_defensive) and router_mode == "active_strategy",
            "active_picks": list(sr.get("picks") or []),
            "reserve_counts": ai_sum.get("counts") or {},
            "reserve_top_picks": reserve_top_picks,
            "red_blocked": red_blocked,
        },
    }
