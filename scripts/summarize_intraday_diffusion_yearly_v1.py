from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "reports" / "intraday_diffusion_threshold_grid_v1"
OUT_DIR = ROOT / "reports" / "intraday_diffusion_yearly_breakdown_v1"


def _pct(value) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if pd.isna(x):
        return ""
    return f"{x:.2%}"


def _year_from_label(value) -> int | None:
    try:
        x = int(value)
    except Exception:
        return None
    if x > 10000000:
        return x // 10000
    if 1900 <= x <= 2100:
        return x
    return None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = pd.read_csv(IN_DIR / "threshold_summary.csv", encoding="utf-8-sig")
    summary["calendar_year"] = summary["year"].map(_year_from_label)
    rows: list[dict] = []
    for (threshold, calendar_year, horizon), g in summary.groupby(["threshold", "calendar_year", "horizon"]):
        wide = g.pivot_table(index=["threshold", "calendar_year", "horizon"], columns="group", values="avg", aggfunc="mean").reset_index()
        if wide.empty:
            continue
        row = wide.iloc[0].to_dict()
        main_accept = row.get("mainline_sector_acceptance")
        market_accept = row.get("market_acceptance")
        rows.append(
            {
                "threshold": threshold,
                "year": int(calendar_year),
                "horizon": int(horizon),
                "mainline_sector_acceptance": main_accept,
                "market_acceptance": market_accept,
                "diff": None if pd.isna(main_accept) or pd.isna(market_accept) else main_accept - market_accept,
                "mainline_sector_all": row.get("mainline_sector_all"),
                "market_all": row.get("market_all"),
                "sample_groups": int(len(g)),
            }
        )
    out = pd.DataFrame(rows)
    out_path = OUT_DIR / "yearly_diffusion_breakdown.csv"
    out.to_csv(out_path, index=False, encoding="utf-8-sig")

    stability_rows: list[dict] = []
    for (threshold, horizon), g in out.groupby(["threshold", "horizon"]):
        vals = pd.to_numeric(g["diff"], errors="coerce").dropna()
        stability_rows.append(
            {
                "threshold": threshold,
                "horizon": int(horizon),
                "years": int(len(vals)),
                "positive_years": int((vals > 0).sum()),
                "positive_ratio": float((vals > 0).mean()) if len(vals) else None,
                "avg_diff": float(vals.mean()) if len(vals) else None,
                "median_diff": float(vals.median()) if len(vals) else None,
                "worst_diff": float(vals.min()) if len(vals) else None,
                "best_diff": float(vals.max()) if len(vals) else None,
            }
        )
    stability = pd.DataFrame(stability_rows)
    stability_path = OUT_DIR / "stability_summary.csv"
    stability.to_csv(stability_path, index=False, encoding="utf-8-sig")

    report_path = OUT_DIR / "REPORT.md"
    lines = [
        "# 盘中扩散年度稳定性拆解 V1",
        "",
        f"- 来源：`{IN_DIR / 'threshold_summary.csv'}`",
        "- 目的：检查 loose/base/strict 三组阈值是否跨年份稳定，而不是依赖单一年份贡献。",
        "",
        "## 稳定性汇总",
        "",
        "| 阈值 | 持有天数 | 年份数 | 正收益年份 | 正收益占比 | 平均差值 | 中位差值 | 最差年份差值 | 最好年份差值 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in stability.sort_values(["threshold", "horizon"]).itertuples(index=False):
        lines.append(
            f"| {row.threshold} | {int(row.horizon)} | {int(row.years)} | {int(row.positive_years)} | "
            f"{_pct(row.positive_ratio)} | {_pct(row.avg_diff)} | {_pct(row.median_diff)} | "
            f"{_pct(row.worst_diff)} | {_pct(row.best_diff)} |"
        )
    lines.extend(["", "## 年度明细：base 阈值", "", "| 年份 | 20日差值 | 60日差值 | 120日差值 |", "|---:|---:|---:|---:|"])
    base = out[out["threshold"].eq("base")].copy()
    for year, g in base.groupby("year"):
        vals = {}
        for horizon in [20, 60, 120]:
            h = g[g["horizon"].eq(horizon)]
            vals[horizon] = h["diff"].iloc[0] if not h.empty else None
        lines.append(f"| {int(year)} | {_pct(vals[20])} | {_pct(vals[60])} | {_pct(vals[120])} |")
    lines.extend(
        [
            "",
            "## 解读",
            "",
            "- 正收益占比高，说明信号跨市场年份更稳。",
            "- 最差年份仍为负，说明它不能替代个股买点和风控。",
            "- 若 120 日稳定性显著高于 20 日，说明它更适合作为主线环境因子，而不是日内直接买入因子。",
            "",
            f"- 年度明细：`{out_path}`",
            f"- 稳定性汇总：`{stability_path}`",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(str(report_path))
    print(str(stability_path))
    print(stability.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
