from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
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


def _base_mask(df: pd.DataFrame) -> pd.Series:
    return (
        df["g3_chain"].eq("panic_v2_deep_wash_repair")
        & df["g3_position_guard"].eq("low_safety_margin")
        & ~df["g3_repair_env_label"].eq("mid_panic_failure_risk")
    )


def _add_daily_context(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    base = out[_base_mask(out)].copy()
    daily = (
        base.groupby("entry_date", as_index=False)
        .agg(
            base_signal_count=("code", "size"),
            day_breadth_ma20=("breadth_ma20", "median"),
            day_index_mom20=("index_mom20", "median"),
            day_market_amount_ratio20=("market_amount_ratio20", "median"),
            day_up_rate=("up_rate", "median"),
        )
    )
    return out.merge(daily, on="entry_date", how="left")


def _policy_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    base = _base_mask(df)
    dry_stress = (
        (pd.to_numeric(df["day_breadth_ma20"], errors="coerce") < 0.30)
        & (pd.to_numeric(df["day_index_mom20"], errors="coerce") < -0.03)
        & (pd.to_numeric(df["day_market_amount_ratio20"], errors="coerce") < 1.00)
    )
    deep_dry_stress = (
        (pd.to_numeric(df["day_breadth_ma20"], errors="coerce") < 0.20)
        & (pd.to_numeric(df["day_index_mom20"], errors="coerce") < -0.05)
        & (pd.to_numeric(df["day_market_amount_ratio20"], errors="coerce") < 1.00)
    )
    crowded_dry_stress = (
        (pd.to_numeric(df["base_signal_count"], errors="coerce") >= 3)
        & (pd.to_numeric(df["day_breadth_ma20"], errors="coerce") < 0.30)
        & (pd.to_numeric(df["day_market_amount_ratio20"], errors="coerce") < 1.00)
    )
    weak_no_capitulation = (
        (pd.to_numeric(df["day_breadth_ma20"], errors="coerce") < 0.30)
        & (pd.to_numeric(df["big_down_rate"], errors="coerce") < 0.20)
        & (pd.to_numeric(df["day_market_amount_ratio20"], errors="coerce") < 1.00)
    )
    return {
        "base": base,
        "pause_dry_stress": base & ~dry_stress,
        "pause_deep_dry_stress": base & ~deep_dry_stress,
        "pause_crowded_dry_stress": base & ~crowded_dry_stress,
        "pause_weak_no_capitulation": base & ~weak_no_capitulation,
    }


def _select(df: pd.DataFrame, policy: str, max_per_day: int) -> pd.DataFrame:
    d = df[_policy_masks(df)[policy]].copy()
    if d.empty:
        return d
    d["_score_sort"] = pd.to_numeric(d.get("candidate_score", 0.0), errors="coerce").fillna(-1e9)
    d["_rank_sort"] = pd.to_numeric(d.get("chain_rank", 1e9), errors="coerce").fillna(1e9)
    d = d.sort_values(["entry_date", "_score_sort", "_rank_sort"], ascending=[True, False, True])
    return d.groupby("entry_date", group_keys=False).head(max_per_day).drop(columns=["_score_sort", "_rank_sort"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit G3 panic systemic pause rules.")
    parser.add_argument("--input", default="reports/gen3_panic_v2_research/failure_env_audit_v1/failure_env_labeled_signals.parquet")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/systemic_pause_audit_v1")
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=30.0)
    parser.add_argument("--max-per-day", type=int, default=5)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(args.input)
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df = _add_daily_context(df)
    df.to_parquet(out_dir / "systemic_pause_labeled_signals.parquet", index=False)
    df.to_csv(out_dir / "systemic_pause_labeled_signals.csv", index=False, encoding="utf-8-sig")

    calendar = _trade_calendar("2020-01-01", "2026-12-31")
    exit_map = _exit_date_map(calendar, args.hold_days)

    rows: list[dict] = []
    skipped_rows: list[dict] = []
    masks = _policy_masks(df)
    base_selected = set(_select(df, "base", args.max_per_day).index.tolist())
    for policy in masks:
        selected = _select(df, policy, args.max_per_day)
        selected_indexes = set(selected.index.tolist())
        skipped = sorted(base_selected - selected_indexes)
        if skipped:
            s = df.loc[skipped].copy()
            s.insert(0, "policy", policy)
            skipped_rows.append(s)
        trades, baskets = _basket_curve(selected, args.hold_days, args.cost_bps, exit_map)
        trades.to_csv(out_dir / f"{policy}_trades.csv", index=False, encoding="utf-8-sig")
        baskets.to_csv(out_dir / f"{policy}_basket_curve.csv", index=False, encoding="utf-8-sig")
        for window_name, (start, end) in WINDOWS.items():
            start_ts = pd.Timestamp(start)
            end_ts = pd.Timestamp(end)
            tw = trades[(pd.to_datetime(trades["entry_date"]) >= start_ts) & (pd.to_datetime(trades["entry_date"]) <= end_ts)].copy()
            bw = baskets[(pd.to_datetime(baskets["entry_ts"]) >= start_ts) & (pd.to_datetime(baskets["entry_ts"]) <= end_ts)].copy()
            if not bw.empty:
                bw["equity"] = (1.0 + bw["basket_ret"]).cumprod()
                bw["drawdown"] = bw["equity"] / bw["equity"].cummax() - 1.0
            item = _metrics(tw, bw, window_name, policy, f"top{args.max_per_day}_per_day", args.hold_days)
            item["skipped_from_base"] = len(skipped)
            rows.append(item)

    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "systemic_pause_summary_raw.csv", index=False, encoding="utf-8-sig")
    display = _display(raw)
    display.to_csv(out_dir / "systemic_pause_summary_display.csv", index=False, encoding="utf-8-sig")
    skipped_all = pd.concat(skipped_rows, ignore_index=True) if skipped_rows else pd.DataFrame()
    skipped_all.to_csv(out_dir / "skipped_signals.csv", index=False, encoding="utf-8-sig")

    full = display[display["window"].eq("full")]
    train = display[display["window"].eq("train_2020_2023")]
    lines = [
        "# G3 Panic 系统性风险暂停审计 V1",
        "",
        "## 口径",
        "",
        f"- 输入：`{args.input}`",
        f"- 输出目录：`{out_dir}`",
        "- 基础口径：`deep_lowpos_avoid_midrisk`，每天最多前 5 个信号，持有 5 日，扣 30bps 成本。",
        "- 本轮只验证少量可解释暂停标签，不做参数网格搜索。",
        "",
        "## 暂停标签",
        "",
        "- `pause_dry_stress`：宽度弱、指数 20 日动量弱、市场成交未释放。",
        "- `pause_deep_dry_stress`：更极端的宽度弱和动量弱。",
        "- `pause_crowded_dry_stress`：同日基础信号密集，同时宽度弱且成交未释放。",
        "- `pause_weak_no_capitulation`：宽度弱、成交未释放，且没有足够大跌出清。",
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
                "skipped_from_base",
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
                "skipped_from_base",
            ]
        ].to_markdown(index=False),
        "",
        "## 判断",
        "",
        "1. 能进入下一轮的暂停规则，必须在 train 中改善回撤，同时不能只靠删除大量样本获得漂亮 full。",
        "2. 如果规则删掉了 2024/2025 的高收益恐慌修复，则说明它过度防守，不适合作为 G3 主线。",
    ]
    (out_dir / "systemic_pause_audit_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

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
