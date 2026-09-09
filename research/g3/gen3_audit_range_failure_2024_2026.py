from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = _PROJECT_ROOT
BASE = _report_path() / "gen3_range_30m_confirm_v1" / "daily_candidates.parquet"
CONFIRMED = _report_path() / "gen3_range_30m_confirm_v1" / "confirmed_30m_candidates.parquet"
OUT_DIR = _report_path() / "gen3_range_failure_2024_2026_v1"


def pct(x: float | None) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x) * 100:.2f}%"


def bucket(x: float, cuts: list[float], labels: list[str]) -> str:
    if pd.isna(x):
        return "missing"
    for cut, label in zip(cuts, labels):
        if x <= cut:
            return label
    return labels[-1]


def summarize(df: pd.DataFrame, group_cols: list[str], ret_col: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=group_cols + ["rows", "days", "mean_ret", "median_ret", "win_rate", "worst", "p10", "best"])

    def win(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float((s > 0).mean())

    def p10(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float(s.quantile(0.10))

    return (
        df.groupby(group_cols, dropna=False)
        .agg(
            rows=("code", "size"),
            days=("entry_date", "nunique"),
            mean_ret=(ret_col, "mean"),
            median_ret=(ret_col, "median"),
            win_rate=(ret_col, win),
            worst=(ret_col, "min"),
            p10=(ret_col, p10),
            best=(ret_col, "max"),
        )
        .reset_index()
        .sort_values(group_cols)
    )


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for c in df.columns:
            v = row[c]
            if c in pct_cols:
                item[c] = pct(v)
            elif isinstance(v, float):
                item[c] = f"{v:.4f}"
            else:
                item[c] = "" if pd.isna(v) else str(v)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = pd.read_parquet(BASE)
    conf = pd.read_parquet(CONFIRMED)
    base["entry_date"] = pd.to_datetime(base["entry_date"], errors="coerce")
    conf["entry_date"] = pd.to_datetime(conf["entry_date"], errors="coerce")
    base["year"] = base["entry_date"].dt.year
    conf["year"] = conf["entry_date"].dt.year

    context_cols = [
        "entry_date",
        "code",
        "name",
        "market_style",
        "chain_rank",
        "candidate_score",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "index_mom20",
        "ret1",
        "mom5",
        "mom10",
        "mom20",
        "drawdown20",
        "range_pos60",
        "amount_ratio20",
        "lower_shadow_ratio",
        "close_position",
        "gap_open",
    ]
    ctx = base[[c for c in context_cols if c in base.columns]].copy()
    merged = conf.merge(ctx, on=["entry_date", "code"], how="left", suffixes=("", "_daily"))
    focus = merged[merged["year"].isin([2024, 2026])].copy()
    ret_col = "fwd_ret_confirm_to_close_5d"
    focus["month"] = focus["entry_date"].dt.strftime("%Y-%m")
    focus["rank_bucket"] = focus["chain_rank"].map(lambda x: bucket(x, [5, 10, 20], ["rank<=5", "rank6-10", "rank11-20", "rank>20"]))
    focus["up_rate_bucket"] = focus["up_rate"].map(lambda x: bucket(x, [0.20, 0.35, 0.50], ["ice<=20%", "cold20-35%", "mid35-50%", "warm>50%"]))
    focus["big_down_bucket"] = focus["big_down_rate"].map(lambda x: bucket(x, [0.10, 0.20, 0.35], ["none<10%", "stress10-20%", "cap20-35%", "crash>35%"]))
    focus["amount_bucket"] = focus["amount_ratio20"].map(lambda x: bucket(x, [0.8, 1.2, 2.0], ["dry<=0.8", "normal0.8-1.2", "active1.2-2.0", "hot>2.0"]))
    focus["range_bucket"] = focus["range_pos60"].map(lambda x: bucket(x, [0.0, 0.08, 0.16], ["below_box", "deep_bottom", "bottom8-16%", "upper16%+"]))
    focus["confirm_hour"] = pd.to_datetime(focus["confirm_datetime"], errors="coerce").dt.hour + pd.to_datetime(focus["confirm_datetime"], errors="coerce").dt.minute / 60.0
    focus["confirm_time_bucket"] = focus["confirm_hour"].map(lambda x: bucket(x, [10.5, 11.5, 14.0], ["10:00-10:30", "10:30-11:30", "13:00-14:00", "14:00+"] ))

    tables = {
        "month": summarize(focus, ["year", "month"], ret_col),
        "rank": summarize(focus, ["year", "rank_bucket"], ret_col),
        "up_rate": summarize(focus, ["year", "up_rate_bucket"], ret_col),
        "big_down": summarize(focus, ["year", "big_down_bucket"], ret_col),
        "amount": summarize(focus, ["year", "amount_bucket"], ret_col),
        "range": summarize(focus, ["year", "range_bucket"], ret_col),
        "confirm_time": summarize(focus, ["year", "confirm_time_bucket"], ret_col),
    }
    for name, table in tables.items():
        table.to_csv(OUT_DIR / f"{name}_summary.csv", index=False, encoding="utf-8-sig")

    worst_days = (
        focus.groupby(["entry_date", "year"], dropna=False)
        .agg(rows=("code", "size"), mean_ret=(ret_col, "mean"), sum_ret=(ret_col, "sum"), win_rate=(ret_col, lambda s: float((s > 0).mean())))
        .reset_index()
        .sort_values("sum_ret")
        .head(20)
    )
    worst_trades = focus.sort_values(ret_col).head(40)
    worst_days.to_csv(OUT_DIR / "worst_days.csv", index=False, encoding="utf-8-sig")
    worst_trades.to_csv(OUT_DIR / "worst_trades.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"mean_ret", "median_ret", "win_rate", "worst", "p10", "best", "sum_ret"}
    report = [
        "# G3 Range 2024/2026 失败归因 V1",
        "",
        "## 口径",
        "",
        "- 样本：`box_bottom_stress_reclaim` 经过 30m 确认后的 2024、2026 年交易。",
        "- 目标：判断失败是否能由少数日期/排名/市场宽度/大跌率/量能/确认时间解释。",
        "- 这是归因审计，不新增交易规则。",
        "",
        "## 最差日期",
        "",
        md_table(worst_days, pct_cols=pct_cols),
        "",
        "## 月度",
        "",
        md_table(tables["month"], pct_cols=pct_cols),
        "",
        "## 排名分层",
        "",
        md_table(tables["rank"], pct_cols=pct_cols),
        "",
        "## 市场上涨率分层",
        "",
        md_table(tables["up_rate"], pct_cols=pct_cols),
        "",
        "## 大跌率分层",
        "",
        md_table(tables["big_down"], pct_cols=pct_cols),
        "",
        "## 个股量能分层",
        "",
        md_table(tables["amount"], pct_cols=pct_cols),
        "",
        "## 箱体位置分层",
        "",
        md_table(tables["range"], pct_cols=pct_cols),
        "",
        "## 确认时间分层",
        "",
        md_table(tables["confirm_time"], pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 如果亏损集中在少数日期，后续应优先定义横盘环境暂停；如果各层普遍差，说明当前横盘触发源本身不够好。",
        "- 本报告不建议直接把某个分层变成过滤规则，除非后续 train/valid/blind 和 slot 复算都成立。",
        "",
    ]
    (OUT_DIR / "range_failure_2024_2026_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({
        "out_dir": str(OUT_DIR),
        "rows": int(len(focus)),
        "years": focus["year"].value_counts().sort_index().to_dict(),
        "worst_day": worst_days.head(1).to_dict(orient="records"),
    }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
