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
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_available, clickhouse_query_df  # noqa: E402


INPUT_PATH = _report_path() / "gen2_event_study" / "v4_event_dataset.parquet"
OUTPUT_DIR = _report_path() / "gen2_single_pattern_backtest"
DEFAULT_INITIAL_CAPITAL = 150000.0


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def _load_trade_dates(start_date: str, end_date: str, fallback_dates: List[str]) -> List[str]:
    if clickhouse_available():
        try:
            df = clickhouse_query_df(
                """
                SELECT DISTINCT trade_date
                FROM kline_daily
                WHERE trade_date BETWEEN ?::DATE AND ?::DATE
                ORDER BY trade_date
                """,
                [start_date, end_date],
            )
            if not df.empty:
                return [str(pd.Timestamp(item).date()) for item in df["trade_date"].tolist()]
        except Exception:
            pass
    return sorted({str(pd.Timestamp(item).date()) for item in fallback_dates})


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def _annualized_return(total_return: float, days: int) -> float:
    if days <= 0:
        return 0.0
    return float((1.0 + total_return) ** (252.0 / days) - 1.0)


def _sharpe(daily_ret: pd.Series) -> float:
    if len(daily_ret) < 2:
        return 0.0
    std = float(daily_ret.std(ddof=0))
    if std <= 0:
        return 0.0
    return float(daily_ret.mean() / std * math.sqrt(252.0))


@dataclass
class Position:
    code: str
    name: str
    entry_date: str
    exit_date: str
    entry_equity: float
    weight_cash: float
    ret: float
    rank: int
    score: float
    reason: str


def _pattern_masks(df: pd.DataFrame) -> Dict[str, pd.Series]:
    entry = df["entry_pass"].fillna(False).astype(bool)
    previous_rank = pd.to_numeric(df["previous_rank"], errors="coerce")
    rank = pd.to_numeric(df["v4_rank"], errors="coerce")
    rank_change = pd.to_numeric(df["rank_change"], errors="coerce")
    score_change = pd.to_numeric(df["score_change"], errors="coerce")
    score = pd.to_numeric(df["v4_score"], errors="coerce")
    pool_streak = pd.to_numeric(df["pool_streak"], errors="coerce").fillna(0)
    mom5 = pd.to_numeric(df["mom5"], errors="coerce")
    mom10 = pd.to_numeric(df["mom10"], errors="coerce")
    mom20 = pd.to_numeric(df["mom20"], errors="coerce")
    vol_ratio = pd.to_numeric(df["vol_ratio"], errors="coerce")
    vol10 = pd.to_numeric(df["vol10"], errors="coerce")
    trade_gap = pd.to_numeric(df["trade_gap"], errors="coerce")

    rank_acceleration = entry & previous_rank.notna() & (
        ((rank_change >= 20) & (score_change > 0))
        | ((previous_rank > 30) & (rank <= 20))
        | ((previous_rank > 50) & (rank <= 30))
    )
    high_score_core = entry & (score >= 0.82) & (rank <= 30) & (pool_streak >= 2)
    momentum_pullback_restart = (
        entry
        & (mom10 >= 0.08)
        & (mom20 >= 0)
        & (mom5 >= 0)
        & (mom5 <= 0.08)
        & (mom5 <= mom10 * 0.60)
        & (vol_ratio >= 0.80)
        & (vol_ratio <= 2.50)
        & (vol10 <= 0.065)
    )
    removed_return = entry & previous_rank.notna() & (trade_gap >= 2) & (rank <= 50) & (score_change > 0)
    score_mid_entry = entry & (score >= 0.75) & (score < 0.85)
    high_score_core_cool = (
        high_score_core
        & (score <= 0.93)
        & (pool_streak <= 15)
        & (mom5 >= 0)
        & (mom5 <= 0.18)
        & (mom10 <= 0.35)
        & (vol_ratio <= 2.20)
        & (vol10 <= 0.060)
    )
    high_score_core_steady = (
        entry
        & (score >= 0.82)
        & (score <= 0.92)
        & (rank <= 20)
        & (pool_streak >= 3)
        & (pool_streak <= 12)
        & (score_change >= -0.03)
        & (mom5 >= 0)
        & (mom5 <= 0.12)
        & (vol10 <= 0.055)
    )
    removed_return_quality = (
        removed_return
        & (trade_gap <= 10)
        & (score >= 0.80)
        & (score <= 0.92)
        & (score_change >= 0.03)
        & (rank_change >= 20)
        & (mom5 >= 0)
        & (mom10 >= 0)
        & (vol10 <= 0.060)
    )
    removed_return_strict = (
        removed_return_quality
        & (trade_gap <= 8)
        & (rank <= 30)
        & (score >= 0.82)
        & (score_change >= 0.05)
        & (rank_change >= 50)
        & (mom5 <= 0.12)
        & (vol_ratio >= 0.80)
        & (vol_ratio <= 2.20)
    )

    return {
        "all_entry_pass": entry,
        "rank_acceleration": rank_acceleration,
        "high_score_core": high_score_core,
        "high_score_core_cool": high_score_core_cool,
        "high_score_core_steady": high_score_core_steady,
        "momentum_pullback_restart": momentum_pullback_restart,
        "removed_return": removed_return,
        "removed_return_quality": removed_return_quality,
        "removed_return_strict": removed_return_strict,
        "score_mid_entry": score_mid_entry,
    }


def _add_trade_gap(df: pd.DataFrame, trade_dates: List[str]) -> pd.DataFrame:
    date_index = {date: idx for idx, date in enumerate(trade_dates)}
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.strftime("%Y-%m-%d")
    out["previous_signal_date"] = pd.to_datetime(out["previous_signal_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["trade_idx"] = out["trade_date"].map(date_index)
    out["previous_trade_idx"] = out["previous_signal_date"].map(date_index)
    out["trade_gap"] = out["trade_idx"] - out["previous_trade_idx"]
    return out


def _candidate_sort(df: pd.DataFrame, pattern: str) -> pd.DataFrame:
    d = df.copy()
    if pattern == "rank_acceleration":
        d["pattern_strength"] = (
            pd.to_numeric(d["rank_change"], errors="coerce").fillna(0) * 0.60
            + pd.to_numeric(d["score_change"], errors="coerce").fillna(0) * 100.0 * 0.25
            + (100.0 - pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999)).clip(lower=0) * 0.15
        )
    elif pattern.startswith("high_score_core"):
        d["pattern_strength"] = (
            pd.to_numeric(d["v4_score"], errors="coerce").fillna(0) * 100.0
            + pd.to_numeric(d["pool_streak"], errors="coerce").fillna(0).clip(upper=10)
            - pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999) * 0.05
        )
    elif pattern == "momentum_pullback_restart":
        d["pattern_strength"] = (
            pd.to_numeric(d["mom10"], errors="coerce").fillna(0) * 100.0
            - pd.to_numeric(d["mom5"], errors="coerce").fillna(0).abs() * 30.0
            + pd.to_numeric(d["v4_score"], errors="coerce").fillna(0) * 20.0
        )
    elif pattern == "removed_return":
        d["pattern_strength"] = (
            pd.to_numeric(d["trade_gap"], errors="coerce").fillna(0).clip(upper=10) * 3.0
            + pd.to_numeric(d["score_change"], errors="coerce").fillna(0) * 100.0
            - pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999) * 0.05
        )
    elif pattern.startswith("removed_return"):
        d["pattern_strength"] = (
            pd.to_numeric(d["trade_gap"], errors="coerce").fillna(0).clip(upper=10) * 3.0
            + pd.to_numeric(d["score_change"], errors="coerce").fillna(0) * 100.0
            + pd.to_numeric(d["rank_change"], errors="coerce").fillna(0).clip(lower=0) * 0.08
            - pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999) * 0.05
        )
    else:
        d["pattern_strength"] = pd.to_numeric(d["v4_score"], errors="coerce").fillna(0) * 100.0 - pd.to_numeric(
            d["v4_rank"], errors="coerce"
        ).fillna(999) * 0.05
    return d.sort_values(["pattern_strength", "v4_score", "v4_rank"], ascending=[False, False, True])


def _run_single_backtest(
    events: pd.DataFrame,
    pattern: str,
    hold_days: int,
    trade_dates: List[str],
    initial_capital: float,
    max_positions: int,
    max_new_per_day: int,
    cost_bps: float,
) -> Dict[str, Any]:
    ret_col = f"fwd_ret_{hold_days}d"
    if ret_col not in events.columns:
        raise ValueError(f"Missing return column: {ret_col}")
    date_index = {date: idx for idx, date in enumerate(trade_dates)}
    by_date = {date: frame for date, frame in events.groupby("trade_date")}
    cash = float(initial_capital)
    positions: List[Position] = []
    closed: List[Dict[str, Any]] = []
    equity_rows: List[Dict[str, Any]] = []
    skipped_no_slot = 0
    skipped_nan_return = 0
    skipped_duplicate = 0

    for date in trade_dates:
        still_open: List[Position] = []
        realized_pnl = 0.0
        for pos in positions:
            if pos.exit_date == date:
                exit_value = pos.weight_cash * (1.0 + pos.ret - cost_bps / 10000.0)
                pnl = exit_value - pos.weight_cash
                cash += exit_value
                realized_pnl += pnl
                closed.append(
                    {
                        "pattern": pattern,
                        "hold_days": hold_days,
                        "code": pos.code,
                        "name": pos.name,
                        "entry_date": pos.entry_date,
                        "exit_date": pos.exit_date,
                        "entry_cash": pos.weight_cash,
                        "ret": pos.ret,
                        "net_ret": pos.ret - cost_bps / 10000.0,
                        "pnl": pnl,
                        "rank": pos.rank,
                        "score": pos.score,
                        "reason": pos.reason,
                    }
                )
            else:
                still_open.append(pos)
        positions = still_open

        candidates = by_date.get(date)
        opened = 0
        if candidates is not None and len(positions) < max_positions:
            candidates = _candidate_sort(candidates, pattern)
            held_codes = {pos.code for pos in positions}
            for _, row in candidates.iterrows():
                if opened >= max_new_per_day or len(positions) >= max_positions:
                    break
                code = str(row["code"])
                if code in held_codes:
                    skipped_duplicate += 1
                    continue
                ret = _safe_float(row.get(ret_col), default=float("nan"))
                if math.isnan(ret):
                    skipped_nan_return += 1
                    continue
                idx = date_index.get(date)
                if idx is None or idx + hold_days >= len(trade_dates):
                    skipped_nan_return += 1
                    continue
                exit_date = trade_dates[idx + hold_days]
                slots_left = max_positions - len(positions)
                budget = cash / max(slots_left, 1)
                if budget <= 0:
                    skipped_no_slot += 1
                    break
                cash -= budget
                positions.append(
                    Position(
                        code=code,
                        name=str(row.get("name") or ""),
                        entry_date=date,
                        exit_date=exit_date,
                        entry_equity=cash + sum(pos.weight_cash for pos in positions),
                        weight_cash=budget,
                        ret=ret,
                        rank=int(_safe_float(row.get("v4_rank"), 0)),
                        score=_safe_float(row.get("v4_score"), 0.0),
                        reason=pattern,
                    )
                )
                held_codes.add(code)
                opened += 1
        if candidates is not None and opened >= max_new_per_day:
            skipped_no_slot += max(0, len(candidates) - opened)

        mark_equity = cash + sum(pos.weight_cash for pos in positions)
        equity_rows.append(
            {
                "date": date,
                "pattern": pattern,
                "hold_days": hold_days,
                "cash": cash,
                "reserved_position_cash": sum(pos.weight_cash for pos in positions),
                "equity": mark_equity,
                "open_positions": len(positions),
                "opened": opened,
                "realized_pnl": realized_pnl,
            }
        )

    for pos in positions:
        skipped_nan_return += 1

    trades = pd.DataFrame(closed)
    curve = pd.DataFrame(equity_rows)
    if curve.empty:
        daily_ret = pd.Series(dtype=float)
        total_return = 0.0
        max_dd = 0.0
    else:
        daily_ret = curve["equity"].astype(float).pct_change().fillna(0.0)
        total_return = float(curve["equity"].iloc[-1] / initial_capital - 1.0)
        max_dd = _max_drawdown(curve["equity"].astype(float))
    win_rate = float((trades["net_ret"] > 0).mean()) if not trades.empty else 0.0
    avg_trade = float(trades["net_ret"].mean()) if not trades.empty else 0.0
    median_trade = float(trades["net_ret"].median()) if not trades.empty else 0.0
    summary = {
        "pattern": pattern,
        "hold_days": hold_days,
        "trade_count": int(len(trades)),
        "total_return": total_return,
        "annualized_return": _annualized_return(total_return, len(curve)),
        "max_drawdown": max_dd,
        "sharpe": _sharpe(daily_ret),
        "win_rate": win_rate,
        "avg_trade_return": avg_trade,
        "median_trade_return": median_trade,
        "final_equity": float(curve["equity"].iloc[-1]) if not curve.empty else initial_capital,
        "skipped_no_slot": int(skipped_no_slot),
        "skipped_nan_return": int(skipped_nan_return),
        "skipped_duplicate": int(skipped_duplicate),
    }
    return {"summary": summary, "trades": trades, "curve": curve}


def _build_monthly_returns(curves_df: pd.DataFrame) -> pd.DataFrame:
    if curves_df.empty:
        return pd.DataFrame()
    d = curves_df.copy()
    d["month"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m")
    rows: List[Dict[str, Any]] = []
    for (pattern, hold_days, month), g in d.groupby(["pattern", "hold_days", "month"], sort=True):
        g = g.sort_values("date")
        start_equity = float(g["equity"].iloc[0])
        end_equity = float(g["equity"].iloc[-1])
        rows.append(
            {
                "pattern": pattern,
                "hold_days": int(hold_days),
                "month": month,
                "start_equity": start_equity,
                "end_equity": end_equity,
                "month_return": float(end_equity / start_equity - 1.0) if start_equity else 0.0,
                "max_drawdown": _max_drawdown(g["equity"].astype(float)),
                "trade_days": int(len(g)),
            }
        )
    return pd.DataFrame(rows)


def _build_failure_samples(trades_df: pd.DataFrame, limit_per_group: int = 20) -> pd.DataFrame:
    if trades_df.empty or "net_ret" not in trades_df.columns:
        return pd.DataFrame()
    failures = trades_df[trades_df["net_ret"] < 0].copy()
    if failures.empty:
        return pd.DataFrame()
    failures = failures.sort_values(["pattern", "hold_days", "net_ret"], ascending=[True, True, True])
    return failures.groupby(["pattern", "hold_days"], group_keys=False).head(limit_per_group).reset_index(drop=True)


def run(
    input_path: Path,
    output_dir: Path,
    hold_days_list: List[int],
    initial_capital: float,
    max_positions: int,
    max_new_per_day: int,
    cost_bps: float,
) -> Dict[str, Any]:
    if not input_path.exists():
        raise FileNotFoundError(f"Missing stage A dataset: {input_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    events = pd.read_parquet(input_path).replace([np.inf, -np.inf], np.nan)
    events["trade_date"] = pd.to_datetime(events["trade_date"]).dt.strftime("%Y-%m-%d")
    start_date = str(events["trade_date"].min())
    end_date = str(events["trade_date"].max())
    trade_dates = _load_trade_dates(start_date, end_date, events["trade_date"].tolist())
    events = _add_trade_gap(events, trade_dates)
    masks = _pattern_masks(events)

    tagged_frames: List[pd.DataFrame] = []
    pattern_counts: Dict[str, int] = {}
    for name, mask in masks.items():
        selected = events.loc[mask].copy()
        selected["pattern"] = name
        pattern_counts[name] = int(len(selected))
        tagged_frames.append(selected)
    tagged = pd.concat(tagged_frames, ignore_index=True) if tagged_frames else pd.DataFrame()
    tagged.to_csv(output_dir / "pattern_events.csv", index=False, encoding="utf-8-sig")

    summaries: List[Dict[str, Any]] = []
    all_trades: List[pd.DataFrame] = []
    all_curves: List[pd.DataFrame] = []
    for pattern in masks.keys():
        pattern_events = tagged[tagged["pattern"] == pattern].copy()
        for hold_days in hold_days_list:
            result = _run_single_backtest(
                pattern_events,
                pattern,
                hold_days,
                trade_dates,
                initial_capital,
                max_positions,
                max_new_per_day,
                cost_bps,
            )
            summaries.append(result["summary"])
            if not result["trades"].empty:
                all_trades.append(result["trades"])
            if not result["curve"].empty:
                all_curves.append(result["curve"])

    summary_df = pd.DataFrame(summaries).sort_values(
        ["hold_days", "total_return", "sharpe"], ascending=[True, False, False]
    )
    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    curves_df = pd.concat(all_curves, ignore_index=True) if all_curves else pd.DataFrame()
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    curves_df.to_csv(output_dir / "equity_curves.csv", index=False, encoding="utf-8-sig")
    monthly_df = _build_monthly_returns(curves_df)
    failures_df = _build_failure_samples(trades_df)
    monthly_df.to_csv(output_dir / "monthly_returns.csv", index=False, encoding="utf-8-sig")
    failures_df.to_csv(output_dir / "failure_samples.csv", index=False, encoding="utf-8-sig")

    payload = {
        "schema_version": 1,
        "input_path": str(input_path.relative_to(REPO_ROOT) if input_path.is_relative_to(REPO_ROOT) else input_path),
        "start_date": start_date,
        "end_date": end_date,
        "trade_days": len(trade_dates),
        "initial_capital": initial_capital,
        "max_positions": max_positions,
        "max_new_per_day": max_new_per_day,
        "cost_bps": cost_bps,
        "hold_days_list": hold_days_list,
        "pattern_counts": pattern_counts,
        "outputs": {
            "pattern_events": "pattern_events.csv",
            "summary": "summary.csv",
            "trades": "trades.csv",
            "equity_curves": "equity_curves.csv",
            "monthly_returns": "monthly_returns.csv",
            "failure_samples": "failure_samples.csv",
        },
        "summary": summaries,
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="G2 single-pattern fixed-hold portfolio backtest.")
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--hold-days", default="3,5,10")
    parser.add_argument("--initial-capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--max-new-per-day", type=int, default=1)
    parser.add_argument("--cost-bps", type=float, default=5.0)
    args = parser.parse_args()
    hold_days_list = [int(item.strip()) for item in str(args.hold_days).split(",") if item.strip()]
    payload = run(
        input_path=Path(args.input),
        output_dir=Path(args.output_dir),
        hold_days_list=hold_days_list,
        initial_capital=float(args.initial_capital),
        max_positions=int(args.max_positions),
        max_new_per_day=int(args.max_new_per_day),
        cost_bps=float(args.cost_bps),
    )
    print(
        json.dumps(
            {
                "output_dir": str(Path(args.output_dir)),
                "patterns": payload["pattern_counts"],
                "summary_rows": len(payload["summary"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
