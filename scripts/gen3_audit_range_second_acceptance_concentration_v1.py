from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reports" / "gen3_range_second_acceptance_v1" / "deep_and_reclaim_any_second_accept__cost30" / "closed_trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_second_acceptance_concentration_v1"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int = 80) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    view = df.head(max_rows).copy()
    for col in view.columns:
        if col in pct_cols:
            view[col] = view[col].map(pct)
        elif pd.api.types.is_float_dtype(view[col]):
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else f"{float(x):.4f}")
        else:
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else str(x))
    suffix = "" if len(df) <= max_rows else f"\n\n_仅展示前 {max_rows} 行，共 {len(df)} 行。_"
    return view.to_markdown(index=False) + suffix


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["year"] = d["entry_date"].dt.year
    d["code"] = d["code"].astype(str)
    for col in [
        "policy_net_ret",
        "realized_pnl",
        "stake",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "index_mom20",
        "runup_from_60d_low",
        "limit_up_count",
        "limit_down_count",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def group_stats(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(cols, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        pnl = pd.to_numeric(g.get("realized_pnl"), errors="coerce")
        item = {col: val for col, val in zip(cols, key)}
        item.update(
            {
                "trade_count": int(len(g)),
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "avg_ret": float(ret.mean()) if len(ret) else 0.0,
                "sum_ret": float(ret.sum()) if len(ret) else 0.0,
                "pnl_sum": float(pnl.sum()) if len(pnl) else 0.0,
                "worst_trade": float(ret.min()) if len(ret) else 0.0,
                "best_trade": float(ret.max()) if len(ret) else 0.0,
            }
        )
        rows.append(item)
    return pd.DataFrame(rows).sort_values("pnl_sum", ascending=False)


def top_trade_concentration(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("realized_pnl", ascending=False).copy()
    total_pnl = float(pd.to_numeric(d["realized_pnl"], errors="coerce").sum())
    d["cum_pnl"] = pd.to_numeric(d["realized_pnl"], errors="coerce").cumsum()
    d["pnl_share_of_total"] = d["realized_pnl"] / total_pnl if total_pnl else 0.0
    d["cum_pnl_share_of_total"] = d["cum_pnl"] / total_pnl if total_pnl else 0.0
    keep = [
        "entry_date",
        "code",
        "name",
        "policy_net_ret",
        "realized_pnl",
        "pnl_share_of_total",
        "cum_pnl_share_of_total",
        "emotion_signal",
        "d0_second_accept",
        "d1_second_accept",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "index_mom20",
        "runup_from_60d_low",
    ]
    return d[[c for c in keep if c in d.columns]]


def leave_one_out(df: pd.DataFrame) -> pd.DataFrame:
    total_pnl = float(pd.to_numeric(df["realized_pnl"], errors="coerce").sum())
    rows = []
    for _, row in df.sort_values("realized_pnl", ascending=False).iterrows():
        removed = float(row["realized_pnl"])
        rows.append(
            {
                "removed_entry_date": row["entry_date"],
                "removed_code": row["code"],
                "removed_name": row.get("name", ""),
                "removed_ret": row["policy_net_ret"],
                "removed_pnl": removed,
                "remaining_pnl": total_pnl - removed,
                "remaining_pnl_pct_of_total": (total_pnl - removed) / total_pnl if total_pnl else 0.0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    total_pnl = float(pd.to_numeric(trades["realized_pnl"], errors="coerce").sum())
    wins = trades[trades["policy_net_ret"] > 0].copy()
    losses = trades[trades["policy_net_ret"] <= 0].copy()
    top = top_trade_concentration(trades)
    by_year = group_stats(trades, ["year"])
    by_stock = group_stats(trades, ["code", "name"])
    by_emotion = group_stats(trades, ["emotion_signal"])
    by_second = group_stats(trades, ["d0_second_accept", "d1_second_accept"])
    loo = leave_one_out(trades)

    files = {
        "audited_trades.csv": trades,
        "top_trade_concentration.csv": top,
        "by_year.csv": by_year,
        "by_stock.csv": by_stock,
        "by_emotion.csv": by_emotion,
        "by_second_accept_type.csv": by_second,
        "leave_one_trade_out.csv": loo,
    }
    for name, df in files.items():
        df.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    top1_share = float(top["pnl_share_of_total"].iloc[0]) if len(top) else 0.0
    top3_share = float(top["pnl_share_of_total"].head(3).sum()) if len(top) else 0.0
    top5_share = float(top["pnl_share_of_total"].head(5).sum()) if len(top) else 0.0
    pnl_without_top1 = float(loo["remaining_pnl"].iloc[0]) if len(loo) else 0.0
    pnl_without_top3 = total_pnl - float(top["realized_pnl"].head(3).sum()) if len(top) else 0.0
    summary = pd.DataFrame(
        [
            {
                "signal_window": "2020-01-01 to 2026-05-29",
                "trade_count": int(len(trades)),
                "win_count": int(len(wins)),
                "loss_count": int(len(losses)),
                "total_pnl": total_pnl,
                "top1_pnl_share": top1_share,
                "top3_pnl_share": top3_share,
                "top5_pnl_share": top5_share,
                "pnl_without_top1": pnl_without_top1,
                "pnl_without_top3": pnl_without_top3,
                "unique_stocks": int(trades["code"].nunique()),
                "unique_years": int(trades["year"].nunique()),
            }
        ]
    )
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "policy_net_ret",
        "pnl_share_of_total",
        "cum_pnl_share_of_total",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "amount_ratio20",
        "index_mom20",
        "runup_from_60d_low",
        "win_rate",
        "avg_ret",
        "sum_ret",
        "worst_trade",
        "best_trade",
        "removed_ret",
        "remaining_pnl_pct_of_total",
        "top1_pnl_share",
        "top3_pnl_share",
        "top5_pnl_share",
    }
    report = "\n".join(
        [
            "# G3 横盘二次承接收益集中度审计 v1",
            "",
            "## 审计范围",
            "- 策略：`deep_and_reclaim_any_second_accept`，中文是“箱体底部且日线修复，并要求D0后半日或D1任一再次放量承接”。",
            "- 回测/审计窗口：2020-01-01 至 2026-05-29。",
            "- 样本：22 笔，使用 30bps 成本的 slot 复算成交明细。",
            "",
            "## 总览",
            md_table(summary, pct_cols=pct_cols),
            "",
            "## 单笔收益贡献",
            md_table(top, pct_cols=pct_cols),
            "",
            "## 按年份",
            md_table(by_year, pct_cols=pct_cols),
            "",
            "## 按股票",
            md_table(by_stock, pct_cols=pct_cols),
            "",
            "## 按情绪",
            md_table(by_emotion, pct_cols=pct_cols),
            "",
            "## 按二次承接类型",
            md_table(by_second, pct_cols=pct_cols),
            "",
            "## 单笔剔除",
            md_table(loo, pct_cols=pct_cols),
            "",
            "## 结论",
            "- 该方向存在明显收益集中风险：必须重点看 top1/top3 贡献，而不能只看全周期收益。",
            "- 若去掉最大贡献票后仍为正，说明方向有一定结构价值；若去掉 top3 后收益大幅塌陷，则只能作为研究线索，不能升级为 G3 主候选源。",
        ]
    )
    (OUT_DIR / "REPORT.md").write_text(report + "\n", encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
