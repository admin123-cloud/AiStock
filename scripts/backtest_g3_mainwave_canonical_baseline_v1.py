"""Transparent canonical-index baseline for the rebuilt G3 mainwave.

This deliberately excludes the legacy learned-sector labels and the old formal
26-trade extract.  It creates one observable shape-only candidate stream using
the QMT canonical SSE Composite (000001.SH), then applies the actual two-slot
capital constraint.  Exit refinement is intentionally a later, isolated step.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.backtest_wave_style_template_strategy_v1 as base  # noqa: E402
from utils.paths import report_path  # noqa: E402


def _window(df: pd.DataFrame, start: str, end: str) -> dict:
    x = df[(pd.to_datetime(df["entry_date"]) >= pd.Timestamp(start)) & (pd.to_datetime(df["entry_date"]) <= pd.Timestamp(end))]
    ret = pd.to_numeric(x.get("net_ret"), errors="coerce").dropna()
    return {
        "trades": int(len(x)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None,
        "worst_ret": float(ret.min()) if len(ret) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-30")
    parser.add_argument("--min-score", type=float, default=88.0)
    args = parser.parse_args()

    # Keep the earlier prefix-key output immutable: it is contaminated by the
    # 000001.SH/000001.SZ collision and remains useful only as an audit trail.
    out = report_path("g3_mainwave_canonical_baseline_v2_codekey")
    out.mkdir(parents=True, exist_ok=True)
    base.INDEX_CODE = "000001.SH"
    base.INDEX_FALLBACK_CODES = []
    ns = argparse.Namespace(
        start_date=args.start_date,
        end_date=args.end_date,
        min_score=args.min_score,
        strict_min_score=100.0,
        cost_bps=30.0,
    )
    load_start = (pd.Timestamp(args.start_date) - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
    load_end = (pd.Timestamp(args.end_date) + pd.Timedelta(days=90)).strftime("%Y-%m-%d")
    stocks = base._load_stocks()
    raw = base._load_daily(load_start, load_end).merge(stocks[["code_key", "stock_name", "industry"]], on="code_key", how="inner")
    features = base._attach_sector(base._add_features(raw, max_hold=20))
    index = base._load_index_features(load_start, load_end)
    if index.empty or set(index.get("index_code", pd.Series(dtype=str)).dropna()) - {"000001.SH"}:
        raise RuntimeError("canonical 000001.SH index features unavailable")
    features = features.merge(index, on="trade_date", how="left")
    candidates = base._build_signal_candidates(features, ns, 20, use_learned_sector=False, strict=False, market_gate="none")
    curve, closed = base._simulate(candidates, slots=2, slot_pct=0.50, daily_open_limit=1)
    candidates.to_csv(out / "shape_only_h20_candidates.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(out / "shape_only_h20_two_slot_closed_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(out / "shape_only_h20_two_slot_equity_curve.csv", index=False, encoding="utf-8-sig")
    summary = {
        "status": "completed",
        "index_code": "000001.SH",
        "candidate_definition": "shape_only_h20; exchange-qualified code key; no learned-sector label; signal-day observable; next-open entry",
        "portfolio_definition": "2 slots, 50% each, max 1 new entry per day, fixed 20-session exit",
        "candidates": int(len(candidates)),
        "candidate_days": int(candidates["entry_date"].nunique()) if not candidates.empty else 0,
        "closed_trades": int(len(closed)),
        "full": _window(closed, args.start_date, args.end_date),
        "recent_2024_2025": _window(closed, "2024-01-01", "2025-12-31"),
        "ytd_2026": _window(closed, "2026-01-01", args.end_date),
        "note": "Baseline only.  It must pass review before adding 30m entry/exit and loss-cooldown layers.",
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
