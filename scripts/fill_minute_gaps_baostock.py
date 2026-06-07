"""Slow gap filler for minute bars using BaoStock.

This is intentionally conservative: it reads an audit detail CSV, finds missing
trade dates per code, and requests BaoStock one code/day at a time.  This avoids
the long-range timeout pattern observed with BaoStock minute APIs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.fetch_baostock_minute_to_clickhouse import (  # noqa: E402
    FREQ_TABLE_MAP,
    fetch_one_timeout,
    upsert_to_clickhouse,
    validate_frame,
)
from utils.market_warehouse import clickhouse_client  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill minute gaps from BaoStock")
    parser.add_argument("--audit-detail", required=True)
    parser.add_argument("--period", default="5", choices=["5", "15", "30", "60"])
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--max-dates-per-code", type=int, default=5)
    parser.add_argument("--adjustflag", default="2", choices=["1", "2", "3"])
    parser.add_argument("--timeout-sec", type=int, default=90)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", default="")
    return parser.parse_args()


def _trade_dates(start: str, end: str) -> list[str]:
    ch = clickhouse_client()
    df = ch.query_df(
        """
        SELECT DISTINCT toString(trade_date) AS d
        FROM trade_calendar
        WHERE trade_date >= %(start)s AND trade_date <= %(end)s AND is_trading = 1
        ORDER BY trade_date
        """,
        parameters={"start": start, "end": end},
    )
    return [str(v)[:10] for v in df["d"].tolist()]


def _covered_dates(table: str, code: str, start: str, end: str) -> set[str]:
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT toString(toDate(datetime)) AS d
        FROM {table}
        WHERE code = %(code)s
          AND datetime >= toDateTime(%(start)s)
          AND datetime < toDateTime(%(end)s) + INTERVAL 1 DAY
        GROUP BY d
        """,
        parameters={"code": code, "start": start, "end": end},
    )
    return {str(v)[:10] for v in df["d"].tolist()}


def main() -> int:
    args = parse_args()
    table = FREQ_TABLE_MAP[args.period]
    audit_path = Path(args.audit_detail)
    if not audit_path.exists():
        raise SystemExit(f"audit detail not found: {audit_path}")

    detail = pd.read_csv(audit_path)
    detail = detail[detail["is_complete"].astype(str).str.lower().isin(["false", "0"])]
    detail = detail.sort_values(["missing_rows", "coverage_rows_pct"], ascending=[False, True]).head(args.top_n)
    codes = [
        str(code).strip().upper()
        for code in detail["code"].tolist()
        if not str(code).strip().upper().endswith(".BJ")
    ]
    trade_dates = _trade_dates(args.start_date, args.end_date)

    report = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "provider": "baostock",
        "table": table,
        "period": f"{args.period}m",
        "dry_run": args.dry_run,
        "codes": codes,
        "results": [],
    }

    total_written = 0
    for code in codes:
        covered = _covered_dates(table, code, args.start_date, args.end_date)
        missing = [d for d in trade_dates if d not in covered][: args.max_dates_per_code]
        code_result = {"code": code, "missing_dates_selected": missing, "dates": []}
        for d in missing:
            item = {"date": d, "status": "pending", "rows": 0, "written": 0}
            try:
                df = fetch_one_timeout(
                    code=code,
                    start_date=d,
                    end_date=d,
                    frequency=args.period,
                    adjustflag=args.adjustflag,
                    source_tag="baostock.gap_fill",
                    timeout_sec=args.timeout_sec,
                )
                item.update({"status": "ok", **validate_frame(df)})
                if not args.dry_run and not df.empty:
                    written = upsert_to_clickhouse(table, df, replace=True)
                    item["written"] = written
                    total_written += written
            except Exception as exc:
                item.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            code_result["dates"].append(item)
            print(json.dumps({"code": code, **item}, ensure_ascii=False), flush=True)
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
        report["results"].append(code_result)

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["total_written"] = total_written
    out = Path(args.report) if args.report else PROJECT_ROOT / "data" / "warehouse" / "exports" / f"baostock_gap_fill_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
