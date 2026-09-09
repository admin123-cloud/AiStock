from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_build_four_path_candidates import INDEX_CODE, _json_default  # noqa: E402
from scripts.gen3_update_panic_shadow import _latest_index_trade_date, _next_index_trade_date, _prev_index_trade_date  # noqa: E402
from scripts.scan_current_wave_style_candidates_v1 import (  # noqa: E402
    _attach_topic_ranks,
    _build_topic_members,
    _build_topic_summary,
    _load_current_candidates,
)
from scheduler.trading_calendar import TradingCalendar  # noqa: E402
from strategies.g3.confirmation import (  # noqa: E402
    FORMAL_CONFIRMATION_NAME,
    confirmation_contract_metadata,
    confirm_first_completed_30m_breakout,
    is_confirmation_data_wall,
)
from strategies.g3.minute_visibility import load_visible_minute_bars  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path, runtime_path  # noqa: E402


OUT_DIR = report_path("gen3_institutional_mainwave_current_v1")
RUNTIME_DIR = runtime_path("gen3_institutional_mainwave_current")
STATE_ALPHA_RUNTIME_DIR = runtime_path("gen3_state_alpha")
CURRENT_WAVE_SCAN_DIR = report_path("current_wave_style_candidate_scan_v1")
MARKET_CONTEXT_ARCHIVE = report_path("gen3_four_path_independent_candidates", "market_context.csv")
STATE_ROUTER_CONTEXT_ARCHIVE = report_path("gen3_state_router_shadow_daily_v1", "g3_state_router_market_context.csv")
CANDIDATE_SNAPSHOT_PATH = runtime_path("gen3_institutional_mainwave_current", "pending_candidate_snapshot.csv")
CANDIDATE_SNAPSHOT_META_PATH = runtime_path("gen3_institutional_mainwave_current", "pending_candidate_snapshot.json")
MAINWAVE_DYNAMIC_COOLDOWN = {
    "policy": "institutional_mainwave_consecutive_loss_dynamic_recovery",
    "trigger_consecutive_losses": 2,
    "min_cooldown_trading_days": 3,
    "max_cooldown_trading_days": 15,
}
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
FIRST_COMPLETED_30M_TIME = (10, 0)


def _date_text(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def _safe_float(value: Any, digits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _calendar_next_trade_date(trade_date: str) -> str:
    base = _date_text(trade_date)
    if not base:
        return ""
    try:
        dt = datetime.strptime(base, "%Y-%m-%d")
        return TradingCalendar.get_next_trading_day(dt).strftime("%Y-%m-%d")
    except Exception:
        return _next_index_trade_date(base)


def _calendar_prev_trade_date(entry_date: str) -> str:
    base = _date_text(entry_date)
    if not base:
        return ""
    try:
        dt = datetime.strptime(base, "%Y-%m-%d")
        return TradingCalendar.get_previous_trading_day(dt).strftime("%Y-%m-%d")
    except Exception:
        return _prev_index_trade_date(base)


def _latest_stock_trade_date() -> str:
    try:
        df = clickhouse_query_df(
            """
            SELECT max(k.trade_date) AS trade_date
            FROM kline_daily k
            JOIN trade_calendar c
              ON c.trade_date = k.trade_date
             AND c.market = 'SH'
             AND c.is_trading = 1
            """
        )
        if not df.empty and pd.notna(df.iloc[0]["trade_date"]):
            return pd.Timestamp(df.iloc[0]["trade_date"]).strftime("%Y-%m-%d")
    except Exception:
        pass
    df = clickhouse_query_df("SELECT max(trade_date) AS trade_date FROM kline_daily")
    if df.empty or pd.isna(df.iloc[0]["trade_date"]):
        return ""
    return pd.Timestamp(df.iloc[0]["trade_date"]).strftime("%Y-%m-%d")


def _calendar_entry_date(value: str) -> str:
    base = _date_text(value)
    if not base:
        return ""
    try:
        dt = datetime.strptime(base, "%Y-%m-%d")
        if TradingCalendar.is_trading_day(dt):
            return base
        return TradingCalendar.get_next_trading_day(dt).strftime("%Y-%m-%d")
    except Exception:
        return base


def _entry_session_pending(entry_date: str, now: datetime | None = None) -> bool:
    """Return whether the entry session has not reached its first 30m close."""
    entry = pd.to_datetime(entry_date, errors="coerce")
    if pd.isna(entry):
        return True
    current = now or datetime.now(BUSINESS_TZ)
    entry_day = entry.normalize()
    current_day = pd.Timestamp(current.date())
    if entry_day > current_day:
        return True
    if entry_day < current_day:
        return False
    return (current.hour, current.minute) < FIRST_COMPLETED_30M_TIME


def _resolve_dates(entry_date: str, decision_date: str) -> tuple[str, str]:
    if decision_date:
        decision = _date_text(decision_date)
        entry = _calendar_entry_date(entry_date) if entry_date else _calendar_next_trade_date(decision)
        # The daily skeleton is always D-1.  Older callers passed the entry
        # day twice during the first-bar refresh; repair that legacy shape at
        # the boundary so an intraday run cannot rebuild today's unfinished
        # daily pool.
        if entry and decision and entry == decision:
            decision = _calendar_prev_trade_date(entry)
        return entry or decision, decision
    if entry_date:
        entry = _calendar_entry_date(entry_date)
        return entry, _calendar_prev_trade_date(entry)
    try:
        latest = _latest_stock_trade_date() or _latest_index_trade_date()
        next_date = _calendar_next_trade_date(latest)
        if next_date and next_date <= latest:
            next_date = ""
        entry = next_date or latest
        decision = _calendar_prev_trade_date(entry)
        return entry, decision
    except Exception:
        summary_path = CURRENT_WAVE_SCAN_DIR / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            decision = _date_text(summary.get("target_date", ""))
            today = pd.Timestamp(datetime.now()).normalize()
            raw_entry = today.strftime("%Y-%m-%d") if decision and today > pd.Timestamp(decision) else decision
            entry = _calendar_entry_date(raw_entry)
            decision = _calendar_prev_trade_date(entry)
            if entry and decision:
                return entry, decision
        raise


def _numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _load_candidate_snapshot(entry_date: str, decision_date: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load the frozen D-1 candidate set used by the entry-day confirmation.

    Rebuilding a daily pool is useful as recovery, but it must be observable.
    A valid snapshot always wins so a changed upstream scan cannot silently
    replace the candidates that were promised after the close.
    """

    base = {
        "status": "snapshot_not_available",
        "path": str(CANDIDATE_SNAPSHOT_PATH),
        "meta_path": str(CANDIDATE_SNAPSHOT_META_PATH),
        "entry_date": entry_date,
        "decision_date": decision_date,
        "rows": 0,
    }
    if not entry_date or not decision_date:
        return pd.DataFrame(), {**base, "status": "snapshot_not_required_missing_dates"}
    if not CANDIDATE_SNAPSHOT_PATH.exists() or not CANDIDATE_SNAPSHOT_META_PATH.exists():
        return pd.DataFrame(), base
    try:
        meta = json.loads(CANDIDATE_SNAPSHOT_META_PATH.read_text(encoding="utf-8"))
        if str(meta.get("entry_date") or "")[:10] != str(entry_date)[:10] or str(meta.get("decision_date") or "")[:10] != str(decision_date)[:10]:
            return pd.DataFrame(), {**base, "status": "snapshot_date_mismatch", "snapshot_meta": meta}
        frame = _read_csv(CANDIDATE_SNAPSHOT_PATH)
        if frame.empty or "code_raw" not in frame.columns:
            return pd.DataFrame(), {**base, "status": "snapshot_empty_or_invalid", "snapshot_meta": meta}
        return frame.reset_index(drop=True), {
            **base,
            "status": "snapshot_loaded",
            "rows": int(len(frame)),
            "generated_at": meta.get("generated_at"),
            "snapshot_meta": meta,
        }
    except Exception as exc:
        return pd.DataFrame(), {**base, "status": "snapshot_read_failed", "error": str(exc)}


def _save_candidate_snapshot(candidates: pd.DataFrame, entry_date: str, decision_date: str, source: str) -> dict[str, Any]:
    """Persist the D-1 skeleton before any intraday confirmation is applied."""

    CANDIDATE_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_csv = CANDIDATE_SNAPSHOT_PATH.with_suffix(".csv.tmp")
    temp_meta = CANDIDATE_SNAPSHOT_META_PATH.with_suffix(".json.tmp")
    try:
        candidates.to_csv(temp_csv, index=False, encoding="utf-8-sig")
        meta = {
            "schema_version": 1,
            "generated_at": datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "entry_date": entry_date,
            "decision_date": decision_date,
            "rows": int(len(candidates)),
            "source": source,
            "confirmation": confirmation_contract_metadata(),
        }
        temp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        temp_csv.replace(CANDIDATE_SNAPSHOT_PATH)
        temp_meta.replace(CANDIDATE_SNAPSHOT_META_PATH)
        return {"status": "snapshot_saved", "path": str(CANDIDATE_SNAPSHOT_PATH), "rows": int(len(candidates)), **meta}
    except Exception as exc:
        for path in (temp_csv, temp_meta):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"status": "snapshot_write_failed", "path": str(CANDIDATE_SNAPSHOT_PATH), "rows": int(len(candidates)), "error": str(exc)}


def _load_current_pool(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    try:
        pool, meta = _load_current_candidates(args)
        meta["source_mode"] = "rebuilt_from_clickhouse"
        return pool, meta
    except Exception as exc:
        summary_path = CURRENT_WAVE_SCAN_DIR / "summary.json"
        if not summary_path.exists():
            raise
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            raise
        if str(summary.get("target_date", "")) != str(args.target_date):
            raise
        template = _read_csv(CURRENT_WAVE_SCAN_DIR / "template_pass_candidates.csv")
        top = _read_csv(CURRENT_WAVE_SCAN_DIR / "top_candidates.csv")
        if template.empty and top.empty:
            raise
        if not template.empty and "template_pass" not in template.columns:
            template["template_pass"] = True
        if not top.empty and "template_pass" not in top.columns:
            top["template_pass"] = top.get("template_label", "").astype(str).ne("观察")
        pool = pd.concat([template, top], ignore_index=True, sort=False)
        if "code_raw" in pool.columns:
            pool = pool.drop_duplicates("code_raw", keep="first")
        meta = {
            **summary,
            "source_mode": "cached_current_wave_scan_fallback",
            "fallback_reason": type(exc).__name__,
            "fallback_error": str(exc),
        }
        return pool, meta


def _build_sector_diffusion(pool: pd.DataFrame) -> pd.DataFrame:
    if pool.empty:
        return pd.DataFrame()
    topic_members = _build_topic_members(pool)
    if topic_members.empty:
        return pd.DataFrame()
    topic_summary = _build_topic_summary(topic_members)
    if topic_summary.empty:
        return pd.DataFrame()
    sector = topic_summary.rename(
        columns={
            "topic_name": "tdx_mainline_sector_name",
            "topic_candidate_count": "sector_candidate_count",
            "topic_avg_score": "sector_avg_score",
            "topic_max_score": "sector_max_score",
            "topic_avg_ret5": "sector_avg_ret5",
            "topic_avg_ret20": "sector_avg_ret20",
            "topic_big_up_sum": "sector_big_up_sum",
            "topic_limit_up_sum": "sector_limit_up_sum",
            "topic_avg_amount20": "sector_avg_amount20",
            "topic_share": "sector_share",
            "topic_diffusion_score": "sector_diffusion_score",
        }
    ).copy()
    sector["topic_name"] = sector["tdx_mainline_sector_name"]
    sector["mainline_sector_source"] = "tdx"
    sector["sector_diffusion_source"] = "tdx_multi_topic"
    return sector.sort_values(["sector_diffusion_score", "sector_candidate_count"], ascending=[False, False]).reset_index(drop=True)


def _industry_key(frame: pd.DataFrame) -> pd.Series:
    """Return the canonical current industry key without using TDX topics as a gate."""
    for column in ["l2_sector_code", "sw_l2_industry_code", "l2_sector_name", "sw_l2_industry_name"]:
        if column in frame.columns:
            value = frame[column].fillna("").astype(str).str.strip()
            if value.ne("").any():
                return value
    return pd.Series("", index=frame.index, dtype="object")


def _attach_same_day_industry_diffusion(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Count same-day mainwave-skeleton peers in each current industry.

    The input has already passed the observable skeleton.  Thus a count of two
    means the candidate has at least one independently qualifying industry peer;
    no historical outcome, learned-sector label, or TDX topic is involved.
    """
    if candidates.empty:
        return candidates.copy(), pd.DataFrame()
    out = candidates.copy()
    out["industry_key"] = _industry_key(out)
    valid = out["industry_key"].ne("")
    grouped = (
        out.loc[valid]
        .groupby("industry_key", as_index=False)
        .agg(
            sector_signal_count=("code_raw", "nunique"),
            sector_avg_score=("wave_style_score", "mean"),
            sector_max_score=("wave_style_score", "max"),
            sector_avg_amount20=("amount20", "mean"),
        )
    )
    grouped["mainline_sector_source"] = "qmt_current_industry"
    grouped["sector_diffusion_source"] = "same_day_mainwave_skeleton_count"
    out = out.merge(grouped, on="industry_key", how="left")
    out["sector_signal_count"] = pd.to_numeric(out["sector_signal_count"], errors="coerce").fillna(0).astype(int)
    return out, grouped.sort_values(["sector_signal_count", "sector_avg_score"], ascending=[False, False]).reset_index(drop=True)


def _compute_30m_features(candidates: pd.DataFrame, entry_date: str, period: int = 30) -> tuple[pd.DataFrame, dict[str, Any]]:
    if int(period) != 30:
        raise ValueError("G3 Score88 formal confirmation requires period=30")
    if candidates.empty:
        return candidates.copy(), {"m30_rows": 0, "m30_table": f"kline_minute_{period}"}
    rows: list[dict[str, Any]] = []
    signal_day = pd.Timestamp(entry_date).normalize()
    first_bar_cutoff = signal_day + pd.Timedelta(hours=10)
    start = (signal_day - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    end = signal_day.strftime("%Y-%m-%d")
    for idx, item in candidates.iterrows():
        code = str(item.get("code_raw") or item.get("code") or "")
        if not code:
            rows.append({"_idx": idx, "m30_ok": False, "m30_status": "missing_code"})
            continue
        bars, visibility = load_visible_minute_bars(
            code,
            period,
            start,
            end,
        )
        visibility_fields = {
            "m30_source": visibility.get("source", ""),
            "m30_visibility_status": visibility.get("status", ""),
            "m30_source_ok": bool(visibility.get("source_ok", False)),
            "m30_main_rows": int(visibility.get("main_rows") or 0),
            "m30_stage_rows": int(visibility.get("stage_rows") or 0),
            "m30_recovered_rows": int(visibility.get("recovered_rows") or 0),
            "m30_conflict_rows": int(visibility.get("conflict_rows") or 0),
            "m30_day_rows": int((visibility.get("day_counts") or {}).get(end, 0) or 0),
        }
        if not bool(visibility.get("source_ok")) or bars.empty:
            status = str(visibility.get("status") or "minute_data_unavailable")
            rows.append(
                {
                    "_idx": idx,
                    "m30_ok": False,
                    "m30_status": status,
                    "m30_error": visibility.get("main_query_error") or visibility.get("stage_query_error", ""),
                    **visibility_fields,
                }
            )
            continue
        bars["datetime"] = pd.to_datetime(bars["business_datetime"], errors="coerce")
        bars["date"] = bars["datetime"].dt.normalize()
        bars = _numeric(bars, ["open", "high", "low", "close", "amount"])
        bars = bars.dropna(subset=["datetime", "close"]).sort_values("datetime")
        visible = bars[bars["datetime"] <= first_bar_cutoff].copy()
        hist = visible.tail(60).copy()
        day = visible[visible["date"].eq(signal_day)].copy()
        if len(hist) < 20 or day.empty:
            rows.append(
                {
                    "_idx": idx,
                    "m30_ok": False,
                    "m30_status": "insufficient_minute_bars",
                    **visibility_fields,
                }
            )
            continue
        confirmation = confirm_first_completed_30m_breakout(visible, end)
        confirmation_status = str(confirmation.get("status") or "")
        if is_confirmation_data_wall(confirmation_status):
            rows.append(
                {
                    "_idx": idx,
                    "m30_ok": False,
                    "m30_status": confirmation_status,
                    "m30_error": confirmation.get("detail", ""),
                    "m30_confirmed": False,
                    "m30_breakout_confirmed": False,
                    "m30_breakout_confirm_datetime": confirmation.get("confirm_datetime", ""),
                    "m30_breakout_confirm_price": confirmation.get("confirm_price"),
                    "m30_breakout_amount_ratio": confirmation.get("amount_ratio"),
                    "m30_prior20_high": confirmation.get("prior20_high"),
                    "m30_prior20_amount_avg": confirmation.get("prior20_amount_avg"),
                    "m30_confirmation_detail": confirmation.get("detail", ""),
                    **visibility_fields,
                }
            )
            continue

        close = pd.to_numeric(hist["close"], errors="coerce")
        amount = pd.to_numeric(hist["amount"], errors="coerce")
        last = float(close.iloc[-1])
        ma20 = float(close.tail(20).mean())
        ma40 = float(close.tail(40).mean()) if len(close) >= 40 else float(close.mean())
        prev6 = float(close.iloc[-7]) if len(close) >= 7 else np.nan
        prev12 = float(close.iloc[-13]) if len(close) >= 13 else np.nan
        amt_last2 = float(amount.tail(2).mean())
        amt_prev20 = float(amount.tail(22).head(20).mean()) if len(amount) >= 22 else float(amount.tail(20).mean())
        day_open = float(day["open"].iloc[0])
        day_high = float(day["high"].max())
        day_low = float(day["low"].min())
        day_close = float(day["close"].iloc[-1])
        day_range = max(day_high - day_low, 1e-9)
        last_open = float(day["open"].iloc[-1])
        breakout_time = confirmation.get("confirm_datetime", "")
        breakout_price = confirmation.get("confirm_price")
        breakout_volume_ratio = confirmation.get("amount_ratio")
        m30_confirmed = bool(confirmation.get("confirmed"))
        rows.append(
            {
                "_idx": idx,
                "m30_ok": True,
                "m30_status": "ok" if m30_confirmed else confirmation_status,
                "m30_close_above_ma20": last / ma20 - 1.0 if ma20 > 0 else np.nan,
                "m30_close_above_ma40": last / ma40 - 1.0 if ma40 > 0 else np.nan,
                "m30_mom6": last / prev6 - 1.0 if prev6 > 0 else np.nan,
                "m30_mom12": last / prev12 - 1.0 if prev12 > 0 else np.nan,
                "m30_amount_last2_ratio": amt_last2 / amt_prev20 if amt_prev20 > 0 else np.nan,
                "m30_day_ret": day_close / day_open - 1.0 if day_open > 0 else np.nan,
                "m30_day_close_pos": (day_close - day_low) / day_range,
                "m30_day_amp": day_high / day_low - 1.0 if day_low > 0 else np.nan,
                "m30_last_bar_ret": day_close / last_open - 1.0 if last_open > 0 else np.nan,
                "m30_confirmed": m30_confirmed,
                "m30_breakout_confirmed": m30_confirmed,
                "m30_breakout_confirm_datetime": breakout_time,
                "m30_breakout_confirm_price": breakout_price,
                "m30_breakout_amount_ratio": breakout_volume_ratio,
                "m30_prior20_high": confirmation.get("prior20_high"),
                "m30_prior20_amount_avg": confirmation.get("prior20_amount_avg"),
                "m30_confirmation_detail": confirmation.get("detail", ""),
                "m30_breakout_rule": FORMAL_CONFIRMATION_NAME,
                **visibility_fields,
            }
        )
    feat = pd.DataFrame(rows).set_index("_idx") if rows else pd.DataFrame()
    out = candidates.join(feat)
    ok_rows = int(pd.Series(out.get("m30_ok", False)).fillna(False).astype(bool).sum()) if not out.empty else 0
    statuses = (
        out.get("m30_visibility_status", pd.Series(dtype=str)).fillna("").astype(str).value_counts().to_dict()
        if not out.empty
        else {}
    )
    recovered_series = pd.to_numeric(
        out.get("m30_recovered_rows", pd.Series(0, index=out.index)),
        errors="coerce",
    ).fillna(0)
    conflict_series = pd.to_numeric(
        out.get("m30_conflict_rows", pd.Series(0, index=out.index)),
        errors="coerce",
    ).fillna(0)
    return out, {
        "m30_rows": int(len(out)),
        "m30_ok_rows": ok_rows,
        "m30_table": f"kline_minute_{period}",
        "confirmation": confirmation_contract_metadata(),
        "m30_visibility_status_counts": {str(key): int(value) for key, value in statuses.items()},
        "m30_recovered_rows": int(recovered_series.sum()) if not out.empty else 0,
        "m30_conflict_rows": int(conflict_series.sum()) if not out.empty else 0,
    }


def _index_mom60(decision_date: str) -> tuple[float | None, dict[str, Any]]:
    end = _date_text(decision_date)
    start = (pd.Timestamp(end) - pd.Timedelta(days=130)).strftime("%Y-%m-%d")
    index_codes = list(dict.fromkeys([INDEX_CODE, "000001.SH", "000300.SH"]))
    last_empty_meta: dict[str, Any] | None = None
    try:
        df = pd.DataFrame()
        index_code = INDEX_CODE
        for candidate_code in index_codes:
            candidate_df = clickhouse_query_df(
                """
                SELECT trade_date, close
                FROM kline_daily
                WHERE code = ?
                  AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                [candidate_code, start, end],
            )
            if not candidate_df.empty and len(candidate_df) >= 61:
                df = candidate_df
                index_code = candidate_code
                break
            last_empty_meta = {"status": "insufficient_index_rows", "rows": int(len(candidate_df)), "index_code": candidate_code}
    except Exception as exc:
        for path in [STATE_ROUTER_CONTEXT_ARCHIVE, MARKET_CONTEXT_ARCHIVE]:
            archived = _read_csv(path)
            if not archived.empty and "trade_date" in archived.columns and "mom60" in archived.columns:
                archived["trade_date"] = pd.to_datetime(archived["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                row = archived[archived["trade_date"].eq(end)].tail(1)
                if not row.empty:
                    value = pd.to_numeric(row["mom60"], errors="coerce").iloc[0]
                    if pd.notna(value):
                        return float(value), {"status": "archive_fallback", "path": str(path), "index_code": INDEX_CODE, "fallback_error": str(exc)}
        for path in [RUNTIME_DIR / "latest_summary.json", OUT_DIR / "summary.json"]:
            if path.exists():
                try:
                    summary = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                meta = summary.get("meta", {})
                if str(meta.get("decision_date", "")) == end and meta.get("index_mom60") is not None:
                    return float(meta["index_mom60"]), {"status": "summary_fallback", "path": str(path), "index_code": INDEX_CODE, "fallback_error": str(exc)}
        raise
    if df.empty or len(df) < 61:
        return None, last_empty_meta or {"status": "insufficient_index_rows", "rows": int(len(df)), "index_code": INDEX_CODE}
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
    if len(df) < 61:
        return None, {"status": "insufficient_index_rows_after_clean", "rows": int(len(df)), "index_code": index_code}
    close = float(df["close"].iloc[-1])
    ma20 = float(df["close"].tail(20).mean()) if len(df) >= 20 else None
    mom20 = float(df["close"].iloc[-1] / df["close"].iloc[-21] - 1.0) if len(df) >= 21 else None
    mom60 = float(df["close"].iloc[-1] / df["close"].iloc[-61] - 1.0)
    return mom60, {
        "status": "ok",
        "rows": int(len(df)),
        "index_code": index_code,
        "index_close": close,
        "index_ma20": ma20,
        "index_mom20": mom20,
        "index_mom60": mom60,
        "index_recovery_ok": bool((ma20 is not None and close >= ma20) or (mom20 is not None and mom20 >= 0.0)),
    }


def _trade_calendar_between(start: str, end: str) -> list[str]:
    start_text = _date_text(start)
    end_text = _date_text(end)
    if not start_text or not end_text:
        return []
    try:
        df = clickhouse_query_df(
            """
            SELECT DISTINCT trade_date
            FROM kline_daily
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [start_text, end_text],
        )
        if not df.empty:
            return pd.to_datetime(df["trade_date"], errors="coerce").dropna().dt.strftime("%Y-%m-%d").tolist()
    except Exception:
        pass
    start_ts = pd.Timestamp(start_text)
    end_ts = pd.Timestamp(end_text)
    days: list[str] = []
    current = start_ts
    while current <= end_ts:
        if current.weekday() < 5:
            days.append(current.strftime("%Y-%m-%d"))
        current += pd.Timedelta(days=1)
    return days


def _closed_mainwave_trades() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in [STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv", RUNTIME_DIR / "shadow_ledger.csv"]:
        df = _read_csv(path)
        if not df.empty:
            df["_ledger_path"] = str(path)
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True, sort=False)
    if "ticket_key" in d.columns:
        d = d.drop_duplicates("ticket_key", keep="last")
    route = d.get("route", pd.Series("", index=d.index)).fillna("").astype(str)
    strategy = d.get("trade_strategy", pd.Series("", index=d.index)).fillna("").astype(str)
    d = d[route.eq("institutional_mainwave") | strategy.isin(["institutional_mainwave_score88", "institutional_score120_mainwave"])].copy()
    if d.empty:
        return d
    for col in ["net_ret", "realized_ret", "account_ret"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    if "net_ret" in d.columns:
        d["_ret"] = d["net_ret"]
    elif "realized_ret" in d.columns:
        d["_ret"] = d["realized_ret"]
    elif "account_ret" in d.columns:
        d["_ret"] = d["account_ret"]
    else:
        d["_ret"] = pd.NA
    status = d.get("last_state", d.get("trade_status", pd.Series("", index=d.index))).fillna("").astype(str).str.lower()
    exit_reason = d.get("exit_reason", d.get("last_exit_reason", pd.Series("", index=d.index))).fillna("").astype(str).str.lower()
    closed_mask = pd.to_numeric(d["_ret"], errors="coerce").notna() | status.str.contains("closed|exit|sold|stopped|hard_stop", regex=True) | exit_reason.str.contains("hard_stop|take_profit|prev_low|policy_exit", regex=True)
    d = d[closed_mask].copy()
    if d.empty:
        return d
    date_col = None
    for candidate in ["exit_date", "policy_exit_date", "closed_at", "updated_at", "created_at", "entry_date"]:
        if candidate in d.columns:
            date_col = candidate
            break
    d["_exit_date"] = pd.to_datetime(d[date_col], errors="coerce").dt.strftime("%Y-%m-%d") if date_col else ""
    d = d[pd.to_numeric(d["_ret"], errors="coerce").notna() & d["_exit_date"].astype(str).ne("")]
    return d.sort_values(["_exit_date", "ticket_key" if "ticket_key" in d.columns else d.columns[0]]).reset_index(drop=True)


def _dynamic_cooldown_state(entry_date: str, recovery_signal_ok: bool) -> dict[str, Any]:
    base = {
        **MAINWAVE_DYNAMIC_COOLDOWN,
        "cooldown_active": False,
        "cooldown_reason": "no_closed_institutional_mainwave_loss_streak",
        "cooldown_scope": "institutional_mainwave_new_buys",
        "recovery_signal_ok": bool(recovery_signal_ok),
        "closed_mainwave_count": 0,
        "consecutive_loss_count": 0,
    }
    closed = _closed_mainwave_trades()
    if closed.empty:
        return base | {"cooldown_reason": "no_closed_institutional_mainwave_history"}
    rets = pd.to_numeric(closed["_ret"], errors="coerce").tolist()
    consecutive = 0
    for value in reversed(rets):
        if value < 0:
            consecutive += 1
        else:
            break
    base["closed_mainwave_count"] = int(len(closed))
    base["consecutive_loss_count"] = int(consecutive)
    base["last_closed_exit_date"] = str(closed["_exit_date"].iloc[-1])
    base["last_closed_ret"] = _safe_float(closed["_ret"].iloc[-1])
    if consecutive < int(MAINWAVE_DYNAMIC_COOLDOWN["trigger_consecutive_losses"]):
        return base | {"cooldown_reason": "consecutive_loss_below_trigger"}

    trigger_row = closed.iloc[-int(MAINWAVE_DYNAMIC_COOLDOWN["trigger_consecutive_losses"])]
    trigger_date = str(trigger_row["_exit_date"])
    cal = _trade_calendar_between(trigger_date, entry_date)
    if not cal:
        return base | {
            "cooldown_active": True,
            "cooldown_reason": "cooldown_calendar_unavailable",
            "cooldown_trigger_date": trigger_date,
        }
    try:
        trigger_idx = cal.index(trigger_date)
    except ValueError:
        trigger_idx = 0
    entry_text = _date_text(entry_date)
    try:
        entry_idx = cal.index(entry_text)
    except ValueError:
        entry_idx = len(cal) - 1
    elapsed = max(0, entry_idx - trigger_idx)
    min_days = int(MAINWAVE_DYNAMIC_COOLDOWN["min_cooldown_trading_days"])
    max_days = int(MAINWAVE_DYNAMIC_COOLDOWN["max_cooldown_trading_days"])
    if elapsed <= min_days:
        active = True
        reason = "min_3_trading_days_cooldown"
    elif elapsed > max_days:
        active = False
        reason = "max_15_trading_days_recheck_release"
    elif recovery_signal_ok:
        active = False
        reason = "market_recovery_signal_release"
    else:
        active = True
        reason = "waiting_market_recovery_signal"
    return base | {
        "cooldown_active": bool(active),
        "cooldown_reason": reason,
        "cooldown_trigger_date": trigger_date,
        "cooldown_elapsed_trading_days": int(elapsed),
        "cooldown_min_release_trading_days": min_days,
        "cooldown_max_recheck_trading_days": max_days,
    }


def _decorate_signal_rows(
    rows: pd.DataFrame,
    entry: str,
    decision: str,
    *,
    eligible: bool,
    pending_next_session_confirmation: bool = False,
) -> pd.DataFrame:
    if rows.empty:
        return rows.copy()
    out = rows.copy()
    out["code"] = out["code_raw"].astype(str)
    out["name"] = out.get("stock_name", "")
    out["trade_date"] = decision
    out["decision_date"] = decision
    out["entry_date"] = entry
    out["route"] = "institutional_mainwave"
    out["route_label"] = "机构主升浪"
    out["route_source"] = "institutional_mainwave_current_builder_v1"
    out["source_quality"] = "current_rebuilt"
    out["profile"] = "g3_market_state_router_v1"
    out["strategy_id"] = "g3_market_state_router_strategy_v1"
    out["entry_mode"] = "mainwave_30m_volume_breakout"
    out["score"] = pd.to_numeric(out["wave_style_score"], errors="coerce")
    sw_sector = out["sw_l2_industry_name"] if "sw_l2_industry_name" in out.columns else out.get("l2_sector_name", pd.Series("", index=out.index))
    out["sector_name"] = out.get("industry_key", sw_sector).fillna("").astype(str)
    out["sector_source"] = "qmt_current_industry"
    out["topic_name"] = out["sector_name"]
    if "sector_family" not in out.columns:
        out["sector_family"] = ""
    out["industry_name"] = sw_sector.fillna("").astype(str)
    out["industry_source"] = "sw"
    out["policy"] = "state_router_institutional_mainwave_shadow"
    out["confirm_rule"] = "wave_style_score_ge_88 + index_mom60_le_5pct + same_day_industry_mainwave_count_ge_2 + 30m_volume_breakout_prior20_high"
    confirm_datetime = out.get("m30_breakout_confirm_datetime", pd.Series("", index=out.index))
    out["confirm_datetime"] = confirm_datetime.fillna("").astype(str)
    out["entry_ts"] = out["confirm_datetime"].where(out["confirm_datetime"].ne(""), pd.Timestamp(entry).strftime("%Y-%m-%d 09:30:00"))
    confirm_price = out.get("m30_breakout_confirm_price", pd.Series(index=out.index, dtype=float))
    close = out.get("close", pd.Series(index=out.index, dtype=float))
    out["reference_close"] = pd.to_numeric(confirm_price, errors="coerce").fillna(pd.to_numeric(close, errors="coerce"))
    out["entry_price"] = pd.NA
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False
    out["router_eligible"] = bool(eligible)
    if eligible:
        out["shadow_status"] = "confirmed_shadow_candidate"
        out["block_reason"] = ""
    else:
        cooldown_active = pd.Series(out.get("mainwave_dynamic_cooldown_active", False), index=out.index).fillna(False).astype(bool)
        status = out.get("m30_status", pd.Series("", index=out.index)).astype(str)
        missing_minute = status.isin(
            [
                "minute_query_failed",
                "minute_data_unavailable",
                "data_conflict",
                "missing_code",
                "missing_minute_bars",
                "insufficient_minute_bars",
            ]
        )
        out["shadow_status"] = "blocked_waiting_confirmation"
        out["block_reason"] = "institutional_wait_30m_confirm_or_no_intraday_data"
        out.loc[~pd.Series(out.get("index_mom60_gate", False), index=out.index).fillna(False).astype(bool), "block_reason"] = "institutional_index_mom60_gt_5pct_strategy_gate"
        out.loc[missing_minute, "block_reason"] = "institutional_30m_data_unavailable"
        out.loc[status.eq("data_conflict"), "block_reason"] = "institutional_30m_source_conflict"
        out.loc[
            pd.Series(out.get("m30_ok", False), index=out.index).fillna(False).astype(bool)
            & ~pd.Series(out.get("m30_confirmed", False), index=out.index).fillna(False).astype(bool),
            "block_reason",
        ] = "institutional_30m_volume_breakout_prior20_high_not_confirmed"
        if cooldown_active.any():
            out.loc[cooldown_active, "shadow_status"] = "blocked_mainwave_dynamic_cooldown"
            out.loc[cooldown_active, "block_reason"] = "institutional_mainwave_dynamic_cooldown_pause_new_buy"
        if pending_next_session_confirmation:
            # 盘后生成的是下一交易日候选池；未来交易日尚未产生首根完成的
            # 30m K 线时，只能等待盘中确认，不能误报为分钟数据缺失。
            out["shadow_status"] = "pending_next_session_confirmation"
            out["block_reason"] = "await_next_trade_session_30m_confirmation"
    return out


def build_current_candidates(
    *,
    entry_date: str = "",
    decision_date: str = "",
    lookback_days: int = 90,
    min_amount20: float = 30000.0,
    min_score: float = 88.0,
    min_sector_signal_count: int = 2,
    max_index_mom60: float = 0.05,
    period: int = 30,
    top_n: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    entry, decision = _resolve_dates(entry_date, decision_date)
    frozen_pool, snapshot_meta = _load_candidate_snapshot(entry, decision)
    snapshot_loaded = not frozen_pool.empty
    if snapshot_loaded:
        pool = frozen_pool.copy()
        scan_meta = {
            **snapshot_meta,
            "source_mode": "frozen_d1_candidate_snapshot",
            "target_date": decision,
        }
    else:
        args = argparse.Namespace(target_date=decision, lookback_days=lookback_days, min_amount20=min_amount20)
        pool, scan_meta = _load_current_pool(args)
        scan_meta = {**scan_meta, "candidate_snapshot": snapshot_meta}
    pool = _numeric(pool, ["wave_style_score", "ret20", "limit_up_days20", "learned_sector_bonus", "mom60"])
    template_pass = (
        pool["template_pass"].fillna(False).astype(bool)
        if "template_pass" in pool.columns
        else pd.Series(False, index=pool.index, dtype=bool)
    )
    template_pool = pool[template_pass].copy()
    legacy_sector = _build_sector_diffusion(template_pool)
    candidates = pool[
        template_pass
        & pd.to_numeric(pool["wave_style_score"], errors="coerce").ge(float(min_score))
        & pd.to_numeric(pool.get("ret20", pd.Series(0.0, index=pool.index)), errors="coerce").fillna(0).lt(0.55)
        & pd.to_numeric(pool.get("limit_up_days20", pd.Series(99.0, index=pool.index)), errors="coerce").fillna(99).le(4)
    ].copy()
    if snapshot_loaded and "sector_signal_count" in candidates.columns:
        candidates["sector_signal_count"] = pd.to_numeric(candidates["sector_signal_count"], errors="coerce").fillna(0).astype(int)
        sector = (
            candidates.groupby("industry_key", as_index=False)
            .agg(
                sector_signal_count=("code_raw", "nunique"),
                sector_avg_score=("wave_style_score", "mean"),
                sector_max_score=("wave_style_score", "max"),
                sector_avg_amount20=("amount20", "mean"),
            )
            if "industry_key" in candidates.columns
            else pd.DataFrame()
        )
        if not sector.empty:
            sector["mainline_sector_source"] = "qmt_current_industry_snapshot"
            sector["sector_diffusion_source"] = "frozen_d1_mainwave_skeleton_count"
    else:
        candidates, sector = _attach_same_day_industry_diffusion(candidates)
    candidates = candidates[candidates["sector_signal_count"].ge(int(min_sector_signal_count))].copy()
    candidate_skeleton = candidates.copy()
    if not candidates.empty:
        candidates = candidates.sort_values(
            ["wave_style_score", "amount20", "code_raw"], ascending=[False, False, True]
        ).drop_duplicates("code_raw", keep="first").head(int(top_n))
    pending_next_session_confirmation = _entry_session_pending(entry)
    snapshot_write_meta: dict[str, Any] = {
        "status": "snapshot_not_needed",
        "path": str(CANDIDATE_SNAPSHOT_PATH),
        "rows": 0,
    }
    if pending_next_session_confirmation and not snapshot_loaded:
        snapshot_write_meta = _save_candidate_snapshot(
            candidate_skeleton,
            entry,
            decision,
            source="institutional_mainwave_current_builder_v1",
        )
    if pending_next_session_confirmation:
        # 入场日是下一交易日：盘后只有 decision 日数据，不能查询未来 entry
        # 日的分钟线。盘中首根完成的 30m K 线出现后，常规刷新再完成确认。
        candidates = candidates.copy()
        if not candidates.empty:
            candidates["m30_ok"] = False
            candidates["m30_status"] = "await_next_trade_session_30m"
            candidates["m30_breakout_confirmed"] = False
            candidates["m30_breakout_confirm_datetime"] = ""
            candidates["m30_breakout_confirm_price"] = np.nan
            candidates["m30_breakout_amount_ratio"] = np.nan
            candidates["m30_breakout_rule"] = "first_completed_entry_day_30m_required"
        m30_meta = {
            "m30_rows": int(len(candidates)),
            "m30_ok_rows": 0,
            "m30_table": f"kline_minute_{period}",
            "status": "pending_next_trade_session_confirmation",
            "reason": "entry_date_is_next_trade_day",
        }
    else:
        candidates, m30_meta = _compute_30m_features(candidates, entry, period=period)
    index_mom60, index_meta = _index_mom60(decision)
    index_gate = index_mom60 is not None and index_mom60 <= float(max_index_mom60)
    pre_confirm_candidates = candidates.copy()
    if not candidates.empty:
        candidates["index_mom60"] = index_mom60
        candidates["index_mom60_gate"] = index_gate
        candidates["index_close"] = index_meta.get("index_close")
        candidates["index_ma20"] = index_meta.get("index_ma20")
        candidates["index_mom20"] = index_meta.get("index_mom20")
        candidates["index_mom60_heat_state"] = "normal"
        candidates["market_heat_position_scale"] = 1.0
        if index_mom60 is not None and index_mom60 > float(max_index_mom60):
            candidates["index_mom60_heat_state"] = "institutional_mom60_gt_5_block"
            candidates["market_heat_position_scale"] = 0.0
        candidates["m30_confirmed"] = pd.Series(candidates.get("m30_breakout_confirmed", False), index=candidates.index).fillna(False).astype(bool)
        pre_confirm_candidates = candidates.copy()
        candidates = candidates[candidates["index_mom60_gate"] & candidates["m30_confirmed"]].copy()

    index_recovery_ok = bool(index_meta.get("index_recovery_ok"))
    candidate_recovery_ok = (
        bool((pre_confirm_candidates["index_mom60_gate"].fillna(False).astype(bool) & pre_confirm_candidates["m30_confirmed"].fillna(False).astype(bool)).any())
        if not pre_confirm_candidates.empty and {"index_mom60_gate", "m30_confirmed"}.issubset(pre_confirm_candidates.columns)
        else False
    )
    recovery_signal_ok = bool(index_gate and index_recovery_ok and candidate_recovery_ok)
    # The current contract is intentionally limited to its explicit four
    # entry gates plus the two-slot risk rules.  Legacy loss-streak cooldown
    # remains visible only as history, not as an additional admission gate.
    cooldown = {
        "policy": "not_in_current_mainwave_contract",
        "cooldown_active": False,
        "cooldown_reason": "removed_from_current_contract",
        "cooldown_scope": "diagnostic_only",
    }
    cooldown_active = False
    if not pre_confirm_candidates.empty:
        pre_confirm_candidates["mainwave_dynamic_cooldown_active"] = cooldown_active
        pre_confirm_candidates["mainwave_dynamic_cooldown_reason"] = cooldown.get("cooldown_reason", "")
        pre_confirm_candidates["mainwave_cooldown_elapsed_trading_days"] = cooldown.get("cooldown_elapsed_trading_days")
        pre_confirm_candidates["mainwave_recovery_signal_ok"] = recovery_signal_ok
    if not candidates.empty:
        candidates["mainwave_dynamic_cooldown_active"] = cooldown_active
        candidates["mainwave_dynamic_cooldown_reason"] = cooldown.get("cooldown_reason", "")
        candidates["mainwave_cooldown_elapsed_trading_days"] = cooldown.get("cooldown_elapsed_trading_days")
        candidates["mainwave_recovery_signal_ok"] = recovery_signal_ok
    confirmed = _decorate_signal_rows(candidates, entry, decision, eligible=True)
    blocked = pre_confirm_candidates.drop(candidates.index, errors="ignore") if not pre_confirm_candidates.empty else pre_confirm_candidates
    blocked = _decorate_signal_rows(
        blocked,
        entry,
        decision,
        eligible=False,
        pending_next_session_confirmation=pending_next_session_confirmation,
    )
    if not confirmed.empty:
        confirmed = confirmed.sort_values(["score", "m30_breakout_confirm_datetime"], ascending=[False, True]).drop_duplicates("code", keep="first").head(int(top_n))
    if not blocked.empty:
        blocked = blocked.sort_values(["score", "sector_signal_count"], ascending=[False, False]).drop_duplicates("code", keep="first").head(int(top_n))

    meta = {
        "source": "institutional_mainwave_current_builder_v1",
        "status": "ok" if not candidates.empty else "no_candidate_after_gates",
        "entry_date": entry,
        "decision_date": decision,
        "scan": scan_meta,
        "pool_rows": int(len(pool)),
        "template_pool_rows": int(len(template_pool)),
        "sector_rows": int(len(sector)),
        "legacy_tdx_sector_rows": int(len(legacy_sector)),
        "rows": int(len(confirmed)),
        "pre_confirm_rows": int(len(pre_confirm_candidates)),
        "pre_confirm_preview": pre_confirm_candidates[
            [c for c in ["code_raw", "stock_name", "industry_key", "sector_signal_count", "template_label", "wave_style_score", "m30_status", "m30_breakout_confirm_datetime"] if c in pre_confirm_candidates.columns]
        ].head(20).to_dict("records")
        if not pre_confirm_candidates.empty
        else [],
        "min_score": float(min_score),
        "min_sector_signal_count": int(min_sector_signal_count),
        "max_index_mom60": float(max_index_mom60),
        "index_mom60": _safe_float(index_mom60),
        "index": index_meta,
        "m30": m30_meta,
        "candidate_snapshot": {
            **snapshot_meta,
            "loaded": bool(snapshot_loaded),
            "write": snapshot_write_meta,
        },
        "candidate_snapshot_required": not bool(pending_next_session_confirmation),
        "candidate_snapshot_loaded": bool(snapshot_loaded),
        "pending_next_session_confirmation": pending_next_session_confirmation,
        "mainwave_dynamic_cooldown": cooldown,
        "mainwave_recovery_signal_ok": recovery_signal_ok,
        "strategy_contract": {
            "entry_gate": "wave_style_score>=88 && index_mom60<=5% && same_day_industry_mainwave_count>=2 && first_completed_30m(close>=prior20_high, close>=open, amount>=prior20_avg*1.20)",
            "confirmation_name": FORMAL_CONFIRMATION_NAME,
            "confirmation_contract": confirmation_contract_metadata(),
            "slot_policy": "2 slots, 50% per slot, max 2 new buys per day",
            "risk_contract": "-10% hard stop; +10% sell half; remaining exits on previous-day low break confirmed by 30m",
            "cooldown_policy": "not part of the current contract; historical loss streak is diagnostic only",
            "industry_boundary": "QMT current industry is the admission boundary; TDX topics are display-only attribution and never an entry gate.",
        },
    }
    return confirmed.reset_index(drop=True), blocked.reset_index(drop=True), pool.reset_index(drop=True), sector.reset_index(drop=True), meta


def _write_outputs(out_dir: Path, runtime_dir: Path, selected: pd.DataFrame, blocked: pd.DataFrame, pool: pd.DataFrame, sector: pd.DataFrame, summary: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    selected.to_csv(out_dir / "g3_institutional_mainwave_current_candidates.csv", index=False, encoding="utf-8-sig")
    blocked.to_csv(out_dir / "g3_institutional_mainwave_blocked_candidates.csv", index=False, encoding="utf-8-sig")
    pool.head(500).to_csv(out_dir / "current_wave_pool_top500.csv", index=False, encoding="utf-8-sig")
    sector.to_csv(out_dir / "current_sector_diffusion.csv", index=False, encoding="utf-8-sig")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    selected.to_csv(runtime_dir / "latest_candidates.csv", index=False, encoding="utf-8-sig")
    blocked.to_csv(runtime_dir / "latest_blocked_candidates.csv", index=False, encoding="utf-8-sig")
    (runtime_dir / "latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    keep = [
        "entry_date",
        "code",
        "name",
        "industry_key",
        "sector_signal_count",
        "template_label",
        "score",
        "m30_breakout_confirm_datetime",
        "m30_breakout_amount_ratio",
        "index_mom60",
        "reference_close",
        "block_reason",
    ]
    preview = selected[[c for c in keep if c in selected.columns]].to_markdown(index=False) if not selected.empty else "_无候选_"
    blocked_preview = blocked[[c for c in keep if c in blocked.columns]].to_markdown(index=False) if not blocked.empty else "_无阻断候选_"
    report = [
        "# G3 机构主升浪当前候选 v1",
        "",
        "## 摘要",
        "",
        f"- entry_date：`{summary.get('entry_date', '')}`",
        f"- decision_date：`{summary.get('decision_date', '')}`",
        f"- 诊断：`{summary.get('diagnosis_code', '')}`",
        f"- 候选数：`{summary.get('candidate_rows', 0)}`",
        "",
        "## 当前候选",
        "",
        preview,
        "",
        "## 阻断观察池",
        "",
        blocked_preview,
        "",
        "## 纪律",
        "",
        "- 本脚本只产生 shadow 观察候选，不开启正式买点和自动下单。",
        "- 当前行业扩散按当日模板候选的横截面分位复算，用于承接历史 score120 + diffusion65 的实盘化近似。",
        "- 入场价留空，只保留信号日参考收盘价，避免盘前夸大可成交收益。",
        "",
    ]
    (out_dir / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build current institutional mainwave candidates for G3 state router.")
    parser.add_argument("--entry-date", default="")
    parser.add_argument("--decision-date", default="")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--runtime-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--min-amount20", type=float, default=30000.0)
    parser.add_argument("--min-score", type=float, default=88.0)
    parser.add_argument("--min-sector-signal-count", type=int, default=2)
    parser.add_argument("--max-index-mom60", type=float, default=0.05)
    parser.add_argument("--period", type=int, default=30, choices=[15, 30])
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args()

    selected, blocked, pool, sector, meta = build_current_candidates(
        entry_date=args.entry_date.strip(),
        decision_date=args.decision_date.strip(),
        lookback_days=int(args.lookback_days),
        min_amount20=float(args.min_amount20),
        min_score=float(args.min_score),
        min_sector_signal_count=int(args.min_sector_signal_count),
        max_index_mom60=float(args.max_index_mom60),
        period=int(args.period),
        top_n=int(args.top_n),
    )
    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_date": meta.get("entry_date", ""),
        "decision_date": meta.get("decision_date", ""),
        "diagnosis_code": "HAS_INSTITUTIONAL_MAINWAVE_CURRENT_CANDIDATE" if not selected.empty else "NO_INSTITUTIONAL_MAINWAVE_CURRENT_CANDIDATE",
        "candidate_rows": int(len(selected)),
        "blocked_rows": int(len(blocked)),
        "auto_order_allowed_rows": 0,
        "formal_buy_signal_rows": 0,
        "order_path_enabled_rows": 0,
        "live_order_enabled": False,
        "meta": meta,
    }
    _write_outputs(Path(args.out_dir), Path(args.runtime_dir), selected, blocked, pool, sector, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
