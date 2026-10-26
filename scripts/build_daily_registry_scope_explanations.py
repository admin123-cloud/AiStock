"""Build a complete local registry/member candidate and explain daily keys.

This is a read-only evidence builder.  It expands the accepted THS
membership manifest with the full per-security candidate manifest, writes a
local registry/member candidate for every source-backed code, explains every
formal key outside the accepted denominator, explains every accepted key
missing from the formal table, and reconciles values against the independent
WStock/TDX evidence table where an exact key exists.

The candidate registry/member is deliberately *not* published to ClickHouse.
All ClickHouse operations are SELECTs with ``readonly=1``; no DDL, DML, or
mutation is issued.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.daily_date_universe import normalize_code, normalize_trade_date
from utils.market_warehouse import clickhouse_client


READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 180, "max_threads": 2}
FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = list(getattr(result, "column_names", ()) or ())
    return [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]


def _open_jsonl(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("r", encoding="utf-8")


def _iter_manifest(path: Path) -> Iterable[dict[str, Any]]:
    with _open_jsonl(path) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _iter_accepted_code_dates(path: Path) -> Iterable[tuple[str, frozenset[str]]]:
    """Extract accepted code/date arrays without materializing JSON trees."""

    code_re = re.compile(r'"code"\s*:\s*"([^"]+)"')
    dates_re = re.compile(r'"dates"\s*:\s*\[(.*?)\]')
    value_re = re.compile(r'"(\d{4}-\d{2}-\d{2})"')
    with _open_jsonl(path) as handle:
        for line in handle:
            if not line.strip():
                continue
            code_match = code_re.search(line)
            dates_match = dates_re.search(line)
            if code_match is None or dates_match is None:
                continue
            yield normalize_code(code_match.group(1)), frozenset(value_re.findall(dates_match.group(1)))


def _source_dates(record: Mapping[str, Any]) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """Return normalized source date sets and their union for one record."""

    sources = record.get("sources") or {}
    source_sets: dict[str, tuple[str, ...]] = {}
    union: set[str] = set()
    if isinstance(sources, Mapping):
        for raw_name, payload in sources.items():
            if not isinstance(payload, Mapping):
                continue
            # Manifest dates are already ISO dates.  Keeping them as strings
            # avoids millions of date-object allocations during startup.
            dates = tuple(sorted({str(value)[:10] for value in payload.get("dates") or []}))
            if dates:
                name = str(raw_name).strip() or "unknown_source"
                source_sets[name] = dates
                union.update(dates)
    return source_sets, tuple(sorted(union))


def _date_digest(code: str, dates: Iterable[str]) -> str:
    payload = "".join(f"{code}\t{day}\n" for day in sorted(set(dates)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _day_number(value: str) -> int:
    return int(str(value)[:10].replace("-", ""))


def _day_text(value: int) -> str:
    text = f"{int(value):08d}"
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _record_meta(record: Mapping[str, Any]) -> dict[str, Any]:
    code = normalize_code(record.get("code"))
    source_sets, union = _source_dates(record)
    status = str(record.get("status") or "").strip()
    source_names = sorted(source_sets)
    if status == "source_date_set_available" and union:
        completeness = "complete"
        acceptance_state = "local_candidate_pending_online_publish"
        digest = _date_digest(code, union)
        dataset_id = f"local-full-candidate:{code}:{digest[:32]}"
    elif status == "date_source_unconfirmed":
        completeness = "unknown"
        acceptance_state = "pending_source_evidence"
        digest = ""
        dataset_id = f"local-full-candidate-pending:{code}"
    else:
        completeness = "unavailable"
        acceptance_state = "source_unavailable"
        digest = ""
        dataset_id = f"local-full-candidate-unavailable:{code}"
    formal = record.get("formal") or {}
    return {
        "code": code,
        "category": str(record.get("category") or "unknown"),
        "status": status,
        "completeness_state": completeness,
        "acceptance_state": acceptance_state,
        "dataset_id": dataset_id,
        "date_set_sha256": digest,
        "member_count": len(union),
        "coverage_start": union[0] if union else None,
        "coverage_end": union[-1] if union else None,
        "source_names": source_names,
        "source_date_counts": {name: len(values) for name, values in sorted(source_sets.items())},
        "formal_date_count": int(formal.get("date_count") or 0),
        "formal_first_date": formal.get("first_date"),
        "formal_last_date": formal.get("last_date"),
        "source_sets": source_sets,
        "candidate_dates": frozenset(union),
    }


def _read_formal_stats(client: Any, *, start_date: date, end_date: date) -> list[dict[str, Any]]:
    result = client.query(
        f"""
        SELECT code,count() AS rows,min(trade_date) AS first_trade_date,max(trade_date) AS last_trade_date
        FROM stock.kline_daily FINAL
        WHERE trade_date >= toDate({_quote(start_date.isoformat())})
          AND trade_date <= toDate({_quote(end_date.isoformat())})
        GROUP BY code ORDER BY code
        """,
        settings=READONLY_SETTINGS,
    )
    return _dict_rows(result)


def _read_formal_batch(client: Any, codes: Iterable[str], *, start_date: date, end_date: date) -> list[dict[str, Any]]:
    code_list = sorted({normalize_code(code) for code in codes})
    if not code_list:
        return []
    code_sql = ",".join(_quote(code) for code in code_list)
    result = client.query(
        f"""
        SELECT code,trade_date,open,high,low,close,volume,amount
        FROM stock.kline_daily FINAL
        WHERE code IN ({code_sql})
          AND trade_date >= toDate({_quote(start_date.isoformat())})
          AND trade_date <= toDate({_quote(end_date.isoformat())})
        ORDER BY trade_date,code
        """,
        settings=READONLY_SETTINGS,
    )
    return _dict_rows(result)


def _read_tdx_batch(client: Any, codes: Iterable[str]) -> list[dict[str, Any]]:
    code_list = sorted({normalize_code(code) for code in codes})
    if not code_list:
        return []
    code_sql = ",".join(_quote(code) for code in code_list)
    result = client.query(
        f"""
        SELECT code,trade_date,open,high,low,close,volume_shares,amount
        FROM stock.wstock_daily_tdx_evidence_20261014_v2
        WHERE code IN ({code_sql})
        ORDER BY trade_date,code
        """,
        settings=READONLY_SETTINGS,
    )
    output: list[dict[str, Any]] = []
    for row in result.result_rows:
        output.append(
            {
                "code": normalize_code(row[0]),
                "trade_date": str(row[1])[:10],
                "open": row[2],
                "high": row[3],
                "low": row[4],
                "close": row[5],
                "volume_shares": row[6],
                "amount": row[7],
                "source_kind": "wstock_tdx_evidence",
            }
        )
    return output


def _read_online_registry_member_state(client: Any) -> dict[str, Any]:
    result = client.query(
        """
        SELECT name
        FROM system.tables
        WHERE database = 'stock'
          AND name IN ('daily_date_universe_registry','daily_date_universe_member')
        ORDER BY name
        """,
        settings={"readonly": 1, "max_execution_time": 60, "max_threads": 1},
    )
    names = [str(row[0]) for row in result.result_rows]
    return {
        "present_tables": names,
        "complete": set(names) == {"daily_date_universe_registry", "daily_date_universe_member"},
    }


def _mismatch_fields(formal: Mapping[str, Any], source: Mapping[str, Any]) -> list[str]:
    mismatch: list[str] = []
    for field in FIELDS:
        left, right = formal.get(field), source.get(field)
        if (left is None) != (right is None):
            mismatch.append(field)
        elif left is not None and right is not None and not math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9):
            mismatch.append(field)
    return mismatch


def _scale_tdx_source(row: Mapping[str, Any], *, category: str) -> dict[str, Any]:
    """Map the TDX volume field using the security's declared type.

    The evidence column is historically named ``volume_shares``.  For stock
    rows it is shares and the formal table stores lots (divide by 100).  For
    index rows the source and formal index volume are already on the same
    scale, so no division is applied.
    """

    output = dict(row)
    raw = row.get("volume_shares")
    if raw is None:
        output["volume"] = None
        output["volume_unit_mapping"] = "unknown_null"
    elif str(category).lower() == "index":
        output["volume"] = float(raw)
        output["volume_unit_mapping"] = "index_raw"
    elif str(category).lower() == "stock":
        output["volume"] = float(raw) / 100.0
        output["volume_unit_mapping"] = "stock_shares_div_100"
    else:
        output["volume"] = float(raw)
        output["volume_unit_mapping"] = "unknown_raw_review"
    return output


def _write_json(handle: Any, row: Mapping[str, Any]) -> None:
    handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")


def build_report(
    *,
    client: Any,
    accepted_manifest: Path,
    candidate_manifest: Path,
    start_date: date,
    end_date: date,
    batch_size: int,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Stream the accepted manifest.  Keeping its full JSON rows and a second
    # set of four million dates at the same time needlessly doubles memory.
    # Manifest dates are already canonical ISO strings; avoid a second
    # normalization pass over four million members during startup.
    accepted_by_code: dict[str, frozenset[str]] = {}
    for code, raw_dates in _iter_accepted_code_dates(accepted_manifest):
        accepted_by_code[code] = frozenset(
            value
            for value in raw_dates
            if start_date.isoformat() <= value <= end_date.isoformat()
        )
    print(f"accepted_manifest_loaded codes={len(accepted_by_code)} members={sum(len(v) for v in accepted_by_code.values())}", flush=True)
    accepted_code_set = set(accepted_by_code)

    formal_stats = _read_formal_stats(client, start_date=start_date, end_date=end_date)
    print(f"formal_stats_loaded codes={len(formal_stats)} keys={sum(int(row['rows']) for row in formal_stats)}", flush=True)
    online_registry_member = _read_online_registry_member_state(client)
    formal_codes = {normalize_code(row["code"]) for row in formal_stats}
    seen_formal_codes: set[str] = set()
    outside_count = 0
    accepted_present = 0
    accepted_missing: list[dict[str, Any]] = []
    outside_counts: Counter[str] = Counter()
    candidate_status_counts: Counter[str] = Counter()
    candidate_member_total = 0
    candidate_code_total = 0
    candidate_formal_key_count = 0
    candidate_formal_key_missing = 0
    source_counts: Counter[str] = Counter()
    tdx_counts: Counter[str] = Counter()
    tdx_by_code: defaultdict[str, Counter[str]] = defaultdict(Counter)
    tdx_mismatch_samples: list[dict[str, Any]] = []
    batch_count = 0

    registry_path = output_dir / "full_registry_candidate.jsonl.gz"
    member_path = output_dir / "full_member_candidate.jsonl.gz"
    outside_path = output_dir / "formal_outside_accepted_key_explanations.jsonl.gz"
    missing_path = output_dir / "accepted_missing_key_explanations.jsonl.gz"
    tdx_path = output_dir / "independent_source_mismatches.jsonl.gz"
    source_meta_path = output_dir / "full_registry_candidate_summary.json"

    with (
        gzip.open(registry_path, "wt", encoding="utf-8") as registry_out,
        gzip.open(member_path, "wt", encoding="utf-8") as member_out,
        gzip.open(outside_path, "wt", encoding="utf-8") as outside_out,
        gzip.open(missing_path, "wt", encoding="utf-8") as missing_out,
        gzip.open(tdx_path, "wt", encoding="utf-8") as tdx_out,
    ):
        pending: list[dict[str, Any]] = []

        def process_batch(batch: list[dict[str, Any]]) -> None:
            nonlocal outside_count, accepted_present, batch_count, candidate_formal_key_count, candidate_formal_key_missing
            if not batch:
                return
            batch_count += 1
            metas = [_record_meta(row) for row in batch]
            codes = [meta["code"] for meta in metas]
            formal_rows = _read_formal_batch(client, codes, start_date=start_date, end_date=end_date)
            tdx_rows = _read_tdx_batch(client, codes)
            meta_by_code = {meta["code"]: meta for meta in metas}
            tdx_by_key = {
                (row["code"], row["trade_date"]): _scale_tdx_source(
                    row, category=str(meta_by_code.get(row["code"], {}).get("category") or "unknown")
                )
                for row in tdx_rows
            }
            for source in tdx_by_key.values():
                tdx_counts[f"source_unit_{source.get('volume_unit_mapping')}"] += 1
            formal_by_key = {(normalize_code(row["code"]), str(row["trade_date"])[:10]): row for row in formal_rows}
            for meta in metas:
                code = meta["code"]
                seen_formal_codes.add(code)
                accepted_dates = accepted_by_code.get(code, frozenset())
                candidate_dates = meta["candidate_dates"]
                if meta["completeness_state"] == "complete":
                    candidate_formal = 0
                    for day in candidate_dates:
                        if (code, day) in formal_by_key:
                            candidate_formal += 1
                    candidate_formal_key_count += candidate_formal
                    candidate_formal_key_missing += max(0, len(candidate_dates) - candidate_formal)
                present = {day for (item_code, day) in formal_by_key if item_code == code}
                present_text = {day for day in present}
                expected_present = {day for day in present_text if day in accepted_dates}
                accepted_present += len(expected_present)
                for day in sorted(accepted_dates - present_text):
                    source = tdx_by_key.get((code, day))
                    missing_detail = {
                        "code": code,
                        "trade_date": day,
                        "reason": "accepted_registry_member_key_missing_from_formal",
                        "accepted_member": True,
                        "candidate_member": day in candidate_dates,
                        "candidate_status": meta["status"],
                        "candidate_source_names": meta["source_names"],
                        "independent_source_key": source is not None,
                        "independent_source_status": None,
                    }
                    if source is not None:
                        missing_detail["independent_source_status"] = "independent_source_value_available_formal_missing"
                    accepted_missing.append(missing_detail)
                    _write_json(missing_out, missing_detail)
                for day in sorted(present_text):
                    if day in accepted_dates:
                        continue
                    if meta["status"] == "source_date_set_available" and day in candidate_dates:
                        reason = "full_candidate_member_present_outside_accepted_scope"
                        member_present = True
                    elif meta["status"] == "date_source_unconfirmed":
                        reason = "registry_member_source_unconfirmed"
                        member_present = False
                    elif meta["status"] == "no_available_date_source":
                        reason = "registry_member_source_unavailable"
                        member_present = False
                    elif not meta["status"]:
                        reason = "registry_member_code_missing_from_full_candidate_manifest"
                        member_present = False
                    elif not candidate_dates:
                        reason = "registry_member_source_date_set_empty"
                        member_present = False
                    elif day < min(candidate_dates):
                        reason = "formal_before_candidate_source_span"
                        member_present = False
                    elif day > max(candidate_dates):
                        reason = "formal_after_candidate_source_span"
                        member_present = False
                    else:
                        reason = "formal_not_in_candidate_member_set"
                        member_present = False
                    source = tdx_by_key.get((code, day))
                    detail = {
                        "code": code,
                        "trade_date": day,
                        "reason": reason,
                        "accepted_member": False,
                        "candidate_member": member_present,
                        "candidate_status": meta["status"] or "missing",
                        "candidate_source_names": meta["source_names"],
                        "candidate_source_span": [meta["coverage_start"], meta["coverage_end"]],
                        "candidate_dataset_id": meta["dataset_id"],
                        "independent_source_key": source is not None,
                        "independent_source_status": None,
                    }
                    if source is None:
                        detail["independent_source_status"] = "independent_source_key_missing"
                    else:
                        mismatch = _mismatch_fields(formal_by_key[(code, day)], source)
                        detail["independent_source_status"] = "independent_source_value_mismatch" if mismatch else "independent_source_value_match"
                        detail["independent_source_mismatch_fields"] = mismatch
                        if mismatch:
                            tdx_counts["value_mismatch"] += 1
                            tdx_by_code[code]["value_mismatch"] += 1
                            mismatch_row = {
                                "code": code,
                                "trade_date": day,
                                "status": "independent_source_value_mismatch",
                                "mismatch_fields": mismatch,
                                "formal_values": {field: formal_by_key[(code, day)].get(field) for field in FIELDS},
                                "source_values": {field: source.get(field) for field in FIELDS},
                                "source_volume_unit_mapping": source.get("volume_unit_mapping"),
                            }
                            _write_json(tdx_out, mismatch_row)
                            if len(tdx_mismatch_samples) < 20:
                                tdx_mismatch_samples.append(mismatch_row)
                        else:
                            tdx_counts["value_match"] += 1
                            tdx_by_code[code]["value_match"] += 1
                    if meta["status"] == "source_date_set_available" and day in candidate_dates:
                        outside_counts["full_candidate_member_present_outside_accepted_scope"] += 1
                    else:
                        outside_counts[reason] += 1
                    outside_count += 1
                    _write_json(outside_out, detail)
            # Independent source accounting includes every formal row with an exact TDX key,
            # including accepted rows and rows outside the accepted denominator.
            for key, formal in formal_by_key.items():
                source = tdx_by_key.get(key)
                if source is None:
                    tdx_counts["formal_key_without_independent_source"] += 1
                    tdx_by_code[key[0]]["formal_key_without_independent_source"] += 1
                else:
                    mismatch = _mismatch_fields(formal, source)
                    if mismatch:
                        # The mismatch ledger above is intentionally keyed to outside rows;
                        # accepted/in-scope mismatches are included here as aggregate samples.
                        tdx_counts["exact_key_value_mismatch_all_formal"] += 1
                        tdx_by_code[key[0]]["exact_key_value_mismatch_all_formal"] += 1
                    else:
                        tdx_counts["exact_key_value_match_all_formal"] += 1
                        tdx_by_code[key[0]]["exact_key_value_match_all_formal"] += 1
            for key in tdx_by_key:
                if key not in formal_by_key:
                    tdx_counts["independent_source_key_without_formal"] += 1
                    tdx_by_code[key[0]]["independent_source_key_without_formal"] += 1

        for raw_record in _iter_manifest(candidate_manifest):
            meta = _record_meta(raw_record)
            candidate_code_total += 1
            candidate_status_counts[meta["status"]] += 1
            candidate_member_total += int(meta["member_count"])
            for name, count in meta["source_date_counts"].items():
                source_counts[name] += count
            registry_row = {key: value for key, value in meta.items() if key not in {"source_sets", "candidate_dates"}}
            _write_json(registry_out, registry_row)
            for day in sorted(meta["candidate_dates"]):
                _write_json(
                    member_out,
                    {
                        "dataset_id": meta["dataset_id"],
                        "code": meta["code"],
                        "trade_date": day,
                        "date_set_sha256": meta["date_set_sha256"],
                        "source_names": meta["source_names"],
                        "acceptance_state": meta["acceptance_state"],
                    },
                )
            pending.append(raw_record)
            if len(pending) >= max(1, batch_size):
                print(f"processing_candidate_batch index={batch_count + 1} codes={len(pending)}", flush=True)
                process_batch(pending)
                pending = []
        if pending:
            print(f"processing_candidate_batch index={batch_count + 1} codes={len(pending)}", flush=True)
        process_batch(pending)

        # A formal code can be absent from the candidate manifest entirely.
        missing_manifest_codes = sorted(formal_codes - seen_formal_codes)
        for offset in range(0, len(missing_manifest_codes), max(1, batch_size)):
            batch = [
                {
                    "code": code,
                    "status": "",
                    "category": "missing_from_full_candidate_manifest",
                    "formal": {},
                    "sources": {},
                }
                for code in missing_manifest_codes[offset : offset + max(1, batch_size)]
            ]
            process_batch(batch)

    # The source reconciliation counts above include repeated all-formal
    # counters per batch only once per key; summarize exact source coverage.
    source_summary = {
        "status": "blocked" if tdx_counts["value_mismatch"] or tdx_counts["formal_key_without_independent_source"] else "healthy",
        "source_table": "stock.wstock_daily_tdx_evidence_20261014_v2",
        "unit_mapping": "stock volume_shares / 100 -> kline_daily lots; index volume_shares unchanged; amount unchanged",
        "counts": dict(sorted(tdx_counts.items())),
        "mismatch_samples": tdx_mismatch_samples,
    }
    outside_summary = {
        "outside_key_count": outside_count,
        "reason_counts": dict(sorted(outside_counts.items())),
        "ledger": str(outside_path),
    }
    missing_summary = {
        "missing_key_count": len(accepted_missing),
        "ledger": str(missing_path),
        "rows": accepted_missing,
    }
    candidate_summary = {
        "readonly": True,
        "candidate_manifest": str(candidate_manifest),
        "accepted_manifest": str(accepted_manifest),
        "registry_candidate": str(registry_path),
        "member_candidate": str(member_path),
        "candidate_code_count": candidate_code_total,
        "candidate_status_counts": dict(sorted(candidate_status_counts.items())),
        "candidate_member_total_across_source_backed_codes": candidate_member_total,
        "source_date_counts_with_overlap": dict(sorted(source_counts.items())),
        "formal_code_count": len(formal_codes),
        "formal_key_count": sum(int(row["rows"]) for row in formal_stats),
        "formal_code_missing_from_candidate_manifest": sorted(formal_codes - seen_formal_codes),
        "accepted_code_count": len(accepted_code_set),
        "accepted_member_count": sum(len(values) for values in accepted_by_code.values()),
        "accepted_formal_present_count": accepted_present,
        "accepted_formal_missing_count": len(accepted_missing),
        "outside": outside_summary,
        "independent_source_reconciliation": source_summary,
        "batch_count": batch_count,
        "batch_size": batch_size,
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
        "online_registry_member": online_registry_member,
        "online_registry_member_publish": "not_attempted",
    }
    source_meta_path.write_text(json.dumps(candidate_summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return candidate_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-manifest", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--start-date", default="1990-01-01")
    parser.add_argument("--end-date", default="2100-01-01")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = build_report(
        client=clickhouse_client(),
        accepted_manifest=args.accepted_manifest,
        candidate_manifest=args.candidate_manifest,
        start_date=normalize_trade_date(args.start_date),
        end_date=normalize_trade_date(args.end_date),
        batch_size=args.batch_size,
        output_dir=args.output_dir,
    )
    print(
        json.dumps(
            {
                "summary": str(args.output_dir / "full_registry_candidate_summary.json"),
                "candidate_code_count": summary["candidate_code_count"],
                "formal_key_count": summary["formal_key_count"],
                "outside_key_count": summary["outside"]["outside_key_count"],
                "missing_key_count": summary["accepted_formal_missing_count"],
                "outside_reason_counts": summary["outside"]["reason_counts"],
                "independent_source_counts": summary["independent_source_reconciliation"]["counts"],
                "production_mutation": summary["production_mutation"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
