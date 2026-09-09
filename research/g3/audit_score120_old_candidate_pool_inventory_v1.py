from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import csv
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402


REPORT_ROOTS = [
    report_path(),
    Path(r"F:\Stock\AiStockResearchArchive\backups"),
    Path(r"F:\Stock\AiStock\reports"),
]
OUT_DIR = report_path("score120_old_candidate_pool_inventory_v1")
FORMAL_SOURCE = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
MAX_SCAN_BYTES = 25 * 1024 * 1024


def _code6(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str)
    extracted = text.str.extract(r"(\d{6})", expand=False)
    return extracted.fillna(text.str.strip())


def _date_text(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d").fillna("")


def _read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        return next(reader, [])


def _key(df: pd.DataFrame) -> pd.Series:
    date_col = next((c for c in ["entry_date", "trade_date", "signal_date", "decision_date", "date"] if c in df.columns), None)
    code_col = next((c for c in ["code", "code6", "code_raw", "stock_code"] if c in df.columns), None)
    if not date_col or not code_col:
        return pd.Series("", index=df.index)
    return _date_text(df[date_col]) + "|" + _code6(df[code_col])


def _num_stats(df: pd.DataFrame, col: str) -> dict[str, Any]:
    if col not in df.columns:
        return {f"{col}_min": None, f"{col}_mean": None, f"{col}_max": None}
    s = pd.to_numeric(df[col], errors="coerce")
    if not s.notna().any():
        return {f"{col}_min": None, f"{col}_mean": None, f"{col}_max": None}
    return {
        f"{col}_min": float(s.min()),
        f"{col}_mean": float(s.mean()),
        f"{col}_max": float(s.max()),
    }


def _top_values(df: pd.DataFrame, col: str, limit: int = 8) -> str:
    if col not in df.columns:
        return ""
    vc = df[col].fillna("").astype(str).value_counts().head(limit)
    return "; ".join(f"{k}:{int(v)}" for k, v in vc.items())


def _candidate_verdict(row: dict[str, Any]) -> str:
    if row["old_formal_overlap"] >= 24 and row.get("avg_rows_per_day", 0) < 2:
        return "old_selected_candidate_set_not_full_cross_section"
    if row["old_formal_overlap"] >= 24 and not row["has_l2_sector_name"] and not row["has_sector_for_distinct"]:
        return "partial_bridge_only_no_industry_mapping"
    if row["old_formal_overlap"] >= 20 and row["has_sector_for_distinct"]:
        return "selected_trade_mapping_audit_only"
    if row["has_sector_diffusion_score"] and row["has_sector_candidate_count"] and row["old_formal_overlap"] == 0:
        return "current_chain_not_old_formal_source"
    if row["has_sector_diffusion_score"] and row["old_formal_overlap"] > 0:
        return "partial_selected_or_derived_source"
    return "not_relevant_for_sector_ab"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    formal = pd.read_csv(FORMAL_SOURCE, encoding="utf-8-sig", low_memory=False)
    old_keys = set(_key(formal))
    rows: list[dict[str, Any]] = []
    target_cols = {
        "sector_diffusion_score",
        "sector_candidate_count",
        "sector_for_distinct",
        "l2_sector_name",
        "industry",
        "industry_top",
        "wave_style_score",
        "selected_score",
        "rank_key",
    }
    for root in REPORT_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.csv"):
            try:
                size = path.stat().st_size
                if size > MAX_SCAN_BYTES:
                    continue
                header = _read_header(path)
            except Exception:
                continue
            cols = set(header)
            if not (cols & target_cols):
                continue
            if not ({"entry_date", "trade_date", "signal_date", "decision_date"} & cols):
                continue
            if not ({"code", "code6", "code_raw", "stock_code"} & cols):
                continue
            if not ({"wave_style_score", "selected_score", "sector_diffusion_score", "rank_key"} & cols):
                continue
            try:
                df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
            except Exception:
                continue
            keys = set(_key(df))
            old_overlap = len(old_keys & keys)
            date_col = next((c for c in ["entry_date", "trade_date", "signal_date", "decision_date", "date"] if c in df.columns), None)
            if date_col:
                day_counts = _date_text(df[date_col]).value_counts()
                unique_days = int(day_counts[day_counts.index != ""].shape[0])
                max_rows_per_day = int(day_counts.max()) if not day_counts.empty else 0
                avg_rows_per_day = float(len(df) / unique_days) if unique_days else None
            else:
                unique_days = 0
                max_rows_per_day = 0
                avg_rows_per_day = None
            row: dict[str, Any] = {
                "path": str(path),
                "bytes": int(size),
                "rows": int(len(df)),
                "cols": int(len(header)),
                "unique_days": unique_days,
                "max_rows_per_day": max_rows_per_day,
                "avg_rows_per_day": avg_rows_per_day,
                "old_formal_overlap": int(old_overlap),
                "old_formal_overlap_rate": float(old_overlap / len(old_keys)) if old_keys else None,
                "has_sector_diffusion_score": "sector_diffusion_score" in cols,
                "has_sector_candidate_count": "sector_candidate_count" in cols,
                "has_l2_sector_name": "l2_sector_name" in cols,
                "has_sector_for_distinct": "sector_for_distinct" in cols,
                "has_industry": "industry" in cols,
                "has_industry_top": "industry_top" in cols,
                "has_trade_strategy": "trade_strategy" in cols,
                "has_source_strategy_label": "source_strategy_label" in cols,
                "top_l2_sector_name": _top_values(df, "l2_sector_name"),
                "top_sector_for_distinct": _top_values(df, "sector_for_distinct"),
                "top_industry": _top_values(df, "industry"),
                "top_trade_strategy": _top_values(df, "trade_strategy"),
            }
            for col in ["wave_style_score", "selected_score", "rank_key", "sector_diffusion_score", "sector_candidate_count"]:
                row.update(_num_stats(df, col))
            row["candidate_pool_verdict"] = _candidate_verdict(row)
            rows.append(row)

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(
            [
                "old_formal_overlap",
                "has_sector_diffusion_score",
                "has_sector_candidate_count",
                "rows",
            ],
            ascending=[False, False, False, False],
        ).reset_index(drop=True)
    out.to_csv(OUT_DIR / "candidate_pool_inventory.csv", index=False, encoding="utf-8-sig")

    verdict_counts = (
        out["candidate_pool_verdict"].value_counts().reset_index()
        if not out.empty and "candidate_pool_verdict" in out.columns
        else pd.DataFrame(columns=["candidate_pool_verdict", "count"])
    )
    if not verdict_counts.empty:
        verdict_counts.columns = ["candidate_pool_verdict", "count"]
    verdict_counts.to_csv(OUT_DIR / "verdict_counts.csv", index=False, encoding="utf-8-sig")

    top = out.head(30) if not out.empty else out
    summary = {
        "status": "completed",
        "scanned_roots": [str(p) for p in REPORT_ROOTS],
        "rows": int(len(out)),
        "old_formal_total": int(len(old_keys)),
        "max_old_formal_overlap": int(out["old_formal_overlap"].max()) if not out.empty else 0,
        "usable_full_old_candidate_pool_found": bool(
            not out.empty
            and (
                (out["old_formal_overlap"] >= len(old_keys))
                & out["has_sector_diffusion_score"]
                & out["has_sector_candidate_count"]
                & (out["rows"] > len(old_keys))
                & (out["avg_rows_per_day"].fillna(0) >= 5)
                & (out["max_rows_per_day"].fillna(0) >= 10)
            ).any()
        ),
        "verdict_counts": verdict_counts.to_dict("records"),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Score120 旧候选池库存审计 v1",
        "",
        "## 结论",
        "",
        f"- 扫描候选文件：{summary['rows']} 个。",
        f"- 旧正式机构主升样本数：{summary['old_formal_total']}。",
        f"- 最大旧样本重合数：{summary['max_old_formal_overlap']}。",
        f"- 是否找到可直接做完整 TDX vs 申万 sector_diffusion A/B 的旧候选池：{summary['usable_full_old_candidate_pool_found']}。",
        "",
        "## 判定分布",
        "",
        verdict_counts.to_markdown(index=False),
        "",
        "## Top 候选文件",
        "",
        top[
            [
                "candidate_pool_verdict",
                "old_formal_overlap",
                "rows",
                "unique_days",
                "avg_rows_per_day",
                "max_rows_per_day",
                "has_sector_diffusion_score",
                "has_sector_candidate_count",
                "has_l2_sector_name",
                "has_sector_for_distinct",
                "wave_style_score_min",
                "wave_style_score_max",
                "selected_score_min",
                "selected_score_max",
                "path",
            ]
        ].to_markdown(index=False)
        if not top.empty
        else "无",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
