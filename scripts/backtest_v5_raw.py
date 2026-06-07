from __future__ import annotations

import argparse
import ctypes
import gc
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from ctypes import wintypes

import numpy as np
import pandas as pd
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.trading import (
    DEFAULT_ENTRY_MIN_SCORE,
    DEFAULT_KEEP_RANK_MULT,
    MODE_MAX_HOLDINGS,
    _calc_signal_breadth,
    _evaluate_intraday_buy_candidate,
    _evaluate_intraday_sell_candidate,
    _load_intraday_minute_bars,
    _score_candidates_on_signal_day,
    _to_float,
)
from scripts.cost_profiles import available_cost_profiles, cost_profile_note, resolve_cost_profile
from scripts.tushare_history_backfill import TushareHistoryBackfill
from utils.database import db

try:
    import psutil
except Exception:
    psutil = None


OUTPUT_DIR = Path(r"F:\Stock\AiStock\output_v5_raw")
STRATEGY_VERSION = "v5_raw"
ARTIFACT_TABLE = "trading_strategy_artifacts"

BUY_SIGNAL_CUTOFF = "14:45"
SELL_RISK_CUTOFF = "10:00"
SNAPSHOT_COVERAGE_THRESHOLD = 0.98
DEFAULT_MAX_MEMORY_MB = 15_500
DEFAULT_MEMORY_SOFT_CLEANUP_MB = 14_500
DEFAULT_MINUTE_CACHE_KEEP_DAYS = 25
DEFAULT_MINUTE_CACHE_AGGRESSIVE_KEEP_DAYS = 8

DEFAULT_PARAMS = {
    "stop_loss": 0.035,
    "take_profit": 0.10,
    "trailing": 0.05,
    "soft_dd": -0.06,
    "hard_dd": -0.08,
    "freeze_days": 1,
    "breadth_low": 0.50,
    "breadth_high": 0.56,
    "invest_on": 0.65,
    "invest_neutral": 0.40,
    "invest_off": 0.10,
    "min_hold_trend": 3,
    "min_hold_score": 10,
    "entry_min_score": DEFAULT_ENTRY_MIN_SCORE,
    "keep_rank_mult": DEFAULT_KEEP_RANK_MULT,
    "max_holdings": 3,
    "slippage_buy": 0.0020,
    "slippage_sell": 0.0020,
    "commission_buy": 0.0003,
    "commission_sell": 0.0003,
    "stamp_tax_sell": 0.0010,
}


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


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    return str(value)


def _ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _get_process_rss_mb() -> Optional[float]:
    if psutil is None:
        if os.name == "nt":
            try:
                class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                        ("PrivateUsage", ctypes.c_size_t),
                    ]

                counters = PROCESS_MEMORY_COUNTERS_EX()
                counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
                get_memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
                get_memory_info.restype = wintypes.BOOL
                ok = get_memory_info(
                    handle,
                    ctypes.byref(counters),
                    counters.cb,
                )
                if ok:
                    return float(counters.WorkingSetSize / (1024 * 1024))
            except Exception:
                return None
        return None
    try:
        proc = psutil.Process(os.getpid())
        return float(proc.memory_info().rss / (1024 * 1024))
    except Exception:
        return None


def _build_date_ranges(df: pd.DataFrame, date_col: str = "date") -> Dict[str, Tuple[int, int]]:
    ranges: Dict[str, Tuple[int, int]] = {}
    if df.empty or date_col not in df.columns:
        return ranges
    day_keys = df[date_col].dt.strftime("%Y-%m-%d")
    start = 0
    values = day_keys.tolist()
    if not values:
        return ranges
    current = values[0]
    for idx in range(1, len(values)):
        if values[idx] != current:
            ranges[current] = (start, idx)
            start = idx
            current = values[idx]
    ranges[current] = (start, len(values))
    return ranges


def _slice_frame_by_date(df: pd.DataFrame, ranges: Dict[str, Tuple[int, int]], trade_date: str) -> pd.DataFrame:
    span = ranges.get(str(trade_date))
    if not span:
        return pd.DataFrame(columns=df.columns)
    start, end = span
    return df.iloc[start:end]


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


def _resolve_v5_mode(breadth: Optional[float], signal_drawdown: Optional[float], params: Dict[str, Any]) -> str:
    breadth_low = _to_float(params.get("breadth_low"))
    breadth_high = _to_float(params.get("breadth_high"))
    soft_dd = _to_float(params.get("soft_dd"))

    if breadth is None:
        mode = "neutral"
    elif breadth_high is not None and breadth >= breadth_high:
        mode = "on"
    elif breadth_low is not None and breadth <= breadth_low:
        mode = "off"
    else:
        mode = "neutral"

    if mode == "on" and soft_dd is not None and signal_drawdown is not None and signal_drawdown <= soft_dd:
        mode = "neutral"
    return mode


class MinuteAccessor:
    def __init__(self, enable_tushare_fallback: bool = False):
        self.enable_tushare_fallback = bool(enable_tushare_fallback)
        self.backfill = TushareHistoryBackfill() if self.enable_tushare_fallback else None
        self.signal_cache: Dict[Tuple[str, str], pd.DataFrame] = {}
        self.exec_cache: Dict[Tuple[str, str, int, str], pd.DataFrame] = {}
        self.fallback_hits = 0
        self.fallback_writes = 0
        self.min_signal_date, self.max_signal_date = self._load_minute_date_bounds()

    def trim_caches(self, current_trade_date: str, keep_days: int = DEFAULT_MINUTE_CACHE_KEEP_DAYS) -> None:
        try:
            cutoff = str((pd.Timestamp(current_trade_date) - pd.Timedelta(days=max(1, int(keep_days)))).date())
        except Exception:
            return
        self.signal_cache = {
            key: value
            for key, value in self.signal_cache.items()
            if str(key[0]) >= cutoff
        }
        self.exec_cache = {
            key: value
            for key, value in self.exec_cache.items()
            if str(key[1]) >= cutoff
        }

    def clear_all_caches(self) -> None:
        self.signal_cache.clear()
        self.exec_cache.clear()

    def _load_minute_date_bounds(self) -> Tuple[Optional[str], Optional[str]]:
        sql = text("SELECT DATE(MIN(datetime)) AS min_date, DATE(MAX(datetime)) AS max_date FROM kline_minute_15")
        with db.engine.connect() as conn:
            row = conn.execute(sql).fetchone()
        if not row:
            return None, None
        min_date = str(row[0]) if row[0] is not None else None
        max_date = str(row[1]) if row[1] is not None else None
        return min_date, max_date

    def load_signal_snapshot(self, signal_date: str) -> pd.DataFrame:
        key = (signal_date, BUY_SIGNAL_CUTOFF)
        if key in self.signal_cache:
            return self.signal_cache[key]
        if self.min_signal_date and signal_date < self.min_signal_date:
            self.signal_cache[key] = pd.DataFrame()
            return self.signal_cache[key]
        if self.max_signal_date and signal_date > self.max_signal_date:
            self.signal_cache[key] = pd.DataFrame()
            return self.signal_cache[key]
        sql = text(
            """
            WITH ranked AS (
                SELECT
                    SUBSTRING(m.code, 1, 6) AS code,
                    m.datetime,
                    m.close,
                    m.amount,
                    ROW_NUMBER() OVER (PARTITION BY SUBSTRING(m.code, 1, 6) ORDER BY m.datetime DESC) AS rn
                FROM kline_minute_15 m
                JOIN stocks s ON s.code = m.code
                WHERE m.datetime >= :start_dt
                  AND m.datetime <= :end_dt
                  AND s.type = 'stock'
                  AND s.quit = 0
                  AND (s.st = 0 OR s.st IS NULL)
            )
            SELECT
                code,
                MAX(CASE WHEN rn = 1 THEN close END) AS close,
                SUM(amount) AS amount,
                MAX(datetime) AS last_dt
            FROM ranked
            GROUP BY code
            """
        )
        with db.engine.connect() as conn:
            df = pd.read_sql(
                sql,
                conn,
                params={
                    "start_dt": f"{signal_date} 00:00:00",
                    "end_dt": f"{signal_date} {BUY_SIGNAL_CUTOFF}:59",
                },
            )
        if not df.empty:
            for col in ["close", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        self.signal_cache[key] = df
        return df

    def prime_day_bars(self, codes: List[str], signal_date: str, periods: List[int], cutoff_time: str) -> None:
        unique_codes = sorted({str(code or "").strip()[:6] for code in (codes or []) if str(code or "").strip()})
        if not unique_codes:
            return
        if self.min_signal_date and signal_date < self.min_signal_date:
            return
        if self.max_signal_date and signal_date > self.max_signal_date:
            return

        for period in periods:
            cache_missing = [
                code
                for code in unique_codes
                if (code, str(signal_date), int(period), str(cutoff_time)) not in self.exec_cache
            ]
            if not cache_missing:
                continue

            table_name = {15: "kline_minute_15", 30: "kline_minute_30"}.get(int(period))
            if not table_name:
                continue
            placeholders = ",".join([f":code_{i}" for i in range(len(cache_missing))])
            sql = text(
                f"""
                SELECT
                    SUBSTRING(code, 1, 6) AS short_code,
                    datetime,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    amount
                FROM {table_name}
                WHERE SUBSTRING(code, 1, 6) IN ({placeholders})
                  AND datetime >= :start_dt
                  AND datetime <= :cutoff_dt
                ORDER BY short_code ASC, datetime ASC
                """
            )
            params: Dict[str, Any] = {
                "start_dt": f"{signal_date} 00:00:00",
                "cutoff_dt": f"{signal_date} {cutoff_time}:59",
            }
            for idx, code in enumerate(cache_missing):
                params[f"code_{idx}"] = code
            with db.engine.connect() as conn:
                df = pd.read_sql(sql, conn, params=params)
            if df.empty:
                for code in cache_missing:
                    self.exec_cache[(code, str(signal_date), int(period), str(cutoff_time))] = pd.DataFrame()
                continue
            for col in ("open", "high", "low", "close", "volume", "amount"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).reset_index(drop=True)
            grouped = {str(code): grp.drop(columns=["short_code"]).reset_index(drop=True) for code, grp in df.groupby("short_code")}
            for code in cache_missing:
                self.exec_cache[(code, str(signal_date), int(period), str(cutoff_time))] = grouped.get(code, pd.DataFrame())

    def load_bars(self, code: str, signal_date: str, period: int, cutoff_time: str) -> pd.DataFrame:
        cache_key = (str(code), str(signal_date), int(period), str(cutoff_time))
        if cache_key in self.exec_cache:
            return self.exec_cache[cache_key]
        if self.min_signal_date and signal_date < self.min_signal_date:
            self.exec_cache[cache_key] = pd.DataFrame()
            return self.exec_cache[cache_key]
        if self.max_signal_date and signal_date > self.max_signal_date:
            self.exec_cache[cache_key] = pd.DataFrame()
            return self.exec_cache[cache_key]
        df = _load_intraday_minute_bars(code=code, signal_date=signal_date, period=period, cutoff_time=cutoff_time)
        if (df is None or df.empty) and self.enable_tushare_fallback and self.backfill is not None:
            period_text = {15: "15m", 30: "30m"}.get(int(period))
            if period_text:
                try:
                    fetched = self.backfill.fetch_minute(
                        code=code,
                        period=period_text,
                        start_dt=f"{signal_date} 09:00:00",
                        end_dt=f"{signal_date} 15:00:00",
                    )
                    if fetched is not None and not fetched.empty:
                        self.fallback_hits += 1
                        self.fallback_writes += int(self.backfill.persist(code=code, period=period_text, df=fetched))
                        df = _load_intraday_minute_bars(
                            code=code, signal_date=signal_date, period=period, cutoff_time=cutoff_time
                        )
                except Exception:
                    pass
        self.exec_cache[cache_key] = df if df is not None else pd.DataFrame()
        return self.exec_cache[cache_key]


class V5RawBacktester:
    def __init__(
        self,
        start_date: str,
        end_date: str,
        initial_capital: float,
        enable_tushare_fallback: bool = False,
        max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
        cost_profile: str = "current",
        cost_overrides: Optional[Dict[str, Optional[float]]] = None,
    ):
        self.start_date = str(start_date)
        self.end_date = str(end_date)
        self.initial_capital = float(initial_capital)
        self.max_memory_mb = int(max_memory_mb)
        self.memory_soft_cleanup_mb = min(int(max_memory_mb), DEFAULT_MEMORY_SOFT_CLEANUP_MB)
        self.minute_cache_keep_days = DEFAULT_MINUTE_CACHE_KEEP_DAYS
        self.minute_cache_aggressive_keep_days = DEFAULT_MINUTE_CACHE_AGGRESSIVE_KEEP_DAYS
        self.runtime_peak_rss_mb = 0.0
        self.params = dict(DEFAULT_PARAMS)
        self.cost_profile = str(cost_profile or "current").strip().lower()
        self.params.update(resolve_cost_profile(self.cost_profile, cost_overrides))
        self.minute = MinuteAccessor(enable_tushare_fallback=enable_tushare_fallback)
        self.trade_dates = self._load_trade_dates()
        self.daily_df = self._load_daily_history()
        self.universe_size = int(self.daily_df["code"].nunique()) if not self.daily_df.empty else 0
        self.daily_signal_df = (
            self.daily_df.sort_values(["date", "code"]).reset_index(drop=True)
            if not self.daily_df.empty
            else self.daily_df.copy()
        )
        self.daily_date_ranges = _build_date_ranges(self.daily_signal_df, "date")
        self.daily_scored_df, self.daily_scored_ranges = self._precompute_daily_scores()
        self.daily_breadth_by_date = self._precompute_daily_breadth()
        self.open_map = self._build_price_map("open")
        self.close_map = self._build_price_map("close")
        self.prev_close_map = self._build_price_map("prev_close")
        self.ma10_map = self._build_price_map("ma10")

        self.cash = float(initial_capital)
        self.positions: Dict[str, Position] = {}
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

    def _load_trade_dates(self) -> List[str]:
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
        with db.engine.connect() as conn:
            rows = conn.execute(sql, {"start_date": self.start_date, "end_date": self.end_date}).fetchall()
        return [str(row[0]) for row in rows]

    def _load_daily_history(self) -> pd.DataFrame:
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
        df["close_sum_prev9"] = g["close"].shift(1).rolling(9).sum().reset_index(level=0, drop=True)
        df["amount_sum_prev19"] = g["amount"].shift(1).rolling(19).sum().reset_index(level=0, drop=True)
        df["amt5_prev"] = g["amount"].shift(1).rolling(5).mean().reset_index(level=0, drop=True)
        df["ret1_daily"] = df["close"] / df["prev_close"] - 1.0
        df["ret_prev9_sum"] = g["ret1_daily"].shift(1).rolling(9).sum().reset_index(level=0, drop=True)
        df["ret_prev9_sumsq"] = (
            (df["ret1_daily"] ** 2).groupby(df["code"]).shift(1).rolling(9).sum().reset_index(level=0, drop=True)
        )
        df["ret1"] = df["ret1_daily"]
        df["mom5"] = df["close"] / df["close_lag5"] - 1.0
        df["mom10"] = df["close"] / df["close_lag10"] - 1.0
        df["ma10"] = g["close"].rolling(10).mean().reset_index(level=0, drop=True)
        df["amt20"] = g["amount"].rolling(20).mean().reset_index(level=0, drop=True)
        df["vol_ratio"] = df["amount"] / df["amt5_prev"]
        df["vol10"] = g["ret1_daily"].rolling(10).std().reset_index(level=0, drop=True)
        df["close_gt_ma10"] = df["close"] > df["ma10"]
        df["signal_source"] = "daily_eod"
        return df

    def _build_price_map(self, field: str) -> Dict[str, Dict[str, float]]:
        mapping: Dict[str, Dict[str, float]] = {}
        if self.daily_df.empty:
            return mapping
        for _, row in self.daily_df[["date", "code", field]].dropna().iterrows():
            mapping.setdefault(str(pd.Timestamp(row["date"]).date()), {})[str(row["code"])] = float(row[field])
        return mapping

    @staticmethod
    def _infer_limit_pct(code: str) -> float:
        code = str(code or "").strip()
        if code.startswith(("300", "688")):
            return 0.20
        if code.startswith(("43", "83", "87", "92")):
            return 0.30
        return 0.10

    def _precompute_daily_scores(self) -> Dict[str, pd.DataFrame]:
        if self.daily_df.empty:
            return pd.DataFrame(), {}
        d = self.daily_df.copy().replace([np.inf, -np.inf], np.nan)
        d = d.dropna(subset=["code", "mom5", "mom10", "vol_ratio", "amt20", "vol10", "ma10", "close"])
        d = d[
            (d["mom5"] > 0)
            & (d["close_gt_ma10"])
            & (d["amt20"] >= 2e8)
            & (d["vol10"] >= 0.008)
            & (d["vol10"] <= 0.09)
        ].copy()
        if d.empty:
            return pd.DataFrame(), {}
        for col in ["mom5", "mom10", "vol_ratio", "amt20"]:
            d[f"r_{col}"] = d.groupby("date")[col].rank(pct=True)
        d["r_vol10_low"] = 1 - d.groupby("date")["vol10"].rank(pct=True)
        d["score"] = (
            0.35 * d["r_mom5"]
            + 0.20 * d["r_mom10"]
            + 0.20 * d["r_vol_ratio"]
            + 0.20 * d["r_amt20"]
            + 0.05 * d["r_vol10_low"]
        )
        d["score_rank"] = d.groupby("date")["score"].rank(method="first", ascending=False).astype(int)
        d = d.sort_values(["date", "score_rank", "code"]).reset_index(drop=True)
        return d, _build_date_ranges(d, "date")

    def _precompute_daily_breadth(self) -> Dict[str, float]:
        if self.daily_df.empty:
            return {}
        d = self.daily_df.copy()
        d = d.dropna(subset=["close", "ma10"])
        d = d[d["amt20"].fillna(0) >= 2e8]
        if d.empty:
            return {}
        breadth = d.groupby("date").apply(lambda frame: float((frame["close"] > frame["ma10"]).mean()))
        return {str(pd.Timestamp(idx).date()): float(value) for idx, value in breadth.items()}

    def _signal_day_snapshot(self, trade_date: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        base_df = _slice_frame_by_date(self.daily_signal_df, self.daily_date_ranges, trade_date).copy()
        if base_df.empty:
            return base_df, {"signal_source": "missing", "snapshot_coverage": 0.0, "snapshot_count": 0}
        snapshot = self.minute.load_signal_snapshot(trade_date)
        coverage = float(len(snapshot) / self.universe_size) if self.universe_size > 0 else 0.0
        metadata = {
            "signal_source": "daily_eod_fallback",
            "snapshot_coverage": round(coverage, 4),
            "snapshot_count": int(len(snapshot)),
        }
        if snapshot.empty or coverage < SNAPSHOT_COVERAGE_THRESHOLD:
            return base_df, metadata

        snap = snapshot.copy()
        snap["code"] = snap["code"].astype(str).str[:6]
        day_df = base_df.merge(
            snap[["code", "close", "amount"]].rename(columns={"close": "close_snapshot", "amount": "amount_snapshot"}),
            on="code",
            how="left",
        )
        day_df["close_signal"] = day_df["close_snapshot"].where(day_df["close_snapshot"].notna(), day_df["close"])
        day_df["amount_signal"] = day_df["amount_snapshot"].where(day_df["amount_snapshot"].notna(), day_df["amount"])
        day_df["ret1"] = day_df["close_signal"] / day_df["prev_close"] - 1.0
        day_df["mom5"] = day_df["close_signal"] / day_df["close_lag5"] - 1.0
        day_df["mom10"] = day_df["close_signal"] / day_df["close_lag10"] - 1.0
        day_df["ma10"] = (day_df["close_sum_prev9"] + day_df["close_signal"]) / 10.0
        day_df["amt20"] = (day_df["amount_sum_prev19"] + day_df["amount_signal"]) / 20.0
        day_df["vol_ratio"] = day_df["amount_signal"] / day_df["amt5_prev"]
        mean10 = (day_df["ret_prev9_sum"] + day_df["ret1"]) / 10.0
        var10 = ((day_df["ret_prev9_sumsq"] + (day_df["ret1"] ** 2)) / 10.0) - (mean10 ** 2)
        day_df["vol10"] = np.sqrt(np.maximum(var10, 0.0))
        day_df["close"] = day_df["close_signal"]
        day_df["amount"] = day_df["amount_signal"]
        day_df["close_gt_ma10"] = day_df["close"] > day_df["ma10"]
        day_df["signal_source"] = "intraday_1445"
        metadata["signal_source"] = "intraday_1445"
        return day_df, metadata

    def _latest_price(self, code: str, trade_date: str) -> Optional[float]:
        return self.close_map.get(trade_date, {}).get(code)

    def _next_trade_date(self, trade_date: str) -> Optional[str]:
        try:
            idx = self.trade_dates.index(trade_date)
        except ValueError:
            return None
        return self.trade_dates[idx + 1] if idx + 1 < len(self.trade_dates) else None

    def _compute_equity(self, trade_date: str) -> Tuple[float, float]:
        market_value = 0.0
        for code, pos in self.positions.items():
            px = self._latest_price(code, trade_date) or pos.avg_cost
            market_value += float(px) * int(pos.shares)
        equity = float(self.cash) + float(market_value)
        return equity, market_value

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

    def _apply_buy(self, code: str, name: str, trade_date: str, raw_price: float, budget: float, reason: str, exec_source: str) -> bool:
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
                "signal_date": trade_date,
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
        prev_close = self.prev_close_map.get(trade_date, {}).get(code)
        if prev_close is None or prev_close <= 0:
            return None, None
        pct = self._infer_limit_pct(code)
        return round(prev_close * (1.0 + pct), 3), round(prev_close * (1.0 - pct), 3)

    def _manage_runtime_memory(self, trade_date: str, force: bool = False) -> None:
        rss = _get_process_rss_mb()
        if rss is not None:
            self.runtime_peak_rss_mb = max(self.runtime_peak_rss_mb, rss)
        self.minute.trim_caches(trade_date, keep_days=self.minute_cache_keep_days)
        if force or (rss is not None and rss >= self.memory_soft_cleanup_mb):
            self.minute.trim_caches(trade_date, keep_days=self.minute_cache_aggressive_keep_days)
            gc.collect()
            rss = _get_process_rss_mb()
            if rss is not None:
                self.runtime_peak_rss_mb = max(self.runtime_peak_rss_mb, rss)
        if rss is not None and rss > self.max_memory_mb:
            self.minute.clear_all_caches()
            gc.collect()
            rss = _get_process_rss_mb()
            if rss is not None:
                self.runtime_peak_rss_mb = max(self.runtime_peak_rss_mb, rss)
            if rss is not None and rss > self.max_memory_mb:
                raise MemoryError(
                    f"v5_raw memory guard exceeded: rss={rss:.1f}MB > limit={self.max_memory_mb}MB"
                )

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
        if codes:
            target_codes = {str(code or "").strip() for code in codes if str(code or "").strip()}
            if not target_codes:
                return result
            working = scored_df[scored_df["code"].astype(str).isin(target_codes)]
        else:
            working = scored_df
        for _, row in working.iterrows():
            result[str(row["code"])] = {
                "score": _to_float(row.get("score")),
                "rank": int(row.get("score_rank") or 0),
                "name": str(row.get("name") or ""),
                "ma10": _to_float(row.get("ma10")),
            }
        return result

    def run(self) -> Dict[str, Any]:
        for trade_date in self.trade_dates:
            self._manage_runtime_memory(trade_date)
            for pos in self.positions.values():
                pos.hold_days += 1

            held_codes_start = list(self.positions.keys())
            self.minute.prime_day_bars(
                codes=held_codes_start,
                signal_date=trade_date,
                periods=[15, 30],
                cutoff_time=SELL_RISK_CUTOFF,
            )
            sold_early: List[str] = []
            for code in list(self.positions.keys()):
                pos = self.positions.get(code)
                if pos is None or pos.entry_date == trade_date:
                    continue
                bars15 = self.minute.load_bars(code=code, signal_date=trade_date, period=15, cutoff_time=SELL_RISK_CUTOFF)
                bars30 = self.minute.load_bars(code=code, signal_date=trade_date, period=30, cutoff_time=SELL_RISK_CUTOFF)
                latest_px = _to_float(bars15.iloc[-1].get("close")) if not bars15.empty else None
                if latest_px is None or self._blocked_by_limit("SELL", code, trade_date, latest_px):
                    continue
                stop_hit = latest_px <= pos.avg_cost * (1.0 - float(self.params["stop_loss"]))
                tp_hit = latest_px >= pos.avg_cost * (1.0 + float(self.params["take_profit"]))
                if tp_hit:
                    pos.take_profit_reached = True
                    pos.peak_price_after_tp = max(float(pos.peak_price_after_tp or 0.0), float(latest_px))
                if pos.take_profit_reached:
                    pos.peak_price_after_tp = max(float(pos.peak_price_after_tp or 0.0), float(latest_px))
                trailing_hit = bool(
                    pos.take_profit_reached
                    and pos.peak_price_after_tp > 0
                    and latest_px <= pos.peak_price_after_tp * (1.0 - float(self.params["trailing"]))
                )
                signal = _evaluate_intraday_sell_candidate(
                    candidate={
                        "code": code,
                        "name": pos.name,
                        "planned_reason": "stop_loss" if stop_hit else ("trailing_stop" if trailing_hit else ("take_profit" if tp_hit else "sell_plan")),
                        "stop_loss_triggered": stop_hit,
                        "take_profit_reached": tp_hit,
                    },
                    bars15=bars15,
                    bars30=bars30,
                    cutoff_time=SELL_RISK_CUTOFF,
                )
                self.sell_eval_rows.append({"date": trade_date, **signal})
                if stop_hit and signal.get("should_sell"):
                    if self._apply_sell(code=code, trade_date=trade_date, raw_price=latest_px, shares=pos.shares, reason="stop_loss", exec_source="minute_10:00"):
                        sold_early.append(code)
                        continue
                if tp_hit and not trailing_hit and signal.get("should_sell"):
                    shares = pos.shares if pos.shares < 200 else max(100, (pos.shares // 2 // 100) * 100)
                    if self._apply_sell(code=code, trade_date=trade_date, raw_price=latest_px, shares=shares, reason="take_profit_half", exec_source="minute_10:00"):
                        sold_early.append(code)
                        if code in self.positions:
                            self.positions[code].take_profit_reached = True
                            self.positions[code].peak_price_after_tp = max(float(self.positions[code].peak_price_after_tp or 0.0), float(latest_px))
                elif trailing_hit and signal.get("should_sell"):
                    if self._apply_sell(code=code, trade_date=trade_date, raw_price=latest_px, shares=pos.shares, reason="trailing_stop", exec_source="minute_10:00"):
                        sold_early.append(code)

            equity_before, market_value_before = self._compute_equity(trade_date)
            signal_drawdown = self._signal_drawdown(equity_before)
            hard_breach = signal_drawdown <= float(self.params["hard_dd"])
            if hard_breach and not self.prev_hard_breach:
                self.freeze_left = max(self.freeze_left, int(self.params["freeze_days"]))

            signal_day_df, source_meta = self._signal_day_snapshot(trade_date)
            if source_meta["signal_source"] == "daily_eod_fallback":
                scored_df = _slice_frame_by_date(self.daily_scored_df, self.daily_scored_ranges, trade_date).copy()
                breadth = self.daily_breadth_by_date.get(trade_date)
            else:
                scored_df = _score_candidates_on_signal_day(signal_day_df)
                breadth = _calc_signal_breadth(signal_day_df)
            mode = _resolve_v5_mode(breadth=breadth, signal_drawdown=signal_drawdown, params=self.params)
            max_holdings = MODE_MAX_HOLDINGS.get(mode, 2)
            keep_top_n = int(max_holdings) * int(self.params["keep_rank_mult"])
            target_rows = scored_df.head(max(1, keep_top_n)).copy() if not scored_df.empty else pd.DataFrame()
            target_pool = target_rows["code"].astype(str).tolist() if not target_rows.empty else []
            score_map = self._rank_score_map(scored_df, codes=list(self.positions.keys()))
            invest_ratio = float(self.params[f"invest_{mode}"])

            weakest_first = sorted(
                list(self.positions.keys()),
                key=lambda c: (
                    score_map.get(c, {}).get("rank", 999999),
                    -(score_map.get(c, {}).get("score") or -999.0),
                ),
                reverse=True,
            )
            structural_sells: List[Tuple[str, str]] = []
            for code in weakest_first:
                pos = self.positions.get(code)
                if pos is None or pos.entry_date == trade_date or code in sold_early:
                    continue
                current_close = self.close_map.get(trade_date, {}).get(code)
                ma10 = self.ma10_map.get(trade_date, {}).get(code) or score_map.get(code, {}).get("ma10")
                if pos.hold_days >= int(self.params["min_hold_trend"]) and current_close is not None and ma10 is not None and current_close < ma10:
                    structural_sells.append((code, "trend_break"))
                    continue
                if pos.hold_days >= int(self.params["min_hold_score"]) and code not in target_pool:
                    structural_sells.append((code, "score_drop"))

            equity_mid, market_value_mid = self._compute_equity(trade_date)
            target_mv = equity_mid * invest_ratio
            retained_codes = [c for c in self.positions.keys() if c not in {x[0] for x in structural_sells}]
            if len(retained_codes) > max_holdings or market_value_mid > target_mv * 1.05:
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
                bars15 = self.minute.load_bars(code=code, signal_date=trade_date, period=15, cutoff_time=BUY_SIGNAL_CUTOFF)
                bars30 = self.minute.load_bars(code=code, signal_date=trade_date, period=30, cutoff_time=BUY_SIGNAL_CUTOFF)
                sell_signal = _evaluate_intraday_sell_candidate(
                    candidate={
                        "code": code,
                        "name": pos.name,
                        "planned_reason": reason,
                        "stop_loss_triggered": False,
                        "take_profit_reached": False,
                    },
                    bars15=bars15,
                    bars30=bars30,
                    cutoff_time=BUY_SIGNAL_CUTOFF,
                )
                self.sell_eval_rows.append({"date": trade_date, **sell_signal})
                raw_price = _to_float(sell_signal.get("trigger_price")) or self._latest_price(code, trade_date)
                if raw_price is None or self._blocked_by_limit("SELL", code, trade_date, raw_price):
                    continue
                exec_source = "minute_14:50" if _to_float(sell_signal.get("trigger_price")) is not None else "daily_close_fallback"
                self._apply_sell(code=code, trade_date=trade_date, raw_price=raw_price, shares=pos.shares, reason=reason, exec_source=exec_source)

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

            prefetch_codes = list(self.positions.keys()) + [str(row["code"]) for row in buy_candidates]
            self.minute.prime_day_bars(
                codes=prefetch_codes,
                signal_date=trade_date,
                periods=[15, 30],
                cutoff_time=BUY_SIGNAL_CUTOFF,
            )

            available_budget = max(0.0, min(self.cash, equity_after_sells * invest_ratio - market_value_after_sells))
            actual_buys: List[str] = []
            blocked_buys: List[str] = []
            if not buy_block_today and available_budget > 0 and buy_candidates:
                per_slot_budget = available_budget / len(buy_candidates)
                for row in buy_candidates:
                    code = str(row["code"])
                    name = str(row.get("name") or "")
                    bars15 = self.minute.load_bars(code=code, signal_date=trade_date, period=15, cutoff_time=BUY_SIGNAL_CUTOFF)
                    bars30 = self.minute.load_bars(code=code, signal_date=trade_date, period=30, cutoff_time=BUY_SIGNAL_CUTOFF)
                    buy_signal = _evaluate_intraday_buy_candidate(
                        candidate={
                            "code": code,
                            "name": name,
                            "score_total": _to_float(row.get("score")),
                            "score_rank": int(row.get("score_rank") or 0),
                        },
                        bars15=bars15,
                        bars30=bars30,
                        cutoff_time=BUY_SIGNAL_CUTOFF,
                    )
                    self.buy_eval_rows.append({"date": trade_date, **buy_signal})
                    raw_price = _to_float(buy_signal.get("trigger_price")) or self._latest_price(code, trade_date)
                    if raw_price is None or self._blocked_by_limit("BUY", code, trade_date, raw_price):
                        blocked_buys.append(code)
                        continue
                    exec_source = "minute_14:50" if _to_float(buy_signal.get("trigger_price")) is not None else "daily_close_fallback"
                    if self._apply_buy(code=code, name=name, trade_date=trade_date, raw_price=raw_price, budget=per_slot_budget, reason="target_entry", exec_source=exec_source):
                        actual_buys.append(code)

            equity_close, market_value_close = self._compute_equity(trade_date)
            position_ratio = market_value_close / equity_close if equity_close > 0 else 0.0
            self.decision_rows.append(
                {
                    "trade_date": trade_date,
                    "signal_date": trade_date,
                    "signal_source": source_meta["signal_source"],
                    "snapshot_coverage": source_meta["snapshot_coverage"],
                    "snapshot_count": source_meta["snapshot_count"],
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
                    "holdings": "|".join(
                        f"{code}:{pos.shares}@{(self._latest_price(code, trade_date) or pos.avg_cost):.2f}"
                        for code, pos in sorted(self.positions.items())
                    ),
                }
            )
            self.signal_source_rows.append({"date": trade_date, **source_meta})
            self.equity_curve.append(
                {
                    "date": trade_date,
                    "equity": round(equity_close, 2),
                    "cash": round(self.cash, 2),
                    "market_value": round(market_value_close, 2),
                    "position_ratio": round(position_ratio, 4),
                }
            )
            self.prev_hard_breach = bool(hard_breach_after)
            if self.freeze_left > 0:
                self.freeze_left = max(0, self.freeze_left - 1)
            self._manage_runtime_memory(trade_date, force=(trade_date == self.trade_dates[-1]))

        return self._build_outputs()

    def _tail_execution_attribution(self, trades_df: pd.DataFrame) -> Dict[str, Any]:
        if trades_df.empty:
            return {"buy_vs_next_open_bps": None, "sell_vs_next_open_bps": None}
        buy_bp: List[float] = []
        sell_bp: List[float] = []
        for _, row in trades_df.iterrows():
            trade_date = str(row["date"])
            code = str(row["code"])
            next_date = self._next_trade_date(trade_date)
            next_open = self.open_map.get(next_date or "", {}).get(code) if next_date else None
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

    def _build_outputs(self) -> Dict[str, Any]:
        curve_df = pd.DataFrame(self.equity_curve)
        trades_df = pd.DataFrame(self.trade_rows)
        decisions_df = pd.DataFrame(self.decision_rows)
        holdings_df = pd.DataFrame(self.holding_rows)
        signal_source_df = pd.DataFrame(self.signal_source_rows)

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
        intraday_signal_days = int((signal_source_df["signal_source"] == "intraday_1445").sum()) if not signal_source_df.empty else 0
        daily_fallback_days = int((signal_source_df["signal_source"] == "daily_eod_fallback").sum()) if not signal_source_df.empty else 0

        normalized_sharpe = None
        if not curve_df.empty and not holdings_df.empty:
            merged = curve_df[["date", "equity"]].merge(holdings_df[["date", "position_ratio"]], on="date", how="left")
            merged["strategy_ret"] = merged["equity"].pct_change().fillna(0.0)
            base_exp = merged["position_ratio"].shift(1).fillna(0.0).clip(lower=0.1, upper=1.0)
            merged["normalized_ret"] = merged["strategy_ret"] / base_exp
            if merged["normalized_ret"].std(ddof=0) > 0:
                normalized_sharpe = float((merged["normalized_ret"].mean() / merged["normalized_ret"].std(ddof=0)) * math.sqrt(252))

        summary = {
            "strategy_version": STRATEGY_VERSION,
            "strategy_name": "V5 Raw Full-Market Rebuild",
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
            "signal_days_intraday": intraday_signal_days,
            "signal_days_daily_fallback": daily_fallback_days,
            "assumptions": {
                "execution": "same_day_14:50_buy + 10:00_risk_sell + 14:50_structural_rebalance",
                "cost_model": {
                    "profile_name": self.cost_profile,
                    "profile_note": cost_profile_note(self.cost_profile),
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
                "memory_guard_mb": self.max_memory_mb,
            },
            "runtime": {
                "peak_rss_mb": round(self.runtime_peak_rss_mb, 2) if self.runtime_peak_rss_mb else None,
                "minute_cache_keep_days": self.minute_cache_keep_days,
                "minute_cache_aggressive_keep_days": self.minute_cache_aggressive_keep_days,
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
                "signal_days_intraday": intraday_signal_days,
                "signal_days_daily_fallback": daily_fallback_days,
                "minute_tushare_fallback_hits": self.minute.fallback_hits,
                "minute_tushare_fallback_writes": self.minute.fallback_writes,
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
            "buy_eval_df": pd.DataFrame(self.buy_eval_rows),
            "sell_eval_df": pd.DataFrame(self.sell_eval_rows),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Raw full-market V5 backtest with stricter costs and attribution")
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", default="2026-04-24")
    parser.add_argument("--initial-capital", type=float, default=100000.0)
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--enable-tushare-minute-fallback", action="store_true")
    parser.add_argument("--max-memory-mb", type=int, default=DEFAULT_MAX_MEMORY_MB)
    parser.add_argument("--cost-profile", choices=available_cost_profiles(), default="current")
    parser.add_argument("--slippage-buy", type=float, default=None)
    parser.add_argument("--slippage-sell", type=float, default=None)
    parser.add_argument("--commission-buy", type=float, default=None)
    parser.add_argument("--commission-sell", type=float, default=None)
    parser.add_argument("--stamp-tax-sell", type=float, default=None)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    _ensure_output_dir(output_dir)

    backtester = V5RawBacktester(
        start_date=args.start_date,
        end_date=args.end_date,
        initial_capital=args.initial_capital,
        enable_tushare_fallback=bool(args.enable_tushare_minute_fallback),
        max_memory_mb=int(args.max_memory_mb),
        cost_profile=args.cost_profile,
        cost_overrides={
            "slippage_buy": args.slippage_buy,
            "slippage_sell": args.slippage_sell,
            "commission_buy": args.commission_buy,
            "commission_sell": args.commission_sell,
            "stamp_tax_sell": args.stamp_tax_sell,
        },
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

    (output_dir / "summary_v5_raw.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )
    (output_dir / "attribution_v5_raw.json").write_text(
        json.dumps(attribution, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )
    curve_df.to_csv(output_dir / "equity_curve_v5_raw.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "daily_trades_v5_raw.csv", index=False, encoding="utf-8-sig")
    decisions_df.to_csv(output_dir / "daily_decisions_v5_raw.csv", index=False, encoding="utf-8-sig")
    holdings_df.to_csv(output_dir / "daily_holdings_v5_raw.csv", index=False, encoding="utf-8-sig")
    signal_source_df.to_csv(output_dir / "signal_source_v5_raw.csv", index=False, encoding="utf-8-sig")
    buy_eval_df.to_csv(output_dir / "buy_eval_v5_raw.csv", index=False, encoding="utf-8-sig")
    sell_eval_df.to_csv(output_dir / "sell_eval_v5_raw.csv", index=False, encoding="utf-8-sig")

    _upsert_artifact(STRATEGY_VERSION, "summary", summary)
    _upsert_artifact(STRATEGY_VERSION, "curve", curve_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "trades", trades_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "decisions", decisions_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "holdings", holdings_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "attribution", attribution)

    print(
        json.dumps(
            {
                "strategy_version": STRATEGY_VERSION,
                "start_date": args.start_date,
                "end_date": args.end_date,
                "total_return": summary["total_return"],
                "sharpe": summary["sharpe"],
                "max_drawdown": summary["max_drawdown"],
                "cost_profile": args.cost_profile,
                "signal_days_intraday": summary["signal_days_intraday"],
                "signal_days_daily_fallback": summary["signal_days_daily_fallback"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
