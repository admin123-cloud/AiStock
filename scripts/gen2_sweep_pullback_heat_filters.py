from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.gen2_strategy import GEN2_OPEN_RULE_V1  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import SORT_MODES, _json_default, _pct  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402

DEFAULT_LABELED_SIGNALS = REPO_ROOT / "reports" / "gen2_v4_factor_profile" / "pullback_signal_factor_labeled.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pullback_heat_filter_sweep"


def _load_signals(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    rule = GEN2_OPEN_RULE_V1
    d = df[
        (df["pattern"] == rule["pattern"])
        & (df["g2_open_state"] == rule["g2_open_state"])
        & (df["trigger_type"] == rule["trigger_type"])
    ].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    for col in ["ma60_gap", "mom20", "ma20_gap", "volume_ratio", "v4_rank", "v4_score", "entry_price"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "entry_price", "confirm_datetime"]).reset_index(drop=True)


def _rules() -> dict[str, tuple[str, Callable[[pd.DataFrame], pd.Series]]]:
    return {
        "baseline": ("no extra heat filter", lambda d: pd.Series(True, index=d.index)),
        "not_hot_25_25": ("ma60_gap <= 25% and mom20 <= 25%", lambda d: (d["ma60_gap"] <= 0.25) & (d["mom20"] <= 0.25)),
        "not_hot_20_20": ("ma60_gap <= 20% and mom20 <= 20%", lambda d: (d["ma60_gap"] <= 0.20) & (d["mom20"] <= 0.20)),
        "not_hot_17_18": ("ma60_gap <= 17% and mom20 <= 18%", lambda d: (d["ma60_gap"] <= 0.17) & (d["mom20"] <= 0.18)),
        "not_extreme_35_40": ("ma60_gap <= 35% and mom20 <= 40%", lambda d: (d["ma60_gap"] <= 0.35) & (d["mom20"] <= 0.40)),
    }


def _factor_snapshot(signals: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in ["ma60_gap", "mom20", "ma20_gap", "volume_ratio", "v4_rank"]:
        if col not in signals.columns:
            continue
        x = pd.to_numeric(signals[col], errors="coerce").dropna()
        if x.empty:
            continue
        out[col] = {
            "mean": float(x.mean()),
            "median": float(x.median()),
            "p25": float(x.quantile(0.25)),
            "p75": float(x.quantile(0.75)),
        }
    return out


def _write_findings(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Pullback Heat Filter Sweep",
        "",
        "Only pre-confirm daily heat filters change. Entry timing, same-day sorting, realistic exits and position constraints remain unchanged.",
        "",
        "| sort | rule | signals | trades | total | benchmark | excess | max_dd | win | avg_trade | note |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['sort_mode']} | {row['rule_key']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('benchmark_return'))} | {_pct(row.get('excess_return'))} | "
            f"{_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | "
            f"{row.get('rule_note', '')} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(
    labeled_signals: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    sort_modes: list[str],
    rule_keys: set[str] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(labeled_signals, start_date, end_date)
    rules = _rules()
    if rule_keys:
        missing = sorted(rule_keys - set(rules))
        if missing:
            raise ValueError(f"Unknown rules: {missing}")
        rules = {key: rules[key] for key in rules if key in rule_keys}
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")

    rows: list[dict[str, Any]] = []
    for sort_mode in sort_modes:
        if sort_mode not in SORT_MODES:
            raise ValueError(f"Unsupported sort mode: {sort_mode}")
        for key, (note, mask_fn) in rules.items():
            filtered = signals[mask_fn(signals).fillna(False)].copy()
            rule_dir = output_dir / "runs" / sort_mode / key
            rule_dir.mkdir(parents=True, exist_ok=True)
            signal_path = rule_dir / "signals.parquet"
            filtered.to_parquet(signal_path, index=False)
            filtered.to_csv(rule_dir / "signals.csv", index=False, encoding="utf-8-sig")
            (rule_dir / "factor_snapshot.json").write_text(
                json.dumps(_factor_snapshot(filtered), ensure_ascii=False, indent=2, default=_json_default),
                encoding="utf-8",
            )
            summary = _run_profile(
                signal_source=signal_path,
                output_dir=rule_dir / "backtest",
                profile=profile,
                start_date=start_date,
                end_date=end_date,
                sort_mode=sort_mode,
            )
            rows.append(
                {
                    "sort_mode": sort_mode,
                    "rule_key": key,
                    "rule_note": note,
                    "filtered_signal_rows": int(len(filtered)),
                    "signal_count": summary.get("signal_count"),
                    "trade_count": summary.get("trade_count"),
                    "total_return": summary.get("total_return"),
                    "benchmark_return": summary.get("benchmark_return"),
                    "excess_return": summary.get("excess_return"),
                    "max_drawdown": summary.get("max_drawdown"),
                    "win_rate": summary.get("win_rate"),
                    "avg_trade_return": summary.get("avg_trade_return"),
                    "final_equity": summary.get("final_equity"),
                    "exit_reason_counts": json.dumps(summary.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
                }
            )

    df = pd.DataFrame(rows)
    df["score"] = df["total_return"].fillna(0.0) + df["max_drawdown"].fillna(0.0) * 0.50
    df = df.sort_values(["sort_mode", "score", "total_return"], ascending=[True, False, False]).reset_index(drop=True)
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    ordered_rows = df.where(pd.notna(df), None).to_dict("records")
    _write_findings(output_dir, ordered_rows)
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "labeled_signals": str(labeled_signals),
        "rows": ordered_rows,
        "outputs": {"summary": "summary.csv", "findings": "findings.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep G2 pullback pre-confirm heat filters.")
    parser.add_argument("--labeled-signals", default=str(DEFAULT_LABELED_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--sort-modes", default="trigger_time,volume_ratio")
    parser.add_argument("--rules", default="", help="Comma-separated rule keys. Empty means all rules.")
    args = parser.parse_args()
    sort_modes = [x.strip() for x in str(args.sort_modes).split(",") if x.strip()]
    rule_keys = {x.strip() for x in str(args.rules).split(",") if x.strip()} if str(args.rules).strip() else None
    payload = run(
        labeled_signals=Path(args.labeled_signals),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        sort_modes=sort_modes,
        rule_keys=rule_keys,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
