"""Isolated read-only preview: no application lifespan, collectors or broker imports.

Set AISTOCK_DATA_ROOT to an isolated snapshot directory before starting this app.
"""
from fastapi import FastAPI
from api.operations import router
from services.runtime_health import read_snapshot
from utils.paths import runtime_path

app = FastAPI(title='AiStock 运行可视化预览（只读）')
app.include_router(router,prefix='/api')


@app.get('/api/health/runtime')
def health():
    return {**read_snapshot(runtime_path('health','latest.json')), 'preview':True}
