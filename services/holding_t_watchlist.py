# -*- coding: utf-8 -*-
"""R45 做T持仓股观察名单存储层（唯一受控写通道，stdlib-only）。

任务书：20261110_ttrade_watchlist_r45.md（R45）

职责边界（fail-closed）：
  - 只负责 holding_tick_watchlist.json 一个文件的读写：原子写（temp+os.replace）
    + 单份滚动备份（<name>.bak 保留上一版本）；
  - 不 import 任何网络/DB/调度器模块（AST 守卫锁定 import 白名单）；
  - 所有输入先整体校验再落盘：任何一项非法 → ValueError，原文件字节不变。

磁盘结构 v2：
  {
    "version": 2,
    "codes": ["300054.SZ", ...],            # 兼容旧读端（GET /holding-tick-analysis 等）
    "items": [
      {"code": "300054.SZ", "name": "鼎龙股份", "qty": 100,
       "source": "manual|ths", "added_at": "...", "updated_at": "..."}
    ],
    "updated_at": "..."
  }
旧结构 {codes:[...], updated_at} 读兼容：合成 items（source=manual、qty=None、
added_at=None、updated_at=文件级 updated_at）；一旦发生任何写，落盘迁移到 v2
store 路径，旧文件保持原字节不动。

合并语义（Owner 1.3，绝不删除）：
  - THS 有 → 更新 qty/name/source=ths/updated_at=now（added_at 不变）；
  - THS 无而列表有 → 保留全部字段（原 ths 条目 source 降标 manual，数量保留最后
    已知值），不删除；
  - THS 新 → 追加（source=ths）；
  - 无任何变化 → 零写（幂等）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")
MAX_ENTRIES = 100
_ENTRY_FIELDS = ("code", "name", "qty", "source", "added_at", "updated_at")
_VALID_SOURCES = ("manual", "ths")


def normalize_code(value: Any) -> str:
    """与 api.gen3_state_alpha._normalize_tick_code 同规则：6 位码自动补交易所后缀。"""
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", text) else ""
    if not re.fullmatch(r"\d{6}", text):
        return ""
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return f"{text}.SH" if text.startswith(("5", "6", "9")) else f"{text}.SZ"


def _now_text(now: datetime | None) -> str:
    return (now or datetime.now(SH_TZ)).isoformat(timespec="seconds")


def _coerce_qty(value: Any) -> int | None:
    """qty 合法值：None（未填）或正整数（含整值 float）；其余一律 ValueError。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"qty 非法: {value!r}")
    if isinstance(value, int):
        qty = value
    elif isinstance(value, float) and float(value).is_integer():
        qty = int(value)
    else:
        raise ValueError(f"qty 非法（须为正整数）: {value!r}")
    if qty <= 0:
        raise ValueError(f"qty 非法（须为正整数）: {value!r}")
    return qty


def _atomic_write_with_backup(path: Path, payload: dict[str, Any]) -> None:
    """原子写 + 单份滚动备份；任何失败原文件保持不变、零 temp 残留。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    backup = path.with_name(path.name + ".bak")
    try:
        temp.write_text(text, encoding="utf-8")
        if path.exists():
            shutil.copy2(path, backup)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _normalize_entries(raw: Any, fallback_updated_at: str | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return entries
    for row in raw:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("code"))
        if not code:
            continue
        source = str(row.get("source") or "manual")
        if source not in _VALID_SOURCES:
            source = "manual"
        qty = row.get("qty")
        entries.append({
            "code": code,
            "name": str(row.get("name") or "").strip(),
            "qty": _coerce_qty(qty) if qty is not None else None,
            "source": source,
            "added_at": row.get("added_at") or None,
            "updated_at": row.get("updated_at") or fallback_updated_at or None,
        })
    return entries


def read_watchlist(store_path: Path, legacy_path: Path | None = None) -> dict[str, Any]:
    """读端点数据源：store(v2) 优先；缺件回退旧 {codes:[...]} 结构（合成 manual items）。"""
    if store_path.exists():
        payload = json.loads(store_path.read_text(encoding="utf-8"))
        entries = _normalize_entries(payload.get("items"), payload.get("updated_at"))
        return {"codes": [e["code"] for e in entries], "items": entries,
                "updated_at": payload.get("updated_at"), "source": "store"}
    if legacy_path is not None and legacy_path.exists():
        payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        codes = payload.get("codes") if isinstance(payload, dict) else None
        updated_at = payload.get("updated_at") if isinstance(payload, dict) else None
        entries = _normalize_entries(
            [{"code": code} for code in (codes or []) if normalize_code(code)],
            updated_at,
        )
        return {"codes": [e["code"] for e in entries], "items": entries,
                "updated_at": updated_at, "source": "legacy"}
    return {"codes": [], "items": [], "updated_at": None, "source": "empty"}


def _persist(store_path: Path, entries: list[dict[str, Any]], now_text: str) -> None:
    _atomic_write_with_backup(store_path, {
        "version": 2,
        "codes": [entry["code"] for entry in entries],
        "items": entries,
        "updated_at": now_text,
    })


def upsert_watchlist(
    store_path: Path,
    add: Any,
    remove: Any,
    *,
    now: datetime | None = None,
    name_lookup: Callable[[list[str]], dict[str, str]] | None = None,
    legacy_path: Path | None = None,
) -> dict[str, Any]:
    """手动增删通道：add=[{code,qty?,name?}|code]；remove=[code]。整体校验后落盘。"""
    if add is None:
        add = []
    if remove is None:
        remove = []
    if not isinstance(add, list) or not isinstance(remove, list):
        raise ValueError("add/remove 必须为数组")
    adds: list[tuple[str, int | None]] = []
    for row in add:
        code = normalize_code(row.get("code") if isinstance(row, dict) else row)
        if not code:
            raise ValueError(f"add 条目代码非法: {row!r}")
        qty = _coerce_qty(row.get("qty") if isinstance(row, dict) else None)
        adds.append((code, qty))
    removes: list[str] = []
    for row in remove:
        code = normalize_code(row)
        if not code:
            raise ValueError(f"remove 条目代码非法: {row!r}")
        removes.append(code)

    current = read_watchlist(store_path, legacy_path=legacy_path)
    entries = [dict(entry) for entry in current["items"]]
    by_code = {entry["code"]: entry for entry in entries}
    names: dict[str, str] = {}
    if name_lookup is not None and adds:
        try:
            names = name_lookup([code for code, _ in adds]) or {}
        except Exception:  # 名称补全失败不阻塞写通道（名称可后补）
            names = {}

    now_text = _now_text(now)
    removed: list[str] = []
    for code in removes:
        if code in by_code:
            entries = [entry for entry in entries if entry["code"] != code]
            by_code.pop(code, None)
            removed.append(code)
    added: list[str] = []
    updated: list[str] = []
    for code, qty in adds:
        existing = by_code.get(code)
        if existing is None:
            entry = {
                "code": code,
                "name": str(names.get(code) or "").strip(),
                "qty": qty,
                "source": "manual",
                "added_at": now_text,
                "updated_at": now_text,
            }
            entries.append(entry)
            by_code[code] = entry
            added.append(code)
            continue
        changed = False
        if qty is not None and qty != existing["qty"]:
            existing["qty"] = qty
            changed = True
        if existing["source"] != "manual":
            existing["source"] = "manual"  # 手动确认覆盖数量 → 来源如实转 manual
            changed = True
        if not existing["name"] and str(names.get(code) or "").strip():
            existing["name"] = str(names.get(code) or "").strip()
            changed = True
        if changed:
            existing["added_at"] = existing["added_at"] or now_text
            existing["updated_at"] = now_text
            updated.append(code)
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"观察名单最多 {MAX_ENTRIES} 条")

    changed = bool(added or updated or removed)
    if changed:
        _persist(store_path, entries, now_text)
    return {
        "ok": True,
        "codes": [entry["code"] for entry in entries],
        "items": entries,
        "updated_at": now_text if changed else current["updated_at"],
        "diff": {"added": added, "updated": updated, "removed": removed},
        "changed": changed,
    }


def sync_watchlist_from_ths(
    store_path: Path,
    holdings: Any,
    *,
    synced_at: str,
    now: datetime | None = None,
    legacy_path: Path | None = None,
) -> dict[str, Any]:
    """THS 持仓合并通道（Owner 1.3：绝不删除任何现有条目）。

    holdings=broker_state.json 的 holdings 行（code/name/shares）；
    synced_at=broker_state.updated_at（数据时点，透传给前端展示）。
    """
    if not str(synced_at or "").strip():
        raise ValueError("synced_at 缺失（broker_state 无 updated_at，fail-closed 不写）")
    if not isinstance(holdings, list):
        raise ValueError("holdings 必须为数组")
    ths: dict[str, dict[str, Any]] = {}
    for row in holdings:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("code"))
        if not code:
            continue
        try:
            qty = _coerce_qty(row.get("shares"))
        except ValueError:
            continue  # 坏行不进 THS 集（视同 THS 无该码 → 保留条目不误删）
        if qty is None:
            continue
        ths[code] = {"name": str(row.get("name") or "").strip(), "qty": qty}

    current = read_watchlist(store_path, legacy_path=legacy_path)
    entries = [dict(entry) for entry in current["items"]]
    now_text = _now_text(now)
    added: list[str] = []
    updated: list[str] = []
    kept: list[str] = []
    changed = False
    for entry in entries:
        code = entry["code"]
        if code in ths:
            row = ths[code]
            material = (
                entry["qty"] != row["qty"]
                or (row["name"] and entry["name"] != row["name"])
                or entry["source"] != "ths"
            )
            if material:
                entry["qty"] = row["qty"]
                if row["name"]:
                    entry["name"] = row["name"]
                entry["source"] = "ths"
                entry["added_at"] = entry["added_at"] or now_text
                entry["updated_at"] = now_text
                updated.append(code)
                changed = True
            else:
                kept.append(code)
        else:
            if entry["source"] == "ths":
                entry["source"] = "manual"  # THS 已无该持仓：降标 manual，绝不删除
                changed = True
            kept.append(code)
    for code, row in ths.items():
        if code not in {entry["code"] for entry in entries}:
            entries.append({
                "code": code,
                "name": row["name"],
                "qty": row["qty"],
                "source": "ths",
                "added_at": now_text,
                "updated_at": now_text,
            })
            added.append(code)
            changed = True
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"观察名单最多 {MAX_ENTRIES} 条")

    if changed:
        _persist(store_path, entries, now_text)
    return {
        "ok": True,
        "codes": [entry["code"] for entry in entries],
        "items": entries,
        "updated_at": now_text if changed else current["updated_at"],
        "synced_at": str(synced_at),
        "source": "broker_state",
        "diff": {"added": added, "updated": updated, "kept": kept},
        "summary": f"新增 {len(added)} / 更新 {len(updated)} / 保留 {len(kept)}",
        "changed": changed,
    }
