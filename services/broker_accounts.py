# -*- coding: utf-8 -*-
"""AiStock 资金账号管理服务层（双账号聚合只读 + 宿主机桌面桥同步指令队列）。

安全铁律：
1. 凭证零接触：`credentials_touched` 恒为 `False`，绝不读取或透传任何账号密码；
2. 绝不下单/撤单：同步指令仅允许触发只读采集（资金/持仓/历史成交）；
3. 最小权限落盘：写侧仅向 `runtime/ths_read_account/commands/` 原子写入只读同步请求文件。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from utils.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))

ACCOUNT_REGISTRY: dict[str, dict[str, str]] = {
    "acct1": {"account_id": "acct1", "label": "广发证券-曾*", "masked_id": "A5****1851", "owner": "曾"},
    "acct2": {"account_id": "acct2", "label": "广发证券-王*美", "masked_id": "A6****5646", "owner": "王"},
}

BRIDGE_ONLINE_TTL_SEC = 180.0


def default_accounts_root() -> Path:
    """返回同花顺远航采集产物根目录（支持 AISTOCK_DATA_ROOT 覆盖）。"""
    return Path(runtime_path("ths_read_account"))


def _read_json_safe(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_read_payload(raw: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """兼容 latest_acctN.json（嵌套 read_account）与直接 ths_client 输出格式。"""
    if not isinstance(raw, Mapping):
        return None
    nested = raw.get("read_account")
    if isinstance(nested, Mapping):
        return nested
    if raw.get("command") == "read-account":
        return raw
    return None


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BJ_TZ)
        return dt.astimezone(BJ_TZ)
    except Exception:  # noqa: BLE001
        return None


def _pick_best_account_raw(root: Path, account_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """优先读分账号 latest_<acct>.json；若其未采集成功，则兼容回退单账号 latest.json（仅限同账号）。"""
    per_acct_path = root / f"latest_{account_id}.json"
    per_acct = _read_json_safe(per_acct_path)
    if per_acct is not None:
        ra = _extract_read_payload(per_acct)
        if ra and ra.get("funds_read_ok"):
            return per_acct, per_acct_path.name

    legacy_path = root / "latest.json"
    legacy = _read_json_safe(legacy_path)
    if legacy is not None:
        ra_leg = _extract_read_payload(legacy)
        leg_acct = (ra_leg or {}).get("account") or legacy.get("account_requested")
        if leg_acct == account_id and ra_leg and ra_leg.get("funds_read_ok"):
            return legacy, legacy_path.name

    if per_acct is not None:
        return per_acct, per_acct_path.name
    return None, None


def get_account_detail(account_id: str, *, root: Optional[Path] = None) -> dict[str, Any]:
    """只读读取单账号标准视图（资金、持仓、历史成交、采集状态与诚实降级标记）。"""
    acct = str(account_id or "").strip()
    meta = ACCOUNT_REGISTRY.get(acct)
    if meta is None:
        raise KeyError(f"unknown account_id: {acct}")

    base = Path(root) if root is not None else default_accounts_root()
    raw, source_file = _pick_best_account_raw(base, acct)
    ra = _extract_read_payload(raw)

    if ra is None:
        return {
            **meta,
            "available": False,
            "status": (raw or {}).get("status") or "unavailable",
            "reason": (raw or {}).get("reason") or "no_snapshot",
            "rc": (raw or {}).get("rc", 4),
            "generated_at": (raw or {}).get("generated_at"),
            "source_file": source_file,
            "credentials_touched": False,
            "funds_read_ok": False,
            "funds": None,
            "holdings": [],
            "holdings_count": 0,
            "fills": [],
            "fills_count": 0,
            "fills_summary": None,
            "coverage": {"holdings_complete": False, "fills_complete": False},
            "identity": {"before": None, "after": None},
        }

    funds = ra.get("funds") if isinstance(ra.get("funds"), dict) else None
    holdings = list(ra.get("holdings") or [])
    fills_obj = ra.get("fills") if isinstance(ra.get("fills"), dict) else {}
    fills_rows = list(fills_obj.get("rows") or [])
    identity = ra.get("identity") if isinstance(ra.get("identity"), dict) else {}
    coverage = ra.get("coverage") if isinstance(ra.get("coverage"), dict) else {}

    # 严格身份一致性守卫：若报告内标注账号不匹配，降级为不可用，绝不串账
    reported_acct = ra.get("account") or identity.get("after") or identity.get("before")
    if reported_acct and str(reported_acct) != acct:
        return {
            **meta,
            "available": False,
            "status": "identity_mismatch",
            "reason": f"reported_account={reported_acct} != {acct}",
            "rc": 4,
            "generated_at": ra.get("generated_at"),
            "source_file": source_file,
            "credentials_touched": False,
            "funds_read_ok": False,
            "funds": None,
            "holdings": [],
            "holdings_count": 0,
            "fills": [],
            "fills_count": 0,
            "fills_summary": None,
            "coverage": {"holdings_complete": False, "fills_complete": False},
            "identity": identity,
        }

    return {
        **meta,
        "available": bool(ra.get("funds_read_ok")),
        "status": str(ra.get("status") or "unavailable"),
        "reason": ra.get("reason"),
        "rc": int(ra.get("rc", 4)),
        "generated_at": ra.get("generated_at") or (raw or {}).get("generated_at"),
        "source_file": source_file,
        "credentials_touched": False,
        "funds_read_ok": bool(ra.get("funds_read_ok")),
        "funds": funds,
        "holdings": holdings,
        "holdings_count": len(holdings),
        "fills": fills_rows,
        "fills_count": len(fills_rows),
        "fills_summary": fills_obj.get("summary"),
        "fills_status": fills_obj.get("status"),
        "coverage": {
            "holdings_complete": bool(coverage.get("holdings_complete")),
            "fills_complete": bool(coverage.get("fills_complete")),
        },
        "identity": {
            "before": identity.get("before"),
            "after": identity.get("after"),
        },
    }


def get_bridge_status(*, root: Optional[Path] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """读取宿主机常驻桌面桥心跳与待执行同步指令队列状态。"""
    base = Path(root) if root is not None else default_accounts_root()
    now_bj = (now.astimezone(BJ_TZ) if (now and now.tzinfo) else (now.replace(tzinfo=BJ_TZ) if now else datetime.now(BJ_TZ)))
    hb = _read_json_safe(base / "bridge_heartbeat.json")
    hb_dt = _parse_iso_dt((hb or {}).get("heartbeat_at"))
    age_sec = round((now_bj - hb_dt).total_seconds(), 1) if hb_dt else None
    online = bool(hb_dt is not None and age_sec is not None and age_sec <= BRIDGE_ONLINE_TTL_SEC)

    cmd_dir = base / "commands"
    pending_files = sorted(cmd_dir.glob("req_*.json")) if cmd_dir.is_dir() else []
    done_dir = cmd_dir / "done"
    latest_done = None
    if done_dir.is_dir():
        done_files = sorted(done_dir.glob("req_*.json"), reverse=True)
        if done_files:
            latest_done = _read_json_safe(done_files[0])

    return {
        "online": online,
        "heartbeat_at": (hb or {}).get("heartbeat_at"),
        "age_seconds": age_sec,
        "state": (hb or {}).get("state") or ("online" if online else "offline"),
        "active_request_id": (hb or {}).get("active_request_id"),
        "pending_count": len(pending_files),
        "pending_ids": [p.stem for p in pending_files[:5]],
        "latest_completed": latest_done,
    }


def get_accounts_overview(*, root: Optional[Path] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """只读返回双账号合并总览、分账详情与宿主机桌面桥状态。"""
    base = Path(root) if root is not None else default_accounts_root()
    accounts = [get_account_detail(aid, root=base) for aid in sorted(ACCOUNT_REGISTRY.keys())]

    total_assets = 0.0
    market_value = 0.0
    balance = 0.0
    available = 0.0
    withdrawable = 0.0
    frozen = 0.0
    holding_profit = 0.0
    daily_profit = 0.0
    ready_count = 0
    total_holdings = 0

    for item in accounts:
        funds = item.get("funds")
        if item.get("funds_read_ok") and isinstance(funds, dict):
            ready_count += 1
            total_assets += _to_float(funds.get("total_assets")) or 0.0
            market_value += _to_float(funds.get("market_value")) or 0.0
            balance += _to_float(funds.get("balance")) or 0.0
            available += _to_float(funds.get("available")) or 0.0
            withdrawable += _to_float(funds.get("withdrawable")) or 0.0
            frozen += _to_float(funds.get("frozen")) or 0.0
            holding_profit += _to_float(funds.get("holding_profit")) or 0.0
            daily_profit += _to_float(funds.get("daily_profit")) or 0.0
        total_holdings += int(item.get("holdings_count") or 0)

    prev_assets = total_assets - daily_profit
    daily_profit_ratio = round((daily_profit / prev_assets) * 100.0, 2) if prev_assets > 0 else None

    return {
        "schema": "aistock_broker_accounts_overview/1",
        "credentials_touched": False,
        "accounts_total": len(accounts),
        "accounts_ready": ready_count,
        "merged": {
            "total_assets": round(total_assets, 2),
            "market_value": round(market_value, 2),
            "balance": round(balance, 2),
            "available": round(available, 2),
            "withdrawable": round(withdrawable, 2),
            "frozen": round(frozen, 2),
            "holding_profit": round(holding_profit, 2),
            "daily_profit": round(daily_profit, 2),
            "daily_profit_ratio": daily_profit_ratio,
            "holdings_count": total_holdings,
        },
        "accounts": accounts,
        "bridge": get_bridge_status(root=base, now=now),
    }


def enqueue_sync_request(
    *,
    accounts: Optional[Sequence[str]] = None,
    with_fills: bool = True,
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """向宿主机桌面桥写入只读同步指令（scoped-write：仅写 runtime/ths_read_account/commands/）。"""
    target_accounts = [str(a).strip() for a in (accounts or sorted(ACCOUNT_REGISTRY.keys())) if str(a).strip()]
    if not target_accounts:
        raise ValueError("accounts 不能为空")
    unknown = [a for a in target_accounts if a not in ACCOUNT_REGISTRY]
    if unknown:
        raise ValueError(f"未知账号: {unknown}")

    base = Path(root) if root is not None else default_accounts_root()
    cmd_dir = base / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)

    # 防重复排队：已有待执行请求时幂等返回现有请求
    existing = sorted(cmd_dir.glob("req_*.json"))
    if existing:
        pending_payload = _read_json_safe(existing[0]) or {}
        return {
            "status": "already_queued",
            "request_id": pending_payload.get("request_id") or existing[0].stem,
            "accounts": pending_payload.get("accounts") or target_accounts,
            "with_fills": bool(pending_payload.get("with_fills", with_fills)),
            "credentials_touched": False,
            "bridge": get_bridge_status(root=base, now=now),
        }

    now_bj = (now.astimezone(BJ_TZ) if (now and now.tzinfo) else (now.replace(tzinfo=BJ_TZ) if now else datetime.now(BJ_TZ)))
    req_id = f"req_{now_bj.strftime('%Y%m%d_%H%M%S_%f')}"
    payload = {
        "schema": "ths_desktop_bridge_request/1",
        "request_id": req_id,
        "action": "read_accounts",
        "accounts": target_accounts,
        "with_fills": bool(with_fills),
        "allow_order": False,
        "credentials_touched": False,
        "requested_at": now_bj.isoformat(),
    }
    target_path = cmd_dir / f"{req_id}.json"
    tmp_path = target_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, target_path)

    return {
        "status": "queued",
        "request_id": req_id,
        "accounts": target_accounts,
        "with_fills": bool(with_fills),
        "credentials_touched": False,
        "bridge": get_bridge_status(root=base, now=now),
    }
