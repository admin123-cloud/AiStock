from __future__ import annotations

"""Materialize accepted Ocean/Harry 5m code-days into isolated files.

The input repair candidate is code-day evidence only.  This script re-reads
the corresponding verified annual ZIP and writes one gzip JSONL per year under
an independent runtime directory.  It never connects to ClickHouse and never
writes shared stage/formal tables.
"""

import argparse
import csv
import gzip
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile


HEADER = ("时间", "代码", "名称", "开盘价", "收盘价", "最高价", "最低价", "成交量", "成交额")
SLOTS = {
    *(f"09:{m:02d}" for m in range(35, 60, 5)),
    *(f"10:{m:02d}" for m in range(0, 60, 5)),
    *(f"11:{m:02d}" for m in range(0, 35, 5)),
    *(f"13:{m:02d}" for m in range(5, 60, 5)),
    *(f"14:{m:02d}" for m in range(0, 60, 5)),
    *(f"15:{m:02d}" for m in range(0, 5, 5)),
}
SOURCE_RE = re.compile(r"^(sh|sz|bj)(\d{6})_\d{4}\.csv$", re.I)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical(code: str) -> str:
    code = code.strip().upper()
    if "." in code:
        return code
    return f"{code[2:]}.{code[:2]}" if code[:2] in {"SH", "SZ", "BJ"} else code


def load_accepted(path: Path, year: int) -> dict[tuple[str, str], dict]:
    accepted: dict[tuple[str, str], dict] = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row.get("year") or 0) != year:
                continue
            key = (str(row.get("code") or ""), str(row.get("trade_date") or ""))
            accepted[key] = row
    return accepted


def num(value: str) -> float:
    return float(value.strip() or 0)


def materialize(year: int, archive: Path, accepted_path: Path, output_root: Path) -> dict:
    accepted = load_accepted(accepted_path, year)
    out_dir = output_root / str(year)
    out_dir.mkdir(parents=True, exist_ok=True)
    detail = out_dir / "candidate_5m.jsonl.gz"
    stats = Counter()
    by_day: dict[tuple[str, str], Counter] = defaultdict(Counter)
    seen_entries: set[str] = set()
    with ZipFile(archive) as z, gzip.open(detail, "wt", encoding="utf-8", newline="\n") as out:
        for info in z.infolist():
            m = SOURCE_RE.fullmatch(info.filename)
            if not m:
                continue
            source_code = f"{m.group(2)}.{m.group(1).upper()}"
            seen_entries.add(info.filename)
            with z.open(info, "r") as raw:
                text = __import__("io").TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                reader = csv.reader(text)
                try:
                    header = tuple(next(reader)[:9])
                except StopIteration:
                    stats["empty_entries"] += 1
                    continue
                if header != HEADER:
                    stats["unsupported_header_entries"] += 1
                    continue
                for fields in reader:
                    if len(fields) < 9:
                        stats["invalid_rows"] += 1
                        continue
                    try:
                        dt = datetime.strptime(fields[0].strip(), "%Y-%m-%d %H:%M:%S")
                        code = canonical(fields[1]) or source_code
                        trade_date = dt.date().isoformat()
                        key = (code, trade_date)
                        if key not in accepted:
                            continue
                        slot = dt.strftime("%H:%M")
                        row = {
                            "code": code,
                            "datetime": dt.isoformat(sep=" "),
                            "open": num(fields[3]),
                            "close": num(fields[4]),
                            "high": num(fields[5]),
                            "low": num(fields[6]),
                            "volume_lots": num(fields[7]),
                            "amount_yuan": num(fields[8]),
                            "source_entry": info.filename,
                            "units_contract": "volume_lots/amount_yuan; raw_unadjusted_price",
                        }
                        out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                        stats["rows"] += 1
                        d = by_day[key]
                        d["rows"] += 1
                        d[f"slot:{slot}"] += 1
                        d["volume_lots"] += row["volume_lots"]
                        d["amount_yuan"] += row["amount_yuan"]
                    except (TypeError, ValueError):
                        stats["invalid_rows"] += 1
    missing_entries = sum(1 for key in accepted if key[0].split(".")[0] not in {""})
    review = Counter()
    for key, expected in accepted.items():
        actual = by_day.get(key, Counter())
        slots = {k[5:] for k in actual if k.startswith("slot:")}
        if actual.get("rows", 0) != 48 or slots != SLOTS:
            review["structure"] += 1
            continue
        if int(round(actual["rows"])) != int(expected.get("minute_rows") or 48):
            review["row_count"] += 1
        else:
            stats["accepted_code_days"] += 1
    manifest = {
        "schema_version": "ocean-5m-detail-candidate-v1",
        "year": year,
        "archive": str(archive),
        "archive_sha256": sha256(archive),
        "accepted_source": str(accepted_path),
        "accepted_source_sha256": sha256(accepted_path),
        "candidate_file": str(detail),
        "candidate_sha256": sha256(detail),
        "accepted_code_days_input": len(accepted),
        "accepted_code_days_materialized": int(stats["accepted_code_days"]),
        "rows_materialized": int(stats["rows"]),
        "review_code_days": dict(review),
        "stats": dict(stats),
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "source_entries_seen": len(seen_entries),
        "status": "complete" if not review else "review",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--accepted", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    args = p.parse_args()
    if not 2000 <= args.year <= 2009:
        raise SystemExit("year must be within 2000-2009")
    print(json.dumps(materialize(args.year, args.archive, args.accepted, args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
