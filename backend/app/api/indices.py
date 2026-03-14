"""
指数数据API路由
提供上证、深证、科创、创业板等指数数据的查询接口
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import date, timedelta
from typing import List, Optional
import logging

from app.database import get_db
from app.models.models import KlineData
from app.services.data_fetcher import LocalDataFetcher

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/indices", tags=["指数数据"])
data_fetcher = LocalDataFetcher()


# 主要指数代码配置
MAJOR_INDICES = {
    "sh000001": {"name": "上证综指", "market": "SH"},
    "sh000300": {"name": "沪深300", "market": "SH"},
    "sh000688": {"name": "科创50", "market": "SH"},
    "sz399001": {"name": "深证成指", "market": "SZ"},
    "sz399006": {"name": "创业板指", "market": "SZ"},
}


@router.get("/today", response_model=dict)
async def get_today_indices(db: Session = Depends(get_db)):
    """
    获取今日主要指数数据

    返回格式：
    {
        "date": "2026-03-14",
        "indices": {
            "sh000001": {"name": "上证综指", "current": 3050.23, "change": 0.8, ...},
            "sz399001": {"name": "深证成指", "current": 9856.34, "change": -0.5, ...},
            ...
        },
        "market_summary": {
            "up_count": 1375,
            "down_count": 3715,
            "total_amount": 6719103718066
        }
    }
    """
    try:
        today = date.today()
        results = {}

        # 获取各主要指数最新数据
        for code, config in MAJOR_INDICES.items():
            try:
                # 获取该指数最近一条K线数据
                latest = db.query(KlineData).filter(
                    KlineData.code == code,
                    KlineData.period == "D"
                ).order_by(desc(KlineData.date)).first()

                if latest:
                    # 计算涨跌幅（如果有前一日数据）
                    change_percent = latest.change_percent or 0

                    results[code] = {
                        "name": config["name"],
                        "market": config["market"],
                        "date": latest.date,
                        "open_price": latest.open_price,
                        "high_price": latest.high_price,
                        "low_price": latest.low_price,
                        "close_price": latest.close_price,
                        "change_percent": change_percent,
                        "volume": latest.volume,
                        "amount": latest.amount
                    }
                else:
                    logger.warning(f"指数 {code} 无数据")
                    results[code] = None

            except Exception as e:
                logger.error(f"获取指数 {code} 数据失败：{e}")
                results[code] = None

        # 计算市场统计
        market_summary = calculate_market_summary(db, today)

        return {
            "date": today,
            "indices": results,
            "market_summary": market_summary
        }

    except Exception as e:
        logger.error(f"获取今日指数数据失败：{e}")
        raise HTTPException(status_code=500, detail="获取数据失败")


@router.get("/history", response_model=List[dict])
async def get_index_history(
    code: str = Query(..., description="指数代码，如 sh000001"),
    days: int = Query(default=30, ge=1, le=365, description="查询天数"),
    db: Session = Depends(get_db)
):
    """
    获取指数历史数据

    示例：
    - `/api/indices/history?code=sh000001&days=30`
    """
    try:
        # 验证指数代码
        if code not in MAJOR_INDICES:
            raise HTTPException(status_code=400, detail=f"不支持的指数代码：{code}")

        # 计算日期范围
        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        # 查询历史数据
        history = db.query(KlineData).filter(
            KlineData.code == code,
            KlineData.period == "D",
            KlineData.date >= start_date,
            KlineData.date <= end_date
        ).order_by(KlineData.date).all()

        return [
            {
                "code": k.code,
                "date": k.date,
                "open_price": k.open_price,
                "high_price": k.high_price,
                "low_price": k.low_price,
                "close_price": k.close_price,
                "volume": k.volume,
                "amount": k.amount,
                "change_percent": k.change_percent
            }
            for k in history
        ]

    except Exception as e:
        logger.error(f"获取指数历史数据失败：{e}")
        raise HTTPException(status_code=500, detail="获取历史数据失败")


@router.get("/realtime", response_model=dict)
async def get_realtime_indices(db: Session = Depends(get_db)):
    """
    获取实时指数数据（模拟实时）

    在实际生产环境中，这里应该连接实时数据源
    目前使用最新的K线数据模拟
    """
    try:
        results = {}

        for code, config in MAJOR_INDICES.items():
            try:
                # 获取最新数据
                latest = db.query(KlineData).filter(
                    KlineData.code == code,
                    KlineData.period == "D"
                ).order_by(desc(KlineData.date)).first()

                if latest:
                    results[code] = {
                        "name": config["name"],
                        "market": config["market"],
                        "current": latest.close_price,
                        "change": latest.change_percent or 0,
                        "open_price": latest.open_price,
                        "high_price": latest.high_price,
                        "low_price": latest.low_price,
                        "volume": latest.volume,
                        "amount": latest.amount,
                        "update_time": latest.date
                    }
                else:
                    results[code] = None

            except Exception as e:
                logger.error(f"获取实时指数 {code} 数据失败：{e}")
                results[code] = None

        return {
            "timestamp": date.today().isoformat(),
            "indices": results
        }

    except Exception as e:
        logger.error(f"获取实时指数数据失败：{e}")
        raise HTTPException(status_code=500, detail="获取实时数据失败")


def calculate_market_summary(db: Session, trade_date: date) -> dict:
    """
    计算市场统计数据

    基于当前已导入的股票数据计算统计
    """
    try:
        # 获取指定日期的所有K线数据
        klines = db.query(KlineData).filter(
            KlineData.date == trade_date,
            KlineData.period == "D"
        ).all()

        if not klines:
            return {
                "total_stocks": 0,
                "up_count": 0,
                "down_count": 0,
                "flat_count": 0,
                "limit_up_count": 0,
                "limit_down_count": 0,
                "total_amount": 0
            }

        # 统计涨跌数量
        up_count = sum(1 for k in klines if k.change_percent > 0)
        down_count = sum(1 for k in klines if k.change_percent < 0)
        flat_count = sum(1 for k in klines if k.change_percent == 0)

        # 统计涨停跌停（假设±9.9%）
        limit_up_count = sum(1 for k in klines if k.change_percent >= 9.9)
        limit_down_count = sum(1 for k in klines if k.change_percent <= -9.9)

        # 统计成交额
        total_amount = sum(k.amount for k in klines)

        return {
            "date": trade_date,
            "total_stocks": len(klines),
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": flat_count,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "total_amount": total_amount
        }

    except Exception as e:
        logger.error(f"计算市场统计失败：{e}")
        return {
            "date": trade_date,
            "total_stocks": 0,
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "limit_up_count": 0,
            "limit_down_count": 0,
            "total_amount": 0
        }