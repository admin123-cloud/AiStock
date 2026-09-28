from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_build_dynamic_router_combo_v1 import (  # noqa: E402
    ROUTE_PRIORITY,
    SOURCE_COST_BPS,
    md_table,
    pct,
    simulate,
    standardize_panic,
    summarize,
    summarize_routes,
    summarize_windows,
)
from scripts.gen3_build_dynamic_router_guarded_v1 import standardize_strong_guarded  # noqa: E402

from utils.paths import report_path  # noqa: E402

OUT_DIR = report_path("gen3_combo_range_filter_v1")
RANGE_SOURCE = report_path("gen3_range_v3_mtm_pressure_v1", "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv")

STRONG_GUARD = {
    "name": "strong_breadth_score_volume5_guard",
    "desc": "strong_main 要求广度、强势分数，并保留 G2 volume5 高分确认。",
    "market_breadth_min": 0.56,
    "g3_strong_score_min": 0.626,
    "score_volume5_min": 0.70,
}

RANGE_FILTERS = [
    {
        "name": "base_range_gap",
        "desc": "不额外过滤 range_gap。",
    },
    {
        "name": "range_no_adx_downtrend",
        "desc": "过滤 adx_layer=downtrend_strength 的弱反弹缺口。",
        "no_adx_downtrend": True,
    },
    {
        "name": "range_no_small_positive_gap",
        "desc": "过滤 0 < gap_open <= 2% 的小幅高开。",
        "no_small_positive_gap": True,
    },
    {
        "name": "range_conservative_combo_b",
        "desc": "过滤 adx_layer=downtrend_strength，且过滤 0 < gap_open <= 2%。",
        "no_adx_downtrend": True,
        "no_small_positive_gap": True,
    },
]


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def _standardize_range_filtered(spec: dict) -> pd.DataFrame:
    d = pd.read_csv(RANGE_SOURCE, low_memory=False)
    d["entry_date"] = _date(d["entry_date"])
    d["policy_exit_date"] = _date(d["policy_exit_date"])
    d["code"] = d["code"].astype(str)
    for col in ["gap_open", "range_v3_score", "entry_open", "policy_net_ret"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    mask = pd.Series(True, index=d.index)
    if spec.get("no_adx_downtrend"):
        mask &= ~d.get("adx_layer", "").astype(str).eq("downtrend_strength")
    if spec.get("no_small_positive_gap"):
        gap = pd.to_numeric(d.get("gap_open"), errors="coerce")
        mask &= ~((gap > 0.0) & (gap <= 0.02))
    d = d[mask].copy()

    out = pd.DataFrame()
    out["entry_date"] = d["entry_date"]
    out["policy_exit_date"] = d["policy_exit_date"]
    out["code"] = d["code"]
    out["name"] = d.get("name", "")
    out["route"] = "range_gap"
    out["route_source"] = spec["name"]
    out["route_priority"] = ROUTE_PRIORITY["range_gap"]
    out["score"] = pd.to_numeric(d.get("range_v3_score", d.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_open"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("policy_net_ret"), errors="coerce")
    return out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _candidate_set(spec: dict) -> pd.DataFrame:
    d = pd.concat(
        [
            standardize_panic(),
            _standardize_range_filtered(spec),
            standardize_strong_guarded(STRONG_GUARD),
        ],
        ignore_index=True,
    )
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"]).copy()
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    window_rows = []
    route_rows = []
    for spec in RANGE_FILTERS:
        candidates = _candidate_set(spec)
        candidates.to_csv(OUT_DIR / f"{spec['name']}_candidates.csv", index=False, encoding="utf-8-sig")
        for cost in [30.0, 50.0, 100.0]:
            curve, closed = simulate(candidates, cost)
            tag = f"{spec['name']}_cost{int(cost)}"
            curve.to_csv(OUT_DIR / f"{tag}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(OUT_DIR / f"{tag}_closed_trades.csv", index=False, encoding="utf-8-sig")
            s = summarize(curve, closed, cost)
            s["variant"] = spec["name"]
            s["desc"] = spec["desc"]
            s["candidate_count"] = int(len(candidates))
            s["range_candidate_count"] = int((candidates["route"] == "range_gap").sum())
            s["strong_candidate_count"] = int((candidates["route"] == "strong_main").sum())
            summary_rows.append(s)
            w = summarize_windows(curve, closed)
            w.insert(0, "cost_bps", cost)
            w.insert(0, "variant", spec["name"])
            window_rows.append(w)
            r = summarize_routes(closed)
            r.insert(0, "cost_bps", cost)
            r.insert(0, "variant", spec["name"])
            route_rows.append(r)

    summary = pd.DataFrame(summary_rows)
    windows = pd.concat(window_rows, ignore_index=True)
    routes = pd.concat(route_rows, ignore_index=True)
    summary.to_csv(OUT_DIR / "combo_range_filter_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "combo_range_filter_windows.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "combo_range_filter_route_attribution.csv", index=False, encoding="utf-8-sig")

    best100 = summary[summary["cost_bps"].eq(100.0)].sort_values(["total_return", "max_drawdown"], ascending=[False, False]).head(1)
    best_variant = best100.iloc[0]["variant"] if not best100.empty else ""
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tested_variants": len(RANGE_FILTERS),
        "best_100bps_variant": best_variant,
        "best_100bps_total_return": float(best100.iloc[0]["total_return"]) if not best100.empty else None,
        "best_100bps_max_drawdown": float(best100.iloc[0]["max_drawdown"]) if not best100.empty else None,
        "next_step": "run_execution_stress_for_best_range_filter_variant",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 range_gap 宽口径过滤回灌组合复算 V1

生成时间：{meta["generated_at"]}

## 目的

把 `range_gap` 宽口径降噪过滤放回完整三链组合，重新跑 slot5、每日最多 2 笔、优先级 `down_panic > strong_main > range_gap` 的 MTM。

这一步验证过滤器是否真的改善组合，而不是只改善单链路表格。

## 总体结果

{md_table(summary, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"})}

## 分窗口

{md_table(windows, {"return", "max_drawdown", "win_rate"})}

## 链路贡献

{md_table(routes, {"win_rate", "avg_trade_return", "worst_trade"})}

## 阶段判断

100bps 最优变体：`{meta["best_100bps_variant"]}`，收益 `{pct(meta["best_100bps_total_return"])}`，回撤 `{pct(meta["best_100bps_max_drawdown"])}`。

下一步必须对最优变体做真实执行压力测试，尤其是 nextopen、跌停延迟和 haircut2。只有通过压力测试，才能说震荡链路被真正改善。
"""
    (OUT_DIR / "combo_range_filter_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
