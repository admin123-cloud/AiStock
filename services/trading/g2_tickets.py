"""Domain transformations; no scheduler, notification or order side effects."""
from typing import Any,Dict
from services.trading.values import _sanitize,_to_float,_normalize_stock_code6

def _gen2_ticket_position_policy(open_state: str) -> Dict[str, Any]:
    state = str(open_state or "UNKNOWN").strip().upper()
    if state == "AGGRESSIVE":
        return {"can_open": True, "target_exposure": "60%-90%", "max_new_positions": 3, "single_position": "25%-35%"}
    if state == "NORMAL":
        return {"can_open": True, "target_exposure": "30%-60%", "max_new_positions": 2, "single_position": "20%-30%"}
    if state == "PROBE":
        return {"can_open": False, "target_exposure": "0%-20%", "max_new_positions": 0, "single_position": "0%"}
    if state == "OFF":
        return {"can_open": False, "target_exposure": "0%", "max_new_positions": 0, "single_position": "0%"}
    return {"can_open": False, "target_exposure": "0%", "max_new_positions": 0, "single_position": "0%"}


def _gen2_ticket_strategy_source(row: Dict[str, Any]) -> str:
    if row.get("pass_mainline1") and row.get("pass_mainline2"):
        return "volume5 + big_bull"
    if row.get("pass_mainline2"):
        return "big_bull"
    if row.get("pass_mainline1") or str(row.get("source_family") or "") == "volume5":
        return "volume5"
    return str(row.get("source_family") or row.get("signal_family") or "g2_v2_complete")


def _gen2_ticket_candidate(row: Dict[str, Any], index: int, policy: Dict[str, Any]) -> Dict[str, Any]:
    code = str(row.get("code") or row.get("code6") or "")
    name = str(row.get("name") or "")
    entry_price = _to_float(row.get("entry_price"))
    single_position = str(policy.get("single_position") or "0%")
    strategy_source = _gen2_ticket_strategy_source(row)
    buy_area = "等待盘中确认价"
    if entry_price is not None:
        buy_area = f"{entry_price:.2f} 附近，严禁明显高开/直线拉升后追价"
    return _sanitize(
        {
            "rank": int(index),
            "code": code,
            "code6": str(row.get("code6") or "") or _normalize_stock_code6(code),
            "name": name,
            "strategy_source": strategy_source,
            "stage_label": row.get("stage_label"),
            "confirm_datetime": row.get("confirm_datetime"),
            "entry_price": entry_price,
            "buy_area": buy_area,
            "suggested_position": single_position,
            "auto_order_allowed": False,
            "requires_manual_approval": True,
            "evidence": {
                "v4_rank": row.get("v4_rank"),
                "v4_score": row.get("v4_score"),
                "alpha191_volume5_score": row.get("alpha191_volume5_score"),
                "alpha191_volume5_rank_in_day": row.get("alpha191_volume5_rank_in_day"),
                "source_family": row.get("source_family"),
                "signal_family": row.get("signal_family"),
                "g2_v2_buy_logic": row.get("g2_v2_buy_logic"),
                "l3_rt_strong3_ratio": row.get("l3_rt_strong3_ratio"),
                "sector_score_bonus": row.get("sector_score_bonus"),
                "pass_mainline1": row.get("pass_mainline1"),
                "pass_mainline2": row.get("pass_mainline2"),
                "pass_official_v2_live": row.get("pass_official_v2_live"),
            },
            "buy_conditions": [
                "只在交易单允许开仓时执行",
                "必须属于 G2 V4 g2_v2_complete 正式候选",
                "盘中 15m/30m 确认信号不能撤销或过期",
                "不追直线拉升，不在明显高开透支后临时加价",
            ],
            "invalid_conditions": [
                "市场状态降为 PROBE/OFF",
                "候选从 G2 V4 正式可买池消失",
                "盘中跌破确认结构或买入理由失效",
                "出现 ST、停牌、涨停买不到或明显流动性异常",
            ],
            "risk_rules": [
                "单票浮亏 -4% 后禁止加仓",
                "单票浮亏 -6% 必须处理",
                "买入后 3 个交易日仍未按预期走强则降级复盘",
            ],
            "review_points": ["T+1 表现", "T+3 是否走强", "T+5 盈亏与买点质量归因"],
            "reason_text": row.get("reason_text"),
        }
    )


def _gen2_ticket_markdown(ticket: Dict[str, Any]) -> str:
    lines = [
        f"# G2 V4 Daily Trade Ticket - {ticket.get('signal_date') or ''}",
        "",
        f"- 生成时间：{ticket.get('generated_at') or ''}",
        f"- 策略口径：{ticket.get('strategy_code') or ''}",
        f"- 市场状态：{ticket.get('market_state') or 'UNKNOWN'}",
        f"- 今日是否允许开仓：{'是' if ticket.get('can_open') else '否'}",
        f"- 允许总仓位：{ticket.get('target_exposure') or '0%'}",
        f"- 正式候选数：{len(ticket.get('formal_candidates') or [])}",
        f"- 自动下单：关闭，必须人工确认",
        "",
        "## 今日纪律",
    ]
    for item in ticket.get("forbidden_actions") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## 正式候选"])
    candidates = ticket.get("formal_candidates") or []
    if not candidates:
        lines.append("- 今日无正式买入候选。")
    for item in candidates:
        evidence = item.get("evidence") or {}
        lines.extend(
            [
                "",
                f"### {item.get('rank')}. {item.get('code')} {item.get('name')}",
                f"- 来源：{item.get('strategy_source')}",
                f"- 买入区间：{item.get('buy_area')}",
                f"- 建议仓位：{item.get('suggested_position')}",
                f"- 确认时间：{item.get('confirm_datetime') or '-'}",
                f"- V4：rank={evidence.get('v4_rank')}, score={evidence.get('v4_score')}",
                f"- 主线证据：mainline1={evidence.get('pass_mainline1')}, mainline2={evidence.get('pass_mainline2')}, l3={evidence.get('l3_rt_strong3_ratio')}",
                f"- 理由：{item.get('reason_text') or '-'}",
                "- 失效条件：" + "；".join(str(x) for x in (item.get("invalid_conditions") or [])),
                "- 风控规则：" + "；".join(str(x) for x in (item.get("risk_rules") or [])),
            ]
        )
    lines.extend(["", "## 数据状态"])
    for stage in ticket.get("pipeline") or []:
        lines.append(f"- {stage.get('label')}: {stage.get('count')} | {stage.get('note')}")
    return "\n".join(lines).rstrip() + "\n"

