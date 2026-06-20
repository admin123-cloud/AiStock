from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.analyze_g3_profitable_signal_recall_v1 import (  # noqa: E402
    OUT_DIR as BASE_RECALL_DIR,
    _attach_recall,
    _group_summary,
    _md_table,
    _read_historical_profitable,
    _summary_row,
    _trade_date_index,
)
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_native_source_bridge_recall_v1")

STRATEGY_LABELS = {
    "institutional_score120_mainwave": "机构主升Score120",
    "old_g3_strong_breakout": "强势突破",
    "volume_runup_supplement": "量能续强补位",
    "range_weak_repair": "震荡弱势修复",
    "panic_capitulation_repair": "恐慌出清修复",
}


def _norm_code(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip().str.upper()


def _read_source(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, low_memory=False)


def _standardize(
    df: pd.DataFrame,
    source_name: str,
    trade_strategy: str,
    source_strategy_label: str,
    name_col: str = "name",
) -> pd.DataFrame:
    if df.empty or "code" not in df.columns or "entry_date" not in df.columns:
        return pd.DataFrame()
    out = pd.DataFrame()
    out["code"] = _norm_code(df["code"])
    out["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    out["name"] = df[name_col] if name_col in df.columns else df.get("stock_name", "")
    out["trade_strategy"] = trade_strategy
    out["trade_strategy_label"] = STRATEGY_LABELS[trade_strategy]
    out["source_strategy_label"] = source_strategy_label
    out["native_source_file"] = source_name
    score_col = next((c for c in ["score", "selected_score", "wave_style_score", "candidate_score", "v4_score"] if c in df.columns), None)
    out["strategy_score"] = pd.to_numeric(df[score_col], errors="coerce") if score_col else 0.0
    out = out[out["entry_date"].notna() & out["code"].ne("")].copy()
    out["entry_date"] = out["entry_date"].dt.date.astype(str)
    return out


def _route_package_candidates(path: Path) -> pd.DataFrame:
    df = _read_source(path)
    if df.empty:
        return pd.DataFrame()
    route = df.get("route", pd.Series("", index=df.index)).fillna("").astype(str)
    parts = []
    mapping = {
        "strong_main": ("old_g3_strong_breakout", "旧G3强势突破原生源"),
        "range_gap": ("range_weak_repair", "旧G3弱势/震荡修复原生源"),
        "down_panic": ("panic_capitulation_repair", "旧G3恐慌修复原生源"),
    }
    for route_key, (strategy, label) in mapping.items():
        part = df[route.eq(route_key)].copy()
        parts.append(_standardize(part, "route_execution_mandate_v3", strategy, label))
    return pd.concat([p for p in parts if not p.empty], ignore_index=True) if parts else pd.DataFrame()


def _build_native_bridge_candidates() -> pd.DataFrame:
    root = Path(r"F:\Stock\AiStockResearchArchive\reports")
    sources = []
    sources.append(
        _standardize(
            _read_source(root / "score120_sector_diffusion_30m_overlay_v1" / "base_trades_with_sector_diffusion_30m.csv"),
            "score120_sector_diffusion_30m_overlay_v1",
            "institutional_score120_mainwave",
            "Score120主升+主线扩散+30m原生源",
            name_col="stock_name",
        )
    )
    sources.append(
        _route_package_candidates(root / "gen3_route_execution_mandate_candidate_package_v3" / "g3_route_execution_mandate_candidate_closed_trades.csv")
    )
    sources.append(
        _standardize(
            _read_source(root / "gen3_range_v3_mtm_pressure_v1" / "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv"),
            "gen3_range_v3_mtm_pressure_v1",
            "range_weak_repair",
            "Range V3弱势低吸原生源",
        )
    )
    sources.append(
        _standardize(
            _read_source(root / "gen3_panic_v2_research" / "final_candidate_v1" / "m30_close5_full_nextopen_cost30_closed_trades.csv"),
            "gen3_panic_v2_research_final_candidate_v1",
            "panic_capitulation_repair",
            "Panic V2 30m恐慌修复原生源",
        )
    )
    sources.append(
        _standardize(
            _read_source(root / "gen2_alpha191_light_constraint_matrix" / "sources" / "volume5_keep80_runup_le100.parquet"),
            "gen2_alpha191_volume5_keep80",
            "volume_runup_supplement",
            "G2量能续强/Alpha191原生源",
        )
    )
    candidates = pd.concat([s for s in sources if not s.empty], ignore_index=True)
    if candidates.empty:
        return candidates
    candidates = candidates.sort_values(["entry_date", "code", "trade_strategy", "strategy_score"], ascending=[True, True, True, False])
    candidates = candidates.drop_duplicates(["entry_date", "code", "trade_strategy"], keep="first").reset_index(drop=True)
    return candidates


def _read_daily_recall_detail() -> pd.DataFrame:
    path = BASE_RECALL_DIR / "profitable_trade_recall_detail.csv"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def _write_report(summary: pd.DataFrame, by_strategy: pd.DataFrame, by_source: pd.DataFrame, bridge: pd.DataFrame, compare: pd.DataFrame) -> None:
    text = f"""# G3 原生信号源挂回五策略召回审计

## 结论

把策略主体减少到 5 个是可以保留的，但五策略必须是“统一交易合同与归因标签”，不能替代原生买点生成器。本次把 Score120、旧G3路由包、Range V3、Panic V2、G2量能续强等现存原生源挂回五策略后，重新检查历史盈利样本召回。

## 原生桥接总体召回

{_md_table(summary, pct_cols={"avg_net_ret", "candidate_exact_rate", "candidate_within_1_trade_day_rate", "candidate_within_3_trade_days_rate"}, money_cols={"realized_pnl"})}

## 与当前日线代理对比

{_md_table(compare, pct_cols={"daily_proxy_selected_3d_rate", "daily_proxy_candidate_3d_rate", "native_bridge_3d_rate"}, money_cols={"realized_pnl"})}

## 按统一策略

{_md_table(by_strategy, pct_cols={"avg_net_ret", "candidate_exact_rate", "candidate_within_1_trade_day_rate", "candidate_within_3_trade_days_rate"}, money_cols={"realized_pnl"})}

## 按原生源

{_md_table(by_source, pct_cols={"avg_net_ret", "candidate_exact_rate", "candidate_within_1_trade_day_rate", "candidate_within_3_trade_days_rate"}, money_cols={"realized_pnl"})}

## 执行判断

1. 若原生桥接召回明显高于日线代理，收益跑丢的主因就是“原生信号源被替换成了弱代理”，不是策略数量减少本身。
2. 下一步完整回测应以这些原生源重新生成候选，再统一进入 5 个策略合同、2槽路由和卖出规则。
3. 修复类虽然召回高，但需要重新约束 30m确认、结构止损与市场压力分层；进攻/补位类优先恢复信号源召回。

候选桥接明细：`native_bridge_candidates.csv`，共 {len(bridge)} 条。
"""
    (OUT_DIR / "REPORT_CN.md").write_text(text, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profitable = _read_historical_profitable()
    bridge = _build_native_bridge_candidates()
    if bridge.empty:
        raise RuntimeError("No native bridge candidates found.")
    date_to_idx = _trade_date_index(profitable["entry_date"])
    recalled = _attach_recall(profitable, bridge, "candidate", date_to_idx)

    summary = pd.DataFrame([_summary_row(recalled, "native_bridge")])
    by_strategy = _group_summary(recalled, ["trade_strategy", "trade_strategy_label"])
    by_source = _group_summary(recalled, ["trade_strategy_label", "candidate_nearest_source_strategy_label"])

    daily = _read_daily_recall_detail()
    if not daily.empty:
        compare = []
        for (strategy, label), gdf in recalled.groupby(["trade_strategy", "trade_strategy_label"], dropna=False):
            ddf = daily[daily["trade_strategy"].astype(str).eq(str(strategy))]
            compare.append(
                {
                    "trade_strategy_label": label,
                    "profitable_trades": len(gdf),
                    "realized_pnl": pd.to_numeric(gdf["realized_pnl"], errors="coerce").sum(),
                    "daily_proxy_candidate_3d_rate": float(ddf["candidate_hit_3d"].mean()) if len(ddf) else 0.0,
                    "daily_proxy_selected_3d_rate": float(ddf["selected_hit_3d"].mean()) if len(ddf) else 0.0,
                    "native_bridge_3d_rate": float(gdf["candidate_hit_3d"].mean()) if len(gdf) else 0.0,
                }
            )
        compare_df = pd.DataFrame(compare).sort_values("realized_pnl", ascending=False)
    else:
        compare_df = pd.DataFrame()

    bridge.to_csv(OUT_DIR / "native_bridge_candidates.csv", index=False, encoding="utf-8-sig")
    recalled.to_csv(OUT_DIR / "native_bridge_recall_detail.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "native_bridge_recall_summary.csv", index=False, encoding="utf-8-sig")
    by_strategy.to_csv(OUT_DIR / "native_bridge_recall_by_strategy.csv", index=False, encoding="utf-8-sig")
    by_source.to_csv(OUT_DIR / "native_bridge_recall_by_source.csv", index=False, encoding="utf-8-sig")
    compare_df.to_csv(OUT_DIR / "native_bridge_vs_daily_proxy.csv", index=False, encoding="utf-8-sig")
    summary_json: dict[str, Any] = {
        "native_bridge_candidates": int(len(bridge)),
        "profitable_trades": int(len(profitable)),
        "overall": summary.iloc[0].to_dict(),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary_json, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary, by_strategy, by_source, bridge, compare_df)
    print(json.dumps(summary_json, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
