"""SMTP 传输层单一实现：连接方案、登录探测与发送循环。

从 api/trading.py 抽取（绞杀式第一步），语义逐行保持：
- attempt plans 顺序（ssl→starttls / starttls→plain）
- 登录探测返回 {ok, mode, port, errors}
- open 失败抛 RuntimeError("SMTP send failed: ...")，文案不变
调用方保留各自的配置加载与消息组装，仅复用传输循环。
"""

from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Tuple


def smtp_attempt_plans(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    plans: List[Dict[str, Any]] = []
    port = int(cfg.get("port") or 0)
    use_ssl = bool(cfg.get("use_ssl"))
    if use_ssl:
        plans.append({"mode": "ssl", "port": port})
        if port == 465:
            plans.append({"mode": "starttls", "port": 587})
    else:
        if port == 587:
            plans.append({"mode": "starttls", "port": port})
        plans.append({"mode": "plain", "port": port})
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in plans:
        key = (item.get("mode"), item.get("port"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def smtp_login_probe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    for plan in smtp_attempt_plans(cfg):
        server = None
        try:
            mode = str(plan.get("mode") or "")
            port = int(plan.get("port") or 0)
            if mode == "ssl":
                server = smtplib.SMTP_SSL(cfg["server"], port, timeout=30)
            else:
                server = smtplib.SMTP(cfg["server"], port, timeout=30)
                server.ehlo()
                if mode == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            server.login(cfg["username"], cfg["password"])
            return {"ok": True, "mode": mode, "port": port, "errors": errors}
        except Exception as exc:
            errors.append(f"{plan.get('mode')}:{plan.get('port')} -> {exc}")
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
    return {"ok": False, "mode": "", "port": None, "errors": errors}


def open_smtp_server(cfg: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    errors: List[str] = []
    for plan in smtp_attempt_plans(cfg):
        server = None
        try:
            mode = str(plan.get("mode") or "")
            port = int(plan.get("port") or 0)
            if mode == "ssl":
                server = smtplib.SMTP_SSL(cfg["server"], port, timeout=30)
            else:
                server = smtplib.SMTP(cfg["server"], port, timeout=30)
                server.ehlo()
                if mode == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            server.login(cfg["username"], cfg["password"])
            return server, {"mode": mode, "port": port}
        except Exception as exc:
            errors.append(f"{plan.get('mode')}:{plan.get('port')} -> {exc}")
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
    raise RuntimeError("SMTP send failed: " + " | ".join(errors))


def send_plain_email(cfg: Dict[str, Any], subject: str, body: str) -> None:
    """按既有语义发送纯文本邮件（From/To 取自 cfg）。"""
    msg = MIMEMultipart()
    msg["From"] = cfg["from_email"]
    msg["To"] = ",".join(cfg["to_emails"])
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    server, _send_meta = open_smtp_server(cfg)
    try:
        server.sendmail(cfg["from_email"], cfg["to_emails"], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass
