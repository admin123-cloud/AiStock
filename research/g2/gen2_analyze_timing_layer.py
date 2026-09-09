from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402


DEFAULT_SIGNALS = (
    _report_path() / "gen2_risk_cool_dynamic_circuit_user_v2_cap_v2" / "sources" / "risk_cool_base.parquet"
)
DEFAULT_OUTPUT_DIR = _report_path() / "gen2_user_v2_timing_layer"


def _num(value: Any, default: float = np.nan) -> float:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _load_daily(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()
    quoted = ", ".join(f"'{code}'" for code in sorted(set(codes)))
    start = (pd.Timestamp(start_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(end_date) + pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    ch = clickhouse_client()
    d = ch.query_df(
        f"""
        SELECT code, trade_date, open, high, low, close, volume, amount
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start}' AND '{end}'
        ORDER BY code, trade_date
        """
    )
    if d.empty:
        return d
    d["code"] = d["code"].astype(str)
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)


def _rolling_breakouts(history: pd.DataFrame) -> pd.DataFrame:
    d = history.copy().sort_values("trade_date").reset_index(drop=True)
    d["prior20_high"] = d["high"].rolling(20, min_periods=10).max().shift(1)
    d["prior60_high"] = d["high"].rolling(60, min_periods=30).max().shift(1)
    d["amount_ma5_prev"] = d["amount"].rolling(5, min_periods=3).mean().shift(1)
    d["break20"] = d["high"] >= d["prior20_high"] * 0.995
    d["break60"] = d["high"] >= d["prior60_high"] * 0.995
    d["amount_expand"] = d["amount"] >= d["amount_ma5_prev"] * 1.20
    d["strong_break20"] = d["break20"] & (d["close"] >= d["prior20_high"] * 0.985) & d["amount_expand"]
    d["strong_break60"] = d["break60"] & (d["close"] >= d["prior60_high"] * 0.985) & d["amount_expand"]
    return d


def _classify_signal(row: pd.Series, daily: pd.DataFrame) -> dict[str, Any]:
    code = str(row["code"])
    entry_date = str(row["entry_date"])
    entry_price = _num(row.get("entry_price"))
    result: dict[str, Any] = {
        "live_timing_tag": "unknown",
        "live_timing_note": "",
        "hindsight_timing_tag": "unknown",
        "hindsight_timing_note": "",
        "entry_break20": False,
        "entry_break60": False,
        "entry_vs_prior20_high": np.nan,
        "entry_vs_prior60_high": np.nan,
        "recent_strong_break_date": "",
        "recent_strong_break_age": np.nan,
        "next_strong_break_date": "",
        "next_strong_break_wait_days": np.nan,
    }
    if daily.empty or not math.isfinite(entry_price) or entry_price <= 0:
        result["live_timing_note"] = "missing_daily_or_price"
        result["hindsight_timing_note"] = "missing_daily_or_price"
        return result

    d = _rolling_breakouts(daily)
    prior = d[d["trade_date"] < entry_date].copy()
    current = d[d["trade_date"] == entry_date].tail(1)
    future = d[d["trade_date"] > entry_date].head(15).copy()
    if prior.empty:
        result["live_timing_note"] = "no_prior_daily"
        result["hindsight_timing_note"] = "no_prior_daily"
        return result

    prior20 = _num(prior["high"].tail(20).max())
    prior60 = _num(prior["high"].tail(60).max())
    result["entry_vs_prior20_high"] = entry_price / prior20 - 1.0 if prior20 > 0 else np.nan
    result["entry_vs_prior60_high"] = entry_price / prior60 - 1.0 if prior60 > 0 else np.nan
    result["entry_break20"] = bool(prior20 > 0 and entry_price >= prior20 * 0.995)
    result["entry_break60"] = bool(prior60 > 0 and entry_price >= prior60 * 0.995)

    recent = d[(d["trade_date"] <= entry_date) & (d["strong_break20"] | d["strong_break60"])].tail(1)
    if not recent.empty:
        break_date = str(recent.iloc[0]["trade_date"])
        result["recent_strong_break_date"] = break_date
        result["recent_strong_break_age"] = int(
            len(d[(d["trade_date"] > break_date) & (d["trade_date"] <= entry_date)])
        )

    next_break = future[future["strong_break20"] | future["strong_break60"]].head(1)
    if not next_break.empty:
        next_date = str(next_break.iloc[0]["trade_date"])
        result["next_strong_break_date"] = next_date
        result["next_strong_break_wait_days"] = int(
            len(d[(d["trade_date"] > entry_date) & (d["trade_date"] <= next_date)])
        )

    age = _num(result["recent_strong_break_age"])
    next_wait = _num(result["next_strong_break_wait_days"])
    if result["entry_break60"] and result["entry_break20"] and age == 0:
        live_tag = "first_breakout_confirm"
        live_note = "entry bar is daily 20/60 high breakout confirmation"
    elif result["entry_break20"] and age <= 3:
        live_tag = "early_breakout_follow"
        live_note = "entry follows a very recent breakout"
    elif result["entry_break20"] and result["entry_vs_prior60_high"] < -0.015:
        live_tag = "short_box_attempt"
        live_note = "entry breaks short box but has not cleared the 60d box top"
    elif math.isfinite(age) and 4 <= age <= 12 and result["entry_vs_prior20_high"] >= -0.03:
        live_tag = "second_confirmation"
        live_note = "entry is a pullback/restart after a recent breakout"
    elif result["entry_vs_prior20_high"] < -0.03 and result["entry_vs_prior60_high"] < -0.05:
        live_tag = "weak_inside_range"
        live_note = "entry is still well inside prior range"
    else:
        live_tag = "neutral_confirm"
        live_note = "no strong timing edge detected"
    if (not result["entry_break60"]) and math.isfinite(next_wait) and next_wait <= 10:
        hindsight_tag = "later_breakout_within_10d"
        hindsight_note = "entry is before a later daily breakout confirmation"
    elif result["entry_break60"] and result["entry_break20"] and age == 0:
        hindsight_tag = "same_day_breakout"
        hindsight_note = "entry and daily breakout confirmation are the same day"
    elif math.isfinite(age) and age > 0:
        hindsight_tag = "after_prior_breakout"
        hindsight_note = "entry occurs after a prior daily breakout"
    else:
        hindsight_tag = "no_near_breakout"
        hindsight_note = "no nearby daily breakout confirmation detected"
    result["live_timing_tag"] = live_tag
    result["live_timing_note"] = live_note
    result["hindsight_timing_tag"] = hindsight_tag
    result["hindsight_timing_note"] = hindsight_note
    if not current.empty:
        result["entry_day_close"] = _num(current.iloc[0].get("close"))
        result["entry_day_high"] = _num(current.iloc[0].get("high"))
        result["entry_day_amount"] = _num(current.iloc[0].get("amount"))
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    signals = pd.read_parquet(args.signals)
    signals["code"] = signals["code"].astype(str)
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals = signals.dropna(subset=["code", "entry_date"]).copy()
    if args.start_date:
        signals = signals[signals["entry_date"] >= str(args.start_date)].copy()
    if args.end_date:
        signals = signals[signals["entry_date"] <= str(args.end_date)].copy()
    if args.limit:
        signals = signals.head(int(args.limit)).copy()
    daily = _load_daily(
        signals["code"].dropna().astype(str).unique().tolist(),
        str(signals["entry_date"].min()),
        str(signals["entry_date"].max()),
    )
    by_code = {code: g.reset_index(drop=True) for code, g in daily.groupby("code", sort=False)}
    rows = []
    for _, row in signals.iterrows():
        rows.append(_classify_signal(row, by_code.get(str(row["code"]), pd.DataFrame())))
    audited = pd.concat([signals.reset_index(drop=True), pd.DataFrame(rows)], axis=1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    audited.to_csv(out_dir / "timing_audit.csv", index=False, encoding="utf-8-sig")
    audited.to_parquet(out_dir / "timing_audit.parquet", index=False)

    tag_summary = (
        audited.groupby("live_timing_tag", dropna=False)
        .agg(
            signals=("code", "count"),
            avg_fwd3=("outcome_fwd_ret_3d", "mean"),
            avg_fwd5=("outcome_fwd_ret_5d", "mean"),
            avg_fwd10=("outcome_fwd_ret_10d", "mean"),
            stop5_rate=("stop5_touch_30m", "mean"),
            good_rate=("outcome_good", "mean"),
            bad_rate=("outcome_bad", "mean"),
        )
        .reset_index()
        .sort_values(["avg_fwd10", "signals"], ascending=[False, False])
    )
    tag_summary.to_csv(out_dir / "live_timing_tag_summary.csv", index=False, encoding="utf-8-sig")
    hindsight_summary = (
        audited.groupby(["live_timing_tag", "hindsight_timing_tag"], dropna=False)
        .agg(
            signals=("code", "count"),
            avg_fwd10=("outcome_fwd_ret_10d", "mean"),
            stop5_rate=("stop5_touch_30m", "mean"),
            good_rate=("outcome_good", "mean"),
            bad_rate=("outcome_bad", "mean"),
        )
        .reset_index()
        .sort_values(["live_timing_tag", "signals"], ascending=[True, False])
    )
    hindsight_summary.to_csv(out_dir / "live_vs_hindsight_timing_summary.csv", index=False, encoding="utf-8-sig")

    sample_codes = {
        "002075.SZ": "沙钢股份",
        "002302.SZ": "西部建设",
        "002384.SZ": "东山精密",
        "300033.SZ": "同花顺",
        "001309.SZ": "德明利",
        "601728.SH": "中国电信",
        "301608.SZ": "博实结",
        "000807.SZ": "云铝股份",
    }
    sample = audited[audited["code"].isin(sample_codes)].copy()
    if not sample.empty:
        sample.insert(2, "review_alias", sample["code"].map(sample_codes))
        sample.to_csv(out_dir / "review_sample_timing.csv", index=False, encoding="utf-8-sig")

    payload = {
        "schema_version": 1,
        "signals": int(len(audited)),
        "start_date": str(signals["entry_date"].min()) if not signals.empty else "",
        "end_date": str(signals["entry_date"].max()) if not signals.empty else "",
        "live_tag_counts": audited["live_timing_tag"].value_counts(dropna=False).to_dict(),
        "hindsight_tag_counts": audited["hindsight_timing_tag"].value_counts(dropna=False).to_dict(),
        "outputs": {
            "audit": "timing_audit.csv",
            "summary": "live_timing_tag_summary.csv",
            "hindsight_summary": "live_vs_hindsight_timing_summary.csv",
            "review_sample": "review_sample_timing.csv",
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(out_dir, tag_summary, hindsight_summary, payload)
    return payload


from research.common.reporting import percent_text as _pct


def _write_report(out_dir: Path, summary: pd.DataFrame, hindsight_summary: pd.DataFrame, payload: dict[str, Any]) -> None:
    lines = [
        "# G2 User V2 Timing Layer Audit",
        "",
        f"Signals: {payload['signals']}",
        f"Window: {payload['start_date']} .. {payload['end_date']}",
        "",
        "## Live-Visible Tags",
        "",
        "| live_timing_tag | signals | avg_fwd3 | avg_fwd5 | avg_fwd10 | stop5 | good | bad |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.to_dict("records"):
        lines.append(
            f"| {row['live_timing_tag']} | {int(row['signals'])} | {_pct(row['avg_fwd3'])} | {_pct(row['avg_fwd5'])} | "
            f"{_pct(row['avg_fwd10'])} | {_pct(row['stop5_rate'])} | {_pct(row['good_rate'])} | {_pct(row['bad_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## Live vs Hindsight",
            "",
            "| live_timing_tag | hindsight_timing_tag | signals | avg_fwd10 | stop5 | good | bad |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in hindsight_summary.to_dict("records"):
        lines.append(
            f"| {row['live_timing_tag']} | {row['hindsight_timing_tag']} | {int(row['signals'])} | "
            f"{_pct(row['avg_fwd10'])} | {_pct(row['stop5_rate'])} | {_pct(row['good_rate'])} | {_pct(row['bad_rate'])} |"
        )
    lines.extend(
        [
            "",
            "Interpretation:",
            "",
            "- `first_breakout_confirm`: entry day is also a daily 20/60 high breakout confirmation.",
            "- `short_box_attempt`: intraday signal breaks a short box but has not cleared the larger 60-day box top; this is the live-visible layer for 东山精密-style watch/confirm decisions.",
            "- `later_breakout_within_10d` is hindsight-only and must not be used directly in live trading.",
            "- `second_confirmation`: pullback/restart after a recent breakout.",
        ]
    )
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit G2 User V2 signal timing quality.")
    parser.add_argument("--signals", default=str(DEFAULT_SIGNALS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--limit", type=int, default=0)
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
