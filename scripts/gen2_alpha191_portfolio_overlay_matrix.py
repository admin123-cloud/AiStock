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

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


DEFAULT_SCORED = ROOT / "reports" / "gen2_alpha191_candidate_core10_t1" / "candidate_alpha191_scored.parquet"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_alpha191_portfolio_overlay_matrix"

DEFAULT_SCORES = [
    "repair2",
    "Alpha144",
    "Alpha097",
    "main3",
    "volume5",
    "core10",
]

SEGMENTS = [
    ("train_2024h2_2025q1", "2024-07-09", "2025-03-31"),
    ("validation_2025q2_q4", "2025-04-01", "2025-12-31"),
    ("blind_2026ytd", "2026-01-01", "2026-05-21"),
]


from research.common.reporting import percent_text as _pct


def _safe_sharpe(curve: pd.DataFrame) -> float | None:
    if curve.empty or "strategy_equity" not in curve.columns:
        return None
    equity = pd.to_numeric(curve["strategy_equity"], errors="coerce").dropna()
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return None
    std = float(rets.std(ddof=1))
    if std <= 0 or not math.isfinite(std):
        return None
    return float(rets.mean() / std * math.sqrt(252))


def _segment_metrics(curve: pd.DataFrame) -> list[dict[str, Any]]:
    if curve.empty:
        return []
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
                "days": int(len(g)),
            }
        )
    return rows


def _train_thresholds(scored: pd.DataFrame, score_col: str, train_start: str, train_end: str) -> dict[str, float]:
    train = scored[
        (scored["entry_date"] >= str(train_start))
        & (scored["entry_date"] <= str(train_end))
    ].copy()
    s = pd.to_numeric(train[score_col], errors="coerce").dropna()
    if s.empty:
        raise RuntimeError(f"No train score for {score_col}.")
    return {
        "keep80": float(s.quantile(0.20)),
        "top50": float(s.quantile(0.50)),
    }


def _rank_by_score(d: pd.DataFrame, score_col: str) -> pd.DataFrame:
    out = d.copy()
    score = pd.to_numeric(out[score_col], errors="coerce")
    out["alpha191_overlay_score"] = score
    out["alpha191_original_v4_rank"] = pd.to_numeric(out["v4_rank"], errors="coerce")
    out["alpha191_original_v4_score"] = pd.to_numeric(out["v4_score"], errors="coerce")
    out["v4_score"] = score.fillna(-1.0)
    out["v4_rank"] = (
        out.groupby("entry_date")["alpha191_overlay_score"]
        .rank(method="first", ascending=False, na_option="bottom")
        .fillna(999)
        .astype(int)
    )
    return out


def _make_source(
    scored: pd.DataFrame,
    score_name: str,
    mode: str,
    thresholds: dict[str, float],
) -> pd.DataFrame:
    score_col = f"score_{score_name}"
    if score_col not in scored.columns:
        raise RuntimeError(f"Missing score column: {score_col}")
    out = scored.copy()
    out["alpha191_overlay_score_name"] = score_name
    out["alpha191_overlay_mode"] = mode
    out["alpha191_overlay_score"] = pd.to_numeric(out[score_col], errors="coerce")

    if mode == "baseline":
        return out
    if mode == "rank":
        return _rank_by_score(out, score_col)
    if mode == "keep80":
        out = out[out["alpha191_overlay_score"] >= thresholds["keep80"]].copy()
        out["alpha191_overlay_threshold"] = thresholds["keep80"]
        return _rank_by_score(out, score_col)
    if mode == "top50":
        out = out[out["alpha191_overlay_score"] >= thresholds["top50"]].copy()
        out["alpha191_overlay_threshold"] = thresholds["top50"]
        return _rank_by_score(out, score_col)
    if mode == "tilt":
        out = _rank_by_score(out, score_col)
        score = pd.to_numeric(out["alpha191_overlay_score"], errors="coerce").fillna(0.0)
        out["alpha191_position_weight"] = (0.50 + 0.50 * score).clip(lower=0.50, upper=1.00)
        return out
    raise ValueError(f"Unknown overlay mode: {mode}")


def _run_one(
    source: pd.DataFrame,
    source_path: Path,
    run_dir: Path,
    policy: str,
    start_date: str,
    end_date: str,
    sort_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source.to_parquet(source_path, index=False)
    source.to_csv(source_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    summary = _run_dynamic(
        signal_source=source_path,
        output_dir=run_dir,
        policy=policy,
        start_date=start_date,
        end_date=end_date,
        sort_mode=sort_mode,
    )
    curve = pd.read_csv(run_dir / "equity_curve.csv")
    return summary, _segment_metrics(curve)


def _write_report(output_dir: Path, rows: pd.DataFrame, segment_rows: pd.DataFrame) -> None:
    lines = [
        "# G2 Alpha191 Portfolio Overlay Matrix",
        "",
        "All variants use T-1 Alpha191 scores and the same dynamic 30m exit/cooldown engine.",
        "",
        "## Full Window",
        "",
        "| variant | score | mode | signals | trades | total | max_dd | sharpe | win | avg_trade |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    show = rows.sort_values(["total_return", "daily_sharpe"], ascending=[False, False])
    for row in show.itertuples(index=False):
        lines.append(
            f"| {row.variant} | {row.score_name} | {row.mode} | {row.signal_count} | {row.trade_count} | "
            f"{_pct(row.total_return)} | {_pct(row.max_drawdown)} | "
            f"{'' if pd.isna(row.daily_sharpe) else f'{float(row.daily_sharpe):.2f}'} | "
            f"{_pct(row.win_rate)} | {_pct(row.avg_trade_return)} |"
        )

    if not segment_rows.empty:
        lines.extend(
            [
                "",
                "## Segments",
                "",
                "| variant | segment | return | max_dd | sharpe |",
                "| --- | --- | ---: | ---: | ---: |",
            ]
        )
        seg = segment_rows.sort_values(["segment", "return_"], ascending=[True, False])
        for row in seg.itertuples(index=False):
            lines.append(
                f"| {row.variant} | {row.segment} | {_pct(row.return_)} | {_pct(row.max_drawdown)} | "
                f"{'' if pd.isna(row.daily_sharpe) else f'{float(row.daily_sharpe):.2f}'} |"
            )
    (output_dir / "portfolio_overlay_matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    scored = pd.read_parquet(args.scored)
    scored["entry_date"] = pd.to_datetime(scored["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    scores = [x.strip() for x in str(args.scores).split(",") if x.strip()]
    modes = [x.strip() for x in str(args.modes).split(",") if x.strip()]

    rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []

    baseline_done = False
    for score_name in scores:
        score_col = f"score_{score_name}"
        thresholds = _train_thresholds(scored, score_col, str(args.train_start), str(args.train_end))
        for mode in modes:
            if mode == "baseline":
                if baseline_done:
                    continue
                score_label = "none"
                variant = "baseline_trigger_time"
                source = _make_source(scored, score_name, mode, thresholds)
                sort_mode = "trigger_time"
                baseline_done = True
            else:
                score_label = score_name
                variant = f"{score_name}_{mode}"
                source = _make_source(scored, score_name, mode, thresholds)
                sort_mode = "rank"
            source_path = output_dir / "sources" / f"{variant}.parquet"
            run_dir = output_dir / "backtests" / variant
            summary, segs = _run_one(
                source=source,
                source_path=source_path,
                run_dir=run_dir,
                policy=str(args.policy),
                start_date=str(args.start_date),
                end_date=str(args.end_date),
                sort_mode=sort_mode,
            )
            curve = pd.read_csv(run_dir / "equity_curve.csv")
            row = {
                "variant": variant,
                "score_name": score_label,
                "mode": mode,
                "policy": str(args.policy),
                "sort_mode": sort_mode,
                "threshold_keep80": None if mode == "baseline" else thresholds["keep80"],
                "threshold_top50": None if mode == "baseline" else thresholds["top50"],
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
            rows.append(row)
            for seg in segs:
                segment_rows.append({"variant": variant, "score_name": score_label, "mode": mode, **seg})

    result = pd.DataFrame(rows).sort_values(["total_return", "daily_sharpe"], ascending=[False, False])
    seg_result = pd.DataFrame(segment_rows)
    if not seg_result.empty:
        seg_result = seg_result.rename(columns={"return": "return_"})
    result.to_csv(output_dir / "portfolio_overlay_summary.csv", index=False, encoding="utf-8-sig")
    seg_result.to_csv(output_dir / "portfolio_overlay_segment_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, result, seg_result)
    payload = {
        "schema_version": 1,
        "scored": str(args.scored),
        "policy": str(args.policy),
        "scores": scores,
        "modes": modes,
        "outputs": {
            "summary": "portfolio_overlay_summary.csv",
            "segments": "portfolio_overlay_segment_summary.csv",
            "report": "portfolio_overlay_matrix.md",
        },
        "top": result.head(20).where(pd.notna(result.head(20)), None).to_dict("records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Alpha191 overlay modes inside the G2/V4 candidate portfolio.")
    parser.add_argument("--scored", default=str(DEFAULT_SCORED))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--scores", default=",".join(DEFAULT_SCORES))
    parser.add_argument("--modes", default="baseline,rank,keep80,top50,tilt")
    parser.add_argument("--policy", default="stop_cd3_skip")
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--train-start", default="2024-07-09")
    parser.add_argument("--train-end", default="2025-03-31")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
