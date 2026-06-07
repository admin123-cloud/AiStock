from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df, clickhouse_scalar


OUT_DIR = ROOT / "reports" / "main_wave_sector_score"


@dataclass(frozen=True)
class ScoreWeights:
    relative_strength: float = 25.0
    breadth: float = 20.0
    accumulation: float = 20.0
    amount_trend: float = 15.0
    leader_chain: float = 10.0
    market_env: float = 10.0


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


def _clip01(value: Any) -> float:
    try:
        x = float(value)
    except Exception:
        return 0.0
    if math.isnan(x) or math.isinf(x):
        return 0.0
    return float(max(0.0, min(1.0, x)))


def _score_between(value: Any, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    try:
        x = float(value)
    except Exception:
        return 0.0
    if math.isnan(x) or math.isinf(x):
        return 0.0
    return _clip01((x - low) / (high - low))


def _score_inverse(value: Any, low: float, high: float) -> float:
    return 1.0 - _score_between(value, low, high)


def _compound(values: pd.Series) -> float:
    vals = pd.to_numeric(values, errors="coerce").dropna()
    if vals.empty:
        return float("nan")
    return float((1.0 + vals).prod() - 1.0)


def _code6(value: Any) -> str:
    return str(value or "").split(".")[0].zfill(6)[-6:]


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
    return sorted(set(levels)) or [1, 2, 3]


def _latest_trade_date() -> str:
    value = clickhouse_scalar("SELECT max(trade_date) FROM kline_daily")
    if value is None:
        raise RuntimeError("kline_daily has no trade_date")
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _load_trade_dates(target_date: str, lookback_days: int) -> list[str]:
    df = clickhouse_query_df(
        """
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        [target_date, int(lookback_days)],
    )
    if df.empty:
        raise RuntimeError(f"no trade dates found before {target_date}")
    dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna().sort_values()
    return [d.strftime("%Y-%m-%d") for d in dates]


def _load_members(levels: list[int], min_members: int) -> pd.DataFrame:
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
        raise RuntimeError("sector_stocks/sectors has no industry membership")
    df["stock_code_raw"] = df["stock_code"].astype(str)
    df["stock_code6"] = df["stock_code"].map(_code6)
    df["level"] = pd.to_numeric(df["level"], errors="coerce").astype("Int64")
    df["stock_count"] = pd.to_numeric(df["stock_count"], errors="coerce")
    counts = df.groupby(["level", "sector_code"])["stock_code"].nunique().rename("member_count").reset_index()
    df = df.merge(counts, on=["level", "sector_code"], how="left")
    df = df[pd.to_numeric(df["member_count"], errors="coerce").fillna(0) >= int(min_members)].copy()
    if df.empty:
        raise RuntimeError("no sector membership left after min_members filter")
    return df.drop_duplicates(["stock_code_raw", "sector_code", "level"]).reset_index(drop=True)


def _load_daily(start_date: str, target_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, trade_date, open, high, low, close, volume, amount, change_pct
        FROM kline_daily
        WHERE trade_date BETWEEN ? AND ?
          AND close > 0
          AND change_pct > -50
          AND change_pct < 50
        ORDER BY code, trade_date
        """,
        [start_date, target_date],
    )
    if df.empty:
        raise RuntimeError("kline_daily query returned no stock rows")
    df["code_raw"] = df["code"].astype(str)
    df["code6"] = df["code"].map(_code6)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount", "change_pct"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "close", "change_pct"]).copy()
    df["ret"] = df["change_pct"] / 100.0
    return df.sort_values(["code6", "trade_date"]).reset_index(drop=True)


def _add_stock_features(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy()
    g = d.groupby("code6", group_keys=False)
    d["ma20"] = g["close"].rolling(20, min_periods=15).mean().reset_index(level=0, drop=True)
    d["ma60"] = g["close"].rolling(60, min_periods=40).mean().reset_index(level=0, drop=True)
    d["high60"] = g["close"].rolling(60, min_periods=40).max().reset_index(level=0, drop=True)
    d["mom20"] = g["close"].pct_change(20)
    d["mom60"] = g["close"].pct_change(60)
    d["amount5"] = g["amount"].rolling(5, min_periods=3).mean().reset_index(level=0, drop=True)
    d["amount20"] = g["amount"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
    d["above_ma20"] = d["close"] >= d["ma20"]
    d["above_ma60"] = d["close"] >= d["ma60"]
    d["new_high60"] = d["close"] >= d["high60"]
    d["stock_amount_ratio5_20"] = d["amount5"] / d["amount20"].replace(0, np.nan)
    return d


def _build_sector_daily(daily: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    merged = daily.merge(
        members,
        left_on="code6",
        right_on="stock_code6",
        how="inner",
        validate="many_to_many",
    )
    if merged.empty:
        raise RuntimeError("daily rows and sector members have no overlap")
    merged["rise"] = merged["ret"] > 0
    merged["strong3"] = merged["ret"] >= 0.03
    keys = ["level", "sector_code", "sector_name", "trade_date"]
    agg = merged.groupby(keys, as_index=False).agg(
        member_bars=("code6", "nunique"),
        sector_ret=("ret", "mean"),
        median_ret=("ret", "median"),
        amount=("amount", "sum"),
        rise_ratio=("rise", "mean"),
        strong3_ratio=("strong3", "mean"),
        ma20_ratio=("above_ma20", "mean"),
        ma60_ratio=("above_ma60", "mean"),
        new_high60_ratio=("new_high60", "mean"),
        stock_mom20_median=("mom20", "median"),
        stock_mom60_median=("mom60", "median"),
        amount_ratio5_20_median=("stock_amount_ratio5_20", "median"),
    )
    top5 = (
        merged.dropna(subset=["mom20"])
        .sort_values(keys + ["mom20"], ascending=[True, True, True, True, False])
        .groupby(keys, as_index=False)
        .head(5)
        .groupby(keys, as_index=False)
        .agg(leader_mom20_top5=("mom20", "mean"), leader_mom60_top5=("mom60", "mean"))
    )
    leader_count = (
        merged.assign(leader_20pct=merged["mom20"] >= 0.20)
        .groupby(keys, as_index=False)
        .agg(leader_20pct_count=("leader_20pct", "sum"))
    )
    agg = agg.merge(top5, on=keys, how="left").merge(leader_count, on=keys, how="left")
    return agg.sort_values(["level", "sector_code", "trade_date"]).reset_index(drop=True)


def _add_sector_feature_frame(sector_daily: pd.DataFrame) -> pd.DataFrame:
    d = sector_daily.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    d = d.dropna(subset=["trade_date"]).sort_values(["level", "sector_code", "trade_date"]).copy()
    d["sector_nav"] = d.groupby(["level", "sector_code"])["sector_ret"].transform(lambda s: (1.0 + s.fillna(0.0)).cumprod())
    group = d.groupby(["level", "sector_code"], group_keys=False)
    for win in [5, 10, 20, 60]:
        d[f"ret{win}"] = group["sector_ret"].rolling(win, min_periods=max(3, win // 2)).apply(_compound, raw=False).reset_index(level=[0, 1], drop=True)
    d["amount5"] = group["amount"].rolling(5, min_periods=3).mean().reset_index(level=[0, 1], drop=True)
    d["amount20"] = group["amount"].rolling(20, min_periods=10).mean().reset_index(level=[0, 1], drop=True)
    d["amount60"] = group["amount"].rolling(60, min_periods=30).mean().reset_index(level=[0, 1], drop=True)
    d["amount_ratio5_20"] = d["amount5"] / d["amount20"].replace(0, np.nan)
    d["amount_ratio20_60"] = d["amount20"] / d["amount60"].replace(0, np.nan)
    d["pre_nav_max_60_20"] = group["sector_nav"].transform(lambda s: s.shift(20).rolling(40, min_periods=20).max())
    d["pre_nav_min_60_20"] = group["sector_nav"].transform(lambda s: s.shift(20).rolling(40, min_periods=20).min())
    d["pre_range_60_20"] = d["pre_nav_max_60_20"] / d["pre_nav_min_60_20"].replace(0, np.nan) - 1.0
    d["breakout_from_pre_high"] = d["sector_nav"] / d["pre_nav_max_60_20"].replace(0, np.nan) - 1.0
    d["rank_ret20_pct"] = d.groupby(["level", "trade_date"])["ret20"].rank(pct=True, ascending=True)
    d["rank_ret60_pct"] = d.groupby(["level", "trade_date"])["ret60"].rank(pct=True, ascending=True)
    d["top20_flag"] = d["rank_ret20_pct"] >= 0.80
    d["top20_persistence10"] = (
        d.groupby(["level", "sector_code"])["top20_flag"]
        .rolling(10, min_periods=5)
        .mean()
        .reset_index(level=[0, 1], drop=True)
    )

    market = (
        d.groupby("trade_date", as_index=False)
        .agg(
            market_ret=("sector_ret", "mean"),
            market_amount=("amount", "sum"),
            market_rise_ratio=("rise_ratio", "mean"),
            market_ma20_ratio=("ma20_ratio", "mean"),
        )
        .sort_values("trade_date")
    )
    for win in [20, 60]:
        market[f"market_ret{win}"] = market["market_ret"].rolling(win, min_periods=max(5, win // 2)).apply(_compound, raw=False)
    d = d.merge(market[["trade_date", "market_ret20", "market_ret60", "market_rise_ratio", "market_ma20_ratio"]], on="trade_date", how="left")
    d["relative_ret20"] = d["ret20"] - d["market_ret20"]
    d["relative_ret60"] = d["ret60"] - d["market_ret60"]
    return d.reset_index(drop=True)


def _add_sector_features(sector_daily: pd.DataFrame, target_date: str) -> pd.DataFrame:
    d = _add_sector_feature_frame(sector_daily)
    target = d[d["trade_date"].dt.strftime("%Y-%m-%d") == target_date].copy()
    if target.empty:
        raise RuntimeError(f"target date {target_date} has no sector rows")
    return target.reset_index(drop=True)


def _score_rows(target: pd.DataFrame) -> pd.DataFrame:
    weights = ScoreWeights()
    rows: list[dict[str, Any]] = []
    for row in target.itertuples(index=False):
        rs = (
            0.35 * _score_between(row.rank_ret20_pct, 0.50, 0.95)
            + 0.25 * _score_between(row.rank_ret60_pct, 0.45, 0.90)
            + 0.25 * _score_between(row.relative_ret20, 0.00, 0.10)
            + 0.15 * _score_between(row.top20_persistence10, 0.20, 0.80)
        )
        breadth = (
            0.25 * _score_between(row.rise_ratio, 0.45, 0.75)
            + 0.20 * _score_between(row.strong3_ratio, 0.03, 0.18)
            + 0.25 * _score_between(row.ma20_ratio, 0.45, 0.80)
            + 0.15 * _score_between(row.ma60_ratio, 0.35, 0.70)
            + 0.15 * _score_between(row.stock_mom20_median, 0.00, 0.12)
        )
        accumulation = (
            0.35 * _score_inverse(row.pre_range_60_20, 0.12, 0.40)
            + 0.25 * _score_between(row.breakout_from_pre_high, -0.02, 0.08)
            + 0.20 * _score_between(row.relative_ret60, -0.03, 0.08)
            + 0.20 * _score_between(row.ma60_ratio, 0.35, 0.70)
        )
        amount_trend = (
            0.45 * _score_between(row.amount_ratio5_20, 0.95, 1.80)
            + 0.30 * _score_between(row.amount_ratio20_60, 0.90, 1.60)
            + 0.25 * _score_between(row.amount_ratio5_20_median, 0.90, 1.60)
        )
        leader_chain = (
            0.40 * _score_between(row.leader_mom20_top5, 0.12, 0.45)
            + 0.25 * _score_between(row.leader_mom60_top5, 0.15, 0.70)
            + 0.20 * _score_between(row.leader_20pct_count / max(float(row.member_bars or 1), 1.0), 0.03, 0.16)
            + 0.15 * _score_between(row.new_high60_ratio, 0.02, 0.18)
        )
        market_env = (
            0.40 * _score_between(row.market_ret20, -0.02, 0.08)
            + 0.35 * _score_between(row.market_ma20_ratio, 0.45, 0.70)
            + 0.25 * _score_between(row.market_rise_ratio, 0.45, 0.65)
        )
        overheat_penalty = 0.0
        overheat_penalty += 6.0 * _score_between(row.ret5, 0.10, 0.20)
        overheat_penalty += 4.0 * _score_between(row.amount_ratio5_20, 1.80, 3.00)
        overheat_penalty += 4.0 * _score_between(row.strong3_ratio, 0.20, 0.45)
        overheat_penalty += 4.0 * _score_between(row.sector_ret, 0.04, 0.08)

        total = (
            weights.relative_strength * rs
            + weights.breadth * breadth
            + weights.accumulation * accumulation
            + weights.amount_trend * amount_trend
            + weights.leader_chain * leader_chain
            + weights.market_env * market_env
            - overheat_penalty
        )
        bucket = "主升浪候选"
        if total >= 80:
            bucket = "主升浪确认"
        elif total >= 65:
            bucket = "主升浪候选"
        elif total >= 50:
            bucket = "普通轮动"
        else:
            bucket = "暂不主攻"

        item = row._asdict()
        item.update(
            {
                "relative_strength_score": weights.relative_strength * rs,
                "breadth_score": weights.breadth * breadth,
                "accumulation_score": weights.accumulation * accumulation,
                "amount_trend_score": weights.amount_trend * amount_trend,
                "leader_chain_score": weights.leader_chain * leader_chain,
                "market_env_score": weights.market_env * market_env,
                "overheat_penalty": overheat_penalty,
                "main_wave_score": max(0.0, min(100.0, total)),
                "main_wave_bucket": bucket,
            }
        )
        rows.append(item)
    out = pd.DataFrame(rows)
    return out.sort_values(["main_wave_score", "relative_strength_score"], ascending=False).reset_index(drop=True)


def _format_pct(value: Any) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


def _write_outputs(scored: pd.DataFrame, args: argparse.Namespace, summary: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / "main_wave_sector_scores.csv"
    json_path = OUT_DIR / "summary.json"
    md_path = OUT_DIR / "REPORT.md"

    export = scored.copy()
    for col in export.columns:
        if pd.api.types.is_datetime64_any_dtype(export[col]):
            export[col] = export[col].dt.strftime("%Y-%m-%d")
    export.to_csv(csv_path, index=False, encoding="utf-8-sig")

    top = export.head(int(args.top)).copy()
    result = {
        "target_date": args.target_date,
        "levels": args.levels,
        "lookback_days": args.lookback_days,
        "min_members": args.min_members,
        "input": summary,
        "top": [
            {k: _safe_float(v) if isinstance(v, (float, np.floating)) else v for k, v in row.items()}
            for row in top.to_dict(orient="records")
        ],
        "output_csv": str(csv_path),
        "output_report": str(md_path),
        "method_note": "当前成分 sector_stocks + 个股日线等权合成板块表现，适合研究主线识别；不是 point-in-time 历史成分，不能直接作为实盘定论。",
    }
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        "# 主升浪板块识别评分",
        "",
        f"- 目标日期：`{args.target_date}`",
        f"- 板块层级：`{args.levels}`",
        f"- 输入股票日线：`{summary['daily_rows']}` 行，板块日线：`{summary['sector_daily_rows']}` 行",
        "- 口径说明：使用当前 `sector_stocks` 成分映射和 `kline_daily` 个股日线等权合成板块，存在历史成分幸存者/重分类偏差。",
        "- 使用方式：优先看 `main_wave_score` 排名前列且 `main_wave_bucket` 为“主升浪确认/主升浪候选”的方向，再进入板块内选股。",
        "",
        "## Top 板块",
        "",
        "| 排名 | 层级 | 板块 | 评分 | 状态 | 20日涨幅 | 60日涨幅 | 20日相对强度 | 扩散 | MA20占比 | 5/20量比 | 过热扣分 |",
        "|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for idx, row in enumerate(top.itertuples(index=False), start=1):
        lines.append(
            f"| {idx} | {int(row.level)} | {row.sector_name} `{row.sector_code}` | "
            f"{float(row.main_wave_score):.2f} | {row.main_wave_bucket} | "
            f"{_format_pct(row.ret20)} | {_format_pct(row.ret60)} | {_format_pct(row.relative_ret20)} | "
            f"{_format_pct(row.rise_ratio)} | {_format_pct(row.ma20_ratio)} | "
            f"{_safe_float(row.amount_ratio5_20, 3)} | {float(row.overheat_penalty):.2f} |"
        )
    lines.extend(
        [
            "",
            "## 评分拆解",
            "",
            "| 板块 | 相对强度 | 扩散 | 蓄势 | 量能 | 梯队 | 市场 | 过热 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in top.head(12).itertuples(index=False):
        lines.append(
            f"| {row.sector_name} | {float(row.relative_strength_score):.2f} | "
            f"{float(row.breadth_score):.2f} | {float(row.accumulation_score):.2f} | "
            f"{float(row.amount_trend_score):.2f} | {float(row.leader_chain_score):.2f} | "
            f"{float(row.market_env_score):.2f} | {float(row.overheat_penalty):.2f} |"
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- 相对强度：20日/60日涨幅排名、相对市场涨幅、近10日进入前20%的持续性。",
            "- 扩散：上涨家数、强3比例、站上 MA20/MA60、板块内个股20日动量中位数。",
            "- 蓄势：前60到前20日的窄幅整理、对前期高点的突破、60日相对强度与 MA60 扩散。",
            "- 量能：板块总成交额 5/20、20/60 抬升，以及板块内个股量比中位数。",
            "- 梯队：前5个股20/60日动量、20日涨幅超过20%的个股占比、60日新高扩散。",
            "- 过热：短期涨幅、量能极端、强3扩散过猛、当日板块大涨会扣分，避免把高潮误判为最佳主升入口。",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(md_path))
    print(str(csv_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Research main-wave sector scoring.")
    parser.add_argument("--target-date", default=None, help="YYYY-MM-DD, default=max(kline_daily.trade_date)")
    parser.add_argument("--levels", default="1,2,3", help="sector levels, e.g. 1,2,3")
    parser.add_argument("--lookback-days", type=int, default=140, help="trading days to load")
    parser.add_argument("--min-members", type=int, default=5)
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args()

    args.target_date = args.target_date or _latest_trade_date()
    args.levels = _parse_levels(args.levels)
    trade_dates = _load_trade_dates(args.target_date, args.lookback_days)
    start_date = trade_dates[0]
    members = _load_members(args.levels, args.min_members)
    daily = _load_daily(start_date, args.target_date)
    daily = _add_stock_features(daily)
    sector_daily = _build_sector_daily(daily, members)
    target = _add_sector_features(sector_daily, args.target_date)
    scored = _score_rows(target)
    summary = {
        "start_date": start_date,
        "target_date": args.target_date,
        "member_rows": int(len(members)),
        "daily_rows": int(len(daily)),
        "sector_daily_rows": int(len(sector_daily)),
        "scored_sectors": int(len(scored)),
    }
    _write_outputs(scored, args, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
