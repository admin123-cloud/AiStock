from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import sys

import numpy as np
import pandas as pd

REPO_ROOT = Path(r"F:\\Stock\\AiStock")
sys.path.insert(0, str(REPO_ROOT))

from api.gen2_strategy import GEN2_OPEN_RULE_V1
from utils.market_warehouse import clickhouse_client

SIGNAL_SOURCE = REPO_ROOT / "reports" / "gen2_30m_fractal_restart_expanded_w2" / "fractal_triggers.parquet"
REPORT_ROOT = REPO_ROOT / "reports" / "gen2_open_v1_market_gate_soft"

INDEX_MAP = {
    "SSE": "999999.SH",
    "SZ_MAIN": "000852.SH",
    "SZ_CY": "399006.SZ",
}


@dataclass
class Lot:
    code: str
    name: str
    buy_date: str
    sell_date: str
    buy_price: float
    shares: float
    capital: float
    v4_rank: int
    v4_score: float
    peak_price: float
    take_profit_done: bool = False


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return str(value)


def _pct(x: Any) -> str:
    return "" if x is None else f"{float(x) * 100:.2f}%"


def _stock_market(code: str) -> str:
    code = str(code)
    if code.endswith(".SH"):
        return "SSE"
    if code.endswith(".SZ") and code.startswith("300"):
        return "SZ_CY"
    if code.endswith(".SZ"):
        return "SZ_MAIN"
    return "OTHER"


def _load_signals(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    d = df[
        (df["pattern"] == GEN2_OPEN_RULE_V1["pattern"])
        & (df["g2_open_state"] == GEN2_OPEN_RULE_V1["g2_open_state"])
        & (df["trigger_type"] == GEN2_OPEN_RULE_V1["trigger_type"])
    ].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()

    d["v4_rank"] = pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999).astype(int)
    d["v4_score"] = pd.to_numeric(d["v4_score"], errors="coerce").fillna(0.0)
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["confirm_datetime"] = pd.to_datetime(d.get("confirm_datetime"), errors="coerce")
    d["confirm_amount"] = pd.to_numeric(d.get("confirm_amount"), errors="coerce")
    d["confirm_amount_ma5_prev"] = pd.to_numeric(d.get("confirm_amount_ma5_prev"), errors="coerce")
    d["volume_ratio"] = np.where(
        d["confirm_amount_ma5_prev"] > 0,
        d["confirm_amount"] / d["confirm_amount_ma5_prev"],
        np.nan,
    )
    d = d.dropna(subset=["entry_date", "code", "entry_price"]).reset_index(drop=True)

    d["stock_market"] = d["code"].astype(str).map(_stock_market)
    d = d[d["stock_market"].isin(["SSE", "SZ_MAIN", "SZ_CY"])].reset_index(drop=True)
    return d


def _load_trade_dates(start_date: str, end_date: str) -> List[str]:
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY trade_date
        """
    )
    return [pd.Timestamp(x).strftime("%Y-%m-%d") for x in df["trade_date"].tolist()]


def _load_daily_prices(codes: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)


def _build_index_gate_states(start_date: str, end_date: str, mode: str) -> Dict[str, Dict[str, bool]]:
    if mode == "none":
        return {}
    if mode not in {"global_sse", "per_market"}:
        raise ValueError(f"Unsupported gate mode: {mode}")

    ch = clickhouse_client()
    out: Dict[str, Dict[str, bool]] = {}
    mode_codes = [INDEX_MAP["SSE"]] if mode == "global_sse" else list(INDEX_MAP.values())

    for code in mode_codes:
        df = ch.query_df(
            f"""
            SELECT trade_date, close
            FROM kline_daily
            WHERE code = '{code}'
              AND trade_date BETWEEN '{start_date}' AND '{end_date}'
            ORDER BY trade_date
            """
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df = df.sort_values("trade_date").reset_index(drop=True)
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["trade_date", "close"])
        df["ma20"] = df["close"].rolling(20, min_periods=20).mean()
        state = (df["close"] >= df["ma20"]).shift(1).fillna(False)
        out[code] = dict(zip(df["trade_date"], state.astype(bool).tolist()))
    return out


def _attach_gate_state(signals: pd.DataFrame, mode: str, states: Dict[str, Dict[str, bool]]) -> pd.DataFrame:
    if mode == "none":
        signals["market_gate_on"] = True
        return signals

    if mode == "global_sse":
        state = states[INDEX_MAP["SSE"]]
        signals["market_gate_on"] = signals["entry_date"].map(state).fillna(False).astype(bool)
        return signals

    sse_state = states[INDEX_MAP["SSE"]]
    szm_state = states[INDEX_MAP["SZ_MAIN"]]
    szcy_state = states[INDEX_MAP["SZ_CY"]]
    m_map = {"SSE": sse_state, "SZ_MAIN": szm_state, "SZ_CY": szcy_state}
    signals["market_gate_on"] = False
    for mkt, state_map in m_map.items():
        mask = signals["stock_market"] == mkt
        signals.loc[mask, "market_gate_on"] = signals.loc[mask, "entry_date"].map(state_map).fillna(False).astype(bool)
    return signals


def _safe_price(price_map: Dict[tuple[str, str], float], code: str, date: str) -> Optional[float]:
    value = price_map.get((code, date))
    if value is None or not np.isfinite(value) or value <= 0:
        return None
    return float(value)


def _sell_lot(
    lot: Lot,
    date: str,
    raw_price: float,
    ratio: float,
    reason: str,
    sell_slippage_bps: float,
    commission_bps: float,
    stamp_tax_bps: float,
) -> Dict[str, Any]:
    sell_ratio = max(0.0, min(float(ratio), 1.0))
    shares = lot.shares * sell_ratio
    capital = lot.capital * sell_ratio
    sell_price = float(raw_price) * (1.0 - float(sell_slippage_bps) / 10000.0)
    gross = shares * sell_price
    sell_fee = gross * (float(commission_bps) + float(stamp_tax_bps)) / 10000.0
    proceeds = gross - sell_fee
    return {
        "code": lot.code,
        "name": lot.name,
        "buy_date": lot.buy_date,
        "sell_date": date,
        "buy_price": lot.buy_price,
        "sell_price": sell_price,
        "raw_sell_price": float(raw_price),
        "sell_ratio": sell_ratio,
        "shares": shares,
        "capital": capital,
        "sell_fee": sell_fee,
        "pnl": proceeds - capital,
        "return": proceeds / capital - 1.0 if capital > 0 else np.nan,
        "exit_reason": reason,
        "v4_rank": lot.v4_rank,
        "v4_score": lot.v4_score,
        "proceeds": proceeds,
        "market_gate_on": True,
    }


def _load_benchmark(start_date: str, end_date: str, benchmark_code: str) -> pd.DataFrame:
    daily = _load_daily_prices([benchmark_code], start_date, end_date)
    if daily.empty:
        return pd.DataFrame(columns=["date", "benchmark_equity"])
    d = daily.rename(columns={"trade_date": "date"}).copy()
    first = float(d["close"].iloc[0])
    d["benchmark_equity"] = d["close"] / first if first > 0 else np.nan
    return d[["date", "benchmark_equity"]]


def run_soft_gate(
    *,
    signal_source: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    initial_cash: float,
    max_positions: int,
    max_buys_per_day: int,
    hold_days: int,
    benchmark_code: str,
    gate_mode: str,
    off_gate_factor: float,
    buy_slippage_bps: float = 5.0,
    sell_slippage_bps: float = 5.0,
    commission_bps: float = 2.5,
    stamp_tax_bps: float = 5.0,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    take_profit_sell_ratio: float = 0.5,
    trailing_stop_pct: Optional[float] = None,
    max_position_weight: float = 0.0,
) -> Dict[str, Any]:

    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(signal_source, start_date, end_date)
    trade_dates = _load_trade_dates(start_date, end_date)
    if not trade_dates:
        raise RuntimeError("No trade dates available for backtest.")

    states = _build_index_gate_states(start_date, end_date, gate_mode)
    signals = _attach_gate_state(signals, gate_mode, states)

    signals = signals[signals["entry_date"].isin(trade_dates)].copy().reset_index(drop=True)
    signals["gate_factor"] = np.where(signals["market_gate_on"], 1.0, float(off_gate_factor))
    signals = signals.sort_values(["entry_date", "confirm_datetime", "v4_rank", "v4_score", "code"], ascending=[True, True, True, False, True], na_position="last")

    signals_by_date = {date: g.copy() for date, g in signals.groupby("entry_date", sort=True)}
    prices = _load_daily_prices(signals["code"].dropna().astype(str).unique().tolist(), start_date, end_date)
    price_map = {(row.code, row.trade_date): float(row.close) for row in prices.itertuples(index=False)}

    cash = float(initial_cash)
    lots: List[Lot] = []
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    date_to_idx = {d: i for i, d in enumerate(trade_dates)}

    for date in trade_dates:
        next_lots: List[Lot] = []
        for lot in lots:
            close_price = _safe_price(price_map, lot.code, date)
            if close_price is None:
                next_lots.append(lot)
                continue

            lot.peak_price = max(float(lot.peak_price), close_price)
            cur_ret = close_price / lot.buy_price - 1.0
            drawdown = close_price / lot.peak_price - 1.0 if lot.peak_price > 0 else 0.0
            sell_reason = ""
            sell_ratio = 0.0

            if stop_loss_pct is not None and cur_ret <= -abs(float(stop_loss_pct)):
                sell_reason = "stop_loss"
                sell_ratio = 1.0
            elif (
                take_profit_pct is not None
                and not lot.take_profit_done
                and cur_ret >= abs(float(take_profit_pct))
                and 0 < float(take_profit_sell_ratio) < 1
            ):
                sell_reason = "take_profit_partial"
                sell_ratio = float(take_profit_sell_ratio)
            elif (
                trailing_stop_pct is not None
                and lot.take_profit_done
                and drawdown <= -abs(float(trailing_stop_pct))
            ):
                sell_reason = "trailing_stop"
                sell_ratio = 1.0
            elif date >= lot.sell_date:
                sell_reason = "time_exit"
                sell_ratio = 1.0

            if sell_ratio > 0:
                trade = _sell_lot(
                    lot=lot,
                    date=date,
                    raw_price=close_price,
                    ratio=sell_ratio,
                    reason=sell_reason,
                    sell_slippage_bps=sell_slippage_bps,
                    commission_bps=commission_bps,
                    stamp_tax_bps=stamp_tax_bps,
                )
                cash += float(trade["proceeds"])
                trades.append({k: v for k, v in trade.items() if k != "proceeds"})
                lot.shares -= float(trade["shares"])
                lot.capital -= float(trade["capital"])
                if sell_reason == "take_profit_partial":
                    lot.take_profit_done = True
                if lot.shares > 1e-9 and lot.capital > 1e-6:
                    next_lots.append(lot)
            else:
                next_lots.append(lot)

        lots = next_lots

        today = signals_by_date.get(date)
        if today is not None and not today.empty:
            held_codes = {lot.code for lot in lots}
            slots = max(0, int(max_positions) - len(lots))
            buy_count = min(slots, int(max_buys_per_day))
            if buy_count > 0 and cash > 0:
                candidates = today[~today["code"].isin(held_codes)].head(buy_count)
                market_value = 0.0
                for lot in lots:
                    close = _safe_price(price_map, lot.code, date) or lot.buy_price
                    market_value += lot.shares * close
                sizing_equity = cash + market_value
                target_cap = sizing_equity / float(max_positions) if int(max_positions) > 0 else cash
                if float(max_position_weight) > 0:
                    target_cap = min(target_cap, sizing_equity * float(max_position_weight))

                for row in candidates.itertuples(index=False):
                    cap_base = min(cash, target_cap) * float(getattr(row, "gate_factor"))
                    if cap_base <= 0:
                        continue
                    raw_buy_price = float(getattr(row, "entry_price"))
                    buy_price = raw_buy_price * (1.0 + float(buy_slippage_bps) / 10000.0)
                    if buy_price <= 0:
                        continue
                    entry_idx = date_to_idx[date]
                    sell_idx = min(entry_idx + int(hold_days), len(trade_dates) - 1)
                    sell_date = trade_dates[sell_idx]
                    buy_fee = cap_base * float(commission_bps) / 10000.0
                    shares = (cap_base - buy_fee) / buy_price
                    if shares <= 0:
                        continue
                    cash -= cap_base
                    lots.append(
                        Lot(
                            code=str(getattr(row, "code")),
                            name=str(getattr(row, "name")),
                            buy_date=date,
                            sell_date=sell_date,
                            buy_price=buy_price,
                            shares=shares,
                            capital=cap_base,
                            v4_rank=int(getattr(row, "v4_rank")),
                            v4_score=float(getattr(row, "v4_score")),
                            peak_price=buy_price,
                        )
                    )

        market_value = 0.0
        for lot in lots:
            close = _safe_price(price_map, lot.code, date) or lot.buy_price
            market_value += lot.shares * close
        equity = cash + market_value
        curve.append({"date": date, "cash": cash, "market_value": market_value, "equity": equity, "holding_count": len(lots)})

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
    if not curve_df.empty:
        curve_df["strategy_equity"] = curve_df["equity"] / float(initial_cash)
        curve_df["peak"] = curve_df["strategy_equity"].cummax()
        curve_df["drawdown"] = curve_df["strategy_equity"] / curve_df["peak"] - 1.0

    benchmark = _load_benchmark(start_date, end_date, benchmark_code)
    if not benchmark.empty:
        curve_df = curve_df.merge(benchmark, on="date", how="left")

    total_return = float(curve_df["strategy_equity"].iloc[-1] - 1.0) if not curve_df.empty else 0.0
    max_drawdown = float(curve_df["drawdown"].min()) if not curve_df.empty else 0.0
    win_rate = float((trades_df["return"] > 0).mean()) if not trades_df.empty else None
    avg_trade_return = float(trades_df["return"].mean()) if not trades_df.empty else None
    benchmark_return = (
        float(curve_df["benchmark_equity"].dropna().iloc[-1] - 1.0)
        if "benchmark_equity" in curve_df.columns and not curve_df["benchmark_equity"].dropna().empty
        else None
    )

    summary = {
        "schema_version": 1,
        "rule": GEN2_OPEN_RULE_V1,
        "start_date": start_date,
        "end_date": end_date,
        "gate_mode": gate_mode,
        "off_gate_factor": float(off_gate_factor),
        "signal_count": int(len(signals)),
        "trade_count": int(len(trades_df)),
        "initial_cash": float(initial_cash),
        "max_positions": int(max_positions),
        "max_buys_per_day": int(max_buys_per_day),
        "hold_days": int(hold_days),
        "sort_mode": "trigger_time",
        "max_position_weight": float(max_position_weight),
        "benchmark_code": benchmark_code,
        "buy_slippage_bps": float(buy_slippage_bps),
        "sell_slippage_bps": float(sell_slippage_bps),
        "commission_bps": float(commission_bps),
        "stamp_tax_bps": float(stamp_tax_bps),
        "stop_loss_pct": None if stop_loss_pct is None else float(stop_loss_pct),
        "take_profit_pct": None if take_profit_pct is None else float(take_profit_pct),
        "take_profit_sell_ratio": float(take_profit_sell_ratio),
        "trailing_stop_pct": None if trailing_stop_pct is None else float(trailing_stop_pct),
        "final_equity": float(curve_df["equity"].iloc[-1]) if not curve_df.empty else float(initial_cash),
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "excess_return": None if benchmark_return is None else total_return - benchmark_return,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "avg_trade_return": avg_trade_return,
    }

    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    curve_df.to_csv(output_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    findings = [
        "# G2 Open V1 Soft Market Gate",
        f"Window: {start_date} to {end_date}",
        f"Mode: {gate_mode}, off gate factor: {off_gate_factor}",
        f"Total return: {_pct(total_return)}",
        f"Benchmark return ({benchmark_code}): {_pct(benchmark_return)}",
        f"Excess return: {_pct(summary['excess_return'])}",
        f"Max drawdown: {_pct(max_drawdown)}",
        f"Trades: {summary['trade_count']}",
        f"Win rate: {_pct(win_rate)}",
        f"Avg trade return: {_pct(avg_trade_return)}",
    ]
    (output_dir / "findings.md").write_text("\n".join(findings), encoding="utf-8")
    return summary


def _run_one(name: str, gate_mode: str, off_gate_factor: float) -> Dict[str, Any]:
    out = REPORT_ROOT / name
    s = run_soft_gate(
        signal_source=SIGNAL_SOURCE,
        output_dir=out,
        start_date="2024-07-09",
        end_date="2026-05-21",
        initial_cash=150000.0,
        max_positions=2,
        max_buys_per_day=1,
        hold_days=3,
        benchmark_code="000852.SH",
        buy_slippage_bps=5.0,
        sell_slippage_bps=5.0,
        commission_bps=2.5,
        stamp_tax_bps=5.0,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        take_profit_sell_ratio=0.5,
        trailing_stop_pct=None,
        max_position_weight=0.50,
        gate_mode=gate_mode,
        off_gate_factor=off_gate_factor,
    )
    return {
        "name": name,
        **{k: s[k] for k in ["total_return", "excess_return", "max_drawdown", "trade_count", "win_rate", "signal_count", "benchmark_return", "final_equity"]},
    }


if __name__ == "__main__":
    cases = [
        ("none", "none", 1.0),
        ("global_sse_soft_0p5", "global_sse", 0.5),
        ("global_sse_soft_0p3", "global_sse", 0.3),
        ("per_market_soft_0p5", "per_market", 0.5),
        ("per_market_soft_0p3", "per_market", 0.3),
    ]
    rows = []
    for name, mode, factor in cases:
        rows.append(_run_one(name, mode, factor))
    print(pd.DataFrame(rows).to_string(index=False))


