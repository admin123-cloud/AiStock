from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_stress_v4_strong_second_093_execution_v1 import (  # noqa: E402
    PROFILES as EXECUTION_PROFILES,
    VARIANT_ROUTE_LIMITS,
    _load_daily_prices,
    annual_metrics,
    apply_execution_profile,
    window_metrics,
)
from scripts.gen3_test_v4_strong_position_scale_v1 import (  # noqa: E402
    PROFILES as CLOSE_PROFILES,
    apply_scale,
    apply_stress,
    load_candidates,
    md_table,
    simulate_scaled,
    summarize,
)
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("gen3_v4_strong_second093_scorecap_probe_v1")


VARIANTS = [
    {"variant": "second093", "score_cap": None},
    {"variant": "second093_score_lt105", "score_cap": 1.05},
    {"variant": "second093_score_lt103", "score_cap": 1.03},
]


def build_variant(base: pd.DataFrame, score_cap: float | None) -> pd.DataFrame:
    d = apply_scale(base, "strong_second_score_ge_093")
    if score_cap is None:
        return d
    strong = d["route"].astype(str).eq("strong_main")
    score = pd.to_numeric(d["score"], errors="coerce")
    return d[~(strong & score.ge(score_cap))].copy()


def _write_tables(
    summary: pd.DataFrame,
    windows: pd.DataFrame,
    annual: pd.DataFrame,
    diagnostics: pd.DataFrame,
) -> None:
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "windows.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual.csv", index=False, encoding="utf-8-sig")
    diagnostics.to_csv(OUT_DIR / "diagnostics.csv", index=False, encoding="utf-8-sig")


def _diagnose_candidates(candidates: pd.DataFrame, variant: str, profile: str) -> dict[str, Any]:
    strong = candidates[candidates["route"].astype(str).eq("strong_main")].copy()
    net = pd.to_numeric(candidates["policy_net_ret"], errors="coerce")
    strong_net = pd.to_numeric(strong.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "profile": profile,
        "candidate_rows": int(len(candidates)),
        "strong_rows": int(len(strong)),
        "mean_candidate_ret": float(net.mean()) if len(net) else 0.0,
        "worst_candidate_ret": float(net.min()) if len(net) else 0.0,
        "bad10_candidate_rate": float((net <= -0.10).mean()) if len(net) else 0.0,
        "mean_strong_ret": float(strong_net.mean()) if len(strong_net) else 0.0,
        "worst_strong_ret": float(strong_net.min()) if len(strong_net) else 0.0,
        "bad8_strong_rate": float((strong_net <= -0.08).mean()) if len(strong_net) else 0.0,
    }


def _write_report(summary: pd.DataFrame, windows: pd.DataFrame, diagnostics: pd.DataFrame) -> None:
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
    }
    focus_profiles = [
        "cost30",
        "cost100",
        "cost30_all_shock2",
        "nextopen_30bps",
        "nextopen_haircut2_30bps",
    ]
    focus = summary[summary["profile"].isin(focus_profiles)].copy()
    win_focus = windows[windows["window"].isin(["weak_gap_2022_2024", "valid_2024_2025", "blind_2026ytd", "full"])].copy()
    lines = [
        "# G3 V4 strong second093 score cap probe v1",
        "",
        "## Scope",
        "",
        "- Keep down_panic and range_gap unchanged from V4 H10 margin.",
        "- Apply strong_second_score_ge_093 first, then optionally drop strong_main rows with score >= cap.",
        "- This is a research-only replay, not a live rule.",
        "",
        "## Summary",
        "",
        md_table(focus, pct_cols=pct_cols),
        "",
        "## Windows",
        "",
        md_table(win_focus, pct_cols=pct_cols),
        "",
        "## Candidate Diagnostics",
        "",
        md_table(diagnostics, pct_cols=pct_cols),
        "",
    ]
    (OUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_candidates()
    daily = _load_daily_prices(base, extra_days=20)
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    diag_rows: list[dict[str, Any]] = []

    for spec in VARIANTS:
        variant = str(spec["variant"])
        candidates = build_variant(base, spec["score_cap"])
        candidates.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")

        for profile in CLOSE_PROFILES:
            profile_name = str(profile["profile"])
            stressed = apply_stress(candidates, profile)
            run_dir = OUT_DIR / f"{variant}__{profile_name}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]), VARIANT_ROUTE_LIMITS.get("strong_second_score_ge_093"))
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            row = summarize(curve, closed, variant, profile_name)
            summary_rows.append(row)
            window_rows.extend(
                {
                    **item,
                    "variant": variant,
                }
                for item in window_metrics(curve, closed, profile_name)
            )
            annual_rows.extend(
                {
                    **item,
                    "variant": variant,
                }
                for item in annual_metrics(curve, closed, profile_name)
            )
            diag_rows.append(_diagnose_candidates(stressed, variant, profile_name))

        for profile in EXECUTION_PROFILES:
            profile_name = str(profile["profile"])
            if profile_name.startswith("close_"):
                continue
            stressed = apply_execution_profile(candidates, daily, profile)
            run_dir = OUT_DIR / f"{variant}__{profile_name}"
            run_dir.mkdir(parents=True, exist_ok=True)
            stressed.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]), VARIANT_ROUTE_LIMITS.get("strong_second_score_ge_093"))
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, variant, profile_name))
            window_rows.extend(
                {
                    **item,
                    "variant": variant,
                }
                for item in window_metrics(curve, closed, profile_name)
            )
            annual_rows.extend(
                {
                    **item,
                    "variant": variant,
                }
                for item in annual_metrics(curve, closed, profile_name)
            )
            diag_rows.append(_diagnose_candidates(stressed, variant, profile_name))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    annual = pd.DataFrame(annual_rows)
    diagnostics = pd.DataFrame(diag_rows)
    _write_tables(summary, windows, annual, diagnostics)
    _write_report(summary, windows, diagnostics)
    print(f"wrote {OUT_DIR}")
    print(
        summary[
            summary["profile"].isin(["cost30", "cost100", "cost30_all_shock2", "nextopen_30bps", "nextopen_haircut2_30bps"])
        ][
            [
                "variant",
                "profile",
                "trade_count",
                "total_return",
                "max_drawdown",
                "win_rate",
                "strong_main_trades",
                "strong_main_pnl",
            ]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
