# -*- coding: utf-8 -*-
"""AR3 v3.0 终极双核实盘雷达引擎（50% AB 混合 F3A+F2A 基本面个股 + #01 跨资产 ETF 轮动）。

核心职责：
1. 持久化 AR3 双核主引擎模式配置（ab_blend / f2a_trend / f3a_pullback + #01/#02 ETF 引擎 + 个股仓位上限），
   支持随市场赚钱效应一键热切换；
2. 实时计算 #01 AR3_9_RegimeFocus_Top1+Panic（及可选 #02 AR3_6_RiskAdjMom_Top2+Panic）
   14 只跨资产 ETF 动量排行与今日精确目标持仓指令（支持冰点企稳反抽接管）；
3. 实时扫描 F2A（CANSLIM 业绩趋势主升）与 F3A（绩优高增回踩低吸）双选股雷达，严格按真实公告披露日
   notice_date <= latest_trade_date 对齐（零未来函数）并叠加近 75 天业绩预亏/预减排雷盾；
4. 滚动回算近 20 个交易日 F2A vs F3A 的真实 5 日（T+1开盘 -> T+6收盘）赚钱效应（胜率与均收益），
   为动态切换主引擎提供客观量化依据。
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))

SAFE_BOND_CODE = "511260.SH"
VALID_STOCK_MODES = ("ab_blend", "f2a_trend", "f3a_pullback")
VALID_ETF_MODES = ("regime_focus", "risk_adj_mom")

STOCK_MODE_META: dict[str, dict[str, Any]] = {
    "ab_blend": {
        "label": "AB 混合（F3A回踩 + F2A趋势）",
        "short_label": "AB 混合（全能卡玛王）",
        "desc": "交替填槽 F3A 绩优回踩与 F2A 业绩趋势主升，兼顾低回撤与高弹性（实证年化 +34.8% · 回撤 20.8% · 卡玛 1.67）",
    },
    "f2a_trend": {
        "label": "F2A 业绩趋势主升优先",
        "short_label": "F2A 业绩趋势主升",
        "desc": "主升行情/高风险偏好赚钱效应强时主攻：高增财报池 + 均线多头排列 + 强势相对强度（实证年化 +35.0% · 回撤 24.9%）",
    },
    "f3a_pullback": {
        "label": "F3A 绩优高增回踩低吸优先",
        "short_label": "F3A 绩优回踩低吸",
        "desc": "结构震荡/追高易亏时主攻：高景气高ROE池 + 中期多头缩量回踩MA10企稳低吸（实证年化 +31.0% · OOS +16.4%）",
    },
}

ETF_MODE_META: dict[str, dict[str, Any]] = {
    "regime_focus": {
        "label": "#01 AR3_9_RegimeFocus_Top1+Panic",
        "short_label": "#01 分周期聚焦 Top1+冰点反抽",
        "desc": "进攻期配 A 股强势宽基 Top2；平衡/防守期 100% 聚焦非 A 股跨资产最强单核（黄金/美股/恒科/红利/国债）；极端冰点企稳切反抽",
    },
    "risk_adj_mom": {
        "label": "#02 AR3_6_RiskAdjMom_Top2+Panic",
        "short_label": "#02 全天候分散 Top2+冰点反抽",
        "desc": "全市场 5 大类资产风险调整动量 Top2（每类限 1 只，强制跨资产分散防单品黑天鹅）+ 极端冰点企稳反抽",
    },
}

ASSET_CLASS_LABELS: dict[str, str] = {
    "cn_broad": "A股宽基",
    "cn_dividend": "A股红利",
    "gold": "黄金避险",
    "overseas": "跨境QDII",
    "bond": "利率债",
}

_RADAR_CACHE: dict[str, Any] = {"key": "", "expires_at": 0.0, "payload": None}


def _config_path() -> Path:
    d = runtime_path("mining_contracts")
    d.mkdir(parents=True, exist_ok=True)
    return d / "ar3_dual_core_config.json"


def load_ar3_config() -> dict[str, Any]:
    """读取持久化的 AR3 v3.0 双核主引擎配置（默认 50% AB 混合 + #01 RegimeFocus + AR4 国家队/ERP 动态股仓）。"""
    default_cfg: dict[str, Any] = {
        "active_stock_mode": "ab_blend",
        "etf_core_mode": "regime_focus",
        "stock_budget_cap_pct": 50,
        "enable_macro_dynamic_cap": True,
        "max_slots": 4,
        "updated_at": None,
        "note": "AR3 v3.0 默认实盘定型：50% AB混合(F3A+F2A) + #01 RegimeFocus_Top1+Panic + AR4 国家队分位动态仓位",
    }
    p = _config_path()
    if not p.exists():
        return default_cfg
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_cfg
        sm = str(raw.get("active_stock_mode") or "ab_blend")
        em = str(raw.get("etf_core_mode") or "regime_focus")
        cap = int(raw.get("stock_budget_cap_pct") or 50)
        dyn = bool(raw.get("enable_macro_dynamic_cap", True))
        slots = int(raw.get("max_slots") or 4)
        return {
            "active_stock_mode": sm if sm in VALID_STOCK_MODES else "ab_blend",
            "etf_core_mode": em if em in VALID_ETF_MODES else "regime_focus",
            "stock_budget_cap_pct": max(20, min(65, cap)),
            "enable_macro_dynamic_cap": dyn,
            "max_slots": max(2, min(6, slots)),
            "updated_at": raw.get("updated_at"),
            "note": str(raw.get("note") or default_cfg["note"]),
        }
    except Exception:
        return default_cfg


def save_ar3_config(
    *,
    active_stock_mode: Optional[str] = None,
    etf_core_mode: Optional[str] = None,
    stock_budget_cap_pct: Optional[int] = None,
    enable_macro_dynamic_cap: Optional[bool] = None,
    max_slots: Optional[int] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """热更新 AR3 v3.0 主引擎切换配置并失效内存缓存。"""
    cur = load_ar3_config()
    if active_stock_mode is not None:
        if active_stock_mode not in VALID_STOCK_MODES:
            raise ValueError(f"不支持的个股引擎模式: {active_stock_mode}")
        cur["active_stock_mode"] = active_stock_mode
    if etf_core_mode is not None:
        if etf_core_mode not in VALID_ETF_MODES:
            raise ValueError(f"不支持的 ETF 核心模式: {etf_core_mode}")
        cur["etf_core_mode"] = etf_core_mode
    if stock_budget_cap_pct is not None:
        cur["stock_budget_cap_pct"] = max(20, min(65, int(stock_budget_cap_pct)))
    if enable_macro_dynamic_cap is not None:
        cur["enable_macro_dynamic_cap"] = bool(enable_macro_dynamic_cap)
    if max_slots is not None:
        cur["max_slots"] = max(2, min(6, int(max_slots)))
    if note is not None:
        cur["note"] = str(note).strip()
    cur["updated_at"] = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    _config_path().write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    invalidate_ar3_radar_cache()
    return cur


def invalidate_ar3_radar_cache() -> None:
    _RADAR_CACHE["key"] = ""
    _RADAR_CACHE["expires_at"] = 0.0
    _RADAR_CACHE["payload"] = None


def _ch_query_df(sql: str, timeout: int = 30) -> pd.DataFrame:
    host = (os.environ.get("AISTOCK_CLICKHOUSE_HOST") or "127.0.0.1").strip()
    port = (os.environ.get("AISTOCK_CLICKHOUSE_PORT") or "8123").strip()
    req = urllib.request.Request(
        f"http://{host}:{port}/",
        data=(sql.strip() + "\nFORMAT Parquet").encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return pq.read_table(io.BytesIO(r.read())).to_pandas()


def _build_etf_radar(
    *,
    regime: str,
    etf_core_mode: str,
    stock_budget_cap_pct: int,
    panic_signal: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """从 stock.etf_kline_daily 计算 14 只跨资产 ETF 最新动量与今日目标配置。"""
    since_90 = (datetime.now(BJ_TZ) - timedelta(days=100)).strftime("%Y-%m-%d")
    try:
        raw = _ch_query_df(
            f"""
            SELECT code, name, asset_class, toString(trade_date) AS trade_date, open, close
            FROM stock.etf_kline_daily FINAL
            WHERE trade_date >= '{since_90}'
            ORDER BY code, trade_date
            """,
            timeout=15,
        )
    except Exception as exc:
        return {
            "available": False,
            "error": f"读取 stock.etf_kline_daily 失败: {exc}",
            "latest_date": None,
            "targets": [],
            "universe": [],
        }

    if raw.empty:
        return {
            "available": False,
            "error": "stock.etf_kline_daily 暂无数据，请先执行跨资产 ETF 同步。",
            "latest_date": None,
            "targets": [],
            "universe": [],
        }

    latest_date = str(raw["trade_date"].max())
    meta_df = raw.drop_duplicates("code")[["code", "name", "asset_class"]]
    name_map = dict(zip(meta_df["code"], meta_df["name"]))
    cls_map = dict(zip(meta_df["code"], meta_df["asset_class"]))

    piv_c = raw.pivot(index="trade_date", columns="code", values="close").sort_index()
    ret1 = piv_c.pct_change(1, fill_method=None) * 100.0
    ret10 = piv_c.pct_change(10, fill_method=None) * 100.0
    ret20 = piv_c.pct_change(20, fill_method=None) * 100.0
    ma20 = piv_c.rolling(20).mean()
    vol20 = piv_c.pct_change(fill_method=None).rolling(20).std() * np.sqrt(242.0)

    universe: list[dict[str, Any]] = []
    for code in sorted(piv_c.columns):
        s_c = piv_c[code].dropna()
        if len(s_c) < 21:
            continue
        dt_c = str(s_c.index[-1])
        close_px = float(s_c.iloc[-1])
        r1 = float(ret1[code].loc[dt_c]) if pd.notna(ret1[code].loc[dt_c]) else 0.0
        r10 = float(ret10[code].loc[dt_c]) if pd.notna(ret10[code].loc[dt_c]) else 0.0
        r20 = float(ret20[code].loc[dt_c]) if pd.notna(ret20[code].loc[dt_c]) else 0.0
        m20 = float(ma20[code].loc[dt_c]) if pd.notna(ma20[code].loc[dt_c]) else close_px
        v20 = float(vol20[code].loc[dt_c]) if pd.notna(vol20[code].loc[dt_c]) else 0.20
        above = close_px > m20
        ram = (r20 / 100.0) / (v20 + 0.05)
        cls = str(cls_map.get(code, "other"))
        universe.append({
            "code": str(code).split(".")[0],
            "ts_code": str(code),
            "name": str(name_map.get(code, code)),
            "asset_class": cls,
            "asset_class_label": ASSET_CLASS_LABELS.get(cls, cls),
            "trade_date": dt_c,
            "close": round(close_px, 4),
            "ret_1d_pct": round(r1, 2),
            "ret_10d_pct": round(r10, 2),
            "ret_20d_pct": round(r20, 2),
            "ma20": round(m20, 4),
            "above_ma20": above,
            "vol_20d_pct": round(v20 * 100.0, 1),
            "risk_adj_mom": round(ram, 3),
            "is_qdii": cls == "overseas",
            "eligible": bool(above and r20 > 0.0),
        })

    def _pick(pool: list[dict[str, Any]], key: str, k: int, per_class: bool) -> list[dict[str, Any]]:
        cands = [x for x in pool if x["eligible"]]
        cands.sort(key=lambda x: float(x[key]), reverse=True)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in cands:
            if per_class and item["asset_class"] in seen:
                continue
            out.append(item)
            seen.add(item["asset_class"])
            if len(out) >= k:
                break
        return out

    by_ts = {x["ts_code"]: x for x in universe}
    non_cn = [x for x in universe if x["asset_class"] != "cn_broad"]
    cn_broad = [x for x in universe if x["asset_class"] == "cn_broad"]

    is_offensive = regime in ("offensive", "extreme_offensive")
    panic_triggered = bool((panic_signal or {}).get("triggered"))
    etf_total_budget_pct = (100 - stock_budget_cap_pct) if is_offensive else 100

    # 计算 ETF 子篮子内部归一化权重（总和 = 100%，再按 etf_total_budget_pct 折算账户绝对权重）
    norm_targets: list[tuple[str, float, str]] = []
    decision_headline = ""

    if panic_triggered:
        # 冰点企稳右侧触发：30% 创业板 + 30% 中证1000 + 40% 非A股风险调整动量最强（否则十年国债）
        safe_top = _pick(non_cn, "risk_adj_mom", 1, False)
        safe_ts = safe_top[0]["ts_code"] if safe_top else SAFE_BOND_CODE
        norm_targets = [
            ("159915.SZ", 0.30, "冰点企稳右侧反抽（止损-6%/止盈+12%/持5日）"),
            ("512100.SH", 0.30, "冰点企稳右侧反抽（止损-6%/止盈+12%/持5日）"),
            (safe_ts, 0.40, "冰点期跨资产避险压舱石（非A股最强）"),
        ]
        decision_headline = "极端冰点右侧阳线已确认：触发 AR2_2A 反抽接管（60% 双创小盘反抽 + 40% 非A股避险底仓）。"
    elif etf_core_mode == "regime_focus":
        # #01 AR3_9_RegimeFocus_Top1+Panic
        if is_offensive:
            cn_top = _pick(cn_broad, "ret_20d_pct", 2, False)
            if cn_top:
                w_each = 1.0 / len(cn_top)
                for item in cn_top:
                    norm_targets.append((item["ts_code"], w_each, "A股进攻期 · 宽基20日动量 Top2"))
                decision_headline = (
                    f"当前处于 A 股进攻期：个股预算 {stock_budget_cap_pct}%（未满槽闲置现金亦归入 ETF），"
                    f"ETF 底仓配置 A 股最强宽基 Top{len(cn_top)}。"
                )
            else:
                top1 = _pick(non_cn, "ret_20d_pct", 1, False)
                ts = top1[0]["ts_code"] if top1 else SAFE_BOND_CODE
                norm_targets.append((ts, 1.0, "A股宽基动量未达标，切非A股最强跨资产"))
                decision_headline = "A 股宽基均未站上 MA20/正动量，ETF 仓位暂切非 A 股跨资产最强标的。"
        else:
            top1 = _pick(non_cn, "ret_20d_pct", 1, False)
            if top1:
                norm_targets.append((top1[0]["ts_code"], 1.0, "非进攻期 · 非A股跨资产20日纯动量 Top1（与A股脱钩）"))
                decision_headline = (
                    f"当前处于非进攻期（{regime}）：个股预算锁死 0%，"
                    f"100% 账户资金切入 #01 非 A 股跨资产最强单核「{top1[0]['name']}」。"
                )
            else:
                norm_targets.append((SAFE_BOND_CODE, 1.0, "全球权益/商品趋势全灭 · 100% 十年国债避险"))
                decision_headline = "当前非进攻期且全部跨资产均跌破趋势线，100% 转入十年国债 ETF 避险。"
    else:
        # #02 AR3_6_RiskAdjMom_Top2+Panic
        top2 = _pick(universe, "risk_adj_mom", 2, True)
        if top2:
            w_each = 1.0 / len(top2)
            for item in top2:
                norm_targets.append((item["ts_code"], w_each, "全市场5大类资产风险调整动量 Top2（跨类分散）"))
            names_txt = " + ".join(x["name"] for x in top2)
            decision_headline = f"#02 全天候跨资产分散模式：ETF 预算 {etf_total_budget_pct}% 均分配置「{names_txt}」。"
        else:
            norm_targets.append((SAFE_BOND_CODE, 1.0, "全市场动量为负 · 100% 十年国债避险"))
            decision_headline = "全市场资产均未满足动量门槛，ETF 预算 100% 转入十年国债 ETF。"

    targets: list[dict[str, Any]] = []
    for ts, nw, reason in norm_targets:
        info = by_ts.get(ts)
        if not info:
            continue
        targets.append({
            **info,
            "basket_weight_pct": round(nw * 100.0, 1),
            "account_weight_pct": round(nw * etf_total_budget_pct, 1),
            "reason": reason,
            "qdii_notice": "跨境QDII场内若溢价 > 4% 建议限价或换同方向次优标的" if info["is_qdii"] else None,
        })

    # 全景表按 20日涨幅排序（#01）或风险调整动量排序（#02）
    sort_key = "ret_20d_pct" if etf_core_mode == "regime_focus" else "risk_adj_mom"
    universe_sorted = sorted(universe, key=lambda x: float(x[sort_key]), reverse=True)

    return {
        "available": True,
        "latest_date": latest_date,
        "etf_core_mode": etf_core_mode,
        "etf_core_label": ETF_MODE_META[etf_core_mode]["label"],
        "etf_total_budget_pct": etf_total_budget_pct,
        "stock_budget_active_pct": stock_budget_cap_pct if is_offensive else 0,
        "panic_triggered": panic_triggered,
        "headline": decision_headline,
        "targets": targets,
        "universe": universe_sorted,
    }


def _scan_f2a_f3a_and_money_effect(
    *,
    active_stock_mode: str,
    max_slots: int,
    stock_budget_cap_pct: int,
    is_offensive: bool,
) -> dict[str, Any]:
    """实时扫描 F2A / F3A 双选股雷达，并滚动回算近 20 日赚钱效应对比。"""
    try:
        # 1. 拉取近 95 个自然日正常上市非 ST 股票日 K（~65 交易日，足以算 MA60 + 20日赚钱效应）
        stocks_df = _ch_query_df(
            """
            SELECT code, name
            FROM stock.stocks FINAL
            WHERE type = 'stock'
              AND st = 0 AND quit = 0 AND listing_status = 'active'
              AND positionCaseInsensitive(name, 'ST') = 0
              AND position(name, '退') = 0
            """,
            timeout=15,
        )
        valid_codes = set(stocks_df["code"].astype(str))
        name_map = dict(zip(stocks_df["code"].astype(str), stocks_df["name"].astype(str)))

        ind_df = _ch_query_df(
            """
            SELECT ss.stock_code AS code, any(s.name) AS industry
            FROM stock.sector_stocks ss
            INNER JOIN stock.sectors s ON ss.sector_code = s.code
            WHERE s.type = 'industry' AND length(s.name) > 0
            GROUP BY ss.stock_code
            """,
            timeout=15,
        )
        ind_map = dict(zip(ind_df["code"].astype(str), ind_df["industry"].astype(str)))

        # 确定最近 70 个交易日起始日（用沪深300索引）
        since_160 = (datetime.now(BJ_TZ) - timedelta(days=160)).strftime("%Y-%m-%d")
        hs_df = _ch_query_df(
            f"""
            SELECT toString(trade_date) AS trade_date, close AS hs300_close
            FROM stock.kline_daily FINAL
            WHERE code = '000300.SH' AND trade_date >= '{since_160}'
            ORDER BY trade_date
            """,
            timeout=15,
        )
        if len(hs_df) < 25:
            return {"available": False, "error": "沪深300日线数据不足，无法扫描双选股雷达。"}
        hs_df = hs_df.tail(70).reset_index(drop=True)
        hs_df["hs300_ret_20d"] = hs_df["hs300_close"].pct_change(20) * 100.0
        start_dt = str(hs_df["trade_date"].iloc[0])
        latest_dt = str(hs_df["trade_date"].iloc[-1])
        start_dt_obj = datetime.strptime(start_dt, "%Y-%m-%d")
        fin_since = (start_dt_obj - timedelta(days=210)).strftime("%Y-%m-%d")
        fc_since = (start_dt_obj - timedelta(days=100)).strftime("%Y-%m-%d")

        kline = _ch_query_df(
            f"""
            SELECT code, toString(trade_date) AS trade_date, open, close, amount
            FROM stock.kline_daily FINAL
            WHERE trade_date >= '{start_dt}' AND trade_date <= '{latest_dt}'
              AND close > 0 AND amount > 0
            ORDER BY code, trade_date
            """,
            timeout=30,
        )
        kline = kline.loc[kline["code"].astype(str).isin(valid_codes)].reset_index(drop=True)
        if kline.empty:
            return {"available": False, "error": "个股日线为空。"}

        # 2. 拉取 PIT 财报（notice_date <= latest_dt）与业绩预告（排雷盾）
        fin = _ch_query_df(
            f"""
            SELECT
                code,
                toString(report_date) AS report_date,
                toString(notice_date) AS notice_date,
                source_type,
                revenue_yoy_pct,
                net_profit,
                net_profit_yoy_pct,
                roe_pct
            FROM stock.stock_financial_disclosure FINAL
            WHERE notice_date > report_date
              AND notice_date >= '{fin_since}'
              AND notice_date <= '{latest_dt}'
            ORDER BY code, notice_date, report_date
            """,
            timeout=20,
        )
        fc = _ch_query_df(
            f"""
            SELECT
                code,
                toString(report_date) AS fc_report_date,
                toString(notice_date) AS fc_notice_date,
                predict_type AS fc_predict_type,
                yoy_lower_pct AS fc_yoy_lower
            FROM stock.stock_earnings_forecast FINAL
            WHERE notice_date >= '{fc_since}'
              AND notice_date <= '{latest_dt}'
            ORDER BY code, notice_date, report_date
            """,
            timeout=20,
        )
    except Exception as exc:
        return {"available": False, "error": f"扫描 F2A/F3A 雷达查询失败: {exc}"}

    # 3. 计算技术指标（严格基于当日及之前收盘，零未来函数）
    g = kline.groupby("code", sort=False)
    kline["prev_close"] = g["close"].shift(1)
    kline["ret_1d_pct"] = (kline["close"] / (kline["prev_close"] + 1e-6) - 1.0) * 100.0
    kline["ret_20d_pct"] = g["close"].pct_change(20) * 100.0
    ma5 = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    ma10 = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    ma20 = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    ma60 = g["close"].transform(lambda s: s.rolling(60, min_periods=45).mean())
    amt20 = g["amount"].transform(lambda s: s.rolling(20, min_periods=15).mean())

    kline["above_ma20"] = (kline["close"] > ma20).astype(np.int8)
    kline["ma_bull_align"] = ((ma5 > ma10) & (ma10 > ma20)).astype(np.int8)
    kline["ma20_gt_ma60"] = (ma20 > ma60).astype(np.int8)
    kline["ma10_bias_pct"] = (kline["close"] / (ma10 + 1e-6) - 1.0) * 100.0
    kline["vol_ratio_20d"] = kline["amount"] / (amt20 + 1.0)
    kline["amt_20d_yi"] = amt20 / 1e8

    # 前向 5 日真实撮合收益（严格仅用于历史 T-25 ~ T-6 的近 20 日赚钱效应评估，绝不进入选股逻辑！）
    kline["next_open"] = g["open"].shift(-1)
    kline["fwd_close_5"] = g["close"].shift(-6)
    kline["fwd_ret_5d_pct"] = (kline["fwd_close_5"] / (kline["next_open"] + 1e-6) - 1.0) * 100.0 - 0.26

    # 过滤掉流动性不足 2 亿的行再做 merge_asof，速度极快
    kline = kline.loc[kline["amt_20d_yi"] >= 2.0].copy()
    kline = kline.merge(hs_df[["trade_date", "hs300_ret_20d"]], on="trade_date", how="left")
    kline["rs_20d_vs_hs300"] = kline["ret_20d_pct"] - kline["hs300_ret_20d"].fillna(0.0)
    kline["trade_dt"] = pd.to_datetime(kline["trade_date"])

    # 4. 严格 PIT merge_asof（notice_date <= trade_date）
    if not fin.empty:
        fin["src_prio"] = (fin["source_type"] == "official").astype(int)
        fin = fin.sort_values(["code", "notice_date", "report_date", "src_prio"]).drop_duplicates(
            subset=["code", "notice_date"], keep="last"
        )
        fin["notice_dt"] = pd.to_datetime(fin["notice_date"])
        kline = kline.sort_values(["trade_dt", "code"]).reset_index(drop=True)
        fin = fin.sort_values(["notice_dt", "code"]).reset_index(drop=True)
        kline = pd.merge_asof(
            kline,
            fin[["code", "notice_dt", "notice_date", "report_date", "revenue_yoy_pct", "net_profit", "net_profit_yoy_pct", "roe_pct"]],
            left_on="trade_dt",
            right_on="notice_dt",
            by="code",
            direction="backward",
        )
        kline["pit_days_since_notice"] = (kline["trade_dt"] - kline["notice_dt"]).dt.days
    else:
        for col in ("notice_date", "report_date", "revenue_yoy_pct", "net_profit", "net_profit_yoy_pct", "roe_pct", "pit_days_since_notice"):
            kline[col] = np.nan

    if not fc.empty:
        fc = fc.sort_values(["code", "fc_notice_date", "fc_report_date"]).drop_duplicates(
            subset=["code", "fc_notice_date"], keep="last"
        )
        fc["fc_notice_dt"] = pd.to_datetime(fc["fc_notice_date"])
        kline = kline.sort_values(["trade_dt", "code"]).reset_index(drop=True)
        fc = fc.sort_values(["fc_notice_dt", "code"]).reset_index(drop=True)
        kline = pd.merge_asof(
            kline,
            fc[["code", "fc_notice_dt", "fc_notice_date", "fc_report_date", "fc_predict_type", "fc_yoy_lower"]],
            left_on="trade_dt",
            right_on="fc_notice_dt",
            by="code",
            direction="backward",
        )
        kline["fc_days_since"] = (kline["trade_dt"] - kline["fc_notice_dt"]).dt.days
    else:
        kline["fc_days_since"] = np.nan
        kline["fc_report_date"] = ""
        kline["fc_predict_type"] = ""
        kline["fc_yoy_lower"] = np.nan

    bad_types = {"首亏", "续亏", "预减", "略减", "增亏"}
    fc_newer = kline["fc_report_date"].fillna("") >= kline["report_date"].fillna("")
    kline["has_earnings_mine"] = (
        kline["fc_days_since"].notna()
        & (kline["fc_days_since"] >= 0)
        & (kline["fc_days_since"] <= 75)
        & fc_newer
        & (kline["fc_predict_type"].isin(bad_types) | (kline["fc_yoy_lower"].fillna(0.0) < -20.0))
    )

    kline["rs20_rank"] = kline.groupby("trade_date")["rs_20d_vs_hs300"].rank(pct=True)
    kline["np_yoy_rank"] = kline.groupby("trade_date")["net_profit_yoy_pct"].rank(pct=True)
    kline["name"] = kline["code"].astype(str).map(name_map).fillna(kline["code"])
    kline["industry"] = kline["code"].astype(str).map(ind_map).fillna("未分类")

    def _select_f2a(df: pd.DataFrame) -> pd.DataFrame:
        m = (
            (~df["has_earnings_mine"])
            & (df["pit_days_since_notice"] <= 185)
            & (df["net_profit_yoy_pct"] >= 30.0)
            & (df["revenue_yoy_pct"] >= 15.0)
            & (df["roe_pct"] >= 6.0)
            & (df["net_profit"] >= 50_000_000.0)
            & (df["amt_20d_yi"] >= 2.0)
            & (df["above_ma20"] > 0)
            & (df["ma_bull_align"] > 0)
            & (df["ret_20d_pct"].between(3.0, 35.0))
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        sub["score"] = 0.55 * sub["rs20_rank"].fillna(0.5) + 0.45 * sub["np_yoy_rank"].fillna(0.5)
        return sub.sort_values("score", ascending=False)

    def _select_f3a(df: pd.DataFrame) -> pd.DataFrame:
        m = (
            (~df["has_earnings_mine"])
            & (df["pit_days_since_notice"] <= 185)
            & (df["net_profit_yoy_pct"] >= 30.0)
            & (df["revenue_yoy_pct"] >= 15.0)
            & (df["roe_pct"] >= 8.0)
            & (df["net_profit"] >= 50_000_000.0)
            & (df["amt_20d_yi"] >= 2.0)
            & (df["ma20_gt_ma60"] > 0)
            & (df["ma10_bias_pct"].between(-3.0, 2.0))
            & (df["ret_1d_pct"] > 0.5)
            & (df["vol_ratio_20d"] <= 1.3)
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        sub["score"] = 0.5 * sub["np_yoy_rank"].fillna(0.5) + 0.5 * sub["rs20_rank"].fillna(0.5)
        return sub.sort_values("score", ascending=False)

    # 5. 最新交易日双雷达结果
    latest_slice = kline.loc[kline["trade_date"] == latest_dt]
    f2a_df = _select_f2a(latest_slice)
    f3a_df = _select_f3a(latest_slice)
    f2a_codes = set(f2a_df["code"].astype(str))
    f3a_codes = set(f3a_df["code"].astype(str))

    slot_w_pct = round(stock_budget_cap_pct / max(1, max_slots), 1)

    def _pack_rows(df: pd.DataFrame, engine_tag: str, limit: int = 12) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen_inds: set[str] = set()
        slot_idx = 0
        for r in df.head(limit * 2).itertuples(index=False):
            code = str(r.code)
            ind = str(r.industry)
            ind_dup = (ind != "未分类") and (ind in seen_inds)
            if not ind_dup:
                seen_inds.add(ind)
                slot_idx += 1
            both = (code in f2a_codes) and (code in f3a_codes)
            cl = float(r.close)
            out.append({
                "code": code.split(".")[0],
                "ts_code": code,
                "name": str(r.name),
                "industry": ind,
                "source_engine": "BOTH" if both else engine_tag,
                "source_label": "F2A+F3A双共振" if both else ("F2A业绩趋势" if engine_tag == "F2A" else "F3A绩优回踩"),
                "close": round(cl, 2),
                "stop_loss_price": round(cl * 0.90, 2),
                "ret_1d_pct": round(float(r.ret_1d_pct), 2) if pd.notna(r.ret_1d_pct) else 0.0,
                "ret_20d_pct": round(float(r.ret_20d_pct), 2) if pd.notna(r.ret_20d_pct) else 0.0,
                "rs_20d_pct": round(float(r.rs_20d_vs_hs300), 2) if pd.notna(r.rs_20d_vs_hs300) else 0.0,
                "amt_20d_yi": round(float(r.amt_20d_yi), 2) if pd.notna(r.amt_20d_yi) else 0.0,
                "ma10_bias_pct": round(float(r.ma10_bias_pct), 2) if pd.notna(r.ma10_bias_pct) else 0.0,
                "vol_ratio_20d": round(float(r.vol_ratio_20d), 2) if pd.notna(r.vol_ratio_20d) else 0.0,
                "notice_date": str(r.notice_date or ""),
                "report_date": str(r.report_date or ""),
                "net_profit_yoy_pct": round(float(r.net_profit_yoy_pct), 1) if pd.notna(r.net_profit_yoy_pct) else None,
                "revenue_yoy_pct": round(float(r.revenue_yoy_pct), 1) if pd.notna(r.revenue_yoy_pct) else None,
                "roe_pct": round(float(r.roe_pct), 1) if pd.notna(r.roe_pct) else None,
                "score": round(float(r.score) * 100.0, 1),
                "industry_duplicate": ind_dup,
                "slot_rank": None if ind_dup else slot_idx,
                "suggested_weight_pct": (slot_w_pct if (not ind_dup and slot_idx <= max_slots and is_offensive) else 0.0),
            })
            if len(out) >= limit:
                break
        return out

    f2a_list = _pack_rows(f2a_df, "F2A", limit=12)
    f3a_list = _pack_rows(f3a_df, "F3A", limit=12)

    # 6. 按当前主引擎模式拼装「优先执行队列」（单行业限1只，前 max_slots 只标红/绿重点执行）
    if active_stock_mode == "f2a_trend":
        raw_exec = list(f2a_list)
    elif active_stock_mode == "f3a_pullback":
        raw_exec = list(f3a_list)
    else:
        # AB 交错混合：优先 F3A[0], F2A[0], F3A[1], F2A[1]...
        raw_exec = []
        seen_c: set[str] = set()
        for idx in range(max(len(f3a_list), len(f2a_list))):
            if idx < len(f3a_list) and f3a_list[idx]["ts_code"] not in seen_c:
                raw_exec.append(dict(f3a_list[idx]))
                seen_c.add(f3a_list[idx]["ts_code"])
            if idx < len(f2a_list) and f2a_list[idx]["ts_code"] not in seen_c:
                raw_exec.append(dict(f2a_list[idx]))
                seen_c.add(f2a_list[idx]["ts_code"])

    exec_queue: list[dict[str, Any]] = []
    seen_inds_exec: set[str] = set()
    rank_exec = 0
    for item in raw_exec:
        ind = item["industry"]
        dup = (ind != "未分类") and (ind in seen_inds_exec)
        if not dup:
            seen_inds_exec.add(ind)
            rank_exec += 1
        item_copy = dict(item)
        item_copy["industry_duplicate"] = dup
        item_copy["slot_rank"] = None if dup else rank_exec
        item_copy["suggested_weight_pct"] = (
            slot_w_pct if (not dup and rank_exec <= max_slots and is_offensive) else 0.0
        )
        exec_queue.append(item_copy)
        if len(exec_queue) >= 10:
            break

    # 7. 滚动回算近 20 个已验证交易日（T-25 ~ T-6）F2A vs F3A 的真实 5 日赚钱效应
    all_dts = sorted(kline["trade_date"].astype(str).unique())
    eval_dts = all_dts[-26:-6] if len(all_dts) >= 26 else all_dts[:-6]

    def _eval_money_effect(sel_fn) -> dict[str, Any]:
        rets: list[float] = []
        active_days = 0
        for d in eval_dts:
            sl = kline.loc[kline["trade_date"] == d]
            cands = sel_fn(sl)
            if cands.empty:
                continue
            # 每日取单行业去重前 4 只评估 T+1开盘 -> T+6收盘真实收益
            picked_r: list[float] = []
            seen_i: set[str] = set()
            for r in cands.head(12).itertuples(index=False):
                ind = str(r.industry)
                if ind != "未分类" and ind in seen_i:
                    continue
                if pd.notna(r.fwd_ret_5d_pct):
                    picked_r.append(float(r.fwd_ret_5d_pct))
                    seen_i.add(ind)
                if len(picked_r) >= 4:
                    break
            if picked_r:
                active_days += 1
                rets.extend(picked_r)
        if not rets:
            return {"samples": 0, "active_days": 0, "win_rate_pct": None, "avg_5d_ret_pct": None, "median_5d_ret_pct": None}
        arr = np.array(rets, dtype=float)
        return {
            "samples": int(len(arr)),
            "active_days": int(active_days),
            "win_rate_pct": round(float((arr > 0).mean() * 100.0), 1),
            "avg_5d_ret_pct": round(float(arr.mean()), 2),
            "median_5d_ret_pct": round(float(np.median(arr)), 2),
        }

    me_f2a = _eval_money_effect(_select_f2a)
    me_f3a = _eval_money_effect(_select_f3a)

    # 客观赚钱效应切换建议
    r_f2a = me_f2a.get("avg_5d_ret_pct")
    r_f3a = me_f3a.get("avg_5d_ret_pct")
    if r_f2a is not None and r_f3a is not None:
        diff = r_f2a - r_f3a
        if r_f2a < 0 and r_f3a < 0:
            suggested_mode = "ab_blend"
            me_headline = (
                f"近20日两条线5日均收益均偏弱（F2A {r_f2a:+.2f}% vs F3A {r_f3a:+.2f}%），"
                "赚钱效应退潮阶段切勿重仓追高，优先执行 #01 跨资产 ETF 防守。"
            )
        elif diff >= 1.2:
            suggested_mode = "f2a_trend"
            me_headline = (
                f"近20日 F2A 业绩趋势主升明显领跑（5日均 {r_f2a:+.2f}% / 胜率 {me_f2a['win_rate_pct']}% vs "
                f"F3A {r_f3a:+.2f}%），若大盘处于进攻期可侧重 F2A 或维持 AB 混合。"
            )
        elif diff <= -1.2:
            suggested_mode = "f3a_pullback"
            me_headline = (
                f"近20日 F3A 绩优回踩低吸明显占优（5日均 {r_f3a:+.2f}% / 胜率 {me_f3a['win_rate_pct']}% vs "
                f"F2A {r_f2a:+.2f}%），追高易吃面，进攻期建议侧重 F3A 低吸或维持 AB 混合。"
            )
        else:
            suggested_mode = "ab_blend"
            me_headline = (
                f"近20日两条线赚钱效应接近（F2A {r_f2a:+.2f}% vs F3A {r_f3a:+.2f}%），"
                "推荐保持默认「AB 混合（全能卡玛王）」交替填槽。"
            )
    else:
        suggested_mode = "ab_blend"
        me_headline = "近期有效样本较少，默认保持「AB 混合（全能卡玛王）」。"

    return {
        "available": True,
        "latest_date": latest_dt,
        "is_offensive": is_offensive,
        "stock_budget_active_pct": stock_budget_cap_pct if is_offensive else 0,
        "slot_weight_pct": slot_w_pct if is_offensive else 0.0,
        "max_slots": max_slots,
        "f2a_total_hits": int(len(f2a_df)),
        "f3a_total_hits": int(len(f3a_df)),
        "exec_queue": exec_queue,
        "f2a_candidates": f2a_list,
        "f3a_candidates": f3a_list,
        "money_effect_20d": {
            "window": f"{eval_dts[0]} ~ {eval_dts[-1]}" if eval_dts else "",
            "f2a": me_f2a,
            "f3a": me_f3a,
            "suggested_mode": suggested_mode,
            "headline": me_headline,
        },
    }


def build_ar3_dual_core_radar(
    *,
    market_gate: Optional[Mapping[str, Any]] = None,
    panic_signal: Optional[Mapping[str, Any]] = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """构建 AR3 v3.0 终极双核实时雷达（接入 AR4 国家队 250 日分位 + ERP 动态股仓上限）。"""
    from services.mining.ar4_macro_service import get_ar4_macro_snapshot

    cfg = load_ar3_config()
    regime = str((market_gate or {}).get("regime") or "balanced")
    panic_flag = bool((panic_signal or {}).get("triggered"))
    dyn_enabled = bool(cfg.get("enable_macro_dynamic_cap", True))
    cache_key = f"{regime}|{panic_flag}|{cfg['active_stock_mode']}|{cfg['etf_core_mode']}|{cfg['stock_budget_cap_pct']}|{dyn_enabled}|{cfg['max_slots']}"

    now_mono = time.monotonic()
    if (
        not force_refresh
        and _RADAR_CACHE["payload"] is not None
        and _RADAR_CACHE["key"] == cache_key
        and now_mono < float(_RADAR_CACHE["expires_at"])
    ):
        return _RADAR_CACHE["payload"]

    macro_snap = get_ar4_macro_snapshot(
        base_stock_cap_pct=int(cfg["stock_budget_cap_pct"]),
        enable_dynamic=dyn_enabled,
        force_refresh=force_refresh,
    )
    macro_dec = macro_snap.get("decision") or {}
    effective_stock_cap = int(macro_dec.get("effective_stock_cap_pct", cfg["stock_budget_cap_pct"]))

    is_offensive = regime in ("offensive", "extreme_offensive")
    etf_radar = _build_etf_radar(
        regime=regime,
        etf_core_mode=cfg["etf_core_mode"],
        stock_budget_cap_pct=effective_stock_cap,
        panic_signal=panic_signal,
    )
    stock_radar = _scan_f2a_f3a_and_money_effect(
        active_stock_mode=cfg["active_stock_mode"],
        max_slots=cfg["max_slots"],
        stock_budget_cap_pct=effective_stock_cap,
        is_offensive=is_offensive and effective_stock_cap > 0,
    )

    cfg_with_eff = dict(cfg)
    cfg_with_eff["effective_stock_budget_cap_pct"] = effective_stock_cap
    cfg_with_eff["dynamic_cap_reason"] = str(macro_dec.get("cap_reason") or "")

    payload: dict[str, Any] = {
        "version": "AR3 v3.0 + AR4 Macro",
        "generated_at": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "regime": regime,
        "is_offensive": is_offensive,
        "config": cfg_with_eff,
        "macro_dashboard": macro_snap,
        "stock_mode_options": [
            {"id": k, **v} for k, v in STOCK_MODE_META.items()
        ],
        "etf_mode_options": [
            {"id": k, **v} for k, v in ETF_MODE_META.items()
        ],
        "evidence": {
            "ab_blend_01_ar4": {"cum_ret_pct": 239.3, "ann_ret_pct": 37.0, "max_dd_pct": 23.0, "sharpe": 1.27, "calmar": 1.61, "oos_pct": 4.8},
            "ab_blend_01": {"cum_ret_pct": 218.6, "ann_ret_pct": 34.8, "max_dd_pct": 20.8, "sharpe": 1.25, "calmar": 1.67, "oos_pct": 4.1},
            "f2a_trend_01": {"cum_ret_pct": 220.9, "ann_ret_pct": 35.0, "max_dd_pct": 24.9, "sharpe": 1.26, "calmar": 1.41, "oos_pct": 10.4},
            "f3a_pullback_01": {"cum_ret_pct": 185.2, "ann_ret_pct": 31.0, "max_dd_pct": 23.5, "sharpe": 1.15, "calmar": 1.32, "oos_pct": 16.4},
            "etf_only_01": {"cum_ret_pct": 157.1, "ann_ret_pct": 27.5, "max_dd_pct": 22.9, "sharpe": 1.05, "calmar": 1.20, "oos_pct": 8.1},
        },
        "etf_radar": etf_radar,
        "stock_radar": stock_radar,
    }
    _RADAR_CACHE["key"] = cache_key
    _RADAR_CACHE["expires_at"] = now_mono + 120.0
    _RADAR_CACHE["payload"] = payload
    return payload
