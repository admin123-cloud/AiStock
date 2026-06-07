# 定时任务调度器使用文档

## 概述

定时任务调度器用于执行周期性的数据同步任务，包括：
- 全量K线数据同步（每周六凌晨1点）
- 实时行情更新（盘中每分钟）

## 快速开始

### 1. 查看配置信息

```bash
cd backend
python scheduler_run.py config
```

### 2. 查看交易日信息

```bash
python scheduler_run.py trading
```

### 3. 手动执行全量K线同步

```bash
python scheduler_run.py sync
```

### 4. 手动执行实时行情更新

```bash
python scheduler_run.py quotes
```

### 5. 启动调度器（独立运行）

```bash
python scheduler_run.py start
```

### 6. 启动后端服务（集成调度器）

```bash
python main.py
```

## 定时任务说明

### 1. 全量K线同步任务

**执行时间**: 每周六凌晨1点

**功能**:
- 同步所有股票的日线、周线、月线、季线、年线K线数据
- 使用前复权数据
- 分批处理，避免API限流

**配置** (在 `src/scheduler/config.py` 中):
```python
"full_sync_kline": {
    "enabled": True,
    "cron_expression": "0 1 * * 6",
    "description": "全量同步日线、周线、月线、季线、年线K线数据",
    "timeout": 7200,  # 2小时超时
}
```

### 2. 实时行情更新任务

**执行时间**: 盘中每分钟（9:30-11:30, 13:00-15:00）

**功能**:
- 只在交易日执行
- 只在交易时间内更新
- 更新自选股的实时行情数据

**配置**:
```python
"realtime_quotes": {
    "enabled": True,
    "description": "盘中每分钟实时更新自选股行情数据",
    "trading_days_only": True,
    "trading_hours": {
        "start": "09:30",
        "end": "15:00",
    },
    "interval": 60,  # 60秒间隔
}
```

## 配置说明

配置文件位置: `src/scheduler/config.py`

### 启用/禁用任务

```python
SCHEDULER_CONFIG = {
    "enabled": True,  # 总开关

    "jobs": {
        "full_sync_kline": {
            "enabled": True,  # 启用/禁用此任务
            ...
        },
        "realtime_quotes": {
            "enabled": True,  # 启用/禁用此任务
            ...
        }
    }
}
```

### K线同步配置

```python
"sync": {
    "kline": {
        "periods": ["1d", "1w", "1M", "1Q", "1Y"],  # 同步的周期
        "count": 5000,  # 每次获取的数量
        "adjust": "forward",  # 复权类型
        "batch_size": 50,  # 每批处理的股票数量
        "delay_between_batches": 1.0,  # 批次间延迟（秒）
    }
}
```

### 行情同步配置

```python
"sync": {
    "quotes": {
        "max_retries": 3,  # 最大重试次数
        "timeout": 10,  # 请求超时（秒）
    }
}
```

## 交易日历

### 判断是否为交易日

```python
from src.scheduler.trading_calendar import TradingCalendar

from datetime import datetime

# 判断是否为交易日
is_trading = TradingCalendar.is_trading_day(datetime.now())

# 判断是否在交易时间内
is_trading_time = TradingCalendar.is_trading_time(datetime.now())

# 判断市场是否开盘
is_market_open = TradingCalendar.is_market_open(datetime.now())

# 计算距离开盘时间
time_to_open = TradingCalendar.time_to_market_open(datetime.now())

# 计算距离收盘时间
time_to_close = TradingCalendar.time_to_market_close(datetime.now())
```

## 日志输出

调度器会输出详细的日志信息：

```
============================================================
启动定时任务调度器
============================================================
INFO | 已调度全量K线同步任务: 0 1 * * 6
INFO | 任务描述: 全量同步日线、周线、月线、季线、年线K线数据
INFO | 已调度实时行情更新任务: 每60秒执行一次
INFO | 任务描述: 盘中每分钟实时更新自选股行情数据
INFO | 定时任务调度器启动成功
```

### 全量K线同步日志

```
============================================================
开始执行全量K线数据同步任务
============================================================
INFO | 共 5235 只股票需要同步K线数据
INFO | 处理第 1/105 批，共 50 只股票
INFO |   600000 1d: 成功获取 5694 条数据
INFO |   600000 1w: 成功获取 1234 条数据
...
INFO | 等待 1.0 秒后处理下一批...
...
============================================================
全量K线数据同步任务完成
============================================================
INFO | 总耗时: 2456.78 秒
INFO | 成功: 5235 只股票
INFO | 失败: 0 只股票
============================================================
```

### 实时行情更新日志

```
INFO | 开始执行实时行情更新任务
INFO | 共 50 只自选股需要更新实时行情
INFO | 600000: 更新成功
...
INFO | 实时行情更新完成 - 成功: 50, 失败: 0
DEBUG | 等待 60 秒后再次执行...
```

## 部署建议

### 开发环境

```bash
# 终端1: 启动API服务器（集成调度器）
python main.py

# 终端2: 如果需要单独运行调度器
python scheduler_run.py start
```

### 生产环境

建议使用 `supervisor` 或 `systemd` 管理调度器进程。

#### Supervisor 配置示例

创建配置文件 `/etc/supervisor/conf.d/aistock-scheduler.conf`:

```ini
[program:aistock-scheduler]
command=/path/to/python /path/to/AiStock/backend/scheduler_run.py start
directory=/path/to/AiStock/backend
user=www-data
autostart=true
autorestart=true
stderr_logfile=/var/log/aistock/scheduler.err.log
stdout_logfile=/var/log/aistock/scheduler.out.log
environment=PYTHONPATH="/path/to/AiStock/backend"
```

#### Systemd 配置示例

创建配置文件 `/etc/systemd/system/aistock-scheduler.service`:

```ini
[Unit]
Description=AiStock Scheduler
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/AiStock/backend
Environment=PYTHONPATH=/path/to/AiStock/backend
ExecStart=/path/to/python /path/to/AiStock/backend/scheduler_run.py start
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务:

```bash
sudo systemctl daemon-reload
sudo systemctl enable aistock-scheduler
sudo systemctl start aistock-scheduler
sudo systemctl status aistock-scheduler
```

## 监控和维护

### 查看调度器日志

```bash
# 查看实时日志
tail -f logs/scheduler.log

# 查看错误日志
tail -f logs/scheduler.error.log
```

### 检查任务执行情况

```bash
# 查看配置
python scheduler_run.py config

# 查看交易日信息
python scheduler_run.py trading
```

### 故障排查

1. **任务未执行**
   - 检查 `SCHEDULER_CONFIG["enabled"]` 是否为 `True`
   - 检查具体任务的 `enabled` 是否为 `True`
   - 查看日志文件确认调度器是否正常运行

2. **实时行情不更新**
   - 检查当前是否为交易日和交易时间
   - 运行 `python scheduler_run.py trading` 查看
   - 检查API Key是否有效

3. **K线同步失败**
   - 检查网络连接
   - 检查API是否正常
   - 检查数据库连接
   - 查看详细错误日志

## 注意事项

1. **API限流**: TickFlow API有频率限制，已通过批次大小和延迟进行控制
2. **数据库性能**: 大量数据同步可能影响数据库性能，建议在低峰期执行
3. **节假日**: 目前节假日判断功能待完善，暂时仅排除周末
4. **日志管理**: 定期清理日志文件，避免磁盘占满
5. **监控告警**: 建议配置监控和告警，及时发现任务失败

## 未来改进

- [ ] 添加节假日数据支持
- [ ] 实现失败重试机制
- [ ] 添加任务执行状态监控
- [ ] 支持动态配置更新
- [ ] 添加任务执行历史记录
- [ ] 支持邮件/短信通知
- [ ] 优化数据库批量插入性能

## 联系方式

如有问题，请查看日志文件或联系开发团队。
