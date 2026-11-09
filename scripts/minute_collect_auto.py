# -*- coding: utf-8 -*-
"""5 分钟数据自动采集器（R21 建立；R22 源升级：东财 push2his 为默认主源）。

源选型（R22 2026-11-09 评估，证据
``F:/Aistock/data/data/runtime/minute_source_eval_r22/eval_comparison_r22.json``）：
  - east=东财 push2his 5m（**默认源**）：带 amount（元）。10 样本股 × 11-06/11-09
    与库内 lc5 基准逐 bar：价 20/20、量 18/20、48 槽 20/20、日额与 kline_daily
    核对 20/20（≤0.1%）、量比 median=1.0000、单码 ~0.4s、深度实测 1536 根≈32
    交易日、覆盖北交所 920xxx（腾讯无覆盖）。已知口径差：15:00 bar 含完整
    收盘集合竞价（TDX lc5 仅部分归因），末 bar 量/额差 >3% 仅出现在竞价重的
    股票（30 码 × 2 日扩展探针 8/60 键、全部且仅 15:00 bar、日合计差 ≤0.06%）。
  - sina=新浪 5m：评估未过、不入生产——低流动键缺零量 bar（本批实测 43/45/42
    根 ≠48 槽）、volume 单位为股且非整百（→非整手，与 kline_minute_5.volume
    Int64 列冲突无法无损入列）、个别 bar amount 噪声（日合计仍一致）。
  - B=腾讯 m5（R21 主源 → R22 兜底）：无 amount（NULL 如实）；最近 320 根≈6.7
    交易日；920xxx.BJ 无覆盖。
  - A=pytdx TDX hq 协议直连：**不可行**（25 个公共 hq 主机全部无法取 K 线），
    实现保留（协议若复活可直接启用）。
  源回退顺序 SOURCE_FALLBACK_ORDER=("east","B")：采集器单次运行单源不混写；
    某源拒绝/不可达（退出码 3/4）时由编排层按序换源重跑（本次 11-02/11-03
    两缺口键即 east 被日线包络如实拒绝后以 B 补齐的实例）。

治理含义（east 为带额主源）：
  1. **日线仲裁升级为强制门禁**（stock.kline_daily 来自 TDX 官网盘后包，构成
     独立第二源：OHLC 包络 ≤1% + 量/额合计 ≤3%，无当日日线一律 fail-closed
     并提示先跑 daily_kline_pipeline）。
  2. amount 口径分源：east 行 amount=元（与 lc5/日线同口径）；B 行 amount=NULL
     如实入库（表列本就 Nullable，绝不以 0 或公式推算造假）；读回验证含
     countIf(amount IS NOT NULL) 非空计数核对（期望值按写入行实际计算——B 行
     应全 NULL、east 行应全非 NULL），不符即 fail-closed 回滚。
  3. 写路径完全复用 R16 治理：同锁 scope ``kline_minute_backfill_window``
     （与 R16 窗口补全互斥）、同 id 方案（minute_row_id，RUN_TAG 同域）、
     锁内反连接、分批插入、读回验证、精确 id 回滚、journal——因 amount=None
     不能过 R16 formal_write 的聚合求和，本文件以同构镜像实现
     ``formal_write_auto``（差异点仅 amount None 感知，其余全部 import 复用）。

CLI：
  --mode {audit,dry-run,formal-write}
  --trade-date YYYY-MM-DD   缺省自动解析=正式表最近缺口日（自 kline_daily 最新日
                            起向回扫 --max-days+15 个交易日内第一个缺口日）
  --source {auto,east,B,A}  auto=按评估结论（当前 east）；sina 未过评估不可选
  --max-days N              自动解析深度上限（默认 5，公共 API 深度边界）；
                            最近缺口深于上限 → fail-closed，提示 TDX 客户端
                            lc5 + R16 窗口补全兜底或分日补
  --verify-existing N       无缺口/写后抽样 N 只既有键与源逐 bar 比对（只读）
  退出码：0=正常/无事可做；1=参数与门禁 fatal（SystemExit）；
          3=源不可达（全部拉取失败）；4=降级（部分拉取失败或拒绝率>5%）
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils.db_write_lock import DbWriteLock  # noqa: E402
from utils.tdx_lc5 import Lc5FormatError, STANDARD_5M_BAR_ENDS, shares_to_lots  # noqa: E402

# 复用 R16（minute_backfill_window）与 1021_1030 既有实现：import 而非复制
import minute_backfill_1021_1030 as mb  # noqa: E402
import minute_backfill_window as mw     # noqa: E402
from minute_backfill_1021_1030 import (  # noqa: E402
    CN_TZ, INSERT_COLUMNS, PRICE_TOL, VOL_AMT_TOL,
    fetch_calendar_gate, fetch_daily_reference, minute_row_id, rel_diff,
    tencent_m5_fetch,
)
from minute_backfill_window import (  # noqa: E402
    LOCK_SCOPE, TARGET_TABLE, ch_window, fetch_missing_keys,
)

__all__ = [
    "EVALUATED_SOURCE", "SOURCE_FALLBACK_ORDER", "EAST_DEPTH_LIMIT_DAYS",
    "EXIT_SOURCE_UNREACHABLE", "EXIT_DEGRADED",
    "resolve_source", "east_m5_fetch", "trading_days_desc", "resolve_trade_date",
    "check_depth_vs_source", "gate_bars", "fetch_day_bars", "collect_day",
    "classify_outcome", "formal_write_auto", "verify_existing",
    "day_readback_summary", "run", "main",
]

# ── 常量契约 ─────────────────────────────────────────────────

#: 2026-11-09 R22 源评估结论：east=东财 push2his 5m（带 amount 元）为默认源；
#: sina=新浪 m5 评估未过（低流动键缺零量 bar≠48 槽、volume 股且非整百无法
#: 无损入 Int64 手列、个别 bar amount 噪声）不入生产；B=腾讯无额兜底；
#: A=pytdx 协议失效（25 主机）保留实现。证据：EVALUATION_EVIDENCE。
EVALUATED_SOURCE = "east"
EVALUATION_EVIDENCE = r"F:\Aistock\data\data\runtime\minute_source_eval_r22\eval_comparison_r22.json"
#: 源回退顺序（编排层换源重跑用；采集器单次运行单源，不静默混源）
SOURCE_FALLBACK_ORDER = ("east", "B")
#: 东财 push2his 单次 lmt 实测上限 1536 根（600519 lmt=4000 仅回 1536）≈32 交易日
#: → 安全深度 25（探针证据见评估报告 depth_probe）
EAST_DEPTH_LIMIT_DAYS = 25
EAST_LMT = 2000
#: 腾讯 m5 最近 320 根 ≈ 6.67 交易日 → B 源安全深度 6 个交易日
TENCENT_DEPTH_LIMIT_DAYS = 6
DEFAULT_MAX_DAYS = 5
SCAN_EXTRA_TRADING_DAYS = 15     # 超界探测余量（发现更深的最近缺口→fail-closed 提示）
REJECT_ALARM_RATE = 0.05         # 拒绝率超过 5% 视为系统性异常（降级退出码）
CALENDAR_LOOKBACK_DAYS = 92      # 日历扫描窗（自然日）

EXIT_OK, EXIT_FATAL = 0, 1
EXIT_SOURCE_UNREACHABLE, EXIT_DEGRADED = 3, 4

DEFAULT_RUNTIME_BASE = r"F:\Aistock\data\data\runtime\minute_collect_auto"
EAST_HOSTS = [
    "https://push2his.eastmoney.com",
    "https://91.push2his.eastmoney.com",
    "https://push2delay.eastmoney.com",
]
EAST_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
EAST_KLINE_URL = ("{host}/api/qt/stock/kline/get?secid={secid}&klt=5&fqt=0"
                  "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
                  "&end=20500101&lmt={lmt}")
PYTDX_HOSTS = [
    ("180.153.18.170", 7709), ("123.125.108.90", 7709),
    ("47.103.48.45", 7709), ("119.147.212.81", 7709),
]
PYTDX_PAGE = 800


def resolve_source(choice: str) -> str:
    """auto → 按评估结论（R22=east）；显式 east/B/A 原样返回。

    sina 评估未过（48 槽不齐/股非整百/amount 噪声）不提供为生产选项。
    """
    if choice == "auto":
        return EVALUATED_SOURCE
    if choice in ("east", "B", "A"):
        return choice
    raise SystemExit(f"非法 --source: {choice!r}（可选 auto/east/B/A；"
                     f"sina 评估未过不入生产，证据 {EVALUATION_EVIDENCE}）")


# ── 东财 push2his 5m 拉取 ────────────────────────────────────

def _east_secid(code: str) -> str:
    num, suffix = code.split(".")
    return ("1." if suffix.upper() == "SH" else "0.") + num  # SH=1；SZ/BJ=0


def east_m5_fetch(code: str, fetcher: Optional[Callable[[str], dict]] = None,
                  lmt: int = EAST_LMT) -> List[dict]:
    """东财 push2his 5m K 线（不复权，secid=市场.代码）。

    klines 行 "时间,开,收,高,低,量(手),额(元)"——volume 与 lc5 同为手
    （R22 实测与 lc5 量比 median=1.0000）；amount=元、与 lc5/kline_daily
    日合计一致（R22 实测 20/20 ≤0.1%）。任意行缺额（"-"）→ RuntimeError
    fail-closed（源契约破坏），0 根 → RuntimeError "返回 0 根"（无覆盖语义）。
    """
    if fetcher is not None:
        payload = fetcher(code)
    else:
        payload = None
        last_exc: Optional[Exception] = None
        for host in EAST_HOSTS:
            try:
                req = urllib.request.Request(
                    EAST_KLINE_URL.format(host=host, secid=_east_secid(code), lmt=lmt),
                    headers={"User-Agent": EAST_UA,
                             "Referer": "https://quote.eastmoney.com/"})
                payload = json.loads(
                    urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
                break
            except Exception as exc:  # noqa: BLE001
                last_exc, payload = exc, None
        if payload is None:
            raise RuntimeError(f"东财 push2his {code} 拉取失败: {last_exc}")
    node = (payload or {}).get("data") or {}
    klines = node.get("klines")
    if not isinstance(klines, list) or not klines:
        raise RuntimeError(f"东财 push2his {code} 返回 0 根")
    out: List[dict] = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7 or parts[6] in ("-", ""):
            raise RuntimeError(f"东财 {code} kline 行缺 amount: {line[:48]}")
        out.append({"datetime": datetime.strptime(parts[0], "%Y-%m-%d %H:%M"),
                    "open": float(parts[1]), "close": float(parts[2]),
                    "high": float(parts[3]), "low": float(parts[4]),
                    "volume": float(parts[5]), "amount": float(parts[6])})
    return out


# ── 交易日解析（纯函数）──────────────────────────────────────

def trading_days_desc(calendar: Dict[str, int], latest_daily: str,
                      lookback_days: int = CALENDAR_LOOKBACK_DAYS) -> List[str]:
    """自 latest_daily（含）向回扫 lookback_days 个自然日，返回降序交易日列表。"""
    anchor = date.fromisoformat(str(latest_daily))
    out = []
    for k in range(lookback_days):
        d = (anchor - timedelta(days=k)).isoformat()
        if calendar.get(d):
            out.append(d)
    return out


def resolve_trade_date(*, explicit: Optional[str], calendar: Dict[str, int],
                       latest_daily: str, missing_by_day: Dict[str, int],
                       daily_rows_by_day: Dict[str, int],
                       max_days: int) -> Optional[Dict]:
    """解析目标交易日（纯函数；非法即 SystemExit fail-closed）。

    explicit 给定：须为交易日且当日 kline_daily 有行（日线仲裁强制）。
    auto：自 latest_daily 向回第一个缺口日；深于 max_days → SystemExit 提示
    TDX 客户端兜底/分日补。无缺口 → None（无事可做）。
    """
    if explicit:
        day = str(explicit)
        if calendar.get(day) != 1:
            raise SystemExit(f"--trade-date {day} 非交易日（trade_calendar）或不在扫描窗内")
        if int(daily_rows_by_day.get(day, 0) or 0) <= 0:
            raise SystemExit(
                f"{day} 当日日线缺失（stock.kline_daily 无行）：日线仲裁为强制门禁，"
                f"先跑 scripts/daily_kline_pipeline.py --formal-write 再采集 5m")
        days = trading_days_desc(calendar, latest_daily)
        depth = days.index(day) + 1 if day in days else 999
        return {"mode": "explicit", "trade_date": day, "depth": depth,
                "missing": int(missing_by_day.get(day, 0) or 0)}

    days = trading_days_desc(calendar, latest_daily)
    for depth, day in enumerate(days, start=1):
        missing = int(missing_by_day.get(day, 0) or 0)
        if missing <= 0:
            continue
        if depth > int(max_days):
            raise SystemExit(
                f"最近缺口 {day} 深度 {depth} 个交易日 > --max-days {max_days}："
                f"公共 API 深度不足，需 TDX 客户端下载 vipdoc lc5 后走 R16 "
                f"scripts/minute_backfill_window.py 窗口补全兜底，或分日多次补")
        if int(daily_rows_by_day.get(day, 0) or 0) <= 0:
            raise SystemExit(
                f"{day} 当日日线缺失：先跑 scripts/daily_kline_pipeline.py --formal-write")
        return {"mode": "auto", "trade_date": day, "depth": depth, "missing": missing}
    return None


def check_depth_vs_source(source: str, depth: int) -> None:
    """各源可回溯深度边界：east 25（lmt 上限 1536 根≈32 交易日取安全余量）；
    B 6（腾讯 m5 最近 320 根 ≈ 6.7 交易日）；A 协议分页无此限。"""
    if source == "B" and int(depth) > TENCENT_DEPTH_LIMIT_DAYS:
        raise SystemExit(
            f"源 B（腾讯 m5）只覆盖最近 320 根 ≈ 6.7 交易日，目标深度 {depth} 无覆盖；"
            f"需 TDX 客户端 lc5 + R16 窗口补全兜底")
    if source == "east" and int(depth) > EAST_DEPTH_LIMIT_DAYS:
        raise SystemExit(
            f"源 east（东财 push2his）单次最多约 1536 根 ≈ 32 交易日"
            f"（安全深度 {EAST_DEPTH_LIMIT_DAYS}），目标深度 {depth} 无覆盖；"
            f"需 TDX 客户端 lc5 + R16 窗口补全兜底")


# ── bar 门禁与行构造 ─────────────────────────────────────────

def _slots_ok(bars: Sequence[dict], day: str) -> Optional[str]:
    if len(bars) != 48:
        return f"槽数 {len(bars)} != 48"
    stamps = [b["datetime"] for b in bars]
    if any(s.strftime("%Y-%m-%d") != day for s in stamps):
        return "混入他日 bar"
    if {s.time() for s in stamps} != set(STANDARD_5M_BAR_ENDS):
        return "时间戳与标准 5m 网格不一致"
    return None


def _ohlc_sanity(bars: Sequence[dict]) -> Optional[str]:
    for b in bars:
        if min(b["open"], b["high"], b["low"], b["close"]) <= 0:
            return f"非正价格 {b['datetime']:%H:%M}"
        if b["high"] < max(b["open"], b["close"]) or b["low"] > min(b["open"], b["close"]) \
                or b["high"] < b["low"]:
            return f"high/low 值域破坏 {b['datetime']:%H:%M}"
    return None


def envelope_gate_pv(bars: Sequence[dict], daily: Sequence[float]) -> Optional[str]:
    """B 源量价仲裁（日线=独立第二源）：OHLC ≤1%、量合计 ≤3%；无额比较（源无额）。"""
    o, h, l, c = bars[0]["open"], max(b["high"] for b in bars), \
        min(b["low"] for b in bars), bars[-1]["close"]
    d_o, d_h, d_l, d_c, d_v, _d_a = daily
    for name, m, d in (("open", o, d_o), ("high", h, d_h), ("low", l, d_l), ("close", c, d_c)):
        if d <= 0:
            return f"daily_{name}<=0 ({d})"
        if rel_diff(m, d) > PRICE_TOL:
            return f"{name} {m:.4f} vs daily {d:.4f} 超 {PRICE_TOL:.0%}"
    m_v = sum(b["volume"] for b in bars)
    if d_v == 0:
        if m_v != 0:
            return f"daily volume=0 但分钟量合计 {m_v}"
    elif rel_diff(m_v, d_v) > VOL_AMT_TOL:
        return f"volume {m_v} vs daily {d_v} 超 {VOL_AMT_TOL:.0%}"
    return None


def build_row(code: str, dt_naive_bj: datetime, o: float, h: float, l: float, c: float,
              vol_lots: int, amount: Optional[float], created_at: datetime) -> Tuple:
    """构造 INSERT_COLUMNS 顺序行；id 与 R16 同域（minute_row_id）。"""
    return (code, dt_naive_bj.replace(tzinfo=CN_TZ), o, h, l, c, int(vol_lots),
            amount, created_at, minute_row_id(code, dt_naive_bj, "5m"))


def gate_bars(code: str, day: str, bars: Sequence[dict], daily: Optional[Sequence[float]],
              *, source: str, created_at: datetime) -> Tuple[List[Tuple], Optional[str]]:
    """逐 (code, day) 门禁：48 槽 → 值域 → 单位 → 日线仲裁 → 行构造。

    返回 (rows, None) 或 ([], 拒绝原因)。分源口径：
      - east：volume 已是手（整数校验），amount=元必在（缺即 fail-closed），
        日线仲裁含额（量/额合计 ≤3%，复用 1021_1030 envelope_gate）；
      - B：volume 手，amount=None（源无额，如实）；
      - A：volume 股→手（shares_to_lots 整百校验），amount=元直传。
    """
    if daily is None:
        return [], ("日线基准行缺失：日线仲裁为强制门禁，"
                    "先跑 scripts/daily_kline_pipeline.py --formal-write")
    if not bars:
        return [], "no_tencent_coverage" if source == "B" else "no_source_coverage"
    reason = _slots_ok(bars, day)
    if reason:
        return [], f"槽位门禁: {reason}"
    reason = _ohlc_sanity(bars)
    if reason:
        return [], f"值域门禁: {reason}"

    lots: List[int] = []
    if source == "B":
        for b in bars:
            v = float(b["volume"])
            if abs(v - round(v)) > 1e-9:
                return [], f"volume 非整数（手）: {v} 疑似单位错配（fail-closed）"
            lots.append(int(round(v)))
        reason = envelope_gate_pv(bars, daily)
        if reason:
            return [], f"日线仲裁: {reason}"
        rows = [build_row(code, b["datetime"], b["open"], b["high"], b["low"], b["close"],
                          lot, None, created_at) for b, lot in zip(bars, lots)]
        return rows, None

    if source == "east":
        for b in bars:
            v = float(b["volume"])
            if abs(v - round(v)) > 1e-9:
                return [], f"volume 非整数（手）: {v} 疑似单位错配（fail-closed）"
            lots.append(int(round(v)))
            if b.get("amount") is None:
                return [], "单位门禁: east 行缺 amount（源契约破坏，fail-closed）"
        ns_bars = [SimpleNamespace(open=b["open"], high=b["high"], low=b["low"],
                                   close=b["close"], volume=lot, amount=float(b["amount"]))
                   for b, lot in zip(bars, lots)]
        reason = mb.envelope_gate(ns_bars, daily)
        if reason:
            return [], f"日线仲裁: {reason}"
        rows = [build_row(code, b["datetime"], b["open"], b["high"], b["low"], b["close"],
                          lot, float(b["amount"]), created_at)
                for b, lot in zip(bars, lots)]
        return rows, None

    # A：amount 存在 → 直接复用 1021_1030 的 envelope_gate（含额 ≤3%）
    try:
        for b in bars:
            v = float(b["volume"])
            if abs(v - round(v)) > 1e-9:
                raise Lc5FormatError(f"{code}: 源 volume 非整数股 {v}")
            lots.append(shares_to_lots(code, int(round(v))))
    except Lc5FormatError as exc:
        return [], f"单位门禁: {exc}"
    ns_bars = [SimpleNamespace(open=b["open"], high=b["high"], low=b["low"], close=b["close"],
                               volume=lot, amount=float(b["amount"]))
               for b, lot in zip(bars, lots)]
    reason = mb.envelope_gate(ns_bars, daily)
    if reason:
        return [], f"日线仲裁: {reason}"
    rows = [build_row(code, b["datetime"], b["open"], b["high"], b["low"], b["close"],
                      lot, float(b["amount"]), created_at) for b, lot in zip(bars, lots)]
    return rows, None


# ── 源拉取 ───────────────────────────────────────────────────

def fetch_day_bars(code: str, day: str, *, source: str,
                   fetcher: Optional[Callable[[str, List[dict]]]] = None,
                   pytdx_api: Any = None) -> List[dict]:
    """拉取 code 在 day 的 5m bar（统一内部格式：naive 北京时间 dict）。

    east：fetcher(code)→全量近期 bar（默认 east_m5_fetch，volume 手、amount 元）。
    B：fetcher(code)→全量近期 bar（默认 tencent_m5_fetch，volume 已手、无额）。
    A：pytdx get_security_bars(category=0) 分页，volume=股、amount=元。
    拉取传输层失败 → RuntimeError 上抛（调用方记 fetch_error）。
    """
    if source in ("east", "B"):
        bars = fetcher(code) if fetcher else (
            east_m5_fetch(code) if source == "east" else tencent_m5_fetch(code))
        return [{"datetime": b["datetime"], "open": float(b["open"]), "high": float(b["high"]),
                 "low": float(b["low"]), "close": float(b["close"]),
                 "volume": float(b["volume"]),
                 "amount": (None if b.get("amount") is None else float(b["amount"]))}
                for b in bars if b["datetime"].strftime("%Y-%m-%d") == day]

    api = pytdx_api or _connect_pytdx()
    num, suffix = code.split(".")
    market = {"SH": 1, "SZ": 0, "BJ": 2}.get(suffix.upper())
    if market is None:
        raise RuntimeError(f"未知市场后缀: {code}")
    merged: Dict[str, dict] = {}
    for start in range(0, PYTDX_PAGE * 8, PYTDX_PAGE):
        page = api.get_security_bars(0, market, num, start, PYTDX_PAGE) or []
        if not page:
            break
        for b in page:
            dt = datetime.strptime(str(b["datetime"])[:16], "%Y-%m-%d %H:%M")
            merged[dt.strftime("%Y-%m-%d %H:%M")] = {
                "datetime": dt, "open": float(b["open"]), "high": float(b["high"]),
                "low": float(b["low"]), "close": float(b["close"]),
                "volume": float(b["vol"]), "amount": float(b["amount"])}
        oldest = min(merged)
        if oldest[:10] < day:  # 已翻到目标日之前，足够
            break
    return [merged[k] for k in sorted(merged) if k.startswith(day)]


def _connect_pytdx() -> Any:
    """惰性连接 TDX hq（协议 2026-11 实测失效：连接/空包即 RuntimeError）。"""
    from pytdx.hq import TdxHq_API  # 惰性 import：模块导入不得拉起 pytdx 栈
    last: Optional[Exception] = None
    for host, port in PYTDX_HOSTS:
        api = TdxHq_API(raise_exception=True, auto_retry=True)
        try:
            if api.connect(host, port, time_out=6):
                return api
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"pytdx 全部主机连接失败（源 A 不可达）: {last}")


# ── collect_day 编排 ─────────────────────────────────────────

def collect_day(missing_keys: List[Tuple[str, str]], daily_ref: Dict,
                *, source: str, created_at: datetime,
                fetcher: Optional[Callable[[str, List[dict]]]] = None,
                pytdx_api: Any = None,
                fetch_workers: int = 1) -> Dict:
    """对单一交易日的缺失键拉取+门禁。如实报告：拒绝/拉取失败绝不造数。"""
    days = {d for d, _ in missing_keys}
    if len(days) != 1:
        raise SystemExit(f"collect_day 只接受单一交易日，收到 {sorted(days)}")
    day = days.pop()
    by_code: Dict[str, List[str]] = {}
    for d, code in missing_keys:
        by_code.setdefault(code, []).append(d)
    codes = sorted(by_code)

    def one(code: str) -> Tuple[str, Optional[List[dict]], Optional[str]]:
        try:
            return code, fetch_day_bars(code, day, source=source,
                                        fetcher=fetcher, pytdx_api=pytdx_api), None
        except RuntimeError as exc:
            if "返回 0 根" in str(exc):
                # 源已应答但该品种无任何 m5 行（如腾讯不覆盖北交所）：
                # 属"无覆盖"（如实拒绝）而非"源不可达"（报警）
                return code, [], None
            return code, None, f"{type(exc).__name__}: {exc}"

    if fetch_workers and fetch_workers > 1:
        with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
            results = list(pool.map(one, codes))
    else:
        results = [one(c) for c in codes]

    rows: List[Tuple] = []
    rejects: List[dict] = []
    fetch_errors: Dict[str, str] = {}
    for code, bars, err in results:
        if err is not None:
            fetch_errors[code] = err
            continue
        assert bars is not None
        if not bars:
            rejects.append({"code": code, "day": day,
                            "reason": "no_tencent_coverage" if source == "B" else "no_source_coverage"})
            continue
        gated, reason = gate_bars(code, day, bars, daily_ref.get((day, code)),
                                  source=source, created_at=created_at)
        if reason:
            rejects.append({"code": code, "day": day, "reason": reason})
        else:
            rows.extend(gated)
    return {"rows": rows, "rejects": rejects, "fetch_errors": fetch_errors,
            "codes": codes, "fetched_ok": len(codes) - len(fetch_errors)}


def classify_outcome(*, missing: int, fetched_ok: int, rejects,
                     fetch_errors: Dict[str, Any], codes: Sequence[str]) -> int:
    """退出码契约：全失败=3 源不可达；部分失败/拒绝率>5%=4 降级；其余 0。

    rejects 接受 int（计数）或带 len 的集合。
    """
    n_rejects = int(rejects) if isinstance(rejects, int) else len(rejects)
    if codes and fetch_errors and len(fetch_errors) >= len(codes):
        return EXIT_SOURCE_UNREACHABLE
    if fetch_errors:
        return EXIT_DEGRADED
    if missing > 0 and codes and n_rejects / len(codes) > REJECT_ALARM_RATE:
        return EXIT_DEGRADED
    return EXIT_OK


# ── 只读既有数据核验 ─────────────────────────────────────────

def _day_bars_query(client: Any, codes: Sequence[str], ch_win: Sequence[str]) -> List[dict]:
    """窗口内逐 bar 只读查询（200 码分片），返回 dt('%Y-%m-%d %H:%M')+code 行。"""
    out: List[dict] = []
    for i in range(0, len(codes), 200):
        chunk = list(codes)[i:i + 200]
        rows = mb._ro(client, f"""
        SELECT substring(toString(toDateTime(datetime, 'Asia/Shanghai')), 1, 16) AS dt,
               code, open, high, low, close, volume, amount
        FROM {TARGET_TABLE}
        WHERE datetime >= toDateTime('{ch_win[0]}') AND datetime < toDateTime('{ch_win[1]}')
          AND code IN ({mb._sql_str(chunk)})
        ORDER BY datetime""")
        out.extend(rows)
    return out


def _codes_with_bars(client: Any, ch_win: Sequence[str]) -> List[str]:
    rows = mb._ro(client, f"""
    SELECT DISTINCT code FROM {TARGET_TABLE}
    WHERE datetime >= toDateTime('{ch_win[0]}') AND datetime < toDateTime('{ch_win[1]}')""")
    return sorted(str(r["code"]) for r in rows)


def verify_existing(client: Any, trade_date: str, codes: Sequence[str], *,
                    source: str, fetcher: Optional[Callable] = None,
                    pytdx_api: Any = None,
                    price_tol: float = PRICE_TOL, vol_tol: float = VOL_AMT_TOL) -> dict:
    """抽样既有 (code, day) 与源逐 bar 比对（只读；口径同 R16 crosscheck）。

    价逐根 ≤price_tol；量：沪市逐根、深市按日合计（分钟边界口径差异）≤vol_tol。
    """
    ch = ch_window(date.fromisoformat(trade_date), date.fromisoformat(trade_date))
    results, pass_n, fail_n = [], 0, 0
    for code in codes:
        db_rows = _day_bars_query(client, [code], ch)
        # ch_window 单日窗口上界多覆盖一整个北京日（R19 已知陷阱）：必须按目标日过滤，
        # 否则相邻日 bar 按 HH:MM 键覆盖目标日值、日量合计翻倍（R22 生产实测触发回滚）
        ours = {str(r["dt"])[11:16]: r for r in db_rows if str(r["dt"])[:10] == trade_date}
        try:
            src = fetch_day_bars(code, trade_date, source=source,
                                 fetcher=fetcher, pytdx_api=pytdx_api)
        except RuntimeError as exc:
            results.append({"code": code, "status": "fetch_error", "error": str(exc)})
            fail_n += 1
            continue
        theirs = {b["datetime"].strftime("%H:%M"): b for b in src}
        issues = []
        vol_our = sum(float(r["volume"]) for r in ours.values())
        vol_tx = sum(b["volume"] for b in theirs.values())
        amt_our = sum(float(r["amount"]) for r in ours.values() if r["amount"] is not None)
        amt_tx = sum(b["amount"] for b in theirs.values() if b.get("amount") is not None)
        for hm, ob in sorted(ours.items()):
            tb = theirs.get(hm)
            if tb is None:
                issues.append(f"{hm} 源缺根")
                continue
            for field in ("open", "high", "low", "close"):
                if rel_diff(float(ob[field]), tb[field]) > price_tol:
                    issues.append(f"{hm} {field} {ob[field]} vs {tb[field]}")
            if not code.endswith(".SZ") and rel_diff(float(ob["volume"]), tb["volume"]) > vol_tol:
                issues.append(f"{hm} volume {ob['volume']} vs {tb['volume']}")
            # 源带额（east/A）时逐 bar 额比对；B 源无额跳过
            if tb.get("amount") is not None and ob["amount"] is not None:
                if rel_diff(float(ob["amount"]), tb["amount"]) > vol_tol:
                    issues.append(f"{hm} amount {ob['amount']:.0f} vs {tb['amount']:.0f}")
        if code.endswith(".SZ") and rel_diff(vol_our, vol_tx) > vol_tol:
            issues.append(f"日量合计 {vol_our} vs {vol_tx}")
        if amt_tx > 0 and amt_our > 0 and rel_diff(amt_our, amt_tx) > vol_tol:
            issues.append(f"日额合计 {amt_our:.0f} vs {amt_tx:.0f}")
        ok = ours and theirs and not issues
        pass_n += ok
        fail_n += not ok
        results.append({"code": code, "status": "pass" if ok else "mismatch",
                        "db_bars": len(ours), "src_bars": len(theirs),
                        "issues": issues[:8]})
    return {"codes": list(codes), "pass": pass_n, "fail": fail_n, "detail": results}


def day_readback_summary(client: Any, trade_date: str) -> dict:
    """目标日在库只读汇总（读回证据）：逐码 bar 数分布 + amount 非空码数。"""
    ch = ch_window(date.fromisoformat(trade_date), date.fromisoformat(trade_date))
    codes = _codes_with_bars(client, ch)
    if not codes:
        return {"codes": 0, "bars_48": 0, "bars_not_48": {}, "amount_nonnull_codes": 0}
    bars = _day_bars_query(client, codes, ch)
    per_code: Counter = Counter()
    amount_nonnull: set = set()
    for r in bars:
        if str(r["dt"])[:10] != trade_date:   # 同上：单日窗口含相邻日，只统计目标日
            continue
        per_code[str(r["code"])] += 1
        if r["amount"] is not None:
            amount_nonnull.add(str(r["code"]))
    not_48 = {c: n for c, n in per_code.items() if n != 48}
    return {"codes": len(per_code), "total_bars": sum(per_code.values()),
            "bars_48": len(per_code) - len(not_48),
            "bars_not_48": dict(sorted(not_48.items())[:8]),
            "amount_nonnull_codes": len(amount_nonnull)}


# ── formal_write_auto（R16 formal_write 的 amount-None 感知镜像）──

def _want_aggregate(rows: List[Tuple]) -> Dict[Tuple[str, str], Tuple[int, float, float, int]]:
    out: Dict[Tuple[str, str], Tuple[int, float, float, int]] = {}
    for r in rows:
        key = (r[1].astimezone(CN_TZ).strftime("%Y-%m-%d"), r[0])
        n, v, a, an = out.get(key, (0, 0.0, 0.0, 0))
        out[key] = (n + 1, v + r[6], a + (r[7] or 0.0), an + (r[7] is not None))
    return out


def _readback_aggregate_auto(client: Any, keys: List[Tuple[str, str]],
                             ch_win: Sequence[str], final: bool = False
                             ) -> Dict[Tuple[str, str], Tuple[int, float, float, int]]:
    suffix = " FINAL" if final else ""
    codes = sorted({c for _, c in keys})
    out: Dict[Tuple[str, str], Tuple[int, float, float, int]] = {}
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        rows = mb._ro(client, f"""
SELECT toString(toDate(datetime + 28800)) AS d, code, count() AS n,
       sum(volume) AS v, sum(amount) AS a,
       countIf(amount IS NOT NULL) AS amt_nonnull
FROM {TARGET_TABLE}{suffix}
WHERE datetime >= toDateTime('{ch_win[0]}') AND datetime < toDateTime('{ch_win[1]}')
  AND code IN ({mb._sql_str(chunk)}) GROUP BY d, code""")
        for r in rows:
            # B 行全 NULL 时 CH sum(amount) 返回 NULL（R22 生产实测），按 0 聚合；
            # 非空计数 amt_nonnull 才是 NULL 语义的核对载体
            out[(str(r["d"]), str(r["code"]))] = (
                int(r["n"]), float(r["v"] or 0.0), float(r["a"] or 0.0), int(r["amt_nonnull"]))
    return out


def _sample_field_check_auto(client: Any, rows: List[Tuple],
                             rng: random.Random, per_key: int = 25) -> dict:
    """抽样逐字段读回（None 字段跳过——B 行 amount=NULL 是预期态）。"""
    by_key: Dict[Tuple[str, str], List[Tuple]] = {}
    for r in rows:
        by_key.setdefault((r[0], r[1].astimezone(CN_TZ).strftime("%Y-%m-%d")), []).append(r)
    sample_keys = rng.sample(sorted(by_key), min(per_key, len(by_key)))
    problems = []
    for code, day in sample_keys:
        want = {r[1].astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M"): r for r in by_key[(code, day)]}
        got_rows = _day_bars_query(client, [code],
                                   ch_window(date.fromisoformat(day), date.fromisoformat(day)))
        # 只取目标北京日：ch_window 单日上界含相邻日，HH:MM 键会被相邻日 bar 覆盖
        got = {str(r["dt"]): r for r in got_rows if str(r["dt"])[:10] == day}
        if len(got) != len(want):
            problems.append(f"{code} {day} 行数 {len(got)} != {len(want)}")
        for hm, w in want.items():
            g = got.get(hm)
            if g is None:
                problems.append(f"{code} {hm} 缺行")
                continue
            for idx, field in ((2, "open"), (3, "high"), (4, "low"), (5, "close"),
                               (6, "volume"), (7, "amount")):
                if w[idx] is None:
                    continue
                # 写入非 None 而库读回 None = 脏数据，fail-closed（不得 float(None) 崩溃）
                if g[field] is None or rel_diff(float(g[field]), float(w[idx])) > 1e-6:
                    problems.append(f"{code} {hm} {field} {g[field]} != {w[idx]}")
    if problems:
        raise RuntimeError(f"{TARGET_TABLE} 抽样字段校验失败: {problems[:5]}")
    return {"sampled": len(sample_keys), "problems": problems[:8], "problem_count": len(problems)}


def formal_write_auto(client_factory: Callable[[], Any], rows: List[Tuple],
                      ch_win: Sequence[str], journal: Callable[[dict], None],
                      *, lock_owner: str = "aistock-engineer",
                      purpose: str = "", batch_rows: int = 50_000) -> dict:
    """锁内反连接 → 分批插入 → 读回（含 amount 非空计数核对）→ 精确 id 回滚。

    与 R16 formal_write 同构；差异仅两点（amount=None 治理）：
      1) 聚合带 amount 非空计数：B 行应全 NULL，读回非空计数不符=脏数据 fail-closed；
      2) 抽样字段校验跳过 None 字段。rows 为空 → noop（不取锁、不写入）。
    """
    if not rows:
        return {"table": TARGET_TABLE, "status": "noop", "rows": 0, "code_days": 0}
    report = {"table": TARGET_TABLE, "tables": {}, "status": "ok"}
    lock_client = client_factory()
    lock = DbWriteLock(lock_client, journal=journal)
    acquired = lock.acquire(LOCK_SCOPE, lock_owner,
                            purpose=purpose or "分钟 5m 自动采集（R21）")
    journal({"event": "lock_acquired", **{k: acquired[k] for k in ("scope", "owner")}})
    inserted_ids: List[int] = []
    try:
        client = client_factory()
        keys = sorted({(r[1].astimezone(CN_TZ).strftime("%Y-%m-%d"), r[0]) for r in rows})
        existing = mw._existing_keys_nonempty(client, keys, ch_win)
        if existing:
            raise RuntimeError(f"{TARGET_TABLE}: 锁内反连接发现存量键 {len(existing)}，"
                               f"例如 {existing[:5]}；fail-closed 中止")
        journal({"event": "anti_join_clean", "table": TARGET_TABLE, "keys": len(keys)})
        for i in range(0, len(rows), batch_rows):
            batch = rows[i:i + batch_rows]
            client.insert(TARGET_TABLE.split(".")[1], batch, column_names=INSERT_COLUMNS)
            journal({"event": "insert", "table": TARGET_TABLE, "rows": len(batch)})
        inserted_ids = [r[9] for r in rows]

        want = _want_aggregate(rows)
        got = _readback_aggregate_auto(client, sorted(want), ch_win)
        problems = []
        for key, expect in want.items():
            actual = got.get(key)
            if actual is None:
                problems.append(f"{key} 读回缺失")
                continue
            if actual[0] != expect[0] or rel_diff(actual[1], expect[1]) > 1e-6 \
                    or rel_diff(actual[2], expect[2]) > 1e-6:
                problems.append(f"{key} 聚合不一致 expect={expect} got={actual}")
            if actual[3] != expect[3]:
                problems.append(f"{key} amount 非空计数 {actual[3]} != 期望 {expect[3]}"
                                f"（B 行应全 NULL；出现非 NULL amount=脏数据）")
        got_final = _readback_aggregate_auto(client, sorted(want), ch_win, final=True)
        if len(got_final) != len(got):
            problems.append(f"FINAL 键数 {len(got_final)} != raw {len(got)}")
        report["tables"][TARGET_TABLE.split(".")[1]] = {
            "rows": len(rows), "code_days": len(want),
            "readback_problems": problems[:10], "problem_count": len(problems)}
        if problems:
            report["status"] = "verify_failed"
            raise RuntimeError(f"{TARGET_TABLE} 读回验证失败 {len(problems)} 项: {problems[:3]}")
        report["sample_field_check"] = _sample_field_check_auto(
            client, rows, random.Random(20261109))
    except Exception as exc:
        journal({"event": "rollback_start", "error": f"{type(exc).__name__}: {exc}"})
        try:
            rollback_client = client_factory()
            for i in range(0, len(inserted_ids), 5000):
                chunk = inserted_ids[i:i + 5000]
                id_list = ",".join(str(x) for x in chunk)
                rollback_client.command(
                    f"ALTER TABLE {TARGET_TABLE} DELETE WHERE id IN ({id_list}) "
                    f"SETTINGS mutations_sync = 1")
            journal({"event": "rollback_delete", "table": TARGET_TABLE,
                     "ids": len(inserted_ids)})
            if inserted_ids:
                try:
                    id_list = ",".join(str(x) for x in inserted_ids[:5000])
                    left = mb._ro(rollback_client,
                                  f"SELECT count() AS n FROM {TARGET_TABLE} WHERE id IN ({id_list})")
                    journal({"event": "rollback_verify", "table": TARGET_TABLE,
                             "residual_first5000": int(left[0]["n"]) if left else None})
                except Exception:  # noqa: BLE001  残余核对失败不掩盖原始异常
                    pass
            report["status"] = "rolled_back"
        except Exception as rb_exc:  # noqa: BLE001
            report["status"] = "rollback_failed"
            journal({"event": "rollback_error", "error": f"{type(rb_exc).__name__}: {rb_exc}"})
        raise
    finally:
        try:
            lock.release(LOCK_SCOPE, lock_owner)
            journal({"event": "lock_released", "scope": LOCK_SCOPE, "owner": lock_owner})
        except Exception as exc:  # noqa: BLE001
            journal({"event": "lock_release_error", "error": str(exc)})
    return report


# ── run / CLI ────────────────────────────────────────────────

def _write_report(out_dir: Path, report: dict, mode: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"report_{mode}_{ts}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return path


def run(argv: Optional[Sequence[str]] = None, *,
        client_factory: Optional[Callable[[], Any]] = None,
        fetcher: Optional[Callable[[str, List[dict]]]] = None,
        pytdx_factory: Optional[Callable[[], Any]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="5 分钟数据自动采集器（R21；写路径复用 R16 窗口治理，只写 stock.kline_minute_5）")
    parser.add_argument("--mode", choices=("audit", "dry-run", "formal-write"), required=True)
    parser.add_argument("--trade-date", default=None, help="YYYY-MM-DD；缺省=自动解析最近缺口日")
    parser.add_argument("--source", choices=("auto", "east", "B", "A"), default="auto",
                        help="auto=按评估结论（R22=east 东财带额）；B=腾讯无额兜底；"
                             "sina 评估未过不可选")
    parser.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS,
                        help=f"自动解析深度上限（默认 {DEFAULT_MAX_DAYS}，公共 API 深度边界）")
    parser.add_argument("--fetch-workers", type=int, default=2)
    parser.add_argument("--verify-existing", type=int, default=0,
                        help="抽样 N 只既有键与源逐 bar 比对（只读）")
    parser.add_argument("--lock-owner", default="aistock-engineer")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args(argv)

    source = resolve_source(args.source)
    client_factory = client_factory or mb._default_client_factory
    client = client_factory()
    out_dir = Path(args.out_dir) if args.out_dir else Path(DEFAULT_RUNTIME_BASE)

    # ── 上下文（只读）：日线锚点、日历、扫描窗缺口、逐日日线行数
    rows = mb._ro(client, "SELECT max(trade_date) AS m FROM stock.kline_daily")
    if not rows or not rows[0].get("m"):
        raise SystemExit("stock.kline_daily 无数据：先跑 scripts/daily_kline_pipeline.py")
    latest_daily = str(rows[0]["m"])
    cal_days = [(date.fromisoformat(latest_daily) - timedelta(days=k)).isoformat()
                for k in range(CALENDAR_LOOKBACK_DAYS)]
    calendar = fetch_calendar_gate(client, cal_days)
    scan_days = trading_days_desc(calendar, latest_daily)
    scan_win = ch_window(date.fromisoformat(scan_days[-1]), date.fromisoformat(scan_days[0]))
    scan_keys = fetch_missing_keys(client, TARGET_TABLE, scan_days, scan_win)
    missing_by_day: Dict[str, int] = dict(Counter(d for d, _ in scan_keys))
    daily_rows_by_day = {str(r["d"]): int(r["n"]) for r in mb._ro(client, f"""
        SELECT toString(trade_date) AS d, count() AS n FROM stock.kline_daily
        WHERE trade_date IN ({mb._sql_str(scan_days)}) GROUP BY trade_date""")}

    target = resolve_trade_date(explicit=args.trade_date, calendar=calendar,
                                latest_daily=latest_daily,
                                missing_by_day=missing_by_day,
                                daily_rows_by_day=daily_rows_by_day,
                                max_days=args.max_days)
    report: dict = {"mode": args.mode, "source": source, "latest_daily": latest_daily,
                    "evaluated_source": EVALUATED_SOURCE,
                    "source_fallback_order": list(SOURCE_FALLBACK_ORDER),
                    "evaluation_evidence": EVALUATION_EVIDENCE}
    if target is None:
        report.update({"status": "no_gap", "scan_trading_days": len(scan_days),
                       "missing_total": sum(missing_by_day.values())})
        _write_report(out_dir, report, args.mode)
        print(json.dumps(report, ensure_ascii=False, default=str))
        return EXIT_OK

    check_depth_vs_source(source, target["depth"])
    trade_date = target["trade_date"]
    ch = ch_window(date.fromisoformat(trade_date), date.fromisoformat(trade_date))
    keys = [(d, c) for d, c in scan_keys if d == trade_date]
    codes = sorted({c for _, c in keys})
    report["target"] = target

    def _existing_sample() -> List[str]:
        existing = _codes_with_bars(client, ch)
        if not existing:
            return []
        n = int(args.verify_existing)
        if len(existing) <= n:
            return existing
        return random.Random(20261109).sample(existing, n)  # 确定性抽样

    if args.mode == "audit":
        report.update({"status": "audit", "missing_keys": len(keys),
                       "distinct_codes": len(codes)})
        _write_report(out_dir, report, args.mode)
        print(json.dumps(report, ensure_ascii=False, default=str))
        return EXIT_OK

    daily_ref = fetch_daily_reference(client, codes, [trade_date]) if codes else {}
    collected = (collect_day(keys, daily_ref, source=source,
                             created_at=datetime.now(timezone.utc),
                             fetcher=fetcher, fetch_workers=args.fetch_workers)
                 if codes else {"rows": [], "rejects": [], "fetch_errors": {},
                                "codes": [], "fetched_ok": 0})
    rows_5m = collected["rows"]
    report["plan"] = {"rows": len(rows_5m),
                      "code_days": len({(r[0], r[1].astimezone(CN_TZ).strftime("%Y-%m-%d"))
                                        for r in rows_5m})}
    report["rejects"] = collected["rejects"][:50]
    report["rejects_total"] = len(collected["rejects"])
    report["fetch_errors"] = {k: v[:120] for k, v in
                              list(collected["fetch_errors"].items())[:20]}
    report["fetch_errors_total"] = len(collected["fetch_errors"])

    exit_code = classify_outcome(missing=len(keys), fetched_ok=collected["fetched_ok"],
                                 rejects=len(collected["rejects"]),
                                 fetch_errors=collected["fetch_errors"],
                                 codes=collected["codes"])

    if args.mode == "dry-run":
        report["status"] = "dry_run_ok" if rows_5m else "no_rows"
        if args.verify_existing > 0:
            sample = _existing_sample()
            if sample:
                report["verify_existing"] = verify_existing(
                    client, trade_date, sample, source=source, fetcher=fetcher)
        _write_report(out_dir, report, args.mode)
        print(json.dumps(report, ensure_ascii=False, default=str))
        return exit_code

    # formal-write
    out_dir.mkdir(parents=True, exist_ok=True)
    if rows_5m:
        journal = mb._journal_writer(out_dir / f"journal_{trade_date}.jsonl")
        report["formal_write"] = formal_write_auto(
            client_factory, rows_5m, ch, journal,
            lock_owner=args.lock_owner,
            purpose=f"R21 5m 自动采集 {trade_date} 源={source}")
    elif not keys:
        report["status"] = "nothing_to_do"
    else:
        report["status"] = ("source_unreachable" if exit_code == EXIT_SOURCE_UNREACHABLE
                            else "no_rows")
    if args.verify_existing > 0:
        sample = _existing_sample()
        if sample:
            report["verify_existing"] = verify_existing(
                client, trade_date, sample, source=source, fetcher=fetcher)
    report["readback"] = day_readback_summary(client, trade_date)
    path = _write_report(out_dir, report, args.mode)
    print(json.dumps({**report, "_report_path": str(path)}, ensure_ascii=False, default=str))
    return exit_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
