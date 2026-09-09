from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1]))

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

from scripts.gen2_alpha191_portfolio_overlay_matrix import _make_source  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


DEFAULT_SCORED = ROOT / "reports" / "gen2_alpha191_candidate_core10_t1" / "candidate_alpha191_scored.parquet"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_alpha191_light_constraint_matrix"

SEGMENTS = [
    ("train_2024h2_2025q1", "2024-07-09", "2025-03-31"),
    ("validation_2025q2_q4", "2025-04-01", "2025-12-31"),
    ("blind_2026ytd", "2026-01-01", "2026-05-21"),
]


from research.common.reporting import percent_text as _pct


def _safe_sharpe(curve: pd.DataFrame) -> float | None:
    if curve.empty or "strategy_equity" not in curve.columns:
        return None
    rets = pd.to_numeric(curve["strategy_equity"], errors="coerce").dropna().pct_change().dropna()
    if len(rets) < 2:
        return None
    std = float(rets.std(ddof=1))
    if std <= 0 or not math.isfinite(std):
        return None
    return float(rets.mean() / std * math.sqrt(252))


def _segment_metrics(curve: pd.DataFrame) -> list[dict[str, Any]]:
    d = curve.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    rows: list[dict[str, Any]] = []
    for name, start, end in SEGMENTS:
        g = d[(d["date"] >= pd.Timestamp(start)) & (d["date"] <= pd.Timestamp(end))].copy()
        if g.empty:
            continue
        first = float(g["strategy_equity"].iloc[0])
        last = float(g["strategy_equity"].iloc[-1])
        rows.append(
            {
                "segment": name,
                "return": last / first - 1.0 if first > 0 else None,
                "max_drawdown": float(g["drawdown"].min()) if "drawdown" in g.columns else None,
                "daily_sharpe": _safe_sharpe(g),
            }
        )
    return rows


def _train_thresholds(scored: pd.DataFrame, score_col: str, train_start: str, train_end: str) -> dict[str, float]:
    train = scored[(scored["entry_date"] >= train_start) & (scored["entry_date"] <= train_end)].copy()
    s = pd.to_numeric(train[score_col], errors="coerce").dropna()
    if s.empty:
        raise RuntimeError(f"No train score for {score_col}")
    return {"keep80": float(s.quantile(0.20)), "top50": float(s.quantile(0.50))}


def _apply_constraint(d: pd.DataFrame, constraint: str) -> pd.DataFrame:
    out = d.copy()
    parts = [x.strip() for x in constraint.split("+") if x.strip()]
    for part in parts:
        if part in {"none", "base", "off"}:
            continue
        if part == "overhead_le5":
            out = out[pd.to_numeric(out["overhead_pressure_amount_share"], errors="coerce").fillna(9.0) <= 0.05].copy()
        elif part == "runup_le100":
            out = out[pd.to_numeric(out["runup_from_60d_low"], errors="coerce").fillna(99.0) <= 1.00].copy()
        elif part == "breadth_le80":
            out = out[pd.to_numeric(out["market_breadth"], errors="coerce").fillna(9.0) <= 0.80].copy()
        elif part == "mom20_0_5":
            mom = pd.to_numeric(out["index_mom20"], errors="coerce")
            out = out[mom.between(0.0, 0.05, inclusive="both")].copy()
        elif part == "index_ge_ma20":
            out = out[out["index_close_ge_ma20"].fillna(False).astype(bool)].copy()
        else:
            raise ValueError(f"Unknown constraint: {part}")
    return out


def _run_one(source: pd.DataFrame, source_path: Path, run_dir: Path, policy: str, start_date: str, end_date: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source.to_parquet(source_path, index=False)
    source.to_csv(source_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    summary = _run_dynamic(source_path, run_dir, policy, start_date, end_date, sort_mode="rank")
    curve = pd.read_csv(run_dir / "equity_curve.csv")
    return summary, _segment_metrics(curve)


def _write_report(output_dir: Path, summary: pd.DataFrame, segments: pd.DataFrame) -> None:
    lines = [
        "# G2 Alpha191 Light Constraint Matrix",
        "",
        "| variant | signals | trades | total | max_dd | sharpe | win | avg_trade |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.sort_values(["total_return", "daily_sharpe"], ascending=[False, False]).itertuples(index=False):
        lines.append(
            f"| {row.variant} | {row.signal_count} | {row.trade_count} | {_pct(row.total_return)} | {_pct(row.max_drawdown)} | "
            f"{'' if pd.isna(row.daily_sharpe) else f'{float(row.daily_sharpe):.2f}'} | {_pct(row.win_rate)} | {_pct(row.avg_trade_return)} |"
        )
    if not segments.empty:
        lines.extend(["", "## Blind 2026", "", "| variant | return | max_dd | sharpe |", "| --- | ---: | ---: | ---: |"])
        blind = segments[segments["segment"].eq("blind_2026ytd")].sort_values("return_", ascending=False)
        for row in blind.itertuples(index=False):
            lines.append(
                f"| {row.variant} | {_pct(row.return_)} | {_pct(row.max_drawdown)} | "
                f"{'' if pd.isna(row.daily_sharpe) else f'{float(row.daily_sharpe):.2f}'} |"
            )
    (output_dir / "light_constraint_matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    scored = pd.read_parquet(args.scored)
    scored["entry_date"] = pd.to_datetime(scored["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    scores = [x.strip() for x in str(args.scores).split(",") if x.strip()]
    modes = [x.strip() for x in str(args.modes).split(",") if x.strip()]
    constraints = [x.strip() for x in str(args.constraints).split(",") if x.strip()]

    rows: list[dict[str, Any]] = []
    seg_rows: list[dict[str, Any]] = []
    for score_name in scores:
        thresholds = _train_thresholds(scored, f"score_{score_name}", str(args.train_start), str(args.train_end))
        for mode in modes:
            base_source = _make_source(scored, score_name, mode, thresholds)
            for constraint in constraints:
                source = _apply_constraint(base_source, constraint)
                variant = f"{score_name}_{mode}_{constraint.replace('+', '__')}"
                summary, segs = _run_one(
                    source,
                    output_dir / "sources" / f"{variant}.parquet",
                    output_dir / "backtests" / variant,
                    str(args.policy),
                    str(args.start_date),
                    str(args.end_date),
                )
                curve = pd.read_csv(output_dir / "backtests" / variant / "equity_curve.csv")
                rows.append(
                    {
                        "variant": variant,
                        "score_name": score_name,
                        "mode": mode,
                        "constraint": constraint,
                        "source_rows": int(len(source)),
                        "signal_count": summary.get("signal_count"),
                        "trade_count": summary.get("trade_count"),
                        "total_return": summary.get("total_return"),
                        "max_drawdown": summary.get("max_drawdown"),
                        "daily_sharpe": _safe_sharpe(curve),
                        "win_rate": summary.get("win_rate"),
                        "avg_trade_return": summary.get("avg_trade_return"),
                        "exit_reason_counts": json.dumps(summary.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
                    }
                )
                for seg in segs:
                    seg_rows.append({"variant": variant, "score_name": score_name, "mode": mode, "constraint": constraint, **seg})

    summary_df = pd.DataFrame(rows).sort_values(["total_return", "daily_sharpe"], ascending=[False, False])
    seg_df = pd.DataFrame(seg_rows)
    if not seg_df.empty:
        seg_df = seg_df.rename(columns={"return": "return_"})
    summary_df.to_csv(output_dir / "light_constraint_summary.csv", index=False, encoding="utf-8-sig")
    seg_df.to_csv(output_dir / "light_constraint_segment_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, summary_df, seg_df)
    payload = {
        "schema_version": 1,
        "outputs": {
            "summary": "light_constraint_summary.csv",
            "segments": "light_constraint_segment_summary.csv",
            "report": "light_constraint_matrix.md",
        },
        "top": summary_df.head(20).where(pd.notna(summary_df.head(20)), None).to_dict("records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Test light constraints on Alpha191 volume5 portfolio overlays.")
    parser.add_argument("--scored", default=str(DEFAULT_SCORED))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--scores", default="volume5")
    parser.add_argument("--modes", default="rank,keep80")
    parser.add_argument("--constraints", default="none,overhead_le5,runup_le100,breadth_le80,overhead_le5+runup_le100,overhead_le5+breadth_le80,runup_le100+breadth_le80,overhead_le5+runup_le100+breadth_le80")
    parser.add_argument("--policy", default="stop_cd3_skip")
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--train-start", default="2024-07-09")
    parser.add_argument("--train-end", default="2025-03-31")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
