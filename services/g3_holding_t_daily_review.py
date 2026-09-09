"""After-close, read-only review for the G3 holding-T workflow."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from execution.g3_holding_t_engine import paper_t_cycle_excess
from utils.paths import runtime_path


RUNTIME_DIR = runtime_path("g3_holding_t_paper")
LEDGER_PATH = RUNTIME_DIR / "plans.jsonl"
SNAPSHOT_DIR = RUNTIME_DIR / "tick_snapshots"
EXECUTIONS_PATH = RUNTIME_DIR / "manual_executions.jsonl"
CONFIRMED_T_INVENTORY_PATH = RUNTIME_DIR / "confirmed_t_inventory.json"
REVIEW_DIR = RUNTIME_DIR / "daily_reviews"


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def load_confirmed_t_inventory(path: Path = CONFIRMED_T_INVENTORY_PATH) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    rows = payload.get("open_cycles") if isinstance(payload, dict) else {}
    rows = rows if isinstance(rows, dict) else {}
    return {str(code).upper(): row for code, row in rows.items() if isinstance(row, dict) and int(row.get("open_shares") or 0) > 0}


def _save_confirmed_t_inventory(rows: dict[str, dict[str, Any]], path: Path = CONFIRMED_T_INVENTORY_PATH) -> None:
    _write_json(path, {"schema_version": 1, "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"), "open_cycles": rows})


def _apply_execution_to_inventory(item: dict[str, Any], path: Path = CONFIRMED_T_INVENTORY_PATH) -> None:
    rows = load_confirmed_t_inventory(path)
    code, side, shares = str(item["code"]), str(item["side"]), int(item["shares"])
    if side == "sell":
        existing = rows.get(code)
        if existing:
            old_shares = int(existing.get("open_shares") or 0)
            reference = ((float(existing.get("reference_sell_price") or 0) * old_shares) + float(item["price"]) * shares) / (old_shares + shares)
            rows[code] = {**existing, "open_shares": old_shares + shares, "reference_sell_price": round(reference, 4), "last_sell_filled_at": item["filled_at"], "price_basis": "actual_manual_fill"}
        else:
            rows[code] = {"code": code, "open_shares": shares, "reference_sell_price": item["price"], "sell_filled_at": item["filled_at"], "price_basis": "actual_manual_fill", "confirmed": True}
    elif side == "buy" and code in rows:
        remaining = int(rows[code].get("open_shares") or 0) - shares
        if remaining > 0:
            rows[code] = {**rows[code], "open_shares": remaining, "last_buy_filled_at": item["filled_at"]}
        else:
            rows.pop(code, None)
    _save_confirmed_t_inventory(rows, path)


def _trade_date(value: str | None = None) -> str:
    if value:
        return datetime.fromisoformat(value).date().isoformat()
    return datetime.now().date().isoformat()


def record_manual_execution(
    payload: dict[str, Any],
    *,
    executions_path: Path = EXECUTIONS_PATH,
    confirmed_inventory_path: Path = CONFIRMED_T_INVENTORY_PATH,
) -> dict[str, Any]:
    """Record a user-confirmed fill only; no broker integration is involved."""
    code = str(payload.get("code") or "").strip().upper()
    side = str(payload.get("side") or "").strip().lower()
    price = _number(payload.get("price"))
    shares = int(_number(payload.get("shares")) or 0)
    filled_at = str(payload.get("filled_at") or datetime.now().isoformat(sep=" ", timespec="seconds"))
    if not code or side not in {"sell", "buy"} or price is None or price <= 0 or shares <= 0:
        raise ValueError("code, side(sell/buy), positive price and shares are required")
    item = {
        "schema_version": 1,
        "recorded_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "trade_date": _trade_date(filled_at),
        "code": code,
        "side": side,
        "price": round(price, 4),
        "shares": shares,
        "filled_at": filled_at,
        "fee": round(_number(payload.get("fee")) or 0.0, 4),
        "note": str(payload.get("note") or "").strip(),
        "source": "manual_user_record",
        "order_path_enabled": False,
    }
    executions_path.parent.mkdir(parents=True, exist_ok=True)
    with executions_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    _apply_execution_to_inventory(item, confirmed_inventory_path)
    return item


def _pair_cycles(executions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    open_sells: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    for row in sorted(executions, key=lambda item: str(item.get("filled_at") or "")):
        if row.get("side") == "sell":
            open_sells.append({**row, "remaining_shares": int(row.get("shares") or 0)})
            continue
        if row.get("side") != "buy":
            continue
        remaining_buy = int(row.get("shares") or 0)
        while remaining_buy > 0 and open_sells:
            sell = open_sells[0]
            matched = min(remaining_buy, int(sell.get("remaining_shares") or 0))
            result = paper_t_cycle_excess(sell.get("price"), row.get("price"))
            cycles.append({
                "sell_filled_at": sell.get("filled_at"),
                "sell_price": sell.get("price"),
                "buy_filled_at": row.get("filled_at"),
                "buy_price": row.get("price"),
                "shares": matched,
                "t_leg_excess_pct": round(float((result or {}).get("t_leg_excess_pct") or 0.0), 4),
                "total_capital_excess_pct": round(float((result or {}).get("total_capital_excess_pct") or 0.0), 4),
            })
            remaining_buy -= matched
            sell["remaining_shares"] = int(sell.get("remaining_shares") or 0) - matched
            if int(sell.get("remaining_shares") or 0) <= 0:
                open_sells.pop(0)
    return cycles, open_sells


def build_daily_review(
    trade_date: str | None = None,
    *,
    ledger_path: Path = LEDGER_PATH,
    snapshot_dir: Path = SNAPSHOT_DIR,
    executions_path: Path = EXECUTIONS_PATH,
) -> dict[str, Any]:
    """Build a per-stock, after-close review without emitting routine email."""
    date_text = _trade_date(trade_date)
    ledger = [row for row in _load_jsonl(ledger_path) if str(row.get("checked_at") or "")[:10] == date_text]
    snapshots = _load_jsonl(snapshot_dir / f"{date_text}.jsonl")
    executions = [row for row in _load_jsonl(executions_path) if row.get("trade_date") == date_text]
    codes = sorted({str(row.get("code") or "") for row in snapshots if row.get("code")} | {
        str(plan.get("code") or "")
        for row in ledger for plan in (row.get("plans") or []) if isinstance(plan, dict) and plan.get("code")
    } | {str(row.get("code") or "") for row in executions if row.get("code")})
    stock_reviews: list[dict[str, Any]] = []
    action_names = {"paper_sell_t_leg", "paper_buyback_t_leg", "watch_sell_t_signal", "watch_buyback_t_signal"}
    for code in codes:
        code_snapshots = [row for row in snapshots if row.get("code") == code]
        code_executions = [row for row in executions if row.get("code") == code]
        prices = [_number((row.get("tick") or {}).get("last_price")) for row in code_snapshots]
        prices = [value for value in prices if value is not None]
        snapshot_decisions = [row.get("decision") for row in code_snapshots if isinstance(row.get("decision"), dict)]
        decisions = list(snapshot_decisions)
        # Snapshots are the new audit source.  The plan ledger remains a
        # backward-compatible decision source for signals emitted before this
        # capture layer was introduced, including the current transition day.
        seen_decisions = {(str(row.get("checked_at") or ""), str(row.get("action") or "")) for row in decisions}
        for entry in ledger:
            for plan in entry.get("plans") or []:
                if not isinstance(plan, dict) or str(plan.get("code") or "") != code:
                    continue
                key = (str(plan.get("checked_at") or entry.get("checked_at") or ""), str(plan.get("action") or ""))
                if key not in seen_decisions:
                    decisions.append(plan)
                    seen_decisions.add(key)
        signal_events = [
            {
                "checked_at": decision.get("checked_at"), "action": decision.get("action"),
                "price": decision.get("market_price"), "price_vs_vwap_pct": decision.get("price_vs_vwap_pct"),
                "rsi6": decision.get("rsi6"), "order_book_imbalance": decision.get("order_book_imbalance"),
                "reasons": decision.get("reasons") or [],
            }
            for decision in snapshot_decisions if decision.get("action") in action_names
        ]
        # A legacy plan could remain action-shaped after the email was already
        # deduplicated.  For the review ledger, retain only actual sent notices
        # from pre-snapshot history; new snapshots supply their own signals.
        snapshot_signal_keys = {(str(item.get("checked_at") or ""), str(item.get("action") or "")) for item in signal_events}
        for entry in ledger:
            sent_actions = {
                (str(notice.get("code") or ""), str(notice.get("action") or ""))
                for notice in (entry.get("notifications") or [])
                if isinstance(notice, dict) and bool(notice.get("sent")) and notice.get("action") in action_names
            }
            for plan in entry.get("plans") or []:
                if not isinstance(plan, dict) or str(plan.get("code") or "") != code:
                    continue
                action = str(plan.get("action") or "")
                checked_at = str(plan.get("checked_at") or entry.get("checked_at") or "")
                key = (checked_at, action)
                if (code, action) not in sent_actions or key in snapshot_signal_keys:
                    continue
                signal_events.append({
                    "checked_at": checked_at, "action": action, "price": plan.get("market_price"),
                    "price_vs_vwap_pct": plan.get("price_vs_vwap_pct"), "rsi6": plan.get("rsi6"),
                    "order_book_imbalance": plan.get("order_book_imbalance"), "reasons": plan.get("reasons") or [],
                })
                snapshot_signal_keys.add(key)
        signal_events.sort(key=lambda item: str(item.get("checked_at") or ""))
        cycles, unpaired_sells = _pair_cycles(code_executions)
        action_counts: dict[str, int] = {}
        for decision in decisions:
            action = str(decision.get("action") or "unknown")
            action_counts[action] = action_counts.get(action, 0) + 1
        stock_reviews.append({
            "code": code,
            "snapshot_count": len(code_snapshots),
            "market_path": {
                "first_price": prices[0] if prices else None,
                "last_price": prices[-1] if prices else None,
                "high_price": max(prices) if prices else None,
                "low_price": min(prices) if prices else None,
            },
            "data_quality": {
                "snapshot_coverage": "complete" if len(code_snapshots) >= 200 else "partial_transition_day",
                "fresh_tick_count": sum(bool((row.get("tick") or {}).get("tick_fresh")) for row in code_snapshots),
                "fresh_minute_count": sum(bool((row.get("minute") or {}).get("minute_fresh")) for row in code_snapshots),
                "five_level_book_count": sum(bool((row.get("tick") or {}).get("bid_price")) for row in code_snapshots),
            },
            "decision_summary": {"action_counts": action_counts, "signal_events": signal_events},
            "manual_executions": code_executions,
            "realized_t_cycles": cycles,
            "open_t_sell_legs": [{key: row.get(key) for key in ("filled_at", "price", "remaining_shares")} for row in unpaired_sells],
            "review_status": "needs_actual_fill" if signal_events and not code_executions else ("completed" if cycles else "observe_only"),
        })
    return {
        "schema_version": 1,
        "trade_date": date_text,
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "mode": "after_close_holding_t_review",
        "order_path_enabled": False,
        "review_scope": "saved_watchlist_and_manual_execution_records",
        "stock_count": len(stock_reviews),
        "ledger_scan_count": len(ledger),
        "snapshot_count": len(snapshots),
        "status": "completed" if snapshots else "no_intraday_data",
        "stocks": stock_reviews,
    }


def run_daily_review(trade_date: str | None = None, *, review_dir: Path = REVIEW_DIR, **kwargs: Any) -> dict[str, Any]:
    review = build_daily_review(trade_date, **kwargs)
    _write_json(review_dir / f"{review['trade_date']}.json", review)
    if review["status"] == "completed":
        _write_json(review_dir / "latest.json", review)
    return review
