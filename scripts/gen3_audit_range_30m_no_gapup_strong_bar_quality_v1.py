from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "reports" / "gen3_range_30m_true_acceptance_structure_v1"
RUN_DIR = SOURCE_DIR / "box_stress_accept_h5__no_gapup_strong_bar__cost30"
OUT_DIR = ROOT / "reports" / "gen3_range_30m_no_gapup_strong_bar_quality_audit_v1"

VARIANT = "box_stress_accept_h5__no_gapup_strong_bar"
VARIANT_CN = "横盘箱体底部不追高30m强承接"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
PROFILE = "cost30"
PROFILE_CN = "30bps交易成本口径"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):,.0f}"


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
                "loss_count": int((rets < 0).sum()) if len(rets) else 0,
            }
        )
    return pd.DataFrame(rows)


def assign_failure_tags(row: pd.Series) -> str:
    tags: list[str] = []
    d1 = row.get("fwd_ret_confirm_to_close_1d")
    d2 = row.get("fwd_ret_confirm_to_close_2d")
    d3 = row.get("fwd_ret_confirm_to_close_3d")
    d5 = row.get("fwd_ret_confirm_to_close_5d")
    d10 = row.get("fwd_ret_confirm_to_close_10d")
    d20 = row.get("fwd_ret_confirm_to_close_20d")
    if pd.notna(d1) and d1 < 0:
        tags.append("D1即弱")
    if pd.notna(d2) and d2 < 0:
        tags.append("D2仍弱")
    if pd.notna(d1) and pd.notna(d2) and d2 < d1 and d2 <= 0.01:
        tags.append("D2修复衰减")
    if pd.notna(d3) and d3 < 0:
        tags.append("D3仍弱")
    if pd.notna(d5) and d5 < 0:
        tags.append("D5失败")
    if pd.notna(d10) and pd.notna(d5) and d10 < d5:
        tags.append("D10回吐")
    if pd.notna(d20) and pd.notna(d5) and d20 < d5:
        tags.append("D20回吐")
    if pd.notna(row.get("gap_open")) and row["gap_open"] > 0.0:
        tags.append("仍有跳空")
    if pd.notna(row.get("range_pos60")) and row["range_pos60"] > 0.08:
        tags.append("不够贴底")
    if pd.notna(row.get("close_position")) and row["close_position"] < 0.55:
        tags.append("日线修复弱")
    return " / ".join(tags) if tags else "无明显早期弱标签"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_path = RUN_DIR / "closed_trades.csv"
    curve_path = RUN_DIR / "mtm_equity_curve.csv"
    summary_path = SOURCE_DIR / "summary.csv"
    windows_path = SOURCE_DIR / "window_metrics.csv"

    trades = pd.read_csv(trades_path, low_memory=False)
    trades = numeric(
        trades,
        [
            "policy_net_ret",
            "realized_pnl",
            "entry_price_used",
            "stake",
            "amount_ratio3",
            "bar_close_pos",
            "bar_ret",
            "range_pos60",
            "runup_from_60d_low",
            "close_position",
            "amount_ratio20",
            "gap_open",
            "index_mom20",
            "confirm_high",
            "confirm_low",
            "entry_price",
            "minute_day_close",
            "open_range_high",
            "intraday_high_so_far",
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
    trades["window"] = "train_2020_2023"
    trades.loc[trades["entry_dt"].between(pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31")), "window"] = "valid_2024_2025"
    trades.loc[trades["entry_dt"].ge(pd.Timestamp("2026-01-01")), "window"] = "blind_2026ytd"
    trades["is_loss"] = trades["policy_net_ret"] < 0
    trades["failure_tags"] = trades.apply(assign_failure_tags, axis=1)
    trades["entry_bar_upper_shadow"] = (trades["confirm_high"] - trades["entry_price"]) / (trades["confirm_high"] - trades["confirm_low"]).replace(0, pd.NA)
    trades["close_vs_open_range_high"] = trades["minute_day_close"] / trades["open_range_high"].replace(0, pd.NA) - 1.0

    summary_rows = pd.read_csv(summary_path, low_memory=False)
    selected_summary = summary_rows[(summary_rows["variant"] == VARIANT) & (summary_rows["profile"] == PROFILE)]
    selected_summary_dict = selected_summary.iloc[0].to_dict() if not selected_summary.empty else {}
    windows = pd.read_csv(windows_path, low_memory=False)
    selected_windows = windows[windows["variant"].eq(VARIANT)].copy()
    selected_windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")

    yearly = summarize_group(trades, "year")
    window_quality = summarize_group(trades, "window")
    tag_rows = []
    for tag in sorted({t for tags in trades["failure_tags"].astype(str) for t in tags.split(" / ")}):
        if not tag:
            continue
        g = trades[trades["failure_tags"].astype(str).str.contains(tag, regex=False)].copy()
        rets = g["policy_net_ret"].dropna()
        tag_rows.append(
            {
                "tag": tag,
                "trade_count": int(len(g)),
                "loss_count": int((rets < 0).sum()) if len(rets) else 0,
                "win_rate": float((rets > 0).mean()) if len(rets) else None,
                "avg_return": float(rets.mean()) if len(rets) else None,
                "sum_pnl": float(g["realized_pnl"].sum()) if len(g) else None,
                "worst_return": float(rets.min()) if len(rets) else None,
            }
        )
    tag_summary = pd.DataFrame(tag_rows).sort_values(["sum_pnl", "trade_count"], ascending=[True, False]) if tag_rows else pd.DataFrame()

    top_trades = trades.sort_values("realized_pnl", ascending=False).head(15)
    loss_trades = trades[trades["policy_net_ret"] < 0].sort_values("policy_net_ret")
    valid_trades = trades[trades["window"].eq("valid_2024_2025")].sort_values("entry_dt")
    valid_losses = valid_trades[valid_trades["policy_net_ret"] < 0].sort_values("policy_net_ret")

    keep_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "window",
        "policy_net_ret",
        "realized_pnl",
        "failure_tags",
        "amount_ratio3",
        "bar_close_pos",
        "bar_ret",
        "gap_open",
        "range_pos60",
        "close_position",
        "index_mom20",
        "entry_bar_upper_shadow",
        "close_vs_open_range_high",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "fwd_ret_confirm_to_close_10d",
        "fwd_ret_confirm_to_close_20d",
    ]
    keep_cols = [col for col in keep_cols if col in trades.columns]

    trades.sort_values("entry_dt")[keep_cols].to_csv(OUT_DIR / "all_trades_quality.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUT_DIR / "yearly_quality.csv", index=False, encoding="utf-8-sig")
    window_quality.to_csv(OUT_DIR / "window_quality.csv", index=False, encoding="utf-8-sig")
    tag_summary.to_csv(OUT_DIR / "failure_tag_summary.csv", index=False, encoding="utf-8-sig")
    top_trades[keep_cols].to_csv(OUT_DIR / "top_trades.csv", index=False, encoding="utf-8-sig")
    loss_trades[keep_cols].to_csv(OUT_DIR / "loss_trades.csv", index=False, encoding="utf-8-sig")
    valid_trades[keep_cols].to_csv(OUT_DIR / "valid_2024_2025_trades.csv", index=False, encoding="utf-8-sig")
    valid_losses[keep_cols].to_csv(OUT_DIR / "valid_2024_2025_losses.csv", index=False, encoding="utf-8-sig")

    total_pnl = float(trades["realized_pnl"].sum())
    top1 = float(top_trades["realized_pnl"].head(1).sum())
    top3 = float(top_trades["realized_pnl"].head(3).sum())
    top5 = float(top_trades["realized_pnl"].head(5).sum())
    losses = loss_trades["policy_net_ret"].dropna()
    valid_rets = valid_trades["policy_net_ret"].dropna()

    curve = pd.read_csv(curve_path, low_memory=False)
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce")
    audit_summary = {
        "variant": VARIANT,
        "variant_cn": VARIANT_CN,
        "profile": PROFILE,
        "profile_cn": PROFILE_CN,
        "signal_backtest_start": SIGNAL_START,
        "signal_backtest_end": SIGNAL_END,
        "curve_start": curve["date"].min().strftime("%Y-%m-%d") if not curve.empty else None,
        "curve_end": curve["date"].max().strftime("%Y-%m-%d") if not curve.empty else None,
        "trade_count": int(len(trades)),
        "total_return": float(selected_summary_dict.get("total_return", float("nan"))),
        "max_drawdown": float(selected_summary_dict.get("max_drawdown", float("nan"))),
        "win_rate": float(selected_summary_dict.get("win_rate", float("nan"))),
        "avg_trade_return": float(selected_summary_dict.get("avg_trade_return", float("nan"))),
        "worst_trade": float(selected_summary_dict.get("worst_trade", float("nan"))),
        "total_pnl": total_pnl,
        "top1_pnl_share": top1 / total_pnl if total_pnl else None,
        "top3_pnl_share": top3 / total_pnl if total_pnl else None,
        "top5_pnl_share": top5 / total_pnl if total_pnl else None,
        "loss_trade_count": int(len(loss_trades)),
        "worst_loss": float(losses.min()) if len(losses) else None,
        "valid_2024_2025_trade_count": int(len(valid_trades)),
        "valid_2024_2025_win_rate": float((valid_rets > 0).mean()) if len(valid_rets) else None,
        "valid_2024_2025_avg_return": float(valid_rets.mean()) if len(valid_rets) else None,
    }
    (OUT_DIR / "audit_summary.json").write_text(json.dumps(audit_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 横盘箱体底部不追高30m强承接质量审计",
        "",
        "## 口径说明",
        f"- 英文名：`{VARIANT}`。",
        "- 中文含义：横盘箱体底部候选里，只保留不追高的30m强承接；要求入场日跳空<=2%、30m量比>=1.5、30m收盘位置>=75%。",
        f"- 回测窗口：{SIGNAL_START} 至 {SIGNAL_END}；成本口径：`{PROFILE}`，即30bps。",
        f"- 实际曲线窗口：{audit_summary['curve_start']} 至 {audit_summary['curve_end']}。",
        "",
        "## 核心结论",
        f"- 全样本 {audit_summary['trade_count']} 笔，收益 {pct(audit_summary['total_return'])}，最大回撤 {pct(audit_summary['max_drawdown'])}，胜率 {pct(audit_summary['win_rate'])}，均笔 {pct(audit_summary['avg_trade_return'])}，最差单笔 {pct(audit_summary['worst_trade'])}。",
        f"- 收益集中度：Top1 PnL 占 {pct(audit_summary['top1_pnl_share'])}，Top3 占 {pct(audit_summary['top3_pnl_share'])}，Top5 占 {pct(audit_summary['top5_pnl_share'])}。",
        f"- 2024-2025 验证窗 {audit_summary['valid_2024_2025_trade_count']} 笔，均笔 {pct(audit_summary['valid_2024_2025_avg_return'])}，胜率 {pct(audit_summary['valid_2024_2025_win_rate'])}。",
        "",
        "## 年度表现",
    ]
    for _, row in yearly.iterrows():
        lines.append(
            f"- {int(row['year'])}：{int(row['trade_count'])} 笔，PnL {money(row['sum_pnl'])}，均笔 {pct(row['avg_return'])}，胜率 {pct(row['win_rate'])}，最差 {pct(row['worst_return'])}。"
        )
    lines.extend(["", "## 2024-2025 亏损票"])
    if valid_losses.empty:
        lines.append("- 2024-2025 没有亏损票。")
    else:
        for _, row in valid_losses.iterrows():
            lines.append(
                f"- {row['entry_date']} {row['name']}({row['code']})：{pct(row['policy_net_ret'])}，标签：{row['failure_tags']}；D1 {pct(row.get('fwd_ret_confirm_to_close_1d'))}，D2 {pct(row.get('fwd_ret_confirm_to_close_2d'))}，D5 {pct(row.get('fwd_ret_confirm_to_close_5d'))}。"
            )
    lines.extend(
        [
            "",
            "## 判断",
            "- 这个方向比原始宽源强，但 2024-2025 验证窗失败，说明不能直接升正式买点。",
            "- 失败重点应继续拆：箱体底部真假、D1/D2修复衰减、以及是否在弱指数环境中承接失败。",
            "- 下一步建议在这69笔上做固定失败标签减仓/退出复验，而不是继续收紧入场阈值。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(audit_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
