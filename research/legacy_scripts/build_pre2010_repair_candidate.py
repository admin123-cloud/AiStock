from __future__ import annotations

"""Build an isolated 2000--2009 repair candidate under the approved policy.

This is a read-only consumer of completed Ocean daily-reconciliation evidence.
It treats small pre-2010 amount differences as acceptable (default <=1%),
while keeping missing 5m slots, invalid OHLC, excessive volume differences,
and price conflicts in review.  It never connects to ClickHouse and never
overwrites a formal table.
"""

import argparse
import gzip
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.minute_daily_quality_gate import EXPECTED_BAR_COUNT, EXPECTED_UNITS
from utils.paths import runtime_path
from utils.purchased_5m_reconciliation import PRICE_RELATIVE_TOLERANCE, RELATIVE_TOLERANCE

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DAILY_SCHEMA = "ocean-daily-reconciliation-v1"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else path.open
    with opener(path, "rt", encoding="utf-8", newline="\n") as f:
        for line in f:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value


def find_year_root(year: int, roots: list[Path]) -> Path | None:
    for root in roots:
        for name in (f"daily_reconciliation_{year}", f"daily_reconciliation_{year}_fast"):
            candidate = root / name
            state = candidate / "state.json"
            if candidate.is_dir() and state.is_file():
                try:
                    payload = json.loads(state.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if payload.get("status") == "complete":
                    return candidate
    return None


def manifest_paths(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in sorted(root.rglob("manifest.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("schema_version") == DAILY_SCHEMA:
            result.append(path)
    return result


def acceptable(row: dict[str, Any], amount_tolerance: float) -> tuple[bool, str]:
    if row.get("structure_pass") is not True:
        return False, "structure"
    if row.get("daily_gate_pass") is not True:
        return False, "daily_gate"
    if int(row.get("minute_rows") or 0) != EXPECTED_BAR_COUNT:
        return False, "minute_rows"
    if int(row.get("unique_datetime_rows") or 0) != EXPECTED_BAR_COUNT:
        return False, "unique_datetime"
    if int(row.get("daily_snapshot_rows") or 0) != 1:
        return False, "daily_snapshot"
    if str(row.get("units_contract") or "") != f"{EXPECTED_UNITS}; raw_unadjusted_price":
        return False, "units_contract"
    price = row.get("price_relative_diff")
    if price is None or float(price) > PRICE_RELATIVE_TOLERANCE:
        return False, "price"
    volume = row.get("volume_relative_diff")
    if volume is None or float(volume) > RELATIVE_TOLERANCE:
        return False, "volume"
    amount = row.get("amount_relative_diff")
    if amount is None or float(amount) > amount_tolerance:
        return False, "amount"
    return True, "accepted_amount_policy"


def trim(row: dict[str, Any], *, year: int, evidence: str) -> dict[str, Any]:
    return {
        "year": year,
        "code": row.get("code"),
        "trade_date": row.get("trade_date"),
        "source_entry": row.get("source_entry"),
        "batch_id": row.get("batch_id"),
        "minute_rows": row.get("minute_rows"),
        "unique_datetime_rows": row.get("unique_datetime_rows"),
        "minute_volume_lots": row.get("minute_volume_lots"),
        "daily_volume_lots": row.get("daily_volume_lots"),
        "minute_amount_yuan": row.get("minute_amount_yuan"),
        "daily_amount_yuan": row.get("daily_amount_yuan"),
        "price_relative_diff": row.get("price_relative_diff"),
        "volume_relative_diff": row.get("volume_relative_diff"),
        "amount_relative_diff": row.get("amount_relative_diff"),
        "amount_unit_status": row.get("amount_unit_status", "accepted_yuan_by_user_policy"),
        "units_contract": row.get("units_contract"),
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compat-2000-root", type=Path, required=True)
    parser.add_argument("--daily-root", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--amount-tolerance", type=float, default=0.01)
    args = parser.parse_args()
    if args.amount_tolerance < 0:
        raise SystemExit("amount_tolerance_must_be_non_negative")
    args.output_root.mkdir(parents=True, exist_ok=True)

    years: list[dict[str, Any]] = []
    total = Counter()
    seen: set[tuple[str, str]] = set()
    duplicate = 0

    for year in range(2000, 2010):
        year_dir = args.output_root / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = year_dir / "candidate.jsonl.gz"
        review_path = year_dir / "review.jsonl.gz"
        accepted = 0
        reviews = 0
        accepted_amount_review = 0
        reasons = Counter()
        local_seen: set[tuple[str, str]] = set()
        with gzip.open(candidate_path, "wt", encoding="utf-8", newline="\n") as out, gzip.open(review_path, "wt", encoding="utf-8", newline="\n") as review_out:
            evidence_roots: list[tuple[str, Path, Path | None]] = []
            if year == 2000:
                evidence_roots.append(("compat_2000", args.compat_2000_root, None))
            else:
                evidence_roots.append(("daily_reconciliation", args.daily_root[0], find_year_root(year, args.daily_root)))

            for evidence_name, _, root in evidence_roots:
                if year == 2000:
                    files = [(args.compat_2000_root / "pass.jsonl.gz", args.compat_2000_root / "review.jsonl.gz")]
                elif root is None:
                    files = []
                else:
                    files = []
                    for manifest in manifest_paths(root):
                        payload = json.loads(manifest.read_text(encoding="utf-8"))
                        files.append((manifest.parent / str(payload.get("pass_file")), manifest.parent / str(payload.get("review_file"))))
                for pass_file, review_file in files:
                    for partition, path in (("pass", pass_file), ("review", review_file)):
                        if not path.is_file():
                            continue
                        for row in iter_jsonl(path):
                            ok, reason = acceptable(row, args.amount_tolerance)
                            key = (str(row.get("code") or ""), str(row.get("trade_date") or ""))
                            if ok:
                                if key in seen or key in local_seen:
                                    duplicate += 1
                                    continue
                                seen.add(key)
                                local_seen.add(key)
                                out.write(json.dumps(trim(row, year=year, evidence=f"{evidence_name}:{partition}"), ensure_ascii=False, separators=(",", ":")) + "\n")
                                accepted += 1
                                total["accepted"] += 1
                                if partition == "review":
                                    accepted_amount_review += 1
                            else:
                                review_out.write(json.dumps({**trim(row, year=year, evidence=f"{evidence_name}:{partition}"), "blocked_reason": reason}, ensure_ascii=False, separators=(",", ":")) + "\n")
                                reviews += 1
                                reasons[reason] += 1
                                total[f"review_{reason}"] += 1
        years.append({
            "year": year,
            "status": "complete" if evidence_roots[0][2] is not None or year == 2000 else "blocked_no_daily_evidence",
            "candidate_rows": accepted,
            "accepted_from_review": accepted_amount_review,
            "review_rows": reviews,
            "review_reasons": dict(reasons),
            "candidate_file": str(candidate_path),
            "candidate_sha256": sha256(candidate_path),
            "review_file": str(review_path),
            "review_sha256": sha256(review_path),
        })

    summary = {
        "schema_version": "pre2010-repair-candidate-v1",
        "run_id": f"pre2010_repair_candidate_{datetime.now(BUSINESS_TZ):%Y%m%dT%H%M%S}",
        "scope": {"start_year": 2000, "end_year": 2009},
        "amount_tolerance": args.amount_tolerance,
        "price_tolerance": PRICE_RELATIVE_TOLERANCE,
        "volume_tolerance": RELATIVE_TOLERANCE,
        "source_precedence": "complete_48_slot_evidence_first; conflicts_remain_review",
        "formal_authorized": False,
        "formal_clickhouse_written": False,
        "duplicate_code_days_skipped": duplicate,
        "totals": dict(total),
        "years": years,
    }
    (args.output_root / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
