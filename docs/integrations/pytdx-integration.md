# pytdx实时行情集成说明

## 概述

已将实时行情数据源从TickFlow切换到pytdx（通达信官方行情接口），大幅提升数据获取速度。

## 改进对比

| 指标 | TickFlow | pytdx | 提升 |
|------|----------|--------|------|
| **轮询时间** | 110分钟 | ~30秒 | **220倍** |
| **批次大小** | 5只/批 | 100只/批 | 20倍 |
| **请求延迟** | 6秒 | 0.5秒 | 12倍 |
| **费用** | 付费 | 免费 | - |
| **稳定性** | 好 | 好 | - |

## 文件变更

### 新增文件

1. **`backend/src/data_sources/pytdx_client.py`**
   - pytdx客户端封装
   - 支持批量查询（100只/批）
   - 自动重连和多服务器failover
   - 上下文管理器支持

### 修改文件

2. **`backend/src/scheduler/tasks/intraday_sentiment_task.py`**
   - 盘中使用pytdx替代TickFlow
   - 批次大小从5调整为100
   - 请求延迟从6秒调整为0.5秒
   - 保留TickFlow作为备选方案

## 使用方法

### 安装依赖

```bash
pip install pytdx
```

### 配置说明

在 `intraday_sentiment_task.py` 中：

```python
# pytdx配置（盘中使用）
from src.data_sources.pytdx_client import PytdxClient
self.batch_size = 100  # 每批查询100只股票
self.request_delay = 0.5  # 请求间延迟0.5秒
```

### 调度器配置

当前配置保持不变（每2分钟执行一次），但每次执行时间大幅缩短：

```
原来：110分钟完成全市场更新 → 无法满足2分钟周期
现在：30秒完成全市场更新 → 完全满足需求
```

## API说明

### PytdxClient

```python
from src.data_sources.pytdx_client import PytdxClient

# 创建客户端
client = PytdxClient(batch_size=100)

# 获取实时行情
quotes = client.get_quotes(['000001', '600000'])

# 使用上下文管理器
with PytdxClient() as client:
    quotes = client.get_quotes(['000001', '600000'])
```

### 返回数据格式

```python
[
    {
        "code": "000001",
        "name": "平安银行",
        "price": 10.50,
        "prev_close": 10.40,
        "open": 10.42,
        "high": 10.55,
        "low": 10.38,
        "volume": 12345678,
        "amount": 123456789.0,
        "change_pct": 0.96,
        "change_amount": 0.10,
        "timestamp": 1234567890.0
    },
    ...
]
```

## 注意事项

1. **换手率**：pytdx不提供换手率数据，暂设为0
2. **历史数据**：pytdx仅用于实时行情，历史K线仍使用TickFlow
3. **连接稳定性**：已实现自动重连和多服务器failover机制
4. **限流**：建议不要超过0.5秒的请求间隔

## 性能优化建议

1. **分层更新策略**（可选）：
   - 优先更新自选股/持股（每30秒）
   - 批量更新其他股票（每2分钟）

2. **并行处理**（可选）：
   - 多进程并行查询不同市场（沪市/深市）
   - 可进一步降低轮询时间到15秒

3. **缓存策略**（可选）：
   - Redis缓存当日已获取数据
   - 避免重复查询相同股票

## 测试建议

### 手动测试

```bash
cd backend
python -c "
from src.data_sources.pytdx_client import PytdxClient

client = PytdxClient()
with client:
    quotes = client.get_quotes(['000001', '600000', '000002'])
    print(quotes)
"
```

### 集成测试

1. 重启调度器服务
2. 检查日志中的更新时间
3. 验证数据完整性

## 回滚方案

如果pytdx出现问题，可快速回滚到TickFlow：

在 `intraday_sentiment_task.py` 中将 `self._execute_pytdx()` 改回 `self._execute_tickflow()`

## 未来优化

1. 考虑添加WebSocket推送接口（实时性更高）
2. 实现增量更新机制（只更新变化的股票）
3. 添加数据质量监控和告警
