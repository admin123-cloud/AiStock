"""实时任务中心只读聚合服务（取代 9 月废弃的 Operations Publisher 静态快照）。

数据全部直采自：
  1. ClickHouse 物理表实时状态（kline_daily / intraday_market_snapshot / kline_minute_5 / sectors / sector_kline_daily）；
  2. runtime/ 真实运行态文件（intraday_snapshot / postclose_auto / postclose_control / market_mood / t_tracker / signal_quality）；
  3. data_center_service.get_health_matrix(days=10) 科学分母交付摘要。
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from services.ops.data_center_service import get_health_matrix
from utils.logger import get_logger
from utils.market_warehouse import clickhouse_available, clickhouse_scalar
from utils.paths import runtime_path

logger = get_logger("task_center_service")
BJ_TZ = ZoneInfo("Asia/Shanghai")

_BACKEND_STARTED_AT = datetime.now(BJ_TZ).isoformat(timespec="seconds")
_CACHE: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
_CACHE_TTL_SEC = 15.0


def _now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def _read_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.exists() and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def _latest_file(directory: Path, pattern: str) -> Optional[Path]:
    try:
        if not directory.exists() or not directory.is_dir():
            return None
        files = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None
    except Exception:
        return None


def _fmt_mtime(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _parse_iso_bj(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    raw = str(text).strip().replace("Z", "+00:00")
    for candidate in (raw, raw.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt.astimezone(BJ_TZ) if dt.tzinfo else dt.replace(tzinfo=BJ_TZ)
        except ValueError:
            continue
    return None


def _is_trading_hours(now_bj: datetime) -> bool:
    if now_bj.weekday() >= 5:
        return False
    hm = (now_bj.hour, now_bj.minute)
    return (9, 30) <= hm <= (11, 35) or (13, 0) <= hm <= (15, 10)


def _query_ch_live_stats() -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "available": False,
        "latest_daily_date": None,
        "intraday_latest_ts": None,
        "intraday_today_rounds": 0,
        "intraday_latest_rows": 0,
        "min5_latest_dt": None,
        "sector_count": 0,
        "sector_latest_date": None,
    }
    if not clickhouse_available():
        return stats
    try:
        today_str = _now_bj().strftime("%Y-%m-%d")
        latest_daily = clickhouse_scalar("SELECT max(trade_date) FROM kline_daily", raw=True)
        stats["available"] = True
        stats["latest_daily_date"] = str(latest_daily)[:10] if latest_daily else None

        try:
            ts = clickhouse_scalar("SELECT max(snapshot_ts) FROM intraday_market_snapshot", raw=True)
            stats["intraday_latest_ts"] = str(ts)[:19] if ts else None
            rounds = clickhouse_scalar(
                "SELECT uniqExact(snapshot_ts) FROM intraday_market_snapshot WHERE toDate(snapshot_ts) = toDate(?)",
                [today_str],
                raw=True,
            )
            stats["intraday_today_rounds"] = int(rounds or 0)
            if ts:
                rows = clickhouse_scalar(
                    "SELECT count() FROM intraday_market_snapshot WHERE snapshot_ts = ?",
                    [ts],
                    raw=True,
                )
                stats["intraday_latest_rows"] = int(rows or 0)
        except Exception as exc:
            logger.debug(f"intraday_market_snapshot probe skipped: {exc}")

        try:
            min5_dt = clickhouse_scalar("SELECT max(datetime) FROM kline_minute_5", raw=True)
            stats["min5_latest_dt"] = str(min5_dt)[:19] if min5_dt else None
        except Exception:
            pass

        try:
            sec_cnt = clickhouse_scalar("SELECT count() FROM sectors FINAL", raw=True)
            sec_dt = clickhouse_scalar("SELECT max(trade_date) FROM sector_kline_daily FINAL", raw=True)
            stats["sector_count"] = int(sec_cnt or 0)
            stats["sector_latest_date"] = str(sec_dt)[:10] if sec_dt else None
        except Exception:
            pass
    except Exception as exc:
        logger.warning(f"ClickHouse live stats query failed: {exc}")
    return stats


def get_task_center_board(*, force_refresh: bool = False) -> Dict[str, Any]:
    now_mono = time.monotonic()
    if not force_refresh and _CACHE.get("payload") is not None and now_mono < float(_CACHE.get("expires_at") or 0.0):
        return _CACHE["payload"]

    now_bj = _now_bj()
    today_str = now_bj.strftime("%Y-%m-%d")
    trading_now = _is_trading_hours(now_bj)
    ch = _query_ch_live_stats()

    # 读取真实 runtime 运行态文件
    intraday_state = _read_json_safe(runtime_path("intraday_snapshot", "scheduler_state.json")) or {}
    postclose_hb = _read_json_safe(runtime_path("postclose_control", "heartbeat.json")) or {}
    postclose_state = _read_json_safe(runtime_path("postclose_control", "state.json")) or {}
    latest_postclose_file = _latest_file(runtime_path("postclose_auto"), "postclose_report_*.json")
    postclose_report = _read_json_safe(latest_postclose_file) if latest_postclose_file else None
    latest_mood_file = _latest_file(runtime_path("market_mood"), "20*.json")
    latest_mood_data = _read_json_safe(latest_mood_file) if latest_mood_file else None
    latest_t_file = _latest_file(runtime_path("t_tracker", "shadow"), "20*.json")
    latest_sig_file = _latest_file(runtime_path("signal_quality"), "*.json")

    # ── 1. 核心服务状态 ──
    hb_ts_str = (
        postclose_hb.get("heartbeat_at_beijing")
        or postclose_hb.get("updated_at")
        or postclose_hb.get("ts")
        or _fmt_mtime(runtime_path("postclose_control", "heartbeat.json"))
    )
    hb_dt = _parse_iso_bj(hb_ts_str)
    watcher_age = (now_bj - hb_dt).total_seconds() if hb_dt else None
    watcher_ok = watcher_age is not None and watcher_age <= 300

    intraday_ts_str = ch.get("intraday_latest_ts") or intraday_state.get("last_success_ts")
    intraday_dt = _parse_iso_bj(intraday_ts_str)
    intraday_age = (now_bj - intraday_dt).total_seconds() if intraday_dt else None
    runner_ok = (
        (not intraday_state.get("alarm"))
        and (
            (intraday_age is not None and intraday_age <= 900)
            or (not trading_now and bool(ch.get("latest_daily_date")))
        )
    )

    certified_date = (
        postclose_state.get("last_certified_date")
        or postclose_state.get("target_date")
        or postclose_state.get("data_date")
    )

    services: List[Dict[str, Any]] = [
        {
            "id": "clickhouse",
            "name": "ClickHouse 正式库",
            "status": "ok" if ch["available"] else "error",
            "detail": f"最新正式日线 {ch['latest_daily_date'] or '—'} · 板块 {ch['sector_count']} 个",
            "heartbeat": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        },
        {
            "id": "backend",
            "name": "API 服务 (aistock-backend)",
            "status": "ok",
            "detail": f"PID {os.getpid()} · 只读观察栈在线",
            "heartbeat": _BACKEND_STARTED_AT[:19].replace("T", " "),
        },
        {
            "id": "pipeline_runner",
            "name": "调度引擎 (pipeline-runner)",
            "status": "ok" if runner_ok else "warn",
            "detail": (
                f"今日盘中已采 {ch['intraday_today_rounds']} 轮 · 最近 {intraday_ts_str or '—'}"
                if ch["intraday_today_rounds"] > 0
                else f"非盘中待命 · 最新快照 {intraday_ts_str or '—'}"
            ),
            "heartbeat": intraday_ts_str or "—",
        },
        {
            "id": "postclose_watcher",
            "name": "盘后认证守卫 (postclose-watcher)",
            "status": "ok" if watcher_ok else "warn",
            "detail": (
                f"已认证 {certified_date or '—'} (g{postclose_state.get('generation') or 1})"
                if postclose_state
                else "等待认证状态文件"
            ),
            "heartbeat": hb_ts_str[:19].replace("T", " ") if isinstance(hb_ts_str, str) else "—",
        },
    ]

    # ── 2. 在运定时任务清单 ──
    intraday_alarm = bool(intraday_state.get("alarm"))
    intraday_fails = int(intraday_state.get("consecutive_failures") or 0)
    if intraday_alarm or intraday_fails >= 3:
        intraday_status = "error"
    elif trading_now and (intraday_age is None or intraday_age > 600):
        intraday_status = "warn"
    else:
        intraday_status = "ok"

    # 盘后三阶段汇总
    pc_stages_text = "—"
    pc_status = "ok"
    pc_last_run = ch["latest_daily_date"] or "—"
    pc_duration = None
    if postclose_report:
        pc_last_run = str(postclose_report.get("finished_at_beijing") or postclose_report.get("run_date") or "—")[:19].replace("T", " ")
        pc_duration = round(float(postclose_report.get("elapsed_sec") or 0), 1)
        pc_status = "error" if postclose_report.get("alarm") or postclose_report.get("failed_stage") else "ok"
        stages = postclose_report.get("stages") or {}
        stage_parts = []
        for s_key, s_label in (("daily_kline", "日线"), ("minute_collect", "5m采集"), ("minute_agg", "15/30/60m聚合")):
            st = stages.get(s_key)
            if isinstance(st, dict):
                stage_parts.append(f"{s_label}:{'OK' if st.get('rc') == 0 else 'FAIL'}({round(float(st.get('elapsed_sec') or 0), 1)}s)")
        pc_stages_text = " · ".join(stage_parts) if stage_parts else f"数据日 {postclose_report.get('run_date')}"
    elif ch["latest_daily_date"]:
        pc_stages_text = f"ClickHouse 已入库至 {ch['latest_daily_date']}"

    mood_modes = []
    if isinstance(latest_mood_data, dict):
        if "realtime" in latest_mood_data:
            mood_modes.append("盘中")
        if "final" in latest_mood_data:
            mood_modes.append("收盘终算")

    tasks: List[Dict[str, Any]] = [
        {
            "id": "intraday_snapshot_collect",
            "name": "盘中全市场快照采集",
            "category": "盘中实时",
            "schedule": "工作日 09:35~15:05 · 每 5 分钟",
            "status": intraday_status,
            "last_run": intraday_ts_str or "—",
            "duration_sec": None,
            "summary": (
                f"今日已采 {ch['intraday_today_rounds']} 轮 · 最新帧 {ch['intraday_latest_rows']:,} 只 · 连续失败 {intraday_fails}"
            ),
            "target": "intraday_market_snapshot / intraday_index_snapshot",
        },
        {
            "id": "intraday_kline_upsert",
            "name": "盘中 5 分钟 K 线落盘",
            "category": "盘中实时",
            "schedule": "工作日 09:35~14:55 · 每 5 分钟",
            "status": "ok" if ch["min5_latest_dt"] else "warn",
            "last_run": ch["min5_latest_dt"] or "—",
            "duration_sec": None,
            "summary": f"kline_minute_5 最新时间戳 {ch['min5_latest_dt'] or '—'}",
            "target": "stock.kline_minute_5",
        },
        {
            "id": "postclose_pipelines",
            "name": "盘后正式日线与分钟线管道",
            "category": "盘后数据",
            "schedule": "工作日 16:15",
            "status": pc_status,
            "last_run": pc_last_run,
            "duration_sec": pc_duration,
            "summary": pc_stages_text,
            "target": "kline_daily / kline_minute_5·15·30·60",
        },
        {
            "id": "tdx_sector_sync",
            "name": "通达信标准板块增量同步",
            "category": "盘后数据",
            "schedule": "工作日 16:45",
            "status": "ok" if ch["sector_count"] > 0 and ch["sector_latest_date"] else "warn",
            "last_run": ch["sector_latest_date"] or "—",
            "duration_sec": None,
            "summary": f"已维护 {ch['sector_count']} 个通达信板块 · 最新日线 {ch['sector_latest_date'] or '—'}",
            "target": "sectors / sector_stocks / sector_kline_daily",
        },
        {
            "id": "market_mood_finalize",
            "name": "涨停生态与连板梯队终算",
            "category": "盘后数据",
            "schedule": "盘中随快照 + 工作日 16:40 终算",
            "status": "ok" if latest_mood_file else "warn",
            "last_run": _fmt_mtime(latest_mood_file) or "—",
            "duration_sec": None,
            "summary": f"最新文件 {latest_mood_file.name if latest_mood_file else '—'}" + (f" ({'+'.join(mood_modes)})" if mood_modes else ""),
            "target": "runtime/market_mood",
        },
        {
            "id": "t_trade_monitor",
            "name": "G3 做 T 盘中监控与影子评估",
            "category": "策略监控",
            "schedule": "工作日 09:25 周期 + 09:31~14:59 实时监控",
            "status": "ok" if latest_t_file else "warn",
            "last_run": _fmt_mtime(latest_t_file) or "—",
            "duration_sec": None,
            "summary": f"最新影子记录 {latest_t_file.name if latest_t_file else '—'}",
            "target": "runtime/t_tracker",
        },
        {
            "id": "signal_quality_report",
            "name": "做 T 信号质量日报",
            "category": "策略监控",
            "schedule": "工作日 15:20",
            "status": "ok" if latest_sig_file else "idle",
            "last_run": _fmt_mtime(latest_sig_file) or "—",
            "duration_sec": None,
            "summary": f"最新日报 {latest_sig_file.name if latest_sig_file else '暂无'}",
            "target": "runtime/signal_quality",
        },
        {
            "id": "postclose_watcher_job",
            "name": "盘后数据认证发布守卫",
            "category": "盘后数据",
            "schedule": "常驻心跳 · 自动认证发布",
            "status": "ok" if watcher_ok else "warn",
            "last_run": hb_ts_str[:19].replace("T", " ") if isinstance(hb_ts_str, str) else "—",
            "duration_sec": None,
            "summary": (
                f"认证日 {certified_date or '—'} · 状态 {postclose_state.get('status') or '—'}"
                if postclose_state
                else "等待首次认证"
            ),
            "target": "runtime/postclose_control",
        },
    ]

    # ── 3. 近 10 日核心数据交付摘要（复用新版 data_center_service 科学口径） ──
    delivery_summary: List[Dict[str, Any]] = []
    try:
        mx = get_health_matrix(days=10, force_refresh=force_refresh)
        for ds in mx.get("datasets", []):
            cells = [c for c in ds.get("cells", []) if c.get("status") != "pending"]
            ok_cnt = sum(1 for c in cells if c.get("status") == "ok")
            warn_cnt = sum(1 for c in cells if c.get("status") == "warn")
            miss_cnt = sum(1 for c in cells if c.get("status") == "missing")
            latest_cell = cells[-1] if cells else None
            delivery_summary.append({
                "id": ds.get("id"),
                "label": ds.get("label"),
                "checked_days": len(cells),
                "ok_days": ok_cnt,
                "warn_days": warn_cnt,
                "missing_days": miss_cnt,
                "latest_date": latest_cell.get("date") if latest_cell else None,
                "latest_coverage": latest_cell.get("coverage") if latest_cell else None,
                "status": "error" if miss_cnt > 0 else ("warn" if warn_cnt > 0 else "ok"),
            })
    except Exception as exc:
        logger.warning(f"task_center delivery_summary failed: {exc}")

    ok_services = sum(1 for s in services if s["status"] == "ok")
    ok_tasks = sum(1 for t in tasks if t["status"] in ("ok", "idle"))
    warn_tasks = sum(1 for t in tasks if t["status"] == "warn")
    err_tasks = sum(1 for t in tasks if t["status"] == "error")

    payload: Dict[str, Any] = {
        "schema": "task_center/2",
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "today": today_str,
        "trading_now": trading_now,
        "summary": {
            "services_ok": ok_services,
            "services_total": len(services),
            "tasks_total": len(tasks),
            "tasks_ok": ok_tasks,
            "tasks_warn": warn_tasks,
            "tasks_error": err_tasks,
            "latest_intraday_ts": intraday_ts_str,
            "intraday_today_rounds": ch["intraday_today_rounds"],
            "latest_certified_date": certified_date or ch["latest_daily_date"],
            "latest_daily_date": ch["latest_daily_date"],
        },
        "services": services,
        "tasks": tasks,
        "delivery_summary": delivery_summary,
    }

    _CACHE["expires_at"] = now_mono + _CACHE_TTL_SEC
    _CACHE["payload"] = payload
    return payload
