from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_range_weak_tail_failure_v1"
SOURCES = {
    "range_stress_top1": ROOT / "reports" / "gen3_range_weak_shadow_mtm_v1" / "range_stress_top1_cost30_closed_trades.csv",
    "weak_low_top1": ROOT / "reports" / "gen3_range_weak_shadow_mtm_v1" / "weak_low_top1_cost30_closed_trades.csv",
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


def _load() -> pd.DataFrame:
    parts = []
    for book, path in SOURCES.items():
        d = pd.read_csv(path, low_memory=False)
        d["book"] = book
        parts.append(d)
    out = pd.concat(parts, ignore_index=True)
    out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce")
    for col in [
        "net_ret",
        "fwd_ret_open_to_close_1d",
        "fwd_ret_open_to_close_2d",
        "fwd_ret_open_to_close_3d",
        "fwd_ret_open_to_close_5d",
        "fwd_ret_open_to_close_10d",
        "fwd_ret_open_to_close_20d",
        "up_rate",
        "breadth_ma20",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "index_mom20",
        "adx20",
        "plus_di20",
        "minus_di20",
        "ret1",
        "mom5",
        "mom10",
        "mom20",
        "drawdown5",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "range_pos20",
        "range_pos60",
        "amount20",
        "amount_ratio20",
        "lower_shadow_ratio",
        "close_position",
        "gap_open",
        "box_width60",
        "candidate_score",
    ]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out["bad10"] = out["net_ret"] <= -0.10
    out["bad15"] = out["net_ret"] <= -0.15
    out["year"] = out["entry_date"].dt.year
    return out


def _add_buckets(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["amount_ratio_bucket"] = pd.cut(
        out["amount_ratio20"],
        bins=[-np.inf, 1.0, 1.5, 2.0, 2.5, np.inf],
        labels=["<=1.0", "1.0-1.5", "1.5-2.0", "2.0-2.5", ">2.5"],
    ).astype(str)
    out["drawdown20_bucket"] = pd.cut(
        out["drawdown20"],
        bins=[-np.inf, -0.30, -0.22, -0.15, -0.08, np.inf],
        labels=["<=-30%", "-30~-22%", "-22~-15%", "-15~-8%", ">-8%"],
    ).astype(str)
    out["range_pos_bucket"] = pd.cut(
        out["range_pos60"],
        bins=[-np.inf, 0.12, 0.20, 0.35, 0.55, np.inf],
        labels=["<=12%", "12-20%", "20-35%", "35-55%", ">55%"],
    ).astype(str)
    out["close_pos_bucket"] = pd.cut(
        out["close_position"],
        bins=[-np.inf, 0.55, 0.75, 0.90, 0.99, np.inf],
        labels=["<=55%", "55-75%", "75-90%", "90-99%", ">=99%"],
    ).astype(str)
    out["index_mom20_bucket"] = pd.cut(
        out["index_mom20"],
        bins=[-np.inf, -0.08, -0.04, 0.0, 0.04, np.inf],
        labels=["<-8%", "-8~-4%", "-4~0%", "0~4%", ">4%"],
    ).astype(str)
    out["breadth_bucket"] = pd.cut(
        out["breadth_ma20"],
        bins=[-np.inf, 0.25, 0.40, 0.55, 0.70, np.inf],
        labels=["<=25%", "25-40%", "40-55%", "55-70%", ">70%"],
    ).astype(str)
    out["up_rate_bucket"] = pd.cut(
        out["up_rate"],
        bins=[-np.inf, 0.25, 0.40, 0.55, 0.70, np.inf],
        labels=["<=25%", "25-40%", "40-55%", "55-70%", ">70%"],
    ).astype(str)
    out["gap_bucket"] = pd.cut(
        out["gap_open"],
        bins=[-np.inf, -0.04, -0.02, 0.02, 0.05, np.inf],
        labels=["gap<-4%", "-4~-2%", "-2~2%", "2~5%", ">5%"],
    ).astype(str)
    return out


def _summary(d: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    def rate(s: pd.Series) -> float:
        return float(s.mean()) if len(s) else np.nan

    def q10(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float(s.quantile(0.10))

    return (
        d.groupby(group_cols, dropna=False)
        .agg(
            rows=("code", "size"),
            mean_ret=("net_ret", "mean"),
            median_ret=("net_ret", "median"),
            bad10_rate=("bad10", rate),
            bad15_rate=("bad15", rate),
            p10=("net_ret", q10),
            worst=("net_ret", "min"),
            win_rate=("net_ret", lambda s: float((s > 0).mean()) if len(s) else np.nan),
        )
        .reset_index()
        .sort_values(group_cols + ["rows"], ascending=[True] * len(group_cols) + [False])
    )


def _guard_probe(d: pd.DataFrame) -> pd.DataFrame:
    guards = []
    specs = {
        "base": pd.Series(True, index=d.index),
        "avoid_amount_gt2_5": d["amount_ratio20"].le(2.5),
        "avoid_amount_gt2_0": d["amount_ratio20"].le(2.0),
        "avoid_closepos_99": d["close_position"].lt(0.99),
        "avoid_deep_drawdown30": d["drawdown20"].gt(-0.30),
        "avoid_gap_up5": d["gap_open"].le(0.05),
        "avoid_range_highvol_deep": ~((d["book"].eq("range_stress_top1")) & (d["amount_ratio20"].gt(2.0)) & (d["drawdown20"].le(-0.18))),
        "avoid_weak_hot_close": ~((d["book"].eq("weak_low_top1")) & (d["close_position"].ge(0.99)) & (d["amount_ratio20"].gt(1.8))),
    }
    for name, mask in specs.items():
        part = d[mask].copy()
        if part.empty:
            guards.append({"guard": name, "rows": 0})
            continue
        row = _summary(part.assign(guard=name), ["guard"]).iloc[0].to_dict()
        row["drop_rows"] = int(len(d) - len(part))
        row["drop_bad10"] = int(d["bad10"].sum() - part["bad10"].sum())
        row["drop_good5"] = int(((d["net_ret"] >= 0.05).sum()) - ((part["net_ret"] >= 0.05).sum()))
        guards.append(row)
    return pd.DataFrame(guards)


def _worst_table(d: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "book",
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "net_ret",
        "fwd_ret_open_to_close_1d",
        "fwd_ret_open_to_close_2d",
        "fwd_ret_open_to_close_3d",
        "amount_ratio20",
        "drawdown20",
        "range_pos60",
        "close_position",
        "gap_open",
        "up_rate",
        "breadth_ma20",
        "big_down_rate",
        "index_mom20",
        "candidate_score",
    ]
    return d.sort_values("net_ret").head(30)[cols]


def _write_report(overall: pd.DataFrame, buckets: pd.DataFrame, guards: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {
        "mean_ret",
        "median_ret",
        "bad10_rate",
        "bad15_rate",
        "p10",
        "worst",
        "win_rate",
        "net_ret",
        "fwd_ret_open_to_close_1d",
        "fwd_ret_open_to_close_2d",
        "fwd_ret_open_to_close_3d",
        "amount_ratio20",
        "drawdown20",
        "range_pos60",
        "close_position",
        "gap_open",
        "up_rate",
        "breadth_ma20",
        "big_down_rate",
        "index_mom20",
    }
    focus_buckets = buckets[
        buckets["bucket_name"].isin(["amount_ratio_bucket", "drawdown20_bucket", "close_pos_bucket", "breadth_bucket", "index_mom20_bucket"])
    ].copy()
    lines = [
        "# G3 横盘/弱反弹 Shadow 尾部失败归因 V1",
        "",
        "## 口径",
        "",
        "- 样本：第39步 30bps shadow 成交，`range_stress_top1` 与 `weak_low_top1`。",
        "- 只使用入场日前已经可见的日线/市场字段，不使用退出后信息做过滤。",
        "- 本报告只做归因和粗护栏探针，不把任何护栏直接纳入正式策略。",
        "",
        "## 总览",
        "",
        _md_table(overall, pct_cols=pct_cols),
        "",
        "## 关键分桶",
        "",
        _md_table(focus_buckets, pct_cols=pct_cols),
        "",
        "## 粗护栏探针",
        "",
        _md_table(guards, pct_cols=pct_cols),
        "",
        "## 最差样本",
        "",
        _md_table(worst, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 如果护栏减少的好交易数量接近或超过减少的坏交易，说明它不是合格风控，只能作为 shadow 标签。",
        "- 单一字段若只解释少数极端样本，不应转成硬规则；下一步优先验证 30m 失败可见性与次日不可成交风险。",
        "",
    ]
    (OUT_DIR / "tail_failure_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = _add_buckets(_load())
    overall = _summary(d, ["book"])
    bucket_parts = []
    for col in [
        "amount_ratio_bucket",
        "drawdown20_bucket",
        "range_pos_bucket",
        "close_pos_bucket",
        "index_mom20_bucket",
        "breadth_bucket",
        "up_rate_bucket",
        "gap_bucket",
    ]:
        part = _summary(d.assign(bucket_name=col, bucket_value=d[col]), ["book", "bucket_name", "bucket_value"])
        bucket_parts.append(part)
    buckets = pd.concat(bucket_parts, ignore_index=True)
    guards = _guard_probe(d)
    worst = _worst_table(d)

    overall.to_csv(OUT_DIR / "overall.csv", index=False, encoding="utf-8-sig")
    buckets.to_csv(OUT_DIR / "bucket_summary.csv", index=False, encoding="utf-8-sig")
    guards.to_csv(OUT_DIR / "guard_probe.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst_trades.csv", index=False, encoding="utf-8-sig")
    _write_report(overall, buckets, guards, worst)
    print(json.dumps({"out_dir": str(OUT_DIR), "overall": overall.to_dict(orient="records"), "guards": guards.to_dict(orient="records")}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
