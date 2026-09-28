from __future__ import annotations

"""Build independent, read-only closure manifests for pre-2010 years.

This consolidates already completed yearly derived dry-run evidence with the
new repair-candidate manifest.  It deliberately does not connect to
ClickHouse or write any formal table.  A year remains ``materialization_pending``
when the repair candidate contains only code-day evidence and no raw 5m detail.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def aggregate_derived(path: Path) -> dict[str, dict[str, int]]:
    payload = read_json(path)
    totals: dict[str, Counter] = {}
    for row in payload.get("rows", []):
        for period, values in (row.get("periods") or {}).items():
            total = totals.setdefault(period, Counter())
            for key in (
                "source_rows",
                "target_logical_rows",
                "missing_target_rows",
                "extra_target_rows",
                "target_duplicate_groups",
                "incomplete_source_buckets",
                "value_difference_rows",
            ):
                total[key] += int(values.get(key) or 0)
    return {period: dict(values) for period, values in totals.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--derived-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2009)
    args = parser.parse_args()

    candidate_manifest = read_json(args.candidate_manifest)
    by_year = {int(item["year"]): item for item in candidate_manifest.get("years", [])}
    args.output_root.mkdir(parents=True, exist_ok=True)
    if not (2000 <= args.start_year <= args.end_year <= 2009):
        raise SystemExit("scope must remain within 2000-2009")
    years: list[dict] = []
    for year in range(args.start_year, args.end_year + 1):
        item = by_year.get(year, {})
        source = args.derived_root / f"year_{year}" / "report.json"
        derived = aggregate_derived(source) if source.is_file() else {}
        derived_payload = read_json(source) if source.is_file() else {}
        out_dir = args.output_root / str(year)
        out_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": "pre2010-year-closure-v1",
            "year": year,
            "candidate_manifest": str(args.candidate_manifest),
            "candidate_manifest_sha256": sha256(args.candidate_manifest),
            "candidate_file": item.get("candidate_file"),
            "candidate_sha256": item.get("candidate_sha256"),
            "review_file": item.get("review_file"),
            "review_sha256": item.get("review_sha256"),
            "candidate_code_days": int(item.get("candidate_rows") or 0),
            "review_code_days": int(item.get("review_rows") or 0),
            "review_reasons": item.get("review_reasons") or {},
            "derived_evidence_file": str(source) if source.is_file() else None,
            "derived_evidence_sha256": sha256(source) if source.is_file() else None,
            "derived": derived,
            "derived_memory_events": derived_payload.get("memory_events") or [],
            "derived_shard_subdivision": derived_payload.get("shard_subdivision"),
            "formal_clickhouse_written": False,
            "formal_mutation": False,
            "status": "materialization_pending",
            "blocking_reasons": [
                "repair_candidate_is_code_day_evidence_only",
                "raw_5m_detail_not_materialized_from_new_candidate",
            ],
            "notes": "Derived evidence is frozen baseline against the old candidate table; it is not proof of the new repair candidate.",
        }
        if report["derived_memory_events"]:
            report["blocking_reasons"].append("derived_run_had_memory_retry_events")
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        years.append(report)

    summary = {
        "schema_version": "pre2010-year-closure-manifest-v1",
        "run_id": f"pre2010_year_closure_{datetime.now(TZ):%Y%m%dT%H%M%S}",
        "scope": {"start_year": args.start_year, "end_year": args.end_year},
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "years": years,
    }
    (args.output_root / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
