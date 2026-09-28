from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = REPO_ROOT / "reports" / "gen2_single_pattern_backtest"
OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pattern_contribution"
DEFAULT_INITIAL_CAPITAL = 150000.0
DEFAULT_TARGETS = {
    ("high_score_core", 10),
    ("removed_return", 5),
    ("removed_return", 10),
}


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return str(value)


def _safe_div(a: float, b: float) -> float:
    return float(a / b) if b else 0.0


def _max_drawdown_from_trades(trades: pd.DataFrame, initial_capital: float) -> float:
    if trades.empty:
        return 0.0
    d = trades.sort_values("exit_date").copy()
    equity = pd.concat(
        [
            pd.Series([float(initial_capital)]),
            initial_capital + d["pnl"].cumsum().reset_index(drop=True),
        ],
        ignore_index=True,
    )
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def _trade_group(trades: pd.DataFrame) -> pd.core.groupby.generic.DataFrameGroupBy:
    return trades.groupby(["pattern", "hold_days"], sort=True)


def _build_contribution_summary(trades: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for (pattern, hold_days), g in _trade_group(trades):
        g = g.copy()
        total_pnl = float(g["pnl"].sum())
        winners = g[g["pnl"] > 0].sort_values("pnl", ascending=False)
        losers = g[g["pnl"] < 0].sort_values("pnl", ascending=True)
        top1 = float(winners["pnl"].head(1).sum())
        top3 = float(winners["pnl"].head(3).sum())
        top5 = float(winners["pnl"].head(5).sum())
        bottom1 = float(losers["pnl"].head(1).sum())
        bottom3 = float(losers["pnl"].head(3).sum())
        rows.append(
            {
                "pattern": pattern,
                "hold_days": int(hold_days),
                "trade_count": int(len(g)),
                "winner_count": int(len(winners)),
                "loser_count": int(len(losers)),
                "total_pnl": total_pnl,
                "total_pnl_return": _safe_div(total_pnl, initial_capital),
                "avg_pnl": float(g["pnl"].mean()) if len(g) else 0.0,
                "median_pnl": float(g["pnl"].median()) if len(g) else 0.0,
                "avg_net_ret": float(g["net_ret"].mean()) if len(g) else 0.0,
                "median_net_ret": float(g["net_ret"].median()) if len(g) else 0.0,
                "top1_pnl": top1,
                "top3_pnl": top3,
                "top5_pnl": top5,
                "top1_share_of_total": _safe_div(top1, total_pnl),
                "top3_share_of_total": _safe_div(top3, total_pnl),
                "top5_share_of_total": _safe_div(top5, total_pnl),
                "bottom1_pnl": bottom1,
                "bottom3_pnl": bottom3,
                "max_drawdown_trade_order": _max_drawdown_from_trades(g, initial_capital),
            }
        )
    return pd.DataFrame(rows)


def _build_leave_topn(trades: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for (pattern, hold_days), g in _trade_group(trades):
        base_pnl = float(g["pnl"].sum())
        winners = g[g["pnl"] > 0].sort_values("pnl", ascending=False)
        for n in [1, 2, 3, 5, 10]:
            removed = winners.head(n)
            remain = g.drop(index=removed.index)
            pnl = float(remain["pnl"].sum())
            rows.append(
                {
                    "pattern": pattern,
                    "hold_days": int(hold_days),
                    "remove_top_winners": n,
                    "removed_trade_count": int(len(removed)),
                    "base_pnl": base_pnl,
                    "remaining_pnl": pnl,
                    "remaining_pnl_return": _safe_div(pnl, initial_capital),
                    "remaining_avg_net_ret": float(remain["net_ret"].mean()) if len(remain) else 0.0,
                    "remaining_median_net_ret": float(remain["net_ret"].median()) if len(remain) else 0.0,
                    "remaining_win_rate": float((remain["net_ret"] > 0).mean()) if len(remain) else 0.0,
                }
            )
    return pd.DataFrame(rows)


def _build_code_concentration(trades: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for (pattern, hold_days), g in _trade_group(trades):
        total_abs_pnl = float(g["pnl"].abs().sum())
        by_code = (
            g.groupby(["code", "name"], dropna=False)
            .agg(trade_count=("code", "size"), pnl=("pnl", "sum"), avg_net_ret=("net_ret", "mean"))
            .reset_index()
        )
        by_code["abs_pnl"] = by_code["pnl"].abs()
        by_code = by_code.sort_values("abs_pnl", ascending=False)
        for rank, (_, row) in enumerate(by_code.head(20).iterrows(), start=1):
            rows.append(
                {
                    "pattern": pattern,
                    "hold_days": int(hold_days),
                    "rank": rank,
                    "code": row["code"],
                    "name": row["name"],
                    "trade_count": int(row["trade_count"]),
                    "pnl": float(row["pnl"]),
                    "abs_pnl_share": _safe_div(float(row["abs_pnl"]), total_abs_pnl),
                    "avg_net_ret": float(row["avg_net_ret"]),
                }
            )
    return pd.DataFrame(rows)


def _build_month_contribution(trades: pd.DataFrame) -> pd.DataFrame:
    d = trades.copy()
    d["entry_month"] = pd.to_datetime(d["entry_date"]).dt.strftime("%Y-%m")
    rows: List[Dict[str, Any]] = []
    for (pattern, hold_days, month), g in d.groupby(["pattern", "hold_days", "entry_month"], sort=True):
        rows.append(
            {
                "pattern": pattern,
                "hold_days": int(hold_days),
                "entry_month": month,
                "trade_count": int(len(g)),
                "pnl": float(g["pnl"].sum()),
                "avg_net_ret": float(g["net_ret"].mean()) if len(g) else 0.0,
                "median_net_ret": float(g["net_ret"].median()) if len(g) else 0.0,
                "win_rate": float((g["net_ret"] > 0).mean()) if len(g) else 0.0,
                "best_trade_pnl": float(g["pnl"].max()) if len(g) else 0.0,
                "worst_trade_pnl": float(g["pnl"].min()) if len(g) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _build_extreme_trades(trades: pd.DataFrame, side: str, n: int = 10) -> pd.DataFrame:
    rows: List[pd.DataFrame] = []
    ascending = side == "worst"
    for _, g in _trade_group(trades):
        rows.append(g.sort_values("pnl", ascending=ascending).head(n))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def run(input_dir: Path, output_dir: Path, initial_capital: float, targets: set[tuple[str, int]]) -> Dict[str, Any]:
    trades_path = input_dir / "trades.csv"
    summary_path = input_dir / "summary.csv"
    if not trades_path.exists():
        raise FileNotFoundError(f"Missing trades.csv: {trades_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    trades = pd.read_csv(trades_path)
    summary = pd.read_csv(summary_path) if summary_path.exists() else pd.DataFrame()
    trades["hold_days"] = trades["hold_days"].astype(int)
    if targets:
        mask = pd.Series(False, index=trades.index)
        for pattern, hold_days in targets:
            mask = mask | ((trades["pattern"] == pattern) & (trades["hold_days"] == int(hold_days)))
        trades = trades[mask].copy()
    contribution = _build_contribution_summary(trades, initial_capital)
    leave_topn = _build_leave_topn(trades, initial_capital)
    code_concentration = _build_code_concentration(trades)
    month_contribution = _build_month_contribution(trades)
    best_trades = _build_extreme_trades(trades, "best")
    worst_trades = _build_extreme_trades(trades, "worst")

    contribution.to_csv(output_dir / "contribution_summary.csv", index=False, encoding="utf-8-sig")
    leave_topn.to_csv(output_dir / "leave_topn.csv", index=False, encoding="utf-8-sig")
    code_concentration.to_csv(output_dir / "code_concentration.csv", index=False, encoding="utf-8-sig")
    month_contribution.to_csv(output_dir / "month_contribution.csv", index=False, encoding="utf-8-sig")
    best_trades.to_csv(output_dir / "best_trades.csv", index=False, encoding="utf-8-sig")
    worst_trades.to_csv(output_dir / "worst_trades.csv", index=False, encoding="utf-8-sig")

    selected_summary = summary[
        summary.apply(lambda row: (row.get("pattern"), int(row.get("hold_days"))) in targets, axis=1)
    ].copy() if not summary.empty and targets else summary
    selected_summary.to_csv(output_dir / "selected_backtest_summary.csv", index=False, encoding="utf-8-sig")

    payload = {
        "schema_version": 1,
        "input_dir": str(input_dir),
        "initial_capital": float(initial_capital),
        "targets": [{"pattern": p, "hold_days": h} for p, h in sorted(targets)],
        "outputs": {
            "contribution_summary": "contribution_summary.csv",
            "leave_topn": "leave_topn.csv",
            "code_concentration": "code_concentration.csv",
            "month_contribution": "month_contribution.csv",
            "best_trades": "best_trades.csv",
            "worst_trades": "worst_trades.csv",
            "selected_backtest_summary": "selected_backtest_summary.csv",
        },
        "contribution_summary": contribution.to_dict(orient="records"),
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def _parse_targets(text: str) -> set[tuple[str, int]]:
    if not text:
        return set(DEFAULT_TARGETS)
    targets: set[tuple[str, int]] = set()
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        pattern, hold = item.rsplit(":", 1)
        targets.add((pattern.strip(), int(hold)))
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze G2 single-pattern trade contribution.")
    parser.add_argument("--input-dir", default=str(INPUT_DIR))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--initial-capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument("--targets", default="")
    args = parser.parse_args()
    payload = run(
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
        initial_capital=float(args.initial_capital),
        targets=_parse_targets(str(args.targets)),
    )
    print(json.dumps({"output_dir": str(args.output_dir), "targets": payload["targets"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
