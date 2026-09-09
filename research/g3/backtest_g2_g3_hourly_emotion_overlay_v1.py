from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_available, clickhouse_query_df, clickhouse_table_exists
from utils.paths import report_path


REPORT_ROOT = report_path()
OUT_DIR = report_path("g2_g3_hourly_emotion_overlay_v1")

G2_DIR = report_path("gen2_v2_complete_strategy")
G2_FULL_TRADES = G2_DIR / "runs" / "official" / "full" / "trades.csv"
G2_BLIND_TRADES = G2_DIR / "runs" / "official" / "blind_2026ytd" / "trades.csv"
G2_SUMMARY = G2_DIR / "summary.json"
G2_SOURCE = report_path("gen2_v2_complete_strategy_2020", "sources", "g2_v2_complete.csv")

G3_DIR = report_path("gen3_v4_research_package_v1")
G3_BASE = G3_DIR / "v4_h10_margin__cost30" / "closed_trades.csv"
G3_SUMMARY = G3_DIR / "g3_v4_research_summary.csv"

INITIAL_CAPITAL = 150_000.0
DAILY_OPEN_LIMIT = 1
OVERLAP_START = pd.Timestamp("2024-09-26")
OVERLAP_END = pd.Timestamp("2026-06-12")

WINDOWS = {
    "full_overlap": ("2024-09-26", "2026-06-12"),
    "valid_2025": ("2025-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-06-12"),
}


def pct(v: Any) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def num(v: Any, digits: int = 2) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.{digits}f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif col in money_cols:
                item[col] = f"{float(value):,.0f}" if pd.notna(value) else ""
            elif isinstance(value, float):
                item[col] = num(value, 4)
            else:
                item[col] = "" if pd.isna(value) else value
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return pd.read_csv(path, low_memory=False)


def load_g2_trades() -> pd.DataFrame:
    if not G2_FULL_TRADES.exists():
        raise FileNotFoundError("missing G2 official trades")
    trades = read_csv(G2_FULL_TRADES)
    trades["window_source"] = "official_full"
    trades["buy_datetime"] = pd.to_datetime(trades["buy_datetime"], errors="coerce")
    trades["sell_datetime"] = pd.to_datetime(trades["sell_datetime"], errors="coerce")
    trades["buy_date"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.normalize()
    trades["sell_date"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.normalize()
    for col in ["capital", "return", "pnl", "v4_rank", "v4_score"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    key_cols = ["code", "name", "buy_date", "buy_datetime", "buy_price", "v4_rank", "v4_score"]
    lots = []
    leg_rows = []
    for key, g in trades.groupby(key_cols, dropna=False, sort=False):
        row = dict(zip(key_cols, key))
        g = g.sort_values("sell_datetime").copy()
        open_capital = float(pd.to_numeric(g["capital"], errors="coerce").sum())
        if open_capital <= 0:
            open_capital = float(pd.to_numeric(g["capital"], errors="coerce").iloc[0])
        candidate_key = f"g2|{row['code']}|{pd.Timestamp(row['buy_datetime']).isoformat()}"
        row.update(
            {
                "candidate_key": candidate_key,
                "entry_datetime": pd.Timestamp(row["buy_datetime"]),
                "entry_date": pd.Timestamp(row["buy_date"]),
                "policy_exit_date": pd.Timestamp(g["sell_date"].max()),
                "route": "g2",
                "route_rank": 0,
                "rank_key": 10000.0 - float(row.get("v4_rank") if pd.notna(row.get("v4_rank")) else 9999),
                "source_open_capital": open_capital,
                "source_family": "g2_official",
            }
        )
        lots.append(row)
        for idx, (_, leg) in enumerate(g.iterrows()):
            cap = float(leg.get("capital") or 0)
            leg_rows.append(
                {
                    "candidate_key": candidate_key,
                    "leg_no": idx + 1,
                    "exit_date": pd.Timestamp(leg["sell_date"]),
                    "capital_ratio": cap / open_capital if open_capital else 1.0,
                    "net_return": float(leg.get("return") or 0.0),
                    "exit_reason": str(leg.get("exit_reason") or ""),
                }
            )
    return pd.DataFrame(lots), pd.DataFrame(leg_rows)


def load_g2_context() -> pd.DataFrame:
    if not G2_SOURCE.exists():
        return pd.DataFrame()
    src = read_csv(G2_SOURCE)
    if "confirm_datetime" not in src.columns:
        return pd.DataFrame()
    src["confirm_datetime"] = pd.to_datetime(src["confirm_datetime"], errors="coerce")
    src["entry_date"] = pd.to_datetime(src["entry_date"], errors="coerce").dt.normalize()
    keep = [
        "code",
        "entry_date",
        "confirm_datetime",
        "source_family",
        "signal_family",
        "g2_v2_buy_logic",
        "rt_confirm_hour",
        "l3_s3",
        "l2_s3",
        "l3_rt_rise_ratio",
        "sector_strong",
        "l2_sector_name",
        "l3_sector_name",
    ]
    return src[[c for c in keep if c in src.columns]].dropna(subset=["code", "entry_date"]).copy()


def enrich_g2(lots: pd.DataFrame) -> pd.DataFrame:
    ctx = load_g2_context()
    if ctx.empty:
        return lots.copy()
    d = lots.merge(ctx, on=["code", "entry_date"], how="left", suffixes=("", "_ctx"))
    d["confirm_datetime"] = d["confirm_datetime"].fillna(d["entry_datetime"])
    d["source_family"] = d.get("source_family_ctx", d["source_family"]).fillna(d["source_family"])
    return d


def load_g3_trades() -> pd.DataFrame:
    g3 = read_csv(G3_BASE)
    g3["entry_date"] = pd.to_datetime(g3["entry_date"], errors="coerce").dt.normalize()
    g3["policy_exit_date"] = pd.to_datetime(g3["policy_exit_date"], errors="coerce").dt.normalize()
    g3["policy_net_ret"] = pd.to_numeric(g3["policy_net_ret"], errors="coerce")
    g3["score"] = pd.to_numeric(g3.get("score", 0), errors="coerce").fillna(0)
    out = g3.dropna(subset=["entry_date", "policy_exit_date", "policy_net_ret"]).copy()
    out["candidate_key"] = out.apply(lambda r: f"g3|{r['route']}|{r['code']}|{r['entry_date'].date()}", axis=1)
    out["entry_datetime"] = out["entry_date"] + pd.Timedelta(hours=10)
    out["route_rank"] = out["route"].map({"strong": 0, "weak": 1, "range": 2, "down_panic": 3}).fillna(5)
    out["rank_key"] = out["score"]
    out["source_family"] = out["route"].astype(str)
    out = out.rename(columns={"policy_net_ret": "raw_net_return"})
    return out[out["entry_date"].between(OVERLAP_START, OVERLAP_END)].copy()


def g3_legs(g3: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "candidate_key": g3["candidate_key"],
            "leg_no": 1,
            "exit_date": g3["policy_exit_date"],
            "capital_ratio": 1.0,
            "net_return": pd.to_numeric(g3["raw_net_return"], errors="coerce"),
            "exit_reason": "g3_policy_exit",
        }
    )


def load_hourly_emotion() -> pd.DataFrame:
    if not clickhouse_available() or not clickhouse_table_exists("kline_minute_60"):
        return pd.DataFrame()
    sql = """
        SELECT
            toStartOfHour(k.datetime) AS hour_time,
            avg(if(k.close > k.open, 1, 0)) AS hour_up_rate,
            avg(if(k.open > 0 AND (k.close - k.open) / k.open > 0.03, 1, 0)) AS hour_strong_rate,
            avg(if(k.open > 0 AND (k.close - k.open) / k.open < -0.03, 1, 0)) AS hour_weak_rate,
            count() AS total_rows
        FROM kline_minute_60 k
        ANY LEFT JOIN stocks s ON s.code = k.code
        WHERE s.type = 'stock'
          AND toHour(k.datetime) IN (10, 11, 13, 14, 15)
        GROUP BY hour_time
        HAVING total_rows >= 100
        ORDER BY hour_time
    """
    d = clickhouse_query_df(sql)
    if d is None or d.empty:
        return pd.DataFrame()
    d["hour_time"] = pd.to_datetime(d["hour_time"], errors="coerce")
    d = d.dropna(subset=["hour_time"]).sort_values("hour_time").copy()
    d["hour_date"] = d["hour_time"].dt.normalize()
    d["hour_up_rate"] = pd.to_numeric(d["hour_up_rate"], errors="coerce")
    d["hour_strong_rate"] = pd.to_numeric(d["hour_strong_rate"], errors="coerce")
    d["hour_weak_rate"] = pd.to_numeric(d["hour_weak_rate"], errors="coerce")
    d["up_rank_20"] = d["hour_up_rate"].rolling(20, min_periods=5).rank(pct=True)
    d["prev_hour_up_rate"] = d["hour_up_rate"].shift(1)
    d["delta_hour_up_rate"] = d["hour_up_rate"] - d["prev_hour_up_rate"]
    d["hour_phase"] = "neutral"
    d.loc[d["hour_up_rate"].le(0.25), "hour_phase"] = "ice"
    d.loc[d["hour_up_rate"].between(0.25, 0.45, inclusive="right"), "hour_phase"] = "repair_low"
    d.loc[d["hour_up_rate"].between(0.45, 0.65, inclusive="right"), "hour_phase"] = "balanced"
    d.loc[d["hour_up_rate"].gt(0.65), "hour_phase"] = "hot"
    d["hour_filter_pass"] = (
        d["hour_phase"].isin(["ice", "repair_low", "balanced"])
        & d["delta_hour_up_rate"].fillna(0).ge(-0.12)
    )
    return d


def attach_hourly(candidates: pd.DataFrame, hourly: pd.DataFrame) -> pd.DataFrame:
    if hourly.empty:
        d = candidates.copy()
        d["hour_filter_pass"] = True
        d["hour_phase"] = "missing_hourly"
        return d
    h = hourly.copy()
    h["join_time"] = h["hour_time"]
    d = candidates.copy()
    d["entry_datetime"] = pd.to_datetime(d["entry_datetime"], errors="coerce")
    d = pd.merge_asof(
        d.sort_values("entry_datetime"),
        h.sort_values("join_time"),
        left_on="entry_datetime",
        right_on="join_time",
        direction="backward",
        tolerance=pd.Timedelta(hours=4),
    )
    d["hour_filter_pass"] = d["hour_filter_pass"].fillna(False).astype(bool)
    d["hour_phase"] = d["hour_phase"].fillna("missing_hourly")
    return d


def benchmark_curve(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not clickhouse_available() or not clickhouse_table_exists("kline_daily"):
        return pd.DataFrame()
    df = clickhouse_query_df(
        """
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = '999999.SH'
          AND trade_date >= ?
          AND trade_date <= ?
        ORDER BY trade_date
        """,
        [pd.Timestamp(start).strftime("%Y-%m-%d"), pd.Timestamp(end).strftime("%Y-%m-%d")],
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).copy()
    df["benchmark_return"] = df["close"] / df["close"].iloc[0] - 1.0
    return df[["date", "close", "benchmark_return"]]


def route_candidates(g2: pd.DataFrame, g3: pd.DataFrame, variant: str, hourly: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    if variant == "g2_only":
        return g2.copy(), "G2 当前正式 30m 买点"
    if variant == "g3_only":
        return g3.copy(), "G3 V4 组合包 h10 margin cost30"
    if variant == "g2_g3_router":
        d = pd.concat([g2, g3], ignore_index=True, sort=False)
    elif variant == "g2_g3_hourly_filter":
        d = pd.concat([g2, g3], ignore_index=True, sort=False)
        d = attach_hourly(d, hourly)
        # G2 强势买点在小时冰点/修复/均衡且不继续恶化时允许；G3 panic 不被该过滤器拦截。
        d = d[(d["route"].eq("down_panic")) | (d["hour_filter_pass"])].copy()
    else:
        raise ValueError(variant)
    d = d.sort_values(["entry_date", "route_rank", "rank_key"], ascending=[True, True, False])
    return d.groupby("entry_date", as_index=False).head(1).copy(), (
        "G2 优先，G3 补位" if variant == "g2_g3_router" else "G2/G3 + 首页小时情绪过滤"
    )


def stake_weight(route: str) -> float:
    if route == "g2":
        return 0.50
    if route == "strong":
        return 0.30
    return 0.20


def simulate(candidates: pd.DataFrame, legs: pd.DataFrame, start: pd.Timestamp = OVERLAP_START, end: pd.Timestamp = OVERLAP_END) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    candidates = candidates.dropna(subset=["entry_date", "policy_exit_date"]).copy()
    cal = [pd.Timestamp(d).normalize() for d in pd.bdate_range(start, end)]
    by_day = {pd.Timestamp(k).normalize(): g.copy() for k, g in candidates.groupby("entry_date")}
    leg_map = {k: g.sort_values("exit_date").copy() for k, g in legs.groupby("candidate_key", sort=False)}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    curve_rows = []
    closed_rows = []
    for day in cal:
        still = []
        realized = 0.0
        for pos in open_pos:
            remain_events = []
            for ev in pos["events"]:
                if pd.Timestamp(ev["exit_date"]).normalize() <= day:
                    principal = pos["remaining_principal_base"] * float(ev["capital_ratio"])
                    proceeds = principal * (1.0 + float(ev["net_return"]))
                    cash += proceeds
                    pnl = proceeds - principal
                    realized += pnl
                    out = {k: v for k, v in pos.items() if k != "events"}
                    out.update(
                        {
                            "policy_exit_date": day,
                            "leg_capital_ratio": float(ev["capital_ratio"]),
                            "policy_net_ret": float(ev["net_return"]),
                            "realized_pnl": pnl,
                            "exit_reason": ev.get("exit_reason", ""),
                        }
                    )
                    closed_rows.append(out)
                    pos["remaining_principal"] -= principal
                else:
                    remain_events.append(ev)
            pos["events"] = remain_events
            if pos["remaining_principal"] > 1e-6 and remain_events:
                still.append(pos)
        open_pos = still
        opened = 0
        todays = by_day.get(day)
        if todays is not None:
            for row in todays.sort_values(["route_rank", "rank_key"], ascending=[True, False]).itertuples(index=False):
                if opened >= DAILY_OPEN_LIMIT:
                    break
                route = str(getattr(row, "route"))
                if route == "g2" and sum(1 for p in open_pos if p["route"] == "g2") >= 2:
                    continue
                if route != "g2" and sum(1 for p in open_pos if p["route"] != "g2") >= 5:
                    continue
                equity_before = cash + sum(float(p["remaining_principal"]) for p in open_pos)
                stake = equity_before * stake_weight(route)
                if stake <= 0 or cash < stake:
                    continue
                key = str(getattr(row, "candidate_key"))
                events = leg_map.get(key, pd.DataFrame())
                if events.empty:
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                pos["remaining_principal"] = stake
                pos["remaining_principal_base"] = stake
                pos["events"] = events.to_dict("records")
                cash -= stake
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["remaining_principal"]) for p in open_pos)
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["remaining_principal"]) for p in open_pos),
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized,
            }
        )
    curve = pd.DataFrame(curve_rows)
    closed = pd.DataFrame(closed_rows)
    if not curve.empty:
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
        curve["drawdown"] = curve["equity"] / curve["equity"].cummax() - 1.0
    return curve, closed


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, label: str, bench: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    total = float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0
    bret = 0.0
    if not bench.empty and not curve.empty:
        b = bench[bench["date"].between(curve["date"].min(), curve["date"].max())]
        if not b.empty:
            bret = float(b["close"].iloc[-1] / b["close"].iloc[0] - 1.0)
    return {
        "variant": variant,
        "label": label,
        "position_count": int(closed["candidate_key"].nunique()) if not closed.empty else 0,
        "leg_count": int(len(closed)),
        "total_return": total,
        "benchmark_return": bret,
        "excess_return": total - bret,
        "max_drawdown": max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
        "avg_leg_return": float(ret.mean()) if len(ret) else 0.0,
        "worst_leg": float(ret.min()) if len(ret) else 0.0,
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, bench: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        c = curve[curve["date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not curve.empty else pd.DataFrame()
        t = closed[pd.to_datetime(closed["entry_date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
        ret = pd.to_numeric(t.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        total = float(c["equity"].iloc[-1] / c["equity"].iloc[0] - 1.0) if len(c) else 0.0
        bret = 0.0
        if not bench.empty and len(c):
            b = bench[bench["date"].between(c["date"].min(), c["date"].max())]
            if not b.empty:
                bret = float(b["close"].iloc[-1] / b["close"].iloc[0] - 1.0)
        rows.append(
            {
                "variant": variant,
                "window": name,
                "position_count": int(t["candidate_key"].nunique()) if not t.empty else 0,
                "leg_count": int(len(t)),
                "return": total,
                "benchmark_return": bret,
                "excess_return": total - bret,
                "max_drawdown": max_drawdown(c["equity"]) if len(c) else 0.0,
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return rows


def concentration(closed: pd.DataFrame, variant: str) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    pos = (
        closed.groupby(["candidate_key", "code", "name", "route"], dropna=False)
        .agg(entry_date=("entry_date", "first"), realized_pnl=("realized_pnl", "sum"), avg_leg_return=("policy_net_ret", "mean"))
        .reset_index()
        .sort_values("realized_pnl", ascending=False)
    )
    total = float(pos["realized_pnl"].sum())
    rows = []
    for n in [0, 1, 3, 5, 10]:
        rest = pos.iloc[n:]
        rows.append(
            {
                "variant": variant,
                "exclude_top_n": n,
                "remaining_positions": int(len(rest)),
                "remaining_pnl": float(rest["realized_pnl"].sum()),
                "remaining_pnl_share": float(rest["realized_pnl"].sum() / total) if total else 0.0,
            }
        )
    return pd.DataFrame(rows)


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, routes: pd.DataFrame, hourly_diag: pd.DataFrame, conc: pd.DataFrame, top: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "benchmark_return",
        "excess_return",
        "max_drawdown",
        "win_rate",
        "avg_leg_return",
        "worst_leg",
        "return",
        "avg_return",
        "remaining_pnl_share",
        "policy_net_ret",
        "hour_up_rate",
        "delta_hour_up_rate",
    }
    money_cols = {"pnl", "realized_pnl", "remaining_pnl"}
    lines = [
        "# G2 + G3 结合首页小时情绪周期历史回测 V1",
        "",
        "## 口径",
        "",
        "- G2：`gen2_v2_complete_strategy` 当前正式 30m 交易腿，使用 full + blind_2026ytd 去重后的真实交易输出。",
        "- G3：`gen3_v4_research_package_v1/v4_h10_margin__cost30`，包含 down_panic/range/strong 等 G3 路由。",
        "- 首页小时情绪：按首页接口同口径，从 ClickHouse `kline_minute_60` 聚合每小时上涨率、强势率、弱势率。",
        "- 组合执行：每日最多开 1 笔；G2 单仓 50%、最多 2 个强势仓；G3 单仓 20%-30%、最多 5 个补位仓。",
        "- 基准：上证指数 `999999.SH` 同区间收盘收益。该轮是研究复算，不写入正式 G2/G3 runtime。",
        "",
        "## 总体结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        "",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 路由贡献",
        "",
        md_table(routes, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 小时情绪过滤诊断",
        "",
        md_table(hourly_diag, pct_cols=pct_cols),
        "",
        "## 集中度",
        "",
        md_table(conc, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最大盈利样本",
        "",
        md_table(top, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最大亏损样本",
        "",
        md_table(worst, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 结论",
        "",
        "- 是否存在超额收益，以 `excess_return` 为准；若组合版高于 G2/G3 单独版且回撤未显著恶化，说明 G2/G3 路由有组合价值。",
        "- 小时情绪过滤若提高超额或降低回撤但交易数大幅减少，适合作为仓位/确认层；若收益下降明显，说明首页小时低点不能硬过滤，只能用于排序或节奏。",
        "- 后续若要进入正式候选，应重建真正的 bar-by-bar G2/G3 同源引擎，而不是继续叠加闭合交易结果。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g2_lots, g2_leg_rows = load_g2_trades()
    g2_lots = enrich_g2(g2_lots)
    g3 = load_g3_trades()
    g3_leg_rows = g3_legs(g3)
    hourly = load_hourly_emotion()

    all_legs = pd.concat([g2_leg_rows, g3_leg_rows], ignore_index=True)
    g2_lots = g2_lots[g2_lots["entry_date"].between(OVERLAP_START, OVERLAP_END)].copy()
    g3 = g3[g3["entry_date"].between(OVERLAP_START, OVERLAP_END)].copy()
    g2_leg_rows = g2_leg_rows[g2_leg_rows["candidate_key"].isin(set(g2_lots["candidate_key"]))].copy()
    g3_leg_rows = g3_leg_rows[g3_leg_rows["candidate_key"].isin(set(g3["candidate_key"]))].copy()

    bench = benchmark_curve(OVERLAP_START, OVERLAP_END)

    g2_lots.to_csv(OUT_DIR / "g2_candidates.csv", index=False, encoding="utf-8-sig")
    g3.to_csv(OUT_DIR / "g3_candidates.csv", index=False, encoding="utf-8-sig")
    all_legs.to_csv(OUT_DIR / "exit_legs.csv", index=False, encoding="utf-8-sig")
    hourly.to_csv(OUT_DIR / "hourly_emotion.csv", index=False, encoding="utf-8-sig")

    summary_rows = []
    window_rows = []
    route_parts = []
    conc_parts = []
    closed_by_variant: dict[str, pd.DataFrame] = {}
    variants = ["g2_only", "g3_only", "g2_g3_router", "g2_g3_hourly_filter"]
    for variant in variants:
        candidates, label = route_candidates(g2_lots, g3, variant, hourly)
        candidates.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
        curve, closed = simulate(candidates, all_legs)
        run_dir = OUT_DIR / variant
        run_dir.mkdir(exist_ok=True)
        curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_legs.csv", index=False, encoding="utf-8-sig")
        closed_by_variant[variant] = closed
        summary_rows.append(summarize(curve, closed, variant, label, bench))
        window_rows.extend(window_metrics(curve, closed, variant, bench))
        if not closed.empty:
            route = (
                closed.groupby("route", dropna=False)
                .agg(position_count=("candidate_key", "nunique"), leg_count=("candidate_key", "size"), pnl=("realized_pnl", "sum"), avg_return=("policy_net_ret", "mean"))
                .reset_index()
            )
            route["variant"] = variant
            route_parts.append(route)
            conc_parts.append(concentration(closed, variant))

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    routes = pd.concat(route_parts, ignore_index=True) if route_parts else pd.DataFrame()
    conc = pd.concat(conc_parts, ignore_index=True) if conc_parts else pd.DataFrame()

    hourly_candidates = pd.read_csv(OUT_DIR / "g2_g3_hourly_filter_candidates.csv", low_memory=False)
    if "hour_phase" in hourly_candidates.columns:
        hourly_diag = (
            hourly_candidates.groupby(["route", "hour_phase"], dropna=False)
            .agg(candidates=("candidate_key", "nunique"), hour_up_rate=("hour_up_rate", "mean"), delta_hour_up_rate=("delta_hour_up_rate", "mean"))
            .reset_index()
        )
    else:
        hourly_diag = pd.DataFrame()

    best_variant = summary.sort_values(["excess_return", "total_return"], ascending=False).iloc[0]["variant"] if not summary.empty else ""
    best_closed = closed_by_variant.get(str(best_variant), pd.DataFrame())
    keep = ["entry_date", "policy_exit_date", "code", "name", "route", "source_family", "policy_net_ret", "realized_pnl", "exit_reason"]
    top = best_closed.sort_values("realized_pnl", ascending=False).head(20)[[c for c in keep if c in best_closed.columns]] if not best_closed.empty else pd.DataFrame()
    worst = best_closed.sort_values("policy_net_ret", ascending=True).head(20)[[c for c in keep if c in best_closed.columns]] if not best_closed.empty else pd.DataFrame()

    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "route_contribution.csv", index=False, encoding="utf-8-sig")
    hourly_diag.to_csv(OUT_DIR / "hourly_filter_diagnostics.csv", index=False, encoding="utf-8-sig")
    conc.to_csv(OUT_DIR / "concentration.csv", index=False, encoding="utf-8-sig")
    top.to_csv(OUT_DIR / "best_variant_top_trades.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "best_variant_worst_trades.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, routes, hourly_diag, conc, top, worst)

    payload = {
        "out_dir": str(OUT_DIR),
        "best_variant": str(best_variant),
        "summary": summary.to_dict("records"),
        "hourly_rows": int(len(hourly)),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
