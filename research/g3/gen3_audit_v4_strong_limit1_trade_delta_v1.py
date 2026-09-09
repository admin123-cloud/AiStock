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
OUT_DIR = _report_path() / "gen3_v4_strong_limit1_trade_delta_v1"


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
    p = SRC_DIR / f"{variant}__{profile}" / "closed_trades.csv"
    d = pd.read_csv(p, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d["route"] = d["route"].astype(str)
    for col in ["score", "policy_net_ret", "stake", "realized_pnl"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    return d


def summarize_group(d: pd.DataFrame, by: list[str]) -> pd.DataFrame:
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
    base = load_closed("base", "cost30")
    limit1 = load_closed("strong_daily_limit1", "cost30")
    key = ["entry_date", "code", "route"]

    b = base[base["route"].eq("strong_main")].copy()
    l = limit1[limit1["route"].eq("strong_main")].copy()
    b_key = b[key].drop_duplicates()
    l_key = l[key].drop_duplicates()
    base_only = b.merge(l_key, on=key, how="left", indicator=True)
    base_only = base_only[base_only["_merge"].eq("left_only")].drop(columns=["_merge"])
    limit_only = l.merge(b_key, on=key, how="left", indicator=True)
    limit_only = limit_only[limit_only["_merge"].eq("left_only")].drop(columns=["_merge"])
    common = b.merge(l[key + ["stake", "realized_pnl"]], on=key, how="inner", suffixes=("_base", "_limit1"))
    common["stake_delta"] = common["stake_limit1"] - common["stake_base"]
    common["pnl_delta"] = common["realized_pnl_limit1"] - common["realized_pnl_base"]

    base_only.to_csv(OUT_DIR / "base_only_strong_trades.csv", index=False, encoding="utf-8-sig")
    limit_only.to_csv(OUT_DIR / "limit1_only_strong_trades.csv", index=False, encoding="utf-8-sig")
    common.to_csv(OUT_DIR / "common_strong_trade_stake_delta.csv", index=False, encoding="utf-8-sig")

    base_only_year = summarize_group(base_only, ["year"])
    limit_only_year = summarize_group(limit_only, ["year"])
    common_year = (
        common.groupby("year")
        .agg(
            trades=("code", "count"),
            base_pnl=("realized_pnl_base", "sum"),
            limit1_pnl=("realized_pnl_limit1", "sum"),
            pnl_delta=("pnl_delta", "sum"),
            avg_stake_delta=("stake_delta", "mean"),
        )
        .reset_index()
    )
    daily_base_only = summarize_group(base_only, ["entry_date"])
    daily_base_only = daily_base_only.sort_values("pnl", ascending=False)

    base_only_year.to_csv(OUT_DIR / "base_only_by_year.csv", index=False, encoding="utf-8-sig")
    limit_only_year.to_csv(OUT_DIR / "limit1_only_by_year.csv", index=False, encoding="utf-8-sig")
    common_year.to_csv(OUT_DIR / "common_by_year.csv", index=False, encoding="utf-8-sig")
    daily_base_only.to_csv(OUT_DIR / "base_only_by_entry_date.csv", index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(
        [
            {
                "bucket": "base_only_skipped_by_limit1",
                "trades": len(base_only),
                "avg_ret": base_only["policy_net_ret"].mean(),
                "pnl": base_only["realized_pnl"].sum(),
                "win_rate": float((base_only["policy_net_ret"] > 0).mean()) if len(base_only) else 0.0,
                "avg_score": base_only["score"].mean(),
            },
            {
                "bucket": "limit1_only_replacement",
                "trades": len(limit_only),
                "avg_ret": limit_only["policy_net_ret"].mean(),
                "pnl": limit_only["realized_pnl"].sum(),
                "win_rate": float((limit_only["policy_net_ret"] > 0).mean()) if len(limit_only) else 0.0,
                "avg_score": limit_only["score"].mean(),
            },
            {
                "bucket": "common_stake_path_delta",
                "trades": len(common),
                "pnl": common["pnl_delta"].sum(),
                "avg_score": common["score"].mean(),
                "avg_stake_delta": common["stake_delta"].mean(),
            },
        ]
    )
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"avg_ret", "win_rate", "worst_ret", "best_ret"}
    lines = [
        "# G3 V4 strong_daily_limit1 交易差异审计 v1",
        "",
        "## 总览",
        "",
        md_table(summary, pct_cols={"avg_ret", "win_rate"}),
        "",
        "## 被 limit1 跳过的 base strong 年度",
        "",
        md_table(base_only_year, pct_cols=pct_cols),
        "",
        "## limit1 替代新增 strong 年度",
        "",
        md_table(limit_only_year, pct_cols=pct_cols),
        "",
        "## 共同成交的仓位路径差异",
        "",
        md_table(common_year),
        "",
        "## 被跳过的最大赢家日期",
        "",
        md_table(daily_base_only, pct_cols=pct_cols, max_rows=15),
        "",
        "## 下一步",
        "",
        "- 如果 base_only 在 2024/2026 是高胜率正收益，limit1 会削掉关键进攻，需要寻找更温和的冷却。",
        "- 如果 base_only 主要在 2025 高收益但 all-shock 风险大，limit1 可作为研究候选继续压力测试。",
        "- 共同成交的 `pnl_delta` 可判断收益下降是否来自被跳过交易，还是复利仓位变小。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
