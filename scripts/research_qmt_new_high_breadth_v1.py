"""Build QMT-canonical A-share new-high breadth research series.

This is intentionally a market-breadth indicator, not a synthetic price index.
It is independent from Tonghuashun proprietary indexes such as 883408/883416/883911.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402
from utils.paths import report_path  # noqa: E402


DEFAULT_OUTPUT_DIR = report_path("qmt_new_high_breadth_v1")
LOOKBACKS = (20, 100)


def _query_breadth(start_date: str, end_date: str) -> pd.DataFrame:
    """Compute only in ClickHouse to retain full listing history without a pandas-sized raw scan."""
    ch = clickhouse_client()
    sql = f"""
    WITH source AS (
        SELECT
            d.code,
            d.trade_date,
            d.close,
            max(d.high) OVER (
                PARTITION BY d.code ORDER BY d.trade_date
                ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prior_high_20,
            max(d.high) OVER (
                PARTITION BY d.code ORDER BY d.trade_date
                ROWS BETWEEN 100 PRECEDING AND 1 PRECEDING
            ) AS prior_high_100,
            max(d.high) OVER (
                PARTITION BY d.code ORDER BY d.trade_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS prior_high_all
        FROM kline_daily AS d
        INNER JOIN stocks AS s ON d.code = s.code
        WHERE s.type = 'stock'
          AND (s.list_date IS NULL OR d.trade_date >= s.list_date)
          AND (s.delist_date IS NULL OR d.trade_date <= s.delist_date)
          AND d.close > 0
          AND d.high > 0
    )
    SELECT
        trade_date,
        count() AS eligible_stocks,
        countIf(prior_high_20 > 0) AS eligible_nh20,
        countIf(prior_high_100 > 0) AS eligible_nh100,
        countIf(prior_high_all > 0) AS eligible_nhall,
        countIf(prior_high_20 > 0 AND close >= prior_high_20) AS new_high_20_count,
        countIf(prior_high_100 > 0 AND close >= prior_high_100) AS new_high_100_count,
        countIf(prior_high_all > 0 AND close >= prior_high_all) AS new_high_all_count
    FROM source
    WHERE trade_date BETWEEN toDate('{start_date}') AND toDate('{end_date}')
    GROUP BY trade_date
    ORDER BY trade_date
    """
    return ch.query_df(sql)


def build(start_date: str, end_date: str, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    df = _query_breadth(start_date, end_date)
    if df.empty:
        raise RuntimeError("QMT canonical daily data returned no new-high breadth rows")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for lookback in LOOKBACKS:
        count_col = f"new_high_{lookback}_count"
        eligible_col = f"eligible_nh{lookback}"
        breadth_col = f"nh{lookback}_breadth"
        df[breadth_col] = df[count_col] / df[eligible_col].where(df[eligible_col] > 0)
        df[f"nh{lookback}_change_1d"] = df[breadth_col].diff()
        df[f"nh{lookback}_ma5"] = df[breadth_col].rolling(5, min_periods=5).mean()
        df[f"nh{lookback}_accel"] = df[breadth_col] - df[f"nh{lookback}_ma5"]
    df["nhall_breadth"] = df["new_high_all_count"] / df["eligible_nhall"].where(df["eligible_nhall"] > 0)
    df["nhall_change_1d"] = df["nhall_breadth"].diff()
    df["nhall_ma5"] = df["nhall_breadth"].rolling(5, min_periods=5).mean()
    df["nhall_accel"] = df["nhall_breadth"] - df["nhall_ma5"]

    # A declared, auditable environment flag; it is not promoted as a trading gate.
    df["breakout_environment"] = (
        (df["nh100_breadth"] > df["nh100_ma5"])
        & (df["nh100_change_1d"] >= 0)
        & (df["nhall_breadth"] > df["nhall_ma5"])
    )
    df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
    df.to_parquet(output_dir / "new_high_breadth_daily.parquet", index=False)
    df.to_csv(output_dir / "new_high_breadth_daily.csv", index=False, encoding="utf-8-sig")

    latest = df.iloc[-1].to_dict()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "research_only": True,
        "source": "QMT canonical stocks + kline_daily in ClickHouse",
        "start_date": start_date,
        "end_date": end_date,
        "definition": {
            "new_high_20": "close >= prior 20 available daily-bar highs",
            "new_high_100": "close >= prior 100 available daily-bar highs",
            "new_high_all": "close >= all prior available daily-bar highs since listing",
            "universe": "A-share stocks listed on the observation date; excludes no stocks merely because they later delisted",
            "breakout_environment": "NH100 and NHALL are both above their 5-day means; NH100 is non-declining day-over-day",
        },
        "rows": int(len(df)),
        "latest": latest,
        "outputs": {"daily": "new_high_breadth_daily.parquet", "csv": "new_high_breadth_daily.csv"},
        "limitations": [
            "This is a QMT-canonical breadth series, not a replication of Tonghuashun proprietary price indexes.",
            "Historical ST status is not reconstructed because the current master flag is not point-in-time.",
            "The environment flag is a research variable only and must pass a separate out-of-sample portfolio replay before promotion.",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        "# QMT 全 A 股新高广度 v1",
        "",
        "研究用途，不复制同花顺专有价格指数，也不接入实盘。",
        "",
        "| 日期 | NH20 | NH100 | NHALL | 突破环境 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in df.tail(20).itertuples(index=False):
        lines.append(
            f"| {row.trade_date} | {row.nh20_breadth:.2%} | {row.nh100_breadth:.2%} | {row.nhall_breadth:.2%} | {'是' if row.breakout_environment else '否'} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build QMT canonical new-high breadth research series")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-07-22")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    print(json.dumps(build(args.start_date, args.end_date, Path(args.output_dir)), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
