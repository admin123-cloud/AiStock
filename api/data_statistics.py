"""
数据缺失统计API

提供数据完整性检查和统计功能
"""

from datetime import datetime, timedelta
from typing import Dict, List, Any
from fastapi import APIRouter, Depends
from sqlalchemy import func, and_, or_, case
from sqlalchemy.sql import select

from utils.database import db
from utils.market_warehouse import clickhouse_available, clickhouse_query_df, clickhouse_scalar
from models.stock_models import Stock, KlineDaily
from utils.logger import get_logger

router = APIRouter(prefix="/data-statistics", tags=["数据统计"])
logger = get_logger("data_statistics")


@router.get("/overview")
def get_data_overview():
    """
    获取数据总览
    
    Returns:
        数据总览信息
    """
    session = next(db.get_session())
    try:
        # 股票总数
        total_stocks = session.query(Stock).filter(
            Stock.type == "stock"
        ).count()
        
        # 按市场统计
        market_stats = session.query(
            Stock.market,
            func.count(Stock.id)
        ).filter(
            Stock.type == "stock"
        ).group_by(Stock.market).all()
        
        market_summary = {market: count for market, count in market_stats}
        
        # 行业统计（前10名）
        industry_stats = session.query(
            Stock.industry,
            func.count(Stock.id)
        ).filter(
            Stock.type == "stock",
            Stock.industry != "",
            Stock.industry.isnot(None)
        ).group_by(Stock.industry).order_by(
            func.count(Stock.id).desc()
        ).limit(10).all()
        
        sector_stats = []
        if hasattr(Stock, "sector"):
            sector_stats = session.query(
                Stock.sector,
                func.count(Stock.id)
            ).filter(
                Stock.type == "stock",
                Stock.sector != "",
                Stock.sector.isnot(None)
            ).group_by(Stock.sector).order_by(
                func.count(Stock.id).desc()
            ).limit(10).all()
        
        return {
            "total_stocks": total_stocks,
            "market_stats": market_summary,
            "top_industries": [
                {"industry": ind, "count": count}
                for ind, count in industry_stats
            ],
            "top_sectors": [
                {"sector": sec, "count": count}
                for sec, count in sector_stats
            ]
        }
        
    finally:
        session.close()


@router.get("/kline-missing")
def get_kline_data_statistics(
    days: int = 30,
    period: str = "daily"
):
    """
    获取K线数据缺失统计
    
    Args:
        days: 统计最近多少天的数据
        period: 周期（daily/weekly/monthly）
    
    Returns:
        K线数据缺失统计
    """
    session = next(db.get_session())
    try:
        # 计算目标日期范围
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        # 应该有的交易日数量（粗略估计，去掉周末）
        expected_days = int(days * 5 / 7)
        
        # 股票总数
        total_stocks = session.query(Stock).filter(
            Stock.type == "stock"
        ).count()

        if period == "daily" and clickhouse_available():
            try:
                start_text = str(start_date)
                end_text = str(end_date)
                stocks_with_kline = int(clickhouse_scalar(
                    """
                    SELECT COUNT(DISTINCT s.code)
                    FROM stocks s
                    JOIN (
                        SELECT DISTINCT substr(code, 1, 6) AS short_code
                        FROM kline_daily
                        WHERE trade_date >= ? AND trade_date <= ?
                    ) k ON substr(s.code, 1, 6) = k.short_code
                    WHERE s.type = 'stock'
                    """,
                    [start_text, end_text],
                ) or 0)
                missing_df = clickhouse_query_df(
                    """
                    SELECT s.code, s.name, s.market
                    FROM stocks s
                    LEFT JOIN (
                        SELECT DISTINCT substr(code, 1, 6) AS short_code
                        FROM kline_daily
                        WHERE trade_date >= ? AND trade_date <= ?
                    ) k ON substr(s.code, 1, 6) = k.short_code
                    WHERE s.type = 'stock' AND k.short_code IS NULL
                    LIMIT 100
                    """,
                    [start_text, end_text],
                )
                missing_stocks = [] if missing_df is None or missing_df.empty else missing_df.to_dict("records")
                missing_count = int(clickhouse_scalar(
                    """
                    SELECT COUNT(*)
                    FROM stocks s
                    LEFT JOIN (
                        SELECT DISTINCT substr(code, 1, 6) AS short_code
                        FROM kline_daily
                        WHERE trade_date >= ? AND trade_date <= ?
                    ) k ON substr(s.code, 1, 6) = k.short_code
                    WHERE s.type = 'stock' AND k.short_code IS NULL
                    """,
                    [start_text, end_text],
                ) or 0)
                stats_df = clickhouse_query_df(
                    """
                    SELECT s.code, s.name, COUNT(k.trade_date) AS kline_count
                    FROM stocks s
                    LEFT JOIN kline_daily k
                      ON substr(s.code, 1, 6) = substr(k.code, 1, 6)
                     AND k.trade_date >= ?
                     AND k.trade_date <= ?
                    WHERE s.type = 'stock'
                    GROUP BY s.code, s.name
                    ORDER BY COUNT(k.trade_date) ASC
                    LIMIT 50
                    """,
                    [start_text, end_text],
                )
                missing_summary = {"no_data": [], "partial_data": []}
                if stats_df is not None and not stats_df.empty:
                    for row in stats_df.itertuples(index=False):
                        kline_count = int(row.kline_count or 0)
                        item = {
                            "code": row.code,
                            "name": row.name,
                            "kline_count": kline_count,
                            "expected": expected_days,
                            "missing_rate": 1 - (kline_count / expected_days) if expected_days > 0 else 0,
                        }
                        if kline_count == 0:
                            item["missing_rate"] = 1.0
                            missing_summary["no_data"].append(item)
                        elif kline_count < expected_days:
                            missing_summary["partial_data"].append(item)
                total_expected = total_stocks * expected_days
                covered_days = int(clickhouse_scalar(
                    """
                    SELECT COUNT(*)
                    FROM stocks s
                    JOIN kline_daily k
                      ON substr(s.code, 1, 6) = substr(k.code, 1, 6)
                    WHERE s.type = 'stock'
                      AND k.trade_date >= ?
                      AND k.trade_date <= ?
                    """,
                    [start_text, end_text],
                ) or 0)
                overall_missing_rate = 1 - (covered_days / total_expected) if total_expected > 0 else 0
                return {
                    "period": period,
                    "days": days,
                    "expected_days": expected_days,
                    "total_stocks": total_stocks,
                    "stocks_with_data": stocks_with_kline,
                    "stocks_without_data": missing_count,
                    "overall_missing_rate": round(overall_missing_rate * 100, 2),
                    "missing_summary": missing_summary,
                    "missing_stocks_sample": missing_stocks,
                    "source": "clickhouse",
                }
            except Exception as exc:
                logger.warning(f"ClickHouse kline statistics fallback to SQLAlchemy engine: {exc}")
        
        # 有K线数据的股票数量（只在股票中统计）
        stocks_with_kline = session.query(
            Stock.code
        ).filter(
            Stock.type == "stock",
            Stock.code.in_(
                session.query(KlineDaily.code).filter(
                    KlineDaily.trade_date >= start_date,
                    KlineDaily.trade_date <= end_date
                ).distinct()
            )
        ).count()
        
        # 无K线数据的股票
        stocks_without_kline = session.query(Stock).filter(
            Stock.type == "stock",
            ~Stock.code.in_(
                session.query(KlineDaily.code).filter(
                    KlineDaily.trade_date >= start_date,
                    KlineDaily.trade_date <= end_date
                ).distinct()
            )
        ).all()
        
        missing_stocks = [
            {"code": s.code, "name": s.name, "market": s.market}
            for s in stocks_without_kline[:100]  # 限制返回数量
        ]
        
        # 按缺失天数统计
        missing_days_stats = session.query(
            Stock.code,
            Stock.name,
            func.count(KlineDaily.trade_date).label('kline_count')
        ).outerjoin(
            KlineDaily,
            and_(
                Stock.code == KlineDaily.code,
                KlineDaily.trade_date >= start_date,
                KlineDaily.trade_date <= end_date
            )
        ).filter(
            Stock.type == "stock"
        ).group_by(
            Stock.code,
            Stock.name
        ).order_by(
            func.count(KlineDaily.trade_date).asc()
        ).limit(50).all()
        
        missing_summary = {
            "no_data": [],  # 完全没有数据
            "partial_data": []  # 数据不全
        }
        
        for code, name, kline_count in missing_days_stats:
            if kline_count == 0:
                missing_summary["no_data"].append({
                    "code": code,
                    "name": name,
                    "kline_count": 0,
                    "expected": expected_days,
                    "missing_rate": 1.0
                })
            elif kline_count < expected_days:
                missing_summary["partial_data"].append({
                    "code": code,
                    "name": name,
                    "kline_count": kline_count,
                    "expected": expected_days,
                    "missing_rate": 1 - (kline_count / expected_days)
                })
        
        # 计算总体缺失率
        total_expected = total_stocks * expected_days
        total_actual = sum(m["kline_count"] for m in 
                        missing_summary["no_data"] + missing_summary["partial_data"])
        overall_missing_rate = 1 - (total_actual / total_expected) if total_expected > 0 else 0
        
        return {
            "period": period,
            "days": days,
            "expected_days": expected_days,
            "total_stocks": total_stocks,
            "stocks_with_data": stocks_with_kline,
            "stocks_without_data": len(stocks_without_kline),
            "overall_missing_rate": round(overall_missing_rate * 100, 2),
            "missing_summary": missing_summary,
            "missing_stocks_sample": missing_stocks
        }
        
    finally:
        session.close()


@router.get("/field-missing")
def get_field_missing_stats():
    """
    获取字段缺失统计
    
    Returns:
        字段缺失统计
    """
    session = next(db.get_session())
    try:
        # 股票总数
        total_stocks = session.query(Stock).filter(
            Stock.type == "stock"
        ).count()
        
        # 行业缺失
        missing_industry = session.query(Stock).filter(
            Stock.type == "stock",
            or_(Stock.industry == "", Stock.industry.is_(None))
        ).count()
        
        missing_sector = 0
        if hasattr(Stock, "sector"):
            missing_sector = session.query(Stock).filter(
                Stock.type == "stock",
                or_(Stock.sector == "", Stock.sector.is_(None))
            ).count()
        
        # 上市日期缺失
        missing_list_date = session.query(Stock).filter(
            Stock.type == "stock",
            Stock.list_date.is_(None)
        ).count()
        
        # 缺失行业字段的股票
        stocks_missing_industry = session.query(Stock).filter(
            Stock.type == "stock",
            or_(Stock.industry == "", Stock.industry.is_(None))
        ).limit(20).all()
        
        return {
            "total_stocks": total_stocks,
            "missing_industry": {
                "count": missing_industry,
                "rate": round(missing_industry / total_stocks * 100, 2) if total_stocks > 0 else 0
            },
            "missing_sector": {
                "count": missing_sector,
                "rate": round(missing_sector / total_stocks * 100, 2) if total_stocks > 0 else 0
            },
            "missing_list_date": {
                "count": missing_list_date,
                "rate": round(missing_list_date / total_stocks * 100, 2) if total_stocks > 0 else 0
            },
            "stocks_missing_industry_sample": [
                {"code": s.code, "name": s.name, "market": s.market}
                for s in stocks_missing_industry
            ]
        }
        
    finally:
        session.close()


@router.get("/completeness")
def get_data_completeness():
    """
    获取数据完整性评分
    
    Returns:
        数据完整性评分和详情
    """
    session = next(db.get_session())
    try:
        total_stocks = session.query(Stock).filter(
            Stock.type == "stock"
        ).count()
        
        if total_stocks == 0:
            return {
                "overall_score": 0,
                "details": {}
            }
        
        # 基础信息完整性
        has_name = session.query(Stock).filter(
            Stock.type == "stock",
            Stock.name != "",
            Stock.name.isnot(None)
        ).count()
        
        has_industry = session.query(Stock).filter(
            Stock.type == "stock",
            Stock.industry != "",
            Stock.industry.isnot(None)
        ).count()
        
        has_sector = total_stocks
        if hasattr(Stock, "sector"):
            has_sector = session.query(Stock).filter(
                Stock.type == "stock",
                Stock.sector != "",
                Stock.sector.isnot(None)
            ).count()
        
        has_list_date = session.query(Stock).filter(
            Stock.type == "stock",
            Stock.list_date.isnot(None)
        ).count()
        
        # K线数据完整性（最近30天）
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=30)
        
        stocks_with_kline = None
        if clickhouse_available():
            try:
                stocks_with_kline = int(clickhouse_scalar(
                    """
                    SELECT COUNT(DISTINCT s.code)
                    FROM stocks s
                    JOIN (
                        SELECT DISTINCT substr(code, 1, 6) AS short_code
                        FROM kline_daily
                        WHERE trade_date >= ? AND trade_date <= ?
                    ) k ON substr(s.code, 1, 6) = k.short_code
                    WHERE s.type = 'stock'
                    """,
                    [str(start_date), str(end_date)],
                ) or 0)
            except Exception as exc:
                logger.warning(f"ClickHouse data completeness fallback to SQLAlchemy engine: {exc}")

        if stocks_with_kline is None:
            stocks_with_kline = session.query(
                Stock.code
            ).filter(
                Stock.type == "stock",
                Stock.code.in_(
                    session.query(KlineDaily.code).filter(
                        KlineDaily.trade_date >= start_date,
                        KlineDaily.trade_date <= end_date
                    ).distinct()
                )
            ).count()
        
        # 计算各项得分
        scores = {
            "basic_info": {
                "name_completeness": round(has_name / total_stocks * 100, 2),
                "industry_completeness": round(has_industry / total_stocks * 100, 2),
                "sector_completeness": round(has_sector / total_stocks * 100, 2),
                "list_date_completeness": round(has_list_date / total_stocks * 100, 2)
            },
            "kline_completeness": {
                "recent_30_days": round(stocks_with_kline / total_stocks * 100, 2)
            }
        }
        
        # 计算总体得分
        basic_score = (
            scores["basic_info"]["name_completeness"] * 0.2 +
            scores["basic_info"]["industry_completeness"] * 0.3 +
            scores["basic_info"]["sector_completeness"] * 0.2 +
            scores["basic_info"]["list_date_completeness"] * 0.3
        )
        
        overall_score = round((basic_score * 0.7 + scores["kline_completeness"]["recent_30_days"] * 0.3), 2)
        
        return {
            "overall_score": overall_score,
            "basic_score": round(basic_score, 2),
            "kline_score": scores["kline_completeness"]["recent_30_days"],
            "details": scores
        }
        
    finally:
        session.close()
