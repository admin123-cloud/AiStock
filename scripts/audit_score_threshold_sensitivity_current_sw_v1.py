from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_score120_mom60_gate_revalidation_v1 import _simulate  # noqa: E402
from scripts.backtest_score120_sector_diffusion_30m_overlay_v1 import _compute_30m_features  # noqa: E402
from scripts.backtest_score120_sector_diffusion_gate_v1 import _build_sector_diffusion  # noqa: E402
from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _load_index, _max_drawdown, _trade_calendar  # noqa: E402
from utils.paths import report_path  # noqa: E402


SCHED_DIR = report_path("wave_style_model_scheduler_v1")
OUT_DIR = report_path("score_threshold_sensitivity_current_sw_v1")
MODELS = [
    ("score110", "scheduler_focus_240d_score110_aggr25"),
    ("score115", "scheduler_focus_240d_score115_aggr25"),
    ("score120", "scheduler_focus_240d_score120_aggr25"),
    ("score125", "scheduler_focus_240d_score125_aggr25"),
]


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x:.2%}"


def _load_trades(model: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    path = SCHED_DIR / model / "closed_trades.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    d = pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in ["net_ret", "rank_key", "wave_style_score", "amount_rank", "index_mom20", "index_mom60"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d[(d["entry_date"] >= start) & (d["entry_date"] <= end)].copy()
    return d


def _metrics(curve: pd.DataFrame, closed: pd.DataFrame, name: str, window: str, start: str, end: str, index_df: pd.DataFrame) -> dict[str, Any]:
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    iw = index_df[(index_df["trade_date"] >= pd.Timestamp(start)) & (index_df["trade_date"] <= pd.Timestamp(end))].copy()
    if cw.empty:
        return {"model": name, "window": window, "closed": 0}
    net = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    strategy_ret = float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0)
    index_ret = float(iw["close"].iloc[-1] / iw["close"].iloc[0] - 1.0) if len(iw) >= 2 else math.nan
    return {
        "model": name,
        "window": window,
        "closed": int(len(tw)),
        "unique_codes": int(tw["code_raw"].nunique()) if (not tw.empty and "code_raw" in tw.columns) else 0,
        "strategy_ret": strategy_ret,
        "index_ret": index_ret,
        "excess_ret": strategy_ret - index_ret if math.isfinite(index_ret) else math.nan,
        "max_drawdown": _max_drawdown(cw["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "mean_trade_ret": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "sum_pnl": float(pd.to_numeric(tw.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()) if len(tw) else 0.0,
        "avg_open_positions": float(cw["open_positions"].mean()),
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _md_table(df: pd.DataFrame, pct_cols: set[str]) -> str:
    if df.empty:
        return "_No data_"
    d = df.copy()
    for col in pct_cols:
        if col in d.columns:
            d[col] = d[col].map(_pct)
    return d.to_markdown(index=False)


def run(start_date: str = "2020-01-01", end_date: str = "2026-07-03") -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    sector = _build_sector_diffusion(start, end)
    index_df = _load_index(start_date, end_date)
    windows = {
        "full": (start_date, end_date),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "post_2024_09": ("2024-09-24", end_date),
        "blind_2026ytd": ("2026-01-01", end_date),
    }
    summary_rows: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    for name, model in MODELS:
        trades = _load_trades(model, start, end)
        base_count = len(trades)
        trades = trades.merge(
            sector[
                [
                    "entry_date",
                    "l2_sector_name",
                    "sector_candidate_count",
                    "sector_share",
                    "sector_diffusion_score",
                ]
            ],
            on=["entry_date", "l2_sector_name"],
            how="left",
        )
        enriched = _compute_30m_features(trades)
        diff = enriched[pd.to_numeric(enriched["sector_diffusion_score"], errors="coerce") >= 65.0].copy()
        m30 = diff[pd.to_numeric(diff["m30_close_above_ma20"], errors="coerce") >= 0.0].copy()
        final = m30[pd.to_numeric(m30["index_mom60"], errors="coerce") <= 0.05].copy()
        final = final.dropna(subset=["entry_date", "policy_exit_date", "net_ret", "index_mom60"]).copy()
        calendar = _trade_calendar(start, max(end, final["policy_exit_date"].max() if not final.empty else end))
        curve, closed = _simulate(final, calendar, "base_no_mom60_gate", slots=2, daily_open_limit=2)
        run_dir = OUT_DIR / name
        run_dir.mkdir(parents=True, exist_ok=True)
        enriched.to_csv(run_dir / "signals_with_current_sw_diffusion_30m.csv", index=False, encoding="utf-8-sig")
        final.to_csv(run_dir / "final_signals.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        signal_rows.append(
            {
                "model": name,
                "base_trades": int(base_count),
                "diff65_trades": int(len(diff)),
                "diff65_m30_trades": int(len(m30)),
                "final_mom60_trades": int(len(final)),
                "final_signal_days": int(final["entry_date"].nunique()) if not final.empty else 0,
            }
        )
        for window, (w_start, w_end) in windows.items():
            summary_rows.append(_metrics(curve, closed, name, window, w_start, w_end, index_df))
    signals = pd.DataFrame(signal_rows)
    summary = pd.DataFrame(summary_rows)
    signals.to_csv(OUT_DIR / "signal_counts.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "portfolio_summary.csv", index=False, encoding="utf-8-sig")
    pct_cols = {"strategy_ret", "index_ret", "excess_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade"}
    full = summary[summary["window"].eq("full")].copy()
    blind = summary[summary["window"].eq("blind_2026ytd")].copy()
    lines = [
        "# Score threshold sensitivity with current Shenwan sectors v1",
        "",
        "## Signal counts",
        "",
        signals.to_markdown(index=False),
        "",
        "## Full window",
        "",
        _md_table(full, pct_cols),
        "",
        "## 2026 YTD",
        "",
        _md_table(blind, pct_cols),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    result = {"status": "completed", "out_dir": str(OUT_DIR), "models": [x[0] for x in MODELS]}
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
