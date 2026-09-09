from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from utils.database import db

try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None

OUTPUT_DIR = Path(r"F:\Stock\AiStock\output_v3_audit")
STRATEGY_VERSION = "v3_audit_raw"

START_DATE = "2026-01-01"
END_DATE = "2026-04-25"
INITIAL_CAPITAL = 100000.0

MAX_HOLDINGS_ON = 3
MAX_HOLDINGS_NEUTRAL = 2
MAX_HOLDINGS_OFF = 1

MIN_TURNOVER20 = 2e8
MIN_VOL10 = 0.008
MAX_VOL10 = 0.09

SLIPPAGE = 0.0002
COMMISSION = 0.0003
STAMP_DUTY_SELL = 0.0005
MIN_COMMISSION = 5.0

ARTIFACT_TABLE = "trading_strategy_artifacts"


@dataclass
class Position:
    code: str
    name: str
    shares: int
    avg_cost: float
    buy_idx: int
    peak_close: float
    took_profit: bool = False


@dataclass
class Params:
    stop_loss: float
    take_profit: float
    trailing: float
    soft_dd: float
    hard_dd: float
    freeze_days: int
    breadth_low: float
    breadth_high: float
    invest_on: float
    invest_neutral: float
    invest_off: float
    min_hold_trend: int
    min_hold_score: int
    entry_min_score: float
    keep_rank_mult: int


DEFAULT_PARAMS = Params(
    stop_loss=0.03,
    take_profit=0.08,
    trailing=0.03,
    soft_dd=-0.08,
    hard_dd=-0.10,
    freeze_days=5,
    breadth_low=0.50,
    breadth_high=0.60,
    invest_on=0.65,
    invest_neutral=0.40,
    invest_off=0.10,
    min_hold_trend=4,
    min_hold_score=8,
    entry_min_score=0.70,
    keep_rank_mult=2,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    return str(value)


def _ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def calc_commission(notional: float) -> float:
    return max(MIN_COMMISSION, notional * COMMISSION)


def round_lot(shares: int) -> int:
    return (int(shares) // 100) * 100


def score_candidates(day_df: pd.DataFrame) -> pd.DataFrame:
    d = day_df.copy().replace([np.inf, -np.inf], np.nan)
    d = d.dropna(subset=["mom5", "mom10", "vol_ratio", "amt20", "vol10", "ma10", "close"])
    d = d[(d["mom5"] > 0) & (d["close_gt_ma10"]) & (d["amt20"] >= MIN_TURNOVER20)]
    d = d[(d["vol10"] >= MIN_VOL10) & (d["vol10"] <= MAX_VOL10)]
    if d.empty:
        return d

    for c in ["mom5", "mom10", "vol_ratio", "amt20"]:
        d[f"r_{c}"] = d[c].rank(pct=True)
    d["r_vol10_low"] = 1 - d["vol10"].rank(pct=True)
    d["score"] = (
        0.35 * d["r_mom5"]
        + 0.20 * d["r_mom10"]
        + 0.20 * d["r_vol_ratio"]
        + 0.20 * d["r_amt20"]
        + 0.05 * d["r_vol10_low"]
    )
    return d.sort_values("score", ascending=False)


def risk_mode_from_signal(signal_dd: float, breadth: float, p: Params, freeze_left: int) -> Tuple[str, bool]:
    if signal_dd <= p.hard_dd:
        return "off", True
    if freeze_left > 0:
        return "off", False

    if breadth < p.breadth_low:
        mode = "off"
    elif breadth < p.breadth_high:
        mode = "neutral"
    else:
        mode = "on"

    if signal_dd <= p.soft_dd:
        if mode == "on":
            mode = "neutral"
        elif mode == "neutral":
            mode = "off"
    return mode, False


def mode_limits(mode: str, p: Params) -> Tuple[float, int]:
    if mode == "on":
        return p.invest_on, MAX_HOLDINGS_ON
    if mode == "neutral":
        return p.invest_neutral, MAX_HOLDINGS_NEUTRAL
    return p.invest_off, MAX_HOLDINGS_OFF


def choose_weights(candidates: List[str], signal_df: pd.DataFrame) -> Dict[str, float]:
    if not candidates:
        return {}
    vals = []
    for c in candidates:
        if c in signal_df.index:
            v = float(signal_df.loc[c, "vol10"]) if pd.notna(signal_df.loc[c, "vol10"]) else np.nan
        else:
            v = np.nan
        vals.append(v)

    arr = np.array(vals, dtype=float)
    med = np.nanmedian(arr) if np.isfinite(arr).any() else 0.03
    arr = np.where(np.isfinite(arr), arr, med)
    arr = np.clip(arr, 0.005, 0.12)
    inv = 1.0 / arr
    w = inv / inv.sum()
    return {c: float(wi) for c, wi in zip(candidates, w)}


class V3AuditBacktester:
    def __init__(self, start_date: str, end_date: str, initial_capital: float, params: Params):
        self.start_date = str(start_date)
        self.end_date = str(end_date)
        self.initial_capital = float(initial_capital)
        self.params = params
        self.trade_dates = self._load_trade_dates()
        self.hist = self._load_hist()
        self.by_date = {d: sdf.set_index("code", drop=False) for d, sdf in self.hist.groupby("date")}
        self.breadth_map = self._build_breadth_map()
        self.cash = self.initial_capital
        self.positions: Dict[str, Position] = {}
        self.equity_peak = self.initial_capital
        self.freeze_left = 0
        self.trades: List[Dict[str, Any]] = []
        self.holds: List[Dict[str, Any]] = []
        self.curve: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []
        self.realized: List[float] = []

    def _load_trade_dates(self) -> List[pd.Timestamp]:
        sql = text(
            """
            SELECT trade_date
            FROM trade_calendar
            WHERE market = 'SH'
              AND is_trading = 1
              AND trade_date >= :start_date
              AND trade_date <= :end_date
            ORDER BY trade_date ASC
            """
        )
        rows = pd.read_sql(sql, db.engine, params={"start_date": self.start_date, "end_date": self.end_date})
        if rows.empty:
            return []
        return list(pd.to_datetime(rows["trade_date"], errors="coerce").dropna())

    def _load_hist(self) -> pd.DataFrame:
        preload_start = str((pd.Timestamp(self.start_date) - pd.Timedelta(days=260)).date())
        sql = text(
            """
            SELECT
                SUBSTRING(k.code, 1, 6) AS code,
                s.name AS name,
                k.trade_date AS date,
                k.open AS open,
                k.high AS high,
                k.low AS low,
                k.close AS close,
                k.amount AS amount
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE k.trade_date >= :start_date
              AND k.trade_date <= :end_date
              AND s.type = 'stock'
              AND (s.st = 0 OR s.st IS NULL)
            ORDER BY code ASC, date ASC
            """
        )
        hist = pd.read_sql(sql, db.engine, params={"start_date": preload_start, "end_date": self.end_date})
        if hist.empty:
            return hist
        hist["date"] = pd.to_datetime(hist["date"], errors="coerce")
        for col in ["open", "high", "low", "close", "amount"]:
            hist[col] = pd.to_numeric(hist[col], errors="coerce")
        hist = hist.dropna(subset=["date", "code", "open", "high", "low", "close"]).copy()
        hist["amount"] = hist["amount"].fillna(0) * 10000.0
        cnt = hist.groupby("code")["date"].count()
        good = cnt[cnt >= 60].index
        hist = hist[hist["code"].isin(good)].copy()
        hist = hist.sort_values(["code", "date"]).reset_index(drop=True)
        g = hist.groupby("code", group_keys=False)
        hist["ret1"] = g["close"].pct_change()
        hist["mom5"] = g["close"].pct_change(5)
        hist["mom10"] = g["close"].pct_change(10)
        hist["ma10"] = g["close"].rolling(10).mean().reset_index(level=0, drop=True)
        hist["amt20"] = g["amount"].rolling(20).mean().reset_index(level=0, drop=True)
        hist["amt5_prev"] = g["amount"].shift(1).rolling(5).mean().reset_index(level=0, drop=True)
        hist["vol_ratio"] = hist["amount"] / hist["amt5_prev"]
        hist["vol10"] = g["ret1"].rolling(10).std().reset_index(level=0, drop=True)
        hist["close_gt_ma10"] = hist["close"] > hist["ma10"]
        return hist

    def _build_breadth_map(self) -> Dict[pd.Timestamp, float]:
        breadth: Dict[pd.Timestamp, float] = {}
        for d, sdf in self.by_date.items():
            mask = sdf["ma10"].notna()
            if mask.any():
                breadth[d] = float((sdf.loc[mask, "close"] > sdf.loc[mask, "ma10"]).mean())
            else:
                breadth[d] = 0.5
        return breadth

    def run(self) -> Dict[str, Any]:
        trade_count = 0
        sell_count = 0
        equity_series: List[Tuple[pd.Timestamp, float]] = []
        dates = [d for d in self.trade_dates if pd.Timestamp(self.start_date) <= d <= pd.Timestamp(self.end_date)]

        for i in range(len(dates) - 1):
            signal_date = dates[i]
            trade_date = dates[i + 1]
            signal_df = self.by_date.get(signal_date)
            trade_df = self.by_date.get(trade_date)
            if signal_df is None or trade_df is None:
                continue

            scored = score_candidates(signal_df.reset_index(drop=True))
            score_map = dict(zip(scored["code"], scored["score"])) if not scored.empty else {}

            signal_mv = 0.0
            for code, pos in self.positions.items():
                px = float(signal_df.loc[code, "close"]) if code in signal_df.index and float(signal_df.loc[code, "close"]) > 0 else pos.avg_cost
                pos.peak_close = max(pos.peak_close, px)
                signal_mv += px * pos.shares

            signal_equity = self.cash + signal_mv
            self.equity_peak = max(self.equity_peak, signal_equity)
            signal_dd = signal_equity / self.equity_peak - 1 if self.equity_peak > 0 else 0.0
            breadth = self.breadth_map.get(signal_date, 0.5)

            mode, refresh_freeze = risk_mode_from_signal(signal_dd, breadth, self.params, self.freeze_left)
            if refresh_freeze:
                self.freeze_left = max(self.freeze_left, self.params.freeze_days)
                mode = "off"

            invest_ratio, max_holdings = mode_limits(mode, self.params)

            sell_plan: Dict[str, Dict[str, Any]] = {}
            keep_top_n = max_holdings * self.params.keep_rank_mult
            keep_set = set(scored["code"].head(keep_top_n).tolist()) if not scored.empty else set()

            for code, pos in list(self.positions.items()):
                if code not in signal_df.index:
                    sell_plan[code] = {"shares": pos.shares, "reason": "missing_data_exit"}
                    continue

                row = signal_df.loc[code]
                close_px = float(row["close"])
                ma10 = float(row["ma10"]) if pd.notna(row["ma10"]) else close_px
                hold_days = i - pos.buy_idx + 1

                if close_px <= pos.avg_cost * (1 - self.params.stop_loss):
                    sell_plan[code] = {"shares": pos.shares, "reason": "stop_loss"}
                    continue
                if (not pos.took_profit) and close_px >= pos.avg_cost * (1 + self.params.take_profit):
                    half = round_lot(pos.shares // 2)
                    if half >= 100:
                        sell_plan[code] = {"shares": half, "reason": "take_profit_partial"}
                    else:
                        sell_plan[code] = {"shares": pos.shares, "reason": "take_profit_small_full"}
                    continue
                if pos.took_profit and close_px <= pos.peak_close * (1 - self.params.trailing):
                    sell_plan[code] = {"shares": pos.shares, "reason": "trailing_stop"}
                    continue
                if hold_days >= self.params.min_hold_trend and close_px < ma10:
                    sell_plan[code] = {"shares": pos.shares, "reason": "trend_break"}
                    continue
                if hold_days >= self.params.min_hold_score and code not in keep_set:
                    sell_plan[code] = {"shares": pos.shares, "reason": "score_drop"}
                    continue

            target_mv_signal = signal_equity * invest_ratio
            if signal_mv > target_mv_signal:
                need_cut = signal_mv - target_mv_signal
                weakest = sorted(list(self.positions.keys()), key=lambda c: score_map.get(c, -1.0))
                for code in weakest:
                    if need_cut <= 0:
                        break
                    pos = self.positions[code]
                    already = int(sell_plan.get(code, {}).get("shares", 0))
                    remain = round_lot(pos.shares - already)
                    if remain < 100:
                        continue
                    ref = float(signal_df.loc[code, "close"]) if code in signal_df.index and float(signal_df.loc[code, "close"]) > 0 else pos.avg_cost
                    if code in sell_plan:
                        sell_plan[code]["shares"] = round_lot(int(sell_plan[code]["shares"]) + remain)
                        sell_plan[code]["reason"] = f"{sell_plan[code]['reason']}+portfolio_cut"
                    else:
                        sell_plan[code] = {"shares": remain, "reason": "portfolio_cut"}
                    need_cut -= remain * ref

            sold_today = set()
            for code in sorted(sell_plan.keys()):
                if code not in self.positions or code not in signal_df.index:
                    continue
                pos = self.positions[code]
                req = min(int(sell_plan[code]["shares"]), pos.shares)
                shares = round_lot(req)
                if shares < 100 and shares != pos.shares:
                    continue
                if shares <= 0:
                    continue
                close_px = float(signal_df.loc[code, "close"])
                if close_px <= 0 or np.isnan(close_px):
                    continue
                exec_px = close_px * (1 - SLIPPAGE)
                gross = exec_px * shares
                comm = calc_commission(gross)
                tax = gross * STAMP_DUTY_SELL
                net = gross - comm - tax
                self.cash += net
                pnl = (exec_px - pos.avg_cost) * shares - comm - tax
                self.realized.append(pnl)
                trade_count += 1
                sell_count += 1
                sold_today.add(code)
                self.trades.append(
                    {
                        "date": str(signal_date.date()),
                        "side": "SELL",
                        "code": code,
                        "name": pos.name,
                        "shares": shares,
                        "price": round(exec_px, 4),
                        "gross": round(gross, 2),
                        "fee": round(comm + tax, 2),
                        "net_cash_change": round(net, 2),
                        "signal_date": str(signal_date.date()),
                        "reason": str(sell_plan[code]["reason"]),
                        "realized_pnl": round(pnl, 2),
                    }
                )
                if shares >= pos.shares:
                    del self.positions[code]
                else:
                    self.positions[code].shares -= shares
                    if "take_profit_partial" in str(sell_plan[code]["reason"]):
                        self.positions[code].took_profit = True

            allow_buy = self.freeze_left == 0
            open_mv = 0.0
            for code, pos in self.positions.items():
                px = float(signal_df.loc[code, "close"]) if code in signal_df.index and float(signal_df.loc[code, "close"]) > 0 else pos.avg_cost
                open_mv += px * pos.shares

            equity_open = self.cash + open_mv
            target_mv_open = equity_open * invest_ratio
            buy_budget = max(0.0, target_mv_open - open_mv)
            held = set(self.positions.keys())
            slots = max(0, max_holdings - len(held))

            candidate_codes: List[str] = []
            if allow_buy and slots > 0 and not scored.empty:
                for _, r in scored.head(max_holdings * 6).iterrows():
                    c = str(r["code"])
                    s = float(r["score"])
                    if s < self.params.entry_min_score:
                        continue
                    if c in held or c in sold_today:
                        continue
                    candidate_codes.append(c)
                    if len(candidate_codes) >= slots:
                        break

            if candidate_codes and buy_budget > 0:
                weights = choose_weights(candidate_codes, signal_df)
                for code in candidate_codes:
                    if code not in signal_df.index:
                        continue
                    close_px = float(signal_df.loc[code, "close"])
                    if close_px <= 0 or np.isnan(close_px):
                        continue
                    exec_px = close_px * (1 + SLIPPAGE)
                    cap = min(self.cash, buy_budget * weights.get(code, 0))
                    raw_sh = int((cap - MIN_COMMISSION) // exec_px)
                    sh = round_lot(raw_sh)
                    if sh < 100:
                        continue
                    gross = exec_px * sh
                    comm = calc_commission(gross)
                    total = gross + comm
                    if total > self.cash:
                        max_sh = int((self.cash - MIN_COMMISSION) // exec_px)
                        sh = round_lot(max_sh)
                        if sh < 100:
                            continue
                        gross = exec_px * sh
                        comm = calc_commission(gross)
                        total = gross + comm
                        if total > self.cash:
                            continue
                    self.cash -= total
                    trade_count += 1
                    row = signal_df.loc[code]
                    name = str(row["name"])
                    ref_close = float(signal_df.loc[code, "close"]) if code in signal_df.index else exec_px
                    self.positions[code] = Position(
                        code=code,
                        name=name,
                        shares=sh,
                        avg_cost=(gross + comm) / sh,
                        buy_idx=i,
                        peak_close=ref_close,
                        took_profit=False,
                    )
                    self.trades.append(
                        {
                            "date": str(signal_date.date()),
                            "side": "BUY",
                            "code": code,
                            "name": name,
                            "shares": sh,
                            "price": round(exec_px, 4),
                            "gross": round(gross, 2),
                            "fee": round(comm, 2),
                            "net_cash_change": round(-total, 2),
                            "signal_date": str(signal_date.date()),
                            "reason": "target_entry",
                            "realized_pnl": np.nan,
                        }
                    )

            close_mv = 0.0
            hold_desc = []
            for code, pos in self.positions.items():
                if code in trade_df.index and float(trade_df.loc[code, "close"]) > 0:
                    px = float(trade_df.loc[code, "close"])
                elif code in trade_df.index and float(trade_df.loc[code, "open"]) > 0:
                    px = float(trade_df.loc[code, "open"])
                else:
                    px = pos.avg_cost
                close_mv += px * pos.shares
                hold_desc.append(f"{code}:{pos.shares}@{px:.2f}")

            equity = self.cash + close_mv
            equity_series.append((trade_date, equity))
            pos_ratio = close_mv / equity if equity > 0 else 0.0
            self.holds.append(
                {
                    "date": str(trade_date.date()),
                    "cash": round(self.cash, 2),
                    "market_value": round(close_mv, 2),
                    "equity": round(equity, 2),
                    "position_ratio": round(pos_ratio, 4),
                    "mode": mode,
                    "signal_drawdown": round(signal_dd, 4),
                    "breadth": round(breadth, 4),
                    "freeze_left": int(self.freeze_left),
                    "holdings": "|".join(sorted(hold_desc)),
                }
            )
            self.curve.append({"date": str(trade_date.date()), "equity": round(equity, 2)})
            self.decisions.append(
                {
                    "signal_date": str(signal_date.date()),
                    "trade_date": str(trade_date.date()),
                    "mode": mode,
                    "breadth": breadth,
                    "signal_drawdown": signal_dd,
                    "target_codes": "|".join(scored["code"].head(max_holdings).tolist()) if not scored.empty else "",
                    "sell_plan_codes": "|".join(sorted(sell_plan.keys())),
                    "sold_today": "|".join(sorted(sold_today)),
                    "buy_candidates": "|".join(candidate_codes),
                }
            )

            if self.freeze_left > 0:
                self.freeze_left -= 1

        return self._build_outputs(trade_count=trade_count, sell_count=sell_count)

    def _build_outputs(self, trade_count: int, sell_count: int) -> Dict[str, Any]:
        curve_df = pd.DataFrame(self.curve)
        trades_df = pd.DataFrame(self.trades)
        decisions_df = pd.DataFrame(self.decisions)
        holdings_df = pd.DataFrame(self.holds)

        if curve_df.empty:
            summary = {
                "start_date": self.start_date,
                "end_date": self.end_date,
                "initial_capital": self.initial_capital,
                "final_equity": self.initial_capital,
                "total_return": 0.0,
                "annualized_return": 0.0,
                "max_drawdown": 0.0,
                "sharpe": 0.0,
                "trade_count": 0,
                "sell_count": 0,
                "win_rate": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_loss_ratio": None,
                "trading_days": 0,
                "universe_size": int(self.hist["code"].nunique()) if not self.hist.empty else 0,
                "assumptions": {
                    "strategy_version": STRATEGY_VERSION,
                    "execution": "signal_close_t_rebalance_raw_rebuild_v3",
                    "t_plus_1": True,
                    "no_same_day_rebuy": True,
                    "non_mandatory_daily_trading": True,
                    "survivorship_bias": "current_stock_master_filters_only; no artifact reuse",
                    "params": self.params.__dict__,
                },
            }
            return {"summary": summary, "curve_df": curve_df, "trades_df": trades_df, "decisions_df": decisions_df, "holdings_df": holdings_df}

        eq = curve_df["equity"].astype(float)
        total_return = float(eq.iloc[-1] / self.initial_capital - 1.0)
        daily_ret = eq.pct_change().fillna(0.0)
        sharpe = float((daily_ret.mean() / daily_ret.std(ddof=0)) * math.sqrt(252)) if len(daily_ret) > 1 and daily_ret.std(ddof=0) > 0 else 0.0
        peak = eq.cummax()
        max_drawdown = float((eq / peak - 1.0).min())
        years = max(len(curve_df) / 252.0, 1 / 252.0)
        annual_return = float((eq.iloc[-1] / self.initial_capital) ** (1.0 / years) - 1.0)

        sell_trades = trades_df[trades_df["side"] == "SELL"].copy() if not trades_df.empty else pd.DataFrame()
        win_rate = float((sell_trades["realized_pnl"].fillna(0) > 0).mean()) if not sell_trades.empty else 0.0
        avg_win = float(sell_trades.loc[sell_trades["realized_pnl"] > 0, "realized_pnl"].mean()) if not sell_trades.empty and (sell_trades["realized_pnl"] > 0).any() else 0.0
        avg_loss = float(-sell_trades.loc[sell_trades["realized_pnl"] < 0, "realized_pnl"].mean()) if not sell_trades.empty and (sell_trades["realized_pnl"] < 0).any() else 0.0

        summary = {
            "strategy_version": STRATEGY_VERSION,
            "strategy_name": "V3 Audit Raw Rebuild",
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "final_equity": round(float(eq.iloc[-1]), 2),
            "total_return": total_return,
            "annualized_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "trade_count": int(trade_count),
            "sell_count": int(sell_count),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_loss_ratio": float(avg_win / avg_loss) if avg_loss > 0 else None,
            "trading_days": int(len(curve_df)),
            "universe_size": int(self.hist["code"].nunique()) if not self.hist.empty else 0,
            "assumptions": {
                "strategy_version": STRATEGY_VERSION,
                "execution": "signal_close_t_rebalance_raw_rebuild_v3",
                "t_plus_1": True,
                "no_same_day_rebuy": True,
                "non_mandatory_daily_trading": True,
                "survivorship_bias": "current_stock_master_filters_only; no artifact reuse",
                "params": self.params.__dict__,
                "cost_model": {
                    "slippage": SLIPPAGE,
                    "commission": COMMISSION,
                    "stamp_duty_sell": STAMP_DUTY_SELL,
                    "min_commission": MIN_COMMISSION,
                },
                "constraints": {
                    "limit_up_cannot_buy": False,
                    "limit_down_cannot_sell": False,
                    "st_filtered_by_current_master": True,
                },
            },
        }
        return {"summary": summary, "curve_df": curve_df, "trades_df": trades_df, "decisions_df": decisions_df, "holdings_df": holdings_df}


def _plot_curve(curve_df: pd.DataFrame, output_path: Path) -> None:
    if plt is None or curve_df.empty:
        return
    fig, ax = plt.subplots(figsize=(12, 5))
    x = pd.to_datetime(curve_df["date"])
    y = (curve_df["equity"].astype(float) / float(curve_df["equity"].iloc[0]) - 1.0) * 100.0
    ax.plot(x, y, color="#16a34a", linewidth=1.8)
    ax.axhline(0, color="#888", linestyle="--", linewidth=1)
    ax.set_title("V3 Audit Raw Rebuild Equity Curve")
    ax.set_ylabel("Cumulative Return %")
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def _upsert_artifact(strategy_version: str, artifact_key: str, payload: Any) -> None:
    sql = text(
        f"""
        INSERT INTO {ARTIFACT_TABLE}(strategy_version, artifact_key, payload_json, updated_at)
        VALUES(:strategy_version, :artifact_key, :payload_json, NOW())
        ON DUPLICATE KEY UPDATE
            payload_json = VALUES(payload_json),
            updated_at = NOW()
        """
    )
    with db.engine.begin() as conn:
        conn.execute(
            sql,
            {
                "strategy_version": strategy_version,
                "artifact_key": artifact_key,
                "payload_json": json.dumps(payload, ensure_ascii=False, default=_json_default),
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict raw-market V3 audit rerun")
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=END_DATE)
    parser.add_argument("--initial-capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    _ensure_output_dir(output_dir)
    bt = V3AuditBacktester(args.start_date, args.end_date, args.initial_capital, DEFAULT_PARAMS)
    result = bt.run()
    summary = result["summary"]
    curve_df = result["curve_df"]
    trades_df = result["trades_df"]
    decisions_df = result["decisions_df"]
    holdings_df = result["holdings_df"]

    (output_dir / "summary_v3_audit_raw.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    curve_df.to_csv(output_dir / "equity_curve_v3_audit_raw.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "daily_trades_v3_audit_raw.csv", index=False, encoding="utf-8-sig")
    decisions_df.to_csv(output_dir / "daily_decisions_v3_audit_raw.csv", index=False, encoding="utf-8-sig")
    holdings_df.to_csv(output_dir / "daily_holdings_v3_audit_raw.csv", index=False, encoding="utf-8-sig")
    _plot_curve(curve_df, output_dir / "equity_curve_v3_audit_raw.png")

    _upsert_artifact(STRATEGY_VERSION, "summary", summary)
    _upsert_artifact(STRATEGY_VERSION, "curve", curve_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "trades", trades_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "decisions", decisions_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "holdings", holdings_df.to_dict(orient="records"))

    print(json.dumps({"summary": summary, "output_dir": str(output_dir)}, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
