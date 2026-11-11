# -*- coding: utf-8 -*-
"""R38c broker_state 成交自动同步引擎（CLI 见 scripts/broker_state_sync.py）。

口径（与 R38b 手工刷新一致，COORDINATION_R38B_BROKER_STATE_FILLS_20261110.md）：
  * 股数：锚点持仓 + 已应用当日 fills 顺序重放（余额链；任意时点卖出 > 持仓
    即透卖，fail-closed 拒绝）；
  * cost_price：移动加权（THS 摊薄成本延续——卖出不改成本；买入
    (cost*shares + amount + fee_est)/new_shares），费用沿用 t_trade_signal._fee_estimate
    （买 max(5, amount*0.000186)、卖 amount*0.000666，R38b 手工 5.07 同源）；
  * available_cash：锚点现金 + 已应用 fills 现金流水（买 −(amount+fee)、卖
    +(amount−fee)），available_cash_derived=true；
  * FIFO 仅为参考口径：当日未平买入腿记 fills_sync.fifo_reference，不进 cost_price。

幂等：broker_state.fills_sync = 固定锚点(base, base_as_of) + 已应用成交(applied)。
持仓永远由「锚点 + applied 重放」纯函数重算——同日重跑、导出器 15:05 重产
全日文件、甚至 broker_state 持仓被手工改过，都不会二次计入或信任脏值；
当日无新成交 → 完全不写（零字节改动）。

fail-closed：fills 缺失/status!=ok/解析失败/坏行/透卖/锚点缺失/state 不可读
→ 零改动 + 告警追加 runtime/t_tracker/broker_state_sync_alerts.jsonl + rc=1。
手工改仓后无新 fills 时重跑 → noop 不覆盖，仅记 state_drift 告警（rc=0）。

零交易、零 formal 表写；只写 broker_state.json 一个运行时文件（写前备份）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from collections import deque
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Optional

from services.t_trade_signal import (  # noqa: F401
    BEIJING_TZ, _fee_estimate, fmt_ts, normalize_fill,
)

SCHEMA = "broker_fills_sync/1"
ALERT_SCHEMA = "broker_state_sync_alert/1"
REPORT_SCHEMA = "broker_state_sync/1"
HOLDING_SOURCE = "ths_fills_autosync"
HARD_STOP_MULT = Decimal("0.88")     # G3 合同：成本 ×0.88 硬止损
TAKE_PROFIT_MULT = Decimal("1.12")   # G3 合同：成本 ×1.12 先减半
ALERTS_FILENAME = "broker_state_sync_alerts.jsonl"
REPORT_DIRNAME = "broker_state_sync"

logger = logging.getLogger("broker_state_autosync")


class SyncRefusal(Exception):
    """fail-closed 拒绝：零改动 + 告警。reason 为稳定枚举，便于归类。"""

    def __init__(self, reason: str, detail: Optional[Mapping[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _q2(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _q3(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _q4(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def fingerprint(fill: Mapping[str, Any]) -> str:
    """成交指纹：ts|side|code|shares|price（去重的唯一键）。"""
    return "{}|{}|{}|{}|{:g}".format(
        fill["ts"], fill["side"], fill["code"], int(fill["shares"]),
        float(fill["price"]))


def compact_fill(fill: Mapping[str, Any]) -> dict:
    """存储进 fills_sync.applied 的最小成交对象（重放所需全部字段）。"""
    return {"ts": fill["ts"], "side": fill["side"], "code": fill["code"],
            "shares": int(fill["shares"]), "price": float(fill["price"]),
            "amount": float(fill["amount"]), "name": fill.get("name") or ""}


# ---------------------------------------------------------------------------
# fills 装载与校验
# ---------------------------------------------------------------------------

def load_fills_payload(fills_file: Path) -> dict:
    """读 fills 文件；缺失/坏 JSON/status!=ok/fills 非列表 → SyncRefusal。"""
    if not fills_file.is_file():
        raise SyncRefusal("fills_missing", {"fills_file": str(fills_file)})
    try:
        payload = json.loads(fills_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncRefusal("fills_parse_error",
                          {"fills_file": str(fills_file), "error": str(exc)}) from exc
    if not isinstance(payload, dict):
        raise SyncRefusal("fills_parse_error", {"fills_file": str(fills_file),
                                                "error": "payload not an object"})
    if payload.get("status") != "ok":
        raise SyncRefusal("fills_unavailable",
                          {"status": payload.get("status"),
                           "fills_file": fills_file.name})
    if not isinstance(payload.get("fills"), list):
        raise SyncRefusal("fills_not_list", {"fills_file": fills_file.name})
    return payload


def normalize_fills_rows(rows: list) -> list[dict]:
    """逐行 normalize；任何一行解析不出（价/量/方向/时间缺）→ 整体拒绝。"""
    out: list[dict] = []
    for idx, row in enumerate(rows):
        src = row if isinstance(row, Mapping) else {}
        fill = normalize_fill(src)
        if fill is None:
            raise SyncRefusal("fills_row_invalid",
                              {"index": idx, "row": str(row)[:200]})
        fill["name"] = str(src.get("name") or "")
        out.append(fill)
    out.sort(key=lambda f: (f["ts_dt"], f["code"]))
    return out


# ---------------------------------------------------------------------------
# 重放（纯函数）
# ---------------------------------------------------------------------------

def replay(base: Mapping[str, Mapping[str, Any]], fills: list[dict]) -> dict:
    """锚点持仓 + fills 顺序重放 → 期末持仓 / 现金流水 / FIFO 参考腿。

    移动加权（THS 摊薄成本延续）：卖出只减股数不改成本；买入摊入金额+费用。
    余额链任意时点卖出 > 持仓 → SyncRefusal("oversell")。
    """
    pos: dict[str, dict] = {}
    for code, v in base.items():
        shares = int(v["shares"])
        cost = v.get("cost_price")
        pos[code] = {"shares": shares,
                     "cost": _d(cost) if (shares > 0 and cost is not None) else None}
    cash_delta = Decimal("0")
    fifo: dict[str, deque] = {code: deque() for code in pos}
    chain: list[dict] = []
    for fill in sorted(fills, key=lambda f: (f["ts"], f["code"])):
        code, side, qty = fill["code"], fill["side"], int(fill["shares"])
        amount = _d(fill["amount"])
        fee = _d(_fee_estimate(side, float(fill["amount"])))
        p = pos.setdefault(code, {"shares": 0, "cost": None})
        fifo.setdefault(code, deque())
        if side == "BUY":
            held = p["shares"]
            base_value = p["cost"] * held if (held > 0 and p["cost"] is not None) \
                else Decimal("0")
            p["shares"] = held + qty
            p["cost"] = (base_value + amount + fee) / p["shares"]
            cash_delta -= amount + fee
            fifo[code].append({"ts": fill["ts"], "price": float(fill["price"]),
                               "shares": qty, "fee_est": float(fee)})
        else:
            if p["shares"] < qty:
                raise SyncRefusal("oversell", {
                    "code": code, "ts": fill["ts"], "held_before": p["shares"],
                    "sell_qty": qty})
            p["shares"] -= qty
            cash_delta += amount - fee
            need = qty
            while need > 0 and fifo[code]:
                leg = fifo[code][0]
                take = min(need, leg["shares"])
                leg["shares"] -= take
                need -= take
                if leg["shares"] <= 0:
                    fifo[code].popleft()
        chain.append({"ts": fill["ts"], "code": code, "side": side, "qty": qty,
                      "balance_after": p["shares"]})
    return {"positions": pos, "cash_delta": cash_delta,
            "fifo_open": {c: list(legs) for c, legs in fifo.items() if legs},
            "balance_chain": chain}


# ---------------------------------------------------------------------------
# 派生新 state（display 层）
# ---------------------------------------------------------------------------

def _rebuild_holding_row(code: str, old: Optional[dict],
                         position: Mapping[str, Any], price: float,
                         exact_cost: Decimal) -> dict:
    shares = int(position["shares"])
    mv = _d(price) * shares
    row = dict(old) if old else {"code": code, "name": "", "source": HOLDING_SOURCE}
    row["code"] = code
    row["shares"] = shares
    row["available_shares"] = shares
    row["cost_price"] = _q3(exact_cost)
    row["entry_price"] = _q3(exact_cost)
    row["hard_stop"] = _q3(exact_cost * HARD_STOP_MULT)
    row["take_profit_1"] = _q3(exact_cost * TAKE_PROFIT_MULT)
    row["current_price"] = price
    row["market_value"] = _q2(mv)
    row["source"] = HOLDING_SOURCE
    if "reference_close" not in row:
        row["reference_close"] = price
    return row


def derive_state(state: dict, replayed: Mapping[str, Any], *,
                 pending_codes: set[str],
                 pending_meta: Mapping[str, Mapping[str, Any]],
                 cash_available: Optional[Decimal], now: datetime,
                 sync_note: str) -> tuple[dict, dict]:
    """由重放结果派生新 broker_state（保留结构键/非派生键）+ diff 摘要。"""
    positions = replayed["positions"]
    old_by_code = {str(h.get("code")): h for h in state.get("holdings") or []}
    order = [c for c in old_by_code if c in positions or c not in pending_codes]
    order += sorted(c for c in positions if c not in old_by_code)

    rows, exact_costs, zeroed = [], {}, []
    for code in order:
        position = positions.get(code)
        if code in pending_codes:
            if position is None or position["shares"] <= 0:
                zeroed.append(code)
                continue
            old = old_by_code.get(code)
            if old is not None and old.get("current_price"):
                price = float(old["current_price"])
            else:
                price = float((pending_meta.get(code) or {}).get("price") or 0.0)
            exact_costs[code] = position["cost"]
            rows.append(_rebuild_holding_row(code, old, position, price,
                                             position["cost"]))
            if old is None and (pending_meta.get(code) or {}).get("name"):
                rows[-1]["name"] = pending_meta[code]["name"]
        elif code in old_by_code:
            rows.append(dict(old_by_code[code]))
    mv_total = sum((_d(r["market_value"]) for r in rows), Decimal("0"))
    cap = dict(state.get("capital") or {})
    available = cash_available if cash_available is not None \
        else _d(cap.get("available_cash") or 0)
    frozen = _d(cap.get("frozen_cash") or 0)
    total = available + frozen + mv_total

    for row in rows:
        if total > 0:
            row["position_pct"] = _q4(_d(row["market_value"]) / total)
        code = str(row["code"])
        exact = exact_costs.get(code)
        cost_ref = exact if exact is not None else _d(row["cost_price"])
        row["pnl_ratio"] = _q2(
            (_d(row["current_price"]) - cost_ref) / cost_ref * 100)

    cap["market_value"] = _q2(mv_total)
    cap["holding_market_value"] = _q2(mv_total)
    if cash_available is not None:
        cap["available_cash"] = _q2(cash_available)
        cap["available_cash_derived"] = True
    cap["total_capital"] = _q2(total)
    cap["available_with_holdings"] = _q2(available + mv_total)
    cap["holding_pnl"] = _q2(sum(
        (_d(r["current_price"])
         - (exact_costs.get(str(r["code"]))
            if exact_costs.get(str(r["code"])) is not None
            else _d(r["cost_price"]))) * r["shares"]
        for r in rows))

    new_state = dict(state)  # 保留原键序；以下键原位更新
    new_state["holdings"] = rows
    new_state["capital"] = cap
    new_state["holding_source"] = HOLDING_SOURCE
    new_state["updated_at"] = fmt_ts(now)
    new_state["sync_message"] = sync_note
    diff = {
        "holdings": {
            str(r["code"]): {k: [(old_by_code.get(str(r["code"])) or {}).get(k),
                                 r.get(k)]
                             for k in ("shares", "cost_price", "market_value",
                                       "pnl_ratio", "position_pct", "source")}
            for r in rows if str(r["code"]) in pending_codes},
        "zeroed": zeroed,
        "capital": {k: [(state.get("capital") or {}).get(k), cap.get(k)]
                    for k in ("available_cash", "market_value", "total_capital",
                              "holding_pnl")},
    }
    return new_state, diff


# ---------------------------------------------------------------------------
# 锚点校验 / init
# ---------------------------------------------------------------------------

def validate_init_base(init: Mapping[str, Any], for_date: date) -> dict:
    if not isinstance(init, dict):
        raise SyncRefusal("init_invalid", {"error": "not an object"})
    as_of = init.get("base_as_of")
    try:
        base_as_of = date.fromisoformat(str(as_of))
    except (TypeError, ValueError) as exc:
        raise SyncRefusal("init_invalid", {"base_as_of": as_of}) from exc
    if base_as_of > for_date:
        raise SyncRefusal("init_invalid",
                          {"error": "base_as_of after sync date",
                           "base_as_of": str(base_as_of)})
    base = init.get("base")
    if not isinstance(base, dict) or not base:
        raise SyncRefusal("init_invalid", {"error": "base empty"})
    clean = {}
    for code, v in base.items():
        if not isinstance(v, Mapping):
            raise SyncRefusal("init_invalid", {"code": str(code)})
        shares = v.get("shares")
        cost = v.get("cost_price")
        if not isinstance(shares, int) or isinstance(shares, bool) or shares < 0:
            raise SyncRefusal("init_invalid", {"code": str(code), "shares": shares})
        if shares > 0 and not isinstance(cost, (int, float)):
            raise SyncRefusal("init_invalid", {"code": str(code), "cost": cost})
        clean[str(code)] = {"shares": shares,
                            "cost_price": float(cost) if cost is not None else None}
    cash = init.get("base_cash_available")
    if cash is not None and not isinstance(cash, (int, float)):
        raise SyncRefusal("init_invalid", {"base_cash_available": cash})
    block = {"schema": SCHEMA, "base_as_of": str(base_as_of), "base": clean,
             "base_cash_available": float(cash) if cash is not None else None,
             "applied": [], "cash_delta": 0.0}
    # R49：首次接入当日无成交时的物化辅助（价格/名称，可选）——
    # derive_state 的行重建需要参考价（无 fills 的锚点基线路径）。
    meta = init.get("base_meta")
    if isinstance(meta, Mapping):
        clean_meta = {}
        for code, v in meta.items():
            if not isinstance(v, Mapping):
                continue
            price = v.get("price")
            if price is not None and not isinstance(price, (int, float)):
                raise SyncRefusal("init_invalid",
                                  {"base_meta_price": {str(code): price}})
            clean_meta[str(code)] = {
                "price": float(price) if price is not None else None,
                "name": str(v.get("name") or "")}
        block["base_meta"] = clean_meta
    if isinstance(init.get("provenance"), Mapping):
        block["provenance"] = dict(init["provenance"])
    return block


# ---------------------------------------------------------------------------
# 主流程（fail-closed 编排；唯一写入点在 pending>0 且非 dry_run）
# ---------------------------------------------------------------------------

def _append_alert(alerts_file: Path, *, now: datetime, for_date: date,
                  reason: str, detail: Mapping[str, Any],
                  fills_name: Optional[str]) -> None:
    alerts_file.parent.mkdir(parents=True, exist_ok=True)
    rec = {"schema": ALERT_SCHEMA, "at": fmt_ts(now), "date": for_date.isoformat(),
           "reason": reason, "detail": dict(detail), "fills_file": fills_name}
    with alerts_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _atomic_write_json(path: Path, payload: Mapping[str, Any], indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=indent,
                              default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_state(state_file: Path) -> dict:
    if not state_file.is_file():
        raise SyncRefusal("broker_state_unreadable",
                          {"state_file": str(state_file), "error": "missing"})
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncRefusal("broker_state_unreadable",
                          {"state_file": str(state_file), "error": str(exc)}) from exc
    if not isinstance(state, dict) or not isinstance(state.get("holdings"), list):
        raise SyncRefusal("broker_state_unreadable",
                          {"state_file": str(state_file), "error": "no holdings list"})
    return state


# ── R47 多账号：accounts 包裹 + legacy 顶层镜像 ───────────────

ACCOUNTS_KEY = "accounts"
ACTIVE_VIEW_SCHEMA = "broker_state_accounts/1"


def read_full_state(state_file: Path, account: str = "acct1") -> dict:
    """读整份 broker_state（允许多账号包裹形态）。缺失：acct1=拒绝（legacy 语义），
    其他账号={}（未接入，由调用方决定跳过）。"""
    if not state_file.is_file():
        if account == "acct1":
            raise SyncRefusal("broker_state_unreadable",
                              {"state_file": str(state_file), "error": "missing"})
        return {}
    try:
        full = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncRefusal("broker_state_unreadable",
                          {"state_file": str(state_file), "error": str(exc)}) from exc
    return full if isinstance(full, dict) else {}


def extract_account_state(full: Mapping[str, Any], account: str = "acct1") -> dict:
    """整份 state → 指定账号切片。

    acct1 权威=顶层镜像（legacy 手工编辑/漂移检测语义原样保留）；
    accounts.acct1 只是结构对称副本（每次写入时刷新，消费者一律走本函数）。
    其他账号=accounts.<acctN>。legacy（无 accounts 键）acct1=整份。
    """
    if not isinstance(full, Mapping):
        return {}
    if account == "acct1":
        if isinstance(full.get("holdings"), list):
            return {k: v for k, v in full.items()
                    if k not in {ACCOUNTS_KEY, "active_view"}}
        accounts = full.get(ACCOUNTS_KEY)
        if isinstance(accounts, Mapping) and isinstance(accounts.get("acct1"), dict):
            return dict(accounts["acct1"])
        return {}
    accounts = full.get(ACCOUNTS_KEY)
    if isinstance(accounts, Mapping) and isinstance(accounts.get(account), dict):
        return dict(accounts[account])
    return {}


def compose_account_state(full: Mapping[str, Any], account_state: Mapping[str, Any],
                          account: str, now: Optional[datetime] = None) -> dict:
    """账号切片 → 整份 state。acct1=顶层镜像（legacy 消费者零改动）；
    其他账号=accounts.<acctN> 原位更新，顶层不动。附 active_view 合并视图。"""
    full = dict(full or {})
    accounts = {k: v for k, v in (full.get(ACCOUNTS_KEY) or {}).items()
                if isinstance(v, dict)}
    accounts[account] = dict(account_state)
    if account == "acct1":
        merged = dict(account_state)
        merged[ACCOUNTS_KEY] = accounts
    else:
        if ACCOUNTS_KEY not in full and isinstance(full.get("holdings"), list):
            accounts.setdefault(
                "acct1", {k: v for k, v in full.items()
                          if k not in {ACCOUNTS_KEY, "active_view"}})
        merged = dict(full)
        merged[ACCOUNTS_KEY] = accounts
    merged["active_view"] = {
        "schema": ACTIVE_VIEW_SCHEMA,
        "accounts": sorted(accounts),
        "generated_at": fmt_ts(now) if now is not None
        else str(account_state.get("updated_at") or ""),
        "note": "顶层字段=acct1 镜像（legacy 兼容）；各账号完整态见 accounts.<acctN>",
    }
    return merged


def should_attempt_account(*, full_state: Mapping[str, Any],
                           fills_file: Optional[Path], account: str) -> bool:
    """是否对该账号执行同步。acct1 恒真；其他账号=fills 在场或已有历史切片
    （已接入账号缺 fills 必须 fail-closed 走拒绝，不能静默跳过）。"""
    if account == "acct1":
        return True
    if fills_file is not None and fills_file.is_file():
        return True
    accounts = full_state.get(ACCOUNTS_KEY) if isinstance(full_state, Mapping) else None
    return isinstance(accounts, Mapping) and isinstance(accounts.get(account), dict)


def iter_account_states(full_state: Mapping[str, Any]) -> list[tuple[str, dict]]:
    """[(account, slice)]，acct1 在前（顶层镜像权威），其余按 accounts 键序。
    仅返回有 holdings 列表的账号切片。"""
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    acct1 = extract_account_state(full_state, "acct1")
    if isinstance(acct1.get("holdings"), list):
        out.append(("acct1", acct1))
        seen.add("acct1")
    accounts = full_state.get(ACCOUNTS_KEY) if isinstance(full_state, Mapping) else None
    if isinstance(accounts, Mapping):
        for account in sorted(accounts):
            if account in seen:
                continue
            slice_ = extract_account_state(full_state, account)
            if isinstance(slice_.get("holdings"), list):
                out.append((account, slice_))
    return out


def union_holdings(full_state: Mapping[str, Any]) -> list[dict]:
    """多账号持仓并集（引擎票池/观察名单合并共用口径）。

    每码一行：shares=各账号之和（仅>0），name 取首个非空；附 account_detail
    {acct: shares} 与 accounts 持有账号列表。legacy 单账号=原行透传（加默认
    account_detail）。"""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for account, slice_ in iter_account_states(full_state):
        for row in slice_.get("holdings") or []:
            if not isinstance(row, Mapping):
                continue
            code = str(row.get("code") or "").strip()
            shares = int(row.get("shares") or 0)
            if not code or shares <= 0:
                continue
            if code not in merged:
                merged[code] = {
                    "code": code, "name": str(row.get("name") or "").strip(),
                    "shares": 0, "account_detail": {},
                    "current_price": row.get("current_price"),
                    "cost_price": row.get("cost_price"),
                }
                order.append(code)
            entry = merged[code]
            entry["shares"] += shares
            entry["account_detail"][account] = shares
            if not entry["name"] and row.get("name"):
                entry["name"] = str(row["name"]).strip()
    for code in order:
        entry = merged[code]
        entry["accounts"] = sorted(entry["account_detail"])
    return [merged[c] for c in order]


def _backup(state_file: Path, now: datetime) -> Path:
    bak = state_file.with_name(
        "{}.bak_r38c_{}".format(state_file.name, now.strftime("%Y%m%d_%H%M%S")))
    shutil.copyfile(state_file, bak)
    return bak


def run_sync(*, state_file: Path, fills_file: Optional[Path], alerts_file: Path,
             report_file: Path, history_file: Path, for_date: date, now: datetime,
             init_base: Optional[Mapping[str, Any]] = None,
             dry_run: bool = False, account: str = "acct1") -> dict:
    """一次同步（单账号）：返回 {"rc", "action", ...}。拒绝 → rc=1 + 告警 + 零改动。

    R47：account 切片语义——state 文件可为多账号包裹（accounts.<acctN>）；
    acct1 写顶层镜像（legacy 消费者零改动），其他账号只动 accounts.<acctN>。
    """
    fills_name = fills_file.name if fills_file else None
    result: dict = {"schema": REPORT_SCHEMA, "at": fmt_ts(now),
                    "date": for_date.isoformat(), "account": account,
                    "state_file": str(state_file)}
    try:
        # 1) fills 文件（缺失/unavailable/坏 JSON/坏行 → 拒绝）
        if fills_file is None:
            raise SyncRefusal("fills_missing", {})
        fills = normalize_fills_rows(load_fills_payload(fills_file)["fills"])

        # 2) broker_state（账号切片）
        state = extract_account_state(read_full_state(state_file, account), account)

        # 3) 锚点：已有 fills_sync 用之；否则必须 --init-base（且不与现存冲突）
        sync = state.get("fills_sync")
        if isinstance(sync, dict) and init_base is not None:
            raise SyncRefusal("init_conflict",
                              {"error": "fills_sync already present"})
        if not isinstance(sync, dict):
            if init_base is None:
                raise SyncRefusal("base_missing",
                                  {"hint": "首次接入需 --init-base 提供锚点"})
            sync = validate_init_base(init_base, for_date)
            result["inited"] = True
        base = sync["base"]
        applied = list(sync.get("applied") or [])
        base_cash = sync.get("base_cash_available")
        if base_cash is not None and not isinstance(base_cash, (int, float)):
            raise SyncRefusal("base_invalid", {"base_cash_available": base_cash})

        # 4) 当日 pending = 今日成交 − 已应用（指纹去重）
        day_prefix = for_date.isoformat()
        seen = {fingerprint(f) for f in applied}
        pending: list[dict] = []
        for fill in [f for f in fills if f["ts"].startswith(day_prefix)]:
            fp = fingerprint(fill)
            if fp in seen:
                continue
            seen.add(fp)
            pending.append(fill)

        if not pending and not result.get("inited"):
            # 无新成交且非首次接入：幂等 noop，完全不写；与锚点重放不一致 → 漂移告警
            # （R49：首次接入当日无成交仍须落锚点基线 → 不走 noop，继续重放写盘）
            drift = _drift_check(state, base, applied) if not result.get("inited") \
                else {}
            result.update({"rc": 0, "action": "noop", "pending": 0,
                           "drift": drift})
            if drift:
                _append_alert(alerts_file, now=now, for_date=for_date,
                              reason="state_drift", detail=drift,
                              fills_name=fills_name)
            _write_report(report_file, history_file, result)
            return result

        # 5) 锚点 + 全量 applied 重放（不信任当前持仓显示值）
        new_applied = applied + [compact_fill(f) for f in pending]
        full = replay(base, new_applied)
        cash_anchor = _d(base_cash) if base_cash is not None else None
        cash_available = (cash_anchor + full["cash_delta"]) \
            if cash_anchor is not None else None
        pending_codes = {f["code"] for f in pending}
        pending_meta = {f["code"]: {"price": f["price"], "name": f.get("name") or ""}
                        for f in pending}
        if result.get("inited") and not pending:
            # R49：首次接入当日无成交 → 锚点持仓全量物化为基线行
            # （derive_state 只重建 pending_codes 行，这里把锚点码并入），
            # 价格/名称取 init_base.base_meta（首读快照现价），缺省 0/空。
            base_meta = sync.get("base_meta") if isinstance(sync, Mapping) else {}
            base_meta = base_meta if isinstance(base_meta, Mapping) else {}
            pending_codes = set(str(c) for c in base)
            pending_meta = {str(c): {
                "price": float((base_meta.get(c) or {}).get("price") or 0.0),
                "name": str((base_meta.get(c) or {}).get("name") or "")}
                for c in base}
        note = ("R38c autosync：锚点 {} + 当日 {} 笔重放（移动加权/THS 摊薄延续；"
                "费率 buy max(5,0.0186%)/sell 0.0666% 估）；现金按流水推导；"
                "FIFO 参考腿见 fills_sync.fifo_reference。零交易零 formal。".format(
                    sync.get("base_as_of"), len(pending)))
        new_state, diff = derive_state(
            state, full, pending_codes=pending_codes, pending_meta=pending_meta,
            cash_available=cash_available, now=now, sync_note=note)

        fills_sync = dict(sync)
        fills_sync.update({
            "applied": new_applied,
            "cash_delta": _q2(full["cash_delta"]),
            "fifo_reference": full["fifo_open"],
            "zeroed": sorted(c for c, p in full["positions"].items()
                             if p["shares"] <= 0),
            "last_sync": {"date": day_prefix, "at": fmt_ts(now),
                          "fills_file": fills_name, "pending": len(pending),
                          "result_shares": {c: p["shares"] for c, p
                                            in full["positions"].items()}},
        })
        new_state["fills_sync"] = fills_sync

        if dry_run:
            result.update({"rc": 0, "action": "check", "pending": len(pending),
                           "plan": diff})
            _write_report(report_file, history_file, result)
            return result

        # 6) 备份 → 账号切片包裹 → 原子写 → 回读校验（不一致则回滚备份）
        full_state = read_full_state(state_file, account)
        composed = compose_account_state(full_state, new_state, account, now)
        bak = _backup(state_file, now)
        _atomic_write_json(state_file, composed)
        reread = extract_account_state(read_full_state(state_file, account), account)
        if (reread.get("fills_sync", {}).get("last_sync", {}).get("at")
                != fmt_ts(now) or reread.get("holdings") != new_state.get("holdings")):
            shutil.copyfile(bak, state_file)
            raise SyncRefusal("write_verify_failed",
                              {"backup": str(bak), "restored": True})
        result.update({"rc": 0, "action": "write", "pending": len(pending),
                       "plan": diff, "backup": str(bak),
                       "zeroed": diff["zeroed"]})
        _write_report(report_file, history_file, result)
        logger.info("[broker_state_sync] %s applied %d fills, zeroed=%s",
                    day_prefix, len(pending), diff["zeroed"])
        return result
    except SyncRefusal as refusal:
        _append_alert(alerts_file, now=now, for_date=for_date,
                      reason=refusal.reason, detail=refusal.detail,
                      fills_name=fills_name)
        result.update({"rc": 1, "action": "refused", "reason": refusal.reason,
                       "detail": refusal.detail})
        _write_report(report_file, history_file, result)
        logger.error("[broker_state_sync] refused: %s %s", refusal.reason,
                     refusal.detail)
        return result


def _drift_check(state: dict, base: Mapping, applied: list) -> dict:
    """noop 路径：当前持仓 vs 锚点重放是否一致（手工改仓检测，不覆盖只告警）。"""
    try:
        replayed = replay(base, applied)
    except SyncRefusal:
        return {"error": "replay_failed"}
    current = {str(h.get("code")): (int(h.get("shares") or 0),
                                    float(h.get("cost_price") or 0))
               for h in state.get("holdings") or []}
    expected = {c: (int(p["shares"]),
                    _q3(p["cost"]) if p["cost"] is not None else 0.0)
                for c, p in replayed["positions"].items()
                if p["shares"] > 0}
    if current == expected:
        return {}
    return {"current": current, "expected_from_anchor": expected}


def _write_report(report_file: Path, history_file: Path, result: Mapping) -> None:
    _atomic_write_json(report_file, result)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    with history_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": result.get("at"), "date": result.get("date"),
                             "action": result.get("action"),
                             "pending": result.get("pending"),
                             "rc": result.get("rc"),
                             "reason": result.get("reason")},
                            ensure_ascii=False) + "\n")
