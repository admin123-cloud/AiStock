from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_SHARE_CAP_CACHE = Path("data/runtime/tdx_share_cap_history.parquet")


def _num(value: Any, default: float = np.nan) -> float:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def load_share_cap_cache(path: str | Path | None = None) -> pd.DataFrame:
    cache = Path(path) if path else DEFAULT_SHARE_CAP_CACHE
    if not cache.exists():
        return pd.DataFrame(columns=["code", "trade_date", "total_share_wan", "float_share_wan"])
    d = pd.read_parquet(cache)
    d["code"] = d["code"].astype(str)
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["total_share_wan", "float_share_wan"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["code", "trade_date"]).sort_values(["code", "trade_date"]).reset_index(drop=True)


def _share_cap_lookup(share_cap: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if share_cap.empty:
        return {}
    return {code: g.reset_index(drop=True) for code, g in share_cap.groupby("code", sort=False)}


def _resolve_cap_yi(
    code: str,
    entry_date: str,
    entry_price: float,
    cap_by_code: dict[str, pd.DataFrame],
) -> tuple[float, str]:
    history = cap_by_code.get(code)
    if history is None or history.empty or entry_price <= 0:
        return np.nan, "missing"
    prior = history[history["trade_date"] <= entry_date].tail(1)
    if prior.empty:
        return np.nan, "missing"
    float_share = _num(prior.iloc[0].get("float_share_wan"))
    total_share = _num(prior.iloc[0].get("total_share_wan"))
    share_wan = float_share if math.isfinite(float_share) and float_share > 0 else total_share
    if not math.isfinite(share_wan) or share_wan <= 0:
        return np.nan, "missing"
    cap_yi = share_wan * 10000.0 * entry_price / 1e8
    if cap_yi < 100.0:
        return cap_yi, "lt100"
    if cap_yi <= 200.0:
        return cap_yi, "100_200"
    return cap_yi, "gt200"


def _pressure_features(history: pd.DataFrame, entry_date: str, entry_price: float, cap_bucket: str) -> dict[str, Any]:
    prior = history[history["trade_date"] < entry_date].tail(260).copy()
    result: dict[str, Any] = {
        "cap_pressure_lookback_days": np.nan,
        "cap_pressure_days": 0,
        "cap_big_pressure_days": 0,
        "cap_pressure_amount_share": np.nan,
        "overhead_pressure_days": 0,
        "overhead_big_pressure_days": 0,
        "overhead_pressure_amount_share": np.nan,
        "prior_max_high_ratio": np.nan,
        "runup_from_60d_low": np.nan,
        "recent60_return": np.nan,
        "cap_pressure_reject": False,
    }
    if prior.empty or entry_price <= 0:
        return result

    if cap_bucket == "lt100":
        lookback = 120
    elif cap_bucket in {"100_200", "gt200"}:
        lookback = 250
    else:
        lookback = 120
    window = prior.tail(lookback).copy()
    result["cap_pressure_lookback_days"] = int(len(window))
    if window.empty:
        return result

    amount = pd.to_numeric(window["amount"], errors="coerce").fillna(0.0)
    amount_total = float(amount.sum())
    amount_median = float(amount[amount > 0].median()) if (amount > 0).any() else 0.0
    amount_q70 = float(amount[amount > 0].quantile(0.70)) if (amount > 0).any() else 0.0
    high = pd.to_numeric(window["high"], errors="coerce")
    low = pd.to_numeric(window["low"], errors="coerce")
    open_ = pd.to_numeric(window["open"], errors="coerce")
    close = pd.to_numeric(window["close"], errors="coerce")

    pressure = (high >= entry_price * 0.98) & (low <= entry_price * 1.15)
    overhead_pressure = (high >= entry_price * 1.03) & (low <= entry_price * 1.25)
    wide_range = (high / low - 1.0).replace([np.inf, -np.inf], np.nan) >= 0.08
    weak_close = close <= (high + low) / 2.0
    significant = pressure & ((amount >= amount_median) | (wide_range & (amount >= amount_q70)) | weak_close)
    climax = pressure & (amount >= amount_q70) & (wide_range | (close < open_))
    overhead_significant = overhead_pressure & (
        (amount >= amount_median) | (wide_range & (amount >= amount_q70)) | weak_close
    )
    overhead_climax = overhead_pressure & (amount >= amount_q70) & (wide_range | (close < open_))

    pressure_days = int(pressure.fillna(False).sum())
    big_pressure_days = int((significant | climax).fillna(False).sum())
    pressure_amount_share = float(amount[pressure.fillna(False)].sum() / amount_total) if amount_total else np.nan
    overhead_days = int(overhead_pressure.fillna(False).sum())
    overhead_big_days = int((overhead_significant | overhead_climax).fillna(False).sum())
    overhead_amount_share = float(amount[overhead_pressure.fillna(False)].sum() / amount_total) if amount_total else np.nan
    prior_max_high = float(high.max()) if high.notna().any() else np.nan
    prior_max_high_ratio = prior_max_high / entry_price - 1.0 if math.isfinite(prior_max_high) else np.nan
    close60 = close.dropna().tail(60)
    recent60_return = close60.iloc[-1] / close60.iloc[0] - 1.0 if len(close60) >= 2 and close60.iloc[0] > 0 else np.nan
    runup_from_60d_low = entry_price / close60.min() - 1.0 if len(close60) >= 2 and close60.min() > 0 else np.nan

    result.update(
        {
            "cap_pressure_days": pressure_days,
            "cap_big_pressure_days": big_pressure_days,
            "cap_pressure_amount_share": pressure_amount_share,
            "overhead_pressure_days": overhead_days,
            "overhead_big_pressure_days": overhead_big_days,
            "overhead_pressure_amount_share": overhead_amount_share,
            "prior_max_high_ratio": prior_max_high_ratio,
            "runup_from_60d_low": runup_from_60d_low,
            "recent60_return": recent60_return,
        }
    )
    if cap_bucket == "lt100":
        reject = overhead_big_days >= 3 or overhead_amount_share >= 0.12
    elif cap_bucket == "100_200":
        reject = (overhead_big_days >= 4 and overhead_amount_share >= 0.04) or overhead_amount_share >= 0.16
    elif cap_bucket == "gt200":
        rebound_to_pressure = (
            overhead_big_days >= 1
            and math.isfinite(runup_from_60d_low)
            and math.isfinite(recent60_return)
            and math.isfinite(prior_max_high_ratio)
            and runup_from_60d_low >= 0.30
            and recent60_return <= 0.25
            and 0.03 <= prior_max_high_ratio <= 0.12
        )
        reject = overhead_big_days >= 10 or overhead_amount_share >= 0.10 or rebound_to_pressure
    else:
        reject = False
    result["cap_pressure_reject"] = bool(reject)
    return result


def _downtrend_rebound_reject(row: pd.Series, history: pd.DataFrame | None, entry_date: str, entry_price: float) -> tuple[bool, str]:
    rank_status = str(row.get("rank_change_status") or "")
    rank_change = _num(row.get("rank_change"), 0.0)
    score_change = _num(row.get("score_change"), 0.0)
    if rank_status == "down" and rank_change <= -50 and score_change <= -0.08:
        return True, "rank_collapse_downtrend_rebound"
    if history is None or history.empty or entry_price <= 0:
        return False, ""
    prior = history[history["trade_date"] < entry_date].tail(80).copy()
    if len(prior) < 40:
        return False, ""
    close = pd.to_numeric(prior["close"], errors="coerce")
    high = pd.to_numeric(prior["high"], errors="coerce")
    ma20 = close.tail(20).mean()
    ma60 = close.tail(60).mean()
    prior60_return = close.iloc[-1] / close.tail(60).iloc[0] - 1.0 if len(close.tail(60)) >= 2 else np.nan
    below_ma60_ratio = float((close.tail(60) < ma60).mean()) if math.isfinite(ma60) else 0.0
    close_under_ma20 = close.iloc[-1] < ma20 if math.isfinite(ma20) else False
    not_cleared_prior_high = entry_price < high.tail(20).max() * 1.01
    if prior60_return <= -0.12 and below_ma60_ratio >= 0.55 and close_under_ma20 and not_cleared_prior_high:
        return True, "daily_downtrend_rebound"
    return False, ""


def apply_user_v2_filters(
    candidates: pd.DataFrame,
    daily: pd.DataFrame,
    share_cap: pd.DataFrame | None = None,
    require_cap_data: bool = False,
) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    d = candidates.copy()
    d["code"] = d["code"].astype(str)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    daily_work = daily.copy()
    if not daily_work.empty:
        daily_work["code"] = daily_work["code"].astype(str)
        daily_work["trade_date"] = pd.to_datetime(daily_work["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in daily_work.columns:
                daily_work[col] = pd.to_numeric(daily_work[col], errors="coerce")
    by_code = {code: group.reset_index(drop=True) for code, group in daily_work.groupby("code", sort=False)}
    cap_by_code = _share_cap_lookup(share_cap if share_cap is not None else pd.DataFrame())

    rows: list[dict[str, Any]] = []
    for _, row in d.iterrows():
        code = str(row["code"])
        entry_date = str(row["entry_date"])
        entry_price = _num(row.get("entry_price"))
        history = by_code.get(code)
        cap_yi, cap_bucket = _resolve_cap_yi(code, entry_date, entry_price, cap_by_code)
        pressure = _pressure_features(history, entry_date, entry_price, cap_bucket) if history is not None else {}
        down_reject, down_reason = _downtrend_rebound_reject(row, history, entry_date, entry_price)
        pressure_amount_share = _num(pressure.get("cap_pressure_amount_share"), 0.0)
        pressure_days = int(_num(pressure.get("cap_pressure_days"), 0.0))
        big_pressure_days = int(_num(pressure.get("cap_big_pressure_days"), 0.0))
        downtrend_into_pressure = bool(
            down_reject
            and (
                bool(pressure.get("cap_pressure_reject"))
                or big_pressure_days >= 3
                or pressure_days >= 8
                or pressure_amount_share >= 0.20
            )
        )
        cap_missing_reject = require_cap_data and cap_bucket == "missing"
        reasons: list[str] = []
        if pressure.get("cap_pressure_reject"):
            reasons.append("cap_pressure_window")
        if downtrend_into_pressure:
            reasons.append("downtrend_rebound_into_pressure")
        if cap_missing_reject:
            reasons.append("share_cap_missing")
        rows.append(
            {
                "float_market_cap_yi": cap_yi,
                "cap_bucket": cap_bucket,
                **pressure,
                "downtrend_rebound_reject": bool(down_reject),
                "downtrend_rebound_reason": down_reason,
                "downtrend_rebound_into_pressure": downtrend_into_pressure,
                "share_cap_missing": cap_bucket == "missing",
                "user_v2_reject_reason": ",".join(reasons),
                "user_v2_pass": not reasons,
            }
        )
    out = pd.concat([d.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    return out


def summarize_filter_result(d: pd.DataFrame) -> dict[str, Any]:
    if d.empty:
        return {"signals_before": 0, "signals_after": 0}
    rejected = d[~d["user_v2_pass"].fillna(False)]
    reason_counts: dict[str, int] = {}
    for raw in rejected.get("user_v2_reject_reason", pd.Series(dtype=str)).fillna("").astype(str):
        for reason in [x for x in raw.split(",") if x]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "signals_before": int(len(d)),
        "signals_after": int(d["user_v2_pass"].fillna(False).sum()),
        "signals_rejected": int(len(rejected)),
        "reason_counts": reason_counts,
        "share_cap_missing": int(d.get("share_cap_missing", pd.Series(dtype=bool)).fillna(False).sum()),
    }
