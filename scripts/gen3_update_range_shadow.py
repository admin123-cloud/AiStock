from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
RUNTIME_DIR = ROOT / "data" / "runtime" / "gen3_range_shadow"
REPORT_DIR = ROOT / "reports" / "gen3_range_shadow"


KEEP_COLUMNS = [
    "entry_date",
    "trade_date",
    "code",
    "name",
    "g3_chain",
    "g3_range_route",
    "g3_shadow_status",
    "chain_rank",
    "candidate_score",
    "market_style",
    "range_pos60",
    "up_rate",
    "big_down_rate",
    "drawdown20",
    "close_position",
    "lower_shadow_ratio",
    "amount_ratio20",
    "entry_open",
    "fwd_ret_open_to_close_5d",
    "shadow_note",
]


def stress_reclaim_mask(df: pd.DataFrame) -> pd.Series:
    bottom = df["range_pos60"].le(0.20)
    stress = df["big_down_rate"].ge(0.10)
    reclaim = df["close_position"].ge(0.55) | df["lower_shadow_ratio"].ge(0.25)
    not_crash = df["big_down_rate"].lt(0.35)
    return bottom & stress & reclaim & not_crash


def build_shadow(entry_date: str | None = None) -> tuple[pd.DataFrame, dict]:
    df = pd.read_parquet(SOURCE).copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df = df[df["g3_chain"].eq("range_box_bottom")].copy()
    target_date = pd.to_datetime(entry_date).date() if entry_date else df["entry_date"].max()
    day = df[df["entry_date"].eq(target_date)].copy()

    day["g3_range_route"] = np.where(stress_reclaim_mask(day), "range_box_bottom_stress_reclaim", "range_box_bottom_watch_only")
    day["g3_shadow_status"] = np.where(day["g3_range_route"].eq("range_box_bottom_stress_reclaim"), "shadow_candidate_unstable_years", "watch_only")
    day["shadow_note"] = np.where(
        day["g3_shadow_status"].eq("shadow_candidate_unstable_years"),
        "横盘箱体底部+出清反抽影子候选；2024/2026仍不稳，暂不进正式组合",
        "原始range_box_bottom过宽，仅观察",
    )
    existing = [c for c in KEEP_COLUMNS if c in day.columns]
    day = day[existing].sort_values(["g3_shadow_status", "chain_rank", "candidate_score"], ascending=[True, True, False], na_position="last").reset_index(drop=True)

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(SOURCE.relative_to(ROOT)),
        "entry_date": str(target_date),
        "rows": int(len(day)),
        "shadow_candidates": int(day["g3_shadow_status"].eq("shadow_candidate_unstable_years").sum()) if not day.empty else 0,
        "status_counts": day["g3_shadow_status"].value_counts(dropna=False).to_dict() if not day.empty else {},
        "route_counts": day["g3_range_route"].value_counts(dropna=False).to_dict() if not day.empty else {},
        "note": "G3 range shadow only. No G2 runtime output is touched.",
    }
    return day, meta


def write_outputs(day: pd.DataFrame, meta: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    date_dir = REPORT_DIR / "live_updates" / meta["entry_date"]
    date_dir.mkdir(parents=True, exist_ok=True)

    day.to_csv(date_dir / "range_shadow_candidates.csv", index=False, encoding="utf-8-sig")
    day.to_parquet(date_dir / "range_shadow_candidates.parquet", index=False)
    day.to_csv(RUNTIME_DIR / "latest_range_shadow_candidates.csv", index=False, encoding="utf-8-sig")
    day.to_parquet(RUNTIME_DIR / "latest_range_shadow_candidates.parquet", index=False)
    (date_dir / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNTIME_DIR / "latest_summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    preview_cols = [c for c in ["code", "name", "g3_range_route", "g3_shadow_status", "chain_rank", "range_pos60", "up_rate", "big_down_rate", "close_position", "lower_shadow_ratio"] if c in day.columns]
    preview = day[preview_cols].head(40).to_markdown(index=False) if not day.empty else "_无候选_"
    report = [
        "# G3 Range Shadow 更新",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 候选日期：{meta['entry_date']}",
        f"- 候选总数：{meta['rows']}",
        f"- 影子候选：{meta['shadow_candidates']}",
        f"- 状态分布：`{meta['status_counts']}`",
        "- 说明：range 当前只做影子观察，不进入正式 G3 组合。",
        "",
        "## 前 40 条",
        "",
        preview,
        "",
        "## 下一步",
        "",
        "- 给 `range_box_bottom_stress_reclaim` 接 30m 确认，检查是否能过滤 2024/2026 的失败段。",
        "- 若 30m 后仍不稳，重写横盘市场定义，不再扩大该候选池。",
        "",
    ]
    (date_dir / "range_shadow_update_cn.md").write_text("\n".join(report), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry-date", default=None)
    args = parser.parse_args()
    day, meta = build_shadow(args.entry_date)
    write_outputs(day, meta)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
