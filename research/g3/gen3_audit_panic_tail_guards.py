from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_panic_policy import (  # noqa: E402
    WINDOWS,
    _basket_curve,
    _display,
    _exit_date_map,
    _metrics,
    _trade_calendar,
)


def _tail_policy_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    deep = df["g3_chain"].eq("panic_v2_deep_wash_repair")
    lowpos = df["g3_position_guard"].eq("low_safety_margin")
    avoid_midrisk = ~df["g3_repair_env_label"].eq("mid_panic_failure_risk")
    base = deep & lowpos & avoid_midrisk

    not_overheated = ~df["g3_volume_context"].eq("overheated_volume")
    not_mom20_crash = pd.to_numeric(df["mom20"], errors="coerce").fillna(0.0) > -0.70
    not_amount_ratio_extreme = pd.to_numeric(df["amount_ratio20"], errors="coerce").fillna(0.0) <= 3.0

    return {
        "base_lowpos_avoid_midrisk": base,
        "base_no_overheated_volume": base & not_overheated,
        "base_no_mom20_le_70pct": base & not_mom20_crash,
        "base_no_overheated_no_mom20crash": base & not_overheated & not_mom20_crash,
        "base_no_all_tail_flags": base & not_overheated & not_mom20_crash & not_amount_ratio_extreme,
    }


def _select(df: pd.DataFrame, policy: str, max_per_day: int) -> pd.DataFrame:
    d = df[_tail_policy_masks(df)[policy]].copy()
    if d.empty:
        return d
    d["_score_sort"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(-1e9)
    d["_rank_sort"] = pd.to_numeric(d.get("chain_rank", 1e9), errors="coerce").fillna(1e9)
    d = d.sort_values(["entry_date", "_score_sort", "_rank_sort"], ascending=[True, False, True])
    return d.groupby("entry_date", group_keys=False).head(max_per_day).drop(columns=["_score_sort", "_rank_sort"])


def _worst_events(baskets: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    if baskets.empty:
        return baskets
    cols = ["entry_ts", "exit_date", "signals", "basket_ret", "gross_basket_ret", "win_rate", "equity", "drawdown"]
    return baskets.sort_values("basket_ret").head(n)[[c for c in cols if c in baskets.columns]].copy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit G3 panic tail guards.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/failure_env_audit_v1/failure_env_labeled_signals.parquet")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/tail_guard_audit_v1")
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=30.0)
    parser.add_argument("--max-per-day", type=int, default=5)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(args.input)
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    calendar = _trade_calendar("2020-01-01", "2026-12-31")
    exit_map = _exit_date_map(calendar, args.hold_days)

    rows: list[dict] = []
    worst_parts: list[pd.DataFrame] = []
    for policy in _tail_policy_masks(df):
        selected = _select(df, policy, args.max_per_day)
        trades, baskets = _basket_curve(selected, args.hold_days, args.cost_bps, exit_map)
        trades.to_csv(out_dir / f"{policy}_trades.csv", index=False, encoding="utf-8-sig")
        baskets.to_csv(out_dir / f"{policy}_basket_curve.csv", index=False, encoding="utf-8-sig")
        worst = _worst_events(baskets)
        if not worst.empty:
            worst.insert(0, "policy", policy)
            worst_parts.append(worst)
        for window_name, (start, end) in WINDOWS.items():
            start_ts = pd.Timestamp(start)
            end_ts = pd.Timestamp(end)
            tw = trades[(pd.to_datetime(trades["entry_date"]) >= start_ts) & (pd.to_datetime(trades["entry_date"]) <= end_ts)].copy()
            bw = baskets[(pd.to_datetime(baskets["entry_ts"]) >= start_ts) & (pd.to_datetime(baskets["entry_ts"]) <= end_ts)].copy()
            if not bw.empty:
                bw["equity"] = (1.0 + bw["basket_ret"]).cumprod()
                bw["drawdown"] = bw["equity"] / bw["equity"].cummax() - 1.0
            rows.append(_metrics(tw, bw, window_name, policy, f"top{args.max_per_day}_per_day", args.hold_days))

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "tail_guard_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "tail_guard_summary_display.csv", index=False, encoding="utf-8-sig")
    worst_all = pd.concat(worst_parts, ignore_index=True) if worst_parts else pd.DataFrame()
    worst_all.to_csv(out_dir / "worst_events.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    train = display[display["window"].eq("train_2020_2023")]
    lines = [
        "# G3 Panic 尾部风险闸门审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入：`{args.input}`",
        f"- 输出目录：`{out_dir}`",
        f"- 基础口径：`deep_wash_repair + range_pos60<=15% + avoid mid_panic_failure_risk`。",
        f"- 固定持有 `{args.hold_days}` 日，往返成本 `{args.cost_bps}` bps，每天最多 `{args.max_per_day}` 个信号。",
        "- 只测试少量粗闸门：剔除市场过热放量、剔除个股20日跌幅超过70%、剔除个股20日量比超过3。",
        "",
        "## full 对照",
        "",
        full[
            [
                "policy",
                "trades",
                "basket_days",
                "mean_basket_ret",
                "basket_win_rate",
                "total_compound_ret",
                "max_closed_basket_drawdown",
                "worst_basket",
            ]
        ].to_markdown(index=False),
        "",
        "## train 对照",
        "",
        train[
            [
                "policy",
                "trades",
                "basket_days",
                "mean_basket_ret",
                "basket_win_rate",
                "total_compound_ret",
                "max_closed_basket_drawdown",
                "worst_basket",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 如果剔除某个尾部标签后，full/train 回撤明显改善且收益没有只靠 valid/blind，那么它才有资格进入下一轮交易级回测。",
        "2. 如果收益显著下降但回撤改善有限，说明该标签不是好闸门。",
        "3. 这仍然不是正式策略，只是尾部风险归因。",
    ]
    (out_dir / "tail_guard_audit_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": args.input,
        "output_dir": str(out_dir),
        "rows": int(len(df)),
        "summary_rows": int(len(raw)),
        "hold_days": args.hold_days,
        "cost_bps": args.cost_bps,
        "max_per_day": args.max_per_day,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
