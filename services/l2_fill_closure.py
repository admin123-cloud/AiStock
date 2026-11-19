# -*- coding: utf-8 -*-
"""L2 做T真实成交闭环状态机（#23-#26：fills 事实源 → open_t 开平/核销、正反T区分、账号股票隔离）。

核心不变量（严格遵守 R56「建议 ≠ 成交」）：
1. **唯一事实源**：仅消费真实成交（fills，兼容 THS 远航导出与标准成交字典），
   绝不消费建议草案或无 `fill_id` 的伪造记录。
2. **账号与日期强隔离**：`target_account` 不一致或缺失 `account` 一律拒绝入账
   （`account_mismatch` / `missing_account`）；指定 `trade_date` 时跨日成交一律拒绝。
3. **正T / 反T 严格区分**：
   - 正T（`forward_T`）：`BUY` 通过 `record_fill_open_t` 建立/加仓 `fill_backed=True` 的 `open_t`；
     后续同账号 `SELL` 按真实成交量部分扣减或全量核销清零 `open_t` 并落 `closed_pairs` 账本；
   - 反T（`reverse_T`）：无 `fill_backed` 正T开仓时的 `SELL` 记录到 `reverse_t_open`
     （绝不污染 `open_t`），后续同账号 `BUY` 优先配对结案 `direction="reverse_T"`，
     仅在回补超量时才为净多头部分建立新正T `open_t`。
4. **全量幂等**：同一 `fill_id` 重复导入直接跳过（`idempotent_skipped`），不重复开仓或二次核销。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from services.t_trade_signal import (
    SignalState,
    fmt_ts,
    is_fill_backed_open_t,
    load_state,
    parse_ts,
    record_fill_open_t,
    save_state,
)

_BUY_TOKENS = frozenset({"BUY", "B", "买入", "证券买入", "买", "融资买入"})
_SELL_TOKENS = frozenset({"SELL", "S", "卖出", "证券卖出", "卖", "融券卖出"})


@dataclass(frozen=True)
class FillRecord:
    account: str
    code: str
    side: str          # "BUY" | "SELL"
    qty: int
    px: float
    fill_id: str
    fill_ts: str       # 规范化北京时间戳 "YYYY-MM-DD HH:MM:SS"
    trade_date: str    # "YYYY-MM-DD"
    draft_id: str = ""


def _normalize_side(raw: Any) -> Optional[str]:
    token = str(raw or "").strip().upper()
    if token in _BUY_TOKENS or any(k in token for k in ("买入",)):
        return "BUY"
    if token in _SELL_TOKENS or any(k in token for k in ("卖出",)):
        return "SELL"
    return None


def _extract_ts(raw: Mapping[str, Any]) -> Optional[datetime]:
    for key in ("fill_ts", "ts", "datetime", "trade_time"):
        val = raw.get(key)
        if val:
            parsed = parse_ts(str(val).strip())
            if parsed is not None:
                return parsed
    d = str(raw.get("date") or raw.get("trade_date") or "").strip()
    t = str(raw.get("time") or raw.get("deal_time") or "").strip()
    if d and t:
        d_norm = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d[:10]
        return parse_ts(f"{d_norm} {t}")
    return None


def normalize_fill_record(
    raw: Mapping[str, Any],
    *,
    default_account: Optional[str] = None,
) -> tuple[Optional[FillRecord], Optional[str]]:
    """标准化并 fail-closed 校验单条成交记录；非法返回 ``(None, reason)``。"""
    if not isinstance(raw, Mapping):
        return None, "invalid_record_type"

    account = str(raw.get("account") or default_account or "").strip()
    if not account:
        return None, "missing_account"

    code = str(raw.get("code") or raw.get("symbol") or "").strip().upper()
    if not code:
        return None, "missing_code"

    fill_id = str(raw.get("fill_id") or raw.get("deal_id") or raw.get("id") or "").strip()
    if not fill_id:
        return None, "missing_fill_id"

    side = _normalize_side(raw.get("side") or raw.get("operation") or raw.get("direction"))
    if side is None:
        return None, f"unsupported_side:{raw.get('side') or raw.get('operation')}"

    raw_qty = raw.get("qty") if raw.get("qty") is not None else raw.get("volume")
    try:
        qty = int(round(float(raw_qty)))
    except (TypeError, ValueError):
        return None, "invalid_qty"
    if qty <= 0:
        return None, "non_positive_qty"

    raw_px = (
        raw.get("px")
        if raw.get("px") is not None
        else (raw.get("price") if raw.get("price") is not None else raw.get("avg_price"))
    )
    try:
        px = float(raw_px)
    except (TypeError, ValueError):
        return None, "invalid_px"
    if px <= 0:
        return None, "non_positive_px"

    ts_dt = _extract_ts(raw)
    if ts_dt is None:
        return None, "invalid_fill_ts"

    ts_iso = fmt_ts(ts_dt)
    return (
        FillRecord(
            account=account,
            code=code,
            side=side,
            qty=qty,
            px=px,
            fill_id=fill_id,
            fill_ts=ts_iso,
            trade_date=ts_iso[:10],
            draft_id=str(raw.get("draft_id") or "").strip(),
        ),
        None,
    )


def _closure_bucket(state: SignalState, account: str) -> dict[str, Any]:
    root = state.data.setdefault("fill_closure", {})
    acct_map = root.setdefault(account, {})
    acct_map.setdefault("consumed_fill_ids", [])
    acct_map.setdefault("reverse_t_open", {})
    acct_map.setdefault("closed_pairs", [])
    return acct_map


def _apply_buy_fill(
    state: SignalState,
    bucket: dict[str, Any],
    rec: FillRecord,
) -> dict[str, Any]:
    """处理单笔 BUY 成交：优先结案同账号反T空腿，净多头走 ``record_fill_open_t``。"""
    rev_map: dict[str, Any] = bucket["reverse_t_open"]
    rem_qty = rec.qty
    closed_pair: Optional[dict[str, Any]] = None

    # 1) 若存在同账号同票反T卖出挂腿，先配对结案反T
    rev = rev_map.get(rec.code)
    if isinstance(rev, dict) and int(rev.get("qty") or 0) > 0:
        rev_qty = int(rev["qty"])
        matched = min(rem_qty, rev_qty)
        sell_px = float(rev["sell_px"])
        gross_pnl = round((sell_px - rec.px) * matched, 2)
        closed_pair = {
            "direction": "reverse_T",
            "account": rec.account,
            "code": rec.code,
            "matched_qty": matched,
            "sell_px": sell_px,
            "buy_px": rec.px,
            "gross_pnl": gross_pnl,
            "sell_fill_ids": list(rev.get("sell_fill_ids") or []),
            "buy_fill_ids": [rec.fill_id],
            "closed_at": rec.fill_ts,
        }
        bucket["closed_pairs"].append(closed_pair)
        left_rev = rev_qty - matched
        if left_rev > 0:
            rev["qty"] = left_rev
        else:
            rev_map.pop(rec.code, None)
        rem_qty -= matched

    # 2) 若无剩余多头，反T结案完成，不新建正T open_t
    if rem_qty <= 0:
        return {"action": "reverse_t_closed", "closed_pair": closed_pair}

    # 3) 正T开仓或同账号正T加仓
    existing = state.open_t(rec.code)
    if is_fill_backed_open_t(existing) and str(existing.get("account") or rec.account) == rec.account:
        old_qty = int(existing.get("qty") or 0)
        old_px = float(existing.get("entry_px") or rec.px)
        new_qty = old_qty + rem_qty
        avg_px = round((old_px * old_qty + rec.px * rem_qty) / max(new_qty, 1), 4)
        ot = record_fill_open_t(
            state,
            rec.code,
            draft_id=rec.draft_id or str(existing.get("draft_id") or ""),
            entry_px=avg_px,
            qty=new_qty,
            target_band=existing.get("target_band"),
            stop=existing.get("stop"),
            ts=rec.fill_ts,
            fill_id=rec.fill_id,
            fill_ts=rec.fill_ts,
            account=rec.account,
            fill_side="BUY",
            fill_qty=rem_qty,
            fill_px=rec.px,
        )
        prior_ids = list(existing.get("buy_fill_ids") or [str(existing.get("fill_id") or "")])
        ot["buy_fill_ids"] = [fid for fid in prior_ids if fid] + [rec.fill_id]
        state.set_open_t(rec.code, ot)
        return {"action": "forward_t_added", "open_t": ot, "closed_pair": closed_pair}

    ot = record_fill_open_t(
        state,
        rec.code,
        draft_id=rec.draft_id,
        entry_px=rec.px,
        qty=rem_qty,
        target_band=None,
        stop=None,
        ts=rec.fill_ts,
        fill_id=rec.fill_id,
        fill_ts=rec.fill_ts,
        account=rec.account,
        fill_side="BUY",
        fill_qty=rem_qty,
        fill_px=rec.px,
    )
    ot["buy_fill_ids"] = [rec.fill_id]
    state.set_open_t(rec.code, ot)
    return {"action": "forward_t_opened", "open_t": ot, "closed_pair": closed_pair}


def _apply_sell_fill(
    state: SignalState,
    bucket: dict[str, Any],
    rec: FillRecord,
) -> dict[str, Any]:
    """处理单笔 SELL 成交：同账号 fill-backed 正T核销；无正T则记反T卖腿。"""
    existing = state.open_t(rec.code)
    existing_acct = str(existing.get("account") or "").strip() if isinstance(existing, dict) else ""

    # 1) 同账号且 fill-backed 的正T开仓 → 核销（部分扣减或全量结案）
    if is_fill_backed_open_t(existing) and (not existing_acct or existing_acct == rec.account):
        open_qty = int(existing.get("qty") or 0)
        buy_px = float(existing.get("entry_px") or 0.0)
        buy_ids = list(existing.get("buy_fill_ids") or [str(existing.get("fill_id") or "")])
        matched = min(rec.qty, open_qty)
        gross_pnl = round((rec.px - buy_px) * matched, 2)
        sell_entry = {
            "id": rec.fill_id,
            "side": "SELL",
            "qty": matched,
            "px": rec.px,
            "ts": rec.fill_ts,
        }
        closed_pair = {
            "direction": "forward_T",
            "account": rec.account,
            "code": rec.code,
            "matched_qty": matched,
            "buy_px": buy_px,
            "sell_px": rec.px,
            "gross_pnl": gross_pnl,
            "buy_fill_ids": [fid for fid in buy_ids if fid],
            "sell_fill_ids": [rec.fill_id],
            "closed_at": rec.fill_ts,
        }
        bucket["closed_pairs"].append(closed_pair)

        remaining = open_qty - matched
        if remaining > 0:
            updated = dict(existing)
            updated["qty"] = remaining
            updated["trimmed"] = True
            close_fills = list(updated.get("close_fills") or [])
            close_fills.append(sell_entry)
            updated["close_fills"] = close_fills
            state.set_open_t(rec.code, updated)
            return {"action": "forward_t_trimmed", "closed_pair": closed_pair, "remaining_qty": remaining}

        # 全量核销，清除 open_t
        state.set_open_t(rec.code, None)
        excess = rec.qty - matched
        if excess > 0:
            # 超出正T开仓量的卖出部分按反T卖腿如实记账
            rev_map: dict[str, Any] = bucket["reverse_t_open"]
            rev = rev_map.get(rec.code)
            if isinstance(rev, dict) and int(rev.get("qty") or 0) > 0:
                o_q = int(rev["qty"])
                o_p = float(rev["sell_px"])
                n_q = o_q + excess
                rev["qty"] = n_q
                rev["sell_px"] = round((o_p * o_q + rec.px * excess) / n_q, 4)
                rev["sell_fill_ids"] = list(rev.get("sell_fill_ids") or []) + [rec.fill_id]
            else:
                rev_map[rec.code] = {
                    "account": rec.account,
                    "qty": excess,
                    "sell_px": rec.px,
                    "sell_fill_ids": [rec.fill_id],
                    "ts": rec.fill_ts,
                }
        return {"action": "forward_t_closed", "closed_pair": closed_pair, "excess_sell_qty": excess}

    # 2) 无正T（或旧建议伪 open_t / 他账号 open_t）→ 绝不伪造正T核销，记为反T卖腿
    rev_map = bucket["reverse_t_open"]
    rev = rev_map.get(rec.code)
    if isinstance(rev, dict) and int(rev.get("qty") or 0) > 0:
        o_q = int(rev["qty"])
        o_p = float(rev["sell_px"])
        n_q = o_q + rec.qty
        rev["qty"] = n_q
        rev["sell_px"] = round((o_p * o_q + rec.px * rec.qty) / n_q, 4)
        rev["sell_fill_ids"] = list(rev.get("sell_fill_ids") or []) + [rec.fill_id]
    else:
        rev_map[rec.code] = {
            "account": rec.account,
            "qty": rec.qty,
            "sell_px": rec.px,
            "sell_fill_ids": [rec.fill_id],
            "ts": rec.fill_ts,
        }
    return {"action": "reverse_t_sell_opened"}


def sync_fills_to_signal_state(
    state: SignalState,
    fills: Sequence[Mapping[str, Any]],
    *,
    target_account: str,
    trade_date: Optional[date] = None,
) -> dict[str, Any]:
    """将标准化真实成交按时间序幂等回放到 ``SignalState``（单账号隔离、正反T闭环）。"""
    acct = str(target_account or "").strip()
    if not acct:
        raise ValueError("sync_fills_to_signal_state: target_account 不能为空")

    target_iso = trade_date.isoformat() if trade_date is not None else None
    bucket = _closure_bucket(state, acct)
    consumed: set[str] = set(bucket["consumed_fill_ids"])

    valid_records: list[FillRecord] = []
    rejected: list[dict[str, Any]] = []
    idempotent_skipped = 0

    for raw in fills:
        rec, err = normalize_fill_record(raw)
        if err is not None or rec is None:
            rejected.append({"reason": err or "invalid", "raw": dict(raw) if isinstance(raw, Mapping) else str(raw)})
            continue
        if rec.account != acct:
            rejected.append({"fill_id": rec.fill_id, "code": rec.code, "reason": "account_mismatch"})
            continue
        if target_iso is not None and rec.trade_date != target_iso:
            rejected.append({"fill_id": rec.fill_id, "code": rec.code, "reason": "date_mismatch"})
            continue
        if rec.fill_id in consumed:
            idempotent_skipped += 1
            continue
        valid_records.append(rec)

    # 按成交时刻升序回放，保证正反T因果顺序确定
    valid_records.sort(key=lambda r: (r.fill_ts, r.fill_id))

    applied_buys = 0
    applied_sells = 0
    reverse_t_sells = 0
    actions: list[dict[str, Any]] = []

    for rec in valid_records:
        if rec.fill_id in consumed:
            idempotent_skipped += 1
            continue
        if rec.side == "BUY":
            res = _apply_buy_fill(state, bucket, rec)
            applied_buys += 1
        else:
            res = _apply_sell_fill(state, bucket, rec)
            applied_sells += 1
            if res["action"] == "reverse_t_sell_opened":
                reverse_t_sells += 1
        consumed.add(rec.fill_id)
        bucket["consumed_fill_ids"].append(rec.fill_id)
        actions.append({"fill_id": rec.fill_id, "code": rec.code, "side": rec.side, "action": res["action"]})

    open_codes = sorted(
        code
        for code, cs in state.data.items()
        if code != "fill_closure"
        and isinstance(cs, dict)
        and is_fill_backed_open_t(cs.get("open_t"))
        and (not str((cs.get("open_t") or {}).get("account") or "").strip()
             or str((cs.get("open_t") or {}).get("account") or "").strip() == acct)
    )
    return {
        "account": acct,
        "trade_date": target_iso,
        "applied_buys": applied_buys,
        "applied_sells": applied_sells,
        "reverse_t_sells": reverse_t_sells,
        "idempotent_skipped": idempotent_skipped,
        "rejected": rejected,
        "actions": actions,
        "open_t_codes": open_codes,
        "reverse_t_open_codes": sorted(bucket["reverse_t_open"].keys()),
        "closed_pairs": list(bucket["closed_pairs"]),
    }


def reconcile_account_fills_closure(
    *,
    root: Path,
    for_date: date,
    target_account: str,
    fills: Sequence[Mapping[str, Any]],
    dry_run: bool = True,
) -> dict[str, Any]:
    """端到端单账号成交闭环对账（默认 ``dry_run=True`` 零落盘，``dry_run=False`` 原子保存状态）。"""
    state = load_state(root, for_date)
    report = sync_fills_to_signal_state(
        state,
        fills,
        target_account=target_account,
        trade_date=for_date,
    )
    report["dry_run"] = bool(dry_run)
    if not dry_run:
        save_state(root, for_date, state)
    return report
