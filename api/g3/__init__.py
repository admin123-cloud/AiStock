"""G3 第三代交易系统精简 API 包（三周期 × 做T × 主升浪 × 风控契约）。"""
from __future__ import annotations

from fastapi import APIRouter

from api.g3.desk import router as desk_router
from api.g3.review import router as review_router
from api.g3.settings import router as settings_router

router = APIRouter(prefix="/g3", tags=["g3"])
router.include_router(desk_router)
router.include_router(review_router)
router.include_router(settings_router)
