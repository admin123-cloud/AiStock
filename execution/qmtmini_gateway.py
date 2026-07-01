from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import uuid4

from utils.paths import runtime_path


OrderSide = Literal["buy", "sell"]
OrderStatus = Literal["dry_run", "rejected"]


@dataclass(frozen=True)
class QmtMiniDryRunOrder:
    order_id: str
    created_at: str
    status: OrderStatus
    side: OrderSide
    stock_code: str
    volume: int
    price: float
    price_type: str
    strategy_name: str
    order_remark: str
    reason: str


class QmtMiniOrderGateway:
    """
    Conservative QMT Mini order gateway.

    This class intentionally implements dry-run only. Real order submission must
    be added behind an explicit user-controlled switch after account, cash,
    position, trading time, and risk checks are wired into the live workflow.
    """

    def __init__(self, ledger_path: Optional[Path] = None):
        self.ledger_path = ledger_path or runtime_path("qmtmini", "dry_run_orders.jsonl")

    def _append(self, row: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _normalize_stock_code(stock_code: str) -> str:
        code = str(stock_code or "").strip().upper()
        if not code:
            raise ValueError("stock_code is required")
        if "." not in code and len(code) == 6:
            if code.startswith(("6", "5", "9")):
                return f"{code}.SH"
            return f"{code}.SZ"
        return code

    @staticmethod
    def _validate(side: str, stock_code: str, volume: int, price: float) -> tuple[OrderSide, str, int, float]:
        normalized_side = str(side or "").strip().lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        normalized_code = QmtMiniOrderGateway._normalize_stock_code(stock_code)
        normalized_volume = int(volume)
        if normalized_volume <= 0:
            raise ValueError("volume must be positive")
        if normalized_volume % 100 != 0:
            raise ValueError("A-share dry-run volume must be a multiple of 100")
        normalized_price = float(price)
        if normalized_price <= 0:
            raise ValueError("price must be positive")
        return normalized_side, normalized_code, normalized_volume, normalized_price

    def dry_run_order(
        self,
        side: str,
        stock_code: str,
        volume: int,
        price: float,
        price_type: str = "FIX_PRICE",
        strategy_name: str = "aistock_qmtmini_dry_run",
        order_remark: str = "",
    ) -> dict[str, Any]:
        try:
            normalized_side, normalized_code, normalized_volume, normalized_price = self._validate(
                side, stock_code, volume, price
            )
            order = QmtMiniDryRunOrder(
                order_id=f"DRY-{uuid4().hex[:16]}",
                created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                status="dry_run",
                side=normalized_side,
                stock_code=normalized_code,
                volume=normalized_volume,
                price=normalized_price,
                price_type=str(price_type or "FIX_PRICE"),
                strategy_name=str(strategy_name or ""),
                order_remark=str(order_remark or ""),
                reason="local dry-run only; QMT order_stock was not called",
            )
            row = asdict(order)
            self._append(row)
            return {"ok": True, "order": row, "ledger_path": str(self.ledger_path)}
        except Exception as exc:
            row = {
                "order_id": f"REJ-{uuid4().hex[:16]}",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "status": "rejected",
                "side": str(side or ""),
                "stock_code": str(stock_code or ""),
                "volume": volume,
                "price": price,
                "price_type": str(price_type or ""),
                "strategy_name": str(strategy_name or ""),
                "order_remark": str(order_remark or ""),
                "reason": str(exc),
            }
            self._append(row)
            return {"ok": False, "order": row, "ledger_path": str(self.ledger_path)}

    def submit_real_order(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Real QMT Mini order submission is intentionally disabled in this gateway")
