from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, runtime_path  # noqa: E402


FINAL_G3_BACKTEST_DIR = report_path("g2_g3_market_style_router_v1")
OUT_DIR = report_path("gen3_hard_gate_daily_closure_audit_v1")
CONTRACT = "g3_final_with_g2_gap_supplement"
PREPARED_PATH = FINAL_G3_BACKTEST_DIR / f"{CONTRACT}_selected_candidates.csv"
BASE_CLOSED_PATH = FINAL_G3_BACKTEST_DIR / f"{CONTRACT}_closed_trades.csv"
BASE_CURVE_PATH = FINAL_G3_BACKTEST_DIR / f"{CONTRACT}_equity_curve.csv"
STATE_ALPHA_RUNTIME_DIR = runtime_path("gen3_state_alpha")

INITIAL_CAPITAL = 1_000_000.0
SLOT_COUNT = 2
SLOT_PCT = 0.50


@dataclass
class RiskState:
    action: str = "normal"
    reason: str = "account_risk_normal"
    current_drawdown: float = 0.0
    consecutive_realized_loss: float = 0.0
    recent_hard_stop_count: int = 0
    closed_count: int = 0


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Path):
        return str(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:.1%}"


def _money(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:,.0f}"


def _date(value: Any) -> pd.Timestamp:
    return pd.to_datetime(value, errors="coerce").normalize()


def _load_prepared() -> pd.DataFrame:
    d = _read_csv(BASE_CLOSED_PATH)
    if d.empty:
        raise FileNotFoundError(f"missing base closed trades: {BASE_CLOSED_PATH}")
    for col in ["entry_date", "policy_exit_date", "decision_date", "context_date"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in ["score", "net_ret", "policy_net_ret", "entry_price", "hard_stop_pct", "take_profit_pct"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["entry_date", "policy_exit_date", "net_ret", "entry_price"]).copy()
    d["trade_key"] = d.get("trade_key", d["entry_date"].dt.strftime("%Y-%m-%d") + "|" + d["code"].astype(str)).astype(str)
    return d.sort_values(["entry_date", "score", "code"], ascending=[True, False, True]).reset_index(drop=True)


def _calendar(trades: pd.DataFrame) -> list[pd.Timestamp]:
    curve = _read_csv(BASE_CURVE_PATH)
    if not curve.empty and "date" in curve.columns:
        dates = pd.to_datetime(curve["date"], errors="coerce").dropna().dt.normalize().drop_duplicates().sort_values()
        if len(dates):
            return list(dates)
    start = trades["entry_date"].min()
    end = trades["policy_exit_date"].max()
    return list(pd.bdate_range(start=start, end=end))


def _candidate_data_gate(row: dict[str, Any]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    for col in ["entry_date", "decision_date", "policy_exit_date", "code", "entry_price", "net_ret", "exit_reason"]:
        value = row.get(col)
        if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
            issues.append(f"missing_{col}")
    if float(row.get("entry_price") or 0) <= 0:
        issues.append("invalid_entry_price")
    return not issues, issues


def _candidate_30m_gate(row: dict[str, Any]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    route = str(row.get("route") or row.get("mode") or "")
    exit_legs = str(row.get("exit_legs") or "")
    exit_reason = str(row.get("exit_reason") or "")
    source_path = str(row.get("source_path") or "").lower()
    if route in {"score120_core", "institutional_mainwave"} or "institutional" in route:
        chain = str(row.get("chain") or "")
        if "30m" not in chain and "30m" not in source_path and "30m" not in exit_legs and "30m" not in exit_reason:
            issues.append("missing_historical_30m_evidence")
    if "hard_stop_30m" in exit_reason or "take_profit_partial_30m" in exit_reason or "prev_low_break_30m" in exit_reason:
        if exit_legs and "dt" not in exit_legs:
            issues.append("missing_30m_exit_timestamp")
    return not issues, issues


def _risk_state(closed: list[dict[str, Any]]) -> RiskState:
    if not closed:
        return RiskState(reason="no_closed_history")
    d = pd.DataFrame(closed).copy()
    d["account_ret"] = pd.to_numeric(d.get("account_ret"), errors="coerce").fillna(0.0)
    eq = (1.0 + d["account_ret"]).cumprod()
    peak = eq.cummax()
    dd = eq / peak - 1.0
    current_dd = float(dd.iloc[-1]) if len(dd) else 0.0
    consecutive = 0.0
    for value in reversed(d["account_ret"].tolist()):
        if value < 0:
            consecutive += abs(float(value))
        else:
            break
    recent = d.tail(20)
    recent_reason = recent.get("exit_reason", pd.Series("", index=recent.index)).fillna("").astype(str).str.lower()
    recent_hard_stop = int(recent_reason.str.contains("hard_stop", regex=False).sum())
    action = "normal"
    reason = "account_risk_normal"
    if current_dd <= -0.18:
        action = "pause_new_buy"
        reason = "mtm_drawdown_pause_new_buy"
    elif consecutive >= 0.20:
        action = "pause_new_buy"
        reason = "consecutive_realized_loss_pause_new_buy"
    elif recent_hard_stop >= 2:
        action = "pause_new_buy"
        reason = "hard_stop_cooldown_pause_new_buy"
    elif current_dd <= -0.15:
        action = "reduce_risk"
        reason = "mtm_drawdown_reduce_risk"
    return RiskState(
        action=action,
        reason=reason,
        current_drawdown=current_dd,
        consecutive_realized_loss=consecutive,
        recent_hard_stop_count=recent_hard_stop,
        closed_count=int(len(d)),
    )


def _simulate(
    trades: pd.DataFrame,
    calendar: list[pd.Timestamp],
    account_gate_mode: str = "none",
    cooldown_days: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in trades.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    gate_rows: list[dict[str, Any]] = []
    cooldown_remaining = 0
    last_trigger_closed_count = 0
    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict[str, Any]] = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                pnl = exit_value - float(pos["stake"])
                cash += exit_value
                realized_pnl += pnl
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                entry_equity = float(out.get("entry_equity") or INITIAL_CAPITAL)
                out["account_ret"] = pnl / entry_equity if entry_equity > 0 else 0.0
                out["account_loss_pct"] = min(0.0, out["account_ret"])
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open
        risk = _risk_state(closed)
        account_block = False
        account_reason = risk.reason
        if account_gate_mode == "current_runtime":
            account_block = risk.action == "pause_new_buy"
        elif account_gate_mode == "cooldown":
            if cooldown_remaining > 0:
                account_block = True
                account_reason = f"cooldown_{cooldown_days}d_after_{account_reason}"
                cooldown_remaining -= 1
            elif risk.action == "pause_new_buy" and risk.closed_count > last_trigger_closed_count:
                account_block = True
                account_reason = f"cooldown_{cooldown_days}d_after_{risk.reason}"
                cooldown_remaining = max(0, cooldown_days - 1)
                last_trigger_closed_count = risk.closed_count
        opened = 0
        blocked = 0
        blocked_keys: list[str] = []
        blocked_ret_sum = 0.0
        todays = by_entry.get(day)
        if todays is not None and not todays.empty:
            for row in todays.sort_values(["score", "code"], ascending=[False, True]).to_dict("records"):
                data_ok, data_issues = _candidate_data_gate(row)
                m30_ok, m30_issues = _candidate_30m_gate(row)
                block_reason = ""
                if not data_ok:
                    block_reason = ";".join(data_issues)
                elif not m30_ok:
                    block_reason = ";".join(m30_issues)
                elif account_block:
                    block_reason = account_reason
                elif opened >= SLOT_COUNT:
                    block_reason = "daily_open_limit"
                elif len(open_pos) >= SLOT_COUNT:
                    block_reason = "slot_full"
                if block_reason:
                    blocked += 1
                    blocked_keys.append(str(row.get("trade_key") or ""))
                    blocked_ret_sum += float(row.get("net_ret") or 0.0)
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * SLOT_PCT
                if stake <= 0 or cash < stake:
                    blocked += 1
                    blocked_keys.append(str(row.get("trade_key") or ""))
                    blocked_ret_sum += float(row.get("net_ret") or 0.0)
                    continue
                pos = row.copy()
                pos["stake"] = stake
                pos["entry_equity"] = equity_before
                pos["contract"] = CONTRACT
                cash -= stake
                open_pos.append(pos)
                opened += 1
        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized_pnl,
                "account_risk_action": risk.action,
                "account_risk_reason": account_reason if account_block else risk.reason,
                "account_gate_blocking": account_block,
            }
        )
        gate_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "candidate_count": int(0 if todays is None else len(todays)),
                "opened": opened,
                "blocked_count": blocked,
                "blocked_trade_keys": "|".join([x for x in blocked_keys if x]),
                "blocked_candidate_ret_sum": blocked_ret_sum,
                "account_risk_action": risk.action,
                "account_risk_reason": account_reason if account_block else risk.reason,
                "account_gate_blocking": account_block,
                "cooldown_remaining": cooldown_remaining,
                "current_drawdown": risk.current_drawdown,
                "consecutive_realized_loss": risk.consecutive_realized_loss,
                "recent_hard_stop_count": risk.recent_hard_stop_count,
                "closed_count": risk.closed_count,
            }
        )
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed), pd.DataFrame(gate_rows)


def _summary(curve: pd.DataFrame, closed: pd.DataFrame) -> dict[str, Any]:
    if curve.empty:
        return {"trades": 0, "return": 0.0, "max_drawdown": 0.0}
    ret = float(curve.iloc[-1]["equity"] / INITIAL_CAPITAL - 1.0)
    dd = float(pd.to_numeric(curve["drawdown"], errors="coerce").min())
    win_rate = float((pd.to_numeric(closed.get("net_ret"), errors="coerce") > 0).mean()) if not closed.empty else 0.0
    worst_trade = float(pd.to_numeric(closed.get("net_ret"), errors="coerce").min()) if not closed.empty else 0.0
    return {
        "trades": int(len(closed)),
        "return": ret,
        "max_drawdown": dd,
        "win_rate": win_rate,
        "worst_trade": worst_trade,
        "sum_pnl": float(pd.to_numeric(closed.get("realized_pnl"), errors="coerce").fillna(0).sum()) if not closed.empty else 0.0,
    }


def _candidate_gate_audit(prepared: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in prepared.to_dict("records"):
        data_ok, data_issues = _candidate_data_gate(row)
        m30_ok, m30_issues = _candidate_30m_gate(row)
        rows.append(
            {
                "entry_date": pd.Timestamp(row["entry_date"]).strftime("%Y-%m-%d"),
                "code": row.get("code"),
                "name": row.get("name"),
                "route": row.get("route") or row.get("mode"),
                "trade_key": row.get("trade_key"),
                "net_ret": row.get("net_ret"),
                "data_gate_ok": data_ok,
                "data_gate_issues": ";".join(data_issues),
                "m30_gate_proxy_ok": m30_ok,
                "m30_gate_proxy_issues": ";".join(m30_issues),
            }
        )
    return pd.DataFrame(rows)


def _write_report(payload: dict[str, Any]) -> None:
    base = payload["base_no_runtime_account_gate"]
    account = payload["with_account_pause_gate"]
    delta = payload["delta"]
    lines = [
        "# G3 Hard Gate Daily Closure Audit v1",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Contract: `{CONTRACT}`",
        f"- Scope: 2 slots / 50% per slot / 12% hard stop / 12% half take-profit / previous-low protection",
        "",
        "## 核心结论",
        "",
        f"- 无账户暂停 gate 历史收益: **{_pct(base['return'])}**，成交 {base['trades']} 笔，最大回撤 {_pct(base['max_drawdown'])}。",
        f"- 加入账户暂停 hard gate 后收益: **{_pct(account['return'])}**，成交 {account['trades']} 笔，最大回撤 {_pct(account['max_drawdown'])}。",
        f"- 收益差: **{_pct(delta['return_delta'])}**，少成交 {delta['trade_delta']} 笔。",
        "",
        "## Gate 作用",
        "",
        "| Gate | 类型 | 历史验证状态 | 结果 |",
        "|---|---|---|---|",
    ]
    for row in payload["gate_effects"]:
        lines.append(f"| {row['gate']} | {row['type']} | {row['audit_scope']} | {row['result']} |")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- `route_health` 不在本审计的硬 gate 中，只作为观察指标。",
            "- 数据 gate 与 30m gate 在历史中只能用候选文件和 30m 出场证据做代理验证；完整 live 字段仍需要每日 shadow/smoke 沉淀。",
            "- 纸面台账与正式下单锁是实时运行 gate，不应改变历史收益，但必须在每日闭环报告里显示是否阻断。",
        ]
    )
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def _write_report(payload: dict[str, Any]) -> None:
    base = payload["base_no_runtime_account_gate"]
    account = payload["with_current_account_pause_gate"]
    delta = payload["current_account_gate_delta"]
    lines = [
        "# G3 Hard Gate Daily Closure Audit v1",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Contract: `{CONTRACT}`",
        "- Scope: 2 slots / 50% per slot / 12% hard stop / 12% half take-profit / previous-low protection",
        "",
        "## Core Conclusion",
        "",
        f"- No account pause gate return: **{_pct(base['return'])}**, trades {base['trades']}, max drawdown {_pct(base['max_drawdown'])}.",
        f"- Current account pause hard gate return: **{_pct(account['return'])}**, trades {account['trades']}, max drawdown {_pct(account['max_drawdown'])}.",
        f"- Return delta: **{_pct(delta['return_delta'])}**, trade delta {delta['trade_delta']}.",
        "",
        "## Account Gate Variants",
        "",
        "| Variant | Return | Drawdown | Trades | Missed Trades | Net Missed PnL |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in payload["account_gate_variants"]:
        lines.append(
            f"| {row['label']} | {_pct(row['return'])} | {_pct(row['max_drawdown'])} | {row['trades']} | {row['missed_trade_count']} | {_money(row['net_missed_pnl'])} |"
        )
    lines.extend(
        [
            "",
            "## Gate Effects",
            "",
            "| Gate | Type | Audit Scope | Result |",
            "|---|---|---|---|",
        ]
    )
    for row in payload["gate_effects"]:
        lines.append(f"| {row['gate']} | {row['type']} | {row['audit_scope']} | {row['result']} |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `route_health` is observation-only and is not included as a hard gate.",
            "- Data gate and 30m gate are historical proxy checks here; full live fields still need daily shadow/smoke accumulation.",
            "- Paper reproducibility and formal order lock are runtime gates. They should be visible in daily closure, but they are not historical alpha filters.",
        ]
    )
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prepared = _load_prepared()
    calendar = _calendar(prepared)
    candidate_gates = _candidate_gate_audit(prepared)
    executable = prepared.copy()
    gate_map = candidate_gates.set_index("trade_key")
    executable["_data_gate_ok"] = executable["trade_key"].map(gate_map["data_gate_ok"]).fillna(False).astype(bool)
    executable["_m30_gate_ok"] = executable["trade_key"].map(gate_map["m30_gate_proxy_ok"]).fillna(False).astype(bool)
    executable = executable[executable["_data_gate_ok"] & executable["_m30_gate_ok"]].copy()

    base_curve, base_closed, base_daily = _simulate(executable, calendar, account_gate_mode="none")
    account_curve, account_closed, account_daily = _simulate(executable, calendar, account_gate_mode="current_runtime")
    base_summary = _summary(base_curve, base_closed)
    account_summary = _summary(account_curve, account_closed)
    missed_keys = sorted(set(base_closed.get("trade_key", pd.Series(dtype=str)).astype(str)) - set(account_closed.get("trade_key", pd.Series(dtype=str)).astype(str)))
    missed = base_closed[base_closed.get("trade_key", pd.Series(dtype=str)).astype(str).isin(missed_keys)].copy() if missed_keys else pd.DataFrame()
    avoided_loss = float(pd.to_numeric(missed.loc[pd.to_numeric(missed.get("realized_pnl"), errors="coerce") < 0, "realized_pnl"], errors="coerce").fillna(0).sum()) if not missed.empty else 0.0
    missed_profit = float(pd.to_numeric(missed.loc[pd.to_numeric(missed.get("realized_pnl"), errors="coerce") > 0, "realized_pnl"], errors="coerce").fillna(0).sum()) if not missed.empty else 0.0

    data_fail = candidate_gates[~candidate_gates["data_gate_ok"]]
    m30_fail = candidate_gates[~candidate_gates["m30_gate_proxy_ok"]]
    pause_days = account_daily[account_daily["account_risk_action"].eq("pause_new_buy")]
    reduce_days = account_daily[account_daily["account_risk_action"].eq("reduce_risk")]
    variant_rows = [
        {
            "label": "no_account_pause_gate",
            **base_summary,
            "missed_trade_count": 0,
            "net_missed_pnl": 0.0,
        },
        {
            "label": "current_runtime_pause_gate",
            **account_summary,
            "missed_trade_count": int(len(missed)),
            "net_missed_pnl": float(pd.to_numeric(missed.get("realized_pnl"), errors="coerce").fillna(0).sum()) if not missed.empty else 0.0,
        },
    ]
    cooldown_outputs: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    for days in [5, 10, 20]:
        c_curve, c_closed, c_daily = _simulate(executable, calendar, account_gate_mode="cooldown", cooldown_days=days)
        c_summary = _summary(c_curve, c_closed)
        c_missed_keys = sorted(set(base_closed.get("trade_key", pd.Series(dtype=str)).astype(str)) - set(c_closed.get("trade_key", pd.Series(dtype=str)).astype(str)))
        c_missed = base_closed[base_closed.get("trade_key", pd.Series(dtype=str)).astype(str).isin(c_missed_keys)].copy() if c_missed_keys else pd.DataFrame()
        variant_rows.append(
            {
                "label": f"account_pause_cooldown_{days}d",
                **c_summary,
                "missed_trade_count": int(len(c_missed)),
                "net_missed_pnl": float(pd.to_numeric(c_missed.get("realized_pnl"), errors="coerce").fillna(0).sum()) if not c_missed.empty else 0.0,
            }
        )
        cooldown_outputs[f"{days}d"] = (c_curve, c_closed, c_daily)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "contract": CONTRACT,
        "source": {
            "prepared_candidates": str(PREPARED_PATH),
            "base_closed": str(BASE_CLOSED_PATH),
            "simulation_input": str(BASE_CLOSED_PATH),
            "runtime_dir": str(STATE_ALPHA_RUNTIME_DIR),
            "candidate_rows": int(len(prepared)),
            "calendar_days": int(len(calendar)),
        },
        "base_no_runtime_account_gate": base_summary,
        "with_current_account_pause_gate": account_summary,
        "current_account_gate_delta": {
            "return_delta": account_summary["return"] - base_summary["return"],
            "trade_delta": account_summary["trades"] - base_summary["trades"],
            "max_drawdown_delta": account_summary["max_drawdown"] - base_summary["max_drawdown"],
            "missed_trade_count": int(len(missed)),
            "avoided_loss_pnl": avoided_loss,
            "missed_profit_pnl": missed_profit,
            "net_missed_pnl": float(pd.to_numeric(missed.get("realized_pnl"), errors="coerce").fillna(0).sum()) if not missed.empty else 0.0,
        },
        "account_gate_variants": variant_rows,
        "gate_effects": [
            {
                "gate": "data_integrity",
                "type": "hard",
                "audit_scope": "historical_proxy",
                "result": f"{len(data_fail)} / {len(candidate_gates)} candidates blocked by missing historical fields",
            },
            {
                "gate": "m30_confirmation",
                "type": "hard",
                "audit_scope": "historical_proxy",
                "result": f"{len(m30_fail)} / {len(candidate_gates)} candidates lack proxy 30m evidence",
            },
            {
                "gate": "account_pause_new_buy",
                "type": "hard",
                "audit_scope": "historical_replay",
                "result": f"{len(pause_days)} current-runtime pause days, {len(reduce_days)} reduce-risk observation days, missed {len(missed)} trades",
            },
            {
                "gate": "paper_reproducibility",
                "type": "hard_runtime",
                "audit_scope": "live_daily_only",
                "result": "tracked by pretrade smoke/current paper audit; not a historical alpha filter",
            },
            {
                "gate": "formal_order_lock",
                "type": "hard_runtime",
                "audit_scope": "live_daily_only",
                "result": "must stay locked until manual promotion; not a historical alpha filter",
            },
        ],
    }
    candidate_gates.to_csv(OUT_DIR / "candidate_gate_audit.csv", index=False, encoding="utf-8-sig")
    base_daily.to_csv(OUT_DIR / "daily_no_account_gate.csv", index=False, encoding="utf-8-sig")
    account_daily.to_csv(OUT_DIR / "daily_with_account_gate.csv", index=False, encoding="utf-8-sig")
    base_closed.to_csv(OUT_DIR / "closed_no_account_gate.csv", index=False, encoding="utf-8-sig")
    account_closed.to_csv(OUT_DIR / "closed_with_account_gate.csv", index=False, encoding="utf-8-sig")
    missed.to_csv(OUT_DIR / "missed_by_account_gate.csv", index=False, encoding="utf-8-sig")
    for key, (_curve, closed, daily) in cooldown_outputs.items():
        daily.to_csv(OUT_DIR / f"daily_account_cooldown_{key}.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"closed_account_cooldown_{key}.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(payload)
    return payload


def main() -> None:
    payload = run()
    print(
        json.dumps(
            {
                "status": "completed",
                "base_return": payload["base_no_runtime_account_gate"]["return"],
                "account_gate_return": payload["with_current_account_pause_gate"]["return"],
                "return_delta": payload["current_account_gate_delta"]["return_delta"],
                "missed_trade_count": payload["current_account_gate_delta"]["missed_trade_count"],
                "account_gate_variants": payload["account_gate_variants"],
                "out_dir": str(OUT_DIR),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
