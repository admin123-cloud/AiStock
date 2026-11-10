# -*- coding: utf-8 -*-
"""R28（2026-11-10）：东财实时快照读侧服务（operations_preview 读链）。

数据来源：scripts/intraday_snapshot_collect.py（pipeline_scheduler 盘中任务，
交易日 09:35~15:05 每 5min）写入的两张运营层表（非 kline_* 正式表）：

  - stock.intraday_market_snapshot  全市场个股明细（DateTime('Asia/Shanghai')，TTL 30d）
  - stock.intraday_index_snapshot   冻结 10 大盘指数（同上）

读侧契约（任务书 20261110_intraday_eastmoney_r28.md）：
  1. /api/market/sentiment 盘中读最新快照（涨跌/涨跌停/成交额合计）；
     涨跌停分类 = R20b 既有板块阈值逻辑（market_overview._limit_up_threshold：
     ST 4.8 / 北交所 29.8 / 创业板·科创板 19.8 / 其余 9.8，对称取负）作用于实时 pct；
  2. /api/market/indices 盘中叠加快照指数点位/涨跌幅（保留昨收基线字段）；
  3. /api/market/emotion-trend 盘中最新日序列点改用快照实时值；
  4. fail-closed：快照不可用/过期（>600s）→ 一律返回 None，调用方回退
     昨收正式日线并明示 data_date；本模块绝不造数。

全部查询 market_warehouse raw=True 基表直查（R20b 教训：不经 SQLAlchemy session、
不碰 FINAL 视图）；snapshot 列已标注 DateTime('Asia/Shanghai')，驱动读回 aware
北京墙钟（R26 §4 实测），新鲜度判断用北京钟减法，不再做 ±8h 补丁。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from utils.logger import get_logger
from utils.market_warehouse import (
    clickhouse_available,
    clickhouse_query_df,
    clickhouse_table_exists,
)

logger = get_logger("intraday_market_snapshot")

SNAPSHOT_TABLE = "intraday_market_snapshot"
INDEX_TABLE = "intraday_index_snapshot"

#: 快照过期阈值：>10min 视为不可用（fail-closed 回退昨收）
FRESH_WINDOW_SEC = 600.0
#: 时钟偏差容忍（读侧与应用主机秒级偏差；负值=快照来自"未来"仍接受小偏移）
FUTURE_TOLERANCE_SEC = 120.0
#: 覆盖率门禁：明细行数 / stocks 主档 type='stock' 数低于该值视为部分快照，拒绝
MIN_COVERAGE_RATIO = 0.90
#: 绝对下限（防主档异常时的兜底；镜像 MIN_EMOTION_CYCLE_STOCKS 量级）
MIN_COVERED_COUNT = 1000

SOURCE_TAG = "eastmoney:clist+ulist"

# 涨跌停分类阈值（与 api.market_overview._limit_up_threshold 同规则，作用于实时 pct；
# 确认日线（R20b kline_daily 路径）沿用扁平 9.9/-9.9 旧口径，两口径差异已在任务书声明）
_LIMIT_THRESHOLD_SQL = """
multiIf(
    upperUTF8(name) LIKE 'ST%' OR upperUTF8(name) LIKE '*ST%', 4.8,
    endsWith(code, '.BJ'), 29.8,
    code LIKE '688%' OR code LIKE '300%' OR code LIKE '301%', 19.8,
    9.8
)
"""


def _table_ready(table: str) -> bool:
    return clickhouse_available() and clickhouse_table_exists(table)


def _coerce_beijing_dt(value: Any) -> Optional[datetime]:
    """把驱动读回的 snapshot_ts 统一成 aware 北京时间。

    标注列正常读回 aware 北京墙（R26 §4）；对 naive（驱动降级路径）按北京墙
    补 +08:00；字符串按 '%Y-%m-%d %H:%M:%S' 解析；不可解析 → None。
    """
    from zoneinfo import ZoneInfo

    cn = ZoneInfo("Asia/Shanghai")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=cn)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(value.strip(), fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=cn)
            except ValueError:
                continue
    if value is not None and hasattr(value, "to_pydatetime"):
        try:
            return _coerce_beijing_dt(value.to_pydatetime())
        except Exception:  # noqa: BLE001
            return None
    return None


def _now_beijing() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _freshness_seconds(ts: Any, now: Optional[datetime] = None) -> Optional[float]:
    """快照时间距今秒数（北京钟）。ts 无效/不可解析 → None。"""
    parsed = _coerce_beijing_dt(ts)
    if parsed is None:
        return None
    now = now or _now_beijing()
    if now.tzinfo is None:
        from zoneinfo import ZoneInfo

        now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return (now - parsed).total_seconds()


def is_fresh(ts: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """快照新鲜度：0s 前 ~ FRESH_WINDOW_SEC 内（容忍 FUTURE_TOLERANCE_SEC 未来偏移）。"""
    delta = _freshness_seconds(ts, now)
    if delta is None:
        return False
    return -FUTURE_TOLERANCE_SEC <= delta <= FRESH_WINDOW_SEC


def latest_snapshot_ts(now: Optional[datetime] = None) -> Optional[datetime]:
    """明细表最新快照时间（轻量标量；供 sentiment 缓存键绑定新快照即时失效）。"""
    if not _table_ready(SNAPSHOT_TABLE):
        return None
    try:
        df = clickhouse_query_df(
            f"""
            SELECT max(snapshot_ts) AS latest_ts
            FROM {SNAPSHOT_TABLE}
            WHERE snapshot_ts >= now('Asia/Shanghai') - INTERVAL 2 DAY
            """,
            raw=True,
        )
        if df is None or df.empty:
            return None
        return df.iloc[0].get("latest_ts")
    except Exception as exc:  # noqa: BLE001 - 读侧任何失败都回退，不冒泡
        logger.warning(f"latest_snapshot_ts query failed: {exc}")
        return None


def _expected_stock_count() -> int:
    try:
        df = clickhouse_query_df(
            "SELECT count() AS cnt FROM stocks WHERE type = 'stock'",
            raw=True,
        )
        if df is None or df.empty:
            return 0
        return int(df.iloc[0].get("cnt") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"intraday snapshot expected stock count failed: {exc}")
        return 0


def latest_market_sentiment_intraday(
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """/market/sentiment 盘中覆盖层：最新快照的涨跌/涨跌停/成交额合计。

    返回形状对齐 _load_qmt_intraday_market_sentiment 的 overlay dict（date/as_of/
    covered_count/.../change_distribution/source），market_overview 既有合并块
    直接消费；附加 mode/turnover_source 两个键供合并块标注来源。
    任何不新鲜/覆盖不足/查询异常 → None（fail-closed，调用方回退昨收口径）。
    """
    if not _table_ready(SNAPSHOT_TABLE):
        return None
    try:
        summary_df = clickhouse_query_df(
            f"""
            SELECT
                max(snapshot_ts) AS latest_ts,
                count() AS covered_count,
                countIf(change_pct > 0) AS up_count,
                countIf(change_pct < 0) AS down_count,
                countIf(change_pct = 0) AS unchanged_count,
                countIf(change_pct >= 5) AS up_5_count,
                countIf(change_pct <= -5) AS down_5_count,
                countIf(change_pct >= {_LIMIT_THRESHOLD_SQL}) AS limit_up_count,
                countIf(change_pct <= -{_LIMIT_THRESHOLD_SQL}) AS limit_down_count,
                avg(change_pct) AS avg_change_percent,
                sum(amount) AS total_amount,
                sumIf(amount, endsWith(k.code, '.SH')) AS sh_amount,
                sumIf(amount, endsWith(k.code, '.SZ')) AS sz_amount,
                sumIf(amount, endsWith(k.code, '.BJ')) AS bj_amount,
                countIf(change_pct >= 7) AS bucket_up_7,
                countIf(change_pct >= 5 AND change_pct < 7) AS bucket_up_5_7,
                countIf(change_pct >= 3 AND change_pct < 5) AS bucket_up_3_5,
                countIf(change_pct > 0 AND change_pct < 3) AS bucket_up_0_3,
                countIf(change_pct = 0) AS bucket_zero,
                countIf(change_pct < 0 AND change_pct > -3) AS bucket_down_0_3,
                countIf(change_pct <= -3 AND change_pct > -5) AS bucket_down_3_5,
                countIf(change_pct <= -5 AND change_pct > -7) AS bucket_down_5_7,
                countIf(change_pct <= -7) AS bucket_down_7
            FROM {SNAPSHOT_TABLE} AS k
            INNER JOIN stocks AS s ON s.code = k.code
            WHERE s.type = 'stock'
              AND k.price > 0
              AND k.snapshot_ts = (
                  SELECT max(snapshot_ts) FROM {SNAPSHOT_TABLE}
                  WHERE snapshot_ts >= now('Asia/Shanghai') - INTERVAL 2 DAY
              )
            """,
            raw=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"intraday market sentiment snapshot query failed: {exc}")
        return None
    if summary_df is None or summary_df.empty:
        return None

    row = summary_df.iloc[0]
    latest_ts = row.get("latest_ts")
    if not is_fresh(latest_ts, now):
        logger.info(
            "intraday market snapshot stale or missing (latest=%s), fail-closed fallback",
            latest_ts,
        )
        return None
    latest_dt = _coerce_beijing_dt(latest_ts)
    covered_count = int(row.get("covered_count") or 0)
    expected_count = _expected_stock_count()
    coverage_ratio = covered_count / expected_count if expected_count else 0.0
    if covered_count < MIN_COVERED_COUNT or (
        expected_count and coverage_ratio < MIN_COVERAGE_RATIO
    ):
        logger.warning(
            "intraday market snapshot coverage insufficient: covered=%d expected=%d ratio=%.3f",
            covered_count, expected_count, coverage_ratio,
        )
        return None

    def count(name: str) -> int:
        return int(row.get(name) or 0)

    distribution = [
        {"label": ">=7", "count": count("bucket_up_7"), "side": "up"},
        {"label": "5~7", "count": count("bucket_up_5_7"), "side": "up"},
        {"label": "3~5", "count": count("bucket_up_3_5"), "side": "up"},
        {"label": "0~3", "count": count("bucket_up_0_3"), "side": "up"},
        {"label": "0", "count": count("bucket_zero"), "side": "flat"},
        {"label": "-3~0", "count": count("bucket_down_0_3"), "side": "down"},
        {"label": "-5~-3", "count": count("bucket_down_3_5"), "side": "down"},
        {"label": "-7~-5", "count": count("bucket_down_5_7"), "side": "down"},
        {"label": "<=-7", "count": count("bucket_down_7"), "side": "down"},
    ]
    return {
        "date": latest_dt.strftime("%Y-%m-%d"),
        "as_of": latest_dt.strftime("%Y-%m-%d %H:%M"),
        "snapshot_ts": latest_dt,
        "covered_count": covered_count,
        "expected_count": expected_count,
        "coverage_ratio": round(coverage_ratio, 4),
        "up_count": count("up_count"),
        "down_count": count("down_count"),
        "unchanged_count": count("unchanged_count"),
        "up_5_percent_count": count("up_5_count"),
        "down_5_percent_count": count("down_5_count"),
        "limit_up_count": count("limit_up_count"),
        "limit_down_count": count("limit_down_count"),
        "avg_change_percent": round(float(row.get("avg_change_percent") or 0.0), 3),
        "total_turnover": float(row.get("total_amount") or 0.0),
        "sh_amount": float(row.get("sh_amount") or 0.0),
        "sz_amount": float(row.get("sz_amount") or 0.0),
        "bj_amount": float(row.get("bj_amount") or 0.0),
        "change_distribution": distribution,
        "source": SOURCE_TAG,
        "mode": "eastmoney_snapshot",
        "turnover_source": "eastmoney_snapshot_aggregate",
    }


def latest_index_quotes(
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """/market/indices 盘中覆盖层：最新快照的指数点位（昨收基线保留）。

    返回 (code -> overlay dict, status)。overlay 形状对齐既有 QMT 覆盖层
    （price/pre_close/volume/amount/snapshot_date/snapshot_time/source），
    _compute_market_indices 的合并逻辑零改动消费。
    快照过期/缺失 → ({}, {"status": "unavailable"|"stale", ...})（fail-closed）。
    """
    if not _table_ready(INDEX_TABLE):
        return {}, {"status": "unavailable", "reason": "missing_table"}
    try:
        df = clickhouse_query_df(
            f"""
            SELECT code, name, price, change_pct, change_amount, prev_close,
                   volume, amount, snapshot_ts
            FROM {INDEX_TABLE}
            WHERE snapshot_ts = (
                SELECT max(snapshot_ts) FROM {INDEX_TABLE}
                WHERE snapshot_ts >= now('Asia/Shanghai') - INTERVAL 2 DAY
            )
            """,
            raw=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"intraday index snapshot query failed: {exc}")
        return {}, {"status": "unavailable", "reason": f"query_error:{exc}"}
    if df is None or df.empty:
        return {}, {"status": "unavailable", "reason": "no_rows"}

    latest_ts = df.iloc[0].get("snapshot_ts")
    if not is_fresh(latest_ts, now):
        return {}, {"status": "stale", "as_of": str(latest_ts)}
    latest_dt = _coerce_beijing_dt(latest_ts)

    quotes: Dict[str, Dict[str, Any]] = {}
    for row in df.itertuples(index=False):
        row_ts = _coerce_beijing_dt(row.snapshot_ts) or latest_dt
        quotes[str(row.code)] = {
            "price": float(row.price) if row.price is not None else None,
            "pre_close": float(row.prev_close) if row.prev_close is not None else None,
            "change_pct": float(row.change_pct) if row.change_pct is not None else None,
            "volume": float(row.volume) if row.volume is not None else None,
            "amount": float(row.amount) if row.amount is not None else None,
            "snapshot_date": row_ts.date(),
            "snapshot_time": row_ts,
            "source": "eastmoney_live_snapshot",
        }
    status = {
        "status": "live",
        "as_of": latest_dt.strftime("%Y-%m-%d %H:%M"),
        "quote_count": len(quotes),
        "source": SOURCE_TAG,
    }
    return quotes, status


def emotion_live_point(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """/market/emotion-trend 盘中最新日序列点（形状对齐 _intraday_emotion_snapshot）。

    口径与旧快照函数一致（prev-close pct：涨=pct>0 占比、strong=>3、weak=<-3；
    intraday_up_rate 以实时 pct 近似——快照表无盘中高低路径）。不新鲜/异常 → None。
    """
    if not _table_ready(SNAPSHOT_TABLE):
        return None
    try:
        df = clickhouse_query_df(
            f"""
            SELECT
                max(snapshot_ts) AS latest_ts,
                count() AS total_stocks,
                avg(change_pct > 0) * 100 AS close_up_rate,
                avgIf(change_pct > 0, endsWith(k.code, '.SH')) * 100 AS sh_up_rate,
                avgIf(change_pct > 0, endsWith(k.code, '.SZ')) * 100 AS sz_up_rate,
                avgIf(change_pct > 0, k.code LIKE '300%.SZ' OR k.code LIKE '301%.SZ') * 100 AS cyb_up_rate,
                avg(change_pct > 3) * 100 AS strong_up_rate,
                avg(change_pct < -3) * 100 AS weak_up_rate
            FROM {SNAPSHOT_TABLE} AS k
            INNER JOIN stocks AS s ON s.code = k.code
            WHERE s.type = 'stock'
              AND k.price > 0
              AND k.snapshot_ts = (
                  SELECT max(snapshot_ts) FROM {SNAPSHOT_TABLE}
                  WHERE snapshot_ts >= now('Asia/Shanghai') - INTERVAL 2 DAY
              )
            """,
            raw=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"emotion live point snapshot query failed: {exc}")
        return None
    if df is None or df.empty:
        return None
    row = df.iloc[0]
    latest_ts = row.get("latest_ts")
    if not is_fresh(latest_ts, now):
        return None
    latest_dt = _coerce_beijing_dt(latest_ts)
    total_stocks = int(row.get("total_stocks") or 0)
    if total_stocks < MIN_COVERED_COUNT:
        return None

    def metric(name: str) -> float:
        return round(float(row.get(name) or 0.0), 2)

    return {
        "date": latest_dt.strftime("%Y-%m-%d"),
        "as_of": latest_dt.strftime("%Y-%m-%d %H:%M"),
        "total_stocks": total_stocks,
        "close_up_rate": metric("close_up_rate"),
        "intraday_up_rate": metric("close_up_rate"),
        "sh_up_rate": metric("sh_up_rate"),
        "sz_up_rate": metric("sz_up_rate"),
        "cyb_up_rate": metric("cyb_up_rate"),
        "strong_up_rate": metric("strong_up_rate"),
        "weak_up_rate": metric("weak_up_rate"),
        "is_temporary": True,
        "source": f"clickhouse:{SNAPSHOT_TABLE}",
    }
