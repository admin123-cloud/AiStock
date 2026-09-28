"""Read-only re-audit of the active pre-2010 review queue under auction-aware gates.

The report separates minute-vs-daily auction residuals from actual blockers.
It does not mutate formal K-line tables or the active review manifest.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.minute_daily_quality_gate import EXPECTED_UNITS, evaluate_code_day
from utils.paths import runtime_path


BASE_RUN = "purchased_history_review_20260922_refit_a_b_confirmed_20260922_v13_harry_a2_2pct"
QUEUE = "stock.purchased_history_data_review_items"
DAILY = "stock.purchased_daily_user_policy_compare_2000_2010"
PURCHASED_FAMILY = "purchased_a_share_history"
PERIODS = {
    "15m": {
        "count": 16,
        "candidate": "stock.purchased_native_15m_candidate_user_policy_20260921",
        "source": "stock.purchased_native_15m_candidate_user_policy_20260921_day_audit",
        "allowed_sources": {"native_purchased_15m", "aggregate_5m"},
        "allowed_quality": {"native_complete", "five_complete_fallback"},
    },
    "30m": {
        "count": 8,
        "candidate": "stock.purchased_native_30m_candidate_user_policy_20260921",
        "source": None,
        "allowed_sources": {"native_purchased", "five_minute_derived"},
        "allowed_quality": {"native_complete_code_day", "five_minute_complete_code_day"},
    },
    "60m": {
        "count": 4,
        "candidate": "stock.purchased_native_60m_candidate_user_policy_20260921",
        "source": None,
        "allowed_sources": {"native_purchased"},
        "allowed_quality": {"native_complete_code_day"},
    },
}

AUDIT_FIELDS = [
    "run_id", "base_run_id", "code", "trade_date", "period", "issue_type",
    "decision", "primary_category", "reasons", "minute_source_family", "daily_source_family",
    "minute_source_kind", "minute_source_ref", "source_units", "minute_rows",
    "unique_datetime_rows", "daily_rows", "minute_open", "minute_high",
    "minute_low", "minute_close", "daily_open", "daily_high", "daily_low",
    "daily_close", "minute_volume_lots", "daily_volume_lots", "minute_amount_yuan",
    "daily_amount_yuan", "price_relative_diff", "volume_relative_diff",
    "amount_relative_diff", "auction_residual_volume_lots",
    "auction_residual_amount_yuan", "original_reason",
]


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _audit_one(
    *, run_id: str, code: str, trade_date: date, period: str, issue_type: str,
    original_reason: str, bars: list[dict[str, Any]], daily: dict[str, Any] | None,
    daily_rows: int, source_kinds: set[str], source_refs: set[str],
    quality_values: set[str], unit_values: set[tuple[str, str]],
) -> dict[str, Any]:
    if period not in PERIODS:
        return {
            "run_id": run_id, "base_run_id": BASE_RUN, "code": code,
            "trade_date": trade_date, "period": period, "issue_type": issue_type,
            "decision": "C", "primary_category": "non_frequency_item_retained",
            "reasons": "non_frequency_review_item_not_auto_processed",
            "minute_source_family": "", "daily_source_family": "",
            "minute_source_kind": "", "minute_source_ref": "", "source_units": "",
            "minute_rows": 0, "unique_datetime_rows": 0, "daily_rows": daily_rows,
            "original_reason": original_reason,
        }

    spec = PERIODS[period]
    allowed_sources = spec["allowed_sources"]
    allowed_quality = spec["allowed_quality"]
    source_is_purchased = bool(source_kinds) and source_kinds <= allowed_sources
    evidence_complete = (
        source_is_purchased
        and len(source_kinds) == 1
        and bool(quality_values)
        and quality_values <= allowed_quality
        and len(unit_values) <= 1
    )
    if period == "15m":
        evidence_complete = evidence_complete and unit_values == {("lots", "yuan")}

    source_family = PURCHASED_FAMILY if source_is_purchased else None
    source_units = EXPECTED_UNITS if evidence_complete else None
    daily_for_gate = None
    if daily is not None:
        daily_for_gate = {
            "open": daily["open"], "high": daily["high"], "low": daily["low"],
            "close": daily["close"], "volume_lots": daily["volume"],
            "amount_yuan": daily["amount"],
        }
    result = evaluate_code_day(
        bars,
        trade_date=trade_date,
        daily=daily_for_gate,
        daily_rows=daily_rows,
        evidence_complete=evidence_complete,
        period=period,
        source_units=source_units,
        price_adjustment="none" if evidence_complete else None,
        minute_source_family=source_family,
        daily_source_family=PURCHASED_FAMILY if daily_rows == 1 else None,
    )
    agg = result.aggregate
    gate_reasons = list(result.reasons)
    if not source_is_purchased:
        gate_reasons.append(f"minute_source_unconfirmed={sorted(source_kinds)}")
    elif len(source_kinds) != 1:
        gate_reasons.append(f"ambiguous_minute_source_kinds={sorted(source_kinds)}")
    if quality_values and not quality_values <= allowed_quality:
        gate_reasons.append(f"candidate_quality_unconfirmed={sorted(quality_values)}")
    if period == "15m" and unit_values != {("lots", "yuan")}:
        gate_reasons.append(f"candidate_units_unconfirmed={sorted(unit_values)}")
    if period != "15m" and len(unit_values) > 1:
        gate_reasons.append(f"candidate_units_conflict={sorted(unit_values)}")
    decision = "A" if result.status == "accepted" and not gate_reasons else "C"
    if decision == "A":
        primary_category = "A_ready"
    elif daily_rows != 1:
        primary_category = "purchased_daily_missing_or_ambiguous"
    elif any(token in gate_reasons for token in (
        "duplicate_datetime", "standard_5m_slots_mismatch"
    )) or any(reason.startswith(("minute_rows=", "unique_datetime=")) for reason in gate_reasons):
        primary_category = "source_structure_incomplete"
    elif any(token in gate_reasons for token in (
        "invalid_positive_ohlc", "ohlc_relation_failed", "negative_quantity",
        "missing_or_nonfinite_quantity", "daily_ohlc_invalid", "daily_quantity_invalid"
    )):
        primary_category = "invalid_minute_bar_or_quantity"
    elif any(token in gate_reasons for token in (
        "minute_high_exceeds_daily_high", "minute_low_below_daily_low"
    )):
        primary_category = "minute_price_outside_daily_range"
    elif any(token in gate_reasons for token in (
        "minute_volume_exceeds_daily", "minute_amount_exceeds_daily",
        "minute_volume_zero_despite_daily_trade", "minute_amount_zero_despite_daily_trade"
    )):
        primary_category = "minute_total_exceeds_or_missing_vs_daily"
    elif any("source_" in reason or "unit" in reason or "adjustment" in reason or "evidence" in reason for reason in gate_reasons):
        primary_category = "source_or_unit_unconfirmed"
    else:
        primary_category = "other_review"

    return {
        "run_id": run_id,
        "base_run_id": BASE_RUN,
        "code": code,
        "trade_date": trade_date,
        "period": period,
        "issue_type": issue_type,
        "decision": decision,
        "primary_category": primary_category,
        "reasons": ";".join(gate_reasons) if gate_reasons else "all_purchase_envelope_gates_pass",
        "minute_source_family": source_family or "",
        "daily_source_family": PURCHASED_FAMILY if daily_rows == 1 else "",
        "minute_source_kind": ",".join(sorted(source_kinds)),
        "minute_source_ref": " | ".join(sorted(source_refs)[:4]),
        "source_units": EXPECTED_UNITS if evidence_complete else "unconfirmed",
        "minute_rows": result.minute_rows,
        "unique_datetime_rows": result.unique_datetime_rows,
        "daily_rows": daily_rows,
        "minute_open": agg.open if agg else None,
        "minute_high": agg.high if agg else None,
        "minute_low": agg.low if agg else None,
        "minute_close": agg.close if agg else None,
        "daily_open": daily["open"] if daily else None,
        "daily_high": daily["high"] if daily else None,
        "daily_low": daily["low"] if daily else None,
        "daily_close": daily["close"] if daily else None,
        "minute_volume_lots": agg.volume_lots if agg else None,
        "daily_volume_lots": daily["volume"] if daily else None,
        "minute_amount_yuan": agg.amount_yuan if agg else None,
        "daily_amount_yuan": daily["amount"] if daily else None,
        "price_relative_diff": result.price_relative_diff,
        "volume_relative_diff": result.volume_relative_diff,
        "amount_relative_diff": result.amount_relative_diff,
        "auction_residual_volume_lots": result.auction_residual_volume_lots,
        "auction_residual_amount_yuan": result.auction_residual_amount_yuan,
        "original_reason": original_reason,
    }


def _load_queue(client, base_run: str) -> list[dict[str, Any]]:
    sql = f"""
        SELECT code,trade_date,period,issue_type,expected_count,actual_count,
               missing_count,source_batch_id,source_archive,source_entry,evidence_ref,reason
        FROM {QUEUE}
        WHERE run_id=%(run_id)s
        ORDER BY trade_date,code,period
    """
    result = client.query(sql, parameters={"run_id": base_run})
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def _load_daily(client, base_run: str) -> tuple[dict[tuple[str, date], dict[str, Any]], Counter]:
    sql = f"""
        SELECT d.code,d.trade_date,count() AS daily_rows,
               any(d.open) AS open,any(d.high) AS high,any(d.low) AS low,
               any(d.close) AS close,any(d.volume) AS volume,any(d.amount) AS amount
        FROM {DAILY} d
        INNER JOIN (
            SELECT code,trade_date FROM {QUEUE}
            WHERE run_id=%(run_id)s GROUP BY code,trade_date
        ) q USING(code,trade_date)
        GROUP BY d.code,d.trade_date
    """
    result = client.query(sql, parameters={"run_id": base_run}, settings={"max_execution_time": 600, "max_threads": 4})
    mapping: dict[tuple[str, date], dict[str, Any]] = {}
    row_counts: Counter = Counter()
    for row in result.result_rows:
        code, day, count, op, hi, lo, cl, vol, amt = row
        key = (str(code), day)
        row_counts[key] = int(count)
        mapping[key] = {
            "open": float(op), "high": float(hi), "low": float(lo), "close": float(cl),
            "volume": float(vol), "amount": float(amt),
        }
    return mapping, row_counts


def _load_period_bars(client, base_run: str, period: str) -> dict[tuple[str, date], list[dict[str, Any]]]:
    spec = PERIODS[period]
    candidate = spec["candidate"]
    if period == "15m":
        audit = spec["source"]
        sql = f"""
            SELECT p.code,p.trade_date,p.datetime,p.open,p.high,p.low,p.close,p.volume,p.amount,
                   p.source_kind,p.quality_status,p.volume_unit,p.amount_unit,p.provenance
            FROM {candidate} p
            INNER JOIN (
                SELECT q.code,q.trade_date,any(a.chosen_source) AS chosen_source,
                       any(a.batch_id) AS chosen_batch
                FROM {QUEUE} q
                LEFT JOIN {audit} a USING(code,trade_date)
                WHERE q.run_id=%(run_id)s AND q.period='15m'
                GROUP BY q.code,q.trade_date
            ) k ON p.code=k.code AND p.trade_date=k.trade_date
                AND p.source_kind=k.chosen_source AND p.batch_id=k.chosen_batch
            WHERE p.source_year BETWEEN 2000 AND 2010
        """
    else:
        sql = f"""
            SELECT p.code,toDate(p.datetime) AS trade_date,p.datetime,p.open,p.high,p.low,p.close,
                   p.volume,p.amount,p.source_kind,p.quality_status,
                   '' AS volume_unit,'' AS amount_unit,p.source_ref
            FROM {candidate} p
            INNER JOIN (
                SELECT code,trade_date FROM {QUEUE}
                WHERE run_id=%(run_id)s AND period='{period}' GROUP BY code,trade_date
            ) q ON p.code=q.code AND toDate(p.datetime)=q.trade_date
            WHERE p.source_year BETWEEN 2000 AND 2010
        """
    result = client.query(sql, parameters={"run_id": base_run}, settings={"max_execution_time": 600, "max_threads": 4})
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for row in result.result_rows:
        code, day, dt, op, hi, lo, cl, vol, amt, source_kind, quality, volume_unit, amount_unit, source_ref = row
        key = (str(code), day)
        grouped[key].append({
            "datetime": dt, "open": float(op), "high": float(hi), "low": float(lo),
            "close": float(cl), "volume": float(vol), "amount": float(amt),
            "source_kind": str(source_kind), "quality_status": str(quality),
            "volume_unit": str(volume_unit), "amount_unit": str(amount_unit),
            "source_ref": str(source_ref),
        })
    return grouped


def _verify_source_contract(client) -> dict[str, Any]:
    daily_source = client.query("""
        SELECT groupUniqArray(source),count(),uniqExact(tuple(code,trade_date))
        FROM stock.purchased_history_daily_snapshot_stage
        WHERE quality_status='valid'
          AND batch_id IN ('purchased_daily_snapshot_pre2010_20260921',
                           'purchased_daily_snapshot_20260921_2010')
          AND trade_date BETWEEN toDate('2000-01-01') AND toDate('2010-12-31')
    """).result_rows[0]
    daily_compare = client.query(f"SELECT count(),uniqExact(tuple(code,trade_date)) FROM {DAILY}").result_rows[0]
    candidate_units = client.query("""
        SELECT groupUniqArray(volume_unit),groupUniqArray(amount_unit)
        FROM stock.purchased_native_15m_candidate_user_policy_20260921
    """).result_rows[0]
    if daily_source[0] != [PURCHASED_FAMILY]:
        raise RuntimeError(f"daily purchased source family changed: {daily_source[0]}")
    if int(daily_compare[0]) != int(daily_compare[1]):
        raise RuntimeError(f"purchased daily compare has duplicate keys: {daily_compare}")
    if candidate_units[0] != ["lots"] or candidate_units[1] != ["yuan"]:
        raise RuntimeError(f"purchased minute unit contract changed: {candidate_units}")
    return {
        "daily_source_families": list(daily_source[0]),
        "daily_snapshot_stage_rows": int(daily_source[1]),
        "daily_snapshot_stage_unique_keys": int(daily_source[2]),
        "daily_compare_rows": int(daily_compare[0]),
        "daily_compare_unique_keys": int(daily_compare[1]),
        "15m_candidate_units": {"volume": candidate_units[0], "amount": candidate_units[1]},
        "high_period_source_kinds": {"30m": sorted(PERIODS["30m"]["allowed_sources"]),
                                     "60m": sorted(PERIODS["60m"]["allowed_sources"])},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-run", default=BASE_RUN)
    parser.add_argument("--run-id", default="purchased_history_review_20260923_purchase_daily_envelope_audit_v1")
    parser.add_argument("--output-dir", type=Path, default=runtime_path("quality_plan", "pre2010_purchase_daily_envelope_20260923_v1"))
    args = parser.parse_args()

    client = clickhouse_client()
    source_contract = _verify_source_contract(client)
    queue = _load_queue(client, args.base_run)
    if not queue:
        raise RuntimeError(f"base review run is empty or missing: {args.base_run}")
    daily, daily_counts = _load_daily(client, args.base_run)

    key_rows: dict[tuple[str, date, str], dict[str, Any]] = {}
    for item in queue:
        key = (str(item["code"]), item["trade_date"], str(item["period"]))
        if key in key_rows:
            raise RuntimeError(f"duplicate review code-day-period key: {key}")
        key_rows[key] = item

    output: list[dict[str, Any]] = []
    source_bar_rows: dict[str, int] = {}
    for period in PERIODS:
        bars_by_key = _load_period_bars(client, args.base_run, period)
        source_bar_rows[period] = sum(len(rows) for rows in bars_by_key.values())
        keys = [key for key in key_rows if key[2] == period]
        for code, day, _ in keys:
            item = key_rows[(code, day, period)]
            bars = bars_by_key.get((code, day), [])
            source_kinds = {row["source_kind"] for row in bars}
            source_refs = {row["source_ref"] for row in bars if row["source_ref"]}
            quality_values = {row["quality_status"] for row in bars}
            unit_values = {(row["volume_unit"], row["amount_unit"]) for row in bars if row["volume_unit"] or row["amount_unit"]}
            daily_rows = daily_counts[(code, day)]
            record = _audit_one(
                run_id=args.run_id, code=code, trade_date=day, period=period,
                issue_type=str(item["issue_type"]), original_reason=str(item["reason"]),
                bars=bars, daily=daily.get((code, day)), daily_rows=daily_rows,
                source_kinds=source_kinds, source_refs=source_refs,
                quality_values=quality_values, unit_values=unit_values,
            )
            output.append(record)
        print(json.dumps({"period": period, "groups": len(keys), "candidate_bar_rows": source_bar_rows[period]}, ensure_ascii=False), flush=True)

    for item in queue:
        if item["period"] not in PERIODS:
            code, day, period = str(item["code"]), item["trade_date"], str(item["period"])
            output.append({
                "run_id": args.run_id, "base_run_id": args.base_run, "code": code,
                "trade_date": day, "period": period, "issue_type": str(item["issue_type"]),
                "decision": "C", "reasons": "source_5m_incomplete_or_non_frequency_issue_retained",
                "primary_category": "source_structure_incomplete",
                "minute_source_family": "purchased_a_share_history", "daily_source_family": "",
                "minute_source_kind": "incomplete_source_audit", "minute_source_ref": str(item["source_archive"]),
                "source_units": "unconfirmed", "minute_rows": int(item["actual_count"] or 0),
                "unique_datetime_rows": None, "daily_rows": daily_counts[(code, day)],
                "original_reason": str(item["reason"]),
            })

    decision_counts: dict[str, dict[str, int]] = {}
    reason_counts: Counter[str] = Counter()
    primary_category_counts: Counter[str] = Counter()
    for rec in output:
        period = str(rec["period"])
        decision_counts.setdefault(period, Counter())
        decision_counts[period][str(rec["decision"])] += 1
        primary_category_counts[str(rec["primary_category"])] += 1
        for reason in str(rec.get("reasons", "")).split(";"):
            if reason and reason != "all_purchase_envelope_gates_pass":
                reason_counts[f"{period}:{reason.split('=', 1)[0]}"] += 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "code_day_period_audit.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for rec in sorted(output, key=lambda row: (str(row["trade_date"]), str(row["code"]), str(row["period"]))):
            writer.writerow({key: _json_value(rec.get(key)) for key in AUDIT_FIELDS})

    accepted_path = args.output_dir / "a_grade_ready_for_promotion.csv"
    accepted_fields = [
        "code", "trade_date", "period", "minute_source_kind", "minute_source_ref",
        "minute_rows", "daily_rows", "minute_high", "daily_high", "minute_low",
        "daily_low", "minute_volume_lots", "daily_volume_lots", "minute_amount_yuan",
        "daily_amount_yuan", "auction_residual_volume_lots", "auction_residual_amount_yuan",
    ]
    with accepted_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=accepted_fields, extrasaction="ignore")
        writer.writeheader()
        for rec in sorted((row for row in output if row["decision"] == "A"), key=lambda row: (str(row["trade_date"]), str(row["code"]), str(row["period"]))):
            writer.writerow({key: _json_value(rec.get(key)) for key in accepted_fields})

    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "base_run_id": args.base_run,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": {
            "source_family": "minute and daily must both be purchased_a_share_history",
            "structure": {"5m": 48, "15m": 16, "30m": 8, "60m": 4},
            "price_gate": "all valid minute OHLC must lie inside purchased daily [low, high]",
            "quantity_gate": "normalized minute volume and amount must not exceed purchased daily totals",
            "shortfall_policy": "any lower-total percentage is informational auction/session residual; no 2% or other lower-bound threshold",
            "remaining_blockers": ["missing/ambiguous purchased daily", "incomplete/duplicate slots", "invalid OHLC/quantity", "minute price outside daily range", "minute total exceeds daily", "source/unit/evidence uncertainty"],
        },
        "source_contract": source_contract,
        "queue_rows": len(queue),
        "candidate_bar_rows": source_bar_rows,
        "decision_counts": {period: dict(counts) for period, counts in sorted(decision_counts.items())},
        "primary_category_counts": dict(primary_category_counts),
        "top_blocker_counts": dict(reason_counts.most_common(30)),
        "formal_tables_mutated": False,
        "active_review_manifest_updated": False,
        "a_grade_rows_are_candidates_only": True,
        "outputs": {"all_results": str(csv_path), "a_grade_candidates": str(accepted_path)},
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
