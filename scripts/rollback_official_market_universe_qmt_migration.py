from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clickhouse_connect import get_client

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.qmtmini_daily_backfill_validate import quote_sql
from utils.paths import report_path, runtime_path


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
BACKUP_MARKER = "_backup_qmt_migration_"
OFFICIAL_BACKUP_TABLES = [
    "stocks",
    "sectors",
    "sector_stocks",
    "sector_kline_daily",
    "kline_daily",
    "kline_minute_5",
    "kline_minute_15",
    "kline_minute_30",
    "kline_minute_60",
]


def log(message: str) -> None:
    print(f"[{datetime.now(BUSINESS_TZ):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def safe_table_suffix() -> str:
    return f"{datetime.now(BUSINESS_TZ):%Y%m%d_%H%M%S_%f}_{os.getpid()}"


def table_exists(client: Any, table: str) -> bool:
    return bool(
        client.query(
            """
            SELECT count()
            FROM system.tables
            WHERE database = currentDatabase()
              AND name = {table:String}
            """,
            parameters={"table": table},
        ).first_row[0]
    )


def ch_client(timeout_sec: int = 3600):
    return get_client(
        host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
        password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
        send_receive_timeout=timeout_sec,
    )


def table_count(client: Any, table: str) -> int | None:
    if not table_exists(client, table):
        return None
    return int(client.query(f"SELECT count() FROM {table}").first_row[0] or 0)


def parse_tables(value: str) -> list[str]:
    if not value.strip():
        return list(OFFICIAL_BACKUP_TABLES)
    requested = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(requested) - set(OFFICIAL_BACKUP_TABLES))
    if unknown:
        raise SystemExit(f"Unknown table(s): {', '.join(unknown)}")
    return requested


def price_date_column(table: str) -> str:
    if table == "kline_daily" or table == "sector_kline_daily":
        return "trade_date"
    if table in {"kline_minute_5", "kline_minute_15", "kline_minute_30", "kline_minute_60"}:
        return "datetime"
    return ""


def restore_price_table_range(client: Any, table: str, backup: str, *, execute: bool) -> dict[str, Any]:
    date_column = price_date_column(table)
    backup_rows = table_count(client, backup)
    row = client.query(
        f"""
        SELECT min(toDate({date_column})), max(toDate({date_column}))
        FROM {backup}
        """
    ).first_row
    start_date = str(row[0]) if row[0] else None
    end_date = str(row[1]) if row[1] else None
    result: dict[str, Any] = {
        "ok": backup_rows is not None and start_date is not None and end_date is not None,
        "table": table,
        "backup_table": backup,
        "executed": execute,
        "backup_rows": backup_rows,
        "restore_start_date": start_date,
        "restore_end_date": end_date,
        "scope": "target_range",
    }
    if not result["ok"]:
        result["reason"] = "backup_empty_or_missing_date_range"
        return result
    if date_column == "trade_date":
        filter_sql = (
            f"trade_date >= toDate({quote_sql(start_date)}) "
            f"AND trade_date <= toDate({quote_sql(end_date)})"
        )
    else:
        filter_sql = (
            f"toDate(datetime) >= toDate({quote_sql(start_date)}) "
            f"AND toDate(datetime) <= toDate({quote_sql(end_date)})"
        )
    before_rows = int(client.query(f"SELECT count() FROM {table} WHERE {filter_sql}").first_row[0] or 0)
    result["before_rows_in_range"] = before_rows
    if not execute:
        result["planned"] = "snapshot current target range, delete target range, insert backup rows"
        return result

    suffix = safe_table_suffix()
    current_snapshot = f"{table}_before_qmt_rollback_{suffix}"
    client.command(f"DROP TABLE IF EXISTS {current_snapshot}")
    client.command(f"CREATE TABLE {current_snapshot} AS {table}")
    client.command(f"INSERT INTO {current_snapshot} SELECT * FROM {table} WHERE {filter_sql}")
    client.command(f"ALTER TABLE {table} DELETE WHERE {filter_sql} SETTINGS mutations_sync = 1")
    client.command(f"INSERT INTO {table} SELECT * FROM {backup}")
    after_rows = int(client.query(f"SELECT count() FROM {table} WHERE {filter_sql}").first_row[0] or 0)
    result["current_snapshot_table"] = current_snapshot
    result["after_rows_in_range"] = after_rows
    result["ok"] = after_rows == backup_rows
    return result


def list_backup_stamps(client: Any) -> dict[str, Any]:
    names = [
        str(row[0])
        for row in client.query(
            """
            SELECT name
            FROM system.tables
            WHERE database = currentDatabase()
              AND position(name, {marker:String}) > 0
            ORDER BY name
            """,
            parameters={"marker": BACKUP_MARKER},
        ).result_rows
    ]
    stamps: dict[str, dict[str, Any]] = {}
    for table in OFFICIAL_BACKUP_TABLES:
        prefix = f"{table}{BACKUP_MARKER}"
        for name in names:
            if not name.startswith(prefix):
                continue
            stamp = name[len(prefix) :]
            item = stamps.setdefault(stamp, {"tables": [], "row_counts": {}})
            item["tables"].append(table)
            item["row_counts"][table] = table_count(client, name)
    return {"backup_stamps": stamps}


def restore_table(client: Any, table: str, stamp: str, *, execute: bool) -> dict[str, Any]:
    backup = f"{table}{BACKUP_MARKER}{stamp}"
    before_rows = table_count(client, table)
    backup_rows = table_count(client, backup)
    if backup_rows is None:
        return {
            "ok": False,
            "table": table,
            "backup_table": backup,
            "reason": "backup_missing",
            "executed": execute,
            "before_rows": before_rows,
        }
    if price_date_column(table):
        result = restore_price_table_range(client, table, backup, execute=execute)
        result["before_rows"] = before_rows
        return result

    result: dict[str, Any] = {
        "ok": True,
        "table": table,
        "backup_table": backup,
        "executed": execute,
        "before_rows": before_rows,
        "backup_rows": backup_rows,
        "scope": "full_table",
    }
    if not execute:
        result["planned"] = "copy backup to temp table, snapshot current table, swap temp into official table"
        return result

    suffix = safe_table_suffix()
    tmp = f"{table}_rollback_tmp_{suffix}"
    current_snapshot = f"{table}_before_qmt_rollback_{suffix}"
    client.command(f"DROP TABLE IF EXISTS {tmp}")
    if table_exists(client, table):
        client.command(f"CREATE TABLE {tmp} AS {table}")
    else:
        client.command(f"CREATE TABLE {tmp} AS {backup}")
    client.command(f"INSERT INTO {tmp} SELECT * FROM {backup}")
    if table_exists(client, table):
        client.command(f"RENAME TABLE {table} TO {current_snapshot}, {tmp} TO {table}")
        result["current_snapshot_table"] = current_snapshot
    else:
        client.command(f"RENAME TABLE {tmp} TO {table}")
        result["current_snapshot_table"] = None
    result["after_rows"] = table_count(client, table)
    result["ok"] = result["after_rows"] == backup_rows
    return result


def maybe_remove_source_marker(stamp: str, *, execute: bool) -> dict[str, Any]:
    marker = runtime_path("official_market_universe_source.json")
    result: dict[str, Any] = {"path": str(marker), "exists": marker.exists(), "executed": execute, "removed": False}
    if not marker.exists():
        return result
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:
        result["ok"] = False
        result["reason"] = f"marker_parse_failed: {type(exc).__name__}: {exc}"
        return result
    result["migration_stamp"] = payload.get("migration_stamp")
    if payload.get("migration_stamp") != stamp:
        result["ok"] = True
        result["reason"] = "marker_stamp_does_not_match"
        return result
    if execute:
        marker.unlink()
        result["removed"] = True
    result["ok"] = True
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rollback AiStock official market universe tables from QMT migration backups.")
    parser.add_argument("--stamp", default="", help="Backup stamp suffix from migrate_official_market_universe_to_qmt.py.")
    parser.add_argument("--tables", default="", help="Comma-separated table list. Default restores all official migration tables.")
    parser.add_argument("--list-stamps", action="store_true", help="List available QMT migration backup stamps and exit.")
    parser.add_argument("--execute", action="store_true", help="Actually restore official tables. Default is dry-run.")
    parser.add_argument(
        "--confirm-rollback-to-backup",
        action="store_true",
        help="Required with --execute. Confirms official tables may be restored from backup tables.",
    )
    parser.add_argument(
        "--remove-source-marker",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove official_market_universe_source.json only when its migration_stamp matches --stamp.",
    )
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_stamp = safe_table_suffix()
    run_dir = report_path("qmt_official_market_universe_rollback", f"run_{run_stamp}")
    run_dir.mkdir(parents=True, exist_ok=True)
    report = Path(args.report) if args.report else run_dir / "summary.json"
    execute = bool(args.execute)
    result: dict[str, Any] = {
        "ok": False,
        "executed": execute,
        "run_stamp": run_stamp,
        "run_dir": str(run_dir),
        "args": vars(args),
        "steps": {},
    }
    log(f"QMT official universe rollback start execute={execute} run_dir={run_dir}")

    if execute and not args.confirm_rollback_to_backup:
        result["error"] = "Missing --confirm-rollback-to-backup for --execute"
        result["report_path"] = str(report)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
        log(f"rollback refused: {result['error']} report={report}")
        return 2

    client = ch_client()
    try:
        if args.list_stamps:
            result["steps"]["backup_stamps"] = list_backup_stamps(client)
            result["ok"] = True
        else:
            if not args.stamp:
                raise ValueError("--stamp is required unless --list-stamps is used")
            tables = parse_tables(args.tables)
            restored: dict[str, Any] = {}
            for table in tables:
                log(f"restore plan table={table} stamp={args.stamp}")
                restored[table] = restore_table(client, table, args.stamp, execute=execute)
            result["steps"]["restore"] = restored
            if args.remove_source_marker:
                result["steps"]["source_marker"] = maybe_remove_source_marker(args.stamp, execute=execute)
            result["ok"] = all(bool(item.get("ok")) for item in restored.values())
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(f"rollback failed: {result['error']}")

    result["report_path"] = str(report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    log(f"rollback summary ok={result['ok']} report={report}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
