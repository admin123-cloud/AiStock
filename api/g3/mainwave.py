"""G3 主升浪每日选股池 API（实时直连 ClickHouse 最新日线与 30m 分钟线）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from services.g3.mainwave_scanner import scan_mainwave_daily

router = APIRouter()


@router.get("/mainwave")
def get_g3_mainwave(refresh: bool = Query(False, description="是否跳过 120s 缓存强制重算")) -> dict[str, Any]:
    """获取基于 ClickHouse 最新交易日实时计算的 G3 Score88 主升浪候选池。"""
    return scan_mainwave_daily(force_refresh=refresh)
