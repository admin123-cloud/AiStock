"""
FastAPI main application entrypoint.
"""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.accounts import router as accounts_router
from api.data_center import router as data_center_router
from api.market_board import router as market_board_router
from api.market_overview import router as market_router
from api.sectors import router as sectors_router
from api.stocks import router as stocks_router
from api.system_config import router as system_router
from api.watchlist import router as watchlist_router
from services.operations.health import build_live_snapshot, read_snapshot
from utils.logger import get_logger
from utils.paths import runtime_path

logger = get_logger("main")


def _log_startup_scheduler_policy() -> None:
    value = str(os.environ.get("AISTOCK_STARTUP_SCHEDULERS_ENABLED", "1")).strip().lower()
    if value in {"0", "false", "no", "off"}:
        logger.warning(
            "AISTOCK_STARTUP_SCHEDULERS_ENABLED is set to a disabled value, "
            "but startup schedulers are forced on by policy."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = __import__("logging").getLogger("aistock.main")
    try:
        from utils.database import db

        db.ensure_ready_for_startup()
        logger.info("Database tables ensured")
    except Exception as exc:
        logger.error(f"Create tables failed: {exc}")
        raise

    logger.info("TdxQuant startup health check skipped; QMT is the active market-data source")

    logger.info("AiStock Backend API started")
    logger.info("API docs: http://localhost:8000/docs")

    if "_log_startup_scheduler_policy" in globals():
        _log_startup_scheduler_policy()

    try:
        from api.system_config import (
            start_core_data_maintenance_scheduler,
            maybe_run_startup_reference_sync,
        )

        core_maintenance_status = start_core_data_maintenance_scheduler()
        if core_maintenance_status is None:
            logger.info("Core data maintenance scheduler remains disabled; Windows Host QMT tasks own market-data production")
        else:
            logger.info("Core data maintenance scheduler started")
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
@app.get("/api/health/ready")
async def health_check():
    runtime_health = read_snapshot(runtime_path("health", "latest.json"))
    return {
        "status": "ok",
        "service": "AiStock Backend",
        "version": "1.0.0",
        "runtime_health": runtime_health,
    }


@app.get("/api/health/runtime")
async def runtime_health_check():
    """Read-only current data/strategy freshness contract for UI and gates."""
    snapshot = build_live_snapshot()
    snapshot["source"] = "live_evaluation"
    return snapshot


app.include_router(data_center_router, prefix="/api")
app.include_router(stocks_router, prefix="/api")
app.include_router(market_board_router, prefix="/api")
app.include_router(market_router, prefix="/api")
app.include_router(sectors_router, prefix="/api")
app.include_router(watchlist_router, prefix="/api")
app.include_router(system_router, prefix="/api", tags=["system"])
app.include_router(accounts_router, prefix="/api")


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
