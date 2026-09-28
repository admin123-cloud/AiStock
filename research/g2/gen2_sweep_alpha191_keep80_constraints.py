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
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


DEFAULT_SOURCE = _report_path() / "gen2_alpha191_t1_keep80_stop_cd3_full" / "variant_sources" / "alpha191_gate_keep80.parquet"
DEFAULT_OUTPUT = _report_path() / "gen2_alpha191_keep80_constraint_sweep"


def _sharpe(curve: pd.DataFrame) -> float | None:
    if curve.empty or "strategy_equity" not in curve.columns:
        return None
    equity = pd.to_numeric(curve["strategy_equity"], errors="coerce")
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return None
    std = float(rets.std(ddof=1))
    if std <= 0 or not math.isfinite(std):
        return None
    return float(rets.mean() / std * math.sqrt(252))


from research.common.reporting import percent_text as _pct


def _profit_concentration(summary: dict[str, Any], trades: pd.DataFrame) -> dict[str, Any]:
    final_equity = float(summary.get("final_equity") or 0.0)
    total_return = float(summary.get("total_return") or 0.0)
    if final_equity <= 0 or total_return <= -0.999999 or trades.empty or "pnl" not in trades.columns:
        return {}
    initial_equity = final_equity / (1.0 + total_return)
    total_pnl = final_equity - initial_equity
    pnl = pd.to_numeric(trades["pnl"], errors="coerce").dropna()
    winners = pnl[pnl > 0].sort_values(ascending=False)
    if total_pnl <= 0 or winners.empty:
        return {
            "top1_profit_share": None,
            "top3_profit_share": None,
            "top5_profit_share": None,
            "remove_top3_return": total_return,
        }

    def share(n: int) -> float:
        return float(winners.head(n).sum() / total_pnl)

    def remove_return(n: int) -> float:
        return float((final_equity - float(winners.head(n).sum())) / initial_equity - 1.0)

    return {
        "top1_profit_share": share(1),
        "top3_profit_share": share(3),
        "top5_profit_share": share(5),
        "remove_top3_return": remove_return(3),
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _variant_specs() -> list[tuple[str, str, Callable[[pd.DataFrame], pd.Series]]]:
    num = lambda d, c: pd.to_numeric(d[c], errors="coerce")
    return [
        ("baseline_keep80", "keep80 原始候选池", lambda d: pd.Series(True, index=d.index)),
        ("score_ge_045", "Alpha191 分数 >= 0.45", lambda d: num(d, "alpha191_gate_score") >= 0.45),
        ("score_ge_050", "Alpha191 分数 >= 0.50", lambda d: num(d, "alpha191_gate_score") >= 0.50),
        ("score_ge_055", "Alpha191 分数 >= 0.55", lambda d: num(d, "alpha191_gate_score") >= 0.55),
        ("score_ge_060", "Alpha191 分数 >= 0.60", lambda d: num(d, "alpha191_gate_score") >= 0.60),
        ("entry_pass_only", "只保留 V4 entry_pass", lambda d: d["entry_pass"].fillna(False).astype(bool)),
        ("rank_up_only", "只保留 V4 排名上升", lambda d: d["rank_change_status"].astype(str).eq("up")),
        ("v4_rank_le_100", "V4 rank <= 100", lambda d: num(d, "v4_rank") <= 100),
        ("v4_rank_le_80", "V4 rank <= 80", lambda d: num(d, "v4_rank") <= 80),
        ("cap_lt200", "流通市值 < 200 亿", lambda d: d["cap_bucket"].astype(str).isin(["lt100", "100_200"])),
        ("cap_gt200", "流通市值 >= 200 亿", lambda d: d["cap_bucket"].astype(str).eq("gt200")),
        ("cap_lt100", "流通市值 < 100 亿", lambda d: d["cap_bucket"].astype(str).eq("lt100")),
        ("no_downtrend_rebound", "排除长期下跌反弹", lambda d: ~d["downtrend_rebound_reject"].fillna(False).astype(bool)),
        (
            "overhead_share_le_003",
            "上方压力金额占比 <= 3%",
            lambda d: num(d, "overhead_pressure_amount_share").fillna(0.0) <= 0.03,
        ),
        (
            "overhead_share_le_005",
            "上方压力金额占比 <= 5%",
            lambda d: num(d, "overhead_pressure_amount_share").fillna(0.0) <= 0.05,
        ),
        ("rt_amt_2_6", "30m 量比 2-6", lambda d: num(d, "rt_30m_amount_ratio").between(2.0, 6.0)),
        ("rt_amt_2_5", "30m 量比 2-5", lambda d: num(d, "rt_30m_amount_ratio").between(2.0, 5.0)),
        (
            "score050_no_downtrend",
            "分数 >= 0.50 且排除下跌反弹",
            lambda d: (num(d, "alpha191_gate_score") >= 0.50) & (~d["downtrend_rebound_reject"].fillna(False).astype(bool)),
        ),
        (
            "score050_overhead005",
            "分数 >= 0.50 且压力占比 <= 5%",
            lambda d: (num(d, "alpha191_gate_score") >= 0.50)
            & (num(d, "overhead_pressure_amount_share").fillna(0.0) <= 0.05),
        ),
        (
            "score050_rank_up",
            "分数 >= 0.50 且 V4 排名上升",
            lambda d: (num(d, "alpha191_gate_score") >= 0.50) & d["rank_change_status"].astype(str).eq("up"),
        ),
        (
            "score050_cap_lt200",
            "分数 >= 0.50 且市值 < 200 亿",
            lambda d: (num(d, "alpha191_gate_score") >= 0.50)
            & d["cap_bucket"].astype(str).isin(["lt100", "100_200"]),
        ),
        (
            "quality_combo",
            "分数 >= 0.50 + 压力<=5% + 排除下跌反弹 + 量比2-6",
            lambda d: (num(d, "alpha191_gate_score") >= 0.50)
            & (num(d, "overhead_pressure_amount_share").fillna(0.0) <= 0.05)
            & (~d["downtrend_rebound_reject"].fillna(False).astype(bool))
            & num(d, "rt_30m_amount_ratio").between(2.0, 6.0),
        ),
    ]


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    variant_dir = output_dir / "variant_sources"
    variant_dir.mkdir(parents=True, exist_ok=True)

    base = pd.read_parquet(source)
    rows: list[dict[str, Any]] = []
    include_variants = {x.strip() for x in str(args.include_variants).split(",") if x.strip()}
    for name, note, mask_fn in _variant_specs():
        if include_variants and name not in include_variants:
            continue
        mask = mask_fn(base).fillna(False)
        variant = base[mask].copy()
        if len(variant) < int(args.min_signals):
            continue
        variant_path = variant_dir / f"{name}.parquet"
        variant.to_parquet(variant_path, index=False)
        for sort_mode in [x.strip() for x in str(args.sort_modes).split(",") if x.strip()]:
            for policy in [x.strip() for x in str(args.policies).split(",") if x.strip()]:
                run_dir = output_dir / "backtests" / name / sort_mode / policy
                summary = _run_dynamic(
                    signal_source=variant_path,
                    output_dir=run_dir,
                    policy=policy,
                    start_date=str(args.start_date),
                    end_date=str(args.end_date),
                    sort_mode=sort_mode,
                )
                curve = pd.read_csv(run_dir / "equity_curve.csv")
                trades = pd.read_csv(run_dir / "trades.csv")
                concentration = _profit_concentration(summary, trades)
                row = {
                    "variant": name,
                    "note": note,
                    "sort_mode": sort_mode,
                    "policy": policy,
                    "signals": int(len(variant)),
                    "trades": int(summary.get("trade_count") or 0),
                    "total_return": summary.get("total_return"),
                    "excess_return": summary.get("excess_return"),
                    "max_drawdown": summary.get("max_drawdown"),
                    "win_rate": summary.get("win_rate"),
                    "avg_trade_return": summary.get("avg_trade_return"),
                    "daily_sharpe": _sharpe(curve),
                    **concentration,
                    "run_dir": str(run_dir),
                }
                row["objective"] = (
                    (float(row["daily_sharpe"] or 0.0) * 0.45)
                    + (float(row["total_return"] or 0.0) * 0.35)
                    + (float(row["max_drawdown"] or 0.0) * 0.80)
                    - (float(row.get("top3_profit_share") or 0.0) * 0.25)
                )
                rows.append(row)

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values(["objective", "daily_sharpe", "total_return"], ascending=[False, False, False])
    result.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, result)
    payload = {
        "schema_version": 1,
        "source": str(source),
        "policies": str(args.policies),
        "rows": result.where(pd.notna(result), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "report.md", "runs": "backtests/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def _write_report(output_dir: Path, result: pd.DataFrame) -> None:
    lines = [
        "# Alpha191 keep80 Constraint Sweep",
        "",
        "Objective favors higher Sharpe/return, lower drawdown and lower Top3 profit concentration.",
        "",
        "| rank | variant | sort | policy | signals | trades | total | sharpe | max_dd | top3 | remove_top3 | note |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for idx, row in enumerate(result.head(30).to_dict("records"), start=1):
        lines.append(
            f"| {idx} | {row['variant']} | {row['sort_mode']} | {row['policy']} | {int(row['signals'])} | {int(row['trades'])} | "
            f"{_pct(row.get('total_return'))} | {float(row.get('daily_sharpe') or 0):.2f} | "
            f"{_pct(row.get('max_drawdown'))} | {_pct(row.get('top3_profit_share'))} | "
            f"{_pct(row.get('remove_top3_return'))} | {row.get('note', '')} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep constraints around Alpha191 keep80 G2 candidate pool.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--policies", default="stop_cd3_skip")
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--sort-modes", default="trigger_time,score")
    parser.add_argument("--min-signals", type=int, default=30)
    parser.add_argument("--include-variants", default="")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
