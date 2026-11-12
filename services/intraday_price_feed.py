# -*- coding: utf-8 -*-
"""盘中实时行情采集（东财 REST 30s 轮询）。

任务书 20261112_l2_integration.md 第 1 项：
  东财 REST 接口投票池（secids=持仓+watchlist），每 30s 一次；
  缓存最新价/量于内存+文件（runtime/t_tracker/price_cache.json，增量写不每秒刷盘）；
  每 5 分钟脚标落盘（文件形，占位后端命名空间在 runtime/ 下）。

数据流转：t_trade_monitor job → 本模块 → price_cache.json（下一拍 RSI 引擎读）。
"""

from __future__ import annotations

__version__ = "1.0.0"

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
import urllib.request

logger = logging.getLogger("intraday_price_feed")

BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
SCHEMA_CACHE = "intraday_price_cache/1"

# 东财 ulist 接口（多码一次请求）
EAST_ULIST_URL = (
    "http://push2.eastmoney.com/api/qt/ulist.np/get"
    "?secids={secids}&fields=f2,f3,f4,f12,f13,f14,f15,f16,f17,f18,f124&fltt=2"
)

CACHE_TTL_SEC = 30.0        # 缓存有效期（与轮询周期一致）
ULIST_CHUNK = 10            # 单请求码数上限
PRICE_CACHE_FILE = "price_cache.json"


# ── 条目 schema ──────────────────────────────────────────────────
# {
#   "ts": "2026-11-12 09:35:01",
#   "quotes": {
#     "002015": {"price": 13.25, "volume": 1234500, "change_pct": 0.85, "ts": "09:35:01"},
#     "300054": {"price": 60.12, "volume": 2345600, "change_pct": -0.32, "ts": "09:35:01"},
#   },
#   "source": "eastmoney_ulist"
# }


# ── 路径 ──────────────────────────────────────────────────────────

def _tracker_dir() -> Path:
    from utils.paths import runtime_path
    return runtime_path("t_tracker")


def _cache_path() -> Path:
    return _tracker_dir() / PRICE_CACHE_FILE


# ── 东财接口 ──────────────────────────────────────────────────────

def east_secid(code: str) -> str:
    code6 = code.strip().split(".")[0]
    if code6.startswith(("6", "9", "5")):
        return f"1.{code6}"
    return f"0.{code6}"


def normalize_code(code: str) -> str:
    return code.strip().split(".")[0]


def fetch_quotes(
    codes: Sequence[str],
    *,
    timeout: float = 10.0,
    url_open: Optional[Callable] = None,
) -> dict[str, dict[str, Any]]:
    """东财 ulist 实时行情批量查询。

    返回 {normalized_code_6: {price, volume, change_pct, ts}}。
    停牌/'-' 跳过；全部失败返回空 dict（由调用方降级）。
    """
    out: dict[str, dict[str, Any]] = {}
    codes6 = [normalize_code(c) for c in codes if c]
    if not codes6:
        return out
    open_ = url_open or urllib.request.urlopen
    for i in range(0, len(codes6), ULIST_CHUNK):
        chunk = codes6[i:i + ULIST_CHUNK]
        secids = ",".join(east_secid(c) for c in chunk)
        req = urllib.request.Request(
            EAST_ULIST_URL.format(secids=secids),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with open_(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("[feed] ulist chunk %s 失败: %s", secids, exc)
            continue
        for it in (data.get("data") or {}).get("diff") or []:
            code6 = str(it.get("f12") or "")
            raw_px = it.get("f2")
            if not code6 or raw_px in (None, "", "-"):
                continue
            ts = None
            if it.get("f124"):
                try:
                    ts = datetime.fromtimestamp(
                        int(it["f124"]), BEIJING_TZ
                    ).strftime("%H:%M:%S")
                except (ValueError, OSError, OverflowError):
                    ts = None
            volume = it.get("f17")  # 总手
            change_pct = it.get("f15")  # 涨跌幅 %
            out[code6] = {
                "price": float(raw_px),
                "volume": float(volume) if volume not in (None, "", "-") else None,
                "change_pct": float(change_pct) if change_pct not in (None, "", "-") else None,
                "ts": ts or datetime.now(BEIJING_TZ).strftime("%H:%M:%S"),
            }
    return out


# ── 缓存读写 ──────────────────────────────────────────────────────

_cache: Optional[dict[str, Any]] = None
_last_fetch_ts: Optional[float] = None


def _now_ts() -> float:
    return time.time()


def read_price_cache() -> Optional[dict[str, Any]]:
    """读磁盘缓存（启动时用；盘中入内存）。"""
    path = _cache_path()
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_price_cache(payload: dict[str, Any]) -> None:
    """原子写 price_cache.json（增量写，不每秒刷盘）。"""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def get_latest_price(code: str) -> Optional[float]:
    """从内存缓存获取某票最新价（None=暂无或过期）。"""
    global _cache
    if _cache is None:
        _cache = read_price_cache()
    if _cache is None:
        return None
    q = (_cache.get("quotes") or {}).get(normalize_code(code))
    return float(q["price"]) if q and q.get("price") is not None else None


def get_latest_quotes() -> dict[str, dict[str, Any]]:
    """返回内存缓存的全部报价（读当前 cache 快照）。"""
    global _cache
    if _cache is None:
        _cache = read_price_cache()
    return (_cache or {}).get("quotes") or {}


# ── 主轮询函数 ────────────────────────────────────────────────────

def refresh(
    codes: Sequence[str],
    *,
    force: bool = False,
    url_open: Optional[Callable] = None,
) -> dict[str, Any]:
    """轮询一次：拉取东财报价 → 更新缓存在内存+文件。

    codes: 6 位码列表（如 ['002015', '300054', '603881']）。
    force: True 跳过 TTL 检查强制轮询。
    返回本拍快照（{ts, quotes, source}）。
    """
    global _cache, _last_fetch_ts

    now_ts = _now_ts()
    if not force and _last_fetch_ts is not None and (now_ts - _last_fetch_ts) < CACHE_TTL_SEC:
        # TTL 内：返回当前缓存（不刷盘）
        return _cache or {"schema": SCHEMA_CACHE, "ts": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"), "quotes": {}, "source": "cache_hit"}

    quotes = fetch_quotes(codes, url_open=url_open)
    now_bj = datetime.now(BEIJING_TZ)

    # 合并旧报价中未返回但还在有效期的码（东财 ulist 有时会漏少数停机码）
    if _cache and _cache.get("quotes"):
        old_q = _cache["quotes"]
        stale_sec = 120.0  # 旧报价最多接管 2min
        for c, q in old_q.items():
            if c not in quotes and q.get("price") is not None:
                q_ts_str = q.get("ts", "")
                try:
                    q_dt = datetime.strptime(q_ts_str, "%H:%M:%S").replace(
                        year=now_bj.year, month=now_bj.month, day=now_bj.day
                    )
                    if (now_bj - q_dt).total_seconds() < stale_sec:
                        quotes[c] = q
                except (ValueError, TypeError):
                    pass

    payload: dict[str, Any] = {
        "schema": SCHEMA_CACHE,
        "ts": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "quotes": quotes,
        "source": "eastmoney_ulist" if any(quotes) else "no_data",
    }

    _cache = payload
    _last_fetch_ts = now_ts

    # 每 5 分钟刷盘一次（避免每秒 IO）
    _write_if_expired(payload, now_ts)

    return payload


def _write_if_expired(payload: dict[str, Any], now_ts: float) -> None:
    """每 300s 刷盘一次，避免高频 IO。"""
    path = _cache_path()
    last_write = 0.0
    if path.exists():
        try:
            last_write = path.stat().st_mtime
        except OSError:
            pass
    if now_ts - last_write >= 300.0:
        _write_price_cache(payload)
        logger.debug("[feed] price_cache 刷盘")


def reset_cache() -> None:
    """清空内存缓存（测试用）。"""
    global _cache, _last_fetch_ts
    _cache = None
    _last_fetch_ts = None


def clear_cache_file() -> None:
    """删除缓存文件（测试用）。"""
    path = _cache_path()
    if path.exists():
        path.unlink()