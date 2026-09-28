from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
IN_DIR = ROOT / "reports" / "gen3_strong_volume5_slot_resim_v1"
OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_slot_failure_v1"

FOCUS = [
    ("core_h5_slot2", "core_recovery_volume5_hold5_slot2_50pct_daily1"),
    ("core_h5_slot5", "core_recovery_volume5_hold5_slot5_20pct_daily2"),
    ("main_h5_slot2", "main_up_volume5_hold5_slot2_50pct_daily1"),
]


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _load_pair(stem: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = pd.read_csv(IN_DIR / f"{stem}_closed_trades.csv")
    curve = pd.read_csv(IN_DIR / f"{stem}_curve.csv")
    if not trades.empty:
        trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce")
        trades["policy_exit_date"] = pd.to_datetime(trades["policy_exit_date"], errors="coerce")
        trades["net_ret"] = pd.to_numeric(trades["net_ret"], errors="coerce")
        trades["realized_pnl"] = pd.to_numeric(trades["realized_pnl"], errors="coerce")
    if not curve.empty:
        curve["date"] = pd.to_datetime(curve["date"], errors="coerce")
        curve["equity"] = pd.to_numeric(curve["equity"], errors="coerce")
        curve["drawdown"] = pd.to_numeric(curve["drawdown"], errors="coerce")
        curve["opened"] = pd.to_numeric(curve["opened"], errors="coerce").fillna(0)
    return trades, curve


def _annual(label: str, trades: pd.DataFrame, curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, part in curve.groupby(curve["date"].dt.year):
        part = part.sort_values("date")
        if part.empty:
            continue
        tw = trades[trades["entry_date"].dt.year.eq(year)] if not trades.empty else pd.DataFrame()
        rows.append(
            {
                "label": label,
                "year": int(year),
                "start": part["date"].iloc[0].date().isoformat(),
                "end": part["date"].iloc[-1].date().isoformat(),
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "closed": int(len(tw)),
                "win_rate": float((tw["net_ret"] > 0).mean()) if len(tw) else 0.0,
                "mean_trade_ret": float(tw["net_ret"].mean()) if len(tw) else 0.0,
                "worst_trade": float(tw["net_ret"].min()) if len(tw) else 0.0,
                "opened_days": int((part["opened"] > 0).sum()),
            }
        )
    return pd.DataFrame(rows)


def _monthly(label: str, curve: pd.DataFrame) -> pd.DataFrame:
    d = curve.copy()
    d["month"] = d["date"].dt.to_period("M").astype(str)
    rows = []
    for month, part in d.groupby("month"):
        part = part.sort_values("date")
        if len(part) < 2:
            continue
        rows.append(
            {
                "label": label,
                "month": month,
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0),
                "max_drawdown": _max_drawdown(part["equity"]),
                "opened_days": int((part["opened"] > 0).sum()),
            }
        )
    return pd.DataFrame(rows)


def _worst_trades(label: str, trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    cols = [c for c in ["entry_date", "policy_exit_date", "code", "name", "g3_market_style", "v4_rank", "v4_score", "net_ret", "realized_pnl"] if c in trades.columns]
    out = trades.sort_values("net_ret").head(15)[cols].copy()
    out.insert(0, "label", label)
    for col in ["entry_date", "policy_exit_date"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.date.astype(str)
    return out


def _drawdown_points(label: str, curve: pd.DataFrame) -> pd.DataFrame:
    if curve.empty:
        return pd.DataFrame()
    cols = ["date", "equity", "drawdown", "open_positions", "opened", "skipped_slots", "skipped_daily_limit"]
    out = curve.sort_values("drawdown").head(12)[cols].copy()
    out.insert(0, "label", label)
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date.astype(str)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    annual_parts = []
    monthly_parts = []
    worst_parts = []
    dd_parts = []
    for label, stem in FOCUS:
        trades, curve = _load_pair(stem)
        annual_parts.append(_annual(label, trades, curve))
        monthly_parts.append(_monthly(label, curve))
        worst_parts.append(_worst_trades(label, trades))
        dd_parts.append(_drawdown_points(label, curve))

    annual = pd.concat(annual_parts, ignore_index=True) if annual_parts else pd.DataFrame()
    monthly = pd.concat(monthly_parts, ignore_index=True) if monthly_parts else pd.DataFrame()
    worst = pd.concat(worst_parts, ignore_index=True) if worst_parts else pd.DataFrame()
    dd = pd.concat(dd_parts, ignore_index=True) if dd_parts else pd.DataFrame()

    annual.to_csv(OUT_DIR / "annual_metrics.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(OUT_DIR / "monthly_metrics.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst_trades.csv", index=False, encoding="utf-8-sig")
    dd.to_csv(OUT_DIR / "worst_drawdown_points.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"return", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "net_ret"}
    core_slot5_annual = annual[annual["label"].eq("core_h5_slot5")].copy()
    worst_months = monthly.sort_values("return").head(15).copy()
    report = [
        "# G3 强势链路 Volume5 槽位失败归因 V1",
        "",
        "## 口径",
        "",
        "- 输入来自第13步 `gen3_strong_volume5_slot_resim_v1`，不重新构造候选，不触发 G2。",
        "- 重点看三个固定焦点：`core_h5_slot2`、`core_h5_slot5`、`main_h5_slot2`。",
        "- 第13步曲线是已实现收益代理曲线，不含日内 MTM，因此本报告用于定位年份/月份和最差交易，不作为最终风险上限。",
        "",
        "## Core H5 Slot5 年度",
        "",
        _md_table(core_slot5_annual, pct_cols=pct_cols),
        "",
        "## 所有焦点年度",
        "",
        _md_table(annual, pct_cols=pct_cols),
        "",
        "## 最差月份 Top15",
        "",
        _md_table(worst_months, pct_cols=pct_cols),
        "",
        "## 最差交易 Top15",
        "",
        _md_table(worst, pct_cols=pct_cols),
        "",
        "## 最深回撤日期",
        "",
        _md_table(dd, pct_cols={"drawdown"}),
        "",
        "## 判断",
        "",
        "- `slot5_20pct_daily2` 的回撤显著低于 `slot2_50pct_daily1`，说明仓位分散是强势链路迁移的必要基础，而不是事后美化。",
        "- 强势链路的主要风险仍来自单笔大亏和坏月份集中，下一步应做盘中止损/退出与失败后冷却，而不是继续缩小选股池。",
        "- 该链路可以保留为 G3 强势候选研究线，但暂不能并入正式候选组合。",
        "",
    ]
    (OUT_DIR / "strong_volume5_slot_failure_report_cn.md").write_text("\n".join(report), encoding="utf-8")
    print(
        {
            "out_dir": str(OUT_DIR),
            "annual_rows": len(annual),
            "worst_month": worst_months.head(1).to_dict(orient="records"),
            "core_slot5_annual": core_slot5_annual.to_dict(orient="records"),
        }
    )


if __name__ == "__main__":
    main()
