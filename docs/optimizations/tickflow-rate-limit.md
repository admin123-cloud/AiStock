# TickFlow API 限流修复说明

## 问题描述

1. **轻量修复间隔过大**：实际请求间隔达到3-5秒，远超过配置的0.2秒
2. **限流限制不明确**：不清楚6秒限制是针对单个股票还是所有TickFlow请求

## 问题根源

### 1. 配置错误
```python
# 原配置（错误）
"delay_between_requests": 0.2  # 0.2秒（200ms）
```
配置为0.2秒，但TickFlow免费版限制是10请求/分钟 = 6秒/请求，导致实际请求被API限流，延迟变长。

### 2. 缺少全局限流机制
原来的`TickFlowClient`只有重试延迟，没有主动限流，无法保证不超过API限制。

## 解决方案

### 1. 修改配置文件

**文件**: `backend/src/scheduler/config.py`

```python
# 修改后
"delay_between_requests": 6.0  # 6秒（符合TickFlow免费版限制）
```

### 2. 添加全局限流器

**文件**: `backend/src/data_sources/tickflow_client.py`

```python
class TickFlowClient:
    # 类级别的限流变量（所有实例共享）
    _last_request_time = 0
    _rate_limit_lock = Lock()
    _request_interval = 6.0  # 6秒一个请求（10请求/分钟）

    @classmethod
    def _wait_for_rate_limit(cls):
        """等待以满足速率限制"""
        with cls._rate_limit_lock:
            current_time = time.time()
            time_since_last_request = current_time - cls._last_request_time

            if time_since_last_request < cls._request_interval:
                wait_time = cls._request_interval - time_since_last_request
                if wait_time > 0:
                    logger.debug(f"速率限制：等待 {wait_time:.2f} 秒...")
                    time.sleep(wait_time)

            cls._last_request_time = time.time()
```

### 3. 在请求前应用限流

```python
def fetch_klines(self, ...):
    for attempt in range(max_retries):
        try:
            # 应用全局速率限制
            self._wait_for_rate_limit()
            
            # 执行请求...
            response = requests.get(url, ...)
```

## 关键问题解答

### Q1: 6秒限制是针对单个股票还是所有TickFlow请求？

**A: 针对所有TickFlow请求（全局限流）**

- TickFlow免费版限制：**10请求/分钟** = **6秒/请求**
- 这个限制是针对API Key级别的，不是针对单个股票
- 所有使用同一API Key的请求都会被计入限流

### Q2: 为什么之前会变成3-5秒？

**原因**：
1. 配置设置为0.2秒，但实际发送频率远超API限制
2. TickFlow服务器对超限请求进行限流，实际响应延迟变长
3. 重试机制增加了额外延迟
4. 最终表现为3-5秒的实际间隔

### Q3: 现在的6秒延迟是固定的吗？

**A: 不是固定的，是保证的最小间隔**

- 系统会记录上次请求时间
- 如果距离上次请求已超过6秒，立即执行（无延迟）
- 如果不足6秒，等待到满6秒
- 这样可以最大化利用API额度，同时避免超限

## 性能影响

### 修改前
- 配置：0.2秒/请求
- 实际：3-5秒/请求（被API限流）
- 5000只股票：需要 25000-41667秒 ≈ **7-11.5小时**

### 修改后
- 配置：6秒/请求
- 实际：6秒/请求（稳定）
- 5000只股票：需要 30000秒 ≈ **8.3小时**

**结论**：实际总耗时相近，但：
- ✅ 请求稳定性更好
- ✅ 不会触发API限流
- ✅ 减少重试和错误
- ✅ 资源利用更高效

## 其他任务的影响

所有使用`TickFlowClient`的任务都会受到限流保护：

| 任务 | 调用方式 | 间隔 |
|------|---------|------|
| 轻量修复（intraday_sentiment） | 循环调用 | 6秒/请求 |
| 全量K线同步（full_sync_kline） | 循环调用 | 6秒/请求 |
| 实时行情（realtime_quotes） | 循环调用 | 6秒/请求 |
| 实时指数（realtime_indices） | 循环调用 | 6秒/请求 |

## 注意事项

1. **多线程/多进程**：使用了线程锁，多线程调用也安全
2. **多实例共享**：所有`TickFlowClient`实例共享同一个限流器
3. **可调整性**：如果升级到付费版，可以修改`_request_interval`
4. **优先级**：不同任务混用时，先来先服务

## 监控建议

可以添加日志监控限流效果：

```python
logger.info(f"实际请求间隔: {time_since_last_request:.2f}秒")
```

这样可以看到：
- 大多数时候：~6秒（正常限流）
- 偶尔：<6秒（说明有空隙）
- 从不 >6秒（说明配置正确）
