from __future__ import annotations

"""Cross-validate the remaining purchased-history review queue with source 1m.

This is an audit-only bridge.  It reads the original ReplayMaster 1m ZIPs,
rebuilds the project's 48 daily 5m bars, and compares them with the purchased
5m stage and purchased daily snapshot.  It never writes a formal K-line table.
The 1m source has no amount field, so amount remains explicitly unresolved.
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZipFile
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path, warehouse_path
from utils.purchased_5m_reconciliation import (
    PRICE_RELATIVE_TOLERANCE,
    RELATIVE_TOLERANCE,
    max_price_abs_diff,
    price_relative_diff,
    within_relative_tolerance,
)

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
QUEUE_TABLE = "stock.purchased_history_data_review_items"
RECON_TABLE = "stock.purchased_history_minute_5_daily_snapshot_recon"
DEFAULT_RUN_ID = "purchased_history_review_20260917_snapshot_recon_r3"
DEFAULT_HS_ROOT = Path(
    r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_复盘大师\1分钟_沪深_按年汇总"
)
DEFAULT_INDEX_ROOT = Path(
    r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_复盘大师\1分钟_指数_按年汇总"
)
DEFAULT_BJ_ROOT = Path(
    r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_复盘大师\1分钟_京市_按年汇总"
)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}{minutes % 60:02d}"


EXPECTED_ENDS = (
    tuple(int(_hhmm(minutes)) for minutes in range(9 * 60 + 35, 11 * 60 + 31, 5))
    + tuple(int(_hhmm(minutes)) for minutes in range(13 * 60 + 5, 15 * 60 + 1, 5))
)


def _parse_float(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        result = float(text.replace(",", ""))
    except ValueError:
        return None
    return result if result == result and abs(result) != float("inf") else None


def _source_name(code: str, year: int, security_type: str) -> str:
    symbol, market = code.split(".", 1)
    if security_type == "index":
        return f"{symbol}_{year}.txt"
    return f"{market.lower()}{symbol}_{year}.txt"


def _source_root(code: str, security_type: str, hs_root: Path, index_root: Path, bj_root: Path) -> Path:
    if security_type == "index":
        return index_root
    if code.upper().endswith(".BJ"):
        return bj_root
    return hs_root


def _load_queue(
    client: Any,
    run_id: str,
    years: set[int],
    limit: int,
    security_type: str | None = None,
) -> dict[tuple[str, date], dict[str, Any]]:
    security_filter = "" if not security_type else " AND security_type=%(security_type)s"
    rows = client.query(
        f"""
        SELECT issue_type, code, trade_date, period, security_type, expected_count,
               actual_count, missing_count, source_batch_id, reason
        FROM {QUEUE_TABLE}
        WHERE run_id=%(run_id)s
          AND toYear(trade_date) IN ({','.join(str(year) for year in sorted(years))})
          {security_filter}
        ORDER BY trade_date, code, issue_type
        """,
        parameters={"run_id": run_id, "security_type": security_type},
    ).result_rows
    result: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        key = (str(row[1]), row[2])
        result.setdefault(
            key,
            {
                "issue_type": str(row[0]),
                "code": str(row[1]),
                "trade_date": row[2],
                "period": str(row[3]),
                "security_type": str(row[4]),
                "expected_count": int(row[5]),
                "actual_count": int(row[6]),
                "missing_count": int(row[7]),
                "source_batch_id": str(row[8]),
                "queue_reason": str(row[9] or ""),
            },
        )
    if limit > 0:
        return dict(list(sorted(result.items(), key=lambda item: item[0]))[:limit])
    return result


def _load_reconciliation(client: Any, keys: set[tuple[str, date]], years: set[int]) -> dict[tuple[str, date], dict[str, Any]]:
    """Load the latest snapshot reconciliation rows in bounded year queries."""
    wanted: dict[tuple[str, date], dict[str, Any]] = {}
    for year in sorted(years):
        rows = client.query(
            f"""
            SELECT code, trade_date,
                   argMax(minute_open, checked_at), argMax(minute_high, checked_at),
                   argMax(minute_low, checked_at), argMax(minute_close, checked_at),
                   argMax(minute_volume_lots, checked_at), argMax(minute_amount_yuan, checked_at),
                   argMax(daily_open, checked_at), argMax(daily_high, checked_at),
                   argMax(daily_low, checked_at), argMax(daily_close, checked_at),
                   argMax(daily_volume_lots, checked_at), argMax(daily_amount_yuan, checked_at),
                   argMax(price_status, checked_at), argMax(volume_status, checked_at),
                   argMax(amount_status, checked_at), argMax(overall_status, checked_at)
            FROM {RECON_TABLE}
            WHERE trade_date >= toDate(%(start)s) AND trade_date < toDate(%(end)s)
            GROUP BY code, trade_date
            HAVING argMax(overall_status, checked_at)='review'
            """,
            parameters={"start": f"{year}-01-01", "end": f"{year + 1}-01-01"},
        ).result_rows
        for row in rows:
            key = (str(row[0]), row[1])
            if key not in keys:
                continue
            wanted[key] = {
                "minute_open": row[2], "minute_high": row[3], "minute_low": row[4], "minute_close": row[5],
                "minute_volume_lots": row[6], "minute_amount_yuan": row[7],
                "daily_open": row[8], "daily_high": row[9], "daily_low": row[10], "daily_close": row[11],
                "daily_volume_lots": row[12], "daily_amount_yuan": row[13],
                "price_status": str(row[14]), "volume_status": str(row[15]),
                "amount_status": str(row[16]), "overall_status": str(row[17]),
            }
    return wanted


def _load_stage(
    client: Any,
    queue: dict[tuple[str, date], dict[str, Any]],
) -> dict[tuple[str, date], dict[str, Any]]:
    """Load daily aggregates from the purchased 5m stage for queue keys.

    A few queue rows cite both a date-specific and a monthly batch.  Prefer
    the batches in the queue's listed order so the date-specific source wins
    when both contain the same code-day.
    """
    batch_priority: dict[tuple[str, date], dict[str, int]] = {}
    batches: set[str] = set()
    for key, item in queue.items():
        ordered = [value.strip() for value in item["source_batch_id"].split(",") if value.strip()]
        batch_priority[key] = {batch: index for index, batch in enumerate(ordered)}
        batches.update(ordered)

    result: dict[tuple[str, date], dict[str, Any]] = {}
    selected_priority: dict[tuple[str, date], int] = {}
    for batch_id in sorted(batches):
        rows = client.query(
            """
            SELECT code, toDate(datetime), count(), uniqExact(datetime),
                   argMin(open, datetime), max(high), min(low), argMax(close, datetime),
                   sumOrNull(source_volume_lots), sumOrNull(source_amount_yuan)
            FROM stock.purchased_history_minute_5_stage
            WHERE batch_id=%(batch_id)s
            GROUP BY code, toDate(datetime)
            """,
            parameters={"batch_id": batch_id},
        ).result_rows
        for row in rows:
            key = (str(row[0]), row[1])
            if key not in queue:
                continue
            priority = batch_priority[key].get(batch_id, 10_000)
            if key in selected_priority and priority >= selected_priority[key]:
                continue
            selected_priority[key] = priority
            result[key] = {
                "stage_rows": int(row[2]),
                "stage_unique_minutes": int(row[3]),
                "minute_open": row[4],
                "minute_high": row[5],
                "minute_low": row[6],
                "minute_close": row[7],
                "minute_volume_lots": row[8],
                "minute_amount_yuan": row[9],
            }
    return result


def _read_source_days(
    archive: ZipFile,
    entry_name: str,
    target_dates: set[date],
) -> tuple[dict[date, list[tuple[int, float, float, float, float, float]]], str]:
    by_day: dict[date, list[tuple[int, float, float, float, float, float]]] = defaultdict(list)
    with archive.open(entry_name) as raw:
        text = raw.read().decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        try:
            day = datetime.strptime(str(row.get("<DTYYYYMMDD>") or ""), "%Y%m%d").date()
            if day not in target_dates:
                continue
            minute = int(str(row.get("<TIME>") or ""))
            values = tuple(_parse_float(row.get(column)) for column in ("<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<VOL>"))
            if any(value is None or value <= 0 for value in values[:4]) or values[4] is None or values[4] < 0:
                continue
            by_day[day].append((minute, *(float(value) for value in values)))
        except (TypeError, ValueError):
            continue
    return by_day, entry_name


def _derive_5m(rows: Iterable[tuple[int, float, float, float, float, float]]) -> tuple[list[dict[str, Any]], int, int]:
    by_time: dict[int, list[tuple[float, float, float, float, float]]] = defaultdict(list)
    for minute, open_, high, low, close, volume in rows:
        by_time[minute].append((open_, high, low, close, volume))
    duplicate_minutes = sum(max(0, len(values) - 1) for values in by_time.values())
    derived: list[dict[str, Any]] = []
    for end in EXPECTED_ENDS:
        end_hour, end_minute = divmod(end, 100)
        end_total = end_hour * 60 + end_minute
        start_total = 9 * 60 + 30 if end == 935 else end_total - 4
        source_minutes = [
            int(_hhmm(minute))
            for minute in range(start_total, end_total + 1)
        ]
        selected = [by_time[minute][0] for minute in source_minutes if len(by_time.get(minute, [])) == 1]
        if len(selected) != len(source_minutes):
            continue
        derived.append(
            {
                "minute": end,
                "open": selected[0][0],
                "high": max(value[1] for value in selected),
                "low": min(value[2] for value in selected),
                "close": selected[-1][3],
                "volume_lots": sum(value[4] for value in selected),
                "source_minute_rows": len(selected),
            }
        )
    return derived, len(by_time), duplicate_minutes


def _daily_from_5m(derived: list[dict[str, Any]]) -> dict[str, float] | None:
    if len(derived) != 48:
        return None
    return {
        "open": float(derived[0]["open"]),
        "high": max(float(row["high"]) for row in derived),
        "low": min(float(row["low"]) for row in derived),
        "close": float(derived[-1]["close"]),
        "volume_lots": sum(float(row["volume_lots"]) for row in derived),
    }


def _status_for_prices(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, float | None]:
    left_values = (left["open"], left["high"], left["low"], left["close"])
    right_values = (right["open"], right["high"], right["low"], right["close"])
    absolute = max_price_abs_diff(left_values, right_values)
    ratio = price_relative_diff(absolute, right_values)
    return ("pass" if ratio is not None and ratio <= PRICE_RELATIVE_TOLERANCE else "review"), ratio


def _status_for_volume(value: object, reference: object) -> tuple[str, float | None]:
    if value is None or reference is None:
        return "missing", None
    measured = abs(float(value) - float(reference)) / max(abs(float(reference)), 1.0)
    return ("pass" if measured <= RELATIVE_TOLERANCE else "review"), measured


def _cross_result(
    base: dict[str, Any],
    recon: dict[str, Any] | None,
    stage: dict[str, Any] | None,
    source_entry: str,
    rows: list[tuple[int, float, float, float, float, float]],
) -> dict[str, Any]:
    result = {
        **base,
        "source_entry": source_entry,
        "one_minute_rows": len(rows),
        "one_minute_unique_minutes": len({row[0] for row in rows}),
        "one_minute_duplicate_minutes": 0,
        "derived_5m_rows": 0,
        "derived_5m_unique_minutes": 0,
        "stage_price_status": "missing",
        "stage_price_relative_diff": None,
        "stage_volume_status": "missing",
        "stage_volume_relative_diff": None,
        "daily_price_status": "missing",
        "daily_price_relative_diff": None,
        "daily_volume_status": "missing",
        "daily_volume_relative_diff": None,
        "amount_status_existing": recon.get("amount_status") if recon else "missing",
        "derived_daily_open": None,
        "derived_daily_high": None,
        "derived_daily_low": None,
        "derived_daily_close": None,
        "derived_daily_volume_lots": None,
        "stage_daily_open": stage.get("minute_open") if stage else None,
        "stage_daily_high": stage.get("minute_high") if stage else None,
        "stage_daily_low": stage.get("minute_low") if stage else None,
        "stage_daily_close": stage.get("minute_close") if stage else None,
        "stage_daily_volume_lots": stage.get("minute_volume_lots") if stage else None,
        "stage_rows": stage.get("stage_rows") if stage else None,
        "stage_unique_minutes": stage.get("stage_unique_minutes") if stage else None,
        "decision": "source_missing",
        "reason": "",
    }
    derived, unique_minutes, duplicates = _derive_5m(rows)
    result["one_minute_unique_minutes"] = unique_minutes
    result["one_minute_duplicate_minutes"] = duplicates
    result["derived_5m_rows"] = len(derived)
    result["derived_5m_unique_minutes"] = len({row["minute"] for row in derived})
    stage_source = recon or stage
    if stage_source is None:
        result["decision"] = "no_purchased_5m_stage_row"
        result["reason"] = "queue item has no purchased 5m stage or daily snapshot reconciliation row"
        return result
    if len(derived) != 48:
        result["decision"] = "one_minute_incomplete"
        result["reason"] = f"derived_5m_rows={len(derived)}, expected=48"
        return result

    purchased_5m_daily = {key: stage_source[f"minute_{key}"] for key in ("open", "high", "low", "close")}
    purchased_5m_daily["volume_lots"] = stage_source["minute_volume_lots"]
    derived_daily = _daily_from_5m(derived)
    result["derived_daily_open"] = derived_daily["open"]
    result["derived_daily_high"] = derived_daily["high"]
    result["derived_daily_low"] = derived_daily["low"]
    result["derived_daily_close"] = derived_daily["close"]
    result["derived_daily_volume_lots"] = derived_daily["volume_lots"]
    stage_price_status, stage_price_ratio = _status_for_prices(
        {
            "open": derived[0]["open"], "high": max(row["high"] for row in derived),
            "low": min(row["low"] for row in derived), "close": derived[-1]["close"],
        },
        purchased_5m_daily,
    )
    stage_volume_status, stage_volume_ratio = _status_for_volume(
        derived_daily["volume_lots"], purchased_5m_daily["volume_lots"]
    )
    result["stage_price_status"] = stage_price_status
    result["stage_price_relative_diff"] = stage_price_ratio
    result["stage_volume_status"] = stage_volume_status
    result["stage_volume_relative_diff"] = stage_volume_ratio

    daily = {
        "open": recon["daily_open"], "high": recon["daily_high"],
        "low": recon["daily_low"], "close": recon["daily_close"],
    } if recon else {}
    if daily and all(value is not None for value in daily.values()):
        daily_price_status, daily_price_ratio = _status_for_prices(derived_daily, daily)
        daily_volume_status, daily_volume_ratio = _status_for_volume(
            derived_daily["volume_lots"], recon["daily_volume_lots"]
        )
        result["daily_price_status"] = daily_price_status
        result["daily_price_relative_diff"] = daily_price_ratio
        result["daily_volume_status"] = daily_volume_status
        result["daily_volume_relative_diff"] = daily_volume_ratio

    if recon is None and stage_price_status == "pass" and stage_volume_status in {"pass", "missing"}:
        result["decision"] = "one_minute_rebuilds_purchased_5m_stage"
        result["reason"] = "1m-derived daily OHLC matches purchased 5m stage; daily project row is absent"
    elif stage_price_status == "pass" and stage_volume_status == "pass" and result["daily_price_status"] == "pass" and result["daily_volume_status"] == "pass":
        if result["amount_status_existing"] == "pass":
            result["decision"] = "one_minute_confirms_all_existing_fields"
            result["reason"] = "1m-derived 5m confirms stage OHLC/volume and daily OHLC/volume/amount gates"
        else:
            result["decision"] = "one_minute_confirms_price_volume_amount_unresolved"
            result["reason"] = "1m confirms OHLC/volume; 1m source has no amount field"
    elif stage_price_status == "pass" and stage_volume_status == "pass":
        result["decision"] = "one_minute_confirms_5m_daily_conflict"
        result["reason"] = "1m-derived daily values match purchased 5m OHLC/volume but the purchased daily snapshot still differs"
    elif result["daily_price_status"] == "pass" and result["daily_volume_status"] == "pass":
        result["decision"] = "one_minute_derived_candidate_stage_conflict"
        result["reason"] = "1m-derived daily values pass but purchased 5m stage differs; amount remains unavailable"
    else:
        result["decision"] = "one_minute_cross_source_conflict"
        result["reason"] = "1m-derived values do not independently pass both stage and daily checks"
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--year", action="append", type=int, dest="years")
    parser.add_argument("--limit", type=int, default=0, help="Limit code-days for a probe")
    parser.add_argument("--hs-root", type=Path, default=DEFAULT_HS_ROOT)
    parser.add_argument("--index-root", type=Path, default=DEFAULT_INDEX_ROOT)
    parser.add_argument("--bj-root", type=Path, default=DEFAULT_BJ_ROOT)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--security-type", choices=("stock", "index"), help="Restrict the audit to one source universe.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    years = set(args.years or range(2000, 2006))
    if any(year < 1990 or year > 2100 for year in years):
        raise SystemExit("invalid year")
    client = clickhouse_client()
    queue = _load_queue(client, args.run_id, years, args.limit, args.security_type)
    stock_keys = {key for key, value in queue.items() if value["security_type"] == "stock"}
    recon = _load_reconciliation(client, stock_keys, years)
    stage = _load_stage(client, queue)
    targets_by_source: dict[tuple[Path, int, str, str], set[date]] = defaultdict(set)
    for code, trade_date in queue:
        security_type = queue[(code, trade_date)]["security_type"]
        targets_by_source[
            (_source_root(code, security_type, args.hs_root, args.index_root, args.bj_root), trade_date.year, security_type, code)
        ].add(trade_date)

    results: list[dict[str, Any]] = []
    source_cache: dict[tuple[Path, int], ZipFile | None] = {}
    try:
        archive_names: dict[tuple[Path, int], set[str]] = {}
        for (root, year, security_type, code), target_dates in sorted(
            targets_by_source.items(), key=lambda item: (str(item[0][0]), item[0][1], item[0][2], item[0][3])
        ):
            archive_path = root / f"{year}_1min.zip"
            if not archive_path.exists():
                for trade_date in sorted(target_dates):
                    key = (code, trade_date)
                    results.append(_cross_result(queue[key], recon.get(key), stage.get(key), "", []))
                continue
            cache_key = (root, year)
            if cache_key not in source_cache:
                source_cache[cache_key] = ZipFile(archive_path)
            archive = source_cache[cache_key]
            if cache_key not in archive_names:
                archive_names[cache_key] = set(archive.namelist())
            names = archive_names[cache_key]
            entry_name = _source_name(code, year, security_type)
            if entry_name not in names:
                for trade_date in sorted(target_dates):
                    key = (code, trade_date)
                    results.append(_cross_result(queue[key], recon.get(key), stage.get(key), entry_name, []))
                continue
            by_day, _ = _read_source_days(archive, entry_name, target_dates)
            for trade_date in sorted(target_dates):
                key = (code, trade_date)
                results.append(_cross_result(queue[key], recon.get(key), stage.get(key), entry_name, by_day.get(trade_date, [])))
    finally:
        for archive in source_cache.values():
            if archive is not None:
                archive.close()

    counts = Counter(str(row["decision"]) for row in results)
    summary = {
        "run_id": args.run_id,
        "years": sorted(years),
        "queue_rows": len(queue),
        "stock_rows": len(stock_keys),
        "result_rows": len(results),
        "decision_counts": dict(counts),
        "price_relative_tolerance": PRICE_RELATIVE_TOLERANCE,
        "relative_tolerance": RELATIVE_TOLERANCE,
        "source_has_amount": False,
        "formal_table_mutation": False,
        "generated_at": datetime.now(BUSINESS_TZ).isoformat(),
    }
    print(json.dumps(summary, ensure_ascii=False))
    for decision, count in counts.most_common():
        print(f"decision={decision} count={count}")
    candidate_printed = 0
    for row in results:
        if row["decision"] in {"one_minute_derived_candidate_stage_conflict", "one_minute_confirms_price_volume_amount_unresolved"}:
            print(
                f"candidate={row['code']}@{row['trade_date']} decision={row['decision']} "
                f"stage_price={row.get('stage_price_relative_diff')} "
                f"stage_volume={row.get('stage_volume_relative_diff')} reason={row['reason']}"
            )
            candidate_printed += 1
            if candidate_printed >= 20:
                break

    if args.write_report:
        report_dir = warehouse_path("purchased_history", "one_minute_cross_validation", args.run_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        import pyarrow as pa
        import pyarrow.parquet as pq

        pq.write_table(pa.Table.from_pylist(results), report_dir / "reconciliation.parquet")
        complete_candidates = [row for row in results if int(row.get("derived_5m_rows") or 0) == 48]
        pq.write_table(pa.Table.from_pylist(complete_candidates), report_dir / "daily_candidates.parquet")
        index_candidates = [
            row for row in complete_candidates
            if row.get("decision") == "one_minute_rebuilds_purchased_5m_stage"
        ]
        pq.write_table(pa.Table.from_pylist(index_candidates), report_dir / "index_daily_rebuild_candidates.parquet")
        manifest_path = runtime_path("purchased_history", f"one_minute_cross_validation_{args.run_id}.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    **summary,
                    "report": str(report_dir / "reconciliation.parquet"),
                    "daily_candidates": str(report_dir / "daily_candidates.parquet"),
                    "index_daily_rebuild_candidates": str(report_dir / "index_daily_rebuild_candidates.parquet"),
                    "complete_candidate_rows": len(complete_candidates),
                    "index_daily_rebuild_candidate_rows": len(index_candidates),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"report={report_dir / 'reconciliation.parquet'}")
        print(f"manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
