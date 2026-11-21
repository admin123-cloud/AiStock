# -*- coding: utf-8 -*-
"""AR3-F 历史财报、业绩快报与业绩预告独立表同步脚本（严格 Point-in-Time 公告日对齐）。

安全与反幻觉铁律：
1. 仅操作独立新表：
   - stock.stock_financial_disclosure（正式季报/年报 + 业绩快报）
   - stock.stock_earnings_forecast（归母净利润业绩预告）
   绝不触碰或修改 stock.kline_daily。
2. 严禁使用 report_date 作为可见时间：每条记录必须保留真实公告披露日 notice_date。
   - 正式季报/快报过滤掉 notice_date 为空或 notice_date <= report_date 的异常脏数据；
   - 业绩预告允许在报告期末前/后公告，但回测只允许在 trade_date >= notice_date 之后才可见。
3. 幂等重跑（ReplacingMergeTree），支持 2022Q1 ~ 2026Q4 全量历史回补与增量更新。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

BJ_TZ = timezone(timedelta(hours=8))

REPORT_DATES_OFFICIAL = [
    "2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31",
    "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
    "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
    "2026-03-31", "2026-06-30", "2026-09-30",
]

REPORT_DATES_FORECAST = [
    "2022-09-30", "2022-12-31",
    "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
    "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
    "2026-03-31", "2026-06-30", "2026-09-30", "2026-12-31",
]


def _ch_exec(sql: str, body: bytes | None = None, timeout: int = 60) -> str:
    host = (os.environ.get("AISTOCK_CLICKHOUSE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("AISTOCK_CLICKHOUSE_PORT") or "8123").strip()
    if body is None:
        req = urllib.request.Request(f"http://{host}:{port}/", data=sql.encode("utf-8"), method="POST")
    else:
        q = urllib.parse.urlencode({"query": sql})
        req = urllib.request.Request(f"http://{host}:{port}/?{q}", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8").strip()


def _to_float_or_none(v: Any) -> float | None:
    if v is None or v == "" or v == "-":
        return None
    try:
        x = float(v)
        if x != x:  # NaN
            return None
        return x
    except Exception:
        return None


def _clean_date(v: Any) -> str | None:
    if not v:
        return None
    s = str(v).strip()[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    return None


def _normalize_code(secucode: Any, raw_code: Any) -> str | None:
    s = str(secucode or "").strip().upper()
    if len(s) == 9 and s[6] == "." and s[7:] in ("SH", "SZ", "BJ"):
        return s
    c = str(raw_code or "").strip()
    if len(c) == 6 and c.isdigit():
        if c.startswith(("6", "9")):
            return f"{c}.SH"
        if c.startswith(("0", "3", "2")):
            return f"{c}.SZ"
        if c.startswith(("4", "8")):
            return f"{c}.BJ"
    return None


def _quarter_num(report_date: str) -> int:
    mmdd = report_date[5:10]
    return {"03-31": 1, "06-30": 2, "09-30": 3, "12-31": 4}.get(mmdd, 0)


def _fetch_em_page(report_name: str, columns: str, filter_str: str, page: int, page_size: int = 500) -> dict[str, Any]:
    params = urllib.parse.urlencode({
        "reportName": report_name,
        "columns": columns,
        "pageNumber": page,
        "pageSize": page_size,
        "sortColumns": "SECURITY_CODE",
        "sortTypes": "1",
        "filter": filter_str,
    })
    url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?{params}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    return {}


def _fetch_all_pages(report_name: str, columns: str, filter_str: str, page_size: int = 500) -> list[dict[str, Any]]:
    first = _fetch_em_page(report_name, columns, filter_str, 1, page_size)
    res = first.get("result") or {}
    rows = list(res.get("data") or [])
    pages = int(res.get("pages") or 1)
    if pages <= 1:
        return rows

    def _one(p: int) -> list[dict[str, Any]]:
        d = _fetch_em_page(report_name, columns, filter_str, p, page_size)
        return list((d.get("result") or {}).get("data") or [])

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for batch in ex.map(_one, range(2, pages + 1)):
            rows.extend(batch)
    return rows


def ensure_tables() -> None:
    ddl_fin = """
    CREATE TABLE IF NOT EXISTS stock.stock_financial_disclosure (
        code String,
        name String,
        report_date Date,
        notice_date Date,
        source_type LowCardinality(String),
        report_quarter UInt8,
        revenue Nullable(Float64),
        revenue_yoy_pct Nullable(Float64),
        net_profit Nullable(Float64),
        net_profit_yoy_pct Nullable(Float64),
        revenue_qoq_pct Nullable(Float64),
        net_profit_qoq_pct Nullable(Float64),
        roe_pct Nullable(Float64),
        gross_margin_pct Nullable(Float64),
        basic_eps Nullable(Float64),
        deduct_eps Nullable(Float64),
        updated_at DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (code, report_date, notice_date, source_type)
    """
    ddl_fc = """
    CREATE TABLE IF NOT EXISTS stock.stock_earnings_forecast (
        code String,
        name String,
        report_date Date,
        notice_date Date,
        predict_type LowCardinality(String),
        profit_lower Nullable(Float64),
        profit_upper Nullable(Float64),
        yoy_lower_pct Nullable(Float64),
        yoy_upper_pct Nullable(Float64),
        yoy_mid_pct Nullable(Float64),
        preyear_profit Nullable(Float64),
        updated_at DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (code, report_date, notice_date)
    """
    ddl_div = """
    CREATE TABLE IF NOT EXISTS stock.stock_dividend_events (
        code String,
        ex_dividend_date Date,
        bonus_per_10 Float64,
        cash_per_10 Float64,
        report_date Date,
        updated_at DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (code, ex_dividend_date, report_date)
    """
    _ch_exec(ddl_fin)
    _ch_exec(ddl_fc)
    _ch_exec(ddl_div)


def _insert_jsonl(table: str, rows: list[dict[str, Any]], chunk_size: int = 10000) -> int:
    total = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in chunk).encode("utf-8")
        _ch_exec(f"INSERT INTO {table} FORMAT JSONEachRow", payload, timeout=120)
        total += len(chunk)
    return total


def sync_official_reports(now_str: str) -> dict[str, Any]:
    cols = (
        "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,SECURITY_TYPE_CODE,REPORTDATE,NOTICE_DATE,"
        "TOTAL_OPERATE_INCOME,YSTZ,PARENT_NETPROFIT,SJLTZ,YSHZ,SJLHZ,"
        "WEIGHTAVG_ROE,XSMLL,BASIC_EPS,DEDUCT_BASIC_EPS"
    )
    out: list[dict[str, Any]] = []
    skipped_bad_date = 0
    for rdate in REPORT_DATES_OFFICIAL:
        flt = f'(REPORTDATE=\'{rdate}\')(SECURITY_TYPE_CODE in ("058001001","058001008"))'
        raw = _fetch_all_pages("RPT_LICO_FN_CPD", cols, flt, page_size=500)
        cnt = 0
        for row in raw:
            code = _normalize_code(row.get("SECUCODE"), row.get("SECURITY_CODE"))
            rd = _clean_date(row.get("REPORTDATE"))
            nd = _clean_date(row.get("NOTICE_DATE"))
            if not code or not rd or not nd:
                skipped_bad_date += 1
                continue
            # 严格零未来函数：正式季报公告日必须严格晚于报告期末
            if nd <= rd:
                skipped_bad_date += 1
                continue
            out.append({
                "code": code,
                "name": str(row.get("SECURITY_NAME_ABBR") or "").strip(),
                "report_date": rd,
                "notice_date": nd,
                "source_type": "official",
                "report_quarter": _quarter_num(rd),
                "revenue": _to_float_or_none(row.get("TOTAL_OPERATE_INCOME")),
                "revenue_yoy_pct": _to_float_or_none(row.get("YSTZ")),
                "net_profit": _to_float_or_none(row.get("PARENT_NETPROFIT")),
                "net_profit_yoy_pct": _to_float_or_none(row.get("SJLTZ")),
                "revenue_qoq_pct": _to_float_or_none(row.get("YSHZ")),
                "net_profit_qoq_pct": _to_float_or_none(row.get("SJLHZ")),
                "roe_pct": _to_float_or_none(row.get("WEIGHTAVG_ROE")),
                "gross_margin_pct": _to_float_or_none(row.get("XSMLL")),
                "basic_eps": _to_float_or_none(row.get("BASIC_EPS")),
                "deduct_eps": _to_float_or_none(row.get("DEDUCT_BASIC_EPS")),
                "updated_at": now_str,
            })
            cnt += 1
        print(f"  [official] {rdate}: {cnt} rows", flush=True)
    inserted = _insert_jsonl("stock.stock_financial_disclosure", out)
    return {"inserted": inserted, "skipped_bad_date": skipped_bad_date}


def sync_express_reports(now_str: str) -> dict[str, Any]:
    cols = (
        "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,SECURITY_TYPE_CODE,REPORT_DATE,NOTICE_DATE,"
        "TOTAL_OPERATE_INCOME,YSTZ,PARENT_NETPROFIT,JLRTBZCL,DJDYSHZ,DJDJLHZ,WEIGHTAVG_ROE,BASIC_EPS"
    )
    out: list[dict[str, Any]] = []
    skipped_bad_date = 0
    for rdate in REPORT_DATES_OFFICIAL:
        flt = f'(REPORT_DATE=\'{rdate}\')(SECURITY_TYPE_CODE in ("058001001","058001008"))'
        raw = _fetch_all_pages("RPT_FCI_PERFORMANCEE", cols, flt, page_size=500)
        cnt = 0
        for row in raw:
            code = _normalize_code(row.get("SECUCODE"), row.get("SECURITY_CODE"))
            rd = _clean_date(row.get("REPORT_DATE"))
            nd = _clean_date(row.get("NOTICE_DATE"))
            if not code or not rd or not nd or nd <= rd:
                skipped_bad_date += 1
                continue
            out.append({
                "code": code,
                "name": str(row.get("SECURITY_NAME_ABBR") or "").strip(),
                "report_date": rd,
                "notice_date": nd,
                "source_type": "express",
                "report_quarter": _quarter_num(rd),
                "revenue": _to_float_or_none(row.get("TOTAL_OPERATE_INCOME")),
                "revenue_yoy_pct": _to_float_or_none(row.get("YSTZ")),
                "net_profit": _to_float_or_none(row.get("PARENT_NETPROFIT")),
                "net_profit_yoy_pct": _to_float_or_none(row.get("JLRTBZCL")),
                "revenue_qoq_pct": _to_float_or_none(row.get("DJDYSHZ")),
                "net_profit_qoq_pct": _to_float_or_none(row.get("DJDJLHZ")),
                "roe_pct": _to_float_or_none(row.get("WEIGHTAVG_ROE")),
                "gross_margin_pct": None,
                "basic_eps": _to_float_or_none(row.get("BASIC_EPS")),
                "deduct_eps": None,
                "updated_at": now_str,
            })
            cnt += 1
        if cnt:
            print(f"  [express]  {rdate}: {cnt} rows", flush=True)
    inserted = _insert_jsonl("stock.stock_financial_disclosure", out)
    return {"inserted": inserted, "skipped_bad_date": skipped_bad_date}


def sync_forecasts(now_str: str) -> dict[str, Any]:
    cols = (
        "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,SECURITY_TYPE_CODE,REPORT_DATE,NOTICE_DATE,"
        "PREDICT_FINANCE_CODE,PREDICT_TYPE,PREDICT_AMT_LOWER,PREDICT_AMT_UPPER,"
        "ADD_AMP_LOWER,ADD_AMP_UPPER,PREYEAR_SAME_PERIOD"
    )
    out: list[dict[str, Any]] = []
    skipped = 0
    for rdate in REPORT_DATES_FORECAST:
        flt = f'(REPORT_DATE=\'{rdate}\')(PREDICT_FINANCE_CODE="004")(SECURITY_TYPE_CODE in ("058001001","058001008"))'
        raw = _fetch_all_pages("RPT_PUBLIC_OP_NEWPREDICT", cols, flt, page_size=500)
        cnt = 0
        for row in raw:
            code = _normalize_code(row.get("SECUCODE"), row.get("SECURITY_CODE"))
            rd = _clean_date(row.get("REPORT_DATE"))
            nd = _clean_date(row.get("NOTICE_DATE"))
            if not code or not rd or not nd:
                skipped += 1
                continue
            lo = _to_float_or_none(row.get("ADD_AMP_LOWER"))
            hi = _to_float_or_none(row.get("ADD_AMP_UPPER"))
            mid = None
            if lo is not None and hi is not None:
                mid = (lo + hi) / 2.0
            elif lo is not None:
                mid = lo
            elif hi is not None:
                mid = hi
            out.append({
                "code": code,
                "name": str(row.get("SECURITY_NAME_ABBR") or "").strip(),
                "report_date": rd,
                "notice_date": nd,
                "predict_type": str(row.get("PREDICT_TYPE") or "").strip(),
                "profit_lower": _to_float_or_none(row.get("PREDICT_AMT_LOWER")),
                "profit_upper": _to_float_or_none(row.get("PREDICT_AMT_UPPER")),
                "yoy_lower_pct": lo,
                "yoy_upper_pct": hi,
                "yoy_mid_pct": mid,
                "preyear_profit": _to_float_or_none(row.get("PREYEAR_SAME_PERIOD")),
                "updated_at": now_str,
            })
            cnt += 1
        if cnt:
            print(f"  [forecast] {rdate}: {cnt} rows", flush=True)
    inserted = _insert_jsonl("stock.stock_earnings_forecast", out)
    return {"inserted": inserted, "skipped": skipped}


def sync_dividends(now_str: str) -> dict[str, Any]:
    cols = "SECUCODE,SECURITY_CODE,EX_DIVIDEND_DATE,BONUS_IT_RATIO,PRETAX_BONUS_RMB,REPORT_DATE,ASSIGN_PROGRESS"
    out: list[dict[str, Any]] = []
    skipped = 0
    for yr in ("2022", "2023", "2024", "2025", "2026"):
        flt = f'(EX_DIVIDEND_DATE>=\'{yr}-01-01\')(EX_DIVIDEND_DATE<=\'{yr}-12-31\')(ASSIGN_PROGRESS="实施分配")'
        raw = _fetch_all_pages("RPT_SHAREBONUS_DET", cols, flt, page_size=500)
        cnt = 0
        for row in raw:
            code = _normalize_code(row.get("SECUCODE"), row.get("SECURITY_CODE"))
            exd = _clean_date(row.get("EX_DIVIDEND_DATE"))
            rd = _clean_date(row.get("REPORT_DATE")) or exd
            if not code or not exd or not rd:
                skipped += 1
                continue
            b10 = _to_float_or_none(row.get("BONUS_IT_RATIO")) or 0.0
            c10 = _to_float_or_none(row.get("PRETAX_BONUS_RMB")) or 0.0
            if b10 == 0.0 and c10 == 0.0:
                continue
            out.append({
                "code": code,
                "ex_dividend_date": exd,
                "bonus_per_10": b10,
                "cash_per_10": c10,
                "report_date": rd,
                "updated_at": now_str,
            })
            cnt += 1
        print(f"  [dividend] {yr}: {cnt} rows", flush=True)
    inserted = _insert_jsonl("stock.stock_dividend_events", out)
    return {"inserted": inserted, "skipped": skipped}


def main() -> None:
    import sys

    t0 = time.time()
    ensure_tables()
    now_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    div_only = "--dividends-only" in sys.argv
    if not div_only:
        print("[1/4] 同步正式季报/半年报/年报 (RPT_LICO_FN_CPD)...", flush=True)
        r_off = sync_official_reports(now_str)
        print("[2/4] 同步业绩快报 (RPT_FCI_PERFORMANCEE)...", flush=True)
        r_exp = sync_express_reports(now_str)
        print("[3/4] 同步业绩预告 (RPT_PUBLIC_OP_NEWPREDICT, 归母净利润)...", flush=True)
        r_fc = sync_forecasts(now_str)
    else:
        r_off = r_exp = r_fc = {"skipped": "dividends-only"}

    print("[4/4] 同步除权除息分红送转 (RPT_SHAREBONUS_DET)...", flush=True)
    r_div = sync_dividends(now_str)

    if not div_only:
        _ch_exec("OPTIMIZE TABLE stock.stock_financial_disclosure FINAL")
        _ch_exec("OPTIMIZE TABLE stock.stock_earnings_forecast FINAL")
    _ch_exec("OPTIMIZE TABLE stock.stock_dividend_events FINAL")

    div_stat = _ch_exec(
        "SELECT count(), uniqExact(code), min(ex_dividend_date), max(ex_dividend_date), countIf(bonus_per_10 > 0) "
        "FROM stock.stock_dividend_events FINAL"
    )
    print(f"\n同步完成 ({time.time() - t0:.1f}s): official={r_off} express={r_exp} forecast={r_fc} dividend={r_div}")
    print("ClickHouse 校验 [stock.stock_dividend_events]:")
    print(div_stat)


if __name__ == "__main__":
    main()
