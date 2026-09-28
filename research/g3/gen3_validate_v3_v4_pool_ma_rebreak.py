from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = _PROJECT_ROOT
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
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_EVENT_DATASET = _report_path() / "gen2_event_study_full" / "v4_event_dataset.parquet"
DEFAULT_STATE_DAILY = _report_path() / "gen2_open_state_research_full" / "g2_open_state_daily.csv"
DEFAULT_OUTPUT_DIR = _report_path() / "gen3_v3_v4_pool_ma_rebreak"


@dataclass(frozen=True)
class CandidateProfile:
    name: str
    source: str
    max_rank: int
    min_score: float
    min_mom20: float
    min_mom10: float
    min_mom5: float
    max_mom5: float
    max_dist_ma20: float
    max_vol_ratio: float
    max_vol10: float
    require_state: str = "AGGRESSIVE"


def _candidate_profiles() -> list[CandidateProfile]:
    return [
        CandidateProfile(
            name="g3_v4_pool_rank100_strong_ma",
            source="in_score_pool",
            max_rank=100,
            min_score=0.0,
            min_mom20=0.04,
            min_mom10=0.03,
            min_mom5=-0.02,
            max_mom5=0.10,
            max_dist_ma20=0.20,
            max_vol_ratio=3.0,
            max_vol10=0.09,
        ),
        CandidateProfile(
            name="g3_v4_entry_rank100_strong_ma",
            source="entry_pass",
            max_rank=100,
            min_score=0.0,
            min_mom20=0.04,
            min_mom10=0.03,
            min_mom5=-0.02,
            max_mom5=0.10,
            max_dist_ma20=0.20,
            max_vol_ratio=3.0,
            max_vol10=0.09,
        ),
        CandidateProfile(
            name="g3_v4_pool_rank50_strong_ma",
            source="in_score_pool",
            max_rank=50,
            min_score=0.0,
            min_mom20=0.05,
            min_mom10=0.04,
            min_mom5=-0.015,
            max_mom5=0.09,
            max_dist_ma20=0.18,
            max_vol_ratio=2.6,
            max_vol10=0.08,
        ),
    ]


def _trigger_profiles() -> list[TriggerProfile]:
    return [
        TriggerProfile("near_ma5_or_ma10_break_prev_bar_high_vol12", "break_prev_bar_high", 1.2, "10:00:00"),
        TriggerProfile("near_ma5_or_ma10_break_day_high_vol12", "break_day_high", 1.2, "10:00:00"),
        TriggerProfile("near_ma5_or_ma10_break_open_range_high_vol12", "break_open_range_high", 1.2, "10:30:00"),
    ]


def _load_states(path: Path) -> pd.DataFrame:
    states = pd.read_csv(path)
    states["trade_date"] = pd.to_datetime(states["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return states[["trade_date", "g2_open_state"]].dropna(subset=["trade_date"]).copy()


def _load_daily_features(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN subtractDays(toDate('{start_date}'), 80) AND toDate('{end_date}')
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "close"]).sort_values(["code", "trade_date"]).reset_index(drop=True)
    grouped = df.groupby("code")["close"]
    df["stock_ma5"] = grouped.transform(lambda s: s.rolling(5, min_periods=5).mean())
    df["stock_ma10"] = grouped.transform(lambda s: s.rolling(10, min_periods=10).mean())
    df["stock_ma20"] = grouped.transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["stock_ma20_slope5"] = df.groupby("code")["stock_ma20"].transform(lambda s: s / s.shift(5) - 1.0)
    df["dist_ma20"] = df["close"] / df["stock_ma20"] - 1.0
    return df[["code", "trade_date", "stock_ma5", "stock_ma10", "stock_ma20", "stock_ma20_slope5", "dist_ma20"]]


def _prepare_events(
    event_dataset: Path,
    state_daily: Path,
    start_date: str,
    end_date: str,
    trade_dates: list[str],
) -> pd.DataFrame:
    raw = pd.read_parquet(event_dataset)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw = raw[(raw["trade_date"] >= "2010-01-01") & (raw["trade_date"] <= end_date)].copy()
    raw["v4_rank"] = pd.to_numeric(raw["v4_rank"], errors="coerce").fillna(9999).astype(int)
    raw = raw[raw["v4_rank"] <= 150].copy()
    for col in ["v4_score", "mom5", "mom10", "mom20", "vol_ratio", "vol10", "close"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw["entry_pass"] = raw["entry_pass"].astype(bool)
    raw["in_score_pool"] = raw["in_score_pool"].astype(bool)

    raw = raw.merge(_load_states(state_daily), on="trade_date", how="left")
    features = _load_daily_features(raw["code"].dropna().astype(str).unique().tolist(), start_date, end_date)
    raw = raw.merge(features, on=["code", "trade_date"], how="left")

    next_date = {trade_dates[i]: trade_dates[i + 1] for i in range(len(trade_dates) - 1)}
    raw["entry_date"] = raw["trade_date"].map(next_date)
    raw = raw.dropna(subset=["entry_date"]).copy()
    raw = raw[(raw["entry_date"] >= start_date) & (raw["entry_date"] <= end_date)].copy()
    return raw.reset_index(drop=True)


def _select_candidates(events: pd.DataFrame, profile: CandidateProfile) -> pd.DataFrame:
    source_mask = events[profile.source].fillna(False).astype(bool)
    d = events[
        source_mask
        & (events["v4_rank"] <= profile.max_rank)
        & (events["v4_score"] >= profile.min_score)
        & (events["g2_open_state"] == profile.require_state)
        & (events["mom20"] >= profile.min_mom20)
        & (events["mom10"] >= profile.min_mom10)
        & (events["mom5"] >= profile.min_mom5)
        & (events["mom5"] <= profile.max_mom5)
        & (events["vol_ratio"] <= profile.max_vol_ratio)
        & (events["vol10"] <= profile.max_vol10)
        & (events["close"] >= events["stock_ma20"])
        & (events["stock_ma20_slope5"] > 0)
        & (events["dist_ma20"] <= profile.max_dist_ma20)
    ].copy()
    d["candidate_profile"] = profile.name
    return d.reset_index(drop=True)


def _near_ma_mask(d: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(d["close_bar"], errors="coerce")
    low = pd.to_numeric(d["low"], errors="coerce")
    ma5 = pd.to_numeric(d["stock_ma5"], errors="coerce")
    ma10 = pd.to_numeric(d["stock_ma10"], errors="coerce")
    near_ma5 = ma5.gt(0) & low.le(ma5 * 1.025) & close.ge(ma5 * 0.995)
    near_ma10 = ma10.gt(0) & low.le(ma10 * 1.025) & close.ge(ma10 * 0.995)
    d["near_ma5"] = near_ma5
    d["near_ma10"] = near_ma10
    return near_ma5 | near_ma10


def _apply_trigger(joined: pd.DataFrame, trigger: TriggerProfile) -> pd.DataFrame:
    d = joined[joined["bar_time"] >= trigger.min_time].copy()
    if d.empty:
        return d
    d = d[d["amount_ma5_prev"] > 0].copy()
    d = d[_near_ma_mask(d)].copy()
    if d.empty:
        return d
    vol_ok = d["amount"] >= d["amount_ma5_prev"] * trigger.vol_mult
    if trigger.trigger_type == "break_day_high":
        price_ok = d["close_bar"] > d["prev_intraday_high"]
    elif trigger.trigger_type == "break_open_range_high":
        price_ok = d["close_bar"] > d["open_range_high"]
    elif trigger.trigger_type == "break_prev_bar_high":
        price_ok = d["close_bar"] > d["prev_bar_high"]
    else:
        raise ValueError(f"Unsupported trigger type: {trigger.trigger_type}")
    d = d[vol_ok & price_ok].copy()
    if d.empty:
        return d
    d = d.sort_values(["entry_date", "code", "datetime"]).groupby(["candidate_profile", "entry_date", "code"], as_index=False).first()
    d["trigger_profile"] = trigger.name
    d["pattern"] = d["candidate_profile"] + "__" + d["trigger_profile"]
    d["trigger_type"] = trigger.trigger_type
    d["confirm_datetime"] = d["datetime"]
    d["entry_price"] = d["close_bar"]
    d["confirm_amount"] = d["amount"]
    d["confirm_amount_ma5_prev"] = d["amount_ma5_prev"]
    d["volume_ratio"] = d["confirm_amount"] / d["confirm_amount_ma5_prev"]
    return d


def _build_signals(events: pd.DataFrame, output_dir: Path, start_date: str, end_date: str) -> pd.DataFrame:
    candidates = pd.concat([_select_candidates(events, p) for p in _candidate_profiles()], ignore_index=True)
    candidates.to_csv(output_dir / "candidates.csv", index=False, encoding="utf-8-sig")
    if candidates.empty:
        return pd.DataFrame()
    bars = _load_signal_bars(candidates["code"].dropna().astype(str).unique().tolist(), start_date, end_date)
    joined = candidates.merge(bars, on=["code", "entry_date"], how="inner", suffixes=("", "_bar"))
    signal_frames = [_apply_trigger(joined, t) for t in _trigger_profiles()]
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
        "g2_open_state",
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "stock_ma5",
        "stock_ma10",
        "dist_ma20",
        "stock_ma20_slope5",
        "near_ma5",
        "near_ma10",
    ]
    signals = signals[keep].sort_values(["entry_date", "confirm_datetime", "v4_rank", "code"]).reset_index(drop=True)
    signals.to_parquet(output_dir / "signals.parquet", index=False)
    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    return signals


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    lines = [
        "# G3 V3 V4-Pool Strong-Zone MA Re-Break Validation",
        "",
        "All entries use previous-day V4 pool context plus signal-day 30m visible confirmation.",
        "",
        "| combo | signals | trades | total | excess | max_dd | win | avg_trade | exits |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['combo']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('exit_reason_counts') or ''} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run(event_dataset: Path, state_daily: Path, output_dir: Path, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_dates = _load_trade_dates("2010-01-01", end_date)
    events = _prepare_events(event_dataset, state_daily, start_date, end_date, trade_dates)
    signals = _build_signals(events, output_dir, start_date, end_date)
    rows: list[dict[str, Any]] = []
    if not signals.empty:
        _write_signal_event_study(signals, output_dir, start_date, end_date)
        for combo, combo_signals in signals.groupby("pattern", sort=True):
            summary = _run_backtest(
                signals=combo_signals.copy(),
                output_dir=output_dir / "runs" / combo,
                start_date=start_date,
                end_date=end_date,
                sort_mode="trigger_time",
            )
            summary["combo"] = combo
            rows.append(summary)
    _write_report(output_dir, rows)
    payload = {
        "schema_version": 1,
        "strategy_name": "G3 V3: V4 pool strong-zone MA5/MA10 re-break",
        "start_date": start_date,
        "end_date": end_date,
        "candidate_profiles": [p.__dict__ for p in _candidate_profiles()],
        "trigger_profiles": [p.__dict__ for p in _trigger_profiles()],
        "candidate_count": int(len(pd.read_csv(output_dir / "candidates.csv"))) if (output_dir / "candidates.csv").exists() else 0,
        "signal_count": int(len(signals)),
        "rows": rows,
        "outputs": {
            "candidates": "candidates.csv",
            "signals": "signals.csv",
            "summary": "summary.csv",
            "report": "report.md",
            "runs": "runs/",
        },
        "notes": [
            "Candidate context uses T-1 V4 score-pool or entry-pass rows and T-1 AGGRESSIVE strong-zone state.",
            "MA5/MA10 proximity uses T-1 daily MA values; signal confirmation uses only signal-day 30m bars at or before confirm_datetime.",
            "Entry requires price near MA5 or MA10 and another 30m volume-expansion breakout.",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate G3 V3 V4-pool strong-zone MA5/MA10 re-break strategy.")
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    args = parser.parse_args()
    payload = run(
        event_dataset=Path(args.event_dataset),
        state_daily=Path(args.state_daily),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
