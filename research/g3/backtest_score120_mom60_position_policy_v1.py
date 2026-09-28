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
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _load_index, _max_drawdown, _trade_calendar  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("score120_activation_regime_v1")
OUT_DIR = report_path("score120_mom60_position_policy_v1")
SOURCE_TRADES = SOURCE_DIR / "diff65_m30_trades_with_regime_corrected.csv"


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except Exception:
        return default
    if not math.isfinite(out):
        return default
    return out


def _pct(value: Any) -> str:
    x = _safe_float(value)
    return "" if x is None else f"{x:.2%}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    out = df.copy()
    for col in pct_cols or set():
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out.to_markdown(index=False)


def _load_trades(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not SOURCE_TRADES.exists():
        raise FileNotFoundError(f"missing source trades: {SOURCE_TRADES}")
    trades = pd.read_csv(SOURCE_TRADES, encoding="utf-8-sig")
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        trades[col] = pd.to_datetime(trades[col], errors="coerce").dt.normalize()
    for col in [
        "net_ret",
        "rank_key",
        "amount_rank",
        "sig_index_mom60",
        "sig_index_mom20",
        "sector_diffusion_score",
        "m30_close_above_ma20",
    ]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    trades = trades[(trades["entry_date"] >= start) & (trades["entry_date"] <= end)].copy()
    return trades.dropna(subset=["entry_date", "policy_exit_date", "net_ret"])


def _policy_slot_pct(row: pd.Series, policy: str) -> float:
    mom60 = _safe_float(row.get("sig_index_mom60"), 999.0) or 999.0
    if policy == "base_50":
        return 0.50
    if policy == "hard_le_5_50":
        return 0.50 if mom60 <= 0.05 else 0.0
    if policy == "hard_le_10_50":
        return 0.50 if mom60 <= 0.10 else 0.0
    if policy == "tier_5_10_50_25_0":
        if mom60 <= 0.05:
            return 0.50
        if mom60 <= 0.10:
            return 0.25
        return 0.0
    raise ValueError(f"unknown policy: {policy}")


def _simulate(
    trades: pd.DataFrame,
    calendar: list[pd.Timestamp],
    *,
    policy: str,
    slots: int,
    daily_open_limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in trades.groupby("entry_date")}
    cash = float(INITIAL_CAPITAL)
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
        skipped_policy = 0
        skipped_capacity = 0
        todays = by_entry.get(day)
        if todays is not None:
            todays = todays.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False])
            for _, row in todays.iterrows():
                if opened >= daily_open_limit or len(open_pos) >= slots:
                    skipped_capacity += 1
                    continue
                slot_pct = _policy_slot_pct(row, policy)
                if slot_pct <= 0:
                    skipped_policy += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * slot_pct
                if stake <= 0 or cash < stake:
                    skipped_capacity += 1
                    continue
                pos = row.to_dict()
                pos["policy"] = policy
                pos["slot_pct"] = slot_pct
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


def _window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, policy: str, window: str, start: str, end: str) -> dict[str, Any]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    cw = curve[(curve["date"] >= start_ts) & (curve["date"] <= end_ts)].copy()
    tw = closed[(closed["entry_date"] >= start_ts) & (closed["entry_date"] <= end_ts)].copy() if not closed.empty else pd.DataFrame()
    iw = index_df[(index_df["trade_date"] >= start_ts) & (index_df["trade_date"] <= end_ts)].copy()
    if cw.empty:
        return {"policy": policy, "window": window, "closed": 0}
    net = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    index_ret = float(iw["close"].iloc[-1] / iw["close"].iloc[0] - 1.0) if len(iw) >= 2 else math.nan
    start_equity = float(cw["equity"].iloc[0])
    end_equity = float(cw["equity"].iloc[-1])
    return {
        "policy": policy,
        "window": window,
        "closed": int(len(tw)),
        "strategy_ret": end_equity / start_equity - 1.0,
        "index_ret": index_ret,
        "excess_ret": end_equity / start_equity - 1.0 - index_ret if math.isfinite(index_ret) else math.nan,
        "max_drawdown": _max_drawdown(cw["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "mean_trade_ret": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "avg_slot_pct": float(pd.to_numeric(tw.get("slot_pct", pd.Series(dtype=float)), errors="coerce").mean()) if len(tw) else 0.0,
        "avg_open_positions": float(cw["open_positions"].mean()),
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, policy: str) -> pd.DataFrame:
    years = sorted(pd.to_datetime(curve["date"]).dt.year.dropna().unique().tolist()) if not curve.empty else []
    return pd.DataFrame([_window_metrics(curve, closed, index_df, policy, str(int(y)), f"{int(y)}-01-01", f"{int(y)}-12-31") for y in years])


def _bucket_summary(closed: pd.DataFrame, policy: str) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    d = closed.copy()
    mom = pd.to_numeric(d["sig_index_mom60"], errors="coerce")
    d["mom60_bucket"] = pd.cut(
        mom,
        bins=[-999.0, 0.05, 0.10, 999.0],
        labels=["<=5%", "5%-10%", ">10%"],
        right=True,
    )
    out = (
        d.groupby("mom60_bucket", observed=False)
        .agg(
            closed=("net_ret", "size"),
            avg_slot_pct=("slot_pct", "mean"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((x > 0).mean()) if len(x) else 0.0),
            pnl=("realized_pnl", "sum"),
            worst_ret=("net_ret", "min"),
        )
        .reset_index()
    )
    out.insert(0, "policy", policy)
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    trades = _load_trades(start, end)
    calendar_end = max(end, trades["policy_exit_date"].max())
    calendar = _trade_calendar(start, calendar_end)
    index_df = _load_index(args.start_date, args.end_date)

    policies = ["base_50", "hard_le_5_50", "hard_le_10_50", "tier_5_10_50_25_0"]
    windows = {
        "full": (args.start_date, args.end_date),
        "post_2024_09": ("2024-09-24", args.end_date),
        "blind_2026ytd": ("2026-01-01", args.end_date),
    }

    summary_rows: list[dict[str, Any]] = []
    annual_frames: list[pd.DataFrame] = []
    bucket_frames: list[pd.DataFrame] = []
    for policy in policies:
        run_dir = OUT_DIR / policy
        run_dir.mkdir(parents=True, exist_ok=True)
        curve, closed = _simulate(trades, calendar, policy=policy, slots=args.slots, daily_open_limit=args.daily_open_limit)
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        for window, (w_start, w_end) in windows.items():
            summary_rows.append(_window_metrics(curve, closed, index_df, policy, window, w_start, w_end))
        annual_frames.append(_annual_metrics(curve, closed, index_df, policy))
        bucket_frames.append(_bucket_summary(closed, policy))

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    buckets = pd.concat(bucket_frames, ignore_index=True) if bucket_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")
    buckets.to_csv(OUT_DIR / "mom60_bucket_summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "source_trades.csv", index=False, encoding="utf-8-sig")

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
    }
    full = summary[summary["window"].eq("full")].sort_values("strategy_ret", ascending=False)
    post = summary[summary["window"].eq("post_2024_09")].sort_values("strategy_ret", ascending=False)
    blind = summary[summary["window"].eq("blind_2026ytd")].sort_values("strategy_ret", ascending=False)
    tier_annual = annual[annual["policy"].eq("tier_5_10_50_25_0")].sort_values("window")
    tier_buckets = buckets[buckets["policy"].eq("tier_5_10_50_25_0")]

    lines = [
        "# score120 + index_mom60 三档仓位回测 v1",
        "",
        "## 规则",
        "",
        "- `index_mom60 <= 5%`：正常单槽 50%。",
        "- `5% < index_mom60 <= 10%`：候选保留，单槽降到 25%。",
        "- `index_mom60 > 10%`：不开仓。",
        f"- 资金口径：初始资金 {INITIAL_CAPITAL:,.0f}，{args.slots} 槽，每日最多 {args.daily_open_limit} 张；底层信号为 `score120_diff65_m30_ma20`。",
        "",
        "## 全周期对比",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 2024-09 后对比",
        "",
        _md_table(post, pct_cols=pct_cols),
        "",
        "## 2026 样本外对比",
        "",
        _md_table(blind, pct_cols=pct_cols),
        "",
        "## 三档规则分年",
        "",
        _md_table(tier_annual, pct_cols=pct_cols),
        "",
        "## 三档规则成交分桶",
        "",
        _md_table(tier_buckets, pct_cols=pct_cols),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")

    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "source_trades": str(SOURCE_TRADES),
        "policies": policies,
        "slots": int(args.slots),
        "daily_open_limit": int(args.daily_open_limit),
        "trades": int(len(trades)),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest score120 index_mom60 tiered position policy.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--daily-open-limit", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
