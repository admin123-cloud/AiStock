"""统一栈安全观察模式入口（Owner 2026-11-09 裁决：观察架构替代 3000 完整栈成为唯一入口）。
R30（2026-11-10 Owner 裁定修复范围升级，任务书 20261110_full_readface_allowlist_r30.md）：
kline_check/system/trading/watchlist 补挂，连同既有 operations/market_overview/stocks/sectors/
cls_news 共 13 个 include），对 162 条 GET 处理器逐条副作用审计后放行 114 条只读路径
（含本文件自有的 2 条 health）；39 条"伪读实写/任务触发/调度器变更/外呼"GET 封禁；全部
127 条 POST/PUT/DELETE 一律封禁（开放写动作须 Owner 另批）。R45（20261110_ttrade_watchlist_
r45.md）Owner 批复唯一例外：2 条 scoped-write POST（holding-t/watchlist-upsert、
holding-t/watchlist-sync-ths）只写 holding_tick_watchlist.json（原子+备份），其余写方法仍全封。

绝对禁区（Owner 明令）：任何被放行端点的处理器链不得读取
log）见 runtime/full_readface_allowlist_r30/ 审计表。
research/cross_strategy/qmtmini_probe.py::run_probe 全链禁运（/system/tdx-gateway/diagnostics
因 run_probe 外呼语义封禁）。

R30 只读面守卫（AISTOCK_READFACE_PREVIEW=1，本文件在导入 router 前设置；完整栈不设、行为
零变化）：4 处"读路径上的惰性写"在预览栈被跳过，使被守卫的 8 条 GET 在预览栈成为纯读——
  - api/stocks.py _build_intraday_daily_bar：跳过盘中展示缓存表 INSERT（响应不变）
  - api/watchlist.py _ensure_user_stocks_table：跳过 lazy CREATE TABLE DDL
  - api/system_config.py _ensure_system_task_run_log_table：跳过 lazy DDL，按表存在性返回
  - api/trading.py _save_gen2_shadow_monitor_state：跳过 GET 读取路径上的状态文件写

R29（20261110_detail_paths_allowlist_r29.md）：详情页端点族 5 条补入（已并入本版 allowlist）。
R15/R13/R12 历史：仅保留只读路径、复用既有 router、include_router 后置过滤、原注册参数
原样保留零行为漂移；docs/redoc/openapi.json/oauth2-redirect 文档端点全部关闭；无应用
lifespan、collector 或 broker 导入。

安全过滤（本文件末尾）：路径白名单 + 方法白名单双重过滤。关键：仅保留「原本就以 GET
注册」的路由对象（methods 子集于 GET/HEAD），防止同路径 POST/PUT/DELETE 兄弟路由对象被
误改方法而暴露写处理器——R30 审计发现同路径多方法注册真实存在
（/operations/data-review-actions GET+POST、/system/startup-reference-sync-setting GET+POST、
/stocks/select/*/runs/{run_id} GET+DELETE、/gen3-state-alpha/holding-t/* GET+PUT、
/trading/gen2/daily-trade-ticket/execution GET+POST、/watchlist/{code} POST/PUT/DELETE）。

Set AISTOCK_DATA_ROOT to an isolated snapshot directory before starting this app.
"""
import os

# R30 只读面标记：必须在导入任何 router 之前设置；四个守卫点在调用时读取。
os.environ.setdefault("AISTOCK_READFACE_PREVIEW", "1")

from fastapi import FastAPI
from api.accounts import router as accounts_router
from api.g3 import router as g3_router
from api.operations import router as operations_router
from api.data_center import router as data_center_router
from api.market_overview import router as market_overview_router
from api.market_board import router as market_board_router
from api.sectors import router as sectors_router
from api.stocks import router as stocks_router
from api.system_config import router as system_router
from api.watchlist import router as watchlist_router
from services.operations.health import read_snapshot
from utils.paths import runtime_path

app = FastAPI(title='AiStock 统一栈预览（安全观察模式，只读）',
              docs_url=None, redoc_url=None, openapi_url=None, oauth2_redirect_url=None)
app.include_router(operations_router, prefix='/api')
app.include_router(data_center_router, prefix='/api')
app.include_router(market_board_router)
app.include_router(market_overview_router, prefix='/api')
app.include_router(sectors_router, prefix='/api')
app.include_router(stocks_router, prefix='/api')
app.include_router(system_router, prefix='/api', tags=['system'])
app.include_router(watchlist_router, prefix='/api')
app.include_router(accounts_router, prefix='/api')
app.include_router(g3_router, prefix='/api')


@app.get('/api/health/runtime')
def health():
    try:
        from services.ops.data_center_service import get_runtime_health_summary
        return get_runtime_health_summary()
    except Exception:
        return {**read_snapshot(runtime_path('health', 'latest.json')), 'preview': True}


@app.get('/api/health/check')
def health_check():
    # 与 api/main.py health_check 响应契约对齐的最小只读等价实现（无 scheduler/DB/lifespan）
    return {
        'status': 'ok',
        'service': 'AiStock Backend',
        'version': '1.0.0',
        'runtime_health': read_snapshot(runtime_path('health', 'latest.json')),
    }


# R30 全站只读面 allowlist：162 条 GET 逐条副作用审计后的放行集（123 条 router GET，
# 其中 8 条经 AISTOCK_READFACE_PREVIEW 守卫后为纯读）+ 2 条本文件 health 端点。
# R40 增补：/gen3-state-alpha/holding-t/shadow-review（影子做T复盘，runtime/t_tracker/shadow 纯读）。
# 封禁集（39 条 GET + 全部 POST/PUT/DELETE）不在此列；逐条判定与证据行号见
# runtime/full_readface_allowlist_r30/ 审计表。按字母序稳定排列，便于评审与测试断言。
# R42（2026-11-10，任务书 20261110_purify_holding_tick_analysis_r42.md）：审计表 #14
# /api/gen3-state-alpha/holding-tick-analysis 净化（GET 链剥离 SMTP 外呼与修复写，
# AST 守卫测试锁定零 smtp/文件写/子进程）后放行：114 → 115 条。
# R45（2026-11-11，任务书 20261110_ttrade_watchlist_r45.md）：scoped-write 唯一放行
# 写端点 2 条 POST（holding-t/watchlist-upsert、holding-t/watchlist-sync-ths）——
# 只写 holding_tick_watchlist.json（原子+备份，AST 守卫），见下方
# _SCOPED_WRITE_PREVIEW_PATHS；其余一切 POST/PUT/DELETE 仍封禁。
# R55（2026-11-11，任务书 20261111_market_breadth_r55.md）：市场看板 P0 三件套
# 3 条只读 GET（market/breadth、market/advance-decline-distribution、
# market/limit-up-stats）入列：153 → 156 条（GET 只读：kline_daily/快照聚合 +
# runtime/market_mood JSON 读；零写零外呼）。
_ALLOWED_PREVIEW_PATHS = frozenset({
    '/api/accounts/overview',
    '/api/accounts/{account_id}',
    '/api/data-center/health-matrix',
    '/api/data-center/kline-inspect',
    '/api/data-center/overview',
    '/api/g3/ai-screening',
    '/api/g3/desk',
    '/api/g3/desk/notify/config',
    '/api/g3/desk/prefill-status/{draft_id}',
    '/api/g3/mainwave',
    '/api/g3/market-gate',
    '/api/g3/mining/overview',
    '/api/g3/mining/router',
    '/api/g3/mining/runs/{run_id}',
    '/api/g3/premarket-brief',
    '/api/g3/review/attribution',
    '/api/g3/review/coach',
    '/api/g3/review/daily',
    '/api/g3/review/forward-eval',
    '/api/g3/review/shadow',
    '/api/g3/review/signal-quality',
    '/api/g3/settings',
    '/api/g3/strategy-matrix',
    '/api/health/check',
    '/api/health/runtime',
    '/api/market/advance-decline-distribution',
    '/api/market/auction-anomaly',
    '/api/market/breadth',
    '/api/market/emotion-trend',
    '/api/market/indices',
    '/api/market/limit-up-ladder',
    '/api/market/limit-up-stats',
    '/api/market/margin-sentiment',
    '/api/market/publication-status',
    '/api/market/sector-heat',
    '/api/market/sector-heat/{code}/stocks',
    '/api/market/sentiment',
    '/api/market/sentiment-radar',
    '/api/operations/data-calendar',
    '/api/operations/data-review-actions',
    '/api/operations/data-review-queue',
    '/api/operations/incidents',
    '/api/operations/mainwave-daily',
    '/api/operations/tasks',
    '/api/sectors/',
    '/api/sectors/stats',
    '/api/sectors/stock-count',
    '/api/sectors/{code}',
    '/api/sectors/{code}/historical-stats',
    '/api/sectors/{code}/history',
    '/api/sectors/{code}/kline',
    '/api/sectors/{code}/stocks',
    '/api/stocks/',
    '/api/stocks/holdings',
    '/api/stocks/with-limit',
    '/api/stocks/{code}',
    '/api/stocks/{code}/boards',
    '/api/stocks/{code}/industry',
    '/api/stocks/{code}/kline-stats',
    '/api/stocks/{code}/kline/{period}',
    '/api/stocks/{code}/navigation',
    '/api/system/all-tasks-status',
    '/api/system/core-data-maintenance/status',
    '/api/system/startup-reference-sync-setting',
    '/api/system/task-status/{task_name}',
    '/api/system/trade-calendar/status',
    '/api/watchlist/',
    '/api/watchlist/check/{code}',
    '/api/watchlist/groups',
})

# R45 scoped-write 白名单：唯一放行的写端点（其余 POST/PUT/DELETE 一律封禁）。
_SCOPED_WRITE_PREVIEW_PATHS = frozenset({
    '/api/accounts/sync',
    '/api/g3/ai-screening/ingest',
    '/api/g3/desk/execution',
    '/api/g3/desk/execution/delete',
    '/api/g3/desk/generate-plan',
    '/api/g3/desk/notify/config',
    '/api/g3/desk/notify/test',
    '/api/g3/desk/prefill-check',
    '/api/g3/desk/prefill-dispatch',
    '/api/g3/desk/sync-fills',
    '/api/g3/mining/export-contract',
    '/api/g3/mining/router/bind',
    '/api/g3/mining/run',
    '/api/g3/premarket-brief/generate',
    '/api/g3/premarket-brief/push-wechat',
    '/api/g3/review/coach/ingest',
    '/api/g3/watchlist/sync-ths',
    '/api/g3/watchlist/upsert',
})

# 后置过滤：只保留 allowlist 内且「原本就以 GET 注册」的路由对象；随后统一置
# methods 为 GET/HEAD（Starlette 依 methods 集合分派，HEAD 命中同一 handler；
# 原注册参数原样保留，零行为漂移）。R45 例外：_SCOPED_WRITE_PREVIEW_PATHS 内
# 「原本就以 POST 注册」的 scoped-write 路由按原方法保留（绝不参与 GET/HEAD
# 归一；同路径 GET/PUT 兄弟仍被丢弃——PUT 处理器保持封禁）。
app.router.routes[:] = [route for route in app.router.routes
                        if (getattr(route, 'path', None) in _ALLOWED_PREVIEW_PATHS
                            and (getattr(route, 'methods', None) or set()) <= {'GET', 'HEAD'})
                        or (getattr(route, 'path', None) in _SCOPED_WRITE_PREVIEW_PATHS
                            and (getattr(route, 'methods', None) or set()) == {'POST'})]
for route in app.router.routes:
    if getattr(route, 'path', None) not in _SCOPED_WRITE_PREVIEW_PATHS:
        route.methods = {'GET', 'HEAD'}
