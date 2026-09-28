from __future__ import annotations

"""Stream-validate an isolated Ocean 5m detail candidate against code-day evidence."""

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

SLOTS = {
    *(f"09:{m:02d}" for m in range(35, 60, 5)),
    *(f"10:{m:02d}" for m in range(0, 60, 5)),
    *(f"11:{m:02d}" for m in range(0, 35, 5)),
    *(f"13:{m:02d}" for m in range(5, 60, 5)),
    *(f"14:{m:02d}" for m in range(0, 60, 5)),
    "15:00",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1.0)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--detail", type=Path, required=True)
    p.add_argument("--accepted", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    expected = {}
    with gzip.open(args.accepted, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                expected[(str(row.get("code")), str(row.get("trade_date")))] = row
    stats = Counter()
    reviews = Counter()
    seen = set()
    current_key = None
    current = None

    def finish(key, agg):
        if key is None:
            return
        seen.add(key)
        exp = expected.get(key)
        if exp is None:
            reviews["unexpected_code_day"] += 1
            return
        stats["code_days"] += 1
        slots = agg["slots"]
        if agg["rows"] != 48 or slots != SLOTS:
            reviews["structure"] += 1
        else:
            stats["structure_pass"] += 1
        if rel(agg["volume"], float(exp.get("minute_volume_lots") or 0.0)) > 1e-9:
            reviews["volume_total_mismatch"] += 1
        if rel(agg["amount"], float(exp.get("minute_amount_yuan") or 0.0)) > 1e-9:
            reviews["amount_total_mismatch"] += 1
        if agg["units_bad"]:
            reviews["unit_contract"] += 1

    with gzip.open(args.detail, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            key = (str(row.get("code")), str(row.get("datetime", ""))[:10])
            if key != current_key:
                finish(current_key, current or {"rows": 0, "slots": set(), "volume": 0.0, "amount": 0.0, "units_bad": False})
                current_key = key
                current = {"rows": 0, "slots": set(), "volume": 0.0, "amount": 0.0, "units_bad": False}
            current["rows"] += 1
            current["slots"].add(str(row.get("datetime", ""))[11:16])
            current["volume"] += float(row.get("volume_lots") or 0.0)
            current["amount"] += float(row.get("amount_yuan") or 0.0)
            current["units_bad"] |= row.get("units_contract") != "volume_lots/amount_yuan; raw_unadjusted_price"
    finish(current_key, current or {"rows": 0, "slots": set(), "volume": 0.0, "amount": 0.0, "units_bad": False})
    stats["expected_code_days"] = len(expected)
    stats["missing_code_days"] = len(set(expected) - seen)
    result = {
        "schema_version": "ocean-5m-detail-audit-v1",
        "detail": str(args.detail),
        "detail_sha256": sha256(args.detail),
        "accepted": str(args.accepted),
        "accepted_sha256": sha256(args.accepted),
        "stats": dict(stats),
        "review_reasons": dict(reviews),
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "status": "pass" if not reviews and stats["missing_code_days"] == 0 else "review",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
