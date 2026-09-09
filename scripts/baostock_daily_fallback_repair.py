"""Controlled Baostock fallback for QMT daily-source absences.

This is intentionally narrower than a normal history importer: it only writes
code/date keys for which QMT returned no daily bar, and only after Baostock's
unadjusted prices have been cross-checked against the QMT bars already present
for the same stock.  Every attempted key is retained in an audit table.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qmtmini_daily_backfill_validate import (  # noqa: E402
    MARKET_STATUS_TABLE,
    SOURCE_ABSENCE_TABLE,
    TARGET_COLUMNS,
    ch_client,
    coverage_summary,
    quote_sql,
)
from utils.kline_units import (
    normalize_akshare_daily_units,
    normalize_baostock_daily_units,
)

AUDIT_TABLE = "kline_daily_fallback_resolution_audit"
SOURCE = "baostock_unadjusted"
PRICE_TOLERANCE = 0.002
VOLUME_TOLERANCE = 0.002


def is_suspension_placeholder(row: Any) -> bool:
    """Detect provider placeholder rows for announced no-trade days."""
    prices = [getattr(row, field, None) for field in ["open", "high", "low", "close"]]
    numeric_prices = [pd.to_numeric(value, errors="coerce") for value in prices]
    if any(pd.isna(value) for value in numeric_prices):
        return False
    if len({round(float(value), 6) for value in numeric_prices}) != 1:
        return False
    volume = pd.to_numeric(getattr(row, "volume", None), errors="coerce")
    amount = pd.to_numeric(getattr(row, "amount", None), errors="coerce")
    return (pd.isna(volume) or float(volume) == 0.0) and (pd.isna(amount) or float(amount) == 0.0)


def tradable_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    mask = [not is_suspension_placeholder(row) for row in frame.itertuples(index=False)]
    return frame.loc[mask].copy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cross-validated Baostock repair for QMT daily absences.")
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="2026-07-27")
    parser.add_argument("--codes", default="", help="Optional comma-separated canonical codes.")
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--candidate-mode", choices=["qmt_absence", "coverage_missing"], default="qmt_absence")
    parser.add_argument("--apply", action="store_true", help="Write only cross-validated missing keys to kline_daily.")
    parser.add_argument("--report", required=True)
    return parser.parse_args()


def ensure_audit_table(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}
        (
            code String, trade_date Date, provider String, status String,
            overlap_rows UInt32, price_match_ratio Float64, volume_match_ratio Float64,
            detail String, observed_at DateTime
        ) ENGINE = ReplacingMergeTree(observed_at)
        ORDER BY (code, trade_date, provider)
        """
    )


def candidates(client: Any, args: argparse.Namespace) -> dict[str, set[date]]:
    code_filter = ""
    if args.codes.strip():
        values = [quote_sql(x.strip().upper()) for x in args.codes.split(",") if x.strip()]
        code_filter = f" AND a.code IN ({','.join(values)})" if values else " AND 0"
    if args.candidate_mode == "qmt_absence":
        sql = f"""
            SELECT DISTINCT a.code, a.trade_date
            FROM {SOURCE_ABSENCE_TABLE} a
            INNER JOIN stocks s ON s.code = a.code
            INNER JOIN trade_calendar c ON c.market='SH' AND c.is_trading=1 AND c.trade_date=a.trade_date
            LEFT JOIN (SELECT code, trade_date FROM kline_daily FINAL) k ON k.code=a.code AND k.trade_date=a.trade_date
            WHERE a.source='qmt_xtquant'
              AND a.trade_date BETWEEN toDate({quote_sql(args.start_date)}) AND toDate({quote_sql(args.end_date)})
              AND (s.quit=0 OR s.quit IS NULL)
              AND (s.list_date IS NULL OR a.trade_date >= s.list_date)
              AND (s.delist_date IS NULL OR a.trade_date <= s.delist_date)
              AND (k.code='' OR k.code IS NULL)
              {code_filter}
            ORDER BY a.code, a.trade_date
        """
        rows = client.query(sql).result_rows
    else:
        # A global ClickHouse cross join needs >5 GiB on this host. Reuse the
        # proven coverage planner to identify affected codes, then perform a
        # small left join per code. This remains exact without a broad join.
        planner_args = argparse.Namespace(start_date=args.start_date, end_date=args.end_date, codes="", code_offset=0, limit=0, include_index=False)
        summary = coverage_summary(client, planner_args, "kline_daily", candidate_limit=10000)
        selected = [str(item["code"]).upper() for item in summary["missing_by_code_sample"]]
        if args.codes.strip():
            wanted = {item.strip().upper() for item in args.codes.split(",") if item.strip()}
            selected = [code for code in selected if code in wanted]
        rows = []
        for code in selected:
            meta = client.query(f"SELECT list_date,delist_date FROM stocks WHERE code={quote_sql(code)}").first_row
            list_clause = f"AND c.trade_date >= toDate({quote_sql(meta[0])})" if meta and meta[0] else ""
            delist_clause = f"AND c.trade_date <= toDate({quote_sql(meta[1])})" if meta and meta[1] else ""
            one = client.query(
                f"""
                SELECT c.trade_date
                FROM trade_calendar c
                LEFT JOIN (SELECT trade_date FROM kline_daily FINAL WHERE code={quote_sql(code)}) k
                    ON k.trade_date=c.trade_date
                WHERE c.market='SH' AND c.is_trading=1
                  AND c.trade_date BETWEEN toDate({quote_sql(args.start_date)}) AND toDate({quote_sql(args.end_date)})
                  {list_clause} {delist_clause}
                  AND k.trade_date=toDate('1970-01-01')
                ORDER BY c.trade_date
                """
            ).result_rows
            rows.extend((code, day) for (day,) in one)
    status_rows = client.query(
        f"SELECT code,start_date,end_date FROM {MARKET_STATUS_TABLE} FINAL "
        f"WHERE start_date<=toDate({quote_sql(args.end_date)}) AND end_date>=toDate({quote_sql(args.start_date)})"
    ).result_rows
    result: dict[str, set[date]] = defaultdict(set)
    for code, trade_date in rows:
        code = str(code).upper()
        if any(code == str(status_code).upper() and start <= trade_date <= end for status_code, start, end in status_rows):
            continue
        result[code].add(trade_date)
    if args.limit_codes:
        return {code: result[code] for code in list(result)[: args.limit_codes]}
    return dict(result)


def baostock_code(code: str) -> str | None:
    raw, suffix = code.split(".", 1)
    if suffix == "SH":
        return f"sh.{raw}"
    if suffix == "SZ":
        return f"sz.{raw}"
    return None


def fetch_bars(bs: Any, code: str, start_date: str, end_date: str) -> pd.DataFrame:
    if code.endswith(".BJ"):
        import akshare as ak
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                raw = ak.stock_zh_a_hist(
                    symbol=code.split(".", 1)[0], period="daily",
                    start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust="",
                )
                if raw is None or raw.empty:
                    return pd.DataFrame()
                # Eastmoney/AKShare's daily history response has stable field order.
                # Index-based normalization avoids locale/encoding dependence.
                frame = pd.DataFrame({
                    "trade_date": pd.to_datetime(raw.iloc[:, 0], errors="coerce").dt.date,
                    "open": pd.to_numeric(raw.iloc[:, 2], errors="coerce"),
                    "close": pd.to_numeric(raw.iloc[:, 3], errors="coerce"),
                    "high": pd.to_numeric(raw.iloc[:, 4], errors="coerce"),
                    "low": pd.to_numeric(raw.iloc[:, 5], errors="coerce"),
                    "volume": pd.to_numeric(raw.iloc[:, 6], errors="coerce"),
                    "amount": pd.to_numeric(raw.iloc[:, 7], errors="coerce"),
                })
                frame = normalize_akshare_daily_units(frame)
                return frame.dropna(subset=["trade_date", "open", "high", "low", "close"]).sort_values("trade_date")
            except Exception as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"akshare BJ request failed: {type(last_error).__name__}: {last_error}")
    symbol = baostock_code(code)
    if not symbol:
        return pd.DataFrame()
    rs = bs.query_history_k_data_plus(
        symbol, "date,open,high,low,close,volume,amount", start_date=start_date, end_date=end_date,
        frequency="d", adjustflag="3",
    )
    if getattr(rs, "error_code", "0") != "0":
        raise RuntimeError(f"{rs.error_code} {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=rs.fields)
    df["trade_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for field in ["open", "high", "low", "close", "volume", "amount"]:
        df[field] = pd.to_numeric(df[field], errors="coerce")
    df = df.dropna(subset=["trade_date", "open", "high", "low", "close"]).sort_values("trade_date")
    return normalize_baostock_daily_units(df)


def qmt_rows(client: Any, code: str, start_date: str, end_date: str) -> dict[date, tuple[float, ...]]:
    rows = client.query(
        f"""SELECT trade_date,open,high,low,close,volume,amount FROM kline_daily FINAL
        WHERE code={quote_sql(code)} AND trade_date BETWEEN toDate({quote_sql(start_date)}) AND toDate({quote_sql(end_date)})"""
    ).result_rows
    return {row[0]: tuple(float(x) for x in row[1:]) for row in rows}


def validation_metrics(frame: pd.DataFrame, existing: dict[date, tuple[float, ...]]) -> tuple[int, float, float, float, float]:
    raw: list[tuple[float, float, float]] = []
    amount_matches: list[bool] = []
    for row in tradable_frame(frame).itertuples(index=False):
        prior = existing.get(row.trade_date)
        if prior is None:
            continue
        prices = [row.open, row.high, row.low, row.close]
        max_price_error = max(abs(prior[i] - float(value)) / max(abs(float(value)), 1e-9) for i, value in enumerate(prices))
        if float(row.volume) > 0:
            raw.append((max_price_error, float(prior[4]) / float(row.volume), float(row.volume)))
        amount_matches.append(abs(float(prior[5]) - float(row.amount)) / max(abs(float(row.amount)), 1e-9) <= PRICE_TOLERANCE)
    if not raw:
        return 0, 0.0, 0.0, 0.0, 0.0
    # Historical AiStock imports used two volume scales.  Infer the prevailing
    # per-code scale from overlapping QMT rows; never assume a provider unit.
    scale = float(pd.Series([ratio for _, ratio, _ in raw]).median())
    volume_ratio = sum(abs((ratio / scale) - 1.0) <= VOLUME_TOLERANCE for _, ratio, _ in raw) / len(raw)
    return len(raw), sum(error <= PRICE_TOLERANCE for error, _, _ in raw) / len(raw), volume_ratio, scale, sum(amount_matches) / len(amount_matches)


def rows_for_missing(
    code: str,
    frame: pd.DataFrame,
    missing: set[date],
    observed_at: datetime,
    volume_scale: float | None = None,
) -> list[tuple]:
    # Kept as a compatibility-only argument for older callers. The inferred
    # database scale is diagnostic and must never alter the storage contract.
    _ = volume_scale
    rows: list[tuple] = []
    prev_close: float | None = None
    for row in tradable_frame(frame).itertuples(index=False):
        close = float(row.close)
        if row.trade_date in missing:
            base = prev_close if prev_close and prev_close > 0 else close
            change = close - base
            rows.append((
                code, row.trade_date, float(row.open), float(row.high), float(row.low), close,
                float(row.volume), float(row.amount),
                (float(row.high) - float(row.low)) / base * 100.0,
                change / base * 100.0, change, 0.0, observed_at,
            ))
        prev_close = close
    return rows


def apply_rows(client: Any, rows: list[tuple]) -> int:
    if not rows:
        return 0
    for offset in range(0, len(rows), 500):
        chunk = rows[offset:offset + 500]
        keys = ",".join(f"({quote_sql(row[0])},toDate({quote_sql(row[1])}))" for row in chunk)
        client.command(f"ALTER TABLE kline_daily DELETE WHERE (code,trade_date) IN ({keys}) SETTINGS mutations_sync=1")
        client.insert("kline_daily", chunk, column_names=TARGET_COLUMNS)
    return len(rows)


def main() -> int:
    args = parse_args()
    client = ch_client(); ensure_audit_table(client)
    targets = candidates(client, args)
    fetch_start = (pd.Timestamp(args.start_date) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    import baostock as bs
    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
    observed = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    audit: list[tuple] = []; accepted: list[tuple] = []
    summary = {"candidate_codes": len(targets), "candidate_code_dates": sum(map(len, targets.values())), "applied_rows": 0, "resolved_rows": 0, "provider_no_bar_rows": 0, "validation_rejected_rows": 0, "provider_error_codes": 0, "codes": []}
    try:
        for code, missing in targets.items():
            try:
                frame = fetch_bars(bs, code, fetch_start, args.end_date)
                existing = qmt_rows(client, code, fetch_start, args.end_date)
                validation_frame = frame[frame["trade_date"] >= pd.Timestamp(args.start_date).date()]
                overlap, price_ratio, volume_ratio, volume_scale, amount_ratio = validation_metrics(validation_frame, existing)
                # Amount and OHLC are provider-independent facts. Historical
                # QMT volume-unit shifts are audited but must not reject an
                # otherwise exact, independently verified raw bar.
                trusted = overlap >= 20 and price_ratio >= 0.99 and amount_ratio >= 0.98
                tradable = tradable_frame(frame)
                bar_dates = set(tradable["trade_date"].tolist()) if not tradable.empty else set()
                placeholder_dates = {
                    row.trade_date
                    for row in frame.itertuples(index=False)
                    if is_suspension_placeholder(row)
                } if not frame.empty else set()
                usable = missing & bar_dates if trusted else set()
                # ``frame`` is already normalized to the storage contract.
                # The overlap-derived scale remains diagnostic only; it must
                # never be used to write historical rows because it reflects
                # the state of the existing database, not the target contract.
                accepted.extend(rows_for_missing(code, frame, usable, observed))
                status = "resolved_by_cross_validated_baostock" if trusted else "provider_data_validation_rejected"
                for trade_date in missing:
                    if trusted and trade_date in bar_dates:
                        outcome = status; summary["resolved_rows"] += 1
                    elif trade_date in placeholder_dates:
                        outcome = "provider_suspension_placeholder"; summary["provider_no_bar_rows"] += 1
                    elif trusted:
                        outcome = "provider_no_daily_bar"; summary["provider_no_bar_rows"] += 1
                    else:
                        outcome = status; summary["validation_rejected_rows"] += 1
                    audit.append((code, trade_date, SOURCE, outcome, overlap, price_ratio, volume_ratio, f"unadjusted_daily_cross_check;amount_match_ratio={amount_ratio:.6g};volume_scale={volume_scale:.10g}", observed))
                summary["codes"].append({"code": code, "missing": len(missing), "overlap": overlap, "price_match_ratio": price_ratio, "amount_match_ratio": amount_ratio, "volume_match_ratio": volume_ratio, "volume_scale": volume_scale, "resolved": len(usable), "suspension_placeholder_rows": len(missing & placeholder_dates)})
            except Exception as exc:
                summary["provider_error_codes"] += 1
                for trade_date in missing:
                    audit.append((code, trade_date, SOURCE, "provider_error", 0, 0.0, 0.0, f"{type(exc).__name__}: {exc}"[:900], observed))
    finally:
        bs.logout()
    if audit:
        client.insert(AUDIT_TABLE, audit, column_names=["code","trade_date","provider","status","overlap_rows","price_match_ratio","volume_match_ratio","detail","observed_at"])
    if args.apply:
        summary["applied_rows"] = apply_rows(client, accepted)
    summary["finished_at"] = observed.isoformat(timespec="seconds")
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
