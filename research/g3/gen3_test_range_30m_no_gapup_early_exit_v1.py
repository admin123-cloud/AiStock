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

from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar
from scripts.gen3_test_range_30m_volume_acceptance_v1 import INITIAL_CAPITAL, simulate, summarize, window_metrics


SOURCE_DIR = _report_path() / "gen3_range_30m_true_acceptance_structure_v1"
RUN_DIR = SOURCE_DIR / "box_stress_accept_h5__no_gapup_strong_bar__cost30"
OUT_DIR = _report_path() / "gen3_range_30m_no_gapup_early_exit_v1"

VARIANT = "box_stress_accept_h5__no_gapup_strong_bar"
VARIANT_CN = "横盘箱体底部不追高30m强承接"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
COST = 0.003


POLICIES = [
    {
        "policy": "base_hold5",
        "policy_cn": "基准：固定持有到D5",
    },
    {
        "policy": "d2_negative_exit_d2",
        "policy_cn": "D2转负则D2收盘退出",
    },
    {
        "policy": "d2_fade_exit_d2",
        "policy_cn": "D2低于D1且D2修复不足1%则D2收盘退出",
    },
    {
        "policy": "d3_negative_exit_d3",
        "policy_cn": "D3仍为负则D3收盘退出",
    },
    {
        "policy": "d2_fade_or_d3_negative_exit",
        "policy_cn": "先看D2修复衰减，未触发再看D3转负退出",
    },
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


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
        return 5, d5 - COST, "持有到D5"
    if policy == "d2_negative_exit_d2":
        if pd.notna(d2) and d2 < 0:
            return 2, d2 - COST, "D2转负，D2收盘退出"
        return 5, d5 - COST, "持有到D5"
    if policy == "d2_fade_exit_d2":
        if pd.notna(d1) and pd.notna(d2) and d2 < d1 and d2 <= 0.01:
            return 2, d2 - COST, "D2低于D1且修复不足，D2收盘退出"
        return 5, d5 - COST, "持有到D5"
    if policy == "d3_negative_exit_d3":
        if pd.notna(d3) and d3 < 0:
            return 3, d3 - COST, "D3仍为负，D3收盘退出"
        return 5, d5 - COST, "持有到D5"
    if policy == "d2_fade_or_d3_negative_exit":
        if pd.notna(d1) and pd.notna(d2) and d2 < d1 and d2 <= 0.01:
            return 2, d2 - COST, "D2低于D1且修复不足，D2收盘退出"
        if pd.notna(d3) and d3 < 0:
            return 3, d3 - COST, "D3仍为负，D3收盘退出"
        return 5, d5 - COST, "持有到D5"
    raise ValueError(policy)


def run_policy(base: pd.DataFrame, policy_spec: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    policy = policy_spec["policy"]
    d = base.copy()
    decisions = d.apply(lambda row: policy_decision(row, policy), axis=1)
    d["policy"] = policy
    d["policy_cn"] = policy_spec["policy_cn"]
    d["alt_hold_days"] = [item[0] for item in decisions]
    d["policy_net_ret"] = [item[1] for item in decisions]
    d["exit_reason"] = [item[2] for item in decisions]
    d["ret_delta_vs_base"] = d["policy_net_ret"] - base["policy_net_ret"]
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.NaT
    for hold in sorted(d["alt_hold_days"].dropna().astype(int).unique()):
        mapper = exit_day_map(d["entry_date_ts"], hold)
        mask = d["alt_hold_days"].eq(hold)
        d.loc[mask, "policy_exit_date"] = d.loc[mask, "entry_date_ts"].map(mapper)
    curve, closed = simulate(d)
    return d, curve, closed


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = pd.read_csv(RUN_DIR / "closed_trades.csv", low_memory=False)
    base = numeric(
        base,
        [
            "policy_net_ret",
            "realized_pnl",
            "entry_price_used",
            "stake",
            "amount_ratio3",
            "bar_close_pos",
            "bar_ret",
            "range_pos60",
            "close_position",
            "gap_open",
            "index_mom20",
            "fwd_ret_confirm_to_close_1d",
            "fwd_ret_confirm_to_close_2d",
            "fwd_ret_confirm_to_close_3d",
            "fwd_ret_confirm_to_close_5d",
            "fwd_ret_confirm_to_close_10d",
            "fwd_ret_confirm_to_close_20d",
        ],
    )
    base["entry_date_ts"] = pd.to_datetime(base["entry_date"], errors="coerce").dt.normalize()
    if "rank_key" not in base.columns:
        base["rank_key"] = pd.to_numeric(base.get("candidate_score", 0.0), errors="coerce").fillna(0.0)

    summary_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    for policy_spec in POLICIES:
        candidates, curve, closed = run_policy(base, policy_spec)
        run_dir = OUT_DIR / policy_spec["policy"]
        run_dir.mkdir(parents=True, exist_ok=True)
        candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        row = summarize(curve, closed, policy_spec["policy"], "cost30", policy_spec["policy_cn"])
        row["changed_count"] = int((closed.get("exit_reason", pd.Series(dtype=str)) != "持有到D5").sum()) if not closed.empty else 0
        summary_rows.append(row)
        window_rows.extend(window_metrics(curve, closed, policy_spec["policy"], "cost30"))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    best = summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).iloc[0].to_dict()
    result = {
        "variant": VARIANT,
        "variant_cn": VARIANT_CN,
        "signal_backtest_start": SIGNAL_START,
        "signal_backtest_end": SIGNAL_END,
        "trade_count": int(len(base)),
        "best_by_return": best,
        "summary": summary.to_dict(orient="records"),
    }
    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 横盘箱体底部不追高30m强承接早期退出复验",
        "",
        "## 口径说明",
        f"- 英文名：`{VARIANT}`。",
        "- 中文含义：横盘箱体底部候选里，只保留不追高的30m强承接。",
        f"- 信号回测窗口：{SIGNAL_START} 至 {SIGNAL_END}；交易数 {len(base)} 笔；成本口径 30bps。",
        "- 本次只改退出，不改入场，不使用 score/rank 再过滤。",
        "",
        "## 策略对照",
    ]
    for _, row in summary.iterrows():
        lines.append(
            f"- `{row['variant']}`：{row['desc']}；{int(row['trade_count'])} 笔，收益 {pct(row['total_return'])}，回撤 {pct(row['max_drawdown'])}，胜率 {pct(row['win_rate'])}，均笔 {pct(row['avg_trade_return'])}，最差 {pct(row['worst_trade'])}，提前退出 {int(row['changed_count'])} 笔。"
        )
    lines.extend(
        [
            "",
            "## 初步判断",
            f"- 收益最高的是 `{best['variant']}`，收益 {pct(best['total_return'])}，回撤 {pct(best['max_drawdown'])}。",
            "- 如果早期退出能改善验证窗和最差单笔，但明显牺牲全样本收益，需要继续看是不是错杀 2026 的大收益票。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
