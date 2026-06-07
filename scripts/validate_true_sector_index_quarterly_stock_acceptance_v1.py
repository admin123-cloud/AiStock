from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.validate_quarterly_mainline_stock_acceptance_v1 as q
from data_fetcher.sources.tdxquant_pool import tdxquant_pool
from scripts.research_main_wave_sector_score import _add_stock_features, _load_daily, _load_members
from scripts.validate_mainline_window_stock_acceptance import (
    _add_stock_acceptance_features,
    _comparison,
    _forward_return_columns,
    _load_stock_pool,
    _pct,
    _safe_float,
)
from scripts.validate_quarterly_mainline_stock_acceptance_cached_v1 import _parse_years
from scripts.validate_quarterly_mainline_stock_acceptance_v1 import _parse_horizons, _window_samples
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "true_sector_index_quarterly_stock_acceptance_v1"


def _load_sectors(level: int) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, name, level
        FROM sectors
        WHERE type = 'industry'
          AND level = ?
        ORDER BY code
        """,
        [int(level)],
    )
    if df.empty:
        raise RuntimeError(f"no sectors for level={level}")
    return df


def _fetch_tdx_sector_close(sectors: pd.DataFrame, start_date: str, end_date: str, batch_size: int) -> pd.DataFrame:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = OUT_DIR / f"tdx_sector_close_L{int(sectors['level'].iloc[0])}_{start_date}_{end_date}.csv"
    if cache_path.exists():
        return pd.read_csv(cache_path, encoding="utf-8-sig")

    codes = sectors["code"].astype(str).tolist()
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), int(batch_size)):
        batch = codes[i : i + int(batch_size)]
        data = tdxquant_pool.get_market_data(
            field_list=[],
            stock_list=batch,
            period="1d",
            start_time=start_date.replace("-", ""),
            end_time=end_date.replace("-", ""),
            count=-1,
            dividend_type="none",
            fill_data=False,
        )
        if not data or "Close" not in data:
            continue
        close = data["Close"]
        if close is None or close.empty:
            continue
        close = close.copy()
        close.index = pd.to_datetime(close.index, errors="coerce")
        close = close.dropna(axis=0, how="all")
        long = close.reset_index().rename(columns={"index": "trade_date"}).melt(
            id_vars=["trade_date"], var_name="sector_code", value_name="close"
        )
        long["sector_code"] = long["sector_code"].astype(str)
        long["close"] = pd.to_numeric(long["close"], errors="coerce")
        long = long.dropna(subset=["trade_date", "sector_code", "close"])
        parts.append(long)
        print(f"FETCH batch={i // int(batch_size) + 1} codes={len(batch)} rows={len(long)}")
    if not parts:
        raise RuntimeError("TdxQuant returned no sector close data")
    out = pd.concat(parts, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["trade_date"]).drop_duplicates(["trade_date", "sector_code"])
    out = out.merge(sectors.rename(columns={"code": "sector_code", "name": "sector_name"}), on="sector_code", how="left")
    out.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return out


def _pick_anchor_dates(index_daily: pd.DataFrame, years: list[int], step_days: int, max_horizon: int) -> list[str]:
    dates = sorted(index_daily["trade_date"].dropna().astype(str).unique().tolist())
    anchors: list[str] = []
    for year in years:
        year_dates = [d for d in dates if f"{year}-01-01" <= d <= f"{year}-12-31"]
        usable = max(0, len(year_dates) - int(max_horizon))
        anchors.extend([year_dates[i] for i in range(0, usable, int(step_days))])
    return anchors


def _select_true_index_windows(
    index_daily: pd.DataFrame,
    anchor_dates: list[str],
    top_n: int,
    min_ret60: float,
    max_ret60: float,
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    d = index_daily.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["trade_date", "sector_code", "close"]).sort_values(["sector_code", "trade_date"])
    g = d.groupby("sector_code", group_keys=False)
    d["ret20"] = g["close"].pct_change(20)
    d["ret60"] = g["close"].pct_change(60)
    d["ma20"] = g["close"].rolling(20, min_periods=15).mean().reset_index(level=0, drop=True)
    d["ma60"] = g["close"].rolling(60, min_periods=40).mean().reset_index(level=0, drop=True)
    market = d.groupby("trade_date", as_index=False).agg(market_ret60=("ret60", "mean"))
    d = d.merge(market, on="trade_date", how="left")
    d["relative_ret60"] = d["ret60"] - d["market_ret60"]
    d["rank_ret60_pct"] = d.groupby("trade_date")["ret60"].rank(pct=True, ascending=True)
    d["ma20_above_ma60"] = d["ma20"] >= d["ma60"]
    d["date_text"] = d["trade_date"].dt.strftime("%Y-%m-%d")

    windows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for anchor in anchor_dates:
        day = d[d["date_text"].eq(anchor)].copy()
        if day.empty:
            continue
        picked = day[
            (day["ret60"].between(float(min_ret60), float(max_ret60)))
            & (day["relative_ret60"] > 0)
            & (day["rank_ret60_pct"] >= 0.75)
            & (day["ma20_above_ma60"])
        ].copy()
        if picked.empty:
            continue
        picked["window_score"] = (
            40.0 * ((picked["rank_ret60_pct"] - 0.75) / 0.25).clip(0.0, 1.0).fillna(0.0)
            + 35.0 * (picked["relative_ret60"] / 0.18).clip(0.0, 1.0).fillna(0.0)
            + 25.0 * ((picked["ret20"]) / 0.18).clip(0.0, 1.0).fillna(0.0)
        )
        picked = picked.sort_values(["window_score", "rank_ret60_pct"], ascending=False).head(int(top_n))
        sector_codes = [str(x) for x in picked["sector_code"].tolist()]
        windows.append({"anchor_date": anchor, "top_sectors": sector_codes})
        for row in picked.itertuples(index=False):
            rows.append(
                {
                    "anchor_date": anchor,
                    "sector_code": str(row.sector_code),
                    "sector_name": str(row.sector_name),
                    "window_score": _safe_float(row.window_score),
                    "ret20": _safe_float(row.ret20),
                    "ret60": _safe_float(row.ret60),
                    "relative_ret60": _safe_float(row.relative_ret60),
                    "rank_ret60_pct": _safe_float(row.rank_ret60_pct),
                }
            )
    return windows, pd.DataFrame(rows)


def _validate_stock_acceptance(
    windows: list[dict[str, Any]],
    years: list[int],
    horizons: list[int],
    members: pd.DataFrame,
    stock_pool: pd.DataFrame,
    lookback_days: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    all_summary: list[pd.DataFrame] = []
    all_detail: list[pd.DataFrame] = []
    for year in years:
        year_windows = [w for w in windows if str(w["anchor_date"]).startswith(str(year))]
        if not year_windows:
            continue
        first_anchor = min(str(w["anchor_date"]) for w in year_windows)
        df = clickhouse_query_df(
            """
            SELECT DISTINCT trade_date
            FROM kline_daily
            WHERE trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [first_anchor, int(lookback_days)],
        )
        load_start = pd.to_datetime(df["trade_date"], errors="coerce").dropna().min().strftime("%Y-%m-%d")
        daily = _load_daily(load_start, f"{year}-12-31")
        stock_daily = _forward_return_columns(_add_stock_acceptance_features(_add_stock_features(daily)), horizons)
        for window in year_windows:
            summary_rows, detail = _window_samples(stock_daily, members, stock_pool, window, horizons)
            if summary_rows:
                all_summary.append(pd.DataFrame(summary_rows))
            if not detail.empty:
                detail["year"] = year
                all_detail.append(detail)
        del daily, stock_daily
        gc.collect()
    summary = pd.concat(all_summary, ignore_index=True) if all_summary else pd.DataFrame()
    detail = pd.concat(all_detail, ignore_index=True) if all_detail else pd.DataFrame()
    return summary, detail


def _print_aggregate(summary: pd.DataFrame) -> None:
    if summary.empty:
        print("AGGREGATE empty")
        return
    aggregate = summary.groupby(["group", "horizon"], as_index=False).agg(avg=("avg", "mean"))
    wide = aggregate.pivot_table(index="horizon", columns="group", values="avg", aggfunc="first").reset_index()
    print("AGGREGATE")
    for row in wide.sort_values("horizon").itertuples(index=False):
        main_accept = getattr(row, "mainline_sector_acceptance", None)
        market_accept = getattr(row, "market_acceptance", None)
        diff = None if main_accept is None or market_accept is None else main_accept - market_accept
        print(
            f"h{int(row.horizon)} main_accept={_pct(main_accept)} "
            f"market_accept={_pct(market_accept)} diff={_pct(diff)} "
            f"main_all={_pct(getattr(row, 'mainline_sector_all', None))} "
            f"market_all={_pct(getattr(row, 'market_all', None))}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate true TdxQuant sector index quarterly window plus stock acceptance.")
    parser.add_argument("--years", default="2020-2025")
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--step-days", type=int, default=60)
    parser.add_argument("--horizons", default="20,60,120")
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--min-ret60", type=float, default=0.02)
    parser.add_argument("--max-ret60", type=float, default=0.80)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    years = _parse_years(args.years)
    horizons = _parse_horizons(args.horizons)
    fetch_start = f"{years[0] - 1}-09-01"
    fetch_end = f"{years[-1]}-12-31"
    sectors = _load_sectors(args.level)
    index_daily = _fetch_tdx_sector_close(sectors, fetch_start, fetch_end, args.batch_size)
    anchor_dates = _pick_anchor_dates(index_daily, years, args.step_days, max(horizons))
    windows, windows_detail = _select_true_index_windows(index_daily, anchor_dates, args.top_n, args.min_ret60, args.max_ret60)

    members = _load_members([args.level], 5)
    stock_pool = _load_stock_pool()
    summary, detail = _validate_stock_acceptance(windows, years, horizons, members, stock_pool, args.lookback_days)
    comparison = _comparison(summary) if not summary.empty else pd.DataFrame()

    q.OUT_DIR = OUT_DIR
    result = {
        "start_date": f"{years[0]}-01-01",
        "end_date": f"{years[-1]}-12-31",
        "years": years,
        "level": args.level,
        "top_n": args.top_n,
        "step_days": args.step_days,
        "horizons": horizons,
        "sector_count": int(len(sectors)),
        "index_rows": int(len(index_daily)),
        "anchor_count": int(len(anchor_dates)),
        "window_count": int(pd.Series([w["anchor_date"] for w in windows]).nunique()) if windows else 0,
        "sector_window_rows": int(len(windows_detail)),
        "method_note": "窗口使用 TdxQuant 真实板块指数 close；个股承接仍使用当前 sector_stocks 成分映射，仍存在历史成分偏差。",
    }
    q._write_outputs(result, windows_detail, summary, comparison, detail)
    (OUT_DIR / "run_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_aggregate(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
