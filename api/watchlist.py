"""
Watchlist API.

The backend is ClickHouse-only now, so this module uses the shared
ClickHouse helpers instead of legacy ORM sessions.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from utils.logger import get_logger
from utils.market_warehouse import (
    clickhouse_client,
    clickhouse_table_exists,
    clickhouse_query_df,
)


router = APIRouter(prefix="/watchlist", tags=["watchlist"])
logger = get_logger("watchlist")

DEFAULT_USER_ID = 1
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def normalize_stock_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    if raw.endswith((".SH", ".SZ", ".BJ")):
        return raw
    code6 = raw[:6]
    if len(code6) == 6:
        if code6.startswith("6"):
            return f"{code6}.SH"
        if code6.startswith(("0", "3")):
            return f"{code6}.SZ"
        if code6.startswith(("4", "8")):
            return f"{code6}.BJ"
    return raw


def _ensure_user_stocks_table() -> None:
    if clickhouse_table_exists("user_stocks"):
        return
    clickhouse_client().command(
        """
        CREATE TABLE IF NOT EXISTS user_stocks
        (
            id Int64,
            user_id Int32,
            code String,
            stock_name Nullable(String),
            group_name String,
            is_holding UInt8,
            position_shares Nullable(Int64),
            position_cost Nullable(Decimal(10, 3)),
            notes Nullable(String),
            sort_order Int32,
            created_at DateTime,
            updated_at DateTime
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (user_id, code)
        """
    )


def _stock_row(code: str) -> Optional[Dict[str, Any]]:
    df = clickhouse_query_df(
        """
        SELECT code, name, market, type
        FROM stocks
        WHERE code = ? OR substring(code, 1, 6) = ?
        ORDER BY code
        LIMIT 1
        """,
        [code, code[:6]],
    )
    if df is None or df.empty:
        return None
    return df.iloc[0].to_dict()


def _watchlist_row(user_id: int, code: str) -> Optional[Dict[str, Any]]:
    _ensure_user_stocks_table()
    df = clickhouse_query_df(
        """
        SELECT *
        FROM user_stocks FINAL
        WHERE user_id = ? AND code = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        [user_id, code],
    )
    if df is None or df.empty:
        return None
    return df.iloc[0].to_dict()


def _insert_watchlist_row(row: Dict[str, Any]) -> None:
    _ensure_user_stocks_table()
    clickhouse_client().insert(
        "user_stocks",
        [[
            row["id"],
            row["user_id"],
            row["code"],
            row.get("stock_name"),
            row.get("group_name") or "default",
            1 if row.get("is_holding") else 0,
            row.get("position_shares"),
            row.get("position_cost"),
            row.get("notes"),
            int(row.get("sort_order") or 0),
            row["created_at"],
            row["updated_at"],
        ]],
        column_names=[
            "id",
            "user_id",
            "code",
            "stock_name",
            "group_name",
            "is_holding",
            "position_shares",
            "position_cost",
            "notes",
            "sort_order",
            "created_at",
            "updated_at",
        ],
    )


def _public_item(row: Dict[str, Any]) -> Dict[str, Any]:
    code = str(row.get("code") or "")
    return {
        "id": row.get("id"),
        "code": code,
        "name": row.get("stock_name") or row.get("name") or code,
        "market": code.split(".")[-1].lower() if "." in code else "",
        "industry": "",
        "group": row.get("group_name") or "default",
        "is_holding": bool(row.get("is_holding") or False),
        "position_shares": row.get("position_shares"),
        "position_cost": row.get("position_cost"),
        "notes": row.get("notes"),
    }


@router.get("/")
def get_watchlist(group: Optional[str] = None):
    _ensure_user_stocks_table()
    params = [DEFAULT_USER_ID]
    group_filter = ""
    if group:
        group_filter = "AND group_name = ?"
        params.append(group)
    df = clickhouse_query_df(
        f"""
        SELECT *
        FROM user_stocks FINAL
        WHERE user_id = ?
        {group_filter}
        ORDER BY group_name, sort_order, code
        """,
        params,
    )
    items = (
        []
        if df is None or df.empty
        else [_public_item(row.to_dict()) for _, row in df.iterrows()]
    )
    return {"total": len(items), "items": items}


@router.post("/{code}")
def add_to_watchlist(code: str, group: str = "default"):
    normalized_code = normalize_stock_code(code)
    stock = _stock_row(normalized_code)
    if not stock:
        raise HTTPException(
            status_code=404,
            detail=f"Stock {normalized_code} not found",
        )

    existing = _watchlist_row(DEFAULT_USER_ID, str(stock["code"]))
    if existing:
        return {
            "success": True,
            "message": "already exists",
            "data": _public_item(existing),
        }

    now = datetime.now(BUSINESS_TZ).replace(tzinfo=None)
    row = {
        "id": time.time_ns() // 1000,
        "user_id": DEFAULT_USER_ID,
        "code": str(stock["code"]),
        "stock_name": str(stock.get("name") or stock["code"]),
        "group_name": group or "default",
        "is_holding": 0,
        "position_shares": None,
        "position_cost": None,
        "notes": None,
        "sort_order": 0,
        "created_at": now,
        "updated_at": now,
    }
    _insert_watchlist_row(row)
    logger.info(f"watchlist add: {row['code']} {row['stock_name']}")
    return {
        "success": True,
        "message": "added",
        "data": _public_item(row),
    }


@router.delete("/{code}")
def remove_from_watchlist(code: str):
    normalized_code = normalize_stock_code(code)
    existing = _watchlist_row(DEFAULT_USER_ID, normalized_code)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail="Stock is not in watchlist",
        )
    clickhouse_client().command(
        (
            "ALTER TABLE user_stocks DELETE "
            "WHERE user_id = %(user_id)s AND code = %(code)s"
        ),
        parameters={"user_id": DEFAULT_USER_ID, "code": normalized_code},
    )
    logger.info(f"watchlist remove: {normalized_code}")
    return {"success": True, "message": "removed"}


@router.get("/check/{code}")
def check_in_watchlist(code: str):
    normalized_code = normalize_stock_code(code)
    existing = _watchlist_row(DEFAULT_USER_ID, normalized_code)
    return {
        "in_watchlist": existing is not None,
        "data": _public_item(existing) if existing else None,
    }


@router.put("/{code}")
def update_watchlist_stock(
    code: str,
    group: Optional[str] = None,
    is_holding: Optional[bool] = None,
    position_shares: Optional[int] = None,
    position_cost: Optional[float] = None,
    notes: Optional[str] = None,
):
    normalized_code = normalize_stock_code(code)
    existing = _watchlist_row(DEFAULT_USER_ID, normalized_code)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail="Stock is not in watchlist",
        )

    now = datetime.now(BUSINESS_TZ).replace(tzinfo=None)
    row = {
        **existing,
        "group_name": (
            group if group is not None else existing.get("group_name")
        ),
        "is_holding": (
            is_holding
            if is_holding is not None
            else existing.get("is_holding")
        ),
        "position_shares": (
            position_shares
            if position_shares is not None
            else existing.get("position_shares")
        ),
        "position_cost": (
            position_cost
            if position_cost is not None
            else existing.get("position_cost")
        ),
        "notes": notes if notes is not None else existing.get("notes"),
        "updated_at": now,
    }
    _insert_watchlist_row(row)
    return {"success": True, "message": "updated", "data": _public_item(row)}


@router.get("/groups")
def get_watchlist_groups():
    _ensure_user_stocks_table()
    df = clickhouse_query_df(
        """
        SELECT group_name, count() AS count
        FROM user_stocks FINAL
        WHERE user_id = ?
        GROUP BY group_name
        ORDER BY group_name
        """,
        [DEFAULT_USER_ID],
    )
    groups = [] if df is None or df.empty else [
        {"name": str(row.group_name or "default"), "count": int(row.count)}
        for row in df.itertuples(index=False)
    ]
    return {"groups": groups}
