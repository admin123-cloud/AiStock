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
from api.operations import router as operations_router
from services.operations.health import read_snapshot
from utils.logger import get_logger
from utils.paths import runtime_path

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from contextlib import suppress
    from services.operations.lifecycle import InstanceLock, startup_specs, publish_heartbeat
    from services.operations.schedulers import registry
    from services.operations.health import write_snapshot
    lock = InstanceLock(runtime_path('operations', 'api-owner.lock'))
    lock.acquire()
    heartbeat = None
    try:
        from utils.database import db
        db.ensure_ready_for_startup()
        enabled = os.environ.get('AISTOCK_STARTUP_SCHEDULERS_ENABLED', '1').lower() not in {'0','false','no','off'}
        registry.initialize(startup_specs(), enabled=enabled)
        app.state.scheduler_registry = registry
        write_snapshot(registry.snapshot(), runtime_path('operations', 'api_tasks.json'))
        heartbeat = asyncio.create_task(publish_heartbeat(runtime_path('operations', 'api_tasks.json')))
        yield
    finally:
        if heartbeat:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        try:
            await asyncio.to_thread(registry.shutdown)
            write_snapshot(registry.snapshot(), runtime_path('operations', 'api_tasks.json'))
        finally:
            lock.release()


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


@app.get('/api/health/live')
async def liveness():
    return {'status': 'alive', 'version': os.environ.get('AISTOCK_RELEASE_VERSION', 'development')}


@app.get('/api/health/check')
@app.get('/api/health/ready')
async def health_check():
    from fastapi.responses import JSONResponse
    from services.operations.schedulers import registry
    state = registry.snapshot()
    return JSONResponse(status_code=200 if state['ready'] else 503, content={
        'status': state['status'], 'service': 'AiStock Backend',
        'version': os.environ.get('AISTOCK_RELEASE_VERSION', 'development'),
        'schedulers': state, 'runtime_health': read_snapshot(runtime_path('health', 'latest.json')),
    })


@app.get("/api/health/runtime")
async def runtime_health_check():
    """Read-only data/strategy freshness contract for UI and strategy gates."""
    return read_snapshot(runtime_path("health", "latest.json"))


app.include_router(operations_router, prefix="/api")
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
