"""Audit recovery of pre-2010 purchased 5m gaps from raw purchased 1m ZIPs.

This is deliberately read-only.  The existing cross-validation script targets
ReplayMaster columns (``<DTYYYYMMDD>``, ``<TIME>``, ...), while the purchased
1m package uses Chinese CSV columns (``时间``, ``开盘价`` ...).  This script
parses the purchased package directly and reports whether the 48 expected 5m
buckets can be rebuilt from complete 1m rows.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import report_path


DEFAULT_RUN_ID = "purchased_history_review_20260918_5m_source_closeout_v13"
DEFAULT_HS_ROOT = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\1分钟_按年汇总")
DEFAULT_INDEX_ROOT = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_指数\1分钟_按年汇总")


def expected_5m_ends() -> tuple[int, ...]:
    def labels(start: int, end: int) -> tuple[int, ...]:
        values: list[int] = []
        for total in range(start, end + 1, 5):
            values.append((total // 60) * 100 + total % 60)
        return tuple(values)

    return labels(9 * 60 + 35, 11 * 60 + 30) + labels(13 * 60 + 5, 15 * 60)


def _hhmm(value: int) -> int:
    return (value // 100) * 60 + value % 100


def _parse_number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() in {"nan", "inf", "-inf"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _source_entry(code: str, year: int) -> str:
    symbol, market = code.split(".", 1)
    # The purchased stock archive prefixes entries with ``sh``/``sz``;
    # the purchased index archive uses the bare index code.
    if symbol.startswith("399") or market == "SH" and symbol.startswith("000"):
        return f"{symbol}_{year}.csv"
    return f"{market.lower()}{symbol}_{year}.csv"


def _parse_datetime(value: Any) -> tuple[date, int] | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date(), parsed.hour * 100 + parsed.minute
        except ValueError:
            continue
    return None


def _read_target_day(archive: ZipFile, entry: str, target: date) -> tuple[list[tuple[int, float, float, float, float, float]], int]:
    rows: list[tuple[int, float, float, float, float, float]] = []
    invalid = 0
    try:
        raw = archive.open(entry)
    except KeyError:
        return rows, invalid
    with raw:
        reader = csv.DictReader((line.decode("utf-8-sig", errors="replace") for line in raw))
        for item in reader:
            parsed = _parse_datetime(item.get("时间") or item.get("datetime") or item.get("日期"))
            if not parsed or parsed[0] != target:
                continue
            stamp, minute = parsed
            volume_value = item.get("成交量")
            # Index archives expose 成交额 but not 成交量.  For recovery
            # eligibility the OHLC path is sufficient; keep index volume at
            # zero rather than treating a valid index bar as malformed.
            values = (
                _parse_number(item.get("开盘价")),
                _parse_number(item.get("最高价")),
                _parse_number(item.get("最低价")),
                _parse_number(item.get("收盘价")),
                0.0 if volume_value in (None, "") else _parse_number(volume_value),
            )
            if any(value is None for value in values) or any(value <= 0 for value in values[:4]) or values[4] < 0:
                invalid += 1
                continue
            rows.append((minute, *(float(value) for value in values)))
    return rows, invalid


def _derive(rows: list[tuple[int, float, float, float, float, float]]) -> tuple[list[dict[str, Any]], list[int], int]:
    by_minute: dict[int, list[tuple[float, float, float, float, float]]] = defaultdict(list)
    for minute, open_, high, low, close, volume in rows:
        by_minute[minute].append((open_, high, low, close, volume))
    duplicate_count = sum(max(0, len(values) - 1) for values in by_minute.values())
    derived: list[dict[str, Any]] = []
    missing: list[int] = []
    for end in expected_5m_ends():
        end_total = _hhmm(end)
        start_total = 9 * 60 + 30 if end == 935 else end_total - 4
        source_minutes = [((minute // 60) * 100 + minute % 60) for minute in range(start_total, end_total + 1)]
        selected = [by_minute[minute][0] for minute in source_minutes if len(by_minute.get(minute, [])) == 1]
        if len(selected) != len(source_minutes):
            missing.append(end)
            continue
        derived.append(
            {
                "minute": end,
                "open": selected[0][0],
                "high": max(item[1] for item in selected),
                "low": min(item[2] for item in selected),
                "close": selected[-1][3],
                "volume_lots": sum(item[4] for item in selected),
            }
        )
    return derived, missing, duplicate_count


def _queue(client: Any, run_id: str) -> list[tuple[str, date, str, int, int]]:
    rows = client.query(
        """
        SELECT code, trade_date, security_type, any(expected_count), any(actual_count)
        FROM stock.purchased_history_data_review_items
        WHERE run_id = %(run_id)s
          AND issue_type IN ('source_5m_incomplete_bars', 'source_5m_file_invalid_no_datetime')
          AND toYear(trade_date) < 2010
        GROUP BY code, trade_date, security_type
        ORDER BY trade_date, code
        """,
        parameters={"run_id": run_id},
    ).result_rows
    return [(str(code), day, str(kind), int(expected), int(actual)) for code, day, kind, expected, actual in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--hs-root", type=Path, default=DEFAULT_HS_ROOT)
    parser.add_argument("--index-root", type=Path, default=DEFAULT_INDEX_ROOT)
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    client = clickhouse_client()
    queue = _queue(client, args.run_id)[: max(1, args.limit)]
    grouped: dict[tuple[Path, int, str], list[tuple[str, date, str, int, int]]] = defaultdict(list)
    for item in queue:
        code, trade_day, security_type, _, _ = item
        root = args.index_root if security_type == "index" else args.hs_root
        grouped[(root, trade_day.year, security_type)].append(item)

    results: list[dict[str, Any]] = []
    for (root, year, security_type), items in sorted(grouped.items(), key=lambda value: (str(value[0][0]), value[0][1], value[0][2])):
        archive_path = root / f"{year}_1min.zip"
        if not archive_path.exists():
            for code, trade_day, kind, expected, actual in items:
                results.append({"code": code, "trade_date": str(trade_day), "security_type": security_type, "source_entry": _source_entry(code, year), "decision": "source_archive_missing", "expected_count": expected, "actual_count": actual})
            continue
        with ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            for code, trade_day, kind, expected, actual in items:
                entry = _source_entry(code, year)
                rows, invalid = _read_target_day(archive, entry, trade_day) if entry in names else ([], 0)
                derived, missing, duplicates = _derive(rows)
                decision = "recoverable_from_purchased_1m" if len(derived) == 48 and not duplicates else "purchased_1m_incomplete"
                results.append(
                    {
                        "code": code,
                        "trade_date": str(trade_day),
                        "security_type": security_type,
                        "source_entry": entry,
                        "raw_1m_rows": len(rows),
                        "invalid_rows": invalid,
                        "duplicate_1m_rows": duplicates,
                        "derived_5m_rows": len(derived),
                        "missing_5m_ends": missing,
                        "expected_count": expected,
                        "actual_count": actual,
                        "decision": decision,
                    }
                )

    counts: dict[str, int] = defaultdict(int)
    for row in results:
        counts[row["decision"]] += 1
    output = {"run_id": args.run_id, "queue_rows": len(queue), "decision_counts": dict(counts), "results": results}
    report = report_path("purchased_history", "raw_1m_recovery_audit", f"{args.run_id}.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": args.run_id, "queue_rows": len(queue), "decision_counts": dict(counts), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
