import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_validate_v2_trend_continuation import (  # noqa: E402
    TriggerProfile,
    _json_default,
    _load_signal_bars,
    _load_trade_dates,
    _pct,
    _run_backtest,
    _write_signal_event_study,
)
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date  # noqa: E402
from utils.paths import report_path  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_EVENT_DATASET = report_path("gen2_event_study_full", "v4_event_dataset.parquet")
DEFAULT_STATE_DAILY = report_path("gen2_open_state_research_full", "g2_open_state_daily.csv")
DEFAULT_OUTPUT_DIR = report_path("gen2_breakout_buy_point_research")


@dataclass(frozen=True)
class CandidateProfile:
    name: str
    source: str
    max_rank: int
    min_mom10: float
    min_mom20: float
    max_mom5: float
    max_vol_ratio: float
    max_vol10: float
    states: tuple[str, ...]


@dataclass(frozen=True)
class BreakoutTriggerProfile:
    name: str
    setup_type: str
    max_box_range: float
    breakout_buffer: float
    vol_mult: float
    max_above_top: float
    min_time: str


def _candidate_profiles() -> list[CandidateProfile]:
    return [
        CandidateProfile("pool_rank100_box", "in_score_pool", 100, 0.02, 0.03, 0.12, 3.0, 0.10, ("NORMAL", "AGGRESSIVE")),
        CandidateProfile("pool_rank200_box", "in_score_pool", 200, 0.02, 0.03, 0.12, 3.0, 0.10, ("NORMAL", "AGGRESSIVE")),
        CandidateProfile("entry_rank100_box", "entry_pass", 100, 0.02, 0.03, 0.12, 3.0, 0.10, ("NORMAL", "AGGRESSIVE")),
    ]


def _breakout_triggers() -> list[BreakoutTriggerProfile]:
    return [
        BreakoutTriggerProfile("box20_break_close_vol12", "box20", 0.35, 1.000, 1.2, 0.12, "10:00:00"),
        BreakoutTriggerProfile("box60_break_close_vol12", "box60", 0.55, 1.000, 1.2, 0.15, "10:00:00"),
        BreakoutTriggerProfile("micro_range_break_vol12", "micro_range", 0.16, 1.000, 1.2, 0.10, "10:00:00"),
        BreakoutTriggerProfile("abc_shake_break_vol12", "abc_shake", 0.26, 1.000, 1.2, 0.12, "10:00:00"),
        BreakoutTriggerProfile("stair_step_break_vol10", "stair_step", 0.20, 1.000, 1.0, 0.12, "10:00:00"),
        BreakoutTriggerProfile("big_bull_rebreak_2_5d_vol12", "big_bull_rebreak_2_5d", 0.18, 1.000, 1.2, 0.15, "10:00:00"),
    ]


def _load_states(path: Path) -> pd.DataFrame:
    states = pd.read_csv(path)
    states["trade_date"] = pd.to_datetime(states["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return states[["trade_date", "g2_open_state"]].dropna(subset=["trade_date"]).copy()


def _load_daily_breakout_features(
    codes: list[str],
    start_date: str,
    end_date: str,
    target_dates_by_code: dict[str, set[str]] | None = None,
) -> pd.DataFrame:
    base_cols = [
        "code",
        "trade_date",
        "prior20_high",
        "prior20_low",
        "box20_range",
        "close_vs_prior20_high",
        "prior60_high",
        "prior60_low",
        "box60_range",
        "close_vs_prior60_high",
    ]
    if not codes:
        return pd.DataFrame(columns=base_cols)
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, open, high, low, close, amount
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN subtractDays(toDate('{start_date}'), 120) AND toDate('{end_date}')
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "high", "low", "close"]).sort_values(["code", "trade_date"])
    g = df.groupby("code", sort=False)
    df["prev_close"] = g["close"].shift(1)
    df["ret1"] = df["close"] / df["prev_close"] - 1.0
    df["amount_ma5_prev_daily"] = g["amount"].transform(lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    for n in (20, 60):
        df[f"prior{n}_high"] = g["high"].transform(lambda s: s.shift(1).rolling(n, min_periods=n).max())
        df[f"prior{n}_low"] = g["low"].transform(lambda s: s.shift(1).rolling(n, min_periods=n).min())
        df[f"box{n}_range"] = df[f"prior{n}_high"] / df[f"prior{n}_low"] - 1.0
        df[f"close_vs_prior{n}_high"] = df["close"] / df[f"prior{n}_high"] - 1.0
    setup_rows: list[dict[str, Any]] = []
    for code, part in df.groupby("code", sort=False):
        target_dates = target_dates_by_code.get(str(code), set()) if target_dates_by_code else set()
        if target_dates_by_code and not target_dates:
            continue
        part = part.reset_index(drop=True)
        for i, row in part.iterrows():
            if target_dates_by_code and row["trade_date"] not in target_dates:
                continue
            out: dict[str, Any] = {"code": code, "trade_date": row["trade_date"]}
            for n in (20, 60):
                top = row.get(f"prior{n}_high")
                low = row.get(f"prior{n}_low")
                rng = row.get(f"box{n}_range")
                out[f"setup_box{n}"] = bool(pd.notna(top) and pd.notna(low) and pd.notna(rng))
                out[f"setup_box{n}_top"] = top
                out[f"setup_box{n}_low"] = low
                out[f"setup_box{n}_range"] = rng

            w6 = part.iloc[max(0, i - 5) : i + 1]
            if len(w6) >= 4:
                top = float(w6["high"].max())
                low = float(w6["low"].min())
                rng = top / low - 1.0 if low > 0 else np.nan
                close_pos = (float(row["close"]) - low) / (top - low) if top > low else np.nan
                out["setup_micro_range"] = bool(pd.notna(rng) and rng <= 0.16 and close_pos >= 0.55)
                out["setup_micro_range_top"] = top
                out["setup_micro_range_low"] = low
                out["setup_micro_range_range"] = rng
            else:
                out["setup_micro_range"] = False

            w10 = part.iloc[max(0, i - 9) : i + 1]
            if len(w10) >= 6:
                top = float(w10["high"].max())
                low = float(w10["low"].min())
                rng = top / low - 1.0 if low > 0 else np.nan
                red_days = int((w10["close"] < w10["open"]).sum())
                down_days = int((w10["ret1"] < -0.005).sum())
                close_pos = (float(row["close"]) - low) / (top - low) if top > low else np.nan
                out["setup_abc_shake"] = bool(
                    pd.notna(rng) and rng <= 0.26 and red_days >= 2 and down_days >= 1 and close_pos >= 0.50
                )
                out["setup_abc_shake_top"] = top
                out["setup_abc_shake_low"] = low
                out["setup_abc_shake_range"] = rng
            else:
                out["setup_abc_shake"] = False

            w5 = part.iloc[max(0, i - 4) : i + 1]
            if len(w5) >= 4:
                top = float(w5["high"].max())
                low = float(w5["low"].min())
                rng = top / low - 1.0 if low > 0 else np.nan
                up_days = int((w5["close"] > w5["prev_close"]).sum())
                total_ret = float(w5["close"].iloc[-1] / w5["close"].iloc[0] - 1.0)
                max_ret = float(w5["ret1"].max())
                close_dd = (w5["close"] / w5["close"].cummax() - 1.0).min()
                out["setup_stair_step"] = bool(
                    pd.notna(rng)
                    and rng <= 0.20
                    and up_days >= 3
                    and 0.015 <= total_ret <= 0.18
                    and max_ret <= 0.065
                    and close_dd >= -0.08
                )
                out["setup_stair_step_top"] = top
                out["setup_stair_step_low"] = low
                out["setup_stair_step_range"] = rng
            else:
                out["setup_stair_step"] = False

            out["setup_big_bull_rebreak_2_5d"] = False
            for lag in range(2, 6):
                j = i - lag
                if j < 20:
                    continue
                big = part.iloc[j]
                big_prior20 = big.get("prior20_high")
                big_amt_ma5 = big.get("amount_ma5_prev_daily")
                big_break = (
                    pd.notna(big_prior20)
                    and pd.notna(big_amt_ma5)
                    and big_amt_ma5 > 0
                    and float(big["ret1"]) >= 0.055
                    and float(big["close"]) >= float(big_prior20) * 0.995
                    and float(big["amount"]) >= float(big_amt_ma5) * 1.5
                )
                if not big_break:
                    continue
                rest = part.iloc[j + 1 : i + 1]
                if len(rest) < 2:
                    continue
                top = float(part.iloc[j : i + 1]["high"].max())
                low = float(rest["low"].min())
                rng = top / low - 1.0 if low > 0 else np.nan
                low_ok = low >= float(big["close"]) * 0.90
                close_ok = float(row["close"]) >= float(big["close"]) * 0.96
                if pd.notna(rng) and rng <= 0.18 and low_ok and close_ok:
                    out["setup_big_bull_rebreak_2_5d"] = True
                    out["setup_big_bull_rebreak_2_5d_top"] = top
                    out["setup_big_bull_rebreak_2_5d_low"] = low
                    out["setup_big_bull_rebreak_2_5d_range"] = rng
                    out["big_bull_lag_days"] = lag
                    break
            setup_rows.append(out)
    setup_df = pd.DataFrame(setup_rows)
    if not setup_rows:
        return df[base_cols].copy()
    return df[base_cols].merge(setup_df, on=["code", "trade_date"], how="left")


def _prepare_events(event_dataset: Path, state_daily: Path, start_date: str, end_date: str, trade_dates: list[str]) -> pd.DataFrame:
    raw = pd.read_parquet(event_dataset)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    event_start = (pd.to_datetime(start_date) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    raw = raw[(raw["trade_date"] >= event_start) & (raw["trade_date"] <= end_date)].copy()
    raw["v4_rank"] = pd.to_numeric(raw["v4_rank"], errors="coerce").fillna(9999).astype(int)
    raw = raw[raw["v4_rank"] <= 220].copy()
    for col in ["v4_score", "mom5", "mom10", "mom20", "vol_ratio", "vol10"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw["entry_pass"] = raw["entry_pass"].fillna(False).astype(bool)
    raw["in_score_pool"] = raw["in_score_pool"].fillna(False).astype(bool)
    raw = raw.merge(_load_states(state_daily), on="trade_date", how="left")
    next_date = {trade_dates[i]: trade_dates[i + 1] for i in range(len(trade_dates) - 1)}
    raw["entry_date"] = raw["trade_date"].map(next_date)
    raw = raw.dropna(subset=["entry_date"]).copy()
    raw = raw[(raw["entry_date"] >= start_date) & (raw["entry_date"] <= end_date)].copy()
    candidate_mask = (
        (
            raw["in_score_pool"]
            & (raw["v4_rank"] <= 200)
            & (raw["g2_open_state"].isin(("NORMAL", "AGGRESSIVE")))
            & (raw["mom10"] >= 0.02)
            & (raw["mom20"] >= 0.03)
            & (raw["mom5"] <= 0.12)
            & (raw["vol_ratio"] <= 3.0)
            & (raw["vol10"] <= 0.10)
        )
        | (
            raw["entry_pass"]
            & (raw["v4_rank"] <= 100)
            & (raw["g2_open_state"].isin(("NORMAL", "AGGRESSIVE")))
            & (raw["mom10"] >= 0.02)
            & (raw["mom20"] >= 0.03)
            & (raw["mom5"] <= 0.12)
            & (raw["vol_ratio"] <= 3.0)
            & (raw["vol10"] <= 0.10)
        )
    )
    raw = raw[candidate_mask].copy()
    target_dates_by_code = {
        str(code): set(group["trade_date"].dropna().astype(str).tolist())
        for code, group in raw.groupby("code", sort=False)
    }
    features = _load_daily_breakout_features(
        raw["code"].dropna().astype(str).unique().tolist(),
        start_date,
        end_date,
        target_dates_by_code=target_dates_by_code,
    )
    raw = raw.merge(features, on=["code", "trade_date"], how="left")
    return raw.reset_index(drop=True)


def _select_candidates(events: pd.DataFrame, profile: CandidateProfile) -> pd.DataFrame:
    source_mask = events[profile.source].fillna(False).astype(bool)
    d = events[
        source_mask
        & (events["v4_rank"] <= profile.max_rank)
        & (events["g2_open_state"].isin(profile.states))
        & (events["mom10"] >= profile.min_mom10)
        & (events["mom20"] >= profile.min_mom20)
        & (events["mom5"] <= profile.max_mom5)
        & (events["vol_ratio"] <= profile.max_vol_ratio)
        & (events["vol10"] <= profile.max_vol10)
    ].copy()
    d["candidate_profile"] = profile.name
    return d.reset_index(drop=True)


def _apply_breakout_trigger(joined: pd.DataFrame, trigger: BreakoutTriggerProfile) -> pd.DataFrame:
    d = joined[joined["bar_time"] >= trigger.min_time].copy()
    if d.empty:
        return d
    setup_col = f"setup_{trigger.setup_type}"
    top_col = f"setup_{trigger.setup_type}_top"
    low_col = f"setup_{trigger.setup_type}_low"
    range_col = f"setup_{trigger.setup_type}_range"
    d = d[
        d.get(setup_col, False).fillna(False).astype(bool)
        & (d[top_col] > 0)
        & (d[low_col] > 0)
        & (d[range_col] <= trigger.max_box_range)
        & (d["amount_ma5_prev"] > 0)
    ].copy()
    if d.empty:
        return d
    price_ok = (d["close_bar"] >= d[top_col] * trigger.breakout_buffer) & (d["close_bar"] <= d[top_col] * (1.0 + trigger.max_above_top))
    vol_ok = d["amount"] >= d["amount_ma5_prev"] * trigger.vol_mult
    d = d[price_ok & vol_ok].copy()
    if d.empty:
        return d
    d = d.sort_values(["entry_date", "code", "datetime"]).groupby(["candidate_profile", "entry_date", "code"], as_index=False).first()
    d["trigger_profile"] = trigger.name
    d["pattern"] = d["candidate_profile"] + "__" + d["trigger_profile"]
    d["trigger_type"] = "breakout_research"
    d["setup_type"] = trigger.setup_type
    d["box_top"] = d[top_col]
    d["box_low"] = d[low_col]
    d["box_range"] = d[range_col]
    d["confirm_datetime"] = d["datetime"]
    d["entry_price"] = d["close_bar"]
    d["confirm_amount"] = d["amount"]
    d["confirm_amount_ma5_prev"] = d["amount_ma5_prev"]
    d["volume_ratio"] = d["confirm_amount"] / d["confirm_amount_ma5_prev"]
    d["breakout_pct"] = d["entry_price"] / d["box_top"] - 1.0
    return d


def _build_signals(events: pd.DataFrame, output_dir: Path, start_date: str, end_date: str) -> pd.DataFrame:
    candidates = pd.concat([_select_candidates(events, p) for p in _candidate_profiles()], ignore_index=True)
    candidates.to_csv(output_dir / "candidates.csv", index=False, encoding="utf-8-sig")
    if candidates.empty:
        return pd.DataFrame()
    bars = _load_signal_bars(candidates["code"].dropna().astype(str).unique().tolist(), start_date, end_date, 30)
    bars = bars.rename(columns={"close": "close_bar"})
    joined = candidates.merge(bars, on=["code", "entry_date"], how="inner", suffixes=("", "_bar"))
    signal_frames = [_apply_breakout_trigger(joined, t) for t in _breakout_triggers()]
    signals = pd.concat([x for x in signal_frames if not x.empty], ignore_index=True) if signal_frames else pd.DataFrame()
    if signals.empty:
        return signals
    keep = [
        "trade_date",
        "entry_date",
        "code",
        "name",
        "v4_rank",
        "v4_score",
        "entry_price",
        "confirm_datetime",
        "confirm_amount",
        "confirm_amount_ma5_prev",
        "volume_ratio",
        "candidate_profile",
        "trigger_profile",
        "pattern",
        "trigger_type",
        "setup_type",
        "box_top",
        "box_low",
        "box_range",
        "breakout_pct",
        "big_bull_lag_days",
        "g2_open_state",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "close_vs_prior20_high",
        "close_vs_prior60_high",
    ]
    signals = signals[keep].sort_values(["entry_date", "confirm_datetime", "v4_rank", "code"]).reset_index(drop=True)
    signals.to_parquet(output_dir / "signals.parquet", index=False)
    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    return signals


def _segment(date_value: Any) -> str:
    d = pd.to_datetime(date_value, errors="coerce")
    if pd.isna(d):
        return "unknown"
    if d < pd.Timestamp("2025-04-01"):
        return "2024H2-2025Q1"
    if d < pd.Timestamp("2026-01-01"):
        return "2025Q2-Q4"
    return "2026YTD"


def _write_report(output_dir: Path, rows: list[dict[str, Any]], signals: pd.DataFrame) -> None:
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    lines = [
        "# G2 Breakout Buy Point Research",
        "",
        "Research only. These signals are not connected to production buy logic.",
        "",
        "| combo | signals | trades | total | excess | max_dd | win | avg_trade | exits |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['combo']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('exit_reason_counts') or ''} |"
        )
    if not signals.empty:
        lines.extend(["", "## Signal Forward Stats", ""])
        signals["segment"] = signals["entry_date"].map(_segment)
        for name, group_cols in {
            "by_pattern": ["pattern"],
            "by_segment_pattern": ["segment", "pattern"],
        }.items():
            agg_spec: dict[str, tuple[str, str]] = {
                "signals": ("code", "count"),
                "avg_breakout": ("breakout_pct", "mean"),
                "avg_setup_range": ("box_range", "mean"),
                "avg_volume_ratio": ("volume_ratio", "mean"),
            }
            if {"fwd_ret_5d", "fwd_ret_10d"}.issubset(signals.columns):
                agg_spec.update(
                    {
                        "avg_fwd5": ("fwd_ret_5d", "mean"),
                        "avg_fwd10": ("fwd_ret_10d", "mean"),
                    }
                )
            stats = (
                signals.groupby(group_cols, dropna=False)
                .agg(**agg_spec)
                .reset_index()
            )
            stats.to_csv(output_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
            lines.append(f"- `{name}.csv` written.")
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run(
    event_dataset: Path,
    state_daily: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    skip_backtest: bool = False,
    patterns: set[str] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_dates = _load_trade_dates("2010-01-01", end_date)
    events = _prepare_events(event_dataset, state_daily, start_date, end_date, trade_dates)
    signals = _build_signals(events, output_dir, start_date, end_date)
    rows: list[dict[str, Any]] = []
    if not signals.empty:
        _write_signal_event_study(signals, output_dir, start_date, end_date)
        grouped = signals.groupby("pattern", sort=True)
        for combo, combo_signals in grouped:
            if skip_backtest:
                continue
            if patterns and combo not in patterns:
                continue
            summary = _run_backtest(
                signals=combo_signals.copy(),
                output_dir=output_dir / "runs" / combo,
                start_date=start_date,
                end_date=end_date,
                period=30,
                sort_mode="trigger_time",
            )
            summary["combo"] = combo
            rows.append(summary)
    _write_report(output_dir, rows, signals)
    payload = {
        "schema_version": 1,
        "research_only": True,
        "strategy_name": "G2 breakout buy point research",
        "start_date": start_date,
        "end_date": end_date,
        "candidate_profiles": [p.__dict__ for p in _candidate_profiles()],
        "trigger_profiles": [p.__dict__ for p in _breakout_triggers()],
        "candidate_count": int(len(pd.read_csv(output_dir / "candidates.csv"))) if (output_dir / "candidates.csv").exists() else 0,
        "signal_count": int(len(signals)),
        "skip_backtest": bool(skip_backtest),
        "patterns": sorted(patterns) if patterns else [],
        "rows": rows,
        "outputs": {
            "candidates": "candidates.csv",
            "signals": "signals.csv",
            "summary": "summary.csv",
            "report": "report.md",
        },
        "notes": [
            "Research-only breakout candidates; not wired into API, frontend, live gate, or production buy logic.",
            "Setup top/low use prior daily bars through T-1 only; signal confirmation uses same-day 30m bars at or before confirm_datetime.",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Research G2 breakout buy points.")
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    add_end_date_argument(parser)
    parser.add_argument("--skip-backtest", action="store_true", help="Only build signals and event-study outputs.")
    parser.add_argument(
        "--patterns",
        default="",
        help="Comma-separated pattern names to backtest. Empty means all patterns.",
    )
    args = parser.parse_args()
    patterns = {x.strip() for x in args.patterns.split(",") if x.strip()} or None
    payload = run(
        Path(args.event_dataset),
        Path(args.state_daily),
        Path(args.output_dir),
        args.start_date,
        resolve_end_date(args.end_date),
        skip_backtest=args.skip_backtest,
        patterns=patterns,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
