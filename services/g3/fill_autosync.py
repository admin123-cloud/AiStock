# -*- coding: utf-8 -*-
"""G3 Phase 3C 进阶：半自动预填单派发队列 + 同花顺真实成交回执自动入账引擎。

安全铁律：
1. 预填单派发强制 mode="prefill_only" 且 allow_submit=False，绝不下发任何自动提交/撤单指令；
2. 仅允许已通过 L2 风控预检（approved=True）且未重复消费的 draft_id 入队；
3. 成交回执自动入账按稳定 fill_id 全量幂等去重，并自动匹配当日预填单继承 R51 分型（t_trade / base_rebalance）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from services.trading.t_daily_plan import append_t_execution, load_intraday_executions
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
BRIDGE_HEARTBEAT_TTL_SEC = 45.0

_BUY_TOKENS = ("BUY", "B", "买入", "证券买入", "买")
_SELL_TOKENS = ("SELL", "S", "卖出", "证券卖出", "卖")


def _prefill_root(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("g3_prefill_drafts")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_json_safe(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 1. 半自动预填单派发与状态查询（prefill_only，绝不自动提交）
# ---------------------------------------------------------------------------

def get_prefill_bridge_status(base_dir: Optional[Path] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """读取宿主机 g3_prefill_bridge 心跳，判断预填桥是否在线。"""
    root = _prefill_root(base_dir)
    hb = _read_json_safe(root / "bridge_heartbeat.json")
    if not hb:
        return {"online": False, "state": "offline", "heartbeat_at": None, "pid": None}
    hb_at = str(hb.get("heartbeat_at") or "")
    online = False
    try:
        dt = datetime.fromisoformat(hb_at)
        ref = now or datetime.now(BJ_TZ)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BJ_TZ)
        age = (ref - dt).total_seconds()
        online = age <= BRIDGE_HEARTBEAT_TTL_SEC
    except Exception:
        online = False
    return {
        "online": online,
        "state": hb.get("state") if online else "offline",
        "heartbeat_at": hb_at or None,
        "pid": hb.get("pid"),
    }


def _find_draft_by_id(draft_id: str, trade_date: str, base_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    root = _prefill_root(base_dir)
    ymd = trade_date.replace("-", "")
    p = root / f"{ymd}.jsonl"
    if not p.exists():
        return None
    for line in reversed(p.read_text(encoding="utf-8", errors="ignore").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("draft_id") == draft_id:
                return obj
        except ValueError:
            continue
    return None


def dispatch_prefill_order(
    *,
    draft_id: str,
    trade_date: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """将已通过 L2 风控预检的草案派发至宿主机预填队列（强制 prefill_only、幂等防重）。"""
    now_bj = datetime.now(BJ_TZ)
    tdate = trade_date or now_bj.strftime("%Y-%m-%d")
    root = _prefill_root(base_dir)
    queue_dir = root / "queue"
    done_dir = root / "done"
    queue_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)

    # 幂等保护：已执行或已在队列中的 draft_id 拒绝重复派发
    done_file = done_dir / f"{draft_id}.json"
    if done_file.exists():
        prev = _read_json_safe(done_file) or {}
        return {
            "status": "already_processed",
            "draft_id": draft_id,
            "mode": "prefill_only",
            "allow_submit": False,
            "result": prev,
        }

    req_file = queue_dir / f"req_{draft_id}.json"
    if req_file.exists():
        return {
            "status": "already_queued",
            "draft_id": draft_id,
            "mode": "prefill_only",
            "allow_submit": False,
            "bridge": get_prefill_bridge_status(base_dir=root, now=now_bj),
        }

    draft = _find_draft_by_id(draft_id, tdate, base_dir=root)
    if not draft:
        raise ValueError(f"未找到当日预检草案 draft_id={draft_id}，请先执行 L2 风控预检")
    if not draft.get("approved"):
        raise ValueError(f"该草案未通过 L2 硬风控预检（{draft.get('summary')}），禁止派发预填")

    order = draft.get("order") or {}
    payload = {
        "schema": "g3_prefill_request/1",
        "draft_id": draft_id,
        "trade_date": tdate,
        "requested_at": now_bj.isoformat(timespec="seconds"),
        "action": "prefill_only",
        "mode": "prefill_only",
        "allow_submit": False,  # 永不可开启
        "account": str(order.get("account_id") or "acct1"),
        "code": str(order.get("code") or ""),
        "name": str(order.get("name") or ""),
        "side": str(order.get("side") or "").upper(),
        "order_type": str(order.get("order_type") or "t_trade"),
        "price": float(order.get("price") or 0.0),
        "qty": int(order.get("shares") or 0),
    }
    _atomic_write_json(req_file, payload)

    return {
        "status": "queued",
        "draft_id": draft_id,
        "mode": "prefill_only",
        "allow_submit": False,
        "bridge": get_prefill_bridge_status(base_dir=root, now=now_bj),
        "message": "预填指令已入队（仅填单绝不提交），等待宿主机桌面桥填入同花顺界面",
    }


def get_prefill_dispatch_status(draft_id: str, base_dir: Optional[Path] = None) -> dict[str, Any]:
    """查询指定 draft_id 的宿主机预填执行状态。"""
    root = _prefill_root(base_dir)
    done_file = root / "done" / f"{draft_id}.json"
    if done_file.exists():
        res = _read_json_safe(done_file) or {}
        return {
            "draft_id": draft_id,
            "state": "done",
            "bridge": get_prefill_bridge_status(base_dir=root),
            "result": res,
        }
    req_file = root / "queue" / f"req_{draft_id}.json"
    if req_file.exists():
        return {
            "draft_id": draft_id,
            "state": "queued",
            "bridge": get_prefill_bridge_status(base_dir=root),
            "result": None,
        }
    return {
        "draft_id": draft_id,
        "state": "not_found",
        "bridge": get_prefill_bridge_status(base_dir=root),
        "result": None,
    }


# ---------------------------------------------------------------------------
# 2. 同花顺真实成交回执自动入账到 G3 分型台账（全量幂等）
# ---------------------------------------------------------------------------

def _norm_side(raw: Any) -> Optional[str]:
    s = str(raw or "").strip().upper()
    if s in _BUY_TOKENS or "买入" in s:
        return "BUY"
    if s in _SELL_TOKENS or "卖出" in s:
        return "SELL"
    return None


def _norm_date(raw: Any) -> Optional[str]:
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", str(raw or ""))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _load_today_prefill_drafts(trade_date: str, base_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    root = _prefill_root(base_dir)
    p = root / f"{trade_date.replace('-', '')}.jsonl"
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("approved"):
                out.append(obj)
        except ValueError:
            continue
    return out


def _stable_fill_id(acct: str, tdate: str, code6: str, side: str, qty: int, px: float, row: Mapping[str, Any]) -> str:
    raw_id = str(row.get("fill_id") or row.get("deal_id") or row.get("cjbh") or row.get("id") or "").strip()
    if raw_id:
        return f"ths:{acct}:{raw_id}"
    ts_part = str(row.get("time") or row.get("deal_time") or row.get("fill_ts") or row.get("ts") or "").strip()
    sig = f"{acct}|{tdate}|{ts_part}|{code6}|{side}|{qty}|{px:.3f}"
    return "ths_fp:" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:14]


def _extract_today_fills_from_ths(ths_dir: Path, trade_date: str) -> list[dict[str, Any]]:
    """从 latest_multi.json（优先）提取当日全部账号的真实成交记录。"""
    multi = _read_json_safe(ths_dir / "latest_multi.json")
    if not multi:
        return []
    accounts = multi.get("accounts")
    if not isinstance(accounts, Mapping):
        return []

    out: list[dict[str, Any]] = []
    for acct_id, entry in accounts.items():
        if not isinstance(entry, Mapping):
            continue
        ra = entry.get("read_account")
        if not isinstance(ra, Mapping):
            continue
        today_obj = ra.get("today_fills")
        fills_obj = ra.get("fills")
        rows = list((today_obj or {}).get("rows") or []) if isinstance(today_obj, Mapping) else []
        if not rows and isinstance(fills_obj, Mapping):
            rows = list(fills_obj.get("rows") or [])
        gen_date = _norm_date(ra.get("generated_at") or multi.get("generated_at"))

        for r in rows:
            if not isinstance(r, Mapping):
                continue
            r_date = _norm_date(r.get("date") or r.get("trade_date") or r.get("fill_ts") or gen_date)
            if r_date != trade_date:
                continue
            code6 = str(r.get("code") or r.get("symbol") or "").split(".")[0].strip().zfill(6)
            side = _norm_side(r.get("side") or r.get("operation") or r.get("direction"))
            try:
                qty = int(round(float(r.get("qty") or r.get("quantity") or r.get("volume") or 0)))
                px = float(r.get("px") or r.get("price") or r.get("avg_price") or 0)
            except (TypeError, ValueError):
                continue
            if not code6 or code6 == "000000" or side is None or qty < 100 or qty % 100 != 0 or px <= 0:
                continue
            out.append({
                "account": str(acct_id),
                "code": code6,
                "name": str(r.get("name") or code6),
                "side": side,
                "shares": qty,
                "price": round(px, 3),
                "fill_id": _stable_fill_id(str(acct_id), trade_date, code6, side, qty, px, r),
            })
    return out


def sync_broker_fills_to_desk_executions(
    *,
    trade_date: Optional[str] = None,
    ths_dir: Optional[Path] = None,
    prefill_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """将同花顺最新当日真实成交幂等同步到 G3 日内分型台账，并自动继承匹配预填单的 R51 分型。"""
    tdate = trade_date or datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    ths_root = ths_dir or runtime_path("ths_read_account")
    fills = _extract_today_fills_from_ths(ths_root, tdate)

    existing = load_intraday_executions(tdate)
    existing_fids = {
        str(e.get("note") or "")
        for e in existing
    }
    # 也精确提取 note 中的 [fill_id=...] 标记
    seen_ids: set[str] = set()
    for e in existing:
        if e.get("fill_id"):
            seen_ids.add(str(e["fill_id"]))
        m = re.search(r"\[fid:([^\]]+)\]", str(e.get("note") or ""))
        if m:
            seen_ids.add(m.group(1))

    drafts = _load_today_prefill_drafts(tdate, base_dir=prefill_dir)
    imported = 0
    skipped = 0
    imported_entries: list[dict[str, Any]] = []

    for f in fills:
        fid = f["fill_id"]
        if fid in seen_ids or any(fid in x for x in existing_fids):
            skipped += 1
            continue

        # 尝试匹配当日预填单，继承 order_type（t_trade / base_rebalance）
        matched_order_type = "t_trade"
        matched_draft_id = ""
        for d in reversed(drafts):
            od = d.get("order") or {}
            if (
                str(od.get("code") or "").zfill(6) == f["code"]
                and str(od.get("side") or "").upper() == f["side"]
                and int(od.get("shares") or 0) == f["shares"]
            ):
                matched_order_type = str(od.get("order_type") or "t_trade")
                matched_draft_id = str(d.get("draft_id") or "")
                break

        note_parts = [f"同花顺回执自动入账 [fid:{fid}]"]
        if matched_draft_id:
            note_parts.append(f"匹配预填单 {matched_draft_id}")

        entry = append_t_execution(
            trade_date=tdate,
            code=f["code"],
            name=f["name"],
            side=f["side"],
            price=f["price"],
            shares=f["shares"],
            order_type=matched_order_type,
            account_label=f["account"],
            note=" · ".join(note_parts),
        )
        seen_ids.add(fid)
        imported += 1
        imported_entries.append(entry)

    return {
        "status": "ok",
        "trade_date": tdate,
        "broker_fills_found": len(fills),
        "imported": imported,
        "idempotent_skipped": skipped,
        "entries": imported_entries,
    }
