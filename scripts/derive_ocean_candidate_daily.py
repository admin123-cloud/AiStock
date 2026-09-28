from __future__ import annotations

"""Build an isolated 1d candidate from validated Ocean 5m detail.

Only code-days with the canonical 48 five-minute slots are emitted.  The
script is deliberately file-only: it never opens a ClickHouse client and
never mutates a formal table.
"""

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path


EXPECTED_SLOTS = {
    *(f"09:{m:02d}" for m in range(35, 60, 5)),
    *(f"10:{m:02d}" for m in range(0, 60, 5)),
    *(f"11:{m:02d}" for m in range(0, 35, 5)),
    *(f"13:{m:02d}" for m in range(5, 60, 5)),
    *(f"14:{m:02d}" for m in range(0, 60, 5)),
    "15:00",
}
UNITS = "volume_lots/amount_yuan; raw_unadjusted_price"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def number(row: dict, name: str) -> float:
    value = row.get(name)
    return float(value or 0.0)


def process(detail: Path, source_manifest: Path, output_root: Path, year: int) -> dict:
    out_dir = output_root / str(year)
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "candidate_1d.jsonl.gz"
    stats = Counter()
    seen_keys: set[tuple[str, str]] = set()
    current_key: tuple[str, str] | None = None
    rows: list[dict] = []

    def finish(group: list[dict]) -> None:
        nonlocal current_key
        if not group or current_key is None:
            return
        stats["input_groups"] += 1
        code, trade_date = current_key
        slots = {str(row.get("datetime", ""))[11:16] for row in group}
        datetimes = {str(row.get("datetime", "")) for row in group}
        units_ok = all(row.get("units_contract") == UNITS for row in group)
        key = (code, trade_date)
        if key in seen_keys:
            stats["duplicate_code_days"] += 1
            return
        seen_keys.add(key)
        if len(group) != 48 or len(datetimes) != 48 or slots != EXPECTED_SLOTS:
            stats["review_structure"] += 1
            return
        if not units_ok:
            stats["review_units"] += 1
            return
        first = group[0]
        last = group[-1]
        result = {
            "code": code,
            "trade_date": trade_date,
            "datetime": str(last.get("datetime")),
            "open": number(first, "open"),
            "high": max(number(row, "high") for row in group),
            "low": min(number(row, "low") for row in group),
            "close": number(last, "close"),
            "volume_lots": sum(number(row, "volume_lots") for row in group),
            "amount_yuan": sum(number(row, "amount_yuan") for row in group),
            "source_bars": 48,
            "units_contract": UNITS,
        }
        writer.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
        stats["candidate_rows"] += 1

    with gzip.open(output, "wt", encoding="utf-8", newline="\n") as writer:
        with gzip.open(detail, "rt", encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                key = (str(row.get("code") or ""), str(row.get("datetime") or "")[:10])
                if current_key is None:
                    current_key = key
                elif key != current_key:
                    finish(rows)
                    rows = []
                    current_key = key
                rows.append(row)
            finish(rows)

    materializer = json.loads(source_manifest.read_text(encoding="utf-8"))
    expected_complete = int(materializer.get("accepted_code_days_materialized", 0))
    manifest = {
        "schema_version": "ocean-daily-candidate-v1",
        "year": year,
        "source_5m": str(detail),
        "source_5m_sha256": sha256(detail),
        "source_5m_manifest": str(source_manifest),
        "source_5m_manifest_sha256": sha256(source_manifest),
        "expected_complete_code_days": expected_complete,
        "review_code_days_skipped": materializer.get("review_code_days", {}),
        "stats": dict(stats),
        "output": {"path": str(output), "sha256": sha256(output)},
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "status": "complete" if (
            stats["candidate_rows"] == expected_complete
            and not stats["review_structure"]
            and not stats["review_units"]
            and not stats["duplicate_code_days"]
        ) else "review",
    }
    manifest_path = out_dir / "daily_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--detail", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    if not 2000 <= args.year <= 2009:
        raise SystemExit("year must be within 2000-2009")
    process(args.detail, args.source_manifest, args.output_root, args.year)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
