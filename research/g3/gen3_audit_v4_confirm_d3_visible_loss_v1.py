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
PROXY_DIR = _report_path() / "gen3_v4_strong_confirm_d3_combo_probe_v1"
VISIBLE_DIR = _report_path() / "gen3_v4_strong_confirm_d3_visible_combo_v1"
OUT_DIR = _report_path() / "gen3_v4_confirm_d3_visible_loss_audit_v1"


def _pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _bucket_score(s: pd.Series) -> pd.Series:
    q = s.rank(pct=True, method="first")
    return pd.cut(
        q,
        bins=[0.0, 0.25, 0.5, 0.75, 1.0],
        labels=["Q1_low", "Q2", "Q3", "Q4_high"],
        include_lowest=True,
    ).astype(str)


def _bucket_l3(s: pd.Series) -> pd.Series:
    return pd.cut(
        s.fillna(-1),
        bins=[-2, -0.001, 0.10, 0.25, 0.40, 0.50],
        labels=["missing", "l3_0_10", "l3_10_25", "l3_25_40", "l3_40_50"],
        include_lowest=True,
    ).astype(str)


def _load_candidates() -> pd.DataFrame:
    proxy = pd.read_csv(PROXY_DIR / "confirm_d3_veto_l3_candidates_standardized.csv", low_memory=False)
    visible = pd.read_csv(VISIBLE_DIR / "d4_open_candidates_standardized.csv", low_memory=False)
    for d in [proxy, visible]:
        d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
        d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
        d["code"] = d["code"].astype(str)
        d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
        d["score"] = pd.to_numeric(d.get("score"), errors="coerce")
        d["l3_s3"] = pd.to_numeric(d.get("l3_s3"), errors="coerce")
    proxy = proxy[proxy["route"].astype(str).eq("strong_main")].copy()
    visible = visible[visible["route"].astype(str).eq("strong_main")].copy()
    key = ["entry_date", "code"]
    cols = key + ["name", "policy_exit_date", "policy_net_ret", "exit_reason_proxy", "score", "l3_s3"]
    vcols = key + ["policy_exit_date", "policy_net_ret", "visible_execution_note"]
    m = proxy[cols].merge(visible[vcols], on=key, how="inner", suffixes=("_proxy", "_d4"))
    m["year"] = m["entry_date"].dt.year
    m["is_early_exit"] = m["exit_reason_proxy"].astype(str).ne("fixed_h5")
    m["ret_delta"] = m["policy_net_ret_d4"] - m["policy_net_ret_proxy"]
    m["exit_delay_days"] = (m["policy_exit_date_d4"] - m["policy_exit_date_proxy"]).dt.days
    m["score_bucket"] = _bucket_score(m["score"])
    m["l3_bucket"] = _bucket_l3(m["l3_s3"])
    return m


def _load_closed(profile: str = "cost30") -> pd.DataFrame:
    proxy = pd.read_csv(PROXY_DIR / f"confirm_d3_veto_l3__{profile}" / "closed_trades.csv", low_memory=False)
    visible = pd.read_csv(VISIBLE_DIR / f"d4_open__{profile}" / "closed_trades.csv", low_memory=False)
    for d in [proxy, visible]:
        d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
        d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
        d["code"] = d["code"].astype(str)
        d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
        d["realized_pnl"] = pd.to_numeric(d["realized_pnl"], errors="coerce")
        d["stake"] = pd.to_numeric(d["stake"], errors="coerce")
        d["score"] = pd.to_numeric(d.get("score"), errors="coerce")
        d["l3_s3"] = pd.to_numeric(d.get("l3_s3"), errors="coerce")
    proxy = proxy[proxy["route"].astype(str).eq("strong_main")].copy()
    visible = visible[visible["route"].astype(str).eq("strong_main")].copy()
    key = ["entry_date", "code"]
    cols = key + ["name", "policy_exit_date", "policy_net_ret", "exit_reason_proxy", "score", "l3_s3", "stake", "realized_pnl"]
    vcols = key + ["policy_exit_date", "policy_net_ret", "visible_execution_note", "stake", "realized_pnl"]
    m = proxy[cols].merge(visible[vcols], on=key, how="inner", suffixes=("_proxy", "_d4"))
    m["year"] = m["entry_date"].dt.year
    m["is_early_exit"] = m["exit_reason_proxy"].astype(str).ne("fixed_h5")
    m["ret_delta"] = m["policy_net_ret_d4"] - m["policy_net_ret_proxy"]
    m["pnl_delta"] = m["realized_pnl_d4"] - m["realized_pnl_proxy"]
    m["stake_delta"] = m["stake_d4"] - m["stake_proxy"]
    m["exit_delay_days"] = (m["policy_exit_date_d4"] - m["policy_exit_date_proxy"]).dt.days
    m["score_bucket"] = _bucket_score(m["score"])
    m["l3_bucket"] = _bucket_l3(m["l3_s3"])
    return m


def _group(df: pd.DataFrame, by: list[str], include_pnl: bool = False) -> pd.DataFrame:
    work = df[df["is_early_exit"]].copy()
    if work.empty:
        return pd.DataFrame()
    agg = {
        "code": "count",
        "policy_net_ret_proxy": "mean",
        "policy_net_ret_d4": "mean",
        "ret_delta": ["mean", "sum"],
        "exit_delay_days": "mean",
    }
    if include_pnl:
        agg.update({"realized_pnl_proxy": "sum", "realized_pnl_d4": "sum", "pnl_delta": "sum"})
    out = work.groupby(by).agg(agg)
    out.columns = ["_".join(c).strip("_") for c in out.columns.to_flat_index()]
    out = out.reset_index().rename(columns={"code_count": "rows"})
    return out.sort_values("ret_delta_sum")


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    d = df.head(max_rows).copy() if max_rows else df.copy()
    rows: list[dict[str, str]] = []
    for _, row in d.iterrows():
        item: dict[str, str] = {}
        for col in d.columns:
            val = row[col]
            if col in pct_cols:
                item[col] = _pct(val)
            elif isinstance(val, float):
                item[col] = f"{val:.4f}"
            else:
                item[col] = "" if pd.isna(val) else str(val)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = _load_candidates()
    closed = _load_closed("cost30")

    cand_early = candidates[candidates["is_early_exit"]].copy()
    closed_early = closed[closed["is_early_exit"]].copy()

    candidate_summary = pd.DataFrame(
        [
            {
                "scope": "candidate_early_exit",
                "rows": len(cand_early),
                "proxy_avg_ret": cand_early["policy_net_ret_proxy"].mean(),
                "d4_avg_ret": cand_early["policy_net_ret_d4"].mean(),
                "avg_ret_delta": cand_early["ret_delta"].mean(),
                "sum_ret_delta": cand_early["ret_delta"].sum(),
                "worse_rows": int((cand_early["ret_delta"] < 0).sum()),
                "better_rows": int((cand_early["ret_delta"] > 0).sum()),
            },
            {
                "scope": "closed_early_exit_cost30",
                "rows": len(closed_early),
                "proxy_avg_ret": closed_early["policy_net_ret_proxy"].mean(),
                "d4_avg_ret": closed_early["policy_net_ret_d4"].mean(),
                "avg_ret_delta": closed_early["ret_delta"].mean(),
                "sum_ret_delta": closed_early["ret_delta"].sum(),
                "proxy_pnl": closed_early["realized_pnl_proxy"].sum(),
                "d4_pnl": closed_early["realized_pnl_d4"].sum(),
                "pnl_delta": closed_early["pnl_delta"].sum(),
                "worse_rows": int((closed_early["ret_delta"] < 0).sum()),
                "better_rows": int((closed_early["ret_delta"] > 0).sum()),
            },
        ]
    )

    candidate_summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(OUT_DIR / "candidate_delta.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(OUT_DIR / "closed_delta_cost30.csv", index=False, encoding="utf-8-sig")

    group_tables = {
        "candidate_by_year.csv": _group(candidates, ["year"]),
        "candidate_by_score_bucket.csv": _group(candidates, ["score_bucket"]),
        "candidate_by_l3_bucket.csv": _group(candidates, ["l3_bucket"]),
        "closed_by_year_cost30.csv": _group(closed, ["year"], include_pnl=True),
        "closed_by_score_bucket_cost30.csv": _group(closed, ["score_bucket"], include_pnl=True),
        "closed_by_l3_bucket_cost30.csv": _group(closed, ["l3_bucket"], include_pnl=True),
    }
    for name, table in group_tables.items():
        table.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    worst_candidates = cand_early.sort_values("ret_delta").head(30)
    worst_closed = closed_early.sort_values("pnl_delta").head(30)
    worst_candidates.to_csv(OUT_DIR / "worst_candidate_delta.csv", index=False, encoding="utf-8-sig")
    worst_closed.to_csv(OUT_DIR / "worst_closed_pnl_delta_cost30.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "proxy_avg_ret",
        "d4_avg_ret",
        "avg_ret_delta",
        "sum_ret_delta",
        "policy_net_ret_proxy_mean",
        "policy_net_ret_d4_mean",
        "ret_delta_mean",
        "ret_delta_sum",
    }
    lines = [
        "# G3 V4 confirm_d3 代理到 D4 可见执行损耗审计 v1",
        "",
        "## 结论",
        "",
        "- 只审计 `strong_main + confirm_d3_veto_l3`，目标是判断 D3 收盘代理卖点与最早可见 D4 开盘卖点之间的损耗来源。",
        "- 这里不是新策略调参，只做失败拆解，避免把未来函数代理继续往正式规则里推进。",
        "",
        "## 总览",
        "",
        _md_table(candidate_summary, pct_cols=pct_cols),
        "",
        "## 候选级年度损耗",
        "",
        _md_table(group_tables["candidate_by_year.csv"], pct_cols=pct_cols),
        "",
        "## 候选级 score 分桶",
        "",
        _md_table(group_tables["candidate_by_score_bucket.csv"], pct_cols=pct_cols),
        "",
        "## 候选级 l3_s3 分桶",
        "",
        _md_table(group_tables["candidate_by_l3_bucket.csv"], pct_cols=pct_cols),
        "",
        "## 成交级年度损耗 cost30",
        "",
        _md_table(group_tables["closed_by_year_cost30.csv"], pct_cols=pct_cols),
        "",
        "## 成交级最大 PnL 损耗 cost30",
        "",
        _md_table(
            worst_closed[
                [
                    "entry_date",
                    "code",
                    "name",
                    "policy_exit_date_proxy",
                    "policy_exit_date_d4",
                    "policy_net_ret_proxy",
                    "policy_net_ret_d4",
                    "ret_delta",
                    "stake_proxy",
                    "stake_d4",
                    "pnl_delta",
                    "score",
                    "l3_s3",
                ]
            ],
            pct_cols={"policy_net_ret_proxy", "policy_net_ret_d4", "ret_delta"},
            max_rows=15,
        ),
        "",
        "## 下一步",
        "",
        "- 如果损耗主要集中在少数年份或高温 l3 桶，先做强势链路的环境/热度降杠杆，而不是继续优化 D3 卖点。",
        "- 如果损耗分布广泛，说明日线确认退出这条路本身太晚，下一轮应转向真实 30m 可见触发或强势入场质量过滤。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
