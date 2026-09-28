"""Scheduled, paper-only T-plan monitor for QMT holdings.

It reads the persisted broker snapshot instead of querying the account in the
intraday loop.  Therefore it respects the account-sync boundary and treats an
invalid snapshot as a hard block.  The monitor never imports an order gateway.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from execution.g3_holding_t_engine import evaluate_portfolio_t, paper_t_cycle_excess
from services.g3_holding_t_daily_review import CONFIRMED_T_INVENTORY_PATH, load_confirmed_t_inventory
from services.g3_holding_t_portfolio_state import STATE_PATH as PORTFOLIO_STATE_PATH, load_state as load_holding_t_portfolio_state
from utils.config import config as app_config
from utils.logger import get_logger
from utils.paths import runtime_path


BROKER_STATE_PATH = runtime_path("gen3_state_alpha", "broker_state.json")
LEDGER_PATH = runtime_path("g3_holding_t_paper", "plans.jsonl")
NOTIFICATION_STATE_PATH = runtime_path("g3_holding_t_paper", "notification_state.json")
WATCHLIST_PATH = runtime_path("gen3_state_alpha", "holding_tick_watchlist.json")
SHADOW_ATTRIBUTION_STATE_PATH = runtime_path("g3_holding_t_paper", "shadow_attribution_state.json")
TICK_SNAPSHOT_DIR = runtime_path("g3_holding_t_paper", "tick_snapshots")
MINUTE_REPAIR_STATE_PATH = runtime_path("g3_holding_t_paper", "minute_repair_state.json")
MINUTE_REPAIR_REPORT_PATH = runtime_path("g3_holding_t_paper", "minute_repair_report.json")
MINUTE_REPAIR_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qmt_fullpush_intraday_aggregator.py"
# A complete T cycle consumes two event notices.  Six keeps the user's
# 2-3-cycle daily boundary while ensuring two confirmed sell legs can both
# receive their corresponding conditional buyback reminders.
MAX_DAILY_ACTION_NOTICES = 6
MINUTE_STALE_NOTICE_SCAN_THRESHOLD = 5
logger = get_logger("g3_holding_t_paper_monitor")


@dataclass(frozen=True)
class _TNotificationSpec:
    """Single source of truth for the action direction shown in an email."""

    direction: str
    direction_label: str
    body_title: str
    subject_label: str
    price_range_type: str


_SELL_T_SPEC = _TNotificationSpec(
    direction="sell",
    direction_label="卖出 T 仓",
    body_title="卖出 T 仓提醒",
    subject_label="卖出T仓",
    price_range_type="sell",
)
_BUYBACK_T_SPEC = _TNotificationSpec(
    direction="buyback",
    direction_label="买回 T 仓",
    body_title="买回 T 仓提醒",
    subject_label="买回T仓",
    price_range_type="buyback",
)
_INITIAL_BUY_SPEC = _TNotificationSpec(
    direction="initial_buy",
    direction_label="买入建仓",
    body_title="买入建仓提醒",
    subject_label="买入建仓",
    price_range_type="initial_buy",
)
_ADD_TO_TARGET_SPEC = _TNotificationSpec(
    direction="add_to_target",
    direction_label="买入补仓",
    body_title="买入补仓提醒",
    subject_label="买入补仓",
    price_range_type="initial_buy",
)

_T_NOTIFICATION_SPECS: dict[str, _TNotificationSpec] = {
    "paper_sell_t_leg": _SELL_T_SPEC,
    "watch_sell_t_signal": _SELL_T_SPEC,
    "paper_buyback_t_leg": _BUYBACK_T_SPEC,
    "watch_buyback_t_signal": _BUYBACK_T_SPEC,
    "paper_initial_buy": _INITIAL_BUY_SPEC,
    "watch_initial_buy_signal": _INITIAL_BUY_SPEC,
    "paper_add_to_target": _ADD_TO_TARGET_SPEC,
    "watch_add_to_target_signal": _ADD_TO_TARGET_SPEC,
}

# These are normal engine outcomes, not notification actions.  Any other
# action, including an empty/missing action, is an integration error and must
# never fall through to a buyback email.
_NON_NOTIFICATION_ACTIONS = {"observe", "blocked", "risk_exit_no_t"}


def _notification_spec(action: Any) -> _TNotificationSpec:
    action_text = str(action or "").strip()
    try:
        return _T_NOTIFICATION_SPECS[action_text]
    except KeyError as exc:
        raise ValueError(f"Unsupported T notification action: {action_text or '<empty>'}") from exc


def _resolved_notification_spec(
    plan: dict[str, Any],
    action_spec: _TNotificationSpec | None = None,
) -> _TNotificationSpec:
    actual = _notification_spec(plan.get("action"))
    if action_spec is not None and action_spec != actual:
        raise ValueError("notification action changed while composing the email")
    return actual


class MarketClient(Protocol):
    def get_full_tick(self, stock_codes: list[str]) -> dict[str, Any]: ...
    def get_market_data(self, **kwargs: Any) -> dict[str, pd.DataFrame]: ...


def _code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        return text
    if len(text) == 6 and text.isdigit():
        return f"{text}.SH" if text.startswith(("5", "6", "9")) else f"{text}.SZ"
    return text


def _stock_names(codes: list[str]) -> dict[str, str]:
    """Best-effort batch lookup for notification display names.

    Stock metadata must never block a T notification.  The canonical exchange
    code remains the fallback when ClickHouse is unavailable or incomplete.
    """
    normalized_codes = sorted({_code(code) for code in codes if _code(code)})
    if not normalized_codes:
        return {}
    code6 = [code.split(".", 1)[0] for code in normalized_codes]
    placeholders = ", ".join("?" for _ in normalized_codes)
    code6_placeholders = ", ".join("?" for _ in code6)
    try:
        from utils.market_warehouse import clickhouse_query_df

        stocks = clickhouse_query_df(
            f"""
            SELECT code, name
            FROM stocks
            WHERE code IN ({placeholders})
               OR substring(code, 1, 6) IN ({code6_placeholders})
            """,
            [*normalized_codes, *code6],
        )
    except Exception as exc:
        logger.warning("holding-T stock name lookup failed; using codes in notifications: %s", exc)
        return {}
    if stocks is None or stocks.empty:
        return {}

    names: dict[str, str] = {}
    for row in stocks.to_dict("records"):
        code = _code(row.get("code"))
        name = str(row.get("name") or "").strip()
        if code and name:
            names[code] = name
    return names


def _display_security(plan: dict[str, Any]) -> str:
    """Render a stock name with its six-digit code, preserving a code fallback."""
    code = str(plan.get("code") or "").strip()
    name = str(plan.get("stock_name") or "").strip()
    if not name:
        return code
    code6 = code.split(".", 1)[0]
    return f"{name}（{code6}）" if code6 else name


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _is_trading_day(now: datetime) -> bool:
    """Use the exchange calendar before doing any intraday work."""
    try:
        from scheduler.trading_calendar import TradingCalendar

        return bool(TradingCalendar.is_trading_day(now))
    except Exception:
        # A calendar outage must not turn a weekend into a trading day.
        return now.weekday() < 5


def _rsi6(closes: list[float]) -> float | None:
    values = pd.Series(closes, dtype="float64").dropna()
    if len(values) < 7:
        return None
    delta = values.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / 6, adjust=False, min_periods=6).mean().iloc[-1]
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / 6, adjust=False, min_periods=6).mean().iloc[-1]
    if pd.isna(gains) or pd.isna(losses):
        return None
    return 100.0 if losses == 0 and gains > 0 else (50.0 if losses == 0 else float(100 - 100 / (1 + gains / losses)))


def _series_for_code(frame: Any, code: str) -> list[float]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    candidates = [code, code.upper(), code.lower()]
    values: Any = None
    for key in candidates:
        if key in frame.index:
            values = frame.loc[key]
            break
        if key in frame.columns:
            values = frame[key]
            break
    if values is None:
        return []
    raw = values.tolist() if isinstance(values, pd.Series) else list(values)
    return [float(value) for value in raw if _number(value) is not None]


def _warehouse_minute_metrics(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Read the QMT-fed intraday 5m table when xtdata's bar cache is stale."""
    if not codes:
        return {}
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        if not clickhouse_table_exists("kline_minute_5"):
            return {}
        quoted = ",".join("'" + str(code).replace("'", "''") + "'" for code in codes)
        frame = clickhouse_query_df(
            f"""
            SELECT code, datetime, close, volume, amount
            FROM kline_minute_5 FINAL
            WHERE code IN ({quoted})
            ORDER BY code, datetime DESC
            LIMIT 30 BY code
            """
        )
    except Exception as exc:
        logger.warning("holding-T ClickHouse 5m fallback failed: %s", exc)
        return {}
    if frame is None or frame.empty:
        return {}
    result: dict[str, dict[str, Any]] = {}
    now = datetime.now()
    for code, rows in frame.groupby(frame["code"].astype(str).str.upper(), dropna=False):
        rows = rows.sort_values("datetime").copy()
        for field in ("close", "volume", "amount"):
            rows[field] = pd.to_numeric(rows[field], errors="coerce")
        rows["bar_vwap"] = rows["amount"] / rows["volume"].replace(0, pd.NA)
        # Some historical QMT rows store volume in lots while amount remains
        # in shares-yuan.  That creates an almost exact 100x VWAP outlier.
        # Normalize only the narrow, evidence-backed 95x--105x band; broader
        # outliers remain rejected below instead of being guessed at.
        unit_mismatch = (
            rows["bar_vwap"].div(rows["close"].replace(0, pd.NA)).gt(95)
            & rows["bar_vwap"].div(rows["close"].replace(0, pd.NA)).lt(105)
        )
        rows.loc[unit_mismatch, "volume"] = rows.loc[unit_mismatch, "volume"] * 100.0
        rows["bar_vwap"] = rows["amount"] / rows["volume"].replace(0, pd.NA)
        # A malformed minute row can carry a normal close but a volume in lots
        # against an amount in shares, creating a 100x VWAP outlier.  Validate
        # every bar against its own close before aggregating the rolling VWAP.
        rows = rows[
            rows["close"].gt(0)
            & rows["bar_vwap"].ge(rows["close"] * 0.5)
            & rows["bar_vwap"].le(rows["close"] * 1.5)
        ]
        if rows.empty:
            continue
        closes = rows["close"].dropna().tolist()
        volumes = rows["volume"].dropna().tolist()
        amounts = rows["amount"].dropna().tolist()
        latest = pd.to_datetime(rows["datetime"].iloc[-1], errors="coerce")
        if pd.isna(latest) or not closes or not volumes or not amounts:
            continue
        latest_naive = latest.to_pydatetime().replace(tzinfo=None)
        age = (now - latest_naive).total_seconds()
        # The current intraday writer persists an +08:00 timestamp that is
        # eight hours ahead of the host clock. Correct only that narrow,
        # observed shape; a genuinely future/stale value remains unsafe.
        if -9 * 3600 <= age <= -7 * 3600:
            age += 8 * 3600
        vwap = sum(amounts) / sum(volumes) if sum(volumes) > 0 else None
        result[str(code)] = {
            "vwap_5m": vwap,
            "rsi6": _rsi6(closes),
            "minute_age_seconds": max(0.0, age),
            "minute_fresh": 0.0 <= age <= 8 * 60,
            "minute_source": "qmt_clickhouse_intraday_5m",
        }
    return result


def _minute_metrics(client: MarketClient, codes: list[str], *, prefer_warehouse: bool = True) -> dict[str, dict[str, Any]]:
    raw = client.get_market_data(
        field_list=["time", "close", "volume", "amount"], stock_list=codes, period="5m", count=30, fill_data=False,
    ) or {}
    output: dict[str, dict[str, Any]] = {}
    for code in codes:
        closes = _series_for_code(raw.get("close"), code)
        volumes = _series_for_code(raw.get("volume"), code)
        amounts = _series_for_code(raw.get("amount"), code)
        timestamps = _series_for_code(raw.get("time"), code)
        vwap = sum(amounts) / sum(volumes) if amounts and volumes and sum(volumes) > 0 else None
        # QMT minute ``volume`` may be expressed in lots while ``amount`` is
        # expressed in yuan. Detect that 100x mismatch from the contemporaneous
        # close instead of applying it to clients that already return shares.
        if vwap is not None and closes and vwap > max(closes) * 10:
            vwap /= 100.0
        latest_timestamp = timestamps[-1] if timestamps else None
        minute_age = None
        if latest_timestamp is not None and latest_timestamp > 1_000_000_000_000:
            minute_age = max(0.0, datetime.now().timestamp() - latest_timestamp / 1000)
        output[code] = {
            "vwap_5m": vwap,
            "rsi6": _rsi6(closes),
            "minute_age_seconds": minute_age,
            "minute_fresh": minute_age is not None and minute_age <= 8 * 60,
            "minute_source": "qmt_xtdata_cache",
        }
    if prefer_warehouse:
        for code, metric in _warehouse_minute_metrics(codes).items():
            if metric.get("minute_fresh"):
                output[code] = metric
    return output


def _market_window(now: datetime) -> bool:
    clock = now.strftime("%H:%M")
    return _is_trading_day(now) and (
        "09:25" <= clock <= "11:35"
        or "13:00" <= clock <= "15:10"
    )


def _minute_blocked_codes(codes: list[str], minute: dict[str, dict[str, Any]]) -> list[str]:
    blocked: list[str] = []
    for code in codes:
        metric = minute.get(code) or {}
        if (
            not bool(metric.get("minute_fresh"))
            or _number(metric.get("vwap_5m")) is None
            or _number(metric.get("rsi6")) is None
        ):
            blocked.append(code)
    return sorted(blocked)


def _request_intraday_minute_repair(
    codes: list[str],
    minute: dict[str, dict[str, Any]],
    *,
    now: datetime,
    state_path: Path = MINUTE_REPAIR_STATE_PATH,
    report_path: Path = MINUTE_REPAIR_REPORT_PATH,
    popen: Any = subprocess.Popen,
) -> dict[str, Any]:
    """Start a bounded, symbol-scoped 5m repair before surfacing a block email."""
    blocked_codes = _minute_blocked_codes(codes, minute)
    if not blocked_codes:
        state = _load_json(state_path, {})
        if (
            state.get("trade_date") == now.date().isoformat()
            and state.get("status") == "started"
        ):
            state["status"] = "completed"
            state["completed_at"] = now.isoformat(sep=" ", timespec="seconds")
            _save_json(state_path, state)
        return {"status": "not_needed", "blocked_codes": blocked_codes, "notify_allowed": True}
    if not _market_window(now):
        return {"status": "not_needed", "blocked_codes": blocked_codes, "notify_allowed": True}

    state = _load_json(state_path, {})
    trade_date = now.date().isoformat()
    previous_codes = state.get("blocked_codes") if isinstance(state.get("blocked_codes"), list) else []
    attempt_count = int(state.get("attempt_count") or 0) if state.get("trade_date") == trade_date and previous_codes == blocked_codes else 0
    last_started_at = str(state.get("last_started_at") or "")
    last_started = None
    if last_started_at:
        try:
            last_started = datetime.fromisoformat(last_started_at)
        except ValueError:
            last_started = None
    elapsed = (now - last_started).total_seconds() if last_started else None
    if attempt_count >= 3 and elapsed is not None and elapsed >= 120:
        return {
            "status": "exhausted",
            "blocked_codes": blocked_codes,
            "last_started_at": last_started_at,
            "attempt_count": attempt_count,
            "notify_allowed": True,
        }
    if state.get("trade_date") == trade_date and previous_codes == blocked_codes and elapsed is not None and elapsed < 120:
        return {
            "status": "cooldown",
            "blocked_codes": blocked_codes,
            "last_started_at": last_started_at,
            "attempt_count": attempt_count,
            "notify_allowed": False,
        }

    if not MINUTE_REPAIR_SCRIPT.exists():
        state = {
            "trade_date": trade_date,
            "blocked_codes": blocked_codes,
            "last_started_at": now.isoformat(sep=" ", timespec="seconds"),
            "status": "unavailable",
            "attempt_count": attempt_count + 1,
        }
        _save_json(state_path, state)
        return {
            "status": "unavailable",
            "blocked_codes": blocked_codes,
            "attempt_count": state["attempt_count"],
            "notify_allowed": True,
        }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = "15:10"
    args = [
        sys.executable,
        str(MINUTE_REPAIR_SCRIPT),
        "--markets", "SH,SZ,BJ",
        "--universe", "stock",
        "--codes", ",".join(blocked_codes),
        "--periods", "5m",
        "--duration-sec", "180",
        "--flush-interval-sec", "30",
        "--poll-full-tick",
        "--write-daily",
        "--connect-retry-sec", "10",
        "--connect-deadline", deadline,
        "--report", str(report_path),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = popen(
            args,
            cwd=str(MINUTE_REPAIR_SCRIPT.parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        logger.warning("holding-T minute repair start failed: %s", exc)
        state = {
            "trade_date": trade_date,
            "blocked_codes": blocked_codes,
            "last_started_at": now.isoformat(sep=" ", timespec="seconds"),
            "status": "start_failed",
            "attempt_count": attempt_count + 1,
            "error": f"{type(exc).__name__}: {exc}",
        }
        _save_json(state_path, state)
        return {
            "status": "start_failed",
            "blocked_codes": blocked_codes,
            "attempt_count": state["attempt_count"],
            "error": f"{type(exc).__name__}: {exc}",
            "notify_allowed": True,
        }
    state = {
        "trade_date": trade_date,
        "blocked_codes": blocked_codes,
        "last_started_at": now.isoformat(sep=" ", timespec="seconds"),
        "pid": getattr(process, "pid", None),
        "report_path": str(report_path),
        "status": "started",
        "attempt_count": attempt_count + 1,
    }
    _save_json(state_path, state)
    return {
        "status": "started",
        "blocked_codes": blocked_codes,
        "last_started_at": state["last_started_at"],
        "pid": state["pid"],
        "attempt_count": state["attempt_count"],
        "notify_allowed": False,
    }


def _tick_plan(raw: dict[str, Any]) -> dict[str, Any]:
    bid = sum(_number(value) or 0.0 for value in (raw.get("bidVol") or [])[:5])
    ask = sum(_number(value) or 0.0 for value in (raw.get("askVol") or [])[:5])
    last = _number(raw.get("lastPrice"))
    upper = _number(raw.get("upperLimit"))
    lower = _number(raw.get("lowerLimit"))
    timestamp = _number(raw.get("time"))
    age = None
    if timestamp is not None and timestamp > 1_000_000_000_000:
        age = max(0.0, datetime.now().timestamp() - timestamp / 1000)
    return {
        "last_price": last,
        "tick_age_seconds": age,
        "tick_fresh": age is not None and age <= 20,
        "order_book_imbalance": (bid - ask) / (bid + ask) if bid + ask else None,
        "limit_up": bool(last and upper and last >= upper),
        "limit_down": bool(last and lower and last <= lower),
    }


def _append(row: dict[str, Any], ledger_path: Path) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _json_value(value: Any) -> Any:
    """Keep only JSON-safe values from a QMT tick response."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value[:5]]
    return str(value)


def _append_tick_snapshots(
    raw_ticks: dict[str, Any],
    ticks: dict[str, dict[str, Any]],
    minute: dict[str, dict[str, Any]],
    plans: list[dict[str, Any]],
    *,
    checked_at: datetime,
    snapshot_dir: Path,
) -> None:
    """Persist one audit-grade, five-level snapshot per tracked stock per scan.

    The monitor remains one-minute based; this is deliberately not a claim of
    full tick-by-tick capture.  It preserves the complete decision context and
    the five levels returned by QMT at that scan time for after-close review.
    """
    by_code = {str(plan.get("code") or ""): plan for plan in plans}
    target = snapshot_dir / f"{checked_at.date().isoformat()}.jsonl"
    for code, tick in ticks.items():
        raw = raw_ticks.get(code) if isinstance(raw_ticks.get(code), dict) else {}
        record = {
            "schema_version": 1,
            "checked_at": checked_at.isoformat(sep=" ", timespec="seconds"),
            "code": code,
            "tick": {
                **tick,
                "exchange_time": _json_value(raw.get("time")),
                "open": _json_value(raw.get("open")),
                "high": _json_value(raw.get("high")),
                "low": _json_value(raw.get("low")),
                "last_close": _json_value(raw.get("lastClose")),
                "volume": _json_value(raw.get("volume")),
                "amount": _json_value(raw.get("amount")),
                "bid_price": _json_value(raw.get("bidPrice")),
                "ask_price": _json_value(raw.get("askPrice")),
                "bid_volume": _json_value(raw.get("bidVol")),
                "ask_volume": _json_value(raw.get("askVol")),
                "upper_limit": _json_value(raw.get("upperLimit")),
                "lower_limit": _json_value(raw.get("lowerLimit")),
            },
            "minute": minute.get(code) or {},
            "decision": by_code.get(code) or {},
        }
        _append(record, target)


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _update_shadow_attribution(
    plans: list[dict[str, Any]],
    ticks: dict[str, dict[str, Any]],
    *,
    now: datetime,
    state_path: Path,
) -> list[dict[str, Any]]:
    """Track paper sell signals without changing alerts or any order path."""
    state = _load_json(state_path, {"open_cycles": {}, "completed_cycles": []})
    open_cycles = state.get("open_cycles") if isinstance(state.get("open_cycles"), dict) else {}
    completed = state.get("completed_cycles") if isinstance(state.get("completed_cycles"), list) else []
    trade_date = now.date().isoformat()
    touched: list[dict[str, Any]] = []
    for key, cycle in list(open_cycles.items()):
        if not isinstance(cycle, dict):
            open_cycles.pop(key, None)
            continue
        if cycle.get("trade_date") != trade_date:
            completed.append({**cycle, "status": "expired_without_eod_mark", "ended_at": now.isoformat(sep=" ", timespec="seconds")})
            open_cycles.pop(key, None)
            continue
        price = _number((ticks.get(str(cycle.get("code") or "")) or {}).get("last_price"))
        if price is None or not bool((ticks.get(str(cycle.get("code") or "")) or {}).get("tick_fresh")):
            continue
        base_price = _number(cycle.get("entry_price"))
        if base_price is None or base_price <= 0:
            continue
        cycle["last_price"] = round(price, 3)
        cycle["last_relative_pct"] = round((price / base_price - 1.0) * 100.0, 3)
        cycle["min_relative_pct"] = min(float(cycle.get("min_relative_pct") or cycle["last_relative_pct"]), cycle["last_relative_pct"])
        cycle["max_relative_pct"] = max(float(cycle.get("max_relative_pct") or cycle["last_relative_pct"]), cycle["last_relative_pct"])
        elapsed = max(0.0, (now - datetime.fromisoformat(str(cycle.get("opened_at")))).total_seconds())
        checkpoints = cycle.get("checkpoints") if isinstance(cycle.get("checkpoints"), dict) else {}
        for minutes in (15, 30, 60):
            label = f"m{minutes}"
            if elapsed >= minutes * 60 and label not in checkpoints:
                checkpoints[label] = {"at": now.isoformat(sep=" ", timespec="seconds"), "relative_pct": cycle["last_relative_pct"]}
        cycle["checkpoints"] = checkpoints
        if now.strftime("%H:%M") >= "14:56":
            completed.append({**cycle, "status": "eod_mark", "ended_at": now.isoformat(sep=" ", timespec="seconds")})
            open_cycles.pop(key, None)
        else:
            open_cycles[key] = cycle
        touched.append({"code": cycle.get("code"), "status": cycle.get("status", "open"), "last_relative_pct": cycle["last_relative_pct"]})
    for plan in plans:
        action = str(plan.get("action") or "")
        if action not in _T_NOTIFICATION_SPECS or _notification_spec(action).direction != "sell":
            continue
        code = str(plan.get("code") or "")
        key = f"{trade_date}:{code}"
        entry_price = _number(plan.get("market_price"))
        if not code or entry_price is None or key in open_cycles:
            continue
        open_cycles[key] = {
            "code": code,
            "trade_date": trade_date,
            "opened_at": now.isoformat(sep=" ", timespec="seconds"),
            "entry_price": round(entry_price, 3),
            "signal_price_vs_vwap_pct": plan.get("price_vs_vwap_pct"),
            "signal_rsi6": plan.get("rsi6"),
            "signal_order_book_imbalance": plan.get("order_book_imbalance"),
            "status": "open",
            "checkpoints": {},
        }
        touched.append({"code": code, "status": "opened", "last_relative_pct": 0.0})
    state["open_cycles"] = open_cycles
    state["completed_cycles"] = completed[-120:]
    _save_json(state_path, state)
    return touched


def _watchlist_codes(path: Path) -> set[str] | None:
    payload = _load_json(path, {})
    raw_codes = payload.get("codes")
    if not isinstance(raw_codes, list):
        return None
    codes = {_code(value) for value in raw_codes if _code(value)}
    return codes or None


def _in_signal_window(now: datetime) -> bool:
    """Only surface freshness incidents while a T action could be generated.

    The first complete 5-minute bar after the 13:00 re-open is not reliably
    available until shortly after 13:05.  Treating the lunch-close bar as an
    outage at 13:00--13:05 produces a false exception email.
    """
    if not _is_trading_day(now):
        return False
    clock = now.strftime("%H:%M")
    return "09:30" <= clock <= "11:30" or "13:06" <= clock <= "14:56"


def _smtp_config() -> dict[str, Any]:
    email_cfg = app_config.get("email", default=None, config_file="settings.yaml") or {}
    if email_cfg.get("enabled", True) is False:
        raise RuntimeError("email.enabled is disabled")
    recipients = email_cfg.get("to_emails") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [str(value).strip() for value in recipients if str(value).strip()]
    cfg = {
        "server": str(email_cfg.get("smtp_server") or "").strip(),
        "port": int(email_cfg.get("smtp_port") or 0),
        "username": str(email_cfg.get("smtp_user") or "").strip(),
        "password": str(email_cfg.get("smtp_password") or "").strip(),
        "from_email": str(email_cfg.get("from_email") or "").strip(),
        "to_emails": recipients,
        "use_ssl": bool(email_cfg.get("use_ssl")) or int(email_cfg.get("smtp_port") or 0) == 465,
    }
    if not all([cfg["server"], cfg["port"], cfg["username"], cfg["password"], cfg["from_email"], cfg["to_emails"]]):
        raise RuntimeError("SMTP config is incomplete")
    return cfg


def _send_default_email(subject: str, body: str) -> dict[str, Any]:
    cfg = _smtp_config()
    message = MIMEMultipart()
    message["From"] = cfg["from_email"]
    message["To"] = ",".join(cfg["to_emails"])
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))
    if cfg["use_ssl"]:
        server = smtplib.SMTP_SSL(cfg["server"], cfg["port"], timeout=30)
    else:
        server = smtplib.SMTP(cfg["server"], cfg["port"], timeout=30)
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
    try:
        server.login(cfg["username"], cfg["password"])
        server.sendmail(cfg["from_email"], cfg["to_emails"], message.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            server.close()
    return {"sent": True, "recipient_count": len(cfg["to_emails"])}


def _suggested_price_range(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Return a narrow manual execution range around the signal price.

    This is an email aid only; the paper monitor never submits a limit order.
    Sell ranges keep the signal price as the lower bound and allow +0.15%;
    initial-buy ranges allow -0.15% to the signal price.  A buyback range is
    capped below the calculated break-even price.
    """
    spec = _notification_spec(plan.get("action"))
    reference = _number(plan.get("market_price"))
    if reference is None or reference <= 0:
        return None
    band = max(0.02, reference * 0.0015)
    if spec.price_range_type == "sell":
        return {
            "price_range_type": spec.price_range_type,
            "price_range_reference": round(reference, 2),
            "price_range_low": round(reference, 2),
            "price_range_high": round(reference + band, 2),
        }
    if spec.price_range_type == "initial_buy":
        return {
            "price_range_type": spec.price_range_type,
            "price_range_reference": round(reference, 2),
            "price_range_low": round(max(0.01, reference - band), 2),
            "price_range_high": round(reference, 2),
        }
    if spec.price_range_type == "buyback":
        ceiling = _number(plan.get("break_even_buyback_price"))
        upper = reference
        if ceiling is not None and ceiling > 0:
            upper = min(upper, ceiling - 0.01)
        upper = max(0.01, upper)
        return {
            "price_range_type": spec.price_range_type,
            "price_range_reference": round(reference, 2),
            "price_range_low": round(max(0.01, upper - band), 2),
            "price_range_high": round(upper, 2),
            "price_range_hard_ceiling": round(ceiling, 2) if ceiling and ceiling > 0 else None,
        }
    raise ValueError(f"Unsupported T price range type: {spec.price_range_type}")


def _notification_subject(plan: dict[str, Any], *, action_spec: _TNotificationSpec | None = None) -> str:
    spec = _resolved_notification_spec(plan, action_spec)
    return f"[AiStock 做T] {_display_security(plan)} {spec.subject_label}提醒"


def _notification_body(
    plan: dict[str, Any],
    *,
    conditional_buyback: bool = False,
    action_spec: _TNotificationSpec | None = None,
) -> str:
    spec = _resolved_notification_spec(plan, action_spec)
    lines = [
        spec.body_title,
        f"操作方向：{spec.direction_label}",
        f"标的：{_display_security(plan)}",
        ("建议数量：请按你实际持仓和可卖老仓自行确定（本系统未连接该账户）。"
         if plan.get("signal_quantity_required") else f"建议数量：{int(plan.get('proposed_shares') or 0)} 股"),
        f"检查时间：{plan.get('checked_at')}",
        f"价格相对 5 分钟 VWAP：{plan.get('price_vs_vwap_pct', '--')}%",
        f"RSI6：{plan.get('rsi6', '--')}",
        f"五档失衡：(买-卖)/(买+卖) = {plan.get('order_book_imbalance', '--')}",
        "理由：" + "；".join(str(item) for item in (plan.get("reasons") or [])),
        "这只是提醒，不会提交任何委托。请在券商端核对价格、可用股数和风险后自行决定。",
    ]
    # Derive the label from the action, never from mutable/stale plan text.
    price_range_type = spec.price_range_type
    low = _number(plan.get("price_range_low"))
    high = _number(plan.get("price_range_high"))
    reference = _number(plan.get("price_range_reference"))
    if low is not None and high is not None:
        if price_range_type == "buyback":
            ceiling = _number(plan.get("price_range_hard_ceiling"))
            ceiling_text = f"；硬上限 {ceiling:.2f} 元" if ceiling is not None else ""
            lines.append(f"建议买回价格小区间：{low:.2f}～{high:.2f} 元{ceiling_text}。")
        elif price_range_type == "initial_buy":
            lines.append(f"建议买入价格小区间：{low:.2f}～{high:.2f} 元（参考价 {reference:.2f} 元）。")
        else:
            lines.append(f"建议卖出价格小区间：{low:.2f}～{high:.2f} 元（参考价 {reference:.2f} 元）。")
    lines.append("收益口径：50%总资金持股不动为基准；T腿固定按总资金25%计算。")
    if plan.get("break_even_buyback_price"):
        lines.append(f"按提醒价及预设摩擦成本，买回价须低于 {plan['break_even_buyback_price']} 元才不劣于持股不动。")
    if plan.get("t_leg_excess_pct") is not None:
        lines.append(f"按提醒价估算：T腿净超额 {plan['t_leg_excess_pct']:.3f}%，折算总资金超额 {plan['total_capital_excess_pct']:.3f}%。")
    # Keep the keyword for callers of the old helper signature, but do not
    # let it override the action-derived direction.
    if conditional_buyback and spec.direction != "buyback":
        raise ValueError("conditional_buyback is only valid for a buyback action")
    if spec.direction == "buyback":
        lines.append("仅当你今天已按此前卖出 T 仓提醒实际成交相同数量时，才可考虑买回；若未卖出，请忽略本邮件。")
    return "\n".join(lines) + "\n"


def run_once(
    *,
    broker_state_path: Path = BROKER_STATE_PATH,
    ledger_path: Path = LEDGER_PATH,
    notification_state_path: Path = NOTIFICATION_STATE_PATH,
    confirmed_inventory_path: Path | None = None,
    portfolio_state_path: Path | None = None,
    watchlist_path: Path = WATCHLIST_PATH,
    shadow_attribution_state_path: Path = SHADOW_ATTRIBUTION_STATE_PATH,
    snapshot_dir: Path | None = None,
    market_client: MarketClient | None = None,
    email_sender: Any = _send_default_email,
    checked_at: datetime | None = None,
    prefer_warehouse_minute: bool = True,
) -> dict[str, Any]:
    """Record one non-executable portfolio T assessment and alert on new T legs.

    A buyback reminder is always explicitly conditional on the user having
    actually filled the prior sell reminder in the broker.  The monitor never
    queries orders, mutates an account, or submits a commission.
    """
    now = checked_at or datetime.now()
    trade_date = now.date().isoformat()
    base: dict[str, Any] = {
        "mode": "paper_only_holding_t_monitor",
        "order_path_enabled": False,
        "checked_at": now.isoformat(sep=" ", timespec="seconds"),
        "max_daily_action_notices": MAX_DAILY_ACTION_NOTICES,
    }
    if not _is_trading_day(now):
        result = {
            **base,
            "ok": True,
            "reason": "non_trading_day",
            "trading_day": False,
            "evaluated_count": 0,
            "plans": [],
            "notifications": [],
        }
        _append(result, ledger_path)
        return result
    snapshot_dir = snapshot_dir or (ledger_path.parent / "tick_snapshots")
    confirmed_inventory_path = confirmed_inventory_path or (CONFIRMED_T_INVENTORY_PATH if ledger_path == LEDGER_PATH else ledger_path.parent / "confirmed_t_inventory.json")
    portfolio_state_path = portfolio_state_path or (PORTFOLIO_STATE_PATH if ledger_path == LEDGER_PATH else ledger_path.parent / "portfolio_state.json")
    try:
        state = json.loads(broker_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result = {**base, "ok": False, "reason": "broker_snapshot_invalid", "error": f"{type(exc).__name__}: {exc}", "plans": []}
        _append(result, ledger_path)
        return result
    positions = state.get("holdings") if isinstance(state.get("holdings"), list) else []
    normalized = [{**row, "code": _code(row.get("code"))} for row in positions if isinstance(row, dict) and _code(row.get("code"))]
    watchlist_codes = _watchlist_codes(watchlist_path)
    if watchlist_codes is not None:
        normalized = [row for row in normalized if row["code"] in watchlist_codes]
    watch_only_codes: set[str] = set()
    if watchlist_codes:
        held_codes = {row["code"] for row in normalized}
        watch_only_codes = watchlist_codes - held_codes
        # A virtual one-lot-capable position reuses the exact same T signal
        # gates. It is only an alerting device: proposed quantities are removed
        # before any result/email is exposed.
        portfolio_positions = load_holding_t_portfolio_state(portfolio_state_path).get("positions") or {}
        normalized.extend({
            "code": code,
            "shares": 400 if float((portfolio_positions.get(code) or {}).get("current_weight_pct") or 0) > 0 else 0,
            "available_shares": 400 if float((portfolio_positions.get(code) or {}).get("current_weight_pct") or 0) > 0 else 0,
            "watch_only": True,
        } for code in sorted(watch_only_codes))
    if not normalized:
        result = {**base, "ok": True, "reason": "no_tracked_holdings", "plans": [], "watchlist_codes": sorted(watchlist_codes or [])}
        _append(result, ledger_path)
        return result
    if market_client is None:
        from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
        market_client = QmtMiniMarketClient()
    codes = [row["code"] for row in normalized]
    stock_names = _stock_names(codes)
    notification_state = _load_json(notification_state_path, {"trade_date": trade_date, "cycles": {}, "action_notice_count": 0, "exception_keys": []})
    if notification_state.get("trade_date") != trade_date:
        notification_state = {"trade_date": trade_date, "cycles": {}, "action_notice_count": 0, "exception_keys": []}
    notices: list[dict[str, Any]] = []
    qmt_market_state = notification_state.get("qmt_market_data_state") if isinstance(notification_state.get("qmt_market_data_state"), dict) else {}
    try:
        raw_ticks = market_client.get_full_tick(codes)
        ticks = {code: _tick_plan((raw_ticks or {}).get(code) or {}) for code in codes}
        if qmt_market_state.get("status") == "unavailable" and bool(qmt_market_state.get("alert_sent")) and _in_signal_window(now):
            alert_at_text = str(qmt_market_state.get("alert_sent_at") or qmt_market_state.get("last_seen_at") or "").strip()
            recovered_at_text = now.isoformat(sep=" ", timespec="seconds")
            recovery_body = (
                "做T监控已确认 QMT Tick /分钟行情恢复，之前的异常提醒可以关闭。\n"
                f"恢复时间：{recovered_at_text}\n"
                f"前次异常时间：{alert_at_text or '--'}\n"
                f"标的：{', '.join(codes)}\n"
                "系统已自动恢复继续扫描，未提交任何委托。\n"
            )
            try:
                email_sender("[AiStock 做T] QMT行情恢复通知", recovery_body)
                notices.append({"code": ",".join(codes), "action": "qmt_market_data_recovered", "sent": True})
                notification_state.pop("qmt_market_data_state", None)
            except Exception as mail_exc:
                notices.append({"code": ",".join(codes), "action": "qmt_market_data_recovered", "sent": False, "reason": f"email_error:{type(mail_exc).__name__}"})
                notification_state["qmt_market_data_state"] = {
                    **qmt_market_state,
                    "status": "unavailable",
                    "recovery_send_error": str(mail_exc),
                    "last_recovery_attempt_at": recovered_at_text,
                }
    except Exception as exc:
        if _in_signal_window(now):
            exception_keys = notification_state.get("exception_keys") if isinstance(notification_state.get("exception_keys"), list) else []
            exception_key = f"qmt_market_data_unavailable:{trade_date}"
            if exception_key not in exception_keys:
                try:
                    email_sender(
                        "[AiStock 做T] QMT行情异常提醒",
                        "做T监控读取 QMT Tick 或分钟行情失败，已停止生成交易提醒。\n"
                        f"检查时间：{now.isoformat(sep=' ', timespec='seconds')}\n"
                        f"标的：{', '.join(codes)}\n"
                        f"错误：{type(exc).__name__}: {exc}\n"
                        "安全边界：本通知不会提交任何委托；QMT/xtdata恢复后会自动继续扫描。\n",
                    )
                    notices.append({"code": ",".join(codes), "action": "qmt_market_data_exception", "sent": True})
                    exception_keys.append(exception_key)
                    notification_state["qmt_market_data_state"] = {
                        "status": "unavailable",
                        "alert_sent": True,
                        "alert_sent_at": now.isoformat(sep=" ", timespec="seconds"),
                        "last_seen_at": now.isoformat(sep=" ", timespec="seconds"),
                        "last_error": f"{type(exc).__name__}: {exc}",
                        "codes": codes[:50],
                    }
                except Exception as mail_exc:
                    notices.append({"code": ",".join(codes), "action": "qmt_market_data_exception", "sent": False, "reason": f"email_error:{type(mail_exc).__name__}"})
                    notification_state["qmt_market_data_state"] = {
                        "status": "unavailable",
                        "alert_sent": False,
                        "last_seen_at": now.isoformat(sep=" ", timespec="seconds"),
                        "last_error": f"{type(exc).__name__}: {exc}",
                        "last_send_error": str(mail_exc),
                        "codes": codes[:50],
                    }
            notification_state["exception_keys"] = exception_keys[-30:]
            _save_json(notification_state_path, notification_state)
        else:
            notification_state["qmt_market_data_state"] = {
                "status": "unavailable",
                "alert_sent": bool(qmt_market_state.get("alert_sent")),
                "last_seen_at": now.isoformat(sep=" ", timespec="seconds"),
                "last_error": f"{type(exc).__name__}: {exc}",
                "codes": codes[:50],
            }
            _save_json(notification_state_path, notification_state)
        result = {
            **base,
            "ok": False,
            "reason": "qmt_market_data_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "plans": [],
            "notifications": notices,
            "minute_repair": {"status": "not_started", "notify_allowed": True},
        }
        _append(result, ledger_path)
        return result
    minute_data_error: str | None = None
    try:
        minute = _minute_metrics(market_client, codes, prefer_warehouse=prefer_warehouse_minute)
    except Exception as exc:
        minute = {}
        minute_data_error = f"{type(exc).__name__}: {exc}"
        logger.warning("holding-T minute data fetch failed; starting repair path: %s", exc)
    try:
        minute_repair = (
            _request_intraday_minute_repair(codes, minute, now=now)
            if now.date() == datetime.now().date()
            else {
                "status": "historical_skip",
                "blocked_codes": _minute_blocked_codes(codes, minute),
                "notify_allowed": True,
            }
        )
    except Exception as exc:
        minute_repair = {
            "status": "repair_error",
            "blocked_codes": _minute_blocked_codes(codes, minute),
            "error": f"{type(exc).__name__}: {exc}",
            "notify_allowed": True,
        }
        logger.warning("holding-T minute repair request failed: %s", exc)
    cycles = notification_state.get("cycles") if isinstance(notification_state.get("cycles"), dict) else {}
    confirmed_cycles = load_confirmed_t_inventory(confirmed_inventory_path)
    sold_by_code = {
        code: int((confirmed_cycles.get(code) or {}).get("open_shares") or 0)
        for code in [row["code"] for row in normalized]
    }
    pending_by_code = {
        code: int((cycles.get(code) or {}).get("sell_notice_shares") or 0)
        for code in [row["code"] for row in normalized]
        if code not in confirmed_cycles
    }
    result = {
        **base,
        "ok": True,
        **evaluate_portfolio_t(
            normalized,
            ticks,
            minute,
            sold_t_shares_by_code=sold_by_code,
            pending_sell_shares_by_code=pending_by_code,
            checked_at=now,
        ),
    }
    result["minute_repair"] = minute_repair
    if minute_data_error:
        result["minute_data_error"] = minute_data_error
    portfolio_positions = load_holding_t_portfolio_state(portfolio_state_path).get("positions") or {}
    for plan in result.get("plans") or []:
        code = str(plan.get("code") or "")
        position_state = portfolio_positions.get(code) or {}
        current_weight = float(position_state.get("current_weight_pct") or 0.0)
        target_weight = float(position_state.get("target_weight_pct") or 50.0)
        metric, tick = minute.get(code) or {}, ticks.get(code) or {}
        if current_weight >= target_weight or not bool(metric.get("minute_fresh")) or not bool(tick.get("tick_fresh")):
            continue
        last, vwap, rsi, imbalance = _number(tick.get("last_price")), _number(metric.get("vwap_5m")), _number(metric.get("rsi6")), _number(tick.get("order_book_imbalance"))
        if not last or not vwap or imbalance is None:
            continue
        discount = last / vwap - 1
        standard_initial_buy = rsi is not None and discount <= -0.002 and rsi <= 35 and imbalance <= -0.10
        panic_initial_buy = rsi is None and discount <= -0.008 and imbalance <= -0.35
        if standard_initial_buy or panic_initial_buy:
            is_initial_buy = current_weight <= 0.0
            action = "paper_initial_buy" if is_initial_buy else "paper_add_to_target"
            position_text = f"当前总资金{current_weight:g}% / 目标{target_weight:g}%"
            if panic_initial_buy:
                reason = (
                    "急跌建仓：RSI暂缺但价格显著低于5分钟VWAP且盘口卖压极重；仅建立总资金25%的首笔仓位"
                    if is_initial_buy else
                    f"急跌补仓：{position_text}，RSI暂缺但价格显著低于5分钟VWAP且盘口卖压极重；仅补总资金25%至目标仓位"
                )
            else:
                reason = (
                    "零仓位建仓：价格低于5分钟VWAP、短周期超卖且卖压占优；仅建立总资金25%的首笔仓位"
                    if is_initial_buy else
                    f"补仓至目标：{position_text}，价格低于5分钟VWAP、短周期超卖且卖压占优；仅补总资金25%至目标仓位"
                )
            plan.update(
                action=action,
                market_price=round(last, 3),
                price_vs_vwap_pct=round(discount * 100, 3),
                rsi6=round(rsi, 2) if rsi is not None else None,
                order_book_imbalance=imbalance,
                current_weight_pct=current_weight,
                target_weight_pct=target_weight,
                reasons=[reason],
            )
    result["minute_sources"] = {
        code: {
            "source": metric.get("minute_source"),
            "fresh": bool(metric.get("minute_fresh")),
            "age_seconds": metric.get("minute_age_seconds"),
        }
        for code, metric in minute.items()
    }
    for plan in result.get("plans") or []:
        if plan.get("action") != "paper_buyback_t_leg":
            continue
        cycle = confirmed_cycles.get(str(plan.get("code") or "")) or {}
        excess = paper_t_cycle_excess(cycle.get("reference_sell_price"), plan.get("market_price"))
        if excess is None:
            plan.update(action="observe", reasons=["缺少此前卖出提醒价格，无法证明做T优于持股不动"])
            continue
        plan.update({key: round(value, 3) for key, value in excess.items()})
        if float(excess["total_capital_excess_pct"]) <= 0:
            plan.update(action="observe", reasons=["买回后不能产生正的总资金超额收益，不做无效T"])
    for plan in result.get("plans") or []:
        if plan.get("code") not in watch_only_codes:
            continue
        plan["mode"] = "signal_only_watchlist_t"
        plan["position_source"] = "watchlist_no_qmt_account"
        plan["signal_quantity_required"] = True
        plan["virtual_signal_shares"] = int(plan.get("proposed_shares") or 0)
        plan["proposed_shares"] = None
        plan["shares"] = None
        plan["available_shares"] = None
        if plan.get("action") == "paper_sell_t_leg":
            plan["action"] = "watch_sell_t_signal"
        elif plan.get("action") == "paper_buyback_t_leg":
            plan["action"] = "watch_buyback_t_signal"
        elif plan.get("action") == "paper_initial_buy":
            plan["action"] = "watch_initial_buy_signal"
        elif plan.get("action") == "paper_add_to_target":
            plan["action"] = "watch_add_to_target_signal"
    for plan in result.get("plans") or []:
        stock_name = stock_names.get(_code(plan.get("code")))
        if stock_name:
            plan["stock_name"] = stock_name
    result["shadow_attribution"] = _update_shadow_attribution(
        result.get("plans") or [], ticks, now=now, state_path=shadow_attribution_state_path,
    )
    for plan in result.get("plans") or []:
        action = str(plan.get("action") or "")
        code = str(plan.get("code") or "")
        cycle = cycles.get(code) if isinstance(cycles.get(code), dict) else {}
        if action in _NON_NOTIFICATION_ACTIONS:
            continue
        try:
            action_spec = _notification_spec(action)
        except ValueError:
            logger.error("blocking email for unsupported T action=%r code=%s", action, code)
            notices.append({"code": code, "action": action, "sent": False, "reason": "unsupported_notification_action"})
            continue
        if int(notification_state.get("action_notice_count") or 0) >= MAX_DAILY_ACTION_NOTICES:
            notices.append({"code": code, "action": action, "sent": False, "reason": "daily_notice_limit_reached"})
            continue
        is_sell = action_spec.direction == "sell"
        is_buyback = action_spec.direction == "buyback"
        if is_sell and cycle.get("sell_notice_shares"):
            continue
        if is_buyback and cycle.get("buyback_notice_sent"):
            continue
        try:
            price_range = _suggested_price_range(plan)
            if price_range:
                plan.update(price_range)
            subject = _notification_subject(plan, action_spec=action_spec)
            body = _notification_body(plan, conditional_buyback=is_buyback, action_spec=action_spec)
            send_result = email_sender(subject, body)
            notification_state["action_notice_count"] = int(notification_state.get("action_notice_count") or 0) + 1
            if is_sell:
                cycles[code] = {
                    "sell_notice_shares": int(plan.get("virtual_signal_shares") or plan.get("proposed_shares") or 100),
                    "sell_notice_at": now.isoformat(sep=" ", timespec="seconds"),
                    "sell_signal_price": plan.get("market_price"),
                    "signal_only": bool(plan.get("signal_quantity_required")),
                }
            elif is_buyback:
                cycles[code] = {**cycle, "buyback_notice_sent": True, "buyback_notice_at": now.isoformat(sep=" ", timespec="seconds")}
            notices.append({"code": code, "action": action, "sent": bool((send_result or {}).get("sent", True))})
        except Exception as exc:
            notices.append({"code": code, "action": action, "sent": False, "reason": f"email_error:{type(exc).__name__}"})
    stale_codes = _minute_blocked_codes(codes, minute)
    exception_keys = notification_state.get("exception_keys") if isinstance(notification_state.get("exception_keys"), list) else []
    if stale_codes and _in_signal_window(now) and bool(minute_repair.get("notify_allowed", True)):
        exception_key = f"minute_data_stale:{trade_date}:{','.join(stale_codes)}"
        stale_state = notification_state.get("minute_stale_state") if isinstance(notification_state.get("minute_stale_state"), dict) else {}
        previous_codes = stale_state.get("codes") if isinstance(stale_state.get("codes"), list) else []
        consecutive_count = int(stale_state.get("consecutive_count") or 0) + 1 if previous_codes == stale_codes else 1
        notification_state["minute_stale_state"] = {
            "codes": stale_codes,
            "first_seen_at": stale_state.get("first_seen_at") if previous_codes == stale_codes else now.isoformat(sep=" ", timespec="seconds"),
            "last_seen_at": now.isoformat(sep=" ", timespec="seconds"),
            "consecutive_count": consecutive_count,
        }
        # Older monitor versions persisted the dedupe key after mailing but did
        # not retain alert_sent. Preserve that incident across an upgrade so a
        # later fresh scan can still send its matching recovery notification.
        if exception_key in exception_keys and previous_codes == stale_codes and not bool(stale_state.get("alert_sent")):
            notification_state["minute_stale_state"] = {
                **notification_state["minute_stale_state"],
                "alert_sent": True,
                "alert_sent_at": stale_state.get("alert_sent_at") or stale_state.get("first_seen_at") or now.isoformat(sep=" ", timespec="seconds"),
                "alert_inferred_from_exception_key": True,
            }
        if exception_key not in exception_keys and consecutive_count >= MINUTE_STALE_NOTICE_SCAN_THRESHOLD:
            try:
                email_sender(
                    "[AiStock 做T] 分钟数据异常提醒",
                    "做T监控已自动尝试实时落库与QMT缓存两路分钟行情。"
                    f"以下标的的5分钟数据仍过期，已停止生成交易信号：{', '.join(stale_codes)}。"
                    "系统不会据此提交任何委托；数据恢复后会自动继续扫描。\n",
                )
                notices.append({"code": ",".join(stale_codes), "action": "minute_data_exception", "sent": True})
                exception_keys.append(exception_key)
                notification_state["minute_stale_state"] = {
                    **notification_state["minute_stale_state"],
                    "alert_sent": True,
                    "alert_sent_at": now.isoformat(sep=" ", timespec="seconds"),
                }
            except Exception as exc:
                notices.append({"code": ",".join(stale_codes), "action": "minute_data_exception", "sent": False, "reason": f"email_error:{type(exc).__name__}"})
        notification_state["exception_keys"] = exception_keys[-30:]
    elif "minute_stale_state" in notification_state or any(
        key.startswith(f"minute_data_stale:{trade_date}:") and key.replace("minute_data_stale:", "minute_data_recovered:", 1) not in exception_keys
        for key in exception_keys
    ):
        stale_state = notification_state.get("minute_stale_state") if isinstance(notification_state.get("minute_stale_state"), dict) else {}
        if not stale_state:
            # A pre-closure monitor can clear its transient stale state before
            # the upgraded monitor gets a fresh scan. Its mailed exception key
            # remains durable evidence that needs one matching recovery mail.
            legacy_key = next(
                key for key in exception_keys
                if key.startswith(f"minute_data_stale:{trade_date}:")
                and key.replace("minute_data_stale:", "minute_data_recovered:", 1) not in exception_keys
            )
            stale_state = {
                "codes": [code for code in legacy_key.split(":", 2)[-1].split(",") if code],
                "first_seen_at": None,
                "alert_sent": True,
                "alert_inferred_from_exception_key": True,
            }
            notification_state["minute_stale_state"] = stale_state
        clear_minute_stale_state = True
        if bool(stale_state.get("alert_sent")) and _in_signal_window(now):
            alert_at_text = str(stale_state.get("alert_sent_at") or stale_state.get("first_seen_at") or "").strip()
            recovered_at_text = now.isoformat(sep=" ", timespec="seconds")
            recovery_body = (
                "做T监控已确认分钟行情恢复，之前的异常提醒可以关闭。\n"
                f"恢复时间：{recovered_at_text}\n"
                f"异常首次出现：{alert_at_text or '--'}\n"
                f"恢复标的：{', '.join(stale_state.get('codes') or stale_codes or codes)}\n"
                "系统已自动恢复继续扫描，未提交任何委托。\n"
            )
            try:
                email_sender("[AiStock 做T] 分钟数据恢复通知", recovery_body)
                notices.append({"code": ",".join(stale_state.get("codes") or stale_codes or codes), "action": "minute_data_recovered", "sent": True})
                recovery_key = f"minute_data_recovered:{trade_date}:{','.join(stale_state.get('codes') or stale_codes or codes)}"
                if recovery_key not in exception_keys:
                    exception_keys.append(recovery_key)
                notification_state["exception_keys"] = exception_keys[-30:]
            except Exception as exc:
                notices.append({"code": ",".join(stale_state.get("codes") or stale_codes or codes), "action": "minute_data_recovered", "sent": False, "reason": f"email_error:{type(exc).__name__}"})
                notification_state["minute_stale_state"] = {
                    **stale_state,
                    "recovery_send_error": str(exc),
                    "last_recovery_attempt_at": recovered_at_text,
                }
                clear_minute_stale_state = False
        if clear_minute_stale_state:
            notification_state.pop("minute_stale_state", None)
    notification_state["cycles"] = cycles
    _save_json(notification_state_path, notification_state)
    result["notifications"] = notices
    result["watchlist_codes"] = sorted(watchlist_codes or [])
    _append_tick_snapshots(
        raw_ticks,
        ticks,
        minute,
        result.get("plans") or [],
        checked_at=now,
        snapshot_dir=snapshot_dir,
    )
    _append(result, ledger_path)
    return result
