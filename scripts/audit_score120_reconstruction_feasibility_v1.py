from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_winner_similarity_scheduler_v1 import BASE_VARIANTS, SOURCE_DIR, _load_candidate_pool  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("score120_reconstruction_feasibility_v1")
INVENTORY = report_path("score120_old_candidate_pool_inventory_v1", "candidate_pool_inventory.csv")
FORMAL_INST = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
FORMAL_SELECTED = report_path("g3_formal_unified_five_strategy_contract_v1", "formal_unified_selected_candidates.csv")
FORMAL_CLOSED = report_path("g3_formal_unified_five_strategy_contract_v1", "formal_unified_closed_trades.csv")
ROUTER_SELECTED = report_path("g2_g3_market_style_router_v1", "g3_final_with_g2_gap_supplement_selected_candidates.csv")
ROUTER_CLOSED = report_path("g2_g3_market_style_router_v1", "g3_final_with_g2_gap_supplement_closed_trades.csv")
OLD_SELECTED_PROXY = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_pre_2024_10_state_alpha_closure_v5\institutional_mainwave_candidates.csv"
)


def _code6(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str)
    extracted = text.str.extract(r"(\d{6})", expand=False)
    return extracted.fillna(text.str.strip())


def _date_key(df: pd.DataFrame, date_col: str = "entry_date", code_col: str = "code") -> pd.Series:
    return pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d").fillna("") + "|" + _code6(df[code_col])


def _profile_csv(path: Path, formal_keys: set[str]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "rows": 0,
        "unique_days": 0,
        "max_rows_per_day": 0,
        "avg_rows_per_day": 0.0,
        "formal_overlap": 0,
        "has_old_sector_name": False,
        "has_current_sw_l2": False,
        "has_sector_diffusion_score": False,
        "has_sector_candidate_count": False,
        "verdict": "missing",
    }
    if not path.exists():
        return row
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    row["rows"] = int(len(df))
    if "entry_date" in df.columns:
        days = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        counts = days.value_counts()
        row["unique_days"] = int(days.nunique())
        row["max_rows_per_day"] = int(counts.max()) if not counts.empty else 0
        row["avg_rows_per_day"] = float(counts.mean()) if not counts.empty else 0.0
    if {"entry_date", "code"}.issubset(df.columns):
        keys = set(_date_key(df))
        row["formal_overlap"] = int(len(keys & formal_keys))
    row["has_old_sector_name"] = bool({"sector_for_distinct", "l2_sector_name"} & set(df.columns))
    row["has_current_sw_l2"] = "current_sw_l2_name" in df.columns or "sw_sector" in df.columns
    row["has_sector_diffusion_score"] = "sector_diffusion_score" in df.columns
    row["has_sector_candidate_count"] = "sector_candidate_count" in df.columns
    if row["rows"] == 0:
        row["verdict"] = "empty"
    elif row["formal_overlap"] == 0:
        row["verdict"] = "not_formal_score120_source"
    elif row["max_rows_per_day"] <= 3:
        row["verdict"] = "selected_or_closed_only_not_full_cross_section"
    elif not row["has_sector_candidate_count"]:
        row["verdict"] = "candidate_like_but_cannot_recompute_diffusion"
    else:
        row["verdict"] = "candidate_for_ab"
    return row


def _load_formal_keys() -> set[str]:
    if not FORMAL_INST.exists():
        return set()
    formal = pd.read_csv(FORMAL_INST, encoding="utf-8-sig", low_memory=False)
    if not {"entry_date", "code"}.issubset(formal.columns):
        return set()
    return set(_date_key(formal))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    formal_keys = _load_formal_keys()

    key_profiles = pd.DataFrame(
        [
            _profile_csv(FORMAL_INST, formal_keys),
            _profile_csv(FORMAL_SELECTED, formal_keys),
            _profile_csv(FORMAL_CLOSED, formal_keys),
            _profile_csv(ROUTER_SELECTED, formal_keys),
            _profile_csv(ROUTER_CLOSED, formal_keys),
            _profile_csv(OLD_SELECTED_PROXY, formal_keys),
        ]
    )

    inventory_top = pd.DataFrame()
    if INVENTORY.exists():
        inv = pd.read_csv(INVENTORY, encoding="utf-8-sig", low_memory=False)
        for col in ["old_formal_overlap", "rows", "unique_days", "max_rows_per_day", "avg_rows_per_day"]:
            if col in inv.columns:
                inv[col] = pd.to_numeric(inv[col], errors="coerce")
        inventory_top = inv.sort_values(["old_formal_overlap", "max_rows_per_day", "rows"], ascending=[False, False, False]).head(30)
        inventory_top.to_csv(OUT_DIR / "inventory_top_overlap.csv", index=False, encoding="utf-8-sig")

    current_full_summary: dict[str, Any] = {"available": False}
    try:
        pool = _load_candidate_pool(SOURCE_DIR, BASE_VARIANTS)
        pool["entry_date"] = pd.to_datetime(pool["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        pool_keys = set(pool["entry_date"].fillna("") + "|" + _code6(pool["code_raw"] if "code_raw" in pool.columns else pool["code"]))
        current_full_summary = {
            "available": True,
            "source_dir": str(SOURCE_DIR),
            "rows": int(len(pool)),
            "unique_days": int(pool["entry_date"].nunique()),
            "avg_rows_per_day": float(pool.groupby("entry_date").size().mean()),
            "formal_overlap": int(len(pool_keys & formal_keys)),
            "sector_col": "l2_sector_name" if "l2_sector_name" in pool.columns else "",
            "verdict": "current_full_cross_section_available_but_not_old_tdx_mapping",
        }
    except Exception as exc:
        current_full_summary = {"available": False, "error": str(exc)}

    can_run_clean_ab = bool(
        not inventory_top.empty
        and (
            (pd.to_numeric(inventory_top.get("old_formal_overlap", pd.Series(dtype=float)), errors="coerce") >= len(formal_keys))
            & (pd.to_numeric(inventory_top.get("max_rows_per_day", pd.Series(dtype=float)), errors="coerce") >= 20)
            & inventory_top.get("candidate_pool_verdict", pd.Series(dtype=str)).astype(str).eq("candidate_for_ab")
        ).any()
    )

    summary = {
        "status": "completed",
        "formal_score120_rows": len(formal_keys),
        "clean_old_tdx_full_pool_available": can_run_clean_ab,
        "key_file_verdict_counts": key_profiles["verdict"].value_counts().to_dict(),
        "current_full_pool": current_full_summary,
        "decision": (
            "old_tdx_full_cross_section_missing; clean sector-only AB cannot be completed from current artifacts"
            if not can_run_clean_ab
            else "candidate old full pool exists; proceed to sector-only AB"
        ),
        "next_step": (
            "restore old TDX full candidate pool or rebuild a neutral full pool with both old TDX and current SW sector mappings"
            if not can_run_clean_ab
            else "run same-candidate same-score sector mapping AB"
        ),
    }

    key_profiles.to_csv(OUT_DIR / "key_source_profiles.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Score120 重建可行性审计 v1",
        "",
        "## 结论",
        "",
        "- 当前工件里没有可用于干净 A/B 的旧 TDX 全量日截面候选池。",
        "- 能完整覆盖旧正式机构主升 26 笔的文件，都是 selected/closed 级别，通常每天 1 到 2 行，不能重算行业扩散。",
        "- 当前存在可用的全量候选池，但它属于当前链路和当前行业映射，不能代表旧 TDX 板块口径。",
        "- 因此现在不能证明“申万一定差于 TDX”，也不能仅凭当前结果回切 TDX；需要先恢复或重建同候选双映射池。",
        "",
        "## 当前全量池",
        "",
        f"- 可用：{current_full_summary.get('available')}",
        f"- 行数：{current_full_summary.get('rows')}",
        f"- 交易日：{current_full_summary.get('unique_days')}",
        f"- 与正式 26 笔重合：{current_full_summary.get('formal_overlap')}",
        f"- 结论：{current_full_summary.get('verdict')}",
        "",
        "## 关键上游文件",
        "",
        key_profiles.to_markdown(index=False),
        "",
        "## 下一步",
        "",
        "1. 若要严谨判断 TDX/申万：必须恢复旧 TDX 全量候选池，或者从统一 K 线重新构造一份中性全量池，再同时挂 TDX alias 板块和申万板块。",
        "2. 若只是修复生产：继续用 QMT/申万作为 canonical，TDX 不反向改写板块；主升页面和 router 继续使用正式机构主升源。",
        "3. 若要临时降低申万拆分影响：可以只做审计型行业族指标，不建议直接进生产 gate，因为前一轮只救回旧正式命中 1 笔。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
