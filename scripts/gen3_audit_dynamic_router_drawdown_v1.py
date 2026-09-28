from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "reports" / "gen3_dynamic_router_combo_v1"
OUT_DIR = ROOT / "reports" / "gen3_dynamic_router_drawdown_audit_v1"


def pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
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


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    curve = pd.read_csv(SOURCE_DIR / "dynamic_router_mtm_equity_curve.csv")
    trades = pd.read_csv(SOURCE_DIR / "dynamic_router_closed_trades.csv")
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce").dt.normalize()
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.normalize()
    trades["policy_exit_date"] = pd.to_datetime(trades["policy_exit_date"], errors="coerce").dt.normalize()
    for col in ["equity", "peak", "drawdown", "worst_open_mtm_ret", "open_down_panic", "open_range_gap", "open_strong_main"]:
        if col in curve.columns:
            curve[col] = pd.to_numeric(curve[col], errors="coerce")
    for col in ["policy_net_ret", "realized_pnl", "stake", "score", "route_priority"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    return curve.dropna(subset=["date", "equity"]).sort_values("date"), trades.dropna(subset=["entry_date", "policy_exit_date"])


def max_drawdown_window(curve: pd.DataFrame) -> dict:
    trough_idx = curve["drawdown"].idxmin()
    trough = curve.loc[trough_idx]
    pre = curve.loc[:trough_idx]
    peak_idx = pre["equity"].idxmax()
    peak = curve.loc[peak_idx]
    after = curve.loc[trough_idx:].copy()
    recovered = after[after["equity"].ge(float(peak["equity"]))]
    recovery_date = recovered["date"].iloc[0] if not recovered.empty else pd.NaT
    return {
        "peak_date": pd.Timestamp(peak["date"]),
        "trough_date": pd.Timestamp(trough["date"]),
        "recovery_date": recovery_date,
        "peak_equity": float(peak["equity"]),
        "trough_equity": float(trough["equity"]),
        "drawdown": float(trough["equity"] / peak["equity"] - 1.0),
        "days_peak_to_trough": int((pd.Timestamp(trough["date"]) - pd.Timestamp(peak["date"])).days),
        "recovered": bool(pd.notna(recovery_date)),
    }


def route_stats(trades: pd.DataFrame, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> pd.DataFrame:
    d = trades.copy()
    if start is not None and end is not None:
        overlaps = d["entry_date"].le(end) & d["policy_exit_date"].ge(start)
        d = d[overlaps].copy()
    rows = []
    for route, part in d.groupby("route"):
        net = part["policy_net_ret"]
        rows.append(
            {
                "route": route,
                "trades": int(len(part)),
                "win_rate": float((net > 0).mean()) if len(net) else 0.0,
                "avg_ret": float(net.mean()) if len(net) else 0.0,
                "sum_pnl": float(part["realized_pnl"].sum()) if "realized_pnl" in part else 0.0,
                "worst_trade": float(net.min()) if len(net) else 0.0,
                "bad10_count": int((net <= -0.10).sum()) if len(net) else 0,
            }
        )
    return pd.DataFrame(rows).sort_values("sum_pnl")


def annual_route_stats(trades: pd.DataFrame) -> pd.DataFrame:
    d = trades.copy()
    d["year"] = d["entry_date"].dt.year
    rows = []
    for (year, route), part in d.groupby(["year", "route"]):
        net = part["policy_net_ret"]
        rows.append(
            {
                "year": int(year),
                "route": route,
                "trades": int(len(part)),
                "win_rate": float((net > 0).mean()),
                "avg_ret": float(net.mean()),
                "sum_pnl": float(part["realized_pnl"].sum()),
                "worst_trade": float(net.min()),
            }
        )
    return pd.DataFrame(rows).sort_values(["year", "sum_pnl"])


def monthly_route_stats(trades: pd.DataFrame) -> pd.DataFrame:
    d = trades.copy()
    d["month"] = d["entry_date"].dt.to_period("M").astype(str)
    rows = []
    for (month, route), part in d.groupby(["month", "route"]):
        net = part["policy_net_ret"]
        rows.append(
            {
                "month": month,
                "route": route,
                "trades": int(len(part)),
                "win_rate": float((net > 0).mean()),
                "avg_ret": float(net.mean()),
                "sum_pnl": float(part["realized_pnl"].sum()),
                "worst_trade": float(net.min()),
            }
        )
    return pd.DataFrame(rows).sort_values("sum_pnl")


def daily_drawdown_context(curve: pd.DataFrame, window: dict) -> pd.DataFrame:
    start = window["peak_date"] - pd.Timedelta(days=10)
    end = window["trough_date"] + pd.Timedelta(days=10)
    cols = [
        "date",
        "equity",
        "drawdown",
        "open_positions",
        "open_down_panic",
        "open_range_gap",
        "open_strong_main",
        "opened",
        "realized_pnl",
        "worst_open_mtm_ret",
    ]
    return curve[curve["date"].between(start, end)][cols].copy()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    curve, trades = load_data()
    window = max_drawdown_window(curve)
    peak_date = window["peak_date"]
    trough_date = window["trough_date"]
    route_all = route_stats(trades)
    route_dd = route_stats(trades, peak_date, trough_date)
    annual = annual_route_stats(trades)
    monthly = monthly_route_stats(trades)
    dd_context = daily_drawdown_context(curve, window)
    dd_trades = trades[trades["entry_date"].le(trough_date) & trades["policy_exit_date"].ge(peak_date)].copy()
    worst_trades = trades.sort_values("policy_net_ret").head(30).copy()

    route_all.to_csv(OUT_DIR / "route_all_attribution.csv", index=False, encoding="utf-8-sig")
    route_dd.to_csv(OUT_DIR / "route_maxdd_window_attribution.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_route_attribution.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(OUT_DIR / "monthly_route_attribution.csv", index=False, encoding="utf-8-sig")
    dd_context.to_csv(OUT_DIR / "maxdd_daily_context.csv", index=False, encoding="utf-8-sig")
    dd_trades.to_csv(OUT_DIR / "maxdd_window_trades.csv", index=False, encoding="utf-8-sig")
    worst_trades.to_csv(OUT_DIR / "worst_30_trades.csv", index=False, encoding="utf-8-sig")

    top_bad_months = monthly.head(12)
    lines = [
        "# G3 三链动态组合回撤归因 V1",
        "",
        "## 最大回撤窗口",
        "",
        f"- 峰值日期：{peak_date.date()}，权益 {window['peak_equity']:.2f}。",
        f"- 谷底日期：{trough_date.date()}，权益 {window['trough_equity']:.2f}。",
        f"- 最大回撤：{pct(window['drawdown'])}，峰谷间隔 {window['days_peak_to_trough']} 天。",
        f"- 是否恢复前高：{'是' if window['recovered'] else '否'}。",
        "",
        "## 全周期链路贡献",
        "",
        md_table(route_all, {"win_rate", "avg_ret", "worst_trade"}),
        "",
        "## 最大回撤窗口链路贡献",
        "",
        md_table(route_dd, {"win_rate", "avg_ret", "worst_trade"}),
        "",
        "## 最差月份",
        "",
        md_table(top_bad_months, {"win_rate", "avg_ret", "worst_trade"}),
        "",
        "## 最差交易",
        "",
        md_table(
            worst_trades[
                [
                    "entry_date",
                    "policy_exit_date",
                    "code",
                    "name",
                    "route",
                    "policy_net_ret",
                    "realized_pnl",
                    "score",
                ]
            ],
            {"policy_net_ret"},
        ),
        "",
        "## 初步判断",
        "",
        "- 下一步降回撤不应只砍收益最低链路，而要看最大回撤窗口里哪条链造成资金曲线连续下行。",
        "- 如果最差月份集中在 strong_main，需要给强势链增加市场门禁或弱势停手机制；如果集中在 range_gap，需要对弱反弹缺口加成本/指数强度保护。",
    ]
    (OUT_DIR / "dynamic_router_drawdown_audit_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "max_drawdown_window": {
                    "peak_date": str(peak_date.date()),
                    "trough_date": str(trough_date.date()),
                    "recovery_date": str(window["recovery_date"].date()) if pd.notna(window["recovery_date"]) else None,
                    "drawdown": window["drawdown"],
                    "days_peak_to_trough": window["days_peak_to_trough"],
                },
                "route_dd": route_dd.to_dict(orient="records"),
                "next_step": "design_drawdown_reduction_rules",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
