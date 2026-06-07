from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE_2020 = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "sources" / "g2_v2_complete.parquet"
OFFICIAL_SUMMARY = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "summary.json"
ATTR_REPORT = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "attribution" / "attribution_report.md"
OUT_DIR = ROOT / "reports" / "gen3_strong_chain_migration_v1"


def pct(x: float | int | None) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x) * 100:.2f}%"


def num(x: float | int | None, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x):.{digits}f}"


def classify_market_style(row: pd.Series) -> str:
    close = row.get("index_close")
    ma20 = row.get("index_ma20")
    ma60 = row.get("index_ma60")
    mom20 = row.get("index_mom20")
    breadth = row.get("market_breadth")

    if pd.notna(close) and pd.notna(ma20) and pd.notna(ma60) and close >= ma20 >= ma60:
        return "main_up"
    if (
        pd.notna(close)
        and pd.notna(ma60)
        and pd.notna(ma20)
        and pd.notna(mom20)
        and pd.notna(breadth)
        and ma20 < ma60
        and close >= ma60
        and mom20 > 0
        and breadth >= 0.5
    ):
        return "weak_recovery"
    if pd.notna(close) and pd.notna(ma20) and close < ma20:
        return "defense_or_failed"
    return "neutral"


def summarize_group(df: pd.DataFrame, keys: list[str], ret_col: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=keys + ["rows", "valid_ret_rows", "unique_days", "mean_ret", "median_ret", "win_rate", "worst_ret", "p10_ret", "best_ret"])
    grouped = df.groupby(keys, dropna=False)

    def win_rate(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float((s > 0).mean())

    def p10(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float(s.quantile(0.10))

    out = grouped.agg(
        rows=("code", "size"),
        valid_ret_rows=(ret_col, "count"),
        unique_days=("entry_date", "nunique"),
        mean_ret=(ret_col, "mean"),
        median_ret=(ret_col, "median"),
        win_rate=(ret_col, win_rate),
        worst_ret=(ret_col, "min"),
        p10_ret=(ret_col, p10),
        best_ret=(ret_col, "max"),
    ).reset_index()
    return out.sort_values(keys + ["rows"], ascending=[True] * len(keys) + [False])


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, num_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    num_cols = num_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            val = row[col]
            if col in pct_cols:
                item[col] = pct(val)
            elif col in num_cols:
                item[col] = num(val)
            elif isinstance(val, float):
                item[col] = num(val, 4)
            else:
                item[col] = "" if pd.isna(val) else str(val)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_official_notes() -> dict:
    notes: dict[str, object] = {}
    if OFFICIAL_SUMMARY.exists():
        notes["summary"] = json.loads(OFFICIAL_SUMMARY.read_text(encoding="utf-8"))
    if ATTR_REPORT.exists():
        text = ATTR_REPORT.read_text(encoding="utf-8", errors="ignore")
        notes["big_bull_excluded"] = "big_bull=0" in text and "没有进入 official full" in text
    return notes


def official_window(notes: dict, name: str) -> dict:
    summary = notes.get("summary")
    if not isinstance(summary, dict):
        return {}
    rows = summary.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("window") == name:
                return row
    windows = summary.get("windows")
    if isinstance(windows, dict) and isinstance(windows.get(name), dict):
        return windows[name]
    return {}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not SOURCE_2020.exists():
        raise FileNotFoundError(SOURCE_2020)

    df = pd.read_parquet(SOURCE_2020).copy()
    df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    df["g3_strong_chain"] = np.select(
        [
            df["source_family"].eq("volume5"),
            df["source_family"].eq("big_bull"),
        ],
        [
            "g3_strong_volume5_sector_bonus",
            "g3_strong_big_bull_rebreak",
        ],
        default="other",
    )
    df["g3_market_style"] = df.apply(classify_market_style, axis=1)

    ret_col = "eval_fwd_ret_5d"
    if "outcome_fwd_ret_5d" in df.columns and "fwd_ret_5d" in df.columns:
        df[ret_col] = df["outcome_fwd_ret_5d"].where(df["outcome_fwd_ret_5d"].notna(), df["fwd_ret_5d"])
    elif "outcome_fwd_ret_5d" in df.columns:
        df[ret_col] = df["outcome_fwd_ret_5d"]
    else:
        df[ret_col] = df["fwd_ret_5d"]
    ret_cols = [c for c in ["fwd_ret_3d", "fwd_ret_5d", "fwd_ret_10d", "outcome_fwd_ret_3d", "outcome_fwd_ret_5d", "outcome_fwd_ret_10d"] if c in df.columns]

    source_summary = summarize_group(df, ["source_family"], ret_col)
    chain_year = summarize_group(df, ["source_family", "year"], ret_col)
    chain_style = summarize_group(df, ["source_family", "g3_market_style"], ret_col)

    ret_matrix_rows = []
    for family, part in df.groupby("source_family", dropna=False):
        row = {"source_family": family, "rows": len(part)}
        for c in ret_cols:
            row[f"{c}_mean"] = part[c].mean()
            row[f"{c}_win"] = float((part[c] > 0).mean())
            row[f"{c}_worst"] = part[c].min()
        ret_matrix_rows.append(row)
    ret_matrix = pd.DataFrame(ret_matrix_rows).sort_values("source_family")

    source_summary.to_csv(OUT_DIR / "source_summary.csv", index=False, encoding="utf-8-sig")
    chain_year.to_csv(OUT_DIR / "chain_year_summary.csv", index=False, encoding="utf-8-sig")
    chain_style.to_csv(OUT_DIR / "chain_style_summary.csv", index=False, encoding="utf-8-sig")
    ret_matrix.to_csv(OUT_DIR / "forward_return_matrix.csv", index=False, encoding="utf-8-sig")

    notes = load_official_notes()
    official_full = official_window(notes, "full")

    big = df[df["source_family"].eq("big_bull")]
    vol = df[df["source_family"].eq("volume5")]
    big_has_ret = (not big.empty) and big[ret_col].notna().any()

    conclusion = []
    conclusion.append("`volume5 + sector_score_bonus` 可以作为 G3 强势链路的候选基础，但必须重新接入 G3 独立候选源和影子验证。")
    if notes.get("big_bull_excluded"):
        conclusion.append("`big_bull` 在 G2 长周期 official 撮合里并未真实进入正式成交集；不能把 G2 完整策略收益当作突破主线已验证。")
    if not big_has_ret:
        conclusion.append("`big_bull` 在当前扩周期源里也缺少同口径前向收益列，当前结论是“尚未验证”，不是“已验证失败”。")
    conclusion.append("本审计只读取既有 G2 源文件，没有触发 G2 重建，也没有写入 G2 runtime。")

    report = [
        "# G3 强势链路迁移审计 V1",
        "",
        "## 数据口径",
        "",
        f"- 源文件：`{SOURCE_2020.relative_to(ROOT)}`",
        f"- 样本日期：{df['entry_date'].min()} ~ {df['entry_date'].max()}",
        f"- 样本数：{len(df)}，交易日数：{df['entry_date'].nunique()}",
        f"- 评价收益列：`{ret_col}`；同时导出多周期前向收益矩阵。",
        "- 用途：只判断 G3 强势市场链路迁移方向，不作为 G2 运行链路变更。",
        "",
        "## 结论",
        "",
        *[f"- {x}" for x in conclusion],
        "",
        "## 官方 G2 长周期结果提示",
        "",
    ]
    if official_full:
        report += [
            f"- official full 总收益：{pct(official_full.get('total_return'))}",
            f"- official full 最大回撤：{pct(official_full.get('max_drawdown') or official_full.get('max_dd'))}",
            f"- official full 成交数：{official_full.get('trade_count', official_full.get('trades', ''))}",
        ]
    else:
        report += ["- 未读取到 official summary。"]
    report += [
        "- 注意：已有归因报告显示 official full 实际成交只覆盖 `volume5`，且扩周期源里 `big_bull` 缺少同口径前向收益；突破主线需要单独补齐评价、撮合与风控审计。",
        "",
        "## 按链路汇总",
        "",
        md_table(source_summary, pct_cols={"mean_ret", "median_ret", "win_rate", "worst_ret", "p10_ret", "best_ret"}),
        "",
        "## 按市场风格汇总",
        "",
        md_table(chain_style, pct_cols={"mean_ret", "median_ret", "win_rate", "worst_ret", "p10_ret", "best_ret"}),
        "",
        "## 按年份汇总",
        "",
        md_table(chain_year, pct_cols={"mean_ret", "median_ret", "win_rate", "worst_ret", "p10_ret", "best_ret"}),
        "",
        "## 下一步",
        "",
        "- 把 `volume5 + sector_score_bonus` 拆成 G3 独立强势候选源，输出 shadow 文件，不写入 G2 runtime。",
        "- `big_bull` 暂不进正式候选组合，只做影子观察；后续单独重建二次突破的撮合入口、退出和风控。",
        "- 随后回到横盘箱体底部链路，重新定义箱体底部与冰点，不沿用弱势 panic 的规则。",
        "",
    ]
    (OUT_DIR / "strong_chain_migration_report_cn.md").write_text("\n".join(report), encoding="utf-8")

    print(json.dumps({
        "out_dir": str(OUT_DIR),
        "rows": int(len(df)),
        "date_min": str(df["entry_date"].min()),
        "date_max": str(df["entry_date"].max()),
        "source_counts": df["source_family"].value_counts(dropna=False).to_dict(),
        "ret_col": ret_col,
        "big_bull_excluded_from_official": bool(notes.get("big_bull_excluded")),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
