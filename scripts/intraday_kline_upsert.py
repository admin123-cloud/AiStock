# -*- coding: utf-8 -*-
"""盘中 5min K 线聚合并增量落盘（R57，每 5 分钟将持仓票的分钟数据聚合为 5min K 写入 CH）。

⚠️ 盘中临时方案（2026-11-13）：腾讯 ifzq.gtimg.cn 分钟接口返回 302，
改用腾讯 qt 快照（qt.gtimg.cn）每 5 分钟采样一次实时价/量写入。
缺陷：O/H/L = 当前 snapshot 价（非真实聚合），volume = 累计手数。
盘后正式管道（TDX lc5）会覆盖当日数据。
TODO: 接入可用替代源做真实 5min 聚合后再废除本临时方案。

取代盘中反复调 API 算 RSI 的做法——数据进 CH 后 t_trade_monitor 可以直接 SQL 查询。

数据流（临时）：
  腾讯 qt 快照 → 采样 O/H/L=当前价 → INSERT INTO kline_minute_5
数据流（正式，盘后）：
  TDX lc5 文件 → build_tdx_minute_periods.py 覆盖写入

风险：
  - 写入用 ReplacingMergeTree + id=cityHash64(code, datetime) 去重
  - 盘中写入不可靠时收盘后正式管道覆盖
  - 只写监控清单内的持仓票（不超过 20 只），不写全市场

调用方式（被 pipeline_scheduler 每 5 分钟调起）：
  python intraday_kline_upsert.py

退出码：0=ok 3=源不可达 1=其他失败
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

BEIJING_TZ = timezone(timedelta(hours=8))

# ── 监控清单（持仓 + watchlist，6 位码格式） ──
MONITOR_CODES = ["002015", "300054", "603881"]

# ── 腾讯 qt 接口（实时快照获取最新价/量；盘中每5分钟采样做5min K线） ──
QT_URL = "http://qt.gtimg.cn/q={code}"

# ── 东财 push2 实时快照（备用） ──
EM_SNAPSHOT_URL = (
    "http://push2.eastmoney.com/api/qt/stock/get"
    "?secid={market}.{code}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f170"
)

# ── CH 地址（容器内外统一：pipeline-runner host 模式用 127.0.0.1） ──
CH_URL = "http://127.0.0.1:8123/"

logger = logging.getLogger("intraday_kline_upsert")


SN_CACHE: dict[str, dict] = {}


def _tencent_code(code: str) -> str:
    return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code


def _em_market(code: str) -> int:
    return 1 if code.startswith(("6", "9")) else 0


def _parse_qt(text: str) -> dict | None:
    """解析腾讯 qt 实时行情返回。"""
    try:
        if "=" not in text:
            return None
        raw = text.split("=", 1)[1].strip('"').strip(";")
        parts = raw.split("~")
        if len(parts) < 50:
            return None
        fields = {
            "name": parts[1],
            "code": parts[2],
            "price": float(parts[3]) if parts[3] else None,
            "pre_close": float(parts[4]) if parts[4] else None,
            "open": float(parts[5]) if parts[5] else None,
            "volume": int(parts[6]) if parts[6] else 0,  # 手
            "bid_vol": int(parts[7]) if parts[7] else 0,
            "ask_vol": int(parts[8]) if parts[8] else 0,
            "high": float(parts[33]) if parts[33] else None,
            "low": float(parts[34]) if parts[34] else None,
            "amount": float(parts[37]) if parts[37] else 0.0,
        }
        return fields
    except (ValueError, IndexError):
        return None


def fetch_qt_snapshot(code: str) -> dict | None:
    """腾讯 qt 实时快照。"""
    tc = _tencent_code(code)
    url = QT_URL.format(code=tc)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("gbk", errors="replace")
    except Exception:
        # fallback 东财 push2
        mkt = _em_market(code)
        url2 = EM_SNAPSHOT_URL.format(market=mkt, code=code)
        req2 = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                data = json.loads(resp2.read())
                d = data.get("data", {})
                if not d:
                    return None
                return {
                    "name": code,
                    "code": code,
                    "price": d.get("f43"),
                    "pre_close": d.get("f44"),
                    "open": d.get("f45"),
                    "high": d.get("f44"),
                    "low": d.get("f44"),
                    "volume": d.get("f47", 0) // 100 if d.get("f47") else 0,
                    "amount": d.get("f48", 0.0),
                }
        except Exception:
            return None
    return _parse_qt(text)


def agg_5min(bars: list[dict], code: str) -> list[dict]:
    """分钟数据 → 5min K 线（含 id=cityHash64 去重）。"""
    if not bars:
        return []
    klines: list[dict] = []
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    for i in range(0, len(bars), 5):
        chunk = bars[i:i + 5]
        t0 = chunk[0]["time"]  # "0930"
        hh, mm = t0[:2], t0[2:]
        opens = chunk[0]["price"]
        close = chunk[-1]["price"]
        high = max(b["price"] for b in chunk)
        low = min(b["price"] for b in chunk)
        prev_cv = bars[i - 1]["cumvol"] if i > 0 else 0
        vol_shares = chunk[-1]["cumvol"] - prev_cv
        vol_shares = max(vol_shares, 0)
        vol_shou = round(vol_shares / 100)  # 手

        # id=cityHash64(code, datetime) 近似
        dt_str = f"{hh}:{mm}:00"
        id_src = f"{code}_{dt_str}"
        row_id = abs(hash(id_src)) % (10 ** 15)

        klines.append({
            "code": code,
            "datetime": dt_str,
            "open": round(opens, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": vol_shou,
            "amount": round(vol_shares * close, 2),
            "created_at": now_str,
            "id": row_id,
        })
    return klines


def insert_rows(rows: list[dict], date_str: str) -> tuple[int, str]:
    """INSERT INTO kline_minute_5。返回 (写入行数, 状态)。"""
    if not rows:
        return 0, "no_data"

    values: list[str] = []
    for r in rows:
        dt_full = f"{date_str} {r['datetime']}"
        values.append(
            f"('{r['code']}', '{dt_full}', "
            f"{r['open']}, {r['high']}, {r['low']}, {r['close']}, "
            f"{r['volume']}, {r['amount']}, '{r['created_at']}', {r['id']})"
        )

    sql = (
        "INSERT INTO stock.kline_minute_5 "
        "(code, datetime, open, high, low, close, volume, amount, created_at, id) VALUES "
        + ",".join(values)
    )

    import urllib.parse
    req = urllib.request.Request(CH_URL, data=sql.encode("utf-8"))
    req.add_header("Content-Type", "text/plain")
    try:
        urllib.request.urlopen(req, timeout=30)
        return len(rows), "ok"
    except Exception as exc:
        return 0, str(exc)


def run_upsert(codes: Optional[list[str]] = None) -> dict:
    """主入口：采集 → 聚合 → 写入 → 回验。返回 {code: {status, rows, ...}}。"""
    codes = codes or MONITOR_CODES
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    results: dict[str, dict] = {}

    for code in codes:
        try:
            snap = fetch_qt_snapshot(code)
        except Exception as exc:
            logger.warning("[upsert] %s 快照采集失败: %s", code, exc)
            results[code] = {"status": "fetch_failed", "error": str(exc)}
            continue

        if not snap or snap.get("price") is None:
            logger.info("[upsert] %s 无快照数据", code)
            results[code] = {"status": "no_data"}
            continue

        # 用快照数据构建一条 5min K 线（每5分钟采样视为一根完整K线）
        now_t = datetime.now(BEIJING_TZ)
        hhmm = now_t.strftime("%H:%M")
        dt_str = f"{hhmm}:00"
        price = snap["price"]
        vol = snap.get("volume", 0)
        amount = snap.get("amount", 0.0)
        row_id = abs(hash(f"{code}_{dt_str}")) % (10 ** 15)

        rows = [{
            "code": code,
            "datetime": dt_str,
            "open": round(price, 2),
            "high": round(price, 2),
            "low": round(price, 2),
            "close": round(price, 2),
            "volume": vol,
            "amount": round(amount, 2),
            "created_at": now_t.strftime("%Y-%m-%d %H:%M:%S"),
            "id": row_id,
        }]

        n, msg = insert_rows(rows, today)
        if msg == "ok":
            logger.info("[upsert] %s 写入 %d 根 5min K", code, n)
            results[code] = {"status": "ok", "rows": n}
        else:
            logger.warning("[upsert] %s 写入失败: %s", code, msg)
            results[code] = {"status": "insert_failed", "error": msg}

    return results


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    try:
        results = run_upsert()
        n_total = sum(
            r.get("rows", 0) for r in results.values()
            if r.get("status") == "ok"
        )
        n_fail = sum(
            1 for r in results.values() if r.get("status", "").endswith("_failed")
        )
        logger.info(
            "[upsert] 完成: 写入 %d / 失败 %d / 无数据 %d",
            n_total, n_fail,
            sum(1 for r in results.values() if "no_data" in r.get("status", "")),
        )
        return 0 if n_fail == 0 else 1
    except Exception:
        logger.exception("[upsert] 致命错误")
        return 3


if __name__ == "__main__":
    sys.exit(main())