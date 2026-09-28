"""Collect broad intraday quote snapshots for temporary minute-bar assembly.

This path intentionally does not call TDX refresh_cache/refresh_kline. It pulls
one broad realtime quote snapshot, stores cumulative volume/amount, and lets the
5m builder derive temporary bars by differencing adjacent snapshots.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.logger import get_logger
from utils.market_warehouse import clickhouse_client

logger = get_logger("intraday_snapshot")


SNAPSHOT_TABLE = "intraday_quote_snapshot"
INDEX_SOURCE_ALIAS = {
    "999999.SH": "000001.SH",
}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _normalize_code(raw_code: Any) -> Optional[str]:
    text = str(raw_code or "").strip().upper()
    if not text:
        return None
    if "." in text:
        code, market = text.split(".", 1)
        code = "".join(ch for ch in code if ch.isdigit())
        market = market[:2]
        return f"{code}.{market}" if len(code) == 6 and market in {"SH", "SZ", "BJ"} else None

    prefix = text[:2].lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return None
    if prefix == "sh" or digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if prefix == "sz" or digits.startswith(("0", "3")):
        return f"{digits}.SZ"
    if prefix == "bj" or digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return None


def _to_sina_code(code: str) -> Optional[str]:
    normalized = _normalize_code(code)
    if not normalized:
        return None
    raw, market = normalized.split(".", 1)
    if market == "SH":
        return f"sh{raw}"
    if market == "SZ":
        return f"sz{raw}"
    if market == "BJ":
        return f"bj{raw}"
    return None


def ensure_snapshot_table() -> None:
    client = clickhouse_client()
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE}
        (
            snapshot_time DateTime,
            snapshot_date Date,
            code String,
            name String,
            asset_type String,
            source String,
            price Float64,
            open Float64,
            high Float64,
            low Float64,
            pre_close Float64,
            volume Float64,
            amount Float64,
            created_at DateTime
        )
        ENGINE = ReplacingMergeTree(created_at)
        PARTITION BY snapshot_date
        ORDER BY (snapshot_date, asset_type, code, snapshot_time)
        """
    )


def _stock_rows_from_akshare_sina(snapshot_time: datetime) -> List[Dict[str, Any]]:
    import akshare as ak

    df = ak.stock_zh_a_spot()
    rows: List[Dict[str, Any]] = []
    for item in df.to_dict("records"):
        code = _normalize_code(item.get("代码"))
        if not code:
            continue
        price = _to_float(item.get("最新价"))
        if price <= 0:
            continue
        rows.append(
            {
                "snapshot_time": snapshot_time,
                "snapshot_date": snapshot_time.date(),
                "code": code,
                "name": str(item.get("名称") or ""),
                "asset_type": "stock",
                "source": "akshare_sina_spot",
                "price": price,
                "open": _to_float(item.get("今开"), price),
                "high": _to_float(item.get("最高"), price),
                "low": _to_float(item.get("最低"), price),
                "pre_close": _to_float(item.get("昨收")),
                "volume": _to_float(item.get("成交量")),
                "amount": _to_float(item.get("成交额")),
                "created_at": datetime.now(timezone.utc),
            }
        )
    return rows


def _stock_rows_from_sina_batch(
    snapshot_time: datetime,
    target_codes: Iterable[str],
    batch_size: int = 80,
) -> List[Dict[str, Any]]:
    import requests

    code_pairs = []
    for code in target_codes:
        normalized = _normalize_code(code)
        sina_code = _to_sina_code(str(code))
        if normalized and sina_code:
            code_pairs.append((normalized, sina_code))
    if not code_pairs:
        return []

    rows: List[Dict[str, Any]] = []
    headers = {
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": "Mozilla/5.0",
    }
    pattern = re.compile(r"var hq_str_([a-z]{2}\d{6})=\"(.*?)\";")
    sina_to_code = {sina_code: normalized for normalized, sina_code in code_pairs}

    for start in range(0, len(code_pairs), batch_size):
        batch = code_pairs[start:start + batch_size]
        url = "https://hq.sinajs.cn/list=" + ",".join(sina_code for _, sina_code in batch)
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "gbk"
        for match in pattern.finditer(response.text):
            sina_code = match.group(1)
            normalized = sina_to_code.get(sina_code)
            if not normalized:
                continue
            parts = match.group(2).split(",")
            if len(parts) < 10:
                continue
            price = _to_float(parts[3])
            if price <= 0:
                continue
            rows.append(
                {
                    "snapshot_time": snapshot_time,
                    "snapshot_date": snapshot_time.date(),
                    "code": normalized,
                    "name": parts[0],
                    "asset_type": "stock",
                    "source": "sina_batch_quote",
                    "price": price,
                    "open": _to_float(parts[1], price),
                    "high": _to_float(parts[4], price),
                    "low": _to_float(parts[5], price),
                    "pre_close": _to_float(parts[2]),
                    "volume": _to_float(parts[8]),
                    "amount": _to_float(parts[9]),
                    "created_at": datetime.now(timezone.utc),
                }
            )
    return rows


def _index_rows_from_akshare_sina(
    snapshot_time: datetime,
    target_codes: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    import akshare as ak

    wanted = {str(code).strip().upper() for code in (target_codes or []) if str(code).strip()}
    source_to_target = {source: target for target, source in INDEX_SOURCE_ALIAS.items()}

    df = ak.stock_zh_index_spot_sina()
    rows: List[Dict[str, Any]] = []
    for item in df.to_dict("records"):
        source_code = _normalize_code(item.get("代码"))
        if not source_code:
            continue
        code = source_to_target.get(source_code, source_code)
        if wanted and code not in wanted and source_code not in wanted:
            continue
        price = _to_float(item.get("最新价"))
        if price <= 0:
            continue
        rows.append(
            {
                "snapshot_time": snapshot_time,
                "snapshot_date": snapshot_time.date(),
                "code": code,
                "name": str(item.get("名称") or ""),
                "asset_type": "index",
                "source": "akshare_sina_index_spot",
                "price": price,
                "open": _to_float(item.get("今开"), price),
                "high": _to_float(item.get("最高"), price),
                "low": _to_float(item.get("最低"), price),
                "pre_close": _to_float(item.get("昨收")),
                "volume": _to_float(item.get("成交量")),
                "amount": _to_float(item.get("成交额")),
                "created_at": datetime.now(timezone.utc),
            }
        )
    return rows


def _insert_snapshot_rows(rows: List[Dict[str, Any]]) -> None:
    ensure_snapshot_table()
    if not rows:
        return
    clickhouse_client().insert(
        SNAPSHOT_TABLE,
        [
            (
                row["snapshot_time"],
                row["snapshot_date"],
                row["code"],
                row["name"],
                row["asset_type"],
                row["source"],
                row["price"],
                row["open"],
                row["high"],
                row["low"],
                row["pre_close"],
                row["volume"],
                row["amount"],
                row["created_at"],
            )
            for row in rows
        ],
        column_names=[
            "snapshot_time",
            "snapshot_date",
            "code",
            "name",
            "asset_type",
            "source",
            "price",
            "open",
            "high",
            "low",
            "pre_close",
            "volume",
            "amount",
            "created_at",
        ],
    )


def load_snapshot_minute_bars(
    codes: Iterable[str],
    start_date: str,
    end_date: str,
    period_minutes: int,
    asset_type: Optional[str] = None,
) -> "pd.DataFrame":
    import pandas as pd

    normalized_codes = [_normalize_code(code) for code in codes]
    normalized_codes = [code for code in normalized_codes if code]
    if not normalized_codes:
        return pd.DataFrame(columns=["code", "datetime", "open", "high", "low", "close", "volume", "amount"])

    start_text = str(start_date)[:10]
    end_text = str(end_date)[:10]
    period = int(period_minutes)
    if period <= 0:
        return pd.DataFrame(columns=["code", "datetime", "open", "high", "low", "close", "volume", "amount"])

    def _quote(value: str) -> str:
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

    code_sql = ", ".join(_quote(code) for code in sorted(set(normalized_codes)))
    asset_filter = f"AND asset_type = {_quote(asset_type)}" if asset_type else ""
    df = clickhouse_client().query_df(
        f"""
        SELECT code, snapshot_time, price, open, high, low, volume, amount
        FROM {SNAPSHOT_TABLE}
        WHERE code IN ({code_sql})
          AND snapshot_date >= toDate({_quote(start_text)})
          AND snapshot_date <= toDate({_quote(end_text)})
          {asset_filter}
        ORDER BY code, snapshot_time
        """
    )
    if df.empty:
        return pd.DataFrame(columns=["code", "datetime", "open", "high", "low", "close", "volume", "amount"])

    d = df.copy()
    d["snapshot_time"] = pd.to_datetime(d["snapshot_time"], errors="coerce")
    for col in ["price", "open", "high", "low", "volume", "amount"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "snapshot_time", "price"])
    d = d[d["price"] > 0].copy()
    if d.empty:
        return pd.DataFrame(columns=["code", "datetime", "open", "high", "low", "close", "volume", "amount"])

    def _bucket_end(ts: pd.Timestamp) -> pd.Timestamp:
        anchor = ts.normalize() + pd.Timedelta(hours=9, minutes=30)
        if ts <= anchor:
            return anchor
        minutes = (ts - anchor).total_seconds() / 60.0
        slots = int((minutes + period - 1e-9) // period)
        return anchor + pd.Timedelta(minutes=slots * period)

    d["datetime"] = d["snapshot_time"].map(_bucket_end)
    rows: List[Dict[str, Any]] = []
    for (code, bucket), group in d.sort_values("snapshot_time").groupby(["code", "datetime"], sort=True):
        first = group.iloc[0]
        last = group.iloc[-1]
        price_series = pd.to_numeric(group["price"], errors="coerce").dropna()
        vol_series = pd.to_numeric(group["volume"], errors="coerce").dropna()
        amt_series = pd.to_numeric(group["amount"], errors="coerce").dropna()
        rows.append(
            {
                "code": code,
                "datetime": bucket,
                "open": float(first.get("price")),
                "high": float(price_series.max()),
                "low": float(price_series.min()),
                "close": float(last.get("price")),
                "volume": float(max(0.0, vol_series.max() - vol_series.min())) if not vol_series.empty else 0.0,
                "amount": float(max(0.0, amt_series.max() - amt_series.min())) if not amt_series.empty else 0.0,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=["code", "datetime", "open", "high", "low", "close", "volume", "amount"])
    return out.sort_values(["code", "datetime"]).reset_index(drop=True)


def collect_global_stock_snapshot(target_codes: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    snapshot_time = datetime.now().replace(tzinfo=timezone.utc)
    errors: List[str] = []
    rows: List[Dict[str, Any]] = []
    source = "akshare_sina_spot"
    target_code_list = list(target_codes or [])
    if target_code_list:
        try:
            rows = _stock_rows_from_sina_batch(snapshot_time, target_codes=target_code_list)
            source = "sina_batch_quote"
        except Exception as exc:
            errors.append(f"sina_batch_quote:{exc}")
            logger.warning(f"sina target batch quote failed, try akshare global stock spot: {exc}")

    if not rows:
        try:
            rows = _stock_rows_from_akshare_sina(snapshot_time)
            source = "akshare_sina_spot"
        except Exception as exc:
            errors.append(f"akshare_sina_spot:{exc}")
            logger.warning(f"akshare global stock spot failed: {exc}")

    if not rows and target_code_list:
        rows = _stock_rows_from_sina_batch(snapshot_time, target_codes=target_code_list)
        source = "sina_batch_quote"
    _insert_snapshot_rows(rows)
    elapsed = time.perf_counter() - started
    result = {
        "source": source,
        "snapshot_time": snapshot_time.strftime("%Y-%m-%d %H:%M:%S"),
        "rows": len(rows),
        "elapsed_sec": round(elapsed, 3),
        "target_count": len(target_code_list),
        "errors": errors,
    }
    logger.info(f"intraday global stock snapshot collected: {result}")
    return result


def collect_global_index_snapshot(target_codes: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    snapshot_time = datetime.now().replace(tzinfo=timezone.utc)
    rows = _index_rows_from_akshare_sina(snapshot_time, target_codes=target_codes)
    _insert_snapshot_rows(rows)
    elapsed = time.perf_counter() - started
    result = {
        "source": "akshare_sina_index_spot",
        "snapshot_time": snapshot_time.strftime("%Y-%m-%d %H:%M:%S"),
        "rows": len(rows),
        "elapsed_sec": round(elapsed, 3),
        "target_codes": list(target_codes or []),
    }
    logger.info(f"intraday global index snapshot collected: {result}")
    return result


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect one global intraday quote snapshot")
    parser.add_argument("--stocks", action="store_true", default=True)
    parser.add_argument("--indices", action="store_true")
    parser.add_argument("--index-codes", default="", help="Comma-separated normalized index codes")
    parser.add_argument("--stock-codes", default="", help="Comma-separated normalized stock codes for fallback batch quote")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.indices:
        target_codes = [item.strip() for item in args.index_codes.split(",") if item.strip()]
        print(collect_global_index_snapshot(target_codes=target_codes or None))
    elif args.stocks:
        target_codes = [item.strip() for item in args.stock_codes.split(",") if item.strip()]
        print(collect_global_stock_snapshot(target_codes=target_codes or None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
