from __future__ import annotations

"""Derive isolated 15m/30m/60m files from one validated 5m candidate."""

import argparse
import gzip
import hashlib
import json
from collections import defaultdict
from pathlib import Path


EXPECTED_SLOTS = {
    *(f"09:{m:02d}" for m in range(35, 60, 5)),
    *(f"10:{m:02d}" for m in range(0, 60, 5)),
    *(f"11:{m:02d}" for m in range(0, 35, 5)),
    *(f"13:{m:02d}" for m in range(5, 60, 5)),
    *(f"14:{m:02d}" for m in range(0, 60, 5)),
    *(f"15:{m:02d}" for m in range(0, 5, 5)),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def emit(group: list[dict], minutes: int, out) -> None:
    if not group:
        return
    first = group[0]
    # Standard slots begin at 09:35 and 13:05.  Group by contiguous input
    # order, which is already validated as exactly the 48 canonical slots.
    idx = int(first["_index"])
    bucket = idx // (48 if minutes == 1 else minutes // 5)
    row = {
        "code": first["code"],
        # Match the existing ClickHouse derivation contract: bucket timestamp
        # is the closing 5m bar (e.g. 09:45 for the first 15m bucket), not the
        # session-start timestamp.
        "datetime": group[-1]["datetime"],
        "open": first["open"],
        "high": max(x["high"] for x in group),
        "low": min(x["low"] for x in group),
        "close": group[-1]["close"],
        "volume_lots": sum(x["volume_lots"] for x in group),
        "amount_yuan": sum(x["amount_yuan"] for x in group),
        "source_bars": len(group),
        "bucket_index": bucket,
        "units_contract": "volume_lots/amount_yuan; raw_unadjusted_price",
    }
    out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def process(detail: Path, output_root: Path, year: int) -> dict:
    out_dir = output_root / str(year)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {m: out_dir / f"candidate_{m}.jsonl.gz" for m in (15, 30, 60, 1)}
    handles = {m: gzip.open(p, "wt", encoding="utf-8", newline="\n") for m, p in outputs.items()}
    counts = defaultdict(int)
    review_code_days = defaultdict(int)
    input_code_days = 0
    current_key = None
    index = 0

    def finish(rows: list[dict]) -> None:
        nonlocal input_code_days
        if not rows:
            return
        input_code_days += 1
        # The materializer intentionally preserves incomplete Ocean days as
        # review evidence.  Never derive 15m/30m/60m from those rows: doing so
        # would create apparently valid bars with fewer than the required
        # 5m inputs and could silently turn a source review into a pass.
        slots = {str(row.get("datetime", ""))[11:16] for row in rows}
        unique_datetimes = {str(row.get("datetime", "")) for row in rows}
        if len(rows) != 48 or len(unique_datetimes) != 48 or slots != EXPECTED_SLOTS:
            review_code_days["structure"] += 1
            return
        for minutes, handle in handles.items():
            size = 48 if minutes == 1 else minutes // 5
            for start in range(0, 48, size):
                emit(rows[start : start + size], minutes, handle)
                counts[minutes] += 1

    try:
        with gzip.open(detail, "rt", encoding="utf-8") as src:
            rows: list[dict] = []
            for line in src:
                if not line.strip():
                    continue
                row = json.loads(line)
                key = (str(row.get("code")), str(row.get("datetime", ""))[:10])
                if current_key is None:
                    current_key = key
                if key != current_key:
                    finish(rows)
                    rows = []
                    current_key = key
                dt = str(row["datetime"])
                hhmm = dt[11:16]
                # Map canonical slot sequence to an index.  The validated 5m
                # stream is ordered; index is reset for each code-day.
                row["_index"] = len(rows)
                rows.append(row)
            finish(rows)
    finally:
        for handle in handles.values():
            handle.close()

    manifest = {
        "schema_version": "ocean-derived-candidate-v1",
        "year": year,
        "source_5m": str(detail),
        "source_5m_sha256": sha256(detail),
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "input_code_days": int(input_code_days),
        "review_code_days": dict(review_code_days),
        "derived_rows": dict(counts),
        "outputs": {str(m): {"path": str(p), "sha256": sha256(p)} for m, p in outputs.items()},
        "status": "complete",
    }
    (out_dir / "derived_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--detail", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    args = p.parse_args()
    process(args.detail, args.output_root, args.year)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
