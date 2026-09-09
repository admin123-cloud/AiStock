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

from api.trading import MODE_MAX_HOLDINGS, _calc_signal_breadth, _score_candidates_on_signal_day, _to_float
from scripts.cost_profiles import cost_profile_note, resolve_cost_profile
from utils.database import db

try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None

OUTPUT_DIR = Path(r"F:\Stock\AiStock\output_v4_audit")
STRATEGY_VERSION = "v4_audit_raw"
ARTIFACT_TABLE = "trading_strategy_artifacts"
DEFAULT_PARAMS = {
    "stop_loss": 0.035,
    "take_profit": 0.10,
    "trailing": 0.05,
    "soft_dd": -0.06,
    "hard_dd": -0.18,
    "freeze_days": 8,
    "breadth_low": 0.50,
    "breadth_high": 0.56,
    "invest_on": 0.85,
    "invest_neutral": 0.55,
    "invest_off": 0.05,
    "min_hold_trend": 3,
    "min_hold_score": 10,
    "entry_min_score": 0.75,
    "keep_rank_mult": 3,
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


class V4AuditBacktester:
    def __init__(
        self,
        start_date: str,
        end_date: str,
        initial_capital: float,
        cost_profile: str = "current",
        cost_overrides: Optional[Dict[str, Optional[float]]] = None,
    ):
        self.start_date = str(start_date)
        self.end_date = str(end_date)
        self.initial_capital = float(initial_capital)
        self.params = dict(DEFAULT_PARAMS)
        self.cost_profile = str(cost_profile or "current").strip().lower()
        self.params.update(resolve_cost_profile(self.cost_profile, cost_overrides))
        self.trade_dates = self._load_trade_dates()
        self.daily_df = self._load_daily_history()
        self.universe_size = int(self.daily_df["code"].nunique()) if not self.daily_df.empty else 0
        self.daily_signal_df = self.daily_df.sort_values(["date", "code"]).reset_index(drop=True)
        self.daily_date_ranges = self._build_date_ranges(self.daily_signal_df)
        self.daily_scored_df, self.daily_scored_ranges = self._precompute_daily_scores()
        self.daily_breadth_by_date = self._precompute_daily_breadth()
        self.close_map = self._build_price_map("close")
        self.prev_close_map = self._build_price_map("prev_close")
        self.ma10_map = self._build_price_map("ma10")
        self.cash = float(initial_capital)
        self.positions: Dict[str, Position] = {}
        self.trade_rows: List[Dict[str, Any]] = []
        self.decision_rows: List[Dict[str, Any]] = []
        self.holding_rows: List[Dict[str, Any]] = []
        self.equity_curve: List[Dict[str, Any]] = []
        self.prev_hard_breach = False
        self.freeze_left = 0
        self.limit_blocked_buys = 0
        self.limit_blocked_sells = 0

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
        df["ret1"] = df["close"] / df["prev_close"] - 1.0
        df["mom5"] = g["close"].pct_change(5)
        df["mom10"] = g["close"].pct_change(10)
        df["ma10"] = g["close"].rolling(10).mean().reset_index(level=0, drop=True)
        df["amt20"] = g["amount"].rolling(20).mean().reset_index(level=0, drop=True)
        df["amt5_prev"] = g["amount"].shift(1).rolling(5).mean().reset_index(level=0, drop=True)
        df["vol_ratio"] = df["amount"] / df["amt5_prev"]
        df["vol10"] = g["ret1"].rolling(10).std().reset_index(level=0, drop=True)
        df["close_gt_ma10"] = df["close"] > df["ma10"]
        return df

    def _build_date_ranges(self, df: pd.DataFrame, date_col: str = "date") -> Dict[str, Tuple[int, int]]:
        ranges: Dict[str, Tuple[int, int]] = {}
        if df.empty:
            return ranges
        keys = df[date_col].dt.strftime("%Y-%m-%d").tolist()
        if not keys:
            return ranges
        start = 0
        current = keys[0]
        for idx in range(1, len(keys)):
            if keys[idx] != current:
                ranges[current] = (start, idx)
                start = idx
                current = keys[idx]
        ranges[current] = (start, len(keys))
        return ranges

    def _slice_frame_by_date(self, df: pd.DataFrame, ranges: Dict[str, Tuple[int, int]], trade_date: str) -> pd.DataFrame:
        span = ranges.get(str(trade_date))
        if not span:
            return pd.DataFrame(columns=df.columns)
        return df.iloc[span[0] : span[1]].copy()

    def _build_price_map(self, field: str) -> Dict[str, Dict[str, float]]:
        mapping: Dict[str, Dict[str, float]] = {}
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

    def _limit_bounds(self, code: str, trade_date: str) -> Tuple[Optional[float], Optional[float]]:
        prev_close = self.prev_close_map.get(trade_date, {}).get(code)
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

    def _precompute_daily_scores(self) -> Tuple[pd.DataFrame, Dict[str, Tuple[int, int]]]:
        d = self.daily_df.copy().replace([np.inf, -np.inf], np.nan)
        d = d.dropna(subset=["code", "mom5", "mom10", "vol_ratio", "amt20", "vol10", "ma10", "close"])
        if d.empty:
            return pd.DataFrame(), {}
        scored_frames: List[pd.DataFrame] = []
        for _, frame in d.groupby("date"):
            scored = _score_candidates_on_signal_day(frame)
            if scored is None or scored.empty:
                continue
            scored_frames.append(scored)
        if not scored_frames:
            return pd.DataFrame(), {}
        all_scored = pd.concat(scored_frames, ignore_index=True)
        all_scored["date"] = pd.to_datetime(all_scored["date"], errors="coerce")
        all_scored = all_scored.sort_values(["date", "score_rank", "code"]).reset_index(drop=True)
        return all_scored, self._build_date_ranges(all_scored)

    def _precompute_daily_breadth(self) -> Dict[str, float]:
        breadth: Dict[str, float] = {}
        for dt, frame in self.daily_df.groupby("date"):
            b = _calc_signal_breadth(frame)
            if b is not None:
                breadth[str(pd.Timestamp(dt).date())] = float(b)
        return breadth

    def _compute_equity(self, trade_date: str) -> Tuple[float, float]:
        market_value = 0.0
        for code, pos in self.positions.items():
            px = self.close_map.get(trade_date, {}).get(code) or pos.avg_cost
            market_value += float(px) * int(pos.shares)
        return float(self.cash) + market_value, market_value

    def _signal_drawdown(self, equity_now: float) -> float:
        series = [float(x["equity"]) for x in self.equity_curve if _to_float(x.get("equity")) is not None]
        if equity_now > 0:
            series.append(float(equity_now))
        if not series:
            return 0.0
        arr = np.array(series, dtype=float)
        peak = np.maximum.accumulate(arr)
        return float(arr[-1] / peak[-1] - 1.0) if peak[-1] > 0 else 0.0

    def _resolve_mode(self, breadth: Optional[float], signal_drawdown: Optional[float]) -> str:
        soft_dd = _to_float(self.params.get("soft_dd"))
        hard_dd = _to_float(self.params.get("hard_dd"))
        breadth_low = _to_float(self.params.get("breadth_low"))
        breadth_high = _to_float(self.params.get("breadth_high"))
        if signal_drawdown is not None and hard_dd is not None and signal_drawdown <= hard_dd:
            return "off"
        mode = "neutral"
        if breadth is not None:
            if breadth_high is not None and breadth >= breadth_high:
                mode = "on"
            elif breadth_low is not None and breadth <= breadth_low:
                mode = "off"
            else:
                mode = "neutral"
        if signal_drawdown is not None and soft_dd is not None and signal_drawdown <= soft_dd and mode == "on":
            mode = "neutral"
        return mode

    def _fee_ratio(self, side: str) -> float:
        if side.upper() == "BUY":
            return float(self.params["commission_buy"])
        return float(self.params["commission_sell"]) + float(self.params["stamp_tax_sell"])

    def _apply_buy(self, code: str, name: str, trade_date: str, raw_price: float, budget: float, reason: str) -> bool:
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
                "exec_source": "daily_close_rebalance",
                "realized_pnl": None,
            }
        )
        return True

    def _apply_sell(self, code: str, trade_date: str, raw_price: float, shares: int, reason: str) -> bool:
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
                "exec_source": "daily_close_rebalance",
                "realized_pnl": round(realized_pnl, 2),
            }
        )
        return True

    def run(self) -> Dict[str, Any]:
        for trade_date in self.trade_dates:
            for pos in self.positions.values():
                pos.hold_days += 1
                px = self.close_map.get(trade_date, {}).get(pos.code)
                if pos.take_profit_reached and px is not None:
                    pos.peak_price_after_tp = max(float(pos.peak_price_after_tp or 0.0), float(px))

            scored_df = self._slice_frame_by_date(self.daily_scored_df, self.daily_scored_ranges, trade_date)
            breadth = self.daily_breadth_by_date.get(trade_date)
            equity_before, _ = self._compute_equity(trade_date)
            signal_drawdown = self._signal_drawdown(equity_before)
            hard_breach = signal_drawdown <= float(self.params["hard_dd"])
            if hard_breach and not self.prev_hard_breach:
                self.freeze_left = max(self.freeze_left, int(self.params["freeze_days"]))

            mode = self._resolve_mode(breadth, signal_drawdown)
            max_holdings = MODE_MAX_HOLDINGS.get(mode, MODE_MAX_HOLDINGS["neutral"])
            keep_top_n = int(max_holdings) * int(self.params["keep_rank_mult"])
            target_rows = scored_df.head(max(1, keep_top_n)).copy() if not scored_df.empty else pd.DataFrame()
            target_pool = target_rows["code"].astype(str).tolist() if not target_rows.empty else []
            invest_ratio = float(self.params[f"invest_{mode}"])

            sold_today: List[str] = []
            for code in list(self.positions.keys()):
                pos = self.positions.get(code)
                if pos is None or pos.entry_date == trade_date:
                    continue
                latest_px = self.close_map.get(trade_date, {}).get(code)
                if latest_px is None or self._blocked_by_limit("SELL", code, trade_date, latest_px):
                    continue
                stop_hit = latest_px <= pos.avg_cost * (1.0 - float(self.params["stop_loss"]))
                tp_hit = latest_px >= pos.avg_cost * (1.0 + float(self.params["take_profit"]))
                if tp_hit and not pos.take_profit_reached:
                    shares = pos.shares if pos.shares < 200 else max(100, (pos.shares // 2 // 100) * 100)
                    label = "take_profit_small_full" if shares >= pos.shares else "take_profit_partial"
                    if self._apply_sell(code, trade_date, latest_px, shares, label):
                        sold_today.append(code)
                        if code in self.positions:
                            self.positions[code].take_profit_reached = True
                            self.positions[code].peak_price_after_tp = float(latest_px)
                        continue
                if stop_hit:
                    if self._apply_sell(code, trade_date, latest_px, pos.shares, "stop_loss"):
                        sold_today.append(code)
                        continue
                trailing_hit = bool(
                    pos.take_profit_reached
                    and pos.peak_price_after_tp > 0
                    and latest_px <= pos.peak_price_after_tp * (1.0 - float(self.params["trailing"]))
                )
                if trailing_hit:
                    if self._apply_sell(code, trade_date, latest_px, pos.shares, "trailing_stop"):
                        sold_today.append(code)
                        continue

            structural_sells: List[Tuple[str, str]] = []
            for code in list(self.positions.keys()):
                pos = self.positions.get(code)
                if pos is None or pos.entry_date == trade_date:
                    continue
                current_close = self.close_map.get(trade_date, {}).get(code)
                ma10 = self.ma10_map.get(trade_date, {}).get(code)
                if pos.hold_days >= int(self.params["min_hold_trend"]) and current_close is not None and ma10 is not None and current_close < ma10:
                    structural_sells.append((code, "trend_break"))
                    continue
                if pos.hold_days >= int(self.params["min_hold_score"]) and code not in target_pool:
                    structural_sells.append((code, "score_drop"))

            equity_mid, market_value_mid = self._compute_equity(trade_date)
            target_mv = equity_mid * invest_ratio
            retained = [c for c in self.positions.keys() if c not in {x[0] for x in structural_sells}]
            if len(retained) > max_holdings or market_value_mid > target_mv * 1.05:
                score_map = (
                    {
                        str(r["code"]): {"rank": int(r.get("score_rank") or 999999), "score": _to_float(r.get("score")) or -999.0}
                        for _, r in scored_df.iterrows()
                    }
                    if not scored_df.empty
                    else {}
                )
                weakest_first = sorted(
                    retained,
                    key=lambda c: (score_map.get(c, {}).get("rank", 999999), -(score_map.get(c, {}).get("score", -999.0))),
                    reverse=True,
                )
                for code in weakest_first:
                    if code in {x[0] for x in structural_sells}:
                        continue
                    structural_sells.append((code, "portfolio_cut"))
                    retained = [c for c in retained if c != code]
                    if len(retained) <= max_holdings:
                        break

            for code, reason in structural_sells:
                pos = self.positions.get(code)
                if pos is None:
                    continue
                px = self.close_map.get(trade_date, {}).get(code)
                if px is None or self._blocked_by_limit("SELL", code, trade_date, px):
                    continue
                if self._apply_sell(code, trade_date, px, pos.shares, reason):
                    sold_today.append(code)

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
                    if code in held_codes or code in sold_today:
                        continue
                    score = _to_float(row.get("score"))
                    if score is None or score < float(self.params["entry_min_score"]):
                        continue
                    buy_candidates.append(row)
                    if len(buy_candidates) >= buy_slots:
                        break

            available_budget = max(0.0, min(self.cash, equity_after_sells * invest_ratio - market_value_after_sells))
            actual_buys: List[str] = []
            blocked_buys: List[str] = []
            if not buy_block_today and available_budget > 0 and buy_candidates:
                per_slot_budget = available_budget / len(buy_candidates)
                for row in buy_candidates:
                    code = str(row["code"])
                    name = str(row.get("name") or "")
                    px = self.close_map.get(trade_date, {}).get(code)
                    if px is None or self._blocked_by_limit("BUY", code, trade_date, px):
                        blocked_buys.append(code)
                        continue
                    if self._apply_buy(code, name, trade_date, px, per_slot_budget, "target_entry"):
                        actual_buys.append(code)

            equity_close, market_value_close = self._compute_equity(trade_date)
            position_ratio = market_value_close / equity_close if equity_close > 0 else 0.0
            self.decision_rows.append(
                {
                    "trade_date": trade_date,
                    "signal_date": trade_date,
                    "signal_source": "daily_eod",
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
                        f"{code}:{pos.shares}@{(self.close_map.get(trade_date, {}).get(code) or pos.avg_cost):.2f}"
                        for code, pos in sorted(self.positions.items())
                    ),
                }
            )
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
        return self._build_outputs()

    def _build_outputs(self) -> Dict[str, Any]:
        curve_df = pd.DataFrame(self.equity_curve)
        trades_df = pd.DataFrame(self.trade_rows)
        decisions_df = pd.DataFrame(self.decision_rows)
        holdings_df = pd.DataFrame(self.holding_rows)
        total_return = annual_return = sharpe = max_drawdown = 0.0
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
        avg_win = (
            float(sell_trades.loc[sell_trades["realized_pnl"] > 0, "realized_pnl"].mean())
            if not sell_trades.empty and (sell_trades["realized_pnl"] > 0).any()
            else 0.0
        )
        avg_loss = (
            float(-sell_trades.loc[sell_trades["realized_pnl"] < 0, "realized_pnl"].mean())
            if not sell_trades.empty and (sell_trades["realized_pnl"] < 0).any()
            else 0.0
        )
        summary = {
            "strategy_version": STRATEGY_VERSION,
            "strategy_name": "V4 Audit Raw Rebuild",
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "final_equity": round(float(curve_df["equity"].iloc[-1]) if not curve_df.empty else self.initial_capital, 2),
            "total_return": total_return,
            "annualized_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "trade_count": int(len(trades_df)),
            "sell_count": int(len(sell_trades)),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_loss_ratio": float(avg_win / avg_loss) if avg_loss > 0 else None,
            "trading_days": int(len(curve_df)),
            "universe_size": self.universe_size,
            "assumptions": {
                "strategy_version": STRATEGY_VERSION,
                "execution": "signal_close_t_rebalance_raw_rebuild",
                "t_plus_1": True,
                "no_same_day_rebuy": True,
                "non_mandatory_daily_trading": True,
                "survivorship_bias": "current_stock_master_filters_only; no artifact reuse",
                "params": self.params,
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
    ax.plot(x, y, color="#2563eb", linewidth=1.8)
    ax.axhline(0, color="#888", linestyle="--", linewidth=1)
    ax.set_title("V4 Audit Raw Rebuild Equity Curve")
    ax.set_ylabel("Cumulative Return %")
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict raw-market V4 audit rerun")
    parser.add_argument("--start-date", default="2023-01-01")
    parser.add_argument("--end-date", default="2026-04-23")
    parser.add_argument("--initial-capital", type=float, default=100000.0)
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--cost-profile", default="current")
    parser.add_argument("--slippage-buy", type=float, default=None)
    parser.add_argument("--slippage-sell", type=float, default=None)
    parser.add_argument("--commission-buy", type=float, default=None)
    parser.add_argument("--commission-sell", type=float, default=None)
    parser.add_argument("--stamp-tax-sell", type=float, default=None)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    _ensure_output_dir(output_dir)
    bt = V4AuditBacktester(
        start_date=args.start_date,
        end_date=args.end_date,
        initial_capital=args.initial_capital,
        cost_profile=args.cost_profile,
        cost_overrides={
            "slippage_buy": args.slippage_buy,
            "slippage_sell": args.slippage_sell,
            "commission_buy": args.commission_buy,
            "commission_sell": args.commission_sell,
            "stamp_tax_sell": args.stamp_tax_sell,
        },
    )
    result = bt.run()
    summary = result["summary"]
    curve_df = result["curve_df"]
    trades_df = result["trades_df"]
    decisions_df = result["decisions_df"]
    holdings_df = result["holdings_df"]

    (output_dir / "summary_v4_audit_raw.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    curve_df.to_csv(output_dir / "equity_curve_v4_audit_raw.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "daily_trades_v4_audit_raw.csv", index=False, encoding="utf-8-sig")
    decisions_df.to_csv(output_dir / "daily_decisions_v4_audit_raw.csv", index=False, encoding="utf-8-sig")
    holdings_df.to_csv(output_dir / "daily_holdings_v4_audit_raw.csv", index=False, encoding="utf-8-sig")
    _plot_curve(curve_df, output_dir / "equity_curve_v4_audit_raw.png")

    _upsert_artifact(STRATEGY_VERSION, "summary", summary)
    _upsert_artifact(STRATEGY_VERSION, "curve", curve_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "trades", trades_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "decisions", decisions_df.to_dict(orient="records"))
    _upsert_artifact(STRATEGY_VERSION, "holdings", holdings_df.to_dict(orient="records"))

    print(
        json.dumps(
            {
                "strategy_version": STRATEGY_VERSION,
                "start_date": args.start_date,
                "end_date": args.end_date,
                "total_return": summary["total_return"],
                "sharpe": summary["sharpe"],
                "max_drawdown": summary["max_drawdown"],
                "trade_count": summary["trade_count"],
                "sell_count": summary["sell_count"],
                "universe_size": summary["universe_size"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
