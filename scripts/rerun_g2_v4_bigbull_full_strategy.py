from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE = report_path("gen2_v2_complete_strategy", "sources", "g2_v2_complete.parquet")
if not SOURCE.exists():
    SOURCE = report_path("g2_sector_integration_probe", "sources", "base.parquet")

MARKET_CONTEXT = Path(r"F:\Stock\AiStockResearchArchive\reports\gen3_four_path_independent_candidates\market_context.csv")
if not MARKET_CONTEXT.exists():
    MARKET_CONTEXT = Path(r"F:\Stock\AiStock\reports\gen3_four_path_independent_candidates\market_context.csv")

OUT_DIR = report_path("g2_v4_bigbull_full_strategy_rerun_20260610")

INITIAL_CAPITAL = 150_000.0
SLOT_PCT = 0.20
SLOTS = 5
DAILY_OPEN_LIMIT = 1
COST_BPS = 30.0
WEAK_1030_RET = -0.03


def _json_default(obj: Any) -> Any:
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return str(obj)


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def load_source() -> pd.DataFrame:
    df = pd.read_parquet(SOURCE).copy()
    df["source_family"] = df["source_family"].astype(str)
    df = df[df["source_family"].eq("big_bull")].copy()
    for col in ["trade_date", "entry_date", "confirm_datetime"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in [
        "entry_price",
        "v4_rank",
        "v4_score",
        "l3_rt_strong3_ratio",
        "l3_s3",
        "rt_return_from_d1_close",
        "rt_breakout_vs_box_top",
        "index_close_ge_ma20",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["code"] = df["code"].astype(str)
    df["name"] = df.get("name", "").astype(str)
    return df.dropna(subset=["entry_date", "code", "entry_price"]).reset_index(drop=True)


def load_market_context() -> pd.DataFrame:
    ctx = pd.read_csv(MARKET_CONTEXT, low_memory=False, encoding="utf-8-sig")
    ctx["trade_date"] = pd.to_datetime(ctx["trade_date"], errors="coerce").dt.normalize()
    keep = [
        "trade_date",
        "market_style",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "breadth_ma20",
        "breadth_ma60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
        "market_amount_ratio20",
        "adx20",
        "plus_di20",
        "minus_di20",
        "mom20",
    ]
    return ctx[[c for c in keep if c in ctx.columns]].rename(
        columns={
            "trade_date": "trade_date",
            "market_style": "market_style_d1",
            "ma_skeleton": "ma_skeleton_d1",
            "volume_price_layer": "volume_price_layer_d1",
            "adx_layer": "adx_layer_d1",
            "mom20": "index_mom20_d1",
        }
    )


def attach_market_context(signals: pd.DataFrame) -> pd.DataFrame:
    ctx = load_market_context()
    out = signals.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.normalize()
    out = out.merge(ctx, on="trade_date", how="left")
    return out


def trade_calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    sql = f"""
    SELECT DISTINCT trade_date
    FROM kline_daily
    WHERE code = '000852.SH'
      AND trade_date BETWEEN toDate({sql_literal(start.strftime('%Y-%m-%d'))})
                         AND toDate({sql_literal(end.strftime('%Y-%m-%d'))})
    ORDER BY trade_date
    """
    df = clickhouse_query_df(sql)
    if df.empty:
        raise RuntimeError("empty trade calendar from kline_daily 000852.SH")
    return pd.to_datetime(df["trade_date"], errors="coerce").dropna().dt.normalize().tolist()


def load_daily(signals: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(signals["code"].dropna().astype(str).unique().tolist())
    start = pd.Timestamp(signals["entry_date"].min()) - pd.Timedelta(days=10)
    end = pd.Timestamp(signals["entry_date"].max()) + pd.Timedelta(days=45)
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 250):
        quoted = ",".join(sql_literal(c) for c in codes[i : i + 250])
        sql = f"""
        SELECT code, trade_date, open, high, low, close, amount
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN toDate({sql_literal(start.strftime('%Y-%m-%d'))})
                             AND toDate({sql_literal(end.strftime('%Y-%m-%d'))})
        ORDER BY code, trade_date
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        raise RuntimeError("empty daily prices for big_bull candidates")
    daily["code"] = daily["code"].astype(str)
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "amount"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    return daily.dropna(subset=["code", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "trade_date"])


def load_30m(signals: pd.DataFrame, calendar: list[pd.Timestamp]) -> pd.DataFrame:
    codes = sorted(signals["code"].dropna().astype(str).unique().tolist())
    next_map = {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}
    entry_days = set(pd.to_datetime(signals["entry_date"], errors="coerce").dt.normalize().dropna())
    need_days = sorted({next_map[d] for d in entry_days if d in next_map})
    if not need_days:
        return pd.DataFrame()
    start = min(need_days)
    end = max(need_days)
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 250):
        quoted = ",".join(sql_literal(c) for c in codes[i : i + 250])
        sql = f"""
        SELECT code, datetime, close
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime BETWEEN toDateTime({sql_literal(start.strftime('%Y-%m-%d 00:00:00'))})
                           AND toDateTime({sql_literal(end.strftime('%Y-%m-%d 23:59:59'))})
        ORDER BY code, datetime
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["code"] = bars["code"].astype(str)
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    bars["time"] = bars["datetime"].dt.strftime("%H:%M")
    bars["close"] = pd.to_numeric(bars["close"], errors="coerce")
    return bars.dropna(subset=["code", "datetime", "close"])


def enrich_exits(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return signals
    cal = trade_calendar(
        pd.Timestamp(signals["entry_date"].min()) - pd.Timedelta(days=20),
        pd.Timestamp(signals["entry_date"].max()) + pd.Timedelta(days=60),
    )
    daily = load_daily(signals)
    bars30 = load_30m(signals, cal)
    daily_by_code = {code: g.reset_index(drop=True) for code, g in daily.groupby("code", sort=False)}
    d1_map = {cal[i]: cal[i + 1] for i in range(len(cal) - 1)}
    bars_key = {}
    if not bars30.empty:
        for (code, day), g in bars30.groupby(["code", "trade_date"]):
            bars_key[(str(code), pd.Timestamp(day))] = g.sort_values("datetime")
    rows: list[dict[str, Any]] = []
    for row in signals.itertuples(index=False):
        item = row._asdict()
        code = str(item["code"])
        entry_date = pd.Timestamp(item["entry_date"]).normalize()
        entry_price = float(item["entry_price"])
        item["base_exit_date"] = pd.NaT
        item["base_exit_price"] = pd.NA
        item["base_policy_ret"] = pd.NA
        item["d1_date"] = d1_map.get(entry_date)
        item["d1_1030_exit_price"] = pd.NA
        item["d1_1030_ret"] = pd.NA
        item["d1_1030_weak"] = False
        g = daily_by_code.get(code)
        if g is not None and entry_price > 0:
            idxs = g.index[g["trade_date"] >= entry_date].tolist()
            if idxs:
                start_idx = int(idxs[0])
                exit_idx = start_idx + 4
                if exit_idx < len(g):
                    item["base_exit_date"] = g.loc[exit_idx, "trade_date"]
                    item["base_exit_price"] = float(g.loc[exit_idx, "close"])
                    item["base_policy_ret"] = item["base_exit_price"] / entry_price - 1.0 - COST_BPS / 10000.0
        d1 = item["d1_date"]
        if d1 is not None:
            bars = bars_key.get((code, pd.Timestamp(d1)))
            if bars is not None and not bars.empty:
                usable = bars[bars["time"].le("10:30")]
                if not usable.empty and entry_price > 0:
                    d1_price = float(usable.iloc[-1]["close"])
                    item["d1_1030_exit_price"] = d1_price
                    item["d1_1030_ret"] = d1_price / entry_price - 1.0
                    item["d1_1030_weak"] = item["d1_1030_ret"] <= WEAK_1030_RET
        rows.append(item)
    out = pd.DataFrame(rows)
    out["policy_exit_date"] = out["base_exit_date"]
    out["policy_exit_price"] = out["base_exit_price"]
    out["policy_net_ret"] = pd.to_numeric(out["base_policy_ret"], errors="coerce")
    out["exit_reason"] = "hold5_close"
    weak = out["d1_1030_weak"].fillna(False).astype(bool) & pd.to_numeric(out["d1_1030_exit_price"], errors="coerce").notna()
    out.loc[weak, "policy_exit_date"] = out.loc[weak, "d1_date"]
    out.loc[weak, "policy_exit_price"] = out.loc[weak, "d1_1030_exit_price"]
    out.loc[weak, "policy_net_ret"] = pd.to_numeric(out.loc[weak, "d1_1030_ret"], errors="coerce") - COST_BPS / 10000.0
    out.loc[weak, "exit_reason"] = "d1_1030_weak_full_exit"
    return out.dropna(subset=["policy_exit_date", "policy_net_ret"]).copy()


def select_variants(signals: pd.DataFrame) -> dict[str, pd.DataFrame]:
    d = signals.copy()
    def numeric_column(name: str, fallback: str | None = None) -> pd.Series:
        """Return an index-aligned numeric column when an older source lacks a field."""
        value = d.get(name)
        if value is None and fallback:
            value = d.get(fallback)
        if not isinstance(value, pd.Series):
            value = pd.Series(float("nan"), index=d.index, dtype=float)
        return pd.to_numeric(value, errors="coerce")

    l3 = numeric_column("l3_rt_strong3_ratio", "l3_s3")
    rt = numeric_column("rt_return_from_d1_close")
    box = numeric_column("rt_breakout_vs_box_top")
    index_raw = d.get("index_close_ge_ma20")
    if isinstance(index_raw, pd.Series) and index_raw.notna().any():
        index_num = pd.to_numeric(index_raw, errors="coerce")
        if index_num.notna().any():
            index_ok = index_num.gt(0)
        else:
            index_ok = index_raw.astype(str).str.lower().isin(["true", "1", "1.0", "yes"])
    else:
        skeleton = d.get("ma_skeleton_d1", pd.Series("", index=d.index)).astype(str)
        index_ok = skeleton.isin(["bull_stack", "weak_repair"])
    breadth = numeric_column("breadth_ma20")
    if breadth.isna().all():
        breadth = numeric_column("up_rate")
    style = d.get("market_style_d1", pd.Series("", index=d.index)).astype(str)
    struct_gate = (
        index_ok
        & breadth.ge(0.50)
        & l3.ge(0.10)
        & rt.ge(0.06)
        & box.ge(0.025)
        & box.le(0.08)
    )
    style_gate = style.isin(["standard_uptrend", "weak_rebound"])
    out = {
        "big_bull_all_hold5": d.assign(position_scale=1.0, variant="big_bull_all_hold5"),
        "struct_gate_full_hold5": d[struct_gate].assign(position_scale=1.0, variant="struct_gate_full_hold5"),
        "final_uptrend_weak_quarter_d1_exit": d[struct_gate & style_gate].assign(
            position_scale=0.25,
            variant="final_uptrend_weak_quarter_d1_exit",
        ),
    }
    return {k: v.reset_index(drop=True) for k, v in out.items()}


def simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    candidates = candidates.sort_values(["entry_date", "v4_score", "v4_rank", "confirm_datetime"], ascending=[True, False, True, True]).copy()
    cal = trade_calendar(
        pd.Timestamp(candidates["entry_date"].min()) - pd.Timedelta(days=5),
        pd.Timestamp(candidates["policy_exit_date"].max()) + pd.Timedelta(days=5),
    )
    by_day = {pd.Timestamp(day): g.copy() for day, g in candidates.groupby(pd.to_datetime(candidates["entry_date"]).dt.normalize())}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    for day in cal:
        still = []
        realized = 0.0
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                pnl = exit_value - float(pos["stake"])
                out = pos.copy()
                out["sell_date"] = day
                out["sell_price"] = pos["policy_exit_price"]
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                closed.append(out)
                realized += pnl
            else:
                still.append(pos)
        open_pos = still

        opened = 0
        skipped = 0
        todays = by_day.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= DAILY_OPEN_LIMIT or len(open_pos) >= SLOTS:
                    skipped += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                scale = float(getattr(row, "position_scale", 1.0) or 1.0)
                stake = equity_before * SLOT_PCT * scale
                if stake <= 0 or cash < stake:
                    skipped += 1
                    continue
                pos = row._asdict()
                pos["buy_date"] = day
                pos["buy_price"] = row.entry_price
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["stake"]) for p in open_pos)
        curve.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped": skipped,
                "realized_pnl": realized,
            }
        )
    curve_df = pd.DataFrame(curve)
    if not curve_df.empty:
        curve_df["peak"] = curve_df["equity"].cummax()
        curve_df["drawdown"] = curve_df["equity"] / curve_df["peak"] - 1.0
        curve_df["ret_from_start"] = curve_df["equity"] / INITIAL_CAPITAL - 1.0
    return curve_df, pd.DataFrame(closed)


def summarize(variant: str, candidates: pd.DataFrame, curve: pd.DataFrame, trades: pd.DataFrame) -> dict[str, Any]:
    net = pd.to_numeric(trades.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "source": str(SOURCE),
        "candidate_count": int(len(candidates)),
        "trade_count": int(len(trades)),
        "start_date": str(pd.to_datetime(candidates["entry_date"], errors="coerce").min().date()) if not candidates.empty else "",
        "end_date": str(pd.to_datetime(candidates["entry_date"], errors="coerce").max().date()) if not candidates.empty else "",
        "final_equity": float(curve["equity"].iloc[-1]) if not curve.empty else INITIAL_CAPITAL,
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "exit_reason_counts": trades.get("exit_reason", pd.Series(dtype=str)).astype(str).value_counts().to_dict() if not trades.empty else {},
    }


def write_report(summary: pd.DataFrame) -> None:
    lines = [
        "# G2 V4 big_bull full 策略独立复跑",
        "",
        "这不是按 331% summary 反推参数，而是从当前可追溯的 G2/V4 `big_bull` 源信号重新生成选股、买点、卖点和收益曲线。",
        "",
        f"- 源文件：`{SOURCE}`",
        f"- 市场四态：`{MARKET_CONTEXT}`",
        f"- 初始资金：`{INITIAL_CAPITAL:,.0f}`",
        f"- 仓位：`SLOTS={SLOTS}`，`SLOT_PCT={SLOT_PCT:.0%}`，`DAILY_OPEN_LIMIT={DAILY_OPEN_LIMIT}`",
        f"- 成本：`{COST_BPS:.0f}bps`",
        f"- D1 弱确认退出：D1 10:30 相对买入价 `<= {WEAK_1030_RET:.0%}` 时全退",
        "",
        "## 汇总",
        "",
        summary.assign(
            total_return=summary["total_return"].map(pct),
            max_drawdown=summary["max_drawdown"].map(pct),
            win_rate=summary["win_rate"].map(pct),
            avg_trade_return=summary["avg_trade_return"].map(pct),
            worst_trade=summary["worst_trade"].map(pct),
        ).to_markdown(index=False),
        "",
        "## 文件",
        "",
        "- `*_candidates.csv`：候选/选股/买点字段。",
        "- `*_trades.csv`：实际撮合后的买卖点和收益。",
        "- `*_equity_curve.csv`：权益曲线。",
        "- `summary.csv/json`：汇总。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = load_source()
    with_ctx = attach_market_context(raw)
    enriched = enrich_exits(with_ctx)
    (OUT_DIR / "big_bull_source_enriched.csv").write_text("", encoding="utf-8")
    enriched.to_csv(OUT_DIR / "big_bull_source_enriched.csv", index=False, encoding="utf-8-sig")
    summaries: list[dict[str, Any]] = []
    for variant, candidates in select_variants(enriched).items():
        candidates.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
        curve, trades = simulate(candidates)
        curve.to_csv(OUT_DIR / f"{variant}_equity_curve.csv", index=False, encoding="utf-8-sig")
        trades.to_csv(OUT_DIR / f"{variant}_trades.csv", index=False, encoding="utf-8-sig")
        summaries.append(summarize(variant, candidates, curve, trades))
    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    write_report(summary)
    print(f"done: {OUT_DIR}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
