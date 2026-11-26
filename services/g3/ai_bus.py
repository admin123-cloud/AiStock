# -*- coding: utf-8 -*-
"""AiStock <-> Antigravity 本地文件异步 AI 决策总线（AI Bus）。

共享目录：runtime_path("g3_ai_coach") / "ai_bus"
（宿主机 F:/Aistock/runtime/g3_ai_coach/ai_bus，容器内 /aistock/data/runtime/g3_ai_coach/ai_bus，
aistock-backend / aistock-pipeline-runner / 宿主机 Antigravity 会话三方天然实时共享）。

安全与稳健铁律：
1. AI 仅输出深度研判、排雷意见与决策建议，绝不直接触发实盘下单或绕过 AR4.6 硬风控；
2. 具备会话在线心跳检测与超时自动降级（Fail-Safe）：会话离线或超时绝不阻塞交易主流程。
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
HEARTBEAT_TTL_SEC = 35.0


def _bus_root() -> Path:
    env_dir = os.getenv("AISTOCK_AI_BUS_DIR", "").strip()
    root = Path(env_dir) if env_dir else (runtime_path("g3_ai_coach") / "ai_bus")
    for sub in ("pending", "results", "done"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None


def touch_session_heartbeat(agent_label: str = "antigravity_session") -> dict[str, Any]:
    """供宿主机统一监听器定期刷新在线心跳（在线时自动复位离线告警冷却）。"""
    root = _bus_root()
    now_bj = datetime.now(BJ_TZ)
    payload = {
        "agent": agent_label,
        "heartbeat_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "heartbeat_ts": time.time(),
    }
    _atomic_write_json(root / "session_heartbeat.json", payload)
    # 恢复在线后重置离线告警状态，保证下次再次掉线时第一时间告警
    alert_file = root / "alert_state.json"
    st = _read_json(alert_file)
    if isinstance(st, dict) and st.get("offline_alerted"):
        st["offline_alerted"] = False
        st["last_offline_ts"] = 0.0
        st["recovered_at"] = payload["heartbeat_at"]
        _atomic_write_json(alert_file, st)
    return payload


def notify_ai_session_alert(
    *,
    reason: str,
    detail: str,
    job_id: Optional[str] = None,
    cooldown_sec: float = 1800.0,
    notify_fn: Any = None,
) -> dict[str, Any]:
    """当 Antigravity 会话离线或超时降级时，立即推送微信 + QQ 邮箱双通道告警（带冷却防刷屏）。"""
    root = _bus_root()
    alert_file = root / "alert_state.json"
    st = _read_json(alert_file)
    if not isinstance(st, dict):
        st = {}

    now_ts = time.time()
    last_ts = float(st.get(f"last_{reason}_ts") or 0.0)
    if (now_ts - last_ts) < cooldown_sec:
        return {"ok": True, "skipped": True, "reason": "cooldown"}

    now_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    _, hb_at = is_session_online()
    title = (
        f"🚨 AiStock AI参谋离线告警（{reason}）"
        if reason == "offline"
        else f"⚠️ AiStock AI参谋超时降级告警（{reason}）"
    )
    body = "\n".join([
        f"- 告警时间：{now_str}",
        f"- 触发原因：{reason}",
        f"- 详细说明：{detail}",
        f"- 最近会话心跳：{hb_at or '无记录'}",
        f"- 关联任务：{job_id or '定时心跳巡检'}",
        "- 系统状态：AiStock 已触发 Fail-Safe 自动降级，正常量化策略与定时任务不受阻断。",
        "- 处理建议：请打开/恢复 Antigravity 会话并重新挂起 `weixin_session_helper.py wait` 监听。",
    ])

    outcomes: dict[str, Any] = {}
    if notify_fn is not None:
        outcomes["wechat"] = notify_fn("ilink_bridge", title, body)
        outcomes["qq_email"] = notify_fn("qq_email", title, body)
    else:
        try:
            from services.trading.t_notify import load_notify_config, send_wechat_notification

            base_cfg = load_notify_config()
            outcomes["wechat"] = send_wechat_notification(title, body, config={**base_cfg, "channel": "ilink_bridge"})
            outcomes["qq_email"] = send_wechat_notification(title, body, config={**base_cfg, "channel": "qq_email"})
        except Exception as exc:  # noqa: BLE001
            outcomes["error"] = f"{type(exc).__name__}: {exc}"

    st[f"last_{reason}_ts"] = now_ts
    st[f"last_{reason}_at"] = now_str
    if reason == "offline":
        st["offline_alerted"] = True
    _atomic_write_json(alert_file, st)
    return {"ok": True, "skipped": False, "outcomes": outcomes}


def check_session_and_alert(*, cooldown_sec: float = 1800.0, notify_fn: Any = None) -> dict[str, Any]:
    """供调度器定时巡检调用：发现会话离线立即触发微信 + 邮箱双告警。"""
    online, hb_at = is_session_online()
    if online:
        return {"online": True, "heartbeat_at": hb_at, "alerted": False}
    res = notify_ai_session_alert(
        reason="offline",
        detail="交易时段巡检检测到 Antigravity 在线监听心跳已中断（超过 35 秒无心跳）。",
        cooldown_sec=cooldown_sec,
        notify_fn=notify_fn,
    )
    return {"online": False, "heartbeat_at": hb_at, "alerted": not bool(res.get("skipped")), "alert_result": res}


def is_session_online(ttl_sec: float = HEARTBEAT_TTL_SEC) -> tuple[bool, Optional[str]]:
    root = _bus_root()
    hb = _read_json(root / "session_heartbeat.json")
    if not isinstance(hb, dict):
        return False, None
    ts = float(hb.get("heartbeat_ts") or 0.0)
    online = (time.time() - ts) <= ttl_sec
    return online, str(hb.get("heartbeat_at") or "") or None


def _append_history(entry: Mapping[str, Any]) -> None:
    root = _bus_root()
    hist = root / "history.jsonl"
    line = json.dumps(dict(entry), ensure_ascii=False) + "\n"
    with hist.open("a", encoding="utf-8") as fh:
        fh.write(line)


def submit_ai_job(
    *,
    kind: str,
    prompt: str,
    context: Optional[Mapping[str, Any]] = None,
    source: str = "desk_ui",
    wait_timeout_sec: float = 0.0,
    poll_interval_sec: float = 1.0,
    require_online_when_waiting: bool = True,
) -> dict[str, Any]:
    """提交一个 AI 思考任务到 pending/；若指定 wait_timeout_sec > 0 则同步等待结果（超时自动降级）。"""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return {"ok": False, "status": "invalid", "detail": "提问内容不能为空"}

    online, hb_at = is_session_online()
    if wait_timeout_sec > 0 and require_online_when_waiting and not online:
        alert_res = notify_ai_session_alert(
            reason="offline",
            detail=f"任务({kind}/{source})需要在线 AI 研判，但 Antigravity 会话未在线，已自动降级。",
        )
        return {
            "ok": False,
            "status": "offline_fallback",
            "session_online": False,
            "heartbeat_at": hb_at,
            "alert": alert_res,
            "detail": "当前 Antigravity 会话未处于在线监听状态，已自动降级并触发微信+邮箱告警",
        }

    root = _bus_root()
    now_bj = datetime.now(BJ_TZ)
    job_id = f"ai_{now_bj.strftime('%Y%m%d_%H%M%S_%f')}_{secrets.token_hex(3)}"
    job_payload: dict[str, Any] = {
        "job_id": job_id,
        "kind": str(kind or "desk_qa").strip(),
        "source": str(source or "desk_ui").strip(),
        "prompt": clean_prompt,
        "context": dict(context) if isinstance(context, Mapping) else {},
        "created_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "created_ts": time.time(),
        "status": "pending",
    }
    _atomic_write_json(root / "pending" / f"{job_id}.json", job_payload)
    _append_history({
        "ts": job_payload["created_at"],
        "job_id": job_id,
        "role": "request",
        "kind": job_payload["kind"],
        "source": job_payload["source"],
        "prompt": clean_prompt,
    })

    if wait_timeout_sec <= 0:
        alert_res = None
        if not online:
            alert_res = notify_ai_session_alert(
                reason="offline",
                detail=f"异步任务({kind}/{source})已入队，但 Antigravity 会话当前离线，无法立即响应。",
                job_id=job_id,
            )
        return {
            "ok": True,
            "status": "pending",
            "job_id": job_id,
            "session_online": online,
            "heartbeat_at": hb_at,
            "alert": alert_res,
            "created_at": job_payload["created_at"],
        }

    deadline = time.monotonic() + float(wait_timeout_sec)
    res_path = root / "results" / f"{job_id}.json"
    while time.monotonic() < deadline:
        res = _read_json(res_path)
        if isinstance(res, dict) and res.get("status") == "completed":
            return {
                "ok": True,
                "status": "completed",
                "job_id": job_id,
                "session_online": True,
                "result": res,
            }
        time.sleep(max(0.25, poll_interval_sec))

    alert_res = notify_ai_session_alert(
        reason="timeout",
        detail=f"任务({kind}/{source})等待 Antigravity 思考超过 {wait_timeout_sec:.0f} 秒未返回，已自动降级。",
        job_id=job_id,
    )
    return {
        "ok": False,
        "status": "timeout_fallback",
        "job_id": job_id,
        "session_online": online,
        "alert": alert_res,
        "detail": f"等待 AI 会话思考超过 {wait_timeout_sec:.0f} 秒，已自动降级并触发微信+邮箱告警",
    }


def write_ai_result(
    *,
    job_id: str,
    answer: str,
    summary: str = "",
    decision: str = "advisory",
    risk_flags: Optional[list[str]] = None,
    actions: Optional[list[str]] = None,
) -> dict[str, Any]:
    """写回 AI 会话思考结果，并将对应 pending 请求归档至 done/。"""
    root = _bus_root()
    now_bj = datetime.now(BJ_TZ)
    pending_file = root / "pending" / f"{job_id}.json"
    req_doc = _read_json(pending_file) or {}

    result_payload: dict[str, Any] = {
        "job_id": job_id,
        "kind": req_doc.get("kind") or "desk_qa",
        "source": req_doc.get("source") or "desk_ui",
        "prompt": req_doc.get("prompt") or "",
        "status": "completed",
        "decision": str(decision or "advisory").strip(),
        "summary": (summary or "").strip(),
        "answer": (answer or "").strip(),
        "risk_flags": list(risk_flags or []),
        "actions": list(actions or []),
        "created_at": req_doc.get("created_at") or "",
        "completed_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "completed_ts": time.time(),
    }
    _atomic_write_json(root / "results" / f"{job_id}.json", result_payload)

    if pending_file.is_file():
        done_doc = dict(req_doc)
        done_doc["status"] = "completed"
        done_doc["completed_at"] = result_payload["completed_at"]
        _atomic_write_json(root / "done" / pending_file.name, done_doc)
        try:
            pending_file.unlink()
        except FileNotFoundError:
            pass

    _append_history({
        "ts": result_payload["completed_at"],
        "job_id": job_id,
        "role": "response",
        "kind": result_payload["kind"],
        "decision": result_payload["decision"],
        "summary": result_payload["summary"] or result_payload["answer"][:120],
        "answer": result_payload["answer"],
    })
    return result_payload


def get_ai_job(job_id: str) -> dict[str, Any]:
    root = _bus_root()
    res = _read_json(root / "results" / f"{job_id}.json")
    if isinstance(res, dict):
        return {"ok": True, "status": "completed", "job_id": job_id, "result": res}
    pend = _read_json(root / "pending" / f"{job_id}.json")
    if isinstance(pend, dict):
        online, hb_at = is_session_online()
        return {
            "ok": True,
            "status": "pending",
            "job_id": job_id,
            "request": pend,
            "session_online": online,
            "heartbeat_at": hb_at,
        }
    return {"ok": False, "status": "not_found", "job_id": job_id}


def get_bus_status(limit: int = 12) -> dict[str, Any]:
    root = _bus_root()
    online, hb_at = is_session_online()
    pending_items = []
    for f in sorted((root / "pending").glob("*.json")):
        doc = _read_json(f)
        if isinstance(doc, dict):
            pending_items.append(doc)
    recent_results = []
    for f in sorted((root / "results").glob("*.json"), reverse=True)[:limit]:
        doc = _read_json(f)
        if isinstance(doc, dict):
            recent_results.append(doc)
    return {
        "ok": True,
        "session_online": online,
        "heartbeat_at": hb_at,
        "pending_count": len(pending_items),
        "pending": pending_items,
        "recent_results": recent_results,
    }
