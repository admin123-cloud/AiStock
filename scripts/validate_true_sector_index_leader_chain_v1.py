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


SOURCE_WINDOWS = ROOT / "reports" / "true_sector_index_quarterly_stock_acceptance_v1" / "windows.csv"
OUT_DIR = ROOT / "reports" / "true_sector_index_leader_chain_v1"


def _load_true_windows(years: list[int]) -> pd.DataFrame:
    if not SOURCE_WINDOWS.exists():
        raise RuntimeError(f"missing source windows: {SOURCE_WINDOWS}")
    df = pd.read_csv(SOURCE_WINDOWS, encoding="utf-8-sig")
    df["anchor_date"] = pd.to_datetime(df["anchor_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["anchor_date", "sector_code"]).copy()
    df["year"] = df["anchor_date"].str.slice(0, 4).astype(int)
    return df[df["year"].isin(set(years))].copy()


def _year_load_start(first_anchor: str, lookback_days: int) -> str:
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
    if df.empty:
        raise RuntimeError(f"no lookback dates before {first_anchor}")
    return pd.to_datetime(df["trade_date"], errors="coerce").dropna().min().strftime("%Y-%m-%d")


def _sector_leader_stats(day: pd.DataFrame, members: pd.DataFrame, window_rows: pd.DataFrame) -> pd.DataFrame:
    sector_codes = set(window_rows["sector_code"].astype(str))
    m = members[members["sector_code"].astype(str).isin(sector_codes)].copy()
    merged = day.merge(m, left_on="code6", right_on="stock_code6", how="inner")
    if merged.empty:
        return pd.DataFrame()
    for col in ["mom20", "mom60", "amount_ratio5_20", "acceptance_score", "close", "ma20", "ma60"]:
        merged[col] = pd.to_numeric(merged.get(col), errors="coerce")
    merged["is_acceptance"] = merged["acceptance"].fillna(False).astype(bool)
    merged["is_leader"] = (
        (merged["mom20"] >= 0.12)
        & (merged["amount_ratio5_20"] >= 1.05)
        & (merged["close"] >= merged["ma20"])
    )
    merged["is_strong_leader"] = (
        (merged["mom20"] >= 0.20)
        & (merged["amount_ratio5_20"] >= 1.10)
        & (merged["close"] >= merged["ma20"])
    )
    rows: list[dict[str, Any]] = []
    for (sector_code, sector_name), g in merged.groupby(["sector_code", "sector_name"], dropna=False):
        member_count = int(g["code6"].nunique())
        acceptance = g[g["is_acceptance"]].copy()
        leaders = g[g["is_leader"]].copy()
        strong_leaders = g[g["is_strong_leader"]].copy()
        top_accept = acceptance.sort_values("acceptance_score", ascending=False).head(5)
        rows.append(
            {
                "anchor_date": str(day["trade_date"].iloc[0])[:10],
                "sector_code": str(sector_code),
                "sector_name": str(sector_name),
                "member_count": member_count,
                "acceptance_count": int(acceptance["code6"].nunique()),
                "acceptance_ratio": _safe_float(acceptance["code6"].nunique() / max(member_count, 1)),
                "leader_count": int(leaders["code6"].nunique()),
                "strong_leader_count": int(strong_leaders["code6"].nunique()),
                "top5_acceptance_score_avg": _safe_float(top_accept["acceptance_score"].mean()),
                "top5_mom20_avg": _safe_float(top_accept["mom20"].mean()),
                "top5_amount_ratio_avg": _safe_float(top_accept["amount_ratio5_20"].mean()),
            }
        )
    return pd.DataFrame(rows)


def _confirm_sectors(
    stats: pd.DataFrame,
    min_acceptance_count: int,
    min_acceptance_ratio: float,
    min_leader_count: int,
    min_top5_score: float,
) -> pd.DataFrame:
    if stats.empty:
        return stats
    s = stats.copy()
    s["leader_chain_score"] = (
        25.0 * (pd.to_numeric(s["acceptance_count"], errors="coerce") / 10.0).clip(0.0, 1.0).fillna(0.0)
        + 25.0 * (pd.to_numeric(s["acceptance_ratio"], errors="coerce") / 0.16).clip(0.0, 1.0).fillna(0.0)
        + 20.0 * (pd.to_numeric(s["leader_count"], errors="coerce") / 8.0).clip(0.0, 1.0).fillna(0.0)
        + 15.0 * (pd.to_numeric(s["strong_leader_count"], errors="coerce") / 4.0).clip(0.0, 1.0).fillna(0.0)
        + 15.0 * (pd.to_numeric(s["top5_acceptance_score_avg"], errors="coerce") / 70.0).clip(0.0, 1.0).fillna(0.0)
    )
    confirmed = s[
        (pd.to_numeric(s["acceptance_count"], errors="coerce") >= int(min_acceptance_count))
        & (pd.to_numeric(s["acceptance_ratio"], errors="coerce") >= float(min_acceptance_ratio))
        & (pd.to_numeric(s["leader_count"], errors="coerce") >= int(min_leader_count))
        & (pd.to_numeric(s["top5_acceptance_score_avg"], errors="coerce") >= float(min_top5_score))
    ].copy()
    return confirmed.sort_values(["leader_chain_score", "acceptance_count"], ascending=False)


def _run_year(
    year: int,
    args: argparse.Namespace,
    windows_df: pd.DataFrame,
    members: pd.DataFrame,
    stock_pool: pd.DataFrame,
    horizons: list[int],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    year_windows = windows_df[windows_df["year"].eq(year)].copy()
    if year_windows.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    first_anchor = year_windows["anchor_date"].min()
    load_start = _year_load_start(first_anchor, args.lookback_days)
    daily = _load_daily(load_start, f"{year}-12-31")
    stock_daily = _forward_return_columns(_add_stock_acceptance_features(_add_stock_features(daily)), horizons)

    stats_parts: list[pd.DataFrame] = []
    summary_parts: list[pd.DataFrame] = []
    detail_parts: list[pd.DataFrame] = []
    for anchor, wr in year_windows.groupby("anchor_date"):
        day = stock_daily[stock_daily["trade_date"].eq(pd.Timestamp(anchor))].copy()
        if day.empty:
            continue
        stats = _sector_leader_stats(day, members, wr)
        confirmed = _confirm_sectors(
            stats,
            args.min_acceptance_count,
            args.min_acceptance_ratio,
            args.min_leader_count,
            args.min_top5_score,
        )
        if not confirmed.empty:
            confirmed["year"] = int(year)
            stats_parts.append(confirmed)
            window = {
                "anchor_date": anchor,
                "top_sectors": confirmed["sector_code"].astype(str).tolist(),
            }
            summary_rows, detail = _window_samples(stock_daily, members, stock_pool, window, horizons)
            if summary_rows:
                summary_parts.append(pd.DataFrame(summary_rows))
            if not detail.empty:
                detail["year"] = int(year)
                detail_parts.append(detail)
    del daily, stock_daily
    gc.collect()
    stats_out = pd.concat(stats_parts, ignore_index=True) if stats_parts else pd.DataFrame()
    summary_out = pd.concat(summary_parts, ignore_index=True) if summary_parts else pd.DataFrame()
    detail_out = pd.concat(detail_parts, ignore_index=True) if detail_parts else pd.DataFrame()
    return stats_out, summary_out, detail_out


def _write_report(result: dict[str, Any], stats: pd.DataFrame, summary: pd.DataFrame, comparison: pd.DataFrame, detail: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stats_path = OUT_DIR / "confirmed_sector_leader_chain.csv"
    summary_path = OUT_DIR / "summary.csv"
    comparison_path = OUT_DIR / "comparison.csv"
    detail_path = OUT_DIR / "mainline_acceptance_examples.csv"
    report_path = OUT_DIR / "REPORT.md"
    json_path = OUT_DIR / "run_summary.json"
    stats.to_csv(stats_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    payload = dict(result)
    payload.update(
        {
            "stats_csv": str(stats_path),
            "summary_csv": str(summary_path),
            "comparison_csv": str(comparison_path),
            "detail_csv": str(detail_path),
            "report": str(report_path),
        }
    )
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    aggregate = summary.groupby(["group", "horizon"], as_index=False).agg(avg=("avg", "mean")) if not summary.empty else pd.DataFrame()
    wide = aggregate.pivot_table(index="horizon", columns="group", values="avg", aggfunc="first").reset_index() if not aggregate.empty else pd.DataFrame()
    lines = [
        "# 真实板块指数 + 龙头链确认 V1",
        "",
        f"- 来源窗口：`{SOURCE_WINDOWS}`",
        f"- 年度：`{result['years']}`",
        f"- 确认窗口数：`{result['confirmed_anchor_count']}`，确认板块行数：`{result['confirmed_sector_rows']}`",
        "- 含义：真实板块指数给出主线环境，龙头链确认要求板块内同时出现足够多承接股、动量龙头和较高 Top5 承接质量。",
        "- 注意：板块成分仍使用当前 `sector_stocks`，不是 point-in-time 成分，结论仍为研究用途。",
        "",
        "## 跨窗口平均结果",
        "",
        "| 持有天数 | 龙头链主线承接股 | 全市场承接股 | 差值 | 龙头链主线全体股 | 全市场全体股 |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in wide.sort_values("horizon").itertuples(index=False) if not wide.empty else []:
        main_accept = getattr(row, "mainline_sector_acceptance", None)
        market_accept = getattr(row, "market_acceptance", None)
        diff = None if main_accept is None or market_accept is None else main_accept - market_accept
        lines.append(
            f"| {int(row.horizon)} | {_pct(main_accept)} | {_pct(market_accept)} | {_pct(diff)} | "
            f"{_pct(getattr(row, 'mainline_sector_all', None))} | {_pct(getattr(row, 'market_all', None))} |"
        )
    lines.extend(
        [
            "",
            "## 解读",
            "",
            "- 如果 20/60 日改善，说明龙头链确认能把真实主线窗口转成更可交易的买点环境。",
            "- 如果只有 120 日改善，说明它仍主要是中期环境因子。",
            "- 如果三档都变弱，说明当前龙头链定义过拟合或过晚，应该转向盘中扩散而不是日线龙头确认。",
            "",
            f"- 确认板块明细：`{stats_path}`",
            f"- 对比表：`{comparison_path}`",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(stats_path))
    print(str(comparison_path))


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
    parser = argparse.ArgumentParser(description="Validate leader-chain confirmation on true sector-index windows.")
    parser.add_argument("--years", default="2020-2025")
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--horizons", default="20,60,120")
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--min-acceptance-count", type=int, default=3)
    parser.add_argument("--min-acceptance-ratio", type=float, default=0.05)
    parser.add_argument("--min-leader-count", type=int, default=3)
    parser.add_argument("--min-top5-score", type=float, default=45.0)
    args = parser.parse_args()

    years = _parse_years(args.years)
    horizons = _parse_horizons(args.horizons)
    windows_df = _load_true_windows(years)
    members = _load_members([args.level], 5)
    stock_pool = _load_stock_pool()

    stats_parts: list[pd.DataFrame] = []
    summary_parts: list[pd.DataFrame] = []
    detail_parts: list[pd.DataFrame] = []
    for year in years:
        print(f"YEAR {year} start")
        stats, summary, detail = _run_year(year, args, windows_df, members, stock_pool, horizons)
        print(f"YEAR {year} confirmed_anchors={0 if stats.empty else stats['anchor_date'].nunique()} sectors={len(stats)}")
        if not stats.empty:
            stats_parts.append(stats)
        if not summary.empty:
            summary_parts.append(summary)
        if not detail.empty:
            detail_parts.append(detail)

    stats = pd.concat(stats_parts, ignore_index=True) if stats_parts else pd.DataFrame()
    summary = pd.concat(summary_parts, ignore_index=True) if summary_parts else pd.DataFrame()
    comparison = _comparison(summary) if not summary.empty else pd.DataFrame()
    detail = pd.concat(detail_parts, ignore_index=True) if detail_parts else pd.DataFrame()
    result = {
        "years": years,
        "level": args.level,
        "horizons": horizons,
        "min_acceptance_count": args.min_acceptance_count,
        "min_acceptance_ratio": args.min_acceptance_ratio,
        "min_leader_count": args.min_leader_count,
        "min_top5_score": args.min_top5_score,
        "source_anchor_count": int(windows_df["anchor_date"].nunique()) if not windows_df.empty else 0,
        "source_sector_rows": int(len(windows_df)),
        "confirmed_anchor_count": int(stats["anchor_date"].nunique()) if not stats.empty else 0,
        "confirmed_sector_rows": int(len(stats)),
    }
    _write_report(result, stats, summary, comparison, detail)
    _print_aggregate(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
