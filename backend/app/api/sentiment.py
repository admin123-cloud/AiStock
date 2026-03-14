"""
大盘情绪数据API路由
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import date, timedelta
from typing import List, Optional
import logging

from app.database import get_db
from app.models.models import SentimentData, KlineData
from app.models.schemas import SentimentDataResponse
from app.services.sentiment_calculator import SentimentService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sentiment", tags=["大盘情绪"])
sentiment_service = SentimentService()


@router.get("/today", response_model=Optional[SentimentDataResponse])
async def get_today_sentiment(db: Session = Depends(get_db)):
    """获取今日情绪数据"""
    try:
        today = date.today()
        sentiment = db.query(SentimentData).filter(
            SentimentData.date == today
        ).first()
        
        if not sentiment:
            raise HTTPException(status_code=404, detail="今日情绪数据暂未生成")
        
        return SentimentDataResponse.from_orm(sentiment)
    
    except Exception as e:
        logger.error(f"获取今日情绪数据失败：{e}")
        raise HTTPException(status_code=500, detail="获取失败")


@router.get("/history", response_model=List[SentimentDataResponse])
async def get_sentiment_history(
    days: int = Query(default=30, ge=1, le=365, description="查询天数"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    db: Session = Depends(get_db)
):
    """
    查询情绪数据历史
    
    示例：
    - `/api/sentiment/history?days=30` (最近30天)
    - `/api/sentiment/history?start_date=2024-01-01&end_date=2024-12-31`
    """
    try:
        query = db.query(SentimentData)
        
        if start_date and end_date:
            query = query.filter(
                SentimentData.date >= start_date,
                SentimentData.date <= end_date
            )
        else:
            cutoff_date = date.today() - timedelta(days=days)
            query = query.filter(SentimentData.date >= cutoff_date)
        
        sentiments = query.order_by(desc(SentimentData.date)).all()
        
        return [SentimentDataResponse.from_orm(s) for s in sentiments]
    
    except Exception as e:
        logger.error(f"查询情绪历史失败：{e}")
        raise HTTPException(status_code=500, detail="查询失败")


@router.get("/date/{trade_date}", response_model=Optional[SentimentDataResponse])
async def get_sentiment_by_date(
    trade_date: date,
    db: Session = Depends(get_db)
):
    """查询特定交易日的情绪数据"""
    try:
        sentiment = db.query(SentimentData).filter(
            SentimentData.date == trade_date
        ).first()
        
        if not sentiment:
            raise HTTPException(status_code=404, detail="未找到指定日期的情绪数据")
        
        return SentimentDataResponse.from_orm(sentiment)
    
    except Exception as e:
        logger.error(f"查询日期情绪数据失败：{e}")
        raise HTTPException(status_code=500, detail="查询失败")


@router.get("/analysis/trend", response_model=dict)
async def get_sentiment_trend(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db)
):
    """
    获取情绪趋势分析
    
    返回：趋势方向、波动性、平均分数等
    """
    try:
        cutoff_date = date.today() - timedelta(days=days)
        sentiments = db.query(SentimentData).filter(
            SentimentData.date >= cutoff_date
        ).order_by(SentimentData.date).all()
        
        if not sentiments:
            raise HTTPException(status_code=404, detail="数据不足")
        
        trend = sentiment_service.get_sentiment_trend(
            [SentimentDataResponse.from_orm(s).model_dump() for s in sentiments],
            window=5
        )
        
        return {
            'days': days,
            'data_count': len(sentiments),
            'start_date': sentiments[0].date,
            'end_date': sentiments[-1].date,
            **trend
        }
    
    except Exception as e:
        logger.error(f"计算情绪趋势失败：{e}")
        raise HTTPException(status_code=500, detail="计算失败")


@router.get("/analysis/extremes", response_model=dict)
async def get_sentiment_extremes(
    days: int = Query(default=365),
    db: Session = Depends(get_db)
):
    """获取情绪数据的极值"""
    try:
        cutoff_date = date.today() - timedelta(days=days)
        sentiments = db.query(SentimentData).filter(
            SentimentData.date >= cutoff_date
        ).order_by(desc(SentimentData.sentiment_score)).all()
        
        if not sentiments:
            raise HTTPException(status_code=404, detail="数据不足")
        
        scores = [s.sentiment_score for s in sentiments if s.sentiment_score]
        
        return {
            'highest_score': max(scores) if scores else None,
            'highest_date': sentiments[0].date,
            'lowest_score': min(scores) if scores else None,
            'lowest_date': sentiments[-1].date,
            'avg_score': sum(scores) / len(scores) if scores else None,
        }
    
    except Exception as e:
        logger.error(f"获取情绪极值失败：{e}")
        raise HTTPException(status_code=500, detail="查询失败")
