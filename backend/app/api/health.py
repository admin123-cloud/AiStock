"""
健康检查API
"""
from fastapi import APIRouter
from datetime import datetime

router = APIRouter(prefix="/health", tags=["健康检查"])


@router.get("/check")
async def health_check():
    """健康检查端点"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "message": "StockPy Server is running"
    }


@router.get("/info")
async def get_info():
    """获取服务信息"""
    return {
        "name": "StockPy - 大盘情绪监控系统",
        "version": "0.1.0",
        "description": "集成实时市场数据、技术分析、自动回测和情绪监控的A股量化交易平台",
        "modules": {
            "kline": "K线数据管理",
            "sentiment": "大盘情绪监控",
            "backtest": "回测系统",
            "indicators": "技术指标",
        }
    }
