from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_box_bottom_audit_v1"


def pct(x: float | None) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x) * 100:.2f}%"


def bucket_range_pos(x: float) -> str:
    if pd.isna(x):
        return "missing"
    if x <= 0.15:
        return "bottom<=15%"
    if x <= 0.30:
        return "low15-30%"
    if x <= 0.50:
        return "mid30-50%"
    return "upper>50%"


def bucket_up_rate(x: float) -> str:
    if pd.isna(x):
        return "missing"
    if x <= 0.20:
        return "ice<=20%"
    if x <= 0.35:
        return "cold20-35%"
    if x <= 0.50:
        return "mid35-50%"
    return "warm>50%"


def bucket_big_down(x: float) -> str:
    if pd.isna(x):
        return "missing"
    if x >= 0.20:
        return "capitulation>=20%"
    if x >= 0.10:
        return "stress10-20%"
    if x >= 0.05:
        return "mild5-10%"
    return "none<5%"


def bucket_drawdown20(x: float) -> str:
    if pd.isna(x):
        return "missing"
    if x <= -0.25:
        return "deep<=-25%"
    if x <= -0.15:
        return "wash-15~-25%"
    if x <= -0.08:
        return "pullback-8~-15%"
    return "shallow>-8%"


def summarize(df: pd.DataFrame, group_cols: list[str], ret_col: str) -> pd.DataFrame:
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
        .sort_values(group_cols + ["rows"], ascending=[True] * len(group_cols) + [False])
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


def variant_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    bottom = df["range_pos60"].le(0.20)
    ice = df["up_rate"].le(0.25)
    stress = df["big_down_rate"].ge(0.10)
    drawdown = df["drawdown20"].le(-0.12)
    reclaim = df["close_position"].ge(0.55) | df["lower_shadow_ratio"].ge(0.25)
    not_crash = df["big_down_rate"].lt(0.35)
    liquid = df["amount_ratio20"].ge(0.8)
    return {
        "base_range_box_bottom": pd.Series(True, index=df.index),
        "box_bottom_only": bottom,
        "box_bottom_icepoint": bottom & ice,
        "box_bottom_stress_reclaim": bottom & stress & reclaim & not_crash,
        "box_bottom_ice_reclaim_liquid": bottom & ice & reclaim & liquid,
        "drawdown_box_reclaim": bottom & drawdown & reclaim & not_crash,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df = df[df["g3_chain"].eq("range_box_bottom")].copy()
    ret_col = "fwd_ret_open_to_close_5d"

    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    df["range_pos60_bucket"] = df["range_pos60"].map(bucket_range_pos)
    df["up_rate_bucket"] = df["up_rate"].map(bucket_up_rate)
    df["big_down_bucket"] = df["big_down_rate"].map(bucket_big_down)
    df["drawdown20_bucket"] = df["drawdown20"].map(bucket_drawdown20)
    df["reclaim_flag"] = np.where((df["close_position"].ge(0.55) | df["lower_shadow_ratio"].ge(0.25)), "reclaim_or_shadow", "no_reclaim")

    bucket_tables = {
        "range_pos60": summarize(df, ["range_pos60_bucket"], ret_col),
        "up_rate": summarize(df, ["up_rate_bucket"], ret_col),
        "big_down": summarize(df, ["big_down_bucket"], ret_col),
        "drawdown20": summarize(df, ["drawdown20_bucket"], ret_col),
        "reclaim": summarize(df, ["reclaim_flag"], ret_col),
    }
    for name, table in bucket_tables.items():
        table.to_csv(OUT_DIR / f"{name}_summary.csv", index=False, encoding="utf-8-sig")

    variant_rows = []
    for name, mask in variant_masks(df).items():
        part = df[mask].copy()
        if part.empty:
            variant_rows.append({"variant": name, "rows": 0, "days": 0})
            continue
        row = summarize(part.assign(variant=name), ["variant"], ret_col).iloc[0].to_dict()
        for window_name, wmask in {
            "train_to_2024": pd.to_datetime(part["entry_date"]) < pd.Timestamp("2025-01-01"),
            "blind_2025_plus": pd.to_datetime(part["entry_date"]) >= pd.Timestamp("2025-01-01"),
        }.items():
            wpart = part[wmask]
            row[f"{window_name}_rows"] = int(len(wpart))
            row[f"{window_name}_mean"] = float(wpart[ret_col].mean()) if not wpart.empty else np.nan
            row[f"{window_name}_win"] = float((wpart[ret_col].dropna() > 0).mean()) if wpart[ret_col].notna().any() else np.nan
        variant_rows.append(row)
    variants = pd.DataFrame(variant_rows).sort_values(["mean_ret", "rows"], ascending=[False, False])
    year_variant = []
    for name, mask in variant_masks(df).items():
        part = df[mask].copy()
        if part.empty:
            continue
        part["variant"] = name
        year_variant.append(summarize(part, ["variant", "year"], ret_col))
    year_variant_df = pd.concat(year_variant, ignore_index=True) if year_variant else pd.DataFrame()

    variants.to_csv(OUT_DIR / "range_variant_summary.csv", index=False, encoding="utf-8-sig")
    year_variant_df.to_csv(OUT_DIR / "range_variant_year_summary.csv", index=False, encoding="utf-8-sig")

    top = variants.head(10)
    report = [
        "# G3 横盘箱体底部审计 V1",
        "",
        "## 口径",
        "",
        f"- 源文件：`{SOURCE.relative_to(ROOT)}`",
        f"- 样本：`range_box_bottom`，{len(df)} 条，{df['entry_date'].min()} ~ {df['entry_date'].max()}",
        f"- 评价：次日开盘到 5 日收盘收益 `{ret_col}`。",
        "- 只测试少量预设结构：箱体底部、上涨率冰点、大跌出清、反抽/下影、流动性，不做参数网格搜索。",
        "",
        "## 变体结果",
        "",
        md_table(top, pct_cols={"mean_ret", "median_ret", "win_rate", "worst", "p10", "best", "train_to_2024_mean", "train_to_2024_win", "blind_2025_plus_mean", "blind_2025_plus_win"}),
        "",
        "## 核心分层",
        "",
        "### 箱体位置",
        "",
        md_table(bucket_tables["range_pos60"], pct_cols={"mean_ret", "median_ret", "win_rate", "worst", "p10", "best"}),
        "",
        "### 上涨率冰点",
        "",
        md_table(bucket_tables["up_rate"], pct_cols={"mean_ret", "median_ret", "win_rate", "worst", "p10", "best"}),
        "",
        "### 大跌出清",
        "",
        md_table(bucket_tables["big_down"], pct_cols={"mean_ret", "median_ret", "win_rate", "worst", "p10", "best"}),
        "",
        "## 初步判断",
        "",
        "- 现有 `range_box_bottom` 不能直接用作 G3 横盘买法，它太宽，容易把普通弱势下跌也收进来。",
        "- 如果变体没有同时在 2025 以后盲测保持正收益，就不能进入正式候选，只能继续定义触发源。",
        "- 下一步应把最稳的 1-2 个变体做成独立候选源，再接 30m 确认；如果盲测不稳，则重写横盘定义。",
        "",
    ]
    (OUT_DIR / "range_box_bottom_audit_report_cn.md").write_text("\n".join(report), encoding="utf-8")

    print(json.dumps({
        "out_dir": str(OUT_DIR),
        "rows": int(len(df)),
        "date_min": str(df["entry_date"].min()),
        "date_max": str(df["entry_date"].max()),
        "top_variants": top[["variant", "rows", "mean_ret", "win_rate", "blind_2025_plus_mean", "blind_2025_plus_win"]].to_dict(orient="records"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
