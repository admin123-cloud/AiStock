# -*- coding: utf-8 -*-
"""AiStock 资金账号管理 API（双账号只读总览、单账号明细、受控只读同步指令入队）。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import broker_accounts as ba

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountSyncRequest(BaseModel):
    accounts: Optional[List[str]] = Field(default=None, description="待同步账号列表（默认全部已注册账号）")
    with_fills: bool = Field(default=True, description="是否同时只读采集历史成交")


@router.get("/overview")
def accounts_overview():
    """只读返回双账号合并资产、分账资金/持仓/成交摘要及宿主机桌面桥心跳状态。"""
    return ba.get_accounts_overview()


@router.get("/{account_id}")
def account_detail(account_id: str):
    """只读返回单账号完整资金、持仓、历史成交及采集证据。"""
    try:
        return ba.get_account_detail(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sync")
def trigger_accounts_sync(body: Optional[AccountSyncRequest] = None):
    """scoped-write：仅向宿主机桌面桥指令队列写入只读采集请求（绝不下单/绝不触碰凭证）。"""
    req = body or AccountSyncRequest()
    try:
        return ba.enqueue_sync_request(accounts=req.accounts, with_fills=req.with_fills)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
