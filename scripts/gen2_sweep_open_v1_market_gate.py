from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1]))

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.gen2_strategy import GEN2_OPEN_RULE_V1  # noqa: E402
from scripts.gen2_backtest_open_v1_intraday_risk import run  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import DEFAULT_SIGNAL_SOURCE, _load_daily_prices  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_open_v1_market_gate_sweep"


from research.common.reporting import numpy_json_default as _json_default


from research.common.reporting import percent_text as _pct


def _load_index_frame(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    d = _load_daily_prices([code], "2010-01-01", end_date)
    if d.empty:
        raise RuntimeError(f"No index daily data for {code}")
    d = d[d["trade_date"] <= end_date].copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.strftime("%Y-%m-%d")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    d["ma20"] = d["close"].rolling(20, min_periods=20).mean()
    d["ma60"] = d["close"].rolling(60, min_periods=60).mean()
    d["ma20_slope5"] = d["ma20"] / d["ma20"].shift(5) - 1.0
    d = d[d["trade_date"] >= start_date].copy()
    return d.reset_index(drop=True)


def _build_gate_dates(start_date: str, end_date: str) -> dict[str, set[str] | None]:
    csi = _load_index_frame("000852.SH", start_date, end_date)
    sse = _load_index_frame("999999.SH", start_date, end_date)
    csi = csi[["trade_date", "close", "ma20", "ma60", "ma20_slope5"]].rename(
        columns={"close": "csi_close", "ma20": "csi_ma20", "ma60": "csi_ma60", "ma20_slope5": "csi_ma20_slope5"}
    )
    sse = sse[["trade_date", "close", "ma20", "ma60", "ma20_slope5"]].rename(
        columns={"close": "sse_close", "ma20": "sse_ma20", "ma60": "sse_ma60", "ma20_slope5": "sse_ma20_slope5"}
    )
    d = csi.merge(sse, on="trade_date", how="inner").sort_values("trade_date").reset_index(drop=True)

    raw_conditions = {
        "none": pd.Series(True, index=d.index),
        "csi1000_prev_close_ge_ma20": d["csi_close"] >= d["csi_ma20"],
        "csi1000_prev_close_ge_ma60": d["csi_close"] >= d["csi_ma60"],
        "csi1000_prev_ma20_slope_pos": d["csi_ma20_slope5"] > 0,
        "sse_prev_close_ge_ma20": d["sse_close"] >= d["sse_ma20"],
        "sse_and_csi1000_prev_close_ge_ma20": (d["sse_close"] >= d["sse_ma20"]) & (d["csi_close"] >= d["csi_ma20"]),
    }

    result: dict[str, set[str] | None] = {"none": None}
    for name, cond in raw_conditions.items():
        if name == "none":
            continue
        # Entry at date D can only use the market condition known after D-1 close.
        allowed = cond.shift(1).fillna(False)
        result[name] = set(d.loc[allowed, "trade_date"].astype(str).tolist())
    return result


def _filtered_signal_source(
    source: Path,
    output_path: Path,
    allowed_dates: set[str] | None,
    start_date: str,
    end_date: str,
) -> tuple[Path, int]:
    if allowed_dates is None:
        return source, -1
    df = pd.read_parquet(source)
    d = df.copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    mask = d["entry_date"].between(start_date, end_date) & d["entry_date"].isin(allowed_dates)
    filtered = d[mask].copy()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filtered.to_parquet(output_path, index=False)
    rule = GEN2_OPEN_RULE_V1
    matching = filtered[
        (filtered["pattern"] == rule["pattern"])
        & (filtered["g2_open_state"] == rule["g2_open_state"])
        & (filtered["trigger_type"] == rule["trigger_type"])
    ]
    return output_path, int(len(matching))


def run_sweep(output_dir: Path, signal_source: Path, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    gate_dates = _build_gate_dates(start_date, end_date)
    rows: list[dict[str, Any]] = []
    for gate_name, allowed_dates in gate_dates.items():
        source, gated_signal_count = _filtered_signal_source(
            source=signal_source,
            output_path=output_dir / "signals" / f"{gate_name}.parquet",
            allowed_dates=allowed_dates,
            start_date=start_date,
            end_date=end_date,
        )
        summary = run(
            signal_source=source,
            output_dir=output_dir / "runs" / gate_name,
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
        rows.append(
            {
                "gate": gate_name,
                "allowed_trade_dates": None if allowed_dates is None else len(allowed_dates),
                "gated_signal_count": None if gated_signal_count < 0 else gated_signal_count,
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
        )

    df = pd.DataFrame(rows)
    df["gate_score"] = (
        df["excess_return"].fillna(0.0)
        + df["total_return"].fillna(0.0) * 0.25
        + df["max_drawdown"].fillna(0.0) * 0.75
    )
    df = df.sort_values(["gate_score", "total_return"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "findings": "findings.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, df)
    return payload


def _write_findings(output_dir: Path, df: pd.DataFrame) -> None:
    lines = [
        "# G2 Open V1 Market Gate Sweep",
        "",
        "Only the open gate changes. Entry pattern, NORMAL state, position sizing, SL/TP and holding days are unchanged.",
        "All gates use previous trading day's index condition to avoid lookahead.",
        "",
        "| gate | allowed_days | gated_signals | trades | total | excess | max_dd | win | avg_trade |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['gate']} | {'' if pd.isna(row.get('allowed_trade_dates')) else int(row.get('allowed_trade_dates') or 0)} | {'' if pd.isna(row.get('gated_signal_count')) else int(row.get('gated_signal_count') or 0)} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep market gates for frozen G2 Open V1 SL5.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    args = parser.parse_args()
    payload = run_sweep(
        output_dir=Path(args.output_dir),
        signal_source=Path(args.signal_source),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
