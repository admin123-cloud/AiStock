from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_main_wave_sector_score import (
    _code6,
    _load_daily,
    _load_members,
)
from utils.market_warehouse import clickhouse_query_df


SOURCE = ROOT / "reports" / "mainline_sector_direction_tdxquant_probe" / "summary.json"
OUT_DIR = ROOT / "reports" / "mainline_window_stock_acceptance_v1"


def _safe_float(value: Any, digits: int = 6) -> Any:
    if value is None:
        return None
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _pct(value: Any) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


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


def _load_windows(level: int, years: list[int], top_n: int) -> list[dict[str, Any]]:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for row in payload.get("yearly", []):
        if int(row.get("level", -1)) != int(level):
            continue
        year = int(row.get("year"))
        if year not in years:
            continue
        sectors = row.get("early_top_sectors", [])[:top_n]
        if not sectors:
            continue
        out.append(
            {
                "year": year,
                "level": level,
                "anchor_date": str(row["anchor_date"]),
                "last_trade_date": str(row["last_trade_date"]),
                "top_sectors": [str(x["code"]) for x in sectors],
                "top_sector_names": [str(x.get("name", "")) for x in sectors],
            }
        )
    if not out:
        raise RuntimeError("no cached true-index windows found")
    return out


def _load_stock_pool() -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, name
        FROM stocks
        WHERE code != ''
        """
    )
    if df.empty:
        return pd.DataFrame(columns=["code_raw", "code6", "stock_name"])
    df["code_raw"] = df["code"].astype(str)
    df["code6"] = df["code"].map(_code6)
    df["stock_name"] = df["name"].astype(str)
    return df[["code_raw", "code6", "stock_name"]].drop_duplicates("code6")


def _add_stock_acceptance_features(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy()
    d = d.sort_values(["code6", "trade_date"]).reset_index(drop=True)
    g = d.groupby("code6", group_keys=False)
    d["ma20"] = g["close"].rolling(20, min_periods=15).mean().reset_index(level=0, drop=True)
    d["ma60"] = g["close"].rolling(60, min_periods=40).mean().reset_index(level=0, drop=True)
    d["low60"] = g["low"].rolling(60, min_periods=40).min().reset_index(level=0, drop=True)
    d["amount5"] = g["amount"].rolling(5, min_periods=3).mean().reset_index(level=0, drop=True)
    d["amount20"] = g["amount"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
    d["mom20"] = g["close"].pct_change(20)
    d["mom60"] = g["close"].pct_change(60)
    d["amount_ratio5_20"] = d["amount5"] / d["amount20"].replace(0, np.nan)
    d["runup_from_60d_low"] = d["close"] / d["low60"].replace(0, np.nan) - 1.0
    d["acceptance"] = (
        (d["close"] >= d["ma20"])
        & (d["ma20"] >= d["ma60"])
        & (d["mom20"] >= 0.03)
        & (d["amount_ratio5_20"].between(1.05, 2.80))
        & (d["runup_from_60d_low"].between(0.05, 1.20))
    )
    d["acceptance_score"] = (
        25.0 * ((d["mom20"] - 0.03) / 0.25).clip(0.0, 1.0).fillna(0.0)
        + 20.0 * ((d["amount_ratio5_20"] - 1.05) / 1.00).clip(0.0, 1.0).fillna(0.0)
        + 20.0 * ((d["close"] / d["ma20"].replace(0, np.nan) - 1.0) / 0.12).clip(0.0, 1.0).fillna(0.0)
        + 20.0 * ((d["ma20"] / d["ma60"].replace(0, np.nan) - 1.0) / 0.12).clip(0.0, 1.0).fillna(0.0)
        + 15.0 * (1.0 - ((d["runup_from_60d_low"] - 0.30) / 0.90).clip(0.0, 1.0).fillna(0.0))
    )
    return d


def _forward_return_columns(daily: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    d = daily.copy()
    g = d.groupby("code6", group_keys=False)
    for horizon in horizons:
        d[f"fwd_ret_{horizon}"] = g["close"].shift(-horizon) / d["close"] - 1.0
    return d


def _summarize(label: str, sample: pd.DataFrame, horizons: list[int], year: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for horizon in horizons:
        col = f"fwd_ret_{horizon}"
        vals = pd.to_numeric(sample.get(col), errors="coerce").dropna()
        rows.append(
            {
                "year": year,
                "group": label,
                "horizon": int(horizon),
                "count": int(len(vals)),
                "avg": _safe_float(vals.mean()),
                "median": _safe_float(vals.median()),
                "win_rate": _safe_float((vals > 0).mean()),
                "best": _safe_float(vals.max()),
                "worst": _safe_float(vals.min()),
            }
        )
    return rows


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

    top_sector_codes = set(window["top_sectors"])
    top_members = members[members["sector_code"].isin(top_sector_codes)].copy()
    top_codes = set(top_members["stock_code6"].astype(str))
    day["in_mainline_sector"] = day["code6"].astype(str).isin(top_codes)
    day = day.merge(stock_pool, on="code6", how="left")

    market_all = day.copy()
    mainline_all = day[day["in_mainline_sector"]].copy()
    market_accept = day[day["acceptance"]].copy()
    mainline_accept = day[day["in_mainline_sector"] & day["acceptance"]].copy()

    summary: list[dict[str, Any]] = []
    summary.extend(_summarize("market_all", market_all, horizons, int(window["year"])))
    summary.extend(_summarize("mainline_sector_all", mainline_all, horizons, int(window["year"])))
    summary.extend(_summarize("market_acceptance", market_accept, horizons, int(window["year"])))
    summary.extend(_summarize("mainline_sector_acceptance", mainline_accept, horizons, int(window["year"])))

    detail = mainline_accept.sort_values("acceptance_score", ascending=False).head(80).copy()
    detail["year"] = int(window["year"])
    detail["anchor_date"] = str(window["anchor_date"])
    detail["top_sector_codes"] = ",".join(window["top_sectors"])
    keep = [
        "year",
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


def _comparison(summary: pd.DataFrame) -> pd.DataFrame:
    key = ["year", "horizon"]
    wide = summary.pivot_table(index=key, columns="group", values="avg", aggfunc="first").reset_index()
    for left, right, name in [
        ("mainline_sector_acceptance", "market_acceptance", "mainline_accept_minus_market_accept"),
        ("mainline_sector_acceptance", "mainline_sector_all", "accept_minus_sector_all"),
        ("mainline_sector_all", "market_all", "sector_all_minus_market_all"),
    ]:
        if left in wide.columns and right in wide.columns:
            wide[name] = wide[left] - wide[right]
    return wide


def _write_outputs(result: dict[str, Any], summary: pd.DataFrame, comparison: pd.DataFrame, detail: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = OUT_DIR / "summary.csv"
    comparison_path = OUT_DIR / "comparison.csv"
    detail_path = OUT_DIR / "mainline_acceptance_examples.csv"
    json_path = OUT_DIR / "summary.json"
    report_path = OUT_DIR / "REPORT.md"

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")

    result = dict(result)
    result.update(
        {
            "summary_csv": str(summary_path),
            "comparison_csv": str(comparison_path),
            "detail_csv": str(detail_path),
            "report": str(report_path),
        }
    )
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

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
        "# 主线窗口 + 个股承接 V1 验证",
        "",
        f"- 数据窗口：`{result['start_date']}` 到 `{result['end_date']}`",
        f"- 真板块指数来源：`{SOURCE}`",
        f"- 板块层级：L{result['level']}，年度：`{result['years']}`，每年取前 `{result['top_n']}` 个早期强势板块",
        "- 个股承接定义：收盘站上 MA20，MA20 站上 MA60，20日动量为正，5/20日成交额放大，且相对60日低点不过度极端。",
        "- 注意：`sector_stocks` 是当前成分映射，不是 point-in-time 历史成分，存在幸存者和重分类偏差；本报告只用于研究方向判断。",
        "",
        "## 跨年度平均结果",
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
            "## 年度差值：主线承接股 - 全市场承接股",
            "",
            "| 年份 | 持有20日 | 持有60日 | 持有120日 |",
            "|---:|---:|---:|---:|",
        ]
    )
    cmp = comparison.copy()
    for year, g in cmp.groupby("year"):
        values = {}
        for h in [20, 60, 120]:
            x = g[g["horizon"].eq(h)]["mainline_accept_minus_market_accept"]
            values[h] = x.iloc[0] if len(x) else None
        lines.append(f"| {int(year)} | {_pct(values[20])} | {_pct(values[60])} | {_pct(values[120])} |")

    lines.extend(
        [
            "",
            "## 初步结论",
            "",
            "- 如果主线承接股显著跑赢全市场承接股，说明“先看主线窗口，再在板块内选承接股”有独立价值。",
            "- 如果只跑赢主线全体股，但跑不赢全市场承接股，说明个股承接有效，板块窗口没有明显增益。",
            "- 如果长期窗口弱于短期窗口，说明该方法更像波段启动过滤，不是完整主升浪持有系统。",
            "",
            f"- 明细样本：`{detail_path}`",
            f"- 对比表：`{comparison_path}`",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(comparison_path))
    print(str(detail_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate true-index mainline window plus stock acceptance.")
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--years", default="2020-2025")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--horizons", default="20,60,120")
    parser.add_argument("--min-members", type=int, default=5)
    parser.add_argument("--lookback-calendar-days", type=int, default=180)
    args = parser.parse_args()

    years = _parse_years(args.years)
    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    windows = _load_windows(args.level, years, args.top_n)
    start_date = (pd.Timestamp(min(x["anchor_date"] for x in windows)) - pd.Timedelta(days=args.lookback_calendar_days)).strftime("%Y-%m-%d")
    end_date = max(x["last_trade_date"] for x in windows)

    members = _load_members([args.level], args.min_members)
    stock_pool = _load_stock_pool()
    daily = _load_daily(start_date, end_date)
    daily = _forward_return_columns(_add_stock_acceptance_features(daily), horizons)

    all_summary: list[dict[str, Any]] = []
    details: list[pd.DataFrame] = []
    for window in windows:
        window_summary, detail = _window_samples(daily, members, stock_pool, window, horizons)
        all_summary.extend(window_summary)
        if not detail.empty:
            details.append(detail)

    summary = pd.DataFrame(all_summary)
    comparison = _comparison(summary)
    detail = pd.concat(details, ignore_index=True) if details else pd.DataFrame()
    result = {
        "level": args.level,
        "years": years,
        "top_n": args.top_n,
        "horizons": horizons,
        "start_date": start_date,
        "end_date": end_date,
        "window_count": len(windows),
        "daily_rows": int(len(daily)),
        "member_rows": int(len(members)),
        "stock_pool_rows": int(len(stock_pool)),
    }
    _write_outputs(result, summary, comparison, detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
