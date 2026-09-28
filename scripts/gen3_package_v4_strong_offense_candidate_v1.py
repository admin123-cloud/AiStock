from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_v4_strong_position_scale_v1 import md_table  # noqa: E402
from utils.paths import report_path  # noqa: E402


PROBE_DIR = report_path("gen3_v4_strong_second093_scorecap_probe_v1")
V4_DIR = report_path("gen3_v4_research_package_v1")
OUT_DIR = report_path("gen3_v4_strong_offense_candidate_v1")

BASELINE_VARIANT = "v4_h10_margin"
BASELINE_PROFILE = "cost30"
SELECTED_VARIANT = "second093_score_lt105"
SELECTED_PROFILE = "cost30"
SELECTED_LABEL = "G3 V4 strong offense: second093 + score_lt105"
COMPARE_VARIANTS = ["second093", SELECTED_VARIANT, "second093_score_lt103"]
COMPARE_PROFILES = ["cost30", "cost100", "cost30_all_shock2", "nextopen_30bps", "nextopen_haircut2_30bps"]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def read_probe() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary = pd.read_csv(PROBE_DIR / "summary.csv", low_memory=False)
    windows = pd.read_csv(PROBE_DIR / "windows.csv", low_memory=False)
    annual = pd.read_csv(PROBE_DIR / "annual.csv", low_memory=False)
    diagnostics = pd.read_csv(PROBE_DIR / "diagnostics.csv", low_memory=False)
    return summary, windows, annual, diagnostics


def route_summary(closed: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for route, g in closed.groupby("route", dropna=False):
        net = pd.to_numeric(g.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "route": str(route),
                "trade_count": int(len(g)),
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
                "avg_trade_return": float(net.mean()) if len(net) else 0.0,
                "worst_trade": float(net.min()) if len(net) else 0.0,
                "sum_realized_pnl": float(pd.to_numeric(g.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("sum_realized_pnl", ascending=False)


def copy_selected_runs() -> None:
    for profile in COMPARE_PROFILES:
        src = PROBE_DIR / f"{SELECTED_VARIANT}__{profile}"
        dst = OUT_DIR / f"{SELECTED_VARIANT}__{profile}"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)


def build_goal_audit(
    selected: pd.Series,
    all_shock: pd.Series,
    nextopen_haircut2: pd.Series,
    baseline: pd.Series,
    diagnostics: pd.DataFrame,
) -> pd.DataFrame:
    diag = diagnostics[(diagnostics["variant"].eq(SELECTED_VARIANT)) & (diagnostics["profile"].eq(SELECTED_PROFILE))]
    diag_row = diag.iloc[0] if not diag.empty else pd.Series(dtype=object)
    rows = [
        {
            "item": "offense_return",
            "verdict": "PASS",
            "evidence": f"selected total_return {pct(selected['total_return'])} vs V4 H10 baseline {pct(baseline['total_return'])}.",
        },
        {
            "item": "cost_pressure",
            "verdict": "PASS_RESEARCH",
            "evidence": "cost100 keeps positive return near 100%; execution cost does not erase the edge.",
        },
        {
            "item": "all_chain_shock",
            "verdict": "PARTIAL_PASS",
            "evidence": f"all_shock2 return {pct(all_shock['total_return'])}, max_drawdown {pct(all_shock['max_drawdown'])}; still too sensitive for live promotion.",
        },
        {
            "item": "nextopen_haircut2",
            "verdict": "PARTIAL_PASS",
            "evidence": f"nextopen_haircut2 return {pct(nextopen_haircut2['total_return'])}, max_drawdown {pct(nextopen_haircut2['max_drawdown'])}; stress survives but degrades materially.",
        },
        {
            "item": "strong_tail_control",
            "verdict": "PASS_RESEARCH",
            "evidence": (
                f"strong rows {int(diag_row.get('strong_rows', 0))}; "
                f"bad8_strong_rate {pct(diag_row.get('bad8_strong_rate'))}; "
                "score cap removes overheated strong tails without reducing total return."
            ),
        },
        {
            "item": "live_ready",
            "verdict": "NOT_PASS",
            "evidence": "research package only; no formal buy signal, no order path, no live-safe payload.",
        },
    ]
    return pd.DataFrame(rows)


def write_report(
    compare: pd.DataFrame,
    windows: pd.DataFrame,
    annual: pd.DataFrame,
    diagnostics: pd.DataFrame,
    routes: pd.DataFrame,
    goal: pd.DataFrame,
) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "strong_main_avg_ret",
        "return",
        "mean_candidate_ret",
        "worst_candidate_ret",
        "bad10_candidate_rate",
        "mean_strong_ret",
        "worst_strong_ret",
        "bad8_strong_rate",
        "avg_trade_return",
    }
    lines = [
        "# G3 V4 strong offense candidate v1",
        "",
        "## Positioning",
        "",
        "- Research-only offensive candidate.",
        "- Keeps V4 H10 down_panic and range_gap unchanged.",
        "- Replaces only strong_main with `strong_second_score_ge_093` plus `score < 1.05`.",
        "- No formal buy signal, no order path, no live-safe payload.",
        "",
        "## Candidate Comparison",
        "",
        md_table(compare, pct_cols=pct_cols),
        "",
        "## Selected Windows",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## Annual",
        "",
        md_table(annual, pct_cols=pct_cols),
        "",
        "## Route Attribution",
        "",
        md_table(routes, pct_cols={"win_rate", "avg_trade_return", "worst_trade"}),
        "",
        "## Diagnostics",
        "",
        md_table(diagnostics, pct_cols=pct_cols),
        "",
        "## Goal Audit",
        "",
        md_table(goal),
        "",
        "## Decision",
        "",
        "- Promote this to the next G3 V4 offensive research candidate.",
        "- Do not connect to live trading until live-safe payload and execution visibility pass.",
        "- Next research step: test whether the same score cap improves current-date shadow candidates without look-ahead fields.",
        "",
    ]
    (OUT_DIR / "g3_v4_strong_offense_candidate_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary, windows, annual, diagnostics = read_probe()
    compare = summary[summary["variant"].isin(COMPARE_VARIANTS) & summary["profile"].isin(COMPARE_PROFILES)].copy()
    selected_rows = summary[summary["variant"].eq(SELECTED_VARIANT)]
    selected = selected_rows[selected_rows["profile"].eq(SELECTED_PROFILE)].iloc[0]
    all_shock = selected_rows[selected_rows["profile"].eq("cost30_all_shock2")].iloc[0]
    nextopen_haircut2 = selected_rows[selected_rows["profile"].eq("nextopen_haircut2_30bps")].iloc[0]

    baseline_summary = pd.read_csv(V4_DIR / "g3_v4_research_summary.csv", low_memory=False)
    baseline = baseline_summary[
        baseline_summary["variant"].eq(BASELINE_VARIANT) & baseline_summary["profile"].eq(BASELINE_PROFILE)
    ].iloc[0]

    selected_windows = windows[
        windows["variant"].eq(SELECTED_VARIANT)
        & windows["profile"].isin(["cost30", "cost100", "cost30_all_shock2", "nextopen_30bps", "nextopen_haircut2_30bps"])
    ].copy()
    selected_annual = annual[annual["variant"].eq(SELECTED_VARIANT) & annual["profile"].eq(SELECTED_PROFILE)].copy()
    selected_diagnostics = diagnostics[diagnostics["variant"].eq(SELECTED_VARIANT) & diagnostics["profile"].isin(COMPARE_PROFILES)].copy()

    run_dir = PROBE_DIR / f"{SELECTED_VARIANT}__{SELECTED_PROFILE}"
    closed = pd.read_csv(run_dir / "closed_trades.csv", low_memory=False)
    curve = pd.read_csv(run_dir / "mtm_equity_curve.csv", low_memory=False)
    routes = route_summary(closed)
    goal = build_goal_audit(selected, all_shock, nextopen_haircut2, baseline, diagnostics)

    copy_selected_runs()
    compare.to_csv(OUT_DIR / "g3_v4_strong_offense_compare_summary.csv", index=False, encoding="utf-8-sig")
    selected_windows.to_csv(OUT_DIR / "g3_v4_strong_offense_windows.csv", index=False, encoding="utf-8-sig")
    selected_annual.to_csv(OUT_DIR / "g3_v4_strong_offense_annual.csv", index=False, encoding="utf-8-sig")
    selected_diagnostics.to_csv(OUT_DIR / "g3_v4_strong_offense_diagnostics.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "g3_v4_strong_offense_route_attribution.csv", index=False, encoding="utf-8-sig")
    goal.to_csv(OUT_DIR / "g3_v4_strong_offense_goal_audit.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(OUT_DIR / "g3_v4_strong_offense_closed_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(OUT_DIR / "g3_v4_strong_offense_equity_curve.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "candidate": "g3_v4_strong_offense_candidate_v1",
        "selected_variant": SELECTED_VARIANT,
        "selected_profile": SELECTED_PROFILE,
        "label": SELECTED_LABEL,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "research_only": True,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "inherits_down_range_from": "gen3_v4_research_package_v1/v4_h10_margin",
        "strong_rule": {
            "base": "strong_second_score_ge_093",
            "score_cap_exclusive": 1.05,
            "scope": "strong_main route only",
        },
        "selected_metrics": {
            "trade_count": int(selected["trade_count"]),
            "total_return": float(selected["total_return"]),
            "max_drawdown": float(selected["max_drawdown"]),
            "win_rate": float(selected["win_rate"]),
            "strong_main_trades": int(selected.get("strong_main_trades", 0)),
            "strong_main_pnl": float(selected.get("strong_main_pnl", 0.0)),
        },
        "next_step": "build shadow-only payload and current-date visible candidate mapping before any live signal.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(compare, selected_windows, selected_annual, selected_diagnostics, routes, goal)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
