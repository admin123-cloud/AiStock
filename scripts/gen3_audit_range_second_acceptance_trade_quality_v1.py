from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "reports" / "gen3_range_second_acceptance_structure_veto_v1"
VARIANT = "deep_and_reclaim_any_second_accept__not_floor_not_strong_unless_extreme_volume"
PROFILE = "cost30"
RUN_DIR = SOURCE_DIR / f"{VARIANT}__{PROFILE}"
OUT_DIR = PROJECT_ROOT / "reports" / "gen3_range_second_acceptance_trade_quality_audit_v1"


def pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{value * 100:+.2f}%"


def money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{value:,.0f}"


def numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def summarize_group(df: pd.DataFrame, by: str) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(by, dropna=False, sort=True):
        rets = g["policy_net_ret"].dropna()
        pnl = g["realized_pnl"].dropna()
        rows.append(
            {
                by: key,
                "trade_count": int(len(g)),
                "win_rate": float((rets > 0).mean()) if len(rets) else None,
                "avg_return": float(rets.mean()) if len(rets) else None,
                "sum_pnl": float(pnl.sum()) if len(pnl) else None,
                "worst_return": float(rets.min()) if len(rets) else None,
                "best_return": float(rets.max()) if len(rets) else None,
                "ice_trades": int(g["emotion_signal"].astype(str).str.contains("ice", case=False, na=False).sum())
                if "emotion_signal" in g.columns
                else 0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_path = RUN_DIR / "closed_trades.csv"
    curve_path = RUN_DIR / "mtm_equity_curve.csv"
    summary_path = SOURCE_DIR / "summary.csv"
    concentration_path = SOURCE_DIR / "concentration.csv"

    trades = pd.read_csv(trades_path, low_memory=False)
    trades = numeric(
        trades,
        [
            "policy_net_ret",
            "realized_pnl",
            "entry_price_used",
            "stake",
            "range_pos60",
            "close_position",
            "amount_ratio3",
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
    trades["entry_dt"] = pd.to_datetime(trades["entry_date"], errors="coerce")
    trades["year"] = trades["entry_dt"].dt.year
    trades["is_recent_2024_2026"] = trades["entry_dt"].dt.date >= pd.Timestamp("2024-01-01").date()
    trades["is_loss"] = trades["policy_net_ret"] < 0
    trades["accept_stage"] = trades.apply(
        lambda row: "D0二次承接" if str(row.get("d0_second_accept")) == "True" else "D1二次承接"
        if str(row.get("d1_second_accept")) == "True"
        else "未标记",
        axis=1,
    )

    curve = pd.read_csv(curve_path, low_memory=False)
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce")
    curve["equity"] = pd.to_numeric(curve["equity"], errors="coerce")
    curve_start = curve["date"].min()
    curve_end = curve["date"].max()

    yearly = summarize_group(trades, "year")
    recent = summarize_group(trades[trades["is_recent_2024_2026"]], "year")
    accept_stage = summarize_group(trades, "accept_stage")
    emotion = summarize_group(trades, "emotion_signal") if "emotion_signal" in trades.columns else pd.DataFrame()
    source_window = summarize_group(trades, "source_desc") if "source_desc" in trades.columns else pd.DataFrame()

    top_trades = trades.sort_values("realized_pnl", ascending=False).head(10)
    loss_trades = trades[trades["policy_net_ret"] < 0].sort_values("policy_net_ret")
    recent_trades = trades[trades["is_recent_2024_2026"]].sort_values("entry_dt")

    keep_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "policy_net_ret",
        "realized_pnl",
        "emotion_signal",
        "source_desc",
        "accept_stage",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "gap_open",
        "index_mom20",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_5d",
        "fwd_ret_confirm_to_close_10d",
        "fwd_ret_confirm_to_close_20d",
    ]
    keep_cols = [col for col in keep_cols if col in trades.columns]

    yearly.to_csv(OUT_DIR / "yearly_quality.csv", index=False, encoding="utf-8-sig")
    recent.to_csv(OUT_DIR / "recent_2024_2026_quality.csv", index=False, encoding="utf-8-sig")
    accept_stage.to_csv(OUT_DIR / "accept_stage_quality.csv", index=False, encoding="utf-8-sig")
    emotion.to_csv(OUT_DIR / "emotion_quality.csv", index=False, encoding="utf-8-sig")
    source_window.to_csv(OUT_DIR / "source_window_quality.csv", index=False, encoding="utf-8-sig")
    top_trades[keep_cols].to_csv(OUT_DIR / "top_trades.csv", index=False, encoding="utf-8-sig")
    loss_trades[keep_cols].to_csv(OUT_DIR / "loss_trades.csv", index=False, encoding="utf-8-sig")
    recent_trades[keep_cols].to_csv(OUT_DIR / "recent_2024_2026_trades.csv", index=False, encoding="utf-8-sig")
    trades.sort_values("entry_dt")[keep_cols].to_csv(OUT_DIR / "all_trades_quality.csv", index=False, encoding="utf-8-sig")

    total_pnl = float(trades["realized_pnl"].sum())
    top1 = float(top_trades["realized_pnl"].head(1).sum())
    top3 = float(top_trades["realized_pnl"].head(3).sum())
    top5 = float(top_trades["realized_pnl"].head(5).sum())
    recent_rets = recent_trades["policy_net_ret"].dropna()
    losses = loss_trades["policy_net_ret"].dropna()
    summary_rows = pd.read_csv(summary_path, low_memory=False)
    selected = summary_rows[(summary_rows["variant"] == VARIANT) & (summary_rows["profile"] == PROFILE)]
    selected_summary = selected.iloc[0].to_dict() if not selected.empty else {}
    concentration = pd.read_csv(concentration_path, low_memory=False)
    selected_conc = concentration[
        (concentration["source_variant"] == "deep_and_reclaim_any_second_accept")
        & (concentration["rule"] == "not_floor_not_strong_unless_extreme_volume")
    ]
    selected_conc_row = selected_conc.iloc[0].to_dict() if not selected_conc.empty else {}

    audit_summary = {
        "variant": VARIANT,
        "variant_cn": "横盘/冰点二次承接结构否决版",
        "profile": PROFILE,
        "profile_cn": "30bps交易成本口径",
        "signal_backtest_start": "2020-01-01",
        "signal_backtest_end": "2026-05-29",
        "curve_start": curve_start.strftime("%Y-%m-%d") if pd.notna(curve_start) else None,
        "curve_end": curve_end.strftime("%Y-%m-%d") if pd.notna(curve_end) else None,
        "trade_count": int(len(trades)),
        "total_return": float(selected_summary.get("total_return", float("nan"))),
        "max_drawdown": float(selected_summary.get("max_drawdown", float("nan"))),
        "win_rate": float(selected_summary.get("win_rate", float("nan"))),
        "total_pnl": total_pnl,
        "top1_pnl_share": top1 / total_pnl if total_pnl else None,
        "top3_pnl_share": top3 / total_pnl if total_pnl else None,
        "top5_pnl_share": top5 / total_pnl if total_pnl else None,
        "loss_trade_count": int(len(loss_trades)),
        "worst_loss": float(losses.min()) if len(losses) else None,
        "recent_2024_2026_trade_count": int(len(recent_trades)),
        "recent_2024_2026_win_rate": float((recent_rets > 0).mean()) if len(recent_rets) else None,
        "recent_2024_2026_avg_return": float(recent_rets.mean()) if len(recent_rets) else None,
        "top3_removed_pnl": selected_conc_row.get("top3_removed_pnl"),
    }
    (OUT_DIR / "audit_summary.json").write_text(json.dumps(audit_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 横盘/冰点二次承接结构否决版成交质量审计",
        "",
        "## 口径说明",
        f"- 英文名：`{VARIANT}`。",
        "- 中文含义：`deep_and_reclaim_any_second_accept` 是箱体底部且日线修复，D0 后半日或 D1 任一出现 30m 放量承接；`not_floor_not_strong_unless_extreme_volume` 是否决既不够贴近箱体底部、日线修复也不够强的样本，除非 30m 极端放量。",
        f"- 成本口径：`{PROFILE}`，即 30bps 交易成本。",
        "- 信号回测窗口：2020-01-01 至 2026-05-29。",
        f"- 实际曲线窗口：{audit_summary['curve_start']} 至 {audit_summary['curve_end']}。",
        "",
        "## 核心结论",
        f"- 全样本 17 笔，30bps 成本口径收益 {pct(audit_summary['total_return'])}，最大回撤 {pct(audit_summary['max_drawdown'])}，胜率 {pct(audit_summary['win_rate'])}。",
        f"- 收益集中度仍高：Top1 PnL 占 {pct(audit_summary['top1_pnl_share'])}，Top3 占 {pct(audit_summary['top3_pnl_share'])}，Top5 占 {pct(audit_summary['top5_pnl_share'])}。",
        f"- 亏损票 {audit_summary['loss_trade_count']} 笔，最差单笔 {pct(audit_summary['worst_loss'])}。",
        f"- 2024-2026 合计 {audit_summary['recent_2024_2026_trade_count']} 笔，平均单笔 {pct(audit_summary['recent_2024_2026_avg_return'])}，胜率 {pct(audit_summary['recent_2024_2026_win_rate'])}。",
        "",
        "## 年度表现",
    ]
    for _, row in yearly.iterrows():
        lines.append(
            f"- {int(row['year'])}：{int(row['trade_count'])} 笔，PnL {money(row['sum_pnl'])}，均笔 {pct(row['avg_return'])}，胜率 {pct(row['win_rate'])}，最差 {pct(row['worst_return'])}。"
        )
    lines.extend(["", "## 亏损票"])
    if loss_trades.empty:
        lines.append("- 没有亏损票。")
    else:
        for _, row in loss_trades.iterrows():
            lines.append(
                f"- {row['entry_date']} {row['name']}({row['code']})：{pct(row['policy_net_ret'])}，{row['accept_stage']}，情绪 {row.get('emotion_signal', '--')}，箱体位置 {pct(row.get('range_pos60'))}，收盘修复 {pct(row.get('close_position'))}，30m量比 {row.get('amount_ratio3', None):.2f}。"
            )
    lines.extend(
        [
            "",
            "## 下一步建议",
            "- 这个标签可以继续作为横盘/冰点候选源观察，但不能升为正式买点：样本只有 17 笔，且收益集中度偏高。",
            "- 下一步应扩大同类候选源，而不是调阈值：寻找更多 30m 真实放量承接样本，重点检查 2024-2026 是否仍能独立贡献。",
            "- 对亏损票优先做入场后 D1/D2 弱确认退出审计，看是否能压住失败形态，而不是继续优化入场 score/rank。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(audit_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
