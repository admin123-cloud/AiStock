"""
股票列表和详情API
"""

from typing import Optional, List, Dict, Any, Callable
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy import func, and_, or_, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from datetime import datetime, timedelta, time as dt_time
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace
import io
import json
import re
import threading
import os
import time
from statistics import median
from urllib.parse import quote
from uuid import uuid4

import pandas as pd
from zoneinfo import ZoneInfo

from utils.database import db
from utils.market_warehouse import (
    clean_minute_bars_df,
    clickhouse_available,
    clickhouse_table_exists,
    clickhouse_query_df,
    clickhouse_scalar,
)
from models.stock_models import (
    Stock,
    Sector,
    SectorStock,
    KlineDaily,
    KlineWeekly,
    KlineMonthly,
    KlineQuarterly,
    KlineMinute1,
    KlineMinute5,
    KlineMinute15,
    KlineMinute30,
    KlineMinute60,
    SelectorRun,
    SelectorRunRecord,
)
from utils.logger import get_logger

# 分钟线模型映射
MINUTE_MODEL_MAP = {
    1: KlineMinute1,
    5: KlineMinute5,
    15: KlineMinute15,
    30: KlineMinute30,
    60: KlineMinute60
}

router = APIRouter(prefix="/stocks", tags=["股票"])
logger = get_logger("stocks")


def _legacy_selector_removed():
    raise HTTPException(status_code=410, detail="legacy single-strategy stock selectors have been removed; use the system strategy workflow")

CLICKHOUSE_KLINE_TABLES = {
    "1d": ("kline_daily", "trade_date"),
    "15m": ("kline_minute_15", "datetime"),
    "30m": ("kline_minute_30", "datetime"),
    "60m": ("kline_minute_60", "datetime"),
}

INTRADAY_DAILY_TABLE = "kline_daily_intraday"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _minute_datetime_to_business_time(value: Any) -> datetime:
    """Return a legacy minute-bar timestamp as an Asia/Shanghai wall time.

    The current full-push writer persisted QMT's China-market wall clock as a
    naive UTC value.  Consequently all minute bars are eight hours ahead in
    ClickHouse (for example, 17:35 instead of the 09:35 opening bar).  Keep
    the correction at the API boundary until the archived minute tables can be
    repaired, so every intraday UI consumer receives the actual trading time.
    """
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("minute timestamp is empty")
    # ClickHouse client may attach Asia/Shanghai to the column on read, but
    # that does not repair the persisted clock value itself.  The stored value
    # is still consistently eight hours ahead of the actual market session.
    return timestamp.to_pydatetime().replace(tzinfo=None) - timedelta(hours=8)


def _clickhouse_code_filter_params(code: str) -> tuple[str, str]:
    short_code = (code or "")[:6]
    return code, short_code


def _build_intraday_daily_bar(code: str) -> Optional[Dict[str, Any]]:
    """Aggregate today's completed 5-minute bars into a display-only daily bar.

    The result is deliberately kept out of ``kline_daily``.  Formal daily bars
    are after-close facts used by strategy and historical calculations; this
    object is only a provisional UI snapshot.
    """
    if not clickhouse_available() or not clickhouse_table_exists("kline_minute_5"):
        return None

    now = datetime.now(BUSINESS_TZ)
    if now.weekday() >= 5 or not (dt_time(9, 30) <= now.time() <= dt_time(15, 35)):
        return None
    trade_date = now.date()
    try:
        snapshot_df = clickhouse_query_df(
            """
            SELECT
                argMin(open, datetime) AS open,
                max(high) AS high,
                min(low) AS low,
                argMax(close, datetime) AS close,
                sum(volume) AS volume,
                sum(amount) AS amount,
                max(datetime) AS as_of
            FROM kline_minute_5 FINAL
            WHERE code = ?
              AND toDate(datetime) = ?
            """,
            [code, trade_date],
        )
        if snapshot_df is None or snapshot_df.empty:
            return None
        row = snapshot_df.iloc[0]
        as_of = row.get("as_of")
        close = float(row.get("close") or 0)
        if as_of is None or close <= 0:
            return None

        previous_close = clickhouse_scalar(
            """
            SELECT close
            FROM kline_daily FINAL
            WHERE code = ?
              AND trade_date < ?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            [code, trade_date],
        )
        previous_close = float(previous_close or 0)
        high = float(row.get("high") or close)
        low = float(row.get("low") or close)
        change_amount = close - previous_close if previous_close > 0 else None
        change_pct = change_amount / previous_close * 100 if previous_close > 0 else None
        amplitude = (high - low) / previous_close * 100 if previous_close > 0 else None
        as_of_dt = _minute_datetime_to_business_time(as_of)
        snapshot_at = now
        payload = {
            "open": float(row.get("open") or close),
            "high": high,
            "low": low,
            "close": close,
            # kline_minute_5 already follows the persisted lots/yuan contract.
            # Do not divide stock volume again when aggregating a provisional
            # daily bar for the page.
            "volume": float(row.get("volume") or 0),
            "amount": float(row.get("amount") or 0),
            "date": trade_date.isoformat(),
            "previous_close": previous_close or None,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "amplitude": amplitude,
            "is_provisional": True,
            "as_of": as_of_dt.isoformat(sep=" ", timespec="seconds"),
            "snapshot_at": now.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"),
            "source": "clickhouse:kline_minute_5",
        }
        # Cache the same display-only fact for list/overview consumers.  A
        # ReplacingMergeTree row is append-only and never mutates formal daily data.
        if clickhouse_table_exists(INTRADAY_DAILY_TABLE):
            try:
                from utils.market_warehouse import clickhouse_client

                clickhouse_client().insert(
                    INTRADAY_DAILY_TABLE,
                    [[
                        code, trade_date, payload["open"], high, low, close,
                        payload["volume"], payload["amount"], previous_close or None,
                        amplitude, change_pct, change_amount, 0.0, snapshot_at,
                        "clickhouse:kline_minute_5", 1,
                    ]],
                    column_names=[
                        "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
                        "previous_close", "amplitude", "change_pct", "change_amount", "turnover_rate",
                        "snapshot_at", "source", "is_provisional",
                    ],
                )
            except Exception as exc:
                logger.warning(f"intraday daily snapshot cache write failed: code={code}, error={exc}")
        return payload
    except Exception as exc:
        logger.warning(f"intraday daily aggregation failed: code={code}, error={exc}")
        return None


def _overlay_fresh_intraday_list_kline(
    stocks: List[Any], latest_kline_map: Dict[str, Any]
) -> Dict[str, Any]:
    """Overlay the current page with today's fresh 5-minute bars.

    The stock list must never present yesterday's close as though it were a
    current quote.  This intentionally stays display-only: formal daily bars
    remain after-close facts in ``kline_daily`` while a live page is enriched
    from the small set of codes the user is actually viewing.
    """
    if not stocks or not clickhouse_available() or not clickhouse_table_exists("kline_minute_5"):
        return latest_kline_map

    now = datetime.now(BUSINESS_TZ)
    if now.weekday() >= 5 or not (dt_time(9, 30) <= now.time() <= dt_time(15, 35)):
        return latest_kline_map

    codes = [stock.code for stock in stocks if getattr(stock, "code", None)]
    if not codes:
        return latest_kline_map

    try:
        placeholders = ",".join(["?"] * len(codes))
        intraday_df = clickhouse_query_df(
            f"""
            SELECT
                code,
                argMax(close, datetime) AS close,
                sum(amount) AS amount,
                max(datetime) AS as_of
            FROM kline_minute_5 FINAL
            WHERE code IN ({placeholders})
              AND toDate(datetime) = ?
            GROUP BY code
            """,
            [*codes, now.date()],
        )
        if intraday_df is None or intraday_df.empty:
            return latest_kline_map

        result = dict(latest_kline_map)
        now_wall_time = now.replace(tzinfo=None)
        for bar in intraday_df.itertuples(index=False):
            close = float(bar.close or 0)
            if close <= 0 or bar.as_of is None:
                continue
            as_of = _minute_datetime_to_business_time(bar.as_of)
            # Do not use a stalled minute feed as a live quote.  The normal
            # five-minute publisher may be a little late, hence a 20-minute
            # tolerance before the daily close remains the honest fallback.
            if as_of.date() != now.date() or now_wall_time - as_of > timedelta(minutes=20):
                continue
            previous = latest_kline_map.get(bar.code)
            previous_close = float(getattr(previous, "close", 0) or 0)
            change_pct = (close / previous_close - 1) * 100 if previous_close > 0 else None
            result[bar.code] = SimpleNamespace(
                trade_date=now.date(),
                close=close,
                change_pct=change_pct,
                amount=float(bar.amount or 0),
                is_provisional=True,
                as_of=as_of,
                source="clickhouse:kline_minute_5",
            )
        return result
    except Exception as exc:
        logger.warning(f"ClickHouse stock list intraday overlay failed: {exc}")
        return latest_kline_map


def _as_date(value: Any):
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "date") and not isinstance(value, str):
        return value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _latest_daily_trade_date(session: Session):
    if clickhouse_available():
        try:
            latest = clickhouse_scalar(
                """
                SELECT MAX(k.trade_date)
                FROM kline_daily k
                JOIN trade_calendar c
                  ON c.trade_date = k.trade_date
                 AND c.market = 'SH'
                 AND c.is_trading = 1
                """
            )
            if latest:
                return _as_date(latest)
        except Exception as exc:
            logger.warning(f"ClickHouse latest daily trade date fallback to SQLAlchemy engine: {exc}")
    return session.query(func.max(KlineDaily.trade_date)).scalar()


def _load_daily_bars_clickhouse(
    code: str,
    end_date,
    *,
    start_date=None,
    limit: Optional[int] = None,
    ascending: bool = True,
) -> Optional[List[Any]]:
    if not clickhouse_available():
        return None
    try:
        conditions = ["(code = ? OR substr(code, 1, 6) = ?)"]
        params: List[Any] = [code, (code or "")[:6]]
        if start_date is not None:
            conditions.append("trade_date >= ?")
            params.append(str(start_date))
        conditions.append("trade_date <= ?")
        params.append(str(end_date))
        order_direction = "ASC" if ascending else "DESC"
        limit_sql = ""
        if limit:
            limit_sql = " LIMIT ?"
            params.append(int(limit))
        df = clickhouse_query_df(
            f"""
            SELECT trade_date, open, high, low, close, volume, amount, change_pct, turnover_rate
            FROM kline_daily
            WHERE {' AND '.join(conditions)}
            ORDER BY trade_date {order_direction}
            {limit_sql}
            """,
            params,
        )
        if df is None:
            return None
        return [
            SimpleNamespace(
                trade_date=_as_date(row.trade_date),
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                amount=row.amount,
                change_pct=row.change_pct,
                turnover_rate=row.turnover_rate,
            )
            for row in df.itertuples(index=False)
        ]
    except Exception as exc:
        logger.warning(f"ClickHouse daily bars fallback to SQLAlchemy engine: {exc}")
        return None


def _load_recent_daily_bars_for_codes_clickhouse(
    codes: List[str],
    end_date,
    bars_needed: int,
    chunk_size: int = 800,
) -> Optional[Dict[str, List[Any]]]:
    if not codes or not clickhouse_available():
        return None
    grouped: Dict[str, List[Any]] = {}
    try:
        date_df = clickhouse_query_df(
            """
            SELECT DISTINCT trade_date
            FROM kline_daily
            WHERE trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [str(end_date), int(bars_needed)],
        )
        if date_df is None or date_df.empty:
            return grouped
        trade_dates = [str(value)[:10] for value in date_df["trade_date"].tolist()]
        date_placeholders = ",".join(["?"] * len(trade_dates))
        for offset in range(0, len(codes), chunk_size):
            chunk = codes[offset: offset + chunk_size]
            code_placeholders = ",".join(["?"] * len(chunk))
            df = clickhouse_query_df(
                f"""
                SELECT code, trade_date, open, high, low, close, volume, amount, change_pct, turnover_rate
                FROM kline_daily
                WHERE trade_date IN ({date_placeholders})
                  AND code IN ({code_placeholders})
                ORDER BY code, trade_date DESC
                """,
                trade_dates + chunk,
            )
            if df is None or df.empty:
                continue
            for row in df.itertuples(index=False):
                grouped.setdefault(row.code, []).append(
                    SimpleNamespace(
                        trade_date=_as_date(row.trade_date),
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        volume=row.volume,
                        amount=row.amount,
                        change_pct=row.change_pct,
                        turnover_rate=row.turnover_rate,
                    )
                )
        return grouped
    except Exception as exc:
        logger.warning(f"ClickHouse bulk daily bars fallback to SQLAlchemy engine: {exc}")
        return None

uptrend_backtest_tasks: Dict[str, Dict[str, Any]] = {}
uptrend_backtest_lock = threading.Lock()

NEW_HIGH_SELECTOR_TYPES = {
    "1m": {"label": "1个月新高", "lookback_days": 20},
    "3m": {"label": "3个月新高", "lookback_days": 60},
    "1y": {"label": "1年新高", "lookback_days": 250},
    "all": {"label": "历史新高", "lookback_days": None},
}

RECENT_HIGH_GUARD_SMALL_FLOAT_CAP_YI = 50.0
RECENT_HIGH_GUARD_MID_FLOAT_CAP_YI = 200.0
RECENT_HIGH_GUARD_RATIO = 1.2
RECENT_HIGH_GUARD_LOOKBACK_SMALL = 120
RECENT_HIGH_GUARD_LOOKBACK_MID = 250





class DatabaseSessionManager:
    """数据库会话管理器"""
    
    @staticmethod
    def get_session() -> Session:
        """获取数据库会话"""
        return next(db.get_session())
    
    @staticmethod
    def close_session(session: Session):
        """关闭数据库会话"""
        try:
            session.close()
        except Exception as e:
            logger.error(f"关闭数据库会话失败: {e}")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _get_stock_by_code(session, code: str) -> Optional[SimpleNamespace]:
    """用原生 SQL 查询单只股票，兼容 ClickHouse（避免 ORM 列别名问题）。"""
    try:
        row = session.execute(
            text(
                """
                SELECT code, name, market, type, region, industry, industry_code,
                       st, quit, list_date, delist_date, self_selected, holding, float_share, total_share
                FROM stocks
                WHERE code = :code
                LIMIT 1
                """
            ),
            {"code": code},
        ).fetchone()
        if not row:
            return None
        return SimpleNamespace(
            code=row[0],
            name=row[1],
            market=row[2],
            type=row[3],
            region=row[4],
            industry=row[5],
            industry_code=row[6],
            st=bool(row[7]),
            quit=bool(row[8]),
            list_date=row[9],
            delist_date=row[10],
            self_selected=bool(row[11]) if row[11] is not None else False,
            holding=bool(row[12]) if row[12] is not None else False,
            float_share=row[13],
            total_share=row[14],
            created_at=None,
            updated_at=None,
        )
    except Exception:
        # 降级到 ORM（非 ClickHouse 环境）
        return session.query(Stock).filter(Stock.code == code).first()


def _truncate_text_by_utf8_bytes(text: str, max_bytes: int) -> str:
    raw = (text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return text or ""
    truncated = raw[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def _prepare_summary_for_storage(summary: Optional[Dict[str, Any]], max_bytes: int = 48_000) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(summary or {})

    def _current_size() -> int:
        return len(_json_dumps(payload).encode("utf-8"))

    if _current_size() <= max_bytes:
        return payload

    if isinstance(payload.get("recent_cases"), list):
        payload["recent_cases"] = payload["recent_cases"][:80]
    if _current_size() <= max_bytes:
        payload["summary_truncated"] = True
        return payload

    perf = payload.get("perf")
    if isinstance(perf, dict):
        if isinstance(perf.get("top_slowest_stocks"), list):
            perf["top_slowest_stocks"] = perf["top_slowest_stocks"][:10]
        if _current_size() > max_bytes:
            payload.pop("perf", None)
    if _current_size() <= max_bytes:
        payload["summary_truncated"] = True
        return payload

    conclusions = payload.get("conclusions")
    if isinstance(conclusions, list):
        payload["conclusions"] = [
            _truncate_text_by_utf8_bytes(str(item), 240) for item in conclusions[:3]
        ]
    if _current_size() <= max_bytes:
        payload["summary_truncated"] = True
        return payload

    minimal = {
        "period": payload.get("period"),
        "overall": payload.get("overall"),
        "by_signal_type": payload.get("by_signal_type"),
        "ongoing_samples": payload.get("ongoing_samples"),
        "matured_samples": payload.get("matured_samples"),
        "conclusions": ["摘要过长，已自动截断存储。"],
        "summary_truncated": True,
    }
    return minimal


def _is_mysql_table_definition_changed_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    msg = str(exc).lower()
    if "table definition has changed" in msg:
        return True
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None)
    if isinstance(args, (list, tuple)) and args:
        try:
            return int(args[0]) == 1412
        except Exception:
            return False
    return False


def _overwrite_selector_run_by_id_with_retry(
    run_id: int,
    selector_name: str,
    run_type: str,
    params: Dict[str, Any],
    summary: Dict[str, Any],
    items: List[Dict[str, Any]],
    signal_events: Optional[List[Dict[str, Any]]] = None,
    extra_records: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    status: str = "completed",
    max_retries: int = 8,
) -> int:
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        session = DatabaseSessionManager.get_session()
        try:
            return _overwrite_selector_run_by_id(
                session=session,
                run_id=run_id,
                selector_name=selector_name,
                run_type=run_type,
                params=params,
                summary=summary,
                items=items,
                signal_events=signal_events,
                extra_records=extra_records,
                status=status,
            )
        except Exception as exc:
            try:
                session.rollback()
            except Exception:
                pass
            last_exc = exc
            if _is_mysql_table_definition_changed_error(exc) and attempt < max_retries:
                try:
                    db.engine.dispose()
                except Exception:
                    pass
                time.sleep(min(0.5 * attempt, 3.0))
                continue
            raise
        finally:
            DatabaseSessionManager.close_session(session)
    if last_exc:
        raise last_exc
    raise RuntimeError("overwrite selector run failed")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _get_active_selector_stocks(session: Session):
    return session.query(Stock).filter(
        Stock.type == "stock",
        Stock.quit == False,
        Stock.st == False,
        ~Stock.name.like("ST%"),
        ~Stock.name.like("*ST%"),
    ).all()


def _avg(values: List[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _rolling_mean(values: List[float], period: int) -> List[Optional[float]]:
    result: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result

    rolling_sum = 0.0
    for idx, value in enumerate(values):
        rolling_sum += value
        if idx >= period:
            rolling_sum -= values[idx - period]
        if idx >= period - 1:
            result[idx] = rolling_sum / period
    return result


def _round_or_none(value: Optional[float], digits: int = 3) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _resolve_float_market_cap_yi(stock: Stock, latest_close: float) -> float:
    float_share_wan = _to_float(getattr(stock, "float_share", None), 0.0)
    if float_share_wan <= 0:
        float_share_wan = _to_float(getattr(stock, "total_share", None), 0.0)
    if float_share_wan <= 0 or latest_close <= 0:
        return 0.0
    return (float_share_wan * 10000 * latest_close) / 1e8


def _passes_recent_high_guard(
    highs: List[float],
    latest_idx: int,
    reference_price: float,
    float_market_cap_yi: float,
) -> bool:
    if reference_price <= 0 or latest_idx <= 0:
        return True
    if float_market_cap_yi < RECENT_HIGH_GUARD_SMALL_FLOAT_CAP_YI:
        lookback_days = RECENT_HIGH_GUARD_LOOKBACK_SMALL
    elif float_market_cap_yi <= RECENT_HIGH_GUARD_MID_FLOAT_CAP_YI:
        lookback_days = RECENT_HIGH_GUARD_LOOKBACK_MID
    else:
        return True
    window_start = max(0, latest_idx - lookback_days + 1)
    window_highs = highs[window_start:latest_idx + 1]
    if not window_highs:
        return True
    max_allowed_high = reference_price * RECENT_HIGH_GUARD_RATIO
    return max(window_highs) <= max_allowed_high


def _analyze_uptrend_candidate(
    stock: Stock,
    daily_bars: List[KlineDaily],
    config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not daily_bars:
        return None

    closes = [float(bar.close or 0) for bar in daily_bars]
    highs = [float(bar.high or 0) for bar in daily_bars]
    lows = [float(bar.low or 0) for bar in daily_bars]
    volumes = [float(bar.volume or 0) for bar in daily_bars]
    dates = [bar.trade_date for bar in daily_bars]
    if any(value <= 0 for value in closes[-config["required_positive_window"]:]):
        return None

    ma_short_list = _rolling_mean(closes, config["ma_short"])
    ma_mid_list = _rolling_mean(closes, config["ma_mid"])
    ma_long_list = _rolling_mean(closes, config["ma_long"])
    latest_idx = len(closes) - 1
    latest_close = closes[latest_idx]
    latest_high = highs[latest_idx]
    latest_volume = volumes[latest_idx]
    latest_date = dates[latest_idx]
    total_share_wan = float(stock.total_share or 0)
    total_market_cap_yi = (total_share_wan * 10000 * latest_close) / 1e8 if total_share_wan > 0 and latest_close > 0 else 0.0
    if total_market_cap_yi < config["min_total_market_cap_yi"]:
        return None
    float_market_cap_yi = _resolve_float_market_cap_yi(stock, latest_close)

    ma_short = ma_short_list[latest_idx]
    ma_mid = ma_mid_list[latest_idx]
    ma_long = ma_long_list[latest_idx]
    if ma_short is None or ma_mid is None or ma_long is None:
        return None

    slope_idx = latest_idx - config["ma_slope_days"]
    ma_mid_prev = ma_mid_list[slope_idx] if slope_idx >= 0 else None
    ma_long_prev = ma_long_list[slope_idx] if slope_idx >= 0 else None
    if ma_mid_prev in (None, 0) or ma_long_prev in (None, 0):
        return None

    box_window = config["box_lookback_days"]
    if len(closes) < box_window + 2:
        return None

    box_slice_start = len(closes) - box_window - 1
    box_slice_end = len(closes) - 1
    base_highs = highs[box_slice_start:box_slice_end]
    base_lows = lows[box_slice_start:box_slice_end]
    base_closes = closes[box_slice_start:box_slice_end]
    if len(base_closes) < config["min_box_days"]:
        return None

    box_top = max(base_highs)
    box_bottom = min(base_lows)
    if box_bottom <= 0 or box_top <= 0 or box_top <= box_bottom:
        return None

    box_range_pct = (box_top - box_bottom) / box_bottom
    base_drift_pct = abs(base_closes[-1] / base_closes[0] - 1.0) if base_closes[0] > 0 else 999
    distance_to_box_top_pct = latest_close / box_top - 1.0
    breakout_pct = latest_high / box_top - 1.0
    box_position_pct = (latest_close - box_bottom) / (box_top - box_bottom) if box_top > box_bottom else 0.0

    prev_volume_window = volumes[max(0, latest_idx - config["volume_window"]):latest_idx]
    prev_volume_avg = _avg(prev_volume_window)
    volume_ratio = latest_volume / prev_volume_avg if prev_volume_avg > 0 else 0.0

    ma_mid_slope_pct = ma_mid / ma_mid_prev - 1.0
    ma_long_slope_pct = ma_long / ma_long_prev - 1.0
    ret_20 = latest_close / closes[latest_idx - 20] - 1.0 if latest_idx >= 20 and closes[latest_idx - 20] > 0 else 0.0
    ret_60 = latest_close / closes[latest_idx - 60] - 1.0 if latest_idx >= 60 and closes[latest_idx - 60] > 0 else 0.0

    base_ok = (
        box_range_pct <= config["box_range_pct_max"]
        and base_drift_pct <= config["base_drift_pct_max"]
        and box_window >= config["min_box_days"]
    )
    ma_bull = ma_short > ma_mid > ma_long
    ma_ready = ma_short >= ma_mid and ma_mid >= ma_long * config["ready_ma_long_ratio"]
    slope_up = ma_mid_slope_pct >= config["ma_mid_slope_min"] and ma_long_slope_pct >= config["ma_long_slope_min"]
    near_box_top = distance_to_box_top_pct >= -config["near_breakout_gap_pct"]
    breakout_confirmed = latest_close >= box_top * (1.0 + config["breakout_pct_min"])
    not_overextended = latest_close <= box_top * (1.0 + config["post_breakout_max_pct"])
    volume_ok = volume_ratio >= config["volume_ratio_min"]
    ready_volume_ok = volume_ratio >= config["ready_volume_ratio_min"]
    recent_high_guard_ok = _passes_recent_high_guard(
        highs=highs,
        latest_idx=latest_idx,
        reference_price=latest_close,
        float_market_cap_yi=float_market_cap_yi,
    )
    if not recent_high_guard_ok:
        return None

    signal_type = None
    signal_label = None
    score = 0.0
    if base_ok and breakout_confirmed and ma_bull and slope_up and volume_ok and not_overextended and ret_20 >= config["ret20_min"]:
        signal_type = "uptrend"
        signal_label = "箱体突破上涨"
        score = (
            50
            + min(breakout_pct * 600, 15)
            + min(ret_20 * 120, 12)
            + min(volume_ratio * 6, 12)
            + min(max(ma_mid_slope_pct, 0) * 800, 11)
        )
    elif base_ok and near_box_top and ma_ready and ma_mid_slope_pct >= config["ready_ma_mid_slope_min"] and ready_volume_ok and box_position_pct >= config["ready_box_position_min"]:
        signal_type = "ready"
        signal_label = "震荡后临近突破"
        score = (
            35
            + min(max(distance_to_box_top_pct + config["near_breakout_gap_pct"], 0) * 400, 10)
            + min(volume_ratio * 5, 10)
            + min(box_position_pct * 15, 15)
            + min(max(ma_mid_slope_pct, 0) * 700, 10)
        )

    if not signal_type:
        return None

    return {
        "code": stock.code,
        "name": stock.name,
        "trade_date": latest_date.isoformat() if latest_date else None,
        "signal_type": signal_type,
        "signal_label": signal_label,
        "industry": stock.industry,
        "market": stock.market,
        "close": round(latest_close, 3),
        "total_market_cap_yi": round(total_market_cap_yi, 2),
        "float_market_cap_yi": round(float_market_cap_yi, 2),
        "box_top": round(box_top, 3),
        "box_bottom": round(box_bottom, 3),
        "box_range_pct": round(box_range_pct * 100, 2),
        "base_drift_pct": round(base_drift_pct * 100, 2),
        "distance_to_box_top_pct": round(distance_to_box_top_pct * 100, 2),
        "breakout_pct": round(breakout_pct * 100, 2),
        "box_position_pct": round(box_position_pct * 100, 2),
        "volume_ratio": round(volume_ratio, 2),
        "ma_short": _round_or_none(ma_short),
        "ma_mid": _round_or_none(ma_mid),
        "ma_long": _round_or_none(ma_long),
        "ma_mid_slope_pct": round(ma_mid_slope_pct * 100, 2),
        "ma_long_slope_pct": round(ma_long_slope_pct * 100, 2),
        "ret_20": round(ret_20 * 100, 2),
        "ret_60": round(ret_60 * 100, 2),
        "score": round(score, 2),
    }


def _pct_change(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current in (None, 0) or base in (None, 0):
        return None
    try:
        current_value = float(current)
        base_value = float(base)
    except Exception:
        return None
    if base_value == 0:
        return None
    return current_value / base_value - 1.0


def _safe_median(values: List[float]) -> Optional[float]:
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return None
    return float(median(cleaned))


def _build_uptrend_candidate_snapshot(
    stock: Stock,
    daily_bars: List[KlineDaily],
    config: Dict[str, Any],
    target_idx: Optional[int] = None,
    precomputed: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not daily_bars:
        return None

    if precomputed:
        closes = precomputed["closes"]
        highs = precomputed["highs"]
        lows = precomputed["lows"]
        volumes = precomputed["volumes"]
        dates = precomputed["dates"]
        ma_short_list = precomputed["ma_short_list"]
        ma_mid_list = precomputed["ma_mid_list"]
        ma_long_list = precomputed["ma_long_list"]
    else:
        closes = [float(bar.close or 0) for bar in daily_bars]
        highs = [float(bar.high or 0) for bar in daily_bars]
        lows = [float(bar.low or 0) for bar in daily_bars]
        volumes = [float(bar.volume or 0) for bar in daily_bars]
        dates = [bar.trade_date for bar in daily_bars]
        ma_short_list = _rolling_mean(closes, config["ma_short"])
        ma_mid_list = _rolling_mean(closes, config["ma_mid"])
        ma_long_list = _rolling_mean(closes, config["ma_long"])

    latest_idx = len(closes) - 1 if target_idx is None else int(target_idx)
    if latest_idx < 0 or latest_idx >= len(closes):
        return None
    if latest_idx < max(config["required_positive_window"], config["ma_long"]) - 1:
        return None

    positive_window_start = latest_idx - config["required_positive_window"] + 1
    if positive_window_start < 0:
        return None
    if any(value <= 0 for value in closes[positive_window_start:latest_idx + 1]):
        return None

    latest_close = closes[latest_idx]
    latest_high = highs[latest_idx]
    latest_volume = volumes[latest_idx]
    latest_date = dates[latest_idx]
    total_share_wan = float(stock.total_share or 0)
    total_market_cap_yi = (total_share_wan * 10000 * latest_close) / 1e8 if total_share_wan > 0 and latest_close > 0 else 0.0
    if total_market_cap_yi < config["min_total_market_cap_yi"]:
        return None
    float_market_cap_yi = _resolve_float_market_cap_yi(stock, latest_close)

    ma_short = ma_short_list[latest_idx]
    ma_mid = ma_mid_list[latest_idx]
    ma_long = ma_long_list[latest_idx]
    if ma_short is None or ma_mid is None or ma_long is None:
        return None

    slope_idx = latest_idx - config["ma_slope_days"]
    ma_mid_prev = ma_mid_list[slope_idx] if slope_idx >= 0 else None
    ma_long_prev = ma_long_list[slope_idx] if slope_idx >= 0 else None
    if ma_mid_prev in (None, 0) or ma_long_prev in (None, 0):
        return None

    box_window = config["box_lookback_days"]
    if latest_idx + 1 < box_window + 1:
        return None

    box_slice_start = latest_idx - box_window
    box_slice_end = latest_idx
    base_highs = highs[box_slice_start:box_slice_end]
    base_lows = lows[box_slice_start:box_slice_end]
    base_closes = closes[box_slice_start:box_slice_end]
    if len(base_closes) < config["min_box_days"]:
        return None

    box_top = max(base_highs)
    box_bottom = min(base_lows)
    if box_bottom <= 0 or box_top <= 0 or box_top <= box_bottom:
        return None

    box_range_pct = (box_top - box_bottom) / box_bottom
    base_drift_pct = abs(base_closes[-1] / base_closes[0] - 1.0) if base_closes[0] > 0 else 999
    distance_to_box_top_pct = latest_close / box_top - 1.0
    breakout_pct = latest_high / box_top - 1.0
    box_position_pct = (latest_close - box_bottom) / (box_top - box_bottom) if box_top > box_bottom else 0.0

    prev_volume_window = volumes[max(0, latest_idx - config["volume_window"]):latest_idx]
    prev_volume_avg = _avg(prev_volume_window)
    volume_ratio = latest_volume / prev_volume_avg if prev_volume_avg > 0 else 0.0

    ma_mid_slope_pct = ma_mid / ma_mid_prev - 1.0
    ma_long_slope_pct = ma_long / ma_long_prev - 1.0
    ret_20 = latest_close / closes[latest_idx - 20] - 1.0 if latest_idx >= 20 and closes[latest_idx - 20] > 0 else 0.0
    ret_60 = latest_close / closes[latest_idx - 60] - 1.0 if latest_idx >= 60 and closes[latest_idx - 60] > 0 else 0.0

    base_ok = (
        box_range_pct <= config["box_range_pct_max"]
        and base_drift_pct <= config["base_drift_pct_max"]
        and box_window >= config["min_box_days"]
    )
    ma_bull = ma_short > ma_mid > ma_long
    ma_ready = ma_short >= ma_mid and ma_mid >= ma_long * config["ready_ma_long_ratio"]
    slope_up = ma_mid_slope_pct >= config["ma_mid_slope_min"] and ma_long_slope_pct >= config["ma_long_slope_min"]
    near_box_top = distance_to_box_top_pct >= -config["near_breakout_gap_pct"]
    breakout_confirmed = latest_close >= box_top * (1.0 + config["breakout_pct_min"])
    not_overextended = latest_close <= box_top * (1.0 + config["post_breakout_max_pct"])
    volume_ok = volume_ratio >= config["volume_ratio_min"]
    ready_volume_ok = volume_ratio >= config["ready_volume_ratio_min"]
    recent_high_guard_ok = _passes_recent_high_guard(
        highs=highs,
        latest_idx=latest_idx,
        reference_price=latest_close,
        float_market_cap_yi=float_market_cap_yi,
    )
    if not recent_high_guard_ok:
        return None

    signal_type = None
    signal_label = None
    score = 0.0
    if base_ok and breakout_confirmed and ma_bull and slope_up and volume_ok and not_overextended and ret_20 >= config["ret20_min"]:
        signal_type = "uptrend"
        signal_label = "箱体突破上涨"
        score = (
            50
            + min(breakout_pct * 600, 15)
            + min(ret_20 * 120, 12)
            + min(volume_ratio * 6, 12)
            + min(max(ma_mid_slope_pct, 0) * 800, 11)
        )
    elif base_ok and near_box_top and ma_ready and ma_mid_slope_pct >= config["ready_ma_mid_slope_min"] and ready_volume_ok and box_position_pct >= config["ready_box_position_min"]:
        signal_type = "ready"
        signal_label = "临近突破观察"
        score = (
            35
            + min(max(distance_to_box_top_pct + config["near_breakout_gap_pct"], 0) * 400, 10)
            + min(volume_ratio * 5, 10)
            + min(box_position_pct * 15, 15)
            + min(max(ma_mid_slope_pct, 0) * 700, 10)
        )

    if not signal_type:
        return None

    return {
        "code": stock.code,
        "name": stock.name,
        "trade_date": latest_date.isoformat() if latest_date else None,
        "signal_type": signal_type,
        "signal_label": signal_label,
        "industry": stock.industry,
        "market": stock.market,
        "close": round(latest_close, 3),
        "total_market_cap_yi": round(total_market_cap_yi, 2),
        "float_market_cap_yi": round(float_market_cap_yi, 2),
        "box_top": round(box_top, 3),
        "box_bottom": round(box_bottom, 3),
        "box_range_pct": round(box_range_pct * 100, 2),
        "base_drift_pct": round(base_drift_pct * 100, 2),
        "distance_to_box_top_pct": round(distance_to_box_top_pct * 100, 2),
        "breakout_pct": round(breakout_pct * 100, 2),
        "box_position_pct": round(box_position_pct * 100, 2),
        "volume_ratio": round(volume_ratio, 2),
        "ma_short": _round_or_none(ma_short),
        "ma_mid": _round_or_none(ma_mid),
        "ma_long": _round_or_none(ma_long),
        "ma_mid_slope_pct": round(ma_mid_slope_pct * 100, 2),
        "ma_long_slope_pct": round(ma_long_slope_pct * 100, 2),
        "ret_20": round(ret_20 * 100, 2),
        "ret_60": round(ret_60 * 100, 2),
        "score": round(score, 2),
    }


def _summarize_uptrend_backtest_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    horizons = [5, 10, 20, 40]
    signal_types = ["uptrend"]
    summary: Dict[str, Any] = {"overall": {}, "by_signal_type": {}}

    def build_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        stats: Dict[str, Any] = {"count": len(rows)}
        if not rows:
            for horizon in horizons:
                stats[f"avg_return_{horizon}d"] = None
                stats[f"win_rate_{horizon}d"] = None
            stats["avg_max_gain_20d"] = None
            stats["avg_max_drawdown_20d"] = None
            stats["success_profile"] = {}
            return stats

        for horizon in horizons:
            key = f"return_{horizon}d"
            values = [float(item[key]) for item in rows if item.get(key) is not None]
            stats[f"avg_return_{horizon}d"] = round(_avg(values) * 100, 2) if values else None
            stats[f"win_rate_{horizon}d"] = round(sum(1 for value in values if value > 0) / len(values) * 100, 2) if values else None

        gains = [float(item["max_gain_20d"]) for item in rows if item.get("max_gain_20d") is not None]
        drawdowns = [float(item["max_drawdown_20d"]) for item in rows if item.get("max_drawdown_20d") is not None]
        stats["avg_max_gain_20d"] = round(_avg(gains) * 100, 2) if gains else None
        stats["avg_max_drawdown_20d"] = round(_avg(drawdowns) * 100, 2) if drawdowns else None

        successful_rows = [item for item in rows if (item.get("return_20d") or -999) >= 0.08]
        stats["success_profile"] = {
            "volume_ratio": round(_safe_median([item.get("volume_ratio") for item in successful_rows]) or 0, 2) if successful_rows else None,
            "breakout_pct": round(_safe_median([item.get("breakout_pct") for item in successful_rows]) or 0, 2) if successful_rows else None,
            "distance_to_box_top_pct": round(_safe_median([item.get("distance_to_box_top_pct") for item in successful_rows]) or 0, 2) if successful_rows else None,
        }
        return stats

    summary["overall"] = build_stats(events)
    for signal_type in signal_types:
        typed_rows = [item for item in events if item.get("signal_type") == signal_type]
        stats = build_stats(typed_rows)
        summary["by_signal_type"][signal_type] = stats

    return summary


def _build_uptrend_backtest_conclusions(summary: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    uptrend_stats = summary.get("by_signal_type", {}).get("uptrend", {})

    if uptrend_stats.get("count", 0) > 0:
        notes.append(
            f"主买点更适合放在突破确认日: 箱体突破信号样本 {uptrend_stats.get('count', 0)} 个，20日平均收益 {uptrend_stats.get('avg_return_20d')}%，胜率 {uptrend_stats.get('win_rate_20d')}%。"
        )
        profile = uptrend_stats.get("success_profile") or {}
        if profile.get("volume_ratio") is not None:
            notes.append(
                f"高质量箱体突破样本常见特征是放量倍数约 {profile.get('volume_ratio')}x、突破幅度约 {profile.get('breakout_pct')}%，说明有效放量突破比单纯贴近上沿更重要。"
            )

    if not notes:
        notes.append("当前参数下样本不足，建议扩大回测年份或适当放宽箱体与量能过滤条件后再校准。")
    return notes


def _run_uptrend_backtest_for_stock(
    stock_payload: Dict[str, Any],
    config: Dict[str, Any],
    start_date,
    end_date,
    preload_date,
    warmup_days: int,
    max_horizon: int,
    cooldown_days: int,
) -> Dict[str, Any]:
    total_start = time.perf_counter()
    session = DatabaseSessionManager.get_session()
    try:
        query_start = time.perf_counter()
        bars = _load_daily_bars_clickhouse(
            stock_payload["code"],
            end_date,
            start_date=preload_date,
            ascending=True,
        )
        if bars is None:
            bars = session.query(KlineDaily).filter(
                KlineDaily.code == stock_payload["code"],
                KlineDaily.trade_date >= preload_date,
                KlineDaily.trade_date <= end_date,
            ).order_by(KlineDaily.trade_date.asc()).all()
        query_seconds = time.perf_counter() - query_start

        if len(bars) < warmup_days + 5:
            return {
                "processed": False,
                "events": [],
                "metrics": {
                    "code": stock_payload.get("code"),
                    "bars_count": len(bars),
                    "query_seconds": round(query_seconds, 4),
                    "prep_seconds": 0.0,
                    "loop_seconds": 0.0,
                    "total_seconds": round(time.perf_counter() - total_start, 4),
                    "events_count": 0,
                },
            }

        stock = SimpleNamespace(
            code=stock_payload.get("code"),
            name=stock_payload.get("name"),
            industry=stock_payload.get("industry"),
            market=stock_payload.get("market"),
            float_share=stock_payload.get("float_share"),
            total_share=stock_payload.get("total_share"),
        )

        prep_start = time.perf_counter()
        stock_events: List[Dict[str, Any]] = []
        last_signal_index: Dict[str, int] = {}
        closes = [float(bar.close or 0) for bar in bars]
        highs = [float(bar.high or 0) for bar in bars]
        lows = [float(bar.low or 0) for bar in bars]
        volumes = [float(bar.volume or 0) for bar in bars]
        dates = [bar.trade_date for bar in bars]
        precomputed = {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": volumes,
            "dates": dates,
            "ma_short_list": _rolling_mean(closes, config["ma_short"]),
            "ma_mid_list": _rolling_mean(closes, config["ma_mid"]),
            "ma_long_list": _rolling_mean(closes, config["ma_long"]),
        }
        prep_seconds = time.perf_counter() - prep_start

        loop_start = time.perf_counter()
        for idx, bar in enumerate(bars):
            trade_date = bar.trade_date
            if trade_date < start_date or trade_date > end_date:
                continue
            if idx < warmup_days:
                continue

            candidate = _build_uptrend_candidate_snapshot(
                stock=stock,
                daily_bars=bars,
                config=config,
                target_idx=idx,
                precomputed=precomputed,
            )
            if not candidate:
                continue

            signal_type = candidate["signal_type"]
            if signal_type != "uptrend":
                continue
            last_idx = last_signal_index.get(signal_type)
            if last_idx is not None and idx - last_idx < cooldown_days:
                continue
            last_signal_index[signal_type] = idx

            event = dict(candidate)
            event["signal_date"] = trade_date.isoformat()
            event["available_horizon_days"] = max(0, len(bars) - idx - 1)
            event["days_since_signal"] = max(0, (end_date - trade_date).days)
            event["is_ongoing_sample"] = event["available_horizon_days"] < max_horizon
            for horizon in [5, 10, 20, 40]:
                future_close = closes[idx + horizon] if idx + horizon < len(closes) else None
                event[f"return_{horizon}d"] = round(_pct_change(future_close, closes[idx]), 4) if future_close else None
                remaining_days = max(0, horizon - event["available_horizon_days"])
                event[f"horizon_{horizon}d_status"] = "ready" if remaining_days == 0 else "pending"
                event[f"horizon_{horizon}d_remaining_days"] = remaining_days

            future_window_high = max(highs[idx + 1:idx + 21]) if idx + 20 < len(highs) else None
            future_window_low = min(lows[idx + 1:idx + 21]) if idx + 20 < len(lows) else None
            event["max_gain_20d"] = round(_pct_change(future_window_high, closes[idx]), 4) if future_window_high else None
            event["max_drawdown_20d"] = round(_pct_change(future_window_low, closes[idx]), 4) if future_window_low else None
            stock_events.append(event)
        loop_seconds = time.perf_counter() - loop_start

        return {
            "processed": True,
            "events": stock_events,
            "metrics": {
                "code": stock_payload.get("code"),
                "bars_count": len(bars),
                "query_seconds": round(query_seconds, 4),
                "prep_seconds": round(prep_seconds, 4),
                "loop_seconds": round(loop_seconds, 4),
                "total_seconds": round(time.perf_counter() - total_start, 4),
                "events_count": len(stock_events),
            },
        }
    finally:
        DatabaseSessionManager.close_session(session)


def _run_uptrend_backtest_analysis(
    session: Session,
    config: Dict[str, Any],
    start_date,
    end_date,
    progress_callback: Optional[Callable[[int, int, int, Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]], None]] = None,
) -> Dict[str, Any]:
    max_horizon = int(config.get("forward_horizon_days", 40))
    cooldown_days = int(config.get("signal_cooldown_days", 15))
    warmup_days = max(config["box_lookback_days"] + 5, config["ma_long"] + config["ma_slope_days"] + 5, 120)
    preload_date = start_date - timedelta(days=warmup_days * 2)

    query = session.query(Stock).filter(
        Stock.type == "stock",
        Stock.list_date.isnot(None),
        Stock.list_date <= start_date - timedelta(days=config["min_list_days"]),
    )
    if not config["include_quit"]:
        # A stock delisted today may still have been a valid constituent at the
        # beginning of a historical backtest.  Use the delisting date instead
        # of applying today's quit flag retroactively to old samples.
        query = query.filter(
            or_(
                Stock.quit == False,
                and_(Stock.delist_date.isnot(None), Stock.delist_date >= start_date),
            )
        )
    if not config["include_st"]:
        query = query.filter(Stock.st == False, ~Stock.name.like("ST%"), ~Stock.name.like("*ST%"))
    stocks = query.all()

    events: List[Dict[str, Any]] = []
    processed_stocks = 0
    perf_metrics: List[Dict[str, Any]] = []
    latest_samples: List[Dict[str, Any]] = []
    stock_payloads = [
        {
            "code": stock.code,
            "name": stock.name,
            "industry": stock.industry,
            "market": stock.market,
            "float_share": stock.float_share,
            "total_share": stock.total_share,
        }
        for stock in stocks
    ]

    requested_workers = int(config.get("backtest_workers", 0) or 0)
    if requested_workers > 0:
        max_workers = min(max(requested_workers, 1), 16)
    else:
        max_workers = min(max((os.cpu_count() or 4), 2), 8)
    max_workers = min(max_workers, len(stock_payloads) or 1)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="uptrend-backtest") as executor:
        future_map = {
            executor.submit(
                _run_uptrend_backtest_for_stock,
                stock_payload,
                config,
                start_date,
                end_date,
                preload_date,
                warmup_days,
                max_horizon,
                cooldown_days,
            ): stock_payload["code"]
            for stock_payload in stock_payloads
        }

        for done_count, future in enumerate(as_completed(future_map), start=1):
            code = future_map[future]
            try:
                stock_result = future.result()
                if stock_result.get("processed"):
                    processed_stocks += 1
                stock_events = stock_result.get("events", []) or []
                events.extend(stock_events)
                latest_samples.extend(stock_events)
                if len(latest_samples) > 120:
                    latest_samples = latest_samples[-120:]
                if stock_result.get("metrics"):
                    perf_metrics.append(stock_result["metrics"])
            except Exception:
                logger.exception(f"uptrend backtest worker failed: {code}")

            if progress_callback:
                progress = min(95, 10 + int(done_count * 85 / max(len(stock_payloads), 1)))
                partial_summary = {
                    "overall_count": len(events),
                    "uptrend_count": sum(1 for item in events if item.get("signal_type") == "uptrend"),
                }
                progress_callback(done_count, len(stock_payloads), progress, list(latest_samples), partial_summary)

    summary = _summarize_uptrend_backtest_events(events)
    total_query_seconds = sum(float(item.get("query_seconds") or 0) for item in perf_metrics)
    total_prep_seconds = sum(float(item.get("prep_seconds") or 0) for item in perf_metrics)
    total_loop_seconds = sum(float(item.get("loop_seconds") or 0) for item in perf_metrics)
    total_worker_seconds = sum(float(item.get("total_seconds") or 0) for item in perf_metrics)
    slowest_stocks = sorted(
        perf_metrics,
        key=lambda item: float(item.get("total_seconds") or 0),
        reverse=True,
    )[:20]
    summary["perf"] = {
        "stocks_profiled": len(perf_metrics),
        "total_query_seconds": round(total_query_seconds, 3),
        "total_prep_seconds": round(total_prep_seconds, 3),
        "total_loop_seconds": round(total_loop_seconds, 3),
        "total_worker_seconds": round(total_worker_seconds, 3),
        "avg_query_seconds_per_stock": round(total_query_seconds / len(perf_metrics), 4) if perf_metrics else 0.0,
        "avg_loop_seconds_per_stock": round(total_loop_seconds / len(perf_metrics), 4) if perf_metrics else 0.0,
        "top_slowest_stocks": slowest_stocks,
    }
    summary["ongoing_samples"] = sum(1 for item in events if item.get("is_ongoing_sample"))
    summary["matured_samples"] = max(0, len(events) - summary["ongoing_samples"])
    summary["period"] = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "processed_stocks": processed_stocks,
        "signal_cooldown_days": cooldown_days,
    }
    summary["conclusions"] = _build_uptrend_backtest_conclusions(summary)
    recent_cases = sorted(
        events,
        key=lambda item: (item.get("signal_date") or "", item.get("score") or 0),
        reverse=True,
    )[:200]

    return {
        "summary": summary,
        "events": events,
        "recent_cases": recent_cases,
    }

def _create_uptrend_config(request: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "box_lookback_days": int(request.get("box_lookback_days", 80)),
        "min_box_days": int(request.get("min_box_days", 40)),
        "box_range_pct_max": float(request.get("box_range_pct_max", 0.35)),
        "base_drift_pct_max": float(request.get("base_drift_pct_max", 0.18)),
        "breakout_pct_min": float(request.get("breakout_pct_min", 0.01)),
        "post_breakout_max_pct": float(request.get("post_breakout_max_pct", 0.15)),
        "near_breakout_gap_pct": float(request.get("near_breakout_gap_pct", 0.03)),
        "volume_window": int(request.get("volume_window", 10)),
        "volume_ratio_min": float(request.get("volume_ratio_min", 1.2)),
        "ready_volume_ratio_min": float(request.get("ready_volume_ratio_min", 0.95)),
        "ma_short": int(request.get("ma_short", 10)),
        "ma_mid": int(request.get("ma_mid", 20)),
        "ma_long": int(request.get("ma_long", 60)),
        "ma_slope_days": int(request.get("ma_slope_days", 5)),
        "ma_mid_slope_min": float(request.get("ma_mid_slope_min", 0.003)),
        "ma_long_slope_min": float(request.get("ma_long_slope_min", 0.0)),
        "ready_ma_mid_slope_min": float(request.get("ready_ma_mid_slope_min", -0.002)),
        "ready_ma_long_ratio": float(request.get("ready_ma_long_ratio", 0.98)),
        "ready_box_position_min": float(request.get("ready_box_position_min", 0.70)),
        "ret20_min": float(request.get("ret20_min", 0.03)),
        "min_total_market_cap_yi": float(request.get("min_total_market_cap_yi", 50)),
        "include_st": bool(request.get("include_st", False)),
        "include_quit": bool(request.get("include_quit", False)),
        "min_list_days": int(request.get("min_list_days", 250)),
        "max_results": int(request.get("max_results", 300)),
        "required_positive_window": int(request.get("required_positive_window", 10)),
        "backtest_workers": int(request.get("backtest_workers", 6)),
    }


def _validate_uptrend_config(config: Dict[str, Any]) -> None:
    if config["min_box_days"] <= 10:
        raise HTTPException(status_code=400, detail="min_box_days must be greater than 10")
    if config["box_lookback_days"] < config["min_box_days"]:
        raise HTTPException(status_code=400, detail="box_lookback_days must be >= min_box_days")
    if not (config["ma_short"] < config["ma_mid"] < config["ma_long"]):
        raise HTTPException(status_code=400, detail="ma_short < ma_mid < ma_long is required")
    if config["max_results"] <= 0:
        raise HTTPException(status_code=400, detail="max_results must be positive")
    if config.get("backtest_workers", 1) <= 0 or config.get("backtest_workers", 1) > 16:
        raise HTTPException(status_code=400, detail="backtest_workers must be between 1 and 16")


def _serialize_selector_run_detail(run: SelectorRun, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        params = json.loads(run.params_json) if run.params_json else {}
    except Exception:
        params = {}
    try:
        summary = json.loads(run.summary_json) if run.summary_json else {}
    except Exception:
        summary = {}
    return {
        "id": int(run.id),
        "run_type": run.run_type,
        "status": run.status,
        "params": params,
        "summary": summary,
        "trade_date": summary.get("trade_date"),
        "config": params,
        "items": items,
        "total_items": int(run.total_items or 0),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


def _serialize_selector_run_list_item(row: SelectorRun) -> Dict[str, Any]:
    try:
        summary = json.loads(row.summary_json) if row.summary_json else {}
    except Exception:
        summary = {}
    try:
        params = json.loads(row.params_json) if row.params_json else {}
    except Exception:
        params = {}
    return {
        "id": int(row.id),
        "run_type": row.run_type,
        "status": row.status,
        "total_items": int(row.total_items or 0),
        "total_signals": int(row.total_signals or 0),
        "summary": summary,
        "params": params,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _load_selector_run_items(session: Session, run_id: int, record_type: str) -> List[Dict[str, Any]]:
    records = session.query(SelectorRunRecord).filter(
        SelectorRunRecord.run_id == run_id,
        SelectorRunRecord.record_type == record_type,
    ).order_by(SelectorRunRecord.id.asc()).all()
    items: List[Dict[str, Any]] = []
    for record in records:
        try:
            payload = json.loads(record.payload_json) if record.payload_json else {}
        except Exception:
            payload = {}
        items.append(payload)
    return items


def _set_uptrend_backtest_task(task_id: str, payload: Dict[str, Any]) -> None:
    with uptrend_backtest_lock:
        task = uptrend_backtest_tasks.setdefault(task_id, {})
        task.update(payload)


def _get_uptrend_backtest_task(task_id: str) -> Optional[Dict[str, Any]]:
    with uptrend_backtest_lock:
        task = uptrend_backtest_tasks.get(task_id)
        return dict(task) if task else None


def _cleanup_uptrend_backtest_tasks(max_keep: int = 50) -> None:
    with uptrend_backtest_lock:
        if len(uptrend_backtest_tasks) <= max_keep:
            return
        ordered_keys = sorted(
            uptrend_backtest_tasks.keys(),
            key=lambda key: uptrend_backtest_tasks[key].get("updated_at") or uptrend_backtest_tasks[key].get("started_at") or "",
            reverse=True,
        )
        for key in ordered_keys[max_keep:]:
            uptrend_backtest_tasks.pop(key, None)


def _run_uptrend_backtest_task(task_id: str, run_id: int, request_config: Dict[str, Any], start_date, end_date) -> None:
    session = DatabaseSessionManager.get_session()
    try:
        _set_uptrend_backtest_task(task_id, {
            "status": "running",
            "progress": 10,
            "error": None,
            "run_id": run_id,
            "updated_at": datetime.now().isoformat(),
        })
        def _on_progress(
            done_count: int,
            total_count: int,
            progress: int,
            partial_items: Optional[List[Dict[str, Any]]] = None,
            partial_summary: Optional[Dict[str, Any]] = None,
        ) -> None:
            payload = {
                "status": "running",
                "progress": progress,
                "processed_stocks": done_count,
                "total_stocks": total_count,
                "run_id": run_id,
                "updated_at": datetime.now().isoformat(),
            }
            if partial_items is not None:
                payload["partial_items"] = partial_items
            if partial_summary is not None:
                payload["partial_summary"] = partial_summary
            _set_uptrend_backtest_task(task_id, payload)

        result = _run_uptrend_backtest_analysis(
            session=session,
            config=request_config,
            start_date=start_date,
            end_date=end_date,
            progress_callback=_on_progress,
        )
        summary_payload = result["summary"]
        run_id = _overwrite_selector_run_by_id_with_retry(
            run_id=run_id,
            selector_name="uptrend_backtest",
            run_type="history",
            params=request_config,
            summary=summary_payload,
            items=result.get("recent_cases", []),
            signal_events=result.get("events", []),
        )
        _set_uptrend_backtest_task(task_id, {
            "status": "completed",
            "progress": 100,
            "result": {
                "run_id": run_id,
                "summary": summary_payload,
                "items": result.get("recent_cases", []),
            },
            "run_id": run_id,
            "updated_at": datetime.now().isoformat(),
        })
    except Exception as exc:
        logger.exception("上涨趋势回测校准失败")
        failed_run_id = None
        try:
            try:
                session.rollback()
            except Exception:
                pass
            failure_summary = {
                "period": {
                    "start_date": start_date.isoformat() if start_date else None,
                    "end_date": end_date.isoformat() if end_date else None,
                    "processed_stocks": 0,
                    "signal_cooldown_days": int(request_config.get("signal_cooldown_days", 0) or 0),
                },
                "overall": {"count": 0},
                "conclusions": [f"回测执行失败: {str(exc)}"],
                "error": str(exc),
            }
            failed_run_id = _overwrite_selector_run_by_id_with_retry(
                run_id=run_id,
                selector_name="uptrend_backtest",
                run_type="history",
                params=request_config,
                summary=failure_summary,
                items=[],
                signal_events=[],
                status="failed",
            )
        except Exception:
            logger.exception("回测失败记录落库失败")
        _set_uptrend_backtest_task(task_id, {
            "status": "failed",
            "progress": 100,
            "error": str(exc),
            "run_id": failed_run_id,
            "updated_at": datetime.now().isoformat(),
        })
    finally:
        DatabaseSessionManager.close_session(session)
        _cleanup_uptrend_backtest_tasks()


def _normalize_new_high_types(raw_types: Any) -> List[str]:
    if raw_types is None:
        return list(NEW_HIGH_SELECTOR_TYPES.keys())
    if isinstance(raw_types, str):
        requested = [raw_types]
    elif isinstance(raw_types, list):
        requested = raw_types
    else:
        requested = []

    normalized: List[str] = []
    for item in requested:
        key = str(item or "").strip().lower()
        if key in NEW_HIGH_SELECTOR_TYPES and key not in normalized:
            normalized.append(key)
    return normalized or list(NEW_HIGH_SELECTOR_TYPES.keys())


def _evaluate_new_high_matches(klines: List[KlineDaily], selected_types: List[str]) -> List[Dict[str, Any]]:
    if len(klines) < 2:
        return []

    latest_kline = klines[-1]
    latest_high = _to_float(latest_kline.high or latest_kline.close)
    latest_close = _to_float(latest_kline.close)
    if latest_high <= 0 or latest_close <= 0:
        return []

    matches: List[Dict[str, Any]] = []
    epsilon = 1e-9

    for selector_type in selected_types:
        config = NEW_HIGH_SELECTOR_TYPES.get(selector_type)
        if not config:
            continue

        lookback_days = config["lookback_days"]
        if lookback_days is None:
            history_klines = klines[:-1]
        else:
            if len(klines) < lookback_days + 1:
                continue
            history_klines = klines[-(lookback_days + 1):-1]

        if not history_klines:
            continue

        previous_high = max(_to_float(item.high or item.close) for item in history_klines)
        if previous_high <= 0:
            continue

        if latest_high <= previous_high + epsilon:
            continue

        breakout_pct = (latest_high - previous_high) / previous_high * 100
        matches.append({
            "type": selector_type,
            "label": config["label"],
            "lookback_days": lookback_days,
            "previous_high": round(previous_high, 3),
            "breakout_high": round(latest_high, 3),
            "close": round(latest_close, 3),
            "breakout_pct": round(breakout_pct, 2),
        })

    return matches


def _attach_sector_analysis(
    session: Session,
    items: List[Dict[str, Any]],
    selected_types: List[str],
) -> Dict[str, Any]:
    if not items:
        return {
            "items": [],
            "sector_summary": [],
            "sector_type_summary": [],
        }

    item_map = {item["code"]: item for item in items if item.get("code")}
    stock_codes = list(item_map.keys())

    rows = session.query(
        SectorStock.stock_code,
        Sector.code,
        Sector.name,
        Sector.type,
        Sector.level,
        Sector.stock_count,
    ).join(
        Sector,
        Sector.code == SectorStock.sector_code,
    ).filter(
        SectorStock.stock_code.in_(stock_codes)
    ).all()

    sector_bucket: Dict[str, Dict[str, Any]] = {}
    sector_type_bucket: Dict[str, Dict[str, Any]] = {}

    for stock_code, sector_code, sector_name, sector_type, sector_level, sector_total_stock_count in rows:
        item = item_map.get(stock_code)
        if not item:
            continue

        sectors = item.setdefault("sectors", [])
        sectors.append({
            "code": sector_code,
            "name": sector_name,
            "type": sector_type,
            "level": int(sector_level or 0),
        })

        sector_entry = sector_bucket.setdefault(
            sector_code,
            {
                "sector_code": sector_code,
                "sector_name": sector_name,
                "sector_type": sector_type,
                "level": int(sector_level or 0),
                "total_stock_count": int(sector_total_stock_count or 0),
                "stock_codes": set(),
                "stock_names": set(),
                "new_high_type_counts": {key: 0 for key in selected_types},
            },
        )
        if stock_code not in sector_entry["stock_codes"]:
            sector_entry["stock_codes"].add(stock_code)
            sector_entry["stock_names"].add(item.get("name") or stock_code)
        for high_type in item.get("new_high_types", []):
            if high_type in sector_entry["new_high_type_counts"]:
                sector_entry["new_high_type_counts"][high_type] += 1

        type_key = sector_type or "unknown"
        type_entry = sector_type_bucket.setdefault(
            type_key,
            {
                "sector_type": type_key,
                "sector_count": set(),
                "stock_codes": set(),
            },
        )
        type_entry["sector_count"].add(sector_code)
        type_entry["stock_codes"].add(stock_code)

    for item in items:
        item["sectors"] = sorted(
            item.get("sectors", []),
            key=lambda sector: (sector.get("type") or "", sector.get("level") or 0, sector.get("code") or ""),
        )
        item["sector_names"] = [sector["name"] for sector in item["sectors"]]

    sector_summary = []
    for sector in sector_bucket.values():
        stock_names = sorted(sector["stock_names"])
        sector_summary.append({
            "sector_code": sector["sector_code"],
            "sector_name": sector["sector_name"],
            "sector_type": sector["sector_type"],
            "level": sector["level"],
            "stock_count": len(sector["stock_codes"]),
            "total_stock_count": int(sector["total_stock_count"] or len(sector["stock_codes"])),
            "fit_ratio": round(
                len(sector["stock_codes"]) / max(int(sector["total_stock_count"] or len(sector["stock_codes"])), 1) * 100,
                2,
            ),
            "stock_codes": sorted(sector["stock_codes"]),
            "stock_names": stock_names,
            "sample_stocks": stock_names[:12],
            "new_high_type_counts": sector["new_high_type_counts"],
        })

    sector_summary.sort(
        key=lambda item: (
            item["stock_count"],
            item["fit_ratio"],
            sum(item["new_high_type_counts"].values()),
            item["sector_name"] or "",
        ),
        reverse=True,
    )

    sector_type_summary = []
    for item in sector_type_bucket.values():
        sector_type_summary.append({
            "sector_type": item["sector_type"],
            "sector_count": len(item["sector_count"]),
            "stock_count": len(item["stock_codes"]),
        })
    sector_type_summary.sort(key=lambda item: (item["stock_count"], item["sector_count"]), reverse=True)

    return {
        "items": items,
        "sector_summary": sector_summary,
        "sector_type_summary": sector_type_summary,
    }


def _save_selector_run(
    session: Session,
    run_type: str,
    params: Dict[str, Any],
    summary: Dict[str, Any],
    items: List[Dict[str, Any]],
    signal_events: Optional[List[Dict[str, Any]]] = None,
    selector_name: str = "main_rise_build_up",
    extra_records: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    status: str = "completed",
) -> int:
    summary_for_storage = _prepare_summary_for_storage(summary)
    run = SelectorRun(
        selector_name=selector_name,
        run_type=run_type,
        status=status,
        params_json=_json_dumps(params),
        summary_json=_json_dumps(summary_for_storage),
        total_items=len(items or []),
        total_signals=len(signal_events or []),
    )
    session.add(run)
    session.flush()

    records: List[SelectorRunRecord] = []
    if run_type == "realtime":
        for item in items or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="realtime_item",
                    code=item.get("code"),
                    stock_name=item.get("name"),
                    payload_json=_json_dumps(item),
                )
            )
    else:
        for item in items or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="history_item",
                    code=item.get("code"),
                    stock_name=item.get("name"),
                    payload_json=_json_dumps(item),
                )
            )
        for event in signal_events or []:
            signal_date = None
            if event.get("signal_date"):
                try:
                    signal_date = datetime.strptime(event["signal_date"], "%Y-%m-%d").date()
                except Exception:
                    signal_date = None
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="signal_event",
                    code=event.get("code"),
                    stock_name=event.get("name"),
                    signal_date=signal_date,
                    payload_json=_json_dumps(event),
                )
            )

    for record_type, payloads in (extra_records or {}).items():
        for payload in payloads or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type=record_type,
                    code=payload.get("code") or payload.get("sector_code"),
                    stock_name=payload.get("name") or payload.get("stock_name") or payload.get("sector_name"),
                    payload_json=_json_dumps(payload),
                )
            )

    if records:
        session.bulk_save_objects(records)
    session.commit()
    return int(run.id)


def _save_or_update_selector_run(
    session: Session,
    run_type: str,
    params: Dict[str, Any],
    summary: Dict[str, Any],
    items: List[Dict[str, Any]],
    signal_events: Optional[List[Dict[str, Any]]] = None,
    selector_name: str = "main_rise_build_up",
    extra_records: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    update_latest_same_params: bool = False,
    status: str = "completed",
) -> int:
    params_json = _json_dumps(params)
    summary_for_storage = _prepare_summary_for_storage(summary)
    if not update_latest_same_params:
        return _save_selector_run(
            session=session,
            run_type=run_type,
            params=params,
            summary=summary,
            items=items,
            signal_events=signal_events,
            selector_name=selector_name,
            extra_records=extra_records,
            status=status,
        )

    run = session.query(SelectorRun).filter(
        SelectorRun.selector_name == selector_name,
        SelectorRun.run_type == run_type,
        SelectorRun.params_json == params_json,
    ).order_by(SelectorRun.updated_at.desc(), SelectorRun.id.desc()).first()

    if run is None:
        return _save_selector_run(
            session=session,
            run_type=run_type,
            params=params,
            summary=summary,
            items=items,
            signal_events=signal_events,
            selector_name=selector_name,
            extra_records=extra_records,
            status=status,
        )

    run.status = status
    run.summary_json = _json_dumps(summary_for_storage)
    run.total_items = len(items or [])
    run.total_signals = len(signal_events or [])
    session.query(SelectorRunRecord).filter(SelectorRunRecord.run_id == run.id).delete()
    session.flush()

    records: List[SelectorRunRecord] = []
    if run_type == "realtime":
        for item in items or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="realtime_item",
                    code=item.get("code"),
                    stock_name=item.get("name"),
                    payload_json=_json_dumps(item),
                )
            )
    else:
        for item in items or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="history_item",
                    code=item.get("code"),
                    stock_name=item.get("name"),
                    payload_json=_json_dumps(item),
                )
            )
        for event in signal_events or []:
            signal_date = None
            if event.get("signal_date"):
                try:
                    signal_date = datetime.strptime(event["signal_date"], "%Y-%m-%d").date()
                except Exception:
                    signal_date = None
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="signal_event",
                    code=event.get("code"),
                    stock_name=event.get("name"),
                    signal_date=signal_date,
                    payload_json=_json_dumps(event),
                )
            )

    for record_type, payloads in (extra_records or {}).items():
        for payload in payloads or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type=record_type,
                    code=payload.get("code") or payload.get("sector_code"),
                    stock_name=payload.get("name") or payload.get("stock_name") or payload.get("sector_name"),
                    payload_json=_json_dumps(payload),
                )
            )

    if records:
        session.bulk_save_objects(records)
    session.commit()
    return int(run.id)


def _overwrite_selector_run_by_id(
    session: Session,
    run_id: int,
    selector_name: str,
    run_type: str,
    params: Dict[str, Any],
    summary: Dict[str, Any],
    items: List[Dict[str, Any]],
    signal_events: Optional[List[Dict[str, Any]]] = None,
    extra_records: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    status: str = "completed",
) -> int:
    summary_for_storage = _prepare_summary_for_storage(summary)
    run = None
    for attempt in range(1, 4):
        try:
            run = session.query(SelectorRun).filter(
                SelectorRun.id == run_id,
                SelectorRun.selector_name == selector_name,
            ).first()
            break
        except Exception as exc:
            if _is_mysql_table_definition_changed_error(exc) and attempt < 4:
                try:
                    session.rollback()
                except Exception:
                    pass
                time.sleep(0.3 * attempt)
                continue
            raise
    if run is None:
        return _save_selector_run(
            session=session,
            run_type=run_type,
            params=params,
            summary=summary,
            items=items,
            signal_events=signal_events,
            selector_name=selector_name,
            extra_records=extra_records,
            status=status,
        )

    run.run_type = run_type
    run.status = status
    run.params_json = _json_dumps(params)
    run.summary_json = _json_dumps(summary_for_storage)
    run.total_items = len(items or [])
    run.total_signals = len(signal_events or [])
    session.query(SelectorRunRecord).filter(SelectorRunRecord.run_id == run.id).delete()
    session.flush()

    records: List[SelectorRunRecord] = []
    if run_type == "realtime":
        for item in items or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="realtime_item",
                    code=item.get("code"),
                    stock_name=item.get("name"),
                    payload_json=_json_dumps(item),
                )
            )
    else:
        for item in items or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="history_item",
                    code=item.get("code"),
                    stock_name=item.get("name"),
                    payload_json=_json_dumps(item),
                )
            )
        for event in signal_events or []:
            signal_date = None
            if event.get("signal_date"):
                try:
                    signal_date = datetime.strptime(event["signal_date"], "%Y-%m-%d").date()
                except Exception:
                    signal_date = None
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type="signal_event",
                    code=event.get("code"),
                    stock_name=event.get("name"),
                    signal_date=signal_date,
                    payload_json=_json_dumps(event),
                )
            )

    for record_type, payloads in (extra_records or {}).items():
        for payload in payloads or []:
            records.append(
                SelectorRunRecord(
                    run_id=run.id,
                    record_type=record_type,
                    code=payload.get("code") or payload.get("sector_code"),
                    stock_name=payload.get("name") or payload.get("stock_name") or payload.get("sector_name"),
                    payload_json=_json_dumps(payload),
                )
            )

    if records:
        session.bulk_save_objects(records)
    session.commit()
    return int(run.id)
@router.post("/select/new-highs")
def select_stocks_by_new_highs(request: dict):
    """历史新高选股，并附带板块归属分析。"""
    selected_types = _normalize_new_high_types(request.get("types"))
    include_st = bool(request.get("include_st", False))
    include_quit = bool(request.get("include_quit", False))
    include_all_time = "all" in selected_types
    max_lookback_days = max(
        (
            int(config["lookback_days"])
            for key, config in NEW_HIGH_SELECTOR_TYPES.items()
            if key in selected_types and config["lookback_days"] is not None
        ),
        default=0,
    )

    session = DatabaseSessionManager.get_session()
    try:
        end_date = _latest_daily_trade_date(session)
        if not end_date:
            return {
                "trade_date": None,
                "selected_types": selected_types,
                "summary": {},
                "total": 0,
                "items": [],
            }

        stocks_query = session.query(Stock).filter(Stock.type == "stock")
        if not include_quit:
            stocks_query = stocks_query.filter(Stock.quit == False)
        if not include_st:
            stocks_query = stocks_query.filter(
                Stock.st == False,
                ~Stock.name.like("ST%"),
                ~Stock.name.like("*ST%"),
            )
        candidate_stocks = stocks_query.all()

        results: List[Dict[str, Any]] = []
        summary = {key: 0 for key in selected_types}

        for stock in candidate_stocks:
            kline_query = session.query(KlineDaily).filter(
                KlineDaily.code == stock.code,
                KlineDaily.trade_date <= end_date,
            )
            if include_all_time:
                klines = kline_query.order_by(KlineDaily.trade_date.asc()).all()
            else:
                klines = list(reversed(
                    kline_query.order_by(KlineDaily.trade_date.desc()).limit(max_lookback_days + 1).all()
                ))

            if len(klines) < 2:
                continue

            latest_kline = klines[-1]
            if latest_kline.trade_date != end_date:
                continue
            if _to_float(latest_kline.volume) <= 0:
                continue

            matches = _evaluate_new_high_matches(klines, selected_types)
            if not matches:
                continue

            for match in matches:
                summary[match["type"]] = summary.get(match["type"], 0) + 1

            max_breakout_pct = max(item["breakout_pct"] for item in matches)
            results.append({
                "code": stock.code,
                "name": stock.name,
                "trade_date": latest_kline.trade_date.isoformat() if latest_kline.trade_date else None,
                "close": round(_to_float(latest_kline.close), 3),
                "high": round(_to_float(latest_kline.high or latest_kline.close), 3),
                "change_pct": round(_to_float(latest_kline.change_pct), 2),
                "volume": int(_to_float(latest_kline.volume)),
                "match_count": len(matches),
                "new_high_types": [item["type"] for item in matches],
                "new_high_labels": [item["label"] for item in matches],
                "max_breakout_pct": round(max_breakout_pct, 2),
                "matches": matches,
            })

        results.sort(
            key=lambda item: (
                item["match_count"],
                item["max_breakout_pct"],
                item["change_pct"],
                item["volume"],
            ),
            reverse=True,
        )
        enriched = _attach_sector_analysis(
            session=session,
            items=results,
            selected_types=selected_types,
        )

        summary_payload = {
            "trade_date": end_date.isoformat(),
            "selected_types": selected_types,
            "type_summary": summary,
            "stock_count": len(results),
            "sector_count": len(enriched["sector_summary"]),
            "sector_type_count": len(enriched["sector_type_summary"]),
        }
        params_payload = {
            "types": selected_types,
            "include_st": include_st,
            "include_quit": include_quit,
        }
        run_id = _save_or_update_selector_run(
            session=session,
            selector_name="new_high_scan",
            run_type="realtime",
            params=params_payload,
            summary=summary_payload,
            items=enriched["items"],
            extra_records={
                "sector_summary_item": enriched["sector_summary"],
                "sector_type_summary_item": enriched["sector_type_summary"],
            },
            update_latest_same_params=True,
        )

        return {
            "trade_date": end_date.isoformat(),
            "selected_types": selected_types,
            "summary": summary,
            "total": len(results),
            "run_id": run_id,
            "items": enriched["items"],
            "sector_summary": enriched["sector_summary"],
            "sector_type_summary": enriched["sector_type_summary"],
        }
    finally:
        DatabaseSessionManager.close_session(session)


class StockDataSyncer:
    """股票数据同步器"""
    
    @staticmethod
    def sync_stock_data(code: str, light_mode: bool = False) -> Dict[str, Any]:
        """同步股票数据
        
        Args:
            code: 股票代码
            light_mode: 是否为轻量模式（只同步缺失数据）
            
        Returns:
            同步结果
        """
        from data_fetcher.manager import DataSourceManager
        
        session = DatabaseSessionManager.get_session()
        try:
            # 检查股票是否存在
            stock = _get_stock_by_code(session, code)
            if not stock:
                raise HTTPException(status_code=404, detail=f"股票 {code} 不存在")
            
            logger.info(f"开始{'轻量' if light_mode else ''}同步股票{code} 的数据")
            
            # 创建TdxQuant客户端（使用连接池）
            data_sources = DataSourceManager()
            logger.info("使用统一数据源管理器同步股票数据")

            class _UnifiedHistoryAdapter:
                def __init__(self, manager):
                    self.manager = manager

                def get_market_data(
                    self,
                    field_list=None,
                    stock_list=None,
                    start_time=None,
                    end_time=None,
                    count=None,
                    dividend_type=None,
                    period="daily",
                    fill_data=None,
                ):
                    target_code = (stock_list or [code])[0]
                    df = self.manager.get_stock_history(
                        target_code,
                        str(start_time or ""),
                        str(end_time or ""),
                        period=period,
                    )
                    if df is None or df.empty:
                        return {}
                    work = df.copy()
                    work["date"] = pd.to_datetime(work.get("date", work.get("datetime")), errors="coerce")
                    work = work.dropna(subset=["date"]).sort_values("date")
                    index = work["date"].dt.strftime("%Y-%m-%d")
                    return {
                        "Open": pd.DataFrame({target_code: pd.to_numeric(work["open"], errors="coerce").values}, index=index),
                        "High": pd.DataFrame({target_code: pd.to_numeric(work["high"], errors="coerce").values}, index=index),
                        "Low": pd.DataFrame({target_code: pd.to_numeric(work["low"], errors="coerce").values}, index=index),
                        "Close": pd.DataFrame({target_code: pd.to_numeric(work["close"], errors="coerce").values}, index=index),
                        "Volume": pd.DataFrame({target_code: pd.to_numeric(work["volume"], errors="coerce").fillna(0).values}, index=index),
                        "Amount": pd.DataFrame({target_code: pd.to_numeric(work["amount"], errors="coerce").fillna(0).values}, index=index),
                    }

                def get_stock_history(self, stock_code, start_date, end_date, period):
                    return self.manager.get_stock_history(stock_code, start_date, end_date, period=period)

            market_source = _UnifiedHistoryAdapter(data_sources)
            
            sync_results = []
            synced_count = 0
            
            # 同步各个周期的K线数据（使用统一的时间单位标准）
            # 标准格式：1m, 5m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y
            periods = [
                {"period": "1d", "label": "日线", "tdx_period": "daily", "model": KlineDaily, "date_field": "trade_date"},
                {"period": "1w", "label": "周线", "tdx_period": "weekly", "model": KlineWeekly, "date_field": "week_start_date"},
                {"period": "1mon", "label": "月线", "tdx_period": "monthly", "model": KlineMonthly, "date_field": "month_start_date"},
                {"period": "1q", "label": "季线", "tdx_period": "quarterly", "model": KlineQuarterly, "date_field": "year"}
            ]
            
            for period_info in periods:
                period = period_info["period"]
                label = period_info["label"]
                tdx_period = period_info["tdx_period"]
                model = period_info["model"]
                date_field = period_info["date_field"]
                
                try:
                    # 轻量模式下检查数据是否存在
                    if light_mode:
                        existing_count = session.query(func.count(model.id)).filter(
                            model.code == code
                        ).scalar()
                        
                        if existing_count > 0:
                            logger.info(f"{label} 已有 {existing_count} 条数据，跳过同步")
                            sync_results.append(f"{label}: 已有 {existing_count} 条，跳过")
                            continue
                    
                    logger.info(f"同步 {label} 数据...")
                    
                    # 使用tdxquant获取K线数据（前复权）
                    # 计算开始日期
                    end_date = datetime.now().strftime('%Y%m%d')
                    if stock.list_date:
                        start_date = stock.list_date.strftime('%Y%m%d')
                    else:
                        start_date = '20000101'  # 默认2000年开始
                    
                    # 构造完整的股票代码（带市场后缀）
                    full_code = code
                    if stock.market:
                        if stock.market == 'sh':
                            full_code = f"{code}.SH"
                        elif stock.market == 'sz':
                            full_code = f"{code}.SZ"
                        elif stock.market == 'bj':
                            full_code = f"{code}.BJ"
                    
                    # 获取K线数据（前复权）
                    data = market_source.get_market_data(
                        field_list=[],
                        stock_list=[full_code],
                        start_time=start_date,
                        end_time=end_date,
                        count=10000,  # 足够大的数量
                        dividend_type='front',  # 前复权
                        period=tdx_period,
                        fill_data=True
                    )
                    
                    # 转换为字典列表
                    klines = []
                    if data and 'Close' in data:
                        close_df = data['Close']
                        open_df = data['Open']
                        high_df = data['High']
                        low_df = data['Low']
                        volume_df = data['Volume']
                        amount_df = data['Amount']
                        
                        if full_code in close_df.columns:
                            for i, (date, close_val) in enumerate(close_df[full_code].items()):
                                kline = {
                                    "open": float(open_df[full_code].iloc[i]) if i < len(open_df[full_code]) else 0,
                                    "high": float(high_df[full_code].iloc[i]) if i < len(high_df[full_code]) else 0,
                                    "low": float(low_df[full_code].iloc[i]) if i < len(low_df[full_code]) else 0,
                                    "close": float(close_val),
                                    "volume": int(volume_df[full_code].iloc[i]) if i < len(volume_df[full_code]) else 0,
                                    "amount": float(amount_df[full_code].iloc[i]) if i < len(amount_df[full_code]) else 0
                                }
                                
                                # 计算涨跌幅
                                if i > 0:
                                    prev_close = float(close_df[full_code].iloc[i-1])
                                    if prev_close > 0:
                                        kline["change_pct"] = ((kline["close"] - prev_close) / prev_close) * 100
                                    else:
                                        kline["change_pct"] = 0
                                else:
                                    kline["change_pct"] = 0
                                
                                # 根据周期添加日期字段
                                date_str = str(date)
                                if period == "1d":
                                    kline["trade_date"] = date_str
                                elif period == "1w":
                                    kline["week_start_date"] = date_str
                                    kline["week_end_date"] = date_str
                                elif period == "1mon":
                                    kline["month_start_date"] = date_str
                                    kline["month_end_date"] = date_str
                                elif period == "1q":
                                    kline["year"] = int(date_str[:4])
                                    kline["quarter"] = (int(date_str[5:7]) - 1) // 3 + 1
                                elif period == "1y":
                                    kline["year"] = int(date_str[:4])
                                
                                klines.append(kline)
                    
                    if klines and len(klines) > 0:
                        # 保存数据
                        for kline in klines:
                            if light_mode:
                                # 轻量模式直接插入
                                kline_obj = model(code=code, **kline)
                                session.add(kline_obj)
                            else:
                                # 非轻量模式检查并更新
                                if period == "1d":
                                    existing = session.query(model).filter(
                                        model.code == code,
                                        model.trade_date == kline["trade_date"]
                                    ).first()
                                elif period == "1w":
                                    existing = session.query(model).filter(
                                        model.code == code,
                                        model.week_start_date == kline["week_start_date"]
                                    ).first()
                                elif period == "1mon":
                                    existing = session.query(model).filter(
                                        model.code == code,
                                        model.month_start_date == kline["month_start_date"]
                                    ).first()
                                elif period == "1q":
                                    existing = session.query(model).filter(
                                        model.code == code,
                                        model.year == kline["year"],
                                        model.quarter == kline["quarter"]
                                    ).first()
                                elif period == "1y":
                                    existing = session.query(model).filter(
                                        model.code == code,
                                        model.year == kline["year"]
                                    ).first()
                                else:
                                    existing = None
                                
                                if existing:
                                    # 更新现有数据
                                    for key, value in kline.items():
                                        if hasattr(existing, key):
                                            setattr(existing, key, value)
                                else:
                                    # 插入新数据
                                    kline_obj = model(code=code, **kline)
                                    session.add(kline_obj)
                        
                        session.commit()
                        synced_count += len(klines)
                        sync_results.append(f"{label}: {'新增' if light_mode else ''} {len(klines)} 条")
                        logger.info(f"{label} 同步完成: {len(klines)} 条")
                    else:
                        sync_results.append(f"{label}: 无数据")
                        logger.warning(f"{label} 无数据")
                    
                except Exception as e:
                    logger.error(f"同步 {label} 失败: {e}")
                    sync_results.append(f"{label}: 同步失败 - {str(e)}")
                    session.rollback()
            
            # 同步分钟线数据
            try:
                logger.info("同步分钟线数据...")
                minute_periods = [5, 15, 30, 60]
                minute_synced = 0
                minute_skipped = 0
                
                for period_minutes in minute_periods:
                    try:
                        # 根据周期获取正确的模型
                        minute_model = MINUTE_MODEL_MAP.get(period_minutes)
                        if not minute_model:
                            logger.warning(f"不支持的分钟周期: {period_minutes}")
                            continue
                        
                        # 轻量模式下检查数据是否存在
                        if light_mode:
                            existing_count = session.query(func.count(minute_model.id)).filter(
                                minute_model.code == code
                            ).scalar()
                            
                            if existing_count > 0:
                                logger.info(f"{period_minutes}分钟线已有 {existing_count} 条数据，跳过")
                                minute_skipped += 1
                                continue
                        
                        # 计算开始日期（最近7天）
                        end_date = datetime.now().strftime('%Y%m%d')
                        start_date = (datetime.now() - timedelta(days=2)).strftime('%Y%m%d')
                        
                        # 转换周期格式（使用统一的时间单位标准）
                        if period_minutes == 1:
                            pytdx_period = "1m"
                        elif period_minutes == 5:
                            pytdx_period = "5m"
                        elif period_minutes == 15:
                            pytdx_period = "15m"
                        elif period_minutes == 30:
                            pytdx_period = "30m"
                        elif period_minutes == 60:
                            pytdx_period = "60m"
                        else:
                            continue
                        
                        # 获取分钟线数据
                        minute_klines_df = market_source.get_stock_history(code, start_date, end_date, pytdx_period)
                        
                        # 转换为字典列表
                        minute_klines = []
                        if minute_klines_df is not None and not minute_klines_df.empty:
                            for index, row in minute_klines_df.iterrows():
                                kline = {
                                    "datetime": row.get('datetime'),
                                    "open": float(row.get('open', 0)),
                                    "high": float(row.get('high', 0)),
                                    "low": float(row.get('low', 0)),
                                    "close": float(row.get('close', 0)),
                                    "volume": int(row.get('volume', 0)),
                                    "amount": float(row.get('amount', 0))
                                }
                                minute_klines.append(kline)
                        
                        if minute_klines and len(minute_klines) > 0:
                            for kline in minute_klines:
                                if light_mode:
                                    # 轻量模式直接插入
                                    kline_obj = minute_model(code=code, **kline)
                                    session.add(kline_obj)
                                else:
                                    # 非轻量模式检查并更新
                                    existing = session.query(minute_model).filter(
                                        minute_model.code == code,
                                        minute_model.datetime == kline["datetime"]
                                    ).first()
                                    
                                    if existing:
                                        for key, value in kline.items():
                                            if key != "datetime" and hasattr(existing, key):
                                                setattr(existing, key, value)
                                    else:
                                        kline_obj = minute_model(code=code, **kline)
                                        session.add(kline_obj)
                            
                            session.commit()
                            minute_synced += len(minute_klines)
                            logger.info(f"{period_minutes}分钟线: {len(minute_klines)} 条")
                        
                    except Exception as e:
                        logger.error(f"同步 {period_minutes}分钟线失败: {e}")
                        session.rollback()
                
                if minute_skipped > 0:
                    sync_results.append(f"分钟线: 跳过 {minute_skipped} 个周期，新增 {minute_synced} 条")
                else:
                    sync_results.append(f"分钟线: {'新增' if light_mode else ''} {minute_synced} 条")
                
                synced_count += minute_synced
                
            except Exception as e:
                logger.error(f"同步分钟线失败 {e}")
                sync_results.append(f"分钟线 同步失败")
            
            return {
                "success": True,
                "code": code,
                "name": stock.name,
                "message": f"{'轻量同步完成，新增' if light_mode else '共同步'}{synced_count} 条数据",
                "details": sync_results,
                "synced_count": synced_count,
                "is_light": light_mode
            }
            
        except Exception as e:
            session.rollback()
            logger.error(f"{'轻量' if light_mode else ''}同步股票 {code} 数据失败: {e}")
            raise HTTPException(status_code=500, detail=f"同步失败: {str(e)}")
        finally:
            DatabaseSessionManager.close_session(session)


@router.get("/")
def get_stocks(
    skip: int = 0,
    limit: int = 50,
    market: Optional[str] = None,
    stock_type: Optional[str] = None,
    search: Optional[str] = None
):
    """
    获取股票列表

    Args:
        skip: 跳过数量
        limit: 返回数量
        market: 市场筛选(sh/sz/bj)
        stock_type: 类型筛选(stock/index/industry/sector)
        search: 搜索关键词（代码或名称）

    Returns:
        股票列表
    """
    session = DatabaseSessionManager.get_session()
    try:
        query = session.query(Stock)

        # 排除退市股票
        query = query.filter(Stock.quit == False)

        # 市场筛选
        if market:
            query = query.filter(Stock.market == market)

        # 类型筛选
        if stock_type:
            query = query.filter(Stock.type == stock_type)

        # 搜索
        if search:
            query = query.filter(
                or_(
                    Stock.code.like(f"%{search}%"),
                    Stock.name.like(f"%{search}%")
                )
            )

        # 按股票代码排序
        query = query.order_by(Stock.code)

        # 分页
        stocks = query.offset(skip).limit(limit).all()

        return [
            {
                "code": stock.code,
                "name": stock.name,
                "market": stock.market,
                "type": stock.type,
                "industry": stock.industry,
                "region": stock.region,
                "industry_code": stock.industry_code,
                "st": stock.st,
                "quit": stock.quit,
                "list_date": stock.list_date.isoformat() if stock.list_date else None,
                "float_share": float(stock.float_share) if stock.float_share else 0,
                "total_share": float(stock.total_share) if stock.total_share else 0,
                "self_selected": stock.self_selected,
                "holding": stock.holding
            }
            for stock in stocks
        ]

    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/with-limit")
def get_stocks_with_limit(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    market: Optional[str] = Query(None, description="市场"),
    stock_type: Optional[str] = Query(None, description="类型"),
    search: Optional[str] = Query(None, description="搜索关键词")
):
    """获取带分页限制的股票列表"""
    session = DatabaseSessionManager.get_session()
    try:
        if not isinstance(market, str):
            market = None
        if not isinstance(stock_type, str):
            stock_type = None
        if not isinstance(search, str):
            search = None

        def _resolve_table_name(base: str) -> str:
            if not clickhouse_available():
                return base
            for t in (base, f"{base}_live"):
                try:
                    if clickhouse_table_exists(t):
                        return t
                except Exception:
                    continue
            return base

        if clickhouse_available() and clickhouse_available():
            stocks_table = _resolve_table_name("stocks")
            if not clickhouse_table_exists(stocks_table):
                return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}

            where_clauses = ["(quit = 0 OR quit IS NULL)"]
            params: List[Any] = []
            if market:
                logger.info(f"市场: {market}")
                where_clauses.append("market = ?")
                params.append(market)
            if stock_type:
                logger.info(f"股票类型: {stock_type}")
                where_clauses.append("type = ?")
                params.append(stock_type)
            if search:
                logger.info(f"搜索关键词: {search}")
                where_clauses.append("(code LIKE ? OR name LIKE ?)")
                params.extend([f"%{search}%", f"%{search}%"])

            where_sql = " AND ".join(where_clauses)
            total = int(clickhouse_scalar(f"SELECT count() FROM {stocks_table} WHERE {where_sql}", params) or 0)

            skip = (page - 1) * page_size
            stock_df = clickhouse_query_df(
                f"""
                SELECT code, name, market, type, industry, region, list_date, st, quit
                FROM {stocks_table}
                WHERE {where_sql}
                ORDER BY code
                LIMIT ? OFFSET ?
                """,
                params + [int(page_size), int(skip)],
            )

            stocks = []
            if stock_df is not None and not stock_df.empty:
                stocks = [
                    SimpleNamespace(
                        code=str(r.code),
                        name=str(r.name or r.code),
                        market=r.market,
                        type=r.type,
                        industry=r.industry,
                        region=r.region,
                        list_date=_as_date(r.list_date) if r.list_date else None,
                        st=bool(r.st) if r.st is not None else False,
                        quit=bool(r.quit) if r.quit is not None else False,
                        industry_code=None,
                        float_share=0,
                        total_share=0,
                        self_selected=False,
                        holding=False,
                    )
                    for r in stock_df.itertuples(index=False)
                ]

            latest_kline_map: Dict[str, Any] = {}
            if stocks:
                kline_table = _resolve_table_name("kline_daily")
                if clickhouse_table_exists(kline_table):
                    try:
                        codes = [stock.code for stock in stocks]
                        placeholders = ",".join(["?"] * len(codes))
                        latest_df = clickhouse_query_df(
                            f"""
                            WITH latest AS (
                                SELECT code, MAX(trade_date) AS max_date
                                FROM {kline_table}
                                WHERE code IN ({placeholders})
                                GROUP BY code
                            )
                            SELECT k.code, k.trade_date, k.close, k.change_pct, k.amount
                            FROM {kline_table} k
                            JOIN latest l ON k.code = l.code AND k.trade_date = l.max_date
                            """,
                            codes,
                        )
                        if latest_df is not None and not latest_df.empty:
                            latest_kline_map = {
                                row.code: SimpleNamespace(
                                    trade_date=_as_date(row.trade_date),
                                    close=row.close,
                                    change_pct=row.change_pct,
                                    amount=row.amount,
                                )
                                for row in latest_df.itertuples(index=False)
                            }
                    except Exception as exc:
                        logger.warning(f"ClickHouse stock list latest kline query failed: {exc}")
                latest_kline_map = _overlay_fresh_intraday_list_kline(stocks, latest_kline_map)

            items = []
            for stock in stocks:
                latest_kline = latest_kline_map.get(stock.code)
                row = {
                    "code": stock.code,
                    "name": stock.name,
                    "market": stock.market,
                    "type": stock.type,
                    "industry": stock.industry,
                    "region": stock.region,
                    "industry_code": stock.industry_code,
                    "st": stock.st,
                    "quit": stock.quit,
                    "list_date": stock.list_date.isoformat() if stock.list_date else None,
                    "float_share": float(stock.float_share) if stock.float_share else 0,
                    "total_share": float(stock.total_share) if stock.total_share else 0,
                    "self_selected": stock.self_selected,
                    "holding": stock.holding,
                }
                if latest_kline:
                    row["latest_kline"] = {
                        "trade_date": latest_kline.trade_date.isoformat() if latest_kline.trade_date else None,
                        "close": float(latest_kline.close) if latest_kline.close is not None else None,
                        "change_pct": float(latest_kline.change_pct) if latest_kline.change_pct is not None else None,
                        "amount": float(latest_kline.amount) if latest_kline.amount is not None else None,
                        "is_provisional": bool(getattr(latest_kline, "is_provisional", False)),
                        "as_of": (
                            getattr(latest_kline, "as_of", None).isoformat(sep=" ", timespec="seconds")
                            if getattr(latest_kline, "as_of", None)
                            else None
                        ),
                        "source": getattr(latest_kline, "source", "kline_daily"),
                    }
                else:
                    row["latest_kline"] = None
                items.append(row)

            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
            }

        query = session.query(Stock).filter(Stock.quit == False)
        if market:
            query = query.filter(Stock.market == market)
        if stock_type:
            query = query.filter(Stock.type == stock_type)
        if search:
            query = query.filter(or_(Stock.code.like(f"%{search}%"), Stock.name.like(f"%{search}%")))

        total = query.count()
        skip = (page - 1) * page_size
        stocks = query.offset(skip).limit(page_size).all()

        latest_kline_map: Dict[str, Any] = {}
        if stocks and clickhouse_available():
            try:
                codes = [stock.code for stock in stocks]
                placeholders = ",".join(["?"] * len(codes))
                latest_df = clickhouse_query_df(
                    f"""
                    WITH latest AS (
                        SELECT code, MAX(trade_date) AS max_date
                        FROM kline_daily
                        WHERE code IN ({placeholders})
                        GROUP BY code
                    )
                    SELECT k.code, k.trade_date, k.close, k.change_pct, k.amount
                    FROM kline_daily k
                    JOIN latest l ON k.code = l.code AND k.trade_date = l.max_date
                    """,
                    codes,
                )
                if latest_df is not None and not latest_df.empty:
                    latest_kline_map = {
                        row.code: SimpleNamespace(
                            trade_date=_as_date(row.trade_date),
                            close=row.close,
                            change_pct=row.change_pct,
                            amount=row.amount,
                        )
                        for row in latest_df.itertuples(index=False)
                    }
            except Exception as exc:
                logger.warning(f"ClickHouse stock list latest kline fallback to SQLAlchemy engine: {exc}")

        latest_kline_map = _overlay_fresh_intraday_list_kline(stocks, latest_kline_map)

        items = []
        latest_trade_date = _latest_daily_trade_date(session)
        for stock in stocks:
            latest_kline = latest_kline_map.get(stock.code)
            if latest_kline is None:
                fallback_query = session.query(KlineDaily).filter(KlineDaily.code == stock.code)
                if latest_trade_date:
                    fallback_query = fallback_query.filter(KlineDaily.trade_date <= latest_trade_date)
                latest_kline = fallback_query.order_by(KlineDaily.trade_date.desc()).first()
            row = {
                "code": stock.code,
                "name": stock.name,
                "market": stock.market,
                "type": stock.type,
                "industry": stock.industry,
                "region": stock.region,
                "industry_code": stock.industry_code,
                "st": stock.st,
                "quit": stock.quit,
                "list_date": stock.list_date.isoformat() if stock.list_date else None,
                "float_share": float(stock.float_share) if stock.float_share else 0,
                "total_share": float(stock.total_share) if stock.total_share else 0,
                "self_selected": stock.self_selected,
                "holding": stock.holding,
            }
            if latest_kline:
                row["latest_kline"] = {
                    "trade_date": latest_kline.trade_date.isoformat() if latest_kline.trade_date else None,
                    "close": float(latest_kline.close) if latest_kline.close is not None else None,
                    "change_pct": float(latest_kline.change_pct) if latest_kline.change_pct is not None else None,
                    "amount": float(latest_kline.amount) if latest_kline.amount is not None else None,
                    "is_provisional": bool(getattr(latest_kline, "is_provisional", False)),
                    "as_of": (
                        getattr(latest_kline, "as_of", None).isoformat(sep=" ", timespec="seconds")
                        if getattr(latest_kline, "as_of", None)
                        else None
                    ),
                    "source": getattr(latest_kline, "source", "kline_daily"),
                }
            else:
                row["latest_kline"] = None
            items.append(row)

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
        }
    except Exception as e:
        logger.error(f"股票列表获取失败: {e}")
        raise e
    finally:
        DatabaseSessionManager.close_session(session)





# ==================== 股票详情接口 ====================


@router.get("/{code}")
def get_stock_detail(code: str):
    """
    获取股票详情

    Args:
        code: 股票代码

    Returns:
        股票详细信息
    """
    session = DatabaseSessionManager.get_session()
    try:
        stock = _get_stock_by_code(session, code)

        if not stock:
            raise HTTPException(status_code=404, detail=f"股票 {code} 不存在")

        return {
            "code": stock.code,
            "name": stock.name,
            "market": stock.market,
            "type": stock.type,
            "industry": stock.industry,
            "region": stock.region,
            "industry_code": stock.industry_code,
            "st": stock.st,
            "quit": stock.quit,
            "list_date": stock.list_date.isoformat() if stock.list_date else None,
            "delist_date": stock.delist_date.isoformat() if stock.delist_date else None,
            "float_share": float(stock.float_share) if stock.float_share else 0,
            "total_share": float(stock.total_share) if stock.total_share else 0,
            "self_selected": stock.self_selected,
            "holding": stock.holding,
            "created_at": stock.created_at.isoformat() if stock.created_at else None,
            "updated_at": stock.updated_at.isoformat() if stock.updated_at else None,
            "status": "delisted" if stock.quit else "active",
        }

    finally:
        DatabaseSessionManager.close_session(session)


@router.post("/{code}/sync")
def sync_stock_data(code: str):
    """
    同步股票数据（从数据源获取并保存到数据库）

    Args:
        code: 股票代码

    Returns:
        同步结果
    """
    return StockDataSyncer.sync_stock_data(code, light_mode=False)


@router.post("/{code}/sync-light")
def sync_stock_data_light(code: str):
    """
    轻量同步股票数据（仅同步缺失的K线数据）

    如果数据库已有该周期的数据，则跳过查询数据源和更新本地数据库

    Args:
        code: 股票代码

    Returns:
        同步结果
    """
    return StockDataSyncer.sync_stock_data(code, light_mode=True)


@router.get("/{code}/kline-stats")
def get_stock_kline_stats(code: str):
    """
    获取股票的K线数据统计（所有周期）

    Args:
        code: 股票代码

    Returns:
        各周期K线数据统计
    """
    session = DatabaseSessionManager.get_session()
    try:
        # 检查股票是否存在
        stock = _get_stock_by_code(session, code)
        if not stock:
            raise HTTPException(status_code=404, detail=f"股票 {code} 不存在")

        stats = {
            "code": code,
            "name": stock.name,
            "periods": {}
        }

        # 使用UNION ALL合并所有查询，减少数据库往返次数
        from sqlalchemy import union_all, literal_column

        # 构建各周期查询
        daily_subquery = session.query(
            literal_column("'1d'").label('period'),
            literal_column("'kline_daily'").label('table'),
            func.count(KlineDaily.id).label('count')
        ).filter(KlineDaily.code == code)

        weekly_subquery = session.query(
            literal_column("'1w'").label('period'),
            literal_column("'kline_weekly'").label('table'),
            func.count(KlineWeekly.id).label('count')
        ).filter(KlineWeekly.code == code)

        monthly_subquery = session.query(
            literal_column("'1mon'").label('period'),
            literal_column("'kline_monthly'").label('table'),
            func.count(KlineMonthly.id).label('count')
        ).filter(KlineMonthly.code == code)

        quarterly_subquery = session.query(
            literal_column("'1q'").label('period'),
            literal_column("'kline_quarterly'").label('table'),
            func.count(KlineQuarterly.id).label('count')
        ).filter(KlineQuarterly.code == code)

        # 合并查询（分钟线单独查询避免慢查询）
        combined_query = union_all(daily_subquery, weekly_subquery, monthly_subquery, quarterly_subquery)

        # 执行合并查询
        results = session.execute(combined_query).fetchall()

        # 填充结果
        for row in results:
            stats["periods"][row.period] = {"count": int(row.count), "table": row.table}

        if clickhouse_available():
            try:
                full_code, short_code = _clickhouse_code_filter_params(code)
                daily_count = clickhouse_scalar(
                    """
                    SELECT COUNT(*)
                    FROM kline_daily
                    WHERE code IN (?, ?)
                    """,
                    [full_code, short_code],
                ) or 0
                stats["periods"]["1d"] = {"count": int(daily_count), "table": "kline_daily"}
            except Exception as exc:
                logger.warning(f"ClickHouse daily kline stats fallback to SQLAlchemy engine: {exc}")

        # 分钟线单独查询（可能数据量较大，避免拖慢整体查询）
        minute_periods_map = {
            1: (KlineMinute1, "kline_minute_1"),
            5: (KlineMinute5, "kline_minute_5"),
            15: (KlineMinute15, "kline_minute_15"),
            30: (KlineMinute30, "kline_minute_30"),
            60: (KlineMinute60, "kline_minute_60")
        }
        for period, (model, table_name) in minute_periods_map.items():
            count = None
            if period in (15, 30, 60) and clickhouse_available():
                try:
                    full_code, short_code = _clickhouse_code_filter_params(code)
                    count = clickhouse_scalar(
                        f"""
                        SELECT COUNT(*)
                        FROM {table_name}
                        WHERE code IN (?, ?)
                        """,
                        [full_code, short_code],
                    )
                except Exception as exc:
                    logger.warning(f"ClickHouse minute kline stats fallback to SQLAlchemy engine: {exc}")
                    count = None
            if count is None:
                count = session.query(func.count(model.id)).filter(
                    model.code == code
                ).scalar() or 0
            period_label = f"{period}m"
            stats["periods"][period_label] = {
                "count": int(count),
                "table": table_name
            }

        # 总计
        total_count = sum(p["count"] for p in stats["periods"].values())
        stats["total_count"] = total_count
        stats["periods_with_data"] = sum(1 for p in stats["periods"].values() if p["count"] > 0)

        logger.info(f"股票 {code} K线统计 {stats['periods_with_data']}/{len(stats['periods'])} 个周期有数据")

        return stats

    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/{code}/navigation")
def get_stock_navigation(code: str):
    """Return adjacent active stocks without loading the full stock universe."""
    session = DatabaseSessionManager.get_session()
    try:
        base_query = session.query(Stock).filter(
            Stock.type == "stock",
            Stock.quit == False,
        )
        previous = (
            base_query
            .filter(Stock.code < code)
            .order_by(Stock.code.desc())
            .first()
        )
        following = (
            base_query
            .filter(Stock.code > code)
            .order_by(Stock.code.asc())
            .first()
        )

        def _serialize(stock):
            return {"code": stock.code, "name": stock.name} if stock else None

        return {"code": code, "previous": _serialize(previous), "next": _serialize(following)}
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/{code}/industry")
def get_stock_industry(code: str):
    """
    获取股票行业信息

    Args:
        code: 股票代码

    Returns:
        行业信息
    """
    from data_fetcher.pytdx import PytdxDataSource
    from utils.config import ConfigManager

    try:
        # 创建Pytdx数据源
        config_manager = ConfigManager()
        data_sources_config = config_manager.get_data_sources_config()
        pytdx_config = data_sources_config.get("pytdx", {})
        data_source = PytdxDataSource(pytdx_config)

        # 获取行业信息
        industry_info = data_source.get_industry_info(code)
        
        if not industry_info:
            return {
                "code": code,
                "industry": None,
                "industry_code": None,
                "message": "未获取到行业信息"
            }
        
        return {
            "code": code,
            "industry": industry_info.get("industry"),
            "industry_code": industry_info.get("industry_code"),
            "message": "获取行业信息成功"
        }
        
    except Exception as e:
        logger.error(f"获取股票 {code} 行业信息失败: {e}")
        return {
            "code": code,
            "industry": None,
            "industry_code": None,
            "message": f"获取行业信息失败: {str(e)}"
        }


@router.get("/{code}/boards")
def get_stock_boards(code: str):
    """
    获取股票所属板块列表
    Args:
        code: 股票代码
    Returns:
        板块列表 { boards: [{ code, name, type }, ...] }
    """
    logger = get_logger("stocks.boards")
    try:
        # 清理股票代码格式: 000001.SZ -> 000001, 600000.SH -> 600000
        bare_code = code.split(".")[0] if "." in code else code
        # 尝试补齐 .SZ/.SH 后缀
        if "." not in code:
            if bare_code.startswith(("6", "5", "9")):
                full_code = f"{bare_code}.SH"
            else:
                full_code = f"{bare_code}.SZ"
        else:
            full_code = code.upper()

        boards = []
        db_session = db.get_session()
        session = next(db_session)
        try:
            sector_stocks = (
                session.query(SectorStock)
                .filter(SectorStock.stock_code == bare_code)
                .all()
            )
            if not sector_stocks:
                # 尝试带后缀查询
                sector_stocks = (
                    session.query(SectorStock)
                    .filter(SectorStock.stock_code == full_code)
                    .all()
                )

            sector_codes = [ss.sector_code for ss in sector_stocks]
            if sector_codes:
                sectors = (
                    session.query(Sector)
                    .filter(Sector.code.in_(sector_codes))
                    .all()
                )
                sector_map = {s.code: s for s in sectors}
                for ss in sector_stocks:
                    sector = sector_map.get(ss.sector_code)
                    if sector:
                        boards.append({
                            "code": sector.code,
                            "name": sector.name,
                            "type": sector.type,
                        })
        finally:
            session.close()

        return {"boards": boards}

    except Exception as e:
        logger.error(f"获取股票 {code} 所属板块失败: {e}")
        return {"boards": []}


def _ensure_listing_status_column(client):
    """Add the lifecycle column before a reference-table swap on older databases."""
    client.command(
        "ALTER TABLE stocks ADD COLUMN IF NOT EXISTS "
        "listing_status LowCardinality(String) DEFAULT 'active' AFTER list_date"
    )


def _reference_rows_by_code(client):
    """Read every stored field so reference refresh never resets user-owned values."""
    result = client.query("SELECT * FROM stocks")
    records = [dict(zip(result.column_names,row)) for row in result.result_rows]
    return {str(row['code']):row for row in records if row.get('code')}


def _insert_reference_rows(client, table, rows, columns, existing):
    groups = {}
    for row in rows:
        updates = dict(zip(columns,row))
        merged = {**existing.get(str(updates['code']),{}), **updates}
        # Missing columns on new rows use the database defaults. Existing rows
        # carry their complete stored values, including flags and timestamps.
        keys = tuple(merged)
        groups.setdefault(keys,[]).append([merged[key] for key in keys])
    for keys, values in groups.items():
        client.insert(table,values,column_names=list(keys))


@router.post("/update")
def update_stock_list():
    """更新股票列表到 ClickHouse"""
    logger = get_logger("stocks.update")
    logger.info("Stock list update started")
    import time
    t0 = time.perf_counter()
    t_fetch_done = t0
    t_transform_done = t0
    t_load_done = t0
    t_swap_done = t0

    try:
        from data_fetcher.manager import DataSourceManager
        import os
        from clickhouse_connect import get_client
        from datetime import date as dt_date

        data_sources = DataSourceManager()
        try:
            from api.system_config import task_manager
            task_manager.update_progress("update_stock_list", {"stage": "fetch", "message": "股票列表更新: 拉取中"})
        except Exception:
            pass
        stock_list = data_sources.get_stock_list(market="ALL", stock_type="stock", source_name="qmt_xtquant")
        t_fetch_done = time.perf_counter()
        if not stock_list:
            logger.error("Stock list is empty")
            return {"success": False, "message": "股票列表为空"}

        host = os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1")
        port = int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123"))
        database = os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock")
        username = os.getenv("AISTOCK_CLICKHOUSE_USER", "default")
        password = os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", "")
        ch = get_client(host=host, port=port, database=database, username=username, password=password)
    except Exception as e:
        logger.error(f"Failed to initialize stock sync: {e}")
        return {"success": False, "message": f"初始化股票同步失败: {e}"}

    try:
        # Older deployments receive this additive schema migration before the swap.
        _ensure_listing_status_column(ch)
        # ClickHouse stocks table uses a simplified schema (no id/self_selected/holding/created_at).
        existing_full = _reference_rows_by_code(ch)
        reference_columns = ['code','name','market','type','industry','region','list_date','listing_status','delist_date','quit','st']
        existing_rows = [tuple(row.get(key) for key in reference_columns)
                         for row in existing_full.values() if row.get('type') == 'stock']
        existing_codes = {str(r[0]) for r in existing_rows if r and r[0]}
        existing_by_code = {str(row[0]): row for row in existing_rows if row and row[0]}

        new_count = 0
        update_count = 0
        skip_count = 0

        current_codes = set()
        rows_to_insert = []
        retired_marked = 0
        unresolved_removed_retained = 0

        metadata_unknown_codes = []
        protected_index_codes = []
        def _to_date_or_none(value):
            return _reference_listing_date({'list_date':value})

        for idx, item in enumerate(stock_list, start=1):
            code = item.get("Code") or item.get("code", "")
            name = item.get("Name") or item.get("name", "")
            if not code or not name:
                skip_count += 1
                continue

            if existing_full.get(str(code),{}).get('type') == 'index':
                protected_index_codes.append(str(code))
                skip_count += 1
                continue
            current_codes.add(code)
            market = code.split(".")[-1] if "." in code else ""

            # The official pool already carries the batched QMT detail. Retry only
            # missing details, and never fall back to an unrelated source.
            stock_info = item if item.get("source") == "qmt_xtquant" and not item.get("metadata_unknown") else None
            if stock_info is None:
                try:
                    stock_info = data_sources.call_with_failover("get_stock_info", code, source_name="qmt_xtquant")
                except Exception as exc:
                    logger.warning(f"Load QMT stock info failed for {code}: {exc}")
            if not stock_info or stock_info.get("metadata_unknown"):
                metadata_unknown_codes.append(code)
                stock_info = item
                if code in existing_by_code:
                    name = existing_by_code[code][1] or name

            st = int((stock_info or {}).get("st", 0) or 0)
            quit_flag = int((stock_info or {}).get("quit", 0) or 0)
            stock_type = str((stock_info or {}).get("type", "stock") or "stock")
            float_share = float((stock_info or {}).get("float_share", 0.0) or 0.0)
            total_share = float((stock_info or {}).get("total_share", 0.0) or 0.0)
            industry = str((stock_info or {}).get("industry", "") or "")
            industry_code = str((stock_info or {}).get("industry_code", "") or "")
            region = str((stock_info or {}).get("region", "") or "")
            raw_list_date = (stock_info or {}).get("list_date")
            previous = existing_by_code.get(code, [None] * 8)
            list_date = _reference_listing_date(stock_info or {}, previous[6])
            listing_status = _reference_listing_status(stock_info or {}, list_date, previous[7])
            raw_delist_date = (stock_info or {}).get("delist_date")
            delist_date = _to_date_or_none(raw_delist_date) if raw_delist_date else None
            if code in metadata_unknown_codes and code in existing_by_code:
                retained = existing_by_code[code]
                industry, region = retained[4] or "", retained[5] or ""
                listing_status = str(retained[7] or 'unknown')
                delist_date = _to_date_or_none(retained[8])
                quit_flag, st = int(retained[9] or 0), int(retained[10] or 0)

            if code in existing_codes:
                update_count += 1
            else:
                new_count += 1

            rows_to_insert.append([
                code,
                name,
                market,
                stock_type,
                industry,
                region,
                list_date,
                listing_status,
                delist_date,
                quit_flag,
                st,
            ])

            if idx % 100 == 0:
                try:
                    from api.system_config import task_manager  # lazy import to avoid hard circular import at module load
                    task_manager.update_progress(
                        "update_stock_list",
                        {
                            "current": idx,
                            "total": len(stock_list),
                            "message": f"股票列表同步中 {idx}/{len(stock_list)}",
                        },
                    )
                except Exception:
                    pass

        # The normal QMT A-share sector no longer contains delisted symbols.
        # Keep prior metadata rows, and convert only the codes that QMT's
        # expired-contract cache positively confirms through ExpireDate.
        removed_codes = sorted(existing_codes - current_codes)
        expired_details: Dict[str, Dict[str, Any]] = {}
        qmt_source = None
        try:
            qmt_source = data_sources.get_source("qmt_xtquant")
            if qmt_source and hasattr(qmt_source, "get_expired_stock_info"):
                expired_details = qmt_source.get_expired_stock_info(removed_codes)
        except Exception as exc:
            logger.warning(f"QMT expired-contract reconciliation failed: {exc}")

        for code in removed_codes:
            existing = existing_by_code[code]
            expired = expired_details.get(code)
            if expired:
                name = str(expired.get("Name") or expired.get("name") or existing[1])
                market = str(expired.get("market") or existing[2] or code.split(".")[-1])
                industry = str(expired.get("industry") or existing[4] or "")
                region = str(expired.get("region") or existing[5] or "")
                list_date = _reference_listing_date(expired, existing[6])
                listing_status = _reference_listing_status(expired, list_date, existing[7])
                delist_date = _to_date_or_none(expired.get("delist_date") or existing[8])
                quit_flag = 1 if delist_date else int(existing[9] or 0)
                st = int(expired.get("st", existing[10]) or 0)
                retired_marked += int(bool(delist_date))
            else:
                # Do not turn a transient upstream omission into a false delist.
                name = existing[1]
                market = existing[2]
                industry = existing[4]
                region = existing[5]
                list_date = existing[6]
                listing_status = existing[7]
                delist_date = existing[8]
                quit_flag = existing[9]
                st = existing[10]
                list_date = _to_date_or_none(list_date)
                quit_flag = int(quit_flag or 0)
                st = int(st or 0)
                unresolved_removed_retained += 1
            current_codes.add(code)
            rows_to_insert.append([code, name, market, "stock", industry or "", region or "", list_date, listing_status, delist_date, quit_flag, st])

        deleted_count = len(existing_codes - current_codes)
        t_transform_done = time.perf_counter()
        try:
            from api.system_config import task_manager
            task_manager.update_progress("update_stock_list", {"stage": "build", "message": "股票列表更新: 构建临时表"})
        except Exception:
            pass

        # 避免 5k+ 行 ALTER UPDATE/DELETE mutation
        tmp_table = "stocks_sync_tmp"
        backup_table = "stocks_sync_backup"
        ch.command(f"DROP TABLE IF EXISTS {tmp_table}")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")
        ch.command(f"CREATE TABLE {tmp_table} AS stocks")

        # 只写入 stock 类型
        ch.command(
            f"""
            INSERT INTO {tmp_table}
            SELECT * FROM stocks WHERE type != 'stock' OR type IS NULL
            """
        )

        if rows_to_insert:
            # clickhouse-connect infers a non-null Date writer from mixed
            # Python values in one batch.  Split null/non-null delist dates so
            # the Nullable(Date) column remains stable on every host version.
            def _has_delist_date(value: Any) -> bool:
                return value is not None and not bool(pd.isna(value))

            with_delist_date = [row for row in rows_to_insert if _has_delist_date(row[8])]
            without_delist_date = [row for row in rows_to_insert if not _has_delist_date(row[8])]
            if without_delist_date:
                _insert_reference_rows(
                    ch, tmp_table,
                    without_delist_date,
                    columns=["code", "name", "market", "type", "industry", "region", "list_date", "listing_status", "delist_date", "quit", "st"],
                    existing=existing_full,
                )
            if with_delist_date:
                _insert_reference_rows(
                    ch, tmp_table,
                    [row[:8] + [_to_date_or_none(row[8])] + row[9:] for row in with_delist_date],
                    columns=[
                        "code", "name", "market", "type", "industry", "region", "list_date", "listing_status", "delist_date",
                        "quit", "st",
                    ],
                    existing=existing_full,
                )
        t_load_done = time.perf_counter()

        # 原子交换表
        try:
            from api.system_config import task_manager
            task_manager.update_progress("update_stock_list", {"stage": "swap", "message": "股票列表更新: 交换表"})
        except Exception:
            pass
        try:
            from api.system_config import task_manager
            task_manager.update_progress("update_index_list", {"stage": "swap", "message": "股票列表更新: 交换表"})
        except Exception:
            pass
        ch.command(f"RENAME TABLE stocks TO {backup_table}, {tmp_table} TO stocks")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")
        t_swap_done = time.perf_counter()

        from services.operations.health import write_snapshot
        from utils.paths import runtime_path
        listing_unknown_codes = [row[0] for row in rows_to_insert if row[7] == 'unknown']
        pending_listing_codes = [row[0] for row in rows_to_insert if row[7] == 'pending_listing']
        pool_metadata = getattr(qmt_source,'last_stock_list_metadata',{}) or {}
        # ``official_codes`` is the raw QMT sector membership.  It can include
        # non-stock contracts which QMT itself identifies in instrument detail
        # (for example ProductID=R).  Treating that raw count as the expected
        # A-share stock count made a correct source-side exclusion look like a
        # backend data loss.  Publish both facts, but use the detail-validated
        # QMT stock pool for the reference-data contract.
        raw_official_count = len(pool_metadata['official_codes']) if 'official_codes' in pool_metadata else None
        validated_official_count = len(pool_metadata['returned_codes']) if 'returned_codes' in pool_metadata else None
        metadata_status = {'generated_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                           'source':'qmt_xtquant', 'returned_pool_count':len(stock_list),
                           'official_pool_count':validated_official_count,
                           'qmt_raw_sector_count':raw_official_count,
                           'pool_evidence':pool_metadata,
                           'excluded_from_stock_scope':[{'code':code,'reason':'existing_index_metadata','retained_type':'index'} for code in sorted(set(protected_index_codes))],
                           'metadata_unknown_codes':sorted(set(metadata_unknown_codes)),
                           'listing_unknown_codes':sorted(set(listing_unknown_codes)),
                           'pending_listing_codes':sorted(set(pending_listing_codes)),
                           'metadata_verified':(validated_official_count == len(stock_list)
                                                and not metadata_unknown_codes and not listing_unknown_codes)}
        metadata_status['status'] = 'healthy' if metadata_status['metadata_verified'] else 'unverified'
        write_snapshot(metadata_status,runtime_path('operations','reference_metadata.json'))
        metrics = {
            "fetch_ms": int((t_fetch_done - t0) * 1000),
            "transform_ms": int((t_transform_done - t_fetch_done) * 1000),
            "load_ms": int((t_load_done - t_transform_done) * 1000),
            "swap_ms": int((t_swap_done - t_load_done) * 1000),
            "total_ms": int((t_swap_done - t0) * 1000),
            "rows_in": len(stock_list),
            "rows_out": len(rows_to_insert),
            "retired_marked": retired_marked,
            "unresolved_removed_retained": unresolved_removed_retained,
        }
        metrics["qps"] = round((metrics["rows_out"] / max(metrics["total_ms"], 1)) * 1000, 2)

        logger.info(
            f"Stock list update finished: new={new_count}, update={update_count}, skip={skip_count}, deleted={deleted_count}, total={len(stock_list)}"
        )
        return {
            "success": True,
            "message": "股票列表更新完成",
            "new_count": new_count,
            "update_count": update_count,
            "skip_count": skip_count,
            "deleted_count": deleted_count,
            "total_count": len(stock_list),
            "metrics": metrics,
            "metadata": metadata_status,
        }
    except Exception as e:
        logger.error(f"Stock list update failed: {e}")
        return {"success": False, "message": f"股票列表更新失败: {e}"}


@router.get("/{code}/kline/{period}")
def get_stock_kline(code: str, period: str, limit: int = 100):
    """
    获取股票K线数据
    
    Args:
        code: 股票代码
        period: 周期 (1m, 15m, 30m, 60m, 1d, 1w, 1mon, 1q, 1y)
        limit: 返回数据条数
    
    Returns:
        K线数据
    """
    session = DatabaseSessionManager.get_session()
    try:
        # 兼容旧版 period 别名（daily/weekly/monthly/yearly → 1d/1w/1mon/1y）
        _period_alias = {
            "daily": "1d", "weekly": "1w", "monthly": "1mon",
            "quarterly": "1q", "yearly": "1y",
        }
        period = _period_alias.get(period, period)

        # 检查股票是否存在
        stock = _get_stock_by_code(session, code)
        if not stock:
            raise HTTPException(status_code=404, detail=f"股票 {code} 不存在")
        
        # 映射周期到模型（使用统一的时间单位标准）
        period_model_map = {
            '1d': KlineDaily,
            '1w': KlineWeekly,
            '1mon': KlineMonthly,
            '1q': KlineQuarterly,
            '1m': KlineMinute1,
            '15m': KlineMinute15,
            '30m': KlineMinute30,
            '60m': KlineMinute60
        }
        
        model = period_model_map.get(period)
        if not model:
            raise HTTPException(status_code=400, detail=f"不支持的周期: {period}")
        
        # 根据周期获取日期字段（使用统一的时间单位标准）
        date_field_map = {
            '1d': 'trade_date',
            '1w': 'week_start_date',
            '1mon': 'month_start_date',
            '1q': 'year',
            '1y': 'year',
            '1m': 'datetime',
            '15m': 'datetime',
            '30m': 'datetime',
            '60m': 'datetime'
        }
        date_field = date_field_map.get(period)

        if period in CLICKHOUSE_KLINE_TABLES and clickhouse_available():
            try:
                table_name, clickhouse_date_field = CLICKHOUSE_KLINE_TABLES[period]
                fetch_limit = int(limit)
                if period in {"15m", "30m", "60m"}:
                    fetch_limit = max(int(limit), 1) * 4
                df = clickhouse_query_df(
                    f"""
                    SELECT {clickhouse_date_field} AS date_value,
                           code, open, high, low, close, volume, amount
                    FROM {table_name} FINAL
                    WHERE code = ?
                    ORDER BY {clickhouse_date_field} DESC
                    LIMIT ?
                    """,
                    [code, fetch_limit],
                )

                data = []
                if df is not None and not df.empty:
                    if period in {"15m", "30m", "60m"}:
                        df = clean_minute_bars_df(df, period, datetime_col="date_value", code_col="code")
                        if len(df) > int(limit):
                            df = df.tail(int(limit)).reset_index(drop=True)
                    df = df.sort_values("date_value")
                    for row in df.itertuples(index=False):
                        date_value = getattr(row, "date_value")
                        if isinstance(date_value, datetime):
                            date_text = (
                                _minute_datetime_to_business_time(date_value).isoformat()
                                if period in {"1m", "5m", "15m", "30m", "60m"}
                                else date_value.isoformat()
                            )
                        else:
                            date_text = str(date_value)
                        data.append({
                            "open": float(row.open) if row.open is not None else 0,
                            "high": float(row.high) if row.high is not None else 0,
                            "low": float(row.low) if row.low is not None else 0,
                            "close": float(row.close) if row.close is not None else 0,
                            "volume": int(row.volume) if row.volume is not None else 0,
                            "amount": float(row.amount) if row.amount is not None else 0,
                            "date": date_text,
                        })

                intraday_daily = _build_intraday_daily_bar(code) if period == "1d" else None
                if intraday_daily:
                    today_text = str(intraday_daily["date"])[:10]
                    data = [item for item in data if str(item.get("date") or "")[:10] != today_text]
                    data.append(intraday_daily)
                    data = sorted(data, key=lambda item: str(item.get("date") or ""))[-int(limit):]

                return {
                    "code": code,
                    "name": stock.name,
                    "period": period,
                    "data": data,
                    "total": len(data),
                    "source": "clickhouse+intraday_daily" if intraday_daily else "clickhouse",
                    "intraday_daily": intraday_daily,
                }
            except Exception as exc:
                logger.warning(f"ClickHouse kline query fallback to SQLAlchemy engine: {exc}")
        
        # 查询K线数据
        query = session.query(model).filter(model.code == code)
        
        # 按日期降序排序
        if date_field:
            if hasattr(model, date_field):
                query = query.order_by(getattr(model, date_field).desc())
        
        # 限制返回数量
        klines = query.limit(limit).all()
        
        # 反转数据，使时间从早到晚
        klines = reversed(klines)
        
        # 转换为前端需要的格式
        data = []
        for kline in klines:
            kline_data = {
                'open': float(kline.open) if kline.open else 0,
                'high': float(kline.high) if kline.high else 0,
                'low': float(kline.low) if kline.low else 0,
                'close': float(kline.close) if kline.close else 0,
                'volume': int(kline.volume) if kline.volume else 0,
                'amount': float(kline.amount) if kline.amount else 0
            }
            
            # 添加日期字段
            if date_field:
                if hasattr(kline, date_field):
                    date_value = getattr(kline, date_field)
                    if isinstance(date_value, datetime):
                        kline_data['date'] = date_value.isoformat()
                    else:
                        kline_data['date'] = str(date_value)
            
            data.append(kline_data)
        
        return {
            'code': code,
            'name': stock.name,
            'period': period,
            'data': data,
            'total': len(data)
        }
        
    finally:
        DatabaseSessionManager.close_session(session)


def _reference_listing_date(index, previous=None):
    """Use dated QMT evidence, including a near-term IPO date, never the refresh date."""
    from datetime import date, timedelta

    def parse(value):
        if isinstance(value, datetime):
            value = value.date()
        if not isinstance(value, date):
            text = str(value or '').strip().replace('/', '-')
            try:
                value = datetime.strptime(text, '%Y%m%d').date() if len(text) == 8 and text.isdigit() else date.fromisoformat(text[:10])
            except (ValueError, TypeError, OverflowError):
                return None
        # QMT publishes an IPO date shortly before trading begins.  Keep that
        # evidence, but reject epoch sentinels and implausible far-future data.
        today = date.today()
        return value if date(1900, 1, 1) <= value <= today + timedelta(days=366) and value != date(1970, 1, 1) else None

    return parse(index.get('list_date')) or parse(index.get('OpenDate')) or parse(previous)


def _reference_listing_status(reference, list_date, previous=None):
    """Classify QMT instruments so unlisted contracts never enter live pools."""
    from datetime import date
    today = date.today()
    if list_date is not None:
        return 'pending_listing' if list_date > today else 'active'

    raw_open_date = str(reference.get('OpenDate') or reference.get('list_date') or '').strip()
    raw_create_date = str(reference.get('CreateDate') or '').strip()
    # QMT uses the epoch OpenDate together with a real CreateDate for a newly
    # created contract that has not begun trading (for example 301686.SZ).
    if raw_open_date in {'0', '00000000', '19700101', '1970-01-01'} and raw_create_date:
        return 'pending_listing'
    if previous == 'pending_listing':
        return previous
    return 'unknown'


def _earliest_daily_dates(client, codes):
    """Return evidence-backed first observed daily date for unresolved indices."""
    if not codes:
        return {}
    result = client.query(
        "SELECT code, min(trade_date) AS earliest_trade_date "
        "FROM kline_daily WHERE code IN {codes:Array(String)} GROUP BY code",
        parameters={'codes': sorted(set(codes))},
    )
    from datetime import date
    dates = {}
    for code, value in result.result_rows:
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date):
            dates[str(code)] = value
        elif value:
            try:
                dates[str(code)] = date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
    return dates


@router.post("/update-indices")
def update_indices():
    """更新指数列表到 ClickHouse。"""
    from data_fetcher.manager import DataSourceManager
    from clickhouse_connect import get_client
    import logging
    import os

    logger = logging.getLogger(__name__)
    import time
    t0 = time.perf_counter()
    t_fetch_done = t0
    t_transform_done = t0
    t_load_done = t0
    t_swap_done = t0

    try:
        data_sources = DataSourceManager()
        logger.info("开始更新指数列表")
        try:
            from api.system_config import task_manager
            task_manager.update_progress("update_index_list", {"stage": "fetch", "message": "指数列表更新: 拉取中"})
        except Exception:
            pass
        indices = data_sources.call_with_failover("get_stock_list", market="9", list_type=1, source_name="qmt_xtquant")
        t_fetch_done = time.perf_counter()
        if not indices:
            logger.warning("指数列表为空")
            return {"success": 0, "error": "指数列表为空"}

        host = os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1")
        port = int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123"))
        database = os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock")
        username = os.getenv("AISTOCK_CLICKHOUSE_USER", "default")
        password = os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", "")
        ch = get_client(host=host, port=port, database=database, username=username, password=password)

        _ensure_listing_status_column(ch)
        existing_full = _reference_rows_by_code(ch)
        existing_rows = [(row['code'],row.get('type'),row.get('list_date'),row.get('listing_status')) for row in existing_full.values()]
        existing_map = {
            str(r[0]): {
                "type": str(r[1] or ""),
                "list_date": r[2],
                "listing_status": str(r[3] or "active"),
            }
            for r in existing_rows if r and r[0]
        }

        added_count = 0
        updated_count = 0
        skipped_count = 0

        index_codes = set()
        index_rows = []

        for index in indices:
            code = str(index.get("code") or "").strip()
            name = str(index.get("name") or "").strip()
            market = str(index.get("market") or "").strip()

            if not code or not name:
                skipped_count += 1
                continue

            exist = existing_map.get(code)
            if exist and exist.get("type") == "stock":
                skipped_count += 1
                continue

            if code in index_codes:
                skipped_count += 1
                continue
            index_codes.add(code)

            if exist and exist.get("type") == "index":
                updated_count += 1
            else:
                added_count += 1

            list_date = _reference_listing_date(index, (exist or {}).get("list_date"))
            index_rows.append([
                code,
                name,
                market,
                "index",
                "",
                "",
                list_date,
                _reference_listing_status(index, list_date, (exist or {}).get("listing_status")),
                0,
                0,
            ])

        # QMT does not supply a bulk authoritative publication date for every
        # index.  When neither QMT nor retained metadata has one, the earliest
        # actual daily market-data record is a transparent operational fallback.
        fallback_codes = [row[0] for row in index_rows if row[6] is None]
        fallback_dates = _earliest_daily_dates(ch, fallback_codes)
        fallback_evidence = []
        for row in index_rows:
            fallback_date = fallback_dates.get(row[0])
            if row[6] is None and fallback_date is not None:
                row[6] = fallback_date
                row[7] = 'active'
                fallback_evidence.append({
                    'code': row[0],
                    'list_date': fallback_date.isoformat(),
                    'source': 'kline_daily.min(trade_date)',
                    'reason': 'qmt_index_listing_date_unavailable',
                })

        try:
            from services.operations.health import write_snapshot
            from utils.paths import runtime_path
            write_snapshot(
                {'generated_at': datetime.now().astimezone().isoformat(),
                 'source': 'qmt_xtquant',
                 'fallbacks': fallback_evidence},
                runtime_path('operations', 'index_listing_date_fallback.json'),
            )
        except Exception as exc:
            logger.warning('Unable to write index listing-date fallback evidence: %s', exc)

        t_transform_done = time.perf_counter()
        try:
            from api.system_config import task_manager
            task_manager.update_progress("update_index_list", {"stage": "build", "message": "指数列表更新: 构建临时表"})
        except Exception:
            pass

        tmp_table = "stocks_index_sync_tmp"
        backup_table = "stocks_index_sync_backup"
        ch.command(f"DROP TABLE IF EXISTS {tmp_table}")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")
        ch.command(f"CREATE TABLE {tmp_table} AS stocks")

        # 仅替换本轮有效更新；QMT暂缺/无效的旧指数完整保留。
        ch.command(
            f"""
            INSERT INTO {tmp_table}
            SELECT * FROM stocks
            WHERE type != 'index' OR type IS NULL OR code NOT IN {{updated_codes:Array(String)}}
            """, parameters={'updated_codes':sorted(index_codes)}
        )

        if index_rows:
            _insert_reference_rows(
                ch, tmp_table,
                index_rows,
                columns=[
                    "code", "name", "market", "type", "industry", "region", "list_date", "listing_status",
                    "quit", "st",
                ],
                existing=existing_full,
            )
        t_load_done = time.perf_counter()

        ch.command(f"RENAME TABLE stocks TO {backup_table}, {tmp_table} TO stocks")
        ch.command(f"DROP TABLE IF EXISTS {backup_table}")
        t_swap_done = time.perf_counter()

        metrics = {
            "fetch_ms": int((t_fetch_done - t0) * 1000),
            "transform_ms": int((t_transform_done - t_fetch_done) * 1000),
            "load_ms": int((t_load_done - t_transform_done) * 1000),
            "swap_ms": int((t_swap_done - t_load_done) * 1000),
            "total_ms": int((t_swap_done - t0) * 1000),
            "rows_in": len(indices),
            "rows_out": len(index_rows),
        }
        metrics["qps"] = round((metrics["rows_out"] / max(metrics["total_ms"], 1)) * 1000, 2)

        logger.info(f"指数列表更新完成: 新增 {added_count} 条，更新 {updated_count} 条，跳过 {skipped_count} 条")
        return {
            "success": added_count + updated_count,
            "added": added_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "metrics": metrics,
        }

    except Exception as e:
        logger.error(f"指数列表更新失败: {e}")
        return {"success": 0, "error": str(e)}


@router.post("/select/upper-shadow")
def select_stocks_by_upper_shadow(
    request: dict
):
    """上影线选股"""
    ratio = request.get("ratio", 50)
    volume = request.get("volume", 100000)
    start_date = request.get("start_date")
    end_date = request.get("end_date")
    
    session = DatabaseSessionManager.get_session()
    try:
        if clickhouse_available():
            try:
                where_clauses = ["s.type = 'stock'", "k.volume >= ?"]
                params: List[Any] = [volume]
                if start_date:
                    where_clauses.append("k.trade_date >= ?")
                    params.append(str(start_date))
                if end_date:
                    where_clauses.append("k.trade_date <= ?")
                    params.append(str(end_date))
                where_sql = " AND ".join(where_clauses)
                df = clickhouse_query_df(
                    f"""
                    WITH latest AS (
                        SELECT k.code, MAX(k.trade_date) AS max_date
                        FROM kline_daily k
                        JOIN stocks s ON s.code = k.code
                        WHERE {where_sql}
                          AND k.high > GREATEST(k.open, k.close)
                          AND ABS(k.close - k.open) > 0
                          AND (k.high - GREATEST(k.open, k.close)) / ABS(k.close - k.open) * 100 >= ?
                        GROUP BY k.code
                    )
                    SELECT s.code, s.name, k.close, k.change_pct, k.volume
                    FROM latest l
                    JOIN kline_daily k ON k.code = l.code AND k.trade_date = l.max_date
                    JOIN stocks s ON s.code = k.code
                    ORDER BY k.trade_date DESC, s.code
                    """,
                    params + [ratio],
                )
                if df is not None:
                    return [
                        {
                            "code": row.code,
                            "name": row.name,
                            "close": row.close,
                            "change_pct": row.change_pct,
                            "volume": row.volume,
                        }
                        for row in df.itertuples(index=False)
                    ]
            except Exception as exc:
                logger.warning(f"ClickHouse upper-shadow selector fallback to SQLAlchemy engine: {exc}")

        # 构建查询
        query = session.query(
            Stock.code,
            Stock.name,
            KlineDaily.close,
            KlineDaily.change_pct,
            KlineDaily.volume
        ).join(
            KlineDaily, Stock.code == KlineDaily.code
        )
        
        # 筛选日期
        if start_date:
            query = query.filter(KlineDaily.trade_date >= start_date)
        if end_date:
            query = query.filter(KlineDaily.trade_date <= end_date)
        
        # 筛选成交量
        query = query.filter(KlineDaily.volume >= volume)
        
        # 筛选上影线长度
        # 上影线长度 = 最高价 - max(开盘价, 收盘价)
        # 实体长度 = abs(收盘价 - 开盘价)
        # 上影线长度比例 = 上影线长度 / 实体长度 * 100
        query = query.filter(
            and_(
                KlineDaily.high > func.greatest(KlineDaily.open, KlineDaily.close),
                func.abs(KlineDaily.close - KlineDaily.open) > 0,
                (KlineDaily.high - func.greatest(KlineDaily.open, KlineDaily.close)) / func.abs(KlineDaily.close - KlineDaily.open) * 100 >= ratio
            )
        )
        
        # 只获取最新的一条记录
        subquery = session.query(
            KlineDaily.code,
            func.max(KlineDaily.trade_date).label("max_date")
        ).group_by(KlineDaily.code).subquery()
        
        query = query.join(
            subquery,
            and_(
                KlineDaily.code == subquery.c.code,
                KlineDaily.trade_date == subquery.c.max_date
            )
        )
        
        # 执行查询
        results = query.all()
        
        # 构建响应
        return [
            {
                "code": item.code,
                "name": item.name,
                "close": item.close,
                "change_pct": item.change_pct,
                "volume": item.volume
            }
            for item in results
        ]
    finally:
        DatabaseSessionManager.close_session(session)

@router.post("/select/main-rise")
def select_stocks_by_main_rise(
    request: dict
):
    """主升浪选股"""
    days = request.get("days", 3)
    rise = request.get("rise", 10)
    volume = request.get("volume", 100000)
    
    session = DatabaseSessionManager.get_session()
    try:
        # 计算起始日期
        end_date = _latest_daily_trade_date(session)
        if not end_date:
            return []
        
        start_date = end_date - timedelta(days=days-1)

        if clickhouse_available():
            try:
                df = clickhouse_query_df(
                    """
                    WITH agg AS (
                        SELECT
                            code,
                            SUM(change_pct) AS total_rise,
                            AVG(volume) AS avg_volume,
                            COUNT(*) AS days_count
                        FROM kline_daily
                        WHERE trade_date >= ? AND trade_date <= ?
                        GROUP BY code
                    )
                    SELECT s.code, s.name, k.close, k.change_pct, k.volume
                    FROM agg a
                    JOIN stocks s ON s.code = a.code
                    JOIN kline_daily k ON k.code = a.code AND k.trade_date = ?
                    WHERE s.type = 'stock'
                      AND a.days_count = ?
                      AND a.total_rise >= ?
                      AND a.avg_volume >= ?
                    ORDER BY a.total_rise DESC, s.code
                    """,
                    [str(start_date), str(end_date), str(end_date), int(days), rise, volume],
                )
                if df is not None:
                    return [
                        {
                            "code": row.code,
                            "name": row.name,
                            "close": row.close,
                            "change_pct": row.change_pct,
                            "volume": row.volume,
                        }
                        for row in df.itertuples(index=False)
                    ]
            except Exception as exc:
                logger.warning(f"ClickHouse main-rise selector fallback to SQLAlchemy engine: {exc}")
        
        # 构建查询，获取每个股票在指定日期范围内的涨跌幅和成交量
        subquery = session.query(
            KlineDaily.code,
            func.sum(KlineDaily.change_pct).label("total_rise"),
            func.avg(KlineDaily.volume).label("avg_volume"),
            func.count(KlineDaily.code).label("days_count")
        ).filter(
            KlineDaily.trade_date >= start_date,
            KlineDaily.trade_date <= end_date
        ).group_by(KlineDaily.code).subquery()
        
        # 筛选符合条件的股票
        query = session.query(
            Stock.code,
            Stock.name,
            KlineDaily.close,
            KlineDaily.change_pct,
            KlineDaily.volume
        ).join(
            subquery, Stock.code == subquery.c.code
        ).join(
            KlineDaily, 
            and_(
                Stock.code == KlineDaily.code,
                KlineDaily.trade_date == end_date
            )
        ).filter(
            subquery.c.days_count == days,
            subquery.c.total_rise >= rise,
            subquery.c.avg_volume >= volume
        )
        
        # 执行查询
        results = query.all()
        
        # 构建响应
        return [
            {
                "code": item.code,
                "name": item.name,
                "close": item.close,
                "change_pct": item.change_pct,
                "volume": item.volume
            }
            for item in results
        ]
    finally:
        DatabaseSessionManager.close_session(session)


def _evaluate_main_rise_build_up_window(
    klines,
    avg_volume_ratio_threshold: float = 1.3,
    min_amplitude: float = 8.0,
    max_amplitude: float = 20.0,
    max_down_day_drop_pct: float = 6.0,
    max_avg_down_pct: float = 3.0,
):
    """评估单个窗口是否满足主升浪建仓（红肥绿瘦）条件。"""
    if not klines:
        return None

    up_days = 0
    down_days = 0
    up_volume = 0
    down_volume = 0
    highs = []
    lows = []
    down_drop_sum = 0.0
    down_drop_max = 0.0

    for item in klines:
        open_price = float(item.open or 0)
        close_price = float(item.close or 0)
        volume = int(item.volume or 0)
        high_price = float(item.high or 0)
        low_price = float(item.low or 0)

        highs.append(high_price)
        lows.append(low_price)

        if close_price > open_price:
            up_days += 1
            up_volume += volume
        elif close_price < open_price:
            down_days += 1
            down_volume += volume
            if open_price > 0:
                drop_pct = (open_price - close_price) / open_price * 100
                down_drop_sum += float(drop_pct)
                down_drop_max = max(down_drop_max, float(drop_pct))

    # 条件1：阳线天数 > 阴线天数
    if up_days <= down_days:
        return None

    # 条件2：振幅介入 8%-20%（窗口振幅区间）
    min_low = min(lows) if lows else 0
    max_high = max(highs) if highs else 0
    if min_low <= 0:
        return None
    amplitude = (max_high - min_low) / min_low * 100
    if amplitude < float(min_amplitude) or amplitude > float(max_amplitude):
        return None

    # 条件3：周期内阴线最大跌幅不得超过 6%
    if down_days > 0 and down_drop_max > float(max_down_day_drop_pct):
        return None

    # 条件4：上涨K线平均成交量 >= 下跌K线平均成交量 * 1.3
    up_avg_vol = (up_volume / up_days) if up_days > 0 else 0.0
    down_avg_vol = (down_volume / down_days) if down_days > 0 else 0.0
    if down_avg_vol > 0:
        if up_avg_vol < down_avg_vol * float(avg_volume_ratio_threshold):
            return None
        avg_volume_ratio_value = up_avg_vol / down_avg_vol
    else:
        avg_volume_ratio_value = 999.0 if up_avg_vol > 0 else 0.0

    # 条件5：阴线平均跌幅 <= 3%
    down_avg_drop = (down_drop_sum / down_days) if down_days > 0 else 0.0
    if down_days > 0 and down_avg_drop > float(max_avg_down_pct):
        return None

    return {
        "up_days": up_days,
        "down_days": down_days,
        "up_volume": up_volume,
        "down_volume": down_volume,
        "up_avg_volume": round(float(up_avg_vol), 2),
        "down_avg_volume": round(float(down_avg_vol), 2),
        "volume_ratio": round(float(avg_volume_ratio_value), 2),
        "amplitude": round(amplitude, 2),
        "max_down_drop_pct": round(float(down_drop_max), 2),
        "avg_down_drop_pct": round(float(down_avg_drop), 2),
    }


@router.post("/select/main-rise-build-up")
def select_stocks_by_main_rise_build_up(
    request: dict
):
    """主升浪建仓阶段选股（红肥绿瘦）。"""
    days = int(request.get("days", 30))
    # 固定规则（红肥绿瘦增强版）
    float_cap_min_yi = float(request.get("float_cap_min_yi", 80))
    float_cap_max_yi = float(request.get("float_cap_max_yi", 300))
    min_amplitude = float(request.get("min_amplitude", 8))
    max_amplitude = float(request.get("max_amplitude", 20))
    max_down_day_drop_pct = float(request.get("max_down_day_drop_pct", 6))
    avg_volume_ratio_threshold = float(request.get("avg_volume_ratio", 1.3))
    max_avg_down_pct = float(request.get("max_avg_down_pct", 3))

    if days <= 0:
        raise HTTPException(status_code=400, detail="days 必须大于 0")

    session = DatabaseSessionManager.get_session()
    try:
        end_date = _latest_daily_trade_date(session)
        if not end_date:
            params = {
                "days": days,
                "float_cap_min_yi": float_cap_min_yi,
                "float_cap_max_yi": float_cap_max_yi,
                "min_amplitude": min_amplitude,
                "max_amplitude": max_amplitude,
                "max_down_day_drop_pct": max_down_day_drop_pct,
                "avg_volume_ratio": avg_volume_ratio_threshold,
                "max_avg_down_pct": max_avg_down_pct,
            }
            summary = {"trade_date": None, "total_candidates": 0}
            run_id = _save_selector_run(
                session=session,
                run_type="realtime",
                params=params,
                summary=summary,
                items=[],
            )
            return {"run_id": run_id, "items": []}

        # 上市不足2个月剔除（按60个自然日近似）
        min_list_date = end_date - timedelta(days=60)

        candidate_stocks = session.query(Stock).filter(
            Stock.type == 'stock',
            Stock.quit == False,
            Stock.st == False,
            Stock.list_date.isnot(None),
            Stock.list_date <= min_list_date,
            ~Stock.name.like('ST%'),
            ~Stock.name.like('*ST%')
        ).all()

        results = []

        for stock in candidate_stocks:
            klines = session.query(KlineDaily).filter(
                KlineDaily.code == stock.code,
                KlineDaily.trade_date <= end_date
            ).order_by(KlineDaily.trade_date.desc()).limit(days).all()

            if len(klines) < days:
                continue

            klines = list(reversed(klines))
            latest_kline = klines[-1]

            # 剔除停牌：最新交易日不是全市场最新交易日，或最新成交量为0
            if latest_kline.trade_date != end_date:
                continue
            if latest_kline.volume is None or latest_kline.volume <= 0:
                continue

            # 条件1：流通盘（流通市值）在 80-300 亿之间
            float_share_wan = float(stock.float_share or 0)  # 万股
            latest_close = float(latest_kline.close or 0)
            float_cap_yi = (float_share_wan * 10000 * latest_close) / 1e8 if float_share_wan > 0 and latest_close > 0 else 0.0
            if float_cap_yi < float_cap_min_yi or float_cap_yi > float_cap_max_yi:
                continue

            metrics = _evaluate_main_rise_build_up_window(
                klines=klines,
                avg_volume_ratio_threshold=avg_volume_ratio_threshold,
                min_amplitude=min_amplitude,
                max_amplitude=max_amplitude,
                max_down_day_drop_pct=max_down_day_drop_pct,
                max_avg_down_pct=max_avg_down_pct,
            )
            if not metrics:
                continue

            results.append({
                "code": stock.code,
                "name": stock.name,
                "close": float(latest_kline.close or 0),
                "change_pct": float(latest_kline.change_pct or 0),
                "volume": int(latest_kline.volume or 0),
                "up_days": metrics["up_days"],
                "down_days": metrics["down_days"],
                "up_volume": metrics["up_volume"],
                "down_volume": metrics["down_volume"],
                "up_avg_volume": metrics["up_avg_volume"],
                "down_avg_volume": metrics["down_avg_volume"],
                "volume_ratio": metrics["volume_ratio"],
                "amplitude": metrics["amplitude"],
                "max_down_drop_pct": metrics["max_down_drop_pct"],
                "avg_down_drop_pct": metrics["avg_down_drop_pct"],
                "float_cap_yi": round(float(float_cap_yi), 2),
                "window_days": days,
            })

        # 优先展示量价比更高的标的
        results.sort(key=lambda x: (x["volume_ratio"], -x["amplitude"]), reverse=True)
        params = {
            "days": days,
            "float_cap_min_yi": float_cap_min_yi,
            "float_cap_max_yi": float_cap_max_yi,
            "min_amplitude": min_amplitude,
            "max_amplitude": max_amplitude,
            "max_down_day_drop_pct": max_down_day_drop_pct,
            "avg_volume_ratio": avg_volume_ratio_threshold,
            "max_avg_down_pct": max_avg_down_pct,
        }
        summary = {
            "trade_date": end_date.isoformat() if end_date else None,
            "total_candidates": len(results),
        }
        run_id = _save_selector_run(
            session=session,
            run_type="realtime",
            params=params,
            summary=summary,
            items=results,
        )
        return {"run_id": run_id, "items": results}
    finally:
        DatabaseSessionManager.close_session(session)


@router.post("/export/ths-block")
def export_ths_block(request: dict):
    """
    导出同花顺自定义板块文件（.blk）。

    约定：
    - request.codes: 股票代码列表，可为 "600000" / "600000.SH" / "600000.SZ" 等
    - 生成内容：每行一个 6 位数字代码（同花顺 blocknew/*.blk 常用格式）
    - 编码：gbk（兼容同花顺老版本读取）
    """
    try:
        codes = request.get("codes") or []
        block_name = request.get("block_name") or "主升浪建仓"

        if not isinstance(codes, list):
            raise HTTPException(status_code=400, detail="codes 必须为数组")

        extracted: List[str] = []
        for item in codes:
            if item is None:
                continue
            s = str(item).strip()
            if not s:
                continue
            m = re.search(r"(\d{6})", s)
            if not m:
                continue
            extracted.append(m.group(1))

        # 去重 + 保持输出稳定
        extracted = sorted(set(extracted))
        if not extracted:
            raise HTTPException(status_code=400, detail="无可导出的股票代码")

        body_text = "\n".join(extracted) + "\n"
        try:
            body_bytes = body_text.encode("gbk", errors="ignore")
        except Exception:
            body_bytes = body_text.encode("utf-8")

        # 文件名尽量避免特殊字符
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", str(block_name))
        today = datetime.now().strftime("%Y%m%d")
        utf8_filename = f"{safe_name}_{today}.blk"
        ascii_filename = f"ths_block_{today}.blk"

        # Header 必须是 latin-1 可编码；中文用 RFC5987 的 filename* 携带
        content_disposition = (
            f'attachment; filename="{ascii_filename}"; '
            f"filename*=UTF-8''{quote(utf8_filename)}"
        )

        return StreamingResponse(
            io.BytesIO(body_bytes),
            media_type="application/octet-stream",
            headers={"Content-Disposition": content_disposition},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"导出同花顺板块失败: {e}")
        raise HTTPException(status_code=500, detail=f"导出失败: {str(e)}")


@router.post("/select/main-rise-build-up/history-analysis")
def analyze_main_rise_build_up_history(
    request: dict
):
    """历史回放主升浪建仓信号，并分析后续是否形成主升浪。"""
    days = int(request.get("days", 30))
    float_cap_min_yi = float(request.get("float_cap_min_yi", 80))
    float_cap_max_yi = float(request.get("float_cap_max_yi", 300))
    min_amplitude = float(request.get("min_amplitude", 8))
    max_amplitude = float(request.get("max_amplitude", 20))
    max_down_day_drop_pct = float(request.get("max_down_day_drop_pct", 6))
    avg_volume_ratio_threshold = float(request.get("avg_volume_ratio", 1.3))
    max_avg_down_pct = float(request.get("max_avg_down_pct", 3))
    future_days = int(request.get("future_days", 40))
    main_rise_threshold = float(request.get("main_rise_threshold", 20))
    signal_start_date = request.get("signal_start_date")
    signal_end_date = request.get("signal_end_date")
    max_signal_items = int(request.get("max_signal_items", 3000))

    if days <= 0 or future_days <= 0:
        raise HTTPException(status_code=400, detail="days 和 future_days 必须大于 0")
    if max_signal_items <= 0:
        raise HTTPException(status_code=400, detail="max_signal_items 必须大于 0")

    start_date_obj = None
    end_date_obj = None
    if signal_start_date:
        start_date_obj = datetime.strptime(signal_start_date, "%Y-%m-%d").date()
    if signal_end_date:
        end_date_obj = datetime.strptime(signal_end_date, "%Y-%m-%d").date()

    session = DatabaseSessionManager.get_session()
    try:
        latest_date = _latest_daily_trade_date(session)
        if not latest_date:
            summary = {"total_signals": 0, "formed_signals": 0, "hit_rate": 0, "stock_count": 0}
            params = {
                "days": days,
                "float_cap_min_yi": float_cap_min_yi,
                "float_cap_max_yi": float_cap_max_yi,
                "min_amplitude": min_amplitude,
                "max_amplitude": max_amplitude,
                "max_down_day_drop_pct": max_down_day_drop_pct,
                "avg_volume_ratio": avg_volume_ratio_threshold,
                "max_avg_down_pct": max_avg_down_pct,
                "future_days": future_days,
                "main_rise_threshold": main_rise_threshold,
                "signal_start_date": start_date_obj.isoformat() if start_date_obj else None,
                "signal_end_date": end_date_obj.isoformat() if end_date_obj else None,
                "max_signal_items": max_signal_items,
            }
            run_id = _save_selector_run(
                session=session,
                run_type="history",
                params=params,
                summary=summary,
                items=[],
                signal_events=[],
            )
            return {"run_id": run_id, "summary": summary, "items": [], "signal_events": []}

        if not end_date_obj or end_date_obj > latest_date:
            end_date_obj = latest_date

        min_list_date = end_date_obj - timedelta(days=60)

        candidate_stocks = session.query(Stock).filter(
            Stock.type == 'stock',
            Stock.quit == False,
            Stock.st == False,
            Stock.list_date.isnot(None),
            Stock.list_date <= min_list_date,
            ~Stock.name.like('ST%'),
            ~Stock.name.like('*ST%')
        ).all()

        items = []
        signal_events = []
        total_signals = 0
        formed_signals = 0

        for stock in candidate_stocks:
            klines = session.query(KlineDaily).filter(
                KlineDaily.code == stock.code,
                KlineDaily.trade_date <= end_date_obj
            ).order_by(KlineDaily.trade_date).all()

            if len(klines) < days + 1:
                continue

            stock_signal_count = 0
            stock_formed_count = 0
            stock_rise_sum = 0.0
            stock_best_rise = -999.0
            stock_worst_drawdown = 0.0
            first_signal_date = None
            last_signal_date = None

            for i in range(days - 1, len(klines) - 1):
                signal_kline = klines[i]
                signal_date = signal_kline.trade_date

                if start_date_obj and signal_date < start_date_obj:
                    continue
                if signal_date > end_date_obj:
                    continue
                if signal_kline.volume is None or signal_kline.volume <= 0:
                    continue

                # 条件：流通盘（流通市值）在 80-300 亿之间（以信号日收盘价计）
                float_share_wan = float(stock.float_share or 0)  # 万股
                signal_close_for_cap = float(signal_kline.close or 0)
                float_cap_yi = (float_share_wan * 10000 * signal_close_for_cap) / 1e8 if float_share_wan > 0 and signal_close_for_cap > 0 else 0.0
                if float_cap_yi < float_cap_min_yi or float_cap_yi > float_cap_max_yi:
                    continue

                window = klines[i - days + 1:i + 1]
                metrics = _evaluate_main_rise_build_up_window(
                    klines=window,
                    avg_volume_ratio_threshold=avg_volume_ratio_threshold,
                    min_amplitude=min_amplitude,
                    max_amplitude=max_amplitude,
                    max_down_day_drop_pct=max_down_day_drop_pct,
                    max_avg_down_pct=max_avg_down_pct,
                )
                if not metrics:
                    continue

                future_klines = klines[i + 1:i + 1 + future_days]
                if not future_klines:
                    continue

                signal_close = float(signal_kline.close or 0)
                if signal_close <= 0:
                    continue

                future_max = max(float(item.high or item.close or 0) for item in future_klines)
                future_min = min(float(item.low or item.close or 0) for item in future_klines)
                max_rise_pct = (future_max - signal_close) / signal_close * 100
                max_drawdown_pct = (future_min - signal_close) / signal_close * 100

                is_formed = max_rise_pct >= main_rise_threshold

                total_signals += 1
                stock_signal_count += 1
                stock_rise_sum += max_rise_pct
                stock_best_rise = max(stock_best_rise, max_rise_pct)
                stock_worst_drawdown = min(stock_worst_drawdown, max_drawdown_pct)

                if is_formed:
                    formed_signals += 1
                    stock_formed_count += 1

                if first_signal_date is None:
                    first_signal_date = signal_date
                last_signal_date = signal_date

                max_kline = max(future_klines, key=lambda x: float(x.high or x.close or 0))
                min_kline = min(future_klines, key=lambda x: float(x.low or x.close or 0))
                hit_date = None
                for fk in future_klines:
                    fk_high = float(fk.high or fk.close or 0)
                    if fk_high >= signal_close * (1 + main_rise_threshold / 100):
                        hit_date = fk.trade_date
                        break

                signal_events.append({
                    "code": stock.code,
                    "name": stock.name,
                    "signal_date": signal_date.isoformat(),
                    "window_start_date": window[0].trade_date.isoformat(),
                    "window_end_date": window[-1].trade_date.isoformat(),
                    "signal_close": round(signal_close, 4),
                    "future_end_date": future_klines[-1].trade_date.isoformat(),
                    "up_days": metrics["up_days"],
                    "down_days": metrics["down_days"],
                    "volume_ratio": metrics["volume_ratio"],
                    "amplitude": metrics["amplitude"],
                    "max_rise_pct": round(max_rise_pct, 2),
                    "max_drawdown_pct": round(max_drawdown_pct, 2),
                    "max_rise_date": max_kline.trade_date.isoformat() if max_kline.trade_date else None,
                    "max_drawdown_date": min_kline.trade_date.isoformat() if min_kline.trade_date else None,
                    "hit_date": hit_date.isoformat() if hit_date else None,
                    "is_formed": is_formed,
                })

            if stock_signal_count > 0:
                items.append({
                    "code": stock.code,
                    "name": stock.name,
                    "signal_count": stock_signal_count,
                    "formed_count": stock_formed_count,
                    "hit_rate": round(stock_formed_count / stock_signal_count * 100, 2),
                    "avg_max_rise": round(stock_rise_sum / stock_signal_count, 2),
                    "best_max_rise": round(stock_best_rise, 2),
                    "worst_drawdown": round(stock_worst_drawdown, 2),
                    "first_signal_date": first_signal_date.isoformat() if first_signal_date else None,
                    "last_signal_date": last_signal_date.isoformat() if last_signal_date else None,
                })

        items.sort(
            key=lambda x: (
                x["last_signal_date"] or "",
                x["hit_rate"],
                x["signal_count"],
                x["avg_max_rise"],
            ),
            reverse=True,
        )

        summary = {
            "total_signals": total_signals,
            "formed_signals": formed_signals,
            "hit_rate": round(formed_signals / total_signals * 100, 2) if total_signals > 0 else 0,
            "future_days": future_days,
            "main_rise_threshold": main_rise_threshold,
            "window_days": days,
            "stock_count": len(items),
            "signal_start_date": start_date_obj.isoformat() if start_date_obj else None,
            "signal_end_date": end_date_obj.isoformat() if end_date_obj else None,
        }
        signal_events.sort(key=lambda x: (x["signal_date"], x["max_rise_pct"]), reverse=True)
        signal_events_total = len(signal_events)
        signal_events_truncated = signal_events_total > max_signal_items
        if signal_events_truncated:
            signal_events = signal_events[:max_signal_items]

        params = {
            "days": days,
            "float_cap_min_yi": float_cap_min_yi,
            "float_cap_max_yi": float_cap_max_yi,
            "min_amplitude": min_amplitude,
            "max_amplitude": max_amplitude,
            "max_down_day_drop_pct": max_down_day_drop_pct,
            "avg_volume_ratio": avg_volume_ratio_threshold,
            "max_avg_down_pct": max_avg_down_pct,
            "future_days": future_days,
            "main_rise_threshold": main_rise_threshold,
            "signal_start_date": start_date_obj.isoformat() if start_date_obj else None,
            "signal_end_date": end_date_obj.isoformat() if end_date_obj else None,
            "max_signal_items": max_signal_items,
        }
        run_id = _save_selector_run(
            session=session,
            run_type="history",
            params=params,
            summary=summary,
            items=items,
            signal_events=signal_events,
        )
        return {
            "run_id": run_id,
            "summary": summary,
            "items": items,
            "signal_events_total": signal_events_total,
            "signal_events_truncated": signal_events_truncated,
            "signal_events": signal_events,
            "max_signal_items": max_signal_items,
        }
    finally:
        DatabaseSessionManager.close_session(session)


@router.post("/select/main-rise-build-up/history-analysis/signals")
def analyze_main_rise_build_up_history_signals(
    request: dict
):
    """按单只股票返回主升浪建仓历史信号明细。"""
    code = request.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="code 不能为空")

    days = int(request.get("days", 30))
    float_cap_min_yi = float(request.get("float_cap_min_yi", 80))
    float_cap_max_yi = float(request.get("float_cap_max_yi", 300))
    min_amplitude = float(request.get("min_amplitude", 8))
    max_amplitude = float(request.get("max_amplitude", 20))
    max_down_day_drop_pct = float(request.get("max_down_day_drop_pct", 6))
    avg_volume_ratio_threshold = float(request.get("avg_volume_ratio", 1.3))
    max_avg_down_pct = float(request.get("max_avg_down_pct", 3))
    future_days = int(request.get("future_days", 40))
    main_rise_threshold = float(request.get("main_rise_threshold", 20))
    signal_start_date = request.get("signal_start_date")
    signal_end_date = request.get("signal_end_date")

    if days <= 0 or future_days <= 0:
        raise HTTPException(status_code=400, detail="days 和 future_days 必须大于 0")

    start_date_obj = datetime.strptime(signal_start_date, "%Y-%m-%d").date() if signal_start_date else None
    end_date_obj = datetime.strptime(signal_end_date, "%Y-%m-%d").date() if signal_end_date else None

    session = DatabaseSessionManager.get_session()
    try:
        stock = _get_stock_by_code(session, code)
        if not stock:
            raise HTTPException(status_code=404, detail=f"股票 {code} 不存在")

        latest_date = _latest_daily_trade_date(session)
        if not latest_date:
            return {"code": code, "name": stock.name, "signal_details": [], "total_signals": 0}

        if not end_date_obj or end_date_obj > latest_date:
            end_date_obj = latest_date

        klines = session.query(KlineDaily).filter(
            KlineDaily.code == code,
            KlineDaily.trade_date <= end_date_obj
        ).order_by(KlineDaily.trade_date).all()

        if len(klines) < days + 1:
            return {"code": code, "name": stock.name, "signal_details": [], "total_signals": 0}

        signal_details = []

        for i in range(days - 1, len(klines) - 1):
            signal_kline = klines[i]
            signal_date = signal_kline.trade_date

            if start_date_obj and signal_date < start_date_obj:
                continue
            if signal_date > end_date_obj:
                continue
            if signal_kline.volume is None or signal_kline.volume <= 0:
                continue

            # 条件：流通盘（流通市值）在 80-300 亿之间（以信号日收盘价计）
            float_share_wan = float(stock.float_share or 0)  # 万股
            signal_close_for_cap = float(signal_kline.close or 0)
            float_cap_yi = (float_share_wan * 10000 * signal_close_for_cap) / 1e8 if float_share_wan > 0 and signal_close_for_cap > 0 else 0.0
            if float_cap_yi < float_cap_min_yi or float_cap_yi > float_cap_max_yi:
                continue

            window = klines[i - days + 1:i + 1]
            metrics = _evaluate_main_rise_build_up_window(
                klines=window,
                avg_volume_ratio_threshold=avg_volume_ratio_threshold,
                min_amplitude=min_amplitude,
                max_amplitude=max_amplitude,
                max_down_day_drop_pct=max_down_day_drop_pct,
                max_avg_down_pct=max_avg_down_pct,
            )
            if not metrics:
                continue

            future_klines = klines[i + 1:i + 1 + future_days]
            if not future_klines:
                continue

            signal_close = float(signal_kline.close or 0)
            if signal_close <= 0:
                continue

            max_kline = max(future_klines, key=lambda x: float(x.high or x.close or 0))
            min_kline = min(future_klines, key=lambda x: float(x.low or x.close or 0))
            future_max = float(max_kline.high or max_kline.close or 0)
            future_min = float(min_kline.low or min_kline.close or 0)

            max_rise_pct = (future_max - signal_close) / signal_close * 100
            max_drawdown_pct = (future_min - signal_close) / signal_close * 100
            is_formed = max_rise_pct >= main_rise_threshold

            hit_date = None
            for fk in future_klines:
                fk_high = float(fk.high or fk.close or 0)
                if fk_high >= signal_close * (1 + main_rise_threshold / 100):
                    hit_date = fk.trade_date
                    break

            signal_details.append({
                "signal_date": signal_date.isoformat(),
                "window_start_date": window[0].trade_date.isoformat(),
                "window_end_date": window[-1].trade_date.isoformat(),
                "signal_close": round(signal_close, 4),
                "up_days": metrics["up_days"],
                "down_days": metrics["down_days"],
                "volume_ratio": metrics["volume_ratio"],
                "amplitude": metrics["amplitude"],
                "future_end_date": future_klines[-1].trade_date.isoformat(),
                "max_rise_pct": round(max_rise_pct, 2),
                "max_drawdown_pct": round(max_drawdown_pct, 2),
                "max_rise_date": max_kline.trade_date.isoformat() if max_kline.trade_date else None,
                "max_drawdown_date": min_kline.trade_date.isoformat() if min_kline.trade_date else None,
                "hit_date": hit_date.isoformat() if hit_date else None,
                "is_formed": is_formed,
            })

        signal_details.sort(key=lambda x: x["signal_date"], reverse=True)

        formed_count = sum(1 for item in signal_details if item["is_formed"])
        return {
            "code": code,
            "name": stock.name,
            "total_signals": len(signal_details),
            "formed_signals": formed_count,
            "hit_rate": round(formed_count / len(signal_details) * 100, 2) if signal_details else 0,
            "signal_details": signal_details,
        }
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/select/main-rise-build-up/runs")
def list_main_rise_build_up_runs(
    run_type: Optional[str] = Query(None, description="realtime/history"),
    limit: int = Query(20, ge=1, le=200),
):
    session = DatabaseSessionManager.get_session()
    try:
        query = session.query(SelectorRun).filter(SelectorRun.selector_name == "main_rise_build_up")
        if run_type in ("realtime", "history"):
            query = query.filter(SelectorRun.run_type == run_type)
        rows = query.order_by(SelectorRun.updated_at.desc(), SelectorRun.id.desc()).limit(limit).all()

        items = []
        for row in rows:
            try:
                summary = json.loads(row.summary_json) if row.summary_json else {}
            except Exception:
                summary = {}
            try:
                params = json.loads(row.params_json) if row.params_json else {}
            except Exception:
                params = {}
            items.append({
                "id": int(row.id),
                "run_type": row.run_type,
                "status": row.status,
                "total_items": int(row.total_items or 0),
                "total_signals": int(row.total_signals or 0),
                "summary": summary,
                "params": params,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            })
        return {"total": len(items), "items": items}
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/select/new-highs/runs")
def list_new_high_runs(
    limit: int = Query(20, ge=1, le=200),
):
    session = DatabaseSessionManager.get_session()
    try:
        rows = session.query(SelectorRun).filter(
            SelectorRun.selector_name == "new_high_scan"
        ).order_by(SelectorRun.updated_at.desc(), SelectorRun.id.desc()).limit(limit).all()

        items = []
        for row in rows:
            try:
                summary = json.loads(row.summary_json) if row.summary_json else {}
            except Exception:
                summary = {}
            try:
                params = json.loads(row.params_json) if row.params_json else {}
            except Exception:
                params = {}
            items.append({
                "id": int(row.id),
                "run_type": row.run_type,
                "status": row.status,
                "total_items": int(row.total_items or 0),
                "total_signals": int(row.total_signals or 0),
                "summary": summary,
                "params": params,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            })
        return {"total": len(items), "items": items}
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/select/new-highs/runs/{run_id}")
def get_new_high_run_detail(run_id: int):
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.id == run_id,
            SelectorRun.selector_name == "new_high_scan",
        ).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在")

        records = session.query(SelectorRunRecord).filter(
            SelectorRunRecord.run_id == run_id
        ).order_by(SelectorRunRecord.id.asc()).all()

        items: List[Dict[str, Any]] = []
        sector_summary: List[Dict[str, Any]] = []
        sector_type_summary: List[Dict[str, Any]] = []
        for record in records:
            try:
                payload = json.loads(record.payload_json) if record.payload_json else {}
            except Exception:
                payload = {}
            if record.record_type == "realtime_item":
                items.append(payload)
            elif record.record_type == "sector_summary_item":
                sector_summary.append(payload)
            elif record.record_type == "sector_type_summary_item":
                sector_type_summary.append(payload)

        try:
            params = json.loads(run.params_json) if run.params_json else {}
        except Exception:
            params = {}
        try:
            summary = json.loads(run.summary_json) if run.summary_json else {}
        except Exception:
            summary = {}

        return {
            "id": int(run.id),
            "run_type": run.run_type,
            "status": run.status,
            "params": params,
            "summary": summary,
            "items": items,
            "sector_summary": sector_summary,
            "sector_type_summary": sector_type_summary,
            "total_items": int(run.total_items or 0),
            "total_signals": int(run.total_signals or 0),
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        }
    finally:
        DatabaseSessionManager.close_session(session)


@router.delete("/select/new-highs/runs/{run_id}")
def delete_new_high_run(run_id: int):
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.id == run_id,
            SelectorRun.selector_name == "new_high_scan",
        ).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在")

        session.query(SelectorRunRecord).filter(SelectorRunRecord.run_id == run_id).delete()
        session.delete(run)
        session.commit()
        return {"success": True, "run_id": run_id}
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/select/main-rise-build-up/runs/{run_id}")
def get_main_rise_build_up_run_detail(run_id: int):
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.id == run_id,
            SelectorRun.selector_name == "main_rise_build_up",
        ).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存?")

        records = session.query(SelectorRunRecord).filter(
            SelectorRunRecord.run_id == run_id
        ).order_by(SelectorRunRecord.id.asc()).all()

        realtime_items: List[Dict[str, Any]] = []
        history_items: List[Dict[str, Any]] = []
        signal_events: List[Dict[str, Any]] = []
        for record in records:
            try:
                payload = json.loads(record.payload_json) if record.payload_json else {}
            except Exception:
                payload = {}
            if record.record_type == "realtime_item":
                realtime_items.append(payload)
            elif record.record_type == "history_item":
                history_items.append(payload)
            elif record.record_type == "signal_event":
                signal_events.append(payload)

        try:
            params = json.loads(run.params_json) if run.params_json else {}
        except Exception:
            params = {}
        try:
            summary = json.loads(run.summary_json) if run.summary_json else {}
        except Exception:
            summary = {}

        return {
            "id": int(run.id),
            "run_type": run.run_type,
            "status": run.status,
            "params": params,
            "summary": summary,
            "items": realtime_items if run.run_type == "realtime" else history_items,
            "signal_events": signal_events,
            "total_items": int(run.total_items or 0),
            "total_signals": int(run.total_signals or 0),
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        }
    finally:
        DatabaseSessionManager.close_session(session)


@router.delete("/select/main-rise-build-up/runs/{run_id}")
def delete_main_rise_build_up_run(run_id: int):
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.id == run_id,
            SelectorRun.selector_name == "main_rise_build_up",
        ).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存?")

        session.query(SelectorRunRecord).filter(SelectorRunRecord.run_id == run_id).delete()
        session.delete(run)
        session.commit()
        return {"success": True, "run_id": run_id}
    finally:
        DatabaseSessionManager.close_session(session)


@router.post("/select/oscillate-accumulate")
def select_stocks_by_oscillate_accumulate(
    request: dict
):
    """震荡吃货选股"""
    days = request.get("days", 30)
    max_amplitude = request.get("max_amplitude", 25)
    volume_ratio = request.get("volume_ratio", 1.2)  # 上涨成交量与下跌成交量的最小比率
    
    session = DatabaseSessionManager.get_session()
    try:
        # 计算起始日期
        end_date = _latest_daily_trade_date(session)
        if not end_date:
            return []
        
        start_date = end_date - timedelta(days=days-1)
        
        # 首先获取所有股票代码
        stock_codes = session.query(Stock.code).all()
        stock_codes = [code[0] for code in stock_codes]
        
        results = []
        
        # 对每个股票进行单独分析
        for code in stock_codes:
            # 获取该股票在指定日期范围内的K线数据
            klines = session.query(KlineDaily).filter(
                KlineDaily.code == code,
                KlineDaily.trade_date >= start_date,
                KlineDaily.trade_date <= end_date
            ).order_by(KlineDaily.trade_date).all()
            
            # 确保有足够的交易日数据
            if len(klines) < days * 0.8:  # 允许最多20%的数据缺失
                continue
            
            # 计算最高价、最低价、起始价、结束价
            max_price = max(kline.high for kline in klines)
            min_price = min(kline.low for kline in klines)
            start_price = klines[0].open
            end_price = klines[-1].close
            
            # 计算振幅
            amplitude = (max_price - min_price) / min_price * 100
            if amplitude > max_amplitude:
                continue
            
            # 计算上涨日总成交量和下跌日总成交量
            up_volume = sum(kline.volume for kline in klines if kline.change_pct > 0)
            down_volume = sum(kline.volume for kline in klines if kline.change_pct < 0)
            
            # 确保上涨成交量大于下跌成交量
            if up_volume <= down_volume * volume_ratio:
                continue
            
            # 获取股票名称
            stock = _get_stock_by_code(session, code)
            if not stock:
                continue
            
            # 计算成交量比率
            volume_ratio_calc = up_volume / down_volume if down_volume > 0 else 0
            
            # 添加到结果列表
            results.append({
                "code": code,
                "name": stock.name,
                "max_price": max_price,
                "min_price": min_price,
                "start_price": start_price,
                "end_price": end_price,
                "up_volume": up_volume,
                "down_volume": down_volume,
                "amplitude": round(amplitude, 2),
                "volume_ratio": round(volume_ratio_calc, 2)
            })
        
        # 构建响应
        return results
    finally:
        DatabaseSessionManager.close_session(session)


@router.post("/select/trend-rebound-15d")
def select_stocks_by_trend_rebound_15d(request: dict):
    """
    策略：
    1) 近 lookback_days 交易日上涨天数 > min_up_days
    2) 回调跌破 ma_period 日线后，再次收盘站上 ma_period 日线触发买入
    3) min15 最新价跌破趋势线（近 min15_trend_bars 均线）触发卖出提示
    """
    lookback_days = int(request.get("lookback_days", 15))
    min_up_days = int(request.get("min_up_days", 10))
    ma_period = int(request.get("ma_period", 5))
    ma_hard_floor_period = 60
    rebound_window = int(request.get("rebound_window", 8))
    min15_recent_bars = int(request.get("min15_recent_bars", 120))
    min15_trend_bars = int(request.get("min15_trend_bars", 20))

    if lookback_days <= 1:
        raise HTTPException(status_code=400, detail="lookback_days 必须大于 1")
    if min_up_days < 0 or min_up_days >= lookback_days:
        raise HTTPException(status_code=400, detail="min_up_days 必须在 [0, lookback_days) 区间")
    if ma_period <= 1:
        raise HTTPException(status_code=400, detail="ma_period 必须大于 1")
    if rebound_window <= 0:
        raise HTTPException(status_code=400, detail="rebound_window 必须大于 0")
    if min15_recent_bars <= min15_trend_bars:
        raise HTTPException(status_code=400, detail="min15_recent_bars 必须大于 min15_trend_bars")

    session = DatabaseSessionManager.get_session()
    try:
        end_date = _latest_daily_trade_date(session)
        if not end_date:
            params_payload = {
                "lookback_days": lookback_days,
                "min_up_days": min_up_days,
                "ma_period": ma_period,
                "rebound_window": rebound_window,
                "min15_recent_bars": min15_recent_bars,
                "min15_trend_bars": min15_trend_bars,
            }
            summary_payload = {"trade_date": None, "total": 0}
            run_id = _save_or_update_selector_run(
                session=session,
                selector_name="trend_rebound_15d",
                run_type="realtime",
                params=params_payload,
                summary=summary_payload,
                items=[],
                update_latest_same_params=True,
            )
            return {"run_id": run_id, "trade_date": None, "total": 0, "items": []}

        min_list_date = end_date - timedelta(days=120)
        stocks = session.query(Stock).filter(
            Stock.type == 'stock',
            Stock.quit == False,
            Stock.st == False,
            Stock.list_date.isnot(None),
            Stock.list_date <= min_list_date,
            ~Stock.name.like('ST%'),
            ~Stock.name.like('*ST%')
        ).all()

        need_daily_bars = max(lookback_days + ma_period + rebound_window + 5, ma_hard_floor_period + 5)
        results: List[Dict[str, Any]] = []

        for stock in stocks:
            daily_bars = session.query(KlineDaily).filter(
                KlineDaily.code == stock.code,
                KlineDaily.trade_date <= end_date
            ).order_by(KlineDaily.trade_date.desc()).limit(need_daily_bars).all()

            if len(daily_bars) < max(lookback_days + ma_period + 2, ma_hard_floor_period):
                continue

            daily_bars = list(reversed(daily_bars))
            closes = [float(k.close or 0) for k in daily_bars]
            dates = [k.trade_date for k in daily_bars]
            if any(c <= 0 for c in closes):
                continue

            # 近 N 天上涨天数（优先用 change_pct，兜底 close 对比）
            recent_bars = daily_bars[-lookback_days:]
            up_days = 0
            prev_close = float(daily_bars[-lookback_days - 1].close or 0)
            for bar in recent_bars:
                if bar.change_pct is not None:
                    if float(bar.change_pct or 0) > 0:
                        up_days += 1
                else:
                    close_val = float(bar.close or 0)
                    if prev_close > 0 and close_val > prev_close:
                        up_days += 1
                    prev_close = close_val

            if up_days <= min_up_days:
                continue

            # 计算 MA
            ma_vals: List[Optional[float]] = [None] * len(closes)
            rolling_sum = 0.0
            for i, close_val in enumerate(closes):
                rolling_sum += close_val
                if i >= ma_period:
                    rolling_sum -= closes[i - ma_period]
                if i >= ma_period - 1:
                    ma_vals[i] = rolling_sum / ma_period

            ma60_vals: List[Optional[float]] = [None] * len(closes)
            rolling_sum_60 = 0.0
            for i, close_val in enumerate(closes):
                rolling_sum_60 += close_val
                if i >= ma_hard_floor_period:
                    rolling_sum_60 -= closes[i - ma_hard_floor_period]
                if i >= ma_hard_floor_period - 1:
                    ma60_vals[i] = rolling_sum_60 / ma_hard_floor_period

            latest_idx = len(closes) - 1
            prev_idx = latest_idx - 1
            if prev_idx < 0 or ma_vals[latest_idx] is None or ma_vals[prev_idx] is None:
                continue

            latest_close = closes[latest_idx]
            latest_ma = float(ma_vals[latest_idx])
            latest_ma60 = ma60_vals[latest_idx]
            if latest_ma60 is None or latest_close <= float(latest_ma60):
                continue
            prev_close = closes[prev_idx]
            prev_ma = float(ma_vals[prev_idx])

            # 再次站上 MA（收盘）
            cross_up = prev_close <= prev_ma and latest_close > latest_ma
            if not cross_up:
                continue

            # 最近 rebound_window 天内出现过“跌破 MA”
            left = max(ma_period - 1, latest_idx - rebound_window)
            breach_idx = -1
            for i in range(left, latest_idx):
                if ma_vals[i] is not None and closes[i] < float(ma_vals[i]):
                    breach_idx = i
            if breach_idx < 0:
                continue

            # min15 趋势线（均线）卖出提示
            m15_bars = session.query(KlineMinute15).filter(
                KlineMinute15.code == stock.code
            ).order_by(KlineMinute15.datetime.desc()).limit(min15_recent_bars).all()

            sell_signal = False
            m15_close = None
            m15_trend = None
            m15_dt = None
            if len(m15_bars) >= min15_trend_bars:
                m15_bars = list(reversed(m15_bars))
                m15_closes = [float(x.close or 0) for x in m15_bars if x.close is not None]
                if len(m15_closes) >= min15_trend_bars:
                    m15_close = m15_closes[-1]
                    m15_trend = sum(m15_closes[-min15_trend_bars:]) / min15_trend_bars
                    sell_signal = bool(m15_close < m15_trend)
                    m15_dt = m15_bars[-1].datetime.isoformat() if m15_bars[-1].datetime else None

            results.append({
                "code": stock.code,
                "name": stock.name,
                "trade_date": dates[latest_idx].isoformat() if dates[latest_idx] else None,
                "lookback_days": lookback_days,
                "up_days": up_days,
                "ma_period": ma_period,
                "close": round(latest_close, 3),
                "ma_value": round(latest_ma, 3),
                "ma60_value": round(float(latest_ma60), 3),
                "pullback_date": dates[breach_idx].isoformat() if dates[breach_idx] else None,
                "buy_signal": True,
                "sell_signal": sell_signal,
                "min15_datetime": m15_dt,
                "min15_close": round(m15_close, 3) if m15_close is not None else None,
                "min15_trend": round(m15_trend, 3) if m15_trend is not None else None,
            })

        results.sort(key=lambda x: (x["up_days"], x["close"]), reverse=True)
        params_payload = {
            "lookback_days": lookback_days,
            "min_up_days": min_up_days,
            "ma_period": ma_period,
            "rebound_window": rebound_window,
            "min15_recent_bars": min15_recent_bars,
            "min15_trend_bars": min15_trend_bars,
        }
        summary_payload = {"trade_date": end_date.isoformat(), "total": len(results)}
        run_id = _save_or_update_selector_run(
            session=session,
            selector_name="trend_rebound_15d",
            run_type="realtime",
            params=params_payload,
            summary=summary_payload,
            items=results,
            update_latest_same_params=True,
        )
        return {
            "run_id": run_id,
            "trade_date": end_date.isoformat(),
            "total": len(results),
            "items": results,
        }
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/select/trend-rebound-15d/latest")
def get_latest_trend_rebound_15d_run():
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.selector_name == "trend_rebound_15d",
            SelectorRun.run_type == "realtime",
        ).order_by(SelectorRun.updated_at.desc(), SelectorRun.id.desc()).first()
        if not run:
            return {"found": False, "run": None}

        items = _load_selector_run_items(session, run_id=int(run.id), record_type="realtime_item")
        detail = _serialize_selector_run_detail(run, items)
        return {"found": True, "run": detail}
    finally:
        DatabaseSessionManager.close_session(session)


@router.post("/select/uptrend")
def select_stocks_by_uptrend(request: dict):
    """
    Identify stocks that are already in uptrend after a box breakout,
    or are still in range but sitting close to the box top.
    """
    config = _create_uptrend_config(request)
    _validate_uptrend_config(config)

    session = DatabaseSessionManager.get_session()
    try:
        trade_date = _latest_daily_trade_date(session)
        if not trade_date:
            summary_payload = {"trade_date": None, "total": 0, "uptrend": 0, "ready": 0}
            run_id = _save_or_update_selector_run(
                session=session,
                selector_name="uptrend_scan",
                run_type="realtime",
                params=config,
                summary=summary_payload,
                items=[],
                update_latest_same_params=True,
            )
            return {"run_id": run_id, "trade_date": None, "summary": summary_payload, "items": [], "config": config}

        min_list_date = trade_date - timedelta(days=config["min_list_days"])
        query = session.query(Stock).filter(
            Stock.type == "stock",
            Stock.list_date.isnot(None),
            Stock.list_date <= min_list_date,
            ~Stock.name.like("ST%"),
            ~Stock.name.like("*ST%"),
        )
        if not config["include_quit"]:
            query = query.filter(Stock.quit == False)
        if not config["include_st"]:
            query = query.filter(Stock.st == False)
        stocks = query.all()

        bars_needed = max(
            config["box_lookback_days"] + 2,
            config["ma_long"] + config["ma_slope_days"] + 2,
            config["volume_window"] + 2,
            90,
        )

        items: List[Dict[str, Any]] = []
        bars_by_code = _load_recent_daily_bars_for_codes_clickhouse(
            [stock.code for stock in stocks],
            trade_date,
            bars_needed,
        )
        for stock in stocks:
            daily_bars = bars_by_code.get(stock.code) if bars_by_code is not None else None
            if daily_bars is None:
                daily_bars = session.query(KlineDaily).filter(
                    KlineDaily.code == stock.code,
                    KlineDaily.trade_date <= trade_date,
                ).order_by(KlineDaily.trade_date.desc()).limit(bars_needed).all()
            if len(daily_bars) < bars_needed * 0.8:
                continue

            candidate = _analyze_uptrend_candidate(stock, list(reversed(daily_bars)), config)
            if candidate:
                items.append(candidate)

        items.sort(
            key=lambda item: (
                1 if item["signal_type"] == "uptrend" else 0,
                item.get("score", 0),
                item.get("breakout_pct", 0),
                item.get("distance_to_box_top_pct", -999),
            ),
            reverse=True,
        )
        if len(items) > config["max_results"]:
            items = items[:config["max_results"]]

        uptrend_count = sum(1 for item in items if item["signal_type"] == "uptrend")
        ready_count = sum(1 for item in items if item["signal_type"] == "ready")

        summary_payload = {
            "trade_date": trade_date.isoformat(),
            "total": len(items),
            "uptrend": uptrend_count,
            "ready": ready_count,
        }
        run_id = _save_or_update_selector_run(
            session=session,
            selector_name="uptrend_scan",
            run_type="realtime",
            params=config,
            summary=summary_payload,
            items=items,
            update_latest_same_params=True,
        )

        return {
            "run_id": run_id,
            "trade_date": trade_date.isoformat(),
            "summary": summary_payload,
            "config": config,
            "items": items,
            "quant_notes": [
                "箱体突破: 长期箱体内震荡后，收盘有效突破箱体上沿，同时均线多头并放量。",
                "即将突破: 仍在箱体内或贴近上沿，均线开始走平/拐头，价格位于箱体上半区。",
                f"额外过滤: 默认剔除 ST、退市，并剔除总市值低于 {config['min_total_market_cap_yi']:.0f} 亿的公司。",
                "这是一版偏稳健的初始量化规则，后续可以继续拿历史回测去优化各阈值。",
            ],
        }
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/select/uptrend/runs")
def list_uptrend_runs(limit: int = Query(20, ge=1, le=200)):
    session = DatabaseSessionManager.get_session()
    try:
        rows = session.query(SelectorRun).filter(
            SelectorRun.selector_name == "uptrend_scan"
        ).order_by(SelectorRun.updated_at.desc(), SelectorRun.id.desc()).limit(limit).all()
        items = [_serialize_selector_run_list_item(row) for row in rows]
        return {"total": len(items), "items": items}
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/select/uptrend/runs/{run_id}")
def get_uptrend_run_detail(run_id: int):
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.id == run_id,
            SelectorRun.selector_name == "uptrend_scan",
        ).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在")

        items = _load_selector_run_items(session, run_id=run_id, record_type="realtime_item")
        return _serialize_selector_run_detail(run, items)
    finally:
        DatabaseSessionManager.close_session(session)


@router.delete("/select/uptrend/runs/{run_id}")
def delete_uptrend_run(run_id: int):
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.id == run_id,
            SelectorRun.selector_name == "uptrend_scan",
        ).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在")

        session.query(SelectorRunRecord).filter(SelectorRunRecord.run_id == run_id).delete()
        session.delete(run)
        session.commit()
        return {"success": True, "run_id": run_id}
    finally:
        DatabaseSessionManager.close_session(session)


@router.post("/select/uptrend/backtest")
def run_uptrend_backtest(request: dict, background_tasks: BackgroundTasks):
    config = _create_uptrend_config(request)
    config["forward_horizon_days"] = int(request.get("forward_horizon_days", 40))
    config["signal_cooldown_days"] = int(request.get("signal_cooldown_days", 15))
    config["lookback_years"] = int(request.get("lookback_years", 5))
    _validate_uptrend_config(config)

    session = DatabaseSessionManager.get_session()
    try:
        latest_trade_date = _latest_daily_trade_date(session)
        if not latest_trade_date:
            raise HTTPException(status_code=400, detail="暂无可用日线数据")

        end_date_value = request.get("end_date")
        start_date_value = request.get("start_date")
        end_date = datetime.strptime(end_date_value, "%Y-%m-%d").date() if end_date_value else latest_trade_date
        if start_date_value:
            start_date = datetime.strptime(start_date_value, "%Y-%m-%d").date()
        else:
            start_date = end_date - timedelta(days=config["lookback_years"] * 365)

        if start_date >= end_date:
            raise HTTPException(status_code=400, detail="start_date must be earlier than end_date")

        config["start_date"] = start_date.isoformat()
        config["end_date"] = end_date.isoformat()

        running_summary = {
            "period": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "processed_stocks": 0,
                "signal_cooldown_days": int(config.get("signal_cooldown_days", 0) or 0),
            },
            "overall": {"count": 0},
            "conclusions": ["回测任务已提交，正在后台运行中。"],
        }
        run_id = _save_selector_run(
            session=session,
            selector_name="uptrend_backtest",
            run_type="history",
            params=config,
            summary=running_summary,
            items=[],
            signal_events=[],
            status="running",
        )
    finally:
        DatabaseSessionManager.close_session(session)

    task_id = f"uptrend_backtest_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{uuid4().hex[:8]}"
    _set_uptrend_backtest_task(task_id, {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "error": None,
        "result": None,
        "partial_items": [],
        "partial_summary": {"overall_count": 0, "uptrend_count": 0},
        "run_id": run_id,
        "processed_stocks": 0,
        "total_stocks": 0,
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "params": config,
    })
    background_tasks.add_task(_run_uptrend_backtest_task, task_id, run_id, config, start_date, end_date)
    return {
        "success": True,
        "task_id": task_id,
        "run_id": run_id,
        "status": "running",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


@router.get("/select/uptrend/backtest/tasks/latest-running")
def get_latest_running_uptrend_backtest_task():
    with uptrend_backtest_lock:
        running_tasks = [
            dict(task)
            for task in uptrend_backtest_tasks.values()
            if task.get("status") == "running"
        ]
    if not running_tasks:
        return {"found": False, "task": None}
    running_tasks.sort(key=lambda item: item.get("updated_at") or item.get("started_at") or "", reverse=True)
    return {"found": True, "task": running_tasks[0]}


@router.get("/select/uptrend/backtest/tasks/{task_id}")
def get_uptrend_backtest_task(task_id: str):
    # Compatibility guard: if route order/caching causes latest-running to land here,
    # still return the expected payload instead of 404.
    if task_id == "latest-running":
        return get_latest_running_uptrend_backtest_task()
    task = _get_uptrend_backtest_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/select/uptrend/backtest/runs")
def list_uptrend_backtest_runs(limit: int = Query(20, ge=1, le=200)):
    session = DatabaseSessionManager.get_session()
    try:
        rows = session.query(SelectorRun).filter(
            SelectorRun.selector_name == "uptrend_backtest"
        ).order_by(SelectorRun.updated_at.desc(), SelectorRun.id.desc()).limit(limit).all()
        items = [_serialize_selector_run_list_item(row) for row in rows]
        return {"total": len(items), "items": items}
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/select/uptrend/backtest/runs/{run_id}")
def get_uptrend_backtest_run_detail(run_id: int):
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.id == run_id,
            SelectorRun.selector_name == "uptrend_backtest",
        ).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在")
        items = _load_selector_run_items(session, run_id=run_id, record_type="history_item")
        detail = _serialize_selector_run_detail(run, items)
        detail["signal_events"] = _load_selector_run_items(session, run_id=run_id, record_type="signal_event")
        return detail
    finally:
        DatabaseSessionManager.close_session(session)


@router.delete("/select/uptrend/backtest/runs/{run_id}")
def delete_uptrend_backtest_run(run_id: int):
    session = DatabaseSessionManager.get_session()
    try:
        run = session.query(SelectorRun).filter(
            SelectorRun.id == run_id,
            SelectorRun.selector_name == "uptrend_backtest",
        ).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在")
        session.query(SelectorRunRecord).filter(SelectorRunRecord.run_id == run_id).delete()
        session.delete(run)
        session.commit()
        return {"success": True, "run_id": run_id}
    finally:
        DatabaseSessionManager.close_session(session)


@router.get("/holdings")
def get_holdings():
    """获取持仓股"""
    # 这里应该从数据库中获取持仓股数据
    # 暂时返回模拟数据
    return [
        {
            "code": "600000.SH",
            "name": "浦发银行",
            "close": 10.23,
            "change_pct": 2.34,
            "hold_count": 1000,
            "cost_price": 9.85,
            "profit": 380.0,
            "profit_rate": 3.86
        },
        {
            "code": "000001.SZ",
            "name": "平安银行",
            "close": 15.67,
            "change_pct": -1.23,
            "hold_count": 500,
            "cost_price": 16.25,
            "profit": -290.0,
            "profit_rate": -3.57
        }
    ]


router.routes = [
    route
    for route in router.routes
    if not (
        getattr(route, "path", "").startswith("/stocks/select/")
        or getattr(route, "path", "").startswith("/select/")
    )
]
router.add_api_route(
    "/select/{legacy_path:path}",
    _legacy_selector_removed,
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
