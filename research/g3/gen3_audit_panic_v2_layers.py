from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df


RET_COLS = [
    "fwd_ret_confirm_to_close_1d",
    "fwd_ret_confirm_to_close_2d",
    "fwd_ret_confirm_to_close_3d",
    "fwd_ret_confirm_to_close_5d",
    "fwd_ret_confirm_to_close_10d",
    "fwd_ret_confirm_to_close_20d",
]


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{v * 100:.2f}%"


def _bucket(series: pd.Series, bins: list[float], labels: list[str]) -> pd.Series:
    return pd.cut(series.astype(float), bins=bins, labels=labels, include_lowest=True).astype("string").fillna("missing")


def _qbucket(series: pd.Series, labels: list[str]) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    valid = s.dropna()
    if valid.nunique() < len(labels):
        return pd.Series(["missing"] * len(series), index=series.index, dtype="string")
    return pd.qcut(s, q=len(labels), labels=labels, duplicates="drop").astype("string").fillna("missing")


def _summarize(df: pd.DataFrame, group_cols: list[str], min_signals: int) -> pd.DataFrame:
    rows: list[dict] = []
    if df.empty:
        return pd.DataFrame()
    for keys, group in df.groupby(group_cols, dropna=False, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        item = {col: val for col, val in zip(group_cols, keys)}
        item["signals"] = int(len(group))
        item["days"] = int(group["entry_date"].nunique())
        item["unique_codes"] = int(group["code"].nunique())
        item["status"] = "ok" if len(group) >= min_signals else "small_sample"
        for col in RET_COLS:
            label = col.replace("fwd_ret_confirm_to_close_", "")
            s = pd.to_numeric(group[col], errors="coerce").dropna()
            item[f"n_{label}"] = int(len(s))
            item[f"mean_{label}"] = float(s.mean()) if len(s) else np.nan
            item[f"median_{label}"] = float(s.median()) if len(s) else np.nan
            item[f"win_{label}"] = float((s > 0).mean()) if len(s) else np.nan
        rows.append(item)
    return pd.DataFrame(rows)


def _display(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    for col in out.columns:
        if col.startswith(("mean_", "median_", "win_")):
            out[col] = out[col].map(_pct)
    return out


def _load_stock_meta(codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=["code", "industry", "float_share", "total_share"])
    escaped = ",".join("'" + c.replace("'", "\\'") + "'" for c in sorted(set(codes)))
    sql = f"""
    SELECT code, industry, float_share, total_share
    FROM stocks
    WHERE code IN ({escaped})
    """
    return clickhouse_query_df(sql)


def _top_bottom_year_notes(year_summary: pd.DataFrame, chain: str) -> list[str]:
    d = year_summary[(year_summary["g3_chain"] == chain) & (year_summary["signals"] >= 10)].copy()
    if d.empty:
        return [f"- `{chain}` 年度样本不足，暂时不能做年份稳定性判断。"]
    d["mean_5d_num"] = pd.to_numeric(d["mean_5d"], errors="coerce")
    best = d.sort_values("mean_5d_num", ascending=False).head(2)
    worst = d.sort_values("mean_5d_num", ascending=True).head(2)
    lines = [f"- `{chain}` 年度稳定性："]
    lines.append("  - 较强年份：" + "；".join(f"{int(r.year)} 信号 {int(r.signals)}，5日 {_pct(r.mean_5d_num)}" for r in best.itertuples()))
    lines.append("  - 较弱年份：" + "；".join(f"{int(r.year)} 信号 {int(r.signals)}，5日 {_pct(r.mean_5d_num)}" for r in worst.itertuples()))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit G3 Panic V2 layer stability.")
    parser.add_argument("--signals", default="reports/gen3_panic_v2_research/intraday_confirm_selected_v1/labeled_confirmed_signals.parquet")
    parser.add_argument("--candidates", default="reports/gen3_panic_v2_research/panic_v2_candidates.parquet")
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/layer_audit_v1")
    parser.add_argument("--min-signals", type=int, default=20)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    signals = pd.read_parquet(args.signals)
    candidates = pd.read_parquet(args.candidates)
    signals["entry_date"] = pd.to_datetime(signals["entry_date"]).dt.date
    candidates["entry_date"] = pd.to_datetime(candidates["entry_date"]).dt.date

    context_cols = [
        "entry_date",
        "code",
        "g3_chain",
        "name",
        "market_style",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "index_mom20",
        "mom20",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "range_pos20",
        "range_pos60",
        "amount20",
        "amount_ratio20",
        "turnover_rate",
        "lower_shadow_ratio",
        "close_position",
        "gap_open",
    ]
    merged = signals.merge(
        candidates[[c for c in context_cols if c in candidates.columns]],
        on=["entry_date", "code", "g3_chain"],
        how="left",
        suffixes=("", "_cand"),
    )
    meta = _load_stock_meta(merged["code"].astype(str).unique().tolist())
    if not meta.empty:
        merged = merged.merge(meta, on="code", how="left")

    merged["year"] = pd.to_datetime(merged["entry_date"]).dt.year
    merged["up_rate_bucket"] = _bucket(
        merged["up_rate"],
        [-np.inf, 0.20, 0.35, 0.50, 0.65, np.inf],
        ["<=20%", "20-35%", "35-50%", "50-65%", ">65%"],
    )
    merged["big_down_bucket"] = _bucket(
        merged["big_down_rate"],
        [-np.inf, 0.03, 0.07, 0.12, 0.20, np.inf],
        ["<=3%", "3-7%", "7-12%", "12-20%", ">20%"],
    )
    merged["range_pos60_bucket"] = _bucket(
        merged["range_pos60"],
        [-np.inf, 0.15, 0.30, 0.50, 0.70, np.inf],
        ["low<=15%", "15-30%", "30-50%", "50-70%", ">70%"],
    )
    merged["drawdown20_bucket"] = _bucket(
        merged["drawdown20"],
        [-np.inf, -0.30, -0.20, -0.12, -0.06, np.inf],
        ["<=-30%", "-30~-20%", "-20~-12%", "-12~-6%", ">-6%"],
    )
    merged["liquidity_amount20_bucket"] = _qbucket(merged["amount20"], ["low_liq", "mid_low_liq", "mid_high_liq", "high_liq"])
    if "float_share" in merged.columns:
        merged["float_mcap_proxy"] = pd.to_numeric(merged["float_share"], errors="coerce") * pd.to_numeric(merged["daily_entry_close"], errors="coerce")
        merged["float_mcap_bucket"] = _qbucket(merged["float_mcap_proxy"], ["small", "mid_small", "mid_large", "large"])
    else:
        merged["float_mcap_bucket"] = "missing"
    merged["industry_top"] = merged.get("industry", pd.Series(index=merged.index, dtype="object")).fillna("missing").replace("", "missing")

    merged.to_parquet(out_dir / "layer_labeled_signals.parquet", index=False)
    merged.to_csv(out_dir / "layer_labeled_signals.csv", index=False, encoding="utf-8-sig")

    specs = {
        "year": ["g3_chain", "year"],
        "market_style": ["g3_chain", "market_style"],
        "up_rate_bucket": ["g3_chain", "up_rate_bucket"],
        "big_down_bucket": ["g3_chain", "big_down_bucket"],
        "range_pos60_bucket": ["g3_chain", "range_pos60_bucket"],
        "drawdown20_bucket": ["g3_chain", "drawdown20_bucket"],
        "liquidity_amount20_bucket": ["g3_chain", "liquidity_amount20_bucket"],
        "float_mcap_bucket": ["g3_chain", "float_mcap_bucket"],
        "industry": ["g3_chain", "industry_top"],
        "confirm_rule": ["g3_chain", "confirm_rule"],
    }
    raw_tables: dict[str, pd.DataFrame] = {}
    for name, group_cols in specs.items():
        raw = _summarize(merged, group_cols, args.min_signals)
        raw_tables[name] = raw
        raw.to_csv(out_dir / f"{name}_summary_raw.csv", index=False, encoding="utf-8-sig")
        _display(raw).to_csv(out_dir / f"{name}_summary_display.csv", index=False, encoding="utf-8-sig")

    deep = merged[merged["g3_chain"] == "panic_v2_deep_wash_repair"].copy()
    no_limit = merged[merged["g3_chain"] == "panic_v2_no_limit_capitulation"].copy()
    ice = merged[merged["g3_chain"] == "panic_v2_icepoint_reclaim"].copy()

    summary = {
        "signals_path": args.signals,
        "candidates_path": args.candidates,
        "output_dir": str(out_dir),
        "rows": int(len(merged)),
        "chains": {str(k): int(v) for k, v in merged["g3_chain"].value_counts().to_dict().items()},
        "min_signals": args.min_signals,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    year_display = _display(raw_tables["year"])
    style_display = _display(raw_tables["market_style"])
    up_display = _display(raw_tables["up_rate_bucket"])
    range_display = _display(raw_tables["range_pos60_bucket"])
    liq_display = _display(raw_tables["liquidity_amount20_bucket"])
    industry_raw = raw_tables["industry"].copy()
    industry_raw["mean_5d_sort"] = pd.to_numeric(industry_raw["mean_5d"], errors="coerce")
    industry_display = _display(industry_raw.sort_values(["g3_chain", "signals", "mean_5d_sort"], ascending=[True, False, False]).drop(columns=["mean_5d_sort"]).groupby("g3_chain").head(8))

    lines: list[str] = [
        "# G3 Panic V2 分层稳定性审计",
        "",
        "## 口径",
        "",
        f"- 输入信号：`{args.signals}`",
        f"- 输入候选：`{args.candidates}`",
        f"- 输出目录：`{out_dir}`",
        f"- 样本数：`{len(merged)}`",
        "- 入场口径：30m 确认价，且已按买入日日线/分钟价格比例校准到日线复权口径。",
        "- 目的：做防过拟合审计，不在本脚本中调参。",
        "",
        "## 核心判断",
        "",
    ]
    lines.extend(_top_bottom_year_notes(raw_tables["year"], "panic_v2_deep_wash_repair"))
    lines.extend(_top_bottom_year_notes(raw_tables["year"], "panic_v2_no_limit_capitulation"))
    lines.append("- `panic_v2_icepoint_reclaim` 样本最多，但多数分层 5 日收益接近 0，当前更适合作为候选池环境标签。")
    lines.append("- 若某一层样本低于阈值，标记为 `small_sample`，不能作为正式规则依据。")

    def add_table(title: str, df: pd.DataFrame, cols: list[str]) -> None:
        lines.extend(["", f"## {title}", ""])
        if df.empty:
            lines.append("无数据。")
            return
        lines.append(df[[c for c in cols if c in df.columns]].to_markdown(index=False))

    common_cols = ["g3_chain", "signals", "days", "unique_codes", "status", "mean_3d", "win_3d", "mean_5d", "win_5d", "mean_10d", "win_10d", "mean_20d", "win_20d"]
    add_table("年度分层", year_display, ["g3_chain", "year", *common_cols[1:]])
    add_table("市场风格分层", style_display, ["g3_chain", "market_style", *common_cols[1:]])
    add_table("上涨率分层", up_display, ["g3_chain", "up_rate_bucket", *common_cols[1:]])
    add_table("个股60日位置分层", range_display, ["g3_chain", "range_pos60_bucket", *common_cols[1:]])
    add_table("流动性分层", liq_display, ["g3_chain", "liquidity_amount20_bucket", *common_cols[1:]])
    add_table("行业样本Top", industry_display, ["g3_chain", "industry_top", *common_cols[1:]])

    lines.extend(
        [
            "",
            "## 下一步建议",
            "",
            "1. `deep_wash_repair` 进入下一轮，但只能做粗粒度、可解释的约束，例如剔除明显弱层，不做多参数网格搜索。",
            "2. `no_limit_capitulation` 继续单独观察，优先补年份和行业分布，不因为 valid/full 漂亮就并入正式买点。",
            "3. `icepoint_reclaim` 暂不作为买点，只作为候选源或市场恐慌标签。",
        ]
    )
    (out_dir / "layer_audit_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
