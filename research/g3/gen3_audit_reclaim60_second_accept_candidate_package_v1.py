from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
SOURCE_DIR = _report_path() / "gen3_range_reclaim60_second_accept_early_exit_v1"
OUT_DIR = _report_path() / "gen3_reclaim60_second_accept_candidate_package_audit_v1"

VARIANT = "reclaim60_second_accept__d3_negative_exit_d3"
VARIANT_CN = "日线收盘修复>=60%，D0/D1二次30m承接，若D3仍为负则D3退出"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
CURVE_END = "2026-06-04"


FEATURES = [
    "range_pos60",
    "close_position",
    "amount_ratio3",
    "bar_close_pos",
    "bar_ret",
    "gap_open",
    "index_mom20",
    "fwd_ret_confirm_to_close_1d",
    "fwd_ret_confirm_to_close_2d",
    "fwd_ret_confirm_to_close_3d",
    "fwd_ret_confirm_to_close_5d",
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value) * 100:+.2f}%"


def money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--"
    return f"{float(value):,.0f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif col in money_cols:
                item[col] = money(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def add_window(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["entry_dt"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["year"] = d["entry_dt"].dt.year
    d["window"] = "train_2020_2023"
    d.loc[d["entry_dt"].between(pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31")), "window"] = "valid_2024_2025"
    d.loc[d["entry_dt"].ge(pd.Timestamp("2026-01-01")), "window"] = "blind_2026ytd"
    return d


def load_profile(profile: str) -> pd.DataFrame:
    path = SOURCE_DIR / f"d3_negative_exit_d3__{profile}" / "closed_trades.csv"
    d = pd.read_csv(path, low_memory=False)
    d["profile"] = profile
    d = numeric(d, ["policy_net_ret", "realized_pnl", "stake", *FEATURES])
    return add_window(d)


def group_returns(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, g in df.groupby(by, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        rets = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        row = {col: val for col, val in zip(by, key)}
        row.update(
            {
                "trade_count": int(len(g)),
                "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                "avg_return": float(rets.mean()) if len(rets) else 0.0,
                "median_return": float(rets.median()) if len(rets) else 0.0,
                "worst_return": float(rets.min()) if len(rets) else 0.0,
                "best_return": float(rets.max()) if len(rets) else 0.0,
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()) if "realized_pnl" in g else 0.0,
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def concentration(df: pd.DataFrame, label: str) -> pd.DataFrame:
    d = df.sort_values("realized_pnl", ascending=False).copy()
    total_pnl = float(pd.to_numeric(d["realized_pnl"], errors="coerce").sum())
    rows = []
    for n in [0, 1, 3, 5, 10]:
        rest = d.iloc[n:].copy()
        rets = pd.to_numeric(rest["policy_net_ret"], errors="coerce")
        pnl = float(pd.to_numeric(rest["realized_pnl"], errors="coerce").sum()) if len(rest) else 0.0
        rows.append(
            {
                "variant": label,
                "exclude_top_n": n,
                "remaining_trades": int(len(rest)),
                "remaining_pnl": pnl,
                "remaining_pnl_share_of_total": pnl / total_pnl if total_pnl else None,
                "remaining_avg_return": float(rets.mean()) if len(rets) else 0.0,
                "remaining_win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                "total_pnl": total_pnl,
            }
        )
    return pd.DataFrame(rows)


def feature_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    d = df.copy()
    d["result"] = d["policy_net_ret"].gt(0).map({True: "win", False: "loss"})
    d["bucket"] = d["window"] + "_" + d["result"]
    for bucket, g in d.groupby("bucket", dropna=False):
        row: dict[str, Any] = {"bucket": bucket, "trade_count": int(len(g))}
        for col in FEATURES:
            if col not in g:
                continue
            s = pd.to_numeric(g[col], errors="coerce").dropna()
            row[f"{col}_median"] = float(s.median()) if len(s) else None
            row[f"{col}_q25"] = float(s.quantile(0.25)) if len(s) else None
            row[f"{col}_q75"] = float(s.quantile(0.75)) if len(s) else None
        rows.append(row)
    return pd.DataFrame(rows).sort_values("bucket")


def promotion_check(summary: pd.DataFrame, windows: pd.DataFrame, conc: pd.DataFrame) -> pd.DataFrame:
    def get_summary(profile: str) -> pd.Series:
        rows = summary[(summary["variant"].eq(VARIANT)) & (summary["profile"].eq(profile))]
        return rows.iloc[0] if not rows.empty else pd.Series(dtype=float)

    def get_window(profile: str, window: str) -> pd.Series:
        rows = windows[(windows["variant"].eq(VARIANT)) & (windows["profile"].eq(profile)) & (windows["window"].eq(window))]
        return rows.iloc[0] if not rows.empty else pd.Series(dtype=float)

    cost30 = get_summary("cost30")
    cost100 = get_summary("cost100")
    shock = get_summary("shock2_cost30")
    valid100 = get_window("cost100", "valid_2024_2025")
    valid_shock = get_window("shock2_cost30", "valid_2024_2025")
    top5 = conc[conc["exclude_top_n"].eq(5)].iloc[0]
    checks = [
        {
            "check": "样本数>=50",
            "pass": bool(cost30.get("trade_count", 0) >= 50),
            "value": int(cost30.get("trade_count", 0)),
            "note": "低于50笔只能算候选研究，不能升级正式策略。",
        },
        {
            "check": "全周期30bps收益为正且回撤<5%",
            "pass": bool(cost30.get("total_return", 0) > 0 and abs(cost30.get("max_drawdown", 1)) < 0.05),
            "value": f"{pct(cost30.get('total_return'))} / {pct(cost30.get('max_drawdown'))}",
            "note": "基础表现通过。",
        },
        {
            "check": "全周期100bps仍为正",
            "pass": bool(cost100.get("total_return", 0) > 0),
            "value": pct(cost100.get("total_return")),
            "note": "高摩擦全周期通过。",
        },
        {
            "check": "全周期2%冲击仍为正",
            "pass": bool(shock.get("total_return", 0) > 0),
            "value": pct(shock.get("total_return")),
            "note": "极端冲击全周期通过。",
        },
        {
            "check": "2024-2025验证段100bps为正",
            "pass": bool(valid100.get("return", 0) > 0),
            "value": pct(valid100.get("return")),
            "note": "验证段高摩擦未通过。",
        },
        {
            "check": "2024-2025验证段2%冲击为正",
            "pass": bool(valid_shock.get("return", 0) > 0),
            "value": pct(valid_shock.get("return")),
            "note": "验证段冲击未通过。",
        },
        {
            "check": "剔除Top5后仍有明显收益",
            "pass": bool(top5.get("remaining_avg_return", 0) > 0.01),
            "value": f"剩余均笔 {pct(top5.get('remaining_avg_return'))}, 剩余PnL {money(top5.get('remaining_pnl'))}",
            "note": "剩余均笔不足1%，收益集中度偏高。",
        },
    ]
    return pd.DataFrame(checks)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profiles = {name: load_profile(name) for name in ["cost30", "cost100", "shock2_cost30"]}
    cost30 = profiles["cost30"]
    summary = pd.read_csv(SOURCE_DIR / "summary.csv", low_memory=False)
    windows = pd.read_csv(SOURCE_DIR / "window_metrics.csv", low_memory=False)
    summary = numeric(summary, ["trade_count", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "changed_count"])
    windows = numeric(windows, ["return", "max_drawdown", "trade_count", "win_rate"])
    target_summary = summary[summary["variant"].eq(VARIANT)].copy()
    target_windows = windows[windows["variant"].eq(VARIANT)].copy()

    by_year = group_returns(cost30, ["year"])
    by_window_profile = pd.concat([group_returns(df, ["profile", "window"]) for df in profiles.values()], ignore_index=True)
    by_emotion = group_returns(cost30, ["emotion_signal"])
    by_exit = group_returns(cost30, ["exit_reason"])
    top_trades = cost30.sort_values("realized_pnl", ascending=False).head(15)
    worst_trades = cost30.sort_values("policy_net_ret").head(15)
    valid_cost30 = cost30[cost30["window"].eq("valid_2024_2025")].sort_values("entry_dt")
    feature_stats = feature_summary(cost30)
    conc = concentration(cost30, VARIANT)
    checks = promotion_check(target_summary, target_windows, conc)

    keep_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "emotion_signal",
        "exit_reason",
        "policy_net_ret",
        "realized_pnl",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "bar_close_pos",
        "bar_ret",
        "gap_open",
        "index_mom20",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
    ]
    keep_cols = [c for c in keep_cols if c in cost30.columns]

    target_summary.to_csv(OUT_DIR / "target_summary.csv", index=False, encoding="utf-8-sig")
    target_windows.to_csv(OUT_DIR / "target_window_metrics.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "yearly_cost30.csv", index=False, encoding="utf-8-sig")
    by_window_profile.to_csv(OUT_DIR / "window_by_profile.csv", index=False, encoding="utf-8-sig")
    by_emotion.to_csv(OUT_DIR / "emotion_cost30.csv", index=False, encoding="utf-8-sig")
    by_exit.to_csv(OUT_DIR / "exit_reason_cost30.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "concentration_cost30.csv", index=False, encoding="utf-8-sig")
    checks.to_csv(OUT_DIR / "promotion_checklist.csv", index=False, encoding="utf-8-sig")
    feature_stats.to_csv(OUT_DIR / "feature_summary_cost30.csv", index=False, encoding="utf-8-sig")
    top_trades[keep_cols].to_csv(OUT_DIR / "top_trades_cost30.csv", index=False, encoding="utf-8-sig")
    worst_trades[keep_cols].to_csv(OUT_DIR / "worst_trades_cost30.csv", index=False, encoding="utf-8-sig")
    valid_cost30[keep_cols].to_csv(OUT_DIR / "valid_2024_2025_trades_cost30.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "avg_return",
        "median_return",
        "worst_return",
        "best_return",
        "remaining_pnl_share_of_total",
        "remaining_avg_return",
        "remaining_win_rate",
    }
    money_cols = {"pnl", "realized_pnl", "remaining_pnl", "total_pnl"}
    summary_row = target_summary[target_summary["profile"].eq("cost30")].iloc[0].to_dict()
    valid100 = target_windows[(target_windows["profile"].eq("cost100")) & (target_windows["window"].eq("valid_2024_2025"))].iloc[0].to_dict()
    valid_shock = target_windows[(target_windows["profile"].eq("shock2_cost30")) & (target_windows["window"].eq("valid_2024_2025"))].iloc[0].to_dict()
    report = [
        "# G3 reclaim60_second_accept 候选方案完整审计 v1",
        "",
        "## 研究对象",
        f"- 英文名：`{VARIANT}`。",
        f"- 中文解释：{VARIANT_CN}。",
        f"- 回测范围：候选信号 {SIGNAL_START} 至 {SIGNAL_END}；资金曲线结算至 {CURVE_END}；完整 slot 复算；样本 {int(summary_row['trade_count'])} 笔。",
        "",
        "## 核心结论",
        f"- 30bps全周期：{int(summary_row['trade_count'])} 笔，收益 {pct(summary_row['total_return'])}，最大回撤 {pct(summary_row['max_drawdown'])}，胜率 {pct(summary_row['win_rate'])}，最差单笔 {pct(summary_row['worst_trade'])}。",
        f"- 100bps全周期仍为正，但 2024-2025 验证段为 {pct(valid100['return'])}；2%冲击全周期仍为正，但 2024-2025 验证段为 {pct(valid_shock['return'])}。",
        "- 结论：这是目前横盘/箱体底部方向里相对最干净的候选源，但还不能升级为正式策略。主要卡点是样本只有38笔、验证段抗摩擦不足、收益仍有较强Top贡献。",
        "",
        "## 升级检查",
        md_table(checks, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 汇总指标",
        md_table(target_summary[["profile", "trade_count", "total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "changed_count"]], pct_cols=pct_cols),
        "",
        "## 分窗口与压力",
        md_table(target_windows[["profile", "window", "return", "max_drawdown", "trade_count", "win_rate"]], pct_cols=pct_cols),
        "",
        "## 年度表现（30bps）",
        md_table(by_year, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 情绪标签表现（30bps）",
        md_table(by_emotion, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 退出原因表现（30bps）",
        md_table(by_exit, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## Top贡献审计（30bps）",
        md_table(conc, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## Top交易（30bps）",
        md_table(top_trades[keep_cols].head(10), pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最差交易（30bps）",
        md_table(worst_trades[keep_cols].head(10), pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 2024-2025验证段交易（30bps）",
        md_table(valid_cost30[keep_cols], pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 下一步判断",
        "- 不建议继续微调 D1/D2/D3 退出阈值；D3弱退出已经是简单且相对稳的版本。",
        "- 下一步应该扩充候选源，而不是继续切这38笔：把“日线修复>=60% + 二次30m承接”迁移到更大的横盘箱体源，目标至少扩到50-100笔，再复验验证段100bps和2%冲击。",
        "- 若扩源后 2024-2025 的100bps/冲击仍为负，应停止把它当独立买法，只保留为G3横盘链路的辅助标签。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    result = {
        "variant": VARIANT,
        "variant_cn": VARIANT_CN,
        "signal_start": SIGNAL_START,
        "signal_end": SIGNAL_END,
        "curve_end": CURVE_END,
        "trade_count": int(summary_row["trade_count"]),
        "cost30_total_return": float(summary_row["total_return"]),
        "cost30_max_drawdown": float(summary_row["max_drawdown"]),
        "valid_2024_2025_cost100_return": float(valid100["return"]),
        "valid_2024_2025_shock2_return": float(valid_shock["return"]),
        "promote": False,
        "reason": "样本数不足50，且2024-2025验证段在100bps和2%冲击下仍为负。",
    }
    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
