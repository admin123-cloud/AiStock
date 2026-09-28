from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_timing_research"
DEFAULT_INDEX_CODES = ["999999.SH", "000300.SH", "000905.SH", "000852.SH", "399006.SZ"]


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return str(value)


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def _longest_underwater_days(equity: pd.Series) -> int:
    if equity.empty:
        return 0
    dd = equity / equity.cummax() - 1.0
    current = 0
    longest = 0
    for is_underwater in (dd < 0).tolist():
        if is_underwater:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return int(longest)


def _annualized_return(total_return: float, days: int) -> float:
    if days <= 0 or total_return <= -1:
        return 0.0
    return float((1.0 + total_return) ** (252.0 / days) - 1.0)


def _sharpe(ret: pd.Series) -> float:
    if len(ret) < 2:
        return 0.0
    std = float(ret.std(ddof=0))
    if std <= 0:
        return 0.0
    return float(ret.mean() / std * math.sqrt(252.0))


def _load_index_daily(codes: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    ch = clickhouse_client()
    quoted = ", ".join([f"'{code}'" for code in codes])
    sql = f"""
        SELECT
            k.code,
            any(s.name) AS name,
            k.trade_date,
            any(k.open) AS open,
            any(k.high) AS high,
            any(k.low) AS low,
            any(k.close) AS close,
            any(k.amount) AS amount,
            any(k.turnover_rate) AS turnover_rate
        FROM (SELECT * FROM kline_daily FINAL) AS k
        LEFT JOIN (SELECT * FROM stocks FINAL) AS s ON s.code = k.code
        WHERE k.code IN ({quoted})
          AND k.trade_date BETWEEN '{start_date}' AND '{end_date}'
        GROUP BY k.code, k.trade_date
        ORDER BY k.code, k.trade_date
    """
    df = ch.query_df(sql)
    if df.empty:
        raise RuntimeError("No index daily rows loaded.")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    return df


def _load_breadth(start_date: str, end_date: str) -> pd.DataFrame:
    ch = clickhouse_client()
    sql = f"""
        WITH daily AS
        (
            SELECT
                k.code,
                k.trade_date,
                any(k.close) AS close
            FROM (SELECT * FROM kline_daily FINAL) AS k
            JOIN (SELECT * FROM stocks FINAL) AS s ON s.code = k.code
            WHERE s.type = 'stock'
              AND coalesce(s.st, 0) = 0
              AND coalesce(s.quit, 0) = 0
              AND k.trade_date BETWEEN '{start_date}' AND '{end_date}'
            GROUP BY k.code, k.trade_date
        ),
        featured AS
        (
            SELECT
                code,
                trade_date,
                close,
                avg(close) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                count() OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS n20
            FROM daily
        )
        SELECT
            trade_date,
            countIf(n20 >= 20) AS stock_count,
            avgIf(close > ma20, n20 >= 20) AS breadth_ma20
        FROM featured
        GROUP BY trade_date
        ORDER BY trade_date
    """
    df = ch.query_df(sql)
    if df.empty:
        return pd.DataFrame(columns=["trade_date", "stock_count", "breadth_ma20"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    return df


def _add_index_features(df: pd.DataFrame) -> pd.DataFrame:
    parts: List[pd.DataFrame] = []
    for _, g in df.groupby("code", sort=False):
        d = g.sort_values("trade_date").copy()
        d["ret"] = d["close"].pct_change().fillna(0.0)
        d["ma20"] = d["close"].rolling(20, min_periods=20).mean()
        d["ma60"] = d["close"].rolling(60, min_periods=60).mean()
        d["ma120"] = d["close"].rolling(120, min_periods=120).mean()
        d["ma20_slope5"] = d["ma20"] / d["ma20"].shift(5) - 1.0
        d["mom20"] = d["close"] / d["close"].shift(20) - 1.0
        d["above_ma20"] = d["close"] > d["ma20"]
        d["cross_up_ma20"] = d["above_ma20"] & (~d["above_ma20"].shift(1).fillna(False))
        d["cross_down_ma20"] = (~d["above_ma20"]) & d["above_ma20"].shift(1).fillna(False)
        d["above_ma20_days"] = d["above_ma20"].astype(int).groupby((~d["above_ma20"]).cumsum()).cumsum()
        d["below_ma20_days"] = (~d["above_ma20"]).astype(int).groupby(d["above_ma20"].cumsum()).cumsum()
        parts.append(d)
    return pd.concat(parts, ignore_index=True)


def _build_signals(d: pd.DataFrame, breadth_gate: float | None = None) -> Dict[str, pd.Series]:
    above20 = d["above_ma20"].fillna(False)
    slope_up = pd.to_numeric(d["ma20_slope5"], errors="coerce").fillna(0.0) > 0
    ma20_gt_ma60 = pd.to_numeric(d["ma20"], errors="coerce") > pd.to_numeric(d["ma60"], errors="coerce")
    ma60_gt_ma120 = pd.to_numeric(d["ma60"], errors="coerce") > pd.to_numeric(d["ma120"], errors="coerce")
    confirm2 = above20 & above20.shift(1).fillna(False)
    confirm3 = above20 & above20.shift(1).fillna(False) & above20.shift(2).fillna(False)
    breadth = pd.to_numeric(d.get("breadth_ma20"), errors="coerce")
    breadth_ok = breadth >= float(breadth_gate) if breadth_gate is not None else pd.Series(True, index=d.index)
    return {
        "buy_hold": pd.Series(True, index=d.index),
        "idx_close_gt_ma20": above20,
        "idx_close_gt_ma20_confirm2": confirm2,
        "idx_close_gt_ma20_slope_up": above20 & slope_up,
        "idx_close_gt_ma20_breadth55": above20 & (breadth >= 0.55),
        "idx_close_gt_ma20_breadth60": above20 & (breadth >= 0.60),
        "idx_ma20_gt_ma60": above20 & ma20_gt_ma60,
        "idx_ma20_gt_ma60_gt_ma120": above20 & ma20_gt_ma60 & ma60_gt_ma120,
        "idx_close_gt_ma20_breadth_custom": above20 & breadth_ok,
    }


def _summarize_strategy(d: pd.DataFrame, signal: pd.Series, signal_name: str, code: str, name: str) -> Dict[str, Any]:
    # Signal is known at close; position takes effect on next close-to-close return.
    position = signal.fillna(False).astype(bool).shift(1).fillna(False).astype(float)
    ret = pd.to_numeric(d["ret"], errors="coerce").fillna(0.0)
    strat_ret = ret * position
    equity = (1.0 + strat_ret).cumprod()
    bh_equity = (1.0 + ret).cumprod()
    invested_days = int((position > 0).sum())
    switches = int((position.diff().fillna(0.0).abs() > 0).sum())
    total_return = float(equity.iloc[-1] - 1.0) if len(equity) else 0.0
    bh_return = float(bh_equity.iloc[-1] - 1.0) if len(bh_equity) else 0.0
    max_dd = _max_drawdown(equity)
    bh_dd = _max_drawdown(bh_equity)
    years = max(len(d) / 252.0, 0.001)
    return {
        "code": code,
        "name": name,
        "signal": signal_name,
        "start_date": str(d["trade_date"].iloc[0]),
        "end_date": str(d["trade_date"].iloc[-1]),
        "trade_days": int(len(d)),
        "invested_days": invested_days,
        "exposure": float(invested_days / len(d)) if len(d) else 0.0,
        "switches": switches,
        "switches_per_year": float(switches / years),
        "total_return": total_return,
        "annualized_return": _annualized_return(total_return, len(d)),
        "max_drawdown": max_dd,
        "sharpe": _sharpe(strat_ret),
        "longest_underwater_days": _longest_underwater_days(equity),
        "buy_hold_return": bh_return,
        "buy_hold_annualized": _annualized_return(bh_return, len(d)),
        "buy_hold_max_drawdown": bh_dd,
        "return_over_buy_hold": total_return - bh_return,
        "drawdown_reduction": abs(bh_dd) - abs(max_dd),
        "calmar": float(_annualized_return(total_return, len(d)) / abs(max_dd)) if max_dd < 0 else 0.0,
    }


def _build_segments(d: pd.DataFrame, signal: pd.Series, signal_name: str, code: str, name: str) -> pd.DataFrame:
    state = signal.fillna(False).astype(bool)
    rows: List[Dict[str, Any]] = []
    if d.empty:
        return pd.DataFrame()
    start_idx = 0
    current = bool(state.iloc[0])
    ret = pd.to_numeric(d["ret"], errors="coerce").fillna(0.0)
    for idx in range(1, len(d) + 1):
        next_state = bool(state.iloc[idx]) if idx < len(d) else None
        if idx < len(d) and next_state == current:
            continue
        seg = d.iloc[start_idx:idx].copy()
        seg_ret = ret.iloc[start_idx:idx]
        seg_total = float((1.0 + seg_ret).prod() - 1.0)
        rows.append(
            {
                "code": code,
                "name": name,
                "signal": signal_name,
                "state": "open" if current else "closed",
                "start_date": seg["trade_date"].iloc[0],
                "end_date": seg["trade_date"].iloc[-1],
                "days": int(len(seg)),
                "index_return": seg_total,
                "max_drawdown": _max_drawdown((1.0 + seg_ret).cumprod()),
            }
        )
        start_idx = idx
        if idx < len(d):
            current = bool(state.iloc[idx])
    return pd.DataFrame(rows)


def run(start_date: str, end_date: str, index_codes: List[str], output_dir: Path, breadth_gate: float) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_df = _add_index_features(_load_index_daily(index_codes, start_date, end_date))
    breadth_df = _load_breadth(start_date, end_date)
    index_df = index_df.merge(breadth_df, on="trade_date", how="left")

    summaries: List[Dict[str, Any]] = []
    segment_frames: List[pd.DataFrame] = []
    signal_daily_frames: List[pd.DataFrame] = []
    for code, g in index_df.groupby("code", sort=False):
        d = g.sort_values("trade_date").dropna(subset=["ma20"]).reset_index(drop=True)
        if d.empty:
            continue
        name = str(d["name"].dropna().iloc[-1]) if d["name"].notna().any() else code
        signals = _build_signals(d, breadth_gate=breadth_gate)
        for signal_name, signal in signals.items():
            if signal_name == "idx_close_gt_ma20_breadth_custom" and abs(breadth_gate - 0.55) < 1e-9:
                continue
            summaries.append(_summarize_strategy(d, signal, signal_name, code, name))
            segment_frames.append(_build_segments(d, signal, signal_name, code, name))
            daily = d[["trade_date", "code", "name", "close", "ma20", "ma60", "ma120", "ma20_slope5", "mom20", "breadth_ma20"]].copy()
            daily["signal"] = signal_name
            daily["open_allowed"] = signal.fillna(False).astype(bool).values
            signal_daily_frames.append(daily)

    summary_df = pd.DataFrame(summaries).sort_values(["code", "annualized_return"], ascending=[True, False])
    segments_df = pd.concat(segment_frames, ignore_index=True) if segment_frames else pd.DataFrame()
    signal_daily_df = pd.concat(signal_daily_frames, ignore_index=True) if signal_daily_frames else pd.DataFrame()

    index_df.to_csv(output_dir / "index_features.csv", index=False, encoding="utf-8-sig")
    breadth_df.to_csv(output_dir / "market_breadth.csv", index=False, encoding="utf-8-sig")
    summary_df.to_csv(output_dir / "timing_summary.csv", index=False, encoding="utf-8-sig")
    segments_df.to_csv(output_dir / "timing_segments.csv", index=False, encoding="utf-8-sig")
    signal_daily_df.to_csv(output_dir / "timing_signal_daily.csv", index=False, encoding="utf-8-sig")

    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "index_codes": index_codes,
        "breadth_gate": breadth_gate,
        "outputs": {
            "index_features": "index_features.csv",
            "market_breadth": "market_breadth.csv",
            "timing_summary": "timing_summary.csv",
            "timing_segments": "timing_segments.csv",
            "timing_signal_daily": "timing_signal_daily.csv",
        },
        "summary": summaries,
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="G2 market timing research based on index MA gates.")
    parser.add_argument("--start-date", default="2010-01-01")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--index-codes", default=",".join(DEFAULT_INDEX_CODES))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--breadth-gate", type=float, default=0.55)
    args = parser.parse_args()
    codes = [item.strip() for item in str(args.index_codes).split(",") if item.strip()]
    payload = run(
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        index_codes=codes,
        output_dir=Path(args.output_dir),
        breadth_gate=float(args.breadth_gate),
    )
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "index_codes": payload["index_codes"],
                "summary_rows": len(payload["summary"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
