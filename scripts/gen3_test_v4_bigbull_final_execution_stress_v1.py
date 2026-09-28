from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = (
    ROOT
    / "reports"
    / "gen3_v4_bigbull_four_state_gate_combo_v1"
    / "g3_plus_bigbull_uptrend_or_weak_rebound_quarter_d1_weak_full_exit_candidates.csv"
)
OUT_DIR = ROOT / "reports" / "gen3_v4_bigbull_final_execution_stress_v1"
BASE_COST_BPS = 30.0


PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "all_shock": 0.0, "bigbull_shock": 0.0, "limit_delay": False},
    {"profile": "cost50", "cost_bps": 50.0, "all_shock": 0.0, "bigbull_shock": 0.0, "limit_delay": False},
    {"profile": "cost100", "cost_bps": 100.0, "all_shock": 0.0, "bigbull_shock": 0.0, "limit_delay": False},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "all_shock": 0.02, "bigbull_shock": 0.0, "limit_delay": False},
    {"profile": "cost30_bigbull_shock2", "cost_bps": 30.0, "all_shock": 0.0, "bigbull_shock": 0.02, "limit_delay": False},
    {"profile": "cost30_limit_delay", "cost_bps": 30.0, "all_shock": 0.0, "bigbull_shock": 0.0, "limit_delay": True},
    {"profile": "cost50_limit_delay", "cost_bps": 50.0, "all_shock": 0.0, "bigbull_shock": 0.0, "limit_delay": True},
    {"profile": "cost100_limit_delay", "cost_bps": 100.0, "all_shock": 0.0, "bigbull_shock": 0.0, "limit_delay": True},
]


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "position_scale", "score", "route_priority"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def load_daily_for_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(candidates["code"].dropna().astype(str).unique().tolist())
    start = candidates["entry_date"].min() - pd.Timedelta(days=10)
    end = candidates["policy_exit_date"].max() + pd.Timedelta(days=20)
    parts: list[pd.DataFrame] = []
    for ci in range(0, len(codes), 250):
        quoted = ",".join(_sql_literal(code) for code in codes[ci : ci + 250])
        sql = f"""
        SELECT code, trade_date, open, high, low, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({_sql_literal(start.strftime('%Y-%m-%d'))})
                             AND toDate({_sql_literal(end.strftime('%Y-%m-%d'))})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["code"] = daily["code"].astype(str)
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])
    daily["prev_close"] = daily.groupby("code")["close"].shift(1)
    daily["next_trade_date"] = daily.groupby("code")["trade_date"].shift(-1)
    daily["next_close"] = daily.groupby("code")["close"].shift(-1)
    daily["next_open"] = daily.groupby("code")["open"].shift(-1)
    daily["is_limit_down_proxy"] = daily["close"].le(daily["prev_close"] * 0.905)
    return daily


def attach_limit_delay(candidates: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        out = candidates.copy()
        out["limit_delay_blocked"] = False
        out["limit_delay_ret_delta"] = 0.0
        return out
    exit_daily = daily[["code", "trade_date", "close", "next_close", "is_limit_down_proxy"]].rename(
        columns={"trade_date": "policy_exit_date", "close": "exit_close"}
    )
    out = candidates.merge(exit_daily, on=["code", "policy_exit_date"], how="left")
    out["limit_delay_blocked"] = out["is_limit_down_proxy"].fillna(False).astype(bool) & out["next_close"].notna() & out["exit_close"].gt(0)
    out["limit_delay_ret_delta"] = 0.0
    mask = out["limit_delay_blocked"]
    out.loc[mask, "limit_delay_ret_delta"] = out.loc[mask, "next_close"] / out.loc[mask, "exit_close"] - 1.0
    return out


def apply_profile(candidates: pd.DataFrame, profile: dict[str, Any], daily: pd.DataFrame) -> pd.DataFrame:
    d = candidates.copy()
    d["stress_profile"] = profile["profile"]
    extra_cost = (float(profile["cost_bps"]) - BASE_COST_BPS) / 10000.0
    route = d["route"].astype(str)
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost - float(profile.get("all_shock", 0.0))
    bigbull = route.eq("strong_bigbull_struct_gate")
    d.loc[bigbull, "policy_net_ret"] = d.loc[bigbull, "policy_net_ret"] - float(profile.get("bigbull_shock", 0.0))
    if bool(profile.get("limit_delay", False)):
        d = attach_limit_delay(d, daily)
        d["policy_net_ret"] = d["policy_net_ret"] + pd.to_numeric(d["limit_delay_ret_delta"], errors="coerce").fillna(0.0)
    else:
        d["limit_delay_blocked"] = False
        d["limit_delay_ret_delta"] = 0.0
    return d


def route_summary(closed: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for route, g in closed.groupby("route", dropna=False):
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "route": str(route),
                "trade_count": int(len(g)),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "pnl": float(pd.to_numeric(g.get("realized_pnl"), errors="coerce").sum()) if "realized_pnl" in g.columns else 0.0,
                "limit_delay_blocked": int(g.get("limit_delay_blocked", pd.Series(False, index=g.index)).fillna(False).astype(bool).sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("pnl", ascending=False)


def annual_summary(closed: pd.DataFrame, variant: str, profile: str) -> pd.DataFrame:
    d = closed.copy()
    d["entry_year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    rows: list[dict[str, Any]] = []
    for year, g in d.groupby("entry_year"):
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        big = g[g["route"].astype(str).eq("strong_bigbull_struct_gate")]
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "year": int(year),
                "trade_count": int(len(g)),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "realized_pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
                "bigbull_trades": int(len(big)),
                "bigbull_pnl": float(pd.to_numeric(big.get("realized_pnl"), errors="coerce").sum()) if not big.empty else 0.0,
            }
        )
    return pd.DataFrame(rows)


def d1_exit_visibility(candidates: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    big = candidates[candidates["route"].astype(str).eq("strong_bigbull_struct_gate")].copy()
    if big.empty:
        return pd.DataFrame()
    calendar = _trade_calendar(big["entry_date"].min(), big["entry_date"].max() + pd.Timedelta(days=10))
    next_map = {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}
    big["d1_date"] = big["entry_date"].map(next_map)
    daily_small = daily[["code", "trade_date", "prev_close", "open", "low", "close", "is_limit_down_proxy"]].rename(columns={"trade_date": "d1_date"})
    out = big.merge(daily_small, on=["code", "d1_date"], how="left")
    out["d1_1030_exit_visible"] = out["d1_1030_exit_ret"].notna()
    out["d1_limit_down_proxy"] = out["is_limit_down_proxy"].fillna(False).astype(bool)
    out["d1_weak_exit_triggered"] = out.get("reduce_triggered", False)
    cols = [
        "entry_date",
        "d1_date",
        "code",
        "name",
        "market_style_d1",
        "entry_price",
        "policy_net_ret",
        "base_policy_ret",
        "d1_1030_exit_ret",
        "d1_1030_weak",
        "reduce_triggered",
        "d1_1030_exit_visible",
        "d1_limit_down_proxy",
        "open",
        "low",
        "close",
    ]
    return out[[c for c in cols if c in out.columns]].copy()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    daily = load_daily_for_candidates(candidates)
    summaries: list[dict[str, Any]] = []
    route_frames: list[pd.DataFrame] = []
    annual_frames: list[pd.DataFrame] = []
    variant = "uptrend_or_weak_rebound_quarter_d1_weak_full_exit"
    candidates.to_csv(OUT_DIR / "base_candidates.csv", index=False, encoding="utf-8-sig")
    d1_vis = d1_exit_visibility(candidates, daily)
    d1_vis.to_csv(OUT_DIR / "bigbull_d1_exit_visibility.csv", index=False, encoding="utf-8-sig")
    for profile in PROFILES:
        prof = apply_profile(candidates, profile, daily)
        run_dir = OUT_DIR / str(profile["profile"])
        run_dir.mkdir(parents=True, exist_ok=True)
        prof.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        curve, closed = simulate_scaled(prof, float(profile["cost_bps"]))
        curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        item = summarize(curve, closed, variant, str(profile["profile"]))
        item["limit_delay_blocked_trades"] = int(closed.get("limit_delay_blocked", pd.Series(False, index=closed.index)).fillna(False).astype(bool).sum())
        summaries.append(item)
        rs = route_summary(closed)
        rs.insert(0, "profile", str(profile["profile"]))
        route_frames.append(rs)
        annual_frames.append(annual_summary(closed, variant, str(profile["profile"])))

    summary = pd.DataFrame(summaries)
    route_df = pd.concat(route_frames, ignore_index=True) if route_frames else pd.DataFrame()
    annual = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "route_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "strong_main_avg_ret",
        "strong_bigbull_struct_gate_avg_ret",
        "avg_ret",
    }
    lines = [
        "# G3 V4 big_bull 四态候选最终执行压力测试 v1",
        "",
        "## 本轮完成",
        "",
        "- 候选版本：`uptrend_or_weak_rebound + quarter + D1 10:30弱全退`。",
        "- 压力口径：30/50/100bps、全交易额外 2% 冲击、big_bull 单独 2% 冲击、退出日跌停不可卖延迟到下一交易日收盘代理。",
        "- D1 10:30 弱退出只作为可见性审计输出，不提前释放仓位复用。",
        "",
        "## 汇总",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 路由贡献",
        "",
        md_table(route_df, pct_cols={"avg_ret", "win_rate"}),
        "",
        "## 年度贡献",
        "",
        md_table(annual, pct_cols={"avg_ret", "win_rate"}),
        "",
        "## D1 10:30 退出可见性",
        "",
        md_table(d1_vis, pct_cols={"policy_net_ret", "base_policy_ret", "d1_1030_exit_ret"}),
        "",
        "## 下一步目标",
        "",
        "- 如果 100bps、all_shock2 或 limit_delay 任一口径明显打穿 base，big_bull 仍只能保留研究。",
        "- 如果压力仍优于 base，下一步再做真实 30m bar 延迟一根成交和成交量容量审计。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"done: {OUT_DIR}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
