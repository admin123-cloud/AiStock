from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _pct  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402

DEFAULT_LABELED_SIGNALS = REPO_ROOT / "reports" / "gen2_v4_factor_profile" / "pullback_signal_factor_labeled.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pullback_factor_filter_sweep"

FACTOR_COLUMNS = [
    "mom20",
    "ma20_gap",
    "ma60_gap",
    "volatility20",
    "intraday_strength_live",
    "close_position_live",
    "volume_ratio",
    "vol_ratio",
]


def _load_labeled_signals(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = df[(df["entry_date"] >= start_date) & (df["entry_date"] <= end_date)].copy()
    for col in FACTOR_COLUMNS:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d = d.dropna(subset=["entry_date", "code", "entry_price", "confirm_datetime"]).reset_index(drop=True)
    return _attach_live_quality(d, start_date, end_date)


def _attach_live_quality(signals: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client

    if signals.empty:
        signals["intraday_strength_live"] = np.nan
        signals["close_position_live"] = np.nan
        return signals
    codes = sorted(signals["code"].dropna().astype(str).unique().tolist())
    quoted = ", ".join([f"'{code}'" for code in codes])
    ch = clickhouse_client()
    bars = ch.query_df(
        f"""
        SELECT code, datetime, open, high, low, close
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime >= toDateTime('{start_date} 09:30:00')
          AND datetime <= toDateTime('{end_date} 15:00:00')
        ORDER BY code, datetime
        """
    )
    out = signals.copy()
    out["intraday_strength_live"] = np.nan
    out["close_position_live"] = np.nan
    if bars.empty:
        return out

    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["entry_date"] = bars["datetime"].dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "datetime", "open", "high", "low", "close"]).sort_values(["code", "datetime"])
    bar_groups = {(code, date): g.copy() for (code, date), g in bars.groupby(["code", "entry_date"], sort=False)}

    strengths: list[float] = []
    positions: list[float] = []
    for row in out.itertuples(index=False):
        code = str(getattr(row, "code"))
        entry_date = str(getattr(row, "entry_date"))
        confirm_dt = pd.Timestamp(getattr(row, "confirm_datetime"))
        day_bars = bar_groups.get((code, entry_date), pd.DataFrame())
        visible = day_bars[day_bars["datetime"] <= confirm_dt] if not day_bars.empty else pd.DataFrame()
        if visible.empty:
            strengths.append(np.nan)
            positions.append(np.nan)
            continue
        day_open = float(visible.iloc[0]["open"])
        high_so_far = float(visible["high"].max())
        low_so_far = float(visible["low"].min())
        confirm_close = float(visible.iloc[-1]["close"])
        strengths.append(confirm_close / day_open - 1.0 if day_open > 0 else np.nan)
        span = high_so_far - low_so_far
        positions.append((confirm_close - low_so_far) / span if span > 0 else np.nan)
    out["intraday_strength_live"] = strengths
    out["close_position_live"] = positions
    return out


def _rules() -> dict[str, tuple[str, Callable[[pd.DataFrame], pd.Series]]]:
    return {
        "baseline": ("no extra factor filter", lambda d: pd.Series(True, index=d.index)),
        "quality_confirm": (
            "confirm-time intraday_strength >= 3% and close_position >= 0.55",
            lambda d: (d["intraday_strength_live"] >= 0.03) & (d["close_position_live"] >= 0.55),
        ),
        "quality_strong_close": (
            "confirm-time intraday_strength >= 3% and close_position >= 0.60",
            lambda d: (d["intraday_strength_live"] >= 0.03) & (d["close_position_live"] >= 0.60),
        ),
        "not_hot_ma60_mom20": (
            "ma60_gap <= 25% and mom20 <= 25%",
            lambda d: (d["ma60_gap"] <= 0.25) & (d["mom20"] <= 0.25),
        ),
        "not_hot_low_vol": (
            "ma60_gap <= 25%, mom20 <= 25%, volatility20 <= 4.5%",
            lambda d: (d["ma60_gap"] <= 0.25) & (d["mom20"] <= 0.25) & (d["volatility20"] <= 0.045),
        ),
        "combo_balanced": (
            "quality_confirm plus ma60_gap <= 25% and mom20 <= 25%",
            lambda d: (d["intraday_strength_live"] >= 0.03)
            & (d["close_position_live"] >= 0.55)
            & (d["ma60_gap"] <= 0.25)
            & (d["mom20"] <= 0.25),
        ),
        "combo_tight": (
            "quality_strong_close plus ma60_gap <= 22%, mom20 <= 25%, volatility20 <= 4.5%",
            lambda d: (d["intraday_strength_live"] >= 0.03)
            & (d["close_position_live"] >= 0.60)
            & (d["ma60_gap"] <= 0.22)
            & (d["mom20"] <= 0.25)
            & (d["volatility20"] <= 0.045),
        ),
    }


def _factor_snapshot(signals: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in FACTOR_COLUMNS:
        if col not in signals.columns:
            continue
        x = pd.to_numeric(signals[col], errors="coerce").dropna()
        if x.empty:
            continue
        out[col] = {
            "mean": float(x.mean()),
            "median": float(x.median()),
            "p25": float(x.quantile(0.25)),
            "p75": float(x.quantile(0.75)),
        }
    return out


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Pullback Factor Filter Sweep",
        "",
        "Input uses the intraday NORMAL, before-confirm signal set with Alpha191-style proxy factors.",
        "Confirm-day quality factors are recomputed from 30m bars visible at confirm_datetime to avoid same-day close lookahead.",
        "Only extra signal filters change. Entry, sizing and realistic previous-low exit fill remain unchanged.",
        "",
        "| rule | signals | trades | total | excess | max_dd | win | avg_trade | note |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('rule_note', '')} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(labeled_signals: Path, output_dir: Path, start_date: str, end_date: str, rule_keys: set[str] | None = None) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_labeled_signals(labeled_signals, start_date, end_date)
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    rows: list[dict[str, Any]] = []

    selected_rules = _rules()
    if rule_keys:
        missing = sorted(rule_keys - set(selected_rules))
        if missing:
            raise ValueError(f"Unknown rules: {missing}")
        selected_rules = {key: selected_rules[key] for key in selected_rules if key in rule_keys}
    for key, (note, mask_fn) in selected_rules.items():
        mask = mask_fn(signals).fillna(False)
        filtered = signals[mask].copy()
        rule_dir = output_dir / key
        rule_dir.mkdir(parents=True, exist_ok=True)
        signal_path = rule_dir / "signals.parquet"
        filtered.to_parquet(signal_path, index=False)
        filtered.to_csv(rule_dir / "signals.csv", index=False, encoding="utf-8-sig")
        (rule_dir / "factor_snapshot.json").write_text(
            json.dumps(_factor_snapshot(filtered), ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        summary = _run_profile(
            signal_source=signal_path,
            output_dir=rule_dir / "backtest",
            profile=profile,
            start_date=start_date,
            end_date=end_date,
        )
        summary["key"] = key
        summary["rule_note"] = note
        summary["filtered_signal_rows"] = int(len(filtered))
        rows.append(summary)

    summary_rows = []
    for row in rows:
        summary_rows.append(
            {
                "key": row["key"],
                "rule_note": row["rule_note"],
                "filtered_signal_rows": row["filtered_signal_rows"],
                "signal_count": row.get("signal_count"),
                "trade_count": row.get("trade_count"),
                "final_equity": row.get("final_equity"),
                "total_return": row.get("total_return"),
                "benchmark_return": row.get("benchmark_return"),
                "excess_return": row.get("excess_return"),
                "max_drawdown": row.get("max_drawdown"),
                "win_rate": row.get("win_rate"),
                "avg_trade_return": row.get("avg_trade_return"),
                "exit_reason_counts": json.dumps(row.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
            }
        )
    summary_df = pd.DataFrame(summary_rows).sort_values("total_return", ascending=False)
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "labeled_signals": str(labeled_signals),
        "profile": profile.__dict__,
        "rows": summary_df.replace({np.nan: None}).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "findings.md"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary_df.to_dict("records"))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep light factor filters for G2 pullback confirmation signals.")
    parser.add_argument("--labeled-signals", default=str(DEFAULT_LABELED_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--rules", default="", help="Comma-separated rule keys. Empty means all rules.")
    args = parser.parse_args()
    rule_keys = {item.strip() for item in str(args.rules).split(",") if item.strip()} if str(args.rules).strip() else None
    payload = run(Path(args.labeled_signals), Path(args.output_dir), str(args.start_date), str(args.end_date), rule_keys=rule_keys)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
