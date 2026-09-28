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
import scripts.gen3_package_v4_research_candidate_v1 as v4pkg
from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy, _prepare_base


PACKAGE_DIR = ROOT / "reports" / "gen3_v4_research_package_v1"
OUT_DIR = ROOT / "reports" / "gen3_v4_strong_confirm_d3_combo_probe_v1"
INITIAL_CAPITAL = 150_000.0

RANGE_VARIANT = "v4_h10_margin"
PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost50", "cost_bps": 50.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "range_shock": 0.0, "all_shock": 0.02},
]


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, strong_rows: int) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    recent = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    route_parts: dict[str, Any] = {}
    for route, g in closed.groupby("route"):
        rnet = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        route_parts[f"{route}_trades"] = int(len(g))
        route_parts[f"{route}_avg_ret"] = float(rnet.mean()) if len(rnet) else None
        route_parts[f"{route}_pnl"] = float(pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0.0).sum())
    return {
        "variant": variant,
        "profile": profile,
        "strong_source_rows": strong_rows,
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


def _load_v4_non_strong() -> pd.DataFrame:
    d = pd.read_csv(PACKAGE_DIR / f"{RANGE_VARIANT}_candidates_standardized.csv", low_memory=False)
    d = d[~d["route"].astype(str).eq("strong_main")].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _standardize_confirm_d3(veto_l3_hot: bool) -> pd.DataFrame:
    base = _prepare_base(30.0)
    d = _apply_policy(base, "confirm_d3_le0", 30.0)
    if veto_l3_hot and "l3_s3" in d.columns:
        d = d[pd.to_numeric(d["l3_s3"], errors="coerce").lt(0.50) | pd.to_numeric(d["l3_s3"], errors="coerce").isna()].copy()
    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "strong_main"
    out["route_source"] = "strong_volume5_confirm_d3_le0_veto_l3_s3_ge50" if veto_l3_hot else "strong_volume5_confirm_d3_le0_all"
    out["route_priority"] = 2
    out["score"] = pd.to_numeric(d.get("v4_score", d.get("score_volume5")), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_price"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("net_ret"), errors="coerce")
    out["exit_reason_proxy"] = d.get("exit_reason_proxy", "")
    out["l3_s3"] = pd.to_numeric(d.get("l3_s3"), errors="coerce")
    return out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _build_candidates(variant: str) -> pd.DataFrame:
    non_strong = _load_v4_non_strong()
    strong = _standardize_confirm_d3(veto_l3_hot=variant.endswith("veto_l3"))
    d = pd.concat([non_strong, strong], ignore_index=True)
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0.0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


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
        "strong_main_avg_ret",
        "range_gap_avg_ret",
        "down_panic_avg_ret",
    }
    lines = [
        "# G3 V4 strong_main confirm_d3 组合替换诊断 v1",
        "",
        "## 边界",
        "",
        "- 只替换 V4 里的 strong_main 候选/退出，down_panic 和 range_gap 沿用 V4 H10 margin。",
        "- `confirm_d3_le0` 是历史研究代理，不是实盘可见规则；它只能说明 strong 退出方向是否值得继续做 30m/次日开盘真实重放。",
        "- `confirm_d3_veto_l3` 沿用 V4 已确认的 l3_s3>=50% 过热剔除。",
        "",
        "## 结果",
        "",
        _md_table(
            summary[
                [
                    "variant",
                    "profile",
                    "strong_source_rows",
                    "trade_count",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "strong_main_trades",
                    "strong_main_avg_ret",
                    "strong_main_pnl",
                ]
            ],
            pct_cols=pct_cols,
        ),
        "",
        "## 初步判断",
        "",
        "- 如果 confirm_d3 组合在 100bps 和 all_shock 下显著优于 V4 当前 strong，但基准收益没有塌陷，则值得进入下一阶段真实 30m/次日开盘执行重放。",
        "- 如果 all_shock 仍脆弱，说明 strong 的问题不只在退出，还包括仓位路径和强势源本身的成交敏感性。",
        "- 这一步不产生正式规则。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    variants = ["confirm_d3_all", "confirm_d3_veto_l3"]
    rows = []
    for variant in variants:
        candidates = _build_candidates(variant)
        candidates.to_csv(OUT_DIR / f"{variant}_candidates_standardized.csv", index=False, encoding="utf-8-sig")
        strong_rows = int(candidates["route"].astype(str).eq("strong_main").sum())
        for profile in PROFILES:
            stressed = v4pkg._apply_stress(candidates, profile)
            curve, closed = v4pkg._simulate(stressed)
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            router.summarize_windows(curve, closed).to_csv(run_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
            router.summarize_routes(closed).to_csv(run_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
            rows.append(_summary(curve, closed, variant, profile["profile"], strong_rows))

    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
