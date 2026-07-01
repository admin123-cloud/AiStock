"""
大盘概览API - 首页数据展示
"""

from collections import defaultdict
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from sqlalchemy import and_, case, desc, func, or_, text

from models.stock_models import EmotionCycle, KlineDaily, Stock
from utils.database import db
from utils.logger import get_logger
from utils.market_warehouse import (
    clickhouse_available,
    clickhouse_table_exists,
    clickhouse_available,
    clickhouse_query_df,
    clickhouse_scalar,
)

router = APIRouter(prefix="/market", tags=["大盘概览"])
logger = get_logger("market_overview")
CORE_INDEX_CODES = {"999999.SH", "399001.SZ"}
DEFAULT_INDEX_JUMP_THRESHOLD = 20.0
CORE_INDEX_JUMP_THRESHOLD = 15.0
MIN_EMOTION_CYCLE_STOCKS = 1000


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

    change_pct = _to_float(getattr(row, "change_pct", None), default=0.0)
    if getattr(row, "change_pct", None) is None:
        open_price = _to_float(getattr(row, "open", None), default=0.0)
        close_price = _to_float(getattr(row, "close", None), default=0.0)
        if open_price > 0:
            change_pct = (close_price - open_price) / open_price * 100

    return change_pct >= _limit_up_threshold(stock)


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


@router.get("/indices")
def get_market_indices(code: Optional[str] = None):
    """获取主要指数数据，并返回关键指数健康状态。"""
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
                        GROUP BY k.trade_date
                        ORDER BY k.trade_date DESC
                        LIMIT 20
                        """.format(kline_table=kline_table, stocks_table=stocks_table)
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

                        for stock in stocks:
                            latest_kline = kline_map.get((stock.code, latest_key))
                            if not latest_kline:
                                if stock.code in CORE_INDEX_CODES:
                                    blocked_codes.append({"code": stock.code, "reason": "missing_latest_kline"})
                                continue

                            previous_kline = kline_map.get((stock.code, previous_key))
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

                            date_value = latest_kline.get("trade_date")
                            indices.append(
                                {
                                    "code": stock.code,
                                    "name": stock.name,
                                    "price": latest_kline.get("close"),
                                    "change": change,
                                    "change_pct": change_pct,
                                    "volume": latest_kline.get("volume"),
                                    "amount": latest_kline.get("amount"),
                                    "date": date_value.strftime("%Y-%m-%d") if hasattr(date_value, "strftime") else str(date_value),
                                    "is_abnormal": is_abnormal,
                                    "validation_reason": validation_reason,
                                }
                            )

                        missing_core = CORE_INDEX_CODES - present_core_codes
                        for missing_code in sorted(missing_core):
                            blocked_codes.append({"code": missing_code, "reason": "missing_core_index"})

                        validation_status = "passed" if not blocked_codes else "failed"
                        validation_reason = "ok" if not blocked_codes else "core_index_validation_failed"
                        latest_date_text = _date_text(latest_date)

                        return {
                            "indices": indices,
                            "trading_date": latest_date_text,
                            "validation_status": validation_status,
                            "validation_reason": validation_reason,
                            "blocked_codes": blocked_codes,
                            "source": "clickhouse",
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
            func.count(case((or_(KlineDaily.change_pct > 0, and_(KlineDaily.change_pct.is_(None), KlineDaily.close > KlineDaily.open)), 1), else_=None)).label("up_count"),
            func.count(case((or_(KlineDaily.change_pct < 0, and_(KlineDaily.change_pct.is_(None), KlineDaily.close < KlineDaily.open)), 1), else_=None)).label("down_count"),
            func.count(case((or_(KlineDaily.change_pct == 0, and_(KlineDaily.change_pct.is_(None), KlineDaily.close == KlineDaily.open)), 1), else_=None)).label("unchanged_count"),
            func.count(case((or_(KlineDaily.change_pct >= 5, and_(KlineDaily.change_pct.is_(None), (KlineDaily.close - KlineDaily.open) / KlineDaily.open * 100 >= 5)), 1), else_=None)).label("up_5_count"),
            func.count(case((or_(KlineDaily.change_pct <= -5, and_(KlineDaily.change_pct.is_(None), (KlineDaily.close - KlineDaily.open) / KlineDaily.open * 100 <= -5)), 1), else_=None)).label("down_5_count"),
            func.count(case((or_(KlineDaily.change_pct >= 9.9, and_(KlineDaily.change_pct.is_(None), (KlineDaily.close - KlineDaily.open) / KlineDaily.open * 100 >= 9.9)), 1), else_=None)).label("limit_up_count"),
            func.count(case((or_(KlineDaily.change_pct <= -9.9, and_(KlineDaily.change_pct.is_(None), (KlineDaily.close - KlineDaily.open) / KlineDaily.open * 100 <= -9.9)), 1), else_=None)).label("limit_down_count"),
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


def _load_market_sentiment_from_clickhouse(session) -> Optional[Dict[str, Any]]:
    if not clickhouse_available():
        return None
    stocks_table = _resolve_table_name("stocks")
    kline_table = _resolve_table_name("kline_daily")
    if not _has_clickhouse_table(stocks_table) or not _has_clickhouse_table(kline_table):
        return None
    latest_date = clickhouse_scalar(
        """
        SELECT MAX(k.trade_date)
        FROM {kline_table} k
        JOIN {stocks_table} s ON s.code = k.code
        JOIN trade_calendar c
          ON c.trade_date = k.trade_date
         AND c.market = 'SH'
         AND c.is_trading = 1
        WHERE s.type = 'stock'
        """.format(kline_table=kline_table, stocks_table=stocks_table)
    )
    if not latest_date:
        return None

    latest_date_text = _date_text(latest_date)
    summary_df = clickhouse_query_df(
        """
        WITH base AS (
            SELECT
                s.market,
                k.amount,
                COALESCE(k.change_pct, (k.close - k.open) / NULLIF(k.open, 0) * 100) AS pct
            FROM {kline_table} k
            JOIN {stocks_table} s ON s.code = k.code
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
    total_amount = float(row.get("total_amount") or 0)
    total = up_count + down_count + unchanged_count
    sentiment_score = 50 + (up_count - down_count) / total * 50 if total > 0 else 50

    turnover_df = clickhouse_query_df(
        """
        SELECT s.market, SUM(k.amount) AS amount
        FROM {kline_table} k
        JOIN {stocks_table} s ON s.code = k.code
        WHERE s.type = 'stock'
          AND s.market IN ('SH', 'SZ')
          AND k.trade_date = ?
          AND k.open > 0
        GROUP BY s.market
        """.format(kline_table=kline_table, stocks_table=stocks_table),
        [latest_date_text],
    )
    turnover_by_market = {}
    if turnover_df is not None and not turnover_df.empty:
        turnover_by_market = {
            str(item.market): float(item.amount or 0)
            for item in turnover_df.itertuples(index=False)
        }

    date_df = clickhouse_query_df(
        """
        SELECT DISTINCT k.trade_date
        FROM {kline_table} k
        JOIN {stocks_table} s ON s.code = k.code
        WHERE s.type = 'stock' AND k.trade_date <= ?
        ORDER BY k.trade_date DESC
        LIMIT 30
        """.format(kline_table=kline_table, stocks_table=stocks_table),
        [latest_date_text],
    )
    all_dates = [] if date_df is None or date_df.empty else [_date_text(v) for v in date_df["trade_date"].tolist()]
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
                    COALESCE(k.change_pct, (k.close - k.open) / NULLIF(k.open, 0) * 100) AS pct
                FROM {kline_table} k
                JOIN {stocks_table} s ON s.code = k.code
                WHERE s.type = 'stock'
                  AND k.open > 0
                  AND k.trade_date IN ({placeholders})
            )
            SELECT
                trade_date,
                SUM(CASE WHEN pct > 0 THEN 1 ELSE 0 END) AS up_count,
                SUM(CASE WHEN pct < 0 THEN 1 ELSE 0 END) AS down_count,
                SUM(CASE WHEN pct >= 9.9 THEN 1 ELSE 0 END) AS limit_up_count,
                SUM(CASE WHEN pct <= -9.9 THEN 1 ELSE 0 END) AS limit_down_count,
                SUM(CASE WHEN pct >= 5 THEN 1 ELSE 0 END) AS up_5_count,
                SUM(CASE WHEN pct <= -5 THEN 1 ELSE 0 END) AS down_5_count
            FROM base
            GROUP BY trade_date
            """.format(kline_table=kline_table, stocks_table=stocks_table, placeholders=placeholders),
            curve_dates,
        )
        if curve_df is not None and not curve_df.empty:
            for item in curve_df.itertuples(index=False):
                key = _date_text(item.trade_date)
                curve_map[key] = {
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

    latest_cycle = (
        session.query(EmotionCycle)
        .filter(EmotionCycle.date == latest_date_text)
        .order_by(desc(EmotionCycle.date))
        .first()
    )
    previous_cycle = None
    if latest_cycle:
        previous_cycle = (
            session.query(EmotionCycle)
            .filter(EmotionCycle.date < latest_cycle.date)
            .order_by(desc(EmotionCycle.date))
            .first()
        )

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

    return {
        "date": latest_date_text,
        "up_count": up_count,
        "down_count": down_count,
        "unchanged_count": unchanged_count,
        "up_5_percent_count": up_5_count,
        "down_5_percent_count": down_5_count,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "avg_change_percent": 0,
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
    }


@router.get("/sentiment")
def get_market_sentiment():
    """获取市场涨跌统计数据（含同花顺风格涨跌分布）。"""
    session = next(db.get_session())
    try:
        clickhouse_payload = _load_market_sentiment_from_clickhouse(session)
        if clickhouse_payload:
            return clickhouse_payload

        latest_date = session.query(func.max(KlineDaily.trade_date)).filter(
            KlineDaily.code == "999999.SH"
        ).scalar()
        if not latest_date:
            latest_date = session.query(func.max(KlineDaily.trade_date)).scalar()
        if not latest_date:
            return {"error": "数据库中没有K线数据"}

        if latest_date:
            from scheduler.trading_calendar import TradingCalendar

            if not TradingCalendar.is_trading_day(latest_date):
                latest_date = TradingCalendar.get_previous_trading_day(latest_date).date()

        effective_change_pct = case(
            (KlineDaily.change_pct.isnot(None), KlineDaily.change_pct),
            else_=((KlineDaily.close - KlineDaily.open) / func.nullif(KlineDaily.open, 0) * 100),
        )

        sentiment_query = session.query(
            func.count(case((effective_change_pct > 0, 1), else_=None)).label("up_count"),
            func.count(case((effective_change_pct < 0, 1), else_=None)).label("down_count"),
            func.count(case((effective_change_pct == 0, 1), else_=None)).label("unchanged_count"),
            func.count(case((effective_change_pct >= 5, 1), else_=None)).label("up_5_count"),
            func.count(case((effective_change_pct <= -5, 1), else_=None)).label("down_5_count"),
            func.count(case((effective_change_pct >= 9.9, 1), else_=None)).label("limit_up_count"),
            func.count(case((effective_change_pct <= -9.9, 1), else_=None)).label("limit_down_count"),
            func.sum(KlineDaily.amount).label("total_amount"),
            func.count(case((effective_change_pct >= 7, 1), else_=None)).label("bucket_up_7"),
            func.count(case((and_(effective_change_pct >= 5, effective_change_pct < 7), 1), else_=None)).label("bucket_up_5_7"),
            func.count(case((and_(effective_change_pct >= 3, effective_change_pct < 5), 1), else_=None)).label("bucket_up_3_5"),
            func.count(case((and_(effective_change_pct > 0, effective_change_pct < 3), 1), else_=None)).label("bucket_up_0_3"),
            func.count(case((effective_change_pct == 0, 1), else_=None)).label("bucket_zero"),
            func.count(case((and_(effective_change_pct < 0, effective_change_pct > -3), 1), else_=None)).label("bucket_down_0_3"),
            func.count(case((and_(effective_change_pct <= -3, effective_change_pct > -5), 1), else_=None)).label("bucket_down_3_5"),
            func.count(case((and_(effective_change_pct <= -5, effective_change_pct > -7), 1), else_=None)).label("bucket_down_5_7"),
            func.count(case((effective_change_pct <= -7, 1), else_=None)).label("bucket_down_7"),
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

        turnover_by_market = dict(
            session.query(Stock.market, func.sum(KlineDaily.amount))
            .join(Stock, Stock.code == KlineDaily.code)
            .filter(
                KlineDaily.trade_date == latest_date,
                Stock.type == "stock",
                Stock.market.in_(["SH", "SZ"]),
                KlineDaily.open > 0,
            )
            .group_by(Stock.market)
            .all()
        )

        distribution = [
            {"label": ">=7", "count": int(sentiment_query.bucket_up_7 or 0), "side": "up"},
            {"label": "5~7", "count": int(sentiment_query.bucket_up_5_7 or 0), "side": "up"},
            {"label": "3~5", "count": int(sentiment_query.bucket_up_3_5 or 0), "side": "up"},
            {"label": "0~3", "count": int(sentiment_query.bucket_up_0_3 or 0), "side": "up"},
            {"label": "0", "count": int(sentiment_query.bucket_zero or 0), "side": "flat"},
            {"label": "-3~0", "count": int(sentiment_query.bucket_down_0_3 or 0), "side": "down"},
            {"label": "-5~-3", "count": int(sentiment_query.bucket_down_3_5 or 0), "side": "down"},
            {"label": "-7~-5", "count": int(sentiment_query.bucket_down_5_7 or 0), "side": "down"},
            {"label": "<=-7", "count": int(sentiment_query.bucket_down_7 or 0), "side": "down"},
        ]

        trend_trade_dates_desc = session.execute(
            text(
                """
                SELECT DISTINCT trade_date
                FROM trade_calendar
                WHERE is_trading = 1 AND market = 'SH' AND trade_date <= :latest_date
                ORDER BY trade_date DESC
                LIMIT 7
                """
            ),
            {"latest_date": latest_date},
        ).fetchall()
        trend_trade_dates = list(reversed([row[0] for row in trend_trade_dates_desc]))
        trend_rows = []
        if trend_trade_dates:
            trend_rows = (
                session.query(
                    KlineDaily.trade_date.label("trade_date"),
                    func.count(case((effective_change_pct >= 5, 1), else_=None)).label("up_5_count"),
                    func.count(case((effective_change_pct <= -5, 1), else_=None)).label("down_5_count"),
                )
                .filter(
                    KlineDaily.trade_date.in_(trend_trade_dates),
                    KlineDaily.code.in_(session.query(Stock.code).filter(Stock.type == "stock")),
                    KlineDaily.open > 0,
                )
                .group_by(KlineDaily.trade_date)
                .all()
            )

        trend_map = {
            row.trade_date: {
                "up_5_count": int(row.up_5_count or 0),
                "down_5_count": int(row.down_5_count or 0),
            }
            for row in trend_rows
        }
        trend_dates_str = [d.strftime("%Y-%m-%d") for d in trend_trade_dates]
        trend_up_counts = [trend_map.get(d, {}).get("up_5_count", 0) for d in trend_trade_dates]
        trend_down_counts = [trend_map.get(d, {}).get("down_5_count", 0) for d in trend_trade_dates]
        if trend_trade_dates and trend_trade_dates[-1] == latest_date:
            trend_up_counts[-1] = up_5_count
            trend_down_counts[-1] = down_5_count

        curve_trade_dates_desc = session.execute(
            text(
                """
                SELECT DISTINCT trade_date
                FROM trade_calendar
                WHERE is_trading = 1 AND market = 'SH' AND trade_date <= :latest_date
                ORDER BY trade_date DESC
                LIMIT 30
                """
            ),
            {"latest_date": latest_date},
        ).fetchall()
        curve_trade_dates = list(reversed([row[0] for row in curve_trade_dates_desc]))
        curve_dates_str = [d.strftime("%Y-%m-%d") for d in curve_trade_dates]

        curve_rows = []
        if curve_trade_dates:
            curve_rows = (
                session.query(
                    KlineDaily.trade_date.label("trade_date"),
                    func.count(case((effective_change_pct > 0, 1), else_=None)).label("up_count"),
                    func.count(case((effective_change_pct < 0, 1), else_=None)).label("down_count"),
                    func.count(case((effective_change_pct >= 9.9, 1), else_=None)).label("limit_up_count"),
                    func.count(case((effective_change_pct <= -9.9, 1), else_=None)).label("limit_down_count"),
                    func.count(case((effective_change_pct >= 5, 1), else_=None)).label("up_5_count"),
                    func.count(case((effective_change_pct <= -5, 1), else_=None)).label("down_5_count"),
                )
                .filter(
                    KlineDaily.trade_date.in_(curve_trade_dates),
                    KlineDaily.code.in_(session.query(Stock.code).filter(Stock.type == "stock")),
                    KlineDaily.open > 0,
                )
                .group_by(KlineDaily.trade_date)
                .all()
            )

        curve_map = {
            row.trade_date: {
                "up_count": int(row.up_count or 0),
                "down_count": int(row.down_count or 0),
                "limit_up_count": int(row.limit_up_count or 0),
                "limit_down_count": int(row.limit_down_count or 0),
                "up_5_count": int(row.up_5_count or 0),
                "down_5_count": int(row.down_5_count or 0),
            }
            for row in curve_rows
        }

        raw_series = {
            "up_count_series": [curve_map.get(d, {}).get("up_count", 0) for d in curve_trade_dates],
            "down_count_series": [curve_map.get(d, {}).get("down_count", 0) for d in curve_trade_dates],
            "limit_up_count_series": [curve_map.get(d, {}).get("limit_up_count", 0) for d in curve_trade_dates],
            "limit_down_count_series": [curve_map.get(d, {}).get("limit_down_count", 0) for d in curve_trade_dates],
            "up_5_count_series": [curve_map.get(d, {}).get("up_5_count", 0) for d in curve_trade_dates],
            "down_5_count_series": [curve_map.get(d, {}).get("down_5_count", 0) for d in curve_trade_dates],
        }

        if curve_trade_dates and curve_trade_dates[-1] == latest_date:
            raw_series["up_count_series"][-1] = up_count
            raw_series["down_count_series"][-1] = down_count
            raw_series["limit_up_count_series"][-1] = limit_up_count
            raw_series["limit_down_count_series"][-1] = limit_down_count
            raw_series["up_5_count_series"][-1] = up_5_count
            raw_series["down_5_count_series"][-1] = down_5_count

        def _normalize_series(series):
            values = [float(v or 0) for v in series]
            if not values:
                return []
            min_val = min(values)
            max_val = max(values)
            if max_val == min_val:
                return [50.0 for _ in values]
            return [round((v - min_val) / (max_val - min_val) * 100, 2) for v in values]

        normalized_series = {
            key: _normalize_series(value)
            for key, value in raw_series.items()
        }

        latest_snapshot = {
            "up_count": up_count,
            "down_count": down_count,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "up_5_count": up_5_count,
            "down_5_count": down_5_count,
        }

        latest_cycle = (
            session.query(EmotionCycle)
            .filter(EmotionCycle.date == latest_date)
            .order_by(desc(EmotionCycle.date))
            .first()
        )
        if latest_cycle:
            previous_cycle = (
                session.query(EmotionCycle)
                .filter(EmotionCycle.date < latest_cycle.date)
                .order_by(desc(EmotionCycle.date))
                .first()
            )
        else:
            latest_cycle = session.query(EmotionCycle).order_by(desc(EmotionCycle.date)).first()
            previous_cycle = (
                session.query(EmotionCycle)
                .filter(EmotionCycle.date < latest_cycle.date)
                .order_by(desc(EmotionCycle.date))
                .first()
                if latest_cycle
                else None
            )

        emotion_phase = _build_emotion_phase(
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
        )

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
            "sh_amount": float(turnover_by_market.get("SH") or 0),
            "sz_amount": float(turnover_by_market.get("SZ") or 0),
            "turnover_source": "stock_aggregate",
            "change_distribution": distribution,
            "up_down_5_trend_7d": {
                "trade_dates": trend_dates_str,
                "up_5_counts": trend_up_counts,
                "down_5_counts": trend_down_counts,
                "latest_date": latest_date.isoformat(),
            },
            "emotion_curve_30d": {
                "trade_dates": curve_dates_str,
                "raw_series": raw_series,
                "normalized_series": normalized_series,
                "latest_snapshot": latest_snapshot,
                "latest_date": latest_date.isoformat(),
            },
            "sentiment_score": sentiment_score,
            "sentiment_level": sentiment_level,
            "emotion_phase": emotion_phase,
        }
    except Exception as exc:
        logger.error(f"获取涨跌统计失败: {exc}")
        return {"error": str(exc)}
    finally:
        session.close()


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

    session = next(db.get_session())
    try:
        latest_date = None
        if clickhouse_available():
            try:
                latest_date = clickhouse_scalar(
                    """
                    SELECT MAX(k.trade_date)
                    FROM kline_daily k
                    JOIN stocks s ON s.code = k.code
                    WHERE s.type = 'stock' AND COALESCE(s.quit, false) = false
                    """
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
                date_df = clickhouse_query_df(
                    """
                    SELECT DISTINCT trade_date
                    FROM kline_daily
                    WHERE trade_date <= ?
                    ORDER BY trade_date DESC
                    LIMIT ?
                    """,
                    [_date_text(latest_date), int(lookback_days)],
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
                kline_df = clickhouse_query_df(
                    f"""
                    SELECT code, trade_date, open, close, change_pct, amount, turnover_rate
                    FROM kline_daily
                    WHERE code IN ({code_placeholders})
                      AND trade_date IN ({date_placeholders})
                      AND open > 0
                    """,
                    stock_codes + date_texts,
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
            change_pct = _to_float(latest_row.change_pct, default=0.0)
            if latest_row.change_pct is None:
                open_price = _to_float(latest_row.open, default=0.0)
                close_price = _to_float(latest_row.close, default=0.0)
                if open_price > 0:
                    change_pct = (close_price - open_price) / open_price * 100

            buckets[bucket].append(
                {
                    "code": code,
                    "name": stock.name,
                    "market": stock.market,
                    "streak": int(streak),
                    "close": round(_to_float(latest_row.close), 3),
                    "change_pct": round(change_pct, 2),
                    "amount": round(_to_float(latest_row.amount), 2),
                    "turnover_rate": round(_to_float(latest_row.turnover_rate), 2),
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


@router.get("/emotion-trend")
def get_emotion_trend(days: int = 30, granularity: str = "daily"):
    """获取情绪周期趋势数据。"""
    session = next(db.get_session())
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
                      AND toHour(k.datetime) IN (10, 11, 13, 14, 15)
                    GROUP BY hour_time
                    HAVING total_rows >= 100
                    ORDER BY hour_time DESC
                    LIMIT 240
                    """
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

        if clickhouse_available():
            emotion_table = _resolve_table_name("emotion_cycle", ["emotion_cycle", "emotion_cycles"])
            if _has_clickhouse_table(emotion_table):
                trend_df = clickhouse_query_df(
                    f"""
                    SELECT
                        date, close_up_rate, intraday_up_rate, sh_up_rate, sz_up_rate, cyb_up_rate,
                        yesterday_monster_up_rate, yesterday_strong_up_rate, yesterday_weak_up_rate,
                        strong_up_rate, weak_up_rate, limit_up_follow_rate
                    FROM {emotion_table}
                    WHERE total_stocks >= {MIN_EMOTION_CYCLE_STOCKS}
                    ORDER BY date DESC
                    LIMIT ?
                    """,
                    [int(days)],
                )
                if trend_df is not None and not trend_df.empty:
                    trend_df = trend_df.iloc[::-1]
                    return {
                        "dates": [_date_text(v) for v in trend_df["date"].tolist()],
                        "close_up_rate": [float(v) for v in trend_df["close_up_rate"].fillna(0).tolist()],
                        "intraday_up_rate": [float(v) for v in trend_df["intraday_up_rate"].fillna(0).tolist()],
                        "sh_up_rate": [float(v) for v in trend_df["sh_up_rate"].fillna(0).tolist()],
                        "sz_up_rate": [float(v) for v in trend_df["sz_up_rate"].fillna(0).tolist()],
                        "cyb_up_rate": [float(v) for v in trend_df["cyb_up_rate"].fillna(0).tolist()],
                        "yesterday_monster_up_rate": [float(v) for v in trend_df["yesterday_monster_up_rate"].fillna(0).tolist()],
                        "yesterday_strong_up_rate": [float(v) for v in trend_df["yesterday_strong_up_rate"].fillna(0).tolist()],
                        "yesterday_weak_up_rate": [float(v) for v in trend_df["yesterday_weak_up_rate"].fillna(0).tolist()],
                        "strong_up_rate": [float(v) for v in trend_df["strong_up_rate"].fillna(0).tolist()],
                        "weak_up_rate": [float(v) for v in trend_df["weak_up_rate"].fillna(0).tolist()],
                        "limit_up_follow_rate": [float(v) for v in trend_df["limit_up_follow_rate"].fillna(0).tolist()],
                        "total": int(len(trend_df)),
                        "source": "clickhouse",
                    }
        emotion_cycles = (
            session.query(EmotionCycle)
            .filter(EmotionCycle.total_stocks >= MIN_EMOTION_CYCLE_STOCKS)
            .order_by(desc(EmotionCycle.date))
            .limit(days)
            .all()
        )
        if not emotion_cycles:
            return {
                "error": "数据库中没有情绪周期数据",
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

        emotion_cycles = list(reversed(emotion_cycles))
        return {
            "dates": [item.date.isoformat() for item in emotion_cycles if item.date],
            "close_up_rate": [float(item.close_up_rate) for item in emotion_cycles if item.close_up_rate is not None],
            "intraday_up_rate": [float(item.intraday_up_rate) for item in emotion_cycles if item.intraday_up_rate is not None],
            "sh_up_rate": [float(item.sh_up_rate) for item in emotion_cycles if item.sh_up_rate is not None],
            "sz_up_rate": [float(item.sz_up_rate) for item in emotion_cycles if item.sz_up_rate is not None],
            "cyb_up_rate": [float(item.cyb_up_rate) for item in emotion_cycles if item.cyb_up_rate is not None],
            "yesterday_monster_up_rate": [float(item.yesterday_monster_up_rate) for item in emotion_cycles if item.yesterday_monster_up_rate is not None],
            "yesterday_strong_up_rate": [float(item.yesterday_strong_up_rate) for item in emotion_cycles if item.yesterday_strong_up_rate is not None],
            "yesterday_weak_up_rate": [float(item.yesterday_weak_up_rate) for item in emotion_cycles if item.yesterday_weak_up_rate is not None],
            "strong_up_rate": [float(item.strong_up_rate) for item in emotion_cycles if item.strong_up_rate is not None],
            "weak_up_rate": [float(item.weak_up_rate) for item in emotion_cycles if item.weak_up_rate is not None],
            "limit_up_follow_rate": [float(item.limit_up_follow_rate) for item in emotion_cycles if item.limit_up_follow_rate is not None],
            "total": len(emotion_cycles),
        }
    except Exception as exc:
        logger.error(f"获取情绪周期趋势失败: {exc}")
        return {
            "error": str(exc),
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
    finally:
        session.close()


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

        from scripts.generate_emotion_cycle import EmotionCycleGenerator

        generator = EmotionCycleGenerator()
        generator.session = session
        emotion_cycle = generator.generate_emotion_cycle(trade_date)

        if not emotion_cycle:
            return {"success": False, "error": f"{trade_date} 情绪周期数据计算失败"}

        session.add(emotion_cycle)
        session.commit()

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
