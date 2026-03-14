"""
K线数据API路由
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import date, timedelta
from typing import List, Optional
import logging

from app.database import get_db
from app.models.models import KlineData, Stock
from app.models.schemas import KlineDataResponse, KlineQuery
from app.services.indicator_calculator import IndicatorService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kline", tags=["K线数据"])
indicator_service = IndicatorService()


@router.get("/list", response_model=List[KlineDataResponse])
async def list_klines(
    code: str = Query(..., description="股票代码"),
    period: str = Query(default="D", description="周期：D/W/M"),
    limit: int = Query(default=100, ge=1, le=1000, description="返回数量"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    db: Session = Depends(get_db)
):
    """
    查询K线数据
    
    示例：
    - `/api/kline/list?code=sh000001&period=D&limit=100`
    - `/api/kline/list?code=sz000858&period=W&start_date=2024-01-01&end_date=2024-12-31`
    """
    try:
        query = db.query(KlineData).filter(
            KlineData.code == code,
            KlineData.period == period
        )
        
        if start_date:
            query = query.filter(KlineData.date >= start_date)
        if end_date:
            query = query.filter(KlineData.date <= end_date)
        
        klines = query.order_by(desc(KlineData.date)).limit(limit).all()
        
        if not klines:
            return []
        
        return [KlineDataResponse.from_orm(k) for k in klines]
    
    except Exception as e:
        logger.error(f"查询K线数据失败：{e}")
        raise HTTPException(status_code=500, detail="查询K线数据失败")


@router.get("/{code}/latest", response_model=Optional[KlineDataResponse])
async def get_latest_kline(
    code: str,
    period: str = Query(default="D"),
    db: Session = Depends(get_db)
):
    """获取股票最新K线数据"""
    try:
        kline = db.query(KlineData).filter(
            KlineData.code == code,
            KlineData.period == period
        ).order_by(desc(KlineData.date)).first()
        
        if not kline:
            raise HTTPException(status_code=404, detail="未找到指定数据")
        
        return KlineDataResponse.from_orm(kline)
    
    except Exception as e:
        logger.error(f"获取最新K线失败：{e}")
        raise HTTPException(status_code=500, detail="获取数据失败")


@router.get("/date/{trade_date}", response_model=List[KlineDataResponse])
async def get_klines_by_date(
    trade_date: date,
    period: str = Query(default="D"),
    db: Session = Depends(get_db)
):
    """
    查询特定交易日的所有股票K线
    
    示例：
    - `/api/kline/date/2024-12-31?period=D`
    """
    try:
        klines = db.query(KlineData).filter(
            KlineData.date == trade_date,
            KlineData.period == period
        ).all()
        
        return [KlineDataResponse.from_orm(k) for k in klines]
    
    except Exception as e:
        logger.error(f"查询日期K线失败：{e}")
        raise HTTPException(status_code=500, detail="查询失败")


@router.get("/{code}/statistics", response_model=dict)
async def get_kline_statistics(
    code: str,
    period: str = Query(default="D"),
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db)
):
    """
    获取K线统计数据
    
    返回：最高价、最低价、平均收益率等统计信息
    """
    try:
        cutoff_date = date.today() - timedelta(days=days)
        
        klines = db.query(KlineData).filter(
            KlineData.code == code,
            KlineData.period == period,
            KlineData.date >= cutoff_date
        ).order_by(KlineData.date).all()
        
        if not klines:
            raise HTTPException(status_code=404, detail="未找到足够的数据")
        
        close_prices = [k.close_price for k in klines if k.close_price]
        changes = [k.change_percent for k in klines if k.change_percent]
        
        stats = {
            'code': code,
            'period': period,
            'count': len(klines),
            'start_date': klines[0].date,
            'end_date': klines[-1].date,
            'highest_close': max(close_prices) if close_prices else None,
            'lowest_close': min(close_prices) if close_prices else None,
            'avg_close': sum(close_prices) / len(close_prices) if close_prices else None,
            'highest_change': max(changes) if changes else None,
            'lowest_change': min(changes) if changes else None,
            'avg_change': sum(changes) / len(changes) if changes else None,
            'total_volume': sum(k.volume for k in klines),
            'total_amount': sum(k.amount for k in klines),
        }
        
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in stats.items()}
    
    except Exception as e:
        logger.error(f"计算统计数据失败：{e}")
        raise HTTPException(status_code=500, detail="计算失败")
