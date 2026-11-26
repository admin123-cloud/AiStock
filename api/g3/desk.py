"""G3 做T实战台 API：打通双账号真实持仓 + 观察名单 + 每日做T作战规划 + 盘中实时行情/RSI + 日内敞口台账。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from services.broker.broker_accounts import get_accounts_overview
from services.g3.ai_screening import load_saved_screening, lookup_stock_ai_sentinel
from services.g3.fill_autosync import (
    dispatch_prefill_order,
    get_prefill_bridge_status,
    get_prefill_dispatch_status,
    sync_broker_fills_to_desk_executions,
)
from services.g3.market_regime_gate import evaluate_market_regime_gate
from services.g3.order_prefill import evaluate_and_build_prefill_draft
from services.g3.premarket_brief import (
    generate_premarket_brief,
    load_latest_premarket_brief,
    push_premarket_brief_wechat,
)
from services.g3.strategy_matrix import build_strategy_matrix
from services.g3.trading_plan_compiler import compile_daily_trading_command
from services.mining.live_router import scan_active_strategy_signals
from services.mining.panic_rebound_monitor import evaluate_panic_rebound_signal
from services.trading.holding_t_watchlist import (
    read_watchlist,
    sync_watchlist_from_ths,
    upsert_watchlist,
)
from services.trading.t_daily_plan import (
    append_t_execution,
    build_market_directive,
    build_stock_daily_plan,
    delete_t_execution,
    fetch_batch_live_quotes,
    fetch_batch_live_rsi_snapshots,
    generate_and_save_daily_plan,
    load_intraday_executions,
    load_saved_daily_plan,
    resolve_effective_params,
    summarize_intraday_executions,
)
from services.trading.t_notify import (
    ensure_draft_watcher_started,
    get_public_notify_config,
    save_notify_config,
    send_wechat_notification,
)
from utils.common.paths import runtime_path

router = APIRouter()
BJ_TZ = timezone(timedelta(hours=8))


def _watchlist_paths() -> tuple[Path, Path]:
    store_path = runtime_path("gen3_state_alpha", "holding_t_watchlist", "holding_tick_watchlist.json")
    legacy_path = runtime_path("gen3_state_alpha", "holding_tick_watchlist.json")
    return store_path, legacy_path


def _read_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _latest_dated_json(directory: Path, prefix: str) -> tuple[str | None, dict[str, Any]]:
    if not directory.exists():
        return None, {}
    files = sorted(directory.glob(f"{prefix}*.json"), reverse=True)
    for f in files:
        data = _read_json_safe(f)
        if data:
            return f.name, data
    return None, {}


def _load_today_drafts(tracker_dir: Path, trade_date: str) -> list[dict[str, Any]]:
    drafts_dir = tracker_dir / "drafts"
    if not drafts_dir.exists():
        return []
    ymd = trade_date.replace("-", "")
    items: list[dict[str, Any]] = []
    # 读取当日 jsonl 或 json 草案文件
    for f in sorted(drafts_dir.glob(f"*{ymd}*")):
        if f.suffix == ".jsonl":
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        items.append(obj)
                except Exception:
                    continue
        elif f.suffix == ".json":
            obj = _read_json_safe(f)
            if isinstance(obj.get("drafts"), list):
                items.extend(obj["drafts"])
            elif obj:
                items.append(obj)
    return items[-50:]


def _collect_live_holdings() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """优先从双账号远航采集（ths_read_account）读取最新真实持仓，回退 broker_state.json。"""
    overview = get_accounts_overview()
    merged_holdings: list[dict[str, Any]] = []
    for acct in overview.get("accounts") or []:
        acct_id = acct.get("account_id", "")
        acct_label = acct.get("label") or acct_id
        for h in acct.get("holdings") or []:
            code = str(h.get("code") or "").split(".")[0].zfill(6)
            if not code or code == "000000":
                continue
            qty = int(float(h.get("quantity") or h.get("shares") or h.get("qty") or 0))
            avail = int(float(h.get("available_quantity") or h.get("available_shares") or qty))
            cur_px = h.get("current_price") or h.get("price")
            mv = h.get("market_value")
            if mv is None and cur_px is not None and qty:
                mv = round(float(cur_px) * qty, 2)
            merged_holdings.append({
                "account_id": acct_id,
                "account_label": acct_label,
                "code": code,
                "name": h.get("name") or "",
                "shares": qty,
                "available_shares": avail,
                "cost_price": h.get("cost_price"),
                "current_price": cur_px,
                "market_value": mv,
                "profit_loss": h.get("floating_profit") if h.get("floating_profit") is not None else h.get("profit_loss"),
                "profit_pct": h.get("profit_ratio") if h.get("profit_ratio") is not None else h.get("profit_pct"),
                "daily_profit": h.get("daily_profit"),
                "daily_profit_pct": h.get("daily_profit_ratio"),
                "source": "ths_read_account",
            })

    if not merged_holdings:
        broker_state = _read_json_safe(runtime_path("gen3_state_alpha", "broker_state.json"))
        for h in broker_state.get("holdings") or []:
            code = str(h.get("code") or "").split(".")[0].zfill(6)
            if not code or code == "000000":
                continue
            merged_holdings.append({
                "account_id": "broker_state",
                "account_label": "历史快照",
                "code": code,
                "name": h.get("name") or "",
                "shares": h.get("qty") or h.get("shares") or 0,
                "available_shares": h.get("available_qty") or h.get("qty") or 0,
                "cost_price": h.get("cost_price"),
                "current_price": h.get("last_price"),
                "market_value": h.get("market_value"),
                "profit_loss": None,
                "profit_pct": None,
                "source": "broker_state_fallback",
            })
    return overview, merged_holdings


@router.get("/desk")
def get_g3_desk(
    refresh_quotes: bool = Query(default=False, description="是否跳过短期内存缓存强制刷新实时行情"),
) -> dict[str, Any]:
    """G3 做T实战台一站式聚合：双账号持仓 + 每日做T规划 + 实时行情/VWAP/RSI + 布防态 + 日内敞口台账。"""
    ensure_draft_watcher_started()
    now_bj = datetime.now(BJ_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")
    tracker_dir = runtime_path("t_tracker")

    overview, live_holdings = _collect_live_holdings()
    store_path, legacy_path = _watchlist_paths()
    watchlist_doc = read_watchlist(store_path, legacy_path=legacy_path)
    watchlist_items = watchlist_doc.get("entries") or watchlist_doc.get("items") or []

    regime_file, regime_doc = _latest_dated_json(tracker_dir, "regime_")
    saved_plan = load_saved_daily_plan(today_str)

    # 优先采用今日最新规划：若今日手动/按需规划存在，合并覆盖 t_tracker 的 regime（补齐新标的并刷新日期）
    regime_date = str(regime_doc.get("date") or "")
    effective_regime_doc = dict(regime_doc)
    plan_source = "t_tracker_regime"
    plan_generated_at = regime_doc.get("generated_at")
    if saved_plan and str(saved_plan.get("date") or "") == today_str:
        merged_codes = dict(regime_doc.get("codes") or {})
        merged_codes.update(saved_plan.get("codes") or {})
        effective_regime_doc = {
            **regime_doc,
            "date": today_str,
            "generated_at": saved_plan.get("generated_at") or plan_generated_at,
            "version": saved_plan.get("version") or regime_doc.get("version"),
            "codes": merged_codes,
            "breadth": saved_plan.get("breadth") or regime_doc.get("breadth"),
        }
        regime_date = today_str
        plan_source = "desk_daily_plan"
        plan_generated_at = saved_plan.get("generated_at")

    trade_date = regime_date or today_str
    is_stale_regime = (regime_date != today_str)
    state_file, state_doc = _latest_dated_json(tracker_dir, "state_")
    contract_stop = _read_json_safe(tracker_dir / "contract_stop.json")
    price_cache = _read_json_safe(tracker_dir / "price_cache.json")
    drafts = _load_today_drafts(tracker_dir, today_str)
    if not drafts and trade_date != today_str:
        drafts = _load_today_drafts(tracker_dir, trade_date)

    # 聚合持仓与观察名单
    holding_by_code: dict[str, list[dict[str, Any]]] = {}
    for h in live_holdings:
        holding_by_code.setdefault(h["code"], []).append(h)

    watch_by_code: dict[str, dict[str, Any]] = {}
    for w in watchlist_items:
        code = str(w.get("code") or "").split(".")[0].zfill(6)
        if code and code != "000000":
            watch_by_code[code] = w

    regime_codes = effective_regime_doc.get("codes") or {}
    all_codes = sorted(set(holding_by_code.keys()) | set(watch_by_code.keys()) | set(regime_codes.keys()))

    # 批量拉取实时行情（现价、涨跌幅、VWAP）
    live_quotes = fetch_batch_live_quotes(
        all_codes,
        price_cache=price_cache,
        force_refresh=refresh_quotes,
    )

    # 市场总纲
    market_directive = build_market_directive(effective_regime_doc.get("breadth"))

    # Phase 3C 进阶：自动幂等同步同花顺当日真实成交回执到日内分型台账
    try:
        fill_sync_meta = sync_broker_fills_to_desk_executions(trade_date=today_str)
    except Exception:
        fill_sync_meta = {"status": "error", "imported": 0, "idempotent_skipped": 0}

    # 当日做T / R51水位调仓执行台账与敞口汇总
    executions = load_intraday_executions(today_str)
    exposure_by_code, exec_summary = summarize_intraday_executions(executions)

    merged_funds = overview.get("merged") or {}
    bridge_info = overview.get("bridge") or {}
    avail_cash = merged_funds.get("available_cash")

    # Phase 1A：大盘择时总闸门 × R51 仓位水位联动
    market_regime_gate = evaluate_market_regime_gate(
        breadth=effective_regime_doc.get("breadth"),
        total_assets=merged_funds.get("total_assets"),
        total_market_value=merged_funds.get("market_value"),
    )

    # 预先识别需要计算 RSI 的标的并准备并发任务（批量 RSI + 主升浪扫描 + 冰点反抽 + 路由扫描）
    rsi_codes: list[str] = []
    rsi_price_map: dict[str, Any] = {}
    for code in all_codes:
        h_list = holding_by_code.get(code, [])
        r_info = regime_codes.get(code) or {}
        total_shares = sum(int(x.get("shares") or 0) for x in h_list)
        t_lot_cap = (total_shares // 3 // 100) * 100 if total_shares >= 300 else 0
        if t_lot_cap >= 100 or r_info:
            px = (live_quotes.get(code) or {}).get("price")
            if not px and h_list:
                px = h_list[0].get("current_price")
            rsi_codes.append(code)
            rsi_price_map[code] = float(px) if px else None

    def _safe_rsi_batch() -> dict[str, dict[str, Any]]:
        return fetch_batch_live_rsi_snapshots(
            rsi_codes,
            rsi_price_map,
            force_refresh=refresh_quotes,
        )

    def _safe_panic() -> dict[str, Any] | None:
        try:
            return evaluate_panic_rebound_signal(market_gate=market_regime_gate)
        except Exception:
            return None

    def _safe_router() -> dict[str, Any] | None:
        try:
            return scan_active_strategy_signals(
                current_regime=str(market_regime_gate.get("regime") or "balanced"),
                regime_label=str(market_regime_gate.get("regime_label") or ""),
                allow_new_mainwave=bool(market_regime_gate.get("allow_new_mainwave")),
                target_position_cap_pct=float(market_regime_gate.get("target_position_cap_pct") or 0.0),
            )
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_rsi = executor.submit(_safe_rsi_batch)
        fut_panic = executor.submit(_safe_panic)
        fut_router = executor.submit(_safe_router)
        rsi_map = fut_rsi.result()
        panic_rebound_signal = fut_panic.result()
        strategy_router = fut_router.result()

    cards: list[dict[str, Any]] = []
    saved_screening_doc = load_saved_screening()
    eligible_count = 0
    ineligible_count = 0
    stale_param_count = 0
    stop_alert_count = 0

    for code in all_codes:
        h_list = holding_by_code.get(code, [])
        w_item = watch_by_code.get(code)
        r_info = regime_codes.get(code) or {}
        s_info = state_doc.get(code) or {}
        total_shares = sum(int(x.get("shares") or 0) for x in h_list)
        available_shares = sum(int(x.get("available_shares") or 0) for x in h_list)
        t_lot_cap = (total_shares // 3 // 100) * 100 if total_shares >= 300 else 0
        name = (
            r_info.get("name")
            or (h_list[0].get("name") if h_list else "")
            or (w_item.get("name") if w_item else "")
            or code
        )

        # 实时行情 + 实时 RSI（来自前置批量并发结果）
        q = dict(live_quotes.get(code) or {})
        if not q.get("price") and h_list:
            fallback_px = h_list[0].get("current_price")
            if fallback_px:
                q["price"] = float(fallback_px)
                q["source"] = "holding_snapshot"
        if t_lot_cap >= 100 or r_info:
            rsi_snap = rsi_map.get(code) or {}
            q["rsi5m"] = rsi_snap.get("rsi5m")
            q["rsi15m"] = rsi_snap.get("rsi15m")
            if not q.get("vwap") and rsi_snap.get("vwap_5m"):
                q["vwap"] = rsi_snap.get("vwap_5m")
                if q.get("price") and q["vwap"]:
                    q["vwap_dev_pct"] = round((float(q["price"]) - float(q["vwap"])) / float(q["vwap"]) * 100.0, 2)

        raw_params = r_info.get("params") or {}
        regime = str(r_info.get("regime") or "unknown")
        prev_close = r_info.get("close") or q.get("prev_close")

        effective_params, params_stale, params_stale_reason = resolve_effective_params(
            regime=regime,
            raw_params=raw_params,
            ma5=r_info.get("ma5"),
            ma10=r_info.get("ma10"),
            ma20=r_info.get("ma20"),
            hi20=r_info.get("hi20"),
            lo20=r_info.get("lo20"),
            prev_close=prev_close,
            live_price=q.get("price"),
        )

        daily_plan = build_stock_daily_plan(
            code=code,
            name=name,
            regime=regime,
            total_shares=total_shares,
            available_shares=available_shares,
            t_lot_cap=t_lot_cap,
            available_cash=float(avail_cash) if avail_cash is not None else None,
            effective_params=effective_params,
            params_stale=params_stale,
            params_stale_reason=params_stale_reason,
            live_quote=q,
            ma5=r_info.get("ma5"),
            ma10=r_info.get("ma10"),
            ma20=r_info.get("ma20"),
            prev_close=prev_close,
            market_directive=market_directive,
        )

        if daily_plan["eligible"]:
            eligible_count += 1
        else:
            ineligible_count += 1
        if params_stale:
            stale_param_count += 1
        if daily_plan.get("action_mode") == "stop_alert":
            stop_alert_count += 1

        cards.append({
            "code": code,
            "name": name,
            "in_holdings": bool(h_list),
            "in_watchlist": bool(w_item),
            "total_shares": total_shares,
            "available_shares": available_shares,
            "t_lot_cap": t_lot_cap,
            "holdings": h_list,
            "watchlist": w_item,
            "regime": regime,
            "ma5": r_info.get("ma5"),
            "ma10": r_info.get("ma10"),
            "ma20": r_info.get("ma20"),
            "pos20": r_info.get("pos20"),
            "hi20": r_info.get("hi20"),
            "lo20": r_info.get("lo20"),
            "prev_close": prev_close,
            "chg5d_pct": r_info.get("chg5d_pct"),
            "params": raw_params,
            "effective_params": effective_params,
            "params_stale": params_stale,
            "params_stale_reason": params_stale_reason,
            "live_quote": q,
            "daily_plan": daily_plan,
            "open_t_exposure": exposure_by_code.get(code),
            "armed_sides": s_info.get("sides") or {},
            "hard_stop": contract_stop.get(code),
            "ai_sentinel": lookup_stock_ai_sentinel(code, saved_doc=saved_screening_doc),
        })

    # 按实战优先级排序：可做T & 风险/机会高优先排前，不可做T零股排后
    cards.sort(key=lambda x: (x["daily_plan"].get("priority", 99), -x["t_lot_cap"], x["code"]))

    # Phase 4 全站联动：基于当前大盘闸门计算三腿动态配额摘要
    try:
        _matrix_snap = build_strategy_matrix(market_gate=market_regime_gate)
        strategy_allocation = {
            "allocation": _matrix_snap.get("allocation"),
            "defense_auto_cash": _matrix_snap.get("defense_auto_cash"),
            "legs": [
                {
                    "leg_id": lg["leg_id"],
                    "name": lg["name"],
                    "target_pct": lg["target_pct"],
                    "status": lg["status"],
                    "status_label": lg["status_label"],
                    "action_route": lg["action_route"],
                }
                for lg in (_matrix_snap.get("legs") or [])
            ],
        }
    except Exception:
        strategy_allocation = None

    accounts_summary_obj = {
        "total_assets": merged_funds.get("total_assets"),
        "total_market_value": merged_funds.get("market_value"),
        "total_cash": avail_cash,
        "total_floating_profit": merged_funds.get("floating_profit"),
        "account_count": overview.get("accounts_total") or len(overview.get("accounts") or []),
        "accounts_ready": overview.get("accounts_ready"),
        "bridge_online": bridge_info.get("online"),
        "updated_at": bridge_info.get("heartbeat_at"),
    }

    trading_command = compile_daily_trading_command(
        market_gate=market_regime_gate,
        strategy_router=strategy_router,
        cards=cards,
        accounts_summary=accounts_summary_obj,
        panic_rebound_signal=panic_rebound_signal,
    )

    return {
        "as_of": now_bj.isoformat(timespec="seconds"),
        "today": today_str,
        "trade_date": trade_date,
        "is_stale_regime": is_stale_regime,
        "plan_meta": {
            "source": plan_source,
            "date": regime_date or None,
            "generated_at": plan_generated_at,
        },
        "market_directive": market_directive,
        "market_regime_gate": market_regime_gate,
        "strategy_allocation": strategy_allocation,
        "strategy_router": strategy_router,
        "trading_command": trading_command,
        "premarket_brief": load_latest_premarket_brief(today_str),
        "prefill_bridge": get_prefill_bridge_status(),
        "fill_sync_meta": {
            "imported": fill_sync_meta.get("imported", 0),
            "broker_fills_found": fill_sync_meta.get("broker_fills_found", 0),
        },
        "intraday_summary": {
            **exec_summary,
            "eligible_count": eligible_count,
            "ineligible_count": ineligible_count,
            "stale_param_count": stale_param_count,
            "stop_alert_count": stop_alert_count,
        },
        "accounts_summary": accounts_summary_obj,
        "regime_meta": {
            "file": regime_file,
            "generated_at": plan_generated_at,
            "version": effective_regime_doc.get("version"),
        },
        "state_meta": {
            "file": state_file,
        },
        "cards": cards,
        "drafts": drafts,
        "executions": executions,
        "notify_config": get_public_notify_config(),
        "watchlist_count": len(watchlist_items),
        "holding_count": len(holding_by_code),
    }


@router.post("/desk/generate-plan")
def post_g3_generate_plan() -> dict[str, Any]:
    """一键生成/刷新今日做T规划（基于 ClickHouse 最新日线 + 全市场宽度 + 当前双账号持仓/观察名单）。"""
    now_bj = datetime.now(BJ_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")
    tracker_dir = runtime_path("t_tracker")
    _, regime_doc = _latest_dated_json(tracker_dir, "regime_")

    _, live_holdings = _collect_live_holdings()
    store_path, legacy_path = _watchlist_paths()
    watchlist_doc = read_watchlist(store_path, legacy_path=legacy_path)
    watchlist_items = watchlist_doc.get("entries") or watchlist_doc.get("items") or []

    code_names: dict[str, str] = {}
    for h in live_holdings:
        code_names[h["code"]] = h.get("name") or code_names.get(h["code"], "")
    for w in watchlist_items:
        c = str(w.get("code") or "").split(".")[0].zfill(6)
        if c and c != "000000":
            code_names.setdefault(c, str(w.get("name") or ""))

    if not code_names:
        raise HTTPException(status_code=400, detail="当前无持仓或观察标的，无法生成今日规划")

    plan = generate_and_save_daily_plan(
        codes=sorted(code_names.items()),
        trade_date=today_str,
        fallback_breadth=regime_doc.get("breadth"),
    )
    return {
        "status": "ok",
        "date": plan["date"],
        "generated_at": plan["generated_at"],
        "codes_count": len(plan.get("codes") or {}),
        "errors": plan.get("errors") or {},
    }


class TExecutionRequest(BaseModel):
    code: str
    name: str = ""
    side: str = Field(..., description="BUY 或 SELL")
    price: float
    shares: int
    order_type: str = Field(default="t_trade", description="t_trade（做T单）或 base_rebalance（R51水位调仓单）")
    account_label: str = ""
    note: str = ""


@router.post("/desk/execution")
def post_g3_execution(req: TExecutionRequest) -> dict[str, Any]:
    """登记一笔当日手工成交（R51 分型：做T单 vs 水位调仓单）。"""
    today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    try:
        entry = append_t_execution(
            trade_date=today_str,
            code=req.code,
            name=req.name,
            side=req.side,
            price=req.price,
            shares=req.shares,
            order_type=req.order_type,
            account_label=req.account_label,
            note=req.note,
        )
        return {"status": "ok", "entry": entry}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class PrefillCheckRequest(BaseModel):
    code: str
    name: str = ""
    side: str = Field(..., description="BUY 或 SELL")
    price: float
    shares: int
    order_type: str = Field(default="t_trade", description="t_trade | base_rebalance")
    account_id: str = "acct1"


@router.post("/desk/prefill-check")
def post_g3_desk_prefill_check(req: PrefillCheckRequest) -> dict[str, Any]:
    """Phase 3C：执行 L2 硬风控预检并生成人工确认预填单草案（零自动下单）。"""
    code6 = str(req.code or "").split(".")[0].strip().zfill(6)
    overview, live_holdings = _collect_live_holdings()
    merged_funds = overview.get("merged") or {}
    h_list = [h for h in live_holdings if h.get("code") == code6]
    total_shares = sum(int(x.get("shares") or 0) for x in h_list)
    available_shares = sum(int(x.get("available_shares") or 0) for x in h_list)

    quotes = fetch_batch_live_quotes([code6])
    live_px = (quotes.get(code6) or {}).get("price")
    if not live_px and h_list:
        live_px = h_list[0].get("current_price")

    try:
        gate = get_g3_market_gate()
    except Exception:
        gate = None

    return evaluate_and_build_prefill_draft(
        code=code6,
        name=req.name or (h_list[0].get("name") if h_list else code6),
        side=req.side,
        price=req.price,
        shares=req.shares,
        order_type=req.order_type,
        account_id=req.account_id,
        live_price=float(live_px) if live_px else None,
        total_shares=total_shares,
        available_shares=available_shares,
        available_cash=float(merged_funds["available_cash"]) if merged_funds.get("available_cash") is not None else None,
        market_gate=gate,
    )


class PrefillDispatchRequest(BaseModel):
    draft_id: str


@router.post("/desk/prefill-dispatch")
def post_g3_desk_prefill_dispatch(req: PrefillDispatchRequest) -> dict[str, Any]:
    """Phase 3C 进阶：将已通过 L2 预检的草案派发至宿主机预填队列（强制 prefill_only，绝不提交）。"""
    try:
        return dispatch_prefill_order(draft_id=req.draft_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/desk/prefill-status/{draft_id}")
def get_g3_desk_prefill_status(draft_id: str) -> dict[str, Any]:
    """查询指定预填草案在宿主机同花顺界面的执行状态。"""
    return get_prefill_dispatch_status(draft_id=draft_id.strip())


@router.post("/desk/sync-fills")
def post_g3_desk_sync_fills() -> dict[str, Any]:
    """Phase 3C 进阶：立即将同花顺当日真实成交回执幂等入账至 G3 分型台账（自动继承 R51 分型）。"""
    return sync_broker_fills_to_desk_executions()


class TExecutionDeleteRequest(BaseModel):
    exec_id: str


@router.post("/desk/execution/delete")
def post_g3_execution_delete(req: TExecutionDeleteRequest) -> dict[str, Any]:
    """撤销一条当日误记的做T成交记录（scoped-write POST）。"""
    today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    ok = delete_t_execution(today_str, req.exec_id)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到该成交记录")
    return {"status": "ok", "deleted_id": req.exec_id}


class NotifyConfigUpdateRequest(BaseModel):
    enabled: bool | None = None
    channel: str | None = None
    pushplus_token: str | None = None
    wecom_webhook: str | None = None
    serverchan_sendkey: str | None = None
    qq_email_to: str | None = None
    only_trading_hours: bool | None = None
    notify_on_draft: bool | None = None
    notify_on_stop_alert: bool | None = None


@router.get("/desk/notify/config")
def get_g3_notify_config() -> dict[str, Any]:
    """读取脱敏后的做T单向微信推送配置。"""
    ensure_draft_watcher_started()
    return get_public_notify_config()


@router.post("/desk/notify/config")
def post_g3_notify_config(req: NotifyConfigUpdateRequest) -> dict[str, Any]:
    """保存做T单向微信推送配置（仅写 runtime/g3_desk_plans/notify_config.json）。"""
    ensure_draft_watcher_started()
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    return {"status": "ok", "config": save_notify_config(patch)}


@router.post("/desk/notify/test")
def post_g3_notify_test() -> dict[str, Any]:
    """发送一条单向微信测试消息，立即验证通道连通性。"""
    now_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    res = send_wechat_notification(
        title="【做T实战台】单向微信通知测试成功",
        markdown_body=(
            f"- **发送时间**：`{now_str}`\n"
            "- **说明**：这是一条来自 AiStock G3 做T实战台的单向推送测试。\n"
            "- **生效范围**：盘中触发买入T / 卖出T 信号草案后 10 秒内将自动推送到你的微信。"
        ),
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=f"发送失败（{res.get('channel')}）：{res.get('detail')}")
    return {"status": "ok", **res}


class WatchlistUpsertRequest(BaseModel):
    add: list[Any] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


@router.post("/watchlist/upsert")
def post_g3_watchlist_upsert(req: WatchlistUpsertRequest) -> dict[str, Any]:
    """Scoped-write：维护做T持仓观察名单（仅写 holding_t_watchlist 授权目录）。"""
    try:
        store_path, legacy_path = _watchlist_paths()
        return upsert_watchlist(
            store_path,
            add=req.add,
            remove=req.remove,
            legacy_path=legacy_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/watchlist/sync-ths")
def post_g3_watchlist_sync_ths() -> dict[str, Any]:
    """Scoped-write：从同花顺双账号真实持仓同步做T观察名单。"""
    try:
        store_path, legacy_path = _watchlist_paths()
        overview, live_holdings = _collect_live_holdings()
        synced_at = str(overview.get("updated_at") or datetime.now(BJ_TZ).isoformat(timespec="seconds"))
        return sync_watchlist_from_ths(
            store_path,
            holdings=live_holdings,
            synced_at=synced_at,
            legacy_path=legacy_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/market-gate")
def get_g3_market_gate() -> dict[str, Any]:
    """Phase 1A 独立接口：大盘择时总闸门 × R51 仓位水位对标（供盘前简报 Agent 与前端消费）。"""
    now_bj = datetime.now(BJ_TZ)
    today_str = now_bj.strftime("%Y-%m-%d")
    tracker_dir = runtime_path("t_tracker")
    _, regime_doc = _latest_dated_json(tracker_dir, "regime_")
    saved_plan = load_saved_daily_plan(today_str)
    breadth = (saved_plan.get("breadth") if saved_plan else None) or regime_doc.get("breadth")
    overview, _ = _collect_live_holdings()
    merged_funds = overview.get("merged") or {}
    return evaluate_market_regime_gate(
        breadth=breadth,
        total_assets=merged_funds.get("total_assets"),
        total_market_value=merged_funds.get("market_value"),
    )


class PremarketBriefGenerateRequest(BaseModel):
    overnight_notes: list[str] = Field(default_factory=list)
    agent_thesis: str | None = None
    source: str = "auto_baseline"


@router.get("/premarket-brief")
def get_g3_premarket_brief(trade_date: str | None = Query(default=None)) -> dict[str, Any]:
    """Phase 2B：读取当日或最新盘前 09:10 AR3 v3.0 终极战报（若已有旧版 schema 则按 AR3 重算）。"""
    brief = load_latest_premarket_brief(trade_date)
    if brief and str(brief.get("schema") or "").startswith("g3_premarket_brief/2_ar3"):
        return brief
    return post_g3_premarket_brief_generate()


@router.post("/premarket-brief/generate")
def post_g3_premarket_brief_generate(
    req: PremarketBriefGenerateRequest | None = None,
) -> dict[str, Any]:
    """Scoped-write：生成并落盘当日 AR3 v3.0 盘前终极战报（主力单账号 ct1曾 + 精确买卖单 + 隔夜研判）。"""
    from services.mining.ar3_rebalance_compiler import compile_ar3_rebalance_plan

    body = req or PremarketBriefGenerateRequest()
    gate = get_g3_market_gate()
    try:
        panic = evaluate_panic_rebound_signal(market_gate=gate)
    except Exception:
        panic = None
    try:
        ar3_plan = compile_ar3_rebalance_plan(
            market_gate=gate,
            panic_signal=panic,
            account_id="acct1",
            smooth_step=True,
            persist=True,
        )
    except Exception:
        ar3_plan = None
    return generate_premarket_brief(
        market_gate=gate,
        ar3_plan=ar3_plan,
        overnight_notes=body.overnight_notes,
        agent_thesis=body.agent_thesis,
        source=body.source,
    )


@router.post("/premarket-brief/push-wechat")
def post_g3_premarket_brief_push_wechat() -> dict[str, Any]:
    """Scoped-write：将最新盘前市场主线简报一键推送至已配置的微信通道。"""
    brief = load_latest_premarket_brief()
    if not brief:
        brief = post_g3_premarket_brief_generate()
    res = push_premarket_brief_wechat(brief)
    return {"status": "ok" if res.get("ok") else "failed", "date": brief.get("date"), **res}


class Acct1ConfirmFillRequest(BaseModel):
    code: str
    side: str
    shares: int
    price: float
    name: str = ""
    note: str = ""
    trigger_next_intraday: bool = True


@router.get("/acct1-advisor/status")
def get_g3_acct1_advisor_status() -> dict[str, Any]:
    """只读查询 acct1 AR4.6 盘中智能买卖提醒引擎当日状态、已推送记录与人工核销成交。"""
    from services.g3.acct1_intraday_advisor import get_acct1_advisor_status

    return get_acct1_advisor_status()


@router.post("/acct1-advisor/run")
def post_g3_acct1_advisor_run(
    mode: str = Query(default="intraday", pattern="^(premarket|intraday|eod)$"),
    dry_run: bool = Query(default=False),
    bypass_session: bool = Query(default=False),
) -> dict[str, Any]:
    """触发 acct1 AR4.6 盘前/盘中/盘后巡检与微信买卖提醒（零自动下单，仅单向推送）。"""
    from services.g3.acct1_intraday_advisor import run_acct1_advisor

    return run_acct1_advisor(mode=mode, dry_run=dry_run, bypass_session=bypass_session)


@router.post("/acct1-advisor/confirm-fill")
def post_g3_acct1_advisor_confirm_fill(req: Acct1ConfirmFillRequest) -> dict[str, Any]:
    """登记一笔 acct1 人工成交并校准当日实时账本；若在盘中可自动衔接推送下一笔 AR4.6 买卖单。"""
    from services.g3.acct1_intraday_advisor import record_acct1_manual_fill, run_acct1_advisor

    try:
        fill = record_acct1_manual_fill(
            code=req.code,
            side=req.side,
            shares=req.shares,
            price=req.price,
            name=req.name,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    followup = None
    if req.trigger_next_intraday:
        try:
            followup = run_acct1_advisor(mode="intraday", dry_run=False, bypass_session=False)
        except Exception as exc:  # noqa: BLE001
            followup = {"status": "error", "detail": str(exc)}
    return {"status": "ok", "fill": fill, "followup_intraday": followup}


# ── AiStock <-> Antigravity 异步 AI 决策总线（AI Bus） ────────────────────────


class AiBusAskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000, description="向在线 AI 参谋提问或请求深度研判")
    kind: str = Field(default="desk_qa", description="任务类型: desk_qa / premarket_brief / panic_audit / postclose_review")
    source: str = Field(default="desk_ui", description="调用来源")
    context: Optional[dict[str, Any]] = Field(default=None, description="附加结构化上下文")
    wait_timeout_sec: float = Field(default=0.0, ge=0.0, le=120.0, description=">0 时同步等待结果，超时自动降级")


@router.get("/ai-bus/status")
def get_g3_ai_bus_status(limit: int = Query(default=12, ge=1, le=50)) -> dict[str, Any]:
    """查询 Antigravity 会话在线心跳、排队任务与最近 AI 参谋思考记录。"""
    from services.g3.ai_bus import get_bus_status

    return get_bus_status(limit=limit)


@router.get("/ai-bus/jobs/{job_id}")
def get_g3_ai_bus_job(job_id: str) -> dict[str, Any]:
    """轮询单个 AI 决策总线任务状态与思考结果。"""
    from services.g3.ai_bus import get_ai_job

    return get_ai_job(job_id)


@router.post("/ai-bus/ask")
def post_g3_ai_bus_ask(req: AiBusAskRequest) -> dict[str, Any]:
    """向本地 AI 决策总线提交思考任务（自动附带大盘闸门与双账号概览上下文，严禁自动下单）。"""
    from services.g3.ai_bus import submit_ai_job

    ctx: dict[str, Any] = dict(req.context or {})
    if "market_gate" not in ctx:
        try:
            gate = evaluate_market_regime_gate()
            ctx["market_gate"] = {
                "regime": gate.get("regime"),
                "regime_label": gate.get("regime_label"),
                "breadth_above_ma20_pct": gate.get("breadth_above_ma20_pct") or gate.get("above_ma20_pct"),
            }
        except Exception:  # noqa: BLE001
            pass

    return submit_ai_job(
        kind=req.kind,
        prompt=req.prompt,
        context=ctx,
        source=req.source,
        wait_timeout_sec=req.wait_timeout_sec,
    )
