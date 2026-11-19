"""Pure intraday-to-daily quality-gate primitives.

This module deliberately has no ClickHouse, filesystem, or network dependency.
It is the small, deterministic contract used by read-only audits before a
candidate package is built.  Promotion and formal-table writes belong to a
separate, explicitly authorized workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from utils.ocean_stage import STANDARD_5M_MINUTES


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
EXPECTED_UNITS = "volume_lots/amount_yuan"
EXPECTED_ADJUSTMENT = "none"
EXPECTED_BARS_BY_PERIOD = {"5m": 48, "15m": 16, "30m": 8, "60m": 4}


@dataclass(frozen=True)
class DailyAggregate:
    """The deterministic daily projection of a complete intraday code-day."""

    open: float
    high: float
    low: float
    close: float
    volume_lots: float
    amount_yuan: float


@dataclass(frozen=True)
class QualityGateResult:
    """Auditable result for one canonical code and trade date."""

    status: str
    reasons: tuple[str, ...]
    minute_rows: int
    unique_datetime_rows: int
    actual_slots: tuple[int, ...]
    aggregate: DailyAggregate | None
    price_relative_diff: float | None
    volume_relative_diff: float | None
    amount_relative_diff: float | None
    auction_residual_volume_lots: float | None
    auction_residual_amount_yuan: float | None


def expected_datetimes(trade_date: date, period: str = "5m") -> tuple[datetime, ...]:
    """Return exact Shanghai session-end timestamps for one code-day."""

    if period not in EXPECTED_BARS_BY_PERIOD:
        raise ValueError(f"unsupported_period={period!r}")
    group_width = int(period[:-1]) // 5
    slots = tuple(
        STANDARD_5M_MINUTES[index + group_width - 1]
        for index in range(0, len(STANDARD_5M_MINUTES), group_width)
    )

    return tuple(
        datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            minute // 60,
            minute % 60,
            tzinfo=BUSINESS_TZ,
        )
        for minute in slots
    )


def _datetime_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BUSINESS_TZ)
    return parsed.astimezone(BUSINESS_TZ)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _field(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def aggregate_minute_to_daily(rows: Sequence[Mapping[str, Any]]) -> DailyAggregate:
    """Aggregate complete intraday rows using the project OHLCV contract.

    The function is intentionally strict: it raises ``ValueError`` for an
    empty or malformed input rather than silently producing a partial daily
    bar.  Completeness and source eligibility are checked by
    :func:`evaluate_code_day` before this function is called by the gate.
    """

    if not rows:
        raise ValueError("cannot_aggregate_empty_minute_rows")
    ordered = sorted(rows, key=lambda row: _datetime_value(row["datetime"]))
    values: list[tuple[float, float, float, float, float, float]] = []
    for row in ordered:
        fields = tuple(
            _number(_field(row, name))
            for name in ("open", "high", "low", "close")
        ) + (
            _number(_field(row, "source_volume_lots", "volume")),
            _number(_field(row, "source_amount_yuan", "amount")),
        )
        if any(value is None for value in fields):
            raise ValueError("cannot_aggregate_invalid_minute_values")
        values.append(tuple(float(value) for value in fields))  # type: ignore[arg-type]
    return DailyAggregate(
        open=values[0][0],
        high=max(value[1] for value in values),
        low=min(value[2] for value in values),
        close=values[-1][3],
        volume_lots=sum(value[4] for value in values),
        amount_yuan=sum(value[5] for value in values),
    )


def aggregate_5m_to_daily(rows: Sequence[Mapping[str, Any]]) -> DailyAggregate:
    """Backward-compatible alias for :func:`aggregate_minute_to_daily`."""

    return aggregate_minute_to_daily(rows)


def _relative_difference(value: float, reference: float) -> float:
    return abs(value - reference) / max(abs(reference), 1.0)


def _daily_values(daily: Mapping[str, Any]) -> tuple[float, float, float, float, float, float] | None:
    fields = tuple(
        _number(_field(daily, name))
        for name in ("open", "high", "low", "close", "volume_lots", "amount_yuan")
    )
    if any(value is None for value in fields):
        return None
    return tuple(float(value) for value in fields)  # type: ignore[arg-type]


def evaluate_code_day(
    rows: Sequence[Mapping[str, Any]],
    *,
    trade_date: date,
    daily: Mapping[str, Any] | None,
    source_units: str | None = EXPECTED_UNITS,
    price_adjustment: str | None = EXPECTED_ADJUSTMENT,
    daily_rows: int = 1,
    evidence_complete: bool = True,
    period: str = "5m",
    minute_source_family: str | None = None,
    daily_source_family: str | None = None,
) -> QualityGateResult:
    """Evaluate one code-day without side effects.

    Auction/session-boundary residuals are informational: price fields need
    only remain inside the daily low/high envelope, and minute volume/amount
    may be lower than daily totals by any percentage. Minute totals exceeding
    daily totals, incomplete structure, invalid bars, or unconfirmed/mismatched
    source families remain blocking. Relative differences are retained solely
    as diagnostics, not as acceptance thresholds.
    """

    reasons: list[str] = []
    fatal: list[str] = []
    datetimes: list[datetime] = []

    if period not in EXPECTED_BARS_BY_PERIOD:
        return QualityGateResult(
            status="review",
            reasons=(f"unsupported_period={period!r}",),
            minute_rows=len(rows),
            unique_datetime_rows=0,
            actual_slots=(),
            aggregate=None,
            price_relative_diff=None,
            volume_relative_diff=None,
            amount_relative_diff=None,
            auction_residual_volume_lots=None,
            auction_residual_amount_yuan=None,
        )
    expected_count = EXPECTED_BARS_BY_PERIOD[period]

    for row in rows:
        try:
            parsed = _datetime_value(row["datetime"])
        except (KeyError, TypeError, ValueError, OverflowError):
            fatal.append("invalid_datetime")
            continue
        datetimes.append(parsed)
        if parsed.date() != trade_date:
            fatal.append("datetime_trade_date_mismatch")
        if parsed.second != 0 or parsed.microsecond != 0:
            fatal.append("datetime_seconds_not_zero")

        ohlc = tuple(_number(row.get(name)) for name in ("open", "high", "low", "close"))
        if any(value is None or value <= 0 for value in ohlc):
            fatal.append("invalid_positive_ohlc")
        elif not (
            ohlc[2] <= min(ohlc[0], ohlc[3])
            and ohlc[1] >= max(ohlc[0], ohlc[3])
            and ohlc[1] >= ohlc[2]
        ):
            fatal.append("ohlc_relation_failed")

        volume = _number(_field(row, "source_volume_lots", "volume"))
        amount = _number(_field(row, "source_amount_yuan", "amount"))
        if volume is None or amount is None:
            fatal.append("missing_or_nonfinite_quantity")
        elif volume < 0 or amount < 0:
            fatal.append("negative_quantity")
        row_adjustment = row.get("price_adjustment")
        if row_adjustment is not None and str(row_adjustment) != EXPECTED_ADJUSTMENT:
            reasons.append(f"price_adjustment_unconfirmed={row_adjustment!r}")

    unique_datetimes = len(set(datetimes))
    actual_slots = tuple(sorted({item.hour * 60 + item.minute for item in datetimes}))
    expected_slots = {
        item.hour * 60 + item.minute for item in expected_datetimes(trade_date, period)
    }
    if len(rows) != expected_count:
        reasons.append(f"minute_rows={len(rows)}; expected={expected_count}")
    if unique_datetimes != expected_count:
        # A missing bar is an incomplete source and therefore review.  A
        # duplicate timestamp is deterministic corruption and is rejected
        # below; do not collapse both cases into the same status.
        reasons.append(f"unique_datetime={unique_datetimes}; expected={expected_count}")
    if set(actual_slots) != expected_slots:
        missing = sorted(expected_slots - set(actual_slots))
        unexpected = sorted(set(actual_slots) - expected_slots)
        reasons.append(f"standard_5m_slots_mismatch; missing={missing}; unexpected={unexpected}")
    if len(datetimes) != len(set(datetimes)):
        fatal.append("duplicate_datetime")
    if not evidence_complete:
        reasons.append("evidence_incomplete")
    if source_units != EXPECTED_UNITS:
        reasons.append(f"units_unconfirmed={source_units!r}")
    if price_adjustment != EXPECTED_ADJUSTMENT:
        reasons.append(f"price_adjustment_unconfirmed={price_adjustment!r}")
    minute_family = str(minute_source_family or "").strip().casefold()
    daily_family = str(daily_source_family or "").strip().casefold()
    if not minute_family:
        reasons.append("minute_source_family_unconfirmed")
    if not daily_family:
        reasons.append("daily_source_family_unconfirmed")
    if minute_family and daily_family and minute_family != daily_family:
        reasons.append(
            f"source_family_mismatch={minute_source_family!r}!={daily_source_family!r}"
        )

    aggregate: DailyAggregate | None = None
    price_ratio = volume_ratio = amount_ratio = None
    auction_volume = auction_amount = None
    structure_ready = not fatal and len(rows) == expected_count and set(actual_slots) == expected_slots
    if structure_ready:
        try:
            aggregate = aggregate_minute_to_daily(rows)
        except ValueError as exc:
            fatal.append(str(exc))
        else:
            if daily is None:
                reasons.append("daily_missing")
            elif daily_rows != 1:
                reasons.append(f"daily_rows={daily_rows}; expected=1")
            else:
                reference = _daily_values(daily)
                if reference is None:
                    reasons.append("daily_values_invalid_or_missing")
                else:
                    d_open, d_high, d_low, d_close, d_volume, d_amount = reference
                    if min(d_open, d_high, d_low, d_close) <= 0 or d_high < max(d_open, d_close) or d_low > min(d_open, d_close) or d_high < d_low:
                        reasons.append("daily_ohlc_invalid")
                    if d_volume < 0 or d_amount < 0:
                        reasons.append("daily_quantity_invalid")
                    price_ratio = max(
                        _relative_difference(aggregate.open, d_open),
                        _relative_difference(aggregate.high, d_high),
                        _relative_difference(aggregate.low, d_low),
                        _relative_difference(aggregate.close, d_close),
                    )
                    volume_ratio = _relative_difference(aggregate.volume_lots, d_volume)
                    amount_ratio = _relative_difference(aggregate.amount_yuan, d_amount)
                    price_epsilon = max(abs(d_high), abs(d_low), 1.0) * 1e-12
                    if aggregate.high > d_high + price_epsilon:
                        reasons.append("minute_high_exceeds_daily_high")
                    if aggregate.low < d_low - price_epsilon:
                        reasons.append("minute_low_below_daily_low")
                    # Only machine-scale summation noise is tolerated; there
                    # is no percentage threshold for lower minute totals.
                    volume_epsilon = max(abs(d_volume), 1.0) * 1e-12
                    amount_epsilon = max(abs(d_amount), 1.0) * 1e-12
                    if aggregate.volume_lots > d_volume + volume_epsilon:
                        reasons.append("minute_volume_exceeds_daily")
                    if aggregate.amount_yuan > d_amount + amount_epsilon:
                        reasons.append("minute_amount_exceeds_daily")
                    if d_volume > 0 and aggregate.volume_lots <= 0:
                        reasons.append("minute_volume_zero_despite_daily_trade")
                    if d_amount > 0 and aggregate.amount_yuan <= 0:
                        reasons.append("minute_amount_zero_despite_daily_trade")
                    auction_volume = max(d_volume - aggregate.volume_lots, 0.0)
                    auction_amount = max(d_amount - aggregate.amount_yuan, 0.0)

    if fatal:
        status = "rejected"
        reasons = fatal + reasons
    elif reasons:
        status = "review"
    else:
        status = "accepted"
    return QualityGateResult(
        status=status,
        reasons=tuple(reasons),
        minute_rows=len(rows),
        unique_datetime_rows=unique_datetimes,
        actual_slots=actual_slots,
        aggregate=aggregate,
        price_relative_diff=price_ratio,
        volume_relative_diff=volume_ratio,
        amount_relative_diff=amount_ratio,
        auction_residual_volume_lots=auction_volume,
        auction_residual_amount_yuan=auction_amount,
    )
