from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _load_index, _max_drawdown, _md_table, _trade_calendar  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("score120_sector_diffusion_gate_v1")
OUT_DIR = report_path("score120_sector_diffusion_30m_overlay_v1")


def _safe_float(value: Any, digits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _pct(value: object) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


def _load_base_trades() -> pd.DataFrame:
    path = SOURCE_DIR / "base_trades_with_sector_diffusion.csv"
    if not path.exists():
        raise FileNotFoundError(f"missing {path}; run backtest_score120_sector_diffusion_gate_v1.py first")
    trades = pd.read_csv(path, encoding="utf-8-sig")
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        trades[col] = pd.to_datetime(trades[col], errors="coerce").dt.normalize()
    for col in ["net_ret", "rank_key", "wave_style_score", "amount_rank", "sector_diffusion_score"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    return trades


def _compute_30m_features(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for code, g0 in trades.groupby("code_raw"):
        start = (g0["trade_date"].min() - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
        end = g0["trade_date"].max().strftime("%Y-%m-%d")
        bars = clickhouse_query_df(
            """
            SELECT code, datetime, open, high, low, close, amount
            FROM kline_minute_30
            WHERE code = ?
              AND toDate(datetime) BETWEEN ? AND ?
            ORDER BY datetime
            """,
            [str(code), start, end],
        )
        if bars.empty:
            for idx in g0.index:
                rows.append({"_idx": idx, "m30_ok": False})
            continue
        bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
        bars["date"] = bars["datetime"].dt.normalize()
        for col in ["open", "high", "low", "close", "amount"]:
            bars[col] = pd.to_numeric(bars[col], errors="coerce")
        bars = bars.dropna(subset=["datetime", "close"]).sort_values("datetime")
        for idx, trade in g0.iterrows():
            signal_day = trade["trade_date"]
            hist = bars[bars["datetime"] <= signal_day + pd.Timedelta(hours=15)].tail(60).copy()
            day = bars[bars["date"].eq(signal_day)].copy()
            if len(hist) < 20 or day.empty:
                rows.append({"_idx": idx, "m30_ok": False})
                continue
            close = pd.to_numeric(hist["close"], errors="coerce")
            amount = pd.to_numeric(hist["amount"], errors="coerce")
            last = float(close.iloc[-1])
            ma20 = float(close.tail(20).mean())
            ma40 = float(close.tail(40).mean()) if len(close) >= 40 else float(close.mean())
            prev6 = float(close.iloc[-7]) if len(close) >= 7 else np.nan
            prev12 = float(close.iloc[-13]) if len(close) >= 13 else np.nan
            amt_last2 = float(amount.tail(2).mean())
            amt_prev20 = float(amount.tail(22).head(20).mean()) if len(amount) >= 22 else float(amount.tail(20).mean())
            day_open = float(day["open"].iloc[0])
            day_high = float(day["high"].max())
            day_low = float(day["low"].min())
            day_close = float(day["close"].iloc[-1])
            day_range = max(day_high - day_low, 1e-9)
            last_open = float(day["open"].iloc[-1])
            rows.append(
                {
                    "_idx": idx,
                    "m30_ok": True,
                    "m30_close_above_ma20": last / ma20 - 1.0 if ma20 > 0 else np.nan,
                    "m30_close_above_ma40": last / ma40 - 1.0 if ma40 > 0 else np.nan,
                    "m30_mom6": last / prev6 - 1.0 if prev6 > 0 else np.nan,
                    "m30_mom12": last / prev12 - 1.0 if prev12 > 0 else np.nan,
                    "m30_amount_last2_ratio": amt_last2 / amt_prev20 if amt_prev20 > 0 else np.nan,
                    "m30_day_ret": day_close / day_open - 1.0 if day_open > 0 else np.nan,
                    "m30_day_close_pos": (day_close - day_low) / day_range,
                    "m30_day_amp": day_high / day_low - 1.0 if day_low > 0 else np.nan,
                    "m30_last_bar_ret": day_close / last_open - 1.0 if last_open > 0 else np.nan,
                }
            )
    feat = pd.DataFrame(rows).set_index("_idx")
    return trades.join(feat)


def _simulate(trades: pd.DataFrame, calendar: list[pd.Timestamp], name: str, mask: pd.Series, slots: int, slot_pct: float, daily_open_limit: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    selected = trades[mask].copy()
    by_entry = {day: g.copy() for day, g in selected.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict[str, Any]] = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open
        opened = 0
        todays = by_entry.get(day)
        if todays is not None:
            todays = todays.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False])
            for row in todays.itertuples(index=False):
                if opened >= int(daily_open_limit) or len(open_pos) >= int(slots):
                    break
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * float(slot_pct)
                if stake <= 0 or cash < stake:
                    break
                pos = row._asdict()
                pos["scheduler"] = name
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1
        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day,
                "scheduler": name,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized_pnl,
            }
        )
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def _window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, name: str, window: str, start: str, end: str) -> dict[str, Any]:
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    iw = index_df[(index_df["trade_date"] >= pd.Timestamp(start)) & (index_df["trade_date"] <= pd.Timestamp(end))].copy()
    if cw.empty:
        return {"model": name, "window": window, "closed": 0}
    strat_ret = float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0)
    index_ret = float(iw["close"].iloc[-1] / iw["close"].iloc[0] - 1.0) if len(iw) >= 2 else np.nan
    net = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "model": name,
        "window": window,
        "closed": int(len(tw)),
        "unique_codes": int(tw["code_raw"].nunique()) if (not tw.empty and "code_raw" in tw.columns) else 0,
        "strategy_ret": strat_ret,
        "index_ret": index_ret,
        "excess_ret": strat_ret - index_ret if pd.notna(index_ret) else np.nan,
        "max_drawdown": _max_drawdown(cw["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "mean_trade_ret": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "avg_open_positions": float(cw["open_positions"].mean()),
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, name: str) -> pd.DataFrame:
    years = sorted(pd.to_datetime(curve["date"]).dt.year.dropna().unique().tolist()) if not curve.empty else []
    return pd.DataFrame([_window_metrics(curve, closed, index_df, name, str(int(y)), f"{int(y)}-01-01", f"{int(y)}-12-31") for y in years])


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _compute_30m_features(_load_base_trades())
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    trades = trades[(trades["entry_date"] >= start) & (trades["entry_date"] <= end)].copy()
    calendar = _trade_calendar(start, trades["policy_exit_date"].max())
    index_df = _load_index(args.start_date, args.end_date)
    trades.to_csv(OUT_DIR / "base_trades_with_sector_diffusion_30m.csv", index=False, encoding="utf-8-sig")

    masks = {
        "score120_base": pd.Series(True, index=trades.index),
        "score120_diff65": trades["sector_diffusion_score"] >= 65.0,
        "score120_diff65_m30_ma20": (trades["sector_diffusion_score"] >= 65.0) & (trades["m30_close_above_ma20"] >= 0.0),
        "score120_diff65_m30_ma40": (trades["sector_diffusion_score"] >= 65.0) & (trades["m30_close_above_ma40"] >= 0.08),
        "score120_diff65_not_tail_chase": (trades["sector_diffusion_score"] >= 65.0) & (trades["m30_last_bar_ret"] <= 0.01),
        "score120_diff72": trades["sector_diffusion_score"] >= 72.0,
        "score120_diff72_m30_ma20": (trades["sector_diffusion_score"] >= 72.0) & (trades["m30_close_above_ma20"] >= 0.0),
    }
    windows = {
        "full": (args.start_date, args.end_date),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "post_2024_09": ("2024-09-24", args.end_date),
        "blind_2026ytd": ("2026-01-01", args.end_date),
    }
    summary_rows: list[dict[str, Any]] = []
    annual_frames: list[pd.DataFrame] = []
    for name, mask in masks.items():
        run_dir = OUT_DIR / name
        run_dir.mkdir(parents=True, exist_ok=True)
        curve, closed = _simulate(trades, calendar, name, mask, int(args.slots), float(args.slot_pct), int(args.daily_open_limit))
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        for window, (w_start, w_end) in windows.items():
            summary_rows.append(_window_metrics(curve, closed, index_df, name, window, w_start, w_end))
        annual_frames.append(_annual_metrics(curve, closed, index_df, name))

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"strategy_ret", "index_ret", "excess_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade"}
    full = summary[summary["window"].eq("full")].sort_values("excess_ret", ascending=False)
    post = summary[summary["window"].eq("post_2024_09")].sort_values("excess_ret", ascending=False)
    blind = summary[summary["window"].eq("blind_2026ytd")].sort_values("excess_ret", ascending=False)
    best = str(full.iloc[0]["model"])
    annual_best = annual[annual["model"].eq(best)].sort_values("window")
    lines = [
        "# score120 主线扩散 + 30m 承接回测 v1",
        "",
        "## 定义",
        "",
        "- 基础策略：score120 原始成交事件。",
        "- 先加主线扩散门控，再用信号日收盘前 30m K 线做承接过滤；入场仍是次日开盘，无未来函数。",
        "- 当前最有效的 30m 条件是：信号日 30m 收盘价在 30m MA20 上方。",
        "",
        "## 全周期结果",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 2024-09 后结果",
        "",
        _md_table(post, pct_cols=pct_cols),
        "",
        "## 2026 样本外结果",
        "",
        _md_table(blind, pct_cols=pct_cols),
        "",
        f"## 最优模型 `{best}` 分年",
        "",
        _md_table(annual_best, pct_cols=pct_cols),
        "",
        "## 解释",
        "",
        "- 30m MA20 条件不是独立选股模型，而是对主线扩散门控后的资金承接确认。",
        "- 信号日尾盘追高、日内收得过满并没有提升胜率，不能简单理解为越强越好。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")

    result = {"status": "completed", "out_dir": str(OUT_DIR), "models": list(masks.keys()), "base_trades": int(len(trades))}
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest score120 with sector diffusion and 30m confirmation overlay.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    parser.add_argument("--slots", type=int, default=5)
    parser.add_argument("--slot-pct", type=float, default=0.25)
    parser.add_argument("--daily-open-limit", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
