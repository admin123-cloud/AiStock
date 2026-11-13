# -*- coding: utf-8 -*-
"""盘中 5min K 线写入（R57→R58，2026-11-13 腾讯真源改造）。

历史：2026-11-13 事故日用 qt 快照采样硬扛（O=H=L=C 失真、VWAP 不可用），
现切换为真实分钟 bar 主源。

数据流（主源，盘中每 5 分钟 + 启动当日回补）：
  utils/market_sources/tencent_kline（vendored a-stock-data V3.10.3，
  proxy.finance.qq.com 通道，真实 OHLC）m5 count=320（回看 ≈6.7 天）
    → 只写**当日已收盘** bar（datetime=周期结束边界，边界 > 北京当前时刻即未收盘，丢弃）
    → INSERT INTO stock.kline_minute_5（裸码命名空间，与盘后 TDX 后缀码命名空间共存；
       ReplacingMergeTree ORDER BY(code,datetime) 同键重写幂等，最新 created_at 胜出）
数据流（fallback，主源失败时）：
  腾讯 qt 快照采样（旧临时方案，O=H=L=C=现价失真，status 标 fallback_qt 降级运行）
数据流（盘后权威覆盖）：
  TDX lc5 → build_tdx_minute_periods.py 写后缀码（300054.SZ）命名空间

字段口径：
  - volume：腾讯 m5 原始「手」（688/689 已在适配层 ÷100 统一为手）；最近一日为整手，
    次日才回填精确股数（对账量容差须覆盖整手舍入）。
  - amount：腾讯无成交额字段 → **估算值 = close × volume(手) × 100**（假设整根 bar 以
    close 成交；真实 VWAP≠close 时按 |vwap-close|/close 比例偏差，仅作盘中参考，盘后
    TDX 管道在后缀码命名空间写真实额）。估算口径以 estimate_amount() 文档为准。

对账（写后自动）：与 kline_minute_5 中已有 TDX 盘后数据（后缀码命名空间）重叠日逐 bar
对比（OHLC 容差 0.1% / 量容差 3%），不一致如实落报告
runtime/intraday_kline_upsert/reconcile_*.json（不猜、不修数）。

调用方式（被 pipeline_scheduler 每 5 分钟调起）：
  python intraday_kline_upsert.py

退出码：0=ok 3=源不可达 1=其他失败
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BEIJING_TZ = timezone(timedelta(hours=8))

# ── 监控清单（持仓 + watchlist，6 位码格式） ──
MONITOR_CODES = ["002015", "300054", "603881"]

# ── 腾讯 qt 接口（fallback 实时快照） ──
QT_URL = "http://qt.gtimg.cn/q={code}"

# ── 东财 push2 实时快照（fallback 的 fallback） ──
EM_SNAPSHOT_URL = (
    "http://push2.eastmoney.com/api/qt/stock/get"
    "?secid={market}.{code}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f170"
)

# ── CH 地址（容器内外统一：pipeline-runner host 模式用 127.0.0.1） ──
CH_URL = "http://127.0.0.1:8123/"

# m5 回看窗口 ≈6.7 天（320 根）；写入只落当日，历史窗口留给对账
M5_FETCH_COUNT = 320

# 对账容差（与任务书一致）：价 0.1%、量 3%
RECON_PRICE_TOL = 0.001
RECON_VOLUME_TOL = 0.03

# 盘中写入行的确定性 id 域（进程无关幂等；替代事故日的 abs(hash()) 随机 id）
ID_DOMAIN = "r58-intraday-m5-v1"

logger = logging.getLogger("intraday_kline_upsert")


def _tencent_code(code: str) -> str:
    return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code


def _code_with_suffix(code: str) -> str:
    """TDX 盘后命名空间的后缀码（对账用）。"""
    return f"{code}.SH" if code.startswith(("6", "9", "5")) else f"{code}.SZ"


def bar_row_id(code: str, dt_text: str) -> int:
    """确定性 id：blake2b(domain|code|datetime) 前 8 字节 → UInt64。

    与 build_tdx_minute_periods._stable_id 同一构造方式（域区分命名空间），
    同一 (code, datetime) 每次算出同一 id → 重跑幂等。
    """
    digest = hashlib.blake2b(
        f"{ID_DOMAIN}|{code}|{dt_text}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=False)


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
    """腾讯 qt 实时快照（fallback 数据源）。"""
    tc = _tencent_code(code)
    url = QT_URL.format(code=tc)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("gbk", errors="replace")
    except Exception:
        # fallback 东财 push2（2026-11-13 起 https+http 均被 WAF 掐，多半不可用）
        mkt = 1 if code.startswith(("6", "9")) else 0
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


def fetch_tencent_m5(code: str) -> dict:
    """主源：vendored tencent_kline m5（真实 OHLC，~6.7 天窗口）。"""
    from utils.market_sources import tencent_kline
    return tencent_kline.fetch_minute_kline(code, "m5", M5_FETCH_COUNT)


def closed_today_bars(rows: list[dict], today: str, now: datetime) -> list[dict]:
    """腾讯 m5 rows → 当日已收盘 bar 列表。

    - datetime 是周期结束边界（'YYYY-MM-DD HH:MM'）：边界时刻 > now 即未收盘，丢弃；
    - 只保留当日（回补窗口 ~6.7 天里只落当日，历史日由 TDX 盘后管道权威覆盖）。
    """
    out = []
    for row in rows:
        dt_text = str(row["datetime"])[:16]
        if not dt_text.startswith(today):
            continue
        boundary = datetime.strptime(dt_text, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING_TZ)
        if boundary > now:
            continue
        out.append(row)
    return out


def estimate_amount(close: float, volume_lots: float) -> float:
    """估算成交额（元）= close × volume(手) × 100。

    口径：假设整根 bar 全部以 close 成交（腾讯 m5 无成交额字段）；
    真实 VWAP≠close 时按 |vwap-close|/close 比例偏差。仅盘中参考，
    盘后 TDX 管道在后缀码命名空间写入真实额。
    """
    return round(close * volume_lots * 100.0, 2)


def build_insert_rows(bars: list[dict], code: str, created_at: str) -> list[dict]:
    """当日已收盘 bar → kline_minute_5 行（裸码命名空间 + 确定性 id + 估算额）。"""
    rows = []
    for bar in bars:
        dt_text = f"{str(bar['datetime'])[:16]}:00"
        volume = int(round(float(bar["volume"])))
        close = float(bar["close"])
        rows.append({
            "code": code,
            "datetime": dt_text,
            "open": round(float(bar["open"]), 2),
            "high": round(float(bar["high"]), 2),
            "low": round(float(bar["low"]), 2),
            "close": round(close, 2),
            "volume": volume,
            # 估算口径见 estimate_amount 文档（不可当真实额用）
            "amount": estimate_amount(close, volume),
            "created_at": created_at,
            "id": bar_row_id(code, dt_text),
        })
    return rows


def insert_rows(rows: list[dict]) -> tuple[int, str]:
    """INSERT INTO kline_minute_5（行内 datetime 已是完整时刻）。返回 (行数, 状态)。"""
    if not rows:
        return 0, "no_data"

    values: list[str] = []
    for r in rows:
        values.append(
            f"('{r['code']}', '{r['datetime']}', "
            f"{r['open']}, {r['high']}, {r['low']}, {r['close']}, "
            f"{r['volume']}, {r['amount']}, '{r['created_at']}', {r['id']})"
        )

    sql = (
        "INSERT INTO stock.kline_minute_5 "
        "(code, datetime, open, high, low, close, volume, amount, created_at, id) VALUES "
        + ",".join(values)
    )

    req = urllib.request.Request(CH_URL, data=sql.encode("utf-8"))
    req.add_header("Content-Type", "text/plain")
    try:
        urllib.request.urlopen(req, timeout=30)
        return len(rows), "ok"
    except Exception as exc:
        return 0, str(exc)


def _write_tencent_bars(code: str, rows: list[dict], now: datetime) -> dict:
    """主源路径：当日已收盘过滤 → 估算额 → 写入（裸码命名空间）。"""
    today = now.strftime("%Y-%m-%d")
    bars = closed_today_bars(rows, today, now)
    if not bars:
        return {"status": "no_data", "fetched": len(rows),
                "detail": "当日无已收盘 bar（非交易日/早盘第一根未收）"}
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    insert = build_insert_rows(bars, code, created_at)
    n, msg = insert_rows(insert)
    if msg != "ok":
        return {"status": "insert_failed", "error": msg}
    return {"status": "ok", "rows": n, "source": "tencent_mkline_m5"}


def _upsert_via_qt_snapshot(code: str, now: datetime) -> dict:
    """降级路径：qt 快照采样一根（O=H=L=C=现价失真，仅供盘中兜底参考）。"""
    snap = fetch_qt_snapshot(code)
    if not snap or snap.get("price") is None:
        return {"status": "no_data"}
    hhmm = now.strftime("%H:%M")
    dt_str = f"{hhmm}:00"
    price = float(snap["price"])
    vol = int(snap.get("volume") or 0)
    row = {
        "code": code,
        "datetime": f"{now.strftime('%Y-%m-%d')} {dt_str}",
        "open": round(price, 2),
        "high": round(price, 2),
        "low": round(price, 2),
        "close": round(price, 2),
        "volume": vol,
        "amount": round(float(snap.get("amount") or 0.0), 2),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "id": bar_row_id(code, dt_str),
    }
    n, msg = insert_rows([row])
    if msg != "ok":
        return {"status": "insert_failed", "error": msg}
    return {"status": "fallback_qt", "rows": n, "source": "qt_snapshot_sampled"}


# ── TDX 盘后数据对账（只读，不修数） ────────────────────────────────

def _ch_query(sql: str) -> str:
    import urllib.parse
    req = urllib.request.Request(CH_URL + "?query=" + urllib.parse.quote(sql))
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def load_tdx_bars(code: str, dates: set[str]) -> dict[str, dict]:
    """TDX 盘后（后缀码命名空间）m5 bar：{'YYYY-MM-DD HH:MM:SS': {o,h,l,c,v(手)}}。

    argMax(col, created_at) 取每 datetime 的最新写入版本（ReplacingMergeTree 语义）。
    查询失败返回 {}（由调用方如实记「对账缺基准」，不猜）。
    """
    if not dates:
        return {}
    suffix_code = _code_with_suffix(code)
    date_list = ",".join(f"'{d}'" for d in sorted(dates))
    sql = (
        "SELECT toString(datetime), argMax(open, created_at), argMax(high, created_at), "
        "argMax(low, created_at), argMax(close, created_at), argMax(volume, created_at) "
        f"FROM stock.kline_minute_5 WHERE code = '{suffix_code}' "
        f"AND toDate(datetime) IN ({date_list}) GROUP BY datetime ORDER BY datetime "
        "FORMAT TabSeparatedWithNames"
    )
    out: dict[str, dict] = {}
    try:
        text = _ch_query(sql)
        lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
        for ln in lines:
            parts = ln.split("\t")
            if len(parts) < 6:
                continue
            out[parts[0][:19]] = {"open": float(parts[1]), "high": float(parts[2]),
                                  "low": float(parts[3]), "close": float(parts[4]),
                                  "volume": float(parts[5])}
    except Exception as exc:  # 对账查询失败：如实报告，不猜
        logger.warning("[reconcile] %s TDX 读取失败: %s", code, exc)
    return out


def reconcile_against_tdx(fetched: dict[str, list[dict]], now: datetime,
                          out_dir: Optional[Path] = None) -> dict:
    """腾讯 m5 全窗口 vs TDX 盘后（后缀码）重叠日逐 bar 对账。

    fetched: {code: tencent rows（含历史日，全回看窗口）}。
    容差：价 0.1% / 量 3%（TDX 为基准；量容差覆盖腾讯最近一日整手舍入）。
    报告落 runtime/intraday_kline_upsert/reconcile_*.json；返回摘要 dict。
    """
    report: dict[str, Any] = {
        "schema": "intraday_kline_reconcile/1",
        "generated_at": now.isoformat(),
        "price_tol": RECON_PRICE_TOL,
        "volume_tol": RECON_VOLUME_TOL,
        "note": "腾讯 m5（proxy 通道）vs TDX 盘后（后缀码命名空间）逐 bar 对账",
        "codes": {},
    }
    overall_ok = True
    for code, rows in fetched.items():
        by_date: dict[str, list[dict]] = {}
        for row in rows:
            by_date.setdefault(str(row["datetime"])[:10], []).append(row)
        tdx = load_tdx_bars(code, set(by_date))
        code_stat: dict[str, Any] = {"dates": {}, "tdx_bars": len(tdx)}
        code_ok = True
        for day in sorted(by_date):
            compared = price_bad = vol_bad = missing_tdx = 0
            examples: list[dict] = []
            for row in by_date[day]:
                ts = f"{str(row['datetime'])[:16]}:00"
                ref = tdx.get(ts)
                if ref is None:
                    missing_tdx += 1
                    continue
                compared += 1
                for field in ("open", "high", "low", "close"):
                    base = ref[field]
                    if base and abs(float(row[field]) - base) / base > RECON_PRICE_TOL:
                        price_bad += 1
                        if len(examples) < 8:
                            examples.append({"ts": ts, "field": field,
                                             "tencent": float(row[field]), "tdx": base})
                v_ref, v_ten = ref["volume"], float(row["volume"])
                if max(v_ref, v_ten) > 0 and \
                        abs(v_ten - v_ref) / max(v_ref, v_ten) > RECON_VOLUME_TOL:
                    vol_bad += 1
                    if len(examples) < 8:
                        examples.append({"ts": ts, "field": "volume",
                                         "tencent": v_ten, "tdx": v_ref})
            day_ok = compared > 0 and price_bad == 0 and vol_bad == 0
            if compared > 0 and not day_ok:
                code_ok = False
                overall_ok = False
            code_stat["dates"][day] = {
                "compared": compared, "price_violations": price_bad,
                "volume_violations": vol_bad, "missing_in_tdx": missing_tdx,
                "ok": day_ok, "examples": examples,
            }
        code_stat["ok"] = code_ok
        report["codes"][code] = code_stat
    report["overall_ok"] = overall_ok

    if out_dir is None:
        try:
            from utils.paths import data_root
            out_dir = data_root() / "runtime" / "intraday_kline_upsert"
        except Exception:
            out_dir = Path(os.environ.get("AISTOCK_DATA_ROOT", r"F:\Aistock\data\data")) \
                / "runtime" / "intraday_kline_upsert"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"reconcile_{now.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf-8")
    report["report_path"] = str(path)
    return report


def run_upsert(codes: Optional[list[str]] = None, reconcile: bool = True,
               now: Optional[datetime] = None) -> dict:
    """主入口：腾讯 m5 主源 → 写入当日已收盘 bar → TDX 重叠日对账。

    主源失败 → qt 快照降级（O=H=L=C 失真，status=fallback_qt）。
    now 可注入（回放/测试用；默认北京当前时刻）。
    返回 {code: {status, rows, ...}}（可能附 _reconcile 键）。
    """
    codes = codes or MONITOR_CODES
    now = now or datetime.now(BEIJING_TZ)
    results: dict[str, dict] = {}
    fetched_all: dict[str, list[dict]] = {}

    for code in codes:
        try:
            result = fetch_tencent_m5(code)
            results[code] = _write_tencent_bars(code, result["rows"], now)
            fetched_all[code] = result["rows"]      # 全窗口留给对账（不再二次拉取）
        except Exception as exc:
            logger.warning("[upsert] %s 腾讯 m5 主源失败，降级 qt 快照: %s", code, exc)
            try:
                results[code] = _upsert_via_qt_snapshot(code, now)
            except Exception as exc2:
                logger.warning("[upsert] %s qt fallback 也失败: %s", code, exc2)
                results[code] = {"status": "fetch_failed", "error": f"{exc}; {exc2}"}

    if reconcile and fetched_all:
        try:
            report = reconcile_against_tdx(fetched_all, now)
            for code in results:
                if isinstance(results[code], dict):
                    results[code]["reconcile_ok"] = \
                        report.get("codes", {}).get(code, {}).get("ok")
            results["_reconcile"] = {"path": report.get("report_path"),
                                     "overall_ok": report.get("overall_ok")}
        except Exception as exc:
            logger.warning("[upsert] 对账失败: %s", exc)
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
            if isinstance(r, dict) and r.get("status") in ("ok", "fallback_qt")
        )
        n_fail = sum(
            1 for r in results.values()
            if isinstance(r, dict) and str(r.get("status", "")).endswith("_failed")
        )
        n_fb = sum(
            1 for r in results.values()
            if isinstance(r, dict) and r.get("status") == "fallback_qt"
        )
        logger.info(
            "[upsert] 完成: 写入 %d 根 / 失败 %d / qt降级 %d / 无数据 %d",
            n_total, n_fail, n_fb,
            sum(1 for r in results.values()
                if isinstance(r, dict) and "no_data" in str(r.get("status", ""))),
        )
        return 0 if n_fail == 0 else 1
    except Exception:
        logger.exception("[upsert] 致命错误")
        return 3


if __name__ == "__main__":
    sys.exit(main())
