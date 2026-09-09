from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402


FORMAL = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
FORMAL_DISPATCH = report_path("g3_formal_five_strategy_dispatch_contract_v1", "formal_trades_with_dispatch_strategy.csv")
SCHEDULER = report_path("wave_style_model_scheduler_v1", "scheduler_focus_240d_score120_aggr25", "closed_trades.csv")
ROUTER_BACKUP = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_market_state_router_strategy_v1\g3_route_execution_mandate_candidate_closed_trades.csv"
)
OLD_PROXY = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_pre_2024_10_state_alpha_closure_v5\institutional_mainwave_candidates.csv"
)
OUT_DIR = report_path("score120_source_lineage_audit_v1")


def _key(df: pd.DataFrame) -> pd.Series:
    date = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    code = df["code"].fillna("").astype(str).str.extract(r"(\d{6})", expand=False).fillna("")
    return date + "|" + code


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    df["trade_key_norm"] = _key(df)
    return df


def _return_map(df: pd.DataFrame) -> pd.Series:
    col = next((c for c in ["policy_net_ret", "net_ret", "stress_net_ret"] if c in df.columns), None)
    return pd.to_numeric(df.set_index("trade_key_norm")[col], errors="coerce") if col else pd.Series(dtype=float)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    formal = _read(FORMAL)
    dispatch = _read(FORMAL_DISPATCH)
    dispatch = dispatch[dispatch.get("trade_strategy", pd.Series("", index=dispatch.index)).eq("institutional_score120_mainwave")].copy()
    scheduler = _read(SCHEDULER)
    proxy = _read(OLD_PROXY)
    router = _read(ROUTER_BACKUP)
    router = router[router.get("mode", pd.Series("", index=router.index)).eq("institutional_mainwave")].copy()

    formal_keys = set(formal["trade_key_norm"])
    source_rows = []
    for name, path, df in [
        ("formal_dispatch_filtered", FORMAL_DISPATCH, dispatch),
        ("scheduler_direct_output", SCHEDULER, scheduler),
        ("state_router_backup_institutional", ROUTER_BACKUP, router),
        ("old_proxy_labeled_scheduler", OLD_PROXY, proxy),
    ]:
        overlap = set(df["trade_key_norm"]) & formal_keys
        source_rows.append({"source": name, "path": str(path), "rows": int(len(df)), "formal26_overlap": int(len(overlap))})

    detail = formal[["trade_key_norm", "entry_date", "code", "name", "net_ret", "wave_style_score", "sector_diffusion_score"]].copy()
    detail = detail.rename(columns={"net_ret": "formal_net_ret"})
    for label, df in [("dispatch", dispatch), ("scheduler", scheduler), ("router", router), ("old_proxy", proxy)]:
        keys = set(df["trade_key_norm"])
        detail[f"in_{label}"] = detail["trade_key_norm"].isin(keys)
        returns = _return_map(df)
        detail[f"{label}_net_ret"] = detail["trade_key_norm"].map(returns)
    detail["dispatch_return_matches_formal"] = (detail["dispatch_net_ret"] - detail["formal_net_ret"]).abs().lt(1e-9)
    detail["proxy_source_label"] = detail["trade_key_norm"].map(
        proxy.set_index("trade_key_norm").get("source_label", pd.Series(dtype=str))
    )
    detail["proxy_selected_score"] = detail["trade_key_norm"].map(
        pd.to_numeric(proxy.set_index("trade_key_norm").get("selected_score", pd.Series(dtype=float)), errors="coerce")
    )
    detail["lineage_status"] = detail.apply(
        lambda r: "formal_dispatch_exact; scheduler_direct_not_matched; proxy_label_not_proof_of_source"
        if bool(r["in_dispatch"]) and not bool(r["in_scheduler"])
        else "requires_manual_trace",
        axis=1,
    )

    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "formal_rows": int(len(formal)),
        "formal_dispatch_overlap": int(detail["in_dispatch"].sum()),
        "formal_dispatch_return_match": int(detail["dispatch_return_matches_formal"].sum()),
        "direct_scheduler_overlap": int(detail["in_scheduler"].sum()),
        "state_router_backup_overlap": int(detail["in_router"].sum()),
        "old_proxy_overlap": int(detail["in_old_proxy"].sum()),
        "verdict": "formal26 is a downstream formal-dispatch extract; saved proxy selected_score has no direct scheduler trade-key recall and must not be treated as a replayable entry rule",
    }
    pd.DataFrame(source_rows).to_csv(OUT_DIR / "source_overlap_summary.csv", index=False, encoding="utf-8-sig")
    detail.to_csv(OUT_DIR / "formal26_lineage_detail.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Score120 正式主升来源血缘审计 v1", "", "## 结论", "",
        f"- 正式26笔与正式调度源逐笔重合 {summary['formal_dispatch_overlap']}/{summary['formal_rows']}，收益也逐笔一致 {summary['formal_dispatch_return_match']}/{summary['formal_rows']}。",
        f"- 与 `scheduler_focus_240d_score120_aggr25` 的直接成交重合为 {summary['direct_scheduler_overlap']}/{summary['formal_rows']}。",
        f"- 旧代理虽覆盖 {summary['old_proxy_overlap']}/{summary['formal_rows']}，但其 `wave_scheduler_score120` 标签不能证明来源；它是后续状态路由加工后的字段拼接。",
        "- 因此 `selected_score` 只能作为历史辅助标签，不能当作正式26笔的可重放选股门槛。", "",
        "## 重建边界", "",
        "- 可重放：正式调度的结果、退出合同和收益归因。",
        "- 不可重放：正式26笔的原始候选全截面、直接生成规则及旧TDX行业扩散。",
        "- 下一阶段必须从统一K线重建一个新候选生成器，并以正式26笔做召回验证；不得声称在复刻旧规则。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
