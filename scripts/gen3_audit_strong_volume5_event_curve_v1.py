from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "sources" / "g2_v2_complete.parquet"
OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_event_curve_v1"


def pct(x: float | None) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x) * 100:.2f}%"


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


def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1.0).min())


def build_event_curve(df: pd.DataFrame, label: str, top_n: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = df.dropna(subset=["eval_fwd_ret_5d"]).copy()
    d = d.sort_values(["entry_date", "v4_rank", "v4_score"], ascending=[True, True, False])
    selected = d.groupby("entry_date", sort=True).head(top_n).copy()
    daily = (
        selected.groupby("entry_date", sort=True)
        .agg(rows=("code", "size"), event_ret=("eval_fwd_ret_5d", "mean"), win_rate=("eval_fwd_ret_5d", lambda s: float((s > 0).mean())))
        .reset_index()
    )
    daily["equity"] = (1.0 + daily["event_ret"]).cumprod()
    daily["drawdown"] = daily["equity"] / daily["equity"].cummax() - 1.0
    selected["variant"] = label
    daily["variant"] = label
    return selected, daily


def summarize(label: str, selected: pd.DataFrame, daily: pd.DataFrame) -> dict:
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
        "mean_signal_ret": float(selected["eval_fwd_ret_5d"].mean()),
        "signal_win_rate": float((selected["eval_fwd_ret_5d"] > 0).mean()),
        "worst_signal": float(selected["eval_fwd_ret_5d"].min()),
    }


def year_summary(label: str, daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    d = daily.copy()
    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows = []
    for year, part in d.groupby("year"):
        eq = (1.0 + part["event_ret"]).cumprod()
        rows.append(
            {
                "variant": label,
                "year": int(year),
                "event_days": int(len(part)),
                "events_return": float(eq.iloc[-1] - 1.0),
                "max_drawdown": max_drawdown(eq),
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
    df = pd.read_parquet(SOURCE)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df = df[df["source_family"].eq("volume5")].copy()
    df["eval_fwd_ret_5d"] = df["outcome_fwd_ret_5d"].where(df["outcome_fwd_ret_5d"].notna(), df["fwd_ret_5d"])
    df["g3_market_style"] = df.apply(classify_market_style, axis=1)

    variants: list[tuple[str, pd.DataFrame, int]] = []
    for top_n in [1, 2, 3, 5]:
        variants.append((f"all_volume5_top{top_n}", df, top_n))
        variants.append((f"main_up_volume5_top{top_n}", df[df["g3_market_style"].eq("main_up")], top_n))
        variants.append((f"core_recovery_volume5_top{top_n}", df[df["g3_market_style"].isin(["main_up", "weak_recovery"])], top_n))

    summaries = []
    years = []
    for label, part, top_n in variants:
        selected, daily = build_event_curve(part, label, top_n)
        selected.to_csv(OUT_DIR / f"{label}_selected.csv", index=False, encoding="utf-8-sig")
        daily.to_csv(OUT_DIR / f"{label}_event_curve.csv", index=False, encoding="utf-8-sig")
        summaries.append(summarize(label, selected, daily))
        y = year_summary(label, daily)
        if not y.empty:
            years.append(y)

    summary = pd.DataFrame(summaries)
    year = pd.concat(years, ignore_index=True) if years else pd.DataFrame()
    summary.to_csv(OUT_DIR / "strong_volume5_event_summary.csv", index=False, encoding="utf-8-sig")
    year.to_csv(OUT_DIR / "strong_volume5_year_summary.csv", index=False, encoding="utf-8-sig")

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
        "# G3 Strong Volume5 事件曲线审计 V1",
        "",
        "## 口径",
        "",
        "- 源：G2 扩周期 `g2_v2_complete.parquet` 中的 `source_family=volume5`，只读既有源，不触发 G2 重建。",
        "- 评价：使用 `outcome_fwd_ret_5d`，缺失时回退 `fwd_ret_5d`。",
        "- 事件曲线：每个 entry_date 取 TopN，按当日候选平均 5 日收益复利串联。",
        "- 市场风格只用于粗分流：`main_up` 与 `main_up + weak_recovery`；不是最终 G3 slot 回测。",
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
        "- 若 core+weak_recovery 明显优于 all，说明强势链路需要市场风格路由；若 main_up 牺牲过多进攻，则弱修复小仓仍有价值。",
        "- 事件曲线只看离散信号，不含重叠持仓、30m 退出、冷却和滑点；下一步必须做 G3 原生 slot 复算。",
        "",
    ]
    (OUT_DIR / "strong_volume5_event_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({
        "out_dir": str(OUT_DIR),
        "source_rows": int(len(df)),
        "style_counts": df["g3_market_style"].value_counts(dropna=False).to_dict(),
        "best_by_total": summary.sort_values("total_return", ascending=False).head(5).to_dict(orient="records"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
