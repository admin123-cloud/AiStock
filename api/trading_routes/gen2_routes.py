"""从 api/trading.py 拆出的 gen2_routes 路由模块。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
import api.trading as _host
globals().update({k: v for k, v in vars(_host).items() if not k.startswith('__') and k != 'router'})

router = APIRouter()

@router.get("/gen2/lab")
def get_gen2_strategy_lab(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(30, ge=10, le=50, description="candidate row limit"),
):
    return build_gen2_strategy_lab(signal_date, int(limit))


@router.get("/gen2/live")
def get_gen2_live(
    target_date: Optional[str] = Query(None, description="target trade date"),
):
    state = _load_gen2_shadow_monitor_state()
    reconciled_official_rebuild = _reconcile_official_rebuild_state_if_needed(state, datetime.now())
    requested_date = _normalize_date_str(target_date)
    latest_trade_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    if not latest_trade_date and not requested_date:
        return {
            "available": False,
            "message": "无法解析可用交易日",
            "requested_date": "",
            "selected_date": "",
            "latest_trade_date": "",
            "snapshot_status": "gen2_live",
            "selection": {},
            "positions": {"rows": []},
            "decision": {"mode": "gen2"},
            "gen2_shadow": {},
        }

    date_resolution = _resolve_gen2_signal_date(requested_date or latest_trade_date)
    selected_date = (
        _normalize_date_str(date_resolution.get("effective"))
        if isinstance(date_resolution, dict) and date_resolution.get("effective")
        else requested_date
        or latest_trade_date
    )
    try:
        shadow = get_gen2_risk_cool_shadow(selected_date, 30)
    except Exception as exc:
        logger.exception("gen2/live failed to load risk_cool_shadow")
        shadow = {
            "available": False,
            "rows": [],
            "message": f"G2 risk_cool shadow load failed: {exc}",
            "strategy_name": "G2 V3 User V2",
            "cooldown_policy": "two_stop_cd3_skip",
            "source_latest_date": selected_date or "",
            "signal_date": selected_date or "",
            "row_count": 0,
            "global_summary": _load_gen2_risk_cool_shadow_summary(),
            "data_freshness": build_gen2_data_freshness(selected_date, None),
            "verification_options": _gen2_shadow_verification_options(),
            "verification_summary": _build_gen2_shadow_verification_summary([]),
            "verification_global_summary": _build_gen2_shadow_verification_summary([]),
            "verification_queue": [],
            "requested_date": requested_date,
            "selection_signal_date": selection_signal_date,
            "display_signal_date": selection_signal_date or requested_date or "",
            "ledger_signal_date": "",
            "is_ledger_fallback": False,
            "date_notice": "",
        }
    try:
        selection_pool = _build_gen2_live_selection_summary(selected_date) if selected_date else {}
        selection_signal_date = _normalize_date_str(selection_pool.get("signal_date") or selected_date)
    except Exception as exc:
        logger.exception("gen2/live failed to build selection_pool")
        selection_pool = {
            "summary": {},
            "branch_freshness": {},
            "live_trade_mode": "latest_driven_mainline",
            "official_branch_blocks_live": False,
            "message": f"G2 selection pool build failed: {exc}",
        }
        selection_signal_date = selected_date
    latest = shadow.get("source_latest_date") or selected_date or ""
    shadow_signal_date = _normalize_date_str(shadow.get("signal_date"))
    fallback = _build_live_fallback_info(requested_date, selection_signal_date, latest_trade_date, latest)
    selected_date_display = _normalize_date_str(selection_signal_date or shadow_signal_date or selected_date or latest)
    fallback_title = str(fallback.get("title") or "").strip()
    return _sanitize(
        {
            "available": bool(shadow.get("available") or selected_date),
            "message": shadow.get("message") or "G2 live data loaded.",
            "requested_date": target_date or "",
            "selected_date": selected_date_display,
            "shadow_signal_date": shadow_signal_date or "",
            "selection_signal_date": selection_signal_date or "",
            "latest_trade_date": latest,
            "snapshot_status": "gen2_live",
            "date_resolution": date_resolution,
            "official_rebuild": reconciled_official_rebuild or state.get("last_official_rebuild_result"),
            "fallback": fallback,
            "fallback_notice": fallback_title,
            "selection": {
                "summary": {
                    "strategy_name": "G2 second-generation strategy",
                    "candidate_count": int(selection_pool.get("summary", {}).get("candidate_count") or shadow.get("row_count") or 0),
                },
                "branch_freshness": selection_pool.get("branch_freshness") or {},
                "live_trade_mode": selection_pool.get("live_trade_mode") or "latest_driven_mainline",
                "official_branch_blocks_live": bool(selection_pool.get("official_branch_blocks_live")),
            },
            "positions": {"rows": []},
            "decision": {
                "mode": "gen2",
                "can_open": True,
                "message": "G2 live mode uses second-generation signal, monitor and manual-holding controls.",
            },
            "gen2_shadow": shadow,
        }
    )


@router.get("/gen2/open-signals")
def get_gen2_open_signals(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(30, ge=10, le=100, description="candidate row limit"),
):
    return build_gen2_open_signals(signal_date, int(limit))


@router.get("/gen2/timing")
def get_gen2_timing(
    signal_date: Optional[str] = Query(None, description="signal date"),
    curve_days: int = Query(420, ge=80, le=1500, description="chart trading days"),
    index_code: str = Query("999999.SH", description="chart index code"),
):
    return build_gen2_timing(signal_date, int(curve_days), index_code)


@router.get("/gen2/backtest")
def get_gen2_backtest_history(
    strategy_code: str = Query("g2_alpha191_volume5_keep80_runup", description="gen2 strategy code"),
):
    return build_gen2_backtest_history(strategy_code)


@router.post("/gen2/backtest/update-latest")
def run_gen2_backtest_update_latest(
    end_date: Optional[str] = Query(None, description="optional backtest end date; blank means latest available source date"),
    legacy_330: bool = Query(True, description="Use legacy 330% g2_v2 complete build mode"),
):
    selected_end_date = _normalize_date_str(end_date) if end_date else None
    with GEN2_BACKTEST_UPDATE_TASK_LOCK:
        for task in GEN2_BACKTEST_UPDATE_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    task_id = f"gen2_backtest_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_gen2_backtest_update_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "end_date": selected_end_date or "",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    thread = threading.Thread(
        target=_run_gen2_backtest_update_task,
        args=(task_id, selected_end_date, bool(legacy_330)),
        name=f"gen2-backtest-update-{task_id}",
        daemon=True,
    )
    thread.start()
    return _get_gen2_backtest_update_task(task_id)


@router.get("/gen2/backtest/update-task/{task_id}")
def get_gen2_backtest_update_task(task_id: str):
    task = _get_gen2_backtest_update_task(task_id)
    if task:
        return task
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.post("/gen2/official-rebuild/update-latest")
def run_gen2_official_rebuild_update_latest(
    end_date: Optional[str] = Query(None, description="optional official rebuild end date; blank means latest trade date"),
    mode: str = Query("full", description="mainline | breakout_only | full"),
    legacy_330: bool = Query(True, description="Use legacy 330% g2_v2 complete build mode"),
):
    selected_end_date = _normalize_date_str(end_date) if end_date else _normalize_date_str(_resolve_latest_stock_trade_date())
    if not selected_end_date:
        return {"status": "failed", "progress": 100, "error": "无法解析正式分支重建日期"}
    normalized_mode = str(mode or "full").strip().lower()
    if normalized_mode not in {"mainline", "breakout_only", "full"}:
        return {"status": "failed", "progress": 100, "error": "Unsupported mode"}
    state = _load_gen2_shadow_monitor_state()
    _reconcile_official_rebuild_state_if_needed(state)
    active = _find_active_gen2_official_rebuild_task()
    if active:
        return active
    return _start_gen2_official_rebuild_task(
        selected_end_date,
        include_breakout=(normalized_mode == "full"),
        timeout_seconds=int(state.get("official_rebuild_timeout_seconds") or 10800),
        breakout_only=(normalized_mode == "breakout_only"),
        legacy_330=bool(legacy_330),
    )


@router.get("/gen2/official-rebuild/update-task/{task_id}")
def get_gen2_official_rebuild_update_task(task_id: str):
    task = _get_gen2_official_rebuild_task(task_id)
    if task:
        return task
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.get("/gen2/factors/registry")
def get_gen2_factor_registry():
    return build_gen2_factor_registry()


@router.get("/gen2/factors/test")
def get_gen2_factor_test(
    factor_id: str = Query("Alpha001", description="factor id, e.g. Alpha001"),
    start_date: Optional[str] = Query(None, description="test start date"),
    end_date: Optional[str] = Query(None, description="test end date"),
    horizon: int = Query(1, ge=1, le=20, description="forward return horizon in trading bars"),
    min_symbols: int = Query(80, ge=20, le=1000, description="minimum daily cross-section size"),
):
    return run_gen2_factor_test(factor_id, start_date, end_date, int(horizon), int(min_symbols))


@router.get("/gen2/selection-pool")
def get_gen2_selection_pool(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(80, ge=10, le=300, description="candidate row limit"),
):
    state = _load_gen2_shadow_monitor_state()
    reconciled_official_rebuild = _reconcile_official_rebuild_state_if_needed(state, datetime.now())
    requested_date = _normalize_date_str(signal_date)
    latest_trade_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    date_resolution = _resolve_gen2_signal_date(requested_date or latest_trade_date) if requested_date or latest_trade_date else {}
    selected_date = (
        _normalize_date_str(date_resolution.get("effective"))
        if isinstance(date_resolution, dict) and date_resolution.get("effective")
        else requested_date
        or latest_trade_date
    )
    try:
        payload = _build_gen2_selection_pool(selected_date, int(limit))
        payload["official_rebuild"] = reconciled_official_rebuild or state.get("last_official_rebuild_result")
        payload["date_resolution"] = date_resolution
        payload["fallback"] = _build_live_fallback_info(
            requested_date,
            str(payload.get("signal_date") or selected_date),
            latest_trade_date,
            str(payload.get("source_latest_date") or payload.get("signal_date") or selected_date or ""),
        )
        return payload
    except Exception as exc:
        logger.exception("gen2/selection-pool failed, returning fallback payload")
        payload = _build_gen2_selection_pool_error_payload(selected_date, int(limit), str(exc))
        payload["official_rebuild"] = reconciled_official_rebuild or state.get("last_official_rebuild_result")
        payload["date_resolution"] = date_resolution
        payload["fallback"] = _build_live_fallback_info(
            requested_date,
            str(payload.get("signal_date") or selected_date),
            latest_trade_date,
            str(payload.get("source_latest_date") or payload.get("signal_date") or selected_date or ""),
        )
        return payload


@router.get("/gen2/mainline-hotspots")
def get_gen2_mainline_hotspots(
    target_date: Optional[str] = Query(None, description="target snapshot date"),
    limit: int = Query(30, ge=5, le=120, description="row limit per section"),
):
    return build_gen2_mainline_hotspots(target_date, int(limit))


@router.post("/gen2/mainline-hotspots/update")
def run_gen2_mainline_hotspots_update(
    target_date: Optional[str] = Query(None, description="target snapshot date"),
    limit: int = Query(30, ge=5, le=120, description="row limit per section"),
    mode: str = Query("all", description="refresh mode: all | sector | theme"),
):
    active_task = _get_active_gen2_mainline_update_task()
    if active_task:
        return active_task
    selected_date = _normalize_date_str(target_date) if target_date else None
    normalized_mode = str(mode or "all").strip().lower()
    if normalized_mode not in {"all", "sector", "theme"}:
        normalized_mode = "all"
    task_id = f"gen2_mainline_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    step_total = 7 if normalized_mode == "all" else 3 if normalized_mode == "sector" else 4
    _set_gen2_mainline_update_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "target_date": selected_date,
            "limit": int(limit),
            "mode": normalized_mode,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "steps_total": step_total,
            "steps_completed": 0,
            "steps": [],
            "message": "主线热点产物刷新已入队",
        },
    )
    thread = threading.Thread(
        target=_run_gen2_mainline_update_task,
        args=(task_id, selected_date, int(limit), normalized_mode),
        name=f"gen2-mainline-update-{task_id}",
        daemon=True,
    )
    thread.start()
    return _get_gen2_mainline_update_task(task_id) or {"task_id": task_id, "status": "queued", "progress": 0}


@router.get("/gen2/mainline-hotspots/update-task/{task_id}")
def get_gen2_mainline_hotspots_update_task(task_id: str):
    task = _get_gen2_mainline_update_task(task_id)
    if not task:
        return {"task_id": task_id, "status": "missing", "progress": 0, "message": "任务不存在"}
    return _sanitize(task)


@router.get("/gen2/workflow/status")
def get_gen2_workflow_status(
    signal_date: Optional[str] = Query(None, description="signal date"),
):
    state = _load_gen2_shadow_monitor_state()
    # R43：GET 只读——不再触发状态 reconcile 落盘（_save_gen2_shadow_monitor_state），
    # official_rebuild 直接取既有状态值；需要刷新走写面 POST。
    reconciled_official_rebuild = None
    requested_date = _normalize_date_str(signal_date)
    latest_trade_date = _normalize_date_str(_resolve_latest_stock_trade_date())
    if not requested_date:
        requested_date = latest_trade_date
    date_resolution = _resolve_gen2_signal_date(requested_date) if requested_date else {}
    selected_date = (
        _normalize_date_str(date_resolution.get("effective"))
        if isinstance(date_resolution, dict) and date_resolution.get("effective")
        else requested_date
        or latest_trade_date
    )
    date_resolution_reason = str(date_resolution.get("reason") or "").strip() or None
    date_resolution_status = date_resolution.get("status") if isinstance(date_resolution.get("status"), dict) else None
    if not selected_date:
        return {"available": False, "ok": False, "message": "无法解析目标信号日，返回空状态。", "pipeline_stages": [], "blockers": []}
    check = _check_gen2_shadow_blockers(
        selected_date,
        datetime.now(),
        alpha191_gate=str(state.get("alpha191_gate") or "g2_v2_complete"),
        recipient_override=state.get("recipient_email"),
    )
    # R43：GET 只读——现场快照，绝不 configure（零 add/remove_job/start）。
    monitor_status = _gen2_shadow_buy_monitor_status_readonly()
    return _sanitize(
        {
            "available": True,
            "ok": bool(check.get("ok")),
            "signal_date": selected_date,
            "checked_at": check.get("checked_at"),
            "prev_trade_date": check.get("prev_trade_date"),
            "alpha191_gate": str(state.get("alpha191_gate") or "g2_v2_complete"),
            "official_rebuild": reconciled_official_rebuild or state.get("last_official_rebuild_result"),
            "monitor": monitor_status,
            "pipeline_stages": check.get("pipeline_stages") or [],
            "pipeline_checks": check.get("pipeline_checks") or [],
            "requested_date": requested_date,
            "date_resolution": date_resolution,
            "date_resolution_reason": date_resolution_reason,
            "date_resolution_status": date_resolution_status,
            "lineage_items": _gen2_workflow_lineage(selected_date, check.get("prev_trade_date")),
            "blockers": check.get("blockers") or [],
            "last_result": state.get("last_result"),
            "message": "工作流当前可执行" if check.get("ok") else "工作流当前不可就绪，需先补齐上游依赖",
        }
    )


@router.get("/gen2/workflow/lineage")
def get_gen2_workflow_lineage(
    signal_date: Optional[str] = Query(None, description="signal date"),
):
    selected_date = _normalize_date_str(signal_date) if signal_date else _normalize_date_str(_resolve_latest_stock_trade_date())
    if not selected_date:
        return {"available": False, "items": [], "message": "无法解析目标信号日。"}
    prev_trade_date = _previous_stock_trade_date(selected_date)
    return _sanitize(
        {
            "available": True,
            "signal_date": selected_date,
            "prev_trade_date": prev_trade_date,
            "items": _gen2_workflow_lineage(selected_date, prev_trade_date),
        }
    )


@router.get("/gen2/daily-trade-ticket")
def get_gen2_daily_trade_ticket(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(30, ge=10, le=100, description="source candidate row limit"),
    formal_limit: int = Query(3, ge=1, le=5, description="formal trade ticket candidate limit"),
    persist: bool = Query(True, description="write JSON and Markdown ticket artifacts"),
):
    # R43：GET 只读——persist 参数优雅忽略（恒不落盘，_save_gen2_daily_trade_ticket
    # 零调用）；outputs 键保留旧形状仅报告目标路径。落盘通道迁移 POST 待批。
    ticket = _build_gen2_daily_trade_ticket_payload(
        signal_date=signal_date,
        limit=limit,
        formal_limit=formal_limit,
    )
    selected = _normalize_date_str(ticket.get("signal_date")) or datetime.now().strftime("%Y-%m-%d")
    ticket["outputs"] = {
        "json_path": str(GEN2_DAILY_TICKET_DIR / f"{selected}.json"),
        "markdown_path": str(GEN2_DAILY_TICKET_DIR / f"{selected}.md"),
        "latest_path": str(GEN2_DAILY_TICKET_LATEST_PATH),
    }
    return ticket


@router.get("/gen2/daily-trade-ticket/execution")
def get_gen2_daily_trade_execution(
    signal_date: Optional[str] = Query(None, description="signal date"),
):
    selected_date = _normalize_date_str(signal_date)
    if not selected_date:
        ticket = _build_gen2_daily_trade_ticket_payload(None, limit=30, formal_limit=3)
        selected_date = _normalize_date_str(ticket.get("signal_date"))
    record = _find_gen2_daily_execution_record(selected_date or "")
    return _sanitize(
        {
            "available": bool(record),
            "signal_date": selected_date or "",
            "record": record,
            "ledger_path": str(GEN2_DAILY_EXECUTION_LEDGER_PATH),
        }
    )


@router.post("/gen2/daily-trade-ticket/execution")
def save_gen2_daily_trade_execution(
    payload: Dict[str, Any] = Body(...),
):
    return _upsert_gen2_daily_execution_record(payload or {})


@router.get("/gen2/risk-cool-shadow")
def get_gen2_risk_cool_shadow(
    signal_date: Optional[str] = Query(None, description="signal date"),
    limit: int = Query(30, ge=5, le=100, description="candidate row limit"),
):
    ledger_df = _load_gen2_risk_cool_shadow_ledger()
    requested_date = _normalize_date_str(signal_date) if signal_date else None
    selection_signal_date = ""
    try:
        selection_summary = _build_gen2_live_selection_summary(requested_date) if requested_date else {}
        selection_signal_date = _normalize_date_str(selection_summary.get("signal_date") or "")
    except Exception as exc:
        logger.warning(f"load gen2 live selection summary for shadow view failed: {exc}")
        selection_signal_date = ""
    if ledger_df.empty or "entry_date" not in ledger_df.columns:
        return {
            "available": False,
            "rows": [],
            "message": "G2 V3 User V2 two_stop_cd3影子台账尚未生成",
            "strategy_name": "G2 V3 User V2",
            "cooldown_policy": "two_stop_cd3_skip",
            "global_summary": _load_gen2_risk_cool_shadow_summary(),
            "data_freshness": build_gen2_data_freshness(signal_date, None),
            "verification_options": _gen2_shadow_verification_options(),
            "verification_summary": _build_gen2_shadow_verification_summary([]),
            "verification_global_summary": _build_gen2_shadow_verification_summary([]),
            "verification_queue": [],
            "requested_date": requested_date,
            "selection_signal_date": selection_signal_date,
            "display_signal_date": selection_signal_date or requested_date or "",
            "ledger_signal_date": "",
            "is_ledger_fallback": False,
            "date_notice": "",
        }

    all_dates = sorted([str(x) for x in ledger_df["entry_date"].dropna().unique() if str(x)])
    if not all_dates:
        return {
            "available": False,
            "rows": [],
            "message": "G2 V3 User V2 two_stop_cd3影子台账没有可用日期",
            "strategy_name": "G2 V3 User V2",
            "cooldown_policy": "two_stop_cd3_skip",
            "global_summary": _load_gen2_risk_cool_shadow_summary(),
            "data_freshness": build_gen2_data_freshness(signal_date, None),
            "verification_options": _gen2_shadow_verification_options(),
            "verification_summary": _build_gen2_shadow_verification_summary([]),
            "verification_global_summary": _build_gen2_shadow_verification_summary([]),
            "verification_queue": [],
            "requested_date": requested_date,
            "selection_signal_date": selection_signal_date,
            "display_signal_date": selection_signal_date or requested_date or "",
            "ledger_signal_date": "",
            "is_ledger_fallback": False,
            "date_notice": "",
        }

    selected_date = all_dates[-1]
    if requested_date:
        earlier_dates = [x for x in all_dates if x <= requested_date]
        selected_date = earlier_dates[-1] if earlier_dates else all_dates[-1]
    display_signal_date = selection_signal_date or requested_date or selected_date
    is_ledger_fallback = bool(display_signal_date and display_signal_date != selected_date)
    date_notice = (
        f"最新 G2 候选日期为 {display_signal_date}，但影子复盘台账最新仅到 {selected_date}；当前先展示最近可用复盘样本。"
        if is_ledger_fallback
        else ""
    )

    date_notice = (
        f"Latest G2 candidate date is {display_signal_date}, but shadow review ledger latest is {selected_date}; showing latest available review rows."
        if is_ledger_fallback
        else ""
    )
    day_df = ledger_df[ledger_df["entry_date"] == selected_date].copy()
    priority = {"executed": 0, "observable": 1, "suspended_by_two_stop_cd3": 2, "suspended_by_stop_cd5": 2}
    if "shadow_status" in day_df.columns:
        day_df["_status_priority"] = day_df["shadow_status"].map(lambda x: priority.get(str(x), 9))
    else:
        day_df["_status_priority"] = 9
    if "day_signal_rank" not in day_df.columns:
        day_df["day_signal_rank"] = np.nan
    day_df = day_df.sort_values(["_status_priority", "day_signal_rank", "confirm_datetime"], na_position="last").head(int(limit))

    rows: List[Dict[str, Any]] = []
    for _, item in day_df.iterrows():
        status = str(item.get("shadow_status") or "").strip()
        is_suspended = status in {"suspended_by_two_stop_cd3", "suspended_by_stop_cd5"}
        code = str(item.get("code") or "").strip()
        code6 = _normalize_stock_code6(code)
        reason = str(item.get("execution_note") or "").strip()
        if is_suspended:
            reason = reason or "已暂停：命中 two_stop_cd3_skip 策略拦截。"
        elif status == "observable":
            reason = reason or "候选信号待观察，尚未触发执行。"
        elif status == "executed":
            reason = reason or "买入指令已进入执行记录。"
        rows.append(
            {
                "code": _to_exchange_stock_code(code6) if code6 else code,
                "code6": code6,
                "name": str(item.get("name") or ""),
                "entry_date": str(item.get("entry_date") or ""),
                "confirm_datetime": str(item.get("confirm_datetime") or ""),
                "shadow_status": status,
                "status_label": _gen2_shadow_status_label(status),
                "can_observe": not is_suspended,
                "is_suspended": is_suspended,
                "day_signal_rank": _to_int(item.get("day_signal_rank"), default=0) or None,
                "entry_price": _to_float(item.get("entry_price")),
                "v4_rank": _to_int(item.get("v4_rank"), default=0) or None,
                "v4_score": _to_float(item.get("v4_score")),
                "rt_return_pct": _pct_value(item.get("rt_return_from_d1_close")),
                "amount_ratio": _to_float(item.get("rt_30m_amount_ratio")),
                "mom5_pct": _pct_value(item.get("mom5")),
                "mom20_pct": _pct_value(item.get("mom20")),
                "vol_ratio": _to_float(item.get("vol_ratio")),
                "alpha191_gate_enabled": bool(item.get("alpha191_gate_enabled")),
                "alpha191_gate_variant": "" if pd.isna(item.get("alpha191_gate_variant")) else str(item.get("alpha191_gate_variant") or ""),
                "alpha191_factor_date": str(item.get("alpha191_factor_date") or ""),
                "alpha191_gate_score": _to_float(item.get("alpha191_gate_score")),
                "alpha191_gate_threshold": _to_float(item.get("alpha191_gate_threshold")),
                "alpha191_gate_pass": bool(item.get("alpha191_gate_pass")),
                "alpha191_gate_reason": str(item.get("alpha191_gate_reason") or ""),
                "alpha191_volume5_score": _to_float(item.get("alpha191_volume5_score")),
                "alpha191_volume5_rank_in_day": _to_int(item.get("alpha191_volume5_rank_in_day"), default=0) or None,
                "alpha191_original_v4_rank": _to_int(item.get("alpha191_original_v4_rank"), default=0) or None,
                "runup_from_60d_low": _to_float(item.get("runup_from_60d_low")),
                "overhead_pressure_amount_share": _to_float(item.get("overhead_pressure_amount_share")),
                "fwd5_pct": _pct_value(item.get("outcome_fwd_ret_5d")),
                "fwd10_pct": _pct_value(item.get("outcome_fwd_ret_10d")),
                "fwd20_pct": _pct_value(item.get("outcome_fwd_ret_20d")),
                "stop5_touch_30m": bool(item.get("stop5_touch_30m")),
                "good": bool(item.get("outcome_good")),
                "bad": bool(item.get("outcome_bad")),
                "reason_text": reason,
            }
        )

    rows = _apply_gen2_shadow_verifications(rows)
    counts = {
        str(k): int(v)
        for k, v in ledger_df[ledger_df["entry_date"] == selected_date]["shadow_status"].value_counts(dropna=False).items()
    }
    result = {
        "available": True,
        "signal_date": selected_date,
        "requested_date": requested_date,
        "source_latest_date": all_dates[-1],
        "display_signal_date": display_signal_date,
        "selection_signal_date": selection_signal_date,
        "ledger_signal_date": selected_date,
        "is_ledger_fallback": is_ledger_fallback,
        "date_notice": date_notice,
        "strategy_name": "G2 V3 User V2",
        "cooldown_policy": "two_stop_cd3_skip",
        "cooldown_active": (counts.get("suspended_by_two_stop_cd3", 0) + counts.get("suspended_by_stop_cd5", 0)) > 0,
        "row_count": int(len(rows)),
        "counts": counts,
        "rows": rows,
        "global_summary": _load_gen2_risk_cool_shadow_summary(),
        "data_freshness": build_gen2_data_freshness(requested_date, selected_date),
        "verification_options": _gen2_shadow_verification_options(),
        "verification_summary": _build_gen2_shadow_verification_summary(rows),
        "verification_global_summary": _build_gen2_shadow_global_verification_summary(ledger_df),
        "verification_queue": _build_gen2_shadow_verification_queue(ledger_df),
        "message": "G2 V3 User V2 two_stop_cd3影子台账只用于观察和风控确认，不代表自动买入指令",
    }
    return _sanitize(result)


@router.post("/gen2/risk-cool-shadow/update")
def run_gen2_risk_cool_shadow_update(
    signal_date: Optional[str] = Query(None, description="signal date"),
    pool_rank: int = Query(200, ge=50, le=500, description="D-1 V4 rank pool upper bound"),
    alpha191_gate: str = Query("off", description="Alpha191 T-1 gate"),
):
    selected_date = _normalize_date_str(signal_date) if signal_date else _normalize_date_str(_resolve_latest_stock_trade_date())
    alpha191_gate = str(alpha191_gate or "off").strip().lower()
    if alpha191_gate not in GEN2_ALPHA191_ACTIVE_GATES:
        return {"status": "failed", "progress": 100, "error": "Unsupported alpha191_gate"}
    if not selected_date:
        return {"status": "failed", "progress": 100, "error": "无法解析影子交易更新日期"}
    with GEN2_SHADOW_UPDATE_TASK_LOCK:
        for task in GEN2_SHADOW_UPDATE_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    task_id = f"gen2_shadow_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_gen2_shadow_update_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "signal_date": selected_date,
            "pool_rank": int(pool_rank),
            "alpha191_gate": alpha191_gate,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    thread = threading.Thread(
        target=_run_gen2_shadow_update_task,
        args=(task_id, selected_date, int(pool_rank), alpha191_gate),
        name=f"gen2-shadow-update-{task_id}",
        daemon=True,
    )
    thread.start()
    return _get_gen2_shadow_update_task(task_id)


@router.get("/gen2/risk-cool-shadow/update-task/{task_id}")
def get_gen2_risk_cool_shadow_update_task(task_id: str):
    task = _get_gen2_shadow_update_task(task_id)
    if task:
        return task
    return {"task_id": task_id, "status": "missing", "progress": 0, "error": "task not found"}


@router.post("/gen2/risk-cool-shadow/verification")
def save_gen2_risk_cool_shadow_verification(payload: Dict[str, Any]):
    return save_gen2_shadow_verification(payload if isinstance(payload, dict) else {})


@router.post("/gen2/backtest/trade-classification")
def save_gen2_backtest_trade_classification(payload: Dict[str, Any]):
    return update_gen2_trade_classification(payload if isinstance(payload, dict) else {})


@router.post("/gen2/paper-order")
def submit_gen2_paper_order(payload: Dict[str, Any]):
    started_at = datetime.now()
    data = payload if isinstance(payload, dict) else {}
    signal_date = _normalize_date_str(data.get("signal_date")) or _normalize_date_str(_resolve_latest_stock_trade_date())
    code6 = _normalize_stock_code6(data.get("code"))
    if not signal_date:
        return _sanitize({"ok": False, "message": "Unable to resolve G2 signal date."})
    if not code6:
        return _sanitize({"ok": False, "message": "Unable to resolve submitted 6-digit stock code."})

    row = _candidate_from_order_payload(data, signal_date, code6)
    if row is None:
        submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
        return _sanitize(
            {
                "ok": False,
                "message": f"{code6} {signal_date} is missing the page candidate snapshot; refresh the G2 page and retry.",
                "candidate": None,
                "candidate_lookup_mode": "request_snapshot_missing",
                "submit_elapsed_seconds": submit_elapsed_seconds,
            }
        )
    candidate_lookup_mode = "request_snapshot"
    if not bool(row.get("buyable")):
        return _sanitize({"ok": False, "message": f"{code6} is not buyable now.", "candidate": row, "candidate_lookup_mode": candidate_lookup_mode})
    if bool(row.get("is_suspended")):
        return _sanitize({"ok": False, "message": f"{code6} is suspended or limit-up now.", "candidate": row, "candidate_lookup_mode": candidate_lookup_mode})

    gate = _market_gate_from_order_payload(data, signal_date)
    if gate is None:
        submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
        return _sanitize(
            {
                "ok": False,
                "message": f"{signal_date} is missing the market gate snapshot; refresh the G2 page and retry.",
                "candidate": row,
                "market_gate": None,
                "candidate_lookup_mode": candidate_lookup_mode,
                "market_gate_lookup_mode": "request_snapshot_missing",
                "submit_elapsed_seconds": submit_elapsed_seconds,
            }
        )
    market_gate_lookup_mode = "request_snapshot"
    if not bool(gate.get("available")):
        return _sanitize({"ok": False, "message": gate.get("message") or "Market gate is unavailable; order is blocked.", "candidate": row, "market_gate": gate, "candidate_lookup_mode": candidate_lookup_mode, "market_gate_lookup_mode": market_gate_lookup_mode})
    if not bool(gate.get("can_open")):
        return _sanitize({"ok": False, "message": gate.get("message") or "Market gate blocks opening a position.", "candidate": row, "market_gate": gate, "candidate_lookup_mode": candidate_lookup_mode, "market_gate_lookup_mode": market_gate_lookup_mode})

    quantity = _to_int(data.get("quantity"), default=0) or 0
    if quantity <= 0:
        quantity = _default_gen2_paper_quantity(row.get("entry_price"))
    if quantity <= 0 or quantity % 100 != 0:
        return _sanitize({"ok": False, "message": "Order quantity must be positive and a multiple of 100.", "candidate": row})

    submit_elapsed_seconds = round((datetime.now() - started_at).total_seconds(), 6)
    return _sanitize(
        {
            "ok": False,
            "message": "Legacy file-bridge paper order has been retired; use the G3 paper execution workflow or QMT Mini trading path.",
            "candidate": row,
            "market_gate": gate,
            "candidate_lookup_mode": candidate_lookup_mode,
            "market_gate_lookup_mode": market_gate_lookup_mode,
            "submit_elapsed_seconds": submit_elapsed_seconds,
        }
    )


@router.get("/gen2/shadow-monitor/status")
def get_gen2_shadow_buy_monitor_status():
    # R43：GET 只读——现场快照，绝不 configure（零 add/remove_job/start）。
    return _sanitize(_gen2_shadow_buy_monitor_status_readonly())


@router.post("/gen2/shadow-monitor/config")
def set_gen2_shadow_buy_monitor_config(payload: Dict[str, Any]):
    state = _load_gen2_shadow_monitor_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "official_rebuild_enabled" in payload:
            state["official_rebuild_enabled"] = bool(payload.get("official_rebuild_enabled"))
        if "official_rebuild_legacy_330" in payload:
            state["official_rebuild_legacy_330"] = bool(payload.get("official_rebuild_legacy_330"))
        if "official_rebuild_include_breakout" in payload:
            state["official_rebuild_include_breakout"] = bool(payload.get("official_rebuild_include_breakout"))
        if "official_rebuild_timeout_seconds" in payload:
            state["official_rebuild_timeout_seconds"] = max(300, _to_int(payload.get("official_rebuild_timeout_seconds"), 10800))
        if "official_rebuild_retry_minutes" in payload:
            state["official_rebuild_retry_minutes"] = max(1, _to_int(payload.get("official_rebuild_retry_minutes"), 10))
        if "interval_seconds" in payload:
            state["interval_seconds"] = max(120, _to_int(payload.get("interval_seconds"), 120))
        if "pool_rank" in payload:
            state["pool_rank"] = min(500, max(50, _to_int(payload.get("pool_rank"), 200)))
        if "alpha191_gate" in payload:
            alpha191_gate = str(payload.get("alpha191_gate") or "off").strip().lower()
            state["alpha191_gate"] = alpha191_gate if alpha191_gate in GEN2_ALPHA191_ACTIVE_GATES else "off"
        if "recipient_email" in payload:
            state["recipient_email"] = str(payload.get("recipient_email") or "").strip()
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "heartbeat_enabled" in payload:
            state["heartbeat_enabled"] = bool(payload.get("heartbeat_enabled"))
        if "heartbeat_email_minutes" in payload:
            state["heartbeat_email_minutes"] = max(30, _to_int(payload.get("heartbeat_email_minutes"), 30))
    _save_gen2_shadow_monitor_state(state)
    return _sanitize(_configure_gen2_shadow_buy_monitor_scheduler())


@router.post("/gen2/shadow-monitor/run-once")
def run_gen2_shadow_buy_monitor_once(payload: Dict[str, Any]):
    recipient_email = None
    force_send = False
    if isinstance(payload, dict):
        txt = str(payload.get("recipient_email") or "").strip()
        recipient_email = txt if txt else None
        force_send = bool(payload.get("force_send"))
    result = _run_gen2_shadow_buy_monitor(force_send=force_send, recipient_override=recipient_email)
    return _sanitize(result)


@router.get("/gen2/strategy-refresh/status")
def get_gen2_strategy_refresh_status():
    # R43：GET 只读——现场快照，绝不 configure（零 add/remove_job/start）。
    return _sanitize(_gen2_strategy_refresh_status_readonly())


@router.post("/gen2/strategy-refresh/config")
def set_gen2_strategy_refresh_config(payload: Dict[str, Any]):
    state = _load_gen2_strategy_refresh_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "data_delay_minutes" in payload:
            state["data_delay_minutes"] = min(20, max(0, _to_int(payload.get("data_delay_minutes"), 2)))
        if "run_shadow_monitor" in payload:
            state["run_shadow_monitor"] = bool(payload.get("run_shadow_monitor"))
        if "run_mainline_hotspots" in payload:
            state["run_mainline_hotspots"] = bool(payload.get("run_mainline_hotspots"))
        if "mainline_mode" in payload:
            mode = str(payload.get("mainline_mode") or "sector").strip().lower()
            state["mainline_mode"] = mode if mode in {"all", "sector", "theme"} else "sector"
        if "mainline_limit" in payload:
            state["mainline_limit"] = min(120, max(5, _to_int(payload.get("mainline_limit"), 30)))
        if "force_each_bar_once" in payload:
            state["force_each_bar_once"] = bool(payload.get("force_each_bar_once"))
    _save_gen2_strategy_refresh_state(state)
    return _sanitize(_configure_gen2_strategy_refresh_scheduler())


@router.post("/gen2/strategy-refresh/run-once")
def run_gen2_strategy_refresh_once(payload: Dict[str, Any]):
    force = True
    if isinstance(payload, dict) and "force" in payload:
        force = bool(payload.get("force"))
    return _sanitize(_run_gen2_strategy_refresh_30m(force=force, source="manual"))


