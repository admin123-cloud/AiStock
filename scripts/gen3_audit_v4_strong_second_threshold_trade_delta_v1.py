from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "reports" / "gen3_v4_strong_position_scale_probe_v1"
OUT_DIR = ROOT / "reports" / "gen3_v4_strong_second_threshold_trade_delta_v1"
VARIANTS = ["strong_second_score_ge_093", "strong_second_score_ge_095"]


def pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    d = df.head(max_rows).copy() if max_rows else df.copy()
    rows: list[dict[str, str]] = []
    for _, row in d.iterrows():
        item: dict[str, str] = {}
        for col in d.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_closed(variant: str) -> pd.DataFrame:
    d = pd.read_csv(SRC_DIR / f"{variant}__cost30" / "closed_trades.csv", low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d["route"] = d["route"].astype(str)
    for col in ["score", "policy_net_ret", "stake", "realized_pnl", "strong_day_rank"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    return d[d["route"].eq("strong_main")].copy()


def summarize(d: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    if d.empty:
        return pd.DataFrame()
    return (
        d.groupby(by)
        .agg(
            trades=("code", "count"),
            avg_ret=("policy_net_ret", "mean"),
            pnl=("realized_pnl", "sum"),
            avg_score=("score", "mean"),
            avg_stake=("stake", "mean"),
            win_rate=("policy_net_ret", lambda s: float((s > 0).mean())),
            worst_ret=("policy_net_ret", "min"),
            best_ret=("policy_net_ret", "max"),
        )
        .reset_index()
        .sort_values(by)
    )


def compare_to_base(base: pd.DataFrame, variant: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    d = load_closed(variant)
    key = ["entry_date", "code", "route"]
    d_keys = d[key].drop_duplicates()
    b_keys = base[key].drop_duplicates()
    skipped = base.merge(d_keys, on=key, how="left", indicator=True)
    skipped = skipped[skipped["_merge"].eq("left_only")].drop(columns=["_merge"])
    added = d.merge(b_keys, on=key, how="left", indicator=True)
    added = added[added["_merge"].eq("left_only")].drop(columns=["_merge"])
    common = base.merge(d[key + ["stake", "realized_pnl"]], on=key, how="inner", suffixes=("_base", "_variant"))
    common["pnl_delta"] = common["realized_pnl_variant"] - common["realized_pnl_base"]
    common["stake_delta"] = common["stake_variant"] - common["stake_base"]
    summary = pd.DataFrame(
        [
            {
                "variant": variant,
                "bucket": "skipped_vs_base",
                "trades": len(skipped),
                "avg_ret": skipped["policy_net_ret"].mean(),
                "pnl": skipped["realized_pnl"].sum(),
                "win_rate": float((skipped["policy_net_ret"] > 0).mean()) if len(skipped) else 0.0,
                "avg_score": skipped["score"].mean(),
            },
            {
                "variant": variant,
                "bucket": "added_vs_base",
                "trades": len(added),
                "avg_ret": added["policy_net_ret"].mean(),
                "pnl": added["realized_pnl"].sum(),
                "win_rate": float((added["policy_net_ret"] > 0).mean()) if len(added) else 0.0,
                "avg_score": added["score"].mean(),
            },
            {
                "variant": variant,
                "bucket": "common_path_delta",
                "trades": len(common),
                "pnl": common["pnl_delta"].sum(),
                "avg_stake_delta": common["stake_delta"].mean(),
            },
        ]
    )
    return summary, summarize(skipped, ["year"]), summarize(added, ["year"]), skipped.sort_values("realized_pnl", ascending=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_closed("base")
    summary_parts = []
    report_lines = ["# G3 V4 strong 第二笔固定阈值交易差异审计 v1", ""]
    for variant in VARIANTS:
        summary, skipped_year, added_year, skipped_top = compare_to_base(base, variant)
        summary_parts.append(summary)
        summary.to_csv(OUT_DIR / f"{variant}_summary.csv", index=False, encoding="utf-8-sig")
        skipped_year.to_csv(OUT_DIR / f"{variant}_skipped_by_year.csv", index=False, encoding="utf-8-sig")
        added_year.to_csv(OUT_DIR / f"{variant}_added_by_year.csv", index=False, encoding="utf-8-sig")
        skipped_top.to_csv(OUT_DIR / f"{variant}_skipped_top_winners.csv", index=False, encoding="utf-8-sig")
        report_lines.extend(
            [
                f"## {variant}",
                "",
                "### 总览",
                "",
                md_table(summary, pct_cols={"avg_ret", "win_rate"}),
                "",
                "### 被跳过 strong 年度",
                "",
                md_table(skipped_year, pct_cols={"avg_ret", "win_rate", "worst_ret", "best_ret"}),
                "",
                "### 被跳过最大赢家",
                "",
                md_table(
                    skipped_top[["entry_date", "code", "name", "score", "strong_day_rank", "policy_net_ret", "stake", "realized_pnl"]],
                    pct_cols={"policy_net_ret"},
                    max_rows=12,
                ),
                "",
            ]
        )

    all_summary = pd.concat(summary_parts, ignore_index=True)
    all_summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    report_lines.extend(
        [
            "## 下一步判断",
            "",
            "- 若 0.93 比 0.95 多保留的收益主要来自 2020/2024/2025 且没有明显扩大弱势窗口亏损，可作为更进攻候选。",
            "- 若 0.95 的回撤优势明显且收益损失可接受，则 0.95 更适合作为稳健候选。",
            "- 两者仍必须继续做 next-open、跌停延迟和真实盘口滑点，不可直接接实盘。",
        ]
    )
    (OUT_DIR / "report_cn.md").write_text("\n".join(report_lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
