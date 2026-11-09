# -*- coding: utf-8 -*-
"""15/30/60 分钟表从 5m 表聚合补齐管道（R19，窗口 11-02~11-09；治理镜像 R16）。

数据源：正式表 ``stock.kline_minute_5``（窗口内已由 R16 补齐，每日码数×48
精确闭合）。不读外部文件、不造 bar：5m 缺失的 code-day 如实跳过并报告
（本窗口已知 4 个 fail-closed code-day）。

存量口径反推（2026-11-09 只读探针，零写入；证据见 runtime 报告）：
  - datetime：**UTC 存储**（= 北京墙面时刻 − 8h；5m 与 15/30/60 同口径），
    15m 末日收盘桶北京 15:00 = 库内 07:00；
  - 栅格：15m 桶界 09:45..11:30 / 13:15..15:00（30m/60m 同法）——与
    ``utils.tdx_lc5.aggregate_5m`` 会话栅格分桶逐一相同（10-30 存量实测）；
  - id：存量 10-21~10-30 段规范终批（created_at 2026-11-01 13 时，a0cfe1a
    收尾批 ``minute-backfill-20261101-tdxlc5-v1``）为
    ``blake2b(f'{batch}|{period}|{code}|{ts}', digest_size=8)`` 大端 UInt64，
    ts=epoch（北京墙面时刻）；探针逐 id 复现 30/30×3 表（测试含黄金向量）。
    该段另有三个早期批次为**顺序计数器域**（双派发遗留，非逐键可复现），
    仅留档表征、不复用；id 探针 fail-closed：复现不出即中止。

流程与门禁（fail-closed，任一失败即中止且不写）：
  1. --mode audit        只读反连接出各表缺失 (code, day)；
  2. --mode dry-run      读 5m 源（48 槽门禁）→ 聚合（O=首 open、C=末 close、
                         H/L=窗口极值、量额=求和）→ 计划行数必须恰为
                         码数×16/×8/×4 → id 存量反推探针 + 对存量 10-30 的
                         栅格/时区/口径逐字段校准 → 报告，零写入；
  3. --mode formal-write 持 DbWriteLock('kline_minute_agg_backfill_window') →
                         锁内逐表反连接（存量非空即中止，only-missing-keys）→
                         分批插入 15/30/60 三表（id=blake2b 同域确定性、
                         created_at=批次 UTC）→ 读回验证（全键聚合×16/8/4 +
                         FINAL 一致 + 新行 id 逐 id 复现 + 抽样 25 股票日
                         逐字段对 5m 重聚合复算）→ 验证失败按精确 id 回滚。

写目标白名单：仅 stock.kline_minute_15/30/60；绝不写 5m/日线。

R22b（2026-11-09，PM 批准协调文件方案 A）：amount NULL 口径——5m 成分任一
NULL → 该聚合行 amount=NULL（绝不部分求和造数；NULL 以 NaN 哨兵经
``aggregate_5m`` 的 sum 按 IEEE 语义自然传播，出库前转回 None）；OHLC/量
不受影响照算；读回验证与抽样复算全部 NULL 感知比对（双 NULL 一致）。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from hashlib import blake2b
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils.db_write_lock import DbWriteLock, LockError  # noqa: E402
from utils.tdx_lc5 import LC5Record, STANDARD_5M_BAR_ENDS, aggregate_5m  # noqa: E402

# 复用既有实现（任务书要求：先检索、复用口径，不复制漂移）
from minute_backfill_1021_1030 import (  # noqa: E402
    CN_TZ,
    INSERT_COLUMNS,
    _default_client_factory,
    _journal_writer,
    _ro,
    _sql_str,
    rel_diff,
)
from minute_backfill_window import (  # noqa: E402  (R16)
    ch_window,
    fetch_missing_keys,
    resolve_window,
    trading_days_gate,
)

__all__ = [
    "TARGET_PERIODS", "TABLE_BY_PERIOD", "AGG_MINUTES", "BARS_PER_CD",
    "SOURCE_TABLE", "LOCK_SCOPE", "ID_BATCH", "ID_PROBE_SEGMENT",
    "ID_PROBE_BATCH_CREATED", "DEFAULT_MAX_WINDOW_DAYS",
    "blake2b_row_id", "naive_bj_to_epoch", "epoch_to_naive_bj", "epoch_to_utc",
    "day_window", "fetch_5m_bars", "build_agg_rows", "plan_report",
    "id_domain_probe", "grid_calibration_probe", "formal_write", "main",
]

TARGET_PERIODS = ("15m", "30m", "60m")
TABLE_BY_PERIOD = {"15m": "stock.kline_minute_15",
                   "30m": "stock.kline_minute_30",
                   "60m": "stock.kline_minute_60"}
AGG_MINUTES = {"15m": 15, "30m": 30, "60m": 60}
BARS_PER_CD = {"15m": 16, "30m": 8, "60m": 4}
SOURCE_TABLE = "stock.kline_minute_5"
LOCK_SCOPE = "kline_minute_agg_backfill_window"   # R19 独立锁 scope（镜像 R16 铁律）
#: 存量 id 反推结论：a0cfe1a 收尾批批次串（逐 id 复现已验，见模块 docstring）
ID_BATCH = "minute-backfill-20261101-tdxlc5-v1"
#: id 探针取样段（存量 10-21~10-30）与其规范终批 created_at 小时窗
ID_PROBE_SEGMENT = ("2026-10-20 16:00:00", "2026-10-31 16:00:00")
ID_PROBE_BATCH_CREATED = ("2026-11-01 13:00:00", "2026-11-01 14:00:00")
DEFAULT_MAX_WINDOW_DAYS = 16  # 与 R16 一致：默认拒绝 >16 天窗口
DEFAULT_RUNTIME_BASE = r"F:\Aistock\data\data\runtime"
RNG_SEED = 20261109


# ── id / 时区（存量反推；测试含真实 DB 黄金向量）──────────────

def blake2b_row_id(batch: str, period: str, code: str, ts: int) -> int:
    """确定性 UInt64 id（复现 a0cfe1a ``planned_row_id``，存量终批逐 id 复现）。"""
    digest = blake2b(f"{batch}|{period}|{code}|{ts}".encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


def naive_bj_to_epoch(dt_naive_beijing: datetime) -> int:
    """北京墙面时刻（naive）→ epoch（与存量 id 的 ts 同一定义）。"""
    return int(dt_naive_beijing.replace(tzinfo=CN_TZ).timestamp())


def epoch_to_naive_bj(ts: int) -> datetime:
    """epoch → 北京墙面时刻（naive），供分组/聚合。"""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(CN_TZ).replace(tzinfo=None)


def epoch_to_utc(ts: int) -> datetime:
    """epoch → UTC-aware datetime（插入用；库内 datetime=UTC 口径）。"""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def day_window(day: str) -> Tuple[str, str]:
    """单个北京日 → 精确半开窗口 [d 00:00, d+1 00:00) 北京钟（R27b：库内
    datetime 已标注 Asia/Shanghai，SQL 侧以 ``toDateTime(?, 'Asia/Shanghai')``
    解析，与旧 UTC 形式 ``[d-1 16:00, d 16:00)`` 同瞬间）。

    注意不能复用 R16 ``ch_window(d, d)``：其上界公式 ``end+2 日 00:00 北京``
    实际覆盖到北京 ``end+2 日 00:00``（多出一整个北京日）——多日窗口反连接
    无害，但单日取数会取到次日数据（本任务 2026-11-09 实测 96/48 根后修正）。
    """
    d = date.fromisoformat(day)
    lo = datetime.combine(d, time(0, 0))
    hi = datetime.combine(d + timedelta(days=1), time(0, 0))
    return lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S")


# ── amount NULL 口径（R22b：成分任一 NULL → 聚合行 NULL，绝不部分求和）──

def _agg_amount_in(value: Any) -> float:
    """DB amount 值 → 聚合传播哨兵：NULL → NaN（IEEE sum 传播 = 任一 NULL 则 NaN）。"""
    return float("nan") if value is None else float(value)


def _agg_amount_out(value: Any) -> Optional[float]:
    """聚合结果 → 出库值：NaN 哨兵转回 None（列 Nullable(Float64)，禁 0/推算）。"""
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) else value


def _amount_matches(got: Any, want: Any, tol: float) -> bool:
    """NULL 感知比对：双 NULL（含 NaN 哨兵）一致；单边 NULL 不一致；数值走 rel_diff。"""
    got_null = got is None or (isinstance(got, float) and math.isnan(got))
    want_null = want is None or (isinstance(want, float) and math.isnan(want))
    if got_null or want_null:
        return got_null and want_null
    return rel_diff(float(got), float(want)) <= tol


def _candidate_aggregate_nullsafe(rows: List[Tuple]) -> Dict[Tuple[str, str], Tuple]:
    """NULL 感知版 R16 ``_candidate_aggregate``：任一行 amount=None → 该键聚合额 None。"""
    out: Dict[Tuple[str, str], Tuple] = {}
    for r in rows:
        day = r[1].astimezone(CN_TZ).strftime("%Y-%m-%d")
        n, v, a = out.get((day, r[0]), (0, 0.0, 0.0))
        amount = r[7]
        new_a = None if (a is None or amount is None) else a + amount
        out[(day, r[0])] = (n + 1, v + r[6], new_a)
    return out


# ── 5m 源读取与 48 槽门禁 ─────────────────────────────────────

def fetch_5m_bars(client: Any, codes: Sequence[str],
                  ch_win: Sequence[str]) -> Tuple[Dict[Tuple[str, str], List[LC5Record]], List[dict]]:
    """窗口内读 5m 正式表 → 按 (code, 北京日) 分组；每股票日必须恰 48 槽。

    任一 (code, day) 不是完整 48 槽标准网格 → fail-closed 记入 rejects
    （不造 bar、不聚合残日）。amount=NULL 行合法（R21/R22 B 源口径），
    以 NaN 哨兵承载继续聚合（R22b）。
    """
    grouped: Dict[Tuple[str, str], List[LC5Record]] = {}
    for i in range(0, len(codes), 200):
        chunk = list(codes)[i:i + 200]
        rows = _ro(client, f"""
SELECT code, toUnixTimestamp(datetime) AS ts, open, high, low, close, volume, amount
FROM {SOURCE_TABLE}
WHERE datetime >= toDateTime('{ch_win[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch_win[1]}', 'Asia/Shanghai')
  AND code IN ({_sql_str(chunk)}) ORDER BY code, ts""")
        for r in rows:
            code = str(r["code"])
            ts = int(r["ts"])
            dt_bj = epoch_to_naive_bj(ts)
            grouped.setdefault((code, dt_bj.strftime("%Y-%m-%d")), []).append(
                LC5Record(code, dt_bj, float(r["open"]), float(r["high"]),
                          float(r["low"]), float(r["close"]), int(r["volume"]),
                          _agg_amount_in(r["amount"])))
    bars_by_key: Dict[Tuple[str, str], List[LC5Record]] = {}
    rejects: List[dict] = []
    for key in sorted(grouped):
        bars = sorted(grouped[key], key=lambda b: b.datetime)
        seen = {b.datetime.time().replace(second=0, microsecond=0) for b in bars}
        if len(bars) != 48 or seen != set(STANDARD_5M_BAR_ENDS):
            rejects.append({"code": key[0], "day": key[1],
                            "reason": f"5m 槽数 {len(bars)}/48 网格 {len(seen)}/48 不合约"})
            continue
        bars_by_key[key] = bars
    return bars_by_key, rejects


def build_agg_rows(bars_by_key: Dict[Tuple[str, str], List[LC5Record]], created_at: datetime,
                   allowed_keys: Optional[Set[Tuple[str, str]]] = None,
                   periods: Sequence[str] = TARGET_PERIODS) -> Dict[str, List[Tuple]]:
    """从 5m bar 聚合高周期插入行（复用 ``utils.tdx_lc5.aggregate_5m`` 栅格口径）。

    行结构对齐 INSERT_COLUMNS：code, datetime(UTC-aware), o/h/l/c,
    volume(int), amount(Optional[float])（成分任一 NULL → None，R22b 口径）,
    created_at, id。
    ``allowed_keys`` 为该表缺失 (day, code) 集合（only-missing-keys）。
    """
    out: Dict[str, List[Tuple]] = {p: [] for p in periods}
    for (code, day) in sorted(bars_by_key):
        if allowed_keys is not None and (day, code) not in allowed_keys:
            continue
        bars = bars_by_key[(code, day)]
        for period in periods:
            for b in aggregate_5m(bars, AGG_MINUTES[period]):
                ts = naive_bj_to_epoch(b.datetime)
                out[period].append((b.code, epoch_to_utc(ts), b.open, b.high, b.low,
                                    b.close, int(b.volume), _agg_amount_out(b.amount),
                                    created_at,
                                    blake2b_row_id(ID_BATCH, period, b.code, ts)))
    return out


def plan_report(candidates: Dict[str, List[Tuple]]) -> Dict[str, dict]:
    """计划报告；exact_bars_per_cd 必须为 True（码数×16/×8/×4 精确闭合）。"""
    out: Dict[str, dict] = {}
    for period in TARGET_PERIODS:
        rows = candidates.get(period, [])
        code_days = len({(r[1].astimezone(CN_TZ).strftime("%Y-%m-%d"), r[0]) for r in rows})
        out[period] = {"rows": len(rows), "code_days": code_days,
                       "exact_bars_per_cd": len(rows) == code_days * BARS_PER_CD[period]}
    return out


# ── 存量反推探针 ──────────────────────────────────────────────

def id_domain_probe(client: Any, per_table: int = 30) -> Dict[str, dict]:
    """存量 10-21~10-30 规范终批逐 id 复现 blake2b 公式；任一不符即 fail-closed。"""
    out: Dict[str, dict] = {}
    failures: List[str] = []
    for period, table in TABLE_BY_PERIOD.items():
        rows = _ro(client, f"""
SELECT code, toUnixTimestamp(datetime) AS ts, id FROM {table}
WHERE datetime >= toDateTime('{ID_PROBE_SEGMENT[0]}') AND datetime < toDateTime('{ID_PROBE_SEGMENT[1]}')
  AND created_at >= toDateTime('{ID_PROBE_BATCH_CREATED[0]}')
  AND created_at < toDateTime('{ID_PROBE_BATCH_CREATED[1]}')
ORDER BY code, ts LIMIT {int(per_table)}""")
        match = mismatch = 0
        for r in rows:
            want = blake2b_row_id(ID_BATCH, period, str(r["code"]), int(r["ts"]))
            if want == int(r["id"]):
                match += 1
            else:
                mismatch += 1
        out[period] = {"sampled": len(rows), "match": match, "mismatch": mismatch,
                       "batch": ID_BATCH}
        if not rows:
            failures.append(f"{period}: 探针段无存量行（created_at 批次 {ID_PROBE_BATCH_CREATED}）")
        elif mismatch:
            failures.append(f"{period}: {mismatch}/{len(rows)} 逐 id 复现失败")
    if failures:
        raise SystemExit(f"id 方案反推探针失败（fail-closed，不写库）: {failures}")
    return out


def grid_calibration_probe(client: Any, codes: Sequence[str], day: str = "2026-10-30",
                           tol: float = 1e-6) -> Dict[str, dict]:
    """对存量 {day} 行做栅格/时区/口径校准：DB 5m 重聚合 vs DB 15/30/60 逐字段。

    任一不一致即 RuntimeError（fail-closed）：证明聚合口径与存量完全同轨后
    才允许 formal-write。
    """
    ch = day_window(day)  # 精确单北京日（ch_window(d,d) 会多覆盖一日，见 day_window docstring）
    out: Dict[str, dict] = {}
    problems_all: List[str] = []
    for period, table in TABLE_BY_PERIOD.items():
        problems: List[str] = []
        total_rows = 0
        for code in codes:
            src = _ro(client, f"""
SELECT toUnixTimestamp(datetime) AS ts, open, high, low, close, volume, amount
FROM {SOURCE_TABLE} WHERE code = '{code}'
AND datetime >= toDateTime('{ch[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch[1]}', 'Asia/Shanghai') ORDER BY ts""")
            if not src:
                continue  # 该码在存量段无 5m 数据（如次新股）：跳过，不算失败
            if len(src) != 48:
                problems.append(f"{code} 存量 5m 槽数 {len(src)} != 48")
                continue
            bars = [LC5Record(code, epoch_to_naive_bj(int(r["ts"])), float(r["open"]),
                              float(r["high"]), float(r["low"]), float(r["close"]),
                              int(r["volume"]), _agg_amount_in(r["amount"])) for r in src]
            want = {naive_bj_to_epoch(b.datetime): b
                    for b in aggregate_5m(bars, AGG_MINUTES[period])}
            got_rows = _ro(client, f"""
SELECT toUnixTimestamp(datetime) AS ts, open, high, low, close, volume, amount
FROM {table} WHERE code = '{code}'
AND datetime >= toDateTime('{ch[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch[1]}', 'Asia/Shanghai') ORDER BY ts""")
            got = {int(r["ts"]): r for r in got_rows}
            total_rows += len(got)
            if set(got) != set(want):
                problems.append(f"{code} 栅格不一致 want={sorted(want)} got={sorted(got)}")
                continue
            if got and epoch_to_naive_bj(max(got)).strftime("%H:%M") != "15:00":
                problems.append(f"{code} 末日桶非北京 15:00（UTC 07:00）：时区口径漂移")
            for ts, wb in sorted(want.items()):
                g = got[ts]
                for field, wv in (("open", wb.open), ("high", wb.high), ("low", wb.low),
                                  ("close", wb.close), ("volume", wb.volume),
                                  ("amount", wb.amount)):
                    bad = (not _amount_matches(g[field], wv, tol) if field == "amount"
                           else rel_diff(float(g[field]), float(wv)) > tol)
                    if bad:
                        problems.append(f"{code} {epoch_to_naive_bj(ts)} {field} "
                                        f"{g[field]} != 聚合复算 {wv}")
        out[period] = {"day": day, "codes": list(codes), "rows": total_rows,
                       "problems": problems[:8], "problem_count": len(problems)}
        problems_all.extend(f"{period}: {p}" for p in problems)
    if problems_all:
        raise RuntimeError(f"栅格/时区/口径校准失败（fail-closed）: {problems_all[:5]}")
    return out


# ── 正式写入（镜像 R16：锁/反连接/读回/回滚/journal）──────────

def _existing_keys_nonempty(client: Any, table: str, keys: List[Tuple[str, str]],
                            ch_win: Sequence[str]) -> List[Tuple[str, str]]:
    """锁内反连接复核：目标 (day, code) 在表里已有的存量键（应为空）。"""
    codes = sorted({c for _, c in keys})
    found = set()
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        rows = _ro(client, f"""
SELECT DISTINCT code, toString(toDate(datetime)) AS d FROM {table}
WHERE datetime >= toDateTime('{ch_win[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch_win[1]}', 'Asia/Shanghai')
  AND code IN ({_sql_str(chunk)})""")
        for r in rows:
            found.add((str(r["d"]), str(r["code"])))
    return sorted(set(keys) & found)


def _readback_aggregate(client: Any, table: str, keys: List[Tuple[str, str]],
                        ch_win: Sequence[str], final: bool = False) -> Dict[Tuple[str, str], Tuple]:
    suffix = " FINAL" if final else ""
    codes = sorted({c for _, c in keys})
    out: Dict[Tuple[str, str], Tuple] = {}
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        rows = _ro(client, f"""
SELECT toString(toDate(datetime)) AS d, code, count() AS n,
       uniqExact(id) AS uid, sum(volume) AS v,
       if(countIf(amount IS NULL) > 0, NULL, sum(amount)) AS a
FROM {table}{suffix}
WHERE datetime >= toDateTime('{ch_win[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch_win[1]}', 'Asia/Shanghai')
  AND code IN ({_sql_str(chunk)}) GROUP BY d, code""")
        for r in rows:
            out[(str(r["d"]), str(r["code"]))] = (int(r["n"]), int(r["uid"]),
                                                  float(r["v"]),
                                                  _agg_amount_out(r["a"]))
    return out


def _id_reproduction_check(client: Any, candidates: Dict[str, List[Tuple]],
                           ch_win: Sequence[str], per_key: int = 25) -> Dict[str, dict]:
    """新写行 id 逐 id 复现（blake2b 公式回算 == 库内 id）；抽样每表 25 股票日全行。"""
    rng = random.Random(RNG_SEED)
    out: Dict[str, dict] = {}
    for period in TARGET_PERIODS:
        table = TABLE_BY_PERIOD[period]
        rows = candidates[period]
        by_key: Dict[Tuple[str, str], List[Tuple]] = {}
        for r in rows:
            by_key.setdefault((r[0], r[1].astimezone(CN_TZ).strftime("%Y-%m-%d")), []).append(r)
        sample_keys = rng.sample(sorted(by_key), min(per_key, len(by_key)))
        problems: List[str] = []
        checked = 0
        for code, day in sample_keys:
            got_rows = _ro(client, f"""
SELECT code, toUnixTimestamp(datetime) AS ts, id FROM {table} WHERE code = '{code}'
AND datetime >= toDateTime('{ch_win[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch_win[1]}', 'Asia/Shanghai')""")
            for r in got_rows:
                checked += 1
                want = blake2b_row_id(ID_BATCH, period, str(r["code"]), int(r["ts"]))
                if want != int(r["id"]):
                    problems.append(f"{code} ts={r['ts']} id={r['id']} != 回算 {want}")
        out[period] = {"sampled_keys": len(sample_keys), "ids_checked": checked,
                       "problems": problems[:8], "problem_count": len(problems)}
        if problems:
            raise RuntimeError(f"{table} 新行 id 逐 id 复现失败: {problems[:5]}")
    return out


def _sample_field_check(client: Any, candidates: Dict[str, List[Tuple]],
                        ch_win: Sequence[str], per_key: int = 25,
                        tol: float = 1e-6) -> Dict[str, dict]:
    """抽样逐字段读回：库内行 vs 5m 正式表重聚合复算（每表 25 股票日全根）。"""
    rng = random.Random(RNG_SEED)
    out: Dict[str, dict] = {}
    for period in TARGET_PERIODS:
        table = TABLE_BY_PERIOD[period]
        rows = candidates[period]
        by_key: Dict[Tuple[str, str], List[Tuple]] = {}
        for r in rows:
            by_key.setdefault((r[0], r[1].astimezone(CN_TZ).strftime("%Y-%m-%d")), []).append(r)
        sample_keys = rng.sample(sorted(by_key), min(per_key, len(by_key)))
        problems: List[str] = []
        for code, day in sample_keys:
            day_ch = day_window(day)  # 精确单北京日（防次日数据混入，2026-11-09 实测修正）
            src = _ro(client, f"""
SELECT toUnixTimestamp(datetime) AS ts, open, high, low, close, volume, amount
FROM {SOURCE_TABLE} WHERE code = '{code}'
AND datetime >= toDateTime('{day_ch[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{day_ch[1]}', 'Asia/Shanghai') ORDER BY ts""")
            if len(src) != 48:
                problems.append(f"{code} {day} 5m 源槽数 {len(src)} != 48")
                continue
            bars = [LC5Record(code, epoch_to_naive_bj(int(r["ts"])), float(r["open"]),
                              float(r["high"]), float(r["low"]), float(r["close"]),
                              int(r["volume"]), _agg_amount_in(r["amount"])) for r in src]
            want = {b.datetime.strftime("%Y-%m-%d %H:%M"): b
                    for b in aggregate_5m(bars, AGG_MINUTES[period])}
            got_rows = _ro(client, f"""
SELECT substring(toString(toDateTime(datetime, 'Asia/Shanghai')), 1, 16) AS dt,
open, high, low, close, volume, amount FROM {table} WHERE code = '{code}'
AND datetime >= toDateTime('{day} 00:00:00', 'Asia/Shanghai')
AND datetime < toDateTime('{day} 00:00:00', 'Asia/Shanghai') + INTERVAL 1 DAY""")
            got = {r["dt"]: r for r in got_rows}
            if len(got) != BARS_PER_CD[period] or set(got) != set(want):
                problems.append(f"{code} {day} 行数/栅格 {len(got)} != {BARS_PER_CD[period]}")
                continue
            for hm, wb in sorted(want.items()):
                g = got[hm]
                for field, wv in (("open", wb.open), ("high", wb.high), ("low", wb.low),
                                  ("close", wb.close), ("volume", wb.volume),
                                  ("amount", wb.amount)):
                    bad = (not _amount_matches(g[field], wv, tol) if field == "amount"
                           else rel_diff(float(g[field]), float(wv)) > tol)
                    if bad:
                        problems.append(f"{code} {hm} {field} {g[field]} != 复算 {wv}")
        out[period] = {"sampled": len(sample_keys), "problems": problems[:8],
                       "problem_count": len(problems)}
        if problems:
            raise RuntimeError(f"{table} 抽样字段校验失败: {problems[:5]}")
    return out


def formal_write(client_factory: Callable[[], Any], candidates: Dict[str, List[Tuple]],
                 ch_win: Sequence[str], journal: Callable[[dict], None],
                 batch_rows: int = 50_000, lock_owner: str = "aistock-engineer",
                 purpose: str = "") -> dict:
    """锁内逐表反连接 → 分批插入 15/30/60 → 读回验证 → 异常按精确 id 回滚。"""
    report = {"tables": {}, "status": "ok"}
    lock_client = client_factory()
    lock = DbWriteLock(lock_client, journal=journal)
    acquired = lock.acquire(LOCK_SCOPE, lock_owner,
                            purpose=purpose or f"15/30/60 从 5m 聚合补齐 {ID_BATCH}")
    journal({"event": "lock_acquired", **{k: acquired[k] for k in ("scope", "owner")}})
    inserted_ids: Dict[str, List[int]] = {p: [] for p in TARGET_PERIODS}
    try:
        client = client_factory()
        for period in TARGET_PERIODS:
            table = TABLE_BY_PERIOD[period]
            rows = candidates[period]
            keys = sorted({(r[1].astimezone(CN_TZ).strftime("%Y-%m-%d"), r[0]) for r in rows})
            existing = _existing_keys_nonempty(client, table, keys, ch_win)
            if existing:
                raise RuntimeError(f"{table}: 锁内反连接发现存量键 {len(existing)}，"
                                   f"例如 {existing[:5]}；fail-closed 中止")
            journal({"event": "anti_join_clean", "table": table, "keys": len(keys)})
            for i in range(0, len(rows), batch_rows):
                batch = rows[i:i + batch_rows]
                client.insert(table.split(".")[1], batch, column_names=INSERT_COLUMNS)
                journal({"event": "insert", "table": table, "rows": len(batch)})
            inserted_ids[period] = [r[9] for r in rows]

        # 读回验证：全键聚合（恰 ×16/8/4、id 无重复）+ FINAL 一致
        for period in TARGET_PERIODS:
            table = TABLE_BY_PERIOD[period]
            rows = candidates[period]
            want = _candidate_aggregate_nullsafe(rows)
            got = _readback_aggregate(client, table, sorted(want), ch_win)
            problems: List[str] = []
            for key, expect in want.items():
                actual = got.get(key)
                if actual is None:
                    problems.append(f"{key} 读回缺失")
                    continue
                if actual[0] != BARS_PER_CD[period] or actual[0] != expect[0] \
                        or actual[1] != actual[0]:
                    problems.append(f"{key} 聚合不一致 expect={expect} got={actual} "
                                    f"(应恰 {BARS_PER_CD[period]} 根且 id 无重复)")
                    continue
                if rel_diff(actual[2], expect[1]) > 1e-6 \
                        or not _amount_matches(actual[3], expect[2], 1e-6):
                    problems.append(f"{key} 量额不一致 expect={expect} got={actual}")
            got_final = _readback_aggregate(client, table, sorted(want), ch_win, final=True)
            if len(got_final) != len(got):
                problems.append(f"FINAL 键数 {len(got_final)} != raw {len(got)}")
            report["tables"][period] = {"rows": len(rows), "code_days": len(want),
                                        "readback_problems": problems[:10],
                                        "problem_count": len(problems)}
            if problems:
                report["status"] = "verify_failed"
                raise RuntimeError(f"{table} 读回验证失败 {len(problems)} 项")
            journal({"event": "readback_ok", "table": table,
                     "rows": len(rows), "code_days": len(want)})

        # 新行 id 逐 id 复现 + 抽样逐字段对 5m 重聚合复算
        report["id_reproduction"] = _id_reproduction_check(client, candidates, ch_win)
        journal({"event": "id_reproduction_ok"})
        report["sample_field_check"] = _sample_field_check(client, candidates, ch_win)
        journal({"event": "sample_field_check_ok"})
    except Exception as exc:
        journal({"event": "rollback_start", "error": f"{type(exc).__name__}: {exc}"})
        try:
            rollback_client = client_factory()
            for period in TARGET_PERIODS:
                table = TABLE_BY_PERIOD[period]
                ids = inserted_ids.get(period, [])
                for i in range(0, len(ids), 5000):
                    chunk = ids[i:i + 5000]
                    id_list = ",".join(str(x) for x in chunk)
                    rollback_client.command(
                        f"ALTER TABLE {table} DELETE WHERE id IN ({id_list}) "
                        f"SETTINGS mutations_sync = 1")
                journal({"event": "rollback_delete", "table": table, "ids": len(ids)})
            for period in TARGET_PERIODS:
                table = TABLE_BY_PERIOD[period]
                ids = inserted_ids.get(period, [])
                if ids:
                    id_list = ",".join(str(x) for x in ids[:5000])
                    left = _ro(rollback_client,
                               f"SELECT count() AS n FROM {table} WHERE id IN ({id_list})")
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


# ── CLI ───────────────────────────────────────────────────────

def _prepare(client: Any, days: Sequence[str], ch: Sequence[str],
             skip_calibration: bool) -> dict:
    """dry-run/formal-write 共用准备（只读 + 内存聚合，零写入）。"""
    trading = trading_days_gate(client, days)
    missing = {p: fetch_missing_keys(client, TABLE_BY_PERIOD[p], days, ch)
               for p in TARGET_PERIODS}
    union_codes = sorted({c for keys in missing.values() for _, c in keys})
    bars_by_key, rejects = fetch_5m_bars(client, union_codes, ch)
    created_at = datetime.now(timezone.utc)
    candidates: Dict[str, List[Tuple]] = {}
    for period in TARGET_PERIODS:
        built = build_agg_rows(bars_by_key, created_at,
                               allowed_keys=set(missing[period]))
        candidates[period] = built[period]
    plan = plan_report(candidates)
    inexact = [p for p in TARGET_PERIODS if not plan[p]["exact_bars_per_cd"]]
    if inexact:
        raise SystemExit(f"计划行数不精确（fail-closed）：{ {p: plan[p] for p in inexact} }")
    missing_union = sorted(set().union(*[set(v) for v in missing.values()]))
    bars_keys = {(day, code) for (code, day) in bars_by_key}  # 统一 (day, code) 键序
    reject_keys = {(r["day"], r["code"]) for r in rejects}
    skipped = [k for k in missing_union if k not in bars_keys and k not in reject_keys]
    probe = id_domain_probe(client)
    calibration = ({"skipped": True} if skip_calibration
                   else grid_calibration_probe(client, union_codes[:5]))
    return {"trading_days": trading, "missing": {p: len(v) for p, v in missing.items()},
            "missing_union": len(missing_union), "candidates": candidates,
            "plan": plan, "rejects": rejects,
            "skipped_no_5m": len(skipped), "skipped_no_5m_keys": skipped,
            "id_probe": probe, "calibration": calibration}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="15/30/60 分钟表从 5m 聚合窗口补齐（R19；只写 kline_minute_15/30/60）")
    parser.add_argument("--mode", choices=("audit", "dry-run", "formal-write"), required=True)
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD（含端点）")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD（含端点）")
    parser.add_argument("--max-window-days", type=int, default=DEFAULT_MAX_WINDOW_DAYS,
                        help=f"窗口天数上限（默认 {DEFAULT_MAX_WINDOW_DAYS}，防误操作）")
    parser.add_argument("--out-dir", default=None,
                        help="报告/journal 输出目录（默认 runtime/minute_agg_backfill_window/<start>_<end>）")
    parser.add_argument("--skip-calibration", action="store_true",
                        help="跳过存量 10-30 栅格校准（仅离线调试）")
    args = parser.parse_args(argv)

    # 窗口校验先于任何库访问（镜像 R16）
    win = resolve_window(args.start_date, args.end_date, args.max_window_days)
    days: List[str] = win["days"]
    ch: List[str] = win["ch_window"]
    out_dir = Path(args.out_dir) if args.out_dir else (
        Path(DEFAULT_RUNTIME_BASE) / "minute_agg_backfill_window" / f"{win['start']}_{win['end']}")
    out_dir.mkdir(parents=True, exist_ok=True)
    client = _default_client_factory()

    if args.mode == "audit":
        report = {"window": win}
        for period in TARGET_PERIODS:
            keys = fetch_missing_keys(client, TABLE_BY_PERIOD[period], days, ch)
            report[period] = {"missing_code_days": len(keys),
                              "distinct_codes": len({c for _, c in keys})}
            with (out_dir / f"missing_keys_{period}.tsv").open("w", encoding="utf-8") as handle:
                for day, code in keys:
                    handle.write(f"{day}\t{code}\n")
        (out_dir / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0

    prep = _prepare(client, days, ch, args.skip_calibration)
    report = {"mode": args.mode, "window": win, "trading_days": prep["trading_days"],
              "missing": prep["missing"], "missing_union": prep["missing_union"],
              "plan": prep["plan"], "rejects": len(prep["rejects"]),
              "skipped_no_5m": prep["skipped_no_5m"],
              "skipped_no_5m_keys": prep["skipped_no_5m_keys"][:20],
              "id_probe": prep["id_probe"], "calibration": prep["calibration"]}
    if prep["rejects"]:
        (out_dir / "rejects.json").write_text(
            json.dumps(prep["rejects"], ensure_ascii=False, indent=2), encoding="utf-8")

    if args.mode == "dry-run":
        report["status"] = "dry_run_ok"
        (out_dir / "dry_run_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0

    # formal-write：锁内逐表重新反连接复核后插入（复用锁外聚合结果做规模报告）
    journal = _journal_writer(out_dir / "journal.jsonl")
    write_report = formal_write(
        _default_client_factory, prep["candidates"], ch, journal,
        purpose=f"15/30/60 从 5m 聚合补齐 {win['start']}~{win['end']} {ID_BATCH}")
    report["formal_write"] = write_report
    (out_dir / "formal_write_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
