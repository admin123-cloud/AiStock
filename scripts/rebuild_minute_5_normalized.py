"""Build a partitioned 5m candidate from the current table without mutations.

Legacy local TDX rows were written as UTC-naive timestamps and raw shares/
amount units.  The candidate retains normal QMT rows and converts only the
known shifted TDX shape (17:xx--23:00) back to China market time.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import report_path


SOURCE = "kline_minute_5"
TARGET = "kline_minute_5_rebuild_20260901"
VALID_TIMES = "(" + ",".join(
    f"'{hour:02d}:{minute:02d}'"
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
    for hour, minute in (divmod(total, 60) for total in range(start + 5, end + 1, 5))
) + ")"


def ident(value: str) -> str:
    if not value or value[0].isdigit() or not value.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier: {value}")
    return value


def create(client, target: str) -> None:
    target = ident(target)
    client.command(f"DROP TABLE IF EXISTS {target}")
    client.command(
        f"""
        CREATE TABLE {target}
        (
          code Nullable(String), datetime Nullable(DateTime),
          open Nullable(Float64), high Nullable(Float64), low Nullable(Float64), close Nullable(Float64),
          volume Nullable(Float64), amount Nullable(Float64), created_at DateTime, id UInt64
        ) ENGINE = ReplacingMergeTree(created_at)
        PARTITION BY toYYYYMM(datetime)
        ORDER BY (code, datetime)
        SETTINGS allow_nullable_key = 1
        """
    )


def build(client, target: str) -> None:
    target = ident(target)
    client.command(
        f"""
        INSERT INTO {target} (code, datetime, open, high, low, close, volume, amount, created_at, id)
        SELECT code, datetime, open, high, low, close,
               CAST(volume, 'Nullable(Float64)'), CAST(amount, 'Nullable(Float64)'),
               coalesce(created_at, toDateTime(0)), id
        FROM {SOURCE} FINAL
        WHERE formatDateTime(datetime, '%H:%i') IN {VALID_TIMES}
        UNION ALL
        SELECT
          code, datetime - INTERVAL 8 HOUR, open, high, low, close,
          CAST(if((endsWith(code, '.SH') AND startsWith(code, '000')) OR
             (endsWith(code, '.SZ') AND startsWith(code, '399')), toFloat64(volume), toFloat64(volume) / 100.0), 'Nullable(Float64)'),
          CAST(amount * 10000.0, 'Nullable(Float64)'), coalesce(created_at, toDateTime(0)), id
        FROM {SOURCE} FINAL
        WHERE toHour(datetime) BETWEEN 17 AND 23
          AND formatDateTime(datetime - INTERVAL 8 HOUR, '%H:%i') IN {VALID_TIMES}
        SETTINGS max_threads = 2, max_memory_usage = 8000000000
        """
    )


def validate(client, target: str) -> dict[str, object]:
    target = ident(target)
    row = client.query(
        f"""
        SELECT count(), uniqExact(code), min(datetime), max(datetime),
               countIf(formatDateTime(datetime, '%H:%i') NOT IN {VALID_TIMES}),
               count() - uniqExact(code, datetime)
        FROM {target} FINAL
        """
    ).result_rows[0]
    return dict(zip(("rows", "codes", "min_datetime", "max_datetime", "invalid_session_rows", "duplicate_keys"), row))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=TARGET)
    parser.add_argument("--phase", choices=("create", "build", "validate", "all"), default="all")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    client = clickhouse_client()
    if args.phase in {"create", "all"}:
        create(client, args.target)
    if args.phase in {"build", "all"}:
        build(client, args.target)
    summary = {"target": args.target, "checked_at": datetime.now().isoformat()}
    if args.phase in {"validate", "all"}:
        summary.update(validate(client, args.target))
    path = Path(args.report) if args.report else report_path("minute_rebuild", f"{args.target}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
