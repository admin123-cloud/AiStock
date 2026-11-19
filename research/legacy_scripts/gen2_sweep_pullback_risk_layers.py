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

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402
from scripts.gen2_sweep_pullback_factor_filters import _load_labeled_signals  # noqa: E402

DEFAULT_LABELED_SIGNALS = REPO_ROOT / "reports" / "gen2_v4_factor_profile" / "pullback_signal_factor_labeled.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pullback_risk_layer_sweep"


def _quality_mask(d: pd.DataFrame) -> pd.Series:
    return (pd.to_numeric(d["intraday_strength_live"], errors="coerce") >= 0.03) & (
        pd.to_numeric(d["close_position_live"], errors="coerce") >= 0.55
    )


def _layer_rules() -> dict[str, tuple[str, Callable[[pd.DataFrame], pd.DataFrame]]]:
    def baseline(d: pd.DataFrame) -> pd.DataFrame:
        out = d.copy()
        out["quality_confirm"] = _quality_mask(out)
        out["capital_weight"] = 1.0
        return out

    def hard_quality(d: pd.DataFrame) -> pd.DataFrame:
        out = d[_quality_mask(d)].copy()
        out["quality_confirm"] = True
        out["capital_weight"] = 1.0
        return out

    def low_50(d: pd.DataFrame) -> pd.DataFrame:
        out = d.copy()
        quality = _quality_mask(out)
        out["quality_confirm"] = quality
        out["capital_weight"] = np.where(quality, 1.0, 0.5)
        return out

    def low_30(d: pd.DataFrame) -> pd.DataFrame:
        out = d.copy()
        quality = _quality_mask(out)
        out["quality_confirm"] = quality
        out["capital_weight"] = np.where(quality, 1.0, 0.3)
        return out

    def low_50_rank30(d: pd.DataFrame) -> pd.DataFrame:
        out = d.copy()
        quality = _quality_mask(out)
        rank_ok = pd.to_numeric(out["v4_rank"], errors="coerce") <= 30
        out = out[quality | rank_ok].copy()
        quality = _quality_mask(out)
        out["quality_confirm"] = quality
        out["capital_weight"] = np.where(quality, 1.0, 0.5)
        return out

    def low_50_rank50(d: pd.DataFrame) -> pd.DataFrame:
        out = d.copy()
        quality = _quality_mask(out)
        rank_ok = pd.to_numeric(out["v4_rank"], errors="coerce") <= 50
        out = out[quality | rank_ok].copy()
        quality = _quality_mask(out)
        out["quality_confirm"] = quality
        out["capital_weight"] = np.where(quality, 1.0, 0.5)
        return out

    return {
        "baseline": ("all signals, full size", baseline),
        "hard_quality": ("only quality_confirm signals, full size", hard_quality),
        "layer_low_50": ("quality_confirm full size, other signals half size", low_50),
        "layer_low_30": ("quality_confirm full size, other signals 30% size", low_30),
        "layer_low_50_rank30": ("quality_confirm full size; non-quality only if v4_rank <= 30 at half size", low_50_rank30),
        "layer_low_50_rank50": ("quality_confirm full size; non-quality only if v4_rank <= 50 at half size", low_50_rank50),
    }


def _weight_stats(signals: pd.DataFrame) -> dict[str, Any]:
    if signals.empty:
        return {"signal_count": 0}
    quality = signals["quality_confirm"].fillna(False).astype(bool) if "quality_confirm" in signals.columns else pd.Series(False, index=signals.index)
    weights = pd.to_numeric(signals.get("capital_weight", 1.0), errors="coerce")
    return {
        "signal_count": int(len(signals)),
        "quality_count": int(quality.sum()),
        "non_quality_count": int((~quality).sum()),
        "avg_capital_weight": float(weights.mean()) if not weights.dropna().empty else None,
    }


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Pullback Risk Layer Sweep",
        "",
        "Quality uses only 30m bars visible at confirm_datetime: intraday_strength_live >= 3% and close_position_live >= 0.55.",
        "Only signal size or low-quality eligibility changes. Entry, ordering and realistic previous-low exit fill remain unchanged.",
        "",
        "| rule | signals | quality | trades | total | excess | max_dd | win | avg_trade | note |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {int(row.get('signal_count') or 0)} | {int(row.get('quality_count') or 0)} | "
            f"{int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | "
            f"{_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | "
            f"{row.get('rule_note', '')} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(labeled_signals: Path, output_dir: Path, start_date: str, end_date: str, rule_keys: set[str] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_labeled_signals(labeled_signals, start_date, end_date)
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    selected = _layer_rules()
    if rule_keys:
        missing = sorted(rule_keys - set(selected))
        if missing:
            raise ValueError(f"Unknown rules: {missing}")
        selected = {key: selected[key] for key in selected if key in rule_keys}

    rows: list[dict[str, Any]] = []
    for key, (note, transform) in selected.items():
        layered = transform(signals).copy()
        rule_dir = output_dir / key
        rule_dir.mkdir(parents=True, exist_ok=True)
        signal_path = rule_dir / "signals.parquet"
        layered.to_parquet(signal_path, index=False)
        layered.to_csv(rule_dir / "signals.csv", index=False, encoding="utf-8-sig")
        stats = _weight_stats(layered)
        (rule_dir / "weight_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        summary = _run_profile(
            signal_source=signal_path,
            output_dir=rule_dir / "backtest",
            profile=profile,
            start_date=start_date,
            end_date=end_date,
        )
        summary["key"] = key
        summary["rule_note"] = note
        summary.update(stats)
        rows.append(summary)

    summary_rows: list[dict[str, Any]] = []
    for row in rows:
        summary_rows.append(
            {
                "key": row["key"],
                "rule_note": row["rule_note"],
                "signal_count": row.get("signal_count"),
                "quality_count": row.get("quality_count"),
                "non_quality_count": row.get("non_quality_count"),
                "avg_capital_weight": row.get("avg_capital_weight"),
                "trade_count": row.get("trade_count"),
                "final_equity": row.get("final_equity"),
                "total_return": row.get("total_return"),
                "benchmark_return": row.get("benchmark_return"),
                "excess_return": row.get("excess_return"),
                "max_drawdown": row.get("max_drawdown"),
                "win_rate": row.get("win_rate"),
                "avg_trade_return": row.get("avg_trade_return"),
                "exit_reason_counts": json.dumps(row.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
            }
        )
    summary_df = pd.DataFrame(summary_rows).sort_values("total_return", ascending=False)
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "labeled_signals": str(labeled_signals),
        "profile": profile.__dict__,
        "rows": summary_df.replace({np.nan: None}).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "findings.md"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary_df.to_dict("records"))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep position-size risk layers for G2 pullback confirmation signals.")
    parser.add_argument("--labeled-signals", default=str(DEFAULT_LABELED_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--rules", default="", help="Comma-separated rule keys. Empty means all rules.")
    args = parser.parse_args()
    rule_keys = {item.strip() for item in str(args.rules).split(",") if item.strip()} if str(args.rules).strip() else None
    payload = run(Path(args.labeled_signals), Path(args.output_dir), str(args.start_date), str(args.end_date), rule_keys=rule_keys)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
