"""G3 automatic order executor.

This module is intentionally closed by default.  It converts only fresh G3
two-mode tickets into two-slot QMT orders after account, session, duplicate and
price checks.  It never changes a router ticket into a formal signal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from execution.qmtmini_gateway import QmtMiniOrderGateway
from utils.paths import runtime_path
from strategies.contracts import formal_g3_score88_contract


FORMAL_G3_CONTRACT = formal_g3_score88_contract()
STRATEGY_ID = FORMAL_G3_CONTRACT["strategy_id"]
# The retired standalone breakout route must never become executable merely
# because its ticket shape resembles the current mainwave contract.
ALLOWED_STRATEGIES = {FORMAL_G3_CONTRACT["trade_strategy"]}
STATE_PATH = runtime_path("gen3_auto_order", "state.json")
RUN_LEDGER_PATH = runtime_path("gen3_auto_order", "runs.jsonl")
MANAGED_POSITIONS_PATH = runtime_path("gen3_auto_order", "managed_positions.jsonl")
TICKETS_PATH = runtime_path("gen3_state_alpha", "latest_shadow_tickets.csv")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROUTER_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_state_router_shadow_daily_v1.py"


def _now() -> datetime:
    return datetime.now()


def _default_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "dry_run": True,
        "strategy_id": STRATEGY_ID,
        "slots": 2,
        "slot_percent": 0.5,
        "daily_order_limit": 2,
        "require_flat_account": True,
        "require_no_open_orders": True,
        "require_fresh_same_day_ticket": True,
        "trading_hours_only": True,
        "interval_seconds": 60,
        "exit_enabled": False,
        "last_run_at": None,
        "last_result": None,
    }


def load_state() -> dict[str, Any]:
    state = _default_state()
    try:
        if STATE_PATH.exists():
            saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                state.update(saved)
    except (OSError, json.JSONDecodeError):
        pass
    state["enabled"] = bool(state.get("enabled"))
    state["dry_run"] = bool(state.get("dry_run", True))
    state["strategy_id"] = STRATEGY_ID
    state["slots"] = max(1, min(2, int(state.get("slots") or 2)))
    state["slot_percent"] = 1.0 / state["slots"]
    state["daily_order_limit"] = max(1, min(state["slots"], int(state.get("daily_order_limit") or state["slots"])))
    state["interval_seconds"] = max(30, int(state.get("interval_seconds") or 60))
    for key in ("require_flat_account", "require_no_open_orders", "require_fresh_same_day_ticket", "trading_hours_only", "exit_enabled"):
        state[key] = bool(state.get(key, True))
    return state


def save_state(state: dict[str, Any]) -> dict[str, Any]:
    payload = _default_state()
    payload.update(state if isinstance(state, dict) else {})
    normalized = load_state_from(payload)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def load_state_from(candidate: dict[str, Any]) -> dict[str, Any]:
    """Normalize an in-memory state without reading or writing files."""
    baseline = _default_state()
    baseline.update(candidate if isinstance(candidate, dict) else {})
    # Reuse the exact same normalization as load_state without temporarily
    # mutating the runtime configuration file.
    baseline["enabled"] = bool(baseline.get("enabled"))
    baseline["dry_run"] = bool(baseline.get("dry_run", True))
    baseline["strategy_id"] = STRATEGY_ID
    baseline["slots"] = max(1, min(2, int(baseline.get("slots") or 2)))
    baseline["slot_percent"] = 1.0 / baseline["slots"]
    baseline["daily_order_limit"] = max(1, min(baseline["slots"], int(baseline.get("daily_order_limit") or baseline["slots"])))
    baseline["interval_seconds"] = max(30, int(baseline.get("interval_seconds") or 60))
    for key in ("require_flat_account", "require_no_open_orders", "require_fresh_same_day_ticket", "trading_hours_only", "exit_enabled"):
        baseline[key] = bool(baseline.get(key, True))
    return baseline


def _in_trading_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hhmm = now.strftime("%H:%M")
    return ("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "14:57")


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "\u662f"}


def refresh_current_30m_router(*, now: datetime | None = None) -> dict[str, Any]:
    """Create a same-day router snapshot after a completed 30-minute bar.

    The caller owns scheduling.  This function does not submit any order.
    """
    checked_at = now or _now()
    if not ROUTER_SCRIPT.exists():
        return {"ok": False, "reason": "router_script_missing", "script": str(ROUTER_SCRIPT)}
    day = checked_at.strftime("%Y-%m-%d")
    # The daily skeleton is frozen on the prior trading day; the current day
    # is reserved for the completed-30m breakout confirmation.  Passing the
    # current day as both dates would use unfinished daily data as a signal.
    command = [sys.executable, str(ROUTER_SCRIPT), "--entry-date", day]
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "reason": "router_refresh_failed", "error": str(exc)}
    if completed.returncode != 0:
        return {
            "ok": False,
            "reason": "router_refresh_failed",
            "return_code": completed.returncode,
            "stderr": completed.stderr[-1500:],
        }
    return {"ok": True, "entry_date": day, "stdout_tail": completed.stdout[-500:]}


def _read_tickets() -> pd.DataFrame:
    if not TICKETS_PATH.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(TICKETS_PATH, encoding="utf-8-sig", low_memory=False)
    except Exception:
        return pd.DataFrame()


def _eligible_tickets(now: datetime) -> tuple[pd.DataFrame, str | None]:
    tickets = _read_tickets()
    if tickets.empty:
        return tickets, "latest_shadow_tickets_missing_or_empty"
    for column in ("qualified_shadow_buy", "router_eligible"):
        if column not in tickets.columns:
            return tickets.iloc[0:0], f"ticket_column_missing:{column}"
    qualified = tickets[tickets["qualified_shadow_buy"].map(_truthy) & tickets["router_eligible"].map(_truthy)].copy()
    if "trade_strategy" in qualified.columns:
        qualified = qualified[qualified["trade_strategy"].astype(str).isin(ALLOWED_STRATEGIES)]
    else:
        return qualified.iloc[0:0], "ticket_column_missing:trade_strategy"
    if "entry_date" not in qualified.columns:
        return qualified.iloc[0:0], "ticket_column_missing:entry_date"
    qualified["_entry_day"] = pd.to_datetime(qualified["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    qualified = qualified[qualified["_entry_day"] == now.strftime("%Y-%m-%d")]
    if qualified.empty:
        return qualified, "no_fresh_same_day_qualified_ticket"
    if "score" in qualified.columns:
        qualified["_score"] = pd.to_numeric(qualified["score"], errors="coerce").fillna(-1e9)
        qualified = qualified.sort_values(["_score", "code"], ascending=[False, True])
    return qualified, None


def _last_prices(codes: list[str]) -> dict[str, float]:
    from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient

    client = QmtMiniMarketClient()
    ticks = client.get_full_tick(codes)
    prices: dict[str, float] = {}
    for code in codes:
        row = (ticks or {}).get(code) or (ticks or {}).get(str(code).upper()) or {}
        for key in ("lastPrice", "last_price", "last", "close"):
            try:
                price = float(row.get(key))
            except (AttributeError, TypeError, ValueError):
                continue
            if price > 0:
                prices[str(code).upper()] = price
                break
    return prices


def _append_run(row: dict[str, Any]) -> None:
    RUN_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LEDGER_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _append_managed_position(row: dict[str, Any]) -> None:
    MANAGED_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MANAGED_POSITIONS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _managed_positions() -> dict[str, dict[str, Any]]:
    if not MANAGED_POSITIONS_PATH.exists():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for line in MANAGED_POSITIONS_PATH.read_text(encoding="utf-8").splitlines()[-1000:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        code = str(row.get("code") or "").upper()
        if code:
            latest[code] = row
    return latest


def _finish(state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    state["last_run_at"] = _now().isoformat(sep=" ", timespec="seconds")
    state["last_result"] = result
    save_state(state)
    _append_run(result)
    return result


def run_once(*, source: str = "manual", dry_run: bool | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate one entry cycle; live QMT submission needs two independent arms."""
    checked_at = now or _now()
    state = load_state()
    effective_dry_run = state["dry_run"] if dry_run is None else bool(dry_run)
    base = {
        "ok": True,
        "source": source,
        "checked_at": checked_at.isoformat(sep=" ", timespec="seconds"),
        "dry_run": effective_dry_run,
        "strategy_id": STRATEGY_ID,
        "submitted": [],
        "blocked": [],
    }
    if not state["enabled"]:
        base.update({"skipped": True, "reason": "executor_disabled"})
        return _finish(state, base)
    if state["trading_hours_only"] and not _in_trading_session(checked_at):
        base.update({"skipped": True, "reason": "not_trading_session"})
        return _finish(state, base)
    tickets, ticket_reason = _eligible_tickets(checked_at)
    if ticket_reason:
        base.update({"skipped": True, "reason": ticket_reason})
        return _finish(state, base)
    from data_fetcher.sources.qmtmini_client import QmtMiniTradingClient

    with QmtMiniTradingClient() as client:
        snapshot = client.account_snapshot(include_sensitive=True)
    if not snapshot.get("ok") or not snapshot.get("asset_available"):
        base.update({"ok": False, "reason": "qmt_account_snapshot_unavailable"})
        return _finish(state, base)
    positions = snapshot.get("positions") or []
    orders = snapshot.get("orders") or []
    if state["require_flat_account"] and positions:
        base.update({"skipped": True, "reason": "account_not_flat", "position_count": len(positions)})
        return _finish(state, base)
    if state["require_no_open_orders"] and orders:
        base.update({"skipped": True, "reason": "account_has_existing_orders", "order_count": len(orders)})
        return _finish(state, base)
    try:
        cash = float((snapshot.get("asset") or {}).get("cash") or 0)
    except (TypeError, ValueError):
        cash = 0.0
    if cash <= 0:
        base.update({"skipped": True, "reason": "no_available_cash"})
        return _finish(state, base)
    tickets = tickets.head(state["daily_order_limit"]).copy()
    codes = [str(code).upper() for code in tickets["code"].tolist()]
    try:
        prices = _last_prices(codes)
    except Exception as exc:
        base.update({"ok": False, "reason": "qmt_quote_unavailable", "error": str(exc)})
        return _finish(state, base)
    gateway = QmtMiniOrderGateway()
    for row in tickets.to_dict(orient="records"):
        code = str(row.get("code") or "").upper()
        price = prices.get(code)
        if not price:
            base["blocked"].append({"code": code, "reason": "latest_price_missing"})
            continue
        volume = int((cash * state["slot_percent"]) // (price * 100)) * 100
        if volume < 100:
            base["blocked"].append({"code": code, "reason": "slot_cash_below_one_lot"})
            continue
        entry_date = str(row.get("entry_date") or "")[:10]
        key = f"{STRATEGY_ID}|{entry_date}|{code}|BUY"
        remark = f"G3-{str(row.get('entry_mode') or 'mainwave')[:18]}"
        if effective_dry_run:
            outcome = gateway.dry_run_order("buy", code, volume, price, price_type="LATEST_PRICE", strategy_name="AiStockG3", order_remark=remark)
        else:
            outcome = gateway.submit_real_order("buy", code, volume, strategy_name="AiStockG3", order_remark=remark, idempotency_key=key)
            if outcome.get("ok"):
                _append_managed_position(
                    {
                        "created_at": checked_at.isoformat(sep=" ", timespec="seconds"),
                        "status": "entry_submitted_waiting_fill",
                        "code": code,
                        "entry_date": entry_date,
                        "ticket_key": row.get("ticket_key"),
                        "hard_stop": row.get("hard_stop"),
                        "structure_stop": row.get("structure_stop"),
                        "take_profit_1": row.get("take_profit_1"),
                        "take_profit_1_sell_ratio": row.get("take_profit_1_sell_ratio"),
                        "entry_order_id": (outcome.get("order") or {}).get("order_id"),
                        "half_take_profit_done": False,
                    }
                )
        base["submitted"].append({"code": code, "price_reference": price, "volume": volume, "idempotency_key": key, "outcome": outcome})
    base["skipped"] = False
    base["reason"] = "orders_evaluated"
    return _finish(state, base)


def run_exit_once(*, source: str = "manual", dry_run: bool | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Apply hard-stop / half-profit / runner-structure exits to managed G3 positions only."""
    checked_at = now or _now()
    state = load_state()
    effective_dry_run = state["dry_run"] if dry_run is None else bool(dry_run)
    result: dict[str, Any] = {"ok": True, "source": source, "checked_at": checked_at.isoformat(sep=" ", timespec="seconds"), "dry_run": effective_dry_run, "submitted": []}
    if not state["enabled"] or not state["exit_enabled"]:
        result.update({"skipped": True, "reason": "exit_executor_disabled"})
        return result
    if state["trading_hours_only"] and not _in_trading_session(checked_at):
        result.update({"skipped": True, "reason": "not_trading_session"})
        return result
    managed = _managed_positions()
    if not managed:
        result.update({"skipped": True, "reason": "no_managed_g3_position"})
        return result
    from data_fetcher.sources.qmtmini_client import QmtMiniTradingClient
    with QmtMiniTradingClient() as client:
        snapshot = client.account_snapshot(include_sensitive=True)
    positions = {str(p.get("stock_code") or "").upper(): p for p in (snapshot.get("positions") or [])}
    active = {
        code: row for code, row in managed.items()
        if code in positions and str(row.get("status") or "") not in {"exit_submitted", "closed"}
    }
    if not active:
        result.update({"skipped": True, "reason": "managed_orders_not_filled_or_position_closed"})
        return result
    prices = _last_prices(list(active))
    gateway = QmtMiniOrderGateway()
    for code, row in active.items():
        price = prices.get(code)
        position = positions[code]
        can_use = int(float(position.get("can_use_volume") or 0)) // 100 * 100
        if not price or can_use < 100:
            continue
        hard_stop = float(row.get("hard_stop") or 0)
        take_profit = float(row.get("take_profit_1") or 0)
        structure_stop = float(row.get("structure_stop") or 0)
        half_done = bool(row.get("half_take_profit_done"))
        action = ""
        volume = 0
        if hard_stop > 0 and price <= hard_stop:
            action, volume = "hard_stop", can_use
        elif not half_done and take_profit > 0 and price >= take_profit:
            action, volume = "take_profit_half", max(100, (can_use // 2 // 100) * 100)
        elif half_done and structure_stop > 0 and price <= structure_stop:
            action, volume = "runner_structure_stop", can_use
        if not action or volume <= 0:
            continue
        key = f"{STRATEGY_ID}|{row.get('entry_date')}|{code}|SELL|{action}"
        remark = f"G3-{action}"
        if effective_dry_run:
            outcome = gateway.dry_run_order("sell", code, volume, price, price_type="LATEST_PRICE", strategy_name="AiStockG3", order_remark=remark)
        else:
            outcome = gateway.submit_real_order("sell", code, volume, strategy_name="AiStockG3", order_remark=remark, idempotency_key=key)
        result["submitted"].append({"code": code, "action": action, "volume": volume, "outcome": outcome})
        if outcome.get("ok") and action == "take_profit_half":
            _append_managed_position({**row, "created_at": checked_at.isoformat(sep=" ", timespec="seconds"), "status": "half_take_profit_submitted", "half_take_profit_done": True})
        elif outcome.get("ok"):
            _append_managed_position({**row, "created_at": checked_at.isoformat(sep=" ", timespec="seconds"), "status": "exit_submitted", "exit_action": action})
    result.update({"skipped": False, "reason": "managed_exits_evaluated"})
    return result
