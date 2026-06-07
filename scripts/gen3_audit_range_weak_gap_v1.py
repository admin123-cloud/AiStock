from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_weak_gap_audit_v1"
RET_COL = "fwd_ret_open_to_close_5d"


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


def _summarize(df: pd.DataFrame, group_cols: list[str], ret_col: str = RET_COL) -> pd.DataFrame:
    def win_rate(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float((s > 0).mean())

    def p10(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float(s.quantile(0.10))

    def p90(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float(s.quantile(0.90))

    return (
        df.groupby(group_cols, dropna=False)
        .agg(
            rows=("code", "size"),
            days=("entry_date", "nunique"),
            mean_ret=(ret_col, "mean"),
            median_ret=(ret_col, "median"),
            win_rate=(ret_col, win_rate),
            p10=(ret_col, p10),
            p90=(ret_col, p90),
            worst=(ret_col, "min"),
            best=(ret_col, "max"),
        )
        .reset_index()
        .sort_values(group_cols)
    )


def _add_common_fields(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    for col in [
        "high60",
        "low60",
        "close",
        "range_pos60",
        "range_pos20",
        "drawdown20",
        "runup_from_60d_low",
        "up_rate",
        "breadth_ma20",
        "big_down_rate",
        "index_mom20",
        "amount_ratio20",
        "close_position",
        "lower_shadow_ratio",
        "mom5",
        "mom10",
        "mom20",
        RET_COL,
    ]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["box_width60"] = (d["high60"] - d["low60"]) / d["low60"]
    d["reclaim_quality"] = np.where(
        (d["close_position"] >= 0.60) | (d["lower_shadow_ratio"] >= 0.30),
        "has_reclaim",
        "no_reclaim",
    )
    d["icepoint_level"] = np.select(
        [
            (d["up_rate"] <= 0.20) | (d["breadth_ma20"] <= 0.25),
            (d["up_rate"] <= 0.35) | (d["breadth_ma20"] <= 0.40),
        ],
        ["ice", "cold"],
        default="normal",
    )
    d["box_width_bucket"] = pd.cut(
        d["box_width60"],
        bins=[-np.inf, 0.25, 0.40, 0.60, np.inf],
        labels=["tight<=25%", "normal25-40%", "wide40-60%", "too_wide>60%"],
    ).astype(str)
    d["range_pos_bucket"] = pd.cut(
        d["range_pos60"],
        bins=[-np.inf, 0.12, 0.20, 0.35, np.inf],
        labels=["floor<=12%", "bottom12-20%", "low20-35%", "above35%"],
    ).astype(str)
    d["drawdown_bucket"] = pd.cut(
        d["drawdown20"],
        bins=[-np.inf, -0.25, -0.15, -0.08, np.inf],
        labels=["deep<=-25%", "wash-15~-25%", "pullback-8~-15%", "shallow>-8%"],
    ).astype(str)
    return d


def _make_range_probes(range_df: pd.DataFrame) -> pd.DataFrame:
    d = range_df.copy()
    masks = {
        "range_v1_all": pd.Series(True, index=d.index),
        "true_box_bottom": (d["box_width60"].between(0.18, 0.55)) & (d["range_pos60"] <= 0.20),
        "ice_box_reclaim": (d["range_pos60"] <= 0.20)
        & (d["icepoint_level"].isin(["ice", "cold"]))
        & (d["reclaim_quality"].eq("has_reclaim")),
        "tight_box_reclaim": (d["box_width60"] <= 0.40)
        & (d["range_pos60"] <= 0.25)
        & (d["reclaim_quality"].eq("has_reclaim")),
        "stress_no_crash": (d["range_pos60"] <= 0.20)
        & (d["big_down_rate"].between(0.05, 0.25))
        & (d["reclaim_quality"].eq("has_reclaim")),
        "avoid_false_range": (d["box_width60"] <= 0.55)
        & (d["range_pos60"] <= 0.25)
        & (d["index_mom20"] >= -0.08)
        & (d["big_down_rate"] < 0.20),
    }
    return _probe_frame(d, masks, "range")


def _make_weak_probes(weak_df: pd.DataFrame) -> pd.DataFrame:
    d = weak_df.copy()
    masks = {
        "weak_v1_all": pd.Series(True, index=d.index),
        "repair_close_strong": (d["close_position"] >= 0.65) & (d["mom5"] >= 0.0),
        "washed_repair": d["drawdown20"].between(-0.25, -0.10)
        & (d["close_position"] >= 0.60)
        & (d["amount_ratio20"].between(0.8, 2.5)),
        "low_not_chasing": (d["range_pos60"].between(0.10, 0.45))
        & (d["runup_from_60d_low"] <= 0.30)
        & (d["mom10"] <= 0.10),
        "rebound_market_ok": (d["index_mom20"] >= -0.05)
        & (d["up_rate"] >= 0.40)
        & (d["breadth_ma20"] >= 0.35),
        "weak_repair_combo": d["drawdown20"].between(-0.25, -0.10)
        & (d["range_pos60"].between(0.10, 0.45))
        & (d["close_position"] >= 0.60)
        & (d["mom5"] >= 0.0)
        & (d["index_mom20"] >= -0.05),
    }
    return _probe_frame(d, masks, "weak_rebound")


def _probe_frame(df: pd.DataFrame, masks: dict[str, pd.Series], family: str) -> pd.DataFrame:
    parts = []
    for probe, mask in masks.items():
        part = df[mask].copy()
        if part.empty:
            parts.append(pd.DataFrame({"probe_family": [family], "probe": [probe], "empty": [True]}))
            continue
        part["probe_family"] = family
        part["probe"] = probe
        part["empty"] = False
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def _worst_days(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    day = (
        df.groupby(["probe_family", "probe", "entry_date"], dropna=False)
        .agg(rows=("code", "size"), mean_ret=(RET_COL, "mean"), worst=(RET_COL, "min"))
        .reset_index()
    )
    return day.sort_values(["probe_family", "probe", "mean_ret"]).groupby(["probe_family", "probe"]).head(5)


def _write_report(summary: pd.DataFrame, by_year: pd.DataFrame, buckets: pd.DataFrame, worst_days: pd.DataFrame) -> None:
    pct_cols = {"mean_ret", "median_ret", "win_rate", "p10", "p90", "worst", "best"}
    focus = summary.sort_values(["probe_family", "mean_ret"], ascending=[True, False])
    report_lines = [
        "# G3 横盘/弱反弹缺口审计 V1",
        "",
        "## 目的",
        "",
        "- 不做新参数最优化，只检查旧 V1 横盘与弱反弹候选到底败在哪里。",
        "- 重点观察 2023/2024：如果某个 probe 只在单一年份好看，不能作为正式买法。",
        "- 所有收益口径仍是信号日收盘后可见、下一交易日开盘买入、5 日收盘前瞻收益，不是组合回测。",
        "",
        "## Probe 总览",
        "",
        _md_table(focus, pct_cols=pct_cols),
        "",
        "## 年度稳定性",
        "",
        _md_table(by_year.sort_values(["probe_family", "probe", "year"]), pct_cols=pct_cols),
        "",
        "## 结构分桶",
        "",
        _md_table(buckets, pct_cols=pct_cols),
        "",
        "## 最差日期",
        "",
        _md_table(worst_days, pct_cols={"mean_ret", "worst"}),
        "",
        "## 初步判断",
        "",
        "- 横盘链路如果只有 `ice_box_reclaim` 或 `tight_box_reclaim` 略有改善，但年度仍不稳定，说明问题在箱体定义，不在 30m 确认。",
        "- 弱反弹链路如果 `weak_repair_combo` 样本大幅减少且 2023/2024 仍薄，说明它更像强势链路的弱版，不能承担独立环境收益源。",
        "- 下一步只允许把通过年度稳定性检查的 probe 转成 shadow 候选；没有通过则继续重写候选源，不并入组合。",
        "",
    ]
    (OUT_DIR / "range_weak_gap_audit_report_cn.md").write_text("\n".join(report_lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _add_common_fields(pd.read_parquet(SOURCE))
    target = df[df["g3_chain"].isin(["range_box_bottom", "weak_rebound_repair"])].copy()
    probes = pd.concat(
        [
            _make_range_probes(target[target["g3_chain"].eq("range_box_bottom")]),
            _make_weak_probes(target[target["g3_chain"].eq("weak_rebound_repair")]),
        ],
        ignore_index=True,
    )
    probes = probes[~probes["empty"].fillna(False)].copy()
    summary = _summarize(probes, ["probe_family", "probe"])
    by_year = _summarize(probes, ["probe_family", "probe", "year"])
    bucket_parts = []
    for family, cols in {
        "range": ["box_width_bucket", "range_pos_bucket", "icepoint_level", "reclaim_quality"],
        "weak_rebound": ["drawdown_bucket", "range_pos_bucket", "icepoint_level", "reclaim_quality"],
    }.items():
        fam = target[target["g3_chain"].eq("range_box_bottom" if family == "range" else "weak_rebound_repair")]
        for col in cols:
            part = _summarize(fam.assign(probe_family=family, bucket_name=col, bucket_value=fam[col]), ["probe_family", "bucket_name", "bucket_value"])
            bucket_parts.append(part)
    buckets = pd.concat(bucket_parts, ignore_index=True)
    worst = _worst_days(probes)

    probes.to_parquet(OUT_DIR / "probe_labeled_candidates.parquet", index=False)
    probes.to_csv(OUT_DIR / "probe_labeled_candidates.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "probe_summary.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "probe_year_summary.csv", index=False, encoding="utf-8-sig")
    buckets.to_csv(OUT_DIR / "structure_bucket_summary.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst_days.csv", index=False, encoding="utf-8-sig")
    _write_report(summary, by_year, buckets, worst)
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
