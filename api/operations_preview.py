"""统一栈安全观察模式入口（Owner 2026-11-09 裁决：观察架构替代 3000 完整栈成为唯一入口）。
R30（2026-11-10 Owner 裁定修复范围升级，任务书 20261110_full_readface_allowlist_r30.md）：
全站只读面开放——挂载全部 12 router（backtest/data_statistics/gen3_shadow/gen3_state_alpha/
kline_check/system/trading/watchlist 补挂，连同既有 operations/market_overview/stocks/sectors/
cls_news 共 13 个 include），对 162 条 GET 处理器逐条副作用审计后放行 114 条只读路径
（含本文件自有的 2 条 health）；39 条"伪读实写/任务触发/调度器变更/外呼"GET 封禁；全部
127 条 POST/PUT/DELETE 一律封禁（开放写动作须 Owner 另批）。R45（20261110_ttrade_watchlist_
r45.md）Owner 批复唯一例外：2 条 scoped-write POST（holding-t/watchlist-upsert、
holding-t/watchlist-sync-ths）只写 holding_tick_watchlist.json（原子+备份），其余写方法仍全封。

绝对禁区（Owner 明令）：任何被放行端点的处理器链不得读取
runtime/gen3_state_alpha/qmtmini_broker_snapshot_sync.log 及其所在子树；审计证据（grep 全
api/ 无该文件名引用；gen3_state_alpha 读文件全部为显式路径字面量，无目录枚举触达该
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
from api.operations import router as operations_router
from api.backtest import router as backtest_router
from api.cls_news import router as cls_news_router
from api.data_statistics import router as data_stats_router
from api.gen3_state_alpha import router as gen3_state_alpha_router
from api.gen3_shadow import router as gen3_shadow_router
from api.kline_check import router as kline_check_router
from api.market_overview import router as market_overview_router
from api.market_board import router as market_board_router
from api.sectors import router as sectors_router
from api.stocks import router as stocks_router
from api.system_config import router as system_router
from api.trading import router as trading_router
from api.watchlist import router as watchlist_router
from services.operations.health import read_snapshot
from utils.paths import runtime_path

app = FastAPI(title='AiStock 统一栈预览（安全观察模式，只读）',
              docs_url=None, redoc_url=None, openapi_url=None, oauth2_redirect_url=None)
app.include_router(operations_router, prefix='/api')
app.include_router(backtest_router, prefix='/api')
app.include_router(cls_news_router, prefix='/api')
app.include_router(data_stats_router, prefix='/api', tags=['data-statistics'])
app.include_router(gen3_state_alpha_router, prefix='/api')
app.include_router(gen3_shadow_router, prefix='/api')
app.include_router(kline_check_router, prefix='/api', tags=['kline-check'])
app.include_router(market_board_router)
app.include_router(market_overview_router, prefix='/api')
app.include_router(sectors_router, prefix='/api')
app.include_router(stocks_router, prefix='/api')
app.include_router(system_router, prefix='/api', tags=['system'])
app.include_router(trading_router, prefix='/api')
app.include_router(watchlist_router, prefix='/api')


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
    '/api/backtest/progress/{task_id}',
    '/api/backtest/result/{task_id}',
    '/api/backtest/strategies',
    '/api/data-statistics/completeness',
    '/api/data-statistics/field-missing',
    '/api/data-statistics/kline-missing',
    '/api/data-statistics/overview',
    '/api/gen3-shadow/guarded',
    '/api/gen3-shadow/live',
    '/api/gen3-shadow/range-filtered',
    '/api/gen3-shadow/route-execution-v3',
    '/api/gen3-shadow/route-execution-v3/backtest',
    '/api/gen3-shadow/state-router-current',
    '/api/gen3-shadow/v4-research/backtest',
    '/api/gen3-shadow/v4-strong-offense',
    '/api/gen3-state-alpha/broker/holdings',
    '/api/gen3-state-alpha/broker/holdings/post-sync-acceptance',
    '/api/gen3-state-alpha/broker/sync/status',
    '/api/gen3-state-alpha/broker/trades',
    '/api/gen3-state-alpha/candidate-omission-reviews',
    '/api/gen3-state-alpha/contract',
    '/api/gen3-state-alpha/current',
    '/api/gen3-state-alpha/daily-review-checklist-reviews',
    '/api/gen3-state-alpha/daily-trend-exit-monitor/status',
    '/api/gen3-state-alpha/evidence-inventory',
    '/api/gen3-state-alpha/formal-action-reviews',
    '/api/gen3-state-alpha/historical-decision-replay-audit',
    '/api/gen3-state-alpha/historical-decision-replay-tasks',
    '/api/gen3-state-alpha/historical-trades',
    '/api/gen3-state-alpha/holding-t/portfolio-state',
    '/api/gen3-state-alpha/holding-t/review',
    '/api/gen3-state-alpha/holding-t/shadow-review',
    '/api/gen3-state-alpha/holding-t/watchlist-sync-ths',
    '/api/gen3-state-alpha/holding-t/watchlist-upsert',
    '/api/gen3-state-alpha/holding-tick-analysis',
    '/api/gen3-state-alpha/holding-tick-analysis/chart/{code}',
    '/api/gen3-state-alpha/holding-tick-watchlist',
    '/api/gen3-state-alpha/launch-day-playbook-reviews',
    '/api/gen3-state-alpha/live-action-console',
    '/api/gen3-state-alpha/live-blocker-evidence-board',
    '/api/gen3-state-alpha/live-launch-decision',
    '/api/gen3-state-alpha/live-launch-packet',
    '/api/gen3-state-alpha/live-launch-readiness-audit',
    '/api/gen3-state-alpha/live-launch-review-snapshot/{snapshot_id}',
    '/api/gen3-state-alpha/live-launch-review-snapshots',
    '/api/gen3-state-alpha/live-learning-ledger',
    '/api/gen3-state-alpha/live-replay-cockpit',
    '/api/gen3-state-alpha/mainwave-opportunities',
    '/api/gen3-state-alpha/no-trade-day-reviews',
    '/api/gen3-state-alpha/observation-scheduler/status',
    '/api/gen3-state-alpha/observation-snapshots',
    '/api/gen3-state-alpha/paper-executions',
    '/api/gen3-state-alpha/paper-watch-reviews',
    '/api/gen3-state-alpha/premarket-control',
    '/api/gen3-state-alpha/pretrade-ticket-reviews',
    '/api/gen3-state-alpha/realtime-readiness-review',
    '/api/gen3-state-alpha/replacement-assessment',
    '/api/gen3-state-alpha/shadow-exit-monitor/status',
    '/api/gen3-state-alpha/shadow-monitor/status',
    '/api/gen3-state-alpha/strategy-tuning-axis-board',
    '/api/gen3-state-alpha/strategy-tuning-completion-audit',
    '/api/gen3-state-alpha/strategy-tuning-current-step-completion-packet',
    '/api/gen3-state-alpha/strategy-tuning-replay-session',
    '/api/gen3-state-alpha/strategy-tuning-replay-suggestions',
    '/api/gen3-state-alpha/strategy-tuning-review-queue',
    '/api/gen3-state-alpha/strategy-tuning-task-reviews',
    '/api/gen3-state-alpha/workflow/refresh-task/{task_id}',
    '/api/gen3-state-alpha/workflow/status',
    '/api/health/check',
    '/api/health/runtime',
    '/api/kline-check/check-single',
    '/api/kline-check/health',
    '/api/kline-check/history',
    '/api/kline-check/history/{check_id}',
    '/api/kline-check/progress/{task_id}',
    '/api/kline-check/repair-progress/{task_id}',
    '/api/kline-check/repair-results/{task_id}',
    '/api/kline-check/results-all/{task_id}',
    '/api/kline-check/results/{task_id}',
    '/api/market/advance-decline-distribution',
    '/api/market/breadth',
    '/api/market/emotion-trend',
    '/api/market/indices',
    '/api/market/indices-legacy',
    '/api/market/limit-up-ladder',
    '/api/market/limit-up-stats',
    '/api/market/margin-sentiment',
    '/api/market/sector-heat',
    '/api/market/sector-heat/{code}/stocks',
    '/api/market/auction-anomaly',
    '/api/market/sentiment-radar',
    '/api/market/sentiment',
    '/api/market/sentiment-legacy',
    '/api/news/cls/event/{event_id}',
    '/api/news/cls/event/{event_id}/candidates',
    '/api/news/cls/latest',
    '/api/news/cls/radar-summary',
    '/api/news/cls/signals',
    '/api/news/cls/status',
    '/api/operations/data-calendar',
    '/api/operations/data-review-actions',
    '/api/operations/data-review-queue',
    '/api/operations/incidents',
    '/api/operations/mainwave-daily',
    '/api/operations/tasks',
    '/api/sectors/',
    '/api/sectors/stats',
    '/api/sectors/stock-count',
    '/api/sectors/sync-kline/status',
    '/api/sectors/sync/status',
    '/api/sectors/update-all-kline-stats/status',
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
    '/api/system/core-data-maintenance/timeline',
    '/api/system/data-source-counts',
    '/api/system/qmt-after-close-execution',
    '/api/system/startup-reference-sync-setting',
    '/api/system/task-status/{task_name}',
    '/api/system/tdx-gateway/diagnostics',
    '/api/system/trade-calendar/status',
    '/api/trading/gen2/backtest',
    '/api/trading/gen2/backtest/update-task/{task_id}',
    '/api/trading/gen2/daily-trade-ticket',
    '/api/trading/gen2/daily-trade-ticket/execution',
    '/api/trading/gen2/factors/registry',
    '/api/trading/gen2/factors/test',
    '/api/trading/gen2/lab',
    '/api/trading/gen2/live',
    '/api/trading/gen2/mainline-hotspots',
    '/api/trading/gen2/mainline-hotspots/update-task/{task_id}',
    '/api/trading/gen2/official-rebuild/update-task/{task_id}',
    '/api/trading/gen2/open-signals',
    '/api/trading/gen2/risk-cool-shadow',
    '/api/trading/gen2/risk-cool-shadow/update-task/{task_id}',
    '/api/trading/gen2/selection-pool',
    '/api/trading/gen2/shadow-monitor/status',
    '/api/trading/gen2/strategy-refresh/status',
    '/api/trading/gen2/timing',
    '/api/trading/gen2/workflow/lineage',
    '/api/trading/gen2/workflow/status',
    '/api/trading/v4/manual-holdings/entry-backtest',
    '/api/trading/v4/manual-holdings/monitor/status',
    '/api/trading/v4/market-gate',
    '/api/watchlist/',
    '/api/watchlist/check/{code}',
    '/api/watchlist/groups',
})

# R45 scoped-write 白名单：唯一放行的写端点（其余 POST/PUT/DELETE 一律封禁）。
# 这两条 POST 只写 holding_tick_watchlist.json（services/holding_t_watchlist 原子
# 写+备份；AST 守卫 tests/test_r45_holding_t_watchlist.py 锁定链上零其他文件写/
# 零 formal 表写/零调度器）。compose 侧仅嵌套 rw 挂载 holding_t_watchlist 子目录，
# gen3_state_alpha 其余文件（broker_state.json 等）保持只读。
_SCOPED_WRITE_PREVIEW_PATHS = frozenset({
    '/api/gen3-state-alpha/holding-t/watchlist-upsert',
    '/api/gen3-state-alpha/holding-t/watchlist-sync-ths',
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
