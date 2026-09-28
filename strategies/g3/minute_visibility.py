from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from utils.trading_sessions import BUSINESS_TZ, completed_bar_times

import pandas as pd

from utils.market_warehouse import clickhouse_query_df


DEFAULT_STAGE_TABLE = "qmt_xtquant_minute_stage"
PERIOD_TO_MINUTES = {5: 5, 15: 15, 30: 30, 60: 60}
VALUE_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]


def valid_bar_times(period: int) -> set[str]:
    minutes = PERIOD_TO_MINUTES.get(int(period))
    return set(completed_bar_times(minutes)) if minutes else set()


def _safe_query(
    query_fn: Callable[..., pd.DataFrame],
    sql: str,
    params: list[Any],
) -> tuple[pd.DataFrame, bool, str]:
    try:
        result = query_fn(sql, params)
        return (result.copy() if isinstance(result, pd.DataFrame) else pd.DataFrame(), True, "")
    except Exception as exc:
        return pd.DataFrame(), False, f"{type(exc).__name__}: {exc}"


def _session_score(values: pd.Series) -> pd.Series:
    minutes = values.dt.hour * 60 + values.dt.minute
    am = (minutes > 9 * 60 + 30) & (minutes <= 11 * 60 + 30)
    pm = (minutes > 13 * 60) & (minutes <= 15 * 60)
    return (am | pm).astype(int)


def _normalize_frame(frame: pd.DataFrame, period: int, source: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["code", "business_datetime", *VALUE_COLUMNS, "source"])

    work = frame.copy()
    if "code" not in work.columns:
        work["code"] = ""
    work["code"] = work["code"].fillna("").astype(str).str.strip()

    raw = pd.to_datetime(work.get("datetime"), errors="coerce")
    epoch = pd.to_numeric(work.get("datetime_epoch"), errors="coerce")
    has_epoch = epoch.notna()
    if has_epoch.any():
        utc = pd.to_datetime(epoch, unit="s", errors="coerce", utc=True)
        utc_wall = utc.dt.tz_localize(None)
        shanghai_wall = utc.dt.tz_convert(BUSINESS_TZ).dt.tz_localize(None)
        valid_times = valid_bar_times(period)
        utc_valid = utc_wall.dt.strftime("%H:%M").isin(valid_times)
        shanghai_valid = shanghai_wall.dt.strftime("%H:%M").isin(valid_times)
        choose_utc = utc_valid & ~shanghai_valid
        choose_shanghai = shanghai_valid & ~utc_valid
        session_utc = _session_score(utc_wall)
        session_shanghai = _session_score(shanghai_wall)
        choose_utc = choose_utc | (~choose_shanghai & (session_utc > session_shanghai))
        normalized = shanghai_wall.where(~choose_utc, utc_wall)
        normalized = normalized.where(has_epoch, raw)
    else:
        if getattr(raw.dt, "tz", None) is not None:
            normalized = raw.dt.tz_convert(BUSINESS_TZ).dt.tz_localize(None)
        else:
            normalized = raw

    work["business_datetime"] = pd.to_datetime(normalized, errors="coerce").astype("datetime64[ns]")
    for column in VALUE_COLUMNS:
        if column not in work.columns:
            work[column] = pd.NA
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work["source"] = source
    work = work.dropna(subset=["code", "business_datetime", "close"]).copy()
    work = work[work["code"].ne("")].copy()
    work = work.drop_duplicates(["code", "business_datetime"], keep="last")
    return work[["code", "business_datetime", *VALUE_COLUMNS, "source"]].sort_values(
        ["code", "business_datetime"]
    )


def _query_minute_source(
    query_fn: Callable[..., pd.DataFrame],
    table: str,
    code: str,
    period: int,
    start_date: str,
    end_date: str,
    source: str,
    *,
    stage: bool = False,
) -> tuple[pd.DataFrame, bool, str]:
    period_filter = "AND period = ?" if stage else ""
    sql = f"""
        SELECT
            code,
            datetime,
            toUnixTimestamp(datetime) AS datetime_epoch,
            open,
            high,
            low,
            close,
            volume,
            amount
        FROM {table} FINAL
        WHERE code = ?
          {period_filter}
          AND datetime >= toDateTime(?) - INTERVAL 2 DAY
          AND datetime < toDateTime(?) + INTERVAL 3 DAY
        ORDER BY datetime
    """
    params = [code]
    if stage:
        params.append(f"{int(period)}m")
    params.extend([start_date + " 00:00:00", end_date + " 00:00:00"])
    frame, ok, error = _safe_query(query_fn, sql, params)
    if not ok:
        return pd.DataFrame(), False, error
    return frame, True, ""


def _conflict_keys(main: pd.DataFrame, stage: pd.DataFrame, tolerance: float = 1e-6) -> list[str]:
    if main.empty or stage.empty:
        return []
    left = main.rename(columns={column: f"{column}_main" for column in VALUE_COLUMNS})
    right = stage.rename(columns={column: f"{column}_stage" for column in VALUE_COLUMNS})
    merged = left.merge(right, on=["code", "business_datetime"], how="inner")
    if merged.empty:
        return []
    conflict = pd.Series(False, index=merged.index)
    for column in VALUE_COLUMNS:
        a = pd.to_numeric(merged[f"{column}_main"], errors="coerce")
        b = pd.to_numeric(merged[f"{column}_stage"], errors="coerce")
        both = a.notna() & b.notna()
        conflict = conflict | (both & (a - b).abs().gt(float(tolerance)))
    return [
        f"{row.code}|{pd.Timestamp(row.business_datetime):%Y-%m-%d %H:%M:%S}"
        for row in merged.loc[conflict, ["code", "business_datetime"]].itertuples(index=False)
    ]


def load_visible_minute_bars(
    code: str,
    period: int,
    start_date: str,
    end_date: str,
    *,
    query_fn: Callable[..., pd.DataFrame] = clickhouse_query_df,
    main_table: str | None = None,
    stage_table: str = DEFAULT_STAGE_TABLE,
    value_tolerance: float = 1e-6,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    code = str(code or "").strip()
    period = int(period)
    main_table = main_table or f"kline_minute_{period}"
    if not code:
        return pd.DataFrame(), {
            "status": "missing_code",
            "source": "",
            "source_ok": False,
            "main_query_ok": False,
            "stage_query_ok": False,
        }

    main_raw, main_query_ok, main_error = _query_minute_source(
        query_fn, main_table, code, period, start_date, end_date, "main_final"
    )
    stage_raw, stage_query_ok, stage_error = _query_minute_source(
        query_fn, stage_table, code, period, start_date, end_date, "stage_final", stage=True
    )
    main = _normalize_frame(main_raw, period, "main_final")
    stage = _normalize_frame(stage_raw, period, "stage_final")
    conflicts = _conflict_keys(main, stage, tolerance=value_tolerance)

    main_keys = set(zip(main["code"], main["business_datetime"])) if not main.empty else set()
    stage_keys = set(zip(stage["code"], stage["business_datetime"])) if not stage.empty else set()
    stage_only = stage_keys - main_keys

    if conflicts:
        combined = pd.concat([main, stage], ignore_index=True, sort=False)
        status = "data_conflict"
        source = "conflict"
        source_ok = False
    elif not main.empty and not stage.empty:
        combined = pd.concat([main, stage], ignore_index=True, sort=False)
        combined = combined.drop_duplicates(["code", "business_datetime"], keep="last").sort_values(
            ["code", "business_datetime"]
        )
        status = "recovered_from_stage" if stage_only else "main_final_and_stage_verified"
        source = "main_plus_stage_recovery" if stage_only else "main_final"
        source_ok = True
    elif not main.empty:
        combined = main.copy()
        status = "main_final" if stage_query_ok else "main_final_stage_unavailable"
        source = "main_final"
        source_ok = True
    elif not stage.empty:
        combined = stage.copy()
        status = "recovered_from_stage"
        source = "stage_fallback"
        source_ok = True
    else:
        combined = pd.DataFrame()
        status = "minute_data_unavailable" if main_query_ok and stage_query_ok else "minute_query_failed"
        source = ""
        source_ok = False

    if not combined.empty:
        combined["business_date"] = combined["business_datetime"].dt.strftime("%Y-%m-%d")
        target = combined[combined["business_date"].between(start_date, end_date)].copy()
    else:
        target = combined.copy()
        target["business_date"] = pd.Series(dtype=str)
    day_counts = (
        target.groupby("business_date", dropna=False).size().astype(int).to_dict()
        if not target.empty
        else {}
    )
    meta = {
        "status": status,
        "source": source,
        "source_ok": bool(source_ok),
        "main_query_ok": bool(main_query_ok),
        "stage_query_ok": bool(stage_query_ok),
        "main_query_error": main_error,
        "stage_query_error": stage_error,
        "main_rows": int(len(main)),
        "stage_rows": int(len(stage)),
        "combined_rows": int(len(target)),
        "main_codes": int(main["code"].nunique()) if not main.empty else 0,
        "stage_codes": int(stage["code"].nunique()) if not stage.empty else 0,
        "recovered_rows": int(len(stage_only)),
        "conflict_rows": int(len(conflicts)),
        "conflict_keys": conflicts[:20],
        "day_counts": {str(key): int(value) for key, value in day_counts.items()},
        "expected_bars_per_day": int(len(valid_bar_times(period))),
        "timezone": "Asia/Shanghai",
        "main_table": main_table,
        "stage_table": stage_table,
        "code": code,
        "start_date": start_date,
        "end_date": end_date,
    }
    return target.reset_index(drop=True), meta


def is_minute_data_failure(status: Any) -> bool:
    return str(status or "") in {
        "missing_code",
        "minute_data_unavailable",
        "minute_query_failed",
        "data_conflict",
    }


def business_now() -> datetime:
    return datetime.now(BUSINESS_TZ)
