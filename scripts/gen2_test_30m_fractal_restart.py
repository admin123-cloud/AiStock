from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from utils.paths import report_path  # noqa: E402

from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_EVENT_DATASET = report_path("gen2_event_study_full", "v4_event_dataset.parquet")
DEFAULT_STATE_DAILY = report_path("gen2_open_state_research_full", "g2_open_state_daily.csv")
DEFAULT_OUTPUT_DIR = report_path("gen2_30m_fractal_restart")
HORIZONS = (1, 2, 3, 5, 10, 20)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return str(value)


def _minute_coverage() -> Dict[str, Any]:
    ch = clickhouse_client()
    df = ch.query_df(
        """
        SELECT
            count() AS rows,
            min(datetime) AS min_dt,
            max(datetime) AS max_dt,
            uniqExact(code) AS codes
        FROM kline_minute_30
        """
    )
    row = df.iloc[0].to_dict()
    return {
        "rows": int(row["rows"]),
        "min_dt": str(row["min_dt"]),
        "max_dt": str(row["max_dt"]),
        "codes": int(row["codes"]),
    }


def _load_state_daily(path: Path) -> pd.DataFrame:
    states = pd.read_csv(path)
    states["trade_date"] = pd.to_datetime(states["trade_date"]).dt.strftime("%Y-%m-%d")
    keep = ["trade_date", "g2_open_state", "breadth_ma20"]
    for horizon in HORIZONS:
        col = f"csi1000_fwd_ret_{horizon}d"
        if col in states.columns:
            keep.append(col)
    return states[keep].copy()


def _load_contexts(event_dataset: Path, state_daily: Path, start_date: str, end_date: str) -> pd.DataFrame:
    events = pd.read_parquet(event_dataset)
    events["trade_date"] = pd.to_datetime(events["trade_date"]).dt.strftime("%Y-%m-%d")
    events = events[(events["trade_date"] >= start_date) & (events["trade_date"] <= end_date)].copy()
    states = _load_state_daily(state_daily)
    events = events.merge(states, on="trade_date", how="left")
    events = _add_context_patterns(events)
    pattern_cols = [col for col in events.columns if col.startswith("ctx_")]
    keep = [
        "trade_date",
        "code",
        "name",
        "v4_rank",
        "v4_score",
        "entry_pass",
        "g2_open_state",
        "breadth_ma20",
        "close",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
    ] + pattern_cols
    d = events[keep].copy()
    mask = pd.Series(False, index=d.index)
    for col in pattern_cols:
        mask = mask | d[col].fillna(False).astype(bool)
    return d[mask].reset_index(drop=True)


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df[col], errors="coerce")


def _add_context_patterns(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    entry = d["entry_pass"].fillna(False).astype(bool)
    rank = _num(d, "v4_rank")
    score = _num(d, "v4_score")
    mom5 = _num(d, "mom5")
    mom10 = _num(d, "mom10")
    mom20 = _num(d, "mom20")
    vol_ratio = _num(d, "vol_ratio")
    vol10 = _num(d, "vol10")
    d["ctx_pullback_restart_base"] = (
        entry
        & rank.le(50)
        & mom10.ge(0.08)
        & mom20.ge(0)
        & mom5.ge(-0.03)
        & mom5.le(0.08)
        & mom5.le(mom10 * 0.65)
        & vol_ratio.ge(0.60)
        & vol_ratio.le(2.80)
        & vol10.le(0.075)
    )
    d["ctx_pullback_restart_rank80"] = (
        entry
        & rank.le(80)
        & mom10.ge(0.06)
        & mom20.ge(-0.02)
        & mom5.ge(-0.04)
        & mom5.le(0.10)
        & mom5.le(mom10 * 0.80)
        & vol_ratio.ge(0.50)
        & vol_ratio.le(3.20)
        & vol10.le(0.085)
    )
    d["ctx_pullback_restart_rank100"] = (
        entry
        & rank.le(100)
        & mom10.ge(0.05)
        & mom20.ge(-0.03)
        & mom5.ge(-0.05)
        & mom5.le(0.11)
        & mom5.le(mom10 * 0.90)
        & vol_ratio.ge(0.45)
        & vol_ratio.le(3.50)
        & vol10.le(0.090)
    )
    d["ctx_pullback_restart_score_pool"] = (
        score.ge(0.70)
        & rank.le(150)
        & mom10.ge(0.05)
        & mom20.ge(-0.02)
        & mom5.ge(-0.05)
        & mom5.le(0.10)
        & mom5.le(mom10 * 0.90)
        & vol_ratio.ge(0.45)
        & vol_ratio.le(3.20)
        & vol10.le(0.090)
    )
    d["ctx_pullback_restart_top30"] = d["ctx_pullback_restart_base"] & rank.le(30)
    d["ctx_pullback_restart_strict"] = (
        entry
        & rank.le(30)
        & score.ge(0.78)
        & mom10.ge(0.10)
        & mom20.ge(0.02)
        & mom5.ge(-0.02)
        & mom5.le(0.06)
        & mom5.le(mom10 * 0.50)
        & vol_ratio.ge(0.70)
        & vol_ratio.le(2.20)
        & vol10.le(0.060)
    )
    d["ctx_momentum_cool_top30"] = (
        entry
        & rank.le(30)
        & mom10.ge(0.05)
        & mom20.ge(0)
        & mom5.ge(-0.03)
        & mom5.le(0.08)
        & vol10.le(0.070)
    )
    return d


def _load_trade_dates(start_date: str, end_date: str) -> List[str]:
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY trade_date
        """
    )
    return [pd.Timestamp(x).strftime("%Y-%m-%d") for x in df["trade_date"].tolist()]


def _add_search_end(contexts: pd.DataFrame, trade_dates: List[str], search_days: int, entry_start_lag_days: int = 0) -> pd.DataFrame:
    idx = {date: i for i, date in enumerate(trade_dates)}
    out = contexts.copy()
    out["trade_idx"] = out["trade_date"].map(idx)
    out = out.dropna(subset=["trade_idx"]).copy()
    out["trade_idx"] = out["trade_idx"].astype(int)
    entry_lag = max(0, int(entry_start_lag_days))
    end_lag = max(entry_lag, int(search_days))
    out["entry_start_idx"] = (out["trade_idx"] + entry_lag).clip(upper=len(trade_dates) - 1)
    out["entry_start_date"] = out["entry_start_idx"].map({i: date for i, date in enumerate(trade_dates)})
    out["search_end_idx"] = (out["trade_idx"] + end_lag).clip(upper=len(trade_dates) - 1)
    out["search_end_date"] = out["search_end_idx"].map({i: date for i, date in enumerate(trade_dates)})
    out = out[out["entry_start_idx"] <= out["search_end_idx"]].copy()
    return out


def _load_minute_bars(codes: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    ch = clickhouse_client()
    codes = sorted(set(str(code) for code in codes))
    batch_size = 250
    chunks: list[list[str]] = [codes[i : i + batch_size] for i in range(0, len(codes), batch_size)]

    frames: list[pd.DataFrame] = []
    for chunk in chunks:
        quoted = ", ".join([f"'{code}'" for code in chunk])
        sql = f"""
            SELECT code, datetime, open, high, low, close, volume, amount
            FROM kline_minute_30
            WHERE code IN ({quoted})
              AND toDate(datetime) >= toDate('{start_date}')
              AND toDate(datetime) <= toDate('{end_date}')
            ORDER BY code, datetime
        """
        part = ch.query_df(sql)
        if not part.empty:
            frames.append(part)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["bar_date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "datetime", "low", "close"]).reset_index(drop=True)

    try:
        existing_dates = set(df["bar_date"].dropna().astype(str).unique().tolist())
        needed_dates = set(pd.date_range(start=start_date, end=end_date).strftime("%Y-%m-%d").tolist())
        missing_dates = sorted(needed_dates - existing_dates)
        if missing_dates:
            from scripts.collect_intraday_snapshots import load_snapshot_minute_bars

            snap = load_snapshot_minute_bars(
                codes=list(codes),
                start_date=missing_dates[0],
                end_date=missing_dates[-1],
                period_minutes=30,
                asset_type="stock",
            )
            if snap is not None and not snap.empty:
                snap["datetime"] = pd.to_datetime(snap["datetime"], errors="coerce")
                snap["bar_date"] = snap["datetime"].dt.strftime("%Y-%m-%d")
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    snap[col] = pd.to_numeric(snap[col], errors="coerce")
                df = pd.concat([df, snap], ignore_index=True, sort=False)
                df = df.dropna(subset=["code", "datetime", "low", "close"])
                df = df.drop_duplicates(["code", "datetime"], keep="last").sort_values(["code", "datetime"]).reset_index(drop=True)
    except Exception:
        pass
    return df


def _find_fractal_triggers(contexts: pd.DataFrame, minute_bars: pd.DataFrame) -> pd.DataFrame:
    if contexts.empty or minute_bars.empty:
        return pd.DataFrame()
    context_by_code = {code: g.copy() for code, g in contexts.groupby("code")}
    rows: List[Dict[str, Any]] = []
    pattern_cols = [col for col in contexts.columns if col.startswith("ctx_")]

    for code, bars in minute_bars.groupby("code", sort=False):
        ctx = context_by_code.get(code)
        if ctx is None or ctx.empty:
            continue
        b = bars.sort_values("datetime").reset_index(drop=True).copy()
        prev_low = b["low"].shift(1)
        next_low = b["low"].shift(-1)
        b["is_bottom_fractal"] = b["low"].lt(prev_low) & b["low"].le(next_low)
        b["fractal_low"] = b["low"]
        b["fractal_close"] = b["close"]
        b["ma5"] = b["close"].rolling(5, min_periods=5).mean()
        b["ma10"] = b["close"].rolling(10, min_periods=10).mean()
        b["amount_ma5_prev"] = b["amount"].shift(1).rolling(5, min_periods=3).mean()
        b["confirm_datetime"] = b["datetime"].shift(-1)
        b["confirm_close"] = b["close"].shift(-1)
        b["confirm_high"] = b["high"].shift(-1)
        b["confirm_amount"] = b["amount"].shift(-1)
        b["confirm_ma5"] = b["ma5"].shift(-1)
        b["confirm_ma10"] = b["ma10"].shift(-1)
        b["confirm_amount_ma5_prev"] = b["amount_ma5_prev"].shift(-1)
        triggers = b[b["is_bottom_fractal"] & b["confirm_datetime"].notna()].copy()
        triggers["confirm_date"] = triggers["confirm_datetime"].dt.strftime("%Y-%m-%d")
        if triggers.empty:
            continue
        for _, event in ctx.iterrows():
            window = triggers[
                (triggers["confirm_date"] >= event.get("entry_start_date", event["trade_date"]))
                & (triggers["confirm_date"] <= event["search_end_date"])
            ].copy()
            if window.empty:
                continue
            first = window.iloc[0]
            trigger_types = ["bottom_fractal_any"]
            confirm_close = float(first["confirm_close"])
            confirm_amount = float(first["confirm_amount"]) if pd.notna(first.get("confirm_amount")) else np.nan
            confirm_ma5 = float(first["confirm_ma5"]) if pd.notna(first.get("confirm_ma5")) else np.nan
            confirm_ma10 = float(first["confirm_ma10"]) if pd.notna(first.get("confirm_ma10")) else np.nan
            amount_ma5 = float(first["confirm_amount_ma5_prev"]) if pd.notna(first.get("confirm_amount_ma5_prev")) else np.nan
            reclaim = confirm_close > float(first["fractal_close"])
            break_high = confirm_close > float(first["high"])
            reclaim_ma5 = pd.notna(confirm_ma5) and confirm_close >= confirm_ma5
            reclaim_ma10 = pd.notna(confirm_ma10) and confirm_close >= confirm_ma10
            volume_expand = pd.notna(amount_ma5) and amount_ma5 > 0 and confirm_amount >= amount_ma5 * 1.20
            if reclaim:
                trigger_types.append("bottom_fractal_reclaim")
            if break_high:
                trigger_types.append("bottom_fractal_break_high")
            if reclaim and reclaim_ma5:
                trigger_types.append("bottom_fractal_reclaim_ma5")
            if reclaim and reclaim_ma10:
                trigger_types.append("bottom_fractal_reclaim_ma10")
            if break_high and reclaim_ma5:
                trigger_types.append("bottom_fractal_break_high_ma5")
            if break_high and reclaim_ma10:
                trigger_types.append("bottom_fractal_break_high_ma10")
            if break_high and volume_expand:
                trigger_types.append("bottom_fractal_break_high_vol")
            if break_high and reclaim_ma5 and volume_expand:
                trigger_types.append("bottom_fractal_break_high_ma5_vol")
            active_patterns = [col.replace("ctx_", "") for col in pattern_cols if bool(event.get(col, False))]
            for pattern in active_patterns:
                for trigger_type in trigger_types:
                    row = event.to_dict()
                    row.update(
                        {
                            "pattern": pattern,
                            "trigger_type": trigger_type,
                            "fractal_datetime": first["datetime"],
                            "confirm_datetime": first["confirm_datetime"],
                            "entry_date": str(first["confirm_date"]),
                            "entry_price": float(first["confirm_close"]),
                            "fractal_low": float(first["fractal_low"]),
                            "fractal_close": float(first["fractal_close"]),
                            "confirm_ma5": confirm_ma5,
                            "confirm_ma10": confirm_ma10,
                            "confirm_amount": confirm_amount,
                            "confirm_amount_ma5_prev": amount_ma5,
                            "volume_expand": bool(volume_expand),
                        }
                    )
                    rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _load_daily_future_prices(codes: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    ch = clickhouse_client()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    sql = f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY code, trade_date
    """
    df = ch.query_df(sql)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)


def _add_forward_returns_from_entry(triggers: pd.DataFrame, daily: pd.DataFrame, trade_dates: List[str]) -> pd.DataFrame:
    if triggers.empty:
        return triggers
    date_idx = {date: i for i, date in enumerate(trade_dates)}
    price_map = {(row.code, row.trade_date): float(row.close) for row in daily.itertuples(index=False)}
    out = triggers.copy()
    out["entry_trade_idx"] = out["entry_date"].map(date_idx)
    for horizon in HORIZONS:
        returns: List[float] = []
        for row in out.itertuples(index=False):
            entry_idx = getattr(row, "entry_trade_idx")
            entry_price = float(getattr(row, "entry_price"))
            if pd.isna(entry_idx) or entry_price <= 0:
                returns.append(np.nan)
                continue
            target_idx = int(entry_idx) + horizon
            if target_idx >= len(trade_dates):
                returns.append(np.nan)
                continue
            target_date = trade_dates[target_idx]
            close = price_map.get((getattr(row, "code"), target_date))
            returns.append(close / entry_price - 1.0 if close is not None else np.nan)
        out[f"entry_fwd_ret_{horizon}d"] = returns
    return out


def _add_index_excess_from_entry(triggers: pd.DataFrame, state_daily: Path) -> pd.DataFrame:
    if triggers.empty:
        return triggers
    states = pd.read_csv(state_daily)
    states["trade_date"] = pd.to_datetime(states["trade_date"]).dt.strftime("%Y-%m-%d")
    index_maps = {
        horizon: dict(zip(states["trade_date"], pd.to_numeric(states[f"csi1000_fwd_ret_{horizon}d"], errors="coerce")))
        for horizon in HORIZONS
        if f"csi1000_fwd_ret_{horizon}d" in states.columns
    }
    out = triggers.copy()
    for horizon, index_map in index_maps.items():
        idx_col = f"entry_csi1000_fwd_ret_{horizon}d"
        ret_col = f"entry_fwd_ret_{horizon}d"
        excess_col = f"entry_excess_vs_csi1000_{horizon}d"
        out[idx_col] = out["entry_date"].map(index_map)
        if ret_col in out.columns:
            out[excess_col] = pd.to_numeric(out[ret_col], errors="coerce") - pd.to_numeric(out[idx_col], errors="coerce")
    return out


def _stats(values: pd.Series) -> Dict[str, Any]:
    x = pd.to_numeric(values, errors="coerce").dropna()
    if x.empty:
        return {"count": 0, "mean": None, "median": None, "win_rate": None, "p25": None, "p75": None}
    return {
        "count": int(len(x)),
        "mean": float(x.mean()),
        "median": float(x.median()),
        "win_rate": float((x > 0).mean()),
        "p25": float(x.quantile(0.25)),
        "p75": float(x.quantile(0.75)),
    }


def _summarize(triggers: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    if triggers.empty:
        return pd.DataFrame()
    for (pattern, state, trigger_type), group in triggers.groupby(["pattern", "g2_open_state", "trigger_type"], dropna=False):
        for horizon in HORIZONS:
            col = f"entry_fwd_ret_{horizon}d"
            if col not in group.columns:
                continue
            row = {
                "pattern": pattern,
                "state": state,
                "trigger_type": trigger_type,
                "horizon": horizon,
            }
            row.update(_stats(group[col]))
            excess_col = f"entry_excess_vs_csi1000_{horizon}d"
            if excess_col in group.columns:
                excess = _stats(group[excess_col])
                row["excess_mean_vs_csi1000"] = excess["mean"]
                row["excess_median_vs_csi1000"] = excess["median"]
                row["excess_win_rate_vs_csi1000"] = excess["win_rate"]
            rows.append(row)
    return pd.DataFrame(rows).sort_values(["pattern", "state", "trigger_type", "horizon"]) if rows else pd.DataFrame()


def _write_findings(output_dir: Path, summary: pd.DataFrame, payload: Dict[str, Any]) -> None:
    lines = [
        "# G2 30m Bottom-Fractal Restart Test",
        "",
        f"Window: {payload['start_date']} to {payload['end_date']}",
        f"Contexts: {payload['context_rows']}",
        f"Triggers: {payload['trigger_rows']}",
        "",
        "## Top Cells",
        "",
    ]
    if summary.empty:
        lines.append("No trigger rows.")
    else:
        top = summary[(summary["count"] >= 20) & (summary["mean"] > 0)].copy()
        top["score"] = top["mean"].fillna(0) * 100 + top["median"].fillna(0) * 50 + (top["win_rate"].fillna(0) - 0.5) * 10
        top = top.sort_values("score", ascending=False).head(30)
        lines.extend(["| pattern | state | trigger | h | count | mean | median | win_rate |", "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"])
        for _, row in top.iterrows():
            lines.append(
                f"| {row['pattern']} | {row['state']} | {row['trigger_type']} | {int(row['horizon'])} | {int(row['count'])} | {float(row['mean']):.2%} | {float(row['median']):.2%} | {float(row['win_rate']):.2%} |"
            )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(
    event_dataset: Path,
    state_daily: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    search_days: int,
    entry_start_lag_days: int = 0,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage = _minute_coverage()
    minute_start = str(pd.Timestamp(coverage["min_dt"]).date())
    minute_end = str(pd.Timestamp(coverage["max_dt"]).date())
    start_date = max(start_date, minute_start)
    end_date = min(end_date, minute_end)
    trade_dates = _load_trade_dates(start_date, end_date)
    contexts = _add_search_end(
        _load_contexts(event_dataset, state_daily, start_date, end_date),
        trade_dates,
        search_days,
        entry_start_lag_days=entry_start_lag_days,
    )
    codes = contexts["code"].dropna().astype(str).unique().tolist()
    minute_bars = _load_minute_bars(codes, start_date, end_date)
    triggers = _find_fractal_triggers(contexts, minute_bars)
    if not triggers.empty:
        daily = _load_daily_future_prices(codes, start_date, end_date)
        triggers = _add_forward_returns_from_entry(triggers, daily, trade_dates)
        triggers = _add_index_excess_from_entry(triggers, state_daily)
    summary = _summarize(triggers)

    contexts.to_parquet(output_dir / "daily_contexts.parquet", index=False)
    triggers.to_parquet(output_dir / "fractal_triggers.parquet", index=False)
    summary.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")

    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "search_days": int(search_days),
        "entry_start_lag_days": int(entry_start_lag_days),
        "lookahead_safe": int(entry_start_lag_days) >= 1,
        "minute_coverage": coverage,
        "context_rows": int(len(contexts)),
        "context_codes": int(contexts["code"].nunique()) if not contexts.empty else 0,
        "minute_rows": int(len(minute_bars)),
        "trigger_rows": int(len(triggers)),
        "trigger_codes": int(triggers["code"].nunique()) if not triggers.empty else 0,
        "outputs": {
            "daily_contexts": "daily_contexts.parquet",
            "fractal_triggers": "fractal_triggers.parquet",
            "summary": "summary.csv",
            "findings": "findings.md",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, summary, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Test strong-stock pullback restart with 30m bottom fractal trigger.")
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-05-22")
    add_end_date_argument(parser)
    parser.add_argument("--search-days", type=int, default=2)
    parser.add_argument(
        "--entry-start-lag-days",
        type=int,
        default=0,
        help="Trading-day lag before minute triggers may start. Use 1 for D-1 confirmed daily context and D intraday execution.",
    )
    args = parser.parse_args()
    payload = run(
        event_dataset=Path(args.event_dataset),
        state_daily=Path(args.state_daily),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=resolve_end_date(args.end_date),
        search_days=int(args.search_days),
        entry_start_lag_days=int(args.entry_start_lag_days),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
