from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import gc
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from api.trading import MODE_MAX_HOLDINGS, _to_float
from scripts.backtest_v5_raw import (
    DEFAULT_MAX_MEMORY_MB,
    DEFAULT_MEMORY_SOFT_CLEANUP_MB,
    DEFAULT_PARAMS,
    _build_date_ranges,
    _ensure_output_dir,
    _json_default,
    _slice_frame_by_date,
    _upsert_artifact,
)
from utils.database import db
from utils.backtest_result_store import publish_backtest_result
from utils.market_warehouse import clickhouse_query_df

try:
    import psutil
except Exception:
    psutil = None


OUTPUT_ROOT = Path(r"F:\Stock\AiStock\output_v6_daily_baseline")
DEFAULT_BUY_TIMING = "next_open"


def _get_process_rss_mb() -> Optional[float]:
    if psutil is None:
        return None
    try:
        proc = psutil.Process(os.getpid())
        return float(proc.memory_info().rss / (1024 * 1024))
    except Exception:
        return None


def _resolve_mode(breadth: Optional[float], signal_drawdown: Optional[float], params: Dict[str, Any]) -> str:
    soft_dd = _to_float(params.get("soft_dd"))
    breadth_low = _to_float(params.get("breadth_low"))
    breadth_high = _to_float(params.get("breadth_high"))

    mode = "neutral"
    if breadth is not None:
        if breadth_high is not None and breadth >= breadth_high:
            mode = "on"
        elif breadth_low is not None and breadth <= breadth_low:
            mode = "off"
    if signal_drawdown is not None and soft_dd is not None and signal_drawdown <= soft_dd and mode == "on":
        mode = "neutral"
    return mode


@dataclass
class Position:
    code: str
    name: str
    shares: int
    avg_cost: float
    entry_date: str
    hold_days: int = 0
    take_profit_reached: bool = False
    peak_price_after_tp: float = 0.0


@dataclass
class PendingBuy:
    signal_date: str
    exec_date: str
    code: str
    name: str
    budget: float
    reason: str
    exec_source: str


class V6DailyBaselineBacktester:
    def __init__(
        self,
        start_date: str,
        end_date: str,
        initial_capital: float,
        max_memory_mb: int,
        buy_timing: str,
    ):
        self.start_date = str(start_date)
        self.end_date = str(end_date)
        self.initial_capital = float(initial_capital)
        self.params = dict(DEFAULT_PARAMS)
        self.params.update(
            {
                "stop_loss": 0.06,
                "soft_dd": -0.05,
                "hard_dd": -0.08,
                "freeze_days": 1,
                "breadth_low": 0.55,
                "breadth_high": 0.62,
                "invest_on": 0.65,
                "invest_neutral": 0.25,
                "invest_off": 0.00,
                "min_hold_trend": 5,
                "entry_min_score": 0.90,
                "keep_rank_mult": 2,
                "max_holdings": 2,
                "time_exit_days": 10,
            }
        )
        self.max_memory_mb = int(max_memory_mb)
        self.memory_soft_cleanup_mb = min(int(max_memory_mb), DEFAULT_MEMORY_SOFT_CLEANUP_MB)
        self.buy_timing = str(buy_timing or DEFAULT_BUY_TIMING).strip().lower()
        if self.buy_timing not in {"next_open"}:
            raise ValueError(f"Unsupported buy_timing: {self.buy_timing}")
        self.strategy_version = "v6_daily_next_open"
        self.strategy_name = "V6 Daily Next Open Baseline"
        self.runtime_peak_rss_mb = 0.0

        self.trade_dates = self._load_trade_dates()
        self.daily_df = self._load_daily_history()
        self.universe_size = int(self.daily_df["code"].nunique()) if not self.daily_df.empty else 0
        self.daily_signal_df = self.daily_df.sort_values(["date", "code"]).reset_index(drop=True)
        self.daily_date_ranges = _build_date_ranges(self.daily_signal_df, "date")
        self.daily_scored_df, self.daily_scored_ranges = self._precompute_daily_scores()
        self.daily_breadth_by_date = self._precompute_daily_breadth()

        self.cash = float(initial_capital)
        self.positions: Dict[str, Position] = {}
        self.pending_buys: Dict[str, List[PendingBuy]] = {}
        self.equity_curve: List[Dict[str, Any]] = []
        self.trade_rows: List[Dict[str, Any]] = []
        self.decision_rows: List[Dict[str, Any]] = []
        self.holding_rows: List[Dict[str, Any]] = []
        self.signal_source_rows: List[Dict[str, Any]] = []
        self.buy_eval_rows: List[Dict[str, Any]] = []
        self.sell_eval_rows: List[Dict[str, Any]] = []
        self.limit_blocked_buys = 0
        self.limit_blocked_sells = 0
        self.freeze_left = 0
        self.prev_hard_breach = False
        self._daily_lookup_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._daily_open_cache: Dict[str, Dict[str, float]] = {}

    def _manage_runtime_memory(self) -> None:
        rss = _get_process_rss_mb()
        if rss is not None:
            self.runtime_peak_rss_mb = max(self.runtime_peak_rss_mb, rss)
        if rss is not None and rss >= self.memory_soft_cleanup_mb:
            self._daily_lookup_cache.clear()
            self._daily_open_cache.clear()
            gc.collect()
            rss = _get_process_rss_mb()
            if rss is not None:
                self.runtime_peak_rss_mb = max(self.runtime_peak_rss_mb, rss)
        if rss is not None and rss > self.max_memory_mb:
            raise MemoryError(f"{self.strategy_version} memory guard exceeded: rss={rss:.1f}MB > limit={self.max_memory_mb}MB")

    def _load_trade_dates(self) -> List[str]:
        rows = clickhouse_query_df(
            """
            SELECT trade_date
            FROM trade_calendar
            WHERE market = 'SH'
              AND is_trading = 1
              AND trade_date >= ?
              AND trade_date <= ?
            ORDER BY trade_date ASC
            """,
            [self.start_date, self.end_date],
        )
        return rows["trade_date"].astype(str).str[:10].tolist() if not rows.empty else []

    def _load_daily_history(self) -> pd.DataFrame:
        preload_start = str((pd.Timestamp(self.start_date) - pd.Timedelta(days=260)).date())
        if self.data_source == "clickhouse":
            df = clickhouse_query_df(
                """
                SELECT
                    substr(k.code, 1, 6) AS code,
                    s.name AS name,
                    k.trade_date AS date,
                    k.open AS open,
                    k.high AS high,
                    k.low AS low,
                    k.close AS close,
                    k.amount AS amount,
                    k.volume AS volume
                FROM kline_daily k
                JOIN stocks s ON s.code = k.code
                WHERE k.trade_date >= ?::DATE
                  AND k.trade_date <= ?::DATE
                  AND s.type = 'stock'
                  AND s.quit = 0
                  AND (s.st = 0 OR s.st IS NULL)
                ORDER BY code ASC, date ASC
                """,
                [preload_start, self.end_date],
            )
            return self._prepare_daily_history_frame(df)
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
                k.amount AS amount,
                k.volume AS volume
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE k.trade_date >= :start_date
              AND k.trade_date <= :end_date
              AND s.type = 'stock'
              AND s.quit = 0
              AND (s.st = 0 OR s.st IS NULL)
            ORDER BY code ASC, date ASC
            """
        )
        df = pd.read_sql(sql, db.engine, params={"start_date": preload_start, "end_date": self.end_date})
        return self._prepare_daily_history_frame(df)

    @staticmethod
    def _prepare_daily_history_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        for col in ["open", "high", "low", "close", "amount", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "code", "open", "high", "low", "close"]).copy()
        df["amount"] = df["amount"].fillna(0) * 10000.0
        df = df.sort_values(["code", "date"]).reset_index(drop=True)
        g = df.groupby("code", group_keys=False)
        df["prev_close"] = g["close"].shift(1)
        df["close_lag5"] = g["close"].shift(5)
        df["close_lag10"] = g["close"].shift(10)
        df["close_lag20"] = g["close"].shift(20)
        df["close_lag60"] = g["close"].shift(60)
        df["amt5_prev"] = g["amount"].shift(1).rolling(5).mean().reset_index(level=0, drop=True)
        df["ret1_daily"] = df["close"] / df["prev_close"] - 1.0
        df["mom5"] = df["close"] / df["close_lag5"] - 1.0
        df["mom10"] = df["close"] / df["close_lag10"] - 1.0
        df["mom20"] = df["close"] / df["close_lag20"] - 1.0
        df["mom60"] = df["close"] / df["close_lag60"] - 1.0
        df["ma10"] = g["close"].rolling(10).mean().reset_index(level=0, drop=True)
        df["ma20"] = g["close"].rolling(20).mean().reset_index(level=0, drop=True)
        df["ma60"] = g["close"].rolling(60).mean().reset_index(level=0, drop=True)
        df["amt20"] = g["amount"].rolling(20).mean().reset_index(level=0, drop=True)
        df["vol_ratio"] = df["amount"] / df["amt5_prev"]
        df["vol10"] = g["ret1_daily"].rolling(10).std().reset_index(level=0, drop=True)
        df["vol20"] = g["ret1_daily"].rolling(20).std().reset_index(level=0, drop=True)
        df["close_gt_ma10"] = df["close"] > df["ma10"]
        df["close_gt_ma20"] = df["close"] > df["ma20"]
        df["ma20_gt_ma60"] = df["ma20"] > df["ma60"]
        df["high20_prev"] = g["high"].shift(1).rolling(20).max().reset_index(level=0, drop=True)
        df["high60_prev"] = g["high"].shift(1).rolling(60).max().reset_index(level=0, drop=True)
        df["breakout20_ratio"] = df["close"] / df["high20_prev"]
        df["breakout60_ratio"] = df["close"] / df["high60_prev"]
        df["signal_source"] = "daily_close_signal"
        return df

    def _precompute_daily_scores(self) -> Tuple[pd.DataFrame, Dict[str, Tuple[int, int]]]:
        if self.daily_df.empty:
            return pd.DataFrame(), {}
        d = self.daily_df.copy().replace([np.inf, -np.inf], np.nan)
        d = d.dropna(subset=["code", "mom20", "mom60", "amt20", "vol20", "ma20", "ma60", "close", "breakout20_ratio", "breakout60_ratio"])
        d = d[
            (d["close_gt_ma20"])
            & (d["ma20_gt_ma60"])
            & (d["mom20"] >= 0.08)
            & (d["mom20"] <= 0.35)
            & (d["mom60"] >= 0.15)
            & (d["mom60"] <= 1.00)
            & (d["amt20"] >= 8e8)
            & (d["vol20"] >= 0.010)
            & (d["vol20"] <= 0.040)
            & (d["breakout20_ratio"] >= 0.995)
            & (d["breakout60_ratio"] >= 0.97)
            & (d["ret1_daily"] <= 0.060)
        ].copy()
        if d.empty:
            return pd.DataFrame(), {}
        for col in ["mom20", "mom60", "amt20", "breakout20_ratio"]:
            d[f"r_{col}"] = d.groupby("date")[col].rank(pct=True)
        d["r_vol20_low"] = 1 - d.groupby("date")["vol20"].rank(pct=True)
        d["score"] = (
            0.35 * d["r_mom20"]
            + 0.20 * d["r_mom60"]
            + 0.20 * d["r_amt20"]
            + 0.20 * d["r_breakout20_ratio"]
            + 0.05 * d["r_vol20_low"]
        )
        d["score_rank"] = d.groupby("date")["score"].rank(method="first", ascending=False).astype(int)
        d = d.sort_values(["date", "score_rank", "code"]).reset_index(drop=True)
        return d, _build_date_ranges(d, "date")

    def _precompute_daily_breadth(self) -> Dict[str, float]:
        if self.daily_df.empty:
            return {}
        d = self.daily_df.dropna(subset=["close", "ma20"]).copy()
        d = d[d["amt20"].fillna(0) >= 5e8]
        if d.empty:
            return {}
        breadth = d.groupby("date").apply(lambda frame: float((frame["close"] > frame["ma20"]).mean()))
        return {str(pd.Timestamp(idx).date()): float(value) for idx, value in breadth.items()}

    @staticmethod
    def _infer_limit_pct(code: str) -> float:
        code = str(code or "").strip()
        if code.startswith(("300", "688")):
            return 0.20
        if code.startswith(("43", "83", "87", "92")):
            return 0.30
        return 0.10

    def _day_slice(self, trade_date: str) -> pd.DataFrame:
        return _slice_frame_by_date(self.daily_signal_df, self.daily_date_ranges, trade_date).copy()

    def _score_slice(self, trade_date: str) -> pd.DataFrame:
        return _slice_frame_by_date(self.daily_scored_df, self.daily_scored_ranges, trade_date).copy()

    def _day_lookup(self, trade_date: str) -> Dict[str, Dict[str, Any]]:
        cached = self._daily_lookup_cache.get(trade_date)
        if cached is not None:
            return cached
        day_df = self._day_slice(trade_date)
        if day_df.empty:
            cached = {}
        else:
            cached = {
                str(row["code"]): {
                    "name": str(row.get("name") or ""),
                    "open": _to_float(row.get("open")),
                    "close": _to_float(row.get("close")),
                    "prev_close": _to_float(row.get("prev_close")),
                    "ma10": _to_float(row.get("ma10")),
                    "ma20": _to_float(row.get("ma20")),
                    "ma60": _to_float(row.get("ma60")),
                }
                for _, row in day_df.iterrows()
            }
        self._daily_lookup_cache[trade_date] = cached
        return cached

    def _open_lookup(self, trade_date: str) -> Dict[str, float]:
        cached = self._daily_open_cache.get(trade_date)
        if cached is not None:
            return cached
        day_df = self._day_slice(trade_date)
        cached = {str(row["code"]): float(row["open"]) for _, row in day_df[["code", "open"]].dropna().iterrows()} if not day_df.empty else {}
        self._daily_open_cache[trade_date] = cached
        return cached

    def _next_trade_date(self, trade_date: str) -> Optional[str]:
        try:
            idx = self.trade_dates.index(trade_date)
        except ValueError:
            return None
        return self.trade_dates[idx + 1] if idx + 1 < len(self.trade_dates) else None

    def _latest_open(self, code: str, trade_date: str) -> Optional[float]:
        return self._open_lookup(trade_date).get(code)

    def _compute_equity(self, trade_date: str) -> Tuple[float, float]:
        market_value = 0.0
        lookup = self._day_lookup(trade_date)
        for code, pos in self.positions.items():
            px = lookup.get(code, {}).get("close") or pos.avg_cost
            market_value += float(px) * int(pos.shares)
        return float(self.cash) + float(market_value), market_value

    def _signal_drawdown(self, equity_now: float) -> float:
        series = [float(x["equity"]) for x in self.equity_curve if _to_float(x.get("equity")) is not None]
        if equity_now > 0:
            series.append(float(equity_now))
        if not series:
            return 0.0
        arr = np.array(series, dtype=float)
        peak = np.maximum.accumulate(arr)
        return float(arr[-1] / peak[-1] - 1.0) if peak[-1] > 0 else 0.0

    def _fee_ratio(self, side: str) -> float:
        if str(side).upper() == "BUY":
            return float(self.params["commission_buy"])
        return float(self.params["commission_sell"]) + float(self.params["stamp_tax_sell"])

    def _apply_buy(self, code: str, name: str, trade_date: str, signal_date: str, raw_price: float, budget: float, reason: str, exec_source: str) -> bool:
        price = float(raw_price) * (1.0 + float(self.params["slippage_buy"]))
        if price <= 0 or budget <= 0:
            return False
        fee_ratio = self._fee_ratio("BUY")
        lot_cost = price * (1.0 + fee_ratio) * 100.0
        lots = int(budget // lot_cost)
        shares = lots * 100
        if shares <= 0:
            return False
        gross = price * shares
        fee = gross * fee_ratio
        total_cost = gross + fee
        if total_cost > self.cash:
            return False
        self.cash -= total_cost
        pos = self.positions.get(code)
        if pos is None:
            self.positions[code] = Position(code=code, name=name, shares=shares, avg_cost=price, entry_date=trade_date)
        else:
            total_shares = pos.shares + shares
            pos.avg_cost = ((pos.avg_cost * pos.shares) + (price * shares)) / total_shares
            pos.shares = total_shares
            pos.name = name or pos.name
            pos.entry_date = trade_date
        self.trade_rows.append(
            {
                "date": trade_date,
                "signal_date": signal_date,
                "side": "BUY",
                "code": code,
                "name": name,
                "shares": shares,
                "price": round(price, 4),
                "gross": round(gross, 2),
                "fee": round(fee, 2),
                "reason": reason,
                "exec_source": exec_source,
                "realized_pnl": None,
            }
        )
        return True

    def _apply_sell(self, code: str, trade_date: str, raw_price: float, shares: int, reason: str, exec_source: str) -> bool:
        pos = self.positions.get(code)
        if pos is None or shares <= 0:
            return False
        shares = min(int(shares), int(pos.shares))
        if shares <= 0:
            return False
        price = float(raw_price) * (1.0 - float(self.params["slippage_sell"]))
        fee_ratio = self._fee_ratio("SELL")
        gross = price * shares
        fee = gross * fee_ratio
        proceeds = gross - fee
        realized_pnl = (price - pos.avg_cost) * shares - fee
        self.cash += proceeds
        pos.shares -= shares
        if pos.shares <= 0:
            del self.positions[code]
        self.trade_rows.append(
            {
                "date": trade_date,
                "signal_date": trade_date,
                "side": "SELL",
                "code": code,
                "name": pos.name,
                "shares": shares,
                "price": round(price, 4),
                "gross": round(gross, 2),
                "fee": round(fee, 2),
                "reason": reason,
                "exec_source": exec_source,
                "realized_pnl": round(realized_pnl, 2),
            }
        )
        return True

    def _limit_bounds(self, code: str, trade_date: str) -> Tuple[Optional[float], Optional[float]]:
        prev_close = self._day_lookup(trade_date).get(code, {}).get("prev_close")
        if prev_close is None or prev_close <= 0:
            return None, None
        pct = self._infer_limit_pct(code)
        return round(prev_close * (1.0 + pct), 3), round(prev_close * (1.0 - pct), 3)

    def _blocked_by_limit(self, side: str, code: str, trade_date: str, raw_price: Optional[float]) -> bool:
        px = _to_float(raw_price)
        if px is None:
            return False
        limit_up, limit_down = self._limit_bounds(code, trade_date)
        if str(side).upper() == "BUY" and limit_up is not None and px >= limit_up * 0.998:
            self.limit_blocked_buys += 1
            return True
        if str(side).upper() == "SELL" and limit_down is not None and px <= limit_down * 1.002:
            self.limit_blocked_sells += 1
            return True
        return False

    def _rank_score_map(self, scored_df: pd.DataFrame, codes: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        if scored_df is None or scored_df.empty:
            return result
        working = scored_df
        if codes:
            target_codes = {str(code or "").strip() for code in codes if str(code or "").strip()}
            working = scored_df[scored_df["code"].astype(str).isin(target_codes)] if target_codes else scored_df.iloc[0:0]
        for _, row in working.iterrows():
            result[str(row["code"])] = {
                "score": _to_float(row.get("score")),
                "rank": int(row.get("score_rank") or 0),
                "name": str(row.get("name") or ""),
                "ma10": _to_float(row.get("ma10")),
                "ma20": _to_float(row.get("ma20")),
                "ma60": _to_float(row.get("ma60")),
            }
        return result

    def _tail_execution_attribution(self, trades_df: pd.DataFrame) -> Dict[str, Any]:
        if trades_df.empty:
            return {"buy_vs_next_open_bps": None, "sell_vs_next_open_bps": None}
        buy_bp: List[float] = []
        sell_bp: List[float] = []
        for _, row in trades_df.iterrows():
            trade_date = str(row["date"])
            code = str(row["code"])
            next_date = self._next_trade_date(trade_date)
            next_open = self._latest_open(code, next_date or "") if next_date else None
            px = _to_float(row.get("price"))
            if next_open is None or px is None or px <= 0:
                continue
            if str(row["side"]).upper() == "BUY":
                buy_bp.append(float((next_open / px - 1.0) * 10000.0))
            else:
                sell_bp.append(float((px / next_open - 1.0) * 10000.0))
        return {
            "buy_vs_next_open_bps": float(np.mean(buy_bp)) if buy_bp else None,
            "sell_vs_next_open_bps": float(np.mean(sell_bp)) if sell_bp else None,
            "buy_sample_count": len(buy_bp),
            "sell_sample_count": len(sell_bp),
        }

    def _execute_pending_buys_at_open(self, trade_date: str) -> Tuple[List[str], List[str]]:
        actual: List[str] = []
        blocked: List[str] = []
        pending_today = list(self.pending_buys.pop(trade_date, []))
        if not pending_today:
            return actual, blocked
        open_lookup = self._open_lookup(trade_date)
        for item in pending_today:
            if item.code in self.positions:
                continue
            open_px = open_lookup.get(item.code)
            if open_px is None:
                continue
            if self._blocked_by_limit("BUY", item.code, trade_date, open_px):
                blocked.append(item.code)
                continue
            if self._apply_buy(item.code, item.name, trade_date, item.signal_date, float(open_px), item.budget, item.reason, item.exec_source):
                actual.append(item.code)
        return actual, blocked

    def run(self) -> Dict[str, Any]:
        progress_every = 60
        for idx, trade_date in enumerate(self.trade_dates, start=1):
            self._manage_runtime_memory()
            for pos in self.positions.values():
                pos.hold_days += 1

            open_actual_buys: List[str] = []
            open_blocked_buys: List[str] = []
            if self.buy_timing == "next_open":
                open_actual_buys, open_blocked_buys = self._execute_pending_buys_at_open(trade_date)

            day_lookup = self._day_lookup(trade_date)
            scored_df = self._score_slice(trade_date)
            breadth = self.daily_breadth_by_date.get(trade_date)

            equity_before, _ = self._compute_equity(trade_date)
            signal_drawdown = self._signal_drawdown(equity_before)
            hard_breach = signal_drawdown <= float(self.params["hard_dd"])
            if hard_breach and not self.prev_hard_breach:
                self.freeze_left = max(self.freeze_left, int(self.params["freeze_days"]))

            mode = _resolve_mode(breadth=breadth, signal_drawdown=signal_drawdown, params=self.params)
            max_holdings = {"on": 2, "neutral": 1, "off": 0}.get(mode, 1)
            keep_top_n = int(max_holdings) * int(self.params["keep_rank_mult"])
            target_rows = scored_df.head(max(1, keep_top_n)).copy() if not scored_df.empty else pd.DataFrame()
            target_pool = target_rows["code"].astype(str).tolist() if not target_rows.empty else []
            score_map = self._rank_score_map(scored_df, codes=list(self.positions.keys()))
            invest_ratio = float(self.params[f"invest_{mode}"])

            sold_early: List[str] = []
            for code in list(self.positions.keys()):
                pos = self.positions.get(code)
                if pos is None or pos.entry_date == trade_date:
                    continue
                latest_px = day_lookup.get(code, {}).get("close")
                if latest_px is None or self._blocked_by_limit("SELL", code, trade_date, latest_px):
                    continue
                stop_hit = latest_px <= pos.avg_cost * (1.0 - float(self.params["stop_loss"]))
                if stop_hit:
                    self.sell_eval_rows.append({"date": trade_date, "code": code, "name": pos.name, "action": "SELL", "reason": "触发止损", "trigger_price": latest_px})
                    if self._apply_sell(code, trade_date, latest_px, pos.shares, "stop_loss", "daily_close_sell"):
                        sold_early.append(code)
                        continue

            weakest_first = sorted(
                list(self.positions.keys()),
                key=lambda c: (score_map.get(c, {}).get("rank", 999999), -(score_map.get(c, {}).get("score") or -999.0)),
                reverse=True,
            )
            structural_sells: List[Tuple[str, str]] = []
            for code in weakest_first:
                pos = self.positions.get(code)
                if pos is None or pos.entry_date == trade_date or code in sold_early:
                    continue
                current_close = day_lookup.get(code, {}).get("close")
                ma20 = day_lookup.get(code, {}).get("ma20") or score_map.get(code, {}).get("ma20")
                if pos.hold_days >= int(self.params["min_hold_trend"]) and current_close is not None and ma20 is not None and current_close < ma20:
                    structural_sells.append((code, "trend_break"))
                    continue
                if pos.hold_days >= int(self.params["time_exit_days"]):
                    structural_sells.append((code, "time_exit"))

            equity_mid, market_value_mid = self._compute_equity(trade_date)
            target_mv = equity_mid * invest_ratio
            retained_codes = [c for c in self.positions.keys() if c not in {x[0] for x in structural_sells}]
            if len(retained_codes) > max_holdings or market_value_mid > target_mv * 1.10:
                for code in weakest_first:
                    if code in {x[0] for x in structural_sells} or code in sold_early:
                        continue
                    if code in self.positions:
                        structural_sells.append((code, "portfolio_cut"))
                        retained_codes = [c for c in retained_codes if c != code]
                        if len(retained_codes) <= max_holdings:
                            break

            for code, reason in structural_sells:
                pos = self.positions.get(code)
                if pos is None:
                    continue
                raw_price = day_lookup.get(code, {}).get("close")
                if raw_price is None or self._blocked_by_limit("SELL", code, trade_date, raw_price):
                    continue
                reason_text = {
                    "trend_break": "持有后跌破 MA20，趋势转弱卖出",
                    "portfolio_cut": "组合超配或持仓数量超限，去弱留强",
                    "time_exit": "达到最长持有天数，时间止盈/退出",
                }.get(reason, reason)
                self.sell_eval_rows.append({"date": trade_date, "code": code, "name": pos.name, "action": "SELL", "reason": reason_text, "trigger_price": raw_price})
                self._apply_sell(code, trade_date, raw_price, pos.shares, reason, "daily_close_sell")

            equity_after_sells, market_value_after_sells = self._compute_equity(trade_date)
            signal_drawdown_after = self._signal_drawdown(equity_after_sells)
            hard_breach_after = signal_drawdown_after <= float(self.params["hard_dd"])
            if hard_breach_after and not self.prev_hard_breach:
                self.freeze_left = max(self.freeze_left, int(self.params["freeze_days"]))
            buy_block_today = bool(self.freeze_left > 0 or mode == "off")

            held_codes = set(self.positions.keys())
            buy_slots = max(0, max_holdings - len(held_codes))
            buy_candidates: List[pd.Series] = []
            if buy_slots > 0 and not target_rows.empty:
                for _, row in target_rows.iterrows():
                    code = str(row["code"])
                    if code in held_codes:
                        continue
                    score = _to_float(row.get("score"))
                    if score is None or score < float(self.params["entry_min_score"]):
                        continue
                    buy_candidates.append(row)
                    if len(buy_candidates) >= buy_slots:
                        break

            available_budget = max(0.0, min(self.cash, equity_after_sells * invest_ratio - market_value_after_sells))
            actual_buys: List[str] = list(open_actual_buys)
            blocked_buys: List[str] = list(open_blocked_buys)
            if not buy_block_today and available_budget > 0 and buy_candidates:
                per_slot_budget = available_budget / len(buy_candidates)
                for _, row in enumerate(buy_candidates):
                    code = str(row["code"])
                    name = str(row.get("name") or day_lookup.get(code, {}).get("name") or "")
                    raw_price = day_lookup.get(code, {}).get("close")
                    reason_text = (
                        f"V6 日线评分满足入场要求: score={(_to_float(row.get('score')) or 0.0):.3f}, "
                        f"mom20={(_to_float(row.get('mom20')) or 0.0):.3f}, "
                        f"break20={(_to_float(row.get('breakout20_ratio')) or 0.0):.3f}, "
                        f"amt20={(_to_float(row.get('amt20')) or 0.0):.0f}"
                    )
                    self.buy_eval_rows.append({"date": trade_date, "code": code, "name": name, "action": "BUY", "reason": reason_text, "trigger_price": raw_price})
                    next_date = self._next_trade_date(trade_date)
                    if not next_date:
                        continue
                    self.pending_buys.setdefault(next_date, []).append(
                        PendingBuy(
                            signal_date=trade_date,
                            exec_date=next_date,
                            code=code,
                            name=name,
                            budget=float(per_slot_budget),
                            reason="target_entry",
                            exec_source="next_day_open",
                        )
                    )
                    actual_buys.append(f"{code}->{next_date}")

            equity_close, market_value_close = self._compute_equity(trade_date)
            position_ratio = market_value_close / equity_close if equity_close > 0 else 0.0
            self.decision_rows.append(
                {
                    "trade_date": trade_date,
                    "signal_date": trade_date,
                    "signal_source": "daily_close_signal",
                    "snapshot_coverage": 1.0,
                    "snapshot_count": int(len(day_lookup)),
                    "mode": mode,
                    "breadth": breadth,
                    "signal_drawdown": signal_drawdown_after,
                    "freeze_left": self.freeze_left,
                    "buy_block_today": int(buy_block_today),
                    "target_codes": "|".join(target_pool),
                    "buy_candidates": "|".join([str(x["code"]) for x in buy_candidates]),
                    "actual_buys": "|".join(actual_buys),
                    "blocked_buys": "|".join(blocked_buys),
                    "held_codes": "|".join(sorted(self.positions.keys())),
                    "market_value": round(market_value_close, 2),
                    "equity": round(equity_close, 2),
                    "position_ratio": round(position_ratio, 4),
                }
            )
            self.holding_rows.append(
                {
                    "date": trade_date,
                    "cash": round(self.cash, 2),
                    "market_value": round(market_value_close, 2),
                    "equity": round(equity_close, 2),
                    "position_ratio": round(position_ratio, 4),
                    "freeze_left": self.freeze_left,
                    "signal_drawdown": round(signal_drawdown_after, 4),
                    "holdings": "|".join(f"{code}:{pos.shares}@{(day_lookup.get(code, {}).get('close') or pos.avg_cost):.2f}" for code, pos in sorted(self.positions.items())),
                }
            )
            self.signal_source_rows.append({"date": trade_date, "signal_source": "daily_close_signal", "snapshot_coverage": 1.0, "snapshot_count": int(len(day_lookup))})
            self.equity_curve.append({"date": trade_date, "equity": round(equity_close, 2), "cash": round(self.cash, 2), "market_value": round(market_value_close, 2), "position_ratio": round(position_ratio, 4)})

            self.prev_hard_breach = bool(hard_breach_after)
            if self.freeze_left > 0:
                self.freeze_left = max(0, self.freeze_left - 1)

            if idx % 60 == 0:
                print(f"[progress] {self.buy_timing} {trade_date} {idx}/{len(self.trade_dates)} equity={equity_close:.2f} positions={len(self.positions)}")

        return self._build_outputs()

    def _build_outputs(self) -> Dict[str, Any]:
        curve_df = pd.DataFrame(self.equity_curve)
        trades_df = pd.DataFrame(self.trade_rows)
        decisions_df = pd.DataFrame(self.decision_rows)
        holdings_df = pd.DataFrame(self.holding_rows)
        signal_source_df = pd.DataFrame(self.signal_source_rows)
        buy_eval_df = pd.DataFrame(self.buy_eval_rows)
        sell_eval_df = pd.DataFrame(self.sell_eval_rows)

        total_return = 0.0
        annual_return = 0.0
        sharpe = 0.0
        max_drawdown = 0.0
        if not curve_df.empty:
            eq = curve_df["equity"].astype(float)
            total_return = float(eq.iloc[-1] / self.initial_capital - 1.0)
            daily_ret = eq.pct_change().fillna(0.0)
            if len(daily_ret) > 1 and daily_ret.std(ddof=0) > 0:
                sharpe = float((daily_ret.mean() / daily_ret.std(ddof=0)) * math.sqrt(252))
            peak = eq.cummax()
            max_drawdown = float((eq / peak - 1.0).min())
            years = max(len(curve_df) / 252.0, 1 / 252.0)
            annual_return = float((eq.iloc[-1] / self.initial_capital) ** (1.0 / years) - 1.0)

        sell_trades = trades_df[trades_df["side"] == "SELL"].copy() if not trades_df.empty else pd.DataFrame()
        win_rate = float((sell_trades["realized_pnl"].fillna(0) > 0).mean()) if not sell_trades.empty else 0.0
        realized_total = float(sell_trades["realized_pnl"].fillna(0).sum()) if not sell_trades.empty else 0.0
        top5_contribution = 0.0
        if not sell_trades.empty:
            winners = sell_trades[sell_trades["realized_pnl"].fillna(0) > 0].sort_values("realized_pnl", ascending=False)
            positive_pnl = float(winners["realized_pnl"].sum())
            if positive_pnl > 0:
                top5_contribution = float(winners.head(5)["realized_pnl"].sum() / positive_pnl)

        avg_position_ratio = float(holdings_df["position_ratio"].mean()) if not holdings_df.empty else 0.0
        median_position_ratio = float(holdings_df["position_ratio"].median()) if not holdings_df.empty else 0.0
        low_exposure_days = int((holdings_df["position_ratio"] < 0.20).sum()) if not holdings_df.empty else 0
        flat_days = int((holdings_df["position_ratio"] < 0.05).sum()) if not holdings_df.empty else 0

        normalized_sharpe = None
        if not curve_df.empty and not holdings_df.empty:
            merged = curve_df[["date", "equity"]].merge(holdings_df[["date", "position_ratio"]], on="date", how="left")
            merged["strategy_ret"] = merged["equity"].pct_change().fillna(0.0)
            base_exp = merged["position_ratio"].shift(1).fillna(0.0).clip(lower=0.1, upper=1.0)
            merged["normalized_ret"] = merged["strategy_ret"] / base_exp
            if merged["normalized_ret"].std(ddof=0) > 0:
                normalized_sharpe = float((merged["normalized_ret"].mean() / merged["normalized_ret"].std(ddof=0)) * math.sqrt(252))

        summary = {
            "strategy_version": self.strategy_version,
            "strategy_name": self.strategy_name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "final_equity": round(float(curve_df["equity"].iloc[-1]) if not curve_df.empty else self.initial_capital, 2),
            "total_return": total_return,
            "annual_return": annual_return,
            "sharpe": sharpe,
            "max_drawdown": max_drawdown,
            "trade_count": int(len(trades_df)),
            "sell_count": int(len(sell_trades)),
            "win_rate": win_rate,
            "realized_total_pnl": realized_total,
            "universe_size": self.universe_size,
            "trading_days": int(len(curve_df)),
            "signal_days_intraday": 0,
            "signal_days_daily_fallback": int(len(curve_df)),
            "assumptions": {
                "execution": "next_day_open_buy + same_day_close_daily_sell",
                "signal_timing": "daily close signal only, no minute factors",
                "cost_model": {
                    "slippage_buy": self.params["slippage_buy"],
                    "slippage_sell": self.params["slippage_sell"],
                    "commission_buy": self.params["commission_buy"],
                    "commission_sell": self.params["commission_sell"],
                    "stamp_tax_sell": self.params["stamp_tax_sell"],
                },
                "constraints": {
                    "limit_up_cannot_buy": True,
                    "limit_down_cannot_sell": True,
                    "st_excluded": True,
                    "max_holdings": self.params["max_holdings"],
                },
                "params": self.params,
                "minute_factors_enabled": False,
                "memory_guard_mb": self.max_memory_mb,
            },
            "runtime": {
                "peak_rss_mb": round(self.runtime_peak_rss_mb, 2) if self.runtime_peak_rss_mb else None,
            },
        }
        attribution = {
            "tail_execution": self._tail_execution_attribution(trades_df),
            "exposure": {
                "avg_position_ratio": avg_position_ratio,
                "median_position_ratio": median_position_ratio,
                "low_exposure_days_lt_20pct": low_exposure_days,
                "flat_days_lt_5pct": flat_days,
                "normalized_sharpe_exposure_adjusted": normalized_sharpe,
            },
            "concentration": {
                "top5_realized_profit_share": top5_contribution,
                "limit_blocked_buys": self.limit_blocked_buys,
                "limit_blocked_sells": self.limit_blocked_sells,
            },
            "data_coverage": {
                "signal_days_intraday": 0,
                "signal_days_daily_fallback": int(len(curve_df)),
            },
        }
        return {
            "summary": summary,
            "attribution": attribution,
            "curve_df": curve_df,
            "trades_df": trades_df,
            "decisions_df": decisions_df,
            "holdings_df": holdings_df,
            "signal_source_df": signal_source_df,
            "buy_eval_df": buy_eval_df,
            "sell_eval_df": sell_eval_df,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="V6 daily next-open baseline backtest")
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", default="2026-04-24")
    parser.add_argument("--initial-capital", type=float, default=100000.0)
    parser.add_argument("--max-memory-mb", type=int, default=DEFAULT_MAX_MEMORY_MB)
    parser.add_argument("--buy-timing", choices=["next_open"], default=DEFAULT_BUY_TIMING)
    parser.add_argument("--data-source", choices=["clickhouse", "mysql"], default="clickhouse")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    version_tag = "v6_daily_next_open"
    output_dir = Path(args.output_dir) if str(args.output_dir).strip() else (OUTPUT_ROOT / args.buy_timing)
    _ensure_output_dir(output_dir)

    backtester = V6DailyBaselineBacktester(
        start_date=args.start_date,
        end_date=args.end_date,
        initial_capital=args.initial_capital,
        max_memory_mb=int(args.max_memory_mb),
        buy_timing=args.buy_timing,
        data_source=args.data_source,
    )
    result = backtester.run()

    summary = result["summary"]
    attribution = result["attribution"]
    curve_df = result["curve_df"]
    trades_df = result["trades_df"]
    decisions_df = result["decisions_df"]
    holdings_df = result["holdings_df"]
    signal_source_df = result["signal_source_df"]
    buy_eval_df = result["buy_eval_df"]
    sell_eval_df = result["sell_eval_df"]

    (output_dir / f"summary_{version_tag}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    (output_dir / f"attribution_{version_tag}.json").write_text(json.dumps(attribution, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    curve_df.to_csv(output_dir / f"equity_curve_{version_tag}.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / f"daily_trades_{version_tag}.csv", index=False, encoding="utf-8-sig")
    decisions_df.to_csv(output_dir / f"daily_decisions_{version_tag}.csv", index=False, encoding="utf-8-sig")
    holdings_df.to_csv(output_dir / f"daily_holdings_{version_tag}.csv", index=False, encoding="utf-8-sig")
    signal_source_df.to_csv(output_dir / f"signal_source_{version_tag}.csv", index=False, encoding="utf-8-sig")
    buy_eval_df.to_csv(output_dir / f"buy_eval_{version_tag}.csv", index=False, encoding="utf-8-sig")
    sell_eval_df.to_csv(output_dir / f"sell_eval_{version_tag}.csv", index=False, encoding="utf-8-sig")

    run_id = f"{version_tag}_{args.start_date}_{args.end_date}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    store_report = publish_backtest_result(
        run_id=run_id,
        strategy_version=version_tag,
        strategy_name=backtester.strategy_name,
        start_date=args.start_date,
        end_date=args.end_date,
        summary=summary,
        params={
            "buy_timing": args.buy_timing,

            "initial_capital": args.initial_capital,
            "max_memory_mb": args.max_memory_mb,
            "strategy_params": backtester.params,
        },
        frames={
            "curve": curve_df,
            "trades": trades_df,
            "decisions": decisions_df,
            "holdings": holdings_df,
            "signal_source": signal_source_df,
            "buy_eval": buy_eval_df,
            "sell_eval": sell_eval_df,
        },
        output_dir=str(output_dir),
    )

    _upsert_artifact(version_tag, "summary", summary)
    _upsert_artifact(version_tag, "curve", curve_df.to_dict(orient="records"))
    _upsert_artifact(version_tag, "trades", trades_df.to_dict(orient="records"))
    _upsert_artifact(version_tag, "decisions", decisions_df.to_dict(orient="records"))
    _upsert_artifact(version_tag, "holdings", holdings_df.to_dict(orient="records"))
    _upsert_artifact(version_tag, "attribution", attribution)

    print(
        json.dumps(
            {
                "strategy_version": version_tag,
                "buy_timing": args.buy_timing,
    
                "start_date": args.start_date,
                "end_date": args.end_date,
                "total_return": summary["total_return"],
                "sharpe": summary["sharpe"],
                "max_drawdown": summary["max_drawdown"],
                "trading_days": summary["trading_days"],
                "trade_count": summary["trade_count"],
                "storage": store_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
