# -*- coding: utf-8 -*-
"""盘中快照隔离单拍入口（P0 首页盘中行情恢复·方案 B·R1，2026-11-17）。

架构方案：aistock-home-stale-20261117-architecture.md §6 方案 B / §7-R1。
容器形态：同镜像 bind-mount 本脚本，外部节拍（宿主计划任务每 5min docker start）单拍，
exit 即停；不 join runner 调度器，不做 mood 尾挂，不触碰任何 kline_* 正式表。

每拍守卫链：
  1. 硬窗口 09:35–15:05 北京钟（含端点；本地实现，不 import pipeline_scheduler，
     避免拖入 APScheduler 依赖树）→ 窗口外 rc=0 零动作；
  2. 交易日守卫：_default_is_trading_day 语义（trade_calendar 主源、probe 12:00 北京钟；
     判断异常 fail-closed 记失败跳拍，绝不放行采集）；
  3. intraday_snapshot_collect.run() 默认 fetcher（东财主源 + 腾讯 qt 自动降级）；
     锁 owner 运行时参数化为 aistock-intraday-standalone（scope 仍 intraday_snapshot_r28，
     collector 文件零修改，try/finally 恢复模块常量）；
  4. 单拍硬超时（默认 240s，可配置）：daemon 线程执行，超时记 failure 退出，
     防拖拍越窗（架构 §9 R-16:05/16:15；东财慢轮 ~4min + 余量）。

状态/报告：runtime/intraday_snapshot/standalone_state.json（与 runner 版
scheduler_state.json 分文件，避免双写方互踩）；collector 报告仍落同目录 report_*.json。
exit_code：0=success/no-op；3=源不可达（透传 collector）；1=其他失败（日历异常/锁冲突/
量不足/读回不符/写异常/超时）。连续失败 ≥3 轮置 alarm（状态文件可被监控读取）。

明确不做：mood 尾挂、任何 scheduler 注册、任何 kline_* 触碰、隐式破锁。
"""
from __future__ import annotations

__version__ = "1.0.0"

import argparse
import json
import sys
import threading
from datetime import date as date_type
from datetime import datetime, time as dt_time, timedelta, timezone
from logging import Logger
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
for _path in (str(PROJECT_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from utils.logger import get_logger  # noqa: E402
import intraday_snapshot_collect as isc  # noqa: E402

logger = get_logger("intraday_snapshot_standalone")

BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 硬窗口（含端点；语义对齐 pipeline_scheduler.INTRADAY_WINDOW_*，本地实现不 import scheduler）
WINDOW_START = dt_time(9, 35)
WINDOW_END = dt_time(15, 5)
ALARM_THRESHOLD = 3                 # 连续失败 ≥3 轮置 alarm（对齐 runner 版语义）
DEFAULT_HARD_TIMEOUT_SEC = 240.0    # 架构 §9：防拖拍越窗
LOCK_OWNER_STANDALONE = "aistock-intraday-standalone"  # 区分审计归属；scope 不变
STATE_SCHEMA = "intraday_snapshot_standalone/1"
STATE_FILENAME = "standalone_state.json"
RECENT_FAILURES_CAP = 20

_REPORT_SUMMARY_KEYS = (
    "status", "reason", "source", "valid_stock_rows", "skipped_no_price",
    "index_rows", "snapshot_ts", "error",
)


def in_intraday_window(now_bj: datetime) -> bool:
    """硬窗口守卫：09:35 ≤ 北京钟 ≤ 15:05（含端点；aware/naive 均按北京钟）。"""
    current = now_bj.timetz().replace(tzinfo=None) if now_bj.tzinfo else now_bj.time()
    return WINDOW_START <= current <= WINDOW_END


def _default_is_trading_day(day: date_type) -> bool:
    """交易日守卫（语义复刻 scheduler._default_is_trading_day，trade_calendar 主源）。

    异常向上传播，由 run_standalone 以 fail-closed 记失败跳拍。
    """
    from scheduler.trading_calendar import CalendarUnknownError, TradingCalendar  # 调用时才查库

    probe = datetime(day.year, day.month, day.day, 12, 0, tzinfo=BEIJING_TZ)
    result = TradingCalendar.resolve_trading_day(probe)
    if result.state == "unknown":
        raise CalendarUnknownError(f"{result.source}: {result.evidence}")
    return result.state == "trading"


def standalone_out_dir(out_root: Optional[Path] = None) -> Path:
    """状态与报告目录：runtime/intraday_snapshot（与 collector 默认 out_dir 同目录）。"""
    if out_root is not None:
        return Path(out_root) / "intraday_snapshot"
    from utils.paths import data_root  # 延迟导入：模块 import 零路径副作用

    return data_root() / "runtime" / "intraday_snapshot"


# ── 状态文件（与 runner 版 scheduler_state.json 分文件）────────

def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("schema", STATE_SCHEMA)
    state.setdefault("consecutive_failures", 0)
    state.setdefault("alarm", False)
    state.setdefault("last_success_ts", None)
    state.setdefault("last_failure_ts", None)
    state.setdefault("last_failure_reason", None)
    state.setdefault("recent_failures", [])
    return state


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _report_summary(report: Any) -> Optional[dict]:
    if not isinstance(report, Mapping):
        return None
    return {k: report.get(k) for k in _REPORT_SUMMARY_KEYS}


def _record_failure(state: dict, *, status: str, reason: Optional[str], rc: int,
                    now: datetime, summary: Optional[dict] = None) -> None:
    state["last_beat_at_beijing"] = now.isoformat()
    state["last_status"] = status
    state["last_rc"] = int(rc)
    state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
    state["alarm"] = state["consecutive_failures"] >= ALARM_THRESHOLD
    state["last_failure_ts"] = now.isoformat()
    state["last_failure_reason"] = str(reason)[:400] if reason else None
    state["recent_failures"] = (state.get("recent_failures", []) + [{
        "ts": state["last_failure_ts"], "rc": int(rc),
        "reason": state["last_failure_reason"],
    }])[-RECENT_FAILURES_CAP:]
    if summary is not None:
        state["last_report_summary"] = summary


def _record_success(state: dict, *, now: datetime,
                    summary: Optional[dict] = None) -> None:
    state["last_beat_at_beijing"] = now.isoformat()
    state["last_status"] = "success"
    state["last_rc"] = 0
    state["consecutive_failures"] = 0
    state["alarm"] = False
    state["last_success_ts"] = now.isoformat()
    state["last_failure_reason"] = None
    if summary is not None:
        state["last_report_summary"] = summary


# ── 单拍硬超时执行器 ───────────────────────────────────────────

def _run_with_hard_timeout(fn: Callable[[], Any], timeout_sec: float) -> tuple:
    """daemon 线程执行 fn() → ("ok", result) | ("error", exc) | ("timeout", None)。

    超时后 fn 所在 daemon 线程随进程退出被终结；届时可能残留的部分写入由
    collector 读回回滚门禁与表 TTL 30d 兜底（架构 §9 R-rollback-fail）。
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 含 LockHeldError 等全部异常面
            box["error"] = exc

    thread = threading.Thread(
        target=target, name="intraday-standalone-collector", daemon=True)
    thread.start()
    thread.join(timeout_sec)
    if thread.is_alive():
        return "timeout", None
    if "error" in box:
        return "error", box["error"]
    return "ok", box["result"]


# ── 单拍主流程 ─────────────────────────────────────────────────

def run_standalone(
    *,
    mode: str = "standalone",
    out_root: Optional[Path] = None,
    is_trading_day: Optional[Callable[[date_type], bool]] = None,
    collector_run: Optional[Callable[[Path], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    hard_timeout_sec: float = DEFAULT_HARD_TIMEOUT_SEC,
    lock_owner: str = LOCK_OWNER_STANDALONE,
    logger_: Optional[Logger] = None,
) -> dict:
    """单拍：硬窗口守卫 → 交易日守卫 → collector.run()（锁 owner 参数化 + 硬超时）→ 状态落盘。

    status: success | failure | no_op_window | no_op_non_trading | failure_calendar。
    rc: 0=success/no-op；3=源不可达（透传 collector）；1=其他失败。
    no_op_* 不动状态文件（零动作）；失败计数连续 ≥ALARM_THRESHOLD 轮置 alarm。
    """
    log = logger_ or logger
    now_bj = (now_fn or (lambda: datetime.now(BEIJING_TZ)))()
    if now_bj.tzinfo is None:
        now_bj = now_bj.replace(tzinfo=BEIJING_TZ)

    beat_dir = standalone_out_dir(out_root)
    beat_dir.mkdir(parents=True, exist_ok=True)
    state_path = beat_dir / STATE_FILENAME

    base = {
        "schema": STATE_SCHEMA,
        "mode": mode,
        "beat_at_beijing": now_bj.isoformat(),
    }

    # 1) 硬窗口守卫：窗口外 rc=0 零动作（不动状态文件）
    if not in_intraday_window(now_bj):
        log.info("[intraday-standalone] 窗口外（%s 北京），跳拍", now_bj.strftime("%H:%M"))
        return {**base, "status": "no_op_window", "reason": None, "rc": 0}

    # 2) 交易日守卫：判断异常 fail-closed 记失败跳拍（绝不放行采集）
    check = is_trading_day or _default_is_trading_day
    try:
        trading = bool(check(now_bj.date()))
    except Exception as exc:  # noqa: BLE001 - 日历判断异常 fail-closed
        log.error("[intraday-standalone] 交易日判断失败：%s", exc)
        reason = f"trading_day_check_failed: {exc}"
        state = _load_state(state_path)
        _record_failure(state, status="failure_calendar", reason=reason,
                        rc=1, now=now_bj)
        _atomic_write_json(state_path, state)
        return {**base, "status": "failure_calendar", "reason": reason, "rc": 1,
                "consecutive_failures": state["consecutive_failures"],
                "alarm": state["alarm"]}
    if not trading:
        log.info("[intraday-standalone] 非交易日（%s），跳拍", now_bj.date().isoformat())
        return {**base, "status": "no_op_non_trading", "reason": None, "rc": 0}

    # 3) 单拍采集：锁 owner 参数化（try/finally 恢复 collector 模块常量）+ 硬超时
    run_collector = collector_run or (lambda out_dir: isc.run(out_dir=out_dir))
    prev_owner = isc.LOCK_OWNER
    isc.LOCK_OWNER = str(lock_owner)
    try:
        # 超时竞态说明：超时路径 owner 常量先被恢复、daemon 线程随进程退出终结，
        # 残留风险由读回回滚门禁与 TTL 兜底（架构 §9）。
        outcome, payload = _run_with_hard_timeout(
            lambda: run_collector(beat_dir), hard_timeout_sec)
    finally:
        isc.LOCK_OWNER = prev_owner

    if outcome == "timeout":
        reason = "hard_timeout"
        log.error("[intraday-standalone] 单拍硬超时（>%.0fs），记失败退出",
                  hard_timeout_sec)
        state = _load_state(state_path)
        _record_failure(state, status="failure", reason=reason,
                        rc=1, now=now_bj)
        _atomic_write_json(state_path, state)
        return {**base, "status": "failure", "reason": reason, "rc": 1,
                "consecutive_failures": state["consecutive_failures"],
                "alarm": state["alarm"]}

    if outcome == "error":
        from utils.db_write_lock import LockHeldError  # 延迟导入（同 collector 惯例）

        exc = payload
        if isinstance(exc, LockHeldError):
            reason = "lock_held"
        else:
            reason = f"collector_exception: {type(exc).__name__}: {exc}"
        log.error("[intraday-standalone] 采集异常退出：%s", reason)
        state = _load_state(state_path)
        _record_failure(state, status="failure", reason=reason,
                        rc=1, now=now_bj)
        _atomic_write_json(state_path, state)
        return {**base, "status": "failure", "reason": reason, "rc": 1,
                "consecutive_failures": state["consecutive_failures"],
                "alarm": state["alarm"]}

    # 4) collector 报告 → exit_code 映射（0/3/1 透传）→ 状态迁移
    report = payload if isinstance(payload, Mapping) else {}
    rc = isc.exit_code(report)
    summary = _report_summary(report)
    if rc == 0:
        state = _load_state(state_path)
        _record_success(state, now=now_bj, summary=summary)
        _atomic_write_json(state_path, state)
        log.info("[intraday-standalone] 快照成功：stocks=%s indices=%s",
                 report.get("valid_stock_rows"), report.get("index_rows"))
        return {**base, "status": "success", "reason": None, "rc": 0,
                "consecutive_failures": 0, "alarm": False,
                "collector_report": summary}

    reason = str(report.get("reason") or "collector_failure")
    log.error("[intraday-standalone] 采集失败 rc=%s reason=%s", rc, reason)
    state = _load_state(state_path)
    _record_failure(state, status="failure", reason=f"collector_rc_{rc}: {reason}",
                    rc=rc, now=now_bj, summary=summary)
    _atomic_write_json(state_path, state)
    return {**base, "status": "failure", "reason": reason, "rc": rc,
            "consecutive_failures": state["consecutive_failures"],
            "alarm": state["alarm"], "collector_report": summary}


def main(argv: Optional[Sequence[str]] = None, *,
         now_fn: Optional[Callable[[], datetime]] = None,
         is_trading_day: Optional[Callable[[date_type], bool]] = None,
         collector_run: Optional[Callable[[Path], Mapping[str, Any]]] = None,
         logger_: Optional[Logger] = None) -> int:
    """CLI 入口（容器单拍 command）。返回进程 exit_code（0/3/1）。"""
    parser = argparse.ArgumentParser(
        description="AiStock 盘中快照隔离单拍入口（方案 B·R1）：窗口/交易日守卫 + "
                    "intraday_snapshot_collect 单拍采集。只写 intraday_market_snapshot / "
                    "intraday_index_snapshot 两张运营表，不触碰 kline_* 正式表，不做 mood 尾挂。")
    parser.add_argument(
        "--out-root", default=None,
        help="runtime 根目录（默认 AISTOCK_DATA_ROOT/runtime；状态与报告落其 "
             "intraday_snapshot/ 子目录）")
    parser.add_argument(
        "--hard-timeout-sec", type=float, default=DEFAULT_HARD_TIMEOUT_SEC,
        help=f"单拍硬超时秒数（默认 {DEFAULT_HARD_TIMEOUT_SEC:g}，超时记失败退出）")
    args = parser.parse_args(argv)
    report = run_standalone(
        out_root=Path(args.out_root) if args.out_root else None,
        is_trading_day=is_trading_day,
        collector_run=collector_run,
        now_fn=now_fn,
        hard_timeout_sec=args.hard_timeout_sec,
        logger_=logger_,
    )
    print(json.dumps({k: report.get(k) for k in (
        "status", "reason", "rc", "consecutive_failures", "alarm", "beat_at_beijing",
    )}, ensure_ascii=False))
    return int(report["rc"])


if __name__ == "__main__":
    raise SystemExit(main())
