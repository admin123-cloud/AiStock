from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_intraday_risk import run  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE  # noqa: E402
from scripts.gen2_sweep_open_v1_market_gate import _build_gate_dates, _filtered_signal_source  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_open_v1_second_round"


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


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _run_sl5(signal_source: Path, output_dir: Path, start_date: str, end_date: str) -> dict[str, Any]:
    return run(
        signal_source=signal_source,
        output_dir=output_dir,
        start_date=start_date,
        end_date=end_date,
        initial_cash=150000.0,
        max_positions=2,
        max_buys_per_day=1,
        hold_days=3,
        benchmark_code="000852.SH",
        buy_slippage_bps=5.0,
        sell_slippage_bps=5.0,
        commission_bps=2.5,
        stamp_tax_bps=5.0,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
        take_profit_sell_ratio=0.5,
        trailing_stop_pct=None,
        sort_mode="trigger_time",
        max_position_weight=0.50,
    )


def _summary_row(group: str, profile: str, start_date: str, end_date: str, summary: dict[str, Any], note: str = "") -> dict[str, Any]:
    return {
        "group": group,
        "profile": profile,
        "start_date": start_date,
        "end_date": end_date,
        "note": note,
        "signal_count": summary.get("signal_count"),
        "trade_count": summary.get("trade_count"),
        "total_return": summary.get("total_return"),
        "benchmark_return": summary.get("benchmark_return"),
        "excess_return": summary.get("excess_return"),
        "max_drawdown": summary.get("max_drawdown"),
        "win_rate": summary.get("win_rate"),
        "avg_trade_return": summary.get("avg_trade_return"),
        "final_equity": summary.get("final_equity"),
    }


def _normalize_signal_frame(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if "confirm_datetime" in df.columns:
        df["confirm_datetime"] = pd.to_datetime(df["confirm_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    if "entry_date" in df.columns:
        df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["code"] = df["code"].astype(str)
    return df


def _write_signal_filter(df: pd.DataFrame, output_path: Path, remove_keys: Iterable[tuple[str, str]]) -> tuple[Path, int]:
    remove_set = set(remove_keys)
    key = list(zip(df["code"].astype(str), df["confirm_datetime"].astype(str)))
    mask = pd.Series([item not in remove_set for item in key], index=df.index)
    filtered = df[mask].copy()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filtered.to_parquet(output_path, index=False)
    return output_path, int(len(df) - len(filtered))


def _top_trade_removal(output_dir: Path, signal_source: Path, baseline_run: Path, start_date: str, end_date: str) -> list[dict[str, Any]]:
    trades = pd.read_csv(baseline_run / "trades.csv")
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce")
    trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    trades["code"] = trades["code"].astype(str)
    winners = trades.sort_values("pnl", ascending=False).dropna(subset=["pnl"])
    signals = _normalize_signal_frame(signal_source)

    rows: list[dict[str, Any]] = []
    for top_n in [1, 3, 5, 10]:
        remove_keys = list(zip(winners.head(top_n)["code"], winners.head(top_n)["buy_datetime"]))
        filtered_source, removed = _write_signal_filter(
            signals,
            output_dir / "signals" / f"remove_top{top_n}_trades.parquet",
            remove_keys,
        )
        summary = _run_sl5(
            signal_source=filtered_source,
            output_dir=output_dir / "runs" / f"remove_top{top_n}_trades",
            start_date=start_date,
            end_date=end_date,
        )
        rows.append(
            _summary_row(
                "top_trade_removal",
                f"remove_top{top_n}_trades",
                start_date,
                end_date,
                summary,
                note=f"removed_signals={removed}",
            )
        )

    code_pnl = trades.groupby("code", as_index=False)["pnl"].sum().sort_values("pnl", ascending=False)
    for top_n in [1, 3, 5]:
        remove_codes = set(code_pnl.head(top_n)["code"].astype(str))
        filtered = signals[~signals["code"].isin(remove_codes)].copy()
        filtered_source = output_dir / "signals" / f"remove_top{top_n}_codes.parquet"
        filtered.to_parquet(filtered_source, index=False)
        summary = _run_sl5(
            signal_source=filtered_source,
            output_dir=output_dir / "runs" / f"remove_top{top_n}_codes",
            start_date=start_date,
            end_date=end_date,
        )
        rows.append(
            _summary_row(
                "top_code_removal",
                f"remove_top{top_n}_codes",
                start_date,
                end_date,
                summary,
                note=f"codes={','.join(sorted(remove_codes))}",
            )
        )
    return rows


def _rolling_windows(output_dir: Path, signal_source: Path) -> list[dict[str, Any]]:
    windows = [
        ("w1_2024h2", "2024-07-09", "2024-12-31"),
        ("w2_2024q4_2025q1", "2024-10-01", "2025-03-31"),
        ("w3_2025h1", "2025-01-01", "2025-06-30"),
        ("w4_2025q2_q3", "2025-04-01", "2025-09-30"),
        ("w5_2025h2", "2025-07-01", "2025-12-31"),
        ("w6_2025q4_2026q1", "2025-10-01", "2026-03-31"),
        ("w7_2026ytd", "2026-01-01", "2026-05-21"),
    ]
    rows: list[dict[str, Any]] = []
    for name, start, end in windows:
        summary = _run_sl5(
            signal_source=signal_source,
            output_dir=output_dir / "runs" / name,
            start_date=start,
            end_date=end,
        )
        rows.append(_summary_row("rolling_window", name, start, end, summary))
    return rows


def _gate_splits(output_dir: Path, signal_source: Path) -> list[dict[str, Any]]:
    splits = [
        ("train_gate_csi_ma20", "2024-07-09", "2025-03-31"),
        ("validation_gate_csi_ma20", "2025-04-01", "2025-12-31"),
        ("blind_gate_csi_ma20", "2026-01-01", "2026-05-21"),
    ]
    rows: list[dict[str, Any]] = []
    for name, start, end in splits:
        gate_dates = _build_gate_dates(start, end)
        allowed_dates = gate_dates["csi1000_prev_close_ge_ma20"]
        filtered_source, gated_count = _filtered_signal_source(
            source=signal_source,
            output_path=output_dir / "signals" / f"{name}.parquet",
            allowed_dates=allowed_dates,
            start_date=start,
            end_date=end,
        )
        summary = _run_sl5(
            signal_source=filtered_source,
            output_dir=output_dir / "runs" / name,
            start_date=start,
            end_date=end,
        )
        rows.append(
            _summary_row(
                "gate_split",
                name,
                start,
                end,
                summary,
                note=f"allowed_days={len(allowed_dates or [])};gated_signals={gated_count}",
            )
        )
    return rows


def _write_outputs(output_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {
            "summary": "summary.csv",
            "conclusion": "second_round_conclusion.md",
            "runs": "runs/",
            "signals": "signals/",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_conclusion(output_dir, df)
    return payload


def _write_conclusion(output_dir: Path, df: pd.DataFrame) -> None:
    lines = [
        "# G2 Open V1 SL5 Second-Round Validation",
        "",
        "Frozen rule: NORMAL + pullback_restart_rank100 + 30m bottom_fractal_break_high_vol, max 2 positions, max 1 buy/day, 50% per stock, 30m -5% stop, +10% half take-profit, 3-day time exit.",
        "",
        "## Summary Table",
        "",
        "| group | profile | window | signals | trades | total | excess | max_dd | win | avg_trade | note |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['group']} | {row['profile']} | {row['start_date']}~{row['end_date']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('note') or ''} |"
        )

    full = df[(df["group"] == "top_trade_removal") | (df["group"] == "top_code_removal")].copy()
    rolling = df[df["group"] == "rolling_window"].copy()
    gates = df[df["group"] == "gate_split"].copy()
    lines.extend(["", "## First Read", ""])
    if not full.empty:
        worst_top = full.sort_values("total_return").iloc[0]
        lines.append(
            f"- Top-winner stress worst case is `{worst_top['profile']}`: total {_pct(worst_top.get('total_return'))}, excess {_pct(worst_top.get('excess_return'))}, max drawdown {_pct(worst_top.get('max_drawdown'))}."
        )
    if not rolling.empty:
        positive = int((rolling["total_return"] > 0).sum())
        excess_positive = int((rolling["excess_return"] > 0).sum())
        lines.append(f"- Rolling windows: {positive}/{len(rolling)} windows positive total return, {excess_positive}/{len(rolling)} windows positive excess.")
    if not gates.empty:
        lines.append(
            "- CSI1000 MA20 defensive gate was tested inside train/validation/blind only as a risk-control candidate, not as a replacement for the offensive base."
        )
    (output_dir / "second_round_conclusion.md").write_text("\n".join(lines), encoding="utf-8")


def run_second_round(output_dir: Path, signal_source: Path, start_date: str, end_date: str, baseline_run: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    baseline_summary = _run_sl5(signal_source, output_dir / "runs" / "baseline_full", start_date, end_date)
    rows.append(_summary_row("baseline", "baseline_full", start_date, end_date, baseline_summary))
    rows.extend(_top_trade_removal(output_dir, signal_source, baseline_run, start_date, end_date))
    rows.extend(_rolling_windows(output_dir, signal_source))
    rows.extend(_gate_splits(output_dir, signal_source))
    return _write_outputs(output_dir, rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Second-round validation for frozen G2 Open V1 SL5.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument(
        "--baseline-run",
        default=str(REPO_ROOT / "reports" / "gen2_open_v1_attack_drawdown" / "runs" / "attack_tp10_sl5_tr_none"),
    )
    args = parser.parse_args()
    payload = run_second_round(
        output_dir=Path(args.output_dir),
        signal_source=Path(args.signal_source),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        baseline_run=Path(args.baseline_run),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
