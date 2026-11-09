"""统一栈安全观察模式入口（Owner 2026-11-09 裁决：观察架构替代 3000 完整栈成为唯一入口，
3001 上缺失的接口从完整后端借鉴补齐）。仅暴露 22 只读路径（GET/HEAD）：
/api/health/runtime、/api/health/check、/api/operations/data-calendar、/api/operations/tasks、
/api/operations/mainwave-daily、/api/operations/incidents、/api/operations/data-review-queue、
/api/market/indices、/api/market/limit-up-ladder、/api/market/sentiment、
/api/market/margin-sentiment、/api/market/emotion-trend、/api/sectors/stats、/api/sectors/{code}、
/api/stocks/、/api/stocks/{code}、/api/stocks/with-limit、/api/news/cls/status、
/api/news/cls/latest、/api/news/cls/signals、/api/news/cls/radar-summary、
/api/news/cls/event/{event_id}。

复用既有 router（api.operations / api.market_overview / api.stocks / api.sectors /
api.cls_news，include_router 后置过滤，原注册参数原样保留，零行为漂移）；
docs/redoc/openapi.json/oauth2-redirect 文档端点全部关闭；无应用 lifespan、collector 或
broker 导入。逐端点审计（任务书 20261109_unified_stack_r13.md）剔除了全部写库/外网/sync
端点；12 条候选中 /api/stocks/search 在 api/stocks.py 无路由定义（搜索由 /api/stocks/ 与
/api/stocks/with-limit 的 search 查询参数承载）、/api/sectors/{date} 实际装饰器为
/{code}、/api/stocks 实际注册形态为 '/api/stocks/'（尾斜杠），故允许集合为 18 而非 19。
R15（Owner 2026-11-09 首页补全裁决，任务书 20261109_unified_stack_homepage_r15.md）：
首页轮询集与情绪页补入 market/sentiment、market/margin-sentiment、market/emotion-trend
与 news/cls/event/{event_id}（事件详情点查，路由模板字面量，与 sectors/{code} 同风格），
允许集合 18→22；四条端点均为只读（逐端点审计见 R15 报告）。

Set AISTOCK_DATA_ROOT to an isolated snapshot directory before starting this app.
"""
from fastapi import FastAPI
from api.operations import router as operations_router
from api.market_overview import router as market_overview_router
from api.stocks import router as stocks_router
from api.sectors import router as sectors_router
from api.cls_news import router as cls_news_router
from services.operations.health import read_snapshot
from utils.paths import runtime_path

app = FastAPI(title='AiStock 统一栈预览（安全观察模式，只读）',
              docs_url=None, redoc_url=None, openapi_url=None, oauth2_redirect_url=None)
app.include_router(operations_router, prefix='/api')
app.include_router(market_overview_router, prefix='/api')
app.include_router(stocks_router, prefix='/api')
app.include_router(sectors_router, prefix='/api')
app.include_router(cls_news_router, prefix='/api')


@app.get('/api/health/runtime')
def health():
    return {**read_snapshot(runtime_path('health','latest.json')), 'preview':True}


@app.get('/api/health/check')
def health_check():
    # 与 api/main.py health_check 响应契约对齐的最小只读等价实现（无 scheduler/DB/lifespan）
    return {
        'status': 'ok',
        'service': 'AiStock Backend',
        'version': '1.0.0',
        'runtime_health': read_snapshot(runtime_path('health', 'latest.json')),
    }


# 安全观察模式路由面收敛：仅保留允许路径的 APIRoute，并把它们的 methods 统一置为 {'GET','HEAD'}
# （Starlette 依 methods 集合分派，HEAD 请求命中同一 handler；原注册参数原样保留，零行为漂移）。
# 允许集合 = R12 既有 7 条 + R13 统一栈扩展 11 条（候选 12 条中 /api/stocks/search 无路由定义）
# + R15 首页补全 4 条（market/sentiment、market/margin-sentiment、market/emotion-trend、
# news/cls/event/{event_id}）。
_ALLOWED_PREVIEW_PATHS = frozenset({
    '/api/health/runtime', '/api/health/check',
    '/api/operations/data-calendar', '/api/operations/tasks',
    '/api/operations/mainwave-daily', '/api/operations/incidents',
    '/api/operations/data-review-queue',
    '/api/market/indices', '/api/market/limit-up-ladder',
    '/api/market/sentiment', '/api/market/margin-sentiment', '/api/market/emotion-trend',
    '/api/sectors/stats', '/api/sectors/{code}',
    '/api/stocks/', '/api/stocks/{code}', '/api/stocks/with-limit',
    '/api/news/cls/status', '/api/news/cls/latest',
    '/api/news/cls/signals', '/api/news/cls/radar-summary',
    '/api/news/cls/event/{event_id}',
})
app.router.routes[:] = [route for route in app.router.routes
                        if getattr(route, 'path', None) in _ALLOWED_PREVIEW_PATHS]
for route in app.router.routes:
    route.methods = {'GET', 'HEAD'}
