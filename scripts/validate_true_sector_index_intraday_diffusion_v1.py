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
OUT_DIR = ROOT / "reports" / "true_sector_index_intraday_diffusion_v1"


def _load_true_windows(years: list[int]) -> pd.DataFrame:
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


def _load_intraday_day(table: str, anchor_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        f"""
        SELECT code, datetime, open, high, low, close, volume, amount
        FROM {table}
        WHERE toDate(datetime) = ?
        """,
        [anchor_date],
    )
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "datetime", "open", "close"]).copy()
    df["code6"] = df["code"].astype(str).str.split(".").str[0].str.zfill(6).str[-6:]
    return df.sort_values(["code6", "datetime"]).reset_index(drop=True)


def _morning_stock_returns(intraday: pd.DataFrame, cutoff_time: str) -> pd.DataFrame:
    if intraday.empty:
        return pd.DataFrame()
    cutoff = pd.to_datetime(intraday["datetime"].dt.strftime("%Y-%m-%d") + " " + cutoff_time)
    visible = intraday[intraday["datetime"] <= cutoff].copy()
    if visible.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for code6, g in visible.groupby("code6"):
        g = g.sort_values("datetime")
        first = g.iloc[0]
        last = g.iloc[-1]
        open_price = float(first["open"] or 0)
        close_price = float(last["close"] or 0)
        if open_price <= 0 or close_price <= 0:
            continue
        rows.append(
            {
                "code6": str(code6),
                "bar_count": int(len(g)),
                "morning_ret": close_price / open_price - 1.0,
                "morning_amount": float(pd.to_numeric(g["amount"], errors="coerce").fillna(0).sum()),
            }
        )
    return pd.DataFrame(rows)


def _sector_diffusion_stats(
    morning: pd.DataFrame,
    members: pd.DataFrame,
    window_rows: pd.DataFrame,
    min_visible_members: int,
) -> pd.DataFrame:
    if morning.empty:
        return pd.DataFrame()
    sector_codes = set(window_rows["sector_code"].astype(str))
    m = members[members["sector_code"].astype(str).isin(sector_codes)].copy()
    merged = morning.merge(m, left_on="code6", right_on="stock_code6", how="inner")
    if merged.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for (sector_code, sector_name), g in merged.groupby(["sector_code", "sector_name"], dropna=False):
        member_count = int(g["code6"].nunique())
        if member_count < int(min_visible_members):
            continue
        ret = pd.to_numeric(g["morning_ret"], errors="coerce").dropna()
        if ret.empty:
            continue
        rows.append(
            {
                "sector_code": str(sector_code),
                "sector_name": str(sector_name),
                "visible_members": member_count,
                "morning_ret_avg": _safe_float(ret.mean()),
                "morning_ret_median": _safe_float(ret.median()),
                "morning_rise_ratio": _safe_float((ret > 0).mean()),
                "morning_strong1_ratio": _safe_float((ret >= 0.01).mean()),
                "morning_strong2_ratio": _safe_float((ret >= 0.02).mean()),
                "morning_strong3_ratio": _safe_float((ret >= 0.03).mean()),
                "morning_weak_ratio": _safe_float((ret <= -0.01).mean()),
            }
        )
    return pd.DataFrame(rows)


def _confirm_diffusion(
    stats: pd.DataFrame,
    min_rise_ratio: float,
    min_strong2_ratio: float,
    min_avg_ret: float,
) -> pd.DataFrame:
    if stats.empty:
        return stats
    s = stats.copy()
    s["intraday_diffusion_score"] = (
        35.0 * (pd.to_numeric(s["morning_rise_ratio"], errors="coerce") / 0.75).clip(0.0, 1.0).fillna(0.0)
        + 30.0 * (pd.to_numeric(s["morning_strong2_ratio"], errors="coerce") / 0.18).clip(0.0, 1.0).fillna(0.0)
        + 20.0 * (pd.to_numeric(s["morning_ret_avg"], errors="coerce") / 0.035).clip(0.0, 1.0).fillna(0.0)
        + 15.0 * (1.0 - (pd.to_numeric(s["morning_weak_ratio"], errors="coerce") / 0.25).clip(0.0, 1.0).fillna(0.0))
    )
    out = s[
        (pd.to_numeric(s["morning_rise_ratio"], errors="coerce") >= float(min_rise_ratio))
        & (pd.to_numeric(s["morning_strong2_ratio"], errors="coerce") >= float(min_strong2_ratio))
        & (pd.to_numeric(s["morning_ret_avg"], errors="coerce") >= float(min_avg_ret))
    ].copy()
    return out.sort_values(["intraday_diffusion_score", "morning_strong2_ratio"], ascending=False)


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
        intraday = _load_intraday_day(args.table, anchor)
        morning = _morning_stock_returns(intraday, args.cutoff_time)
        stats = _sector_diffusion_stats(morning, members, wr, args.min_visible_members)
        confirmed = _confirm_diffusion(stats, args.min_rise_ratio, args.min_strong2_ratio, args.min_avg_ret)
        if confirmed.empty:
            continue
        confirmed["anchor_date"] = anchor
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
    stats_path = OUT_DIR / "confirmed_intraday_diffusion_sectors.csv"
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
        "# 真实板块指数 + 盘中扩散 V1",
        "",
        f"- 来源窗口：`{SOURCE_WINDOWS}`",
        f"- 分钟周期：`{result['table']}`，可见截止：`{result['cutoff_time']}`",
        f"- 确认窗口数：`{result['confirmed_anchor_count']}`，确认板块行数：`{result['confirmed_sector_rows']}`",
        "- 信号含义：真实板块指数给出主线环境，盘中扩散要求锚点日上午板块内上涨比例、2%以上强势比例和平均涨幅同步达标。",
        "- 避免未来函数：扩散统计只使用锚点日 `cutoff_time` 之前的分钟线；前瞻收益仍按锚点日收盘后的日线持有计算。",
        "",
        "## 跨窗口平均结果",
        "",
        "| 持有天数 | 盘中扩散主线承接股 | 全市场承接股 | 差值 | 盘中扩散主线全体股 | 全市场全体股 |",
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
            "- 若 20/60 日差值改善，说明盘中扩散能把中期主线环境转成更可交易的启动确认。",
            "- 若 120 日仍强但 20/60 日弱，说明盘中扩散仍偏环境确认，不能直接当买点。",
            "- 若三档都弱，说明主升浪板块捕捉不能只靠行业层面，需要回到个股 G2/V4 的实时相对强度。",
            "",
            f"- 扩散板块明细：`{stats_path}`",
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
    global OUT_DIR
    parser = argparse.ArgumentParser(description="Validate intraday diffusion on true sector-index windows.")
    parser.add_argument("--years", default="2020-2025")
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--horizons", default="20,60,120")
    parser.add_argument("--lookback-days", type=int, default=160)
    parser.add_argument("--table", default="kline_minute_60")
    parser.add_argument("--cutoff-time", default="11:30:00")
    parser.add_argument("--min-visible-members", type=int, default=15)
    parser.add_argument("--min-rise-ratio", type=float, default=0.55)
    parser.add_argument("--min-strong2-ratio", type=float, default=0.06)
    parser.add_argument("--min-avg-ret", type=float, default=0.006)
    parser.add_argument("--out-dir-name", default="true_sector_index_intraday_diffusion_v1")
    args = parser.parse_args()
    OUT_DIR = ROOT / "reports" / args.out_dir_name

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
        "table": args.table,
        "cutoff_time": args.cutoff_time,
        "min_visible_members": args.min_visible_members,
        "min_rise_ratio": args.min_rise_ratio,
        "min_strong2_ratio": args.min_strong2_ratio,
        "min_avg_ret": args.min_avg_ret,
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
