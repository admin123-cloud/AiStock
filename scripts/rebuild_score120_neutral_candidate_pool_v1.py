from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402


TEMPLATE_ROOT = report_path("wave_style_template_strategy_backtest_v1")
FORMAL_SOURCE = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OLD_ANCHOR_SOURCE = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_pre_2024_10_state_alpha_closure_v5\institutional_mainwave_candidates.csv"
)
OUT_DIR = report_path("score120_neutral_candidate_pool_v1")
SHAPE_VARIANTS = ("shape_only_h5", "shape_only_h10", "shape_only_h20")


def _code6(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.extract(r"(\d{6})", expand=False).fillna("")


def _key(df: pd.DataFrame, date_col: str, code_col: str) -> pd.Series:
    date = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    return date + "|" + _code6(df[code_col])


def _read_shape_variant(variant: str) -> pd.DataFrame:
    path = TEMPLATE_ROOT / variant / "candidates.csv"
    columns = [
        "trade_date", "entry_date", "code_raw", "stock_name", "l2_sector_code", "l2_sector_name",
        "template_label", "wave_style_score", "rank_key", "amount_rank", "mom20", "mom60", "ret5",
        "ret20", "limit_up_days20", "big_up_days20", "max_dd20", "index_mom20", "index_mom60",
        "net_ret", "policy_exit_date", "hold_days",
    ]
    df = pd.read_csv(path, usecols=lambda c: c in columns, encoding="utf-8-sig", low_memory=False)
    df["variant"] = variant
    df["candidate_key"] = _key(df, "entry_date", "code_raw")
    return df


def _first_nonempty(series: pd.Series) -> str:
    for value in series:
        if pd.notna(value) and str(value).strip() and str(value).lower() != "nan":
            return str(value)
    return ""


def _numeric_max(series: pd.Series) -> float | None:
    value = pd.to_numeric(series, errors="coerce").max()
    return None if pd.isna(value) else float(value)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shapes = pd.concat([_read_shape_variant(v) for v in SHAPE_VARIANTS], ignore_index=True)
    neutral = (
        shapes.groupby("candidate_key", as_index=False)
        .agg(
            entry_date=("entry_date", _first_nonempty),
            trade_date=("trade_date", _first_nonempty),
            code=("code_raw", _first_nonempty),
            stock_name=("stock_name", _first_nonempty),
            l2_sector_code=("l2_sector_code", _first_nonempty),
            l2_sector_name=("l2_sector_name", _first_nonempty),
            template_label=("template_label", _first_nonempty),
            neutral_variants=("variant", lambda s: "|".join(sorted(set(s.astype(str))))),
            neutral_variant_count=("variant", "nunique"),
            wave_style_score=("wave_style_score", _numeric_max),
            rank_key=("rank_key", _numeric_max),
            amount_rank=("amount_rank", _numeric_max),
            mom20=("mom20", _numeric_max), mom60=("mom60", _numeric_max),
            ret5=("ret5", _numeric_max), ret20=("ret20", _numeric_max),
            limit_up_days20=("limit_up_days20", _numeric_max),
            big_up_days20=("big_up_days20", _numeric_max), max_dd20=("max_dd20", _numeric_max),
            index_mom20=("index_mom20", _numeric_max), index_mom60=("index_mom60", _numeric_max),
        )
    )
    neutral["candidate_source"] = "neutral_shape_template_union_h5_h10_h20"
    neutral["historical_tdx_mapping_available"] = False
    neutral["historical_tdx_mapping_note"] = "warehouse only retains 2026-07-06 TDX snapshot; do not use it as historical fact"

    formal = pd.read_csv(FORMAL_SOURCE, encoding="utf-8-sig", low_memory=False)
    formal["candidate_key"] = _key(formal, "entry_date", "code")
    formal_keys = set(formal["candidate_key"])
    neutral["formal26_status"] = neutral["candidate_key"].map(
        lambda x: "formal_anchor_represented_in_neutral_pool" if x in formal_keys else "not_formal_anchor"
    )

    old = pd.read_csv(OLD_ANCHOR_SOURCE, encoding="utf-8-sig", low_memory=False)
    old["candidate_key"] = _key(old, "entry_date", "code")
    old = old.drop_duplicates("candidate_key").copy()
    old["old_selected_anchor"] = True
    anchor_cols = [c for c in [
        "candidate_key", "old_selected_anchor", "variant", "scheduler", "selected_variant", "selected_score",
        "selected_total", "selected_mean", "selected_win_rate", "sector_diffusion_score",
        "m30_close_above_ma20", "sig_index_mom60", "net_ret", "policy_exit_date",
    ] if c in old.columns]
    anchors = old[anchor_cols].rename(columns={"variant": "old_variant", "net_ret": "old_proxy_net_ret"})
    neutral = neutral.merge(anchors, on="candidate_key", how="left")
    neutral["old_selected_anchor"] = neutral["old_selected_anchor"].fillna(False)

    formal_anchor = formal[["candidate_key", "entry_date", "code", "name", "net_ret", "wave_style_score", "sector_diffusion_score"]].copy()
    formal_anchor = formal_anchor.rename(columns={"name": "formal_name", "net_ret": "formal_net_ret"})
    anchor_only = formal_anchor[~formal_anchor["candidate_key"].isin(set(neutral["candidate_key"]))].copy()
    daily_pool_count = neutral.groupby("entry_date")["candidate_key"].size()
    anchor_only["neutral_candidates_on_entry_date"] = anchor_only["entry_date"].astype(str).map(daily_pool_count).fillna(0).astype(int)
    anchor_only["entry_date_observable_in_neutral_pool"] = anchor_only["neutral_candidates_on_entry_date"].gt(0)
    anchor_only["reconstruction_status"] = "formal_anchor_only_not_observable_in_neutral_pool"

    daily = (
        neutral.groupby("entry_date", as_index=False)
        .agg(
            neutral_candidate_count=("candidate_key", "size"),
            formal_anchor_count=("formal26_status", lambda s: int((s == "formal_anchor_represented_in_neutral_pool").sum())),
            old_selected_anchor_count=("old_selected_anchor", "sum"),
        )
    )
    neutral["neutral_rank_by_wave_style_score"] = neutral.groupby("entry_date")["wave_style_score"].rank(
        method="min", ascending=False
    )
    formal_attribution = formal_anchor.merge(
        neutral[["candidate_key", "wave_style_score", "rank_key", "neutral_rank_by_wave_style_score"]],
        on="candidate_key",
        how="left",
        suffixes=("_formal", "_neutral"),
    ).merge(
        daily[["entry_date", "neutral_candidate_count"]], on="entry_date", how="left"
    )
    formal_attribution["neutral_candidate_count"] = formal_attribution["neutral_candidate_count"].fillna(0).astype(int)
    formal_attribution["neutral_observation_status"] = formal_attribution["wave_style_score_neutral"].notna().map(
        {True: "template_present", False: "template_absent_but_entry_date_observable"}
    )
    formal_attribution = formal_attribution.merge(anchors, on="candidate_key", how="left")
    summary: dict[str, Any] = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "neutral_pool_rows": int(len(neutral)),
        "neutral_pool_days": int(neutral["entry_date"].nunique()),
        "formal26_total": int(len(formal)),
        "formal26_represented_in_neutral_pool": int(neutral["formal26_status"].eq("formal_anchor_represented_in_neutral_pool").sum()),
        "formal26_anchor_only_not_observable": int(len(anchor_only)),
        "formal26_anchor_only_with_entry_date_observable": int(anchor_only["entry_date_observable_in_neutral_pool"].sum()),
        "formal26_attribution_status_counts": formal_attribution["neutral_observation_status"].value_counts().to_dict(),
        "old_selected_anchor_represented": int(neutral["old_selected_anchor"].sum()),
        "old_selected_anchor_total": int(len(old)),
        "historical_tdx_snapshot_dates": ["2026-07-06"],
        "verdict": "neutral pool is usable for candidate-volume and current-SW attribution; it is not a one-to-one replay of old Score120 scheduler or historical TDX diffusion",
    }

    neutral.to_csv(OUT_DIR / "neutral_candidate_pool.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(OUT_DIR / "daily_candidate_attribution.csv", index=False, encoding="utf-8-sig")
    formal_attribution.to_csv(OUT_DIR / "formal26_neutral_attribution.csv", index=False, encoding="utf-8-sig")
    anchor_only.to_csv(OUT_DIR / "formal_anchor_only_not_observable.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Score120 中性候选池重建 v1", "",
        "## 结论", "",
        f"- 中性候选池：{summary['neutral_pool_rows']} 行，覆盖 {summary['neutral_pool_days']} 个交易日。",
        f"- 正式主升 26 笔中，有 {summary['formal26_represented_in_neutral_pool']} 笔在中性池可观测；另有 {summary['formal26_anchor_only_not_observable']} 笔仅能作为正式锚点保存。",
        f"- 这 {summary['formal26_anchor_only_not_observable']} 笔的入场日都有其他中性候选，说明是中性模板规则未覆盖，而不是全市场候选底表缺日期。",
        "- 中性池不使用行业学习、滚动赢家调度或旧TDX行业扩散，适合先审计候选量、同日竞争和当前申万归因。",
        "- 历史TDX快照缺失；仓库仅有2026-07-06快照，严禁用于推断历史板块扩散。", "",
        "## 可用与不可用", "",
        "- 可用：按日候选数、正式样本覆盖、同日容量、当前申万行业归因、模板特征分布。",
        "- 不可用：把中性池结果当成旧Score120调度器的一比一收益重放，或对旧TDX/申万做干净的历史行业A/B。", "",
        "## 产物", "",
        "- `neutral_candidate_pool.csv`：去重后的中性模板候选池与旧样本锚点字段。",
        "- `daily_candidate_attribution.csv`：逐日候选数和锚点覆盖。",
        "- `formal26_neutral_attribution.csv`：正式26笔的逐日候选数、模板内主升分排名及模板外分支标记。",
        "- `formal_anchor_only_not_observable.csv`：无法在中性池观测的正式样本，后续必须单独追溯来源。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
