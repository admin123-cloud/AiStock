from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_intraday_risk import run  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_open_v1_attack_drawdown"


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


def _clean_optional_floats(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.astype(object)
    return cleaned.where(pd.notna(cleaned), None)


def _run_name(stop_loss: float | None, trailing_stop: float | None) -> str:
    stop_part = "sl_none" if stop_loss is None else f"sl{int(round(stop_loss * 100))}"
    trail_part = "tr_none" if trailing_stop is None else f"tr{int(round(trailing_stop * 100))}"
    return f"attack_tp10_{stop_part}_{trail_part}"


def run_sweep(
    output_dir: Path,
    signal_source: Path,
    start_date: str,
    end_date: str,
    stop_loss_grid: list[float | None],
    trailing_stop_grid: list[float | None],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for stop_loss in stop_loss_grid:
        for trailing_stop in trailing_stop_grid:
            name = _run_name(stop_loss, trailing_stop)
            summary = run(
                signal_source=signal_source,
                output_dir=output_dir / "runs" / name,
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
                stop_loss_pct=stop_loss,
                take_profit_pct=0.10,
                take_profit_sell_ratio=0.5,
                trailing_stop_pct=trailing_stop,
                sort_mode="trigger_time",
                max_position_weight=0.50,
            )
            rows.append(
                {
                    "profile": name,
                    "stop_loss_pct": stop_loss,
                    "trailing_stop_pct": trailing_stop,
                    "total_return": summary.get("total_return"),
                    "benchmark_return": summary.get("benchmark_return"),
                    "excess_return": summary.get("excess_return"),
                    "max_drawdown": summary.get("max_drawdown"),
                    "trade_count": summary.get("trade_count"),
                    "win_rate": summary.get("win_rate"),
                    "avg_trade_return": summary.get("avg_trade_return"),
                    "final_equity": summary.get("final_equity"),
                }
            )

    df = pd.DataFrame(rows)
    df["attack_score"] = (
        df["excess_return"].fillna(0.0)
        + df["total_return"].fillna(0.0) * 0.25
        + df["max_drawdown"].fillna(0.0) * 0.75
    )
    df = df.sort_values(["attack_score", "total_return"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    records = _clean_optional_floats(df).to_dict("records")

    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "base": {
            "max_positions": 2,
            "max_buys_per_day": 1,
            "max_position_weight": 0.50,
            "sort_mode": "trigger_time",
            "take_profit_pct": 0.10,
            "take_profit_sell_ratio": 0.5,
            "hold_days": 3,
        },
        "rows": records,
        "outputs": {"summary": "summary.csv", "findings": "findings.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, df)
    return payload


def _write_findings(output_dir: Path, df: pd.DataFrame) -> None:
    lines = [
        "# G2 Open V1 Attack Drawdown Sweep",
        "",
        "Base: max positions 2, max buys/day 1, max position weight 50%, trigger_time sort, TP10 half, 3-day time exit.",
        "",
        "| profile | stop | trail | total | excess | max_dd | win | avg_trade | trades |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['profile']} | {_pct(row.get('stop_loss_pct'))} | {_pct(row.get('trailing_stop_pct'))} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {int(row.get('trade_count') or 0)} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep drawdown controls for the aggressive G2 Open V1 base.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--stop-loss-grid", default="none,0.05,0.06,0.07,0.08,0.10")
    parser.add_argument("--trailing-stop-grid", default="none")
    args = parser.parse_args()
    payload = run_sweep(
        output_dir=Path(args.output_dir),
        signal_source=Path(args.signal_source),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        stop_loss_grid=_parse_float_grid(args.stop_loss_grid),
        trailing_stop_grid=_parse_float_grid(args.trailing_stop_grid),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
