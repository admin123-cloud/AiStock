from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from clickhouse_connect import get_client


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
from utils.kline_units import normalize_qmt_daily_units
from utils.paths import report_path
from utils.qmt_universe import qmt_universe_filter_sql


DEFAULT_STAGE_TABLE = "kline_daily_qmtmini_stage"
SOURCE_ABSENCE_TABLE = "kline_daily_qmt_source_absence_audit"
MARKET_STATUS_TABLE = "kline_daily_market_status_audit"
SECURITY_TYPE_AUDIT_TABLE = "security_master_type_evidence_audit"

# These are issuer/exchange-announcement-backed intervals.  They are not QMT
# guesses: their purpose is to stop a known suspension or post-delisting day
# being mistaken for an upstream data outage.  New events are appended here
# only with a durable announcement URL and are synced idempotently by the
# nightly coverage job.
VERIFIED_MARKET_STATUS_EVENTS = (
    ("002036.SZ", "2026-07-23", "2026-07-29", "suspended", "https://paper.cnstock.com/html/2026-07/25/content_2248624.htm", "issuer-announced control-change suspension"),
    ("920305.BJ", "2026-02-05", "2026-02-10", "suspended", "https://file.finance.sina.com.cn/211.154.219.97%3A9494/MRGG/CNSEBJ_STOCK/2026/2026-2/2026-02-04/11952275.PDF", "停牌核查"),
    ("920305.BJ", "2026-04-30", "2026-07-08", "suspended", "https://file.finance.sina.com.cn/211.154.219.97%3A9494/MRGG/CNSEBJ_STOCK/2026/2026-5/2026-05-14/12321470.PDF", "退市风险警示停牌"),
    ("002731.SZ", "2026-05-06", "2026-07-06", "suspended", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12431721&stockid=002731", "未按期披露年报停牌"),
    ("002898.SZ", "2026-05-06", "2026-06-25", "suspended", "https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-30/c13f8e8f-38a2-435b-bf8d-acd091bc191e.PDF", "终止上市决定前停牌"),
    ("002898.SZ", "2026-07-17", "2099-12-31", "delisted", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12450278&stockid=002898", "摘牌终止上市"),
    ("688121.SH", "2026-05-06", "2026-07-06", "suspended", "https://big5.sse.com.cn/site/cht/www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2026-06-13/688121_20260613_89RJ.pdf", "未按期披露年报停牌"),
    ("600735.SH", "2026-02-26", "2026-04-24", "suspended", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12194279&stockid=600735", "资金占用整改停牌"),
    ("001331.SZ", "2026-01-05", "2026-01-05", "suspended", "https://static.cninfo.com.cn/finalpage/2026-01-06/1224920853.PDF", "停牌核查"),
    ("001331.SZ", "2026-05-28", "2026-06-26", "suspended", "https://paper.cnstock.com/html/2026-05/28/content_2222993.htm", "要约收购结果确认停牌"),
)
# Fact-checked BSE suspensions, cross-confirmed with an independent daily
# history provider.  Keep these append-only and include a durable notice URL.
VERIFIED_MARKET_STATUS_EVENTS += (
    ("920058.BJ", "2026-05-13", "2026-05-26", "suspended", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllMemordDetail.php?stockid=920058", "issuer-announced asset-restructuring suspension"),
    ("920675.BJ", "2026-06-08", "2026-06-22", "suspended", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12405499&stockid=920675", "issuer-announced restructuring suspension"),
    ("920685.BJ", "2026-07-16", "2026-07-27", "suspended", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12447634&stockid=920685", "issuer-announced asset-purchase suspension"),
    ("920179.BJ", "2026-01-30", "2026-02-05", "suspended", "https://static.cninfo.com.cn/finalpage/2026-01-29/1224956935.PDF", "issuer-announced acquisition suspension"),
    ("920641.BJ", "2026-01-05", "2026-01-08", "suspended", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11906576&stockid=920641", "control-change suspension before resumption"),
    ("920023.BJ", "2026-04-29", "2026-04-29", "suspended", "https://wap.stockstar.com/detail/IG2026042800063184", "one-day risk-warning suspension"),
    ("920090.BJ", "2026-04-23", "2026-04-23", "suspended", "https://www.xyzq.com.cn/xysec/info/article/f89a05229a99444d915c10c1eb44c340", "one-day risk-warning suspension"),
    ("920575.BJ", "2026-04-30", "2026-04-30", "suspended", "https://static.cninfo.com.cn/finalpage/2026-04-29/1225267197.PDF", "risk-warning suspension"),
    ("920305.BJ", "2026-02-11", "2026-02-11", "suspended", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11963154&stockid=920305", "suspension verification completed after close"),
)
VERIFIED_MARKET_STATUS_EVENTS += (
    ("920174.BJ", "2024-11-26", "2024-12-09", "suspended", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11591740&stockid=920174", "issuer-announced asset-restructuring suspension"),
    ("920856.BJ", "2023-10-17", "2023-10-27", "suspended", "https://m.nbd.com.cn/articles/2023-10-17/3057565.html", "issuer-announced asset-restructuring suspension"),
    ("920808.BJ", "2025-05-26", "2025-06-09", "suspended", "https://data.eastmoney.com/stockcalendar/920808.html", "control-change suspension"),
    ("920753.BJ", "2023-07-17", "2023-07-25", "suspended", "https://static.cninfo.com.cn/finalpage/2023-07-25/1217384622.PDF", "issuer-announced asset-acquisition suspension"),
    ("920090.BJ", "2023-08-02", "2023-08-08", "suspended", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?CompanyCode=80177332&gather=1&id=9383138", "issuer-announced control-change suspension"),
    ("920198.BJ", "2023-12-04", "2023-12-08", "suspended", "https://file.finance.sina.com.cn/211.154.219.97%3A9494/MRGG/CNSEBJ_STOCK/2023/2023-12/2023-12-08/9692807.PDF", "issuer-announced control-change suspension"),
    ("920171.BJ", "2023-11-28", "2023-11-30", "suspended", "https://data.eastmoney.com/stockcalendar/920171.html", "exchange-recorded abnormal-volatility suspension"),
    ("920526.BJ", "2023-11-28", "2023-11-30", "suspended", "https://file.finance.sina.com.cn/211.154.219.97%3A9494/MRGG/CNSEBJ_STOCK/2023/2023-11/2023-11-30/9674039.PDF", "issuer-announced abnormal-volatility suspension"),
    ("920964.BJ", "2024-10-22", "2024-10-25", "suspended", "https://file.finance.sina.com.cn/211.154.219.97%3A9494/MRGG/CNSEBJ_STOCK/2024/2024-10/2024-10-21/10533790.PDF", "issuer-announced control-change suspension"),
    ("920961.BJ", "2025-09-16", "2025-09-22", "suspended", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11939745&stockid=920961", "issuer-announced asset-restructuring suspension"),
    ("920641.BJ", "2025-12-31", "2025-12-31", "suspended", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11890731&stockid=920641", "issuer-announced control-change suspension"),
    ("920305.BJ", "2025-07-07", "2025-07-11", "suspended", "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11235005&stockid=835305", "exchange-rule abnormal-volatility suspension"),
    ("920239.BJ", "2022-01-24", "2022-02-09", "suspended", "https://qxb-pdf-osscache.qixin.com/AnBaseinfo/18ad6f6a836d20e6ae61dfb24476b5a9.pdf", "issuer-announced asset-restructuring suspension"),
    ("920263.BJ", "2021-12-21", "2022-01-04", "suspended", "https://q.stock.sohu.com/cn/836263/bw_1.shtml", "issuer-announced asset-purchase suspension"),
    ("920415.BJ", "2021-12-24", "2022-01-07", "suspended", "https://static.cninfo.com.cn/finalpage/2022-01-07/1212120142.PDF", "issuer-announced asset-restructuring suspension"),
    ("920415.BJ", "2022-07-25", "2022-07-29", "suspended", "https://pdf.dfcfw.com/pdf/H2_AN202207221576465022_1.pdf", "issuer-announced asset-restructuring suspension"),
)
# Historical SH/SZ one-day suspensions found while reconciling the 2020-onward
# coverage backlog.  These are issuer/exchange disclosures, not inferred from
# a missing provider row, so they remain safe to apply automatically overnight.
VERIFIED_MARKET_STATUS_EVENTS += (
    ("002309.SZ", "2022-05-30", "2022-05-30", "suspended", "https://disc.static.szse.cn/download/disc/disk03/finalpage/2022-05-30/2ebe64d4-9784-4644-955d-3455e1333986.PDF", "other-risk-warning suspension"),
    ("002564.SZ", "2022-01-24", "2022-02-11", "suspended", "https://static.cninfo.com.cn/finalpage/2022-01-24/1212258848.PDF", "issuer-announced asset-restructuring suspension"),
    ("002656.SZ", "2020-01-10", "2020-01-10", "suspended", "https://pdf.dfcfw.com/pdf/H2_AN202001091373794217_1.PDF", "other-risk-warning suspension"),
    ("600289.SH", "2020-02-27", "2020-02-27", "suspended", "https://pdf.dfcfw.com/pdf/H2_AN202002271375585905_1.pdf", "abnormal-volatility verification suspension"),
    ("600289.SH", "2021-05-19", "2021-05-19", "suspended", "https://finance.sina.com.cn/stock/hkstock/ggscyd/2021-05-18/doc-ikmyaawc6073949.shtml", "risk-warning removal suspension"),
    ("600289.SH", "2021-12-27", "2021-12-27", "suspended", "https://m.gelonghui.com/news/656816", "tender-offer result confirmation suspension"),
    ("600530.SH", "2020-04-30", "2020-04-30", "suspended", "https://epaper.cs.com.cn/zgzqb/images/2020-04/30/B059/ZQBXP0590430C.pdf", "ticker-change suspension"),
    ("600530.SH", "2021-05-14", "2021-05-14", "suspended", "https://m.sohu.com/a/466816630_114984/", "important-announcement suspension"),
    ("603389.SH", "2021-05-12", "2021-05-12", "suspended", "https://epaper.cs.com.cn/zgzqb/images/2021-05/12/B035/zqB03512.pdf", "risk-warning removal suspension"),
    ("600530.SH", "2020-04-29", "2020-04-29", "suspended", "https://www.stcn.com/company/gsxw/202004/t20200429_1734566.html", "risk-warning implementation suspension"),
    ("603389.SH", "2020-04-28", "2020-04-28", "suspended", "https://pdf.dfcfw.com/pdf/H2_AN202003201376733703_1.pdf", "risk-warning implementation suspension"),
)
# Securities which were included in the QMT stock master with type=stock but
# are demonstrably BSE convertible bonds.  They must not generate daily-stock
# coverage obligations or enter stock selection.  Keep the issuer notice URL
# beside each correction so the nightly reconciliation remains fact-based.
VERIFIED_NON_STOCK_SECURITIES = (
    ("810011.BJ", "bond", "https://tongbicapital.com/article/detail?id=1173366", "优机股份可转换债券（优机定转）"),
    ("810013.BJ", "bond", "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?CompanyCode=80477737&gather=1&id=12387830", "万通发展可转换债券（万通定转）"),
)
FIELD_LIST = ["open", "high", "low", "close", "volume", "amount"]
# The daily table is a closed-bar dataset. Do not persist QMT's live daily
# snapshot before the Shanghai close; intraday bars are maintained separately.
QMT_DAILY_CLOSE_TIME = (15, 5)


def abnormal_daily_data_policy() -> dict[str, object]:
    """Return the durable policy shared by after-close and overnight repairs.

    A source-empty day is evidence of an acquisition problem, not evidence of
    a market suspension.  The ordering below is intentionally conservative so
    an automated repair never converts an unverified business event into data.
    """
    return {
        "version": "daily_abnormal_data_v1",
        "classification_order": [
            "verified_suspension_or_delisting",
            "verified_non_stock_security",
            "persisted_daily_bar",
            "qmt_bounded_retry",
            "cross_source_ohlc_amount_validation",
            "provider_conflict_or_no_bar_pending_confirmation",
        ],
        "write_rule": "persist only a cross-source bar with >=99% OHLC and >=98% amount overlap agreement",
        "status_rule": "exclude only issuer/exchange-announcement-backed suspension or delisting intervals",
        "conflict_rule": "provider disagreement, a shared no-bar result, or a failed second source remains audited and is never auto-written or auto-exempted",
        "recovery_windows": ["after_close", "overnight"],
    }
TARGET_COLUMNS = [
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "change_pct",
    "change_amount",
    "turnover_rate",
    "created_at",
]
QMT_DAILY_CODE_ALIASES = {
    # Strategy code kept by AiStock/TDX history; QMT Mini exposes SSE Composite as 000001.SH.
    "999999.SH": "000001.SH",
}


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def quote_sql(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def qmt_source_code(code: str) -> str:
    return QMT_DAILY_CODE_ALIASES.get(str(code).upper(), str(code).upper())


def ch_client():
    return get_client(
        host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
        password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
    )


def ensure_stage_table(client, stage_table: str) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {stage_table}
        (
            code String,
            trade_date Date,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume Float64,
            amount Float64,
            amplitude Float64,
            change_pct Float64,
            change_amount Float64,
            turnover_rate Float64,
            created_at Nullable(DateTime),
            id UInt64 DEFAULT 0
        )
        ENGINE = ReplacingMergeTree
        ORDER BY (code, trade_date)
        """
    )


def ensure_source_absence_table(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {SOURCE_ABSENCE_TABLE}
        (
            code String, trade_date Date, source String, classification String,
            observed_at DateTime
        ) ENGINE = ReplacingMergeTree(observed_at)
        ORDER BY (code, trade_date, source)
        """
    )


def ensure_market_status_table(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {MARKET_STATUS_TABLE}
        (
            code String, start_date Date, end_date Date, status String,
            evidence_url String, reason String, observed_at DateTime
        ) ENGINE = ReplacingMergeTree(observed_at)
        ORDER BY (code, start_date, end_date, status)
        """
    )


def ensure_security_type_audit_table(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {SECURITY_TYPE_AUDIT_TABLE}
        (
            code String, security_type String, evidence_url String,
            reason String, observed_at DateTime
        ) ENGINE = ReplacingMergeTree(observed_at)
        ORDER BY (code, security_type)
        """
    )


def sync_verified_market_status_events(client) -> int:
    """Persist the reviewed suspension/delisting intervals without deleting raw audits."""
    ensure_market_status_table(client)
    now = datetime.now().replace(microsecond=0)
    client.insert(
        MARKET_STATUS_TABLE,
        [
            (code, date.fromisoformat(start_date), date.fromisoformat(end_date), status, url, reason, now)
            for code, start_date, end_date, status, url, reason in VERIFIED_MARKET_STATUS_EVENTS
        ],
        column_names=["code", "start_date", "end_date", "status", "evidence_url", "reason", "observed_at"],
    )
    return len(VERIFIED_MARKET_STATUS_EVENTS)


def reconcile_verified_delist_metadata(client) -> int:
    """Apply only announcement-backed delistings to the QMT-canonical stock master."""
    count = 0
    for code, start_date, _end_date, status, _url, _reason in VERIFIED_MARKET_STATUS_EVENTS:
        if status != "delisted":
            continue
        client.command(
            "ALTER TABLE stocks UPDATE "
            f"quit = 1, delist_date = toDate({quote_sql(start_date)}) "
            f"WHERE code = {quote_sql(code)} AND (quit = 0 OR delist_date IS NULL OR delist_date > toDate({quote_sql(start_date)}))"
        )
        count += 1
    return count


def reconcile_verified_non_stock_metadata(client) -> int:
    """Correct reviewed non-stock instruments in the QMT-canonical master."""
    ensure_security_type_audit_table(client)
    now = datetime.now().replace(microsecond=0)
    client.insert(
        SECURITY_TYPE_AUDIT_TABLE,
        [(code, security_type, url, reason, now) for code, security_type, url, reason in VERIFIED_NON_STOCK_SECURITIES],
        column_names=["code", "security_type", "evidence_url", "reason", "observed_at"],
    )
    count = 0
    for code, security_type, _url, _reason in VERIFIED_NON_STOCK_SECURITIES:
        client.command(
            "ALTER TABLE stocks UPDATE "
            f"type = {quote_sql(security_type)} "
            f"WHERE code = {quote_sql(code)} AND type != {quote_sql(security_type)} "
            "SETTINGS mutations_sync = 1"
        )
        count += 1
    return count


def known_market_status_keys(
    expected: set[tuple[str, Any]],
    status_rows: list[tuple[Any, ...]],
) -> set[tuple[str, Any]]:
    """Return expected code-days covered by an announcement-backed status interval."""
    result: set[tuple[str, Any]] = set()
    for code, start_date, end_date, *_ in status_rows:
        normalized = str(code).upper()
        result.update(
            (candidate_code, trade_date)
            for candidate_code, trade_date in expected
            if candidate_code == normalized and start_date <= trade_date <= end_date
        )
    return result


def remediation_plan(
    expected: set[tuple[str, Any]],
    actual: set[tuple[str, Any]],
    source_absent: set[tuple[str, Any]],
    market_status_excluded: set[tuple[str, Any]],
    candidate_limit: int,
) -> dict[str, Any]:
    """Classify every expected daily key into a reusable, source-aware repair plan.

    The order is deliberate: announcement-backed status wins; then persisted
    bars; then a QMT-empty key that needs a bounded QMT retry and, only when
    an explicit TDX gateway is available, a canonical-code fallback probe.
    """
    effective = expected - market_status_excluded
    qmt_empty = (effective - actual) & source_absent
    missing = effective - actual - source_absent

    def by_code(keys: set[tuple[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[Any]] = {}
        for code, trade_date in keys:
            grouped.setdefault(code, []).append(trade_date)
        items = [
            {
                "code": code,
                "missing_days": len(days),
                "first_missing": str(min(days)),
                "last_missing": str(max(days)),
            }
            for code, days in grouped.items()
        ]
        items.sort(key=lambda item: (-int(item["missing_days"]), str(item["code"])))
        return items if candidate_limit <= 0 else items[:candidate_limit]

    return {
        "missing_keys": missing,
        "qmt_empty_keys": qmt_empty,
        "market_status_excluded_keys": market_status_excluded,
        "repair_backlog_code_dates": len(missing) + len(qmt_empty),
        "qmt_retry_code_dates": len(qmt_empty),
        "tdx_fallback_code_dates": len(qmt_empty),
        "missing_by_code": by_code(missing),
        "qmt_retry_by_code": by_code(qmt_empty),
    }


def ensure_request_log_table(client) -> None:
    client.command(
        """
        CREATE TABLE IF NOT EXISTS qmt_xtquant_request_log
        (
            request_id String,
            task_name String,
            phase String,
            period String,
            code_count UInt32,
            start_time String,
            end_time String,
            status String,
            attempts UInt8,
            elapsed_sec Float64,
            rows_returned UInt64,
            error String,
            created_at DateTime
        )
        ENGINE = MergeTree
        ORDER BY (created_at, task_name, phase, period)
        """
    )


def insert_request_log(
    client,
    *,
    task_name: str,
    phase: str,
    period: str,
    codes: list[str],
    start_time: str,
    end_time: str,
    status: str,
    attempts: int,
    elapsed_sec: float,
    rows_returned: int,
    error: str = "",
) -> None:
    try:
        ensure_request_log_table(client)
        now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        request_id = f"{task_name}:{phase}:{period}:{now:%Y%m%d%H%M%S%f}:{len(codes)}"
        client.insert(
            "qmt_xtquant_request_log",
            [
                (
                    request_id,
                    task_name,
                    phase,
                    period,
                    len(codes),
                    start_time,
                    end_time,
                    status,
                    int(attempts),
                    float(round(elapsed_sec, 6)),
                    int(rows_returned),
                    str(error or "")[:2000],
                    now,
                )
            ],
            column_names=[
                "request_id",
                "task_name",
                "phase",
                "period",
                "code_count",
                "start_time",
                "end_time",
                "status",
                "attempts",
                "elapsed_sec",
                "rows_returned",
                "error",
                "created_at",
            ],
        )
    except Exception as exc:
        log(f"request log insert skipped: {type(exc).__name__}: {exc}")


def load_codes(client, codes_arg: str, limit: int, include_index: bool, end_date: str = "") -> list[str]:
    if codes_arg.strip():
        codes = [item.strip().upper() for item in codes_arg.split(",") if item.strip()]
    else:
        universe = "stock,index" if include_index else "stock"
        type_filter = qmt_universe_filter_sql(universe)
        list_date_filter = ""
        if end_date.strip():
            list_date_filter = f"AND (type = 'index' OR list_date IS NULL OR list_date <= toDate({quote_sql(end_date)}))"
        rows = client.query(
            f"""
            SELECT code
            FROM stocks
            WHERE (quit = 0 OR quit IS NULL)
              AND ({type_filter})
              {list_date_filter}
            ORDER BY type, code
            """
        ).result_rows
        codes = [str(row[0]).upper() for row in rows]
    return codes[:limit] if limit > 0 else codes


def select_codes_for_args(client, args: argparse.Namespace) -> list[str]:
    codes = load_codes(client, args.codes, 0, args.include_index, args.end_date)
    offset = max(0, int(getattr(args, "code_offset", 0) or 0))
    if offset > 0:
        codes = codes[offset:]
    limit = max(0, int(getattr(args, "limit", 0) or 0))
    return codes[:limit] if limit > 0 else codes


def existing_stage_codes(client, stage_table: str) -> set[str]:
    try:
        rows = client.query(f"SELECT DISTINCT code FROM {stage_table}").result_rows
    except Exception:
        return set()
    return {str(row[0]).upper() for row in rows}


def expected_daily_coverage_keys(
    codes: list[str],
    trade_dates: list[Any],
    metadata: dict[str, dict[str, Any]],
) -> tuple[set[tuple[str, Any]], dict[str, int]]:
    """Build the auditable daily-bar contract from calendar and security life dates.

    A calendar day is expected only while the instrument is listed.  A missing
    listing date is not silently treated as proof of a data gap: it remains an
    eligible date but is counted for metadata review in the report.
    """
    expected: set[tuple[str, Any]] = set()
    excluded_pre_listing = 0
    excluded_post_delisting = 0
    missing_list_date_codes = 0
    for code in codes:
        item = metadata.get(code, {})
        list_date = item.get("list_date")
        delist_date = item.get("delist_date")
        if list_date is None:
            missing_list_date_codes += 1
        for trade_date in trade_dates:
            if list_date is not None and trade_date < list_date:
                excluded_pre_listing += 1
                continue
            if delist_date is not None and trade_date > delist_date:
                excluded_post_delisting += 1
                continue
            expected.add((code, trade_date))
    return expected, {
        "excluded_pre_listing_code_dates": excluded_pre_listing,
        "excluded_post_delisting_code_dates": excluded_post_delisting,
        "metadata_review_codes": missing_list_date_codes,
    }


def coverage_summary(client, args: argparse.Namespace, table: str, candidate_limit: int = 200) -> dict[str, Any]:
    """Validate daily coverage against the SH trading calendar, not row counts.

    A newly-listed stock is only expected to have bars from its listing date.
    This deliberately materializes the requested date range in Python: the
    normal repair window is small (for example H1) and it makes the exact
    missing code/date keys auditable and safe to repair.
    """
    codes = select_codes_for_args(client, args)
    if not codes:
        return {
            "expected_code_dates": 0,
            "actual_code_dates": 0,
            "missing_code_dates": 0,
            "source_absent_code_dates": 0,
            "repair_backlog_code_dates": 0,
            "qmt_retry_code_dates": 0,
            "tdx_fallback_code_dates": 0,
            "complete_codes": 0,
            "incomplete_codes": 0,
            "missing_by_code_sample": [],
            "qmt_retry_by_code_sample": [],
            "missing_by_trade_date_sample": [],
        }
    code_sql = ",".join(quote_sql(code) for code in codes)
    trade_dates = [
        row[0]
        for row in client.query(
            f"""
            SELECT trade_date FROM trade_calendar
            WHERE market = 'SH' AND is_trading = 1
              AND trade_date >= toDate({quote_sql(args.start_date)})
              AND trade_date <= toDate({quote_sql(args.end_date)})
            ORDER BY trade_date
            """
        ).result_rows
    ]
    listing_rows = client.query(
        f"SELECT code, list_date, delist_date FROM stocks WHERE code IN ({code_sql})"
    ).result_rows
    metadata = {
        str(row[0]).upper(): {"list_date": row[1], "delist_date": row[2]}
        for row in listing_rows
    }
    expected, contract_counts = expected_daily_coverage_keys(codes, trade_dates, metadata)
    ensure_market_status_table(client)
    market_status_rows = client.query(
        f"""
        SELECT code, start_date, end_date, status, evidence_url, reason
        FROM {MARKET_STATUS_TABLE} FINAL
        WHERE code IN ({code_sql})
          AND start_date <= toDate({quote_sql(args.end_date)})
          AND end_date >= toDate({quote_sql(args.start_date)})
        """
    ).result_rows
    market_status_excluded = known_market_status_keys(expected, market_status_rows)
    effective_expected = expected - market_status_excluded
    actual_rows = client.query(
        f"""
        SELECT code, trade_date FROM {table} FINAL
        WHERE code IN ({code_sql})
          AND trade_date >= toDate({quote_sql(args.start_date)})
          AND trade_date <= toDate({quote_sql(args.end_date)})
        """
    ).result_rows
    actual = {(str(row[0]).upper(), row[1]) for row in actual_rows}
    ensure_source_absence_table(client)
    source_absent_rows = client.query(
        f"SELECT code, trade_date FROM {SOURCE_ABSENCE_TABLE} "
        f"WHERE source = 'qmt_xtquant' AND code IN ({code_sql}) "
        f"AND trade_date >= toDate({quote_sql(args.start_date)}) AND trade_date <= toDate({quote_sql(args.end_date)})"
    ).result_rows
    source_absent = {(str(row[0]).upper(), row[1]) for row in source_absent_rows}
    plan = remediation_plan(expected, actual, source_absent, market_status_excluded, candidate_limit)
    missing = sorted(plan["missing_keys"], key=lambda item: (item[0], item[1]))
    missing_by_code: dict[str, list[Any]] = {}
    missing_by_date: dict[Any, int] = {}
    for code, trade_date in missing:
        missing_by_code.setdefault(code, []).append(trade_date)
        missing_by_date[trade_date] = missing_by_date.get(trade_date, 0) + 1
    return {
        "coverage_contract": {
            "calendar": "SH trading days only",
            "universe": "stocks with quit = 0 or NULL",
            "listing_rule": "trade_date >= list_date when present",
            "delisting_rule": "trade_date <= delist_date when present",
            "market_status_rule": "announcement-backed suspension and post-delisting intervals are excluded; raw QMT absences remain auditable",
            "missing_rule": "expected calendar key absent from persisted daily bars",
            "source_absence_rule": "unresolved QMT responses require confirmation; they are not treated as proven business absences",
        },
        "calendar_expected_code_dates": len(expected),
        "expected_code_dates": len(effective_expected),
        "actual_code_dates": len(effective_expected & actual),
        "missing_code_dates": len(missing),
        "source_absent_code_dates": len((effective_expected - actual) & source_absent),
        "market_status_excluded_code_dates": len(market_status_excluded),
        "market_status_events": len(market_status_rows),
        "repair_backlog_code_dates": plan["repair_backlog_code_dates"],
        "qmt_retry_code_dates": plan["qmt_retry_code_dates"],
        "tdx_fallback_code_dates": plan["tdx_fallback_code_dates"],
        "repair_candidate_code_dates": len(missing),
        **contract_counts,
        "complete_codes": len(codes) - len(missing_by_code),
        "incomplete_codes": len(missing_by_code),
        "missing_by_code_sample": plan["missing_by_code"],
        "qmt_retry_by_code_sample": plan["qmt_retry_by_code"],
        "missing_by_trade_date_sample": [
            {"trade_date": str(trade_date), "missing_codes": count}
            for trade_date, count in sorted(missing_by_date.items(), reverse=True)[:100]
        ],
    }


def _field_frame(data: dict[str, Any], field: str) -> pd.DataFrame:
    value = data.get(field)
    if value is None:
        value = data.get(field.capitalize())
    return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _normalize_date(value: Any) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        text = str(value)
        if text.endswith(".0"):
            text = text[:-2]
        if len(text) >= 8:
            parsed = pd.to_datetime(text[:8], format="%Y%m%d", errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).normalize()


def _is_unclosed_current_daily_bar(trade_date: Any, created_at: datetime) -> bool:
    now = created_at.astimezone(ZoneInfo("Asia/Shanghai")) if created_at.tzinfo else created_at
    return trade_date == now.date() and (now.hour, now.minute) < QMT_DAILY_CLOSE_TIME


def rows_from_qmt(
    data: dict[str, Any],
    batch_codes: list[str],
    created_at: datetime,
    start_date: str,
    end_date: str,
    index_codes: set[str] | None = None,
) -> tuple[list[tuple], dict[str, int]]:
    close_df = _field_frame(data, "close")
    if close_df.empty:
        return [], {code: 0 for code in batch_codes}

    index_codes = {str(item).upper() for item in (index_codes or set())}
    frames = {field: _field_frame(data, field) for field in FIELD_LIST}
    rows: list[tuple] = []
    counts: dict[str, int] = {}
    for code in batch_codes:
        source_code = qmt_source_code(code)
        if source_code in close_df.index:
            one = pd.DataFrame(index=close_df.columns)
            for field, frame in frames.items():
                one[field] = pd.to_numeric(frame.loc[source_code], errors="coerce") if source_code in frame.index else 0.0
        elif source_code in close_df.columns:
            one = pd.DataFrame(index=close_df.index)
            for field, frame in frames.items():
                one[field] = pd.to_numeric(frame[source_code], errors="coerce") if source_code in frame.columns else 0.0
        else:
            counts[code] = 0
            continue

        normalized_index = [_normalize_date(item) for item in one.index]
        one.index = normalized_index
        one = one[~pd.isna(one.index)].copy()
        one = one.dropna(subset=["open", "high", "low", "close"]).sort_index()
        one = one[~one.index.duplicated(keep="last")]
        start_ts = pd.Timestamp(start_date).normalize()
        end_ts = pd.Timestamp(end_date).normalize()
        one = one[(one.index >= start_ts) & (one.index <= end_ts)]
        if one.empty:
            counts[code] = 0
            continue

        # SDK standard volume is lots for stocks and indices; raw DAT is separate.
        instrument_type = (
            "index"
            if str(code).upper() in index_codes or str(source_code).upper() in index_codes
            else "stock"
        )
        one = normalize_qmt_daily_units(one, instrument_type=instrument_type)

        prev_close = one["close"].shift(1)
        change_amount = (one["close"] - prev_close).fillna(0.0)
        change_pct = ((change_amount / prev_close.replace(0, pd.NA)) * 100).fillna(0.0)
        amplitude = (((one["high"] - one["low"]) / prev_close.replace(0, pd.NA)) * 100).fillna(0.0)
        count = 0
        for idx, values in one.iterrows():
            if _is_unclosed_current_daily_bar(idx.date(), created_at):
                continue
            rows.append(
                (
                    code,
                    idx.date(),
                    float(values["open"] or 0),
                    float(values["high"] or 0),
                    float(values["low"] or 0),
                    float(values["close"] or 0),
                    float(values.get("volume", 0) or 0),
                    float(values.get("amount", 0) or 0),
                    float(amplitude.loc[idx] or 0),
                    float(change_pct.loc[idx] or 0),
                    float(change_amount.loc[idx] or 0),
                    0.0,
                    created_at,
                )
            )
            count += 1
        counts[code] = count
    return rows, counts


def write_report(path: str, payload: dict[str, Any]) -> None:
    report = Path(path)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def fetch_to_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    ensure_stage_table(client, args.stage_table)
    ensure_request_log_table(client)
    if args.reset_stage:
        log(f"truncate stage table {args.stage_table}")
        client.command(f"TRUNCATE TABLE {args.stage_table}")

    codes = select_codes_for_args(client, args)
    index_codes = {
        str(row[0]).upper()
        for row in client.query("SELECT code FROM stocks WHERE type = 'index'").result_rows
    }
    done_codes = existing_stage_codes(client, args.stage_table) if args.resume else set()
    target_codes = [code for code in codes if code not in done_codes]
    market = QmtMiniMarketClient()
    market.connect()
    created_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    total_rows = 0
    empty_codes: list[str] = []
    failed: list[dict[str, str]] = []
    started = time.perf_counter()
    log(f"qmt fetch target_codes={len(target_codes)} skipped_existing={len(done_codes)} batch_size={args.batch_size}")

    for batch_no, batch_codes in enumerate(chunked(target_codes, args.batch_size), start=1):
        batch_started = time.perf_counter()
        attempts = 0
        batch_error = ""
        rows: list[tuple] = []
        qmt_batch_codes = sorted({qmt_source_code(code) for code in batch_codes})
        try:
            for attempt in range(1, args.max_retries + 2):
                attempts = attempt
                try:
                    if not args.skip_download:
                        if args.use_batch_download:
                            market.download_history_data2(
                                qmt_batch_codes,
                                "1d",
                                args.start_date.replace("-", ""),
                                args.end_date.replace("-", ""),
                            )
                        else:
                            for code in qmt_batch_codes:
                                market.download_history_data(code, "1d", args.start_date.replace("-", ""), args.end_date.replace("-", ""))
                    batch_error = ""
                    break
                except Exception as exc:
                    batch_error = f"{type(exc).__name__}: {exc}"
                    if attempt > args.max_retries:
                        raise
                    time.sleep(args.retry_sleep)
            data = market.get_market_data(
                field_list=FIELD_LIST,
                stock_list=qmt_batch_codes,
                period="1d",
                start_time=args.start_date.replace("-", ""),
                end_time=args.end_date.replace("-", ""),
                count=-1,
                dividend_type=args.dividend_type,
                fill_data=False,
            )
            rows, counts = rows_from_qmt(
                data,
                batch_codes,
                created_at,
                args.start_date,
                args.end_date,
                index_codes=index_codes,
            )
            empty_codes.extend([code for code, count in counts.items() if count == 0])
            if rows:
                client.insert(args.stage_table, rows, column_names=TARGET_COLUMNS)
                total_rows += len(rows)
            elapsed = time.perf_counter() - batch_started
            insert_request_log(
                client,
                task_name="qmt_xtquant_daily_backfill",
                phase="fetch",
                period="1d",
                codes=batch_codes,
                start_time=args.start_date,
                end_time=args.end_date,
                status="success",
                attempts=attempts,
                elapsed_sec=elapsed,
                rows_returned=len(rows),
            )
            log(f"batch {batch_no}: codes={len(batch_codes)} rows={len(rows)} attempts={attempts} elapsed={elapsed:.2f}s")
        except Exception as exc:
            elapsed = time.perf_counter() - batch_started
            failed.append({"batch": str(batch_no), "codes": ",".join(batch_codes), "error": f"{type(exc).__name__}: {exc}"})
            insert_request_log(
                client,
                task_name="qmt_xtquant_daily_backfill",
                phase="fetch",
                period="1d",
                codes=batch_codes,
                start_time=args.start_date,
                end_time=args.end_date,
                status="failed",
                attempts=attempts or 1,
                elapsed_sec=elapsed,
                rows_returned=len(rows),
                error=batch_error or f"{type(exc).__name__}: {exc}",
            )
            log(f"batch {batch_no}: failed {type(exc).__name__}: {exc}")
        if args.sleep > 0:
            time.sleep(args.sleep)

    summary = {
        "phase": "fetch",
        "codes": len(codes),
        "target_codes": len(target_codes),
        "stage_rows_inserted_this_run": total_rows,
        "empty_codes": len(set(empty_codes)),
        "empty_codes_sample": sorted(set(empty_codes))[:200],
        "failed_batches": len(failed),
        "elapsed_sec": round(time.perf_counter() - started, 3),
    }
    write_report(args.report, {"summary": summary, "empty_codes_sample": sorted(set(empty_codes))[:200], "failed": failed})
    if failed:
        raise RuntimeError(f"qmt daily fetch has failed batches: {len(failed)} report={args.report}")
    return summary


def validate_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = select_codes_for_args(client, args)
    code_sql = ",".join(quote_sql(code) for code in codes) or "''"
    summary_row = client.query(
        f"""
        SELECT count(), uniqExact(code), min(trade_date), max(trade_date)
        FROM {args.stage_table}
        WHERE code IN ({code_sql})
        """
    ).first_row
    compare_rows = client.query(
        f"""
        SELECT
            count() AS overlap_rows,
            countIf(abs(q.close - k.close) > {args.price_tolerance}) AS close_diff_rows,
            max(abs(q.close - k.close)) AS max_close_abs_diff,
            avg(abs(q.close - k.close)) AS avg_close_abs_diff,
            countIf(abs(q.volume - k.volume) > {args.volume_tolerance}) AS volume_diff_rows
        FROM {args.stage_table} q
        INNER JOIN (SELECT * FROM kline_daily FINAL) k ON q.code = k.code AND q.trade_date = k.trade_date
        WHERE q.code IN ({code_sql})
          AND q.trade_date >= toDate({quote_sql(args.start_date)})
          AND q.trade_date <= toDate({quote_sql(args.end_date)})
        """
    ).first_row
    date_rows = client.query(
        f"""
        SELECT
            q.trade_date,
            uniqExact(q.code) AS qmt_codes,
            uniqExact(k.code) AS current_codes,
            countIf(k.code = '' OR k.code IS NULL) AS qmt_only_rows
        FROM {args.stage_table} q
        LEFT JOIN (SELECT * FROM kline_daily FINAL) k ON q.code = k.code AND q.trade_date = k.trade_date
        WHERE q.code IN ({code_sql})
          AND q.trade_date >= toDate({quote_sql(args.start_date)})
          AND q.trade_date <= toDate({quote_sql(args.end_date)})
        GROUP BY q.trade_date
        ORDER BY q.trade_date DESC
        LIMIT 20
        """
    ).result_rows
    missing_rows = client.query(
        f"""
        SELECT s.code, s.name, s.type
        FROM stocks s
        LEFT JOIN (SELECT DISTINCT code FROM {args.stage_table}) q ON s.code = q.code
        WHERE s.code IN ({code_sql}) AND (q.code = '' OR q.code IS NULL)
        ORDER BY s.type, s.code
        LIMIT 100
        """
    ).result_rows
    summary = {
        "phase": "validate_stage",
        "target_codes": len(codes),
        "stage_rows": int(summary_row[0] or 0),
        "stage_codes": int(summary_row[1] or 0),
        "min_date": str(summary_row[2]) if summary_row[2] else None,
        "max_date": str(summary_row[3]) if summary_row[3] else None,
        "missing_codes_sample": [{"code": row[0], "name": row[1], "type": row[2]} for row in missing_rows],
        "overlap_rows": int(compare_rows[0] or 0),
        "close_diff_rows": int(compare_rows[1] or 0),
        "max_close_abs_diff": float(compare_rows[2] or 0),
        "avg_close_abs_diff": float(compare_rows[3] or 0),
        "volume_diff_rows": int(compare_rows[4] or 0),
        "latest_date_coverage": [
            {
                "trade_date": str(row[0]),
                "qmt_codes": int(row[1] or 0),
                "current_codes": int(row[2] or 0),
                "qmt_only_rows": int(row[3] or 0),
            }
            for row in date_rows
        ],
    }
    summary.update(coverage_summary(client, args, args.stage_table))
    write_report(args.report, {"summary": summary})
    log("stage validation " + json.dumps(summary, ensure_ascii=False))
    return summary


def apply_stage(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = select_codes_for_args(client, args)
    total_inserted = 0
    for chunk_no, code_chunk in enumerate(chunked(codes, args.delete_chunk_size), start=1):
        code_sql = ",".join(quote_sql(code) for code in code_chunk)
        staged_keys = client.query(
            f"""
            SELECT s.code, s.trade_date
            FROM {args.stage_table} AS s
            INNER JOIN trade_calendar AS c
                ON c.market = 'SH' AND c.is_trading = 1 AND c.trade_date = s.trade_date
            WHERE s.code IN ({code_sql})
              AND s.trade_date >= toDate({quote_sql(args.start_date)})
              AND s.trade_date <= toDate({quote_sql(args.end_date)})
            """
        ).result_rows
        log(f"apply chunk {chunk_no}: replace staged keys={len(staged_keys)} codes={len(code_chunk)}")
        # Never delete a broad code/date range.  A partial QMT response must not
        # erase prior good bars that are absent from this staging run.
        for key_chunk in chunked([f"({quote_sql(code)}, toDate({quote_sql(trade_date)}))" for code, trade_date in staged_keys], 5000):
            client.command(
                f"""
                ALTER TABLE kline_daily
                DELETE WHERE (code, trade_date) IN ({", ".join(key_chunk)})
                SETTINGS mutations_sync = 1
                """
            )
        client.command(
            f"""
            INSERT INTO kline_daily ({", ".join(TARGET_COLUMNS)})
            WITH
                staged AS (
                    SELECT
                        code,
                        trade_date,
                        open,
                        high,
                        low,
                        close,
                        volume,
                        amount,
                        turnover_rate,
                        created_at
                    FROM {args.stage_table}
                    WHERE code IN ({code_sql})
                      AND trade_date >= toDate({quote_sql(args.start_date)})
                      AND trade_date <= toDate({quote_sql(args.end_date)})
                ),
                trading_staged AS (
                    SELECT s.*
                    FROM staged AS s
                    INNER JOIN trade_calendar AS c
                        ON c.market = 'SH'
                       AND c.is_trading = 1
                       AND c.trade_date = s.trade_date
                ),
                prev AS (
                    SELECT
                        code,
                        argMax(trade_date, trade_date) AS prev_trade_date,
                        argMax(close, trade_date) AS close
                    FROM kline_daily
                    WHERE code IN ({code_sql})
                      AND trade_date < toDate({quote_sql(args.start_date)})
                    GROUP BY code
                ),
                metric_source AS (
                    SELECT code, trade_date, high, low, close FROM trading_staged
                    UNION ALL
                    SELECT code, prev_trade_date AS trade_date, close AS high, close AS low, close FROM prev
                ),
                metric_calc AS (
                    SELECT
                        code,
                        trade_date,
                        lagInFrame(close, 1, 0.0) OVER (
                            PARTITION BY code
                            ORDER BY trade_date
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) AS prev_close
                    FROM metric_source
                )
            SELECT
                s.code,
                s.trade_date,
                s.open,
                s.high,
                s.low,
                s.close,
                s.volume,
                s.amount,
                if(m.prev_close > 0, (s.high - s.low) / m.prev_close * 100, 0.0) AS amplitude,
                if(m.prev_close > 0, (s.close - m.prev_close) / m.prev_close * 100, 0.0) AS change_pct,
                if(m.prev_close > 0, s.close - m.prev_close, 0.0) AS change_amount,
                s.turnover_rate,
                s.created_at
            FROM trading_staged s
            LEFT JOIN metric_calc m ON s.code = m.code AND s.trade_date = m.trade_date
            """
        )
        inserted = client.query(
            f"""
            SELECT count()
            FROM {args.stage_table} AS s
            INNER JOIN trade_calendar AS c
                ON c.market = 'SH'
               AND c.is_trading = 1
               AND c.trade_date = s.trade_date
            WHERE s.code IN ({code_sql})
              AND s.trade_date >= toDate({quote_sql(args.start_date)})
              AND s.trade_date <= toDate({quote_sql(args.end_date)})
            """
        ).first_row[0]
        total_inserted += int(inserted or 0)
    summary = {"phase": "apply", "codes": len(codes), "inserted_rows": total_inserted}
    write_report(args.report, {"summary": summary})
    log("apply summary " + json.dumps(summary, ensure_ascii=False))
    return summary


def validate_target(args: argparse.Namespace) -> dict[str, Any]:
    client = ch_client()
    codes = select_codes_for_args(client, args)
    code_sql = ",".join(quote_sql(code) for code in codes) or "''"
    row = client.query(
        f"""
        SELECT count(), uniqExact(code), min(trade_date), max(trade_date)
        FROM kline_daily
        WHERE code IN ({code_sql})
          AND trade_date >= toDate({quote_sql(args.start_date)})
          AND trade_date <= toDate({quote_sql(args.end_date)})
        """
    ).first_row
    dup = client.query(
        f"""
        SELECT count()
        FROM (
            SELECT code, trade_date, count() c
            FROM kline_daily
            WHERE code IN ({code_sql})
              AND trade_date >= toDate({quote_sql(args.start_date)})
              AND trade_date <= toDate({quote_sql(args.end_date)})
            GROUP BY code, trade_date
            HAVING c > 1
        )
        """
    ).first_row[0]
    summary = {
        "phase": "validate_target",
        "target_codes": len(codes),
        "rows": int(row[0] or 0),
        "codes": int(row[1] or 0),
        "min_date": str(row[2]) if row[2] else None,
        "max_date": str(row[3]) if row[3] else None,
        "duplicate_keys": int(dup or 0),
    }
    summary.update(coverage_summary(client, args, "kline_daily"))
    write_report(args.report, {"summary": summary})
    log("target validation " + json.dumps(summary, ensure_ascii=False))
    return summary


def parse_args() -> argparse.Namespace:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    default_report = report_path("qmtmini_daily_backfill_validate", f"qmtmini_daily_{today.replace('-', '')}.json")
    parser = argparse.ArgumentParser(description="Fetch and validate QMT Mini 1d bars against ClickHouse kline_daily.")
    parser.add_argument("--phase", choices=["fetch", "validate-stage", "apply", "validate-target", "all"], default="validate-stage")
    parser.add_argument("--start-date", default="2026-06-01")
    parser.add_argument("--end-date", default=today)
    parser.add_argument("--codes", default="", help="Comma-separated stock codes. Empty means stocks from ClickHouse.")
    parser.add_argument("--code-offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--delete-chunk-size", type=int, default=200)
    parser.add_argument("--stage-table", default=DEFAULT_STAGE_TABLE)
    parser.add_argument("--reset-stage", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.02)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=1.0)
    parser.add_argument("--use-batch-download", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-download", action="store_true", help="Query QMT API directly without download_history_data/download_history_data2.")
    parser.add_argument("--dividend-type", default="none", choices=["none", "front", "back"])
    parser.add_argument("--price-tolerance", type=float, default=0.001)
    parser.add_argument("--volume-tolerance", type=float, default=1.0)
    parser.add_argument("--require-complete", action="store_true", help="Fail when the trade-calendar coverage matrix has missing code/date keys.")
    parser.add_argument("--report", default=str(default_report))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summaries: list[dict[str, Any]] = []
    if args.phase in {"fetch", "all"}:
        summaries.append(fetch_to_stage(args))
    if args.phase in {"validate-stage", "all"}:
        summaries.append(validate_stage(args))
    if args.phase in {"apply", "all"}:
        summaries.append(apply_stage(args))
    if args.phase in {"validate-target", "all"}:
        summaries.append(validate_target(args))
    if args.require_complete:
        incomplete = [item for item in summaries if int(item.get("missing_code_dates", 0) or 0) > 0]
        empty_fetch = [item for item in summaries if int(item.get("empty_codes", 0) or 0) > 0]
        if incomplete or empty_fetch:
            raise RuntimeError(
                "daily coverage incomplete: "
                + json.dumps({"coverage": incomplete, "empty_fetch": empty_fetch}, ensure_ascii=False)
            )
    log("done " + json.dumps(summaries, ensure_ascii=False))


if __name__ == "__main__":
    main()
