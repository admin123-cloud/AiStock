from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402


BASE = ROOT / "reports" / "gen2_breakout_buy_point_research" / "breakout_family_intraday_strength_probe"
COMBO = BASE / "combo_policy_probe"
OUT = COMBO / "quality_veto_1000_probe"
SOURCE = COMBO / "sources" / "rtret60_or_breakbox25.parquet"
WINDOWS = {
    "full": ("2024-07-09", "2026-05-26"),
    "train": ("2024-07-09", "2025-03-31"),
    "valid": ("2025-04-01", "2025-12-31"),
    "blind": ("2026-01-01", "2026-05-26"),
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


def _load_source() -> pd.DataFrame:
    d = pd.read_parquet(SOURCE).copy()
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["confirm_time"] = d["confirm_datetime"].dt.strftime("%H:%M")
    d["source_family"] = d.get("source_family", "").fillna("")
    d["v4_rank"] = pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999).astype(int)
    d["v4_score"] = pd.to_numeric(d["v4_score"], errors="coerce").fillna(0.0)
    d["confirm_amount"] = pd.to_numeric(d["confirm_amount"], errors="coerce")
    d["confirm_amount_ma5_prev"] = pd.to_numeric(d["confirm_amount_ma5_prev"], errors="coerce")
    d["confirm_volume_ratio"] = np.where(
        d["confirm_amount_ma5_prev"] > 0,
        d["confirm_amount"] / d["confirm_amount_ma5_prev"],
        np.nan,
    )
    return d


def _target_mask(d: pd.DataFrame) -> pd.Series:
    return (d["confirm_time"] == "10:00") & (d["source_family"] == "volume5")


def _variant_masks(d: pd.DataFrame) -> dict[str, pd.Series]:
    target = _target_mask(d)
    masks: dict[str, pd.Series] = {"base_combo_or": pd.Series(False, index=d.index)}
    for rank in [1, 2, 3]:
        masks[f"skip_1000_v5_rank_gt{rank}"] = target & (d["v4_rank"] > rank)
    for score in [0.45, 0.50, 0.55, 0.60, 0.65]:
        bp = int(score * 100)
        masks[f"skip_1000_v5_score_lt{bp}"] = target & (d["v4_score"] < score)
    for vol in [2.0, 3.0, 4.0, 5.0, 6.0]:
        bp = int(vol * 10)
        masks[f"skip_1000_v5_vol_gt{bp}x"] = target & (d["confirm_volume_ratio"] > vol)
    for rank in [1, 2]:
        for score in [0.50, 0.55, 0.60]:
            bp = int(score * 100)
            masks[f"skip_1000_v5_rank_gt{rank}_and_score_lt{bp}"] = target & (d["v4_rank"] > rank) & (d["v4_score"] < score)
    for rank in [1, 2]:
        for vol in [2.0, 3.0, 4.0]:
            bp = int(vol * 10)
            masks[f"skip_1000_v5_rank_gt{rank}_and_vol_gt{bp}x"] = target & (d["v4_rank"] > rank) & (d["confirm_volume_ratio"] > vol)
    for score in [0.50, 0.60]:
        for vol in [3.0, 4.0, 5.0]:
            sbp = int(score * 100)
            vbp = int(vol * 10)
            masks[f"skip_1000_v5_score_lt{sbp}_and_vol_gt{vbp}x"] = (
                target & (d["v4_score"] < score) & (d["confirm_volume_ratio"] > vol)
            )
    masks["skip_1000_v5_rank_gt1_or_score_lt50"] = target & ((d["v4_rank"] > 1) | (d["v4_score"] < 0.50))
    masks["skip_1000_v5_rank_gt1_or_vol_gt50x"] = target & ((d["v4_rank"] > 1) | (d["confirm_volume_ratio"] > 5.0))
    return masks


def _write_source(name: str, d: pd.DataFrame, skip_mask: pd.Series) -> Path:
    source_dir = OUT / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    keep = d[~skip_mask].drop(columns=["confirm_time", "confirm_volume_ratio"]).copy()
    path = source_dir / f"{name}.parquet"
    keep.to_parquet(path, index=False)
    keep.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return path


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    d = _load_source()
    masks = _variant_masks(d)
    rows: list[dict[str, Any]] = []
    for name, skip_mask in masks.items():
        source_path = _write_source(name, d, skip_mask)
        skipped = d[skip_mask].copy()
        for window, (start, end) in WINDOWS.items():
            run_dir = OUT / "runs" / name / window
            summary = _run_dynamic(source_path, run_dir, "stop_cd3_skip", start, end, sort_mode="trigger_time")
            rows.append(
                {
                    "variant": name,
                    "window": window,
                    "skipped_signals": int(len(skipped)),
                    "skipped_2026ytd": int(((skipped["entry_date"] >= "2026-01-01") & (skipped["entry_date"] <= "2026-05-26")).sum())
                    if "entry_date" in skipped.columns
                    else 0,
                    "signals": summary["signal_count"],
                    "trades": summary["trade_count"],
                    "total_return": summary["total_return"],
                    "excess_return": summary["excess_return"],
                    "max_drawdown": summary["max_drawdown"],
                    "win_rate": summary["win_rate"],
                    "avg_trade_return": summary["avg_trade_return"],
                }
            )
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "quality_veto_1000_summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "quality_veto_1000_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    pivot = df.pivot_table(index="variant", columns="window", values="total_return", aggfunc="first")
    keep_cols = [c for c in ["full", "train", "valid", "blind"] if c in pivot.columns]
    pivot = pivot[keep_cols].sort_values(["blind", "full"], ascending=False)
    pivot.to_csv(OUT / "quality_veto_1000_total_pivot.csv", encoding="utf-8-sig")

    lines = [
        "# 10:00 volume5 quality veto matrix",
        "",
        "All rules only filter 10:00 volume5 candidates. Big-bull and later confirmations are untouched.",
        "",
        "| variant | skipped | skipped_2026 | full | train | valid | blind |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    records = []
    for variant, g in df.groupby("variant"):
        item = {"variant": variant}
        first = g.iloc[0]
        item["skipped"] = int(first["skipped_signals"])
        item["skipped_2026"] = int(first["skipped_2026ytd"])
        for _, row in g.iterrows():
            item[str(row["window"])] = row["total_return"]
        records.append(item)
    for row in sorted(records, key=lambda x: (x.get("blind", -999), x.get("full", -999)), reverse=True):
        lines.append(
            f"| {row['variant']} | {row['skipped']} | {row['skipped_2026']} | {_pct(row.get('full'))} | "
            f"{_pct(row.get('train'))} | {_pct(row.get('valid'))} | {_pct(row.get('blind'))} |"
        )
    (OUT / "quality_veto_1000_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "variants": len(masks), "output": str(OUT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
