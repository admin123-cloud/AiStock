from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "reports" / "gen3_range_floor10_reclaim60_emotion_split_v1"
OUT_DIR = ROOT / "reports" / "gen3_range_neutral_only_concentration_audit_v1"
PROFILES = ["cost30", "cost100", "shock2_cost30"]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    view = df.copy()
    if max_rows is not None:
        view = view.head(max_rows)
    rows: list[dict[str, Any]] = []
    for _, row in view.iterrows():
        item: dict[str, Any] = {}
        for col in view.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_closed(profile: str) -> pd.DataFrame:
    p = SOURCE_DIR / f"neutral_only__{profile}" / "closed_trades.csv"
    d = pd.read_csv(p)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    d["profile"] = profile
    for col in [
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "index_mom20",
        "limit_up_count",
        "limit_down_count",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def concentration_one(d: pd.DataFrame) -> dict[str, Any]:
    x = d.sort_values("policy_net_ret", ascending=False).copy()
    total = float(x["policy_net_ret"].sum())
    top1 = float(x["policy_net_ret"].head(1).sum())
    top3 = float(x["policy_net_ret"].head(3).sum())
    top5 = float(x["policy_net_ret"].head(5).sum())
    without_top1 = x.iloc[1:]
    without_top3 = x.iloc[3:]
    without_top5 = x.iloc[5:]
    return {
        "profile": str(x["profile"].iloc[0]) if len(x) else "",
        "trade_count": int(len(x)),
        "sum_ret": total,
        "avg_ret": float(x["policy_net_ret"].mean()) if len(x) else 0.0,
        "win_rate": float((x["policy_net_ret"] > 0).mean()) if len(x) else 0.0,
        "top1_sum": top1,
        "top1_share": float(top1 / total) if total else 0.0,
        "top3_sum": top3,
        "top3_share": float(top3 / total) if total else 0.0,
        "top5_sum": top5,
        "top5_share": float(top5 / total) if total else 0.0,
        "without_top1_sum": float(without_top1["policy_net_ret"].sum()) if len(without_top1) else 0.0,
        "without_top3_sum": float(without_top3["policy_net_ret"].sum()) if len(without_top3) else 0.0,
        "without_top5_sum": float(without_top5["policy_net_ret"].sum()) if len(without_top5) else 0.0,
        "worst_trade": float(x["policy_net_ret"].min()) if len(x) else 0.0,
    }


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (profile, year), g in d.groupby(["profile", "year"]):
        rows.append(
            {
                "profile": profile,
                "year": int(year),
                "trade_count": int(len(g)),
                "sum_ret": float(g["policy_net_ret"].sum()),
                "avg_ret": float(g["policy_net_ret"].mean()),
                "win_rate": float((g["policy_net_ret"] > 0).mean()),
                "best_trade": float(g["policy_net_ret"].max()),
                "worst_trade": float(g["policy_net_ret"].min()),
            }
        )
    return pd.DataFrame(rows).sort_values(["profile", "year"])


def top_trades(d: pd.DataFrame) -> pd.DataFrame:
    keep = [
        "profile",
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "index_mom20",
        "limit_up_count",
        "limit_down_count",
    ]
    out = d.sort_values(["profile", "policy_net_ret"], ascending=[True, False])[keep].copy()
    out["entry_date"] = out["entry_date"].dt.strftime("%Y-%m-%d")
    return out


def stock_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (profile, code, name), g in d.groupby(["profile", "code", "name"]):
        rows.append(
            {
                "profile": profile,
                "code": code,
                "name": name,
                "trade_count": int(len(g)),
                "sum_ret": float(g["policy_net_ret"].sum()),
                "avg_ret": float(g["policy_net_ret"].mean()),
                "best_trade": float(g["policy_net_ret"].max()),
                "worst_trade": float(g["policy_net_ret"].min()),
            }
        )
    return pd.DataFrame(rows).sort_values(["profile", "sum_ret"], ascending=[True, False])


def leave_one_year_out(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for profile, gp in d.groupby("profile"):
        years = sorted(gp["year"].dropna().unique().tolist())
        for year in years:
            left = gp[~gp["year"].eq(year)]
            rows.append(
                {
                    "profile": profile,
                    "removed_year": int(year),
                    "left_trade_count": int(len(left)),
                    "left_sum_ret": float(left["policy_net_ret"].sum()) if len(left) else 0.0,
                    "left_avg_ret": float(left["policy_net_ret"].mean()) if len(left) else 0.0,
                    "left_win_rate": float((left["policy_net_ret"] > 0).mean()) if len(left) else 0.0,
                }
            )
    return pd.DataFrame(rows).sort_values(["profile", "left_sum_ret"])


def write_report(conc: pd.DataFrame, years: pd.DataFrame, top: pd.DataFrame, stocks: pd.DataFrame, loo: pd.DataFrame) -> None:
    pct_cols = {
        "sum_ret",
        "avg_ret",
        "win_rate",
        "top1_sum",
        "top1_share",
        "top3_sum",
        "top3_share",
        "top5_sum",
        "top5_share",
        "without_top1_sum",
        "without_top3_sum",
        "without_top5_sum",
        "worst_trade",
        "best_trade",
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "index_mom20",
        "left_sum_ret",
        "left_avg_ret",
        "left_win_rate",
    }
    cost30 = conc[conc["profile"].eq("cost30")].iloc[0]
    shock = conc[conc["profile"].eq("shock2_cost30")].iloc[0]
    lines = [
        "# G3 neutral_only 收益集中度审计 v1",
        "",
        "## 策略名解释",
        "",
        "- `neutral_only`：中文是“中性情绪横盘箱体底部修复源”。它不要求前一日是冰点，只要求满足 `floor10_reclaim60` 的结构和30m承接。",
        "- `floor10_reclaim60`：冰点后3日窗口里，箱体位置不高于10%，且日线收盘修复不低于60%的横盘箱体底部30m承接买法。这里拿其中 emotion_signal=neutral 的样本单独审计。",
        "- `top1_share/top3_share`：最大1笔/3笔盈利占总收益比例，用来检查是否靠少数个案撑收益。",
        "- `without_top1_sum`：去掉最大赢家后的单笔净收益合计；如果大幅转弱，说明策略有集中度风险。",
        "",
        "## 本轮结论",
        "",
        f"- `neutral_only` 在 30bps 下合计 {pct(cost30['sum_ret'])}，但最大赢家一笔贡献 {pct(cost30['top1_sum'])}，占总收益 {pct(cost30['top1_share'])}；前三笔贡献 {pct(cost30['top3_sum'])}，占总收益 {pct(cost30['top3_share'])}。",
        f"- 去掉最大赢家后，30bps 合计仍有 {pct(cost30['without_top1_sum'])}；去掉前三大赢家后变为 {pct(cost30['without_top3_sum'])}。这说明它不是完全靠一笔，但前三笔依赖很重。",
        f"- 在 2%冲击口径下，`neutral_only` 合计 {pct(shock['sum_ret'])}；去掉前三大赢家后为 {pct(shock['without_top3_sum'])}。如果这个数转负，就说明高摩擦下不够稳。",
        "- 因此 `neutral_only` 不能直接作为正式子策略；它更适合作为“需要重建入场质量定义”的候选源。下一步应拆分它的盈利形态，而不是用一个统一过滤硬砍。",
        "",
        "## 集中度汇总",
        "",
        md_table(conc, pct_cols=pct_cols),
        "",
        "## 年度收益",
        "",
        md_table(years, pct_cols=pct_cols),
        "",
        "## 留一年剔除审计",
        "",
        md_table(loo, pct_cols=pct_cols),
        "",
        "## 个股贡献",
        "",
        md_table(stocks, pct_cols=pct_cols, max_rows=30),
        "",
        "## 全部成交按收益排序",
        "",
        md_table(top, pct_cols=pct_cols, max_rows=60),
        "",
        "## 下一步目标",
        "",
        "- 把 `neutral_only` 拆成至少两类：一类是“急跌错杀后强修复”，一类是“弱反抽后继续阴跌”。两者不能共用同一套买法。",
        "- 优先验证 30m 二次承接：首次承接后，D1或当日下午是否还有新的放量确认；这比再调 `amount_ratio3` 单阈值更接近根因。",
        "- 同时保留 `neutral_only` 的逐笔观察表，不接入正式 G3。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_d = pd.concat([load_closed(profile) for profile in PROFILES], ignore_index=True)
    conc = pd.DataFrame([concentration_one(all_d[all_d["profile"].eq(profile)]) for profile in PROFILES])
    years = year_summary(all_d)
    top = top_trades(all_d)
    stocks = stock_summary(all_d)
    loo = leave_one_year_out(all_d)
    conc.to_csv(OUT_DIR / "concentration_summary.csv", index=False, encoding="utf-8-sig")
    years.to_csv(OUT_DIR / "year_summary.csv", index=False, encoding="utf-8-sig")
    top.to_csv(OUT_DIR / "trades_sorted.csv", index=False, encoding="utf-8-sig")
    stocks.to_csv(OUT_DIR / "stock_summary.csv", index=False, encoding="utf-8-sig")
    loo.to_csv(OUT_DIR / "leave_one_year_out.csv", index=False, encoding="utf-8-sig")
    write_report(conc, years, top, stocks, loo)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
