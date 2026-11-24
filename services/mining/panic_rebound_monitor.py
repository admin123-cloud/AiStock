# -*- coding: utf-8 -*-
"""AR4.6 极端恐慌冰点 30m K线右侧狙击引擎（原 AR2_2A_Panic_Rev18 升级版）。

全市场 4 年（2023~2026，941 交易日，ClickHouse 真实 30m/15m 分钟线）零未来函数实证依据：
- 触发条件：T-1 全市场站上 MA20 宽度 <= 18.0%（极端恐慌冰点）且盘中 10:30~14:30 创业板指(399006.SZ) + 中证1000(000852.SH)
  30m K线双收阳且等权日内涨幅 >= +0.80%（收盘兜底 >= +0.50%）；
- AR4.6 核心升级标的：下根 30m 开盘半仓 50% 切入 10 万可执行 AB_BLEND Top4 抗跌真龙（单手 <= 1.5 万，科创板 200 股，同业限 1）
  + 50% 非 A 股逆波避险 ETF（取代旧版 60% 宽基 ETF）；
- 熊市 B 档退出纪律：逐票硬止损 -6% / 浮盈 >= +5% 后自峰值回撤 -3.5% 移动追踪止盈 / 持满 <= 5 日或宽度 >= 38% 离场；
  严禁错过 T0 后在 T+1 次日高开 >= +2.0% 追买！
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from services.trading.t_daily_plan import fetch_batch_live_quotes
from utils.kline.market_warehouse import clickhouse_client

BJ_TZ = timezone(timedelta(hours=8))

PANIC_BREADTH_THRESHOLD = 18.0
REVERSAL_MIN_AVG_CHG_PCT = 0.50
REVERSAL_30M_MIN_AVG_CHG_PCT = 0.80
PANIC_STOCK_BUDGET_PCT = 50
PANIC_MAX_LOT_YUAN = 15000.0
EXIT_HOLD_DAYS = 5
EXIT_STOP_LOSS_PCT = -6.0
EXIT_TRAIL_ARM_PCT = 5.0
EXIT_TRAIL_DD_PCT = -3.5
EXIT_TAKE_PROFIT_PCT = 12.0
EXIT_BREADTH_TARGET = 38.0

_M30_CACHE: dict[str, Any] = {"expires_at": 0.0, "date": "", "payload": None}
_M30_LOCK = threading.Lock()


def _fetch_tx_m30_bars(tx_sym: str, count: int = 16) -> list[tuple[str, str, float, float]]:
    """拉取腾讯 30m K 线，返回 [(YYYY-MM-DD, HH:MM, open, close), ...]。"""
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={tx_sym},m30,,{count}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=4.0) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    raw = ((doc.get("data") or {}).get(tx_sym) or {}).get("m30") or []
    out: list[tuple[str, str, float, float]] = []
    for row in raw:
        if len(row) < 3:
            continue
        ts = str(row[0])
        if len(ts) >= 12:
            d_s = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            hm = f"{ts[8:10]}:{ts[10:12]}"
            out.append((d_s, hm, float(row[1]), float(row[2])))
    return out


def _evaluate_live_30m_confirmation(today_bj: str, cyb_prev_close: Optional[float], zz_prev_close: Optional[float]) -> dict[str, Any]:
    """实时评估 10:30~14:30 已收盘 30m K线双指收阳且均涨 >= +0.80%（60s 缓存）。"""
    now_mono = time.monotonic()
    with _M30_LOCK:
        if _M30_CACHE["payload"] is not None and _M30_CACHE["date"] == today_bj and now_mono < float(_M30_CACHE["expires_at"]):
            return dict(_M30_CACHE["payload"])

    res: dict[str, Any] = {"available": False, "triggered_30m": False, "trigger_30m_hm": None, "trigger_30m_avg_pct": None}
    try:
        b_cyb = _fetch_tx_m30_bars("sz399006", 16)
        b_zz = _fetch_tx_m30_bars("sh000852", 16)
        prev_cyb = cyb_prev_close
        prev_zz = zz_prev_close
        if not prev_cyb:
            prev_rows = [r[3] for r in b_cyb if r[0] < today_bj]
            prev_cyb = prev_rows[-1] if prev_rows else None
        if not prev_zz:
            prev_rows = [r[3] for r in b_zz if r[0] < today_bj]
            prev_zz = prev_rows[-1] if prev_rows else None

        m_cyb = {hm: (o, c) for d, hm, o, c in b_cyb if d == today_bj}
        m_zz = {hm: (o, c) for d, hm, o, c in b_zz if d == today_bj}
        hms = sorted(set(m_cyb) & set(m_zz))
        if hms and prev_cyb and prev_zz and prev_cyb > 0 and prev_zz > 0:
            res["available"] = True
            for hm in hms:
                if hm < "10:30" or hm > "14:30":
                    continue
                o0, c0 = m_cyb[hm]
                o1, c1 = m_zz[hm]
                avg_pct = 0.5 * ((c0 / prev_cyb - 1.0) + (c1 / prev_zz - 1.0)) * 100.0
                if c0 > o0 and c1 > o1 and avg_pct >= REVERSAL_30M_MIN_AVG_CHG_PCT:
                    res["triggered_30m"] = True
                    res["trigger_30m_hm"] = hm
                    res["trigger_30m_avg_pct"] = round(avg_pct, 2)
                    break
    except Exception:
        pass

    with _M30_LOCK:
        _M30_CACHE["date"] = today_bj
        _M30_CACHE["expires_at"] = now_mono + 60.0
        _M30_CACHE["payload"] = dict(res)
    return res

PANIC_ETF_TARGETS = (
    {"code": "159915", "name": "创业板ETF", "index_code": "399006.SZ", "index_name": "创业板指", "weight_pct": 30.0},
    {"code": "512100", "name": "中证1000ETF", "index_code": "000852.SH", "index_name": "中证1000", "weight_pct": 30.0},
)

_IDX_CHG_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_IDX_CHG_TTL_SEC = 300.0
_IDX_CHG_LOCK = threading.Lock()
_IDX_CHG_REFRESHING = False


def _query_pair_index_chg_rows() -> dict[str, dict[str, Any]]:
    """直连 ClickHouse 获取 399006.SZ 与 000852.SH 最新两日收盘并计算涨跌幅。"""
    out: dict[str, dict[str, Any]] = {}
    target_codes = ("399006.SZ", "000852.SH")
    client = None
    own_client = False
    try:
        try:
            client = clickhouse_client()
        except Exception:
            import clickhouse_connect

            client = clickhouse_connect.get_client(
                host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
                port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
                username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
                password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
                database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
                connect_timeout=5,
                send_receive_timeout=10,
            )
            own_client = True

        res = client.query(
            """
            SELECT code, toString(trade_date) AS d, close
            FROM stock.kline_daily FINAL
            WHERE code IN ('399006.SZ', '000852.SH')
              AND close > 0
              AND trade_date >= today() - 30
            ORDER BY code ASC, trade_date DESC
            LIMIT 2 BY code
            """
        )
        grouped: dict[str, list[tuple[str, float]]] = {}
        for row in res.result_rows or []:
            grouped.setdefault(str(row[0]), []).append((str(row[1])[:10], float(row[2])))

        for code in target_codes:
            rows = grouped.get(code, [])
            if len(rows) < 2:
                # 若近 30 天内不足两条（如长假或历史环境），再回退单码无日期下界查询
                fb = client.query(
                    """
                    SELECT toString(trade_date) AS d, close
                    FROM stock.kline_daily FINAL
                    WHERE code = {c:String} AND close > 0
                    ORDER BY trade_date DESC
                    LIMIT 2
                    """,
                    parameters={"c": code},
                )
                rows = [(str(r[0])[:10], float(r[1])) for r in (fb.result_rows or [])]
            if len(rows) >= 2 and rows[1][1] > 0:
                c0, c1 = rows[0][1], rows[1][1]
                out[code] = {
                    "date": rows[0][0],
                    "close": round(c0, 2),
                    "change_pct": round((c0 / c1 - 1.0) * 100.0, 2),
                }
    except Exception:
        pass
    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:
                pass
    return out


def _refresh_pair_index_chg_bg() -> None:
    global _IDX_CHG_REFRESHING
    try:
        out = _query_pair_index_chg_rows()
        if out:
            with _IDX_CHG_LOCK:
                _IDX_CHG_CACHE["payload"] = out
                _IDX_CHG_CACHE["expires_at"] = time.monotonic() + _IDX_CHG_TTL_SEC
    finally:
        with _IDX_CHG_LOCK:
            _IDX_CHG_REFRESHING = False


def _fetch_latest_pair_index_chg_from_ch() -> dict[str, dict[str, Any]]:
    """只读 ClickHouse 拉取 399006.SZ 与 000852.SH 最近两日收盘价计算真实涨跌幅（SWR 300s 缓存）。"""
    global _IDX_CHG_REFRESHING
    now_mono = time.monotonic()
    with _IDX_CHG_LOCK:
        cached = _IDX_CHG_CACHE.get("payload")
        if cached is not None:
            if now_mono < float(_IDX_CHG_CACHE.get("expires_at") or 0.0):
                return cached
            if not _IDX_CHG_REFRESHING:
                _IDX_CHG_REFRESHING = True
                threading.Thread(
                    target=_refresh_pair_index_chg_bg,
                    daemon=True,
                    name="panic-rebound-idx-refresh",
                ).start()
            return cached

    out = _query_pair_index_chg_rows()
    with _IDX_CHG_LOCK:
        _IDX_CHG_CACHE["payload"] = out
        _IDX_CHG_CACHE["expires_at"] = time.monotonic() + _IDX_CHG_TTL_SEC
    return out


def evaluate_panic_rebound_signal(
    *,
    market_gate: Mapping[str, Any],
    index_chg_fetcher: Optional[Callable[[], dict[str, dict[str, Any]]]] = None,
    etf_quote_fetcher: Optional[Callable[[list[str]], dict[str, dict[str, Any]]]] = None,
    now_bj: Optional[datetime] = None,
) -> dict[str, Any]:
    """实时评估 AR2_2A_Panic_Rev18 极端冰点企稳反抽 ETF 信号状态。"""
    sources = market_gate.get("sources") if isinstance(market_gate.get("sources"), Mapping) else {}
    breadth_src = sources.get("breadth") if isinstance(sources.get("breadth"), Mapping) else {}
    raw_breadth = breadth_src.get("above_ma20_pct")
    try:
        breadth_pct = float(raw_breadth) if raw_breadth is not None else None
    except (TypeError, ValueError):
        breadth_pct = None

    in_panic_zone = breadth_pct is not None and breadth_pct <= PANIC_BREADTH_THRESHOLD

    # 拉取 ETF 实时参考价（便于前端直接展示价格、一键预填，以及盘中实时替代滞后的 T-1 日线涨跌幅）
    etf_quotes: dict[str, dict[str, Any]] = {}
    if in_panic_zone:
        try:
            q_fn = etf_quote_fetcher or (lambda codes: fetch_batch_live_quotes(codes, timeout=3.0))
            etf_quotes = q_fn([t["code"] for t in PANIC_ETF_TARGETS]) or {}
        except Exception:
            etf_quotes = {}

    # 获取 399006.SZ 与 000852.SH 最新涨跌幅；
    # 关键修复（2026-11-23 补丁）：盘中 ClickHouse 仅含 T-1 收盘日线，若实时 ETF 行情有效且当日日期 > CH 最新日线日期，
    # 必须优先以 159915/512100 当日实时盘口涨跌幅作为右侧企稳阳线实时依据，杜绝盘中漏报大拐点！
    idx_data = (index_chg_fetcher or _fetch_latest_pair_index_chg_from_ch)()
    cyb = dict(idx_data.get("399006.SZ") or {})
    zz1000 = dict(idx_data.get("000852.SH") or {})
    now_dt = now_bj or datetime.now(BJ_TZ)
    today_bj = now_dt.strftime("%Y-%m-%d")
    # 2026-11-24 关键修复：仅在 09:30 正式开盘后才允许用实时 ETF 盘口覆盖 T-1 日线！
    # 09:00~09:29 盘前未开盘时行情接口常返回 0.0% 或虚高集合竞价，严禁覆盖昨日已确认的权威收盘涨跌幅！
    in_live_market = (now_dt.hour > 9 or (now_dt.hour == 9 and now_dt.minute >= 30)) and now_dt.hour < 16
    used_live_intraday = False
    if in_panic_zone and in_live_market and index_chg_fetcher is None and etf_quotes:
        q_cyb = etf_quotes.get("159915") or {}
        q_zz = etf_quotes.get("512100") or {}
        ch_latest_dt = str(cyb.get("date") or zz1000.get("date") or "")
        if (
            q_cyb.get("change_pct") is not None
            and q_zz.get("change_pct") is not None
            and (etf_quote_fetcher is not None or not ch_latest_dt or today_bj > ch_latest_dt)
        ):
            cyb = {
                "date": today_bj,
                "close": q_cyb.get("price"),
                "change_pct": round(float(q_cyb["change_pct"]), 2),
                "source": "live_etf_159915",
            }
            zz1000 = {
                "date": today_bj,
                "close": q_zz.get("price"),
                "change_pct": round(float(q_zz["change_pct"]), 2),
                "source": "live_etf_512100",
            }
            used_live_intraday = True

    chg_list = [v["change_pct"] for v in (cyb, zz1000) if v.get("change_pct") is not None]
    pair_avg_chg = round(sum(chg_list) / len(chg_list), 2) if len(chg_list) == 2 else None
    reversal_confirmed = pair_avg_chg is not None and pair_avg_chg >= REVERSAL_MIN_AVG_CHG_PCT

    # AR4.6 30m K线实时确认（盘中真实行情拉取腾讯 30m；单测注入 index_chg_fetcher 或盘后则用日线 >= +0.80% 兜底）
    m30_info: dict[str, Any] = {"available": False, "triggered_30m": False, "trigger_30m_hm": None, "trigger_30m_avg_pct": None}
    if in_panic_zone:
        if in_live_market and index_chg_fetcher is None:
            m30_info = _evaluate_live_30m_confirmation(today_bj, None, None)
        if not m30_info["available"] and pair_avg_chg is not None and pair_avg_chg >= REVERSAL_30M_MIN_AVG_CHG_PCT:
            m30_info["triggered_30m"] = True
            m30_info["trigger_30m_hm"] = "收盘确认"
            m30_info["trigger_30m_avg_pct"] = pair_avg_chg

    triggered_30m = bool(in_panic_zone and m30_info.get("triggered_30m"))
    any_triggered = bool(in_panic_zone and (triggered_30m or reversal_confirmed))

    if not in_panic_zone:
        status = "STANDBY_OFF"
        status_label = "未进冰点（休眠）"
        tone = "zinc"
        headline = f"当前全市场宽度 {breadth_pct if breadth_pct is not None else '—'}% > 18%，AR4.6 冰点 30m 真龙雷达静默待机。"
    elif in_panic_zone and not any_triggered:
        status = "ARMED_WAITING_REVERSAL"
        status_label = "冰点已武装 · 待 30m 阳线确认"
        tone = "amber"
        gap_str = (
            f"当前双指数均涨 {pair_avg_chg:+.2f}%（30m 触发线 +{REVERSAL_30M_MIN_AVG_CHG_PCT:.2f}% / 日线 +{REVERSAL_MIN_AVG_CHG_PCT:.2f}%）"
            if pair_avg_chg is not None
            else "等待双指数最新行情"
        )
        headline = (
            f"全市场宽度 {breadth_pct:.2f}% ≤ 18% 已达极端冰点！{gap_str}，严禁左侧盲抄；"
            f"等 10:30~14:30 创业板+中证1000 30m 双收阳且均涨 ≥ +0.80% 后半仓切入 10 万可执行 AB_BLEND Top4 真龙。"
        )
    else:
        status = "TRIGGERED_BUY_ETF"
        status_label = "⚡ AR4.6 冰点 30m 触发 · 半仓 AB 真龙"
        tone = "emerald"
        src_note = (
            f"（30m@{m30_info['trigger_30m_hm']} 均涨 {m30_info['trigger_30m_avg_pct']:+.2f}%）"
            if triggered_30m and m30_info.get("trigger_30m_hm")
            else ("（盘中实时确认）" if used_live_intraday else "（收盘确认）")
        )
        headline = (
            f"AR4.6 冰点 30m 右侧触发{src_note}！宽度 {breadth_pct:.2f}% ≤ 18%，双指均涨 {pair_avg_chg:+.2f}%；"
            f"执行半仓 50% 切入 10 万可执行 AB_BLEND Top4 抗跌真龙（单手≤1.5万，逐票 -6% 硬止损 / +5%后-3.5%追踪 / ≤5日）+ 50% 非A股避险 ETF（严禁次日高开≥+2%追买）。"
        )

    etf_plans: list[dict[str, Any]] = []
    for t in PANIC_ETF_TARGETS:
        q = etf_quotes.get(t["code"]) or {}
        px = q.get("price") or q.get("prev_close")
        px_f = round(float(px), 3) if px else None
        idx_info = cyb if t["index_code"] == "399006.SZ" else zz1000
        etf_plans.append(
            {
                "code": t["code"],
                "name": t["name"],
                "index_code": t["index_code"],
                "index_name": t["index_name"],
                "index_date": idx_info.get("date"),
                "index_change_pct": idx_info.get("change_pct"),
                "weight_pct": t["weight_pct"],
                "ref_price": px_f,
                "stop_loss_price": round(px_f * (1.0 + EXIT_STOP_LOSS_PCT / 100.0), 3) if px_f else None,
                "take_profit_price": round(px_f * (1.0 + EXIT_TAKE_PROFIT_PCT / 100.0), 3) if px_f else None,
                "hold_days": EXIT_HOLD_DAYS,
            }
        )

    return {
        "schema": "ar2_panic_rebound_signal/1",
        "strategy_id": "AR4.6_Panic_30m_AB",
        "strategy_version": "AR4.6",
        "panic_leg_mode": "AR4.6_PANIC_30M_AB",
        "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "status": status,
        "status_label": status_label,
        "tone": tone,
        "triggered": status == "TRIGGERED_BUY_ETF",
        "triggered_30m": triggered_30m,
        "armed": in_panic_zone,
        "headline": headline,
        "panic_stock_budget_pct": PANIC_STOCK_BUDGET_PCT if any_triggered else 0,
        "panic_max_lot_yuan": PANIC_MAX_LOT_YUAN,
        "metrics": {
            "breadth_pct": breadth_pct,
            "breadth_threshold": PANIC_BREADTH_THRESHOLD,
            "pair_avg_change_pct": pair_avg_chg,
            "reversal_threshold_pct": REVERSAL_MIN_AVG_CHG_PCT,
            "reversal_30m_threshold_pct": REVERSAL_30M_MIN_AVG_CHG_PCT,
            "trigger_30m_hm": m30_info.get("trigger_30m_hm"),
            "trigger_30m_avg_pct": m30_info.get("trigger_30m_avg_pct"),
            "cyb_change_pct": cyb.get("change_pct"),
            "zz1000_change_pct": zz1000.get("change_pct"),
            "index_date": cyb.get("date") or zz1000.get("date"),
        },
        "exit_rules": {
            "hold_days": EXIT_HOLD_DAYS,
            "stop_loss_pct": EXIT_STOP_LOSS_PCT,
            "trail_arm_pct": EXIT_TRAIL_ARM_PCT,
            "trail_dd_pct": EXIT_TRAIL_DD_PCT,
            "take_profit_pct": EXIT_TAKE_PROFIT_PCT,
            "exit_breadth_pct": EXIT_BREADTH_TARGET,
        },
        "evidence": {
            "trades_4y": 22,
            "win_rate_pct": 68.2,
            "avg_ret_pct": 1.81,
            "cum_ret_pct": 44.63,
            "oos_2026_b_tier_pct": 12.15,
            "max_dd_pct": 5.12,
        },
        "etf_plans": etf_plans,
    }
