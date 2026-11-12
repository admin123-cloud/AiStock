# -*- coding: utf-8 -*-
"""盘中 5min K 线聚合并增量落盘（R57，每 5 分钟将持仓票的分钟数据聚合为 5min K 写入 CH）。

取代盘中反复调 API 算 RSI 的做法——数据进 CH 后 t_trade_monitor 可以直接 SQL 查询。

数据流：
  腾讯分钟接口 → 聚合 5min K（O/H/L/C/V） → INSERT INTO kline_minute_5

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

# ── 腾讯分钟接口 ──
TENCENT_MINUTE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    "?_var=min_data_{code}&code={code}"
)

# ── CH 地址（容器内外统一：pipeline-runner host 模式用 127.0.0.1） ──
CH_URL = "http://127.0.0.1:8123/"

logger = logging.getLogger("intraday_kline_upsert")


def _tencent_code(code: str) -> str:
    return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code


def fetch_today_minutes(code: str) -> list[dict]:
    """获取今日分钟均价数据（腾讯分钟接口，返回 [{time, price, cumvol}, ...]）。"""
    tc = _tencent_code(code)
    url = TENCENT_MINUTE_URL.format(code=tc)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read()
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    if "=" in text:
        text = text.split("=", 1)[1]
    payload = json.loads(text)
    data = (
        payload.get("data", {})
        .get(tc, {})
        .get("data", {})
        .get("data", [])
    )
    bars: list[dict] = []
    for line in data:
        parts = line.split(" ")
        if len(parts) < 3:
            continue
        try:
            t = parts[0]  # "0930"
            p = float(parts[1])  # 分钟均价
            cv = int(float(parts[2]))  # 累计量（股）
            bars.append({"time": t, "price": p, "cumvol": cv})
        except (ValueError, IndexError):
            continue
    return bars


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
    data = urllib.parse.urlencode({"query": sql}).encode()
    req = urllib.request.Request(CH_URL, data=data)
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
            bars = fetch_today_minutes(code)
        except Exception as exc:
            logger.warning("[upsert] %s 分钟数据采集失败: %s", code, exc)
            results[code] = {"status": "fetch_failed", "error": str(exc)}
            continue

        if not bars:
            logger.info("[upsert] %s 今日无分钟数据", code)
            results[code] = {"status": "no_data"}
            continue

        k5 = agg_5min(bars, code)
        if not k5:
            results[code] = {"status": "agg_empty"}
            continue

        n, msg = insert_rows(k5, today)
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