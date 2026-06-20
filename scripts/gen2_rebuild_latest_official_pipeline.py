from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_runtime_dates import resolve_end_date
from utils.paths import report_path

PYTHON = sys.executable
REPORTS_ROOT = report_path()
TIMING_DIR = report_path("gen2_timing_research")
OPEN_STATE_DIR = report_path("gen2_open_state_research_full")
EVENT_DATASET = report_path("gen2_event_study_full", "v4_event_dataset.parquet")
TRIGGER_DIR = report_path("gen2_30m_fractal_restart_realistic_d1_w2")
FILTER_DIR = report_path("gen2_intraday_normal_signal_filters_tday_context")
REALTIME_DIR = report_path("gen2_realtime_candidate_recall_study")
CANDIDATE_DIR = report_path("gen2_candidate_outcome_supervision")
RISK_COOL_DIR = report_path("gen2_risk_cool_dynamic_circuit_user_v2_cap_v2")
SHADOW_DIR = report_path("gen2_risk_cool_shadow_ledger")
BREAKOUT_BUY_POINT_DIR = report_path("gen2_breakout_buy_point_research")
V2_COMPLETE_DIR = report_path("gen2_v2_complete_strategy")
V2_COMPLETE_LEGACY_RUN_DIR = report_path("gen2_v2_complete_strategy", "runs", "official_legacy_330", "full")


def _run_step(title: str, args: list[str]) -> dict[str, Any]:
    cmd = [PYTHON, *args]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    return {
        "title": title,
        "cmd": cmd,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-20:]),
        "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-20:]),
    }


def run(
    end_date: str,
    include_breakout: bool = True,
    breakout_only: bool = False,
    legacy_330: bool = False,
) -> dict[str, Any]:
    mainline_steps: list[tuple[str, list[str]]] = [
        (
            "refresh_open_state_timing",
            [
                "scripts/gen2_timing_regime_research.py",
                "--start-date",
                "2010-01-01",
                "--end-date",
                end_date,
                "--output-dir",
                str(TIMING_DIR),
            ],
        ),
        (
            "refresh_open_state_daily",
            [
                "scripts/gen2_build_open_state_research.py",
                "--timing-dir",
                str(TIMING_DIR),
                "--event-dataset",
                str(EVENT_DATASET),
                "--output-dir",
                str(OPEN_STATE_DIR),
                "--factor-mode",
                "proxy",
            ],
        ),
        (
            "refresh_30m_triggers",
            [
                "scripts/gen2_test_30m_fractal_restart.py",
                "--event-dataset",
                str(EVENT_DATASET),
                "--state-daily",
                str(OPEN_STATE_DIR / "g2_open_state_daily.csv"),
                "--output-dir",
                str(TRIGGER_DIR),
                "--start-date",
                "2024-07-09",
                "--end-date",
                end_date,
                "--search-days",
                "2",
                "--entry-start-lag-days",
                "1",
            ],
        ),
        (
            "filter_intraday_normal",
            [
                "scripts/gen2_filter_signals_by_intraday_normal.py",
                "--input",
                str(TRIGGER_DIR / "fractal_triggers.parquet"),
                "--state-daily",
                str(OPEN_STATE_DIR / "g2_open_state_daily.csv"),
                "--output-dir",
                str(FILTER_DIR),
                "--start-date",
                "2024-07-09",
                "--end-date",
                end_date,
            ],
        ),
        (
            "realtime_candidate_recall",
            [
                "scripts/gen2_realtime_candidate_recall_study.py",
                "--target",
                str(FILTER_DIR / "signals_intraday_normal_30m_before_confirm.parquet"),
                "--event-dataset",
                str(EVENT_DATASET),
                "--state-daily",
                str(OPEN_STATE_DIR / "g2_open_state_daily.csv"),
                "--output-dir",
                str(REALTIME_DIR),
                "--start-date",
                "2024-07-09",
                "--end-date",
                end_date,
            ],
        ),
        (
            "candidate_outcome_supervision",
            [
                "scripts/gen2_candidate_outcome_supervision.py",
                "--candidates",
                str(REALTIME_DIR / "realtime_candidates.parquet"),
                "--output-dir",
                str(CANDIDATE_DIR),
                "--pool",
                "d1_rank200",
                "--start-date",
                "2024-07-09",
                "--end-date",
                end_date,
            ],
        ),
        (
            "risk_cool_dynamic_circuit",
            [
                "scripts/gen2_backtest_risk_cool_dynamic_circuit.py",
                "--candidates",
                str(CANDIDATE_DIR / "labeled_candidates.parquet"),
                "--output-dir",
                str(RISK_COOL_DIR),
                "--start-date",
                "2024-07-09",
                "--end-date",
                end_date,
                "--policies",
                "two_stop_cd3_skip",
                "--preference-filter",
                "user_v2",
            ],
        ),
        (
            "shadow_ledger",
            [
                "scripts/gen2_build_risk_cool_shadow_ledger.py",
                "--source",
                str(RISK_COOL_DIR / "sources" / "risk_cool_base.csv"),
                "--run-dir",
                str(RISK_COOL_DIR / "backtests" / "two_stop_cd3_skip"),
                "--output-dir",
                str(SHADOW_DIR),
            ],
        ),
    ]
    breakout_steps: list[tuple[str, list[str]]] = [
        (
            "breakout_buy_point_research",
            [
                "scripts/gen2_research_box_breakout_buy_points.py",
                "--output-dir",
                str(BREAKOUT_BUY_POINT_DIR),
                "--start-date",
                "2024-07-09",
                "--end-date",
                end_date,
                "--skip-backtest",
            ],
        ),
        (
            "breakout_combo_policy",
            [
                "scripts/gen2_research_breakout_intraday_combo.py",
                "--end-date",
                end_date,
            ],
        ),
        (
            "breakout_sector_filter",
            [
                "scripts/gen2_research_breakout_sector_filter_matrix.py",
                "--end-date",
                end_date,
            ],
        ),
        (
            "volume5_sector_layer",
            [
                "scripts/gen2_research_volume5_sector_layer.py",
                "--end-date",
                end_date,
                "--force-rebuild",
            ],
        ),
        (
            "breakout_sector_integration",
            [
                "scripts/gen2_research_breakout_sector_integration.py",
                "--end-date",
                end_date,
            ],
        ),
        (
            "build_g2_v2_complete",
            [
                "scripts/gen2_build_v2_complete_strategy.py",
                "--output-dir",
                str(V2_COMPLETE_DIR),
                "--end-date",
                end_date,
                "--replace-official",
                "--source-only",
                "--populate-sort-probe",
            ]
            + (["--legacy-330", "--official-legacy-run-dir", str(V2_COMPLETE_LEGACY_RUN_DIR)] if legacy_330 else []),
        ),
    ]

    if breakout_only:
        # Daily official refresh only needs the latest formal source/freshness files.
        # Research probes and backtests stay available through explicit heavy tasks.
        steps = [breakout_steps[-1]]
    else:
        steps = list(mainline_steps)
        if include_breakout:
            steps.extend(breakout_steps)
        if legacy_330 and not include_breakout:
            steps.append(
                (
                    "build_g2_v2_complete",
                    [
                        "scripts/gen2_build_v2_complete_strategy.py",
                        "--output-dir",
                        str(V2_COMPLETE_DIR),
                        "--end-date",
                        end_date,
                        "--replace-official",
                        "--source-only",
                        "--populate-sort-probe",
                        "--legacy-330",
                        "--official-legacy-run-dir",
                        str(V2_COMPLETE_LEGACY_RUN_DIR),
                    ],
                )
            )

    results: list[dict[str, Any]] = []
    for title, cmd in steps:
        item = _run_step(title, cmd)
        results.append(item)
        if not item["ok"]:
            break

    payload = {
        "end_date": end_date,
        "include_breakout": include_breakout,
        "breakout_only": breakout_only,
        "legacy_330": legacy_330,
        "mode": "breakout_only" if breakout_only else ("full" if include_breakout else "mainline"),
        "ok": all(item["ok"] for item in results),
        "completed_steps": len(results),
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the official G2 latest-data pipeline to the newest trade date.")
    parser.add_argument("--end-date", default="", help="Optional end date in YYYY-MM-DD; blank means latest stock trade date.")
    parser.add_argument("--skip-breakout", action="store_true", help="Only rebuild the pullback/risk_cool/shadow mainline.")
    parser.add_argument("--breakout-only", action="store_true", help="Only rebuild breakout/G2 v2 complete official display branch.")
    parser.add_argument("--legacy-330", action="store_true", help="Use legacy 330%% mode in G2 v2 complete build.")
    args = parser.parse_args()
    payload = run(
        resolve_end_date(args.end_date),
        include_breakout=not bool(args.skip_breakout),
        breakout_only=bool(args.breakout_only),
        legacy_330=bool(args.legacy_330),
    )
    if not payload.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
