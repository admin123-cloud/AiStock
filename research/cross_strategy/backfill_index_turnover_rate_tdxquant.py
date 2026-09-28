from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from utils.market_warehouse import clickhouse_query_df


REPO_ROOT = Path(__file__).resolve().parents[1]
PARQUET_ROOT = REPO_ROOT / "data" / "warehouse" / "parquet" / "kline_daily"
REPORT_DIR = REPO_ROOT / "reports" / "index_turnover_backfill"

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill index historical turnover_rate from TdxQuant Volume and get_gb_info Ltgb."
    )
    parser.add_argument("--codes", default="", help="Comma separated index codes. Empty means all index codes with gaps.")
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD inclusive.")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD inclusive.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of index codes for smoke runs.")
    parser.add_argument("--chunk-size", type=int, default=250, help="Date count per get_gb_info call.")
    parser.add_argument("--dry-run", action="store_true", help="Compute only; do not rewrite parquet files.")
    parser.add_argument("--report-name", default="", help="Optional report filename.")
    return parser.parse_args()


def ensure_tq():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from data_fetcher.sources.tdxquant_pool import tdxquant_pool

    return tdxquant_pool.get_client(), "tdxquant_pool"


def date_text(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def ymd(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def chunked(items: List[str], size: int) -> Iterable[List[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def load_target_rows(args: argparse.Namespace) -> pd.DataFrame:
    filters = ["s.type='index'", "k.volume IS NOT NULL", "k.volume > 0", "coalesce(k.turnover_rate, 0) = 0"]
    params: List[Any] = []
    if args.start_date:
        filters.append("k.trade_date >= ?")
        params.append(args.start_date)
    if args.end_date:
        filters.append("k.trade_date <= ?")
        params.append(args.end_date)
    if args.codes.strip():
        codes = [item.strip().upper() for item in args.codes.split(",") if item.strip()]
        placeholders = ",".join(["?"] * len(codes))
        filters.append(f"k.code IN ({placeholders})")
        params.extend(codes)

    sql = f"""
        SELECT k.code, s.name, k.trade_date, k.volume, k.turnover_rate
        FROM kline_daily k
        JOIN stocks s ON s.code = k.code
        WHERE {' AND '.join(filters)}
        ORDER BY k.code, k.trade_date
    """
    df = clickhouse_query_df(sql, params)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    if args.limit and args.limit > 0:
        keep_codes = df["code"].drop_duplicates().head(args.limit).tolist()
        df = df[df["code"].isin(keep_codes)].copy()
    return df.reset_index(drop=True)


def fetch_ltgb_map(tq: Any, code: str, dates: List[str], chunk_size: int) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for part in chunked(dates, max(1, chunk_size)):
        try:
            rows = tq.get_gb_info(stock_code=code, date_list=[d.replace("-", "") for d in part], count=len(part))
        except Exception as exc:
            print(f"[warn] get_gb_info failed code={code} dates={part[0]}..{part[-1]} error={exc}")
            rows = []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_date = row.get("Date")
            raw_ltgb = row.get("Ltgb")
            try:
                key = pd.Timestamp(str(int(raw_date))).strftime("%Y-%m-%d")
                value = float(raw_ltgb)
            except Exception:
                continue
            if value > 0:
                result[key] = value
    return result


def build_updates(tq: Any, targets: pd.DataFrame, chunk_size: int) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    total_codes = targets["code"].nunique() if not targets.empty else 0
    started = time.time()
    for idx, (code, group) in enumerate(targets.groupby("code", sort=True), start=1):
        dates = group["trade_date"].drop_duplicates().sort_values().tolist()
        ltgb_map = fetch_ltgb_map(tq, str(code), dates, chunk_size)
        work = group.copy()
        work["ltgb"] = work["trade_date"].map(ltgb_map)
        work["computed_turnover_rate"] = (pd.to_numeric(work["volume"], errors="coerce") * 10000.0) / work["ltgb"]
        work = work.replace([float("inf"), -float("inf")], pd.NA)
        ok = work["computed_turnover_rate"].notna() & (work["computed_turnover_rate"] > 0)
        frames.append(work[ok][["code", "trade_date", "computed_turnover_rate", "ltgb"]].copy())
        print(
            f"[fetch] {idx}/{total_codes} {code} dates={len(dates)} ltgb={len(ltgb_map)} "
            f"updates={int(ok.sum())} elapsed={time.time() - started:.1f}s"
        )
    if not frames:
        return pd.DataFrame(columns=["code", "trade_date", "computed_turnover_rate", "ltgb"])
    return pd.concat(frames, ignore_index=True)


def rewrite_parquet_partitions(updates: pd.DataFrame, dry_run: bool) -> Dict[str, Any]:
    if updates.empty:
        return {"files": [], "updated_rows": 0}
    updates = updates.copy()
    updates["trade_date"] = pd.to_datetime(updates["trade_date"]).dt.strftime("%Y-%m-%d")
    updates["year"] = pd.to_datetime(updates["trade_date"]).dt.year
    files: List[Dict[str, Any]] = []
    total_updated = 0
    for year, year_updates in updates.groupby("year", sort=True):
        parquet_path = PARQUET_ROOT / f"year={int(year)}" / "data.parquet"
        if not parquet_path.exists():
            files.append({"year": int(year), "path": str(parquet_path), "status": "missing", "updated_rows": 0})
            continue
        df = pd.read_parquet(parquet_path)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        key_to_value = {
            (str(row.code), str(row.trade_date)): float(row.computed_turnover_rate)
            for row in year_updates.itertuples(index=False)
        }
        mask = df.apply(lambda row: (str(row["code"]), str(row["trade_date"])) in key_to_value, axis=1)
        update_count = int(mask.sum())
        if update_count:
            df.loc[mask, "turnover_rate"] = df.loc[mask].apply(
                lambda row: key_to_value[(str(row["code"]), str(row["trade_date"]))],
                axis=1,
            )
            if not dry_run:
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
                df.to_parquet(parquet_path, index=False)
        total_updated += update_count
        files.append(
            {
                "year": int(year),
                "path": str(parquet_path),
                "status": "dry_run" if dry_run else "updated",
                "updated_rows": update_count,
            }
        )
        print(f"[write] year={year} rows={update_count} dry_run={dry_run}")
    return {"files": files, "updated_rows": total_updated}


def validate_updates(updates: pd.DataFrame) -> Dict[str, Any]:
    """Validate the computed turnover rates against existing ClickHouse data."""
    if updates.empty:
        return {"sample": [], "summary": {}}
    sample_keys = updates.sort_values(["code", "trade_date"]).groupby("code").tail(2).head(30)

    # Load ClickHouse data for validation codes/dates
    codes = sample_keys["code"].tolist()
    dates = sample_keys["trade_date"].tolist()
    placeholders = ",".join(["?"] * len(codes))
    date_placeholders = ",".join(["?"] * len(dates))

    # Query ClickHouse for validation data
    ch_df = clickhouse_query_df(
        f"""
        SELECT k.code, s.name, k.trade_date, k.volume, k.turnover_rate
        FROM kline_daily k
        JOIN stocks s ON s.code = k.code
        WHERE k.code IN ({placeholders}) AND k.trade_date IN ({date_placeholders})
        ORDER BY k.code, k.trade_date
        """,
        codes + dates,
    )
    if ch_df.empty:
        return {"sample": [], "summary": {}, "gap_summary": {"index_rows": 0, "zero_turnover": 0, "positive_turnover": 0}}

    # Merge with computed values
    sample_keys_lookup = dict(zip(
        zip(sample_keys["code"], sample_keys["trade_date"]),
        zip(sample_keys["computed_turnover_rate"], sample_keys["ltgb"])
    ))
    ch_df["computed_turnover_rate"] = ch_df.apply(
        lambda r: sample_keys_lookup.get((str(r["code"]), str(r["trade_date"])), (None, None))[0],
        axis=1
    )
    ch_df["ltgb"] = ch_df.apply(
        lambda r: sample_keys_lookup.get((str(r["code"]), str(r["trade_date"])), (None, None))[1],
        axis=1
    )
    ch_df["abs_diff"] = (ch_df["turnover_rate"] - ch_df["computed_turnover_rate"]).abs()

    sample = json.loads(ch_df.to_json(orient="records", force_ascii=False, date_format="iso"))

    # Summary stats
    rows_checked = len(ch_df)
    max_abs_diff = float(ch_df["abs_diff"].max()) if not ch_df["abs_diff"].isna().all() else 0
    avg_abs_diff = float(ch_df["abs_diff"].mean()) if not ch_df["abs_diff"].isna().all() else 0
    positive_rows = int((ch_df["turnover_rate"] > 0).sum())

    # Gap summary: query ClickHouse
    gap_df = clickhouse_query_df(
        """
        SELECT count(*) AS index_rows,
               sum(case when coalesce(k.turnover_rate,0)=0 then 1 else 0 end) AS zero_turnover,
               sum(case when k.turnover_rate > 0 then 1 else 0 end) AS positive_turnover
        FROM kline_daily k
        JOIN stocks s ON s.code = k.code
        WHERE s.type='index'
        """
    )
    gap_summary = json.loads(gap_df.to_json(orient="records", force_ascii=False))[0] if not gap_df.empty else {}

    return {
        "sample": sample,
        "summary": {"rows_checked": rows_checked, "max_abs_diff": max_abs_diff, "avg_abs_diff": avg_abs_diff, "positive_rows": positive_rows},
        "gap_summary": gap_summary,
    }


def main() -> int:
    args = parse_args()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    targets = load_target_rows(args)
    print(f"[target] rows={len(targets)} codes={targets['code'].nunique() if not targets.empty else 0}")
    if targets.empty:
        return 0

    tq = None
    init_path = ""
    try:
        tq, init_path = ensure_tq()
        print(f"[tdx] initialized init_path={init_path}")
        updates = build_updates(tq, targets, args.chunk_size)
    finally:
        if tq is not None:
            try:
                tq.close()
            except Exception:
                pass

    print(f"[updates] rows={len(updates)} codes={updates['code'].nunique() if not updates.empty else 0}")
    write_result = rewrite_parquet_partitions(updates, args.dry_run)
    validation = validate_updates(updates) if not args.dry_run else {"skipped": "dry_run"}
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "args": vars(args),
        "tdx_init_path": init_path,
        "target_rows": int(len(targets)),
        "target_codes": int(targets["code"].nunique()) if not targets.empty else 0,
        "computed_rows": int(len(updates)),
        "computed_codes": int(updates["code"].nunique()) if not updates.empty else 0,
        "write_result": write_result,
        "validation": validation,
    }
    report_name = args.report_name or f"index_turnover_backfill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path = REPORT_DIR / report_name
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[report] {report_path}")
    print(json.dumps(report["validation"], ensure_ascii=False, indent=2, default=str)[:5000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
