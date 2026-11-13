# -*- coding: utf-8 -*-
"""TDX 本地 lday 日线读取器（Owner 2026-11-13 裁决：日线改走客户端本地文件）。

数据源：``D:/TDX/vipdoc/{sh,sz,bj}/lday/*.day``（通达信客户端盘后下载产物）。

文件格式（32 字节/记录，小端；2026-11-13 用当日官网包入库数据实测定案）：

    offset 0   date      u32  YYYYMMDD
    offset 4   open      i32  价格×100（sh600000 2026-11-13: 1019 → 10.19；既非
                             float32 也非 ×1000——任务书原"float32"表述经实测修正）
    offset 8   high      i32  ×100
    offset 12  low       i32  ×100
    offset 16  close     i32  ×100
    offset 20  amount    f32  元（float32 仅 ~7 位有效数字，十亿元级额有百元级舍入）
    offset 24  vol       i32  个股=股（÷100→手，与官网包同纲，含科创板）；
                             指数=原值不换算（sh000001 实测 472293657 与库内逐值相等）
    offset 28  reserved  i32  保留（指数文件常见非 0，忽略）

宇宙过滤复用 scripts/tdx_lc5_loader.py 规则：is_a_share_stock（沪 600/601/603/
605/688/689、深 000/001/002/003/300/301、北 92）+ MAINTAINED_INDICES 冻结 10 指数
（与 daily_kline_pipeline.FROZEN_INDEX_CODES 同集）。

时序门禁（fail-closed）：只从「含当日记录 且 mtime ≥ 当日 16:00 北京时间」的
文件取数——mtime 即客户端盘后下载完成证据；不满足的文件逐个排除并如实计数，
防止拿昨日文件或盘中半日文件写今日（无当日记录的文件结构上取不到行，双保险）。
"""

from __future__ import annotations

import hashlib
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

BEIJING_TZ = timezone(timedelta(hours=8))

# 通达信客户端盘后下载就绪时刻（Owner 2026-11-13 裁决：服务器 15:30~16:00 就绪，
# 客户端任务 ≥16:00 触发；mtime 早于 16:00 视为下载未完成/盘中残留）
POSTCLOSE_READY_CLOCK = 16 * 3600

RECORD = struct.Struct("<Iiiiifii")  # date, open, high, low, close, amount, vol, reserved
RECORD_SIZE = RECORD.size            # 32

MARKET_BY_DIR = {"sh": "SH", "sz": "SZ", "bj": "BJ"}

LDAY_MARKETS = ("sh", "sz", "bj")


def _proj_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _import_universe_rules():
    """复用 tdx_lc5_loader 的 A股/指数过滤规则（scripts 包内，双路径可导入）。"""
    try:
        from scripts.tdx_lc5_loader import MAINTAINED_INDICES, is_a_share_stock
    except ImportError:  # 以脚本方式直跑时 sys.path[0]=scripts/
        import sys
        root = str(_proj_root())
        if root not in sys.path:
            sys.path.insert(0, root)
        from scripts.tdx_lc5_loader import MAINTAINED_INDICES, is_a_share_stock
    return is_a_share_stock, MAINTAINED_INDICES


def parse_lday_bytes(data: bytes) -> list[dict[str, Any]]:
    """整文件字节 → 记录列表（升序）。文件长度非 32 倍数直接抛错（fail-closed）。"""
    if len(data) % RECORD_SIZE:
        raise ValueError(
            f"lday 文件长度 {len(data)} 不是 {RECORD_SIZE} 的倍数（损坏/非 lday 格式）")
    out: list[dict[str, Any]] = []
    for offset in range(0, len(data), RECORD_SIZE):
        (ymd, open_i, high_i, low_i, close_i, amount_f, vol, _reserved
         ) = RECORD.unpack_from(data, offset)
        out.append({
            "date": int(ymd),
            "open": open_i / 100.0,
            "high": high_i / 100.0,
            "low": low_i / 100.0,
            "close": close_i / 100.0,
            "amount": float(amount_f),
            "vol": int(vol),
        })
    return out


def read_day_file(path: Path, trade_date: str) -> Optional[dict[str, Any]]:
    """读单文件当日记录。返回 {open..close, amount, vol, prev_close} 或 None。

    prev_close 取文件内当日记录的前一条 close（不存在则 0.0——管道对
    prev_close=0 的处理是派生字段置 0，与官网包路径一致）。
    """
    ymd = int(trade_date.replace("-", ""))
    data = path.read_bytes()
    records = parse_lday_bytes(data)
    prev_close = 0.0
    found: Optional[dict[str, Any]] = None
    for rec in records:
        if rec["date"] == ymd:
            found = rec
            break
        prev_close = rec["close"]
    if found is None:
        return None
    found["prev_close"] = prev_close
    return found


def _mtime_beijing(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=BEIJING_TZ)


def fresh_deadline(trade_date: str) -> datetime:
    """当日 16:00 北京时间（mtime 门禁阈值）。"""
    day = datetime.strptime(trade_date, "%Y-%m-%d")
    return day.replace(hour=16, minute=0, second=0, tzinfo=BEIJING_TZ)


def scan_lday_universe(vipdoc_root: str | Path, trade_date: str,
                       *, clock_fn=time.time) -> dict[str, Any]:
    """扫描三市 lday 目录 → 当日契约行（含时序门禁过滤）+ 证据统计。

    返回：
      rows        契约行（与 normalize_to_contract 输出同构；volume 已按
                  个股股÷100→手 / 指数原值 归一；name 置空，lday 无名称）
      timing      门禁证据统计（files_total / universe_files / has_today /
                  fresh / stale_mtime_excluded / no_today_record / zero_volume）
      digest      输入指纹（sha256 over 文件名+mtime+大小+当日值，进 lineage）
    """
    is_a_share_stock, maintained = _import_universe_rules()
    root = Path(vipdoc_root)
    deadline = fresh_deadline(trade_date)
    now = clock_fn()

    rows: list[dict[str, Any]] = []
    evidence: list[str] = []
    stats = {"files_total": 0, "universe_files": 0, "has_today": 0,
             "fresh": 0, "stale_mtime_excluded": 0, "no_today_record": 0,
             "zero_volume": 0, "excluded_non_universe": 0, "read_errors": []}

    for market_dir in LDAY_MARKETS:
        suffix = MARKET_BY_DIR[market_dir]
        directory = root / market_dir / "lday"
        if not directory.is_dir():
            stats["read_errors"].append(f"missing dir: {directory}")
            continue
        for path in sorted(directory.glob("*.day")):
            stats["files_total"] += 1
            bare = path.stem[len(market_dir):]
            full_code = f"{bare}.{suffix}"
            if not (is_a_share_stock(full_code) or full_code in maintained):
                stats["excluded_non_universe"] += 1
                continue
            stats["universe_files"] += 1
            try:
                mtime = _mtime_beijing(path)
                rec = read_day_file(path, trade_date)
            except (OSError, ValueError) as exc:
                stats["read_errors"].append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            evidence.append(f"{path.name}|{mtime.isoformat()}|{path.stat().st_size}")
            if rec is None:
                stats["no_today_record"] += 1
                continue
            stats["has_today"] += 1
            if mtime < deadline:
                # 时序门禁 fail-closed：有当日记录但 mtime 早于 16:00 → 拒用该文件
                stats["stale_mtime_excluded"] += 1
                continue
            stats["fresh"] += 1
            is_index = full_code in maintained
            raw_vol = rec["vol"]
            if raw_vol <= 0:
                stats["zero_volume"] += 1          # 停牌/无成交（含防御量纲破坏的
                continue                           # 有价无量指数），与官网包口径一致地排除
            evidence.append(
                f"{full_code}|{rec['open']}|{rec['high']}|{rec['low']}|{rec['close']}"
                f"|{rec['amount']}|{raw_vol}")
            rows.append({
                "code": full_code,
                "market": suffix.lower(),
                "date": trade_date,
                "name": "",
                "prev_close": round(float(rec["prev_close"]), 4),
                "open": round(rec["open"], 4),
                "high": round(rec["high"], 4),
                "low": round(rec["low"], 4),
                "close": round(rec["close"], 4),
                "volume": round(raw_vol / 100.0, 2) if not is_index else round(float(raw_vol), 2),
                "volume_unit": "lots",
                "volume_raw": raw_vol,
                "volume_raw_unit": "lots" if is_index else "shares",
                "volume_divisor": 1 if is_index else 100,
                "amount": round(rec["amount"], 2),
                "amount_unit": "CNY",
                "amount_available": True,
                "is_index": is_index,
            })

    digest = hashlib.sha256(("\n".join(evidence) + f"\ntrade_date={trade_date}"
                             + f"\nscan_ts={now}").encode("utf-8")).hexdigest()
    return {"rows": rows, "timing": stats, "digest": digest,
            "fresh_deadline": deadline.isoformat()}


def scan_lc5_universe_files(vipdoc_root: str | Path) -> set[str]:
    """fzline 目录宇宙文件扫描（不读内容、不查新鲜度）——lc5 覆盖缺口对齐用。"""
    is_a_share_stock, maintained = _import_universe_rules()
    root = Path(vipdoc_root)
    codes: set[str] = set()
    for market_dir in LDAY_MARKETS:
        suffix = MARKET_BY_DIR[market_dir]
        directory = root / market_dir / "fzline"
        if not directory.is_dir():
            continue
        for path in directory.glob("*.lc5"):
            bare = path.stem[len(market_dir):]
            full_code = f"{bare}.{suffix}"
            if is_a_share_stock(full_code) or full_code in maintained:
                codes.add(full_code)
    return codes


def vwap_unit_check(rows: list[dict[str, Any]], *, sample_size: int = 50,
                    band_tolerance: float = 0.005) -> dict[str, Any]:
    """vwap 反推法抽样验证 vol 单位（个股）：amount÷vol(股) 均价须落回 [low, high]。

    股假设：vwap_shares = amount / vol_raw；手假设：vwap_lots = amount / (vol_raw*100)
    （后者若成立则前者放大 100 倍必然冲出区间）。指数行不参与（指数 volume 为
    原值口径，vwap 反推不适用，量纲由与正式表全量对照裁决）。
    """
    stock_rows = [r for r in rows if not r.get("is_index") and r["volume_raw"] > 0]
    if not stock_rows:
        return {"status": "fail", "reason": "无个股样本可验证", "samples": []}
    step = max(1, len(stock_rows) // max(1, sample_size))
    samples = stock_rows[::step][:sample_size] if sample_size else stock_rows
    entries = []
    shares_ok = lots_ok = 0
    for row in samples:
        vol_raw = float(row["volume_raw"])
        vwap_shares = row["amount"] / vol_raw
        vwap_lots = row["amount"] / (vol_raw * 100.0)
        lo, hi = row["low"] * (1 - band_tolerance), row["high"] * (1 + band_tolerance)
        in_shares = lo <= vwap_shares <= hi
        in_lots = lo <= vwap_lots <= hi
        shares_ok += in_shares
        lots_ok += in_lots
        entries.append({"code": row["code"], "vwap_shares": round(vwap_shares, 4),
                        "vwap_lots": round(vwap_lots, 4), "low": row["low"],
                        "high": row["high"], "shares_hypothesis_ok": in_shares,
                        "lots_hypothesis_ok": in_lots})
    total = len(entries)
    shares_ratio = shares_ok / total
    ok = shares_ratio >= 0.9 and lots_ok == 0
    return {
        "status": "pass" if ok else "fail",
        "unit_verdict": "shares(vol÷100→lots)" if ok else "INCONCLUSIVE",
        "sampled": total, "shares_hypothesis_pass": shares_ok,
        "lots_hypothesis_pass": lots_ok,
        "shares_pass_ratio": round(shares_ratio, 4),
        "band_tolerance": band_tolerance,
        "samples": entries[:100],
        "note": "不一致即 fail——不得猜换算，停下报告（Owner 2026-11-13 B5 裁决）",
    }
