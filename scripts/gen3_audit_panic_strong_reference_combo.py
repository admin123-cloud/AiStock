from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PANIC_CURVE = ROOT / "reports" / "gen3_panic_v2_research" / "failure_exit_30m_slot_resim_v1" / "m30_close5_full_nextopen_slot5_20pct_equity_curve.csv"
PANIC_TRADES = ROOT / "reports" / "gen3_panic_v2_research" / "failure_exit_30m_slot_resim_v1" / "m30_close5_full_nextopen_slot5_20pct_closed_trades.csv"
STRONG_CURVE = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "runs" / "official" / "full" / "equity_curve.csv"
STRONG_TRADES = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "runs" / "official" / "full" / "trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_panic_strong_reference_combo_v1"


def pct(x: float | None) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{x * 100:.2f}%"


def max_drawdown(series: pd.Series) -> float:
    peak = series.cummax()
    return float((series / peak - 1.0).min())


def curve_metrics(curve: pd.DataFrame, equity_col: str) -> dict:
    s = curve[equity_col].astype(float)
    return {
        "start_date": str(curve["date"].min().date()),
        "end_date": str(curve["date"].max().date()),
        "days": int(len(curve)),
        "total_return": float(s.iloc[-1] / s.iloc[0] - 1.0),
        "max_drawdown": max_drawdown(s),
    }


def year_summary(curve: pd.DataFrame, equity_col: str, label: str) -> pd.DataFrame:
    df = curve[["date", equity_col]].copy()
    df["year"] = df["date"].dt.year
    rows = []
    for year, part in df.groupby("year"):
        first = float(part[equity_col].iloc[0])
        last = float(part[equity_col].iloc[-1])
        rows.append({
            "strategy": label,
            "year": int(year),
            "return": last / first - 1.0,
            "max_drawdown": max_drawdown(part[equity_col].astype(float)),
            "days": int(len(part)),
        })
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
    panic = pd.read_csv(PANIC_CURVE, parse_dates=["date"]).sort_values("date")
    strong = pd.read_csv(STRONG_CURVE, parse_dates=["date"]).sort_values("date")

    panic["panic_equity_norm"] = panic["equity"] / float(panic["equity"].iloc[0])
    strong["strong_equity_norm"] = strong["strategy_equity"]

    dates = pd.DataFrame({"date": pd.date_range(min(panic["date"].min(), strong["date"].min()), max(panic["date"].max(), strong["date"].max()), freq="D")})
    combo = dates.merge(panic[["date", "panic_equity_norm"]], on="date", how="left").merge(strong[["date", "strong_equity_norm"]], on="date", how="left")
    combo[["panic_equity_norm", "strong_equity_norm"]] = combo[["panic_equity_norm", "strong_equity_norm"]].ffill()
    combo = combo.dropna(subset=["panic_equity_norm", "strong_equity_norm"]).copy()
    combo["combo_50_50_norm"] = combo["panic_equity_norm"] * 0.5 + combo["strong_equity_norm"] * 0.5

    metrics = pd.DataFrame([
        {"strategy": "panic_g3_final_candidate", **curve_metrics(panic.rename(columns={"equity": "panic_equity"}), "panic_equity")},
        {"strategy": "strong_volume5_g2_reference", **curve_metrics(strong.rename(columns={"strategy_equity": "strong_equity"}), "strong_equity")},
        {"strategy": "reference_combo_50_50", **curve_metrics(combo.rename(columns={"combo_50_50_norm": "combo_equity"}), "combo_equity")},
    ])
    yearly = pd.concat([
        year_summary(panic.rename(columns={"equity": "panic_equity"}), "panic_equity", "panic_g3_final_candidate"),
        year_summary(strong.rename(columns={"strategy_equity": "strong_equity"}), "strong_equity", "strong_volume5_g2_reference"),
        year_summary(combo.rename(columns={"combo_50_50_norm": "combo_equity"}), "combo_equity", "reference_combo_50_50"),
    ], ignore_index=True)

    panic_trades = pd.read_csv(PANIC_TRADES)
    strong_trades = pd.read_csv(STRONG_TRADES)
    trade_summary = pd.DataFrame([
        {
            "strategy": "panic_g3_final_candidate",
            "trades": len(panic_trades),
            "mean_trade_ret": panic_trades["net_ret"].mean(),
            "win_rate": (panic_trades["net_ret"] > 0).mean(),
            "worst_trade": panic_trades["net_ret"].min(),
        },
        {
            "strategy": "strong_volume5_g2_reference",
            "trades": len(strong_trades),
            "mean_trade_ret": strong_trades["return"].mean(),
            "win_rate": (strong_trades["return"] > 0).mean(),
            "worst_trade": strong_trades["return"].min(),
        },
    ])

    combo.to_csv(OUT_DIR / "reference_combo_equity_curve.csv", index=False, encoding="utf-8-sig")
    metrics.to_csv(OUT_DIR / "reference_combo_metrics.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUT_DIR / "reference_combo_year_summary.csv", index=False, encoding="utf-8-sig")
    trade_summary.to_csv(OUT_DIR / "reference_combo_trade_summary.csv", index=False, encoding="utf-8-sig")

    report = [
        "# G3 Panic + Strong 参考组合审计 V1",
        "",
        "## 口径说明",
        "",
        "- Panic：使用 G3 已验证候选 `m30_close5_full_nextopen + slot5_20pct` 的逐日 MTM 曲线。",
        "- Strong：暂时使用 G2 official full 的 `volume5` 资金曲线作为迁移前参考，不代表 G3 独立重算已经完成。",
        "- 组合：仅做 50/50 归一化叠加，目的是观察收益来源和年份互补性；不是最终资金管理方案。",
        "- 本脚本只读取报告文件并写入 G3 报告目录，不触发 G2 重建，不写 G2 runtime。",
        "",
        "## 核心指标",
        "",
        md_table(metrics, pct_cols={"total_return", "max_drawdown"}),
        "",
        "## 交易统计",
        "",
        md_table(trade_summary, pct_cols={"mean_trade_ret", "win_rate", "worst_trade"}),
        "",
        "## 年度表现",
        "",
        md_table(yearly, pct_cols={"return", "max_drawdown"}),
        "",
        "## 判断",
        "",
        "- Panic 的价值是低回撤尾部控制；Strong 的价值是进攻，但参考曲线最大回撤明显偏大，不能直接并入 G3 正式组合。",
        "- 2022、2023 对 `volume5` 仍是不友好年份；G3 强势链路必须保留市场风格/候选质量双约束，不能只靠指数强弱开关。",
        "- 下一步应做 G3 原生 `strong_volume5` 的 slot 复算，而不是继续用 G2 official 曲线替代。",
        "",
    ]
    (OUT_DIR / "panic_strong_reference_combo_report_cn.md").write_text("\n".join(report), encoding="utf-8")

    print(json.dumps({
        "out_dir": str(OUT_DIR),
        "metrics": metrics.to_dict(orient="records"),
        "panic_trades": int(len(panic_trades)),
        "strong_reference_trades": int(len(strong_trades)),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
