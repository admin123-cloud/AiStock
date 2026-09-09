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

from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _load_index, _max_drawdown, _trade_calendar  # noqa: E402
from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table, _pct  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE = report_path("score120_sector_diffusion_30m_overlay_v1", "base_trades_with_sector_diffusion_30m.csv")
OUT_DIR = report_path("score120_mom60_gate_revalidation_v1")


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value) if math.isfinite(float(value)) else None
    if pd.isna(value):
        return None
    return str(value)


def _load_signals(start: str, end: str) -> pd.DataFrame:
    if not SOURCE.exists():
        raise FileNotFoundError(f"missing source: {SOURCE}; run backtest_score120_sector_diffusion_30m_overlay_v1.py first")
    d = pd.read_csv(SOURCE, encoding="utf-8-sig")
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    numeric = [
        "net_ret",
        "rank_key",
        "amount_rank",
        "sector_diffusion_score",
        "m30_close_above_ma20",
        "index_mom60",
        "index_mom20",
        "realized_pnl",
    ]
    for col in numeric:
        d[col] = pd.to_numeric(d.get(col), errors="coerce")
    d = d[(d["entry_date"] >= pd.Timestamp(start)) & (d["entry_date"] <= pd.Timestamp(end))].copy()
    signal = d[(d["sector_diffusion_score"] >= 65.0) & (d["m30_close_above_ma20"] >= 0.0)].copy()
    signal["sector_gate_source"] = "diff65"
    signal["sector_gate_rule"] = "sector_diffusion>=65"
    signal = signal.dropna(subset=["entry_date", "policy_exit_date", "net_ret", "index_mom60"])
    return signal.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False]).reset_index(drop=True)


def _slot_pct(row: pd.Series, policy: str) -> float:
    mom60 = float(row["index_mom60"])
    if policy == "base_no_mom60_gate":
        return 0.50
    if policy == "hard_le_5":
        return 0.50 if mom60 <= 0.05 else 0.0
    if policy == "hard_le_8":
        return 0.50 if mom60 <= 0.08 else 0.0
    if policy == "hard_le_10":
        return 0.50 if mom60 <= 0.10 else 0.0
    if policy == "tier_le5_full_5to10_half":
        if mom60 <= 0.05:
            return 0.50
        if mom60 <= 0.10:
            return 0.25
        return 0.0
    raise ValueError(policy)


def _simulate(signals: pd.DataFrame, calendar: list[pd.Timestamp], policy: str, slots: int, daily_open_limit: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in signals.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []

    for day in calendar:
        realized = 0.0
        still_open: list[dict[str, Any]] = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        opened = 0
        skipped_policy = 0
        skipped_capacity = 0
        todays = by_entry.get(day)
        if todays is not None:
            todays = todays.sort_values(["rank_key", "amount_rank"], ascending=[False, False])
            for _, row in todays.iterrows():
                if opened >= daily_open_limit or len(open_pos) >= slots:
                    skipped_capacity += 1
                    continue
                pct = _slot_pct(row, policy)
                if pct <= 0:
                    skipped_policy += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * pct
                if stake <= 0 or cash < stake:
                    skipped_capacity += 1
                    continue
                pos = row.to_dict()
                pos["policy"] = policy
                pos["slot_pct"] = pct
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1

        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day,
                "policy": policy,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_policy": skipped_policy,
                "skipped_capacity": skipped_capacity,
                "realized_pnl": realized,
            }
        )
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def _metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, policy: str, window: str, start: str, end: str) -> dict[str, Any]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    cw = curve[(curve["date"] >= start_ts) & (curve["date"] <= end_ts)].copy()
    tw = closed[(closed["entry_date"] >= start_ts) & (closed["entry_date"] <= end_ts)].copy() if not closed.empty else pd.DataFrame()
    iw = index_df[(index_df["trade_date"] >= start_ts) & (index_df["trade_date"] <= end_ts)].copy()
    if cw.empty:
        return {"policy": policy, "window": window, "closed": 0}
    net = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    index_ret = float(iw["close"].iloc[-1] / iw["close"].iloc[0] - 1.0) if len(iw) >= 2 else math.nan
    ret = float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0)
    return {
        "policy": policy,
        "window": window,
        "closed": int(len(tw)),
        "strategy_ret": ret,
        "index_ret": index_ret,
        "excess_ret": ret - index_ret if math.isfinite(index_ret) else math.nan,
        "max_drawdown": _max_drawdown(cw["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "mean_trade_ret": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "sum_pnl": float(pd.to_numeric(tw.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()) if len(tw) else 0.0,
        "avg_slot_pct": float(pd.to_numeric(tw.get("slot_pct", pd.Series(dtype=float)), errors="coerce").mean()) if len(tw) else 0.0,
    }


def _signal_bucket(signals: pd.DataFrame) -> pd.DataFrame:
    d = signals.copy()
    d["mom60_bucket"] = pd.cut(
        d["index_mom60"],
        bins=[-999, 0.0, 0.05, 0.08, 0.10, 0.15, 999],
        labels=["<=0%", "0-5%", "5-8%", "8-10%", "10-15%", ">15%"],
        right=True,
    )
    out = (
        d.groupby("mom60_bucket", observed=False)
        .agg(
            signals=("net_ret", "size"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda s: float((s > 0).mean()) if len(s) else 0.0),
            bad10_rate=("net_ret", lambda s: float((s <= -0.10).mean()) if len(s) else 0.0),
            worst_ret=("net_ret", "min"),
            avg_sector_diffusion=("sector_diffusion_score", "mean"),
            avg_m30_ma20=("m30_close_above_ma20", "mean"),
        )
        .reset_index()
    )
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(args.start_date, args.end_date)
    calendar = _trade_calendar(pd.Timestamp(args.start_date), max(pd.Timestamp(args.end_date), signals["policy_exit_date"].max()))
    index_df = _load_index(args.start_date, args.end_date)
    policies = ["base_no_mom60_gate", "hard_le_5", "hard_le_8", "hard_le_10", "tier_le5_full_5to10_half"]
    windows = {
        "full": (args.start_date, args.end_date),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "post_2024_09": ("2024-09-24", args.end_date),
        "blind_2026ytd": ("2026-01-01", args.end_date),
    }
    summary_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    closed_frames: list[pd.DataFrame] = []
    for policy in policies:
        run_dir = OUT_DIR / policy
        run_dir.mkdir(parents=True, exist_ok=True)
        curve, closed = _simulate(signals, calendar, policy, int(args.slots), int(args.daily_open_limit))
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        closed_frames.append(closed)
        for window, (start, end) in windows.items():
            summary_rows.append(_metrics(curve, closed, index_df, policy, window, start, end))
        for year in sorted(pd.to_datetime(curve["date"]).dt.year.dropna().unique()):
            annual_rows.append(_metrics(curve, closed, index_df, policy, str(int(year)), f"{int(year)}-01-01", f"{int(year)}-12-31"))

    summary = pd.DataFrame(summary_rows)
    annual = pd.DataFrame(annual_rows)
    bucket = _signal_bucket(signals)
    signals.to_csv(OUT_DIR / "score120_diff65_m30_ma20_source_signals.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "policy_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "policy_annual_summary.csv", index=False, encoding="utf-8-sig")
    bucket.to_csv(OUT_DIR / "source_signal_mom60_bucket.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "strategy_ret",
        "index_ret",
        "excess_ret",
        "max_drawdown",
        "win_rate",
        "mean_trade_ret",
        "worst_trade",
        "avg_slot_pct",
        "avg_ret",
        "bad10_rate",
        "avg_sector_diffusion",
        "avg_m30_ma20",
    }
    money_cols = {"sum_pnl"}
    full = summary[summary["window"].eq("full")].sort_values(["strategy_ret", "max_drawdown"], ascending=[False, False])
    valid = summary[summary["window"].eq("valid_2024_2025")].sort_values("strategy_ret", ascending=False)
    blind = summary[summary["window"].eq("blind_2026ytd")].sort_values("strategy_ret", ascending=False)
    post = summary[summary["window"].eq("post_2024_09")].sort_values("strategy_ret", ascending=False)
    lines = [
        "# Score120 机构主升 mom60 门槛重新验证 v1",
        "",
        "## 验证口径",
        "",
        "- 底层信号：`score120 + sector_diffusion>=65 + signal-day 30m close>=MA20`。",
        "- 资金口径：G3 当前合同近似，2 槽、单槽 50%、每日最多 2 张。",
        "- 对照政策：无 mom60 门槛、`<=5%`、`<=8%`、`<=10%`、以及 `<=5%全仓/5%-10%半仓`。",
        "",
        "## 单笔信号按 mom60 分桶",
        "",
        _md_table(bucket, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 全周期资金曲线对照",
        "",
        _md_table(full, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 2024-2025 验证窗口",
        "",
        _md_table(valid, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 2024-09 后高波动窗口",
        "",
        _md_table(post, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 2026 样本外窗口",
        "",
        _md_table(blind, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 分年明细",
        "",
        _md_table(annual.sort_values(["policy", "window"]), pct_cols=pct_cols, money_cols=money_cols, max_rows=80),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "source": str(SOURCE),
        "signals": int(len(signals)),
        "policies": policies,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Revalidate index_mom60 gate for score120 institutional mainwave.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--daily-open-limit", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
