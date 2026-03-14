# 🚀 StockPy 快速启动指南

一个集成实时市场数据、技术分析、自动回测和情绪监控的A股量化交易平台。

---

## ⚡ 5分钟快速开始

### Windows用户

1. **运行启动脚本**
   ```bash
   run.bat
   ```
   脚本将自动处理所有初始化操作

2. **访问应用**
   - 后端API: http://localhost:8000
   - API文档: http://localhost:8000/docs

### macOS/Linux用户

1. **运行启动脚本**
   ```bash
   chmod +x run.sh
   ./run.sh
   ```

2. **访问应用**
   - 后端API: http://localhost:8000
   - API文档: http://localhost:8000/docs

### 跨平台（Python）

1. **安装依赖**
   ```bash
   cd backend
   pip install -r requirements.txt
   ```

2. **配置环境**
   ```bash
   cp .env.example .env
   # 根据需要编辑.env文件
   ```

3. **启动服务**
   ```bash
   python -m uvicorn app.main:app --reload
   ```

4. **访问应用**
   - 后端: http://localhost:8000
   - API文档: http://localhost:8000/docs
   - 前端: 在浏览器中打开 `frontend/src/index.html`

---

## 📋 项目结构

```
StockPy/
├── backend/                    # Python FastAPI后端
│   ├── app/
│   │   ├── main.py            # 应用入口
│   │   ├── config.py          # 配置管理
│   │   ├── database.py        # 数据库连接
│   │   ├── api/               # API接口
│   │   ├── models/            # 数据模型
│   │   ├── services/          # 业务逻辑
│   │   ├── tasks/             # 定时任务
│   │   └── websocket/         # WebSocket
│   ├── requirements.txt
│   └── .env.example
├── frontend/                   # 前端应用
│   └── src/
│       ├── index.html         # 主页
│       ├── css/style.css      # 样式
│       └── js/app.js          # 脚本
├── database/
│   └── init.sql               # 数据库初始化脚本
├── docker-compose.yml         # Docker编排
├── README.md                  # 项目说明
└── run.{py,sh,bat}           # 启动脚本
```

---

## 🎯 核心功能

### ✅ 已实现

- **📊 K线数据管理**
  - 支持多周期（日/周/月）
  - 数据库持久化
  - REST API查询

- **💡 大盘情绪监控**
  - 上涨/下跌家数统计
  - 平均涨跌幅计算
  - 特殊涨跌统计（涨幅5%、跌停等）
  - **综合情绪评分** (0-100分)
    - 极度乐观 (≥80)
    - 乐观 (65-80)
    - 中性偏乐观 (55-65)
    - 中性 (45-55)
    - 中性偏悲观 (35-45)
    - 悲观 (20-35)
    - 极度悲观 (<20)

- **🔧 技术指标分析**
  - 移动平均线 (MA): 5, 10, 20, 50, 100, 200日
  - MACD: DIF, DEA, HISTOGRAM
  - RSI: 6, 12, 24周期
  - 布林带 (Bollinger Bands)
  - ATR (真实波幅)

- **⏰ 定时任务**
  - 每日下午4点自动更新数据
  - 自动计算情绪指标
  - 可配置更新时间

- **🌐 Web前端**
  - 响应式设计
  - 实时数据展示
  - 交互式图表（ECharts）
  - WebSocket实时推送

- **📡 WebSocket支持**
  - 实时数据推送
  - 自动重连机制
  - 心跳保活

### 📋 成员功能（快速实现路线）

#### 第一阶段：数据源集成
- [ ] 接入腾讯财经API
- [ ] 接入东方财富API
- [ ] 实现网页爬取（BeautifulSoup）
- [ ] 本地文件导入支持

#### 第二阶段：交易系统
- [ ] 实现回测引擎
- [ ] 策略框架
- [ ] 月度收益计算
- [ ] 风险指标（最大回撤、夏普比率等）

#### 第三阶段：高级功能
- [ ] 实时告警机制
- [ ] 自定义指标权重
- [ ] 用户认证和权限
- [ ] 数据导出（CSV、Excel）

#### 第四阶段：性能优化
- [ ] Redis缓存层
- [ ] 数据库优化
- [ ] 性能监控
- [ ] 日志系统完善

---

## 🔌 API 端点

### 健康检查
```bash
GET /api/health/check
GET /api/health/info
```

### K线数据
```bash
GET /api/kline/list                  # 查询K线
GET /api/kline/{code}/latest         # 最新K线
GET /api/kline/date/{date}           # 日期K线
GET /api/kline/{code}/statistics     # 统计数据
```

### 大盘情绪
```bash
GET /api/sentiment/today             # 今日情绪
GET /api/sentiment/history           # 历史数据
GET /api/sentiment/date/{date}       # 日期情绪
GET /api/sentiment/analysis/trend    # 趋势分析
GET /api/sentiment/analysis/extremes # 极值分析
```

### WebSocket
```
ws://localhost:8000/ws/market
```

---

## ⚙️ 环境配置

编辑 `backend/.env` 文件：

```ini
# 数据库
DATABASE_URL=sqlite:///./stockpy.db
# DATABASE_URL=mysql+pymysql://user:pass@localhost/stockpy

# API密钥
TENCENT_API_KEY=your_key
EASTMONEY_API_KEY=your_key

# 定时任务
UPDATE_SCHEDULE_TIME=16:00           # 下午4点
UPDATE_TIMEZONE=Asia/Shanghai

# 应用
DEBUG=True
HOST=0.0.0.0
PORT=8000

# 数据源
DATA_SOURCE_TYPE=local               # local/api/web_scrape
LOCAL_DATA_PATH=./data
```

---

## 🐳 Docker部署

### 快速启动

```bash
# 构建并启动所有服务
docker-compose up -d

# 查看服务状态
docker-compose ps

# 查看日志
docker-compose logs -f backend
```

### 服务清单

- **MySQL** (port 3306): 数据存储
- **Redis** (port 6379): 缓存（可选）
- **FastAPI** (port 8000): 后端API
- **Nginx** (port 80): 前端和代理

### 停止服务

```bash
docker-compose down

# 清理卷
docker-compose down -v
```

---

## 📊 数据库

### 表结构

- **stock**: 股票基础信息
- **kline_data**: K线数据
- **sentiment_data**: 大盘情绪数据
- **technical_indicator**: 技术指标
- **backtest_result**: 回测结果

### 初始化

```bash
# SQLite（自动）
python -c "from app.database import init_db; init_db()"

# MySQL（需手动导入）
mysql -u user -p database < database/init.sql
```

---

## 🔗 本地数据源

将数据文件放在 `backend/data/` 目录：

```
data/
├── sh000001_D.csv         # 上证指数日线
├── sz000858_W.csv         # 五粮液周线
└── stocks.csv             # 股票列表
```

**CSV格式示例**:
```
date,code,open_price,high_price,low_price,close_price,volume,amount,change_percent
2024-01-01,sh000001,3000,3100,2950,3050,1000000,50000000000,1.67
```

---

## 🧪 测试

### 单元测试（规划中）

```bash
pytest tests/ -v
```

### API测试

```bash
# 测试健康检查
curl http://localhost:8000/api/health/check

# 测试K线查询
curl "http://localhost:8000/api/kline/list?code=sh000001&period=D&limit=10"

# 测试情绪数据
curl http://localhost:8000/api/sentiment/today
```

---

## 🚨 故障排除

### 后端无法启动

```bash
# 1. 检查Python版本
python --version          # 需要3.9+

# 2. 清除旧的包
pip install --upgrade pip setuptools

# 3. 重新安装依赖
pip install -r requirements.txt

# 4. 检查数据库
sqlite3 stockpy.db "SELECT 1;"
```

### 无法连接到数据库

```bash
# SQLite
ls -la stockpy.db

# MySQL
mysql -h localhost -u stockpy -p stockpy
```

### 前端无法加载

- 确保后端API已启动 (http://localhost:8000)
- 检查浏览器控制台错误信息
- 清除浏览器缓存 (Ctrl+Shift+Delete)

### WebSocket连接失败

- 检查后端服务是否运行
- 查看防火墙设置
- 检查代理配置

---

## 📚 文档导航

- [后端README](./backend/README.md) - 后端详细文档
- [前端README](./frontend/README.md) - 前端详细文档
- [API文档](http://localhost:8000/docs) - 交互式API文档
- [主README](./README.md) - 项目总体介绍

---

## 🔐 生产部署

### Nginx配置

编辑 `nginx.conf` 并根据需要调整

### SSL/TLS

```bash
# 生成自签名证书（测试用）
openssl req -x509 -newkey rsa:4096 -nodes \
  -out server.crt -keyout server.key -days 365
```

### 监控和日志

```bash
# 查看日志
tail -f logs/app.log

# 系统监控
docker stats
```

---

## 💡 常见问题

**Q: 能否在Windows上运行？**
A: 是的，运行 `run.bat` 即可。

**Q: 如何添加自己的数据源？**
A: 继承 `DataFetcher` 类并在 `data_fetcher.py` 中实现。

**Q: 如何修改情绪指标权重？**
A: 编辑 `.env` 中的 `sentiment_weights` 或 `config.py`。

**Q: 生产环境推荐什么数据库？**
A: MySQL 8.0+ 或 PostgreSQL 12+。

**Q: 如何实时更新数据？**
A: 使用WebSocket连接或配置实时任务（每5分钟）。

---

## 📞 支持和贡献

- 提交Issue报告问题
- Pull Request欢迎
- 讨论新功能建议

---

## 📄 许可证

MIT License

---

## 🎉 致谢

感谢所有技术文档和开源社区的支持！

---

**当前版本**: 0.1.0  
**最后更新**: 2024年12月  
**开发状态**: 积极开发中 🚧

---

**现在就开始运行吧！** 👉 `./run.sh` 或 `run.bat`
