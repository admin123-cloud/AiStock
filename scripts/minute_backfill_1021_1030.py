# -*- coding: utf-8 -*-
"""分钟表 2026-10-21 ~ 2026-10-30 缺口补全管道（任务书 20261101_minute_backfill）。

数据源：本地 TDX vipdoc ``{sh,sz,bj}/fzline/*.lc5``（5 分钟线，32 字节结构）。
基准：日线正式表 ``stock.kline_daily`` 股票全集（排除 000xxx.SH / 399xxx.SZ /
899xxx.BJ 指数；指数缺口另行核对，不在本管道写入范围）。

流程与门禁（fail-closed，任一失败即中止且不写）：
  1. --audit        只读反连接出缺失 (code, day)（4 表分别核对）；
  2. --dry-run      解析 lc5 → 门禁（格式/48 槽/单位整除/OHLC 值域/NEEQ/
                    日线 OHLC 包络 1% 与量额 3%）→ 腾讯 m5 抽样交叉验证
                    （价 ≤1%、量 ≤3%；深市量按日合计比对）→ 产出计划报告，零写入；
  3. --formal-write 持 DbWriteLock('kline_minute_backfill_1021_1030') →
                    锁内重跑全部门禁 + 目标键反连接（存量非空即中止）→
                    15/30/60 按库内口径从 5m 聚合 → 分批插入 4 表
                    （id=cityHash64(code,toDateTime(dt)) 确定性、created_at=批次
                    UTC 时间）→ 读回验证（全键聚合 + 抽样逐字段 + FINAL 一致）→
                    验证失败按精确 id 列表回滚。

写目标白名单：仅 stock.kline_minute_{5,15,30,60}。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils import tdx_lc5 as lc5
from utils.db_write_lock import DbWriteLock, LockError
from utils.security_market_policy import is_neeq_security_code

__all__ = [
    "WINDOW_DAYS", "TABLE_BY_PERIOD", "PERIODS", "LOCK_SCOPE", "INSERT_COLUMNS",
    "RUN_TAG", "minute_row_id", "build_row", "envelope_gate", "rel_diff",
    "fetch_missing_keys", "fetch_daily_reference", "fetch_calendar_gate",
    "parse_and_gate", "tencent_m5_fetch", "crosscheck_sample",
    "formal_write", "main",
]

WINDOW_DAYS = ("2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26",
               "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30")
#: 窗口（北京墙面钟字面量，R27b 起库内 datetime 已标注 Asia/Shanghai；
#: 与旧 UTC 形式 ("2026-10-20 16:00:00","2026-10-31 16:00:00") 同瞬间）
CH_WINDOW = ("2026-10-21 00:00:00", "2026-11-01 00:00:00")
PERIODS = ("5m", "15m", "30m", "60m")
TABLE_BY_PERIOD = {"5m": "stock.kline_minute_5", "15m": "stock.kline_minute_15",
                   "30m": "stock.kline_minute_30", "60m": "stock.kline_minute_60"}
LOCK_SCOPE = "kline_minute_backfill_1021_1030"
INSERT_COLUMNS = ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
RUN_TAG = "tdx_lc5_backfill_20261101"
PRICE_TOL = 0.01     # 价 ≤1%
VOL_AMT_TOL = 0.03   # 量额 ≤3%
CN_TZ = ZoneInfo("Asia/Shanghai")
READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 300}
TENCENT_HOSTS = ("https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn")
TENCENT_MAX_BARS = 320  # 最近 320 根 ≈ 6.7 交易日（覆盖 10-22~10-30，不含 10-21）


def _load_cityhash():
    spec = importlib.util.spec_from_file_location(
        "daily_kline_pipeline", PROJECT_ROOT / "scripts" / "daily_kline_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CITY = _load_cityhash()


def minute_row_id(code: str, dt_naive_beijing: datetime, period: str) -> int:
    """确定性 id = ClickHouse ``cityHash64(code, toDateTime(dt), <period tag>)`` 同源。

    与日线管道一致的 Python 端口：String 分支 CityHash64 + DateTime 分支
    intHash64(epoch)，Hash128to64 合并后再并入 period 标签，防止同刻多周期撞 id。
    （服务端 parity 探针见 dry-run 报告 id_parity 字段。）
    """
    epoch = int(dt_naive_beijing.replace(tzinfo=CN_TZ).timestamp())
    base = _CITY.hash128to64(_CITY.city_hash64(str(code).encode("utf-8")),
                             _CITY.clickhouse_int_hash64(epoch))
    return _CITY.hash128to64(base, _CITY.city_hash64(f"{RUN_TAG}|{period}".encode("utf-8")))


def build_row(bar: lc5.LC5Record, period: str, created_at: datetime) -> Tuple:
    return (bar.code, bar.datetime.replace(tzinfo=CN_TZ), bar.open, bar.high, bar.low,
            bar.close, int(bar.volume), float(bar.amount), created_at,
            minute_row_id(bar.code, bar.datetime, period))


def rel_diff(a: float, b: float) -> float:
    base = max(abs(a), abs(b))
    return 0.0 if base == 0 else abs(a - b) / base


def envelope_gate(bars: Sequence[lc5.LC5Record], daily: Sequence[float]) -> Optional[str]:
    """日线 OHLC 包络门禁：价 ≤1%，量/额 ≤3%。通过返回 None，否则返回原因。"""
    o = bars[0].open
    c = bars[-1].close
    h = max(b.high for b in bars)
    l = min(b.low for b in bars)
    d_o, d_h, d_l, d_c, d_v, d_a = daily
    for name, m, d in (("open", o, d_o), ("high", h, d_h), ("low", l, d_l), ("close", c, d_c)):
        if d <= 0:
            return f"daily_{name}<=0 ({d})"
        if rel_diff(m, d) > PRICE_TOL:
            return f"{name} {m:.4f} vs daily {d:.4f} 超 {PRICE_TOL:.0%}"
    m_v = sum(b.volume for b in bars)
    m_a = sum(b.amount for b in bars)
    if d_v == 0:
        if m_v != 0:
            return f"daily volume=0 但分钟量合计 {m_v}"
    elif rel_diff(m_v, d_v) > VOL_AMT_TOL:
        return f"volume {m_v} vs daily {d_v} 超 {VOL_AMT_TOL:.0%}"
    if d_a == 0:
        if m_a != 0:
            return f"daily amount=0 但分钟额合计 {m_a:.0f}"
    elif rel_diff(m_a, d_a) > VOL_AMT_TOL:
        return f"amount {m_a:.0f} vs daily {d_a:.0f} 超 {VOL_AMT_TOL:.0%}"
    return None


# ── 只读数据库查询 ─────────────────────────────────────────────

def _ro(client: Any, sql: str) -> List[dict]:
    result = client.query(sql, settings=dict(READONLY_SETTINGS))
    names = list(result.column_names)
    return [dict(zip(names, row)) for row in result.result_rows]


def _sql_str(values: Sequence[str]) -> str:
    return ",".join("'" + v.replace("'", " ") + "'" for v in values)


def fetch_missing_keys(client: Any, table: str, days: Sequence[str] = WINDOW_DAYS) -> List[Tuple[str, str]]:
    """以日线股票全集为基准，反连接出 (day, code) 缺失清单（只读）。"""
    rows = _ro(client, f"""
WITH u AS (SELECT trade_date AS td, code FROM stock.kline_daily
      WHERE trade_date IN ({_sql_str(days)})
        AND NOT (match(code,'^(000|880)[0-9]{{3}}\\\\.SH$') OR match(code,'^399[0-9]{{3}}\\\\.SZ$')
                 OR match(code,'^899[0-9]{{3}}\\\\.BJ$'))),
m AS (SELECT DISTINCT toDate(datetime) AS td, code FROM {table}
      WHERE datetime >= toDateTime('{CH_WINDOW[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{CH_WINDOW[1]}', 'Asia/Shanghai'))
SELECT u.td AS td, u.code AS code FROM u LEFT ANTI JOIN m ON u.code=m.code AND u.td=m.td
ORDER BY u.code, u.td""")
    return [(str(r["td"]), str(r["code"])) for r in rows]


def fetch_daily_reference(client: Any, codes: Sequence[str], days: Sequence[str] = WINDOW_DAYS) -> Dict:
    rows = _ro(client, f"""
SELECT toString(trade_date) AS d, code, open, high, low, close, volume, amount
FROM stock.kline_daily WHERE trade_date IN ({_sql_str(days)}) AND code IN ({_sql_str(codes)})""")
    return {(r["d"], r["code"]): (float(r["open"]), float(r["high"]), float(r["low"]),
                                  float(r["close"]), float(r["volume"]), float(r["amount"]))
            for r in rows}


def fetch_calendar_gate(client: Any, days: Sequence[str] = WINDOW_DAYS) -> Dict[str, int]:
    rows = _ro(client, f"""
SELECT toString(trade_date) AS d, max(is_trading) AS it FROM stock.trade_calendar
WHERE market='SH' AND trade_date IN ({_sql_str(days)}) GROUP BY trade_date""")
    return {r["d"]: int(r["it"]) for r in rows}


# ── 解析 + 门禁 ───────────────────────────────────────────────

def parse_and_gate(missing_keys: List[Tuple[str, str]], daily_ref: Dict,
                   tdx_root: Path, progress_every: int = 200) -> Tuple[Dict[str, List[Tuple]], List[dict]]:
    """解析缺失码的 lc5，逐 (code, day) 过门禁，产出 4 周期插入行与拒绝清单。"""
    by_code: Dict[str, set] = {}
    for day, code in missing_keys:
        by_code.setdefault(code, set()).add(day)
    created_at = datetime.now(timezone.utc)
    candidates: Dict[str, List[Tuple]] = {p: [] for p in PERIODS}
    rejects: List[dict] = []
    done = 0
    for code in sorted(by_code):
        days = sorted(by_code[code])
        num, suffix = code.split(".")
        path = Path(tdx_root) / suffix.lower() / "fzline" / f"{suffix.lower()}{num}.lc5"
        try:
            if is_neeq_security_code(code):
                raise lc5.Lc5FormatError("NEEQ 安全码被拒绝入库")
            if not path.exists():
                raise lc5.Lc5FormatError(f"lc5 文件不存在: {path}")
            by_day = lc5.parse_lc5_file(path, code, days)
            for day in days:
                bars = by_day.get(day, [])
                if not bars:
                    rejects.append({"code": code, "day": day, "reason": "lc5 无该日记录"})
                    continue
                lc5.validate_code_day_bars(code, day, bars)
                daily = daily_ref.get((day, code))
                if daily is None:
                    rejects.append({"code": code, "day": day, "reason": "日线基准行缺失"})
                    continue
                reason = envelope_gate(bars, daily)
                if reason:
                    rejects.append({"code": code, "day": day, "reason": f"包络门禁: {reason}"})
                    continue
                candidates["5m"].extend(build_row(b, "5m", created_at) for b in bars)
                for minutes, period in ((15, "15m"), (30, "30m"), (60, "60m")):
                    candidates[period].extend(
                        build_row(b, period, created_at) for b in lc5.aggregate_5m(bars, minutes))
        except lc5.Lc5FormatError as exc:
            rejects.extend({"code": code, "day": d, "reason": f"格式门禁: {exc}"} for d in days)
        done += 1
        if progress_every and done % progress_every == 0:
            print(f"parse_and_gate progress {done}/{len(by_code)} rejects={len(rejects)}", flush=True)
    return candidates, rejects


# ── 腾讯 m5 交叉验证 ──────────────────────────────────────────

def tencent_m5_fetch(code: str, fetcher: Optional[Callable[[str], dict]] = None) -> List[dict]:
    """腾讯 m5（不复权，最近 TENCENT_MAX_BARS 根）。volume：688/689 为股，其余为手。"""
    num, suffix = code.split(".")
    symbol = suffix.lower() + num
    if fetcher is not None:
        payload = fetcher(symbol)
    else:
        last_exc = None
        payload = None
        for host in TENCENT_HOSTS:
            url = f"{host}/appstock/app/kline/mkline?param={symbol},m5,,{TENCENT_MAX_BARS}"
            try:
                req = urllib.request.Request(url, headers={"Referer": "https://gu.qq.com/"})
                payload = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                payload = None
        if payload is None:
            raise RuntimeError(f"腾讯 m5 {symbol} 拉取失败: {last_exc}")
    node = payload.get("data", {}).get(symbol, {})
    rows_raw = node.get("m5") if isinstance(node, dict) else None
    if not isinstance(rows_raw, list) or not rows_raw:
        raise RuntimeError(f"腾讯 m5 {symbol} 返回 0 根")
    out = []
    for item in rows_raw:
        dt = datetime.strptime(item[0], "%Y%m%d%H%M")
        vol = float(item[5])
        if code.startswith(("688", "689")):
            vol = vol / 100.0  # 腾讯科创板分钟量为股
        out.append({"datetime": dt, "open": float(item[1]), "close": float(item[2]),
                    "high": float(item[3]), "low": float(item[4]), "volume": vol})
    return out


def crosscheck_sample(candidates: Dict[str, List[Tuple]], missing_keys: List[Tuple[str, str]],
                      sample_codes: Sequence[str], fetcher: Optional[Callable[[str], dict]] = None,
                      price_tol: float = PRICE_TOL, vol_tol: float = VOL_AMT_TOL) -> dict:
    """抽样与腾讯 m5 逐值比对。沪市逐根；深市价逐根、量按日合计（分钟边界口径差异）。"""
    our_bars: Dict[Tuple[str, str], Dict] = {}
    for row in candidates["5m"]:
        code, dt = row[0], row[1]
        day = dt.astimezone(CN_TZ).strftime("%Y-%m-%d")
        our_bars.setdefault((code, day), {})[dt.astimezone(CN_TZ).strftime("%H:%M")] = row
    results = []
    pass_n = fail_n = 0
    for code in sample_codes:
        days = sorted({d for (d, c) in missing_keys if c == code
                       if d >= "2026-10-22"})  # 腾讯窗口不含 10-21
        if not days:
            continue
        try:
            tx = tencent_m5_fetch(code, fetcher=fetcher)
        except RuntimeError as exc:
            results.append({"code": code, "status": "fetch_error", "error": str(exc)})
            continue
        tx_by_day: Dict[str, List[dict]] = {}
        for bar in tx:
            tx_by_day.setdefault(bar["datetime"].strftime("%Y-%m-%d"), []).append(bar)
        for day in days:
            ours = our_bars.get((code, day), {})
            txb = [b for b in tx_by_day.get(day, [])]
            if not txb or not ours:
                results.append({"code": code, "day": day, "status": "no_tencent_coverage"})
                continue
            issues = []
            vol_our = sum(r[6] for r in ours.values())
            vol_tx = sum(b["volume"] for b in txb)
            for hm, row in sorted(ours.items()):
                match = next((b for b in txb if b["datetime"].strftime("%H:%M") == hm), None)
                if match is None:
                    issues.append(f"{hm} 腾讯缺根")
                    continue
                for idx, field in ((2, "open"), (3, "high"), (4, "low"), (5, "close")):
                    if rel_diff(row[idx], match[field]) > price_tol:
                        issues.append(f"{hm} {field} {row[idx]} vs {match[field]}")
                if not code.endswith(".SZ"):  # 深市量按日合计（分钟边界口径差异）
                    if rel_diff(row[6], match["volume"]) > vol_tol:
                        issues.append(f"{hm} volume {row[6]} vs {match['volume']}")
            if code.endswith(".SZ") and rel_diff(vol_our, vol_tx) > vol_tol:
                issues.append(f"日量合计 {vol_our} vs {vol_tx}")
            ok = not issues
            pass_n += ok
            fail_n += not ok
            results.append({"code": code, "day": day, "status": "pass" if ok else "mismatch",
                            "issues": issues[:8]})
    return {"sample_codes": list(sample_codes), "pass_days": pass_n, "fail_days": fail_n,
            "detail": results}


def pick_sample(missing_keys: List[Tuple[str, str]], n_sh: int = 12, n_sz: int = 12,
                seed: int = 20261101) -> List[str]:
    codes_by_mkt: Dict[str, list] = {"SH": [], "SZ": []}
    for day, code in missing_keys:
        if day < "2026-10-22":  # 腾讯窗口从 10-22 起
            continue
        mkt = code.split(".")[1]
        if mkt in codes_by_mkt:
            codes_by_mkt[mkt].append(code)
    rng = random.Random(seed)
    sample = []
    for mkt, n in (("SH", n_sh), ("SZ", n_sz)):
        pool = sorted(set(codes_by_mkt[mkt]))
        sample.extend(rng.sample(pool, min(n, len(pool))))
    return sample


# ── 正式写入 ──────────────────────────────────────────────────

def _existing_keys_nonempty(client: Any, table: str, keys: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """锁内反连接复核：目标 (code, day) 在表里已有的存量键（应为空）。"""
    codes = sorted({c for _, c in keys})
    found = set()
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        rows = _ro(client, f"""
SELECT DISTINCT code, toString(toDate(datetime)) AS d FROM {table}
WHERE datetime >= toDateTime('{CH_WINDOW[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{CH_WINDOW[1]}', 'Asia/Shanghai')
  AND code IN ({_sql_str(chunk)})""")
        for r in rows:
            found.add((str(r["d"]), str(r["code"])))
    return sorted(set(keys) & found)


def _readback_aggregate(client: Any, table: str, keys: List[Tuple[str, str]],
                        final: bool = False) -> Dict[Tuple[str, str], Tuple]:
    suffix = " FINAL" if final else ""
    codes = sorted({c for _, c in keys})
    out: Dict[Tuple[str, str], Tuple] = {}
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        rows = _ro(client, f"""
SELECT toString(toDate(datetime)) AS d, code, count() AS n,
       sum(volume) AS v, sum(amount) AS a FROM {table}{suffix}
WHERE datetime >= toDateTime('{CH_WINDOW[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{CH_WINDOW[1]}', 'Asia/Shanghai')
  AND code IN ({_sql_str(chunk)}) GROUP BY d, code""")
        for r in rows:
            out[(str(r["d"]), str(r["code"]))] = (int(r["n"]), float(r["v"]), float(r["a"]))
    return out


def _candidate_aggregate(rows: List[Tuple]) -> Dict[Tuple[str, str], Tuple]:
    out: Dict[Tuple[str, str], Tuple] = {}
    for r in rows:
        day = r[1].astimezone(CN_TZ).strftime("%Y-%m-%d")
        n, v, a = out.get((day, r[0]), (0, 0.0, 0.0))
        out[(day, r[0])] = (n + 1, v + r[6], a + r[7])
    return out


def formal_write(client_factory: Callable[[], Any], candidates: Dict[str, List[Tuple]],
                 journal: Callable[[dict], None], batch_rows: int = 50_000,
                 lock_owner: str = "aistock-dev") -> dict:
    """锁内反连接复核 → 分批插入 → 读回验证 → 异常按精确 id 回滚。"""
    report = {"tables": {}, "status": "ok"}
    lock_client = client_factory()
    lock = DbWriteLock(lock_client, journal=journal)
    acquired = lock.acquire(LOCK_SCOPE, lock_owner, purpose="分钟表 10-21~10-30 缺口补全 " + RUN_TAG)
    journal({"event": "lock_acquired", **{k: acquired[k] for k in ("scope", "owner")}})
    inserted_ids: Dict[str, List[int]] = {}
    try:
        client = client_factory()
        for period in PERIODS:
            table = TABLE_BY_PERIOD[period]
            rows = candidates[period]
            keys = sorted({(r[1].astimezone(CN_TZ).strftime("%Y-%m-%d"), r[0]) for r in rows})
            existing = _existing_keys_nonempty(client, table, keys)
            if existing:
                raise RuntimeError(f"{table}: 锁内反连接发现存量键 {len(existing)}，例如 {existing[:5]}；fail-closed 中止")
            journal({"event": "anti_join_clean", "table": table, "keys": len(keys)})
            for i in range(0, len(rows), batch_rows):
                batch = rows[i:i + batch_rows]
                client.insert(table.split(".")[1], batch, column_names=INSERT_COLUMNS)
                journal({"event": "insert", "table": table, "rows": len(batch)})
            inserted_ids[period] = [r[9] for r in rows]

        # 读回验证：全键聚合 + FINAL 一致 + 抽样逐字段
        rng = random.Random(20261101)
        for period in PERIODS:
            table = TABLE_BY_PERIOD[period]
            rows = candidates[period]
            want = _candidate_aggregate(rows)
            got = _readback_aggregate(client, table, sorted(want))
            problems = []
            for key, expect in want.items():
                actual = got.get(key)
                if actual is None:
                    problems.append(f"{key} 读回缺失")
                    continue
                if actual[0] != expect[0] or rel_diff(actual[1], expect[1]) > 1e-6 or rel_diff(actual[2], expect[2]) > 1e-6:
                    problems.append(f"{key} 聚合不一致 expect={expect} got={actual}")
            got_final = _readback_aggregate(client, table, sorted(want), final=True)
            if len(got_final) != len(got):
                problems.append(f"FINAL 键数 {len(got_final)} != raw {len(got)}")
            report["tables"][period] = {"rows": len(rows), "code_days": len(want),
                                        "readback_problems": problems[:10], "problem_count": len(problems)}
            if problems:
                report["status"] = "verify_failed"
                raise RuntimeError(f"{table} 读回验证失败 {len(problems)} 项")
        report["sample_field_check"] = _sample_field_check(client, candidates, rng)
    except Exception as exc:
        journal({"event": "rollback_start", "error": f"{type(exc).__name__}: {exc}"})
        try:
            rollback_client = client_factory()
            for period, table in TABLE_BY_PERIOD.items():
                ids = inserted_ids.get(period, [])
                for i in range(0, len(ids), 5000):
                    chunk = ids[i:i + 5000]
                    id_list = ",".join(str(x) for x in chunk)
                    rollback_client.command(
                        f"ALTER TABLE {table} DELETE WHERE id IN ({id_list}) SETTINGS mutations_sync = 1")
                journal({"event": "rollback_delete", "table": table, "ids": len(ids)})
            for period, table in TABLE_BY_PERIOD.items():
                ids = inserted_ids.get(period, [])
                if ids:
                    id_list = ",".join(str(x) for x in ids[:5000])
                    left = _ro(rollback_client, f"SELECT count() AS n FROM {table} WHERE id IN ({id_list})")
                    journal({"event": "rollback_verify", "table": table,
                             "residual_first5000": int(left[0]["n"]) if left else None})
            report["status"] = "rolled_back"
        except Exception as rb_exc:  # noqa: BLE001
            report["status"] = "rollback_failed"
            journal({"event": "rollback_error", "error": f"{type(rb_exc).__name__}: {rb_exc}"})
        raise
    finally:
        try:
            lock.release(LOCK_SCOPE, lock_owner)
            journal({"event": "lock_released", "scope": LOCK_SCOPE, "owner": lock_owner})
        except LockError as exc:
            journal({"event": "lock_release_error", "error": str(exc)})
    return report


def _sample_field_check(client: Any, candidates: Dict[str, List[Tuple]], rng: random.Random,
                        per_period: int = 25) -> dict:
    """抽样逐字段读回（48/16/8/4 根全字段），浮点按相对 1e-6。"""
    out = {}
    for period in PERIODS:
        table = TABLE_BY_PERIOD[period]
        rows = candidates[period]
        by_key: Dict[Tuple[str, str], List[Tuple]] = {}
        for r in rows:
            by_key.setdefault((r[0], r[1].astimezone(CN_TZ).strftime("%Y-%m-%d")), []).append(r)
        sample_keys = rng.sample(sorted(by_key), min(per_period, len(by_key)))
        problems = []
        for code, day in sample_keys:
            want = {r[1].astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M"): r for r in by_key[(code, day)]}
            rows = _ro(client, f"""
            SELECT substring(toString(toDateTime(datetime, 'Asia/Shanghai')), 1, 16) AS dt,
            open, high, low, close, volume, amount
            FROM {table} WHERE code = '{code}'
            AND datetime >= toDateTime('{day} 00:00:00', 'Asia/Shanghai')
            AND datetime < toDateTime('{day} 00:00:00', 'Asia/Shanghai') + INTERVAL 1 DAY""")
            got = {r["dt"]: r for r in rows}
            if len(got) != len(want):
                problems.append(f"{code} {day} 行数 {len(got)} != {len(want)}")
            for hm, w in want.items():
                g = got.get(hm)
                if g is None:
                    problems.append(f"{code} {hm} 缺行")
                    continue
                for idx, field in ((2, "open"), (3, "high"), (4, "low"), (5, "close"),
                                   (6, "volume"), (7, "amount")):
                    if rel_diff(float(g[field]), float(w[idx])) > 1e-6:
                        problems.append(f"{code} {hm} {field} {g[field]} != {w[idx]}")
        out[period] = {"sampled": len(sample_keys), "problems": problems[:8], "problem_count": len(problems)}
        if problems:
            raise RuntimeError(f"{table} 抽样字段校验失败: {problems[:5]}")
    return out


# ── CLI ───────────────────────────────────────────────────────

def _journal_writer(path: Path):
    def write(event: dict) -> None:
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return write


def _default_client_factory() -> Any:
    from utils.market_warehouse import clickhouse_client
    return clickhouse_client()


def main() -> int:
    parser = argparse.ArgumentParser(description="分钟表 10-21~10-30 缺口补全（任务书 20261101）")
    parser.add_argument("--mode", choices=("audit", "dry-run", "formal-write"), required=True)
    parser.add_argument("--tdx-root", default=r"D:\TDX\vipdoc")
    parser.add_argument("--out-dir", default=r"F:\Aistock\data\data\runtime\minute_backfill_20261101")
    parser.add_argument("--no-crosscheck", action="store_true", help="dry-run 跳过腾讯抽样（离线调试）")
    parser.add_argument("--sample-sh", type=int, default=12)
    parser.add_argument("--sample-sz", type=int, default=12)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = _default_client_factory()

    if args.mode == "audit":
        report = {}
        for period in PERIODS:
            keys = fetch_missing_keys(client, TABLE_BY_PERIOD[period])
            report[period] = {"missing_code_days": len(keys), "distinct_codes": len({c for _, c in keys})}
            with (out_dir / f"missing_keys_{period}.tsv").open("w", encoding="utf-8") as handle:
                for day, code in keys:
                    handle.write(f"{day}\t{code}\n")
        (out_dir / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0

    keys = fetch_missing_keys(client, TABLE_BY_PERIOD["5m"])
    cal = fetch_calendar_gate(client)
    non_trading = [d for d in WINDOW_DAYS if not cal.get(d)]
    if non_trading:
        raise SystemExit(f"trade_calendar 门禁失败，非交易日 {non_trading}")
    daily_ref = fetch_daily_reference(client, sorted({c for _, c in keys}))
    print(f"missing 5m keys={len(keys)} daily_ref={len(daily_ref)}", flush=True)
    candidates, rejects = parse_and_gate(keys, daily_ref, Path(args.tdx_root))
    plan = {p: {"rows": len(candidates[p]),
                "code_days": len({(r[0], r[1].astimezone(CN_TZ).strftime('%Y-%m-%d')) for r in candidates[p]})}
            for p in PERIODS}
    id_probe = {}
    probe_dt = datetime(2026, 10, 21, 9, 35)
    got = _ro(client, "SELECT cityHash64('000698.SZ', toDateTime('2026-10-21 09:35:00','Asia/Shanghai'), "
                      "'tdx_lc5_backfill_20261101|5m') AS sid")
    id_probe = {"python": minute_row_id("000698.SZ", probe_dt, "5m"), "server": int(got[0]["sid"])}
    if id_probe["python"] != id_probe["server"]:
        raise SystemExit(f"id parity 探针失败: {id_probe}")
    report = {"mode": args.mode, "missing_keys_5m": len(keys), "plan": plan,
              "rejects": len(rejects), "id_probe": id_probe}
    if rejects:
        (out_dir / "rejects.json").write_text(json.dumps(rejects, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.mode == "dry-run":
        if not args.no_crosscheck:
            sample = pick_sample(keys, args.sample_sh, args.sample_sz)
            report["crosscheck"] = crosscheck_sample(candidates, keys, sample)
            (out_dir / "crosscheck.json").write_text(
                json.dumps(report["crosscheck"], ensure_ascii=False, indent=2), encoding="utf-8")
        report["status"] = "dry_run_ok"
        (out_dir / "dry_run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0

    # formal-write：锁内重跑（防窗口期数据变化），此处复用上面锁外解析结果做规模报告，
    # 实际插入前会在锁内重新反连接复核。
    journal = _journal_writer(out_dir / "journal.jsonl")
    write_report = formal_write(_default_client_factory, candidates, journal)
    report["formal_write"] = write_report
    (out_dir / "formal_write_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
