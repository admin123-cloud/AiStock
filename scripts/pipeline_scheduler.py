# -*- coding: utf-8 -*-
"""R24 项目内盘后管道调度器（APScheduler 编排，替代外部定时）。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261109_pipeline_scheduler_r24.md

设计：
  1. BlockingScheduler（APScheduler，项目已有依赖），时区 Asia/Shanghai，
     cron 工作日（mon-fri）16:15 触发 run_postclose_pipelines()；
  2. run_postclose_pipelines() 为可在测试中 import 的纯编排函数（依赖全部可注入）：
     - 顺序 subprocess：daily_kline_pipeline.py --formal-write（失败重试 3 次、
       间隔 600s）→ minute_collect_auto.py --mode formal-write（rc 0/1 继续，
       rc 3 报警终止，其余 rc fail-closed 终止）→
       minute_agg_backfill_window.py --mode formal-write --start-date <today-5d>
       --end-date <today>；
     - 每步每次尝试的 stdout/stderr 落 AISTOCK_DATA_ROOT/runtime/postclose_auto/
       （文件名含日期+阶段+尝试序号）；汇总 JSON 报告含各步 rc/耗时/关键计数，
       失败时 status=failure（含 failed_stage/alarm 标记）；
  3. 交易日判断走 trade_calendar（经 scheduler.trading_calendar.TradingCalendar，
     只读）；非交易日直接记 no_op 报告退出；判断本身异常时 fail-closed 记
     failure 报告退出（不静默跳过采集）；
  4. 健康自证：模块顶部版本号；启动时日志输出调度注册信息（job/触发器/下次
     运行时间）；SIGTERM/SIGINT 优雅退出（scheduler.shutdown(wait=False)）；
  5. CLI：--run-now 立即执行一次后退出（部署验证）；--once 注册 cron 后等待
     下一次触发、单次执行完毕即退出（容器手动调用）；缺省=常驻调度。

边界（任务书）：本文件只做编排与报告，自身不连 DB、不写业务表、不碰容器。
"""
from __future__ import annotations

__version__ = "1.0.0"

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent

# Asia/Shanghai 自 1991 年起固定 +08:00（无夏令时）；静态 tzinfo 免 tzdata 依赖，
# 策略与 scheduler/config.py 的 _BEIJING_TZINFO 一致。
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

JOB_ID = "postclose_pipelines"
CRON_DAY_OF_WEEK = "mon-fri"
CRON_HOUR = 16
CRON_MINUTE = 15

STAGE_DAILY = "daily_kline"
STAGE_MINUTE = "minute_collect"
STAGE_AGG = "minute_agg"
STAGE_ORDER = (STAGE_DAILY, STAGE_MINUTE, STAGE_AGG)

# daily_kline_pipeline 失败重试 3 次（首次 + 3 次重试 = 最多 4 次尝试）
DAILY_RETRY_MAX = 3
DAILY_RETRY_INTERVAL_SEC = 600.0

# minute_collect_auto.py 退出码语义（见其模块头）：0/1=可继续，3=源不可达报警，
# 4=降级；任务书只认 0/1 继续、3 报警终止，故 4 及其他值按 fail-closed 终止。
MINUTE_COLLECT_CONTINUE_RC = frozenset({0, 1})
MINUTE_COLLECT_ALARM_RC = 3

# 聚合窗口：当日往前 5 个自然日（含端点；脚本内部按交易日裁剪）
AGG_WINDOW_DAYS = 5

REPORT_SCHEMA = "postclose_auto/1"

logger = logging.getLogger("pipeline_scheduler")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def cn_now(now_fn: Optional[Callable[[], datetime]] = None) -> datetime:
    """当前北京时间（可注入 now_fn 供测试）。"""
    if now_fn is not None:
        dt = now_fn()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BEIJING_TZ)
        return dt.astimezone(BEIJING_TZ)
    return datetime.now(BEIJING_TZ)


def default_out_root() -> Path:
    """报告/日志根目录：AISTOCK_DATA_ROOT/runtime/postclose_auto（env 优先）。"""
    import sys as _sys

    _root = str(Path(__file__).resolve().parents[1])
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from utils.paths import data_root  # 延迟导入，保持模块 import 轻量

    return data_root() / "runtime" / "postclose_auto"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8")


def _load_json(path: Optional[Path]) -> Optional[dict]:
    if path is None or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _extract_key_counts(report: Optional[Mapping[str, Any]], limit: int = 24) -> dict:
    """从步骤自身报告 JSON 提取关键计数（顶层标量 + 一层展开的数值），尽力而为。"""
    if not isinstance(report, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, value in report.items():
        if len(out) >= limit:
            break
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif key in ("status", "trade_date") and isinstance(value, str):
            out[key] = value
        elif isinstance(value, Mapping):
            for sub_key, sub_value in list(value.items())[:12]:
                if len(out) >= limit:
                    break
                if isinstance(sub_value, (bool, int, float)):
                    out[f"{key}.{sub_key}"] = sub_value
    return out


# ---------------------------------------------------------------------------
# 交易日判断（默认实现走 trade_calendar，只读；测试注入 fake）
# ---------------------------------------------------------------------------

def _default_is_trading_day(day: date) -> bool:
    from scheduler.trading_calendar import TradingCalendar  # 延迟导入：调用时才查库

    probe = datetime(day.year, day.month, day.day, 12, 0, tzinfo=BEIJING_TZ)
    return bool(TradingCalendar.is_trading_day(probe))


# ---------------------------------------------------------------------------
# 阶段命令构造（纯函数，供单测校验契约）
# ---------------------------------------------------------------------------

def build_stage_commands(run_date: date, out_dir: Path) -> dict[str, dict[str, Any]]:
    """三阶段的 subprocess 命令与各自报告文件定位。"""
    stamp = run_date.strftime("%Y%m%d")
    daily_report = out_dir / f"{stamp}_{STAGE_DAILY}_report.json"
    return {
        STAGE_DAILY: {
            "cmd": [
                sys.executable, str(SCRIPTS_DIR / "daily_kline_pipeline.py"),
                "--formal-write", "--output", str(daily_report),
            ],
            "report_path": daily_report,
        },
        STAGE_MINUTE: {
            "cmd": [
                sys.executable, str(SCRIPTS_DIR / "minute_collect_auto.py"),
                "--mode", "formal-write", "--out-dir", str(out_dir),
            ],
            "report_glob": "report_formal-write_*.json",
        },
        STAGE_AGG: {
            "cmd": [
                sys.executable, str(SCRIPTS_DIR / "minute_agg_backfill_window.py"),
                "--mode", "formal-write",
                "--start-date", (run_date - timedelta(days=AGG_WINDOW_DAYS)).isoformat(),
                "--end-date", run_date.isoformat(),
                "--out-dir", str(out_dir),
            ],
            "report_path": out_dir / "formal_write_report.json",
        },
    }


def _default_runner(cmd: Sequence[str]) -> dict[str, Any]:
    proc = subprocess.run(
        list(cmd), cwd=str(PROJECT_ROOT), capture_output=True, text=True
    )
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _resolve_step_report(spec: Mapping[str, Any], out_dir: Path) -> Optional[Path]:
    path = spec.get("report_path")
    if path is not None:
        return Path(path) if Path(path).is_file() else None
    pattern = spec.get("report_glob")
    if not pattern:
        return None
    candidates = sorted(out_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# 单阶段执行（含重试），返回阶段结果 dict
# ---------------------------------------------------------------------------

def _run_stage(
    stage: str,
    spec: Mapping[str, Any],
    *,
    out_dir: Path,
    run_date: date,
    runner: Callable[[Sequence[str]], Mapping[str, Any]],
    retry_max: int,
    retry_interval_sec: float,
    sleep_fn: Callable[[float], None],
    log: logging.Logger,
) -> dict[str, Any]:
    stamp = run_date.strftime("%Y%m%d")
    cmd = list(spec["cmd"])
    attempts = 0
    started = time.monotonic()
    rc: Optional[int] = None
    stdout_paths: list[str] = []
    stderr_paths: list[str] = []
    while True:
        attempts += 1
        result = runner(cmd) or {}
        try:
            rc = int(result.get("returncode", -1))
        except (TypeError, ValueError):
            rc = -1
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        stdout_path = out_dir / f"{stamp}_{stage}.attempt{attempts}.stdout.log"
        stderr_path = out_dir / f"{stamp}_{stage}.attempt{attempts}.stderr.log"
        _write_text(stdout_path, stdout)
        _write_text(stderr_path, stderr)
        stdout_paths.append(stdout_path.name)
        stderr_paths.append(stderr_path.name)
        if rc == 0 or attempts > retry_max:
            break
        log.warning(
            "[%s] 第 %d 次尝试 rc=%s，%ds 后重试（最多重试 %d 次）",
            stage, attempts, rc, int(retry_interval_sec), retry_max,
        )
        sleep_fn(retry_interval_sec)
    elapsed = round(time.monotonic() - started, 3)
    log.info("[%s] 完成 rc=%s attempts=%d elapsed=%.1fs", stage, rc, attempts, elapsed)
    step_report_path = _resolve_step_report(spec, out_dir)
    return {
        "rc": rc,
        "attempts": attempts,
        "retries": max(0, attempts - 1),
        "elapsed_sec": elapsed,
        "stdout_logs": stdout_paths,
        "stderr_logs": stderr_paths,
        "step_report": str(step_report_path) if step_report_path else None,
        "key_counts": _extract_key_counts(_load_json(step_report_path)),
    }


# ---------------------------------------------------------------------------
# 纯编排函数（测试入口）
# ---------------------------------------------------------------------------

def run_postclose_pipelines(
    *,
    run_date: Optional[date] = None,
    out_root: Optional[Path] = None,
    mode: str = "manual",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    retry_interval_sec: float = DAILY_RETRY_INTERVAL_SEC,
    now_fn: Optional[Callable[[], datetime]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """盘后三管道编排。返回汇总报告 dict（同时原子写入报告文件）。

    status: success | failure | no_op；失败时含 failed_stage / alarm / reason。
    """
    log = logger_ or logger
    started_utc = datetime.now(timezone.utc)
    started_bj = started_utc.astimezone(BEIJING_TZ)
    resolved_date = run_date or cn_now(now_fn).date()
    out_dir = Path(out_root) if out_root else default_out_root()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = resolved_date.strftime("%Y%m%d")
    summary_path = out_dir / f"postclose_report_{stamp}.json"

    base_report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "version": __version__,
        "mode": mode,
        "run_date": resolved_date.isoformat(),
        "started_at_utc": started_utc.isoformat(),
        "started_at_beijing": started_bj.isoformat(),
        "out_dir": str(out_dir),
    }

    def _finish(extra: dict[str, Any]) -> dict:
        finished_utc = datetime.now(timezone.utc)
        report = {
            **base_report,
            **extra,
            "finished_at_utc": finished_utc.isoformat(),
            "finished_at_beijing": finished_utc.astimezone(BEIJING_TZ).isoformat(),
            "elapsed_sec": round((finished_utc - started_utc).total_seconds(), 3),
        }
        _atomic_write_json(summary_path, report)
        return report

    # -- 交易日判断（trade_calendar 只读；异常 fail-closed，不静默跳过采集） --
    check = is_trading_day or _default_is_trading_day
    try:
        trading = bool(check(resolved_date))
        trade_check_error = None
    except Exception as exc:  # noqa: BLE001 - 任何判断失败都不得继续编排
        trading, trade_check_error = None, f"{type(exc).__name__}: {exc}"
    if trading is None:
        log.error("交易日判断失败，fail-closed 终止：%s", trade_check_error)
        return _finish({
            "status": "failure",
            "failed_stage": "trading_day_check",
            "alarm": False,
            "reason": f"trading_day_check_failed: {trade_check_error}",
            "stages": {},
        })
    if not trading:
        log.info("[%s] 非交易日，no-op 退出（报告已记录）", resolved_date)
        return _finish({
            "status": "no_op",
            "failed_stage": None,
            "alarm": False,
            "reason": "non_trading_day",
            "stages": {},
        })

    # -- 顺序执行三阶段 --
    run = runner or _default_runner
    sleep = sleep_fn or time.sleep
    specs = build_stage_commands(resolved_date, out_dir)
    stage_results: dict[str, dict[str, Any]] = {}
    failed_stage: Optional[str] = None
    alarm = False
    reason: Optional[str] = None

    for stage in STAGE_ORDER:
        retry_max = DAILY_RETRY_MAX if stage == STAGE_DAILY else 0
        result = _run_stage(
            stage, specs[stage],
            out_dir=out_dir, run_date=resolved_date, runner=run,
            retry_max=retry_max, retry_interval_sec=retry_interval_sec,
            sleep_fn=sleep, log=log,
        )
        stage_results[stage] = result
        rc = result["rc"]
        # rc 语义：日线/聚合要求 rc==0；5m 采集 rc 0/1 均可继续（任务书）
        if stage == STAGE_MINUTE:
            stage_ok = rc in MINUTE_COLLECT_CONTINUE_RC
        else:
            stage_ok = rc == 0
        if stage_ok:
            continue
        failed_stage = stage
        if stage == STAGE_DAILY:
            reason = "daily_kline_retry_exhausted"
        elif stage == STAGE_MINUTE:
            if rc == MINUTE_COLLECT_ALARM_RC:
                alarm = True
                reason = "minute_collect_alarm_rc3_source_unreachable"
            else:
                reason = f"minute_collect_unexpected_rc_fail_closed: {rc}"
        else:
            reason = f"minute_agg_failed_rc: {rc}"
        break

    status = "success" if failed_stage is None else "failure"
    report = _finish({
        "status": status,
        "failed_stage": failed_stage,
        "alarm": alarm,
        "reason": reason,
        "trading_day": True,
        "stages": stage_results,
    })
    if status == "success":
        log.info("盘后管道完成：status=success stages=%s", list(stage_results))
    else:
        log.error(
            "盘后管道失败：failed_stage=%s alarm=%s reason=%s", failed_stage, alarm, reason
        )
    return report


def report_exit_code(report: Mapping[str, Any]) -> int:
    """成功/no-op 返回 0，失败返回 1（供 CLI / 容器退出码）。"""
    return 0 if report.get("status") in ("success", "no_op") else 1


# ---------------------------------------------------------------------------
# 调度器装配
# ---------------------------------------------------------------------------

def build_cron_trigger():
    """工作日 16:15（Asia/Shanghai）cron 触发器。"""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day_of_week=CRON_DAY_OF_WEEK,
        hour=CRON_HOUR,
        minute=CRON_MINUTE,
        timezone=BEIJING_TZ,
    )


def register_postclose_job(scheduler, job_func: Optional[Callable[[], Any]] = None):
    """向 scheduler 注册盘后管道 cron 任务；返回该 job。"""
    if job_func is None:
        def job_func() -> dict:  # type: ignore[misc]
            return run_postclose_pipelines(mode="scheduled")
    scheduler.add_job(
        job_func,
        trigger=build_cron_trigger(),
        id=JOB_ID,
        name="盘后三管道编排（daily→5m→聚合）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler.get_job(JOB_ID)


def _request_shutdown(scheduler, log: logging.Logger) -> None:
    """优雅退出：shutdown 不等待（已在跑的本次编排会自然跑完写完报告）。"""
    from apscheduler.schedulers import SchedulerNotRunningError

    try:
        scheduler.shutdown(wait=False)
        log.info("调度器已 shutdown（优雅退出）")
    except SchedulerNotRunningError:
        pass  # 已停止（如 --once 单次完成后的重复信号），静默即可


def _install_signal_handlers(scheduler, log: logging.Logger) -> None:
    def _handler(signum, _frame):  # pragma: no cover - 信号入口，逻辑在 _request_shutdown
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        log.info("收到信号 %s，优雅退出调度器", name)
        _request_shutdown(scheduler, log)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # 非主线程等场景：跳过注册
            log.warning("无法注册信号处理器 %s（非主线程？）", sig)


def _log_pipeline_result(pipeline_fn: Callable[..., dict]) -> dict:
    report = pipeline_fn(mode="scheduled")
    log = logging.getLogger("pipeline_scheduler")
    log.info(
        "盘后管道触发完成：status=%s failed_stage=%s alarm=%s",
        report.get("status"), report.get("failed_stage"), report.get("alarm"),
    )
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AiStock 盘后管道调度器（R24）：工作日 16:15（Asia/Shanghai）编排"
                    " 日线→5m 采集→分钟聚合三管道。",
    )
    parser.add_argument(
        "--run-now", action="store_true",
        help="立即执行一次 run_postclose_pipelines() 后退出（部署验证用；"
             "同样受交易日 no-op 与各步 rc 语义约束）",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="注册 cron 后等待下一次触发，单次执行完毕即优雅退出"
             "（容器健康检查外的手动单次调用）",
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    return parser


def _setup_logging(level: str = "INFO") -> None:
    if logger.handlers:  # 已配置（如测试或宿主进程）
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    logger.addHandler(handler)
    try:
        logger.setLevel(getattr(logging, level.upper()))
    except AttributeError:
        logger.setLevel(logging.INFO)


def serve(
    argv: Optional[Sequence[str]] = None,
    *,
    pipeline_fn: Optional[Callable[..., dict]] = None,
    scheduler_factory=None,
) -> int:
    """CLI 入口：默认常驻调度；--run-now 立即单次；--once 触发一次后退出。"""
    args = build_arg_parser().parse_args(argv)
    _setup_logging(args.log_level)
    pipeline = pipeline_fn or run_postclose_pipelines

    if args.run_now:
        logger.info("--run-now：立即执行一次盘后管道后退出（不启动调度器）")
        return report_exit_code(pipeline(mode="manual"))

    from apscheduler.schedulers.blocking import BlockingScheduler

    factory = scheduler_factory or BlockingScheduler
    scheduler = factory(timezone=BEIJING_TZ)
    register_postclose_job(scheduler, lambda: _log_pipeline_result(pipeline))

    if args.once:
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

        def _shutdown_after_first(_event):
            logger.info("--once：单次执行完毕，调度器退出")
            _request_shutdown(scheduler, logger)

        scheduler.add_listener(_shutdown_after_first, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    _install_signal_handlers(scheduler, logger)
    job = scheduler.get_job(JOB_ID)
    next_run = getattr(job, "next_run_time", None) if job else None
    logger.info(
        "调度注册：job_id=%s cron=[day_of_week=%s %02d:%02d %s] next_run=%s "
        "once=%s version=%s pid=%d",
        JOB_ID, CRON_DAY_OF_WEEK, CRON_HOUR, CRON_MINUTE, "Asia/Shanghai",
        next_run.isoformat() if next_run else "pending", args.once,
        __version__, os.getpid(),
    )
    try:
        scheduler.start()  # BlockingScheduler：阻塞直至 shutdown
    except (KeyboardInterrupt, SystemExit):
        logger.info("主循环退出，调度器收尾")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return serve(argv)


if __name__ == "__main__":
    raise SystemExit(main())
