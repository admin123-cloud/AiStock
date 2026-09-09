from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402

V4_DIR = report_path("gen3_v4_research_package_v1")
OUT_DIR = report_path("gen3_v4_strong_path_shock_audit_v1")


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


def score_bucket(s: pd.Series) -> pd.Series:
    q = s.rank(pct=True, method="first")
    return pd.cut(q, [0, 0.25, 0.5, 0.75, 1.0], labels=["Q1_low", "Q2", "Q3", "Q4_high"], include_lowest=True).astype(str)


def load_pair() -> pd.DataFrame:
    base = pd.read_csv(V4_DIR / "v4_h10_margin__cost30" / "closed_trades.csv", low_memory=False)
    shock = pd.read_csv(V4_DIR / "v4_h10_margin__cost30_all_shock2" / "closed_trades.csv", low_memory=False)
    for d in [base, shock]:
        d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
        d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
        d["code"] = d["code"].astype(str)
        for col in ["score", "policy_net_ret", "stake", "realized_pnl"]:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    base = base[base["route"].astype(str).eq("strong_main")].copy()
    shock = shock[shock["route"].astype(str).eq("strong_main")].copy()
    key = ["entry_date", "code"]
    cols = key + ["name", "policy_exit_date", "score", "policy_net_ret", "stake", "realized_pnl"]
    m = base[cols].merge(
        shock[key + ["policy_net_ret", "stake", "realized_pnl"]],
        on=key,
        suffixes=("_base", "_shock"),
        how="inner",
    )
    m["year"] = m["entry_date"].dt.year
    m["month"] = m["entry_date"].dt.to_period("M").astype(str)
    m["score_bucket"] = score_bucket(m["score"])
    m["ret_delta"] = m["policy_net_ret_shock"] - m["policy_net_ret_base"]
    m["stake_delta"] = m["stake_shock"] - m["stake_base"]
    m["pnl_delta"] = m["realized_pnl_shock"] - m["realized_pnl_base"]
    m["direct_cost_loss_est"] = -0.02 * m["stake_base"]
    m["path_stake_loss_est"] = m["pnl_delta"] - m["direct_cost_loss_est"]
    return m


def group(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    out = (
        df.groupby(by)
        .agg(
            trades=("code", "count"),
            base_pnl=("realized_pnl_base", "sum"),
            shock_pnl=("realized_pnl_shock", "sum"),
            pnl_delta=("pnl_delta", "sum"),
            direct_cost_loss_est=("direct_cost_loss_est", "sum"),
            path_stake_loss_est=("path_stake_loss_est", "sum"),
            avg_base_ret=("policy_net_ret_base", "mean"),
            avg_shock_ret=("policy_net_ret_shock", "mean"),
            avg_base_stake=("stake_base", "mean"),
            avg_shock_stake=("stake_shock", "mean"),
        )
        .reset_index()
    )
    return out.sort_values("pnl_delta")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = load_pair()
    d.to_csv(OUT_DIR / "strong_trade_delta.csv", index=False, encoding="utf-8-sig")

    by_year = group(d, ["year"])
    by_month = group(d, ["month"])
    by_score = group(d, ["score_bucket"])
    by_code = group(d, ["code", "name"]).head(200)

    daily_density = (
        d.groupby("entry_date")
        .agg(
            strong_trades=("code", "count"),
            base_pnl=("realized_pnl_base", "sum"),
            shock_pnl=("realized_pnl_shock", "sum"),
            pnl_delta=("pnl_delta", "sum"),
            avg_score=("score", "mean"),
            avg_base_stake=("stake_base", "mean"),
        )
        .reset_index()
        .sort_values("pnl_delta")
    )
    for name, table in {
        "by_year.csv": by_year,
        "by_month.csv": by_month,
        "by_score_bucket.csv": by_score,
        "by_code.csv": by_code,
        "by_entry_date.csv": daily_density,
    }.items():
        table.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(
        [
            {
                "trades": len(d),
                "base_pnl": d["realized_pnl_base"].sum(),
                "shock_pnl": d["realized_pnl_shock"].sum(),
                "pnl_delta": d["pnl_delta"].sum(),
                "direct_cost_loss_est": d["direct_cost_loss_est"].sum(),
                "path_stake_loss_est": d["path_stake_loss_est"].sum(),
                "avg_base_ret": d["policy_net_ret_base"].mean(),
                "avg_shock_ret": d["policy_net_ret_shock"].mean(),
                "avg_base_stake": d["stake_base"].mean(),
                "avg_shock_stake": d["stake_shock"].mean(),
            }
        ]
    )
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"avg_base_ret", "avg_shock_ret"}
    lines = [
        "# G3 V4 H10 strong_main all-shock 路径损耗审计 v1",
        "",
        "## 总览",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 年度损耗",
        "",
        md_table(by_year, pct_cols=pct_cols),
        "",
        "## 分数桶损耗",
        "",
        md_table(by_score, pct_cols=pct_cols),
        "",
        "## 月度最大损耗",
        "",
        md_table(by_month, pct_cols=pct_cols, max_rows=15),
        "",
        "## 单日最大损耗",
        "",
        md_table(daily_density, max_rows=15),
        "",
        "## 个股最大损耗",
        "",
        md_table(by_code, pct_cols=pct_cols, max_rows=15),
        "",
        "## 下一步",
        "",
        "- 如果路径损耗主要来自少数高密度日期，优先测试 strong_main 单日容量/冷却，而不是删规则。",
        "- 如果 Q1/Q2 分数桶损耗明显大于 Q3/Q4，优先做低质半仓；反之说明 score 不是有效护栏。",
        "- 如果 path_stake_loss_est 占比高，说明复利路径和连续开仓比单笔 2% 成本更关键。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
