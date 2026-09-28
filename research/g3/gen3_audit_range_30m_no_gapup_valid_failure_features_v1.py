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
RUN_DIR = (
    _report_path()
    / "gen3_range_30m_true_acceptance_structure_v1"
    / "box_stress_accept_h5__no_gapup_strong_bar__cost30"
)
OUT_DIR = _report_path() / "gen3_range_30m_no_gapup_valid_failure_features_v1"

VARIANT = "box_stress_accept_h5__no_gapup_strong_bar"
VARIANT_CN = "横盘箱体底部不追高30m强承接"
SIGNAL_START = "2020-01-01"
SIGNAL_END = "2026-05-29"
PROFILE = "cost30"
PROFILE_CN = "30bps交易成本口径"


FEATURES = [
    "range_pos60",
    "gap_open",
    "close_position",
    "index_mom20",
    "amount_ratio20",
    "amount_ratio3",
    "bar_close_pos",
    "bar_ret",
    "entry_bar_upper_shadow",
    "close_vs_open_range_high",
    "fwd_ret_confirm_to_close_1d",
    "fwd_ret_confirm_to_close_2d",
    "fwd_ret_confirm_to_close_3d",
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


def group_name(row: pd.Series) -> str:
    ret = row["policy_net_ret"]
    window = row["window"]
    side = "win" if ret > 0 else "loss"
    return f"{window}_{side}"


def summarize_returns(df: pd.DataFrame, name: str) -> dict[str, Any]:
    rets = df["policy_net_ret"].dropna()
    return {
        "group": name,
        "trade_count": int(len(df)),
        "total_pnl": float(df["realized_pnl"].sum()) if "realized_pnl" in df else None,
        "win_rate": float((rets > 0).mean()) if len(rets) else None,
        "avg_return": float(rets.mean()) if len(rets) else None,
        "median_return": float(rets.median()) if len(rets) else None,
        "worst_return": float(rets.min()) if len(rets) else None,
        "best_return": float(rets.max()) if len(rets) else None,
    }


def summarize_features(df: pd.DataFrame, by: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, g in df.groupby(by, dropna=False):
        row: dict[str, Any] = {"group": key, "trade_count": int(len(g))}
        for col in FEATURES:
            if col not in g:
                continue
            s = pd.to_numeric(g[col], errors="coerce").dropna()
            row[f"{col}_median"] = float(s.median()) if len(s) else None
            row[f"{col}_q25"] = float(s.quantile(0.25)) if len(s) else None
            row[f"{col}_q75"] = float(s.quantile(0.75)) if len(s) else None
        rows.append(row)
    return pd.DataFrame(rows)


def hypothesis_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "base_all": pd.Series(True, index=df.index),
        "tight_floor_le8pct": df["range_pos60"].le(0.08),
        "daily_reclaim_ge70pct": df["close_position"].ge(0.70),
        "no_positive_gap": df["gap_open"].le(0.0),
        "not_late_confirm": pd.to_datetime(df["confirm_datetime"], errors="coerce").dt.time < pd.Timestamp("15:00").time(),
        "not_failed_open_range_high": df["close_vs_open_range_high"].ge(-0.005),
        "no_upper_shadow_warning": df["entry_bar_upper_shadow"].le(0.20),
        "d1_not_negative": df["fwd_ret_confirm_to_close_1d"].ge(0.0),
        "d2_not_negative": df["fwd_ret_confirm_to_close_2d"].ge(0.0),
        "d3_not_negative": df["fwd_ret_confirm_to_close_3d"].ge(0.0),
        "floor8_and_reclaim70": df["range_pos60"].le(0.08) & df["close_position"].ge(0.70),
        "floor8_no_failed_open_range": df["range_pos60"].le(0.08) & df["close_vs_open_range_high"].ge(-0.005),
        "reclaim70_no_failed_open_range": df["close_position"].ge(0.70) & df["close_vs_open_range_high"].ge(-0.005),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_path = RUN_DIR / "closed_trades.csv"
    curve_path = RUN_DIR / "mtm_equity_curve.csv"
    trades = pd.read_csv(trades_path, low_memory=False)
    numeric_cols = list(set(FEATURES + ["policy_net_ret", "realized_pnl", "confirm_high", "confirm_low", "entry_price", "minute_day_close", "open_range_high"]))
    trades = numeric(trades, numeric_cols)
    trades["entry_dt"] = pd.to_datetime(trades["entry_date"], errors="coerce")
    trades["window"] = "train_2020_2023"
    trades.loc[trades["entry_dt"].between(pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31")), "window"] = "valid_2024_2025"
    trades.loc[trades["entry_dt"].ge(pd.Timestamp("2026-01-01")), "window"] = "blind_2026ytd"
    trades["entry_bar_upper_shadow"] = (trades["confirm_high"] - trades["entry_price"]) / (trades["confirm_high"] - trades["confirm_low"]).replace(0, pd.NA)
    trades["close_vs_open_range_high"] = trades["minute_day_close"] / trades["open_range_high"].replace(0, pd.NA) - 1.0
    trades["result_group"] = trades.apply(group_name, axis=1)

    feature_by_group = summarize_features(trades, "result_group").sort_values("group")
    feature_by_window = summarize_features(trades, "window").sort_values("group")

    return_rows = []
    for key, g in trades.groupby("result_group", dropna=False):
        return_rows.append(summarize_returns(g, key))
    return_by_group = pd.DataFrame(return_rows).sort_values("group")

    hypo_rows = []
    for name, mask in hypothesis_masks(trades).items():
        kept = trades[mask.fillna(False)].copy()
        row = summarize_returns(kept, name)
        row["kept_ratio"] = len(kept) / len(trades) if len(trades) else None
        valid = kept[kept["window"].eq("valid_2024_2025")]
        blind = kept[kept["window"].eq("blind_2026ytd")]
        train = kept[kept["window"].eq("train_2020_2023")]
        row["train_trade_count"] = int(len(train))
        row["train_avg_return"] = float(train["policy_net_ret"].mean()) if len(train) else None
        row["valid_trade_count"] = int(len(valid))
        row["valid_avg_return"] = float(valid["policy_net_ret"].mean()) if len(valid) else None
        row["valid_win_rate"] = float((valid["policy_net_ret"] > 0).mean()) if len(valid) else None
        row["blind_trade_count"] = int(len(blind))
        row["blind_avg_return"] = float(blind["policy_net_ret"].mean()) if len(blind) else None
        hypo_rows.append(row)
    hypothesis_summary = pd.DataFrame(hypo_rows).sort_values(["valid_avg_return", "avg_return"], ascending=[False, False])

    valid_losses = trades[(trades["window"].eq("valid_2024_2025")) & (trades["policy_net_ret"] < 0)].copy()
    valid_losses = valid_losses.sort_values("policy_net_ret")
    keep_cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "policy_net_ret",
        "realized_pnl",
        "range_pos60",
        "gap_open",
        "close_position",
        "index_mom20",
        "amount_ratio3",
        "bar_close_pos",
        "bar_ret",
        "entry_bar_upper_shadow",
        "close_vs_open_range_high",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
    ]
    keep_cols = [c for c in keep_cols if c in trades.columns]

    feature_by_group.to_csv(OUT_DIR / "feature_by_group.csv", index=False, encoding="utf-8-sig")
    feature_by_window.to_csv(OUT_DIR / "feature_by_window.csv", index=False, encoding="utf-8-sig")
    return_by_group.to_csv(OUT_DIR / "return_by_group.csv", index=False, encoding="utf-8-sig")
    hypothesis_summary.to_csv(OUT_DIR / "hypothesis_summary.csv", index=False, encoding="utf-8-sig")
    valid_losses[keep_cols].to_csv(OUT_DIR / "valid_2024_2025_losses_features.csv", index=False, encoding="utf-8-sig")

    curve = pd.read_csv(curve_path, low_memory=False)
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce")
    summary = {
        "variant": VARIANT,
        "variant_cn": VARIANT_CN,
        "profile": PROFILE,
        "profile_cn": PROFILE_CN,
        "signal_backtest_start": SIGNAL_START,
        "signal_backtest_end": SIGNAL_END,
        "curve_start": curve["date"].min().strftime("%Y-%m-%d") if len(curve) else None,
        "curve_end": curve["date"].max().strftime("%Y-%m-%d") if len(curve) else None,
        "trade_count": int(len(trades)),
        "valid_2024_2025_trade_count": int((trades["window"] == "valid_2024_2025").sum()),
        "valid_2024_2025_loss_count": int(len(valid_losses)),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    top_hypo = hypothesis_summary.head(8).copy()
    report = [
        "# G3 横盘30m承接 2024-2025失效特征审计 v1",
        "",
        "## 对象",
        f"- 英文名：`{VARIANT}`。",
        f"- 中文解释：{VARIANT_CN}。它先找横盘箱体底部候选，再要求入场日不追高，并出现30m放量强承接。",
        f"- 回测范围：信号 {SIGNAL_START} 至 {SIGNAL_END}；曲线 {summary['curve_start']} 至 {summary['curve_end']}；口径 {PROFILE_CN}；交易 {len(trades)} 笔。",
        "",
        "## 本步结论",
        "- 这一步不做 score/rank 过滤，只做固定结构归因。",
        "- 2024-2025 验证段亏损不是单纯靠 D3 弱退出能解决，应该继续研究入场日结构：是否足够贴箱体底、日线修复是否强、是否收复早盘高点、以及确认bar是否接近尾盘。",
        "- 下面的假设表只是研究线索，不能直接作为正式参数；下一步需要把靠前的固定假设做完整 slot 复算和 30bps/100bps/2%冲击压力测试。",
        "",
        "## 固定结构假设分组",
        top_hypo.to_markdown(index=False),
        "",
        "## 输出文件",
        "- `feature_by_group.csv`：训练/验证/盲测赢家与亏损样本的特征分布。",
        "- `hypothesis_summary.csv`：固定结构假设的交易保留与收益分组。",
        "- `valid_2024_2025_losses_features.csv`：2024-2025亏损交易明细。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")


if __name__ == "__main__":
    main()
