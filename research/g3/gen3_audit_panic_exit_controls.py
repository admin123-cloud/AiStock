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

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_panic_policy import (  # noqa: E402
    WINDOWS,
    _display,
    _exit_date_map,
    _metrics,
    _trade_calendar,
)


def _base_mask(df: pd.DataFrame) -> pd.Series:
    return (
        df["g3_chain"].eq("panic_v2_deep_wash_repair")
        & df["g3_position_guard"].eq("low_safety_margin")
        & ~df["g3_repair_env_label"].eq("mid_panic_failure_risk")
    )


def _select(df: pd.DataFrame, max_per_day: int) -> pd.DataFrame:
    d = df[_base_mask(df)].copy()
    d["_score_sort"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(-1e9)
    d["_rank_sort"] = pd.to_numeric(d.get("chain_rank", 1e9), errors="coerce").fillna(1e9)
    d = d.sort_values(["entry_date", "_score_sort", "_rank_sort"], ascending=[True, False, True])
    return d.groupby("entry_date", group_keys=False).head(max_per_day).drop(columns=["_score_sort", "_rank_sort"])


def _apply_exit_rule(trades: pd.DataFrame, rule: str) -> pd.DataFrame:
    d = trades.copy()
    r1 = pd.to_numeric(d["fwd_ret_confirm_to_close_1d"], errors="coerce")
    r2 = pd.to_numeric(d["fwd_ret_confirm_to_close_2d"], errors="coerce")
    r3 = pd.to_numeric(d["fwd_ret_confirm_to_close_3d"], errors="coerce")
    r5 = pd.to_numeric(d["fwd_ret_confirm_to_close_5d"], errors="coerce")

    d["exit_rule"] = rule
    if rule == "hold5_fixed":
        d["chosen_hold_days"] = 5
        d["chosen_ret"] = r5
    elif rule == "stop1d_m5_else_hold5":
        stop1 = r1 <= -0.05
        d["chosen_hold_days"] = 5
        d.loc[stop1, "chosen_hold_days"] = 1
        d["chosen_ret"] = r5
        d.loc[stop1, "chosen_ret"] = r1[stop1]
    elif rule == "stop1d_m5_stop2d_m8_else_hold5":
        stop1 = r1 <= -0.05
        stop2 = (~stop1) & (r2 <= -0.08)
        d["chosen_hold_days"] = 5
        d.loc[stop1, "chosen_hold_days"] = 1
        d.loc[stop2, "chosen_hold_days"] = 2
        d["chosen_ret"] = r5
        d.loc[stop1, "chosen_ret"] = r1[stop1]
        d.loc[stop2, "chosen_ret"] = r2[stop2]
    elif rule == "stop1d_m5_stop2d_m8_stop3d_m10_else_hold5":
        stop1 = r1 <= -0.05
        stop2 = (~stop1) & (r2 <= -0.08)
        stop3 = (~stop1) & (~stop2) & (r3 <= -0.10)
        d["chosen_hold_days"] = 5
        d.loc[stop1, "chosen_hold_days"] = 1
        d.loc[stop2, "chosen_hold_days"] = 2
        d.loc[stop3, "chosen_hold_days"] = 3
        d["chosen_ret"] = r5
        d.loc[stop1, "chosen_ret"] = r1[stop1]
        d.loc[stop2, "chosen_ret"] = r2[stop2]
        d.loc[stop3, "chosen_ret"] = r3[stop3]
    else:
        raise ValueError(f"Unknown rule: {rule}")
    return d


def _curve(trades: pd.DataFrame, cost_bps: float, exit_maps: dict[int, dict[pd.Timestamp, pd.Timestamp]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = trades.dropna(subset=["chosen_ret"]).copy()
    d["entry_ts"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d["chosen_hold_days"] = d["chosen_hold_days"].astype(int)
    d["exit_date"] = [
        exit_maps[int(h)].get(pd.Timestamp(e).normalize())
        for e, h in zip(d["entry_ts"], d["chosen_hold_days"])
    ]
    d = d.dropna(subset=["exit_date"]).copy()
    d["gross_ret"] = pd.to_numeric(d["chosen_ret"], errors="coerce")
    d["net_ret"] = d["gross_ret"] - cost_bps / 10000.0
    baskets = (
        d.groupby(["entry_ts", "exit_date"], as_index=False)
        .agg(
            signals=("code", "size"),
            unique_codes=("code", "nunique"),
            basket_ret=("net_ret", "mean"),
            gross_basket_ret=("gross_ret", "mean"),
            win_rate=("net_ret", lambda s: float((s > 0).mean())),
        )
        .sort_values("exit_date")
    )
    if not baskets.empty:
        baskets["equity"] = (1.0 + baskets["basket_ret"]).cumprod()
        baskets["drawdown"] = baskets["equity"] / baskets["equity"].cummax() - 1.0
    return d, baskets


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit G3 panic exit controls.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/failure_env_audit_v1/failure_env_labeled_signals.parquet")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/exit_control_audit_v1")
    parser.add_argument("--cost-bps", type=float, default=30.0)
    parser.add_argument("--max-per-day", type=int, default=5)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(args.input)
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    selected = _select(df, args.max_per_day)
    calendar = _trade_calendar("2020-01-01", "2026-12-31")
    exit_maps = {h: _exit_date_map(calendar, h) for h in [1, 2, 3, 5]}

    rules = [
        "hold5_fixed",
        "stop1d_m5_else_hold5",
        "stop1d_m5_stop2d_m8_else_hold5",
        "stop1d_m5_stop2d_m8_stop3d_m10_else_hold5",
    ]
    rows: list[dict] = []
    for rule in rules:
        trades = _apply_exit_rule(selected, rule)
        trades, baskets = _curve(trades, args.cost_bps, exit_maps)
        trades.to_csv(out_dir / f"{rule}_trades.csv", index=False, encoding="utf-8-sig")
        baskets.to_csv(out_dir / f"{rule}_basket_curve.csv", index=False, encoding="utf-8-sig")
        for window_name, (start, end) in WINDOWS.items():
            start_ts = pd.Timestamp(start)
            end_ts = pd.Timestamp(end)
            tw = trades[(pd.to_datetime(trades["entry_date"]) >= start_ts) & (pd.to_datetime(trades["entry_date"]) <= end_ts)].copy()
            bw = baskets[(pd.to_datetime(baskets["entry_ts"]) >= start_ts) & (pd.to_datetime(baskets["entry_ts"]) <= end_ts)].copy()
            if not bw.empty:
                bw["equity"] = (1.0 + bw["basket_ret"]).cumprod()
                bw["drawdown"] = bw["equity"] / bw["equity"].cummax() - 1.0
            item = _metrics(tw, bw, window_name, rule, f"top{args.max_per_day}_per_day", 5)
            item["stop_1d_count"] = int((tw.get("chosen_hold_days", pd.Series(dtype=int)) == 1).sum()) if not tw.empty else 0
            item["stop_2d_count"] = int((tw.get("chosen_hold_days", pd.Series(dtype=int)) == 2).sum()) if not tw.empty else 0
            item["stop_3d_count"] = int((tw.get("chosen_hold_days", pd.Series(dtype=int)) == 3).sum()) if not tw.empty else 0
            rows.append(item)

    raw = pd.DataFrame(rows).rename(columns={"policy": "exit_rule"})
    raw.to_csv(out_dir / "exit_control_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "exit_control_summary_display.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    train = display[display["window"].eq("train_2020_2023")]
    lines = [
        "# G3 Panic 失败退出审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入：`{args.input}`",
        f"- 输出目录：`{out_dir}`",
        "- 候选：`deep_lowpos_avoid_midrisk`，每天最多前 5 个信号。",
        f"- 成本：往返 `{args.cost_bps}` bps。",
        "- 退出规则为研究用近似：用买入后第 1/2/3/5 个交易日收盘收益判断，不含盘中止损成交质量。",
        "",
        "## full 对照",
        "",
        full[
            [
                "exit_rule",
                "trades",
                "basket_days",
                "mean_basket_ret",
                "basket_win_rate",
                "total_compound_ret",
                "max_closed_basket_drawdown",
                "worst_basket",
                "stop_1d_count",
                "stop_2d_count",
                "stop_3d_count",
            ]
        ].to_markdown(index=False),
        "",
        "## train 对照",
        "",
        train[
            [
                "exit_rule",
                "trades",
                "basket_days",
                "mean_basket_ret",
                "basket_win_rate",
                "total_compound_ret",
                "max_closed_basket_drawdown",
                "worst_basket",
                "stop_1d_count",
                "stop_2d_count",
                "stop_3d_count",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 如果失败退出显著压缩 worst basket 和 train 回撤，同时不过度牺牲 full/valid/blind 收益，则下一轮应进入交易级回测。",
        "2. 该审计仍使用日收盘近似止损，不代表实盘一定能成交；正式化前必须改成分钟级可见止损。",
    ]
    (out_dir / "exit_control_audit_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "input": args.input,
        "output_dir": str(out_dir),
        "rows": int(len(df)),
        "selected_rows": int(len(selected)),
        "summary_rows": int(len(raw)),
        "cost_bps": args.cost_bps,
        "max_per_day": args.max_per_day,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
