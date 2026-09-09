from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
IN_FILE = _report_path() / "gen3_weak_rebound_g2v4_entry_30m_accept_v1" / "weak_rebound_30m_accept_audit.csv"
OUT_DIR = _report_path() / "gen3_weak_rebound_g2v4_entry_fail_d1_repair_v1"


def pct(x: Any) -> str:
    if x is None or pd.isna(x):
        return "--"
    return f"{float(x) * 100:+.2f}%"


def num(x: Any) -> str:
    if x is None or pd.isna(x):
        return "--"
    if isinstance(x, float):
        return f"{x:.4f}"
    return str(x)


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            item[col] = pct(value) if col in pct_cols else num(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_data() -> pd.DataFrame:
    d = pd.read_csv(IN_FILE, low_memory=False)
    d["entry_date_ts"] = pd.to_datetime(d["entry_date_ts"], errors="coerce").dt.normalize()
    d["buy_datetime"] = pd.to_datetime(d["buy_datetime"], errors="coerce")
    bool_cols = [
        "entry_warning",
        "early_weak_confirm",
        "d1_weak_confirm",
        "d2_weak_confirm",
        "basic_accept",
        "strong_accept",
        "fail_after_buy",
        "bad5_trade",
        "loss_trade",
    ]
    for col in bool_cols:
        if col in d.columns:
            d[col] = d[col].map({"True": True, "False": False, True: True, False: False}).fillna(False)
    num_cols = [
        "weighted_return",
        "buy_price",
        "d1_close",
        "d2_close",
        "d1_close_ret",
        "d2_close_ret",
        "d1_low_ret",
        "d2_low_ret",
        "entry_upper_shadow",
        "entry_intraday_ret",
        "entry_break_high20",
        "eod_exit_ret_cost30",
        "best_post_close_ret",
        "worst_post_close_ret",
    ]
    for col in num_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    d["upper_shadow_fail"] = d["entry_upper_shadow"].ge(0.60)
    d["intraday_fail"] = d["entry_intraday_ret"].le(-0.02)
    d["breakout_fail"] = d["entry_break_high20"].le(-0.02)
    d["post30_fail"] = d["fail_after_buy"]
    d["entry_fail"] = d["upper_shadow_fail"] | d["intraday_fail"] | d["breakout_fail"] | d["post30_fail"]
    d["d1_repair"] = d["d1_close_ret"].ge(0.03)
    d["d1_no_repair"] = ~d["d1_repair"]
    d["d1_fail_no_repair"] = d["entry_fail"] & d["d1_no_repair"]
    d["d1_exit_ret_cost30"] = d["d1_close"] / d["buy_price"] - 1.0 - 0.003
    d["d2_exit_ret_cost30"] = d["d2_close"] / d["buy_price"] - 1.0 - 0.003
    return d


def summarize(d: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if d.empty:
        return pd.DataFrame()
    return (
        d.groupby(keys, dropna=False)
        .agg(
            positions=("candidate_key", "nunique"),
            legs=("leg_count", "sum"),
            mean_return=("weighted_return", "mean"),
            median_return=("weighted_return", "median"),
            sum_return=("weighted_return", "sum"),
            win_rate=("weighted_return", lambda s: float((pd.to_numeric(s, errors="coerce") > 0).mean())),
            bad5_rate=("weighted_return", lambda s: float((pd.to_numeric(s, errors="coerce") <= -0.05).mean())),
            d1_repair_rate=("d1_repair", "mean"),
            early_weak_rate=("early_weak_confirm", "mean"),
            entry_fail_rate=("entry_fail", "mean"),
            d1_exit_mean=("d1_exit_ret_cost30", "mean"),
        )
        .reset_index()
    )


def policy_probe(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    trigger = d["d1_fail_no_repair"]
    policies = {
        "base_original": pd.Series(d["weighted_return"].values, index=d.index),
        "entry_fail_d1_no_repair_full_exit": d["weighted_return"].where(~trigger, d["d1_exit_ret_cost30"]),
        "entry_fail_d1_no_repair_half_exit": d["weighted_return"].where(~trigger, 0.5 * d["d1_exit_ret_cost30"] + 0.5 * d["weighted_return"]),
        "entry_fail_d1_no_repair_d2_full_exit": d["weighted_return"].where(~trigger, d["d2_exit_ret_cost30"]),
    }
    for policy, ret in policies.items():
        x = d.copy()
        x["probe_return"] = pd.to_numeric(ret, errors="coerce")
        scopes = {
            "all": pd.Series(True, index=x.index),
            "entry_fail": x["entry_fail"],
            "entry_fail_d1_repair": x["entry_fail"] & x["d1_repair"],
            "entry_fail_d1_no_repair": x["d1_fail_no_repair"],
            "no_entry_fail": ~x["entry_fail"],
        }
        for scope, mask in scopes.items():
            g = x[mask.fillna(False)].copy()
            rows.append(
                {
                    "policy": policy,
                    "scope": scope,
                    "positions": int(g["candidate_key"].nunique()) if not g.empty else 0,
                    "mean_return": float(g["probe_return"].mean()) if not g.empty else 0.0,
                    "median_return": float(g["probe_return"].median()) if not g.empty else 0.0,
                    "sum_return": float(g["probe_return"].sum()) if not g.empty else 0.0,
                    "win_rate": float((g["probe_return"] > 0).mean()) if not g.empty else 0.0,
                    "bad5_rate": float((g["probe_return"] <= -0.05).mean()) if not g.empty else 0.0,
                }
            )
    return pd.DataFrame(rows)


def concentration(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for label, g in {
        "all": d,
        "entry_fail_d1_repair": d[d["entry_fail"] & d["d1_repair"]],
        "entry_fail_d1_no_repair": d[d["d1_fail_no_repair"]],
        "no_entry_fail": d[~d["entry_fail"]],
    }.items():
        s = g.sort_values("weighted_return", ascending=False)
        total = float(s["weighted_return"].sum()) if not s.empty else 0.0
        for n in [0, 1, 2, 3]:
            rest = s.iloc[n:]
            rows.append(
                {
                    "scope": label,
                    "exclude_top_n": n,
                    "positions_left": int(len(rest)),
                    "sum_return_left": float(rest["weighted_return"].sum()) if not rest.empty else 0.0,
                    "share_left": float(rest["weighted_return"].sum() / total) if total else 0.0,
                }
            )
    return pd.DataFrame(rows)


def write_report(
    by_structure: pd.DataFrame,
    by_source: pd.DataFrame,
    policies: pd.DataFrame,
    conc: pd.DataFrame,
    samples: pd.DataFrame,
) -> None:
    pct_cols = {
        "mean_return",
        "median_return",
        "sum_return",
        "win_rate",
        "bad5_rate",
        "d1_repair_rate",
        "early_weak_rate",
        "entry_fail_rate",
        "d1_exit_mean",
        "weighted_return",
        "d1_close_ret",
        "d2_close_ret",
        "d1_exit_ret_cost30",
        "d2_exit_ret_cost30",
        "entry_upper_shadow",
        "entry_intraday_ret",
        "entry_break_high20",
        "best_post_close_ret",
        "worst_post_close_ret",
        "sum_return_left",
        "share_left",
    }
    lines = [
        "# G3 weak_rebound G2 v4 入场失败 + D1 修复审计 v1",
        "",
        "## 回测时间与口径",
        "- 审计对象：G2 v4 实际成交窗口 2024-09-26 至 2026-05-20 中的 weak_rebound 样本，12 笔持仓 / 21 条交易腿。",
        "- 数据来源：上一轮 30m 入场承接审计结果 + 日线 D1/D2 收盘表现。",
        "- 本轮是候选级结构探针，不是最终逐日 MTM；若候选级不成立，不进入路由。",
        "- 固定定义，不做参数网格搜索，避免 12 笔样本过拟合。",
        "",
        "## 英文名解释",
        "- `entry_fail`：入场失败，满足任一条件：入场日上影线过重、日内收弱、未有效突破 20 日高点、或买入后 30m 出现失败形态。",
        "- `d1_repair`：D1 修复，买入后第 1 个交易日收盘相对入场日收盘上涨至少 3%。",
        "- `d1_no_repair`：D1 没有修复。",
        "- `entry_fail_d1_no_repair_full_exit`：入场失败且 D1 不修复时，按 D1 收盘价扣 30bps 全退出。",
        "- `entry_fail_d1_no_repair_half_exit`：入场失败且 D1 不修复时，D1 先退一半，另一半保留原 G2 退出。",
        "- `entry_fail_d1_no_repair_d2_full_exit`：同样触发后延迟到 D2 收盘全退出。",
        "",
        "## 结构分组结果",
        md_table(by_structure, pct_cols=pct_cols),
        "",
        "## 来源 x 结构",
        md_table(by_source, pct_cols=pct_cols),
        "",
        "## 策略探针",
        md_table(policies, pct_cols=pct_cols),
        "",
        "## 集中度",
        md_table(conc, pct_cols=pct_cols),
        "",
        "## 样本明细",
        md_table(samples, pct_cols=pct_cols),
        "",
        "## 阶段判断",
        "- 如果 `entry_fail_d1_no_repair` 仍有较高收益，说明不能把入场失败 + D1 不修复简单作为退出条件。",
        "- 如果全退/半退收益明显低于 `base_original`，这条风控不能进入 G3 路由。",
        "- 若只有少数亏损被识别，而大赢家也被错杀，下一步应转向更早的入场前结构质量，而不是入场后退出。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = load_data()
    by_structure = summarize(d, ["entry_fail", "d1_repair", "early_weak_confirm"])
    by_source = summarize(d, ["source_family", "entry_fail", "d1_repair"])
    policies = policy_probe(d)
    conc = concentration(d)
    sample_cols = [
        "entry_date_ts",
        "code",
        "name",
        "source_family",
        "weighted_return",
        "entry_fail",
        "d1_repair",
        "d1_fail_no_repair",
        "entry_upper_shadow",
        "entry_intraday_ret",
        "entry_break_high20",
        "fail_after_buy",
        "d1_close_ret",
        "d2_close_ret",
        "d1_exit_ret_cost30",
        "d2_exit_ret_cost30",
        "exit_reasons",
    ]
    samples = d[[c for c in sample_cols if c in d.columns]].sort_values("entry_date_ts")

    d.to_csv(OUT_DIR / "entry_fail_d1_repair_audit.csv", index=False, encoding="utf-8-sig")
    by_structure.to_csv(OUT_DIR / "by_structure.csv", index=False, encoding="utf-8-sig")
    by_source.to_csv(OUT_DIR / "by_source_structure.csv", index=False, encoding="utf-8-sig")
    policies.to_csv(OUT_DIR / "policy_probe.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "concentration.csv", index=False, encoding="utf-8-sig")
    samples.to_csv(OUT_DIR / "sample_details.csv", index=False, encoding="utf-8-sig")
    write_report(by_structure, by_source, policies, conc, samples)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
