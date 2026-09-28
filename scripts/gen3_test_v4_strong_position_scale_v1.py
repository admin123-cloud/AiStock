from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.gen3_build_dynamic_router_combo_v1 as router
import scripts.gen3_package_v4_research_candidate_v1 as v4pkg
import scripts.gen3_test_range_execution_margin_v1 as margin
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar
from utils.paths import report_path


PACKAGE_DIR = report_path("gen3_v4_research_package_v1")
OUT_DIR = report_path("gen3_v4_strong_position_scale_probe_v1")
INITIAL_CAPITAL = 150_000.0

PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "all_shock": 0.02},
]
VARIANT_ROUTE_LIMITS = {
    "base": None,
    "strong_score_lt_060_half": None,
    "strong_score_lt_070_half": None,
    "strong_q1_half": None,
    "strong_q1q2_half": None,
    "strong_daily_limit1": {"strong_main": 1},
    "strong_daily_limit1_q1q2_half": {"strong_main": 1},
    "strong_second_half": None,
    "strong_second_score_ge_070": None,
    "strong_second_score_ge_080": None,
    "strong_second_score_ge_090": None,
    "strong_second_score_ge_093": None,
    "strong_second_score_ge_095": None,
    "strong_second_q75": None,
}


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        item: dict[str, str] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(PACKAGE_DIR / "v4_h10_margin_candidates_standardized.csv", low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0.0)
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])
    d["strong_day_rank"] = pd.NA
    strong = d["route"].astype(str).eq("strong_main")
    d.loc[strong, "strong_day_rank"] = (
        d.loc[strong]
        .sort_values(["entry_date", "score"], ascending=[True, False])
        .groupby("entry_date")
        .cumcount()
        + 1
    )
    d["strong_day_rank"] = pd.to_numeric(d["strong_day_rank"], errors="coerce")
    return d


def apply_scale(candidates: pd.DataFrame, variant: str) -> pd.DataFrame:
    d = candidates.copy()
    d["scale_variant"] = variant
    d["position_scale"] = 1.0
    d["scale_note"] = "full"
    strong = d["route"].astype(str).eq("strong_main")
    strong_score = d.loc[strong, "score"]
    q25 = float(strong_score.quantile(0.25))
    q50 = float(strong_score.quantile(0.50))
    q75 = float(strong_score.quantile(0.75))

    if variant in {"base", "strong_daily_limit1"}:
        return d
    if variant == "strong_score_lt_060_half":
        mask = strong & d["score"].lt(0.60)
    elif variant == "strong_score_lt_070_half":
        mask = strong & d["score"].lt(0.70)
    elif variant == "strong_q1_half":
        mask = strong & d["score"].le(q25)
    elif variant in {"strong_q1q2_half", "strong_daily_limit1_q1q2_half"}:
        mask = strong & d["score"].le(q50)
    elif variant == "strong_second_half":
        mask = strong & d["strong_day_rank"].ge(2)
    elif variant == "strong_second_score_ge_070":
        d = d[~(strong & d["strong_day_rank"].ge(2) & d["score"].lt(0.70))].copy()
        return d
    elif variant == "strong_second_score_ge_080":
        d = d[~(strong & d["strong_day_rank"].ge(2) & d["score"].lt(0.80))].copy()
        return d
    elif variant == "strong_second_score_ge_090":
        d = d[~(strong & d["strong_day_rank"].ge(2) & d["score"].lt(0.90))].copy()
        return d
    elif variant == "strong_second_score_ge_093":
        d = d[~(strong & d["strong_day_rank"].ge(2) & d["score"].lt(0.93))].copy()
        return d
    elif variant == "strong_second_score_ge_095":
        d = d[~(strong & d["strong_day_rank"].ge(2) & d["score"].lt(0.95))].copy()
        return d
    elif variant == "strong_second_q75":
        d = d[~(strong & d["strong_day_rank"].ge(2) & d["score"].lt(q75))].copy()
        return d
    else:
        raise ValueError(variant)

    d.loc[mask, "position_scale"] = 0.5
    d.loc[mask, "scale_note"] = variant
    return d


def apply_stress(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    extra_cost = (float(profile["cost_bps"]) - router.SOURCE_COST_BPS) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost - float(profile.get("all_shock", 0.0))
    d["stress_profile"] = profile["profile"]
    d["all_shock_applied"] = float(profile.get("all_shock", 0.0)) > 0
    return d


def simulate_scaled(candidates: pd.DataFrame, cost_bps: float, route_limit_override: dict[str, int] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = candidates.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False]).copy()
    cal = _trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    close_map = router.load_daily_close(candidates)
    by_day = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    old_limits = dict(router.ROUTE_DAILY_LIMIT)
    limits = dict(margin.quality.ROUTE_DAILY_LIMIT)
    if route_limit_override:
        limits.update(route_limit_override)
    router.ROUTE_DAILY_LIMIT = limits
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    mtm_cost = cost_bps / 10000.0
    try:
        for day in cal:
            realized = 0.0
            still = []
            for pos in open_pos:
                if pos["policy_exit_date"] <= day:
                    exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                    cash += exit_value
                    pnl = exit_value - float(pos["stake"])
                    out = pos.copy()
                    out["exit_value"] = exit_value
                    out["realized_pnl"] = pnl
                    closed.append(out)
                    realized += pnl
                else:
                    still.append(pos)
            open_pos = still

            opened = 0
            skipped = 0
            route_opened = {k: 0 for k in router.ROUTE_DAILY_LIMIT}
            todays = by_day.get(day)
            if todays is not None:
                todays = todays.sort_values(["route_priority", "score"], ascending=[False, False])
                for row in todays.itertuples(index=False):
                    route = str(row.route)
                    if opened >= router.DAILY_OPEN_LIMIT or len(open_pos) >= router.SLOTS:
                        skipped += 1
                        continue
                    if route_opened.get(route, 0) >= router.ROUTE_DAILY_LIMIT.get(route, 1):
                        skipped += 1
                        continue
                    equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                    scale = float(getattr(row, "position_scale", 1.0) or 1.0)
                    stake = equity_before * router.SLOT_PCT * scale
                    if stake <= 0 or cash < stake:
                        skipped += 1
                        continue
                    pos = row._asdict()
                    pos["stake"] = stake
                    cash -= stake
                    open_pos.append(pos)
                    route_opened[route] = route_opened.get(route, 0) + 1
                    opened += 1

            mtm_value = 0.0
            worst_open_mtm_ret = 0.0
            missing_close_positions = 0
            for pos in open_pos:
                close = close_map.get((str(pos["code"]), day))
                entry_price = float(pos["entry_price"])
                if close is None or entry_price <= 0:
                    mtm_value += float(pos["stake"])
                    missing_close_positions += 1
                    continue
                mtm_ret = close / entry_price - 1.0 - mtm_cost
                worst_open_mtm_ret = min(worst_open_mtm_ret, float(mtm_ret))
                mtm_value += float(pos["stake"]) * (1.0 + float(mtm_ret))
            equity = cash + mtm_value
            rows.append(
                {
                    "date": day,
                    "cash": cash,
                    "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                    "mtm_value": mtm_value,
                    "equity": equity,
                    "open_positions": len(open_pos),
                    "opened": opened,
                    "skipped": skipped,
                    "realized_pnl": realized,
                    "worst_open_mtm_ret": worst_open_mtm_ret,
                    "missing_close_positions": missing_close_positions,
                }
            )
    finally:
        router.ROUTE_DAILY_LIMIT = old_limits

    curve = pd.DataFrame(rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed)


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    scaled = pd.to_numeric(closed.get("position_scale", pd.Series(dtype=float)), errors="coerce").fillna(1.0).lt(1.0)
    route_parts: dict[str, Any] = {}
    for route, g in closed.groupby("route"):
        rnet = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        route_parts[f"{route}_trades"] = int(len(g))
        route_parts[f"{route}_avg_ret"] = float(rnet.mean()) if len(rnet) else None
        route_parts[f"{route}_pnl"] = float(pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0.0).sum())
        route_parts[f"{route}_scaled_trades"] = int(pd.to_numeric(g.get("position_scale", 1.0), errors="coerce").fillna(1.0).lt(1.0).sum())
    return {
        "variant": variant,
        "profile": profile,
        "trade_count": int(len(closed)),
        "scaled_trades": int(scaled.sum()),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        **route_parts,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base_candidates = load_candidates()
    variants = [
        "base",
        "strong_score_lt_060_half",
        "strong_score_lt_070_half",
        "strong_q1_half",
        "strong_q1q2_half",
        "strong_daily_limit1",
        "strong_daily_limit1_q1q2_half",
        "strong_second_half",
        "strong_second_score_ge_070",
        "strong_second_score_ge_080",
        "strong_second_score_ge_090",
        "strong_second_score_ge_093",
        "strong_second_score_ge_095",
        "strong_second_q75",
    ]
    summaries: list[dict[str, Any]] = []
    for variant in variants:
        scaled = apply_scale(base_candidates, variant)
        scaled.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            stressed = apply_stress(scaled, profile)
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]), VARIANT_ROUTE_LIMITS.get(variant))
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, variant, str(profile["profile"])))

    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret", "strong_main_avg_ret"}
    lines = [
        "# G3 V4 strong_main 仓位缩放探针 v1",
        "",
        "## 边界",
        "",
        "- 这是研究探针，不是正式规则；`Q1/Q2` 使用全样本分位，仅用于判断方向，不能直接实盘化。",
        "- 只缩放 strong_main 仓位，down_panic/range_gap 不动。",
        "- 缩放方式是在开仓资金上乘以 `position_scale=0.5`，不是把收益率减半。",
        "",
        "## 结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 若半仓能显著改善 all_shock2 且正常收益仍高于 guarded，说明仓位路径方向值得继续。",
        "- 若 all_shock2 改善很小，说明问题不是低分 strong，而是强势链路整体对逐笔冲击过敏。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
