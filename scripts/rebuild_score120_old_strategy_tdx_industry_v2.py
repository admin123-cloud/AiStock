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


OLD_STRATEGY_SOURCE = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_score120_core_strategy_v1\g3_route_execution_mandate_candidate_closed_trades.csv"
)
FORMAL_SOURCE = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OUT_DIR = report_path("score120_old_strategy_tdx_industry_v2")


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


def _trade_key(df: pd.DataFrame, date_col: str = "entry_date", code_col: str = "code") -> pd.Series:
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


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x:.2%}"


def _sector_summary(df: pd.DataFrame, sector_col: str, label: str) -> pd.DataFrame:
    work = df.copy()
    work[sector_col] = work.get(sector_col, pd.Series("", index=work.index)).map(_clean)
    work = work[work[sector_col] != ""].copy()
    if work.empty:
        return pd.DataFrame()
    rows = []
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
    return pd.DataFrame(rows).sort_values(["sum_realized_pnl", "rows"], ascending=[False, False])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    old = _read_csv(OLD_STRATEGY_SOURCE).copy()
    formal = _read_csv(FORMAL_SOURCE).copy()

    old["trade_key"] = _trade_key(old, "entry_date", "code")
    formal["trade_key"] = _trade_key(formal, "entry_date", "code")
    formal_keys = set(formal["trade_key"])

    old["old_strategy_condition_source"] = "backup_gen3_score120_core_strategy_v1"
    old["old_strategy_condition_chain"] = old.get("chain", pd.Series("", index=old.index)).map(_clean)
    old["old_strategy_gate_kept"] = (
        (pd.to_numeric(old.get("sector_diffusion_score"), errors="coerce") >= 65.0)
        & (pd.to_numeric(old.get("m30_close_above_ma20"), errors="coerce") >= 0.0)
        & (pd.to_numeric(old.get("sig_index_mom60"), errors="coerce") <= 0.05)
    )
    old["in_restored_formal_26"] = old["trade_key"].isin(formal_keys)
    old["code6"] = _code6(old["code"])

    codes = sorted(set(old["code"].fillna("").astype(str)) | set(old["code6"].fillna("").astype(str)))
    tdx = _load_tdx_map(codes)
    sw = _load_sw_map(codes)
    if not tdx.empty:
        old = old.merge(tdx.add_prefix("tdx_"), left_on="code6", right_on="tdx_code6", how="left")
    if not sw.empty:
        old = old.merge(sw.add_prefix("sw_"), left_on="code6", right_on="sw_code6", how="left")

    old["current_tdx_industry_name"] = old.get("tdx_tdx_industry_names", pd.Series("", index=old.index)).map(_clean)
    old["current_sw_l2_name"] = old.get("sw_sw_l2_name", pd.Series("", index=old.index)).map(_clean)
    old["tdx_only_for_mainline_identification"] = True
    old["sw_industry_owner_kept"] = True
    old["rebuild_scope"] = "old_strategy_conditions_plus_current_tdx_industry_membership; no_current_adjusted_candidate_pool"

    selected_cols = [
        "trade_key",
        "code",
        "name",
        "stock_name",
        "entry_date",
        "policy_exit_date",
        "net_ret",
        "realized_pnl",
        "rank_key",
        "wave_style_score",
        "selected_score",
        "score",
        "l2_sector_name",
        "current_sw_l2_name",
        "current_tdx_industry_name",
        "tdx_tdx_industry_codes",
        "tdx_tdx_block_names",
        "tdx_tdx_concept_names",
        "tdx_tdx_snapshot_date",
        "sector_candidate_count",
        "sector_avg_score",
        "sector_avg_ret5",
        "sector_avg_ret20",
        "sector_share",
        "sector_diffusion_score",
        "m30_close_above_ma20",
        "sig_index_mom60",
        "old_strategy_gate_kept",
        "old_strategy_condition_chain",
        "old_strategy_condition_source",
        "in_restored_formal_26",
        "tdx_only_for_mainline_identification",
        "sw_industry_owner_kept",
        "rebuild_scope",
    ]
    selected = old[[c for c in selected_cols if c in old.columns]].copy()

    formal_subset = old[old["in_restored_formal_26"]].copy()
    sector_compare = pd.concat(
        [
            _sector_summary(old, "l2_sector_name", "old_strategy_by_saved_sw_l2"),
            _sector_summary(old, "current_tdx_industry_name", "old_strategy_by_current_tdx_881"),
            _sector_summary(old, "current_sw_l2_name", "old_strategy_by_current_sw_l2"),
            _sector_summary(formal_subset, "current_tdx_industry_name", "formal26_subset_by_current_tdx_881"),
            _sector_summary(formal_subset, "current_sw_l2_name", "formal26_subset_by_current_sw_l2"),
        ],
        ignore_index=True,
    )

    def metrics(df: pd.DataFrame) -> dict[str, Any]:
        ret = pd.to_numeric(df.get("net_ret"), errors="coerce")
        pnl = pd.to_numeric(df.get("realized_pnl"), errors="coerce")
        return {
            "rows": int(len(df)),
            "unique_trade_keys": int(df["trade_key"].nunique()) if "trade_key" in df.columns else int(len(df)),
            "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
            "sum_net_ret": float(ret.sum()) if ret.notna().any() else None,
            "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
            "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
            "tdx_mapped_rows": int((df.get("current_tdx_industry_name", pd.Series("", index=df.index)).map(_clean) != "").sum()),
            "sw_mapped_rows": int((df.get("current_sw_l2_name", pd.Series("", index=df.index)).map(_clean) != "").sum()),
            "old_gate_pass_rows": int(df.get("old_strategy_gate_kept", pd.Series(False, index=df.index)).fillna(False).sum()),
        }

    old_metrics = metrics(old)
    formal_metrics = metrics(formal_subset)
    formal_original = metrics(formal)
    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "selection_contract": "old_strategy_conditions",
        "source": str(OLD_STRATEGY_SOURCE),
        "formal_reference_source": str(FORMAL_SOURCE),
        "out_dir": str(OUT_DIR),
        "old_strategy": old_metrics,
        "formal_26_subset_inside_old_strategy": formal_metrics,
        "formal_26_reference_dispatch_returns": formal_original,
        "formal_26_overlap": int(old["in_restored_formal_26"].sum()),
        "tdx_snapshot_dates": sorted(set(old.get("tdx_tdx_snapshot_date", pd.Series(dtype=str)).dropna().astype(str))),
        "verdict": "Use old Score120 strategy conditions as selection source. Current adjusted candidate pool is excluded from this rebuild.",
    }

    selected.to_csv(OUT_DIR / "old_score120_strategy_conditions_with_tdx_industry.csv", index=False, encoding="utf-8-sig")
    old.to_csv(OUT_DIR / "old_score120_strategy_conditions_full_enriched.csv", index=False, encoding="utf-8-sig")
    formal_subset.to_csv(OUT_DIR / "formal26_subset_in_old_strategy_conditions_with_tdx.csv", index=False, encoding="utf-8-sig")
    sector_compare.to_csv(OUT_DIR / "sector_contribution_old_strategy_tdx_vs_sw.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    lines = [
        "# Score120 旧策略选股条件 + TDX 行业重建 v2",
        "",
        "## 结论",
        "",
        f"- 本版选股条件源：`{OLD_STRATEGY_SOURCE}`。",
        "- 明确不使用当前调整后的 `wave_style_template_strategy_backtest_v1` / `BASE_VARIANTS` 候选池。",
        f"- 旧策略样本：{old_metrics['rows']} 笔，旧 gate 通过 {old_metrics['old_gate_pass_rows']} / {old_metrics['rows']}。",
        f"- 旧策略样本 PnL：{old_metrics['sum_realized_pnl']:,.2f}；收益和：{old_metrics['sum_net_ret']:.2%}；胜率：{_pct(old_metrics['win_rate'])}。",
        f"- 旧策略中覆盖正式 26 笔：{summary['formal_26_overlap']} / 26。",
        f"- 正式 26 笔在旧策略源内的原始 stake PnL：{formal_metrics['sum_realized_pnl']:,.2f}；正式 dispatch 参考 PnL：{formal_original['sum_realized_pnl']:,.2f}。",
        f"- TDX 881 行业映射覆盖：{old_metrics['tdx_mapped_rows']} / {old_metrics['rows']}；申万 L2 映射覆盖：{old_metrics['sw_mapped_rows']} / {old_metrics['rows']}。",
        "",
        "## 口径边界",
        "",
        "- TDX 行业/板块只作为主线识别行情基础，不反向改写个股申万行业归属。",
        "- 申万 L2 继续作为个股所属行业、行业归因和行业暴露口径。",
        "- 本版用于恢复旧策略选股条件和行业映射，不重跑当前调整后的选股逻辑。",
        "",
        "## 产物",
        "",
        "- `old_score120_strategy_conditions_with_tdx_industry.csv`：旧策略条件样本 + TDX/申万字段。",
        "- `formal26_subset_in_old_strategy_conditions_with_tdx.csv`：正式 26 笔在旧策略源中的子集。",
        "- `sector_contribution_old_strategy_tdx_vs_sw.csv`：TDX 881 与申万 L2 行业收益对照。",
        "- `summary.json`：机器可读摘要。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8-sig")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
