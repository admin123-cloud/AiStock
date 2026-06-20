"""
FastAPI main application entrypoint.
"""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.backtest import router as backtest_router
from api.cls_news import router as cls_news_router
from api.data_statistics import router as data_stats_router
from api.gen3_state_alpha import router as gen3_state_alpha_router
from api.gen3_shadow import router as gen3_shadow_router
from api.kline_check import router as kline_check_router
from api.market_overview import router as market_router
from api.sectors import router as sectors_router
from api.stocks import router as stocks_router
from api.system_config import router as system_router
from api.trading import router as trading_router
from api.watchlist import router as watchlist_router
from utils.logger import get_logger

logger = get_logger("main")


def _strict_tdxquant_startup_enabled() -> bool:
    value = str(os.environ.get("AISTOCK_STRICT_TDXQ_STARTUP", "0")).strip().lower()
    return value in {"1", "true", "yes", "on"}



@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from utils.database import db

        db.ensure_ready_for_startup()
        logger.info("Database tables ensured")
    except Exception as exc:
        logger.error(f"Create tables failed: {exc}")
        raise

    strict_tdxquant_startup = _strict_tdxquant_startup_enabled()
    try:
        from data_fetcher.sources.tdxquant_pool import tdxquant_pool

        tdxquant_pool.require_available_for_startup(notify=strict_tdxquant_startup)
        logger.info("TdxQuant startup health check passed")
    except Exception as exc:
        if strict_tdxquant_startup:
            logger.error(f"TdxQuant startup health check failed: {exc}")
            raise
        logger.warning(f"TdxQuant startup health check failed, continuing in degraded mode: {exc}")

    logger.info("AiStock Backend API started")
    logger.info("API docs: http://localhost:8000/docs")

    try:
        from api.system_config import (
            start_core_data_maintenance_scheduler,
            maybe_run_startup_reference_sync,
        )
        from api.gen3_state_alpha import init_gen3_state_alpha_monitor_scheduler_from_config
        from api.trading import (
            init_gen2_strategy_refresh_scheduler_from_config,
            init_gen2_shadow_buy_monitor_scheduler_from_config,
            init_v4_manual_holdings_monitor_scheduler_from_config,
        )

        start_core_data_maintenance_scheduler()
        logger.info("Core data maintenance scheduler started")
        v4_monitor_status = init_v4_manual_holdings_monitor_scheduler_from_config()
        logger.info(f"V4 manual holdings monitor scheduler initialized: {v4_monitor_status}")
        gen2_shadow_monitor_status = init_gen2_shadow_buy_monitor_scheduler_from_config()
        logger.info(f"G2 shadow buy monitor scheduler initialized: {gen2_shadow_monitor_status}")
        gen2_strategy_refresh_status = init_gen2_strategy_refresh_scheduler_from_config()
        logger.info(f"G2 strategy refresh scheduler initialized: {gen2_strategy_refresh_status}")
        gen3_state_alpha_monitor_status = init_gen3_state_alpha_monitor_scheduler_from_config()
        logger.info(f"G3 State Alpha monitor scheduler initialized: {gen3_state_alpha_monitor_status}")
        if maybe_run_startup_reference_sync():
            logger.info("Startup reference data sync enabled")
        else:
            logger.info("Startup reference data sync skipped")
    except Exception as exc:
        logger.warning(f"Core data maintenance scheduler did not start: {exc}")

    yield

    logger.info("AiStock Backend API stopped")


app = FastAPI(
    title="AiStock Backend API",
    description="Stock data service API",
    version="1.0.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health/check")
async def health_check():
    return {
        "status": "ok",
        "service": "AiStock Backend",
        "version": "1.0.0",
    }


app.include_router(data_stats_router, prefix="/api", tags=["data-statistics"])
app.include_router(stocks_router, prefix="/api")
app.include_router(market_router, prefix="/api")
app.include_router(sectors_router, prefix="/api")
app.include_router(trading_router, prefix="/api")
app.include_router(watchlist_router, prefix="/api")
app.include_router(system_router, prefix="/api", tags=["system"])
app.include_router(kline_check_router, prefix="/api", tags=["kline-check"])
app.include_router(backtest_router, prefix="/api", tags=["backtest"])
app.include_router(gen3_state_alpha_router, prefix="/api")
app.include_router(gen3_shadow_router, prefix="/api")
app.include_router(cls_news_router, prefix="/api")


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
