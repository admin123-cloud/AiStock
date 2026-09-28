"""
Build an audited TDX sector snapshot for G3 mainline experiments.

This script does not modify canonical Shenwan/QMT tables. It writes only
source-side reference tables when --execute is passed:

- source_sectors
- source_sector_stocks

G3 can use these tables for TDX mainline strength. Stock industry ownership,
attribution, and exposure continue to use Shenwan/canonical industry tables.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from data_fetcher.sources.tdx_gateway_client import TdxGatewayClient
from utils.market_warehouse import clickhouse_client, clickhouse_query_df
from utils.paths import report_path


SOURCE = "tdx"
SECTOR_TABLE = "source_sectors"
MEMBER_TABLE = "source_sector_stocks"
REPORT_DIR = report_path("tdx_sector_snapshot_v1")


def first_value(row: Any, keys: list[str]) -> str:
    if isinstance(row, dict):
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return str(value).strip()
    for key in keys:
        if hasattr(row, key):
            value = getattr(row, key)
            if value not in (None, ""):
                return str(value).strip()
    return ""


def normalize_sector(row: Any, index: int) -> dict[str, Any]:
    code = first_value(row, ["code", "Code", "sector_code", "block_code", "id", "ID"])
    name = first_value(row, ["name", "Name", "sector_name", "block_name", "sector", "block"])
    sector_type = first_value(row, ["type", "Type", "sector_type", "block_type", "category"])
    level = first_value(row, ["level", "Level", "sector_level"])
    if not code and name:
        code = name
    if not name and code:
        name = code
    try:
        level_num = int(float(level)) if level not in ("", None) else 0
    except Exception:
        level_num = 0
    return {
        "source": SOURCE,
        "sector_code": code or f"tdx_sector_{index}",
        "sector_name": name or code or f"tdx_sector_{index}",
        "sector_type": sector_type or "tdx_sector",
        "level": level_num,
    }


def normalize_stock_code(row: Any) -> str:
    code = first_value(row, ["code", "Code", "stock_code", "StockCode"])
    if not code:
        return ""
    code = code.strip().upper()
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return code


def ensure_tables() -> None:
    ch = clickhouse_client()
    ch.command(
        f"""
        CREATE TABLE IF NOT EXISTS {SECTOR_TABLE}
        (
            source String,
            sector_code String,
            sector_name String,
            sector_type String,
            level Int32 DEFAULT 0,
            canonical_sector_code String DEFAULT '',
            canonical_sector_name String DEFAULT '',
            match_method String DEFAULT '',
            confidence Float64 DEFAULT 0,
            alias_status String DEFAULT '',
            snapshot_date Date,
            snapshot_at DateTime,
            id String
        )
        ENGINE = ReplacingMergeTree(snapshot_at)
        ORDER BY (source, snapshot_date, sector_code, canonical_sector_code)
        """
    )
    ch.command(
        f"""
        CREATE TABLE IF NOT EXISTS {MEMBER_TABLE}
        (
            source String,
            sector_code String,
            sector_name String,
            sector_type String,
            source_stock_code String,
            canonical_code String,
            alias_status String DEFAULT '',
            match_method String DEFAULT '',
            confidence Float64 DEFAULT 0,
            snapshot_date Date,
            snapshot_at DateTime,
            id String
        )
        ENGINE = ReplacingMergeTree(snapshot_at)
        ORDER BY (source, snapshot_date, sector_code, canonical_code, source_stock_code)
        """
    )


def load_stock_alias_map() -> dict[str, str]:
    df = clickhouse_query_df(
        """
        SELECT canonical_code, source_code, source_market
        FROM source_instrument_alias
        WHERE source = 'tdx'
          AND alias_status = 'active'
        """
    )
    mapping: dict[str, str] = {}
    for row in df.itertuples(index=False):
        canonical = str(row.canonical_code).strip().upper()
        source_code = str(row.source_code).strip().upper()
        source_market = str(row.source_market).strip().upper()
        if source_code:
            mapping[source_code] = canonical
            if "." not in source_code and source_market in {"SH", "SZ", "BJ"}:
                mapping[f"{source_code}.{source_market}"] = canonical
    return mapping


def load_sw_l2_name_map() -> dict[str, tuple[str, str]]:
    df = clickhouse_query_df(
        """
        SELECT code, name
        FROM sectors
        WHERE type = 'industry'
          AND level = 2
        """
    )
    return {str(row.name).strip(): (str(row.code), str(row.name)) for row in df.itertuples(index=False)}


def fetch_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    client = TdxGatewayClient(args.gateway_url, timeout=args.timeout)

    try:
        health = client.health()
    except Exception as exc:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "execute": bool(args.execute),
            "status": "blocked_gateway_unreachable",
            "gateway_url": args.gateway_url,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not health.get("ok"):
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "execute": bool(args.execute),
            "status": "blocked_gateway_not_ok",
            "gateway_url": args.gateway_url,
            "health": health,
        }

    sectors_raw = client.get_sector_list() or []
    sectors = [normalize_sector(row, idx) for idx, row in enumerate(sectors_raw, start=1)]
    if args.limit:
        sectors = sectors[: args.limit]

    stock_alias = load_stock_alias_map()
    sw_l2_name_map = load_sw_l2_name_map()
    snapshot_at = datetime.now()
    snapshot_date = snapshot_at.date()

    sector_rows: list[list[Any]] = []
    member_rows: list[list[Any]] = []
    failures: list[dict[str, Any]] = []
    unmapped_members = 0

    for sector in sectors:
        sector_code = str(sector["sector_code"])
        sector_name = str(sector["sector_name"])
        sector_type = str(sector.get("sector_type") or "tdx_sector")
        level = int(sector.get("level") or 0)
        canonical_code, canonical_name = sw_l2_name_map.get(sector_name, ("", ""))
        match_method = "same_name_sw_l2" if canonical_code else "unmatched"
        confidence = 0.95 if canonical_code else 0.0
        sector_rows.append(
            [
                SOURCE,
                sector_code,
                sector_name,
                sector_type,
                level,
                canonical_code,
                canonical_name,
                match_method,
                confidence,
                "active" if canonical_code else "unmatched",
                snapshot_date,
                snapshot_at,
                f"{SOURCE}:{sector_code}:{canonical_code or 'unmatched'}:{snapshot_date}",
            ]
        )
        try:
            stocks = client.get_stock_list_in_sector(sector_code) or []
        except Exception as exc:
            failures.append({"sector_code": sector_code, "sector_name": sector_name, "error": str(exc)})
            continue
        for item in stocks:
            source_stock = normalize_stock_code(item)
            if not source_stock:
                continue
            canonical_stock = stock_alias.get(source_stock, "")
            if not canonical_stock and "." in source_stock:
                canonical_stock = source_stock
            if not canonical_stock:
                unmapped_members += 1
            member_rows.append(
                [
                    SOURCE,
                    sector_code,
                    sector_name,
                    sector_type,
                    source_stock,
                    canonical_stock,
                    "active" if canonical_stock else "unmatched",
                    "tdx_stock_alias_or_canonical_code" if canonical_stock else "unmatched",
                    0.95 if canonical_stock else 0.0,
                    snapshot_date,
                    snapshot_at,
                    f"{SOURCE}:{sector_code}:{source_stock}:{canonical_stock or 'unmatched'}:{snapshot_date}",
                ]
            )

    if args.execute:
        ensure_tables()
        ch = clickhouse_client()
        if sector_rows:
            ch.insert(
                SECTOR_TABLE,
                sector_rows,
                column_names=[
                    "source",
                    "sector_code",
                    "sector_name",
                    "sector_type",
                    "level",
                    "canonical_sector_code",
                    "canonical_sector_name",
                    "match_method",
                    "confidence",
                    "alias_status",
                    "snapshot_date",
                    "snapshot_at",
                    "id",
                ],
            )
        if member_rows:
            ch.insert(
                MEMBER_TABLE,
                member_rows,
                column_names=[
                    "source",
                    "sector_code",
                    "sector_name",
                    "sector_type",
                    "source_stock_code",
                    "canonical_code",
                    "alias_status",
                    "match_method",
                    "confidence",
                    "snapshot_date",
                    "snapshot_at",
                    "id",
                ],
            )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "execute": bool(args.execute),
        "status": "completed",
        "gateway_url": args.gateway_url,
        "health": health,
        "sectors_fetched": len(sectors),
        "sector_rows_prepared": len(sector_rows),
        "sector_members_prepared": len(member_rows),
        "unmatched_sector_rows": sum(1 for row in sector_rows if row[9] != "active"),
        "unmapped_member_rows": unmapped_members,
        "sector_member_fetch_failures": failures[:50],
        "output_tables": [SECTOR_TABLE, MEMBER_TABLE] if args.execute else [],
        "note": "TDX snapshot is source-side only; Shenwan/canonical industry tables are not modified.",
    }


def write_report(result: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        "# TDX sector snapshot v1",
        "",
        f"- generated_at: `{result.get('generated_at')}`",
        f"- status: `{result.get('status')}`",
        f"- execute: `{result.get('execute')}`",
        f"- gateway_url: `{result.get('gateway_url')}`",
        "",
    ]
    if result.get("status") != "completed":
        lines.extend(["## Blocker", "", str(result.get("error") or result.get("health") or "unknown"), ""])
    else:
        lines.extend(
            [
                "## Summary",
                "",
                f"- TDX sectors: `{result.get('sectors_fetched')}`",
                f"- source_sectors rows: `{result.get('sector_rows_prepared')}`",
                f"- source_sector_stocks rows: `{result.get('sector_members_prepared')}`",
                f"- unmatched TDX sectors: `{result.get('unmatched_sector_rows')}`",
                f"- unmapped stock members: `{result.get('unmapped_member_rows')}`",
                "",
                "## Boundary",
                "",
                str(result.get("note")),
                "",
            ]
        )
    (REPORT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build TDX source-side sector snapshot tables.")
    parser.add_argument("--gateway-url", default=os.environ.get("AISTOCK_TDX_GATEWAY_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = fetch_snapshot(args)
    write_report(result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"report_dir={REPORT_DIR}")
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
