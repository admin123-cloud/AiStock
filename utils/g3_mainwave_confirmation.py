"""Canonical G3 Score88 first completed 30m confirmation.

The formal contract has one intraday admission rule.  Keeping it here avoids
the runtime, smoke test, and research replay drifting into different meanings
of "30m confirmation".
"""

from __future__ import annotations

from typing import Any

import pandas as pd


FORMAL_CONFIRMATION_NAME = "first_completed_30m_volume_breakout_prior20_high"
CONFIRMATION_PERIOD_MINUTES = 30
FIRST_COMPLETED_BAR_TIME = "10:00:00"
PRIOR_BAR_COUNT = 20
MIN_AMOUNT_RATIO = 1.20
AMOUNT_FIELD = "amount"


def confirmation_contract_metadata() -> dict[str, Any]:
    """Return the machine-readable interpretation of the formal rule."""

    return {
        "name": FORMAL_CONFIRMATION_NAME,
        "period_minutes": CONFIRMATION_PERIOD_MINUTES,
        "first_completed_bar_time": FIRST_COMPLETED_BAR_TIME,
        "prior_bar_count": PRIOR_BAR_COUNT,
        "amount_field": AMOUNT_FIELD,
        "minimum_amount_ratio": MIN_AMOUNT_RATIO,
        "require_bullish_bar": True,
        "require_close_at_or_above_prior_high": True,
        "later_bars_may_not_replace_first_bar": True,
        "timezone": "Asia/Shanghai",
    }


def _empty_result(status: str, *, entry_date: str, detail: str = "") -> dict[str, Any]:
    return {
        "confirmed": False,
        "status": status,
        "detail": detail,
        "entry_date": str(entry_date)[:10],
        "confirm_datetime": "",
        "confirm_price": None,
        "amount_ratio": None,
        "prior20_high": None,
        "prior20_amount_avg": None,
        "observed_first_bar": False,
        "rule": FORMAL_CONFIRMATION_NAME,
        "confirmation_contract": confirmation_contract_metadata(),
    }


def confirm_first_completed_30m_breakout(
    bars: pd.DataFrame,
    entry_date: str,
    *,
    amount_column: str = AMOUNT_FIELD,
) -> dict[str, Any]:
    """Evaluate exactly the first completed 30m bar on ``entry_date``.

    A-share 30m bars are represented by their closing wall-clock time, so the
    first complete bar is the 10:00 bar.  The function deliberately does not
    scan 10:30 or later as a substitute when 10:00 fails.  A missing or
    malformed bar is a data-wall condition, while a well-formed bar that fails
    the rule is a normal strategy rejection.
    """

    entry_text = str(entry_date or "")[:10]
    day = pd.to_datetime(entry_text, errors="coerce")
    if pd.isna(day):
        return _empty_result("invalid_entry_date", entry_date=entry_text)
    if bars is None or bars.empty:
        return _empty_result("minute_data_unavailable", entry_date=entry_text)

    work = bars.copy()
    datetime_series = work.get("business_datetime")
    if datetime_series is None:
        datetime_series = work.get("datetime")
    if datetime_series is None:
        return _empty_result("minute_datetime_missing", entry_date=entry_text)
    work["_datetime"] = pd.to_datetime(datetime_series, errors="coerce")
    work["_date"] = work["_datetime"].dt.strftime("%Y-%m-%d")
    work["_time"] = work["_datetime"].dt.strftime("%H:%M:%S")
    for column in ["open", "high", "close", amount_column]:
        if column not in work.columns:
            work[column] = pd.NA
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.dropna(subset=["_datetime"]).sort_values("_datetime").reset_index(drop=True)

    day_rows = work[
        work["_date"].eq(day.strftime("%Y-%m-%d"))
        & work["_time"].eq(FIRST_COMPLETED_BAR_TIME)
    ].copy()
    if day_rows.empty:
        return _empty_result(
            "first_completed_30m_bar_missing",
            entry_date=entry_text,
            detail="expected first completed 30m bar at 10:00:00 was not visible",
        )

    first = day_rows.iloc[0]
    confirm_datetime = pd.Timestamp(first["_datetime"])
    prior = work[work["_datetime"].lt(confirm_datetime)].tail(PRIOR_BAR_COUNT).copy()
    if len(prior) != PRIOR_BAR_COUNT:
        result = _empty_result(
            "insufficient_prior_30m_bars",
            entry_date=entry_text,
            detail=f"need {PRIOR_BAR_COUNT} prior completed 30m bars, got {len(prior)}",
        )
        result.update(
            {
                "observed_first_bar": True,
                "confirm_datetime": confirm_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "observed_close": _number(first.get("close")),
                "observed_open": _number(first.get("open")),
                "observed_amount": _number(first.get(amount_column)),
            }
        )
        return result

    prior_high = pd.to_numeric(prior["high"], errors="coerce").max()
    prior_amount_avg = pd.to_numeric(prior[amount_column], errors="coerce").mean()
    close = _number(first.get("close"))
    open_price = _number(first.get("open"))
    amount = _number(first.get(amount_column))
    amount_ratio = amount / prior_amount_avg if amount is not None and prior_amount_avg and pd.notna(prior_amount_avg) else None
    missing_values = [
        name
        for name, value in {
            "open": open_price,
            "close": close,
            "amount": amount,
            "prior_high": _number(prior_high),
            "prior_amount_avg": _number(prior_amount_avg),
        }.items()
        if value is None
    ]
    if missing_values:
        result = _empty_result(
            "first_completed_30m_bar_values_missing",
            entry_date=entry_text,
            detail="missing values: " + ",".join(missing_values),
        )
        result.update(
            {
                "observed_first_bar": True,
                "confirm_datetime": confirm_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "observed_close": close,
                "observed_open": open_price,
                "observed_amount": amount,
                "prior20_high": _number(prior_high),
                "prior20_amount_avg": _number(prior_amount_avg),
                "amount_ratio": amount_ratio,
            }
        )
        return result

    bullish = bool(close >= open_price)
    above_prior_high = bool(close >= float(prior_high))
    volume_ok = bool(amount_ratio >= MIN_AMOUNT_RATIO) if amount_ratio is not None else False
    confirmed = bullish and above_prior_high and volume_ok
    status = "confirmed" if confirmed else "first_completed_30m_bar_not_confirmed"
    result = {
        "confirmed": confirmed,
        "status": status,
        "detail": "" if confirmed else "first 10:00 bar failed one or more formal conditions",
        "entry_date": entry_text,
        "confirm_datetime": confirm_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        "confirm_price": close,
        "amount_ratio": amount_ratio,
        "prior20_high": float(prior_high),
        "prior20_amount_avg": float(prior_amount_avg),
        "observed_first_bar": True,
        "observed_open": open_price,
        "observed_close": close,
        "observed_amount": amount,
        "bullish_bar": bullish,
        "close_at_or_above_prior20_high": above_prior_high,
        "amount_ratio_ok": volume_ok,
        "rule": FORMAL_CONFIRMATION_NAME,
        "confirmation_contract": confirmation_contract_metadata(),
    }
    return result


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def is_confirmation_data_wall(status: Any) -> bool:
    return str(status or "") in {
        "minute_data_unavailable",
        "minute_query_failed",
        "data_conflict",
        "missing_code",
        "minute_datetime_missing",
        "missing_minute_bars",
        "insufficient_minute_bars",
        "first_completed_30m_bar_missing",
        "insufficient_prior_30m_bars",
        "first_completed_30m_bar_values_missing",
        "candidate_snapshot_required_not_loaded",
        "snapshot_read_failed",
        "snapshot_empty_or_invalid",
        "snapshot_write_failed",
        "invalid_entry_date",
    }
