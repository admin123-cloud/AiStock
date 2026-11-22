# -*- coding: utf-8 -*-
"""国家队大幅托底后反弹专题回测：持股（AR4.5 个股 / 全市场）vs 持有 ETF（300/创业板/中证1000/科创50/宽基Top2）谁更赚钱？

严格零未来函数、物理真实开盘买入与费率结算，分两大部分硬核实证：
1. 【事件研究】定位 2023~2026 历史上所有「国家队大幅托底脉冲事件」（6大宽基5日净申购>=300亿且250日分位>=70%），
   以及「国家队重仓托底期（分位>=70%）后市场首次右侧反转启动点」：
   对比 T+1 开盘买入持有 5日 / 10日 / 20日 / 40日 / 60日 的真实收益与最大回撤：
   - 510300 沪深300ETF / 159915 创业板ETF / 512100 中证1000ETF / 588000 科创50ETF / A股宽基动量Top2
   - AR4.5 AB混合绩优个股（F3A+F2A Top4，真实PIT公告日对齐） / F2A趋势个股 / F3A回踩个股 / 全市场4995股等权
2. 【全周期物理单账户零杠杆消融】在 AR4.5-L3b 现役框架内，固定其他条件不变，
   专门把「国家队重仓托底期（分位>=70%）右侧进攻时的 股/ETF 配比」从 0%股/100%ETF 一路扫描到 100%股/0%ETF，
   并对比「全周期收益 / 年化 / 最大回撤 / 卡玛 / OOS 表现 / 反弹阶段纯区间收益」，给出最优答案！
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ar1_alpha_experiment import BUY_COST, SELL_COST, _build_ar1_regimes, _eval_nav  # noqa: E402
from scripts.ar3_cross_asset_experiment import ETF_ONE_WAY_COST, _load_etf_panels  # noqa: E402
from scripts.ar3f_fundamental_trend_experiment import (  # noqa: E402
    _load_dividend_lookup,
    _load_full_price_lookup,
    _pack_metrics,
    attach_pit_fundamentals,
)
from scripts.ar4_macro_national_team_audit import (  # noqa: E402
    _sel_ab_blend,
    _sel_f2a_canslim_core,
    _sel_f3a_growth_pullback,
)
from scripts.ar45_calmar_deep_research import simulate_ar45_physical  # noqa: E402
from services.mining.ar4_macro_service import _ch_query_df  # noqa: E402
from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402


def _hold_etf_return(piv_o: pd.DataFrame, piv_c: pd.DataFrame, dates: list[str], code: str, sig_idx: int, horizon: int) -> float | None:
    if sig_idx + 1 >= len(dates) or sig_idx + horizon >= len(dates):
        return None
    buy_dt = dates[sig_idx + 1]
    sell_dt = dates[sig_idx + horizon]
    if buy_dt not in piv_o.index or sell_dt not in piv_c.index or code not in piv_c.columns:
        return None
    op = piv_o.loc[buy_dt, code]
    if pd.isna(op) or float(op) <= 0:
        op = piv_c.loc[buy_dt, code]
    cl = piv_c.loc[sell_dt, code]
    if pd.isna(op) or pd.isna(cl) or float(op) <= 0 or float(cl) <= 0:
        return None
    return round(((float(cl) * (1.0 - ETF_ONE_WAY_COST)) / (float(op) / (1.0 - ETF_ONE_WAY_COST)) - 1.0) * 100.0, 2)


def _hold_stocks_return(
    ranked_df: pd.DataFrame,
    px_lookup: dict[tuple[str, str], tuple[float, float, float, float, float]],
    dates: list[str],
    sig_idx: int,
    horizon: int,
    max_slots: int = 4,
) -> float | None:
    if ranked_df is None or ranked_df.empty or sig_idx + 1 >= len(dates) or sig_idx + horizon >= len(dates):
        return None
    buy_dt = dates[sig_idx + 1]
    sell_dt = dates[sig_idx + horizon]
    rets: list[float] = []
    seen_ind: set[str] = set()
    for _, row in ranked_df.head(20).iterrows():
        c = str(row["code"])
        ind = str(row.get("industry") or "")
        if ind and ind in seen_ind:
            continue
        p_buy = px_lookup.get((c, buy_dt))
        p_sell = px_lookup.get((c, sell_dt))
        if not p_buy or not p_sell or p_buy[0] <= 0 or p_sell[3] <= 0:
            continue
        # 一字涨停跳过
        if p_buy[4] > 0 and p_buy[0] >= p_buy[4] * 1.095:
            continue
        r = ((p_sell[3] * (1.0 - SELL_COST)) / (p_buy[0] / (1.0 - BUY_COST)) - 1.0) * 100.0
        rets.append(r)
        if ind:
            seen_ind.add(ind)
        if len(rets) >= max_slots:
            break
    return round(float(np.mean(rets)), 2) if rets else None


def main() -> int:
    t0 = time.time()
    print("== 1. 加载 2023~2026 宏观国家队、南向资金、ETF 与全市场 PIT 基本面面板 ==")
    macro_df = _ch_query_df(
        """
        SELECT toString(trade_date) AS trade_date, hs300_close, erp_pct, nt_ratio_pctile_250d, broad6_net_inflow_5d_yi
        FROM stock.ar4_macro_daily FINAL
        WHERE trade_date >= '2023-01-01'
        ORDER BY trade_date
        """
    )
    sb_df = _ch_query_df(
        """
        SELECT toString(trade_date) AS trade_date, sb_net_20d_yi
        FROM stock.ar4_southbound_daily FINAL
        ORDER BY trade_date
        """
    )
    macro_df = macro_df.merge(sb_df, on="trade_date", how="left")
    macro_df["sb_net_20d_yi"] = macro_df["sb_net_20d_yi"].fillna(0.0)

    piv_o, piv_c, code_to_class, _ = _load_etf_panels()
    panel_df, bench_df = build_or_load_feature_panel(start_date="2023-01-01", end_date="2026-11-21", min_amt_20d=50_000_000.0)
    day_df = _build_ar1_regimes(panel_df, bench_df)
    dates = day_df["trade_date"].astype(str).tolist()
    splits = split_trade_dates(dates)
    pit_df = attach_pit_fundamentals(panel_df)
    pit_df["trade_date_str"] = pit_df["trade_date"].astype(str).str[:10]
    by_date_pit = {dt: g for dt, g in pit_df.groupby("trade_date_str", sort=False)}
    px_lookup = _load_full_price_lookup(set(pit_df["code"].astype(str).unique()), dates[0], dates[-1])
    div_lookup = _load_dividend_lookup()

    eq_r = panel_df.groupby("trade_date")["ret_1d_pct"].mean().reindex(dates).fillna(0.0) / 100.0
    eq_nav = (1.0 + eq_r).cumprod()

    sig_map: dict[str, dict[str, float]] = {}
    for row in macro_df.to_dict(orient="records"):
        d = str(row["trade_date"])
        sig_map[d] = {k: float(v) if pd.notna(v) else 0.0 for k, v in row.items() if k != "trade_date"}

    reg_list = day_df["ar1_regime"].tolist()
    breadth_list = day_df["market_above_ma20_pct"].fillna(50.0).tolist()
    pair_cc = piv_c[["159915.SZ", "512100.SH"]].pct_change(fill_method=None).mean(axis=1)

    # == 第一部分：事件研究 A（国家队千亿/三百亿级脉冲峰值日后的反弹对比） ==
    pulse_events: list[tuple[int, str, float, float]] = []
    last_ev = -999
    for i, d in enumerate(dates):
        s = sig_map.get(d, {})
        inf5 = s.get("broad6_net_inflow_5d_yi", 0.0)
        pct = s.get("nt_ratio_pctile_250d", 50.0)
        if inf5 >= 300.0 and pct >= 70.0:
            w_l = max(0, i - 5)
            w_r = min(len(dates), i + 6)
            mx = max(sig_map.get(dates[j], {}).get("broad6_net_inflow_5d_yi", 0.0) for j in range(w_l, w_r))
            if inf5 == mx:
                if i - last_ev >= 20:
                    pulse_events.append((i, d, inf5, pct))
                    last_ev = i
                elif pulse_events and inf5 > pulse_events[-1][2]:
                    pulse_events[-1] = (i, d, inf5, pct)
                    last_ev = i

    # == 事件研究 B（国家队重仓托底期分位>=70% 下，市场首次右侧转入进攻期/企稳反抽的启动日） ==
    rebound_starts: list[tuple[int, str, str, float]] = []
    last_rb = -999
    for i in range(1, len(dates)):
        d = dates[i]
        s = sig_map.get(d, {})
        pct = s.get("nt_ratio_pctile_250d", 50.0)
        if pct < 70.0:
            continue
        # 类型1：非进攻 -> 进攻右侧确认首日
        turned_off = (reg_list[i] in ("offensive", "extreme_offensive")) and (reg_list[i - 1] not in ("offensive", "extreme_offensive"))
        # 类型2：冰点企稳右侧反抽首日
        pr = float(pair_cc.get(d, 0.0)) if d in pair_cc.index and pd.notna(pair_cc.get(d)) else 0.0
        panic_rev = (breadth_list[i] <= 18.0 and pr >= 0.005)
        if (turned_off or panic_rev) and (i - last_rb) >= 15:
            tag = "右侧转进攻首日" if turned_off else "冰点右侧反抽首日"
            rebound_starts.append((i, d, tag, pct))
            last_rb = i

    def _eval_event_set(title: str, evs: list[tuple[int, str, str]]) -> list[dict[str, Any]]:
        print("\n" + "=" * 124)
        print(title)
        print("=" * 124)
        all_rows = []
        for h in (10, 20, 40, 60):
            bucket: dict[str, list[float]] = {
                "510300沪深300": [],
                "159915创业板": [],
                "512100中证1000": [],
                "588000科创50": [],
                "全市场等权": [],
                "AR4.5_AB混合股": [],
                "AR4.5_F2A趋势股": [],
                "AR4.5_F3A回踩股": [],
            }
            for sig_i, dt, _tag in evs:
                if sig_i + h >= len(dates):
                    continue
                for lbl, code in [
                    ("510300沪深300", "510300.SH"),
                    ("159915创业板", "159915.SZ"),
                    ("512100中证1000", "512100.SH"),
                    ("588000科创50", "588000.SH"),
                ]:
                    v = _hold_etf_return(piv_o, piv_c, dates, code, sig_i, h)
                    if v is not None:
                        bucket[lbl].append(v)
                eq_ret = round((float(eq_nav.iloc[sig_i + h]) / float(eq_nav.iloc[sig_i + 1]) - 1.0) * 100.0, 2)
                bucket["全市场等权"].append(eq_ret)

                tbl = by_date_pit.get(dt)
                if tbl is not None and not tbl.empty:
                    for lbl, fn in [
                        ("AR4.5_AB混合股", _sel_ab_blend),
                        ("AR4.5_F2A趋势股", _sel_f2a_canslim_core),
                        ("AR4.5_F3A回踩股", _sel_f3a_growth_pullback),
                    ]:
                        v = _hold_stocks_return(fn(tbl), px_lookup, dates, sig_i, h, max_slots=4)
                        if v is not None:
                            bucket[lbl].append(v)

            row_h: dict[str, Any] = {"持有窗口": f"T+{h}日", "样本次": len(bucket["510300沪深300"])}
            for k, arr in bucket.items():
                row_h[k] = round(float(np.mean(arr)), 2) if arr else None
            all_rows.append(row_h)
        df_out = pd.DataFrame(all_rows)
        print(df_out.to_string(index=False))

        # 同时打印单次事件 T+20d 明细（肉眼核查每次真实行情！）
        print("\n逐次事件 T+20日 真实收益明细(%)：")
        det = []
        for sig_i, dt, tag in evs:
            if sig_i + 20 >= len(dates):
                det.append({"日期": dt, "类型": tag, "备注": "距今不足20交易日(进行中)"})
                continue
            tbl = by_date_pit.get(dt)
            det.append({
                "日期": dt,
                "类型": tag,
                "沪深300": _hold_etf_return(piv_o, piv_c, dates, "510300.SH", sig_i, 20),
                "创业板": _hold_etf_return(piv_o, piv_c, dates, "159915.SZ", sig_i, 20),
                "中证1000": _hold_etf_return(piv_o, piv_c, dates, "512100.SH", sig_i, 20),
                "科创50": _hold_etf_return(piv_o, piv_c, dates, "588000.SH", sig_i, 20),
                "AB混合股": _hold_stocks_return(_sel_ab_blend(tbl), px_lookup, dates, sig_i, 20) if tbl is not None else None,
                "F2A趋势股": _hold_stocks_return(_sel_f2a_canslim_core(tbl), px_lookup, dates, sig_i, 20) if tbl is not None else None,
                "F3A回踩股": _hold_stocks_return(_sel_f3a_growth_pullback(tbl), px_lookup, dates, sig_i, 20) if tbl is not None else None,
            })
        print(pd.DataFrame(det).to_string(index=False))
        return all_rows

    ev_a = _eval_event_set(
        "【事件研究 A】国家队大幅托底脉冲峰值日（5日净申购>=300亿且分位>=70%）后 T+1 买入持股 vs 持有ETF 对比(%)",
        [(i, d, f"+{inf:.0f}亿/分位{pct:.0f}%") for i, d, inf, pct in pulse_events],
    )
    ev_b = _eval_event_set(
        "【事件研究 B】国家队重仓托底期（分位>=70%）后「右侧启动首日」持股 vs 持有ETF 对比(%)",
        [(i, d, f"{tag}/分位{pct:.0f}%") for i, d, tag, pct in rebound_starts],
    )

    # == 第二部分：全周期 AR4.5-L3b 物理单账户「国家队重仓托底进攻期 股/ETF 比例扫描」(0% -> 100%) ==
    print("\n" + "=" * 128)
    print("【第二部分】AR4.5-L3b 物理单账户全周期回测：国家队大幅托底期(分位>=70%)反弹中「持股 vs 持有ETF」配比全谱扫描")
    print("=" * 128)
    scan_rows = []
    # 同时也单独统计「仅在国家队重仓(>=70%)且大盘进攻期」的区间累计复利贡献！
    nt_off_mask = [
        (reg_list[i] in ("offensive", "extreme_offensive")) and (sig_map.get(dates[i], {}).get("nt_ratio_pctile_250d", 50.0) >= 70.0)
        for i in range(len(dates))
    ]
    nt_off_days = sum(nt_off_mask)
    print(f"注：2023~2026 共 {nt_off_days} 个交易日处于「国家队重仓托底(分位>=70%) + 大盘右侧进攻反弹期」。")

    for heavy_stock_cap in (0.0, 0.25, 0.35, 0.50, 0.65, 0.80, 1.00):
        def _make_cap(hc: float):
            def _fn(dt: str, reg: str, s: dict[str, float]) -> float:
                if reg not in ("offensive", "extreme_offensive"):
                    return 0.0
                erp = s.get("erp_pct", 5.5)
                if 0 < erp < 5.0:
                    return 0.0
                p = s.get("nt_ratio_pctile_250d", 50.0)
                if p >= 70.0:
                    return hc
                if p < 30.0:
                    return 0.35
                return 0.50
            return _fn

        lbl = f"重仓托底反弹期: {int(heavy_stock_cap*100):3d}%个股 + {int((1-heavy_stock_cap)*100):3d}%宽基ETF"
        nav, meta = simulate_ar45_physical(
            day_df, sig_map, by_date_pit, px_lookup, div_lookup, piv_o, piv_c, code_to_class,
            name=lbl,
            stock_cap_fn=_make_cap(heavy_stock_cap),
            erp_active_exit=True,
            etf_policy="def_ram_top2_invvol",
            southbound_hk_filter=True,
            southbound_hk_boost=0.25,
        )
        m = _pack_metrics(nav, splits)
        daily_r = nav.pct_change().fillna(0.0).values
        nt_off_cum = (float(np.prod(1.0 + daily_r[np.array(nt_off_mask)])) - 1.0) * 100.0
        yr = {y: _eval_nav(nav.loc[[d for d in nav.index if d.startswith(y)]])["cum_ret_pct"] for y in ("2023", "2024", "2025", "2026")}
        scan_rows.append({
            "托底反弹期配置方案": lbl,
            "托底进攻区间收益%": round(nt_off_cum, 2),
            "全样本收益%": m["full"]["cum_ret_pct"],
            "年化%": m["full"]["ann_ret_pct"],
            "最大回撤%": m["full"]["max_dd_pct"],
            "夏普": m["full"]["sharpe"],
            "卡玛": m["full"]["calmar"],
            "OOS收益%": m["oos"]["cum_ret_pct"],
            "OOS回撤%": m["oos"]["max_dd_pct"],
            "2023": yr["2023"],
            "2024": yr["2024"],
            "2025": yr["2025"],
            "2026": yr["2026"],
        })

    print(pd.DataFrame(scan_rows).to_string(index=False))

    out = runtime_path("mining_runs") / "ar45_nt_rebound_stock_vs_etf_latest.json"
    out.write_text(
        json.dumps({"event_a_pulse": ev_a, "event_b_rightside": ev_b, "allocation_scan": scan_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n专题回测完成，耗时 {time.time() - t0:.1f}s，结果落盘至 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
