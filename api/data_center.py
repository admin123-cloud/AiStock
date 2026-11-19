"""统一数据中心 API（替代旧 api/kline_check.py 与 api/data_statistics.py）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from services.ops.data_center_service import (
    get_data_overview,
    get_health_matrix,
    inspect_kline,
)

router = APIRouter(prefix="/data-center", tags=["data-center"])


@router.get("/overview")
def api_data_overview(refresh: bool = Query(False)) -> dict[str, Any]:
    return get_data_overview(force_refresh=refresh)


@router.get("/health-matrix")
def api_health_matrix(
    days: int = Query(10, ge=5, le=30),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    return get_health_matrix(days=days, force_refresh=refresh)


@router.get("/kline-inspect")
def api_kline_inspect(
    code: str = Query("000001.SH", min_length=6, max_length=16),
    days: int = Query(10, ge=5, le=30),
) -> dict[str, Any]:
    return inspect_kline(raw_code=code, days=days)
