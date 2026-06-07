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

from data_fetcher.sources.tdxquant_pool import tdxquant_pool
from scripts.research_main_wave_sector_score import _load_members
from scripts.validate_true_sector_index_intraday_diffusion_v1 import (
    _confirm_diffusion,
    _load_intraday_day,
    _morning_stock_returns,
    _sector_diffusion_stats,
)
from scripts.validate_true_sector_index_quarterly_stock_acceptance_v1 import _load_sectors
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "mainline_intraday_diffusion_factor"

THRESHOLDS = [
    {"name": "loose", "min_rise_ratio": 0.52, "min_strong2_ratio": 0.04, "min_avg_ret": 0.004},
    {"name": "base", "min_rise_ratio": 0.55, "min_strong2_ratio": 0.06, "min_avg_ret": 0.006},
    {"name": "strict", "min_rise_ratio": 0.60, "min_strong2_ratio": 0.08, "min_avg_ret": 0.008},
]


def _latest_complete_intraday_date(table: str, min_codes: int) -> str:
    df = clickhouse_query_df(
        f"""
        SELECT toDate(datetime) AS d, uniq(code) AS codes
        FROM {table}
        WHERE datetime >= now() - INTERVAL 45 DAY
        GROUP BY d
        HAVING codes >= ?
        ORDER BY d DESC
        LIMIT 1
        """,
        [int(min_codes)],
    )
    if df.empty:
        raise RuntimeError(f"no complete recent intraday date in {table}")
    return pd.to_datetime(df["d"].iloc[0]).strftime("%Y-%m-%d")


def _fetch_sector_close_tdxquant(
    sectors: pd.DataFrame,
    target_date: str,
    lookback_calendar_days: int,
    batch_size: int,
) -> pd.DataFrame:
    start_date = (pd.Timestamp(target_date) - pd.Timedelta(days=int(lookback_calendar_days))).strftime("%Y-%m-%d")
    codes = sectors["code"].astype(str).tolist()
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), int(batch_size)):
        batch = codes[i : i + int(batch_size)]
        data = tdxquant_pool.get_market_data(
            field_list=[],
            stock_list=batch,
            period="1d",
            start_time=start_date.replace("-", ""),
            end_time=target_date.replace("-", ""),
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
        long = close.reset_index().rename(columns={"index": "trade_date"}).melt(
            id_vars=["trade_date"],
            var_name="sector_code",
            value_name="close",
        )
        long["close"] = pd.to_numeric(long["close"], errors="coerce")
        long = long.dropna(subset=["trade_date", "sector_code", "close"])
        parts.append(long)
    if not parts:
        raise RuntimeError("TdxQuant returned no sector close data")
    out = pd.concat(parts, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out.dropna(subset=["trade_date"]).drop_duplicates(["trade_date", "sector_code"])
    names = sectors.rename(columns={"code": "sector_code", "name": "sector_name"})
    return out.merge(names, on="sector_code", how="left")


def _fetch_sector_close_clickhouse_fallback(
    sectors: pd.DataFrame,
    target_date: str,
    lookback_calendar_days: int,
) -> pd.DataFrame:
    """Research-only fallback when TdxQuant is temporarily unavailable."""
    start_date = (pd.Timestamp(target_date) - pd.Timedelta(days=int(lookback_calendar_days))).strftime("%Y-%m-%d")
    df = clickhouse_query_df(
        """
        SELECT
            code AS sector_code,
            trade_date,
            change_pct
        FROM sector_kline_daily
        WHERE trade_date >= ?
          AND trade_date <= ?
        ORDER BY sector_code, trade_date
        """,
        [start_date, target_date],
    )
    if df.empty:
        raise RuntimeError("TdxQuant unavailable and sector_kline_daily fallback has no data")
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["sector_code"] = df["sector_code"].astype(str)
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0.0)
    df["daily_ret"] = df["change_pct"]
    if df["daily_ret"].abs().median() > 1.0:
        df["daily_ret"] = df["daily_ret"] / 100.0
    df["daily_ret"] = df["daily_ret"].clip(-0.30, 0.30)
    df["close"] = 1000.0 * (1.0 + df["daily_ret"]).groupby(df["sector_code"]).cumprod()
    names = sectors.rename(columns={"code": "sector_code", "name": "sector_name"}).copy()
    names["sector_code"] = names["sector_code"].astype(str)
    out = df[["trade_date", "sector_code", "close"]].merge(names, on="sector_code", how="left")
    out["sector_name"] = out["sector_name"].fillna(out["sector_code"])
    return out


def _true_index_window(
    index_daily: pd.DataFrame,
    target_date: str,
    top_n: int,
    min_ret60: float,
    max_ret60: float,
) -> pd.DataFrame:
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
    d["date_text"] = d["trade_date"].dt.strftime("%Y-%m-%d")
    target = d[d["date_text"].eq(target_date)].copy()
    if target.empty:
        raise RuntimeError(f"sector index has no target date {target_date}")
    picked = target[
        (target["ret60"].between(float(min_ret60), float(max_ret60)))
        & (target["relative_ret60"] > 0)
        & (target["rank_ret60_pct"] >= 0.75)
        & (target["ma20"] >= target["ma60"])
    ].copy()
    if picked.empty:
        return picked
    picked["true_index_window_score"] = (
        40.0 * ((picked["rank_ret60_pct"] - 0.75) / 0.25).clip(0.0, 1.0).fillna(0.0)
        + 35.0 * (picked["relative_ret60"] / 0.18).clip(0.0, 1.0).fillna(0.0)
        + 25.0 * (picked["ret20"] / 0.18).clip(0.0, 1.0).fillna(0.0)
    )
    return picked.sort_values(["true_index_window_score", "rank_ret60_pct"], ascending=False).head(int(top_n))


def _recent_fallback_window(index_daily: pd.DataFrame, target_date: str, top_n: int) -> pd.DataFrame:
    d = index_daily.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["trade_date", "sector_code", "close"]).sort_values(["sector_code", "trade_date"])
    max_count = int(d.groupby("sector_code")["trade_date"].count().max() or 0)
    period = min(20, max_count - 1)
    if period < 5:
        return pd.DataFrame()
    short_period = max(3, period // 2)
    g = d.groupby("sector_code", group_keys=False)
    d["ret20"] = g["close"].pct_change(short_period)
    d["ret60"] = g["close"].pct_change(period)
    d["ma20"] = g["close"].rolling(short_period, min_periods=max(3, short_period - 2)).mean().reset_index(level=0, drop=True)
    d["ma60"] = g["close"].rolling(period, min_periods=max(5, period - 3)).mean().reset_index(level=0, drop=True)
    market = d.groupby("trade_date", as_index=False).agg(market_ret60=("ret60", "mean"))
    d = d.merge(market, on="trade_date", how="left")
    d["relative_ret60"] = d["ret60"] - d["market_ret60"]
    d["rank_ret60_pct"] = d.groupby("trade_date")["ret60"].rank(pct=True, ascending=True)
    d["date_text"] = d["trade_date"].dt.strftime("%Y-%m-%d")
    target = d[d["date_text"].eq(target_date)].copy()
    if target.empty:
        return pd.DataFrame()
    picked = target[
        (target["ret60"] > 0)
        & (target["relative_ret60"] > 0)
        & (target["rank_ret60_pct"] >= 0.75)
        & (target["ma20"] >= target["ma60"])
    ].copy()
    if picked.empty:
        return picked
    picked["true_index_window_score"] = (
        45.0 * ((picked["rank_ret60_pct"] - 0.75) / 0.25).clip(0.0, 1.0).fillna(0.0)
        + 35.0 * (picked["relative_ret60"] / 0.12).clip(0.0, 1.0).fillna(0.0)
        + 20.0 * (picked["ret20"] / 0.10).clip(0.0, 1.0).fillna(0.0)
    )
    picked["window_period"] = period
    return picked.sort_values(["true_index_window_score", "rank_ret60_pct"], ascending=False).head(int(top_n))


def _classify_threshold(row: pd.Series) -> str:
    passed = []
    for threshold in THRESHOLDS:
        if (
            float(row.get("morning_rise_ratio", 0) or 0) >= threshold["min_rise_ratio"]
            and float(row.get("morning_strong2_ratio", 0) or 0) >= threshold["min_strong2_ratio"]
            and float(row.get("morning_ret_avg", 0) or 0) >= threshold["min_avg_ret"]
        ):
            passed.append(threshold["name"])
    if "strict" in passed:
        return "strict"
    if "base" in passed:
        return "base"
    if "loose" in passed:
        return "loose"
    return ""


def _format_pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if math.isnan(x) or math.isinf(x):
        return ""
    return f"{x:.2%}"


def _write_outputs(result: dict[str, Any], factor: pd.DataFrame, window: pd.DataFrame, diffusion: pd.DataFrame) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    factor_path = OUT_DIR / "mainline_intraday_diffusion_factor.csv"
    window_path = OUT_DIR / "true_index_window_sectors.csv"
    diffusion_path = OUT_DIR / "intraday_diffusion_stats.csv"
    json_path = OUT_DIR / "summary.json"
    report_path = OUT_DIR / "REPORT.md"
    factor_columns = [
        "sector_name",
        "sector_code",
        "threshold_level",
        "mainline_intraday_score",
        "true_index_window_score",
        "intraday_diffusion_score",
        "ret60",
        "relative_ret60",
        "morning_rise_ratio",
        "morning_strong2_ratio",
        "morning_ret_avg",
    ]
    if factor.empty:
        factor = pd.DataFrame(columns=factor_columns)
    factor.to_csv(factor_path, index=False, encoding="utf-8-sig")
    window.to_csv(window_path, index=False, encoding="utf-8-sig")
    diffusion.to_csv(diffusion_path, index=False, encoding="utf-8-sig")
    payload = dict(result)
    payload.update(
        {
            "factor_csv": str(factor_path),
            "window_csv": str(window_path),
            "diffusion_csv": str(diffusion_path),
            "report": str(report_path),
        }
    )
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 主线盘中扩散因子 research_only",
        "",
        f"- 目标日期：`{result['target_date']}`",
        f"- 分钟表：`{result['table']}`，可见截止：`{result['cutoff_time']}`",
        f"- 板块层级：L{result['level']}",
        f"- 板块指数来源：`{result['index_source']}`",
        "- 用途：仅作 G2/V4 环境加分、候选池倾斜、仓位上限调节的研究因子，不是独立买点。",
        f"- 窗口模式：`{result['window_mode']}`",
        "- 历史依据：前面的验证显示 60/120 日更稳定，20 日不稳定，因此不得直接转成短线买入触发。",
        "",
        "## 当前确认板块",
        "",
        "| 板块 | 阈值 | 综合分 | 指数窗口分 | 扩散分 | 60日涨幅 | 60日相对强度 | 上午上涨比例 | 上午强2比例 | 上午均涨 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in factor.sort_values("mainline_intraday_score", ascending=False).itertuples(index=False):
        lines.append(
            f"| {row.sector_name} `{row.sector_code}` | {row.threshold_level} | "
            f"{float(row.mainline_intraday_score):.2f} | {float(row.true_index_window_score):.2f} | "
            f"{float(row.intraday_diffusion_score):.2f} | {_format_pct(row.ret60)} | "
            f"{_format_pct(row.relative_ret60)} | {_format_pct(row.morning_rise_ratio)} | "
            f"{_format_pct(row.morning_strong2_ratio)} | {_format_pct(row.morning_ret_avg)} |"
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- `true_index_window_score`：板块指数 60 日强度、相对强度、20 日延续强度。",
            "- `intraday_diffusion_score`：目标日上午板块内个股上涨比例、2%以上强势比例、上午平均涨幅、弱势占比。",
            "- `threshold_level`：loose/base/strict；strict 更适合作为中期主线环境，base 更适合作为观察提示。",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(factor_path))
    print(f"factor_count={len(factor)}")
    if not factor.empty:
        cols = ["sector_name", "sector_code", "threshold_level", "mainline_intraday_score"]
        print(factor.sort_values("mainline_intraday_score", ascending=False)[cols].head(10).to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate research-only mainline intraday diffusion factor.")
    parser.add_argument("--target-date", default=None)
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--table", default="kline_minute_60")
    parser.add_argument("--cutoff-time", default="11:30:00")
    parser.add_argument("--min-complete-codes", type=int, default=3000)
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--lookback-calendar-days", type=int, default=150)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--min-ret60", type=float, default=0.02)
    parser.add_argument("--max-ret60", type=float, default=0.80)
    parser.add_argument("--min-visible-members", type=int, default=15)
    args = parser.parse_args()

    target_date = args.target_date or _latest_complete_intraday_date(args.table, args.min_complete_codes)
    sectors = _load_sectors(args.level)
    index_source = "tdxquant_sector_index"
    try:
        index_daily = _fetch_sector_close_tdxquant(sectors, target_date, args.lookback_calendar_days, args.batch_size)
    except Exception as exc:
        index_source = f"clickhouse_sector_kline_daily_fallback; tdxquant_error={exc}"
        index_daily = _fetch_sector_close_clickhouse_fallback(sectors, target_date, args.lookback_calendar_days)

    window_mode = "true_index_60d_window"
    window = _true_index_window(index_daily, target_date, args.top_n, args.min_ret60, args.max_ret60)
    if window.empty and index_source.startswith("clickhouse_sector_kline_daily_fallback"):
        window_mode = "fallback_recent_window_observation_only"
        window = _recent_fallback_window(index_daily, target_date, args.top_n)
    members = _load_members([args.level], 5)
    intraday = _load_intraday_day(args.table, target_date)
    morning = _morning_stock_returns(intraday, args.cutoff_time)
    diffusion = _sector_diffusion_stats(
        morning,
        members,
        window[["sector_code", "sector_name"]].copy() if not window.empty else pd.DataFrame(columns=["sector_code", "sector_name"]),
        args.min_visible_members,
    )

    factor = pd.DataFrame()
    if not window.empty and not diffusion.empty:
        base_confirmed = _confirm_diffusion(diffusion, 0.52, 0.04, 0.004)
        factor = window.merge(base_confirmed, on=["sector_code", "sector_name"], how="inner")
        if not factor.empty:
            factor["threshold_level"] = factor.apply(_classify_threshold, axis=1)
            factor = factor[factor["threshold_level"].ne("")].copy()
            factor["mainline_intraday_score"] = (
                0.45 * pd.to_numeric(factor["true_index_window_score"], errors="coerce").fillna(0.0)
                + 0.55 * pd.to_numeric(factor["intraday_diffusion_score"], errors="coerce").fillna(0.0)
            )
            for col in factor.columns:
                if pd.api.types.is_datetime64_any_dtype(factor[col]):
                    factor[col] = factor[col].dt.strftime("%Y-%m-%d")

    export_window = window.copy()
    for col in export_window.columns:
        if pd.api.types.is_datetime64_any_dtype(export_window[col]):
            export_window[col] = export_window[col].dt.strftime("%Y-%m-%d")

    result = {
        "target_date": target_date,
        "level": args.level,
        "table": args.table,
        "cutoff_time": args.cutoff_time,
        "research_only": True,
        "index_source": index_source,
        "window_mode": window_mode,
        "sector_count": int(len(sectors)),
        "index_rows": int(len(index_daily)),
        "true_index_window_count": int(len(window)),
        "diffusion_rows": int(len(diffusion)),
        "factor_count": int(len(factor)),
        "method_note": "真实板块指数窗口 + 上午盘中扩散；仅用于研究型环境加分，不是买点。若 index_source 为 fallback，则只代表当前研究观察，不代表历史验证口径。",
    }
    _write_outputs(result, factor, export_window, diffusion)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
