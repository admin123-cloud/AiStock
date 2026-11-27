# -*- coding: utf-8 -*-
"""AR4.6 极端冰点 30m 战役跨日持久化与熊市 B 档退出引擎。

核心纪律（严格对齐 AR4.6 实证规则与实盘 SOP）：
1. 冰点 30m 突破触发建仓后，进入 `T0 ~ T0+5` 活跃战役持有期与 `i+6` 冷却锁：
   - T+1 起合规模板真龙属于「策略预期内 · 冰点 B 档持有」，绝不因次日无新 30m 信号而误判为「预期外待清退」；
   - 冷却锁期内不续期、不加仓、不补买换股；提前离场的资金回流现金或避险 ETF（十年国债 511260 / 红利低波 512890）。
2. 熊市 B 档逐票退出判定（T+1 ~ T0+5）：
   - 硬止损：相对券商成本价 <= -6.0% → 立即清退卖出（豁免早盘防割肉延迟，第一时间保护本金）；
   - 移动追踪止盈：入场后（T+1 起统计峰值，严禁拿 T0 入场前高点）累计浮盈 >= +5.0% 后，自峰值回撤 >= -3.5% → 止盈离场；
   - 宽度修复止盈：全市场站上 MA20 宽度 >= 38.0% → 全员清退回防 ETF；
   - 时间止损：最晚持满 T0+5 交易日 14:40 收盘前清退离场。
3. 排雷硬违规遗留持仓特例（如上线前漏进的黄灯低催化/刚摘帽标的）：
   - 标记 `screening_violation=True`，不纳入合规目标持仓，保持可一键卖出状态，供人工逢反抽优先处置；若破 -6% 硬止损同步强制告警。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))

DEFAULT_MAX_HOLD_DAYS = 5
DEFAULT_COOLDOWN_DAYS = 6
HARD_STOP_PCT = -6.0
TRAIL_ARM_PCT = 5.0
TRAIL_DD_PCT = -3.5
BREADTH_EXIT_THRESHOLD = 38.0
TIME_STOP_CUTOFF = dt_time(14, 40)


def _campaign_path(base_dir: Optional[Path] = None) -> Path:
    root = base_dir or runtime_path("acct1_advisor")
    root.mkdir(parents=True, exist_ok=True)
    return root / "active_panic_campaign.json"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _parse_date(val: Any) -> Optional[date]:
    s = str(val or "").strip()[:10]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def count_trading_days_between(start_str: str, end_str: str) -> int:
    """计算 start_str -> end_str 之间经过的交易日数（T0=0, T+1=1, ..., T+5=5）。"""
    d0 = _parse_date(start_str)
    d1 = _parse_date(end_str)
    if d0 is None or d1 is None or d1 <= d0:
        return 0
    cnt = 0
    cur = d0 + timedelta(days=1)
    while cur <= d1:
        if _is_weekday(cur):
            cnt += 1
        cur += timedelta(days=1)
    return cnt


def add_trading_days(start_str: str, n: int) -> str:
    """从 start_str 往后推 n 个工作日，返回 YYYY-MM-DD。"""
    d0 = _parse_date(start_str)
    if d0 is None:
        return str(start_str or "")
    cur = d0
    rem = max(0, int(n))
    while rem > 0:
        cur += timedelta(days=1)
        if _is_weekday(cur):
            rem -= 1
    return cur.isoformat()


def load_active_panic_campaign(*, base_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """读取当前持久化的 AR4.6 冰点战役档案；不存在或损坏返回 None。"""
    fp = _campaign_path(base_dir)
    if not fp.is_file():
        return None
    try:
        doc = json.loads(fp.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


def save_active_panic_campaign(
    doc: Mapping[str, Any],
    *,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """原子落盘 AR4.6 冰点战役档案。"""
    out = dict(doc)
    _atomic_write_json(_campaign_path(base_dir), out)
    return out


def evaluate_panic_campaign_state(
    *,
    holdings: Sequence[Mapping[str, Any]],
    live_prices: Mapping[str, float],
    breadth_pct: Optional[float],
    now_bj: Optional[datetime] = None,
    account_id: str = "acct1",
    base_dir: Optional[Path] = None,
    persist_updates: bool = True,
) -> dict[str, Any]:
    """评估活跃冰点战役状态、逐票 B 档退出判决与 i+6 冷却锁。

    返回结构：
    - active: 是否存在未结束的冰点战役（持仓中或冷却期内）
    - in_cooldown: 是否处于 i+6 冷却锁（禁止新增/补买/换股）
    - held_days / max_hold_days / entry_date / exit_deadline_date / cooldown_until_date
    - positions: {code6: {...逐票 B 档判定...}}
    - compliant_hold_codes: set[str]（当前仍应作为策略预期内持有的合规冰点票）
    - exit_signal_codes: dict[str, dict]（已触发 B 档退出或排雷违规需卖出的票及原因）
    """
    now = now_bj or datetime.now(BJ_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BJ_TZ)
    today_str = now.strftime("%Y-%m-%d")

    doc = load_active_panic_campaign(base_dir=base_dir)
    if not doc or str(doc.get("account_id") or "acct1") != str(account_id):
        return {
            "active": False,
            "in_cooldown": False,
            "held_days": 0,
            "max_hold_days": DEFAULT_MAX_HOLD_DAYS,
            "cooldown_days": DEFAULT_COOLDOWN_DAYS,
            "entry_date": None,
            "exit_deadline_date": None,
            "cooldown_until_date": None,
            "positions": {},
            "compliant_hold_codes": [],
            "exit_signals": {},
            "summary": None,
        }

    entry_date = str(doc.get("entry_date") or "").strip()[:10]
    max_hold = int(doc.get("max_hold_trading_days") or DEFAULT_MAX_HOLD_DAYS)
    cooldown_days = int(doc.get("cooldown_trading_days") or DEFAULT_COOLDOWN_DAYS)
    held_days = count_trading_days_between(entry_date, today_str)
    exit_deadline = add_trading_days(entry_date, max_hold)
    cooldown_until = add_trading_days(entry_date, cooldown_days)

    # 若已超过冷却期且状态已结束，返回非活跃
    if str(doc.get("status") or "") == "closed" and held_days >= cooldown_days:
        return {
            "active": False,
            "in_cooldown": False,
            "held_days": held_days,
            "max_hold_days": max_hold,
            "cooldown_days": cooldown_days,
            "entry_date": entry_date,
            "exit_deadline_date": exit_deadline,
            "cooldown_until_date": cooldown_until,
            "positions": {},
            "compliant_hold_codes": [],
            "exit_signals": {},
            "summary": None,
        }

    holdings_map: dict[str, Mapping[str, Any]] = {}
    for h in holdings:
        c6 = str(h.get("code") or "").split(".")[0].strip().zfill(6)
        if c6 and int(h.get("shares") or 0) > 0:
            holdings_map[c6] = h

    raw_positions = doc.get("positions") if isinstance(doc.get("positions"), dict) else {}
    updated_positions: dict[str, dict[str, Any]] = {}
    eval_positions: dict[str, dict[str, Any]] = {}
    compliant_hold_codes: list[str] = []
    exit_signals: dict[str, dict[str, Any]] = {}
    dirty = False

    still_held_count = 0
    for raw_code, pos in raw_positions.items():
        if not isinstance(pos, dict):
            continue
        c6 = str(raw_code).split(".")[0].strip().zfill(6)
        p_copy = dict(pos)
        h = holdings_map.get(c6)
        if h is None:
            if p_copy.get("status") != "exited":
                p_copy["status"] = "exited"
                p_copy["exited_at"] = today_str
                dirty = True
            updated_positions[c6] = p_copy
            continue

        still_held_count += 1
        entry_px = float(p_copy.get("entry_price") or h.get("cost_price") or h.get("current_price") or 0.0)
        live_px = float(live_prices.get(c6) or h.get("current_price") or entry_px)
        prev_peak = float(p_copy.get("peak_price") or entry_px)

        # 严格遵守 SOP：仅从 T+1（held_days >= 1）起更新入场后新高，避免 T0 盘前高点污染追踪止盈
        eff_peak = prev_peak
        if held_days >= 1 and live_px > eff_peak:
            eff_peak = round(live_px, 3)
            p_copy["peak_price"] = eff_peak
            p_copy["peak_updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            dirty = True

        pnl_pct = round((live_px / entry_px - 1.0) * 100.0, 2) if entry_px > 0 else 0.0
        peak_gain_pct = round((eff_peak / entry_px - 1.0) * 100.0, 2) if entry_px > 0 else 0.0
        dd_from_peak_pct = round((live_px / eff_peak - 1.0) * 100.0, 2) if eff_peak > 0 else 0.0
        stop_px = round(entry_px * (1.0 + HARD_STOP_PCT / 100.0), 2) if entry_px > 0 else None
        trail_arm_px = round(entry_px * (1.0 + TRAIL_ARM_PCT / 100.0), 2) if entry_px > 0 else None
        trail_armed = peak_gain_pct >= TRAIL_ARM_PCT
        trail_stop_px = round(eff_peak * (1.0 + TRAIL_DD_PCT / 100.0), 2) if (trail_armed and eff_peak > 0) else None
        is_violation = bool(p_copy.get("screening_violation"))
        violation_note = str(p_copy.get("violation_note") or "AI 排雷硬禁买遗留持仓")

        action = "HOLD_BTIER"
        exit_reason = ""
        bypass_floor_guard = False

        if is_violation:
            if held_days >= 1 and pnl_pct <= HARD_STOP_PCT:
                action = "SELL_VIOLATION_STOP"
                exit_reason = f"排雷违规持仓且已触发 {HARD_STOP_PCT:.0f}% 硬止损（当前 {pnl_pct:+.2f}%），立即清退"
                bypass_floor_guard = True
            else:
                action = "SELL_VIOLATION"
                exit_reason = f"{violation_note}；逢反抽优先清退（严守 {HARD_STOP_PCT:.0f}% 止损 ¥{stop_px}）"
                bypass_floor_guard = False
        elif held_days == 0:
            action = "HOLD_T0"
        elif pnl_pct <= HARD_STOP_PCT:
            action = "EXIT_HARD_STOP"
            exit_reason = f"触及冰点 B 档 {HARD_STOP_PCT:.0f}% 硬止损（成本 ¥{entry_px:.2f} → 现价 ¥{live_px:.2f}，{pnl_pct:+.2f}%）"
            bypass_floor_guard = True
        elif trail_armed and dd_from_peak_pct <= TRAIL_DD_PCT:
            action = "EXIT_TRAILING_STOP"
            exit_reason = f"触及冰点 B 档追踪止盈（入场峰值 +{peak_gain_pct:.1f}% → 回撤 {dd_from_peak_pct:.1f}%，跌破 ¥{trail_stop_px:.2f}）"
            bypass_floor_guard = True
        elif breadth_pct is not None and float(breadth_pct) >= BREADTH_EXIT_THRESHOLD:
            action = "EXIT_BREADTH_RECOVERY"
            exit_reason = f"全市场宽度已修复至 {float(breadth_pct):.1f}% ≥ {BREADTH_EXIT_THRESHOLD:.0f}%，按冰点纪律清退回防 ETF"
            bypass_floor_guard = True
        elif held_days > max_hold or (held_days == max_hold and now.time() >= TIME_STOP_CUTOFF):
            action = "EXIT_TIME_STOP"
            exit_reason = f"已达冰点最长持仓窗口 T+{held_days}/{max_hold}（截止 {exit_deadline} 尾盘），时间止损离场"
            bypass_floor_guard = True

        item_eval = {
            "code": c6,
            "name": str(p_copy.get("name") or h.get("name") or c6),
            "entry_date": entry_date,
            "held_days": held_days,
            "max_hold_days": max_hold,
            "exit_deadline_date": exit_deadline,
            "entry_price": round(entry_px, 3),
            "live_price": round(live_px, 3),
            "peak_price": round(eff_peak, 3),
            "pnl_pct": pnl_pct,
            "peak_gain_pct": peak_gain_pct,
            "dd_from_peak_pct": dd_from_peak_pct,
            "stop_loss_price": stop_px,
            "trail_arm_price": trail_arm_px,
            "trail_armed": trail_armed,
            "trail_stop_price": trail_stop_px,
            "screening_violation": is_violation,
            "violation_note": violation_note if is_violation else "",
            "action": action,
            "exit_reason": exit_reason,
            "bypass_floor_guard": bypass_floor_guard,
        }
        eval_positions[c6] = item_eval
        updated_positions[c6] = p_copy

        if action in ("HOLD_BTIER", "HOLD_T0"):
            compliant_hold_codes.append(c6)
        else:
            exit_signals[c6] = item_eval

    in_cooldown = held_days < cooldown_days
    new_status = "active" if still_held_count > 0 else ("cooldown" if in_cooldown else "closed")
    if doc.get("status") != new_status:
        dirty = True

    if persist_updates and dirty:
        new_doc = {
            **doc,
            "status": new_status,
            "positions": updated_positions,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            save_active_panic_campaign(new_doc, base_dir=base_dir)
        except Exception:
            pass

    active = still_held_count > 0 or in_cooldown
    summary = (
        f"冰点战役 T+{held_days}/{max_hold}（入场 {entry_date} · 最晚 {exit_deadline} 离场 · "
        f"i+6 冷却锁至 {cooldown_until} 不换股）"
        if active
        else None
    )
    return {
        "active": active,
        "status": new_status,
        "in_cooldown": in_cooldown,
        "held_days": held_days,
        "max_hold_days": max_hold,
        "cooldown_days": cooldown_days,
        "entry_date": entry_date,
        "exit_deadline_date": exit_deadline,
        "cooldown_until_date": cooldown_until,
        "still_held_count": still_held_count,
        "positions": eval_positions,
        "compliant_hold_codes": compliant_hold_codes,
        "exit_signals": exit_signals,
        "summary": summary,
    }
