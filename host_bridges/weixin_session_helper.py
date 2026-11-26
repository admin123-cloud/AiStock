# -*- coding: utf-8 -*-
"""Antigravity 当前会话双通道统一监听/回复助手（微信收件箱 + AiStock AI Bus，零外部依赖）。

用法：
1. 双通道阻塞等待（同时监听微信 inbox 与 AiStock ai_bus，自动刷在线心跳，有任一任务即打印上下文并退出唤醒会话）：
   python weixin_session_helper.py wait [--interval 1.0] [--timeout 0]
2. 回复微信提问并推回微信、归档：
   python weixin_session_helper.py reply --text "解答内容" [--id <msg_id>]
   python weixin_session_helper.py reply --text-file reply.txt [--id <msg_id>]
3. 回复 AiStock AI Bus 任务并写回结构化决策结果、归档：
   python weixin_session_helper.py respond-job --id <job_id> --answer "深度分析" [--summary "一句话结论"] [--decision advisory]
4. 查看双通道当前 pending 队列与最近历史：
   python weixin_session_helper.py list [--history 10]
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_STATE_DIR = Path(os.getenv("AISTOCK_WEIXIN_DIR", r"F:\Aistock\runtime\weixin_ilink"))
DEFAULT_AI_BUS_DIR = Path(os.getenv("AISTOCK_AI_BUS_DIR", r"F:\Aistock\runtime\g3_ai_coach\ai_bus"))
DEFAULT_BRIDGE_URL = os.getenv("AISTOCK_WEIXIN_BRIDGE_URL", "http://127.0.0.1:8789").rstrip("/")
DEFAULT_API_BASE = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:8077").rstrip("/")


def _fetch_live_strategy_constitution(api_base: str = DEFAULT_API_BASE) -> dict[str, Any]:
    """每次唤醒时从在线后端实时拉取现役策略宪法快照，防止长会话上下文压缩导致策略漂移。"""
    grounding_rule = (
        "【SSOT 铁律】严禁凭历史对话压缩记忆回答现役策略版本或参数；"
        "一切策略/持仓/交易结论必须严格以本 live_strategy_constitution 在线运行态及源码/H:\\Agent-Memory-System\\projects\\aistock 为准。"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = api_base.rstrip("/")
    try:
        gate = json.loads(opener.open(f"{base}/api/g3/market-gate", timeout=5.0).read().decode("utf-8"))
        radar = json.loads(opener.open(f"{base}/api/g3/mining/ar3/radar", timeout=8.0).read().decode("utf-8"))
        etf = radar.get("etf_radar") or {}
        targets = [
            {
                "code": t.get("code"),
                "name": t.get("name"),
                "weight_pct": t.get("account_weight_pct"),
                "close": t.get("close"),
                "above_ma20": t.get("above_ma20"),
            }
            for t in (etf.get("targets") or [])
        ]
        return {
            "available": True,
            "fetched_at": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "strategy_version": radar.get("version") or etf.get("etf_core_label"),
            "etf_core_label": etf.get("etf_core_label"),
            "ref_date": gate.get("ref_date") or etf.get("latest_date"),
            "regime": gate.get("regime") or radar.get("regime"),
            "regime_label": gate.get("regime_label"),
            "allow_new_mainwave": bool(gate.get("allow_new_mainwave")),
            "is_offensive": bool(radar.get("is_offensive")),
            "is_panic_ab_active": bool(radar.get("is_panic_ab_active")),
            "panic_triggered": bool(etf.get("panic_triggered")),
            "stock_budget_active_pct": etf.get("stock_budget_active_pct"),
            "etf_total_budget_pct": etf.get("etf_total_budget_pct"),
            "fallback_mode": etf.get("fallback_mode"),
            "cash_defense_pct": etf.get("cash_defense_pct"),
            "etf_targets": targets,
            "etf_headline": etf.get("headline"),
            "gate_action_summary": gate.get("action_summary"),
            "grounding_rule": grounding_rule,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "fetched_at": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "error": f"{type(exc).__name__}: {exc}",
            "grounding_rule": grounding_rule,
        }


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _touch_ai_bus_heartbeat(ai_bus_dir: Path) -> None:
    now_bj = datetime.now(BJ_TZ)
    payload = {
        "agent": "antigravity_unified_session",
        "heartbeat_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "heartbeat_ts": time.time(),
    }
    _atomic_write_json(ai_bus_dir / "session_heartbeat.json", payload)


def _load_pending_dir(pending_dir: Path) -> list[dict[str, Any]]:
    if not pending_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for f in sorted(pending_dir.glob("*.json")):
        data = _read_json(f)
        if isinstance(data, dict):
            items.append(data)
    return items


def _load_jsonl_tail(file_path: Path, limit: int) -> list[dict[str, Any]]:
    if not file_path.is_file() or limit <= 0:
        return []
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    result: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            result.append(json.loads(line))
        except ValueError:
            continue
    return result


def cmd_wait(state_dir: Path, ai_bus_dir: Path, interval: float, timeout: float, history_limit: int) -> int:
    for sub in ("pending", "results", "done"):
        (ai_bus_dir / sub).mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    last_hb = 0.0
    while True:
        now_m = time.monotonic()
        if (now_m - last_hb) >= 10.0:
            try:
                _touch_ai_bus_heartbeat(ai_bus_dir)
            except Exception:  # noqa: BLE001
                pass
            last_hb = now_m

        wx_pending = _load_pending_dir(state_dir / "inbox" / "pending")
        bus_pending = _load_pending_dir(ai_bus_dir / "pending")
        if wx_pending or bus_pending:
            if wx_pending and bus_pending:
                event = "multi_channel_ready"
            elif bus_pending:
                event = "ai_bus_job_ready"
            else:
                event = "weixin_inbox_ready"
            out: dict[str, Any] = {
                "event": event,
                "live_strategy_constitution": _fetch_live_strategy_constitution(),
                "pending_count": len(wx_pending),
                "pending": wx_pending,
                "ai_bus_pending_count": len(bus_pending),
                "ai_bus_pending": bus_pending,
                "recent_history": _load_jsonl_tail(state_dir / "chat_history.jsonl", history_limit),
                "ai_bus_recent_history": _load_jsonl_tail(ai_bus_dir / "history.jsonl", history_limit),
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        if timeout > 0 and (now_m - start) >= timeout:
            print(json.dumps({"event": "timeout", "pending_count": 0, "ai_bus_pending_count": 0}, ensure_ascii=False))
            return 2
        time.sleep(max(0.2, interval))


def cmd_list(state_dir: Path, ai_bus_dir: Path, history_limit: int) -> int:
    out = {
        "weixin_pending": _load_pending_dir(state_dir / "inbox" / "pending"),
        "ai_bus_pending": _load_pending_dir(ai_bus_dir / "pending"),
        "recent_history": _load_jsonl_tail(state_dir / "chat_history.jsonl", history_limit),
        "ai_bus_recent_history": _load_jsonl_tail(ai_bus_dir / "history.jsonl", history_limit),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_reply(bridge_url: str, text: str, msg_id: str | None) -> int:
    payload: dict[str, Any] = {"text": text}
    if msg_id:
        payload["id"] = msg_id
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{bridge_url}/reply",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20.0) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    print(raw)
    data = json.loads(raw) if raw.strip() else {}
    return 0 if data.get("ok") else 1


def cmd_respond_job(
    ai_bus_dir: Path,
    job_id: str | None,
    answer: str,
    summary: str,
    decision: str,
    risks: list[str],
    actions: list[str],
) -> int:
    pending_dir = ai_bus_dir / "pending"
    results_dir = ai_bus_dir / "results"
    done_dir = ai_bus_dir / "done"
    for d in (pending_dir, results_dir, done_dir):
        d.mkdir(parents=True, exist_ok=True)

    target_file: Path | None = None
    if job_id:
        cand = pending_dir / f"{job_id}.json"
        if cand.is_file():
            target_file = cand
    else:
        files = sorted(pending_dir.glob("*.json"))
        if files:
            target_file = files[0]
            job_id = target_file.stem

    if not job_id:
        print(json.dumps({"ok": False, "detail": "未指定 job_id 且 pending 为空"}, ensure_ascii=False))
        return 1

    req_doc = _read_json(target_file) if target_file else {}
    if not isinstance(req_doc, dict):
        req_doc = {}

    now_bj = datetime.now(BJ_TZ)
    completed_at = now_bj.strftime("%Y-%m-%d %H:%M:%S")
    res_doc = {
        "job_id": job_id,
        "kind": req_doc.get("kind") or "desk_qa",
        "source": req_doc.get("source") or "desk_ui",
        "prompt": req_doc.get("prompt") or "",
        "status": "completed",
        "decision": (decision or "advisory").strip(),
        "summary": (summary or "").strip(),
        "answer": (answer or "").strip(),
        "risk_flags": [r for r in risks if r.strip()],
        "actions": [a for a in actions if a.strip()],
        "created_at": req_doc.get("created_at") or "",
        "completed_at": completed_at,
        "completed_ts": time.time(),
    }
    _atomic_write_json(results_dir / f"{job_id}.json", res_doc)

    if target_file and target_file.is_file():
        done_doc = dict(req_doc)
        done_doc["status"] = "completed"
        done_doc["completed_at"] = completed_at
        _atomic_write_json(done_dir / target_file.name, done_doc)
        try:
            target_file.unlink()
        except FileNotFoundError:
            pass

    hist_line = json.dumps(
        {
            "ts": completed_at,
            "job_id": job_id,
            "role": "response",
            "kind": res_doc["kind"],
            "decision": res_doc["decision"],
            "summary": res_doc["summary"] or res_doc["answer"][:120],
            "answer": res_doc["answer"],
        },
        ensure_ascii=False,
    ) + "\n"
    with (ai_bus_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(hist_line)

    _touch_ai_bus_heartbeat(ai_bus_dir)
    print(json.dumps({"ok": True, "job_id": job_id, "archived": bool(target_file), "completed_at": completed_at}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Antigravity unified WeChat + AI Bus session helper")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--ai-bus-dir", default=str(DEFAULT_AI_BUS_DIR))
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_wait = sub.add_parser("wait")
    p_wait.add_argument("--interval", type=float, default=1.0)
    p_wait.add_argument("--timeout", type=float, default=0.0)
    p_wait.add_argument("--history", type=int, default=6)

    p_list = sub.add_parser("list")
    p_list.add_argument("--history", type=int, default=10)

    p_reply = sub.add_parser("reply")
    p_reply.add_argument("--id", default=None)
    p_reply.add_argument("--text", default="")
    p_reply.add_argument("--text-file", default=None)

    p_resp = sub.add_parser("respond-job")
    p_resp.add_argument("--id", default=None)
    p_resp.add_argument("--answer", default="")
    p_resp.add_argument("--answer-file", default=None)
    p_resp.add_argument("--summary", default="")
    p_resp.add_argument("--decision", default="advisory")
    p_resp.add_argument("--risk", action="append", default=[])
    p_resp.add_argument("--action", action="append", default=[])

    args = parser.parse_args()
    state_dir = Path(args.state_dir)
    ai_bus_dir = Path(args.ai_bus_dir)

    if args.cmd == "wait":
        return cmd_wait(state_dir, ai_bus_dir, args.interval, args.timeout, args.history)
    if args.cmd == "list":
        return cmd_list(state_dir, ai_bus_dir, args.history)
    if args.cmd == "reply":
        text = args.text
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8")
        elif not text and not sys.stdin.isatty():
            text = sys.stdin.read()
        return cmd_reply(args.bridge_url, text, args.id)
    if args.cmd == "respond-job":
        ans = args.answer
        if args.answer_file:
            ans = Path(args.answer_file).read_text(encoding="utf-8")
        elif not ans and not sys.stdin.isatty():
            ans = sys.stdin.read()
        return cmd_respond_job(ai_bus_dir, args.id, ans, args.summary, args.decision, args.risk, args.action)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
