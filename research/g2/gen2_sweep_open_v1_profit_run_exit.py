from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_intraday_risk import run  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE  # noqa: E402

DEFAULT_OUTPUT_DIR = _report_path() / "gen2_open_v1_profit_run_exit"


from research.common.reporting import numpy_json_default as _json_default


from research.common.reporting import percent_text as _pct


def _parse_float_grid(text: str) -> list[float | None]:
    values: list[float | None] = []
    for item in str(text or "").split(","):
        token = item.strip().lower()
        if not token:
            continue
        if token in {"none", "null", "na"}:
            values.append(None)
        else:
            values.append(float(token))
    return values


def _parse_int_grid(text: str) -> list[int]:
    return [int(item.strip()) for item in str(text or "").split(",") if item.strip()]


def _profile_name(hold_days: int, trailing_stop: float | None) -> str:
    trail = "tr_none" if trailing_stop is None else f"tr{int(round(trailing_stop * 100))}"
    return f"hold{int(hold_days)}_{trail}"


def _trade_diagnostics(run_dir: Path) -> dict[str, Any]:
    trades_path = run_dir / "trades.csv"
    if not trades_path.exists():
        return {}
    trades = pd.read_csv(trades_path)
    if trades.empty:
        return {}
    trades["pnl"] = pd.to_numeric(trades.get("pnl"), errors="coerce")
    trades["return"] = pd.to_numeric(trades.get("return"), errors="coerce")
    gross_profit = float(trades.loc[trades["pnl"] > 0, "pnl"].sum())
    top5_profit = float(trades.sort_values("pnl", ascending=False).head(5)["pnl"].sum())
    reason_counts = trades["exit_reason"].fillna("").astype(str).value_counts().to_dict()
    return {
        "gross_profit": gross_profit,
        "top5_profit_share": top5_profit / gross_profit if gross_profit > 0 else None,
        "median_trade_return": float(trades["return"].median()) if trades["return"].notna().any() else None,
        "p90_trade_return": float(trades["return"].quantile(0.9)) if trades["return"].notna().any() else None,
        "exit_reason_counts": reason_counts,
    }


def run_sweep(
    output_dir: Path,
    signal_source: Path,
    start_date: str,
    end_date: str,
    hold_days_grid: list[int],
    trailing_stop_grid: list[float | None],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for hold_days in hold_days_grid:
        for trailing_stop in trailing_stop_grid:
            name = _profile_name(hold_days, trailing_stop)
            run_dir = output_dir / "runs" / name
            summary = run(
                signal_source=signal_source,
                output_dir=run_dir,
                start_date=start_date,
                end_date=end_date,
                initial_cash=150000.0,
                max_positions=2,
                max_buys_per_day=1,
                hold_days=hold_days,
                benchmark_code="000852.SH",
                buy_slippage_bps=5.0,
                sell_slippage_bps=5.0,
                commission_bps=2.5,
                stamp_tax_bps=5.0,
                stop_loss_pct=0.05,
                take_profit_pct=0.10,
                take_profit_sell_ratio=0.5,
                trailing_stop_pct=trailing_stop,
                sort_mode="trigger_time",
                max_position_weight=0.50,
            )
            diag = _trade_diagnostics(run_dir)
            rows.append(
                {
                    "profile": name,
                    "hold_days": int(hold_days),
                    "trailing_stop_pct": trailing_stop,
                    "signal_count": summary.get("signal_count"),
                    "trade_count": summary.get("trade_count"),
                    "total_return": summary.get("total_return"),
                    "benchmark_return": summary.get("benchmark_return"),
                    "excess_return": summary.get("excess_return"),
                    "max_drawdown": summary.get("max_drawdown"),
                    "win_rate": summary.get("win_rate"),
                    "avg_trade_return": summary.get("avg_trade_return"),
                    "median_trade_return": diag.get("median_trade_return"),
                    "p90_trade_return": diag.get("p90_trade_return"),
                    "top5_profit_share": diag.get("top5_profit_share"),
                    "exit_reason_counts": json.dumps(diag.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
                    "final_equity": summary.get("final_equity"),
                }
            )

    df = pd.DataFrame(rows)
    df["profit_run_score"] = (
        df["excess_return"].fillna(0.0)
        + df["total_return"].fillna(0.0) * 0.25
        + df["max_drawdown"].fillna(0.0) * 0.75
        + df["p90_trade_return"].fillna(0.0) * 0.20
    )
    df = df.sort_values(["profit_run_score", "total_return"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "conclusion": "profit_run_exit_conclusion.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_conclusion(output_dir, df)
    return payload


def _write_conclusion(output_dir: Path, df: pd.DataFrame) -> None:
    best = df.iloc[0].to_dict() if not df.empty else {}
    baseline = df[(df["hold_days"] == 3) & (df["trailing_stop_pct"].isna())]
    baseline_row = baseline.iloc[0].to_dict() if not baseline.empty else {}
    lines = [
        "# G2 Open V1 Profit-Run / Weakness-Exit Sweep",
        "",
        "Purpose: keep the entry and position rules frozen, then compare whether profits should run beyond the 3-day base and whether post-profit 30m trailing weakness exits help.",
        "",
        "Fixed base: max 2 positions, max 1 buy/day, 50% per stock, trigger_time sort, 30m -5% hard stop, +10% sell half, CSI1000 benchmark.",
        "",
        "Interpretation note: trailing stop only becomes active after the first +10% half take-profit. It is therefore a post-profit weakness exit, not an entry failure stop.",
        "",
    ]
    if baseline_row:
        lines.extend(
            [
                "## Baseline",
                "",
                f"- 3-day / no trailing: total {_pct(baseline_row.get('total_return'))}, excess {_pct(baseline_row.get('excess_return'))}, max drawdown {_pct(baseline_row.get('max_drawdown'))}, win {_pct(baseline_row.get('win_rate'))}, avg trade {_pct(baseline_row.get('avg_trade_return'))}.",
                "",
            ]
        )
    if best:
        lines.extend(
            [
                "## Best By Balanced Score",
                "",
                f"- `{best.get('profile')}`: total {_pct(best.get('total_return'))}, excess {_pct(best.get('excess_return'))}, max drawdown {_pct(best.get('max_drawdown'))}, win {_pct(best.get('win_rate'))}, avg trade {_pct(best.get('avg_trade_return'))}, p90 trade {_pct(best.get('p90_trade_return'))}.",
                "",
            ]
        )
    lines.extend(
        [
            "## Matrix",
            "",
            "| profile | hold | trail | trades | total | excess | max_dd | win | avg | median | p90 | top5_profit_share | exits |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['profile']} | {int(row.get('hold_days') or 0)} | {_pct(row.get('trailing_stop_pct'))} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {_pct(row.get('median_trade_return'))} | {_pct(row.get('p90_trade_return'))} | {_pct(row.get('top5_profit_share'))} | {row.get('exit_reason_counts') or ''} |"
        )
    (output_dir / "profit_run_exit_conclusion.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep profit-run and post-profit weakness exits for G2 Open V1 SL5.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--hold-days-grid", default="3,5,8,10")
    parser.add_argument("--trailing-stop-grid", default="none,0.04,0.06,0.08")
    args = parser.parse_args()
    payload = run_sweep(
        output_dir=Path(args.output_dir),
        signal_source=Path(args.signal_source),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        hold_days_grid=_parse_int_grid(args.hold_days_grid),
        trailing_stop_grid=_parse_float_grid(args.trailing_stop_grid),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
