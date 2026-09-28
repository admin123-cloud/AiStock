from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.gen3_build_dynamic_router_combo_v1 as router
import scripts.gen3_test_strong_quality_filter_v1 as quality
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "gen3_strong_quality_filter_execution_stress_v1"
INITIAL_CAPITAL = 150_000.0

PROFILES = [
    {"profile": "cost30_base", "cost_bps": 30.0, "tail_shock": 0.0, "limitdown_delay": False},
    {"profile": "cost50_base", "cost_bps": 50.0, "tail_shock": 0.0, "limitdown_delay": False},
    {"profile": "cost100_base", "cost_bps": 100.0, "tail_shock": 0.0, "limitdown_delay": False},
    {"profile": "cost30_tail2pct", "cost_bps": 30.0, "tail_shock": 0.02, "limitdown_delay": False},
    {"profile": "cost30_limitdown_delay", "cost_bps": 30.0, "tail_shock": 0.0, "limitdown_delay": True},
    {"profile": "cost50_limitdown_tail2pct", "cost_bps": 50.0, "tail_shock": 0.02, "limitdown_delay": True},
]

VARIANTS = [
    {"variant": "base_plusweak", "filter": "none"},
    {"variant": "veto_l3_s3_ge50", "filter": "l3_s3_lt50_or_missing"},
]


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _load_daily_ohlc(candidates: pd.DataFrame) -> pd.DataFrame:
    codes = sorted({str(c) for c in candidates["code"].dropna().tolist() if re.fullmatch(r"[0-9A-Z.]+", str(c))})
    if not codes:
        return pd.DataFrame()
    start = pd.to_datetime(candidates["entry_date"], errors="coerce").min() - pd.Timedelta(days=20)
    end = pd.to_datetime(candidates["policy_exit_date"], errors="coerce").max() + pd.Timedelta(days=20)
    code_list = ",".join(_sql_literal(code) for code in codes)
    sql = f"""
    SELECT code, trade_date, open, high, low, close
    FROM kline_daily
    WHERE code IN ({code_list})
      AND trade_date BETWEEN toDate({_sql_literal(start.strftime('%Y-%m-%d'))})
      AND toDate({_sql_literal(end.strftime('%Y-%m-%d'))})
    ORDER BY code, trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "close"]).copy()
    d["prev_close"] = d.groupby("code")["close"].shift(1)
    d["is_limitdown_proxy"] = d["prev_close"].gt(0) & (d["close"] / d["prev_close"] - 1.0).le(-0.095)
    d["next_trade_date"] = d.groupby("code")["trade_date"].shift(-1)
    d["next_open"] = d.groupby("code")["open"].shift(-1)
    return d


def _apply_profile(candidates: pd.DataFrame, ohlc: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["stress_profile"] = profile["profile"]
    d["execution_delay_days"] = 0
    d["limitdown_delayed"] = False
    d["tail_shock_applied"] = False

    extra_cost = (float(profile["cost_bps"]) - router.SOURCE_COST_BPS) / 10000.0
    d["stress_net_ret"] = d["policy_net_ret"] - extra_cost

    if float(profile.get("tail_shock", 0.0)) > 0:
        shock = float(profile["tail_shock"])
        # Tail shock proxies next-day open impact for exits that cannot be completed cleanly at planned close.
        d["stress_net_ret"] = d["stress_net_ret"] - shock
        d["tail_shock_applied"] = True

    if profile.get("limitdown_delay") and not ohlc.empty:
        key_cols = ["code", "trade_date", "is_limitdown_proxy", "next_trade_date", "next_open"]
        daily = ohlc[key_cols].rename(columns={"trade_date": "policy_exit_date"})
        d = d.merge(daily, on=["code", "policy_exit_date"], how="left")
        mask = d["is_limitdown_proxy"].fillna(False) & d["next_open"].notna() & d["entry_price"].gt(0)
        delayed_ret = d.loc[mask, "next_open"] / d.loc[mask, "entry_price"] - 1.0 - float(profile["cost_bps"]) / 10000.0
        if float(profile.get("tail_shock", 0.0)) > 0:
            delayed_ret = delayed_ret - float(profile["tail_shock"])
        d.loc[mask, "stress_net_ret"] = delayed_ret
        d.loc[mask, "execution_delay_days"] = (
            pd.to_datetime(d.loc[mask, "next_trade_date"], errors="coerce") - d.loc[mask, "policy_exit_date"]
        ).dt.days.fillna(0).astype(int)
        d.loc[mask, "limitdown_delayed"] = True
        d = d.drop(columns=[c for c in ["is_limitdown_proxy", "next_trade_date", "next_open"] if c in d.columns])

    d["policy_net_ret_original"] = d["policy_net_ret"]
    d["policy_net_ret"] = d["stress_net_ret"]
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    old_limits = dict(router.ROUTE_DAILY_LIMIT)
    try:
        router.ROUTE_DAILY_LIMIT = dict(quality.ROUTE_DAILY_LIMIT)
        return router.simulate(candidates, router.SOURCE_COST_BPS)
    finally:
        router.ROUTE_DAILY_LIMIT = old_limits


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    recent = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    return {
        "variant": variant,
        "profile": profile,
        "trade_count": int(len(closed)),
        "strong_trade_count": int((closed.get("route", pd.Series(dtype=str)).astype(str) == "strong_main").sum()) if not closed.empty else 0,
        "limitdown_delayed_trades": int(closed.get("limitdown_delayed", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()) if not closed.empty else 0,
        "tail_shock_trades": int(closed.get("tail_shock_applied", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()) if not closed.empty else 0,
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(curve["equity"]),
        "recent_return": float(recent["equity"].iloc[-1] / recent["equity"].iloc[0] - 1.0) if not recent.empty else None,
        "recent_max_drawdown": _max_drawdown(recent["equity"]) if not recent.empty else None,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
    }


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


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


def _write_report(summary: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "recent_return",
        "recent_max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
    }
    lines = [
        "# G3 strong 质量过滤执行压力测试 v1",
        "",
        "## 边界",
        "",
        "- 比较 `base_plusweak` 与 `veto_l3_s3_ge50`。",
        "- 跌停不可卖为日线代理：计划退出日收盘相对前收 <= -9.5% 时，延迟到下一交易日开盘。",
        "- 尾盘/次日冲击为额外 -2% 压力，不代表真实成交价。",
        "- 该测试仍是研究口径，不接实盘。",
        "",
        "## 压力结果",
        "",
        _md_table(
            summary[
                [
                    "variant",
                    "profile",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "trade_count",
                    "strong_trade_count",
                    "limitdown_delayed_trades",
                    "tail_shock_trades",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "worst_open_mtm_ret",
                ]
            ],
            pct_cols=pct_cols,
        ),
        "",
        "## 判断",
        "",
        "- 若 `veto_l3_s3_ge50` 在多数压力下仍优于 base，说明该过滤不是单一成本假设下的偶然结果。",
        "- 若 100bps 或 -2% 冲击下收益大幅塌陷，下一步必须先降低执行敏感性，再考虑页面候选升级。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for variant in VARIANTS:
        base_candidates = quality._load_candidates(variant["filter"])
        ohlc = _load_daily_ohlc(base_candidates)
        for profile in PROFILES:
            stressed = _apply_profile(base_candidates, ohlc, profile)
            curve, closed = _simulate(stressed)
            stem = f"{variant['variant']}__{profile['profile']}"
            out_dir = OUT_DIR / stem
            out_dir.mkdir(parents=True, exist_ok=True)
            stressed.to_csv(out_dir / "candidates_stressed.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(out_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(out_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            router.summarize_windows(curve, closed).to_csv(out_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
            router.summarize_routes(closed).to_csv(out_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
            rows.append(_summary(curve, closed, variant["variant"], profile["profile"]))
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "stress_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(
        json.dumps({"status": "completed", "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_report(summary)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
