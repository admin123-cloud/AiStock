# -*- coding: utf-8 -*-
"""分钟 5m 表缺口补全管道（窗口参数化；R16 泛化自 minute_backfill_1021_1030）。

数据源：本地 TDX vipdoc ``{sh,sz,bj}/fzline/*.lc5``（5 分钟线，32 字节结构）。
基准：日线正式表 ``stock.kline_daily`` 股票全集（排除 000xxx.SH / 880xxx.SH /
399xxx.SZ / 899xxx.BJ 指数；指数缺口不在本管道写入范围）。

流程与门禁（fail-closed，任一失败即中止且不写；治理完全镜像 1021_1030）：
  1. --mode audit        只读反连接出缺失 (code, day)；
  2. --mode dry-run      解析 lc5 → 门禁（格式/48 槽/单位整除/OHLC 值域/NEEQ/
                         日线 OHLC 包络 1% 与量额 3%）→ 腾讯 m5 抽样交叉验证
                         （价 ≤1%、量 ≤3%；深市量按日合计比对）→ 计划报告，零写入；
  3. --mode formal-write 持 DbWriteLock('kline_minute_backfill_window') →
                         锁内重跑目标键反连接（存量非空即中止）→ 分批插入
                         （id=cityHash64 同源确定性、created_at=批次 UTC 时间）→
                         读回验证（全键聚合 + FINAL 一致 + 抽样逐字段）→
                         验证失败按精确 id 列表回滚。

与 1021_1030 的差异（本切片边界，任务书 20261109_minute_backfill_window_r16）：
  - 窗口泛化：--start-date/--end-date（YYYY-MM-DD，含端点），默认拒绝 >16 天窗口；
  - **只写 stock.kline_minute_5**（15/30/60 不在本切片写入范围，缺口另行任务）；
  - 腾讯交叉验证无 10-21 硬编码排除（窗口即边界；腾讯无覆盖的 (code, day)
    如实报 no_tencent_coverage）。

最大化复用：窗口无关的解析/门禁/行构造/腾讯拉取直接 import 自
minute_backfill_1021_1030（其内部复用 utils/tdx_lc5）；仅窗口相关 SQL、
单表写入路径与无硬编码过滤的交叉验证在本文件重写并保持同构。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils.db_write_lock import DbWriteLock, LockError  # noqa: E402

# 复用 1021_1030 既有实现（任务书要求：优先 import 复用而非复制）
from minute_backfill_1021_1030 import (  # noqa: E402
    CN_TZ,
    INSERT_COLUMNS,
    PRICE_TOL,
    RUN_TAG,
    VOL_AMT_TOL,
    fetch_calendar_gate,
    fetch_daily_reference,
    minute_row_id,
    parse_and_gate,
    rel_diff,
    tencent_m5_fetch,
    _candidate_aggregate,
    _default_client_factory,
    _journal_writer,
    _ro,
    _sql_str,
)

__all__ = [
    "DEFAULT_MAX_WINDOW_DAYS", "TARGET_PERIOD", "TARGET_TABLE", "LOCK_SCOPE",
    "ch_window", "resolve_window", "trading_days_gate",
    "fetch_missing_keys", "fetch_daily_reference_checked",
    "crosscheck_sample", "pick_sample", "id_parity_probe", "formal_write", "main",
]

TARGET_PERIOD = "5m"
TARGET_TABLE = "stock.kline_minute_5"
LOCK_SCOPE = "kline_minute_backfill_window"
DEFAULT_MAX_WINDOW_DAYS = 16  # 防误操作：默认拒绝 >16 天窗口
DEFAULT_TDX_ROOT = r"D:\TDX\vipdoc"
DEFAULT_RUNTIME_BASE = r"F:\Aistock\data\data\runtime"


# ── 窗口参数 ───────────────────────────────────────────────────

def _parse_iso_date(text: str, label: str) -> date:
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"--{label} 必须是 YYYY-MM-DD：{text!r}") from exc


def ch_window(start: date, end: date) -> Tuple[str, str]:
    """窗口（北京墙面日，含端点）→ 库内 datetime 窗口（半开，北京钟字面量）。

    R27b：库内 datetime 已标注 Asia/Shanghai，窗口边界直接用北京墙 00:00；
    SQL 侧统一以 ``toDateTime(?, 'Asia/Shanghai')`` 解析。上界仍刻意取
    ``end+2 日 00:00``（与旧 UTC 形式同瞬间：``end+1 日 16:00 UTC``），
    多覆盖一个北京日对反连接无害。与 1021_1030.CH_WINDOW 同一公式
    （测试断言 parity）。
    """
    lo = datetime.combine(start, time(0, 0))
    hi = datetime.combine(end + timedelta(days=2), time(0, 0))
    return lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S")


def resolve_window(start_text: str, end_text: str,
                   max_window_days: int = DEFAULT_MAX_WINDOW_DAYS) -> Dict:
    """校验并解析窗口参数（纯函数，零库访问；非法即 SystemExit）。"""
    start = _parse_iso_date(start_text, "start-date")
    end = _parse_iso_date(end_text, "end-date")
    if end < start:
        raise SystemExit(f"窗口倒置：start={start} end={end}")
    if int(max_window_days) < 1:
        raise SystemExit(f"max_window_days 必须 >= 1：{max_window_days}")
    span = (end - start).days + 1
    if span > int(max_window_days):
        raise SystemExit(f"窗口 {span} 天 > 上限 {max_window_days} 天"
                         f"（默认 {DEFAULT_MAX_WINDOW_DAYS}，防误操作；如确需更长用 --max-window-days 显式放宽）")
    days = [(start + timedelta(days=i)).isoformat() for i in range(span)]
    return {"start": start.isoformat(), "end": end.isoformat(), "span_days": span,
            "days": days, "ch_window": list(ch_window(start, end))}


def trading_days_gate(client: Any, days: Sequence[str]) -> List[str]:
    """按 trade_calendar 过滤出窗口内交易日；全窗口无交易日即 fail-closed。

    trade_calendar 无行的日期（周末/节假日）视为非交易日；is_trading=0 同样排除。
    """
    cal = fetch_calendar_gate(client, days)
    trading = [d for d in days if cal.get(d)]
    if not trading:
        raise SystemExit(f"窗口内无交易日（trade_calendar）：{list(days)}")
    return trading


# ── 只读数据库查询（窗口参数化）────────────────────────────────

def fetch_missing_keys(client: Any, table: str, days: Sequence[str],
                       ch_win: Sequence[str]) -> List[Tuple[str, str]]:
    """以日线股票全集为基准，反连接出 (day, code) 缺失清单（只读；窗口参数化）。"""
    rows = _ro(client, f"""
WITH u AS (SELECT trade_date AS td, code FROM stock.kline_daily
      WHERE trade_date IN ({_sql_str(days)})
        AND NOT (match(code,'^(000|880)[0-9]{{3}}\\\\.SH$') OR match(code,'^399[0-9]{{3}}\\\\.SZ$')
                 OR match(code,'^899[0-9]{{3}}\\\\.BJ$'))),
m AS (SELECT DISTINCT toDate(datetime) AS td, code FROM {table}
      WHERE datetime >= toDateTime('{ch_win[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch_win[1]}', 'Asia/Shanghai'))
SELECT u.td AS td, u.code AS code FROM u LEFT ANTI JOIN m ON u.code=m.code AND u.td=m.td
ORDER BY u.code, u.td""")
    return [(str(r["td"]), str(r["code"])) for r in rows]


def fetch_daily_reference_checked(client: Any, codes: Sequence[str],
                                  days: Sequence[str]) -> Dict:
    """日线基准行；days 必填显式传窗口（防回落到 1021_1030 默认窗口的回归）。"""
    if not days:
        raise SystemExit("fetch_daily_reference_checked: days 不能为空")
    return fetch_daily_reference(client, codes, days)


# ── 腾讯 m5 交叉验证（镜像 1021_1030，去掉 10-21 硬编码过滤）──

def crosscheck_sample(candidates: Dict[str, List[Tuple]], missing_keys: List[Tuple[str, str]],
                      sample_codes: Sequence[str], fetcher: Optional[Callable[[str], dict]] = None,
                      price_tol: float = PRICE_TOL, vol_tol: float = VOL_AMT_TOL) -> dict:
    """抽样与腾讯 m5 逐值比对。沪市逐根；深市价逐根、量按日合计（分钟边界口径差异）。

    与 1021_1030 的差异：不再硬编码排除 <10-22 的日期——窗口即边界；腾讯
    m5（最近 ~320 根）无覆盖的 (code, day) 如实报 no_tencent_coverage。
    """
    our_bars: Dict[Tuple[str, str], Dict] = {}
    for row in candidates["5m"]:
        code, dt = row[0], row[1]
        day = dt.astimezone(CN_TZ).strftime("%Y-%m-%d")
        our_bars.setdefault((code, day), {})[dt.astimezone(CN_TZ).strftime("%H:%M")] = row
    results = []
    pass_n = fail_n = 0
    for code in sample_codes:
        days = sorted({d for (d, c) in missing_keys if c == code})
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
                seed: int = 20261109) -> List[str]:
    """按市场分层抽样（SH/SZ 各 n；BJ 等其他市场不进腾讯抽样池）。"""
    codes_by_mkt: Dict[str, list] = {"SH": [], "SZ": []}
    for day, code in missing_keys:
        mkt = code.split(".")[1]
        if mkt in codes_by_mkt:
            codes_by_mkt[mkt].append(code)
    rng = random.Random(seed)
    sample = []
    for mkt, n in (("SH", n_sh), ("SZ", n_sz)):
        pool = sorted(set(codes_by_mkt[mkt]))
        sample.extend(rng.sample(pool, min(n, len(pool))))
    return sample


# ── id parity 探针（窗口参数化）────────────────────────────────

def id_parity_probe(client: Any, start: date) -> dict:
    """Python 端 minute_row_id 与服务器 cityHash64 parity 探针（窗口首日 09:35）。"""
    probe_dt = datetime.combine(start, time(9, 35))
    tag = f"{RUN_TAG}|{TARGET_PERIOD}"
    got = _ro(client,
              f"SELECT cityHash64('000698.SZ', toDateTime('{probe_dt:%Y-%m-%d %H:%M:%S}',"
              f"'Asia/Shanghai'), '{tag}') AS sid")
    return {"python": minute_row_id("000698.SZ", probe_dt, TARGET_PERIOD),
            "server": int(got[0]["sid"])}


# ── 正式写入（仅 stock.kline_minute_5）────────────────────────

def _existing_keys_nonempty(client: Any, keys: List[Tuple[str, str]],
                            ch_win: Sequence[str]) -> List[Tuple[str, str]]:
    """锁内反连接复核：目标 (code, day) 在表里已有的存量键（应为空）。"""
    codes = sorted({c for _, c in keys})
    found = set()
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        rows = _ro(client, f"""
SELECT DISTINCT code, toString(toDate(datetime)) AS d FROM {TARGET_TABLE}
WHERE datetime >= toDateTime('{ch_win[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch_win[1]}', 'Asia/Shanghai')
  AND code IN ({_sql_str(chunk)})""")
        for r in rows:
            found.add((str(r["d"]), str(r["code"])))
    return sorted(set(keys) & found)


def _readback_aggregate(client: Any, keys: List[Tuple[str, str]], ch_win: Sequence[str],
                        final: bool = False) -> Dict[Tuple[str, str], Tuple]:
    suffix = " FINAL" if final else ""
    codes = sorted({c for _, c in keys})
    out: Dict[Tuple[str, str], Tuple] = {}
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        rows = _ro(client, f"""
SELECT toString(toDate(datetime)) AS d, code, count() AS n,
       sum(volume) AS v, sum(amount) AS a FROM {TARGET_TABLE}{suffix}
WHERE datetime >= toDateTime('{ch_win[0]}', 'Asia/Shanghai') AND datetime < toDateTime('{ch_win[1]}', 'Asia/Shanghai')
  AND code IN ({_sql_str(chunk)}) GROUP BY d, code""")
        for r in rows:
            out[(str(r["d"]), str(r["code"]))] = (int(r["n"]), float(r["v"]), float(r["a"]))
    return out


def _sample_field_check(client: Any, rows: List[Tuple], rng: random.Random,
                        per_key: int = 25) -> dict:
    """抽样逐字段读回（48 根全字段），浮点按相对 1e-6（镜像 1021_1030，仅 5m）。"""
    by_key: Dict[Tuple[str, str], List[Tuple]] = {}
    for r in rows:
        by_key.setdefault((r[0], r[1].astimezone(CN_TZ).strftime("%Y-%m-%d")), []).append(r)
    sample_keys = rng.sample(sorted(by_key), min(per_key, len(by_key)))
    problems = []
    for code, day in sample_keys:
        want = {r[1].astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M"): r for r in by_key[(code, day)]}
        got_rows = _ro(client, f"""
        SELECT substring(toString(toDateTime(datetime, 'Asia/Shanghai')), 1, 16) AS dt,
        open, high, low, close, volume, amount
        FROM {TARGET_TABLE} WHERE code = '{code}'
        AND datetime >= toDateTime('{day} 00:00:00', 'Asia/Shanghai')
        AND datetime < toDateTime('{day} 00:00:00', 'Asia/Shanghai') + INTERVAL 1 DAY""")
        got = {r["dt"]: r for r in got_rows}
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
    out = {"sampled": len(sample_keys), "problems": problems[:8], "problem_count": len(problems)}
    if problems:
        raise RuntimeError(f"{TARGET_TABLE} 抽样字段校验失败: {problems[:5]}")
    return out


def formal_write(client_factory: Callable[[], Any], rows_5m: List[Tuple], ch_win: Sequence[str],
                 journal: Callable[[dict], None], batch_rows: int = 50_000,
                 lock_owner: str = "aistock-engineer", purpose: str = "") -> dict:
    """锁内反连接复核 → 分批插入 5m → 读回验证 → 异常按精确 id 回滚（仅 kline_minute_5）。"""
    report = {"table": TARGET_TABLE, "tables": {}, "status": "ok"}
    lock_client = client_factory()
    lock = DbWriteLock(lock_client, journal=journal)
    acquired = lock.acquire(LOCK_SCOPE, lock_owner,
                            purpose=purpose or f"分钟 5m 窗口补全 {RUN_TAG}")
    journal({"event": "lock_acquired", **{k: acquired[k] for k in ("scope", "owner")}})
    inserted_ids: List[int] = []
    try:
        client = client_factory()
        keys = sorted({(r[1].astimezone(CN_TZ).strftime("%Y-%m-%d"), r[0]) for r in rows_5m})
        existing = _existing_keys_nonempty(client, keys, ch_win)
        if existing:
            raise RuntimeError(f"{TARGET_TABLE}: 锁内反连接发现存量键 {len(existing)}，"
                               f"例如 {existing[:5]}；fail-closed 中止")
        journal({"event": "anti_join_clean", "table": TARGET_TABLE, "keys": len(keys)})
        for i in range(0, len(rows_5m), batch_rows):
            batch = rows_5m[i:i + batch_rows]
            client.insert(TARGET_TABLE.split(".")[1], batch, column_names=INSERT_COLUMNS)
            journal({"event": "insert", "table": TARGET_TABLE, "rows": len(batch)})
        inserted_ids = [r[9] for r in rows_5m]

        # 读回验证：全键聚合 + FINAL 一致 + 抽样逐字段
        want = _candidate_aggregate(rows_5m)
        got = _readback_aggregate(client, sorted(want), ch_win)
        problems = []
        for key, expect in want.items():
            actual = got.get(key)
            if actual is None:
                problems.append(f"{key} 读回缺失")
                continue
            if actual[0] != expect[0] or rel_diff(actual[1], expect[1]) > 1e-6 \
                    or rel_diff(actual[2], expect[2]) > 1e-6:
                problems.append(f"{key} 聚合不一致 expect={expect} got={actual}")
        got_final = _readback_aggregate(client, sorted(want), ch_win, final=True)
        if len(got_final) != len(got):
            problems.append(f"FINAL 键数 {len(got_final)} != raw {len(got)}")
        report["tables"][TARGET_PERIOD] = {"rows": len(rows_5m), "code_days": len(want),
                                           "readback_problems": problems[:10],
                                           "problem_count": len(problems)}
        if problems:
            report["status"] = "verify_failed"
            raise RuntimeError(f"{TARGET_TABLE} 读回验证失败 {len(problems)} 项")
        report["sample_field_check"] = _sample_field_check(client, rows_5m, random.Random(20261109))
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
            journal({"event": "rollback_delete", "table": TARGET_TABLE, "ids": len(inserted_ids)})
            if inserted_ids:
                id_list = ",".join(str(x) for x in inserted_ids[:5000])
                left = _ro(rollback_client,
                           f"SELECT count() AS n FROM {TARGET_TABLE} WHERE id IN ({id_list})")
                journal({"event": "rollback_verify", "table": TARGET_TABLE,
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

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="分钟 5m 窗口缺口补全（R16，泛化 1021_1030；只写 stock.kline_minute_5）")
    parser.add_argument("--mode", choices=("audit", "dry-run", "formal-write"), required=True)
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD（含端点）")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD（含端点）")
    parser.add_argument("--max-window-days", type=int, default=DEFAULT_MAX_WINDOW_DAYS,
                        help=f"窗口天数上限（默认 {DEFAULT_MAX_WINDOW_DAYS}，防误操作）")
    parser.add_argument("--tdx-root", default=DEFAULT_TDX_ROOT)
    parser.add_argument("--out-dir", default=None,
                        help="报告/journal 输出目录（默认 runtime/minute_backfill_window/<start>_<end>）")
    parser.add_argument("--no-crosscheck", action="store_true", help="dry-run 跳过腾讯抽样（离线调试）")
    parser.add_argument("--sample-sh", type=int, default=12)
    parser.add_argument("--sample-sz", type=int, default=12)
    args = parser.parse_args(argv)

    # 窗口校验先于任何库访问：非法参数默认零库访问即被拒绝
    win = resolve_window(args.start_date, args.end_date, args.max_window_days)
    days: List[str] = win["days"]
    ch: List[str] = win["ch_window"]
    out_dir = Path(args.out_dir) if args.out_dir else (
        Path(DEFAULT_RUNTIME_BASE) / "minute_backfill_window" / f"{win['start']}_{win['end']}")
    out_dir.mkdir(parents=True, exist_ok=True)
    client = _default_client_factory()

    if args.mode == "audit":
        keys = fetch_missing_keys(client, TARGET_TABLE, days, ch)
        report = {"window": win, "missing_code_days": len(keys),
                  "distinct_codes": len({c for _, c in keys})}
        with (out_dir / "missing_keys_5m.tsv").open("w", encoding="utf-8") as handle:
            for day, code in keys:
                handle.write(f"{day}\t{code}\n")
        (out_dir / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0

    keys = fetch_missing_keys(client, TARGET_TABLE, days, ch)
    trading = trading_days_gate(client, days)
    daily_ref = fetch_daily_reference_checked(client, sorted({c for _, c in keys}), days)
    print(f"missing 5m keys={len(keys)} daily_ref={len(daily_ref)}", flush=True)
    candidates, rejects = parse_and_gate(keys, daily_ref, Path(args.tdx_root))
    rows_5m = candidates[TARGET_PERIOD]
    plan = {"rows": len(rows_5m),
            "code_days": len({(r[0], r[1].astimezone(CN_TZ).strftime("%Y-%m-%d")) for r in rows_5m})}
    probe = id_parity_probe(client, date.fromisoformat(win["start"]))
    if probe["python"] != probe["server"]:
        raise SystemExit(f"id parity 探针失败: {probe}")
    report = {"mode": args.mode, "window": win, "trading_days": trading,
              "missing_keys_5m": len(keys), "plan": plan, "rejects": len(rejects),
              "id_probe": probe}
    if rejects:
        (out_dir / "rejects.json").write_text(json.dumps(rejects, ensure_ascii=False, indent=2),
                                              encoding="utf-8")

    if args.mode == "dry-run":
        if not args.no_crosscheck:
            sample = pick_sample(keys, args.sample_sh, args.sample_sz)
            report["crosscheck"] = crosscheck_sample({TARGET_PERIOD: rows_5m}, keys, sample)
            (out_dir / "crosscheck.json").write_text(
                json.dumps(report["crosscheck"], ensure_ascii=False, indent=2), encoding="utf-8")
        report["status"] = "dry_run_ok"
        (out_dir / "dry_run_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0

    # formal-write：锁内重新反连接复核后插入（复用锁外解析结果做规模报告）
    journal = _journal_writer(out_dir / "journal.jsonl")
    write_report = formal_write(
        _default_client_factory, rows_5m, ch, journal,
        purpose=f"分钟 5m {win['start']}~{win['end']} 窗口补全 {RUN_TAG}")
    report["formal_write"] = write_report
    (out_dir / "formal_write_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
