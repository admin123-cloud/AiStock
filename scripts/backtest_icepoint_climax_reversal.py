from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils.database import db


@dataclass
class BacktestResult:
    params: Dict[str, float]
    hold_days: int
    trade_count: int
    win_rate: float
    avg_ret: float
    median_ret: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    avg_holding_days: float


def _to_float_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _normalize_stock_codes(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for v in values:
        if v is None:
            continue
        s = str(v).strip().replace(" ", "")
        if not s:
            continue
        # keep 6-digit numeric symbol style
        for code in s.replace(";", ",").replace("|", ",").split(","):
            c = code.strip().replace(".SH", "").replace(".SZ", "")
            if len(c) == 6 and c.isdigit() and c not in seen:
                out.append(c)
                seen.add(c)
    return out


def _load_stock_pool(
    stock_codes: Optional[str] = None,
    stock_pool_file: Optional[str] = None,
) -> List[str]:
    codes: List[str] = []
    if stock_codes:
        codes.extend(_normalize_stock_codes([stock_codes]))

    if stock_pool_file:
        file_path = Path(stock_pool_file)
        if not file_path.exists():
            raise FileNotFoundError(f"stock_pool_file not found: {file_path}")
        try:
            pool_df = pd.read_csv(file_path, encoding="utf-8")
        except Exception:
            pool_df = pd.read_csv(file_path, encoding="utf-8-sig")
        if pool_df.empty:
            return codes
        if "code" in pool_df.columns:
            col = pool_df["code"]
        elif "stock_code" in pool_df.columns:
            col = pool_df["stock_code"]
        else:
            col = pool_df.iloc[:, 0]
        codes.extend(_normalize_stock_codes(col.astype(str).tolist()))

    seen: set[str] = set()
    deduped = []
    for c in codes:
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)
    return deduped


def _load_trade_dates(start_date: str, end_date: str) -> List[str]:
    df = pd.read_sql(
        text(
            """
            SELECT trade_date
            FROM trade_calendar
            WHERE market = 'SH'
              AND is_trading = 1
              AND trade_date >= :start_date
              AND trade_date <= :end_date
            ORDER BY trade_date ASC
            """
        ),
        db.engine,
        params={"start_date": start_date, "end_date": end_date},
    )
    if df.empty:
        return []
    return pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist()


def _load_market_counts(
    start_date: str,
    end_date: str,
    trade_dates: List[str],
    stock_codes: Optional[List[str]] = None,
) -> pd.DataFrame:
    stock_filter = ""
    if stock_codes:
        quoted = ",".join([f"'{code}'" for code in stock_codes])
        stock_filter = f"AND substring(code, 1, 6) IN ({quoted}) "

    query = f"""
            WITH daily_prices AS (
                SELECT
                    SUBSTRING(code, 1, 6) AS code,
                    toDate(trade_date) AS trade_date,
                    toFloat64(close) AS close,
                    close_prev
                FROM (
                    SELECT
                        code,
                        trade_date,
                        close,
                        lagInFrame(close, 1) OVER (PARTITION BY code ORDER BY trade_date) AS close_prev
                    FROM kline_daily
                    WHERE trade_date >= :start_date
                      AND trade_date <= :end_date
                      {stock_filter}
                ) AS kd
                INNER JOIN stocks s ON s.code = kd.code
                WHERE s.type = 'stock'
                  AND s.quit = 0
                  AND (s.st = 0 OR s.st IS NULL)
                  AND close_prev > 0
            )
            SELECT
                trade_date AS trade_date_str,
                COUNT() AS stock_cnt,
                SUM((close / close_prev - 1.0) >= (CASE
                    WHEN code LIKE '300%' OR code LIKE '688%' THEN 0.199
                    WHEN substring(code, 1, 2) IN ('43', '83', '87', '92') THEN 0.299
                    ELSE 0.099
                END)) AS limit_up_count,
                SUM((close / close_prev - 1.0) <= (-CASE
                    WHEN code LIKE '300%' OR code LIKE '688%' THEN 0.199
                    WHEN substring(code, 1, 2) IN ('43', '83', '87', '92') THEN 0.299
                    ELSE 0.099
                END)) AS limit_down_count
            FROM daily_prices
            GROUP BY trade_date
            ORDER BY trade_date
            """
    df = pd.read_sql(
        text(query),
        db.engine,
        params={"start_date": start_date, "end_date": end_date},
    )
    if df.empty:
        return df

    df["trade_date_str"] = pd.to_datetime(df["trade_date_str"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["trade_date_str", "stock_cnt", "limit_up_count", "limit_down_count"]).copy()
    df["trade_date_str"] = df["trade_date_str"].astype(str)
    df["trade_date_str"] = df["trade_date_str"].str.slice(0, 10)

    # 保留交易日对齐
    df = df[df["trade_date_str"].isin(trade_dates)].copy().sort_values("trade_date_str").reset_index(drop=True)
    df["stock_cnt"] = _to_float_series(df["stock_cnt"]).astype(float).astype(int)
    df["limit_up_count"] = _to_float_series(df["limit_up_count"]).astype(float).astype(int)
    df["limit_down_count"] = _to_float_series(df["limit_down_count"]).astype(float).astype(int)
    return df


def _build_market_events(stock_panel: pd.DataFrame, trade_dates: List[str], roll_window: int, quantiles: Tuple[int, ...]) -> pd.DataFrame:
    daily = stock_panel.copy()
    daily = daily[daily["trade_date_str"].isin(trade_dates)].copy()
    daily = daily.sort_values("trade_date_str").reset_index(drop=True)

    for q in quantiles:
        qv = q / 100.0
        daily[f"down_q{q}"] = daily["limit_down_count"].rolling(roll_window, min_periods=max(30, int(roll_window * 0.35))).quantile(qv)
        daily[f"up_q{q}"] = daily["limit_up_count"].rolling(roll_window, min_periods=max(30, int(roll_window * 0.35))).quantile(qv)

    daily["down_ratio"] = _to_float_series(daily["limit_down_count"]) / _to_float_series(daily["stock_cnt"]) 
    daily["up_ratio"] = _to_float_series(daily["limit_up_count"]) / _to_float_series(daily["stock_cnt"])
    return daily


def _load_index_series(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    idx = pd.read_sql(
        text(
            """
            SELECT trade_date, open, close
            FROM kline_daily
            WHERE code = :code
              AND trade_date >= :start_date
              AND trade_date <= :end_date
            ORDER BY trade_date
            """
        ),
        db.engine,
        params={"code": index_code, "start_date": start_date, "end_date": end_date},
    )
    if idx.empty:
        return idx

    idx["trade_date"] = pd.to_datetime(idx["trade_date"], errors="coerce")
    idx["open"] = _to_float_series(idx["open"])
    idx["close"] = _to_float_series(idx["close"])
    idx = idx.dropna(subset=["trade_date", "open", "close"]).sort_values("trade_date").reset_index(drop=True)
    idx["trade_date_str"] = idx["trade_date"].dt.strftime("%Y-%m-%d")
    idx = idx.drop_duplicates(subset=["trade_date_str"], keep="last").reset_index(drop=True)
    return idx


def _build_trades(
    market: pd.DataFrame,
    idx: pd.DataFrame,
    down_lo_q: int,
    down_hi_q: int,
    up_lo_q: int,
    up_hi_q: int,
    hold_days: int,
    cost_bps: float,
    filter_mode: str = "none",
    filter_rebound_days: int = 5,
    filter_rebound_thres: float = 0.0,
    filter_dominance_ratio: float = 1.2,
) -> pd.DataFrame:
    if market.empty or idx.empty:
        return pd.DataFrame()

    # price lookup
    idx_sorted = idx.sort_values("trade_date").reset_index(drop=True)
    idx_sorted["i"] = range(len(idx_sorted))
    idx_sorted = idx_sorted.set_index("trade_date_str")

    rows: List[Dict[str, float]] = []
    last_exit_idx = -1

    for _, r in market.iterrows():
        t = r["trade_date_str"]
        if t not in idx_sorted.index:
            continue

        # Skip head with unavailable rolling quantiles
        if pd.isna(r.get(f"down_q{down_hi_q}")) or pd.isna(r.get(f"up_q{up_hi_q}")):
            continue

        down_cnt = int(r["limit_down_count"])
        up_cnt = int(r["limit_up_count"])
        signal = 0.0

        if down_cnt >= r[f"down_q{down_hi_q}"]:
            signal += 1.0
        elif down_cnt >= r[f"down_q{down_lo_q}"]:
            signal += 0.6

        if up_cnt >= r[f"up_q{up_hi_q}"]:
            signal -= 1.0
        elif up_cnt >= r[f"up_q{up_lo_q}"]:
            signal -= 0.6

        if signal > 0.0:
            sign = 1
            weight = min(signal, 1.0)
        elif signal < 0.0:
            sign = -1
            weight = min(-signal, 1.0)
        else:
            continue

        t_idx = int(idx_sorted.at[t, "i"])
        entry_idx = t_idx + 1
        exit_idx = entry_idx + hold_days - 1
        rebound_idx = entry_idx + filter_rebound_days - 1

        if entry_idx >= len(idx_sorted) or exit_idx >= len(idx_sorted):
            continue
        if filter_mode != "none" and rebound_idx >= len(idx_sorted):
            continue
        if entry_idx <= last_exit_idx:
            continue

        # hold from next open to exit close
        entry_open = float(idx_sorted.iloc[entry_idx]["open"])
        exit_close = float(idx_sorted.iloc[exit_idx]["close"])
        if not (entry_open > 0 and exit_close > 0):
            continue

        gross = (exit_close / entry_open - 1.0) * sign
        net = gross * weight
        # cost both legs + per turnover on entry/exit for used notional weight
        net -= 2.0 * (cost_bps / 10000.0) * weight

        # Optional momentum/reversal confirmation filter
        if filter_mode in {"single", "compound"}:
            future_close = float(idx_sorted.iloc[rebound_idx]["close"])
            confirm_ret = future_close / entry_open - 1.0
            if sign > 0 and confirm_ret < filter_rebound_thres:
                continue
            if sign < 0 and confirm_ret > -filter_rebound_thres:
                continue

            if filter_mode == "compound":
                up_cnt = int(r["limit_up_count"])
                dom_ratio = (down_cnt + 0.0001) / (up_cnt + 0.0001)
                if sign > 0 and dom_ratio < filter_dominance_ratio:
                    continue
                if sign < 0 and dom_ratio > 1.0 / max(filter_dominance_ratio, 0.0001):
                    continue

        rows.append(
            {
                "signal_date": t,
                "entry_idx": entry_idx,
                "exit_idx": exit_idx,
                "entry_date": idx_sorted.iloc[entry_idx]["trade_date"],
                "exit_date": idx_sorted.iloc[exit_idx]["trade_date"],
                "sign": float(sign),
                "weight": float(weight),
                "down_cnt": float(down_cnt),
                "up_cnt": float(up_cnt),
                "gross": float(gross),
                "net": float(net),
                "abs_daily_ret": abs(net),
                "hold_days": int(hold_days),
                "filter_mode": filter_mode,
            }
        )

        # non-overlap schedule
        last_exit_idx = exit_idx

    return pd.DataFrame(rows)


def _trade_stats(trades: pd.DataFrame, idx: pd.DataFrame, hold_days: int, start_capital: float = 1.0) -> BacktestResult:
    if trades.empty:
        return BacktestResult(
            params={},
            hold_days=hold_days,
            trade_count=0,
            win_rate=0.0,
            avg_ret=0.0,
            median_ret=0.0,
            total_return=0.0,
            annual_return=0.0,
            max_drawdown=0.0,
            sharpe=0.0,
            avg_holding_days=0.0,
        )

    trades = trades.sort_values(["entry_idx", "signal_date"]).reset_index(drop=True)
    rets = trades["net"].to_numpy(dtype=float)
    if len(rets) == 0:
        return BacktestResult(0, hold_days, 0, 0, 0, 0, 0, 0, 0, 0)

    equity_curve = [start_capital]
    for r in rets:
        equity_curve.append(equity_curve[-1] * (1.0 + r))

    equity = np.array(equity_curve, dtype=float)
    total = equity[-1] - 1.0
    # trade-day annualization based on total calendar (approx)
    if len(idx) > 1:
        ann = (equity[-1] ** (252.0 / max(len(trades) * hold_days, 1))) - 1.0
    else:
        ann = 0.0

    draw = pd.Series(equity).pct_change().fillna(0.0)
    if draw.std(ddof=0) > 1e-12:
        sharpe = float((draw.mean() / draw.std(ddof=0)) * math.sqrt(252.0))
    else:
        sharpe = 0.0

    cummax = np.maximum.accumulate(equity)
    dd = equity / cummax - 1.0
    mdd = float(dd.min())

    win = float((rets > 0).mean()) if len(rets) else 0.0
    avg_ret = float(rets.mean())
    med_ret = float(np.median(rets))

    return BacktestResult(
        params={},
        hold_days=hold_days,
        trade_count=int(len(rets)),
        win_rate=win,
        avg_ret=avg_ret,
        median_ret=med_ret,
        total_return=total,
        annual_return=ann,
        max_drawdown=mdd,
        sharpe=sharpe,
        avg_holding_days=float(np.mean(trades["hold_days"])) if len(trades) else 0.0,
    )


def run_grid(
    start_date: str,
    end_date: str,
    quantiles: Tuple[int, ...],
    hold_days_list: Tuple[int, ...],
    output_dir: Path,
    cost_bps: float,
    filter_modes: Tuple[str, ...] = ("none", "single", "compound"),
    filter_rebound_days: int = 5,
    filter_rebound_thres_values: Tuple[float, ...] = (0.0,),
    filter_dominance_ratio_values: Tuple[float, ...] = (1.2,),
    index_code: str = "999999.SH",
    roll_window: int = 252,
    stock_codes: Optional[List[str]] = None,
) -> pd.DataFrame:
    trade_dates = _load_trade_dates(start_date, end_date)
    market = _load_market_counts(start_date, end_date, trade_dates, stock_codes=stock_codes)
    idx = _load_index_series(index_code, start_date, end_date)

    if not trade_dates:
        raise RuntimeError("trade_calendar is empty for range")
    if market.empty:
        raise RuntimeError("market count panel empty for range")
    if idx.empty:
        raise RuntimeError("index series empty for range")

    idx_dates = idx["trade_date_str"].tolist()
    idx = idx[idx["trade_date_str"].isin(set(trade_dates))].copy()
    if idx.empty:
        raise RuntimeError("index no rows after date intersection")

    market = _build_market_events(market, trade_dates, roll_window=roll_window, quantiles=quantiles)
    market = market[market["trade_date_str"].isin(idx["trade_date_str"])].copy()

    records: List[Dict[str, float]] = []
    stock_pool_size = len(stock_codes) if stock_codes else 0
    for down_lo in quantiles:
        for down_hi in quantiles:
            if down_hi <= down_lo:
                continue
            for up_lo in quantiles:
                for up_hi in quantiles:
                    if up_hi <= up_lo:
                        continue
                    for hold_days in hold_days_list:
                        for filter_mode in filter_modes:
                            for filter_rebound_thres in filter_rebound_thres_values:
                                for filter_dominance_ratio in filter_dominance_ratio_values:
                                    trades = _build_trades(
                                        market=market,
                                        idx=idx,
                                        down_lo_q=down_lo,
                                        down_hi_q=down_hi,
                                        up_lo_q=up_lo,
                                        up_hi_q=up_hi,
                                        hold_days=hold_days,
                                        cost_bps=cost_bps,
                                        filter_mode=filter_mode,
                                        filter_rebound_days=filter_rebound_days,
                                        filter_rebound_thres=filter_rebound_thres,
                                        filter_dominance_ratio=filter_dominance_ratio,
                                    )
                                    stat = _trade_stats(trades, idx, hold_days)
                                    rec = {
                                        "stock_pool_size": stock_pool_size,
                                        "down_lo_q": int(down_lo),
                                        "down_hi_q": int(down_hi),
                                        "up_lo_q": int(up_lo),
                                        "up_hi_q": int(up_hi),
                                        "hold_days": int(hold_days),
                                        "filter_mode": filter_mode,
                                        "filter_rebound_thres": round(float(filter_rebound_thres), 4),
                                        "filter_dominance_ratio": round(float(filter_dominance_ratio), 4),
                                        "trade_count": int(stat.trade_count),
                                        "win_rate": round(stat.win_rate, 4),
                                        "avg_ret": round(stat.avg_ret, 4),
                                        "median_ret": round(stat.median_ret, 4),
                                        "total_return": round(stat.total_return, 4),
                                        "annual_return": round(stat.annual_return, 4),
                                        "max_drawdown": round(stat.max_drawdown, 4),
                                        "sharpe": round(stat.sharpe, 4),
                                    }
                                    if stat.trade_count > 0:
                                        rec["mean_down_when_traded"] = round(float(trades["down_cnt"].mean()), 4)
                                        rec["mean_up_when_traded"] = round(float(trades["up_cnt"].mean()), 4)
                                    else:
                                        rec["mean_down_when_traded"] = 0.0
                                        rec["mean_up_when_traded"] = 0.0
                                    records.append(rec)

    out = pd.DataFrame(records)
    out = out.sort_values(["hold_days", "total_return", "sharpe", "win_rate", "trade_count"], ascending=[True, False, False, False, False]).reset_index(drop=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_dir / "grid_summary.csv", index=False, encoding="utf-8-sig")

    top5_5 = out[out["hold_days"] == 5].head(20)
    top5_20 = out[out["hold_days"] == 20].head(20)
    top5_5.to_csv(output_dir / "top_5d.csv", index=False, encoding="utf-8-sig")
    top5_20.to_csv(output_dir / "top_20d.csv", index=False, encoding="utf-8-sig")

    best = out.loc[out.groupby("hold_days")["total_return"].idxmax()]
    best = best.sort_values("hold_days")
    best.to_csv(output_dir / "best_by_hold.csv", index=False, encoding="utf-8-sig")

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Icepoint vs Climax reversal grid backtest")
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", default="2026-06-02")
    parser.add_argument("--hold-days", nargs="+", type=int, default=[5, 20])
    parser.add_argument("--cost-bps", type=float, default=30.0)
    parser.add_argument("--quantiles", nargs="+", type=int, default=[85, 90, 92, 95, 96, 97, 98, 99])
    parser.add_argument(
        "--filter-modes",
        nargs="+",
        choices=("none", "single", "compound"),
        default=["none", "single", "compound"],
    )
    parser.add_argument("--filter-rebound-days", type=int, default=5)
    parser.add_argument(
        "--filter-rebound-thres-values",
        nargs="+",
        type=float,
        default=[0.0, 0.002, 0.005, 0.01],
    )
    parser.add_argument(
        "--filter-dominance-ratio-values",
        nargs="+",
        type=float,
        default=[1.0, 1.1, 1.2, 1.4],
    )
    parser.add_argument("--output-dir", default="reports/icepoint_climax_reversal_backtest")
    parser.add_argument("--stock-codes", default=None)
    parser.add_argument(
        "--stock-pool-file",
        default=None,
        help="CSV file path with code column (or first column if no header) for stock pool filtering.",
    )
    args = parser.parse_args()

    stock_codes = _load_stock_pool(args.stock_codes, args.stock_pool_file)
    stock_scope = "全市场" if len(stock_codes) == 0 else f"{len(stock_codes)}只池化股票"
    print(f"[Pool] stock scope: {stock_scope}")

    out = run_grid(
        start_date=args.start_date,
        end_date=args.end_date,
        quantiles=tuple(args.quantiles),
        hold_days_list=tuple(args.hold_days),
        filter_modes=tuple(args.filter_modes),
        output_dir=Path(args.output_dir),
        cost_bps=args.cost_bps,
        filter_rebound_days=args.filter_rebound_days,
        filter_rebound_thres_values=tuple(args.filter_rebound_thres_values),
        filter_dominance_ratio_values=tuple(args.filter_dominance_ratio_values),
        stock_codes=stock_codes,
    )

    print("Top by hold=5")
    print(out[out["hold_days"] == 5].head(15).to_string(index=False))
    print("\nTop by hold=20")
    print(out[out["hold_days"] == 20].head(15).to_string(index=False))
    print("\nTop by hold=5 by filter")
    print(
        out[(out["hold_days"] == 5)]
        .sort_values(["trade_count", "total_return", "sharpe"], ascending=[False, False, False])
        .head(15)
        .to_string(index=False)
    )
    print("\nTop by hold=20 by filter")
    print(
        out[(out["hold_days"] == 20)]
        .sort_values(["trade_count", "total_return", "sharpe"], ascending=[False, False, False])
        .head(15)
        .to_string(index=False)
    )
    print("\nBest saved to:")
    print(Path(args.output_dir).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
