from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "reports" / "gen3_range_box_structural_entry_v1" / "box_first_panic_strong_reclaim__cost30"
OUT_DIR = ROOT / "reports" / "gen3_box_first_panic_strong_reclaim_audit_v1"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
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


def add_bins(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["year"] = d["entry_date_ts"].dt.year.astype("Int64").astype(str)
    d["window"] = pd.cut(
        d["entry_date_ts"],
        bins=pd.to_datetime(["2019-12-31", "2023-12-31", "2025-12-31", "2026-12-31"]),
        labels=["train_2020_2023", "valid_2024_2025", "blind_2026ytd"],
    ).astype(str)
    d["result_bucket"] = pd.cut(
        d["policy_net_ret"],
        bins=[-10, -0.10, -0.03, 0, 0.03, 0.10, 10],
        labels=["deep_loss_<=-10", "loss_-10_-3", "small_loss_-3_0", "small_win_0_3", "win_3_10", "big_win_>=10"],
    ).astype(str)
    d["breadth_bucket"] = pd.cut(
        d["up_rate"],
        bins=[-1, 0.20, 0.35, 0.50, 0.70, 2],
        labels=["ice_<=20", "weak_20_35", "mid_35_50", "warm_50_70", "hot_>70"],
    ).astype(str)
    d["big_down_bucket"] = pd.cut(
        d["big_down_rate"],
        bins=[-1, 0.05, 0.10, 0.20, 0.40, 2],
        labels=["calm_<5", "mild_5_10", "panic_10_20", "heavy_20_40", "crash_>40"],
    ).astype(str)
    d["amount20_bucket"] = pd.cut(
        d["amount_ratio20"],
        bins=[-1, 0.8, 1.0, 1.5, 2.5, 100],
        labels=["low_<0.8", "normal_0.8_1", "active_1_1.5", "hot_1.5_2.5", "extreme_>2.5"],
    ).astype(str)
    d["runup_bucket"] = pd.cut(
        d["runup_from_60d_low"],
        bins=[-10, 0, 0.05, 0.15, 0.30, 10],
        labels=["below_low", "tiny_0_5", "bounce_5_15", "runup_15_30", "high_>30"],
    ).astype(str)
    d["mom10_bucket"] = pd.cut(
        d["mom10"],
        bins=[-10, -0.20, -0.12, -0.06, 0, 10],
        labels=["collapse_<-20", "deep_-20_-12", "drop_-12_-6", "mild_-6_0", "positive"],
    ).astype(str)
    d["index_mom_bucket"] = pd.cut(
        d["index_mom20"],
        bins=[-10, -0.10, -0.05, 0, 0.05, 10],
        labels=["index_bad_<-10", "index_weak_-10_-5", "index_soft_-5_0", "index_ok_0_5", "index_strong_>5"],
    ).astype(str)
    return d


def group_stats(df: pd.DataFrame, by: str) -> pd.DataFrame:
    g = df.groupby(by, dropna=False)
    out = g["policy_net_ret"].agg(["count", "mean", "median", "min", "max"]).reset_index()
    out["win_rate"] = g["policy_net_ret"].apply(lambda s: float((s > 0).mean())).values
    out["deep_loss_rate"] = g["policy_net_ret"].apply(lambda s: float((s <= -0.10).mean())).values
    out["big_win_rate"] = g["policy_net_ret"].apply(lambda s: float((s >= 0.10).mean())).values
    return out.sort_values(["mean", "count"], ascending=[False, False])


def compare_features(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "up_rate",
        "big_down_rate",
        "index_mom20",
        "breadth_ma20",
        "breadth_ma60",
        "drawdown5",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "range_pos60",
        "amount_ratio5",
        "amount_ratio20",
        "lower_shadow_ratio",
        "close_position",
        "gap_open",
        "mom5",
        "mom10",
        "mom20",
    ]
    rows = []
    for col in cols:
        x = pd.to_numeric(df[col], errors="coerce")
        win = x[df["policy_net_ret"] > 0]
        loss = x[df["policy_net_ret"] <= 0]
        deep = x[df["policy_net_ret"] <= -0.10]
        rows.append(
            {
                "feature": col,
                "win_mean": float(win.mean()) if len(win) else None,
                "loss_mean": float(loss.mean()) if len(loss) else None,
                "deep_loss_mean": float(deep.mean()) if len(deep) else None,
                "diff_win_minus_loss": float(win.mean() - loss.mean()) if len(win) and len(loss) else None,
            }
        )
    return pd.DataFrame(rows).sort_values("diff_win_minus_loss", ascending=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    closed = pd.read_csv(IN_DIR / "closed_trades.csv", encoding="utf-8-sig")
    for col in closed.columns:
        if col not in {"code", "name", "g3_chain", "market_style", "ma_skeleton", "volume_price_layer", "adx_layer", "variant", "desc"}:
            closed[col] = pd.to_numeric(closed[col], errors="ignore")
    closed = add_bins(closed)

    group_cols = [
        "year",
        "window",
        "market_style",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_bucket",
        "big_down_bucket",
        "amount20_bucket",
        "runup_bucket",
        "mom10_bucket",
        "index_mom_bucket",
        "result_bucket",
    ]
    for col in group_cols:
        group_stats(closed, col).to_csv(OUT_DIR / f"group_by_{col}.csv", index=False, encoding="utf-8-sig")

    feature_cmp = compare_features(closed)
    feature_cmp.to_csv(OUT_DIR / "feature_win_loss_compare.csv", index=False, encoding="utf-8-sig")

    worst = closed.sort_values("policy_net_ret").head(30)
    worst_cols = [
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "market_style",
        "up_rate",
        "big_down_rate",
        "index_mom20",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "amount_ratio20",
        "close_position",
        "lower_shadow_ratio",
        "mom10",
    ]
    worst[worst_cols].to_csv(OUT_DIR / "worst_30_trades.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "mean",
        "median",
        "min",
        "max",
        "win_rate",
        "deep_loss_rate",
        "big_win_rate",
        "policy_net_ret",
        "up_rate",
        "big_down_rate",
        "index_mom20",
        "drawdown10",
        "drawdown20",
        "runup_from_60d_low",
        "amount_ratio20",
        "close_position",
        "lower_shadow_ratio",
        "mom10",
    }
    report = [
        "# G3 box_first_panic_strong_reclaim 失败样本归因 v1",
        "",
        "## 英文名解释",
        "",
        "- `box_first_panic_strong_reclaim`：箱体底部强修复恐慌。个股在箱体极低位置，短期跌幅较深，入场日收盘修复较强。",
        "- `deep_loss_rate`：单笔 5 日净收益小于等于 -10% 的深亏比例。",
        "- `big_win_rate`：单笔 5 日净收益大于等于 +10% 的大赚比例。",
        "",
        "## 按年份",
        "",
        md_table(group_stats(closed, "year"), pct_cols=pct_cols),
        "",
        "## 按市场风格",
        "",
        md_table(group_stats(closed, "market_style"), pct_cols=pct_cols),
        "",
        "## 按上涨率宽度",
        "",
        md_table(group_stats(closed, "breadth_bucket"), pct_cols=pct_cols),
        "",
        "## 按市场大跌比例",
        "",
        md_table(group_stats(closed, "big_down_bucket"), pct_cols=pct_cols),
        "",
        "## 按 20 日量能",
        "",
        md_table(group_stats(closed, "amount20_bucket"), pct_cols=pct_cols),
        "",
        "## 按近 10 日动量",
        "",
        md_table(group_stats(closed, "mom10_bucket"), pct_cols=pct_cols),
        "",
        "## 赢家/输家特征均值差",
        "",
        md_table(feature_cmp, pct_cols={c for c in feature_cmp.columns if c != "feature"}),
        "",
        "## 最大亏损 30 笔",
        "",
        md_table(worst[worst_cols], pct_cols=pct_cols),
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
