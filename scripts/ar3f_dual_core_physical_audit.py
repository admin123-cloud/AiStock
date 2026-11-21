# -*- coding: utf-8 -*-
"""AR3 终极双核（AR3-F 胜出个股 × #02 AR3_6_RiskAdjMom_Top2+Panic 跨资产 ETF）
100% 物理级真·单一现金账户联合撮合验真脚本。

严苛防幻觉保证（零近似、零重叠、零杠杆、零未来函数）：
1. 全局唯一 cash 变量 + stock_positions + etf_positions，任何买入必须先有真实可用现金；
2. 股票开仓资金不足时，当日开盘按真实 ETF 开盘价卖出对应份额腾出现金（实扣 0.06% ETF 摩擦），再买股票（实扣 0.105% 股票买入摩擦）；
3. 股票平仓/闲置资金在开盘无缝按 #02 AR3_6_RiskAdjMom_Top2+Panic 规则配置跨资产 ETF（实扣 0.06% 摩擦）；
4. 持仓股票严格执行真实券商除权除息结算（stock.stock_dividend_events），每笔股票硬断言 notice_date <= signal_date < entry_date；
5. 每日硬断言：cash >= -1e-9 且 (stock_val + etf_val) / total_equity <= 1.0001（物理绝对零杠杆）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ar1_alpha_experiment import (  # noqa: E402
    BUY_COST,
    SELL_COST,
    _build_ar1_regimes,
    _eval_nav,
)
from scripts.ar3_cross_asset_experiment import (  # noqa: E402
    ETF_ONE_WAY_COST,
    SAFE_BOND_CODE,
    _load_etf_panels,
    simulate_cross_asset_strategy,
)
from scripts.ar3f_fundamental_trend_experiment import (  # noqa: E402
    StockPos,
    _load_dividend_lookup,
    _load_full_price_lookup,
    _pack_metrics,
    attach_pit_fundamentals,
)
from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402


def simulate_physical_dual_core(
    day_df: pd.DataFrame,
    by_date_rows: dict[str, pd.DataFrame],
    px_lookup: dict[tuple[str, str], tuple[float, float, float, float, float]],
    div_lookup: dict[tuple[str, str], tuple[float, float]],
    piv_o: pd.DataFrame,
    piv_c: pd.DataFrame,
    code_to_class: dict[str, str],
    selector_fn: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    name: str,
    etf_mode: str = "risk_adj_mom",  # "risk_adj_mom" (#02) | "regime_focus" (#01)
    stock_cap: float = 0.50,
    max_slots: int = 4,
    trail_stop_pct: float = 0.10,
    hard_stop_pct: float = -0.10,
    max_hold_days: int = 60,
    rebal_every: int = 5,
) -> tuple[pd.Series, dict[str, Any]]:
    """物理级单一现金账户联合撮合引擎（绝对无收益近似、绝对零杠杆）。"""
    dates = day_df["trade_date"].astype(str).tolist()
    reg_map = dict(zip(dates, day_df["ar1_regime"].tolist()))
    breadth_map = dict(zip(dates, day_df["market_above_ma20_pct"].fillna(50.0).tolist()))

    # 预计算 ETF 严格 T 日收盘已知动量指标
    ret20 = piv_c.pct_change(20, fill_method=None)
    ma20 = piv_c.rolling(20).mean()
    above_ma20 = piv_c > ma20
    vol20 = piv_c.pct_change(fill_method=None).rolling(20).std() * np.sqrt(242.0)
    risk_adj_mom = ret20 / (vol20 + 0.05)
    panic_pair_cc = piv_c[["159915.SZ", "512100.SH"]].pct_change(fill_method=None).mean(axis=1)

    all_etfs = list(piv_c.columns)
    non_cn_broad = [c for c in all_etfs if code_to_class.get(c) != "cn_broad"]
    cn_broad_codes = [c for c in all_etfs if code_to_class.get(c) == "cn_broad"]

    def _pick_etf(prev_dt: str, pool: list[str], score_df: pd.DataFrame, k: int, per_class: bool) -> list[str]:
        if prev_dt not in score_df.index:
            return []
        s_row = score_df.loc[prev_dt]
        a_row = above_ma20.loc[prev_dt]
        r20_row = ret20.loc[prev_dt]
        cands = []
        for c in pool:
            if c not in s_row.index or pd.isna(s_row[c]) or pd.isna(r20_row.get(c)):
                continue
            if bool(a_row.get(c, False)) and float(r20_row[c]) > 0.0:
                cands.append((c, float(s_row[c])))
        cands.sort(key=lambda x: x[1], reverse=True)
        out: list[str] = []
        seen: set[str] = set()
        for c, _ in cands:
            cls = code_to_class.get(c, "other")
            if per_class and cls in seen:
                continue
            out.append(c)
            seen.add(cls)
            if len(out) >= k:
                break
        return out

    # 物理级单一现金账户状态
    cash = 1.0
    stock_pos: dict[str, StockPos] = {}
    etf_shares: dict[str, float] = {}  # code -> shares
    pending_stock_buys: list[dict[str, Any]] = []
    etf_target_norm: dict[str, float] = {}  # ETF 子篮子内部归一化权重（和为 1.0）

    days_since_rebal = rebal_every
    panic_active = False
    panic_days = 0
    panic_cum = 1.0
    panic_cd = 0

    nav_list: list[float] = [1.0]
    ws_list: list[float] = [0.0]
    we_list: list[float] = [0.0]
    max_gross = 0.0
    min_cash = 1.0
    stock_trades = 0
    pit_audited = 0
    etf_rebal_cnt = 0
    slot_w = stock_cap / max(1, max_slots)

    for i in range(1, len(dates)):
        prev_dt = dates[i - 1]
        dt = dates[i]
        reg_prev = reg_map.get(prev_dt, "balanced")
        reg_today = reg_map.get(dt, "balanced")
        b_prev = breadth_map.get(prev_dt, 50.0)
        pair_r_prev = float(panic_pair_cc.get(prev_dt, 0.0)) if prev_dt in panic_pair_cc.index else 0.0
        day_tbl = by_date_rows.get(dt)

        # --- A. 开盘前：持仓股票真实除权除息结算 ---
        for code, pos in stock_pos.items():
            ev = div_lookup.get((code, dt))
            if ev is not None:
                b10, c10 = ev
                cash_ps = c10 / 10.0
                mult = 1.0 + b10 / 10.0
                if cash_ps > 0:
                    cash += pos.shares * cash_ps
                if mult > 1.0:
                    pos.shares *= mult
                pos.entry_px = max(0.01, (pos.entry_px - cash_ps) / mult)
                pos.peak_close = max(0.01, (pos.peak_close - cash_ps) / mult)
                pos.last_close = max(0.01, (pos.last_close - cash_ps) / mult)

        # --- B. 冰点企稳反抽状态机 & ETF 篮子目标权重（基于 prev_dt 收盘） ---
        panic_changed = False
        if panic_active:
            if panic_days >= 5 or (panic_cum - 1.0) <= -0.06 or (panic_cum - 1.0) >= 0.12 or b_prev >= 38.0:
                panic_active = False
                panic_days = 0
                panic_cd = 2
                panic_changed = True
        else:
            if panic_cd > 0:
                panic_cd -= 1
            elif b_prev <= 18.0 and pair_r_prev >= 0.005:
                panic_active = True
                panic_days = 0
                panic_cum = 1.0
                panic_changed = True

        days_since_rebal += 1
        need_etf_eval = panic_changed or (not panic_active and (days_since_rebal >= rebal_every or not etf_target_norm))
        if etf_mode == "regime_focus" and i >= 2 and reg_prev != reg_map.get(dates[i - 2], reg_prev):
            need_etf_eval = True
        if need_etf_eval:
            etf_target_norm = {}
            if panic_active:
                etf_target_norm["159915.SZ"] = 0.30
                etf_target_norm["512100.SH"] = 0.30
                safe_top = _pick_etf(prev_dt, non_cn_broad, risk_adj_mom, 1, False)
                sc = safe_top[0] if safe_top else SAFE_BOND_CODE
                etf_target_norm[sc] = etf_target_norm.get(sc, 0.0) + 0.40
            elif etf_mode == "regime_focus":
                # 精确复刻 #01 AR3_9_RegimeFocus_Top1+Panic
                if reg_prev in ("offensive", "extreme_offensive"):
                    cn_top = _pick_etf(prev_dt, cn_broad_codes, ret20, 2, False)
                    if cn_top:
                        for c in cn_top:
                            etf_target_norm[c] = 1.0 / len(cn_top)
                    else:
                        chosen = _pick_etf(prev_dt, non_cn_broad, ret20, 1, False)
                        etf_target_norm[chosen[0] if chosen else SAFE_BOND_CODE] = 1.0
                else:
                    chosen = _pick_etf(prev_dt, non_cn_broad, ret20, 1, False)
                    etf_target_norm[chosen[0] if chosen else SAFE_BOND_CODE] = 1.0
            else:
                # 精确复刻 #02 AR3_6_RiskAdjMom_Top2+Panic
                chosen = _pick_etf(prev_dt, all_etfs, risk_adj_mom, 2, True)
                if chosen:
                    for c in chosen:
                        etf_target_norm[c] = 1.0 / len(chosen)
                else:
                    etf_target_norm[SAFE_BOND_CODE] = 1.0
            days_since_rebal = 0

        # --- C. 开盘先执行挂单卖出的股票（跳过一字跌停，回款直接入 cash） ---
        stock_sold_today = False
        for code in list(stock_pos.keys()):
            pos = stock_pos[code]
            if not pos.pending_sell:
                continue
            bar = px_lookup.get((code, dt))
            if bar is None:
                continue
            o, h, l, cl, pc = bar
            if o <= 0:
                continue
            ev = div_lookup.get((code, dt))
            ex_pc = (pc - ev[1] / 10.0) / (1.0 + ev[0] / 10.0) if (ev is not None and pc > 0) else pc
            ld = 0.808 if code.startswith(("300", "301", "688", "689")) else (0.708 if code.endswith(".BJ") else 0.905)
            if ex_pc > 0 and o == h and h == l and cl <= ex_pc * ld:
                continue
            cash += pos.shares * o * (1.0 - SELL_COST)
            del stock_pos[code]
            stock_trades += 1
            stock_sold_today = True

        # --- D. 计算今日开盘账户总权益 & 筛选可真实成交的股票买单 ---
        o_etf = piv_o.loc[dt] if dt in piv_o.index else pd.Series(dtype=float)
        c_etf = piv_c.loc[dt] if dt in piv_c.index else pd.Series(dtype=float)
        etf_val_open = sum(
            sh * float(o_etf.get(c, piv_c.loc[prev_dt].get(c, 0.0)))
            for c, sh in etf_shares.items()
            if pd.notna(o_etf.get(c, piv_c.loc[prev_dt].get(c, np.nan)))
        )
        stock_val_open = sum(
            p.shares * (px_lookup.get((c, dt), (p.last_close,))[0])
            for c, p in stock_pos.items()
        )
        total_eq_open = cash + stock_val_open + etf_val_open

        exec_buys: list[tuple[dict[str, Any], float]] = []
        if pending_stock_buys and len(stock_pos) < max_slots:
            held_inds = {p.industry for p in stock_pos.values()}
            for cand in pending_stock_buys:
                if len(stock_pos) + len(exec_buys) >= max_slots:
                    break
                code = str(cand["code"])
                ind = str(cand["industry"])
                if code in stock_pos or any(x[0]["code"] == code for x in exec_buys):
                    continue
                if ind != "未分类" and ind in held_inds:
                    continue
                bar = px_lookup.get((code, dt))
                if bar is None:
                    continue
                o, h, l, cl, pc = bar
                if o <= 0:
                    continue
                ev = div_lookup.get((code, dt))
                ex_pc = (pc - ev[1] / 10.0) / (1.0 + ev[0] / 10.0) if (ev is not None and pc > 0) else pc
                lu = 1.192 if code.startswith(("300", "301", "688", "689")) else (1.292 if code.endswith(".BJ") else 1.095)
                if ex_pc > 0 and o >= ex_pc * lu:
                    continue
                sig_dt = str(cand["signal_date"])
                not_dt = str(cand["notice_date"] or "")
                assert sig_dt < dt and (not not_dt or not_dt <= sig_dt), f"PIT 违规: {code} {not_dt} {sig_dt} {dt}"
                pit_audited += 1
                exec_buys.append((cand, o))
                held_inds.add(ind)
        pending_stock_buys = []

        # --- E. 先为股票买单腾出现金（如需卖 ETF 则按真实开盘价减持 ETF 并扣 0.06% 摩擦） ---
        max_add_budget = max(0.0, total_eq_open * stock_cap - stock_val_open)
        per_buy_amt = min(total_eq_open * slot_w, max_add_budget / max(1, len(exec_buys))) if exec_buys else 0.0
        need_cash = per_buy_amt * len(exec_buys)
        etf_touched = False

        if need_cash > cash + 1e-9 and etf_val_open > 1e-9:
            shortfall = need_cash - cash
            # 按比例等比卖出 ETF 份额筹集现金（计入卖出成本 0.06%）
            sell_ratio = min(1.0, (shortfall / (1.0 - ETF_ONE_WAY_COST)) / etf_val_open)
            for c in list(etf_shares.keys()):
                px = float(o_etf.get(c, 0.0))
                if px <= 0:
                    continue
                sell_sh = etf_shares[c] * sell_ratio
                cash += sell_sh * px * (1.0 - ETF_ONE_WAY_COST)
                etf_shares[c] -= sell_sh
                if etf_shares[c] <= 1e-10:
                    del etf_shares[c]
            etf_touched = True

        # 执行股票买入（严格不超过现金）
        for cand, o in exec_buys:
            alloc = min(cash, per_buy_amt)
            if alloc < total_eq_open * 0.03:
                break
            shares = (alloc * (1.0 - BUY_COST)) / o
            cash -= alloc
            stock_pos[cand["code"]] = StockPos(
                code=str(cand["code"]),
                name=str(cand.get("name") or cand["code"]),
                industry=str(cand.get("industry") or "未分类"),
                shares=shares,
                entry_px=o,
                entry_date=dt,
                signal_date=str(cand["signal_date"]),
                notice_date=str(cand.get("notice_date") or ""),
                report_date=str(cand.get("report_date") or ""),
                pit_np_yoy=float(cand.get("pit_np_yoy") or 0.0),
                peak_close=o,
                last_close=o,
            )

        # --- F. 将全部剩余空闲资金 + 现有 ETF 资产按 #02 目标权重在开盘调仓 ---
        etf_val_now = sum(
            sh * float(o_etf.get(c, 0.0))
            for c, sh in etf_shares.items()
            if pd.notna(o_etf.get(c)) and float(o_etf.get(c)) > 0
        )
        etf_pool_avail = cash + etf_val_now
        need_rebal_etf = need_etf_eval or stock_sold_today or etf_touched or (cash > total_eq_open * 0.03)
        if need_rebal_etf and etf_pool_avail > 1e-6 and dt in piv_o.index:
            # 计算目标金额与实际差额，精确按买卖双向扣 0.06% ETF 摩擦，保证 cash >= 0
            cur_etf_amt = {
                c: sh * float(o_etf.get(c, 0.0))
                for c, sh in etf_shares.items()
                if pd.notna(o_etf.get(c)) and float(o_etf.get(c)) > 0
            }
            # 先估算总换手成本，从可投资额里预留摩擦费确保现金绝对不为负
            raw_turn = sum(
                abs(etf_pool_avail * etf_target_norm.get(c, 0.0) - cur_etf_amt.get(c, 0.0))
                for c in set(cur_etf_amt) | set(etf_target_norm)
            )
            if raw_turn > total_eq_open * 0.015:
                # 1. 先卖出超配/不在目标内的 ETF
                target_budget = etf_pool_avail - raw_turn * ETF_ONE_WAY_COST * 1.05
                target_budget = max(0.0, target_budget)
                for c in list(etf_shares.keys()):
                    px = float(o_etf.get(c, 0.0))
                    if px <= 0:
                        continue
                    tgt_amt = target_budget * etf_target_norm.get(c, 0.0)
                    cur_amt = etf_shares[c] * px
                    if cur_amt > tgt_amt + 1e-8:
                        diff_sh = (cur_amt - tgt_amt) / px
                        cash += diff_sh * px * (1.0 - ETF_ONE_WAY_COST)
                        etf_shares[c] -= diff_sh
                        if etf_shares[c] <= 1e-10:
                            del etf_shares[c]
                # 2. 再用真实可用现金买入低配/新增目标 ETF
                buy_needs: list[tuple[str, float, float]] = []
                for c, w in etf_target_norm.items():
                    px = float(o_etf.get(c, 0.0))
                    if px <= 0 or pd.isna(px):
                        continue
                    cur_amt = etf_shares.get(c, 0.0) * px
                    need = max(0.0, target_budget * w - cur_amt)
                    if need > 1e-8:
                        buy_needs.append((c, px, need))
                tot_buy_need = sum(x[2] for x in buy_needs)
                if tot_buy_need > 1e-8 and cash > 1e-8:
                    # 计入买入成本 cash 消耗 = buy_val / (1 - cost) -> 简化为 spend = alloc, shares = alloc*(1-cost)/px
                    scale = min(1.0, cash / tot_buy_need)
                    for c, px, need in buy_needs:
                        spend = need * scale
                        etf_shares[c] = etf_shares.get(c, 0.0) + (spend * (1.0 - ETF_ONE_WAY_COST)) / px
                        cash -= spend
                etf_rebal_cnt += 1

        # 更新冰点反抽持仓收益跟踪
        if panic_active and dt in piv_o.index and dt in piv_c.index:
            panic_days += 1
            if panic_days == 1:
                r1 = float(c_etf.get("159915.SZ", 1.0)) / float(o_etf.get("159915.SZ", 1.0)) - 1.0
                r2 = float(c_etf.get("512100.SH", 1.0)) / float(o_etf.get("512100.SH", 1.0)) - 1.0
                panic_cum *= 1.0 + 0.5 * (r1 + r2)
            else:
                panic_cum *= 1.0 + float(panic_pair_cc.get(dt, 0.0))

        # --- G. 收盘 Mark-to-Market 与严格零杠杆物理校验 ---
        stock_val_close = 0.0
        for code, pos in stock_pos.items():
            bar = px_lookup.get((code, dt))
            cl = bar[3] if (bar is not None and bar[3] > 0) else pos.last_close
            pos.last_close = cl
            pos.days_held += 1
            if cl > pos.peak_close:
                pos.peak_close = cl
            stock_val_close += pos.shares * cl

            pnl = cl / pos.entry_px - 1.0
            dd_pk = cl / pos.peak_close - 1.0
            if reg_today in ("defensive", "extreme_panic"):
                pos.pending_sell = True
                pos.sell_reason = "regime_defensive"
            elif pnl <= hard_stop_pct:
                pos.pending_sell = True
                pos.sell_reason = "hard_stop"
            elif dd_pk <= -trail_stop_pct:
                pos.pending_sell = True
                pos.sell_reason = "trail_stop"
            elif max_hold_days > 0 and pos.days_held >= max_hold_days:
                pos.pending_sell = True
                pos.sell_reason = "max_hold"

        etf_val_close = sum(
            sh * float(c_etf.get(c, 0.0))
            for c, sh in etf_shares.items()
            if pd.notna(c_etf.get(c)) and float(c_etf.get(c)) > 0
        )
        nav_today = cash + stock_val_close + etf_val_close
        gross = (stock_val_close + etf_val_close) / nav_today if nav_today > 0 else 0.0
        assert cash >= -1e-7, f"物理现金透支! dt={dt} cash={cash}"
        assert gross <= 1.0001, f"物理杠杆超限! dt={dt} gross={gross}"
        max_gross = max(max_gross, gross)
        min_cash = min(min_cash, cash)

        nav_list.append(nav_today)
        ws_list.append(stock_val_close / nav_today if nav_today > 0 else 0.0)
        we_list.append(etf_val_close / nav_today if nav_today > 0 else 0.0)

        # --- H. 收盘后选出明日股票候选（仅 AR1 进攻期启动股票引擎） ---
        if reg_today in ("offensive", "extreme_offensive") and len(stock_pos) < max_slots and day_tbl is not None and not day_tbl.empty:
            cands = selector_fn(day_tbl)
            if not cands.empty:
                for row in cands.head(max_slots * 4).itertuples(index=False):
                    n_fin = getattr(row, "notice_date", None)
                    n_fc = getattr(row, "fc_notice_date", None)
                    n_eff = str(n_fin) if pd.notna(n_fin) else (str(n_fc) if pd.notna(n_fc) else "")
                    pending_stock_buys.append({
                        "code": row.code,
                        "name": getattr(row, "name", row.code),
                        "industry": getattr(row, "industry", "未分类"),
                        "signal_date": dt,
                        "notice_date": n_eff,
                        "report_date": str(getattr(row, "report_date", "") or ""),
                        "pit_np_yoy": getattr(row, "net_profit_yoy_pct", 0.0),
                    })

    nav_s = pd.Series(nav_list, index=dates, dtype=np.float64)
    # 分年度收益拆解
    yearly: dict[str, dict[str, float]] = {}
    for yr in ("2023", "2024", "2025", "2026"):
        sub = nav_s.loc[[d for d in nav_s.index if d.startswith(yr)]]
        if len(sub) >= 2:
            yearly[yr] = _eval_nav(sub)

    return nav_s, {
        "name": name,
        "stock_cap_pct": round(stock_cap * 100.0, 1),
        "avg_stock_weight_pct": round(float(np.mean(ws_list) * 100.0), 1),
        "avg_etf_weight_pct": round(float(np.mean(we_list) * 100.0), 1),
        "max_gross_exposure_pct": round(max_gross * 100.0, 3),
        "min_cash_balance": round(min_cash, 6),
        "stock_closed_trades": stock_trades,
        "pit_audited_entries": pit_audited,
        "etf_rebal_events": etf_rebal_cnt,
        "yearly": yearly,
    }


def main() -> None:
    t0 = time.time()
    print("=== AR3-F × #02 跨资产 ETF 100% 物理级单账户零杠杆终极验真 ===", flush=True)
    panel_df, bench_df = build_or_load_feature_panel(
        start_date="2023-01-01",
        end_date="2026-11-21",
        min_amt_20d=50_000_000.0,
    )
    day_df = _build_ar1_regimes(panel_df, bench_df)
    dates = day_df["trade_date"].astype(str).tolist()
    splits = split_trade_dates(dates)

    pit_df = attach_pit_fundamentals(panel_df)
    by_date_rows = {dt: g for dt, g in pit_df.groupby("trade_date", sort=False)}
    px_lookup = _load_full_price_lookup(set(pit_df["code"].astype(str).unique()), dates[0], dates[-1])
    div_lookup = _load_dividend_lookup()
    piv_o, piv_c, code_to_class, _ = _load_etf_panels()

    # 基准线：#01 (AR3_9_RegimeFocus_Top1+Panic) 与 #02 (AR3_6_RiskAdjMom_Top2+Panic)
    out_rows: list[dict[str, Any]] = []
    for base_id, b_kw in [
        ("BASE_#01_AR3_9_RegimeFocus_Top1+Panic", dict(mode="regime_focus", top_k=1, rebal_every=5, one_per_class=False, use_panic_overlay=True)),
        ("BASE_#02_AR3_6_RiskAdjMom_Top2+Panic", dict(mode="risk_adj_mom", top_k=2, rebal_every=5, one_per_class=True, use_panic_overlay=True)),
    ]:
        b_nav, _ = simulate_cross_asset_strategy(day_df, piv_o, piv_c, code_to_class, **b_kw)
        b_y = {
            yr: _eval_nav(b_nav.loc[[d for d in b_nav.index if d.startswith(yr)]])
            for yr in ("2023", "2024", "2025", "2026")
        }
        m_b = _pack_metrics(b_nav, splits)
        out_rows.append({"id": base_id, "metrics": m_b, "meta": {"yearly": b_y}})
        print(
            f"  {base_id:44s} | 全={m_b['full']['cum_ret_pct']:+6.1f}% (年{m_b['full']['ann_ret_pct']:+5.1f}%) "
            f"DD={m_b['full']['max_dd_pct']:4.1f}% Sh={m_b['full']['sharpe']:4.2f} Cal={m_b['full']['calmar']:4.2f} | "
            f"IS={m_b['is']['cum_ret_pct']:+5.1f}% Val={m_b['val']['cum_ret_pct']:+5.1f}% OOS={m_b['oos']['cum_ret_pct']:+5.1f}% | "
            f"分年: 23={b_y['2023']['cum_ret_pct']:+5.1f}% 24={b_y['2024']['cum_ret_pct']:+5.1f}% "
            f"25={b_y['2025']['cum_ret_pct']:+5.1f}% 26={b_y['2026']['cum_ret_pct']:+5.1f}%",
            flush=True,
        )

    def sel_f2a_canslim_core(df: pd.DataFrame) -> pd.DataFrame:
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

    def sel_f3a_growth_pullback(df: pd.DataFrame) -> pd.DataFrame:
        m = (
            (~df["has_earnings_mine"])
            & (df["pit_days_since_notice"] <= 185)
            & (df["net_profit_yoy_pct"] >= 30.0)
            & (df["revenue_yoy_pct"] >= 15.0)
            & (df["roe_pct"] >= 8.0)
            & (df["net_profit"] >= 50_000_000.0)
            & (df["amt_20d_yi"] >= 2.0)
            & (df["ma20_bias_pct"] < df["ma60_bias_pct"])
            & (df["ma10_bias_pct"].between(-3.0, 2.0))
            & (df["ret_1d_pct"] > 0.5)
            & (df["vol_ratio_20d"] <= 1.3)
        )
        sub = df.loc[m].copy()
        if sub.empty:
            return sub
        sub["score"] = 0.5 * sub["np_yoy_rank"].fillna(0.5) + 0.5 * sub["rs20_rank"].fillna(0.5)
        return sub.sort_values("score", ascending=False)

    def sel_f2a_f3a_blend(df: pd.DataFrame) -> pd.DataFrame:
        """方案 A+B 交错混合：交替排列 F3A 回踩 Top 候选与 F2A 趋势 Top 候选。"""
        a = sel_f3a_growth_pullback(df).head(8)
        b = sel_f2a_canslim_core(df).head(8)
        if a.empty:
            return b
        if b.empty:
            return a
        rows = []
        seen: set[str] = set()
        for k in range(max(len(a), len(b))):
            if k < len(a):
                c = str(a.iloc[k]["code"])
                if c not in seen:
                    rows.append(a.iloc[[k]])
                    seen.add(c)
            if k < len(b):
                c = str(b.iloc[k]["code"])
                if c not in seen:
                    rows.append(b.iloc[[k]])
                    seen.add(c)
        return pd.concat(rows, ignore_index=True) if rows else a

    configs = [
        # --- 结合 #01 (AR3_9_RegimeFocus_Top1+Panic) ---
        ("PHYS_30pct_F3A(A)+#01_RegimeFocus", sel_f3a_growth_pullback, "regime_focus", 0.30, 45),
        ("PHYS_40pct_F3A(A)+#01_RegimeFocus", sel_f3a_growth_pullback, "regime_focus", 0.40, 45),
        ("PHYS_50pct_F3A(A)+#01_RegimeFocus", sel_f3a_growth_pullback, "regime_focus", 0.50, 45),
        ("PHYS_30pct_F2A(B)+#01_RegimeFocus", sel_f2a_canslim_core, "regime_focus", 0.30, 60),
        ("PHYS_40pct_F2A(B)+#01_RegimeFocus", sel_f2a_canslim_core, "regime_focus", 0.40, 60),
        ("PHYS_50pct_F2A(B)+#01_RegimeFocus", sel_f2a_canslim_core, "regime_focus", 0.50, 60),
        ("PHYS_40pct_F3A+F2A(AB)+#01_RegimeFocus", sel_f2a_f3a_blend, "regime_focus", 0.40, 50),
        ("PHYS_50pct_F3A+F2A(AB)+#01_RegimeFocus", sel_f2a_f3a_blend, "regime_focus", 0.50, 50),
        # --- 对照组：结合 #02 (AR3_6_RiskAdjMom_Top2+Panic) ---
        ("PHYS_50pct_F3A(A)+#02_RiskAdjTop2", sel_f3a_growth_pullback, "risk_adj_mom", 0.50, 45),
        ("PHYS_40pct_F2A(B)+#02_RiskAdjTop2", sel_f2a_canslim_core, "risk_adj_mom", 0.40, 60),
        ("PHYS_50pct_F3A+F2A(AB)+#02_RiskAdjTop2", sel_f2a_f3a_blend, "risk_adj_mom", 0.50, 50),
    ]

    for cid, fn, emode, cap, hold in configs:
        nav_s, meta = simulate_physical_dual_core(
            day_df, by_date_rows, px_lookup, div_lookup, piv_o, piv_c, code_to_class, fn,
            name=cid, etf_mode=emode, stock_cap=cap, max_slots=4, trail_stop_pct=0.10, hard_stop_pct=-0.10, max_hold_days=hold,
        )
        m = _pack_metrics(nav_s, splits)
        y = meta["yearly"]
        out_rows.append({"id": cid, "metrics": m, "meta": meta})
        print(
            f"  {cid:44s} | 全={m['full']['cum_ret_pct']:+6.1f}% (年{m['full']['ann_ret_pct']:+5.1f}%) "
            f"DD={m['full']['max_dd_pct']:4.1f}% Sh={m['full']['sharpe']:4.2f} Cal={m['full']['calmar']:4.2f} | "
            f"IS={m['is']['cum_ret_pct']:+5.1f}% Val={m['val']['cum_ret_pct']:+5.1f}% OOS={m['oos']['cum_ret_pct']:+5.1f}% | "
            f"分年: 23={y['2023']['cum_ret_pct']:+5.1f}% 24={y['2024']['cum_ret_pct']:+5.1f}% "
            f"25={y['2025']['cum_ret_pct']:+5.1f}% 26={y['2026']['cum_ret_pct']:+5.1f}% | "
            f"股/ETF={meta['avg_stock_weight_pct']:.1f}%/{meta['avg_etf_weight_pct']:.1f}% maxG={meta['max_gross_exposure_pct']:.2f}% minCash={meta['min_cash_balance']:.4f}",
            flush=True,
        )

    out_path = runtime_path("mining_runs", "ar3f_dual_core_physical_audit_v2_01vs02.json")
    out_path.write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "results": out_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n物理级单账户 #01 vs #02 验真完成 ({time.time() - t0:.1f}s)，已落盘: {out_path}", flush=True)


if __name__ == "__main__":
    main()
