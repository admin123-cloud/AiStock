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


OUT_DIR = ROOT / "reports" / "gen3_execution_shock_by_route_v1"
INITIAL_CAPITAL = 150_000.0

PROFILES = [
    {"profile": "no_shock", "routes": [], "shock": 0.0},
    {"profile": "shock_strong_2pct", "routes": ["strong_main"], "shock": 0.02},
    {"profile": "shock_range_2pct", "routes": ["range_gap"], "shock": 0.02},
    {"profile": "shock_panic_2pct", "routes": ["down_panic"], "shock": 0.02},
    {"profile": "shock_down_range_2pct", "routes": ["down_panic", "range_gap"], "shock": 0.02},
    {"profile": "shock_all_2pct", "routes": ["down_panic", "range_gap", "strong_main"], "shock": 0.02},
]


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _apply_route_shock(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["shock_profile"] = profile["profile"]
    d["shock_applied"] = False
    routes = set(profile["routes"])
    shock = float(profile["shock"])
    if routes and shock > 0:
        mask = d["route"].astype(str).isin(routes)
        d.loc[mask, "policy_net_ret"] = d.loc[mask, "policy_net_ret"] - shock
        d.loc[mask, "shock_applied"] = True
    return d


def _simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    old_limits = dict(router.ROUTE_DAILY_LIMIT)
    try:
        router.ROUTE_DAILY_LIMIT = dict(quality.ROUTE_DAILY_LIMIT)
        return router.simulate(candidates, router.SOURCE_COST_BPS)
    finally:
        router.ROUTE_DAILY_LIMIT = old_limits


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, profile: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    recent = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    return {
        "profile": profile,
        "trade_count": int(len(closed)),
        "shock_trades": int(closed.get("shock_applied", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()) if not closed.empty else 0,
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(curve["equity"]),
        "recent_return": float(recent["equity"].iloc[-1] / recent["equity"].iloc[0] - 1.0) if not recent.empty else None,
        "recent_max_drawdown": _max_drawdown(recent["equity"]) if not recent.empty else None,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
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
    base = summary[summary["profile"].eq("no_shock")].iloc[0]
    d = summary.copy()
    d["return_drop_vs_base"] = d["total_return"] - float(base["total_return"])
    d["recent_drop_vs_base"] = d["recent_return"] - float(base["recent_return"])
    pct_cols = {
        "total_return",
        "max_drawdown",
        "recent_return",
        "recent_max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "return_drop_vs_base",
        "recent_drop_vs_base",
    }
    lines = [
        "# G3 执行冲击按链路归因 v1",
        "",
        "## 边界",
        "",
        "- 基准候选：`veto_l3_s3_ge50`。",
        "- 每次只对指定 route 的成交额外扣减 2%，用于衡量执行冲击敏感性。",
        "- 这是压力归因，不是真实成交模拟。",
        "",
        "## 结果",
        "",
        _md_table(
            d[
                [
                    "profile",
                    "total_return",
                    "return_drop_vs_base",
                    "max_drawdown",
                    "recent_return",
                    "recent_drop_vs_base",
                    "recent_max_drawdown",
                    "trade_count",
                    "shock_trades",
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
        "- 收益下降最大的 route 是下一步优先降低执行冲击敏感性的对象。",
        "- 如果 strong 冲击最敏感，说明不能只修 down/range 的同日卖出；strong 也需要更真实的成交模型。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base_candidates = quality._load_candidates("l3_s3_lt50_or_missing")
    rows = []
    for profile in PROFILES:
        candidates = _apply_route_shock(base_candidates, profile)
        curve, closed = _simulate(candidates)
        stem = profile["profile"]
        out_dir = OUT_DIR / stem
        out_dir.mkdir(parents=True, exist_ok=True)
        curve.to_csv(out_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(out_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        router.summarize_windows(curve, closed).to_csv(out_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
        router.summarize_routes(closed).to_csv(out_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
        rows.append(_summary(curve, closed, stem))
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "shock_by_route_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
