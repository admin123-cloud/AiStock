from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


OUT = (
    ROOT
    / "reports"
    / "gen2_breakout_buy_point_research"
    / "breakout_family_intraday_strength_probe"
    / "combo_policy_probe"
    / "sector_context_probe"
    / "sector_filter_matrix"
)
ENRICHED = OUT / "sector_enriched_combo_or.parquet"
WINDOWS = {
    "full": ("2024-07-09", "2026-05-26"),
    "train": ("2024-07-09", "2025-03-31"),
    "valid": ("2025-04-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-05-26"),
}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return str(obj)


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _write_source(name: str, df: pd.DataFrame) -> Path:
    source_dir = OUT / "robustness_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{name}.parquet"
    drop_cols = ["confirm_time", "code6"]
    out = df.drop(columns=[c for c in drop_cols if c in df.columns]).copy()
    out.to_parquet(path, index=False)
    return path


def _source_counts(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "signals": int(len(df)),
        "volume5_signals": int((df["source_family"] == "volume5").sum()) if "source_family" in df.columns else None,
        "bigbull_signals": int((df["source_family"] == "big_bull").sum()) if "source_family" in df.columns else None,
    }


def main() -> None:
    df = pd.read_parquet(ENRICHED).copy()
    l3_s3 = pd.to_numeric(df["l3_rt_strong3_ratio"], errors="coerce").fillna(-1.0)
    thresholds = [0.00, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12]
    rows: list[dict[str, Any]] = []
    for th in thresholds:
        name = f"l3_s3_ge_{int(round(th * 100)):02d}"
        source_df = df[l3_s3 >= th].copy()
        source_path = _write_source(name, source_df)
        counts = _source_counts(source_df)
        for window, (start, end) in WINDOWS.items():
            run_dir = OUT / "robustness_runs" / name / window
            summary = _run_dynamic(source_path, run_dir, "stop_cd3_skip", start, end, sort_mode="trigger_time")
            rows.append(
                {
                    "threshold": th,
                    "variant": name,
                    "window": window,
                    **counts,
                    "trades": summary["trade_count"],
                    "total_return": summary["total_return"],
                    "excess_return": summary["excess_return"],
                    "max_drawdown": summary["max_drawdown"],
                    "win_rate": summary["win_rate"],
                    "avg_trade_return": summary["avg_trade_return"],
                }
            )
    result = pd.DataFrame(rows)
    result.to_csv(OUT / "sector_l3_s3_threshold_robustness.csv", index=False, encoding="utf-8-sig")
    (OUT / "sector_l3_s3_threshold_robustness.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    lines = [
        "# L3 Strong3 Threshold Robustness",
        "",
        "| threshold | window | signals | trades | total | excess | max_dd | win | avg_trade |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['threshold']:.2f} | {row['window']} | {row['signals']} | {row['trades']} | "
            f"{_pct(row['total_return'])} | {_pct(row['excess_return'])} | {_pct(row['max_drawdown'])} | "
            f"{_pct(row['win_rate'])} | {_pct(row['avg_trade_return'])} |"
        )
    (OUT / "sector_l3_s3_threshold_robustness.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(OUT), "rows": len(rows)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
