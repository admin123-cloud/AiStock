# -*- coding: utf-8 -*-
"""acct1（广发证券-曾*）AR4.6 专属盘中智能买卖提醒引擎。

设计定位（严格对齐用户指令与 acct2 托管微信体验）：
1. 唯一策略大脑：100% 绑定 AR4.6（compile_ar3_rebalance_plan + build_ar3_dual_core_radar），
   绝不混用旧做T RSI 买单；防守/平衡期严禁推送任何 A 股个股买入，只放行跨资产逆波 ETF 与旧底仓清退卖出；
2. 允许盘中实时买卖（09:35~11:28 & 13:00~14:50，新开买仓截止 14:45），严格遵守 A 股 T+1、
   100 股整手、涨跌停保护、最小下单额（>=2500 元，清仓尾单除外）与 ETF 盘中 MA20 站稳确认；
3. 微信通知风格完全对标 acct2（独立账号标签 `acct1·AR4.6`，一眼区分操作方向、股数、盘口参考价、
   金额、策略理由、止损位与执行后预计资金），并带 2 小时同向防刷屏冷却（每日同向至多 2 次）；
4. 支持「聊天报单核销」（record_acct1_manual_fill）：在同花顺下次快照前自动叠加已成交回款与持股变化，
   用户卖出回款后可立刻触发衔接的 AR4.6 买单提示；同花顺快照追平后自动以券商实盘为准。
"""
from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from services.broker.broker_accounts import get_accounts_overview
from services.g3.ar45_review_service import build_ar45_review_report
from services.g3.market_regime_gate import evaluate_market_regime_gate
from services.mining.ar3_dual_core_live import build_ar3_dual_core_radar
from services.mining.ar3_rebalance_compiler import compile_ar3_rebalance_plan
from services.trading.t_daily_plan import fetch_batch_live_quotes
from services.trading.t_notify import send_wechat_notification
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
ACCOUNT_ID = "acct1"
MIN_ORDER_YUAN = 2500.0  # 低于 2500 元的碎单不推送（清仓尾单除外），省去最低 5 元佣金摩擦
COOLDOWN_SEC = 2 * 3600  # 同标的同方向未成交时 2 小时冷却
MAX_PUSH_PER_KEY_PER_DAY = 2  # 同标的同方向每日至多推送 2 次（上午 1 次 + 下午 1 次）

# 2026-11-23 实盘自省补丁：极端冰点防地板割肉保护阈值
PANIC_BREADTH_THRESHOLD = 18.0   # 全市场站上 MA20 宽度 <= 18% 视为极端恐慌冰点区
PANIC_SHARP_DROP_PCT = -2.5      # 极端冰点区内，个股盘中急跌 <= -2.5% 禁止在早盘/盘中地板价催卖
PANIC_TAIL_CONFIRM_TIME = dt_time(14, 35)  # 冰点急跌保护至 14:35 尾盘确认，或等盘中反抽至 VWAP 上方/跌幅收窄至 -1% 内
SMOOTH_DAILY_DONE_RATIO = 0.25   # 单票当日已人工核销卖出额 >= 总资产 25% 即视为完成当日 30% 平稳步长，不再二次催卖

MORNING_START = dt_time(9, 35)
MORNING_END = dt_time(11, 28)
AFTERNOON_START = dt_time(13, 0)
AFTERNOON_SELL_END = dt_time(14, 50)
AFTERNOON_BUY_END = dt_time(14, 45)

logger = logging.getLogger("acct1_advisor")


def _advisor_dir(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("acct1_advisor")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _state_path(trade_date: str, base_dir: Optional[Path] = None) -> Path:
    return _advisor_dir(base_dir) / f"state_{trade_date.replace('-', '')}.json"


def _fills_path(trade_date: str, base_dir: Optional[Path] = None) -> Path:
    return _advisor_dir(base_dir) / f"fills_{trade_date.replace('-', '')}.jsonl"


def _policy_path(base_dir: Optional[Path] = None) -> Path:
    return _advisor_dir(base_dir) / "push_policy.json"


def _live_signals_path(trade_date: str, base_dir: Optional[Path] = None) -> Path:
    return _advisor_dir(base_dir) / f"live_signals_{trade_date.replace('-', '')}.json"


def _broadcasts_path(trade_date: str, base_dir: Optional[Path] = None) -> Path:
    return _advisor_dir(base_dir) / f"broadcasts_{trade_date.replace('-', '')}.jsonl"


def load_live_signals(trade_date: str, *, base_dir: Optional[Path] = None) -> dict[str, Any]:
    """读取当日最近一次 10 分钟轮询产出的实时可执行待填单快照。"""
    doc = _load_json(_live_signals_path(trade_date, base_dir))
    if not doc:
        return {
            "date": trade_date,
            "updated_at": None,
            "regime": None,
            "regime_label": None,
            "breadth_pct": None,
            "available_cash": None,
            "total_assets": None,
            "orders": [],
            "skipped_summary": [],
        }
    return doc


def append_intraday_broadcast(
    entry: Mapping[str, Any],
    *,
    trade_date: str,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """追加一条 10 分钟盘面总结播报（仅页面展示，绝不触发微信通知）。"""
    fp = _broadcasts_path(trade_date, base_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(entry)
    with fp.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def get_intraday_broadcast_feed(
    *,
    limit: int = 50,
    now_bj: Optional[datetime] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """合并 AI Bus 深度盘面播报（kind=intraday_broadcast）与规则兜底快照，供 /g3/ai-live 滚动播放。"""
    from services.g3.ai_bus import get_bus_status

    now = now_bj or datetime.now(BJ_TZ)
    trade_date = now.strftime("%Y-%m-%d")
    items: list[dict[str, Any]] = []

    # 1. 读取今日与昨日规则引擎 10 分钟播报流
    for delta in (0, 1):
        d_str = (now.date() - timedelta(days=delta)).strftime("%Y-%m-%d")
        fp = _broadcasts_path(d_str, base_dir)
        if not fp.exists():
            continue
        for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    items.append(obj)
            except ValueError:
                continue

    # 2. 合并 AI Bus 已完成的 intraday_broadcast 深度研判
    bus = get_bus_status(limit=max(limit, 30))
    for r in bus.get("recent_results") or []:
        if not isinstance(r, dict) or str(r.get("kind") or "") != "intraday_broadcast":
            continue
        ts_str = str(r.get("completed_at") or r.get("created_at") or "")
        items.append({
            "id": str(r.get("job_id") or f"ai_{ts_str}"),
            "ts": ts_str,
            "slot": ts_str[:16],
            "channel": "ai_bus",
            "channel_label": "Antigravity AI 深度播报",
            "decision": str(r.get("decision") or "advisory"),
            "headline": str(r.get("summary") or "AI 盘面研判完成"),
            "body": str(r.get("answer") or ""),
            "risk_flags": list(r.get("risk_flags") or []),
            "actions": list(r.get("actions") or []),
            "executable_count": None,
        })

    # 按时间倒序排列并截断
    items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    live_sig = load_live_signals(trade_date, base_dir=base_dir)
    return {
        "ok": True,
        "date": trade_date,
        "session_online": bool(bus.get("session_online")),
        "heartbeat_at": bus.get("heartbeat_at"),
        "pending_ai_jobs": int(bus.get("pending_count") or 0),
        "last_poll_at": live_sig.get("updated_at"),
        "live_orders_count": len(live_sig.get("orders") or []),
        "items": items[: max(1, min(int(limit), 100))],
    }


def load_push_policy(base_dir: Optional[Path] = None) -> dict[str, Any]:
    """热读取 acct1 推送收口策略（不重启即可生效，默认关闭暂停以保持兼容）。"""
    raw = _load_json(_policy_path(base_dir))
    return {
        "pause_intraday_auto_push": bool(raw.get("pause_intraday_auto_push", False)),
        "pause_ar2_radar_broadcast": bool(raw.get("pause_ar2_radar_broadcast", False)),
        "reason": str(raw.get("reason") or ""),
        "updated_at": raw.get("updated_at"),
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _in_intraday_session(now_bj: datetime, *, for_buy: bool = False) -> bool:
    if now_bj.weekday() >= 5:
        return False
    t = now_bj.time()
    in_am = MORNING_START <= t <= MORNING_END
    pm_end = AFTERNOON_BUY_END if for_buy else AFTERNOON_SELL_END
    in_pm = AFTERNOON_START <= t <= pm_end
    return in_am or in_pm


def _parse_iso_bj(raw: Any) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=BJ_TZ)
        return dt.astimezone(BJ_TZ)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=BJ_TZ)
        except ValueError:
            continue
    return None


def _price_limit_pct(code6: str) -> float:
    if code6.startswith(("300", "301", "688", "588", "159915")):
        return 20.0
    if code6.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def _is_etf(code6: str) -> bool:
    return code6.startswith(("51", "56", "58", "159", "16"))


def _calc_fees(side: str, amount: float, is_etf: bool) -> float:
    comm = max(5.0, round(amount * 0.00025, 2)) if amount > 0 else 0.0
    tax = round(amount * 0.0005, 2) if (side.upper() == "SELL" and not is_etf and amount > 0) else 0.0
    return round(comm + tax, 2)


# ---------------------------------------------------------------------------
# 手工成交核销与快照叠加（对齐 acct2 聊天回写体验）
# ---------------------------------------------------------------------------

def load_manual_fills(trade_date: str, *, base_dir: Optional[Path] = None, lookback_days: int = 1) -> list[dict[str, Any]]:
    """加载当日（或近 lookback_days 天）人工核销成交记录，用于跨日仍早于券商快照时的精准叠加。"""
    try:
        base_dt = datetime.strptime(trade_date, "%Y-%m-%d").date()
    except ValueError:
        base_dt = datetime.now(BJ_TZ).date()
    rows: list[dict[str, Any]] = []
    for delta in range(max(1, lookback_days) - 1, -1, -1):
        d_str = (base_dt - timedelta(days=delta)).strftime("%Y-%m-%d")
        fp = _fills_path(d_str, base_dir)
        if not fp.exists():
            continue
        for line in fp.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except ValueError:
                continue
    return rows


def record_acct1_manual_fill(
    *,
    code: str,
    side: str,
    shares: int,
    price: float,
    name: str = "",
    note: str = "",
    now_bj: Optional[datetime] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """登记一笔 acct1 人工成交，立即生效并在快照追平前自动叠加到可用现金与持仓。"""
    now = now_bj or datetime.now(BJ_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BJ_TZ)
    trade_date = now.strftime("%Y-%m-%d")
    code6 = str(code).split(".")[0].strip().zfill(6)
    side_u = "BUY" if str(side).strip().upper().startswith("B") else "SELL"
    qty = int(shares)
    px = float(price)
    if qty <= 0 or px <= 0:
        raise ValueError("成交股数与成交均价必须大于 0")
    is_etf = _is_etf(code6)
    amt = round(qty * px, 2)
    fee = _calc_fees(side_u, amt, is_etf)
    rec = {
        "ts": now.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "account_id": ACCOUNT_ID,
        "code": code6,
        "name": name or code6,
        "side": side_u,
        "shares": qty,
        "price": round(px, 3 if is_etf else 2),
        "amount": amt,
        "est_fee": fee,
        "is_etf": is_etf,
        "note": note,
    }
    fp = _fills_path(trade_date, base_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def apply_manual_fills_overlay(
    overview: Mapping[str, Any],
    trade_date: str,
    *,
    base_dir: Optional[Path] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """将晚于 acct1 快照时间（含近 4 天跨日未刷新快照期间）的手工成交叠加至 overview 副本中。"""
    fills = load_manual_fills(trade_date, base_dir=base_dir, lookback_days=4)
    if not fills:
        return copy.deepcopy(dict(overview)), []

    ov = copy.deepcopy(dict(overview))
    accounts = ov.get("accounts") or []
    acct = next((a for a in accounts if isinstance(a, dict) and str(a.get("account_id")) == ACCOUNT_ID), None)
    if not acct:
        return ov, []

    snap_dt = _parse_iso_bj(acct.get("generated_at"))
    applied: list[dict[str, Any]] = []
    funds = acct.setdefault("funds", {})
    holdings: list[dict[str, Any]] = acct.setdefault("holdings", [])

    avail_cash = float(funds.get("available") or 0.0)
    by_code: dict[str, dict[str, Any]] = {}
    for h in holdings:
        if isinstance(h, dict):
            c6 = str(h.get("code") or "").split(".")[0].strip().zfill(6)
            if c6:
                by_code[c6] = h

    for f in fills:
        f_dt = _parse_iso_bj(f.get("ts"))
        if snap_dt is not None and f_dt is not None and f_dt <= snap_dt:
            continue  # 券商实盘快照已包含该笔成交，跳过叠加
        c6 = str(f["code"]).zfill(6)
        side_u = str(f["side"]).upper()
        qty = int(f["shares"])
        px = float(f["price"])
        amt = float(f.get("amount") or (qty * px))
        fee = float(f.get("est_fee") or _calc_fees(side_u, amt, _is_etf(c6)))

        if side_u == "SELL":
            avail_cash += max(0.0, amt - fee)
            if c6 in by_code:
                cur_q = int(float(by_code[c6].get("quantity") or 0))
                cur_av = int(float(by_code[c6].get("available_quantity") if by_code[c6].get("available_quantity") is not None else cur_q))
                new_q = max(0, cur_q - qty)
                by_code[c6]["quantity"] = new_q
                by_code[c6]["available_quantity"] = max(0, min(new_q, cur_av - qty))
                by_code[c6]["current_price"] = px
                by_code[c6]["market_value"] = round(new_q * px, 2)
        else:
            avail_cash = max(0.0, avail_cash - (amt + fee))
            if c6 in by_code:
                cur_q = int(float(by_code[c6].get("quantity") or 0))
                new_q = cur_q + qty
                by_code[c6]["quantity"] = new_q
                # 跨日已解锁：若成交日 < 今日，则 T+1 自动解冻进 available_quantity；当日新买计入 today_locked_shares
                if str(f.get("trade_date") or "") < trade_date:
                    cur_av = int(float(by_code[c6].get("available_quantity") or 0))
                    by_code[c6]["available_quantity"] = min(new_q, cur_av + qty)
                else:
                    by_code[c6]["today_locked_shares"] = int(float(by_code[c6].get("today_locked_shares") or 0)) + qty
                by_code[c6]["current_price"] = px
                by_code[c6]["market_value"] = round(new_q * px, 2)
            else:
                unlocked = str(f.get("trade_date") or "") < trade_date
                item = {
                    "code": c6,
                    "name": f.get("name") or c6,
                    "quantity": qty,
                    "available_quantity": qty if unlocked else 0,
                    "today_locked_shares": 0 if unlocked else qty,
                    "cost_price": px,
                    "current_price": px,
                    "market_value": round(qty * px, 2),
                }
                holdings.append(item)
                by_code[c6] = item
        applied.append(f)

    if applied:
        mv_total = sum(
            float(h.get("quantity") or 0) * float(h.get("current_price") or 0)
            for h in holdings
            if isinstance(h, dict) and float(h.get("quantity") or 0) > 0
        )
        funds["available"] = round(avail_cash, 2)
        funds["market_value"] = round(mv_total, 2)
        funds["total_assets"] = round(avail_cash + mv_total, 2)
        acct["holdings"] = [h for h in holdings if isinstance(h, dict) and float(h.get("quantity") or 0) > 0]
    return ov, applied


# ---------------------------------------------------------------------------
# 微信文案格式化（严格对标 acct2 清晰可读风格）
# ---------------------------------------------------------------------------

def _format_sell_wechat(
    order: Mapping[str, Any],
    plan: Mapping[str, Any],
    live_px: float,
    pct_chg: Optional[float],
    now_str: str,
) -> tuple[str, str]:
    acct = plan["account"]
    code = str(order["code"])
    name = str(order["name"])
    qty = int(order["suggested_shares"])
    full_sh = int(order.get("full_clear_shares") or qty)
    amt = round(qty * live_px, 2)
    est_cash_after = round(float(acct["available_cash"]) + amt * 0.9992, 2)
    px_str = f"{live_px:.3f}" if order.get("is_etf") else f"{live_px:.2f}"
    chg_str = f"（当日 {pct_chg:+.2f}%）" if pct_chg is not None else ""

    buy_hint_parts = [
        f"{b['name']}({b['code']})目标{b['target_weight_pct']}%"
        for b in (plan.get("buy_orders") or [])[:2]
    ]
    next_hint = f"卖出回款后将自动衔接推送买单（{' + '.join(buy_hint_parts)}）" if buy_hint_parts else "卖出后保留现金防守"

    title = f"【🔴 acct1·AR4.6 卖出】{name}({code}) {qty}股 @ {px_str}"
    lines = [
        f"- 账号：`acct1`（{acct['label']} · AR4.6 策略驱动）",
        f"- 操作：**卖出 {qty} 股** `{code}` {name}" + (f"（总可卖 {full_sh} 股，平稳步长分批）" if full_sh > qty else "（清仓可用）"),
        f"- 盘口参考：`{px_str}`{chg_str}，预计回款 `{amt:,.2f}` 元",
        f"- 时间：`{now_str}`｜当前大盘：`{plan['regime_label']}`",
        f"- 策略理由：{order.get('reason') or 'AR4.6 调仓卖出'}",
        f"- 卖后预计可用现金：`{est_cash_after:,.2f}` 元｜{next_hint}",
        f"- 提示：打开 https://stock.aistock3.fun/g3/desk 可直接「一键填单至同花顺（prefill_only）」并点「登记已成交」校准账本；也可直接回复「acct1 已卖 {code} {qty}股 @ 均价」。",
    ]
    return title, "\n".join(lines)


def _format_buy_wechat(
    order: Mapping[str, Any],
    plan: Mapping[str, Any],
    live_px: float,
    pct_chg: Optional[float],
    ma20: Optional[float],
    now_str: str,
) -> tuple[str, str]:
    acct = plan["account"]
    code = str(order["code"])
    name = str(order["name"])
    qty = int(order["immediate_shares"])
    is_etf = bool(order.get("is_etf") or order.get("asset_kind") == "etf")
    amt = round(qty * live_px, 2)
    rem_cash = max(0.0, round(float(acct["available_cash"]) - amt - _calc_fees("BUY", amt, is_etf), 2))
    px_str = f"{live_px:.3f}" if is_etf else f"{live_px:.2f}"
    stop_px = order.get("stop_loss_price")
    stop_str = (f"{float(stop_px):.3f}" if is_etf else f"{float(stop_px):.2f}") if stop_px else "—"
    min_px = order.get("min_buy_price") or (ma20 if is_etf and ma20 else None)
    max_px = order.get("max_buy_price")
    min_str = (f"{float(min_px):.3f}" if is_etf else f"{float(min_px):.2f}") if min_px else "—"
    max_str = (f"{float(max_px):.3f}" if is_etf else f"{float(max_px):.2f}") if max_px else "—"
    min_reason = str(order.get("min_buy_reason") or ("跌破 MA20 放弃" if is_etf else "跌破放弃线不买"))
    chg_str = f"（当日 {pct_chg:+.2f}%）" if pct_chg is not None else ""
    ma20_str = f"｜盘中站稳 MA20(`{ma20:.3f}`)" if (is_etf and ma20 and live_px >= ma20) else ""

    title = f"【🟢 acct1·AR4.6 买入】{name}({code}) {qty}股 @ {px_str}"
    lines = [
        f"- 账号：`acct1`（{acct['label']} · AR4.6 策略驱动）",
        f"- 操作：**买入 {qty} 股** `{code}` {name}（目标占比 `{order.get('target_weight_pct', 0)}%`）",
        f"- 盘口参考：`{px_str}`{chg_str}，预计金额 `{amt:,.2f}` 元｜参考止损：`{stop_str}`",
        f"- 买入红线：跌破 `{min_str}`（{min_reason}）｜涨超 `{max_str}` 谨慎追高",
        f"- 时间：`{now_str}`｜当前大盘：`{plan['regime_label']}`{ma20_str}",
        f"- 策略理由：{order.get('reason') or 'AR4.6 目标配置建仓'}",
        f"- 买后预计剩余现金：`{rem_cash:,.2f}` 元｜账户总资产：`{float(acct['total_assets']):,.2f}` 元",
        f"- 提示：打开 https://stock.aistock3.fun/g3/desk 可直接「一键填单至同花顺（prefill_only）」并点「登记已成交」同步账本；也可直接回复「acct1 已买 {code} {qty}股 @ 均价」。",
    ]
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# 三大标准执行模式：premarket / intraday / eod
# ---------------------------------------------------------------------------

def run_acct1_advisor(
    mode: str = "intraday",
    *,
    dry_run: bool = False,
    bypass_session: bool = False,
    suppress_wechat: bool = False,
    now_bj: Optional[datetime] = None,
    base_dir: Optional[Path] = None,
    overview_override: Optional[Mapping[str, Any]] = None,
    radar_override: Optional[Mapping[str, Any]] = None,
    quotes_override: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """执行 acct1 AR4.6 盘前/盘中/盘后巡检与微信买卖提醒。"""
    now = now_bj or datetime.now(BJ_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BJ_TZ)
    trade_date = now.strftime("%Y-%m-%d")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    if mode == "intraday" and not bypass_session and not _in_intraday_session(now, for_buy=False):
        return {
            "status": "skipped_outside_session",
            "mode": mode,
            "ts": now_str,
            "pushed": [],
        }

    # 盘中推送收口开关（热读取 push_policy.json）：
    # 即使 pause_intraday_auto_push=True，10 分钟巡检仍照常校验实时盘口并更新页面待填单队列与盘面播报，
    # 仅控制是否把单笔买卖单推送到微信（盘面播报本身绝不推微信）。
    policy = load_push_policy(base_dir)
    allow_wechat_order_push = (not dry_run) and (not suppress_wechat) and (not bool(policy.get("pause_intraday_auto_push")))

    # 1. 拉取真实账户概览并叠加尚未入快照的手工成交
    raw_overview = dict(overview_override) if overview_override is not None else get_accounts_overview()
    eff_overview, applied_fills = apply_manual_fills_overlay(raw_overview, trade_date, base_dir=base_dir)

    # 2. 日终复盘模式直接走 AR4.6 真钱归因报告
    if mode == "eod":
        rev = build_ar45_review_report(account_id=ACCOUNT_ID, days=30, accounts_overview=eff_overview)
        acct = rev["account"]
        kpis = rev["kpis"]
        leg_lines = [
            f"  • {leg['label']}：{leg['count']} 只，市值 `{leg['market_value']:,.0f}` 元，浮盈 `{leg['floating_profit']:+,.2f}` 元"
            for leg in (rev.get("legs") or [])
        ]
        title = f"【📊 acct1·AR4.6 日终复盘】{trade_date}"
        body = "\n".join([
            f"- 账号：`acct1`（{acct['label']}）",
            f"- 总资产：`{acct['total_assets']:,.2f}` 元｜现金 `{acct['available_cash']:,.2f}`｜仓位 `{acct['position_pct']}%`",
            f"- 当日真钱盈亏：`{acct['daily_profit']:+,.2f}` 元" if acct.get("daily_profit") is not None else "- 当日盈亏：待券商快照更新",
            f"- 纪律评分：`{kpis['discipline_score']}/100`｜破-10%止损红线持仓：`{kpis['stop_violation_count']}` 只",
            "- 三腿持仓归因：",
            *leg_lines,
            "- 复盘详情：打开 `/g3/review` 查看净值曲线与逐笔 Shadow 对账。",
        ])
        notify_res = {"ok": True, "detail": "dry_run"} if dry_run else send_wechat_notification(title, body)
        return {
            "status": "ok",
            "mode": "eod",
            "ts": now_str,
            "title": title,
            "body": body,
            "notify": notify_res,
            "applied_manual_fills": len(applied_fills),
        }

    # 3. 编译 AR4.6 唯一权威调仓计划（先卖后买）
    if radar_override is not None:
        radar = dict(radar_override)
        raw_b = (radar.get("market_breadth_pct") if radar.get("market_breadth_pct") is not None else None)
        market_gate = {
            "regime": str(radar.get("regime") or "defensive"),
            "regime_label": "主升进攻期" if radar.get("is_offensive") else "防守退潮·趋势下跌",
            "allow_new_mainwave": bool(radar.get("is_offensive")),
            "sources": {"breadth": {"above_ma20_pct": raw_b}} if raw_b is not None else {},
        }
        panic_sig = {"triggered": bool((radar.get("etf_radar") or {}).get("panic_triggered"))}
    else:
        market_gate = evaluate_market_regime_gate()
        try:
            from services.mining.panic_rebound_monitor import evaluate_panic_rebound_signal

            panic_sig = evaluate_panic_rebound_signal(market_gate=market_gate)
        except Exception:
            panic_sig = None
        radar = build_ar3_dual_core_radar(market_gate=market_gate, panic_signal=panic_sig, force_refresh=False)

    # 提取当前市场宽度（用于冰点防地板割肉保护 & AR2_2A 实时右侧反抽播报）
    _b_src = ((market_gate.get("sources") or {}).get("breadth") or {}) if isinstance(market_gate, Mapping) else {}
    try:
        breadth_pct: Optional[float] = float(_b_src["above_ma20_pct"]) if _b_src.get("above_ma20_pct") is not None else None
    except (TypeError, ValueError):
        breadth_pct = None
    in_panic_zone = breadth_pct is not None and breadth_pct <= PANIC_BREADTH_THRESHOLD

    # 汇总当日已核销卖出金额（按代码，用于判断 30% 单日平稳减仓步长是否已完成，避免二次催卖）
    today_sold_yuan_by_code: dict[str, float] = {}
    for f in load_manual_fills(trade_date, base_dir=base_dir):
        if str(f.get("side") or "").upper() == "SELL":
            fc = str(f.get("code") or "").zfill(6)
            today_sold_yuan_by_code[fc] = today_sold_yuan_by_code.get(fc, 0.0) + float(f.get("amount") or 0.0)

    plan = compile_ar3_rebalance_plan(
        market_gate=market_gate,
        panic_signal=panic_sig,
        account_id=ACCOUNT_ID,
        smooth_step=True,
        persist=False,
        ar3_radar=radar,
        accounts_overview=eff_overview,
        panic_campaign_dir=_advisor_dir(base_dir),
        now_bj=now,
    )
    acct = plan["account"]
    is_offensive = bool(plan.get("is_offensive"))

    # 收集 ETF MA20 字典（用于盘中买 ETF 前站稳 MA20 校验）
    etf_ma20_map: dict[str, float] = {}
    for e in ((radar.get("etf_radar") or {}).get("universe") or []) + ((radar.get("etf_radar") or {}).get("targets") or []):
        if isinstance(e, Mapping) and e.get("code") and e.get("ma20"):
            etf_ma20_map[str(e["code"]).split(".")[0].zfill(6)] = float(e["ma20"])

    # 4. 盘前计划推送模式
    if mode == "premarket":
        sell_lines = [
            f"  • [卖出] {s['name']}({s['code']}) {s['suggested_shares']}股 @ ~{s['ref_price']}（约 ¥{s['est_amount']:,.0f}）"
            for s in (plan.get("sell_orders") or [])
            if int(s.get("suggested_shares") or 0) >= 100 and not s.get("blocked_t1")
        ]
        buy_lines = [
            f"  • [买入] {b['name']}({b['code']}) 现可买 {b['immediate_shares']}股 / 卖后应买 {b['post_sell_shares']}股 @ ~{b['ref_price']}（目标 {b['target_weight_pct']}%）"
            for b in (plan.get("buy_orders") or [])
        ]
        title = f"【📋 acct1·AR4.6 盘前计划】{trade_date}"
        body_lines = [
            f"- 账号：`acct1`（{acct['label']}）｜大盘：**{plan['regime_label']}**",
            f"- 总资产：`{acct['total_assets']:,.2f}` 元｜可用现金：`{acct['available_cash']:,.2f}` 元｜仓位：`{acct['position_pct']}%`",
            "- 第一步·今日拟卖出：",
            *(sell_lines or ["  • 无卖出任务"]),
            "- 第二步·今日拟买入（卖后衔接）：",
            *(buy_lines or ["  • 无买入任务"]),
            "- 盘中巡检：09:35~14:50 每 10 分钟实时校验盘口与 MA20，触发即推送单笔可执行指令。",
        ]
        body = "\n".join(body_lines)
        notify_res = {"ok": True, "detail": "dry_run"} if dry_run else send_wechat_notification(title, body)
        return {
            "status": "ok",
            "mode": "premarket",
            "ts": now_str,
            "title": title,
            "body": body,
            "notify": notify_res,
        }

    # 5. 盘中巡检模式（mode == "intraday"）：严格实时盘口 + 门禁校验 + 冷却去重
    codes_needed = list({
        str(x["code"]).zfill(6)
        for x in (plan.get("sell_orders") or []) + (plan.get("buy_orders") or [])
        if x.get("code")
    })
    if quotes_override is not None:
        live_quotes = dict(quotes_override)
    else:
        try:
            live_quotes = fetch_batch_live_quotes(codes_needed) if codes_needed else {}
        except Exception:  # noqa: BLE001
            live_quotes = {}

    st_path = _state_path(trade_date, base_dir)
    state = _load_json(st_path)
    sent_map: dict[str, dict[str, Any]] = state.setdefault("sent", {})
    now_epoch = now.timestamp()

    pushed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    executable_orders: list[dict[str, Any]] = []

    def _can_push(key: str, fingerprint: str) -> tuple[bool, str]:
        rec = sent_map.get(key)
        if not rec:
            return True, "first_today"
        if int(rec.get("count") or 0) >= MAX_PUSH_PER_KEY_PER_DAY and rec.get("fingerprint") == fingerprint:
            return False, "daily_cap_reached"
        elapsed = now_epoch - float(rec.get("last_ts") or 0.0)
        if rec.get("fingerprint") == fingerprint and elapsed < COOLDOWN_SEC:
            return False, f"cooldown_{int(elapsed)}s"
        return True, "refresh_or_changed"

    # 5A. 先审卖出单（优先清退破位/非 AR4.6 持仓）
    for s in plan.get("sell_orders") or []:
        c6 = str(s["code"]).zfill(6)
        qty = int(s.get("suggested_shares") or 0)
        full_sh = int(s.get("full_clear_shares") or qty)
        if s.get("blocked_t1") or qty < 100:
            skipped.append({"code": c6, "side": "SELL", "reason": "t1_blocked_or_zero"})
            continue
        q = live_quotes.get(c6) or {}
        live_px = float(q.get("price") or s.get("ref_price") or 0.0)
        pct_chg = float(q["change_pct"]) if q.get("change_pct") is not None else None
        if live_px <= 0:
            skipped.append({"code": c6, "side": "SELL", "reason": "no_live_price"})
            continue
        est_amt = qty * live_px
        is_tail_clear = (qty == full_sh and qty == int(s.get("current_shares") or 0))
        if est_amt < MIN_ORDER_YUAN and not is_tail_clear:
            skipped.append({"code": c6, "side": "SELL", "reason": f"below_min_order_{est_amt:.0f}"})
            continue
        # 跌停保护：封死跌停不发无效卖单
        lim = _price_limit_pct(c6)
        if pct_chg is not None and pct_chg <= -(lim - 0.5):
            skipped.append({"code": c6, "side": "SELL", "reason": f"limit_down_{pct_chg:.2f}%"})
            continue

        # 2026-11-23 补丁①：单日 30% 平稳减仓步长已完成（当日已核销卖出额 >= 总资产 25%）→ 不再同日二次催卖
        total_assets_cur = float(acct.get("total_assets") or 0.0)
        sold_today_amt = today_sold_yuan_by_code.get(c6, 0.0)
        if total_assets_cur > 0 and sold_today_amt >= total_assets_cur * SMOOTH_DAILY_DONE_RATIO:
            skipped.append({"code": c6, "side": "SELL", "reason": f"smooth_step_daily_done_{sold_today_amt:.0f}"})
            continue

        # 2026-11-23 补丁②：极端冰点防地板割肉保护门
        # 当全市场宽度 <= 18% 且拟卖为 A 股非 ETF 个股、且时间早于 14:35 尾盘确认时：
        # 若盘中急跌 <= -2.5%（或明显跌破分时均价线 VWAP 且跌幅 < -1%），禁止早盘恐慌地板割肉，
        # 改等盘中反抽修复或 14:35 尾盘确认！
        # 注意：若已触及冰点 B 档硬止损(-6%)/追踪止盈/时间止损（bypass_panic_floor_guard=True），严禁延迟，必须立即放行！
        is_etf_sell = bool(s.get("is_etf") or _is_etf(c6))
        bypass_floor_guard = bool(s.get("bypass_panic_floor_guard"))
        vwap_px = float(q["vwap"]) if q.get("vwap") else 0.0
        if in_panic_zone and not is_etf_sell and not bypass_floor_guard and now.time() < PANIC_TAIL_CONFIRM_TIME:
            is_sharp_drop = pct_chg is not None and pct_chg <= PANIC_SHARP_DROP_PCT
            is_weak_below_vwap = (vwap_px > 0 and live_px < vwap_px * 0.992 and pct_chg is not None and pct_chg < -1.0)
            if is_sharp_drop or is_weak_below_vwap:
                skipped.append({
                    "code": c6,
                    "side": "SELL",
                    "reason": f"panic_zone_no_floor_sell_chg_{pct_chg:.2f}%_wait_rebound_or_1435",
                })
                continue

        key = f"{c6}:SELL"
        # 无论是否处于微信 2 小时冷却或暂停微信推送，只要此刻通过全部盘中门禁，即登记进页面「实时待填单」列表
        executable_orders.append({
            "key": key,
            "code": c6,
            "name": str(s.get("name") or c6),
            "side": "SELL",
            "shares": qty,
            "price": round(live_px, 3 if is_etf_sell else 2),
            "est_amount": round(est_amt, 2),
            "is_etf": is_etf_sell,
            "pct_chg": round(pct_chg, 2) if pct_chg is not None else None,
            "available_shares": int(s.get("available_shares") or 0),
            "current_shares": int(s.get("current_shares") or 0),
            "reason": str(s.get("reason") or "AR4.6 调仓卖出"),
            "triggered_at": now_str,
        })

        # 用可用股数作为指纹：避免价格 1 厘波动导致 ±100 股变化而绕过 2 小时防刷屏冷却；
        # 仅当真实持仓/可卖股数发生变化（如人工成交部分卖出）时立即解除冷却。
        fp = f"SELL:avail{int(s.get('available_shares') or 0)}"
        ok_push, why = _can_push(key, fp)
        if not ok_push:
            skipped.append({"code": c6, "side": "SELL", "reason": why})
            continue
        if not allow_wechat_order_push and not dry_run:
            skipped.append({"code": c6, "side": "SELL", "reason": "wechat_push_paused_page_only"})
            continue

        title, body = _format_sell_wechat(s, plan, live_px, pct_chg, now_str)
        res = {"ok": True, "detail": "dry_run"} if dry_run else send_wechat_notification(title, body)
        if res.get("ok"):
            if not dry_run:
                prev_cnt = int((sent_map.get(key) or {}).get("count") or 0)
                sent_map[key] = {"last_ts": now_epoch, "last_at": now_str, "count": prev_cnt + 1, "fingerprint": fp}
            pushed.append({"code": c6, "name": s["name"], "side": "SELL", "shares": qty, "price": live_px, "title": title, "body": body, "notify": res})
        else:
            skipped.append({"code": c6, "side": "SELL", "reason": f"notify_failed:{res.get('detail')}"})

    # 5B. 再审买入单（仅在买窗内、现金立即可买 >= 2500 元、且严守 AR4.6 总闸门！）
    in_buy_window = bypass_session or _in_intraday_session(now, for_buy=True)
    for b in plan.get("buy_orders") or []:
        c6 = str(b["code"]).zfill(6)
        qty = int(b.get("immediate_shares") or 0)
        is_etf = bool(b.get("is_etf") or b.get("asset_kind") == "etf" or _is_etf(c6))
        if not in_buy_window:
            skipped.append({"code": c6, "side": "BUY", "reason": "after_1445_buy_cutoff"})
            continue
        # AR4.6 终极红线：非进攻期绝对禁止推送任何 A 股个股买单
        if not is_offensive and not is_etf:
            skipped.append({"code": c6, "side": "BUY", "reason": "ar45_defensive_block_a_stock"})
            continue
        # AI 排雷严格版硬门禁：个股买单红灯 / 黄灯≤2 / ST / 近12月刚摘帽禁止入待填单与推微信
        if not is_etf:
            from services.g3.ai_screening import evaluate_buy_risk_gate

            ai_gate = evaluate_buy_risk_gate(c6, name=str(b.get("name") or c6), is_etf=False, now_bj=now)
            if ai_gate["hard_blocked"]:
                skipped.append({"code": c6, "side": "BUY", "reason": f"ai_screening_blocked:{ai_gate['reason']}"})
                continue
        if qty < 100:
            skipped.append({"code": c6, "side": "BUY", "reason": "insufficient_cash_wait_sell"})
            continue
        q = live_quotes.get(c6) or {}
        live_px = float(q.get("price") or b.get("ref_price") or 0.0)
        pct_chg = float(q["change_pct"]) if q.get("change_pct") is not None else None
        if live_px <= 0:
            skipped.append({"code": c6, "side": "BUY", "reason": "no_live_price"})
            continue
        est_amt = qty * live_px
        if est_amt < MIN_ORDER_YUAN:
            skipped.append({"code": c6, "side": "BUY", "reason": f"below_min_order_{est_amt:.0f}"})
            continue
        # 涨停保护：不追涨停板
        lim = _price_limit_pct(c6)
        if pct_chg is not None and pct_chg >= (lim - 0.5):
            skipped.append({"code": c6, "side": "BUY", "reason": f"limit_up_{pct_chg:.2f}%"})
            continue
        # 单一权威裁决：严格按策略编译器标出的 entry_rule 校验，绝不私自发明与策略大脑冲突的二次规则
        # - trend_above_ma20（常规防守趋势 ETF）：严格要求现价 >= MA20
        # - panic_rebound_etf（AR2_2A 冰点右侧反抽 159915/512100）：天然超跌在 MA20 下方，豁免 MA20 校验
        entry_rule = str(b.get("entry_rule") or ("panic_rebound_etf" if b.get("is_panic_etf") else "trend_above_ma20"))
        ma20 = etf_ma20_map.get(c6)
        if is_etf and not is_offensive and entry_rule != "panic_rebound_etf" and ma20 and live_px < ma20:
            skipped.append({"code": c6, "side": "BUY", "reason": f"etf_below_ma20_{live_px:.3f}<{ma20:.3f}"})
            continue
        min_buy_px = b.get("min_buy_price")
        if min_buy_px is not None and float(min_buy_px) > 0 and live_px < float(min_buy_px):
            skipped.append({"code": c6, "side": "BUY", "reason": f"below_min_buy_{live_px:.3f}<{float(min_buy_px):.3f}"})
            continue

        key = f"{c6}:BUY"
        # 无论是否处于微信 2 小时冷却或暂停微信推送，只要此刻通过全部盘中门禁，即登记进页面「实时待填单」列表
        executable_orders.append({
            "key": key,
            "code": c6,
            "name": str(b.get("name") or c6),
            "side": "BUY",
            "shares": qty,
            "price": round(live_px, 3 if is_etf else 2),
            "est_amount": round(est_amt, 2),
            "is_etf": is_etf,
            "pct_chg": round(pct_chg, 2) if pct_chg is not None else None,
            "stop_loss_price": b.get("stop_loss_price"),
            "min_buy_price": b.get("min_buy_price"),
            "min_buy_reason": b.get("min_buy_reason"),
            "max_buy_price": b.get("max_buy_price"),
            "max_buy_reason": b.get("max_buy_reason"),
            "target_weight_pct": b.get("target_weight_pct"),
            "reason": str(b.get("reason") or "AR4.6 目标配置建仓"),
            "triggered_at": now_str,
        })

        # 用「可用现金按 2500 元分档」作为指纹：卖出回款后现金跃升会立即允许推送衔接买单，
        # 同时避免价格微幅波动导致 ±100 股变化而绕过 2 小时防刷屏冷却。
        cash_bucket = int(float(acct["available_cash"]) // 2500)
        fp = f"BUY:c{cash_bucket}"
        ok_push, why = _can_push(key, fp)
        if not ok_push:
            skipped.append({"code": c6, "side": "BUY", "reason": why})
            continue
        if not allow_wechat_order_push and not dry_run:
            skipped.append({"code": c6, "side": "BUY", "reason": "wechat_push_paused_page_only"})
            continue

        title, body = _format_buy_wechat(b, plan, live_px, pct_chg, ma20, now_str)
        res = {"ok": True, "detail": "dry_run"} if dry_run else send_wechat_notification(title, body)
        if res.get("ok"):
            if not dry_run:
                prev_cnt = int((sent_map.get(key) or {}).get("count") or 0)
                sent_map[key] = {"last_ts": now_epoch, "last_at": now_str, "count": prev_cnt + 1, "fingerprint": fp}
            pushed.append({"code": c6, "name": b["name"], "side": "BUY", "shares": qty, "price": live_px, "title": title, "body": body, "notify": res})
        else:
            skipped.append({"code": c6, "side": "BUY", "reason": f"notify_failed:{res.get('detail')}"})

    # 5C. 2026-11-23 补丁③：极端冰点右侧反抽雷达（AR2_2A）盘中实时触发播报（当日至多推 1 次；受 pause_ar2_radar_broadcast 与 suppress_wechat 控制）
    if (
        in_panic_zone
        and "PANIC_REBOUND:ALERT" not in sent_map
        and not suppress_wechat
        and not policy.get("pause_ar2_radar_broadcast")
    ):
        try:
            from services.mining.panic_rebound_monitor import evaluate_panic_rebound_signal

            pr = evaluate_panic_rebound_signal(
                market_gate=market_gate,
                etf_quote_fetcher=(lambda _codes: {k: v for k, v in live_quotes.items()}) if quotes_override is not None else None,
                now_bj=now,
            )
            if pr.get("triggered"):
                m = pr.get("metrics") or {}
                avg_chg = m.get("pair_avg_change_pct")
                cyb_chg = m.get("cyb_change_pct")
                zz_chg = m.get("zz1000_change_pct")
                p_title = f"【🎯 acct1·AR2_2A 冰点右侧拐点触发】创业板+中证1000 盘中均涨 {avg_chg:+.2f}%"
                p_body = "\n".join([
                    f"- 账号：`acct1`（{acct['label']} · AR4.6 × AR2_2A 冰点反抽雷达）",
                    f"- 触发依据：全市场宽度 `{breadth_pct:.2f}% ≤ 18%`（极端冰点）且 **创业板 `{cyb_chg:+.2f}%` + 中证1000 `{zz_chg:+.2f}%` = 均涨 `{avg_chg:+.2f}%` ≥ +0.50%**",
                    f"- 历史实证：近 4 年仅触发 13 次（胜率 69.2% · 单笔均 +2.06% · 最大回撤 3.7%）",
                    "- 操作纪律：① 如手里还有待清退弱势旧仓，可借情绪修复反抽逢高减仓，**切忌地板割肉后又追高买回单票**；② 如需博弈短期情绪反抽，仅限高流动性宽基 ETF（`159915` 创业板ETF + `512100` 中证1000ETF，止损 -6% / 止盈 +12% / 持 5 日或宽度 ≥ 38% 离场）。",
                    f"- 时间：`{now_str}`｜当前可用现金：`{float(acct['available_cash']):,.2f}` 元",
                ])
                res = {"ok": True, "detail": "dry_run"} if dry_run else send_wechat_notification(p_title, p_body)
                if res.get("ok"):
                    if not dry_run:
                        sent_map["PANIC_REBOUND:ALERT"] = {"last_ts": now_epoch, "last_at": now_str, "count": 1, "fingerprint": f"PR:{trade_date}"}
                    pushed.append({"code": "AR2_2A", "name": "冰点右侧反抽拐点", "side": "ALERT", "shares": 0, "price": 0.0, "title": p_title, "body": p_body, "notify": res})
        except Exception as exc:  # noqa: BLE001
            logger.debug("[acct1_advisor] 冰点右侧雷达检测跳过: %s", exc)

    # 5D. 落盘当日实时可执行待填单队列（供 /g3/desk 一键预填）+ 写入 10 分钟页面盘面播报（绝不推微信）
    live_payload = {
        "date": trade_date,
        "updated_at": now_str,
        "regime": plan["regime"],
        "regime_label": plan["regime_label"],
        "breadth_pct": round(breadth_pct, 2) if breadth_pct is not None else None,
        "available_cash": round(float(acct["available_cash"]), 2),
        "total_assets": round(float(acct["total_assets"]), 2),
        "orders": executable_orders,
        "skipped_summary": skipped[:12],
    }
    ai_broadcast_job_id: Optional[str] = None
    if not dry_run:
        _save_json(_live_signals_path(trade_date, base_dir), live_payload)
        etf_targets = [
            f"{t.get('name')}({t.get('code')}) {t.get('account_weight_pct', t.get('weight_pct', 0))}%"
            for t in ((radar.get("etf_radar") or {}).get("targets") or [])
            if isinstance(t, Mapping)
        ]
        order_briefs = [
            f"{'卖出' if o['side'] == 'SELL' else '买入'} {o['name']}({o['code']}) {o['shares']}股 @ ¥{o['price']}"
            for o in executable_orders
        ]
        b_txt = f"{breadth_pct:.2f}%" if breadth_pct is not None else "—"
        headline = (
            f"{now.strftime('%H:%M')} 盘面巡检 · {plan['regime_label']}（宽度 {b_txt}）"
            + (f" · 触发 {len(executable_orders)} 笔待填单" if executable_orders else " · 暂无触发单，稳守观望")
        )
        body_lines = [
            f"• 大盘与门禁：{plan['regime_label']}（全市场站上 MA20 宽度 {b_txt}，{'进攻期放行个股' if is_offensive else '非进攻期个股硬锁 0%'}）",
            f"• AR4.6 目标组合：{' + '.join(etf_targets) if etf_targets else '现金防守'}",
            f"• acct1 账户现状：总资产 ¥{float(acct['total_assets']):,.0f}｜可用现金 ¥{float(acct['available_cash']):,.0f}｜仓位 {acct.get('position_pct', '—')}%",
            f"• 本轮可执行指令：{'；'.join(order_briefs) if order_briefs else '无新增可执行单（等待盘口/门禁触发）'}",
        ]
        append_intraday_broadcast(
            {
                "id": f"rule_{now.strftime('%Y%m%d_%H%M%S')}",
                "ts": now_str,
                "slot": now.strftime("%Y-%m-%d %H:%M"),
                "channel": "rule_snapshot",
                "channel_label": "10分钟规则引擎快照",
                "decision": "action" if executable_orders else "advisory",
                "regime": plan["regime"],
                "regime_label": plan["regime_label"],
                "breadth_pct": round(breadth_pct, 2) if breadth_pct is not None else None,
                "headline": headline,
                "body": "\n".join(body_lines),
                "risk_flags": ["极端冰点区（禁止早盘地板割肉）"] if in_panic_zone else [],
                "actions": order_briefs,
                "executable_count": len(executable_orders),
            },
            trade_date=trade_date,
            base_dir=base_dir,
        )
        # 若 Antigravity 在线监听处于活跃状态，异步派发一条 intraday_broadcast 给 AI Bus 生成深度盘面总结（绝不推微信、不阻塞；隔离单测传入 base_dir 时不污染生产 AI Bus）
        if base_dir is None:
            try:
                from services.g3.ai_bus import is_session_online, submit_ai_job

                ai_online, _ = is_session_online()
                if ai_online:
                    ai_prompt = (
                        f"【10分钟定时盘面播报（仅展示在 /g3/ai-live，禁止推微信）】"
                        f"请基于 {now_str} 实时数据（大盘：{plan['regime_label']}，宽度：{b_txt}，"
                        f"AR4.6目标：{'+'.join(etf_targets) or '现金防守'}，"
                        f"acct1可用现金 ¥{float(acct['available_cash']):,.0f}，本轮待执行单：{len(executable_orders)} 笔），"
                        "给出 1 句核心盘面定调（写进 summary）+ 3~4 条简明盘面/资金/纪律要点（写进 answer）。"
                    )
                    job_res = submit_ai_job(
                        kind="intraday_broadcast",
                        prompt=ai_prompt,
                        context={
                            "ts": now_str,
                            "regime": plan["regime"],
                            "regime_label": plan["regime_label"],
                            "breadth_pct": breadth_pct,
                            "etf_targets": etf_targets,
                            "executable_orders": executable_orders,
                            "available_cash": acct["available_cash"],
                            "total_assets": acct["total_assets"],
                            "no_wechat": True,
                        },
                        source="intraday_10m_cron",
                        wait_timeout_sec=0.0,
                    )
                    ai_broadcast_job_id = job_res.get("job_id")
            except Exception as exc:  # noqa: BLE001
                logger.debug("[acct1_advisor] 派发 AI 盘面播报跳过: %s", exc)

    if not dry_run and pushed:
        state["date"] = trade_date
        state["updated_at"] = now_str
        _save_json(st_path, state)

    return {
        "status": "ok",
        "mode": "intraday",
        "ts": now_str,
        "regime": plan["regime"],
        "regime_label": plan["regime_label"],
        "available_cash": acct["available_cash"],
        "applied_manual_fills": len(applied_fills),
        "executable_count": len(executable_orders),
        "executable_orders": executable_orders,
        "pushed_count": len(pushed),
        "pushed": pushed,
        "skipped": skipped,
        "ai_broadcast_job_id": ai_broadcast_job_id,
    }


def get_acct1_advisor_status(
    *,
    now_bj: Optional[datetime] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    now = now_bj or datetime.now(BJ_TZ)
    trade_date = now.strftime("%Y-%m-%d")
    state = _load_json(_state_path(trade_date, base_dir))
    fills = load_manual_fills(trade_date, base_dir=base_dir)
    live_signals = load_live_signals(trade_date, base_dir=base_dir)
    return {
        "account_id": ACCOUNT_ID,
        "date": trade_date,
        "in_sell_session": _in_intraday_session(now, for_buy=False),
        "in_buy_session": _in_intraday_session(now, for_buy=True),
        "last_poll_at": live_signals.get("updated_at"),
        "live_signals": live_signals,
        "sent_today": state.get("sent") or {},
        "manual_fills_today": fills,
        "push_policy": load_push_policy(base_dir),
        "schedule": {
            "premarket": "mon-fri 09:15 (Asia/Shanghai)",
            "intraday": "mon-fri 09:35~11:25 & 13:05~14:45 每10分钟 (Asia/Shanghai)",
            "eod": "mon-fri 15:15 (Asia/Shanghai)",
        },
    }
