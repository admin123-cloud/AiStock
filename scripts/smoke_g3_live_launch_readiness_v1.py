from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, runtime_path  # noqa: E402


BASE_URL = os.getenv("AISTOCK_API_BASE_URL", "http://127.0.0.1:8000/api").rstrip("/")
OUT_DIR = report_path("g3_live_launch_smoke_v1")
ATTEMPTS_PATH = runtime_path("gen3_state_alpha", "premarket_action_attempts.json")
SNAPSHOTS_PATH = runtime_path("gen3_state_alpha", "live_launch_review_snapshots.json")
TUNING_REVIEWS_PATH = runtime_path("gen3_state_alpha", "strategy_tuning_task_reviews.json")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _request_json(method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 120) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "http_status": response.status,
                "path": path,
                "json": json.loads(raw) if raw else None,
            }
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed: Any
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        return {
            "ok": False,
            "http_status": exc.code,
            "path": path,
            "error": str(exc),
            "json": parsed,
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "http_status": None,
            "path": path,
            "error": str(exc),
            "json": None,
        }


def _attempt_count() -> int:
    if not ATTEMPTS_PATH.exists():
        return 0
    try:
        data = json.loads(ATTEMPTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return -1
    if isinstance(data, dict):
        attempts = data.get("attempts")
        return len(attempts) if isinstance(attempts, list) else -1
    return len(data) if isinstance(data, list) else -1


def _snapshot_count() -> int:
    if not SNAPSHOTS_PATH.exists():
        return 0
    try:
        data = json.loads(SNAPSHOTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return -1
    if isinstance(data, dict):
        rows = data.get("snapshots")
        return len(rows) if isinstance(rows, list) else -1
    return len(data) if isinstance(data, list) else -1


def _tuning_review_count() -> int:
    if not TUNING_REVIEWS_PATH.exists():
        return 0
    try:
        data = json.loads(TUNING_REVIEWS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return -1
    if isinstance(data, dict):
        reviews = data.get("reviews")
        return len(reviews) if isinstance(reviews, dict) else -1
    return -1


def _summary_value(readiness: dict[str, Any], key: str, default: Any = None) -> Any:
    data = readiness.get("json") if isinstance(readiness, dict) else {}
    if not isinstance(data, dict):
        return default
    summary = data.get("summary")
    if isinstance(summary, dict) and key in summary:
        return summary.get(key)
    return data.get(key, default)


def _packet_summary_value(packet: dict[str, Any], key: str, default: Any = None) -> Any:
    if not isinstance(packet, dict):
        return default
    summary = packet.get("summary")
    if isinstance(summary, dict) and key in summary:
        return summary.get(key)
    return packet.get(key, default)


def _make_check(name: str, passed: bool, detail: str, severity: str = "block") -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "severity": severity,
        "detail": detail,
    }


def _write_outputs(result: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    checks = result.get("checks", [])
    blocking_failed = [x for x in checks if x.get("severity") == "block" and not x.get("passed")]
    warning_failed = [x for x in checks if x.get("severity") != "block" and not x.get("passed")]
    summary = result.get("readiness_summary", {})
    command = result.get("premarket_command", {})
    command = command if isinstance(command, dict) else {}
    lines = [
        "# G3 实盘启动烟测 v1",
        "",
        f"- 生成时间：{result.get('generated_at')}",
        f"- API：`{result.get('base_url')}`",
        f"- 结论：`{result.get('status')}`",
        f"- 阻断失败：{len(blocking_failed)}",
        f"- 观察失败：{len(warning_failed)}",
        "",
        "## 当前准入摘要",
        "",
        f"- 下一交易日：{summary.get('next_trade_entry_date')}",
        f"- 下一交易日买入票：{summary.get('next_trade_ticket_count')}",
        f"- 盘前动作状态：{summary.get('live_admission_status')}",
        f"- 人工买入允许：{summary.get('live_admission_buy_allowed')}",
        f"- 下一动作：{summary.get('live_premarket_next_action')}",
        f"- 正式买入信号：{result.get('formal_buy_signal')}",
        f"- 自动下单允许：{result.get('auto_order_allowed')}",
        f"- 下单通道开启：{result.get('order_path_enabled')}",
        f"- 动作尝试数：{summary.get('live_premarket_action_attempt_count')}",
        f"- 当前指挥动作：{command.get('action_label') or '--'}",
        f"- 当前动作组：`{command.get('action_group') or '--'}`",
        f"- 可由 API 执行：{command.get('can_execute_by_api')}",
        f"- 需要确认：{command.get('requires_confirmation')}",
        "",
        "## 检查项",
        "",
    ]
    for item in checks:
        mark = "PASS" if item.get("passed") else "FAIL"
        lines.append(f"- `{mark}` {item.get('name')}：{item.get('detail')}")
    lines.append("")
    lines.append("## 解释")
    lines.append("")
    lines.append("这个烟测只验证实盘启动链路的运行安全与证据边界，不以历史收益最大化作为通过条件。真实买卖仍由 G3 复盘准入、盘前动作清理、人工确认和正式下单锁定共同约束。")
    (OUT_DIR / "SUMMARY_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_smoke() -> dict[str, Any]:
    before_attempts = _attempt_count()
    before_snapshots = _snapshot_count()
    before_tuning_reviews = _tuning_review_count()
    health = _request_json("GET", "/health/check", timeout=15)
    candidate_reviews = _request_json("GET", "/gen3-state-alpha/candidate-omission-reviews", timeout=30)
    daily_reviews = _request_json("GET", "/gen3-state-alpha/daily-review-checklist-reviews", timeout=30)
    launch_playbook_reviews = _request_json("GET", "/gen3-state-alpha/launch-day-playbook-reviews", timeout=30)
    launch_review_snapshots = _request_json("GET", "/gen3-state-alpha/live-launch-review-snapshots", timeout=30)
    launch_review_snapshots_json = launch_review_snapshots.get("json") if isinstance(launch_review_snapshots.get("json"), dict) else {}
    launch_review_snapshot_rows = launch_review_snapshots_json.get("snapshots") if isinstance(launch_review_snapshots_json.get("snapshots"), list) else []
    first_snapshot_id = ""
    if launch_review_snapshot_rows:
        first_snapshot = launch_review_snapshot_rows[0] if isinstance(launch_review_snapshot_rows[0], dict) else {}
        first_snapshot_id = str(first_snapshot.get("snapshot_id") or "")
    launch_review_snapshot_detail = (
        _request_json("GET", f"/gen3-state-alpha/live-launch-review-snapshot/{first_snapshot_id}", timeout=30)
        if first_snapshot_id
        else {"ok": True, "json": {"ok": True, "skipped": True, "reason": "no snapshots yet"}, "http_status": 200}
    )
    live_learning_ledger = _request_json("GET", "/gen3-state-alpha/live-learning-ledger", timeout=60)
    live_launch_decision = _request_json("GET", "/gen3-state-alpha/live-launch-decision", timeout=60)
    live_blocker_board = _request_json("GET", "/gen3-state-alpha/live-blocker-evidence-board", timeout=60)
    live_readiness_audit = _request_json("GET", "/gen3-state-alpha/live-launch-readiness-audit", timeout=60)
    after_readiness_audit_attempts = _attempt_count()
    after_readiness_audit_snapshots = _snapshot_count()
    strategy_tuning_axis_board = _request_json("GET", "/gen3-state-alpha/strategy-tuning-axis-board", timeout=60)
    after_strategy_tuning_attempts = _attempt_count()
    after_strategy_tuning_snapshots = _snapshot_count()
    strategy_tuning_review_queue = _request_json("GET", "/gen3-state-alpha/strategy-tuning-review-queue", timeout=60)
    after_strategy_queue_attempts = _attempt_count()
    after_strategy_queue_snapshots = _snapshot_count()
    strategy_tuning_completion_audit = _request_json("GET", "/gen3-state-alpha/strategy-tuning-completion-audit", timeout=60)
    after_strategy_completion_attempts = _attempt_count()
    after_strategy_completion_snapshots = _snapshot_count()
    strategy_tuning_replay_suggestions = _request_json("GET", "/gen3-state-alpha/strategy-tuning-replay-suggestions", timeout=60)
    after_strategy_suggestions_attempts = _attempt_count()
    after_strategy_suggestions_snapshots = _snapshot_count()
    strategy_tuning_replay_session = _request_json("GET", "/gen3-state-alpha/strategy-tuning-replay-session", timeout=60)
    after_strategy_session_attempts = _attempt_count()
    after_strategy_session_snapshots = _snapshot_count()
    strategy_tuning_current_step_completion = _request_json("GET", "/gen3-state-alpha/strategy-tuning-current-step-completion-packet", timeout=60)
    after_strategy_step_completion_attempts = _attempt_count()
    after_strategy_step_completion_snapshots = _snapshot_count()
    strategy_tuning_task_reviews_before = _request_json("GET", "/gen3-state-alpha/strategy-tuning-task-reviews", timeout=30)
    after_strategy_reviews_read_attempts = _attempt_count()
    after_strategy_reviews_read_snapshots = _snapshot_count()
    premarket_control = _request_json("GET", "/gen3-state-alpha/premarket-control", timeout=60)
    broker_sync_preflight = _request_json("GET", "/gen3-state-alpha/broker/holdings/sync-ths-preflight", timeout=60)
    after_broker_preflight_attempts = _attempt_count()
    after_broker_preflight_snapshots = _snapshot_count()
    broker_sync_confirmation = _request_json("GET", "/gen3-state-alpha/broker/holdings/sync-ths-confirmation-packet", timeout=60)
    after_broker_confirmation_attempts = _attempt_count()
    after_broker_confirmation_snapshots = _snapshot_count()
    broker_sync_outcome = _request_json("GET", "/gen3-state-alpha/broker/holdings/sync-ths-outcome", timeout=60)
    after_broker_outcome_attempts = _attempt_count()
    after_broker_outcome_snapshots = _snapshot_count()
    broker_post_sync_acceptance = _request_json("GET", "/gen3-state-alpha/broker/holdings/post-sync-acceptance", timeout=60)
    after_broker_post_sync_acceptance_attempts = _attempt_count()
    after_broker_post_sync_acceptance_snapshots = _snapshot_count()
    after_broker_post_sync_acceptance_reviews = _tuning_review_count()
    live_action_console = _request_json("GET", "/gen3-state-alpha/live-action-console", timeout=60)
    after_live_action_console_attempts = _attempt_count()
    after_live_action_console_snapshots = _snapshot_count()
    after_live_action_console_reviews = _tuning_review_count()
    live_action_console_empty_review = _request_json("POST", "/gen3-state-alpha/live-action-console/step-review", {}, timeout=60)
    after_live_action_console_empty_review_attempts = _attempt_count()
    after_live_action_console_empty_review_snapshots = _snapshot_count()
    after_live_action_console_empty_review_reviews = _tuning_review_count()
    live_replay_cockpit = _request_json("GET", "/gen3-state-alpha/live-replay-cockpit", timeout=60)
    after_live_replay_cockpit_attempts = _attempt_count()
    after_live_replay_cockpit_snapshots = _snapshot_count()
    after_live_replay_cockpit_reviews = _tuning_review_count()
    historical_decision_replay = _request_json("GET", "/gen3-state-alpha/historical-decision-replay-tasks?limit=40", timeout=60)
    after_historical_decision_replay_attempts = _attempt_count()
    after_historical_decision_replay_snapshots = _snapshot_count()
    after_historical_decision_replay_reviews = _tuning_review_count()
    historical_decision_replay_audit = _request_json("GET", "/gen3-state-alpha/historical-decision-replay-audit", timeout=60)
    after_historical_decision_replay_audit_attempts = _attempt_count()
    after_historical_decision_replay_audit_snapshots = _snapshot_count()
    after_historical_decision_replay_audit_reviews = _tuning_review_count()
    broker_post_sync_record = _request_json(
        "POST",
        "/gen3-state-alpha/broker/holdings/post-sync-acceptance/record-execution-evidence",
        {"source": "g3_live_launch_smoke_v1", "refresh": False},
        timeout=60,
    )
    after_broker_post_sync_record_attempts = _attempt_count()
    after_broker_post_sync_record_snapshots = _snapshot_count()
    after_broker_post_sync_record_reviews = _tuning_review_count()
    empty_candidate = _request_json("POST", "/gen3-state-alpha/candidate-omission-review-and-run", {}, timeout=90)
    after_empty_attempts = _attempt_count()
    empty_launch_playbook = _request_json("POST", "/gen3-state-alpha/launch-day-playbook-review-and-run", {}, timeout=90)
    after_empty_launch_playbook_attempts = _attempt_count()
    after_empty_launch_playbook_snapshots = _snapshot_count()
    execute_without_confirm = _request_json(
        "POST",
        "/gen3-state-alpha/premarket-control/execute-next",
        {"source": "g3_live_launch_smoke_v1"},
        timeout=90,
    )
    after_unconfirmed_execute_attempts = _attempt_count()
    after_unconfirmed_execute_snapshots = _snapshot_count()
    initial_control_json = premarket_control.get("json") if isinstance(premarket_control.get("json"), dict) else {}
    initial_command = initial_control_json.get("command") if isinstance(initial_control_json.get("command"), dict) else {}
    initial_action_group = str(initial_command.get("action_group") or "")
    wrong_action_group = "rerun_readiness_audit" if initial_action_group != "rerun_readiness_audit" else "sync_broker_holding_price"
    wrong_action_execute = _request_json(
        "POST",
        "/gen3-state-alpha/premarket-control/execute-next",
        {
            "source": "g3_live_launch_smoke_v1_wrong_action_guard",
            "action_group": wrong_action_group,
            "confirm": True,
        },
        timeout=90,
    )
    after_wrong_action_attempts = _attempt_count()
    after_wrong_action_snapshots = _snapshot_count()
    readiness = _request_json("POST", "/gen3-state-alpha/realtime-readiness-review/run", {}, timeout=180)
    after_readiness_attempts = _attempt_count()
    after_readiness_snapshots = _snapshot_count()
    launch_packet = _request_json("GET", "/gen3-state-alpha/live-launch-packet", timeout=90)
    after_launch_packet_read_attempts = _attempt_count()
    after_launch_packet_read_snapshots = _snapshot_count()
    launch_packet_run = _request_json("POST", "/gen3-state-alpha/live-launch-packet/run", {}, timeout=240)
    after_launch_packet_run_attempts = _attempt_count()
    after_launch_packet_run_snapshots = _snapshot_count()

    readiness_json = readiness.get("json") if isinstance(readiness.get("json"), dict) else {}
    summary = readiness_json.get("summary") if isinstance(readiness_json, dict) else {}
    summary = summary if isinstance(summary, dict) else {}

    formal_buy_signal = bool(readiness_json.get("formal_buy_signal")) if isinstance(readiness_json, dict) else False
    auto_order_allowed = bool(readiness_json.get("auto_order_allowed")) if isinstance(readiness_json, dict) else False
    order_path_enabled = bool(readiness_json.get("order_path_enabled")) if isinstance(readiness_json, dict) else False
    empty_json = empty_candidate.get("json") if isinstance(empty_candidate.get("json"), dict) else {}
    empty_launch_playbook_json = empty_launch_playbook.get("json") if isinstance(empty_launch_playbook.get("json"), dict) else {}
    snapshot_detail_json = launch_review_snapshot_detail.get("json") if isinstance(launch_review_snapshot_detail.get("json"), dict) else {}
    snapshot_detail_payload = snapshot_detail_json.get("detail") if isinstance(snapshot_detail_json.get("detail"), dict) else {}
    snapshot_detail_generated_at = str(snapshot_detail_payload.get("generated_at") or "")
    snapshot_detail_has_learning_ledger = "live_learning_ledger" in snapshot_detail_payload or bool(snapshot_detail_json.get("skipped"))
    snapshot_detail_is_legacy = bool(snapshot_detail_generated_at and snapshot_detail_generated_at < "2026-06-21 19:15:00")
    live_learning_json = live_learning_ledger.get("json") if isinstance(live_learning_ledger.get("json"), dict) else {}
    live_learning_rows = live_learning_json.get("learning_ledger") if isinstance(live_learning_json.get("learning_ledger"), list) else []
    live_decision_json = live_launch_decision.get("json") if isinstance(live_launch_decision.get("json"), dict) else {}
    live_decision = live_decision_json.get("decision") if isinstance(live_decision_json.get("decision"), dict) else {}
    live_blocker_board_json = live_blocker_board.get("json") if isinstance(live_blocker_board.get("json"), dict) else {}
    live_blocker_board_rows = live_blocker_board_json.get("rows") if isinstance(live_blocker_board_json.get("rows"), list) else []
    live_readiness_audit_json = live_readiness_audit.get("json") if isinstance(live_readiness_audit.get("json"), dict) else {}
    live_readiness_audit_payload = live_readiness_audit_json.get("audit") if isinstance(live_readiness_audit_json.get("audit"), dict) else {}
    strategy_tuning_json = strategy_tuning_axis_board.get("json") if isinstance(strategy_tuning_axis_board.get("json"), dict) else {}
    strategy_tuning_summary = strategy_tuning_json.get("summary") if isinstance(strategy_tuning_json.get("summary"), dict) else {}
    strategy_tuning_axis_rows = strategy_tuning_json.get("axis_rows") if isinstance(strategy_tuning_json.get("axis_rows"), list) else []
    strategy_tuning_risk_rows = strategy_tuning_json.get("risk_rows") if isinstance(strategy_tuning_json.get("risk_rows"), list) else []
    strategy_queue_json = strategy_tuning_review_queue.get("json") if isinstance(strategy_tuning_review_queue.get("json"), dict) else {}
    strategy_queue_summary = strategy_queue_json.get("summary") if isinstance(strategy_queue_json.get("summary"), dict) else {}
    strategy_queue_axis_rows = strategy_queue_json.get("axis_task_summary") if isinstance(strategy_queue_json.get("axis_task_summary"), list) else []
    strategy_queue_task_rows = strategy_queue_json.get("task_rows") if isinstance(strategy_queue_json.get("task_rows"), list) else []
    strategy_completion_json = strategy_tuning_completion_audit.get("json") if isinstance(strategy_tuning_completion_audit.get("json"), dict) else {}
    strategy_completion_audit_payload = strategy_completion_json.get("audit") if isinstance(strategy_completion_json.get("audit"), dict) else {}
    strategy_completion_summary = strategy_completion_json.get("summary") if isinstance(strategy_completion_json.get("summary"), dict) else {}
    strategy_completion_axis_rows = strategy_completion_json.get("axis_completion_rows") if isinstance(strategy_completion_json.get("axis_completion_rows"), list) else []
    strategy_suggestions_json = strategy_tuning_replay_suggestions.get("json") if isinstance(strategy_tuning_replay_suggestions.get("json"), dict) else {}
    strategy_suggestions_summary = strategy_suggestions_json.get("summary") if isinstance(strategy_suggestions_json.get("summary"), dict) else {}
    strategy_suggestion_rows = strategy_suggestions_json.get("suggestions") if isinstance(strategy_suggestions_json.get("suggestions"), list) else []
    strategy_session_json = strategy_tuning_replay_session.get("json") if isinstance(strategy_tuning_replay_session.get("json"), dict) else {}
    strategy_session_payload = strategy_session_json.get("session") if isinstance(strategy_session_json.get("session"), dict) else {}
    strategy_session_summary = strategy_session_json.get("summary") if isinstance(strategy_session_json.get("summary"), dict) else {}
    strategy_session_steps = strategy_session_json.get("session_steps") if isinstance(strategy_session_json.get("session_steps"), list) else []
    strategy_step_completion_json = strategy_tuning_current_step_completion.get("json") if isinstance(strategy_tuning_current_step_completion.get("json"), dict) else {}
    strategy_step_completion_packet = strategy_step_completion_json.get("packet") if isinstance(strategy_step_completion_json.get("packet"), dict) else {}
    strategy_step_completion_gaps = strategy_step_completion_packet.get("gaps") if isinstance(strategy_step_completion_packet.get("gaps"), list) else []
    strategy_task_reviews_before_json = strategy_tuning_task_reviews_before.get("json") if isinstance(strategy_tuning_task_reviews_before.get("json"), dict) else {}
    broker_preflight_json = broker_sync_preflight.get("json") if isinstance(broker_sync_preflight.get("json"), dict) else {}
    broker_confirmation_json = broker_sync_confirmation.get("json") if isinstance(broker_sync_confirmation.get("json"), dict) else {}
    broker_confirmation_fingerprint = broker_confirmation_json.get("action_fingerprint") if isinstance(broker_confirmation_json.get("action_fingerprint"), dict) else {}
    broker_outcome_json = broker_sync_outcome.get("json") if isinstance(broker_sync_outcome.get("json"), dict) else {}
    broker_outcome_payload = broker_outcome_json.get("outcome") if isinstance(broker_outcome_json.get("outcome"), dict) else {}
    broker_post_sync_acceptance_json = broker_post_sync_acceptance.get("json") if isinstance(broker_post_sync_acceptance.get("json"), dict) else {}
    broker_post_sync_acceptance_payload = broker_post_sync_acceptance_json.get("acceptance") if isinstance(broker_post_sync_acceptance_json.get("acceptance"), dict) else {}
    live_action_console_json = live_action_console.get("json") if isinstance(live_action_console.get("json"), dict) else {}
    live_action_console_summary = live_action_console_json.get("summary") if isinstance(live_action_console_json.get("summary"), dict) else {}
    live_action_console_steps = live_action_console_json.get("steps") if isinstance(live_action_console_json.get("steps"), list) else []
    live_action_console_empty_review_json = live_action_console_empty_review.get("json") if isinstance(live_action_console_empty_review.get("json"), dict) else {}
    live_replay_cockpit_json = live_replay_cockpit.get("json") if isinstance(live_replay_cockpit.get("json"), dict) else {}
    live_replay_cockpit_summary = live_replay_cockpit_json.get("summary") if isinstance(live_replay_cockpit_json.get("summary"), dict) else {}
    live_replay_cockpit_action_items = live_replay_cockpit_json.get("action_items") if isinstance(live_replay_cockpit_json.get("action_items"), list) else []
    live_replay_cockpit_axis_progress = live_replay_cockpit_json.get("axis_progress") if isinstance(live_replay_cockpit_json.get("axis_progress"), list) else []
    live_replay_cockpit_checks = live_replay_cockpit_json.get("natural_trade_checks") if isinstance(live_replay_cockpit_json.get("natural_trade_checks"), list) else []
    historical_decision_replay_json = historical_decision_replay.get("json") if isinstance(historical_decision_replay.get("json"), dict) else {}
    historical_decision_replay_summary = historical_decision_replay_json.get("summary") if isinstance(historical_decision_replay_json.get("summary"), dict) else {}
    historical_decision_replay_tasks = historical_decision_replay_json.get("tasks") if isinstance(historical_decision_replay_json.get("tasks"), list) else []
    historical_decision_replay_audit_json = historical_decision_replay_audit.get("json") if isinstance(historical_decision_replay_audit.get("json"), dict) else {}
    historical_decision_replay_audit_summary = historical_decision_replay_audit_json.get("summary") if isinstance(historical_decision_replay_audit_json.get("summary"), dict) else {}
    historical_decision_replay_audit_payload = historical_decision_replay_audit_json.get("audit") if isinstance(historical_decision_replay_audit_json.get("audit"), dict) else {}
    historical_decision_replay_axis_rows = historical_decision_replay_audit_json.get("axis_audit_rows") if isinstance(historical_decision_replay_audit_json.get("axis_audit_rows"), list) else []
    broker_post_sync_record_json = broker_post_sync_record.get("json") if isinstance(broker_post_sync_record.get("json"), dict) else {}
    broker_post_sync_record_acceptance = broker_post_sync_record_json.get("acceptance") if isinstance(broker_post_sync_record_json.get("acceptance"), dict) else {}
    smoke_tuning_task = strategy_queue_task_rows[0] if strategy_queue_task_rows and isinstance(strategy_queue_task_rows[0], dict) else {}
    strategy_tuning_task_review_save = (
        _request_json(
            "POST",
            "/gen3-state-alpha/strategy-tuning-task-review",
            {
                "task_key": smoke_tuning_task.get("task_key"),
                "axis": smoke_tuning_task.get("axis"),
                "axis_label": smoke_tuning_task.get("axis_label"),
                "origin": smoke_tuning_task.get("origin"),
                "object": smoke_tuning_task.get("object"),
                "problem_signal": smoke_tuning_task.get("problem_signal"),
                "source_status": smoke_tuning_task.get("source_status"),
                "review_result": "continue_watch",
                "evidence": smoke_tuning_task.get("evidence"),
                "completion_evidence": smoke_tuning_task.get("completion_evidence"),
                "review_note": "smoke: record tuning review without enabling trading",
                "optimization_suggestion": smoke_tuning_task.get("suggested_learning"),
                "next_action": smoke_tuning_task.get("next_action"),
                "optimization_boundary": smoke_tuning_task.get("optimization_boundary"),
                "can_execute_trade": False,
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            },
            timeout=60,
        )
        if smoke_tuning_task.get("task_key")
        else {"ok": True, "json": {"ok": True, "skipped": True, "reason": "no tuning tasks"}, "http_status": 200}
    )
    after_strategy_review_save_attempts = _attempt_count()
    after_strategy_review_save_snapshots = _snapshot_count()
    after_strategy_review_save_count = _tuning_review_count()
    strategy_tuning_task_reviews_after = _request_json("GET", "/gen3-state-alpha/strategy-tuning-task-reviews", timeout=30)
    strategy_tuning_review_queue_after_save = _request_json("GET", "/gen3-state-alpha/strategy-tuning-review-queue", timeout=60)
    strategy_tuning_completion_after_save = _request_json("GET", "/gen3-state-alpha/strategy-tuning-completion-audit", timeout=60)
    strategy_review_save_json = strategy_tuning_task_review_save.get("json") if isinstance(strategy_tuning_task_review_save.get("json"), dict) else {}
    strategy_task_reviews_after_json = strategy_tuning_task_reviews_after.get("json") if isinstance(strategy_tuning_task_reviews_after.get("json"), dict) else {}
    strategy_queue_after_save_json = strategy_tuning_review_queue_after_save.get("json") if isinstance(strategy_tuning_review_queue_after_save.get("json"), dict) else {}
    strategy_queue_after_save_summary = strategy_queue_after_save_json.get("summary") if isinstance(strategy_queue_after_save_json.get("summary"), dict) else {}
    strategy_completion_after_save_json = strategy_tuning_completion_after_save.get("json") if isinstance(strategy_tuning_completion_after_save.get("json"), dict) else {}
    strategy_completion_after_save_summary = strategy_completion_after_save_json.get("summary") if isinstance(strategy_completion_after_save_json.get("summary"), dict) else {}
    control_json = premarket_control.get("json") if isinstance(premarket_control.get("json"), dict) else {}
    command = control_json.get("command") if isinstance(control_json.get("command"), dict) else {}
    unconfirmed_json = execute_without_confirm.get("json") if isinstance(execute_without_confirm.get("json"), dict) else {}
    unconfirmed_error = str(unconfirmed_json.get("error", ""))
    unconfirmed_ok = (
        unconfirmed_json.get("ok") is False
        and unconfirmed_error in {"confirmation_required", "manual_review_required", "current_action_not_executable_yet"}
    )
    wrong_action_json = wrong_action_execute.get("json") if isinstance(wrong_action_execute.get("json"), dict) else {}
    wrong_action_error = str(wrong_action_json.get("error", ""))
    wrong_action_ok = wrong_action_json.get("ok") is False and wrong_action_error == "action_group_changed"
    launch_packet_json = launch_packet.get("json") if isinstance(launch_packet.get("json"), dict) else {}
    launch_packet_payload = launch_packet_json.get("packet") if isinstance(launch_packet_json.get("packet"), dict) else {}
    launch_packet_axis_rows = launch_packet_json.get("review_axis_matrix") if isinstance(launch_packet_json.get("review_axis_matrix"), list) else []
    launch_packet_playbook_rows = launch_packet_json.get("launch_day_playbook") if isinstance(launch_packet_json.get("launch_day_playbook"), list) else []
    launch_packet_learning_rows = launch_packet_json.get("launch_day_learning_queue") if isinstance(launch_packet_json.get("launch_day_learning_queue"), list) else []
    launch_packet_run_json = launch_packet_run.get("json") if isinstance(launch_packet_run.get("json"), dict) else {}
    launch_packet_run_payload = launch_packet_run_json.get("packet") if isinstance(launch_packet_run_json.get("packet"), dict) else {}
    launch_packet_run_axis_rows = launch_packet_run_json.get("review_axis_matrix") if isinstance(launch_packet_run_json.get("review_axis_matrix"), list) else []
    launch_packet_run_playbook_rows = launch_packet_run_json.get("launch_day_playbook") if isinstance(launch_packet_run_json.get("launch_day_playbook"), list) else []
    launch_packet_run_learning_rows = launch_packet_run_json.get("launch_day_learning_queue") if isinstance(launch_packet_run_json.get("launch_day_learning_queue"), list) else []

    checks = [
        _make_check("backend_health", health.get("ok") and health.get("http_status") == 200, f"HTTP {health.get('http_status')}"),
        _make_check("candidate_review_endpoint", candidate_reviews.get("ok"), f"HTTP {candidate_reviews.get('http_status')}"),
        _make_check("daily_review_endpoint", daily_reviews.get("ok"), f"HTTP {daily_reviews.get('http_status')}"),
        _make_check("launch_day_playbook_review_endpoint", launch_playbook_reviews.get("ok"), f"HTTP {launch_playbook_reviews.get('http_status')}"),
        _make_check("live_launch_review_snapshot_endpoint", launch_review_snapshots.get("ok"), f"HTTP {launch_review_snapshots.get('http_status')}"),
        _make_check("live_launch_review_snapshot_detail_endpoint", bool(snapshot_detail_json.get("ok")), f"HTTP {launch_review_snapshot_detail.get('http_status')}, snapshot_id={first_snapshot_id or 'skipped'}"),
        _make_check("live_launch_review_snapshot_learning_ledger_shape", snapshot_detail_has_learning_ledger or snapshot_detail_is_legacy, f"snapshot_id={first_snapshot_id or 'skipped'}, legacy={snapshot_detail_is_legacy}, has_live_learning_ledger={snapshot_detail_has_learning_ledger}"),
        _make_check("live_learning_ledger_endpoint", bool(live_learning_json.get("ok")) and isinstance(live_learning_rows, list), f"HTTP {live_learning_ledger.get('http_status')}, rows={len(live_learning_rows)}"),
        _make_check("live_launch_decision_endpoint", bool(live_decision_json.get("ok")) and bool(live_decision.get("decision_status")), f"HTTP {live_launch_decision.get('http_status')}, status={live_decision.get('decision_status')}"),
        _make_check("live_launch_decision_locks_buy", not bool(live_decision.get("can_buy")) and not bool(live_decision_json.get("formal_buy_signal")) and not bool(live_decision_json.get("auto_order_allowed")) and not bool(live_decision_json.get("order_path_enabled")), f"can_buy={live_decision.get('can_buy')}, formal={live_decision_json.get('formal_buy_signal')}, auto={live_decision_json.get('auto_order_allowed')}, path={live_decision_json.get('order_path_enabled')}"),
        _make_check("live_blocker_evidence_board_endpoint", bool(live_blocker_board_json.get("ok")) and isinstance(live_blocker_board_rows, list), f"HTTP {live_blocker_board.get('http_status')}, rows={len(live_blocker_board_rows)}"),
        _make_check("live_blocker_evidence_board_locks_buy", not bool(live_blocker_board_json.get("formal_buy_signal")) and not bool(live_blocker_board_json.get("auto_order_allowed")) and not bool(live_blocker_board_json.get("order_path_enabled")), f"formal={live_blocker_board_json.get('formal_buy_signal')}, auto={live_blocker_board_json.get('auto_order_allowed')}, path={live_blocker_board_json.get('order_path_enabled')}"),
        _make_check("live_launch_readiness_audit_endpoint", bool(live_readiness_audit_json.get("ok")) and bool(live_readiness_audit_payload.get("audit_status")), f"HTTP {live_readiness_audit.get('http_status')}, status={live_readiness_audit_payload.get('audit_status')}"),
        _make_check("live_launch_readiness_audit_locks_buy", not bool(live_readiness_audit_payload.get("can_buy")) and not bool(live_readiness_audit_json.get("formal_buy_signal")) and not bool(live_readiness_audit_json.get("auto_order_allowed")) and not bool(live_readiness_audit_json.get("order_path_enabled")), f"can_buy={live_readiness_audit_payload.get('can_buy')}, formal={live_readiness_audit_json.get('formal_buy_signal')}, auto={live_readiness_audit_json.get('auto_order_allowed')}, path={live_readiness_audit_json.get('order_path_enabled')}"),
        _make_check("live_launch_readiness_audit_does_not_write_attempt", after_readiness_audit_attempts == before_attempts, f"before={before_attempts}, after_audit={after_readiness_audit_attempts}"),
        _make_check("live_launch_readiness_audit_does_not_write_snapshot", after_readiness_audit_snapshots == before_snapshots, f"before_snapshots={before_snapshots}, after_audit={after_readiness_audit_snapshots}"),
        _make_check("strategy_tuning_axis_board_endpoint", bool(strategy_tuning_json.get("ok")) and strategy_tuning_json.get("mode") == "g3_strategy_tuning_axis_board" and isinstance(strategy_tuning_axis_rows, list), f"HTTP {strategy_tuning_axis_board.get('http_status')}, axes={len(strategy_tuning_axis_rows)}, risks={len(strategy_tuning_risk_rows)}"),
        _make_check("strategy_tuning_axis_board_profit_only_disabled", strategy_tuning_summary.get("profit_only_optimization_allowed") is False and strategy_tuning_summary.get("can_change_strategy_contract") is False, f"profit_only={strategy_tuning_summary.get('profit_only_optimization_allowed')}, contract_change={strategy_tuning_summary.get('can_change_strategy_contract')}"),
        _make_check("strategy_tuning_axis_board_locks_buy", not bool(strategy_tuning_json.get("formal_buy_signal")) and not bool(strategy_tuning_json.get("auto_order_allowed")) and not bool(strategy_tuning_json.get("order_path_enabled")), f"formal={strategy_tuning_json.get('formal_buy_signal')}, auto={strategy_tuning_json.get('auto_order_allowed')}, path={strategy_tuning_json.get('order_path_enabled')}"),
        _make_check("strategy_tuning_axis_board_does_not_write_attempt", after_strategy_tuning_attempts == after_readiness_audit_attempts, f"after_audit={after_readiness_audit_attempts}, after_tuning={after_strategy_tuning_attempts}"),
        _make_check("strategy_tuning_axis_board_does_not_write_snapshot", after_strategy_tuning_snapshots == after_readiness_audit_snapshots, f"after_audit={after_readiness_audit_snapshots}, after_tuning={after_strategy_tuning_snapshots}"),
        _make_check("strategy_tuning_review_queue_endpoint", bool(strategy_queue_json.get("ok")) and strategy_queue_json.get("mode") == "g3_strategy_tuning_review_queue" and isinstance(strategy_queue_task_rows, list), f"HTTP {strategy_tuning_review_queue.get('http_status')}, axes={len(strategy_queue_axis_rows)}, tasks={len(strategy_queue_task_rows)}"),
        _make_check("strategy_tuning_review_queue_profit_only_disabled", strategy_queue_summary.get("profit_only_optimization_allowed") is False and strategy_queue_summary.get("can_change_strategy_contract") is False, f"profit_only={strategy_queue_summary.get('profit_only_optimization_allowed')}, contract_change={strategy_queue_summary.get('can_change_strategy_contract')}"),
        _make_check("strategy_tuning_review_queue_locks_trade", not bool(strategy_queue_summary.get("can_execute_trade")) and not bool(strategy_queue_json.get("formal_buy_signal")) and not bool(strategy_queue_json.get("auto_order_allowed")) and not bool(strategy_queue_json.get("order_path_enabled")), f"can_execute={strategy_queue_summary.get('can_execute_trade')}, formal={strategy_queue_json.get('formal_buy_signal')}, auto={strategy_queue_json.get('auto_order_allowed')}, path={strategy_queue_json.get('order_path_enabled')}"),
        _make_check("strategy_tuning_review_queue_does_not_write_attempt", after_strategy_queue_attempts == after_strategy_tuning_attempts, f"after_tuning={after_strategy_tuning_attempts}, after_queue={after_strategy_queue_attempts}"),
        _make_check("strategy_tuning_review_queue_does_not_write_snapshot", after_strategy_queue_snapshots == after_strategy_tuning_snapshots, f"after_tuning={after_strategy_tuning_snapshots}, after_queue={after_strategy_queue_snapshots}"),
        _make_check("strategy_tuning_completion_audit_endpoint", bool(strategy_completion_json.get("ok")) and strategy_completion_json.get("mode") == "g3_strategy_tuning_completion_audit" and bool(strategy_completion_audit_payload.get("audit_status")), f"HTTP {strategy_tuning_completion_audit.get('http_status')}, status={strategy_completion_audit_payload.get('audit_status')}, axes={len(strategy_completion_axis_rows)}"),
        _make_check("strategy_tuning_completion_audit_locks_trade", not bool(strategy_completion_audit_payload.get("can_buy")) and not bool(strategy_completion_summary.get("can_execute_trade")) and not bool(strategy_completion_json.get("formal_buy_signal")) and not bool(strategy_completion_json.get("auto_order_allowed")) and not bool(strategy_completion_json.get("order_path_enabled")), f"can_buy={strategy_completion_audit_payload.get('can_buy')}, can_execute={strategy_completion_summary.get('can_execute_trade')}, formal={strategy_completion_json.get('formal_buy_signal')}, auto={strategy_completion_json.get('auto_order_allowed')}, path={strategy_completion_json.get('order_path_enabled')}"),
        _make_check("strategy_tuning_completion_audit_does_not_write_attempt", after_strategy_completion_attempts == after_strategy_queue_attempts, f"after_queue={after_strategy_queue_attempts}, after_completion={after_strategy_completion_attempts}"),
        _make_check("strategy_tuning_completion_audit_does_not_write_snapshot", after_strategy_completion_snapshots == after_strategy_queue_snapshots, f"after_queue={after_strategy_queue_snapshots}, after_completion={after_strategy_completion_snapshots}"),
        _make_check("strategy_tuning_replay_suggestions_endpoint", bool(strategy_suggestions_json.get("ok")) and strategy_suggestions_json.get("mode") == "g3_strategy_tuning_replay_suggestions" and isinstance(strategy_suggestion_rows, list), f"HTTP {strategy_tuning_replay_suggestions.get('http_status')}, suggestions={len(strategy_suggestion_rows)}, open={strategy_suggestions_summary.get('open_task_count')}"),
        _make_check("strategy_tuning_replay_suggestions_locks_trade", not bool(strategy_suggestions_summary.get("can_execute_trade")) and strategy_suggestions_summary.get("profit_only_optimization_allowed") is False and strategy_suggestions_summary.get("can_change_strategy_contract") is False and not bool(strategy_suggestions_json.get("formal_buy_signal")) and not bool(strategy_suggestions_json.get("auto_order_allowed")) and not bool(strategy_suggestions_json.get("order_path_enabled")), f"can_execute={strategy_suggestions_summary.get('can_execute_trade')}, profit_only={strategy_suggestions_summary.get('profit_only_optimization_allowed')}, contract_change={strategy_suggestions_summary.get('can_change_strategy_contract')}, formal={strategy_suggestions_json.get('formal_buy_signal')}, auto={strategy_suggestions_json.get('auto_order_allowed')}, path={strategy_suggestions_json.get('order_path_enabled')}"),
        _make_check("strategy_tuning_replay_suggestions_does_not_write_attempt", after_strategy_suggestions_attempts == after_strategy_completion_attempts, f"after_completion={after_strategy_completion_attempts}, after_suggestions={after_strategy_suggestions_attempts}"),
        _make_check("strategy_tuning_replay_suggestions_does_not_write_snapshot", after_strategy_suggestions_snapshots == after_strategy_completion_snapshots, f"after_completion={after_strategy_completion_snapshots}, after_suggestions={after_strategy_suggestions_snapshots}"),
        _make_check("strategy_tuning_replay_session_endpoint", bool(strategy_session_json.get("ok")) and strategy_session_json.get("mode") == "g3_strategy_tuning_replay_session" and isinstance(strategy_session_steps, list) and bool(strategy_session_payload.get("session_status")), f"HTTP {strategy_tuning_replay_session.get('http_status')}, status={strategy_session_payload.get('session_status')}, steps={len(strategy_session_steps)}"),
        _make_check("strategy_tuning_replay_session_locks_trade", not bool(strategy_session_summary.get("can_execute_trade")) and strategy_session_summary.get("profit_only_optimization_allowed") is False and strategy_session_summary.get("can_change_strategy_contract") is False and not bool(strategy_session_json.get("formal_buy_signal")) and not bool(strategy_session_json.get("auto_order_allowed")) and not bool(strategy_session_json.get("order_path_enabled")), f"can_execute={strategy_session_summary.get('can_execute_trade')}, profit_only={strategy_session_summary.get('profit_only_optimization_allowed')}, contract_change={strategy_session_summary.get('can_change_strategy_contract')}, formal={strategy_session_json.get('formal_buy_signal')}, auto={strategy_session_json.get('auto_order_allowed')}, path={strategy_session_json.get('order_path_enabled')}"),
        _make_check("strategy_tuning_replay_session_does_not_write_attempt", after_strategy_session_attempts == after_strategy_suggestions_attempts, f"after_suggestions={after_strategy_suggestions_attempts}, after_session={after_strategy_session_attempts}"),
        _make_check("strategy_tuning_replay_session_does_not_write_snapshot", after_strategy_session_snapshots == after_strategy_suggestions_snapshots, f"after_suggestions={after_strategy_suggestions_snapshots}, after_session={after_strategy_session_snapshots}"),
        _make_check("strategy_tuning_current_step_completion_endpoint", bool(strategy_step_completion_json.get("ok")) and strategy_step_completion_json.get("mode") == "g3_strategy_tuning_current_step_completion_packet" and bool(strategy_step_completion_packet.get("completion_status")), f"HTTP {strategy_tuning_current_step_completion.get('http_status')}, status={strategy_step_completion_packet.get('completion_status')}, gaps={len(strategy_step_completion_gaps)}"),
        _make_check("strategy_tuning_current_step_completion_locks_trade", not bool(strategy_step_completion_packet.get("manual_live_review_allowed")) and not bool(strategy_step_completion_json.get("formal_buy_signal")) and not bool(strategy_step_completion_json.get("auto_order_allowed")) and not bool(strategy_step_completion_json.get("order_path_enabled")), f"manual_live_review_allowed={strategy_step_completion_packet.get('manual_live_review_allowed')}, formal={strategy_step_completion_json.get('formal_buy_signal')}, auto={strategy_step_completion_json.get('auto_order_allowed')}, path={strategy_step_completion_json.get('order_path_enabled')}"),
        _make_check("strategy_tuning_current_step_completion_does_not_write_attempt", after_strategy_step_completion_attempts == after_strategy_session_attempts, f"after_session={after_strategy_session_attempts}, after_step_completion={after_strategy_step_completion_attempts}"),
        _make_check("strategy_tuning_current_step_completion_does_not_write_snapshot", after_strategy_step_completion_snapshots == after_strategy_session_snapshots, f"after_session={after_strategy_session_snapshots}, after_step_completion={after_strategy_step_completion_snapshots}"),
        _make_check("strategy_tuning_task_reviews_endpoint", bool(strategy_task_reviews_before_json.get("ok")) and strategy_task_reviews_before_json.get("mode") == "g3_strategy_tuning_task_reviews", f"HTTP {strategy_tuning_task_reviews_before.get('http_status')}, count={strategy_task_reviews_before_json.get('count')}"),
        _make_check("strategy_tuning_task_reviews_read_does_not_write_attempt", after_strategy_reviews_read_attempts == after_strategy_step_completion_attempts, f"after_step_completion={after_strategy_step_completion_attempts}, after_review_read={after_strategy_reviews_read_attempts}"),
        _make_check("strategy_tuning_task_reviews_read_does_not_write_snapshot", after_strategy_reviews_read_snapshots == after_strategy_step_completion_snapshots, f"after_step_completion={after_strategy_step_completion_snapshots}, after_review_read={after_strategy_reviews_read_snapshots}"),
        _make_check("strategy_tuning_task_review_save_ok", bool(strategy_review_save_json.get("ok")) and not bool(strategy_review_save_json.get("formal_buy_signal")) and not bool(strategy_review_save_json.get("auto_order_allowed")) and not bool(strategy_review_save_json.get("order_path_enabled")), f"HTTP {strategy_tuning_task_review_save.get('http_status')}, skipped={strategy_review_save_json.get('skipped')}, result={(strategy_review_save_json.get('review') or {}).get('review_result') if isinstance(strategy_review_save_json.get('review'), dict) else None}"),
        _make_check("strategy_tuning_task_review_save_does_not_write_attempt", after_strategy_review_save_attempts == after_broker_post_sync_record_attempts, f"after_post_sync_record={after_broker_post_sync_record_attempts}, after_tuning_save={after_strategy_review_save_attempts}"),
        _make_check("strategy_tuning_task_review_save_does_not_write_snapshot", after_strategy_review_save_snapshots == after_broker_post_sync_record_snapshots, f"after_post_sync_record={after_broker_post_sync_record_snapshots}, after_tuning_save={after_strategy_review_save_snapshots}"),
        _make_check("strategy_tuning_task_review_save_updates_review_ledger", strategy_review_save_json.get("skipped") or after_strategy_review_save_count >= before_tuning_reviews, f"before={before_tuning_reviews}, after={after_strategy_review_save_count}, endpoint_count={strategy_task_reviews_after_json.get('count')}"),
        _make_check("strategy_tuning_review_queue_merges_reviews", strategy_review_save_json.get("skipped") or _summary_value({"json": {"summary": strategy_queue_after_save_summary}}, "review_count", 0) >= 1, f"review_count={strategy_queue_after_save_summary.get('review_count')}, reviewed={strategy_queue_after_save_summary.get('reviewed_count')}, followup={strategy_queue_after_save_summary.get('followup_count')}"),
        _make_check("strategy_tuning_completion_audit_after_save_merges_reviews", strategy_review_save_json.get("skipped") or _summary_value({"json": {"summary": strategy_completion_after_save_summary}}, "review_count", 0) >= 1, f"review_count={strategy_completion_after_save_summary.get('review_count')}, todo={strategy_completion_after_save_summary.get('todo_count')}, followup={strategy_completion_after_save_summary.get('followup_count')}"),
        _make_check("premarket_control_endpoint", premarket_control.get("ok") and bool(command), f"HTTP {premarket_control.get('http_status')}, action={command.get('action_group')}"),
        _make_check("broker_sync_preflight_endpoint", bool(broker_preflight_json.get("ok")) and broker_preflight_json.get("mode") == "g3_state_alpha_broker_holdings_sync_preflight", f"HTTP {broker_sync_preflight.get('http_status')}, status={broker_preflight_json.get('preflight_status')}"),
        _make_check("broker_sync_preflight_locks_buy", not bool(broker_preflight_json.get("formal_buy_signal")) and not bool(broker_preflight_json.get("auto_order_allowed")) and not bool(broker_preflight_json.get("order_path_enabled")), f"formal={broker_preflight_json.get('formal_buy_signal')}, auto={broker_preflight_json.get('auto_order_allowed')}, path={broker_preflight_json.get('order_path_enabled')}"),
        _make_check("broker_sync_preflight_does_not_write_attempt", after_broker_preflight_attempts == before_attempts, f"before={before_attempts}, after_preflight={after_broker_preflight_attempts}"),
        _make_check("broker_sync_preflight_does_not_write_snapshot", after_broker_preflight_snapshots == before_snapshots, f"before_snapshots={before_snapshots}, after_preflight={after_broker_preflight_snapshots}"),
        _make_check("broker_sync_confirmation_packet_endpoint", bool(broker_confirmation_json.get("ok")) and broker_confirmation_json.get("mode") == "g3_broker_holdings_sync_confirmation_packet", f"HTTP {broker_sync_confirmation.get('http_status')}, status={broker_confirmation_json.get('packet_status')}"),
        _make_check("broker_sync_confirmation_packet_fingerprint", broker_confirmation_fingerprint.get("action_group") in {"sync_broker_holding_price", ""}, f"action_group={broker_confirmation_fingerprint.get('action_group')}, object={broker_confirmation_fingerprint.get('object')}"),
        _make_check("broker_sync_confirmation_packet_locks_buy", not bool(broker_confirmation_json.get("formal_buy_signal")) and not bool(broker_confirmation_json.get("auto_order_allowed")) and not bool(broker_confirmation_json.get("order_path_enabled")), f"formal={broker_confirmation_json.get('formal_buy_signal')}, auto={broker_confirmation_json.get('auto_order_allowed')}, path={broker_confirmation_json.get('order_path_enabled')}"),
        _make_check("broker_sync_confirmation_does_not_write_attempt", after_broker_confirmation_attempts == after_broker_preflight_attempts, f"after_preflight={after_broker_preflight_attempts}, after_confirmation={after_broker_confirmation_attempts}"),
        _make_check("broker_sync_confirmation_does_not_write_snapshot", after_broker_confirmation_snapshots == after_broker_preflight_snapshots, f"after_preflight={after_broker_preflight_snapshots}, after_confirmation={after_broker_confirmation_snapshots}"),
        _make_check("broker_sync_outcome_endpoint", bool(broker_outcome_json.get("ok")) and broker_outcome_json.get("mode") == "g3_broker_holdings_sync_outcome_verifier", f"HTTP {broker_sync_outcome.get('http_status')}, status={broker_outcome_payload.get('outcome_status')}"),
        _make_check("broker_sync_outcome_locks_buy", not bool(broker_outcome_payload.get("can_buy")) and not bool(broker_outcome_json.get("formal_buy_signal")) and not bool(broker_outcome_json.get("auto_order_allowed")) and not bool(broker_outcome_json.get("order_path_enabled")), f"can_buy={broker_outcome_payload.get('can_buy')}, formal={broker_outcome_json.get('formal_buy_signal')}, auto={broker_outcome_json.get('auto_order_allowed')}, path={broker_outcome_json.get('order_path_enabled')}"),
        _make_check("broker_sync_outcome_does_not_write_attempt", after_broker_outcome_attempts == after_broker_confirmation_attempts, f"after_confirmation={after_broker_confirmation_attempts}, after_outcome={after_broker_outcome_attempts}"),
        _make_check("broker_sync_outcome_does_not_write_snapshot", after_broker_outcome_snapshots == after_broker_confirmation_snapshots, f"after_confirmation={after_broker_confirmation_snapshots}, after_outcome={after_broker_outcome_snapshots}"),
        _make_check("broker_post_sync_acceptance_endpoint", bool(broker_post_sync_acceptance_json.get("ok")) and broker_post_sync_acceptance_json.get("mode") == "g3_broker_post_sync_acceptance_packet" and bool(broker_post_sync_acceptance_payload.get("acceptance_status")), f"HTTP {broker_post_sync_acceptance.get('http_status')}, status={broker_post_sync_acceptance_payload.get('acceptance_status')}"),
        _make_check("broker_post_sync_acceptance_locks_trade", not bool(broker_post_sync_acceptance_payload.get("manual_live_review_allowed")) and not bool(broker_post_sync_acceptance_payload.get("can_buy")) and not bool(broker_post_sync_acceptance_json.get("formal_buy_signal")) and not bool(broker_post_sync_acceptance_json.get("auto_order_allowed")) and not bool(broker_post_sync_acceptance_json.get("order_path_enabled")), f"manual={broker_post_sync_acceptance_payload.get('manual_live_review_allowed')}, can_buy={broker_post_sync_acceptance_payload.get('can_buy')}, formal={broker_post_sync_acceptance_json.get('formal_buy_signal')}, auto={broker_post_sync_acceptance_json.get('auto_order_allowed')}, path={broker_post_sync_acceptance_json.get('order_path_enabled')}"),
        _make_check("broker_post_sync_acceptance_does_not_write_attempt", after_broker_post_sync_acceptance_attempts == after_broker_outcome_attempts, f"after_outcome={after_broker_outcome_attempts}, after_acceptance={after_broker_post_sync_acceptance_attempts}"),
        _make_check("broker_post_sync_acceptance_does_not_write_snapshot", after_broker_post_sync_acceptance_snapshots == after_broker_outcome_snapshots, f"after_outcome={after_broker_outcome_snapshots}, after_acceptance={after_broker_post_sync_acceptance_snapshots}"),
        _make_check("live_action_console_endpoint", bool(live_action_console_json.get("ok")) and live_action_console_json.get("mode") == "g3_live_action_console" and len(live_action_console_steps) >= 5, f"HTTP {live_action_console.get('http_status')}, current={live_action_console_summary.get('current_action_key')}, steps={len(live_action_console_steps)}"),
        _make_check("live_action_console_current_sync", live_action_console_summary.get("current_action_key") == "sync_broker_holding_price", f"current={live_action_console_summary.get('current_action_key')}, status={live_action_console_summary.get('current_action_status')}"),
        _make_check("live_action_console_locks_trade", not bool(live_action_console_summary.get("live_buy_allowed")) and not bool(live_action_console_json.get("formal_buy_signal")) and not bool(live_action_console_json.get("auto_order_allowed")) and not bool(live_action_console_json.get("order_path_enabled")), f"live_buy={live_action_console_summary.get('live_buy_allowed')}, formal={live_action_console_json.get('formal_buy_signal')}, auto={live_action_console_json.get('auto_order_allowed')}, path={live_action_console_json.get('order_path_enabled')}"),
        _make_check("live_action_console_does_not_write_attempt", after_live_action_console_attempts == after_broker_post_sync_acceptance_attempts, f"after_acceptance={after_broker_post_sync_acceptance_attempts}, after_console={after_live_action_console_attempts}"),
        _make_check("live_action_console_does_not_write_snapshot", after_live_action_console_snapshots == after_broker_post_sync_acceptance_snapshots, f"after_acceptance={after_broker_post_sync_acceptance_snapshots}, after_console={after_live_action_console_snapshots}"),
        _make_check("live_action_console_does_not_write_review", after_live_action_console_reviews == after_broker_post_sync_acceptance_reviews, f"after_acceptance_reviews={after_broker_post_sync_acceptance_reviews}, after_console_reviews={after_live_action_console_reviews}"),
        _make_check("live_action_console_empty_review_rejected", live_action_console_empty_review_json.get("ok") is False and "action_key" in str(live_action_console_empty_review_json.get("error") or ""), f"HTTP {live_action_console_empty_review.get('http_status')}, error={live_action_console_empty_review_json.get('error')}"),
        _make_check("live_action_console_empty_review_does_not_write_attempt", after_live_action_console_empty_review_attempts == after_live_action_console_attempts, f"after_console={after_live_action_console_attempts}, after_empty_review={after_live_action_console_empty_review_attempts}"),
        _make_check("live_action_console_empty_review_does_not_write_snapshot", after_live_action_console_empty_review_snapshots == after_live_action_console_snapshots, f"after_console={after_live_action_console_snapshots}, after_empty_review={after_live_action_console_empty_review_snapshots}"),
        _make_check("live_action_console_empty_review_does_not_write_review", after_live_action_console_empty_review_reviews == after_live_action_console_reviews, f"after_console_reviews={after_live_action_console_reviews}, after_empty_review_reviews={after_live_action_console_empty_review_reviews}"),
        _make_check("live_replay_cockpit_endpoint", bool(live_replay_cockpit_json.get("ok")) and live_replay_cockpit_json.get("mode") == "g3_live_replay_cockpit" and len(live_replay_cockpit_action_items) >= 2 and len(live_replay_cockpit_axis_progress) >= 5, f"HTTP {live_replay_cockpit.get('http_status')}, actions={len(live_replay_cockpit_action_items)}, axes={len(live_replay_cockpit_axis_progress)}"),
        _make_check("live_replay_cockpit_current_sync", live_replay_cockpit_summary.get("current_action_key") == "sync_broker_holding_price", f"current={live_replay_cockpit_summary.get('current_action_key')}, status={live_replay_cockpit_summary.get('current_action_status')}"),
        _make_check("live_replay_cockpit_locks_trade", not bool(live_replay_cockpit_summary.get("can_execute_trade")) and live_replay_cockpit_summary.get("profit_only_optimization_allowed") is False and live_replay_cockpit_summary.get("can_change_strategy_contract") is False and not bool(live_replay_cockpit_json.get("formal_buy_signal")) and not bool(live_replay_cockpit_json.get("auto_order_allowed")) and not bool(live_replay_cockpit_json.get("order_path_enabled")), f"can_execute={live_replay_cockpit_summary.get('can_execute_trade')}, profit_only={live_replay_cockpit_summary.get('profit_only_optimization_allowed')}, contract_change={live_replay_cockpit_summary.get('can_change_strategy_contract')}, formal={live_replay_cockpit_json.get('formal_buy_signal')}, auto={live_replay_cockpit_json.get('auto_order_allowed')}, path={live_replay_cockpit_json.get('order_path_enabled')}"),
        _make_check("live_replay_cockpit_checks_present", len(live_replay_cockpit_checks) >= 4, f"checks={len(live_replay_cockpit_checks)}"),
        _make_check("live_replay_cockpit_does_not_write_attempt", after_live_replay_cockpit_attempts == after_live_action_console_empty_review_attempts, f"after_empty_review={after_live_action_console_empty_review_attempts}, after_cockpit={after_live_replay_cockpit_attempts}"),
        _make_check("live_replay_cockpit_does_not_write_snapshot", after_live_replay_cockpit_snapshots == after_live_action_console_empty_review_snapshots, f"after_empty_review={after_live_action_console_empty_review_snapshots}, after_cockpit={after_live_replay_cockpit_snapshots}"),
        _make_check("live_replay_cockpit_does_not_write_review", after_live_replay_cockpit_reviews == after_live_action_console_empty_review_reviews, f"after_empty_review_reviews={after_live_action_console_empty_review_reviews}, after_cockpit_reviews={after_live_replay_cockpit_reviews}"),
        _make_check("historical_decision_replay_endpoint", bool(historical_decision_replay_json.get("ok")) and historical_decision_replay_json.get("mode") == "g3_historical_decision_replay_tasks" and len(historical_decision_replay_tasks) > 0, f"HTTP {historical_decision_replay.get('http_status')}, tasks={len(historical_decision_replay_tasks)}, trades={historical_decision_replay_summary.get('trade_count')}"),
        _make_check("historical_decision_replay_has_axes", bool((historical_decision_replay_summary.get("axis_counts") or {}).get("buy_point") or (historical_decision_replay_summary.get("axis_counts") or {}).get("sell_point") or (historical_decision_replay_summary.get("axis_counts") or {}).get("model_switch") or (historical_decision_replay_summary.get("axis_counts") or {}).get("selection")), f"axis_counts={historical_decision_replay_summary.get('axis_counts')}"),
        _make_check("historical_decision_replay_locks_trade", not bool(historical_decision_replay_summary.get("can_execute_trade")) and historical_decision_replay_summary.get("profit_only_optimization_allowed") is False and historical_decision_replay_summary.get("can_change_strategy_contract") is False and not bool(historical_decision_replay_json.get("formal_buy_signal")) and not bool(historical_decision_replay_json.get("auto_order_allowed")) and not bool(historical_decision_replay_json.get("order_path_enabled")), f"can_execute={historical_decision_replay_summary.get('can_execute_trade')}, profit_only={historical_decision_replay_summary.get('profit_only_optimization_allowed')}, contract_change={historical_decision_replay_summary.get('can_change_strategy_contract')}, formal={historical_decision_replay_json.get('formal_buy_signal')}, auto={historical_decision_replay_json.get('auto_order_allowed')}, path={historical_decision_replay_json.get('order_path_enabled')}"),
        _make_check("historical_decision_replay_does_not_write_attempt", after_historical_decision_replay_attempts == after_live_replay_cockpit_attempts, f"after_cockpit={after_live_replay_cockpit_attempts}, after_historical={after_historical_decision_replay_attempts}"),
        _make_check("historical_decision_replay_does_not_write_snapshot", after_historical_decision_replay_snapshots == after_live_replay_cockpit_snapshots, f"after_cockpit={after_live_replay_cockpit_snapshots}, after_historical={after_historical_decision_replay_snapshots}"),
        _make_check("historical_decision_replay_does_not_write_review", after_historical_decision_replay_reviews == after_live_replay_cockpit_reviews, f"after_cockpit_reviews={after_live_replay_cockpit_reviews}, after_historical_reviews={after_historical_decision_replay_reviews}"),
        _make_check("historical_decision_replay_audit_endpoint", bool(historical_decision_replay_audit_json.get("ok")) and historical_decision_replay_audit_json.get("mode") == "g3_historical_decision_replay_audit" and len(historical_decision_replay_axis_rows) >= 4, f"HTTP {historical_decision_replay_audit.get('http_status')}, status={historical_decision_replay_audit_payload.get('audit_status')}, axes={len(historical_decision_replay_axis_rows)}"),
        _make_check("historical_decision_replay_audit_locks_trade", not bool(historical_decision_replay_audit_payload.get("can_buy")) and not bool(historical_decision_replay_audit_summary.get("can_execute_trade")) and historical_decision_replay_audit_summary.get("profit_only_optimization_allowed") is False and historical_decision_replay_audit_summary.get("can_change_strategy_contract") is False and not bool(historical_decision_replay_audit_json.get("formal_buy_signal")) and not bool(historical_decision_replay_audit_json.get("auto_order_allowed")) and not bool(historical_decision_replay_audit_json.get("order_path_enabled")), f"can_buy={historical_decision_replay_audit_payload.get('can_buy')}, can_execute={historical_decision_replay_audit_summary.get('can_execute_trade')}, profit_only={historical_decision_replay_audit_summary.get('profit_only_optimization_allowed')}, contract_change={historical_decision_replay_audit_summary.get('can_change_strategy_contract')}, formal={historical_decision_replay_audit_json.get('formal_buy_signal')}, auto={historical_decision_replay_audit_json.get('auto_order_allowed')}, path={historical_decision_replay_audit_json.get('order_path_enabled')}"),
        _make_check("historical_decision_replay_audit_does_not_write_attempt", after_historical_decision_replay_audit_attempts == after_historical_decision_replay_attempts, f"after_tasks={after_historical_decision_replay_attempts}, after_audit={after_historical_decision_replay_audit_attempts}"),
        _make_check("historical_decision_replay_audit_does_not_write_snapshot", after_historical_decision_replay_audit_snapshots == after_historical_decision_replay_snapshots, f"after_tasks={after_historical_decision_replay_snapshots}, after_audit={after_historical_decision_replay_audit_snapshots}"),
        _make_check("historical_decision_replay_audit_does_not_write_review", after_historical_decision_replay_audit_reviews == after_historical_decision_replay_reviews, f"after_tasks_reviews={after_historical_decision_replay_reviews}, after_audit_reviews={after_historical_decision_replay_audit_reviews}"),
        _make_check("broker_post_sync_record_rejected_until_ready", broker_post_sync_record_json.get("ok") is False and broker_post_sync_record_json.get("error") == "post_sync_acceptance_not_ready", f"HTTP {broker_post_sync_record.get('http_status')}, error={broker_post_sync_record_json.get('error')}, status={broker_post_sync_record_acceptance.get('acceptance_status')}"),
        _make_check("broker_post_sync_record_locks_trade", not bool(broker_post_sync_record_json.get("formal_buy_signal")) and not bool(broker_post_sync_record_json.get("auto_order_allowed")) and not bool(broker_post_sync_record_json.get("order_path_enabled")), f"formal={broker_post_sync_record_json.get('formal_buy_signal')}, auto={broker_post_sync_record_json.get('auto_order_allowed')}, path={broker_post_sync_record_json.get('order_path_enabled')}"),
        _make_check("broker_post_sync_record_does_not_write_attempt", after_broker_post_sync_record_attempts == after_historical_decision_replay_audit_attempts, f"after_historical_audit={after_historical_decision_replay_audit_attempts}, after_record={after_broker_post_sync_record_attempts}"),
        _make_check("broker_post_sync_record_does_not_write_snapshot", after_broker_post_sync_record_snapshots == after_historical_decision_replay_audit_snapshots, f"after_historical_audit={after_historical_decision_replay_audit_snapshots}, after_record={after_broker_post_sync_record_snapshots}"),
        _make_check("broker_post_sync_record_does_not_write_review_when_blocked", after_broker_post_sync_record_reviews == after_historical_decision_replay_audit_reviews, f"after_historical_audit_reviews={after_historical_decision_replay_audit_reviews}, after_record_reviews={after_broker_post_sync_record_reviews}"),
        _make_check("empty_candidate_review_rejected", empty_json.get("ok") is False and "missing candidate omission review key" in str(empty_json.get("error", "")), str(empty_json.get("error"))),
        _make_check("empty_request_does_not_write_attempt", after_empty_attempts == before_attempts, f"before={before_attempts}, after_empty={after_empty_attempts}"),
        _make_check("empty_launch_day_playbook_review_rejected", empty_launch_playbook_json.get("ok") is False and "missing launch day playbook review key" in str(empty_launch_playbook_json.get("error", "")), str(empty_launch_playbook_json.get("error"))),
        _make_check("empty_launch_day_playbook_review_does_not_write_attempt", after_empty_launch_playbook_attempts == after_empty_attempts, f"after_empty={after_empty_attempts}, after_empty_playbook={after_empty_launch_playbook_attempts}"),
        _make_check("empty_launch_day_playbook_review_does_not_write_snapshot", after_empty_launch_playbook_snapshots == before_snapshots, f"before_snapshots={before_snapshots}, after_empty_playbook={after_empty_launch_playbook_snapshots}"),
        _make_check("execute_next_requires_confirm_or_manual_review", unconfirmed_ok, f"error={unconfirmed_error}"),
        _make_check("unconfirmed_execute_does_not_write_attempt", after_unconfirmed_execute_attempts == after_empty_launch_playbook_attempts, f"after_empty_playbook={after_empty_launch_playbook_attempts}, after_unconfirmed={after_unconfirmed_execute_attempts}"),
        _make_check("unconfirmed_execute_does_not_write_snapshot", after_unconfirmed_execute_snapshots == before_snapshots, f"before_snapshots={before_snapshots}, after_unconfirmed={after_unconfirmed_execute_snapshots}"),
        _make_check("wrong_action_group_rejected_as_stale", wrong_action_ok, f"error={wrong_action_error}, actual={wrong_action_json.get('actual_action_group')}, expected={wrong_action_json.get('expected_action_group')}"),
        _make_check("wrong_action_does_not_write_attempt", after_wrong_action_attempts == after_unconfirmed_execute_attempts, f"after_unconfirmed={after_unconfirmed_execute_attempts}, after_wrong_action={after_wrong_action_attempts}"),
        _make_check("wrong_action_does_not_write_snapshot", after_wrong_action_snapshots == after_unconfirmed_execute_snapshots, f"after_unconfirmed={after_unconfirmed_execute_snapshots}, after_wrong_action={after_wrong_action_snapshots}"),
        _make_check("readiness_run_ok", bool(readiness_json.get("ok")), f"HTTP {readiness.get('http_status')}, ok={readiness_json.get('ok')}"),
        _make_check("auto_order_locked", not auto_order_allowed, f"auto_order_allowed={auto_order_allowed}"),
        _make_check("order_path_locked", not order_path_enabled, f"order_path_enabled={order_path_enabled}"),
        _make_check("readiness_does_not_write_attempt", after_readiness_attempts == after_unconfirmed_execute_attempts, f"after_unconfirmed={after_unconfirmed_execute_attempts}, after_readiness={after_readiness_attempts}"),
        _make_check("readiness_does_not_write_snapshot", after_readiness_snapshots == after_unconfirmed_execute_snapshots, f"after_unconfirmed={after_unconfirmed_execute_snapshots}, after_readiness={after_readiness_snapshots}"),
        _make_check("live_launch_packet_endpoint", launch_packet.get("ok") and bool(launch_packet_payload), f"HTTP {launch_packet.get('http_status')}, status={launch_packet_payload.get('status')}"),
        _make_check("live_launch_packet_run_ok", bool(launch_packet_run_json.get("ok")) and bool(launch_packet_run_payload), f"HTTP {launch_packet_run.get('http_status')}, ok={launch_packet_run_json.get('ok')}, status={launch_packet_run_payload.get('status')}"),
        _make_check("live_launch_review_axis_matrix_present", bool(launch_packet_run_axis_rows or launch_packet_axis_rows), f"axis_rows={len(launch_packet_run_axis_rows or launch_packet_axis_rows)}"),
        _make_check("live_launch_day_playbook_present", bool(launch_packet_run_playbook_rows or launch_packet_playbook_rows), f"playbook_rows={len(launch_packet_run_playbook_rows or launch_packet_playbook_rows)}"),
        _make_check(
            "live_launch_learning_queue_shape",
            "launch_day_learning_queue" in launch_packet_run_json or "launch_day_learning_queue" in launch_packet_json,
            f"learning_rows={len(launch_packet_run_learning_rows or launch_packet_learning_rows)}",
        ),
        _make_check("launch_packet_does_not_write_attempt", after_launch_packet_run_attempts == after_readiness_attempts, f"after_readiness={after_readiness_attempts}, after_packet_read={after_launch_packet_read_attempts}, after_packet_run={after_launch_packet_run_attempts}"),
        _make_check("launch_packet_does_not_write_snapshot", after_launch_packet_run_snapshots == after_readiness_snapshots, f"after_readiness={after_readiness_snapshots}, after_packet_read={after_launch_packet_read_snapshots}, after_packet_run={after_launch_packet_run_snapshots}"),
        _make_check(
            "manual_admission_observed",
            _summary_value(readiness, "live_admission_buy_allowed") in (True, False),
            f"live_admission_buy_allowed={_summary_value(readiness, 'live_admission_buy_allowed')}",
            severity="watch",
        ),
    ]

    blocking_failed = [x for x in checks if x["severity"] == "block" and not x["passed"]]
    status = "pass" if not blocking_failed else "fail"
    result = {
        "version": "g3_live_launch_smoke_v1",
        "generated_at": _now_text(),
        "base_url": BASE_URL,
        "status": status,
        "formal_buy_signal": formal_buy_signal,
        "auto_order_allowed": auto_order_allowed,
        "order_path_enabled": order_path_enabled,
        "attempt_count_before": before_attempts,
        "snapshot_count_before": before_snapshots,
        "attempt_count_after_empty": after_empty_attempts,
        "attempt_count_after_empty_launch_playbook": after_empty_launch_playbook_attempts,
        "snapshot_count_after_empty_launch_playbook": after_empty_launch_playbook_snapshots,
        "broker_sync_preflight": {
            "preflight_status": broker_preflight_json.get("preflight_status"),
            "can_prompt_manual_sync": broker_preflight_json.get("can_prompt_manual_sync"),
            "gateway_configured": broker_preflight_json.get("gateway_configured"),
            "current_action_group": broker_preflight_json.get("current_action_group"),
            "holdings_count": broker_preflight_json.get("holdings_count"),
            "broker_staleness_minutes": broker_preflight_json.get("broker_staleness_minutes"),
            "blockers": broker_preflight_json.get("blockers"),
            "warnings": broker_preflight_json.get("warnings"),
        },
        "broker_sync_confirmation_packet": {
            "packet_status": broker_confirmation_json.get("packet_status"),
            "safe_to_prompt": broker_confirmation_json.get("safe_to_prompt"),
            "requires_confirmation": broker_confirmation_json.get("requires_confirmation"),
            "action_fingerprint": broker_confirmation_fingerprint,
            "expected_request": broker_confirmation_json.get("expected_request"),
            "blockers": broker_confirmation_json.get("blockers"),
            "warnings": broker_confirmation_json.get("warnings"),
        },
        "broker_sync_outcome": {
            "outcome_status": broker_outcome_payload.get("outcome_status"),
            "outcome_label": broker_outcome_payload.get("outcome_label"),
            "can_enter_live_manual_review": broker_outcome_payload.get("can_enter_live_manual_review"),
            "can_buy": broker_outcome_payload.get("can_buy"),
            "next_action": broker_outcome_payload.get("next_action"),
            "sync_attempt_count": broker_outcome_json.get("sync_attempt_count"),
            "broker": broker_outcome_json.get("broker"),
            "blocker_summary": broker_outcome_json.get("blocker_summary"),
        },
        "broker_post_sync_acceptance": {
            "acceptance_status": broker_post_sync_acceptance_payload.get("acceptance_status"),
            "can_record_execution_evidence": broker_post_sync_acceptance_payload.get("can_record_execution_evidence"),
            "manual_live_review_allowed": broker_post_sync_acceptance_payload.get("manual_live_review_allowed"),
            "sync_attempt_count": broker_post_sync_acceptance_json.get("sync_attempt_count"),
            "broker": broker_post_sync_acceptance_json.get("broker"),
            "gap_count": len(broker_post_sync_acceptance_json.get("gaps") if isinstance(broker_post_sync_acceptance_json.get("gaps"), list) else []),
        },
        "live_action_console": {
            "console_status": live_action_console_summary.get("console_status"),
            "current_action_key": live_action_console_summary.get("current_action_key"),
            "current_action_status": live_action_console_summary.get("current_action_status"),
            "ready_step_count": live_action_console_summary.get("ready_step_count"),
            "done_step_count": live_action_console_summary.get("done_step_count"),
            "blocking_step_count": live_action_console_summary.get("blocking_step_count"),
            "step_count": len(live_action_console_steps),
        },
        "live_replay_cockpit": {
            "cockpit_status": live_replay_cockpit_summary.get("cockpit_status"),
            "current_action_key": live_replay_cockpit_summary.get("current_action_key"),
            "next_review_axis": live_replay_cockpit_summary.get("next_review_axis"),
            "todo_count": live_replay_cockpit_summary.get("todo_count"),
            "followup_count": live_replay_cockpit_summary.get("followup_count"),
            "watch_count": live_replay_cockpit_summary.get("watch_count"),
            "action_item_count": len(live_replay_cockpit_action_items),
            "axis_progress_count": len(live_replay_cockpit_axis_progress),
            "check_count": len(live_replay_cockpit_checks),
        },
        "historical_decision_replay": {
            "task_count": historical_decision_replay_summary.get("task_count"),
            "trade_count": historical_decision_replay_summary.get("trade_count"),
            "loss_trade_count": historical_decision_replay_summary.get("loss_trade_count"),
            "hard_stop_count": historical_decision_replay_summary.get("hard_stop_count"),
            "axis_counts": historical_decision_replay_summary.get("axis_counts"),
            "formal_buy_signal": historical_decision_replay_json.get("formal_buy_signal"),
            "auto_order_allowed": historical_decision_replay_json.get("auto_order_allowed"),
            "order_path_enabled": historical_decision_replay_json.get("order_path_enabled"),
        },
        "historical_decision_replay_audit": {
            "audit_status": historical_decision_replay_audit_payload.get("audit_status"),
            "primary_gap": historical_decision_replay_audit_payload.get("primary_gap"),
            "required_axis_count": historical_decision_replay_audit_summary.get("required_axis_count"),
            "blocked_axis_count": historical_decision_replay_audit_summary.get("blocked_axis_count"),
            "todo_count": historical_decision_replay_audit_summary.get("todo_count"),
            "followup_count": historical_decision_replay_audit_summary.get("followup_count"),
            "reviewed_count": historical_decision_replay_audit_summary.get("reviewed_count"),
            "axis_row_count": len(historical_decision_replay_axis_rows),
            "formal_buy_signal": historical_decision_replay_audit_json.get("formal_buy_signal"),
            "auto_order_allowed": historical_decision_replay_audit_json.get("auto_order_allowed"),
            "order_path_enabled": historical_decision_replay_audit_json.get("order_path_enabled"),
        },
        "broker_post_sync_record": {
            "ok": broker_post_sync_record_json.get("ok"),
            "error": broker_post_sync_record_json.get("error"),
            "acceptance_status": broker_post_sync_record_acceptance.get("acceptance_status"),
            "formal_buy_signal": broker_post_sync_record_json.get("formal_buy_signal"),
            "auto_order_allowed": broker_post_sync_record_json.get("auto_order_allowed"),
            "order_path_enabled": broker_post_sync_record_json.get("order_path_enabled"),
            "review_count_before": after_broker_post_sync_acceptance_reviews,
            "review_count_after": after_broker_post_sync_record_reviews,
        },
        "live_launch_readiness_audit": {
            "audit_status": live_readiness_audit_payload.get("audit_status"),
            "audit_label": live_readiness_audit_payload.get("audit_label"),
            "can_enter_live_manual_review": live_readiness_audit_payload.get("can_enter_live_manual_review"),
            "can_buy": live_readiness_audit_payload.get("can_buy"),
            "primary_blocker": live_readiness_audit_payload.get("primary_blocker"),
            "next_action": live_readiness_audit_payload.get("next_action"),
            "summary": live_readiness_audit_json.get("summary"),
        },
        "strategy_tuning_axis_board": {
            "axis_count": len(strategy_tuning_axis_rows),
            "risk_item_count": strategy_tuning_summary.get("risk_item_count"),
            "needs_review_count": strategy_tuning_summary.get("needs_review_count"),
            "profit_only_optimization_allowed": strategy_tuning_summary.get("profit_only_optimization_allowed"),
            "can_change_strategy_contract": strategy_tuning_summary.get("can_change_strategy_contract"),
            "live_admission_status": strategy_tuning_summary.get("live_admission_status"),
            "axis_counts": strategy_tuning_summary.get("axis_counts"),
        },
        "strategy_tuning_review_queue": {
            "axis_count": len(strategy_queue_axis_rows),
            "task_count": strategy_queue_summary.get("task_count"),
            "todo_count": strategy_queue_summary.get("todo_count"),
            "reviewed_count": strategy_queue_after_save_summary.get("reviewed_count", strategy_queue_summary.get("reviewed_count")),
            "followup_count": strategy_queue_after_save_summary.get("followup_count", strategy_queue_summary.get("followup_count")),
            "watch_count": strategy_queue_summary.get("watch_count"),
            "review_count": strategy_queue_after_save_summary.get("review_count", strategy_queue_summary.get("review_count")),
            "profit_only_optimization_allowed": strategy_queue_summary.get("profit_only_optimization_allowed"),
            "can_change_strategy_contract": strategy_queue_summary.get("can_change_strategy_contract"),
            "can_execute_trade": strategy_queue_summary.get("can_execute_trade"),
        },
        "strategy_tuning_completion_audit": {
            "audit_status": (strategy_completion_after_save_json.get("audit") or strategy_completion_audit_payload).get("audit_status") if isinstance(strategy_completion_after_save_json.get("audit") or strategy_completion_audit_payload, dict) else None,
            "audit_label": (strategy_completion_after_save_json.get("audit") or strategy_completion_audit_payload).get("audit_label") if isinstance(strategy_completion_after_save_json.get("audit") or strategy_completion_audit_payload, dict) else None,
            "axis_count": len(strategy_completion_axis_rows),
            "task_count": strategy_completion_after_save_summary.get("task_count", strategy_completion_summary.get("task_count")),
            "todo_count": strategy_completion_after_save_summary.get("todo_count", strategy_completion_summary.get("todo_count")),
            "followup_count": strategy_completion_after_save_summary.get("followup_count", strategy_completion_summary.get("followup_count")),
            "watch_count": strategy_completion_after_save_summary.get("watch_count", strategy_completion_summary.get("watch_count")),
            "reviewed_count": strategy_completion_after_save_summary.get("reviewed_count", strategy_completion_summary.get("reviewed_count")),
            "review_count": strategy_completion_after_save_summary.get("review_count", strategy_completion_summary.get("review_count")),
            "hard_gap_count": strategy_completion_after_save_summary.get("hard_gap_count", strategy_completion_summary.get("hard_gap_count")),
            "watch_gap_count": strategy_completion_after_save_summary.get("watch_gap_count", strategy_completion_summary.get("watch_gap_count")),
            "profit_only_optimization_allowed": strategy_completion_summary.get("profit_only_optimization_allowed"),
            "can_change_strategy_contract": strategy_completion_summary.get("can_change_strategy_contract"),
            "can_execute_trade": strategy_completion_summary.get("can_execute_trade"),
        },
        "strategy_tuning_replay_suggestions": {
            "suggestion_count": strategy_suggestions_summary.get("suggestion_count"),
            "open_task_count": strategy_suggestions_summary.get("open_task_count"),
            "result_counts": strategy_suggestions_summary.get("result_counts"),
            "axis_counts": strategy_suggestions_summary.get("axis_counts"),
            "profit_only_optimization_allowed": strategy_suggestions_summary.get("profit_only_optimization_allowed"),
            "can_change_strategy_contract": strategy_suggestions_summary.get("can_change_strategy_contract"),
            "can_execute_trade": strategy_suggestions_summary.get("can_execute_trade"),
        },
        "strategy_tuning_replay_session": {
            "session_status": strategy_session_payload.get("session_status"),
            "audit_status": strategy_session_payload.get("audit_status"),
            "progress_pct": strategy_session_summary.get("progress_pct"),
            "remaining_count": strategy_session_summary.get("remaining_count"),
            "session_step_count": strategy_session_summary.get("session_step_count"),
            "current_step": strategy_session_payload.get("current_step"),
            "profit_only_optimization_allowed": strategy_session_summary.get("profit_only_optimization_allowed"),
            "can_change_strategy_contract": strategy_session_summary.get("can_change_strategy_contract"),
            "can_execute_trade": strategy_session_summary.get("can_execute_trade"),
        },
        "strategy_tuning_current_step_completion": {
            "completion_status": strategy_step_completion_packet.get("completion_status"),
            "gap_count": len(strategy_step_completion_gaps),
            "gaps": strategy_step_completion_gaps,
            "can_mark_step_done": strategy_step_completion_packet.get("can_mark_step_done"),
            "manual_live_review_allowed": strategy_step_completion_packet.get("manual_live_review_allowed"),
            "after_done_check": strategy_step_completion_packet.get("after_done_check"),
        },
        "strategy_tuning_task_review_save": {
            "ok": strategy_review_save_json.get("ok"),
            "skipped": strategy_review_save_json.get("skipped"),
            "key": strategy_review_save_json.get("key"),
            "review_count_before": before_tuning_reviews,
            "review_count_after": after_strategy_review_save_count,
            "endpoint_count_after": strategy_task_reviews_after_json.get("count"),
            "formal_buy_signal": strategy_review_save_json.get("formal_buy_signal"),
            "auto_order_allowed": strategy_review_save_json.get("auto_order_allowed"),
            "order_path_enabled": strategy_review_save_json.get("order_path_enabled"),
        },
        "attempt_count_after_unconfirmed_execute": after_unconfirmed_execute_attempts,
        "snapshot_count_after_unconfirmed_execute": after_unconfirmed_execute_snapshots,
        "attempt_count_after_wrong_action": after_wrong_action_attempts,
        "snapshot_count_after_wrong_action": after_wrong_action_snapshots,
        "attempt_count_after_readiness": after_readiness_attempts,
        "snapshot_count_after_readiness": after_readiness_snapshots,
        "attempt_count_after_launch_packet_read": after_launch_packet_read_attempts,
        "snapshot_count_after_launch_packet_read": after_launch_packet_read_snapshots,
        "attempt_count_after_launch_packet_run": after_launch_packet_run_attempts,
        "snapshot_count_after_launch_packet_run": after_launch_packet_run_snapshots,
        "premarket_command": command,
        "live_launch_decision": live_decision,
        "live_blocker_evidence_board_summary": live_blocker_board_json.get("summary") if isinstance(live_blocker_board_json.get("summary"), dict) else {},
        "launch_packet_summary": {
            "status": launch_packet_run_payload.get("status") or launch_packet_payload.get("status"),
            "review_axis_count": len(launch_packet_run_axis_rows or launch_packet_axis_rows),
            "launch_day_playbook_count": len(launch_packet_run_playbook_rows or launch_packet_playbook_rows),
            "launch_day_learning_queue_count": len(launch_packet_run_learning_rows or launch_packet_learning_rows),
            "live_admission_status": _packet_summary_value(launch_packet_run_payload, "live_admission_status", _packet_summary_value(launch_packet_payload, "live_admission_status")),
            "live_admission_buy_allowed": _packet_summary_value(launch_packet_run_payload, "live_admission_buy_allowed", _packet_summary_value(launch_packet_payload, "live_admission_buy_allowed")),
            "live_premarket_next_action": _packet_summary_value(launch_packet_run_payload, "live_premarket_next_action", _packet_summary_value(launch_packet_payload, "live_premarket_next_action")),
            "formal_launch_ready": _packet_summary_value(launch_packet_run_payload, "formal_launch_ready", _packet_summary_value(launch_packet_payload, "formal_launch_ready")),
            "live_manual_launch_ready": _packet_summary_value(launch_packet_run_payload, "live_manual_launch_ready", _packet_summary_value(launch_packet_payload, "live_manual_launch_ready")),
        },
        "readiness_summary": {
            "generated_at": summary.get("generated_at"),
            "verdict": summary.get("verdict"),
            "next_trade_entry_date": summary.get("next_trade_entry_date"),
            "next_trade_ticket_count": summary.get("next_trade_ticket_count"),
            "candidate_count": summary.get("candidate_count"),
            "real_holding_count": summary.get("real_holding_count"),
            "holding_formal_refresh_pending_count": summary.get("holding_formal_refresh_pending_count"),
            "candidate_omission_pending_count": summary.get("candidate_omission_pending_count"),
            "pretrade_action_pending_count": summary.get("pretrade_action_pending_count"),
            "pretrade_formal_action_pending_count": summary.get("pretrade_formal_action_pending_count"),
            "formal_launch_ready": summary.get("formal_launch_ready"),
            "formal_launch_status": summary.get("formal_launch_status"),
            "first_live_decision_status": summary.get("first_live_decision_status"),
            "first_live_execution_posture": summary.get("first_live_execution_posture"),
            "live_admission_status": summary.get("live_admission_status"),
            "live_admission_buy_allowed": summary.get("live_admission_buy_allowed"),
            "live_admission_blocking_command_count": summary.get("live_admission_blocking_command_count"),
            "live_premarket_next_action": summary.get("live_premarket_next_action"),
            "live_premarket_action_attempt_count": summary.get("live_premarket_action_attempt_count"),
            "live_manual_launch_ready": summary.get("live_manual_launch_ready"),
            "strategy_learning_backlog_count": summary.get("strategy_learning_backlog_count"),
        },
        "checks": checks,
        "artifacts": {
            "summary_json": str(OUT_DIR / "summary.json"),
            "summary_cn": str(OUT_DIR / "SUMMARY_CN.md"),
            "attempts_path": str(ATTEMPTS_PATH),
            "snapshots_path": str(SNAPSHOTS_PATH),
        },
    }
    _write_outputs(result)
    return result


def main() -> int:
    result = run_smoke()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
