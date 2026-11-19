# -*- coding: utf-8 -*-
"""R40 影子做T引擎（observe_only，零真实交易零 formal 写）。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261110_shadow_ttrade_r40.md
规格来源：R39 报告 §五/§六（runtime/ttrade_r39/REPORT.md）修正式 —— Owner 已批观察验证。

判别式（R40 定稿，R39 已测格 g1%/rsi[55,80)/vd0.5% + 直线度 down≤3）：
  卖侧 vertical_ramp_distribution 触发 =
    横盘上沿态（T-1 严格式 regime=sideways ∧ pos20≥0.65；下降态硬排除）
    ∧ gain30 ≥ +1.0%（6 根 5m 涨幅，允许跨日）
    ∧ down30 ≤ 3（30min 段内下行 bar 数，直线度）
    ∧ RSI5 ∈ [55, 80)（Wilder RSI14，120 根收窗）
    ∧ VWAP 乖离 ≥ +0.5%（当日累计 VWAP，含当前已收 bar）
  px = 信号 bar 收盘价（不用快照，回放确定性）。

物理隔离（任务书红线）：
  - 影子台账只写 runtime/t_tracker/shadow/ 目录；
  - 本模块零调用 drafts/pending_push/SignalState —— 影子草案绝不进 pending_push、
    绝不微信推送、绝不影响真草案；
  - t_trade_tracker.py --beat 内嵌调用但独立 try/except：影子失败不影响真节拍。

R46（2026-11-11）影子同步 1 分钟快车道（observe_only 不变）：
  - shadow_fast_tick：实时价刷新 5m 末根重估 intrabar 特征 → fast_vertical_ramp_
    distribution 信号（bar_status=open、intrabar_rsi5 实值），同 armed/30min 冷却
    （与慢车道 shadow_beat 共用同一日台账 meta）；
  - shadow_beat 收线复核：open 的 fast 信号以原形成中 bar 收线特征复核——成立
    → confirmed_bar（虚拟成交/结局跟踪照常），瓦解 → expired_unconfirmed（留档
    统计，不虚拟成交不进判定）；同波去重并入（不另发新信号）。

虚拟成交与结局跟踪：
  - 成交价 = 信号 bar 下一根已收 bar 的开盘价；量 = 底仓 1/3 向下取整百
    （t_lot_cap；<300 股 → 0，改用纯比率口径并标记）；
  - 结局 h30/h60/h120 = 成交 bar 起 +30/60/120min，取同日第一根 ts≥目标 的已收
    bar 收盘价（跨午休/尾盘自然顺延，同日无 bar → null）；close = 当日 15:00 bar；
  - 判定基准（滚动统计口径）= h120 净%；h120=null → close 净%（close_fallback）；
  - 费用 = 引擎 reconcile 同款（卖 0.0666%+买 0.0186%，买佣 min 5 元）；
  - 通过标准：4 周（28 自然日）滚动 wr≥60% ∧ avg≥+0.3%（R39 Phase C）。
"""
from __future__ import annotations

__version__ = "1.1.0"

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from services import t_trade_signal as tts

SCHEMA_SHADOW_DAY = "t_tracker_shadow_day/1"
SCHEMA_SHADOW_SIGNAL = "t_tracker_shadow_signal/1"
SCHEMA_SHADOW_SUMMARY = "t_tracker_shadow_summary/1"
TRIGGER_KIND = "vertical_ramp_distribution"

# ── 判别式参数定稿（R39 §五格 + R40 回归锚 2026-11-10 09:40 bar 实测过门）──
GAIN30_MIN = 0.010          # 30min（6 根 5m）涨幅 ≥ +1.0%
DOWN30_MAX = 3              # 段内下行 bar ≤ 3（直线度）
RSI5_LO, RSI5_HI = 55.0, 80.0
VWAP_DEV_MIN = 0.005        # VWAP 乖离 ≥ +0.5%
POS20_MIN = 0.65            # 横盘上沿（T-1 严格式 pos20；R39 锚例 0.671 需 0.65 门）
RSI_WINDOW_BARS = 120       # RSI 收窗（镜像引擎/监控器）
HORIZONS_MIN = (30, 60, 120)
DAY_CLOSE_TS_SUFFIX = " 15:00"
SHADOW_COOLDOWN_SEC = 30 * 60   # 同票 30min 冷却兜底（镜像真引擎）
# 通过标准（R39 Phase C：side_upper wr≥60% 且 avg≥+0.3% 再议上线）
PASS_WIN_RATE = 0.60
PASS_AVG_PCT = 0.30
ROLLING_WINDOW_DAYS = 28    # 4 周滚动

logger = logging.getLogger("t_trade_shadow")

SIDE_SELL = tts.SIDE_SELL


# ---------------------------------------------------------------------------
# 路径（影子台账物理隔离：shadow/ 独立目录）
# ---------------------------------------------------------------------------

def shadow_dir(root: Path) -> Path:
    return root / "shadow"


def shadow_day_path(root: Path, day: date) -> Path:
    return shadow_dir(root) / f"{day.strftime('%Y%m%d')}.json"


def shadow_summary_path(root: Path, day: date) -> Path:
    return shadow_dir(root) / f"{day.strftime('%Y%m%d')}_summary.json"


# ---------------------------------------------------------------------------
# 特征与判别式（纯函数）
# ---------------------------------------------------------------------------

def compute_features(bars5: Sequence[Mapping[str, Any]], today: date) -> Optional[dict[str, Any]]:
    """四特征 + px（bars5 = 已收 bar 升序；不足返回 None）。"""
    if len(bars5) < 7:
        return None
    closes = [float(b["close"]) for b in bars5]
    rsi5 = tts.rsi_wilder(closes[-RSI_WINDOW_BARS:])
    if rsi5 is None:
        return None
    today_str = today.isoformat()
    bars_today = [b for b in bars5 if str(b["ts"]).startswith(today_str)]
    if not bars_today:
        return None
    vwap = tts.day_vwap(bars_today)
    if not vwap or vwap <= 0:
        return None
    px = closes[-1]
    vwap_dev = (px - vwap) / vwap
    gain30 = px / closes[-7] - 1 if closes[-7] else None
    down30 = sum(1 for i in range(len(closes) - 6, len(closes))
                 if closes[i] < closes[i - 1])
    return {
        "px": px,
        "gain30_pct": round(gain30 * 100, 2) if gain30 is not None else None,
        "down30": down30,
        "rsi5": round(rsi5, 1),
        "vwap": round(vwap, 3),
        "vwap_dev_pct": round(vwap_dev * 100, 2),
        "signal_bar_ts": str(bars5[-1]["ts"]),
    }


def evaluate_gate(regime_info: Mapping[str, Any], feats: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """R40 定稿判别式。返回 (触发?, 按序命中的拒绝原因列表；触发时为空)。"""
    regime = str(regime_info.get("regime") or "")
    pos20 = regime_info.get("pos20")
    reasons: list[str] = []
    if regime == "down":
        reasons.append("down_regime_excluded")
        return False, reasons
    if regime != "sideways":
        reasons.append("not_sideways_regime")
    elif pos20 is None or float(pos20) < POS20_MIN:
        reasons.append("pos20_below_min")
    if feats["gain30_pct"] is None or feats["gain30_pct"] < GAIN30_MIN * 100:
        reasons.append("gain30_below_min")
    if feats["down30"] > DOWN30_MAX:
        reasons.append("ramp_not_straight")
    if not (RSI5_LO <= feats["rsi5"] < RSI5_HI):
        reasons.append("rsi5_out_of_band")
    if feats["vwap_dev_pct"] < VWAP_DEV_MIN * 100:
        reasons.append("vwap_dev_below_min")
    return (not reasons), reasons


def _gate_hot(regime_info: Mapping[str, Any], feats: Mapping[str, Any]) -> bool:
    ok, _ = evaluate_gate(regime_info, feats)
    return ok


# ---------------------------------------------------------------------------
# 台账读写（整档原子重写；与 drafts/pending_push 零交集）
# ---------------------------------------------------------------------------

def _empty_day_doc(day: date) -> dict[str, Any]:
    return {"schema": SCHEMA_SHADOW_DAY, "version": __version__,
            "date": day.isoformat(), "updated_at": "", "signals": [], "meta": {}}


def load_day_doc(root: Path, day: date) -> dict[str, Any]:
    data = tts._load_json(shadow_day_path(root, day))
    if not data or not isinstance(data.get("signals"), list):
        return _empty_day_doc(day)
    data.setdefault("meta", {})
    return data


def save_day_doc(root: Path, day: date, doc: dict[str, Any], now: datetime) -> None:
    doc["updated_at"] = tts.fmt_ts(now)
    tts._atomic_write_json(shadow_day_path(root, day), doc)


# ---------------------------------------------------------------------------
# 结局跟踪（纯函数：给定已收 bar 序列推进单条信号）
# ---------------------------------------------------------------------------

def _outcome_for_sell(fill_px: float, qty: int, px_h: float) -> dict[str, Any]:
    """卖 1/3 @fill → 虚拟买回 @px_h 的回合净盈亏（引擎费率）。"""
    sell_amount = fill_px * qty
    buy_amount = px_h * qty
    gross = (fill_px - px_h) * qty
    fees = tts._fee_estimate("SELL", sell_amount) + tts._fee_estimate("BUY", buy_amount)
    net = gross - fees
    base = sell_amount if sell_amount > 0 else fill_px  # qty=0 时退化为比率口径
    return {
        "px": px_h,
        "gross_pct": round(gross / base * 100, 3) if base else None,
        "fees": round(fees, 2),
        "net_pct": round(net / base * 100, 3) if base else None,
        "win": bool(net > 0) if base else None,
    }


def _advance_signal(sig: dict[str, Any], bars5: Sequence[Mapping[str, Any]],
                    today: date) -> dict[str, Any]:
    """用当前已收 bar 推进信号：虚拟成交 → 各时限结局 → 终结判定。"""
    if sig["status"] == "finalized":
        return sig
    if sig.get("status") == "expired_unconfirmed":
        return sig   # R46：瓦解的 fast 信号不虚拟成交、不进结局跟踪
    today_str = today.isoformat()
    today_bars = [b for b in bars5 if str(b["ts"]).startswith(today_str)]
    day_done = any(str(b["ts"]).endswith(DAY_CLOSE_TS_SUFFIX) or str(b["ts"]) >= f"{today_str} 15:00"
                   for b in today_bars)

    # 1) 虚拟成交：信号 bar 的下一根已收 bar 开盘价
    fill = sig.get("virtual_fill")
    if not fill:
        nxt = [b for b in today_bars if str(b["ts"]) > sig["signal_bar_ts"]]
        if nxt:
            nb = nxt[0]
            qty = int(sig.get("virtual_qty") or 0)
            price = float(nb.get("open") or nb["close"])
            amount = price * qty
            sig["virtual_fill"] = {
                "bar_ts": str(nb["ts"]), "price": price, "qty": qty,
                "amount": round(amount, 2),
                "fee_est": tts._fee_estimate("SELL", amount) if qty else 0.0,
                "qty_note": "t_lot_below_min_ratio_basis" if qty == 0 else "",
            }
            sig["status"] = "tracking"
            fill = sig["virtual_fill"]
        else:
            return sig  # 下一根 bar 未收，等待下一拍

    # 2) 各时限结局：同日第一根 ts ≥ 目标 的已收 bar 收盘价
    fill_ts = tts.parse_ts(fill["bar_ts"])
    outcomes = sig.setdefault("outcomes", {})
    for h in HORIZONS_MIN:
        if outcomes.get(f"h{h}") is not None:
            continue
        target = fill_ts + timedelta(minutes=h)
        later = [b for b in today_bars if tts.parse_ts(str(b["ts"])) >= target]
        if later:
            outcomes[f"h{h}"] = {
                "target_ts": tts.fmt_ts(target),
                "bar_ts": str(later[0]["ts"]),
                **_outcome_for_sell(fill["price"], fill["qty"], float(later[0]["close"])),
            }
        elif day_done:
            outcomes[f"h{h}"] = {"target_ts": tts.fmt_ts(target), "unreachable": True}
    # 3) 当日收盘结局
    if outcomes.get("close") is None and day_done:
        closes = [b for b in today_bars if str(b["ts"]) >= f"{today_str} 14:55"]
        if closes:
            outcomes["close"] = {
                "bar_ts": str(closes[-1]["ts"]),
                **_outcome_for_sell(fill["price"], fill["qty"], float(closes[-1]["close"])),
            }

    # 4) 终结：close 已出且各时限均已解决（含 unreachable）
    horizons_done = all(outcomes.get(f"h{h}") is not None for h in HORIZONS_MIN)
    if outcomes.get("close") is not None and horizons_done:
        h120 = outcomes.get("h120") or {}
        if h120.get("net_pct") is not None:
            sig["judged"] = {"basis": "h120", "net_pct": h120["net_pct"], "win": h120["win"]}
        else:
            sig["judged"] = {"basis": "close_fallback", "net_pct": outcomes["close"]["net_pct"],
                             "win": outcomes["close"]["win"]}
        sig["status"] = "finalized"
    return sig


def _latest_net(sig: Mapping[str, Any]) -> Optional[float]:
    """展示用最新虚拟盈亏：判定基准优先，回退最新已出结局。"""
    if sig.get("judged"):
        return sig["judged"]["net_pct"]
    outcomes = sig.get("outcomes") or {}
    for key in ("h120", "h60", "h30", "close"):
        o = outcomes.get(key)
        if isinstance(o, dict) and o.get("net_pct") is not None:
            return o["net_pct"]
    return None


# ---------------------------------------------------------------------------
# 影子节拍（盘中；与真节拍同数据源但独立调用、独立落盘）
# ---------------------------------------------------------------------------

def shadow_beat(
    *,
    now: datetime,
    out_dir: Optional[Path] = None,
    holdings_loader: Optional[Callable[[], dict]] = None,
    regime_loader: Optional[Callable[[date], Optional[dict]]] = None,
    bars5_loader: Optional[Callable[[str], list[dict]]] = None,
    log: Optional[logging.Logger] = None,
) -> dict:
    """影子一拍：判别式评估 + 台账推进。只写 shadow/，不触碰 drafts/pending_push。"""
    log = log or logger
    root = tts.tracker_dir(out_dir)
    day = now.date()
    load_holdings = holdings_loader or tts.load_broker_holdings
    load_bars5 = bars5_loader or (lambda code: tts.fetch_east_klines(code, 5, lmt=RSI_WINDOW_BARS))

    def _default_regime_loader(d: date) -> Optional[dict]:
        return tts._load_json(tts.regime_file_path(root, d))

    load_regime = regime_loader or _default_regime_loader
    base = {"schema": "t_tracker_shadow_beat/1", "version": __version__, "ts": tts.fmt_ts(now)}

    try:
        holdings = load_holdings()
    except Exception as exc:  # noqa: BLE001 - fail-closed：持仓不可读 → 本拍放弃
        log.error("[shadow] 持仓加载失败：%s", exc)
        return {**base, "status": "failure", "reason": f"holdings_unreadable: {exc}"}
    regime = load_regime(day)
    if not regime or not isinstance(regime.get("codes"), dict):
        log.error("[shadow] %s 周期文件缺失/不可读，本拍 fail-closed", day)
        return {**base, "status": "failure", "reason": "regime_file_missing"}

    doc = load_day_doc(root, day)
    signals: list[dict] = doc["signals"]
    meta = doc["meta"]
    new_ids: list[str] = []
    eval_log: list[dict] = doc.setdefault("eval_log", [])

    # R46：请求内 memo（评估/结算/结局推进共用一次装载）
    memo5: dict[str, list[dict]] = {}

    def _bars5_closed(code: str) -> list[dict]:
        if code not in memo5:
            memo5[code] = tts.closed_bars(load_bars5(code), now)
        return memo5[code]

    # R46 收线复核：open 的 fast 信号以原形成中 bar 收线特征结算
    settled_ids = _settle_open_fast_signals(
        signals=signals, regime=regime, now=now, day=day, bars5_loader=_bars5_closed,
        eval_log=eval_log, log=log)

    for holding in holdings["holdings"]:
        code = str(holding["code"]).split(".")[0]
        info = regime["codes"].get(code)
        if not info:
            continue
        try:
            bars5 = _bars5_closed(code)
            feats = compute_features(bars5, day)
            if feats is None:
                eval_log.append({"ts": tts.fmt_ts(now), "code": code, "decision": "skip",
                                 "reason": "insufficient_bars"})
                continue
            ok, reasons = evaluate_gate(info, feats)
            m = meta.setdefault(code, {"armed": True, "last_fire": None})
            if not ok:
                m["armed"] = True   # 条件离开 → 重布防（镜像真引擎语义）
                eval_log.append({"ts": tts.fmt_ts(now), "code": code, "decision": "suppressed",
                                 "reasons": reasons,
                                 "features": {k: feats[k] for k in
                                              ("gain30_pct", "down30", "rsi5", "vwap_dev_pct")}})
                continue
            if not m["armed"]:
                eval_log.append({"ts": tts.fmt_ts(now), "code": code, "decision": "suppressed",
                                 "reasons": ["disarmed_wait_rearm"]})
                continue
            if m.get("last_fire"):
                last = tts.parse_ts(str(m["last_fire"]))
                if last and (now - last).total_seconds() < SHADOW_COOLDOWN_SEC:
                    eval_log.append({"ts": tts.fmt_ts(now), "code": code, "decision": "suppressed",
                                     "reasons": ["cooldown_30min"]})
                    continue
            base_shares = int(holding.get("shares") or 0)
            qty = tts.t_lot_cap(base_shares)
            # R46 同波去重：近窗 open 的 fast 信号 → 并入升级，不另发
            fast_sig = _mergeable_fast_signal(signals, code, now)
            if fast_sig is not None:
                fast_sig["bar_status"] = "confirmed_bar"
                fast_sig["confirmed_at"] = tts.fmt_ts(now)
                fast_sig["confirm_source"] = "shadow_beat_merge"
                fast_sig["closed_rsi5"] = feats["rsi5"]
                eval_log.append({"ts": tts.fmt_ts(now), "code": code,
                                 "decision": "merged_into_fast",
                                 "signal_id": fast_sig["id"]})
                m["armed"] = False
                m["last_fire"] = tts.fmt_ts(now)
                continue
            sig_id = _make_signal_id(now, code, signals)
            signals.append({
                "schema": SCHEMA_SHADOW_SIGNAL, "id": sig_id,
                "signal_ts": tts.fmt_ts(now), "signal_bar_ts": feats["signal_bar_ts"],
                "code": code, "name": info.get("name") or holding.get("name") or "",
                "side": SIDE_SELL, "trigger_kind": TRIGGER_KIND, "observe_only": True,
                "regime": str(info.get("regime") or ""), "pos20": info.get("pos20"),
                "base_shares": base_shares, "virtual_qty": qty,
                "features": {k: feats[k] for k in
                             ("px", "gain30_pct", "down30", "rsi5", "vwap", "vwap_dev_pct")},
                "virtual_fill": None, "outcomes": {}, "judged": None,
                "status": "awaiting_fill", "updated_at": tts.fmt_ts(now),
            })
            new_ids.append(sig_id)
            m["armed"] = False
            m["last_fire"] = tts.fmt_ts(now)
            log.info("[shadow] %s %s 触发影子卖T信号（observe_only，零推送）",
                     tts.fmt_ts(now), code)
        except Exception as exc:  # noqa: BLE001 - 单票失败如实记录
            eval_log.append({"ts": tts.fmt_ts(now), "code": code, "decision": "skip",
                             "reason": f"error:{type(exc).__name__}: {exc}"})
            log.error("[shadow] %s 评估失败：%s", code, exc)

    # 结局推进（含本拍新信号：若下一根 bar 已收则同拍成交）
    for sig in signals:
        try:
            bars5 = _bars5_closed(str(sig["code"]))
            _advance_signal(sig, bars5, day)
        except Exception as exc:  # noqa: BLE001
            log.error("[shadow] %s 结局推进失败：%s", sig.get("id"), exc)

    save_day_doc(root, day, doc, now)
    eval_log[:] = eval_log[-400:]
    return {**base, "status": "ok", "new_signal_ids": new_ids,
            "signals_total": len(signals),
            "finalized": sum(1 for s in signals if s["status"] == "finalized"),
            "fast_settled": settled_ids}


def _make_signal_id(now: datetime, code: str, signals: Sequence[Mapping[str, Any]]) -> str:
    seq = len([s for s in signals if s.get("code") == code]) + 1
    sid = f"S{now.strftime('%Y%m%d')}_{code}_vrd_{seq:02d}"
    while any(s.get("id") == sid for s in signals):
        seq += 1
        sid = f"S{now.strftime('%Y%m%d')}_{code}_vrd_{seq:02d}"
    return sid


# ---------------------------------------------------------------------------
# R46 影子快车道（1 分钟，observe_only；与 shadow_beat 共用台账/meta/冷却）
# ---------------------------------------------------------------------------

def _settle_open_fast_signals(
    *,
    signals: list[dict],
    regime: Mapping[str, Any],
    now: datetime,
    day: date,
    bars5_loader: Callable[[str], list[dict]],
    eval_log: list[dict],
    log: Optional[logging.Logger] = None,
) -> list[str]:
    """收线复核：open 的 fast 信号以原形成中 bar 收线特征结算（原地改写）。

    成立 → confirmed_bar（虚拟成交/结局跟踪照常推进）；瓦解 →
    expired_unconfirmed（status 同步标记，_advance_signal 跳过：不虚拟成交、
    不进 4 周判定统计，只留档）。bar 尚未收/数据未到 → 保持 open。
    """
    log = log or logger
    settled: list[str] = []
    for sig in signals:
        if sig.get("lane") != "fast" or sig.get("bar_status") != "open":
            continue
        code = str(sig.get("code") or "")
        forming_ts = tts.parse_ts(str(sig.get("forming_bar_ts") or ""))
        info = (regime.get("codes") or {}).get(code)
        if forming_ts is None or info is None or now < forming_ts:
            continue
        try:
            bars5_closed = bars5_loader(code)
        except Exception as exc:  # noqa: BLE001 - 行情失败保留 open 等下一拍
            log.error("[shadow-fast-settle] %s 行情失败，保留 open：%s", sig.get("id"), exc)
            continue
        settled_bar = next((b for b in bars5_closed
                            if (tts.parse_ts(str(b["ts"])) or now) >= forming_ts), None)
        if settled_bar is None:
            continue
        end_ts = tts.parse_ts(str(settled_bar["ts"]))
        closes_up_to = [b for b in bars5_closed
                        if (tts.parse_ts(str(b["ts"])) or now) <= end_ts]
        feats = compute_features(closes_up_to, day)
        if feats is None:
            ok, reasons = False, ["insufficient_bars"]
        else:
            ok, reasons = evaluate_gate(info, feats)
        if ok:
            sig["bar_status"] = "confirmed_bar"
            sig["confirmed_at"] = tts.fmt_ts(now)
            sig["confirm_source"] = "shadow_beat_settle"
            sig["closed_rsi5"] = feats["rsi5"]
            sig["confirm_bar_ts"] = feats["signal_bar_ts"]
        else:
            sig["bar_status"] = "expired_unconfirmed"
            sig["status"] = "expired_unconfirmed"
            sig["expired_at"] = tts.fmt_ts(now)
            sig["expire_reasons"] = reasons
            sig["expire_bar_ts"] = feats["signal_bar_ts"] if feats else None
        settled.append(str(sig.get("id")))
        log.info("[shadow-fast-settle] %s → %s", sig.get("id"), sig["bar_status"])
    return settled


def _mergeable_fast_signal(signals: Sequence[Mapping[str, Any]], code: str,
                           now: datetime) -> Optional[dict]:
    """同波可并入的 open fast 信号：同码、同卖侧、近窗 ≤MERGE_WAVE_SEC。"""
    for s in signals:
        if s.get("lane") != "fast" or s.get("bar_status") != "open":
            continue
        if str(s.get("code")) != code or s.get("side") != SIDE_SELL:
            continue
        s_ts = tts.parse_ts(str(s.get("signal_ts") or ""))
        if s_ts is None or (now - s_ts).total_seconds() > tts.MERGE_WAVE_SEC:
            continue
        return s  # type: ignore[return-value]
    return None


def shadow_fast_tick(
    *,
    now: datetime,
    out_dir: Optional[Path] = None,
    holdings_loader: Optional[Callable[[], dict]] = None,
    regime_loader: Optional[Callable[[date], Optional[dict]]] = None,
    bars5_loader: Optional[Callable[[str], list[dict]]] = None,
    quotes_loader: Optional[Callable[[Sequence[str]], dict]] = None,
    log: Optional[logging.Logger] = None,
) -> dict:
    """影子快车道一拍（R46，1 分钟）：intrabar 特征重估 → fast 影子信号。

    与 shadow_beat 同判别式/同台账/同 armed+30min 冷却（共用 day doc meta）；
    实时价刷新 5m 末根（forming bar）重估四特征。物理隔离不变：只写 shadow/，
    零 drafts/pending_push 触碰。收线确认/瓦解由 shadow_beat 结算。
    """
    log = log or logger
    root = tts.tracker_dir(out_dir)
    day = now.date()
    load_pool = holdings_loader or tts.load_t_pool
    load_bars5 = bars5_loader or (lambda code: tts.cached_east_klines(code, 5))
    load_quotes = quotes_loader or tts.fetch_east_ulist_quotes

    def _default_regime_loader(d: date) -> Optional[dict]:
        return tts._load_json(tts.regime_file_path(root, d))

    load_regime = regime_loader or _default_regime_loader
    base = {"schema": "t_tracker_shadow_fast_beat/1", "version": __version__,
            "ts": tts.fmt_ts(now)}

    try:
        pool = load_pool()
    except Exception as exc:  # noqa: BLE001 - fail-closed：持仓不可读 → 本拍放弃
        log.error("[shadow-fast] 持仓加载失败：%s", exc)
        return {**base, "status": "failure", "reason": f"holdings_unreadable: {exc}"}
    regime = load_regime(day)
    if not regime or not isinstance(regime.get("codes"), dict):
        log.error("[shadow-fast] %s 周期文件缺失/不可读，本拍 fail-closed", day)
        return {**base, "status": "failure", "reason": "regime_file_missing"}

    doc = load_day_doc(root, day)
    signals: list[dict] = doc["signals"]
    meta = doc["meta"]
    new_ids: list[str] = []
    eval_log: list[dict] = doc.setdefault("eval_log", [])

    pool_codes = [str(h.get("code") or "").split(".")[0] for h in pool["holdings"]]
    try:
        quotes = load_quotes(pool_codes) or {}
    except Exception as exc:  # noqa: BLE001 - 实时价失败 → 本拍降级收线特征
        log.warning("[shadow-fast] ulist 失败，本拍退化为收线特征：%s", exc)
        quotes = {}

    for holding in pool["holdings"]:
        code = str(holding["code"]).split(".")[0]
        info = regime["codes"].get(code)
        if not info:
            continue
        try:
            raw5 = load_bars5(code)
            bars5_closed = tts.closed_bars(raw5, now)
            px = None
            q = quotes.get(code)
            if q and q.get("price"):
                px = float(q["price"])
            elif bars5_closed:
                px = float(bars5_closed[-1]["close"])
            if px is None:
                eval_log.append({"ts": tts.fmt_ts(now), "code": code,
                                 "decision": "skip", "reason": "no_price"})
                continue
            bars5_fast, forming_ts = tts.build_intrabar_bars5(raw5, px, now)
            feats = compute_features(bars5_fast, day)
            if feats is None:
                eval_log.append({"ts": tts.fmt_ts(now), "code": code,
                                 "decision": "skip", "reason": "insufficient_bars"})
                continue
            ok, reasons = evaluate_gate(info, feats)
            m = meta.setdefault(code, {"armed": True, "last_fire": None})
            if not ok:
                m["armed"] = True   # 条件离开 → 重布防（镜像真引擎语义）
                continue
            if not m["armed"]:
                continue
            if m.get("last_fire"):
                last = tts.parse_ts(str(m["last_fire"]))
                if last and (now - last).total_seconds() < SHADOW_COOLDOWN_SEC:
                    continue
            base_shares = int(holding.get("shares") or 0)
            qty = tts.t_lot_cap(base_shares)
            sig_id = _make_signal_id(now, code, signals)
            # 结算锚 = 触发时的末根 bar（形成中优先；退化路径回退触发已收 bar）
            settle_bar_ts = forming_ts or str(bars5_fast[-1]["ts"])
            signals.append({
                "schema": SCHEMA_SHADOW_SIGNAL, "id": sig_id,
                "signal_ts": tts.fmt_ts(now), "signal_bar_ts": settle_bar_ts,
                "code": code, "name": info.get("name") or holding.get("name") or "",
                "side": SIDE_SELL, "trigger_kind": f"fast_{TRIGGER_KIND}",
                "observe_only": True, "lane": "fast", "bar_status": "open",
                "intrabar_rsi5": feats["rsi5"], "forming_bar_ts": settle_bar_ts,
                "regime": str(info.get("regime") or ""), "pos20": info.get("pos20"),
                "base_shares": base_shares, "virtual_qty": qty,
                "features": {k: feats[k] for k in
                             ("px", "gain30_pct", "down30", "rsi5", "vwap", "vwap_dev_pct")},
                "virtual_fill": None, "outcomes": {}, "judged": None,
                "status": "awaiting_fill", "updated_at": tts.fmt_ts(now),
            })
            new_ids.append(sig_id)
            m["armed"] = False
            m["last_fire"] = tts.fmt_ts(now)
            log.info("[shadow-fast] %s %s fast 影子卖T信号（observe_only，零推送）",
                     tts.fmt_ts(now), code)
        except Exception as exc:  # noqa: BLE001 - 单票失败如实记录
            eval_log.append({"ts": tts.fmt_ts(now), "code": code, "decision": "skip",
                             "reason": f"error:{type(exc).__name__}: {exc}"})
            log.error("[shadow-fast] %s 评估失败：%s", code, exc)

    save_day_doc(root, day, doc, now)
    eval_log[:] = eval_log[-400:]
    return {**base, "status": "ok", "new_signal_ids": new_ids,
            "signals_total": len(signals)}


# ---------------------------------------------------------------------------
# 统计（判定基准 = h120 净%；null → close_fallback）与 4 周滚动
# ---------------------------------------------------------------------------

def judged_records(signals: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [s for s in signals if (s.get("judged") or {}).get("net_pct") is not None]


def _stats(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_net_pct": None}
    nets = [float(r["judged"]["net_pct"]) for r in records]
    wins = sum(1 for v in nets if v > 0)
    return {"n": len(nets), "wins": wins, "losses": len(nets) - wins,
            "win_rate": round(wins / len(nets), 4),
            "avg_net_pct": round(sum(nets) / len(nets), 3)}


def _pass_progress(stats: Mapping[str, Any]) -> dict[str, Any]:
    wr = stats.get("win_rate")
    avg = stats.get("avg_net_pct")
    wr_pass = None if wr is None else wr >= PASS_WIN_RATE
    avg_pass = None if avg is None else avg >= PASS_AVG_PCT
    return {
        "win_rate_min": PASS_WIN_RATE, "avg_net_pct_min": PASS_AVG_PCT,
        "win_rate_pass": wr_pass, "avg_net_pct_pass": avg_pass,
        "overall_pass": None if wr_pass is None else bool(wr_pass and avg_pass),
        "win_rate_progress_pct": None if wr is None else round(min(wr / PASS_WIN_RATE, 9.99) * 100, 1),
        "avg_progress_pct": None if avg is None or avg <= 0 else round(min(avg / PASS_AVG_PCT, 9.99) * 100, 1),
    }


def rolling_4w(root: Path, as_of: date) -> dict[str, Any]:
    """4 周（28 自然日）滚动判定统计：聚合窗口内各日台账的判定记录。

    只认 YYYYMMDD.json 日台账文件（排除 *_summary.json / *_replay_report.json 等伴生件）。
    """
    window_start = as_of - timedelta(days=ROLLING_WINDOW_DAYS)
    records: list[dict[str, Any]] = []
    days: list[str] = []
    shadow = shadow_dir(root)
    if shadow.is_dir():
        for path in sorted(shadow.glob("*.json")):
            if not re.fullmatch(r"\d{8}", path.stem):
                continue
            try:
                d = date.fromisoformat(path.stem[:4] + "-" + path.stem[4:6] + "-" + path.stem[6:8])
            except ValueError:
                continue
            if not (window_start <= d <= as_of):
                continue
            doc = tts._load_json(path) or {}
            recs = judged_records(doc.get("signals") or [])
            records.extend(dict(r, date=d.isoformat()) for r in recs)
            days.append(d.isoformat())
    by_code: dict[str, list] = {}
    for r in records:
        by_code.setdefault(str(r.get("code")), []).append(r)
    return {
        "window_days": ROLLING_WINDOW_DAYS,
        "from": window_start.isoformat(), "to": as_of.isoformat(),
        "days_with_data": sorted(days),
        "stats": _stats(records),
        "per_code": {c: _stats(v) for c, v in sorted(by_code.items())},
        "progress": _pass_progress(_stats(records)),
    }


def finalize_shadow_day(
    *,
    for_date: date,
    now: datetime,
    out_dir: Optional[Path] = None,
    holdings_loader: Optional[Callable[[], dict]] = None,
    regime_loader: Optional[Callable[[date], Optional[dict]]] = None,
    bars5_loader: Optional[Callable[[str], list[dict]]] = None,
    log: Optional[logging.Logger] = None,
) -> dict:
    """日终固化：收盘后最后一拍（全日 bar 已收）→ 当日 summary。"""
    log = log or logger
    root = tts.tracker_dir(out_dir)
    beat = shadow_beat(now=datetime.combine(for_date, datetime.min.time()).replace(hour=15, minute=5),
                       out_dir=out_dir, holdings_loader=holdings_loader,
                       regime_loader=regime_loader, bars5_loader=bars5_loader, log=log)
    doc = load_day_doc(root, for_date)
    signals = doc.get("signals") or []
    judged = judged_records(signals)
    close_only = [s for s in signals
                  if (s.get("judged") or {}).get("basis") == "close_fallback"]
    # R46 影子快车道统计（expired 不虚拟成交不进判定，单列留档）
    fast_lane = {
        "signals": sum(1 for s in signals if s.get("lane") == "fast"),
        "confirmed_bar": sum(1 for s in signals if s.get("bar_status") == "confirmed_bar"),
        "expired_unconfirmed": sum(1 for s in signals if s.get("bar_status") == "expired_unconfirmed"),
        "open_unresolved": sum(1 for s in signals
                               if s.get("lane") == "fast" and s.get("bar_status") == "open"),
    }
    fast_settled_n = fast_lane["confirmed_bar"] + fast_lane["expired_unconfirmed"]
    fast_lane["hit_rate"] = (round(fast_lane["confirmed_bar"] / fast_settled_n, 4)
                             if fast_settled_n else None)
    summary = {
        "schema": SCHEMA_SHADOW_SUMMARY, "version": __version__,
        "date": for_date.isoformat(), "generated_at": tts.fmt_ts(now),
        "beat_status": beat.get("status"),
        "signals_total": len(signals),
        "finalized": sum(1 for s in signals if s.get("status") == "finalized"),
        "tracking": sum(1 for s in signals if s.get("status") == "tracking"),
        "judged_stats": _stats(judged),
        "close_fallback_count": len(close_only),
        "judged_basis": "h120_net_pct(close_fallback_when_unreachable)",
        "fast_lane": fast_lane,
        "params": shadow_params_snapshot(),
        "rolling_4w": rolling_4w(root, for_date),
        "signal_ids": [s.get("id") for s in signals],
    }
    tts._atomic_write_json(shadow_summary_path(root, for_date), summary)
    log.info("[shadow] %s 日终固化：%d 信号 %d 终结 判定 wr=%s avg=%s",
             for_date, len(signals), summary["finalized"],
             summary["judged_stats"]["win_rate"], summary["judged_stats"]["avg_net_pct"])
    return summary


def shadow_params_snapshot() -> dict[str, Any]:
    return {
        "trigger_kind": TRIGGER_KIND,
        "regime_gate": f"sideways&pos20>={POS20_MIN}(down_excluded)",
        "gain30_min_pct": GAIN30_MIN * 100,
        "down30_max": DOWN30_MAX,
        "rsi5_band": [RSI5_LO, RSI5_HI],  # 左闭右开 [55, 80)
        "vwap_dev_min_pct": VWAP_DEV_MIN * 100,
        "fill": "next_bar_open,qty=base//3//100*100",
        "horizons_min": list(HORIZONS_MIN),
        "judged_basis": "h120_net_pct,close_fallback",
        "pass_standard": {"win_rate_min": PASS_WIN_RATE, "avg_net_pct_min": PASS_AVG_PCT},
        "rolling_window_days": ROLLING_WINDOW_DAYS,
        "observe_only": True,
    }


# ---------------------------------------------------------------------------
# 只读聚合（API 端点用；零写入）
# ---------------------------------------------------------------------------

def read_shadow_review(trade_date: Optional[str] = None) -> dict[str, Any]:
    """读影子台账（触发清单 + 滚动统计 + 通过标准达成度）。纯读。"""
    root = tts.tracker_dir()   # 与影子拍同根（AISTOCK_T_TRACKER_DIR / runtime_path 同源）
    day = date.fromisoformat(trade_date) if trade_date else datetime.now(tts.BEIJING_TZ).date()
    doc = tts._load_json(shadow_day_path(root, day)) or {}
    signals = doc.get("signals") or []
    for s in signals:
        s["latest_net_pct"] = _latest_net(s)
    rolling = rolling_4w(root, day)
    return {
        "date": day.isoformat(),
        "exists": bool(doc),
        "signals": signals,
        "day_stats": _stats(judged_records(signals)),
        "rolling_4w": rolling,
        "params": shadow_params_snapshot(),
        "isolation": {"observe_only": True, "push": "never", "pending_push": "never",
                      "formal_writes": "none", "dir": "runtime/t_tracker/shadow/"},
    }
