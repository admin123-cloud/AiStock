# 定时任务模块

该模块负责管理和执行所有定时任务，采用模块化设计，每个任务独立为一个文件，方便管理和维护。

## 目录结构

```
scheduler/
├── __init__.py           # 包初始化文件
├── README.md            # 本文档
├── config.py            # 定时任务配置文件
├── scheduler.py         # 调度器核心逻辑
├── trading_calendar.py  # 交易日历工具
├── cli.py               # 命令行启动工具
└── tasks/               # 定时任务目录
    ├── __init__.py      # 任务包初始化文件
    ├── full_sync_kline_task.py      # 全量K线同步任务
    ├── realtime_quotes_task.py      # 实时行情更新任务
    └── realtime_indices_task.py     # 实时指数更新任务
```

## 任务列表

### 1. 全量K线同步任务 (full_sync_kline_task.py)

**功能**: 同步所有股票和指数的K线数据

**执行时间**: 每周六凌晨1点

**详细说明**:
- 同步日线、周线、月线、季线、年线数据
- 使用前复权数据
- 分批处理，每批50只股票
- 支持断点续传

**配置位置**: `SCHEDULER_CONFIG["jobs"]["full_sync_kline"]`

### 2. 实时行情更新任务 (realtime_quotes_task.py)

**功能**: 更新自选股的实时行情

**执行时间**: 盘中每分钟执行一次

**详细说明**:
- 只在交易时间内执行（9:30-15:00）
- 只在交易日执行
- 更新自选股列表的实时行情

**配置位置**: `SCHEDULER_CONFIG["jobs"]["realtime_quotes"]`

**TODO**:
- 添加Watchlist模型支持
- 实现TickFlow实时行情接口调用

### 3. 实时指数更新任务 (realtime_indices_task.py)

**功能**: 更新主要指数的实时K线数据

**执行时间**: 盘中每2分钟执行一次

**详细说明**:
- 只在交易时间内执行（9:30-15:00）
- 只在交易日执行
- 更新5个主要指数：
  - 上证指数 (000001.SH)
  - 深证成指 (399001.SZ)
  - 创业板指 (399006.SZ)
  - 沪深300 (000300.SH)
  - 上证50 (000016.SH)
- 每次获取最新一条K线数据并更新数据库

**配置位置**: `SCHEDULER_CONFIG["jobs"]["realtime_indices"]`

### 4. 盘中涨跌统计更新任务 (intraday_sentiment_task.py)

**功能**: 盘中实时更新所有股票的涨跌数据

**执行时间**: 盘中每5分钟执行一次（可配置）

**详细说明**:
- 只在交易时间内执行（9:30-15:00）
- 只在交易日执行
- 更新所有股票的最新K线数据（实时数据）
- 用于首页的涨跌统计展示
- 分批处理，每批50只股票
- 使用不复权数据（实时涨跌幅）

**配置位置**: `SCHEDULER_CONFIG["jobs"]["intraday_sentiment"]`

**重要说明**:
- 该任务确保盘中涨跌统计数据的实时性
- 配合盘后从数据库查询的最新交易日数据
- 每5分钟更新一次所有股票的最新K线，确保数据是最新的

## 配置说明

所有任务配置都在 `config.py` 文件中：

```python
SCHEDULER_CONFIG = {
    "enabled": True,  # 是否启用定时任务
    "jobs": {
        # 各个任务的配置
    },
    "sync": {
        # 数据同步配置
    },
    "notification": {
        # 通知配置
    },
}
```

## 使用方法

### 1. 命令行启动

```bash
cd backend
python scheduler_run.py
```

### 2. 手动执行单个任务

```python
from scheduler.tasks import RealtimeIndicesTask

task = RealtimeIndicesTask()
await task.execute()
```

### 3. 在代码中启动调度器

```python
from scheduler.scheduler import SchedulerManager

# 获取调度器实例
scheduler = SchedulerManager.get_instance()

# 启动调度器
await scheduler.start()

# 停止调度器
await scheduler.stop()
```

## 添加新任务

1. 在 `tasks/` 目录下创建新的任务文件（如 `new_task.py`）
2. 定义任务类，继承基本结构
3. 实现 `execute()` 方法
4. 在 `tasks/__init__.py` 中导入新任务
5. 在 `config.py` 中添加任务配置
6. 在 `scheduler.py` 中注册新任务

示例：

```python
# tasks/new_task.py
"""新任务说明"""

import asyncio
from utils.logger import get_logger

logger = get_logger("NewTask")


class NewTask:
    """新任务"""

    def __init__(self):
        """初始化任务"""
        pass

    async def execute(self):
        """
        执行任务
        """
        logger.info("开始执行新任务")
        # 实现任务逻辑
        logger.info("新任务执行完成")
```

## 日志查看

所有定时任务的日志都会输出到日志系统中，可以通过日志级别筛选：

```python
from src.utils.logger import get_logger

logger = get_logger("NewTask")
logger.info("信息日志")
logger.warning("警告日志")
logger.error("错误日志")
```

## 注意事项

1. 所有任务都是异步执行的，使用 `async/await` 语法
2. 数据库操作使用 `db.get_session()` 获取会话，使用后必须关闭
3. API请求需要添加适当的延迟，避免触发限流
4. 任务执行失败会记录错误日志，但不会中断整个调度器
5. 调度器支持优雅关闭（Ctrl+C）

## 故障排查

1. **任务不执行**: 检查 `config.py` 中任务是否启用
2. **数据库错误**: 检查数据库连接是否正常
3. **API限流**: 检查请求频率和延迟设置
4. **日志无输出**: 检查日志配置和级别设置
