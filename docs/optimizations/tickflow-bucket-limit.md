# TickFlow API 分桶限流修复说明

## 问题更正

根据用户反馈，TickFlow的限流规则是：
- **日线数据接口**：10次/分钟 = 6秒/请求
- **实时行情接口**：10次/分钟 = 6秒/请求
- **两个接口的限流是独立的，互不影响**

之前实现的全局限流是错误的，需要改为按接口类型的分桶限流。

## 分桶限流实现

### 1. 数据结构

```python
class TickFlowClient:
    # 按接口类型分别跟踪请求时间
    _request_trackers: dict[str, dict] = defaultdict(lambda: {
        "last_request_time": 0,
        "lock": Lock()
    })

    # 各接口类型的限流配置
    _rate_limits: dict[str, float] = {
        "klines": 6.0,      # 日线数据：6秒/请求
        "quotes": 6.0,      # 实时行情：6秒/请求
        "default": 6.0       # 默认：6秒/请求
    }
```

### 2. 接口类型判断

```python
@classmethod
def _get_request_type(cls, period: str) -> str:
    """根据请求参数确定接口类型"""
    # 日线、周线、月线、季线等归为klines接口
    if period in ["1d", "1w", "1M", "1Q", "1Y"]:
        return "klines"

    # 分钟线归为quotes接口（实时行情）
    if period in ["1m", "5m", "15m", "30m", "60m"]:
        return "quotes"

    return "default"
```

### 3. 分桶限流方法

```python
@classmethod
def _wait_for_rate_limit(cls, request_type: str):
    """等待以满足速率限制（按接口类型分桶限流）"""
    tracker = cls._request_trackers[request_type]
    rate_limit = cls._rate_limits.get(request_type, cls._rate_limits["default"])

    with tracker["lock"]:
        current_time = time.time()
        time_since_last_request = current_time - tracker["last_request_time"]

        if time_since_last_request < rate_limit:
            wait_time = rate_limit - time_since_last_request
            if wait_time > 0:
                logger.debug(f"[{request_type}] 速率限制：等待 {wait_time:.2f} 秒...")
                time.sleep(wait_time)

        tracker["last_request_time"] = time.time()
```

### 4. 应用限流

```python
def fetch_klines(self, code: str, period: str = "1d", ...):
    # 确定请求类型
    request_type = self._get_request_type(period)

    for attempt in range(max_retries):
        try:
            # 应用按接口类型的速率限制
            self._wait_for_rate_limit(request_type)

            # 执行请求...
            response = requests.get(url, ...)
```

## 分桶限流的优势

### 1. 接口独立
- 日线接口和实时行情接口的请求计数是分开的
- 两者互不影响，可以并行使用

### 2. 性能优化
**场景**：同时更新日线和分钟线数据

| 接口类型 | 全局限流（错误） | 分桶限流（正确） |
|---------|----------------|----------------|
| 日线接口 | 6秒/请求 | 6秒/请求 |
| 实时行情 | 需等待日线完成 | 6秒/请求 |
| 总耗时 | 2只股票 = 12秒 | 2只股票 = 6秒（并行） |

### 3. 资源利用率
- 全局限流：只能使用一个接口的额度（10次/分钟）
- 分桶限流：可以使用两个接口的额度（10+10=20次/分钟）

## 实际应用场景

### 场景1：轻量修复（只更新日线）
```python
# 只使用 klines 接口
client.fetch_klines(code="600000", period="1d", count=1)
# 限流：6秒/请求
# 吞吐量：10次/分钟
```

### 场景2：更新分钟线数据
```python
# 只使用 quotes 接口
client.fetch_klines(code="600000", period="5m", count=1)
# 限流：6秒/请求
# 吞吐量：10次/分钟
```

### 场景3：同时更新日线和分钟线
```python
# 并行使用两个接口
client.fetch_klines(code="600000", period="1d", count=1)   # klines桶
client.fetch_klines(code="600000", period="5m", count=1)   # quotes桶

# 每个接口独立限流：6秒/请求
# 总吞吐量：20次/分钟（10+10）
```

## 配置建议

### scheduler/config.py
```python
# 轻量修复（日线）
"intraday_sentiment": {
    "batch_size": 50,
    "delay_between_requests": 6.0,  # 必须等于6秒
}

# 全量同步（日线）
"full_sync_kline": {
    "batch_size": 50,
    "delay_between_batches": 6.0,  # 必须等于6秒
}
```

## 性能对比

### 更新5000只股票日线数据
- 单接口吞吐量：10次/分钟 = 600次/小时
- 总耗时：5000 / 600 ≈ **8.3小时**

### 更新5000只股票分钟线数据
- 单接口吞吐量：10次/分钟 = 600次/小时
- 总耗时：5000 / 600 ≈ **8.3小时**

### 同时更新日线和分钟线（并行）
- 总吞吐量：20次/分钟 = 1200次/小时
- 总耗时：10000 / 1200 ≈ **8.3小时**

**结论**：分桶限流可以将并行任务的总吞吐量提升一倍！

## 注意事项

1. **线程安全**：每个接口类型有独立的锁
2. **实例共享**：所有`TickFlowClient`实例共享同一套限流器
3. **可扩展性**：新增接口类型只需在`_rate_limits`中添加配置
4. **自动识别**：通过`period`参数自动识别接口类型

## 监控建议

可以添加日志监控各接口的请求情况：

```python
logger.info(f"[{request_type}] 请求间隔: {time_since_last_request:.2f}秒")
```

这样可以看到：
- klines接口：~6秒
- quotes接口：~6秒
- 两个接口互不影响
