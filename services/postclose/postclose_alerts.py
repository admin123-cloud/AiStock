# -*- coding: utf-8 -*-
"""S11：脱敏结构化风险事件与持久化告警 Outbox（零秘密泄漏、去重且允许升级）。

契约要点：
1. 字段白名单（``ALLOWED_ALERT_FIELDS``）：严禁写入账户资产、凭据、原始异常栈、
   命令行环境变量等非白名单字段；对全部字符串值执行敏感模式清洗。
2. 持久化 Outbox（``<outbox_dir>/outbox.json``，原子替换，重启保留）。
3. 去重与升级：按 ``(target_date, plan_id, phase, reason_code, episode)`` 去重；
   同键严重级别升级（如 ``warning -> critical``）必须立即重新入队告警。
4. 通知状态正交分离：``pending`` / ``smtp_accepted`` / ``uncertain`` / ``failed`` /
   ``delivery_confirmed``；发送超时记为 ``uncertain``，绝不谎称送达，也不触发业务重跑。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

ALLOWED_ALERT_FIELDS: frozenset[str] = frozenset({
    "event_id",
    "target_date",
    "plan_id",
    "generation",
    "phase",
    "reason_code",
    "severity",
    "episode",
    "write_attempted",
    "writes_known",
    "scope_summary",
    "last_certified_date",
    "read_availability",
    "backup_status",
    "owner_role",
    "evidence_id",
    "created_at",
})

SEVERITY_RANK: dict[str, int] = {
    "info": 0,
    "warning": 1,
    "critical": 2,
}

DELIVERY_STATES: frozenset[str] = frozenset({
    "pending",
    "smtp_accepted",
    "uncertain",
    "failed",
    "delivery_confirmed",
})

_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization|bearer|cookie|session[_-]?id|account[_-]?(?:no|id|balance|cash)|broker[_-]?key)\b"
    r"(\s*[:=]\s*\S+|\s+\S+)*"
)


class AlertMailer(Protocol):
    """脱敏邮件投递适配器（测试用 FakeMailer，不直连真实 SMTP）。"""

    def send(self, payload: Mapping[str, Any]) -> None:
        ...


def _scrub_text(value: Any, *, max_len: int = 160) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # 剥离多行异常栈，只保留单行摘要并脱敏敏感键
    single_line = " ".join(text.splitlines()).strip()
    scrubbed = _SECRET_PATTERN.sub("[REDACTED]", single_line)
    return scrubbed[:max_len]


def sanitize_alert_event(raw: Mapping[str, Any], *, now: Optional[datetime] = None) -> dict[str, Any]:
    """按严格白名单构造脱敏告警载荷，丢弃所有非白名单字段并清洗敏感词。"""
    stamp = (now or datetime.now(BEIJING_TZ))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=BEIJING_TZ)
    else:
        stamp = stamp.astimezone(BEIJING_TZ)

    severity = str(raw.get("severity") or "warning").strip().lower()
    if severity not in SEVERITY_RANK:
        severity = "warning"

    target_date = _scrub_text(raw.get("target_date"), max_len=16)
    plan_id = _scrub_text(raw.get("plan_id"), max_len=64)
    phase = _scrub_text(raw.get("phase"), max_len=48)
    reason_code = _scrub_text(raw.get("reason_code"), max_len=96)
    episode = _scrub_text(raw.get("episode") or "default", max_len=48)

    dedup_raw = f"{target_date}|{plan_id}|{phase}|{reason_code}|{episode}"
    event_id = hashlib.sha256(dedup_raw.encode("utf-8")).hexdigest()[:24]

    cleaned = {
        "event_id": event_id,
        "target_date": target_date,
        "plan_id": plan_id,
        "generation": int(raw.get("generation") or 0),
        "phase": phase,
        "reason_code": reason_code,
        "severity": severity,
        "episode": episode,
        "write_attempted": bool(raw.get("write_attempted", False)),
        "writes_known": bool(raw.get("writes_known", True)),
        "scope_summary": _scrub_text(raw.get("scope_summary"), max_len=120),
        "last_certified_date": _scrub_text(raw.get("last_certified_date"), max_len=16),
        "read_availability": _scrub_text(raw.get("read_availability") or "unknown", max_len=32),
        "backup_status": _scrub_text(raw.get("backup_status") or "unknown", max_len=32),
        "owner_role": _scrub_text(raw.get("owner_role") or "oncall_owner", max_len=48),
        "evidence_id": _scrub_text(raw.get("evidence_id"), max_len=80),
        "created_at": stamp.isoformat(),
    }
    assert set(cleaned) == ALLOWED_ALERT_FIELDS
    return cleaned


@dataclass(frozen=True)
class EnqueueResult:
    event_id: str
    deduplicated: bool
    escalated: bool
    delivery_status: str


class AlertOutbox:
    """磁盘持久化去重告警 Outbox（重启安全，状态与业务正交）。"""

    def __init__(self, outbox_dir: Path):
        self.outbox_dir = Path(outbox_dir)
        self.outbox_path = self.outbox_dir / "outbox.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.outbox_path.is_file():
            return {}
        data = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("corrupt outbox.json")
        return data

    def _save(self, records: Mapping[str, dict[str, Any]]) -> None:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.outbox_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.outbox_path)

    def enqueue(self, raw_event: Mapping[str, Any], *, now: Optional[datetime] = None) -> EnqueueResult:
        """入队脱敏事件：同键同/低级别去重；严重级别升级立即重新置为 pending。"""
        payload = sanitize_alert_event(raw_event, now=now)
        eid = payload["event_id"]
        records = self._load()
        existing = records.get(eid)

        if existing is not None:
            old_rank = SEVERITY_RANK.get(str(existing["payload"].get("severity", "info")), 0)
            new_rank = SEVERITY_RANK.get(payload["severity"], 0)
            if new_rank <= old_rank:
                return EnqueueResult(
                    event_id=eid,
                    deduplicated=True,
                    escalated=False,
                    delivery_status=str(existing.get("delivery_status", "pending")),
                )
            records[eid] = {
                "payload": payload,
                "delivery_status": "pending",
                "attempts": int(existing.get("attempts", 0)),
                "escalated_from": existing["payload"].get("severity"),
            }
            self._save(records)
            return EnqueueResult(
                event_id=eid,
                deduplicated=False,
                escalated=True,
                delivery_status="pending",
            )

        records[eid] = {
            "payload": payload,
            "delivery_status": "pending",
            "attempts": 0,
            "escalated_from": None,
        }
        self._save(records)
        return EnqueueResult(
            event_id=eid,
            deduplicated=False,
            escalated=False,
            delivery_status="pending",
        )

    def list_records(self) -> dict[str, dict[str, Any]]:
        return self._load()

    def dispatch_pending(self, mailer: AlertMailer) -> dict[str, str]:
        """投递所有 pending 事件：超时标 uncertain，错误标 failed，接受标 smtp_accepted。"""
        records = self._load()
        outcomes: dict[str, str] = {}
        changed = False
        for eid, rec in records.items():
            if rec.get("delivery_status") != "pending":
                continue
            changed = True
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            try:
                mailer.send(dict(rec["payload"]))
                rec["delivery_status"] = "smtp_accepted"
            except TimeoutError:
                rec["delivery_status"] = "uncertain"
            except Exception:
                rec["delivery_status"] = "failed"
            outcomes[eid] = rec["delivery_status"]
        if changed:
            self._save(records)
        return outcomes

    def confirm_delivery(self, event_id: str) -> bool:
        records = self._load()
        rec = records.get(event_id)
        if rec is None:
            return False
        rec["delivery_status"] = "delivery_confirmed"
        self._save(records)
        return True


def _mask_email(addr: str) -> str:
    text = str(addr or "").strip()
    if "@" not in text:
        return "***"
    local, domain = text.split("@", 1)
    prefix = local[:2] if len(local) > 2 else local[:1]
    return f"{prefix}***@{domain}"


def load_sanitized_smtp_config(
    config_path: Optional[Path] = None,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """加载 SMTP 配置并同步生成脱敏摘要（绝不输出密码或明文凭据）。"""
    raw_email: dict[str, Any] = {}
    if config_path is not None and Path(config_path).is_file():
        import yaml

        loaded = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict) and isinstance(loaded.get("email"), dict):
            raw_email = dict(loaded["email"])
    else:
        try:
            from utils.config import config

            cfg_obj = config.get("email") if hasattr(config, "get") else {}
            if isinstance(cfg_obj, dict):
                raw_email = dict(cfg_obj)
        except Exception:
            raw_email = {}

    enabled = bool(raw_email.get("enabled", False))
    server = str(raw_email.get("smtp_server") or "").strip()
    port = int(raw_email.get("smtp_port") or 465)
    username = str(raw_email.get("smtp_user") or "").strip()
    password = str(raw_email.get("smtp_password") or "").strip()
    from_email = str(raw_email.get("from_email") or username).strip()
    to_emails = [str(x).strip() for x in (raw_email.get("to_emails") or []) if str(x).strip()]
    use_ssl = bool(raw_email.get("use_ssl", port == 465))
    configured = bool(server and port > 0 and username and password and from_email and to_emails)

    summary = {
        "enabled": enabled,
        "configured": configured,
        "smtp_server": server,
        "smtp_port": port,
        "use_ssl": use_ssl,
        "recipient_count": len(to_emails),
        "recipients_masked": [_mask_email(a) for a in to_emails],
    }
    if not (enabled and configured):
        return None, summary

    transport_cfg = {
        "server": server,
        "port": port,
        "username": username,
        "password": password,
        "from_email": from_email,
        "to_emails": to_emails,
        "use_ssl": use_ssl,
    }
    return transport_cfg, summary


class SmtpAlertMailer:
    """S11 脱敏 SMTP 邮件发送器（复用 utils.email_transport，绝不泄露秘密）。"""

    def __init__(self, transport_cfg: Mapping[str, Any], *, sender_fn=None):
        self._cfg = dict(transport_cfg)
        self._sender_fn = sender_fn

    def send(self, payload: Mapping[str, Any]) -> None:
        import socket

        safe = sanitize_alert_event(payload)
        subject = (
            f"[AiStock][{safe['severity'].upper()}][{safe['phase']}] "
            f"{safe['target_date']} {safe['reason_code']}"
        )
        body_lines = [
            "AiStock 盘后治理脱敏告警（严格白名单字段，无账户/凭据信息）：",
            "",
            json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2),
        ]
        body = "\n".join(body_lines)
        send_fn = self._sender_fn
        if send_fn is None:
            from utils.email_transport import send_plain_email

            send_fn = send_plain_email
        try:
            send_fn(self._cfg, subject, body)
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError("smtp_timeout") from exc
        except RuntimeError as exc:
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                raise TimeoutError("smtp_timeout") from exc
            raise
