from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "sources" / "g2_v2_complete.parquet"
RUNTIME_DIR = ROOT / "data" / "runtime" / "gen3_strong_shadow"
REPORT_DIR = ROOT / "reports" / "gen3_strong_shadow"


KEEP_COLUMNS = [
    "entry_date",
    "trade_date",
    "code",
    "name",
    "source_family",
    "signal_family",
    "g2_v2_buy_logic",
    "g3_strong_route",
    "g3_shadow_status",
    "g3_market_style",
    "v4_rank",
    "v4_score",
    "entry_price",
    "confirm_datetime",
    "rt_return_from_d1_close",
    "l3_sector_name",
    "l3_rt_strong3_ratio",
    "l3_rt_rise_ratio",
    "sector_score_bonus",
    "runup_from_60d_low",
    "index_close",
    "index_ma20",
    "index_ma60",
    "index_mom20",
    "market_breadth",
    "eval_fwd_ret_5d",
    "shadow_note",
]


def classify_market_style(row: pd.Series) -> str:
    close = row.get("index_close")
    ma20 = row.get("index_ma20")
    ma60 = row.get("index_ma60")
    mom20 = row.get("index_mom20")
    breadth = row.get("market_breadth")

    if pd.notna(close) and pd.notna(ma20) and pd.notna(ma60) and close >= ma20 >= ma60:
        return "main_up"
    if (
        pd.notna(close)
        and pd.notna(ma60)
        and pd.notna(ma20)
        and pd.notna(mom20)
        and pd.notna(breadth)
        and ma20 < ma60
        and close >= ma60
        and mom20 > 0
        and breadth >= 0.5
    ):
        return "weak_recovery"
    if pd.notna(close) and pd.notna(ma20) and close < ma20:
        return "defense_or_failed"
    return "neutral"


def assign_route(row: pd.Series) -> str:
    if row.get("source_family") == "volume5":
        return "strong_volume5_sector_bonus"
    if row.get("source_family") == "big_bull":
        return "strong_big_bull_rebreak_unvalidated"
    return "strong_other_shadow"


def assign_status(row: pd.Series) -> str:
    if row.get("source_family") == "volume5" and row.get("g3_market_style") in {"main_up", "weak_recovery"}:
        return "shadow_candidate"
    if row.get("source_family") == "volume5":
        return "watch_only_market_style_weak"
    if row.get("source_family") == "big_bull":
        return "watch_only_unvalidated"
    return "watch_only"


def build_shadow(entry_date: str | None = None) -> tuple[pd.DataFrame, dict]:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    df = pd.read_parquet(SOURCE).copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

    target_date = pd.to_datetime(entry_date).date() if entry_date else df["entry_date"].max()
    day = df[df["entry_date"].eq(target_date)].copy()

    if "outcome_fwd_ret_5d" in day.columns and "fwd_ret_5d" in day.columns:
        day["eval_fwd_ret_5d"] = day["outcome_fwd_ret_5d"].where(day["outcome_fwd_ret_5d"].notna(), day["fwd_ret_5d"])
    elif "outcome_fwd_ret_5d" in day.columns:
        day["eval_fwd_ret_5d"] = day["outcome_fwd_ret_5d"]
    elif "fwd_ret_5d" in day.columns:
        day["eval_fwd_ret_5d"] = day["fwd_ret_5d"]
    else:
        day["eval_fwd_ret_5d"] = np.nan

    day["g3_market_style"] = day.apply(classify_market_style, axis=1)
    day["g3_strong_route"] = day.apply(assign_route, axis=1)
    day["g3_shadow_status"] = day.apply(assign_status, axis=1)
    day["shadow_note"] = np.select(
        [
            day["g3_shadow_status"].eq("shadow_candidate"),
            day["g3_shadow_status"].eq("watch_only_market_style_weak"),
            day["g3_shadow_status"].eq("watch_only_unvalidated"),
        ],
        [
            "volume5强势链路候选，仅用于G3影子观察",
            "volume5触发但市场风格不顺风，仅观察",
            "big_bull未完成同口径长周期验证，仅观察",
        ],
        default="仅观察",
    )

    existing = [c for c in KEEP_COLUMNS if c in day.columns]
    day = day[existing].sort_values(["g3_shadow_status", "v4_rank", "code"], na_position="last").reset_index(drop=True)

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(SOURCE.relative_to(ROOT)),
        "entry_date": str(target_date),
        "rows": int(len(day)),
        "status_counts": day["g3_shadow_status"].value_counts(dropna=False).to_dict() if not day.empty else {},
        "route_counts": day["g3_strong_route"].value_counts(dropna=False).to_dict() if not day.empty else {},
        "market_style_counts": day["g3_market_style"].value_counts(dropna=False).to_dict() if not day.empty else {},
        "note": "G3 strong shadow only. No G2 runtime output is touched.",
    }
    return day, meta


def write_report(day: pd.DataFrame, meta: dict) -> None:
    date_dir = REPORT_DIR / "live_updates" / meta["entry_date"]
    date_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    day.to_csv(date_dir / "strong_shadow_candidates.csv", index=False, encoding="utf-8-sig")
    day.to_parquet(date_dir / "strong_shadow_candidates.parquet", index=False)
    day.to_csv(RUNTIME_DIR / "latest_strong_shadow_candidates.csv", index=False, encoding="utf-8-sig")
    day.to_parquet(RUNTIME_DIR / "latest_strong_shadow_candidates.parquet", index=False)
    (date_dir / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNTIME_DIR / "latest_summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    preview_cols = [c for c in ["code", "name", "source_family", "g3_market_style", "g3_shadow_status", "v4_rank", "l3_sector_name", "l3_rt_strong3_ratio"] if c in day.columns]
    preview = day[preview_cols].head(30).to_markdown(index=False) if not day.empty else "_无候选_"
    lines = [
        "# G3 强势链路 Shadow 更新",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 候选日期：{meta['entry_date']}",
        f"- 源文件：`{meta['source']}`",
        f"- 候选总数：{meta['rows']}",
        f"- 状态分布：`{meta['status_counts']}`",
        f"- 链路分布：`{meta['route_counts']}`",
        f"- 市场风格分布：`{meta['market_style_counts']}`",
        "- 说明：只写入 G3 shadow 目录，不写入 G2 runtime，不触发 G2 重建。",
        "",
        "## 前 30 条",
        "",
        preview,
        "",
        "## 下一步",
        "",
        "- 对 `shadow_candidate` 做逐日 MTM 和 slot 复算，确认它与 panic 链路组合后的资金曲线。",
        "- 对 `big_bull` 单独补齐可比较前向收益和撮合口径，暂不纳入正式候选组合。",
    ]
    (date_dir / "strong_shadow_update_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry-date", default=None, help="Entry date to replay, default latest in source.")
    args = parser.parse_args()
    day, meta = build_shadow(args.entry_date)
    write_report(day, meta)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
