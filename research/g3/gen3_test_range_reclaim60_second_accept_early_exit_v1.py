from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_range_30m_volume_acceptance_v1 import PROFILES, md_table, simulate, summarize, window_metrics  # noqa: E402


SOURCE = _report_path() / "gen3_range_ice_recent3_second_accept_expanded_source_v1" / "reclaim60_second_accept_signals.csv"
OUT_DIR = _report_path() / "gen3_range_reclaim60_second_accept_early_exit_v1"

VARIANT = "reclaim60_second_accept"
VARIANT_CN = "日线收盘修复>=60%，并出现D0/D1二次30m承接"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
CURVE_END = "2026-06-04"


POLICIES = [
    ("base_hold5", "基准：固定持有到D5"),
    ("d2_negative_exit_d2", "D2转负则D2收盘退出"),
    ("d2_fade_exit_d2", "D2低于D1且D2修复不足1%，则D2收盘退出"),
    ("d3_negative_exit_d3", "D3仍为负则D3收盘退出"),
    ("d2_fade_or_d3_negative_exit", "先看D2修复衰减，否则D3仍弱退出"),
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def load_signals() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    for col in [
        "rank_key",
        "candidate_score",
        "entry_price_adjusted",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["rank_key"] = pd.to_numeric(d.get("rank_key", d.get("candidate_score", 0.0)), errors="coerce").fillna(0.0)
    d["hold_days"] = 5
    d["variant"] = VARIANT
    d["desc"] = VARIANT_CN
    return d.dropna(subset=["entry_date", "code", "entry_price_adjusted", "fwd_ret_confirm_to_close_5d"]).copy()


def exit_day_map(entry_dates: pd.Series, hold_days: int) -> dict[pd.Timestamp, pd.Timestamp]:
    start = pd.to_datetime(entry_dates).min()
    end = pd.to_datetime(entry_dates).max() + pd.Timedelta(days=30)
    cal = _trade_calendar(start, end)
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for idx, day in enumerate(cal):
        j = idx + hold_days - 1
        if j < len(cal):
            out[pd.Timestamp(day).normalize()] = pd.Timestamp(cal[j]).normalize()
    return out


def policy_decision(row: pd.Series, policy: str) -> tuple[int, float, str]:
    d1 = row["fwd_ret_confirm_to_close_1d"]
    d2 = row["fwd_ret_confirm_to_close_2d"]
    d3 = row["fwd_ret_confirm_to_close_3d"]
    d5 = row["fwd_ret_confirm_to_close_5d"]
    if policy == "base_hold5":
        return 5, d5, "持有到D5"
    if policy == "d2_negative_exit_d2" and pd.notna(d2) and d2 < 0:
        return 2, d2, "D2转负退出"
    if policy == "d2_fade_exit_d2" and pd.notna(d1) and pd.notna(d2) and d2 < d1 and d2 <= 0.01:
        return 2, d2, "D2修复衰减退出"
    if policy == "d3_negative_exit_d3" and pd.notna(d3) and d3 < 0:
        return 3, d3, "D3仍弱退出"
    if policy == "d2_fade_or_d3_negative_exit":
        if pd.notna(d1) and pd.notna(d2) and d2 < d1 and d2 <= 0.01:
            return 2, d2, "D2修复衰减退出"
        if pd.notna(d3) and d3 < 0:
            return 3, d3, "D3仍弱退出"
    return 5, d5, "持有到D5"


def build_candidates(signals: pd.DataFrame, profile: dict[str, Any], policy: str, policy_cn: str) -> pd.DataFrame:
    d = signals.copy()
    decisions = d.apply(lambda row: policy_decision(row, policy), axis=1)
    cost = float(profile["cost_bps"]) / 10000.0
    shock = float(profile["shock"])
    d["policy"] = policy
    d["policy_cn"] = policy_cn
    d["alt_hold_days"] = [item[0] for item in decisions]
    d["gross_policy_ret"] = [item[1] for item in decisions]
    d["exit_reason"] = [item[2] for item in decisions]
    d["policy_net_ret"] = pd.to_numeric(d["gross_policy_ret"], errors="coerce") - cost - shock
    d["entry_date_ts"] = d["entry_date"]
    d["entry_price_used"] = pd.to_numeric(d["entry_price_adjusted"], errors="coerce")
    d["policy_exit_date"] = pd.NaT
    for hold_days in sorted(d["alt_hold_days"].dropna().astype(int).unique()):
        mapper = exit_day_map(d["entry_date_ts"], hold_days)
        mask = d["alt_hold_days"].eq(hold_days)
        d.loc[mask, "policy_exit_date"] = d.loc[mask, "entry_date_ts"].map(mapper)
    return d.dropna(subset=["entry_date_ts", "policy_exit_date", "policy_net_ret", "entry_price_used"]).copy()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    signals = load_signals()
    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    exit_rows: list[dict[str, Any]] = []

    for policy, policy_cn in POLICIES:
        for profile in PROFILES:
            candidates = build_candidates(signals, profile, policy, policy_cn)
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{policy}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            variant = f"{VARIANT}__{policy}"
            row = summarize(curve, closed, variant, str(profile["profile"]), policy_cn)
            row["policy"] = policy
            row["policy_cn"] = policy_cn
            row["changed_count"] = int(closed["exit_reason"].ne("持有到D5").sum()) if not closed.empty and "exit_reason" in closed else 0
            summary_rows.append(row)
            window_rows.extend(window_metrics(curve, closed, variant, str(profile["profile"])))
            if str(profile["profile"]) == "cost30" and not closed.empty:
                for reason, g in closed.groupby("exit_reason", dropna=False):
                    rets = pd.to_numeric(g["policy_net_ret"], errors="coerce")
                    exit_rows.append(
                        {
                            "policy": policy,
                            "exit_reason": reason,
                            "trade_count": int(len(g)),
                            "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                            "avg_return": float(rets.mean()) if len(rets) else 0.0,
                            "worst_return": float(rets.min()) if len(rets) else 0.0,
                        }
                    )

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    exits = pd.DataFrame(exit_rows)
    summary.insert(0, "signal_start", SIGNAL_START)
    summary.insert(1, "signal_end", SIGNAL_END)
    summary.insert(2, "curve_end", CURVE_END)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    exits.to_csv(OUT_DIR / "exit_reason_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return", "avg_return", "worst_return"}
    cost30 = summary[summary["profile"].eq("cost30")].sort_values(["total_return", "max_drawdown"], ascending=[False, False]).copy()
    pressure = summary[summary["profile"].isin(["cost100", "shock2_cost30"])].copy()
    report = [
        "# G3 reclaim60_second_accept 早期弱确认退出复验 v1",
        "",
        "## 回测范围",
        f"- 英文名：`{VARIANT}`。",
        f"- 中文解释：{VARIANT_CN}。",
        f"- 候选信号入场窗口：{SIGNAL_START} 至 {SIGNAL_END}；资金曲线结算至 {CURVE_END}；样本 {len(signals)} 笔。",
        "- 本轮只改退出，不改入场，不使用 score/rank 二次过滤。",
        "",
        "## 30bps结果",
        md_table(cost30, pct_cols=pct_cols),
        "",
        "## 压力口径",
        md_table(pressure, pct_cols=pct_cols),
        "",
        "## 分窗口",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 退出原因统计（30bps）",
        md_table(exits, pct_cols=pct_cols),
        "",
        "## 判断口径",
        "- 如果早期退出能显著改善 2024-2025 和 2%冲击口径，同时不大幅牺牲全周期收益，才说明退出有研究价值。",
        "- 如果只是降低回撤但把收益来源切掉，下一步应回到候选源质量，而不是继续调退出。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    result = {
        "variant": VARIANT,
        "variant_cn": VARIANT_CN,
        "signal_start": SIGNAL_START,
        "signal_end": SIGNAL_END,
        "curve_end": CURVE_END,
        "trade_count": int(len(signals)),
        "best_cost30": cost30.head(3).to_dict(orient="records"),
    }
    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
