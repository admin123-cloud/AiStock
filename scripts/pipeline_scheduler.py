# -*- coding: utf-8 -*-
"""R24 项目内盘后管道调度器（APScheduler 编排，替代外部定时）。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261109_pipeline_scheduler_r24.md

设计：
  1. BlockingScheduler（APScheduler，项目已有依赖），时区 Asia/Shanghai，
     cron 工作日（mon-fri）16:15 触发 run_postclose_pipelines()；
     R28（2026-11-10）：新增盘中任务 intraday_snapshot_collect——cron 工作日
     09:00~15:55 每 5min 触发，节拍函数内窗口守卫收口到 09:35~15:05，subprocess
     调 intraday_snapshot_collect.py 写两张运营层快照表（不碰 kline_* 正式表），
     连续失败 ≥3 轮置 alarm（状态落 runtime/intraday_snapshot/scheduler_state.json）；
  3. R37（2026-11-10）：L1 半自动做T三任务（全部 subprocess 调
     scripts/t_trade_tracker.py，产物在 runtime/t_tracker 运营层，零交易零
     formal 表写）——t_trade_regime 工作日 09:25 周期判定；t_trade_signal_beat
     工作日 09:36~14:56 每 5min（minute 1-56/5，与快照 */5 错峰 1min）；
     t_trade_reconcile 工作日 15:10 当日成交对账。16:15 盘后管道不受影响；
  4. R40（2026-11-10）：影子做T（observe_only）——t_trade_signal_beat 每拍附带影子
     评估（services/t_trade_shadow.py，独立异常面，失败不影响真节拍），产物
     runtime/t_tracker/shadow/，绝不进 pending_push；t_trade_shadow_finalize
     工作日 15:16 日终固化 + 4 周滚动统计。零交易零 formal 表写不变；
  5. R46（2026-11-11）：做T 1 分钟快车道 t_trade_signal_fast——东财 ulist 实时价
     + 5m 末根实时刷新重估 intrabar（R34 价带类触发），fast 草案 bar_status=open，
     慢车道收线复核升级 confirmed_bar / 瓦解 expired_unconfirmed；交易日 09:31~
     14:59 每分钟（cron 秒错峰 23s），票池=broker_state ∪ watchlist；影子同步
     快车道（--fast-tick 内嵌，独立异常面）。零交易零 formal 写不变；
  6. R42（2026-11-10）：QMT Tick 可用性告警任务 holding_tick_qmt_alert——
     GET /gen3-state-alpha/holding-tick-analysis 净化为纯读后，SMTP 告警外呼
     与告警状态写的通知通道。subprocess 调 scripts/holding_tick_qmt_alert.py
     --once；cron 工作日 09:00~15:50 每 10min，窗口守卫收口 09:25~15:10，
     脚本侧 30min 冷却限实际发信频率。零 formal 表写、零交易；
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

__version__ = "1.5.0"

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

_DEFAULT_SCRIPTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = _DEFAULT_SCRIPTS_DIR
PROJECT_ROOT = _DEFAULT_SCRIPTS_DIR.parent
JOBS_DIR = PROJECT_ROOT / "pipelines" / "jobs"


def _job_script(name: str) -> Path:
    """解析管道子脚本路径：测试若覆盖 SCRIPTS_DIR 则优先尊重，否则优先命中 pipelines/jobs/。"""
    if SCRIPTS_DIR != _DEFAULT_SCRIPTS_DIR:
        return SCRIPTS_DIR / name
    candidate = JOBS_DIR / name
    return candidate if candidate.exists() else (SCRIPTS_DIR / name)

# Asia/Shanghai 自 1991 年起固定 +08:00（无夏令时）；静态 tzinfo 免 tzdata 依赖，
# 策略与 scheduler/config.py 的 _BEIJING_TZINFO 一致。
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

JOB_ID = "postclose_pipelines"
CRON_DAY_OF_WEEK = "mon-fri"
CRON_HOUR = 16
CRON_MINUTE = 15

# R28（2026-11-10）：盘中东财实时快照采集任务——交易日 09:35~15:05 每 5min，
# 写 intraday_market_snapshot / intraday_index_snapshot 两张运营层表（非 kline_*）。
INTRADAY_JOB_ID = "intraday_snapshot_collect"
INTRADAY_CRON_MINUTE_STEP = 5
INTRADAY_CRON_HOURS = "9-15"      # 触发面 09:00~15:55，窗口守卫在函数内收口
INTRADAY_WINDOW_START = dt_time(9, 35)
INTRADAY_WINDOW_END = dt_time(15, 5)
# 连续失败 ≥3 轮置 alarm（任务书：失败退避重试，连续失败告警字段）
INTRADAY_ALARM_THRESHOLD = 3

# R55（2026-11-11）：涨停生态聚合（市场看板三件套③）——
#   intraday 追加：快照采集成功后同节拍 subprocess market_mood_update.py
#     --intraday（realtime 键，独立异常面：mood 失败不计入快照失败）；
#   finalize：工作日 16:40（盘后 16:15 管道后）subprocess --finalize
#     （final 键=kline_daily 收盘封板终算，覆盖盘中值；窗口 16:30~23:50 兜迟到）。
MARKET_MOOD_SCRIPT = "market_mood_update.py"
MARKET_MOOD_FINALIZE_JOB_ID = "market_mood_finalize"
MARKET_MOOD_FINALIZE_CRON_TIME = dt_time(16, 40)
MARKET_MOOD_FINALIZE_WINDOW_START = dt_time(16, 30)
MARKET_MOOD_FINALIZE_WINDOW_END = dt_time(23, 50)

# R37（2026-11-10）：L1 半自动做T三任务（产物 runtime/t_tracker，零交易零 formal 写）。
TTRACKER_REGIME_JOB_ID = "t_trade_regime"        # 工作日 09:25 盘前周期判定
TTRACKER_BEAT_JOB_ID = "t_trade_signal_beat"     # 工作日 09:36~14:56 每 5min 信号节拍
TTRACKER_RECONCILE_JOB_ID = "t_trade_reconcile"  # 工作日 15:10 当日成交对账
TTRACKER_SCRIPT = "t_trade_tracker.py"
TTRACKER_BROKER_SYNC_JOB_ID = "broker_state_sync"   # 工作日 15:15 fills→broker_state（R38c）
TTRACKER_BROKER_SYNC_SCRIPT = "broker_state_sync.py"
TTRACKER_SHADOW_FINALIZE_JOB_ID = "t_trade_shadow_finalize"  # 工作日 15:16 影子做T日终固化（R40）
# 做T信号质量日报（2026-11-13 Owner 要求）：工作日 15:20——收盘后读当日
# drafts（含 expired 终态）× kline_daily 收盘价，按 trigger_kind 出
# 触发数/确认率/瓦解率/触发→收盘 bps，产物 runtime/signal_quality/。
SIGNAL_QUALITY_JOB_ID = "signal_quality_report"
SIGNAL_QUALITY_SCRIPT = "signal_quality_report.py"
SIGNAL_QUALITY_CRON_TIME = dt_time(15, 20)
# R46（2026-11-11）：做T 1 分钟快车道——交易日 09:31~14:59 每分钟（cron 触发面
# 09:00~14:59，窗口守卫收口）；秒级错峰 23s（快照=分*/5 秒0、慢 beat=分1-56/5 秒0、
# PM watcher=分偶数 2min），与快照/慢 beat 均衡不撞线。
TTRACKER_FAST_JOB_ID = "t_trade_signal_fast"
TTRACKER_FAST_CRON_HOURS = "9-14"
TTRACKER_FAST_CRON_MINUTE = "*"
TTRACKER_FAST_CRON_SECOND = 23
TTRACKER_FAST_WINDOW_START = dt_time(9, 31)
TTRACKER_FAST_WINDOW_END = dt_time(14, 59)

# R42（2026-11-10，任务书 20261110_purify_holding_tick_analysis_r42.md）：
# QMT Tick 可用性告警任务——GET /gen3-state-alpha/holding-tick-analysis 净化为纯读后，
# SMTP 告警外呼与告警状态写的去处（scripts/holding_tick_qmt_alert.py 复用 gen3 内
# _notify_holding_tick_qmt_unavailable/_mark_holding_tick_qmt_available 原语义，
# 30 分钟冷却/交易时段/邮件开关门控不变）。cron 触发面 09:00~15:50，窗口收口
# 09:25~15:10；告警邮件冷却 30min，实际发信至多每 30 分钟一封。
HOLDING_TICK_ALERT_JOB_ID = "holding_tick_qmt_alert"
HOLDING_TICK_ALERT_SCRIPT = "holding_tick_qmt_alert.py"
HOLDING_TICK_ALERT_CRON_HOURS = "9-15"
HOLDING_TICK_ALERT_CRON_MINUTE = "*/10"
HOLDING_TICK_ALERT_WINDOW_START = dt_time(9, 25)
HOLDING_TICK_ALERT_WINDOW_END = dt_time(15, 10)
# 快照任务 minute=*/5（09:00/09:05/…），做T节拍 minute=1-56/5（09:01/09:06/…）→ 错峰 1min；
# 窗口收口后实际节拍 09:36~14:56（任务书：09:35~14:55 每 5min + 错峰 1min 语义）。
TTRACKER_BEAT_CRON_MINUTE = "1-56/5"
TTRACKER_BEAT_CRON_HOURS = "9-14"
TTRACKER_BEAT_WINDOW_START = dt_time(9, 36)
TTRACKER_BEAT_WINDOW_END = dt_time(14, 56)
TTRACKER_REGIME_CRON_TIME = dt_time(9, 25)
TTRACKER_RECONCILE_CRON_TIME = dt_time(15, 10)
TTRACKER_BROKER_SYNC_CRON_TIME = dt_time(15, 15)   # R38c：reconcile 后 5min
TTRACKER_SHADOW_FINALIZE_CRON_TIME = dt_time(15, 16)  # R40：15:15 后收盘已定，影子日终固化

# ── L2 t_trade_monitor（2026-11-12，任务书 20261112_l2_integration.md）────
# 工作日 09:25-15:00 每 30s 心跳（APScheduler cron 触发面 09:25~15:00 每分钟，
# 函数内二次守卫收口 09:25~15:00；cron 每分钟执行一次，函数内 30s 自循环）。
TTRADE_MONITOR_JOB_ID = "t_trade_monitor_v2"
TTRADE_MONITOR_CRON_HOURS = "9-15"
TTRADE_MONITOR_CRON_MINUTE = "25-59"  # 09:25~15:59 每分钟触发
TTRADE_MONITOR_WINDOW_START = dt_time(9, 25)
TTRADE_MONITOR_WINDOW_END = dt_time(15, 0)
TTRADE_MONITOR_HEARTBEAT_SEC = 30       # 每 30s 一次心跳
TTRADE_MONITOR_SUBLOOP_SEC = 30         # 自循环间隔（APScheduler 触发的函数内循环次数）

STAGE_INTRADAY = "intraday_snapshot"

STAGE_DAILY = "daily_kline"
STAGE_MINUTE = "minute_collect"
STAGE_AGG = "minute_agg"
STAGE_ORDER = (STAGE_DAILY, STAGE_MINUTE, STAGE_AGG)
# Owner 2026-11-13 裁决：Stage1 日线主源切本地 lday；官网包保留为自动回退源
STAGE_DAILY_FALLBACK = "daily_kline_pkg_fallback"
# 2026-11-19：Stage2 5m 主源切本地 tdx_lc5；东财保留为自动回退源
STAGE_MINUTE_FALLBACK = "minute_collect_east_fallback"

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
    from services.intraday_phase_gate import resolve_three_state_trading_day

    return resolve_three_state_trading_day(day)


# ---------------------------------------------------------------------------
# 阶段命令构造（纯函数，供单测校验契约）
# ---------------------------------------------------------------------------

def build_stage_commands(run_date: date, out_dir: Path) -> dict[str, dict[str, Any]]:
    """三阶段的 subprocess 命令与各自报告文件定位。"""
    stamp = run_date.strftime("%Y%m%d")
    daily_report = out_dir / f"{stamp}_{STAGE_DAILY}_report.json"
    daily_pkg_report = out_dir / f"{stamp}_{STAGE_DAILY_FALLBACK}_report.json"
    return {
        STAGE_DAILY: {
            # 2026-11-13 起：主源=客户端本地 vipdoc lday（时序门禁 mtime≥16:00
            # fail-closed；量纲/覆盖/对照门禁见 pipeline 报告）
            "cmd": [
                sys.executable, str(_job_script("daily_kline_pipeline.py")),
                "--source", "local", "--formal-write", "--output", str(daily_report),
            ],
            "report_path": daily_report,
        },
        # 本地 lday 失败（重试耗尽）时的官网包回退（exact-key 幂等，安全补写）
        STAGE_DAILY_FALLBACK: {
            "cmd": [
                sys.executable, str(_job_script("daily_kline_pipeline.py")),
                "--formal-write", "--output", str(daily_pkg_report),
            ],
            "report_path": daily_pkg_report,
        },
        STAGE_MINUTE: {
            # 2026-11-19 起：主源=容器内本地 /d/TDX/vipdoc lc5（覆盖全市场 A 股 + 11 大主要指数）
            "cmd": [
                sys.executable, str(_job_script("minute_collect_auto.py")),
                "--source", "tdx_lc5",
                "--mode", "formal-write", "--out-dir", str(out_dir),
            ],
            "report_glob": "report_formal-write_*.json",
        },
        STAGE_MINUTE_FALLBACK: {
            "cmd": [
                sys.executable, str(_job_script("minute_collect_auto.py")),
                "--source", "east",
                "--mode", "formal-write", "--out-dir", str(out_dir),
            ],
            "report_glob": "report_formal-write_*.json",
        },
        STAGE_AGG: {
            "cmd": [
                sys.executable, str(_job_script("minute_agg_backfill_window.py")),
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

def _resolve_publication_store(publication_store: Optional[Any], *, is_production_run: bool) -> Optional[Any]:
    """解析盘后发布控制面存储：显式注入优先；生产调度默认启用，测试 mock runner 默认跳过。"""
    if publication_store is not None:
        return publication_store
    if not is_production_run:
        return None
    try:
        from services.postclose_publication import PublicationStore

        return PublicationStore()
    except Exception:  # noqa: BLE001
        return None


def _next_publication_generation(pub_store: Any) -> int:
    try:
        raw = pub_store._read_raw_state()
        if isinstance(raw, dict):
            return max(int(raw.get("generation") or 0), 0) + 1
    except Exception:  # noqa: BLE001
        pass
    return 1


def _sync_publication_importing(
    pub_store: Optional[Any],
    *,
    resolved_date: date,
    plan_id: str,
    generation: int,
    now_bj: datetime,
    log: logging.Logger,
) -> None:
    if pub_store is None:
        return
    try:
        from services.postclose_contracts import StageState

        pub_store.write_stage(
            target_date=resolved_date,
            plan_id=plan_id,
            generation=generation,
            approval_id="scheduled_postclose_cutover",
            status=StageState.IMPORTING,
            reason="postclose_pipelines_running",
            now=now_bj,
        )
    except Exception as exc:  # noqa: BLE001 - 控制面同步独立异常面，不阻塞主管道
        log.warning("[publication] 写入 IMPORTING 控制面失败: %s", exc)


def _sync_publication_result(
    pub_store: Optional[Any],
    *,
    resolved_date: date,
    plan_id: str,
    generation: int,
    status: str,
    failed_stage: Optional[str],
    reason: Optional[str],
    stage_results: Mapping[str, Any],
    summary_path: Path,
    now_bj: datetime,
    log: logging.Logger,
) -> None:
    if pub_store is None:
        return
    try:
        from services.postclose_contracts import PublishCertificate, StageState
        from services.postclose.postclose_publication import compute_publish_certificate_expiry
        from services.postclose_source_prepare import canonical_sha256

        if status == "success":
            digest_payload = {
                "target_date": resolved_date.isoformat(),
                "plan_id": plan_id,
                "generation": generation,
                "stages": {k: {"rc": v.get("rc"), "attempts": v.get("attempts")} for k, v in stage_results.items()},
            }
            stage_digest = canonical_sha256(digest_payload)
            cert = PublishCertificate(
                target_date=resolved_date,
                plan_id=plan_id,
                generation=generation,
                approval_id="scheduled_postclose_cutover",
                issued_at=now_bj,
                expires_at=compute_publish_certificate_expiry(resolved_date, now_bj),
                issuer="postclose_orchestrator",
                evidence_refs=(str(summary_path),),
                schema_digest=canonical_sha256("postclose_auto/1"),
                manifest_digest=stage_digest,
                clean_digest=stage_digest,
                source_digest=stage_digest,
                verification_digest=stage_digest,
                coverage_status="complete",
                published_at=now_bj,
            )
            pub_store.publish_certified(cert, now=now_bj, reason="postclose_pipelines_success")
        else:
            pub_store.write_stage(
                target_date=resolved_date,
                plan_id=plan_id,
                generation=generation,
                approval_id="scheduled_postclose_cutover",
                status=StageState.IMPORT_FAILED,
                reason=reason or f"{failed_stage or 'postclose'}_failed",
                now=now_bj,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("[publication] 同步盘后发布结果失败: %s", exc)


def _run_tdx_sector_sync_once(log: Optional[logging.Logger] = None) -> bool:
    """执行一次通达信标准板块主档/成分股/近10日日线增量同步。"""
    lg = log or logger
    cmd = [sys.executable, "-m", "pipelines.jobs.tdx_sector_sync", "--formal-write", "--recent-days", "10"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0:
            lg.info("[tdx_sector_sync] 盘后板块增量同步完成")
            return True
        lg.warning("[tdx_sector_sync] 同步失败 rc=%s stderr=%s", proc.returncode, (proc.stderr or "")[-300:])
        return False
    except Exception as exc:  # noqa: BLE001
        lg.warning("[tdx_sector_sync] 执行异常: %s", exc)
        return False


def _run_ar46_postclose_sync_once(log: Optional[logging.Logger] = None) -> None:
    """执行一次 AR4.6 盘后维护：46只全天候及行业ETF + 国家队份额 + 南向资金 + ERP + 明日调仓预案。"""
    import urllib.request as _urlreq

    lg = log or logger
    now_bj = cn_now()
    opener = _urlreq.build_opener(_urlreq.ProxyHandler({}))
    base = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:3000").rstrip("/")

    def _post(path: str, payload: dict | None = None, timeout: int = 180) -> dict:
        data = json.dumps(payload or {}).encode("utf-8")
        req = _urlreq.Request(
            f"{base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(path: str, timeout: int = 90) -> dict:
        req = _urlreq.Request(f"{base}{path}", method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    is_fri = now_bj.weekday() == 4
    try:
        ar3_res = _post(f"/api/g3/mining/ar3/sync?sync_etf=true&sync_financials={'true' if is_fri else 'false'}", timeout=240)
        lg.info("[ar45_postclose_sync] 46只全天候及行业ETF同步完成 (fri_fin=%s): status=%s", is_fri, ar3_res.get("status"))
    except Exception as exc:  # noqa: BLE001
        lg.warning("[ar45_postclose_sync] AR3 46只ETF同步异常（继续AR4同步）: %s", exc)

    try:
        ar4_res = _post("/api/g3/mining/ar4/sync", timeout=180)
        sb = (ar4_res.get("sync") or {}).get("southbound") or {}
        lg.info(
            "[ar45_postclose_sync] 国家队份额+南向资金+ERP同步完成: status=%s sb20d=%s",
            ar4_res.get("status"), sb.get("sb_net_20d_yi"),
        )
    except Exception as exc:  # noqa: BLE001
        lg.warning("[ar45_postclose_sync] AR4 国家队/南向/ERP同步异常: %s", exc)

    try:
        stk_res = _post("/api/data-center/sync-stocks", timeout=120)
        lg.info(
            "[ar45_postclose_sync] 证券主数据 stock.stocks 增量同步完成: added=%s promoted=%s renamed_or_st=%s delisted=%s",
            stk_res.get("added_count"), stk_res.get("promoted_count"),
            stk_res.get("renamed_or_st_count"), stk_res.get("delisted_count"),
        )
    except Exception as exc:  # noqa: BLE001
        lg.warning("[ar45_postclose_sync] 证券主数据 stock.stocks 同步异常: %s", exc)

    try:
        plan = _get("/api/g3/mining/ar3/rebalance-plan?account_id=acct1&smooth_step=true&persist=true", timeout=90)
        _post("/api/g3/premarket-brief/generate", payload={"push_wechat": False}, timeout=90)
        lg.info(
            "[ar45_postclose_sync] 明日AR4.6调仓预案与简报缓存已刷新: regime=%s sells=%d buys=%d",
            plan.get("regime"), len(plan.get("sell_orders") or []), len(plan.get("buy_orders") or []),
        )
    except Exception as exc:  # noqa: BLE001
        lg.warning("[ar45_postclose_sync] 调仓预案/简报缓存刷新异常: %s", exc)


def _run_postclose_downstream_hooks(resolved_date: date, log: Optional[logging.Logger] = None) -> None:
    """盘后三管道认证成功后自动联动下游维护：板块同步 → 涨停生态终算 → 46只ETF/国家队/ERP同步。"""
    lg = log or logger
    lg.info("[postclose_cascade] 开始联动触发盘后下游维护任务（date=%s）", resolved_date.isoformat())

    # 1. 通达信标准板块增量同步（依赖当日 .day 文件与 kline_daily）
    _run_tdx_sector_sync_once(lg)

    # 2. 涨停生态与连板梯队终算（依赖 CERTIFIED kline_daily）
    mood_cmd = [
        sys.executable, "-m", "pipelines.jobs.market_mood_update",
        "--finalize", "--date", resolved_date.isoformat(), "--require-certified",
    ]
    try:
        proc = subprocess.run(mood_cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            lg.info("[postclose_cascade] market_mood 终算完成 rc=0")
        else:
            lg.warning("[postclose_cascade] market_mood 终算失败 rc=%s stderr=%s", proc.returncode, (proc.stderr or "")[-300:])
    except Exception as exc:  # noqa: BLE001
        lg.warning("[postclose_cascade] market_mood 终算异常: %s", exc)

    # 3. AR4.6 46只ETF + 国家队持仓份额 + 南向资金 + 沪深300 ERP + 明日预案同步
    _run_ar46_postclose_sync_once(lg)


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
    publication_store: Optional[Any] = None,
    post_success_hooks: Optional[Sequence[Callable[[date], Any]]] = None,
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

    # -- 顺序执行三阶段（P1-3：同步 S9/S11 发布控制面状态 IMPORTING -> CERTIFIED/IMPORT_FAILED） --
    pub_store = _resolve_publication_store(publication_store, is_production_run=(runner is None))
    pub_plan_id = f"postclose_auto_{stamp}"
    pub_generation = _next_publication_generation(pub_store) if pub_store is not None else 1
    _sync_publication_importing(
        pub_store,
        resolved_date=resolved_date,
        plan_id=pub_plan_id,
        generation=pub_generation,
        now_bj=cn_now(now_fn),
        log=log,
    )

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
        if stage == STAGE_DAILY:
            # Owner 2026-11-13：本地 lday 主源失败（重试耗尽）→ 官网包自动回退 + 报警。
            # 回退成功则视为当日日线已保障，继续 minute/聚合阶段（exact-key 幂等，
            # 两源同 id 反连接，绝不重复写）；回退也失败才整体 failure。
            log.error(
                "[daily] 本地 lday 源失败（rc=%s，重试耗尽），自动回退官网包并置 alarm", rc)
            fallback = _run_stage(
                STAGE_DAILY_FALLBACK, specs[STAGE_DAILY_FALLBACK],
                out_dir=out_dir, run_date=resolved_date, runner=run,
                retry_max=0, retry_interval_sec=retry_interval_sec,
                sleep_fn=sleep, log=log,
            )
            stage_results[STAGE_DAILY_FALLBACK] = fallback
            alarm = True
            if fallback["rc"] == 0:
                reason = "daily_local_lday_failed_pkg_fallback_ok"
                continue
            failed_stage = stage
            reason = "daily_local_lday_and_pkg_both_failed"
            break
        if stage == STAGE_MINUTE:
            # 2026-11-19：本地 tdx_lc5 主源失败 → 自动回退东财并置 alarm
            log.error(
                "[minute] 本地 tdx_lc5 源失败（rc=%s），自动回退东财源并置 alarm", rc)
            fallback = _run_stage(
                STAGE_MINUTE_FALLBACK, specs[STAGE_MINUTE_FALLBACK],
                out_dir=out_dir, run_date=resolved_date, runner=run,
                retry_max=0, retry_interval_sec=retry_interval_sec,
                sleep_fn=sleep, log=log,
            )
            stage_results[STAGE_MINUTE_FALLBACK] = fallback
            alarm = True
            if fallback["rc"] in MINUTE_COLLECT_CONTINUE_RC:
                reason = "minute_local_tdx_lc5_failed_east_fallback_ok"
                continue
            failed_stage = stage
            if fallback["rc"] == MINUTE_COLLECT_ALARM_RC:
                reason = "minute_collect_alarm_rc3_source_unreachable"
            else:
                reason = f"minute_collect_unexpected_rc_fail_closed: {fallback['rc']}"
            break
        failed_stage = stage
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
    _sync_publication_result(
        pub_store,
        resolved_date=resolved_date,
        plan_id=pub_plan_id,
        generation=pub_generation,
        status=status,
        failed_stage=failed_stage,
        reason=reason,
        stage_results=stage_results,
        summary_path=summary_path,
        now_bj=cn_now(now_fn),
        log=log,
    )
    if status == "success":
        log.info("盘后管道完成：status=success stages=%s", list(stage_results))
        if post_success_hooks is not None:
            for hook in post_success_hooks:
                try:
                    hook(resolved_date)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[postclose_cascade] 后置联动 hook 异常: %s", exc)
        elif runner is None:
            _run_postclose_downstream_hooks(resolved_date, log)
    else:
        log.error(
            "盘后管道失败：failed_stage=%s alarm=%s reason=%s", failed_stage, alarm, reason
        )
    return report


def report_exit_code(report: Mapping[str, Any]) -> int:
    """成功/no-op 返回 0，失败返回 1（供 CLI / 容器退出码）。"""
    return 0 if report.get("status") in ("success", "no_op") else 1


# ---------------------------------------------------------------------------
# R28 盘中东财快照节拍（交易日 09:35~15:05 每 5min）
# ---------------------------------------------------------------------------

def intraday_out_dir(out_root: Optional[Path] = None) -> Path:
    return (Path(out_root) if out_root else default_out_root()).parent / "intraday_snapshot"


def in_intraday_window(now_bj: datetime) -> bool:
    """窗口守卫：09:35:00 <= 北京钟 < 15:05:00（左闭右开；15:05:00 起硬截断禁写）。"""
    current = now_bj.timetz().replace(tzinfo=None) if now_bj.tzinfo else now_bj.time()
    return INTRADAY_WINDOW_START <= current < INTRADAY_WINDOW_END


def _load_intraday_state(path: Path) -> dict:
    state = _load_json(path)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("consecutive_failures", 0)
    state.setdefault("alarm", False)
    state.setdefault("last_success_ts", None)
    state.setdefault("last_failure_ts", None)
    state.setdefault("last_failure_reason", None)
    state.setdefault("recent_failures", [])
    return state


def run_intraday_snapshot_beat(
    *,
    mode: str = "scheduled",
    out_root: Optional[Path] = None,
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """盘中快照单节拍：窗口/交易日守卫 → subprocess 采集器 → 状态/告警落盘。

    status: success | failure | no_op_window | no_op_non_trading | failure_calendar。
    采集失败（含源不可达 rc=3、读回不符 rc=1）计入 consecutive_failures，
    连续 ≥INTRADAY_ALARM_THRESHOLD 轮置 alarm=True（状态文件可被监控读取）。
    """
    log = logger_ or logger
    now_bj = cn_now(now_fn)
    beat_dir = intraday_out_dir(out_root)
    beat_dir.mkdir(parents=True, exist_ok=True)
    state_path = beat_dir / "scheduler_state.json"
    state = _load_intraday_state(state_path)

    base = {
        "schema": "intraday_snapshot_beat/1",
        "mode": mode,
        "beat_at_beijing": now_bj.isoformat(),
    }

    if not in_intraday_window(now_bj):
        log.info("[intraday] 窗口外（%s 北京），跳过", now_bj.strftime("%H:%M"))
        return {**base, "status": "no_op_window"}

    check = is_trading_day or _default_is_trading_day
    try:
        trading = bool(check(now_bj.date()))
    except Exception as exc:  # noqa: BLE001 - 日历判断异常 fail-closed 记失败
        log.error("[intraday] 交易日判断失败：%s", exc)
        state["consecutive_failures"] += 1
        state["alarm"] = state["consecutive_failures"] >= INTRADAY_ALARM_THRESHOLD
        state["last_failure_ts"] = datetime.now(BEIJING_TZ).isoformat()
        state["last_failure_reason"] = f"trading_day_check_failed: {exc}"
        _atomic_write_json(state_path, state)
        return {**base, "status": "failure_calendar", "error": state["last_failure_reason"]}
    if not trading:
        return {**base, "status": "no_op_non_trading"}

    run = runner or _default_runner
    cmd = [
        sys.executable, str(_job_script("intraday_snapshot_collect.py")),
        "--out-dir", str(beat_dir),
    ]
    result = run(cmd) or {}
    try:
        rc = int(result.get("returncode", -1))
    except (TypeError, ValueError):
        rc = -1

    if rc == 0:
        state["consecutive_failures"] = 0
        state["alarm"] = False
        state["last_success_ts"] = datetime.now(BEIJING_TZ).isoformat()
        state["last_failure_reason"] = None
        status = "success"
        # R55：快照采集成功后追加涨停生态聚合（独立异常面：mood 失败不拖垮
        # 快照 success 状态，只记录 mood_rc）
        mood_rc = _run_market_mood_intraday(run, log)
    else:
        mood_rc = None
        state["consecutive_failures"] += 1
        state["alarm"] = state["consecutive_failures"] >= INTRADAY_ALARM_THRESHOLD
        state["last_failure_ts"] = datetime.now(BEIJING_TZ).isoformat()
        state["last_failure_reason"] = f"collector_rc_{rc}"
        state["recent_failures"] = (state["recent_failures"] + [{
            "ts": state["last_failure_ts"], "rc": rc,
            "stderr_tail": str(result.get("stderr") or "")[-400:],
        }])[-20:]
        status = "failure"
        log.error(
            "[intraday] 采集失败 rc=%s（连续 %d 轮，alarm=%s）",
            rc, state["consecutive_failures"], state["alarm"],
        )
    _atomic_write_json(state_path, state)
    return {
        **base,
        "status": status,
        "rc": rc,
        "mood_rc": mood_rc,
        "consecutive_failures": state["consecutive_failures"],
        "alarm": state["alarm"],
    }


def _run_market_mood_intraday(
    runner: Callable[[Sequence[str]], Mapping[str, Any]],
    log: logging.Logger,
) -> Optional[int]:
    """R55：盘中涨停生态聚合追加计算（subprocess，独立异常面）。"""
    cmd = [sys.executable, str(_job_script(MARKET_MOOD_SCRIPT)), "--intraday"]
    try:
        result = runner(cmd) or {}
        try:
            mood_rc = int(result.get("returncode", -1))
        except (TypeError, ValueError):
            mood_rc = -1
        if mood_rc != 0:
            log.warning("[intraday] market_mood 追加计算 rc=%s（不影响快照状态）", mood_rc)
        return mood_rc
    except Exception as exc:  # noqa: BLE001 - mood 独立异常面
        log.warning("[intraday] market_mood 追加计算异常：%s", exc)
        return None


def build_intraday_cron_trigger():
    """交易日面 cron：mon-fri 09:00~15:55 每 5min（窗口由节拍函数收口）。"""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day_of_week=CRON_DAY_OF_WEEK,
        hour=INTRADAY_CRON_HOURS,
        minute=f"*/{INTRADAY_CRON_MINUTE_STEP}",
        timezone=BEIJING_TZ,
    )


def register_intraday_job(scheduler, job_func: Optional[Callable[[], Any]] = None):
    """向 scheduler 注册盘中快照 cron 任务；返回该 job。"""
    if job_func is None:
        def job_func() -> dict:  # type: ignore[misc]
            return run_intraday_snapshot_beat(mode="scheduled")
    scheduler.add_job(
        job_func,
        trigger=build_intraday_cron_trigger(),
        id=INTRADAY_JOB_ID,
        name="盘中东财实时快照采集（R28，09:35~15:05 每 5min；R55 尾挂涨停生态聚合）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler.get_job(INTRADAY_JOB_ID)


# ---------------------------------------------------------------------------
# R55（2026-11-11）：涨停生态 finalize（盘后日线终算）
# ---------------------------------------------------------------------------

def in_market_mood_finalize_window(now_bj: datetime) -> bool:
    """窗口守卫：16:30 ≤ 北京钟 ≤ 23:50（cron 16:40 单发，窗口兜迟到重放）。"""
    current = now_bj.timetz().replace(tzinfo=None) if now_bj.tzinfo else now_bj.time()
    return MARKET_MOOD_FINALIZE_WINDOW_START <= current <= MARKET_MOOD_FINALIZE_WINDOW_END


def run_market_mood_finalize_beat(
    *,
    mode: str = "scheduled",
    out_root: Optional[Path] = None,
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
    require_certified: bool = False,
    control_root: Optional[Path] = None,
) -> dict:
    """R55 涨停生态终算节拍：窗口/交易日守卫 → subprocess --finalize。

    status: success | failure | no_op_window | no_op_non_trading | failure_calendar。
    幂等：finalize 重算同日 final 键（读侧取最新），重复跑无副作用。
    """
    log = logger_ or logger
    now_bj = cn_now(now_fn)
    base = {
        "schema": "market_mood_finalize_beat/1",
        "mode": mode,
        "beat_at_beijing": now_bj.isoformat(),
    }

    if not in_market_mood_finalize_window(now_bj):
        log.info("[market_mood] 终算窗口外（%s 北京），跳过", now_bj.strftime("%H:%M"))
        return {**base, "status": "no_op_window"}

    check = is_trading_day or _default_is_trading_day
    try:
        trading = bool(check(now_bj.date()))
    except Exception as exc:  # noqa: BLE001
        log.error("[market_mood] 交易日判断失败：%s", exc)
        return {**base, "status": "failure_calendar", "error": str(exc)}
    if not trading:
        return {**base, "status": "no_op_non_trading"}

    run = runner or _default_runner
    cmd = [
        sys.executable, str(_job_script(MARKET_MOOD_SCRIPT)),
        "--finalize", "--date", now_bj.date().isoformat(),
    ]
    if require_certified:
        cmd.append("--require-certified")
    if control_root is not None:
        cmd.extend(["--control-root", str(control_root)])
    try:
        result = run(cmd) or {}
        rc = int(result.get("returncode", -1))
    except Exception as exc:  # noqa: BLE001
        log.error("[market_mood] 终算执行异常：%s", exc)
        return {**base, "status": "failure", "error": str(exc)}
    if rc != 0:
        log.error("[market_mood] 终算失败 rc=%s", rc)
        return {**base, "status": "failure", "rc": rc}
    log.info("[market_mood] 终算完成 rc=0")
    return {**base, "status": "success", "rc": rc}


def build_market_mood_finalize_trigger():
    """交易日面 cron：mon-fri 16:40 单发（窗口由节拍函数收口）。"""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day_of_week=CRON_DAY_OF_WEEK,
        hour=MARKET_MOOD_FINALIZE_CRON_TIME.hour,
        minute=MARKET_MOOD_FINALIZE_CRON_TIME.minute,
        timezone=BEIJING_TZ,
    )


def register_market_mood_finalize_job(scheduler, job_func: Optional[Callable[[], Any]] = None):
    """向 scheduler 注册涨停生态终算 cron 任务；返回该 job。"""
    if job_func is None:
        def job_func() -> dict:  # type: ignore[misc]
            return run_market_mood_finalize_beat(mode="scheduled")
    scheduler.add_job(
        job_func,
        trigger=build_market_mood_finalize_trigger(),
        id=MARKET_MOOD_FINALIZE_JOB_ID,
        name="涨停生态终算（R55，工作日 16:40，kline_daily 收盘封板）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler.get_job(MARKET_MOOD_FINALIZE_JOB_ID)


# ---------------------------------------------------------------------------
# R37 L1 半自动做T三任务（regime 09:25 / beat 09:36~14:56 每5min / reconcile 15:10）
# ---------------------------------------------------------------------------

def in_t_tracker_beat_window(now_bj: datetime) -> bool:
    """窗口守卫：09:36 ≤ 北京钟 ≤ 14:56（cron 触发面 09:01~14:56 由它收口）。"""
    current = now_bj.timetz().replace(tzinfo=None) if now_bj.tzinfo else now_bj.time()
    return TTRACKER_BEAT_WINDOW_START <= current <= TTRACKER_BEAT_WINDOW_END


def in_t_tracker_fast_window(now_bj: datetime) -> bool:
    """R46 快车道窗口守卫：09:31 ≤ 北京钟 ≤ 14:59（cron 触发面 09:00~14:59 由它收口）。"""
    current = now_bj.timetz().replace(tzinfo=None) if now_bj.tzinfo else now_bj.time()
    return TTRACKER_FAST_WINDOW_START <= current <= TTRACKER_FAST_WINDOW_END


def _run_t_tracker_step(
    step: str,
    cli_flag: str,
    *,
    mode: str,
    now_bj: datetime,
    is_trading_day: Optional[Callable[[date], bool]],
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]],
    window_guard: Optional[Callable[[datetime], bool]] = None,
    logger_: Optional[logging.Logger] = None,
    script: str = TTRACKER_SCRIPT,
) -> dict:
    """做T任务通用节拍：窗口/交易日守卫 → subprocess → 报告 dict（不落盘状态）。"""
    log = logger_ or logger
    base = {"schema": "t_tracker_scheduler_step/1", "step": step, "mode": mode,
            "beat_at_beijing": now_bj.isoformat()}
    if window_guard is not None and not window_guard(now_bj):
        return {**base, "status": "no_op_window"}
    check = is_trading_day or _default_is_trading_day
    try:
        trading = bool(check(now_bj.date()))
    except Exception as exc:  # noqa: BLE001 - 日历判断异常 fail-closed 记失败
        log.error("[t_tracker:%s] 交易日判断失败：%s", step, exc)
        return {**base, "status": "failure", "reason": f"trading_day_check_failed: {exc}"}
    if not trading:
        return {**base, "status": "no_op_non_trading"}
    run = runner or _default_runner
    cmd = [sys.executable, str(_job_script(script)), cli_flag]
    result = run(cmd) or {}
    try:
        rc = int(result.get("returncode", -1))
    except (TypeError, ValueError):
        rc = -1
    if rc != 0:
        log.error("[t_tracker:%s] rc=%s stderr=%s", step, rc,
                  str(result.get("stderr") or "")[-300:])
    return {**base, "status": "success" if rc == 0 else "failure", "rc": rc}


def run_t_tracker_regime(
    *, mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """09:25 盘前周期判定（写 runtime/t_tracker/regime_<date>.json）。"""
    return _run_t_tracker_step(
        "regime", "--regime", mode=mode, now_bj=cn_now(now_fn),
        is_trading_day=is_trading_day, runner=runner, logger_=logger_,
    )


def run_t_tracker_beat(
    *, mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """盘中信号节拍（草案落 runtime/t_tracker/drafts + pending_push）。"""
    return _run_t_tracker_step(
        "beat", "--beat", mode=mode, now_bj=cn_now(now_fn),
        is_trading_day=is_trading_day, runner=runner,
        window_guard=in_t_tracker_beat_window, logger_=logger_,
    )


def run_t_tracker_fast_tick(
    *, mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """R46 快车道 1 分钟拍：实时价 intrabar 重估 → fast 草案（价带类触发）。"""
    return _run_t_tracker_step(
        "fast_tick", "--fast-tick", mode=mode, now_bj=cn_now(now_fn),
        is_trading_day=is_trading_day, runner=runner,
        window_guard=in_t_tracker_fast_window, logger_=logger_,
    )


def run_t_tracker_reconcile(
    *, mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """15:10 当日成交对账（台账 runtime/t_tracker/ledger/）。"""
    return _run_t_tracker_step(
        "reconcile", "--reconcile", mode=mode, now_bj=cn_now(now_fn),
        is_trading_day=is_trading_day, runner=runner, logger_=logger_,
    )


def run_broker_state_sync(
    *, mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """15:15 fills→broker_state 幂等同步（R38c；脚本自带 fail-closed 门禁）。"""
    return _run_t_tracker_step(
        "broker_state_sync", "--sync", mode=mode, now_bj=cn_now(now_fn),
        is_trading_day=is_trading_day, runner=runner, logger_=logger_,
        script=TTRACKER_BROKER_SYNC_SCRIPT,
    )


def run_t_tracker_shadow_finalize(
    *,
    mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """15:16 影子做T日终固化（R40；observe_only 台账 shadow/ + 4 周滚动统计）。"""
    return _run_t_tracker_step(
        "shadow_finalize", "--shadow-finalize", mode=mode, now_bj=cn_now(now_fn),
        is_trading_day=is_trading_day, runner=runner, logger_=logger_,
    )


def run_signal_quality_report(
    *,
    mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """15:20 信号质量日报（drafts×kline_daily → runtime/signal_quality JSON+MD）。

    脚本自带收盘价回退链（kline_daily→东财实时价）与幂等覆盖；当日无草案
    也产出零计数报告（rc 0），失败 rc 1 → 本步 failure。
    """
    return _run_t_tracker_step(
        "signal_quality", "--today", mode=mode, now_bj=cn_now(now_fn),
        is_trading_day=is_trading_day, runner=runner, logger_=logger_,
        script=SIGNAL_QUALITY_SCRIPT,
    )


def register_signal_quality_job(scheduler):
    """注册信号质量日报 cron（工作日 15:20，Asia/Shanghai）；返回该 job。"""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_signal_quality_report,
        trigger=CronTrigger(
            day_of_week=CRON_DAY_OF_WEEK,
            hour=SIGNAL_QUALITY_CRON_TIME.hour,
            minute=SIGNAL_QUALITY_CRON_TIME.minute,
            timezone=BEIJING_TZ,
        ),
        id=SIGNAL_QUALITY_JOB_ID,
        name="做T信号质量日报（工作日 15:20，drafts×kline_daily→runtime/signal_quality）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler.get_job(SIGNAL_QUALITY_JOB_ID)


WEEKLY_DATA_AUDIT_JOB_ID = "weekly_data_audit"
WEEKLY_DATA_AUDIT_SCRIPT = "run_weekly_data_audit.py"
WEEKLY_DATA_AUDIT_DAYS = 30
WEEKLY_DATA_AUDIT_DAY_OF_WEEK = "mon"
WEEKLY_DATA_AUDIT_CRON_TIME = dt_time(10, 30)


def run_weekly_data_audit(
    *,
    mode: str = "scheduled",
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """每周只读审计最近 30 个交易日数据交付（从 Windows 计划任务迁入项目内调度）。"""
    log = logger_ or logger
    now_bj = cn_now(now_fn)
    run = runner or _default_runner
    cmd = [
        sys.executable,
        str(_job_script(WEEKLY_DATA_AUDIT_SCRIPT)),
        "--days",
        str(WEEKLY_DATA_AUDIT_DAYS),
    ]
    result = run(cmd) or {}
    try:
        rc = int(result.get("returncode", -1))
    except (TypeError, ValueError):
        rc = -1
    if rc != 0:
        log.error("[weekly_data_audit] rc=%s stderr=%s", rc,
                  str(result.get("stderr") or "")[-300:])
    return {
        "step": WEEKLY_DATA_AUDIT_JOB_ID,
        "mode": mode,
        "date": now_bj.date().isoformat(),
        "status": "success" if rc == 0 else "failure",
        "rc": rc,
    }


def register_weekly_data_audit_job(scheduler):
    """注册每周数据交付只读审计（每周一 10:30，Asia/Shanghai）。"""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_weekly_data_audit,
        trigger=CronTrigger(
            day_of_week=WEEKLY_DATA_AUDIT_DAY_OF_WEEK,
            hour=WEEKLY_DATA_AUDIT_CRON_TIME.hour,
            minute=WEEKLY_DATA_AUDIT_CRON_TIME.minute,
            timezone=BEIJING_TZ,
        ),
        id=WEEKLY_DATA_AUDIT_JOB_ID,
        name="每周只读数据交付审计（周一 10:30，最近 30 个交易日）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler.get_job(WEEKLY_DATA_AUDIT_JOB_ID)


def in_holding_tick_alert_window(now_bj: datetime) -> bool:
    """R42 窗口守卫：09:25 ≤ 北京钟 ≤ 15:10（cron 触发面 09:00~15:50 由它收口）。"""
    current = now_bj.timetz().replace(tzinfo=None) if now_bj.tzinfo else now_bj.time()
    return HOLDING_TICK_ALERT_WINDOW_START <= current <= HOLDING_TICK_ALERT_WINDOW_END


def run_holding_tick_qmt_alert(
    *,
    mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    runner: Optional[Callable[[Sequence[str]], Mapping[str, Any]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """R42：QMT Tick 可用性告警节拍（holding-tick-analysis GET 净化后的通知通道）。

    --once 脚本内含告警闸门（30min 冷却/交易时段/邮件开关）与恢复闭环；
    窗口外/非交易日 no_op，交易日判断异常 fail-closed。
    """
    return _run_t_tracker_step(
        "holding_tick_qmt_alert", "--once", mode=mode, now_bj=cn_now(now_fn),
        is_trading_day=is_trading_day, runner=runner,
        window_guard=in_holding_tick_alert_window, logger_=logger_,
        script=HOLDING_TICK_ALERT_SCRIPT,
    )


def build_t_tracker_beat_cron_trigger():
    """做T节拍 cron：mon-fri 09:01~14:56 每 5min 错峰 +1min（窗口函数再收口）。"""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day_of_week=CRON_DAY_OF_WEEK,
        hour=TTRACKER_BEAT_CRON_HOURS,
        minute=TTRACKER_BEAT_CRON_MINUTE,
        timezone=BEIJING_TZ,
    )


def build_t_tracker_fast_cron_trigger():
    """R46 快车道 cron：mon-fri 09:00~14:59 每分钟，秒错峰 23s（窗口函数再收口
    09:31~14:59；秒 23 与快照（分*/5 秒0）/慢 beat（分1-56/5 秒0）不撞线）。"""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day_of_week=CRON_DAY_OF_WEEK,
        hour=TTRACKER_FAST_CRON_HOURS,
        minute=TTRACKER_FAST_CRON_MINUTE,
        second=TTRACKER_FAST_CRON_SECOND,
        timezone=BEIJING_TZ,
    )


def register_t_tracker_jobs(scheduler) -> list:
    """注册做T三任务（regime/beat/reconcile）；返回 job 列表。"""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_t_tracker_regime,
        trigger=CronTrigger(
            day_of_week=CRON_DAY_OF_WEEK,
            hour=TTRACKER_REGIME_CRON_TIME.hour,
            minute=TTRACKER_REGIME_CRON_TIME.minute,
            timezone=BEIJING_TZ,
        ),
        id=TTRACKER_REGIME_JOB_ID,
        name="L1做T·盘前周期判定（R37，工作日 09:25）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_t_tracker_beat,
        trigger=build_t_tracker_beat_cron_trigger(),
        id=TTRACKER_BEAT_JOB_ID,
        name="L1做T·盘中信号节拍（R37，09:36~14:56 每 5min 错峰快照 1min）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_t_tracker_fast_tick,
        trigger=build_t_tracker_fast_cron_trigger(),
        id=TTRACKER_FAST_JOB_ID,
        name="L1做T·1分钟快车道（R46，交易日 09:31~14:59 每分钟，秒错峰23）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_t_tracker_reconcile,
        trigger=CronTrigger(
            day_of_week=CRON_DAY_OF_WEEK,
            hour=TTRACKER_RECONCILE_CRON_TIME.hour,
            minute=TTRACKER_RECONCILE_CRON_TIME.minute,
            timezone=BEIJING_TZ,
        ),
        id=TTRACKER_RECONCILE_JOB_ID,
        name="L1做T·当日成交对账（R37，工作日 15:10）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_broker_state_sync,
        trigger=CronTrigger(
            day_of_week=CRON_DAY_OF_WEEK,
            hour=TTRACKER_BROKER_SYNC_CRON_TIME.hour,
            minute=TTRACKER_BROKER_SYNC_CRON_TIME.minute,
            timezone=BEIJING_TZ,
        ),
        id=TTRACKER_BROKER_SYNC_JOB_ID,
        name="L1做T·broker_state 成交同步（R38c，工作日 15:15）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_t_tracker_shadow_finalize,
        trigger=CronTrigger(
            day_of_week=CRON_DAY_OF_WEEK,
            hour=TTRACKER_SHADOW_FINALIZE_CRON_TIME.hour,
            minute=TTRACKER_SHADOW_FINALIZE_CRON_TIME.minute,
            timezone=BEIJING_TZ,
        ),
        id=TTRACKER_SHADOW_FINALIZE_JOB_ID,
        name="影子做T·日终固化+滚动统计（R40，工作日 15:16，observe_only 零推送）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return [scheduler.get_job(jid) for jid in
            (TTRACKER_REGIME_JOB_ID, TTRACKER_BEAT_JOB_ID, TTRACKER_FAST_JOB_ID,
             TTRACKER_RECONCILE_JOB_ID,
             TTRACKER_BROKER_SYNC_JOB_ID, TTRACKER_SHADOW_FINALIZE_JOB_ID)]


def register_holding_tick_alert_job(scheduler):
    """注册 R42 QMT Tick 可用性告警任务；返回该 job。

    holding-tick-analysis GET 净化后的通知通道：工作日 09:00~15:50 cron 每 10min，
    窗口守卫收口 09:25~15:10；脚本侧告警闸门（30min 冷却）限实际发信频率。
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_holding_tick_qmt_alert,
        trigger=CronTrigger(
            day_of_week=CRON_DAY_OF_WEEK,
            hour=HOLDING_TICK_ALERT_CRON_HOURS,
            minute=HOLDING_TICK_ALERT_CRON_MINUTE,
            timezone=BEIJING_TZ,
        ),
        id=HOLDING_TICK_ALERT_JOB_ID,
        name="QMT Tick 可用性告警（R42，工作日 09:25~15:10 每 10min）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler.get_job(HOLDING_TICK_ALERT_JOB_ID)


# ---------------------------------------------------------------------------
# L2 t_trade_monitor（2026-11-12，任务书 20261112_l2_integration.md）
# 工作日 09:25-15:00 每 30s 心跳：price_feed → RSI → 信号 → 弹窗 → 下单
# ---------------------------------------------------------------------------

_L2_LAST_SIGNAL: dict[str, datetime] = {}  # code:account → last signal time


def _is_trading_day_simple(d: date) -> bool:
    """交易日粗略判定（周一到周五，不含假期）。"""
    return d.weekday() < 5


# ── R56 L2 午休闸（外层，2026-11-16 任务书 l2_alert_trade_closure_v1 R3 裁决）──
# 与 services.t_trade_signal.l2_time_phase 同一套相位语义（北京时间）：
# 正式窗 [09:35,11:30)∪[13:00,14:55]；11:30–11:35 / 12:55–13:00 边界提示窗
# （不发交易信号）；11:35–12:55 静默；09:35 前 / 14:55 后不发。
# 边界 notice 按「日期+窗口」至多一次；模块级去重表进程内跨 beat 共享。
_L2_MIDDAY_NOTIFIED: dict[str, bool] = {}


def l2_midday_gate(
    now_bj: datetime,
    *,
    notified: Optional[dict] = None,
    is_trading_day: Optional[Callable[[date], bool]] = None,
) -> dict[str, Any]:
    """L2 午休闸纯函数（R56）：返回 {status, phase, run_heartbeat, notice}。

    - 交易日判断异常 → trading_day_check_failed（fail-closed 静默跳拍）；
    - 非交易日 → non_trading_noop；
    - trading → run_heartbeat=True；midday_silent / boundary_* / outside_window
      → run_heartbeat=False（边界窗 notice 按「日期+窗口」至多一次）。
    """
    check = is_trading_day or _is_trading_day_simple
    try:
        trading = bool(check(now_bj.date()))
    except Exception as exc:  # noqa: BLE001 - 日历源故障必须 fail-closed
        return {"status": "trading_day_check_failed", "phase": None,
                "run_heartbeat": False, "notice": False, "error": str(exc)}
    if not trading:
        return {"status": "non_trading_noop", "phase": None,
                "run_heartbeat": False, "notice": False}

    from services import t_trade_signal as _sig  # 延迟导入（同 _run_l2_heartbeat）

    if now_bj.tzinfo is not None:
        now_bj = now_bj.astimezone(BEIJING_TZ).replace(tzinfo=None)
    phase = _sig.l2_time_phase(now_bj.time())
    gate: dict[str, Any] = {
        "status": phase, "phase": phase,
        "run_heartbeat": phase == "trading", "notice": False,
    }
    if phase in ("boundary_am", "boundary_pm"):
        window = "am" if phase == "boundary_am" else "pm"
        seen = notified if notified is not None else _L2_MIDDAY_NOTIFIED
        key = f"{now_bj.date().isoformat()}|{window}"
        if not seen.get(key):
            seen[key] = True
            gate["notice"] = True
    return gate


def _run_l2_heartbeat(now_bj: datetime, log: logging.Logger) -> dict[str, Any]:
    """L2 单次心跳：行情采集 → RSI → 信号判定 → 弹窗。"""
    try:
        from services import intraday_price_feed as feed
        from services import t_trade_signal as sig
        from scripts import alert_local as alert
    except ImportError as exc:
        log.error("[t_trade_monitor] 模块导入失败: %s", exc)
        return {"status": "import_error", "error": str(exc)}

    report: dict[str, Any] = {
        "ts": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ok",
        "codes": {},
        "signals": [],
        "errors": [],
    }

    # 1. 加载持仓
    try:
        holdings = sig.load_t_pool()
        codes = [str(h.get("code") or "").split(".")[0] for h in holdings.get("holdings") or []]
    except Exception as exc:
        log.error("[t_trade_monitor] 持仓加载失败: %s", exc)
        report["status"] = "holdings_failed"
        report["errors"].append(str(exc))
        return report

    if not codes:
        return {**report, "status": "no_holdings"}

    # 2. 刷新行情缓存
    try:
        feed.refresh(codes)
    except Exception as exc:
        log.warning("[t_trade_monitor] 行情刷新失败: %s", exc)

    # 2.5 AR4.6 大盘总闸门（最高权威：非进攻期源头严禁生成任何 A 股个股 buy-T 草案）
    ar45_gate = {"allow_buy": False, "regime": "defensive"}
    try:
        from services.trading.t_notify import get_ar45_buy_gate_status

        ar45_gate = get_ar45_buy_gate_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("[t_trade_monitor] 读取 AR4.6 总闸门异常，按 fail-closed 拦截 buy-T: %s", exc)

    # 3. 逐票评估
    for holding in holdings.get("holdings") or []:
        code = str(holding.get("code") or "").split(".")[0]
        name = holding.get("name") or ""
        shares = int(holding.get("shares") or 0)
        account = str(holding.get("account") or "acct1")

        # 价格
        price = feed.get_latest_price(code)
        px_source = "price_cache"
        if price is None:
            try:
                bars5 = sig.fetch_east_klines(code, 5)
                live = [b for b in bars5 if (sig.parse_ts(str(b["ts"])) or now_bj) <= now_bj]
                if live:
                    price = float(live[-1]["close"])
                    px_source = "m5_close"
            except Exception:
                pass

        if price is None or price <= 0:
            report["codes"][code] = {"price": None, "error": "no_price"}
            continue

        # RSI 计算
        rsi15, rsi5 = None, None
        try:
            cls15 = [float(b["close"]) for b in sig.fetch_east_klines(code, 15)]
            cls5 = [float(b["close"]) for b in sig.fetch_east_klines(code, 5)]
            rsi15 = sig.rsi_wilder(cls15, 14) if len(cls15) > 14 else None
            rsi5 = sig.rsi_wilder(cls5, 14) if len(cls5) > 14 else None
        except Exception as exc:
            log.debug("[t_trade_monitor] %s RSI 计算失败: %s", code, exc)

        # L2 信号判定
        last_l2_time = _L2_LAST_SIGNAL.get(f"{code}:{account}")
        sig_result = sig.evaluate_l2_signal(
            code=code, name=name,
            price=price, price_source=px_source,
            shares=shares,
            rsi15=rsi15, rsi5=rsi5,
            now=now_bj,
            last_l2_time=last_l2_time,
        )

        code_report: dict[str, Any] = {
            "price": price, "px_source": px_source,
            "rsi15": rsi15, "rsi5": rsi5, "shares": shares,
        }

        if sig_result.get("signal"):
            signal = sig_result["signal"]
            draft = sig_result.get("draft")
            # 1) 冷却门禁生效时严禁重复落盘或弹窗（修复原先每 30 秒刷屏漏洞）
            if sig_result.get("cooldown_active"):
                code_report["suppressed"] = "cooldown"
                report["codes"][code] = code_report
                continue
            # 2) AR4.6 大盘总闸门拦截：防守/平衡/恐慌期严禁发出任何 A 股个股 buy-T 买入提醒
            if "buy" in str(signal).lower() and not ar45_gate.get("allow_buy"):
                code_report["suppressed"] = f"ar45_{ar45_gate.get('regime', 'defensive')}_buy_blocked"
                log.info(
                    "[t_trade_monitor] AR4.6 总闸门拦截 %s(%s) buy-T 信号（当前大盘=%s，严禁买A股）",
                    name,
                    code,
                    ar45_gate.get("regime"),
                )
                report["codes"][code] = code_report
                continue
            code_report["signal"] = signal
            if draft:
                code_report["draft_id"] = draft["id"]

            # 草案落盘
            try:
                sig.run_l2_beat(
                    code=code, name=name,
                    price=price, price_source=px_source,
                    shares=shares,
                    rsi15=rsi15, rsi5=rsi5,
                    now=now_bj,
                    last_l2_time=last_l2_time,
                    write_draft=True,
                )
            except Exception as exc:
                code_report["draft_error"] = str(exc)

            # 弹窗
            try:
                tier = draft.get("tier") if draft else None
                qty = draft.get("qty", 0) if draft else 0
                alert.show_trade_alert(
                    code=code, name=name,
                    side=signal, price=price, qty=qty,
                    tier=tier,
                    rsi15=rsi15, rsi5=rsi5,
                    extra=f"账号: {account}",
                )
                code_report["alert"] = True
            except Exception as exc:
                log.warning("[t_trade_monitor] %s 弹窗失败: %s", code, exc)
                code_report["alert_error"] = str(exc)

            # 冷却更新
            _L2_LAST_SIGNAL[f"{code}:{account}"] = now_bj
            report["signals"].append({
                "code": code, "account": account,
                "signal": signal, "draft_id": draft.get("id") if draft else None,
            })
        elif sig_result.get("suppressed"):
            code_report["suppressed"] = sig_result["suppressed"]["reason"]

        report["codes"][code] = code_report

    return report


def _run_l2_eod_report(now_bj: datetime) -> dict[str, Any]:
    """L2 日终报告：汇总当日 draft。"""
    try:
        from services import t_trade_signal as sig
    except ImportError:
        return {"status": "import_error"}
    report: dict[str, Any] = {
        "schema": "t_tracker_eod/1",
        "date": now_bj.date().isoformat(),
        "generated_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ok",
    }
    root = sig.tracker_dir()
    drafts_file = root / "drafts" / f"{now_bj.strftime('%Y%m%d')}.jsonl"
    try:
        drafts = sig.read_jsonl(drafts_file)
        l2 = [d for d in drafts if d.get("schema") == "t_tracker_l2_draft/1"]
        report["l2_drafts_total"] = len(l2)
        report["l2_signals"] = [{
            "id": d["id"], "code": d.get("code"), "side": d.get("side"),
            "tier": d.get("tier"), "ts": d.get("ts"),
        } for d in l2]
        report["all_drafts_total"] = len(drafts)
    except Exception as exc:
        report["error"] = str(exc)

    from utils.paths import runtime_path
    eod_path = runtime_path("t_tracker", "eod", f"{now_bj.strftime('%Y%m%d')}.json")
    eod_path.parent.mkdir(parents=True, exist_ok=True)
    import json, os
    tmp = eod_path.with_name(eod_path.name + ".tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, eod_path)
    return report


def run_t_trade_monitor(
    *,
    mode: str = "scheduled",
    is_trading_day: Optional[Callable[[date], bool]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    logger_: Optional[logging.Logger] = None,
) -> dict:
    """L2 盘中监控节拍。APScheduler cron 每分钟触发一次。

    函数内做 30s 自循环心跳（09:25~15:00 窗口内持续监控）。
    15:00 整生成日终报告后退出本拍。
    """
    log = logger_ or logging.getLogger("t_trade_monitor")
    now_bj = cn_now(now_fn)
    base = {
        "schema": "t_tracker_monitor_beat/1",
        "mode": mode,
        "beat_at_beijing": now_bj.isoformat(),
    }

    # 窗口守卫
    current = now_bj.timetz().replace(tzinfo=None) if now_bj.tzinfo else now_bj.time()
    if current < TTRADE_MONITOR_WINDOW_START or current > TTRADE_MONITOR_WINDOW_END:
        return {**base, "status": "no_op_window"}

    # 交易日守卫
    check = is_trading_day or _is_trading_day_simple
    try:
        trading = bool(check(now_bj.date()))
    except Exception as exc:
        log.error("[t_trade_monitor] 交易日判断失败：%s", exc)
        return {**base, "status": "failure", "reason": f"trading_day_check_failed: {exc}"}
    if not trading:
        return {**base, "status": "no_op_non_trading"}

    # 主心跳循环
    subloop_count = 0
    signals_fired = 0
    midday_silent_beats = 0
    midday_boundary_notices: list[str] = []
    while True:
        # L2 心跳 now 必须与 L1 t_trade_tracker 一致用 naive 北京时间：
        # closed_bars 的 ts<=now 比较不允许 naive/aware 混用（R50 起 bug）
        now = cn_now(now_fn).replace(tzinfo=None)
        t = now.timetz().replace(tzinfo=None) if now.tzinfo else now.time()

        # 窗口退出
        if t < TTRADE_MONITOR_WINDOW_START or t > TTRADE_MONITOR_WINDOW_END:
            log.debug("[t_trade_monitor] 窗口退出（%s）", t.strftime("%H:%M:%S") if hasattr(t, 'strftime') else str(t))
            break

        # 15:00 整日终报告
        if t.hour == 15 and t.minute == 0:
            if t.second <= 30:
                eod = _run_l2_eod_report(now)
                log.info("[t_trade_monitor] 日终报告: l2_drafts=%d", eod.get("l2_drafts_total", 0))

        # R56 午休闸（外层）：静默/边界/窗外跳过心跳（零信号零弹窗零行情拉取）；
        # 边界窗每「日期+窗口」至多一次可观察提示；闸门自身异常同样 fail-closed
        # 跳拍（不静默扩大 15:00 后旧外窗守卫——窗口退出检查仍在闸前）。
        try:
            gate = l2_midday_gate(now, notified=_L2_MIDDAY_NOTIFIED,
                                  is_trading_day=check)
        except Exception as exc:  # noqa: BLE001
            log.error("[t_trade_monitor] 午休闸异常（fail-closed 跳拍）：%s", exc)
            gate = {"status": "gate_error", "phase": None,
                    "run_heartbeat": False, "notice": False}
        if not gate.get("run_heartbeat"):
            gate_status = str(gate.get("status") or "")
            if gate_status == "midday_silent":
                midday_silent_beats += 1
            elif gate.get("notice"):
                window = "am" if gate_status == "boundary_am" else "pm"
                midday_boundary_notices.append(window)
                log.info("[t_trade_monitor] 午休边界提示（%s 窗，不发交易信号）：%s",
                         window, now.strftime("%H:%M:%S"))
            subloop_count += 1
            # 自循环 30s 间隔（与心跳同节拍，保持循环计数/退出语义不变）
            time.sleep(TTRADE_MONITOR_HEARTBEAT_SEC)
            # 电池保护：最多 60 次循环（≈30min）后退出，让 APScheduler 重触发
            if subloop_count >= 60:
                break
            continue

        # 心跳
        try:
            report = _run_l2_heartbeat(now, log)
        except Exception as exc:
            log.error("[t_trade_monitor] 心跳异常: %s", exc, exc_info=True)
            report = {"signals": []}
        new_signals = len(report.get("signals", []))
        if new_signals:
            signals_fired += new_signals
            log.info("[t_trade_monitor] 信号触发: %s",
                     ", ".join(s["code"] for s in report["signals"]))

        subloop_count += 1
        # 自循环 30s 间隔
        time.sleep(TTRADE_MONITOR_HEARTBEAT_SEC)

        # 电池保护：最多 60 次循环（≈30min）后退出，让 APScheduler 重触发
        if subloop_count >= 60:
            break

    return {
        **base,
        "status": "ok",
        "subloop_count": subloop_count,
        "signals_fired": signals_fired,
        # R56：午休闸可观察汇总（静默拍数 + 边界提示窗序列）
        "midday": {"silent_beats": midday_silent_beats,
                   "boundary_notices": midday_boundary_notices},
    }


def register_t_trade_monitor_job(scheduler):
    """向 scheduler 注册 L2 盘中监控 cron 任务。"""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_t_trade_monitor,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=TTRADE_MONITOR_CRON_HOURS,
            minute=TTRADE_MONITOR_CRON_MINUTE,
            timezone=BEIJING_TZ,
        ),
        id=TTRADE_MONITOR_JOB_ID,
        name="L2做T·盘中监控（R50-L2，工作日 09:25~15:00 每 30s 心跳，自循环）",
        max_instances=1,
        misfire_grace_time=120,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler.get_job(TTRADE_MONITOR_JOB_ID)


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


def _write_postclose_runner_heartbeat() -> None:
    """S11 心跳落盘：供独立只读 postclose-watcher 检测 runner 存活。"""
    try:
        from services.postclose_publication import PublicationStore

        PublicationStore().write_heartbeat(runner_id="aistock-pipeline-runner")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[heartbeat] 写入 postclose_control/heartbeat.json 失败: %s", exc)


def register_postclose_heartbeat_job(scheduler):
    """注册 S11 盘后调度器存活心跳（每 120s 原子刷新 heartbeat.json）。"""
    from apscheduler.triggers.interval import IntervalTrigger

    _write_postclose_runner_heartbeat()
    scheduler.add_job(
        _write_postclose_runner_heartbeat,
        trigger=IntervalTrigger(seconds=120, timezone=BEIJING_TZ),
        id="postclose_runner_heartbeat",
        name="S11 调度器存活心跳（heartbeat.json）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler.get_job("postclose_runner_heartbeat")


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
    register_intraday_job(scheduler)
    register_t_tracker_jobs(scheduler)
    register_signal_quality_job(scheduler)
    # QMT 已全线退役：停止在 serve() 中注册 holding_tick_qmt_alert
    register_weekly_data_audit_job(scheduler)
    register_market_mood_finalize_job(scheduler)
    register_t_trade_monitor_job(scheduler)
    _register_intraday_kline_upsert(scheduler)
    register_postclose_heartbeat_job(scheduler)

    if args.once:
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

        def _shutdown_after_first(_event):
            logger.info("--once：单次执行完毕，调度器退出")
            _request_shutdown(scheduler, logger)

        scheduler.add_listener(_shutdown_after_first, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    _install_signal_handlers(scheduler, logger)
    job = scheduler.get_job(JOB_ID)
    next_run = getattr(job, "next_run_time", None) if job else None
    intraday_job = scheduler.get_job(INTRADAY_JOB_ID)
    intraday_next = getattr(intraday_job, "next_run_time", None) if intraday_job else None
    logger.info(
        "调度注册：job_id=%s cron=[day_of_week=%s %02d:%02d %s] next_run=%s "
        "once=%s version=%s pid=%d",
        JOB_ID, CRON_DAY_OF_WEEK, CRON_HOUR, CRON_MINUTE, "Asia/Shanghai",
        next_run.isoformat() if next_run else "pending", args.once,
        __version__, os.getpid(),
    )
    logger.info(
        "调度注册：job_id=%s cron=[day_of_week=%s %s 每%02dmin %s] 窗口=%s~%s next_run=%s",
        INTRADAY_JOB_ID, CRON_DAY_OF_WEEK, INTRADAY_CRON_HOURS,
        INTRADAY_CRON_MINUTE_STEP, "Asia/Shanghai",
        INTRADAY_WINDOW_START.strftime("%H:%M"), INTRADAY_WINDOW_END.strftime("%H:%M"),
        intraday_next.isoformat() if intraday_next else "pending",
    )
    logger.info(
        "调度注册：R37 L1做T regime=%s@%s beat=%s(min %s,%s 时) reconcile=%s@%s "
        "broker_sync=%s@%s shadow_finalize=%s@%s（R40 observe_only）",
        TTRACKER_REGIME_JOB_ID, TTRACKER_REGIME_CRON_TIME.strftime("%H:%M"),
        TTRACKER_BEAT_JOB_ID, TTRACKER_BEAT_CRON_MINUTE, TTRACKER_BEAT_CRON_HOURS,
        TTRACKER_RECONCILE_JOB_ID, TTRACKER_RECONCILE_CRON_TIME.strftime("%H:%M"),
        TTRACKER_BROKER_SYNC_JOB_ID, TTRACKER_BROKER_SYNC_CRON_TIME.strftime("%H:%M"),
        TTRACKER_SHADOW_FINALIZE_JOB_ID, TTRACKER_SHADOW_FINALIZE_CRON_TIME.strftime("%H:%M"),
    )
    quality_job = scheduler.get_job(SIGNAL_QUALITY_JOB_ID)
    quality_next = getattr(quality_job, "next_run_time", None) if quality_job else None
    logger.info(
        "调度注册：%s cron=[day_of_week=%s %s %s] next_run=%s（做T信号质量日报：drafts×kline_daily→runtime/signal_quality）",
        SIGNAL_QUALITY_JOB_ID, CRON_DAY_OF_WEEK,
        SIGNAL_QUALITY_CRON_TIME.strftime("%H:%M"), "Asia/Shanghai",
        quality_next.isoformat() if quality_next else "pending",
    )
    audit_job = scheduler.get_job(WEEKLY_DATA_AUDIT_JOB_ID)
    audit_next = getattr(audit_job, "next_run_time", None) if audit_job else None
    logger.info(
        "调度注册：%s cron=[day_of_week=%s %s %s] next_run=%s（每周30日数据交付只读审计）",
        WEEKLY_DATA_AUDIT_JOB_ID, WEEKLY_DATA_AUDIT_DAY_OF_WEEK,
        WEEKLY_DATA_AUDIT_CRON_TIME.strftime("%H:%M"), "Asia/Shanghai",
        audit_next.isoformat() if audit_next else "pending",
    )
    mood_job = scheduler.get_job(MARKET_MOOD_FINALIZE_JOB_ID)
    mood_next = getattr(mood_job, "next_run_time", None) if mood_job else None
    logger.info(
        "调度注册：R55 %s cron=[day_of_week=%s %s %s] 窗口=%s~%s（涨停生态终算）next_run=%s",
        MARKET_MOOD_FINALIZE_JOB_ID, CRON_DAY_OF_WEEK,
        MARKET_MOOD_FINALIZE_CRON_TIME.strftime("%H:%M"), "Asia/Shanghai",
        MARKET_MOOD_FINALIZE_WINDOW_START.strftime("%H:%M"),
        MARKET_MOOD_FINALIZE_WINDOW_END.strftime("%H:%M"),
        mood_next.isoformat() if mood_next else "pending",
    )
    # 每日盘后 16:45 增量同步通达信标准板块主档、成分股与近 10 日板块指数日线（兜底，主管道完成后也会自动联动）
    def _run_tdx_sector_sync_job() -> None:
        now_bj = cn_now()
        try:
            if not _default_is_trading_day(now_bj.date()):
                return
        except Exception as exc:
            logger.warning("[tdx_sector_sync] 交易日判断失败: %s", exc)
            return
        _run_tdx_sector_sync_once(logger)

    from apscheduler.triggers.cron import CronTrigger as _TdxSectorCronTrigger
    scheduler.add_job(
        _run_tdx_sector_sync_job,
        trigger=_TdxSectorCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=16, minute=45, timezone=BEIJING_TZ),
        id="tdx_sector_sync",
        name="通达信标准板块主档/成分股/日线盘后增量同步（工作日 16:45）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("调度注册：tdx_sector_sync cron=[day_of_week=%s 16:45 Asia/Shanghai]（通达信标准板块盘后同步）", CRON_DAY_OF_WEEK)

    # 每日盘后 17:15 容器级 24/7 自动喂料 AR4.6：46只全天候及行业ETF + 上交所国家队份额 + 南向资金 + 沪深300 ERP + 明日调仓预案
    def _run_ar45_postclose_sync_job() -> None:
        now_bj = cn_now()
        try:
            if not _default_is_trading_day(now_bj.date()):
                return
        except Exception as exc:
            logger.warning("[ar45_postclose_sync] 交易日判断失败（fail-closed跳过）: %s", exc)
            return
        _run_ar46_postclose_sync_once(logger)

    from apscheduler.triggers.cron import CronTrigger as _Ar45SyncCronTrigger
    scheduler.add_job(
        _run_ar45_postclose_sync_job,
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=17, minute=15, timezone=BEIJING_TZ),
        id="ar45_postclose_sync",
        name="AR4.6 46只ETF+国家队份额+南向资金+ERP盘后24/7自动喂料（工作日 17:15）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("调度注册：ar45_postclose_sync cron=[day_of_week=%s 17:15 Asia/Shanghai]（AR4.6 46只ETF+国家队+南向+ERP盘后同步）", CRON_DAY_OF_WEEK)

    # 注册 acct1 AR4.6 专属盘前/盘中/盘后微信智能买卖提醒（对齐 acct2 同款体验，严格 AR4.6 策略驱动）
    def _run_acct1_ar45_advisor_job(mode: str) -> None:
        import urllib.request as _urlreq

        now_bj = datetime.now(BEIJING_TZ)
        today_d = now_bj.date()
        try:
            is_td = _default_is_trading_day(today_d)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[acct1_ar45_%s] 交易日校验异常（fail-closed 跳过）: %s", mode, exc)
            return
        if not is_td:
            logger.info("[acct1_ar45_%s] %s 非交易日，静默跳过", mode, today_d)
            return

        base = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
        opener = _urlreq.build_opener(_urlreq.ProxyHandler({}))
        url = f"{base}/api/g3/acct1-advisor/run?mode={mode}&dry_run=false"
        try:
            req = _urlreq.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with opener.open(req, timeout=90) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            logger.info(
                "[acct1_ar45_%s] 巡检完成: status=%s pushed=%s regime=%s",
                mode, res.get("status"), res.get("pushed_count", len(res.get("pushed") or [])), res.get("regime"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[acct1_ar45_%s] 调用失败: %s", mode, exc)

    scheduler.add_job(
        lambda: _run_acct1_ar45_advisor_job("premarket"),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=9, minute=15, timezone=BEIJING_TZ),
        id="acct1_ar45_premarket",
        name="acct1 AR4.6 盘前作战计划推送（工作日 09:15）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_acct1_ar45_advisor_job("intraday"),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour="9-11,13-14", minute="5,15,25,35,45,55", timezone=BEIJING_TZ),
        id="acct1_ar45_intraday",
        name="acct1 AR4.6 盘中实时买卖提醒（工作日 09:35~14:50 每10m）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_acct1_ar45_advisor_job("eod"),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=15, minute=15, timezone=BEIJING_TZ),
        id="acct1_ar45_eod",
        name="acct1 AR4.6 日终真钱复盘推送（工作日 15:15）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("调度注册：acct1_ar45_advisor (premarket=09:15 / intraday=09:35~14:50每10m / eod=15:15 Asia/Shanghai)")

    # 注册每日 15:08 收盘同花顺双账号自动只读快照同步 + 20:00 晚间快照时效自检（根治快照停在上午）
    def _run_eod_broker_snapshot_sync_job(stage: str) -> None:
        import urllib.request as _urlreq

        now_bj = datetime.now(BEIJING_TZ)
        today_d = now_bj.date()
        try:
            is_td = _default_is_trading_day(today_d)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[broker_sync_%s] 交易日校验异常跳过: %s", stage, exc)
            return
        if not is_td:
            return

        base = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
        opener = _urlreq.build_opener(_urlreq.ProxyHandler({}))
        try:
            if stage == "evening_check":
                with opener.open(_urlreq.Request(f"{base}/api/accounts/overview", method="GET"), timeout=15) as r:
                    ov = json.loads(r.read().decode("utf-8"))
                a1 = next((a for a in (ov.get("accounts") or []) if a.get("account_id") == "acct1"), None)
                gen_at = str((a1 or {}).get("generated_at") or "")
                # 判断快照是否已覆盖今日 15:00 后（UTC 07:00+ 或北京时间 15:00+）
                today_s = today_d.isoformat()
                is_fresh_eod = False
                if gen_at.startswith(today_s):
                    try:
                        dt_snap = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
                        dt_bj = dt_snap.astimezone(BEIJING_TZ) if dt_snap.tzinfo else dt_snap.replace(tzinfo=BEIJING_TZ)
                        is_fresh_eod = dt_bj.hour >= 15
                    except ValueError:
                        pass
                if is_fresh_eod:
                    logger.info("[broker_sync_evening_check] acct1 今日盘后快照已新鲜 (%s)，无需补采", gen_at)
                    return
                logger.warning("[broker_sync_evening_check] acct1 快照仍停留在盘中 (%s)，立即触发补采", gen_at)

            payload = json.dumps({"accounts": ["acct1", "acct2"], "minimize_after": True}).encode("utf-8")
            req = _urlreq.Request(
                f"{base}/api/accounts/sync",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(req, timeout=20) as r:
                res = json.loads(r.read().decode("utf-8"))
            req_id = str(res.get("request_id") or "")
            bridge_now = res.get("bridge") if isinstance(res.get("bridge"), dict) else {}
            logger.info("[broker_sync_%s] 已触发双账号只读同步: status=%s req=%s", stage, res.get("status"), req_id)

            # 轮询最多 90 秒等待宿主机桌面桥回执；若离线/需人工重登(need_manual_panel_login)/超时，立即推微信告警
            fail_reason = ""
            if not bridge_now.get("online", True):
                fail_reason = "宿主机同花顺桌面桥离线（bridge_offline）"
            elif req_id:
                done_rec = None
                for _ in range(18):
                    time.sleep(5)
                    try:
                        with opener.open(_urlreq.Request(f"{base}/api/accounts/overview", method="GET"), timeout=15) as r2:
                            ov2 = json.loads(r2.read().decode("utf-8"))
                        lc = (ov2.get("bridge") or {}).get("latest_completed") or {}
                        if str(lc.get("request_id") or "") == req_id:
                            done_rec = lc
                            break
                    except Exception:
                        continue
                if done_rec is None:
                    fail_reason = f"同步指令 {req_id} 超过 90 秒未完成（可能同花顺未打开或正处于人工键鼠活跃窗口）"
                else:
                    st_done = str(done_rec.get("status") or "")
                    rc_done = done_rec.get("rc")
                    if st_done == "need_manual_panel_login":
                        fail_reason = "同花顺交易面板已自动超时锁屏/退出登录（need_manual_panel_login），系统严守「凭证零接触」不自动输密码"
                    elif st_done != "ok" or (rc_done is not None and int(rc_done) != 0):
                        fail_reason = f"双账号同步未成功（status={st_done}, rc={rc_done}）"
                    else:
                        logger.info("[broker_sync_%s] 双账号收盘快照已成功对齐 (req=%s)", stage, req_id)

            if fail_reason:
                logger.warning("[broker_sync_%s] 触发人工介入告警: %s", stage, fail_reason)
                try:
                    from services.trading.t_notify import load_notify_config, send_wechat_notification

                    stage_label = "15:08 收盘快照同步" if stage == "close_1508" else "20:00 晚间复盘快照自检"
                    alert_title = f"【⚠️ 同花顺快照同步需要人工处理】{stage_label}"
                    alert_body = "\n".join([
                        f"- 触发节点：`{stage_label}`（`{now_bj.strftime('%Y-%m-%d %H:%M')}`）",
                        f"- 失败原因：{fail_reason}",
                        "- 影响说明：未同步前券商原始快照可能停留在盘中旧时点（`acct1` 聊天核销成交仍会自动叠加兜底）。",
                        "- 处理动作：请重新打开/登录同花顺远航交易面板，然后在网页 `/g3/accounts` 点击「一键同步双账号」即可立即对齐。",
                    ])
                    base_cfg = load_notify_config()
                    wres = send_wechat_notification(alert_title, alert_body, config={**base_cfg, "channel": "ilink_bridge"})
                    mres = send_wechat_notification(alert_title, alert_body, config={**base_cfg, "channel": "qq_email"})
                    logger.info("[broker_sync_%s] 故障双通道告警已发: wechat=%s email=%s", stage, wres.get("ok"), mres.get("ok"))
                except Exception as nexc:  # noqa: BLE001
                    logger.warning("[broker_sync_%s] 发送告警通知失败: %s", stage, nexc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[broker_sync_%s] 触发双账号同步异常: %s", stage, exc)

    scheduler.add_job(
        lambda: _run_eod_broker_snapshot_sync_job("close_1508"),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=15, minute=8, timezone=BEIJING_TZ),
        id="broker_snapshot_sync_1508",
        name="收盘后双账号同花顺自动只读快照同步（工作日 15:08）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_eod_broker_snapshot_sync_job("evening_check"),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=20, minute=0, timezone=BEIJING_TZ),
        id="broker_snapshot_check_2000",
        name="晚间 20:00 复盘前快照时效核查与补采（工作日 20:00）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("调度注册：broker_snapshot_sync (close=15:08 / evening_check=20:00 Asia/Shanghai)")

    # ── G3 早盘 09:40 成交核销与实盘持仓确认简报（收编原 Windows 计划任务） ──
    def _run_g3_postopen_brief_0940_job() -> None:
        try:
            import urllib.request as _urlreq
            import json as _json
            from scripts.g3_postopen_brief_0940 import run_postopen_brief

            api_base = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:8077").rstrip("/")

            def _local_http(path: str, *, method: str = "GET", body=None, timeout: int = 60):
                data = _json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
                headers = {"Content-Type": "application/json; charset=utf-8"} if data is not None else {}
                req = _urlreq.Request(f"{api_base}{path}", data=data, headers=headers, method=method)
                with _urlreq.urlopen(req, timeout=timeout) as resp:
                    return _json.loads(resp.read().decode("utf-8"))

            rc = run_postopen_brief(http_fn=_local_http)
            logger.info("[g3_postopen_brief_0940] 完成: rc=%s", rc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[g3_postopen_brief_0940] 执行异常: %s", exc)

    scheduler.add_job(
        _run_g3_postopen_brief_0940_job,
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=9, minute=40, timezone=BEIJING_TZ),
        id="g3_postopen_brief_0940",
        name="早盘 09:40 持仓同步与实盘确认简报推送（工作日 09:40）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("调度注册：g3_postopen_brief_0940 (cron=09:40 mon-fri Asia/Shanghai)")

    # ── AI Bus 会话在线心跳巡检（交易时段离线立即微信 + QQ邮箱双通道告警） ──
    def _run_ai_bus_heartbeat_watch_job() -> None:
        try:
            now_bj = datetime.now(BEIJING_TZ)
            # 仅工作日 09:05~15:05 交易时段巡检，夜间休市不打扰
            if now_bj.time() < dt_time(9, 5) or now_bj.time() > dt_time(15, 5):
                return
            from scripts.g3_profit_loop_job import _is_trading_day
            ok_td, _ = _is_trading_day(now_bj.strftime("%Y-%m-%d"))
            if not ok_td:
                return
            from services.g3.ai_bus import check_session_and_alert

            res = check_session_and_alert(cooldown_sec=1800.0)
            if not res.get("online"):
                logger.warning("[ai_bus_heartbeat_watch] Antigravity 会话离线: alerted=%s", res.get("alerted"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai_bus_heartbeat_watch] 巡检异常: %s", exc)

    scheduler.add_job(
        _run_ai_bus_heartbeat_watch_job,
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour="9-15", minute="*/5", timezone=BEIJING_TZ),
        id="ai_bus_heartbeat_watch",
        name="AI 参谋会话心跳巡检（离线即时微信+邮箱双告警）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("调度注册：ai_bus_heartbeat_watch (cron=09:05~15:05 每5m mon-fri Asia/Shanghai)")

    # ── AR3/AR4.6 盘中哨兵（收编原 Windows 任务 AiStock_AR3_IntradaySentinel） ──
    def _run_ar3_intraday_sentinel_job() -> None:
        try:
            now_bj = datetime.now(BEIJING_TZ)
            t = now_bj.time()
            if not (dt_time(9, 40) <= t <= dt_time(11, 30) or dt_time(13, 0) <= t <= dt_time(14, 55)):
                return
            from scripts.ar3_intraday_sentinel import run_sentinel

            rc = run_sentinel()
            logger.info("[ar3_intraday_sentinel] 完成: rc=%s", rc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ar3_intraday_sentinel] 执行异常: %s", exc)

    scheduler.add_job(
        _run_ar3_intraday_sentinel_job,
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour="9-14", minute="10,25,40,55", timezone=BEIJING_TZ),
        id="ar3_intraday_sentinel",
        name="AR4.6 盘中哨兵（09:40~14:55 每15m）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("调度注册：ar3_intraday_sentinel (cron=09:40~14:55 每15m mon-fri Asia/Shanghai)")

    # ── Acct2 托管纸面演练引擎（收编原 4 个 Windows 任务 AiStock_Acct2_DryRun_*） ──
    def _run_acct2_dryrun_job(mode: str, window_start: Optional[dt_time] = None, window_end: Optional[dt_time] = None) -> None:
        try:
            now_bj = datetime.now(BEIJING_TZ)
            if window_start is not None and window_end is not None:
                if not (window_start <= now_bj.time() <= window_end):
                    return
            from scripts.acct2_dryrun_runner import run_mode

            rc = run_mode(mode)
            logger.info("[acct2_dryrun_%s] 完成: rc=%s", mode, rc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[acct2_dryrun_%s] 执行异常: %s", mode, exc)

    scheduler.add_job(
        lambda: _run_acct2_dryrun_job("premarket"),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=9, minute=15, timezone=BEIJING_TZ),
        id="acct2_dryrun_premarket",
        name="Acct2 托管演练盘前计划（工作日 09:15）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_acct2_dryrun_job("intraday", dt_time(9, 35), dt_time(11, 30)),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour="9-11", minute="5,15,25,35,45,55", timezone=BEIJING_TZ),
        id="acct2_dryrun_intraday_am",
        name="Acct2 托管演练早盘巡检（09:35~11:30 每10m）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_acct2_dryrun_job("intraday", dt_time(13, 5), dt_time(15, 0)),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour="13-14", minute="5,15,25,35,45,55", timezone=BEIJING_TZ),
        id="acct2_dryrun_intraday_pm",
        name="Acct2 托管演练午盘巡检（13:05~15:00 每10m）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _run_acct2_dryrun_job("eod"),
        trigger=_Ar45SyncCronTrigger(day_of_week=CRON_DAY_OF_WEEK, hour=15, minute=10, timezone=BEIJING_TZ),
        id="acct2_dryrun_eod",
        name="Acct2 托管演练盘后结算（工作日 15:10）",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("调度注册：acct2_dryrun (premarket=09:15 / am=09:35~11:30每10m / pm=13:05~15:00每10m / eod=15:10)")
    try:
        scheduler.start()  # BlockingScheduler：阻塞直至 shutdown
    except (KeyboardInterrupt, SystemExit):
        logger.info("主循环退出，调度器收尾")
    return 0


# ── R57 盘中 5min K 线落盘（2026-11-12） ─────────────────────────

INTRADAY_KLINE_UPSERT_JOB_ID = "intraday_kline_upsert"
INTRADAY_KLINE_UPSERT_CRON_HOURS = "9-14"
INTRADAY_KLINE_UPSERT_CRON_MINUTE = "35-59/5"  # 09:35~14:55 每 5min
INTRADAY_KLINE_UPSERT_WINDOW_START = dt_time(9, 35)
INTRADAY_KLINE_UPSERT_WINDOW_END = dt_time(14, 55)


def _register_intraday_kline_upsert(scheduler):
    """注册 R57 盘中 5min K 线落盘任务（每 5min 聚合写入 CH）。"""
    from apscheduler.triggers.cron import CronTrigger

    def _run_upsert():
        from datetime import datetime
        from services.intraday_phase_gate import guard_intraday_execution

        allowed, reason = guard_intraday_execution(
            datetime.now(BEIJING_TZ),
            trading_day_checker=_default_is_trading_day,
            require_window_start=True,
        )
        if not allowed:
            logger.info("[kline_upsert] 跳过盘中 5min K 落盘（reason=%s）", reason)
            return
        from scripts.intraday_kline_upsert import run_upsert
        logger.info("[kline_upsert] 执行 5min K 落盘")
        result = run_upsert()
        n_ok = sum(1 for r in result.values() if r.get("status") == "ok")
        n_tot = len(result)
        logger.info("[kline_upsert] %d/%d 票写入完成", n_ok, n_tot)

    scheduler.add_job(
        _run_upsert,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=INTRADAY_KLINE_UPSERT_CRON_HOURS,
            minute=INTRADAY_KLINE_UPSERT_CRON_MINUTE,
            timezone=BEIJING_TZ,
        ),
        id=INTRADAY_KLINE_UPSERT_JOB_ID,
        name="R57 盘中5min K线落盘（持仓票，腾讯分钟接口聚合后写入CH）",
        max_instances=1,
        misfire_grace_time=120,
        coalesce=True,
        replace_existing=True,
    )


# ── R59 盘后分钟K线全量写入（TDX lc5，2026-11-12） ─────────────

TDX_POSTCLOSE_JOB_ID = "tdx_postclose_minutes"
TDX_POSTCLOSE_CRON_HOUR = 16
TDX_POSTCLOSE_CRON_MINUTE = 15


def _register_tdx_postclose_minutes(scheduler):
    """R59 已退役（Owner 2026-11-19 P1 裁决④）：盘后分钟K线写入链停止注册，本入口无条件 fail-closed。"""
    raise RuntimeError("R59 盘后分钟K线写入已于2026-11-19按Owner P1 裁决④退役，禁止注册或调用本入口")


def main(argv: Optional[Sequence[str]] = None) -> int:
    return serve(argv)


if __name__ == "__main__":
    raise SystemExit(main())
