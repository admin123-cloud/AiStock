"""API routes module"""
from fastapi import APIRouter

# 导入各个路由模块
from app.api import kline, sentiment, health, indices, market

# 创建主路由（统一/api前缀）
api_router = APIRouter(prefix="/api")

# 注册子路由（各路由去掉/api前缀，只保留功能名称）
api_router.include_router(health.router)
api_router.include_router(kline.router)
api_router.include_router(sentiment.router)
api_router.include_router(indices.router)
api_router.include_router(market.router)

__all__ = ["api_router"]
