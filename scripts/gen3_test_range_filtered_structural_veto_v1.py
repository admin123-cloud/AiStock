from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_audit_guarded_execution_stress_v1 import (  # noqa: E402
    STRESS_SPECS,
    apply_stress,
    load_daily_ohlc,
    prepare_daily_maps,
    route_attribution,
    simulate,
    summarize,
    summarize_windows,
)
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_build_dynamic_router_combo_v1 import ROUTE_PRIORITY  # noqa: E402
from scripts.gen3_build_dynamic_router_guarded_v1 import standardize_strong_guarded  # noqa: E402


OUT_DIR = ROOT / "reports" / "gen3_range_filtered_structural_veto_v1"
PANIC_SOURCE = (
    ROOT
    / "reports"
    / "gen3_panic_v2_research"
    / "final_candidate_v1"
    / "m30_close5_full_nextopen_cost30_closed_trades.csv"
)
RANGE_SOURCE = (
    ROOT
    / "reports"
    / "gen3_range_v3_mtm_pressure_v1"
    / "range_v3_weak_low_not_chasing_h5_cost30_closed_trades.csv"
)

STRONG_GUARD = {
    "name": "strong_breadth_score_volume5_guard",
    "desc": "strong_main guarded volume5",
    "market_breadth_min": 0.56,
    "g3_strong_score_min": 0.626,
    "score_volume5_min": 0.70,
}

VARIANTS = [
    {
        "name": "base_range_conservative_combo_b",
        "desc_cn": "当前保守横盘组合基准。",
    },
    {
        "name": "range_no_weak_trend",
        "desc_cn": "横盘链路过滤 adx_layer=weak_trend_strength，避免弱趋势未修复。",
        "range_no_weak_trend": True,
    },
    {
        "name": "range_box_width_le60",
        "desc_cn": "横盘链路过滤 box_width60>0.60，避免箱体太宽、波动过大。",
        "range_box_width_le": 0.60,
    },
    {
        "name": "range_close_position_ge80",
        "desc_cn": "横盘链路要求 close_position>=0.80，只留收盘位置较强的修复。",
        "range_close_position_ge": 0.80,
    },
    {
        "name": "down_no_overheated_volume",
        "desc_cn": "恐慌链路过滤 g3_volume_context=overheated_volume，避免放量出货语义。",
        "down_no_overheated_volume": True,
    },
    {
        "name": "down_no_unclear_repair",
        "desc_cn": "恐慌链路过滤 g3_repair_env_label=neutral_or_unclassified，避免修复环境不清晰。",
        "down_no_unclear_repair": True,
    },
    {
        "name": "down_no_overheated_unclear",
        "desc_cn": "恐慌链路同时过滤过热量能和修复环境不清晰。",
        "down_no_overheated_volume": True,
        "down_no_unclear_repair": True,
    },
    {
        "name": "struct_veto_combo_a",
        "desc_cn": "组合结构 veto：横盘过滤弱趋势和宽箱体，恐慌过滤过热量能和不清晰修复。",
        "range_no_weak_trend": True,
        "range_box_width_le": 0.60,
        "down_no_overheated_volume": True,
        "down_no_unclear_repair": True,
    },
    {
        "name": "struct_veto_combo_b",
        "desc_cn": "组合结构 veto：横盘要求收盘位置强，恐慌过滤过热量能和不清晰修复。",
        "range_close_position_ge": 0.80,
        "down_no_overheated_volume": True,
        "down_no_unclear_repair": True,
    },
    {
        "name": "struct_veto_combo_c",
        "desc_cn": "组合结构 veto：横盘过滤弱趋势并要求收盘位置强，恐慌过滤过热量能和不清晰修复。",
        "range_no_weak_trend": True,
        "range_close_position_ge": 0.80,
        "down_no_overheated_volume": True,
        "down_no_unclear_repair": True,
    },
    {
        "name": "struct_veto_combo_d",
        "desc_cn": "组合结构 veto：横盘过滤宽箱体并要求收盘位置强，恐慌过滤过热量能和不清晰修复。",
        "range_box_width_le": 0.60,
        "range_close_position_ge": 0.80,
        "down_no_overheated_volume": True,
        "down_no_unclear_repair": True,
    },
    {
        "name": "struct_veto_combo_e",
        "desc_cn": "组合结构 veto：在 combo_c 基础上，恐慌链路再过滤 strong_capitulation/strong_clearance。",
        "range_no_weak_trend": True,
        "range_close_position_ge": 0.80,
        "down_no_overheated_volume": True,
        "down_no_unclear_repair": True,
        "down_no_strong_clearance": True,
    },
]


def _date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def _standardize_panic_filtered(spec: dict) -> pd.DataFrame:
    d = pd.read_csv(PANIC_SOURCE, low_memory=False)
    d["entry_date"] = _date(d["entry_date"])
    d["policy_exit_date"] = _date(d["policy_exit_date"])
    d["code"] = d["code"].astype(str)
    mask = pd.Series(True, index=d.index)
    if spec.get("down_no_overheated_volume") and "g3_volume_context" in d.columns:
        mask &= ~d["g3_volume_context"].astype(str).eq("overheated_volume")
    if spec.get("down_no_unclear_repair") and "g3_repair_env_label" in d.columns:
        mask &= ~d["g3_repair_env_label"].astype(str).eq("neutral_or_unclassified")
    if spec.get("down_no_strong_clearance"):
        if "g3_capitulation_strength" in d.columns:
            mask &= ~d["g3_capitulation_strength"].astype(str).eq("strong_capitulation")
        if "g3_repair_env_label" in d.columns:
            mask &= ~d["g3_repair_env_label"].astype(str).eq("strong_clearance")
    d = d[mask].copy()

    out = pd.DataFrame()
    out["entry_date"] = d["entry_date"]
    out["policy_exit_date"] = d["policy_exit_date"]
    out["code"] = d["code"]
    out["name"] = d.get("name", "")
    out["route"] = "down_panic"
    out["route_source"] = "panic_v2_m30_close5_full_nextopen"
    out["route_priority"] = ROUTE_PRIORITY["down_panic"]
    out["score"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_price_adjusted", d.get("entry_price")), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("policy_net_ret", d.get("net_ret")), errors="coerce")
    return out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _standardize_range_filtered(spec: dict) -> pd.DataFrame:
    d = pd.read_csv(RANGE_SOURCE, low_memory=False)
    d["entry_date"] = _date(d["entry_date"])
    d["policy_exit_date"] = _date(d["policy_exit_date"])
    d["code"] = d["code"].astype(str)
    for col in ["gap_open", "box_width60", "close_position", "range_v3_score", "entry_open", "policy_net_ret"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    mask = pd.Series(True, index=d.index)
    mask &= ~d.get("adx_layer", "").astype(str).eq("downtrend_strength")
    gap = pd.to_numeric(d.get("gap_open"), errors="coerce")
    mask &= ~((gap > 0.0) & (gap <= 0.02))
    if spec.get("range_no_weak_trend"):
        mask &= ~d.get("adx_layer", "").astype(str).eq("weak_trend_strength")
    if "range_box_width_le" in spec and "box_width60" in d.columns:
        mask &= d["box_width60"].isna() | (d["box_width60"] <= float(spec["range_box_width_le"]))
    if "range_close_position_ge" in spec and "close_position" in d.columns:
        mask &= d["close_position"].isna() | (d["close_position"] >= float(spec["range_close_position_ge"]))
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
            _standardize_panic_filtered(spec),
            _standardize_range_filtered(spec),
            standardize_strong_guarded(STRONG_GUARD),
        ],
        ignore_index=True,
    )
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def _pct(v: object) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def _md(df: pd.DataFrame, pct_cols: set[str]) -> str:
    if df.empty:
        return "_无数据_"
    out = df.copy()
    for col in pct_cols:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    for col in out.columns:
        if col not in pct_cols and pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else f"{v:.4f}")
    return out.to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_candidates = {spec["name"]: _candidate_set(spec) for spec in VARIANTS}
    union = pd.concat(all_candidates.values(), ignore_index=True)
    daily = load_daily_ohlc(union)
    daily_map = prepare_daily_maps(daily, union)
    calendar = _trade_calendar(union["entry_date"].min(), union["policy_exit_date"].max() + pd.Timedelta(days=20))
    close_map = {(str(r.code), pd.Timestamp(r.trade_date).normalize()): float(r.close) for r in daily.itertuples(index=False)}

    summary_rows = []
    window_rows = []
    route_parts = []
    for spec in VARIANTS:
        name = spec["name"]
        candidates = all_candidates[name]
        candidates.to_csv(OUT_DIR / f"{name}_candidates.csv", index=False, encoding="utf-8-sig")
        for stress_spec in STRESS_SPECS:
            stressed = apply_stress(candidates, stress_spec, calendar, daily_map)
            curve, closed = simulate(stressed, calendar, close_map)
            profile = stress_spec["profile"]
            if profile in {"close_100bps", "nextopen_haircut2_30bps"}:
                closed.to_csv(OUT_DIR / f"{name}_{profile}_closed_trades.csv", index=False, encoding="utf-8-sig")
            row = summarize(curve, closed, profile)
            row["variant"] = name
            row["desc_cn"] = spec["desc_cn"]
            row["candidate_count"] = int(len(candidates))
            summary_rows.append(row)
            window_rows.extend([{**w, "variant": name} for w in summarize_windows(curve, closed, profile)])
            route = route_attribution(closed, profile)
            route.insert(0, "variant", name)
            route_parts.append(route)

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    routes = pd.concat(route_parts, ignore_index=True)
    summary.to_csv(OUT_DIR / "structural_veto_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "structural_veto_windows.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "structural_veto_route_attribution.csv", index=False, encoding="utf-8-sig")

    focus = summary[summary["profile"].isin(["close_100bps", "nextopen_haircut2_30bps"])].copy()
    close100 = focus[focus["profile"].eq("close_100bps")].sort_values(["total_return", "max_drawdown"], ascending=[False, False]).head(5)
    haircut = focus[focus["profile"].eq("nextopen_haircut2_30bps")].sort_values(["total_return", "max_drawdown"], ascending=[False, False]).head(5)
    best_haircut = haircut.iloc[0].to_dict()

    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "variant_count": len(VARIANTS),
        "best_haircut_variant": best_haircut["variant"],
        "best_haircut_total_return": best_haircut["total_return"],
        "best_haircut_max_drawdown": best_haircut["max_drawdown"],
        "next_step": "decide_whether_structural_veto_is_worth_promoting",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 range-filtered 结构 veto 复验 V1

生成时间：{meta["generated_at"]}

## 英文名解释

- `range_gap`：横盘/弱反弹链路，寻找箱体底部或弱反弹修复机会。
- `down_panic`：弱势恐慌链路，寻找非理性出清后的修复机会。
- `structural_veto`：结构否决，不用 score/rank，而是用趋势层、箱体宽度、量能语义、修复环境等可解释字段剔除风险样本。
- `nextopen_haircut2_30bps`：最严苛执行压力，次日开盘退出再额外扣 2%。

## 100bps 前五

{_md(close100, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"})}

## 极端冲击前五

{_md(haircut, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"})}

## 判断

本轮只验证结构 veto，不调 score/rank。如果某个结构 veto 能让 `nextopen_haircut2_30bps` 从负收益明显改善，同时 100bps 不被大幅削弱，才值得继续。
"""
    (OUT_DIR / "structural_veto_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
