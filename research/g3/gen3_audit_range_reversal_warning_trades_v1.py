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
SOURCE = _report_path() / "gen3_range_ice_recent3_entry_quality_v1" / "ice_recent3_base.csv"
OUT_DIR = _report_path() / "gen3_range_reversal_warning_trades_v1"


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


def load_base() -> pd.DataFrame:
    df = pd.read_csv(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["confirm_datetime"] = pd.to_datetime(df["confirm_datetime"], errors="coerce")
    for col in [
        "confirm_high",
        "prev_bar_high",
        "entry_price",
        "bar_ret",
        "bar_close_pos",
        "amount_ratio3",
        "range_pos60",
        "close_position",
        "gap_open",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_5d",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["d0_break_attempt"] = df["confirm_high"] > df["prev_bar_high"]
    df["d0_retracement"] = df["entry_price"] <= df["confirm_high"] * 0.995
    df["d0_high_pullback"] = (df["bar_ret"] > 0.02) & (df["bar_close_pos"] <= 0.55)
    df["entry_reversal_warning"] = df["d0_break_attempt"] & (df["d0_retracement"] | df["d0_high_pullback"])
    df["cost30_5d"] = df["fwd_ret_confirm_to_close_5d"] - 0.003
    df["cost100_5d"] = df["fwd_ret_confirm_to_close_5d"] - 0.010
    df["shock2_cost30_5d"] = df["fwd_ret_confirm_to_close_5d"] - 0.003 - 0.020
    df["d1_weak"] = df["fwd_ret_confirm_to_close_1d"] <= -0.03
    df["d2_weak"] = df["fwd_ret_confirm_to_close_2d"] <= -0.03
    df["year"] = df["entry_date"].dt.year
    return df


def summarize_group(df: pd.DataFrame, label_col: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for label, part in df.groupby(label_col, dropna=False):
        ret = pd.to_numeric(part["cost30_5d"], errors="coerce")
        rows.append(
            {
                label_col: label,
                "trades": int(len(part)),
                "cost30_sum": float(ret.sum()),
                "cost30_avg": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "worst": float(ret.min()),
                "best": float(ret.max()),
                "d1_weak_count": int(part["d1_weak"].sum()),
                "d2_weak_count": int(part["d2_weak"].sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = load_base()
    warnings = df[df["entry_reversal_warning"]].copy()
    normal = df[~df["entry_reversal_warning"]].copy()

    cols = [
        "entry_date",
        "code",
        "name",
        "confirm_datetime",
        "bar_ret",
        "bar_close_pos",
        "amount_ratio3",
        "range_pos60",
        "close_position",
        "gap_open",
        "cost30_5d",
        "cost100_5d",
        "shock2_cost30_5d",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "d1_weak",
        "d2_weak",
    ]
    warnings[cols].sort_values("cost30_5d", ascending=False).to_csv(
        OUT_DIR / "warning_trades.csv", index=False, encoding="utf-8-sig"
    )

    group_summary = summarize_group(
        df.assign(warning_group=df["entry_reversal_warning"].map({True: "warning", False: "normal"})),
        "warning_group",
    )
    year_summary = summarize_group(warnings, "year").sort_values("year")
    top_contrib = warnings[cols].sort_values("cost30_5d", ascending=False).head(8)
    bottom_contrib = warnings[cols].sort_values("cost30_5d", ascending=True).head(8)

    group_summary.to_csv(OUT_DIR / "group_summary.csv", index=False, encoding="utf-8-sig")
    year_summary.to_csv(OUT_DIR / "warning_year_summary.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# G3 range 入场日冲高回落/突破失败样本审计 v1",
        "",
        "## 本轮结论",
        "",
        "- `entry_reversal_warning` 不能直接当作减仓或卖出警戒。它在当前 `ice_recent3_base` 里只有 15 笔，但 5日持有的 cost30 平均收益明显高于非警戒样本。",
        "- `warn_fast_d1` 和 `weak_d1d2_exit` 这类早期弱确认退出没有改善收益，反而降低总收益；说明这批样本不是“弱就该跑”的典型失败链路。",
        "- 当前更合理的理解是：在冰点后横盘箱体底部，部分“突破失败/冲高回落”可能是强承接后的换手，而不是单纯失败。不能把它误杀。",
        "",
        "## 英文策略名解释",
        "",
        "- `entry_reversal_warning`：入场日冲高回落/突破失败警戒。定义为 30m 确认 K 线尝试突破前一根高点，但收盘从高点回落，或者放量上涨后收盘位置偏低。",
        "- `reversal_only`：只保留上述警戒样本，观察它本身是不是坏信号。",
        "- `reversal_filtered`：剔除上述警戒样本，观察是否能降低亏损。",
        "- `warn_fast_d1`：只有警戒样本在 D1 收盘已经跌破确认价约3%时才快速退出。",
        "- `weak_d1d2_exit`：不区分警戒，所有样本只要 D1/D2 早期弱就快速退出。",
        "",
        "## 警戒 vs 非警戒",
        "",
        md_table(group_summary, pct_cols={"cost30_sum", "cost30_avg", "win_rate", "worst", "best"}),
        "",
        "## 警戒样本年度分布",
        "",
        md_table(year_summary, pct_cols={"cost30_sum", "cost30_avg", "win_rate", "worst", "best"}),
        "",
        "## 警戒样本收益贡献 Top",
        "",
        md_table(top_contrib, pct_cols={"bar_ret", "bar_close_pos", "amount_ratio3", "range_pos60", "close_position", "gap_open", "cost30_5d", "cost100_5d", "shock2_cost30_5d", "fwd_ret_confirm_to_close_1d", "fwd_ret_confirm_to_close_2d"}),
        "",
        "## 警戒样本亏损 Top",
        "",
        md_table(bottom_contrib, pct_cols={"bar_ret", "bar_close_pos", "amount_ratio3", "range_pos60", "close_position", "gap_open", "cost30_5d", "cost100_5d", "shock2_cost30_5d", "fwd_ret_confirm_to_close_1d", "fwd_ret_confirm_to_close_2d"}),
        "",
        "## 下一步目标",
        "",
        "- 不再把 `entry_reversal_warning` 当作卖出触发。",
        "- 下一步应该研究这 15 笔里真正亏损的共性：是否集中在 D1/D2 无修复、成交量退潮、或非冰点窗口。",
        "- 如果亏损共性能被结构性解释，再做“只处理亏损型警戒”的小样本复验；如果解释不了，停止这条退出链，转回新的横盘/箱体底部独立候选源。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
