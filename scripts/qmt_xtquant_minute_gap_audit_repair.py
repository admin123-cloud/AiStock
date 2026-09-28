from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.qmtmini_daily_backfill_validate import ch_client, quote_sql
from utils.paths import report_path
from utils.qmt_universe import qmt_universe_filter_sql


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
PERIOD_TABLES = {
    "5m": ("kline_minute_5", 48),
    "15m": ("kline_minute_15", 16),
    "30m": ("kline_minute_30", 8),
    "60m": ("kline_minute_60", 4),
}


def regular_time_sql(period: str, column: str = "datetime") -> str:
    step = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}[period]
    values: list[str] = []
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)):
        current = start + step
        while current <= end:
            values.append(f"({current // 60},{current % 60})")
            current += step
    return f"(toHour({column}), toMinute({column})) IN ({','.join(values)})"


def log(message: str) -> None:
    print(f"[{datetime.now(BUSINESS_TZ):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def json_default(value: Any) -> str:
    return str(value)


def clean_text_tail(value: str | None, limit: int = 4000) -> str:
    text = (value or "").replace("\x00", "")
    return text[-int(limit) :]


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    step = max(1, int(size))
    for idx in range(0, len(items), step):
        yield items[idx : idx + step]


def parse_periods(value: str) -> list[str]:
    periods = [item.strip() for item in str(value or "").split(",") if item.strip()]
    bad = [item for item in periods if item not in PERIOD_TABLES]
    if bad:
        raise ValueError(f"unsupported periods: {bad}; supported={sorted(PERIOD_TABLES)}")
    return periods or ["5m"]


def universe_filter_sql(code_expr: str, universe: str) -> str:
    return qmt_universe_filter_sql(
        universe,
        code_expr=code_expr,
        type_expr="s.type",
        name_expr="s.name",
    )


def load_trade_dates(client: Any, start_date: str, end_date: str, max_dates: int = 0, order: str = "asc") -> list[str]:
    rows = client.query(
        f"""
        SELECT trade_date
        FROM trade_calendar
        WHERE market = 'SH'
          AND is_trading = 1
          AND trade_date >= toDate({quote_sql(start_date)})
          AND trade_date <= toDate({quote_sql(end_date)})
        ORDER BY trade_date {"DESC" if order == "desc" else "ASC"}
        {f"LIMIT {int(max_dates)}" if int(max_dates or 0) > 0 else ""}
        """
    ).result_rows
    dates = [str(row[0]) for row in rows]
    return list(reversed(dates)) if order == "desc" and int(max_dates or 0) > 0 else dates


def parse_code_list(value: str) -> list[str]:
    return [item.strip().upper() for item in str(value or "").split(",") if item.strip()]


def load_universe(client: Any, universe: str, codes: list[str] | None = None) -> pd.DataFrame:
    filt = universe_filter_sql("s.code", universe)
    code_filter = ""
    wanted = list(dict.fromkeys(codes or []))
    if wanted:
        code_filter = "AND s.code IN (" + ", ".join(quote_sql(code) for code in wanted) + ")"
    df = client.query_df(
        f"""
        SELECT
            s.code,
            any(s.name) AS name,
            any(s.type) AS stock_type,
            min(toDateOrNull(toString(s.list_date))) AS list_date
        FROM stocks s
        WHERE (s.quit = 0 OR s.quit IS NULL)
          AND ({filt})
          {code_filter}
        GROUP BY s.code
        ORDER BY s.code
        """
    )
    if wanted:
        existing = {str(code).upper() for code in df["code"].tolist()} if not df.empty else set()
        missing = [code for code in wanted if code not in existing]
        if missing:
            df = pd.concat(
                [
                    df,
                    pd.DataFrame(
                        [
                            {"code": code, "name": "", "stock_type": "manual", "list_date": pd.NaT}
                            for code in missing
                        ]
                    ),
                ],
                ignore_index=True,
            )
    df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce").dt.date
    return df


def expected_codes_for_date(client: Any, universe: pd.DataFrame, trade_date: str) -> list[str]:
    day = pd.Timestamp(trade_date).normalize()
    list_dates = pd.to_datetime(universe["list_date"], errors="coerce").dt.normalize()
    rows = universe[list_dates.isna() | (list_dates <= day)]
    listed_codes = {str(code) for code in rows["code"].tolist()}
    daily_rows = client.query(
        f"""
        SELECT DISTINCT code
        FROM kline_daily
        WHERE trade_date = toDate({quote_sql(trade_date)})
          -- A zero-volume placeholder is emitted for securities that are not
          -- yet listed (and for all-day suspensions).  It must not create a
          -- minute-bar repair obligation for that date.
          AND (volume > 0 OR amount > 0)
        """
    ).result_rows
    daily_codes = {str(row[0]) for row in daily_rows if row and row[0]}
    return sorted(listed_codes & daily_codes)


def audit_date_period(
    client: Any,
    universe: pd.DataFrame,
    trade_date: str,
    period: str,
    *,
    chunk_size: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    table, expected_bars = PERIOD_TABLES[period]
    next_day = (pd.Timestamp(trade_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    codes = expected_codes_for_date(client, universe, trade_date)
    meta = universe.set_index("code").to_dict("index")
    issues: list[dict[str, Any]] = []
    total_rows = 0
    complete_codes = 0
    started = time.perf_counter()

    for idx, code_chunk in enumerate(chunked(codes, chunk_size), start=1):
        code_sql = ",".join(quote_sql(code) for code in code_chunk)
        df = client.query_df(
            f"""
            SELECT
                assumeNotNull(code) AS code,
                count() AS bars,
                countIf(NOT ({regular_time_sql(period)})) AS invalid_session_bars,
                min(assumeNotNull(datetime)) AS min_dt,
                max(assumeNotNull(datetime)) AS max_dt
            FROM {table}
            WHERE code IN ({code_sql})
              AND datetime >= toDateTime({quote_sql(trade_date + " 00:00:00")})
              AND datetime < toDateTime({quote_sql(next_day + " 00:00:00")})
            GROUP BY code
            """
        )
        actual = {str(row.code): row for row in df.itertuples(index=False)}
        for code in code_chunk:
            row = actual.get(code)
            bars = int(row.bars or 0) if row is not None else 0
            invalid_session_bars = int(row.invalid_session_bars or 0) if row is not None else 0
            total_rows += bars
            min_dt = str(row.min_dt) if row is not None and row.min_dt is not None else ""
            max_dt = str(row.max_dt) if row is not None and row.max_dt is not None else ""
            reason = ""
            if bars == 0:
                reason = "missing"
            elif bars < expected_bars:
                reason = "incomplete"
            elif min_dt and min_dt < trade_date + " 09:00:00":
                reason = "bad_early_time"
            elif max_dt and max_dt < trade_date + " 15:00:00":
                reason = "incomplete_tail"
            elif invalid_session_bars > 0:
                reason = "bad_session_time"
            if reason:
                info = meta.get(code, {})
                issues.append(
                    {
                        "trade_date": trade_date,
                        "period": period,
                        "code": code,
                        "name": info.get("name"),
                        "stock_type": info.get("stock_type"),
                        "bars": bars,
                        "invalid_session_bars": invalid_session_bars,
                        "min_dt": min_dt,
                        "max_dt": max_dt,
                        "reason": reason,
                    }
                )
            else:
                complete_codes += 1
        if idx % 10 == 0:
            log(f"audit {trade_date} {period} chunk={idx} codes={len(codes)} elapsed={time.perf_counter() - started:.1f}s")

    expected_rows = len(codes) * expected_bars
    reason_counts = dict(Counter(item["reason"] for item in issues))
    summary = {
        "trade_date": trade_date,
        "period": period,
        "table": table,
        "expected_bars_per_day": expected_bars,
        "expected_codes": len(codes),
        "complete_codes": complete_codes,
        "issue_codes": len(issues),
        "reason_counts": reason_counts,
        "expected_rows": expected_rows,
        "actual_rows": total_rows,
        "missing_rows": max(expected_rows - total_rows, 0),
        "row_coverage_pct": round(total_rows / max(1, expected_rows) * 100.0, 4),
        "elapsed_sec": round(time.perf_counter() - started, 3),
    }
    return summary, issues


def build_repair_cmd(args: argparse.Namespace, trade_date: str, codes: list[str], report_file: Path) -> list[str]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "qmt_xtquant_minute_backfill_validate.py"),
        "--phase",
        "all",
        "--start-date",
        trade_date,
        "--end-date",
        trade_date,
        "--codes",
        ",".join(codes),
        "--periods",
        args.periods,
        "--batch-size",
        str(args.repair_batch_size),
        "--batch-timeout-sec",
        str(args.batch_timeout_sec),
        "--max-retries",
        str(args.max_retries),
        "--retry-sleep",
        str(args.retry_sleep),
        "--min-5m-bars-per-day",
        "48",
        "--min-source-5m-bars-per-day",
        "48",
        "--reset-stage",
        "--reset-stage-codes-only",
        # QMT's own downloader is synchronous.  Keeping a normal-sized
        # repair batch in this process avoids the Windows spawn/IPC stall
        # observed when every batch is wrapped in another Python process.
        "--in-process",
        "--report",
        str(report_file),
    ]
    if args.skip_download:
        cmd.append("--skip-download")
    if args.repair_incomplete_cache:
        cmd.append("--repair-incomplete-cache")
    return cmd


def run_repair(args: argparse.Namespace, run_dir: Path, trade_date: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
    codes = sorted({str(item["code"]) for item in issues if item.get("code")})
    if args.repair_code_offset > 0:
        codes = codes[args.repair_code_offset :]
    if args.max_repair_codes > 0:
        codes = codes[: args.max_repair_codes]
    if not codes:
        return {"ok": True, "trade_date": trade_date, "skipped": "no_issue_codes"}
    results: list[dict[str, Any]] = []
    failures = 0
    offset_tag = f"offset_{max(0, int(args.repair_code_offset or 0)):06d}"
    chunks = list(chunked(codes, args.repair_code_chunk_size))

    def run_chunk(item: tuple[int, list[str]]) -> dict[str, Any]:
        idx, code_chunk = item
        report_file = run_dir / "repair_reports" / f"repair_{trade_date.replace('-', '')}_{offset_tag}_{idx:04d}.json"
        cmd = build_repair_cmd(args, trade_date, code_chunk, report_file)
        log(f"repair {trade_date} chunk={idx} codes={len(code_chunk)}")
        if args.dry_run:
            result = {"ok": True, "dry_run": True, "cmd": cmd, "report": str(report_file)}
        else:
            started = time.perf_counter()
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                stdout, stderr = proc.communicate(timeout=args.chunk_timeout_sec)
                result = {
                    "ok": proc.returncode == 0,
                    "returncode": proc.returncode,
                    "elapsed_sec": round(time.perf_counter() - started, 3),
                    "cmd": cmd,
                    "report": str(report_file),
                    "stdout_tail": clean_text_tail(stdout),
                    "stderr_tail": clean_text_tail(stderr),
                }
            except subprocess.TimeoutExpired:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                except Exception:
                    proc.kill()
                stdout, stderr = proc.communicate()
                result = {
                    "ok": False,
                    "returncode": "timeout",
                    "elapsed_sec": round(time.perf_counter() - started, 3),
                    "cmd": cmd,
                    "report": str(report_file),
                    "stdout_tail": clean_text_tail(stdout),
                    "stderr_tail": clean_text_tail(stderr),
                    "error": f"repair chunk timeout after {args.chunk_timeout_sec}s",
                }
        result["chunk_index"] = idx
        result["codes"] = len(code_chunk)
        return result

    workers = max(1, int(args.repair_workers or 1))
    if workers <= 1 or len(chunks) <= 1:
        for item in enumerate(chunks, start=1):
            result = run_chunk(item)
            if not result.get("ok"):
                failures += 1
            results.append(result)
            if failures and args.stop_on_error:
                break
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run_chunk, item) for item in enumerate(chunks, start=1)]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if not result.get("ok"):
                    failures += 1
                results.append(result)
                if failures and args.stop_on_error:
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()
                    break
        results.sort(key=lambda item: int(item.get("chunk_index") or 0))
    return {
        "ok": failures == 0,
        "trade_date": trade_date,
        "repair_code_offset": args.repair_code_offset,
        "repair_codes": len(codes),
        "chunks": len(results),
        "repair_workers": workers,
        "failures": failures,
        "results": results[-10:],
    }


def parse_args() -> argparse.Namespace:
    today = datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="Audit and repair QMT minute gaps using code-chunked ClickHouse reads.")
    parser.add_argument("--mode", choices=["audit", "repair"], default="audit")
    parser.add_argument("--start-date", default=today)
    parser.add_argument("--end-date", default=today)
    parser.add_argument("--periods", default="5m")
    parser.add_argument("--universe", default="stock,index")
    parser.add_argument("--codes", default="", help="Comma-separated code list. When set, audit/repair only these codes.")
    parser.add_argument("--order", choices=["asc", "desc"], default="asc")
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--audit-code-chunk-size", type=int, default=400)
    parser.add_argument("--repair-code-chunk-size", type=int, default=40)
    parser.add_argument("--repair-workers", type=int, default=1)
    parser.add_argument("--repair-batch-size", type=int, default=1)
    parser.add_argument("--repair-code-offset", type=int, default=0, help="Skip this many sorted issue codes before repair, for rolling resumes.")
    parser.add_argument("--max-repair-codes", type=int, default=0)
    parser.add_argument("--batch-timeout-sec", type=int, default=180)
    parser.add_argument("--chunk-timeout-sec", type=int, default=7200)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=1.0)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--repair-incomplete-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--issue-file", default="", help="Reuse a previous issues.csv for repair instead of re-auditing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    periods = parse_periods(args.periods)
    explicit_codes = parse_code_list(args.codes)
    client = ch_client()
    trade_dates = load_trade_dates(client, args.start_date, args.end_date, args.max_dates, args.order)
    if not trade_dates:
        raise RuntimeError(f"no trading dates: {args.start_date}~{args.end_date}")
    # Keep the universe available for the post-repair audit too.  A repair
    # subprocess returning zero only proves it ran; it does not prove the
    # target bars are complete.
    universe = load_universe(client, args.universe, explicit_codes)
    stamp = datetime.now(BUSINESS_TZ).strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.report_dir) if args.report_dir else report_path("qmt_minute_gap_audit_repair", f"run_{stamp}")
    run_dir.mkdir(parents=True, exist_ok=True)
    log(json.dumps({"mode": args.mode, "dates": trade_dates, "periods": periods, "universe": len(universe), "codes": len(explicit_codes), "run_dir": str(run_dir), "issue_file": args.issue_file}, ensure_ascii=False))

    summaries: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []
    repair_results: list[dict[str, Any]] = []

    if args.issue_file and args.mode == "repair":
        issue_df = pd.read_csv(args.issue_file, dtype=str).fillna("")
        issue_df = issue_df[issue_df["trade_date"].astype(str).isin(set(trade_dates))]
        issue_df = issue_df[issue_df["period"].astype(str).isin(set(periods))]
        all_issues = issue_df.to_dict("records")
        for trade_date in trade_dates:
            date_issues = [item for item in all_issues if str(item.get("trade_date")) == trade_date]
            summaries.append(
                {
                    "trade_date": trade_date,
                    "periods": periods,
                    "source": "issue_file",
                    "issue_codes": len({str(item.get("code")) for item in date_issues if item.get("code")}),
                    "issue_rows": len(date_issues),
                    "reason_counts": dict(Counter(str(item.get("reason") or "") for item in date_issues)),
                }
            )
            repair_results.append(run_repair(args, run_dir, trade_date, date_issues))
    else:
        for trade_date in trade_dates:
            date_issues_by_code: dict[str, dict[str, Any]] = {}
            for period in periods:
                summary, issues = audit_date_period(client, universe, trade_date, period, chunk_size=args.audit_code_chunk_size)
                summaries.append(summary)
                all_issues.extend(issues)
                for item in issues:
                    date_issues_by_code.setdefault(str(item["code"]), item)
                log("audit_summary " + json.dumps(summary, ensure_ascii=False, default=json_default))
            if args.mode == "repair":
                repair_results.append(run_repair(args, run_dir, trade_date, list(date_issues_by_code.values())))

    final_validation: dict[str, Any] | None = None
    final_issues: list[dict[str, Any]] = []
    if args.mode == "repair":
        final_days: list[dict[str, Any]] = []
        for trade_date in trade_dates:
            period_results: list[dict[str, Any]] = []
            for period in periods:
                summary, issues = audit_date_period(client, universe, trade_date, period, chunk_size=args.audit_code_chunk_size)
                final_issues.extend(issues)
                period_results.append(
                    {
                        "period": period,
                        "expected_codes": int(summary["expected_codes"]),
                        "complete_codes": int(summary["complete_codes"]),
                        "issue_codes": int(summary["issue_codes"]),
                        "reason_counts": summary["reason_counts"],
                    }
                )
            remaining_issues = sum(item["issue_codes"] for item in period_results)
            final_days.append(
                {
                    "trade_date": trade_date,
                    "closed": remaining_issues == 0,
                    "remaining_issue_codes": remaining_issues,
                    "periods": period_results,
                }
            )
        final_validation = {
            "closed": all(day["closed"] for day in final_days),
            "trade_dates": trade_dates,
            "days": final_days,
        }
        (run_dir / "final_validation.json").write_text(
            json.dumps(final_validation, ensure_ascii=False, indent=2, default=json_default),
            encoding="utf-8",
        )

    pd.DataFrame(summaries).to_csv(run_dir / "summary.csv", index=False, encoding="utf-8-sig")
    # The resumable queue must reflect the post-repair audit.  Reusing the
    # initial audit queue makes every scheduled retry reprocess resolved codes.
    queue_issues = final_issues if args.mode == "repair" else all_issues
    pd.DataFrame(queue_issues).to_csv(run_dir / "issues.csv", index=False, encoding="utf-8-sig")
    result = {
        "ok": all(int(item.get("issue_codes") or 0) == 0 for item in summaries)
        if args.mode == "audit"
        else bool(final_validation and final_validation["closed"]) and all(bool(item.get("ok")) for item in repair_results),
        "mode": args.mode,
        "run_dir": str(run_dir),
        "trade_dates": trade_dates,
        "periods": periods,
        "summary": summaries,
        "initial_issue_count": len(all_issues),
        "initial_unique_issue_codes": len({str(item.get("code")) for item in all_issues if item.get("code")}),
        "issue_count": len(queue_issues),
        "issue_sample": queue_issues[:100],
        "repair_results": repair_results,
        "final_validation": final_validation,
    }
    (run_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    log("done " + json.dumps({"ok": result["ok"], "initial_issue_count": len(all_issues), "issue_count": len(queue_issues), "run_dir": str(run_dir)}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
