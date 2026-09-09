"""Audit persisted daily volume/amount units against controlled source values.

The primary market-data source remains QMT/xtquant.  Local TDX day files are
only a bounded, auditable fallback for historical unit reconciliation.  The
QMT stage table is temporary and is trusted for comparison only when the
caller supplies the timestamp of the current stage refresh.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.repair_kline_daily_units_from_tdx import repair_from_tdx
from utils.kline_units import DAILY_UNIT_MISMATCH_CLASSES, classify_daily_unit_pair
from utils.paths import runtime_path
from services.operations.stage_contract import require_stage_contract


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_STAGE_TABLE = "kline_daily_qmtmini_coverage_lots_v1_stage"
DEFAULT_TDX_ROOT = Path(r"D:\TDX\vipdoc")


def default_tdx_root() -> Path:
    configured = str(os.environ.get("AISTOCK_LOCAL_TDX_ROOT") or "").strip()
    return Path(configured) if configured else DEFAULT_TDX_ROOT


def _quote_sql(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _safe_identifier(value: str) -> str:
    identifier = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", identifier):
        raise ValueError(f"unsafe ClickHouse identifier: {value!r}")
    return identifier


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def summarize_unit_comparisons(
    rows: Iterable[dict[str, Any]],
    *,
    source: str,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Summarize source comparisons without touching ClickHouse."""

    counts: Counter[str] = Counter()
    samples: dict[str, dict[str, Any]] = {}
    mismatch_samples: list[dict[str, Any]] = []
    latest_date: date | None = None
    latest_rows = 0
    latest_mismatches = 0
    checked = 0

    for raw in rows:
        code = str(raw.get("code") or "").upper()
        trade_date = _as_date(raw.get("trade_date"))
        db_volume = _as_float(raw.get("db_volume"))
        db_amount = _as_float(raw.get("db_amount"))
        source_volume = _as_float(raw.get("source_volume"))
        source_amount = _as_float(raw.get("source_amount"))
        classification = classify_daily_unit_pair(
            db_volume,
            db_amount,
            source_volume,
            source_amount,
        )
        counts[classification] += 1
        checked += 1
        if trade_date is not None and (latest_date is None or trade_date > latest_date):
            latest_date = trade_date
            latest_rows = 0
            latest_mismatches = 0
        if trade_date == latest_date:
            latest_rows += 1
            if classification != "ok":
                latest_mismatches += 1
        item = {
            "code": code,
            "trade_date": str(trade_date) if trade_date else str(raw.get("trade_date") or ""),
            "db_volume": db_volume,
            "source_volume": source_volume,
            "db_amount": db_amount,
            "source_amount": source_amount,
            "classification": classification,
        }
        samples.setdefault(classification, item)
        if classification in DAILY_UNIT_MISMATCH_CLASSES and len(mismatch_samples) < sample_limit:
            mismatch_samples.append(item)

    mismatch_rows = checked - counts.get("ok", 0)
    return {
        "source": source,
        "status": "healthy" if mismatch_rows == 0 else "degraded",
        "rows_checked": checked,
        "mismatch_rows": mismatch_rows,
        "repair_key_count": mismatch_rows,
        "classification_counts": dict(sorted(counts.items())),
        "sample_by_class": samples,
        "mismatch_samples": mismatch_samples,
        "latest_trade_date": str(latest_date) if latest_date else None,
        "latest_trade_date_rows": latest_rows,
        "latest_trade_date_mismatch_rows": latest_mismatches,
    }


def audit_tdx_units(
    start_date: str,
    end_date: str,
    *,
    tdx_root: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Audit, and optionally repair, only mismatched TDX-covered keys."""

    root = Path(tdx_root or default_tdx_root())
    base = {
        "source": "tdx_day",
        "tdx_root": str(root),
        "start_date": start_date,
        "end_date": end_date,
        "apply_requested": bool(apply),
    }
    if not root.exists():
        return {
            **base,
            "status": "unavailable",
            "reason": "tdx_root_missing",
            "rows_checked": 0,
            "mismatch_rows": 0,
            "unresolved_mismatch_rows": 0,
        }
    try:
        result = repair_from_tdx(
            start_date,
            end_date,
            root,
            apply=apply,
        )
    except Exception as exc:
        return {
            **base,
            "status": "unavailable",
            "reason": "tdx_audit_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "rows_checked": 0,
            "mismatch_rows": 0,
            "unresolved_mismatch_rows": 0,
        }

    if not result.get("ok"):
        return {
            **base,
            **result,
            "status": "unavailable",
            "unresolved_mismatch_rows": 0,
        }

    post_apply = None
    if apply and int(result.get("rows_to_repair") or 0) > 0:
        try:
            post_apply = repair_from_tdx(
                start_date,
                end_date,
                root,
                apply=False,
            )
        except Exception as exc:
            post_apply = {
                "ok": False,
                "status": "unavailable",
                "reason": "tdx_post_apply_audit_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }

    if post_apply is not None and not post_apply.get("ok"):
        unresolved = int(result.get("rows_to_repair") or 0)
        status = "degraded"
    else:
        unresolved = int(
            (post_apply or {}).get(
                "rows_to_repair",
                result.get("rows_to_repair") or 0,
            )
            or 0
        )
        status = "healthy" if unresolved == 0 else "degraded"
    return {
        **base,
        **result,
        "status": status,
        "mismatch_rows": int(result.get("rows_to_repair") or 0),
        "unresolved_mismatch_rows": unresolved,
        "post_apply": post_apply,
    }


def _stage_summary(client, stage_table: str) -> dict[str, Any]:
    row = client.query(
        f"""
        SELECT count(), max(trade_date), max(created_at)
        FROM {stage_table} FINAL
        """
    ).first_row
    return {
        "stage_rows": int(row[0] or 0),
        "stage_latest_trade_date": str(row[1]) if row[1] else None,
        "stage_latest_created_at": str(row[2]) if row[2] else None,
    }


def audit_qmt_stage_units(
    client,
    start_date: str,
    end_date: str,
    *,
    stage_table: str = DEFAULT_STAGE_TABLE,
    stage_created_after: datetime | None = None,
    sample_limit: int = 100,
) -> dict[str, Any]:
    """Compare a freshly generated QMT stage with persisted daily bars."""

    table = _safe_identifier(stage_table)
    base = {
        "source": "qmt_xtquant_stage",
        "stage_table": table,
        "temporary_stage": True,
        "trusted_for_auto_repair": False,
        "start_date": start_date,
        "end_date": end_date,
    }
    try:
        require_stage_contract(client, table)
        stage_summary = _stage_summary(client, table)
    except Exception as exc:
        return {
            **base,
            "status": "unavailable",
            "reason": "qmt_stage_table_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "rows_checked": 0,
            "mismatch_rows": 0,
            "unresolved_mismatch_rows": 0,
        }
    if stage_created_after is None:
        return {
            **base,
            **stage_summary,
            "status": "skipped",
            "reason": "temporary_stage_requires_current_run_timestamp",
            "rows_checked": 0,
            "mismatch_rows": 0,
            "unresolved_mismatch_rows": 0,
        }

    created_after = stage_created_after.replace(tzinfo=None, microsecond=0)
    base['trusted_for_auto_repair'] = True
    rows = client.query(
        f"""
        SELECT
            s.code,
            s.trade_date,
            k.volume AS db_volume,
            k.amount AS db_amount,
            s.volume AS source_volume,
            s.amount AS source_amount
        FROM (
            SELECT code, trade_date, volume, amount, created_at
            FROM {table} FINAL
        ) AS s
        INNER JOIN (
            SELECT code, trade_date, volume, amount
            FROM kline_daily FINAL
        ) AS k
            ON k.code = s.code AND k.trade_date = s.trade_date
        WHERE s.trade_date >= toDate({_quote_sql(start_date)})
          AND s.trade_date <= toDate({_quote_sql(end_date)})
          AND s.created_at >= toDateTime({_quote_sql(created_after.strftime("%Y-%m-%d %H:%M:%S"))})
        ORDER BY s.trade_date DESC, s.code
        """
    ).result_rows
    comparisons = [
        {
            "code": row[0],
            "trade_date": row[1],
            "db_volume": row[2],
            "db_amount": row[3],
            "source_volume": row[4],
            "source_amount": row[5],
        }
        for row in rows
    ]
    summary = summarize_unit_comparisons(
        comparisons,
        source="qmt_xtquant_stage",
        sample_limit=sample_limit,
    )
    return {
        **base,
        **stage_summary,
        "stage_created_after": created_after.isoformat(sep=" "),
        "trusted_for_auto_repair": True,
        "unresolved_mismatch_rows": int(summary["mismatch_rows"]),
        **summary,
    }


def build_unit_audit_report(
    start_date: str,
    end_date: str,
    *,
    mode: str,
    tdx: dict[str, Any],
    qmt_stage_before: dict[str, Any] | None = None,
    qmt_stage_after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable report consumed by maintenance and health readers."""

    qmt_stage = (
        qmt_stage_after
        if qmt_stage_after and qmt_stage_after.get("status") != "unavailable"
        else qmt_stage_before
    ) or {
        "source": "qmt_xtquant_stage",
        "status": "skipped",
        "reason": "no_current_stage_refresh",
        "temporary_stage": True,
        "trusted_for_auto_repair": False,
        "unresolved_mismatch_rows": 0,
    }
    unresolved_tdx = int(tdx.get("unresolved_mismatch_rows") or 0)
    unresolved_qmt = int(qmt_stage.get("unresolved_mismatch_rows") or 0)
    unresolved = unresolved_tdx + unresolved_qmt
    source_statuses = {str(item.get("status") or "") for item in (tdx, qmt_stage)}
    if unresolved:
        status = "degraded"
    elif source_statuses <= {"skipped", "unavailable"}:
        status = "unavailable"
    else:
        status = "healthy"
    return {
        "schema_version": 1,
        "checked_at": datetime.now(BUSINESS_TZ).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "unit_contract": {
            "stock_volume": "lots",
            "stock_amount": "yuan",
            "qmt_stock_daily_input": "lots/yuan",
            "qmt_index_daily_input": "SDK volume lots/amount yuan; raw DAT has a separate contract",
            "tdx_day_input": "volume shares converted to lots; amount yuan",
        },
        "status": status,
        "unresolved_mismatch_rows": unresolved,
        "repair_policy": "compare by (code, trade_date); repair only classified source mismatches; never apply a broad date-range multiplier",
        "tdx": tdx,
        "qmt_stage_before_apply": qmt_stage_before,
        "qmt_stage_after_apply": qmt_stage_after,
        "qmt_stage": qmt_stage,
        "repair_actions": {
            "tdx_rows_repaired": int(tdx.get("rows_repaired") or 0),
            "qmt_stage_rows_repaired": max(
                0,
                int((qmt_stage_before or {}).get("mismatch_rows") or 0)
                - int((qmt_stage_after or {}).get("mismatch_rows") or 0),
            ),
        },
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    now = datetime.now(BUSINESS_TZ)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["audit", "repair"], default="audit")
    parser.add_argument("--start-date", default=f"{now.year}-01-01")
    parser.add_argument("--end-date", default=now.strftime("%Y-%m-%d"))
    parser.add_argument("--tdx-root", type=Path, default=default_tdx_root())
    parser.add_argument("--report", type=Path, default=runtime_path("daily_kline_coverage", "unit_audit_latest.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tdx = audit_tdx_units(
        args.start_date,
        args.end_date,
        tdx_root=args.tdx_root,
        apply=args.mode == "repair",
    )
    report = build_unit_audit_report(
        args.start_date,
        args.end_date,
        mode=args.mode,
        tdx=tdx,
    )
    write_report(args.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(args.report),
                "unresolved_mismatch_rows": report["unresolved_mismatch_rows"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] in {"healthy", "unavailable"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
