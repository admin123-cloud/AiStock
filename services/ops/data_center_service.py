"""数据中心统一查询服务（直连 ClickHouse 物理表，零旧视图/旧缓存误报）。

提供三大核心能力：
  1. get_data_overview()       — ClickHouse 各核心表存储占用、总行数、时间跨度与资产池统计
  2. get_health_matrix(days)   — 交易日交付矩阵（股票日线/5m/15m/30m/60m + 11大核心指数 + 行业板块）
  3. inspect_kline(code, days) — 单标的/单指数全周期 K 线完整性槽位体检（日线=1, 5m=48, 15m=16, 30m=8, 60m=4）
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import clickhouse_connect

from utils.common.logger import get_logger

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

_MATRIX_CACHE: dict[int, dict[str, Any]] = {}
_OVERVIEW_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_CACHE_TTL = 60.0


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
    c = _client()
    try:
        return list(c.query(sql, parameters=params or {}).result_rows)
    finally:
        c.close()


def _normalize_code(raw: str) -> str:
    s = raw.strip().upper()
    if "." in s:
        return s
    if len(s) == 6 and s.isdigit():
        if s.startswith(("6", "9")):
            return f"{s}.SH"
        if s.startswith(("8", "4")):
            return f"{s}.BJ"
        return f"{s}.SZ"
    return s


def get_data_overview(*, force_refresh: bool = False) -> dict[str, Any]:
    """查询 ClickHouse 核心行情与基础表的真实存储占用、行数与时间覆盖范围。"""
    now_mono = time.monotonic()
    if not force_refresh and _OVERVIEW_CACHE["payload"] is not None and now_mono < float(_OVERVIEW_CACHE["expires_at"]):
        return _OVERVIEW_CACHE["payload"]

    now_bj = datetime.now(BJ_TZ)
    target_tables = [
        ("kline_daily", "股票/指数日线", "trade_date"),
        ("kline_minute_5", "5分钟 K 线", "datetime"),
        ("kline_minute_15", "15分钟 K 线", "datetime"),
        ("kline_minute_30", "30分钟 K 线", "datetime"),
        ("kline_minute_60", "60分钟 K 线", "datetime"),
        ("sector_kline_daily", "行业板块日线", "trade_date"),
        ("stocks", "证券主数据", "updated_at"),
        ("trade_calendar", "交易日历", "trade_date"),
    ]

    # 1. 从 system.parts 读取每张表的物理行数与磁盘占用（毫秒级）
    parts_rows = _query(
        """
        SELECT
            table,
            sum(rows) AS total_rows,
            sum(bytes_on_disk) AS disk_bytes
        FROM system.parts
        WHERE database = 'stock' AND active = 1
        GROUP BY table
        """
    )
    parts_map = {str(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in parts_rows}

    # 2. 证券池计数
    pool_rows = _query(
        """
        SELECT
            countIf(type = 'stock' AND quit = 0 AND listing_status = 'active') AS active_stocks,
            countIf(type = 'index') AS total_indices,
            (SELECT count() FROM stock.sectors WHERE type = 'industry') AS industry_sectors
        FROM stock.stocks FINAL
        """
    )
    active_stocks, total_indices, industry_sectors = (
        (int(pool_rows[0][0]), int(pool_rows[0][1]), int(pool_rows[0][2]))
        if pool_rows
        else (0, 0, 0)
    )

    # 3. 逐表查 min/max 时间范围
    table_stats: list[dict[str, Any]] = []
    total_rows_all = 0
    total_bytes_all = 0
    latest_daily_date = None

    for tbl, label, time_col in target_tables:
        rows_cnt, disk_bytes = parts_map.get(tbl, (0, 0))
        total_rows_all += rows_cnt
        total_bytes_all += disk_bytes
        min_t, max_t = None, None
        if rows_cnt > 0:
            try:
                r = _query(f"SELECT toString(min({time_col})), toString(max({time_col})) FROM stock.{tbl}")
                if r:
                    min_t, max_t = str(r[0][0]), str(r[0][1])
            except Exception as exc:
                logger.warning("table %s min/max failed: %s", tbl, exc)
        if tbl == "kline_daily" and max_t:
            latest_daily_date = max_t[:10]
        table_stats.append({
            "table": tbl,
            "label": label,
            "rows": rows_cnt,
            "size_mb": round(disk_bytes / (1024 * 1024), 2),
            "min_time": min_t,
            "max_time": max_t,
        })

    payload = {
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "latest_daily_date": latest_daily_date,
        "summary": {
            "active_stocks": active_stocks,
            "core_indices": len(CORE_INDICES),
            "total_indices_in_catalog": total_indices,
            "industry_sectors": industry_sectors,
            "total_rows": total_rows_all,
            "total_size_gb": round(total_bytes_all / (1024 ** 3), 2),
        },
        "tables": table_stats,
    }
    _OVERVIEW_CACHE["payload"] = payload
    _OVERVIEW_CACHE["expires_at"] = now_mono + _CACHE_TTL
    return payload


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


def get_health_matrix(days: int = 10, *, force_refresh: bool = False) -> dict[str, Any]:
    """实时计算近 N 个交易日的真实数据交付矩阵（科学分母、零假警报）。"""
    days = max(5, min(30, int(days)))
    now_mono = time.monotonic()
    cached = _MATRIX_CACHE.get(days)
    if not force_refresh and cached and now_mono < float(cached["expires_at"]):
        return cached["payload"]

    now_bj = datetime.now(BJ_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")
    after_postclose = (now_bj.hour, now_bj.minute) >= (17, 30)

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

    # 2. 在市股票总数作为日线分母
    active_stock_rows = _query(
        "SELECT count() FROM stock.stocks FINAL WHERE type = 'stock' AND quit = 0 AND listing_status = 'active'"
    )
    active_stock_count = int(active_stock_rows[0][0]) if active_stock_rows else 5570

    # 3. 股票日线每日覆盖数（作为当日分钟线的科学分母！）
    stock_daily_rows = _query(
        """
        SELECT toString(trade_date) AS d, uniqExact(code) AS cnt
        FROM stock.kline_daily
        WHERE trade_date >= toDate({s:String})
          AND code IN (SELECT code FROM stock.stocks FINAL WHERE type = 'stock' AND quit = 0 AND listing_status = 'active')
        GROUP BY d
        """,
        {"s": start_date},
    )
    stock_daily_map = {str(r[0]): int(r[1]) for r in stock_daily_rows}

    # 4. 股票分钟线（5/15/30/60m）每日覆盖数（仅统计在市股票，精准对标当日日线）
    minute_maps: dict[str, dict[str, int]] = {}
    idx_minute_maps: dict[str, dict[str, int]] = {}
    for p in ("5", "15", "30", "60"):
        tbl = f"kline_minute_{p}"
        s_rows = _query(
            f"""
            SELECT toString(toDate(datetime)) AS d, uniqExact(code) AS cnt
            FROM stock.{tbl}
            WHERE datetime >= toDateTime('{start_date} 00:00:00')
              AND code IN (SELECT code FROM stock.stocks FINAL WHERE type = 'stock' AND quit = 0 AND listing_status = 'active')
            GROUP BY d
            """
        )
        minute_maps[p] = {str(r[0]): int(r[1]) for r in s_rows}

        i_rows = _query(
            f"""
            SELECT toString(toDate(datetime)) AS d, uniqExact(code) AS cnt
            FROM stock.{tbl}
            WHERE datetime >= toDateTime('{start_date} 00:00:00')
              AND code IN ({core_idx_sql})
            GROUP BY d
            """
        )
        idx_minute_maps[p] = {str(r[0]): int(r[1]) for r in i_rows}

    # 5. 11 大核心指数日线
    idx_daily_rows = _query(
        f"""
        SELECT toString(trade_date) AS d, uniqExact(code) AS cnt
        FROM stock.kline_daily
        WHERE trade_date >= toDate('{start_date}')
          AND code IN ({core_idx_sql})
        GROUP BY d
        """
    )
    idx_daily_map = {str(r[0]): int(r[1]) for r in idx_daily_rows}

    # 6. 通达信标准板块日线（行业/概念/风格/地区）
    total_sectors_rows = _query("SELECT count() FROM stock.sectors FINAL")
    expected_sectors = int(total_sectors_rows[0][0]) if total_sectors_rows and int(total_sectors_rows[0][0]) > 0 else 604
    sec_rows = _query(
        f"""
        SELECT toString(trade_date) AS d, uniqExact(code) AS cnt
        FROM stock.sector_kline_daily FINAL
        WHERE trade_date >= toDate('{start_date}')
        GROUP BY d
        """
    )
    sec_map = {str(r[0]): int(r[1]) for r in sec_rows}

    def _build_cells(actual_map: dict[str, int], expected_fn, ok_threshold: float = 0.99) -> list[dict[str, Any]]:
        cells = []
        for d in dates:
            is_pending = (d == today_str and not after_postclose) or (d > today_str)
            exp = expected_fn(d)
            act = actual_map.get(d, 0)
            # 若今天尚未到 17:30 盘后完成但数据已经落库，也如实展示
            if is_pending and act == 0:
                cov = None
                st = "pending"
            else:
                cov = round(min(100.0, (act / exp) * 100.0), 1) if exp > 0 else None
                st = _cell_status(act, exp, False, ok_threshold=ok_threshold)
            cells.append({
                "date": d,
                "actual": act,
                "expected": exp,
                "coverage": cov,
                "missing": max(0, exp - act),
                "status": st,
            })
        return cells

    core_idx_n = len(CORE_INDICES)
    datasets = [
        {
            "id": "stock_daily",
            "label": "股票日线",
            "note": f"分母 = 在市股票 {active_stock_count} 只（≥99% 视为完整，容忍日常停牌）",
            "cells": _build_cells(stock_daily_map, lambda _: active_stock_count, ok_threshold=0.99),
        },
        {
            "id": "stock_5m",
            "label": "股票 5分钟",
            "note": "分母 = 当日有成交日线股数（≥99.5% 视为完整）",
            "cells": _build_cells(minute_maps["5"], lambda d: stock_daily_map.get(d, active_stock_count), ok_threshold=0.995),
        },
        {
            "id": "stock_15m",
            "label": "股票 15分钟",
            "note": "分母 = 当日有成交日线股数（≥99.5% 视为完整）",
            "cells": _build_cells(minute_maps["15"], lambda d: stock_daily_map.get(d, active_stock_count), ok_threshold=0.995),
        },
        {
            "id": "stock_30m",
            "label": "股票 30分钟",
            "note": "分母 = 当日有成交日线股数（≥99.5% 视为完整）",
            "cells": _build_cells(minute_maps["30"], lambda d: stock_daily_map.get(d, active_stock_count), ok_threshold=0.995),
        },
        {
            "id": "stock_60m",
            "label": "股票 60分钟",
            "note": "分母 = 当日有成交日线股数（≥99.5% 视为完整）",
            "cells": _build_cells(minute_maps["60"], lambda d: stock_daily_map.get(d, active_stock_count), ok_threshold=0.995),
        },
        {
            "id": "index_daily",
            "label": f"核心指数日线 ({core_idx_n}只)",
            "note": "分母 = 正式维护的 11 大核心权益指数",
            "cells": _build_cells(idx_daily_map, lambda _: core_idx_n, ok_threshold=1.0),
        },
        {
            "id": "index_5m",
            "label": f"核心指数 5分钟 ({core_idx_n}只)",
            "note": "分母 = 11 大核心权益指数",
            "cells": _build_cells(idx_minute_maps["5"], lambda _: core_idx_n, ok_threshold=1.0),
        },
        {
            "id": "index_15m",
            "label": f"核心指数 15分钟 ({core_idx_n}只)",
            "note": "分母 = 11 大核心权益指数",
            "cells": _build_cells(idx_minute_maps["15"], lambda _: core_idx_n, ok_threshold=1.0),
        },
        {
            "id": "index_30m",
            "label": f"核心指数 30分钟 ({core_idx_n}只)",
            "note": "分母 = 11 大核心权益指数",
            "cells": _build_cells(idx_minute_maps["30"], lambda _: core_idx_n, ok_threshold=1.0),
        },
        {
            "id": "index_60m",
            "label": f"核心指数 60分钟 ({core_idx_n}只)",
            "note": "分母 = 11 大核心权益指数",
            "cells": _build_cells(idx_minute_maps["60"], lambda _: core_idx_n, ok_threshold=1.0),
        },
        {
            "id": "sector_daily",
            "label": f"通达信板块日线 ({expected_sectors}个)",
            "note": f"通达信标准行业/概念/风格/地区板块官方指数日线（分母 = {expected_sectors} 个）",
            "cells": _build_cells(sec_map, lambda _: expected_sectors, ok_threshold=0.95),
        },
    ]

    payload = {
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "days": days,
        "dates": dates,
        "core_indices": list(CORE_INDICES),
        "datasets": datasets,
    }
    _MATRIX_CACHE[days] = {"expires_at": now_mono + _CACHE_TTL, "payload": payload}
    return payload


def inspect_kline(raw_code: str, days: int = 10) -> dict[str, Any]:
    """对单只股票或指数执行全周期（日线/5m/15m/30m/60m）K线槽位完整性体检。"""
    code = _normalize_code(raw_code)
    days = max(5, min(30, int(days)))
    now_bj = datetime.now(BJ_TZ)

    # 1. 查询标的名称与类型
    info_rows = _query(
        "SELECT anyLast(name), anyLast(type), anyLast(listing_status) FROM stock.stocks FINAL WHERE code = {c:String}",
        {"c": code},
    )
    name = str(info_rows[0][0]) if info_rows and info_rows[0][0] else code
    sec_type = str(info_rows[0][1]) if info_rows and info_rows[0][1] else "unknown"

    # 2. 近 N 个交易日
    cal_rows = _query(
        """
        SELECT toString(trade_date) AS d
        FROM stock.trade_calendar
        WHERE market = 'SH' AND is_trading = 1 AND trade_date < today()
        ORDER BY trade_date DESC
        LIMIT {n:UInt32}
        """,
        {"n": days},
    )
    dates = [str(r[0]) for r in reversed(cal_rows)]
    start_date = dates[0] if dates else "2026-11-01"

    # 3. 全周期总览（总根数、起止时间、近 N 日每日槽数）
    periods_spec = [
        ("daily", "日线", "kline_daily", "trade_date", 1),
        ("5m", "5分钟", "kline_minute_5", "datetime", 48),
        ("15m", "15分钟", "kline_minute_15", "datetime", 16),
        ("30m", "30分钟", "kline_minute_30", "datetime", 8),
        ("60m", "60分钟", "kline_minute_60", "datetime", 4),
    ]

    period_summaries: list[dict[str, Any]] = []
    daily_grid: dict[str, dict[str, int]] = {d: {} for d in dates}

    for key, label, tbl, col, expected_bars in periods_spec:
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
        for d in dates:
            cnt = rec_map.get(d, 0)
            daily_grid[d][key] = cnt
            if cnt >= expected_bars:
                ok_days += 1

        period_summaries.append({
            "period": key,
            "label": label,
            "expected_per_day": expected_bars,
            "total_bars": total_bars,
            "min_time": min_t,
            "max_time": max_t,
            "recent_ok_days": ok_days,
            "recent_total_days": len(dates),
            "complete": ok_days == len(dates) and len(dates) > 0,
        })

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
        "days": len(dates),
        "periods": period_summaries,
        "daily_rows": rows,
    }


def get_runtime_health_summary() -> dict[str, Any]:
    """基于 ClickHouse 物理表生成实时运行健康摘要（供顶部 RuntimeBanner 与 /api/health/runtime 使用）。"""
    now_bj = datetime.now(BJ_TZ)
    matrix = get_health_matrix(days=3, force_refresh=False)
    datasets = matrix.get("datasets", [])
    core_datasets = [d for d in datasets if d.get("id") != "sector_daily"]

    latest_date = ""
    components: list[dict[str, Any]] = []
    all_ok = True
    for ds in core_datasets:
        cells = [c for c in ds.get("cells", []) if c.get("status") not in ("pending", "optional")]
        if not cells:
            continue
        last_cell = cells[-1]
        if not latest_date and last_cell.get("date"):
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
        })

    return {
        "schema_version": 2,
        "source": "live_clickhouse",
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "latest_trade_date": latest_date,
        "publisher_status": "healthy",
        "status": "healthy" if all_ok else "degraded",
        "strategy_actionable": all_ok,
        "summary_text": (
            f"最新交易日 {latest_date}，股票与核心指数全周期覆盖完整"
            if all_ok and latest_date
            else f"最新交易日 {latest_date or '未知'} 存在部分数据缺口，详见数据健康与监控"
        ),
        "components": components,
    }
