from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _max_drawdown, _trade_calendar  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("score120_sector_diffusion_30m_overlay_v1")
SOURCE_TRADES = SOURCE_DIR / "base_trades_with_sector_diffusion_30m.csv"
OUT_DIR = report_path("score120_activation_regime_v1")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def _simulate(
    trades: pd.DataFrame,
    *,
    name: str,
    mask: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
    slots: int,
    slot_pct: float,
    daily_open_limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    selected = trades[mask].copy()
    if selected.empty:
        return pd.DataFrame(), pd.DataFrame()
    calendar = _trade_calendar(start, max(end, selected["policy_exit_date"].max()))
    by_entry = {day: g.copy() for day, g in selected.groupby("entry_date")}
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
        todays = by_entry.get(day)
        if todays is not None:
            todays = todays.sort_values(["rank_key", "amount_rank"], ascending=[False, False])
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
                "name": name,
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
    return curve, pd.DataFrame(closed)


def _metric(name: str, curve: pd.DataFrame, closed: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    rets = pd.to_numeric(closed.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
    if curve.empty:
        ret = 0.0
        dd = 0.0
        post = math.nan
        blind = math.nan
    else:
        ret = float(curve["equity"].iloc[-1] / curve["equity"].iloc[0] - 1.0)
        dd = _max_drawdown(curve["equity"])
        dates = pd.to_datetime(curve["date"], errors="coerce")
        post_curve = curve[dates.ge(pd.Timestamp("2024-09-24"))].copy()
        blind_curve = curve[dates.ge(pd.Timestamp("2026-01-01"))].copy()
        post = float(post_curve["equity"].iloc[-1] / post_curve["equity"].iloc[0] - 1.0) if len(post_curve) >= 2 else math.nan
        blind = float(blind_curve["equity"].iloc[-1] / blind_curve["equity"].iloc[0] - 1.0) if len(blind_curve) >= 2 else math.nan
    return {
        "name": name,
        "trades": int(len(closed)),
        "ret": ret,
        "dd": dd,
        "post": post,
        "blind": blind,
        "win": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg": float(rets.mean()) if len(rets) else 0.0,
        "worst": float(rets.min()) if len(rets) else 0.0,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not SOURCE_TRADES.exists():
        raise FileNotFoundError(f"missing source trades: {SOURCE_TRADES}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = pd.read_csv(SOURCE_TRADES, encoding="utf-8-sig", low_memory=False)
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    numeric_cols = [
        "net_ret",
        "gross_ret",
        "rank_key",
        "wave_style_score",
        "amount_rank",
        "sector_diffusion_score",
        "m30_close_above_ma20",
        "index_mom60",
        "index_mom20",
    ]
    for col in numeric_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    d = d[(d["entry_date"] >= start) & (d["entry_date"] <= end)].copy()
    base_mask = d["sector_diffusion_score"].ge(65.0) & d["m30_close_above_ma20"].ge(0.0)
    source = d[base_mask].copy()
    source["sector_gate_source"] = "diff65"
    source["sector_gate_rule"] = "sector_diffusion>=65 && m30_close_above_ma20>=0"
    source["original_policy_exit_date"] = source["policy_exit_date"]
    source["original_net_ret"] = source["net_ret"]
    source["risk_exit_note"] = "base_policy"
    source["d1_date"] = (source["entry_date"] + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    source["d2_date"] = (source["entry_date"] + pd.Timedelta(days=2)).dt.strftime("%Y-%m-%d")
    source["sig_index_mom60"] = source["index_mom60"]
    source["sig_index_mom20"] = source["index_mom20"]
    source.to_csv(OUT_DIR / "diff65_m30_trades_with_regime_corrected.csv", index=False, encoding="utf-8-sig")

    daily = (
        source[["entry_date", "sig_index_mom60", "sig_index_mom20"]]
        .drop_duplicates("entry_date")
        .rename(columns={"entry_date": "date"})
        .sort_values("date")
    )
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    daily.to_csv(OUT_DIR / "daily_market_regime_corrected.csv", index=False, encoding="utf-8-sig")

    masks = {
        "base": pd.Series(True, index=source.index),
        "sig_mom60_le_0.05": source["sig_index_mom60"].le(0.05),
        "sig_mom60_le_0.08": source["sig_index_mom60"].le(0.08),
        "not_sig_index_bull": source["sig_index_mom20"].le(0.0),
        "sig_mom60_le005_mktchg_ge0": source["sig_index_mom60"].le(0.05) & source["sig_index_mom20"].ge(0.0),
        "mkt_count_chg20_ge0": source.get("sector_candidate_count_chg5", pd.Series(0.0, index=source.index)).fillna(0).ge(0.0),
    }
    rows: list[dict[str, Any]] = []
    for name, mask in masks.items():
        curve, closed = _simulate(
            source,
            name=name,
            mask=mask,
            start=start,
            end=end,
            slots=int(args.slots),
            slot_pct=float(args.slot_pct),
            daily_open_limit=int(args.daily_open_limit),
        )
        run_dir = OUT_DIR / name
        run_dir.mkdir(parents=True, exist_ok=True)
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        rows.append(_metric(name, curve, closed, args.start_date, args.end_date))
    gates = pd.DataFrame(rows)
    gates.to_csv(OUT_DIR / "fixed_gate_results_corrected.csv", index=False, encoding="utf-8-sig")

    summary = {
        "status": "completed",
        "source": str(SOURCE_TRADES),
        "out_dir": str(OUT_DIR),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "source_rows": int(len(source)),
        "mom60_le_005_rows": int(source["sig_index_mom60"].le(0.05).sum()),
        "slots": int(args.slots),
        "slot_pct": float(args.slot_pct),
        "daily_open_limit": int(args.daily_open_limit),
    }
    (OUT_DIR / "build_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build score120 activation regime sources from 30m overlay outputs.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-07-06")
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--slot-pct", type=float, default=0.5)
    parser.add_argument("--daily-open-limit", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
