from __future__ import annotations

"""Read-only annual audit for Ocean/Harry 5m archives.

This command deliberately does not import a ClickHouse writer or create a
stage table.  It streams one CSV member at a time, writes only evidence under
the caller supplied output directory, reconciles to the purchased daily
snapshot, and (optionally) performs a read-only formal-key count query.
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
import re
import sys
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.ocean_stage import EXPECTED_HEADER, STANDARD_5M_MINUTES
from utils.purchased_5m_reconciliation import PRICE_RELATIVE_TOLERANCE, RELATIVE_TOLERANCE

EXPECTED_SLOTS = set(STANDARD_5M_MINUTES)
CODE_RE = re.compile(r"^(sh|sz|bj)(\d{6})$", re.IGNORECASE)
ARCHIVE_RE = re.compile(r"^(20\d{2})_5min\.zip$", re.IGNORECASE)


def canonical_code(value: str) -> str | None:
    match = CODE_RE.fullmatch(value.strip())
    return f"{match.group(2)}.{match.group(1).upper()}" if match else None


def finite_float(value: str) -> float | None:
    try:
        result = float(value.strip().replace(",", ""))
    except (AttributeError, TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def rel_diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return abs(left - right) / max(abs(right), 1.0)


def reason_category(reason: str) -> str:
    """Collapse dynamic evidence values into stable counters."""
    for token in (part.strip() for part in reason.split(";")):
        if not token:
            continue
        if token.startswith("standard_5m_slots_mismatch"):
            return "standard_5m_slots_mismatch"
        if token.startswith("daily_snapshot="):
            return "daily_snapshot_quality"
        if token.startswith("price_relative_diff="):
            return "price_relative_diff"
        if token.startswith("volume_relative_diff="):
            return "volume_relative_diff"
        if token.startswith("amount_relative_diff="):
            return "amount_relative_diff"
        if token.startswith("accepted_bars="):
            return "accepted_bars"
        if token.startswith("unique_datetime="):
            return "unique_datetime"
        if token.startswith("invalid_rows="):
            return "invalid_rows"
        if token.startswith("duplicate_datetime_rows="):
            return "duplicate_datetime_rows"
        if token.startswith("daily_snapshot_missing"):
            return "daily_snapshot_missing"
        return token.split("=", 1)[0]
    return "unknown"


@dataclass
class DayStats:
    source_rows: int = 0
    valid_ohlc_rows: int = 0
    accepted_rows: int = 0
    invalid_rows: int = 0
    quantity_invalid_rows: int = 0
    duplicate_datetime_rows: int = 0
    slots: Counter = field(default_factory=Counter)
    first_dt: str | None = None
    last_dt: str | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume_lots: float = 0.0
    amount_yuan: float = 0.0
    quantity_rows: int = 0
    file_errors: list[str] = field(default_factory=list)

    def add(self, dt: datetime, open_: float, high: float, low: float, close: float,
            volume: float | None, amount: float | None) -> None:
        self.source_rows += 1
        key = dt.strftime("%H:%M")
        self.slots[key] += 1
        if self.slots[key] > 1:
            self.duplicate_datetime_rows += 1
        self.valid_ohlc_rows += 1
        if self.first_dt is None or dt.isoformat() < self.first_dt:
            self.first_dt = dt.isoformat()
            self.open = open_
        if self.last_dt is None or dt.isoformat() > self.last_dt:
            self.last_dt = dt.isoformat()
            self.close = close
        self.high = high if self.high is None else max(self.high, high)
        self.low = low if self.low is None else min(self.low, low)
        quantity_bad = volume is None or amount is None or volume < 0 or amount < 0 or not float(volume).is_integer()
        if quantity_bad:
            self.invalid_rows += 1
            self.quantity_invalid_rows += 1
            return
        self.accepted_rows += 1
        self.quantity_rows += 1
        self.volume_lots += volume
        self.amount_yuan += amount


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_daily(root: Path, year: int) -> tuple[dict[tuple[str, date], dict[str, Any]], int]:
    """Load only the requested parquet year and detect duplicate daily keys."""
    import pyarrow.parquet as pq

    path = root / f"trade_year={year}" / "part-00000.parquet"
    if not path.is_file():
        return {}, 0
    columns = ["code", "trade_date", "security_type", "quality_status", "open", "high", "low", "close", "volume_lots", "amount_yuan"]
    result: dict[tuple[str, date], dict[str, Any]] = {}
    duplicates = 0
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=100_000, columns=columns):
        data = batch.to_pydict()
        for index, code in enumerate(data["code"]):
            trade_date = data["trade_date"][index]
            key = (str(code), trade_date)
            if key in result:
                duplicates += 1
                continue
            result[key] = {name: data[name][index] for name in columns[2:]}
    return result, duplicates


def audit_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, archive_year: int) -> tuple[str, dict[str, DayStats], list[str]]:
    source_entry = info.filename.replace("\\", "/")
    source_code = Path(source_entry).stem.rsplit("_", 1)[0]
    code = canonical_code(source_code)
    entry_errors: list[str] = []
    if not code:
        entry_errors.append("unclassified_source_code")
    if not re.search(r"_(20\d{2})\.csv$", source_entry, re.IGNORECASE):
        entry_errors.append("entry_name_missing_year")
    else:
        entry_year = int(re.search(r"_(20\d{2})\.csv$", source_entry, re.IGNORECASE).group(1))
        if entry_year != archive_year:
            entry_errors.append("entry_year_mismatch")
    stats: dict[str, DayStats] = {}
    with archive.open(info, "r") as binary:
        text = None
        try:
            text = __import__("io").TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            reader = csv.reader(text)
            header = next(reader, [])
            if tuple(header) != EXPECTED_HEADER:
                entry_errors.append("unsupported_header")
            else:
                for cells in reader:
                    if len(cells) != len(EXPECTED_HEADER):
                        entry_errors.append("invalid_row_shape")
                        continue
                    try:
                        dt = datetime.strptime(cells[0].strip(), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        entry_errors.append("invalid_timestamp")
                        continue
                    day = stats.setdefault(dt.date().isoformat(), DayStats())
                    if dt.year != archive_year:
                        day.invalid_rows += 1
                        day.file_errors.append("datetime_year_mismatch")
                        continue
                    if code is None or cells[1].strip().lower() != source_code.lower():
                        day.invalid_rows += 1
                        day.file_errors.append("csv_code_does_not_match_filename")
                        continue
                    values = [finite_float(cells[i]) for i in (3, 4, 5, 6)]
                    if any(value is None or value <= 0 for value in values):
                        day.source_rows += 1
                        day.invalid_rows += 1
                        day.file_errors.append("positive_ohlc_failed")
                        continue
                    open_, close, high, low = [float(value) for value in values]
                    if high < max(open_, close) or low > min(open_, close):
                        day.source_rows += 1
                        day.invalid_rows += 1
                        day.file_errors.append("ohlc_relation_failed")
                        continue
                    day.add(dt, open_, high, low, close, finite_float(cells[7]), finite_float(cells[8]))
        finally:
            if text is not None:
                text.detach()
            # Reading to EOF is required for ZipExtFile CRC verification.
    return code or source_code, stats, sorted(set(entry_errors))


def reconcile(code: str, trade_date: date, stats: DayStats, daily: dict[str, Any] | None,
              entry_errors: list[str]) -> dict[str, Any]:
    actual_slots = set(stats.slots)
    expected_slots = {f"{m // 60:02d}:{m % 60:02d}" for m in EXPECTED_SLOTS}
    reasons: list[str] = []
    if entry_errors:
        reasons.extend(entry_errors)
    if stats.accepted_rows != 48:
        reasons.append(f"accepted_bars={stats.accepted_rows}; expected=48")
    if len(actual_slots) != 48:
        reasons.append(f"unique_datetime={len(actual_slots)}; expected=48")
    if stats.duplicate_datetime_rows:
        reasons.append(f"duplicate_datetime_rows={stats.duplicate_datetime_rows}")
    if stats.invalid_rows:
        reasons.append(f"invalid_rows={stats.invalid_rows}")
    if actual_slots != expected_slots:
        reasons.append(f"standard_5m_slots_mismatch; missing={sorted(expected_slots - actual_slots)}; unexpected={sorted(actual_slots - expected_slots)}")
    price_diff = volume_diff = amount_diff = None
    if daily is None:
        reasons.append("daily_snapshot_missing")
    else:
        if daily.get("quality_status") != "valid":
            reasons.append(f"daily_snapshot={daily.get('quality_status')}")
        for field_name in ("open", "high", "low", "close"):
            value = daily.get(field_name)
            if value is None or float(value) <= 0:
                reasons.append(f"daily_{field_name}_invalid")
        daily_prices = (daily.get("open"), daily.get("high"), daily.get("low"), daily.get("close"))
        minute_prices = (stats.open, stats.high, stats.low, stats.close)
        diffs = [rel_diff(float(a), float(b)) for a, b in zip(minute_prices, daily_prices) if a is not None and b is not None]
        price_diff = max(diffs) if len(diffs) == 4 else None
        if price_diff is None or price_diff > PRICE_RELATIVE_TOLERANCE:
            reasons.append(f"price_relative_diff={price_diff}")
        volume_diff = rel_diff(stats.volume_lots, daily.get("volume_lots"))
        amount_diff = rel_diff(stats.amount_yuan, daily.get("amount_yuan"))
        if volume_diff is None or volume_diff > RELATIVE_TOLERANCE:
            reasons.append(f"volume_relative_diff={volume_diff}")
        if amount_diff is None or amount_diff > RELATIVE_TOLERANCE:
            reasons.append(f"amount_relative_diff={amount_diff}")
    return {
        "code": code, "trade_date": trade_date.isoformat(),
        "source_rows": stats.source_rows, "accepted_rows": stats.accepted_rows,
        "valid_ohlc_rows": stats.valid_ohlc_rows, "invalid_rows": stats.invalid_rows,
        "quantity_invalid_rows": stats.quantity_invalid_rows,
        "unique_datetime_rows": len(actual_slots), "duplicate_datetime_rows": stats.duplicate_datetime_rows,
        "actual_slots": sorted(actual_slots), "expected_slots": sorted(expected_slots),
        "minute_open": stats.open, "minute_high": stats.high, "minute_low": stats.low, "minute_close": stats.close,
        "minute_volume_lots": stats.volume_lots, "minute_amount_yuan": stats.amount_yuan,
        "daily_present": daily is not None, "daily_quality_status": daily.get("quality_status") if daily else "missing",
        "daily_open": daily.get("open") if daily else None, "daily_high": daily.get("high") if daily else None,
        "daily_low": daily.get("low") if daily else None, "daily_close": daily.get("close") if daily else None,
        "daily_volume_lots": daily.get("volume_lots") if daily else None, "daily_amount_yuan": daily.get("amount_yuan") if daily else None,
        "price_relative_diff": price_diff, "volume_relative_diff": volume_diff, "amount_relative_diff": amount_diff,
        "status": "pass" if not reasons else "review", "reason": "; ".join(reasons),
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def audit_year(year: int, archive_path: Path, daily_root: Path, out_root: Path, formal: bool) -> dict[str, Any]:
    year_root = out_root / str(year)
    year_root.mkdir(parents=True, exist_ok=True)
    marker_path = archive_path.with_name(archive_path.name + ".verified.json")
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.is_file() else {}
    archive_sha = sha256_file(archive_path)
    marker_sha = marker.get("sha256") or marker.get("archive_sha256")
    daily, daily_duplicates = load_daily(daily_root, year)
    entry_summaries: list[dict[str, Any]] = []
    crc_verified = 0
    csv_entries = 0
    pass_keys: set[tuple[str, str]] = set()
    audit_days = pass_days = review_days = 0
    reason_categories: Counter = Counter()
    all_path = year_root / "day_audit.jsonl.gz"
    review_path = year_root / "review.jsonl.gz"
    # Keep only compact pass keys in memory.  The per-day evidence is written
    # immediately; retaining hundreds of thousands of dicts can exceed 2 GiB.
    with gzip.open(all_path, "wt", encoding="utf-8", newline="\n") as all_handle, gzip.open(review_path, "wt", encoding="utf-8", newline="\n") as review_handle, zipfile.ZipFile(archive_path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if not info.filename.lower().endswith(".csv"):
                with archive.open(info, "r") as stream:
                    while stream.read(1024 * 1024):
                        pass
                crc_verified += 1
                continue
            csv_entries += 1
            code, stats_map, entry_errors = audit_entry(archive, info, year)
            entry_day_count = 0
            for trade_date_text, stats in sorted(stats_map.items()):
                item = reconcile(code, date.fromisoformat(trade_date_text), stats, daily.get((code, date.fromisoformat(trade_date_text))), entry_errors)
                line = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                all_handle.write(line + "\n")
                audit_days += 1
                if item["status"] == "pass":
                    pass_days += 1
                    pass_keys.add((item["code"], item["trade_date"]))
                else:
                    review_days += 1
                    review_handle.write(line + "\n")
                    reason_categories[reason_category(item["reason"])] += 1
                entry_day_count += 1
            entry_summaries.append({"source_entry": info.filename, "code": code, "day_count": entry_day_count, "entry_errors": entry_errors, "file_size": info.file_size, "crc": info.CRC})
            crc_verified += 1
    formal_diff: dict[str, Any] = {"status": "not_requested", "read_only": True}
    if formal:
        try:
            client = clickhouse_client()
            query = f"SELECT code,toDate(datetime) AS d,count() AS n FROM stock.kline_minute_5 WHERE datetime >= toDateTime('{year}-01-01 00:00:00') AND datetime < toDateTime('{year+1}-01-01 00:00:00') GROUP BY code,d SETTINGS max_threads=1,max_memory_usage=2000000000"
            actual = {(str(row[0]), str(row[1])): int(row[2]) for row in client.query(query).result_rows}
            partial = sorted(({"code": code, "trade_date": trade_date, "formal_rows": actual.get((code, trade_date), 0), "expected_rows": 48} for code, trade_date in pass_keys if actual.get((code, trade_date), 0) != 48), key=lambda x: (x["code"], x["trade_date"]))
            formal_diff = {"status": "checked", "read_only": True, "expected_pass_code_days": len(pass_keys), "formal_code_days": len(actual), "missing_or_partial_code_days": len(partial), "missing_or_partial": partial[:10000], "truncated": len(partial) > 10000}
        except Exception as exc:
            formal_diff = {"status": "failed", "read_only": True, "error": f"{type(exc).__name__}: {exc}"}
    summary = {
        "year": year, "archive": str(archive_path), "archive_size": archive_path.stat().st_size,
        "marker": str(marker_path), "marker_sha256": marker_sha, "local_sha256": archive_sha,
        "archive_sha_match": bool(marker_sha and str(marker_sha) == archive_sha),
        "marker_verified_at": marker.get("verified_at"), "csv_entries": csv_entries, "crc_verified_entries": crc_verified,
        "daily_snapshot": str(daily_root / f"trade_year={year}" / "part-00000.parquet"), "daily_snapshot_rows": len(daily), "daily_duplicate_keys": daily_duplicates,
        "audit_days": audit_days, "pass_days": pass_days, "review_days": review_days,
        "formal_authorized": False, "formal_diff": formal_diff,
        "reason_categories": dict(reason_categories),
        "evidence_files": {"day_audit": str(all_path), "review": str(review_path)},
    }
    write_json(year_root / "entry_summary.json", entry_summaries)
    write_json(year_root / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-root", type=Path, default=Path(r"E:\AStockHistory\harry"))
    parser.add_argument("--daily-root", type=Path, default=Path(r"F:\AiStock\data\data\warehouse\purchased_history\daily_snapshot\purchased_daily_snapshot_20260916_r1"))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--year", action="append", type=int, required=True)
    parser.add_argument("--formal-readonly", action="store_true")
    args = parser.parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)
    summaries = []
    for year in sorted(set(args.year)):
        archive = args.archive_root / f"{year}_5min.zip"
        if not archive.is_file():
            summaries.append({"year": year, "status": "missing_archive", "archive": str(archive)})
            continue
        summaries.append(audit_year(year, archive, args.daily_root, args.output_root, args.formal_readonly))
    manifest = {"segment": "2011-2020", "years": sorted(set(args.year)), "formal_authorized": False, "write": False, "summaries": summaries}
    manifest_payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str).encode("utf-8")
    (args.output_root / "segment_summary.json").write_bytes(manifest_payload)
    (args.output_root / "evidence_sha256.json").write_text(json.dumps({"segment_summary.json": hashlib.sha256(manifest_payload).hexdigest(), **{str(s.get("year")): {"summary": hashlib.sha256((args.output_root / str(s.get("year")) / "summary.json").read_bytes()).hexdigest()} for s in summaries if (args.output_root / str(s.get("year")) / "summary.json").is_file()}}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
