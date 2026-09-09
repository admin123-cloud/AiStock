from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.gen3_build_dynamic_router_combo_v1 as router
import scripts.gen3_package_v4_research_candidate_v1 as v4pkg


PACKAGE_DIR = _report_path() / "gen3_v4_research_package_v1"
OUT_DIR = _report_path() / "gen3_v4_strong_score_filter_probe_v1"
INITIAL_CAPITAL = 150_000.0
VARIANT = "v4_h10_margin"

PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "range_shock": 0.0, "all_shock": 0.02},
]


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, variant_name: str, profile: str, min_score: float | None) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    recent = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    route_parts: dict[str, Any] = {}
    for route, g in closed.groupby("route"):
        rnet = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        route_parts[f"{route}_trades"] = int(len(g))
        route_parts[f"{route}_avg_ret"] = float(rnet.mean()) if len(rnet) else None
        route_parts[f"{route}_pnl"] = float(pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0.0).sum())
    return {
        "variant": variant_name,
        "profile": profile,
        "strong_min_score": min_score,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(curve["equity"]),
        "recent_return": float(recent["equity"].iloc[-1] / recent["equity"].iloc[0] - 1.0) if not recent.empty else None,
        "recent_max_drawdown": _max_drawdown(recent["equity"]) if not recent.empty else None,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        **route_parts,
    }


def _simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    return v4pkg._simulate(candidates)


def _filter_strong(candidates: pd.DataFrame, min_score: float | None) -> pd.DataFrame:
    if min_score is None:
        return candidates.copy()
    d = candidates.copy()
    score = pd.to_numeric(d["score"], errors="coerce")
    strong = d["route"].astype(str).eq("strong_main")
    keep = ~strong | score.ge(float(min_score))
    return d[keep].copy()


from research.common.reporting import percent_text as _pct


from research.common.reporting import markdown_table as _md_table


def _write_report(summary: pd.DataFrame) -> None:
    pivot = summary.pivot_table(
        index=["variant", "strong_min_score"],
        columns="profile",
        values=["total_return", "max_drawdown", "trade_count", "strong_main_trades", "strong_main_avg_ret"],
        aggfunc="first",
    )
    flat_rows = []
    for idx, row in pivot.iterrows():
        variant, score = idx
        base_ret = row.get(("total_return", "cost30"))
        shock_ret = row.get(("total_return", "cost30_all_shock2"))
        flat_rows.append(
            {
                "variant": variant,
                "strong_min_score": score,
                "base_return": base_ret,
                "shock_return": shock_ret,
                "shock_retention": (shock_ret + 1.0) / (base_ret + 1.0) - 1.0 if pd.notna(base_ret) and pd.notna(shock_ret) and (base_ret + 1.0) != 0 else None,
                "base_dd": row.get(("max_drawdown", "cost30")),
                "shock_dd": row.get(("max_drawdown", "cost30_all_shock2")),
                "base_trades": row.get(("trade_count", "cost30")),
                "shock_trades": row.get(("trade_count", "cost30_all_shock2")),
                "base_strong_trades": row.get(("strong_main_trades", "cost30")),
                "shock_strong_trades": row.get(("strong_main_trades", "cost30_all_shock2")),
                "base_strong_avg": row.get(("strong_main_avg_ret", "cost30")),
                "shock_strong_avg": row.get(("strong_main_avg_ret", "cost30_all_shock2")),
            }
        )
    flat = pd.DataFrame(flat_rows).sort_values(["shock_return", "base_return"], ascending=[False, False])
    flat.to_csv(OUT_DIR / "strong_score_filter_pivot.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# G3 V4 strong_main score 分层压力测试 v1",
        "",
        "## 边界",
        "",
        "- 这是诊断，不是新规则。",
        "- 只对 strong_main 做 score 粗分层；down_panic/range_gap 不变。",
        "- 目标是判断强势链路抗冲击问题能否靠简单质量过滤缓解。",
        "",
        "## 结果",
        "",
        _md_table(
            flat,
            pct_cols={"base_return", "shock_return", "shock_retention", "base_dd", "shock_dd", "base_strong_avg", "shock_strong_avg"},
        ),
        "",
        "## 判断",
        "",
        "- 如果过滤后基准收益大幅下降，而全链路冲击没有明显改善，说明问题不是低分样本噪声，而是强势链路整体成交/仓位路径脆弱。",
        "- 如果过滤后全链路冲击改善但基准收益损失太大，不能直接升级为规则，需要再找更独立的强势质量特征。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_csv(PACKAGE_DIR / f"{VARIANT}_candidates_standardized.csv", low_memory=False)
    candidates["score"] = pd.to_numeric(candidates["score"], errors="coerce")
    candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce")
    candidates["policy_exit_date"] = pd.to_datetime(candidates["policy_exit_date"], errors="coerce")
    strong_scores = candidates.loc[candidates["route"].astype(str).eq("strong_main"), "score"].dropna()
    thresholds: list[tuple[str, float | None]] = [("baseline", None)]
    for q in [0.25, 0.50, 0.65, 0.75]:
        thresholds.append((f"strong_score_q{int(q * 100)}", float(strong_scores.quantile(q))))

    rows = []
    for label, threshold in thresholds:
        filtered = _filter_strong(candidates, threshold)
        for profile in PROFILES:
            stressed = v4pkg._apply_stress(filtered, profile)
            curve, closed = _simulate(stressed)
            run_dir = OUT_DIR / f"{label}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            router.summarize_windows(curve, closed).to_csv(run_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
            router.summarize_routes(closed).to_csv(run_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
            rows.append(_summary(curve, closed, label, profile["profile"], threshold))

    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "strong_score_filter_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
