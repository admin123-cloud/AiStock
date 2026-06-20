from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_build_four_path_candidates import (  # noqa: E402
    INDEX_CODE,
    _add_index_features,
    _add_stock_features,
    _build_market_context,
    _json_default,
    _load_index_daily,
    _load_stock_daily,
    _load_trade_dates,
    _select_four_path_candidates,
    _with_entry_date,
)
from scripts.gen3_institutional_mainwave_current_v1 import build_current_candidates as _institutional_current_candidates  # noqa: E402
from scripts.gen3_update_panic_shadow import (  # noqa: E402
    _attach_intraday_confirmation as _panic_intraday_confirmation,
    _build_daily_candidates as _panic_daily_candidates,
    _latest_index_trade_date,
    _next_index_trade_date,
    _prev_index_trade_date,
)
from scheduler.trading_calendar import TradingCalendar  # noqa: E402
from utils.paths import report_path, runtime_path  # noqa: E402


OUT_DIR = report_path("gen3_state_router_shadow_daily_v1")
RUNTIME_DIR = runtime_path("gen3_state_router_shadow")
STATE_ALPHA_RUNTIME_DIR = runtime_path("gen3_state_alpha")
MARKET_CONTEXT_ARCHIVE = report_path("gen3_four_path_independent_candidates", "market_context.csv")
INSTITUTIONAL_REPLAY = report_path("gen3_score120_core_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv")
CURRENT_WAVE_SCAN_SUMMARY = report_path("current_wave_style_candidate_scan_v1", "summary.json")
MODE_TRADE_LIBRARY = report_path("gen3_market_mode_router_v1", "mode_trade_library.csv")
G2_GAP_SUPPLEMENT_LIVE_DIR = report_path("gen2_risk_cool_shadow_ledger", "live_updates")
G2_GAP_SUPPLEMENT_LEDGER = report_path("gen2_risk_cool_shadow_ledger", "shadow_ledger.csv")
G2_V2_COMPLETE_SOURCE = report_path("gen2_v2_complete_strategy", "sources", "g2_v2_complete.parquet")
G2_VOLUME5_ALPHA191_SOURCE = report_path("gen2_alpha191_light_constraint_matrix", "sources", "volume5_keep80_runup_le100.parquet")
FINAL_G3_STRATEGY_ID = "g3_final_with_g2_gap_supplement"
FINAL_G3_STRATEGY_NAME = "G3 Final With G2 Gap Supplement"
FINAL_G3_STRATEGY_NAME_CN = "G3最终版"
FINAL_G3_LEGACY_BASE_PROFILE = "g3_final_top2_mainwave_sector_exempt_v1"
FINAL_G3_PROFILE = "g3_final_with_g2_gap_supplement"
FINAL_G3_PROFILE_NAME = "G3最终版：二槽主升 + G2空档补位"

TRADE_STRATEGY_LABELS = {
    "institutional_score120_mainwave": "机构主升Score120",
    "old_g3_strong_breakout": "强势突破",
    "volume_runup_supplement": "量能续强补位",
    "range_weak_repair": "震荡弱势修复",
    "panic_capitulation_repair": "恐慌出清修复",
}

ROUTE_HEALTH_CONTRACTS = {
    "institutional_mainwave": {
        "window_days": 240,
        "min_count": 2,
        "min_avg_ret": 0.0,
        "max_big_loss_rate": 0.34,
        "big_loss_ret": -0.12,
        "max_worst_loss": -0.25,
    },
    "panic_repair": {
        "window_days": 240,
        "min_count": 3,
        "min_avg_ret": 0.0,
        "max_big_loss_rate": 0.34,
        "big_loss_ret": -0.12,
        "max_worst_loss": -0.18,
    },
    "old_g3_route_v3": {
        "window_days": 240,
        "min_count": 5,
        "min_avg_ret": 0.0,
        "max_big_loss_rate": 0.34,
        "big_loss_ret": -0.12,
        "max_worst_loss": -0.18,
    },
    "g2_gap_supplement": {
        "window_days": 240,
        "min_count": 3,
        "min_avg_ret": 0.0,
        "max_big_loss_rate": 0.34,
        "big_loss_ret": -0.08,
        "max_worst_loss": -0.12,
    },
}


def _date_text(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except Exception:
        return default
    return x if pd.notna(x) else default


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _latest_entry_date_from_frame(df: pd.DataFrame, date_col: str = "entry_date") -> str:
    if df.empty or date_col not in df.columns:
        return ""
    dates = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    dates = dates.dropna()
    return str(dates.max()) if len(dates) else ""


def _latest_entry_date_from_path(path: Path, date_col: str = "entry_date") -> str:
    if not path.exists():
        return ""
    try:
        if path.suffix.lower() == ".parquet":
            df = pd.read_parquet(path, columns=[date_col])
        else:
            df = pd.read_csv(path, usecols=[date_col], low_memory=False, encoding="utf-8-sig")
    except Exception:
        return ""
    return _latest_entry_date_from_frame(df, date_col=date_col)


def _latest_g2_live_update_date() -> str:
    if not G2_GAP_SUPPLEMENT_LIVE_DIR.exists():
        return ""
    dates: list[str] = []
    for path in G2_GAP_SUPPLEMENT_LIVE_DIR.glob("*_filtered_signals.*"):
        text = path.name.split("_filtered_signals", 1)[0]
        date_text = _date_text(text)
        if date_text:
            dates.append(date_text)
    return max(dates) if dates else ""


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


def _resolve_entry_date(value: str) -> tuple[str, str]:
    if value:
        entry_date = _calendar_entry_date(value)
        return entry_date, _calendar_prev_trade_date(entry_date)
    try:
        latest = _latest_index_trade_date()
        if not latest:
            return "", ""
        next_date = _calendar_next_trade_date(latest)
        if next_date and next_date <= latest:
            next_date = ""
        entry_date = next_date or latest
        decision_date = _calendar_prev_trade_date(entry_date)
        return entry_date, decision_date
    except Exception:
        if CURRENT_WAVE_SCAN_SUMMARY.exists():
            summary = json.loads(CURRENT_WAVE_SCAN_SUMMARY.read_text(encoding="utf-8"))
            decision_date = _date_text(summary.get("target_date", ""))
            today = pd.Timestamp(datetime.now()).normalize()
            raw_entry_date = today.strftime("%Y-%m-%d") if decision_date and today > pd.Timestamp(decision_date) else decision_date
            entry_date = _calendar_entry_date(raw_entry_date)
            decision_date = _calendar_prev_trade_date(entry_date)
            if entry_date and decision_date:
                return entry_date, decision_date
        return "", ""


def _current_market_context(decision_date: str, min_amount20: float, max_codes: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not decision_date:
        return pd.DataFrame(), {"ok": False, "reason": "missing_decision_date"}
    try:
        stocks = _load_stock_daily(decision_date, decision_date, max_codes=max_codes)
        features = _add_stock_features(stocks)
        index = _load_index_daily(decision_date, decision_date)
        context = _build_market_context(features, _add_index_features(index), min_amount20=min_amount20)
        context = context[pd.to_datetime(context["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").eq(decision_date)].copy()
        context = context.drop_duplicates(subset=["trade_date"], keep="last")
        return context, {"ok": True, "source": "clickhouse_rebuilt", "rows": int(len(context))}
    except Exception as exc:
        archived = _read_csv(MARKET_CONTEXT_ARCHIVE)
        if not archived.empty and "trade_date" in archived.columns:
            archived["trade_date"] = pd.to_datetime(archived["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            context = archived[archived["trade_date"].eq(decision_date)].copy()
            if not context.empty:
                context = context.drop_duplicates(subset=["trade_date"], keep="last")
                return context, {"ok": True, "source": "archive_fallback", "rows": int(len(context)), "rebuild_error": str(exc)}
        return pd.DataFrame(), {"ok": False, "reason": str(exc)}


def _institutional_regime_health(entry_date: str, window_days: int = 240, min_count: int = 2) -> dict[str, Any]:
    health = _route_regime_health("institutional_mainwave", entry_date, window_days=window_days, min_count=min_count)
    return _institutional_health_aliases(health)


def _route_regime_health(route: str, entry_date: str, window_days: int | None = None, min_count: int | None = None) -> dict[str, Any]:
    contract = dict(ROUTE_HEALTH_CONTRACTS.get(route, {}))
    if window_days is not None:
        contract["window_days"] = window_days
    if min_count is not None:
        contract["min_count"] = min_count
    window = int(contract.get("window_days") or 240)
    minimum = int(contract.get("min_count") or 2)
    min_avg = float(contract.get("min_avg_ret") or 0.0)
    big_loss_ret = float(contract.get("big_loss_ret") or -0.12)
    max_big_loss_rate = float(contract.get("max_big_loss_rate") or 1.0)
    max_worst_loss = float(contract.get("max_worst_loss") or -1.0)

    entry_ts = pd.to_datetime(entry_date, errors="coerce")
    if pd.isna(entry_ts):
        return {
            "route_health_ok": False,
            "route_health_reason": "missing_entry_date",
            "route_health_route": route,
            "route_health_window_days": window,
            "route_health_min_count": minimum,
        }
    lib = _read_csv(MODE_TRADE_LIBRARY)
    if lib.empty:
        return {
            "route_health_ok": False,
            "route_health_reason": "missing_mode_trade_library",
            "route_health_route": route,
            "route_health_window_days": window,
            "route_health_min_count": minimum,
        }
    lib["policy_exit_date"] = pd.to_datetime(lib.get("policy_exit_date"), errors="coerce").dt.normalize()
    lib["net_ret"] = pd.to_numeric(lib.get("net_ret"), errors="coerce")
    route_rows = lib[lib.get("mode", pd.Series("", index=lib.index)).astype(str).eq(route)].copy()
    route_rows = route_rows.dropna(subset=["policy_exit_date", "net_ret"])
    start = entry_ts.normalize() - pd.Timedelta(days=window)
    hist = route_rows[route_rows["policy_exit_date"].lt(entry_ts.normalize()) & route_rows["policy_exit_date"].ge(start)].copy()
    rets = pd.to_numeric(hist["net_ret"], errors="coerce").dropna()
    if rets.empty:
        return {
            "route_health_ok": False,
            "route_health_reason": f"no_recent_{route}_trade_history",
            "route_health_route": route,
            "route_health_window_days": window,
            "route_health_min_count": minimum,
            "route_health_count": 0,
        }
    avg_ret = float(rets.mean())
    win_rate = float((rets > 0).mean())
    big_loss_rate = float((rets <= big_loss_ret).mean())
    worst_ret = float(rets.min())
    ok = int(len(rets)) >= minimum and avg_ret > min_avg and big_loss_rate <= max_big_loss_rate and worst_ret >= max_worst_loss
    failed: list[str] = []
    if int(len(rets)) < minimum:
        failed.append("count_below_min")
    if avg_ret <= min_avg:
        failed.append("avg_ret_not_positive")
    if big_loss_rate > max_big_loss_rate:
        failed.append("big_loss_rate_too_high")
    if worst_ret < max_worst_loss:
        failed.append("worst_loss_too_deep")
    return {
        "route_health_ok": bool(ok),
        "route_health_reason": f"recent_{route}_healthy" if ok else f"recent_{route}_not_healthy",
        "route_health_failed_rules": failed,
        "route_health_route": route,
        "route_health_window_days": window,
        "route_health_min_count": minimum,
        "route_health_count": int(len(rets)),
        "route_health_avg_ret": avg_ret,
        "route_health_win_rate": win_rate,
        "route_health_big_loss_rate": big_loss_rate,
        "route_health_big_loss_ret": big_loss_ret,
        "route_health_worst_ret": worst_ret,
        "route_health_min_avg_ret": min_avg,
        "route_health_max_big_loss_rate": max_big_loss_rate,
        "route_health_max_worst_loss": max_worst_loss,
    }


def _institutional_health_aliases(health: dict[str, Any]) -> dict[str, Any]:
    out = dict(health)
    out.update(
        {
            "inst_regime_ok": bool(health.get("route_health_ok", False)),
            "inst_regime_reason": health.get("route_health_reason"),
            "inst_regime_window_days": health.get("route_health_window_days"),
            "inst_regime_min_count": health.get("route_health_min_count"),
            "inst_regime_count": health.get("route_health_count", 0),
            "inst_regime_avg_ret": health.get("route_health_avg_ret"),
            "inst_regime_win_rate": health.get("route_health_win_rate"),
            "inst_regime_big_loss_rate": health.get("route_health_big_loss_rate"),
            "inst_regime_worst_ret": health.get("route_health_worst_ret"),
        }
    )
    return out


def _apply_route_health(rows: pd.DataFrame, route: str, entry_date: str, meta: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    health = _route_regime_health(route, entry_date)
    out = rows.copy()
    for key, value in health.items():
        if isinstance(value, (list, dict)):
            out[key] = json.dumps(value, ensure_ascii=False, default=_json_default)
        else:
            out[key] = value
    meta = {**meta, "route_health": health}
    if route == "institutional_mainwave":
        aliases = _institutional_health_aliases(health)
        for key, value in aliases.items():
            if isinstance(value, (list, dict)):
                out[key] = json.dumps(value, ensure_ascii=False, default=_json_default)
            else:
                out[key] = value
        meta["institutional_regime_health"] = aliases
    if not bool(health.get("route_health_ok", False)):
        if "route_health_observation" not in out.columns:
            out["route_health_observation"] = ""
        reason = str(health.get("route_health_reason") or f"{route}_route_health_not_ok")
        out["route_health_observation"] = out["route_health_observation"].map(lambda x: _append_block_reason(x, reason))
    return out, meta


def _append_block_reason(old: Any, reason: str) -> str:
    text = "" if pd.isna(old) else str(old)
    if not text:
        return reason
    if reason in text:
        return text
    return f"{text}|{reason}"


def _safe_num(value: Any) -> float | None:
    try:
        x = float(value)
    except Exception:
        return None
    return x if pd.notna(x) else None


def _pct_price(base: float | None, pct: float) -> float | None:
    if base is None or base <= 0:
        return None
    return round(base * (1.0 + pct), 4)


def _infer_trade_strategy(item: dict[str, Any]) -> tuple[str, str, str]:
    route = str(item.get("route") or "")
    market_style = str(item.get("market_style") or "")
    g3_chain = str(item.get("g3_chain") or item.get("route_source") or "")

    if route == "institutional_mainwave":
        key = "institutional_score120_mainwave"
        return key, TRADE_STRATEGY_LABELS[key], "机构主升浪Score120核心"
    if route == "g2_gap_supplement":
        key = "volume_runup_supplement"
        source = "G2量能续强+板块加分" if bool(item.get("sector_bonus", False)) else "G2量能续强"
        return key, TRADE_STRATEGY_LABELS[key], source
    if route == "panic_repair":
        key = "panic_capitulation_repair" if market_style == "standard_downtrend" else "range_weak_repair"
        source = "下跌恐慌修复" if key == "panic_capitulation_repair" else "震荡恐慌修复"
        return key, TRADE_STRATEGY_LABELS[key], source
    if route == "old_g3_route_v3":
        if g3_chain in {"down_panic", "downtrend_panic_capitulation"}:
            key = "panic_capitulation_repair"
            return key, TRADE_STRATEGY_LABELS[key], "旧G3恐慌修复"
        if g3_chain in {"strong_main", "strong_breakout"}:
            key = "old_g3_strong_breakout"
            return key, TRADE_STRATEGY_LABELS[key], "强势突破"
        key = "range_weak_repair"
        return key, TRADE_STRATEGY_LABELS[key], "旧G3弱势/震荡修复"

    key = "range_weak_repair"
    return key, TRADE_STRATEGY_LABELS[key], str(item.get("route_label") or route or "未细分来源")


def _attach_trade_strategy_fields(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    inferred = out.apply(lambda row: _infer_trade_strategy(row.to_dict()), axis=1)
    out["trade_strategy"] = inferred.map(lambda x: x[0])
    out["trade_strategy_label"] = inferred.map(lambda x: x[1])
    out["source_strategy_label"] = inferred.map(lambda x: x[2])
    return out


def _build_shadow_tickets(selected: pd.DataFrame, summary: dict[str, Any]) -> pd.DataFrame:
    if selected.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for item in selected.to_dict("records"):
        route = str(item.get("route") or "")
        trade_strategy, trade_strategy_label, source_strategy_label = _infer_trade_strategy(item)
        risk_scale = _safe_num(item.get("account_risk_position_scale"))
        if risk_scale is None or risk_scale <= 0:
            risk_scale = 1.0
        market_heat_scale = _safe_num(item.get("market_heat_position_scale"))
        heat_state = str(item.get("index_mom60_heat_state") or "")
        if market_heat_scale is None:
            market_heat_scale = 1.0
        if heat_state == "high_heat_reduce_position" and 0 < market_heat_scale < 1.0:
            market_heat_scale = 1.0
        risk_scale = risk_scale * market_heat_scale
        reference_close = _safe_num(item.get("reference_close")) or _safe_num(item.get("close"))
        decision_low = _safe_num(item.get("low"))
        m30_above_ma20 = _safe_num(item.get("m30_close_above_ma20"))
        m30_ma20_proxy = round(reference_close / (1.0 + m30_above_ma20), 4) if reference_close and m30_above_ma20 is not None and (1.0 + m30_above_ma20) > 0 else None

        if route == "institutional_mainwave":
            position_pct = 0.50
            max_position_pct = 0.50
            stop_candidates = [x for x in [decision_low, m30_ma20_proxy, _pct_price(reference_close, -0.12)] if x is not None and x > 0]
            structure_stop = round(max(stop_candidates), 4) if stop_candidates else _pct_price(reference_close, -0.12)
            hard_stop = _pct_price(reference_close, -0.12)
            take_profit_1 = _pct_price(reference_close, 0.12)
            take_profit_1_ratio = 0.5
            exit_contract = "2-slot compound default: 30m hard stop -12%; take +12% sell half; after take-profit, break previous-day low exits remaining; cooldown after repeated hard stops."
        elif route == "g2_gap_supplement":
            position_pct = 0.50
            max_position_pct = 0.50
            g2_fractal_low = _safe_num(item.get("fractal_low"))
            structure_stop = g2_fractal_low or _pct_price(reference_close, -0.05)
            hard_stop = _pct_price(reference_close, -0.05)
            take_profit_1 = _pct_price(reference_close, 0.10)
            take_profit_1_ratio = 0.5
            exit_contract = "G2 gap supplement: buy point comes from g2_v2_complete 30m trigger; use G2 mature stop line, -5% 30m hard stop, +10% partial take-profit, previous-day/structure weakness exit and risk-cool cooldown."
        elif trade_strategy == "panic_capitulation_repair":
            pressure_state = "downtrend_pressure" if str(item.get("market_style") or "") == "standard_downtrend" else "range_panic"
            position_pct = 0.25
            max_position_pct = 0.50
            structure_stop = _pct_price(reference_close, -0.05)
            hard_stop = _pct_price(reference_close, -0.08)
            take_profit_1 = _pct_price(reference_close, 0.08)
            take_profit_1_ratio = 0.5
            exit_contract = "panic capitulation repair unified: daily capitulation-release setup; 30m repair confirmation when intraday data exists; -5% structure stop, -8% hard stop, +8% sell half, remaining exits on previous-day low break or weak 30m close; downtrend pressure uses smaller first size."
        elif trade_strategy == "range_weak_repair":
            pressure_state = "deep_wash_range" if route == "panic_repair" else "weak_rebound_structure"
            position_pct = 0.50
            max_position_pct = 0.50
            structure_stop = _pct_price(reference_close, -0.06)
            hard_stop = _pct_price(reference_close, -0.10)
            take_profit_1 = _pct_price(reference_close, 0.08)
            take_profit_1_ratio = 0.5
            exit_contract = "range weak repair unified: non-mainwave range/weak-rebound repair; daily structure uses drawdown/range-position/close-strength filters, panic-range source adds pressure-release and 30m confirmation; -6% structure stop, -10% hard stop, +8% sell half, remaining exits on previous-day low break or weak 30m close."
        else:
            position_pct = 0.50
            max_position_pct = 0.50
            structure_stop = _pct_price(reference_close, -0.06)
            hard_stop = _pct_price(reference_close, -0.10)
            take_profit_1 = _pct_price(reference_close, 0.08)
            take_profit_1_ratio = 0.5
            exit_contract = "old G3 route: use legacy 30m weakness and previous-day low break as exit proxy."
        position_pct = round(position_pct * risk_scale, 6)
        max_position_pct = round(max_position_pct * risk_scale, 6)

        qualified = bool(item.get("router_eligible", True)) and str(item.get("source_quality") or "") == "current_rebuilt"
        ticket_key = f"{FINAL_G3_STRATEGY_ID}|{item.get('entry_date', '')}|{route}|{item.get('code', '')}"
        rows.append(
            {
                "ticket_key": ticket_key,
                "strategy_id": FINAL_G3_STRATEGY_ID,
                "strategy_name": FINAL_G3_STRATEGY_NAME,
                "strategy_name_cn": FINAL_G3_STRATEGY_NAME_CN,
                "strategy_profile": FINAL_G3_PROFILE,
                "strategy_profile_name": FINAL_G3_PROFILE_NAME,
                "entry_date": item.get("entry_date", summary.get("entry_date", "")),
                "decision_date": item.get("decision_date", summary.get("decision_date", "")),
                "confirm_datetime": item.get("confirm_datetime", ""),
                "planned_entry_ts": item.get("entry_ts", ""),
                "entry_price_policy": "next_trade_day_open_or_manual_shadow_fill",
                "reference_close": reference_close,
                "code": item.get("code", ""),
                "name": item.get("name") or item.get("stock_name", ""),
                "route": route,
                "route_label": item.get("route_label", ""),
                "trade_strategy": trade_strategy,
                "trade_strategy_label": trade_strategy_label,
                "source_strategy_label": source_strategy_label,
                "index_mom60_heat_state": item.get("index_mom60_heat_state", ""),
                "market_heat_position_scale": market_heat_scale,
                "panic_pressure_state": (
                    "downtrend_pressure"
                    if trade_strategy == "panic_capitulation_repair" and str(item.get("market_style") or "") == "standard_downtrend"
                    else ("range_panic" if trade_strategy == "panic_capitulation_repair" else "")
                ),
                "range_repair_state": (
                    "deep_wash_range"
                    if trade_strategy == "range_weak_repair" and route == "panic_repair"
                    else ("weak_rebound_structure" if trade_strategy == "range_weak_repair" else "")
                ),
                "qualified_shadow_buy": qualified,
                "paper_trade_ready": qualified,
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "position_pct": position_pct if qualified else 0.0,
                "max_position_pct": max_position_pct,
                "portfolio_slot_count": 2,
                "daily_open_limit": 2,
                "structure_stop": structure_stop,
                "hard_stop": hard_stop,
                "m30_ma20_proxy": m30_ma20_proxy,
                "take_profit_1": take_profit_1,
                "take_profit_1_sell_ratio": take_profit_1_ratio,
                "exit_contract": exit_contract,
                "score": _safe_num(item.get("score")),
                "sector_name": item.get("l2_sector_name") or item.get("industry", ""),
                "sector_diffusion_score": _safe_num(item.get("sector_diffusion_score")),
                "sector_diffusion_label": item.get("sector_diffusion_label") or _sector_diffusion_label(item.get("sector_diffusion_score")),
                "sector_candidate_count": _safe_num(item.get("sector_candidate_count")),
                "sector_share": _safe_num(item.get("sector_share")),
                "sector_avg_score": _safe_num(item.get("sector_avg_score")),
                "sector_avg_ret5": _safe_num(item.get("sector_avg_ret5")),
                "sector_avg_ret20": _safe_num(item.get("sector_avg_ret20")),
                "same_sector_selected_count": int(_safe_num(item.get("same_sector_selected_count")) or 0),
                "opportunity_explain": item.get("opportunity_explain") or _sector_opportunity_text(item, int(_safe_num(item.get("same_sector_selected_count")) or 0)),
                "wave_style_score": _safe_num(item.get("wave_style_score")),
                "m30_confirmed": bool(item.get("m30_confirmed", False)),
                "m30_status": item.get("m30_status", ""),
                "inst_regime_ok": bool(item.get("inst_regime_ok", False)),
                "block_reason": item.get("block_reason", ""),
                "shadow_status": "qualified_shadow_buy" if qualified else "blocked_shadow_observe",
                "created_at": summary.get("generated_at", ""),
                "last_state": "planned",
            }
        )
    return pd.DataFrame(rows)


def _shadow_ledger_append_decision(now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now()
    minutes = current.hour * 60 + current.minute
    in_session_window = current.weekday() < 5 and (9 * 60) <= minutes <= (15 * 60 + 30)
    return {
        "allowed": bool(in_session_window),
        "reason": "trading_session_append_window" if in_session_window else "outside_trading_session_do_not_turn_candidates_into_holdings",
        "checked_at": current.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _append_shadow_ledger(runtime_dir: Path, tickets: pd.DataFrame, append_allowed: bool = True) -> pd.DataFrame:
    ledger_path = runtime_dir / "shadow_ledger.csv"
    ledger = _read_csv(ledger_path)
    if not append_allowed:
        return ledger
    if tickets.empty:
        return ledger

    combined = pd.concat([ledger, tickets], ignore_index=True, sort=False) if not ledger.empty else tickets.copy()
    logical_key = [col for col in ["entry_date", "code", "route"] if col in combined.columns]
    if len(logical_key) == 3:
        combined = combined.drop_duplicates(subset=logical_key, keep="last")
    elif "ticket_key" in combined.columns:
        combined = combined.drop_duplicates(subset=["ticket_key"], keep="last")
    combined.to_csv(ledger_path, index=False, encoding="utf-8-sig")
    return combined


def _account_risk_state(entry_date: str) -> dict[str, Any]:
    ledger_path = STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"
    ledger = _read_csv(ledger_path)
    base = {
        "account_risk_ok": True,
        "account_risk_action": "normal",
        "account_risk_reason": "no_closed_shadow_history",
        "position_scale": 1.0,
        "closed_count": 0,
        "current_drawdown": 0.0,
        "consecutive_realized_loss": 0.0,
        "recent_hard_stop_count": 0,
        "ledger_path": str(ledger_path),
    }
    if ledger.empty:
        return base
    d = ledger.copy()
    for col in ["net_ret", "realized_ret", "account_ret", "position_pct"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    status = d.get("last_state", d.get("trade_status", pd.Series("", index=d.index))).fillna("").astype(str).str.lower()
    exit_reason = d.get("exit_reason", d.get("last_exit_reason", pd.Series("", index=d.index))).fillna("").astype(str).str.lower()
    if "account_ret" in d.columns:
        ret_source = d["account_ret"]
    elif "net_ret" in d.columns:
        ret_source = d["net_ret"]
    elif "realized_ret" in d.columns:
        ret_source = d["realized_ret"]
    else:
        ret_source = pd.Series(pd.NA, index=d.index)
    has_ret = pd.to_numeric(ret_source, errors="coerce").notna()
    closed_mask = has_ret | status.str.contains("closed|exit|sold|stopped|hard_stop", regex=True) | exit_reason.str.contains("hard_stop|take_profit|prev_low|policy_exit", regex=True)
    closed = d[closed_mask].copy()
    if closed.empty:
        return base
    if "account_ret" in closed.columns and closed["account_ret"].notna().any():
        account_ret = pd.to_numeric(closed["account_ret"], errors="coerce")
    else:
        if "net_ret" in closed.columns:
            trade_ret_source = closed["net_ret"]
        elif "realized_ret" in closed.columns:
            trade_ret_source = closed["realized_ret"]
        else:
            trade_ret_source = pd.Series(pd.NA, index=closed.index)
        trade_ret = pd.to_numeric(trade_ret_source, errors="coerce")
        pos = pd.to_numeric(closed.get("position_pct"), errors="coerce").fillna(0.0)
        account_ret = trade_ret * pos
    closed["account_ret_calc"] = account_ret.fillna(0.0)
    date_col = None
    for candidate in ["exit_date", "policy_exit_date", "closed_at", "updated_at", "created_at", "entry_date"]:
        if candidate in closed.columns:
            date_col = candidate
            break
    if date_col:
        closed["_risk_date"] = pd.to_datetime(closed[date_col], errors="coerce")
    else:
        closed["_risk_date"] = pd.NaT
    closed = closed.sort_values(["_risk_date", "ticket_key" if "ticket_key" in closed.columns else closed.columns[0]])
    eq = (1.0 + closed["account_ret_calc"]).cumprod()
    peak = eq.cummax()
    dd = (eq / peak) - 1.0
    current_dd = float(dd.iloc[-1]) if len(dd) else 0.0
    consecutive_loss = 0.0
    for value in reversed(closed["account_ret_calc"].tolist()):
        if value < 0:
            consecutive_loss += abs(float(value))
        else:
            break
    recent = closed.tail(20).copy()
    recent_status = recent.get("last_state", recent.get("trade_status", pd.Series("", index=recent.index))).fillna("").astype(str).str.lower()
    recent_exit = recent.get("exit_reason", recent.get("last_exit_reason", pd.Series("", index=recent.index))).fillna("").astype(str).str.lower()
    recent_hard_stop_count = int((recent_status.str.contains("hard_stop", regex=False) | recent_exit.str.contains("hard_stop", regex=False)).sum())

    action = "normal"
    reason = "account_risk_normal"
    ok = True
    position_scale = 1.0
    if current_dd <= -0.18:
        action = "pause_new_buy"
        reason = "mtm_drawdown_pause_new_buy"
        ok = False
        position_scale = 0.0
    elif consecutive_loss >= 0.20:
        action = "pause_new_buy"
        reason = "consecutive_realized_loss_pause_new_buy"
        ok = False
        position_scale = 0.0
    elif recent_hard_stop_count >= 2:
        action = "pause_new_buy"
        reason = "hard_stop_cooldown_pause_new_buy"
        ok = False
        position_scale = 0.0
    elif current_dd <= -0.15:
        action = "reduce_risk"
        reason = "mtm_drawdown_reduce_risk"
        position_scale = 0.5
    return {
        "account_risk_ok": bool(ok),
        "account_risk_action": action,
        "account_risk_reason": reason,
        "position_scale": position_scale,
        "closed_count": int(len(closed)),
        "current_drawdown": current_dd,
        "consecutive_realized_loss": consecutive_loss,
        "recent_hard_stop_count": recent_hard_stop_count,
        "risk_window_closed_count": int(len(recent)),
        "ledger_path": str(ledger_path),
        "entry_date": entry_date,
    }


def _apply_account_risk(selected: pd.DataFrame, risk: dict[str, Any]) -> pd.DataFrame:
    if selected.empty:
        return selected.copy()
    out = selected.copy()
    action = str(risk.get("account_risk_action") or "normal")
    reason = str(risk.get("account_risk_reason") or "")
    scale = _safe_float(risk.get("position_scale"), 1.0)
    out["account_risk_action"] = action
    out["account_risk_reason"] = reason
    out["account_risk_position_scale"] = scale
    if action == "pause_new_buy":
        if "router_eligible" not in out.columns:
            out["router_eligible"] = True
        out["router_eligible"] = False
        if "block_reason" not in out.columns:
            out["block_reason"] = ""
        out["block_reason"] = out["block_reason"].map(lambda x: _append_block_reason(x, reason))
        out["shadow_status"] = "blocked_account_risk_pause_new_buy"
        return out.iloc[0:0].copy()
    return out


def _panic_source(entry_date: str, top_n: int, min_amount20: float, max_codes: int, period: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    daily, daily_meta = _panic_daily_candidates(
        entry_date=entry_date,
        top_n=top_n,
        min_amount20=min_amount20,
        max_codes=max_codes,
    )
    rows, intra_meta = _panic_intraday_confirmation(daily, period)
    if rows.empty:
        return rows, {**daily_meta, **intra_meta, "source": "panic_current_builder"}
    out = rows.copy()
    out["route"] = "panic_repair"
    out["route_label"] = "恐慌修复"
    out["route_source"] = "panic_current_builder"
    out["source_quality"] = "current_rebuilt"
    out["profile"] = "g3_market_state_router_v1"
    out["strategy_id"] = "g3_market_state_router_strategy_v1"
    out["score"] = pd.to_numeric(out.get("candidate_score"), errors="coerce")
    out["entry_date"] = entry_date
    out["policy"] = "state_router_panic_shadow"
    out["confirm_rule"] = out.get("confirm_rule", "panic_30m_confirm")
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False
    out["router_eligible"] = out.get("shadow_status", pd.Series("", index=out.index)).astype(str).eq("intraday_confirmed")
    out["block_reason"] = out.get("shadow_status", "").astype(str).where(
        out.get("shadow_status", pd.Series("", index=out.index)).astype(str).eq("intraday_confirmed"),
        "panic_wait_intraday_confirm_or_no_intraday_data",
    )
    out = _attach_trade_strategy_fields(out)
    meta = {**daily_meta, **intra_meta, "source": "panic_current_builder"}
    out, meta = _apply_route_health(out, "panic_repair", entry_date, meta)
    return out, meta


def _institutional_replay_source(entry_date: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = _read_csv(INSTITUTIONAL_REPLAY)
    if df.empty:
        return pd.DataFrame(), {"source": "institutional_replay", "rows": 0, "status": "missing"}
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    day = df[df["entry_date"].eq(entry_date)].copy()
    if day.empty:
        return day, {"source": "institutional_replay", "rows": 0, "status": "no_replay_row_for_entry_date"}
    day["route"] = "institutional_mainwave"
    day["route_label"] = "机构主升浪"
    day["route_source"] = "score120_core_replay_adapter"
    day["source_quality"] = "historical_replay_only"
    day["profile"] = "g3_market_state_router_v1"
    day["strategy_id"] = "g3_market_state_router_strategy_v1"
    day["score"] = pd.to_numeric(day.get("score", day.get("selected_score")), errors="coerce")
    day["policy"] = "state_router_institutional_replay_shadow"
    day["shadow_action"] = "observe_only"
    day["auto_order_allowed"] = False
    day["formal_buy_signal"] = False
    day["order_path_enabled"] = False
    day["block_reason"] = "historical_replay_only_requires_current_candidate_generator"
    return day, {"source": "institutional_replay", "rows": int(len(day)), "status": "replay_only"}


def _institutional_current_source(
    entry_date: str,
    decision_date: str,
    top_n: int,
    min_amount20: float,
    period: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    confirmed, blocked, _pool, _sector, meta = _institutional_current_candidates(
        entry_date=entry_date,
        decision_date=decision_date,
        min_amount20=min_amount20,
        period=period,
        top_n=top_n,
    )
    meta = {**meta, "source": "institutional_mainwave_current_builder_v1"}
    rows = pd.concat([confirmed, blocked], ignore_index=True, sort=False) if not blocked.empty else confirmed
    if rows.empty:
        return rows, meta
    rows, meta = _apply_route_health(rows, "institutional_mainwave", entry_date, meta)
    return rows, meta


def _old_g3_current_source(entry_date: str, decision_date: str, top_n: int, min_amount20: float, max_codes: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not entry_date or not decision_date:
        return pd.DataFrame(), {"source": "old_g3_current_builder_v1", "rows": 0, "status": "missing_dates"}
    try:
        trade_dates = _load_trade_dates(decision_date, entry_date)
        stocks = _load_stock_daily(decision_date, decision_date, max_codes=max_codes)
        features = _add_stock_features(stocks)
        index = _load_index_daily(decision_date, decision_date)
        context = _build_market_context(features, _add_index_features(index), min_amount20=min_amount20)
        candidates = _select_four_path_candidates(features, context, top_n=top_n, min_amount20=min_amount20)
        candidates = _with_entry_date(candidates, trade_dates)
        candidates = candidates[pd.to_datetime(candidates["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d").eq(entry_date)].copy()
    except Exception as exc:
        return pd.DataFrame(), {"source": "old_g3_current_builder_v1", "rows": 0, "status": "failed", "error": str(exc)}
    if candidates.empty:
        return candidates, {"source": "old_g3_current_builder_v1", "rows": 0, "status": "no_candidate"}
    candidates["router_eligible"] = ~candidates["g3_chain"].astype(str).eq("range_box_bottom")
    candidates["route"] = "old_g3_route_v3"
    candidates["route_label"] = "旧G3跨周期路由"
    candidates["route_source"] = "old_g3_four_path_current_builder_v1"
    candidates["source_quality"] = "current_rebuilt"
    candidates["profile"] = "g3_market_state_router_v1"
    candidates["strategy_id"] = "g3_market_state_router_strategy_v1"
    candidates["score"] = pd.to_numeric(candidates.get("candidate_score"), errors="coerce")
    candidates["policy"] = "state_router_old_g3_current_shadow"
    candidates["confirm_rule"] = "previous_day_four_path_current_non_range_low_absorb"
    candidates["confirm_datetime"] = pd.Timestamp(decision_date).strftime("%Y-%m-%d 15:00:00")
    candidates["entry_ts"] = pd.Timestamp(entry_date).strftime("%Y-%m-%d 09:30:00")
    candidates["shadow_action"] = "observe_only"
    candidates["auto_order_allowed"] = False
    candidates["formal_buy_signal"] = False
    candidates["order_path_enabled"] = False
    candidates["shadow_status"] = "current_rebuilt_route_candidate"
    candidates["block_reason"] = ""
    candidates.loc[~candidates["router_eligible"], "shadow_status"] = "blocked_range_low_absorb_not_regular_route"
    candidates.loc[~candidates["router_eligible"], "block_reason"] = "range_box_bottom_not_regular_state_router_route"
    candidates = _attach_trade_strategy_fields(candidates)
    meta = {
        "source": "old_g3_current_builder_v1",
        "rows": int(len(candidates)),
        "eligible_rows": int(candidates["router_eligible"].fillna(False).astype(bool).sum()),
        "blocked_rows": int((~candidates["router_eligible"].fillna(False).astype(bool)).sum()),
        "status": "current_rebuilt",
    }
    candidates, meta = _apply_route_health(candidates, "old_g3_route_v3", entry_date, meta)
    eligible_rows = int(candidates["router_eligible"].fillna(False).astype(bool).sum())
    meta["eligible_rows"] = eligible_rows
    meta["blocked_rows"] = int(len(candidates) - eligible_rows)
    return candidates, meta


def _load_g2_gap_supplement_frame(entry_date: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    source_status = {
        "requested_entry_date": entry_date,
        "latest_live_update_date": _latest_g2_live_update_date(),
        "latest_ledger_entry_date": "",
        "latest_v2_complete_entry_date": _latest_entry_date_from_path(G2_V2_COMPLETE_SOURCE),
        "latest_volume5_alpha191_entry_date": _latest_entry_date_from_path(G2_VOLUME5_ALPHA191_SOURCE),
    }
    live_csv = G2_GAP_SUPPLEMENT_LIVE_DIR / f"{entry_date}_filtered_signals.csv"
    if live_csv.exists():
        rows = _read_csv(live_csv)
        return rows, {
            **source_status,
            "source_file": str(live_csv),
            "source_freshness": "live_filtered_signals",
            "fresh_for_entry_date": True,
            "rows": int(len(rows)),
        }
    ledger = _read_csv(G2_GAP_SUPPLEMENT_LEDGER)
    source_status["latest_ledger_entry_date"] = _latest_entry_date_from_frame(ledger)
    if ledger.empty:
        return pd.DataFrame(), {
            **source_status,
            "source_file": str(G2_GAP_SUPPLEMENT_LEDGER),
            "source_freshness": "missing",
            "fresh_for_entry_date": False,
            "stale_reason": "g2_gap_supplement_ledger_missing",
            "rows": 0,
        }
    ledger["entry_date"] = pd.to_datetime(ledger.get("entry_date"), errors="coerce").dt.strftime("%Y-%m-%d")
    rows = ledger[ledger["entry_date"].eq(entry_date)].copy()
    has_any_current_source = any(
        str(source_status.get(key) or "") >= entry_date
        for key in ["latest_live_update_date", "latest_ledger_entry_date", "latest_v2_complete_entry_date", "latest_volume5_alpha191_entry_date"]
    )
    return rows, {
        **source_status,
        "source_file": str(G2_GAP_SUPPLEMENT_LEDGER),
        "source_freshness": "shadow_ledger_fallback" if not rows.empty else "stale_no_entry_date_rows",
        "fresh_for_entry_date": False,
        "stale_reason": "" if rows.empty and has_any_current_source else f"g2_gap_supplement_source_not_fresh_for_{entry_date}",
        "rows": int(len(rows)),
    }


def _g2_gap_supplement_source(entry_date: str, top_n: int) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows, meta = _load_g2_gap_supplement_frame(entry_date)
    meta = {**meta, "source": "g2_gap_supplement_current_builder_v1"}
    if rows.empty:
        return rows, {**meta, "status": "no_candidate"}
    out = rows.copy()
    if "entry_date" not in out.columns:
        out["entry_date"] = entry_date
    out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out[out["entry_date"].eq(entry_date)].copy()
    if out.empty:
        return out, {**meta, "status": "no_candidate_for_entry_date"}
    if "buy_allowed" in out.columns:
        buy_allowed = out["buy_allowed"].fillna(False).astype(str).str.lower().isin({"1", "true", "yes", "ok"})
        out = out[buy_allowed].copy()
    if out.empty:
        return out, {**meta, "status": "all_candidates_blocked_by_g2_buy_allowed"}

    out["route"] = "g2_gap_supplement"
    out["route_label"] = "G2补充买点"
    out["route_source"] = "gen2_risk_cool_shadow_filtered_signals"
    out["source_quality"] = "current_rebuilt" if bool(meta.get("fresh_for_entry_date")) else "ledger_fallback_observe"
    out["profile"] = "g3_market_state_router_v1"
    out["strategy_id"] = FINAL_G3_STRATEGY_ID
    out["strategy_profile"] = FINAL_G3_PROFILE
    out["strategy_profile_name"] = FINAL_G3_PROFILE_NAME
    out["score"] = pd.to_numeric(out.get("v4_score", out.get("score")), errors="coerce")
    out["policy"] = "state_router_g2_gap_supplement_shadow"
    out["confirm_rule"] = "g2_v2_complete_30m_buy_point"
    if "entry_ts" not in out.columns:
        out["entry_ts"] = out.get("confirm_datetime", "")
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False
    out["shadow_status"] = "g2_gap_supplement_candidate"
    out["router_eligible"] = bool(meta.get("fresh_for_entry_date"))
    out["block_reason"] = ""
    out.loc[~out["router_eligible"], "shadow_status"] = "blocked_g2_gap_ledger_fallback_observe"
    out.loc[~out["router_eligible"], "block_reason"] = "g2_gap_supplement_requires_fresh_filtered_signals"
    out = out.sort_values(
        [c for c in ["day_signal_rank", "v4_rank", "score", "code"] if c in out.columns],
        ascending=[True, True, False, True][: len([c for c in ["day_signal_rank", "v4_rank", "score", "code"] if c in out.columns])],
    ).head(top_n)
    meta.update(
        {
            "status": "current_rebuilt" if bool(meta.get("fresh_for_entry_date")) else "ledger_fallback_observe",
            "rows": int(len(out)),
            "eligible_rows": int(out["router_eligible"].fillna(False).astype(bool).sum()),
            "blocked_rows": int((~out["router_eligible"].fillna(False).astype(bool)).sum()),
        }
    )
    out, meta = _apply_route_health(out, "g2_gap_supplement", entry_date, meta)
    return out, meta


def _route_order(context: pd.DataFrame, available: set[str]) -> tuple[list[str], str]:
    if context.empty:
        return [m for m in ["panic_repair", "institutional_mainwave", "old_g3_route_v3", "g2_gap_supplement"] if m in available], "missing_market_context_default_priority"
    row = context.iloc[0]
    style = str(row.get("market_style") or "")
    up_rate = _safe_float(row.get("up_rate"))
    big_down = _safe_float(row.get("big_down_rate"))
    limit_down = _safe_float(row.get("limit_down_proxy_rate"))
    panic = big_down >= 0.15 or limit_down >= 0.03 or (style == "standard_downtrend" and up_rate <= 0.35)
    if panic and "panic_repair" in available:
        return ["panic_repair"], "前日恐慌扩散，优先恐慌修复"
    if "institutional_mainwave" in available:
        return ["institutional_mainwave"], "机构主升候选存在，优先机构主升"
    if "old_g3_route_v3" in available:
        return ["old_g3_route_v3"], "无机构主升，回到旧G3跨周期路由"
    if "g2_gap_supplement" in available:
        return ["g2_gap_supplement"], "G3主路由无可用候选，启用G2补充买点"
    return [], "无可用候选"


def _sector_key(row: dict[str, Any]) -> str:
    for key in ["l2_sector_name", "industry", "sector_name", "template_label"]:
        value = row.get(key)
        text = "" if pd.isna(value) else str(value).strip()
        if text:
            return text
    return ""


def _sector_diffusion_label(score: Any) -> str:
    value = _safe_num(score)
    if value is None:
        return "扩散未知"
    if value >= 80:
        return "强扩散"
    if value >= 65:
        return "有效扩散"
    if value >= 50:
        return "观察扩散"
    return "扩散不足"


def _sector_pct_text(value: Any) -> str:
    num = _safe_num(value)
    if num is None:
        return "--"
    return f"{num * 100:.1f}%"


def _sector_opportunity_text(item: dict[str, Any], same_sector_selected_count: int = 0) -> str:
    route = str(item.get("route") or "")
    if route != "institutional_mainwave":
        sector = _sector_key(item)
        return f"非机构主升路线；板块{sector or '未识别'}只用于重复暴露检查。"

    sector = _sector_key(item) or "未识别板块"
    diffusion = _safe_num(item.get("sector_diffusion_score"))
    count = _safe_num(item.get("sector_candidate_count"))
    share = item.get("sector_share")
    avg_score = _safe_num(item.get("sector_avg_score"))
    avg_ret5 = _safe_num(item.get("sector_avg_ret5"))
    label = _sector_diffusion_label(diffusion)
    parts = [f"机构主升机会落在{sector}，{label}"]
    if diffusion is not None:
        parts.append(f"扩散分{diffusion:.1f}")
    if count is not None:
        parts.append(f"同板块模板候选{int(count)}只")
    if share is not None:
        parts.append(f"占全池{_sector_pct_text(share)}")
    if avg_score is not None:
        parts.append(f"板块均分{avg_score:.1f}")
    if avg_ret5 is not None:
        parts.append(f"5日均涨幅{_sector_pct_text(avg_ret5)}")
    if same_sector_selected_count >= 2:
        parts.append("本次同板块多票属于机构主升共振，合同允许")
    elif same_sector_selected_count == 1:
        parts.append("本次为该板块单票暴露")
    return "；".join(parts) + "。"


def _allow_same_sector(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    return str(candidate.get("route") or "") == "institutional_mainwave" and str(existing.get("route") or "") == "institutional_mainwave"


def _select_router_candidates(all_rows: pd.DataFrame, context: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    if all_rows.empty:
        return all_rows.copy(), {"selected_route": "", "reason": "no_source_candidates"}
    eligible = all_rows.copy()
    if "router_eligible" in eligible.columns:
        eligible = eligible[eligible["router_eligible"].fillna(True).astype(bool)].copy()
    if eligible.empty:
        return all_rows.iloc[0:0].copy(), {
            "selected_route": "",
            "reason": "no_router_eligible_source_candidates",
            "available_routes": sorted(set(all_rows["route"].astype(str))),
        }
    available = set(eligible["route"].astype(str))
    modes, reason = _route_order(context, available)
    if not modes:
        return all_rows.iloc[0:0].copy(), {"selected_route": "", "reason": reason, "available_routes": sorted(available)}
    selected = eligible[eligible["route"].isin(modes)].copy()
    selected["route_pick_rank"] = selected["route"].map({m: i for i, m in enumerate(modes)}).fillna(99)
    selected["_score_sort"] = pd.to_numeric(selected.get("score"), errors="coerce").fillna(-1e9)
    ranked = selected.sort_values(["route_pick_rank", "_score_sort", "code"], ascending=[True, False, True]).copy()
    picked_rows: list[dict[str, Any]] = []
    skipped_same_sector = 0
    skipped_duplicate_code = 0
    for row in ranked.to_dict("records"):
        sector = _sector_key(row)
        if sector and any(_sector_key(picked) == sector and not _allow_same_sector(row, picked) for picked in picked_rows):
            skipped_same_sector += 1
            continue
        if any(str(picked.get("code") or "") == str(row.get("code") or "") for picked in picked_rows):
            skipped_duplicate_code += 1
            continue
        picked_rows.append(row)
        if len(picked_rows) >= 2:
            break
    supplement_rows = 0
    if len(picked_rows) < 2 and "g2_gap_supplement" in available:
        supplement = eligible[eligible["route"].astype(str).eq("g2_gap_supplement")].copy()
        supplement["_score_sort"] = pd.to_numeric(supplement.get("score"), errors="coerce").fillna(-1e9)
        supplement = supplement.sort_values(["_score_sort", "code"], ascending=[False, True]).copy()
        for row in supplement.to_dict("records"):
            sector = _sector_key(row)
            if any(str(picked.get("code") or "") == str(row.get("code") or "") for picked in picked_rows):
                skipped_duplicate_code += 1
                continue
            if sector and any(_sector_key(picked) == sector and not _allow_same_sector(row, picked) for picked in picked_rows):
                skipped_same_sector += 1
                continue
            row["supplement_role"] = "g2_gap_fill_after_g3_primary"
            row["route_pick_rank"] = 90
            picked_rows.append(row)
            supplement_rows += 1
            if len(picked_rows) >= 2:
                break
    selected = pd.DataFrame(picked_rows, columns=ranked.columns).drop(columns=["_score_sort"], errors="ignore")
    if not selected.empty:
        sector_counts = selected.apply(lambda row: _sector_key(row.to_dict()), axis=1).value_counts().to_dict()
        selected["same_sector_selected_count"] = selected.apply(
            lambda row: int(sector_counts.get(_sector_key(row.to_dict()), 0) or 0),
            axis=1,
        )
        selected["opportunity_explain"] = selected.apply(
            lambda row: _sector_opportunity_text(row.to_dict(), int(row.get("same_sector_selected_count") or 0)),
            axis=1,
        )
    return selected, {
        "selected_route": str(selected.iloc[0]["route"]) if not selected.empty else "",
        "reason": reason,
        "available_routes": sorted(available),
        "daily_open_limit": 2,
        "same_sector_policy": "allow_same_sector_only_between_institutional_mainwave_candidates; g2 supplement cannot duplicate code or sector with selected non-mainwave candidates",
        "skipped_same_sector": skipped_same_sector,
        "skipped_duplicate_code": skipped_duplicate_code,
        "g2_gap_supplement_rows": supplement_rows,
    }


def _standardize_output(rows: pd.DataFrame, entry_date: str, decision_date: str) -> pd.DataFrame:
    if rows.empty:
        return rows.copy()
    out = rows.copy()
    for col, default in [
        ("entry_date", entry_date),
        ("confirm_datetime", pd.Timestamp(decision_date).strftime("%Y-%m-%d 15:00:00") if decision_date else ""),
        ("entry_ts", pd.Timestamp(entry_date).strftime("%Y-%m-%d 09:30:00") if entry_date else ""),
        ("entry_price", pd.NA),
        ("chain", ""),
        ("g3_chain", ""),
    ]:
        if col not in out.columns:
            out[col] = default
    out["chain"] = out["chain"].where(out["chain"].astype(str).ne(""), "state_router:" + out["route"].astype(str))
    out["g3_chain"] = out["g3_chain"].where(out["g3_chain"].astype(str).ne(""), out["chain"])
    out["candidate_key"] = out["route"].astype(str) + "|" + out["entry_date"].astype(str) + "|" + out["code"].astype(str)
    if "same_sector_selected_count" not in out.columns:
        out["same_sector_selected_count"] = 0
    if "sector_diffusion_label" not in out.columns:
        out["sector_diffusion_label"] = out.get("sector_diffusion_score", pd.Series(pd.NA, index=out.index)).map(_sector_diffusion_label)
    if "opportunity_explain" not in out.columns:
        out["opportunity_explain"] = out.apply(lambda row: _sector_opportunity_text(row.to_dict(), 0), axis=1)
    return out


def _route_diagnostics(all_rows: pd.DataFrame) -> pd.DataFrame:
    if all_rows.empty:
        return pd.DataFrame(
            columns=[
                "route",
                "route_label",
                "rows",
                "eligible_rows",
                "blocked_rows",
                "top_block_reason",
                "route_health_ok",
                "route_health_count",
                "route_health_avg_ret",
                "route_health_win_rate",
                "route_health_big_loss_rate",
                "route_health_worst_ret",
            ]
        )
    d = all_rows.copy()
    if "router_eligible" not in d.columns:
        d["router_eligible"] = True
    d["router_eligible"] = d["router_eligible"].fillna(True).astype(bool)
    if "block_reason" not in d.columns:
        d["block_reason"] = ""
    rows: list[dict[str, Any]] = []
    for route, g in d.groupby("route", dropna=False):
        blocked = g[~g["router_eligible"]].copy()
        reason = ""
        if not blocked.empty:
            reason_counts = blocked["block_reason"].fillna("").astype(str).value_counts()
            reason = str(reason_counts.index[0]) if len(reason_counts) else ""
        rows.append(
            {
                "route": route,
                "route_label": str(g.get("route_label", pd.Series("", index=g.index)).iloc[0]),
                "rows": int(len(g)),
                "eligible_rows": int(g["router_eligible"].sum()),
                "blocked_rows": int((~g["router_eligible"]).sum()),
                "top_block_reason": reason,
                "route_health_ok": bool(g.get("route_health_ok", pd.Series(True, index=g.index)).iloc[0]),
                "route_health_count": int(_safe_float(g.get("route_health_count", pd.Series(0, index=g.index)).iloc[0])),
                "route_health_avg_ret": _safe_float(g.get("route_health_avg_ret", pd.Series(0.0, index=g.index)).iloc[0]),
                "route_health_win_rate": _safe_float(g.get("route_health_win_rate", pd.Series(0.0, index=g.index)).iloc[0]),
                "route_health_big_loss_rate": _safe_float(g.get("route_health_big_loss_rate", pd.Series(0.0, index=g.index)).iloc[0]),
                "route_health_worst_ret": _safe_float(g.get("route_health_worst_ret", pd.Series(0.0, index=g.index)).iloc[0]),
            }
        )
    return pd.DataFrame(rows).sort_values(["eligible_rows", "rows"], ascending=[False, False]).reset_index(drop=True)


def _strategy_contract() -> dict[str, Any]:
    return {
        "strategy_id": FINAL_G3_STRATEGY_ID,
        "legacy_research_strategy_id": "g3_market_state_router_strategy_v1",
        "strategy_name": FINAL_G3_STRATEGY_NAME,
        "strategy_name_cn": FINAL_G3_STRATEGY_NAME_CN,
        "profile": FINAL_G3_PROFILE,
        "profile_name": FINAL_G3_PROFILE_NAME,
        "legacy_base_profile": FINAL_G3_LEGACY_BASE_PROFILE,
        "finalized_variant": FINAL_G3_PROFILE,
        "legacy_finalized_variant": "eligible_top2_sector_guard_mainwave_exempt_sector_for_distinct",
        "stage": "shadow_trade_mature",
        "mode": "qualified_shadow_trade",
        "promotion_policy": {
            "formal_order_status": "disabled",
            "promotion_gate": [
                "shadow ledger must record planned/fill/exit states without missing trigger timestamps",
                "30m confirmation freshness must be available before a qualified shadow buy",
                "route health must be recorded as observation-only diagnostics and must not hard-block qualified shadow buys",
                "account risk gate must allow new buys: MTM drawdown, consecutive realized loss, and hard-stop cooldown",
                "exit replay must prove structure stop and hard stop are observable before order integration",
            ],
        },
        "portfolio_contract": {
            "base_capital_mode": "slot_based_shadow",
            "daily_open_limit": 2,
            "position_framework": "2_slots_compound_default",
            "slots": 2,
            "slot_pct": 0.50,
            "trade_strategy_framework": "5_consolidated_trade_strategies",
            "trade_strategy_count": 5,
            "institutional_mainwave_position_pct": 0.50,
            "panic_capitulation_repair_position_pct": 0.25,
            "panic_capitulation_repair_max_position_pct": 0.50,
            "range_weak_repair_position_pct": 0.50,
            "old_g3_route_position_pct": 0.50,
            "max_single_name_position_pct": 0.50,
            "g2_gap_supplement_position_pct": 0.50,
            "g2_gap_supplement_role": "fill unused G3 slots only; does not replace panic_repair or institutional_mainwave primary routes",
            "same_sector_policy": "allow same sector only when both selected candidates are institutional_mainwave; otherwise skip duplicated sector exposure",
        },
        "trade_strategy_policy": [
            {
                "trade_strategy": "range_weak_repair",
                "label_cn": "震荡弱势修复",
                "source_routes": ["old_g3_route_v3", "panic_repair"],
                "source_strategies": ["旧G3弱势/震荡修复", "震荡恐慌修复"],
                "entry_contract": "非主升、非下跌主杀环境中的弱势/震荡修复；旧G3来源提供 drawdown/range_pos/close_position/amount_ratio 结构过滤，panic_range 来源提供压力释放与 30m 修复确认。",
                "exit_contract": "-6% 结构止损、-10% 硬止损、+8% 先减半，剩余仓以前低跌破或 30m 转弱退出。",
                "position_contract": "基础首仓 20%，最大 25%；deep_wash_range 正常首仓，weak_rebound_structure 在指数高热或宽度退潮时降到 12.5% 或只观察。",
            },
            {
                "trade_strategy": "panic_capitulation_repair",
                "label_cn": "恐慌出清修复",
                "source_routes": ["old_g3_route_v3", "panic_repair"],
                "source_strategies": ["旧G3恐慌修复", "下跌恐慌修复"],
                "entry_contract": "日线先出现恐慌出清/压力释放，旧G3 down_panic 与 panic_repair standard_downtrend 均归入同一交易策略；有盘中数据时要求 30m 修复确认。",
                "exit_contract": "-5% 结构止损、-8% 硬止损、+8% 先减半，剩余仓以前低跌破或 30m 转弱退出。",
                "position_contract": "基础首仓 20%；standard_downtrend 压力态首仓降为 12.5%，仍属于同一策略的压力分层，不再拆成两个策略。",
            }
        ],
        "exit_contract": {
            "source": "g2_stop_structure_cooldown_migrated",
            "hard_stop": {
                "type": "m30_hard_stop",
                "loss_pct": 0.12,
                "action": "sell_all",
            },
            "take_profit": {
                "type": "m30_take_profit_partial",
                "profit_pct": 0.12,
                "sell_ratio": 0.50,
            },
            "structure_exit": {
                "type": "previous_day_low_break_after_take_profit",
                "confirm_bar": "30m",
                "action": "sell_remaining",
            },
            "cooldown": {
                "trigger": "hard_stop_30m_count>=2",
                "lookback_trading_days": 20,
                "cooldown_trading_days": 3,
                "scope": "institutional_mainwave_new_buys",
            },
            "risk_limits": {
                "max_single_trade_account_loss_pct": 0.065,
                "max_mtm_drawdown_pct": 0.18,
                "max_consecutive_realized_loss_pct": 0.20,
                "mtm_drawdown_reduce_risk_pct": 0.15,
                "mtm_drawdown_pause_new_buy_pct": 0.18,
            },
        },
        "route_priority": [
            {
                "route": "panic_repair",
                "label": "恐慌修复",
                "activation": "前一交易日出现恐慌扩散，且恐慌候选完成盘中确认",
                "source": "panic_current_builder",
                "router_eligible": "shadow_status == intraday_confirmed; rolling_route_health is observation only",
                "regime_gate": "past exited panic_repair trades within 240 calendar days: count>=3, avg_ret>0, big_loss_rate<=34%, worst_ret>=-18%",
            },
            {
                "route": "institutional_mainwave",
                "label": "机构主升浪",
                "activation": "非恐慌优先场景下，出现机构主升浪确认候选",
                "source": "institutional_mainwave_current_builder_v1",
                "router_eligible": "score>=120 && sector_diffusion>=65 && 30m close>=MA20; index_mom60 is observation at 5%-10% and only >10% blocks new open; rolling_240d_institutional_avg_ret is observation only",
                "regime_gate": "past exited institutional_mainwave trades within 240 calendar days: count>=2, avg_ret>0, big_loss_rate<=34%, worst_ret>=-25%",
                "failure_exit_audit_target": "historical upper-bound audit supports studying failed exit near -15%; not yet treated as executable stop without path replay",
            },
            {
                "route": "old_g3_route_v3",
                "label": "旧G3跨周期路由",
                "activation": "无机构主升确认候选时，回到旧G3非低吸链路",
                "source": "old_g3_four_path_current_builder_v1",
                "router_eligible": "g3_chain != range_box_bottom; rolling_route_health is observation only",
                "regime_gate": "past exited old_g3_route_v3 trades within 240 calendar days: count>=5, avg_ret>0, big_loss_rate<=34%, worst_ret>=-18%",
            },
            {
                "route": "g2_gap_supplement",
                "label": "G2补充买点",
                "activation": "G3主路由未占满2槽，且当天存在新鲜g2_v2_complete过滤买点",
                "source": "gen2_risk_cool_shadow_filtered_signals",
                "router_eligible": "fresh filtered_signals for entry_date and buy_allowed=true; rolling route_health is observation only",
                "regime_gate": "past exited g2 supplement trades within 240 calendar days: count>=3, avg_ret>0, big_loss_rate<=34%, worst_ret>=-12%",
                "role": "slot gap supplement, not primary route replacement",
            },
        ],
        "blocked_observation": [
            "机构主升日线和主线扩散已过但30m未确认，只进入阻断观察池",
            "range_box_bottom 低吸链路只观察，不作为当前常规状态路由",
            "数据服务不可用时不生成正式买点",
        ],
        "guardrails": {
            "auto_order_allowed": False,
            "formal_buy_signal": False,
            "order_path_enabled": False,
            "g2_runtime_touched": False,
            "shadow_trading_enabled": True,
            "real_order_integration": "disabled",
        },
    }


def _write_outputs(out_dir: Path, runtime_dir: Path, summary: dict[str, Any], all_rows: pd.DataFrame, selected: pd.DataFrame, context: pd.DataFrame) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    STATE_ALPHA_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    diagnostics = _route_diagnostics(all_rows)
    contract = _strategy_contract()
    tickets = _build_shadow_tickets(selected, summary)
    append_decision = summary.get("shadow_ledger_append") if isinstance(summary.get("shadow_ledger_append"), dict) else {}
    ledger = _append_shadow_ledger(STATE_ALPHA_RUNTIME_DIR, tickets, append_allowed=bool(append_decision.get("allowed", True)))
    all_rows.to_csv(out_dir / "g3_state_router_all_source_candidates.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(out_dir / "g3_state_router_selected_candidates.csv", index=False, encoding="utf-8-sig")
    tickets.to_csv(out_dir / "g3_state_alpha_shadow_tickets.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(out_dir / "g3_state_router_route_diagnostics.csv", index=False, encoding="utf-8-sig")
    (out_dir / "g3_state_router_strategy_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    if not context.empty:
        context.to_csv(out_dir / "g3_state_router_market_context.csv", index=False, encoding="utf-8-sig")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    selected.to_csv(runtime_dir / "latest_candidates.csv", index=False, encoding="utf-8-sig")
    all_rows.to_csv(runtime_dir / "latest_all_source_candidates.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(runtime_dir / "latest_route_diagnostics.csv", index=False, encoding="utf-8-sig")
    (runtime_dir / "latest_strategy_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    (runtime_dir / "latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    if bool(append_decision.get("allowed", True)):
        tickets.to_csv(STATE_ALPHA_RUNTIME_DIR / "latest_shadow_tickets.csv", index=False, encoding="utf-8-sig")
        (STATE_ALPHA_RUNTIME_DIR / "latest_strategy_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        (STATE_ALPHA_RUNTIME_DIR / "latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    else:
        afterhours_tickets_path = STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_shadow_tickets.csv"
        has_existing_afterhours = False
        if afterhours_tickets_path.exists():
            try:
                has_existing_afterhours = not pd.read_csv(afterhours_tickets_path, low_memory=False, encoding="utf-8-sig").empty
            except Exception:
                has_existing_afterhours = False
        if not tickets.empty or not has_existing_afterhours:
            tickets.to_csv(afterhours_tickets_path, index=False, encoding="utf-8-sig")
            (STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_strategy_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
            (STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    preview_cols = [
        "entry_date",
        "code",
        "name",
        "route",
        "route_label",
        "trade_strategy_label",
        "source_strategy_label",
        "source_quality",
        "score",
        "confirm_datetime",
        "entry_price",
        "block_reason",
        "router_eligible",
    ]
    ticket_cols = [
        "entry_date",
        "code",
        "name",
        "route",
        "trade_strategy_label",
        "source_strategy_label",
        "qualified_shadow_buy",
        "planned_entry_ts",
        "reference_close",
        "position_pct",
        "structure_stop",
        "hard_stop",
        "take_profit_1",
        "shadow_status",
    ]
    selected_preview = selected[[c for c in preview_cols if c in selected.columns]].to_markdown(index=False) if not selected.empty else "_无候选_"
    all_preview = all_rows[[c for c in preview_cols if c in all_rows.columns]].head(30).to_markdown(index=False) if not all_rows.empty else "_无候选_"
    ticket_preview = tickets[[c for c in ticket_cols if c in tickets.columns]].to_markdown(index=False) if not tickets.empty else "_无影子票据_"
    diagnostics_preview = diagnostics.to_markdown(index=False) if not diagnostics.empty else "_无诊断_"
    report = [
        "# G3 状态路由当日 Shadow v1",
        "",
        "## 摘要",
        "",
        f"- entry_date：`{summary.get('entry_date', '')}`",
        f"- decision_date：`{summary.get('decision_date', '')}`",
        f"- 诊断：`{summary.get('diagnosis_code', '')}`",
        f"- 选择路由：`{summary.get('selected_route', '')}`",
        f"- 自动下单：`{summary.get('auto_order_allowed_rows', 0)}`",
        "",
        "## 当前选中候选",
        "",
        selected_preview,
        "",
        "## G3 State Alpha 影子交易票据",
        "",
        ticket_preview,
        "",
        "## 全部来源候选",
        "",
        all_preview,
        "",
        "## 路由诊断",
        "",
        diagnostics_preview,
        "",
        "## 纪律",
        "",
        "- 本脚本只做 shadow 观察，不开启正式买点和自动下单。",
        "- G3 State Alpha 影子票据会写入 runtime 台账，用于审计触发、仓位、止损、退出合同。",
        "- 机构主升浪已经切换为当前候选复算源，但仍需 shadow 观察确认稳定性。",
        "- 旧G3跨周期路由已经切换为当前四链路复算源；`range_box_bottom` 仅入阻断观察池，不作为常规路由。",
        "- 正式升级前仍需完成连续 shadow 观察与成交可得性校验。",
        "",
    ]
    (out_dir / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build current G3 state-router shadow candidates.")
    parser.add_argument("--entry-date", default="")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--runtime-dir", default=str(RUNTIME_DIR))
    parser.add_argument("--period", type=int, default=30, choices=[15, 30])
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--min-amount20", type=float, default=30000.0)
    parser.add_argument("--max-codes", type=int, default=0)
    args = parser.parse_args()

    entry_date, decision_date = _resolve_entry_date(args.entry_date.strip())
    if not entry_date:
        raise RuntimeError("Cannot resolve entry date.")

    context, context_meta = _current_market_context(decision_date, float(args.min_amount20), int(args.max_codes or 0))
    source_frames: list[pd.DataFrame] = []
    source_meta: list[dict[str, Any]] = []
    builders = [
        ("panic_current_builder", lambda: _panic_source(entry_date, int(args.top_n), float(args.min_amount20), int(args.max_codes or 0), int(args.period))),
        ("institutional_mainwave_current_builder_v1", lambda: _institutional_current_source(entry_date, decision_date, int(args.top_n), float(args.min_amount20), int(args.period))),
        ("old_g3_current_builder_v1", lambda: _old_g3_current_source(entry_date, decision_date, int(args.top_n), float(args.min_amount20), int(args.max_codes or 0))),
        ("g2_gap_supplement_current_builder_v1", lambda: _g2_gap_supplement_source(entry_date, int(args.top_n))),
    ]
    for name, builder in builders:
        try:
            rows, meta = builder()
        except Exception as exc:
            rows, meta = pd.DataFrame(), {"source": name, "rows": 0, "status": "failed", "error": str(exc)}
        source_meta.append(meta)
        if not rows.empty:
            source_frames.append(rows)

    all_rows = pd.concat(source_frames, ignore_index=True, sort=False) if source_frames else pd.DataFrame()
    all_rows = _standardize_output(all_rows, entry_date, decision_date)
    all_rows = _attach_trade_strategy_fields(all_rows)
    selected, route_meta = _select_router_candidates(all_rows, context)
    selected = _standardize_output(selected, entry_date, decision_date)
    selected = _attach_trade_strategy_fields(selected)
    account_risk = _account_risk_state(entry_date)
    selected = _apply_account_risk(selected, account_risk)
    route_diagnostics = _route_diagnostics(all_rows)
    diagnosis_code = "HAS_STATE_ROUTER_SHADOW_CANDIDATE" if not selected.empty else "NO_STATE_ROUTER_CANDIDATE"
    if not context_meta.get("ok"):
        diagnosis_code = "MARKET_CONTEXT_UNAVAILABLE"
    if account_risk.get("account_risk_action") == "pause_new_buy":
        diagnosis_code = "ACCOUNT_RISK_PAUSE_NEW_BUY"
    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_date": entry_date,
        "decision_date": decision_date,
        "diagnosis_code": diagnosis_code,
        "selected_route": route_meta.get("selected_route", ""),
        "route_reason": route_meta.get("reason", ""),
        "daily_open_limit": int(route_meta.get("daily_open_limit") or 2),
        "same_sector_policy": route_meta.get("same_sector_policy", ""),
        "skipped_same_sector": int(route_meta.get("skipped_same_sector") or 0),
        "skipped_duplicate_code": int(route_meta.get("skipped_duplicate_code") or 0),
        "g2_gap_supplement_rows": int(route_meta.get("g2_gap_supplement_rows") or 0),
        "account_risk": account_risk,
        "source_rows": int(len(all_rows)),
        "selected_rows": int(len(selected)),
        "shadow_ticket_rows": int(len(selected)),
        "qualified_shadow_buy_rows": int(len(selected[selected.get("router_eligible", pd.Series(False, index=selected.index)).fillna(False).astype(bool)])) if not selected.empty else 0,
        "auto_order_allowed_rows": 0,
        "formal_buy_signal_rows": 0,
        "order_path_enabled_rows": 0,
        "market_context": context_meta,
        "sources": source_meta,
        "shadow_ledger_append": _shadow_ledger_append_decision(),
        "route_diagnostics": route_diagnostics.to_dict("records") if not route_diagnostics.empty else [],
        "eligible_source_rows": int(route_diagnostics["eligible_rows"].sum()) if not route_diagnostics.empty else 0,
        "blocked_source_rows": int(route_diagnostics["blocked_rows"].sum()) if not route_diagnostics.empty else 0,
        "g2_runtime_touched": False,
        "live_order_enabled": False,
        "requires_current_institutional_generator": False,
        "requires_current_old_g3_generator": False,
        "g2_gap_supplement_enabled": True,
        "final_profile": FINAL_G3_PROFILE,
        "final_profile_name": FINAL_G3_PROFILE_NAME,
        "legacy_base_profile": FINAL_G3_LEGACY_BASE_PROFILE,
    }
    _write_outputs(Path(args.out_dir), Path(args.runtime_dir), summary, all_rows, selected, context)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
