# -*- coding: utf-8 -*-
"""同花顺远航单账号预填单前置门禁（#17 / #28：仅预填、绝不提交/撤单/切号）。

安全铁律（fail-closed，零 GUI/零交易原语）：
1. **绝不提交/撤单**：强制 `mode == "prefill_only"` 且 `allow_submit is False`；
   任何要求真实下单、自动确认或撤单的请求一律拒绝（`submit_forbidden`）。
2. **Owner 活跃让位**：必须提供有效 `guard_snapshot`（`ok is True` 且 `owner_active is False`），
   缺证据或 Owner 活跃一律拒绝（`owner_active_or_guard_invalid`）。
3. **单账号身份强绑定**：必须提供有效 `identity_snapshot`（`account == target_account`
   且 `fingerprint` 非空），严禁跨账号或身份不明时放行，绝不触发自动切号。
4. **A股委托参数与风控上限**：代码规范、方向仅 `BUY`/`SELL`、数量为正且为 100 股整数倍
   （A股标准手数）、价格为正、单笔数量与单笔金额不超过风控上限、草案日期匹配交易日。
5. **草案幂等去重**：已消费的 `draft_id` 拒绝二次放行（`duplicate_draft_id`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional, Sequence

# 覆盖 A 股个股（00/30/60/68/83/87/88/92）与沪深场内 ETF/LOF（15/16/50/51/52/56/58）
_CODE_RE = re.compile(r"^(?:00|30|60|68|83|87|88|92|15|16|50|51|52|56|58)\d{4}(?:\.(?:SH|SZ|BJ))?$")
_ALLOWED_MODES = frozenset({"prefill_only"})
_ALLOWED_SIDES = frozenset({"BUY", "SELL"})


@dataclass(frozen=True)
class PrefillPolicy:
    """预填风控策略上限（默认保守单笔上限）。"""

    max_order_qty: int = 50_000
    max_order_notional: float = 200_000.0
    lot_size: int = 100
    allow_submit: bool = False  # 永远不可开启，仅用于显式断言审计


def evaluate_prefill_gate(
    *,
    draft: Mapping[str, Any],
    target_account: str,
    trade_date: date,
    guard_snapshot: Optional[Mapping[str, Any]],
    identity_snapshot: Optional[Mapping[str, Any]],
    consumed_draft_ids: Sequence[str] = (),
    mode: str = "prefill_only",
    allow_submit: bool = False,
    policy: Optional[PrefillPolicy] = None,
) -> dict[str, Any]:
    """纯函数预填门禁裁决：全部通过返回 `allowed=True` 与只读 `ticket`，否则 fail-closed。"""
    pol = policy or PrefillPolicy()
    reasons: list[str] = []

    # 1) 绝对禁止提交/撤单模式
    norm_mode = str(mode or "").strip().lower()
    if allow_submit or pol.allow_submit or norm_mode not in _ALLOWED_MODES:
        reasons.append("submit_forbidden")

    # 2) Owner 空闲守卫必须明确 ok=True 且 owner_active=False
    if not (
        isinstance(guard_snapshot, Mapping)
        and guard_snapshot.get("ok") is True
        and guard_snapshot.get("owner_active") is False
    ):
        reasons.append("owner_active_or_guard_invalid")

    # 3) 单账号身份强绑定（绝不切号）
    acct = str(target_account or "").strip()
    if not acct:
        reasons.append("missing_target_account")
    if not isinstance(identity_snapshot, Mapping):
        reasons.append("missing_identity_snapshot")
    else:
        ident_acct = str(identity_snapshot.get("account") or "").strip()
        ident_fp = str(identity_snapshot.get("fingerprint") or "").strip()
        if not ident_acct or not ident_fp:
            reasons.append("identity_unreadable")
        elif acct and ident_acct != acct:
            reasons.append("account_mismatch_no_autoswitch")

    # 4) 草案参数与风控上限校核
    if not isinstance(draft, Mapping):
        reasons.append("invalid_draft")
        return _reject(reasons, target_account=acct)

    draft_id = str(draft.get("draft_id") or draft.get("id") or "").strip()
    if not draft_id:
        reasons.append("missing_draft_id")
    elif draft_id in set(consumed_draft_ids):
        reasons.append("duplicate_draft_id")

    draft_acct = str(draft.get("account") or acct).strip()
    if acct and draft_acct != acct:
        reasons.append("draft_account_mismatch")

    draft_date = str(draft.get("trade_date") or draft.get("date") or "").strip()[:10]
    if draft_date != trade_date.isoformat():
        reasons.append("draft_date_mismatch")

    code = str(draft.get("code") or draft.get("symbol") or "").strip().upper()
    if not _CODE_RE.match(code):
        reasons.append("invalid_symbol")

    side = str(draft.get("side") or "").strip().upper()
    if side not in _ALLOWED_SIDES:
        reasons.append("invalid_side")

    qty: Optional[int] = None
    try:
        raw_qty = draft.get("qty") if draft.get("qty") is not None else draft.get("quantity")
        qty_f = float(raw_qty)
        if qty_f.is_integer():
            qty = int(qty_f)
        else:
            reasons.append("non_integer_qty")
    except (TypeError, ValueError):
        reasons.append("invalid_qty")

    if qty is not None:
        if qty <= 0:
            reasons.append("non_positive_qty")
        elif pol.lot_size > 0 and (qty % pol.lot_size != 0):
            reasons.append("qty_not_lot_multiple")
        elif qty > pol.max_order_qty:
            reasons.append("qty_exceeds_limit")

    price: Optional[float] = None
    try:
        raw_px = draft.get("price") if draft.get("price") is not None else draft.get("px")
        price = float(raw_px)
    except (TypeError, ValueError):
        reasons.append("invalid_price")

    if price is not None and price <= 0:
        reasons.append("non_positive_price")

    notional: Optional[float] = None
    if qty is not None and price is not None and qty > 0 and price > 0:
        notional = round(qty * price, 2)
        if notional > pol.max_order_notional:
            reasons.append("notional_exceeds_limit")

    if reasons:
        return _reject(reasons, target_account=acct, draft_id=draft_id)

    return {
        "allowed": True,
        "status": "prefill_allowed",
        "reasons": [],
        "mode": "prefill_only",
        "submitted": False,
        "broker_order_id": None,
        "ticket": {
            "draft_id": draft_id,
            "account": acct,
            "trade_date": trade_date.isoformat(),
            "code": code,
            "side": side,
            "qty": qty,
            "price": price,
            "notional": notional,
            "mode": "prefill_only",
            "submitted": False,
            "broker_order_id": None,
        },
    }


def _reject(
    reasons: list[str],
    *,
    target_account: str,
    draft_id: str = "",
) -> dict[str, Any]:
    return {
        "allowed": False,
        "status": "prefill_rejected_fail_closed",
        "reasons": reasons,
        "mode": "prefill_only",
        "submitted": False,
        "broker_order_id": None,
        "account": target_account,
        "draft_id": draft_id,
        "ticket": None,
    }
