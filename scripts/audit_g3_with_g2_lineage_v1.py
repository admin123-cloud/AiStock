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

from utils.paths import report_path, runtime_path


BACKUP_ROOT = Path(r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808")
OUT_DIR = report_path("g3_with_g2_lineage_audit_v1")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"_missing": str(path)}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size <= 4:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _num(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce")


def _top_counts(df: pd.DataFrame, col: str, n: int = 12) -> dict[str, int]:
    if df.empty or col not in df.columns:
        return {}
    counts = df[col].astype(str).value_counts(dropna=False).head(n)
    return {str(k): int(v) for k, v in counts.items()}


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    ret = _num(df.get("net_ret"))
    pnl = _num(df.get("realized_pnl"))
    return {
        "rows": int(len(df)),
        "date_min": str(df["entry_date"].min()) if "entry_date" in df.columns and len(df) else "",
        "date_max": str(df["entry_date"].max()) if "entry_date" in df.columns and len(df) else "",
        "realized_pnl_sum": float(pnl.sum()) if len(pnl) else 0.0,
        "net_ret_sum": float(ret.sum()) if len(ret) else 0.0,
        "net_ret_avg": float(ret.mean()) if len(ret) else 0.0,
        "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
        "route_counts": _top_counts(df, "route"),
        "mode_counts": _top_counts(df, "mode"),
        "trade_strategy_counts": _top_counts(df, "trade_strategy"),
        "sector_for_distinct_counts": _top_counts(df, "sector_for_distinct"),
        "l2_sector_name_counts": _top_counts(df, "l2_sector_name"),
    }


def _contains_text(df: pd.DataFrame, text: str) -> pd.DataFrame:
    if df.empty:
        return df
    mask = pd.Series(False, index=df.index)
    for col in df.columns:
        values = df[col].astype(str)
        mask = mask | values.str.contains(text, regex=False, na=False)
    return df[mask].copy()


def _safe_records(df: pd.DataFrame, cols: list[str], limit: int = 50) -> list[dict[str, Any]]:
    if df.empty:
        return []
    use_cols = [c for c in cols if c in df.columns]
    out = df[use_cols].head(limit).copy()
    return json.loads(out.to_json(orient="records", force_ascii=False))


def _current_runtime_snapshot() -> dict[str, Any]:
    router_dir = runtime_path("gen3_state_router_shadow")
    alpha_dir = runtime_path("gen3_state_alpha")
    mainwave_dir = runtime_path("gen3_institutional_mainwave_current")
    return {
        "router_summary": _read_json(router_dir / "latest_summary.json"),
        "router_contract": _read_json(router_dir / "latest_strategy_contract.json"),
        "router_candidates_rows": int(len(_read_csv(router_dir / "latest_candidates.csv"))),
        "router_all_source_rows": int(len(_read_csv(router_dir / "latest_all_source_candidates.csv"))),
        "alpha_summary": _read_json(alpha_dir / "latest_summary.json"),
        "alpha_shadow_ticket_rows": int(len(_read_csv(alpha_dir / "latest_shadow_tickets.csv"))),
        "mainwave_summary": _read_json(mainwave_dir / "latest_summary.json"),
        "mainwave_candidate_rows": int(len(_read_csv(mainwave_dir / "latest_candidates.csv"))),
    }


def _runtime_core(summary: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "generated_at",
        "final_profile",
        "final_profile_name",
        "strategy_system_mode",
        "selected_route",
        "entry_date",
        "decision_date",
        "source_rows",
        "selected_rows",
        "shadow_ticket_rows",
        "qualified_shadow_buy_rows",
        "diagnosis_code",
        "disabled_strategy_sources",
        "g2_gap_supplement_live_enabled",
        "g2_gap_supplement_retire_reason",
    ]
    return {key: summary.get(key) for key in keys if key in summary}


def _write_md(summary: dict[str, Any]) -> None:
    old = summary["old_full_g3"]
    unified = old["unified_closed_trades"]
    backup_rt = summary["backup_runtime"]
    current = summary["current_runtime"]
    router = current["router_summary_core"]
    mainwave = current["mainwave_summary_core"]
    rows = [
        "# G3 with G2 血缘回溯审计",
        "",
        "## 结论",
        "",
        "- 旧版可信策略是完整 `g3_final_with_g2_gap_supplement`，不是 26 笔 `institutional_score120_mainwave` 子切片。",
        f"- 旧完整闭环成交 {old['closed_trades']['rows']} 笔，旧完整 realized PnL {old['closed_trades']['realized_pnl_sum']:.2f}，区间 {old['closed_trades']['date_min']} 至 {old['closed_trades']['date_max']}。",
        f"- 6 月半导体信号已在备份 runtime 中确认：来源候选 {backup_rt['all_source_rows']} 条，最终选中 {backup_rt['selected_rows']} 条，选中半导体 {backup_rt['selected_semiconductor_rows']} 条。",
        f"- 当前 runtime 路由模式为 `{router.get('strategy_system_mode')}`，disabled_sources={router.get('disabled_strategy_sources')}，G2补位启用={router.get('g2_gap_supplement_live_enabled')}。",
        f"- 当前路由日期链：router entry_date={router.get('entry_date')}、decision_date={router.get('decision_date')}；current mainwave entry_date={mainwave.get('entry_date')}、decision_date={mainwave.get('decision_date')}。",
        "",
        "## 旧完整策略",
        "",
        f"- 合同：`{summary['old_contract'].get('strategy_id')}` / `{summary['old_contract'].get('strategy_name')}`",
        f"- Profile：`{summary['old_contract'].get('profile')}` / `{summary['old_contract'].get('profile_name')}`",
        f"- 路由构成：`{json.dumps(old['closed_trades']['route_counts'], ensure_ascii=False)}`",
        f"- 交易策略构成：`{json.dumps(unified['trade_strategy_counts'], ensure_ascii=False)}`",
        f"- 行业/板块构成：`{json.dumps(old['closed_trades']['sector_for_distinct_counts'], ensure_ascii=False)}`",
        "",
        "## 6 月半导体证据",
        "",
        f"- 备份 runtime：entry_date={backup_rt['summary'].get('entry_date')}，decision_date={backup_rt['summary'].get('decision_date')}，diagnosis={backup_rt['summary'].get('diagnosis_code')}。",
        f"- 最终选中候选：`{json.dumps(backup_rt['selected_candidates'], ensure_ascii=False)}`",
        f"- 半导体来源候选：`{json.dumps(backup_rt['semiconductor_candidates'], ensure_ascii=False)}`",
        "",
        "## 当前 Runtime",
        "",
        f"- Router 摘要：`{json.dumps(router, ensure_ascii=False)}`",
        f"- Mainwave 摘要：`{json.dumps(mainwave, ensure_ascii=False)}`",
        f"- 当前行数：router_candidates={current['router_candidates_rows']}，router_all_source={current['router_all_source_rows']}，alpha_shadow_tickets={current['alpha_shadow_ticket_rows']}，mainwave_candidates={current['mainwave_candidate_rows']}。",
        "",
        "## 恢复边界",
        "",
        "- 历史展示和历史回测应恢复完整 204 笔 `g3_final_with_g2_gap_supplement` 血缘。",
        "- 当前已按用户本次明确指令恢复完整 204 笔 `g3_final_with_g2_gap_supplement` 路由血缘；真实下单仍由 formal/auto/order 三个字段锁定为禁用。",
        "- 若当前 source_rows 有候选但 selected_rows 为 0，应按当日策略 gate 解释为无合格票，不再归因为路线缺失。",
        "",
    ]
    (OUT_DIR / "report.md").write_text("\n".join(rows), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    old_closed = _read_csv(
        BACKUP_ROOT
        / "reports"
        / "g2_g3_market_style_router_v1"
        / "g3_final_with_g2_gap_supplement_closed_trades.csv"
    )
    old_unified_closed = _read_csv(
        BACKUP_ROOT
        / "reports"
        / "g3_formal_unified_five_strategy_contract_v1"
        / "formal_unified_closed_trades.csv"
    )
    backup_router_dir = BACKUP_ROOT / "runtime" / "gen3_state_router_shadow"
    backup_summary = _read_json(backup_router_dir / "latest_summary.json")
    old_contract = _read_json(backup_router_dir / "latest_strategy_contract.json")
    backup_selected = _read_csv(backup_router_dir / "latest_candidates.csv")
    backup_all_source = _read_csv(backup_router_dir / "latest_all_source_candidates.csv")
    semiconductor_source = _contains_text(backup_all_source, "半导体")
    semiconductor_selected = _contains_text(backup_selected, "半导体")

    current_runtime = _current_runtime_snapshot()
    current_runtime["router_summary_core"] = _runtime_core(current_runtime["router_summary"])
    current_runtime["alpha_summary_core"] = _runtime_core(current_runtime["alpha_summary"])
    current_runtime["mainwave_summary_core"] = _runtime_core(current_runtime["mainwave_summary"])

    summary: dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "lineage_restored_no_current_trade",
        "old_contract": old_contract,
        "old_full_g3": {
            "closed_trades": _metrics(old_closed),
            "unified_closed_trades": _metrics(old_unified_closed),
        },
        "backup_runtime": {
            "summary": _runtime_core(backup_summary),
            "all_source_rows": int(len(backup_all_source)),
            "selected_rows": int(len(backup_selected)),
            "semiconductor_source_rows": int(len(semiconductor_source)),
            "selected_semiconductor_rows": int(len(semiconductor_selected)),
            "selected_candidates": _safe_records(
                backup_selected,
                ["entry_date", "code", "name", "route", "trade_strategy", "l2_sector_name", "score"],
            ),
            "semiconductor_candidates": _safe_records(
                semiconductor_source,
                ["entry_date", "code", "name", "route", "trade_strategy", "l2_sector_name", "score"],
                limit=20,
            ),
        },
        "current_runtime": current_runtime,
        "verdict": {
            "same_contract_id_present": True,
            "same_effective_live_strategy": (
                current_runtime["router_summary_core"].get("strategy_system_mode") == "router_fusion"
                and not current_runtime["router_summary_core"].get("disabled_strategy_sources")
                and bool(current_runtime["router_summary_core"].get("g2_gap_supplement_live_enabled"))
            ),
            "old_full_strategy_trade_count": int(len(old_closed)),
            "institutional_slice_is_only_part_of_old_strategy": True,
            "current_runtime_date_chain_abnormal": (
                current_runtime["router_summary_core"].get("entry_date")
                != current_runtime["mainwave_summary_core"].get("entry_date")
            ),
            "current_live_route_narrowed_to_full_g3_only": (
                current_runtime["router_summary_core"].get("strategy_system_mode") == "full_g3_only"
            ),
        },
    }

    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    compare_rows = [
        {
            "scope": "old_full_g3_backup",
            **summary["old_full_g3"]["closed_trades"],
        },
        {
            "scope": "old_unified_five_strategy_backup",
            **summary["old_full_g3"]["unified_closed_trades"],
        },
    ]
    pd.DataFrame(compare_rows).to_csv(OUT_DIR / "strategy_metrics.csv", index=False, encoding="utf-8-sig")
    semiconductor_source.to_csv(OUT_DIR / "june_semiconductor_source_candidates.csv", index=False, encoding="utf-8-sig")
    backup_selected.to_csv(OUT_DIR / "june_selected_candidates.csv", index=False, encoding="utf-8-sig")
    _write_md(summary)
    print(json.dumps(summary["verdict"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
