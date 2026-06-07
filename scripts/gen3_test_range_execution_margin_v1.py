from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.gen3_build_dynamic_router_combo_v1 as router
import scripts.gen3_test_strong_quality_filter_v1 as quality


OUT_DIR = ROOT / "reports" / "gen3_range_execution_margin_v1"
RANGE_DIR = ROOT / "reports" / "gen3_range_v3_gap_candidate_source_v1"
INITIAL_CAPITAL = 150_000.0


RANGE_VARIANTS = [
    {"variant": "range_h5_base", "source": "range_v3_weak_low_not_chasing_h5", "filter": "none"},
    {"variant": "range_h10_base", "source": "range_v3_weak_low_not_chasing_h10", "filter": "none"},
    {"variant": "range_h5_margin", "source": "range_v3_weak_low_not_chasing_h5", "filter": "margin_v1"},
    {"variant": "range_h10_margin", "source": "range_v3_weak_low_not_chasing_h10", "filter": "margin_v1"},
]

STRESS = [
    {"profile": "normal_30bps", "cost_bps": 30.0, "range_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "range_shock": 0.0},
    {"profile": "range_shock_2pct", "cost_bps": 30.0, "range_shock": 0.02},
    {"profile": "cost100_range_shock_2pct", "cost_bps": 100.0, "range_shock": 0.02},
]


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _load_range(spec: dict[str, str]) -> pd.DataFrame:
    path = RANGE_DIR / f"{spec['source']}_closed_trades.csv"
    d = pd.read_csv(path, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    for col in [
        "entry_open",
        "policy_net_ret",
        "net_ret",
        "range_v3_score",
        "range_pos60",
        "runup_from_60d_low",
        "close_position",
        "gap_open",
        "amount_ratio20",
        "index_mom20",
        "hold_days",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    if spec["filter"] == "margin_v1":
        # Fixed, interpretable safety-margin filter from prior layer audit.
        # Avoid very cold floors, exact high closes, high opening chase, and weak index momentum.
        d = d[
            d["range_pos60"].between(0.25, 0.45, inclusive="both")
            & d["runup_from_60d_low"].ge(0.18)
            & d["close_position"].between(0.80, 0.995, inclusive="both")
            & d["gap_open"].le(0.008)
            & d["index_mom20"].ge(0.0)
        ].copy()
    elif spec["filter"] != "none":
        raise ValueError(spec["filter"])

    out = pd.DataFrame()
    out["entry_date"] = d["entry_date"]
    out["policy_exit_date"] = d["policy_exit_date"]
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "range_gap"
    out["route_source"] = f"{spec['source']}_{spec['filter']}"
    out["route_priority"] = router.ROUTE_PRIORITY["range_gap"]
    out["score"] = pd.to_numeric(d.get("range_v3_score", d.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_open"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("policy_net_ret", d.get("net_ret")), errors="coerce")
    return out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _load_candidates(range_spec: dict[str, str]) -> pd.DataFrame:
    strong_candidates = quality._standardize_strong("l3_s3_lt50_or_missing")
    d = pd.concat([router.standardize_panic(), _load_range(range_spec), strong_candidates], ignore_index=True)
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"]).copy()
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def _apply_stress(candidates: pd.DataFrame, stress: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    extra_cost = (float(stress["cost_bps"]) - router.SOURCE_COST_BPS) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost
    if float(stress["range_shock"]) > 0:
        mask = d["route"].astype(str).eq("range_gap")
        d.loc[mask, "policy_net_ret"] = d.loc[mask, "policy_net_ret"] - float(stress["range_shock"])
        d["range_shock_applied"] = mask
    else:
        d["range_shock_applied"] = False
    return d


def _simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    old_limits = dict(router.ROUTE_DAILY_LIMIT)
    try:
        router.ROUTE_DAILY_LIMIT = dict(quality.ROUTE_DAILY_LIMIT)
        return router.simulate(candidates, router.SOURCE_COST_BPS)
    finally:
        router.ROUTE_DAILY_LIMIT = old_limits


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    recent = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    route_rows = []
    for route, g in closed.groupby("route"):
        route_rows.append(
            {
                f"{route}_trades": int(len(g)),
                f"{route}_avg_ret": float(pd.to_numeric(g["policy_net_ret"], errors="coerce").mean()),
                f"{route}_pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0).sum()),
            }
        )
    route_summary = {}
    for row in route_rows:
        route_summary.update(row)
    return {
        "variant": variant,
        "profile": profile,
        "trade_count": int(len(closed)),
        "range_shock_trades": int(closed.get("range_shock_applied", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()) if not closed.empty else 0,
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(curve["equity"]),
        "recent_return": float(recent["equity"].iloc[-1] / recent["equity"].iloc[0] - 1.0) if not recent.empty else None,
        "recent_max_drawdown": _max_drawdown(recent["equity"]) if not recent.empty else None,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        **route_summary,
    }


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _write_report(summary: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "recent_return",
        "recent_max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "range_gap_avg_ret",
    }
    lines = [
        "# G3 range 执行安全垫对照 v1",
        "",
        "## 边界",
        "",
        "- strong 使用 `veto_l3_s3_ge50`，只替换 range 链路。",
        "- `margin_v1` 是固定解释型过滤：中低箱体、runup>=18%、不极端收最高、不开盘追高、指数20日动量非负。",
        "- 目标是提高 range 单笔安全垫，降低 -2% 执行冲击敏感性。",
        "",
        "## 结果",
        "",
        _md_table(
            summary[
                [
                    "variant",
                    "profile",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "trade_count",
                    "range_gap_trades",
                    "range_gap_avg_ret",
                    "range_shock_trades",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                ]
            ],
            pct_cols=pct_cols,
        ),
        "",
        "## 判断",
        "",
        "- 若 margin 后 range 交易数过低但冲击敏感性仍高，说明当前 range 源不适合作为组合收益来源，只能降级为观察或小仓位。",
        "- 若 H10 在冲击下更稳但回撤扩大，需要继续找更早退出确认，而不是直接延长持有。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for range_spec in RANGE_VARIANTS:
        candidates = _load_candidates(range_spec)
        for stress in STRESS:
            stressed = _apply_stress(candidates, stress)
            curve, closed = _simulate(stressed)
            stem = f"{range_spec['variant']}__{stress['profile']}"
            out_dir = OUT_DIR / stem
            out_dir.mkdir(parents=True, exist_ok=True)
            stressed.to_csv(out_dir / "candidates_standardized.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(out_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(out_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            router.summarize_windows(curve, closed).to_csv(out_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
            router.summarize_routes(closed).to_csv(out_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
            rows.append(_summary(curve, closed, range_spec["variant"], stress["profile"]))
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "range_execution_margin_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
