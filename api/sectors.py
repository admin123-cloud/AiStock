"""
板块监控API
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from sqlalchemy import func, desc, or_, and_
from datetime import datetime, timedelta
from types import SimpleNamespace
import math
import threading
import time

from utils.database import db
from utils.market_warehouse import clickhouse_available, clickhouse_table_exists, clickhouse_query_df, clickhouse_scalar
from models.stock_models import Sector, SectorStock, Stock, SectorKlineDaily, KlineDaily
from utils.logger import get_logger

router = APIRouter(prefix="/sectors", tags=["板块"])
logger = get_logger("sectors")

_LATEST_TRADE_DATE_CACHE = {"expires_at": 0.0, "value": None}
_LATEST_TRADE_DATE_LOCK = threading.Lock()
_LATEST_TRADE_DATE_TTL_SECONDS = 60.0


def _date_text(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _finite_float(value):
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _finite_int(value):
    num = _finite_float(value)
    return int(num) if num is not None else None


def _latest_stock_trade_date_from_clickhouse():
    if not clickhouse_available():
        return None
    now = time.monotonic()
    with _LATEST_TRADE_DATE_LOCK:
        if _LATEST_TRADE_DATE_CACHE["value"] is not None and now < float(_LATEST_TRADE_DATE_CACHE["expires_at"]):
            return _LATEST_TRADE_DATE_CACHE["value"]
    try:
        val = clickhouse_scalar(
            """
            SELECT MAX(k.trade_date)
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            JOIN trade_calendar c
              ON c.trade_date = k.trade_date
             AND c.market = 'SH'
             AND c.is_trading = 1
            WHERE s.type = 'stock'
              AND k.trade_date >= today() - 30
            """,
            raw=True,
        )
        if not val or _date_text(val) in ("1970-01-01", "0000-00-00"):
            val = clickhouse_scalar(
                """
                SELECT MAX(k.trade_date)
                FROM kline_daily k
                JOIN stocks s ON s.code = k.code
                JOIN trade_calendar c
                  ON c.trade_date = k.trade_date
                 AND c.market = 'SH'
                 AND c.is_trading = 1
                WHERE s.type = 'stock'
                """,
                raw=True,
            )
        if val and _date_text(val) not in ("1970-01-01", "0000-00-00"):
            with _LATEST_TRADE_DATE_LOCK:
                _LATEST_TRADE_DATE_CACHE["value"] = val
                _LATEST_TRADE_DATE_CACHE["expires_at"] = time.monotonic() + _LATEST_TRADE_DATE_TTL_SECONDS
            return val
        return None
    except Exception as exc:
        logger.warning(f"ClickHouse sector latest trade date fallback to SQLAlchemy engine: {exc}")
        return None


def _daily_rows_for_codes_on_date_from_clickhouse(stock_codes, trade_date):
    if not stock_codes or not trade_date or not clickhouse_available():
        return None
    try:
        placeholders = ",".join(["?"] * len(stock_codes))
        df = clickhouse_query_df(
            f"""
            SELECT
                code,
                argMax(change_pct, created_at) AS change_pct,
                argMax(volume, created_at) AS volume,
                argMax(amount, created_at) AS amount
            FROM kline_daily
            WHERE trade_date = ?
              AND code IN ({placeholders})
            GROUP BY code
            """,
            [_date_text(trade_date)] + list(stock_codes),
            raw=True,
        )
        if df is None:
            return None
        return [
            {
                "code": row.code,
                "change_pct": _finite_float(row.change_pct),
                "volume": _finite_float(row.volume),
                "amount": _finite_float(row.amount),
            }
            for row in df.itertuples(index=False)
        ]
    except Exception as exc:
        logger.warning(f"ClickHouse sector daily rows fallback to SQLAlchemy engine: {exc}")
        return None

def _calculate_sector_rising_stats(session, sector_code, days=15, threshold=10):
    """
    计算板块过去N个交易日上涨天数超过阈值的股票数量

    Args:
        session: 数据库会话
        sector_code: 板块代码
        days: 统计天数，默认5天
        threshold: 上涨天数阈值，默认10天

    Returns:
        dict: {
            'total_stocks': 总成分股数
            'rising_stocks': 上涨天数超过阈值的股票数
            'ratio': 比例
        }
    """
    try:
        # 获取板块的成分股
        sector_stocks = session.query(SectorStock.stock_code).filter(
            SectorStock.sector_code == sector_code
        ).all()

        if not sector_stocks:
            return {
                'total_stocks': 0,
                'rising_stocks': 0,
                'ratio': 0.0
            }

        stock_codes = [ss.stock_code for ss in sector_stocks]

        # 获取最近N个交易日的板块K线数据
        recent_klines = session.query(
            SectorKlineDaily.trade_date,
            SectorKlineDaily.stock_count,
            SectorKlineDaily.rise_count
        ).filter(
            SectorKlineDaily.code == sector_code
        ).order_by(
            SectorKlineDaily.trade_date.desc()
        ).limit(days).all()

        if len(recent_klines) < days:
            # 板块K线数据不足，使用股票K线数据计算
            recent_dates = []
            if clickhouse_available():
                try:
                    date_df = clickhouse_query_df(
                        """
                        SELECT DISTINCT trade_date
                        FROM kline_daily
                        WHERE trade_date >= today() - 90
                        ORDER BY trade_date DESC
                        LIMIT ?
                        """,
                        [int(days)],
                        raw=True,
                    )
                    if date_df is None or len(date_df) < int(days):
                        date_df = clickhouse_query_df(
                            """
                            SELECT DISTINCT trade_date
                            FROM kline_daily
                            ORDER BY trade_date DESC
                            LIMIT ?
                            """,
                            [int(days)],
                            raw=True,
                        )
                    if date_df is not None and not date_df.empty:
                        recent_dates = [(d,) for d in date_df["trade_date"].tolist()]
                except Exception as exc:
                    logger.warning(f"ClickHouse sector recent dates fallback to SQLAlchemy engine: {exc}")
            if not recent_dates:
                recent_dates = session.query(
                    KlineDaily.trade_date
                ).distinct().order_by(
                    KlineDaily.trade_date.desc()
                ).limit(days).all()

            if len(recent_dates) < days:
                return {
                    'total_stocks': len(stock_codes),
                    'rising_stocks': 0,
                    'ratio': 0.0
                }

            date_list = [d[0] for d in recent_dates]
            rising_stock_count = None
            if clickhouse_available():
                try:
                    code_placeholders = ",".join(["?"] * len(stock_codes))
                    date_placeholders = ",".join(["?"] * len(date_list))
                    df = clickhouse_query_df(
                        f"""
                        SELECT code,
                               SUM(CASE WHEN pct > 0 THEN 1 ELSE 0 END) AS rising_days
                        FROM (
                            SELECT code, trade_date, argMax(change_pct, created_at) AS pct
                            FROM kline_daily
                            WHERE code IN ({code_placeholders})
                              AND trade_date IN ({date_placeholders})
                            GROUP BY code, trade_date
                        )
                        GROUP BY code
                        """,
                        stock_codes + [_date_text(d) for d in date_list],
                        raw=True,
                    )
                    if df is not None:
                        rising_stock_count = sum(
                            1 for row in df.itertuples(index=False)
                            if int(row.rising_days or 0) >= threshold
                        )
                except Exception as exc:
                    logger.warning(f"ClickHouse sector rising stats fallback to SQLAlchemy engine: {exc}")

            if rising_stock_count is None:
                rising_stock_count = 0
                for stock_code in stock_codes:
                    kline_data = session.query(
                        KlineDaily.trade_date,
                        KlineDaily.change_pct
                    ).filter(
                        and_(
                            KlineDaily.code == stock_code,
                            KlineDaily.trade_date.in_(date_list)
                        )
                    ).all()
                    rising_days = sum(1 for _, change_pct in kline_data if change_pct is not None and change_pct > 0)
                    if rising_days >= threshold:
                        rising_stock_count += 1

            ratio = (rising_stock_count / len(stock_codes) * 100) if stock_codes else 0.0

            return {
                'total_stocks': len(stock_codes),
                'rising_stocks': rising_stock_count,
                'ratio': round(ratio, 2)
            }
        else:
            # 使用板块K线数据快速计算
            # 统计板块整体上涨的天数
            rising_days = sum(1 for _, _, rise_count in recent_klines if rise_count is not None and rise_count > 0)

            # 计算平均上涨比例
            avg_rise_ratio = 0
            for _, stock_count, rise_count in recent_klines:
                if stock_count and stock_count > 0:
                    avg_rise_ratio += (rise_count / stock_count)

            avg_rise_ratio = avg_rise_ratio / len(recent_klines) if recent_klines else 0

            # 计算满足条件的股票数（简单估算）
            rising_stocks = int(stock_codes[0].count if hasattr(stock_codes, 'count') else len(stock_codes) * (avg_rise_ratio if rising_days >= threshold else 0))

            return {
                'total_stocks': len(stock_codes),
                'rising_stocks': 0,  # 板块K线不直接提供个股级别的统计
                'ratio': round(avg_rise_ratio * 100, 2)
            }

    except Exception as e:
        logger.error(f"计算板块 {sector_code} 的上涨统计失败: {e}")
        return {
            'total_stocks': 0,
            'rising_stocks': 0,
            'ratio': 0.0
        }


def _calculate_sector_change_pct(session, sector_code):
    """
    计算板块涨跌幅（优先使用板块K线表数据）

    Args:
        session: 数据库会话
        sector_code: 板块代码

    Returns:
        dict: 包含涨跌幅、上涨家数、下跌家数的字典
    """
    try:
        sector_stocks = session.query(SectorStock.stock_code).filter(
            SectorStock.sector_code == sector_code
        ).all()

        if not sector_stocks:
            return {
                "change_pct": None,
                "rise_count": 0,
                "fall_count": 0
            }

        stock_codes = [ss.stock_code for ss in sector_stocks]
        latest_stock_date_value = _latest_stock_trade_date_from_clickhouse()
        latest_stock_date = (latest_stock_date_value,) if latest_stock_date_value else None
        if latest_stock_date is None:
            latest_stock_date = session.query(
                KlineDaily.trade_date
            ).filter(
                KlineDaily.code.in_(stock_codes)
            ).distinct().order_by(
                KlineDaily.trade_date.desc()
            ).first()

        latest_sector_kline = None
        if clickhouse_table_exists("sector_kline_daily"):
            latest_sector_kline = session.query(
                SectorKlineDaily.trade_date,
                SectorKlineDaily.change_pct,
                SectorKlineDaily.stock_count,
                SectorKlineDaily.rise_count,
                SectorKlineDaily.fall_count,
                SectorKlineDaily.flat_count,
            ).filter(
                SectorKlineDaily.code == sector_code
            ).order_by(
                SectorKlineDaily.trade_date.desc()
            ).first()

        if latest_sector_kline and latest_sector_kline[1] is not None:
            sector_trade_date = latest_sector_kline[0]
            latest_trade_date = latest_stock_date[0] if latest_stock_date else None
            sector_has_counts = (latest_sector_kline[3] or 0) + (latest_sector_kline[4] or 0) + (latest_sector_kline[5] or 0) > 0
            sector_is_current = latest_trade_date is None or _date_text(sector_trade_date) == _date_text(latest_trade_date)
            if sector_is_current and (sector_has_counts or not latest_sector_kline[2]):
                return {
                    "change_pct": float(latest_sector_kline[1]),
                    "rise_count": latest_sector_kline[3] or 0,
                    "fall_count": latest_sector_kline[4] or 0
                }

        if not latest_stock_date:
            return {
                "change_pct": None,
                "rise_count": 0,
                "fall_count": 0
            }

        trade_date = latest_stock_date[0]
        clickhouse_rows = _daily_rows_for_codes_on_date_from_clickhouse(stock_codes, trade_date)
        if clickhouse_rows is not None:
            kline_data = [
                SimpleNamespace(code=row["code"], change_pct=row["change_pct"])
                for row in clickhouse_rows
            ]
        else:
            kline_data = session.query(
                KlineDaily.code,
                KlineDaily.change_pct
            ).filter(
                and_(
                    KlineDaily.code.in_(stock_codes),
                    KlineDaily.trade_date == trade_date
                )
            ).all()

        if not kline_data:
            return {
                "change_pct": None,
                "rise_count": 0,
                "fall_count": 0
            }

        valid_changes = []
        rise_count = 0
        fall_count = 0

        for k in kline_data:
            if k.change_pct is None:
                continue
            valid_changes.append(float(k.change_pct))
            if k.change_pct > 0:
                rise_count += 1
            elif k.change_pct < 0:
                fall_count += 1

        if not valid_changes:
            return {
                "change_pct": None,
                "rise_count": 0,
                "fall_count": 0
            }

        avg_change = sum(valid_changes) / len(valid_changes)
        return {
            "change_pct": round(float(avg_change), 2),
            "rise_count": rise_count,
            "fall_count": fall_count
        }

    except Exception as e:
        logger.error(f"计算板块 {sector_code} 的涨跌幅失败: {e}")
        return {
            "change_pct": None,
            "rise_count": 0,
            "fall_count": 0
        }


@router.get("/")
def get_sectors(
    page: int = 1,
    page_size: int = 20,
    sector_type: Optional[str] = None,
    level: Optional[int] = None,
    search: Optional[str] = None,
    sort_by: Optional[str] = None  # 排序字段：stock_count（成分股数）、name（板块名称），不传则按成分股数降序
):
    """
    获取板块列表

    Args:
        page: 页码
        page_size: 每页数量
        sector_type: 类型筛选(industry/concept/index)
        level: 层级筛选(1/2/3)
        search: 搜索关键词
        sort_by: 排序字段：stock_count（成分股数降序）、stock_count_asc（成分股数升序）、name（板块名称），默认按成分股数降序

    Returns:
        板块列表
    """
    session = next(db.get_session())
    try:
        # 构建基础查询
        query = session.query(
            Sector.code,
            Sector.name,
            Sector.type,
            Sector.level,
            func.count(SectorStock.stock_code).label('stock_count')
        ).outerjoin(
            SectorStock,
            Sector.code == SectorStock.sector_code
        )

        # 类型筛选
        sector_type_value = sector_type
        level_value = level
        
        # 处理前端传递的复合类型（如 industry_level1）
        if sector_type and '_' in sector_type:
            parts = sector_type.split('_')
            if len(parts) == 2 and parts[0] == 'industry' and parts[1].startswith('level'):
                sector_type_value = 'industry'
                level_value = int(parts[1].replace('level', ''))
        
        if sector_type_value:
            query = query.filter(Sector.type == sector_type_value)
        
        # 层级筛选
        if level_value:
            query = query.filter(Sector.level == level_value)

        # 搜索
        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Sector.code.like(search_pattern),
                    Sector.name.like(search_pattern)
                )
            )

        # 分组
        query = query.group_by(
            Sector.code,
            Sector.name,
            Sector.type,
            Sector.level
        )

        # 排序逻辑
        if sort_by == 'rise_ratio_desc':
            # 按今日涨幅降序（客户端排序）
            # 这里不做数据库排序，由客户端处理
            query = query.order_by(desc(func.count(SectorStock.stock_code)), Sector.code)
        elif sort_by == 'rise_ratio_asc':
            # 按今日涨幅升序（客户端排序）
            # 这里不做数据库排序，由客户端处理
            query = query.order_by(func.count(SectorStock.stock_code), Sector.code)
        elif sort_by == 'stats_15d_rise_ratio_desc':
            # 按15日涨幅降序（客户端排序）
            # 这里不做数据库排序，由客户端处理
            query = query.order_by(desc(func.count(SectorStock.stock_code)), Sector.code)
        elif sort_by == 'stats_15d_rise_ratio_asc':
            # 按15日涨幅升序（客户端排序）
            # 这里不做数据库排序，由客户端处理
            query = query.order_by(func.count(SectorStock.stock_code), Sector.code)
        else:
            # 默认按今日涨幅降序
            query = query.order_by(desc(func.count(SectorStock.stock_code)), Sector.code)

        # 总数（使用子查询）
        count_query = session.query(func.count(Sector.id.distinct()))
        if sector_type_value:
            count_query = count_query.filter(Sector.type == sector_type_value)
        if level_value:
            count_query = count_query.filter(Sector.level == level_value)
        if search:
            search_pattern = f"%{search}%"
            count_query = count_query.filter(
                or_(
                    Sector.code.like(search_pattern),
                    Sector.name.like(search_pattern)
                )
            )
        total = count_query.scalar()

        # 分页
        offset = (page - 1) * page_size
        results = query.offset(offset).limit(page_size).all()

        # 批量从 ClickHouse sector_kline_daily 取当前页板块最新涨跌统计，避免逐条 N+1 查询
        batch_sector_stats: dict[str, dict] = {}
        if results and clickhouse_available():
            try:
                page_codes = [r[0] for r in results]
                latest_td = _latest_stock_trade_date_from_clickhouse()
                ph = ",".join(["?"] * len(page_codes))
                if latest_td:
                    sk_df = clickhouse_query_df(
                        f"""
                        SELECT code,
                               argMax(change_pct, created_at) AS change_pct,
                               argMax(stock_count, created_at) AS stock_count,
                               argMax(rise_count, created_at) AS rise_count,
                               argMax(fall_count, created_at) AS fall_count,
                               argMax(flat_count, created_at) AS flat_count
                        FROM sector_kline_daily
                        WHERE trade_date = ?
                          AND code IN ({ph})
                        GROUP BY code
                        """,
                        [_date_text(latest_td)] + page_codes,
                        raw=True,
                    )
                else:
                    sk_df = clickhouse_query_df(
                        f"""
                        SELECT code,
                               change_pct,
                               stock_count,
                               rise_count,
                               fall_count,
                               flat_count
                        FROM sector_kline_daily FINAL
                        WHERE code IN ({ph})
                        ORDER BY code ASC, trade_date DESC
                        LIMIT 1 BY code
                        """,
                        page_codes,
                        raw=True,
                    )
                if sk_df is not None and not sk_df.empty:
                    for srow in sk_df.itertuples(index=False):
                        chg = _finite_float(getattr(srow, "change_pct", None))
                        if chg is None:
                            continue
                        r_cnt = int(getattr(srow, "rise_count", 0) or 0)
                        f_cnt = int(getattr(srow, "fall_count", 0) or 0)
                        fl_cnt = int(getattr(srow, "flat_count", 0) or 0)
                        sc_cnt = int(getattr(srow, "stock_count", 0) or 0)
                        if (r_cnt + f_cnt + fl_cnt > 0) or not sc_cnt:
                            batch_sector_stats[str(srow.code)] = {
                                "change_pct": chg,
                                "rise_count": r_cnt,
                                "fall_count": f_cnt,
                            }
            except Exception as exc:
                logger.warning(f"批量获取板块日线统计失败，将按单板块回退: {exc}")

        # 构建返回数据
        items = []
        for code, name, type_, level, stock_count in results:
            try:
                sector_stats = batch_sector_stats.get(code)
                if sector_stats is None:
                    sector_stats = _calculate_sector_change_pct(session, code)
                # 计算上涨比例
                total_valid = sector_stats.get('rise_count', 0) + sector_stats.get('fall_count', 0)
                rise_ratio = (sector_stats.get('rise_count', 0) / total_valid * 100) if total_valid > 0 else 0
                items.append({
                    "code": code,
                    "name": name,
                    "type": type_,
                    "level": level,
                    "stock_count": stock_count or 0,
                    "change_pct": sector_stats.get('change_pct'),
                    "rise_count": sector_stats.get('rise_count', 0),
                    "fall_count": sector_stats.get('fall_count', 0),
                    "rise_ratio": round(rise_ratio, 2)
                })
            except Exception as e:
                logger.error(f"处理板块 {code} 失败: {e}")
                # 即使计算失败，也添加板块基本信息
                items.append({
                    "code": code,
                    "name": name,
                    "type": type_,
                    "level": level,
                    "stock_count": stock_count or 0,
                    "change_pct": None,
                    "rise_count": 0,
                    "fall_count": 0,
                    "rise_ratio": 0
                })

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items
        }

    except Exception as e:
        logger.error(f"获取板块列表失败: {e}")
        return {
            "total": 0,
            "page": page,
            "page_size": page_size,
            "items": []
        }
    finally:
        session.close()


@router.get("/stats")
def get_sectors_stats():
    """
    获取板块统计信息

    Returns:
        统计数据
    """
    session = next(db.get_session())
    try:
        # 按类型统计
        type_stats = session.query(
            Sector.type,
            func.count(Sector.id).label('count')
        ).group_by(Sector.type).all()

        by_type = {t: c for t, c in type_stats}

        # 总数
        total = session.query(Sector).count()

        # 行业板块数
        industry_count = by_type.get('industry', 0)

        # 概念板块数
        concept_count = by_type.get('concept', 0)

        return {
            "total": total,
            "industry_count": industry_count,
            "concept_count": concept_count,
            "by_type": by_type
        }

    finally:
        session.close()


@router.get("/stock-count")
def get_sectors_stock_count(
    sector_type: Optional[str] = None,
    page: int = 1,
    page_size: int = 50
):
    """
    获取板块成分股统计（联合查询 sectors 和 sector_stocks 表）

    Args:
        sector_type: 类型筛选(industry/concept)，不传则返回所有类型
        page: 页码
        page_size: 每页数量

    Returns:
        板块成分股统计列表
    """
    session = next(db.get_session())
    try:
        # 联合查询统计每个板块的成分股数量
        query = session.query(
            Sector.code,
            Sector.name,
            Sector.type,
            func.count(SectorStock.stock_code).label('actual_stock_count')
        ).outerjoin(
            SectorStock,
            Sector.code == SectorStock.sector_code
        ).group_by(
            Sector.code,
            Sector.name,
            Sector.type
        )

        # 类型筛选
        if sector_type:
            query = query.filter(Sector.type == sector_type)

        # 总数
        total = query.count()

        # 分页
        offset = (page - 1) * page_size
        results = query.order_by(
            desc('actual_stock_count'),
            Sector.code
        ).offset(offset).limit(page_size).all()

        items = []
        for code, name, type_, count in results:
            items.append({
                "code": code,
                "name": name,
                "type": type_,
                "stock_count": count or 0
            })

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items
        }

    finally:
        session.close()


@router.get("/{code}")
def get_sector_detail(code: str):
    """
    获取板块详情

    Args:
        code: 板块代码

    Returns:
        板块详情和成分股
    """
    session = next(db.get_session())
    try:
        # 板块信息
        sector = session.query(Sector).filter(Sector.code == code).first()
        if not sector:
            raise HTTPException(status_code=404, detail=f"板块 {code} 不存在在")

        # 成分股
        stocks_query = session.query(SectorStock, Stock).outerjoin(
            Stock, SectorStock.stock_code == Stock.code
        ).filter(SectorStock.sector_code == code)

        # 获取最近一个交易日
        latest_date_value = _latest_stock_trade_date_from_clickhouse()
        if latest_date_value:
            trade_date = latest_date_value
        else:
            latest_date = session.query(
                KlineDaily.trade_date
            ).order_by(
                KlineDaily.trade_date.desc()
            ).first()
            trade_date = latest_date[0] if latest_date else None

        stock_pairs = stocks_query.all()
        stock_codes = [ss.stock_code for ss, _ in stock_pairs]
        kline_map = {}
        clickhouse_rows = None
        if trade_date:
            clickhouse_rows = _daily_rows_for_codes_on_date_from_clickhouse(stock_codes, trade_date)
            if clickhouse_rows is not None:
                kline_map = {row["code"]: row for row in clickhouse_rows}

        stocks = []
        for ss, stock in stock_pairs:
            item = {
                "code": ss.stock_code,
                "name": stock.name if stock else ss.stock_code,
                "market": stock.market if stock else None,
                "change_pct": None,
                "volume": None,
                "amount": None
            }

            # 如果有最近的交易日，查询该股票的K线数据
            if trade_date:
                stock_kline = kline_map.get(ss.stock_code)
                if stock_kline is None and clickhouse_rows is None:
                    stock_kline = session.query(KlineDaily).filter(
                        KlineDaily.code == ss.stock_code,
                        KlineDaily.trade_date == trade_date
                    ).first()

                if stock_kline:
                    if isinstance(stock_kline, dict):
                        item["change_pct"] = _finite_float(stock_kline.get("change_pct"))
                        item["volume"] = _finite_int(stock_kline.get("volume"))
                        item["amount"] = _finite_float(stock_kline.get("amount"))
                    else:
                        item["change_pct"] = _finite_float(stock_kline.change_pct)
                        item["volume"] = _finite_int(stock_kline.volume)
                        item["amount"] = _finite_float(stock_kline.amount)

            stocks.append(item)

        # 计算板块总体数据
        total_volume = sum(item["volume"] for item in stocks if item["volume"])
        total_amount = sum(item["amount"] for item in stocks if item["amount"])
        rise_count = sum(1 for item in stocks if item["change_pct"] and item["change_pct"] > 0)
        fall_count = sum(1 for item in stocks if item["change_pct"] and item["change_pct"] < 0)
        flat_count = sum(1 for item in stocks if item["change_pct"] == 0)

        # 计算板块平均涨跌幅
        valid_change_pcts = [item["change_pct"] for item in stocks if item["change_pct"] is not None]
        avg_change_pct = sum(valid_change_pcts) / len(valid_change_pcts) if valid_change_pcts else None

        return {
            "code": sector.code,
            "name": sector.name,
            "type": sector.type,
            "stock_count": sector.stock_count or len(stocks),
            "trade_date": str(trade_date) if trade_date else None,
            "total_volume": total_volume,
            "total_amount": total_amount,
            "rise_count": rise_count,
            "fall_count": fall_count,
            "flat_count": flat_count,
            "avg_change_pct": avg_change_pct,
            "stocks": stocks
        }

    finally:
        session.close()


@router.get("/{code}/history")
def get_sector_history(code: str, days: int = 15):
    """
    获取板块历史数据

    Args:
        code: 板块代码
        days: 历史K线周期数量，默认15个

    Returns:
        板块历史数据列表
    """
    session = next(db.get_session())
    try:
        # 验证板块是否存在
        sector = session.query(Sector).filter(Sector.code == code).first()
        if not sector:
            raise HTTPException(status_code=404, detail=f"板块 {code} 不存在")

        # 查询板块历史数据，按交易日期降序排序，限制返回指定数量的记录
        # 降序排序后，最近的日期会在前面
        history_data = session.query(
            SectorKlineDaily.trade_date,
            SectorKlineDaily.rise_count,
            SectorKlineDaily.fall_count,
            SectorKlineDaily.flat_count,
            SectorKlineDaily.change_pct
        ).filter(
            SectorKlineDaily.code == code
        ).order_by(
            SectorKlineDaily.trade_date.desc()
        ).limit(days).all()

        # 格式化数据
        result = []
        for trade_date, rise_count, fall_count, flat_count, change_pct in history_data:
            result.append({
                "trade_date": str(trade_date),
                "rise_count": rise_count or 0,
                "fall_count": fall_count or 0,
                "flat_count": flat_count or 0,
                "change_pct": float(change_pct) if change_pct else None
            })

        return result

    finally:
        session.close()


@router.get("/{code}/stocks")
def get_sector_stocks(code: str, page: int = 1, page_size: int = 50):
    """
    获取板块成分股

    Args:
        code: 板块代码
        page: 页码
        page_size: 每页数量

    Returns:
        成分股列表
    """
    session = next(db.get_session())
    try:
        # 检查板块是否存在
        sector = session.query(Sector).filter(Sector.code == code).first()
        if not sector:
            raise HTTPException(status_code=404, detail=f"板块 {code} 不存在")

        # 先统计总数
        total = session.query(SectorStock).filter(SectorStock.sector_code == code).count()

        # 查询成分股
        query = session.query(SectorStock, Stock).outerjoin(
            Stock, SectorStock.stock_code == Stock.code
        ).filter(SectorStock.sector_code == code)

        offset = (page - 1) * page_size
        results = query.order_by(SectorStock.id).offset(offset).limit(page_size).all()

        stocks = []
        for ss, stock in results:
            item = {
                "code": ss.stock_code,
                "name": stock.name if stock else ss.stock_code,
                "market": stock.market if stock else None,
                "type": stock.type if stock else None,
                "industry": stock.industry if stock else None
            }
            stocks.append(item)

        return {
            "sector_code": code,
            "sector_name": sector.name,
            "total": total,
            "page": page,
            "page_size": page_size,
            "stocks": stocks
        }

    finally:
        session.close()


@router.get("/{code}/kline")
def get_sector_kline(
    code: str,
    days: int = 30
):
    """
    获取板块K线数据

    Args:
        code: 板块代码
        days: 查询天数

    Returns:
        板块K线数据
    """
    session = next(db.get_session())
    try:
        # 检查板块是否存在
        sector = session.query(Sector).filter(Sector.code == code).first()
        if not sector:
            raise HTTPException(status_code=404, detail=f"板块 {code} 不存在")

        # 查询板块K线数据
        klines = session.query(
            SectorKlineDaily.trade_date,
            SectorKlineDaily.change_pct,
            SectorKlineDaily.stock_count,
            SectorKlineDaily.rise_count,
            SectorKlineDaily.fall_count,
            SectorKlineDaily.flat_count,
            SectorKlineDaily.limit_up_count,
            SectorKlineDaily.limit_down_count,
            SectorKlineDaily.total_amount,
            SectorKlineDaily.total_volume,
            SectorKlineDaily.open,
            SectorKlineDaily.high,
            SectorKlineDaily.low,
            SectorKlineDaily.close
        ).filter(
            SectorKlineDaily.code == code
        ).order_by(
            SectorKlineDaily.trade_date.desc()
        ).limit(days).all()

        if not klines:
            return {
                "code": code,
                "name": sector.name,
                "klines": []
            }

        # 格式化K线数据
        kline_items = []
        for k in klines:
            kline_items.append({
                "trade_date": k.trade_date.isoformat() if k.trade_date else None,
                "change_pct": float(k.change_pct) if k.change_pct else None,
                "stock_count": k.stock_count,
                "rise_count": k.rise_count,
                "fall_count": k.fall_count,
                "flat_count": k.flat_count,
                "limit_up_count": k.limit_up_count,
                "limit_down_count": k.limit_down_count,
                "total_amount": float(k.total_amount) if k.total_amount else None,
                "total_volume": k.total_volume,
                "open": float(k.open) if k.open else None,
                "high": float(k.high) if k.high else None,
                "low": float(k.low) if k.low else None,
                "close": float(k.close) if k.close else None
            })

        return {
            "code": code,
            "name": sector.name,
            "klines": kline_items
        }

    finally:
        session.close()


@router.get("/{code}/historical-stats")
def get_sector_historical_stats(code: str):
    """
    获取板块历史涨跌统计（5日、10日、25日）

    Args:
        code: 板块代码

    Returns:
        历史涨跌统计
    """
    session = next(db.get_session())
    try:
        # 检查板块是否存在
        sector = session.query(Sector).filter(Sector.code == code).first()
        if not sector:
            raise HTTPException(status_code=404, detail=f"板块 {code} 不存在")

        # 获取板块最近的K线数据
        recent_klines = session.query(
            SectorKlineDaily.trade_date,
            SectorKlineDaily.rise_count,
            SectorKlineDaily.fall_count,
            SectorKlineDaily.stock_count
        ).filter(
            SectorKlineDaily.code == code
        ).order_by(
            SectorKlineDaily.trade_date.desc()
        ).limit(15).all()

        if not recent_klines:
            return {
                "code": code,
                "name": sector.name,
                "stats_5d": None,
                "stats_10d": None,
                "stats_15d": None
            }

        # 计算不同周期的统计数据
        def calculate_stats(klines, days):
            if len(klines) < days:
                return None

            total_rise = 0
            total_fall = 0
            valid_days = 0

            for k in klines[:days]:
                if k.stock_count and k.stock_count > 0:
                    total_rise += k.rise_count if k.rise_count else 0
                    total_fall += k.fall_count if k.fall_count else 0
                    valid_days += 1

            if valid_days == 0:
                return None

            # 计算上涨比例
            rise_ratio = (total_rise / (total_rise + total_fall) * 100) if (total_rise + total_fall) > 0 else 0

            return {
                "days": days,
                "rise_days": total_rise,
                "fall_days": total_fall,
                "total_days": total_rise + total_fall,
                "rise_ratio": round(rise_ratio, 2),
                "ratio_text": f"{total_rise}/{total_rise + total_fall}"
            }

        return {
            "code": code,
            "name": sector.name,
            "stats_5d": calculate_stats(recent_klines, 5),
            "stats_10d": calculate_stats(recent_klines, 10),
            "stats_15d": calculate_stats(recent_klines, 15)
        }

    finally:
        session.close()
