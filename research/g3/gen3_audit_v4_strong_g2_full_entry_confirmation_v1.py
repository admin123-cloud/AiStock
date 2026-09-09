from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


G3_PATH = _report_path() / "gen3_v4_strong_entry_quality_audit_v1" / "entry_quality_enriched.csv"
G2_PATH = _report_path() / "gen2_v2_complete_strategy_2020" / "sources" / "g2_v2_complete.parquet"
OUT_DIR = _report_path() / "gen3_v4_strong_g2_full_entry_confirmation_v1"


def pct(v: Any) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


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


def load_g3() -> pd.DataFrame:
    d = pd.read_csv(G3_PATH, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d = d[d["route"].astype(str).eq("strong_main")].copy()
    for col in [
        "policy_net_ret",
        "score",
        "strong_day_rank",
        "min_low_ret",
        "max_high_ret",
        "giveback_from_high",
        "d1_close_ret",
        "d2_close_ret",
        "entry_upper_shadow",
        "entry_break_high20",
        "entry_amount_ratio20",
        "runup_from_60d_low",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["entry_year"] = d["entry_date"].dt.year
    return d.dropna(subset=["entry_date", "code", "policy_net_ret"]).copy()


def load_g2_labels() -> pd.DataFrame:
    g = pd.read_parquet(G2_PATH)
    g["entry_date"] = pd.to_datetime(g["entry_date"], errors="coerce").dt.normalize()
    g["code"] = g["code"].astype(str)
    for col in [
        "v4_rank",
        "v4_score",
        "l3_rt_strong3_ratio",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "sector_score_bonus",
        "entry_price",
    ]:
        if col in g.columns:
            g[col] = pd.to_numeric(g[col], errors="coerce")
    keep = [
        "entry_date",
        "code",
        "source_family",
        "signal_family",
        "g2_v2_buy_logic",
        "v4_rank",
        "v4_score",
        "entry_price",
        "confirm_datetime",
        "l3_rt_strong3_ratio",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "sector_strong",
        "sector_score_bonus",
    ]
    g = g[[c for c in keep if c in g.columns]].copy()
    g = g.sort_values(
        ["entry_date", "code", "source_family", "v4_score", "v4_rank"],
        ascending=[True, True, True, False, True],
    ).drop_duplicates(["entry_date", "code"], keep="first")
    g = g.rename(
        columns={
            "source_family": "g2_source_family",
            "signal_family": "g2_signal_family",
            "g2_v2_buy_logic": "g2_buy_logic",
            "v4_rank": "g2_v4_rank",
            "v4_score": "g2_v4_score",
            "entry_price": "g2_entry_price",
            "confirm_datetime": "g2_confirm_datetime",
            "l3_rt_strong3_ratio": "g2_l3_rt_strong3_ratio",
            "rt_return_from_d1_close": "g2_rt_return_from_d1_close",
            "rt_breakout_vs_box_top": "g2_rt_breakout_vs_box_top",
            "sector_strong": "g2_sector_strong",
            "sector_score_bonus": "g2_sector_score_bonus",
        }
    )
    return g


def attach_labels(g3: pd.DataFrame, g2: pd.DataFrame) -> pd.DataFrame:
    d = g3.merge(g2, on=["entry_date", "code"], how="left")
    d["g2_full_confirm"] = d["g2_source_family"].notna()
    d["g2_volume5_confirm"] = d["g2_source_family"].astype(str).eq("volume5")
    d["g2_bigbull_confirm"] = d["g2_source_family"].astype(str).eq("big_bull")
    d["g2_sector_bonus_confirm"] = d["g2_sector_score_bonus"].fillna(0).gt(0)
    d["g2_sector_strong_confirm"] = d["g2_sector_strong"].fillna(False).astype(bool)
    d["g2_intraday_strong_confirm"] = (
        d["g2_rt_return_from_d1_close"].ge(0.06)
        | d["g2_rt_breakout_vs_box_top"].ge(0.025)
        | d["g2_volume5_confirm"]
    )
    d["g2_confirm_family"] = "none"
    d.loc[d["g2_volume5_confirm"], "g2_confirm_family"] = "volume5"
    d.loc[d["g2_bigbull_confirm"], "g2_confirm_family"] = "big_bull"
    d.loc[d["g2_sector_bonus_confirm"] & d["g2_volume5_confirm"], "g2_confirm_family"] = "volume5_sector_bonus"
    return d


def summarize_group(d: pd.DataFrame, key: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for k, g in d.groupby(key, dropna=False):
        rows.append(
            {
                key: str(k),
                "笔数": int(len(g)),
                "均值": float(g["policy_net_ret"].mean()),
                "胜率": float((g["policy_net_ret"] > 0).mean()),
                "中位数": float(g["policy_net_ret"].median()),
                "最差": float(g["policy_net_ret"].min()),
                "平均最大浮盈": float(g["max_high_ret"].mean()),
                "平均最大浮亏": float(g["min_low_ret"].mean()),
                "早期失败数": int(g["failure_tag"].astype(str).str.contains("早期失败", regex=False).sum()),
                "持仓失败数": int(g["failure_tag"].astype(str).str.contains("持仓期失败", regex=False).sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("均值", ascending=False)


def summarize_year(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for year, part in d.groupby("entry_year"):
        for label, g in [
            ("base_all", part),
            ("g2_full_confirm", part[part["g2_full_confirm"]]),
            ("no_g2_confirm", part[~part["g2_full_confirm"]]),
        ]:
            if g.empty:
                continue
            rows.append(
                {
                    "year": int(year),
                    "group": label,
                    "笔数": int(len(g)),
                    "均值": float(g["policy_net_ret"].mean()),
                    "胜率": float((g["policy_net_ret"] > 0).mean()),
                    "最差": float(g["policy_net_ret"].min()),
                }
            )
    return pd.DataFrame(rows)


def top_examples(d: pd.DataFrame, mask_col: str, ascending: bool, n: int = 12) -> pd.DataFrame:
    cols = [
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "max_high_ret",
        "min_low_ret",
        "failure_tag",
        "g2_confirm_family",
        "g2_buy_logic",
        "g2_l3_rt_strong3_ratio",
        "g2_rt_return_from_d1_close",
        "entry_upper_shadow",
        "entry_break_high20",
    ]
    x = d[d[mask_col]].sort_values("policy_net_ret", ascending=ascending).head(n).copy()
    x = x[[c for c in cols if c in x.columns]]
    x["entry_date"] = pd.to_datetime(x["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["policy_net_ret", "max_high_ret", "min_low_ret", "g2_l3_rt_strong3_ratio", "g2_rt_return_from_d1_close", "entry_upper_shadow", "entry_break_high20"]:
        if col in x.columns:
            x[col] = x[col].map(pct)
    return x.rename(
        columns={
            "entry_date": "入场日",
            "code": "代码",
            "name": "名称",
            "policy_net_ret": "收益",
            "max_high_ret": "最高浮盈",
            "min_low_ret": "最大浮亏",
            "failure_tag": "标签",
            "g2_confirm_family": "G2确认",
            "g2_buy_logic": "G2逻辑",
            "g2_l3_rt_strong3_ratio": "板块强3",
            "g2_rt_return_from_d1_close": "盘中强度",
            "entry_upper_shadow": "入场上影",
            "entry_break_high20": "20日突破",
        }
    )


def write_report(d: pd.DataFrame, by_flag: pd.DataFrame, by_family: pd.DataFrame, by_year: pd.DataFrame) -> None:
    pct_cols = {"均值", "胜率", "中位数", "最差", "平均最大浮盈", "平均最大浮亏"}
    full_count = int(d["g2_full_confirm"].sum())
    total = int(len(d))
    full_mean = float(d.loc[d["g2_full_confirm"], "policy_net_ret"].mean()) if full_count else 0.0
    miss_mean = float(d.loc[~d["g2_full_confirm"], "policy_net_ret"].mean()) if full_count < total else 0.0
    lines = [
        "# G3 V4 strong_main 吸收 G2 full 强势入场确认审计 v1",
        "",
        "## 边界",
        "",
        "- 不使用 score/rank 做新增过滤。",
        "- 只把 G2 完整版结构确认按 `entry_date + code` 贴到 G3 strong_main 历史成交上。",
        "- G2 标签来自正式完整思路：主线一 `volume5_keep80_runup + sector_score_bonus`；主线二 `big_bull_rebreak_2_5d + intraday_strength + sector_strong`。",
        "- 本轮是入口质量审计，不改退出、不做 slot 复算、不把结论直接接实盘。",
        "",
        "## 关键结论",
        "",
        f"- G3 strong_main 样本 {total} 笔，其中命中 G2 full 强势确认 {full_count} 笔，占比 {pct(full_count / total if total else 0)}。",
        f"- 命中 G2 full 确认的均值 {pct(full_mean)}；未命中的均值 {pct(miss_mean)}。",
        "- 如果命中组明显优于未命中组，说明 G3 强势链路应优先吸收 G2 的结构确认，而不是继续用买后弱确认补救。",
        "",
        "## 按 G2 确认标记",
        "",
        md_table(by_flag, pct_cols=pct_cols),
        "",
        "## 按 G2 确认家族",
        "",
        md_table(by_family, pct_cols=pct_cols),
        "",
        "## 年度对比",
        "",
        md_table(by_year, pct_cols={"均值", "胜率", "最差"}),
        "",
        "## G2 命中组最差样本",
        "",
        md_table(top_examples(d, "g2_full_confirm", True)),
        "",
        "## G2 未命中组最好样本",
        "",
        md_table(top_examples(d.assign(no_g2_confirm=~d["g2_full_confirm"]), "no_g2_confirm", False)),
        "",
        "## 下一步目标",
        "",
        "- 若 G2 命中组明显更强，下一步做固定组合：G3 strong_main 只保留 G2 full 结构确认，或作为优先级提升，而不是 score/rank 过滤。",
        "- 若 G2 未命中组也有大量大赢家，下一步需要拆出这些非 G2 赢家的独立触发源，避免把 G3 做成 G2 的简单复刻。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g3 = load_g3()
    g2 = load_g2_labels()
    d = attach_labels(g3, g2)
    by_flag = summarize_group(d, "g2_full_confirm")
    by_family = summarize_group(d, "g2_confirm_family")
    by_year = summarize_year(d)
    d.to_csv(OUT_DIR / "g3_strong_with_g2_full_labels.csv", index=False, encoding="utf-8-sig")
    by_flag.to_csv(OUT_DIR / "summary_by_g2_flag.csv", index=False, encoding="utf-8-sig")
    by_family.to_csv(OUT_DIR / "summary_by_g2_family.csv", index=False, encoding="utf-8-sig")
    by_year.to_csv(OUT_DIR / "summary_by_year.csv", index=False, encoding="utf-8-sig")
    write_report(d, by_flag, by_family, by_year)
    print(f"done: {OUT_DIR}")
    print(by_flag.to_string(index=False))
    print(by_family.to_string(index=False))


if __name__ == "__main__":
    main()
