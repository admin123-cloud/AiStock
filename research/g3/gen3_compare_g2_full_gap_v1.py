from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_g2_full_gap_attribution_v1"

G2_DIR = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "runs" / "official" / "full"
G2_ATTR_DIR = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "attribution"
G3_DIR = ROOT / "reports" / "gen3_route_execution_mandate_candidate_package_v3"

G2_CURVE = G2_DIR / "equity_curve.csv"
G2_TRADES = G2_DIR / "trades.csv"
G2_SIGNALS = G2_DIR / "signals.csv"
G2_SUMMARY = G2_DIR / "summary.json"

G3_CURVE = G3_DIR / "g3_route_execution_mandate_candidate_equity_curve.csv"
G3_TRADES = G3_DIR / "g3_route_execution_mandate_candidate_closed_trades.csv"
G3_SUMMARY = G3_DIR / "summary.json"

RECENT_START = "2024-06-01"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, low_memory=False)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _max_drawdown(equity: pd.Series) -> float:
    s = pd.to_numeric(equity, errors="coerce").dropna()
    if s.empty:
        return 0.0
    return float((s / s.cummax() - 1.0).min())


def _curve_window(curve: pd.DataFrame, start: str, date_col: str = "date", equity_col: str = "equity") -> dict[str, Any]:
    d = curve.copy()
    d[date_col] = pd.to_datetime(d[date_col], errors="coerce")
    d[equity_col] = pd.to_numeric(d[equity_col], errors="coerce")
    d = d.dropna(subset=[date_col, equity_col]).sort_values(date_col)
    part = d[d[date_col].ge(pd.Timestamp(start))].copy()
    if part.empty:
        return {"start": start, "available": False}
    start_eq = float(part[equity_col].iloc[0])
    end_eq = float(part[equity_col].iloc[-1])
    return {
        "start": str(part[date_col].iloc[0].date()),
        "end": str(part[date_col].iloc[-1].date()),
        "trading_days": int(len(part)),
        "start_equity": start_eq,
        "end_equity": end_eq,
        "return": end_eq / start_eq - 1.0 if start_eq > 0 else None,
        "max_drawdown": _max_drawdown(part[equity_col]),
    }


def _annual(curve: pd.DataFrame, date_col: str = "date", equity_col: str = "equity") -> pd.DataFrame:
    d = curve.copy()
    d[date_col] = pd.to_datetime(d[date_col], errors="coerce")
    d[equity_col] = pd.to_numeric(d[equity_col], errors="coerce")
    d = d.dropna(subset=[date_col, equity_col]).sort_values(date_col)
    rows = []
    for year, g in d.groupby(d[date_col].dt.year, sort=True):
        start = float(g[equity_col].iloc[0])
        end = float(g[equity_col].iloc[-1])
        rows.append(
            {
                "year": int(year),
                "start": str(g[date_col].iloc[0].date()),
                "end": str(g[date_col].iloc[-1].date()),
                "return": end / start - 1.0 if start > 0 else None,
                "max_drawdown": _max_drawdown(g[equity_col]),
                "trading_days": int(len(g)),
            }
        )
    return pd.DataFrame(rows)


def _monthly(curve: pd.DataFrame, label: str, date_col: str = "date", equity_col: str = "equity") -> pd.DataFrame:
    d = curve.copy()
    d[date_col] = pd.to_datetime(d[date_col], errors="coerce")
    d[equity_col] = pd.to_numeric(d[equity_col], errors="coerce")
    d = d.dropna(subset=[date_col, equity_col]).sort_values(date_col)
    d = d[d[date_col].ge(pd.Timestamp(RECENT_START))]
    d["month"] = d[date_col].dt.strftime("%Y-%m")
    rows = []
    for month, g in d.groupby("month", sort=True):
        start = float(g[equity_col].iloc[0])
        end = float(g[equity_col].iloc[-1])
        rows.append(
            {
                "strategy": label,
                "month": month,
                "return": end / start - 1.0 if start > 0 else None,
                "max_drawdown": _max_drawdown(g[equity_col]),
                "start_equity": start,
                "end_equity": end,
            }
        )
    return pd.DataFrame(rows)


def _prepare_g2_trades(trades: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["buy_date"], errors="coerce")
    t["exit_date"] = pd.to_datetime(t["sell_date"], errors="coerce")
    for col in ["pnl", "capital", "return"]:
        t[col] = pd.to_numeric(t[col], errors="coerce")
    grouped = (
        t.groupby(["code", "name", "entry_date"], dropna=False)
        .agg(
            exit_date=("exit_date", "max"),
            rows=("code", "size"),
            capital=("capital", "sum"),
            pnl=("pnl", "sum"),
            avg_return=("return", "mean"),
            worst_return=("return", "min"),
            best_return=("return", "max"),
            exit_reason=("exit_reason", lambda x: "|".join(sorted(set(str(v) for v in x if pd.notna(v))))),
        )
        .reset_index()
    )
    grouped["net_return"] = grouped["pnl"] / grouped["capital"].replace(0, pd.NA)
    grouped["entry_date_str"] = grouped["entry_date"].dt.strftime("%Y-%m-%d")
    grouped["key"] = grouped["code"].astype(str) + "|" + grouped["entry_date_str"].astype(str)
    grouped["strategy"] = "G2_full"

    s = signals.copy()
    if not s.empty and "entry_date" in s.columns:
        s["entry_date"] = pd.to_datetime(s["entry_date"], errors="coerce")
        keep = [
            c
            for c in [
                "code",
                "entry_date",
                "signal_family",
                "source_family",
                "g2_v2_buy_logic",
                "sector_strong",
                "sector_score_bonus",
                "v4_rank",
                "v4_score",
                "score_volume5",
                "setup_type",
                "trigger_type",
                "l3_rt_strong3_ratio",
                "rt_breakout_vs_box_top",
            ]
            if c in s.columns
        ]
        signal_meta = s[keep].sort_values(["entry_date", "code"]).drop_duplicates(["code", "entry_date"])
        grouped = grouped.merge(signal_meta, on=["code", "entry_date"], how="left")
    return grouped


def _prepare_g3_trades(trades: pd.DataFrame) -> pd.DataFrame:
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], errors="coerce")
    t["exit_date"] = pd.to_datetime(t["stress_exit_date"], errors="coerce")
    for col in ["realized_pnl", "stake", "stress_net_ret", "score"]:
        if col in t.columns:
            t[col] = pd.to_numeric(t[col], errors="coerce")
    t["entry_date_str"] = t["entry_date"].dt.strftime("%Y-%m-%d")
    t["key"] = t["code"].astype(str) + "|" + t["entry_date_str"].astype(str)
    t["strategy"] = "G3_V3"
    t["net_return"] = t["stress_net_ret"]
    t["pnl"] = t["realized_pnl"]
    t["capital"] = t["stake"]
    return t


def _trade_summary(trades: pd.DataFrame, label: str) -> dict[str, Any]:
    if trades.empty:
        return {"strategy": label, "trade_count": 0}
    recent = trades[trades["entry_date"].ge(pd.Timestamp(RECENT_START))].copy()
    rets = pd.to_numeric(recent["net_return"], errors="coerce").dropna()
    pnl = pd.to_numeric(recent["pnl"], errors="coerce").fillna(0.0)
    return {
        "strategy": label,
        "recent_start": RECENT_START,
        "trade_count": int(len(recent)),
        "win_rate": float((rets > 0).mean()) if not rets.empty else None,
        "avg_trade_return": float(rets.mean()) if not rets.empty else None,
        "median_trade_return": float(rets.median()) if not rets.empty else None,
        "worst_trade": float(rets.min()) if not rets.empty else None,
        "best_trade": float(rets.max()) if not rets.empty else None,
        "pnl_sum": float(pnl.sum()),
        "top5_pnl_sum": float(recent.nlargest(5, "pnl")["pnl"].sum()) if "pnl" in recent.columns else None,
    }


def _family_summary(g2: pd.DataFrame) -> pd.DataFrame:
    recent = g2[g2["entry_date"].ge(pd.Timestamp(RECENT_START))].copy()
    if recent.empty:
        return pd.DataFrame()
    family_col = "signal_family" if "signal_family" in recent.columns else "source_family"
    rows = []
    for family, g in recent.groupby(family_col, dropna=False):
        rets = pd.to_numeric(g["net_return"], errors="coerce").dropna()
        rows.append(
            {
                "family": str(family),
                "trade_count": int(len(g)),
                "pnl_sum": float(pd.to_numeric(g["pnl"], errors="coerce").fillna(0).sum()),
                "win_rate": float((rets > 0).mean()) if not rets.empty else None,
                "avg_return": float(rets.mean()) if not rets.empty else None,
                "best_return": float(rets.max()) if not rets.empty else None,
                "worst_return": float(rets.min()) if not rets.empty else None,
            }
        )
    return pd.DataFrame(rows).sort_values("pnl_sum", ascending=False)


def _overlap(g2: pd.DataFrame, g3: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    g2r = g2[g2["entry_date"].ge(pd.Timestamp(RECENT_START))].copy()
    g3r = g3[g3["entry_date"].ge(pd.Timestamp(RECENT_START))].copy()

    shared = g2r.merge(
        g3r[["key", "route", "net_return", "pnl", "capital", "exit_date"]],
        on="key",
        how="inner",
        suffixes=("_g2", "_g3"),
    )
    shared["return_gap_g2_minus_g3"] = pd.to_numeric(shared["net_return_g2"], errors="coerce") - pd.to_numeric(
        shared["net_return_g3"], errors="coerce"
    )
    shared["pnl_gap_g2_minus_g3"] = pd.to_numeric(shared["pnl_g2"], errors="coerce") - pd.to_numeric(
        shared["pnl_g3"], errors="coerce"
    )

    g2_only = g2r[~g2r["key"].isin(set(g3r["key"]))].copy().sort_values("pnl", ascending=False)
    g3_only = g3r[~g3r["key"].isin(set(g2r["key"]))].copy().sort_values("pnl", ascending=False)
    return shared.sort_values("pnl_gap_g2_minus_g3", ascending=False), g2_only, g3_only


def _pct(value: Any) -> str:
    try:
        if value is None or pd.isna(value):
            return "--"
        n = float(value)
        return f"{n * 100:+.2f}%"
    except Exception:
        return "--"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    g2_curve = _read_csv(G2_CURVE)
    g2_trades_raw = _read_csv(G2_TRADES)
    g2_signals = _read_csv(G2_SIGNALS)
    g3_curve = _read_csv(G3_CURVE)
    g3_trades_raw = _read_csv(G3_TRADES)

    g2_trades = _prepare_g2_trades(g2_trades_raw, g2_signals)
    g3_trades = _prepare_g3_trades(g3_trades_raw)

    window_rows = pd.DataFrame(
        [
            {"strategy": "G2_full", **_curve_window(g2_curve, RECENT_START)},
            {"strategy": "G3_V3", **_curve_window(g3_curve, RECENT_START)},
        ]
    )
    annual = pd.concat(
        [
            _annual(g2_curve).assign(strategy="G2_full"),
            _annual(g3_curve).assign(strategy="G3_V3"),
        ],
        ignore_index=True,
    )
    monthly = pd.concat([_monthly(g2_curve, "G2_full"), _monthly(g3_curve, "G3_V3")], ignore_index=True)
    trade_summary = pd.DataFrame([_trade_summary(g2_trades, "G2_full"), _trade_summary(g3_trades, "G3_V3")])
    family_summary = _family_summary(g2_trades)
    shared, g2_only, g3_only = _overlap(g2_trades, g3_trades)

    top_g2_winners = g2_trades[g2_trades["entry_date"].ge(pd.Timestamp(RECENT_START))].nlargest(20, "pnl")
    top_g3_winners = g3_trades[g3_trades["entry_date"].ge(pd.Timestamp(RECENT_START))].nlargest(20, "pnl")

    outputs = {
        "window_compare": window_rows,
        "annual_compare": annual,
        "monthly_compare": monthly,
        "trade_summary": trade_summary,
        "g2_family_summary": family_summary,
        "shared_trades": shared,
        "g2_only_trades": g2_only,
        "g3_only_trades": g3_only,
        "top_g2_winners": top_g2_winners,
        "top_g3_winners": top_g3_winners,
    }
    for name, df in outputs.items():
        df.to_csv(OUT_DIR / f"{name}.csv", index=False, encoding="utf-8-sig")

    g2_recent = window_rows[window_rows["strategy"].eq("G2_full")].iloc[0].to_dict()
    g3_recent = window_rows[window_rows["strategy"].eq("G3_V3")].iloc[0].to_dict()
    gap = None
    if g2_recent.get("return") is not None and g3_recent.get("return") is not None:
        gap = float(g2_recent["return"]) - float(g3_recent["return"])

    manifest = {
        "status": "completed",
        "recent_start": RECENT_START,
        "g2_source": {
            "summary": str(G2_SUMMARY.relative_to(ROOT)),
            "curve": str(G2_CURVE.relative_to(ROOT)),
            "trades": str(G2_TRADES.relative_to(ROOT)),
            "signals": str(G2_SIGNALS.relative_to(ROOT)),
            "summary_json": _read_json(G2_SUMMARY),
        },
        "g3_source": {
            "summary": str(G3_SUMMARY.relative_to(ROOT)),
            "curve": str(G3_CURVE.relative_to(ROOT)),
            "trades": str(G3_TRADES.relative_to(ROOT)),
            "summary_json": _read_json(G3_SUMMARY),
        },
        "recent_return_gap_g2_minus_g3": gap,
        "shared_trade_count": int(len(shared)),
        "g2_only_trade_count": int(len(g2_only)),
        "g3_only_trade_count": int(len(g3_only)),
        "outputs": {name: f"{name}.csv" for name in outputs},
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# G3 V3 与 G2 full 收益差距归因",
        "",
        f"- 对比窗口：{RECENT_START} 至各自产物末日。",
        f"- G2 full 窗口收益：{_pct(g2_recent.get('return'))}，回撤：{_pct(g2_recent.get('max_drawdown'))}，交易：{int(trade_summary.loc[trade_summary['strategy'].eq('G2_full'), 'trade_count'].iloc[0])}。",
        f"- G3 V3 窗口收益：{_pct(g3_recent.get('return'))}，回撤：{_pct(g3_recent.get('max_drawdown'))}，交易：{int(trade_summary.loc[trade_summary['strategy'].eq('G3_V3'), 'trade_count'].iloc[0])}。",
        f"- 收益差距 G2-G3：{_pct(gap)}。",
        "",
        "## 初步判断",
        "",
        f"- 同日同股交集交易：{len(shared)} 笔。",
        f"- G2 有、G3 没有的交易：{len(g2_only)} 笔；这是优先研究的强势漏失池。",
        f"- G3 有、G2 没有的交易：{len(g3_only)} 笔；这是需要确认是否稀释攻击力的防守/震荡池。",
        "",
        "## 下一步",
        "",
        "- 逐笔检查 `g2_only_trades.csv` 的前 20 个盈利样本，判断 G3 是因为市场风格门控、候选源、排序、执行退出还是仓位模型漏掉。",
        "- 把 G2 强势样本按 `signal_family/g2_v2_buy_logic/sector_strong` 拆成可迁移规则，先做强势链路增强，不动 down/range 防守链路。",
    ]
    (OUT_DIR / "gap_attribution_report_cn.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
