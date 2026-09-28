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
SOURCE = _report_path() / "gen3_range_weak_gap_audit_v1" / "probe_labeled_candidates.parquet"
OUT_DIR = _report_path() / "gen3_range_weak_rank_capacity_v1"
RET_COL = "fwd_ret_open_to_close_5d"
TOP_NS = (1, 3, 5, 10)
FOCUS_PROBES = {
    "range": ["range_v1_all", "ice_box_reclaim", "stress_no_crash"],
    "weak_rebound": ["weak_v1_all", "low_not_chasing", "weak_repair_combo"],
}


def _pct(x: object) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x):.2%}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            val = row[col]
            if col in pct_cols:
                item[col] = _pct(val)
            elif isinstance(val, float):
                item[col] = f"{val:.4f}"
            else:
                item[col] = "" if pd.isna(val) else str(val)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _summarize(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    def win_rate(s: pd.Series) -> float:
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
            mean_ret=(RET_COL, "mean"),
            median_ret=(RET_COL, "median"),
            win_rate=(RET_COL, win_rate),
            p10=(RET_COL, p10),
            worst=(RET_COL, "min"),
            best=(RET_COL, "max"),
        )
        .reset_index()
        .sort_values(group_cols)
    )


def _select_topn(df: pd.DataFrame) -> pd.DataFrame:
    parts = []
    focus_pairs = {(family, probe) for family, probes in FOCUS_PROBES.items() for probe in probes}
    d = df[df[["probe_family", "probe"]].apply(tuple, axis=1).isin(focus_pairs)].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    d["candidate_score"] = pd.to_numeric(d["candidate_score"], errors="coerce")
    d[RET_COL] = pd.to_numeric(d[RET_COL], errors="coerce")
    d = d.dropna(subset=["entry_date", "candidate_score", RET_COL])
    d = d.sort_values(["probe_family", "probe", "entry_date", "candidate_score", "amount20"], ascending=[True, True, True, False, False])
    d["probe_day_rank"] = d.groupby(["probe_family", "probe", "entry_date"]).cumcount() + 1
    for top_n in TOP_NS:
        part = d[d["probe_day_rank"] <= top_n].copy()
        part["top_n"] = top_n
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def _daily_basket_summary(selected: pd.DataFrame) -> pd.DataFrame:
    day = (
        selected.groupby(["probe_family", "probe", "top_n", "entry_date"], dropna=False)
        .agg(rows=("code", "size"), basket_ret=(RET_COL, "mean"), worst=(RET_COL, "min"))
        .reset_index()
    )
    day["year"] = pd.to_datetime(day["entry_date"], errors="coerce").dt.year
    return day


def _write_report(summary: pd.DataFrame, annual: pd.DataFrame, basket: pd.DataFrame, worst_days: pd.DataFrame) -> None:
    pct_cols = {"mean_ret", "median_ret", "win_rate", "p10", "worst", "best", "basket_ret"}
    lines = [
        "# G3 横盘/弱反弹排序与容量审计 V1",
        "",
        "## 口径",
        "",
        "- 只读取第37步 probe 候选，不新增买点条件。",
        "- 每个 probe 按 `candidate_score` 在同一入场日内取 Top1/Top3/Top5/Top10。",
        "- 这里仍是事件收益审计，不是资金曲线，也不代表可以进入组合。",
        "",
        "## TopN 事件质量",
        "",
        _md_table(summary.sort_values(["probe_family", "top_n", "mean_ret"], ascending=[True, True, False]), pct_cols=pct_cols),
        "",
        "## 年度稳定性",
        "",
        _md_table(annual.sort_values(["probe_family", "probe", "top_n", "year"]), pct_cols=pct_cols),
        "",
        "## 每日篮子最差日期",
        "",
        _md_table(worst_days, pct_cols={"basket_ret", "worst"}),
        "",
        "## 判断",
        "",
        "- 如果 Top1/Top3 仍不能让 2022/2023/2024 同时改善，说明排序和容量不是核心问题。",
        "- 如果只有 Top1 好，且交易日过少，则只能作为影子观察，不足以承担第三条独立收益链路。",
        "- 下一步应根据本报告决定：可以影子化的转 shadow；不能影子化的重写候选源。",
        "",
    ]
    (OUT_DIR / "rank_capacity_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    selected = _select_topn(pd.read_parquet(SOURCE))
    basket = _daily_basket_summary(selected)
    summary = _summarize(selected, ["probe_family", "probe", "top_n"])
    annual = _summarize(selected, ["probe_family", "probe", "top_n", "year"])
    worst_days = (
        basket.sort_values(["probe_family", "probe", "top_n", "basket_ret"])
        .groupby(["probe_family", "probe", "top_n"])
        .head(5)
        .reset_index(drop=True)
    )

    selected.to_parquet(OUT_DIR / "topn_selected_candidates.parquet", index=False)
    selected.to_csv(OUT_DIR / "topn_selected_candidates.csv", index=False, encoding="utf-8-sig")
    basket.to_csv(OUT_DIR / "topn_daily_basket.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "topn_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "topn_year_summary.csv", index=False, encoding="utf-8-sig")
    worst_days.to_csv(OUT_DIR / "topn_worst_days.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, annual, basket, worst_days)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "summary": summary.to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
