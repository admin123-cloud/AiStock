from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df, clickhouse_scalar  # noqa: E402
from utils.kline_store import filter_trading_day_rows  # noqa: E402


SH_TZ = ZoneInfo("Asia/Shanghai")


def _parse_levels(text: str) -> list[int]:
    levels: list[int] = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        value = int(item)
        if value not in {1, 2, 3}:
            raise ValueError("levels only supports 1,2,3")
        levels.append(value)
    return sorted(set(levels)) or [2]


def _latest_trade_date() -> str:
    value = clickhouse_scalar("SELECT max(trade_date) FROM kline_daily")
    if value is None:
        raise RuntimeError("kline_daily has no trade_date")
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _sql_list(values: list[Any]) -> str:
    out: list[str] = []
    for value in values:
        text = str(value).replace("\\", "\\\\").replace("'", "\\'")
        out.append(f"'{text}'")
    return ",".join(out)


def ensure_sector_kline_schema() -> None:
    client = clickhouse_client()
    existing = clickhouse_query_df("DESCRIBE TABLE sector_kline_daily")
    names = set(existing["name"].astype(str).tolist()) if not existing.empty and "name" in existing.columns else set()
    for name, after in [
        ("open", "trade_date"),
        ("high", "open"),
        ("low", "high"),
        ("close", "low"),
    ]:
        if name not in names:
            client.command(f"ALTER TABLE sector_kline_daily ADD COLUMN IF NOT EXISTS {name} Nullable(Float64) AFTER {after}")


def load_members(levels: list[int], min_members: int) -> pd.DataFrame:
    level_text = ",".join(str(x) for x in levels)
    df = clickhouse_query_df(
        f"""
        SELECT
            ss.stock_code AS stock_code,
            s.code AS sector_code,
            s.name AS sector_name,
            s.level AS level,
            s.stock_count AS stock_count
        FROM sector_stocks ss
        INNER JOIN sectors s ON ss.sector_code = s.code
        WHERE s.type = 'industry'
          AND s.level IN ({level_text})
        """
    )
    if df.empty:
        raise RuntimeError("no Shenwan industry members found in sector_stocks/sectors")
    df["stock_code"] = df["stock_code"].astype(str)
    df["sector_code"] = df["sector_code"].astype(str)
    df["level"] = pd.to_numeric(df["level"], errors="coerce").astype("Int64")
    counts = df.groupby(["sector_code", "level"])["stock_code"].nunique().rename("member_count").reset_index()
    df = df.merge(counts, on=["sector_code", "level"], how="left")
    df = df[pd.to_numeric(df["member_count"], errors="coerce").fillna(0) >= int(min_members)].copy()
    if df.empty:
        raise RuntimeError("no Shenwan industry members left after min_members filter")
    return df.drop_duplicates(["sector_code", "stock_code"]).reset_index(drop=True)


def load_daily(codes: list[str], start_date: str, end_date: str, max_abs_change_pct: float = 21.0, *, query_df=None) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    query_start = pd.Timestamp(start_date) - pd.Timedelta(days=14)
    code_text = _sql_list(codes)
    df = (query_df or clickhouse_query_df)(
        f"""
        SELECT code, trade_date, open, high, low, close, volume, amount, change_pct
        FROM kline_daily FINAL
        WHERE code IN ({code_text})
          AND trade_date BETWEEN ? AND ?
          AND open > 0
          AND high > 0
          AND low > 0
          AND close > 0
          AND change_pct > -50
          AND change_pct < 50
        ORDER BY code, trade_date
        """,
        [query_start.strftime("%Y-%m-%d"), end_date],
    )
    if df.empty:
        raise RuntimeError("kline_daily query returned no rows")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount", "change_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).copy()
    df = df.sort_values(["code", "trade_date", "amount"]).groupby(["code", "trade_date"], as_index=False).tail(1)
    df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
    change = pd.to_numeric(df["change_pct"], errors="coerce") / 100.0
    max_ret = float(max_abs_change_pct) / 100.0
    raw_prev = df.groupby("code")["close"].shift(1)
    price_ret = df["close"].astype(float) / raw_prev.astype(float) - 1.0
    use_price_basis = raw_prev.gt(0) & price_ret.between(-max_ret, max_ret)
    use_change_basis = change.between(-max_ret, max_ret)
    implied_prev = df["close"].astype(float) / (1.0 + change)
    df["prev_close"] = np.where(use_price_basis, raw_prev, np.where(use_change_basis, implied_prev, np.nan))
    df["basis_source"] = np.where(use_price_basis, "price_ratio", np.where(use_change_basis, "change_pct", "invalid"))
    df = df[pd.to_numeric(df["prev_close"], errors="coerce").fillna(0) > 0].copy()
    prev = df["prev_close"].astype(float)
    df["open_ret"] = df["open"].astype(float) / prev - 1.0
    df["high_ret"] = df["high"].astype(float) / prev - 1.0
    df["low_ret"] = df["low"].astype(float) / prev - 1.0
    df["close_ret"] = df["close"].astype(float) / prev - 1.0
    mask = (df["trade_date"] >= pd.Timestamp(start_date)) & (df["trade_date"] <= pd.Timestamp(end_date))
    return df.loc[mask].reset_index(drop=True)


def build_sector_rows(members: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    merged = members[["sector_code", "stock_code"]].merge(daily, left_on="stock_code", right_on="code", how="inner")
    if merged.empty:
        raise RuntimeError("no sector-member kline rows after joining Shenwan members with kline_daily")
    ret_cols = ["open_ret", "high_ret", "low_ret", "close_ret"]
    for col in ret_cols:
        merged[col] = pd.to_numeric(merged[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    merged = merged.dropna(subset=["sector_code", "trade_date", "close_ret"]).copy()
    merged["rise_flag"] = merged["close_ret"] > 0.0001
    merged["fall_flag"] = merged["close_ret"] < -0.0001
    merged["flat_flag"] = ~(merged["rise_flag"] | merged["fall_flag"])
    stock_change = pd.to_numeric(merged["change_pct"], errors="coerce")
    merged["limit_up_flag"] = stock_change >= 9.9
    merged["limit_down_flag"] = stock_change <= -9.9
    grouped = (
        merged.groupby(["sector_code", "trade_date"], as_index=False)
        .agg(
            open_ret=("open_ret", "median"),
            high_ret=("high_ret", "median"),
            low_ret=("low_ret", "median"),
            close_ret=("close_ret", "median"),
            stock_count=("code", "nunique"),
            rise_count=("rise_flag", "sum"),
            fall_count=("fall_flag", "sum"),
            flat_count=("flat_flag", "sum"),
            limit_up_count=("limit_up_flag", "sum"),
            limit_down_count=("limit_down_flag", "sum"),
            total_amount=("amount", "sum"),
            total_volume=("volume", "sum"),
        )
        .sort_values(["sector_code", "trade_date"])
        .reset_index(drop=True)
    )
    now = datetime.now(SH_TZ).replace(tzinfo=None)
    rows: list[dict[str, Any]] = []
    for sector_code, frame in grouped.groupby("sector_code", sort=False):
        prev_close = 1000.0
        for _, row in frame.sort_values("trade_date").iterrows():
            open_price = prev_close * (1.0 + float(row["open_ret"]))
            high_price = prev_close * (1.0 + float(row["high_ret"]))
            low_price = prev_close * (1.0 + float(row["low_ret"]))
            close_price = prev_close * (1.0 + float(row["close_ret"]))
            rows.append(
                {
                    "code": str(sector_code),
                    "trade_date": pd.Timestamp(row["trade_date"]).date(),
                    "open": round(open_price, 6),
                    "high": round(max(open_price, high_price, close_price), 6),
                    "low": round(min(open_price, low_price, close_price), 6),
                    "close": round(close_price, 6),
                    "change_pct": round(float(row["close_ret"]) * 100.0, 6),
                    "stock_count": int(row["stock_count"]),
                    "rise_count": int(row["rise_count"]),
                    "fall_count": int(row["fall_count"]),
                    "flat_count": int(row["flat_count"]),
                    "limit_up_count": int(row["limit_up_count"]),
                    "limit_down_count": int(row["limit_down_count"]),
                    "total_amount": float(row["total_amount"] or 0.0),
                    "total_volume": int(row["total_volume"] or 0),
                    "created_at": now,
                }
            )
            prev_close = close_price if close_price > 0 else prev_close
    return pd.DataFrame(rows)


def load_official_sw_index_map(levels: list[int]) -> pd.DataFrame:
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on runtime package
        raise RuntimeError(f"akshare unavailable for official SW index fetch: {exc}") from exc

    frames: list[pd.DataFrame] = []
    loaders = {
        1: ak.sw_index_first_info,
        2: ak.sw_index_second_info,
        3: ak.sw_index_third_info,
    }
    for level in levels:
        loader = loaders.get(int(level))
        if loader is None:
            continue
        info = loader()
        if info is None or info.empty:
            continue
        code_col = info.columns[0]
        name_col = info.columns[1]
        count_col = info.columns[2] if int(level) == 1 else info.columns[3]
        frame = pd.DataFrame(
            {
                "level": int(level),
                "official_code": info[code_col].astype(str).str.strip(),
                "sector_name": info[name_col].astype(str).str.strip(),
                "official_member_count": pd.to_numeric(info[count_col], errors="coerce").fillna(0).astype(int),
            }
        )
        frame["official_symbol"] = frame["official_code"].str.replace(".SI", "", regex=False)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["level", "official_code", "official_symbol", "sector_name", "official_member_count"])
    return pd.concat(frames, ignore_index=True).drop_duplicates(["level", "sector_name"])


def fetch_official_sw_index_history(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak  # type: ignore

    raw = ak.index_hist_sw(symbol=str(symbol).replace(".SI", ""), period="day")
    if raw is None or raw.empty:
        return pd.DataFrame()
    cols = list(raw.columns)
    if len(cols) < 8:
        raise RuntimeError(f"unexpected index_hist_sw columns for {symbol}: {cols}")
    out = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(raw[cols[1]], errors="coerce"),
            "close": pd.to_numeric(raw[cols[2]], errors="coerce"),
            "open": pd.to_numeric(raw[cols[3]], errors="coerce"),
            "high": pd.to_numeric(raw[cols[4]], errors="coerce"),
            "low": pd.to_numeric(raw[cols[5]], errors="coerce"),
            "total_volume": pd.to_numeric(raw[cols[6]], errors="coerce").fillna(0.0) * 100000000.0,
            "total_amount": pd.to_numeric(raw[cols[7]], errors="coerce").fillna(0.0) * 100000000.0,
        }
    )
    out = out.dropna(subset=["trade_date", "open", "high", "low", "close"]).copy()
    out = out[(out["trade_date"] >= pd.Timestamp(start_date)) & (out["trade_date"] <= pd.Timestamp(end_date))].copy()
    if out.empty:
        return out
    out = out.sort_values("trade_date").reset_index(drop=True)
    prev_close = out["close"].shift(1)
    out["change_pct"] = (out["close"] / prev_close - 1.0) * 100.0
    first = out.index[0]
    if pd.isna(out.loc[first, "change_pct"]):
        open_price = float(out.loc[first, "open"] or 0.0)
        close_price = float(out.loc[first, "close"] or 0.0)
        out.loc[first, "change_pct"] = (close_price / open_price - 1.0) * 100.0 if open_price > 0 else 0.0
    out["trade_date"] = out["trade_date"].dt.date
    return out


def apply_official_sw_index_ohlc(
    synthetic_rows: pd.DataFrame,
    members: pd.DataFrame,
    start_date: str,
    end_date: str,
    levels: list[int],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    official_map = load_official_sw_index_map(levels)
    if official_map.empty:
        return synthetic_rows, {"enabled": True, "mapped_sector_count": 0, "official_row_count": 0, "errors": ["empty_official_map"]}

    sector_meta = members[["sector_code", "sector_name", "level"]].drop_duplicates().copy()
    sector_meta["level"] = pd.to_numeric(sector_meta["level"], errors="coerce").astype("Int64")
    mapped = sector_meta.merge(official_map, on=["level", "sector_name"], how="inner")
    if mapped.empty:
        return synthetic_rows, {"enabled": True, "mapped_sector_count": 0, "official_row_count": 0, "errors": ["no_sector_name_match"]}

    synthetic = synthetic_rows.copy()
    synthetic["trade_date"] = pd.to_datetime(synthetic["trade_date"], errors="coerce").dt.date
    synthetic_idx = synthetic.set_index(["code", "trade_date"], drop=False)

    official_frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for _, item in mapped.iterrows():
        sector_code = str(item.get("sector_code") or "").strip()
        symbol = str(item.get("official_symbol") or "").strip()
        official_code = str(item.get("official_code") or "").strip()
        if not sector_code or not symbol:
            continue
        try:
            hist = fetch_official_sw_index_history(symbol, start_date, end_date)
        except Exception as exc:
            errors.append(f"{sector_code}:{official_code}:{exc}")
            continue
        if hist.empty:
            errors.append(f"{sector_code}:{official_code}:empty_history")
            continue
        hist["code"] = sector_code
        hist["created_at"] = datetime.now(SH_TZ).replace(tzinfo=None)
        for col in ["stock_count", "rise_count", "fall_count", "flat_count", "limit_up_count", "limit_down_count"]:
            hist[col] = 0
        for idx, row in hist.iterrows():
            key = (sector_code, row["trade_date"])
            if key in synthetic_idx.index:
                source = synthetic_idx.loc[key]
                if isinstance(source, pd.DataFrame):
                    source = source.iloc[-1]
                for col in ["stock_count", "rise_count", "fall_count", "flat_count", "limit_up_count", "limit_down_count"]:
                    hist.at[idx, col] = int(source.get(col) or 0)
            if int(hist.at[idx, "stock_count"] or 0) <= 0:
                hist.at[idx, "stock_count"] = int(item.get("official_member_count") or 0)
        official_frames.append(hist)

    if not official_frames:
        return synthetic_rows, {
            "enabled": True,
            "mapped_sector_count": int(len(mapped)),
            "official_row_count": 0,
            "errors": errors or ["no_official_history"],
        }

    official_rows = pd.concat(official_frames, ignore_index=True)
    official_codes = set(official_rows["code"].astype(str).unique().tolist())
    non_official = synthetic[~synthetic["code"].astype(str).isin(official_codes)].copy()
    expected_columns = [
        "code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "change_pct",
        "stock_count",
        "rise_count",
        "fall_count",
        "flat_count",
        "limit_up_count",
        "limit_down_count",
        "total_amount",
        "total_volume",
        "created_at",
    ]
    for frame in (non_official, official_rows):
        for col in expected_columns:
            if col not in frame.columns:
                frame[col] = None
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
        frame["created_at"] = pd.to_datetime(frame["created_at"], errors="coerce").dt.to_pydatetime()
    non_official = non_official[expected_columns].copy()
    official_rows = official_rows[expected_columns].copy()
    out = pd.concat([non_official, official_rows], ignore_index=True)
    out = out.sort_values(["code", "trade_date"]).reset_index(drop=True)
    return out, {
        "enabled": True,
        "mapped_sector_count": int(len(mapped)),
        "official_sector_count": int(len(official_codes)),
        "official_row_count": int(len(official_rows)),
        "errors": errors[:20],
    }


def write_sector_rows(rows: pd.DataFrame, start_date: str, end_date: str, sector_codes: list[str], execute: bool) -> None:
    if rows.empty:
        raise RuntimeError("no sector_kline_daily rows to write")
    rows = filter_trading_day_rows("1d", rows)
    if rows.empty:
        raise RuntimeError("no trading-day sector_kline_daily rows to write")
    client = clickhouse_client()
    if not execute:
        return
    client.command(
        f"""
        ALTER TABLE sector_kline_daily
        DELETE WHERE trade_date BETWEEN toDate('{start_date}') AND toDate('{end_date}')
          AND code IN ({_sql_list(sector_codes)})
        """
    )
    client.insert_df(
        "sector_kline_daily",
        rows[
            [
                "code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "change_pct",
                "stock_count",
                "rise_count",
                "fall_count",
                "flat_count",
                "limit_up_count",
                "limit_down_count",
                "total_amount",
                "total_volume",
                "created_at",
            ]
        ],
        column_names=[
            "code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "change_pct",
            "stock_count",
            "rise_count",
            "fall_count",
            "flat_count",
            "limit_up_count",
            "limit_down_count",
            "total_amount",
            "total_volume",
            "created_at",
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild sector_kline_daily from Shenwan industry members.")
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--levels", default="1,2,3")
    parser.add_argument("--min-members", type=int, default=1)
    parser.add_argument("--max-stock-change-pct", type=float, default=21.0)
    parser.add_argument("--official-index", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--official-index-levels", default="1,2")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    end_date = args.end_date or _latest_trade_date()
    levels = _parse_levels(args.levels)
    ensure_sector_kline_schema()
    members = load_members(levels, args.min_members)
    codes = sorted(members["stock_code"].astype(str).unique().tolist())
    daily = load_daily(codes, args.start_date, end_date, max_abs_change_pct=float(args.max_stock_change_pct))
    rows = build_sector_rows(members, daily)
    official_summary: dict[str, Any] = {"enabled": bool(args.official_index)}
    if args.official_index:
        official_levels = [level for level in levels if level in set(_parse_levels(args.official_index_levels))]
        if official_levels:
            rows, official_summary = apply_official_sw_index_ohlc(rows, members, args.start_date, end_date, official_levels)
        else:
            official_summary = {"enabled": True, "mapped_sector_count": 0, "official_row_count": 0, "skipped": "no_level_enabled"}
    sector_codes = sorted(members["sector_code"].astype(str).unique().tolist())
    write_sector_rows(rows, args.start_date, end_date, sector_codes, execute=bool(args.execute))
    summary = {
        "execute": bool(args.execute),
        "start_date": args.start_date,
        "end_date": end_date,
        "levels": levels,
        "member_rows": int(len(members)),
        "stock_codes": int(len(codes)),
        "sector_codes": int(len(sector_codes)),
        "daily_rows": int(len(daily)),
        "sector_kline_rows": int(len(rows)),
        "official_index": official_summary,
        "max_stock_change_pct": float(args.max_stock_change_pct),
        "min_trade_date": str(rows["trade_date"].min()),
        "max_trade_date": str(rows["trade_date"].max()),
    }
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
