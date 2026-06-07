from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from api.gen2_factor import (
    _add_factor,
    _add_forward_returns,
    _date_or_default,
    _estimate_prewarm_days,
    _latest_trade_date,
    _load_daily_ohlcv,
    build_gen2_factor_registry,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = REPO_ROOT / "reports" / "gen2_alpha191_factor_tests"


def _safe_float(value: Any) -> Optional[float]:
    try:
        val = float(value)
    except Exception:
        return None
    return val if math.isfinite(val) else None


def _sharpe(series: pd.Series, horizon: int) -> Optional[float]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 20:
        return None
    std = float(s.std(ddof=1))
    if not math.isfinite(std) or std <= 0:
        return None
    return float(s.mean() / std * math.sqrt(252.0 / max(1, horizon)))


def _max_drawdown(series: pd.Series) -> Optional[float]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return None
    equity = (1.0 + s).cumprod()
    dd = equity / equity.cummax() - 1.0
    return float(dd.min())


def _load_targets(report_dir: Path, top_n: int) -> pd.DataFrame:
    path = report_dir / "alpha191_recent_2y_best_by_factor.csv"
    df = pd.read_csv(path)
    df = df[df["available"] == True].copy()  # noqa: E712
    df = df.sort_values(["effectiveness_score", "edge_mean"], ascending=[False, False]).head(top_n)
    return df[["factor_id", "horizon", "direction"]].copy()


def _factor_map() -> Dict[str, Dict[str, Any]]:
    payload = build_gen2_factor_registry()
    return {str(item.get("id")): item for item in payload.get("factors") or []}


def compute_sharpe(report_dir: Path, top_n: int, start_date: Optional[str], end_date: Optional[str]) -> Path:
    targets = _load_targets(report_dir, top_n)
    latest = _latest_trade_date()
    if not latest:
        raise RuntimeError("No kline_daily trade date found")
    end_ts = _date_or_default(end_date, pd.Timestamp(latest))
    start_ts = _date_or_default(start_date, end_ts - pd.DateOffset(years=2))
    factors = _factor_map()
    metas = [factors[fid] for fid in targets["factor_id"].drop_duplicates() if fid in factors]
    horizons = sorted({int(x) for x in targets["horizon"].dropna().astype(int)})
    prewarm = max([_estimate_prewarm_days(meta) for meta in metas] or [420])

    print(f"loading daily bars {start_ts:%Y-%m-%d}..{end_ts:%Y-%m-%d}, prewarm={prewarm}, targets={len(targets)}", flush=True)
    daily = _load_daily_ohlcv(
        start_ts.strftime("%Y-%m-%d"),
        end_ts.strftime("%Y-%m-%d"),
        horizon=max(horizons),
        prewarm_days=prewarm,
    )
    base = _add_forward_returns(daily, horizons=horizons)
    rows: List[Dict[str, Any]] = []

    for idx, meta in enumerate(metas, start=1):
        fid = str(meta.get("id"))
        factor_targets = targets[targets["factor_id"] == fid]
        print(f"{idx:03d}/{len(metas):03d} {fid}", flush=True)
        factor_df = _add_factor(base, meta)
        eval_df = factor_df[(factor_df["date"] >= start_ts) & (factor_df["date"] <= end_ts)].copy()

        for _, target in factor_targets.iterrows():
            horizon = int(target["horizon"])
            direction = str(target["direction"])
            ret_col = f"fwd_{horizon}d"
            daily_rows = []
            for date_value, day in eval_df[["date", "factor_value", ret_col]].dropna().groupby("date"):
                if len(day) < 80 or day["factor_value"].nunique() < 5:
                    continue
                rank = day["factor_value"].rank(method="first", pct=True)
                high = day.loc[rank >= 0.8, ret_col]
                low = day.loc[rank <= 0.2, ret_col]
                if high.empty or low.empty:
                    continue
                high_mean = float(high.mean())
                low_mean = float(low.mean())
                if direction == "high":
                    long_ret = high_mean
                    weak_ret = low_mean
                    edge_ret = high_mean - low_mean
                else:
                    long_ret = low_mean
                    weak_ret = high_mean
                    edge_ret = low_mean - high_mean
                daily_rows.append(
                    {
                        "date": pd.Timestamp(date_value).strftime("%Y-%m-%d"),
                        "long_ret": long_ret,
                        "weak_ret": weak_ret,
                        "edge_ret": edge_ret,
                    }
                )
            series_df = pd.DataFrame(daily_rows)
            rows.append(
                {
                    "factor_id": fid,
                    "horizon": horizon,
                    "direction": direction,
                    "days": int(len(series_df)),
                    "long_mean_return": _safe_float(series_df["long_ret"].mean()) if not series_df.empty else None,
                    "long_std": _safe_float(series_df["long_ret"].std(ddof=1)) if len(series_df) > 1 else None,
                    "long_sharpe": _sharpe(series_df["long_ret"], horizon) if not series_df.empty else None,
                    "long_max_drawdown": _max_drawdown(series_df["long_ret"]) if not series_df.empty else None,
                    "edge_mean_return": _safe_float(series_df["edge_ret"].mean()) if not series_df.empty else None,
                    "edge_std": _safe_float(series_df["edge_ret"].std(ddof=1)) if len(series_df) > 1 else None,
                    "edge_sharpe": _sharpe(series_df["edge_ret"], horizon) if not series_df.empty else None,
                    "edge_max_drawdown": _max_drawdown(series_df["edge_ret"]) if not series_df.empty else None,
                    "edge_positive_ratio": _safe_float((series_df["edge_ret"] > 0).mean()) if not series_df.empty else None,
                }
            )

    out = pd.DataFrame(rows)
    ranking_path = report_dir / "alpha191_recent_2y_best_by_factor.csv"
    ranking = pd.read_csv(ranking_path)
    merged = ranking.merge(out, on=["factor_id", "horizon", "direction"], how="left", suffixes=("", "_daily"))
    merged_path = report_dir / "alpha191_recent_2y_best_by_factor_with_sharpe.csv"
    merged.to_csv(merged_path, index=False, encoding="utf-8-sig")
    out.to_csv(report_dir / "alpha191_recent_2y_top_factor_sharpe.csv", index=False, encoding="utf-8-sig")
    return merged_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    args = parser.parse_args()
    path = compute_sharpe(Path(args.report_dir), args.top_n, args.start_date, args.end_date)
    print(path)


if __name__ == "__main__":
    main()
