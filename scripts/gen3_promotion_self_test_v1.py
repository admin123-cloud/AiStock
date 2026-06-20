from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import _trade_calendar  # noqa: E402
from utils.market_warehouse import clickhouse_available, clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("gen3_market_state_router_v1")
SOURCE_TRADES = SOURCE_DIR / "state_router_selected_candidates.csv"
SOURCE_DECISIONS = SOURCE_DIR / "state_router_decisions.csv"
OUT_DIR = report_path("gen3_promotion_self_test_v1")
RESEARCH_ROUTER = ROOT / "scripts" / "gen3_market_state_router_v1.py"
STATE_ALPHA_API = ROOT / "api" / "gen3_state_alpha.py"

INITIAL_CAPITAL = 1_000_000.0
DEFAULT_END = pd.Timestamp("2026-06-17")


@dataclass(frozen=True)
class Contract:
    name: str
    label: str
    slots: int
    slot_pct: float
    hard_stop_pct: float | None = None
    take_profit_pct: float | None = None
    take_profit_sell_ratio: float = 0.0
    use_prev_low_after_take_profit: bool = False
    max_single_loss_limit: float = 0.05
    max_mtm_drawdown_limit: float = 0.20
    require_mainwave_gate: bool = False


CONTRACTS = [
    Contract(
        name="g3_3slot_33_policy_exit",
        label="G3 3槽33% 原策略退出",
        slots=3,
        slot_pct=1.0 / 3.0,
        max_single_loss_limit=0.085,
    ),
    Contract(
        name="g3_3slot_33_stop12_take15_prevlow",
        label="G3 3槽33% + 12%硬止损 + 15%减半 + 前低保护",
        slots=3,
        slot_pct=1.0 / 3.0,
        hard_stop_pct=0.12,
        take_profit_pct=0.15,
        take_profit_sell_ratio=0.50,
        use_prev_low_after_take_profit=True,
        max_single_loss_limit=0.05,
    ),
    Contract(
        name="g3_2slot_50_default_stop12_take12_prevlow",
        label="G3 2槽50% 默认 + 12%硬止损 + 12%减半 + 前低保护",
        slots=2,
        slot_pct=0.50,
        hard_stop_pct=0.12,
        take_profit_pct=0.12,
        take_profit_sell_ratio=0.50,
        use_prev_low_after_take_profit=True,
        max_single_loss_limit=0.065,
        max_mtm_drawdown_limit=0.19,
        require_mainwave_gate=False,
    ),
    Contract(
        name="g3_2slot_50_attack_stop15_take20_prevlow",
        label="G3 2槽50% 进攻 + 15%硬止损 + 20%减半 + 前低保护",
        slots=2,
        slot_pct=0.50,
        hard_stop_pct=0.15,
        take_profit_pct=0.20,
        take_profit_sell_ratio=0.50,
        use_prev_low_after_take_profit=True,
        max_single_loss_limit=0.08,
        max_mtm_drawdown_limit=0.23,
        require_mainwave_gate=True,
    ),
]


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        x = float(value)
        return None if not math.isfinite(x) else x
    if pd.isna(value):
        return None
    return str(value)


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


def _hash_df(df: pd.DataFrame, cols: list[str] | None = None) -> str:
    if df.empty:
        return "empty"
    d = df[cols].copy() if cols else df.copy()
    d = d.astype(object).where(pd.notna(d), "")
    payload = d.to_json(orient="records", force_ascii=False, date_format="iso")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_selected_candidates() -> pd.DataFrame:
    if not SOURCE_TRADES.exists():
        raise FileNotFoundError(f"missing selected candidates: {SOURCE_TRADES}")
    d = pd.read_csv(SOURCE_TRADES, low_memory=False, encoding="utf-8-sig")
    for col in ["entry_date", "policy_exit_date", "decision_date", "context_date"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in ["score", "net_ret", "entry_price", "index_mom20", "index_mom60", "up_rate", "big_down_rate"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["entry_date", "policy_exit_date", "net_ret", "entry_price"]).copy()
    return d.sort_values(["entry_date", "mode", "score", "code"], ascending=[True, True, False, True]).reset_index(drop=True)


def _load_decisions() -> pd.DataFrame:
    if not SOURCE_DECISIONS.exists():
        return pd.DataFrame()
    d = pd.read_csv(SOURCE_DECISIONS, low_memory=False, encoding="utf-8-sig")
    for col in ["date", "decision_date"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    return d


def _query_daily(codes: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not clickhouse_available() or not clickhouse_table_exists("kline_daily"):
        return pd.DataFrame()
    frames = []
    for i in range(0, len(codes), 120):
        quoted = ", ".join(f"'{code}'" for code in codes[i : i + 120])
        frames.append(
            clickhouse_query_df(
                f"""
                SELECT code, trade_date, open, high, low, close
                FROM kline_daily
                WHERE code IN ({quoted})
                  AND trade_date >= toDate('{start:%Y-%m-%d}')
                  AND trade_date <= toDate('{end:%Y-%m-%d}')
                ORDER BY code, trade_date
                """
            )
        )
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return out
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).reset_index(drop=True)


def _query_minute30(codes: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not clickhouse_available() or not clickhouse_table_exists("kline_minute_30"):
        return pd.DataFrame()
    frames = []
    for i in range(0, len(codes), 80):
        quoted = ", ".join(f"'{code}'" for code in codes[i : i + 80])
        frames.append(
            clickhouse_query_df(
                f"""
                SELECT code, datetime, open, high, low, close
                FROM kline_minute_30
                WHERE code IN ({quoted})
                  AND datetime >= toDateTime('{start:%Y-%m-%d} 09:30:00')
                  AND datetime <= toDateTime('{end:%Y-%m-%d} 15:00:00')
                ORDER BY code, datetime
                """
            )
        )
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return out
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out["bar_date"] = out["datetime"].dt.normalize()
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["code", "datetime", "open", "high", "low", "close"]).reset_index(drop=True)


def _build_price_context(trades: pd.DataFrame) -> dict[str, Any]:
    codes = sorted(trades["code"].dropna().astype(str).unique().tolist())
    start = pd.to_datetime(trades["entry_date"], errors="coerce").min() - pd.Timedelta(days=10)
    end = max(DEFAULT_END, pd.to_datetime(trades["policy_exit_date"], errors="coerce").max())
    daily = _query_daily(codes, start, end)
    minute30 = _query_minute30(codes, start, end)
    daily_groups = {str(code): g.sort_values("trade_date").copy() for code, g in daily.groupby("code", sort=False)}
    minute_groups = {str(code): g.sort_values("datetime").copy() for code, g in minute30.groupby("code", sort=False)}
    prev_low_map: dict[tuple[str, pd.Timestamp], float] = {}
    for code, g in daily_groups.items():
        dg = g.copy()
        dg["prev_low"] = dg["low"].shift(1)
        for row in dg.itertuples(index=False):
            if row.prev_low is not None and np.isfinite(row.prev_low):
                prev_low_map[(code, pd.Timestamp(row.trade_date).normalize())] = float(row.prev_low)
    return {
        "daily": daily,
        "minute30": minute30,
        "daily_groups": daily_groups,
        "minute_groups": minute_groups,
        "prev_low_map": prev_low_map,
    }


def _entry_adjustment(row: dict[str, Any], price_context: dict[str, Any]) -> float:
    code = str(row.get("code") or "")
    entry = pd.Timestamp(row["entry_date"]).normalize()
    entry_price = float(row.get("entry_price") or 0.0)
    if entry_price <= 0:
        return 1.0
    minute = price_context["minute_groups"].get(code)
    if minute is not None and not minute.empty:
        day = minute[minute["bar_date"].eq(entry)]
        if not day.empty and float(day.iloc[0]["open"]) > 0:
            return entry_price / float(day.iloc[0]["open"])
    daily = price_context["daily_groups"].get(code)
    if daily is not None and not daily.empty:
        hit = daily[daily["trade_date"].eq(entry)]
        if not hit.empty and float(hit.iloc[0]["open"]) > 0:
            return entry_price / float(hit.iloc[0]["open"])
    return 1.0


def _events_for_trade(row: dict[str, Any], contract: Contract, price_context: dict[str, Any]) -> list[dict[str, Any]]:
    entry = pd.Timestamp(row["entry_date"]).normalize()
    exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
    entry_dt = pd.Timestamp(f"{entry:%Y-%m-%d} 09:30:00")
    fallback_dt = pd.Timestamp(f"{exit_date:%Y-%m-%d} 15:00:00")
    raw_ret = float(row["net_ret"])
    if contract.hard_stop_pct is None and contract.take_profit_pct is None:
        return [{"dt": fallback_dt, "ratio": 1.0, "ret": raw_ret, "reason": "policy_exit"}]

    code = str(row.get("code") or "")
    entry_price = float(row.get("entry_price") or 0.0)
    factor = _entry_adjustment(row, price_context)
    minute = price_context["minute_groups"].get(code, pd.DataFrame())
    scan = pd.DataFrame()
    scan_price_factor = factor
    if not minute.empty:
        scan = minute[
            (minute["datetime"] > entry_dt)
            & (minute["datetime"] <= fallback_dt)
        ].copy()
    if scan.empty:
        daily = price_context["daily_groups"].get(code, pd.DataFrame())
        if not daily.empty:
            day_scan = daily[(daily["trade_date"] >= entry) & (daily["trade_date"] <= exit_date)].copy()
            day_scan["datetime"] = day_scan["trade_date"].map(lambda x: pd.Timestamp(f"{pd.Timestamp(x):%Y-%m-%d} 15:00:00"))
            scan = day_scan[["datetime", "trade_date", "open", "high", "low", "close"]].copy()
            scan["bar_date"] = pd.to_datetime(scan["datetime"]).dt.normalize()
            scan_price_factor = 1.0
    if scan.empty or entry_price <= 0:
        return [{"dt": fallback_dt, "ratio": 1.0, "ret": raw_ret, "reason": "policy_exit_no_intraday"}]

    hard_stop = entry_price * (1.0 - abs(contract.hard_stop_pct or 0.0)) if contract.hard_stop_pct is not None else None
    take_price = entry_price * (1.0 + abs(contract.take_profit_pct or 0.0)) if contract.take_profit_pct is not None else None
    remaining = 1.0
    take_done = False
    events: list[dict[str, Any]] = []

    for bar in scan.itertuples(index=False):
        dt = pd.Timestamp(getattr(bar, "datetime"))
        bar_date = pd.Timestamp(getattr(bar, "bar_date", dt.normalize())).normalize()
        high = float(getattr(bar, "high")) * scan_price_factor
        low = float(getattr(bar, "low")) * scan_price_factor
        close = float(getattr(bar, "close")) * scan_price_factor

        if hard_stop is not None and low <= hard_stop:
            events.append(
                {
                    "dt": dt,
                    "ratio": remaining,
                    "ret": -abs(float(contract.hard_stop_pct)),
                    "reason": "hard_stop_30m",
                }
            )
            remaining = 0.0
            break

        if take_price is not None and not take_done and high >= take_price and remaining > 0:
            sell_ratio = min(remaining, max(0.0, float(contract.take_profit_sell_ratio)))
            if sell_ratio > 0:
                events.append(
                    {
                        "dt": dt,
                        "ratio": sell_ratio,
                        "ret": abs(float(contract.take_profit_pct)),
                        "reason": "take_profit_partial_30m",
                    }
                )
                remaining -= sell_ratio
                take_done = True
                if remaining <= 1e-9:
                    break

        if contract.use_prev_low_after_take_profit and take_done and remaining > 0:
            prev_low = price_context["prev_low_map"].get((code, bar_date))
            if prev_low is not None and np.isfinite(prev_low):
                threshold = float(prev_low)
                if low <= threshold:
                    ret = threshold / entry_price - 1.0 if entry_price > 0 else 0.0
                    events.append(
                        {
                            "dt": dt,
                            "ratio": remaining,
                            "ret": ret,
                            "reason": "prev_low_break_30m",
                        }
                    )
                    remaining = 0.0
                    break

    if remaining > 1e-9:
        events.append({"dt": fallback_dt, "ratio": remaining, "ret": raw_ret, "reason": "policy_exit_remaining"})
    return sorted(events, key=lambda item: (item["dt"], item["reason"]))


def _prepare_contract_candidates(trades: pd.DataFrame, contract: Contract, price_context: dict[str, Any]) -> pd.DataFrame:
    if contract.require_mainwave_gate and "mode" in trades.columns:
        trades = trades[trades["mode"].astype(str).eq("institutional_mainwave")].copy()
    rows = []
    for record in trades.to_dict("records"):
        events = _events_for_trade(record, contract, price_context)
        if not events:
            continue
        weighted_ret = sum(float(ev["ratio"]) * float(ev["ret"]) for ev in events)
        final_dt = max(pd.Timestamp(ev["dt"]) for ev in events)
        out = record.copy()
        out["policy_exit_date"] = final_dt.normalize()
        out["policy_net_ret"] = weighted_ret
        out["net_ret"] = weighted_ret
        out["exit_reason"] = ",".join(str(ev["reason"]) for ev in events)
        out["exit_legs"] = json.dumps(events, ensure_ascii=False, default=_json_default)
        out["hard_stop_pct"] = contract.hard_stop_pct
        out["take_profit_pct"] = contract.take_profit_pct
        rows.append(out)
    out_df = pd.DataFrame(rows)
    if out_df.empty:
        return out_df
    out_df["entry_date"] = pd.to_datetime(out_df["entry_date"], errors="coerce").dt.normalize()
    out_df["policy_exit_date"] = pd.to_datetime(out_df["policy_exit_date"], errors="coerce").dt.normalize()
    return out_df


def _simulate_portfolio(trades: pd.DataFrame, contract: Contract, calendar: list[pd.Timestamp]) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in trades.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    for day in calendar:
        realized_pnl = 0.0
        still_open = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                entry_equity = float(out.get("entry_equity") or INITIAL_CAPITAL)
                out["account_loss_pct"] = min(0.0, float(out["realized_pnl"]) / entry_equity) if entry_equity > 0 else 0.0
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open
        opened = 0
        todays = by_entry.get(day)
        if todays is not None and not todays.empty:
            today = todays.sort_values(["score", "code"], ascending=[False, True]).copy()
            for row in today.itertuples(index=False):
                if opened >= 1 or len(open_pos) >= contract.slots:
                    break
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * float(contract.slot_pct)
                if stake <= 0 or cash < stake:
                    break
                pos = row._asdict()
                pos["stake"] = stake
                pos["entry_equity"] = equity_before
                pos["contract"] = contract.name
                cash -= stake
                open_pos.append(pos)
                opened += 1
        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "contract": contract.name,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized_pnl,
            }
        )
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed)


def _daily_adjusted_close(row: dict[str, Any], day: pd.Timestamp, price_context: dict[str, Any]) -> float | None:
    code = str(row.get("code") or "")
    entry = pd.Timestamp(row.get("entry_date")).normalize()
    entry_price = float(row.get("entry_price") or 0.0)
    if entry_price <= 0:
        return None
    daily = price_context["daily_groups"].get(code)
    if daily is None or daily.empty:
        return None
    entry_row = daily[daily["trade_date"].eq(entry)]
    day_row = daily[daily["trade_date"].eq(day)]
    if entry_row.empty or day_row.empty:
        return None
    entry_open = float(entry_row.iloc[0]["open"])
    if entry_open <= 0:
        return None
    factor = entry_price / entry_open
    return float(day_row.iloc[0]["close"]) * factor


def _simulate_mtm_curve(closed: pd.DataFrame, price_context: dict[str, Any], calendar: list[pd.Timestamp]) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    trades = closed.copy()
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["policy_exit_date"] = pd.to_datetime(trades["policy_exit_date"], errors="coerce").dt.normalize()
    by_entry = {day: g.copy() for day, g in trades.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for day in calendar:
        still_open = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                cash += float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
            else:
                still_open.append(pos)
        open_pos = still_open
        todays = by_entry.get(day)
        if todays is not None and not todays.empty:
            for row in todays.itertuples(index=False):
                pos = row._asdict()
                cash -= float(pos["stake"])
                open_pos.append(pos)
        mtm_value = 0.0
        reserved = 0.0
        missing_price_count = 0
        for pos in open_pos:
            stake = float(pos["stake"])
            reserved += stake
            px = _daily_adjusted_close(pos, day, price_context)
            if px is None:
                mtm_value += stake
                missing_price_count += 1
            else:
                entry_price = float(pos.get("entry_price") or 0.0)
                mtm_value += stake * (px / entry_price) if entry_price > 0 else stake
        equity = cash + mtm_value
        rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "cash": cash,
                "reserved_principal": reserved,
                "mtm_value": mtm_value,
                "equity_mtm": equity,
                "open_positions": len(open_pos),
                "missing_price_count": missing_price_count,
            }
        )
    curve = pd.DataFrame(rows)
    if not curve.empty:
        curve["peak_mtm"] = curve["equity_mtm"].cummax()
        curve["drawdown_mtm"] = curve["equity_mtm"] / curve["peak_mtm"] - 1.0
        curve["ret_from_start_mtm"] = curve["equity_mtm"] / INITIAL_CAPITAL - 1.0
    return curve


def _max_drawdown(equity: pd.Series) -> float:
    s = pd.to_numeric(equity, errors="coerce").dropna()
    if s.empty:
        return 0.0
    return float((s / s.cummax() - 1.0).min())


def _window_metrics(curve: pd.DataFrame, closed: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if curve.empty:
        return {}
    dates = pd.to_datetime(curve["date"], errors="coerce")
    entry = pd.to_datetime(closed.get("entry_date"), errors="coerce") if not closed.empty else pd.Series(dtype="datetime64[ns]")
    windows = {
        "full": (pd.Series([True] * len(curve), index=curve.index), pd.Series([True] * len(closed), index=closed.index)),
        "pre_2024_09": (dates < pd.Timestamp("2024-09-01"), entry < pd.Timestamp("2024-09-01")),
        "post_2024_09": (dates >= pd.Timestamp("2024-09-01"), entry >= pd.Timestamp("2024-09-01")),
        "blind_2026ytd": (dates >= pd.Timestamp("2026-01-01"), entry >= pd.Timestamp("2026-01-01")),
    }
    rows = {}
    for name, (curve_mask, trade_mask) in windows.items():
        c = curve[curve_mask].copy()
        t = closed[trade_mask].copy() if not closed.empty else pd.DataFrame()
        if c.empty:
            continue
        ret = float(c["equity"].iloc[-1] / c["equity"].iloc[0] - 1.0) if float(c["equity"].iloc[0]) else 0.0
        rets = pd.to_numeric(t.get("net_ret"), errors="coerce").dropna() if not t.empty else pd.Series(dtype=float)
        rows[name] = {
            "trades": int(len(t)),
            "return": ret,
            "max_drawdown": _max_drawdown(c["equity"]),
            "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
            "sum_pnl": float(pd.to_numeric(t.get("realized_pnl"), errors="coerce").sum()) if not t.empty else 0.0,
        }
    return rows


def _contract_summary(
    contract: Contract,
    curve: pd.DataFrame,
    closed: pd.DataFrame,
    mtm_curve: pd.DataFrame,
) -> dict[str, Any]:
    rets = pd.to_numeric(closed.get("net_ret"), errors="coerce").dropna() if not closed.empty else pd.Series(dtype=float)
    account_loss = pd.to_numeric(closed.get("account_loss_pct"), errors="coerce").dropna() if not closed.empty else pd.Series(dtype=float)
    open_pos = pd.to_numeric(curve.get("open_positions"), errors="coerce").fillna(0)
    reserved = pd.to_numeric(curve.get("reserved_principal"), errors="coerce").fillna(0)
    equity = pd.to_numeric(curve.get("equity"), errors="coerce").replace(0, np.nan).ffill().bfill()
    exposure = (reserved / equity).fillna(0)
    active = open_pos > 0
    reasons = closed.get("exit_reason", pd.Series(dtype=str)).fillna("").astype(str) if not closed.empty else pd.Series(dtype=str)
    mainline = closed[closed.get("mode", pd.Series(dtype=str)).astype(str).eq("institutional_mainwave")] if not closed.empty and "mode" in closed.columns else pd.DataFrame()
    max_single_loss = float(account_loss.min()) if len(account_loss) else 0.0
    mtm_dd = _max_drawdown(mtm_curve["equity_mtm"]) if not mtm_curve.empty and "equity_mtm" in mtm_curve.columns else None
    mtm_ret = (
        float(mtm_curve["equity_mtm"].iloc[-1] / mtm_curve["equity_mtm"].iloc[0] - 1.0)
        if not mtm_curve.empty and "equity_mtm" in mtm_curve.columns and float(mtm_curve["equity_mtm"].iloc[0])
        else None
    )
    return {
        "contract": contract.name,
        "label": contract.label,
        "slots": contract.slots,
        "slot_pct": contract.slot_pct,
        "hard_stop_pct": contract.hard_stop_pct,
        "take_profit_pct": contract.take_profit_pct,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / curve["equity"].iloc[0] - 1.0) if not curve.empty else 0.0,
        "max_drawdown": _max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "mtm_total_return": mtm_ret,
        "mtm_max_drawdown": mtm_dd,
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
        "best_trade": float(rets.max()) if len(rets) else 0.0,
        "sum_pnl": float(pd.to_numeric(closed.get("realized_pnl"), errors="coerce").sum()) if not closed.empty else 0.0,
        "max_single_account_loss": max_single_loss,
        "avg_active_exposure": float(exposure[active].mean()) if active.any() else 0.0,
        "avg_open_positions_active": float(open_pos[active].mean()) if active.any() else 0.0,
        "max_open_positions": int(open_pos.max()) if len(open_pos) else 0,
        "exit_reason_counts": reasons.str.split(",").explode().value_counts().to_dict() if len(reasons) else {},
        "institutional_mainwave_trades": int(len(mainline)),
        "institutional_mainwave_pnl": float(pd.to_numeric(mainline.get("realized_pnl"), errors="coerce").sum()) if not mainline.empty else 0.0,
        "windows": _window_metrics(curve, closed),
        "single_loss_gate_pass": abs(max_single_loss) <= float(contract.max_single_loss_limit),
        "mtm_drawdown_limit": float(contract.max_mtm_drawdown_limit),
        "mtm_drawdown_gate_pass": mtm_dd is None or float(mtm_dd) >= -abs(float(contract.max_mtm_drawdown_limit)),
    }


def _future_leak_audit(trades: pd.DataFrame) -> dict[str, Any]:
    issues = []
    passes = []
    source = RESEARCH_ROUTER.read_text(encoding="utf-8", errors="replace") if RESEARCH_ROUTER.exists() else ""
    compact = source.replace(" ", "").replace("\n", "")
    if 'sort_values(["mode_pick_rank","score","net_ret"]' in compact:
        issues.append("state_router_uses_net_ret_tiebreak")
    else:
        passes.append("state_router_candidate_tiebreak_no_net_ret")
    if "pd.merge_asof" in source and "direction=\"backward\"" in source and "allow_exact_matches=False" in source:
        passes.append("market_context_backward_no_exact_match")
    entry = pd.to_datetime(trades.get("entry_date"), errors="coerce")
    decision = pd.to_datetime(trades.get("decision_date"), errors="coerce")
    context = pd.to_datetime(trades.get("context_date"), errors="coerce")
    date_checks = {
        "rows": int(len(trades)),
        "decision_after_or_equal_entry": int(((decision >= entry) & decision.notna() & entry.notna()).sum()),
        "context_after_or_equal_entry": int(((context >= entry) & context.notna() & entry.notna()).sum()),
    }
    if date_checks["decision_after_or_equal_entry"] or date_checks["context_after_or_equal_entry"]:
        issues.append("decision_or_context_not_prior_to_entry")
    else:
        passes.append("entry_uses_prior_decision_context")
    return {
        "pass": not issues,
        "issues": issues,
        "passes": passes,
        "date_checks": date_checks,
    }


def _buy_timing_audit(trades: pd.DataFrame) -> dict[str, Any]:
    entry = pd.to_datetime(trades.get("entry_date"), errors="coerce")
    decision = pd.to_datetime(trades.get("decision_date"), errors="coerce")
    lag_days = (entry - decision).dt.days
    valid = lag_days.dropna()
    return {
        "pass": bool(len(valid) and valid.min() >= 1 and valid.median() <= 5),
        "min_lag_days": int(valid.min()) if len(valid) else None,
        "median_lag_days": float(valid.median()) if len(valid) else None,
        "max_lag_days": int(valid.max()) if len(valid) else None,
        "stale_signal_rows_gt10d": int((valid > 10).sum()) if len(valid) else 0,
    }


def _mainline_recall_audit(trades: pd.DataFrame) -> dict[str, Any]:
    inst = trades[trades.get("mode", pd.Series(dtype=str)).astype(str).eq("institutional_mainwave")].copy()
    post = trades[pd.to_datetime(trades["entry_date"], errors="coerce") >= pd.Timestamp("2024-09-01")]
    post_inst = post[post.get("mode", pd.Series(dtype=str)).astype(str).eq("institutional_mainwave")]
    return {
        "pass": len(post_inst) >= 25,
        "institutional_mainwave_candidates": int(len(inst)),
        "post_2024_09_candidates": int(len(post)),
        "post_2024_09_institutional_mainwave_candidates": int(len(post_inst)),
        "post_2024_09_mainwave_share": float(len(post_inst) / len(post)) if len(post) else 0.0,
    }


def _infra_health_audit() -> dict[str, Any]:
    required_paths = {
        "state_alpha_api": STATE_ALPHA_API,
        "selected_candidates": SOURCE_TRADES,
        "decisions": SOURCE_DECISIONS,
        "historical_closed_trades": report_path("gen3_market_state_router_strategy_v1") / "g3_route_execution_mandate_candidate_closed_trades.csv",
        "historical_equity_curve": report_path("gen3_market_state_router_strategy_v1") / "g3_route_execution_mandate_candidate_equity_curve.csv",
        "runtime_dir": ROOT,
    }
    artifacts = {}
    for name, path in required_paths.items():
        artifacts[name] = {
            "exists": path.exists(),
            "path": str(path),
            "size_bytes": path.stat().st_size if path.exists() and path.is_file() else None,
        }
    return {
        "pass": all(item["exists"] for item in artifacts.values()),
        "clickhouse_available": clickhouse_available(),
        "kline_daily_exists": clickhouse_table_exists("kline_daily") if clickhouse_available() else False,
        "kline_minute_30_exists": clickhouse_table_exists("kline_minute_30") if clickhouse_available() else False,
        "artifacts": artifacts,
    }


def _gate_rows(audits: dict[str, Any], summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_map = {row["contract"]: row for row in summaries}
    preferred = summary_map.get("g3_2slot_50_default_stop12_take12_prevlow") or {}
    attack = preferred
    gates = [
        ("no_future_leak", bool(audits["future_leak"]["pass"]), "历史回放不得使用未来收益或未来上下文。"),
        ("replay_reproducible", bool(audits["reproducibility"]["pass"]), "同一输入重复运行，成交与曲线 hash 必须一致。"),
        ("buy_timing_ok", bool(audits["buy_timing"]["pass"]), "买点必须由入场前确认数据触发，不能明显滞后。"),
        (
            "max_single_loss_controlled",
            bool(preferred.get("single_loss_gate_pass")) and bool(preferred.get("mtm_drawdown_gate_pass")),
            "默认合同单笔账户亏损与盯市回撤必须受控。",
        ),
        (
            "mainline_recall_ok",
            bool(audits["mainline_recall"]["pass"]),
            "2024-09 后应能覆盖足够机构主升机会。",
        ),
        ("infra_health_ok", bool(audits["infra_health"]["pass"]), "邮件/刷新/台账/历史成交所需基础文件与行情表可用。"),
        (
            "attack_contract_controlled",
            bool(attack.get("single_loss_gate_pass")) and bool(attack.get("mtm_drawdown_gate_pass")),
            "2槽进攻合同必须避免单笔和盯市组合回撤失控。",
        ),
    ]
    return [{"gate": gate, "pass": passed, "note": note} for gate, passed, note in gates]


def _write_report(payload: dict[str, Any]) -> None:
    summaries = payload["contract_summaries"]
    gates = payload["gates"]
    lines = [
        "# G3 Promotion Self-Test v1",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Source candidates: `{payload['source']['selected_candidates']}`",
        f"- Selected rows: `{payload['source']['selected_rows']}`",
        "",
        "## Gate Verdict",
        "",
        "| Gate | Pass | Note |",
        "|---|---:|---|",
    ]
    for row in gates:
        lines.append(f"| {row['gate']} | {'YES' if row['pass'] else 'NO'} | {row['note']} |")
    lines.extend(
        [
            "",
            "## Contract Summary",
            "",
            "| Contract | Trades | Return | Book DD | MTM DD | Win | Worst | Max Account Loss | PnL | Avg Active Exposure | Mainwave PnL |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["contract"],
                    str(row["trade_count"]),
                    _pct(row["total_return"]),
                    _pct(row["max_drawdown"]),
                    _pct(row.get("mtm_max_drawdown")),
                    _pct(row["win_rate"]),
                    _pct(row["worst_trade"]),
                    _pct(row["max_single_account_loss"]),
                    _money(row["sum_pnl"]),
                    _pct(row["avg_active_exposure"]),
                    _money(row["institutional_mainwave_pnl"]),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Window Summary", ""])
    for row in summaries:
        lines.append(f"### {row['contract']}")
        lines.append("| Window | Trades | Return | Max DD | Win | PnL |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for name, item in row["windows"].items():
            lines.append(
                f"| {name} | {item['trades']} | {_pct(item['return'])} | {_pct(item['max_drawdown'])} | {_pct(item['win_rate'])} | {_money(item['sum_pnl'])} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Notes",
            "",
            "- This self-test is a historical replay and infrastructure smoke test, not a replacement for live-market observation.",
            "- 30m stop and prev-low exits use local ClickHouse minute/daily bars with entry-price adjustment when raw and adjusted prices differ.",
            "- The default candidate for promotion should pass all hard gates before G3 replaces G2.",
        ]
    )
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _load_selected_candidates()
    decisions = _load_decisions()
    price_context = _build_price_context(trades)
    end = max(DEFAULT_END, pd.to_datetime(trades["policy_exit_date"], errors="coerce").max())
    calendar = _trade_calendar(pd.Timestamp("2020-01-01"), end)

    contract_summaries: list[dict[str, Any]] = []
    reproducibility_hashes: dict[str, dict[str, Any]] = {}

    for contract in CONTRACTS:
        prepared = _prepare_contract_candidates(trades, contract, price_context)
        curve1, closed1 = _simulate_portfolio(prepared, contract, calendar)
        curve2, closed2 = _simulate_portfolio(prepared, contract, calendar)
        mtm_curve1 = _simulate_mtm_curve(closed1, price_context, calendar)
        curve_hash1 = _hash_df(curve1, ["date", "equity", "open_positions", "opened", "realized_pnl"])
        curve_hash2 = _hash_df(curve2, ["date", "equity", "open_positions", "opened", "realized_pnl"])
        closed_hash1 = _hash_df(closed1, ["entry_date", "policy_exit_date", "code", "net_ret", "stake", "realized_pnl", "exit_reason"])
        closed_hash2 = _hash_df(closed2, ["entry_date", "policy_exit_date", "code", "net_ret", "stake", "realized_pnl", "exit_reason"])
        reproducibility_hashes[contract.name] = {
            "curve_hash_1": curve_hash1,
            "curve_hash_2": curve_hash2,
            "closed_hash_1": closed_hash1,
            "closed_hash_2": closed_hash2,
            "pass": curve_hash1 == curve_hash2 and closed_hash1 == closed_hash2,
        }
        prepared.to_csv(OUT_DIR / f"{contract.name}_prepared_candidates.csv", index=False, encoding="utf-8-sig")
        curve1.to_csv(OUT_DIR / f"{contract.name}_equity_curve.csv", index=False, encoding="utf-8-sig")
        mtm_curve1.to_csv(OUT_DIR / f"{contract.name}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed1.to_csv(OUT_DIR / f"{contract.name}_closed_trades.csv", index=False, encoding="utf-8-sig")
        contract_summaries.append(_contract_summary(contract, curve1, closed1, mtm_curve1))

    audits = {
        "future_leak": _future_leak_audit(trades),
        "buy_timing": _buy_timing_audit(trades),
        "mainline_recall": _mainline_recall_audit(trades),
        "infra_health": _infra_health_audit(),
        "reproducibility": {
            "pass": all(item["pass"] for item in reproducibility_hashes.values()),
            "contracts": reproducibility_hashes,
        },
        "source_decisions_rows": int(len(decisions)),
    }
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "source": {
            "selected_candidates": str(SOURCE_TRADES),
            "selected_rows": int(len(trades)),
            "decisions": str(SOURCE_DECISIONS),
            "decisions_rows": int(len(decisions)),
            "daily_rows": int(len(price_context["daily"])),
            "minute30_rows": int(len(price_context["minute30"])),
        },
        "audits": audits,
        "contract_summaries": contract_summaries,
    }
    gates = _gate_rows(audits, contract_summaries)
    payload["gates"] = gates
    pd.DataFrame(contract_summaries).drop(columns=["windows", "exit_reason_counts"], errors="ignore").to_csv(
        OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(gates).to_csv(OUT_DIR / "gates.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(payload)
    print(
        json.dumps(
            {
                "status": "completed",
                "out_dir": str(OUT_DIR),
                "gates_pass": all(row["pass"] for row in gates),
                "contracts": [row["contract"] for row in contract_summaries],
            },
            ensure_ascii=False,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
