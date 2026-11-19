from __future__ import annotations

"""Validate isolated Ocean 1d candidates without touching ClickHouse."""

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(path: Path, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = int(manifest["expected_complete_code_days"])
    seen: set[tuple[str, str]] = set()
    counters = Counter()
    units_bad = 0
    bad_bars = 0
    bad_ohlc = 0
    bad_amount = 0
    bad_datetime = 0
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            counters["rows"] += 1
            code = str(row.get("code") or "")
            trade_date = str(row.get("trade_date") or str(row.get("datetime") or "")[:10])
            key = (code, trade_date)
            if key in seen:
                counters["duplicate_keys"] += 1
            seen.add(key)
            if int(row.get("source_bars") or 0) != 48:
                bad_bars += 1
            if row.get("units_contract") != "volume_lots/amount_yuan; raw_unadjusted_price":
                units_bad += 1
            if str(row.get("datetime") or "")[11:19] != "15:00:00":
                bad_datetime += 1
            try:
                if min(float(row.get(name) or 0.0) for name in ("open", "high", "low", "close")) <= 0:
                    bad_ohlc += 1
                if float(row.get("volume_lots") or 0.0) < 0 or float(row.get("amount_yuan") or 0.0) < 0:
                    bad_amount += 1
            except (TypeError, ValueError):
                bad_amount += 1
    counters.update({
        "bad_source_bars": bad_bars,
        "bad_units": units_bad,
        "bad_datetime": bad_datetime,
        "bad_ohlc": bad_ohlc,
        "bad_amount_or_volume": bad_amount,
        "expected_rows": expected,
    })
    status = "complete" if (
        counters["rows"] == expected
        and counters["duplicate_keys"] == 0
        and not bad_bars and not units_bad and not bad_datetime
        and not bad_ohlc and not bad_amount
    ) else "review"
    return {
        "year": int(manifest["year"]),
        "candidate": str(path),
        "candidate_sha256": sha256(path),
        "manifest": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "stats": dict(counters),
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--years", default="2000,2001,2002")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    checks = []
    for year_text in args.years.split(","):
        year = int(year_text.strip())
        year_dir = args.root / str(year)
        checks.append(check(year_dir / "candidate_1d.jsonl.gz", year_dir / "daily_manifest.json"))
    total = sum(int(item["stats"]["rows"]) for item in checks)
    expected = sum(int(item["stats"]["expected_rows"]) for item in checks)
    report = {
        "schema_version": "ocean-daily-local-gate-v1",
        "root": str(args.root),
        "years": checks,
        "total_candidate_rows": total,
        "total_expected_rows": expected,
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "status": "complete" if total == expected and all(x["status"] == "complete" for x in checks) else "review",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
