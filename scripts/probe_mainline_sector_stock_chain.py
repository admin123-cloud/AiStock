from __future__ import annotations

import math
from typing import Any

import pandas as pd

from utils.market_warehouse import clickhouse_query_df


L2_SECTOR_SQL = """
SELECT code AS sector_code, name AS sector_name, level, type
FROM sectors
WHERE type = 'industry' AND level = 2
ORDER BY sector_code
"""


def _quote_list(values: list[str]) -> str:
    safe = [str(v or "").replace("\\", "\\\\").replace("'", "\\'") for v in values if str(v or "").strip()]
    return ",".join(f"'{item}'" for item in safe)


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _fetch_l2_sectors() -> pd.DataFrame:
    df = clickhouse_query_df(L2_SECTOR_SQL)
    if df.empty:
        return pd.DataFrame(columns=["sector_code", "sector_name", "level", "type"])
    out = df.copy()
    out["sector_code"] = out["sector_code"].astype(str)
    out["sector_name"] = out["sector_name"].astype(str)
    return out


def _fetch_sector_history(sectors: pd.DataFrame) -> pd.DataFrame:
    if sectors.empty or "sector_code" not in sectors.columns:
        return pd.DataFrame(columns=["sector_code", "sector_name", "date", "close", "change_pct"])
    codes = sorted(sectors["sector_code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame(columns=["sector_code", "sector_name", "date", "close", "change_pct"])
    sql = f"""
    SELECT code AS sector_code, trade_date, change_pct
    FROM sector_kline_daily
    WHERE code IN ({_quote_list(codes)})
    ORDER BY sector_code, trade_date
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return pd.DataFrame(columns=["sector_code", "sector_name", "date", "close", "change_pct"])
    out = df.copy()
    out["sector_code"] = out["sector_code"].astype(str)
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    out["change_pct"] = pd.to_numeric(out["change_pct"], errors="coerce").fillna(0.0)
    out["daily_ret"] = out["change_pct"] / 100.0
    out["close"] = out.groupby("sector_code")["daily_ret"].transform(lambda s: 1000.0 * (1.0 + s).cumprod())
    out = out.merge(sectors[["sector_code", "sector_name"]], on="sector_code", how="left")
    out["date"] = out["trade_date"]
    return out[["sector_code", "sector_name", "date", "close", "change_pct"]].dropna(subset=["date"]).reset_index(drop=True)


def _sector_rank_for_year(hist: pd.DataFrame, year: int) -> tuple[pd.DataFrame | None, dict[str, Any] | None]:
    if hist.empty:
        return None, None
    d = hist.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["sector_code", "date", "close"]).sort_values(["sector_code", "date"])
    ydf = d[d["date"].dt.year.eq(int(year))].copy()
    if ydf.empty:
        return None, None
    unique_dates = sorted(pd.Timestamp(x) for x in ydf["date"].drop_duplicates().tolist())
    if not unique_dates:
        return None, None
    first = unique_dates[0]
    anchor_candidates = [x for x in unique_dates if x <= pd.Timestamp(f"{int(year)}-02-28")]
    anchor = anchor_candidates[-1] if anchor_candidates else unique_dates[min(len(unique_dates) - 1, max(0, len(unique_dates) // 3))]
    anchor_idx = unique_dates.index(anchor)
    h20 = unique_dates[min(len(unique_dates) - 1, anchor_idx + 20)]
    first_map = (
        ydf[ydf["date"].eq(first)][["sector_code", "close"]]
        .rename(columns={"close": "first_close"})
        .drop_duplicates("sector_code")
    )
    anchor_map = (
        ydf[ydf["date"].eq(anchor)][["sector_code", "close"]]
        .rename(columns={"close": "anchor_close"})
        .drop_duplicates("sector_code")
    )
    ranked = (
        ydf[["sector_code", "sector_name"]]
        .drop_duplicates("sector_code")
        .merge(first_map, on="sector_code", how="inner")
        .merge(anchor_map, on="sector_code", how="inner")
    )
    ranked = ranked[(ranked["first_close"] > 0) & (ranked["anchor_close"] > 0)].copy()
    if ranked.empty:
        return None, None
    ranked["sector_early_ret"] = ranked["anchor_close"] / ranked["first_close"] - 1.0
    ranked["sector_rank"] = ranked["sector_early_ret"].rank(ascending=False, method="first").astype(int)
    ranked = ranked.sort_values(["sector_rank", "sector_code"]).reset_index(drop=True)
    meta = {
        "year": int(year),
        "first": first.strftime("%Y-%m-%d"),
        "anchor": anchor.strftime("%Y-%m-%d"),
        "horizon_dates": {"h20": h20.strftime("%Y-%m-%d")},
        "sector_count": int(len(ranked)),
    }
    return ranked[["sector_code", "sector_name", "sector_early_ret", "sector_rank"]], meta


def _fetch_sector_members(sectors: pd.DataFrame) -> pd.DataFrame:
    if sectors.empty or "sector_code" not in sectors.columns:
        return pd.DataFrame(columns=["sector_code", "sector_name", "stock_code", "stock_name"])
    codes = sorted(sectors["sector_code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame(columns=["sector_code", "sector_name", "stock_code", "stock_name"])
    sql = f"""
    SELECT
        ss.sector_code,
        ss.stock_code,
        COALESCE(st.name, ss.stock_code) AS stock_name
    FROM sector_stocks ss
    LEFT JOIN stocks st ON st.code = ss.stock_code
    WHERE ss.sector_code IN ({_quote_list(codes)})
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        return pd.DataFrame(columns=["sector_code", "sector_name", "stock_code", "stock_name"])
    out = df.copy()
    out["sector_code"] = out["sector_code"].astype(str)
    out["stock_code"] = out["stock_code"].astype(str)
    out["stock_name"] = out["stock_name"].astype(str)
    out = out.merge(sectors[["sector_code", "sector_name"]].drop_duplicates("sector_code"), on="sector_code", how="left")
    return out[["sector_code", "sector_name", "stock_code", "stock_name"]].drop_duplicates(["sector_code", "stock_code"]).reset_index(drop=True)


def _fetch_index_features(trade_date: str) -> pd.DataFrame:
    target = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    sql = """
    SELECT code, trade_date, close
    FROM kline_daily
    WHERE code IN ('000852.SH', '999999.SH')
      AND trade_date <= ?
    ORDER BY code, trade_date
    """
    df = clickhouse_query_df(sql, [target])
    if df.empty:
        return pd.DataFrame(columns=["code", "trade_date", "close", "ma20"])
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"])
    out["ma20"] = out.groupby("code")["close"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    return out


def _market_flags(index_features: pd.DataFrame, trade_date: str) -> dict[str, Any]:
    if index_features.empty:
        return {
            "trade_date": trade_date,
            "csi1000_above_ma20": None,
            "sse_above_ma20": None,
        }

    def _flag_for(code: str) -> bool | None:
        row = (
            index_features[index_features["code"].eq(code)]
            .sort_values("trade_date")
            .tail(1)
        )
        if row.empty:
            return None
        close = _safe_float(row.iloc[0].get("close"))
        ma20 = _safe_float(row.iloc[0].get("ma20"))
        if close is None or ma20 is None:
            return None
        return bool(close >= ma20)

    return {
        "trade_date": trade_date,
        "csi1000_above_ma20": _flag_for("000852.SH"),
        "sse_above_ma20": _flag_for("999999.SH"),
    }
