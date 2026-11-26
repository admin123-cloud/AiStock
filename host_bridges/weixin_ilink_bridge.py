# -*- coding: utf-8 -*-
"""AiStock 专属微信交易助手桥接服务（腾讯 iLink Bot 协议，零外部依赖）。

职责：
1. 独占托管已绑定的 iLink Bot（凭证位于 F:/Aistock/runtime/weixin_ilink/）：
   - 长轮询 ilink/bot/getupdates 接收 Owner 微信指令并自动刷新 context_token；
   - 提供本地 HTTP 接口（默认 0.0.0.0:8789）：
     * GET  /health -> 返回在线状态、账号 ID、最近收发时间
     * POST /send   -> 供 AiStock 后端（Docker 通过 host.docker.internal:8789）单向推送做T提醒
2. 微信快捷交易指令（秒级直连 AiStock http://127.0.0.1:3000/api）：
   - 帮助 / help
   - 规划 / 做T / 计划
   - 持仓 / 敞口 / 资金
   - 任务 / 状态
   - 刷新 / 重算
   - 6位代码或股票名（如 002015 / 协鑫能科）-> 单票详细作战剧本
   - 记卖 <代码> <价格> <股数>  |  记买 <代码> <价格> <股数>
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

BJ_TZ = timezone(timedelta(hours=8))

ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1

DEFAULT_STATE_DIR = Path(os.getenv("AISTOCK_WEIXIN_DIR", r"F:\Aistock\runtime\weixin_ilink"))
DEFAULT_API_BASE = os.getenv("AISTOCK_API_BASE", "http://127.0.0.1:3000/api")
DEFAULT_PORT = int(os.getenv("AISTOCK_WEIXIN_BRIDGE_PORT", "8789"))

LOG_PATH = Path(r"F:\Aistock\storage\logs\weixin_ilink_bridge.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("weixin_ilink_bridge")


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None


def _ilink_headers(token: str, body: str) -> dict[str, str]:
    uin = base64.b64encode(str(int.from_bytes(secrets.token_bytes(4), "big")).encode("utf-8")).decode("ascii")
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "Authorization": f"Bearer {token}",
    }


def _ilink_post(base_url: str, endpoint: str, token: str, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    body = json.dumps(
        {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    url = f"{base_url.rstrip('/')}/{endpoint}"
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=_ilink_headers(token, body),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


_REF_MSG_ID_RE = re.compile(
    r"^\s*[\[#【]?(?:id)(\d{1,6})[\]】]?\s*[:：\s]\s*(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_LEADING_MSG_ID_RE = re.compile(r"^\s*[\[#【]?id\d{4,6}[\]】]?(?:\s|$)", re.IGNORECASE)


def normalize_short_msg_id(raw_num: str | int) -> str:
    """规范化短消息编号为 id0001 形式（不足 4 位左补零）。"""
    num = int(raw_num)
    return f"id{num:04d}"


def parse_ref_msg_id(text: str) -> tuple[Optional[str], str]:
    """解析用户消息前缀中的引用编号（如 'id0001:选2' / 'ID1：同意' / 'id0001 确认'）。"""
    raw = (text or "").strip()
    m = _REF_MSG_ID_RE.match(raw)
    if not m:
        return None, raw
    return normalize_short_msg_id(m.group(1)), m.group(2).strip()


class ILinkBridge:
    """iLink 凭证管理、单向推送与双向长轮询控制器。"""

    def __init__(self, state_dir: Path, api_base: str):
        self.state_dir = state_dir
        self.api_base = api_base.rstrip("/")
        self._lock = threading.Lock()
        self._inbox_lock = threading.Lock()
        self._seen_msg_ids: dict[str, float] = {}
        self.inbox_pending_dir = self.state_dir / "inbox" / "pending"
        self.inbox_done_dir = self.state_dir / "inbox" / "done"
        self.history_path = self.state_dir / "chat_history.jsonl"
        self.msg_seq_path = self.state_dir / "inbox" / "msg_seq.json"
        self.outbound_index_path = self.state_dir / "inbox" / "outbound_index.jsonl"
        self.inbox_pending_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_done_dir.mkdir(parents=True, exist_ok=True)
        self.account_id: str = ""
        self.token: str = ""
        self.base_url: str = "https://ilinkai.weixin.qq.com"
        self.sync_buf: str = ""
        self.context_tokens: dict[str, str] = {}
        self.default_peer: str = ""
        self.last_rx_at: Optional[str] = None
        self.last_tx_at: Optional[str] = None
        self._load_state()

    def _load_state(self) -> None:
        acct_files = [
            f for f in sorted(self.state_dir.glob("*.json"))
            if not f.name.endswith(".context-tokens.json") and not f.name.endswith(".sync.json")
        ]
        if not acct_files:
            raise RuntimeError(f"未在 {self.state_dir} 找到 iLink 账号凭证文件")
        acct_file = acct_files[0]
        self.account_id = acct_file.stem
        acct = _read_json(acct_file) or {}
        self.token = str(acct.get("token") or "").strip()
        self.base_url = str(acct.get("base_url") or self.base_url).strip()
        if not self.token:
            raise RuntimeError(f"账号凭证 {acct_file.name} 缺少 token")

        ct_raw = _read_json(self.state_dir / f"{self.account_id}.context-tokens.json") or {}
        if isinstance(ct_raw, dict):
            for peer, val in ct_raw.items():
                if isinstance(val, str) and val.strip():
                    self.context_tokens[peer] = val.strip()
                elif isinstance(val, dict):
                    tok = str(val.get("context_token") or val.get("token") or "").strip()
                    if tok:
                        self.context_tokens[peer] = tok
        if self.context_tokens:
            self.default_peer = next(iter(self.context_tokens.keys()))
        elif acct.get("user_id"):
            self.default_peer = str(acct["user_id"])

        sy = _read_json(self.state_dir / f"{self.account_id}.sync.json") or {}
        if isinstance(sy, dict):
            self.sync_buf = str(sy.get("get_updates_buf") or "")

        logger.info(
            "iLink 凭证加载完成: account=%s, peer=%s",
            self.account_id,
            self.default_peer[:12] + "..." if self.default_peer else "none",
        )

    def _save_context_tokens(self) -> None:
        _atomic_write_json(self.state_dir / f"{self.account_id}.context-tokens.json", self.context_tokens)

    def _save_sync_buf(self) -> None:
        _atomic_write_json(self.state_dir / f"{self.account_id}.sync.json", {"get_updates_buf": self.sync_buf})

    def _next_short_msg_id(self) -> str:
        """线程安全分配递增短消息编号（id0001, id0002, ...），持久化在 inbox/msg_seq.json。"""
        with self._inbox_lock:
            cur = _read_json(self.msg_seq_path)
            seq = int(cur.get("seq", 0)) if isinstance(cur, dict) else 0
            seq += 1
            _atomic_write_json(self.msg_seq_path, {"seq": seq})
            return normalize_short_msg_id(seq)

    def _record_outbound_index(
        self,
        short_id: str,
        text: str,
        *,
        source: str = "",
        in_reply_to: str = "",
        peer: str = "",
    ) -> None:
        record = {
            "short_id": short_id,
            "ts": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "in_reply_to": in_reply_to,
            "peer": peer,
            "text": text,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._inbox_lock:
            self.outbound_index_path.parent.mkdir(parents=True, exist_ok=True)
            with self.outbound_index_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def lookup_outbound_message(self, short_id: str) -> Optional[dict[str, Any]]:
        """按短消息编号（如 id0001）反查历史发出消息原文。"""
        wanted = (short_id or "").strip().lower()
        if not wanted or not self.outbound_index_path.is_file():
            return None
        with self._inbox_lock:
            lines = self.outbound_index_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for raw_line in reversed(lines):
            try:
                rec = json.loads(raw_line)
            except ValueError:
                continue
            if str(rec.get("short_id") or "").strip().lower() == wanted:
                return rec
        return None

    def send_text(
        self,
        text: str,
        peer: Optional[str] = None,
        *,
        source: str = "",
        in_reply_to: str = "",
        assign_id: bool = True,
    ) -> dict[str, Any]:
        """发送纯文本微信消息（自动附加 [idxxxx] 编号并索引，超过 1800 字自动分段）。"""
        target = (peer or self.default_peer).strip()
        if not target:
            return {"ok": False, "detail": "尚未记录接收微信用户（请先给机器人发任意一条消息）"}
        clean = (text or "").strip()
        if not clean:
            return {"ok": False, "detail": "消息内容不能为空"}

        short_id: Optional[str] = None
        outbound_text = clean
        if assign_id:
            if _LEADING_MSG_ID_RE.match(clean):
                m_lead = re.match(r"^\s*[\[#【]?(id\d{4,6})[\]】]?", clean, re.IGNORECASE)
                short_id = m_lead.group(1).lower() if m_lead else self._next_short_msg_id()
            else:
                short_id = self._next_short_msg_id()
                outbound_text = f"[{short_id}]\n{clean}"

        chunks = [outbound_text[i:i + 1800] for i in range(0, len(outbound_text), 1800)]
        last_resp: dict[str, Any] = {}
        with self._lock:
            ctx_tok = self.context_tokens.get(target, "")

        for chunk in chunks:
            msg_obj: dict[str, Any] = {
                "from_user_id": "",
                "to_user_id": target,
                "client_id": f"aistock_{secrets.token_hex(8)}",
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": chunk}}],
            }
            if ctx_tok:
                msg_obj["context_token"] = ctx_tok
            try:
                resp = _ilink_post(self.base_url, EP_SEND_MESSAGE, self.token, {"msg": msg_obj}, timeout=15.0)
                last_resp = resp
                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)
                if ret not in (0, None) or errcode not in (0, None):
                    # 尝试无 context_token 重试一次（兼容 iLink 会话刷新）
                    if ctx_tok and (ret == -2 or errcode == -2):
                        msg_obj.pop("context_token", None)
                        resp2 = _ilink_post(self.base_url, EP_SEND_MESSAGE, self.token, {"msg": msg_obj}, timeout=15.0)
                        if resp2.get("ret", 0) in (0, None) and resp2.get("errcode", 0) in (0, None):
                            last_resp = resp2
                            continue
                    return {
                        "ok": False,
                        "detail": f"iLink 返回错误 ret={ret} errcode={errcode} errmsg={resp.get('errmsg') or resp.get('msg') or ''}",
                    }
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

        if short_id:
            self._record_outbound_index(
                short_id,
                clean,
                source=source,
                in_reply_to=in_reply_to,
                peer=target,
            )

        self.last_tx_at = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
        res: dict[str, Any] = {"ok": True, "detail": "已送达微信", "peer": target[:10] + "...", "resp": last_resp}
        if short_id:
            res["short_id"] = short_id
        return res

    # ── AiStock 快捷交易指令 ────────────────────────────────────

    def _call_aistock(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 20.0) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _handle_quick_command(self, text: str) -> Optional[str]:
        cmd = text.strip()
        low = cmd.lower()

        if low in ("帮助", "help", "?", "？", "指令"):
            return (
                "【AiStock 专属微信交易助手】\n"
                "1. 规划 / 做T ：查今日大盘定调与各票买卖带/现价/RSI\n"
                "2. 持仓 / 敞口 ：查双账号资金、持仓浮盈与今日T仓敞口\n"
                "3. 任务 / 状态 ：查当前运行中的调度任务\n"
                "4. 刷新 ：立即重算今日做T规划\n"
                "5. 发股票代码或名称（如 002015 / 协鑫能科）：查单票做T剧本\n"
                "6. 记卖 002015 12.20 1000 ：登记卖出T成交\n"
                "7. 记买 002015 11.90 1000 ：登记买入T成交\n"
                "8. 直接发送任意问题/追问 ：转交当前工作区会话在线解答"
            )

        if low in ("任务", "运行任务", "状态", "tasks"):
            res = self._call_aistock("GET", "/operations/tasks")
            tasks = res.get("tasks") or res.get("items") or (res if isinstance(res, list) else [])
            running = [t for t in tasks if str(t.get("status") or "").lower() in ("running", "active", "enabled")]
            lines = [f"⚙️ AiStock 运行任务概览（共 {len(tasks)} 项，活跃 {len(running)} 项）"]
            for t in (running or tasks)[:12]:
                name = t.get("name") or t.get("title") or t.get("task_id") or "—"
                st = t.get("status") or "—"
                sched = t.get("schedule") or t.get("cron") or t.get("last_run_at") or ""
                lines.append(f"• {name} [{st}] {sched}".strip())
            return "\n".join(lines)

        if low in ("规划", "做t", "计划", "plan"):
            d = self._call_aistock("GET", "/g3/desk?refresh_quotes=true")
            md = d.get("market_directive") or {}
            lines = [
                f"📊 今日做T规划（{d.get('trade_date')}）",
                f"大盘定调：{md.get('mode_label', '—')}（站上MA20: {md.get('above_ma20_pct', '—')}%）",
                "────────────",
            ]
            for c in d.get("cards") or []:
                plan = c.get("daily_plan") or {}
                if not plan.get("eligible"):
                    continue
                q = c.get("live_quote") or {}
                eff = c.get("effective_params") or {}
                bb = eff.get("buy_band") or []
                sb = eff.get("sell_band") or []
                lines.append(
                    f"• {c.get('name')}({c.get('code')}) [{plan.get('action_label')}]\n"
                    f"  现价 {q.get('price', '—')} ({q.get('change_pct', '—')}%) | 均价 {q.get('vwap', '—')} | RSI5m {q.get('rsi5m', '—')}\n"
                    f"  买带 {bb[0] if len(bb)==2 else '—'}~{bb[1] if len(bb)==2 else '—'} | 卖带 {sb[0] if len(sb)==2 else '—'}~{sb[1] if len(sb)==2 else '—'} | 止损 {eff.get('stop', '—')}\n"
                    f"  建议单笔 {plan.get('recommended_lot', 0)} 股"
                )
            return "\n".join(lines)

        if low in ("持仓", "敞口", "资金", "status"):
            d = self._call_aistock("GET", "/g3/desk")
            acc = d.get("accounts_summary") or {}
            intra = d.get("intraday_summary") or {}
            lines = [
                "💼 双账号持仓与今日T仓敞口",
                f"总资产：¥{acc.get('total_assets', '—')} | 可用现金：¥{acc.get('total_cash', '—')}",
                f"未平T敞口：{intra.get('open_exposure_shares', 0)} 股 | 已平T盈亏：¥{intra.get('realized_t_pnl', 0)}",
                "────────────",
            ]
            for c in d.get("cards") or []:
                if not c.get("in_holdings"):
                    continue
                q = c.get("live_quote") or {}
                exp = c.get("open_t_exposure") or {}
                exp_txt = f" [{exp.get('status_label')}]" if exp.get("net_shares") else ""
                lines.append(
                    f"• {c.get('name')}({c.get('code')}): {c.get('total_shares')}股 | 现价 {q.get('price', '—')} ({q.get('change_pct', '—')}%){exp_txt}"
                )
            return "\n".join(lines)

        if low in ("刷新", "重算", "更新规划"):
            res = self._call_aistock("POST", "/g3/desk/generate-plan", {}, timeout=30.0)
            return f"✅ 已重新生成今日（{res.get('date')}）做T规划，覆盖 {res.get('codes_count')} 只标的（{res.get('generated_at')}）。回复「规划」查看详情。"

        m = re.match(r"^(记卖|记买)\s+(\d{6})\s+([0-9]+(?:\.[0-9]+)?)\s+(\d+)$", cmd)
        if m:
            act, code, px_s, sh_s = m.groups()
            side = "SELL" if act == "记卖" else "BUY"
            res = self._call_aistock("POST", "/g3/desk/execution", {
                "code": code,
                "side": side,
                "price": float(px_s),
                "shares": int(sh_s),
                "note": "微信指令登记",
            })
            ent = res.get("entry") or {}
            return f"✅ 已登记{act}：{ent.get('code')} {ent.get('shares')}股 @ {ent.get('price')}（{ent.get('ts')}）。回复「敞口」查看最新敞口。"

        # 单票精准查询（输入 6 位代码或股票名称关键字）
        if re.fullmatch(r"\d{6}", cmd) or (2 <= len(cmd) <= 6 and not any(ch.isspace() for ch in cmd)):
            d = self._call_aistock("GET", "/g3/desk?refresh_quotes=true")
            for c in d.get("cards") or []:
                if cmd == str(c.get("code")) or (cmd in str(c.get("name") or "")):
                    q = c.get("live_quote") or {}
                    eff = c.get("effective_params") or {}
                    plan = c.get("daily_plan") or {}
                    bb = eff.get("buy_band") or []
                    sb = eff.get("sell_band") or []
                    lines = [
                        f"📌 {c.get('name')}({c.get('code')}) · {plan.get('action_label', '—')}",
                        f"现价：{q.get('price', '—')} ({q.get('change_pct', '—')}%) | 均价：{q.get('vwap', '—')}",
                        f"RSI5m：{q.get('rsi5m', '—')} | RSI15m：{q.get('rsi15m', '—')}",
                        f"买带：{bb[0] if len(bb)==2 else '—'}~{bb[1] if len(bb)==2 else '—'} | 卖带：{sb[0] if len(sb)==2 else '—'}~{sb[1] if len(sb)==2 else '—'}",
                        f"止损：{eff.get('stop', '—')} | 建议单笔：{plan.get('recommended_lot', 0)} 股",
                    ]
                    for sc in (plan.get("scenarios") or [])[:3]:
                        lines.append(f"• [{sc.get('tag')}] {sc.get('text')}")
                    return "\n".join(lines)

        return None

    def _append_chat_history(self, role: str, msg_id: str, content: str, sender: str = "") -> None:
        record = {
            "ts": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "id": msg_id,
            "role": role,
            "sender": sender,
            "content": content,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._inbox_lock:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def _enqueue_session_question(
        self,
        sender: str,
        text: str,
        *,
        ref_msg_id: Optional[str] = None,
        clean_text: Optional[str] = None,
    ) -> dict[str, Any]:
        now = datetime.now(BJ_TZ)
        msg_id = f"{now.strftime('%Y%m%d_%H%M%S_%f')}_{secrets.token_hex(3)}"
        ref_record = self.lookup_outbound_message(ref_msg_id) if ref_msg_id else None
        payload: dict[str, Any] = {
            "id": msg_id,
            "sender": sender,
            "text": text,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "created_ts": time.time(),
            "status": "pending",
        }
        if ref_msg_id:
            payload["ref_msg_id"] = ref_msg_id
            payload["clean_text"] = (clean_text or text).strip()
            payload["ref_found"] = bool(ref_record)
            if ref_record:
                payload["referenced_message"] = {
                    "short_id": ref_record.get("short_id"),
                    "ts": ref_record.get("ts"),
                    "source": ref_record.get("source"),
                    "text": ref_record.get("text"),
                }
        pending_file = self.inbox_pending_dir / f"{msg_id}.json"
        _atomic_write_json(pending_file, payload)
        self._append_chat_history("user", msg_id, text, sender=sender)
        logger.info("已将微信问题写入会话收件箱: id=%s ref=%s", msg_id, ref_msg_id or "-")
        return payload

    def list_pending_inbox(self, history_limit: int = 10) -> dict[str, Any]:
        pending = []
        for f in sorted(self.inbox_pending_dir.glob("*.json")):
            item = _read_json(f)
            if isinstance(item, dict):
                pending.append(item)
        history: list[dict[str, Any]] = []
        if self.history_path.is_file() and history_limit > 0:
            with self._inbox_lock:
                lines = self.history_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for raw_line in lines[-history_limit:]:
                try:
                    history.append(json.loads(raw_line))
                except ValueError:
                    continue
        return {"ok": True, "pending": pending, "history": history}

    def reply_inbox_question(self, text: str, msg_id: Optional[str] = None, peer: Optional[str] = None) -> dict[str, Any]:
        clean = (text or "").strip()
        if not clean:
            return {"ok": False, "detail": "回复内容不能为空"}

        target_peer = peer
        target_file: Optional[Path] = None
        item: Optional[dict[str, Any]] = None
        with self._inbox_lock:
            if msg_id:
                candidate = self.inbox_pending_dir / f"{msg_id}.json"
                if candidate.is_file():
                    target_file = candidate
                    item = _read_json(candidate)
            else:
                files = sorted(self.inbox_pending_dir.glob("*.json"))
                if files:
                    target_file = files[0]
                    item = _read_json(target_file)
                    msg_id = target_file.stem

        if isinstance(item, dict) and not target_peer:
            target_peer = str(item.get("sender") or "").strip() or None

        send_res = self.send_text(
            clean,
            peer=target_peer,
            source="session_reply",
            in_reply_to=str(msg_id or ""),
            assign_id=True,
        )
        if not send_res.get("ok"):
            return send_res

        replied_id = msg_id or f"reply_{datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')}"
        short_id = send_res.get("short_id")
        hist_content = f"[{short_id}]\n{clean}" if short_id else clean
        self._append_chat_history("assistant", replied_id, hist_content, sender=target_peer or "")

        if target_file and target_file.is_file():
            with self._inbox_lock:
                done_payload = dict(item) if isinstance(item, dict) else {"id": replied_id}
                done_payload["status"] = "done"
                done_payload["replied_at"] = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
                done_payload["reply_text"] = clean
                if short_id:
                    done_payload["reply_short_id"] = short_id
                done_file = self.inbox_done_dir / target_file.name
                _atomic_write_json(done_file, done_payload)
                try:
                    target_file.unlink()
                except FileNotFoundError:
                    pass

        return {**send_res, "replied_id": replied_id, "archived": bool(target_file)}

    def _dispatch_incoming(self, sender: str, text: str) -> None:
        try:
            ref_msg_id, clean_text = parse_ref_msg_id(text)
            if not ref_msg_id:
                quick = self._handle_quick_command(text)
                if quick is not None:
                    self.send_text(quick, peer=sender, source="quick_cmd", assign_id=True)
                    return

            item = self._enqueue_session_question(
                sender,
                text,
                ref_msg_id=ref_msg_id,
                clean_text=clean_text,
            )
            ref_hint = f"（关联 {ref_msg_id}）" if ref_msg_id else ""
            self.send_text(
                f"⏳ 已收到问题{ref_hint}，已写入本地收件箱，正在等待当前工作区会话在线解答…",
                peer=sender,
                source="inbox_ack",
                assign_id=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理微信消息失败")
            self.send_text(f"⚠️ 指令处理出错：{type(exc).__name__}: {exc}", peer=sender, source="error", assign_id=False)

    def poll_forever(self) -> None:
        """长轮询 iLink getupdates 接收微信消息。"""
        logger.info("开始 iLink getupdates 长轮询...")
        while True:
            try:
                resp = _ilink_post(
                    self.base_url,
                    EP_GET_UPDATES,
                    self.token,
                    {"get_updates_buf": self.sync_buf},
                    timeout=40.0,
                )
                ret = resp.get("ret", 0)
                if ret not in (0, None):
                    logger.warning("getupdates 返回异常 ret=%s errmsg=%s", ret, resp.get("errmsg"))
                    time.sleep(5)
                    continue

                new_buf = str(resp.get("get_updates_buf") or "")
                if new_buf and new_buf != self.sync_buf:
                    self.sync_buf = new_buf
                    self._save_sync_buf()

                msgs = resp.get("msgs") or []
                now_ts = time.time()
                # 清理过期去重缓存
                self._seen_msg_ids = {k: v for k, v in self._seen_msg_ids.items() if now_ts - v < 300}

                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    # 忽略机器人自己发出的消息
                    if m.get("message_type") == MSG_TYPE_BOT:
                        continue
                    mid = str(m.get("message_id") or m.get("client_id") or "")
                    if mid and mid in self._seen_msg_ids:
                        continue
                    if mid:
                        self._seen_msg_ids[mid] = now_ts

                    sender = str(m.get("from_user_id") or "").strip()
                    ctx_tok = str(m.get("context_token") or "").strip()
                    if sender and ctx_tok:
                        with self._lock:
                            self.context_tokens[sender] = ctx_tok
                            if not self.default_peer:
                                self.default_peer = sender
                            self._save_context_tokens()

                    # 白名单保护：仅响应已绑定的 Owner
                    if self.default_peer and sender != self.default_peer:
                        logger.info("忽略非 Owner 消息: %s", sender[:12])
                        continue

                    text_parts = []
                    for it in m.get("item_list") or []:
                        if isinstance(it, dict) and it.get("type") == ITEM_TEXT:
                            ti = it.get("text_item") or {}
                            if ti.get("text"):
                                text_parts.append(str(ti["text"]))
                    content = "\n".join(text_parts).strip()
                    if not content:
                        continue

                    self.last_rx_at = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
                    logger.info("收到微信指令: %s", content[:80])
                    threading.Thread(
                        target=self._dispatch_incoming,
                        args=(sender, content),
                        daemon=True,
                    ).start()
            except (TimeoutError, urllib.error.URLError) as exc:
                # 长轮询超时属正常现象，立即重连
                if "timed out" not in str(exc).lower():
                    logger.warning("getupdates 网络波动: %s", exc)
                    time.sleep(3)
            except Exception as exc:  # noqa: BLE001
                logger.warning("getupdates 异常: %s", exc)
                time.sleep(5)


PUSH_POLICY_PATH = Path(r"F:\Aistock\runtime\acct2_managed\push_routing_policy.json")


def _match_suppressed_legacy_acct2(full_text: str, source: str) -> Optional[str]:
    """热读取推送收口策略：暂停旧系统关于 acct2/王账号 的推送，仅放行 allowed_sources。"""
    if not PUSH_POLICY_PATH.exists():
        return None
    try:
        policy = json.loads(PUSH_POLICY_PATH.read_text(encoding="utf-8"))
        if not policy.get("pause_legacy_acct2_push"):
            return None
        allowed = {str(s).strip() for s in (policy.get("allowed_sources") or []) if str(s).strip()}
        if source and source in allowed:
            return None
        haystack = (full_text or "").lower()
        for kw in policy.get("legacy_acct2_keywords") or []:
            k = str(kw).strip()
            if k and k.lower() in haystack:
                return k
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy] 读取推送策略失败（放行）: %s", exc)
    return None


def run_http_server(bridge: ILinkBridge, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
            logger.debug(fmt, *args)

        def _reply(self, status: int, obj: dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/health"):
                self._reply(200, {
                    "ok": True,
                    "account_id": bridge.account_id,
                    "peer_ready": bool(bridge.default_peer),
                    "last_rx_at": bridge.last_rx_at,
                    "last_tx_at": bridge.last_tx_at,
                })
                return
            if self.path.startswith("/inbox"):
                self._reply(200, bridge.list_pending_inbox())
                return
            self._reply(404, {"ok": False, "detail": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/reply"):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    self._reply(400, {"ok": False, "detail": "invalid json"})
                    return
                res = bridge.reply_inbox_question(
                    text=str(data.get("text") or data.get("content") or ""),
                    msg_id=data.get("id"),
                    peer=data.get("peer"),
                )
                self._reply(200 if res.get("ok") else 400, res)
                return
            if self.path.startswith("/send"):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    self._reply(400, {"ok": False, "detail": "invalid json"})
                    return
                title = str(data.get("title") or "").strip()
                text = str(data.get("text") or data.get("content") or "").strip()
                full = f"{title}\n{text}".strip() if title else text
                source = str(data.get("source") or "").strip()
                suppressed_kw = _match_suppressed_legacy_acct2(full, source)
                if suppressed_kw is not None:
                    logger.info("[policy] 已拦截旧 acct2/王账号推送（命中=%s, source=%s）: %s", suppressed_kw, source or "legacy", title or full[:60])
                    self._reply(200, {"ok": True, "suppressed": True, "detail": f"suppressed_legacy_acct2:{suppressed_kw}"})
                    return
                res = bridge.send_text(full, peer=data.get("peer"), source=source or "http_send", assign_id=True)
                self._reply(200 if res.get("ok") else 400, res)
                return
            self._reply(404, {"ok": False, "detail": "not found"})

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, name="ilink-http", daemon=True)
    t.start()
    logger.info("iLink 本地 HTTP 桥接已监听 0.0.0.0:%d", port)
    return srv


def main() -> None:
    bridge = ILinkBridge(DEFAULT_STATE_DIR, DEFAULT_API_BASE)
    run_http_server(bridge, DEFAULT_PORT)
    bridge.poll_forever()


if __name__ == "__main__":
    main()
