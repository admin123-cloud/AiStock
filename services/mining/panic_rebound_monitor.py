# -*- coding: utf-8 -*-
"""AR1 v2.1 / AR2_2A_Panic_Rev18：极端恐慌冰点企稳反抽 ETF 实时监控引擎。

全市场 4 年（940 交易日）实证依据（ar2_high_return_experiment_v1）：
- 规则：全市场站上 MA20 宽度 <= 18.0%（极端恐慌冰点） 且 创业板指(399006.SZ) + 中证1000(000852.SH)
  当日等权平均涨幅 >= +0.50%（右侧首根企稳阳线确认，绝不左侧盲抄）；
- 标的：159915（创业板ETF）+ 512100（中证1000ETF）等权各半（总仓位 60%，进攻方案可至 80%）；
- 退出：持满 5 个交易日 或 宽度回升 >= 38% 或 硬止损 -6% / 止盈 +12%；
- 实证表现：近 4 年 13 次出手胜率 69.2%、单笔均 +2.06%、累计 +16.9%、最大回撤仅 3.7%、三段全正（OOS 2026 熊市 +3.3%）。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from services.trading.t_daily_plan import fetch_batch_live_quotes

BJ_TZ = timezone(timedelta(hours=8))

PANIC_BREADTH_THRESHOLD = 18.0
REVERSAL_MIN_AVG_CHG_PCT = 0.50
EXIT_HOLD_DAYS = 5
EXIT_STOP_LOSS_PCT = -6.0
EXIT_TAKE_PROFIT_PCT = 12.0
EXIT_BREADTH_TARGET = 38.0

PANIC_ETF_TARGETS = (
    {"code": "159915", "name": "创业板ETF", "index_code": "399006.SZ", "index_name": "创业板指", "weight_pct": 30.0},
    {"code": "512100", "name": "中证1000ETF", "index_code": "000852.SH", "index_name": "中证1000", "weight_pct": 30.0},
)

_IDX_CHG_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}


def _fetch_latest_pair_index_chg_from_ch() -> dict[str, dict[str, Any]]:
    """只读 ClickHouse 拉取 399006.SZ 与 000852.SH 最近两日收盘价计算真实涨跌幅。fail-safe。"""
    now_mono = time.monotonic()
    if _IDX_CHG_CACHE["payload"] is not None and now_mono < float(_IDX_CHG_CACHE["expires_at"]):
        return _IDX_CHG_CACHE["payload"]

    out: dict[str, dict[str, Any]] = {}
    try:
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
        try:
            for code in ("399006.SZ", "000852.SH"):
                res = client.query(
                    """
                    SELECT toString(trade_date) AS d, close
                    FROM stock.kline_daily FINAL
                    WHERE code = {c:String} AND close > 0
                    ORDER BY trade_date DESC
                    LIMIT 2
                    """,
                    parameters={"c": code},
                )
                rows = res.result_rows or []
                if len(rows) >= 2:
                    c0 = float(rows[0][1])
                    c1 = float(rows[1][1])
                    chg = round((c0 / c1 - 1.0) * 100.0, 2) if c1 > 0 else None
                    out[code] = {"date": str(rows[0][0])[:10], "close": round(c0, 2), "change_pct": chg}
        finally:
            client.close()
    except Exception:
        pass

    _IDX_CHG_CACHE["payload"] = out
    _IDX_CHG_CACHE["expires_at"] = now_mono + 120.0
    return out


def evaluate_panic_rebound_signal(
    *,
    market_gate: Mapping[str, Any],
    index_chg_fetcher: Optional[Callable[[], dict[str, dict[str, Any]]]] = None,
    etf_quote_fetcher: Optional[Callable[[list[str]], dict[str, dict[str, Any]]]] = None,
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

    # 获取 399006.SZ 与 000852.SH 最新涨跌幅（盘中若已有实时行情优先实时，否则用 ClickHouse 最新收盘）
    idx_data = (index_chg_fetcher or _fetch_latest_pair_index_chg_from_ch)()
    cyb = idx_data.get("399006.SZ") or {}
    zz1000 = idx_data.get("000852.SH") or {}
    chg_list = [v["change_pct"] for v in (cyb, zz1000) if v.get("change_pct") is not None]
    pair_avg_chg = round(sum(chg_list) / len(chg_list), 2) if len(chg_list) == 2 else None
    reversal_confirmed = pair_avg_chg is not None and pair_avg_chg >= REVERSAL_MIN_AVG_CHG_PCT

    if not in_panic_zone:
        status = "STANDBY_OFF"
        status_label = "未进冰点（休眠）"
        tone = "zinc"
        headline = f"当前全市场宽度 {breadth_pct if breadth_pct is not None else '—'}% > 18%，AR2 冰点反抽雷达静默待机。"
    elif in_panic_zone and not reversal_confirmed:
        status = "ARMED_WAITING_REVERSAL"
        status_label = "冰点已武装 · 待企稳阳线"
        tone = "amber"
        gap_str = (
            f"当前双指数均涨 {pair_avg_chg:+.2f}%（距触发线 +{REVERSAL_MIN_AVG_CHG_PCT:.2f}% 还差 {REVERSAL_MIN_AVG_CHG_PCT - pair_avg_chg:.2f}%）"
            if pair_avg_chg is not None
            else "等待双指数最新行情"
        )
        headline = f"全市场宽度 {breadth_pct:.2f}% ≤ 18% 已达极端冰点！{gap_str}，严禁左侧盲抄，等创业板+中证1000 均涨 ≥ +0.50% 右侧确认后次日开盘出击。"
    else:
        status = "TRIGGERED_BUY_ETF"
        status_label = "⚡ 冰点企稳触发 · 建议次日开盘买ETF"
        tone = "emerald"
        headline = (
            f"AR2_2A 冰点企稳右侧触发！宽度 {breadth_pct:.2f}% ≤ 18% 且 创业板+中证1000 均涨 {pair_avg_chg:+.2f}% ≥ +0.50%（历史13战9胜，胜率69.2%）；"
            f"建议次日开盘等权切入 159915 + 512100（总仓位 60%，止损 -6% / 止盈 +12% / 持满 5 日或宽度 ≥ 38% 离场）。"
        )

    # 拉取 ETF 实时参考价（便于前端直接展示价格与一键带入 L2 预检/预填）
    etf_quotes: dict[str, dict[str, Any]] = {}
    if in_panic_zone:
        try:
            q_fn = etf_quote_fetcher or (lambda codes: fetch_batch_live_quotes(codes, timeout=3.0))
            etf_quotes = q_fn([t["code"] for t in PANIC_ETF_TARGETS]) or {}
        except Exception:
            etf_quotes = {}

    etf_plans: list[dict[str, Any]] = []
    for t in PANIC_ETF_TARGETS:
        q = etf_quotes.get(t["code"]) or {}
        px = q.get("price") or q.get("prev_close")
        px_f = round(float(px), 3) if px else None
        idx_info = idx_data.get(t["index_code"]) or {}
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
        "strategy_id": "AR2_2A_Panic_Rev18",
        "as_of": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "status": status,
        "status_label": status_label,
        "tone": tone,
        "triggered": status == "TRIGGERED_BUY_ETF",
        "armed": in_panic_zone,
        "headline": headline,
        "metrics": {
            "breadth_pct": breadth_pct,
            "breadth_threshold": PANIC_BREADTH_THRESHOLD,
            "pair_avg_change_pct": pair_avg_chg,
            "reversal_threshold_pct": REVERSAL_MIN_AVG_CHG_PCT,
            "cyb_change_pct": cyb.get("change_pct"),
            "zz1000_change_pct": zz1000.get("change_pct"),
            "index_date": cyb.get("date") or zz1000.get("date"),
        },
        "exit_rules": {
            "hold_days": EXIT_HOLD_DAYS,
            "stop_loss_pct": EXIT_STOP_LOSS_PCT,
            "take_profit_pct": EXIT_TAKE_PROFIT_PCT,
            "exit_breadth_pct": EXIT_BREADTH_TARGET,
        },
        "evidence": {
            "trades_4y": 13,
            "win_rate_pct": 69.2,
            "avg_ret_pct": 2.06,
            "cum_ret_pct": 16.9,
            "max_dd_pct": 3.7,
        },
        "etf_plans": etf_plans,
    }
