"""Read-only triage of purchased-history 5m review-queue gaps (pre-2010).

Purpose
-------
The 数据审核 queue reports missing 5m data points.  Before looking for external
data sources, this script classifies every queue item by *root cause*, because
the three causes need completely different treatment:

``A_parse_definition``
    The source file has all 48 bars, but the importer rejected rows for a
    structural reason (e.g. index files carry 成交额 but no 成交量 column, or
    a differently ordered replay series).  Data is present -> fixable without
    any new data source.

``B_source_corrupt``
    The source file has 48 bars but the OHLC values themselves are corrupt
    (non-positive close / frozen open carry-forward).  No local copy can repair
    this: the same defect is present in every purchased copy (5m, 1m, and the
    independent 复盘大师 1m mirror) because they share one upstream vendor feed.

``C_genuinely_short``
    The source file has fewer than 48 rows.  Real absence at the vendor.

``D_project_daily``
    Queue item is about the project daily table, not the 5m source.

This script NEVER writes to any market table, stage table or review ledger.
It only SELECTs and writes a JSON report under the research archive.

Usage
-----
    python scripts/audit_purchased_5m_gap_triage.py
    python scripts/audit_purchased_5m_gap_triage.py --run-id <run_id>
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import report_path

SOURCE_AUDIT = "stock.purchased_history_minute_5_source_audit_v2"
QUEUE = "stock.purchased_history_data_review_items"
STAGE = "stock.purchased_history_minute_5_stage"


def _classify_queue_item(issue_type: str, actual_count: int, invalid_rows: int, expected: int, probe: str) -> str:
    """Classify one queue item.

    ``probe`` is the result of reading the raw source file for this code-day
    (see ``_probe_source``); it is the authoritative signal because the audit
    table's ``invalid_row_count`` is 0 both for "all rows structurally
    rejected" and for "no parseable row at all".
    """
    if issue_type == "project_daily_missing_for_5m":
        return "D_project_daily"
    if probe == "negative_or_zero_close":
        return "B_source_corrupt"
    if probe == "complete_48_positive":
        # 48 bars present and every OHLC positive: the importer rejected rows
        # for a structural reason (column layout / code form), not bad values.
        return "A_parse_definition"
    if probe == "short_file":
        return "C_genuinely_short"
    if actual_count >= expected:
        return "A_parse_definition"
    if invalid_rows > 0:
        return "B_source_corrupt"
    return "C_genuinely_short"


def _probe_source(archive_path: Path, entry: str, trade_date: str, is_index: bool) -> tuple[str, dict[str, Any]]:
    """Read the raw purchased 5m entry for one code-day and report its true state.

    Returns a short verdict plus supporting detail.  This is the ground truth
    the importer's audit row cannot express, because the importer discards a
    row as soon as any OHLC field is non-positive and then only counts it.
    """
    detail: dict[str, Any] = {}
    if not archive_path.exists():
        return "archive_missing", detail
    try:
        with ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            if entry not in names:
                return "entry_missing", detail
            raw = archive.read(entry)
    except Exception as exc:  # pragma: no cover - defensive
        detail["error"] = str(exc)
        return "unreadable", detail

    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    detail["header"] = header
    target = trade_date.replace("-", "/")
    rows = 0
    non_positive = 0
    frozen_open_runs = 0
    prev_open: float | None = None
    run = 0
    for cells in reader:
        if len(cells) < 7:
            continue
        stamp = cells[0].strip().replace("-", "/")
        if not stamp.startswith(target):
            continue
        rows += 1
        try:
            o, c = float(cells[3]), float(cells[4])
            h, low = float(cells[5]), float(cells[6])
        except ValueError:
            non_positive += 1
            continue
        if min(o, c, h, low) <= 0:
            non_positive += 1
        if prev_open is not None and o == prev_open:
            run += 1
        else:
            if run >= 3:
                frozen_open_runs += 1
            run = 0
        prev_open = o
    if run >= 3:
        frozen_open_runs += 1
    detail["rows"] = rows
    detail["non_positive_ohlc_rows"] = non_positive
    detail["frozen_open_runs"] = frozen_open_runs

    if rows == 0:
        return "no_rows_for_date", detail
    if rows < 48:
        return "short_file", detail
    if non_positive > 0:
        return "negative_or_zero_close", detail
    return "complete_48_positive", detail


HS_MONTHLY = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按月归档")
INDEX_MONTHLY = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_指数\5分钟_按月归档")


def _locate_source(code: str, trade_date: str, is_index: bool) -> tuple[Path, str]:
    """Resolve a code-day to its purchased 5m daily ZIP and in-archive entry."""
    day = trade_date.replace("-", "")
    month = f"{day[:4]}-{day[4:6]}"
    root = INDEX_MONTHLY if is_index else HS_MONTHLY
    archive_path = root / month / f"{day}_5min.zip"
    if is_index:
        # Index entries are the bare symbol, zero padded to six digits.
        entry = f"{code.split('.')[0]}.csv"
    else:
        symbol, market = code.split(".", 1)
        entry = f"{market.lower()}{symbol}.csv"
    return archive_path, entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", default="", help="Queue run_id; default = latest by observed_at")
    parser.add_argument("--limit", type=int, default=0, help="Optional cap on queue rows examined")
    args = parser.parse_args()

    client = clickhouse_client()

    if args.run_id:
        run_id = args.run_id
    else:
        rows = client.query(f"SELECT run_id FROM {QUEUE} GROUP BY run_id ORDER BY max(observed_at) DESC LIMIT 1").result_rows
        if not rows:
            print("no review queue rows found", file=sys.stderr)
            return 2
        run_id = str(rows[0][0])

    limit_sql = f" LIMIT {int(args.limit)}" if args.limit and args.limit > 0 else ""
    queue_rows = client.query(
        f"""
        SELECT code, trade_date, security_type, issue_type,
               any(expected_count), any(actual_count)
        FROM {QUEUE}
        WHERE run_id = %(run_id)s
        GROUP BY code, trade_date, security_type, issue_type
        ORDER BY trade_date, code
        {limit_sql}
        """,
        parameters={"run_id": run_id},
    ).result_rows

    # Row-level truth per code-day from the immutable source audit.
    audit: dict[tuple[str, str], tuple[int, int, int, int]] = {}
    for code, day, src_rows, valid, invalid, exp in client.query(
        f"""
        SELECT code, toString(toDate(min_datetime)) AS d,
               max(source_row_count), max(valid_ohlc_row_count),
               max(invalid_row_count), max(expected_bar_count)
        FROM {SOURCE_AUDIT}
        WHERE min_datetime IS NOT NULL
        GROUP BY code, d
        """
    ).result_rows:
        audit[(str(code), str(day))] = (int(src_rows), int(valid), int(invalid), int(exp))

    stage_ok: dict[tuple[str, str], int] = {}
    for code, day, n in client.query(
        f"""
        SELECT code, toString(toDate(datetime)) AS d, count() AS n
        FROM {STAGE}
        GROUP BY code, d
        """
    ).result_rows:
        stage_ok[(str(code), str(day))] = int(n)

    results: list[dict[str, Any]] = []
    probe_cache: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for code, day, security_type, issue_type, expected, actual in queue_rows:
        key = (str(code), str(day))
        src_rows, valid, invalid, exp = audit.get(key, (0, 0, 0, 0))
        is_index = str(security_type) == "index"
        cache_key = (str(code), str(day))
        if cache_key not in probe_cache:
            archive_path, entry = _locate_source(str(code), str(day), is_index)
            probe_cache[cache_key] = _probe_source(archive_path, entry, str(day), is_index)
        probe, detail = probe_cache[cache_key]
        root = _classify_queue_item(str(issue_type), int(actual), invalid, int(expected), probe)
        results.append(
            {
                "code": str(code),
                "trade_date": str(day),
                "security_type": str(security_type),
                "issue_type": str(issue_type),
                "expected_count": int(expected),
                "queue_actual_count": int(actual),
                "audit_source_rows": src_rows,
                "audit_valid_ohlc_rows": valid,
                "audit_invalid_rows": invalid,
                "rows_present_but_rejected": bool(valid + invalid == src_rows and src_rows == int(expected)),
                "stage_5m_rows": stage_ok.get(key, 0),
                "probe": probe,
                "probe_detail": detail,
                "root_cause": root,
            }
        )

    by_cause = Counter(item["root_cause"] for item in results)
    by_date: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        by_date[item["trade_date"]][item["root_cause"]] += 1
    by_year = Counter(item["trade_date"][:4] for item in results)
    by_exchange = Counter(item["code"].split(".")[-1] for item in results)
    by_type = Counter(item["security_type"] for item in results)
    all_rows_present = sum(1 for item in results if item["rows_present_but_rejected"])

    payload = {
        "run_id": run_id,
        "queue_rows": len(results),
        "root_cause_counts": dict(by_cause),
        "rows_present_but_rejected_count": all_rows_present,
        "by_year": dict(sorted(by_year.items())),
        "by_exchange": dict(by_exchange),
        "by_security_type": dict(by_type),
        "top_dates": [
            {"trade_date": day, "total": sum(counter.values()), **dict(counter)}
            for day, counter in sorted(by_date.items(), key=lambda kv: -sum(kv[1].values()))[:15]
        ],
        "results": results,
    }

    out = report_path("purchased_history", "gap_triage", f"{run_id}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {k: payload[k] for k in ("run_id", "queue_rows", "root_cause_counts", "rows_present_but_rejected_count", "by_year", "by_exchange")}
    summary["report"] = str(out)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
