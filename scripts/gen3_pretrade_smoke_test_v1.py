from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from strategies.g3.confirmation import is_confirmation_data_wall  # noqa: E402
from strategies.g3.minute_visibility import load_visible_minute_bars  # noqa: E402
from utils.paths import report_path, runtime_path  # noqa: E402


STATE_ALPHA_RUNTIME_DIR = runtime_path("gen3_state_alpha")
STATE_ROUTER_RUNTIME_DIR = runtime_path("gen3_state_router_shadow")
OUT_DIR = report_path("gen3_pretrade_smoke_test_v1")
SHADOW_SCRIPT = ROOT / "scripts" / "gen3_state_router_shadow_daily_v1.py"
PROMOTION_DIR = report_path("gen3_promotion_self_test_v1")
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
FIRST_COMPLETED_30M_TIME = (10, 0)


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Path):
        return str(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "ok"}


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        x = float(value)
    except Exception:
        return default
    return x if pd.notna(x) else default


def _date_text(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def _dt_text(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d %H:%M:%S")


def _artifact(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path), "size_bytes": None, "modified_at": None}
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


class GateBook:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, gate: str, ok: bool, severity: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.rows.append(
            {
                "gate": gate,
                "ok": bool(ok),
                "severity": severity,
                "status": "pass" if ok else ("blocked" if severity == "block" else "warn"),
                "message": message,
                "detail": detail or {},
            }
        )

    def blockers(self) -> list[dict[str, Any]]:
        return [x for x in self.rows if not x["ok"] and x["severity"] == "block"]

    def warnings(self) -> list[dict[str, Any]]:
        return [x for x in self.rows if not x["ok"] and x["severity"] == "warn"]


def _run_shadow_refresh(entry_date: str | None) -> dict[str, Any]:
    cmd = [sys.executable, str(SHADOW_SCRIPT)]
    if entry_date:
        cmd.extend(["--entry-date", entry_date])
    started = datetime.now()
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=900)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
        "command": " ".join(cmd),
    }


def _latest_daily_date() -> dict[str, Any]:
    try:
        df = clickhouse_query_df("SELECT max(trade_date) AS latest_trade_date, count() AS rows FROM kline_daily")
        if df.empty:
            return {"ok": False, "reason": "empty_result"}
        return {"ok": True, "latest_trade_date": _date_text(df.iloc[0].get("latest_trade_date")), "rows": int(df.iloc[0].get("rows") or 0)}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _minute30_coverage(tickets: pd.DataFrame, decision_date: str) -> list[dict[str, Any]]:
    if tickets.empty:
        return []
    rows: list[dict[str, Any]] = []
    for item in tickets.to_dict("records"):
        code = str(item.get("code") or "").strip()
        confirm_date = _date_text(item.get("confirm_datetime")) or decision_date
        if not code or not confirm_date:
            rows.append({"code": code, "date": confirm_date, "ok": False, "reason": "missing_code_or_confirm_date"})
            continue
        try:
            bars, visibility = load_visible_minute_bars(
                code,
                30,
                confirm_date,
                confirm_date,
            )
            count = int(len(bars))
            latest = (
                pd.Timestamp(bars["business_datetime"].max()).strftime("%Y-%m-%d %H:%M:%S")
                if not bars.empty
                else ""
            )
            rows.append(
                {
                    "code": code,
                    "date": confirm_date,
                    "ok": bool(visibility.get("source_ok")) and count > 0 and not visibility.get("conflict_rows"),
                    "rows": count,
                    "latest_datetime": latest,
                    "source": visibility.get("source", ""),
                    "visibility_status": visibility.get("status", ""),
                    "main_rows": int(visibility.get("main_rows") or 0),
                    "stage_rows": int(visibility.get("stage_rows") or 0),
                    "recovered_rows": int(visibility.get("recovered_rows") or 0),
                    "conflict_rows": int(visibility.get("conflict_rows") or 0),
                }
            )
        except Exception as exc:
            rows.append({"code": code, "date": confirm_date, "ok": False, "reason": str(exc)})
    return rows


def _promotion_gate_status() -> dict[str, Any]:
    gates_path = PROMOTION_DIR / "gates.csv"
    gates = _read_csv(gates_path)
    if gates.empty:
        return {"ok": False, "reason": "missing_or_empty_gates_csv", "path": str(gates_path)}
    pass_col = gates.get("pass")
    if pass_col is None:
        return {"ok": False, "reason": "missing_pass_column", "path": str(gates_path)}
    ok = pass_col.map(_truthy).all()
    return {
        "ok": bool(ok),
        "path": str(gates_path),
        "total": int(len(gates)),
        "failed": gates.loc[~pass_col.map(_truthy), "gate"].astype(str).tolist() if not ok else [],
    }


def _contract_checks(contract: dict[str, Any]) -> dict[str, Any]:
    portfolio = contract.get("portfolio_contract") or {}
    exit_contract = contract.get("exit_contract") or {}
    hard_stop = exit_contract.get("hard_stop") or {}
    take_profit = exit_contract.get("take_profit") or {}
    risk_limits = exit_contract.get("risk_limits") or {}
    checks = {
        "slots": int(portfolio.get("slots") or 0) == 2,
        "slot_pct": abs(float(portfolio.get("slot_pct") or 0) - 0.50) < 1e-9,
        "institutional_position": abs(float(portfolio.get("institutional_mainwave_position_pct") or 0) - 0.50) < 1e-9,
        "hard_stop": abs(float(hard_stop.get("loss_pct") or 0) - 0.10) < 1e-9 and hard_stop.get("action") == "sell_all",
        "take_profit": abs(float(take_profit.get("profit_pct") or 0) - 0.10) < 1e-9 and abs(float(take_profit.get("sell_ratio") or 0) - 0.50) < 1e-9,
        "max_single_loss": float(risk_limits.get("max_single_trade_account_loss_pct") or 9) <= 0.065,
        "formal_disabled": not _truthy((contract.get("guardrails") or {}).get("formal_buy_signal"))
        and not _truthy((contract.get("guardrails") or {}).get("auto_order_allowed"))
        and not _truthy((contract.get("guardrails") or {}).get("order_path_enabled")),
    }
    return {"ok": all(checks.values()), "checks": checks, "portfolio_contract": portfolio, "exit_contract": exit_contract}


def _ticket_contract_issues(tickets: pd.DataFrame) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for item in tickets.to_dict("records"):
        code = str(item.get("code") or "")
        route = str(item.get("route") or "")
        ref = _safe_float(item.get("reference_close"))
        position = _safe_float(item.get("position_pct"))
        slots = int(_safe_float(item.get("portfolio_slot_count"), 0) or 0)
        hard = _safe_float(item.get("hard_stop"))
        take = _safe_float(item.get("take_profit_1"))
        structure = _safe_float(item.get("structure_stop"))
        sell_ratio = _safe_float(item.get("take_profit_1_sell_ratio"))
        row_issues: list[str] = []
        if route == "institutional_mainwave":
            if position is None or abs(position - 0.50) > 1e-9:
                row_issues.append("position_pct_not_50pct")
            if slots != 2:
                row_issues.append("portfolio_slot_count_not_2")
            if ref is None or ref <= 0:
                row_issues.append("missing_reference_close")
            else:
                if hard is None or abs(hard - ref * 0.90) / ref > 0.003:
                    row_issues.append("hard_stop_not_near_minus_10pct")
                if take is None or abs(take - ref * 1.10) / ref > 0.003:
                    row_issues.append("take_profit_not_near_plus_10pct")
                if structure is None or structure <= 0 or structure > ref:
                    row_issues.append("structure_stop_invalid")
            if sell_ratio is None or abs(sell_ratio - 0.50) > 1e-9:
                row_issues.append("take_profit_sell_ratio_not_half")
        if row_issues:
            issues.append({"ticket_key": item.get("ticket_key"), "code": code, "route": route, "issues": row_issues})
    return issues


def _latest_ledger_rows(ledger: pd.DataFrame) -> pd.DataFrame:
    if ledger.empty or "ticket_key" not in ledger.columns:
        return pd.DataFrame()
    df = ledger.copy()
    df["_created_sort"] = pd.to_datetime(df.get("created_at"), errors="coerce")
    df = df.sort_values(["ticket_key", "_created_sort"]).drop_duplicates(subset=["ticket_key"], keep="last")
    return df.drop(columns=["_created_sort"], errors="ignore")


def _ledger_status(tickets: pd.DataFrame, ledger: pd.DataFrame) -> dict[str, Any]:
    latest = _latest_ledger_rows(ledger)
    ticket_keys = set(tickets.get("ticket_key", pd.Series(dtype=str)).dropna().astype(str)) if not tickets.empty else set()
    ledger_keys = set(latest.get("ticket_key", pd.Series(dtype=str)).dropna().astype(str)) if not latest.empty else set()
    missing = sorted(ticket_keys - ledger_keys)
    active_states = {"planned", "open_shadow", "filled", "partial", "partial_taken"}
    if latest.empty:
        active = pd.DataFrame()
    else:
        status_col = latest.get("last_state", latest.get("trade_status", pd.Series("", index=latest.index))).astype(str)
        active = latest[status_col.isin(active_states)].copy()
    exposure = float(pd.to_numeric(active.get("position_pct"), errors="coerce").fillna(0).sum()) if not active.empty else 0.0
    return {
        "ok": not missing and len(active) <= 2 and exposure <= 1.000001,
        "missing_ticket_keys": missing,
        "active_count": int(len(active)),
        "active_exposure": exposure,
        "active_codes": active.get("code", pd.Series(dtype=str)).astype(str).tolist() if not active.empty else [],
    }


def _summary_source_data_walls(summary: dict[str, Any], *, live_confirmation_window: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in summary.get("source_data_wall_rows") or []:
        if isinstance(item, dict):
            rows.append(item)
    for meta in summary.get("sources") or []:
        if not isinstance(meta, dict):
            continue
        source = str(meta.get("source") or "")
        status = str(meta.get("status") or "").strip().lower()
        if status == "retired_from_runtime":
            continue
        reason = ""
        if status in {"failed", "error", "builder_failed", "source_failed"}:
            reason = status
        snapshot = meta.get("candidate_snapshot") if isinstance(meta.get("candidate_snapshot"), dict) else {}
        snapshot_status = str(snapshot.get("status") or "").strip().lower()
        snapshot_write = snapshot.get("write") if isinstance(snapshot.get("write"), dict) else {}
        snapshot_write_status = str(snapshot_write.get("status") or "").strip().lower()
        if snapshot_status in {"snapshot_read_failed", "snapshot_empty_or_invalid"}:
            reason = snapshot_status
        if snapshot_write_status == "snapshot_write_failed":
            reason = snapshot_write_status
        if live_confirmation_window and bool(meta.get("candidate_snapshot_required")) and not bool(meta.get("candidate_snapshot_loaded")):
            reason = snapshot_status or "candidate_snapshot_required_not_loaded"
        if not reason:
            continue
        rows.append(
            {
                "source": source,
                "status": status,
                "reason": reason,
                "rows": int(meta.get("rows") or 0),
                "candidate_snapshot_required": bool(meta.get("candidate_snapshot_required")),
                "candidate_snapshot_loaded": bool(meta.get("candidate_snapshot_loaded")),
                "candidate_snapshot_status": snapshot_status,
            }
        )
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in rows:
        key = (str(item.get("source") or ""), str(item.get("status") or ""), str(item.get("reason") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _write_report(payload: dict[str, Any]) -> None:
    lines = [
        "# G3 Pretrade Smoke Test v1",
        "",
        f"- Generated at: `{payload.get('generated_at')}`",
        f"- Verdict: `{payload.get('verdict')}`",
        f"- Entry date: `{payload.get('entry_date') or '--'}`",
        f"- Decision date: `{payload.get('decision_date') or '--'}`",
        f"- Trade action: `{payload.get('trade_action')}`",
        "",
        "## Gates",
        "",
        "| Gate | Status | Severity | Message |",
        "|---|---:|---:|---|",
    ]
    for gate in payload.get("gates") or []:
        lines.append(f"| {gate.get('gate')} | {gate.get('status')} | {gate.get('severity')} | {gate.get('message')} |")
    lines.extend(["", "## Qualified Tickets", "", "| Code | Name | Route | Position | Ref | Hard Stop | Take Profit | 30m |", "|---|---|---|---:|---:|---:|---:|---:|"])
    for item in payload.get("qualified_tickets") or []:
        lines.append(
            "| {code} | {name} | {route} | {position_pct:.1%} | {reference_close:.2f} | {hard_stop:.2f} | {take_profit_1:.2f} | {m30} |".format(
                code=item.get("code") or "",
                name=item.get("name") or "",
                route=item.get("route") or "",
                position_pct=float(item.get("position_pct") or 0),
                reference_close=float(item.get("reference_close") or 0),
                hard_stop=float(item.get("hard_stop") or 0),
                take_profit_1=float(item.get("take_profit_1") or 0),
                m30="YES" if _truthy(item.get("m30_confirmed")) else "NO",
            )
        )
    lines.extend(["", "## Notes", "", "- `shadow_ready` means the system is ready for shadow/manual review, not automatic order routing.", "- If `trade_action=no_trade`, the chain is healthy but today's strategy contract has no qualified buy."])
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def run(entry_date: str | None = None, refresh: bool = False) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    refresh_result = _run_shadow_refresh(entry_date) if refresh else {"ok": None, "skipped": True}

    summary_path = STATE_ALPHA_RUNTIME_DIR / "latest_summary.json"
    tickets_path = STATE_ALPHA_RUNTIME_DIR / "latest_shadow_tickets.csv"
    ledger_path = STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"
    contract_path = STATE_ALPHA_RUNTIME_DIR / "latest_strategy_contract.json"
    diagnostics_path = STATE_ROUTER_RUNTIME_DIR / "latest_route_diagnostics.csv"
    all_source_path = STATE_ROUTER_RUNTIME_DIR / "latest_all_source_candidates.csv"

    summary = _read_json(summary_path)
    contract = _read_json(contract_path)
    tickets = _read_csv(tickets_path)
    ledger = _read_csv(ledger_path)
    diagnostics = _read_csv(diagnostics_path)
    all_source = _read_csv(all_source_path)
    qualified = tickets[tickets.get("qualified_shadow_buy", pd.Series(False, index=tickets.index)).map(_truthy)].copy() if not tickets.empty else pd.DataFrame()

    gate = GateBook()
    gate.add("shadow_refresh", bool(refresh_result.get("ok")) if refresh else True, "block", "G3 shadow refresh completed" if refresh else "refresh skipped by caller", refresh_result)
    artifacts = {name: _artifact(path) for name, path in {"summary": summary_path, "tickets": tickets_path, "ledger": ledger_path, "contract": contract_path}.items()}
    gate.add("runtime_artifacts", all(x["exists"] and (x["size_bytes"] or 0) > 0 for x in artifacts.values()), "block", "runtime artifacts exist and are non-empty", artifacts)
    gate.add("summary_parse", bool(summary), "block", "latest_summary.json is readable", {"path": str(summary_path)})
    gate.add("contract_parse", bool(contract), "block", "latest_strategy_contract.json is readable", {"path": str(contract_path)})
    account_risk = summary.get("account_risk") or {}
    account_action = str(account_risk.get("account_risk_action") or "normal")
    gate.add(
        "account_risk_gate",
        account_action != "pause_new_buy",
        "block",
        "account-level drawdown/cooldown gate must allow new buys",
        account_risk or {"account_risk_reason": "missing_account_risk_summary"},
    )
    selected_route = str(summary.get("selected_route") or "")
    if selected_route and not diagnostics.empty and "route" in diagnostics.columns:
        route_row = diagnostics[diagnostics["route"].astype(str).eq(selected_route)].copy()
        route_health_ok = (not route_row.empty) and _truthy(route_row.iloc[0].get("route_health_ok", True))
        route_health_detail = route_row.iloc[0].to_dict() if not route_row.empty else {"selected_route": selected_route, "reason": "missing_route_diagnostics_row"}
    else:
        route_health_ok = True
        route_health_detail = {"selected_route": selected_route, "reason": "no_selected_route" if not selected_route else "missing_diagnostics"}
    gate.add("selected_route_health_observation", route_health_ok, "warn", "selected route rolling health is observation-only", route_health_detail)

    entry = str(summary.get("entry_date") or "")
    decision = str(summary.get("decision_date") or "")
    daily = _latest_daily_date()
    gate.add("clickhouse_daily_fresh", bool(daily.get("ok")) and (not decision or str(daily.get("latest_trade_date") or "") >= decision), "block", "kline_daily covers decision date", daily)
    minute_exists = clickhouse_table_exists("kline_minute_30")
    minute_rows = _minute30_coverage(qualified, decision)
    gate.add("clickhouse_minute30_table", minute_exists, "block", "kline_minute_30 table exists", {"exists": minute_exists})
    gate.add("qualified_ticket_30m_confirmed", qualified.empty or qualified.get("m30_confirmed", pd.Series(False, index=qualified.index)).map(_truthy).all(), "block", "qualified tickets must have 30m confirmation", {"qualified_count": int(len(qualified))})
    gate.add("qualified_ticket_30m_data", not minute_rows or all(x.get("ok") for x in minute_rows), "block", "qualified tickets have 30m rows on confirm date", {"coverage": minute_rows})

    live_confirmation_window = bool(entry and summary.get("live_confirmation_window"))
    if not live_confirmation_window:
        now = datetime.now(BUSINESS_TZ)
        live_confirmation_window = bool(
            entry
            and now.strftime("%Y-%m-%d") == entry
            and (now.hour, now.minute) >= FIRST_COMPLETED_30M_TIME
        )
    summary_source_failures = _summary_source_data_walls(summary, live_confirmation_window=live_confirmation_window)
    source_failures: list[dict[str, Any]] = []
    if not all_source.empty:
        for item in all_source.to_dict("records"):
            m30_status = str(item.get("m30_status") or "")
            visibility_status = str(item.get("m30_visibility_status") or "")
            if is_confirmation_data_wall(m30_status) or is_confirmation_data_wall(visibility_status):
                source_failures.append(
                    {
                        "code": item.get("code") or item.get("code_raw") or "",
                        "m30_status": m30_status,
                        "m30_visibility_status": visibility_status,
                        "block_reason": item.get("block_reason") or "",
                        "m30_main_rows": item.get("m30_main_rows"),
                        "m30_stage_rows": item.get("m30_stage_rows"),
                        "m30_conflict_rows": item.get("m30_conflict_rows"),
                    }
                )
    summary_minute_failure_rows = int(summary.get("minute_data_failure_rows") or 0)
    diagnosis_code = str(summary.get("diagnosis_code") or "")
    data_wall_diagnosis = diagnosis_code in {
        "SOURCE_BUILDER_UNAVAILABLE",
        "BUILDER_OR_CANDIDATE_SNAPSHOT_UNAVAILABLE",
        "MINUTE_DATA_UNAVAILABLE",
        "PENDING_CONFIRMATION_RETRY_REQUIRED",
    }
    now = datetime.now(BUSINESS_TZ)
    pending = _truthy(summary.get("pending_next_session_confirmation"))
    pending_after_first_bar = bool(
        pending
        and now.strftime("%Y-%m-%d") == entry
        and (now.hour, now.minute) >= FIRST_COMPLETED_30M_TIME
    )
    gate.add(
        "source_builder_visibility",
        not bool(summary_source_failures),
        "block",
        "active source builders and frozen candidate snapshots must remain visible",
        {
            "live_confirmation_window": live_confirmation_window,
            "source_data_wall_count": int(summary.get("source_data_wall_count") or len(summary_source_failures)),
            "failures": summary_source_failures[:20],
        },
    )
    gate.add(
        "minute_source_visibility",
        not (bool(source_failures) or summary_minute_failure_rows > 0 or data_wall_diagnosis),
        "block",
        "30m data failures cannot be downgraded to a normal no-trade result",
        {
            "live_confirmation_window": live_confirmation_window,
            "summary_minute_data_failure_rows": summary_minute_failure_rows,
            "diagnosis_code": diagnosis_code,
            "source_failure_count": int(len(source_failures)),
            "failures": source_failures[:20],
        },
    )
    gate.add(
        "pending_confirmation_window",
        not pending_after_first_bar,
        "block",
        "pending next-session candidates must be re-evaluated after the first completed 30m bar",
        {
            "pending_next_session_confirmation": pending,
            "entry_date": entry,
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "first_completed_30m_time": "10:00",
        },
    )
    gate.add(
        "candidate_ticket_reconciliation",
        not (live_confirmation_window and bool(source_failures) and not qualified.empty),
        "block",
        "source candidates and qualified tickets must remain explainably reconcilable",
        {
            "source_rows": int(summary.get("source_rows") or len(all_source)),
            "selected_rows": int(summary.get("selected_rows") or 0),
            "qualified_ticket_count": int(len(qualified)),
            "source_failure_count": int(len(source_failures)),
        },
    )

    formal_rows = int(summary.get("formal_buy_signal_rows") or 0)
    auto_rows = int(summary.get("auto_order_allowed_rows") or 0)
    order_rows = int(summary.get("order_path_enabled_rows") or 0)
    gate.add("formal_order_locked", formal_rows == 0 and auto_rows == 0 and order_rows == 0, "block", "formal/auto order path remains disabled before manual promotion", {"formal": formal_rows, "auto": auto_rows, "order": order_rows})

    contract_status = _contract_checks(contract)
    gate.add("g3_2slot_contract", bool(contract_status["ok"]), "block", "runtime contract matches 2-slot default risk contract", contract_status)
    ticket_issues = _ticket_contract_issues(qualified)
    gate.add("ticket_risk_prices", not ticket_issues, "block", "qualified ticket prices and sizing match risk contract", {"issues": ticket_issues})

    ledger_status = _ledger_status(qualified, ledger)
    gate.add("shadow_ledger_consistency", bool(ledger_status["ok"]), "block", "ledger covers tickets and respects 2-slot exposure", ledger_status)
    gate.add("daily_open_limit", len(qualified) <= 2, "block", "daily new qualified buys must not exceed the 2-slot limit", {"qualified_count": int(len(qualified))})

    promotion = _promotion_gate_status()
    gate.add("promotion_self_test", bool(promotion.get("ok")), "warn", "latest promotion self-test gates are green", promotion)

    ticket_records = json.loads(qualified.astype(object).where(pd.notna(qualified), None).to_json(orient="records", force_ascii=False)) if not qualified.empty else []
    blockers = gate.blockers()
    warnings = gate.warnings()
    verdict = "blocked" if blockers else ("shadow_ready_with_warnings" if warnings else "shadow_ready")
    if ticket_records:
        trade_action = "buy_review"
    elif blockers:
        trade_action = "blocked_data"
    else:
        trade_action = "no_trade"
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": verdict,
        "trade_action": trade_action,
        "entry_date": entry,
        "decision_date": decision,
        "selected_route": summary.get("selected_route"),
        "diagnosis_code": summary.get("diagnosis_code"),
        "qualified_ticket_count": int(len(qualified)),
        "qualified_tickets": ticket_records,
        "gates": gate.rows,
        "blockers": blockers,
        "warnings": warnings,
        "artifacts": artifacts,
        "route_diagnostics": json.loads(diagnostics.astype(object).where(pd.notna(diagnostics), None).to_json(orient="records", force_ascii=False)) if not diagnostics.empty else [],
        "refresh": refresh_result,
    }
    (OUT_DIR / "latest_pretrade_smoke.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    pd.DataFrame(gate.rows).to_csv(OUT_DIR / "latest_pretrade_gates.csv", index=False, encoding="utf-8-sig")
    _write_report(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run G3 pretrade smoke checks before shadow/manual live use.")
    parser.add_argument("--entry-date", default="", help="Optional entry date passed to G3 shadow refresh.")
    parser.add_argument("--refresh", action="store_true", help="Run gen3_state_router_shadow_daily_v1 before checking artifacts.")
    args = parser.parse_args()
    payload = run(entry_date=args.entry_date or None, refresh=bool(args.refresh))
    print(
        json.dumps(
            {
                "status": "completed",
                "verdict": payload["verdict"],
                "trade_action": payload["trade_action"],
                "qualified_ticket_count": payload["qualified_ticket_count"],
                "blockers": [x["gate"] for x in payload["blockers"]],
                "warnings": [x["gate"] for x in payload["warnings"]],
                "out_dir": str(OUT_DIR),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
