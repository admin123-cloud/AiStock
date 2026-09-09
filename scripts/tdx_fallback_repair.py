from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df
from utils.kline_store import filter_trading_day_tuples
from utils.kline_units import normalize_tdxquant_daily_units
from utils.paths import report_path, runtime_path


ALIAS_TABLE = "source_instrument_alias"
AUDIT_TABLE = "tdx_fallback_repair_audit"
SOURCE = "tdx"
TZ = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _existing_keys_by_code(table: str, code_values: Iterable[str], start_expr: str, end_expr: str, date_column: str) -> set[tuple[str, object]]:
    """Read existing keys in bounded chunks so ClickHouse query-size limits do not abort full-universe repairs."""
    codes = list(dict.fromkeys(str(code) for code in code_values))
    result: set[tuple[str, object]] = set()
    for offset in range(0, len(codes), 500):
        code_list = ",".join(sql_literal(code) for code in codes[offset : offset + 500])
        date_filter = date_column if date_column == "trade_date" else f"toDate({date_column})"
        existing = clickhouse_query_df(
            f"SELECT code, {date_column} FROM {table} WHERE code IN ({code_list}) "
            f"AND {date_filter} BETWEEN toDate({sql_literal(start_expr)}) AND toDate({sql_literal(end_expr)})"
        )
        result.update((str(row.code), pd.to_datetime(getattr(row, date_column)).date() if date_column == "trade_date" else pd.to_datetime(getattr(row, date_column)).to_pydatetime()) for row in existing.itertuples(index=False))
    return result


def stable_id(*parts: object) -> int:
    raw = "|".join("" if part is None else str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") & ((1 << 63) - 1)


def normalize_code(code: str) -> tuple[str, str, str]:
    text = str(code).strip().upper()
    if "." in text:
        bare, market = text.split(".", 1)
    else:
        bare, market = text, ""
    return text, bare, market


def tdx_request_code(source_code: str, source_market: str) -> str:
    market = str(source_market or "").strip().upper()
    code = str(source_code).strip().upper()
    return f"{code}.{market}" if market else code


def ensure_tables() -> None:
    client = clickhouse_client()
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {ALIAS_TABLE}
        (
            source String,
            canonical_code String,
            canonical_name String,
            canonical_market String,
            canonical_type String,
            source_code String,
            source_name String,
            source_market String,
            alias_status String,
            match_method String,
            confidence Float64,
            note String,
            created_at DateTime,
            updated_at DateTime,
            id UInt64
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (source, canonical_code, source_code, source_market)
        """
    )
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}
        (
            request_id String,
            stage String,
            period String,
            target_table String,
            start_date Date,
            end_date Date,
            canonical_code String,
            source String,
            source_code String,
            status String,
            reason String,
            qmt_rows UInt64,
            tdx_rows UInt64,
            applied_rows UInt64,
            diff_close_max Float64,
            diff_volume_ratio_max Float64,
            created_at DateTime,
            details String,
            id UInt64
        )
        ENGINE = MergeTree
        ORDER BY (created_at, request_id, period, canonical_code)
        """
    )


def write_report(name: str, payload: dict) -> Path:
    filename = f"{now_cn().strftime('%Y%m%d_%H%M%S_%f')}_{os.getpid()}_{name}.json"
    candidates = [
        report_path("tdx_fallback_repair", filename),
        runtime_path("tdx_fallback_repair", filename),
    ]
    content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    last_error: Exception | None = None
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return path
        except (PermissionError, OSError) as exc:
            last_error = exc
    raise RuntimeError(f"Unable to write TDX fallback report: {last_error}")


def sync_alias_from_qmt(execute: bool) -> dict:
    ensure_tables()
    stocks = clickhouse_query_df(
        """
        SELECT code, name, market, type
        FROM stocks
        WHERE type IN ('stock', 'index')
        ORDER BY code
        """
    )
    ts = now_cn()
    rows = []
    for row in stocks.itertuples(index=False):
        canonical_code, source_code, source_market = normalize_code(row.code)
        rows.append(
            (
                SOURCE,
                canonical_code,
                str(row.name or ""),
                str(row.market or ""),
                str(row.type or ""),
                source_code,
                str(row.name or ""),
                source_market,
                "active",
                "qmt_canonical_rule_v1",
                0.95,
                "seeded_from_qmt_canonical_universe",
                ts,
                ts,
                stable_id(SOURCE, canonical_code, source_code, source_market),
            )
        )
    if execute and rows:
        clickhouse_client().insert(
            ALIAS_TABLE,
            rows,
            column_names=[
                "source",
                "canonical_code",
                "canonical_name",
                "canonical_market",
                "canonical_type",
                "source_code",
                "source_name",
                "source_market",
                "alias_status",
                "match_method",
                "confidence",
                "note",
                "created_at",
                "updated_at",
                "id",
            ],
        )
    payload = {"execute": execute, "alias_rows_prepared": len(rows), "source": SOURCE}
    payload["report_path"] = str(write_report("sync_alias_from_qmt", payload))
    return payload


def parse_codes(raw_codes: str | None) -> list[str]:
    if not raw_codes:
        return []
    return [normalize_code(item)[0] for item in raw_codes.replace(";", ",").split(",") if item.strip()]


def load_codes(raw_codes: str | None, limit: int) -> list[str]:
    codes = parse_codes(raw_codes)
    if codes:
        return codes[:limit] if limit > 0 else codes
    suffix = f"LIMIT {int(limit)}" if limit > 0 else ""
    df = clickhouse_query_df(
        f"""
        SELECT code
        FROM stocks
        WHERE type IN ('stock', 'index')
        ORDER BY code
        {suffix}
        """
    )
    return [str(code) for code in df["code"].tolist()]


def load_aliases(codes: Iterable[str]) -> pd.DataFrame:
    codes = list(codes)
    if not codes:
        return pd.DataFrame()
    code_list = ",".join(sql_literal(code) for code in codes)
    return clickhouse_query_df(
        f"""
        SELECT canonical_code, canonical_name, canonical_type, source_code, source_name, source_market
        FROM {ALIAS_TABLE} FINAL
        WHERE source = {sql_literal(SOURCE)}
          AND alias_status = 'active'
          AND canonical_code IN ({code_list})
        """
    )


def load_trade_dates(start_date: str, end_date: str) -> list[date]:
    sql = f"""
    SELECT trade_date
    FROM trade_calendar
    WHERE trade_date BETWEEN toDate({sql_literal(start_date)}) AND toDate({sql_literal(end_date)})
      AND market = 'SH'
      AND is_trading = 1
    ORDER BY trade_date
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return []
    return [pd.to_datetime(x).date() for x in df["trade_date"].tolist()]


def insert_audit(rows: list[tuple]) -> None:
    if not rows:
        return
    clickhouse_client().insert(
        AUDIT_TABLE,
        rows,
        column_names=[
            "request_id",
            "stage",
            "period",
            "target_table",
            "start_date",
            "end_date",
            "canonical_code",
            "source",
            "source_code",
            "status",
            "reason",
            "qmt_rows",
            "tdx_rows",
            "applied_rows",
            "diff_close_max",
            "diff_volume_ratio_max",
            "created_at",
            "details",
            "id",
        ],
    )


def audit_daily_gaps(args: argparse.Namespace) -> dict:
    ensure_tables()
    codes = load_codes(args.codes, args.limit)
    aliases = load_aliases(codes)
    alias_by_code = {row.canonical_code: row for row in aliases.itertuples(index=False)}
    trade_dates = load_trade_dates(args.start_date, args.end_date)
    if not trade_dates:
        raise RuntimeError("No trading dates found for the requested range.")

    code_list = ",".join(sql_literal(code) for code in codes)
    existing = clickhouse_query_df(
        f"""
        SELECT code, trade_date
        FROM kline_daily
        WHERE code IN ({code_list})
          AND trade_date BETWEEN toDate({sql_literal(args.start_date)}) AND toDate({sql_literal(args.end_date)})
        """
    )
    existing_keys = {(str(row.code), pd.to_datetime(row.trade_date).date()) for row in existing.itertuples(index=False)}
    audit_rows = []
    missing = []
    ts = now_cn()
    for code in codes:
        alias = alias_by_code.get(code)
        for trade_day in trade_dates:
            if (code, trade_day) in existing_keys:
                continue
            source_code = "" if alias is None else str(alias.source_code)
            status = "blocked" if alias is None else "planned"
            reason = "missing_alias" if alias is None else "missing_qmt_daily"
            missing.append({"code": code, "trade_date": str(trade_day), "source_code": source_code, "status": status})
            audit_rows.append(
                (
                    args.request_id,
                    "gap_audit",
                    "1d",
                    "kline_daily",
                    date.fromisoformat(args.start_date),
                    date.fromisoformat(args.end_date),
                    code,
                    SOURCE,
                    source_code,
                    status,
                    reason,
                    0,
                    0,
                    0,
                    0.0,
                    0.0,
                    ts,
                    json.dumps({"trade_date": str(trade_day)}, ensure_ascii=False),
                    stable_id(args.request_id, "gap_audit", "1d", code, trade_day),
                )
            )
    if args.execute:
        insert_audit(audit_rows)
    payload = {
        "execute": bool(args.execute),
        "request_id": args.request_id,
        "period": "1d",
        "codes_checked": len(codes),
        "trade_dates_checked": len(trade_dates),
        "missing_count": len(missing),
        "missing_preview": missing[:50],
    }
    payload["report_path"] = str(write_report("audit_daily_gaps", payload))
    return payload


def expected_minute_bars(period: str) -> int:
    mapping = {"5m": 48, "15m": 16, "30m": 8, "60m": 4}
    if period not in mapping:
        raise ValueError(f"Unsupported minute period: {period}")
    return mapping[period]


def minute_target_table(period: str) -> str:
    if period not in {"5m", "15m", "30m", "60m"}:
        raise ValueError(f"Unsupported minute period: {period}")
    return f"kline_minute_{period[:-1]}"


def audit_minute_gaps(args: argparse.Namespace) -> dict:
    ensure_tables()
    period = args.period
    target_table = minute_target_table(period)
    codes = load_codes(args.codes, args.limit)
    aliases = load_aliases(codes)
    alias_by_code = {row.canonical_code: row for row in aliases.itertuples(index=False)}
    code_list = ",".join(sql_literal(code) for code in codes)
    counts = clickhouse_query_df(
        f"""
        SELECT code, count() AS rows_count
        FROM {target_table}
        WHERE code IN ({code_list})
          AND toDate(datetime) BETWEEN toDate({sql_literal(args.start_date)}) AND toDate({sql_literal(args.end_date)})
        GROUP BY code
        """
    )
    expected = expected_minute_bars(period) * len(load_trade_dates(args.start_date, args.end_date))
    count_by_code = {str(row.code): int(row.rows_count) for row in counts.itertuples(index=False)}
    audit_rows = []
    missing = []
    ts = now_cn()
    for code in codes:
        got = count_by_code.get(code, 0)
        if got >= expected:
            continue
        alias = alias_by_code.get(code)
        source_code = "" if alias is None else str(alias.source_code)
        status = "blocked" if alias is None else "planned"
        reason = "missing_alias" if alias is None else "minute_count_below_expected"
        missing.append({"code": code, "qmt_rows": got, "expected_rows": expected, "source_code": source_code, "status": status})
        audit_rows.append(
            (
                args.request_id,
                "gap_audit",
                period,
                target_table,
                date.fromisoformat(args.start_date),
                date.fromisoformat(args.end_date),
                code,
                SOURCE,
                source_code,
                status,
                reason,
                got,
                0,
                0,
                0.0,
                0.0,
                ts,
                json.dumps({"expected_rows": expected}, ensure_ascii=False),
                stable_id(args.request_id, "gap_audit", period, code, args.start_date, args.end_date),
            )
        )
    if args.execute:
        insert_audit(audit_rows)
    payload = {
        "execute": bool(args.execute),
        "request_id": args.request_id,
        "period": period,
        "target_table": target_table,
        "codes_checked": len(codes),
        "expected_rows_per_code": expected,
        "gap_code_count": len(missing),
        "gap_preview": missing[:50],
    }
    payload["report_path"] = str(write_report("audit_minute_gaps", payload))
    return payload


def tdx_field_frame(data: dict, field: str):
    if not isinstance(data, dict):
        return None
    for key in [field, field.lower(), field.upper(), field.capitalize()]:
        frame = data.get(key)
        if frame is not None:
            return frame
    return None


def frame_value(data: dict, field: str, idx, code: str) -> float:
    frame = tdx_field_frame(data, field)
    if frame is None or code not in frame.columns:
        return 0.0
    value = frame.at[idx, code]
    if pd.isna(value):
        return 0.0
    return float(value)


def fetch_daily_from_tdx(args: argparse.Namespace) -> dict:
    ensure_tables()
    if not args.allow_legacy_tdx:
        raise RuntimeError("TDX fetch is gated. Re-run with --allow-legacy-tdx when QMT repair has failed.")
    os.environ["AISTOCK_ALLOW_LEGACY_TDX"] = "1"
    from data_fetcher.sources.tdxquant_pool import tdxquant_pool

    aliases = load_aliases(load_codes(args.codes, args.limit))
    if aliases.empty:
        raise RuntimeError("No active TDX aliases found for requested codes.")

    request_codes = [tdx_request_code(row.source_code, row.source_market) for row in aliases.itertuples(index=False)]
    data = tdxquant_pool.get_market_data(
        field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
        stock_list=request_codes,
        period="1d",
        start_time=args.start_date.replace("-", ""),
        end_time=args.end_date.replace("-", ""),
        count=-1,
        dividend_type="none",
        fill_data=False,
    )
    rows = []
    audit_rows = []
    ts = now_cn()
    alias_rows = list(aliases.itertuples(index=False))
    for alias in alias_rows:
        request_code = tdx_request_code(alias.source_code, alias.source_market)
        close_frame = tdx_field_frame(data, "Close")
        if close_frame is None or request_code not in close_frame.columns:
            continue
        prev_close = 0.0
        for idx, close_value in close_frame[request_code].dropna().items():
            trade_date = pd.to_datetime(idx).date()
            values = {field: frame_value(data, field, idx, request_code) for field in ["Open", "High", "Low", "Close", "Volume", "Amount"]}
            normalized = normalize_tdxquant_daily_units(
                pd.DataFrame([{
                    "volume": values["Volume"],
                    "amount": values["Amount"],
                }]),
                instrument_type=str(alias.canonical_type or "stock"),
            ).iloc[0]
            values["Volume"] = float(normalized["volume"])
            values["Amount"] = float(normalized["amount"])
            change_amount = values["Close"] - prev_close if prev_close else 0.0
            change_pct = change_amount / prev_close * 100.0 if prev_close else 0.0
            amplitude = 0.0
            if prev_close:
                amplitude = (values["High"] - values["Low"]) / prev_close * 100.0
            rows.append(
                (
                    str(alias.canonical_code),
                    trade_date,
                    values["Open"],
                    values["High"],
                    values["Low"],
                    values["Close"],
                    values["Volume"],
                    values["Amount"],
                    amplitude,
                    change_pct,
                    change_amount,
                    0.0,
                    ts,
                    stable_id(args.request_id, str(alias.canonical_code), trade_date),
                )
            )
            prev_close = values["Close"]

    existing_keys: set[tuple[str, date]] = set()
    if rows:
        existing_keys = _existing_keys_by_code(
            "kline_daily", [row[0] for row in rows], args.start_date, args.end_date, "trade_date"
        )
    missing_rows = [row for row in rows if (str(row[0]), row[1]) not in existing_keys]

    applied_by_code: dict[str, int] = {}
    for row in missing_rows:
        applied_by_code[str(row[0])] = applied_by_code.get(str(row[0]), 0) + 1
    for alias in alias_rows:
        code = str(alias.canonical_code)
        fetched_count = sum(1 for row in rows if str(row[0]) == code)
        applied_count = applied_by_code.get(code, 0)
        audit_rows.append(
            (
                args.request_id,
                "direct_write",
                "1d",
                "kline_daily",
                date.fromisoformat(args.start_date),
                date.fromisoformat(args.end_date),
                code,
                SOURCE,
                str(alias.source_code),
                "applied" if applied_count else ("skipped_existing" if fetched_count else "no_tdx_rows"),
                "tdx_alias_direct_to_canonical",
                0,
                fetched_count,
                applied_count,
                0.0,
                0.0,
                ts,
                json.dumps({"source_name": str(alias.source_name)}, ensure_ascii=False),
                stable_id(args.request_id, "direct_write", "1d", code),
            )
        )

    if args.execute and missing_rows:
        daily_columns = [
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
        missing_rows = filter_trading_day_tuples("1d", missing_rows, daily_columns)
        if missing_rows:
            clickhouse_client().insert(
                "kline_daily",
                missing_rows,
                column_names=daily_columns,
            )
    if args.execute:
        insert_audit(audit_rows)
    payload = {
        "execute": bool(args.execute),
        "request_id": args.request_id,
        "request_code_count": len(request_codes),
        "rows_fetched": len(rows),
        "missing_rows_ready_to_write": len(missing_rows),
        "target_table": "kline_daily",
    }
    payload["report_path"] = str(write_report("fetch_daily_from_tdx", payload))
    return payload


def fetch_minute_from_tdx(args: argparse.Namespace) -> dict:
    ensure_tables()
    if not args.allow_legacy_tdx:
        raise RuntimeError("TDX fetch is gated. Re-run with --allow-legacy-tdx when QMT repair has failed.")
    os.environ["AISTOCK_ALLOW_LEGACY_TDX"] = "1"
    from data_fetcher.sources.tdxquant_pool import tdxquant_pool

    period = args.period
    aliases = load_aliases(load_codes(args.codes, args.limit))
    if aliases.empty:
        raise RuntimeError("No active TDX aliases found for requested codes.")

    request_codes = [tdx_request_code(row.source_code, row.source_market) for row in aliases.itertuples(index=False)]
    data = tdxquant_pool.get_market_data(
        field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
        stock_list=request_codes,
        period=period,
        start_time=args.start_date.replace("-", ""),
        end_time=args.end_date.replace("-", ""),
        count=-1,
        dividend_type="none",
        fill_data=False,
    )
    rows = []
    audit_rows = []
    ts = now_cn()
    for alias in aliases.itertuples(index=False):
        request_code = tdx_request_code(alias.source_code, alias.source_market)
        close_frame = tdx_field_frame(data, "Close")
        if close_frame is None or request_code not in close_frame.columns:
            continue
        for idx, _close_value in close_frame[request_code].dropna().items():
            bar_dt = pd.to_datetime(idx).to_pydatetime()
            values = {field: frame_value(data, field, idx, request_code) for field in ["Open", "High", "Low", "Close", "Volume", "Amount"]}
            rows.append(
                (
                    str(alias.canonical_code),
                    bar_dt,
                    values["Open"],
                    values["High"],
                    values["Low"],
                    values["Close"],
                    int(round(values["Volume"])) if period == "5m" else values["Volume"],
                    values["Amount"],
                    ts,
                    stable_id(args.request_id, period, str(alias.canonical_code), bar_dt),
                )
            )

    target_table = minute_target_table(period)
    existing_keys: set[tuple[str, datetime]] = set()
    if rows:
        existing_keys = _existing_keys_by_code(
            target_table, [row[0] for row in rows], args.start_date, args.end_date, "datetime"
        )
    missing_rows = [row for row in rows if (str(row[0]), row[1]) not in existing_keys]

    applied_by_code: dict[str, int] = {}
    for row in missing_rows:
        applied_by_code[str(row[0])] = applied_by_code.get(str(row[0]), 0) + 1
    for alias in aliases.itertuples(index=False):
        code = str(alias.canonical_code)
        fetched_count = sum(1 for row in rows if str(row[0]) == code)
        applied_count = applied_by_code.get(code, 0)
        audit_rows.append(
            (
                args.request_id,
                "direct_write",
                period,
                target_table,
                date.fromisoformat(args.start_date),
                date.fromisoformat(args.end_date),
                code,
                SOURCE,
                str(alias.source_code),
                "applied" if applied_count else ("skipped_existing" if fetched_count else "no_tdx_rows"),
                "tdx_alias_direct_to_canonical",
                0,
                fetched_count,
                applied_count,
                0.0,
                0.0,
                ts,
                json.dumps({"source_name": str(alias.source_name)}, ensure_ascii=False),
                stable_id(args.request_id, "direct_write", period, code),
            )
        )

    if args.execute and missing_rows:
        minute_columns = [
            "code",
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "created_at",
            "id",
        ]
        missing_rows = filter_trading_day_tuples(period, missing_rows, minute_columns)
        if missing_rows:
            clickhouse_client().insert(
                target_table,
                missing_rows,
                column_names=minute_columns,
            )
    if args.execute:
        insert_audit(audit_rows)
    payload = {
        "execute": bool(args.execute),
        "request_id": args.request_id,
        "period": period,
        "request_code_count": len(request_codes),
        "rows_fetched": len(rows),
        "missing_rows_ready_to_write": len(missing_rows),
        "target_table": target_table,
    }
    payload["report_path"] = str(write_report("fetch_minute_from_tdx", payload))
    return payload


def validate_setup(args: argparse.Namespace) -> dict:
    ensure_tables()
    tables = [ALIAS_TABLE, AUDIT_TABLE]
    table_list = ",".join(sql_literal(table) for table in tables)
    existing = clickhouse_query_df(
        f"""
        SELECT name, total_rows
        FROM system.tables
        WHERE database = currentDatabase()
          AND name IN ({table_list})
        ORDER BY name
        """
    )
    stocks_count = int(clickhouse_query_df("SELECT count() AS c FROM stocks WHERE type IN ('stock', 'index')").iloc[0]["c"])
    alias_stats = clickhouse_query_df(
        f"""
        SELECT
            count() AS alias_rows,
            uniqExact(canonical_code) AS canonical_codes,
            countIf(alias_status = 'active') AS active_rows,
            count() - uniqExact(source, canonical_code, source_code, source_market) AS duplicate_keys
        FROM {ALIAS_TABLE} FINAL
        WHERE source = {sql_literal(SOURCE)}
        """
    )
    alias_row = alias_stats.iloc[0]
    ok = (
        len(existing) == len(tables)
        and int(alias_row["active_rows"]) >= stocks_count
        and int(alias_row["duplicate_keys"]) == 0
    )
    payload = {
        "ok": bool(ok),
        "tables_expected": tables,
        "tables_found": existing.to_dict(orient="records"),
        "stocks_count": stocks_count,
        "alias_rows": int(alias_row["alias_rows"]),
        "active_alias_rows": int(alias_row["active_rows"]),
        "alias_canonical_codes": int(alias_row["canonical_codes"]),
        "duplicate_alias_keys": int(alias_row["duplicate_keys"]),
    }
    payload["report_path"] = str(write_report("validate_setup", payload))
    if args.fail_on_error and not ok:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, default=str))
    return payload


def default_request_id() -> str:
    return "tdx_fallback_" + now_cn().strftime("%Y%m%d_%H%M%S")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Controlled TDX fallback repair path for QMT canonical data.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-schema")

    p = sub.add_parser("sync-alias-from-qmt")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("audit-gaps")
    p.add_argument("--period", default="1d", choices=["1d", "5m", "15m", "30m", "60m"])
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--codes")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--request-id", default=default_request_id())

    p = sub.add_parser("fetch-daily-from-tdx")
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--codes")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--request-id", default=default_request_id())
    p.add_argument("--allow-legacy-tdx", action="store_true")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("fetch-minute-from-tdx")
    p.add_argument("--period", required=True, choices=["5m", "15m", "30m", "60m"])
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--codes")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--request-id", default=default_request_id())
    p.add_argument("--allow-legacy-tdx", action="store_true")
    p.add_argument("--execute", action="store_true")

    p = sub.add_parser("validate-setup")
    p.add_argument("--fail-on-error", action="store_true")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "init-schema":
        ensure_tables()
        payload = {"created_or_verified": [ALIAS_TABLE, AUDIT_TABLE]}
        payload["report_path"] = str(write_report("init_schema", payload))
    elif args.command == "sync-alias-from-qmt":
        payload = sync_alias_from_qmt(args.execute)
    elif args.command == "audit-gaps":
        payload = audit_daily_gaps(args) if args.period == "1d" else audit_minute_gaps(args)
    elif args.command == "fetch-daily-from-tdx":
        payload = fetch_daily_from_tdx(args)
    elif args.command == "fetch-minute-from-tdx":
        payload = fetch_minute_from_tdx(args)
    elif args.command == "validate-setup":
        payload = validate_setup(args)
    else:
        raise AssertionError(args.command)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
