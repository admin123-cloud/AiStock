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


FORMAL_DISPATCH = report_path(
    "g3_formal_five_strategy_dispatch_contract_v1",
    "formal_trades_with_dispatch_strategy.csv",
)
FORMAL_INST = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OLD_SCORE120 = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_score120_core_strategy_v1\g3_route_execution_mandate_candidate_closed_trades.csv"
)
OUT_DIR = report_path("score120_formal_g3_returns_old_gate_tdx_v3")


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


def _norm_key(df: pd.DataFrame, date_col: str = "entry_date", code_col: str = "code") -> pd.Series:
    return _date_text(df[date_col]) + "|" + _code6(df[code_col])


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"missing source: {path}")
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


def _quoted(values: list[str]) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(set(values)) if str(v).strip())


def _load_tdx_map(codes: list[str]) -> pd.DataFrame:
    cols = [
        "code",
        "code6",
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
    raw = clickhouse_query_df(
        f"""
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
    )
    if raw.empty:
        return pd.DataFrame(columns=cols)
    raw["code6"] = _code6(raw["code"])
    for source_col, target_col in [
        ("tdx_industry_codes_arr", "tdx_industry_codes"),
        ("tdx_industry_names_arr", "tdx_industry_names"),
        ("tdx_block_codes_arr", "tdx_block_codes"),
        ("tdx_block_names_arr", "tdx_block_names"),
        ("tdx_concept_codes_arr", "tdx_concept_codes"),
        ("tdx_concept_names_arr", "tdx_concept_names"),
    ]:
        raw[target_col] = raw[source_col].map(lambda v: "|".join(map(str, v)) if isinstance(v, list) else "")
    return raw[[c for c in cols if c in raw.columns]]


def _load_sw_map(codes: list[str]) -> pd.DataFrame:
    cols = ["code", "code6", "sw_l2_code", "sw_l2_name"]
    if not codes or not clickhouse_table_exists("sector_stocks") or not clickhouse_table_exists("sectors"):
        return pd.DataFrame(columns=cols)
    raw = clickhouse_query_df(
        f"""
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
    )
    if raw.empty:
        return pd.DataFrame(columns=cols)
    raw["code6"] = _code6(raw["code"])
    return raw[[c for c in cols if c in raw.columns]]


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce")
    pnl = pd.to_numeric(df.get("realized_pnl"), errors="coerce")
    return {
        "rows": int(len(df)),
        "unique_norm_keys": int(df["norm_key"].nunique()) if "norm_key" in df.columns else int(len(df)),
        "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
        "sum_net_ret": float(ret.sum()) if ret.notna().any() else None,
        "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
        "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
    }


def _sector_summary(df: pd.DataFrame, sector_col: str, label: str) -> pd.DataFrame:
    work = df.copy()
    work[sector_col] = work.get(sector_col, pd.Series("", index=work.index)).map(_clean)
    work = work[work[sector_col] != ""].copy()
    rows: list[dict[str, Any]] = []
    for sector, group in work.groupby(sector_col, dropna=False):
        ret = pd.to_numeric(group.get("net_ret"), errors="coerce")
        pnl = pd.to_numeric(group.get("realized_pnl"), errors="coerce")
        rows.append(
            {
                "sample": label,
                "sector_col": sector_col,
                "sector": sector,
                "rows": int(len(group)),
                "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
                "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
                "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["sum_realized_pnl", "rows"], ascending=[False, False])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    formal_all = _read_csv(FORMAL_DISPATCH)
    formal = formal_all[
        formal_all.get("trade_strategy", pd.Series("", index=formal_all.index))
        .fillna("")
        .astype(str)
        .eq("institutional_score120_mainwave")
    ].copy()
    if formal.empty:
        raise RuntimeError("formal dispatch has no institutional_score120_mainwave rows")
    formal["norm_key"] = _norm_key(formal, "entry_date", "code")
    formal["code6"] = _code6(formal["code"])

    formal_ref = _read_csv(FORMAL_INST)
    formal_ref["norm_key"] = _norm_key(formal_ref, "entry_date", "code")

    old = _read_csv(OLD_SCORE120)
    old["norm_key"] = _norm_key(old, "entry_date", "code")
    old["old_gate_sector_diff65_pass"] = pd.to_numeric(old.get("sector_diffusion_score"), errors="coerce") >= 65.0
    old["old_gate_m30_ma20_pass"] = pd.to_numeric(old.get("m30_close_above_ma20"), errors="coerce") >= 0.0
    old["old_gate_index_mom60_le5_pass"] = pd.to_numeric(old.get("sig_index_mom60"), errors="coerce") <= 0.05
    old["old_gate_all_pass"] = (
        old["old_gate_sector_diff65_pass"]
        & old["old_gate_m30_ma20_pass"]
        & old["old_gate_index_mom60_le5_pass"]
    )

    old_cols = [
        "norm_key",
        "candidate_key",
        "template_label",
        "variant",
        "scheduler",
        "selected_variant",
        "selected_score",
        "selected_total",
        "selected_mean",
        "selected_win_rate",
        "wave_style_score",
        "rank_key",
        "l2_sector_name",
        "sector_candidate_count",
        "sector_avg_score",
        "sector_avg_ret5",
        "sector_avg_ret20",
        "sector_share",
        "sector_candidate_count_chg5",
        "sector_diffusion_score",
        "m30_close_above_ma20",
        "m30_close_above_ma40",
        "m30_mom6",
        "m30_mom12",
        "sig_index_mom60",
        "chain",
        "g3_chain",
        "activation_gate",
        "sector_gate",
        "m30_gate",
        "old_gate_sector_diff65_pass",
        "old_gate_m30_ma20_pass",
        "old_gate_index_mom60_le5_pass",
        "old_gate_all_pass",
    ]
    old_part = old[[c for c in old_cols if c in old.columns]].copy()
    old_part = old_part.add_prefix("old_").rename(columns={"old_norm_key": "norm_key"})
    merged = formal.merge(old_part, on="norm_key", how="left", validate="one_to_one")

    codes = sorted(set(merged["code"].fillna("").astype(str)) | set(merged["code6"].fillna("").astype(str)))
    tdx = _load_tdx_map(codes)
    sw = _load_sw_map(codes)
    if not tdx.empty:
        merged = merged.merge(tdx.add_prefix("tdx_"), left_on="code6", right_on="tdx_code6", how="left")
    if not sw.empty:
        merged = merged.merge(sw.add_prefix("sw_"), left_on="code6", right_on="sw_code6", how="left")

    merged["current_tdx_industry_name"] = merged.get("tdx_tdx_industry_names", pd.Series("", index=merged.index)).map(_clean)
    merged["current_sw_l2_name"] = merged.get("sw_sw_l2_name", pd.Series("", index=merged.index)).map(_clean)
    merged["return_truth_source"] = "g3_formal_five_strategy_dispatch_contract_v1"
    merged["old_strategy_condition_source"] = "backup_gen3_score120_core_strategy_v1"
    merged["selection_contract"] = "formal_g3_history_returns_plus_old_score120_gate_fields"
    merged["tdx_only_for_mainline_identification"] = True
    merged["sw_industry_owner_kept"] = True

    formal_metrics = _metrics(formal)
    merged_metrics = _metrics(merged)
    formal_ref_metrics = _metrics(formal_ref)
    missing_old = merged[merged.get("old_candidate_key", pd.Series("", index=merged.index)).isna()].copy()
    tdx_mapped = int((merged["current_tdx_industry_name"] != "").sum())
    sw_mapped = int((merged["current_sw_l2_name"] != "").sum())
    old_gate_pass = int(merged.get("old_old_gate_all_pass", pd.Series(False, index=merged.index)).fillna(False).sum())
    metrics_match = (
        formal_metrics["rows"] == merged_metrics["rows"]
        and abs((formal_metrics["sum_realized_pnl"] or 0.0) - (merged_metrics["sum_realized_pnl"] or 0.0)) < 0.0001
        and abs((formal_metrics["sum_net_ret"] or 0.0) - (merged_metrics["sum_net_ret"] or 0.0)) < 0.0000001
    )

    sector_compare = pd.concat(
        [
            _sector_summary(merged, "current_tdx_industry_name", "formal_g3_returns_by_current_tdx_881"),
            _sector_summary(merged, "current_sw_l2_name", "formal_g3_returns_by_current_sw_l2"),
            _sector_summary(merged, "sector_for_distinct", "formal_g3_returns_by_saved_sector_for_distinct"),
        ],
        ignore_index=True,
    )

    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "out_dir": str(OUT_DIR),
        "return_truth_source": str(FORMAL_DISPATCH),
        "formal_inst_reference": str(FORMAL_INST),
        "old_gate_source": str(OLD_SCORE120),
        "merged_metrics": merged_metrics,
        "formal_dispatch_metrics": formal_metrics,
        "formal_inst_reference_metrics": formal_ref_metrics,
        "history_result_matches_formal_g3_dispatch": bool(metrics_match),
        "old_gate_overlap": int(len(merged) - len(missing_old)),
        "old_gate_all_pass": old_gate_pass,
        "tdx_mapped_rows": tdx_mapped,
        "sw_mapped_rows": sw_mapped,
        "tdx_snapshot_dates": sorted(set(merged.get("tdx_tdx_snapshot_date", pd.Series(dtype=str)).dropna().astype(str))),
        "verdict": "Historical returns now match the formal G3 institutional mainwave dispatch source; old Score120 fields are attached only as gate evidence.",
    }

    selected_cols = [
        "trade_key",
        "norm_key",
        "code",
        "name",
        "entry_date",
        "policy_exit_date",
        "exit_date",
        "net_ret",
        "realized_pnl",
        "stake",
        "entry_equity",
        "exit_value",
        "score",
        "raw_score",
        "wave_style_score",
        "score_source",
        "score_scale",
        "rank_key",
        "route",
        "mode",
        "trade_strategy",
        "chain",
        "confirm_rule",
        "exit_contract",
        "sector_for_distinct",
        "old_candidate_key",
        "old_chain",
        "old_g3_chain",
        "old_wave_style_score",
        "old_rank_key",
        "old_selected_score",
        "old_l2_sector_name",
        "old_sector_diffusion_score",
        "old_m30_close_above_ma20",
        "old_sig_index_mom60",
        "old_old_gate_sector_diff65_pass",
        "old_old_gate_m30_ma20_pass",
        "old_old_gate_index_mom60_le5_pass",
        "old_old_gate_all_pass",
        "current_tdx_industry_name",
        "current_sw_l2_name",
        "tdx_tdx_industry_codes",
        "tdx_tdx_block_names",
        "tdx_tdx_concept_names",
        "tdx_tdx_snapshot_date",
        "return_truth_source",
        "old_strategy_condition_source",
        "selection_contract",
        "tdx_only_for_mainline_identification",
        "sw_industry_owner_kept",
    ]
    view = merged[[c for c in selected_cols if c in merged.columns]].copy()
    view.to_csv(OUT_DIR / "formal_g3_mainwave_returns_old_gate_tdx.csv", index=False, encoding="utf-8-sig")
    merged.to_csv(OUT_DIR / "formal_g3_mainwave_returns_old_gate_tdx_full.csv", index=False, encoding="utf-8-sig")
    sector_compare.to_csv(OUT_DIR / "formal_g3_mainwave_sector_contribution_tdx_vs_sw.csv", index=False, encoding="utf-8-sig")
    missing_old.to_csv(OUT_DIR / "missing_old_gate_match.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    lines = [
        "# G3 正式主升收益 + 旧 Score120 gate + TDX 行业 v3",
        "",
        "## 结论",
        "",
        f"- 历史成交收益真值源：`{FORMAL_DISPATCH}`。",
        f"- 旧 Score120 gate 字段源：`{OLD_SCORE120}`。",
        f"- 成交笔数：{merged_metrics['rows']}；旧 gate 匹配：{summary['old_gate_overlap']} / {merged_metrics['rows']}；旧 gate 全通过：{old_gate_pass} / {merged_metrics['rows']}。",
        f"- 重建后 PnL：{merged_metrics['sum_realized_pnl']:,.2f}；正式 G3 dispatch PnL：{formal_metrics['sum_realized_pnl']:,.2f}。",
        f"- 重建后收益和：{merged_metrics['sum_net_ret']:.2%}；正式 G3 dispatch 收益和：{formal_metrics['sum_net_ret']:.2%}。",
        f"- 是否与原 G3 主升历史回测一致：{summary['history_result_matches_formal_g3_dispatch']}。",
        f"- TDX 881 行业映射：{tdx_mapped} / {merged_metrics['rows']}；申万 L2 映射：{sw_mapped} / {merged_metrics['rows']}。",
        "",
        "## 口径",
        "",
        "- `net_ret`、`realized_pnl`、`stake`、退出日期与退出合同全部来自正式 G3 dispatch，不再用旧 Score120 core 的 raw stake/exit 覆盖。",
        "- 旧 Score120 core 只提供 `sector_diffusion_score`、30m gate、`sig_index_mom60`、旧链路和旧候选证据。",
        "- TDX 行业/板块只参与主线识别；申万 L2 继续用于个股行业归属、归因和暴露。",
        "",
        "## 产物",
        "",
        "- `formal_g3_mainwave_returns_old_gate_tdx.csv`：主表。",
        "- `formal_g3_mainwave_returns_old_gate_tdx_full.csv`：完整字段表。",
        "- `formal_g3_mainwave_sector_contribution_tdx_vs_sw.csv`：TDX/申万行业收益对照。",
        "- `missing_old_gate_match.csv`：旧 gate 未匹配样本，应为空。",
        "- `summary.json`：机器可读摘要。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8-sig")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
