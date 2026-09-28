from __future__ import annotations

"""Audit purchased 5m/daily reconciliation rows against independent daily feeds.

The default mode is read-only.  ``--write-evidence`` only appends an audit
table and a manifest; it never changes the purchased snapshot, 5m stage, or
formal K-line tables.  A daily snapshot is considered externally confirmed
when the user-approved Sina daily source agrees with it.  Eastmoney (through
AkShare) is retained as optional corroboration only.  A 5m
candidate is deliberately not promoted merely because the daily snapshot is
confirmed: the two sources answer different questions.
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df
from utils.paths import runtime_path
from utils.purchased_5m_reconciliation import PRICE_RELATIVE_TOLERANCE, RELATIVE_TOLERANCE

TABLE = "stock.purchased_history_external_daily_recon"
RECON = "stock.purchased_history_minute_5_daily_snapshot_recon"
TZ = ZoneInfo("Asia/Shanghai")


def _float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        result = float(value)
        return result if pd.notna(result) else None
    except (TypeError, ValueError):
        return None


def _code_without_suffix(code: str) -> str:
    return str(code).split(".", 1)[0].zfill(6)


def _symbol_for_sina(code: str) -> str:
    value = str(code).upper()
    return ("sh" if value.endswith(".SH") else "sz") + _code_without_suffix(value)


def _canonical_frame(raw: pd.DataFrame | None, provider: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    aliases = {
        "日期": "date", "交易日期": "date", "trade_date": "date", "date": "date",
        "开盘": "open", "open": "open", "开盘价": "open",
        "最高": "high", "high": "high", "最高价": "high",
        "最低": "low", "low": "low", "最低价": "low",
        "收盘": "close", "close": "close", "收盘价": "close",
        "成交量": "volume", "volume": "volume", "vol": "volume",
        "成交额": "amount", "amount": "amount",
    }
    frame = raw.rename(columns={str(k): v for k, v in aliases.items()}).copy()
    # Eastmoney/AkShare has changed Chinese labels and encoding several times;
    # positional fallback is intentionally restricted to the known 7 columns.
    if not {"date", "open", "high", "low", "close"}.issubset(frame.columns) and len(frame.columns) >= 7:
        frame = frame.iloc[:, :7].copy()
        frame.columns = ["date", "open", "close", "high", "low", "volume", "amount"]
    for col in ["date", "open", "high", "low", "close", "volume", "amount"]:
        if col not in frame:
            frame[col] = None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
    # Sina returns shares; Eastmoney historical A-share volume is already lots.
    if provider == "sina":
        frame["volume"] = frame["volume"] / 100.0
    return frame[["date", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)


def _fetch_provider(code: str, year: int, provider: str) -> pd.DataFrame:
    import akshare as ak

    symbol = _code_without_suffix(code)
    start, end = f"{year}0101", f"{year}1231"
    if provider == "sina":
        raw = ak.stock_zh_a_daily(symbol=_symbol_for_sina(code), start_date=start, end_date=end, adjust="")
    else:
        raw = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust="")
    return _canonical_frame(raw, provider)


def _latest_review(client: Any, years: list[int], limit: int) -> pd.DataFrame:
    year_sql = "" if not years else "AND toYear(trade_date) IN (" + ",".join(map(str, years)) + ")"
    limit_sql = f"LIMIT {int(limit)}" if limit > 0 else ""
    return clickhouse_query_df(
        f"""
        SELECT code, trade_date,
               argMax(minute_open, checked_at) AS minute_open,
               argMax(minute_high, checked_at) AS minute_high,
               argMax(minute_low, checked_at) AS minute_low,
               argMax(minute_close, checked_at) AS minute_close,
               argMax(minute_volume_lots, checked_at) AS minute_volume_lots,
               argMax(minute_amount_yuan, checked_at) AS minute_amount_yuan,
               argMax(daily_open, checked_at) AS daily_open,
               argMax(daily_high, checked_at) AS daily_high,
               argMax(daily_low, checked_at) AS daily_low,
               argMax(daily_close, checked_at) AS daily_close,
               argMax(daily_volume_lots, checked_at) AS daily_volume_lots,
               argMax(daily_amount_yuan, checked_at) AS daily_amount_yuan,
               argMax(overall_status, checked_at) AS overall_status
        FROM {RECON}
        WHERE 1=1 {year_sql}
        GROUP BY code, trade_date
        HAVING overall_status='review'
        ORDER BY trade_date, code
        {limit_sql}
        """
    )


def _relative(a: Any, b: Any) -> float | None:
    left, right = _float(a), _float(b)
    if left is None or right is None:
        return None
    return abs(left - right) / max(abs(right), 1.0)


def _matches_provider(row: dict[str, Any], provider: dict[str, Any], prefix: str) -> tuple[bool, list[str]]:
    fields = [("open", f"{prefix}_open"), ("high", f"{prefix}_high"), ("low", f"{prefix}_low"), ("close", f"{prefix}_close")]
    missing: list[str] = []
    for left, right in fields:
        if _float(row.get(right)) is None or _float(provider.get(left)) is None:
            missing.append(left)
        elif _relative(provider.get(left), row.get(right)) is None or _relative(provider.get(left), row.get(right)) > PRICE_RELATIVE_TOLERANCE:
            missing.append(left)
    for left, right in [("volume", f"{prefix}_volume_lots"), ("amount", f"{prefix}_amount_yuan")]:
        if _float(row.get(right)) is None or _float(provider.get(left)) is None:
            continue
        if _relative(provider.get(left), row.get(right)) > RELATIVE_TOLERANCE:
            missing.append(left)
    return not missing, missing


def _decision(row: dict[str, Any], providers: dict[str, dict[str, Any]]) -> tuple[str, str, list[str]]:
    snapshot = []
    minute = []
    for name, provider in providers.items():
        snap_ok, snap_bad = _matches_provider(row, provider, "daily")
        min_ok, min_bad = _matches_provider(row, provider, "minute")
        snapshot.append((name, snap_ok, snap_bad))
        minute.append((name, min_ok, min_bad))
    snap_good = [name for name, ok, _ in snapshot if ok]
    min_good = [name for name, ok, _ in minute if ok]
    # User-approved policy: Sina is the authoritative external corroboration
    # for this purchased source.  One matching Sina row is sufficient; the
    # optional Eastmoney result must not veto it or be required for promotion.
    if "sina" in snap_good:
        return "snapshot_confirmed", "Sina matches purchased daily snapshot within configured tolerances", snap_good
    if "sina" in min_good:
        return "minute_confirmed", "Sina matches 5m-derived daily aggregate within configured tolerances", min_good
    if snap_good and min_good:
        return "provider_conflict", "providers split between purchased snapshot and 5m aggregate", snap_good + min_good
    if snap_good:
        return "snapshot_single_provider", "only one provider matched the purchased daily snapshot", snap_good
    if min_good:
        return "minute_single_provider", "only one provider matched the 5m-derived daily aggregate", min_good
    return "unresolved", "no independent provider matched within configured tolerances", []


def _ensure_table(client: Any) -> None:
    client.command(f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        run_id String, code String, trade_date Date, provider_count UInt8,
        providers String, provider_details String, decision LowCardinality(String),
        reason String, evidence_ref String, checked_at DateTime('Asia/Shanghai')
    ) ENGINE = MergeTree ORDER BY (trade_date, code, run_id)
    """)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", action="append", type=int, dest="years")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--run-id", default=datetime.now(TZ).strftime("external_daily_audit_%Y%m%d_%H%M%S"))
    parser.add_argument("--write-evidence", action="store_true")
    args = parser.parse_args()
    client = clickhouse_client()
    frame = _latest_review(client, sorted(set(args.years or [])), args.limit)
    if frame.empty:
        print(json.dumps({"run_id": args.run_id, "result_rows": 0}, ensure_ascii=False))
        return 0

    provider_cache: dict[tuple[str, int, str], pd.DataFrame] = {}
    results: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        code, day = str(raw["code"]), pd.Timestamp(raw["trade_date"]).date()
        row = {k: (v.item() if hasattr(v, "item") else v) for k, v in raw.items()}
        providers: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        for provider in ("sina", "eastmoney"):
            key = (code, day.year, provider)
            try:
                if key not in provider_cache:
                    provider_cache[key] = _fetch_provider(code, day.year, provider)
                hit = provider_cache[key]
                hit = hit[hit["date"] == day.isoformat()]
                if not hit.empty:
                    providers[provider] = hit.iloc[0].to_dict()
            except Exception as exc:
                errors[provider] = str(exc)[:300]
            time.sleep(0.2)
        decision, reason, matches = _decision(row, providers) if providers else ("provider_unavailable", "no provider returned a usable row", [])
        details = {"providers": providers, "errors": errors, "matches": matches, "source_row": row}
        results.append({
            "run_id": args.run_id, "code": code, "trade_date": day,
            "provider_count": len(providers), "providers": ",".join(sorted(providers)),
            "provider_details": json.dumps(details, ensure_ascii=False, default=str),
            "decision": decision, "reason": reason, "evidence_ref": "", "checked_at": datetime.now(TZ),
        })

    counts = Counter(item["decision"] for item in results)
    summary = {"run_id": args.run_id, "target_rows": len(results), "decision_counts": dict(counts), "formal_table_mutation": False, "write_evidence": args.write_evidence}
    report = runtime_path("purchased_history", "external_daily_audit", f"{args.run_id}.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if args.write_evidence:
        _ensure_table(client)
        for item in results:
            item["evidence_ref"] = str(report)
        client.insert(TABLE, [list(item.values()) for item in results], column_names=list(results[0].keys()))
    print(json.dumps({**summary, "report": str(report)}, ensure_ascii=False))
    for key, value in counts.most_common():
        print(f"decision={key} count={value}")
    for item in results[:20]:
        print(f"{item['code']} {item['trade_date']} decision={item['decision']} providers={item['providers']} reason={item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
