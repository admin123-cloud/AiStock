# AiStock 数据源获取方式梳理

## 概述

本文档详细梳理了AiStock项目中不同级别K线数据（分钟级、日线及以上级别）和实时数据的数据源获取方式，包括数据源选择、获取策略、任务调度和数据存储等方面。

## 一、分钟级K线数据（1min, 5min, 15min, 30min, 60min）

### 1.1 主要数据源
**通达信（Pytdx）** - 主要数据源

### 1.2 获取方式
```python
# 数据源：src/data_sources/pytdx.py
class PytdxDataSource:
    def get_stock_history(self, stock_code, start_date, end_date, period):
        # 支持的周期映射
        period_map = {
            "1min": 8,    # KLINE_TYPE_1MIN
            "5min": 0,    # KLINE_TYPE_5MIN
            "15min": 1,   # KLINE_TYPE_15MIN
            "30min": 2,   # KLINE_TYPE_30MIN
            "60min": 3,   # KLINE_TYPE_1HOUR
        }
```

### 1.3 任务调度
**任务文件**：`src/scheduler/tasks/minute_kline_task.py`

**调度策略**：
- **定时任务**：盘中定时执行（交易时间内）
- **数据范围**：获取过去7天的分钟级数据
- **批次处理**：每批100只股票，批次间延迟2秒
- **并发控制**：单只股票串行处理，避免请求过快

**执行流程**：
1. 获取所有活跃股票列表
2. 分批处理（每批100只）
3. 对每只股票获取5个周期的分钟K线（1min, 5min, 15min, 30min, 60min）
4. 数据转换和存储

### 1.4 数据存储
**存储位置**：MySQL数据库
**表名**：`kline_minute_1`, `kline_minute_5`, `kline_minute_15`, `kline_minute_30`, `kline_minute_60`

**存储字段**：
```sql
code, datetime, open, high, low, close, volume, amount, created_at
```

### 1.5 技术特点
- **数据源稳定性**：通达信服务器集群，高可用
- **数据限制**：分钟线数据有访问频率限制，需要分页获取
- **数据完整性**：获取过去60个交易日的数据
- **错误处理**：支持重试机制和故障转移

---

## 二、日线及以上级别K线数据（daily, weekly, monthly, quarterly, yearly）

### 2.1 主要数据源
**通达信（Pytdx）** - 主要数据源
**Tushare** - 备选数据源（需要token）

### 2.2 获取方式

#### 通达信数据源
```python
# 数据源：src/data_sources/pytdx.py
class PytdxDataSource:
    def get_stock_history(self, stock_code, start_date, end_date, period):
        # 支持的周期映射
        period_map = {
            "daily": 4,      # KLINE_TYPE_DAILY
            "weekly": 5,     # KLINE_TYPE_WEEKLY
            "monthly": 6,    # KLINE_TYPE_MONTHLY
            "quarterly": 10, # KLINE_TYPE_3MONTH
            "yearly": 11     # KLINE_TYPE_YEARLY
        }
```

#### Tushare数据源
```python
# 数据源：src/data_sources/tushare.py
class TushareDataSource:
    def get_stock_history(self, stock_code, start_date, end_date, period):
        # 使用Tushare Pro API
        # 支持日线、周线、月线等
        df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
```

### 2.3 任务调度
**任务文件**：`src/scheduler/tasks/full_sync_kline_task.py`

**调度策略**：
- **定时任务**：每周六凌晨1点执行（非交易日）
- **数据范围**：从2026-01-01开始的全量数据
- **批次处理**：每批50只股票，批次间延迟2秒
- **全周期同步**：一次性同步所有10个周期（1min-60min + daily-yearly）

**执行流程**：
1. 获取所有股票和指数代码
2. 分批处理（每批50只）
3. 对每只股票同步10个周期的K线数据
4. 数据保存到数据库

### 2.4 数据存储
**存储位置**：MySQL数据库
**表名**：`kline_daily`, `kline_weekly`, `kline_monthly`, `kline_quarterly`, `kline_yearly`

**存储字段**：
```sql
code, trade_date, open, high, low, close, volume, amount, 
change_pct, turnover_rate, created_at
```

### 2.5 技术特点
- **全量同步**：每周六执行全量数据同步
- **数据完整性**：确保历史数据完整
- **增量更新**：支持增量更新机制
- **多数据源**：主数据源故障时可切换到备选数据源

---

## 三、实时行情数据

### 3.1 主要数据源
**TickFlow** - 主要实时数据源（商业API）
**通达信（Pytdx）** - 备选实时数据源
**Tushare** - 备选数据源（需要token）

### 3.2 获取方式

#### TickFlow数据源
```python
# 数据源：src/data_sources/tickflow.py
class TickFlowDataSource:
    def get_stock_quote(self, stock_code):
        # 使用TickFlow API获取实时行情
        quotes = self._client.quotes.get(symbols=[symbol], as_dataframe=False)
        
        # 返回字段
        return {
            'code': code,
            'name': name,
            'price': last_price,
            'open': open,
            'high': high,
            'low': low,
            'pre_close': prev_close,
            'volume': volume,
            'amount': amount,
            'change': change_amount,
            'change_pct': change_pct,
            'time': timestamp
        }
```

#### 通达信实时数据源
```python
# 数据源：src/data_sources/pytdx.py
class PytdxDataSource:
    def get_stock_quote(self, stock_code):
        # 使用通达信获取实时行情
        quotes = self.client.get_quotes([stock_code])
        
        # 返回字段
        return {
            "code": quote["code"],
            "name": quote["name"],
            "price": quote["price"],
            "open": quote["open"],
            "high": quote["high"],
            "low": quote["low"],
            "pre_close": quote["prev_close"],
            "volume": quote["volume"],
            "amount": quote["amount"],
            "change": quote["change_amount"],
            "change_pct": quote["change_pct"],
            "time": datetime.now()
        }
```

### 3.3 任务调度
**任务文件**：`src/scheduler/tasks/realtime_quotes_task.py`

**调度策略**：
- **定时任务**：盘中每分钟执行一次
- **执行条件**：只在交易时间内执行
- **数据范围**：自选股列表（目前暂时获取所有活跃股票前100只）
- **并发控制**：串行处理，避免请求过快

**执行流程**：
1. 检查是否在交易时间
2. 获取自选股列表
3. 逐个获取实时行情
4. 保存到数据库（TODO：RealtimeQuote模型待实现）

### 3.4 数据存储
**存储位置**：MySQL数据库（TODO：RealtimeQuote模型待实现）
**表名**：`realtime_quotes`（待创建）

**存储字段**（建议）：
```sql
code, name, price, open, high, low, pre_close, volume, amount, 
change, change_pct, bid1, ask1, update_time, created_at
```

### 3.5 技术特点
- **实时性**：分钟级更新
- **数据源优先级**：TickFlow > 通达信 > Tushare
- **故障转移**：主数据源故障时自动切换到备选数据源
- **限流控制**：避免请求频率过高导致被封

---

## 四、数据源管理架构

### 4.1 数据源管理器
**文件**：`src/data_sources/manager.py`

```python
class DataSourceManager:
    # 注册的数据源
    SOURCE_CLASSES = {
        "pytdx": PytdxDataSource,      # 通达信
        "eastmoney": EastMoneyDataSource,  # 东方财富
        "tencent": TencentDataSource,      # 腾讯
        "tushare": TushareDataSource,      # Tushare
        "tickflow": TickFlowDataSource,    # TickFlow
    }
    
    def get_available_sources(self):
        # 获取所有可用数据源（按优先级排序）
        pass
    
    def get_primary_source(self):
        # 获取主数据源
        pass
```

### 4.2 数据源优先级配置
**配置文件**：`config/data_sources.yaml`

```yaml
data_sources:
  pytdx:
    enabled: true
    priority: 0  # 优先级最高
    batch_size: 150
    
  tickflow:
    enabled: true
    priority: 1
    token: "your_token_here"
    
  tushare:
    enabled: false
    priority: 2
    token: ""  # 需要配置
```

### 4.3 故障转移机制
```python
def get_data_with_fallback(data_type, stock_code, **kwargs):
    """带故障转移的数据获取"""
    sources = get_available_sources()
    
    for source in sources:
        try:
            if data_type == "quote":
                return source.get_stock_quote(stock_code)
            elif data_type == "history":
                return source.get_stock_history(stock_code, **kwargs)
        except Exception as e:
            logger.warning(f"{source.name} 获取数据失败，切换到下一个数据源")
            continue
    
    raise DataSourceException("所有数据源都不可用")
```

---

## 五、数据获取策略对比

| 数据类型 | 主要数据源 | 备选数据源 | 更新频率 | 数据范围 | 存储方式 |
|---------|-----------|-----------|---------|---------|---------|
| 1分钟K线 | 通达信 | - | 盘中定时 | 过去7天 | MySQL |
| 5分钟K线 | 通达信 | - | 盘中定时 | 过去7天 | MySQL |
| 15分钟K线 | 通达信 | - | 盘中定时 | 过去7天 | MySQL |
| 30分钟K线 | 通达信 | - | 盘中定时 | 过去7天 | MySQL |
| 60分钟K线 | 通达信 | - | 盘中定时 | 过去7天 | MySQL |
| 日线 | 通达信 | Tushare | 每周六全量 | 全量历史 | MySQL |
| 周线 | 通达信 | Tushare | 每周六全量 | 全量历史 | MySQL |
| 月线 | 通达信 | Tushare | 每周六全量 | 全量历史 | MySQL |
| 季线 | 通达信 | Tushare | 每周六全量 | 全量历史 | MySQL |
| 年线 | 通达信 | Tushare | 每周六全量 | 全量历史 | MySQL |
| 实时行情 | TickFlow | 通达信 | 每分钟 | 当前时刻 | MySQL（TODO） |
| 股票列表 | Tushare | 通达信 | 每日 | 全量 | MySQL |

---

## 六、存在的问题和改进建议

### 6.1 当前存在的问题

1. **实时数据存储未完成**
   - RealtimeQuote模型待实现
   - 实时数据表结构待定义

2. **Tushare数据源未启用**
   - 缺少有效的Tushare token
   - 作为备选数据源未实际使用

3. **数据源健康检查不完善**
   - 缺少定期的数据源可用性检查
   - 故障转移机制需要完善

4. **数据一致性检查缺失**
   - 缺少数据完整性校验
   - 缺少缺失数据检测和补全机制

5. **限流和熔断机制不完善**
   - 缺少统一的限流控制
   - 缺少熔断机制防止级联故障

### 6.2 改进建议

1. **完善实时数据存储**
   ```python
   # 创建RealtimeQuote模型
   class RealtimeQuote(Base):
       __tablename__ = 'realtime_quotes'
       
       id = Column(Integer, primary_key=True)
       code = Column(String(10), index=True)
       name = Column(String(50))
       price = Column(DECIMAL(10, 2))
       # ... 其他字段
       update_time = Column(DateTime)
       created_at = Column(DateTime, default=datetime.now)
   ```

2. **启用Tushare数据源**
   - 申请Tushare token
   - 配置为备选数据源
   - 实现数据格式转换

3. **添加数据源健康检查**
   ```python
   class DataSourceHealthChecker:
       def check_all_sources(self):
           for source in self.sources:
               health = source.health_check()
               if not health.is_healthy:
                   self.alert(f"{source.name} 数据源异常")
   ```

4. **实现数据一致性检查**
   ```python
   class DataConsistencyChecker:
       def check_kline_data(self, code, period):
           # 检查数据连续性
           # 检查数据完整性
           # 自动补全缺失数据
           pass
   ```

5. **完善限流和熔断机制**
   ```python
   from circuitbreaker import circuit
   
   @circuit(failure_threshold=5, recovery_timeout=60)
   def get_data_from_source(source, **kwargs):
       return source.get_data(**kwargs)
   ```

---

## 七、总结

AiStock项目目前主要使用**通达信（Pytdx）**作为K线数据的主要数据源，**TickFlow**作为实时行情的主要数据源。数据获取采用分层架构，通过数据源管理器统一管理多个数据源，支持故障转移和优先级切换。

**核心特点**：
1. **多数据源支持**：通达信、TickFlow、Tushare、东方财富等
2. **分级数据获取**：分钟级数据盘中获取，日线及以上级别周末全量同步
3. **故障转移机制**：主数据源故障时自动切换到备选数据源
4. **任务调度管理**：使用APScheduler进行定时任务调度

**待完善项**：
1. 实时数据存储实现
2. Tushare数据源启用
3. 数据一致性检查
4. 限流和熔断机制

---

## 八、相关文件清单

### 数据源实现
- `src/data_sources/pytdx.py` - 通达信数据源
- `src/data_sources/tickflow.py` - TickFlow数据源
- `src/data_sources/tushare.py` - Tushare数据源
- `src/data_sources/manager.py` - 数据源管理器

### 任务调度
- `src/scheduler/tasks/minute_kline_task.py` - 分钟K线任务
- `src/scheduler/tasks/full_sync_kline_task.py` - 全量K线同步任务
- `src/scheduler/tasks/realtime_quotes_task.py` - 实时行情任务

### 配置
- `config/data_sources.yaml` - 数据源配置
- `src/scheduler/config.py` - 调度器配置

### 数据存储
- `src/services/data_storage.py` - 数据存储服务
- `src/models/stock.py` - 股票模型

---

*文档生成时间：2026-03-27*
*版本：v1.0*
