from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df, clickhouse_scalar  # noqa: E402


COLUMNS = [
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "change_pct",
    "change_amount",
    "turnover_rate",
    "created_at",
    "id",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair kline_daily change metrics from previous trading close.")
    parser.add_argument("--start-date", required=True, help="Inclusive YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="Inclusive YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50000)
    return parser.parse_args()


def _quote(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _previous_trade_date(start_date: str) -> str | None:
    value = clickhouse_scalar(
        """
        SELECT max(trade_date)
        FROM trade_calendar
        WHERE market = 'SH'
          AND is_trading = 1
          AND trade_date < ?
        """,
        [date.fromisoformat(start_date)],
    )
    if value is None:
        return None
    return pd.to_datetime(value).date().isoformat()


def _to_insert_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def repair_metrics(
    start_date: str,
    end_date: str,
    dry_run: bool = False,
    batch_size: int = 50000,
) -> dict[str, Any]:
    load_start = _previous_trade_date(start_date) or start_date
    df = clickhouse_query_df(
        f"""
        SELECT {", ".join(COLUMNS)}
        FROM kline_daily FINAL
        WHERE trade_date >= toDate({_quote(load_start)})
          AND trade_date <= toDate({_quote(end_date)})
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return {"ok": False, "reason": "no_rows", "start_date": start_date, "end_date": end_date}

    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    for col in ("open", "high", "low", "close", "volume", "amount", "turnover_rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["_old_change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0.0)
    df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
    prev_close = df.groupby("code")["close"].shift(1)
    recalculated_change_pct = (((df["close"] - prev_close) / prev_close) * 100).where(prev_close > 0, 0.0).fillna(0.0)
    mismatch_mask = (
        (df["trade_date"] >= date.fromisoformat(start_date))
        & (df["trade_date"] <= date.fromisoformat(end_date))
        & ((df["_old_change_pct"] - recalculated_change_pct).abs() > 0.2)
    )
    before_bad = int(mismatch_mask.sum())
    df["change_amount"] = (df["close"] - prev_close).where(prev_close > 0, 0.0)
    df["change_pct"] = recalculated_change_pct
    df["amplitude"] = (((df["high"] - df["low"]) / prev_close) * 100).where(prev_close > 0, 0.0)
    for col in ("change_amount", "change_pct", "amplitude"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    target = df[(df["trade_date"] >= date.fromisoformat(start_date)) & (df["trade_date"] <= date.fromisoformat(end_date))].copy()
    if target.empty:
        return {"ok": False, "reason": "no_target_rows", "start_date": start_date, "end_date": end_date}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "start_date": start_date,
            "end_date": end_date,
            "rows_to_rewrite": int(len(target)),
            "mismatched_before": before_bad,
            "qmt_amount_unit_rows": 0,
        }

    client = clickhouse_client()
    client.command(
        f"""
        ALTER TABLE kline_daily
        DELETE WHERE trade_date >= toDate({_quote(start_date)})
          AND trade_date <= toDate({_quote(end_date)})
        SETTINGS mutations_sync = 1
        """
    )
    rows = [
        tuple(_to_insert_value(value) for value in row)
        for row in target[COLUMNS].itertuples(index=False, name=None)
    ]
    for start in range(0, len(rows), batch_size):
        client.insert("kline_daily", rows[start:start + batch_size], column_names=COLUMNS)

    return {
        "ok": True,
        "dry_run": False,
        "start_date": start_date,
        "end_date": end_date,
        "rows_rewritten": int(len(rows)),
        "mismatched_before": before_bad,
        "qmt_amount_unit_rows": 0,
    }


def main() -> int:
    args = parse_args()
    result = repair_metrics(
        args.start_date,
        args.end_date,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
    )
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
