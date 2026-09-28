from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from utils.paths import report_path, runtime_path


BASE_CLOSED_TRADES_PATH = report_path(
    "g3_formal_unified_five_strategy_contract_v1",
    "formal_unified_closed_trades.csv",
)
INSTITUTIONAL_REPLAY_PATH = report_path(
    "institutional_mainwave_history_topic_replay_v1",
    "replayed_closed_trades.csv",
)
OUT_DIR = report_path("g3_historical_trades_with_open_positions_v1")
OUT_TRADES_PATH = OUT_DIR / "historical_trades_with_open_positions.csv"
OUT_OPEN_PATH = OUT_DIR / "open_positions_included.csv"
OUT_SUMMARY_PATH = OUT_DIR / "summary.json"

RUNTIME_DIR = runtime_path("gen3_state_alpha")
SHADOW_LEDGER_PATH = RUNTIME_DIR / "shadow_ledger.csv"
LATEST_TICKETS_PATH = RUNTIME_DIR / "latest_shadow_tickets.csv"
AFTERHOURS_TICKETS_PATH = RUNTIME_DIR / "afterhours_latest_shadow_tickets.csv"
BROKER_STATE_PATH = RUNTIME_DIR / "broker_state.json"
PAPER_EXECUTIONS_PATH = RUNTIME_DIR / "paper_executions.json"


OPEN_STATES = {
    "open",
    "opened",
    "holding",
    "open_shadow",
    "shadow_holding",
    "position_open",
    "active",
    "broker_open",
}
CLOSED_STATES = {"closed", "sold", "exit", "exited", "cancelled", "canceled", "rejected"}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _date_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""
    return text[:10]


def _normalize_code(value: Any) -> str:
    text = "" if value is None or pd.isna(value) else str(value).strip()
    if not text:
        return ""
    if "." in text:
        return text.upper()
    code6 = text.zfill(6)[-6:]
    if code6.startswith(("6", "9")):
        return f"{code6}.SH"
    return f"{code6}.SZ"


def _is_institutional_mainwave(df: pd.DataFrame) -> pd.Series:
    mask = pd.Series(False, index=df.index)
    for col in ["mode", "route", "trade_strategy", "route_strategy", "source_family"]:
        if col in df.columns:
            text = df[col].fillna("").astype(str)
            mask = mask | text.str.contains(
                "institutional_mainwave|score120_core|institutional_score120_mainwave",
                case=False,
                regex=True,
            )
    return mask


def _state_tokens(row: pd.Series) -> set[str]:
    parts: list[str] = []
    for col in ["trade_status", "position_status", "holding_status", "last_state", "shadow_status", "status"]:
        if col in row.index:
            parts.append(str(row.get(col) or "").strip().lower())
    return {part for text in parts for part in text.replace("-", "_").replace("/", "_").split() if part}


def _open_shadow_rows_from_ledger() -> pd.DataFrame:
    df = _read_csv(SHADOW_LEDGER_PATH)
    if df.empty:
        return pd.DataFrame()
    if "ticket_key" in df.columns:
        df = df.drop_duplicates(subset=["ticket_key"], keep="last")
    rows = []
    for _, row in df.iterrows():
        exit_date = _date_text(
            row.get("exit_date")
            or row.get("policy_exit_date")
            or row.get("closed_at")
            or row.get("sell_date")
            or row.get("sell_datetime")
        )
        if exit_date:
            continue
        states = _state_tokens(row)
        if states & CLOSED_STATES:
            continue
        if states & OPEN_STATES:
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    return _shadow_ticket_like_to_historical(pd.DataFrame(rows), source_type="runtime_shadow_ledger_open")


def _open_shadow_rows_from_tickets(path: Path, source_type: str) -> pd.DataFrame:
    df = _read_csv(path)
    if df.empty:
        return pd.DataFrame()
    if "ticket_key" in df.columns:
        df = df.drop_duplicates(subset=["ticket_key"], keep="last")
    if "qualified_shadow_buy" in df.columns:
        qualified = df["qualified_shadow_buy"].fillna(False).astype(str).str.lower().isin({"true", "1", "yes", "ok"})
        df = df[qualified]
    if df.empty:
        return pd.DataFrame()
    return _shadow_ticket_like_to_historical(df, source_type=source_type)


def _shadow_ticket_like_to_historical(df: pd.DataFrame, *, source_type: str) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out["entry_date"] = df.get("entry_date", "")
    out["policy_exit_date"] = ""
    out["decision_date"] = df.get("decision_date", "")
    out["confirm_datetime"] = df.get("confirm_datetime", "")
    out["entry_ts"] = df.get("planned_entry_ts", df.get("entry_ts", ""))
    out["exit_date"] = ""
    out["exit_ts"] = ""
    out["exit_datetime"] = ""
    out["code"] = df.get("code", "").map(_normalize_code) if "code" in df.columns else ""
    out["name"] = df.get("name", df.get("stock_name", ""))
    out["route"] = df.get("route", "")
    out["route_label"] = df.get("route_label", "")
    out["mode"] = df.get("route", "")
    out["mode_label"] = "current open holding"
    out["score"] = pd.to_numeric(df.get("wave_style_score", df.get("score")), errors="coerce")
    out["raw_score"] = pd.to_numeric(df.get("score"), errors="coerce")
    out["score_source"] = "wave_style_score"
    out["score_scale"] = "mainwave_0_140"
    out["net_ret"] = pd.NA
    out["stress_net_ret"] = pd.NA
    out["policy_net_ret"] = pd.NA
    out["entry_price"] = pd.to_numeric(df.get("reference_close", df.get("entry_price")), errors="coerce")
    out["reference_close"] = pd.to_numeric(df.get("reference_close", df.get("entry_price")), errors="coerce")
    out["stake"] = pd.NA
    out["exit_value"] = pd.NA
    out["realized_pnl"] = pd.NA
    out["market_style"] = ""
    out["policy"] = "g3_state_alpha_open_position"
    out["confirm_rule"] = df.get("m30_status", "")
    out["live_ready"] = df.get("paper_trade_ready", df.get("qualified_shadow_buy", False))
    out["shadow_action"] = "open_shadow_buy"
    out["formal_buy_signal"] = df.get("formal_buy_signal", False)
    out["auto_order_allowed"] = df.get("auto_order_allowed", False)
    out["order_path_enabled"] = df.get("order_path_enabled", False)
    out["block_reason"] = df.get("block_reason", "")
    out["position_slots"] = df.get("portfolio_slot_count", pd.NA)
    out["slot_pct"] = pd.to_numeric(df.get("position_pct"), errors="coerce")
    out["position_pct"] = pd.to_numeric(df.get("position_pct"), errors="coerce")
    out["trade_status"] = "open_shadow"
    out["shadow_status"] = df.get("shadow_status", "holding")
    out["last_state"] = df.get("last_state", "open")
    out["planned_entry_ts"] = df.get("planned_entry_ts", "")
    out["structure_stop"] = pd.to_numeric(df.get("structure_stop"), errors="coerce")
    out["hard_stop"] = pd.to_numeric(df.get("hard_stop"), errors="coerce")
    out["take_profit_1"] = pd.to_numeric(df.get("take_profit_1"), errors="coerce")
    out["exit_contract"] = df.get("exit_contract", "")
    out["source_type"] = source_type
    key = df.get("ticket_key", "")
    out["trade_key"] = key.where(key.astype(str).str.len() > 0, "open_position|" + out["entry_date"].astype(str) + "|" + out["code"].astype(str)) if isinstance(key, pd.Series) else "open_position|" + out["entry_date"].astype(str) + "|" + out["code"].astype(str)
    out["candidate_key"] = out["trade_key"]
    return out.reset_index(drop=True)


def _broker_open_rows() -> pd.DataFrame:
    state = _read_json(BROKER_STATE_PATH)
    holdings = state.get("holdings") if isinstance(state.get("holdings"), list) else []
    capital = state.get("capital") if isinstance(state.get("capital"), dict) else {}
    total_capital = pd.to_numeric(pd.Series([capital.get("total_capital")]), errors="coerce").iloc[0]
    rows: list[dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        shares = pd.to_numeric(pd.Series([item.get("shares") or item.get("quantity")]), errors="coerce").iloc[0]
        if pd.isna(shares) or float(shares) <= 0:
            continue
        code = _normalize_code(item.get("code"))
        cost_price = pd.to_numeric(pd.Series([item.get("cost_price") or item.get("entry_price")]), errors="coerce").iloc[0]
        current_price = pd.to_numeric(pd.Series([item.get("current_price") or item.get("reference_close")]), errors="coerce").iloc[0]
        market_value = pd.to_numeric(pd.Series([item.get("market_value")]), errors="coerce").iloc[0]
        if pd.isna(market_value) and not pd.isna(current_price):
            market_value = float(current_price) * float(shares)
        position_pct = float(market_value) / float(total_capital) if not pd.isna(market_value) and not pd.isna(total_capital) and float(total_capital) else pd.NA
        entry_price = cost_price if not pd.isna(cost_price) else current_price
        pnl_ratio = pd.NA
        if not pd.isna(entry_price) and not pd.isna(current_price) and float(entry_price) > 0:
            pnl_ratio = float(current_price) / float(entry_price) - 1.0
        rows.append(
            {
                "entry_date": _date_text(item.get("entry_date") or state.get("updated_at") or datetime.now().strftime("%Y-%m-%d")),
                "policy_exit_date": "",
                "decision_date": "",
                "confirm_datetime": "",
                "entry_ts": "",
                "exit_date": "",
                "exit_ts": "",
                "exit_datetime": "",
                "code": code,
                "name": item.get("name") or code,
                "route": "broker_real_position",
                "route_label": "real broker holding",
                "mode": "broker_real_position",
                "mode_label": "current open holding",
                "net_ret": pnl_ratio,
                "entry_price": entry_price,
                "reference_close": current_price,
                "stake": market_value,
                "exit_value": pd.NA,
                "realized_pnl": pd.NA,
                "policy": "broker_open_position",
                "live_ready": True,
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "position_pct": position_pct,
                "slot_pct": position_pct,
                "trade_status": "open_shadow",
                "shadow_status": "broker_open",
                "last_state": "open",
                "source_type": "broker_state_open_holding",
                "trade_key": f"broker_open|{code}",
                "candidate_key": f"broker_open|{code}",
            }
        )
    return pd.DataFrame(rows)


def _paper_execution_rows() -> pd.DataFrame:
    data = _read_json(PAPER_EXECUTIONS_PATH)
    executions = data.get("executions") if isinstance(data.get("executions"), list) else []
    if not executions:
        return pd.DataFrame()
    buys: dict[tuple[str, str], dict[str, Any]] = {}
    sells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in executions:
        if not isinstance(item, dict):
            continue
        marker = " ".join(str(item.get(key) or "") for key in ["name", "ticket_key", "source", "notes"]).lower()
        if "e2e" in marker or "test" in marker or "ptrade_bridge" in marker:
            continue
        code = _normalize_code(item.get("code"))
        entry_date = _date_text(item.get("entry_date") or item.get("created_at"))
        if not code or not entry_date:
            continue
        side = str(item.get("side") or "").upper()
        status = str(item.get("status") or "").lower()
        key = (code, entry_date)
        if side == "BUY" or status == "paper_submitted":
            current = buys.get(key)
            if current is None or str(item.get("created_at") or "") < str(current.get("created_at") or ""):
                buys[key] = item
        elif side == "SELL" or "exit" in status or "sell" in status:
            sells.setdefault(key, []).append(item)

    rows: list[dict[str, Any]] = []
    for key, buy in buys.items():
        code, entry_date = key
        sell_rows = sorted(sells.get(key) or [], key=lambda row: str(row.get("created_at") or row.get("exit_datetime") or ""))
        entry_price = pd.to_numeric(pd.Series([buy.get("execution_price") or buy.get("entry_price")]), errors="coerce").iloc[0]
        buy_qty = pd.to_numeric(pd.Series([buy.get("quantity")]), errors="coerce").iloc[0]
        sold_qty = 0.0
        sell_notional = 0.0
        exit_candidates: list[str] = []
        exit_reasons: list[str] = []
        for sell in sell_rows:
            qty = abs(float(pd.to_numeric(pd.Series([sell.get("quantity")]), errors="coerce").fillna(0).iloc[0] or 0))
            price = pd.to_numeric(pd.Series([sell.get("execution_price") or sell.get("exit_price")]), errors="coerce").iloc[0]
            if qty > 0 and not pd.isna(price):
                sold_qty += qty
                sell_notional += qty * float(price)
            raw_exit = str(sell.get("exit_datetime") or sell.get("exit_date") or "").strip()
            if raw_exit and _date_text(raw_exit) >= entry_date:
                exit_candidates.append(raw_exit)
            elif sell.get("created_at"):
                exit_candidates.append(str(sell.get("created_at")))
            if sell.get("exit_reason"):
                exit_reasons.append(str(sell.get("exit_reason")))
        buy_qty_value = float(buy_qty) if not pd.isna(buy_qty) else 0.0
        is_closed = bool(sell_rows) and (sold_qty >= max(buy_qty_value * 0.5, 1.0) or any(float(pd.to_numeric(pd.Series([s.get("remaining_position_pct_after")]), errors="coerce").fillna(1).iloc[0]) <= 0 for s in sell_rows))
        exit_dt = max(exit_candidates) if exit_candidates and is_closed else ""
        net_ret = pd.NA
        realized_pnl = pd.NA
        if sold_qty > 0 and not pd.isna(entry_price) and float(entry_price) > 0:
            cost = sold_qty * float(entry_price)
            net_ret = sell_notional / cost - 1.0
            realized_pnl = sell_notional - cost
        rows.append(
            {
                "engine": "g3_state_alpha_paper_execution",
                "trade_key": f"paper_execution|{entry_date}|{code}",
                "code": code,
                "name": buy.get("name") or code,
                "entry_date": entry_date,
                "entry_datetime": buy.get("planned_entry_ts") or buy.get("created_at") or entry_date,
                "entry_ts": buy.get("planned_entry_ts") or buy.get("created_at") or entry_date,
                "policy_exit_date": _date_text(exit_dt),
                "exit_date": _date_text(exit_dt),
                "exit_datetime": exit_dt,
                "exit_ts": exit_dt,
                "net_ret": net_ret,
                "entry_price": entry_price,
                "score": buy.get("wave_style_score") or buy.get("score"),
                "raw_score": buy.get("score"),
                "score_source": "paper_execution",
                "score_scale": "runtime_shadow_score",
                "route": buy.get("route") or "paper_execution",
                "mode": buy.get("route") or "paper_execution",
                "route_label": buy.get("route_label") or buy.get("route") or "paper execution",
                "trade_strategy": buy.get("trade_strategy") or ("institutional_score120_mainwave" if buy.get("route") == "institutional_mainwave" else buy.get("route")),
                "trade_strategy_label": buy.get("trade_strategy_label") or buy.get("route_label") or buy.get("route"),
                "exit_reason": ",".join(dict.fromkeys(exit_reasons)),
                "decision_date": _date_text(buy.get("decision_date")),
                "confirm_datetime": buy.get("confirm_datetime") or "",
                "stake": buy.get("notional"),
                "exit_value": sell_notional if sold_qty > 0 else pd.NA,
                "realized_pnl": realized_pnl,
                "account_ret": net_ret,
                "contract": buy.get("contract") or "",
                "strategy_profile": buy.get("strategy_id") or "",
                "strategy_profile_name": buy.get("contract") or "",
                "trade_status": "closed" if is_closed else "open_shadow",
                "source_type": "paper_execution_reconstructed_trade",
                "position_slots": 2,
                "slot_pct": buy.get("position_pct"),
                "position_pct": buy.get("position_pct"),
                "reference_close": buy.get("execution_price"),
                "structure_stop": buy.get("structure_stop"),
                "hard_stop": buy.get("hard_stop"),
                "take_profit_1": buy.get("take_profit_1"),
                "exit_contract": buy.get("exit_contract"),
                "live_ready": True,
                "formal_buy_signal": bool(buy.get("formal_buy_signal")),
                "auto_order_allowed": bool(buy.get("auto_order_allowed")),
                "order_path_enabled": bool(buy.get("order_path_enabled")),
                "shadow_action": "paper_execution_reconstructed",
                "candidate_key": buy.get("ticket_key") or f"paper_execution|{entry_date}|{code}",
                "paper_buy_execution_id": buy.get("execution_id"),
                "paper_sell_execution_ids": ",".join(str(s.get("execution_id") or "") for s in sell_rows),
                "paper_buy_quantity": buy_qty,
                "paper_sold_quantity": sold_qty,
            }
        )
    return pd.DataFrame(rows)


def _open_position_rows() -> pd.DataFrame:
    frames = [
        _broker_open_rows(),
        _open_shadow_rows_from_ledger(),
        _open_shadow_rows_from_tickets(LATEST_TICKETS_PATH, "latest_shadow_ticket_open"),
        _open_shadow_rows_from_tickets(AFTERHOURS_TICKETS_PATH, "afterhours_shadow_ticket_open"),
    ]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    if "code" in out.columns:
        out["code"] = out["code"].map(_normalize_code)
    if "trade_key" in out.columns:
        out = out.drop_duplicates(subset=["trade_key"], keep="first")
    elif "code" in out.columns:
        out = out.drop_duplicates(subset=["code"], keep="first")
    return out.reset_index(drop=True)


def _align_and_concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    columns: list[str] = []
    seen: set[str] = set()
    for frame in frames:
        for col in frame.columns:
            if col not in seen:
                seen.add(col)
                columns.append(col)
    return pd.concat([frame.reindex(columns=columns) for frame in frames], ignore_index=True, sort=False)


def build() -> dict[str, Any]:
    base = _read_csv(BASE_CLOSED_TRADES_PATH)
    replay = _read_csv(INSTITUTIONAL_REPLAY_PATH)
    paper_rows = _paper_execution_rows()
    open_rows = _open_position_rows()

    base_rows = int(len(base))
    removed_institutional_rows = 0
    if not base.empty and not replay.empty:
        institutional_mask = _is_institutional_mainwave(base)
        removed_institutional_rows = int(institutional_mask.sum())
        base = base[~institutional_mask].copy()

    if not replay.empty:
        replay = replay.copy()
        replay["trade_status"] = "closed"
        replay["source_type"] = "institutional_mainwave_topic_replay_closed_trade"

    integrated = _align_and_concat([base, replay, paper_rows, open_rows])
    if not integrated.empty:
        if "code" in integrated.columns:
            integrated["code"] = integrated["code"].map(_normalize_code)
        if "trade_status" not in integrated.columns:
            integrated["trade_status"] = "closed"
        integrated["trade_status"] = integrated["trade_status"].fillna("closed")
        if "trade_key" in integrated.columns:
            integrated = integrated.drop_duplicates(subset=["trade_key"], keep="last")
        sort_cols = [col for col in ["entry_date", "code"] if col in integrated.columns]
        if sort_cols:
            integrated = integrated.sort_values(sort_cols, ascending=[False] + [True] * (len(sort_cols) - 1), kind="mergesort")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    integrated.to_csv(OUT_TRADES_PATH, index=False, encoding="utf-8-sig")
    open_rows.to_csv(OUT_OPEN_PATH, index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "base_closed_trades_path": str(BASE_CLOSED_TRADES_PATH),
        "institutional_replay_path": str(INSTITUTIONAL_REPLAY_PATH),
        "output_path": str(OUT_TRADES_PATH),
        "base_rows": base_rows,
        "removed_institutional_rows": removed_institutional_rows,
        "replayed_institutional_rows": int(len(replay)),
        "paper_execution_rows": int(len(paper_rows)),
        "open_position_rows": int(len(open_rows)),
        "final_rows": int(len(integrated)),
        "trade_status_counts": integrated.get("trade_status", pd.Series(dtype=object)).fillna("").astype(str).value_counts().to_dict() if not integrated.empty else {},
        "source_type_counts": integrated.get("source_type", pd.Series(dtype=object)).fillna("").astype(str).value_counts().to_dict() if not integrated.empty else {},
        "artifacts": {
            "open_positions_included": str(OUT_OPEN_PATH),
            "broker_state": str(BROKER_STATE_PATH),
            "shadow_ledger": str(SHADOW_LEDGER_PATH),
            "latest_shadow_tickets": str(LATEST_TICKETS_PATH),
            "afterhours_shadow_tickets": str(AFTERHOURS_TICKETS_PATH),
            "paper_executions": str(PAPER_EXECUTIONS_PATH),
        },
    }
    OUT_SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    summary = build()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
