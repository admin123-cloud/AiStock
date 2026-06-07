"""
全量检查股票/指数 K 线完整性（ClickHouse）。

检查维度：
1) 日线（kline_daily）按交易日历检查覆盖率
2) 5 分钟（kline_minute_5）按交易日历 * 48 根/日检查覆盖率
3) 15 分钟（kline_minute_15）按交易日历 * 16 根/日检查覆盖率
4) 重复行、空值风险（按周期）

输出：
- 控制台摘要
- CSV 明细（默认输出到 data/reports/kline_audit_YYYYmmdd_HHMMSS）
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_query_df


REPO_ROOT = PROJECT_ROOT
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "reports"

MINUTE_BARS_PER_DAY = {
    "5m": 48,
    "15m": 16,
    "30m": 8,
    "60m": 4,
}


@dataclass
class PeriodSpec:
    period: str
    table: str
    date_expr: str
    expected_per_day: int


PERIOD_SPECS: List[PeriodSpec] = [
    PeriodSpec(period="1d", table="kline_daily", date_expr="trade_date", expected_per_day=1),
    PeriodSpec(period="5m", table="kline_minute_5", date_expr="toDate(datetime)", expected_per_day=MINUTE_BARS_PER_DAY["5m"]),
    PeriodSpec(period="15m", table="kline_minute_15", date_expr="toDate(datetime)", expected_per_day=MINUTE_BARS_PER_DAY["15m"]),
    PeriodSpec(period="30m", table="kline_minute_30", date_expr="toDate(datetime)", expected_per_day=MINUTE_BARS_PER_DAY["30m"]),
    PeriodSpec(period="60m", table="kline_minute_60", date_expr="toDate(datetime)", expected_per_day=MINUTE_BARS_PER_DAY["60m"]),
]


def _norm_date(v: object) -> Optional[date]:
    if v is None:
        return None
    ts = pd.to_datetime(v, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _load_trade_days(start_date: date, end_date: date, market: str) -> List[date]:
    sql = """
    SELECT trade_date
    FROM trade_calendar
    WHERE market = ?
      AND is_trading = 1
      AND trade_date >= ?
      AND trade_date <= ?
    ORDER BY trade_date
    """
    df = clickhouse_query_df(sql, [market, start_date, end_date])
    if df.empty:
        return []
    return [_norm_date(x) for x in df["trade_date"].tolist() if _norm_date(x) is not None]


def _load_symbols(asset_type: str) -> pd.DataFrame:
    if asset_type == "all":
        sql = """
        SELECT code, name, type, market, list_date
        FROM stocks
        WHERE (quit = 0 OR quit IS NULL)
          AND type IN ('stock', 'index')
        ORDER BY type, code
        """
        return clickhouse_query_df(sql)

    sql = """
    SELECT code, name, type, market, list_date
    FROM stocks
    WHERE (quit = 0 OR quit IS NULL)
      AND type = ?
    ORDER BY code
    """
    return clickhouse_query_df(sql, [asset_type])


def _compute_expected_days(symbols: pd.DataFrame, trade_days: List[date], end_date: date) -> pd.Series:
    arr = np.array(trade_days, dtype="datetime64[D]")
    if arr.size == 0:
        return pd.Series([0] * len(symbols), index=symbols.index, dtype="int64")

    vals: List[int] = []
    for _, row in symbols.iterrows():
        ld = _norm_date(row.get("list_date"))
        if ld is None:
            ld = trade_days[0]
        if ld > end_date:
            vals.append(0)
            continue
        idx = int(np.searchsorted(arr, np.datetime64(ld), side="left"))
        vals.append(max(0, len(trade_days) - idx))
    return pd.Series(vals, index=symbols.index, dtype="int64")


def _load_actual_counts(table: str, date_expr: str, start_date: date, end_date: date, asset_type: str) -> pd.DataFrame:
    uniq_expr = "uniqExact(tuple(k.code, k.datetime))" if "minute" in table else "uniqExact(tuple(k.code, k.trade_date))"
    sql = f"""
    SELECT
        k.code AS code,
        count() AS raw_rows,
        {uniq_expr} AS uniq_rows,
        countDistinct({date_expr}) AS covered_days
    FROM {table} k
    INNER JOIN stocks s ON s.code = k.code
    WHERE {date_expr} >= ?
      AND {date_expr} <= ?
      AND (s.quit = 0 OR s.quit IS NULL)
      AND (
        (? = 'all' AND s.type IN ('stock', 'index'))
        OR s.type = ?
      )
    GROUP BY k.code
    """
    return clickhouse_query_df(sql, [start_date, end_date, asset_type, asset_type])


def _load_data_quality(table: str, date_expr: str, start_date: date, end_date: date, asset_type: str) -> Dict[str, int]:
    # 重复：总行数 - 唯一(code, datetime/date)
    uniq_expr = "uniqExact(tuple(k.code, k.datetime))" if "minute" in table else "uniqExact(tuple(k.code, k.trade_date))"
    null_cols = "open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL"
    sql = f"""
    SELECT
        count() AS total_rows,
        {uniq_expr} AS uniq_rows,
        countIf({null_cols}) AS null_ohlc_rows
    FROM {table} k
    INNER JOIN stocks s ON s.code = k.code
    WHERE {date_expr} >= ?
      AND {date_expr} <= ?
      AND (s.quit = 0 OR s.quit IS NULL)
      AND (
        (? = 'all' AND s.type IN ('stock', 'index'))
        OR s.type = ?
      )
    """
    df = clickhouse_query_df(sql, [start_date, end_date, asset_type, asset_type])
    if df.empty:
        return {"total_rows": 0, "uniq_rows": 0, "dup_rows": 0, "null_ohlc_rows": 0}
    row = df.iloc[0]
    total_rows = int(row.get("total_rows") or 0)
    uniq_rows = int(row.get("uniq_rows") or 0)
    dup_rows = max(0, total_rows - uniq_rows)
    null_rows = int(row.get("null_ohlc_rows") or 0)
    return {
        "total_rows": total_rows,
        "uniq_rows": uniq_rows,
        "dup_rows": dup_rows,
        "null_ohlc_rows": null_rows,
    }


def _audit_period(
    symbols: pd.DataFrame,
    expected_days: pd.Series,
    period_spec: PeriodSpec,
    start_date: date,
    end_date: date,
    asset_type: str,
) -> tuple[pd.DataFrame, Dict[str, object]]:
    actual = _load_actual_counts(
        table=period_spec.table,
        date_expr=period_spec.date_expr,
        start_date=start_date,
        end_date=end_date,
        asset_type=asset_type,
    )
    merged = symbols[["code", "name", "type", "market", "list_date"]].copy()
    merged["expected_days"] = expected_days.values
    merged["expected_rows"] = merged["expected_days"] * period_spec.expected_per_day

    if actual.empty:
        merged["raw_rows"] = 0
        merged["uniq_rows"] = 0
        merged["covered_days"] = 0
    else:
        actual = actual.rename(columns={"raw_rows": "raw_rows", "covered_days": "covered_days"})
        merged = merged.merge(actual, on="code", how="left")
        merged["raw_rows"] = merged["raw_rows"].fillna(0).astype("int64")
        merged["uniq_rows"] = merged["uniq_rows"].fillna(0).astype("int64")
        merged["covered_days"] = merged["covered_days"].fillna(0).astype("int64")

    merged["missing_days"] = (merged["expected_days"] - merged["covered_days"]).clip(lower=0)
    merged["effective_covered_days"] = merged[["covered_days", "expected_days"]].min(axis=1)
    merged["effective_uniq_rows"] = merged[["uniq_rows", "expected_rows"]].min(axis=1)
    merged["excess_days"] = (merged["covered_days"] - merged["expected_days"]).clip(lower=0)
    merged["excess_rows"] = (merged["uniq_rows"] - merged["expected_rows"]).clip(lower=0)
    merged["missing_rows"] = (merged["expected_rows"] - merged["effective_uniq_rows"]).clip(lower=0)
    merged["coverage_days_pct"] = np.where(
        merged["expected_days"] > 0,
        merged["effective_covered_days"] / merged["expected_days"] * 100.0,
        100.0,
    )
    merged["coverage_rows_pct"] = np.where(
        merged["expected_rows"] > 0,
        merged["effective_uniq_rows"] / merged["expected_rows"] * 100.0,
        100.0,
    )
    merged["is_complete"] = (merged["missing_days"] == 0) & (merged["missing_rows"] == 0)
    merged["period"] = period_spec.period
    merged = merged.sort_values(["is_complete", "coverage_rows_pct", "code"], ascending=[True, True, True]).reset_index(drop=True)

    quality = _load_data_quality(
        table=period_spec.table,
        date_expr=period_spec.date_expr,
        start_date=start_date,
        end_date=end_date,
        asset_type=asset_type,
    )

    summary = {
        "period": period_spec.period,
        "table": period_spec.table,
        "symbols": int(len(merged)),
        "complete_symbols": int(merged["is_complete"].sum()),
        "incomplete_symbols": int((~merged["is_complete"]).sum()),
        "expected_days_total": int(merged["expected_days"].sum()),
        "covered_days_total": int(merged["covered_days"].sum()),
        "effective_covered_days_total": int(merged["effective_covered_days"].sum()),
        "expected_rows_total": int(merged["expected_rows"].sum()),
        "raw_rows_total": int(merged["raw_rows"].sum()),
        "uniq_rows_total": int(merged["uniq_rows"].sum()),
        "effective_uniq_rows_total": int(merged["effective_uniq_rows"].sum()),
        "missing_days_total": int(merged["missing_days"].sum()),
        "missing_rows_total": int(merged["missing_rows"].sum()),
        "excess_days_total": int(merged["excess_days"].sum()),
        "excess_rows_total": int(merged["excess_rows"].sum()),
        "coverage_days_pct": round(
            (float(merged["effective_covered_days"].sum()) / float(max(1, merged["expected_days"].sum()))) * 100.0,
            4,
        ),
        "coverage_rows_pct": round(
            (float(merged["effective_uniq_rows"].sum()) / float(max(1, merged["expected_rows"].sum()))) * 100.0,
            4,
        ),
        **quality,
    }
    return merged, summary


def run_audit(
    asset_type: str,
    market: str,
    start_date: Optional[date],
    end_date: date,
    out_dir: Path,
    top_n: int,
) -> int:
    symbols = _load_symbols(asset_type)
    if symbols.empty:
        print("[ERROR] stocks 表无可检查证券")
        return 2

    symbols["list_date"] = pd.to_datetime(symbols["list_date"], errors="coerce").dt.date
    min_list_date = symbols["list_date"].dropna().min()
    if min_list_date is None:
        min_list_date = end_date
    if start_date is None:
        start_date = min_list_date
    if start_date > end_date:
        print(f"[ERROR] start_date({start_date}) > end_date({end_date})")
        return 2

    trade_days = _load_trade_days(start_date=start_date, end_date=end_date, market=market)
    if not trade_days:
        print(f"[ERROR] trade_calendar 无交易日: market={market}, range={start_date}~{end_date}")
        return 2

    expected_days = _compute_expected_days(symbols=symbols, trade_days=trade_days, end_date=end_date)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = out_dir / f"kline_audit_{timestamp}"
    report_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 96)
    print("K线完整性检查（ClickHouse）")
    print(f"asset_type={asset_type}, market={market}, range={start_date}~{end_date}, trade_days={len(trade_days)}")
    print(f"symbols={len(symbols)}, report_dir={report_dir}")
    print("=" * 96)

    summary_rows: List[Dict[str, object]] = []
    for spec in PERIOD_SPECS:
        detail_df, summary = _audit_period(
            symbols=symbols,
            expected_days=expected_days,
            period_spec=spec,
            start_date=start_date,
            end_date=end_date,
            asset_type=asset_type,
        )
        summary_rows.append(summary)

        detail_path = report_dir / f"detail_{spec.period}.csv"
        detail_df.to_csv(detail_path, index=False, encoding="utf-8-sig")

        bad_df = detail_df[~detail_df["is_complete"]].head(top_n)
        print(
            f"[{spec.period}] complete={summary['complete_symbols']}/{summary['symbols']}, "
            f"rows_coverage={summary['coverage_rows_pct']}%, days_coverage={summary['coverage_days_pct']}%, "
            f"missing_rows={summary['missing_rows_total']}, dup_rows={summary['dup_rows']}, null_ohlc={summary['null_ohlc_rows']}"
        )
        if not bad_df.empty:
            print(f"  Top{min(top_n, len(bad_df))} 不完整样本:")
            cols = ["code", "name", "type", "expected_rows", "raw_rows", "missing_rows", "coverage_rows_pct"]
            print(bad_df[cols].to_string(index=False))
        else:
            print("  所有证券完整")

    summary_df = pd.DataFrame(summary_rows)
    summary_path = report_dir / "summary.csv"
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")

    print("-" * 96)
    print("汇总:")
    print(summary_df.to_string(index=False))
    print("-" * 96)
    print(f"已输出: {summary_path}")
    print(f"明细: {report_dir / 'detail_1d.csv'}")
    print(f"明细: {report_dir / 'detail_5m.csv'}")
    print(f"明细: {report_dir / 'detail_15m.csv'}")

    # 若任一周期有不完整，返回码 1，便于 CI/自动化感知
    has_incomplete = any(int(x["incomplete_symbols"]) > 0 for x in summary_rows)
    return 1 if has_incomplete else 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="检查股票/指数在 1d/5m/15m 周期的完整性（按交易日历）")
    p.add_argument("--asset-type", choices=["stock", "index", "all"], default="all", help="检查范围")
    p.add_argument("--market", default="SH", help="交易日历市场（默认 SH）")
    p.add_argument("--start-date", default="", help="起始日期 YYYY-MM-DD；空=按证券最早上市日")
    p.add_argument("--end-date", default="", help="结束日期 YYYY-MM-DD；空=今天")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="报告输出目录")
    p.add_argument("--top-n", type=int, default=20, help="控制台展示不完整样本数量")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    end_date = _norm_date(args.end_date) if args.end_date else date.today()
    if end_date is None:
        raise ValueError(f"invalid --end-date: {args.end_date}")
    start_date = _norm_date(args.start_date) if args.start_date else None
    if args.start_date and start_date is None:
        raise ValueError(f"invalid --start-date: {args.start_date}")
    return run_audit(
        asset_type=args.asset_type,
        market=args.market,
        start_date=start_date,
        end_date=end_date,
        out_dir=Path(args.out_dir),
        top_n=max(1, int(args.top_n)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
