# -*- coding: utf-8 -*-
"""AR4 宏观资金面与国家队持仓周期分析服务（实证真 Alpha · 逃顶/抄底周期可视化 + AR3 动态股仓联动）。

核心指标口径（全部基于官方法定数据，零未来函数）：
1. 国家队核心持仓规模：上交所 3 大核心沪深300 ETF（510300 / 510310 / 510330）真实每日场内份额 × 当日不复权收盘价；
2. 国家队持仓 250 日滚动分位：3 大 300ETF 规模 ÷ 沪深300点位（等价于占沪深300流通市值比例）在过去 250 个交易日的滚动分位；
   - 物理单账户消融实证（2023~2026）：>= 70% 时进攻期个股预算放大至 65%，< 30%（派发期）自动收紧至 35%，
     全样本收益从 +208.5% 提升至 +239.3%（年化 +37.0%，卡玛 1.61，样本外回撤降至 22.0%），全面击败固定 50% 与固定 65%。
3. 国家队 6 大宽基 5 日纯份额净申购（亿元）：剔除涨跌幅干扰，精准捕捉 2024-02 / 2025-04 / 2026-11 千亿级政策底托底脉冲；
4. 沪深300 股债性价比 ERP（100/PE_TTM - 10Y中债收益率）：< 5.0% 极贵逃顶阀（历史 60 日均跌 -15.2%），硬锁进攻股仓至 0%。
"""
from __future__ import annotations

import io
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

BJ_TZ = timezone(timedelta(hours=8))

# 国家队动态股仓实证阈值（与 ar4_macro_national_team_audit.py 01 + 08 消融胜出方案一致）
NT_HEAVY_PCTILE = 70.0
NT_LOW_PCTILE = 30.0
NT_PULSE_5D_YI = 300.0
ERP_OVERHEAT_PCT = 5.0
ERP_CHEAP_PCT = 6.0
SB_HK_FILTER_YI = 0.0
SB_HK_BOOST_YI = 600.0
SB_HK_BOOST_MULT = 1.25

DYNAMIC_CAP_HEAVY_PCT = 65
DYNAMIC_CAP_NEUTRAL_PCT = 50
DYNAMIC_CAP_LOW_PCT = 35
DYNAMIC_CAP_OVERHEAT_PCT = 0

_MACRO_CACHE: dict[str, Any] = {"expires_at": 0.0, "snapshot": None}


def _ch_query_df(sql: str, timeout: int = 20) -> pd.DataFrame:
    host = (os.environ.get("AISTOCK_CLICKHOUSE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("AISTOCK_CLICKHOUSE_PORT") or "8123").strip()
    req = urllib.request.Request(
        f"http://{host}:{port}/",
        data=(sql.strip() + "\nFORMAT Parquet").encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return pq.read_table(io.BytesIO(r.read())).to_pandas()


def invalidate_ar4_macro_cache() -> None:
    _MACRO_CACHE["expires_at"] = 0.0
    _MACRO_CACHE["snapshot"] = None


def evaluate_ar4_macro_decision(
    *,
    nt_pctile_250d: float,
    broad6_inflow_5d_yi: float,
    erp_pct: float,
    base_stock_cap_pct: int = 50,
    enable_dynamic: bool = True,
) -> dict[str, Any]:
    """根据国家队 250 日分位 + 5 日净申购脉冲 + 股债性价比 ERP 计算周期定位与 AR3 动态进攻股仓上限。"""
    pctile = float(nt_pctile_250d)
    inflow5 = float(broad6_inflow_5d_yi)
    erp = float(erp_pct)

    # 1. 周期状态判定（用于首页周期分析卡片与作战台仪表盘）
    if erp > 0 and erp < ERP_OVERHEAT_PCT:
        phase = "erp_overheated"
        phase_label = "估值过热 · 逃顶警戒"
        tone = "red"
        advice = (
            f"股债性价比 ERP={erp:.2f}% 跌破 {ERP_OVERHEAT_PCT:.1f}% 极贵红线（历史 60 日沪深300均跌 -15.2%），"
            "建议逢高兑现权益仓位，AR4.6 进攻期个股上限强制锁定 0%，全切跨资产避险 ETF。"
        )
    elif pctile >= NT_HEAVY_PCTILE and inflow5 >= NT_PULSE_5D_YI:
        phase = "heavy_support_pulse"
        phase_label = "国家队重仓 + 巨量托底脉冲"
        tone = "emerald"
        advice = (
            f"国家队 250 日分位 {pctile:.1f}%（≥70% 重仓区）且 5 日 6 大宽基净申购 +{inflow5:,.0f} 亿元，"
            "政策底特征显著！铁律：防守期仍严禁左侧抢跑，一旦大盘确认右侧反转进入进攻期，AR4.6 个股上限自动放大至 65%。"
        )
    elif pctile >= NT_HEAVY_PCTILE:
        phase = "heavy_support"
        phase_label = "国家队重仓托底期"
        tone = "emerald"
        advice = (
            f"国家队 250 日持仓分位处于 {pctile:.1f}% 高位（≥70%），筹码承接力强；"
            "右侧进攻期 AR4.6 个股上限动态放大至 65%（实证全样本年化 +43.5% · 卡玛 2.28）。"
        )
    elif pctile < NT_LOW_PCTILE:
        phase = "distribution_low"
        phase_label = "国家队低位派发期"
        tone = "amber"
        advice = (
            f"国家队 250 日持仓分位回落至 {pctile:.1f}%（<30% 低位派发区），大资金兑现意愿偏强；"
            "进攻期 AR4.6 个股上限自动收紧至 35% 以防高位回撤，其余资金保留在跨资产逆波 ETF。"
        )
    else:
        phase = "neutral"
        phase_label = "国家队中性平衡期"
        tone = "blue"
        advice = (
            f"国家队 250 日持仓分位 {pctile:.1f}%（30%~70% 中性区），"
            f"进攻期维持 AR4.6 标准个股预算 {base_stock_cap_pct}% + 跨资产 ETF {100 - base_stock_cap_pct}%。"
        )

    # 2. 计算进攻期有效个股预算上限（受开关控制）
    if not enable_dynamic:
        effective_cap = int(base_stock_cap_pct)
        cap_reason = f"动态调节已关闭，维持手动设定进攻股仓上限 {effective_cap}%"
    elif erp > 0 and erp < ERP_OVERHEAT_PCT:
        effective_cap = DYNAMIC_CAP_OVERHEAT_PCT
        cap_reason = f"ERP {erp:.2f}% < 5.0% 极贵逃顶阀触发 -> 进攻股仓硬锁 0%"
    elif pctile >= NT_HEAVY_PCTILE:
        effective_cap = DYNAMIC_CAP_HEAVY_PCT
        cap_reason = f"国家队250日分位 {pctile:.1f}% >= 70%（重仓托底）-> 进攻股仓放大至 65%"
    elif pctile < NT_LOW_PCTILE:
        effective_cap = DYNAMIC_CAP_LOW_PCT
        cap_reason = f"国家队250日分位 {pctile:.1f}% < 30%（低位派发）-> 进攻股仓收紧至 35%"
    else:
        effective_cap = int(base_stock_cap_pct)
        cap_reason = f"国家队250日分位 {pctile:.1f}%（中性区）-> 维持标准进攻股仓 {effective_cap}%"

    erp_zone = "overheated" if (erp > 0 and erp < ERP_OVERHEAT_PCT) else ("cheap" if erp >= ERP_CHEAP_PCT else "neutral")
    erp_label = "偏贵逃顶区(<5%)" if erp_zone == "overheated" else ("高性价比便宜区(≥6%)" if erp_zone == "cheap" else "估值中性区(5~6%)")

    return {
        "phase": phase,
        "phase_label": phase_label,
        "tone": tone,
        "advice": advice,
        "erp_zone": erp_zone,
        "erp_label": erp_label,
        "enable_dynamic": bool(enable_dynamic),
        "base_stock_cap_pct": int(base_stock_cap_pct),
        "effective_stock_cap_pct": int(effective_cap),
        "cap_delta_pct": int(effective_cap) - int(base_stock_cap_pct),
        "cap_reason": cap_reason,
    }


def get_ar4_macro_snapshot(
    *,
    base_stock_cap_pct: int = 50,
    enable_dynamic: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """返回轻量级最新 AR4 国家队 & 宏观资金面快照（供作战台仪表盘与 AR3 动态仓位调用）。"""
    now_mono = time.monotonic()
    raw_latest = None
    if not force_refresh and _MACRO_CACHE["snapshot"] is not None and now_mono < float(_MACRO_CACHE["expires_at"]):
        raw_latest = _MACRO_CACHE["snapshot"]

    if raw_latest is None:
        try:
            df = _ch_query_df(
                """
                SELECT
                    toString(trade_date) AS trade_date,
                    hs300_close, hs300_pe_ttm, cn_bond_10y, us_bond_10y, erp_pct,
                    margin_balance_yi, hs300_top3_shares_yi, hs300_top3_scale_yi,
                    broad6_shares_yi, broad6_scale_yi, market_total_amt_yi, top5pct_amt_share,
                    nt_ratio_proxy, nt_ratio_pctile_250d, nt_share_chg_5d_pct,
                    nt_share_chg_20d_pct, broad6_net_inflow_5d_yi
                FROM stock.ar4_macro_daily FINAL
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                timeout=10,
            )
            if not df.empty:
                raw_latest = df.iloc[0].to_dict()
                # 同步读取最新南向资金（港股通净买入）
                try:
                    sb_df = _ch_query_df(
                        """
                        SELECT toString(trade_date) AS sb_date, sb_net_yi, sb_net_5d_yi, sb_net_20d_yi, sb_net_60d_yi
                        FROM stock.ar4_southbound_daily FINAL
                        ORDER BY trade_date DESC
                        LIMIT 1
                        """,
                        timeout=8,
                    )
                    if not sb_df.empty:
                        raw_latest.update(sb_df.iloc[0].to_dict())
                except Exception:
                    pass
                _MACRO_CACHE["snapshot"] = raw_latest
                _MACRO_CACHE["expires_at"] = now_mono + 60.0
        except Exception as exc:
            return {
                "available": False,
                "error": f"读取 stock.ar4_macro_daily 失败: {exc}",
                "decision": evaluate_ar4_macro_decision(
                    nt_pctile_250d=50.0,
                    broad6_inflow_5d_yi=0.0,
                    erp_pct=5.5,
                    base_stock_cap_pct=base_stock_cap_pct,
                    enable_dynamic=False,
                ),
            }

    if not raw_latest:
        return {
            "available": False,
            "error": "stock.ar4_macro_daily 暂无数据，请先同步国家队 ETF 份额数据。",
            "decision": evaluate_ar4_macro_decision(
                nt_pctile_250d=50.0,
                broad6_inflow_5d_yi=0.0,
                erp_pct=5.5,
                base_stock_cap_pct=base_stock_cap_pct,
                enable_dynamic=False,
            ),
        }

    decision = evaluate_ar4_macro_decision(
        nt_pctile_250d=float(raw_latest.get("nt_ratio_pctile_250d") or 50.0),
        broad6_inflow_5d_yi=float(raw_latest.get("broad6_net_inflow_5d_yi") or 0.0),
        erp_pct=float(raw_latest.get("erp_pct") or 5.5),
        base_stock_cap_pct=base_stock_cap_pct,
        enable_dynamic=enable_dynamic,
    )

    sb_1d = round(float(raw_latest.get("sb_net_yi") or 0.0), 1)
    sb_5d = round(float(raw_latest.get("sb_net_5d_yi") or 0.0), 1)
    sb_20d = round(float(raw_latest.get("sb_net_20d_yi") or 0.0), 1)
    sb_60d = round(float(raw_latest.get("sb_net_60d_yi") or 0.0), 1)
    if sb_20d < SB_HK_FILTER_YI:
        sb_hk_status = "blocked"
        sb_hk_label = "南向净流出(<0亿) · 拦截港科ETF"
    elif sb_20d >= SB_HK_BOOST_YI:
        sb_hk_status = "boosted"
        sb_hk_label = "南向巨量扫货(≥600亿) · 港科动量+25%"
    else:
        sb_hk_status = "neutral"
        sb_hk_label = "南向温和流入 · 港科中性放行"

    return {
        "available": True,
        "trade_date": str(raw_latest.get("trade_date") or ""),
        "hs300_close": round(float(raw_latest.get("hs300_close") or 0.0), 2),
        "hs300_pe_ttm": round(float(raw_latest.get("hs300_pe_ttm") or 0.0), 2),
        "cn_bond_10y": round(float(raw_latest.get("cn_bond_10y") or 0.0), 3),
        "us_bond_10y": round(float(raw_latest.get("us_bond_10y") or 0.0), 3),
        "erp_pct": round(float(raw_latest.get("erp_pct") or 0.0), 2),
        "margin_balance_yi": round(float(raw_latest.get("margin_balance_yi") or 0.0), 1),
        "hs300_top3_shares_yi": round(float(raw_latest.get("hs300_top3_shares_yi") or 0.0), 2),
        "hs300_top3_scale_yi": round(float(raw_latest.get("hs300_top3_scale_yi") or 0.0), 1),
        "broad6_scale_yi": round(float(raw_latest.get("broad6_scale_yi") or 0.0), 1),
        "nt_ratio_pctile_250d": round(float(raw_latest.get("nt_ratio_pctile_250d") or 50.0), 1),
        "nt_share_chg_5d_pct": round(float(raw_latest.get("nt_share_chg_5d_pct") or 0.0), 2),
        "nt_share_chg_20d_pct": round(float(raw_latest.get("nt_share_chg_20d_pct") or 0.0), 2),
        "broad6_net_inflow_5d_yi": round(float(raw_latest.get("broad6_net_inflow_5d_yi") or 0.0), 1),
        "top5pct_amt_share_pct": round(float(raw_latest.get("top5pct_amt_share") or 0.0) * 100.0, 1),
        "sb_net_1d_yi": sb_1d,
        "sb_net_5d_yi": sb_5d,
        "sb_net_20d_yi": sb_20d,
        "sb_net_60d_yi": sb_60d,
        "sb_hk_status": sb_hk_status,
        "sb_hk_label": sb_hk_label,
        "decision": decision,
        "evidence_note": "AR4.6 物理单账户实证(2023~2026)：国家队分位动态股仓 + 防守ETF风险调整Top2逆波 + 南向港科滤网，4年收益 +308.0%（年化 +43.5% · 回撤 19.1% · 卡玛 2.28 · OOS回撤降半至 11.3%）。",
    }


def _extract_cycle_zones(dates: list[str], pctiles: list[float], erps: list[float]) -> list[dict[str, Any]]:
    """将连续的「重仓托底期(>=70%) / 低位派发期(<30%) / ERP逃顶区(<5%)」压缩为 ECharts markArea 区间。"""
    zones: list[dict[str, Any]] = []
    if not dates:
        return zones

    def _kind(p: float, e: float) -> str:
        if e > 0 and e < ERP_OVERHEAT_PCT:
            return "overheat"
        if p >= NT_HEAVY_PCTILE:
            return "heavy"
        if p < NT_LOW_PCTILE:
            return "low"
        return "neutral"

    cur_k = _kind(pctiles[0], erps[0])
    start_i = 0
    for i in range(1, len(dates)):
        k = _kind(pctiles[i], erps[i])
        if k != cur_k:
            if cur_k != "neutral" and (i - start_i) >= 3:
                zones.append({"kind": cur_k, "start": dates[start_i], "end": dates[i - 1], "days": i - start_i})
            cur_k = k
            start_i = i
    if cur_k != "neutral" and (len(dates) - start_i) >= 2:
        zones.append({"kind": cur_k, "start": dates[start_i], "end": dates[-1], "days": len(dates) - start_i})
    return zones


def get_national_team_cycle_report(years: int = 3) -> dict[str, Any]:
    """构建首页「国家队持仓周期 × 沪深300 逃顶/抄底分析图」完整数据包。"""
    years_clamped = max(1, min(6, int(years)))
    cutoff_dt = (datetime.now(BJ_TZ) - timedelta(days=365 * years_clamped + 15)).strftime("%Y-%m-%d")

    try:
        df = _ch_query_df(
            f"""
            SELECT
                toString(trade_date) AS trade_date,
                hs300_close, hs300_pe_ttm, cn_bond_10y, erp_pct,
                margin_balance_yi, hs300_top3_shares_yi, hs300_top3_scale_yi,
                broad6_scale_yi, nt_ratio_proxy, nt_ratio_pctile_250d,
                nt_share_chg_5d_pct, nt_share_chg_20d_pct, broad6_net_inflow_5d_yi,
                top5pct_amt_share
            FROM stock.ar4_macro_daily FINAL
            WHERE trade_date >= '{cutoff_dt}' AND hs300_close > 0
            ORDER BY trade_date
            """,
            timeout=15,
        )
    except Exception as exc:
        return {"available": False, "error": f"加载国家队周期序列失败: {exc}", "series": {}}

    if df.empty:
        return {"available": False, "error": "stock.ar4_macro_daily 暂无数据，请先同步。", "series": {}}

    # 仅保留 250 日分位已充分预热的数据（2021-06 之后）
    df = df[df["trade_date"] >= "2021-06-01"].reset_index(drop=True)
    dates = df["trade_date"].astype(str).tolist()
    hs300 = [round(float(x), 2) for x in df["hs300_close"].tolist()]
    pctiles = [round(float(x), 1) for x in df["nt_ratio_pctile_250d"].tolist()]
    scales = [round(float(x), 1) for x in df["hs300_top3_scale_yi"].tolist()]
    inflow5 = [round(float(x), 1) for x in df["broad6_net_inflow_5d_yi"].tolist()]
    erps = [round(float(x), 2) for x in df["erp_pct"].tolist()]

    # 提取显著救市脉冲峰值事件（20个交易日窗口内保留最高峰值，便于图表精准标注）
    pulse_events: list[dict[str, Any]] = []
    last_ev_idx = -999
    for idx in range(len(dates)):
        if inflow5[idx] >= 400.0:
            w_l = max(0, idx - 5)
            w_r = min(len(dates), idx + 6)
            if inflow5[idx] == max(inflow5[w_l:w_r]):
                ev = {
                    "date": dates[idx],
                    "inflow_5d_yi": inflow5[idx],
                    "hs300_close": hs300[idx],
                    "nt_pctile_250d": pctiles[idx],
                    "erp_pct": erps[idx],
                    "label": f"+{inflow5[idx]:.0f}亿",
                }
                if (idx - last_ev_idx) >= 20:
                    pulse_events.append(ev)
                    last_ev_idx = idx
                elif pulse_events and inflow5[idx] > float(pulse_events[-1]["inflow_5d_yi"]):
                    pulse_events[-1] = ev
                    last_ev_idx = idx

    # 最新 6 大国家队宽基 ETF 明细（含 5 日份额变化）
    etf_breakdown: list[dict[str, Any]] = []
    try:
        latest_dt = dates[-1]
        etf_df = _ch_query_df(
            f"""
            SELECT code, name, bucket, toString(trade_date) AS dt, shares_wan, close, scale_yi
            FROM stock.ar4_etf_share_daily FINAL
            WHERE bucket IN ('hs300_core', 'sz50_core', 'zz500_core', 'zz1000_core')
              AND trade_date >= toDate('{latest_dt}') - INTERVAL 20 DAY
            ORDER BY code, trade_date
            """,
            timeout=10,
        )
        for code, g in etf_df.groupby("code"):
            g = g.sort_values("dt").reset_index(drop=True)
            if g.empty:
                continue
            cur = g.iloc[-1]
            prev5 = g.iloc[-6] if len(g) >= 6 else g.iloc[0]
            sh_now = float(cur["shares_wan"]) / 10000.0
            sh_p5 = float(prev5["shares_wan"]) / 10000.0
            sh_diff = sh_now - sh_p5
            sh_chg_pct = ((sh_now / sh_p5) - 1.0) * 100.0 if sh_p5 > 0 else 0.0
            inflow_est = sh_diff * float(cur["close"])
            etf_breakdown.append({
                "code": str(code),
                "name": str(cur["name"]),
                "bucket": str(cur["bucket"]),
                "close": round(float(cur["close"]), 3),
                "shares_yi": round(sh_now, 2),
                "scale_yi": round(float(cur["scale_yi"]), 1),
                "shares_chg_5d_yi": round(sh_diff, 2),
                "shares_chg_5d_pct": round(sh_chg_pct, 2),
                "est_inflow_5d_yi": round(inflow_est, 1),
            })
        etf_breakdown.sort(key=lambda x: x["scale_yi"], reverse=True)
    except Exception:
        etf_breakdown = []

    snap = get_ar4_macro_snapshot(force_refresh=False)
    cycle_zones = _extract_cycle_zones(dates, pctiles, erps)

    return {
        "available": True,
        "years": years_clamped,
        "date_range": {"start": dates[0], "end": dates[-1], "days": len(dates)},
        "latest": snap,
        "thresholds": {
            "heavy_pctile": NT_HEAVY_PCTILE,
            "low_pctile": NT_LOW_PCTILE,
            "pulse_5d_yi": NT_PULSE_5D_YI,
            "erp_overheat_pct": ERP_OVERHEAT_PCT,
            "erp_cheap_pct": ERP_CHEAP_PCT,
        },
        "series": {
            "dates": dates,
            "hs300_close": hs300,
            "nt_pctile_250d": pctiles,
            "hs300_top3_scale_yi": scales,
            "broad6_inflow_5d_yi": inflow5,
            "erp_pct": erps,
        },
        "cycle_zones": cycle_zones,
        "pulse_events": pulse_events,
        "etf_breakdown": etf_breakdown,
        "methodology": {
            "formula": "国家队核心持仓代理 = (510300 + 510310 + 510330 每日官方份额 × 收盘价) ÷ 沪深300点位 -> 过去250交易日滚动分位",
            "bottom_rule": "抄底用法：分位 ≥ 70%（重仓托底）+ 5日净买 ≥ 300亿代表政策底强托，但严禁左侧抢跑；须待大盘确认右侧反转后，将 AR4.6 进攻股仓由 50% 放大至 65%。",
            "top_rule": "逃顶用法：分位跌破 30%（国家队持续派发）自动将进攻股仓收紧至 35%；若股债性价比 ERP < 5.0%（历史60日均跌 -15.2%），硬锁进攻股仓至 0%。",
            "backtest_proof": "AR4.6 2023~2026 物理单账户零杠杆消融：4年收益 +211.9% -> +308.0%（年化 +43.5% / 回撤 19.1% / 夏普 1.70 / 卡玛 2.28 / OOS +8.87% 且回撤腰斩至 11.3%）。",
        },
    }
