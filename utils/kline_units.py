"""Shared market-data unit conversions.

The persisted daily contract is:

* stock/index ``volume``: lots (手)
* ``amount``: yuan (元)

Provider fields are not assumed to share that contract.  Every formal daily
writer should call one of the source-specific helpers below before staging or
persisting rows.
"""

from __future__ import annotations

import pandas as pd


DAILY_VOLUME_UNIT = "lots"
DAILY_AMOUNT_UNIT = "yuan"
_VOLUME_DIVISORS = {
    "lots": 1.0,
    "shares": 100.0,
}
_AMOUNT_MULTIPLIERS = {
    "yuan": 1.0,
    "thousand_yuan": 1000.0,
    "ten_thousand_yuan": 10000.0,
}

# Current xtdata/get_market_data standard volume is lots for stocks and indices.
# Raw DAT integers are a different source contract and must not reuse this rule.
QMT_DAILY_VOLUME_TO_LOTS = 1.0
QMT_DAILY_INDEX_VOLUME_TO_LOTS = 1.0
QMT_DAILY_AMOUNT_TO_YUAN = 1.0
QMT_DAILY_SOURCE_NAMES = frozenset({"qmt", "qmtmini", "qmt_xtquant", "xtquant"})
DAILY_UNIT_MISMATCH_CLASSES = frozenset(
    {
        "missing_both",
        "missing_amount",
        "missing_volume",
        "source_nonpositive",
        "too_large_100x",
        "too_small_100x",
        "amount_large_only",
        "amount_small_10000_only",
        "volume_large_only",
        "other",
    }
)
DAILY_UNIT_REPAIRABLE_CLASSES = frozenset(
    {
        "missing_both",
        "missing_amount",
        "missing_volume",
        "too_large_100x",
        "too_small_100x",
        "amount_large_only",
        "amount_small_10000_only",
        "volume_large_only",
    }
)


def classify_daily_unit_pair(
    db_volume: float,
    db_amount: float,
    source_volume: float,
    source_amount: float,
) -> str:
    """Classify persisted-vs-source unit ratios for one daily bar."""

    db_volume = float(db_volume or 0.0)
    db_amount = float(db_amount or 0.0)
    source_volume = float(source_volume or 0.0)
    source_amount = float(source_amount or 0.0)
    if source_volume <= 0.0 or source_amount <= 0.0:
        return "source_nonpositive"
    if db_volume <= 0.0 and db_amount <= 0.0:
        return "missing_both"
    if db_volume <= 0.0:
        return "missing_volume"
    if db_amount <= 0.0:
        return "missing_amount"
    volume_ratio = db_volume / source_volume if source_volume > 0 else 0.0
    amount_ratio = db_amount / source_amount if source_amount > 0 else 0.0
    if 0.8 <= volume_ratio <= 1.2 and 0.8 <= amount_ratio <= 1.2:
        return "ok"
    if 80.0 <= volume_ratio <= 120.0 and 80.0 <= amount_ratio <= 120.0:
        return "too_large_100x"
    if 0.008 <= volume_ratio <= 0.012 and 0.008 <= amount_ratio <= 0.012:
        return "too_small_100x"
    if 0.8 <= volume_ratio <= 1.2 and 80.0 <= amount_ratio <= 120.0:
        return "amount_large_only"
    if 80.0 <= volume_ratio <= 120.0 and 0.00008 <= amount_ratio <= 0.00012:
        return "amount_small_10000_only"
    if 80.0 <= volume_ratio <= 120.0 and 0.8 <= amount_ratio <= 1.2:
        return "volume_large_only"
    return "other"


def normalize_daily_units(
    frame: pd.DataFrame,
    *,
    volume_unit: str = DAILY_VOLUME_UNIT,
    amount_unit: str = DAILY_AMOUNT_UNIT,
) -> pd.DataFrame:
    """Convert one provider frame into the persisted lots/yuan contract."""

    volume_key = str(volume_unit or DAILY_VOLUME_UNIT).strip().lower()
    amount_key = str(amount_unit or DAILY_AMOUNT_UNIT).strip().lower()
    if volume_key not in _VOLUME_DIVISORS:
        raise ValueError(f"unsupported daily volume unit: {volume_unit!r}")
    if amount_key not in _AMOUNT_MULTIPLIERS:
        raise ValueError(f"unsupported daily amount unit: {amount_unit!r}")

    result = frame.copy()
    if "volume" in result.columns:
        result["volume"] = (
            pd.to_numeric(result["volume"], errors="coerce").fillna(0.0)
            / _VOLUME_DIVISORS[volume_key]
        )
    if "amount" in result.columns:
        result["amount"] = (
            pd.to_numeric(result["amount"], errors="coerce").fillna(0.0)
            * _AMOUNT_MULTIPLIERS[amount_key]
        )
    return result


def normalize_qmt_daily_units(
    frame: pd.DataFrame,
    *,
    instrument_type: str = "stock",
) -> pd.DataFrame:
    """Normalize SDK standard volume/amount; security type does not change units."""
    return normalize_daily_units(frame, volume_unit=MINUTE_UNIT_CONTRACT['qmt_sdk_daily_volume_unit'], amount_unit="yuan")



def normalize_qmt_intraday_units(
    frame: pd.DataFrame,
    *,
    instrument_type: str = "stock",
) -> pd.DataFrame:
    """Normalize legacy raw QMT DAT fields; SDK/tick.volume use minute helpers."""

    return normalize_daily_units(frame, volume_unit="shares" if str(instrument_type).lower() == "index" else "lots", amount_unit="yuan")


def normalize_tdxquant_daily_units(
    frame: pd.DataFrame,
    *,
    instrument_type: str = "stock",
) -> pd.DataFrame:
    """Normalize TDXQuant/TQCenter daily fields.

    The TDXQuant/TQCenter API exposes daily volume in lots and amount in yuan,
    for both stocks and indexes.  Keeping this as an explicit no-op makes the
    source contract visible at every formal write boundary and prevents a
    later caller from silently guessing a different scale.
    """

    _ = instrument_type
    return normalize_daily_units(frame, volume_unit="lots", amount_unit="yuan")


def normalize_tdx_day_units(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize raw local TDX ``.day`` records to lots/yuan."""

    return normalize_daily_units(frame, volume_unit="shares", amount_unit="yuan")


def normalize_baostock_daily_units(frame: pd.DataFrame) -> pd.DataFrame:
    """Baostock daily volume is shares; amount is yuan."""

    return normalize_daily_units(frame, volume_unit="shares", amount_unit="yuan")


def normalize_akshare_daily_units(frame: pd.DataFrame) -> pd.DataFrame:
    """AkShare A-share daily fields are lots/yuan."""

    return normalize_daily_units(frame, volume_unit="lots", amount_unit="yuan")


def normalize_tushare_daily_units(frame: pd.DataFrame) -> pd.DataFrame:
    """Tushare ``daily.amount`` is thousand yuan; ``vol`` is lots."""

    return normalize_daily_units(frame, volume_unit="lots", amount_unit="thousand_yuan")


# Native QMT minute fields and standardized tick.volume agree in lots for
# sampled SH/SZ/BJ stocks and indices. Raw pvolume is exchange-dependent.
import json as _json
import math as _math
from pathlib import Path as _Path
MINUTE_UNIT_CONTRACT = _json.loads((_Path(__file__).resolve().parents[1]/'config/minute_units.json').read_text(encoding='utf-8'))
MINUTE_UNIT_CONTRACT_ID = MINUTE_UNIT_CONTRACT['contract_id']


def normalize_qmt_minute_values(volume, amount, *, volume_unit=None, amount_unit=None):
    """Convert explicit source units only; never fit units against old database rows."""
    volume_unit = volume_unit or MINUTE_UNIT_CONTRACT['qmt_history_volume_unit']
    amount_unit = amount_unit or MINUTE_UNIT_CONTRACT['amount_unit']
    if volume_unit not in ('lots','shares') or amount_unit != 'yuan':
        raise ValueError('unverified_minute_source_units')
    try:
        volume, amount = float(volume), float(amount)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('minute_volume_amount_missing_or_invalid') from exc
    if not _math.isfinite(volume) or not _math.isfinite(amount) or volume < 0 or amount < 0:
        raise ValueError('minute_volume_amount_missing_or_invalid')
    return volume / (100.0 if volume_unit == 'shares' else 1.0), amount


def qmt_tick_lots_yuan(tick):
    # pvolume is shares in SH/SZ samples but already lots in BJ samples.
    # A missing standardized field is unknown, not zero and not a /100 guess.
    if MINUTE_UNIT_CONTRACT['qmt_tick_volume_field'] not in tick:
        raise ValueError('qmt_tick_volume_unit_unverified')
    return normalize_qmt_minute_values(tick.get('volume'),tick.get('amount'),
                                      volume_unit=MINUTE_UNIT_CONTRACT['qmt_tick_volume_unit'])
