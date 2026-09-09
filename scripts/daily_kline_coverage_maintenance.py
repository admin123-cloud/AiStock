"""Audit and bounded repair for persisted daily K-line coverage."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.audit_kline_daily_units import (
    audit_qmt_stage_units,
    audit_tdx_units,
    build_unit_audit_report,
    default_tdx_root,
    write_report as write_unit_audit_report,
)
from scripts import qmtmini_daily_backfill_validate as daily
from utils.paths import runtime_path


def _default_end_date(client) -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    # The closed-bar table must not be judged against today's bar before the
    # Shanghai close.  This also makes an intraday audit independent of the
    # current snapshot collector's timing.
    cutoff = now.date() if (now.hour, now.minute) >= (15, 5) else (now.date() - timedelta(days=1))
    row = client.query(
        "SELECT max(trade_date) FROM trade_calendar "
        f"WHERE market = 'SH' AND is_trading = 1 AND trade_date <= toDate('{cutoff:%Y-%m-%d}')"
    ).first_row
    if not row or not row[0]:
        raise RuntimeError("no SH trading date available for daily coverage audit")
    return str(row[0])


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_rolling_state(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _blocked_codes(state: dict) -> set[str]:
    return {str(code).upper() for code in state.get("blocked_codes", []) if str(code).strip()}


def _unresolved_records(state: dict) -> dict[str, dict]:
    """Return a durable, auditable classification record for each blocked code."""

    raw = state.get("unresolved")
    records = {
        str(code).upper(): dict(value)
        for code, value in raw.items()
        if str(code).strip() and isinstance(value, dict)
    } if isinstance(raw, dict) else {}
    for code in _blocked_codes(state):
        records.setdefault(
            code,
            {
                "classification": "prior_unresolved_source_outcome_not_persisted",
                "requires_business_confirmation": True,
            },
        )
    return records


def _unresolved_summary(state: dict) -> dict:
    records = _unresolved_records(state)
    counts: dict[str, int] = {}
    for item in records.values():
        key = str(item.get("classification") or "unclassified")
        counts[key] = counts.get(key, 0) + 1
    return {
        "unresolved_codes": len(records),
        "classification_counts": dict(sorted(counts.items())),
        "requires_business_confirmation_codes": sum(
            1 for item in records.values() if item.get("requires_business_confirmation")
        ),
    }


def _retryable_prior_unresolved_codes(state: dict) -> set[str]:
    """Keep source-empty codes in the bounded retry rotation until a source recovers."""
    return {
        code
        for code, item in _unresolved_records(state).items()
        if str(item.get("classification") or "") in {
            "prior_unresolved_source_outcome_not_persisted",
            "partial_or_unverified_source_coverage_pending_confirmation",
            "source_no_daily_bars_pending_business_confirmation",
        }
    }


def _write_rolling_state(path: Path, state: dict, blocked_codes: set[str]) -> None:
    state = dict(state)
    records = _unresolved_records(state)
    state["schema_version"] = 3
    state["blocked_codes"] = sorted(blocked_codes)
    state["unresolved"] = {code: records[code] for code in sorted(blocked_codes) if code in records}
    _write(path, state)


def _record_qmt_source_absences(client, args: argparse.Namespace, codes: list[str]) -> int:
    """Persist date-level QMT absences observed in a successful staged fetch."""
    if not codes:
        return 0
    daily.ensure_source_absence_table(client)
    code_sql = ",".join(daily.quote_sql(code) for code in codes)
    trade_dates = [row[0] for row in client.query(
        f"SELECT trade_date FROM trade_calendar WHERE market='SH' AND is_trading=1 "
        f"AND trade_date >= toDate({daily.quote_sql(args.start_date)}) AND trade_date <= toDate({daily.quote_sql(args.end_date)})"
    ).result_rows]
    metadata = {str(row[0]).upper(): {"list_date": row[1], "delist_date": row[2]} for row in client.query(
        f"SELECT code, list_date, delist_date FROM stocks WHERE code IN ({code_sql})"
    ).result_rows}
    expected, _ = daily.expected_daily_coverage_keys(codes, trade_dates, metadata)
    staged = {(str(row[0]).upper(), row[1]) for row in client.query(
        f"SELECT code, trade_date FROM {args.stage_table} WHERE code IN ({code_sql})"
    ).result_rows}
    absent = expected - staged
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if absent:
        client.insert(daily.SOURCE_ABSENCE_TABLE, [(code, day, "qmt_xtquant", "source_absent_after_successful_daily_probe", now) for code, day in absent], column_names=["code", "trade_date", "source", "classification", "observed_at"])
    return len(absent)


def _cross_source_fallback_command(start_date: str, end_date: str, codes: list[str], report: Path) -> list[str]:
    """Build the bounded fallback command used by both after-close and night runs."""
    return [
        sys.executable,
        str(REPO_ROOT / "scripts" / "baostock_daily_fallback_repair.py"),
        "--start-date", start_date,
        "--end-date", end_date,
        "--codes", ",".join(codes),
        "--candidate-mode", "qmt_absence",
        "--apply",
        "--report", str(report),
    ]


def _run_cross_source_fallback(start_date: str, end_date: str, codes: list[str], report: Path) -> dict:
    """Run the auditable Baostock/AkShare fallback without widening its scope."""
    if not codes:
        return {"status": "skipped", "reason": "no_target_codes"}
    command = _cross_source_fallback_command(start_date, end_date, codes, report)
    try:
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    result: dict = {
        "status": "completed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "report": str(report),
    }
    if completed.returncode:
        result["error"] = (completed.stderr or completed.stdout)[-1200:]
    return result


def parse_args() -> argparse.Namespace:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    parser = argparse.ArgumentParser(description="Audit and safely maintain daily K-line calendar coverage.")
    parser.add_argument("--mode", choices=["audit", "repair"], default="audit")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--scope", choices=["year", "latest"], default="year")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-repair-codes", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--cross-source-fallback", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--unit-audit", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tdx-root", type=Path, default=default_tdx_root())
    parser.add_argument("--report", type=Path, default=runtime_path("daily_kline_coverage", "latest.json"))
    return parser.parse_args()


def _daily_args(start_date: str, end_date: str, codes: list[str], batch_size: int, report: Path) -> argparse.Namespace:
    return argparse.Namespace(
        phase="all", start_date=start_date, end_date=end_date, codes=",".join(codes), code_offset=0,
        limit=0, include_index=False, batch_size=batch_size, delete_chunk_size=100,
        stage_table="kline_daily_qmtmini_coverage_stage", reset_stage=True, resume=False,
        sleep=0.02, max_retries=2, retry_sleep=1.0, use_batch_download=True,
        skip_download=False, dividend_type="none", price_tolerance=0.001,
        volume_tolerance=1.0, require_complete=False, report=str(report),
    )


def _attach_unit_audit(
    payload: dict,
    args: argparse.Namespace,
    start_date: str,
    end_date: str,
    qmt_stage_before: dict | None,
    qmt_stage_after: dict | None,
) -> None:
    if args.unit_audit:
        tdx_unit_audit = audit_tdx_units(
            start_date,
            end_date,
            tdx_root=args.tdx_root,
            # Historical unit repair is deliberately independent from the
            # recurring coverage task. This job reports mismatches only.
            apply=False,
        )
        unit_audit = build_unit_audit_report(
            start_date,
            end_date,
            mode=args.mode,
            tdx=tdx_unit_audit,
            qmt_stage_before=qmt_stage_before,
            qmt_stage_after=qmt_stage_after,
        )
        unit_report_path = args.report.with_name("unit_audit_latest.json")
        write_unit_audit_report(unit_report_path, unit_audit)
        payload["unit_audit"] = unit_audit
        payload["unit_audit_report"] = str(unit_report_path)
        return
    payload["unit_audit"] = {
        "status": "skipped",
        "reason": "unit_audit_disabled",
        "unresolved_mismatch_rows": 0,
    }


def main() -> int:
    args = parse_args()
    client = daily.ch_client()
    end_date = args.end_date or _default_end_date(client)
    args.start_date = args.start_date or (end_date if args.scope == 'latest' else f'{end_date[:4]}-01-01')
    if args.start_date > end_date:
        raise ValueError('start_date must not be after the last closed trading date')
    # Keep the coverage contract aligned with reviewed exchange/issuer events.
    # This is idempotent: raw QMT-absence evidence is retained in its own
    # table, while only verified status intervals are exempted from repair.
    daily.sync_verified_market_status_events(client)
    daily.reconcile_verified_delist_metadata(client)
    daily.reconcile_verified_non_stock_metadata(client)
    audit_args = _daily_args(args.start_date, end_date, [], args.batch_size, args.report)
    before = daily.coverage_summary(client, audit_args, "kline_daily")
    state_path = args.report.with_name("rolling_state.json")
    state = _read_rolling_state(state_path)
    qmt_stage_started_at: datetime | None = None
    qmt_stage_before: dict | None = None
    qmt_stage_after: dict | None = None
    payload: dict = {
        "schema_version": 1,
        "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "mode": args.mode,
        "start_date": args.start_date,
        "end_date": end_date,
        "before": before,
        "abnormal_daily_data_policy": daily.abnormal_daily_data_policy(),
        "unresolved_summary": _unresolved_summary(state),
    }
    if args.mode == "repair" and not before["repair_backlog_code_dates"]:
        payload["repair"] = {"status": "completed", "target_codes": [], "steps": [], "reason": "coverage_already_complete"}
    if args.mode == "repair" and before["repair_backlog_code_dates"]:
        # A QMT source can legitimately lack a suspended/delisted code's old
        # bars.  Do not let that bounded set monopolize every continuous run:
        # retain it for a later retry and advance through the remaining gaps.
        full_summary = daily.coverage_summary(client, audit_args, "kline_daily", candidate_limit=0)
        all_candidates_by_code = {
            str(item["code"]).upper(): item
            for item in [*full_summary["missing_by_code_sample"], *full_summary["qmt_retry_by_code_sample"]]
        }
        all_candidates = list(all_candidates_by_code.values())
        blocked_codes = _blocked_codes(state)
        # Existing schema-v1 state was created by the old infinite loop and
        # cannot prove where a complete no-progress cycle began.  Discard its
        # cursor once, establish an explicit baseline, and run one auditable
        # full pass from the current coverage state.
        if state.get("cycle_baseline_repair_backlog_code_dates") is None:
            state["cycle_baseline_repair_backlog_code_dates"] = int(before["repair_backlog_code_dates"])
            state["cycle_started_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
            blocked_codes.clear()
        # A legacy blocked entry only means the old loop omitted its source
        # outcome.  It is not proof that QMT has no bar, and therefore must
        # receive one bounded, auditable source probe before it can block a
        # later continuous retry cycle.
        retryable_prior = _retryable_prior_unresolved_codes(state)
        available = [
            item
            for item in all_candidates
            if item["code"].upper() not in blocked_codes or item["code"].upper() in retryable_prior
        ]
        if not available:
            baseline_backlog = state.get("cycle_baseline_repair_backlog_code_dates")
            if baseline_backlog is not None:
                current_backlog = int(before["repair_backlog_code_dates"])
                if current_backlog >= int(baseline_backlog):
                    # Persist the legacy blocked-code list in schema 3 even
                    # when no repair is attempted.  Otherwise a terminal
                    # no-progress cycle would keep reporting a state that
                    # cannot identify its unresolved business-confirmation
                    # requirement on the next run.
                    _write_rolling_state(state_path, state, blocked_codes)
                    payload["repair"] = {
                        "status": "completed",
                        "target_codes": [],
                        "steps": [],
                        "reason": "no_progress_after_full_cycle",
                        "cycle": {
                            "baseline_repair_backlog_code_dates": int(baseline_backlog),
                            "after_repair_backlog_code_dates": current_backlog,
                            "candidate_codes": len(all_candidates),
                        },
                        "stop_continuous": True,
                        "unresolved_summary": _unresolved_summary(state),
                    }
                    payload["after"] = before
                    payload["status"] = "degraded"
                    _attach_unit_audit(
                        payload,
                        args,
                        args.start_date,
                        end_date,
                        qmt_stage_before,
                        qmt_stage_after,
                    )
                    _write(args.report, payload)
                    print(
                        json.dumps(
                            {
                                "status": payload["status"],
                                "report": str(args.report),
                                "repair_backlog_code_dates": current_backlog,
                                "stop_continuous": True,
                                "unit_audit_status": payload["unit_audit"].get("status"),
                                "unit_audit_unresolved_mismatch_rows": payload["unit_audit"].get("unresolved_mismatch_rows", 0),
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 0
                # The completed pass made progress, so begin another pass from
                # the improved baseline instead of stopping prematurely.
                state["cycle_baseline_repair_backlog_code_dates"] = current_backlog
                state["cycle_started_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
                blocked_codes.clear()
            available = all_candidates
        # Rotate bounded QMT retries across every unresolved code.  Without a
        # cursor, a persistent source-empty symbol at the top of the ranking
        # would monopolize every nightly batch and the rest would never get a
        # fresh QMT probe or a later TDX fallback opportunity.
        retry_cursor = max(0, int(state.get("qmt_retry_cursor", 0) or 0))
        if available:
            retry_cursor %= len(available)
            available = available[retry_cursor:] + available[:retry_cursor]
        codes = [item["code"] for item in available[: max(0, args.max_repair_codes)]]
        if available and codes:
            state["qmt_retry_cursor"] = (retry_cursor + len(codes)) % len(available)
        if codes:
            qmt_stage_started_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
            repair_args = _daily_args(args.start_date, end_date, codes, args.batch_size, args.report.with_name("latest_repair.json"))
            try:
                fetch_step = daily.fetch_to_stage(repair_args)
                source_absent_count = _record_qmt_source_absences(client, repair_args, codes)
                qmt_stage_before = audit_qmt_stage_units(
                    client,
                    args.start_date,
                    end_date,
                    stage_table=repair_args.stage_table,
                    stage_created_after=qmt_stage_started_at,
                )
                steps = [
                    fetch_step,
                    daily.validate_stage(repair_args),
                    {"qmt_stage_unit_audit_before_apply": qmt_stage_before},
                    daily.apply_stage(repair_args),
                ]
                target_validation = daily.validate_target(repair_args)
                steps.append(target_validation)
                qmt_stage_after = audit_qmt_stage_units(
                    client,
                    args.start_date,
                    end_date,
                    stage_table=repair_args.stage_table,
                    stage_created_after=qmt_stage_started_at,
                )
                steps.append({"qmt_stage_unit_audit_after_apply": qmt_stage_after})
                if args.cross_source_fallback:
                    fallback_report = args.report.with_name("cross_source_fallback_latest.json")
                    steps.append({
                        "cross_source_fallback": _run_cross_source_fallback(
                            args.start_date, end_date, codes, fallback_report,
                        )
                    })
                    # The fallback is allowed to fill only keys which QMT
                    # successfully probed but left empty. Revalidate after it
                    # instead of relying on its own provider summary.
                    target_validation = daily.validate_target(repair_args)
                    steps.append(target_validation)
            except Exception as exc:
                payload["repair"] = {
                    "status": "failed",
                    "target_codes": codes,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                payload["status"] = "failed"
                _write(args.report, payload)
                print(json.dumps({"status": payload["status"], "report": str(args.report), "error": payload["repair"]["error"]}, ensure_ascii=False))
                return 1
            unresolved = {
                str(item["code"]).upper()
                for item in target_validation.get("missing_by_code_sample", [])
                if str(item.get("code") or "").upper() in {code.upper() for code in codes}
            }
            empty_source_codes = {
                str(code).upper()
                for code in steps[0].get("empty_codes_sample", [])
            }
            records = _unresolved_records(state)
            for code in {code.upper() for code in codes} - unresolved:
                records.pop(code, None)
                blocked_codes.discard(code)
            recorded_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
            for code in unresolved:
                records[code] = {
                    "classification": (
                        "source_no_daily_bars_pending_business_confirmation"
                        if code in empty_source_codes
                        else "partial_or_unverified_source_coverage_pending_confirmation"
                    ),
                    "requires_business_confirmation": True,
                    "recorded_at": recorded_at,
                }
            state["unresolved"] = records
            blocked_codes.update(unresolved)
            _write_rolling_state(state_path, state, blocked_codes)
            payload["repair"] = {
                "status": "completed",
                "target_codes": codes,
                "steps": steps,
                "source_absent_code_dates_recorded": source_absent_count,
                "selection": {
                    "skipped_prior_unresolved_codes": len(_blocked_codes(_read_rolling_state(state_path)) - unresolved),
                    "qmt_retry_cursor_next": state.get("qmt_retry_cursor", 0),
                },
                "unresolved_target_codes": sorted(unresolved),
                "unresolved_target_classification": [
                    {
                        "code": code,
                        "classification": (
                            "source_no_daily_bars_pending_business_confirmation"
                            if code in empty_source_codes
                            else "partial_or_unverified_source_coverage_pending_confirmation"
                        ),
                    }
                    for code in sorted(unresolved)
                ],
                "unresolved_summary": _unresolved_summary(state),
            }
            payload["after"] = daily.coverage_summary(client, audit_args, "kline_daily")
    _attach_unit_audit(
        payload,
        args,
        args.start_date,
        end_date,
        qmt_stage_before,
        qmt_stage_after,
    )
    final = payload.get("after", before)
    payload["status"] = "healthy" if final["repair_backlog_code_dates"] == 0 else "degraded"
    if int(payload["unit_audit"].get("unresolved_mismatch_rows") or 0) > 0:
        payload["status"] = "degraded"
    _write(args.report, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "report": str(args.report),
                "missing_code_dates": final["missing_code_dates"],
                "unit_audit_status": payload["unit_audit"].get("status"),
                "unit_audit_unresolved_mismatch_rows": payload["unit_audit"].get("unresolved_mismatch_rows", 0),
            },
            ensure_ascii=False,
        )
    )
    # A bounded repair may complete correctly while older gaps remain.  Keep
    # that coverage state explicit in the report, but reserve a non-zero exit
    # code for an execution failure so the host scheduler does not mislabel a
    # completed, still-unclosed maintenance pass as a runtime failure.
    return 0 if args.mode == "repair" else (0 if payload["status"] == "healthy" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
