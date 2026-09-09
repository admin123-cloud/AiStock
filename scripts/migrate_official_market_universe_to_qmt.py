from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
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

from data_fetcher.sources.qmtmini import QmtMiniDataSource
from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
from scripts.qmtmini_daily_backfill_validate import quote_sql
from scripts.sync_sectors_and_mapping import _classify_qmt_sector, _is_qmt_universe_sector
from utils.paths import report_path, runtime_path


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
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
MINUTE_TABLES = {
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
    "60m": "kline_minute_60",
}


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


def table_count(client: Any, table: str) -> int | None:
    if not table_exists(client, table):
        return None
    return int(client.query(f"SELECT count() FROM {table}").first_row[0] or 0)


def table_columns(client: Any, table: str) -> list[str]:
    return [str(row[0]) for row in client.query(f"DESCRIBE TABLE {table}").result_rows]


def create_table_like(client: Any, target: str, source: str, *, replace: bool = True) -> None:
    if replace:
        client.command(f"DROP TABLE IF EXISTS {target}")
    client.command(f"CREATE TABLE {target} AS {source}")


def ch_client(timeout_sec: int = 3600):
    return get_client(
        host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
        password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
        send_receive_timeout=timeout_sec,
    )


def backup_filter_sql(table: str, args: argparse.Namespace) -> str:
    if table == "kline_daily" or table == "sector_kline_daily":
        return (
            f"WHERE trade_date >= toDate({quote_sql(args.start_date)}) "
            f"AND trade_date <= toDate({quote_sql(args.end_date)})"
        )
    if table in MINUTE_TABLES.values():
        minute_start = args.minute_start_date or args.start_date
        minute_end = args.minute_end_date or args.end_date
        return (
            f"WHERE toDate(datetime) >= toDate({quote_sql(minute_start)}) "
            f"AND toDate(datetime) <= toDate({quote_sql(minute_end)})"
        )
    return ""


def backup_date_bounds(table: str, args: argparse.Namespace) -> tuple[date, date, str] | None:
    if table == "kline_daily" or table == "sector_kline_daily":
        return date.fromisoformat(args.start_date), date.fromisoformat(args.end_date), "trade_date"
    if table in MINUTE_TABLES.values():
        minute_start = args.minute_start_date or args.start_date
        minute_end = args.minute_end_date or args.end_date
        return date.fromisoformat(minute_start), date.fromisoformat(minute_end), "datetime"
    return None


def iter_date_chunks(start: date, end: date, days: int):
    current = start
    while current <= end:
        chunk_end = min(end, current + timedelta(days=max(days, 1) - 1))
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def backup_chunk_filter(date_column: str, start: date, end: date) -> str:
    if date_column == "trade_date":
        return (
            f"trade_date >= toDate({quote_sql(start.isoformat())}) "
            f"AND trade_date <= toDate({quote_sql(end.isoformat())})"
        )
    return (
        f"toDate(datetime) >= toDate({quote_sql(start.isoformat())}) "
        f"AND toDate(datetime) <= toDate({quote_sql(end.isoformat())})"
    )


def table_count_where(client: Any, table: str, filter_sql: str) -> int | None:
    if not table_exists(client, table):
        return None
    return int(client.query(f"SELECT count() FROM {table} {filter_sql}").first_row[0] or 0)


def backup_tables(client: Any, tables: list[str], stamp: str, args: argparse.Namespace, *, execute: bool) -> dict[str, Any]:
    backups: dict[str, Any] = {}
    for table in tables:
        if not table_exists(client, table):
            backups[table] = {"ok": False, "reason": "missing"}
            continue
        backup = f"{table}_backup_qmt_migration_{stamp}"
        filter_sql = backup_filter_sql(table, args)
        row_count = table_count_where(client, table, filter_sql)
        chunks: list[dict[str, Any]] = []
        if execute:
            client.command(f"DROP TABLE IF EXISTS {backup}")
            client.command(f"CREATE TABLE {backup} AS {table} ENGINE = MergeTree ORDER BY tuple()")
            bounds = backup_date_bounds(table, args)
            if bounds:
                start_date, end_date, date_column = bounds
                for chunk_start, chunk_end in iter_date_chunks(start_date, end_date, args.backup_date_chunk_days):
                    chunk_filter = backup_chunk_filter(date_column, chunk_start, chunk_end)
                    before_chunk_rows = table_count(client, backup) or 0
                    client.command(f"INSERT INTO {backup} SELECT * FROM {table} WHERE {chunk_filter}")
                    after_chunk_rows = table_count(client, backup) or 0
                    chunks.append(
                        {
                            "start_date": chunk_start.isoformat(),
                            "end_date": chunk_end.isoformat(),
                            "inserted_rows": after_chunk_rows - before_chunk_rows,
                        }
                    )
            else:
                client.command(f"INSERT INTO {backup} SELECT * FROM {table}")
        backup_count = table_count(client, backup) if execute else None
        backups[table] = {
            "ok": (backup_count == row_count) if execute else True,
            "backup_table": backup,
            "rows": row_count,
            "backup_rows": backup_count,
            "filter": filter_sql,
            "scope": "target_range" if filter_sql else "full_table",
            "chunks": chunks,
            "executed": execute,
        }
    return backups


def _normalize_qmt_row(item: dict[str, Any], row_type: str) -> list[Any] | None:
    code = str(item.get("code") or item.get("Code") or "").strip().upper()
    name = str(item.get("name") or item.get("Name") or "").strip()
    if not code or not name:
        return None
    market = str(item.get("market") or (code.split(".")[-1] if "." in code else "") or "").strip().upper()
    list_date = item.get("list_date") or datetime.now(BUSINESS_TZ).date()
    if isinstance(list_date, str):
        parsed = None
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(list_date[:10], fmt).date()
                break
            except Exception:
                pass
        list_date = parsed or datetime.now(BUSINESS_TZ).date()
    return [
        code,
        name,
        market,
        row_type,
        str(item.get("industry") or ""),
        str(item.get("region") or ""),
        list_date,
        int(item.get("quit") or 0),
        int(item.get("st") or 0),
    ]


def fetch_qmt_security_rows() -> dict[str, Any]:
    source = QmtMiniDataSource()
    stocks = source.get_stock_list(market="ALL", stock_type="stock") or []
    indices = source.get_stock_list(market="9", stock_type="index", list_type=1) or []
    rows: list[list[Any]] = []
    seen: set[str] = set()
    skipped = 0
    for item in stocks:
        row = _normalize_qmt_row(item, "stock")
        if not row:
            skipped += 1
            continue
        seen.add(str(row[0]))
        rows.append(row)
    for item in indices:
        row = _normalize_qmt_row(item, "index")
        if not row:
            skipped += 1
            continue
        if str(row[0]) in seen:
            continue
        rows.append(row)
        seen.add(str(row[0]))
    return {"rows": rows, "stocks": len(stocks), "indices": len(indices), "skipped": skipped}


def rebuild_stocks(client: Any, *, execute: bool, probe_qmt: bool) -> dict[str, Any]:
    if not execute and not probe_qmt:
        return {"ok": True, "executed": False, "probe_qmt": False, "planned": "rebuild stocks and indices from QMT"}
    fetched = fetch_qmt_security_rows()
    rows = fetched["rows"]
    result = {
        "ok": len(rows) > 0,
        "executed": execute,
        "qmt_stock_rows": fetched["stocks"],
        "qmt_index_rows": fetched["indices"],
        "rows_out": len(rows),
        "skipped": fetched["skipped"],
    }
    if execute:
        tmp = f"stocks_qmt_tmp_{safe_table_suffix()}"
        create_table_like(client, tmp, "stocks")
        if rows:
            client.insert(
                tmp,
                rows,
                column_names=["code", "name", "market", "type", "industry", "region", "list_date", "quit", "st"],
            )
        client.command("DROP TABLE IF EXISTS stocks_legacy_before_qmt_swap")
        client.command(f"RENAME TABLE stocks TO stocks_legacy_before_qmt_swap, {tmp} TO stocks")
        client.command("DROP TABLE IF EXISTS stocks_legacy_before_qmt_swap")
        result["target_rows"] = table_count(client, "stocks")
    return result


def fetch_qmt_sector_rows(include_all: bool) -> tuple[list[list[Any]], list[list[Any]], dict[str, Any]]:
    client = QmtMiniMarketClient()
    client.connect()
    download_sector_data_status = "not_called"
    try:
        xtdata = client._ensure_connected()
        if hasattr(xtdata, "download_sector_data"):
            xtdata.download_sector_data()
            download_sector_data_status = "ok"
    except Exception as exc:
        download_sector_data_status = f"{type(exc).__name__}: {exc}"
    sector_names = sorted({str(item).strip() for item in client.get_sector_list() if str(item).strip()})
    if not include_all:
        sector_names = [name for name in sector_names if _is_qmt_universe_sector(name)]
    now = datetime.now(BUSINESS_TZ).replace(tzinfo=None)
    sector_rows: list[list[Any]] = []
    mapping_rows: list[list[Any]] = []
    failed: list[dict[str, str]] = []
    for sector_name in sector_names:
        sector_code = f"qmt:{sector_name}"
        sector_type, level = _classify_qmt_sector(sector_name)
        sector_rows.append([sector_code, sector_name, sector_type, None, level, 0, now])
        try:
            stocks = client.get_stock_list_in_sector(sector_name) or []
        except Exception as exc:
            failed.append({"sector": sector_name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for code in stocks:
            code_text = str(code.get("Code") if isinstance(code, dict) else code).strip().upper()
            if code_text:
                mapping_rows.append([sector_code, code_text, 0.0, now])
    meta = {
        "sector_names": len(sector_names),
        "failed": failed[:50],
        "failed_count": len(failed),
        "download_sector_data_status": download_sector_data_status,
    }
    return sector_rows, mapping_rows, meta


def _official_universe_codes(client: Any) -> set[str]:
    try:
        rows = client.query("SELECT code FROM stocks").result_rows
    except Exception:
        return set()
    return {str(row[0]).strip().upper() for row in rows if row and str(row[0]).strip()}


def rebuild_sectors(client: Any, *, execute: bool, include_all: bool, probe_qmt: bool) -> dict[str, Any]:
    if not execute and not probe_qmt:
        return {"ok": True, "executed": False, "probe_qmt": False, "planned": "rebuild sectors and sector_stocks from QMT"}
    sector_rows, mapping_rows, meta = fetch_qmt_sector_rows(include_all)
    universe_codes = _official_universe_codes(client)
    dropped_non_universe = 0
    if universe_codes:
        before = len(mapping_rows)
        mapping_rows = [row for row in mapping_rows if str(row[1]).strip().upper() in universe_codes]
        dropped_non_universe = before - len(mapping_rows)
        mapped_sector_codes = {str(row[0]) for row in mapping_rows}
        sector_rows = [row for row in sector_rows if str(row[0]) in mapped_sector_codes]
    result = {
        "ok": bool(sector_rows) and bool(mapping_rows),
        "executed": execute,
        "sector_rows": len(sector_rows),
        "mapping_rows": len(mapping_rows),
        "dropped_non_universe_mappings": dropped_non_universe,
        **meta,
    }
    if execute and (not sector_rows or not mapping_rows):
        raise RuntimeError("QMT returned empty sector data; abort sector table swap")
    if execute:
        tmp_sectors = f"sectors_qmt_tmp_{safe_table_suffix()}"
        tmp_mapping = f"sector_stocks_qmt_tmp_{safe_table_suffix()}"
        create_table_like(client, tmp_sectors, "sectors")
        create_table_like(client, tmp_mapping, "sector_stocks")
        if sector_rows:
            client.insert(
                tmp_sectors,
                sector_rows,
                column_names=["code", "name", "type", "parent_code", "level", "stock_count", "created_at"],
            )
        if mapping_rows:
            client.insert(tmp_mapping, mapping_rows, column_names=["sector_code", "stock_code", "weight", "created_at"])
        # Refresh stock_count before swap.
        counted = f"sectors_qmt_counted_{safe_table_suffix()}"
        create_table_like(client, counted, "sectors")
        client.command(
            f"""
            INSERT INTO {counted} (code, name, type, parent_code, level, stock_count, created_at)
            SELECT
                s.code,
                s.name,
                s.type,
                s.parent_code,
                s.level,
                toInt32(ifNull(m.cnt, 0)) AS stock_count,
                s.created_at
            FROM {tmp_sectors} s
            LEFT JOIN (
                SELECT sector_code, count() AS cnt
                FROM {tmp_mapping}
                GROUP BY sector_code
            ) m ON m.sector_code = s.code
            """
        )
        client.command(f"DROP TABLE IF EXISTS {tmp_sectors}")
        client.command("DROP TABLE IF EXISTS sectors_legacy_before_qmt_swap")
        client.command("DROP TABLE IF EXISTS sector_stocks_legacy_before_qmt_swap")
        client.command(
            "RENAME TABLE "
            f"sectors TO sectors_legacy_before_qmt_swap, "
            f"sector_stocks TO sector_stocks_legacy_before_qmt_swap, "
            f"{counted} TO sectors, "
            f"{tmp_mapping} TO sector_stocks"
        )
        client.command("DROP TABLE IF EXISTS sectors_legacy_before_qmt_swap")
        client.command("DROP TABLE IF EXISTS sector_stocks_legacy_before_qmt_swap")
        result["target_sector_rows"] = table_count(client, "sectors")
        result["target_mapping_rows"] = table_count(client, "sector_stocks")
    return result


def truncate_sector_kline(client: Any, *, execute: bool) -> dict[str, Any]:
    before = table_count(client, "sector_kline_daily")
    if execute and table_exists(client, "sector_kline_daily"):
        client.command("TRUNCATE TABLE sector_kline_daily")
    return {"ok": True, "executed": execute, "before_rows": before, "after_rows": table_count(client, "sector_kline_daily") if execute else before}


def reset_price_range(client: Any, args: argparse.Namespace, *, execute: bool) -> dict[str, Any]:
    minute_start = args.minute_start_date or args.start_date
    minute_end = args.minute_end_date or args.end_date
    plan = {
        "kline_daily": {
            "date_column": "trade_date",
            "start_date": args.start_date,
            "end_date": args.end_date,
        },
        **{
            table: {
                "date_column": "datetime",
                "start_date": minute_start,
                "end_date": minute_end,
            }
            for table in MINUTE_TABLES.values()
        },
    }
    result: dict[str, Any] = {"ok": True, "executed": execute, "tables": {}}
    for table, item in plan.items():
        if not table_exists(client, table):
            result["tables"][table] = {"ok": False, "reason": "missing"}
            continue
        if item["date_column"] == "trade_date":
            before = int(
                client.query(
                    f"""
                    SELECT count()
                    FROM {table}
                    WHERE trade_date >= toDate({quote_sql(item["start_date"])})
                      AND trade_date <= toDate({quote_sql(item["end_date"])})
                    """
                ).first_row[0]
                or 0
            )
            if execute:
                client.command(
                    f"""
                    ALTER TABLE {table}
                    DELETE WHERE trade_date >= toDate({quote_sql(item["start_date"])})
                      AND trade_date <= toDate({quote_sql(item["end_date"])})
                    SETTINGS mutations_sync = 1
                    """
                )
        else:
            before = int(
                client.query(
                    f"""
                    SELECT count()
                    FROM {table}
                    WHERE toDate(datetime) >= toDate({quote_sql(item["start_date"])})
                      AND toDate(datetime) <= toDate({quote_sql(item["end_date"])})
                    """
                ).first_row[0]
                or 0
            )
            if execute:
                client.command(
                    f"""
                    ALTER TABLE {table}
                    DELETE WHERE toDate(datetime) >= toDate({quote_sql(item["start_date"])})
                      AND toDate(datetime) <= toDate({quote_sql(item["end_date"])})
                    SETTINGS mutations_sync = 1
                    """
                )
        result["tables"][table] = {
            "ok": True,
            "before_rows": before,
            "start_date": item["start_date"],
            "end_date": item["end_date"],
            "after_rows": table_count(client, table) if execute else None,
        }
    return result


def run_subprocess(cmd: list[str], timeout_sec: int, *, execute: bool) -> dict[str, Any]:
    if not execute:
        return {"ok": True, "executed": False, "cmd": cmd}
    started = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    return {
        "ok": proc.returncode == 0,
        "executed": True,
        "returncode": proc.returncode,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "cmd": cmd,
        "stdout_tail": (proc.stdout or "")[-4000:],
        "stderr_tail": (proc.stderr or "")[-4000:],
    }


def run_daily_rebuild(args: argparse.Namespace, run_dir: Path, *, execute: bool) -> dict[str, Any]:
    report = run_dir / "qmt_daily_rebuild.json"
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "qmtmini_daily_backfill_validate.py"),
        "--phase",
        "all",
        "--start-date",
        args.start_date,
        "--end-date",
        args.end_date,
        "--include-index",
        "--batch-size",
        str(args.daily_batch_size),
        "--delete-chunk-size",
        str(args.daily_delete_chunk_size),
        "--max-retries",
        str(args.max_retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--reset-stage",
        "--report",
        str(report),
    ]
    if args.limit > 0:
        cmd.extend(["--limit", str(args.limit)])
    return run_subprocess(cmd, args.daily_timeout_sec, execute=execute)


def run_sector_kline_rebuild(args: argparse.Namespace, *, execute: bool) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "generate_sector_kline.py"),
        "--mode",
        "range",
        "--start",
        args.start_date,
        "--end",
        args.end_date,
    ]
    return run_subprocess(cmd, args.sector_kline_timeout_sec, execute=execute)


def run_minute_rebuild(args: argparse.Namespace, run_dir: Path, *, execute: bool) -> dict[str, Any]:
    report_dir = run_dir / "qmt_minute_rebuild"
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_gap_audit_repair.py"),
        "--mode",
        "repair",
        "--start-date",
        args.minute_start_date or args.start_date,
        "--end-date",
        args.minute_end_date or args.end_date,
        "--periods",
        "5m,15m,30m,60m",
        "--universe",
        "stock,index",
        "--repair-batch-size",
        str(args.minute_batch_size),
        "--repair-code-chunk-size",
        str(args.minute_code_chunk_size),
        "--batch-timeout-sec",
        str(args.minute_batch_timeout_sec),
        "--chunk-timeout-sec",
        str(args.minute_chunk_timeout_sec),
        "--max-retries",
        str(args.max_retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--report-dir",
        str(report_dir),
    ]
    return run_subprocess(cmd, args.minute_timeout_sec, execute=execute)


def validate_official_tables(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    stock_rows = client.query(
        """
        SELECT
            count(),
            countIf(type = 'stock'),
            countIf(type = 'index')
        FROM stocks
        """
    ).first_row
    sector_rows = client.query("SELECT count(), uniqExact(code) FROM sectors").first_row
    mapping_rows = client.query("SELECT count(), uniqExact(stock_code), uniqExact(sector_code) FROM sector_stocks").first_row
    daily_rows = client.query(
        f"""
        SELECT count(), uniqExact(code), min(trade_date), max(trade_date)
        FROM kline_daily
        WHERE trade_date >= toDate({quote_sql(args.start_date)})
          AND trade_date <= toDate({quote_sql(args.end_date)})
        """
    ).first_row
    sector_daily_rows = client.query(
        f"""
        SELECT count(), uniqExact(code), min(trade_date), max(trade_date)
        FROM sector_kline_daily
        WHERE trade_date >= toDate({quote_sql(args.start_date)})
          AND trade_date <= toDate({quote_sql(args.end_date)})
        """
    ).first_row
    minute: dict[str, Any] = {}
    minute_start = args.minute_start_date or args.start_date
    minute_end = args.minute_end_date or args.end_date
    for period, table in MINUTE_TABLES.items():
        if not table_exists(client, table):
            minute[period] = {"exists": False}
            continue
        row = client.query(
            f"""
            SELECT count(), uniqExact(code), min(datetime), max(datetime)
            FROM {table}
            WHERE toDate(datetime) >= toDate({quote_sql(minute_start)})
              AND toDate(datetime) <= toDate({quote_sql(minute_end)})
            """
        ).first_row
        minute[period] = {
            "exists": True,
            "rows": int(row[0] or 0),
            "codes": int(row[1] or 0),
            "min_datetime": str(row[2]) if row[2] else None,
            "max_datetime": str(row[3]) if row[3] else None,
        }
    return {
        "stocks": {"rows": int(stock_rows[0] or 0), "stock_rows": int(stock_rows[1] or 0), "index_rows": int(stock_rows[2] or 0)},
        "sectors": {"rows": int(sector_rows[0] or 0), "codes": int(sector_rows[1] or 0)},
        "sector_stocks": {"rows": int(mapping_rows[0] or 0), "stocks": int(mapping_rows[1] or 0), "sectors": int(mapping_rows[2] or 0)},
        "daily": {
            "rows": int(daily_rows[0] or 0),
            "codes": int(daily_rows[1] or 0),
            "min_date": str(daily_rows[2]) if daily_rows[2] else None,
            "max_date": str(daily_rows[3]) if daily_rows[3] else None,
        },
        "sector_daily": {
            "rows": int(sector_daily_rows[0] or 0),
            "sectors": int(sector_daily_rows[1] or 0),
            "min_date": str(sector_daily_rows[2]) if sector_daily_rows[2] else None,
            "max_date": str(sector_daily_rows[3]) if sector_daily_rows[3] else None,
        },
        "minute": minute,
    }


def validate_thresholds(summary: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    checks = {
        "stocks_min": int(summary["stocks"]["stock_rows"]) >= args.min_stock_rows,
        "indices_min": int(summary["stocks"]["index_rows"]) >= args.min_index_rows,
        "sectors_min": int(summary["sectors"]["rows"]) >= args.min_sector_rows,
        "sector_members_min": int(summary["sector_stocks"]["rows"]) >= args.min_sector_member_rows,
        "daily_codes_min": int(summary["daily"]["codes"]) >= args.min_daily_codes,
    }
    if args.require_minute_validation:
        checks["minute_5m_codes_min"] = int(summary["minute"].get("5m", {}).get("codes") or 0) >= args.min_minute_5m_codes
    if args.require_sector_daily_validation:
        checks["sector_daily_rows_min"] = int(summary["sector_daily"]["rows"]) >= args.min_sector_daily_rows
    return {"ok": all(checks.values()), "checks": checks}


def validate_qmt_probe_thresholds(steps: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    checks: dict[str, bool] = {}
    stocks = steps.get("stocks")
    if isinstance(stocks, dict) and stocks.get("probe_qmt") is not False and "qmt_stock_rows" in stocks:
        checks["qmt_stock_rows_min"] = int(stocks.get("qmt_stock_rows") or 0) >= args.min_stock_rows
        checks["qmt_index_rows_min"] = int(stocks.get("qmt_index_rows") or 0) >= args.min_index_rows
        checks["qmt_security_rows_cover_daily_min"] = int(stocks.get("rows_out") or 0) >= args.min_daily_codes
    sectors = steps.get("sectors")
    if isinstance(sectors, dict) and sectors.get("probe_qmt") is not False and "sector_rows" in sectors:
        checks["qmt_sector_rows_min"] = int(sectors.get("sector_rows") or 0) >= args.min_sector_rows
        checks["qmt_sector_members_min"] = int(sectors.get("mapping_rows") or 0) >= args.min_sector_member_rows
    if not checks:
        return None
    return {"ok": all(checks.values()), "checks": checks}


def step_succeeded(step: Any) -> bool:
    if not isinstance(step, dict):
        return True
    if "ok" in step:
        return bool(step.get("ok"))
    return all(step_succeeded(item) for item in step.values())


def write_official_source_marker(result: dict[str, Any]) -> str:
    marker = runtime_path("official_market_universe_source.json")
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active_source": "qmt_xtquant",
        "provider": "qmt_xtquant_host_collector",
        "migration_stamp": result.get("stamp"),
        "migration_report": str(result.get("report_path") or ""),
        "updated_at": datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "rules": {
            "legacy_tdx_official_write": "disabled",
            "qmt_minute_source": "5m_only",
            "derived_minutes": ["15m", "30m", "60m"],
            "strategy_tables": ["kline_daily", "kline_minute_5", "kline_minute_15", "kline_minute_30", "kline_minute_60"],
        },
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(marker)


def should_write_official_source_marker(result: dict[str, Any]) -> bool:
    for key in ("stocks", "sectors", "daily", "sector_kline_daily", "minute"):
        step = result.get("steps", {}).get(key)
        if isinstance(step, dict) and step.get("executed"):
            return True
    return False


def parse_args() -> argparse.Namespace:
    today = datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="Migrate AiStock official market universe to QMT-only data source.")
    parser.add_argument("--execute", action="store_true", help="Actually rebuild official tables. Default is dry-run.")
    parser.add_argument(
        "--confirm-official-qmt-switch",
        action="store_true",
        help="Required with --execute. Confirms official market tables and target price ranges may be rebuilt from QMT.",
    )
    parser.add_argument("--probe-qmt", action="store_true", help="In dry-run mode, connect to QMT and count source rows.")
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default=today)
    parser.add_argument("--minute-start-date", default="")
    parser.add_argument("--minute-end-date", default="")
    parser.add_argument("--limit", type=int, default=0, help="Limit QMT daily codes for smoke tests.")
    parser.add_argument("--skip-backup", action="store_true")
    parser.add_argument("--skip-stock-rebuild", action="store_true")
    parser.add_argument("--skip-sector-rebuild", action="store_true")
    parser.add_argument("--skip-daily-rebuild", action="store_true")
    parser.add_argument("--skip-sector-kline-rebuild", action="store_true")
    parser.add_argument("--skip-minute-rebuild", action="store_true")
    parser.add_argument(
        "--reset-price-range",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Before QMT rebuild, delete official daily/minute rows in the target date range after backups.",
    )
    parser.add_argument("--qmt-sector-include-all", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--daily-batch-size", type=int, default=80)
    parser.add_argument("--daily-delete-chunk-size", type=int, default=200)
    parser.add_argument("--daily-timeout-sec", type=int, default=6 * 60 * 60)
    parser.add_argument("--backup-date-chunk-days", type=int, default=7)
    parser.add_argument("--sector-kline-timeout-sec", type=int, default=2 * 60 * 60)
    parser.add_argument("--minute-batch-size", type=int, default=1, help="QMT minute source batch. Keep 1 unless proven safe.")
    parser.add_argument("--minute-code-chunk-size", type=int, default=1)
    parser.add_argument("--minute-batch-timeout-sec", type=int, default=180)
    parser.add_argument("--minute-chunk-timeout-sec", type=int, default=900)
    parser.add_argument("--minute-timeout-sec", type=int, default=8 * 60 * 60)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=1.0)
    parser.add_argument("--min-stock-rows", type=int, default=4500)
    parser.add_argument("--min-index-rows", type=int, default=20)
    parser.add_argument("--min-sector-rows", type=int, default=30)
    parser.add_argument("--min-sector-member-rows", type=int, default=3000)
    parser.add_argument("--min-daily-codes", type=int, default=5000)
    parser.add_argument("--min-sector-daily-rows", type=int, default=1)
    parser.add_argument("--min-minute-5m-codes", type=int, default=4500)
    parser.add_argument("--require-sector-daily-validation", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--require-minute-validation", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--report", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stamp = safe_table_suffix()
    run_dir = report_path("qmt_official_market_universe_migration", f"run_{stamp}")
    run_dir.mkdir(parents=True, exist_ok=True)
    report = Path(args.report) if args.report else run_dir / "summary.json"
    execute = bool(args.execute)
    log(f"QMT official universe migration start execute={execute} run_dir={run_dir}")

    result: dict[str, Any] = {
        "ok": False,
        "executed": execute,
        "stamp": stamp,
        "run_dir": str(run_dir),
        "args": vars(args),
        "steps": {},
    }
    if execute and not args.confirm_official_qmt_switch:
        result["error"] = "Missing --confirm-official-qmt-switch for --execute"
        result["report_path"] = str(report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
        log(f"migration refused: {result['error']} report={report}")
        return 2

    client = ch_client()
    try:
        result["precheck"] = validate_official_tables(client, args)
        if not args.skip_backup:
            log("backup official tables")
            result["steps"]["backup"] = backup_tables(client, OFFICIAL_BACKUP_TABLES, stamp, args, execute=execute)
        if not args.skip_stock_rebuild:
            log("rebuild stocks from QMT")
            result["steps"]["stocks"] = rebuild_stocks(client, execute=execute, probe_qmt=bool(args.probe_qmt))
        if not args.skip_sector_rebuild:
            log("rebuild sectors and sector_stocks from QMT")
            result["steps"]["sectors"] = rebuild_sectors(
                client,
                execute=execute,
                include_all=args.qmt_sector_include_all,
                probe_qmt=bool(args.probe_qmt),
            )
            result["steps"]["sector_kline_daily_reset"] = truncate_sector_kline(client, execute=execute)
        if args.reset_price_range and (not args.skip_daily_rebuild or not args.skip_minute_rebuild):
            log("reset official price rows in target ranges")
            result["steps"]["price_range_reset"] = reset_price_range(client, args, execute=execute)
        if not args.skip_daily_rebuild:
            log("run QMT daily rebuild")
            result["steps"]["daily"] = run_daily_rebuild(args, run_dir, execute=execute)
        if not args.skip_sector_kline_rebuild and not args.skip_daily_rebuild:
            log("rebuild sector_kline_daily from rebuilt QMT universe and daily prices")
            result["steps"]["sector_kline_daily"] = run_sector_kline_rebuild(args, execute=execute)
        if not args.skip_minute_rebuild:
            log("run QMT minute rebuild from 5m source")
            result["steps"]["minute"] = run_minute_rebuild(args, run_dir, execute=execute)
        result["postcheck"] = validate_official_tables(client, args)
        result["thresholds"] = validate_thresholds(result["postcheck"], args)
        result["qmt_probe_thresholds"] = validate_qmt_probe_thresholds(result["steps"], args)
        step_ok = all(step_succeeded(step) for step in result["steps"].values())
        qmt_probe_ok = True if result["qmt_probe_thresholds"] is None else bool(result["qmt_probe_thresholds"]["ok"])
        result["ok"] = bool(step_ok and result["thresholds"]["ok"] and qmt_probe_ok)
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(f"migration failed: {result['error']}")
    report.parent.mkdir(parents=True, exist_ok=True)
    result["report_path"] = str(report)
    if execute and result.get("ok") and should_write_official_source_marker(result):
        result["official_source_marker"] = write_official_source_marker(result)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    log(f"migration summary ok={result['ok']} report={report}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
