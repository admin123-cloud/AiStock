from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from utils.paths import report_path  # noqa: E402


FORMAL_SOURCE = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OLD_CANDIDATE_SOURCE = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_score120_core_strategy_v1\g3_route_execution_mandate_candidate_closed_trades.csv"
)
OUT_DIR = report_path("score120_formal_tdx_industry_rebuild_v1")


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
    except Exception:
        pass
    return value


def _clean(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _code6(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str)
    extracted = text.str.extract(r"(\d{6})", expand=False)
    return extracted.fillna(text.str.strip())


def _date_text(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d").fillna("")


def _key(df: pd.DataFrame, date_col: str = "entry_date", code_col: str = "code") -> pd.Series:
    return _date_text(df[date_col]) + "|" + _code6(df[code_col])


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"missing required source: {path}")
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


def _quoted(values: list[str]) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(set(values)) if str(v).strip())


def _load_tdx_map(codes: list[str]) -> pd.DataFrame:
    cols = [
        "code",
        "tdx_snapshot_date",
        "tdx_industry_codes",
        "tdx_industry_names",
        "tdx_block_codes",
        "tdx_block_names",
        "tdx_concept_codes",
        "tdx_concept_names",
        "tdx_membership_count",
    ]
    if not codes or not clickhouse_table_exists("source_sector_stocks"):
        return pd.DataFrame(columns=cols)
    sql = f"""
    SELECT
        canonical_code AS code,
        max(snapshot_date) AS tdx_snapshot_date,
        groupUniqArrayIf(sector_code, startsWith(sector_code, '881')) AS tdx_industry_codes_arr,
        groupUniqArrayIf(sector_name, startsWith(sector_code, '881')) AS tdx_industry_names_arr,
        groupUniqArrayIf(sector_code, NOT startsWith(sector_code, '881')) AS tdx_block_codes_arr,
        groupUniqArrayIf(sector_name, NOT startsWith(sector_code, '881')) AS tdx_block_names_arr,
        groupUniqArrayIf(sector_code, startsWith(sector_code, '8805') OR startsWith(sector_code, '8806') OR startsWith(sector_code, '8807') OR startsWith(sector_code, '8809')) AS tdx_concept_codes_arr,
        groupUniqArrayIf(sector_name, startsWith(sector_code, '8805') OR startsWith(sector_code, '8806') OR startsWith(sector_code, '8807') OR startsWith(sector_code, '8809')) AS tdx_concept_names_arr,
        count() AS tdx_membership_count
    FROM source_sector_stocks
    WHERE source = 'tdx'
      AND canonical_code IN ({_quoted(codes)})
    GROUP BY canonical_code
    """
    raw = clickhouse_query_df(sql)
    if raw.empty:
        return pd.DataFrame(columns=cols)
    out = raw.copy()
    for source_col, target_col in [
        ("tdx_industry_codes_arr", "tdx_industry_codes"),
        ("tdx_industry_names_arr", "tdx_industry_names"),
        ("tdx_block_codes_arr", "tdx_block_codes"),
        ("tdx_block_names_arr", "tdx_block_names"),
        ("tdx_concept_codes_arr", "tdx_concept_codes"),
        ("tdx_concept_names_arr", "tdx_concept_names"),
    ]:
        out[target_col] = out[source_col].map(lambda v: "|".join(map(str, v)) if isinstance(v, list) else "")
    out["code6"] = _code6(out["code"])
    return out[[c for c in cols + ["code6"] if c in out.columns]]


def _load_sw_map(codes: list[str]) -> pd.DataFrame:
    cols = ["code", "sw_l2_code", "sw_l2_name", "code6"]
    if not codes or not clickhouse_table_exists("sector_stocks") or not clickhouse_table_exists("sectors"):
        return pd.DataFrame(columns=cols)
    sql = f"""
    SELECT
        ss.stock_code AS code,
        any(s.code) AS sw_l2_code,
        any(s.name) AS sw_l2_name
    FROM sector_stocks ss
    INNER JOIN sectors s ON ss.sector_code = s.code
    WHERE ss.stock_code IN ({_quoted(codes)})
      AND s.type = 'industry'
      AND s.level = 2
    GROUP BY ss.stock_code
    """
    raw = clickhouse_query_df(sql)
    if raw.empty:
        return pd.DataFrame(columns=cols)
    raw["code6"] = _code6(raw["code"])
    return raw[cols]


def _sector_return(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    if col not in df.columns:
        return pd.DataFrame(columns=["sample", "sector_col", "sector", "rows", "sum_realized_pnl", "avg_net_ret", "win_rate"])
    work = df.copy()
    work["__sector"] = work[col].map(_clean)
    work = work[work["__sector"] != ""]
    rows: list[dict[str, Any]] = []
    for sector, group in work.groupby("__sector", dropna=False):
        pnl = pd.to_numeric(group.get("realized_pnl"), errors="coerce")
        ret = pd.to_numeric(group.get("net_ret"), errors="coerce")
        rows.append(
            {
                "sample": label,
                "sector_col": col,
                "sector": sector,
                "rows": int(len(group)),
                "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
                "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
                "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
            }
        )
    return pd.DataFrame(rows).sort_values(["sum_realized_pnl", "rows"], ascending=[False, False])


def _gate_bool(series: pd.Series, op: str, threshold: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if op == ">=":
        return values >= threshold
    if op == "<=":
        return values <= threshold
    raise ValueError(op)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    formal = _read_csv(FORMAL_SOURCE).copy()
    old_candidate = _read_csv(OLD_CANDIDATE_SOURCE).copy()

    formal["key"] = _key(formal, "entry_date", "code")
    old_candidate["key"] = _key(old_candidate, "entry_date", "code")

    keep_old = [
        "key",
        "trade_date",
        "code",
        "l2_sector_name",
        "template_label",
        "rank_key",
        "wave_style_score",
        "selected_score",
        "sector_candidate_count",
        "sector_avg_score",
        "sector_avg_ret5",
        "sector_avg_ret20",
        "sector_share",
        "sector_candidate_count_chg5",
        "sector_diffusion_score",
        "m30_ok",
        "m30_close_above_ma20",
        "m30_close_above_ma40",
        "m30_mom6",
        "m30_mom12",
        "sig_index_mom60",
        "activation_gate_pass",
        "chain",
        "g3_chain",
        "activation_gate",
        "sector_gate",
        "m30_gate",
    ]
    old_part = old_candidate[[c for c in keep_old if c in old_candidate.columns]].copy()
    old_part = old_part.add_prefix("old_candidate_").rename(columns={"old_candidate_key": "key"})
    merged = formal.merge(old_part, on="key", how="left", validate="one_to_one")

    merged["code6"] = _code6(merged["code"])
    codes = sorted(set(merged["code"].fillna("").astype(str)) | set(merged["code6"].fillna("").astype(str)))
    tdx_map = _load_tdx_map(codes)
    sw_map = _load_sw_map(codes)
    if not tdx_map.empty:
        merged = merged.merge(tdx_map.add_prefix("tdx_"), left_on="code6", right_on="tdx_code6", how="left")
    if not sw_map.empty:
        merged = merged.merge(sw_map.add_prefix("sw_"), left_on="code6", right_on="sw_code6", how="left")

    merged["saved_historical_tdx_sector"] = merged.get("sector_for_distinct", pd.Series("", index=merged.index)).map(_clean)
    merged["current_tdx_industry_name"] = merged.get("tdx_tdx_industry_names", pd.Series("", index=merged.index)).map(_clean)
    merged["current_sw_l2_name"] = merged.get("sw_sw_l2_name", pd.Series("", index=merged.index)).map(_clean)
    merged["tdx_industry_matches_saved_sector"] = (
        merged["saved_historical_tdx_sector"] != ""
    ) & merged.apply(lambda row: row["saved_historical_tdx_sector"] in str(row["current_tdx_industry_name"]).split("|"), axis=1)
    merged["sw_l2_matches_saved_sector"] = merged["saved_historical_tdx_sector"].eq(merged["current_sw_l2_name"])

    sector_score = pd.to_numeric(
        merged.get("old_candidate_sector_diffusion_score", merged.get("sector_diffusion_score")),
        errors="coerce",
    )
    m30_score = pd.to_numeric(merged.get("old_candidate_m30_close_above_ma20"), errors="coerce")
    idx_mom60 = pd.to_numeric(merged.get("old_candidate_sig_index_mom60", merged.get("mom60")), errors="coerce")
    merged["same_candidate_score_gate_source"] = "formal_26_plus_old_backup_candidate_gate_fields"
    merged["same_gate_sector_diff65_pass"] = sector_score >= 65.0
    merged["same_gate_m30_ma20_pass"] = m30_score >= 0.0
    merged["same_gate_index_mom60_le5_pass"] = idx_mom60 <= 0.05
    merged["same_gate_all_pass"] = (
        merged["same_gate_sector_diff65_pass"]
        & merged["same_gate_m30_ma20_pass"].fillna(False)
        & merged["same_gate_index_mom60_le5_pass"].fillna(False)
    )
    merged["tdx_rebuild_scope"] = "attach_current_tdx_881_industry_and_tdx_blocks; no_full_cross_section_diffusion_recompute"

    restored_cols = [
        "key",
        "code",
        "name",
        "entry_date",
        "policy_exit_date",
        "exit_date",
        "net_ret",
        "realized_pnl",
        "score",
        "raw_score",
        "wave_style_score",
        "sector_diffusion_score",
        "old_candidate_sector_diffusion_score",
        "old_candidate_m30_close_above_ma20",
        "old_candidate_sig_index_mom60",
        "saved_historical_tdx_sector",
        "current_tdx_industry_name",
        "current_sw_l2_name",
        "tdx_industry_matches_saved_sector",
        "sw_l2_matches_saved_sector",
        "tdx_tdx_industry_codes",
        "tdx_tdx_block_names",
        "tdx_tdx_concept_names",
        "tdx_tdx_snapshot_date",
        "tdx_tdx_membership_count",
        "same_candidate_score_gate_source",
        "same_gate_sector_diff65_pass",
        "same_gate_m30_ma20_pass",
        "same_gate_index_mom60_le5_pass",
        "same_gate_all_pass",
        "tdx_rebuild_scope",
    ]
    restored = merged[[c for c in restored_cols if c in merged.columns]].copy()

    by_saved = _sector_return(merged, "saved_historical_tdx_sector", "formal_by_saved_historical_tdx_sector")
    by_current_tdx = _sector_return(merged, "current_tdx_industry_name", "formal_by_current_tdx_881_industry")
    by_sw = _sector_return(merged, "current_sw_l2_name", "formal_by_current_sw_l2")
    sector_compare = pd.concat([by_saved, by_current_tdx, by_sw], ignore_index=True)

    gate_summary = pd.DataFrame(
        [
            {"gate": "sector_diffusion_score >= 65", "pass_count": int(merged["same_gate_sector_diff65_pass"].sum()), "total": int(len(merged))},
            {"gate": "m30_close_above_ma20 >= 0", "pass_count": int(merged["same_gate_m30_ma20_pass"].sum()), "total": int(len(merged))},
            {"gate": "sig_index_mom60 <= 0.05", "pass_count": int(merged["same_gate_index_mom60_le5_pass"].sum()), "total": int(len(merged))},
            {"gate": "all three restored gates", "pass_count": int(merged["same_gate_all_pass"].sum()), "total": int(len(merged))},
        ]
    )

    pnl = pd.to_numeric(merged.get("realized_pnl"), errors="coerce")
    ret = pd.to_numeric(merged.get("net_ret"), errors="coerce")
    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "formal_source": str(FORMAL_SOURCE),
        "old_candidate_source": str(OLD_CANDIDATE_SOURCE),
        "out_dir": str(OUT_DIR),
        "rows": int(len(merged)),
        "old_candidate_overlap": int(merged["old_candidate_code"].notna().sum()) if "old_candidate_code" in merged.columns else 0,
        "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
        "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
        "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
        "saved_tdx_sector_match_current_tdx_881": int(merged["tdx_industry_matches_saved_sector"].sum()),
        "saved_tdx_sector_match_current_sw_l2": int(merged["sw_l2_matches_saved_sector"].sum()),
        "tdx_industry_mapped_rows": int((merged["current_tdx_industry_name"] != "").sum()),
        "sw_l2_mapped_rows": int((merged["current_sw_l2_name"] != "").sum()),
        "same_gate_all_pass": int(merged["same_gate_all_pass"].sum()),
        "tdx_snapshot_dates": sorted(set(merged.get("tdx_tdx_snapshot_date", pd.Series(dtype=str)).dropna().astype(str))),
        "scope_note": "Same candidate, same saved score, same restored gate fields are preserved. TDX industry is attached from current source_sector_stocks 881 membership; full historical sector_diffusion recompute still requires the old full daily candidate cross-section.",
    }

    restored.to_csv(OUT_DIR / "formal_score120_restored_same_candidate_tdx_industry.csv", index=False, encoding="utf-8-sig")
    merged.to_csv(OUT_DIR / "formal_score120_restored_full_enriched.csv", index=False, encoding="utf-8-sig")
    sector_compare.to_csv(OUT_DIR / "formal_score120_sector_contribution_tdx_vs_sw.csv", index=False, encoding="utf-8-sig")
    gate_summary.to_csv(OUT_DIR / "formal_score120_restored_gate_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    lines = [
        "# Score120 正式机构主升 TDX 行业恢复包 v1",
        "",
        "## 结论",
        "",
        f"- 恢复基准：正式机构主升历史源 `{FORMAL_SOURCE}`，共 {summary['rows']} 笔。",
        f"- 旧候选 gate 字段覆盖：{summary['old_candidate_overlap']} / {summary['rows']}。",
        f"- TDX 881 行业映射覆盖：{summary['tdx_industry_mapped_rows']} / {summary['rows']}；申万二级映射覆盖：{summary['sw_l2_mapped_rows']} / {summary['rows']}。",
        f"- 历史保存行业 `sector_for_distinct` 与当前 TDX 881 行业一致：{summary['saved_tdx_sector_match_current_tdx_881']} / {summary['rows']}。",
        f"- 历史保存行业与当前申万二级一致：{summary['saved_tdx_sector_match_current_sw_l2']} / {summary['rows']}。",
        f"- 同候选、同分数、同 gate 三项均通过：{summary['same_gate_all_pass']} / {summary['rows']}。",
        f"- PnL：{summary['sum_realized_pnl']:,.2f}；平均收益：{summary['avg_net_ret']:.2%}；胜率：{summary['win_rate']:.2%}。",
        "",
        "## 边界",
        "",
        "- 本包恢复的是正式 26 笔历史源口径，并补上当前 TDX 行业/板块信息。",
        "- 当前可用文件没有旧全量日截面候选池，因此这里不重算完整 TDX `sector_diffusion_score`，只保留旧 gate 字段并做同候选映射对照。",
        "- 若要做真正的 TDX vs 申万扩散分 A/B，需要找回或重建旧全量候选截面。",
        "",
        "## Gate 摘要",
        "",
        gate_summary.to_markdown(index=False),
        "",
        "## 行业收益对照",
        "",
        sector_compare.to_markdown(index=False),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
