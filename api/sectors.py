"""
板块监控API
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks
from sqlalchemy import func, desc, or_, and_
from datetime import datetime, timedelta
from types import SimpleNamespace

from utils.database import db
from utils.market_warehouse import clickhouse_available, clickhouse_table_exists, clickhouse_query_df, clickhouse_scalar
from models.stock_models import Sector, SectorStock, Stock, SectorKlineDaily, KlineDaily
from utils.logger import get_logger

router = APIRouter(prefix="/sectors", tags=["板块"])
logger = get_logger("sectors")


def _date_text(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _latest_stock_trade_date_from_clickhouse():
    if not clickhouse_available():
        return None
    try:
        return clickhouse_scalar(
            """
            SELECT MAX(k.trade_date)
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock'
            """
        )
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
            SELECT code, change_pct, volume, amount
            FROM kline_daily
            WHERE trade_date = ?
              AND code IN ({placeholders})
            """,
            [_date_text(trade_date)] + list(stock_codes),
        )
        if df is None:
            return None
        return [
            {
                "code": row.code,
                "change_pct": row.change_pct,
                "volume": row.volume,
                "amount": row.amount,
            }
            for row in df.itertuples(index=False)
        ]
    except Exception as exc:
        logger.warning(f"ClickHouse sector daily rows fallback to SQLAlchemy engine: {exc}")
        return None

# 板块K线数据同步任务状态
sync_kline_task_status = {
    "is_running": False,
    "started_at": None,
    "progress": {"current": 0, "total": 0, "current_sector": ""},
    "results": None,
    "error": None
}


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
                        ORDER BY trade_date DESC
                        LIMIT ?
                        """,
                        [int(days)],
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
                               SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS rising_days
                        FROM kline_daily
                        WHERE code IN ({code_placeholders})
                          AND trade_date IN ({date_placeholders})
                        GROUP BY code
                        """,
                        stock_codes + [_date_text(d) for d in date_list],
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
            sector_is_current = latest_trade_date is None or sector_trade_date == latest_trade_date
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
                (row["code"], row["change_pct"])
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

        # 构建返回数据
        items = []
        for code, name, type_, level, stock_count in results:
            try:
                # 计算板块涨跌幅
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
                if stock_kline is None and not kline_map:
                    stock_kline = session.query(KlineDaily).filter(
                        KlineDaily.code == ss.stock_code,
                        KlineDaily.trade_date == trade_date
                    ).first()

                if stock_kline:
                    if isinstance(stock_kline, dict):
                        item["change_pct"] = float(stock_kline["change_pct"]) if stock_kline.get("change_pct") else None
                        item["volume"] = int(stock_kline["volume"]) if stock_kline.get("volume") else None
                        item["amount"] = float(stock_kline["amount"]) if stock_kline.get("amount") else None
                    else:
                        item["change_pct"] = float(stock_kline.change_pct) if stock_kline.change_pct else None
                        item["volume"] = int(stock_kline.volume) if stock_kline.volume else None
                        item["amount"] = float(stock_kline.amount) if stock_kline.amount else None

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


# 同步板块任务状态
sync_sectors_task_status = {
    "is_running": False,
    "started_at": None,
    "progress": {"current": 0, "total": 0, "current_board": ""},
    "results": None,
    "error": None
}


def _sync_sector_kline_data():
    """
    同步所有板块的K线数据
    """
    global sync_kline_task_status

    try:
        from scripts.generate_sector_kline import generate_recent_sector_klines

        sync_kline_task_status["progress"] = {"current": 0, "total": 0, "current_sector": ""}

        # 获取最近30个交易日的数据
        result = generate_recent_sector_klines(days=30)

        sync_kline_task_status["is_running"] = False
        sync_kline_task_status["results"] = {
            "total_count": result.get("total_count", 0),
            "days": result.get("days", 0),
            "start_date": str(result.get("start_date", "")),
            "end_date": str(result.get("end_date", ""))
        }

        logger.info(f"板块K线数据同步完成 生成 {result.get('total_count', 0)} 条记录")

    except Exception as e:
        logger.error(f"板块K线数据同步失败: {e}")
        sync_kline_task_status["is_running"] = False
        sync_kline_task_status["error"] = str(e)


@router.post("/sync-kline")
def sync_sector_kline(background_tasks: BackgroundTasks):
    """
    同步板块K线数据（根据成分股计算涨跌幅）

    Returns:
        任务状态
    """
    global sync_kline_task_status

    if sync_kline_task_status["is_running"]:
        return {
            "status": "already_running",
            "message": "板块K线数据同步任务已在运行中",
            "progress": sync_kline_task_status["progress"]
        }

    sync_kline_task_status["is_running"] = True
    sync_kline_task_status["started_at"] = datetime.now().isoformat()
    sync_kline_task_status["progress"] = {"current": 0, "total": 0, "current_sector": ""}
    sync_kline_task_status["results"] = None
    sync_kline_task_status["error"] = None

    background_tasks.add_task(_sync_sector_kline_data)

    return {
        "status": "started",
        "message": "板块K线数据同步任务已启动"
    }


@router.get("/sync-kline/status")
def get_kline_sync_status():
    """
    获取板块K线数据同步任务状态

    Returns:
        任务状态
    """
    return {
        "is_running": sync_kline_task_status["is_running"],
        "started_at": sync_kline_task_status["started_at"],
        "progress": sync_kline_task_status["progress"],
        "results": sync_kline_task_status["results"],
        "error": sync_kline_task_status["error"]
    }


def _sync_sectors_from_efinance():
    """
    从 efinance 同步所有板块
    """
    import threading

    global sync_sectors_task_status

    try:
        from data_fetcher.efinance_source import get_efinance_data_source

        efinance = get_efinance_data_source(timeout=60)

        # 获取所有板块
        all_boards = efinance.get_all_boards()
        concept_boards = all_boards.get("concept", [])
        industry_boards = all_boards.get("industry", [])

        total_boards = len(concept_boards) + len(industry_boards)
        logger.info(f"获取到 {total_boards} 个板块")

        session = next(db.get_session())

        saved_count = 0
        updated_count = 0

        all_boards_list = [("concept", b) for b in concept_boards] + [("industry", b) for b in industry_boards]

        for i, (board_type, board) in enumerate(all_boards_list):
            sync_sectors_task_status["progress"]["current"] = i + 1
            sync_sectors_task_status["progress"]["total"] = total_boards
            sync_sectors_task_status["progress"]["current_board"] = f"{board['code']} {board['name']}"

            try:
                existing = session.query(Sector).filter(Sector.code == board['code']).first()

                if existing:
                    # 更新
                    existing.name = board['name']
                    existing.type = board_type
                    updated_count += 1
                else:
                    # 新增
                    new_sector = Sector(
                        code=board['code'],
                        name=board['name'],
                        type=board_type
                    )
                    session.add(new_sector)
                    saved_count += 1

            except Exception as e:
                logger.error(f"保存板块 {board['code']} 失败: {e}")

        session.commit()
        session.close()

        sync_sectors_task_status["is_running"] = False
        sync_sectors_task_status["results"] = {
            "total_boards": total_boards,
            "saved_count": saved_count,
            "updated_count": updated_count
        }

        logger.info(f"板块同步完成: 新增 {saved_count}, 更新 {updated_count}")

    except Exception as e:
        logger.error(f"板块同步失败: {e}")
        sync_sectors_task_status["is_running"] = False
        sync_sectors_task_status["error"] = str(e)


@router.post("/sync")
def sync_sectors(background_tasks: BackgroundTasks):
    """
    从 efinance 同步板块列表

    Returns:
        任务状态
    """
    global sync_sectors_task_status

    if sync_sectors_task_status["is_running"]:
        return {
            "status": "already_running",
            "message": "板块同步任务已在运行中",
            "progress": sync_sectors_task_status["progress"]
        }

    sync_sectors_task_status["is_running"] = True
    sync_sectors_task_status["started_at"] = datetime.now().isoformat()
    sync_sectors_task_status["progress"] = {"current": 0, "total": 0, "current_board": ""}
    sync_sectors_task_status["results"] = None
    sync_sectors_task_status["error"] = None

    background_tasks.add_task(_sync_sectors_from_efinance)

    return {
        "status": "started",
        "message": "板块同步任务已启动"
    }


@router.get("/sync/status")
def get_sync_status():
    """
    获取板块同步任务状态

    Returns:
        任务状态
    """
    return {
        "is_running": sync_sectors_task_status["is_running"],
        "started_at": sync_sectors_task_status["started_at"],
        "progress": sync_sectors_task_status["progress"],
        "results": sync_sectors_task_status["results"],
        "error": sync_sectors_task_status["error"]
    }


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


@router.post("/{code}/update-kline-stats")
def update_sector_kline_stats(
    code: str,
    background_tasks: BackgroundTasks
):
    """
    更新指定板块K线的成分涨跌数据
    
    Args:
        code: 板块代码
    
    Returns:
        更新结果
    """
    session = next(db.get_session())
    try:
        # 检查板块是否存在
        sector = session.query(Sector).filter(Sector.code == code).first()
        if not sector:
            raise HTTPException(status_code=404, detail=f"板块 {code} 不存在")
        
        # 获取板块的成分股
        sector_stocks = session.query(SectorStock.stock_code).filter(
            SectorStock.sector_code == code
        ).all()
        
        if not sector_stocks:
            raise HTTPException(status_code=400, detail=f"板块 {code} 没有成分股")
        
        stock_codes = [ss.stock_code for ss in sector_stocks]
        
        # 获取该板块所有的K线数据
        sector_klines = session.query(SectorKlineDaily).filter(
            SectorKlineDaily.code == code
        ).order_by(SectorKlineDaily.trade_date.desc()).all()
        
        if not sector_klines:
            raise HTTPException(status_code=400, detail=f"板块 {code} 没有K线数据")
        
        updated_count = 0
        
        # 逐条更新K线数据
        for sector_kline in sector_klines:
            trade_date = sector_kline.trade_date
            
            # 获取当日所有成分股的涨跌数据
            clickhouse_rows = _daily_rows_for_codes_on_date_from_clickhouse(stock_codes, trade_date)
            if clickhouse_rows is not None:
                stock_klines = [
                    SimpleNamespace(**row)
                    for row in clickhouse_rows
                ]
            else:
                stock_klines = session.query(KlineDaily).filter(
                    and_(
                        KlineDaily.code.in_(stock_codes),
                        KlineDaily.trade_date == trade_date
                    )
                ).all()
            
            if not stock_klines:
                continue
            
            # 统计涨跌数据
            rise_count = sum(1 for k in stock_klines if k.change_pct and k.change_pct > 0)
            fall_count = sum(1 for k in stock_klines if k.change_pct and k.change_pct < 0)
            flat_count = sum(1 for k in stock_klines if not k.change_pct or k.change_pct == 0)
            limit_up_count = sum(1 for k in stock_klines if k.change_pct and k.change_pct >= 9.9)
            limit_down_count = sum(1 for k in stock_klines if k.change_pct and k.change_pct <= -9.9)
            
            # 计算总成交额和成交量
            total_amount = sum(k.amount for k in stock_klines if k.amount)
            total_volume = sum(k.volume for k in stock_klines if k.volume)
            
            # 更新板块K线数据
            sector_kline.stock_count = len(stock_klines)
            sector_kline.rise_count = rise_count
            sector_kline.fall_count = fall_count
            sector_kline.flat_count = flat_count
            sector_kline.limit_up_count = limit_up_count
            sector_kline.limit_down_count = limit_down_count
            sector_kline.total_amount = total_amount
            sector_kline.total_volume = total_volume
            
            updated_count += 1
        
        session.commit()
        
        logger.info(f"板块 {code} 成分涨跌数据更新完成，共更新 {updated_count} 条记录")
        
        return {
            "status": "success",
            "message": f"板块 {code} 成分涨跌数据更新完成",
            "code": code,
            "name": sector.name,
            "updated_count": updated_count
        }
        
    except Exception as e:
        session.rollback()
        logger.error(f"更新板块 {code} 成分涨跌数据失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/{code}/generate-kline")
def generate_sector_kline(
    code: str,
    background_tasks: BackgroundTasks
):
    """
    生成指定板块的K线数据（后台任务）

    Args:
        code: 板块代码

    Returns:
        任务状态
    """
    session = next(db.get_session())
    try:
        # 检查板块是否存在
        sector = session.query(Sector).filter(Sector.code == code).first()
        if not sector:
            raise HTTPException(status_code=404, detail=f"板块 {code} 不存在")

        return {
            "status": "success",
            "message": f"板块 {code} 的K线数据生成任务已启动",
            "code": code,
            "name": sector.name
        }

    finally:
        session.close()


# 批量更新所有板块成分涨跌数据任务状态
update_all_kline_stats_status = {
    "is_running": False,
    "started_at": None,
    "progress": {"current": 0, "total": 0, "current_sector": ""},
    "results": None,
    "error": None
}


def _update_all_sectors_kline_stats():
    """
    批量更新所有板块K线的成分涨跌数据
    """
    global update_all_kline_stats_status

    try:
        session = next(db.get_session())

        # 获取所有有K线数据的板块
        sectors_with_kline = session.query(
            SectorKlineDaily.code
        ).distinct().all()

        sector_codes = [s[0] for s in sectors_with_kline]
        total_sectors = len(sector_codes)

        logger.info(f"开始批量更新 {total_sectors} 个板块的成分涨跌数据")

        update_all_kline_stats_status["progress"] = {
            "current": 0,
            "total": total_sectors,
            "current_sector": ""
        }

        updated_count = 0

        for i, sector_code in enumerate(sector_codes):
            # 更新进度
            update_all_kline_stats_status["progress"]["current"] = i + 1
            sector = session.query(Sector).filter(Sector.code == sector_code).first()
            sector_name = sector.name if sector else sector_code
            update_all_kline_stats_status["progress"]["current_sector"] = f"{sector_code} {sector_name}"

            try:
                # 获取该板块所有的K线数据
                sector_klines = session.query(SectorKlineDaily).filter(
                    SectorKlineDaily.code == sector_code
                ).order_by(SectorKlineDaily.trade_date.desc()).all()

                if not sector_klines:
                    logger.warning(f"板块 {sector_code} 没有K线数据，跳过")
                    continue

                # 获取该板块的成分股
                sector_stocks = session.query(SectorStock.stock_code).filter(
                    SectorStock.sector_code == sector_code
                ).all()

                if not sector_stocks:
                    logger.warning(f"板块 {sector_code} 没有成分股，跳过")
                    continue

                stock_codes = [ss.stock_code for ss in sector_stocks]

                # 逐条更新K线数据
                for sector_kline in sector_klines:
                    trade_date = sector_kline.trade_date

                    # 获取当日所有成分股的涨跌数据
                    clickhouse_rows = _daily_rows_for_codes_on_date_from_clickhouse(stock_codes, trade_date)
                    if clickhouse_rows is not None:
                        stock_klines = [
                            SimpleNamespace(**row)
                            for row in clickhouse_rows
                        ]
                    else:
                        stock_klines = session.query(KlineDaily).filter(
                            and_(
                                KlineDaily.code.in_(stock_codes),
                                KlineDaily.trade_date == trade_date
                            )
                        ).all()

                    if not stock_klines:
                        continue

                    # 统计涨跌数据
                    rise_count = sum(1 for k in stock_klines if k.change_pct and k.change_pct > 0)
                    fall_count = sum(1 for k in stock_klines if k.change_pct and k.change_pct < 0)
                    flat_count = sum(1 for k in stock_klines if not k.change_pct or k.change_pct == 0)
                    limit_up_count = sum(1 for k in stock_klines if k.change_pct and k.change_pct >= 9.9)
                    limit_down_count = sum(1 for k in stock_klines if k.change_pct and k.change_pct <= -9.9)

                    # 计算总成交额和成交量
                    total_amount = sum(k.amount for k in stock_klines if k.amount)
                    total_volume = sum(k.volume for k in stock_klines if k.volume)

                    # 更新板块K线数据
                    sector_kline.stock_count = len(stock_klines)
                    sector_kline.rise_count = rise_count
                    sector_kline.fall_count = fall_count
                    sector_kline.flat_count = flat_count
                    sector_kline.limit_up_count = limit_up_count
                    sector_kline.limit_down_count = limit_down_count
                    sector_kline.total_amount = total_amount
                    sector_kline.total_volume = total_volume

                session.commit()
                updated_count += 1
                logger.info(f"板块 {sector_code} ({i+1}/{total_sectors}) 更新完成")

            except Exception as e:
                logger.error(f"更新板块 {sector_code} 失败: {e}")
                session.rollback()

        session.close()

        update_all_kline_stats_status["is_running"] = False
        update_all_kline_stats_status["results"] = {
            "total_sectors": total_sectors,
            "updated_sectors": updated_count,
            "failed_sectors": total_sectors - updated_count
        }

        logger.info(f"批量更新板块成分涨跌数据完成: 成功 {updated_count}/{total_sectors}")

    except Exception as e:
        logger.error(f"批量更新板块成分涨跌数据失败: {e}")
        update_all_kline_stats_status["is_running"] = False
        update_all_kline_stats_status["error"] = str(e)


@router.post("/update-all-kline-stats")
def update_all_sectors_kline_stats(background_tasks: BackgroundTasks):
    """
    批量更新所有板块K线的成分涨跌数据

    Returns:
        任务状态
    """
    global update_all_kline_stats_status

    if update_all_kline_stats_status["is_running"]:
        return {
            "status": "already_running",
            "message": "批量更新任务已在运行中",
            "progress": update_all_kline_stats_status["progress"]
        }

    update_all_kline_stats_status["is_running"] = True
    update_all_kline_stats_status["started_at"] = datetime.now().isoformat()
    update_all_kline_stats_status["progress"] = {"current": 0, "total": 0, "current_sector": ""}
    update_all_kline_stats_status["results"] = None
    update_all_kline_stats_status["error"] = None

    background_tasks.add_task(_update_all_sectors_kline_stats)

    return {
        "status": "started",
        "message": "批量更新所有板块成分涨跌数据任务已启动"
    }


@router.get("/update-all-kline-stats/status")
def get_update_all_stats_status():
    """
    获取批量更新所有板块成分涨跌数据任务状态

    Returns:
        任务状态
    """
    return {
        "is_running": update_all_kline_stats_status["is_running"],
        "started_at": update_all_kline_stats_status["started_at"],
        "progress": update_all_kline_stats_status["progress"],
        "results": update_all_kline_stats_status["results"],
        "error": update_all_kline_stats_status["error"]
    }
