"""
市场统计API路由
提供市场整体统计数据的查询接口
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import date, timedelta
from typing import Optional
import logging

from app.database import get_db
from app.models.models import KlineData
from app.api.indices import calculate_market_summary

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/market", tags=["市场统计"])


@router.get("/today", response_model=dict)
async def get_today_market(db: Session = Depends(get_db)):
    """
    获取今日市场统计数据

    返回格式：
    {
        "date": "2026-03-14",
        "market_summary": {
            "total_stocks": 5183,
            "up_count": 1375,
            "down_count": 3715,
            "flat_count": 93,
            "limit_up_count": 60,
            "limit_down_count": 9,
            "total_amount": 6719103718066
        }
    }
    """
    try:
        today = date.today()

        # 首先尝试今日数据，如果没有则使用最近交易日
        summary = calculate_market_summary(db, today)

        # 如果今日没有数据，查找最近的交易日
        if summary["total_stocks"] == 0:
            # 向前查找最近7天内有数据的日子
            for days in range(1, 8):
                check_date = today - timedelta(days=days)
                summary = calculate_market_summary(db, check_date)
                if summary["total_stocks"] > 0:
                    logger.info(f"使用 {check_date} 的数据作为今日数据")
                    break

        return {
            "date": today,
            "market_summary": summary
        }

    except Exception as e:
        logger.error(f"获取今日市场统计失败：{e}")
        raise HTTPException(status_code=500, detail="获取市场统计失败")


@router.get("/summary", response_model=dict)
async def get_market_summary(
    trade_date: Optional[date] = Query(None, description="交易日期"),
    db: Session = Depends(get_db)
):
    """
    获取指定日期的市场概况

    示例：
    - `/api/market/summary?date=2026-03-14`
    - `/api/market/summary` (默认今日）
    """
    try:
        target_date = trade_date or date.today()

        # 获取指定日期的市场统计
        summary = calculate_market_summary(db, target_date)

        return {
            "date": target_date,
            "market_summary": summary
        }

    except Exception as e:
        logger.error(f"获取市场概况失败：{e}")
        raise HTTPException(status_code=500, detail="获取市场概况失败")


@router.get("/recent", response_model=dict)
async def get_recent_market(
    days: int = Query(default=7, ge=1, le=30, description="查询天数"),
    db: Session = Depends(get_db)
):
    """
    获取最近N天的市场统计

    返回格式：
    {
        "recent_days": [
            {
                "date": "2026-03-14",
                "market_summary": {...}
            },
            ...
        ]
    }
    """
    try:
        end_date = date.today()
        recent_data = []

        for i in range(days):
            check_date = end_date - timedelta(days=i)
            summary = calculate_market_summary(db, check_date)

            # 只包含有数据的日期
            if summary["total_stocks"] > 0:
                recent_data.append({
                    "date": check_date,
                    "market_summary": summary
                })

        return {
            "end_date": end_date,
            "days": days,
            "recent_days": recent_data
        }

    except Exception as e:
        logger.error(f"获取最近市场统计失败：{e}")
        raise HTTPException(status_code=500, detail="获取最近市场统计失败")


@router.get("/blocks", response_model=dict)
async def get_market_blocks(db: Session = Depends(get_db)):
    """
    获取市场板块统计（模拟）

    在实际应用中，这里应该基于股票行业分类计算
    """
    try:
        # 模拟板块数据
        blocks = [
            {
                "name": "金融",
                "stocks_count": 245,
                "up_count": 120,
                "down_count": 115,
                "avg_change": 0.5
            },
            {
                "name": "科技",
                "stocks_count": 520,
                "up_count": 310,
                "down_count": 195,
                "avg_change": 1.2
            },
            {
                "name": "消费",
                "stocks_count": 380,
                "up_count": 200,
                "down_count": 170,
                "avg_change": 0.8
            }
        ]

        return {
            "date": date.today(),
            "blocks": blocks
        }

    except Exception as e:
        logger.error(f"获取市场板块统计失败：{e}")
        raise HTTPException(status_code=500, detail="获取板块统计失败")