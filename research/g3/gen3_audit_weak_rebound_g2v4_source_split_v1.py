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
IN_FILE = _report_path() / "gen3_weak_rebound_g2v4_quality_v1" / "g2_v4_candidates_entry_quality.csv"
LEG_SUMMARY = _report_path() / "gen3_weak_rebound_g2v4_d1d2_legcash_v1" / "summary.csv"
OUT_DIR = _report_path() / "gen3_weak_rebound_g2v4_source_split_v1"

WINDOWS = {
    "full_g2_actual_2024_09_26_2026_05_20": ("2024-09-26", "2026-05-20"),
    "train_2024_09_26_2024_12_31": ("2024-09-26", "2024-12-31"),
    "valid_2025": ("2025-01-01", "2025-12-31"),
    "blind_2026_ytd_2026_01_01_2026_05_20": ("2026-01-01", "2026-05-20"),
}

NAME_CN = {
    "weak_rebound": "弱势反弹市场：指数不是标准主升，但有反弹修复结构",
    "standard_uptrend": "标准主升市场：趋势和量价更顺的强势环境",
    "volume5": "G2 v4 量能反包/放量修复主线",
    "big_bull": "G2 v4 大阳线后二次突破买点",
    "entry_warning": "入场日质量警戒：冲高回落、突破失败或收盘弱于开盘",
    "early_weak_confirm": "D1/D2 早期弱确认：买入后第1/2个交易日收盘转弱",
}


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
    for col in [
        "weighted_return",
        "leg_count",
        "entry_warning",
        "early_weak_confirm",
        "d1_weak_confirm",
        "d2_weak_confirm",
        "bad5_trade",
        "loss_trade",
        "entry_upper_shadow",
        "entry_intraday_ret",
        "entry_break_high20",
        "runup_from_60d_low",
        "d1_close_ret",
        "d2_close_ret",
        "v4_rank",
        "v4_score",
    ]:
        if col in d.columns and d[col].dtype == object:
            if col in {"entry_warning", "early_weak_confirm", "d1_weak_confirm", "d2_weak_confirm", "bad5_trade", "loss_trade"}:
                d[col] = d[col].map({"True": True, "False": False, True: True, False: False}).fillna(False)
            else:
                d[col] = pd.to_numeric(d[col], errors="coerce")
    d["source_family"] = d["source_family"].fillna("unknown")
    d["market_style"] = d["market_style"].fillna("unknown_context")
    d["entry_year"] = d["entry_date_ts"].dt.year.astype("Int64")
    return d.dropna(subset=["entry_date_ts", "candidate_key"]).copy()


def summarize(g: pd.DataFrame, label: dict[str, Any]) -> dict[str, Any]:
    if g.empty:
        row = {
            "positions": 0,
            "legs": 0,
            "mean_return": 0.0,
            "median_return": 0.0,
            "sum_return": 0.0,
            "win_rate": 0.0,
            "bad5_rate": 0.0,
            "loss_rate": 0.0,
            "entry_warning_rate": 0.0,
            "early_weak_rate": 0.0,
            "top1_share": 0.0,
            "top3_share": 0.0,
            "remove_top3_sum": 0.0,
        }
        row.update(label)
        return row
    ret = pd.to_numeric(g["weighted_return"], errors="coerce").fillna(0.0)
    total = float(ret.sum())
    top = ret.sort_values(ascending=False)
    row = {
        "positions": int(g["candidate_key"].nunique()),
        "legs": int(pd.to_numeric(g["leg_count"], errors="coerce").fillna(0).sum()),
        "first_entry": g["entry_date_ts"].min().strftime("%Y-%m-%d"),
        "last_entry": g["entry_date_ts"].max().strftime("%Y-%m-%d"),
        "mean_return": float(ret.mean()),
        "median_return": float(ret.median()),
        "sum_return": total,
        "win_rate": float((ret > 0).mean()),
        "bad5_rate": float((ret <= -0.05).mean()),
        "loss_rate": float((ret < 0).mean()),
        "entry_warning_rate": float(g["entry_warning"].mean()),
        "early_weak_rate": float(g["early_weak_confirm"].mean()),
        "top1_share": float(top.iloc[:1].sum() / total) if total > 0 else 0.0,
        "top3_share": float(top.iloc[:3].sum() / total) if total > 0 else 0.0,
        "remove_top3_sum": float(top.iloc[3:].sum()),
    }
    row.update(label)
    return row


def grouped_summary(d: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for key, g in d.groupby(keys, dropna=False, sort=False):
        if not isinstance(key, tuple):
            key = (key,)
        rows.append(summarize(g, dict(zip(keys, key))))
    cols = keys + [c for c in rows[0].keys() if c not in keys] if rows else keys
    return pd.DataFrame(rows)[cols] if rows else pd.DataFrame(columns=cols)


def window_source_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for window, (start, end) in WINDOWS.items():
        w = d[d["entry_date_ts"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        for scope, g in [
            ("all_g2_v4", w),
            ("weak_rebound_only", w[w["market_style"].eq("weak_rebound")]),
            ("standard_uptrend_only", w[w["market_style"].eq("standard_uptrend")]),
        ]:
            for source, sg in g.groupby("source_family", dropna=False):
                rows.append(summarize(sg, {"window": window, "scope": scope, "source_family": source}))
            if g.empty:
                rows.append(summarize(g, {"window": window, "scope": scope, "source_family": "none"}))
    return pd.DataFrame(rows)


def weak_rebound_source_matrix(d: pd.DataFrame) -> pd.DataFrame:
    w = d[d["market_style"].eq("weak_rebound")].copy()
    rows = []
    for source, sg in w.groupby("source_family", dropna=False):
        rows.append(summarize(sg, {"bucket": "source_total", "source_family": source}))
        rows.append(summarize(sg[~sg["entry_warning"]], {"bucket": "no_entry_warning", "source_family": source}))
        rows.append(summarize(sg[sg["entry_warning"]], {"bucket": "entry_warning", "source_family": source}))
        rows.append(summarize(sg[~sg["early_weak_confirm"]], {"bucket": "d1d2_ok", "source_family": source}))
        rows.append(summarize(sg[sg["early_weak_confirm"]], {"bucket": "d1d2_weak", "source_family": source}))
    return pd.DataFrame(rows)


def source_policy_probe(d: pd.DataFrame) -> pd.DataFrame:
    w = d[d["market_style"].eq("weak_rebound")].copy()
    rows = []
    for source, sg in w.groupby("source_family", dropna=False):
        for policy in ["base_original", "weak_d1d2_full_exit_daily", "weak_d1d2_half_exit_daily"]:
            x = sg.copy()
            ret = pd.to_numeric(x["weighted_return"], errors="coerce").copy()
            d1 = x["d1_weak_confirm"].fillna(False)
            d2 = x["d2_weak_confirm"].fillna(False)
            if policy == "weak_d1d2_full_exit_daily":
                ret.loc[d1] = pd.to_numeric(x.loc[d1, "d1_close_ret"], errors="coerce")
                ret.loc[~d1 & d2] = pd.to_numeric(x.loc[~d1 & d2, "d2_close_ret"], errors="coerce")
            elif policy == "weak_d1d2_half_exit_daily":
                early = pd.Series(index=x.index, dtype=float)
                early.loc[d1] = pd.to_numeric(x.loc[d1, "d1_close_ret"], errors="coerce")
                early.loc[~d1 & d2] = pd.to_numeric(x.loc[~d1 & d2, "d2_close_ret"], errors="coerce")
                trigger = d1 | d2
                ret.loc[trigger] = 0.5 * early.loc[trigger] + 0.5 * ret.loc[trigger]
            x["weighted_return"] = ret
            rows.append(summarize(x, {"source_family": source, "policy": policy}))
    return pd.DataFrame(rows)


def concentration_table(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    w = d[d["market_style"].eq("weak_rebound")].copy()
    for source, sg in w.groupby("source_family", dropna=False):
        g = sg.sort_values("weighted_return", ascending=False).copy()
        total = float(g["weighted_return"].sum())
        for n in [0, 1, 2, 3, 5]:
            rest = g.iloc[n:]
            rows.append(
                {
                    "source_family": source,
                    "exclude_top_n": n,
                    "positions_left": int(len(rest)),
                    "sum_return_left": float(rest["weighted_return"].sum()),
                    "share_left": float(rest["weighted_return"].sum() / total) if total else 0.0,
                }
            )
    return pd.DataFrame(rows)


def sample_tables(d: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    w = d[d["market_style"].eq("weak_rebound")].copy()
    cols = [
        "entry_date_ts",
        "code",
        "name",
        "source_family",
        "weighted_return",
        "entry_warning",
        "early_weak_confirm",
        "entry_upper_shadow",
        "entry_break_high20",
        "d1_close_ret",
        "d2_close_ret",
        "exit_reasons",
    ]
    cols = [c for c in cols if c in w.columns]
    top = w.sort_values("weighted_return", ascending=False).head(15)[cols]
    worst = w.sort_values("weighted_return", ascending=True).head(15)[cols]
    return top, worst


def write_report(
    d: pd.DataFrame,
    by_state_source: pd.DataFrame,
    window_source: pd.DataFrame,
    matrix: pd.DataFrame,
    policy: pd.DataFrame,
    conc: pd.DataFrame,
    top: pd.DataFrame,
    worst: pd.DataFrame,
) -> None:
    pct_cols = {
        "mean_return",
        "median_return",
        "sum_return",
        "win_rate",
        "bad5_rate",
        "loss_rate",
        "entry_warning_rate",
        "early_weak_rate",
        "top1_share",
        "top3_share",
        "remove_top3_sum",
        "sum_return_left",
        "share_left",
        "weighted_return",
        "entry_upper_shadow",
        "entry_break_high20",
        "d1_close_ret",
        "d2_close_ret",
    }

    weak = d[d["market_style"].eq("weak_rebound")]
    weak_source = grouped_summary(weak, ["source_family"])

    leg_text = ""
    if LEG_SUMMARY.exists():
        s = pd.read_csv(LEG_SUMMARY)
        keep = s[
            s["book"].isin(["balanced_router", "range_unknown_router"])
            & s["policy"].eq("base_original")
            & s["profile"].isin(["base_close30", "shock2"])
        ].copy()
        leg_text = md_table(
            keep[["book", "policy", "profile", "positions", "legs", "total_return", "max_drawdown", "win_rate", "bad5_rate"]],
            pct_cols={"total_return", "max_drawdown", "win_rate", "bad5_rate"},
        )

    lines = [
        "# G3 weak_rebound 吸收 G2 v4 来源拆分审计 v1",
        "",
        "## 回测时间与口径",
        "- G2 v4 实际候选来源窗口：2024-09-26 至 2026-05-20，共 62 笔持仓、101 条交易腿。",
        "- 本报告主审计对象：四状态中标记为 `weak_rebound` 的 G2 v4 候选，共 "
        f"{weak['candidate_key'].nunique()} 笔持仓。",
        "- 总曲线参考窗口：2020-01-01 至 2026-06-04；但 G2 v4 实际有成交的吸收窗口是 2024-09-26 至 2026-05-20，不能把收益理解为完整 6 年独立信号。",
        "- 本报告收益是候选级加权收益审计，不是新策略最终逐日 MTM；逐日 MTM 参考上一轮 leg-cash 报告。",
        "- 压力口径：候选级使用原始 G2 分腿收益；表末引用上一轮 `base_close30` 和 `shock2` 现金流压力结果。",
        "",
        "## 英文名解释",
        f"- `weak_rebound`：{NAME_CN['weak_rebound']}。",
        f"- `standard_uptrend`：{NAME_CN['standard_uptrend']}。",
        f"- `volume5`：{NAME_CN['volume5']}。",
        f"- `big_bull`：{NAME_CN['big_bull']}。",
        f"- `entry_warning`：{NAME_CN['entry_warning']}。",
        f"- `early_weak_confirm`：{NAME_CN['early_weak_confirm']}。",
        "- `base_original`：保留 G2 v4 原始分批卖出。",
        "- `weak_d1d2_full_exit_daily`：D1/D2 走弱时按日线收盘估算全退出。",
        "- `weak_d1d2_half_exit_daily`：D1/D2 走弱时按日线收盘估算先退一半，另一半保留原 G2 出口。",
        "",
        "## 一、市场状态 x 来源",
        md_table(by_state_source, pct_cols=pct_cols),
        "",
        "## 二、weak_rebound 来源总览",
        md_table(weak_source, pct_cols=pct_cols),
        "",
        "## 三、分窗口 x 来源",
        md_table(window_source, pct_cols=pct_cols),
        "",
        "## 四、weak_rebound 来源质量矩阵",
        md_table(matrix, pct_cols=pct_cols),
        "",
        "## 五、D1/D2 退出按来源探针",
        md_table(policy, pct_cols=pct_cols),
        "",
        "## 六、集中度",
        md_table(conc, pct_cols=pct_cols),
        "",
        "## 七、weak_rebound 最好样本",
        md_table(top, pct_cols=pct_cols),
        "",
        "## 八、weak_rebound 最差样本",
        md_table(worst, pct_cols=pct_cols),
        "",
        "## 九、上一轮逐日现金流压力参考",
        leg_text or "_未找到上一轮 leg-cash summary.csv_",
        "",
        "## 阶段结论",
        "- weak_rebound 不是应该被简单禁用的环境；它里面的 G2 v4 收益需要按来源拆分处理。",
        "- 如果收益主要来自 `big_bull`，说明弱反弹里真正有效的是二次突破强票；G3 应吸收的是突破确认逻辑，而不是所有弱反弹追涨。",
        "- 如果收益主要来自 `volume5`，说明弱反弹里有效的是放量修复/反包承接；后续应研究 30m 真实放量承接源，而不是继续调止盈止损。",
        "- 如果某一来源 remove_top3 后收益明显塌陷，说明样本高度集中，不能直接作为正式规则，只能作为候选研究方向。",
        "- 下一步应基于本报告结论，选择保留的来源做逐日 MTM 路由复算：`weak_rebound_big_bull_only`、`weak_rebound_volume5_only`、以及二者分别叠加 D1/D2 风险标签的版本。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = load_data()
    by_state_source = grouped_summary(d, ["market_style", "source_family"]).sort_values(
        ["market_style", "positions"], ascending=[True, False]
    )
    window_source = window_source_summary(d)
    matrix = weak_rebound_source_matrix(d)
    policy = source_policy_probe(d)
    conc = concentration_table(d)
    top, worst = sample_tables(d)

    d.to_csv(OUT_DIR / "input_candidates.csv", index=False, encoding="utf-8-sig")
    by_state_source.to_csv(OUT_DIR / "by_state_source.csv", index=False, encoding="utf-8-sig")
    window_source.to_csv(OUT_DIR / "window_source_summary.csv", index=False, encoding="utf-8-sig")
    matrix.to_csv(OUT_DIR / "weak_rebound_source_matrix.csv", index=False, encoding="utf-8-sig")
    policy.to_csv(OUT_DIR / "weak_rebound_source_policy_probe.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "weak_rebound_source_concentration.csv", index=False, encoding="utf-8-sig")
    top.to_csv(OUT_DIR / "weak_rebound_top_samples.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "weak_rebound_worst_samples.csv", index=False, encoding="utf-8-sig")
    write_report(d, by_state_source, window_source, matrix, policy, conc, top, worst)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
