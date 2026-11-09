"""
大盘概览API - 首页数据展示
"""

from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta, timezone
import json
import os
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from sqlalchemy import and_, case, desc, func, or_, text

from models.stock_models import EmotionCycle, KlineDaily, Stock
from services.market_turnover_forecast import (
    get_cached_intraday_turnover_forecast,
    get_intraday_turnover_forecast,
)
from services.margin_sentiment import load_margin_sentiment
from utils.database import db
from utils.logger import get_logger
from utils.daily_quality_read import finite_number
from utils.market_warehouse import (
    clickhouse_available,
    clickhouse_table_exists,
    clickhouse_available,
    clickhouse_query_df,
    clickhouse_scalar,
)

router = APIRouter(prefix="/market", tags=["大盘概览"])
logger = get_logger("market_overview")
CORE_INDEX_CODES = {"000001.SH", "399001.SZ"}
DEFAULT_INDEX_JUMP_THRESHOLD = 20.0
CORE_INDEX_JUMP_THRESHOLD = 15.0
MIN_EMOTION_CYCLE_STOCKS = 1000
MIN_QMT_INTRADAY_COVERAGE_RATIO = 0.90
_QMT_INTRADAY_SENTIMENT_CACHE: Dict[str, Any] = {"expires_at": 0.0, "trade_date": "", "payload": None}
# The home page must never silently switch to public AkShare/Sina quotes.  The
# host bridge is a read-only proxy for the same QMT quote service used by the
# intraday collector.  Keep the alias while the stock master still contains
# the legacy Shanghai composite code.
HOME_LIVE_INDEX_CODES = {"999999.SH", "000001.SH", "000680.SH", "399001.SZ", "399006.SZ"}
QMT_INDEX_CODE_ALIASES = {"999999.SH": "000001.SH"}
# R13（2026-11-09）：QMT 主机桥不可达时的进程内负缓存——60 秒内不再重试，避免前端
# ~30 秒轮询对不在线的桥反复撞出 503 连串；桥恢复后成功响应会清除缓存。
_QMT_BRIDGE_UNREACHABLE_CACHE: Dict[str, Any] = {"expires_at": 0.0, "reason": ""}
_QMT_BRIDGE_UNREACHABLE_CACHE_SECONDS = 60.0
# R13（2026-11-09）：/market/indices 结果的进程内 TTL 缓存（monotonic 时钟，按 code
# 参数分键）；缓存命中直接返回，避免每次轮询都重跑 date_counts 聚合与 QMT 桥探测。
_MARKET_INDICES_RESULT_CACHE: Dict[str, Dict[str, Any]] = {}
_MARKET_INDICES_RESULT_CACHE_TTL_SECONDS = 45.0
# R20（2026-11-09，Owner 指令）：/market/sentiment 数据源改为 kline_daily 按需
# 重算（停读 market_sentiment_snapshot 预计算快照——刷新调度器已停用，首页 date
# 停在 2026-10-30），缓存分档：盘中（Asia/Shanghai 交易日 09:15~15:00）TTL=300s
# 每 5 分钟重算一次；盘后/非交易时段缓存键绑定 kline_daily 的 max(trade_date)
# （raw 轻量查询、免 FINAL），数据日期不变即命中，当天补数完成后首个请求触发
# 重算一次。/market/margin-sentiment（60 秒，键 days）、/market/emotion-trend
# （60 秒，键 days|granularity）维持 R15 的 TTL 缓存不变；error 载荷不缓存。
_MARKET_SENTIMENT_RESULT_CACHE: Dict[str, Dict[str, Any]] = {}
_SENTIMENT_INTRADAY_TTL_SECONDS = 300.0
_MARGIN_SENTIMENT_RESULT_CACHE: Dict[str, Dict[str, Any]] = {}
_MARGIN_SENTIMENT_RESULT_CACHE_TTL_SECONDS = 60.0
_EMOTION_TREND_RESULT_CACHE: Dict[str, Dict[str, Any]] = {}
_EMOTION_TREND_RESULT_CACHE_TTL_SECONDS = 60.0


def _intraday_emotion_snapshot() -> Optional[Dict[str, Any]]:
    """Build a provisional, same-day breadth snapshot from QMT facts.

    This intentionally stays out of ``emotion_cycle``: that table is the
    after-close, confirmed daily series.  The snapshot is only appended to the
    API response while the current trade day has sufficiently broad 5-minute
    coverage, so the home page can show the live state without treating it as a
    confirmed close.
    """
    if not clickhouse_available() or not _has_clickhouse_table("kline_daily_intraday"):
        return None

    trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    try:
        snapshot_df = clickhouse_query_df(
            """
            WITH intraday_latest AS (
                SELECT
                    code,
                    argMax(close, snapshot_at) AS last_close,
                    argMax(previous_close, snapshot_at) AS previous_close,
                    max(snapshot_at) AS as_of
                FROM kline_daily_intraday FINAL
                WHERE trade_date = ?
                GROUP BY code
            )
            SELECT
                count() AS total_stocks,
                max(i.as_of) AS as_of,
                avg(i.last_close > i.previous_close) * 100 AS close_up_rate,
                avgIf(i.last_close > i.previous_close, s.market = 'SH') * 100 AS sh_up_rate,
                avgIf(i.last_close > i.previous_close, s.market = 'SZ') * 100 AS sz_up_rate,
                avgIf(i.last_close > i.previous_close, i.code LIKE '300%%.SZ' OR i.code LIKE '301%%.SZ') * 100 AS cyb_up_rate,
                avg((i.last_close - i.previous_close) / i.previous_close * 100 > 3) * 100 AS strong_up_rate,
                avg((i.last_close - i.previous_close) / i.previous_close * 100 < -3) * 100 AS weak_up_rate
            FROM intraday_latest AS i
            INNER JOIN stocks AS s FINAL ON s.code = i.code
            WHERE s.type = 'stock' AND i.last_close > 0 AND i.previous_close > 0
            """,
            [trade_date],
        )
    except Exception as exc:
        logger.warning(f"intraday emotion snapshot query failed: {exc}")
        return None

    if snapshot_df is None or snapshot_df.empty:
        return None
    row = snapshot_df.iloc[0]
    total_stocks = int(row.get("total_stocks") or 0)
    as_of = row.get("as_of")
    if total_stocks < MIN_EMOTION_CYCLE_STOCKS or as_of is None:
        return None

    def metric(name: str) -> float:
        return round(float(row.get(name) or 0.0), 2)

    return {
        "date": trade_date.isoformat(),
        "as_of": _datetime_text(as_of),
        "total_stocks": total_stocks,
        "close_up_rate": metric("close_up_rate"),
        "intraday_up_rate": metric("close_up_rate"),
        "sh_up_rate": metric("sh_up_rate"),
        "sz_up_rate": metric("sz_up_rate"),
        "cyb_up_rate": metric("cyb_up_rate"),
        "strong_up_rate": metric("strong_up_rate"),
        "weak_up_rate": metric("weak_up_rate"),
        "is_temporary": True,
        "source": "clickhouse:kline_daily_intraday",
    }


def _append_intraday_emotion_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Append today's provisional point to a confirmed daily trend payload."""
    snapshot = _intraday_emotion_snapshot()
    if not snapshot or snapshot["date"] in payload.get("dates", []):
        return payload

    payload["dates"].append(snapshot["date"])
    live_fields = {
        "close_up_rate",
        "intraday_up_rate",
        "sh_up_rate",
        "sz_up_rate",
        "cyb_up_rate",
        "strong_up_rate",
        "weak_up_rate",
    }
    for field in (
        "close_up_rate",
        "intraday_up_rate",
        "sh_up_rate",
        "sz_up_rate",
        "cyb_up_rate",
        "yesterday_monster_up_rate",
        "yesterday_strong_up_rate",
        "yesterday_weak_up_rate",
        "strong_up_rate",
        "weak_up_rate",
        "limit_up_follow_rate",
    ):
        series = payload.setdefault(field, [])
        value = snapshot[field] if field in live_fields else (series[-1] if series else 0.0)
        series.append(float(value))

    payload["total"] = len(payload["dates"])
    payload["intraday_snapshot"] = snapshot
    return payload


def _resolve_table_name(base: str, candidates: Optional[List[str]] = None) -> str:
    names = candidates or [base, f"{base}_live"]
    if not clickhouse_available():
        return names[0]
    for name in names:
        try:
            if clickhouse_table_exists(name):
                return name
        except Exception:
            continue
    return names[0]


def _has_clickhouse_table(table_name: str) -> bool:
    if not clickhouse_available():
        return True
    try:
        return clickhouse_table_exists(table_name)
    except Exception:
        return False


def _latest_daily_trade_date_from_calendar():
    if not clickhouse_available():
        return None
    kline_table = _resolve_table_name("kline_daily")
    if not _has_clickhouse_table(kline_table) or not _has_clickhouse_table("trade_calendar"):
        return None
    try:
        return clickhouse_scalar(
            """
            SELECT MAX(k.trade_date)
            FROM {kline_table} k
            JOIN trade_calendar c
              ON c.trade_date = k.trade_date
             AND c.market = 'SH'
             AND c.is_trading = 1
            """.format(kline_table=kline_table)
        )
    except Exception as exc:
        logger.warning(f"latest daily trade date calendar query failed: {exc}")
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _limit_up_threshold(stock: Stock) -> float:
    code = (stock.code or "").upper()
    name = (stock.name or "").upper()
    market = (stock.market or "").upper()

    if stock.st or name.startswith("ST") or name.startswith("*ST"):
        return 4.8
    if market == "BJ" or code.startswith("8") or code.startswith("4"):
        return 29.8
    if code.startswith("688") or code.startswith("300") or code.startswith("301"):
        return 19.8
    return 9.8


def _is_limit_up_day(stock: Stock, row: Any) -> bool:
    if row is None:
        return False

    change_pct = finite_number(getattr(row, 'change_pct', None))
    return change_pct is not None and change_pct >= _limit_up_threshold(stock)


def _format_phase_reason(reasons: List[str]) -> str:
    if not reasons:
        return ""
    return "；".join(reasons[:4])


def _sentiment_level(score: float) -> str:
    if score >= 80:
        return "极度乐观"
    if score >= 65:
        return "乐观"
    if score >= 55:
        return "偏乐观"
    if score >= 45:
        return "中性"
    if score >= 35:
        return "偏悲观"
    if score >= 20:
        return "悲观"
    return "极度悲观"


def _date_text(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _datetime_text(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value).replace("T", " ")
    return text[:16]


def _latest_qmt_home_index_quotes(codes: List[str]) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Read the home-page indices from the read-only QMT host bridge.

    The old implementation read ``intraday_quote_snapshot``, whose writer is
    AkShare/Sina.  Do not fall back to that table: a missing QMT quote must be
    visible as unavailable rather than presented as a real-time quote.
    """
    requested = [str(code).upper() for code in codes if str(code).upper() in HOME_LIVE_INDEX_CODES]
    if not requested:
        return {}, {"provider": "qmt_xtdata", "status": "not_requested"}

    qmt_codes = [QMT_INDEX_CODE_ALIASES.get(code, code) for code in requested]
    bridge_url = str(
        os.getenv("AISTOCK_QMT_TICK_BRIDGE_URL", "http://host.docker.internal:8766/full-tick")
    ).strip()
    if not bridge_url:
        return {}, {"provider": "qmt_xtdata", "status": "unavailable", "reason": "bridge_url_missing"}

    # R13：桥不可达负缓存命中——60 秒窗口内直接返回不可用，不再发起网络请求。
    if time.monotonic() < float(_QMT_BRIDGE_UNREACHABLE_CACHE.get("expires_at") or 0.0):
        return {}, {
            "provider": "qmt_xtdata",
            "status": "unavailable",
            "reason": f"bridge_negative_cache: {_QMT_BRIDGE_UNREACHABLE_CACHE.get('reason') or 'unreachable'}",
        }

    try:
        separator = "&" if "?" in bridge_url else "?"
        # R13：urllib 的单一 timeout 同时约束 connect 与 read，压到 2 秒，避免对不在线
        # 的主机桥长时间阻塞首页请求（原为 5 秒）。
        with urlopen(f"{bridge_url}{separator}{urlencode({'codes': ','.join(qmt_codes)})}", timeout=2) as response:
            body = json.loads(response.read().decode("utf-8"))
        raw_ticks = body.get("ticks") if isinstance(body, dict) and body.get("ok") else None
        if not isinstance(raw_ticks, dict):
            raise RuntimeError(str((body or {}).get("message") or "invalid_qmt_bridge_response"))
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning(f"QMT home index quote unavailable: {reason}")
        # R13：任何"桥不可用"结果（连接失败/超时/无效响应）都进入 60 秒负缓存。
        _QMT_BRIDGE_UNREACHABLE_CACHE.update(
            {
                "expires_at": time.monotonic() + _QMT_BRIDGE_UNREACHABLE_CACHE_SECONDS,
                "reason": reason,
            }
        )
        return {}, {"provider": "qmt_xtdata", "status": "unavailable", "reason": reason}
    # R13：桥可用——清除负缓存，后续失败可被立即感知。
    _QMT_BRIDGE_UNREACHABLE_CACHE.update({"expires_at": 0.0, "reason": ""})

    quotes: Dict[str, Dict[str, Any]] = {}
    for requested_code, qmt_code in zip(requested, qmt_codes):
        tick = raw_ticks.get(qmt_code) or raw_ticks.get(requested_code)
        if not isinstance(tick, dict):
            continue
        price = _to_float(tick.get("lastPrice") or tick.get("last") or tick.get("price"))
        if price <= 0:
            continue
        try:
            tick_time = datetime.fromtimestamp(float(tick.get("time")) / 1000, tz=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
        except Exception:
            tick_time = datetime.now(ZoneInfo("Asia/Shanghai"))
        quotes[requested_code] = {
            "snapshot_date": tick_time.date(),
            "snapshot_time": tick_time,
            "price": price,
            "pre_close": _to_float(tick.get("lastClose") or tick.get("preClose")),
            "volume": _to_float(tick.get("pvolume") or tick.get("volume")),
            "amount": _to_float(tick.get("amount")),
            "source": "qmt_xtdata_host_bridge",
        }

    status = "live" if quotes else "unavailable"
    return quotes, {
        "provider": "qmt_xtdata",
        "status": status,
        "quote_count": len(quotes),
        "requested_count": len(requested),
        "as_of": max((_datetime_text(item["snapshot_time"]) for item in quotes.values()), default=None),
    }


def _latest_qmt_intraday_index_quotes(codes: List[str]) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Read today's QMT index facts when the host quote bridge is unavailable."""
    requested = [str(code).upper() for code in codes if str(code).upper() in HOME_LIVE_INDEX_CODES]
    if not requested or not clickhouse_available() or not _has_clickhouse_table("kline_daily_intraday"):
        return {}, {"provider": "qmt_clickhouse", "status": "unavailable"}

    trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    source_codes = [QMT_INDEX_CODE_ALIASES.get(code, code) for code in requested]
    placeholders = ",".join(["?"] * len(source_codes))
    try:
        snapshot_df = clickhouse_query_df(
            f"""
            SELECT
                code,
                argMax(close, snapshot_at) AS price,
                argMax(previous_close, snapshot_at) AS pre_close,
                argMax(volume, snapshot_at) AS volume,
                argMax(amount, snapshot_at) AS amount,
                max(snapshot_at) AS snapshot_time
            FROM kline_daily_intraday FINAL
            WHERE trade_date = ? AND code IN ({placeholders})
            GROUP BY code
            """,
            [trade_date] + source_codes,
        )
    except Exception as exc:
        logger.warning(f"QMT intraday index fact query failed: {exc}")
        return {}, {
            "provider": "qmt_clickhouse",
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    quotes: Dict[str, Dict[str, Any]] = {}
    if snapshot_df is not None and not snapshot_df.empty:
        source_to_requested = {source: requested[index] for index, source in enumerate(source_codes)}
        for row in snapshot_df.itertuples(index=False):
            requested_code = source_to_requested.get(str(row.code).upper())
            price = _to_float(row.price)
            pre_close = _to_float(row.pre_close)
            if not requested_code or price <= 0 or pre_close <= 0:
                continue
            quotes[requested_code] = {
                "snapshot_date": trade_date,
                "snapshot_time": row.snapshot_time,
                "price": price,
                "pre_close": pre_close,
                "volume": _to_float(row.volume),
                "amount": _to_float(row.amount),
                "source": "qmt_clickhouse_intraday",
            }

    return quotes, {
        "provider": "qmt_clickhouse",
        "status": "live" if quotes else "unavailable",
        "quote_count": len(quotes),
        "requested_count": len(requested),
        "as_of": max((_datetime_text(item["snapshot_time"]) for item in quotes.values()), default=None),
    }


def _load_qmt_intraday_market_sentiment() -> Optional[Dict[str, Any]]:
    """Aggregate the latest completed QMT five-minute bars for the home page.

    This is intentionally a read from ``kline_minute_5`` rather than from the
    legacy AkShare/Sina snapshot table.  The cache is shorter than the source
    cadence and prevents several simultaneous homepage requests from repeating
    the full-market aggregation.
    """
    trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    cached = _QMT_INTRADAY_SENTIMENT_CACHE.get("payload")
    if (
        cached is not None
        and _QMT_INTRADAY_SENTIMENT_CACHE.get("trade_date") == trade_date.isoformat()
        and time.monotonic() < float(_QMT_INTRADAY_SENTIMENT_CACHE.get("expires_at") or 0)
    ):
        return cached

    def finish(payload: Optional[Dict[str, Any]], cache_seconds: int = 75) -> Optional[Dict[str, Any]]:
        _QMT_INTRADAY_SENTIMENT_CACHE.update(
            {
                "trade_date": trade_date.isoformat(),
                "payload": payload,
                "expires_at": time.monotonic() + cache_seconds,
            }
        )
        return payload

    if not clickhouse_available() or not _has_clickhouse_table("kline_minute_5"):
        return finish(None, 60)

    try:
        expected_count = int(
            clickhouse_scalar("SELECT count() FROM stocks WHERE type = 'stock'", raw=True) or 0
        )
        previous_date = clickhouse_scalar(
            "SELECT max(trade_date) FROM trade_calendar"
            " WHERE is_trading = 1 AND market = 'SH' AND trade_date < ?",
            [trade_date],
            raw=True,
        )
        if expected_count <= 0 or not previous_date:
            return finish(None, 60)
        # R20b：镜像 R14 `_overlay_fresh_intraday_list_kline` 的 argMax(created_at)
        # 等价 FINAL 去重改法，raw=True 基表直查 kline_minute_5；谓词不用
        # toDate(datetime)=?（实测在 toYYYYMM(datetime) 分区上完全不裁剪，
        # 894M 行/4.2GiB 全表扫描），改 datetime 范围谓词（分区裁剪到月分区）。
        # latest 与 turnover 合并为单次去重扫描（同一 (code, datetime) 内层
        # argMax 出 close/amount，外层一次 GROUP BY code 得三值），比 R14 的
        # 两路各扫一次减半读量；外层别名避开 as_of（CH 别名代入会触发
        # ILLEGAL_AGGREGATION，R14 原版形状在该引擎版本上即因此抛错）。
        # previous_close 直查 kline_daily 基表并以 argMax(close, created_at) 去重。
        summary_df = clickhouse_query_df(
            """
            WITH minute_bars AS (
                SELECT
                    code,
                    argMax(close, as_of) AS close,
                    max(as_of) AS latest_as_of,
                    sum(amount) AS amount
                FROM (
                    SELECT
                        code,
                        datetime AS as_of,
                        argMax(close, created_at) AS close,
                        argMax(amount, created_at) AS amount
                    FROM kline_minute_5
                    WHERE datetime >= ? AND datetime < ? + INTERVAL 1 DAY
                    GROUP BY code, datetime
                )
                GROUP BY code
            ),
            previous_close AS (
                SELECT code, argMax(close, created_at) AS close
                FROM kline_daily
                WHERE trade_date = ?
                GROUP BY code
            ),
            base AS (
                SELECT
                    s.code AS code,
                    s.market AS market,
                    b.close AS close,
                    p.close AS previous_close,
                    b.latest_as_of AS as_of,
                    b.amount AS amount,
                    (b.close - p.close) / nullIf(p.close, 0) * 100 AS pct
                FROM stocks AS s
                INNER JOIN minute_bars AS b ON b.code = s.code
                INNER JOIN previous_close AS p ON p.code = s.code
                WHERE s.type = 'stock' AND b.close > 0 AND p.close > 0
            )
            SELECT
                count() AS covered_count,
                max(as_of) AS as_of,
                sum(pct > 0) AS up_count,
                sum(pct < 0) AS down_count,
                sum(pct = 0) AS unchanged_count,
                sum(pct >= 5) AS up_5_count,
                sum(pct <= -5) AS down_5_count,
                sum(pct >= 9.9) AS limit_up_count,
                sum(pct <= -9.9) AS limit_down_count,
                avg(pct) AS avg_change_percent,
                sum(amount) AS total_amount,
                sumIf(amount, market = 'SH') AS sh_amount,
                sumIf(amount, market = 'SZ') AS sz_amount,
                sumIf(amount, market = 'BJ') AS bj_amount,
                sum(pct >= 7) AS bucket_up_7,
                sum(pct >= 5 AND pct < 7) AS bucket_up_5_7,
                sum(pct >= 3 AND pct < 5) AS bucket_up_3_5,
                sum(pct > 0 AND pct < 3) AS bucket_up_0_3,
                sum(pct = 0) AS bucket_zero,
                sum(pct < 0 AND pct > -3) AS bucket_down_0_3,
                sum(pct <= -3 AND pct > -5) AS bucket_down_3_5,
                sum(pct <= -5 AND pct > -7) AS bucket_down_5_7,
                sum(pct <= -7) AS bucket_down_7
            FROM base
            """,
            [trade_date, trade_date, previous_date],
            raw=True,
        )
    except Exception as exc:
        logger.warning(f"QMT intraday market sentiment query failed: {exc}")
        return finish(None, 60)

    if summary_df is None or summary_df.empty:
        return finish(None, 60)
    row = summary_df.iloc[0]
    covered_count = int(row.get("covered_count") or 0)
    as_of = row.get("as_of")
    coverage_ratio = covered_count / expected_count if expected_count else 0.0
    if as_of is None or covered_count <= 0 or coverage_ratio < MIN_QMT_INTRADAY_COVERAGE_RATIO:
        logger.warning(
            "QMT intraday market sentiment coverage insufficient: "
            f"covered={covered_count}, expected={expected_count}, ratio={coverage_ratio:.3f}"
        )
        return finish(None, 60)

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
    return finish(
        {
            "date": trade_date.isoformat(),
            "as_of": _datetime_text(as_of),
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
            "source": "qmt:kline_minute_5",
        }
    )


def _empty_emotion_trend(message: str = "") -> Dict[str, Any]:
    payload = {
        "dates": [],
        "close_up_rate": [],
        "intraday_up_rate": [],
        "sh_up_rate": [],
        "sz_up_rate": [],
        "cyb_up_rate": [],
        "yesterday_monster_up_rate": [],
        "yesterday_strong_up_rate": [],
        "yesterday_weak_up_rate": [],
        "strong_up_rate": [],
        "weak_up_rate": [],
        "limit_up_follow_rate": [],
    }
    if message:
        payload["error"] = message
    return payload


def _build_emotion_phase(
    *,
    trade_date: Any,
    sentiment_score: float,
    up_count: int,
    down_count: int,
    unchanged_count: int,
    up_5_count: int,
    down_5_count: int,
    limit_up_count: int,
    limit_down_count: int,
    latest_cycle: Optional[EmotionCycle],
    previous_cycle: Optional[EmotionCycle],
) -> Dict[str, Any]:
    total = max(1, up_count + down_count + unchanged_count)
    up_ratio = up_count / total * 100
    down_ratio = down_count / total * 100
    breadth = (up_count - down_count) / total * 100
    strong_spread = (up_5_count - down_5_count) / total * 100
    limit_spread = (limit_up_count - limit_down_count) / total * 100
    down_5_ratio = down_5_count / total * 100
    close_up_rate = _to_float(getattr(latest_cycle, "close_up_rate", 0.0))
    strong_up_rate = _to_float(getattr(latest_cycle, "strong_up_rate", 0.0))
    limit_up_follow_rate = _to_float(getattr(latest_cycle, "limit_up_follow_rate", 0.0))
    cycle_date = getattr(latest_cycle, "date", None)

    prev_close_up = _to_float(getattr(previous_cycle, "close_up_rate", close_up_rate), close_up_rate)
    prev_strong_up = _to_float(getattr(previous_cycle, "strong_up_rate", strong_up_rate), strong_up_rate)
    delta_close_up = close_up_rate - prev_close_up
    delta_strong_up = strong_up_rate - prev_strong_up

    phase_code = "start"
    phase_name = "启动期"
    action_hint = "轻仓试错，确认主线后再逐步加仓。"
    risk_hint = "信号仍在构建期，避免一次性重仓。"
    reasons: List[str] = []
    confidence_hits = 0

    is_ice = (
        (sentiment_score <= 30 and close_up_rate <= 45)
        or (down_ratio >= 65 and delta_close_up <= 0)
        or (breadth <= -25 and down_5_ratio >= 2.8)
    )
    is_euphoria = (
        sentiment_score >= 78
        and close_up_rate >= 68
        and strong_up_rate >= 35
        and limit_up_follow_rate >= 58
    )
    is_decline = (
        (delta_close_up <= -4 and close_up_rate < 60)
        or (sentiment_score < 52 and delta_close_up < 0 and limit_up_follow_rate < 50)
        or (strong_spread < 0 and limit_spread < 0 and sentiment_score < 56)
    )
    is_ferment = (
        sentiment_score >= 60
        and close_up_rate >= 55
        and strong_up_rate >= 22
        and delta_close_up >= -1
    )

    if is_ice:
        phase_code = "ice"
        phase_name = "冰点期(股灾)"
        action_hint = "按计划分批低吸，出现系统性恐慌时可加大买入力度。"
        risk_hint = "优先控制仓位节奏，避免抄底过早。"
        if sentiment_score <= 30:
            reasons.append(f"情绪分数偏低({sentiment_score:.1f})")
            confidence_hits += 1
        if down_ratio >= 60:
            reasons.append(f"下跌家数占比偏高({down_ratio:.1f}%)")
            confidence_hits += 1
        if close_up_rate <= 45:
            reasons.append(f"收盘上涨率偏低({close_up_rate:.1f}%)")
            confidence_hits += 1
        if delta_close_up <= 0:
            reasons.append(f"短期动量未转强({delta_close_up:.1f})")
            confidence_hits += 1
    elif is_euphoria:
        phase_code = "euphoria"
        phase_name = "高潮期"
        action_hint = "分批止盈，降低追高频率，保留核心仓位。"
        risk_hint = "高波动区间，防止情绪反转。"
        if sentiment_score >= 78:
            reasons.append(f"情绪分数高位({sentiment_score:.1f})")
            confidence_hits += 1
        if close_up_rate >= 68:
            reasons.append(f"收盘上涨率高位({close_up_rate:.1f}%)")
            confidence_hits += 1
        if strong_up_rate >= 35:
            reasons.append(f"强势上涨率较高({strong_up_rate:.1f}%)")
            confidence_hits += 1
        if limit_up_follow_rate >= 58:
            reasons.append(f"涨停溢价较高({limit_up_follow_rate:.1f}%)")
            confidence_hits += 1
    elif is_decline:
        phase_code = "decline"
        phase_name = "退潮期"
        action_hint = "控制回撤，减仓弱势品种，等待下一轮启动信号。"
        risk_hint = "退潮阶段容易出现连锁回撤，避免频繁逆势加仓。"
        if delta_close_up <= -4:
            reasons.append(f"收盘上涨率回落({delta_close_up:.1f})")
            confidence_hits += 1
        if delta_strong_up <= -2:
            reasons.append(f"强势上涨率回落({delta_strong_up:.1f})")
            confidence_hits += 1
        if limit_up_follow_rate < 50:
            reasons.append(f"涨停溢价走弱({limit_up_follow_rate:.1f}%)")
            confidence_hits += 1
        if sentiment_score < 56:
            reasons.append(f"情绪分数回落({sentiment_score:.1f})")
            confidence_hits += 1
    elif is_ferment:
        phase_code = "ferment"
        phase_name = "发酵期"
        action_hint = "持股为主，围绕主线做结构优化。"
        risk_hint = "避免追涨过度，保留机动仓位。"
        if sentiment_score >= 60:
            reasons.append(f"情绪分数维持强势({sentiment_score:.1f})")
            confidence_hits += 1
        if close_up_rate >= 55:
            reasons.append(f"收盘上涨率较高({close_up_rate:.1f}%)")
            confidence_hits += 1
        if strong_up_rate >= 22:
            reasons.append(f"强势上涨率抬升({strong_up_rate:.1f}%)")
            confidence_hits += 1
        if delta_close_up >= -1:
            reasons.append(f"短期动量稳定({delta_close_up:.1f})")
            confidence_hits += 1
    else:
        if 38 <= sentiment_score <= 58:
            reasons.append(f"情绪分数处于启动区间({sentiment_score:.1f})")
            confidence_hits += 1
        if 42 <= close_up_rate <= 58:
            reasons.append(f"收盘上涨率进入修复区间({close_up_rate:.1f}%)")
            confidence_hits += 1
        if delta_close_up >= 2:
            reasons.append(f"短期动量改善({delta_close_up:.1f})")
            confidence_hits += 1
        if limit_spread >= 0:
            reasons.append(f"涨停-跌停扩散为正({limit_spread:.2f})")
            confidence_hits += 1

    confidence = min(0.95, 0.45 + confidence_hits * 0.12)
    is_stale = bool(cycle_date and trade_date and cycle_date != trade_date)

    return {
        "phase_code": phase_code,
        "phase_name": phase_name,
        "confidence": round(confidence, 2),
        "action_hint": action_hint,
        "risk_hint": risk_hint,
        "reason_summary": _format_phase_reason(reasons),
        "metrics": {
            "up_ratio": round(up_ratio, 2),
            "down_ratio": round(down_ratio, 2),
            "breadth": round(breadth, 2),
            "strong_spread": round(strong_spread, 2),
            "limit_spread": round(limit_spread, 2),
            "close_up_rate": round(close_up_rate, 2),
            "strong_up_rate": round(strong_up_rate, 2),
            "limit_up_follow_rate": round(limit_up_follow_rate, 2),
            "delta_close_up_rate": round(delta_close_up, 2),
            "delta_strong_up_rate": round(delta_strong_up, 2),
        },
        "data_status": {
            "emotion_cycle_date": cycle_date.isoformat() if cycle_date else None,
            "trade_date": trade_date.isoformat() if trade_date else None,
            "is_stale": is_stale,
        },
    }


@router.get("/indices-legacy")
def _legacy_get_market_indices(code: Optional[str] = None):
    """获取主要指数数据。"""
    try:
        session = next(db.get_session())
        try:
            stock_query = session.query(Stock).filter(Stock.type == "index")
            if code:
                stock_query = stock_query.filter(Stock.code == code)
            stocks = stock_query.all()

            latest_date = _latest_daily_trade_date_from_calendar() or session.query(func.max(KlineDaily.trade_date)).scalar()
            if not latest_date:
                return {"indices": [], "trading_date": None}

            previous_date = None
            try:
                previous_date = session.execute(
                    text(
                        "SELECT MAX(trade_date) FROM trade_calendar "
                        "WHERE trade_date < :latest_date AND is_trading = 1"
                    ),
                    {"latest_date": latest_date},
                ).scalar()
            except Exception as exc:
                logger.warning(f"获取前一个交易日失败: {exc}")

            if not previous_date:
                from scheduler.trading_calendar import TradingCalendar

                previous_date = TradingCalendar.get_previous_trading_day(latest_date)

            indices = []
            for stock in stocks:
                latest_kline = session.query(KlineDaily).filter(
                    KlineDaily.code == stock.code,
                    KlineDaily.trade_date == latest_date,
                ).first()
                if not latest_kline:
                    continue

                previous_kline = session.query(KlineDaily).filter(
                    KlineDaily.code == stock.code,
                    KlineDaily.trade_date == previous_date,
                ).first()

                change = 0
                change_pct = 0
                if previous_kline:
                    change = latest_kline.close - previous_kline.close
                    change_pct = (change / previous_kline.close) * 100

                indices.append(
                    {
                        "code": stock.code,
                        "name": stock.name,
                        "price": latest_kline.close,
                        "change": change,
                        "change_pct": change_pct,
                        "volume": latest_kline.volume,
                        "amount": latest_kline.amount,
                        "date": latest_kline.trade_date.strftime("%Y-%m-%d"),
                    }
                )

            return {"indices": indices, "trading_date": latest_date.strftime("%Y-%m-%d")}
        finally:
            session.close()
    except Exception as exc:
        logger.error(f"获取指数数据失败: {exc}")
        return {"indices": [], "trading_date": None}


def _compute_market_indices(code: Optional[str] = None):
    """获取主要指数数据，并返回关键指数健康状态（原始计算路径，R13 起由带 TTL 缓存的
    get_market_indices 包装）。"""
    try:
        session = next(db.get_session())
        try:
            stocks: List[Any] = []
            if clickhouse_available():
                stocks_table = _resolve_table_name("stocks")
                if not _has_clickhouse_table(stocks_table):
                    return {
                        "indices": [],
                        "trading_date": None,
                        "validation_status": "failed",
                        "validation_reason": f"missing_table:{stocks_table}",
                        "blocked_codes": [],
                    }
                stocks_sql = """
                    SELECT code, name
                    FROM {stocks_table}
                    WHERE type = 'index'
                      AND code IS NOT NULL
                      AND (quit = 0 OR quit IS NULL)
                """
                stocks_sql = stocks_sql.format(stocks_table=stocks_table)
                params: List[Any] = []
                if code:
                    stocks_sql += " AND code = ?"
                    params.append(code)
                stocks_sql += " ORDER BY code"
                stocks_df = clickhouse_query_df(stocks_sql, params)
                if stocks_df is not None and not stocks_df.empty:
                    stocks = [
                        SimpleNamespace(code=str(row.code), name=str(row.name or row.code))
                        for row in stocks_df.itertuples(index=False)
                    ]
            else:
                stock_query = session.query(Stock).filter(Stock.type == "index")
                if code:
                    stock_query = stock_query.filter(Stock.code == code)
                stocks = stock_query.all()

            if clickhouse_available():
                try:
                    kline_table = _resolve_table_name("kline_daily")
                    # R13：date_counts 加 60 自然日下界（Asia/Shanghai）——聚合输入从全表
                    # ~5 年压到 ~40 个交易日；本端点语义只需最近 20 个交易日（原
                    # LIMIT 20 DESC），窗口不改变结果，但消除对 17.6M 行 kline_daily
                    # 的无界全表 GROUP BY（CH code 241 内存超限根因）。
                    indices_since = (
                        datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=60)
                    ).isoformat()
                    date_counts_df = clickhouse_query_df(
                        """
                        SELECT k.trade_date AS trade_date, COUNT(DISTINCT k.code) AS row_count
                        FROM {kline_table} k
                        JOIN {stocks_table} s ON s.code = k.code
                        JOIN trade_calendar c
                          ON c.trade_date = k.trade_date
                         AND c.market = 'SH'
                         AND c.is_trading = 1
                        WHERE s.type = 'index'
                          AND k.trade_date >= ?
                        GROUP BY k.trade_date
                        ORDER BY k.trade_date DESC
                        LIMIT 20
                        """.format(kline_table=kline_table, stocks_table=stocks_table),
                        [indices_since],
                    )
                    latest_date = None
                    if date_counts_df is not None and not date_counts_df.empty:
                        max_count = int(date_counts_df["row_count"].max() or 0)
                        min_count = max(1, int(max_count * 0.70))
                        for row in date_counts_df.itertuples(index=False):
                            if int(row.row_count or 0) >= min_count:
                                latest_date = row.trade_date
                                break

                    if latest_date:
                        latest_date = _date_text(latest_date)
                        previous_date = None
                        try:
                            previous_date = session.execute(
                                text(
                                    "SELECT MAX(trade_date) FROM trade_calendar "
                                    "WHERE trade_date < :latest_date AND is_trading = 1"
                                ),
                                {"latest_date": latest_date},
                            ).scalar()
                        except Exception as exc:
                            logger.warning(f"ClickHouse indices previous trade date fallback failed: {exc}")

                        if not previous_date:
                            from scheduler.trading_calendar import TradingCalendar

                            previous_date = TradingCalendar.get_previous_trading_day(latest_date)

                        codes = [stock.code for stock in stocks]
                        latest_key = _date_text(latest_date)
                        previous_key = _date_text(previous_date)
                        kline_map: Dict[tuple[str, str], Dict[str, Any]] = {}
                        if codes:
                            placeholders = ",".join(["?"] * len(codes))
                            params = [latest_key, previous_key] + codes
                            kline_df = clickhouse_query_df(
                                """
                                SELECT code, trade_date, close, volume, amount
                                FROM {kline_table}
                                WHERE trade_date IN (?, ?)
                                  AND code IN ({placeholders})
                                """.format(kline_table=kline_table, placeholders=placeholders),
                                params,
                            )
                            if kline_df is not None and not kline_df.empty:
                                for row in kline_df.itertuples(index=False):
                                    kline_map[(row.code, str(row.trade_date)[:10])] = {
                                        "trade_date": row.trade_date,
                                        "close": row.close,
                                        "volume": row.volume,
                                        "amount": row.amount,
                                    }

                        indices: List[Dict[str, Any]] = []
                        blocked_codes: List[Dict[str, Any]] = []
                        present_core_codes = set()
                        intraday_snapshots, fact_snapshot_status = _latest_qmt_intraday_index_quotes(codes)
                        bridge_snapshots, bridge_status = _latest_qmt_home_index_quotes(codes)
                        # Prefer the host bridge when it is available, while retaining
                        # today's ClickHouse fact snapshot as the durable fallback.
                        intraday_snapshots.update(bridge_snapshots)
                        intraday_quote_status = {
                            "provider": "qmt_xtdata+qmt_clickhouse",
                            "status": "live" if intraday_snapshots else "unavailable",
                            "quote_count": len(intraday_snapshots),
                            "requested_count": len(codes),
                            "as_of": max(
                                (
                                    _datetime_text(item["snapshot_time"])
                                    for item in intraday_snapshots.values()
                                    if item.get("snapshot_time")
                                ),
                                default=None,
                            ),
                            "bridge_status": bridge_status,
                            "fact_snapshot_status": {
                                "status": "live" if any(
                                    item.get("source") == "qmt_clickhouse_intraday"
                                    for item in intraday_snapshots.values()
                                ) else "unavailable",
                                "as_of": fact_snapshot_status.get("as_of"),
                            },
                        }
                        intraday_dates = [
                            _date_text(item.get("snapshot_date"))
                            for item in intraday_snapshots.values()
                            if item.get("snapshot_date")
                        ]
                        intraday_trading_date = max(intraday_dates) if intraday_dates else None

                        for stock in stocks:
                            latest_kline = kline_map.get((stock.code, latest_key))
                            if not latest_kline:
                                if stock.code in CORE_INDEX_CODES:
                                    blocked_codes.append({"code": stock.code, "reason": "missing_latest_kline"})
                                continue

                            previous_kline = kline_map.get((stock.code, previous_key))
                            intraday_snapshot = intraday_snapshots.get(stock.code)
                            change = 0.0
                            change_pct = 0.0
                            latest_close = float(
                                (intraday_snapshot or {}).get("price")
                                if intraday_snapshot
                                else latest_kline.get("close")
                                or 0
                            )
                            prev_close = 0.0
                            if intraday_snapshot and intraday_snapshot.get("pre_close"):
                                prev_close = float(intraday_snapshot.get("pre_close") or 0)
                            elif previous_kline and previous_kline.get("close"):
                                prev_close = float(previous_kline.get("close") or 0)
                            if prev_close > 0:
                                change = latest_close - prev_close
                                change_pct = (change / prev_close) * 100

                            threshold = CORE_INDEX_JUMP_THRESHOLD if stock.code in CORE_INDEX_CODES else DEFAULT_INDEX_JUMP_THRESHOLD
                            is_abnormal = latest_close <= 0 or (prev_close > 0 and abs(change_pct) > threshold)
                            validation_reason = "ok"
                            if latest_close <= 0:
                                validation_reason = "invalid_close"
                            elif prev_close > 0 and abs(change_pct) > threshold:
                                validation_reason = f"change_pct_exceeds_{threshold}%"

                            if stock.code in CORE_INDEX_CODES:
                                present_core_codes.add(stock.code)
                                if is_abnormal:
                                    blocked_codes.append(
                                        {
                                            "code": stock.code,
                                            "reason": validation_reason,
                                            "change_pct": change_pct,
                                        }
                                    )

                            date_value = latest_kline.get("trade_date")
                            snapshot_date = (intraday_snapshot or {}).get("snapshot_date")
                            snapshot_time = (intraday_snapshot or {}).get("snapshot_time")
                            row_date = snapshot_date or date_value
                            data_source = (intraday_snapshot or {}).get("source", "kline_daily") if intraday_snapshot else "kline_daily"
                            indices.append(
                                {
                                    "code": stock.code,
                                    "name": stock.name,
                                    "price": latest_close,
                                    "change": change,
                                    "change_pct": change_pct,
                                    "volume": (intraday_snapshot or {}).get("volume", latest_kline.get("volume")),
                                    "amount": (intraday_snapshot or {}).get("amount", latest_kline.get("amount")),
                                    "date": row_date.strftime("%Y-%m-%d") if hasattr(row_date, "strftime") else str(row_date),
                                    "snapshot_time": _datetime_text(snapshot_time) if snapshot_time else None,
                                    "data_source": data_source,
                                    "is_abnormal": is_abnormal,
                                    "validation_reason": validation_reason,
                                }
                            )

                        missing_core = CORE_INDEX_CODES - present_core_codes
                        for missing_code in sorted(missing_core):
                            blocked_codes.append({"code": missing_code, "reason": "missing_core_index"})

                        validation_status = "passed" if not blocked_codes else "failed"
                        validation_reason = "ok" if not blocked_codes else "core_index_validation_failed"
                        latest_date_text = intraday_trading_date or _date_text(latest_date)

                        return {
                            "indices": indices,
                            "trading_date": latest_date_text,
                            "validation_status": validation_status,
                            "validation_reason": validation_reason,
                            "blocked_codes": blocked_codes,
                            "source": "clickhouse+qmt_intraday_quote" if intraday_trading_date else "clickhouse",
                            "intraday_snapshot_time": intraday_quote_status.get("as_of"),
                            "intraday_quote_status": intraday_quote_status,
                        }
                except Exception as exc:
                    logger.warning(f"ClickHouse market indices fallback to SQLAlchemy engine: {exc}")

            date_count_rows = (
                session.query(KlineDaily.trade_date, func.count(func.distinct(KlineDaily.code)).label("row_count"))
                .join(Stock, Stock.code == KlineDaily.code)
                .filter(Stock.type == "index")
                .group_by(KlineDaily.trade_date)
                .order_by(KlineDaily.trade_date.desc())
                .limit(20)
                .all()
            )
            latest_date = None
            if date_count_rows:
                max_count = max(int(row_count or 0) for _, row_count in date_count_rows)
                min_count = max(1, int(max_count * 0.70))
                from scheduler.trading_calendar import TradingCalendar

                for trade_date, row_count in date_count_rows:
                    trade_dt = trade_date if hasattr(trade_date, "hour") else SimpleNamespace(date=lambda value=trade_date: value)
                    if int(row_count or 0) >= min_count and TradingCalendar.is_trading_day(trade_dt):
                        latest_date = trade_date
                        break
            if not latest_date:
                return {
                    "indices": [],
                    "trading_date": None,
                    "validation_status": "failed",
                    "validation_reason": "no_kline_data",
                    "blocked_codes": [],
                }

            previous_date = None
            try:
                previous_date = session.execute(
                    text(
                        "SELECT MAX(trade_date) FROM trade_calendar "
                        "WHERE trade_date < :latest_date AND is_trading = 1"
                    ),
                    {"latest_date": latest_date},
                ).scalar()
            except Exception as exc:
                logger.warning(f"获取前一交易日失败: {exc}")

            if not previous_date:
                from scheduler.trading_calendar import TradingCalendar

                previous_date = TradingCalendar.get_previous_trading_day(latest_date)

            indices: List[Dict[str, Any]] = []
            blocked_codes: List[Dict[str, Any]] = []
            present_core_codes = set()

            for stock in stocks:
                latest_kline = session.execute(
                    text(
                        """
                        SELECT trade_date, close, volume, amount
                        FROM kline_daily
                        WHERE code = :code AND trade_date = :trade_date
                        LIMIT 1
                        """
                    ),
                    {"code": stock.code, "trade_date": latest_date},
                ).mappings().first()
                if not latest_kline:
                    if stock.code in CORE_INDEX_CODES:
                        blocked_codes.append({"code": stock.code, "reason": "missing_latest_kline"})
                    continue

                previous_kline = session.execute(
                    text(
                        """
                        SELECT trade_date, close, volume, amount
                        FROM kline_daily
                        WHERE code = :code AND trade_date = :trade_date
                        LIMIT 1
                        """
                    ),
                    {"code": stock.code, "trade_date": previous_date},
                ).mappings().first()

                change = 0.0
                change_pct = 0.0
                latest_close = float(latest_kline.get("close") or 0)
                if previous_kline and previous_kline.get("close"):
                    prev_close = float(previous_kline.get("close") or 0)
                    if prev_close > 0:
                        change = latest_close - prev_close
                        change_pct = (change / prev_close) * 100

                threshold = CORE_INDEX_JUMP_THRESHOLD if stock.code in CORE_INDEX_CODES else DEFAULT_INDEX_JUMP_THRESHOLD
                is_abnormal = latest_close <= 0 or (previous_kline is not None and abs(change_pct) > threshold)
                validation_reason = "ok"
                if latest_close <= 0:
                    validation_reason = "invalid_close"
                elif previous_kline is not None and abs(change_pct) > threshold:
                    validation_reason = f"change_pct_exceeds_{threshold}%"

                if stock.code in CORE_INDEX_CODES:
                    present_core_codes.add(stock.code)
                    if is_abnormal:
                        blocked_codes.append(
                            {
                                "code": stock.code,
                                "reason": validation_reason,
                                "change_pct": change_pct,
                            }
                        )

                indices.append(
                    {
                        "code": stock.code,
                        "name": stock.name,
                        "price": latest_kline.get("close"),
                        "change": change,
                        "change_pct": change_pct,
                        "volume": latest_kline.get("volume"),
                        "amount": latest_kline.get("amount"),
                        "date": (
                            latest_kline.get("trade_date").strftime("%Y-%m-%d")
                            if hasattr(latest_kline.get("trade_date"), "strftime")
                            else str(latest_kline.get("trade_date"))
                        ),
                        "is_abnormal": is_abnormal,
                        "validation_reason": validation_reason,
                    }
                )

            missing_core = CORE_INDEX_CODES - present_core_codes
            for missing_code in sorted(missing_core):
                blocked_codes.append({"code": missing_code, "reason": "missing_core_index"})

            validation_status = "passed" if not blocked_codes else "failed"
            validation_reason = "ok" if not blocked_codes else "core_index_validation_failed"

            return {
                "indices": indices,
                "trading_date": latest_date.strftime("%Y-%m-%d"),
                "validation_status": validation_status,
                "validation_reason": validation_reason,
                "blocked_codes": blocked_codes,
            }
        finally:
            session.close()
    except Exception as exc:
        logger.error(f"获取指数数据失败: {exc}")
        return {
            "indices": [],
            "trading_date": None,
            "validation_status": "failed",
            "validation_reason": str(exc),
            "blocked_codes": [],
        }


@router.get("/indices")
def get_market_indices(code: Optional[str] = None):
    """获取主要指数数据，并返回关键指数健康状态。

    R13（2026-11-09）：进程内 TTL 结果缓存（45 秒，monotonic 时钟，按 code 参数
    分键）；缓存命中直接返回，不重跑 date_counts 聚合与 QMT 桥探测。
    """
    cache_key = code or ""
    now = time.monotonic()
    cached = _MARKET_INDICES_RESULT_CACHE.get(cache_key)
    if cached is not None and now < float(cached.get("expires_at") or 0.0):
        return cached["payload"]
    payload = _compute_market_indices(code)
    _MARKET_INDICES_RESULT_CACHE[cache_key] = {
        "expires_at": now + _MARKET_INDICES_RESULT_CACHE_TTL_SECONDS,
        "payload": payload,
    }
    return payload


@router.get("/sentiment-legacy")
def _legacy_get_market_sentiment():
    """获取市场涨跌统计数据。"""
    session = next(db.get_session())
    try:
        latest_date = session.query(func.max(KlineDaily.trade_date)).filter(
            KlineDaily.code == "999999.SH"
        ).scalar()
        if not latest_date:
            latest_date = session.query(func.max(KlineDaily.trade_date)).scalar()
        if latest_date:
            from scheduler.trading_calendar import TradingCalendar

            if not TradingCalendar.is_trading_day(latest_date):
                latest_date = TradingCalendar.get_previous_trading_day(latest_date).date()
        if not latest_date:
            return {"error": "数据库中没有K线数据"}

        sentiment_query = session.query(
            func.count(case((KlineDaily.change_pct > 0, 1), else_=None)).label("up_count"),
            func.count(case((KlineDaily.change_pct < 0, 1), else_=None)).label("down_count"),
            func.count(case((KlineDaily.change_pct == 0, 1), else_=None)).label("unchanged_count"),
            func.count(case((KlineDaily.change_pct >= 5, 1), else_=None)).label("up_5_count"),
            func.count(case((KlineDaily.change_pct <= -5, 1), else_=None)).label("down_5_count"),
            func.count(case((KlineDaily.change_pct >= 9.9, 1), else_=None)).label("limit_up_count"),
            func.count(case((KlineDaily.change_pct <= -9.9, 1), else_=None)).label("limit_down_count"),
            func.sum(KlineDaily.amount).label("total_amount"),
        ).filter(
            KlineDaily.trade_date == latest_date,
            KlineDaily.code.in_(session.query(Stock.code).filter(Stock.type == "stock")),
            KlineDaily.open > 0,
        ).first()

        if not sentiment_query:
            return {"error": "无法获取涨跌统计数据"}

        up_count = int(sentiment_query.up_count or 0)
        down_count = int(sentiment_query.down_count or 0)
        unchanged_count = int(sentiment_query.unchanged_count or 0)
        up_5_count = int(sentiment_query.up_5_count or 0)
        down_5_count = int(sentiment_query.down_5_count or 0)
        limit_up_count = int(sentiment_query.limit_up_count or 0)
        limit_down_count = int(sentiment_query.limit_down_count or 0)
        total_amount = float(sentiment_query.total_amount or 0)

        total = up_count + down_count + unchanged_count
        sentiment_score = 50 + (up_count - down_count) / total * 50 if total > 0 else 50
        if sentiment_score >= 80:
            sentiment_level = "极度乐观"
        elif sentiment_score >= 65:
            sentiment_level = "乐观"
        elif sentiment_score >= 55:
            sentiment_level = "偏乐观"
        elif sentiment_score >= 45:
            sentiment_level = "中性"
        elif sentiment_score >= 35:
            sentiment_level = "偏悲观"
        elif sentiment_score >= 20:
            sentiment_level = "悲观"
        else:
            sentiment_level = "极度悲观"

        sh_kline = session.query(KlineDaily).filter(
            KlineDaily.code == "999999.SH",
            KlineDaily.trade_date == latest_date,
        ).first()
        sz_kline = session.query(KlineDaily).filter(
            KlineDaily.code == "399001.SZ",
            KlineDaily.trade_date == latest_date,
        ).first()

        return {
            "date": latest_date.isoformat(),
            "up_count": up_count,
            "down_count": down_count,
            "unchanged_count": unchanged_count,
            "up_5_percent_count": up_5_count,
            "down_5_percent_count": down_5_count,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "avg_change_percent": 0,
            "total_turnover": total_amount,
            "sh_amount": float(sh_kline.amount) if sh_kline and sh_kline.amount else 0,
            "sz_amount": float(sz_kline.amount) if sz_kline and sz_kline.amount else 0,
            "sentiment_score": sentiment_score,
            "sentiment_level": sentiment_level,
        }
    except Exception as exc:
        logger.error(f"获取涨跌统计失败: {exc}")
        return {"error": str(exc)}
    finally:
        session.close()


def _load_market_sentiment_from_clickhouse() -> Optional[Dict[str, Any]]:
    """R20（2026-11-09）：直接从 kline_daily 按需计算，不再读预计算快照。

    旧路径优先读 market_sentiment_snapshot 预计算快照（由已停用的调度器刷新，
    首页 date 停在 2026-10-30）；R20 起停读快照，始终走下方 kline_daily 聚合。
    R20b（2026-11-09 回归修复）：全链改 market_warehouse raw=True 基表直查——
    R20 事故中本函数的 JOIN-MAX 日期解析与 ORM 回退被映射到无界 FINAL 视图
    子查询，日期/代码谓词无法下推，单条 90~173 秒拖垮连接池。R20b 改法：
    数据日期 = kline_daily 纯 max(trade_date)（raw、分区级秒回）；30 日窗口
    日期改 trade_calendar 直查（raw、小表）；汇总/分市场额/30 日聚合直查基表，
    trade_date = ? / IN (...) 谓词随分区裁剪下推，行级去重用 FINAL 修饰符
    （等价原视图语义）；情绪周期改 raw 两行查询；全程不经 SQLAlchemy session。
    口径提取自快照任务本体 services.market_sentiment_cache.refresh_daily_history
    （api/system_config.py refresh_market_sentiment_snapshot_task 调用）：
    universe = kline_daily JOIN stocks WHERE s.type='stock' AND k.open>0；
    up/down/unchanged = pct>0/<0/=0；up_5/down_5 = >=5/<=-5；
    limit_up/down = >=9.9/<=-9.9；9 桶分布边界同其 _COLUMNS。提取复用，不重写。
    """
    if not clickhouse_available():
        return None
    stocks_table = _resolve_table_name("stocks")
    kline_table = _resolve_table_name("kline_daily")
    if not _has_clickhouse_table(stocks_table) or not _has_clickhouse_table(kline_table):
        return None
    if not _has_clickhouse_table("trade_calendar"):
        return None
    latest_date_text = _kline_daily_latest_trade_date()
    if not latest_date_text:
        return None
    latest_date = datetime.strptime(latest_date_text, "%Y-%m-%d").date()

    summary_df = clickhouse_query_df(
        """
        WITH base AS (
            SELECT
                s.market,
                k.amount,
                k.change_pct AS pct
            FROM {kline_table} AS k FINAL
            JOIN {stocks_table} AS s ON s.code = k.code
            WHERE s.type = 'stock'
              AND k.trade_date = ?
              AND k.open > 0
        )
        SELECT
            SUM(CASE WHEN pct > 0 THEN 1 ELSE 0 END) AS up_count,
            SUM(CASE WHEN pct < 0 THEN 1 ELSE 0 END) AS down_count,
            SUM(CASE WHEN pct = 0 THEN 1 ELSE 0 END) AS unchanged_count,
            SUM(CASE WHEN pct >= 5 THEN 1 ELSE 0 END) AS up_5_count,
            SUM(CASE WHEN pct <= -5 THEN 1 ELSE 0 END) AS down_5_count,
            SUM(CASE WHEN pct >= 9.9 THEN 1 ELSE 0 END) AS limit_up_count,
            SUM(CASE WHEN pct <= -9.9 THEN 1 ELSE 0 END) AS limit_down_count,
            AVG(pct) AS avg_change_percent,
            SUM(amount) AS total_amount,
            SUM(CASE WHEN pct >= 7 THEN 1 ELSE 0 END) AS bucket_up_7,
            SUM(CASE WHEN pct >= 5 AND pct < 7 THEN 1 ELSE 0 END) AS bucket_up_5_7,
            SUM(CASE WHEN pct >= 3 AND pct < 5 THEN 1 ELSE 0 END) AS bucket_up_3_5,
            SUM(CASE WHEN pct > 0 AND pct < 3 THEN 1 ELSE 0 END) AS bucket_up_0_3,
            SUM(CASE WHEN pct = 0 THEN 1 ELSE 0 END) AS bucket_zero,
            SUM(CASE WHEN pct < 0 AND pct > -3 THEN 1 ELSE 0 END) AS bucket_down_0_3,
            SUM(CASE WHEN pct <= -3 AND pct > -5 THEN 1 ELSE 0 END) AS bucket_down_3_5,
            SUM(CASE WHEN pct <= -5 AND pct > -7 THEN 1 ELSE 0 END) AS bucket_down_5_7,
            SUM(CASE WHEN pct <= -7 THEN 1 ELSE 0 END) AS bucket_down_7
        FROM base
        """.format(kline_table=kline_table, stocks_table=stocks_table),
        [latest_date_text],
        raw=True,
    )
    if summary_df is None or summary_df.empty:
        return None
    row = summary_df.iloc[0]

    def _int_col(name: str) -> int:
        return int(row.get(name) or 0)

    up_count = _int_col("up_count")
    down_count = _int_col("down_count")
    unchanged_count = _int_col("unchanged_count")
    up_5_count = _int_col("up_5_count")
    down_5_count = _int_col("down_5_count")
    limit_up_count = _int_col("limit_up_count")
    limit_down_count = _int_col("limit_down_count")
    avg_change_percent = float(row.get("avg_change_percent") or 0)
    total_amount = float(row.get("total_amount") or 0)
    total = up_count + down_count + unchanged_count
    sentiment_score = 50 + (up_count - down_count) / total * 50 if total > 0 else 50

    turnover_df = clickhouse_query_df(
        """
        SELECT s.market, SUM(k.amount) AS amount
        FROM {kline_table} AS k FINAL
        JOIN {stocks_table} s ON s.code = k.code
        WHERE s.type = 'stock'
          AND s.market IN ('SH', 'SZ')
          AND k.trade_date = ?
          AND k.open > 0
        GROUP BY s.market
        """.format(kline_table=kline_table, stocks_table=stocks_table),
        [latest_date_text],
        raw=True,
    )
    turnover_by_market = {}
    if turnover_df is not None and not turnover_df.empty:
        turnover_by_market = {
            str(item.market): float(item.amount or 0)
            for item in turnover_df.itertuples(index=False)
        }

    calendar_df = clickhouse_query_df(
        """
        SELECT trade_date
        FROM trade_calendar
        WHERE is_trading = 1 AND market = 'SH' AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT 30
        """,
        [latest_date_text],
        raw=True,
    )
    all_dates = (
        []
        if calendar_df is None or calendar_df.empty
        else [_date_text(v) for v in calendar_df["trade_date"].tolist()]
    )
    curve_dates = list(reversed(all_dates))
    trend_dates = curve_dates[-7:]
    curve_map: Dict[str, Dict[str, int]] = {}

    if curve_dates:
        placeholders = ",".join(["?"] * len(curve_dates))
        curve_df = clickhouse_query_df(
            f"""
            WITH base AS (
                SELECT
                    k.trade_date,
                    k.amount AS amount,
                    s.market AS market,
                    k.change_pct AS pct
                FROM {kline_table} AS k FINAL
                JOIN {stocks_table} s ON s.code = k.code
                WHERE s.type = 'stock'
                  AND k.open > 0
                  AND k.trade_date IN ({placeholders})
            )
            SELECT
                trade_date,
                SUM(amount) AS total_amount,
                SUM(CASE WHEN market = 'SH' THEN amount ELSE 0 END) AS sh_amount,
                SUM(CASE WHEN market = 'SZ' THEN amount ELSE 0 END) AS sz_amount,
                SUM(CASE WHEN market = 'BJ' THEN amount ELSE 0 END) AS bj_amount,
                SUM(CASE WHEN pct > 0 THEN 1 ELSE 0 END) AS up_count,
                SUM(CASE WHEN pct < 0 THEN 1 ELSE 0 END) AS down_count,
                SUM(CASE WHEN pct >= 9.9 THEN 1 ELSE 0 END) AS limit_up_count,
                SUM(CASE WHEN pct <= -9.9 THEN 1 ELSE 0 END) AS limit_down_count,
                SUM(CASE WHEN pct >= 5 THEN 1 ELSE 0 END) AS up_5_count,
                SUM(CASE WHEN pct <= -5 THEN 1 ELSE 0 END) AS down_5_count
            FROM base
            GROUP BY trade_date
            """,
            curve_dates,
            raw=True,
        )
        if curve_df is not None and not curve_df.empty:
            for item in curve_df.itertuples(index=False):
                key = _date_text(item.trade_date)
                curve_map[key] = {
                    "total_amount": float(item.total_amount or 0),
                    "sh_amount": float(item.sh_amount or 0),
                    "sz_amount": float(item.sz_amount or 0),
                    "bj_amount": float(item.bj_amount or 0),
                    "up_count": int(item.up_count or 0),
                    "down_count": int(item.down_count or 0),
                    "limit_up_count": int(item.limit_up_count or 0),
                    "limit_down_count": int(item.limit_down_count or 0),
                    "up_5_count": int(item.up_5_count or 0),
                    "down_5_count": int(item.down_5_count or 0),
                }

    raw_series = {
        "up_count_series": [curve_map.get(d, {}).get("up_count", 0) for d in curve_dates],
        "down_count_series": [curve_map.get(d, {}).get("down_count", 0) for d in curve_dates],
        "limit_up_count_series": [curve_map.get(d, {}).get("limit_up_count", 0) for d in curve_dates],
        "limit_down_count_series": [curve_map.get(d, {}).get("limit_down_count", 0) for d in curve_dates],
        "up_5_count_series": [curve_map.get(d, {}).get("up_5_count", 0) for d in curve_dates],
        "down_5_count_series": [curve_map.get(d, {}).get("down_5_count", 0) for d in curve_dates],
    }

    def _normalize_series(series):
        values = [float(v or 0) for v in series]
        if not values:
            return []
        min_val = min(values)
        max_val = max(values)
        if max_val == min_val:
            return [50.0 for _ in values]
        return [round((v - min_val) / (max_val - min_val) * 100, 2) for v in values]

    latest_cycle = None
    previous_cycle = None
    if _has_clickhouse_table("emotion_cycle"):
        try:
            cycle_df = clickhouse_query_df(
                """
                SELECT
                    toString(date) AS date,
                    close_up_rate,
                    strong_up_rate,
                    limit_up_follow_rate
                FROM emotion_cycle
                ORDER BY date DESC
                LIMIT 2
                """,
                raw=True,
            )
        except Exception as exc:
            logger.warning(f"sentiment emotion cycle raw query failed: {exc}")
            cycle_df = None
        if cycle_df is not None and not cycle_df.empty:
            cycle_rows = list(cycle_df.itertuples(index=False))

            def _cycle_row(item: Any) -> Any:
                return SimpleNamespace(
                    date=datetime.strptime(str(item.date)[:10], "%Y-%m-%d").date(),
                    close_up_rate=item.close_up_rate,
                    strong_up_rate=item.strong_up_rate,
                    limit_up_follow_rate=item.limit_up_follow_rate,
                )

            latest_cycle = _cycle_row(cycle_rows[0])
            if len(cycle_rows) > 1:
                previous_cycle = _cycle_row(cycle_rows[1])

    distribution = [
        {"label": ">=7", "count": _int_col("bucket_up_7"), "side": "up"},
        {"label": "5~7", "count": _int_col("bucket_up_5_7"), "side": "up"},
        {"label": "3~5", "count": _int_col("bucket_up_3_5"), "side": "up"},
        {"label": "0~3", "count": _int_col("bucket_up_0_3"), "side": "up"},
        {"label": "0", "count": _int_col("bucket_zero"), "side": "flat"},
        {"label": "-3~0", "count": _int_col("bucket_down_0_3"), "side": "down"},
        {"label": "-5~-3", "count": _int_col("bucket_down_3_5"), "side": "down"},
        {"label": "-7~-5", "count": _int_col("bucket_down_5_7"), "side": "down"},
        {"label": "<=-7", "count": _int_col("bucket_down_7"), "side": "down"},
    ]
    latest_snapshot = {
        "up_count": up_count,
        "down_count": down_count,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "up_5_count": up_5_count,
        "down_5_count": down_5_count,
    }
    intraday = _load_qmt_intraday_market_sentiment()
    # R20b：成交额预测的重查询只允许在「盘中且 overlay 命中」分支同步执行
    # （endpoint TTL=300s 吸收，且其结果仅盘中有意义）；该服务内部走非 raw
    # 通道（视图重写 + toDate 谓词不裁剪），query_log 实证冷启动可达
    # 126~223 秒 / 27.5~55GiB 全表扫描。盘后/非交易时段/overlay 未命中一律
    # 改读同日持久化缓存（get_cached，读文件零查询、重启即恢复），杜绝部署
    # 后首个请求被拖垮。服务本体的 raw 化留待后续任务（涉及其他文件）。
    if intraday is not None and _is_sentiment_intraday_session(
        datetime.now(ZoneInfo("Asia/Shanghai"))
    ):
        turnover_forecast = get_intraday_turnover_forecast(current_snapshot=intraday)
    else:
        turnover_forecast = get_cached_intraday_turnover_forecast()

    payload = {
        "date": latest_date_text,
        "up_count": up_count,
        "down_count": down_count,
        "unchanged_count": unchanged_count,
        "up_5_percent_count": up_5_count,
        "down_5_percent_count": down_5_count,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "avg_change_percent": round(avg_change_percent, 3),
        "total_turnover": total_amount,
        "sh_amount": float(turnover_by_market.get("SH") or 0),
        "sz_amount": float(turnover_by_market.get("SZ") or 0),
        "turnover_source": "stock_aggregate",
        "change_distribution": distribution,
        "up_down_5_trend_7d": {
            "trade_dates": trend_dates,
            "up_5_counts": [curve_map.get(d, {}).get("up_5_count", 0) for d in trend_dates],
            "down_5_counts": [curve_map.get(d, {}).get("down_5_count", 0) for d in trend_dates],
            "latest_date": latest_date_text,
        },
        "emotion_curve_30d": {
            "trade_dates": curve_dates,
            "raw_series": raw_series,
            "normalized_series": {key: _normalize_series(value) for key, value in raw_series.items()},
            "latest_snapshot": latest_snapshot,
            "latest_date": latest_date_text,
        },
        "turnover_trend_30d": {
            "trade_dates": curve_dates,
            "amounts": [float(curve_map.get(d, {}).get("total_amount", 0)) for d in curve_dates],
            "sh_amounts": [float(curve_map.get(d, {}).get("sh_amount", 0)) for d in curve_dates],
            "sz_amounts": [float(curve_map.get(d, {}).get("sz_amount", 0)) for d in curve_dates],
            "bj_amounts": [float(curve_map.get(d, {}).get("bj_amount", 0)) for d in curve_dates],
            "threshold_amount": 2_000_000_000_000,
            "unit": "CNY",
            "latest_date": latest_date_text,
            "latest_is_provisional": False,
        },
        "turnover_forecast_intraday": turnover_forecast,
        "sentiment_score": sentiment_score,
        "sentiment_level": _sentiment_level(sentiment_score),
        "emotion_phase": _build_emotion_phase(
            trade_date=latest_date,
            sentiment_score=sentiment_score,
            up_count=up_count,
            down_count=down_count,
            unchanged_count=unchanged_count,
            up_5_count=up_5_count,
            down_5_count=down_5_count,
            limit_up_count=limit_up_count,
            limit_down_count=limit_down_count,
            latest_cycle=latest_cycle,
            previous_cycle=previous_cycle,
        ),
        "source": "clickhouse",
        # R20：与快照时代载荷结构对齐——确认日线口径下无盘中态；
        # 盘中覆盖层（_load_qmt_intraday_market_sentiment 命中时）会整体覆写。
        "intraday_status": None,
    }
    if not intraday:
        return payload

    turnover_trend = payload["turnover_trend_30d"]
    intraday_date = str(intraday["date"])
    trend_dates = list(turnover_trend["trade_dates"])
    trend_values = {
        "amounts": float(intraday["total_turnover"]),
        "sh_amounts": float(intraday["sh_amount"]),
        "sz_amounts": float(intraday["sz_amount"]),
        "bj_amounts": float(intraday.get("bj_amount") or 0),
    }
    if trend_dates and trend_dates[-1] == intraday_date:
        for key, value in trend_values.items():
            turnover_trend[key][-1] = value
    else:
        turnover_trend["trade_dates"] = (trend_dates + [intraday_date])[-30:]
        for key, value in trend_values.items():
            turnover_trend[key] = (list(turnover_trend[key]) + [value])[-30:]
    turnover_trend.update(
        {
            "latest_date": intraday_date,
            "latest_is_provisional": True,
            "latest_as_of": intraday["as_of"],
        }
    )

    total = int(intraday["up_count"]) + int(intraday["down_count"]) + int(intraday["unchanged_count"])
    sentiment_score = (
        50 + (int(intraday["up_count"]) - int(intraday["down_count"])) / total * 50
        if total > 0
        else 50
    )
    payload.update(
        {
            "date": intraday["date"],
            "up_count": intraday["up_count"],
            "down_count": intraday["down_count"],
            "unchanged_count": intraday["unchanged_count"],
            "up_5_percent_count": intraday["up_5_percent_count"],
            "down_5_percent_count": intraday["down_5_percent_count"],
            "limit_up_count": intraday["limit_up_count"],
            "limit_down_count": intraday["limit_down_count"],
            "avg_change_percent": intraday["avg_change_percent"],
            "total_turnover": intraday["total_turnover"],
            "sh_amount": intraday["sh_amount"],
            "sz_amount": intraday["sz_amount"],
            "turnover_source": "qmt_5m_stock_aggregate",
            "change_distribution": intraday["change_distribution"],
            "sentiment_score": round(sentiment_score, 2),
            "sentiment_level": _sentiment_level(sentiment_score),
            "source": "qmt_5m_intraday+clickhouse_daily_history",
            "intraday_status": {
                "mode": "provisional_5m",
                "as_of": intraday["as_of"],
                "covered_count": intraday["covered_count"],
                "expected_count": intraday["expected_count"],
                "coverage_ratio": intraday["coverage_ratio"],
                "source": intraday["source"],
            },
            # Curves and the emotion phase remain confirmed daily values.  They
            # are deliberately not mutated with an incomplete trading session.
            "daily_close_date": latest_date_text,
        }
    )
    return payload


@router.get("/margin-sentiment")
def get_margin_sentiment(days: int = 30):
    """Return confirmed exchange-published daily margin-financing sentiment.

    R15（2026-11-09）：进程内 TTL 结果缓存（60 秒，monotonic 时钟，键 days），
    与 R13/R14 缓存同风格；数据源 market_margin_sentiment 为官方交易所日度摘要
    的预计算落库事实（本就快），TTL 仅防首页轮询重复查询；available=False 载荷
    照常缓存（无 "error" 键）。
    """
    cache_key = str(int(days))
    now = time.monotonic()
    cached = _MARGIN_SENTIMENT_RESULT_CACHE.get(cache_key)
    if cached is not None and now < float(cached.get("expires_at") or 0.0):
        return cached["payload"]
    payload = load_margin_sentiment(days=days)
    _MARGIN_SENTIMENT_RESULT_CACHE[cache_key] = {
        "expires_at": now + _MARGIN_SENTIMENT_RESULT_CACHE_TTL_SECONDS,
        "payload": payload,
    }
    return payload


def _compute_market_sentiment():
    """获取市场涨跌统计数据（含同花顺风格涨跌分布）。

    原始计算路径，R15 起由带 TTL 缓存的 get_market_sentiment 包装。R20b 起
    全链 market_warehouse raw=True 基表直查（_load_market_sentiment_from_
    clickhouse 为唯一数据源），不再走 SQLAlchemy ORM 回退——R20 事故中 ORM
    实体映射到无界 FINAL 视图、谓词不下推，单条查询 90~173 秒拖垮连接池。
    """
    try:
        clickhouse_payload = _load_market_sentiment_from_clickhouse()
        if clickhouse_payload:
            return clickhouse_payload
    except Exception as exc:
        logger.error(f"获取涨跌统计失败: {exc}")
        return {"error": str(exc)}
    return {"error": "数据库中没有K线数据"}


_SENTIMENT_INTRADAY_START = dt_time(9, 15)
_SENTIMENT_INTRADAY_END = dt_time(15, 0)


def _is_sentiment_intraday_session(now_sh: datetime) -> bool:
    """R20 盘中判定：Asia/Shanghai 交易日 09:15~15:00（含端点）。

    交易日判定复用 scheduler.trading_calendar.TradingCalendar（trade_calendar
    表为主、周末规则兜底）；日历异常时 fail-closed 视为盘后——走数据日期绑定
    缓存，绝不把判不出的时段当作盘中长缓存。
    """
    try:
        from scheduler.trading_calendar import TradingCalendar

        if not TradingCalendar.is_trading_day(now_sh):
            return False
    except Exception as exc:
        logger.warning(f"sentiment intraday session trading-day check failed: {exc}")
        return False
    current = now_sh.time()
    return _SENTIMENT_INTRADAY_START <= current <= _SENTIMENT_INTRADAY_END


def _kline_daily_latest_trade_date() -> Optional[str]:
    """R20 盘后缓存键：kline_daily 的 max(trade_date)（raw 轻量通道、免 FINAL）。"""
    if not clickhouse_available():
        return None
    kline_table = _resolve_table_name("kline_daily")
    if not _has_clickhouse_table(kline_table):
        return None
    try:
        value = clickhouse_scalar(f"SELECT max(trade_date) FROM {kline_table}", raw=True)
    except Exception as exc:
        logger.warning(f"sentiment cache key max(trade_date) query failed: {exc}")
        return None
    return _date_text(value) if value is not None else None


@router.get("/sentiment")
def get_market_sentiment():
    """获取市场涨跌统计数据（含同花顺风格涨跌分布）。

    R20（2026-11-09）：数据源改为 kline_daily 最新有数据交易日按需重算（停读
    已停用调度器维护的预计算快照）。缓存分档：盘中（Asia/Shanghai 交易日
    09:15~15:00）TTL=300s 每 5 分钟重算一次；盘后/非交易时段缓存键绑定
    kline_daily 的 max(trade_date)，数据日期不变即命中、补数完成后首个请求
    重算一次。error 载荷不缓存（与 R14/R15 一致）。
    """
    cached = _MARKET_SENTIMENT_RESULT_CACHE.get("")
    now = time.monotonic()
    if cached is not None:
        if _is_sentiment_intraday_session(datetime.now(ZoneInfo("Asia/Shanghai"))):
            if now < float(cached.get("expires_at") or 0.0):
                return cached["payload"]
        else:
            latest_trade_date = _kline_daily_latest_trade_date()
            if latest_trade_date is not None and cached.get("data_date") == latest_trade_date:
                return cached["payload"]
    payload = _compute_market_sentiment()
    if isinstance(payload, dict) and "error" in payload:
        return payload
    _MARKET_SENTIMENT_RESULT_CACHE[""] = {
        "expires_at": now + _SENTIMENT_INTRADAY_TTL_SECONDS,
        # 计算完成后现取数据日期作盘后缓存键：若计算期间恰有补数落地，键不等
        # 会让下个请求再重算一次并收敛（恰好实现"补齐后重算一次"）。
        "data_date": _kline_daily_latest_trade_date(),
        "payload": payload,
    }
    return payload


# R14（2026-11-09）：/market/limit-up-ladder 结果的进程内 TTL 缓存（60 秒，
# monotonic 时钟，键含归一化后的 min_streak/max_streak/history_days），与 R13
# /market/indices 缓存同风格；error 载荷不缓存。
_LIMIT_UP_LADDER_RESULT_CACHE: Dict[str, Dict[str, Any]] = {}
_LIMIT_UP_LADDER_RESULT_CACHE_TTL_SECONDS = 60.0


@router.get("/limit-up-ladder")
def get_limit_up_ladder(
    min_streak: int = 2,
    max_streak: int = 7,
    history_days: int = 10,
):
    """获取最新交易日连板天梯。"""
    min_streak = max(1, min(int(min_streak), 20))
    max_streak = max(min_streak, min(int(max_streak), 20))
    history_days = max(1, min(int(history_days), 30))

    cache_key = f"{min_streak}|{max_streak}|{history_days}"
    now = time.monotonic()
    cached = _LIMIT_UP_LADDER_RESULT_CACHE.get(cache_key)
    if cached is not None and now < float(cached.get("expires_at") or 0.0):
        return cached["payload"]
    payload = _compute_limit_up_ladder(min_streak, max_streak, history_days)
    if isinstance(payload, dict) and "error" in payload:
        return payload
    _LIMIT_UP_LADDER_RESULT_CACHE[cache_key] = {
        "expires_at": now + _LIMIT_UP_LADDER_RESULT_CACHE_TTL_SECONDS,
        "payload": payload,
    }
    return payload


def _compute_limit_up_ladder(
    min_streak: int,
    max_streak: int,
    history_days: int,
):
    session = next(db.get_session())
    try:
        latest_date = None
        if clickhouse_available():
            try:
                # R14（2026-11-09）：raw=True 绕过 kline_daily_quality_read 视图
                # 直查基表；MAX(trade_date) 纯聚合，与 FINAL 结果一致。
                latest_date = clickhouse_scalar(
                    """
                    SELECT MAX(k.trade_date)
                    FROM kline_daily k
                    JOIN stocks s ON s.code = k.code
                    WHERE s.type = 'stock' AND COALESCE(s.quit, false) = false
                    """,
                    raw=True,
                )
            except Exception as exc:
                logger.warning(f"ClickHouse limit-up latest date fallback to SQLAlchemy engine: {exc}")
        if not latest_date:
            latest_date = (
                session.query(func.max(KlineDaily.trade_date))
                .filter(
                    KlineDaily.code.in_(
                        session.query(Stock.code).filter(
                            Stock.type == "stock",
                            Stock.quit == False,
                        )
                    )
                )
                .scalar()
            )
        if not latest_date:
            return {
                "trade_date": None,
                "summary": {"limit_up_total": 0, "ladder_total": 0, "highest_streak": 0},
                "buckets": [],
                "history": [],
            }

        lookback_days = max(max_streak + 3, history_days + max_streak + 2)
        trade_dates_desc = []
        if clickhouse_available():
            try:
                # R14（2026-11-09）：raw=True 直查基表；DISTINCT trade_date 与
                # FINAL 去重后结果一致，trade_date <= ? 谓词随基表下推。
                date_df = clickhouse_query_df(
                    """
                    SELECT DISTINCT trade_date
                    FROM kline_daily
                    WHERE trade_date <= ?
                    ORDER BY trade_date DESC
                    LIMIT ?
                    """,
                    [_date_text(latest_date), int(lookback_days)],
                    raw=True,
                )
                if date_df is not None and not date_df.empty:
                    trade_dates_desc = [item for item in date_df["trade_date"].tolist()]
            except Exception as exc:
                logger.warning(f"ClickHouse limit-up dates fallback to SQLAlchemy engine: {exc}")
        if not trade_dates_desc:
            date_rows = (
                session.query(KlineDaily.trade_date)
                .filter(KlineDaily.trade_date <= latest_date)
                .distinct()
                .order_by(KlineDaily.trade_date.desc())
                .limit(lookback_days)
                .all()
            )
            trade_dates_desc = [row[0] for row in date_rows]
        if not trade_dates_desc:
            return {
                "trade_date": _date_text(latest_date),
                "summary": {"limit_up_total": 0, "ladder_total": 0, "highest_streak": 0},
                "buckets": [],
                "history": [],
            }

        stocks = (
            session.query(Stock)
            .filter(
                Stock.type == "stock",
                Stock.quit == False,
            )
            .all()
        )
        stock_map = {item.code: item for item in stocks}
        stock_codes = list(stock_map.keys())
        if not stock_map:
            return {
                "trade_date": _date_text(latest_date),
                "summary": {"limit_up_total": 0, "ladder_total": 0, "highest_streak": 0},
                "buckets": [],
                "history": [],
            }

        kline_rows = []
        if clickhouse_available():
            try:
                code_placeholders = ",".join(["?"] * len(stock_codes))
                date_texts = [_date_text(item) for item in trade_dates_desc]
                date_placeholders = ",".join(["?"] * len(date_texts))
                # R14（2026-11-09）：绕过 kline_daily_quality_read 视图直查基表，
                # code/日期谓词随基表 WHERE 下推；GROUP BY (code, trade_date) +
                # argMax(col, created_at) 等价 FINAL 版本语义，HAVING open > 0
                # 与原视图行上的 WHERE open > 0 一致。
                kline_df = clickhouse_query_df(
                    f"""
                    SELECT
                        code,
                        trade_date,
                        argMax(open, created_at) AS open,
                        argMax(close, created_at) AS close,
                        argMax(change_pct, created_at) AS change_pct,
                        argMax(amount, created_at) AS amount,
                        argMax(turnover_rate, created_at) AS turnover_rate
                    FROM kline_daily
                    WHERE code IN ({code_placeholders})
                      AND trade_date IN ({date_placeholders})
                    GROUP BY code, trade_date
                    HAVING open > 0
                    """,
                    stock_codes + date_texts,
                    raw=True,
                )
                if kline_df is not None:
                    kline_rows = [
                        SimpleNamespace(
                            code=row.code,
                            trade_date=_date_text(row.trade_date),
                            open=row.open,
                            close=row.close,
                            change_pct=row.change_pct,
                            amount=row.amount,
                            turnover_rate=row.turnover_rate,
                        )
                        for row in kline_df.itertuples(index=False)
                    ]
            except Exception as exc:
                logger.warning(f"ClickHouse limit-up kline fallback to SQLAlchemy engine: {exc}")

        if not kline_rows:
            kline_rows = (
                session.query(
                    KlineDaily.code,
                    KlineDaily.trade_date,
                    KlineDaily.open,
                    KlineDaily.close,
                    KlineDaily.change_pct,
                    KlineDaily.amount,
                    KlineDaily.turnover_rate,
                )
                .filter(
                    KlineDaily.code.in_(stock_codes),
                    KlineDaily.trade_date.in_(trade_dates_desc),
                    KlineDaily.open > 0,
                )
                .all()
            )

        kline_map: Dict[str, Dict[Any, Any]] = defaultdict(dict)
        for row in kline_rows:
            kline_map[row.code][_date_text(row.trade_date)] = row

        is_limit_map: Dict[str, Dict[Any, bool]] = defaultdict(dict)
        for code, date_rows_map in kline_map.items():
            stock = stock_map.get(code)
            if not stock:
                continue
            for trade_date, row in date_rows_map.items():
                is_limit_map[code][trade_date] = _is_limit_up_day(stock, row)

        buckets: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        limit_up_total = 0
        ladder_total = 0
        highest_streak = 0

        trade_dates_desc = [_date_text(item) for item in trade_dates_desc]
        latest_trade_date = trade_dates_desc[0]
        for code, stock in stock_map.items():
            latest_row = kline_map.get(code, {}).get(latest_trade_date)
            if not latest_row:
                continue

            if not is_limit_map.get(code, {}).get(latest_trade_date, False):
                continue

            limit_up_total += 1

            streak = 0
            for trade_date in trade_dates_desc:
                if is_limit_map.get(code, {}).get(trade_date, False):
                    streak += 1
                else:
                    break

            highest_streak = max(highest_streak, streak)
            if streak < min_streak:
                continue

            ladder_total += 1
            bucket = min(streak, max_streak)
            change_pct = finite_number(latest_row.change_pct)

            buckets[bucket].append(
                {
                    "code": code,
                    "name": stock.name,
                    "market": stock.market,
                    "streak": int(streak),
                    "close": round(_to_float(latest_row.close), 3),
                    "change_pct": round(change_pct, 2) if change_pct is not None else None,
                    "amount": round(_to_float(latest_row.amount), 2),
                    "turnover_rate": finite_number(latest_row.turnover_rate),
                }
            )

        bucket_items: List[Dict[str, Any]] = []
        for streak in range(max_streak, min_streak - 1, -1):
            label = f"{streak}板"
            if streak == max_streak:
                label = f"{streak}板+"

            items = sorted(
                buckets.get(streak, []),
                key=lambda item: (item.get("streak", 0), item.get("change_pct", 0), item.get("amount", 0)),
                reverse=True,
            )

            bucket_items.append(
                {
                    "streak": streak,
                    "label": label,
                    "count": len(items),
                    "items": items,
                }
            )

        history = []
        history_dates = list(reversed(trade_dates_desc[:history_days]))
        for target_date in history_dates:
            row_counts: Dict[str, int] = {}
            date_index = trade_dates_desc.index(target_date)
            for streak in range(min_streak, max_streak + 1):
                row_counts[str(streak)] = 0

            for code in stock_codes:
                if not is_limit_map.get(code, {}).get(target_date, False):
                    continue

                streak = 0
                for idx in range(date_index, len(trade_dates_desc)):
                    cursor_date = trade_dates_desc[idx]
                    if is_limit_map.get(code, {}).get(cursor_date, False):
                        streak += 1
                    else:
                        break
                if streak < min_streak:
                    continue

                bucket = min(streak, max_streak)
                row_counts[str(bucket)] = row_counts.get(str(bucket), 0) + 1

            history.append(
                {
                    "date": _date_text(target_date),
                    "buckets": row_counts,
                }
            )

        return {
            "trade_date": _date_text(latest_trade_date),
            "summary": {
                "limit_up_total": limit_up_total,
                "ladder_total": ladder_total,
                "highest_streak": highest_streak,
            },
            "params": {
                "min_streak": min_streak,
                "max_streak": max_streak,
                "history_days": history_days,
            },
            "buckets": bucket_items,
            "history": history,
        }
    except Exception as exc:
        logger.error(f"获取连板天梯失败: {exc}")
        return {"error": str(exc)}
    finally:
        session.close()


# R25（2026-11-09）：/market/emotion-trend 数据源改为按需重算（镜像 R20b）。
# 窗口上限 250 日，防无界 5m 扫描（kline_minute_5 约 9 亿行，历史深至 2000 年）。
_EMOTION_TREND_MAX_DAYS = 250


@router.get("/emotion-trend")
def get_emotion_trend(days: int = 30, granularity: str = "daily"):
    """获取情绪周期趋势数据。

    R15（2026-11-09）：进程内 TTL 结果缓存（60 秒，monotonic 时钟）。
    R25（2026-11-09）：daily 分支改按需重算（emotion_cycle 预计算表写入方
    已停用，表停在 2026-10-30）。缓存键在 days|granularity 基础上追加
    kline_daily 的 max(trade_date)（与 R20 sentiment 同语义）——数据日期
    推进即重算；键查询失败（None）fail-closed 不吃缓存；error 载荷不缓存。
    """
    data_date = _kline_daily_latest_trade_date()
    if data_date is None:
        return _compute_emotion_trend(days=days, granularity=granularity)
    cache_key = f"{int(days)}|{granularity}|{data_date}"
    now = time.monotonic()
    cached = _EMOTION_TREND_RESULT_CACHE.get(cache_key)
    if cached is not None and now < float(cached.get("expires_at") or 0.0):
        return cached["payload"]
    payload = _compute_emotion_trend(days=days, granularity=granularity)
    if isinstance(payload, dict) and "error" in payload:
        return payload
    _EMOTION_TREND_RESULT_CACHE[cache_key] = {
        "expires_at": now + _EMOTION_TREND_RESULT_CACHE_TTL_SECONDS,
        "payload": payload,
    }
    # 同参数组合只保留当前数据日期的键，防陈旧键累积
    prefix = f"{int(days)}|{granularity}|"
    for stale in [k for k in _EMOTION_TREND_RESULT_CACHE if k.startswith(prefix) and k != cache_key]:
        _EMOTION_TREND_RESULT_CACHE.pop(stale, None)
    return payload


def _emotion_trend_window_dates(latest_date: Optional[str], days: int):
    """R25：请求窗口与扫描范围——trade_calendar raw 直查（镜像 R20b）。

    取截至最新有数据交易日的 calendar 日期 days+5 行：前 days 行为窗口，
    最老一行为昨日分类/前收的缓冲起点（数据驱动 date_pairs 会再对齐到
    真实前数据日，缓冲只为覆盖偶发数据缺口）。
    """
    bounded = max(1, min(int(days), _EMOTION_TREND_MAX_DAYS))
    calendar_df = clickhouse_query_df(
        f"""
        SELECT trade_date
        FROM trade_calendar
        WHERE is_trading = 1 AND market = 'SH' AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT {bounded + 5}
        """,
        [latest_date],
        raw=True,
    )
    if calendar_df is None or calendar_df.empty:
        return [], None
    dates_desc = [_date_text(v) for v in calendar_df["trade_date"].tolist()]
    window = list(reversed(dates_desc[:bounded]))
    range_start = dates_desc[-1]
    return window, range_start


def _emotion_core_aggregates(range_start: str, latest_date: str):
    """R25：日线类比率——口径提取自写入方 scripts/generate_emotion_cycle.py
    EmotionCycleGenerator._stock_kline_rows / calculate_up_rate / calculate_ratio：

    universe = kline_daily FINAL JOIN stocks WHERE s.type='stock'（注意：写入方
    无 open>0 过滤，与 sentiment 链不同）；close_up_rate = pct>0 占比；
    sh/sz/cyb = code 前缀 6% / 0%|3% / 3%；strong = pct>3；weak = pct<-3；
    total_stocks = 全部行（change_pct NULL 计入分母、不计入分子）。
    """
    return clickhouse_query_df(
        """
        WITH base AS (
            SELECT k.trade_date AS trade_date, k.code AS code, k.change_pct AS pct
            FROM kline_daily AS k FINAL
            INNER JOIN stocks AS s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date >= ? AND k.trade_date <= ?
        )
        SELECT
            trade_date,
            count() AS total_stocks,
            sum(pct > 0) * 100 / count() AS close_up_rate,
            sumIf(pct > 0, code LIKE '6%%') * 100 / nullIf(countIf(code LIKE '6%%'), 0) AS sh_up_rate,
            sumIf(pct > 0, (code LIKE '0%%' OR code LIKE '3%%')) * 100 / nullIf(countIf(code LIKE '0%%' OR code LIKE '3%%'), 0) AS sz_up_rate,
            sumIf(pct > 0, code LIKE '3%%') * 100 / nullIf(countIf(code LIKE '3%%'), 0) AS cyb_up_rate,
            sum(pct > 3) * 100 / count() AS strong_up_rate,
            sum(pct < -3) * 100 / count() AS weak_up_rate
        FROM base
        GROUP BY trade_date
        ORDER BY trade_date
        """,
        [range_start, latest_date],
        raw=True,
    )


def _emotion_yesterday_aggregates(range_start: str, latest_date: str):
    """R25：昨日妖股/强势/弱势——口径提取自写入方 get_yesterday_stocks_by_
    category + calculate_category_up_rate：

    前数据日（lagInFrame 前一个有数据日，镜像写入方 max(trade_date)<T 的
    数据驱动语义，非日历日）pct>9.5（妖股）/ >5（强势）/ <-5（弱势）的
    代码在当日 pct>0 占比；分母=前日分类代码中当日有行的（停牌剔除，
    INNER JOIN 语义）；无候选=0.0（写入方同）。
    classified 先收敛到 |pct|>5 小集合（>9.5 与 >5 同侧、<-5 另一侧，
    不改变任何桶口径），实测 30 日窗口 0.88s→0.2s 且逐位等价（probe6）。
    """
    return clickhouse_query_df(
        """
        WITH stock_dates AS (
            SELECT DISTINCT k.trade_date AS trade_date
            FROM kline_daily AS k FINAL
            INNER JOIN stocks AS s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date >= ? AND k.trade_date <= ?
        ),
        date_pairs AS (
            SELECT trade_date,
                lagInFrame(toNullable(trade_date), 1, NULL) OVER (
                    ORDER BY trade_date ASC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                ) AS prev_date
            FROM stock_dates
        ),
        classified AS (
            SELECT k.trade_date AS prev_date, k.code AS code, k.change_pct AS pct
            FROM kline_daily AS k FINAL
            INNER JOIN stocks AS s ON s.code = k.code
            WHERE s.type = 'stock'
              AND k.trade_date >= ? AND k.trade_date <= ?
              AND (k.change_pct > 5 OR k.change_pct < -5)
        ),
        today AS (
            SELECT k.trade_date AS trade_date, k.code AS code, k.change_pct AS pct
            FROM kline_daily AS k FINAL
            INNER JOIN stocks AS s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date >= ? AND k.trade_date <= ?
        )
        SELECT
            dp.trade_date AS trade_date,
            if(countIf(y.pct > 9.5) > 0, sumIf(t.pct > 0, y.pct > 9.5) * 100 / countIf(y.pct > 9.5), 0) AS yesterday_monster_up_rate,
            if(countIf(y.pct > 5) > 0, sumIf(t.pct > 0, y.pct > 5) * 100 / countIf(y.pct > 5), 0) AS yesterday_strong_up_rate,
            if(countIf(y.pct < -5) > 0, sumIf(t.pct > 0, y.pct < -5) * 100 / countIf(y.pct < -5), 0) AS yesterday_weak_up_rate
        FROM date_pairs AS dp
        INNER JOIN classified AS y ON y.prev_date = dp.prev_date
        INNER JOIN today AS t ON t.code = y.code AND t.trade_date = dp.trade_date
        GROUP BY dp.trade_date
        ORDER BY dp.trade_date
        """,
        [range_start, latest_date] * 4,
        raw=True,
    )


def _emotion_intraday_aggregates(range_start: str, latest_date: str, window_start: str):
    """R25：intraday_up_rate 真口径——表存值是写入方占位（generate_emotion_
    cycle.py L301-302 "盘中上涨率：暂时用收盘上涨率代替"），R25 改为
    kline_minute_5 当日末根 close > 前数据日收盘（kline_daily argMax(close,
    created_at) 去重）：

    · 基表 raw 直查；(code, datetime) argMax(col, created_at) 去重（R14/
      R20b 模式）与 (code, 日) argMax(close, tuple(datetime, created_at))
      取末根两层合成，单次扫描；
    · datetime 范围谓词（toDate 谓词在 toYYYYMM 分区实测不裁剪）；
    · R27b：kline_minute_5 datetime 已标注 Asia/Shanghai（北京钟），北京
      交易日时段即同日 09:35~15:00，toDate(datetime) 与 trade_date 同日对齐；
    · toHour>=13（北京 13:00 后）预过滤为纯优化：与全量逐日零差异
      （covered/rate diff 全 0，probe5 2026-11-09），扫描量 48→13 根/日，
      30 日窗口 2.74s→0.95s。
    """
    return clickhouse_query_df(
        """
        WITH stock_dates AS (
            SELECT DISTINCT k.trade_date AS trade_date
            FROM kline_daily AS k FINAL
            INNER JOIN stocks AS s ON s.code = k.code
            WHERE s.type = 'stock' AND k.trade_date >= ? AND k.trade_date <= ?
        ),
        date_pairs AS (
            SELECT trade_date,
                lagInFrame(toNullable(trade_date), 1, NULL) OVER (
                    ORDER BY trade_date ASC ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                ) AS prev_date
            FROM stock_dates
        ),
        last_bar AS (
            SELECT code, toDate(datetime) AS trade_date,
                argMax(close, tuple(datetime, created_at)) AS last_close
            FROM kline_minute_5
            WHERE datetime >= ? AND datetime < ? + INTERVAL 1 DAY
              AND toHour(datetime) >= 13
            GROUP BY code, trade_date
        ),
        prev_close AS (
            SELECT k.code AS code, dp.trade_date AS trade_date,
                argMax(k.close, k.created_at) AS close
            FROM kline_daily AS k FINAL
            INNER JOIN date_pairs AS dp ON dp.prev_date = k.trade_date
            WHERE k.trade_date >= ? AND k.trade_date <= ?
            GROUP BY k.code, dp.trade_date
        )
        SELECT
            lb.trade_date AS trade_date,
            count() AS covered_stocks,
            sum(lb.last_close > pc.close) * 100 / count() AS intraday_up_rate
        FROM last_bar AS lb
        INNER JOIN prev_close AS pc ON pc.code = lb.code AND pc.trade_date = lb.trade_date
        INNER JOIN stocks AS s ON s.code = lb.code
        WHERE s.type = 'stock' AND lb.last_close > 0 AND pc.close > 0
        GROUP BY lb.trade_date
        ORDER BY lb.trade_date
        """,
        [range_start, latest_date, window_start, latest_date, range_start, latest_date],
        raw=True,
    )


def _compute_emotion_trend(days: int = 30, granularity: str = "daily"):
    """获取情绪周期趋势数据（原始计算路径）。

    R25（2026-11-09）：daily 分支改按需重算（镜像 R20b sentiment 模式）——
    emotion_cycle 预计算表写入方（api/system_config.py 情绪周期计算/修复
    任务）已停用，表停在 2026-10-30。现对请求窗口逐日从 kline_daily FINAL
    + stocks 算日线类比率、kline_minute_5 末根算 intraday，全部 market_
    warehouse raw=True 基表直查 + 日期范围谓词；口径提取自写入方
    scripts/generate_emotion_cycle.py（对拍报告见任务书 R25）。ORM/表回退
    路径删除；granularity=hourly 分支行为保持（仅查询 raw 化）。
    """
    try:
        if granularity == "hourly":
            if clickhouse_available() and _has_clickhouse_table("kline_minute_60"):
                query_days = max(1, min(int(days), 90))
                trend_df = clickhouse_query_df(
                    f"""
                    SELECT
                        toStartOfHour(k.datetime) AS hour_time,
                        avg(if(k.close > k.open, 1, 0)) * 100 AS close_up_rate,
                        avg(if(k.close > k.open, 1, 0)) * 100 AS intraday_up_rate,
                        avg(if(k.code LIKE '%.SH' AND k.close > k.open, 1, if(k.code LIKE '%.SH', 0, NULL))) * 100 AS sh_up_rate,
                        avg(if(k.code LIKE '%.SZ' AND k.close > k.open, 1, if(k.code LIKE '%.SZ', 0, NULL))) * 100 AS sz_up_rate,
                        avg(if((k.code LIKE '300%.SZ' OR k.code LIKE '301%.SZ') AND k.close > k.open, 1, if((k.code LIKE '300%.SZ' OR k.code LIKE '301%.SZ'), 0, NULL))) * 100 AS cyb_up_rate,
                        avg(if(k.open > 0 AND (k.close - k.open) / k.open * 100 > 3, 1, 0)) * 100 AS strong_up_rate,
                        avg(if(k.open > 0 AND (k.close - k.open) / k.open * 100 < -3, 1, 0)) * 100 AS weak_up_rate,
                        count() AS total_rows
                    FROM kline_minute_60 k
                    ANY LEFT JOIN stocks s ON s.code = k.code
                    WHERE k.datetime >= now() - INTERVAL {query_days} DAY
                      AND s.type = 'stock'
                      AND toHour(k.datetime) BETWEEN 9 AND 15
                    GROUP BY hour_time
                    HAVING total_rows >= 100
                    ORDER BY hour_time DESC
                    LIMIT 240
                    """,
                    raw=True,
                )
                if trend_df is not None and not trend_df.empty:
                    trend_df = trend_df.iloc[::-1]
                    close_values = trend_df["close_up_rate"].fillna(0).tolist()
                    strong_values = trend_df["strong_up_rate"].fillna(0).tolist()
                    weak_values = trend_df["weak_up_rate"].fillna(0).tolist()
                    return {
                        "dates": [_datetime_text(v) for v in trend_df["hour_time"].tolist()],
                        "close_up_rate": [float(v) for v in close_values],
                        "intraday_up_rate": [float(v) for v in trend_df["intraday_up_rate"].fillna(0).tolist()],
                        "sh_up_rate": [float(v) for v in trend_df["sh_up_rate"].fillna(0).tolist()],
                        "sz_up_rate": [float(v) for v in trend_df["sz_up_rate"].fillna(0).tolist()],
                        "cyb_up_rate": [float(v) for v in trend_df["cyb_up_rate"].fillna(0).tolist()],
                        "yesterday_monster_up_rate": [float(v) for v in close_values],
                        "yesterday_strong_up_rate": [float(v) for v in strong_values],
                        "yesterday_weak_up_rate": [float(v) for v in weak_values],
                        "strong_up_rate": [float(v) for v in strong_values],
                        "weak_up_rate": [float(v) for v in weak_values],
                        "limit_up_follow_rate": [0.0 for _ in close_values],
                        "total": int(len(trend_df)),
                        "granularity": "hourly",
                        "source": "clickhouse:kline_minute_60",
                    }
            return _empty_emotion_trend("数据库中没有可用的60分钟K线情绪数据")

        if not clickhouse_available():
            return _empty_emotion_trend("数据库中没有情绪周期数据")
        for table in ("kline_daily", "stocks", "trade_calendar"):
            if not _has_clickhouse_table(_resolve_table_name(table)):
                return _empty_emotion_trend("数据库中没有情绪周期数据")
        latest_date = _kline_daily_latest_trade_date()
        if not latest_date:
            return _empty_emotion_trend("数据库中没有情绪周期数据")
        window_dates, range_start = _emotion_trend_window_dates(latest_date, days)
        if not window_dates or not range_start:
            return _empty_emotion_trend("数据库中没有情绪周期数据")

        core_df = _emotion_core_aggregates(range_start, latest_date)
        if core_df is None or core_df.empty:
            return _empty_emotion_trend("数据库中没有情绪周期数据")
        core_map = {
            _date_text(v): row
            for v, row in zip(core_df["trade_date"].tolist(), core_df.to_dict("records"))
        }
        yday_df = _emotion_yesterday_aggregates(range_start, latest_date)
        yday_map: Dict[str, Dict[str, Any]] = {}
        if yday_df is not None and not yday_df.empty:
            yday_map = {
                _date_text(v): row
                for v, row in zip(yday_df["trade_date"].tolist(), yday_df.to_dict("records"))
            }
        intra_df = _emotion_intraday_aggregates(range_start, latest_date, window_dates[0])
        intra_map: Dict[str, float] = {}
        if intra_df is not None and not intra_df.empty:
            intra_map = {
                _date_text(v): float(row.get("intraday_up_rate") or 0.0)
                for v, row in zip(intra_df["trade_date"].tolist(), intra_df.to_dict("records"))
            }

        min_stocks = int(MIN_EMOTION_CYCLE_STOCKS)
        dates = [
            d for d in window_dates
            if d in core_map and int(core_map[d].get("total_stocks") or 0) >= min_stocks
        ]
        if not dates:
            return _empty_emotion_trend("数据库中没有情绪周期数据")

        def _num(value, default=0.0):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            return default if value != value else round(value, 2)

        def _core(d: str, field: str) -> float:
            return _num(core_map[d].get(field))

        payload = {
            "dates": dates,
            "close_up_rate": [_core(d, "close_up_rate") for d in dates],
            # 5m 无覆盖日回退收盘口径（保持写入方 intraday=close 占位语义）
            "intraday_up_rate": [
                _num(intra_map[d]) if d in intra_map else _core(d, "close_up_rate")
                for d in dates
            ],
            "sh_up_rate": [_core(d, "sh_up_rate") for d in dates],
            "sz_up_rate": [_core(d, "sz_up_rate") for d in dates],
            "cyb_up_rate": [_core(d, "cyb_up_rate") for d in dates],
            "yesterday_monster_up_rate": [
                _num(yday_map.get(d, {}).get("yesterday_monster_up_rate")) for d in dates
            ],
            "yesterday_strong_up_rate": [
                _num(yday_map.get(d, {}).get("yesterday_strong_up_rate")) for d in dates
            ],
            "yesterday_weak_up_rate": [
                _num(yday_map.get(d, {}).get("yesterday_weak_up_rate")) for d in dates
            ],
            "strong_up_rate": [_core(d, "strong_up_rate") for d in dates],
            "weak_up_rate": [_core(d, "weak_up_rate") for d in dates],
            # 涨停溢价率：写入方常量占位 0.0（generate_emotion_cycle.py L341-342）
            "limit_up_follow_rate": [0.0 for _ in dates],
            "total": len(dates),
            "source": "clickhouse",
        }
        return payload
    except Exception as exc:
        logger.error(f"获取情绪周期趋势失败: {exc}")
        return _empty_emotion_trend(str(exc))


@router.post("/calculate-emotion-cycle")
def calculate_emotion_cycle():
    """计算最新交易日情绪周期数据。"""
    session = next(db.get_session())
    try:
        trade_date = _latest_daily_trade_date_from_calendar()
        latest_kline = None if trade_date else session.query(KlineDaily).order_by(KlineDaily.trade_date.desc()).first()
        if not trade_date and not latest_kline:
            return {"success": False, "error": "数据库中没有K线数据，请先获取行情数据"}

        if not trade_date:
            trade_date = latest_kline.trade_date
        existing = session.query(EmotionCycle).filter(EmotionCycle.date == trade_date).first()
        if existing:
            session.delete(existing)
            session.commit()

        from scripts.generate_emotion_cycle import EmotionCycleGenerator, publish_emotion_cycle_to_clickhouse

        generator = EmotionCycleGenerator()
        generator.session = session
        emotion_cycle = generator.generate_emotion_cycle(trade_date)

        if not emotion_cycle:
            return {"success": False, "error": f"{trade_date} 情绪周期数据计算失败"}

        session.add(emotion_cycle)
        session.commit()
        publish_emotion_cycle_to_clickhouse(emotion_cycle)

        return {
            "success": True,
            "date": trade_date.isoformat(),
            "data": {
                "close_up_rate": float(emotion_cycle.close_up_rate),
                "intraday_up_rate": float(emotion_cycle.intraday_up_rate),
                "sh_up_rate": float(emotion_cycle.sh_up_rate),
                "sz_up_rate": float(emotion_cycle.sz_up_rate),
                "cyb_up_rate": float(emotion_cycle.cyb_up_rate),
                "yesterday_monster_up_rate": float(emotion_cycle.yesterday_monster_up_rate),
                "yesterday_strong_up_rate": float(emotion_cycle.yesterday_strong_up_rate),
                "yesterday_weak_up_rate": float(emotion_cycle.yesterday_weak_up_rate),
                "strong_up_rate": float(emotion_cycle.strong_up_rate),
                "weak_up_rate": float(emotion_cycle.weak_up_rate),
                "limit_up_follow_rate": float(emotion_cycle.limit_up_follow_rate),
                "total_stocks": emotion_cycle.total_stocks,
            },
        }
    except Exception as exc:
        logger.error(f"计算情绪周期数据失败: {exc}")
        session.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        session.close()
