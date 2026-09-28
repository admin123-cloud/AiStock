from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_fetcher.sources.tdxquant_pool import tdxquant_pool  # noqa: E402
from utils.market_warehouse import clickhouse_client  # noqa: E402


DEFAULT_CANDIDATES = ROOT / "reports" / "gen2_candidate_outcome_supervision" / "labeled_candidates.parquet"
DEFAULT_OUTPUT = ROOT / "data" / "runtime" / "tdx_share_cap_history.parquet"


def _load_code_ranges(candidates: Path, start_date: str, end_date: str) -> pd.DataFrame:
    d = pd.read_parquet(candidates)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    if d.empty:
        return pd.DataFrame(columns=["code", "start_date", "end_date"])
    ranges = d.groupby("code", as_index=False).agg(
        min_date=("entry_date", "min"),
        max_date=("entry_date", "max"),
    )
    ranges["start_date"] = pd.to_datetime(ranges["min_date"]) - pd.Timedelta(days=430)
    ranges["end_date"] = pd.to_datetime(ranges["max_date"])
    ranges["start_date"] = ranges["start_date"].dt.strftime("%Y-%m-%d")
    ranges["end_date"] = ranges["end_date"].dt.strftime("%Y-%m-%d")
    return ranges[["code", "start_date", "end_date"]]


def _fallback_trade_dates(code: str, start_date: str, end_date: str) -> list[str]:
    ch = clickhouse_client()
    rows = ch.query_df(
        f"""
        SELECT trade_date
        FROM kline_daily
        WHERE code = '{code}'
          AND trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY trade_date
        """
    )
    if rows.empty:
        return []
    return pd.to_datetime(rows["trade_date"], errors="coerce").dt.strftime("%Y%m%d").dropna().tolist()


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if number > 0 else None
    except Exception:
        return None


def _normalize_rows(code: str, rows: list[dict[str, Any]] | Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if rows is None:
        return out
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = row.get("Date") or row.get("date") or row.get("日期")
        if date is None:
            continue
        trade_date = pd.to_datetime(str(date), errors="coerce")
        if pd.isna(trade_date):
            continue
        zgb = _as_float(row.get("Zgb") or row.get("zgb") or row.get("总股本"))
        ltgb = _as_float(row.get("Ltgb") or row.get("ltgb") or row.get("流通股本"))
        out.append(
            {
                "code": code,
                "trade_date": trade_date.strftime("%Y-%m-%d"),
                "total_share": zgb,
                "float_share": ltgb,
                "total_share_wan": zgb / 10000.0 if zgb else None,
                "float_share_wan": ltgb / 10000.0 if ltgb else None,
            }
        )
    return out


def run(args: argparse.Namespace) -> pd.DataFrame:
    ranges = _load_code_ranges(Path(args.candidates), str(args.start_date), str(args.end_date))
    if args.codes:
        wanted = {x.strip() for x in str(args.codes).split(",") if x.strip()}
        ranges = ranges[ranges["code"].isin(wanted)].copy()
    if args.limit:
        ranges = ranges.head(int(args.limit)).copy()
    records: list[dict[str, Any]] = []
    for row in ranges.itertuples(index=False):
        code = str(row.code)
        start = str(row.start_date)
        end = str(row.end_date)
        raw = tdxquant_pool.get_gb_info_by_date(code, start, end)
        if raw is None:
            dates = _fallback_trade_dates(code, start, end)
            if dates:
                raw = tdxquant_pool.get_gb_info(code, dates, len(dates))
        normalized = _normalize_rows(code, raw)
        print(f"{code} {start}..{end} rows={len(normalized)}")
        records.extend(normalized)
    out = pd.DataFrame(records)
    if out.empty:
        raise RuntimeError("No share-cap rows fetched from TdxQuant.")
    out = out.drop_duplicates(["code", "trade_date"], keep="last").sort_values(["code", "trade_date"]).reset_index(drop=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(output, index=False)
    out.to_csv(output.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical Zgb/Ltgb share-cap data from TdxQuant.")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--codes", default="")
    parser.add_argument("--limit", type=int, default=0)
    df = run(parser.parse_args())
    print({"rows": int(len(df)), "codes": int(df["code"].nunique())})


if __name__ == "__main__":
    main()
