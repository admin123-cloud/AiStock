from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
    _safe_float,
    _summarize,
)
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "quarterly_mainline_stock_acceptance_v1"


def _parse_horizons(text: str) -> list[int]:
    return [int(x.strip()) for x in str(text).split(",") if x.strip()]


def _load_trade_dates(start_date: str, end_date: str) -> list[str]:
    df = clickhouse_query_df(
        """
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        [start_date, end_date],
    )
    if df.empty:
        raise RuntimeError("no trade dates")
    return [x.strftime("%Y-%m-%d") for x in pd.to_datetime(df["trade_date"], errors="coerce").dropna()]


def _pick_anchor_dates(trade_dates: list[str], start_date: str, step_days: int, max_horizon: int) -> list[str]:
    dates = [d for d in trade_dates if d >= start_date]
    usable = max(0, len(dates) - int(max_horizon))
    return [dates[i] for i in range(0, usable, int(step_days))]


def _select_windows(
    sector_features: pd.DataFrame,
    anchor_dates: list[str],
    level: int,
    top_n: int,
    min_ret60: float,
    max_ret60: float,
    min_ma20_ratio: float,
    min_rise_ratio: float,
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    f = sector_features.copy()
    f["date_text"] = pd.to_datetime(f["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    windows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for anchor in anchor_dates:
        day = f[(f["date_text"].eq(anchor)) & (f["level"].eq(level))].copy()
        if day.empty:
            continue
        for col in ["ret20", "ret60", "rank_ret60_pct", "relative_ret60", "ma20_ratio", "rise_ratio", "amount_ratio20_60"]:
            day[col] = pd.to_numeric(day[col], errors="coerce")
        picked = day[
            (day["ret60"].between(min_ret60, max_ret60))
            & (day["rank_ret60_pct"] >= 0.75)
            & (day["relative_ret60"] > 0)
            & (day["ma20_ratio"] >= min_ma20_ratio)
            & (day["rise_ratio"] >= min_rise_ratio)
        ].copy()
        if picked.empty:
            continue
        picked["window_score"] = (
            35.0 * ((picked["rank_ret60_pct"] - 0.75) / 0.25).clip(0.0, 1.0).fillna(0.0)
            + 25.0 * ((picked["relative_ret60"]) / 0.18).clip(0.0, 1.0).fillna(0.0)
            + 20.0 * ((picked["ma20_ratio"] - min_ma20_ratio) / 0.30).clip(0.0, 1.0).fillna(0.0)
            + 10.0 * ((picked["rise_ratio"] - min_rise_ratio) / 0.25).clip(0.0, 1.0).fillna(0.0)
            + 10.0 * ((picked["amount_ratio20_60"] - 0.95) / 0.50).clip(0.0, 1.0).fillna(0.0)
        )
        picked = picked.sort_values(["window_score", "rank_ret60_pct"], ascending=False).head(top_n)
        sector_codes = [str(x) for x in picked["sector_code"].tolist()]
        windows.append(
            {
                "anchor_date": anchor,
                "level": level,
                "top_sectors": sector_codes,
            }
        )
        for row in picked.itertuples(index=False):
            rows.append(
                {
                    "anchor_date": anchor,
                    "level": int(row.level),
                    "sector_code": str(row.sector_code),
                    "sector_name": str(row.sector_name),
                    "window_score": _safe_float(row.window_score),
                    "ret20": _safe_float(row.ret20),
                    "ret60": _safe_float(row.ret60),
                    "relative_ret60": _safe_float(row.relative_ret60),
                    "ma20_ratio": _safe_float(row.ma20_ratio),
                    "rise_ratio": _safe_float(row.rise_ratio),
                    "amount_ratio20_60": _safe_float(row.amount_ratio20_60),
                }
            )
    return windows, pd.DataFrame(rows)


def _window_samples(
    stock_daily: pd.DataFrame,
    members: pd.DataFrame,
    stock_pool: pd.DataFrame,
    window: dict[str, Any],
    horizons: list[int],
) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    anchor = pd.Timestamp(window["anchor_date"])
    day = stock_daily[stock_daily["trade_date"].eq(anchor)].copy()
    if day.empty:
        return [], pd.DataFrame()

    top_members = members[members["sector_code"].isin(set(window["top_sectors"]))].copy()
    top_codes = set(top_members["stock_code6"].astype(str))
    day["in_mainline_sector"] = day["code6"].astype(str).isin(top_codes)
    day = day.merge(stock_pool, on="code6", how="left")

    market_all = day.copy()
    mainline_all = day[day["in_mainline_sector"]].copy()
    market_accept = day[day["acceptance"]].copy()
    mainline_accept = day[day["in_mainline_sector"] & day["acceptance"]].copy()

    summary: list[dict[str, Any]] = []
    label_date = int(str(window["anchor_date"]).replace("-", ""))
    summary.extend(_summarize("market_all", market_all, horizons, label_date))
    summary.extend(_summarize("mainline_sector_all", mainline_all, horizons, label_date))
    summary.extend(_summarize("market_acceptance", market_accept, horizons, label_date))
    summary.extend(_summarize("mainline_sector_acceptance", mainline_accept, horizons, label_date))

    detail = mainline_accept.sort_values("acceptance_score", ascending=False).head(50).copy()
    detail["anchor_date"] = str(window["anchor_date"])
    detail["top_sector_codes"] = ",".join(window["top_sectors"])
    keep = [
        "anchor_date",
        "code_raw",
        "stock_name",
        "close",
        "mom20",
        "mom60",
        "amount_ratio5_20",
        "runup_from_60d_low",
        "acceptance_score",
        "top_sector_codes",
    ] + [f"fwd_ret_{h}" for h in horizons]
    return summary, detail[[c for c in keep if c in detail.columns]]


def _write_outputs(
    result: dict[str, Any],
    windows_detail: pd.DataFrame,
    summary: pd.DataFrame,
    comparison: pd.DataFrame,
    detail: pd.DataFrame,
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    windows_path = OUT_DIR / "windows.csv"
    summary_path = OUT_DIR / "summary.csv"
    comparison_path = OUT_DIR / "comparison.csv"
    detail_path = OUT_DIR / "mainline_acceptance_examples.csv"
    json_path = OUT_DIR / "summary.json"
    report_path = OUT_DIR / "REPORT.md"

    windows_detail.to_csv(windows_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    payload = dict(result)
    payload.update(
        {
            "windows_csv": str(windows_path),
            "summary_csv": str(summary_path),
            "comparison_csv": str(comparison_path),
            "detail_csv": str(detail_path),
            "report": str(report_path),
        }
    )
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    aggregate = summary.groupby(["group", "horizon"], as_index=False).agg(
        count=("count", "sum"),
        avg=("avg", "mean"),
        median=("median", "mean"),
        win_rate=("win_rate", "mean"),
    )
    aggregate_cmp = aggregate.pivot_table(index="horizon", columns="group", values="avg", aggfunc="first").reset_index()
    if "mainline_sector_acceptance" in aggregate_cmp.columns and "market_acceptance" in aggregate_cmp.columns:
        aggregate_cmp["mainline_accept_minus_market_accept"] = (
            aggregate_cmp["mainline_sector_acceptance"] - aggregate_cmp["market_acceptance"]
        )

    lines = [
        "# 季度滚动主线窗口 + 个股承接 V1",
        "",
        f"- 区间：`{result['start_date']}` 到 `{result['end_date']}`",
        f"- 板块层级：L{result['level']}，检查步长：每 `{result['step_days']}` 个交易日一次",
        f"- 触发窗口数：`{result['window_count']}`，窗口内板块数：每次最多 `{result['top_n']}` 个",
        "- 重要说明：本版用当前行业成分 + 个股日线等权合成板块强度，属于季度滚动近似验证，不是真板块指数 point-in-time 回测。",
        "",
        "## 跨窗口平均结果",
        "",
        "| 持有天数 | 主线承接股 | 全市场承接股 | 差值 | 主线全体股 | 全市场全体股 |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregate_cmp.sort_values("horizon").itertuples(index=False):
        lines.append(
            f"| {int(row.horizon)} | {_pct(getattr(row, 'mainline_sector_acceptance', None))} | "
            f"{_pct(getattr(row, 'market_acceptance', None))} | "
            f"{_pct(getattr(row, 'mainline_accept_minus_market_accept', None))} | "
            f"{_pct(getattr(row, 'mainline_sector_all', None))} | {_pct(getattr(row, 'market_all', None))} |"
        )
    lines.extend(
        [
            "",
            "## 解读规则",
            "",
            "- 若 `主线承接股 - 全市场承接股` 在 60/120 日显著为正，说明主线窗口过滤对主升浪持有有增益。",
            "- 若只有 20 日为正，说明它更像短线启动/波段过滤，不足以定义主升浪板块。",
            "- 若差值不稳定或为负，说明板块窗口没有独立选股能力，后续应转向更严格的真实指数、盘口扩散和龙头链确认。",
            "",
            f"- 窗口明细：`{windows_path}`",
            f"- 对比表：`{comparison_path}`",
            f"- 个股样本：`{detail_path}`",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(windows_path))
    print(str(comparison_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Quarterly rolling synthetic-sector mainline window plus stock acceptance.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2025-12-31")
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

    horizons = _parse_horizons(args.horizons)
    trade_dates = _load_trade_dates(args.start_date, args.end_date)
    anchors = _pick_anchor_dates(trade_dates, args.start_date, args.step_days, max(horizons))
    if not anchors:
        raise RuntimeError("no usable anchor dates")
    load_start_df = clickhouse_query_df(
        """
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [anchors[0], int(args.lookback_days)],
    )
    load_start = pd.to_datetime(load_start_df["trade_date"], errors="coerce").dropna().min().strftime("%Y-%m-%d")

    members = _load_members([args.level], args.min_members)
    stock_pool = _load_stock_pool()
    daily = _load_daily(load_start, args.end_date)
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

    all_summary: list[dict[str, Any]] = []
    details: list[pd.DataFrame] = []
    for window in windows:
        window_summary, detail = _window_samples(stock_daily, members, stock_pool, window, horizons)
        all_summary.extend(window_summary)
        if not detail.empty:
            details.append(detail)

    summary = pd.DataFrame(all_summary)
    comparison = _comparison(summary) if not summary.empty else pd.DataFrame()
    detail = pd.concat(details, ignore_index=True) if details else pd.DataFrame()
    result = {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "load_start": load_start,
        "level": args.level,
        "top_n": args.top_n,
        "step_days": args.step_days,
        "horizons": horizons,
        "anchor_count": len(anchors),
        "window_count": len(windows),
        "member_rows": int(len(members)),
        "daily_rows": int(len(daily)),
        "sector_daily_rows": int(len(sector_daily)),
    }
    _write_outputs(result, windows_detail, summary, comparison, detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
