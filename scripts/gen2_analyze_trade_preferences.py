from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_RUN_DIR = (
    REPO_ROOT
    / "reports"
    / "gen2_prev_low_exit_fill_compare"
    / "runs"
    / "gap_confirm_30m_close__intraday_30m_close"
)
DEFAULT_CLASSIFICATIONS = REPO_ROOT / "data" / "runtime" / "gen2_trade_classifications.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_trade_preference_reverse"

CLASS_PRIORITY = {
    "favorite": 5,
    "watch": 4,
    "normal": 3,
    "problem": 2,
    "reject": 1,
}

POSITIVE_CLASSES = {"favorite", "watch"}
NEGATIVE_CLASSES = {"reject"}

STRUCTURE_FACTORS = [
    "preference_score_v2",
    "preference_score_v3",
    "launch_setup_score",
    "clean_breakout_score",
    "overhead_pressure_score",
    "effective_pressure_score",
    "pressure_absorption_score",
    "late_stage_risk_score",
    "wave_late_score",
    "ma_chase_risk_score",
    "downtrend_rebound_score",
    "volume_bear_distribution_score",
    "ma5_distance_at_entry",
    "ma10_distance_at_entry",
    "runup_20d",
    "runup_60d",
    "prior60_return",
    "ma60_slope_20",
    "below_ma60_ratio_60",
    "giant_bear_amount_ratio_60",
    "wick_noise_score",
    "long_wick_day_ratio_20",
    "large_bear_volume_days_60",
    "small_bull_ratio_10",
    "big_bull_ratio_5",
    "pullback_hold_ma10_score",
    "sig_mom20",
    "sig_volume_ratio",
]

TEMPORAL_PATTERN_FACTORS = [
    "post5_min_ret",
    "post5_max_ret",
    "post5_close_ret",
    "post10_max_ret",
    "post10_min_ret",
    "post5_stop_loss_touch",
    "post5_hold_prior20_high",
    "post5_hold_ma10",
    "post5_retest_hold_score",
    "post10_secondary_volume_breakout",
    "post_absorption_confirmed",
]


def _pct(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.{digits}f}%"


def _num(value: Any, default: float = np.nan) -> float:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if not math.isfinite(value):
        return np.nan
    return min(max(value, low), high)


def _trade_key(row: pd.Series | dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key, "") or "").strip()
        for key in ["code", "buy_datetime", "sell_datetime", "sell_ratio", "exit_reason"]
    )


def _load_classifications(path: Path) -> pd.DataFrame:
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"items": {}}
    rows: list[dict[str, Any]] = []
    for full_key, item in (raw.get("items") or {}).items():
        if not isinstance(item, dict):
            continue
        trade_key = str(item.get("trade_key") or full_key.split(":", 1)[-1])
        rows.append(
            {
                "full_key": full_key,
                "trade_key": trade_key,
                "classification": str(item.get("classification") or ""),
                "manual_note": str(item.get("note") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
        )
    return pd.DataFrame(rows)


def _load_trades(run_dir: Path, classifications: pd.DataFrame) -> pd.DataFrame:
    trades_path = run_dir / "trades.csv"
    if classifications.empty or not trades_path.exists():
        return pd.DataFrame()
    trades = pd.read_csv(trades_path)
    trades["trade_key"] = trades.apply(_trade_key, axis=1)
    d = trades.merge(classifications, on="trade_key", how="inner")
    if d.empty:
        return d
    for col in ["return", "pnl", "capital", "v4_rank", "v4_score", "buy_price", "sell_price", "sell_ratio"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["buy_datetime"] = pd.to_datetime(d["buy_datetime"], errors="coerce")
    d["sell_datetime"] = pd.to_datetime(d["sell_datetime"], errors="coerce")
    d["buy_date"] = pd.to_datetime(d["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["entry_id"] = d["code"].astype(str) + "|" + d["buy_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return d


def _load_signals(run_dir: Path) -> pd.DataFrame:
    signals_path = run_dir / "signals.csv"
    if not signals_path.exists():
        return pd.DataFrame()
    signals = pd.read_csv(signals_path)
    signals["code"] = signals["code"].astype(str)
    signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce")
    signals["entry_id"] = signals["code"] + "|" + signals["confirm_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in [
        "v4_rank",
        "v4_score",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "volume_ratio",
        "entry_price",
        "confirm_ma5",
        "confirm_ma10",
        "entry_fwd_ret_20d",
    ]:
        if col in signals.columns:
            signals[col] = pd.to_numeric(signals[col], errors="coerce")
    keep_cols = [
        "entry_id",
        "entry_date",
        "entry_price",
        "confirm_datetime",
        "confirm_ma5",
        "confirm_ma10",
        "v4_rank",
        "v4_score",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "volume_ratio",
        "breadth_ma20",
        "entry_fwd_ret_20d",
    ]
    return signals[[col for col in keep_cols if col in signals.columns]].copy()


def _entry_level(trades: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for entry_id, group in trades.groupby("entry_id", sort=False):
        selected = group.sort_values(
            "classification",
            key=lambda s: s.map(CLASS_PRIORITY).fillna(0),
            ascending=False,
        ).iloc[0]
        row = selected.to_dict()
        row["legs"] = int(len(group))
        row["entry_pnl"] = float(pd.to_numeric(group["pnl"], errors="coerce").sum())
        row["entry_capital"] = float(pd.to_numeric(group["capital"], errors="coerce").sum())
        row["entry_mean_return"] = float(pd.to_numeric(group["return"], errors="coerce").mean())
        row["entry_class_set"] = "/".join(
            sorted(set(group["classification"]), key=lambda c: -CLASS_PRIORITY.get(str(c), 0))
        )
        row["entry_notes"] = " | ".join([str(x) for x in group["manual_note"].dropna().tolist() if str(x).strip()])
        rows.append(row)
    entries = pd.DataFrame(rows)
    if signals.empty:
        entries["entry_date"] = pd.to_datetime(entries["buy_datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        entries["entry_price"] = entries["buy_price"]
        return entries
    merged = entries.merge(signals, on="entry_id", how="left", suffixes=("", "_signal"))
    merged["entry_date"] = merged["entry_date"].fillna(pd.to_datetime(merged["buy_datetime"], errors="coerce").dt.strftime("%Y-%m-%d"))
    merged["entry_price"] = pd.to_numeric(merged["entry_price"], errors="coerce").fillna(pd.to_numeric(merged["buy_price"], errors="coerce"))
    return merged


def _load_daily(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join(f"'{code}'" for code in sorted(set(codes)))
    start = (pd.Timestamp(start_date) - pd.Timedelta(days=240)).strftime("%Y-%m-%d")
    client = clickhouse_client()
    df = client.query_df(
        f"""
        SELECT code, trade_date, open, high, low, close, volume, amount
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)


def _prior_features(history: pd.DataFrame, entry_date: str, entry_price: float) -> dict[str, Any]:
    prior = history[history["trade_date"] < entry_date].tail(120).copy()
    result: dict[str, Any] = {
        "daily_coverage": int(len(prior)),
    }
    if prior.empty or not math.isfinite(entry_price) or entry_price <= 0:
        return result

    prior20 = prior.tail(20)
    prior60 = prior.tail(60)
    prior120 = prior.tail(120)

    close = prior["close"]
    ma5 = close.tail(5).mean() if len(close) >= 5 else np.nan
    ma10 = close.tail(10).mean() if len(close) >= 10 else np.nan
    ma20 = close.tail(20).mean() if len(close) >= 20 else np.nan
    ma60 = close.tail(60).mean() if len(close) >= 60 else np.nan

    result["ma5_distance_at_entry"] = entry_price / ma5 - 1.0 if ma5 and math.isfinite(ma5) else np.nan
    result["ma10_distance_at_entry"] = entry_price / ma10 - 1.0 if ma10 and math.isfinite(ma10) else np.nan
    result["ma20_distance_at_entry"] = entry_price / ma20 - 1.0 if ma20 and math.isfinite(ma20) else np.nan
    result["ma60_distance_at_entry"] = entry_price / ma60 - 1.0 if ma60 and math.isfinite(ma60) else np.nan
    result["runup_20d"] = entry_price / prior20["low"].min() - 1.0 if len(prior20) else np.nan
    result["runup_60d"] = entry_price / prior60["low"].min() - 1.0 if len(prior60) else np.nan
    result["prior60_return"] = float(prior60["close"].iloc[-1] / prior60["close"].iloc[0] - 1.0) if len(prior60) >= 2 else np.nan
    result["ma60_slope_20"] = float(ma60 / close.iloc[-80:-20].mean() - 1.0) if len(close) >= 80 and math.isfinite(ma60) else np.nan
    result["below_ma60_ratio_60"] = float((prior60["close"] < ma60).mean()) if len(prior60) and math.isfinite(ma60) else np.nan
    result["prev20_high_gap"] = entry_price / prior20["high"].max() - 1.0 if len(prior20) else np.nan
    result["prev60_high_gap"] = entry_price / prior60["high"].max() - 1.0 if len(prior60) else np.nan

    pressure = prior120[
        (prior120["high"] >= entry_price * 0.98)
        & (prior120["low"] <= entry_price * 1.15)
    ].copy()
    result["overhead_pressure_days_120"] = int(len(pressure))
    result["overhead_pressure_density_120"] = float(len(pressure) / max(len(prior120), 1))
    amount_total = prior120["amount"].sum()
    result["overhead_pressure_amount_share_120"] = float(pressure["amount"].sum() / amount_total) if amount_total else np.nan
    recent_pressure = prior60[
        (prior60["high"] >= entry_price * 0.98)
        & (prior60["low"] <= entry_price * 1.12)
    ]
    result["recent_pressure_density_60"] = float(len(recent_pressure) / max(len(prior60), 1))

    platform = prior20 if len(prior20) >= 10 else prior
    platform_mid = platform["close"].mean()
    platform_range = (platform["high"].max() - platform["low"].min()) / platform_mid if platform_mid else np.nan
    breakout_margin = entry_price / platform["high"].max() - 1.0 if len(platform) else np.nan
    result["platform_range_20"] = float(platform_range)
    result["breakout_margin_20"] = float(breakout_margin)
    tight_score = max(0.0, 1.0 - min(float(platform_range or 1.0), 0.35) / 0.35)
    break_score = max(0.0, min((float(breakout_margin or 0.0) + 0.03) / 0.08, 1.0))
    pressure_penalty = min(float(result["overhead_pressure_density_120"] or 0.0) / 0.35, 1.0)
    result["clean_breakout_score"] = round(0.55 * break_score + 0.35 * tight_score + 0.10 * (1.0 - pressure_penalty), 4)
    result["overhead_pressure_score"] = round(
        0.55 * min(float(result["overhead_pressure_density_120"] or 0.0) / 0.35, 1.0)
        + 0.45 * min(float(result["overhead_pressure_amount_share_120"] or 0.0) / 0.45, 1.0),
        4,
    )

    # User-preference proxies: launch confirmation, not a late hot chase.
    last10 = prior.tail(10).copy()
    last5 = prior.tail(5).copy()
    ret1 = last10["close"].pct_change()
    body_pct = (last10["close"] / last10["open"] - 1.0).replace([np.inf, -np.inf], np.nan)
    small_bull = (body_pct > 0.0) & (body_pct <= 0.045)
    big_bull = body_pct > 0.06
    result["small_bull_ratio_10"] = float(small_bull.mean()) if len(small_bull) else np.nan
    result["big_bull_ratio_5"] = float((big_bull.tail(5)).mean()) if len(big_bull) else np.nan
    result["prior5_return"] = float(last5["close"].iloc[-1] / last5["close"].iloc[0] - 1.0) if len(last5) >= 2 else np.nan
    result["prior10_return"] = float(last10["close"].iloc[-1] / last10["close"].iloc[0] - 1.0) if len(last10) >= 2 else np.nan
    result["pullback_low_to_ma10_10d"] = float(last10["low"].min() / ma10 - 1.0) if ma10 and math.isfinite(ma10) and len(last10) else np.nan
    pullback_low_to_ma10 = float(result.get("pullback_low_to_ma10_10d") or np.nan)
    if math.isfinite(pullback_low_to_ma10) and pullback_low_to_ma10 >= -0.035:
        result["pullback_hold_ma10_score"] = _clip(1.0 - abs(pullback_low_to_ma10) / 0.10)
    else:
        result["pullback_hold_ma10_score"] = 0.0

    ma5_dist = float(result.get("ma5_distance_at_entry") or 0.0)
    ma10_dist = float(result.get("ma10_distance_at_entry") or 0.0)
    runup20 = float(result.get("runup_20d") or 0.0)
    runup60 = float(result.get("runup_60d") or 0.0)
    result["ma_chase_risk_score"] = round(_clip((ma5_dist - 0.08) / 0.12), 4)
    result["wave_late_score"] = round(
        _clip(
            0.35 * _clip((runup60 - 0.80) / 0.90)
            + 0.25 * _clip((runup20 - 0.35) / 0.45)
            + 0.20 * float(result["ma_chase_risk_score"])
            + 0.20 * _clip(float(result["big_bull_ratio_5"] or 0.0) / 0.40)
        ),
        4,
    )
    result["downtrend_rebound_score"] = round(
        _clip(
            0.45 * _clip((-(float(result.get("prior60_return") or 0.0))) / 0.35)
            + 0.35 * float(result.get("below_ma60_ratio_60") or 0.0)
            + 0.20 * _clip(float(result["overhead_pressure_score"]) / 0.65)
        ),
        4,
    )
    result["late_stage_risk_score"] = round(
        0.45 * _clip((runup60 - 0.65) / 0.75)
        + 0.25 * _clip(float(result["overhead_pressure_score"]) / 0.65)
        + 0.30 * _clip(float(result["big_bull_ratio_5"] or 0.0) / 0.45),
        4,
    )
    launch_base = (
        0.30 * float(result["clean_breakout_score"])
        + 0.25 * _clip(float(result["small_bull_ratio_10"] or 0.0) / 0.55)
        + 0.20 * _clip((float(result["prior10_return"] or 0.0) + 0.02) / 0.20)
        + 0.25 * float(result["pullback_hold_ma10_score"])
    )
    result["launch_setup_score"] = round(_clip(launch_base), 4)

    wick_base = prior20 if len(prior20) >= 10 else prior
    span = (wick_base["high"] - wick_base["low"]).replace(0, np.nan)
    upper = (wick_base["high"] - wick_base[["open", "close"]].max(axis=1)) / span
    lower = (wick_base[["open", "close"]].min(axis=1) - wick_base["low"]) / span
    wick_sum = (upper + lower).replace([np.inf, -np.inf], np.nan)
    result["wick_noise_score"] = float(wick_sum.mean()) if not wick_sum.dropna().empty else np.nan
    result["long_wick_day_ratio_20"] = float((wick_sum > 0.55).mean()) if len(wick_sum) else np.nan
    result["upper_wick_mean_20"] = float(upper.mean()) if not upper.dropna().empty else np.nan

    large_bear = prior60[
        (prior60["close"] < prior60["open"])
        & ((prior60["open"] / prior60["close"] - 1.0) >= 0.04)
        & (prior60["amount"] >= prior60["amount"].rolling(20, min_periods=5).mean() * 1.8)
    ]
    result["large_bear_volume_days_60"] = int(len(large_bear))
    amount_ma20 = prior60["amount"].shift(1).rolling(20, min_periods=5).mean()
    bear_body = (prior60["open"] / prior60["close"] - 1.0).replace([np.inf, -np.inf], np.nan)
    candle_span = (prior60["high"] - prior60["low"]).replace(0, np.nan)
    close_position = ((prior60["close"] - prior60["low"]) / candle_span).replace([np.inf, -np.inf], np.nan)
    amount_ratio = (prior60["amount"] / amount_ma20).replace([np.inf, -np.inf], np.nan)
    bear_distribution = (
        ((bear_body - 0.03) / 0.08).clip(0, 1)
        * (amount_ratio / 3.0).clip(0, 1)
        * (1.0 - close_position.fillna(0.5)).clip(0, 1)
    )
    result["volume_bear_distribution_score"] = round(float(bear_distribution.max()) if not bear_distribution.dropna().empty else 0.0, 4)
    giant_bear_amount = amount_ratio[(prior60["close"] < prior60["open"]) & (bear_body >= 0.035)]
    result["giant_bear_amount_ratio_60"] = float(giant_bear_amount.max()) if not giant_bear_amount.dropna().empty else 0.0
    distribution_penalty = _clip(float(result["large_bear_volume_days_60"]) / 3.0)
    result["preference_score_v2"] = round(
        _clip(
            0.42 * float(result["launch_setup_score"])
            + 0.18 * _clip((float(result.get("sig_mom20", np.nan)) if "sig_mom20" in result else 0.25) / 0.40)
            + 0.40 * (1.0 - float(result["overhead_pressure_score"]))
            - 0.18 * float(result["late_stage_risk_score"])
            - 0.12 * float(result["ma_chase_risk_score"])
            - 0.08 * distribution_penalty,
        ),
        4,
    )
    return result


def _post_entry_features(history: pd.DataFrame, entry_date: str, entry_price: float) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if history.empty or not math.isfinite(entry_price) or entry_price <= 0:
        return result

    prior = history[history["trade_date"] < entry_date].tail(60).copy()
    post = history[history["trade_date"] >= entry_date].head(10).copy()
    if post.empty:
        return result

    prior20 = prior.tail(20)
    prior20_high = float(prior20["high"].max()) if len(prior20) else np.nan
    ma10 = float(prior["close"].tail(10).mean()) if len(prior) >= 10 else np.nan
    amount_ma20 = float(prior["amount"].tail(20).mean()) if len(prior) >= 5 else np.nan

    post5 = post.head(5)
    result["post5_min_ret"] = float(post5["low"].min() / entry_price - 1.0) if len(post5) else np.nan
    result["post5_max_ret"] = float(post5["high"].max() / entry_price - 1.0) if len(post5) else np.nan
    result["post5_close_ret"] = float(post5["close"].iloc[-1] / entry_price - 1.0) if len(post5) else np.nan
    result["post10_max_ret"] = float(post["high"].max() / entry_price - 1.0)
    result["post10_min_ret"] = float(post["low"].min() / entry_price - 1.0)
    result["post5_stop_loss_touch"] = int(float(result["post5_min_ret"]) <= -0.05) if math.isfinite(float(result["post5_min_ret"])) else 0

    post5_low = float(post5["low"].min()) if len(post5) else np.nan
    result["post5_hold_prior20_high"] = (
        int(math.isfinite(prior20_high) and entry_price >= prior20_high * 0.98 and post5_low >= prior20_high * 0.97)
        if math.isfinite(post5_low)
        else 0
    )
    result["post5_hold_ma10"] = int(math.isfinite(ma10) and math.isfinite(post5_low) and post5_low >= ma10 * 0.97)

    min_ret = float(result.get("post5_min_ret") or np.nan)
    max_ret10 = float(result.get("post10_max_ret") or np.nan)
    if math.isfinite(min_ret) and math.isfinite(max_ret10):
        shallow_retest = _clip((min_ret + 0.08) / 0.10)
        upside_after = _clip((max_ret10 - 0.05) / 0.15)
        hold_bonus = 0.5 * float(result["post5_hold_prior20_high"]) + 0.5 * float(result["post5_hold_ma10"])
        result["post5_retest_hold_score"] = round(_clip(0.45 * shallow_retest + 0.35 * upside_after + 0.20 * hold_bonus), 4)
    else:
        result["post5_retest_hold_score"] = np.nan

    secondary_breakout = 0
    if len(post) >= 2 and math.isfinite(amount_ma20) and amount_ma20 > 0:
        rolling_high = max(entry_price, prior20_high if math.isfinite(prior20_high) else entry_price)
        for _, bar in post.iloc[1:].iterrows():
            amount_ratio = float(bar["amount"] / amount_ma20) if math.isfinite(float(bar["amount"])) else np.nan
            if amount_ratio >= 1.6 and float(bar["close"]) > rolling_high * 1.01:
                secondary_breakout = 1
                break
            rolling_high = max(rolling_high, float(bar["high"]))
    result["post10_secondary_volume_breakout"] = secondary_breakout
    result["post_absorption_confirmed"] = int(
        (float(result.get("post5_hold_prior20_high") or 0) > 0 or float(result.get("post5_hold_ma10") or 0) > 0)
        and (float(result.get("post5_close_ret") or -1.0) > 0 or secondary_breakout)
    )
    return result


def _add_daily_features(entries: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    if entries.empty or daily.empty:
        return entries
    by_code = {code: group.reset_index(drop=True) for code, group in daily.groupby("code", sort=False)}
    feature_rows: list[dict[str, Any]] = []
    for _, row in entries.iterrows():
        history = by_code.get(str(row.get("code")))
        if history is None:
            feature_rows.append({})
            continue
        prior_features = _prior_features(history, str(row.get("entry_date")), _num(row.get("entry_price")))
        post_features = _post_entry_features(history, str(row.get("entry_date")), _num(row.get("entry_price")))
        feature_rows.append({**prior_features, **post_features})
    features = pd.DataFrame(feature_rows)
    return pd.concat([entries.reset_index(drop=True), features.reset_index(drop=True)], axis=1)


def _preference_label(value: str) -> str:
    if value in POSITIVE_CLASSES:
        return "positive"
    if value in NEGATIVE_CLASSES:
        return "negative"
    return "neutral"


def _summarize_groups(entries: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, Any]] = []
    factor_rows: list[dict[str, Any]] = []
    factors = STRUCTURE_FACTORS
    for cls, group in entries.groupby("classification", dropna=False):
        rows.append(
            {
                "classification": cls,
                "entries": int(len(group)),
                "win_rate": float((group["entry_pnl"] > 0).mean()) if len(group) else np.nan,
                "avg_entry_return": float(pd.to_numeric(group["entry_mean_return"], errors="coerce").mean()),
                "median_entry_return": float(pd.to_numeric(group["entry_mean_return"], errors="coerce").median()),
                "total_pnl": float(pd.to_numeric(group["entry_pnl"], errors="coerce").sum()),
                "avg_rank": float(pd.to_numeric(group["v4_rank"], errors="coerce").mean()),
            }
        )
        for factor in factors:
            if factor not in group.columns:
                continue
            values = pd.to_numeric(group[factor], errors="coerce").dropna()
            if values.empty:
                continue
            factor_rows.append(
                {
                    "classification": cls,
                    "factor": factor,
                    "count": int(len(values)),
                    "mean": float(values.mean()),
                    "median": float(values.median()),
                    "p25": float(values.quantile(0.25)),
                    "p75": float(values.quantile(0.75)),
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(factor_rows)


def _positive_negative_effects(entries: pd.DataFrame) -> pd.DataFrame:
    d = entries[entries["preference_label"].isin(["positive", "negative"])].copy()
    factors = STRUCTURE_FACTORS
    pos = d[d["preference_label"] == "positive"]
    neg = d[d["preference_label"] == "negative"]
    rows: list[dict[str, Any]] = []
    for factor in factors:
        if factor not in d.columns:
            continue
        x_pos = pd.to_numeric(pos[factor], errors="coerce").dropna()
        x_neg = pd.to_numeric(neg[factor], errors="coerce").dropna()
        if x_pos.empty or x_neg.empty:
            continue
        pooled = pd.concat([x_pos, x_neg])
        std = pooled.std()
        rows.append(
            {
                "factor": factor,
                "positive_mean": float(x_pos.mean()),
                "negative_mean": float(x_neg.mean()),
                "spread": float(x_pos.mean() - x_neg.mean()),
                "effect": float((x_pos.mean() - x_neg.mean()) / std) if std and math.isfinite(std) else np.nan,
                "positive_median": float(x_pos.median()),
                "negative_median": float(x_neg.median()),
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values("effect", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def _temporal_pattern_summary(entries: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for cls, group in entries.groupby("classification", dropna=False):
        for factor in TEMPORAL_PATTERN_FACTORS:
            if factor not in group.columns:
                continue
            values = pd.to_numeric(group[factor], errors="coerce").dropna()
            if values.empty:
                continue
            rows.append(
                {
                    "classification": cls,
                    "factor": factor,
                    "count": int(len(values)),
                    "mean": float(values.mean()),
                    "median": float(values.median()),
                    "p25": float(values.quantile(0.25)),
                    "p75": float(values.quantile(0.75)),
                }
            )
    return pd.DataFrame(rows)


def _temporal_positive_negative_effects(entries: pd.DataFrame) -> pd.DataFrame:
    d = entries[entries["preference_label"].isin(["positive", "negative"])].copy()
    pos = d[d["preference_label"] == "positive"]
    neg = d[d["preference_label"] == "negative"]
    rows: list[dict[str, Any]] = []
    for factor in TEMPORAL_PATTERN_FACTORS:
        if factor not in d.columns:
            continue
        x_pos = pd.to_numeric(pos[factor], errors="coerce").dropna()
        x_neg = pd.to_numeric(neg[factor], errors="coerce").dropna()
        if x_pos.empty or x_neg.empty:
            continue
        pooled = pd.concat([x_pos, x_neg])
        std = pooled.std()
        rows.append(
            {
                "factor": factor,
                "positive_mean": float(x_pos.mean()),
                "negative_mean": float(x_neg.mean()),
                "spread": float(x_pos.mean() - x_neg.mean()),
                "effect": float((x_pos.mean() - x_neg.mean()) / std) if std and math.isfinite(std) else np.nan,
                "positive_median": float(x_pos.median()),
                "negative_median": float(x_neg.median()),
            }
        )
    out = pd.DataFrame(rows)
    return out.sort_values("effect", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def _keyword_summary(classifications: pd.DataFrame) -> pd.DataFrame:
    patterns = {
        "pressure": r"压力|密集",
        "breakout": r"突破|启动|确认|放量",
        "ma_distance": r"远离|ma5|5日线|贴近",
        "wick_noise": r"上影线|下影线|股性|做T|筹码",
        "runup_late": r"涨幅|翻倍|120%|50%|三浪|5浪|五浪",
        "downtrend_rebound": r"单边下跌|长期.*下跌|反弹",
        "distribution": r"出货|巨量阴线|卖点",
    }
    rows: list[dict[str, Any]] = []
    for cls, group in classifications.groupby("classification", dropna=False):
        notes = group["manual_note"].fillna("").astype(str)
        for key, pattern in patterns.items():
            rows.append(
                {
                    "classification": cls,
                    "keyword_group": key,
                    "hits": int(notes.str.contains(pattern, case=False, regex=True).sum()),
                    "note_count": int((notes.str.strip() != "").sum()),
                }
            )
    return pd.DataFrame(rows)


def _add_composite_scores(entries: pd.DataFrame) -> pd.DataFrame:
    d = entries.copy()
    for col in STRUCTURE_FACTORS:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    mom20_component = (pd.to_numeric(d.get("sig_mom20"), errors="coerce").fillna(0.25) / 0.40).clip(0, 1)
    volume_component = (pd.to_numeric(d.get("sig_volume_ratio"), errors="coerce").fillna(3.0) / 6.0).clip(0, 1)
    distribution_penalty = (pd.to_numeric(d.get("large_bear_volume_days_60"), errors="coerce").fillna(0) / 3.0).clip(0, 1)
    breakout_component = pd.to_numeric(d.get("clean_breakout_score"), errors="coerce").fillna(0.35).clip(0, 1)
    pressure = pd.to_numeric(d.get("overhead_pressure_score"), errors="coerce").fillna(0.5).clip(0, 1)
    d["pressure_absorption_score"] = (
        0.45 * ((pd.to_numeric(d.get("sig_volume_ratio"), errors="coerce").fillna(3.0) - 3.0) / 5.0).clip(0, 1)
        + 0.35 * breakout_component
        + 0.20 * mom20_component
    ).clip(0, 1).round(4)
    d["effective_pressure_score"] = (pressure * (1.0 - 0.45 * d["pressure_absorption_score"])).clip(0, 1).round(4)
    d["preference_score_v2"] = (
        0.34 * pd.to_numeric(d.get("launch_setup_score"), errors="coerce").fillna(0.35)
        + 0.16 * mom20_component
        + 0.08 * volume_component
        + 0.42 * (1.0 - pressure)
        - 0.18 * pd.to_numeric(d.get("late_stage_risk_score"), errors="coerce").fillna(0.35)
        - 0.12 * pd.to_numeric(d.get("ma_chase_risk_score"), errors="coerce").fillna(0.0)
        - 0.08 * distribution_penalty
    ).clip(0, 1).round(4)
    d["preference_score_v3"] = (
        0.30 * pd.to_numeric(d.get("launch_setup_score"), errors="coerce").fillna(0.35)
        + 0.14 * mom20_component
        + 0.08 * volume_component
        + 0.38 * (1.0 - d["effective_pressure_score"])
        - 0.16 * pd.to_numeric(d.get("late_stage_risk_score"), errors="coerce").fillna(0.35)
        - 0.12 * pd.to_numeric(d.get("wave_late_score"), errors="coerce").fillna(0.0)
        - 0.10 * pd.to_numeric(d.get("ma_chase_risk_score"), errors="coerce").fillna(0.0)
        - 0.10 * pd.to_numeric(d.get("downtrend_rebound_score"), errors="coerce").fillna(0.0)
        - 0.10 * pd.to_numeric(d.get("volume_bear_distribution_score"), errors="coerce").fillna(0.0)
    ).clip(0, 1).round(4)
    return d


def _rule_tests(entries: pd.DataFrame) -> pd.DataFrame:
    d = entries[entries["preference_label"].isin(["positive", "negative"])].copy()
    if d.empty:
        return pd.DataFrame()

    rules = {
        "score_v2_ge_050": d["preference_score_v2"] >= 0.50,
        "score_v2_ge_055": d["preference_score_v2"] >= 0.55,
        "score_v3_ge_050": d["preference_score_v3"] >= 0.50,
        "score_v3_ge_055": d["preference_score_v3"] >= 0.55,
        "low_pressure_le_035": d["overhead_pressure_score"] <= 0.35,
        "low_pressure_le_040": d["overhead_pressure_score"] <= 0.40,
        "effective_pressure_le_040": d["effective_pressure_score"] <= 0.40,
        "launch_ge_045_pressure_le_045": (d["launch_setup_score"] >= 0.45) & (d["overhead_pressure_score"] <= 0.45),
        "avoid_late_and_pressure": (d["late_stage_risk_score"] <= 0.55) & (d["overhead_pressure_score"] <= 0.45),
        "score_v2_core": (
            (d["preference_score_v2"] >= 0.50)
            & (d["overhead_pressure_score"] <= 0.45)
            & (d["ma_chase_risk_score"] <= 0.65)
        ),
        "score_v3_core": (
            (d["preference_score_v3"] >= 0.50)
            & (d["effective_pressure_score"] <= 0.45)
            & (d["wave_late_score"] <= 0.55)
            & (d["volume_bear_distribution_score"] <= 0.55)
        ),
        "pressure_absorbed_core": (
            (d["launch_setup_score"] >= 0.45)
            & (d["effective_pressure_score"] <= 0.50)
            & (d["downtrend_rebound_score"] <= 0.55)
            & (d["wave_late_score"] <= 0.60)
        ),
    }
    rows: list[dict[str, Any]] = []
    positives = d["preference_label"] == "positive"
    negatives = d["preference_label"] == "negative"
    for name, mask in rules.items():
        selected = d[mask.fillna(False)]
        tp = int(((mask.fillna(False)) & positives).sum())
        fp = int(((mask.fillna(False)) & negatives).sum())
        fn = int(((~mask.fillna(False)) & positives).sum())
        precision = tp / (tp + fp) if (tp + fp) else np.nan
        recall = tp / (tp + fn) if (tp + fn) else np.nan
        rows.append(
            {
                "rule": name,
                "selected": int(len(selected)),
                "positive_selected": tp,
                "reject_selected": fp,
                "precision_vs_reject": precision,
                "recall_positive": recall,
                "avg_entry_return": float(pd.to_numeric(selected["entry_mean_return"], errors="coerce").mean()) if len(selected) else np.nan,
                "win_rate": float((selected["entry_pnl"] > 0).mean()) if len(selected) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _rule_review(entries: pd.DataFrame) -> pd.DataFrame:
    d = entries[entries["preference_label"].isin(["positive", "negative"])].copy()
    if d.empty:
        return pd.DataFrame()
    selected = (
        (pd.to_numeric(d["preference_score_v3"], errors="coerce") >= 0.50)
        & (pd.to_numeric(d["effective_pressure_score"], errors="coerce") <= 0.45)
        & (pd.to_numeric(d["wave_late_score"], errors="coerce") <= 0.55)
        & (pd.to_numeric(d["volume_bear_distribution_score"], errors="coerce") <= 0.55)
    )
    d["score_v3_core_selected"] = selected
    d["review_bucket"] = np.select(
        [
            selected & (d["preference_label"] == "negative"),
            (~selected) & (d["preference_label"] == "positive"),
        ],
        ["false_positive_reject", "missed_positive"],
        default="aligned",
    )
    cols = [
        "review_bucket",
        "entry_date",
        "code",
        "name",
        "classification",
        "entry_mean_return",
        "preference_score_v2",
        "preference_score_v3",
        "launch_setup_score",
        "overhead_pressure_score",
        "effective_pressure_score",
        "pressure_absorption_score",
        "late_stage_risk_score",
        "wave_late_score",
        "ma_chase_risk_score",
        "downtrend_rebound_score",
        "volume_bear_distribution_score",
        "manual_note",
    ]
    return d[[col for col in cols if col in d.columns]].sort_values(["review_bucket", "entry_date", "code"])


def _write_report(
    output_dir: Path,
    entries: pd.DataFrame,
    group_summary: pd.DataFrame,
    effects: pd.DataFrame,
    temporal_effects: pd.DataFrame,
    keyword_summary: pd.DataFrame,
    rules: pd.DataFrame,
) -> None:
    positive = entries[entries["preference_label"] == "positive"]
    negative = entries[entries["preference_label"] == "negative"]
    lines = [
        "# G2 Trade Preference Reverse Study",
        "",
        "## Scope",
        "",
        f"- Entry samples: `{len(entries)}`",
        f"- Positive samples (`favorite/watch`): `{len(positive)}`",
        f"- Negative samples (`reject`): `{len(negative)}`",
        f"- Date range: `{entries['entry_date'].min()}` to `{entries['entry_date'].max()}`",
        "",
        "## Classification Summary",
        "",
        "| classification | entries | win | avg_ret | median_ret | total_pnl | avg_rank |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    order = ["favorite", "watch", "normal", "problem", "reject"]
    for _, row in group_summary.set_index("classification").reindex(order).dropna(how="all").reset_index().iterrows():
        lines.append(
            f"| {row['classification']} | {int(row['entries'])} | {_pct(row['win_rate'])} | "
            f"{_pct(row['avg_entry_return'])} | {_pct(row['median_entry_return'])} | "
            f"{row['total_pnl']:.2f} | {row['avg_rank']:.1f} |"
        )
    lines.extend(["", "## Positive vs Reject Factor Effects", ""])
    lines.append("| factor | positive_mean | reject_mean | spread | effect |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for _, row in effects.head(12).iterrows():
        lines.append(
            f"| {row['factor']} | {row['positive_mean']:.4f} | {row['negative_mean']:.4f} | "
            f"{row['spread']:.4f} | {row['effect']:.3f} |"
        )
    lines.extend(["", "## Post-Entry Temporal Pattern Effects", ""])
    lines.append("> These are reverse-study labels only. They use post-entry bars and must not be used as entry-time filters.")
    lines.append("")
    lines.append("| factor | positive_mean | reject_mean | spread | effect |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for _, row in temporal_effects.head(12).iterrows():
        lines.append(
            f"| {row['factor']} | {row['positive_mean']:.4f} | {row['negative_mean']:.4f} | "
            f"{row['spread']:.4f} | {row['effect']:.3f} |"
        )
    lines.extend(["", "## Candidate Rule Tests", ""])
    lines.append("| rule | selected | positive | reject | precision | recall | avg_ret | win |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for _, row in rules.iterrows():
        lines.append(
            f"| {row['rule']} | {int(row['selected'])} | {int(row['positive_selected'])} | "
            f"{int(row['reject_selected'])} | {_pct(row['precision_vs_reject'])} | "
            f"{_pct(row['recall_positive'])} | {_pct(row['avg_entry_return'])} | {_pct(row['win_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## First Read",
            "",
            "- Positive samples are still not explained by V4 rank alone; rank is a weak preference proxy.",
            "- User notes and factors both point to pressure/late-stage risk as the first negative dimension.",
            "- V2 adds launch setup, late-stage risk and MA chase risk proxies to better reflect the user's note language.",
            "- V3 adds pressure absorption, distribution bar, downtrend rebound and late-wave chase proxies for the newest false-positive/false-negative review.",
            "- The current factor scores are research labels, not final trading rules. They must be validated on unlabelled and later blind windows before hardening.",
            "",
            "## Candidate Rule Modules",
            "",
            "1. `overhead_pressure_score`: penalize dense nearby overhead pressure and recent trapped-volume zones.",
            "2. `clean_breakout_score`: reward tight platform breakout and avoid fake rebound into pressure.",
            "3. `ma5_distance_at_entry`: avoid chasing too far away from MA5/MA10; wait for pullback/re-volume confirmation.",
            "4. `wick_noise_score`: penalize long-wick, unstable, heavily T-traded names.",
            "5. `large_bear_volume_days_60`: penalize recent high-volume bearish distribution bars.",
            "6. `effective_pressure_score`: reduce pressure only when volume and breakout evidence suggest absorption.",
            "",
            "## Outputs",
            "",
            "- `entry_samples.csv`: entry-level labelled samples with computed factors.",
            "- `classification_summary.csv`: label-level performance summary.",
            "- `factor_by_class.csv`: factor distributions by manual classification.",
            "- `positive_negative_effects.csv`: positive-vs-reject factor separation.",
            "- `temporal_pattern_summary.csv`: post-entry time-structure labels by classification.",
            "- `temporal_positive_negative_effects.csv`: post-entry positive-vs-reject separation for reverse study only.",
            "- `rule_tests.csv`: first-pass candidate rule diagnostics against labelled positives/rejects.",
            "- `rule_review_score_v3_core.csv`: false-positive and missed-positive review table for the V3 core rule.",
            "- `keyword_summary.csv`: note keyword summary by classification.",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    classifications = _load_classifications(Path(args.classifications))
    trades = _load_trades(run_dir, classifications)
    signals = _load_signals(run_dir)
    entries = _entry_level(trades, signals)
    if entries.empty:
        raise RuntimeError("No labelled G2 entries found.")

    codes = sorted(entries["code"].dropna().astype(str).unique().tolist())
    start_date = str(entries["entry_date"].min())
    entry_end_date = str(entries["entry_date"].max())
    sell_end = pd.to_datetime(trades["sell_datetime"], errors="coerce").max()
    end_date = (sell_end + pd.Timedelta(days=20)).strftime("%Y-%m-%d") if pd.notna(sell_end) else entry_end_date
    daily = _load_daily(codes, start_date, end_date)
    entries = _add_daily_features(entries, daily)
    entries["preference_label"] = entries["classification"].map(_preference_label)

    for col in ["v4_rank", "v4_score", "mom5", "mom10", "mom20", "vol_ratio", "vol10", "volume_ratio", "breadth_ma20"]:
        if col in entries.columns:
            entries[f"sig_{col}"] = pd.to_numeric(entries[col], errors="coerce")
    entries = _add_composite_scores(entries)

    group_summary, factor_by_class = _summarize_groups(entries)
    effects = _positive_negative_effects(entries)
    temporal_summary = _temporal_pattern_summary(entries)
    temporal_effects = _temporal_positive_negative_effects(entries)
    rules = _rule_tests(entries)
    review = _rule_review(entries)
    keywords = _keyword_summary(classifications)

    entries.to_csv(output_dir / "entry_samples.csv", index=False, encoding="utf-8-sig")
    group_summary.to_csv(output_dir / "classification_summary.csv", index=False, encoding="utf-8-sig")
    factor_by_class.to_csv(output_dir / "factor_by_class.csv", index=False, encoding="utf-8-sig")
    effects.to_csv(output_dir / "positive_negative_effects.csv", index=False, encoding="utf-8-sig")
    temporal_summary.to_csv(output_dir / "temporal_pattern_summary.csv", index=False, encoding="utf-8-sig")
    temporal_effects.to_csv(output_dir / "temporal_positive_negative_effects.csv", index=False, encoding="utf-8-sig")
    rules.to_csv(output_dir / "rule_tests.csv", index=False, encoding="utf-8-sig")
    review.to_csv(output_dir / "rule_review_score_v3_core.csv", index=False, encoding="utf-8-sig")
    keywords.to_csv(output_dir / "keyword_summary.csv", index=False, encoding="utf-8-sig")

    summary = {
        "entry_count": int(len(entries)),
        "trade_leg_count": int(len(trades)),
        "start_date": start_date,
        "end_date": entry_end_date,
        "daily_feature_end_date": end_date,
        "class_counts": {str(k): int(v) for k, v in entries["classification"].value_counts().to_dict().items()},
        "output_dir": str(output_dir),
        "top_effects": effects.head(8).to_dict("records"),
        "top_temporal_effects": temporal_effects.head(8).to_dict("records"),
        "rule_tests": rules.to_dict("records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, entries, group_summary, effects, temporal_effects, keywords, rules)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Reverse-engineer labelled G2 trade preference factors.")
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--classifications", default=str(DEFAULT_CLASSIFICATIONS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
