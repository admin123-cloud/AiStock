"""从 api/system_config.py 拆出的 routes_maintenance 路由模块。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
import api.system_config as _host
globals().update({k: v for k, v in vars(_host).items() if not k.startswith('__') and k != 'router'})

router = APIRouter()

@router.get("/startup-reference-sync-setting")
async def get_startup_reference_sync_setting():
    return {
        "success": True,
        "data": {
            "enabled": get_startup_reference_sync_enabled(),
            "default_enabled": _startup_reference_sync_default_enabled(),
            "takes_effect_on_restart": True,
        },
    }


@router.get("/qmt-after-close-execution")
async def get_qmt_after_close_execution():
    data = _latest_qmt_after_close_execution()
    data["overnight_repair"] = _latest_qmt_overnight_repair_execution()
    return {"success": True, "data": data}


@router.post("/startup-reference-sync-setting")
async def update_startup_reference_sync_setting(request: StartupReferenceSyncToggleRequest):
    try:
        enabled = set_startup_reference_sync_enabled(request.enabled)
        return {
            "success": True,
            "message": "启动初始化基硢数据弢关已更新，重启后生效",
            "data": {
                "enabled": enabled,
                "default_enabled": _startup_reference_sync_default_enabled(),
                "takes_effect_on_restart": True,
            },
        }
    except Exception as exc:
        logger.error(f"更新启动初始化基硢数据弢关失? {exc}")
        raise HTTPException(status_code=500, detail="task is already running")


@router.post("/core-data-maintenance/start")
async def start_core_data_maintenance():
    try:
        results = start_core_data_maintenance_scheduler()
        return {"success": True, "message": "ok", "data": results}
    except Exception as exc:
        logger.error(f"启动核心数据自动维护失败: {exc}")
        task_manager.set_error("core_data_maintenance", str(exc))
        raise HTTPException(status_code=500, detail="启动核心数据自动维护失败")


@router.post("/core-data-maintenance/stop")
async def stop_core_data_maintenance():
    try:
        results = stop_core_data_maintenance_scheduler()
        return {"success": True, "message": "ok", "data": results}
    except Exception as exc:
        logger.error(f"停止核心数据自动维护失败: {exc}")
        raise HTTPException(status_code=500, detail="停止核心数据自动维护失败")


@router.get("/core-data-maintenance/status")
async def get_core_data_maintenance_status():
    return {"success": True, "data": _build_core_data_maintenance_payload(timeline_limit=20, include_history=False)}


@router.get("/data-source-counts")
async def get_data_source_counts(trade_date: Optional[str] = Query(default=None)):
    return {"success": True, "data": _build_data_source_counts_payload(trade_date)}


@router.post("/data-source-counts/repair-date")
async def repair_data_source_counts_date(trade_date: str = Query(...)):
    normalized_date = _parse_data_source_count_date(trade_date)
    task_name = "data_source_date_repair"
    current = task_manager.get_task_status(task_name)
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="task is already running")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")
    task_manager.update_progress(task_name, {"message": f"任务已入队，准备修复 {normalized_date} 数据?.."})

    thread = threading.Thread(target=data_source_date_repair_task, args=(normalized_date, True), daemon=True)
    thread.start()
    return {
        "success": True,
        "message": f"{normalized_date} 数据源修复任务已启动",
        "task_name": task_name,
        "task": task_manager.get_task_status(task_name),
    }


@router.get("/core-data-maintenance/timeline")
async def get_core_data_maintenance_timeline(limit: int = 30):
    safe_limit = max(1, min(limit, 100))
    data = _build_core_data_maintenance_payload(timeline_limit=safe_limit)
    return {
        "success": True,
        "data": {
            "timeline": data.get("timeline", []),
            "overall_status": data.get("overall_status"),
            "running_task": data.get("running_task"),
        },
    }


@router.post("/core-data-maintenance/run-official-daily-close")
async def run_official_daily_close_now(background_tasks: BackgroundTasks):
    """Manually trigger official daily close sync immediately."""
    task_name = "official_daily_close_sync"
    current = task_manager.get_task_status(task_name)
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="task is already running")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")
    task_manager.update_progress(task_name, {"message": "Task queued and ready to execute..."})

    thread = threading.Thread(target=official_daily_close_sync_task, args=(True,), daemon=True)
    thread.start()
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name),
    }


@router.post("/core-data-maintenance/run-task/{task_key}")
async def run_core_maintenance_task_now(
    task_key: str,
    force: bool = Query(default=False, description="Force intraday tasks even outside trading window"),
    days: int = Query(default=30, ge=1, le=365, description="Repair lookback trading days"),
    periods: Optional[List[str]] = Query(default=None, description="Minute periods to repair"),
):
    """Manually trigger a single core maintenance task immediately."""
    task_configs = {
        "trade_calendar": {
            "task_name": "update_trade_calendar",
            "runner": lambda force=False: threading.Thread(target=update_trade_calendar_task, daemon=True).start(),
            "message": "task message",
        },
        "stock_list_sync": {
            "task_name": "update_stock_list",
            "runner": lambda force=False: threading.Thread(target=update_stock_list_task, daemon=True).start(),
            "message": "task message",
        },
        "index_list_sync": {
            "task_name": "update_index_list",
            "runner": lambda force=False: threading.Thread(target=update_index_list_task, daemon=True).start(),
            "message": "task message",
        },
        "sector_list_sync": {
            "task_name": "sync_sectors",
            "runner": lambda force=False: threading.Thread(target=sync_sectors_task, daemon=True).start(),
            "message": "task message",
        },
        "stock_intraday": {
            "task_name": "update_stock_today_data",
            "runner": lambda force=False: threading.Thread(
                target=update_stock_today_data_task, args=(["1d"], force), daemon=True
            ).start(),
            "message": "task message",
        },
        "market_intraday_minutes": {
            "task_name": "update_market_today_minute_data",
            "runner": lambda force=False: threading.Thread(
                target=update_market_today_minute_data_task, args=(["5m", "15m", "30m", "60m"], force), daemon=True
            ).start(),
            "message": "盘中股票/指数分钟级快照任务已手动启动",
        },
        "market_intraday_kline_refresh": {
            "task_name": "sync_today_intraday_kline",
            "runner": lambda force=False: threading.Thread(
                target=sync_today_intraday_kline_task, kwargs={"task_already_started": True}, daemon=True
            ).start(),
            "message": "当天全市?5m/30m分钟K线落库任务已手动启动",
        },
        "minute_kline_daily_repair_validate": {
            "task_name": "minute_kline_daily_repair_validate",
            "runner": lambda force=False: threading.Thread(
                target=minute_kline_daily_repair_validate_task, args=(True,), daemon=True
            ).start(),
        "message": "朢?个交易日分钟K线补全验证任务已手动启动",
        },
        "market_minute_history_repair": {
            "task_name": "market_minute_history_repair",
            "runner": lambda force=False: threading.Thread(
                target=market_minute_history_repair_task, args=(True, days, periods), daemon=True
            ).start(),
            "message": "股票/指数历史分钟级修复任务已手动启动",
        },
        "sector_intraday_stats_refresh": {
            "task_name": "update_sector_intraday_stats",
            "runner": lambda force=False: threading.Thread(
                target=update_sector_intraday_stats_task, kwargs={"force": force}, daemon=True
            ).start(),
            "message": "task message",
        },
        "official_daily": {
            "task_name": "official_daily_close_sync",
            "runner": lambda force=False: threading.Thread(
                target=official_daily_close_sync_task, args=(True,), daemon=True
            ).start(),
            "message": "task message",
        },
        "repair_daily": {
            "task_name": "repair_previous_daily_kline",
            "runner": lambda force=False: threading.Thread(
                target=repair_previous_daily_kline_task, args=(True,), daemon=True
            ).start(),
            "message": "task message",
        },
        "daily_coverage": {
            "task_name": "daily_kline_coverage_maintenance",
            "runner": lambda force=False: threading.Thread(
                target=daily_kline_coverage_maintenance_task, args=(True,), daemon=True
            ).start(),
            "message": "daily K-line calendar coverage audit and bounded repair started",
        },
    }

    force_tasks = {"stock_intraday", "market_intraday_minutes", "market_intraday_kline_refresh", "sector_intraday_stats_refresh"}
    force_for_task = bool(force) or task_key in force_tasks

    config = task_configs.get(task_key)
    if not config:
        raise HTTPException(status_code=404, detail="未找到对应的维护任务")

    task_name = config["task_name"]
    current = task_manager.get_task_status(task_name)
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="task is already running")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")
    task_manager.update_progress(task_name, {"message": "Task queued and ready to execute..."})

    config["runner"](force=force_for_task)
    return {
        "success": True,
        "message": config["message"],
        "task_name": task_name,
        "task": task_manager.get_task_status(task_name),
    }


@router.post("/tdx-gateway/initialize")
async def initialize_tdx_gateway():
    return {
        "success": False,
        "message": "TDX Gateway is a legacy diagnostics path; QMT/xtquant is the active market-data source.",
        "data": {
            "deprecated": True,
            "replacement": "QMT/xtquant data source maintenance",
            "active_source": "qmt_xtquant",
        },
    }


@router.get("/tdx-gateway/diagnostics")
async def get_tdx_gateway_diagnostics(run_probe: bool = False):
    """R43：GET 只读——run_probe 默认 False（外呼探测 POST 化待批）。"""
    return {
        "success": True,
        "message": "TDX Gateway diagnostics are legacy and disabled; use QMT/xtquant data-source diagnostics.",
        "data": {
            "deprecated": True,
            "requested_run_probe": run_probe,
            "active_source": "qmt_xtquant",
            "replacement": "core-data-maintenance and QMT minute gap repair tasks",
        },
    }


@router.post("/tdx-gateway/probe")
async def probe_tdx_gateway():
    return {
        "success": False,
        "message": "TDX Gateway probe is legacy and disabled; QMT/xtquant is the active market-data source.",
        "data": {
            "deprecated": True,
            "active_source": "qmt_xtquant",
        },
    }


@router.post("/tdx-gateway/recover")
async def recover_tdx_gateway(allow_restart: bool = True):
    return {
        "success": False,
        "message": "TDX Gateway recovery is legacy and disabled; use QMT/xtquant repair paths.",
        "data": {
            "deprecated": True,
            "allow_restart_requested": allow_restart,
            "active_source": "qmt_xtquant",
        },
    }


@router.post("/tdx-gateway/restart")
async def restart_tdx_gateway():
    return {
        "success": False,
        "message": "TDX Gateway restart is legacy and disabled; QMT/xtquant is the active market-data source.",
        "data": {
            "deprecated": True,
            "active_source": "qmt_xtquant",
        },
    }


@router.post("/core-data-maintenance/sync-core-assets")
async def sync_core_assets_now():
    """Manually run the core asset sync pipeline in sequence."""
    current = task_manager.get_task_status("core_data_manual_sync")
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="core data sync task is already running")

    if not kickoff_manual_core_data_sync():
        raise HTTPException(status_code=400, detail="core data sync task is already running")

    return {
        "success": True,
        "message": "核心数据同步任务已启动，将按顺序同步股票、指数板块的列表和当日日K",
        "task_name": "core_data_manual_sync",
        "task": task_manager.get_task_status("core_data_manual_sync"),
    }


@router.post("/core-data-maintenance/refresh-today-full-market")
async def refresh_today_full_market_now():
    """Refresh today's stock/index/sector data and run integrity checks when available."""
    current = task_manager.get_task_status("today_full_market_refresh")
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="today full market refresh task is already running")

    if not kickoff_manual_today_full_market_refresh():
        raise HTTPException(status_code=400, detail="today full market refresh task is already running")

    return {
        "success": True,
        "message": "当天股票、指数板块数据一键更新任务已启动，将按顺序刷新并在盘后执行完整自棢",
        "task_name": "today_full_market_refresh",
        "task": task_manager.get_task_status("today_full_market_refresh"),
    }


