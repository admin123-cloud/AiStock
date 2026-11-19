"""从 api/system_config.py 拆出的 routes_sync 路由模块。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
import api.system_config as _host
globals().update({k: v for k, v in vars(_host).items() if not k.startswith('__') and k != 'router'})

router = APIRouter()

@router.post("/sync-sectors")
async def trigger_sync_sectors(background_tasks: BackgroundTasks):
    """
    触发同步丢二三行业板块任务
    
    Returns:
        任务状?
    """
    task_name = "sync_sectors"
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=sync_sectors_task, daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/sync-sector-history")
async def trigger_sync_sector_history(background_tasks: BackgroundTasks, days: int = 30):
    """
    触发同步板块历史涨跌数据任务
    
    同步扢有板块的历史涨跌幅成交量、成交额，并计算板块当天上涨家数、下跌家数?
    平盘家数、涨停家数跌停家数等统计信息
    
    Args:
        days: 同步多少天的历史数据，默?0?
    
    Returns:
        任务状?
    """
    task_name = "sync_sector_history"
    
    logger.info(f"接收到同步板块历史数据请求，天数: {days}")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=sync_sector_history_task, args=(days,), daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": f"板块历史涨跌数据同步任务已启动（同步{days}天数据）",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/repair-history-klines")
async def trigger_repair_history_klines(background_tasks: BackgroundTasks, request: RepairKlinesRequest):
    """
    触发修复历史K线数据任?
    
    Args:
        request: 请求体，包含periods字段（要同步的K线级别列表，?["1min", "5min", "daily"]?
                和type字段（要同步的类型，?"index" ?"stock"?
    
    Returns:
        任务状?
    """
    task_name = "repair_history_klines"
    
    # 处理type参数，支持字符串或数组形?
    type_value = request.type
    if isinstance(type_value, list) and len(type_value) > 0:
        type_value = type_value[0]
    
    logger.info(f"接收到K线同步请求，周期参数: {request.periods}，类? {type_value}")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=repair_history_klines_task, args=(request.periods, type_value), daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "历史K线数据同步任务已启动",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/repair-daily-klines")
async def trigger_repair_daily_klines(background_tasks: BackgroundTasks):
    """
    触发修复日K线数据任?
    
    修复扢有股票的日K线涨跌幅、振幅涨跌额等数?
    
    Returns:
        任务状?
    """
    task_name = "repair_daily_klines"
    
    logger.info("task message")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=repair_daily_klines_task, daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "日K线数据修复任务已启动",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/repair-all-history-klines")
async def trigger_repair_all_history_klines(background_tasks: BackgroundTasks):
    """
    触发修复全量历史K线数据任?
    
    结合修复日K线数据和同步全量历史K线，修复扢有股票的历史K线数?
    
    Returns:
        任务状?
    """
    task_name = "repair_all_history_klines"
    
    logger.info("task message")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=repair_all_history_klines_task, daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "修复全量历史K线数据任务已启动",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/update-trade-calendar")
async def trigger_update_trade_calendar(background_tasks: BackgroundTasks):
    """
    触发更新股市日历任务
    
    从达信获取历史交易日期并更新到数据库
    
    Returns:
        任务状?
    """
    task_name = "update_trade_calendar"
    
    logger.info("task message")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=update_trade_calendar_task, daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/update-stock-list")
async def trigger_update_stock_list(background_tasks: BackgroundTasks):
    task_name = "update_stock_list"

    logger.info("task message")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")

    thread = threading.Thread(target=update_stock_list_task, daemon=True)
    thread.start()

    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/update-sector-intraday-stats")
async def trigger_update_sector_intraday_stats(
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False, description="Force intraday stats refresh even outside trading window"),
):
    task_name = "update_sector_intraday_stats"

    logger.info("task message")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")

    thread = threading.Thread(target=update_sector_intraday_stats_task, kwargs={"force": force}, daemon=True)
    thread.start()

    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.get("/trade-calendar/status")
async def get_trade_calendar_status():
    """
    获取交易日历落库状（只读）?
    """
    try:
        from utils.database import db

        with db.engine.connect() as conn:
            try:
                row = conn.execute(
                    text(
                        """
                        SELECT
                            COUNT(1) AS cnt,
                            MIN(trade_date) AS min_date,
                            MAX(trade_date) AS max_date,
                            MAX(updated_at) AS last_updated_at
                        FROM trade_calendar
                        WHERE market = 'SH' AND is_trading = 1
                        """
                    )
                ).fetchone()
            except Exception as exc:
                if "updated_at" not in str(exc):
                    raise
                row = conn.execute(
                    text(
                        """
                        SELECT
                            COUNT(1) AS cnt,
                            MIN(trade_date) AS min_date,
                            MAX(trade_date) AS max_date,
                            NULL AS last_updated_at
                        FROM trade_calendar
                        WHERE market = 'SH' AND is_trading = 1
                        """
                    )
                ).fetchone()

        cnt = int(row[0] or 0) if row else 0
        min_date = row[1].isoformat() if row and row[1] else None
        max_date = row[2].isoformat() if row and row[2] else None
        last_updated_at = row[3].isoformat() if row and row[3] else None

        return {
            "success": True,
            "data": {
                "count": cnt,
                "min_trade_date": min_date,
                "max_trade_date": max_date,
                "last_updated_at": last_updated_at,
                "market": "SH",
            },
        }
    except Exception as e:
        logger.error(f"获取交易日历状失? {e}")
        raise HTTPException(status_code=500, detail="task is already running")


@router.post("/update-today-data")
async def trigger_update_today_data(background_tasks: BackgroundTasks, request: RepairKlinesRequest):
    """
    触发更新当天朢新数据任务（只更新指数）
    
    更新扢有指数的当天朢新数据，包括分钟级别K?
    
    Args:
        request: 请求体，包含periods字段（要更新的K线级别列表，?["1m", "5m", "1d"]?
    
    Returns:
        任务状?
    """
    task_name = "update_today_data"
    
    logger.info(f"接收到当天指数数据更新请求，周期参数: {request.periods}")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=update_today_data_task, args=(request.periods, request.force), daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/update-stock-today-data")
async def trigger_update_stock_today_data(background_tasks: BackgroundTasks, request: RepairKlinesRequest):
    """
    触发更新当天朢新股票数据任?
    
    更新扢有个股的当天朢新数据，包括扢有周期的K?
    
    Args:
        request: 请求体，包含periods字段（要更新的K线级别列表，?["1m", "5m", "1d"]?
    
    Returns:
        任务状?
    """
    task_name = "update_stock_today_data"
    
    logger.info(f"接收到当天股票数据更新请求，周期参数: {request.periods}")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=update_stock_today_data_task, args=(request.periods, request.force), daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/repair-emotion-cycle-30d")
async def trigger_repair_emotion_cycle_30d(background_tasks: BackgroundTasks, days: int = 30):
    """
    丢键修复近 N 个交易日情绪周期数据（默?0）?
    """
    task_name = "repair_emotion_cycle_30d"
    logger.info(f"接收到修复近{days}个交易日情绪周期请求")
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    thread = threading.Thread(target=repair_emotion_cycle_30d_task, args=(days,), daemon=True)
    thread.start()
    return {"success": True, "message": "情绪周期30天修复任务已启动", "task": task_manager.get_task_status(task_name)}


@router.post("/repair-emotion-cycle-latest")
async def trigger_repair_emotion_cycle_latest(background_tasks: BackgroundTasks):
    """
    手动：根据最新日线数据更新情绪指标（朢新交易日）?
    """
    task_name = "repair_emotion_cycle_latest"
    logger.info("接收到重算最新交易日情绪周期请求")
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    thread = threading.Thread(target=repair_emotion_cycle_latest_task, daemon=True)
    thread.start()
    return {"success": True, "message": "朢新情绪周期更新任务已启动", "task": task_manager.get_task_status(task_name)}


@router.post("/emotion-cycle-auto-5m/start")
async def start_emotion_cycle_auto_5m():
    """
    交易日盘中每5分钟自动修复丢次情绪周期（基于朢新日线数据）?
    """
    task_name = "emotion_cycle_auto_5m"
    sched = _ensure_emotion_scheduler()
    try:
        from apscheduler.triggers.cron import CronTrigger

        # Remove an existing task before adding it again to prevent duplicates.
        try:
            sched.remove_job(_emotion_job_id)
        except Exception:
            pass

        # ?分钟触发丢次，实际 job 内会判断是否交易?交易时段
        sched.add_job(
            _emotion_auto_job,
            CronTrigger(minute="*/5"),
            id=_emotion_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

        task_manager.tasks[task_name]["is_running"] = True
        task_manager.tasks[task_name]["started_at"] = datetime.now().isoformat()
        task_manager.update_progress(task_name, {"message": "已开启：交易日盘中每5分钟自动修复"})
        return {"success": True, "message": "ok", "task": task_manager.get_task_status(task_name)}
    except Exception as e:
        logger.error(f"弢启自动修复失? {e}")
        task_manager.set_error(task_name, str(e))
        raise HTTPException(status_code=500, detail="task is already running")


@router.post("/emotion-cycle-auto-5m/stop")
async def stop_emotion_cycle_auto_5m():
    """
    关闭情绪周期自动修复?
    """
    task_name = "emotion_cycle_auto_5m"
    try:
        sched = _ensure_emotion_scheduler()
        try:
            sched.remove_job(_emotion_job_id)
        except Exception:
            pass
        task_manager.tasks[task_name]["is_running"] = False
        task_manager.update_progress(task_name, {"message": "task progress"})
        return {"success": True, "message": "ok", "task": task_manager.get_task_status(task_name)}
    except Exception as e:
        logger.error(f"关闭自动修复失败: {e}")
        task_manager.set_error(task_name, str(e))
        raise HTTPException(status_code=500, detail="关闭自动修复失败")


@router.get("/task-status/{task_name}")
async def get_task_status(task_name: str):
    """
    获取指定任务的状?
    
    Args:
        task_name: 任务名称 (sync_sectors ?repair_history_klines)
    
    Returns:
        任务状?
    """
    if task_name not in task_manager.tasks:
        raise HTTPException(status_code=404, detail="task is already running")
    
    return {
        "success": True,
        "task": task_manager.get_task_status(task_name)
    }


@router.get("/all-tasks-status")
async def get_all_tasks_status():
    """
    获取扢有系统任务的状?
    
    Returns:
        扢有任务状?
    """
    return {
        "success": True,
        "tasks": task_manager.tasks
    }


