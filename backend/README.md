# StockPy - 大盘情绪监控系统

StockPy是一个集成实时市场数据、K线分析、技术指标计算、自动回测和大盘情绪监控的A股量化交易平台。

## 项目结构

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI应用入口
│   ├── config.py            # 配置管理
│   ├── database.py          # 数据库连接
│   ├── api/                 # API接口（RESTful）
│   │   ├── __init__.py
│   │   ├── health.py        # 健康检查
│   │   ├── kline.py         # K线数据接口
│   │   ├── sentiment.py     # 情绪数据接口
│   │   └── backtest.py      # 回测接口（规划中）
│   ├── models/              # 数据模型
│   │   ├── models.py        # SQLAlchemy ORM模型
│   │   └── schemas.py       # Pydantic验证模型
│   ├── services/            # 业务逻辑服务
│   │   ├── data_fetcher.py       # 数据获取
│   │   ├── indicator_calculator.py # 指标计算
│   │   └── sentiment_calculator.py # 情绪计算
│   ├── tasks/               # 定时任务
│   │   └── scheduler.py     # 任务调度
│   └── websocket/           # WebSocket实时推送
│       └── manager.py       # 连接管理
├── requirements.txt         # Python依赖
└── .env.example            # 环境配置示例
```

## 快速开始

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

根据需要修改 `.env` 中的配置：
- `DATABASE_URL`: 数据库连接字符串
- `UPDATE_SCHEDULE_TIME`: 数据更新时间（默认16:00）
- `DEBUG`: 调试模式（True/False）

### 3. 启动应用

```bash
python -m app.main
```

服务将启动在 `http://localhost:8000`

## API文档

启动后访问以下地址查看API文档：
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## 核心功能说明

### 1. K线数据管理

**数据库表**: `kline_data`

**API端点**:
- `GET /api/kline/list` - 查询K线数据（可按日期范围筛选）
- `GET /api/kline/{code}/latest` - 获取最新K线
- `GET /api/kline/date/{date}` - 查询特定日期的K线
- `GET /api/kline/{code}/statistics` - 获取统计数据

### 2. 大盘情绪监控

**数据库表**: `sentiment_data`

**计算指标**:
- 上涨/下跌家数
- 平均涨跌幅
- 成交额
- 涨幅5%+家数
- 跌幅5%+家数
- 涨跌停家数
- **综合情绪评分** (0-100)
  - 极度悲观 (< 20)
  - 悲观 (20-35)
  - 中性偏悲观 (35-45)
  - 中性 (45-55)
  - 中性偏乐观 (55-65)
  - 乐观 (65-80)
  - 极度乐观 (>= 80)

**API端点**:
- `GET /api/sentiment/today` - 获取今日情绪
- `GET /api/sentiment/history` - 历史情绪数据
- `GET /api/sentiment/date/{date}` - 特定日期情绪
- `GET /api/sentiment/analysis/trend` - 情绪趋势
- `GET /api/sentiment/analysis/extremes` - 情绪极值

### 3. 技术指标

支持计算：
- 移动平均线 (MA): 5, 10, 20, 50, 100, 200周期
- MACD: DIF, DEA, HISTOGRAM
- RSI: 6, 12, 24周期
- 布林带 (Bollinger Bands)
- ATR (真实波幅)

### 4. 定时任务

**每日下午4点执行**:
1. 获取所有股票的最新K线
2. 计算技术指标
3. 生成大盘情绪数据

**实时更新** (可选):
- 每5分钟在交易时间（9:30-15:00）更新实时数据

### 5. WebSocket 实时推送

连接到 `ws://localhost:8000/ws/market` 可接收：

```json
{
  "type": "kline_update",
  "timestamp": "2024-12-31T16:00:00",
  "code": "sh000001",
  "data": { ... }
}
```

```json
{
  "type": "sentiment_update",
  "timestamp": "2024-12-31T16:00:00",
  "data": { 
    "up_count": 1234,
    "down_count": 1234,
    "sentiment_score": 65.5,
    ...
  }
}
```

## 数据源配置

在 `.env` 中配置 `DATA_SOURCE_TYPE`：

### 本地数据源 (local)
- 支持从同花顺、通达信等本地文件读取
- 在 `./data` 目录下放置CSV文件
- 文件格式: `{code}_{period}.csv`

### 在线API (api)
- 腾讯财经 (tencent)
- 东方财富 (eastmoney)

### 网页爬取 (web_scrape)
- 使用BeautifulSoup爬取新浪财经等

## 数据库初始化

启动时自动创建数据库表。如需手动初始化：

```python
from app.database import init_db
init_db()
```

## 生产部署

### 使用Gunicorn

```bash
pip install gunicorn

gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --access-logfile logs/access.log \
  --error-logfile logs/error.log
```

### 使用Docker

```bash
docker build -t stockpy:latest .
docker run -p 8000:8000 stockpy:latest
```

## 配置说明

### 情绪指标权重

可在 `config.py` 中自定义：

```python
sentiment_weights = {
    "up_count": 0.2,           # 上涨家数权重
    "down_count": 0.2,         # 下跌家数权重
    "avg_change": 0.15,        # 平均涨跌幅权重
    "turnover_rate": 0.15,     # 成交额权重
    "up_5_percent": 0.1,       # 涨幅5%权重
    "down_5_percent": 0.1,     # 跌幅5%权重
    "limit_up": 0.05,          # 涨停权重
    "limit_down": 0.05,        # 跌停权重
}
```

## 日志

日志文件位置：
- 标准输出：控制台（DEBUG级别）
- 文件日志：`logs/` 目录下（INFO及以上级别）

## 性能优化

- 使用数据库索引加速查询
- K线数据缓存关键指标
- 异步I/O操作
- 连接池管理

## 扩展功能规划

- [ ] 通达信/同花顺接口集成
- [ ] 更多技术指标(KDJ, BOLL等)
- [ ] 异常告警机制
- [ ] 用户认证和权限管理
- [ ] 数据导出功能(CSV, Excel)
- [ ] 高级回测引擎
- [ ] 策略框架

## 常见问题

**Q: 数据库选择？**
A: 开发环境使用SQLite，生产环境建议使用MySQL或PostgreSQL

**Q: 如何自定义数据源？**
A: 在 `data_fetcher.py` 中继承 `DataFetcher` 类实现自己的数据源

**Q: 如何修改更新时间和权重？**
A: 修改 `.env` 文件中的 `UPDATE_SCHEDULE_TIME` 和 `config.py` 中的 `sentiment_weights`

## 开发指南

### 添加新的API接口

1. 在 `app/api/` 目录下创建新文件（如 `new_feature.py`）
2. 定义FastAPI路由
3. 在 `app/api/__init__.py` 中注册路由

### 添加新的业务服务

1. 在 `app/services/` 目录下创建新文件
2. 实现业务逻辑
3. 在API路由中调用

### 添加新的定时任务

1. 在 `app/tasks/scheduler.py` 中定义任务方法
2. 在 `start()` 方法中注册任务

## 依赖文件

- `requirements.txt`: Python包依赖文件

## 许可证

MIT License

## 支持

有问题或建议，欢迎提交Issue或Pull Request。

---

**版本**: 0.1.0
**最后更新**: 2024年12月
**开发状态**: 积极开发中 🚧
