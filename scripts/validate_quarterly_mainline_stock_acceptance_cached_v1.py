from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.validate_quarterly_mainline_stock_acceptance_v1 as q
from scripts.research_main_wave_sector_score import (
    _add_sector_feature_frame,
    _add_stock_features,
    _build_sector_daily,
    _load_daily,
    _load_members,
)
from scripts.validate_mainline_window_stock_acceptance import (
    _add_stock_acceptance_features,
    _comparison,
    _forward_return_columns,
    _load_stock_pool,
    _pct,
)
from scripts.validate_quarterly_mainline_stock_acceptance_v1 import (
    _load_trade_dates,
    _parse_horizons,
    _pick_anchor_dates,
    _select_windows,
    _window_samples,
)
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "quarterly_mainline_stock_acceptance_cached_v1"


def _parse_years(text: str) -> list[int]:
    years: list[int] = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            left, right = item.split("-", 1)
            years.extend(range(int(left), int(right) + 1))
        else:
            years.append(int(item))
    return sorted(set(years))


def _year_load_start(anchor: str, lookback_days: int) -> str:
    df = clickhouse_query_df(
        """
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [anchor, int(lookback_days)],
    )
    if df.empty:
        raise RuntimeError(f"no lookback dates before {anchor}")
    return pd.to_datetime(df["trade_date"], errors="coerce").dropna().min().strftime("%Y-%m-%d")


def _run_year(
    year: int,
    args: argparse.Namespace,
    members: pd.DataFrame,
    stock_pool: pd.DataFrame,
    horizons: list[int],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    trade_dates = _load_trade_dates(start_date, end_date)
    anchors = _pick_anchor_dates(trade_dates, start_date, args.step_days, max(horizons))
    if not anchors:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    load_start = _year_load_start(anchors[0], args.lookback_days)
    daily = _load_daily(load_start, end_date)
    stock_daily = _forward_return_columns(_add_stock_acceptance_features(_add_stock_features(daily)), horizons)
    sector_daily = _build_sector_daily(stock_daily, members)
    sector_features = _add_sector_feature_frame(sector_daily)
    windows, windows_detail = _select_windows(
        sector_features,
        anchors,
        args.level,
        args.top_n,
        args.min_ret60,
        args.max_ret60,
        args.min_ma20_ratio,
        args.min_rise_ratio,
    )

    all_summary: list[dict] = []
    details: list[pd.DataFrame] = []
    for window in windows:
        window_summary, detail = _window_samples(stock_daily, members, stock_pool, window, horizons)
        all_summary.extend(window_summary)
        if not detail.empty:
            detail["year"] = int(year)
            details.append(detail)

    summary = pd.DataFrame(all_summary)
    detail = pd.concat(details, ignore_index=True) if details else pd.DataFrame()
    if not windows_detail.empty:
        windows_detail["year"] = int(year)
    del daily, stock_daily, sector_daily, sector_features
    gc.collect()
    return windows_detail, summary, detail


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
        main_all = getattr(row, "mainline_sector_all", None)
        market_all = getattr(row, "market_all", None)
        diff = None
        if main_accept is not None and market_accept is not None:
            diff = main_accept - market_accept
        print(
            f"h{int(row.horizon)} main_accept={_pct(main_accept)} "
            f"market_accept={_pct(market_accept)} diff={_pct(diff)} "
            f"main_all={_pct(main_all)} market_all={_pct(market_all)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Cached yearly quarterly mainline window validation.")
    parser.add_argument("--years", default="2020-2025")
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--step-days", type=int, default=60)
    parser.add_argument("--horizons", default="20,60,120")
    parser.add_argument("--min-members", type=int, default=5)
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--min-ret60", type=float, default=0.02)
    parser.add_argument("--max-ret60", type=float, default=0.60)
    parser.add_argument("--min-ma20-ratio", type=float, default=0.48)
    parser.add_argument("--min-rise-ratio", type=float, default=0.45)
    args = parser.parse_args()

    years = _parse_years(args.years)
    horizons = _parse_horizons(args.horizons)
    members = _load_members([args.level], args.min_members)
    stock_pool = _load_stock_pool()

    windows_parts: list[pd.DataFrame] = []
    summary_parts: list[pd.DataFrame] = []
    detail_parts: list[pd.DataFrame] = []
    for year in years:
        print(f"YEAR {year} start")
        windows_detail, summary, detail = _run_year(year, args, members, stock_pool, horizons)
        print(f"YEAR {year} windows={0 if windows_detail.empty else windows_detail['anchor_date'].nunique()} rows={len(summary)}")
        if not windows_detail.empty:
            windows_parts.append(windows_detail)
        if not summary.empty:
            summary_parts.append(summary)
        if not detail.empty:
            detail_parts.append(detail)

    windows_detail = pd.concat(windows_parts, ignore_index=True) if windows_parts else pd.DataFrame()
    summary = pd.concat(summary_parts, ignore_index=True) if summary_parts else pd.DataFrame()
    comparison = _comparison(summary) if not summary.empty else pd.DataFrame()
    detail = pd.concat(detail_parts, ignore_index=True) if detail_parts else pd.DataFrame()

    result = {
        "start_date": f"{years[0]}-01-01",
        "end_date": f"{years[-1]}-12-31",
        "years": years,
        "level": args.level,
        "top_n": args.top_n,
        "step_days": args.step_days,
        "horizons": horizons,
        "window_count": int(windows_detail["anchor_date"].nunique()) if not windows_detail.empty else 0,
        "sector_window_rows": int(len(windows_detail)),
        "member_rows": int(len(members)),
        "stock_pool_rows": int(len(stock_pool)),
        "method_note": "按年切片执行，口径与 quarterly_mainline_stock_acceptance_v1 一致；仍使用当前成分合成板块，非 point-in-time 真板块指数。",
    }
    q.OUT_DIR = OUT_DIR
    q._write_outputs(result, windows_detail, summary, comparison, detail)
    (OUT_DIR / "run_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_aggregate(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
