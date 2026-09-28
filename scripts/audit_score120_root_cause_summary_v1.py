from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


OUT_DIR = report_path("score120_root_cause_summary_v1")
FORMAL_SUMMARY = report_path("g3_formal_unified_five_strategy_contract_v1", "summary.json")
FORMAL_SELECTED = report_path("g3_formal_unified_five_strategy_contract_v1", "formal_unified_selected_candidates.csv")
FORMAL_INST = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
CURRENT_BAD = report_path("gen3_score120_core_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv")
SECTOR_SWITCH = report_path("score120_sector_switch_gap_audit_v1", "summary.json")
FAMILY_PROXY = report_path("score120_sector_family_proxy_v1", "summary.json")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _code6(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str)
    extracted = text.str.extract(r"(\d{6})", expand=False)
    return extracted.fillna(text.str.strip())


def _date_key(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("") + "|" + _code6(df["code"])


def _trade_metrics(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        return {"sample": label, "exists": False}
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    ret = pd.to_numeric(df.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    row: dict[str, Any] = {
        "sample": label,
        "exists": True,
        "rows": int(len(df)),
        "entry_start": str(df["entry_date"].min()) if "entry_date" in df.columns and not df.empty else "",
        "entry_end": str(df["entry_date"].max()) if "entry_date" in df.columns and not df.empty else "",
        "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
        "sum_net_ret": float(ret.sum()) if ret.notna().any() else None,
        "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
    }
    for col in ["score", "wave_style_score", "selected_score", "sector_diffusion_score"]:
        if col in df.columns:
            values = pd.to_numeric(df[col], errors="coerce")
            row[f"{col}_min"] = float(values.min()) if values.notna().any() else None
            row[f"{col}_max"] = float(values.max()) if values.notna().any() else None
            row[f"{col}_non_null"] = int(values.notna().sum())
    return row


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    formal_summary = _read_json(FORMAL_SUMMARY)
    sector_switch = _read_json(SECTOR_SWITCH)
    family_proxy = _read_json(FAMILY_PROXY)

    metrics = pd.DataFrame(
        [
            _trade_metrics(FORMAL_INST, "formal_institutional_score120_26"),
            _trade_metrics(CURRENT_BAD, "current_gen3_score120_core_strategy_v1_43"),
        ]
    )

    overlap_summary: dict[str, Any] = {}
    if FORMAL_INST.exists() and CURRENT_BAD.exists():
        formal = pd.read_csv(FORMAL_INST, encoding="utf-8-sig", low_memory=False)
        current = pd.read_csv(CURRENT_BAD, encoding="utf-8-sig", low_memory=False)
        formal_keys = set(_date_key(formal))
        current_keys = set(_date_key(current))
        overlap_summary = {
            "formal_rows": int(len(formal)),
            "current_rows": int(len(current)),
            "entry_date_code_overlap": int(len(formal_keys & current_keys)),
            "formal_only": int(len(formal_keys - current_keys)),
            "current_only": int(len(current_keys - formal_keys)),
        }

        formal_copy = formal.copy()
        current_copy = current.copy()
        formal_copy["entry_date_code_key"] = _date_key(formal_copy)
        current_copy["entry_date_code_key"] = _date_key(current_copy)
        formal_copy["sample"] = "formal_only_or_overlap"
        current_copy["sample"] = "current_only_or_overlap"
        compare_cols = [
            c
            for c in [
                "sample",
                "entry_date_code_key",
                "entry_date",
                "code",
                "name",
                "stock_name",
                "net_ret",
                "score",
                "wave_style_score",
                "selected_score",
                "sector_diffusion_score",
                "l2_sector_name",
            ]
            if c in set(formal_copy.columns) | set(current_copy.columns)
        ]
        pd.concat([formal_copy, current_copy], ignore_index=True, sort=False)[compare_cols].to_csv(
            OUT_DIR / "formal_vs_current_score120_rows.csv",
            index=False,
            encoding="utf-8-sig",
        )

    strategy_metrics = pd.DataFrame(formal_summary.get("strategy_metrics") or [])
    if not strategy_metrics.empty:
        strategy_metrics.to_csv(OUT_DIR / "formal_unified_strategy_metrics.csv", index=False, encoding="utf-8-sig")

    metrics.to_csv(OUT_DIR / "score120_trade_sample_metrics.csv", index=False, encoding="utf-8-sig")

    formal_total = formal_summary.get("formal_summary") or {}
    inst_metric = {}
    if not strategy_metrics.empty and "trade_strategy" in strategy_metrics.columns:
        inst_rows = strategy_metrics[strategy_metrics["trade_strategy"].astype(str).eq("institutional_score120_mainwave")]
        if not inst_rows.empty:
            inst_metric = inst_rows.iloc[0].to_dict()

    summary = {
        "status": "completed",
        "formal_unified_total_return": formal_total.get("total_return"),
        "formal_unified_max_drawdown": formal_total.get("max_drawdown"),
        "formal_unified_trade_count": formal_total.get("trade_count"),
        "formal_institutional_trade_count": inst_metric.get("trade_count"),
        "formal_institutional_avg_trade_return": inst_metric.get("avg_trade_return"),
        "formal_institutional_win_rate": inst_metric.get("win_rate"),
        "formal_institutional_pnl_contribution_rate": inst_metric.get("pnl_contribution_rate"),
        "formal_vs_current_overlap": overlap_summary,
        "sector_switch_top3_old_formal_current_sw": sector_switch.get("old_formal_current_sw_l2_top3_share"),
        "sector_switch_top3_old_formal_old_sector": sector_switch.get("old_formal_old_sector_top3_share"),
        "family_proxy_old_formal_hits_sw65": family_proxy.get("old_formal_hits_sw65"),
        "family_proxy_old_formal_hits_family65": family_proxy.get("old_formal_hits_family65"),
        "family_proxy_old_formal_hits_family_rescue": family_proxy.get("old_formal_hits_family_rescue"),
        "production_fix": {
            "institutional_source": str(FORMAL_INST),
            "bad_current_package": str(CURRENT_BAD),
            "decision": "do_not_use_current_gen3_score120_core_strategy_v1_as_formal_institutional_mainwave_source",
        },
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Score120 根因归档 v1",
        "",
        "## 结论",
        "",
        "- 当前低收益不是一个干净的“TDX 板块切到申万板块”实验。",
        "- 正式 G3 融合版收益来自五策略统一合同；其中机构主升 Score120 是 26 笔高贡献交易，而不是当前 43 笔研究包。",
        "- 当前 `gen3_score120_core_strategy_v1` 与正式机构主升在 entry_date+code 上重合度极低，且使用 `selected_score` 调度分，不能直接当作正式 `wave_style_score` 口径比较。",
        "- 申万二级确实存在语义拆分，但行业族代理只救回少数旧样本，不能解释主要收益断崖。",
        "",
        "## 核心数字",
        "",
        f"- 正式 G3 融合版：收益 {formal_total.get('total_return'):.4f}，最大回撤 {formal_total.get('max_drawdown'):.4f}，交易 {formal_total.get('trade_count')} 笔。",
        f"- 正式机构主升：交易 {inst_metric.get('trade_count')} 笔，平均收益 {inst_metric.get('avg_trade_return'):.4f}，胜率 {inst_metric.get('win_rate'):.4f}，PnL 贡献 {inst_metric.get('pnl_contribution_rate'):.4f}。",
        f"- 正式 26 笔 vs 当前 43 笔：entry_date+code 重合 {overlap_summary.get('entry_date_code_overlap')} 笔。",
        f"- 行业族代理：旧正式命中申万二级通过 {family_proxy.get('old_formal_hits_sw65')}，行业族通过 {family_proxy.get('old_formal_hits_family65')}，只新增 {family_proxy.get('old_formal_hits_family_rescue')}。",
        "",
        "## 样本指标",
        "",
        metrics.to_markdown(index=False),
        "",
        "## 处置建议",
        "",
        "1. 正式 G3 / 页面 / router 继续使用 `gen3_score120_formal_institutional_source_v1/closed_trades.csv` 作为机构主升历史源。",
        "2. `gen3_score120_core_strategy_v1` 仅保留为研究包，不进入正式收益对照和正式买入解释。",
        "3. 不建议仅凭当前结果切回 TDX 板块；应先重建旧 TDX 全量日截面候选池，做一对一同候选、同分数、同 gate 的申万/TDX 对照。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
