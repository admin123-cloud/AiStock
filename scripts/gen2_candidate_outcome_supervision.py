from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_portfolio import _json_default, _load_trade_dates, _pct  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _run_profile  # noqa: E402
from scripts.gen2_runtime_dates import add_end_date_argument, resolve_end_date  # noqa: E402


DEFAULT_CANDIDATES = ROOT / "reports" / "gen2_realtime_candidate_recall_study" / "realtime_candidates.parquet"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_candidate_outcome_supervision"


FEATURES = [
    "v4_rank",
    "v4_score",
    "mom5",
    "mom10",
    "mom20",
    "vol_ratio",
    "vol10",
    "rt_return_from_d1_close",
    "rt_confirm_vs_ma5",
    "rt_confirm_vs_ma10",
    "rt_fractal_rebound",
    "rt_30m_amount_ratio",
    "rt_confirm_hour",
]


def _load_candidates(path: Path, pool: str, start_date: str, end_date: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Candidates file not found: {path}")
    d = pd.read_parquet(path)
    if d.empty:
        return pd.DataFrame(columns=["realtime_pool", "entry_date", "confirm_datetime", "code", "entry_price", "candidate_key"])
    d = d[d["realtime_pool"].eq(pool)].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    for col in FEATURES + ["entry_price"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["candidate_key"] = d["code"].astype(str) + "|" + d["confirm_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return d.dropna(subset=["entry_date", "code", "confirm_datetime", "entry_price"]).reset_index(drop=True)


def _load_daily(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client
    if not codes:
        return pd.DataFrame(columns=["code", "trade_date", "close"])
    quoted = ", ".join(f"'{code}'" for code in sorted(set(codes)))
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)


def _load_minute30(codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client
    if not codes:
        return pd.DataFrame(columns=["code", "datetime", "bar_date", "low"])
    quoted = ", ".join(f"'{code}'" for code in sorted(set(codes)))
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, datetime, low
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime >= toDateTime('{start_date} 09:30:00')
          AND datetime <= toDateTime('{end_date} 15:00:00')
        ORDER BY code, datetime
        """
    )
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["bar_date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    return df.dropna(subset=["code", "datetime", "low"]).reset_index(drop=True)


def _add_outcomes(candidates: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if candidates.empty:
        base = candidates.copy()
        for col in FEATURES + [f"outcome_fwd_ret_{d}d" for d in [3, 5, 10, 20]] + [
            "stop5_touch_30m",
            "stop5_touch_datetime",
            "outcome_good",
            "outcome_bad",
        ]:
            base[col] = pd.Series([np.nan] * len(base))
        base["stop5_touch_30m"] = base["stop5_touch_30m"].fillna(False)
        base["stop5_touch_datetime"] = pd.Series([pd.NaT] * len(base))
        base["outcome_good"] = pd.Series([False] * len(base))
        base["outcome_bad"] = pd.Series([False] * len(base))
        return base

    trade_dates = _load_trade_dates(start_date, end_date)
    idx = {date: i for i, date in enumerate(trade_dates)}
    codes = candidates["code"].dropna().astype(str).unique().tolist()
    daily = _load_daily(codes, start_date, end_date)
    price_map = {(row.code, row.trade_date): float(row.close) for row in daily.itertuples(index=False)}
    minute = _load_minute30(codes, start_date, end_date)
    minute_by_code = {code: g.sort_values("datetime").reset_index(drop=True) for code, g in minute.groupby("code", sort=False)}

    rows: list[dict[str, Any]] = []
    for row in candidates.itertuples(index=False):
        entry_date = str(getattr(row, "entry_date"))
        entry_idx = idx.get(entry_date)
        entry_price = float(getattr(row, "entry_price"))
        code = str(getattr(row, "code"))
        confirm_dt = pd.Timestamp(getattr(row, "confirm_datetime"))
        item: dict[str, Any] = {}
        for horizon in [3, 5, 10, 20]:
            target_date = trade_dates[min(entry_idx + horizon, len(trade_dates) - 1)] if entry_idx is not None else None
            close = price_map.get((code, target_date)) if target_date else None
            item[f"outcome_fwd_ret_{horizon}d"] = close / entry_price - 1.0 if close is not None and entry_price > 0 else np.nan
        stop_price = entry_price * 1.0005 * 0.95
        stop_touch = False
        stop_dt = pd.NaT
        if entry_idx is not None:
            end_date_5 = trade_dates[min(entry_idx + 5, len(trade_dates) - 1)]
            bars = minute_by_code.get(code)
            if bars is not None and not bars.empty:
                scan = bars[(bars["datetime"] > confirm_dt) & (bars["bar_date"] <= end_date_5)]
                hit = scan[scan["low"] <= stop_price]
                if not hit.empty:
                    stop_touch = True
                    stop_dt = pd.Timestamp(hit.iloc[0]["datetime"])
        item["stop5_touch_30m"] = bool(stop_touch)
        item["stop5_touch_datetime"] = stop_dt
        rows.append(item)
    out = pd.concat([candidates.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    out["outcome_good"] = (pd.to_numeric(out["outcome_fwd_ret_10d"], errors="coerce") >= 0.05) & (~out["stop5_touch_30m"].fillna(False))
    out["outcome_bad"] = out["stop5_touch_30m"].fillna(False) | (pd.to_numeric(out["outcome_fwd_ret_5d"], errors="coerce") <= -0.05)
    return out


def _feature_contrast(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    good = d[d["outcome_good"]] if "outcome_good" in d.columns else d.iloc[:0]
    bad = d[d["outcome_bad"]] if "outcome_bad" in d.columns else d.iloc[:0]
    n = len(d)

    def _series(val: Any) -> pd.Series:
        if isinstance(val, pd.Series):
            return val
        return pd.Series(dtype=float, index=range(n)) if n else pd.Series(dtype=float)

    for col in FEATURES:
        g = _series(pd.to_numeric(good.get(col), errors="coerce")).dropna()
        b = _series(pd.to_numeric(bad.get(col), errors="coerce")).dropna()
        allv = _series(pd.to_numeric(d.get(col), errors="coerce")).dropna()
        if g.empty or b.empty:
            continue
        rows.append(
            {
                "feature": col,
                "good_mean": float(g.mean()),
                "bad_mean": float(b.mean()),
                "diff_good_minus_bad": float(g.mean() - b.mean()),
                "good_median": float(g.median()),
                "bad_median": float(b.median()),
                "all_median": float(allv.median()) if not allv.empty else np.nan,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["feature", "good_mean", "bad_mean", "diff_good_minus_bad", "good_median", "bad_median", "all_median"])
    return pd.DataFrame(rows).sort_values("diff_good_minus_bad", ascending=False)


def _rule_masks(d: pd.DataFrame) -> dict[str, pd.Series]:
    def _s(col: str) -> pd.Series:
        if col in d.columns:
            return pd.to_numeric(d[col], errors="coerce")
        return pd.Series(np.nan, index=d.index)

    return {
        "all": pd.Series(True, index=d.index),
        "risk_cool_v1": (
            _s("mom20").le(0.40)
            & _s("mom5").le(0.18)
            & _s("vol_ratio").le(2.20)
            & _s("rt_return_from_d1_close").between(-0.02, 0.14)
            & _s("rt_fractal_rebound").le(0.22)
        ),
        "risk_cool_v2": (
            _s("mom20").le(0.30)
            & _s("mom5").le(0.14)
            & _s("vol_ratio").le(1.90)
            & _s("rt_return_from_d1_close").between(0.00, 0.12)
            & _s("rt_30m_amount_ratio").between(1.5, 7.0)
        ),
        "quality_sweet_v1": (
            _s("v4_rank").between(50, 220)
            & _s("v4_score").ge(0.62)
            & _s("rt_return_from_d1_close").between(0.00, 0.12)
            & _s("rt_30m_amount_ratio").between(1.8, 7.0)
            & _s("mom20").le(0.45)
        ),
        "stop_avoid_v1": (
            _s("rt_return_from_d1_close").le(0.12)
            & _s("rt_fractal_rebound").le(0.18)
            & _s("mom5").le(0.16)
            & _s("vol_ratio").le(2.40)
        ),
        "early_cool_v1": (
            _s("rt_confirm_hour").le(10.5)
            & _s("rt_return_from_d1_close").between(0.00, 0.12)
            & _s("mom20").le(0.35)
            & _s("vol_ratio").le(2.20)
        ),
    }


def _summarize_rules(d: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for rule, mask in _rule_masks(d).items():
        g = d[mask.fillna(False)].copy()
        if g.empty:
            continue
        rows.append(
            {
                "rule": rule,
                "signals": int(g["candidate_key"].nunique()),
                "days": int(g["entry_date"].nunique()),
                "good_rate": float(g["outcome_good"].mean()),
                "bad_rate": float(g["outcome_bad"].mean()),
                "stop5_rate": float(g["stop5_touch_30m"].mean()),
                "avg_fwd5": float(pd.to_numeric(g["outcome_fwd_ret_5d"], errors="coerce").mean()),
                "avg_fwd10": float(pd.to_numeric(g["outcome_fwd_ret_10d"], errors="coerce").mean()),
                "target_precision": float(g["is_same_day_target"].mean()) if "is_same_day_target" in g.columns else np.nan,
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "rule",
                "signals",
                "days",
                "good_rate",
                "bad_rate",
                "stop5_rate",
                "avg_fwd5",
                "avg_fwd10",
                "target_precision",
            ]
        )
    return pd.DataFrame(rows).sort_values(["bad_rate", "avg_fwd10"], ascending=[True, False])


def _prepare_source(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["pattern"] = "pullback_restart_rank100"
    out["g2_open_state"] = "NORMAL"
    out["trigger_type"] = "bottom_fractal_break_high_vol"
    out["v4_rank"] = pd.to_numeric(out["v4_rank"], errors="coerce").fillna(999).astype(int)
    out["v4_score"] = pd.to_numeric(out["v4_score"], errors="coerce").fillna(0.0)
    return out


def _backtest_rules(d: pd.DataFrame, output_dir: Path, start_date: str, end_date: str) -> pd.DataFrame:
    profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    rows: list[dict[str, Any]] = []
    for rule, mask in _rule_masks(d).items():
        if rule == "all":
            continue
        selected = d[mask.fillna(False)].copy()
        if selected.empty:
            continue
        source = output_dir / "sources" / f"{rule}.parquet"
        source.parent.mkdir(parents=True, exist_ok=True)
        _prepare_source(selected).to_parquet(source, index=False)
        _prepare_source(selected).to_csv(source.with_suffix(".csv"), index=False, encoding="utf-8-sig")
        summary = _run_profile(
            signal_source=source,
            output_dir=output_dir / "backtests" / rule,
            profile=profile,
            start_date=start_date,
            end_date=end_date,
            sort_mode="trigger_time",
        )
        summary["rule"] = rule
        rows.append(summary)
    return pd.DataFrame(rows)


def _write_report(output_dir: Path, rule_summary: pd.DataFrame, contrast: pd.DataFrame, backtests: pd.DataFrame) -> None:
    lines = [
        "# G2 Candidate Outcome Supervision",
        "",
        "Labels use future outcomes for research only: fwd returns and 5-day 30m stop touch. Decision features remain realtime-visible.",
        "",
        "## Rule Outcome Summary",
        "",
        "| rule | signals | good | bad | stop5 | avg5 | avg10 | target_precision |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in rule_summary.iterrows():
        lines.append(
            f"| {row['rule']} | {int(row['signals'])} | {_pct(row['good_rate'])} | {_pct(row['bad_rate'])} | "
            f"{_pct(row['stop5_rate'])} | {_pct(row['avg_fwd5'])} | {_pct(row['avg_fwd10'])} | {_pct(row['target_precision'])} |"
        )
    lines.extend(["", "## Backtest", ""])
    lines.append("| rule | signals | trades | total | excess_vs_csi1000 | max_dd | win | avg_trade |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for _, row in backtests.iterrows():
        lines.append(
            f"| {row['rule']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
        )
    lines.extend(["", "## Feature Contrast", ""])
    lines.append("| feature | good_mean | bad_mean | diff | good_median | bad_median |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for _, row in contrast.reindex(contrast["diff_good_minus_bad"].abs().sort_values(ascending=False).index).head(20).iterrows():
        lines.append(
            f"| {row['feature']} | {row['good_mean']:.4f} | {row['bad_mean']:.4f} | {row['diff_good_minus_bad']:.4f} | "
            f"{row['good_median']:.4f} | {row['bad_median']:.4f} |"
        )
    (output_dir / "outcome_supervision_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = _load_candidates(Path(args.candidates), str(args.pool), str(args.start_date), str(args.end_date))
    labeled = _add_outcomes(candidates, str(args.start_date), str(args.end_date))
    contrast = _feature_contrast(labeled)
    rule_summary = _summarize_rules(labeled)
    backtests = _backtest_rules(labeled, output_dir, str(args.start_date), str(args.end_date))

    labeled.to_csv(output_dir / "labeled_candidates.csv", index=False, encoding="utf-8-sig")
    labeled.to_parquet(output_dir / "labeled_candidates.parquet", index=False)
    contrast.to_csv(output_dir / "feature_contrast.csv", index=False, encoding="utf-8-sig")
    rule_summary.to_csv(output_dir / "rule_outcome_summary.csv", index=False, encoding="utf-8-sig")
    backtests.to_csv(output_dir / "rule_backtest_summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, rule_summary, contrast, backtests)
    payload = {
        "schema_version": 1,
        "candidates": str(Path(args.candidates)),
        "pool": str(args.pool),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "candidate_count": int(labeled["candidate_key"].nunique()),
        "rule_summary": rule_summary.where(pd.notna(rule_summary), None).to_dict("records"),
        "backtests": backtests.where(pd.notna(backtests), None).to_dict("records"),
        "outputs": {
            "report": "outcome_supervision_report.md",
            "labeled_candidates": "labeled_candidates.parquet",
            "rule_summary": "rule_outcome_summary.csv",
            "backtests": "rule_backtest_summary.csv",
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Supervise realtime G2 candidates by actual forward returns and 30m stop-touch outcomes.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--pool", default="d1_rank200")
    parser.add_argument("--start-date", default="2024-07-09")
    add_end_date_argument(parser)
    args = parser.parse_args()
    args.end_date = resolve_end_date(args.end_date)
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
