# TickFlow K线数据获取重试机制

## 概述

`TickFlowClient.fetch_klines()` 方法已内置智能重试机制，用于处理偶发的网络连接错误、超时错误和SSL错误。

## 重试配置

### 默认参数

```python
def fetch_klines(
    self,
    code: str,
    period: str = "1d",
    count: int = 5000,
    adjust: str = "forward",
    max_retries: int = 3  # 默认重试3次
)
```

### 重试策略

- **最大重试次数**: 默认3次（可通过 `max_retries` 参数自定义）
- **退避策略**: 指数退避 + 随机抖动
  - 第1次重试: 约 1 秒
  - 第2次重试: 约 2 秒
  - 第3次重试: 约 4 秒
- **随机抖动**: ±50%，避免多个请求同时重试造成雪崩

## 支持重试的错误类型

### 1. ConnectionError（连接错误）
- 网络连接被远程主机关闭
- 连接被中断

示例错误:
```
ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接。')
RemoteDisconnected('Remote end closed connection without response')
```

### 2. Timeout（超时错误）
- 请求超时（默认30秒）

示例错误:
```
HTTPSConnectionPool(host='api.tickflow.org', port=443): Read timeout
```

### 3. SSLError（SSL错误）
- SSL连接错误
- SSL协议违规

示例错误:
```
SSLEOFError(8, 'EOF occurred in violation of protocol (_ssl.c:1007)')
```

### 4. RequestException（其他HTTP错误）
- 其他请求相关错误

## 不重试的错误类型

以下错误不会触发重试，直接返回失败：
- API返回的业务错误（`data.error != 0`）
- 数据解析错误
- 其他未知异常

## 使用示例

### 基本使用（使用默认重试配置）

```python
from src.data_sources.tickflow_client import TickFlowClient

client = TickFlowClient()

# 获取日线K线数据（默认重试3次）
result = client.fetch_klines("600000", "1d", count=5000, adjust="forward")
```

### 自定义重试次数

```python
# 增加重试次数到5次
result = client.fetch_klines(
    "600000",
    "1d",
    count=5000,
    adjust="forward",
    max_retries=5
)

# 不重试
result = client.fetch_klines(
    "600000",
    "1d",
    count=5000,
    adjust="forward",
    max_retries=1
)
```

## 日志输出

重试过程会在日志中记录：

```
INFO | 获取 600000 (600000.SH) 1d K线数据（复权: forward）...
WARNING | 连接错误（第1次尝试）: 远程主机强迫关闭了一个现有的连接。，1.2秒后重试...
WARNING | 连接错误（第2次尝试）: 远程主机强迫关闭了一个现有的连接。，2.1秒后重试...
INFO | 解析列式数据: 5694 条记录
INFO | 成功获取 600000 1d K线数据: 5694 条
```

如果所有重试都失败：

```
WARNING | 连接错误（第1次尝试）: 远程主机强迫关闭了一个现有的连接。，1.2秒后重试...
WARNING | 连接错误（第2次尝试）: 远程主机强迫关闭了一个现有的连接。，2.1秒后重试...
WARNING | 连接错误（第3次尝试）: 远程主机强迫关闭了一个现有的连接。，4.3秒后重试...
ERROR | 获取K线数据失败: 连接错误，已重试3次
```

## 注意事项

1. **频率限制**: 虽然有重试机制，但仍需注意API的频率限制
2. **总耗时**: 重试会增加总耗时，3次重试最多可能增加约7-8秒
3. **并发请求**: 大量并发请求时，建议适当降低 `max_retries` 或添加请求间隔
4. **错误监控**: 密切关注日志中的错误信息，如果频繁触发重试，可能需要检查网络环境或联系API服务商

## 与TickFlow SDK重试机制的区别

TickFlow SDK也有内置重试机制，但我们的实现有以下优势：

| 特性 | TickFlow SDK | 我们的实现 |
|------|-------------|-----------|
| 支持adjust参数 | 不支持 | ✅ 支持 |
| 可自定义重试次数 | 固定 | ✅ 可配置 |
| 细粒度异常处理 | ✅ | ✅ 更详细 |
| 指数退避 + 抖动 | ✅ | ✅ |
| 详细日志 | 基础 | ✅ 更详细 |

## 性能优化建议

### 批量获取数据

对于大量股票，建议分批获取并添加适当延迟：

```python
import time

client = TickFlowClient()
stocks = ["600000", "000001", "600519"]  # 股票代码列表

for i, code in enumerate(stocks):
    print(f"处理 {i+1}/{len(stocks)}: {code}")

    # 获取日线数据
    result = client.fetch_klines(code, "1d", count=5000, adjust="forward")

    # 保存到数据库...

    # 添加延迟，避免触发频率限制
    if i < len(stocks) - 1:
        time.sleep(0.5)  # 500ms延迟
```

### 使用并发（谨慎使用）

如果需要更快的速度，可以使用线程池，但要注意控制并发数：

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

def fetch_single_stock(client, code):
    try:
        return code, client.fetch_klines(code, "1d", count=5000, adjust="forward")
    except Exception as e:
        return code, None

client = TickFlowClient()
stocks = ["600000", "000001", "600519"]  # 股票代码列表

# 使用线程池，最多3个并发
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = [executor.submit(fetch_single_stock, client, code) for code in stocks]

    for future in as_completed(futures):
        code, result = future.result()
        if result:
            print(f"{code}: 成功获取 {len(result)} 条数据")
        else:
            print(f"{code}: 获取失败")

    # 每批结束后添加延迟
    time.sleep(1)
```

## 故障排查

如果频繁遇到重试失败，请检查：

1. **网络连接**: 检查网络是否稳定，是否有防火墙限制
2. **API状态**: 访问 https://api.tickflow.org 检查API是否正常运行
3. **API Key**: 确认API Key是否有效，未过期
4. **频率限制**: 检查是否触发了API的频率限制
5. **SSL证书**: 确保系统时间正确，SSL证书验证通过

## 测试

运行测试脚本验证重试机制：

```bash
cd backend
python test_retry.py
```

## 更新日期

2026-03-21
