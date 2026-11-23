# -*- coding: utf-8 -*-
"""AR3 v3.0 唯一权威每日持仓 Diff 调仓编译器 + 批量 L2 预填派发 + 影子对账引擎。

核心设计原则：
1. 默认精准锁定主力单账号 ct1曾（account_id='acct1'），严格按该账号真实总资产、可用现金与真实持仓计算，
   绝不混入其他账号资金；
2. 物理单账户零杠杆（总目标仓位 <= 100%）、先卖后买现金守恒（留 1% 佣金/滑点缓冲）；
3. 所有买卖股数精确向下取整为 100 股整数倍（清仓零股除外），同时区分「现有现金立即可买股数」与「卖后总计应买股数」；
4. 统一输出标准 OrderIntent 协议：当前默认走 AiStock_G3_PrefillBridge（严格 prefill_only + allow_submit=False），
   预留 MiniQMT xtquant.xttrader 内存直连适配器字段供后续能力演练；
5. 自动落盘每日计划单至 runtime/ar3_execution_plans/，盘后与真实成交回执做 Shadow Ledger 滑点与完成率对账。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from services.broker.broker_accounts import get_accounts_overview
from services.g3.fill_autosync import dispatch_prefill_order
from services.g3.order_prefill import (
    AR3_CROSS_ASSET_ETF_WHITELIST,
    DEFAULT_MAX_ORDER_AMOUNT,
    evaluate_and_build_prefill_draft,
)
from services.mining.ar3_dual_core_live import build_ar3_dual_core_radar
from services.trading.t_daily_plan import fetch_batch_live_quotes, load_intraday_executions
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_PRIMARY_ACCOUNT_ID = "acct1"  # 广发证券-曾* (ct1曾)
MIN_REBALANCE_GAP_YUAN = 1800.0  # 小于 1800 元的微小差额不折腾换手
CASH_SAFETY_BUFFER = 0.99  # 买单预留 1% 费用与滑点缓冲
SMOOTH_MAX_SELL_PCT_PER_DAY = 30.0  # AR1 平稳减仓步长上限：单日 <= 30% 总资产


def _plans_dir(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("ar3_execution_plans")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _floor_lot(shares: float) -> int:
    if shares < 100:
        return 0
    return int(math.floor(shares / 100.0) * 100)


def _is_etf_code(code6: str) -> bool:
    return code6 in AR3_CROSS_ASSET_ETF_WHITELIST or code6.startswith(("51", "56", "58", "159"))


def _round_px(px: float, is_etf: bool) -> float:
    return round(float(px), 3 if is_etf else 2)


def _extract_account_snapshot(
    overview: Mapping[str, Any],
    account_id: str = DEFAULT_PRIMARY_ACCOUNT_ID,
) -> dict[str, Any]:
    """从双账号采集结果中精准提取目标单账号（默认 acct1 / ct1曾）的资金与持仓。"""
    accounts = overview.get("accounts") or []
    chosen: Optional[Mapping[str, Any]] = None
    available_accounts: list[dict[str, Any]] = []
    for a in accounts:
        if not isinstance(a, Mapping):
            continue
        aid = str(a.get("account_id") or "")
        lbl = str(a.get("label") or aid)
        funds = a.get("funds") if isinstance(a.get("funds"), Mapping) else {}
        available_accounts.append({
            "account_id": aid,
            "label": lbl,
            "total_assets": funds.get("total_assets"),
            "available_cash": funds.get("available"),
            "market_value": funds.get("market_value"),
            "holdings_count": len(a.get("holdings") or []),
        })
        if aid == account_id:
            chosen = a

    if chosen is None and accounts:
        chosen = accounts[0]
        account_id = str(chosen.get("account_id") or account_id)

    if chosen is None:
        return {
            "account_id": account_id,
            "label": "未检测到账户快照",
            "available": False,
            "total_assets": 0.0,
            "available_cash": 0.0,
            "market_value": 0.0,
            "position_pct": 0.0,
            "holdings": [],
            "fills": [],
            "available_accounts": available_accounts,
            "snapshot_at": None,
        }

    funds = chosen.get("funds") if isinstance(chosen.get("funds"), Mapping) else {}
    holdings_out: list[dict[str, Any]] = []
    computed_mv = 0.0
    for h in chosen.get("holdings") or []:
        code6 = str(h.get("code") or "").split(".")[0].strip().zfill(6)
        if not code6 or code6 == "000000":
            continue
        qty = int(float(h.get("quantity") or h.get("shares") or h.get("qty") or 0))
        if qty <= 0:
            continue
        avail = int(float(h.get("available_quantity") if h.get("available_quantity") is not None else qty))
        px = float(h.get("current_price") or h.get("price") or 0.0)
        cost = float(h.get("cost_price") or 0.0)
        mv = float(h.get("market_value") or 0.0)
        if mv <= 0 and px > 0:
            mv = round(px * qty, 2)
        computed_mv += mv
        pnl_pct = round((px / cost - 1.0) * 100.0, 2) if (px > 0 and cost > 0) else None
        holdings_out.append({
            "code": code6,
            "name": str(h.get("name") or code6),
            "shares": qty,
            "available_shares": max(0, min(qty, avail)),
            "current_price": px,
            "cost_price": cost,
            "market_value": round(mv, 2),
            "profit_pct": pnl_pct,
            "is_etf": _is_etf_code(code6),
        })

    avail_cash = float(funds.get("available") or 0.0)
    mv_total = float(funds.get("market_value") or computed_mv)
    total_assets = float(funds.get("total_assets") or (avail_cash + mv_total))
    pos_pct = round((mv_total / total_assets) * 100.0, 1) if total_assets > 0 else 0.0

    return {
        "account_id": str(chosen.get("account_id") or account_id),
        "label": str(chosen.get("label") or account_id),
        "available": True,
        "total_assets": round(total_assets, 2),
        "available_cash": round(avail_cash, 2),
        "market_value": round(mv_total, 2),
        "position_pct": pos_pct,
        "holdings": holdings_out,
        "fills": list(chosen.get("fills") or []),
        "available_accounts": available_accounts,
        "snapshot_at": chosen.get("generated_at"),
    }


def _split_l2_suborders(
    *,
    code: str,
    name: str,
    side: str,
    price: float,
    total_shares: int,
    max_order_amount: float = DEFAULT_MAX_ORDER_AMOUNT,
) -> list[dict[str, Any]]:
    """若单笔委托超过 L2 单笔金额上限，自动拆分为合规的 100 股整数倍子单。"""
    if total_shares <= 0 or price <= 0:
        return []
    if total_shares * price <= max_order_amount:
        return [{
            "code": code,
            "name": name,
            "side": side,
            "price": price,
            "shares": total_shares,
            "est_amount": round(total_shares * price, 2),
        }]
    max_lots = _floor_lot((max_order_amount * 0.98) / price)
    if max_lots < 100:
        max_lots = 100
    suborders: list[dict[str, Any]] = []
    rem = total_shares
    while rem >= 100:
        chunk = min(rem, max_lots)
        chunk = _floor_lot(chunk) if rem > max_lots else rem
        if chunk <= 0:
            break
        suborders.append({
            "code": code,
            "name": name,
            "side": side,
            "price": price,
            "shares": chunk,
            "est_amount": round(chunk * price, 2),
        })
        rem -= chunk
        if len(suborders) >= 8:
            break
    return suborders


def compile_ar3_rebalance_plan(
    *,
    market_gate: Mapping[str, Any],
    panic_signal: Optional[Mapping[str, Any]] = None,
    account_id: str = DEFAULT_PRIMARY_ACCOUNT_ID,
    smooth_step: bool = True,
    persist: bool = True,
    accounts_overview: Optional[Mapping[str, Any]] = None,
    ar3_radar: Optional[Mapping[str, Any]] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """编译唯一权威「AR3 v3.0 今日精确买卖调仓单（主力单账号 ct1曾）」。"""
    now = datetime.now(BJ_TZ)
    today_str = now.strftime("%Y-%m-%d")
    ymd = now.strftime("%Y%m%d")

    if accounts_overview is not None:
        overview = accounts_overview
    else:
        overview = get_accounts_overview()
        if account_id == DEFAULT_PRIMARY_ACCOUNT_ID:
            try:
                from services.g3.acct1_intraday_advisor import apply_manual_fills_overlay

                overview, _ = apply_manual_fills_overlay(overview, today_str)
            except Exception:
                pass
    acct = _extract_account_snapshot(overview, account_id=account_id)
    radar = (
        dict(ar3_radar)
        if ar3_radar is not None
        else build_ar3_dual_core_radar(market_gate=market_gate, panic_signal=panic_signal)
    )

    cfg = radar.get("config") or {}
    etf_radar = radar.get("etf_radar") or {}
    stock_radar = radar.get("stock_radar") or {}
    regime = str(radar.get("regime") or market_gate.get("regime") or "defensive")
    is_offensive = bool(radar.get("is_offensive"))
    max_slots = int(cfg.get("max_slots") or 4)
    raw_eff_cap = cfg.get("effective_stock_budget_cap_pct")
    stock_cap_pct = float(raw_eff_cap if raw_eff_cap is not None else (cfg.get("stock_budget_cap_pct") or 50))
    slot_w_pct = stock_cap_pct / max(1, max_slots)

    total_assets = float(acct["total_assets"])
    avail_cash = float(acct["available_cash"])
    holdings_by_code = {h["code"]: h for h in acct["holdings"]}

    # 1. 构建 AR3 v3.0 物理单账户目标权重表（总和硬锁死 <= 100.0%）
    target_map: dict[str, dict[str, Any]] = {}
    stock_used_pct = 0.0

    if is_offensive and stock_cap_pct > 0 and stock_radar.get("available"):
        # 防频繁换手：若现有持仓已在 F2A/F3A 候选池内且未触发 -10% 硬止损，优先保留槽位
        f2a_pool = {c["code"]: c for c in (stock_radar.get("f2a_candidates") or [])}
        f3a_pool = {c["code"]: c for c in (stock_radar.get("f3a_candidates") or [])}
        all_pool = {**f3a_pool, **f2a_pool}
        kept_inds: set[str] = set()
        slots_taken = 0

        for h in acct["holdings"]:
            if h["is_etf"]:
                continue
            cand = all_pool.get(h["code"])
            if cand and (h["profit_pct"] is None or h["profit_pct"] > -10.0) and slots_taken < max_slots:
                target_map[h["code"]] = {
                    "code": h["code"],
                    "name": cand["name"],
                    "asset_kind": "stock",
                    "target_weight_pct": round(slot_w_pct, 2),
                    "ref_price": float(cand["close"]),
                    "stop_loss_price": round(float(cand["close"]) * 0.90, 2),
                    "reason": f"已在持仓且仍在 {cand['source_label']} 池内（防换手保留槽位）",
                    "source_label": cand["source_label"],
                    "notice_date": cand.get("notice_date"),
                    "net_profit_yoy_pct": cand.get("net_profit_yoy_pct"),
                    "qdii_notice": None,
                }
                if cand.get("industry") and cand["industry"] != "未分类":
                    kept_inds.add(str(cand["industry"]))
                slots_taken += 1
                stock_used_pct += slot_w_pct

        for cand in stock_radar.get("exec_queue") or []:
            if slots_taken >= max_slots:
                break
            c6 = str(cand["code"])
            ind = str(cand.get("industry") or "未分类")
            if c6 in target_map:
                continue
            if ind != "未分类" and ind in kept_inds:
                continue
            target_map[c6] = {
                "code": c6,
                "name": cand["name"],
                "asset_kind": "stock",
                "target_weight_pct": round(slot_w_pct, 2),
                "ref_price": float(cand["close"]),
                "stop_loss_price": float(cand["stop_loss_price"]),
                "reason": f"{cand['source_label']} 优先第 {slots_taken + 1} 槽（公告 {cand.get('notice_date')} · 净利+{cand.get('net_profit_yoy_pct')}%）",
                "source_label": cand["source_label"],
                "notice_date": cand.get("notice_date"),
                "net_profit_yoy_pct": cand.get("net_profit_yoy_pct"),
                "qdii_notice": None,
            }
            if ind != "未分类":
                kept_inds.add(ind)
            slots_taken += 1
            stock_used_pct += slot_w_pct

    # 剩余全部预算（含股票未满槽闲置现金 + 非进攻期 100%）分配给跨资产 ETF 目标篮子
    etf_budget_pct = max(0.0, 100.0 - stock_used_pct)
    etf_targets = etf_radar.get("targets") or []
    for item in etf_targets:
        c6 = str(item["code"])
        bw = float(item.get("basket_weight_pct") or 0.0) / 100.0
        w_pct = round(etf_budget_pct * bw, 2)
        if w_pct <= 0.5:
            continue
        is_panic_etf = bool(etf_radar.get("panic_triggered")) and c6 in ("159915", "512100")
        cl = float(item.get("close") or 0.0)
        stop_px = round(cl * 0.94, 3) if is_panic_etf else round(float(item.get("ma20") or cl * 0.95), 3)
        target_map[c6] = {
            "code": c6,
            "name": str(item["name"]),
            "asset_kind": "etf",
            "entry_rule": "panic_rebound_etf" if is_panic_etf else "trend_above_ma20",
            "is_panic_etf": is_panic_etf,
            "target_weight_pct": w_pct,
            "ref_price": cl,
            "stop_loss_price": stop_px,
            "reason": str(item.get("reason") or "AR4.5-L3b 跨资产 ETF 轮动目标"),
            "source_label": str(item.get("asset_class_label") or "跨资产ETF"),
            "notice_date": None,
            "net_profit_yoy_pct": None,
            "qdii_notice": item.get("qdii_notice"),
        }

    # 尝试补充最新实时行情价（盘中更准，盘后静默回退收盘价）
    all_need_codes = sorted(set(holdings_by_code.keys()) | set(target_map.keys()))
    live_quotes: dict[str, dict[str, Any]] = {}
    try:
        if all_need_codes:
            live_quotes = fetch_batch_live_quotes(all_need_codes)
    except Exception:
        live_quotes = {}

    def _eff_price(code6: str, fallback_px: float) -> float:
        q = live_quotes.get(code6) or {}
        p = q.get("price")
        if p and float(p) > 0:
            return float(p)
        return float(fallback_px)

    # 2. 第一步 Diff：生成「卖出指令单」（严格受 available_shares 与平稳减仓步长约束）
    sell_orders: list[dict[str, Any]] = []
    hold_keep_list: list[dict[str, Any]] = []

    # 优先处置：硬止损破位 / 非目标个股 / 浮亏最重
    def _sell_prio(h: Mapping[str, Any]) -> tuple[int, float]:
        in_tgt = h["code"] in target_map
        pnl = float(h["profit_pct"]) if h["profit_pct"] is not None else 0.0
        prio = 0
        if not in_tgt:
            prio += 100
        if not h["is_etf"] and not is_offensive:
            prio += 50
        if pnl <= -10.0:
            prio += 40
        return (-prio, pnl)

    smooth_budget_yuan = (
        total_assets * (SMOOTH_MAX_SELL_PCT_PER_DAY / 100.0) if (smooth_step and total_assets > 0) else float("inf")
    )
    rem_smooth_yuan = smooth_budget_yuan

    for h in sorted(acct["holdings"], key=_sell_prio):
        c6 = h["code"]
        px = _eff_price(c6, h["current_price"])
        if px <= 0:
            continue
        cur_mv = h["shares"] * px
        tgt = target_map.get(c6)
        tgt_yuan = total_assets * (float(tgt["target_weight_pct"]) / 100.0) if (tgt and total_assets > 0) else 0.0
        excess_yuan = cur_mv - tgt_yuan

        if excess_yuan <= MIN_REBALANCE_GAP_YUAN:
            hold_keep_list.append({
                "code": c6,
                "name": h["name"],
                "shares": h["shares"],
                "market_value": round(cur_mv, 2),
                "target_weight_pct": tgt["target_weight_pct"] if tgt else 0.0,
                "action": "HOLD",
                "reason": "已在目标仓位区间内，继续持有",
            })
            continue

        avail_sh = int(h["available_shares"])
        if avail_sh <= 0:
            sell_orders.append({
                "code": c6,
                "name": h["name"],
                "is_etf": h["is_etf"],
                "side": "SELL",
                "order_type": "base_rebalance",
                "current_shares": h["shares"],
                "available_shares": 0,
                "full_clear_shares": 0,
                "suggested_shares": 0,
                "limit_price": _round_px(px * 0.998, h["is_etf"]),
                "ref_price": _round_px(px, h["is_etf"]),
                "est_amount": 0.0,
                "profit_pct": h["profit_pct"],
                "reason": "非目标持仓但今日 T+1 可卖股数为 0，待下一交易日卖出",
                "blocked_t1": True,
                "suborders": [],
            })
            continue

        # 一步到位应卖股数（清仓全卖可用，减仓按整百）
        if tgt_yuan <= 0:
            full_sh = avail_sh
        else:
            full_sh = min(avail_sh, _floor_lot(excess_yuan / px))
            if full_sh == 0 and excess_yuan >= px * 100:
                full_sh = min(avail_sh, 100)

        # 平稳步长约束（单日 <= 30% 总资产），小市值零头（<= 10% 总资产如 5855 元的鼎龙股份）直接清掉不留尾巴
        if smooth_step and total_assets > 0:
            if cur_mv <= total_assets * 0.12 and tgt_yuan <= 0:
                sug_sh = full_sh
            else:
                cap_sh = _floor_lot(max(0.0, rem_smooth_yuan) / px)
                sug_sh = min(full_sh, cap_sh)
                if sug_sh == 0 and rem_smooth_yuan >= px * 100 and full_sh >= 100:
                    sug_sh = 100
        else:
            sug_sh = full_sh

        est_amt = round(sug_sh * px, 2)
        if smooth_step and sug_sh > 0:
            rem_smooth_yuan = max(0.0, rem_smooth_yuan - est_amt)

        limit_px = _round_px(px * 0.998, h["is_etf"])  # 卖单让价 0.2% 保障成交
        why_parts: list[str] = []
        if tgt is None:
            why_parts.append("非 AR4.5-L3b 当前目标标的")
        else:
            why_parts.append(f"超配（现 {round(cur_mv / total_assets * 100, 1)}% > 目标 {tgt['target_weight_pct']}%）")
        if h["profit_pct"] is not None and h["profit_pct"] <= -10.0:
            why_parts.append(f"已破-10%硬止损({h['profit_pct']:+.1f}%)")
        if smooth_step and sug_sh < full_sh:
            why_parts.append(f"按单日≤30%平稳步长分批减仓（可切一步到位清 {full_sh} 股）")

        sell_orders.append({
            "code": c6,
            "name": h["name"],
            "is_etf": h["is_etf"],
            "side": "SELL",
            "order_type": "base_rebalance",
            "current_shares": h["shares"],
            "available_shares": avail_sh,
            "full_clear_shares": full_sh,
            "suggested_shares": sug_sh,
            "limit_price": limit_px,
            "ref_price": _round_px(px, h["is_etf"]),
            "est_amount": est_amt,
            "profit_pct": h["profit_pct"],
            "reason": "；".join(why_parts),
            "blocked_t1": False,
            "suborders": _split_l2_suborders(
                code=c6, name=h["name"], side="SELL", price=limit_px, total_shares=sug_sh
            ),
        })

    est_total_sell_proceeds = round(sum(o["est_amount"] for o in sell_orders), 2)

    # 3. 第二步 Diff：生成「买入指令单」（严格现金守恒：区分现有现金立即可买 & 卖后总计应买）
    buy_orders: list[dict[str, Any]] = []
    immediate_pool = max(0.0, avail_cash * CASH_SAFETY_BUFFER)
    post_sell_pool = max(0.0, (avail_cash + est_total_sell_proceeds) * CASH_SAFETY_BUFFER)

    raw_gaps: list[tuple[str, dict[str, Any], float, float]] = []
    for c6, tgt in target_map.items():
        px = _eff_price(c6, tgt["ref_price"])
        if px <= 0:
            continue
        cur_mv = (holdings_by_code[c6]["shares"] * px) if c6 in holdings_by_code else 0.0
        tgt_yuan = total_assets * (float(tgt["target_weight_pct"]) / 100.0) if total_assets > 0 else 0.0
        gap_yuan = tgt_yuan - cur_mv
        if gap_yuan >= MIN_REBALANCE_GAP_YUAN:
            raw_gaps.append((c6, tgt, px, gap_yuan))

    # 优先给股票槽位或最大缺口分配资金，若总缺口 > post_sell_pool 则按比例缩放保持严格零杠杆
    total_gap = sum(x[3] for x in raw_gaps)
    scale_imm = min(1.0, immediate_pool / total_gap) if total_gap > 0 else 0.0
    scale_post = min(1.0, post_sell_pool / total_gap) if total_gap > 0 else 0.0

    for c6, tgt, px, gap_yuan in sorted(raw_gaps, key=lambda x: (0 if x[1]["asset_kind"] == "stock" else 1, -x[3])):
        is_etf = tgt["asset_kind"] == "etf"
        limit_px = _round_px(px * 1.002, is_etf)  # 买单溢价 0.2% 保障成交且符合 L2 0.5% 偏离限制
        imm_sh = _floor_lot((gap_yuan * scale_imm) / limit_px)
        post_sh = _floor_lot((gap_yuan * scale_post) / limit_px)
        if post_sh < 100 and imm_sh < 100:
            continue
        buy_orders.append({
            "code": c6,
            "name": tgt["name"],
            "asset_kind": tgt["asset_kind"],
            "entry_rule": tgt.get("entry_rule") or ("offensive_stock" if not is_etf else "trend_above_ma20"),
            "is_panic_etf": bool(tgt.get("is_panic_etf")),
            "is_etf": is_etf,
            "side": "BUY",
            "order_type": "base_rebalance",
            "target_weight_pct": tgt["target_weight_pct"],
            "source_label": tgt["source_label"],
            "ref_price": _round_px(px, is_etf),
            "limit_price": limit_px,
            "stop_loss_price": tgt["stop_loss_price"],
            "immediate_shares": imm_sh,
            "est_immediate_amount": round(imm_sh * limit_px, 2),
            "post_sell_shares": post_sh,
            "est_post_sell_amount": round(post_sh * limit_px, 2),
            "suggested_shares": post_sh if est_total_sell_proceeds > 0 else imm_sh,
            "reason": tgt["reason"],
            "qdii_notice": tgt.get("qdii_notice"),
            "suborders_immediate": _split_l2_suborders(
                code=c6, name=tgt["name"], side="BUY", price=limit_px, total_shares=imm_sh
            ),
            "suborders_post_sell": _split_l2_suborders(
                code=c6, name=tgt["name"], side="BUY", price=limit_px, total_shares=post_sh
            ),
        })

    # 4. 标准化 OrderIntent（统一协议：先卖后买，供 L2.5 同花顺批量预填 & 未来 MiniQMT 适配器共用）
    order_intents: list[dict[str, Any]] = []
    seq = 1
    for so in sell_orders:
        if so["suggested_shares"] > 0:
            order_intents.append({
                "seq": seq,
                "phase": "1_SELL_FIRST",
                "account_id": acct["account_id"],
                "code": so["code"],
                "name": so["name"],
                "side": "SELL",
                "order_type": "base_rebalance",
                "price": so["limit_price"],
                "shares": so["suggested_shares"],
                "est_amount": so["est_amount"],
                "reason": so["reason"],
            })
            seq += 1
    for bo in buy_orders:
        sh = bo["post_sell_shares"] if est_total_sell_proceeds > 0 else bo["immediate_shares"]
        if sh > 0:
            order_intents.append({
                "seq": seq,
                "phase": "2_BUY_AFTER_SELL",
                "account_id": acct["account_id"],
                "code": bo["code"],
                "name": bo["name"],
                "side": "BUY",
                "order_type": "base_rebalance",
                "price": bo["limit_price"],
                "shares": sh,
                "immediate_shares": bo["immediate_shares"],
                "est_amount": round(sh * bo["limit_price"], 2),
                "stop_loss_price": bo["stop_loss_price"],
                "reason": bo["reason"],
            })
            seq += 1

    plan: dict[str, Any] = {
        "schema": "ar3_rebalance_plan/1",
        "date": today_str,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "regime": regime,
        "regime_label": str(market_gate.get("regime_label") or regime),
        "is_offensive": is_offensive,
        "panic_triggered": bool(etf_radar.get("panic_triggered")),
        "smooth_step": smooth_step,
        "config": cfg,
        "account": {k: v for k, v in acct.items() if k != "fills"},
        "cash_flow": {
            "current_available_cash": round(avail_cash, 2),
            "est_sell_proceeds": est_total_sell_proceeds,
            "est_post_sell_cash": round(avail_cash + est_total_sell_proceeds, 2),
            "immediate_buy_budget": round(immediate_pool, 2),
            "post_sell_buy_budget": round(post_sell_pool, 2),
        },
        "target_allocation": list(target_map.values()),
        "sell_orders": sell_orders,
        "buy_orders": buy_orders,
        "hold_keep": hold_keep_list,
        "order_intents": order_intents,
        "money_effect_20d": stock_radar.get("money_effect_20d"),
        "macro_dashboard": radar.get("macro_dashboard"),
        "execution_adapter": {
            "active": "ths_prefill_bridge",
            "mode": "prefill_only",
            "allow_submit": False,
            "qmt_ready_protocol": "ar3_rebalance_plan/1 -> order_intents (适配 MiniQMT xtquant 内存接口演练)",
        },
    }

    if persist and acct["available"]:
        pdir = _plans_dir(base_dir)
        dated = pdir / f"{ymd}_{acct['account_id']}.json"
        latest = pdir / f"latest_{acct['account_id']}.json"
        txt = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
        dated.write_text(txt, encoding="utf-8")
        latest.write_text(txt, encoding="utf-8")

    return plan


def execute_ar3_batch_prefill(
    *,
    account_id: str,
    orders: Sequence[Mapping[str, Any]],
    market_gate: Mapping[str, Any],
    auto_split_l2: bool = True,
    accounts_overview: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """按「先卖后买」顺序对 AR3 调仓委托执行 L2 硬风控预检并派发到同花顺预填桥（严格 prefill_only）。"""
    overview = accounts_overview if accounts_overview is not None else get_accounts_overview()
    acct = _extract_account_snapshot(overview, account_id=account_id)
    h_by_code = {h["code"]: h for h in acct["holdings"]}
    avail_cash = float(acct["available_cash"])

    # 强制先卖后买排序
    ordered = sorted(
        [dict(o) for o in orders if int(o.get("shares") or 0) >= 100 and float(o.get("price") or 0) > 0],
        key=lambda x: (0 if str(x.get("side") or "").upper() == "SELL" else 1),
    )

    codes = sorted({str(o.get("code") or "").split(".")[0].zfill(6) for o in ordered})
    quotes = fetch_batch_live_quotes(codes) if codes else {}

    # 若启用自动拆单，超过 L2 单笔 25,000 元上限的委托拆分为多笔合规子单
    expanded: list[dict[str, Any]] = []
    for o in ordered:
        c6 = str(o.get("code") or "").split(".")[0].zfill(6)
        side = str(o.get("side") or "").upper()
        px = float(o.get("price") or 0.0)
        sh = int(o.get("shares") or 0)
        nm = str(o.get("name") or c6)
        otype = str(o.get("order_type") or "base_rebalance")
        if auto_split_l2 and px * sh > DEFAULT_MAX_ORDER_AMOUNT:
            for sub in _split_l2_suborders(code=c6, name=nm, side=side, price=px, total_shares=sh):
                expanded.append({**sub, "order_type": otype})
        else:
            expanded.append({"code": c6, "name": nm, "side": side, "price": px, "shares": sh, "order_type": otype})

    results: list[dict[str, Any]] = []
    dispatched_count = 0
    blocked_count = 0
    # 模拟同批卖单释放现金（仅在同批先卖后买时，提示或顺延校验）
    sim_cash = avail_cash
    for item in expanded:
        c6 = item["code"]
        h = h_by_code.get(c6) or {}
        live_px = (quotes.get(c6) or {}).get("price") or h.get("current_price") or item["price"]
        check_res = evaluate_and_build_prefill_draft(
            code=c6,
            name=item["name"],
            side=item["side"],
            price=float(item["price"]),
            shares=int(item["shares"]),
            order_type=item["order_type"],
            account_id=acct["account_id"],
            live_price=float(live_px) if live_px else None,
            total_shares=int(h.get("shares") or 0),
            available_shares=int(h.get("available_shares") or 0),
            available_cash=sim_cash,
            market_gate=market_gate,
        )
        row: dict[str, Any] = {
            **item,
            "draft_id": check_res.get("draft_id"),
            "approved": bool(check_res.get("approved")),
            "blockers": check_res.get("blockers") or [],
            "warnings": check_res.get("warnings") or [],
            "dispatched": False,
            "dispatch_detail": None,
        }
        if check_res.get("approved") and check_res.get("draft_id"):
            try:
                disp = dispatch_prefill_order(draft_id=str(check_res["draft_id"]))
                row["dispatched"] = True
                row["dispatch_detail"] = disp
                dispatched_count += 1
                if item["side"] == "SELL":
                    sim_cash += float(item["price"]) * int(item["shares"]) * 0.998
                else:
                    sim_cash = max(0.0, sim_cash - float(item["price"]) * int(item["shares"]))
            except Exception as exc:
                row["dispatch_detail"] = {"error": str(exc)}
                blocked_count += 1
        else:
            blocked_count += 1
        results.append(row)

    return {
        "status": "ok" if dispatched_count > 0 else "blocked",
        "account_id": acct["account_id"],
        "account_label": acct["label"],
        "mode": "prefill_only",
        "allow_submit": False,
        "total_orders": len(expanded),
        "dispatched_count": dispatched_count,
        "blocked_count": blocked_count,
        "results": results,
    }


def reconcile_ar3_shadow_ledger(
    *,
    account_id: str = DEFAULT_PRIMARY_ACCOUNT_ID,
    days: int = 20,
    base_dir: Optional[Path] = None,
    accounts_overview: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Shadow Ledger 对账：核对近 N 日 AR3 计划单 vs 实盘成交回执/台账（完成率与平均滑点）。"""
    pdir = _plans_dir(base_dir)
    files = sorted(pdir.glob(f"20*_{account_id}.json"), reverse=True)[:days]
    overview = accounts_overview if accounts_overview is not None else get_accounts_overview()
    acct = _extract_account_snapshot(overview, account_id=account_id)

    # 聚合券商近期回执 (date, code, side) -> {shares, amount}
    broker_fills: dict[tuple[str, str, str], dict[str, float]] = {}
    for f in acct.get("fills") or []:
        raw_d = str(f.get("date") or "").replace("-", "")
        if len(raw_d) != 8:
            continue
        d_iso = f"{raw_d[:4]}-{raw_d[4:6]}-{raw_d[6:8]}"
        c6 = str(f.get("code") or "").split(".")[0].zfill(6)
        side = str(f.get("side") or "").upper()
        qty = float(f.get("quantity") or 0)
        amt = float(f.get("amount") or (qty * float(f.get("price") or 0)))
        if qty <= 0 or side not in ("BUY", "SELL"):
            continue
        slot = broker_fills.setdefault((d_iso, c6, side), {"shares": 0.0, "amount": 0.0})
        slot["shares"] += qty
        slot["amount"] += amt

    daily_records: list[dict[str, Any]] = []
    tot_planned = 0
    tot_executed = 0
    slippages_bps: list[float] = []

    for fp in files:
        try:
            plan = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        dt = str(plan.get("date") or "")
        intents = plan.get("order_intents") or []
        # 结合当日手工/自动同步的 desk executions
        desk_execs = load_intraday_executions(dt) if dt else []
        exec_map: dict[tuple[str, str], dict[str, float]] = {}
        for (fd, fc, fs), v in broker_fills.items():
            if fd == dt:
                exec_map[(fc, fs)] = dict(v)
        for ex in desk_execs:
            c6 = str(ex.get("code") or "").split(".")[0].zfill(6)
            side = str(ex.get("side") or "").upper()
            if (c6, side) not in exec_map:
                sh = float(ex.get("shares") or 0)
                px = float(ex.get("price") or 0)
                if sh > 0 and px > 0:
                    exec_map[(c6, side)] = {"shares": sh, "amount": sh * px}

        matched_cnt = 0
        items_out: list[dict[str, Any]] = []
        for it in intents:
            c6 = str(it.get("code") or "")
            side = str(it.get("side") or "").upper()
            plan_sh = int(it.get("shares") or 0)
            plan_px = float(it.get("price") or 0.0)
            actual = exec_map.get((c6, side))
            act_sh = int(actual["shares"]) if actual else 0
            act_px = round(actual["amount"] / actual["shares"], 4) if (actual and actual["shares"] > 0) else None
            slip_bps = None
            if act_px and plan_px > 0:
                raw_diff = (act_px - plan_px) / plan_px if side == "BUY" else (plan_px - act_px) / plan_px
                slip_bps = round(raw_diff * 10000.0, 1)
                slippages_bps.append(slip_bps)
            if act_sh > 0:
                matched_cnt += 1
            items_out.append({
                "code": c6,
                "name": it.get("name"),
                "side": side,
                "plan_shares": plan_sh,
                "plan_price": plan_px,
                "actual_shares": act_sh,
                "actual_price": act_px,
                "slippage_bps": slip_bps,
                "status": "MATCHED" if act_sh >= plan_sh * 0.8 else ("PARTIAL" if act_sh > 0 else "UNEXECUTED"),
            })

        tot_planned += len(intents)
        tot_executed += matched_cnt
        daily_records.append({
            "date": dt,
            "regime": plan.get("regime"),
            "planned_orders": len(intents),
            "executed_orders": matched_cnt,
            "completion_pct": round(matched_cnt / len(intents) * 100.0, 1) if intents else 100.0,
            "items": items_out,
        })

    return {
        "schema": "ar3_shadow_reconcile/1",
        "account_id": acct["account_id"],
        "account_label": acct["label"],
        "days_tracked": len(daily_records),
        "total_planned_orders": tot_planned,
        "total_executed_orders": tot_executed,
        "overall_completion_pct": round(tot_executed / tot_planned * 100.0, 1) if tot_planned > 0 else None,
        "avg_slippage_bps": round(sum(slippages_bps) / len(slippages_bps), 1) if slippages_bps else None,
        "daily_records": daily_records,
    }


def format_ar3_plan_wechat_markdown(plan: Mapping[str, Any]) -> str:
    """将 AR3 v3.0 今日精确买卖调仓单格式化为手机端一页式微信推送文案。"""
    acct = plan.get("account") or {}
    cf = plan.get("cash_flow") or {}
    cfg = plan.get("config") or {}
    sells = [o for o in (plan.get("sell_orders") or []) if int(o.get("suggested_shares") or 0) > 0]
    buys = plan.get("buy_orders") or []
    me = plan.get("money_effect_20d") or {}

    lines = [
        f"⚡ **AR4.5-L3b 今日最终买卖单 ({plan.get('date')})**",
        f"• 主力账号：{acct.get('label')}（总资产 ¥{acct.get('total_assets', 0):,.0f}｜可用 ¥{acct.get('available_cash', 0):,.0f}｜现仓位 {acct.get('position_pct', 0)}%）",
        f"• 大盘五态：**{plan.get('regime_label')}**｜个股模式：`{cfg.get('active_stock_mode')}`｜ETF底座：`{cfg.get('etf_core_mode')}`",
        "",
        f"📉 **【第一步·先卖清单】（共 {len(sells)} 笔，预计回款 ¥{cf.get('est_sell_proceeds', 0):,.0f}）**",
    ]
    if not sells:
        lines.append("• 今日无需卖出调仓")
    else:
        for o in sells:
            lines.append(
                f"• 卖 **{o['name']}({o['code']})** `{o['suggested_shares']}股` @ ≤¥{o['limit_price']} "
                f"（约¥{o['est_amount']:,.0f}｜{o['reason']}）"
            )

    lines.append("")
    lines.append(
        f"📈 **【第二步·后买清单】（现有现金可买 ¥{cf.get('immediate_buy_budget', 0):,.0f}｜卖后可买 ¥{cf.get('post_sell_buy_budget', 0):,.0f}）**"
    )
    if not buys:
        lines.append("• 今日无需买入新仓")
    else:
        for o in buys:
            lines.append(
                f"• 买 **{o['name']}({o['code']})** 现可买 `{o['immediate_shares']}股` / 卖后总买 `{o['post_sell_shares']}股` "
                f"@ ≥¥{o['limit_price']}（目标{o['target_weight_pct']}%｜止损参考 ¥{o['stop_loss_price']}）"
            )
            if o.get("qdii_notice"):
                lines.append(f"  ⚠️ {o['qdii_notice']}")

    if me.get("headline"):
        lines.append("")
        lines.append(f"🧭 **近20日赚钱效应**：{me['headline']}")

    lines.append("")
    lines.append("👉 打开作战台 `/g3/desk` 点「一键顺序预填至同花顺」，核对确认即可执行（零自动下单）。")
    return "\n".join(lines)
