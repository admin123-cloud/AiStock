"""Build a read-only 2000--2009 quality summary and candidate manifest.

The input is the existing local Ocean daily-reconciliation evidence.  This
script never opens ClickHouse and never promotes a row.  It writes only a
new, immutable runtime report containing accepted code-day candidates; rows
that are incomplete or contradictory stay in review.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

# Allow the script to be invoked directly from the repository root or from a
# scheduler, matching the other standalone AiStock scripts.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.minute_daily_quality_gate import EXPECTED_UNITS, EXPECTED_BAR_COUNT
from utils.paths import runtime_path


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DAILY_SCHEMA = "ocean-daily-reconciliation-v1"
PERIOD_FACTORS = {"5m": 1, "15m": 3, "30m": 6, "60m": 12}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_not_object={path}")
    return value


def _iter_rows(path: Path, expected_sha256: str | None) -> Iterator[dict[str, Any]]:
    if expected_sha256 and _sha256(path) != expected_sha256:
        raise ValueError(f"evidence_hash_mismatch={path}")
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"evidence_row_not_object={path}:{line_number}")
            yield value


def _candidate_row(row: dict[str, Any]) -> bool:
    """Apply the strict candidate gate to one existing pass-evidence row."""

    return bool(
        row.get("overall_status") == "pass"
        and row.get("structure_pass") is True
        and row.get("daily_gate_pass") is True
        and int(row.get("minute_rows") or 0) == EXPECTED_BAR_COUNT
        and int(row.get("unique_datetime_rows") or 0) == EXPECTED_BAR_COUNT
        and int(row.get("daily_snapshot_rows") or 0) == 1
        and str(row.get("units_contract") or "") == f"{EXPECTED_UNITS}; raw_unadjusted_price"
        and not str(row.get("reason") or "").strip()
    )


def _daily_roots(daily_root: Path, legacy_root: Path, year: int) -> list[Path]:
    names = (f"daily_reconciliation_{year}", f"daily_reconciliation_{year}_fast")
    result: list[Path] = []
    for root in (daily_root, legacy_root):
        for name in names:
            path = root / name
            if path.is_dir() and path not in result:
                result.append(path)
    return result


def _manifest_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(root.rglob("manifest.json")):
        try:
            payload = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("schema_version") == DAILY_SCHEMA:
            paths.append(path)
    return paths


def _state(root: Path) -> dict[str, Any]:
    path = root / "state.json"
    return _load_json(path) if path.is_file() else {"status": "missing"}


def _stage_summary(path: Path | None) -> dict[int, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    payload = _load_json(path)
    result: dict[int, dict[str, Any]] = {}
    for row in payload.get("years", []):
        if isinstance(row, dict) and row.get("year") is not None:
            result[int(row["year"])] = row
    return result


def _candidate_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("code") or ""), str(row.get("trade_date") or "")


def _trim_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": row.get("code"),
        "trade_date": row.get("trade_date"),
        "source_entry": row.get("source_entry"),
        "batch_id": row.get("batch_id"),
        "source_archive": row.get("source_archive"),
        "source_manifest_sha256": row.get("source_manifest_sha256"),
        "minute_rows": row.get("minute_rows"),
        "unique_datetime_rows": row.get("unique_datetime_rows"),
        "price_relative_diff": row.get("price_relative_diff"),
        "volume_relative_diff": row.get("volume_relative_diff"),
        "amount_relative_diff": row.get("amount_relative_diff"),
        "units_contract": row.get("units_contract"),
        "evidence_partition": row.get("evidence_partition"),
    }


def summarize_year(
    year: int,
    *,
    daily_roots: Iterable[Path],
    output_root: Path,
    stage_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize one year and write its accepted code-day candidate list."""

    roots = list(daily_roots)
    output_root.mkdir(parents=True, exist_ok=True)
    raw_path = output_root / f"candidate_{year}.raw.jsonl.gz"
    candidate_path = output_root / f"candidate_{year}.jsonl.gz"
    summary: dict[str, Any] = {
        "year": year,
        "status": "not_started",
        "daily_roots": [str(path) for path in roots],
        "manifest_count": 0,
        "pass_evidence_rows": 0,
        "review_evidence_rows": 0,
        "rejected_evidence_rows": 0,
        "accepted_code_days_before_dedup": 0,
        "candidate_code_days": 0,
        "duplicate_candidate_code_days": 0,
        "candidate_file": str(candidate_path),
        "candidate_sha256": None,
        "formal_authorized": False,
        "stage_evidence": stage_evidence or {},
        "errors": [],
    }
    if not roots:
        summary["status"] = "not_started"
        return summary

    selected = roots[0]
    state = _state(selected)
    summary["state"] = state
    manifests = _manifest_paths(selected)
    summary["manifest_count"] = len(manifests)
    if state.get("status") != "complete":
        summary["status"] = "blocked"
        if state.get("error"):
            summary["errors"].append(str(state["error"]))
        else:
            summary["errors"].append(f"daily_state={state.get('status')}")
        return summary
    if not manifests:
        summary["status"] = "not_started"
        if state.get("error"):
            summary["errors"].append(str(state["error"]))
        return summary

    accepted_keys: set[tuple[str, str]] = set()
    duplicate_keys: set[tuple[str, str]] = set()
    try:
        with gzip.open(raw_path, "wt", encoding="utf-8", newline="\n") as raw_handle:
            for manifest_path in manifests:
                manifest = _load_json(manifest_path)
                entry_dir = manifest_path.parent
                for partition, count_key, sha_key in (
                    ("pass", "pass_evidence_rows", "pass_sha256"),
                    ("review", "review_evidence_rows", "review_sha256"),
                ):
                    filename = manifest.get(f"{partition}_file")
                    if not filename:
                        raise ValueError(f"manifest_{partition}_file_missing={manifest_path}")
                    evidence_path = entry_dir / str(filename)
                    for row in _iter_rows(evidence_path, manifest.get(sha_key)):
                        if partition == "review":
                            summary[count_key] += 1
                            continue
                        summary["pass_evidence_rows"] += 1
                        if str(row.get("overall_status") or "pass") == "rejected":
                            summary["rejected_evidence_rows"] += 1
                        if not _candidate_row(row):
                            summary["review_evidence_rows"] += 1
                            continue
                        summary["accepted_code_days_before_dedup"] += 1
                        key = _candidate_key(row)
                        if key in accepted_keys:
                            duplicate_keys.add(key)
                        else:
                            accepted_keys.add(key)
                        raw_handle.write(json.dumps(_trim_candidate(row), ensure_ascii=False, separators=(",", ":")) + "\n")
    except (OSError, ValueError, json.JSONDecodeError, EOFError) as exc:
        summary["status"] = "blocked"
        summary["errors"].append(f"{type(exc).__name__}: {exc}")
        raw_path.unlink(missing_ok=True)
        return summary

    summary["duplicate_candidate_code_days"] = len(duplicate_keys)
    with gzip.open(raw_path, "rt", encoding="utf-8", newline="\n") as source, gzip.open(candidate_path, "wt", encoding="utf-8", newline="\n") as target:
        for line in source:
            row = json.loads(line)
            if _candidate_key(row) in duplicate_keys:
                continue
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            summary["candidate_code_days"] += 1
    raw_path.unlink(missing_ok=True)
    summary["candidate_sha256"] = _sha256(candidate_path)
    summary["status"] = "complete" if state.get("status") == "complete" else "blocked"
    if state.get("status") != "complete":
        summary["errors"].append(f"daily_state={state.get('status')}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2009)
    parser.add_argument("--daily-root", type=Path, default=runtime_path("segment_2000_2010"))
    parser.add_argument("--legacy-daily-root", type=Path, default=runtime_path("ocean_vps_local_stage"))
    parser.add_argument("--stage-summary", type=Path, default=runtime_path("segment_2000_2010", "stage_summary_2005_2010.json"))
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    if args.start_year < 2000 or args.end_year > 2009 or args.start_year > args.end_year:
        raise SystemExit("range must be within 2000-2009")
    run_id = args.run_id or f"pre2010_quality_gate_{datetime.now(BUSINESS_TZ):%Y%m%d_%H%M%S}"
    output_root = args.output_root or runtime_path("pre2010_quality_gate", run_id)
    output_root.mkdir(parents=True, exist_ok=True)
    stages = _stage_summary(args.stage_summary)
    years: list[dict[str, Any]] = []
    for year in range(args.start_year, args.end_year + 1):
        result = summarize_year(
            year,
            daily_roots=_daily_roots(args.daily_root, args.legacy_daily_root, year),
            output_root=output_root,
            stage_evidence=stages.get(year),
        )
        (output_root / f"year_{year}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        years.append(result)
    summary = {
        "schema_version": "pre2010-quality-gate-v1",
        "run_id": run_id,
        "generated_at": datetime.now(BUSINESS_TZ).isoformat(timespec="seconds"),
        "scope": {"start_year": args.start_year, "end_year": args.end_year},
        "output_root": str(output_root),
        "formal_authorized": False,
        "formal_clickhouse_written": False,
        "period_derivation": {
            "input": "accepted 5m code-day only",
            "expected_rows_per_code_day": {label: EXPECTED_BAR_COUNT // factor for label, factor in PERIOD_FACTORS.items()},
            "requires_unique_datetime": True,
            "requires_positive_ohlcv": True,
            "requires_units_contract": EXPECTED_UNITS,
            "requires_daily_gate": True,
            "writes_performed": False,
        },
        "years": years,
        "totals": {
            "candidate_code_days": sum(int(row.get("candidate_code_days", 0)) for row in years),
            "review_evidence_rows": sum(int(row.get("review_evidence_rows", 0)) for row in years),
            "rejected_evidence_rows": sum(int(row.get("rejected_evidence_rows", 0)) for row in years),
        },
    }
    (output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
