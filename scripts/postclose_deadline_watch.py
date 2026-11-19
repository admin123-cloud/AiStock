# -*- coding: utf-8 -*-
"""S11：独立只读 Deadline 观察者（不导入任何采集/清理/恢复写手模块）。

职责：
- 仅读取获批控制面目录 ``postclose_control`` 下的 ``state.json`` 与 ``heartbeat.json``；
- 检测：控制目录缺失/错配、runner 心跳缺失或过期、15:05 收口超时、
  16:15 清理超时、16:40 发布认证超时，以及 ``UNKNOWN_WRITES / RECOVERY_REQUIRED /
  DEADLINE_MISSED / SOURCE_FAILED`` 风险态；
- 将脱敏结构化事件写入 ``AlertOutbox``，绝不启动采集或修复任务。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.postclose_alerts import (  # noqa: E402
    AlertOutbox,
    SmtpAlertMailer,
    load_sanitized_smtp_config,
)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

CLOSE_DEADLINE = time(15, 5)
CLEAN_DEADLINE = time(16, 15)
PUBLISH_DEADLINE = time(16, 40)

POST_CLOSE_OK_STATUSES = frozenset({
    "INTRADAY_CLOSED",
    "PREPARED",
    "BACKED_UP",
    "CLEANING",
    "CLEANED",
    "IMPORTING",
    "VERIFYING",
    "CERTIFIED",
    "NO_OP_CLOSED",
})

POST_CLEAN_OK_STATUSES = frozenset({
    "CLEANED",
    "IMPORTING",
    "VERIFYING",
    "CERTIFIED",
    "NO_OP_CLOSED",
})

IMMEDIATE_CRITICAL_STATUSES = frozenset({
    "UNKNOWN_WRITES",
    "RECOVERY_REQUIRED",
    "DEADLINE_MISSED",
    "SOURCE_FAILED",
})


def _to_bj(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(BEIJING_TZ)
    if now.tzinfo is None or now.utcoffset() is None:
        return now.replace(tzinfo=BEIJING_TZ)
    return now.astimezone(BEIJING_TZ)


def inspect_postclose_deadlines(
    *,
    control_root: Path,
    outbox: AlertOutbox,
    now: Optional[datetime] = None,
    is_trading_day: bool = True,
    max_heartbeat_age_sec: float = 300.0,
    allow_uninitialized_state: bool = False,
) -> list[dict[str, Any]]:
    """纯只读巡检：检查控制面证据并把触发的风险事件入队 ``AlertOutbox``。"""
    now_bj = _to_bj(now)
    today_iso = now_bj.date().isoformat()
    wall = now_bj.timetz().replace(tzinfo=None)
    emitted: list[dict[str, Any]] = []

    def _emit(**fields: Any) -> None:
        res = outbox.enqueue(fields, now=now_bj)
        emitted.append({"event_id": res.event_id, "reason_code": fields["reason_code"], "severity": fields["severity"]})

    control_dir = Path(control_root)
    state_path = control_dir / "state.json"
    hb_path = control_dir / "heartbeat.json"

    # 1) 控制目录缺失或路径错配
    if not control_dir.is_dir():
        _emit(
            target_date=today_iso,
            plan_id="watcher",
            generation=0,
            phase="watch_control",
            reason_code="control_root_missing",
            severity="critical",
            episode="control_dir",
            read_availability="not_ready",
            evidence_id="watcher:control_root_missing",
        )
        return emitted

    # 2) Runner 心跳核验
    if not hb_path.is_file():
        _emit(
            target_date=today_iso,
            plan_id="watcher",
            generation=0,
            phase="watch_heartbeat",
            reason_code="runner_heartbeat_missing",
            severity="critical",
            episode="heartbeat",
            read_availability="unknown",
            evidence_id="watcher:heartbeat_missing",
        )
    else:
        try:
            hb_data = json.loads(hb_path.read_text(encoding="utf-8"))
            hb_at = _to_bj(datetime.fromisoformat(str(hb_data["heartbeat_at"])))
            age = (now_bj - hb_at).total_seconds()
            if age > max_heartbeat_age_sec:
                _emit(
                    target_date=today_iso,
                    plan_id="watcher",
                    generation=0,
                    phase="watch_heartbeat",
                    reason_code="runner_heartbeat_stale",
                    severity="critical",
                    episode="heartbeat",
                    scope_summary=f"age_sec={int(age)}",
                    evidence_id="watcher:heartbeat_stale",
                )
        except Exception:
            _emit(
                target_date=today_iso,
                plan_id="watcher",
                generation=0,
                phase="watch_heartbeat",
                reason_code="runner_heartbeat_corrupt",
                severity="critical",
                episode="heartbeat",
                evidence_id="watcher:heartbeat_corrupt",
            )

    if not is_trading_day:
        return emitted

    # 3) state.json 读取与旧日/缺失核验
    if not state_path.is_file():
        if wall >= CLOSE_DEADLINE and not allow_uninitialized_state:
            _emit(
                target_date=today_iso,
                plan_id="watcher",
                generation=0,
                phase="watch_state",
                reason_code="control_state_missing_after_1505",
                severity="critical",
                episode=today_iso,
                read_availability="not_ready",
                evidence_id="watcher:state_missing",
            )
        return emitted

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state not dict")
    except Exception:
        _emit(
            target_date=today_iso,
            plan_id="watcher",
            generation=0,
            phase="watch_state",
            reason_code="control_state_corrupt",
            severity="critical",
            episode=today_iso,
            read_availability="not_ready",
            evidence_id="watcher:state_corrupt",
        )
        return emitted

    state_date = str(state.get("target_date") or "")
    status = str(state.get("status") or "unknown")
    plan_id = str(state.get("plan_id") or "unknown")
    generation = int(state.get("generation") or 0)
    last_cert = str(state.get("last_certified_date") or "")

    # 4) 处于非当日旧文件且已过 15:05
    if state_date != today_iso:
        if wall >= CLOSE_DEADLINE:
            _emit(
                target_date=today_iso,
                plan_id=plan_id,
                generation=generation,
                phase="watch_deadline",
                reason_code="stale_control_state_previous_day",
                severity="critical",
                episode=today_iso,
                last_certified_date=last_cert,
                read_availability="not_ready",
                evidence_id=f"watcher:stale_date:{state_date}",
            )
        return emitted

    # 5) 立即高危态
    if status in IMMEDIATE_CRITICAL_STATUSES:
        _emit(
            target_date=today_iso,
            plan_id=plan_id,
            generation=generation,
            phase="watch_state",
            reason_code=f"postclose_state_{status.lower()}",
            severity="critical",
            episode=f"{today_iso}:{status}",
            write_attempted=status in ("UNKNOWN_WRITES", "RECOVERY_REQUIRED"),
            writes_known=status != "UNKNOWN_WRITES",
            last_certified_date=last_cert,
            read_availability="not_ready",
            evidence_id=f"state:{plan_id}:g{generation}",
        )

    # 6) 15:05 / 16:15 / 16:40 节点检查
    if wall >= CLOSE_DEADLINE and status not in POST_CLOSE_OK_STATUSES:
        _emit(
            target_date=today_iso,
            plan_id=plan_id,
            generation=generation,
            phase="deadline_1505",
            reason_code="close_deadline_1505_missed",
            severity="critical",
            episode=today_iso,
            last_certified_date=last_cert,
            read_availability="not_ready",
            evidence_id=f"deadline_1505:{status}",
        )
    if wall >= CLEAN_DEADLINE and status not in POST_CLEAN_OK_STATUSES:
        _emit(
            target_date=today_iso,
            plan_id=plan_id,
            generation=generation,
            phase="deadline_1615",
            reason_code="clean_deadline_1615_missed",
            severity="critical",
            episode=today_iso,
            last_certified_date=last_cert,
            read_availability="not_ready",
            evidence_id=f"deadline_1615:{status}",
        )
    if wall >= PUBLISH_DEADLINE and status not in ("CERTIFIED", "NO_OP_CLOSED"):
        _emit(
            target_date=today_iso,
            plan_id=plan_id,
            generation=generation,
            phase="deadline_1640",
            reason_code="publish_deadline_1640_missed",
            severity="critical",
            episode=today_iso,
            last_certified_date=last_cert,
            read_availability="not_ready",
            evidence_id=f"deadline_1640:{status}",
        )
    return emitted


def _run_once(
    *,
    control_root: Path,
    outbox: AlertOutbox,
    is_trading_day: bool,
    send_email: bool,
    config_path: Optional[Path],
    allow_uninitialized_state: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    events = inspect_postclose_deadlines(
        control_root=control_root,
        outbox=outbox,
        is_trading_day=is_trading_day,
        allow_uninitialized_state=allow_uninitialized_state,
    )
    mail_outcomes: dict[str, str] = {}
    smtp_summary: dict[str, Any] = {}
    if send_email:
        cfg, smtp_summary = load_sanitized_smtp_config(config_path)
        if cfg is not None:
            mail_outcomes = outbox.dispatch_pending(SmtpAlertMailer(cfg))
    return events, mail_outcomes, smtp_summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    import time as _time

    parser = argparse.ArgumentParser(description="AiStock S11 独立只读盘后 Deadline 观察器")
    parser.add_argument("--control-root", required=True, help="postclose_control 只读控制面目录")
    parser.add_argument("--outbox-dir", required=True, help="告警 Outbox 持久化目录")
    parser.add_argument("--config-path", default="", help="可选 YAML 配置文件路径（仅读 email 段，绝不打印凭据）")
    parser.add_argument("--send-email", action="store_true", help="对 pending 脱敏告警执行 SMTP 投递")
    parser.add_argument("--test-email", action="store_true", help="发送 1 封脱敏 S11 测试邮件并打印脱敏回执后退出")
    parser.add_argument("--loop-seconds", type=int, default=0, help="常驻轮询间隔秒数（0=单次退出；sidecar 建议 120）")
    parser.add_argument(
        "--allow-uninitialized-state",
        action="store_true",
        help="S8 尚未切入实盘前的过渡观察模式：state.json 尚未创建时不报 15:05 缺失，但仍严格监控心跳与已存在 state.json 违约",
    )
    parser.add_argument("--non-trading-day", action="store_true", help="显式标注非交易日")
    args = parser.parse_args(argv)

    outbox = AlertOutbox(Path(args.outbox_dir))
    cfg_path = Path(args.config_path) if str(args.config_path or "").strip() else None

    if args.test_email:
        cfg, summary = load_sanitized_smtp_config(cfg_path)
        if cfg is None:
            print(json.dumps({"status": "blocked", "reason": "smtp_not_configured_or_disabled", "smtp": summary}, ensure_ascii=False))
            return 2
        now_bj = datetime.now(BEIJING_TZ)
        probe_outbox = AlertOutbox(Path(args.outbox_dir) / "test_probes")
        enq = probe_outbox.enqueue(
            {
                "target_date": now_bj.date().isoformat(),
                "plan_id": "s11-smtp-probe",
                "generation": 0,
                "phase": "watch_smtp_probe",
                "reason_code": "s11_watcher_smtp_probe",
                "severity": "info",
                "episode": now_bj.strftime("%Y%m%d_%H%M%S"),
                "scope_summary": "S11 独立只读观察器脱敏连通性测试（无业务写入/无账户信息）",
                "read_availability": "read_only_probe",
                "evidence_id": f"smtp_probe:{now_bj.strftime('%Y%m%d_%H%M%S')}",
            },
            now=now_bj,
        )
        outcomes = probe_outbox.dispatch_pending(SmtpAlertMailer(cfg))
        delivery = outcomes.get(enq.event_id, "unknown")
        print(
            json.dumps(
                {
                    "status": "ok" if delivery == "smtp_accepted" else "error",
                    "event_id": enq.event_id,
                    "delivery_status": delivery,
                    "smtp": summary,
                },
                ensure_ascii=False,
            )
        )
        return 0 if delivery == "smtp_accepted" else 1

    while True:
        events, mail_outcomes, smtp_summary = _run_once(
            control_root=Path(args.control_root),
            outbox=outbox,
            is_trading_day=not args.non_trading_day,
            send_email=bool(args.send_email),
            config_path=cfg_path,
            allow_uninitialized_state=bool(args.allow_uninitialized_state),
        )
        print(
            json.dumps(
                {
                    "emitted": events,
                    "count": len(events),
                    "mail_outcomes": mail_outcomes,
                    "smtp": smtp_summary,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if int(args.loop_seconds or 0) <= 0:
            return 0 if not events else 2
        _time.sleep(max(10, int(args.loop_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
