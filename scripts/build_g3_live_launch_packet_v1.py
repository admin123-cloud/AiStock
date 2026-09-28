from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, runtime_path  # noqa: E402


READINESS_DIR = report_path("g3_realtime_readiness_review_v1")
SMOKE_DIR = report_path("g3_live_launch_smoke_v1")
OUT_DIR = report_path("g3_live_launch_packet_v1")
PLAYBOOK_REVIEWS_PATH = runtime_path("gen3_state_alpha", "launch_day_playbook_reviews.json")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()
    return df.astype(object).where(pd.notna(df), None)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok"}
    return False


def _text(value: Any, default: str = "--") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return default if not text or text.lower() in {"nan", "nat", "none"} else text


def _stable_key(parts: list[Any]) -> str:
    return "|".join(_text(part, "") for part in parts).strip("|")


def _load_playbook_reviews() -> dict[str, dict[str, Any]]:
    data = _read_json(PLAYBOOK_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _attach_playbook_reviews(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviews = _load_playbook_reviews()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = _stable_key(
            [
                row.get("window"),
                row.get("checkpoint_time"),
                row.get("review_axis"),
                row.get("object"),
                row.get("action_type"),
            ]
        )
        review = reviews.get(key, {}) if key else {}
        item = {
            **row,
            "review_key": key,
            "review_status": review.get("review_status", "pending_review"),
            "review_result": review.get("review_result", ""),
            "review_result_label": review.get("review_result_label", ""),
            "review_note": review.get("review_note", ""),
            "review_updated_at": review.get("updated_at", ""),
        }
        out.append(item)
    return out


def _learning_status_from_review(status: Any) -> str:
    value = _text(status, "").lower()
    if value in {"issue_found", "blocked"}:
        return "needs_review"
    if value in {"data_gap", "continue_watch"}:
        return "watch_more"
    if value == "validated":
        return "validated_rule"
    return "pending"


def _learning_axis_label(axis: Any) -> str:
    return {
        "execution_evidence": "执行证据",
        "buy_point": "买点",
        "sell_point": "卖点/退出",
        "selection": "选股模式",
        "model_switch": "策略切换",
        "natural_trade_consistency": "自然交易一致性",
    }.get(_text(axis, ""), _text(axis, "未分类"))


def _build_launch_day_learning_queue(playbook_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for row in playbook_rows:
        review_status = _text(row.get("review_status"), "")
        learning_status = _learning_status_from_review(review_status)
        if learning_status not in {"needs_review", "watch_more"}:
            continue
        axis = _text(row.get("review_axis"), "unknown")
        queue.append(
            {
                "priority": len(queue) + 1,
                "learning_status": learning_status,
                "review_status": review_status,
                "review_result": _text(row.get("review_result"), ""),
                "review_result_label": _text(row.get("review_result_label"), ""),
                "review_axis": axis,
                "axis_label": _learning_axis_label(axis),
                "object": row.get("object"),
                "action_type": row.get("action_type"),
                "evidence": row.get("review_note") or row.get("evidence_to_collect"),
                "hidden_risk": row.get("fail_condition"),
                "optimization_direction": row.get("required_action") or row.get("pass_condition"),
                "natural_trade_boundary": row.get("natural_trade_boundary"),
                "review_key": row.get("review_key"),
                "source": "launch_day_playbook",
                "updated_at": row.get("review_updated_at"),
            }
        )
    return queue


def _records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    if limit is not None:
        out = out.head(limit)
    return json.loads(out.to_json(orient="records", force_ascii=False))


def _md_table(rows: list[dict[str, Any]], columns: list[str], limit: int = 12) -> list[str]:
    if not rows:
        return ["无。"]
    clipped = rows[:limit]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in clipped:
        values = []
        for col in columns:
            value = _text(row.get(col), "")
            value = value.replace("\n", " ").replace("|", "/")
            values.append(value[:180])
        lines.append("| " + " | ".join(values) + " |")
    if len(rows) > limit:
        lines.append(f"\n还有 {len(rows) - limit} 行，详见 CSV。")
    return lines


def _build_launch_steps(action_sequence: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in _records(action_sequence):
        action_group = _text(row.get("action_group"), "")
        rows.append(
            {
                "step": row.get("step"),
                "action_group": action_group,
                "action_label": row.get("action_label"),
                "can_execute_now": _truthy(row.get("can_execute_now")),
                "requires_manual_confirmation": _truthy(row.get("requires_manual_confirmation")),
                "recommended_ui_action": row.get("recommended_ui_action"),
                "recommended_api_action": row.get("recommended_api_action"),
                "expected_effect": row.get("expected_effect"),
                "completion_check": row.get("completion_check"),
                "stop_if_fail": row.get("stop_if_fail"),
                "natural_trade_boundary": row.get("natural_trade_boundary"),
                "object": row.get("object"),
                "evidence_statuses": row.get("evidence_statuses"),
            }
        )
    return rows


def _issue_axis_counts(daily_review: pd.DataFrame) -> dict[str, int]:
    if daily_review.empty or "review_axis" not in daily_review.columns:
        return {}
    pending = daily_review.copy()
    if "review_status" in pending.columns:
        pending = pending[
            pending["review_status"]
            .astype(str)
            .str.lower()
            .isin(["pending_review", "pending", "pending_evidence", "pending_observation", "", "none", "nan"])
        ]
    return {str(k): int(v) for k, v in pending["review_axis"].fillna("unknown").value_counts().to_dict().items()}


def _first_text(df: pd.DataFrame, column: str) -> str:
    if df.empty or column not in df.columns:
        return ""
    for value in df[column].tolist():
        text = _text(value, "")
        if text:
            return text
    return ""


def _build_review_axis_matrix(daily_checklist: pd.DataFrame) -> list[dict[str, Any]]:
    labels = {
        "execution_evidence": "执行证据",
        "buy_point": "买点",
        "sell_point": "卖点/退出",
        "selection": "选股模式",
        "model_switch": "策略切换",
        "natural_trade_consistency": "自然交易一致性",
        "after_close_attribution": "盘后归因",
    }
    order = {
        "execution_evidence": 0,
        "buy_point": 1,
        "sell_point": 2,
        "selection": 3,
        "model_switch": 4,
        "natural_trade_consistency": 5,
        "after_close_attribution": 6,
    }
    defaults = {
        "execution_evidence": {
            "pass_condition": "真实持仓/价格/审计证据补齐，阻断消失或明确降级为观察。",
            "fail_condition": "同步失败、证据无法落账、审计仍阻断，禁止真实买入。",
            "launch_decision": "先清执行证据，再讨论策略。",
        },
        "buy_point": {
            "pass_condition": "买点符合 30m 确认、热度边界和不追高要求。",
            "fail_condition": "纸面观察显示过早、过急、追高或确认不足。",
            "launch_decision": "只复盘，不为补仓放宽买点。",
        },
        "sell_point": {
            "pass_condition": "卖点按刷新价格、硬止损、先减半、前低保护执行。",
            "fail_condition": "退出价格过期、止损止盈冲突或持仓风险污染新开仓。",
            "launch_decision": "先处理退出合同，再评估新开仓。",
        },
        "selection": {
            "pass_condition": "被挡候选有清晰合同理由，后续走势未证明核心规则误杀。",
            "fail_condition": "被挡候选明显优于正式候选且原阻断理由不足。",
            "launch_decision": "候选进入遗漏观察，不直接替代正式票。",
        },
        "model_switch": {
            "pass_condition": "无票、补位、修复和主升切换符合市场状态与角色边界。",
            "fail_condition": "自然空仓被误判、补位替代主路由或切换迟钝。",
            "launch_decision": "先归因是否自然空仓，再决定是否调切换。",
        },
        "natural_trade_consistency": {
            "pass_condition": "结论来自合同、证据和自然交易边界。",
            "fail_condition": "用单日收益倒推放宽规则。",
            "launch_decision": "保持自然交易纪律。",
        },
    }
    base_axes = ["execution_evidence", "buy_point", "sell_point", "selection", "model_switch", "natural_trade_consistency"]
    if daily_checklist.empty or "review_axis" not in daily_checklist.columns:
        axes = base_axes
        return [
            {
                "review_axis": axis,
                "axis_label": labels.get(axis, axis),
                "pending_count": 0,
                "high_risk_count": 0,
                "issue_found_count": 0,
                "formal_required_count": 0,
                "first_object": "",
                "next_action": "",
                **defaults.get(axis, {}),
            }
            for axis in axes
        ]

    rows: list[dict[str, Any]] = []
    found_axes = [str(x) for x in daily_checklist["review_axis"].dropna().unique().tolist()]
    axes = list(dict.fromkeys([*base_axes, *found_axes]))
    for axis in sorted(axes, key=lambda x: order.get(x, 99)):
        subset = daily_checklist[daily_checklist["review_axis"].astype(str) == axis].copy()
        status = subset.get("review_status", pd.Series(dtype=str)).astype(str).str.lower()
        risk = subset.get("risk_level", pd.Series(dtype=str)).astype(str).str.lower()
        formal = subset.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy)
        pending = status.isin(["pending_review", "pending", "pending_evidence", "pending_observation", "", "none", "nan"])
        issue = status.isin(["issue_found", "blocked"])
        default = defaults.get(axis, {})
        rows.append(
            {
                "review_axis": axis,
                "axis_label": labels.get(axis, axis),
                "pending_count": int(pending.sum()),
                "high_risk_count": int(risk.isin(["high", "block", "danger"]).sum()),
                "issue_found_count": int(issue.sum()),
                "formal_required_count": int(formal.sum()),
                "first_object": _first_text(subset, "object"),
                "next_action": _first_text(subset, "target_action") or _first_text(subset, "evidence_to_collect"),
                "pass_condition": _first_text(subset, "pass_condition") or default.get("pass_condition", ""),
                "fail_condition": _first_text(subset, "fail_condition") or default.get("fail_condition", ""),
                "natural_trade_boundary": _first_text(subset, "natural_trade_boundary"),
                "launch_decision": default.get("launch_decision", "按该轴逐项复盘，问题样本进入学习队列。"),
            }
        )
    return rows


def _build_launch_day_playbook(
    *,
    summary: dict[str, Any],
    command: dict[str, Any],
    launch_steps: list[dict[str, Any]],
    review_axis_matrix: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(
        *,
        priority: str,
        window: str,
        checkpoint_time: str,
        action_type: str,
        review_axis: str,
        object_name: Any,
        required_action: Any,
        evidence_to_collect: Any,
        pass_condition: Any,
        fail_condition: Any,
        natural_trade_boundary: Any,
        launch_permission_effect: str,
        source: str,
    ) -> None:
        rows.append(
            {
                "priority": priority,
                "window": window,
                "checkpoint_time": checkpoint_time,
                "action_type": action_type,
                "review_axis": review_axis,
                "object": object_name,
                "required_action": required_action,
                "evidence_to_collect": evidence_to_collect,
                "pass_condition": pass_condition,
                "fail_condition": fail_condition,
                "natural_trade_boundary": natural_trade_boundary,
                "launch_permission_effect": launch_permission_effect,
                "source": source,
            }
        )

    first_step = launch_steps[0] if launch_steps else {}
    add(
        priority="P0",
        window="盘前",
        checkpoint_time="09:00-09:15",
        action_type="清理首个阻断",
        review_axis="execution_evidence",
        object_name=first_step.get("object") or (command.get("current_action") or {}).get("object") or command.get("action_group"),
        required_action=first_step.get("action_label") or command.get("action_label") or command.get("next_action"),
        evidence_to_collect=first_step.get("expected_effect") or "记录真实持仓、价格、处理结果与重跑审计状态。",
        pass_condition=first_step.get("completion_check") or "重跑审计后阻断下降或消失。",
        fail_condition=first_step.get("stop_if_fail") or "证据不可信或同步失败时禁止真实买入。",
        natural_trade_boundary=first_step.get("natural_trade_boundary") or "先清执行证据，再判断交易逻辑。",
        launch_permission_effect="未完成前禁止真实买入",
        source="launch_steps",
    )
    add(
        priority="P0",
        window="盘前",
        checkpoint_time="09:15-09:25",
        action_type="重跑准入审计",
        review_axis="execution_evidence",
        object_name="G3 实战准入",
        required_action="重跑 readiness 审计并重建启动包",
        evidence_to_collect="记录 live_admission_status、blocking_command_count、next_action 与启动包状态。",
        pass_condition="live_admission_blocking_command_count=0，且状态进入人工复核；若无票则进入自然空仓复盘。",
        fail_condition="仍有阻断时回到新的第一阻断动作；不跳过 gate。",
        natural_trade_boundary="不因临近开盘或收益压力绕过准入。",
        launch_permission_effect="决定是否进入人工最终复核",
        source="readiness_review",
    )
    if not _truthy(summary.get("live_admission_buy_allowed")):
        add(
            priority="P1",
            window="盘前",
            checkpoint_time="09:25 前",
            action_type="记录不买入原因",
            review_axis="model_switch",
            object_name=summary.get("next_trade_entry_date"),
            required_action=summary.get("live_premarket_next_action") or "记录无票/阻断归因",
            evidence_to_collect="记录无票、候选遗漏、执行证据、模型切换是否自然。",
            pass_condition="能区分自然空仓、候选误杀、数据缺口和流程缺口。",
            fail_condition="为了补满仓位而放宽买点、选股或切换规则。",
            natural_trade_boundary="无票日允许自然空仓，不补票。",
            launch_permission_effect="保持禁止真实买入，进入观察复盘",
            source="launch_summary",
        )

    for axis in review_axis_matrix:
        pending = int(axis.get("pending_count") or 0)
        high = int(axis.get("high_risk_count") or 0)
        formal = int(axis.get("formal_required_count") or 0)
        if pending <= 0 and high <= 0 and formal <= 0:
            continue
        add(
            priority="P1" if axis.get("review_axis") == "execution_evidence" else "P2",
            window="盘中/盘后",
            checkpoint_time="盘中观察，盘后归因",
            action_type=f"{axis.get('axis_label') or axis.get('review_axis')}复盘",
            review_axis=axis.get("review_axis"),
            object_name=axis.get("first_object") or axis.get("axis_label"),
            required_action=axis.get("next_action") or axis.get("launch_decision"),
            evidence_to_collect=axis.get("next_action") or "记录事实样本、触发规则、结果和是否符合合同。",
            pass_condition=axis.get("pass_condition"),
            fail_condition=axis.get("fail_condition"),
            natural_trade_boundary=axis.get("natural_trade_boundary") or "不按单日收益倒推放宽规则。",
            launch_permission_effect="进入学习队列或继续观察，不直接改合同",
            source="review_axis_matrix",
        )
    return _attach_playbook_reviews(rows)


def build_packet() -> dict[str, Any]:
    summary = _read_json(READINESS_DIR / "summary.json")
    smoke = _read_json(SMOKE_DIR / "summary.json")
    command = smoke.get("premarket_command") if isinstance(smoke.get("premarket_command"), dict) else {}

    action_sequence = _read_csv(READINESS_DIR / "live_premarket_action_sequence.csv")
    command_sheet = _read_csv(READINESS_DIR / "live_premarket_command_sheet.csv")
    blocker_plan = _read_csv(READINESS_DIR / "live_blocker_resolution_plan.csv")
    manual_acceptance = _read_csv(READINESS_DIR / "live_manual_launch_acceptance.csv")
    daily_board = _read_csv(READINESS_DIR / "daily_live_review_board.csv")
    hidden_risk = _read_csv(READINESS_DIR / "live_hidden_risk_watchlist.csv")
    candidate_omission = _read_csv(READINESS_DIR / "candidate_omission_checklist.csv")
    no_trade = _read_csv(READINESS_DIR / "no_trade_day_review.csv")
    strategy_backlog = _read_csv(READINESS_DIR / "strategy_learning_backlog.csv")
    day1_journal = _read_csv(READINESS_DIR / "live_day1_review_journal.csv")
    daily_checklist = _read_csv(READINESS_DIR / "live_daily_review_execution_checklist.csv")

    hidden_high = hidden_risk
    if not hidden_high.empty and "risk_level" in hidden_high.columns:
        hidden_high = hidden_high[hidden_high["risk_level"].astype(str).str.lower().isin(["high", "block", "danger"])]

    formal_blockers = command_sheet
    if not formal_blockers.empty and "formal_trade_required" in formal_blockers.columns:
        formal_blockers = formal_blockers[formal_blockers["formal_trade_required"].map(_truthy)]

    launch_steps = _build_launch_steps(action_sequence)
    review_axis_matrix = _build_review_axis_matrix(daily_checklist)
    launch_day_playbook = _build_launch_day_playbook(
        summary=summary,
        command=command,
        launch_steps=launch_steps,
        review_axis_matrix=review_axis_matrix,
    )
    launch_day_learning_queue = _build_launch_day_learning_queue(launch_day_playbook)
    learning_status_counts = pd.Series(
        [row.get("learning_status") for row in launch_day_learning_queue],
        dtype=object,
    ).value_counts().to_dict()
    status = "blocked" if not _truthy(summary.get("live_admission_buy_allowed")) else "manual_review_ready"
    packet = {
        "version": "g3_live_launch_packet_v1",
        "generated_at": _now_text(),
        "status": status,
        "summary": {
            "readiness_generated_at": summary.get("generated_at"),
            "smoke_generated_at": smoke.get("generated_at"),
            "smoke_status": smoke.get("status"),
            "next_trade_entry_date": summary.get("next_trade_entry_date"),
            "next_trade_ticket_count": summary.get("next_trade_ticket_count"),
            "live_admission_status": summary.get("live_admission_status"),
            "live_admission_buy_allowed": summary.get("live_admission_buy_allowed"),
            "live_admission_blocking_command_count": summary.get("live_admission_blocking_command_count"),
            "live_premarket_next_action": summary.get("live_premarket_next_action"),
            "formal_launch_ready": summary.get("formal_launch_ready"),
            "live_manual_launch_ready": summary.get("live_manual_launch_ready"),
            "strategy_learning_backlog_count": summary.get("strategy_learning_backlog_count"),
            "launch_day_learning_queue_count": len(launch_day_learning_queue),
            "launch_day_learning_needs_review_count": int(learning_status_counts.get("needs_review", 0)),
            "launch_day_learning_watch_more_count": int(learning_status_counts.get("watch_more", 0)),
            "candidate_omission_pending_count": summary.get("candidate_omission_pending_count"),
            "no_trade_day_pending_review_count": summary.get("no_trade_day_pending_review_count"),
            "daily_live_review_board_pending_count": summary.get("daily_live_review_board_pending_count"),
        },
        "current_command": command,
        "launch_steps": launch_steps,
        "review_axis_matrix": review_axis_matrix,
        "launch_day_playbook": launch_day_playbook,
        "launch_day_learning_queue": launch_day_learning_queue,
        "review_axis_pending_counts": _issue_axis_counts(daily_checklist),
        "counts": {
            "formal_blocker_count": len(formal_blockers),
            "candidate_omission_count": len(candidate_omission),
            "no_trade_review_count": len(no_trade),
            "hidden_high_count": len(hidden_high),
            "strategy_backlog_count": len(strategy_backlog),
            "day1_journal_count": len(day1_journal),
        },
        "artifacts": {
            "readiness_summary": str(READINESS_DIR / "summary.json"),
            "smoke_summary": str(SMOKE_DIR / "summary.json"),
            "launch_packet_json": str(OUT_DIR / "launch_packet.json"),
            "launch_packet_cn": str(OUT_DIR / "LAUNCH_PACKET_CN.md"),
            "premarket_step_cards": str(OUT_DIR / "premarket_step_cards.csv"),
            "review_axis_matrix": str(OUT_DIR / "review_axis_matrix.csv"),
            "launch_day_playbook": str(OUT_DIR / "launch_day_playbook.csv"),
            "launch_day_learning_queue": str(OUT_DIR / "launch_day_learning_queue.csv"),
        },
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(launch_steps).to_csv(OUT_DIR / "premarket_step_cards.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(review_axis_matrix).to_csv(OUT_DIR / "review_axis_matrix.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(launch_day_playbook).to_csv(OUT_DIR / "launch_day_playbook.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(launch_day_learning_queue).to_csv(OUT_DIR / "launch_day_learning_queue.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "launch_packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(packet, formal_blockers, blocker_plan, candidate_omission, no_trade, hidden_high, daily_checklist, strategy_backlog, day1_journal)
    return packet


def _write_markdown(
    packet: dict[str, Any],
    formal_blockers: pd.DataFrame,
    blocker_plan: pd.DataFrame,
    candidate_omission: pd.DataFrame,
    no_trade: pd.DataFrame,
    hidden_high: pd.DataFrame,
    daily_board: pd.DataFrame,
    strategy_backlog: pd.DataFrame,
    day1_journal: pd.DataFrame,
) -> None:
    summary = packet["summary"]
    command = packet["current_command"]
    lines = [
        "# G3 实盘启动包 v1",
        "",
        f"- 生成时间：{packet['generated_at']}",
        f"- 启动状态：`{packet['status']}`",
        f"- 烟测状态：`{summary.get('smoke_status')}`",
        f"- 下一交易日：{summary.get('next_trade_entry_date')}",
        f"- 下一交易日买入票：{summary.get('next_trade_ticket_count')}",
        f"- 当前准入：`{summary.get('live_admission_status')}`",
        f"- 人工买入允许：{summary.get('live_admission_buy_allowed')}",
        f"- 盘前阻断动作：{summary.get('live_admission_blocking_command_count')}",
        f"- 当前下一步：{summary.get('live_premarket_next_action')}",
        "",
        "## 结论",
        "",
    ]
    if _truthy(summary.get("live_admission_buy_allowed")):
        lines.append("当前可以进入人工实盘复核，但仍不代表自动下单；必须复核候选、真实持仓、现金和下单锁定。")
    else:
        lines.append("当前不买入。系统已经进入实战前处理流程，但仍有盘前动作未清；先处理证据和复审，再判断是否交易。")

    lines += [
        "",
        "## 盘前第一动作",
        "",
        f"- 动作组：`{command.get('action_group') or '--'}`",
        f"- 动作：{command.get('action_label') or command.get('next_action') or '--'}",
        f"- 可执行：{command.get('can_execute_now')}",
        f"- 需要确认：{command.get('requires_confirmation')}",
        f"- 推荐页面动作：{command.get('recommended_ui_action') or '--'}",
        f"- 推荐 API：`{command.get('recommended_api_action') or '--'}`",
        f"- 完成检查：{(command.get('current_action') or {}).get('completion_check') or '--'}",
        f"- 失败边界：{(command.get('current_action') or {}).get('stop_if_fail') or '--'}",
        f"- 自然交易边界：{(command.get('current_action') or {}).get('natural_trade_boundary') or '--'}",
        "",
        "## 盘前步骤卡",
        "",
    ]
    lines += _md_table(packet["launch_steps"], ["step", "action_group", "action_label", "can_execute_now", "completion_check"], limit=8)

    lines += [
        "",
        "## 实战复盘轴矩阵",
        "",
        "这张矩阵用于约束调优方向：先看执行证据，再逐项复盘买点、卖点、选股和策略切换；问题样本进入学习队列，不按单日收益倒推放宽规则。",
        "",
    ]
    lines += _md_table(
        packet.get("review_axis_matrix", []),
        ["axis_label", "pending_count", "high_risk_count", "formal_required_count", "first_object", "next_action", "launch_decision"],
        limit=10,
    )

    lines += [
        "",
        "## 启动日执行剧本",
        "",
        "按时间窗口执行：盘前先清证据和准入，盘中只观察记录，盘后再按复盘轴归因；任何一步失败都不反向放宽买点、卖点、选股或策略切换。",
        "",
    ]
    lines += _md_table(
        packet.get("launch_day_playbook", []),
        ["priority", "window", "checkpoint_time", "action_type", "review_axis", "object", "required_action", "review_status", "launch_permission_effect"],
        limit=16,
    )

    lines += [
        "",
        "## 启动日学习队列",
        "",
        f"- 需调优复盘：{summary.get('launch_day_learning_needs_review_count')}；继续观察/数据缺口：{summary.get('launch_day_learning_watch_more_count')}",
        "",
    ]
    lines += _md_table(
        packet.get("launch_day_learning_queue", []),
        ["priority", "learning_status", "axis_label", "object", "evidence", "hidden_risk", "optimization_direction"],
        limit=12,
    )

    lines += [
        "",
        "## 正式必处理阻断",
        "",
    ]
    lines += _md_table(_records(formal_blockers), ["priority", "review_scope", "object", "action", "post_action_check"], limit=10)

    lines += [
        "",
        "## 阻断解决计划",
        "",
    ]
    lines += _md_table(_records(blocker_plan), ["priority", "resolution_type", "object", "recommended_ui_action", "completion_check"], limit=10)

    lines += [
        "",
        "## 候选遗漏复盘",
        "",
    ]
    lines += _md_table(_records(candidate_omission), ["rank", "code", "name", "entry_date", "selection_state", "review_status", "action_recommendation"], limit=10)

    lines += [
        "",
        "## 无票日复盘",
        "",
    ]
    lines += _md_table(_records(no_trade), ["rank", "entry_date", "review_type", "posture", "review_status", "hidden_risk", "next_action"], limit=8)

    lines += [
        "",
        "## 高优先级隐患",
        "",
    ]
    lines += _md_table(_records(hidden_high), ["priority", "risk_level", "issue_area", "review_scope", "object", "risk_signal", "next_review_action"], limit=12)

    lines += [
        "",
        "## 四轴逐项复盘覆盖",
        "",
        f"- 待复盘轴计数：{json.dumps(packet.get('review_axis_pending_counts', {}), ensure_ascii=False)}",
        "",
    ]
    lines += _md_table(_records(daily_board), ["priority", "review_window", "checkpoint_time", "review_axis", "review_scope", "object", "review_status"], limit=12)

    lines += [
        "",
        "## 策略学习队列",
        "",
    ]
    lines += _md_table(_records(strategy_backlog), ["priority", "issue_area", "review_axis", "object", "optimization_suggestion"], limit=10)

    lines += [
        "",
        "## Day1 复盘日志",
        "",
    ]
    lines += _md_table(_records(day1_journal), ["priority", "journal_window", "checkpoint_time", "journal_type", "object", "action"], limit=12)

    lines += [
        "",
        "## 禁止事项",
        "",
        "- 不因无票日强行补票。",
        "- 不因单日收益压力放宽买点、选股或策略切换规则。",
        "- 不在盘前动作未清时允许真实买入。",
        "- 不把执行证据缺口误调成策略参数。",
        "- 不开启自动下单或真实下单通道。",
    ]
    (OUT_DIR / "LAUNCH_PACKET_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    packet = build_packet()
    print(json.dumps(packet, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
