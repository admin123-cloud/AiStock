from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, window_metrics  # noqa: E402
from scripts.gen3_test_range_box_stress_d1d2_weak_exit_v1 import (  # noqa: E402
    apply_exit_policy,
    load_daily_opens,
    summarize,
)
from scripts.gen3_test_range_box_stress_icepoint_climax_overlay_v1 import md_table, simulate_scaled  # noqa: E402


SOURCE = _report_path() / "gen3_range_box_stress_ice_window_source_v1" / "ice_recent3_signals.csv"
OUT_DIR = _report_path() / "gen3_range_ice_recent3_d1d2_exit_v1"

VARIANTS = [
    {"variant": "ice_recent3_hold5", "desc": "冰点后3日窗口，原始持有5日", "d1_exit": False, "d2_exit": False},
    {"variant": "ice_recent3_d1_exit", "desc": "冰点后3日窗口，D1弱则D2开盘退", "d1_exit": True, "d2_exit": False},
    {"variant": "ice_recent3_d2_exit", "desc": "冰点后3日窗口，D2仍弱则D3开盘退", "d1_exit": False, "d2_exit": True},
    {"variant": "ice_recent3_d1_or_d2_exit", "desc": "冰点后3日窗口，D1/D2弱确认快速退", "d1_exit": True, "d2_exit": True},
]


def prepare_base() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    opens = load_daily_opens(d)
    d = d.merge(opens, on=["code", "entry_date"], how="left")
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["entry_date_ts"], errors="coerce") + pd.Timedelta(days=8)
    for col in [
        "entry_price_adjusted",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "candidate_score",
        "amount_ratio3",
        "d2_open",
        "d3_open",
        "rank_key",
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["rank_key"] = d["rank_key"].fillna(d["candidate_score"].fillna(0.0) + d["amount_ratio3"].fillna(0.0) * 0.02)
    d["entry_price_used"] = d["entry_price_adjusted"]
    d["position_scale"] = 1.0
    return d.dropna(subset=["entry_date_ts", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = prepare_base()
    base.to_csv(OUT_DIR / "ice_recent3_base_with_exit_opens.csv", index=False, encoding="utf-8-sig")
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for spec in VARIANTS:
        for profile in PROFILES:
            candidates = apply_exit_policy(base, spec, profile)
            candidates["source_variant"] = spec["variant"]
            candidates["source_desc"] = spec["desc"]
            curve, closed = simulate_scaled(candidates)
            run_dir = OUT_DIR / f"{spec['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, str(spec["variant"]), str(profile["profile"]), str(spec["desc"])))
            window_rows.extend(window_metrics(curve, closed, str(spec["variant"]), str(profile["profile"])))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    lines = [
        "# G3 ice_recent3 D1/D2早期弱确认退出复验 v1",
        "",
        "## 策略名解释",
        "",
        "- `ice_recent3`：中文是“冰点后3个交易日窗口”。前一交易日或近3个交易日内出现冰点后，允许横盘箱体底部30m承接买入。",
        "- `d1_exit`：D1收盘相对确认买入价跌超过3%，下一交易日开盘快速退出。",
        "- `d2_exit`：D2收盘仍相对确认买入价跌超过3%，下一交易日开盘快速退出。",
        "",
        "## 复验结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "",
        "- 如果 D1/D2 快退能改善 2%冲击且不显著伤害 30bps，才保留。",
        "- 如果只改善少数年份或让 2024-2025 更差，则不应作为正式退出。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
