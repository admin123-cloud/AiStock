# -*- coding: utf-8 -*-
"""G3 每日做T规划与盘中实时作战辅助引擎。

职责：
1. 盘中实时行情 + 当日分时均价（VWAP）+ 实时 5m/15m Wilder RSI（带短期内存缓存，fail-safe）；
2. 校准价带新鲜度校验与动态派生接管（解决静态校准带因股价跌破止损或大幅偏离而失效的问题）；
3. 市场宽度（breadth）→ 今日做T总纲（防守反T / 震荡双向 / 强势正T）；
4. 单票每日做T作战剧本（策略定调、买卖挂单阶梯、价格标尺坐标、盘中情景预案、预警标签）；
5. 每日规划快照落盘与日内手工做T成交账本（实时追踪未平 T 仓敞口与当日做T盈亏）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from services.trading import t_trade_signal as tts
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger("t_daily_plan")

QUOTE_CACHE_TTL_SEC = 15.0
RSI_CACHE_TTL_SEC = 45.0

_qt_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_rsi_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_PLAN_CACHE_LOCK = threading.Lock()


def _plan_store_dir() -> Path:
    """aistock-backend 可写目录（/aistock/data/runtime/g3_desk_plans），不触碰 t_tracker:ro。"""
    p = runtime_path("g3_desk_plans")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_float(v: Any) -> Optional[float]:
    if v in (None, "", "-"):
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _round2(v: Optional[float]) -> Optional[float]:
    return round(v, 2) if v is not None else None


def _pct_diff(cur: Optional[float], ref: Optional[float]) -> Optional[float]:
    if cur is None or ref is None or ref <= 0:
        return None
    return round((cur - ref) / ref * 100.0, 2)


# ---------------------------------------------------------------------------
# 1. 盘中实时快照（腾讯 qt 批量 + price_cache 兜底）与实时 RSI
# ---------------------------------------------------------------------------

def _qt_symbol(code: str) -> str:
    c = str(code).split(".")[0].zfill(6)
    if c.startswith(("6", "9", "5")):
        return f"sh{c}"
    if c.startswith(("8", "4", "92")):
        return f"bj{c}"
    return f"sz{c}"


def fetch_batch_live_quotes(
    codes: Sequence[str],
    *,
    price_cache: Optional[Mapping[str, Any]] = None,
    force_refresh: bool = False,
    timeout: float = 2.5,
) -> dict[str, dict[str, Any]]:
    """批量拉取实时行情（现价、昨收、今开、高低、涨跌幅、当日 VWAP）。fail-safe。"""
    codes6 = sorted({str(c).split(".")[0].zfill(6) for c in codes if c and str(c).strip()})
    if not codes6:
        return {}

    now_ts = time.time()
    out: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    if not force_refresh:
        with _PLAN_CACHE_LOCK:
            for c in codes6:
                hit = _qt_cache.get(c)
                if hit is not None and (now_ts - hit[0]) < QUOTE_CACHE_TTL_SEC:
                    out[c] = dict(hit[1])
                else:
                    missing.append(c)
    else:
        missing = list(codes6)

    if missing:
        symbols = ",".join(_qt_symbol(c) for c in missing)
        url = f"http://qt.gtimg.cn/q={symbols}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("gbk", errors="replace")
            for line in raw.splitlines():
                line = line.strip()
                if "=" not in line or "~" not in line:
                    continue
                body = line.split("=", 1)[1].strip().strip('"').strip(";")
                parts = body.split("~")
                if len(parts) < 38:
                    continue
                code6 = str(parts[2] or "").zfill(6)
                price = _safe_float(parts[3])
                prev_close = _safe_float(parts[4])
                open_px = _safe_float(parts[5])
                vol_lots = _safe_float(parts[6])      # 手（100股）
                high_px = _safe_float(parts[33])
                low_px = _safe_float(parts[34])
                chg_pct = _safe_float(parts[32])
                amt_wan = _safe_float(parts[37])      # 万元
                ts_raw = str(parts[30] or "")
                ts_fmt = None
                if len(ts_raw) >= 14:
                    ts_fmt = f"{ts_raw[8:10]}:{ts_raw[10:12]}:{ts_raw[12:14]}"

                vwap = None
                if amt_wan and vol_lots and vol_lots > 0:
                    # 成交额(元) / 成交量(股)；科创板 688 的 qt parts[6] 为股
                    shares = vol_lots if code6.startswith("688") else vol_lots * 100.0
                    cand = (amt_wan * 10000.0) / shares
                    if price and 0.7 * price <= cand <= 1.3 * price:
                        vwap = round(cand, 3)

                if price and price > 0:
                    q = {
                        "price": round(price, 3),
                        "prev_close": round(prev_close, 3) if prev_close else None,
                        "open": round(open_px, 3) if open_px else None,
                        "high": round(high_px, 3) if high_px else None,
                        "low": round(low_px, 3) if low_px else None,
                        "change_pct": round(chg_pct, 2) if chg_pct is not None else _pct_diff(price, prev_close),
                        "vwap": vwap,
                        "vwap_dev_pct": _pct_diff(price, vwap),
                        "quote_ts": ts_fmt,
                        "source": "tencent_qt",
                    }
                    with _PLAN_CACHE_LOCK:
                        _qt_cache[code6] = (now_ts, q)
                    out[code6] = dict(q)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[t_daily_plan] 腾讯实时行情拉取失败，将降级使用缓存: %s", exc)

    # 兜底：price_cache.json
    pc_quotes = (price_cache or {}).get("quotes") or {}
    for c in codes6:
        if c in out:
            continue
        pc = pc_quotes.get(c)
        px = _safe_float(pc.get("price")) if isinstance(pc, Mapping) else None
        if px and px > 0:
            out[c] = {
                "price": round(px, 3),
                "prev_close": None,
                "open": None,
                "high": None,
                "low": None,
                "change_pct": None,
                "vwap": None,
                "vwap_dev_pct": None,
                "quote_ts": str(pc.get("ts") or ""),
                "source": "price_cache",
            }
    return out


def fetch_live_rsi_snapshot(
    code: str,
    live_price: Optional[float] = None,
    *,
    force_refresh: bool = False,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """拉取单票实时 5m/15m RSI14 与当日 5m VWAP（带 45s 内存缓存，fail-safe）。"""
    code6 = str(code).split(".")[0].zfill(6)
    now_ts = time.time()
    if not force_refresh:
        with _PLAN_CACHE_LOCK:
            hit = _rsi_cache.get(code6)
            if hit is not None and (now_ts - hit[0]) < RSI_CACHE_TTL_SEC:
                return dict(hit[1])

    result: dict[str, Any] = {
        "rsi5m": None,
        "rsi15m": None,
        "vwap_5m": None,
        "bar_ts": None,
        "source": None,
    }
    try:
        bars5 = tts.cached_east_klines(code6, 5, ttl=45.0, timeout=timeout)
        if bars5:
            closes5 = [float(b["close"]) for b in bars5 if _safe_float(b.get("close")) is not None]
            if live_price and closes5:
                closes5 = closes5[:-1] + [float(live_price)]
            r5 = tts.rsi_wilder(closes5, 14)
            result["rsi5m"] = round(r5, 1) if r5 is not None else None
            result["bar_ts"] = str(bars5[-1].get("ts") or "")
            today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
            today_bars = [b for b in bars5 if str(b.get("ts") or "").startswith(today_str)]
            vwap5 = tts.day_vwap(today_bars) if today_bars else None
            if vwap5:
                result["vwap_5m"] = round(vwap5, 3)
            result["source"] = "kline_5m"

        bars15 = tts.cached_east_klines(code6, 15, ttl=90.0, timeout=timeout)
        if bars15:
            closes15 = [float(b["close"]) for b in bars15 if _safe_float(b.get("close")) is not None]
            if live_price and closes15:
                closes15 = closes15[:-1] + [float(live_price)]
            r15 = tts.rsi_wilder(closes15, 14)
            result["rsi15m"] = round(r15, 1) if r15 is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[t_daily_plan] %s 实时 RSI 拉取失败: %s", code6, exc)

    with _PLAN_CACHE_LOCK:
        _rsi_cache[code6] = (now_ts, result)
    return dict(result)


def fetch_batch_live_rsi_snapshots(
    codes: Sequence[str],
    live_price_map: Optional[Mapping[str, Optional[float]]] = None,
    *,
    force_refresh: bool = False,
    timeout: float = 3.0,
) -> dict[str, dict[str, Any]]:
    """并发批量拉取多只标的实时 5m/15m RSI14 与当日 5m VWAP，避免串行放大网络延迟。"""
    codes6 = sorted({str(c).split(".")[0].zfill(6) for c in codes if c and str(c).strip()})
    if not codes6:
        return {}
    prices = live_price_map or {}
    out: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(codes6)))) as executor:
        futures = {
            c: executor.submit(
                fetch_live_rsi_snapshot,
                c,
                prices.get(c),
                force_refresh=force_refresh,
                timeout=timeout,
            )
            for c in codes6
        }
        for c, fut in futures.items():
            try:
                out[c] = fut.result()
            except Exception:
                out[c] = {"rsi5m": None, "rsi15m": None, "vwap_5m": None, "bar_ts": None, "source": None}
    return out


# ---------------------------------------------------------------------------
# 2. 校准带有效性检验 & 动态价带自愈
# ---------------------------------------------------------------------------

def resolve_effective_params(
    *,
    regime: str,
    raw_params: Mapping[str, Any],
    ma5: Optional[float],
    ma10: Optional[float],
    ma20: Optional[float],
    hi20: Optional[float],
    lo20: Optional[float],
    prev_close: Optional[float],
    live_price: Optional[float],
) -> tuple[dict[str, Any], bool, Optional[str]]:
    """检查原 params 是否因静态校准带过期而错位；若失效则基于最新日线动态派生有效价带。"""
    params = dict(raw_params or {})
    source = str(params.get("source") or "")
    ref_px = live_price or prev_close

    stale = False
    reason: Optional[str] = None

    buy_band = params.get("buy_band")
    stop_px = _safe_float(params.get("stop"))

    if ref_px and source.startswith("R54-calib"):
        if stop_px and ref_px < stop_px * 0.995:
            stale = True
            reason = f"静态校准带({source})已过期：现价 {ref_px:.2f} 已低于旧止损 {stop_px:.2f}，已自动切换今日动态价带"
        elif isinstance(buy_band, (list, tuple)) and len(buy_band) == 2:
            b_lo, b_hi = _safe_float(buy_band[0]), _safe_float(buy_band[1])
            if b_lo and ref_px < b_lo * 0.96:
                stale = True
                reason = f"静态校准带({source})偏离过大：现价 {ref_px:.2f} 低于旧买带下沿 {b_lo:.2f} 超 4%，已切换动态价带"
            elif b_hi and ref_px > b_hi * 1.15:
                stale = True
                reason = f"静态校准带({source})偏离过大：现价 {ref_px:.2f} 高于旧买带上沿 {b_hi:.2f} 超 15%，已切换动态价带"

    if stale or (not buy_band and regime in ("sideways", "up", "down") and ref_px):
        # 基于最新 20 日区间 + 均线 + 参考价派生贴合今日实战的动态价带
        anchor = prev_close or ref_px
        eff_lo20 = min(x for x in (lo20, ref_px, anchor) if x is not None)
        eff_hi20 = max(x for x in (hi20, ref_px, anchor) if x is not None)
        rng = max(eff_hi20 - eff_lo20, anchor * 0.06)

        if regime == "down":
            # 下降周期：以反T为主；卖带锚定 MA5/昨收上方 1.5%~3.5%，接回买带锚定现价下方 2%~4%
            sell_lo = round(min(x for x in [anchor * 1.015, ma5 or anchor * 1.02] if x), 2)
            sell_hi = round(max(sell_lo * 1.018, (ma10 or anchor * 1.038)), 2)
            buy_hi = round(min(ref_px * 0.985, anchor * 0.98), 2)
            buy_lo = round(min(buy_hi * 0.975, eff_lo20), 2)
            dyn_stop = round(min(buy_lo, eff_lo20, ref_px) * 0.985, 2)
            eff = {
                **params,
                "source": "dynamic-down-rebased" if stale else "dynamic-down-derived",
                "buy_band": [buy_lo, buy_hi],
                "sell_band": [sell_lo, sell_hi],
                "buy_rsi5_max": params.get("buy_rsi5_max") or 25.0,
                "sell_rsi5_min": params.get("reduce_rsi5_min") or 65.0,
                "exit_rsi5_min": 65.0,
                "stop": dyn_stop,
            }
            return eff, stale, reason

        if regime == "up":
            # 上升周期：买带贴合 MA5/回踩 -1.5%~-0.5%，卖带 +2.5%~+4.5%
            base = ma5 or anchor
            buy_lo = round(min(base * 0.985, anchor * 0.98), 2)
            buy_hi = round(min(base * 1.002, anchor * 0.995), 2)
            sell_lo = round(max(anchor * 1.025, ref_px * 1.02), 2)
            sell_hi = round(max(anchor * 1.045, sell_lo * 1.02), 2)
            dyn_stop = round(min(ma10 or base * 0.96, buy_lo * 0.98), 2)
            eff = {
                **params,
                "source": "dynamic-up-rebased" if stale else "dynamic-up-derived",
                "buy_band": [buy_lo, buy_hi],
                "sell_band": [sell_lo, sell_hi],
                "buy_rsi5_max": params.get("buy_rsi5_max") or 35.0,
                "sell_rsi5_min": params.get("exit_rsi5_trim_min") or 75.0,
                "exit_rsi5_min": params.get("exit_rsi5_min") or 80.0,
                "stop": dyn_stop,
            }
            return eff, stale, reason

        # sideways 动态重算
        b_lo = round(eff_lo20, 2)
        b_hi = round(min(eff_lo20 + 0.18 * rng, anchor * 0.992), 2)
        if b_hi <= b_lo:
            b_lo = round(anchor * 0.965, 2)
            b_hi = round(anchor * 0.985, 2)
        s_lo = round(max(anchor * 1.02, ma5 or anchor * 1.02), 2)
        s_hi = round(max(s_lo * 1.025, ma10 or eff_hi20 - 0.15 * rng), 2)
        dyn_stop = round(b_lo * 0.985, 2)
        eff = {
            **params,
            "source": "dynamic-sideways-rebased" if stale else "dynamic-sideways-derived",
            "buy_band": [b_lo, b_hi],
            "sell_band": [s_lo, s_hi],
            "buy_rsi5_max": params.get("buy_rsi5_max") or 30.0,
            "sell_rsi5_min": params.get("sell_rsi5_min") or 70.0,
            "exit_rsi5_min": params.get("exit_rsi5_min") or 70.0,
            "stop": dyn_stop,
        }
        return eff, stale, reason

    return params, stale, reason


# ---------------------------------------------------------------------------
# 3. 市场总纲（Market Directive）
# ---------------------------------------------------------------------------

def build_market_directive(breadth: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """将市场宽度指标转化为交易员今日做T总纲。"""
    b = dict(breadth or {})
    ma20_pct = _safe_float(b.get("above_ma20_pct"))
    ma5_pct = _safe_float(b.get("above_ma5_pct"))
    nh = b.get("new_high_60d")
    nl = b.get("new_low_60d")
    hl_ratio = _safe_float(b.get("hl_ratio"))
    level = str(b.get("level") or "unknown").lower()

    if level == "weak" or (ma20_pct is not None and ma20_pct < 25.0):
        mode = "defensive_reverse_t"
        mode_label = "防守模式 · 优先反T"
        tone = "red"
        summary = "全市场宽度处于弱势/冰点区，日内严控低吸抄底（防正T变补仓套牢），优先逢冲高先卖后买做反T降本。"
        rules = [
            "反T优先：拉升至卖1或分时均线上方 +1.5% 且量能不继时，先卖 T 仓，回踩接回",
            "正T从严：仅在强支撑 + RSI5m 超卖且未破止损时轻仓试探，绝不逆势硬接飞刀",
            "尾盘纪律：14:50 前务必平掉当日 T 仓敞口，不留隔夜加仓风险",
        ]
    elif level == "strong" or (ma20_pct is not None and ma20_pct >= 55.0):
        mode = "offensive_forward_t"
        mode_label = "强势模式 · 正T优先"
        tone = "emerald"
        summary = "市场宽度走强，承接较好；可优先利用盘中急跌回踩买带或均线做正T（先买后卖），拉升分批高抛。"
        rules = [
            "正T优先：急跌触及买带且 RSI5m ≤ 35 时分批低吸，反抽至卖带或 RSI5m ≥ 75 兑现",
            "卖晚一点：强势票避免在刚突破分时均线时过早做反T卖飞底仓",
            "止损兜底：单票有效跌破止损线立即取消低吸计划",
        ]
    else:
        mode = "balanced_both"
        mode_label = "震荡模式 · 双向做T"
        tone = "amber"
        summary = "市场处于结构震荡期，正反T皆可；严格依托个股 20 日箱体上下沿、分时均价与 RSI5m 极值高抛低吸。"
        rules = [
            "高开冲高做反T：高开或急拉远离分时均线时先卖后买",
            "低开急杀做正T：急杀进入买入价带且 RSI5m ≤ 30 时先买后卖",
            "严格控量：单笔不超过底仓 1/3，日内敞口收盘前归零",
        ]

    return {
        "mode": mode,
        "mode_label": mode_label,
        "tone": tone,
        "level": level,
        "breadth_date": b.get("date"),
        "above_ma5_pct": ma5_pct,
        "above_ma20_pct": ma20_pct,
        "above_ma60_pct": _safe_float(b.get("above_ma60_pct")),
        "new_high_60d": nh,
        "new_low_60d": nl,
        "hl_ratio": hl_ratio,
        "summary": summary,
        "rules": rules,
    }


# ---------------------------------------------------------------------------
# 4. 单票每日做T规划（Playbook + 价格标尺 + 情景剧本）
# ---------------------------------------------------------------------------

def build_stock_daily_plan(
    *,
    code: str,
    name: str,
    regime: str,
    total_shares: int,
    available_shares: int,
    t_lot_cap: int,
    available_cash: Optional[float],
    effective_params: Mapping[str, Any],
    params_stale: bool,
    params_stale_reason: Optional[str],
    live_quote: Mapping[str, Any],
    ma5: Optional[float],
    ma10: Optional[float],
    ma20: Optional[float],
    prev_close: Optional[float],
    market_directive: Mapping[str, Any],
) -> dict[str, Any]:
    """生成单票每日可执行做T规划。"""
    eligible = t_lot_cap >= 100
    cur_px = _safe_float(live_quote.get("price")) or prev_close
    vwap = _safe_float(live_quote.get("vwap"))
    rsi5 = _safe_float(live_quote.get("rsi5m"))
    rsi15 = _safe_float(live_quote.get("rsi15m"))

    buy_band = effective_params.get("buy_band")
    sell_band = effective_params.get("sell_band")
    stop_px = _safe_float(effective_params.get("stop"))
    buy_rsi_max = _safe_float(effective_params.get("buy_rsi5_max")) or 30.0
    sell_rsi_min = _safe_float(effective_params.get("sell_rsi5_min")) or 70.0

    b_lo = _safe_float(buy_band[0]) if isinstance(buy_band, (list, tuple)) and len(buy_band) == 2 else None
    b_hi = _safe_float(buy_band[1]) if isinstance(buy_band, (list, tuple)) and len(buy_band) == 2 else None
    s_lo = _safe_float(sell_band[0]) if isinstance(sell_band, (list, tuple)) and len(sell_band) == 2 else None
    s_hi = _safe_float(sell_band[1]) if isinstance(sell_band, (list, tuple)) and len(sell_band) == 2 else None

    if not eligible:
        reason = "底仓不足 300 股（单笔 T 上限为 0），不满足做T条件" if total_shares > 0 else "当前无真实持仓（仅观察）"
        return {
            "eligible": False,
            "ineligible_reason": reason,
            "action_mode": "ineligible",
            "action_label": "暂不可做T",
            "action_tone": "zinc",
            "recommended_lot": 0,
            "can_forward_t": False,
            "can_reverse_t": False,
            "cash_note": None,
            "price_ladder": {"buy_levels": [], "sell_levels": [], "stop_loss": stop_px},
            "price_gauge": None,
            "scenarios": [],
            "alerts": [],
            "priority": 99,
        }

    # 计算建议单笔股数与现金/可用股约束
    rev_lot = min(t_lot_cap, (available_shares // 100) * 100)
    can_reverse_t = rev_lot >= 100
    fwd_lot = t_lot_cap
    cash_note = None
    if available_cash is not None and cur_px and cur_px > 0:
        max_cash_lot = int(available_cash // (cur_px * 100)) * 100
        if max_cash_lot < fwd_lot:
            fwd_lot = max_cash_lot
            if fwd_lot < 100:
                cash_note = f"可用现金 ¥{available_cash:,.0f} 不足 1 手，今日仅可做反T（先卖后买）"
            else:
                cash_note = f"受可用现金限制，正T单笔上限降至 {fwd_lot} 股"
    can_forward_t = fwd_lot >= 100

    # 判定今日主策略
    mkt_mode = str(market_directive.get("mode") or "balanced_both")
    below_stop = bool(cur_px and stop_px and cur_px <= stop_px)
    near_stop = bool(cur_px and stop_px and not below_stop and (cur_px - stop_px) / stop_px <= 0.01)

    alerts: list[dict[str, str]] = []
    if params_stale and params_stale_reason:
        alerts.append({"level": "warn", "text": params_stale_reason})

    if below_stop:
        action_mode = "stop_alert"
        action_label = "跌破风控线 · 禁做正T"
        action_tone = "red"
        priority = 1
        alerts.append({"level": "danger", "text": f"现价 {cur_px:.2f} 已低于风控止损线 {stop_px:.2f}，严禁低吸抄底，仅允许反弹减仓/反T"})
    elif regime == "down":
        action_mode = "reverse_t_only"
        action_label = "下降周期 · 只做反T"
        action_tone = "red"
        priority = 2
        alerts.append({"level": "warn", "text": "个股处于下降周期（均线空头），今日禁止先买后卖，仅趁反抽做反T降本"})
    elif mkt_mode == "defensive_reverse_t":
        action_mode = "reverse_t_first"
        action_label = "防守优先 · 先卖后买"
        action_tone = "amber"
        priority = 3
    elif regime == "up":
        action_mode = "forward_t_first"
        action_label = "上升周期 · 正T优先"
        action_tone = "emerald"
        priority = 4
    else:
        action_mode = "both_t"
        action_label = "横盘震荡 · 双向做T"
        action_tone = "blue"
        priority = 5

    if near_stop and cur_px and stop_px:
        dist = (cur_px - stop_px) / stop_px * 100.0
        alerts.append({"level": "danger", "text": f"距止损线 {stop_px:.2f} 仅 {dist:.2f}%，密切盯盘防守"})
        priority = min(priority, 1)

    if rsi5 is not None:
        if rsi5 <= buy_rsi_max:
            alerts.append({"level": "opportunity", "text": f"RSI5m={rsi5:.1f} 已进入超卖触发区（≤{buy_rsi_max:.0f}）"})
        elif rsi5 >= sell_rsi_min:
            alerts.append({"level": "opportunity", "text": f"RSI5m={rsi5:.1f} 已进入超买触发区（≥{sell_rsi_min:.0f}）"})

    if cur_px and vwap:
        dev = (cur_px - vwap) / vwap * 100.0
        if dev >= 1.5:
            alerts.append({"level": "opportunity", "text": f"现价高于分时均线 +{dev:.2f}%，进入反T/高抛观察区"})
        elif dev <= -1.5 and action_mode not in ("stop_alert", "reverse_t_only"):
            alerts.append({"level": "info", "text": f"现价低于分时均线 {dev:.2f}%，关注急杀企稳信号"})

    recommended_lot = rev_lot if action_mode in ("stop_alert", "reverse_t_only", "reverse_t_first") else (fwd_lot if can_forward_t else rev_lot)
    if recommended_lot <= 0:
        recommended_lot = t_lot_cap

    # 构建挂单阶梯
    buy_levels: list[dict[str, Any]] = []
    sell_levels: list[dict[str, Any]] = []
    if b_hi is not None and b_lo is not None:
        buy_levels.append({
            "tier": "买1（轻仓/接回）",
            "price": round(b_hi, 2),
            "lot": recommended_lot if action_mode not in ("stop_alert",) else 0,
            "note": "反T接回首选位" if action_mode in ("reverse_t_only", "reverse_t_first", "stop_alert") else f"买带上沿，配合 RSI5m≤{buy_rsi_max:.0f}",
        })
        buy_levels.append({
            "tier": "买2（强支撑）",
            "price": round(b_lo, 2),
            "lot": recommended_lot if action_mode not in ("stop_alert", "reverse_t_only") else 0,
            "note": "区间下沿支撑，贴近止损严守纪律",
        })
    if s_lo is not None and s_hi is not None:
        sell_levels.append({
            "tier": "卖1（反T先卖/减压）",
            "price": round(s_lo, 2),
            "lot": rev_lot,
            "note": f"反抽阻力位/均线压制，配合 RSI5m≥{sell_rsi_min:.0f}",
        })
        sell_levels.append({
            "tier": "卖2（强阻力高抛）",
            "price": round(s_hi, 2),
            "lot": rev_lot,
            "note": "波段上沿阻力，果断锁定 T 利润",
        })

    # 构建价格标尺（Price Gauge）
    price_gauge = None
    pts = [p for p in (stop_px, b_lo, b_hi, vwap, cur_px, s_lo, s_hi) if p is not None and p > 0]
    if pts and cur_px:
        lo_bound = min(pts)
        hi_bound = max(pts)
        pad = max((hi_bound - lo_bound) * 0.12, cur_px * 0.01)
        g_min = round(lo_bound - pad, 2)
        g_max = round(hi_bound + pad, 2)
        span = max(g_max - g_min, 0.01)

        def _pos(v: Optional[float]) -> Optional[float]:
            if v is None:
                return None
            return round(max(0.0, min(100.0, (v - g_min) / span * 100.0)), 1)

        if stop_px and cur_px <= stop_px:
            zone, zone_label = "below_stop", "已破止损"
        elif b_lo and b_hi and b_lo <= cur_px <= b_hi:
            zone, zone_label = "in_buy_band", "买入价带内"
        elif s_lo and s_hi and s_lo <= cur_px <= s_hi:
            zone, zone_label = "in_sell_band", "卖出价带内"
        elif b_hi and cur_px < b_hi:
            zone, zone_label = "below_buy", "买带下方"
        elif s_lo and cur_px > s_hi:
            zone, zone_label = "above_sell", "卖带上方"
        else:
            zone, zone_label = "neutral", "区间中性带"

        price_gauge = {
            "min_px": g_min,
            "max_px": g_max,
            "current_px": round(cur_px, 2),
            "current_pct": _pos(cur_px),
            "stop_px": _round2(stop_px),
            "stop_pct": _pos(stop_px),
            "vwap_px": _round2(vwap),
            "vwap_pct": _pos(vwap),
            "buy_start_pct": _pos(b_lo),
            "buy_end_pct": _pos(b_hi),
            "sell_start_pct": _pos(s_lo),
            "sell_end_pct": _pos(s_hi),
            "zone": zone,
            "zone_label": zone_label,
            "dist_to_stop_pct": _pct_diff(cur_px, stop_px),
            "dist_to_buy_pct": _pct_diff(cur_px, b_hi),
            "dist_to_sell_pct": _pct_diff(cur_px, s_lo),
        }

    # 构建盘中情景剧本
    scenarios: list[dict[str, str]] = []
    if s_lo is not None and b_hi is not None:
        scenarios.append({
            "tag": "冲高反T剧本",
            "tone": "rose",
            "text": f"若盘中急拉至 {s_lo:.2f}~{s_hi:.2f}（或高于分时均线 +1.5%）且 RSI5m ≥ {sell_rsi_min:.0f}，先卖出 {rev_lot} 股，回踩分时均线或 {b_hi:.2f} 一带接回。",
        })
    if action_mode in ("stop_alert", "reverse_t_only"):
        scenarios.append({
            "tag": "急杀防守剧本",
            "tone": "amber",
            "text": f"当前为{'破位风控' if action_mode == 'stop_alert' else '下降周期'}态，盘中急杀不主动挂正T买单，避免补仓套牢；仅在先卖出 T 仓后按低点分批接回。",
        })
    elif b_lo is not None and b_hi is not None:
        scenarios.append({
            "tag": "急杀正T剧本",
            "tone": "emerald",
            "text": f"若缩量回踩 {b_lo:.2f}~{b_hi:.2f} 且 RSI5m ≤ {buy_rsi_max:.0f}（未破止损 {stop_px:.2f}），可低吸 {fwd_lot if can_forward_t else rev_lot} 股，反抽至分时均线或 {s_lo:.2f} 了结。",
        })
    if stop_px is not None:
        scenarios.append({
            "tag": "风控底线",
            "tone": "zinc",
            "text": f"有效跌破 {stop_px:.2f} 立即取消当日买单；14:50 前清零日内 T 仓敞口。",
        })

    return {
        "eligible": True,
        "ineligible_reason": None,
        "action_mode": action_mode,
        "action_label": action_label,
        "action_tone": action_tone,
        "recommended_lot": recommended_lot,
        "can_forward_t": can_forward_t,
        "can_reverse_t": can_reverse_t,
        "cash_note": cash_note,
        "price_ladder": {
            "buy_levels": buy_levels,
            "sell_levels": sell_levels,
            "stop_loss": _round2(stop_px),
        },
        "price_gauge": price_gauge,
        "scenarios": scenarios,
        "alerts": alerts,
        "priority": priority,
    }


# ---------------------------------------------------------------------------
# 5. 每日规划快照落盘（支持一键生成今日最新规划）
# ---------------------------------------------------------------------------

def _daily_plan_path(trade_date: str) -> Path:
    ymd = trade_date.replace("-", "")
    return _plan_store_dir() / f"plan_{ymd}.json"


def load_saved_daily_plan(trade_date: str) -> Optional[dict[str, Any]]:
    p = _daily_plan_path(trade_date)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def generate_and_save_daily_plan(
    *,
    codes: Sequence[tuple[str, str]],
    trade_date: str,
    fallback_breadth: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """实时从 ClickHouse 日线重算全部监控标的的三周期判定与价带参数，并保存为今日规划快照。"""
    now_bj = datetime.now(BJ_TZ)
    for_day = date.fromisoformat(trade_date)
    code_map: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for code, name in codes:
        c6 = str(code).split(".")[0].zfill(6)
        if not c6 or c6 == "000000":
            continue
        try:
            bars = tts.ch_daily_bars(c6, for_day)
            info = tts.compute_regime(bars)
            info.update({
                "code": c6,
                "name": name or c6,
                "data_rows": len(bars),
                "params": tts.resolve_params(c6, info["regime"], info),
            })
            code_map[c6] = info
        except Exception as exc:  # noqa: BLE001
            errors[c6] = f"{type(exc).__name__}: {exc}"

    breadth: dict[str, Any] = dict(fallback_breadth or {})
    try:
        # 优先刷新最新市场宽度（若 CH 可用）
        fresh_b = tts._regime_breadth_annotation(code_map, None, logger)  # noqa: SLF001
        if isinstance(fresh_b, Mapping) and fresh_b.get("status") == "ok":
            breadth = dict(fresh_b)
    except Exception:  # noqa: BLE001
        pass

    payload = {
        "schema": "g3_daily_plan/1",
        "version": tts.__version__,
        "date": trade_date,
        "generated_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "desk_on_demand",
        "codes": code_map,
        "errors": errors,
        "breadth": breadth,
    }
    p = _daily_plan_path(trade_date)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return payload


# ---------------------------------------------------------------------------
# 6. 当日做T手动执行台账 & 敞口/盈亏统计
# ---------------------------------------------------------------------------

def _exec_log_path(trade_date: str) -> Path:
    ymd = trade_date.replace("-", "")
    return _plan_store_dir() / f"exec_{ymd}.jsonl"


def load_intraday_executions(trade_date: str) -> list[dict[str, Any]]:
    p = _exec_log_path(trade_date)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and not obj.get("deleted"):
                rows.append(obj)
        except ValueError:
            continue
    return rows


def append_t_execution(
    *,
    trade_date: str,
    code: str,
    name: str,
    side: str,
    price: float,
    shares: int,
    order_type: str = "t_trade",
    account_label: str = "",
    note: str = "",
) -> dict[str, Any]:
    """登记一笔当日手工成交。side ∈ {'BUY', 'SELL'}；order_type ∈ {'t_trade', 'base_rebalance'}。"""
    side_u = side.upper().strip()
    if side_u not in ("BUY", "SELL"):
        raise ValueError("side 必须为 BUY 或 SELL")
    if price <= 0 or shares <= 0 or shares % 100 != 0:
        raise ValueError("price 必须 > 0，shares 必须为 100 的正整数倍")
    otype = str(order_type or "t_trade").strip().lower()
    if otype not in ("t_trade", "base_rebalance"):
        raise ValueError("order_type 必须为 t_trade 或 base_rebalance")
    code6 = str(code).split(".")[0].zfill(6)
    entry = {
        "id": uuid.uuid4().hex[:10],
        "trade_date": trade_date,
        "ts": datetime.now(BJ_TZ).strftime("%H:%M:%S"),
        "code": code6,
        "name": name or code6,
        "side": side_u,
        "order_type": otype,
        "price": round(float(price), 3),
        "shares": int(shares),
        "amount": round(float(price) * int(shares), 2),
        "account_label": account_label,
        "note": note.strip(),
    }
    p = _exec_log_path(trade_date)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def delete_t_execution(trade_date: str, exec_id: str) -> bool:
    """删除/撤销一条当日误记的做T记录。"""
    p = _exec_log_path(trade_date)
    if not p.exists():
        return False
    rows = load_intraday_executions(trade_date)
    kept = [r for r in rows if r.get("id") != exec_id]
    if len(kept) == len(rows):
        return False
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept),
        encoding="utf-8",
    )
    os.replace(tmp, p)
    return True


def summarize_intraday_executions(executions: Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """R51 分型汇总：t_trade（做T单）计日内敞口与配对盈亏；base_rebalance（水位调仓单）独立记账不触发未平T敞口告警。"""
    by_code: dict[str, dict[str, Any]] = {}
    t_exec_count = 0
    rebal_count = 0
    rebal_buy_amount = 0.0
    rebal_sell_amount = 0.0

    for e in executions:
        otype = str(e.get("order_type") or "t_trade").strip().lower()
        sh = int(e.get("shares") or 0)
        amt = float(e.get("amount") or 0.0)
        side_u = str(e.get("side") or "").upper()

        if otype == "base_rebalance":
            rebal_count += 1
            if side_u == "BUY":
                rebal_buy_amount += amt
            else:
                rebal_sell_amount += amt
            continue

        t_exec_count += 1
        c = str(e.get("code") or "")
        st = by_code.setdefault(c, {
            "buy_shares": 0,
            "buy_amount": 0.0,
            "sell_shares": 0,
            "sell_amount": 0.0,
            "net_shares": 0,
            "matched_shares": 0,
            "realized_pnl": 0.0,
            "status": "flat",
            "status_label": "已平",
        })
        if side_u == "BUY":
            st["buy_shares"] += sh
            st["buy_amount"] += amt
        else:
            st["sell_shares"] += sh
            st["sell_amount"] += amt

    total_realized = 0.0
    total_open_abs = 0
    open_codes = 0

    for c, st in by_code.items():
        b_sh, s_sh = st["buy_shares"], st["sell_shares"]
        net = b_sh - s_sh
        matched = min(b_sh, s_sh)
        pnl = 0.0
        if matched > 0:
            avg_b = st["buy_amount"] / b_sh
            avg_s = st["sell_amount"] / s_sh
            gross = (avg_s - avg_b) * matched
            fee = (avg_b * matched * tts.FEE_BUY_PCT) + (avg_s * matched * tts.FEE_SELL_PCT)
            pnl = round(gross - fee, 2)
        st["net_shares"] = net
        st["matched_shares"] = matched
        st["realized_pnl"] = pnl
        if net > 0:
            st["status"] = "long_open"
            st["status_label"] = f"待卖出 +{net} 股"
            open_codes += 1
            total_open_abs += abs(net)
        elif net < 0:
            st["status"] = "short_open"
            st["status_label"] = f"待接回 {net} 股"
            open_codes += 1
            total_open_abs += abs(net)
        else:
            st["status"] = "flat"
            st["status_label"] = "日内已平"
        total_realized += pnl

    summary = {
        "execution_count": len(executions),
        "t_trade_count": t_exec_count,
        "rebalance_count": rebal_count,
        "rebalance_buy_amount": round(rebal_buy_amount, 2),
        "rebalance_sell_amount": round(rebal_sell_amount, 2),
        "rebalance_net_amount": round(rebal_buy_amount - rebal_sell_amount, 2),
        "open_exposure_codes": open_codes,
        "open_exposure_shares": total_open_abs,
        "realized_t_pnl": round(total_realized, 2),
    }
    return by_code, summary
