from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CONFIRMED = ROOT / "reports" / "gen3_range_30m_confirm_v1" / "confirmed_30m_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_event_curve_v1"


def pct(x: float | None) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x) * 100:.2f}%"


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def build_event_curve(df: pd.DataFrame, top_n: int, ret_col: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = df.dropna(subset=[ret_col]).copy()
    d = d.sort_values(["entry_date", "chain_rank", "candidate_score"], ascending=[True, True, False])
    selected = d.groupby("entry_date", sort=True).head(top_n).copy()
    daily = (
        selected.groupby("entry_date", sort=True)
        .agg(rows=("code", "size"), event_ret=(ret_col, "mean"), win_rate=(ret_col, lambda s: float((s > 0).mean())))
        .reset_index()
    )
    daily["equity"] = (1.0 + daily["event_ret"]).cumprod()
    daily["peak"] = daily["equity"].cummax()
    daily["drawdown"] = daily["equity"] / daily["peak"] - 1.0
    return selected, daily


def summarize_curve(label: str, selected: pd.DataFrame, daily: pd.DataFrame, ret_col: str) -> dict:
    if daily.empty:
        return {"variant": label, "signals": 0, "event_days": 0}
    return {
        "variant": label,
        "signals": int(len(selected)),
        "event_days": int(len(daily)),
        "total_return": float(daily["equity"].iloc[-1] - 1.0),
        "max_drawdown": max_drawdown(daily["equity"]),
        "mean_event_ret": float(daily["event_ret"].mean()),
        "event_win_rate": float((daily["event_ret"] > 0).mean()),
        "worst_event": float(daily["event_ret"].min()),
        "mean_signal_ret": float(selected[ret_col].mean()),
        "signal_win_rate": float((selected[ret_col] > 0).mean()),
        "worst_signal": float(selected[ret_col].min()),
    }


def year_summary(selected: pd.DataFrame, daily: pd.DataFrame, label: str, ret_col: str) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    d = daily.copy()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows = []
    for year, part in d.groupby("year"):
        rows.append(
            {
                "variant": label,
                "year": int(year),
                "event_days": int(len(part)),
                "events_return": float((1.0 + part["event_ret"]).prod() - 1.0),
                "max_drawdown": max_drawdown((1.0 + part["event_ret"]).cumprod()),
                "mean_event_ret": float(part["event_ret"].mean()),
                "event_win_rate": float((part["event_ret"] > 0).mean()),
                "worst_event": float(part["event_ret"].min()),
            }
        )
    return pd.DataFrame(rows)


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for c in df.columns:
            v = row[c]
            if c in pct_cols:
                item[c] = pct(v)
            elif isinstance(v, float):
                item[c] = f"{v:.4f}"
            else:
                item[c] = "" if pd.isna(v) else str(v)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(CONFIRMED)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    ret_col = "fwd_ret_confirm_to_close_5d"
    df = df.dropna(subset=[ret_col]).copy()
    df["year"] = df["entry_date"].dt.year

    summaries = []
    years = []
    for top_n in [3, 5, 10, 20]:
        label = f"top{top_n}_per_day"
        selected, daily = build_event_curve(df, top_n, ret_col)
        selected.to_csv(OUT_DIR / f"{label}_selected.csv", index=False, encoding="utf-8-sig")
        daily.to_csv(OUT_DIR / f"{label}_event_curve.csv", index=False, encoding="utf-8-sig")
        summaries.append(summarize_curve(label, selected, daily, ret_col))
        years.append(year_summary(selected, daily, label, ret_col))

    summary = pd.DataFrame(summaries)
    year = pd.concat(years, ignore_index=True) if years else pd.DataFrame()
    summary.to_csv(OUT_DIR / "event_curve_summary.csv", index=False, encoding="utf-8-sig")
    year.to_csv(OUT_DIR / "event_curve_year_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "total_return",
        "max_drawdown",
        "mean_event_ret",
        "event_win_rate",
        "worst_event",
        "mean_signal_ret",
        "signal_win_rate",
        "worst_signal",
        "events_return",
    }
    report = [
        "# G3 Range 事件曲线与每日拥挤审计 V1",
        "",
        "## 口径",
        "",
        "- 样本：`box_bottom_stress_reclaim` 经过 30m 确认后的候选，剔除 5 日收益尚未完整的样本。",
        "- 事件曲线：每个 entry_date 只取排名前 N，按当日候选平均收益作为一笔事件收益，再复利串联。",
        "- 这是拥挤度/排序有效性审计，不是最终 slot 回测，也不含资金占用、重叠持仓、滑点和停牌限制。",
        "",
        "## 总体结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 年度结果",
        "",
        md_table(year, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 如果 top3/top5 明显优于 top20，说明横盘链路需要非常严格的每日容量控制。",
        "- 如果所有 topN 在 2024/2026 都不稳，说明排序无法解决环境失效，需要重写横盘定义。",
        "- 该审计不能替代完整 slot 复算，只用于决定是否值得进入下一步。",
        "",
    ]
    (OUT_DIR / "range_event_curve_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({
        "out_dir": str(OUT_DIR),
        "rows_with_complete_5d": int(len(df)),
        "summary": summary.to_dict(orient="records"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
