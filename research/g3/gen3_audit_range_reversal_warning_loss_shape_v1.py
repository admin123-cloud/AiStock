from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
SOURCE = _report_path() / "gen3_range_reversal_warning_trades_v1" / "warning_trades.csv"
OUT_DIR = _report_path() / "gen3_range_reversal_warning_loss_shape_v1"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无样本_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def summarize(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for group, part in df.groupby(group_col, dropna=False):
        ret = pd.to_numeric(part["cost30_5d"], errors="coerce")
        rows.append(
            {
                group_col: group,
                "trades": int(len(part)),
                "ret_sum": float(ret.sum()),
                "ret_avg": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "d1_avg": float(part["fwd_ret_confirm_to_close_1d"].mean()),
                "d2_avg": float(part["fwd_ret_confirm_to_close_2d"].mean()),
                "amount3_avg": float(part["amount_ratio3"].mean()),
                "bar_close_pos_avg": float(part["bar_close_pos"].mean()),
                "range_pos60_avg": float(part["range_pos60"].mean()),
                "close_position_avg": float(part["close_position"].mean()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    for col in [
        "cost30_5d",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "amount_ratio3",
        "bar_close_pos",
        "range_pos60",
        "close_position",
        "gap_open",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["result_group"] = df["cost30_5d"].gt(0).map({True: "winner", False: "loser"})
    df["d1_repair"] = df["fwd_ret_confirm_to_close_1d"].ge(0)
    df["d2_repair"] = df["fwd_ret_confirm_to_close_2d"].ge(0)
    df["d1_or_d2_repair"] = df["d1_repair"] | df["d2_repair"]
    df["d1d2_no_repair"] = ~df["d1_or_d2_repair"]
    df["low_amount3"] = df["amount_ratio3"].lt(2.0)
    df["weak_bar_close"] = df["bar_close_pos"].lt(0.70)
    df["daily_reclaim_weak"] = df["close_position"].lt(0.60)
    df["not_deep_floor"] = df["range_pos60"].gt(0.10)

    group = summarize(df, "result_group")
    flags = []
    for flag in ["d1d2_no_repair", "low_amount3", "weak_bar_close", "daily_reclaim_weak", "not_deep_floor"]:
        tmp = summarize(df.assign(flag_value=df[flag].map({True: f"{flag}=true", False: f"{flag}=false"})), "flag_value")
        tmp.insert(0, "flag", flag)
        flags.append(tmp)
    flag_summary = pd.concat(flags, ignore_index=True)

    losses = df[df["result_group"].eq("loser")].sort_values("cost30_5d")
    winners = df[df["result_group"].eq("winner")].sort_values("cost30_5d", ascending=False)

    group.to_csv(OUT_DIR / "winner_loser_summary.csv", index=False, encoding="utf-8-sig")
    flag_summary.to_csv(OUT_DIR / "flag_summary.csv", index=False, encoding="utf-8-sig")
    losses.to_csv(OUT_DIR / "loss_trades.csv", index=False, encoding="utf-8-sig")
    winners.to_csv(OUT_DIR / "winner_trades.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "ret_sum",
        "ret_avg",
        "win_rate",
        "d1_avg",
        "d2_avg",
        "bar_close_pos_avg",
        "range_pos60_avg",
        "close_position_avg",
        "cost30_5d",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "bar_close_pos",
        "range_pos60",
        "close_position",
        "gap_open",
    }
    lines = [
        "# G3 range 冲高回落警戒样本亏损形态审计 v1",
        "",
        "## 本轮结论",
        "",
        "- `entry_reversal_warning` 整体不是坏信号，继续作为卖出/减仓方向不成立。",
        "- 15 笔警戒样本中，亏损更像来自“早期没有修复”和“日线修复质量不足”的组合，而不是来自冲高回落本身。",
        "- 样本太小，当前只能形成研究假设，不能作为正式规则接入。",
        "",
        "## 英文名解释",
        "",
        "- `entry_reversal_warning`：入场日冲高回落/突破失败警戒。",
        "- `d1d2_no_repair`：D1 和 D2 都没有回到确认价以上，代表买入后两天没有早期修复。",
        "- `low_amount3`：30m 确认量能相对前三根均量小于2倍，代表承接强度不足。",
        "- `weak_bar_close`：30m 确认K线收盘位置低于70%，代表确认K线没有收在较高位置。",
        "- `daily_reclaim_weak`：日线收盘修复位置低于60%，代表日线级别修复不够强。",
        "- `not_deep_floor`：箱体位置高于10%，代表不是非常贴近箱体底部。",
        "",
        "## 赢家 vs 输家",
        "",
        md_table(group, pct_cols=pct_cols),
        "",
        "## 结构标签对照",
        "",
        md_table(flag_summary, pct_cols=pct_cols),
        "",
        "## 亏损样本",
        "",
        md_table(losses[[
            "entry_date",
            "code",
            "name",
            "cost30_5d",
            "fwd_ret_confirm_to_close_1d",
            "fwd_ret_confirm_to_close_2d",
            "amount_ratio3",
            "bar_close_pos",
            "range_pos60",
            "close_position",
            "gap_open",
            "d1d2_no_repair",
            "low_amount3",
            "daily_reclaim_weak",
        ]], pct_cols=pct_cols),
        "",
        "## 下一步目标",
        "",
        "- 不再研究 `entry_reversal_warning` 整体剔除或整体快速退出。",
        "- 下一步只验证一个更窄的假设：`entry_reversal_warning + d1d2_no_repair + daily_reclaim_weak` 是否是真正亏损型警戒。",
        "- 如果这个窄假设仍然不能改善压力口径，就停止该方向，转回横盘/箱体底部独立候选源重建。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
