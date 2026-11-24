# -*- coding: utf-8 -*-
"""G3 做T实战台单向微信通知服务与后台草案监听器。

支持 4 种零封号风险的单向微信推送通道（可单独或组合开启）：
1. pushplus      : PushPlus（推送加）个人微信推送（推荐，免费 200 条/天）
2. wecom_webhook : 企业微信群机器人 Webhook（免费无上限，普通微信可直接接收）
3. serverchan    : Server酱 Turbo（SendKey）个人微信服务号推送
4. qq_email      : 复用系统已配置的 QQ 邮箱 SMTP（配合微信“QQ邮箱提醒”零注册即用）

持久化目录：runtime/g3_desk_plans/（aistock-backend 授权可写目录，绝不修改 t_tracker:ro）。
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger("t_notify")

_watcher_started = False
_watcher_lock = threading.Lock()

# AR4.6 大盘总闸门 60 秒缓存 + 同向防刷屏冷却（30 分钟）+ 过期草案拦截（15 分钟）
_GATE_CACHE_TTL_SEC = 60.0
_gate_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_SAME_SIDE_COOLDOWN_SEC = 30 * 60
_STALE_DRAFT_MAX_AGE_SEC = 15 * 60


def get_ar45_buy_gate_status(*, force_refresh: bool = False) -> dict[str, Any]:
    """查询当前 AR4.6 大盘总闸门是否允许任何 A 股个股买入（含底仓做T买侧）。

    遵循 fail-closed 原则：只要市场非 offensive（即防守/平衡/恐慌期），或门禁查询异常，
    一律禁止发出任何 A 股个股 buy-T 买入提醒（仅放行 sell-T 高抛反T减仓）。
    """
    now_mono = time.monotonic()
    if not force_refresh and _gate_cache["data"] is not None and (now_mono - float(_gate_cache["ts"])) < _GATE_CACHE_TTL_SEC:
        return dict(_gate_cache["data"])
    try:
        from services.g3.market_regime_gate import evaluate_market_regime_gate

        gate = evaluate_market_regime_gate()
        regime = str(gate.get("regime") or "defensive")
        allow_new = bool(gate.get("allow_new_mainwave"))
        allow_buy = (regime == "offensive") and allow_new
        res = {
            "allow_buy": allow_buy,
            "regime": regime,
            "regime_label": str(gate.get("regime_label") or regime),
            "reason": "ok" if allow_buy else f"ar45_{regime}_no_a_stock_buy",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[t_notify] 查询 AR4.6 总闸门异常，按 fail-closed 拦截买入信号: %s", exc)
        res = {
            "allow_buy": False,
            "regime": "unknown_fail_closed",
            "regime_label": "闸门异常·默认禁买",
            "reason": "ar45_gate_error_fail_closed",
        }
    _gate_cache["ts"] = now_mono
    _gate_cache["data"] = res
    return dict(res)


def _store_dir() -> Path:
    p = runtime_path("g3_desk_plans")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _config_path() -> Path:
    return _store_dir() / "notify_config.json"


def _notified_path(trade_date: str) -> Path:
    ymd = trade_date.replace("-", "")
    return _store_dir() / f"notified_{ymd}.json"


def _default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "channel": "ilink_bridge",  # ilink_bridge | pushplus | wecom_webhook | serverchan | qq_email
        "ilink_bridge_url": os.getenv("AISTOCK_WEIXIN_BRIDGE_URL", "http://host.docker.internal:8789"),
        "pushplus_token": "",
        "wecom_webhook": "",
        "serverchan_sendkey": "",
        "qq_email_to": "",
        "only_trading_hours": True,
        "notify_on_draft": True,
        "notify_on_stop_alert": True,
        "updated_at": None,
    }


def load_notify_config() -> dict[str, Any]:
    cfg = _default_config()
    p = _config_path()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception:  # noqa: BLE001
            pass
    return cfg


def _mask_secret(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}****{s[-4:]}"


def _check_ilink_bridge_health(url: str) -> dict[str, Any]:
    base = (url or "http://host.docker.internal:8789").rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return {"online": bool(data.get("ok")), **data}
    except Exception:  # noqa: BLE001
        return {"online": False}


def get_public_notify_config() -> dict[str, Any]:
    """返回脱敏后的通知配置，供前端表单展示。"""
    cfg = load_notify_config()
    smtp = _load_smtp_settings()
    bridge_url = str(cfg.get("ilink_bridge_url") or os.getenv("AISTOCK_WEIXIN_BRIDGE_URL", "http://host.docker.internal:8789"))
    ilink_health = _check_ilink_bridge_health(bridge_url)
    return {
        "enabled": bool(cfg.get("enabled")),
        "channel": str(cfg.get("channel") or "ilink_bridge"),
        "ilink_bridge_url": bridge_url,
        "ilink_bridge_online": bool(ilink_health.get("online")),
        "ilink_account_id": ilink_health.get("account_id") or "",
        "ilink_sdk_available": bool(ilink_health.get("sdk_available")),
        "has_pushplus_token": bool(cfg.get("pushplus_token")),
        "pushplus_token_masked": _mask_secret(str(cfg.get("pushplus_token") or "")),
        "has_wecom_webhook": bool(cfg.get("wecom_webhook")),
        "wecom_webhook_masked": _mask_secret(str(cfg.get("wecom_webhook") or "")),
        "has_serverchan_sendkey": bool(cfg.get("serverchan_sendkey")),
        "serverchan_sendkey_masked": _mask_secret(str(cfg.get("serverchan_sendkey") or "")),
        "qq_email_to": str(cfg.get("qq_email_to") or smtp.get("user") or ""),
        "smtp_ready": bool(smtp.get("server") and smtp.get("user") and smtp.get("password")),
        "only_trading_hours": bool(cfg.get("only_trading_hours", True)),
        "notify_on_draft": bool(cfg.get("notify_on_draft", True)),
        "notify_on_stop_alert": bool(cfg.get("notify_on_stop_alert", True)),
        "updated_at": cfg.get("updated_at"),
    }


def save_notify_config(patch: Mapping[str, Any]) -> dict[str, Any]:
    """保存通知配置；敏感密钥字段若传入空字符串或 None，保留原值（除非明确传 __CLEAR__）。"""
    cfg = load_notify_config()
    if "enabled" in patch:
        cfg["enabled"] = bool(patch["enabled"])
    if "channel" in patch and str(patch["channel"]) in ("ilink_bridge", "pushplus", "wecom_webhook", "serverchan", "qq_email"):
        cfg["channel"] = str(patch["channel"])
    for key in ("pushplus_token", "wecom_webhook", "serverchan_sendkey"):
        if key in patch and patch[key] is not None:
            v = str(patch[key]).strip()
            if v == "__CLEAR__":
                cfg[key] = ""
            elif v and "****" not in v:
                cfg[key] = v
    if "qq_email_to" in patch and patch["qq_email_to"] is not None:
        cfg["qq_email_to"] = str(patch["qq_email_to"]).strip()
    for bkey in ("only_trading_hours", "notify_on_draft", "notify_on_stop_alert"):
        if bkey in patch:
            cfg[bkey] = bool(patch[bkey])

    cfg["updated_at"] = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    p = _config_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return get_public_notify_config()


def _load_smtp_settings() -> dict[str, Any]:
    """读取 settings.local.yaml 中的 SMTP 配置（用于 QQ 邮箱微信提醒兜底通道）。"""
    candidates = [
        Path("/app/config/settings.local.yaml"),
        Path(__file__).resolve().parents[2] / "config" / "settings.local.yaml",
        Path(__file__).resolve().parents[2] / "config" / "settings.docker-app.yaml",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            # 兼容根级或 email/alert 嵌套结构
            alert = doc.get("alert") or doc.get("email") or doc
            server = alert.get("smtp_server") or doc.get("smtp_server") or ""
            port = int(alert.get("smtp_port") or doc.get("smtp_port") or 465)
            user = alert.get("smtp_user") or doc.get("smtp_user") or ""
            pwd = alert.get("smtp_password") or doc.get("smtp_password") or ""
            if server and user and pwd:
                return {"server": server, "port": port, "user": user, "password": pwd}
        except Exception:  # noqa: BLE001
            continue
    return {}


# ---------------------------------------------------------------------------
# 4 种单向微信推送实现
# ---------------------------------------------------------------------------

def send_wechat_notification(
    title: str,
    markdown_body: str,
    *,
    config: Optional[Mapping[str, Any]] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """按当前配置发送一条单向微信通知。返回 {ok, channel, detail}。"""
    cfg = dict(config) if config is not None else load_notify_config()
    channel = str(cfg.get("channel") or "ilink_bridge")

    try:
        if channel == "ilink_bridge":
            base = str(cfg.get("ilink_bridge_url") or os.getenv("AISTOCK_WEIXIN_BRIDGE_URL", "http://host.docker.internal:8789")).rstrip("/")
            payload = json.dumps({"title": title, "text": markdown_body}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{base}/send",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8", errors="replace"))
            return {"ok": bool(res.get("ok")), "channel": channel, "detail": str(res.get("detail") or res)}

        if channel == "pushplus":
            token = str(cfg.get("pushplus_token") or "").strip()
            if not token:
                return {"ok": False, "channel": channel, "detail": "未配置 PushPlus Token"}
            payload = json.dumps({
                "token": token,
                "title": title,
                "content": markdown_body,
                "template": "markdown",
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://www.pushplus.plus/send",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8", errors="replace"))
            ok = int(res.get("code") or 0) == 200
            return {"ok": ok, "channel": channel, "detail": str(res.get("msg") or res)}

        if channel == "wecom_webhook":
            url = str(cfg.get("wecom_webhook") or "").strip()
            if not url.startswith("http"):
                return {"ok": False, "channel": channel, "detail": "未配置有效的企业微信机器人 Webhook URL"}
            payload = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": f"### {title}\n{markdown_body}"},
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8", errors="replace"))
            ok = int(res.get("errcode") if res.get("errcode") is not None else -1) == 0
            return {"ok": ok, "channel": channel, "detail": str(res.get("errmsg") or res)}

        if channel == "serverchan":
            key = str(cfg.get("serverchan_sendkey") or "").strip()
            if not key:
                return {"ok": False, "channel": channel, "detail": "未配置 Server酱 SendKey"}
            data = urllib.parse.urlencode({"title": title, "desp": markdown_body}).encode("utf-8")
            req = urllib.request.Request(
                f"https://sctapi.ftqq.com/{key}.send",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8", errors="replace"))
            ok = int(res.get("code") if res.get("code") is not None else -1) == 0
            return {"ok": ok, "channel": channel, "detail": str(res.get("message") or res)}

        if channel == "qq_email":
            smtp = _load_smtp_settings()
            if not smtp:
                return {"ok": False, "channel": channel, "detail": "系统未配置 QQ 邮箱 SMTP 凭据"}
            to_addr = str(cfg.get("qq_email_to") or smtp["user"]).strip()
            msg = MIMEText(markdown_body, "plain", "utf-8")
            msg["Subject"] = Header(title, "utf-8")
            msg["From"] = smtp["user"]
            msg["To"] = to_addr
            with smtplib.SMTP_SSL(smtp["server"], int(smtp["port"]), timeout=timeout) as client:
                client.login(smtp["user"], smtp["password"])
                client.sendmail(smtp["user"], [to_addr], msg.as_string())
            return {"ok": True, "channel": channel, "detail": f"已发送至 {to_addr}（微信 QQ 邮箱提醒可实时接收）"}

        return {"ok": False, "channel": channel, "detail": f"不支持的通知通道: {channel}"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[t_notify] 微信通知发送失败 (%s): %s", channel, exc)
        return {"ok": False, "channel": channel, "detail": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 后台轻量监听器：自动捕捉新做T信号草案并单向推送微信（带去重）
# ---------------------------------------------------------------------------

def _load_notified_state(trade_date: str) -> tuple[set[str], dict[str, float]]:
    p = _notified_path(trade_date)
    if not p.exists():
        return set(), {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return set(), {}
        ids = set(data.get("ids") or [])
        raw_cd = data.get("cooldowns") or {}
        cooldowns = {str(k): float(v) for k, v in raw_cd.items() if isinstance(v, (int, float))}
        return ids, cooldowns
    except Exception:  # noqa: BLE001
        return set(), {}


def _save_notified_state(trade_date: str, ids: set[str], cooldowns: Mapping[str, float]) -> None:
    p = _notified_path(trade_date)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {"date": trade_date, "ids": sorted(ids), "cooldowns": dict(cooldowns)},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, p)


def _load_notified_ids(trade_date: str) -> set[str]:
    ids, _ = _load_notified_state(trade_date)
    return ids


def _save_notified_ids(trade_date: str, ids: set[str]) -> None:
    _, cooldowns = _load_notified_state(trade_date)
    _save_notified_state(trade_date, ids, cooldowns)


def _parse_draft_ts_bj(raw: Any) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=BJ_TZ)
        except ValueError:
            continue
    return None


def _in_trading_hours(now_bj: datetime) -> bool:
    if now_bj.weekday() >= 5:
        return False
    t = now_bj.time()
    return (dt_time(9, 15) <= t <= dt_time(11, 35)) or (dt_time(12, 55) <= t <= dt_time(15, 5))


def format_draft_message(
    draft: Mapping[str, Any],
    *,
    gate_info: Optional[Mapping[str, Any]] = None,
) -> tuple[str, str]:
    """将一条做T信号草案格式化为单向推送标题与 Markdown 正文。"""
    code = str(draft.get("code") or "")
    name = str(draft.get("name") or code)
    side_raw = str(draft.get("side") or "")
    is_buy = "buy" in side_raw.lower()
    side_label = "🟢 买入T信号" if is_buy else "🔴 卖出T/高抛减仓信号"
    px = draft.get("px") or draft.get("price") or draft.get("trigger_price") or "—"
    qty = draft.get("qty") or draft.get("shares") or "—"
    ts = str(draft.get("ts") or draft.get("created_at") or "")
    trig = draft.get("trigger") if isinstance(draft.get("trigger"), Mapping) else {}
    sig_blk = draft.get("signal") if isinstance(draft.get("signal"), Mapping) else {}
    kind = str(
        trig.get("kind")
        or draft.get("trigger_kind")
        or sig_blk.get("type")
        or draft.get("lane")
        or "做T信号"
    )
    rsi5 = trig.get("rsi5") if trig.get("rsi5") is not None else (sig_blk.get("rsi5") if sig_blk.get("rsi5") is not None else draft.get("intrabar_rsi5"))
    rsi15 = trig.get("rsi15") if trig.get("rsi15") is not None else sig_blk.get("rsi15")
    vwap = trig.get("vwap")

    title = f"【{side_label}】{name}({code}) @ {px}"
    lines = [
        f"- **标的**：{name}（`{code}`）",
        f"- **方向**：{side_label}（建议 `{qty}` 股）",
        f"- **触发价**：`{px}`（时间 `{ts}`）",
        f"- **触发依据**：`{kind}`",
    ]
    if rsi5 is not None or rsi15 is not None:
        lines.append(f"- **实时指标**：RSI15m=`{rsi15 if rsi15 is not None else '—'}` / RSI5m=`{rsi5 if rsi5 is not None else '—'}` / VWAP=`{vwap if vwap is not None else '—'}`")
    if gate_info and gate_info.get("regime_label"):
        lines.append(f"- **AR4.6 大盘状态**：`{gate_info['regime_label']}`")
    if not is_buy:
        lines.append("- **提示**：高抛减仓/反T卖点参考（若为非 AR4.6 历史旧底仓，优先按实战交易台清退）。")
    else:
        lines.append("- **提示**：单向提醒，请结合同花顺盘口与「资金账号与做T」确认后手动操作。")
    return title, "\n".join(lines)


def poll_and_push_new_drafts_once(
    *,
    now_bj: Optional[datetime] = None,
    tracker_dir_override: Optional[Path] = None,
    gate_info_override: Optional[Mapping[str, Any]] = None,
) -> int:
    """扫描今日 drafts 与 pending_push，经 AR4.6 总闸门与 30m 防刷屏冷却后单向推送微信。"""
    cfg = load_notify_config()
    if not cfg.get("enabled") or not cfg.get("notify_on_draft", True):
        return 0
    now_dt = now_bj or datetime.now(BJ_TZ)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=BJ_TZ)
    if cfg.get("only_trading_hours", True) and not _in_trading_hours(now_dt):
        return 0

    today_str = now_dt.strftime("%Y-%m-%d")
    ymd = today_str.replace("-", "")
    tracker_dir = tracker_dir_override or runtime_path("t_tracker")
    notified, cooldowns = _load_notified_state(today_str)
    state_dirty = False

    candidates: list[dict[str, Any]] = []
    # 1) 优先扫描 pending_push
    pp_dir = tracker_dir / "pending_push"
    if pp_dir.is_dir():
        for f in sorted(pp_dir.glob(f"*{ymd}*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    obj.setdefault("id", f.stem)
                    candidates.append(obj)
            except Exception:  # noqa: BLE001
                continue

    # 2) 补扫当日 drafts jsonl
    d_file = tracker_dir / "drafts" / f"{ymd}.jsonl"
    if d_file.is_file():
        for idx, line in enumerate(d_file.read_text(encoding="utf-8", errors="ignore").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    obj.setdefault("id", f"{ymd}_line_{idx}")
                    candidates.append(obj)
            except ValueError:
                continue

    if not candidates:
        return 0

    gate_info = dict(gate_info_override) if gate_info_override is not None else get_ar45_buy_gate_status()
    allow_buy = bool(gate_info.get("allow_buy"))
    now_epoch = now_dt.timestamp()

    sent_count = 0
    for d in candidates:
        did = str(d.get("id") or "")
        if not did or did in notified:
            continue
        # 1) 已过期草案直接标为已见
        status = str(d.get("bar_status") or d.get("status") or "")
        if status == "expired_unconfirmed":
            notified.add(did)
            state_dirty = True
            continue

        # 2) 超过 15 分钟的滞留旧草案不再补发（防断线恢复后集中轰炸）
        dts = _parse_draft_ts_bj(d.get("ts") or d.get("created_at"))
        if dts is not None and (now_dt - dts).total_seconds() > _STALE_DRAFT_MAX_AGE_SEC:
            notified.add(did)
            state_dirty = True
            logger.info("[t_notify] 跳过超时滞留草案 %s (ts=%s)", did, d.get("ts"))
            continue

        # 3) AR4.6 总闸门硬拦截：非进攻期严禁推送任何 A 股个股买入T信号
        side_raw = str(d.get("side") or "")
        is_buy = "buy" in side_raw.lower()
        code6 = str(d.get("code") or "").split(".")[0].strip()
        if is_buy and not allow_buy:
            notified.add(did)
            state_dirty = True
            logger.info(
                "[t_notify] AR4.6 总闸门拦截冲突 buy-T 信号 %s (%s, regime=%s)",
                did,
                code6,
                gate_info.get("regime"),
            )
            continue

        # 4) 同标的同方向 30 分钟冷却门禁（杜绝 L2 心跳每 30 秒换 draft_id 重复刷屏）
        cd_key = f"{code6}:{'buy' if is_buy else 'sell'}"
        last_sent_ts = float(cooldowns.get(cd_key) or 0.0)
        if last_sent_ts > 0 and (now_epoch - last_sent_ts) < _SAME_SIDE_COOLDOWN_SEC:
            notified.add(did)
            state_dirty = True
            logger.info(
                "[t_notify] 同向 30m 冷却跳过重复草案 %s (%s, 距上次 %ds)",
                did,
                cd_key,
                int(now_epoch - last_sent_ts),
            )
            continue

        title, body = format_draft_message(d, gate_info=gate_info)
        res = send_wechat_notification(title, body, config=cfg)
        if res.get("ok"):
            notified.add(did)
            cooldowns[cd_key] = now_epoch
            state_dirty = True
            sent_count += 1
            logger.info("[t_notify] 已单向推送做T草案 %s -> %s", did, res.get("channel"))
        else:
            logger.warning("[t_notify] 推送草案 %s 失败: %s", did, res.get("detail"))

    if state_dirty:
        _save_notified_state(today_str, notified, cooldowns)
    return sent_count


def _watcher_loop() -> None:
    while True:
        try:
            poll_and_push_new_drafts_once()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[t_notify] watcher tick error: %s", exc)
        time.sleep(10)


def ensure_draft_watcher_started() -> None:
    """幂等启动后台 10 秒草案监听线程（daemon，不阻塞进程退出）。"""
    global _watcher_started
    if _watcher_started:
        return
    with _watcher_lock:
        if _watcher_started:
            return
        t = threading.Thread(target=_watcher_loop, name="g3-t-notify-watcher", daemon=True)
        t.start()
        _watcher_started = True
        logger.info("[t_notify] G3 做T单向微信推送监听线程已启动（10s 轮询）")
