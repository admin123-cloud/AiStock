from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path

import pandas as pd


ROOT = _PROJECT_ROOT
SRC_DIR = _report_path() / "gen3_v4_strong_position_scale_probe_v1"
OUT_DIR = _report_path() / "gen3_v4_strong_second_q75_trade_delta_v1"


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


def load_closed(variant: str, profile: str = "cost30") -> pd.DataFrame:
    d = pd.read_csv(SRC_DIR / f"{variant}__{profile}" / "closed_trades.csv", low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d["route"] = d["route"].astype(str)
    for col in ["score", "policy_net_ret", "stake", "realized_pnl", "strong_day_rank"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    return d


def summarize(d: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    if d.empty:
        return pd.DataFrame()
    out = (
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
    )
    return out.sort_values(by)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = load_closed("base")
    q75 = load_closed("strong_second_q75")
    key = ["entry_date", "code", "route"]
    base_strong = base[base["route"].eq("strong_main")].copy()
    q75_strong = q75[q75["route"].eq("strong_main")].copy()

    q75_keys = q75_strong[key].drop_duplicates()
    base_keys = base_strong[key].drop_duplicates()
    skipped = base_strong.merge(q75_keys, on=key, how="left", indicator=True)
    skipped = skipped[skipped["_merge"].eq("left_only")].drop(columns=["_merge"])
    added = q75_strong.merge(base_keys, on=key, how="left", indicator=True)
    added = added[added["_merge"].eq("left_only")].drop(columns=["_merge"])
    common = base_strong.merge(q75_strong[key + ["stake", "realized_pnl"]], on=key, how="inner", suffixes=("_base", "_q75"))
    common["stake_delta"] = common["stake_q75"] - common["stake_base"]
    common["pnl_delta"] = common["realized_pnl_q75"] - common["realized_pnl_base"]

    skipped.to_csv(OUT_DIR / "skipped_by_q75.csv", index=False, encoding="utf-8-sig")
    added.to_csv(OUT_DIR / "added_by_q75.csv", index=False, encoding="utf-8-sig")
    common.to_csv(OUT_DIR / "common_stake_delta.csv", index=False, encoding="utf-8-sig")

    skipped_year = summarize(skipped, ["year"])
    added_year = summarize(added, ["year"])
    skipped_rank = summarize(skipped, ["strong_day_rank"]) if "strong_day_rank" in skipped.columns else pd.DataFrame()
    skipped_top = skipped.sort_values("realized_pnl", ascending=False).head(30)
    common_year = (
        common.groupby("year")
        .agg(
            trades=("code", "count"),
            base_pnl=("realized_pnl_base", "sum"),
            q75_pnl=("realized_pnl_q75", "sum"),
            pnl_delta=("pnl_delta", "sum"),
            avg_stake_delta=("stake_delta", "mean"),
        )
        .reset_index()
    )

    for name, table in {
        "skipped_by_year.csv": skipped_year,
        "added_by_year.csv": added_year,
        "skipped_by_strong_day_rank.csv": skipped_rank,
        "common_by_year.csv": common_year,
        "skipped_top_winners.csv": skipped_top,
    }.items():
        table.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(
        [
            {
                "bucket": "skipped_by_q75",
                "trades": len(skipped),
                "avg_ret": skipped["policy_net_ret"].mean(),
                "pnl": skipped["realized_pnl"].sum(),
                "win_rate": float((skipped["policy_net_ret"] > 0).mean()) if len(skipped) else 0.0,
                "avg_score": skipped["score"].mean(),
            },
            {
                "bucket": "added_by_q75",
                "trades": len(added),
                "avg_ret": added["policy_net_ret"].mean(),
                "pnl": added["realized_pnl"].sum(),
                "win_rate": float((added["policy_net_ret"] > 0).mean()) if len(added) else 0.0,
                "avg_score": added["score"].mean(),
            },
            {
                "bucket": "common_path_delta",
                "trades": len(common),
                "pnl": common["pnl_delta"].sum(),
                "avg_stake_delta": common["stake_delta"].mean(),
            },
        ]
    )
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"avg_ret", "win_rate", "worst_ret", "best_ret"}
    lines = [
        "# G3 V4 strong_second_q75 交易差异审计 v1",
        "",
        "## 总览",
        "",
        md_table(summary, pct_cols={"avg_ret", "win_rate"}),
        "",
        "## 被 q75 跳过的 strong 年度",
        "",
        md_table(skipped_year, pct_cols=pct_cols),
        "",
        "## q75 替代新增 strong 年度",
        "",
        md_table(added_year, pct_cols=pct_cols),
        "",
        "## 被跳过 strong_day_rank 分布",
        "",
        md_table(skipped_rank, pct_cols=pct_cols),
        "",
        "## 共同成交仓位路径差异",
        "",
        md_table(common_year),
        "",
        "## 被跳过的最大赢家",
        "",
        md_table(
            skipped_top[["entry_date", "code", "name", "score", "strong_day_rank", "policy_net_ret", "stake", "realized_pnl"]],
            pct_cols={"policy_net_ret"},
            max_rows=15,
        ),
        "",
        "## 下一步",
        "",
        "- 若 q75 跳过的赢家主要是第 2 笔低分强势，且 2024/2026 损失小于 daily_limit1，则 q75 比 limit1 更适合作为候选护栏。",
        "- 仍需做 next-open、跌停延迟与真实盘口滑点审计，不能直接接实盘。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
