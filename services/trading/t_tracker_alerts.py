# -*- coding: utf-8 -*-
"""盘中 beat suppressed 异常告警（2026-11-13 Owner 要求，做T运行层 A 方案）。

背景：beat 的 codes[].suppressed.reason 里藏着 evaluate_error（评估异常——
如 KeyError/NameError/tz bug 曾静默失效 3 天无人知）。业务闸
（t_lot_below_min/not_in_pool/cooldown 等）是正常拒发，不该告警。

语义（区别于业务闸）：
  - reason 含 evaluate_error 前缀，或含 Exception 类关键词
    （KeyError/NameError/HTTPError/TypeError/Traceback 等 \\w*(Error|Exception|
    Warning) 模式）→ 评估异常 → 告警；
  - 业务闸/数据闸 reason（t_lot_below_min、not_in_pool、cooldown_*、
    outside_time_window、disarmed_wait_rearm、daily_t_cap、
    rsi_insufficient_data、stale_minute_data、empty_minute_data、
    no_regime_info…）不含上述关键词 → 永远静默；
  - 同异常（code + 异常类名）10min 去重冷却；冷却只压推送/落 pending，
    不压审计 jsonl。

产物（runtime/t_tracker/alerts/，beat 写方=pipeline-runner 容器）：
  - YYYYMMDD.jsonl：当日全部异常 suppress 审计行（deduped 标记）；
  - cooldown.json：去重状态 {fingerprint: 首次告警 ts}；
  - pending/*.json：待推送告警；pushed/：已消费。

微信通道：watch_drafts.py 同款（PM watcher 轮询 pending/ → 打印即推送，
弹窗/spool 机制复用）；beat 进程内 print 进容器日志兜底。告警链路任何
失败都绝不影响节拍本身（调用方全程 try/except 包裹，见 t_trade_signal
._alert_beat_suppressions）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

__version__ = "1.0.0"

SCHEMA_ALERT_LOG = "t_tracker_alert_log/1"
SCHEMA_ALERT_PENDING = "t_tracker_alert_pending/1"

ALERTS_DIR = "alerts"
PENDING_DIR = "pending"
PUSHED_DIR = "pushed"
COOLDOWN_FILE = "cooldown.json"

#: 同异常（code+异常类名）去重冷却窗口（秒）
COOLDOWN_SEC = 10 * 60.0

#: evaluate_error 是 t_trade_signal 的异常封装前缀（run_beat/run_fast_tick）
EXCEPTION_PREFIX = "evaluate_error"
#: Exception 类名模式（KeyError/NameError/HTTPError/TypeError/Traceback…）
_EXC_CLASS_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning)|Traceback)\b")
#: evaluate_error 行内异常类（"evaluate_error: KeyError: 'sell'" → KeyError）
_EXC_AFTER_PREFIX_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning))\b")

logger = logging.getLogger("t_tracker_alerts")


# ── 判别（纯函数）────────────────────────────────────────────

def is_exception_reason(reason: Any) -> bool:
    """reason 是否评估异常（区别于业务闸）。业务闸标识符不含异常关键词。"""
    if not reason or not isinstance(reason, str):
        return False
    if reason.strip().startswith(EXCEPTION_PREFIX):
        return True
    return bool(_EXC_CLASS_RE.search(reason))


def exception_class_of(reason: Any) -> str:
    """从 reason 提取异常类名（去重指纹用）；提取失败回退 unknown。"""
    text = str(reason or "").strip()
    if text.startswith(EXCEPTION_PREFIX):
        text = text[len(EXCEPTION_PREFIX):].lstrip(" :")
    m = _EXC_AFTER_PREFIX_RE.match(text) or _EXC_CLASS_RE.search(text)
    return m.group(1) if m else "unknown"


def summarize_reason(reason: Any, limit: int = 160) -> str:
    """异常摘要（去掉 evaluate_error 前缀，截断）。"""
    text = str(reason or "").strip()
    if text.startswith(EXCEPTION_PREFIX):
        text = text[len(EXCEPTION_PREFIX):].lstrip(" :")
    return text[:limit]


def scan_beat_codes(beat_record: Mapping[str, Any]) -> list[dict]:
    """扫 beat_record.codes[]，抽出全部异常型 suppressed。"""
    finds: list[dict] = []
    codes = beat_record.get("codes") or {}
    if not isinstance(codes, Mapping):
        return finds
    for res_key, res in codes.items():
        if not isinstance(res, Mapping):
            continue
        sup = res.get("suppressed")
        reason = (sup or {}).get("reason") if isinstance(sup, Mapping) else None
        if not is_exception_reason(reason):
            continue
        code = str(res.get("code") or res_key).split(".")[0]
        finds.append({
            "res_key": str(res_key),
            "code": code,
            "reason": str(reason),
            "exc_class": exception_class_of(reason),
            "summary": summarize_reason(reason),
        })
    return finds


# ── 冷却状态 ────────────────────────────────────────────────

def _cooldown_path(root: Path) -> Path:
    return root / ALERTS_DIR / COOLDOWN_FILE


def load_cooldown(root: Path) -> dict:
    """读去重状态；损坏/缺失 → 空 dict（fail-open：宁可多告不可漏告）。"""
    path = _cooldown_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cooldown(root: Path, state: Mapping[str, Any]) -> None:
    path = _cooldown_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, default=str) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


# ── 推送（watch_drafts.py 同款：print 通道 + pending 交 PM watcher）────

def default_push(texts: Sequence[str]) -> None:
    """beat 进程内即时输出（容器日志）；微信送达由 PM watcher 消费 pending/。"""
    for line in texts:
        print(line, flush=True)


def format_alert_text(alert: Mapping[str, Any]) -> str:
    lane = str(alert.get("lane") or "slow")
    lane_label = "快车道" if lane == "fast" else ("慢车道" if lane == "slow" else lane)
    first_seen = str(alert.get("first_seen", "")).replace("T", " ")
    return (
        f"🚨【做T引擎异常】{alert.get('name') or ''}({alert.get('code')})\n"
        f"▸ {alert.get('summary', '')[:120]}\n"
        f"▸ {lane_label} {first_seen} 首现（同异常 10min 去重）\n"
        f"——评估异常≠业务闸：需当日排查；业务闸静默不告警"
    )


# ── 主入口（beat 写方调用；自身异常由调用方兜底）────────────────

def process_beat_suppressions(
    *,
    root: Path,
    beat_record: Mapping[str, Any],
    now: datetime,
    lane: str = "slow",
    push: Optional[Callable[[Sequence[str]], None]] = None,
    log: Optional[logging.Logger] = None,
) -> dict:
    """处理一拍 beat 的异常型 suppressed：审计 jsonl + 冷去重 + pending + 推送。

    返回 {"status": ..., "found": N, "alerted": [...], "deduped": N}；
    本函数只处理数据面，任何推送失败不抛出（推送 best-effort）。
    """
    log = log or logger
    root = Path(root)
    finds = scan_beat_codes(beat_record)
    if not finds:
        return {"status": "no_exceptions", "found": 0, "alerted": [], "deduped": 0}

    alerts_dir = root / ALERTS_DIR
    alerts_dir.mkdir(parents=True, exist_ok=True)
    (alerts_dir / PENDING_DIR).mkdir(parents=True, exist_ok=True)
    (alerts_dir / PUSHED_DIR).mkdir(parents=True, exist_ok=True)

    day_file = alerts_dir / f"{now.strftime('%Y%m%d')}.jsonl"
    state = load_cooldown(root)
    now_iso = now.isoformat(timespec="seconds")

    alerted: list[dict] = []
    deduped = 0
    for find in finds:
        fingerprint = f"{find['code']}:{find['exc_class']}"
        first_ts = _parse_iso(state.get(fingerprint))
        is_new = first_ts is None or (now - first_ts).total_seconds() >= COOLDOWN_SEC
        row = {
            "schema": SCHEMA_ALERT_LOG, "ts": now_iso, "lane": lane,
            "beat_ts": beat_record.get("ts"), "fingerprint": fingerprint,
            "code": find["code"], "reason": find["reason"][:400],
            "exc_class": find["exc_class"], "deduped": not is_new,
        }
        try:
            with day_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            log.error("[alerts] 审计 jsonl 写失败（继续）：%s", exc)

        if not is_new:
            deduped += 1
            continue
        state[fingerprint] = now_iso
        alert = {
            "schema": SCHEMA_ALERT_PENDING, "fingerprint": fingerprint,
            "code": find["code"], "name": "",
            "exc_class": find["exc_class"], "summary": find["summary"],
            "lane": lane, "first_seen": now_iso,
            "beat_ts": beat_record.get("ts"),
        }
        alerted.append(alert)

    if alerted:
        _save_cooldown(root, state)
        for alert in alerted:
            pending = alerts_dir / PENDING_DIR / (
                f"{now.strftime('%Y%m%d_%H%M%S')}_{alert['code']}_{alert['exc_class']}.json")
            tmp = pending.with_name(pending.name + ".tmp")
            try:
                tmp.write_text(
                    json.dumps(alert, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
                os.replace(tmp, pending)
            except OSError as exc:
                log.error("[alerts] pending 写失败（跳过该条推送）：%s", exc)
        try:
            (push or default_push)([format_alert_text(a) for a in alerted])
        except Exception as exc:  # noqa: BLE001 - 推送 best-effort
            log.error("[alerts] 推送失败（pending 已落盘待补达）：%s", exc)

    return {"status": "alerted" if alerted else "all_deduped",
            "found": len(finds), "alerted": alerted, "deduped": deduped}


def replay_beat_file(
    *,
    root: Path,
    beat_path: Path,
    now: Optional[datetime] = None,
    lane: Optional[str] = None,
    push: Optional[Callable[[Sequence[str]], None]] = None,
    log: Optional[logging.Logger] = None,
) -> list[dict]:
    """按序回放一个 beats jsonl 的全部拍（运维补告/首版取证用）。

    lane 缺省按文件名（fast_ 前缀=快车道）；只对最后一拍告警去重语义与线上
    一致（逐拍走 process_beat_suppressions，冷却状态如实推进）。
    """
    lane = lane or ("fast" if Path(beat_path).name.startswith("fast_") else "slow")
    out: list[dict] = []
    now = now or datetime.now()
    try:
        lines = Path(beat_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        out.append(process_beat_suppressions(
            root=root, beat_record=record, now=now, lane=lane, push=push, log=log))
    return out
