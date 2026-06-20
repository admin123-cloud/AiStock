from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_build_four_path_candidates import INDEX_CODE, _json_default  # noqa: E402
from scripts.gen3_update_panic_shadow import _latest_index_trade_date, _next_index_trade_date, _prev_index_trade_date  # noqa: E402
from scripts.scan_current_wave_style_candidates_v1 import _load_current_candidates  # noqa: E402
from scheduler.trading_calendar import TradingCalendar  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path, runtime_path  # noqa: E402


OUT_DIR = report_path("gen3_institutional_mainwave_current_v1")
RUNTIME_DIR = runtime_path("gen3_institutional_mainwave_current")
CURRENT_WAVE_SCAN_DIR = report_path("current_wave_style_candidate_scan_v1")
MARKET_CONTEXT_ARCHIVE = report_path("gen3_four_path_independent_candidates", "market_context.csv")
STATE_ROUTER_CONTEXT_ARCHIVE = report_path("gen3_state_router_shadow_daily_v1", "g3_state_router_market_context.csv")


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


def _resolve_dates(entry_date: str, decision_date: str) -> tuple[str, str]:
    if decision_date:
        decision = _date_text(decision_date)
        entry = _calendar_entry_date(entry_date) if entry_date else _calendar_next_trade_date(decision)
        return entry or decision, decision
    if entry_date:
        entry = _calendar_entry_date(entry_date)
        return entry, _calendar_prev_trade_date(entry)
    try:
        latest = _latest_index_trade_date()
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
    d = pool.copy()
    d = _numeric(d, ["wave_style_score", "ret5", "ret20", "amount20", "big_up_days20", "limit_up_days20"])
    if "code_raw" not in d.columns:
        d["code_raw"] = d.get("code", "")
    d = d.dropna(subset=["l2_sector_name"]).copy()
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values(["l2_sector_name", "code_raw", "wave_style_score"], ascending=[True, True, False]).drop_duplicates(["l2_sector_name", "code_raw"])
    sector = (
        d.groupby("l2_sector_name", dropna=False)
        .agg(
            sector_candidate_count=("code_raw", "count"),
            sector_avg_score=("wave_style_score", "mean"),
            sector_max_score=("wave_style_score", "max"),
            sector_avg_ret5=("ret5", "mean"),
            sector_avg_ret20=("ret20", "mean"),
            sector_big_up_sum=("big_up_days20", "sum"),
            sector_limit_up_sum=("limit_up_days20", "sum"),
            sector_avg_amount20=("amount20", "mean"),
        )
        .reset_index()
    )
    market_count = max(int(len(d)), 1)
    sector["market_candidate_count"] = market_count
    sector["sector_share"] = sector["sector_candidate_count"] / market_count
    for col in ["sector_candidate_count", "sector_avg_score", "sector_avg_ret5", "sector_avg_ret20", "sector_share"]:
        sector[f"{col}_pct"] = pd.to_numeric(sector[col], errors="coerce").rank(pct=True)
    sector["sector_diffusion_score"] = (
        sector["sector_candidate_count_pct"] * 30.0
        + sector["sector_avg_score_pct"] * 20.0
        + sector["sector_avg_ret5_pct"] * 15.0
        + sector["sector_avg_ret20_pct"] * 15.0
        + sector["sector_share_pct"] * 20.0
    )
    return sector.sort_values(["sector_diffusion_score", "sector_candidate_count"], ascending=[False, False]).reset_index(drop=True)


def _compute_30m_features(candidates: pd.DataFrame, decision_date: str, period: int = 30) -> tuple[pd.DataFrame, dict[str, Any]]:
    if candidates.empty:
        return candidates.copy(), {"m30_rows": 0, "m30_table": f"kline_minute_{period}"}
    table = f"kline_minute_{period}"
    rows: list[dict[str, Any]] = []
    signal_day = pd.Timestamp(decision_date).normalize()
    start = (signal_day - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    end = signal_day.strftime("%Y-%m-%d")
    for idx, item in candidates.iterrows():
        code = str(item.get("code_raw") or item.get("code") or "")
        if not code:
            rows.append({"_idx": idx, "m30_ok": False, "m30_status": "missing_code"})
            continue
        try:
            bars = clickhouse_query_df(
                f"""
                SELECT code, datetime, open, high, low, close, amount
                FROM {table}
                WHERE code = ?
                  AND toDate(datetime) BETWEEN ? AND ?
                ORDER BY datetime
                """,
                [code, start, end],
            )
        except Exception as exc:
            rows.append({"_idx": idx, "m30_ok": False, "m30_status": "minute_query_failed", "m30_error": str(exc)})
            continue
        if bars.empty:
            rows.append({"_idx": idx, "m30_ok": False, "m30_status": "missing_minute_bars"})
            continue
        bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
        bars["date"] = bars["datetime"].dt.normalize()
        bars = _numeric(bars, ["open", "high", "low", "close", "amount"])
        bars = bars.dropna(subset=["datetime", "close"]).sort_values("datetime")
        hist = bars[bars["datetime"] <= signal_day + pd.Timedelta(hours=15)].tail(60).copy()
        day = bars[bars["date"].eq(signal_day)].copy()
        if len(hist) < 20 or day.empty:
            rows.append({"_idx": idx, "m30_ok": False, "m30_status": "insufficient_minute_bars"})
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
        rows.append(
            {
                "_idx": idx,
                "m30_ok": True,
                "m30_status": "ok",
                "m30_close_above_ma20": last / ma20 - 1.0 if ma20 > 0 else np.nan,
                "m30_close_above_ma40": last / ma40 - 1.0 if ma40 > 0 else np.nan,
                "m30_mom6": last / prev6 - 1.0 if prev6 > 0 else np.nan,
                "m30_mom12": last / prev12 - 1.0 if prev12 > 0 else np.nan,
                "m30_amount_last2_ratio": amt_last2 / amt_prev20 if amt_prev20 > 0 else np.nan,
                "m30_day_ret": day_close / day_open - 1.0 if day_open > 0 else np.nan,
                "m30_day_close_pos": (day_close - day_low) / day_range,
                "m30_day_amp": day_high / day_low - 1.0 if day_low > 0 else np.nan,
                "m30_last_bar_ret": day_close / last_open - 1.0 if last_open > 0 else np.nan,
            }
        )
    feat = pd.DataFrame(rows).set_index("_idx") if rows else pd.DataFrame()
    out = candidates.join(feat)
    ok_rows = int(pd.Series(out.get("m30_ok", False)).fillna(False).astype(bool).sum()) if not out.empty else 0
    return out, {"m30_rows": int(len(out)), "m30_ok_rows": ok_rows, "m30_table": table}


def _index_mom60(decision_date: str) -> tuple[float | None, dict[str, Any]]:
    end = _date_text(decision_date)
    start = (pd.Timestamp(end) - pd.Timedelta(days=130)).strftime("%Y-%m-%d")
    try:
        df = clickhouse_query_df(
            """
            SELECT trade_date, close
            FROM kline_daily
            WHERE code = ?
              AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [INDEX_CODE, start, end],
        )
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
        return None, {"status": "insufficient_index_rows", "rows": int(len(df))}
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
    if len(df) < 61:
        return None, {"status": "insufficient_index_rows_after_clean", "rows": int(len(df))}
    mom60 = float(df["close"].iloc[-1] / df["close"].iloc[-61] - 1.0)
    return mom60, {"status": "ok", "rows": int(len(df)), "index_code": INDEX_CODE}


def _decorate_signal_rows(rows: pd.DataFrame, entry: str, decision: str, *, eligible: bool) -> pd.DataFrame:
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
    out["score"] = pd.to_numeric(out["wave_style_score"], errors="coerce")
    out["policy"] = "state_router_institutional_mainwave_shadow"
    out["confirm_rule"] = "score120_current_diffusion65_m30_ma20_index_mom60_soft_5pct_hard_10pct"
    out["confirm_datetime"] = pd.Timestamp(decision).strftime("%Y-%m-%d 15:00:00")
    out["entry_ts"] = pd.Timestamp(entry).strftime("%Y-%m-%d 09:30:00")
    out["reference_close"] = pd.to_numeric(out.get("close"), errors="coerce")
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
        status = out.get("m30_status", pd.Series("", index=out.index)).astype(str)
        missing_minute = status.isin(["minute_query_failed", "missing_minute_bars", "insufficient_minute_bars"])
        out["shadow_status"] = "blocked_waiting_confirmation"
        out["block_reason"] = "institutional_wait_30m_confirm_or_no_intraday_data"
        out.loc[~pd.Series(out.get("index_mom60_gate", False), index=out.index).fillna(False).astype(bool), "block_reason"] = "institutional_index_mom60_gt_10pct_hard_gate"
        out.loc[missing_minute, "block_reason"] = "institutional_30m_data_unavailable"
        out.loc[
            pd.Series(out.get("m30_ok", False), index=out.index).fillna(False).astype(bool)
            & ~pd.Series(out.get("m30_confirmed", False), index=out.index).fillna(False).astype(bool),
            "block_reason",
        ] = "institutional_30m_ma20_not_confirmed"
    return out


def build_current_candidates(
    *,
    entry_date: str = "",
    decision_date: str = "",
    lookback_days: int = 90,
    min_amount20: float = 30000.0,
    min_score: float = 120.0,
    min_sector_diffusion: float = 65.0,
    max_index_mom60: float = 0.10,
    period: int = 30,
    top_n: int = 20,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    entry, decision = _resolve_dates(entry_date, decision_date)
    args = argparse.Namespace(target_date=decision, lookback_days=lookback_days, min_amount20=min_amount20)
    pool, scan_meta = _load_current_pool(args)
    pool = _numeric(pool, ["wave_style_score", "ret20", "limit_up_days20", "learned_sector_bonus", "mom60"])
    template_pool = pool[pool.get("template_pass", False).fillna(False).astype(bool)].copy() if "template_pass" in pool.columns else pool.copy()
    sector = _build_sector_diffusion(template_pool)
    candidates = pool[
        pool.get("template_pass", False).fillna(False).astype(bool)
        & pd.to_numeric(pool["wave_style_score"], errors="coerce").ge(float(min_score))
        & pd.to_numeric(pool.get("learned_sector_bonus", 0), errors="coerce").fillna(0).gt(0)
        & pd.to_numeric(pool.get("ret20", 0), errors="coerce").fillna(0).lt(0.55)
        & pd.to_numeric(pool.get("limit_up_days20", 0), errors="coerce").fillna(99).le(4)
    ].copy()
    if not sector.empty and not candidates.empty:
        candidates = candidates.merge(
            sector[
                [
                    "l2_sector_name",
                    "sector_candidate_count",
                    "sector_avg_score",
                    "sector_avg_ret5",
                    "sector_avg_ret20",
                    "sector_share",
                    "sector_diffusion_score",
                ]
            ],
            on="l2_sector_name",
            how="left",
        )
    sector_score = pd.to_numeric(
        candidates["sector_diffusion_score"] if "sector_diffusion_score" in candidates.columns else pd.Series(np.nan, index=candidates.index),
        errors="coerce",
    )
    candidates = candidates[sector_score.ge(float(min_sector_diffusion))].copy()
    candidates, m30_meta = _compute_30m_features(candidates, decision, period=period)
    index_mom60, index_meta = _index_mom60(decision)
    index_gate = index_mom60 is not None and index_mom60 <= float(max_index_mom60)
    pre_confirm_candidates = candidates.copy()
    if not candidates.empty:
        candidates["index_mom60"] = index_mom60
        candidates["index_mom60_gate"] = index_gate
        candidates["index_mom60_heat_state"] = "normal"
        candidates["market_heat_position_scale"] = 1.0
        if index_mom60 is not None and index_mom60 > 0.05:
            candidates["index_mom60_heat_state"] = "high_heat_observe"
            candidates["market_heat_position_scale"] = 1.0
        if index_mom60 is not None and index_mom60 > float(max_index_mom60):
            candidates["index_mom60_heat_state"] = "extreme_heat_no_open"
            candidates["market_heat_position_scale"] = 0.0
        m30_above_ma20 = pd.to_numeric(
            candidates["m30_close_above_ma20"] if "m30_close_above_ma20" in candidates.columns else pd.Series(np.nan, index=candidates.index),
            errors="coerce",
        )
        candidates["m30_confirmed"] = (
            pd.Series(candidates.get("m30_ok", False), index=candidates.index).fillna(False).astype(bool)
            & m30_above_ma20.ge(0.0)
        )
        pre_confirm_candidates = candidates.copy()
        candidates = candidates[candidates["index_mom60_gate"] & candidates["m30_confirmed"]].copy()
    confirmed = _decorate_signal_rows(candidates, entry, decision, eligible=True)
    blocked = pre_confirm_candidates.drop(candidates.index, errors="ignore") if not pre_confirm_candidates.empty else pre_confirm_candidates
    blocked = _decorate_signal_rows(blocked, entry, decision, eligible=False)
    if not confirmed.empty:
        confirmed = confirmed.sort_values(["score", "sector_diffusion_score", "m30_close_above_ma20"], ascending=[False, False, False]).head(int(top_n))
    if not blocked.empty:
        blocked = blocked.sort_values(["score", "sector_diffusion_score"], ascending=[False, False]).head(int(top_n))

    meta = {
        "source": "institutional_mainwave_current_builder_v1",
        "status": "ok" if not candidates.empty else "no_candidate_after_gates",
        "entry_date": entry,
        "decision_date": decision,
        "scan": scan_meta,
        "pool_rows": int(len(pool)),
        "template_pool_rows": int(len(template_pool)),
        "sector_rows": int(len(sector)),
        "rows": int(len(confirmed)),
        "pre_confirm_rows": int(len(pre_confirm_candidates)),
        "pre_confirm_preview": pre_confirm_candidates[
            [c for c in ["code_raw", "stock_name", "l2_sector_name", "template_label", "wave_style_score", "sector_diffusion_score", "m30_status"] if c in pre_confirm_candidates.columns]
        ].head(20).to_dict("records")
        if not pre_confirm_candidates.empty
        else [],
        "min_score": float(min_score),
        "min_sector_diffusion": float(min_sector_diffusion),
        "max_index_mom60": float(max_index_mom60),
        "index_mom60": _safe_float(index_mom60),
        "index": index_meta,
        "m30": m30_meta,
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
        "l2_sector_name",
        "template_label",
        "score",
        "sector_diffusion_score",
        "m30_close_above_ma20",
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
    parser.add_argument("--min-score", type=float, default=120.0)
    parser.add_argument("--min-sector-diffusion", type=float, default=65.0)
    parser.add_argument("--max-index-mom60", type=float, default=0.10)
    parser.add_argument("--period", type=int, default=30, choices=[15, 30])
    parser.add_argument("--top-n", type=int, default=20)
    args = parser.parse_args()

    selected, blocked, pool, sector, meta = build_current_candidates(
        entry_date=args.entry_date.strip(),
        decision_date=args.decision_date.strip(),
        lookback_days=int(args.lookback_days),
        min_amount20=float(args.min_amount20),
        min_score=float(args.min_score),
        min_sector_diffusion=float(args.min_sector_diffusion),
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
