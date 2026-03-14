# StockPy - 大盘情绪监控系统

一个集成实时市场数据、技术分析、自动回测和情绪监控的A股量化交易平台。

## 🎯 核心功能

### 已实现功能
- ✅ A股K线数据管理（多周期）
- ✅ 大盘情绪指标计算
- ✅ 定时数据更新任务
- ✅ 技术指标分析框架
- ✅ 回测系统架构

### 计划功能
- [ ] 接入真实市场数据API（腾讯财经、东方财富）
- [ ] 添加技术指标分析（MA、MACD、RSI等）
- [ ] 实现告警机制（情绪突变提醒）
- [ ] 支持自定义指标权重
- [ ] WebSocket 实时数据推送
- [ ] 用户认证和权限管理
- [ ] 数据导出（CSV、Excel）
- [ ] 高级图表分析

## 🏗️ 项目架构

```
backend (Python FastAPI)
  ├─ 数据采集层: 同花顺、通达信、网页爬取
  ├─ 数据处理层: K线处理、指标计算
  ├─ 业务逻辑层: 情绪监控、回测引擎
  ├─ 定时任务: APScheduler (下午4点更新)
  └─ 实时推送: WebSocket

database (SQLite/MySQL)
  ├─ 股票基础信息
  ├─ K线数据
  ├─ 大盘情绪数据
  └─ 回测结果

frontend (React/Vue)
  ├─ K线展示
  ├─ 情绪指标仪表板
  ├─ 实时数据更新
  └─ 回测结果分析
```

## 🚀 快速开始

### 环境要求
- Python 3.9+
- Node.js 14+ （前端开发）
- SQLite 或 MySQL

### 后端安装

```bash
cd backend
pip install -r requirements.txt
python -m app.main
```

### 前端安装

```bash
cd frontend
npm install
npm run dev
```

## 📊 大盘情绪指标说明

系统每日计算以下情绪指标：

| 指标 | 说明 |
|------|------|
| 上涨家数 | 当日上涨的股票数量 |
| 下跌家数 | 当日下跌的股票数量 |
| 涨跌幅均值 | 全市场平均涨跌幅 |
| 成交额 | 沪深两市总成交额 |
| 涨幅5%+ | 涨幅超过5%的股票数 |
| 跌幅5%+ | 跌幅超过5%的股票数 |
| 涨跌停 | 一字涨停或跌停股票数 |

## 🔗 数据源规划

- **本地数据源**: 同花顺、通达信（实时行情）
- **在线API**: 腾讯财经、东方财富、新浪财经
- **网页爬取**: BeautifulSoup/Selenium（备选）

## 📝 文件说明

- `database/`: 数据库初始化脚本
- `backend/`: Python FastAPI后端
  - `app/main.py`: 应用入口
  - `app/config.py`: 配置管理
  - `app/services/`: 业务逻辑
  - `app/tasks/`: 定时任务
  - `app/api/`: API接口定义
- `frontend/`: Web前端代码

## 💡 使用示例

### 查询K线数据
```bash
curl http://localhost:8000/api/kline/sh000001?period=D&limit=100
```

### 获取今日情绪数据
```bash
curl http://localhost:8000/api/sentiment/today
```

### 启动回测
```bash
curl -X POST http://localhost:8000/api/backtest/start \
  -H "Content-Type: application/json" \
  -d '{"strategy":"macd","start_date":"2024-01-01"}'
```

## 🔒 配置管理

复制 `backend/.env.example` 为 `.env`，配置：

```
DATABASE_URL=sqlite:///./stockpy.db
API_TENCENT_KEY=your_key
API_EASTMONEY_KEY=your_key
UPDATE_TIME=16:00
```

## 📚 技术栈

### 后端
- FastAPI: Web框架
- SQLAlchemy: ORM
- APScheduler: 定时任务
- Pandas: 数据处理
- TA-Lib: 技术指标（可选）

### 前端
- React / Vue.js
- ECharts: 图表库
- Axios: HTTP客户端
- WebSocket: 实时通信

### 数据库
- SQLite: 开发环境
- MySQL: 生产环境

## 🔄 更新流程

```
每日下午4点触发
  ↓
获取最新行情数据（同花顺/通达信）
  ↓
处理K线数据
  ↓
计算技术指标
  ↓
生成大盘情绪数据
  ↓
保存到数据库
  ↓
WebSocket推送到前端
```

## 📞 支持

如有问题，请提交Issue或联系开发团队。

## 📄 License

MIT License

---

**开发中** 🚧 持续完善功能中...
