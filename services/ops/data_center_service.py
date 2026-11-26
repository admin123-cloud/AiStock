"""数据中心统一查询服务（直连 ClickHouse 物理表，零旧视图/旧缓存误报）。

提供四大核心能力：
  1. get_data_overview()          — ClickHouse 全量现役业务表存储占用、总行数、时间跨度与资产池统计
  2. get_health_matrix(days)      — 交易日交付矩阵（A股日线/分钟线 + 11大核心指数 + 板块 + G3实战核心/全量ETF + 国家队份额 + 宏观ERP/债率 + 南向资金）
  3. inspect_kline(code, days)    — 单标的 K 线槽位体检（支持股票、核心指数与跨资产/宽基/国债 ETF）
  4. get_runtime_health_summary() — 顶部全局健康横幅摘要（含 A 股、指数与 G3 实战策略核心数据）
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import clickhouse_connect

from utils.common.logger import get_logger
from utils.kline.market_warehouse import clickhouse_client

logger = get_logger("data_center")
BJ_TZ = timezone(timedelta(hours=8))

# 生产正式维护的 11 大核心权益指数
CORE_INDICES: tuple[str, ...] = (
    "000001.SH",  # 上证指数
    "000016.SH",  # 上证50
    "000300.SH",  # 沪深300
    "000680.SH",  # 科创综指
    "000688.SH",  # 科创50
    "000852.SH",  # 中证1000
    "000905.SH",  # 中证500
    "399001.SZ",  # 深证成指
    "399006.SZ",  # 创业板指
    "399106.SZ",  # 深证综指
    "899050.BJ",  # 北证50
)

CORE_INDEX_NAMES: dict[str, str] = {
    "000001.SH": "上证指数",
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000680.SH": "科创综指",
    "000688.SH": "科创50",
    "000852.SH": "中证1000",
    "000905.SH": "中证500",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "399106.SZ": "深证综指",
    "899050.BJ": "北证50",
}

# G3 实战（AR4.5 / AR4.6）核心跨资产 ETF 类别（宽基 / 红利 / 黄金 / 豆粕商品 / 跨境 / 利率债）
CORE_CROSS_ASSET_CLASSES: tuple[str, ...] = (
    "cn_broad",
    "cn_dividend",
    "gold",
    "commodity",
    "overseas",
    "bond",
)

ETF_ASSET_CLASS_LABELS: dict[str, str] = {
    "cn_broad": "A股宽基",
    "cn_dividend": "A股红利",
    "gold": "黄金避险",
    "commodity": "商品期货",
    "overseas": "跨境QDII",
    "bond": "利率债",
    "cn_sector": "行业主题",
    "commodity_ext": "商品扩展",
    "overseas_ext": "全球跨境",
}

# G3 实战核心跨资产 15 只 ETF 清单（与 scripts/sync_cross_asset_etf_daily.py 对齐）
CORE_CROSS_ASSET_ETFS: dict[str, str] = {
    "510300.SH": "沪深300ETF",
    "510500.SH": "中证500ETF",
    "512100.SH": "中证1000ETF",
    "159915.SZ": "创业板ETF",
    "588000.SH": "科创50ETF",
    "512890.SH": "红利低波ETF",
    "510880.SH": "红利ETF",
    "518880.SH": "黄金ETF",
    "159985.SZ": "豆粕ETF",
    "513100.SH": "纳指ETF",
    "513500.SH": "标普500ETF",
    "513180.SH": "恒生科技ETF",
    "513050.SH": "中概互联ETF",
    "511260.SH": "十年国债ETF",
    "511090.SH": "30年国债ETF",
}

# AR4 国家队核心监测 15 只上交所 ETF 份额清单（与 scripts/sync_ar4_macro_etf_shares.py 对齐）
AR4_NATIONAL_TEAM_ETFS: dict[str, str] = {
    "510300.SH": "华泰柏瑞沪深300ETF",
    "510310.SH": "易方达沪深300ETF",
    "510330.SH": "华夏沪深300ETF",
    "510050.SH": "华夏上证50ETF",
    "510500.SH": "南方中证500ETF",
    "512100.SH": "南方中证1000ETF",
    "588000.SH": "华夏科创50ETF",
    "510880.SH": "华泰柏瑞红利ETF",
    "512890.SH": "华泰柏瑞红利低波ETF",
    "518880.SH": "华安黄金ETF",
    "513100.SH": "国泰纳指ETF",
    "513500.SH": "博时标普500ETF",
    "513180.SH": "华夏恒生科技ETF",
    "511260.SH": "国泰十年国债ETF",
    "511090.SH": "鹏扬30年国债ETF",
}

_MATRIX_CACHE: dict[int, dict[str, Any]] = {}
_OVERVIEW_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_CACHE_TTL = 180.0
_OVERVIEW_LOCK = threading.Lock()
_OVERVIEW_REFRESHING = False
_MATRIX_LOCK = threading.Lock()
_MATRIX_REFRESHING: set[int] = set()


def _client():
    return clickhouse_connect.get_client(
        host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
        port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
        password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
        database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
        connect_timeout=10,
        send_receive_timeout=60,
    )


def _query(sql: str, params: dict[str, Any] | None = None) -> list[tuple]:
    own_client = False
    try:
        c = clickhouse_client()
    except Exception:
        c = _client()
        own_client = True
    try:
        return list(c.query(sql, parameters=params or {}).result_rows)
    finally:
        if own_client:
            c.close()


def _normalize_code(raw: str) -> str:
    """将 6 位证券代码规范化为带交易所后缀的代码（兼容上交所 5/6/9 开头含 ETF 与国债 ETF）。"""
    s = raw.strip().upper()
    if "." in s:
        return s
    if len(s) == 6 and s.isdigit():
        if s.startswith(("5", "6", "9")):
            return f"{s}.SH"
        if s.startswith(("8", "4")):
            return f"{s}.BJ"
        return f"{s}.SZ"
    return s


def invalidate_data_center_cache() -> None:
    """清空数据中心内存缓存（供外部同步任务完成后调用）。"""
    with _OVERVIEW_LOCK:
        _OVERVIEW_CACHE["payload"] = None
        _OVERVIEW_CACHE["expires_at"] = 0.0
    with _MATRIX_LOCK:
        _MATRIX_CACHE.clear()


def _refresh_data_overview_bg() -> None:
    global _OVERVIEW_REFRESHING
    try:
        payload = _compute_data_overview()
        with _OVERVIEW_LOCK:
            _OVERVIEW_CACHE["payload"] = payload
            _OVERVIEW_CACHE["expires_at"] = time.monotonic() + _CACHE_TTL
    except Exception as exc:
        logger.warning("background data overview refresh failed: %s", exc)
    finally:
        with _OVERVIEW_LOCK:
            _OVERVIEW_REFRESHING = False


def get_data_overview(*, force_refresh: bool = False) -> dict[str, Any]:
    """查询 ClickHouse 全部现役业务表的真实存储占用、行数与时间覆盖范围（SWR 180s 缓存）。"""
    global _OVERVIEW_REFRESHING
    now_mono = time.monotonic()
    if not force_refresh:
        with _OVERVIEW_LOCK:
            cached_payload = _OVERVIEW_CACHE.get("payload")
            if cached_payload is not None:
                if now_mono < float(_OVERVIEW_CACHE.get("expires_at") or 0.0):
                    return cached_payload
                if not _OVERVIEW_REFRESHING:
                    _OVERVIEW_REFRESHING = True
                    threading.Thread(
                        target=_refresh_data_overview_bg,
                        daemon=True,
                        name="data-overview-refresh",
                    ).start()
                return cached_payload

    payload = _compute_data_overview()
    with _OVERVIEW_LOCK:
        _OVERVIEW_CACHE["payload"] = payload
        _OVERVIEW_CACHE["expires_at"] = time.monotonic() + _CACHE_TTL
    return payload


def _compute_data_overview() -> dict[str, Any]:
    now_bj = datetime.now(BJ_TZ)
    # (table, label, time_col, group)
    target_tables: list[tuple[str, str, str, str]] = [
        ("kline_daily", "股票/指数日线", "trade_date", "market"),
        ("kline_minute_5", "5分钟 K 线", "datetime", "market"),
        ("kline_minute_15", "15分钟 K 线", "datetime", "market"),
        ("kline_minute_30", "30分钟 K 线", "datetime", "market"),
        ("kline_minute_60", "60分钟 K 线", "datetime", "market"),
        ("sector_kline_daily", "通达信板块日线", "trade_date", "market"),
        ("etf_kline_daily", "宽基/红利/国债/黄金/跨境 ETF 日线", "trade_date", "g3_strategy"),
        ("ar4_etf_share_daily", "上交所国家队 ETF 份额日线", "trade_date", "g3_strategy"),
        ("ar4_macro_daily", "宏观 ERP 与 10Y 中美债收益率", "trade_date", "g3_strategy"),
        ("ar4_southbound_daily", "港股通南向资金净买日线", "trade_date", "g3_strategy"),
        ("stock_financial_disclosure", "上市公司财报披露（选股/排雷）", "report_date", "g3_strategy"),
        ("stock_earnings_forecast", "上市公司业绩预告（排雷盾）", "report_date", "g3_strategy"),
        ("stock_dividend_events", "分红除权事件（高股息校验）", "ex_dividend_date", "g3_strategy"),
        ("stocks", "证券主数据", "updated_at", "reference"),
        ("trade_calendar", "交易日历", "trade_date", "reference"),
    ]

    cls_sql = ", ".join(f"'{c}'" for c in CORE_CROSS_ASSET_CLASSES)

    # 1 & 2. 并发读取 system.parts、证券池计数与 ETF 资产池计数
    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_parts = executor.submit(
            _query,
            """
            SELECT
                table,
                sum(rows) AS total_rows,
                sum(bytes_on_disk) AS disk_bytes
            FROM system.parts
            WHERE database = 'stock' AND active = 1
            GROUP BY table
            """,
        )
        fut_pool = executor.submit(
            _query,
            """
            SELECT
                countIf(type = 'stock' AND quit = 0 AND listing_status = 'active') AS active_stocks,
                countIf(type = 'index') AS total_indices,
                (SELECT count() FROM stock.sectors WHERE type = 'industry') AS industry_sectors
            FROM stock.stocks FINAL
            """,
        )
        fut_etf_pool = executor.submit(
            _query,
            f"""
            SELECT
                uniqExact(code) AS total_etfs,
                uniqExactIf(code, asset_class IN ({cls_sql})) AS core_etfs,
                (SELECT uniqExact(code) FROM stock.ar4_etf_share_daily FINAL) AS nt_etfs
            FROM stock.etf_kline_daily FINAL
            """,
        )
        parts_rows = fut_parts.result()
        pool_rows = fut_pool.result()
        try:
            etf_pool_rows = fut_etf_pool.result()
        except Exception as exc:
            logger.warning("etf pool stat query failed: %s", exc)
            etf_pool_rows = []

    parts_map = {str(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in parts_rows}
    active_stocks, total_indices, industry_sectors = (
        (int(pool_rows[0][0]), int(pool_rows[0][1]), int(pool_rows[0][2]))
        if pool_rows
        else (0, 0, 0)
    )
    total_etfs, core_etfs, nt_etfs = (
        (int(etf_pool_rows[0][0]), int(etf_pool_rows[0][1]), int(etf_pool_rows[0][2]))
        if etf_pool_rows
        else (0, len(CORE_CROSS_ASSET_ETFS), len(AR4_NATIONAL_TEAM_ETFS))
    )

    # 3. 并发查各表 min/max 时间范围
    def _fetch_table_stat(spec: tuple[str, str, str, str]) -> tuple[dict[str, Any], int, int]:
        tbl, label, time_col, group = spec
        rows_cnt, disk_bytes = parts_map.get(tbl, (0, 0))
        min_t, max_t = None, None
        if rows_cnt > 0:
            try:
                if tbl == "stocks" and time_col == "updated_at":
                    r = _query(
                        "SELECT toString(minIf(updated_at, updated_at > toDateTime('2000-01-01 00:00:00'))), "
                        "toString(max(updated_at)) FROM stock.stocks FINAL"
                    )
                else:
                    r = _query(f"SELECT toString(min({time_col})), toString(max({time_col})) FROM stock.{tbl}")
                if r:
                    min_t, max_t = str(r[0][0]), str(r[0][1])
            except Exception as exc:
                logger.warning("table %s min/max failed: %s", tbl, exc)
        return (
            {
                "table": tbl,
                "label": label,
                "group": group,
                "rows": rows_cnt,
                "size_mb": round(disk_bytes / (1024 * 1024), 2),
                "min_time": min_t,
                "max_time": max_t,
            },
            rows_cnt,
            disk_bytes,
        )

    table_stats: list[dict[str, Any]] = []
    total_rows_all = 0
    total_bytes_all = 0
    max_by_table: dict[str, str | None] = {}

    with ThreadPoolExecutor(max_workers=8) as executor:
        for stat, rows_cnt, disk_bytes in executor.map(_fetch_table_stat, target_tables):
            table_stats.append(stat)
            total_rows_all += rows_cnt
            total_bytes_all += disk_bytes
            max_by_table[stat["table"]] = stat["max_time"][:10] if stat["max_time"] else None

    return {
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "latest_daily_date": max_by_table.get("kline_daily"),
        "summary": {
            "active_stocks": active_stocks,
            "core_indices": len(CORE_INDICES),
            "total_indices_in_catalog": total_indices,
            "industry_sectors": industry_sectors,
            "core_etfs": core_etfs or len(CORE_CROSS_ASSET_ETFS),
            "total_etfs": total_etfs,
            "national_team_etfs": nt_etfs or len(AR4_NATIONAL_TEAM_ETFS),
            "latest_etf_date": max_by_table.get("etf_kline_daily"),
            "latest_etf_share_date": max_by_table.get("ar4_etf_share_daily"),
            "latest_macro_date": max_by_table.get("ar4_macro_daily"),
            "latest_southbound_date": max_by_table.get("ar4_southbound_daily"),
            "total_rows": total_rows_all,
            "total_size_gb": round(total_bytes_all / (1024 ** 3), 2),
        },
        "tables": table_stats,
    }


def _cell_status(actual: int, expected: int, is_future_or_pending: bool, ok_threshold: float = 0.99) -> str:
    if is_future_or_pending:
        return "pending"
    if expected <= 0:
        return "unknown"
    ratio = actual / expected
    if ratio >= ok_threshold:
        return "ok"
    if ratio >= 0.95:
        return "warn"
    return "missing"


def _refresh_health_matrix_bg(days: int) -> None:
    try:
        payload = _compute_health_matrix(days=days)
        with _MATRIX_LOCK:
            _MATRIX_CACHE[days] = {"expires_at": time.monotonic() + _CACHE_TTL, "payload": payload}
    except Exception as exc:
        logger.warning("background health matrix refresh (%d) failed: %s", days, exc)
    finally:
        with _MATRIX_LOCK:
            _MATRIX_REFRESHING.discard(days)


def get_health_matrix(days: int = 10, *, force_refresh: bool = False) -> dict[str, Any]:
    """实时计算近 N 个交易日的真实数据交付矩阵（SWR 180s 缓存、并发统计）。"""
    days = max(5, min(30, int(days)))
    now_mono = time.monotonic()
    if not force_refresh:
        with _MATRIX_LOCK:
            cached = _MATRIX_CACHE.get(days)
            if cached and cached.get("payload") is not None:
                if now_mono < float(cached.get("expires_at") or 0.0):
                    return cached["payload"]
                if days not in _MATRIX_REFRESHING:
                    _MATRIX_REFRESHING.add(days)
                    threading.Thread(
                        target=_refresh_health_matrix_bg,
                        args=(days,),
                        daemon=True,
                        name=f"health-matrix-refresh-{days}",
                    ).start()
                return cached["payload"]

    payload = _compute_health_matrix(days=days)
    with _MATRIX_LOCK:
        _MATRIX_CACHE[days] = {"expires_at": time.monotonic() + _CACHE_TTL, "payload": payload}
    return payload


def _safe_query(sql: str, params: dict[str, Any] | None = None, label: str = "") -> list[tuple]:
    try:
        return _query(sql, params)
    except Exception as exc:
        logger.warning("health matrix query (%s) failed: %s", label, exc)
        return []


def _compute_health_matrix(days: int = 10) -> dict[str, Any]:
    now_bj = datetime.now(BJ_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")
    after_postclose = (now_bj.hour, now_bj.minute) >= (17, 30)
    after_g3_feed = (now_bj.hour, now_bj.minute) >= (18, 0)

    # 1. 取最近 days 个交易日（含今天）
    cal_rows = _query(
        """
        SELECT toString(trade_date) AS d
        FROM stock.trade_calendar
        WHERE market = 'SH' AND is_trading = 1 AND trade_date <= today()
        ORDER BY trade_date DESC
        LIMIT {n:UInt32}
        """,
        {"n": days},
    )
    dates = [str(r[0]) for r in reversed(cal_rows)]
    if not dates:
        return {"generated_at": now_bj.isoformat(timespec="seconds"), "dates": [], "datasets": []}

    start_date = dates[0]
    core_idx_sql = ", ".join(f"'{c}'" for c in CORE_INDICES)
    cls_sql = ", ".join(f"'{c}'" for c in CORE_CROSS_ASSET_CLASSES)

    # 2–10. 并发执行全市场与 G3 实战策略所有统计查询
    with ThreadPoolExecutor(max_workers=8) as executor:
        fut_active = executor.submit(
            _query,
            "SELECT count() FROM stock.stocks FINAL WHERE type = 'stock' AND quit = 0 AND listing_status = 'active'",
        )
        fut_stock_daily = executor.submit(
            _query,
            """
            SELECT toString(trade_date) AS d, uniqExact(code) AS cnt
            FROM stock.kline_daily
            WHERE trade_date >= toDate({s:String})
              AND code IN (SELECT code FROM stock.stocks FINAL WHERE type = 'stock' AND quit = 0 AND listing_status = 'active')
            GROUP BY d
            """,
            {"s": start_date},
        )
        fut_min_s = {
            p: executor.submit(
                _query,
                f"""
                SELECT toString(toDate(datetime)) AS d, uniqExact(code) AS cnt
                FROM stock.kline_minute_{p}
                WHERE datetime >= toDateTime('{start_date} 00:00:00')
                  AND code IN (SELECT code FROM stock.stocks FINAL WHERE type = 'stock' AND quit = 0 AND listing_status = 'active')
                GROUP BY d
                """,
            )
            for p in ("5", "15", "30", "60")
        }
        fut_min_i = {
            p: executor.submit(
                _query,
                f"""
                SELECT toString(toDate(datetime)) AS d, groupUniqArray(code) AS codes
                FROM stock.kline_minute_{p}
                WHERE datetime >= toDateTime('{start_date} 00:00:00')
                  AND code IN ({core_idx_sql})
                GROUP BY d
                """,
            )
            for p in ("5", "15", "30", "60")
        }
        fut_idx_daily = executor.submit(
            _query,
            f"""
            SELECT toString(trade_date) AS d, groupUniqArray(code) AS codes
            FROM stock.kline_daily
            WHERE trade_date >= toDate('{start_date}')
              AND code IN ({core_idx_sql})
            GROUP BY d
            """,
        )
        fut_total_sec = executor.submit(_query, "SELECT count() FROM stock.sectors FINAL")
        fut_sec_daily = executor.submit(
            _query,
            f"""
            SELECT toString(trade_date) AS d, uniqExact(code) AS cnt
            FROM stock.sector_kline_daily FINAL
            WHERE trade_date >= toDate('{start_date}')
            GROUP BY d
            """,
        )
        # G3 实战扩展数据：ETF 池元数据 + 核心/全量 ETF 日线 + 国家队份额 + 宏观 + 南向
        fut_etf_meta = executor.submit(
            _safe_query,
            "SELECT code, any(name), any(asset_class) FROM stock.etf_kline_daily FINAL GROUP BY code",
            None,
            "etf_meta",
        )
        fut_etf_core = executor.submit(
            _safe_query,
            f"""
            SELECT toString(trade_date) AS d, groupUniqArray(code) AS codes
            FROM stock.etf_kline_daily FINAL
            WHERE trade_date >= toDate('{start_date}')
              AND asset_class IN ({cls_sql})
            GROUP BY d
            """,
            None,
            "etf_core",
        )
        fut_etf_all = executor.submit(
            _safe_query,
            f"""
            SELECT toString(trade_date) AS d, groupUniqArray(code) AS codes
            FROM stock.etf_kline_daily FINAL
            WHERE trade_date >= toDate('{start_date}')
            GROUP BY d
            """,
            None,
            "etf_all",
        )
        fut_etf_share = executor.submit(
            _safe_query,
            f"""
            SELECT toString(trade_date) AS d, groupUniqArray(code) AS codes
            FROM stock.ar4_etf_share_daily FINAL
            WHERE trade_date >= toDate('{start_date}')
            GROUP BY d
            """,
            None,
            "ar4_etf_share",
        )
        fut_macro = executor.submit(
            _safe_query,
            f"""
            SELECT toString(trade_date) AS d, count() AS cnt
            FROM stock.ar4_macro_daily FINAL
            WHERE trade_date >= toDate('{start_date}')
            GROUP BY d
            """,
            None,
            "ar4_macro",
        )
        fut_southbound = executor.submit(
            _safe_query,
            f"""
            SELECT toString(trade_date) AS d, count() AS cnt
            FROM stock.ar4_southbound_daily FINAL
            WHERE trade_date >= toDate('{start_date}')
            GROUP BY d
            """,
            None,
            "ar4_southbound",
        )

        active_stock_rows = fut_active.result()
        stock_daily_rows = fut_stock_daily.result()
        minute_maps = {p: {str(r[0]): int(r[1]) for r in fut.result()} for p, fut in fut_min_s.items()}
        idx_minute_sets = {p: {str(r[0]): set(str(x) for x in r[1]) for r in fut.result()} for p, fut in fut_min_i.items()}
        idx_daily_sets = {str(r[0]): set(str(x) for x in r[1]) for r in fut_idx_daily.result()}
        total_sectors_rows = fut_total_sec.result()
        sec_rows = fut_sec_daily.result()

        etf_meta_rows = fut_etf_meta.result()
        etf_core_sets = {str(r[0]): set(str(x) for x in r[1]) for r in fut_etf_core.result()}
        etf_all_sets = {str(r[0]): set(str(x) for x in r[1]) for r in fut_etf_all.result()}
        etf_share_sets = {str(r[0]): set(str(x) for x in r[1]) for r in fut_etf_share.result()}
        macro_map = {str(r[0]): min(1, int(r[1])) for r in fut_macro.result()}
        sb_map = {str(r[0]): min(1, int(r[1])) for r in fut_southbound.result()}

    active_stock_count = int(active_stock_rows[0][0]) if active_stock_rows else 5570
    stock_daily_map = {str(r[0]): int(r[1]) for r in stock_daily_rows}
    expected_sectors = int(total_sectors_rows[0][0]) if total_sectors_rows and int(total_sectors_rows[0][0]) > 0 else 604
    sec_map = {str(r[0]): int(r[1]) for r in sec_rows}

    # 构造核心 ETF / 全量 ETF / 国家队 ETF 字典（代码 -> 名称）
    etf_all_names: dict[str, str] = dict(CORE_CROSS_ASSET_ETFS)
    etf_core_names: dict[str, str] = dict(CORE_CROSS_ASSET_ETFS)
    for code, name, acls in etf_meta_rows:
        c_str = str(code)
        n_str = str(name or c_str)
        etf_all_names[c_str] = n_str
        if str(acls) in CORE_CROSS_ASSET_CLASSES:
            etf_core_names[c_str] = n_str
    nt_etf_names: dict[str, str] = dict(AR4_NATIONAL_TEAM_ETFS)
    for s_codes in etf_share_sets.values():
        for c_str in s_codes:
            nt_etf_names.setdefault(c_str, etf_all_names.get(c_str, c_str))

    def _build_cells(
        actual_map: dict[str, int],
        expected_fn,
        ok_threshold: float = 0.99,
        *,
        pending_today_until_ready: bool | None = None,
        always_pending_today_if_empty: bool = False,
        missing_items_fn=None,
    ) -> list[dict[str, Any]]:
        ready_today = after_postclose if pending_today_until_ready is None else pending_today_until_ready
        cells = []
        for d in dates:
            is_pending = (d > today_str) or (
                d == today_str and (always_pending_today_if_empty or not ready_today)
            )
            exp = expected_fn(d)
            act = actual_map.get(d, 0)
            if is_pending and act == 0:
                cov = None
                st = "pending"
                missing_items: list[str] = []
            else:
                cov = round(min(100.0, (act / exp) * 100.0), 1) if exp > 0 else None
                st = _cell_status(act, exp, False, ok_threshold=ok_threshold)
                missing_items = missing_items_fn(d) if (missing_items_fn and act < exp) else []
            cell: dict[str, Any] = {
                "date": d,
                "actual": act,
                "expected": exp,
                "coverage": cov,
                "missing": max(0, exp - act),
                "status": st,
            }
            if missing_items:
                cell["missing_items"] = missing_items
            cells.append(cell)
        return cells

    def _missing_from_universe(present_map: dict[str, set[str]], universe: dict[str, str], limit: int = 20):
        all_codes = sorted(universe.keys())

        def _fn(d: str) -> list[str]:
            present = present_map.get(d, set())
            miss = [f"{c} {universe[c]}" for c in all_codes if c not in present]
            return miss[:limit]

        return _fn

    core_idx_n = len(CORE_INDICES)
    etf_core_n = len(etf_core_names)
    etf_all_n = len(etf_all_names)
    nt_etf_n = len(nt_etf_names)

    datasets = [
        # ── 组 1：G3 实战策略数据（宽基 / 红利 / 国债 / 黄金 / 跨境 / 国家队份额 / 宏观 / 南向） ──
        {
            "id": "etf_core_daily",
            "group": "g3_strategy",
            "group_label": "G3 实战策略数据（宽基 / 国债 / 宏观 / 南向）",
            "label": f"实战核心跨资产 ETF ({etf_core_n}只)",
            "note": "AR4.6 核心白名单：A股宽基(5) + 红利(2) + 黄金(1) + 美股/港科(4) + 10Y/30Y国债(2)，要求 100% 完整",
            "cells": _build_cells(
                {d: len(s) for d, s in etf_core_sets.items()},
                lambda _: etf_core_n,
                ok_threshold=1.0,
                pending_today_until_ready=after_g3_feed,
                missing_items_fn=_missing_from_universe(etf_core_sets, etf_core_names),
            ),
        },
        {
            "id": "etf_all_daily",
            "group": "g3_strategy",
            "group_label": "G3 实战策略数据（宽基 / 国债 / 宏观 / 南向）",
            "label": f"全量 ETF 日线池 ({etf_all_n}只)",
            "note": "含 15 只核心跨资产 + 30 只行业主题 + 3 只商品扩展 + 13 只全球跨境 ETF（≥98% 视为完整）",
            "cells": _build_cells(
                {d: len(s) for d, s in etf_all_sets.items()},
                lambda _: etf_all_n,
                ok_threshold=0.98,
                pending_today_until_ready=after_g3_feed,
                missing_items_fn=_missing_from_universe(etf_all_sets, etf_all_names),
            ),
        },
        {
            "id": "ar4_etf_share_daily",
            "group": "g3_strategy",
            "group_label": "G3 实战策略数据（宽基 / 国债 / 宏观 / 南向）",
            "label": f"国家队 ETF 份额 ({nt_etf_n}只)",
            "note": "上交所官方 15 只核心宽基/红利/国债/黄金 ETF 每日份额（上交所 T+1 公布前一日，当日未公布标为尚未到期）",
            "cells": _build_cells(
                {d: len(s) for d, s in etf_share_sets.items()},
                lambda _: nt_etf_n,
                ok_threshold=1.0,
                always_pending_today_if_empty=True,
                missing_items_fn=_missing_from_universe(etf_share_sets, nt_etf_names),
            ),
        },
        {
            "id": "ar4_macro_daily",
            "group": "g3_strategy",
            "group_label": "G3 实战策略数据（宽基 / 国债 / 宏观 / 南向）",
            "label": "宏观 ERP & 10Y 中美债",
            "note": "stock.ar4_macro_daily（沪深300 PE/ERP、中债10Y/2Y、美债10Y、国家队持仓分位）每日 1 条",
            "cells": _build_cells(
                macro_map,
                lambda _: 1,
                ok_threshold=1.0,
                pending_today_until_ready=after_g3_feed,
            ),
        },
        {
            "id": "ar4_southbound_daily",
            "group": "g3_strategy",
            "group_label": "G3 实战策略数据（宽基 / 国债 / 宏观 / 南向）",
            "label": "港股通南向资金净买",
            "note": "stock.ar4_southbound_daily（20日南向净买港科滤网开关）每日 1 条",
            "cells": _build_cells(
                sb_map,
                lambda _: 1,
                ok_threshold=1.0,
                pending_today_until_ready=after_g3_feed,
            ),
        },
        # ── 组 2：A 股行情、核心指数与行业板块 ──
        {
            "id": "stock_daily",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": "股票日线",
            "note": f"分母 = 在市股票 {active_stock_count} 只（≥99% 视为完整，容忍日常停牌）",
            "cells": _build_cells(stock_daily_map, lambda _: active_stock_count, ok_threshold=0.99),
        },
        {
            "id": "stock_5m",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": "股票 5分钟",
            "note": "分母 = 当日有成交日线股数（≥99.5% 视为完整）",
            "cells": _build_cells(minute_maps["5"], lambda d: stock_daily_map.get(d, active_stock_count), ok_threshold=0.995),
        },
        {
            "id": "stock_15m",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": "股票 15分钟",
            "note": "分母 = 当日有成交日线股数（≥99.5% 视为完整）",
            "cells": _build_cells(minute_maps["15"], lambda d: stock_daily_map.get(d, active_stock_count), ok_threshold=0.995),
        },
        {
            "id": "stock_30m",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": "股票 30分钟",
            "note": "分母 = 当日有成交日线股数（≥99.5% 视为完整）",
            "cells": _build_cells(minute_maps["30"], lambda d: stock_daily_map.get(d, active_stock_count), ok_threshold=0.995),
        },
        {
            "id": "stock_60m",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": "股票 60分钟",
            "note": "分母 = 当日有成交日线股数（≥99.5% 视为完整）",
            "cells": _build_cells(minute_maps["60"], lambda d: stock_daily_map.get(d, active_stock_count), ok_threshold=0.995),
        },
        {
            "id": "index_daily",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": f"核心指数日线 ({core_idx_n}只)",
            "note": "分母 = 正式维护的 11 大核心权益指数",
            "cells": _build_cells(
                {d: len(s) for d, s in idx_daily_sets.items()},
                lambda _: core_idx_n,
                ok_threshold=1.0,
                missing_items_fn=_missing_from_universe(idx_daily_sets, CORE_INDEX_NAMES),
            ),
        },
        {
            "id": "index_5m",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": f"核心指数 5分钟 ({core_idx_n}只)",
            "note": "分母 = 11 大核心权益指数",
            "cells": _build_cells(
                {d: len(s) for d, s in idx_minute_sets["5"].items()},
                lambda _: core_idx_n,
                ok_threshold=1.0,
                missing_items_fn=_missing_from_universe(idx_minute_sets["5"], CORE_INDEX_NAMES),
            ),
        },
        {
            "id": "index_15m",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": f"核心指数 15分钟 ({core_idx_n}只)",
            "note": "分母 = 11 大核心权益指数",
            "cells": _build_cells(
                {d: len(s) for d, s in idx_minute_sets["15"].items()},
                lambda _: core_idx_n,
                ok_threshold=1.0,
                missing_items_fn=_missing_from_universe(idx_minute_sets["15"], CORE_INDEX_NAMES),
            ),
        },
        {
            "id": "index_30m",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": f"核心指数 30分钟 ({core_idx_n}只)",
            "note": "分母 = 11 大核心权益指数",
            "cells": _build_cells(
                {d: len(s) for d, s in idx_minute_sets["30"].items()},
                lambda _: core_idx_n,
                ok_threshold=1.0,
                missing_items_fn=_missing_from_universe(idx_minute_sets["30"], CORE_INDEX_NAMES),
            ),
        },
        {
            "id": "index_60m",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": f"核心指数 60分钟 ({core_idx_n}只)",
            "note": "分母 = 11 大核心权益指数",
            "cells": _build_cells(
                {d: len(s) for d, s in idx_minute_sets["60"].items()},
                lambda _: core_idx_n,
                ok_threshold=1.0,
                missing_items_fn=_missing_from_universe(idx_minute_sets["60"], CORE_INDEX_NAMES),
            ),
        },
        {
            "id": "sector_daily",
            "group": "market",
            "group_label": "A 股行情、核心指数与行业板块",
            "label": f"通达信板块日线 ({expected_sectors}个)",
            "note": f"通达信标准行业/概念/风格/地区板块官方指数日线（分母 = {expected_sectors} 个）",
            "cells": _build_cells(sec_map, lambda _: expected_sectors, ok_threshold=0.95),
        },
    ]

    return {
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "days": days,
        "dates": dates,
        "core_indices": list(CORE_INDICES),
        "core_etfs": sorted(etf_core_names.keys()),
        "datasets": datasets,
    }


def _inspect_etf(code: str, etf_name: str, asset_class: str, dates: list[str], now_bj: datetime) -> dict[str, Any]:
    """针对宽基/红利/国债/黄金/跨境 ETF 执行日线（及国家队份额日线）完整性体检。"""
    start_date = dates[0] if dates else "2026-11-01"
    with ThreadPoolExecutor(max_workers=4) as executor:
        fut_daily_agg = executor.submit(
            _safe_query,
            "SELECT uniqExact(trade_date), toString(min(trade_date)), toString(max(trade_date)) FROM stock.etf_kline_daily FINAL WHERE code = {c:String}",
            {"c": code},
            "etf_daily_agg",
        )
        fut_daily_rec = executor.submit(
            _safe_query,
            "SELECT toString(trade_date) AS d, uniqExact(trade_date) AS cnt FROM stock.etf_kline_daily FINAL WHERE code = {c:String} AND trade_date >= toDate({s:String}) GROUP BY d",
            {"c": code, "s": start_date},
            "etf_daily_rec",
        )
        fut_share_agg = executor.submit(
            _safe_query,
            "SELECT uniqExact(trade_date), toString(min(trade_date)), toString(max(trade_date)) FROM stock.ar4_etf_share_daily FINAL WHERE code = {c:String}",
            {"c": code},
            "etf_share_agg",
        )
        fut_share_rec = executor.submit(
            _safe_query,
            "SELECT toString(trade_date) AS d, uniqExact(trade_date) AS cnt FROM stock.ar4_etf_share_daily FINAL WHERE code = {c:String} AND trade_date >= toDate({s:String}) GROUP BY d",
            {"c": code, "s": start_date},
            "etf_share_rec",
        )
        daily_agg = fut_daily_agg.result()
        daily_rec = fut_daily_rec.result()
        share_agg = fut_share_agg.result()
        share_rec = fut_share_rec.result()

    daily_total = int(daily_agg[0][0]) if daily_agg and daily_agg[0][0] else 0
    daily_min = str(daily_agg[0][1]) if daily_agg and daily_total > 0 else None
    daily_max = str(daily_agg[0][2]) if daily_agg and daily_total > 0 else None
    daily_map = {str(r[0]): int(r[1]) for r in daily_rec}

    daily_ok = sum(1 for d in dates if daily_map.get(d, 0) >= 1)
    periods: list[dict[str, Any]] = [
        {
            "period": "daily",
            "label": "ETF 日线 (etf_kline_daily)",
            "expected_per_day": 1,
            "total_bars": daily_total,
            "min_time": daily_min,
            "max_time": daily_max,
            "recent_ok_days": daily_ok,
            "recent_total_days": len(dates),
            "complete": daily_ok == len(dates) and len(dates) > 0,
        }
    ]

    share_total = int(share_agg[0][0]) if share_agg and share_agg[0][0] else 0
    share_map = {str(r[0]): int(r[1]) for r in share_rec}
    has_share = share_total > 0 or code in AR4_NATIONAL_TEAM_ETFS
    if has_share:
        today_str = now_bj.strftime("%Y-%m-%d")
        share_eval_dates = [d for d in dates if d < today_str] or dates
        share_ok = sum(1 for d in share_eval_dates if share_map.get(d, 0) >= 1)
        periods.append(
            {
                "period": "share",
                "label": "国家队份额日线 (T+1)",
                "expected_per_day": 1,
                "total_bars": share_total,
                "min_time": str(share_agg[0][1]) if share_agg and share_total > 0 else None,
                "max_time": str(share_agg[0][2]) if share_agg and share_total > 0 else None,
                "recent_ok_days": share_ok,
                "recent_total_days": len(share_eval_dates),
                "complete": share_ok == len(share_eval_dates) and len(share_eval_dates) > 0,
            }
        )

    rows = [
        {
            "date": d,
            "daily": daily_map.get(d, 0),
            "share": share_map.get(d, 0) if has_share else None,
            "all_ok": daily_map.get(d, 0) >= 1,
        }
        for d in reversed(dates)
    ]

    cls_label = ETF_ASSET_CLASS_LABELS.get(asset_class, asset_class or "ETF")
    return {
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "code": code,
        "name": etf_name,
        "type": f"etf · {cls_label}",
        "is_etf": True,
        "has_share": has_share,
        "days": len(dates),
        "periods": periods,
        "daily_rows": rows,
    }


def inspect_kline(raw_code: str, days: int = 10) -> dict[str, Any]:
    """对单只股票、核心指数或宽基/国债/黄金/跨境 ETF 执行全周期 K 线完整性体检。"""
    code = _normalize_code(raw_code)
    days = max(5, min(30, int(days)))
    now_bj = datetime.now(BJ_TZ)
    after_postclose = (now_bj.hour, now_bj.minute) >= (17, 30)
    date_op = "<=" if after_postclose else "<"

    # 1 & 2. 并发查询 stocks 主表、etf_kline_daily 与近 N 个已收盘交易日
    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_info = executor.submit(
            _query,
            "SELECT anyLast(name), anyLast(type), anyLast(listing_status) FROM stock.stocks FINAL WHERE code = {c:String}",
            {"c": code},
        )
        fut_etf = executor.submit(
            _safe_query,
            "SELECT anyLast(name), anyLast(asset_class), count() FROM stock.etf_kline_daily FINAL WHERE code = {c:String}",
            {"c": code},
            "inspect_etf_meta",
        )
        fut_cal = executor.submit(
            _query,
            f"""
            SELECT toString(trade_date) AS d
            FROM stock.trade_calendar
            WHERE market = 'SH' AND is_trading = 1 AND trade_date {date_op} today()
            ORDER BY trade_date DESC
            LIMIT {{n:UInt32}}
            """,
            {"n": days},
        )
        info_rows = fut_info.result()
        etf_rows = fut_etf.result()
        cal_rows = fut_cal.result()

    dates = [str(r[0]) for r in reversed(cal_rows)]

    # 若命中跨资产/宽基/国债 ETF 表，优先走 ETF 体检逻辑（不误报分钟线缺槽）
    if etf_rows and int(etf_rows[0][2] or 0) > 0:
        etf_name = str(etf_rows[0][0] or CORE_CROSS_ASSET_ETFS.get(code, code))
        asset_class = str(etf_rows[0][1] or "")
        return _inspect_etf(code, etf_name, asset_class, dates, now_bj)

    name = str(info_rows[0][0]) if info_rows and info_rows[0][0] else code
    sec_type = str(info_rows[0][1]) if info_rows and info_rows[0][1] else "unknown"
    start_date = dates[0] if dates else "2026-11-01"

    # 3. 并发检查股票/指数 5 个周期（日线/5m/15m/30m/60m）
    periods_spec = [
        ("daily", "日线", "kline_daily", "trade_date", 1),
        ("5m", "5分钟", "kline_minute_5", "datetime", 48),
        ("15m", "15分钟", "kline_minute_15", "datetime", 16),
        ("30m", "30分钟", "kline_minute_30", "datetime", 8),
        ("60m", "60分钟", "kline_minute_60", "datetime", 4),
    ]

    def _inspect_period(spec: tuple[str, str, str, str, int]) -> tuple[dict[str, Any], dict[str, int]]:
        key, label, tbl, _col, expected_bars = spec
        if key == "daily":
            agg = _query(
                f"SELECT uniqExact(trade_date), toString(min(trade_date)), toString(max(trade_date)) FROM stock.{tbl} WHERE code = {{c:String}}",
                {"c": code},
            )
            recent = _query(
                f"SELECT toString(trade_date) AS d, uniqExact(trade_date) AS cnt FROM stock.{tbl} WHERE code = {{c:String}} AND trade_date >= toDate({{s:String}}) GROUP BY d",
                {"c": code, "s": start_date},
            )
        else:
            agg = _query(
                f"SELECT uniqExact(datetime), toString(min(datetime)), toString(max(datetime)) FROM stock.{tbl} WHERE code = {{c:String}}",
                {"c": code},
            )
            recent = _query(
                f"SELECT toString(toDate(datetime)) AS d, uniqExact(datetime) AS cnt FROM stock.{tbl} WHERE code = {{c:String}} AND datetime >= toDateTime('{{s}} 00:00:00') GROUP BY d".replace("{s}", start_date),
                {"c": code},
            )

        total_bars = int(agg[0][0]) if agg and agg[0][0] else 0
        min_t = str(agg[0][1]) if agg and total_bars > 0 else None
        max_t = str(agg[0][2]) if agg and total_bars > 0 else None
        rec_map = {str(r[0]): int(r[1]) for r in recent}

        ok_days = 0
        counts_for_dates: dict[str, int] = {}
        for d in dates:
            cnt = rec_map.get(d, 0)
            counts_for_dates[d] = cnt
            if cnt >= expected_bars:
                ok_days += 1

        summary = {
            "period": key,
            "label": label,
            "expected_per_day": expected_bars,
            "total_bars": total_bars,
            "min_time": min_t,
            "max_time": max_t,
            "recent_ok_days": ok_days,
            "recent_total_days": len(dates),
            "complete": ok_days == len(dates) and len(dates) > 0,
        }
        return summary, counts_for_dates

    period_summaries: list[dict[str, Any]] = []
    daily_grid: dict[str, dict[str, int]] = {d: {} for d in dates}

    with ThreadPoolExecutor(max_workers=5) as executor:
        for summary, counts_for_dates in executor.map(_inspect_period, periods_spec):
            key = summary["period"]
            period_summaries.append(summary)
            for d, cnt in counts_for_dates.items():
                daily_grid[d][key] = cnt

    rows = [
        {
            "date": d,
            "daily": daily_grid[d].get("daily", 0),
            "5m": daily_grid[d].get("5m", 0),
            "15m": daily_grid[d].get("15m", 0),
            "30m": daily_grid[d].get("30m", 0),
            "60m": daily_grid[d].get("60m", 0),
            "all_ok": (
                daily_grid[d].get("daily", 0) >= 1
                and daily_grid[d].get("5m", 0) >= 48
                and daily_grid[d].get("15m", 0) >= 16
                and daily_grid[d].get("30m", 0) >= 8
                and daily_grid[d].get("60m", 0) >= 4
            ),
        }
        for d in reversed(dates)
    ]

    return {
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "code": code,
        "name": name,
        "type": sec_type,
        "is_etf": False,
        "has_share": False,
        "days": len(dates),
        "periods": period_summaries,
        "daily_rows": rows,
    }


def get_runtime_health_summary() -> dict[str, Any]:
    """基于 ClickHouse 物理表生成实时运行健康摘要（含 A 股、核心指数与 G3 实战核心 ETF/宏观/南向/国家队份额）。"""
    now_bj = datetime.now(BJ_TZ)
    matrix = get_health_matrix(days=5, force_refresh=False)
    datasets = matrix.get("datasets", [])
    # sector_daily 与扩展 etf_all_daily 为辅助池；实战核心 14 只 ETF / 国家队份额 / 宏观 / 南向 / 股票 / 指数全部参与主闸门体检
    non_blocking_ids = {"sector_daily", "etf_all_daily"}
    core_datasets = [d for d in datasets if d.get("id") not in non_blocking_ids]

    latest_date = ""
    components: list[dict[str, Any]] = []
    all_ok = True
    for ds in core_datasets:
        cells = [c for c in ds.get("cells", []) if c.get("status") not in ("pending", "optional")]
        if not cells:
            continue
        last_cell = cells[-1]
        if ds.get("id") == "stock_daily" and last_cell.get("date"):
            latest_date = str(last_cell["date"])
        elif not latest_date and last_cell.get("date"):
            latest_date = str(last_cell["date"])
        st = last_cell.get("status")
        healthy = st == "ok"
        if not healthy:
            all_ok = False
        components.append({
            "name": ds.get("id", ""),
            "label": ds.get("label", ds.get("id", "")),
            "status": "healthy" if healthy else ("degraded" if st == "warn" else "blocked"),
            "date": last_cell.get("date"),
            "coverage": last_cell.get("coverage"),
            "actual": last_cell.get("actual"),
            "expected": last_cell.get("expected"),
            "missing_items": last_cell.get("missing_items") or [],
        })

    return {
        "schema_version": 3,
        "source": "live_clickhouse",
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "latest_trade_date": latest_date,
        "publisher_status": "healthy",
        "status": "healthy" if all_ok else "degraded",
        "strategy_actionable": all_ok,
        "summary_text": (
            f"最新交易日 {latest_date}，A 股、核心指数与 G3 实战 ETF/宏观/国家队数据全周期完整"
            if all_ok and latest_date
            else f"最新交易日 {latest_date or '未知'} 存在数据缺口或滞后，详见左侧「数据健康与监控」"
        ),
        "components": components,
    }
