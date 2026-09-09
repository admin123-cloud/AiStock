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


SRC_DIR = _report_path() / "gen3_v4_strong_entry_warning_early_exit_v1"
QUALITY_PATH = _report_path() / "gen3_v4_strong_entry_quality_audit_v1" / "entry_quality_enriched.csv"
OUT_DIR = _report_path() / "gen3_v4_strong_entry_warning_trigger_outcomes_v1"

BASE_PATH = SRC_DIR / "base_093_candidates.csv"
STRICT_HALF_PATH = SRC_DIR / "warn_strict_d1d2_half_proxy_candidates.csv"
STRICT_FAST_PATH = SRC_DIR / "warn_strict_d1d2_fast_exit_candidates.csv"


def pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def md_table(df: pd.DataFrame, cols: list[str] | None = None) -> str:
    if cols is not None:
        d = df[cols].copy()
    else:
        d = df.copy()
    if d.empty:
        return "_无数据_"
    out = ["| " + " | ".join(map(str, d.columns)) + " |"]
    out.append("| " + " | ".join(["---"] * len(d.columns)) + " |")
    for _, row in d.iterrows():
        out.append("| " + " | ".join("" if pd.isna(x) else str(x) for x in row.tolist()) + " |")
    return "\n".join(out)


def load_candidate(path: Path, suffix: str) -> pd.DataFrame:
    d = pd.read_csv(path, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "score",
        "strong_day_rank",
        "entry_price",
        "policy_net_ret",
        "original_policy_net_ret",
        "early_open_ret_30bps",
        "entry_upper_shadow",
        "entry_break_high20",
        "d1_close_ret",
        "d2_close_ret",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    keep = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "route",
        "score",
        "strong_day_rank",
        "entry_price",
        "policy_net_ret",
        "original_policy_net_ret",
        "early_open_ret_30bps",
        "early_exit_note",
        "entry_upper_shadow",
        "entry_break_high20",
        "d1_close_ret",
        "d2_close_ret",
        "failure_tag",
    ]
    d = d[[c for c in keep if c in d.columns]].copy()
    rename = {
        "policy_exit_date": f"policy_exit_date_{suffix}",
        "policy_net_ret": f"policy_net_ret_{suffix}",
        "original_policy_net_ret": f"original_policy_net_ret_{suffix}",
        "early_open_ret_30bps": f"early_open_ret_30bps_{suffix}",
        "early_exit_note": f"early_exit_note_{suffix}",
    }
    return d.rename(columns=rename)


def load_quality() -> pd.DataFrame:
    q = pd.read_csv(QUALITY_PATH, low_memory=False, encoding="utf-8-sig")
    q["entry_date"] = pd.to_datetime(q["entry_date"], errors="coerce").dt.normalize()
    q["policy_exit_date"] = pd.to_datetime(q["policy_exit_date"], errors="coerce").dt.normalize()
    q["code"] = q["code"].astype(str)
    keep = [
        "entry_date",
        "policy_exit_date",
        "code",
        "min_low_ret",
        "min_low_date",
        "min_low_day_index",
        "max_high_ret",
        "max_high_date",
        "max_high_day_index",
        "giveback_from_high",
        "d1_low_ret",
        "d2_low_ret",
        "entry_close_ret",
        "entry_gap_ret",
        "entry_intraday_ret",
        "entry_amount_ratio20",
        "runup_from_60d_low",
        "rank_bucket",
        "score_bucket",
        "upper_shadow_bucket",
        "break20_bucket",
        "d1_bucket",
        "d2_bucket",
    ]
    q = q[[c for c in keep if c in q.columns]].copy()
    for col in [
        "min_low_ret",
        "max_high_ret",
        "giveback_from_high",
        "d1_low_ret",
        "d2_low_ret",
        "entry_close_ret",
        "entry_gap_ret",
        "entry_intraday_ret",
        "entry_amount_ratio20",
        "runup_from_60d_low",
    ]:
        if col in q.columns:
            q[col] = pd.to_numeric(q[col], errors="coerce")
    return q


def classify(row: pd.Series, action_col: str) -> str:
    old = row["base_ret"]
    new = row[action_col]
    delta = new - old
    if pd.isna(old) or pd.isna(new):
        return "未知"
    if old < 0 and delta > 0:
        return "救亏损"
    if old > 0 and delta < 0:
        return "误伤盈利"
    if old < 0 and delta < 0:
        return "加深亏损"
    if old > 0 and delta > 0:
        return "改善盈利"
    return "中性"


def summarize_by(d: pd.DataFrame, key: str, ret_col: str, delta_col: str) -> pd.DataFrame:
    rows = []
    for k, g in d.groupby(key, dropna=False):
        rows.append(
            {
                key: "" if pd.isna(k) else k,
                "笔数": len(g),
                "原始均值": pct(g["base_ret"].mean()),
                "新均值": pct(g[ret_col].mean()),
                "平均变化": pct(g[delta_col].mean()),
                "原始亏损笔数": int((g["base_ret"] < 0).sum()),
                "误伤盈利笔数": int((g["outcome_half"] == "误伤盈利").sum()),
                "救亏损笔数": int((g["outcome_half"] == "救亏损").sum()),
                "加深亏损笔数": int((g["outcome_half"] == "加深亏损").sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    base = load_candidate(BASE_PATH, "base")
    half = load_candidate(STRICT_HALF_PATH, "half")
    fast = load_candidate(STRICT_FAST_PATH, "fast")
    quality = load_quality()

    base_keys = ["entry_date", "code", "name", "route", "score", "strong_day_rank", "entry_price"]
    d = base.merge(
        half[
            [
                "entry_date",
                "code",
                "policy_net_ret_half",
                "early_open_ret_30bps_half",
                "early_exit_note_half",
                "policy_exit_date_half",
            ]
        ],
        on=["entry_date", "code"],
        how="left",
    )
    d = d.merge(
        fast[
            [
                "entry_date",
                "code",
                "policy_net_ret_fast",
                "early_open_ret_30bps_fast",
                "early_exit_note_fast",
                "policy_exit_date_fast",
            ]
        ],
        on=["entry_date", "code"],
        how="left",
    )
    d = d.merge(quality, left_on=["entry_date", "code", "policy_exit_date_base"], right_on=["entry_date", "code", "policy_exit_date"], how="left")

    d["base_ret"] = pd.to_numeric(d["policy_net_ret_base"], errors="coerce")
    d["half_ret"] = pd.to_numeric(d["policy_net_ret_half"], errors="coerce")
    d["fast_ret"] = pd.to_numeric(d["policy_net_ret_fast"], errors="coerce")
    d["half_delta"] = d["half_ret"] - d["base_ret"]
    d["fast_delta"] = d["fast_ret"] - d["base_ret"]
    d["triggered"] = d["early_exit_note_half"].astype(str).ne("base_policy") & d["early_exit_note_half"].notna()
    d["entry_year"] = d["entry_date"].dt.year
    d["outcome_half"] = d.apply(lambda r: classify(r, "half_ret"), axis=1)
    d["outcome_fast"] = d.apply(lambda r: classify(r, "fast_ret"), axis=1)

    triggered = d[d["triggered"] & d["route"].astype(str).eq("strong_main")].copy()
    triggered = triggered.sort_values(["entry_date", "code"]).reset_index(drop=True)

    export = triggered[
        [
            "entry_date",
            "code",
            "name",
            "score",
            "strong_day_rank",
            "base_ret",
            "half_ret",
            "half_delta",
            "fast_ret",
            "fast_delta",
            "outcome_half",
            "outcome_fast",
            "early_exit_note_half",
            "entry_upper_shadow",
            "entry_break_high20",
            "d1_close_ret",
            "d2_close_ret",
            "min_low_ret",
            "max_high_ret",
            "giveback_from_high",
            "failure_tag",
            "upper_shadow_bucket",
            "break20_bucket",
            "d1_bucket",
            "d2_bucket",
        ]
    ].copy()
    export.to_csv(OUT_DIR / "triggered_trades.csv", index=False, encoding="utf-8-sig")

    by_outcome = summarize_by(triggered, "outcome_half", "half_ret", "half_delta")
    by_note = summarize_by(triggered, "early_exit_note_half", "half_ret", "half_delta")
    by_year = summarize_by(triggered, "entry_year", "half_ret", "half_delta")
    by_shadow = summarize_by(triggered, "upper_shadow_bucket", "half_ret", "half_delta")
    by_break = summarize_by(triggered, "break20_bucket", "half_ret", "half_delta")

    by_outcome.to_csv(OUT_DIR / "summary_by_outcome.csv", index=False, encoding="utf-8-sig")
    by_note.to_csv(OUT_DIR / "summary_by_note.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "summary_by_year.csv", index=False, encoding="utf-8-sig")

    harmed = export[export["outcome_half"].eq("误伤盈利")].sort_values("half_delta")
    deepened = export[export["outcome_half"].eq("加深亏损")].sort_values("half_delta")
    saved = export[export["outcome_half"].eq("救亏损")].sort_values("half_delta", ascending=False)

    top_cols = [
        "entry_date",
        "code",
        "name",
        "base_ret_fmt",
        "half_ret_fmt",
        "half_delta_fmt",
        "early_exit_note_half",
        "entry_upper_shadow_fmt",
        "entry_break_high20_fmt",
        "d1_close_ret_fmt",
        "d2_close_ret_fmt",
        "max_high_ret_fmt",
        "failure_tag",
    ]
    view = export.copy()
    for col in ["base_ret", "half_ret", "half_delta", "entry_upper_shadow", "entry_break_high20", "d1_close_ret", "d2_close_ret", "max_high_ret"]:
        view[f"{col}_fmt"] = view[col].map(pct)
    view["entry_date"] = pd.to_datetime(view["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")

    def top_table(frame: pd.DataFrame, n: int = 12) -> pd.DataFrame:
        idx = frame.index
        return view.loc[idx, top_cols].head(n).rename(
            columns={
                "entry_date": "入场日",
                "code": "代码",
                "name": "名称",
                "base_ret_fmt": "原始收益",
                "half_ret_fmt": "半仓代理收益",
                "half_delta_fmt": "变化",
                "early_exit_note_half": "触发",
                "entry_upper_shadow_fmt": "入场上影",
                "entry_break_high20_fmt": "20日突破",
                "d1_close_ret_fmt": "D1",
                "d2_close_ret_fmt": "D2",
                "max_high_ret_fmt": "持仓最高",
                "failure_tag": "失败标签",
            }
        )

    report = []
    report.append("# G3 V4 strong_main 入场警戒 + D1/D2 弱确认触发明细审计 v1")
    report.append("")
    report.append("## 结论")
    report.append("")
    total = len(triggered)
    report.append(f"- 严格触发样本共 {total} 笔，仍然不使用 score/rank 做新增过滤。")
    report.append(
        f"- 半仓代理口径：原始触发均值 {pct(triggered['base_ret'].mean())}，处理后均值 {pct(triggered['half_ret'].mean())}，平均变化 {pct(triggered['half_delta'].mean())}。"
    )
    report.append(
        f"- 快速退出口径：处理后均值 {pct(triggered['fast_ret'].mean())}，平均变化 {pct(triggered['fast_delta'].mean())}。"
    )
    report.append(
        f"- 半仓代理中，救亏损 {int((triggered['outcome_half'] == '救亏损').sum())} 笔，误伤盈利 {int((triggered['outcome_half'] == '误伤盈利').sum())} 笔，加深亏损 {int((triggered['outcome_half'] == '加深亏损').sum())} 笔。"
    )
    report.append("- 当前证据说明：这个结构警戒能识别一批问题入场，但 D1/D2 收盘后再处理偏晚，且会切掉部分原本反弹票；它暂时不能升级为正式交易规则。")
    report.append("")
    report.append("## 按结果分类")
    report.append("")
    report.append(md_table(by_outcome))
    report.append("")
    report.append("## 按触发时点")
    report.append("")
    report.append(md_table(by_note))
    report.append("")
    report.append("## 按年份")
    report.append("")
    report.append(md_table(by_year))
    report.append("")
    report.append("## 按上影分组")
    report.append("")
    report.append(md_table(by_shadow))
    report.append("")
    report.append("## 按 20 日突破失败分组")
    report.append("")
    report.append(md_table(by_break))
    report.append("")
    report.append("## 被误伤的盈利票")
    report.append("")
    report.append(md_table(top_table(harmed)))
    report.append("")
    report.append("## 被加深的亏损票")
    report.append("")
    report.append(md_table(top_table(deepened)))
    report.append("")
    report.append("## 真正救到的亏损票")
    report.append("")
    report.append(md_table(top_table(saved)))
    report.append("")
    report.append("## 下一步目标")
    report.append("")
    report.append("- 不继续调收益阈值。下一步应把触发样本按“D1/D2 后是否还有盘中反抽可卖”拆开，验证是不是需要 30m 级别的早期弱确认，而不是等日线收盘。")
    report.append("- 同时保留本轮结论：严格警戒更像仓位/执行风险提醒，不是独立选股过滤器；不能拿它替代强势主线入场逻辑。")

    (OUT_DIR / "report_cn.md").write_text("\n".join(report), encoding="utf-8")
    print(f"done: {OUT_DIR}")
    print(f"triggered={len(triggered)}")
    print(by_outcome.to_string(index=False))


if __name__ == "__main__":
    main()
