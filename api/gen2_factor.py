from __future__ import annotations

import json
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from api.alpha191_engine import (
    Alpha191FormulaError,
    Alpha191UnsupportedError,
    enrich_alpha191_fields,
    evaluate_alpha191_formula,
    normalize_formula,
    required_external_fields,
)
from utils.market_warehouse import clickhouse_query_df


REPO_ROOT = Path(__file__).resolve().parents[1]
ALPHA191_JS_PATH = REPO_ROOT / "frontend" / "src" / "data" / "gen2Alpha191.js"
BENCHMARK_CODE = "000300.SH"


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d")
    if pd.isna(value):
        return None
    return value


@lru_cache(maxsize=1)
def load_alpha191_registry() -> Dict[str, Any]:
    text = ALPHA191_JS_PATH.read_text(encoding="utf-8")
    source_match = re.search(r"export const alpha191Source = (\{[\s\S]*?\})\s*\n\nexport const alpha191Factors", text)
    factors_match = re.search(r"export const alpha191Factors = (\[[\s\S]*\])\s*$", text)
    if not factors_match:
        return {
            "source": {},
            "factors": [],
            "message": "Alpha191 registry source file is not parseable",
        }

    source: Dict[str, Any] = {}
    if source_match:
        for key in ["name", "title", "url", "origin", "extraction"]:
            m = re.search(rf"{key}:\s*'([^']*)'", source_match.group(1))
            if m:
                source[key] = m.group(1)

    factors = json.loads(factors_match.group(1))
    for row in factors:
        formula = str(row.get("formula") or "")
        missing = required_external_fields(formula)
        row["formula_normalized"] = normalize_formula(formula)
        row["backend_missing_fields"] = missing
        row["backend_status"] = "needs_data_source" if missing else "implemented"
        row["backend_status_text"] = "needs extra data source" if missing else "implemented"
    return {"source": source, "factors": factors, "message": ""}


def build_gen2_factor_registry() -> Dict[str, Any]:
    payload = load_alpha191_registry()
    factors = payload.get("factors") or []
    return {
        "available": bool(factors),
        "source": payload.get("source") or {},
        "factors": factors,
        "summary": {
            "total": len(factors),
            "backend_implemented": sum(1 for item in factors if item.get("backend_status") == "implemented"),
            "needs_data_source": sum(1 for item in factors if item.get("backend_status") == "needs_data_source"),
            "pending": 0,
        },
        "message": payload.get("message") or "",
    }


def _normalize_factor_id(factor_id: str) -> str:
    text = str(factor_id or "").strip().lower().replace("_", "")
    if text.startswith("alpha") and len(text) > 5:
        digits = re.sub(r"\D", "", text[5:])
        if digits:
            return f"alpha{int(digits):03d}"
    if text.isdigit():
        return f"alpha{int(text):03d}"
    return text


def _factor_meta(factor_id: str) -> Optional[Dict[str, Any]]:
    normalized = _normalize_factor_id(factor_id)
    for item in load_alpha191_registry().get("factors") or []:
        if _normalize_factor_id(str(item.get("id") or "")) == normalized:
            return item
    return None


def _latest_trade_date() -> Optional[str]:
    df = clickhouse_query_df("SELECT max(trade_date) AS max_date FROM kline_daily")
    if df.empty or pd.isna(df.iloc[0].get("max_date")):
        return None
    return pd.Timestamp(df.iloc[0]["max_date"]).strftime("%Y-%m-%d")


def _date_or_default(value: Optional[str], fallback: pd.Timestamp) -> pd.Timestamp:
    if value:
        try:
            return pd.Timestamp(value).normalize()
        except Exception:
            pass
    return fallback.normalize()


def _load_benchmark(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT
          trade_date AS date,
          open AS benchmark_open,
          close AS benchmark_close
        FROM kline_daily
        WHERE code = ?
          AND trade_date >= toDate(?)
          AND trade_date <= toDate(?)
        ORDER BY trade_date
        """,
        [BENCHMARK_CODE, start_date, end_date],
    )
    if df.empty:
        return pd.DataFrame(columns=["date", "benchmark_open", "benchmark_close"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["benchmark_open"] = pd.to_numeric(df["benchmark_open"], errors="coerce")
    df["benchmark_close"] = pd.to_numeric(df["benchmark_close"], errors="coerce")
    return df.dropna(subset=["date"]).copy()


def _load_daily_ohlcv(start_date: str, end_date: str, horizon: int, prewarm_days: int = 420) -> pd.DataFrame:
    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    query_start = (start_ts - pd.Timedelta(days=max(prewarm_days, 30))).strftime("%Y-%m-%d")
    query_end = (end_ts + pd.Timedelta(days=max(horizon * 3 + 10, 15))).strftime("%Y-%m-%d")
    df = clickhouse_query_df(
        """
        SELECT
          substr(k.code, 1, 6) AS code,
          any(s.name) AS name,
          k.trade_date AS date,
          k.open AS open,
          k.high AS high,
          k.low AS low,
          k.close AS close,
          k.volume AS volume,
          k.amount AS amount
        FROM kline_daily k
        JOIN stocks s ON s.code = k.code
        WHERE k.trade_date >= toDate(?)
          AND k.trade_date <= toDate(?)
          AND s.type = 'stock'
          AND s.quit = 0
          AND (s.st = 0 OR s.st IS NULL)
          AND k.open > 0
          AND k.close > 0
          AND k.volume > 0
        GROUP BY k.code, k.trade_date, k.open, k.high, k.low, k.close, k.volume, k.amount
        ORDER BY k.code, k.trade_date
        """,
        [query_start, query_end],
    )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["code"] = df["code"].astype(str).str[:6]
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "code", "open", "high", "low", "close", "volume"]).copy()
    benchmark = _load_benchmark(query_start, query_end)
    if not benchmark.empty:
        df = df.merge(benchmark, on="date", how="left")
    return df.sort_values(["code", "date"]).reset_index(drop=True)


def _estimate_prewarm_days(meta: Dict[str, Any]) -> int:
    formula = str(meta.get("formula_normalized") or meta.get("formula") or "")
    windows = [int(x) for x in re.findall(r",\s*(\d{1,3})(?=[,\)])", formula)]
    max_window = max(windows or [40])
    return int(max(80, min(520, max_window * 1.7 + 35)))


def _add_factor(df: pd.DataFrame, meta: Dict[str, Any]) -> pd.DataFrame:
    d = enrich_alpha191_fields(df)
    missing = meta.get("backend_missing_fields") or []
    if missing:
        raise Alpha191UnsupportedError(f"Missing external fields: {', '.join(missing)}")
    d["factor_value"] = evaluate_alpha191_formula(d, str(meta.get("formula") or ""))
    return d


def _add_forward_returns(df: pd.DataFrame, horizons: List[int]) -> pd.DataFrame:
    d = df.sort_values(["code", "date"]).copy()
    g = d.groupby("code", group_keys=False)
    for horizon in horizons:
        d[f"fwd_{horizon}d"] = g["close"].shift(-horizon) / d["close"] - 1.0
    return d


def _daily_ic(df: pd.DataFrame, target: str, min_symbols: int) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for date_value, day in df[["date", "factor_value", target]].dropna().groupby("date"):
        if len(day) < min_symbols or day["factor_value"].nunique() < 5:
            continue
        ic = day["factor_value"].rank(method="average").corr(day[target].rank(method="average"))
        if pd.notna(ic) and math.isfinite(float(ic)):
            rows.append({"date": pd.Timestamp(date_value).strftime("%Y-%m-%d"), "ic": float(ic), "n": int(len(day))})
    if not rows:
        return {"days": 0, "mean_ic": None, "median_ic": None, "ic_ir": None, "positive_ratio": None, "series": []}
    s = pd.Series([row["ic"] for row in rows], dtype=float)
    std = float(s.std(ddof=1)) if len(s) > 1 else 0.0
    mean = float(s.mean())
    return {
        "days": int(len(rows)),
        "mean_ic": mean,
        "median_ic": float(s.median()),
        "ic_std": std,
        "ic_ir": float(mean / std * math.sqrt(252)) if std > 0 else None,
        "positive_ratio": float((s > 0).mean()),
        "series": rows[-80:],
    }


def _quantile_spread(df: pd.DataFrame, target: str, min_symbols: int, q: float = 0.2) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    bucket_rows: List[Dict[str, Any]] = []
    for date_value, day in df[["date", "factor_value", target]].dropna().groupby("date"):
        if len(day) < min_symbols or day["factor_value"].nunique() < 5:
            continue
        rank = day["factor_value"].rank(method="first", pct=True)
        low = day.loc[rank <= q, target]
        high = day.loc[rank >= 1.0 - q, target]
        if low.empty or high.empty:
            continue
        rows.append(
            {
                "date": pd.Timestamp(date_value).strftime("%Y-%m-%d"),
                "top_mean": float(high.mean()),
                "bottom_mean": float(low.mean()),
                "spread": float(high.mean() - low.mean()),
            }
        )
        buckets = np.minimum(np.floor(rank * 5).astype(int), 4) + 1
        tmp = day.assign(bucket=buckets)
        for bucket, group in tmp.groupby("bucket"):
            bucket_rows.append({"bucket": int(bucket), "ret": float(group[target].mean())})
    if not rows:
        return {"days": 0, "top_bottom_spread": None, "spread_positive_ratio": None, "buckets": [], "series": []}
    spread = pd.Series([row["spread"] for row in rows], dtype=float)
    bucket_df = pd.DataFrame(bucket_rows)
    buckets = []
    if not bucket_df.empty:
        for bucket, group in bucket_df.groupby("bucket"):
            buckets.append({"bucket": int(bucket), "mean_return": float(group["ret"].mean())})
    return {
        "days": int(len(rows)),
        "top_bottom_spread": float(spread.mean()),
        "spread_positive_ratio": float((spread > 0).mean()),
        "buckets": buckets,
        "series": rows[-80:],
    }


def run_gen2_factor_test(
    factor_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    horizon: int = 1,
    min_symbols: int = 80,
) -> Dict[str, Any]:
    normalized = _normalize_factor_id(factor_id)
    meta = _factor_meta(normalized)
    if meta is None:
        return {"available": False, "message": f"Unknown factor: {factor_id}", "factor_id": factor_id}

    latest = _latest_trade_date()
    if not latest:
        return {"available": False, "implemented": True, "factor": meta, "message": "No kline_daily trade date found"}
    end_ts = _date_or_default(end_date, pd.Timestamp(latest))
    start_ts = _date_or_default(start_date, end_ts - pd.Timedelta(days=420))
    horizon = int(max(1, min(int(horizon or 1), 20)))
    min_symbols = int(max(20, min(int(min_symbols or 80), 1000)))

    prewarm_days = _estimate_prewarm_days(meta)
    daily = _load_daily_ohlcv(
        start_ts.strftime("%Y-%m-%d"),
        end_ts.strftime("%Y-%m-%d"),
        horizon=horizon,
        prewarm_days=prewarm_days,
    )
    if daily.empty:
        return {"available": False, "implemented": True, "factor": meta, "message": "Daily OHLCV data is empty"}

    try:
        factor_df = _add_factor(daily, meta)
    except Alpha191UnsupportedError as exc:
        return {
            "available": False,
            "implemented": False,
            "factor": meta,
            "message": f"{meta.get('id')} requires additional data source: {exc}",
        }
    except Alpha191FormulaError as exc:
        return {
            "available": False,
            "implemented": False,
            "factor": meta,
            "message": f"{meta.get('id')} formula parse failed: {exc}",
        }

    enriched = _add_forward_returns(factor_df, horizons=sorted({1, 3, 5, horizon}))
    eval_df = enriched[(enriched["date"] >= start_ts) & (enriched["date"] <= end_ts)].copy()
    target = f"fwd_{horizon}d"
    valid = eval_df.dropna(subset=["factor_value", target]).copy()
    if valid.empty:
        return {
            "available": False,
            "implemented": True,
            "factor": meta,
            "message": "No valid factor/return samples",
            "sample": {"loaded_rows": int(len(daily)), "eval_rows": int(len(eval_df))},
        }

    ic = _daily_ic(valid, target, min_symbols=min_symbols)
    spread = _quantile_spread(valid, target, min_symbols=min_symbols)
    latest_rows = (
        valid.sort_values(["date", "factor_value"], ascending=[False, False])
        .head(20)[["date", "code", "name", "factor_value", target]]
        .copy()
    )
    latest_rows["date"] = latest_rows["date"].dt.strftime("%Y-%m-%d")

    return {
        "available": True,
        "implemented": True,
        "factor": meta,
        "params": {
            "start_date": start_ts.strftime("%Y-%m-%d"),
            "end_date": end_ts.strftime("%Y-%m-%d"),
            "horizon": horizon,
            "target": target,
            "min_symbols": min_symbols,
            "decision_lag": "Factor is generated after T close and used for T+1 or later decisions.",
            "benchmark_code": BENCHMARK_CODE,
            "prewarm_days": prewarm_days,
        },
        "sample": {
            "loaded_rows": int(len(daily)),
            "eval_rows": int(len(eval_df)),
            "valid_rows": int(len(valid)),
            "trade_days": int(valid["date"].nunique()),
            "symbols": int(valid["code"].nunique()),
            "factor_coverage": float(valid["factor_value"].notna().mean()),
        },
        "ic": ic,
        "spread": spread,
        "latest_rows": [{k: _json_safe(v) for k, v in row.items()} for row in latest_rows.to_dict("records")],
        "message": f"{meta.get('id')} factor test completed",
    }
