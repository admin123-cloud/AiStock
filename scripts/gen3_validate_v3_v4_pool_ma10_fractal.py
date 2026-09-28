from __future__ import annotations

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
    _json_default,
    _load_signal_bars,
    _load_trade_dates,
    _pct,
    _write_signal_event_study,
)
from utils.market_warehouse import clickhouse_client  # noqa: E402

DEFAULT_EVENT_DATASET = REPO_ROOT / "reports" / "gen2_event_study_full" / "v4_event_dataset.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen3_v3_v4_pool_ma10_fractal"


@dataclass(frozen=True)
class CandidateProfile:
    name: str
    source: str
    max_rank: int
    min_score: float = 0.0


def _candidate_profiles() -> list[CandidateProfile]:
    return [
        CandidateProfile("v4_pool_rank100", "in_score_pool", 100),
    ]


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
    df["stock_ma10"] = grouped.transform(lambda s: s.rolling(10, min_periods=10).mean())
    df["stock_ma20"] = grouped.transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["stock_ma20_slope5"] = df.groupby("code")["stock_ma20"].transform(lambda s: s / s.shift(5) - 1.0)
    return df[["code", "trade_date", "stock_ma10", "stock_ma20", "stock_ma20_slope5"]]


def _prepare_events(
    event_dataset: Path,
    start_date: str,
    end_date: str,
    trade_dates: list[str],
) -> pd.DataFrame:
    raw = pd.read_parquet(event_dataset)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw = raw[(raw["trade_date"] >= "2010-01-01") & (raw["trade_date"] <= end_date)].copy()
    raw["v4_rank"] = pd.to_numeric(raw["v4_rank"], errors="coerce").fillna(9999).astype(int)
    raw = raw[raw["v4_rank"] <= 150].copy()
    for col in ["v4_score", "close", "mom5", "mom10", "mom20", "vol_ratio", "vol10"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw["entry_pass"] = raw["entry_pass"].fillna(False).astype(bool)
    raw["in_score_pool"] = raw["in_score_pool"].fillna(False).astype(bool)

    features = _load_daily_features(raw["code"].dropna().astype(str).unique().tolist(), start_date, end_date)
    raw = raw.merge(features, on=["code", "trade_date"], how="left")
    next_date = {trade_dates[i]: trade_dates[i + 1] for i in range(len(trade_dates) - 1)}
    raw["entry_date"] = raw["trade_date"].map(next_date)
    raw = raw.dropna(subset=["entry_date", "stock_ma10"]).copy()
    raw = raw[(raw["entry_date"] >= start_date) & (raw["entry_date"] <= end_date)].copy()
    return raw.reset_index(drop=True)


def _select_candidates(events: pd.DataFrame, profile: CandidateProfile) -> pd.DataFrame:
    d = events[
        events[profile.source].astype(bool)
        & (events["v4_rank"] <= profile.max_rank)
        & (events["v4_score"] >= profile.min_score)
        & (events["close"] >= events["stock_ma10"])
    ].copy()
    d["candidate_profile"] = profile.name
    return d.reset_index(drop=True)


def _find_ma10_bottom_fractals(joined: pd.DataFrame) -> pd.DataFrame:
    if joined.empty:
        return joined
    rows: list[dict[str, Any]] = []
    for (profile, entry_date, code), group in joined.groupby(["candidate_profile", "entry_date", "code"], sort=False):
        bars = group.sort_values("datetime").reset_index(drop=True).copy()
        prev_low = bars["low"].shift(1)
        next_low = bars["low"].shift(-1)
        bars["is_bottom_fractal"] = bars["low"].lt(prev_low) & bars["low"].le(next_low)
        bars["confirm_datetime"] = bars["datetime"].shift(-1)
        bars["confirm_close"] = bars["close_bar"].shift(-1)
        bars["confirm_amount"] = bars["amount"].shift(-1)
        bars["confirm_amount_ma5_prev"] = bars["amount_ma5_prev"].shift(-1)
        ma10 = pd.to_numeric(bars["stock_ma10"], errors="coerce")
        bars["touch_ma10_no_break"] = ma10.gt(0) & bars["low"].ge(ma10) & bars["low"].le(ma10 * 1.025)
        hits = bars[bars["is_bottom_fractal"] & bars["touch_ma10_no_break"] & bars["confirm_datetime"].notna()].copy()
        if hits.empty:
            continue
        first = hits.iloc[0].to_dict()
        first["candidate_profile"] = profile
        first["pattern"] = f"{profile}__ma10_no_break_bottom_fractal_30m"
        first["trigger_profile"] = "ma10_no_break_bottom_fractal_30m"
        first["trigger_type"] = "bottom_fractal_ma10_no_break"
        first["entry_price"] = float(first["confirm_close"])
        first["confirm_amount"] = float(first["confirm_amount"]) if pd.notna(first.get("confirm_amount")) else np.nan
        first["confirm_amount_ma5_prev"] = (
            float(first["confirm_amount_ma5_prev"]) if pd.notna(first.get("confirm_amount_ma5_prev")) else np.nan
        )
        first["volume_ratio"] = (
            first["confirm_amount"] / first["confirm_amount_ma5_prev"]
            if first["confirm_amount_ma5_prev"] and first["confirm_amount_ma5_prev"] > 0
            else np.nan
        )
        rows.append(first)
    return pd.DataFrame(rows)


def _build_signals(events: pd.DataFrame, output_dir: Path, start_date: str, end_date: str) -> pd.DataFrame:
    candidates = pd.concat([_select_candidates(events, p) for p in _candidate_profiles()], ignore_index=True)
    candidates.to_csv(output_dir / "candidates.csv", index=False, encoding="utf-8-sig")
    if candidates.empty:
        return pd.DataFrame()
    bars = _load_signal_bars(candidates["code"].dropna().astype(str).unique().tolist(), start_date, end_date, 30)
    if bars.empty:
        return pd.DataFrame()
    bars = bars.rename(columns={"close": "close_bar"})
    context = candidates[["code", "entry_date", "stock_ma10"]].drop_duplicates(["code", "entry_date"]).copy()
    joined = bars.merge(context, on=["code", "entry_date"], how="inner")
    if joined.empty:
        return pd.DataFrame()
    joined = joined.sort_values(["code", "entry_date", "datetime"]).reset_index(drop=True)
    group = joined.groupby(["code", "entry_date"], sort=False)
    joined["is_bottom_fractal"] = joined["low"].lt(group["low"].shift(1)) & joined["low"].le(group["low"].shift(-1))
    joined["confirm_datetime"] = group["datetime"].shift(-1)
    joined["confirm_close"] = group["close_bar"].shift(-1)
    joined["confirm_amount"] = group["amount"].shift(-1)
    joined["confirm_amount_ma5_prev"] = group["amount_ma5_prev"].shift(-1)
    ma10 = pd.to_numeric(joined["stock_ma10"], errors="coerce")
    joined["touch_ma10_no_break"] = ma10.gt(0) & joined["low"].ge(ma10) & joined["low"].le(ma10 * 1.025)
    hits = joined[joined["is_bottom_fractal"] & joined["touch_ma10_no_break"] & joined["confirm_datetime"].notna()].copy()
    if hits.empty:
        return pd.DataFrame()
    hits = hits.sort_values(["entry_date", "code", "datetime"]).groupby(["entry_date", "code"], as_index=False).first()
    hit_cols = [
        "entry_date",
        "code",
        "confirm_datetime",
        "confirm_close",
        "confirm_amount",
        "confirm_amount_ma5_prev",
        "low",
        "close_bar",
        "touch_ma10_no_break",
    ]
    signals = candidates.merge(hits[hit_cols], on=["code", "entry_date"], how="inner")
    signals["trigger_profile"] = "ma10_no_break_bottom_fractal_30m"
    signals["trigger_type"] = "bottom_fractal_ma10_no_break"
    signals["pattern"] = signals["candidate_profile"] + "__ma10_no_break_bottom_fractal_30m"
    signals["entry_price"] = signals["confirm_close"]
    signals["volume_ratio"] = signals["confirm_amount"] / signals["confirm_amount_ma5_prev"]
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
        "mom5",
        "mom10",
        "mom20",
        "vol_ratio",
        "vol10",
        "stock_ma10",
        "stock_ma20",
        "stock_ma20_slope5",
        "low",
        "close_bar",
        "touch_ma10_no_break",
    ]
    signals = signals[keep].sort_values(["entry_date", "confirm_datetime", "v4_rank", "code"]).reset_index(drop=True)
    signals.to_parquet(output_dir / "signals.parquet", index=False)
    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    return signals


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("avg_ret_5d", ascending=False)
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    lines = [
        "# G3 V3 V4-Pool MA10 Bottom-Fractal Validation",
        "",
        "No timing layer. Candidates are previous-day V4 rows; entry uses signal-day 30m bottom-fractal confirmation after MA10 pullback without breaking T-1 MA10.",
        "",
        "| pattern | signals | avg1d | win1d | avg3d | win3d | avg5d | win5d | avg10d | win10d |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['pattern']} | {int(row.get('signal_count') or 0)} | {_pct(row.get('avg_ret_1d'))} | {_pct(row.get('win_1d'))} | {_pct(row.get('avg_ret_3d'))} | {_pct(row.get('win_3d'))} | {_pct(row.get('avg_ret_5d'))} | {_pct(row.get('win_5d'))} | {_pct(row.get('avg_ret_10d'))} | {_pct(row.get('win_10d'))} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run(event_dataset: Path, output_dir: Path, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_dates = _load_trade_dates("2010-01-01", end_date)
    events = _prepare_events(event_dataset, start_date, end_date, trade_dates)
    signals = _build_signals(events, output_dir, start_date, end_date)
    rows: list[dict[str, Any]] = []
    if not signals.empty:
        _write_signal_event_study(signals, output_dir, start_date, end_date)
        event_summary_path = output_dir / "signal_event_study.csv"
        if event_summary_path.exists():
            rows = pd.read_csv(event_summary_path).to_dict("records")
    _write_report(output_dir, rows)
    payload = {
        "schema_version": 1,
        "strategy_name": "G3 V3: V4 pool MA10 no-break 30m bottom fractal",
        "start_date": start_date,
        "end_date": end_date,
        "candidate_profiles": [p.__dict__ for p in _candidate_profiles()],
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
            "No market-timing or G2/G3 strong-zone filter is used.",
            "Candidate context uses T-1 V4 rows; MA10 is the T-1 daily MA10.",
            "The 30m bottom fractal is confirmed by the next 30m bar, and the buy price uses that confirmation bar close.",
            "MA10 pullback condition: fractal-bar low >= T-1 MA10 and low <= T-1 MA10 * 1.025.",
            "This run is an event study only; the slower 30m portfolio exit engine is intentionally skipped.",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate G3 V3 V4-pool MA10 no-break 30m bottom-fractal strategy.")
    parser.add_argument("--event-dataset", default=str(DEFAULT_EVENT_DATASET))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    args = parser.parse_args()
    payload = run(
        event_dataset=Path(args.event_dataset),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
