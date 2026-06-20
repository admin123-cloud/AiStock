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

from scripts.research_main_wave_sector_score import (  # noqa: E402
    _add_sector_feature_frame,
    _add_stock_features,
    _build_sector_daily,
    _code6,
    _load_daily,
    _load_members,
)
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("gen3_mainline_icepoint_pullback_validation_v1")
INDEX_CODE = "999999.SH"


def _safe_float(value: Any, digits: int = 6) -> Any:
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


def _parse_horizons(text: str) -> list[int]:
    return [int(x.strip()) for x in str(text).split(",") if x.strip()]


def _load_stock_pool() -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, name
        FROM stocks
        WHERE code != ''
          AND type = 'stock'
          AND quit = 0
          AND (st = 0 OR st IS NULL)
        """
    )
    if df.empty:
        return pd.DataFrame(columns=["code_raw", "code6", "stock_name"])
    df["code_raw"] = df["code"].astype(str)
    df["code6"] = df["code"].map(_code6)
    df["stock_name"] = df["name"].astype(str)
    return df[["code_raw", "code6", "stock_name"]].drop_duplicates("code6")


def _load_index(start_date: str, end_date: str) -> pd.DataFrame:
    preload = (pd.Timestamp(start_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
    df = clickhouse_query_df(
        """
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = ?
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        [INDEX_CODE, preload, end_date],
    )
    if df.empty:
        raise RuntimeError(f"index {INDEX_CODE} has no rows")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    df["ma20"] = df["close"].rolling(20, min_periods=15).mean()
    df["ma60"] = df["close"].rolling(60, min_periods=40).mean()
    df["mom20"] = df["close"].pct_change(20)
    return df[df["trade_date"] >= pd.Timestamp(start_date)].copy()


def _load_emotion_cycle(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT date AS trade_date, close_up_rate, strong_up_rate, weak_up_rate, total_stocks
        FROM emotion_cycle
        WHERE date BETWEEN ? AND ?
          AND total_stocks >= 1000
        ORDER BY date
        """,
        [start_date, end_date],
    )
    if df.empty:
        return pd.DataFrame(columns=["trade_date", "close_up_rate", "strong_up_rate", "weak_up_rate", "total_stocks"])
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["close_up_rate", "strong_up_rate", "weak_up_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["total_stocks"] = pd.to_numeric(df["total_stocks"], errors="coerce")
    return df.dropna(subset=["trade_date", "close_up_rate"]).copy()


def _market_regime(stock_daily: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    d = stock_daily.copy()
    d["amount20"] = d.groupby("code6")["amount"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    d["ma20"] = d.groupby("code6")["close"].transform(lambda s: s.rolling(20, min_periods=15).mean())
    calc_emotion = (
        d[(d["trade_date"] >= pd.Timestamp(start_date)) & (d["trade_date"] <= pd.Timestamp(end_date))]
        .assign(is_up=lambda x: pd.to_numeric(x["change_pct"], errors="coerce") > 0)
        .groupby("trade_date", as_index=False)
        .agg(calc_close_up_rate=("is_up", lambda s: round(float(s.mean() * 100.0), 2)), calc_total_stocks=("code6", "nunique"))
    )
    liquid = d[d["amount20"].fillna(0) >= 3e8].copy()
    breadth = (
        liquid.assign(above_ma20=liquid["close"] >= liquid["ma20"])
        .groupby("trade_date", as_index=False)
        .agg(breadth=("above_ma20", "mean"))
    )
    idx = _load_index(start_date, end_date)
    env = idx.merge(breadth, on="trade_date", how="left")
    emotion = _load_emotion_cycle(start_date, end_date)
    env = env.merge(emotion, on="trade_date", how="left")
    env = env.merge(calc_emotion, on="trade_date", how="left")
    env["emotion_source"] = np.where(env["close_up_rate"].notna(), "emotion_cycle", "kline_daily_rebuild")
    env["close_up_rate"] = env["close_up_rate"].fillna(env["calc_close_up_rate"])
    env["total_stocks"] = env["total_stocks"].fillna(env["calc_total_stocks"])
    env["regime"] = "range"
    up = (
        (env["close"] > env["ma20"])
        & (env["ma20"] > env["ma60"])
        & (env["mom20"] >= 0.02)
        & (env["breadth"] >= 0.55)
    )
    down = (
        (env["close"] < env["ma20"])
        & (env["ma20"] < env["ma60"])
        & (env["mom20"] <= -0.02)
        & (env["breadth"] <= 0.45)
    )
    env.loc[up, "regime"] = "trend_up"
    env.loc[down, "regime"] = "trend_down"
    env["breadth_rank20_low"] = 1.0 - env["breadth"].rolling(20, min_periods=10).rank(pct=True)
    env["small_icepoint_market"] = env["breadth_rank20_low"] >= 0.75
    env["emotion_icepoint_20_30"] = env["close_up_rate"].between(20.0, 30.0)
    env["emotion_icepoint_18_32"] = env["close_up_rate"].between(18.0, 32.0)
    env = env.sort_values("trade_date").reset_index(drop=True)
    prev20 = env["emotion_icepoint_20_30"].shift(1).rolling(3, min_periods=1).max().fillna(0).astype(bool)
    prev18 = env["emotion_icepoint_18_32"].shift(1).rolling(3, min_periods=1).max().fillna(0).astype(bool)
    prev3_min_up = env["close_up_rate"].shift(1).rolling(3, min_periods=1).min()
    env["emotion_repair35_after_20_30"] = prev20 & (env["close_up_rate"] >= 35.0)
    env["emotion_repair40_after_20_30"] = prev20 & (env["close_up_rate"] >= 40.0)
    env["emotion_repair45_after_20_30"] = prev20 & (env["close_up_rate"] >= 45.0)
    env["emotion_repair32_jump10_after_20_30"] = prev20 & (env["close_up_rate"] >= 32.0) & ((env["close_up_rate"] - prev3_min_up) >= 10.0)
    env["emotion_repair35_after_18_32"] = prev18 & (env["close_up_rate"] >= 35.0)
    env["emotion_repair32_50_after_20_30"] = prev20 & env["close_up_rate"].between(32.0, 50.0)
    env["emotion_repair35_55_after_18_32"] = prev18 & env["close_up_rate"].between(35.0, 55.0)
    return env[
        [
            "trade_date",
            "breadth",
            "breadth_rank20_low",
            "small_icepoint_market",
            "close_up_rate",
            "strong_up_rate",
            "weak_up_rate",
            "emotion_source",
            "emotion_icepoint_20_30",
            "emotion_icepoint_18_32",
            "emotion_repair35_after_20_30",
            "emotion_repair40_after_20_30",
            "emotion_repair45_after_20_30",
            "emotion_repair32_jump10_after_20_30",
            "emotion_repair35_after_18_32",
            "emotion_repair32_50_after_20_30",
            "emotion_repair35_55_after_18_32",
            "regime",
        ]
    ]


def _bool_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(False, index=df.index)
    return df[col].fillna(False).astype(bool)


def _num_series(df: pd.DataFrame, *cols: str) -> pd.Series:
    for col in cols:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def _add_pullback_features(stock_daily: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    d = stock_daily.copy().sort_values(["code6", "trade_date"]).reset_index(drop=True)
    g = d.groupby("code6", group_keys=False)
    d["ma10"] = g["close"].rolling(10, min_periods=8).mean().reset_index(level=0, drop=True)
    d["ma20"] = g["close"].rolling(20, min_periods=15).mean().reset_index(level=0, drop=True)
    d["ma60"] = g["close"].rolling(60, min_periods=40).mean().reset_index(level=0, drop=True)
    d["high20"] = g["high"].rolling(20, min_periods=10).max().reset_index(level=0, drop=True)
    d["high60"] = g["high"].rolling(60, min_periods=30).max().reset_index(level=0, drop=True)
    d["low20"] = g["low"].rolling(20, min_periods=10).min().reset_index(level=0, drop=True)
    d["amount5"] = g["amount"].rolling(5, min_periods=3).mean().reset_index(level=0, drop=True)
    d["amount20"] = g["amount"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
    d["mom20"] = g["close"].pct_change(20)
    d["mom60"] = g["close"].pct_change(60)
    d["ret1"] = g["close"].pct_change()
    d["ret3"] = g["close"].pct_change(3)
    d["pullback_from_high20"] = d["close"] / d["high20"].replace(0, np.nan) - 1.0
    d["distance_ma20"] = d["close"] / d["ma20"].replace(0, np.nan) - 1.0
    d["amount_ratio5_20"] = d["amount5"] / d["amount20"].replace(0, np.nan)
    d["range20"] = d["high20"] / d["low20"].replace(0, np.nan) - 1.0
    d["near_high20"] = d["high"] >= d["high20"] * 0.995
    bar_no = d.groupby("code6").cumcount()
    high_bar = bar_no.where(d["near_high20"])
    d["last_high20_bar"] = high_bar.groupby(d["code6"]).ffill()
    d["days_since_high20"] = bar_no - d["last_high20_bar"]
    d["cool_day"] = (d["ret1"] <= 0.015) & (d["close"] <= d["high20"] * 0.985)
    d["cool_len"] = (
        d.groupby("code6")["cool_day"]
        .transform(lambda s: s.groupby((~s).cumsum()).cumcount() + 1)
        .where(d["cool_day"], 0)
    )
    d["strong_consolidation"] = (
        (d["close"] >= d["ma20"])
        & (d["ma20"] >= d["ma60"])
        & (d["mom20"].between(0.05, 0.35))
        & (d["mom60"].between(0.10, 1.20))
        & (d["pullback_from_high20"].between(-0.15, -0.025))
        & (d["distance_ma20"].between(-0.02, 0.12))
        & (d["amount_ratio5_20"].between(0.55, 1.60))
        & (d["range20"].between(0.08, 0.45))
    )
    d["stock_small_icepoint_4_8"] = d["cool_len"].between(4, 8) & (d["ret3"].between(-0.12, 0.03))
    d["stock_pullback_age_4_8"] = (
        d["days_since_high20"].between(4, 8)
        & d["pullback_from_high20"].between(-0.15, -0.025)
        & d["ret3"].between(-0.13, 0.04)
    )
    d["stock_pullback_age_4_12"] = (
        d["days_since_high20"].between(4, 12)
        & d["pullback_from_high20"].between(-0.16, -0.02)
        & d["ret3"].between(-0.15, 0.05)
    )
    for h in horizons:
        d[f"fwd_ret_{h}"] = g["close"].shift(-h) / d["close"] - 1.0
    return d


def _select_mainline_windows(
    sector_features: pd.DataFrame,
    level: int,
    top_n: int,
    min_score: float,
) -> pd.DataFrame:
    f = sector_features[sector_features["level"].eq(level)].copy()
    for col in ["ret20", "ret60", "rank_ret60_pct", "relative_ret60", "ma20_ratio", "rise_ratio", "amount_ratio20_60"]:
        f[col] = pd.to_numeric(f[col], errors="coerce")
    f["mainline_window_score"] = (
        30.0 * ((f["rank_ret60_pct"] - 0.70) / 0.30).clip(0.0, 1.0).fillna(0.0)
        + 25.0 * (f["relative_ret60"] / 0.20).clip(0.0, 1.0).fillna(0.0)
        + 20.0 * ((f["ma20_ratio"] - 0.48) / 0.32).clip(0.0, 1.0).fillna(0.0)
        + 15.0 * ((f["rise_ratio"] - 0.45) / 0.30).clip(0.0, 1.0).fillna(0.0)
        + 10.0 * ((f["amount_ratio20_60"] - 0.95) / 0.55).clip(0.0, 1.0).fillna(0.0)
    )
    picked = f[
        (f["ret60"].between(0.06, 0.80))
        & (f["rank_ret60_pct"] >= 0.70)
        & (f["relative_ret60"] > 0)
        & (f["ma20_ratio"] >= 0.48)
        & (f["mainline_window_score"] >= float(min_score))
    ].copy()
    picked["mainline_rank"] = picked.groupby("trade_date")["mainline_window_score"].rank(method="first", ascending=False)
    return picked[picked["mainline_rank"] <= int(top_n)].copy()


def _summarize(label: str, sample: pd.DataFrame, horizons: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for h in horizons:
        vals = pd.to_numeric(sample.get(f"fwd_ret_{h}"), errors="coerce").dropna()
        rows.append(
            {
                "group": label,
                "horizon": int(h),
                "count": int(len(vals)),
                "avg": _safe_float(vals.mean()),
                "median": _safe_float(vals.median()),
                "win_rate": _safe_float((vals > 0).mean()),
                "best": _safe_float(vals.max()),
                "worst": _safe_float(vals.min()),
                "p25": _safe_float(vals.quantile(0.25)) if len(vals) else None,
                "p75": _safe_float(vals.quantile(0.75)) if len(vals) else None,
            }
        )
    return rows


def _annual_summary(sample: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    if sample.empty:
        return pd.DataFrame()
    d = sample.copy()
    d["year"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.year
    rows: list[dict[str, Any]] = []
    for year, g in d.groupby("year"):
        row: dict[str, Any] = {"year": int(year), "count": int(len(g))}
        for h in horizons:
            vals = pd.to_numeric(g.get(f"fwd_ret_{h}"), errors="coerce").dropna()
            row[f"h{h}_avg"] = _safe_float(vals.mean())
            row[f"h{h}_win_rate"] = _safe_float((vals > 0).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def _write_report(
    result: dict[str, Any],
    summary: pd.DataFrame,
    annual: pd.DataFrame,
    examples: pd.DataFrame,
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_mainline_icepoint.csv", index=False, encoding="utf-8-sig")
    examples.to_csv(OUT_DIR / "examples.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    pivot = summary.pivot_table(index="horizon", columns="group", values="avg", aggfunc="first").reset_index()
    if {"mainline_icepoint", "market_strong_icepoint"}.issubset(pivot.columns):
        pivot["mainline_minus_market_strong"] = pivot["mainline_icepoint"] - pivot["market_strong_icepoint"]
    if {"mainline_icepoint", "mainline_strong_no_icepoint"}.issubset(pivot.columns):
        pivot["icepoint_minus_mainline_no_icepoint"] = pivot["mainline_icepoint"] - pivot["mainline_strong_no_icepoint"]
    pivot.to_csv(OUT_DIR / "comparison.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# G3 主线小冰点强势盘整验证 V1",
        "",
        "## 目标",
        "",
        "验证用户定义的 G3 思路：上涨/可交易震荡周期中，选择主线板块内强势盘整股票，在 4-8 个交易日小冰点窗口低吸；纯下跌周期尽量不交易。",
        "",
        "## 口径",
        "",
        f"- 区间：`{result['start_date']}` 到 `{result['end_date']}`",
        f"- 板块层级：L{result['level']}，每日最多 `{result['top_n']}` 个主线板块",
        "- 市场状态：复用 V6 思路，上证指数 MA20/MA60 + 20 日动量 + 全市场宽度；`trend_down` 全部排除。",
        "- 主线板块：60 日相对强度、MA20 广度、上涨扩散和成交趋势综合过滤。",
        "- 个股：MA20 在 MA60 上方、20/60 日强势、距离 20 日高点回撤 2.5%-15%、靠近 MA20、缩量或温和量。",
        "- 小冰点：个股连续冷却 4-8 日，并叠加市场 20 日宽度低分位。",
        "",
        "## 分组结果",
        "",
        summary.to_markdown(index=False),
        "",
        "## 关键差值",
        "",
        pivot.to_markdown(index=False),
        "",
        "## 年度主线小冰点",
        "",
        annual.to_markdown(index=False) if not annual.empty else "_无样本_",
        "",
        "## 初步解读",
        "",
        "- 若 `mainline_icepoint` 同时跑赢 `market_strong_icepoint` 和 `mainline_strong_no_icepoint`，说明“主线 + 小冰点”有独立增益。",
        "- 若只跑赢全市场但跑不赢主线非冰点，说明主线有效，小冰点定义需要重做。",
        "- 若样本过少或年度不稳定，下一步应放宽小冰点定义或改成板块内分位冰点。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    horizons = _parse_horizons(args.horizons)
    max_h = max(horizons)
    load_start = (pd.Timestamp(args.start_date) - pd.Timedelta(days=160)).strftime("%Y-%m-%d")
    load_end = (pd.Timestamp(args.end_date) + pd.Timedelta(days=max_h + 10)).strftime("%Y-%m-%d")

    members = _load_members([int(args.level)], int(args.min_members))
    raw_daily = _load_daily(load_start, load_end)
    stock_pool = _load_stock_pool()
    daily = raw_daily.merge(stock_pool[["code6", "stock_name"]], on="code6", how="inner")
    daily = daily[(daily["trade_date"] >= pd.Timestamp(load_start)) & (daily["trade_date"] <= pd.Timestamp(load_end))].copy()

    stock_features = _add_stock_features(daily)
    sector_daily = _build_sector_daily(stock_features, members)
    sector_features = _add_sector_feature_frame(sector_daily)
    mainline_windows = _select_mainline_windows(sector_features, int(args.level), int(args.top_n), float(args.min_mainline_score))

    market_env = _market_regime(daily, args.start_date, args.end_date)
    stock_signal = _add_pullback_features(daily, horizons)
    stock_signal = stock_signal.merge(market_env, on="trade_date", how="left")
    stock_signal = stock_signal[
        (stock_signal["trade_date"] >= pd.Timestamp(args.start_date))
        & (stock_signal["trade_date"] <= pd.Timestamp(args.end_date))
        & stock_signal["regime"].isin(["trend_up", "range"])
    ].copy()

    win_keep = [
        "trade_date",
        "sector_code",
        "sector_name",
        "mainline_window_score",
        "mainline_rank",
        "sector_ret",
        "rise_ratio",
        "strong3_ratio",
        "amount_ratio5_20",
        "ret5",
        "ret10",
        "ret20",
        "ret60",
        "relative_ret60",
        "rank_ret60_pct",
    ]
    win_keys = mainline_windows[[c for c in win_keep if c in mainline_windows.columns]].copy()
    stock_mainline = stock_signal.merge(
        members[["stock_code6", "sector_code"]].drop_duplicates(),
        left_on="code6",
        right_on="stock_code6",
        how="left",
    ).merge(win_keys, on=["trade_date", "sector_code"], how="left")
    stock_mainline["in_mainline"] = stock_mainline["mainline_window_score"].notna()

    base_strong = stock_mainline[stock_mainline["strong_consolidation"]].copy()
    mainline_strong = base_strong[base_strong["in_mainline"]].copy()
    mainline_strict_icepoint = mainline_strong[
        mainline_strong["stock_small_icepoint_4_8"] & mainline_strong["small_icepoint_market"].fillna(False)
    ].copy()
    market_strict_icepoint = base_strong[
        base_strong["stock_small_icepoint_4_8"] & base_strong["small_icepoint_market"].fillna(False)
    ].copy()
    market_pullback_age_4_8 = base_strong[base_strong["stock_pullback_age_4_8"]].copy()
    market_pullback_age_4_12 = base_strong[base_strong["stock_pullback_age_4_12"]].copy()
    mainline_pullback_age_4_8 = mainline_strong[mainline_strong["stock_pullback_age_4_8"]].copy()
    mainline_pullback_age_4_12 = mainline_strong[mainline_strong["stock_pullback_age_4_12"]].copy()
    mainline_sector_cool_4_8 = mainline_pullback_age_4_8[
        pd.to_numeric(mainline_pullback_age_4_8.get("ret5"), errors="coerce").between(-0.08, 0.035)
    ].copy()
    market_emotion_20_30_strong = base_strong[_bool_series(base_strong, "emotion_icepoint_20_30")].copy()
    market_emotion_18_32_strong = base_strong[_bool_series(base_strong, "emotion_icepoint_18_32")].copy()
    mainline_emotion_20_30_strong = mainline_strong[_bool_series(mainline_strong, "emotion_icepoint_20_30")].copy()
    mainline_emotion_18_32_strong = mainline_strong[_bool_series(mainline_strong, "emotion_icepoint_18_32")].copy()
    mainline_emotion_20_30_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_icepoint_20_30")
    ].copy()
    mainline_emotion_18_32_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_icepoint_18_32")
    ].copy()
    mainline_repair35_20_30_strong = mainline_strong[_bool_series(mainline_strong, "emotion_repair35_after_20_30")].copy()
    mainline_repair40_20_30_strong = mainline_strong[_bool_series(mainline_strong, "emotion_repair40_after_20_30")].copy()
    mainline_repair45_20_30_strong = mainline_strong[_bool_series(mainline_strong, "emotion_repair45_after_20_30")].copy()
    mainline_repair32_jump10_strong = mainline_strong[_bool_series(mainline_strong, "emotion_repair32_jump10_after_20_30")].copy()
    mainline_repair35_18_32_strong = mainline_strong[_bool_series(mainline_strong, "emotion_repair35_after_18_32")].copy()
    mainline_repair35_20_30_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_repair35_after_20_30")
    ].copy()
    mainline_repair40_20_30_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_repair40_after_20_30")
    ].copy()
    mainline_repair45_20_30_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_repair45_after_20_30")
    ].copy()
    mainline_repair32_jump10_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_repair32_jump10_after_20_30")
    ].copy()
    mainline_repair35_18_32_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_repair35_after_18_32")
    ].copy()
    mainline_repair32_50_20_30_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_repair32_50_after_20_30")
    ].copy()
    mainline_repair35_55_18_32_pullback_4_8 = mainline_pullback_age_4_8[
        _bool_series(mainline_pullback_age_4_8, "emotion_repair35_55_after_18_32")
    ].copy()
    stock_accept = (
        _num_series(mainline_pullback_age_4_8, "ret1").between(0.0, 0.055)
        & _num_series(mainline_pullback_age_4_8, "amount_ratio5_20", "amount_ratio5_20_x").between(0.75, 1.85)
    )
    sector_accept = (
        _num_series(mainline_pullback_age_4_8, "sector_ret").between(0.0, 0.045)
        & (_num_series(mainline_pullback_age_4_8, "rise_ratio") >= 0.52)
        & _num_series(mainline_pullback_age_4_8, "strong3_ratio").between(0.015, 0.22)
    )
    base_repair35_18 = _bool_series(mainline_pullback_age_4_8, "emotion_repair35_after_18_32")
    base_repair35_55_18 = _bool_series(mainline_pullback_age_4_8, "emotion_repair35_55_after_18_32")
    base_repair32_jump = _bool_series(mainline_pullback_age_4_8, "emotion_repair32_jump10_after_20_30")
    mainline_repair35_18_32_pullback_stock_accept = mainline_pullback_age_4_8[base_repair35_18 & stock_accept].copy()
    mainline_repair35_18_32_pullback_sector_accept = mainline_pullback_age_4_8[base_repair35_18 & sector_accept].copy()
    mainline_repair35_18_32_pullback_dual_accept = mainline_pullback_age_4_8[base_repair35_18 & stock_accept & sector_accept].copy()
    mainline_repair35_55_18_32_pullback_stock_accept = mainline_pullback_age_4_8[base_repair35_55_18 & stock_accept].copy()
    mainline_repair35_55_18_32_pullback_dual_accept = mainline_pullback_age_4_8[base_repair35_55_18 & stock_accept & sector_accept].copy()
    mainline_repair32_jump10_pullback_dual_accept = mainline_pullback_age_4_8[base_repair32_jump & stock_accept & sector_accept].copy()
    mainline_no_strict_icepoint = mainline_strong[
        ~(mainline_strong["stock_small_icepoint_4_8"] & mainline_strong["small_icepoint_market"].fillna(False))
    ].copy()

    summary = pd.DataFrame(
        _summarize("market_strong_all", base_strong, horizons)
        + _summarize("market_strict_icepoint", market_strict_icepoint, horizons)
        + _summarize("market_pullback_age_4_8", market_pullback_age_4_8, horizons)
        + _summarize("market_pullback_age_4_12", market_pullback_age_4_12, horizons)
        + _summarize("mainline_strong_all", mainline_strong, horizons)
        + _summarize("mainline_strong_no_strict_icepoint", mainline_no_strict_icepoint, horizons)
        + _summarize("mainline_strict_icepoint", mainline_strict_icepoint, horizons)
        + _summarize("mainline_pullback_age_4_8", mainline_pullback_age_4_8, horizons)
        + _summarize("mainline_pullback_age_4_12", mainline_pullback_age_4_12, horizons)
        + _summarize("mainline_sector_cool_4_8", mainline_sector_cool_4_8, horizons)
        + _summarize("market_emotion_20_30_strong", market_emotion_20_30_strong, horizons)
        + _summarize("market_emotion_18_32_strong", market_emotion_18_32_strong, horizons)
        + _summarize("mainline_emotion_20_30_strong", mainline_emotion_20_30_strong, horizons)
        + _summarize("mainline_emotion_18_32_strong", mainline_emotion_18_32_strong, horizons)
        + _summarize("mainline_emotion_20_30_pullback_4_8", mainline_emotion_20_30_pullback_4_8, horizons)
        + _summarize("mainline_emotion_18_32_pullback_4_8", mainline_emotion_18_32_pullback_4_8, horizons)
        + _summarize("mainline_repair35_20_30_strong", mainline_repair35_20_30_strong, horizons)
        + _summarize("mainline_repair40_20_30_strong", mainline_repair40_20_30_strong, horizons)
        + _summarize("mainline_repair45_20_30_strong", mainline_repair45_20_30_strong, horizons)
        + _summarize("mainline_repair32_jump10_strong", mainline_repair32_jump10_strong, horizons)
        + _summarize("mainline_repair35_18_32_strong", mainline_repair35_18_32_strong, horizons)
        + _summarize("mainline_repair35_20_30_pullback_4_8", mainline_repair35_20_30_pullback_4_8, horizons)
        + _summarize("mainline_repair40_20_30_pullback_4_8", mainline_repair40_20_30_pullback_4_8, horizons)
        + _summarize("mainline_repair45_20_30_pullback_4_8", mainline_repair45_20_30_pullback_4_8, horizons)
        + _summarize("mainline_repair32_jump10_pullback_4_8", mainline_repair32_jump10_pullback_4_8, horizons)
        + _summarize("mainline_repair35_18_32_pullback_4_8", mainline_repair35_18_32_pullback_4_8, horizons)
        + _summarize("mainline_repair32_50_20_30_pullback_4_8", mainline_repair32_50_20_30_pullback_4_8, horizons)
        + _summarize("mainline_repair35_55_18_32_pullback_4_8", mainline_repair35_55_18_32_pullback_4_8, horizons)
        + _summarize("mainline_repair35_18_32_pullback_stock_accept", mainline_repair35_18_32_pullback_stock_accept, horizons)
        + _summarize("mainline_repair35_18_32_pullback_sector_accept", mainline_repair35_18_32_pullback_sector_accept, horizons)
        + _summarize("mainline_repair35_18_32_pullback_dual_accept", mainline_repair35_18_32_pullback_dual_accept, horizons)
        + _summarize("mainline_repair35_55_18_32_pullback_stock_accept", mainline_repair35_55_18_32_pullback_stock_accept, horizons)
        + _summarize("mainline_repair35_55_18_32_pullback_dual_accept", mainline_repair35_55_18_32_pullback_dual_accept, horizons)
        + _summarize("mainline_repair32_jump10_pullback_dual_accept", mainline_repair32_jump10_pullback_dual_accept, horizons)
    )
    annual = _annual_summary(mainline_repair35_55_18_32_pullback_4_8, horizons)

    keep_cols = [
        "trade_date",
        "code_raw",
        "stock_name",
        "sector_name",
        "regime",
        "close_up_rate",
        "emotion_source",
        "breadth",
        "mainline_window_score",
        "sector_ret",
        "rise_ratio",
        "strong3_ratio",
        "mom20",
        "mom60",
        "ret1",
        "pullback_from_high20",
        "distance_ma20",
        "amount_ratio5_20",
        "days_since_high20",
        "cool_len",
        "ret5",
        "ret20",
    ] + [f"fwd_ret_{h}" for h in horizons]
    examples = mainline_repair35_55_18_32_pullback_4_8.sort_values(["trade_date", "mainline_window_score", "mom20"], ascending=[False, False, False])
    examples = examples[[c for c in keep_cols if c in examples.columns]].head(int(args.example_limit)).copy()
    for col in examples.columns:
        if pd.api.types.is_datetime64_any_dtype(examples[col]):
            examples[col] = examples[col].dt.strftime("%Y-%m-%d")

    result = {
        "status": "completed",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "level": int(args.level),
        "top_n": int(args.top_n),
        "min_mainline_score": float(args.min_mainline_score),
        "horizons": horizons,
        "daily_rows": int(len(daily)),
        "emotion_rebuild_note": "close_up_rate uses emotion_cycle when available, otherwise rebuilds from kline_daily change_pct > 0.",
        "sector_feature_rows": int(len(sector_features)),
        "mainline_window_rows": int(len(mainline_windows)),
        "base_strong_rows": int(len(base_strong)),
        "mainline_strong_rows": int(len(mainline_strong)),
        "market_strict_icepoint_rows": int(len(market_strict_icepoint)),
        "mainline_strict_icepoint_rows": int(len(mainline_strict_icepoint)),
        "market_pullback_age_4_8_rows": int(len(market_pullback_age_4_8)),
        "mainline_pullback_age_4_8_rows": int(len(mainline_pullback_age_4_8)),
        "mainline_sector_cool_4_8_rows": int(len(mainline_sector_cool_4_8)),
        "market_emotion_20_30_strong_rows": int(len(market_emotion_20_30_strong)),
        "mainline_emotion_20_30_strong_rows": int(len(mainline_emotion_20_30_strong)),
        "mainline_emotion_20_30_pullback_4_8_rows": int(len(mainline_emotion_20_30_pullback_4_8)),
        "mainline_emotion_18_32_pullback_4_8_rows": int(len(mainline_emotion_18_32_pullback_4_8)),
        "mainline_repair35_20_30_strong_rows": int(len(mainline_repair35_20_30_strong)),
        "mainline_repair40_20_30_strong_rows": int(len(mainline_repair40_20_30_strong)),
        "mainline_repair45_20_30_strong_rows": int(len(mainline_repair45_20_30_strong)),
        "mainline_repair35_20_30_pullback_4_8_rows": int(len(mainline_repair35_20_30_pullback_4_8)),
        "mainline_repair40_20_30_pullback_4_8_rows": int(len(mainline_repair40_20_30_pullback_4_8)),
        "mainline_repair45_20_30_pullback_4_8_rows": int(len(mainline_repair45_20_30_pullback_4_8)),
        "mainline_repair32_50_20_30_pullback_4_8_rows": int(len(mainline_repair32_50_20_30_pullback_4_8)),
        "mainline_repair35_55_18_32_pullback_4_8_rows": int(len(mainline_repair35_55_18_32_pullback_4_8)),
        "mainline_repair35_18_32_pullback_dual_accept_rows": int(len(mainline_repair35_18_32_pullback_dual_accept)),
        "mainline_repair35_55_18_32_pullback_dual_accept_rows": int(len(mainline_repair35_55_18_32_pullback_dual_accept)),
        "mainline_repair32_jump10_pullback_dual_accept_rows": int(len(mainline_repair32_jump10_pullback_dual_accept)),
        "out_dir": str(OUT_DIR),
    }
    _write_report(result, summary, annual, examples)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate G3 mainline 4-8 day icepoint pullback idea.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-05-29")
    parser.add_argument("--level", type=int, default=2)
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--min-members", type=int, default=8)
    parser.add_argument("--min-mainline-score", type=float, default=55.0)
    parser.add_argument("--horizons", default="4,5,8,10")
    parser.add_argument("--example-limit", type=int, default=200)
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
