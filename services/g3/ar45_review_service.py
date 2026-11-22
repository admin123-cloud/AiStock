# -*- coding: utf-8 -*-
"""AR4.5-L3b 实盘复盘与真钱归因聚合服务（彻底替代旧 R40/AR1 鸡肋台账）。

核心原则（铁律防造假）：
1. 100% 基于主力账号 ct1曾（默认 acct1）的真实每日账户快照（runtime/ths_read_account/read_multi_*.json）与当前真实持仓/回执，
   绝不虚构历史不存在的账户净值；
2. 净值采用「真实当日盈亏 ÷ 前日总资产」累乘口径（避免银证转入/转出被误算成虚假盈利或亏损）；
3. 同步对齐同期沪深300 基准与 AR4.5-L3b 理想机械执行影子净值，直观暴露实盘与策略之间的执行剪刀差；
4. 将当前持仓与近期成交按三腿清晰拆账：AR4.5 跨资产 ETF 腿 / AR4.5 绩优个股腿 / 历史旧底仓与做T腿；
5. 联动 Shadow Ledger 计划单逐笔对账与 AI 交易教练出血点诊断，一屏完成盘后复盘闭环。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from services.broker.broker_accounts import get_accounts_overview
from services.g3.attribution import build_four_dim_attribution
from services.g3.order_prefill import AR3_CROSS_ASSET_ETF_WHITELIST
from services.g3.trading_coach import build_trading_coach_report
from services.mining.ar3_rebalance_compiler import reconcile_ar3_shadow_ledger
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))


def _safe_float(v: Any) -> Optional[float]:
    if v in (None, "", "-"):
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _read_json_safe(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _norm_date(raw: str) -> Optional[str]:
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", str(raw or ""))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _is_etf_code(code6: str) -> bool:
    return code6 in AR3_CROSS_ASSET_ETF_WHITELIST or code6.startswith(("51", "56", "58", "159"))


def _collect_account_daily_snapshots(
    account_id: str = "acct1",
    days: int = 30,
    snap_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """从 runtime/ths_read_account/read_multi_*.json 提取目标账号每日最新有效资产快照。"""
    base_dir = snap_dir or runtime_path("ths_read_account")
    if not base_dir.exists():
        return []

    cutoff = (datetime.now(BJ_TZ).date() - timedelta(days=max(7, days + 5))).strftime("%Y-%m-%d")
    by_date: dict[str, dict[str, Any]] = {}

    for fp in sorted(base_dir.glob("read_multi_*.json")):
        doc = _read_json_safe(fp)
        if not isinstance(doc, Mapping):
            continue
        gen_at = str(doc.get("generated_at") or fp.stem)
        dt = _norm_date(gen_at)
        if not dt or dt < cutoff:
            continue

        accounts = doc.get("accounts")
        if not isinstance(accounts, Mapping):
            continue
        blk = accounts.get(account_id)
        if not isinstance(blk, Mapping):
            continue
        ra = blk.get("read_account") if isinstance(blk.get("read_account"), Mapping) else blk
        funds = ra.get("funds") if isinstance(ra.get("funds"), Mapping) else {}
        ta = _safe_float(funds.get("total_assets"))
        if ta is None or ta <= 0:
            continue
        mv = _safe_float(funds.get("market_value")) or 0.0
        avail = _safe_float(funds.get("available")) or max(0.0, ta - mv)
        dp = _safe_float(funds.get("daily_profit"))
        hp = _safe_float(funds.get("holding_profit"))
        holdings = ra.get("holdings") if isinstance(ra.get("holdings"), list) else []
        if dp is None and holdings:
            dps = [_safe_float(h.get("daily_profit")) for h in holdings if isinstance(h, Mapping)]
            dps_valid = [x for x in dps if x is not None]
            if dps_valid:
                dp = round(sum(dps_valid), 2)
        if hp is None and holdings:
            hps = [_safe_float(h.get("floating_profit")) for h in holdings if isinstance(h, Mapping)]
            hps_valid = [x for x in hps if x is not None]
            if hps_valid:
                hp = round(sum(hps_valid), 2)

        prev = by_date.get(dt)
        if prev is None or gen_at >= prev["generated_at"]:
            by_date[dt] = {
                "date": dt,
                "generated_at": gen_at,
                "total_assets": round(ta, 2),
                "market_value": round(mv, 2),
                "available_cash": round(avail, 2),
                "position_pct": round(mv / ta * 100.0, 1) if ta > 0 else 0.0,
                "daily_profit": round(dp, 2) if dp is not None else None,
                "holding_profit": round(hp, 2) if hp is not None else None,
                "holdings_count": len(holdings),
            }

    return [by_date[d] for d in sorted(by_date.keys())]


def _fetch_hs300_close_map(start_date: str) -> dict[str, float]:
    """从 ClickHouse ar4_macro_daily 获取沪深300 日收盘价映射，fail-safe。"""
    try:
        from services.mining.ar4_macro_service import _ch_query_df

        df = _ch_query_df(
            f"""
            SELECT toString(trade_date) AS dt, hs300_close
            FROM stock.ar4_macro_daily FINAL
            WHERE trade_date >= '{start_date}' AND hs300_close > 0
            ORDER BY trade_date
            """,
            timeout=10,
        )
        return {str(r["dt"]): float(r["hs300_close"]) for r in df.to_dict(orient="records")}
    except Exception:
        return {}


def _load_ar45_backtest_curve_map() -> dict[str, float]:
    """从已落盘的历史回测报告读取 AR4.5-L3b 理想净值点位映射（按日期前向填充使用）。"""
    rp = runtime_path("mining_runs") / "ar1_historical_backtest_latest.json"
    doc = _read_json_safe(rp)
    if not isinstance(doc, Mapping):
        return {}
    out: dict[str, float] = {}
    for pt in doc.get("curve_points") or []:
        if isinstance(pt, Mapping) and pt.get("date") and pt.get("ar45_l3b") is not None:
            out[str(pt["date"])] = float(pt["ar45_l3b"])
    return out


def _lookup_prev_or_exact(mapping: Mapping[str, float], dt: str) -> Optional[float]:
    if not mapping:
        return None
    if dt in mapping:
        return mapping[dt]
    keys = [k for k in mapping.keys() if k <= dt]
    return mapping[max(keys)] if keys else None


def build_ar45_review_report(
    *,
    account_id: str = "acct1",
    days: int = 30,
    accounts_overview: Optional[Mapping[str, Any]] = None,
    snap_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """构建 AR4.5-L3b 真钱实盘复盘与策略腿归因完整数据包。"""
    now = datetime.now(BJ_TZ)
    overview = accounts_overview if accounts_overview is not None else get_accounts_overview()
    accounts_list = overview.get("accounts") or []
    chosen_acct = next((a for a in accounts_list if str(a.get("account_id")) == account_id), None)
    if chosen_acct is None and accounts_list:
        chosen_acct = accounts_list[0]
        account_id = str(chosen_acct.get("account_id") or account_id)

    acct_label = str((chosen_acct or {}).get("label") or account_id)
    funds = (chosen_acct or {}).get("funds") if isinstance((chosen_acct or {}).get("funds"), Mapping) else {}
    cur_assets = float(funds.get("total_assets") or 0.0)
    cur_mv = float(funds.get("market_value") or 0.0)
    cur_avail = float(funds.get("available") or 0.0)
    cur_daily_pnl = _safe_float(funds.get("daily_profit"))
    cur_holding_pnl = _safe_float(funds.get("holding_profit"))

    # 1. 每日实盘净值 vs 沪深300 vs AR4.5-L3b 理想影子曲线
    snaps = _collect_account_daily_snapshots(account_id=account_id, days=days, snap_dir=snap_dir)
    # 若今日最新 overview 尚未落入 snapshots，补上最新一条
    if chosen_acct and cur_assets > 0:
        ov_dt = _norm_date(str(chosen_acct.get("generated_at") or now.strftime("%Y-%m-%d"))) or now.strftime("%Y-%m-%d")
        if not snaps or snaps[-1]["date"] < ov_dt:
            snaps.append({
                "date": ov_dt,
                "generated_at": str(chosen_acct.get("generated_at") or now.isoformat(timespec="seconds")),
                "total_assets": round(cur_assets, 2),
                "market_value": round(cur_mv, 2),
                "available_cash": round(cur_avail, 2),
                "position_pct": round(cur_mv / cur_assets * 100.0, 1) if cur_assets > 0 else 0.0,
                "daily_profit": round(cur_daily_pnl, 2) if cur_daily_pnl is not None else None,
                "holding_profit": round(cur_holding_pnl, 2) if cur_holding_pnl is not None else None,
                "holdings_count": len(chosen_acct.get("holdings") or []),
            })

    start_dt = snaps[0]["date"] if snaps else (now.date() - timedelta(days=days)).strftime("%Y-%m-%d")
    hs_map = _fetch_hs300_close_map(start_dt)
    ar45_map = _load_ar45_backtest_curve_map()

    nav_curve: list[dict[str, Any]] = []
    actual_nav = 1.0
    cum_pnl_yuan = 0.0
    hs_base = _lookup_prev_or_exact(hs_map, start_dt) if snaps else None
    ar45_base = _lookup_prev_or_exact(ar45_map, start_dt) if snaps else None

    for idx, s in enumerate(snaps):
        dt = s["date"]
        dp = s.get("daily_profit")
        if idx > 0:
            prev_ta = snaps[idx - 1]["total_assets"]
            if dp is not None and prev_ta > 0:
                actual_nav *= (1.0 + dp / prev_ta)
                cum_pnl_yuan += dp
            elif prev_ta > 0:
                # 无当日盈亏字段时仅在资产变化 < 15% 时视为非出入金涨跌
                r_raw = s["total_assets"] / prev_ta - 1.0
                if abs(r_raw) < 0.15:
                    actual_nav *= (1.0 + r_raw)
                    cum_pnl_yuan += (s["total_assets"] - prev_ta)
        elif dp is not None:
            cum_pnl_yuan += dp

        hs_cur = _lookup_prev_or_exact(hs_map, dt)
        ar45_cur = _lookup_prev_or_exact(ar45_map, dt)
        hs_nav = round(hs_cur / hs_base, 4) if (hs_cur and hs_base and hs_base > 0) else None
        ar45_nav = round(ar45_cur / ar45_base, 4) if (ar45_cur and ar45_base and ar45_base > 0) else None

        nav_curve.append({
            "date": dt,
            "total_assets": s["total_assets"],
            "position_pct": s["position_pct"],
            "daily_profit": dp,
            "actual_nav": round(actual_nav, 4),
            "actual_ret_pct": round((actual_nav - 1.0) * 100.0, 2),
            "hs300_nav": hs_nav,
            "hs300_ret_pct": round((hs_nav - 1.0) * 100.0, 2) if hs_nav is not None else None,
            "ar45_ideal_nav": ar45_nav,
            "ar45_ideal_ret_pct": round((ar45_nav - 1.0) * 100.0, 2) if ar45_nav is not None else None,
        })

    # 2. Shadow Ledger 逐日计划单 vs 实盘成交对账
    shadow = reconcile_ar3_shadow_ledger(account_id=account_id, days=days)

    # 3. 持仓三腿真钱归因（AR4.5 跨资产 ETF / AR4.5 绩优个股 / 历史旧底仓与做T）
    # 读取最新计划单中的目标标的集合，用于精准划分哪些是现役目标、哪些是历史遗留旧票
    latest_plan = _read_json_safe(runtime_path("ar3_execution_plans") / f"latest_{account_id}.json") or {}
    target_codes = {
        str(x.get("code") or "").split(".")[0].zfill(6)
        for x in (latest_plan.get("target_allocation") or [])
        if isinstance(x, Mapping)
    }

    legs_summary: dict[str, dict[str, Any]] = {
        "ar45_etf": {"leg_id": "ar45_etf", "label": "AR4.5 跨资产 ETF 腿", "market_value": 0.0, "daily_profit": 0.0, "floating_profit": 0.0, "count": 0, "items": []},
        "ar45_stock": {"leg_id": "ar45_stock", "label": "AR4.5 绩优基本面个股腿", "market_value": 0.0, "daily_profit": 0.0, "floating_profit": 0.0, "count": 0, "items": []},
        "legacy_and_t": {"leg_id": "legacy_and_t", "label": "历史旧底仓 & 待清退腿", "market_value": 0.0, "daily_profit": 0.0, "floating_profit": 0.0, "count": 0, "items": []},
    }
    stop_violations: list[dict[str, Any]] = []

    for h in (chosen_acct or {}).get("holdings") or []:
        if not isinstance(h, Mapping):
            continue
        code6 = str(h.get("code") or "").split(".")[0].strip().zfill(6)
        qty = int(float(h.get("quantity") or h.get("shares") or h.get("qty") or 0))
        if not code6 or code6 == "000000" or qty <= 0:
            continue
        name = str(h.get("name") or code6)
        px = float(h.get("current_price") or h.get("price") or 0.0)
        cost = float(h.get("cost_price") or 0.0)
        mv = float(h.get("market_value") or (px * qty))
        dp = float(_safe_float(h.get("daily_profit")) or 0.0)
        fp = float(_safe_float(h.get("floating_profit")) or ((px - cost) * qty if (px > 0 and cost > 0) else 0.0))
        pnl_pct = round((px / cost - 1.0) * 100.0, 2) if (px > 0 and cost > 0) else None
        is_etf = _is_etf_code(code6)
        in_target = code6 in target_codes

        if is_etf:
            bucket = "ar45_etf"
            status_tag = "AR4.5目标ETF" if in_target else "非目标ETF"
        elif in_target:
            bucket = "ar45_stock"
            status_tag = "AR4.5目标个股"
        else:
            bucket = "legacy_and_t"
            status_tag = "历史旧底仓·待清退"

        if not is_etf and pnl_pct is not None and pnl_pct <= -10.0:
            stop_violations.append({
                "code": code6,
                "name": name,
                "profit_pct": pnl_pct,
                "floating_profit": round(fp, 2),
                "market_value": round(mv, 2),
            })

        legs_summary[bucket]["market_value"] = round(legs_summary[bucket]["market_value"] + mv, 2)
        legs_summary[bucket]["daily_profit"] = round(legs_summary[bucket]["daily_profit"] + dp, 2)
        legs_summary[bucket]["floating_profit"] = round(legs_summary[bucket]["floating_profit"] + fp, 2)
        legs_summary[bucket]["count"] += 1
        legs_summary[bucket]["items"].append({
            "code": code6,
            "name": name,
            "shares": qty,
            "cost_price": round(cost, 3),
            "current_price": round(px, 3),
            "market_value": round(mv, 2),
            "weight_pct": round(mv / cur_assets * 100.0, 1) if cur_assets > 0 else 0.0,
            "daily_profit": round(dp, 2),
            "floating_profit": round(fp, 2),
            "profit_pct": pnl_pct,
            "status_tag": status_tag,
            "breached_stop": bool(not is_etf and pnl_pct is not None and pnl_pct <= -10.0),
        })

    # 4. 联动做T净增厚与 AI 交易教练
    attr = build_four_dim_attribution(days=days)
    t_trade_pnl = (attr.get("summary") or {}).get("t_trade_pnl")
    coach = build_trading_coach_report(days=min(30, days))

    # 止损与纪律综合评分（100 满分，每只破-10%未清仓个股扣 20 分，执行完成率低扣分）
    discipline_score = 100
    discipline_score -= len(stop_violations) * 20
    comp_pct = shadow.get("overall_completion_pct")
    if comp_pct is not None and comp_pct < 80.0:
        discipline_score -= int(round((80.0 - float(comp_pct)) * 0.4))
    discipline_score = max(10, min(100, discipline_score))

    ar45_in_strategy_fp = round(
        legs_summary["ar45_etf"]["floating_profit"] + legs_summary["ar45_stock"]["floating_profit"], 2
    )
    legacy_drag_fp = round(legs_summary["legacy_and_t"]["floating_profit"], 2)

    last_pt = nav_curve[-1] if nav_curve else {}
    excess_vs_hs300 = (
        round(float(last_pt["actual_ret_pct"]) - float(last_pt["hs300_ret_pct"]), 2)
        if last_pt.get("actual_ret_pct") is not None and last_pt.get("hs300_ret_pct") is not None
        else None
    )

    data_notes: list[str] = []
    if len(nav_curve) < 5:
        data_notes.append(
            f"当前主力账号 {acct_label} 已落盘 {len(nav_curve)} 个交易日真实快照（起始 {start_dt}），后续每日盘后自动累积净值曲线，绝不虚构历史数据。"
        )
    if shadow.get("days_tracked", 0) < 3:
        data_notes.append(
            f"AR4.5-L3b 每日计划单 Shadow 对账已开始追踪（当前 {shadow.get('days_tracked', 0)} 天），每个交易日 15:15 同步回执后自动更新滑点与跟单完成率。"
        )

    return {
        "schema": "ar45_review_attribution/1",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "account": {
            "account_id": account_id,
            "label": acct_label,
            "total_assets": round(cur_assets, 2),
            "market_value": round(cur_mv, 2),
            "available_cash": round(cur_avail, 2),
            "position_pct": round(cur_mv / cur_assets * 100.0, 1) if cur_assets > 0 else 0.0,
            "daily_profit": cur_daily_pnl,
            "holding_profit": cur_holding_pnl,
        },
        "window_days": days,
        "kpis": {
            "tracked_days": len(nav_curve),
            "period_pnl_yuan": round(cum_pnl_yuan, 2),
            "actual_ret_pct": last_pt.get("actual_ret_pct"),
            "hs300_ret_pct": last_pt.get("hs300_ret_pct"),
            "excess_vs_hs300_pct": excess_vs_hs300,
            "ar45_ideal_ret_pct": last_pt.get("ar45_ideal_ret_pct"),
            "completion_pct": comp_pct,
            "avg_slippage_bps": shadow.get("avg_slippage_bps"),
            "in_strategy_floating_pnl": ar45_in_strategy_fp,
            "legacy_drag_floating_pnl": legacy_drag_fp,
            "t_trade_pnl": t_trade_pnl,
            "discipline_score": discipline_score,
            "stop_violation_count": len(stop_violations),
        },
        "nav_curve": nav_curve,
        "legs": list(legs_summary.values()),
        "stop_violations": stop_violations,
        "shadow_reconcile": shadow,
        "coach": coach,
        "data_notes": data_notes,
    }
