from __future__ import annotations

"""Read-only local Ocean day reconciliation and formal-key dryrun.

This command reads only local Ocean manifests/JSONL and the existing Parquet
daily snapshot.  Formal keys may be supplied as a local JSONL export; no
ClickHouse client, candidate table, or formal writer is used.
"""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.ocean_audit import (
    canonical_datetime_key,
    dryrun_missing_formal_keys,
    find_code_date_conflicts,
    iter_local_manifests,
    load_daily_snapshot_day,
    reconcile_stage_rows,
    select_manifest_scope,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--daily-root", type=Path, required=True)
    parser.add_argument(
        "--formal-keys-jsonl",
        type=Path,
        help="Optional read-only JSONL export containing {code,datetime}; never written by this command.",
    )
    parser.add_argument("--batch-id", help="Exact local batch scope; required when multiple batches exist.")
    parser.add_argument("--policy-hash", help="Exact accepted-stock policy hash; required when multiple policies exist.")
    return parser.parse_args()


def _formal_keys(path: Path | None):
    if path is None:
        # Missing export is unknown, not an asserted empty formal table.
        return None
    result = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                result.append((str(value["code"]), str(value["datetime"])))
    return result


def main() -> int:
    args = parse_args()
    formal_keys = _formal_keys(args.formal_keys_jsonl)
    manifests = select_manifest_scope(
        iter_local_manifests(args.local_root), batch_id=args.batch_id, policy_hash=args.policy_hash,
    )
    prepared = []
    all_reconciliations = []
    for manifest in manifests:
        rows = list(manifest.iter_stage_rows())
        day_audits = list(manifest.iter_day_audits())
        days = {
            _as_date(row.get("trade_date") or row.get("datetime")) for row in rows
        } | {
            _as_date(audit.get("trade_date")) for audit in day_audits
        }
        daily = {}
        for trade_date in sorted(days):
            daily.update(load_daily_snapshot_day(args.daily_root, trade_date))
        reconciliations = reconcile_stage_rows(rows, daily, day_audits=day_audits)
        all_reconciliations.extend(reconciliations)
        prepared.append((manifest, rows, reconciliations))

    conflict_keys = set(find_code_date_conflicts(all_reconciliations))
    output = []
    for manifest, rows, reconciliations in prepared:
        pass_keys = {
            (item.code, item.trade_date)
            for item in reconciliations
            if item.overall_status == "pass"
        } - conflict_keys
        candidate_rows = [
            row for row in rows
            if (
                str(row.get("code") or ""),
                _as_date(row.get("trade_date") or row.get("datetime")),
            ) in pass_keys
        ]
        dryrun = dryrun_missing_formal_keys(candidate_rows, formal_keys)
        output.append({
            "manifest": str(manifest.path),
            "archive_sha256": manifest.payload["archive_sha256"],
            "source_entry": manifest.payload["source_entry"],
            "structure_pass": manifest.payload["structure_pass"],
            "formal_authorized": False,
            "reconciliation": [asdict(item) for item in reconciliations],
            "candidate_dryrun": asdict(dryrun),
            "code_date_conflicts": [
                {"code": code, "trade_date": trade_date.isoformat()}
                for code, trade_date in sorted(conflict_keys)
                if any(item.code == code and item.trade_date == trade_date for item in reconciliations)
            ],
        })
    print(json.dumps({
        "write": False,
        "formal_authorized": False,
        "missing_keys_status": "not_checked" if formal_keys is None else "checked",
        "scope": {
            "batch_id": manifests[0].payload["batch_id"],
            "accepted_stock_policy_hash": manifests[0].payload["accepted_stock_policy_hash"],
        },
        "entries": output,
    }, ensure_ascii=False, default=str))
    return 0


def _as_date(value):
    if isinstance(value, datetime):
        return date.fromisoformat(canonical_datetime_key(value)[:10])
    if isinstance(value, date):
        return value
    text = str(value or "")
    if "T" in text or " " in text:
        # This helper is only used for grouping; the audit module performs the
        # canonical Shanghai conversion for actual datetime comparisons.
        return date.fromisoformat(canonical_datetime_key(text)[:10])
    return date.fromisoformat(text[:10])


if __name__ == "__main__":
    raise SystemExit(main())
