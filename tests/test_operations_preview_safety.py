"""统一栈安全观察模式：HTTP 路由面 allowlist 的契约测试（R13 修订版，仅准备，不运行）。

Owner 2026-11-09 统一栈裁决：安全观察架构（preview app）替代 3000 完整栈成为唯一
入口，3001 上缺失的接口从完整后端借鉴只读面（任务书
20261109_unified_stack_r13.md）。据此 allowlist 由 R11 的 7 路径扩展为统一栈
只读面；候选 19 条经逐端点审计与实际路由定义核对后收敛为 18 条路径——
  - GET、HEAD /api/health/runtime
  - GET、HEAD /api/health/check
  - GET、HEAD /api/operations/data-calendar
  - GET、HEAD /api/operations/tasks
  - GET、HEAD /api/operations/mainwave-daily
  - GET、HEAD /api/operations/incidents
  - GET、HEAD /api/operations/data-review-queue
  - GET、HEAD /api/market/indices
  - GET、HEAD /api/market/limit-up-ladder
  - GET、HEAD /api/sectors/stats
  - GET、HEAD /api/sectors/{code}
  - GET、HEAD /api/stocks/
  - GET、HEAD /api/stocks/{code}
  - GET、HEAD /api/stocks/with-limit
  - GET、HEAD /api/news/cls/status
  - GET、HEAD /api/news/cls/latest
  - GET、HEAD /api/news/cls/signals
  - GET、HEAD /api/news/cls/radar-summary
R15（Owner 2026-11-09 首页补全裁决，任务书 20261109_unified_stack_homepage_r15.md）：
首页轮询集（market/sentiment、market/margin-sentiment）、情绪页（market/emotion-trend）
与新闻事件详情点查（news/cls/event/{event_id}，路由模板字面量，与 sectors/{code} 同
风格）补入 4 条只读路径，allowlist 18→22——
  - GET、HEAD /api/market/sentiment
  - GET、HEAD /api/market/margin-sentiment
  - GET、HEAD /api/market/emotion-trend
  - GET、HEAD /api/news/cls/event/{event_id}
HEAD 仅限上述 22 个 GET 路径；OPTIONS、其他方法或路径都不在 allowlist，不得从
路由矩阵中滤掉。docs、redoc、OpenAPI 端点必须全部关闭；任何写库/外网/sync 端点
（如 cls_news 的 POST /sync、stocks 的 update/sync/kline 写路径、market 的
calculate-emotion-cycle、sectors 的 sync/update 类）都必须被后置过滤剔除。
与任务书 12 条候选的形态差异：/api/stocks/search 在 api/stocks.py 无路由定义
（搜索由 /api/stocks/ 与 /api/stocks/with-limit 的 search 查询参数承载）；
/api/sectors/{date} 实际装饰器为 @router.get("/{code}")；/api/stocks 实际注册
形态为 '/api/stocks/'（APIRouter(prefix="/stocks") 与 @router.get("/") 拼接产生
尾斜杠），前端经 Starlette redirect_slashes 307 可达 '/api/stocks'。

已核实的导入期安全根因（R11 任务书，R13 沿用）：设置 AISTOCK_* 临时目录环境变量
不足以隔离 import——`api.operations_preview` 的导入链一旦进入 `utils.*`，会先执行
真实 `utils/__init__.py`；该初始化导入配置与数据库模块，真实 `utils.database` 在
模块级执行 `db = DatabaseManager()` 并构造 ClickHouse engine。因此本测试的隔离边
界不是环境变量，而是：

1. pytest collection/module-level 只导入标准库与测试框架；不在模块级 import
   api/services/strategies/utils，也不在模块级修改 sys.path / sys.modules。
2. 每个测试函数内、在任何 mutation/import 之前，先对项目命名空间
   api、services、strategies、utils 及所有后代做 preflight：任何相关项已在
   sys.modules 中即 fail-closed 停止。
3. 随后安装测试自有的 fake `utils` package（`__path__` 为空，禁止回落到真实
   包文件）以及经源码调用链核对所需的最小 fake 子模块。R11 集合为
   `utils.paths`、`utils.trading_sessions`；R13 统一栈新增挂载
   （api.market_overview / api.stocks / api.sectors / api.cls_news 及其模块级
   依赖链 models.stock_models、services.{market_turnover_forecast,
   market_sentiment_cache,margin_sentiment,cls_news_service}）经逐一读取导入
   语句核对后另需 `utils.database`（db/Base）、`utils.logger`、
   `utils.market_warehouse`（7 个名字）、`utils.daily_quality_read`，以及
   `utils.paths` 补 cache_path/report_path（cls_news_service 模块级调用，仅构造
   tmp 隔离根下 Path）。任何未预期的 `utils.*` 导入（含真实 `utils/__init__.py`
   的连带初始化）都不会发生：fake 包已整体替换命名空间，空 `__path__` 使新的
   子模块请求以 ModuleNotFoundError 失败关闭。api、services、strategies 按任务
   书从 worktree 真实加载；models/、data_fetcher/（不在受控命名空间）经核对仅
   依赖 stdlib/第三方库与上述 fake utils 名字（data_fetcher 两个 __init__.py 均
   为惰性导入；cls_telegraph 仅 stdlib+requests）。
4. 导入、路由表读取、teardown 全部由 _SysModulesGuard 按test-driven-development
   skill 的 sys.modules stub/import guard 纪律管理：构造即做基线快照；只回滚
   仍是测试自有对象的条目（guard 私有 exact-object registry，`is` 恒等比较）；外来替换（含置
   None）保持原样并聚合为清理冲突；原始导入异常保留为 __cause__；key 缺失与
   值为 None 严格区分。清理逻辑另用局部合成 mapping/namespace 的单元测试覆盖
   absent key、present-None、foreign None 属性与冲突清理路径，不在 collection
   时导入任何应用模块。
5. teardown 清除本测试新导入的全部项目命名空间条目及其父包属性引用；清理
   无法安全完成时以 _CleanupConflict fail-closed，绝不静默留下污染。

安全边界（只观察路由元数据，不触发任何运行时逻辑）：
- 只读取 FastAPI 路由表元数据（app.routes 的 path/methods 与 docs_url 等
  构造参数）：不调用 indices、data-calendar、health 或其他任何 handler；不使用
  TestClient；不触发 ASGI lifespan；不构造数据库/HTTP 客户端；不写 SQLite；
  不发起网络请求。测试自身不做源码扫描，也不直接读取配置或运行时数据；
  但 Python import 会加载明确受审的 api/services/strategies Python 源码，
  真实 utils.* 包仍由 fake 空 __path__ 隔离，不扩大允许导入范围。
- AISTOCK_* 四个隔离根环境变量仍通过 monkeypatch 设置，但这只是纵深防御，
  不是隔离边界；fake utils.paths 即使被意外调用也只会返回 tmp_path 下的路径。
"""

import importlib
import sys
import types
from datetime import timedelta, timezone
from pathlib import Path

import pytest

# 受控项目命名空间：preflight、guard 与 teardown 都以这四个顶层包为界。
_PROJECT_NAMESPACES = ('api', 'services', 'strategies', 'utils')

# PM 路由 allowlist：显式硬编码，禁止由仓库枚举推导。
# Owner 2026-11-09 统一栈裁决（任务书 20261109_unified_stack_r13.md）：安全观察
# 架构替代 3000 完整栈，从完整后端借鉴只读 GET 面，allowlist 由 R11 的 7 路径
# 扩展为 18 路径；仅覆盖该只读面，写库/外网/sync 端点（cls_news 的 sync、
# stocks 的 update/sync/kline 写路径、market 的 calculate-emotion-cycle、
# sectors 的 sync/update 类）一律禁止。
# Owner 2026-11-09 首页补全裁决（任务书 20261109_unified_stack_homepage_r15.md）：
# 补入 market/sentiment、market/margin-sentiment、market/emotion-trend（情绪页）
# 与 news/cls/event/{event_id}（事件详情点查）4 条只读路径，allowlist 18→22。
# 完整 (METHOD, path) 矩阵：22 条路径各自的 GET 与 HEAD 共 44 对，一个不多、
# 一个不少；任何 OPTIONS、其他方法、未识别路由类型或额外路径都是违规。路径模板
# 取自各 router 文件的实际装饰器（含 /api/stocks/ 尾斜杠与 /api/sectors/{code}、
# /api/news/cls/event/{event_id} 参数名）。
ALLOWED_BUSINESS_ROUTE_PAIRS = frozenset({
    ('GET', '/api/health/runtime'),
    ('HEAD', '/api/health/runtime'),
    ('GET', '/api/health/check'),
    ('HEAD', '/api/health/check'),
    ('GET', '/api/operations/data-calendar'),
    ('HEAD', '/api/operations/data-calendar'),
    ('GET', '/api/operations/tasks'),
    ('HEAD', '/api/operations/tasks'),
    ('GET', '/api/operations/mainwave-daily'),
    ('HEAD', '/api/operations/mainwave-daily'),
    ('GET', '/api/operations/incidents'),
    ('HEAD', '/api/operations/incidents'),
    ('GET', '/api/operations/data-review-queue'),
    ('HEAD', '/api/operations/data-review-queue'),
    ('GET', '/api/market/indices'),
    ('HEAD', '/api/market/indices'),
    ('GET', '/api/market/limit-up-ladder'),
    ('HEAD', '/api/market/limit-up-ladder'),
    ('GET', '/api/market/sentiment'),
    ('HEAD', '/api/market/sentiment'),
    ('GET', '/api/market/margin-sentiment'),
    ('HEAD', '/api/market/margin-sentiment'),
    ('GET', '/api/market/emotion-trend'),
    ('HEAD', '/api/market/emotion-trend'),
    ('GET', '/api/sectors/stats'),
    ('HEAD', '/api/sectors/stats'),
    ('GET', '/api/sectors/{code}'),
    ('HEAD', '/api/sectors/{code}'),
    ('GET', '/api/stocks/'),
    ('HEAD', '/api/stocks/'),
    ('GET', '/api/stocks/{code}'),
    ('HEAD', '/api/stocks/{code}'),
    ('GET', '/api/stocks/with-limit'),
    ('HEAD', '/api/stocks/with-limit'),
    ('GET', '/api/news/cls/status'),
    ('HEAD', '/api/news/cls/status'),
    ('GET', '/api/news/cls/latest'),
    ('HEAD', '/api/news/cls/latest'),
    ('GET', '/api/news/cls/signals'),
    ('HEAD', '/api/news/cls/signals'),
    ('GET', '/api/news/cls/radar-summary'),
    ('HEAD', '/api/news/cls/radar-summary'),
    ('GET', '/api/news/cls/event/{event_id}'),
    ('HEAD', '/api/news/cls/event/{event_id}'),
})

# FastAPI 自动文档端点必须关闭（否定断言对象）。
FORBIDDEN_DOCUMENTATION_PATHS = ('/docs', '/redoc', '/openapi.json')

# 隔离根环境变量 -> tmp_path 下的独立子目录名（四个互不相同）。
# 纵深防御，不是隔离边界；见模块 docstring。
_ISOLATION_ROOT_ENV = {
    'AISTOCK_DATA_ROOT': 'isolated-data-root',
    'AISTOCK_REPORTS_ROOT': 'isolated-reports-root',
    'AISTOCK_ARTIFACTS_ROOT': 'isolated-artifacts-root',
    'AISTOCK_LOGS_ROOT': 'isolated-logs-root',
}

# 唯一“条目/属性缺失”标记：与“存在但值为 None”严格区分。
_MISSING = object()
def _make_fake_module(name):
    # 普通、无所有权属性的合成模块：仅用于不经 guard.install 的场景
    # （外来替换对象、模拟导入系统新增的子模块条目等）。需要安装进
    # sys.modules 的测试自有模块必须由 _SysModulesGuard.new_module 创建，
    # 其所有权由 guard 私有 exact-object registry（id -> 对象强引用）证明，
    # 不占用任何模块属性。
    return types.ModuleType(name)


class _CleanupConflict(RuntimeError):
    """聚合的 teardown 冲突；原始导入异常保留为 __cause__。"""


class _SysModulesGuard:
    """sys.modules 条目与父包属性的快照/回滚机制（mapping 可注入合成对象）。

    纪律（test-driven-development skill，sys.modules stub/import guard）：
    - 构造即做基线快照：任何 mutation 之前记录受控命名空间内既有条目，
      key 缺失用 _MISSING 表示，与“存在但值为 None”严格区分；
    - install 只接受 guard 私有 exact-object registry（new_module 工厂
      id->对象强引用登记）中的精确对象，且要求目标 key 缺失，已被占用即
      fail-closed 拒绝覆盖；
    - record_import_surface 在导入后记录新增条目，以及仅测试自有父包上的
      子模块属性设置；外来父包上的属性不做清理，直接记为延迟冲突；
    - cleanup 只在当前对象仍是测试自有的那个对象（恒等比较）时回滚/清除；
      外来替换（含置 None）保持原样并聚合为 _CleanupConflict，其余自有项
      继续 best-effort 清理；
    - 清理冲突聚合成单次抛出；若受保护操作本身抛出过异常，将其保留为
      __cause__，不被清理异常掩盖。
    """

    def __init__(self, mapping, namespaces):
        self._mapping = mapping
        self._namespaces = tuple(namespaces)
        # guard 私有 exact-object registry：id(module) -> module 强引用。
        # 所有权证明只存在于 guard 内部，不经由任何模块属性。
        self._owned = {}
        self._baseline = {
            name: mapping[name] if name in mapping else _MISSING
            for name in list(mapping)
            if self._matches(name)
        }
        self._installed = {}          # name -> guard 安装的精确对象
        self._import_surface = {}     # name -> 导入后观测到的精确对象
        self._attr_plan = []          # (父包对象, 属性名, 期望的子模块对象)
        self._deferred_conflicts = []

    def _matches(self, name):
        return any(
            name == ns or name.startswith(ns + '.') for ns in self._namespaces
        )

    def new_module(self, name):
        """受控工厂：创建测试自有合成模块并登记进私有 exact-object registry。

        install 只接受经由本工厂创建并登记的精确对象；所有权不写入模块属性。
        """
        module = types.ModuleType(name)
        self._owned[id(module)] = module
        return module

    def _is_registered(self, module):
        # 判定同时验证 registry 中该 id 命中的对象 `is` 当前对象。
        return self._owned.get(id(module)) is module

    def _is_guard_scope(self, name):
        return name in self._installed or name in self._import_surface

    def install(self, name, module):
        if not self._matches(name):
            raise ValueError(f'拒绝安装受控命名空间之外的条目：{name!r}')
        if not self._is_registered(module):
            raise ValueError(f'拒绝安装未经 guard 私有登记的精确对象：{name!r}')
        current = self._mapping[name] if name in self._mapping else _MISSING
        if current is not _MISSING and current is not module:
            raise RuntimeError(f'fail-closed：{name!r} 已被占用，拒绝覆盖')
        self._mapping[name] = module
        self._installed[name] = module

    def record_import_surface(self):
        # 两遍式、顺序无关：第一遍仅记录，不做父包归属判定；待本次导入面
        # 全部落入 _import_surface 后，第二遍再对本次新记录的名字建立父包
        # 属性清理计划或延迟冲突，父包判定不依赖 sys.modules 键的迭代先后。
        new_names = []
        for name in list(self._mapping):
            if not self._matches(name) or name in self._baseline:
                continue
            if name in self._import_surface:
                continue
            module = self._mapping[name]
            self._import_surface[name] = module
            new_names.append(name)
        for name in new_names:
            parent_name, dot, attr_name = name.rpartition('.')
            if not dot:
                continue
            module = self._import_surface[name]
            parent = self._mapping.get(parent_name, _MISSING)
            if parent is _MISSING or parent is None:
                # 父包缺失或为 None：没有持有属性的宿主对象；条目本身仍会被清理。
                continue
            if self._is_guard_scope(parent_name):
                self._attr_plan.append((parent, attr_name, module))
            else:
                self._deferred_conflicts.append(
                    f'父包 {parent_name!r} 非测试自有，其属性 {attr_name!r} 不做清理'
                )

    def cleanup(self, original_exc=None):
        conflicts = list(self._deferred_conflicts)
        touched = list(self._import_surface)
        touched.extend(
            name for name in self._installed if name not in self._import_surface
        )
        for name in touched:
            if name in self._baseline:
                continue
            expected = self._import_surface.get(name, self._installed.get(name))
            if name in self._mapping:
                current = self._mapping[name]
                if current is expected:
                    del self._mapping[name]
                elif current is None:
                    conflicts.append(
                        f'sys.modules[{name!r}] 被外来方置为 None，保持原样'
                    )
                else:
                    conflicts.append(
                        f'sys.modules[{name!r}] 被外来对象替换，保持原样'
                    )
            # 当前缺失且基线缺失：期望终态已达成，不是冲突。
        for name, baseline_value in self._baseline.items():
            if name in self._mapping:
                if self._mapping[name] is not baseline_value:
                    conflicts.append(
                        f'sys.modules[{name!r}] 基线条目被外来方替换，保持原样'
                    )
            elif baseline_value is not _MISSING:
                conflicts.append(
                    f'sys.modules[{name!r}] 基线条目被外来方移除，不复活'
                )
        for parent, attr_name, expected in self._attr_plan:
            current = getattr(parent, attr_name, _MISSING)
            if current is _MISSING:
                continue
            if current is expected:
                try:
                    delattr(parent, attr_name)
                except AttributeError as exc:
                    conflicts.append(f'父包属性 {attr_name!r} 删除失败：{exc}')
            elif current is None:
                conflicts.append(
                    f'父包属性 {attr_name!r} 被外来方置为 None，保持原样'
                )
            else:
                conflicts.append(
                    f'父包属性 {attr_name!r} 被外来对象替换，保持原样'
                )
        if conflicts:
            failure = _CleanupConflict(
                'sys.modules 清理存在未消除的污染（fail-closed）：'
                + '；'.join(conflicts)
            )
            if original_exc is not None:
                raise failure from original_exc
            raise failure


def _assert_project_namespaces_absent():
    """preflight：任何受控命名空间模块已在 sys.modules 中即 fail-closed。

    必须发生在一切 mutation（含环境变量设置）与 import 之前。
    """
    present = sorted(
        name
        for name in sys.modules
        if any(
            name == ns or name.startswith(ns + '.')
            for ns in _PROJECT_NAMESPACES
        )
    )
    assert not present, (
        '隔离预检失败（fail-closed）：以下项目命名空间模块已在 sys.modules '
        f'中，本测试拒绝在此状态下导入 app：{present}'
    )


def _install_fake_utils(guard, isolated_root):
    """安装测试自有 fake utils 包与最小子模块；__path__ 为空，绝不回落真实包。

    R11（观察树）核对的最小集：`api.operations_preview`（当时仅挂
    api.operations）的模块级导入链只需要：
    - utils.paths.runtime_path（api/operations_preview.py、api/operations.py）
    - utils.trading_sessions.BUSINESS_TZ / completed_bar_times
      （services/operations/{health,schedulers,delivery}.py；BUSINESS_TZ 在
      schedulers.registry 构造时即被使用，故为纯内存固定偏移时区）

    R13 统一栈新增挂载 api.market_overview / api.stocks / api.sectors /
    api.cls_news（及其模块级依赖 models.stock_models、
    services.{market_turnover_forecast,market_sentiment_cache,margin_sentiment,
    cls_news_service}），经逐一读取上述文件的模块级导入语句核对，另需：
    - utils.paths.cache_path / report_path（services/cls_news_service.py L17
      导入、L25-29 模块级即调用；只构造 tmp 隔离根下 Path，不触磁盘）
    - utils.database.Base（models/stock_models.py L12——全 models/ 目录唯一的
      utils 导入；ORM 声明基类，用真实 sqlalchemy declarative_base() 纯内存
      构造，不建 engine、不连库）
    - utils.database.db（api/{market_overview,stocks,sectors}.py 模块级导入；
      仅 handler 运行期才调用 get_session，这里给 fail-closed 哨兵）
    - utils.logger.get_logger（api/{market_overview,stocks,sectors}.py 与
      services/{market_sentiment_cache,margin_sentiment,cls_news_service}.py
      模块级即调用；返回 stdlib logging.getLogger 的 Logger，纯内存）
    - utils.market_warehouse 的 clickhouse_available / clickhouse_table_exists
      / clickhouse_query_df / clickhouse_scalar / clean_minute_bars_df /
      clickhouse_client / quality_clickhouse_client（api/{market_overview,
      stocks,sectors}.py 与 services/{market_turnover_forecast,
      market_sentiment_cache,margin_sentiment,cls_news_service}.py 模块级
      导入名的并集；全部只在 handler 运行期调用，fail-closed 哨兵）
    - utils.daily_quality_read.finite_number（api/market_overview.py L25、
      services/cls_news_service.py L2 模块级导入名；handler 运行期才调用）

    其余任何 utils.* 导入（含真实 utils/__init__.py 及其 config/database
    连带初始化）都被空 __path__ 以 ModuleNotFoundError 失败关闭，不会转而
    加载真实文件。不经 utils 的模块级依赖也经核对：data_fetcher/__init__.py
    与 data_fetcher/sources/__init__.py 无模块级导入（均为函数内惰性导入），
    data_fetcher/sources/cls_telegraph.py 仅 stdlib+requests。
    """
    utils_pkg = guard.new_module('utils')
    utils_pkg.__path__ = []  # 关键：空包路径 -> 未预期 utils.* 导入即失败关闭

    utils_paths = guard.new_module('utils.paths')

    def _runtime_path(*parts):
        # 仅当某个 route handler 被调用时才可能执行；本测试从不调用 handler。
        # 即便意外执行，也只会指向 tmp 隔离根，绝不触达真实数据根。
        return Path(isolated_root, 'runtime', *parts)

    def _cache_path(*parts):
        # services/cls_news_service 模块级构造 STATE_PATH 时调用；纯 Path 构造。
        return Path(isolated_root, 'cache', *parts)

    def _report_path(*parts):
        # services/cls_news_service 模块级构造四个报告工件路径时调用；纯 Path 构造。
        return Path(isolated_root, 'reports', *parts)

    utils_paths.runtime_path = _runtime_path
    utils_paths.cache_path = _cache_path
    utils_paths.report_path = _report_path

    utils_sessions = guard.new_module('utils.trading_sessions')
    # 固定偏移时区：纯内存构造，不读 tzdata/系统时区库。
    utils_sessions.BUSINESS_TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
    utils_sessions.SESSION_MINUTES = ((570, 690), (780, 900))

    def _completed_bar_times(period):
        raise AssertionError('安全观察模式路由测试不得调用交易时段计算')

    utils_sessions.completed_bar_times = _completed_bar_times

    def _forbidden_util_call(*args, **kwargs):
        raise AssertionError('安全观察模式路由测试不得触发真实 utils 数据访问')

    utils_database = guard.new_module('utils.database')

    class _FailClosedDB:
        @staticmethod
        def get_session():
            raise AssertionError('安全观察模式路由测试不得获取数据库会话')

    utils_database.db = _FailClosedDB()
    # models/stock_models.py 需要 ORM 声明基类：用真实 sqlalchemy
    # declarative_base() 纯内存构造（不建 engine、不连库），保证 Column/
    # relationship 等声明机制在模型类定义期可用；函数内惰性导入，不在
    # collection 期引入第三方依赖。
    from sqlalchemy.ext.declarative import declarative_base as _declarative_base

    utils_database.Base = _declarative_base()

    utils_logger = guard.new_module('utils.logger')
    import logging as _logging

    utils_logger.get_logger = _logging.getLogger

    utils_warehouse = guard.new_module('utils.market_warehouse')
    for _name in (
        'clickhouse_available',
        'clickhouse_table_exists',
        'clickhouse_query_df',
        'clickhouse_scalar',
        'clean_minute_bars_df',
        'clickhouse_client',
        'quality_clickhouse_client',
    ):
        setattr(utils_warehouse, _name, _forbidden_util_call)

    utils_quality = guard.new_module('utils.daily_quality_read')
    utils_quality.finite_number = _forbidden_util_call

    utils_pkg.paths = utils_paths
    utils_pkg.trading_sessions = utils_sessions
    utils_pkg.database = utils_database
    utils_pkg.logger = utils_logger
    utils_pkg.market_warehouse = utils_warehouse
    utils_pkg.daily_quality_read = utils_quality
    guard.install('utils', utils_pkg)
    guard.install('utils.paths', utils_paths)
    guard.install('utils.trading_sessions', utils_sessions)
    guard.install('utils.database', utils_database)
    guard.install('utils.logger', utils_logger)
    guard.install('utils.market_warehouse', utils_warehouse)
    guard.install('utils.daily_quality_read', utils_quality)


def _extract_route_pairs(app):
    """折叠完整 (METHOD, path) 集合；不滤掉任何方法，未识别路由 fail-closed。

    - 不跳过 OPTIONS / HEAD / POST 等任何方法：allowlist 之外的任何方法都算
      多余路由。
    - 带 path 但无 methods 的路由（Mount/WebSocketRoute 等）记为
      ('NON_REQUEST_ROUTING', path)：挂载面不允许静默通过。
    - 无法读取有效 path 的路由记为 ('UNINSPECTABLE_ROUTE', 类型名)：宁可让
      断言失败，也不放过不可审查的路由。
    """
    pairs = set()
    for route in app.routes:
        path = getattr(route, 'path', None)
        methods = getattr(route, 'methods', None)
        if not isinstance(path, str) or not path:
            pairs.add(('UNINSPECTABLE_ROUTE', type(route).__name__))
            continue
        if not methods:
            pairs.add(('NON_REQUEST_ROUTING', path))
            continue
        for method in methods:
            pairs.add((str(method).upper(), path))
    return pairs


def _chain_reaches(root, target):
    # 小型纯内存可达性遍历：判断 target 是否可从 root 沿异常链
    # （__cause__/__context__）到达，语义与 R6 契约测试中的 _chain_contains
    # 一致；仅用于 _guarded_teardown 避免把同一异常重复链入异常链。
    pending = [root]
    seen = set()
    while pending:
        node = pending.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if node is target:
            return True
        pending.append(node.__cause__)
        pending.append(node.__context__)
    return False


def _guarded_teardown(guard, original_exc=None):
    """无条件 teardown：record_import_surface 失败不得跳过 cleanup。

    - 先执行 guard.record_import_surface()；其抛出的任何异常
      （BaseException）捕获为 record_exc，绝不让 cleanup 被跳过；
    - record_exc 与 original_exc 同时存在时，在调用 cleanup 之前把
      record_exc 链入 original_exc 的异常链（若链上尚不可达，则设
      original_exc.__context__ = record_exc；已可达则不重复设置），
      既不丢弃 record 阶段失败原因，也不掩盖被保护操作的原始异常；
    - 无条件执行 guard.cleanup(original_exc=primary)，其中
      primary = original_exc if original_exc is not None else record_exc
      （二者皆无则为 None）；cleanup 抛出的 _CleanupConflict 按既有
      guard 语义继续向外抛（guard 已保证从 primary 链起）；
    - cleanup 正常返回后：有 original_exc 则重抛 original_exc 本身，
      否则有 record_exc 则抛 record_exc 本身（同一异常对象，不包装
      替换），都无则正常返回。
    """
    record_exc = None
    try:
        guard.record_import_surface()
    except BaseException as exc:  # record 失败不得跳过 cleanup
        record_exc = exc
    primary = original_exc if original_exc is not None else record_exc
    if record_exc is not None and original_exc is not None:
        if not _chain_reaches(original_exc, record_exc):
            original_exc.__context__ = record_exc
    guard.cleanup(original_exc=primary)
    if original_exc is not None:
        raise original_exc
    if record_exc is not None:
        raise record_exc


def _inspect_preview_route_surface(tmp_path, monkeypatch):
    """在 fake utils 隔离下导入真实 preview app，只读取路由表元数据。

    只做 import 与属性读取；返回纯数据（frozenset/tuple），不向测试函数泄漏
    任何模块对象；teardown 由 guard 在本函数内完成。
    """
    _assert_project_namespaces_absent()
    for env_name, subdir in _ISOLATION_ROOT_ENV.items():
        monkeypatch.setenv(env_name, str(tmp_path / subdir))
    guard = _SysModulesGuard(sys.modules, _PROJECT_NAMESPACES)
    body_error = None
    try:
        _install_fake_utils(guard, tmp_path)
        preview = importlib.import_module('api.operations_preview')
        app = preview.app
        surface = {
            'pairs': frozenset(_extract_route_pairs(app)),
            'paths': frozenset(
                path
                for path in (getattr(route, 'path', None) for route in app.routes)
                if isinstance(path, str)
            ),
            'docs_urls': (app.docs_url, app.redoc_url, app.openapi_url),
        }
    except BaseException as exc:  # 清理必须同样覆盖导入失败路径
        body_error = exc
        raise
    finally:
        _guarded_teardown(guard, original_exc=body_error)
    return surface


def test_business_http_routes_exactly_match_allowlist(tmp_path, monkeypatch):
    """完整 (METHOD, path) 矩阵必须恰好等于 PM allowlist，不得多也不得少。"""
    surface = _inspect_preview_route_surface(tmp_path, monkeypatch)
    actual = surface['pairs']
    extra = sorted(actual - ALLOWED_BUSINESS_ROUTE_PAIRS)
    missing = sorted(ALLOWED_BUSINESS_ROUTE_PAIRS - actual)
    assert not extra and not missing, (
        '统一栈安全观察模式的业务 HTTP 路由面必须恰好等于 PM allowlist：'
        f'{sorted(ALLOWED_BUSINESS_ROUTE_PAIRS)}。\n'
        f'多余 (method, path)（不得暴露）: {extra}\n'
        f'缺失 (method, path): {missing}\n'
        '注：OPTIONS 与任何未识别路由类型都不得从矩阵中滤掉；当前 '
        'api.operations_preview 挂载 operations/market_overview/stocks/'
        'sectors/cls_news 五个 router 并后置过滤为 22 路径 × {GET, HEAD}'
        '（R13 统一栈 + R15 首页补全，Owner 2026-11-09 裁决：观察架构替代 '
        '3000，从完整后端借鉴只读面）。'
    )


def test_documentation_paths_absent_from_route_table(tmp_path, monkeypatch):
    """/docs、/redoc、/openapi.json 不得出现在实际路由表。"""
    surface = _inspect_preview_route_surface(tmp_path, monkeypatch)
    still_exposed = [
        p for p in FORBIDDEN_DOCUMENTATION_PATHS if p in surface['paths']
    ]
    assert not still_exposed, (
        f'FastAPI 自动文档端点必须关闭，但路由表中仍存在: {still_exposed}'
    )


def test_documentation_constructor_urls_are_none(tmp_path, monkeypatch):
    """docs_url、redoc_url、openapi_url 三个构造参数必须全部为 None。"""
    surface = _inspect_preview_route_surface(tmp_path, monkeypatch)
    docs_urls = surface['docs_urls']
    assert docs_urls == (None, None, None), (
        '应用必须以 docs_url=None、redoc_url=None、openapi_url=None 构造；'
        f'当前 docs_url={docs_urls[0]!r}, redoc_url={docs_urls[1]!r}, '
        f'openapi_url={docs_urls[2]!r}'
    )


# ---------------------------------------------------------------------------
# _SysModulesGuard 清理纪律的单元测试：全部使用局部合成 mapping/namespace，
# 不触碰真实 sys.modules，也不在 collection/运行期导入任何应用模块。
# ---------------------------------------------------------------------------


def test_guard_cleanup_treats_foreign_removed_entry_as_clean():
    """absent key：外来方移除我们安装的条目 = 期望终态，不误报冲突。"""
    mapping = {}
    guard = _SysModulesGuard(mapping, ('pkg',))
    guard.install('pkg', guard.new_module('pkg'))
    del mapping['pkg']  # 外来方移除
    guard.record_import_surface()
    guard.cleanup()  # 不得抛错
    assert 'pkg' not in mapping


def test_guard_cleanup_distinguishes_present_none_from_absent_key():
    """present-None：基线“存在但值为 None”不得被当作缺失而删除。"""
    mapping = {'pkg': None}  # 存在但值为 None（导入机制的 None 标记）
    guard = _SysModulesGuard(mapping, ('pkg',))
    guard.record_import_surface()
    guard.cleanup()  # 无变化 -> 无冲突
    assert 'pkg' in mapping and mapping['pkg'] is None


def test_guard_cleanup_leaves_foreign_none_attribute_and_fails_closed():
    """foreign None 属性：父包属性被外来方置 None 时保持原样并报冲突。"""
    mapping = {}
    guard = _SysModulesGuard(mapping, ('pkg',))
    parent = guard.new_module('pkg')
    guard.install('pkg', parent)
    child = _make_fake_module('pkg.child')
    mapping['pkg.child'] = child  # 模拟导入系统新增子模块条目
    parent.child = child          # 模拟导入系统设置父包属性
    guard.record_import_surface()
    parent.child = None           # 外来方把父包属性替换为 None
    with pytest.raises(_CleanupConflict) as excinfo:
        guard.cleanup()
    assert "'child'" in str(excinfo.value)
    assert 'pkg' not in mapping           # 自有条目照常清理
    assert 'pkg.child' not in mapping
    assert parent.child is None           # 外来替换保持原样，不得删除/改写


def test_guard_cleanup_aggregates_conflicts_and_preserves_original_cause():
    """冲突清理：外来替换保持原样、自有项继续清理、冲突聚合且保留 cause。"""
    mapping = {}
    guard = _SysModulesGuard(mapping, ('pkg',))
    parent = guard.new_module('pkg')
    guard.install('pkg', parent)
    child_a = _make_fake_module('pkg.a')
    child_b = _make_fake_module('pkg.b')
    mapping['pkg.a'] = child_a
    mapping['pkg.b'] = child_b
    parent.a = child_a
    parent.b = child_b
    guard.record_import_surface()
    foreign = _make_fake_module('pkg.a')
    mapping['pkg.a'] = foreign   # 外来替换 sys.modules 条目
    parent.b = object()          # 外来替换父包属性
    original = RuntimeError('original import failure')
    with pytest.raises(_CleanupConflict) as excinfo:
        guard.cleanup(original_exc=original)
    assert excinfo.value.__cause__ is original
    message = str(excinfo.value)
    assert "'pkg.a'" in message and "'b'" in message  # 两处冲突都被聚合
    assert mapping['pkg.a'] is foreign                # 外来条目保持原样
    assert 'pkg.b' not in mapping                     # 自有条目继续清理
    assert 'pkg' not in mapping
    assert not hasattr(parent, 'a')                   # 仍是自有对象的属性已删除
    assert parent.b is not child_b                    # 外来属性保持原样


# ---------------------------------------------------------------------------
# _make_fake_module 所有权机制的纯合成回归：不触碰真实 sys.modules，
# 不导入任何应用模块（R5 已实现 guard 私有 exact-object 登记取代模块
# 属性哨兵；本测试当前应当通过，作为回归保护）。
# ---------------------------------------------------------------------------


def test_fake_module_does_not_expose_owner_marker_attribute():
    """纯合成回归（R5 已实现 guard 私有登记，当前应当通过）：fake 模块不得把
    所有权哨兵暴露为模块属性。

    所有权哨兵是测试基础设施的内部实现细节；R5 之前的实现曾把它 setattr 成
    `_FAKE_OWNER_ATTR`（'__aistock_safety_test_owner__'）模块属性，等于把
    内部标记泄漏进 fake 模块对外可见的命名空间：vars()/dir() 可枚举，被
    测的 api/services/strategies 代码与任何导入方同样可见、可遮蔽。
    R5 起所有权改由 guard 私有 exact-object registry 证明，不再占用任何
    模块属性；本测试保留为回归性质，防止重新引入模块属性哨兵。
    """
    module = _make_fake_module('utils')
    assert not hasattr(module, '__aistock_safety_test_owner__'), (
        "当前实现把私有哨兵挂在模块属性 '__aistock_safety_test_owner__' 上，"
        'fake 模块的公开命名空间因此暴露内部所有权标记（可枚举、可被被测'
        '代码遮蔽或探测）。所有权判定必须改为不占用模块属性的内部机制；'
        '本切片仅添加该 RED 断言，不修改 _make_fake_module 或哨兵实现。'
    )


# ---------------------------------------------------------------------------
# _guarded_teardown 受审契约的纯合成 RED 测试（R6：仅添加测试，不实现）。
# _inspect_preview_route_surface 的 finally 块中，record_import_surface 若
# 抛出任何异常，其后的 cleanup 将不会执行——fake utils 与已导入的应用
# 模块会残留在 sys.modules。_guarded_teardown(guard, original_exc=None)
# 是拟议修复契约：record_import_surface 的失败不得跳过 cleanup，也不得
# 掩盖被保护操作的原始异常。本切片只写测试，函数尚未实现，两条测试
# 预期在“契约存在”断言处失败（预期 RED 形态），实现修复属后续切片。
# 全部使用局部合成 mapping，不触碰真实 sys.modules，不导入任何应用模块。
# ---------------------------------------------------------------------------


def test_guarded_teardown_runs_cleanup_when_record_fails():
    """契约：record_import_surface 失败不得跳过 cleanup，异常必须外抛。"""
    mapping = {}
    guard = _SysModulesGuard(mapping, ('pkg',))
    guard.install('pkg', guard.new_module('pkg'))

    record_error = RuntimeError('record failed')

    def _record_import_surface_boom():
        raise record_error

    guard.record_import_surface = _record_import_surface_boom  # 实例属性覆盖

    assert globals().get('_guarded_teardown') is not None, (
        '受审契约缺失：模块级辅助函数 '
        '_guarded_teardown(guard, original_exc=None) 尚未实现（预期 RED '
        '形态）。该契约要求 record_import_surface 抛出的任何异常都不得'
        '导致 cleanup 被跳过——否则 fake utils 与已导入的应用模块将残留'
        '在 sys.modules，违反 fail-closed 清理纪律。'
    )

    with pytest.raises(RuntimeError) as excinfo:
        _guarded_teardown(guard)
    assert excinfo.value is record_error, (
        'record_import_surface 的失败异常必须原样向外抛出（同一异常对象），'
        '不得被吞掉或替换；teardown 辅助不得静默掩盖 record 阶段的失败。'
    )

    assert 'pkg' not in mapping, (
        'record_import_surface 失败后 cleanup 仍必须执行：guard 安装的 '
        'sys.modules 条目必须被清理，不得因 record 阶段异常而残留污染。'
    )


def test_guarded_teardown_preserves_body_error_priority():
    """契约：record 失败不得掩盖被保护操作（body）的原始异常。"""
    mapping = {}
    guard = _SysModulesGuard(mapping, ('pkg',))
    guard.install('pkg', guard.new_module('pkg'))

    record_error = RuntimeError('record failed')

    def _record_import_surface_boom():
        raise record_error

    guard.record_import_surface = _record_import_surface_boom  # 实例属性覆盖

    assert globals().get('_guarded_teardown') is not None, (
        '受审契约缺失：模块级辅助函数 '
        '_guarded_teardown(guard, original_exc=None) 尚未实现（预期 RED '
        '形态）。该契约还要求 original_exc 优先：被保护操作的原始异常'
        '不得被 teardown 阶段的 record 失败掩盖。'
    )

    def _chain_contains(root, target):
        # 纯内存遍历异常链（__cause__/__context__）；不触碰任何外部状态。
        pending = [root]
        seen = set()
        while pending:
            node = pending.pop()
            if node is None or id(node) in seen:
                continue
            seen.add(id(node))
            if node is target:
                return True
            pending.append(node.__cause__)
            pending.append(node.__context__)
        return False

    body_error = RuntimeError('body failed')
    with pytest.raises(RuntimeError) as excinfo:
        _guarded_teardown(guard, original_exc=body_error)
    raised = excinfo.value
    assert (
        raised is body_error
        or (
            isinstance(raised, _CleanupConflict)
            and raised.__cause__ is body_error
        )
    ), (
        'body 异常优先：最终向外抛出的必须是 original_exc 本身，或以它为 '
        "__cause__ 的 _CleanupConflict；record_import_surface 的失败不得"
        '抢占或掩盖被保护操作的原始异常。'
    )
    assert _chain_contains(raised, record_error), (
        'record_import_surface 的失败异常必须保留在最终异常的异常链中'
        '（__context__/__cause__ 任一），不得丢弃 record 阶段的失败原因。'
    )

    assert 'pkg' not in mapping, (
        'record_import_surface 失败后 cleanup 仍必须执行：guard 安装的 '
        'sys.modules 条目必须被清理，不得因 record 阶段异常而残留污染。'
    )


# ---------------------------------------------------------------------------
# record_import_surface 顺序无关性契约的纯合成 RED 测试（R8：仅添加测试，
# 不改任何现有实现）。PM 只读探针（与已批准路由测试同款 fake-utils 隔离，
# 不触 DB/服务/网络）实证 sys.modules 迭代顺序中存在“子模块键先于父包键”
# 的形态（services.operations.health、services.operations.read_models 先于
# 父包 services.operations）。当前单遍实现按迭代顺序以“父包名是否已在
# _installed/_import_surface 中”判定父包是否测试自有：子先于父到达时父包
# 被误判为外来，子模块的父包属性清理被记为延迟冲突，cleanup fail-closed
# 抛 _CleanupConflict。正确契约：受控命名空间内、不在基线的父包必然属于
# 本次导入面，guard 记录与父包属性清理计划不得依赖 sys.modules 键的迭代
# 顺序。实现修复属 R9；本测试不导入 app、不触碰真实 sys.modules、无网络/
# 文件 I/O。
# ---------------------------------------------------------------------------


def test_record_import_surface_is_order_independent():
    """契约：guard 记录与父包属性清理不得依赖 sys.modules 键的迭代顺序。

    纯合成复现真实导入观察到的“子模块条目先于父包条目”顺序：父包由
    guard.new_module 受控工厂创建但模拟“被导入”（不经 guard.install），
    子模块为普通合成模块。当前实现会把该父包误判为外来并使 cleanup
    fail-closed（预期 RED）；R9 修复后 cleanup 必须无冲突完成，条目与
    父包属性全部清理干净。
    """
    mapping = {}
    guard = _SysModulesGuard(mapping, ('pkg',))
    parent = guard.new_module('pkg')        # 受控工厂登记；模拟“被导入”
    child = _make_fake_module('pkg.child')  # 模拟导入系统新增的子模块条目

    # 按真实导入观察到的“子先于父”顺序写入；parent 不经 guard.install——
    # 真实场景中 services.operations 是被导入的，不是被安装的。
    mapping['pkg.child'] = child
    mapping['pkg'] = parent
    parent.child = child                    # 模拟导入系统设置父包属性

    guard.record_import_surface()

    try:
        guard.cleanup()
    except _CleanupConflict as exc:
        raise AssertionError(
            'guard 记录与父包属性清理不得依赖 sys.modules 键的迭代顺序：'
            '受控命名空间内、不在基线的父包属于本次导入面，不得因子模块'
            '键先于父包键出现而误判为外来、拒绝清理其属性（fail-closed '
            f'误报）：{exc}'
        ) from exc

    assert 'pkg' not in mapping, (
        '顺序无关契约：受控命名空间内、不在基线的父包属于本次导入面，'
        'cleanup 必须清除该条目，不得因键迭代顺序而豁免。'
    )
    assert 'pkg.child' not in mapping, (
        '顺序无关契约：子模块条目属于本次导入面，cleanup 必须清除该条目。'
    )
    assert not hasattr(parent, 'child'), (
        '顺序无关契约：父包属性清理计划必须在父包键后到的情况下仍被建立'
        '并执行，导入系统设置的子模块属性不得残留在父包上。'
    )
