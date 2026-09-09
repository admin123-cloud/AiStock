# AiStock - 量化交易AI系统

一个基于Python的量化交易系统，集成了机器学习、深度学习和强化学习模型，支持多种数据源、交易策略和回测功能。

## 项目结构

```
aistock/
│
├── data/                          # 数据层
│   ├── raw/                       # 原始数据（未处理）
│   ├── processed/                 # 处理后数据
│   └── cache/                     # 缓存数据
│
├── models/                        # AI模型层
│   ├── lstm_model.py              # LSTM预测模型
│   ├── transformer_model.py       # Transformer模型
│   ├── reinforcement_model.py     # 强化学习模型
│   ├── ensemble.py                # 模型集成
│   └── model_utils.py             # 模型工具函数
│
├── strategies/                    # 策略层
│   ├── base_strategy.py           # 策略基类
│   ├── momentum_strategy.py       # 动量策略
│   ├── mean_reversion.py          # 均值回归
│   ├── ml_strategy.py             # 机器学习策略
│   └── strategy_utils.py          # 策略工具
│
├── backtest/                      # 回测层
│   ├── backtest_engine.py         # 回测引擎核心
│   ├── performance.py             # 绩效评估
│   ├── risk_analysis.py           # 风险分析
│   └── visualization.py           # 可视化结果
│
├── execution/                     # 执行层
│   ├── broker.py                  # 券商接口
│   ├── order_manager.py           # 订单管理
│   ├── position_manager.py        # 仓位管理
│   └── risk_controller.py         # 风控模块
│
├── data_fetcher/                  # 数据获取
│   ├── base_fetcher.py            # 数据源基类
│   ├── tdx_fetcher.py             # pytdx数据源
│   ├── ak_fetcher.py              # akshare数据源
│   ├── eastmoney.py               # 东方财富数据源
│   ├── tencent.py                 # 腾讯数据源
│   ├── tickflow.py                # TickFlow数据源
│   ├── websocket_fetcher.py       # 实时行情
│   └── data_cleaner.py            # 数据清洗
│
├── features/                      # 特征工程
│   ├── technical_features.py      # 技术指标特征
│   ├── fundamental_features.py    # 基本面特征
│   ├── alternative_features.py    # 另类数据特征
│   └── feature_selector.py        # 特征选择
│
├── config/                        # 配置管理
│   ├── settings.py                # 全局配置
│   ├── settings.yaml              # YAML配置文件
│   └── data_sources.yaml          # 数据源配置
│
├── utils/                         # 工具函数
│   ├── logger.py                  # 日志管理
│   ├── config.py                  # 配置管理
│   ├── database.py               # 数据库工具
│   ├── helpers.py                # 通用工具
│   ├── decorators.py             # 装饰器（重试、计时）
│   └── exceptions.py             # 自定义异常
│
├── tests/                         # 测试
│   ├── test_data.py               # 数据层测试
│   ├── test_models.py             # 模型测试
│   ├── test_strategies.py         # 策略测试
│   └── test_backtest.py           # 回测测试
│
├── notebooks/                     # 研究笔记
│   ├── 01_data_exploration.ipynb  # 数据探索
│   ├── 02_feature_engineering.ipynb # 特征工程
│   ├── 03_model_training.ipynb    # 模型训练
│   └── 04_backtest_analysis.ipynb  # 回测分析
│
├── scripts/                       # 运维脚本
│   ├── run_backtest.py            # 运行回测
│   ├── live_trading.py            # 实盘交易
│   ├── update_data.py             # 更新数据
│   └── cron_jobs.py               # 定时任务
│
├── logs/                          # 日志文件
│   ├── backtest.log               # 回测日志
│   ├── trading.log                # 交易日志
│   └── error.log                  # 错误日志
│
├── reports/                       # 报告输出
│   ├── backtest_results/          # 回测结果
│   ├── performance_reports/       # 绩效报告
│   └── figures/                   # 图表输出
│
├── frontend/                      # 前端界面
│   ├── src/                       # 源代码
│   ├── public/                    # 静态资源
│   └── package.json              # 前端依赖
│
├── requirements.txt               # Python依赖
├── setup.py                       # 项目安装
├── .env.example                   # 环境变量示例
├── .gitignore                     # Git忽略文件
└── README.md                      # 项目说明
```

## 功能特性

### 数据获取
- 支持多种数据源：通达信、东方财富、腾讯、Tushare等
- 实时行情数据获取
- 历史K线数据获取
- 数据清洗和预处理

### AI模型
- LSTM时间序列预测模型
- Transformer模型
- 强化学习模型
- 模型集成和优化

### 交易策略
- 动量策略
- 均值回归策略
- 机器学习策略
- 自定义策略开发

### 回测系统
- 完整的回测引擎
- 绩效评估指标
- 风险分析工具
- 可视化报告

### 执行系统
- 券商接口集成
- 订单管理
- 仓位管理
- 风险控制

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置环境

```bash
cp .env.example .env
# 编辑.env文件，填入必要的配置信息
```

### 运行示例

#### 更新数据
```bash
python scripts/update_data.py
```


#### 启动定时任务
```bash
python scripts/cron_jobs.py
```

## 开发指南

### 添加新的数据源

1. 在 `data_fetcher/` 目录下创建新的数据源文件
2. 继承 `BaseDataSource` 类
3. 实现必要的方法
4. 在 `data_fetcher/__init__.py` 中注册

### 开发新的策略

1. 在 `strategies/` 目录下创建策略文件
2. 继承 `BaseStrategy` 类
3. 实现 `generate_signals` 方法
4. 在 `strategies/__init__.py` 中注册

### 训练自定义模型

1. 在 `models/` 目录下创建模型文件
2. 定义模型架构
3. 实现训练和预测方法
4. 在 `scripts/train_model.py` 中调用

## 测试

```bash
# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_data.py

# 运行测试并生成覆盖率报告
pytest --cov=. --cov-report=html
```

## 文档

详细的文档请查看 `docs/` 目录。

## 贡献

欢迎提交Issue和Pull Request！

## 许可证

MIT License

## 联系方式

- 项目主页：https://github.com/yourusername/aistock
- 邮箱：contact@aistock.com

## 免责声明

本系统仅供学习和研究使用，不构成任何投资建议。使用本系统进行实盘交易的风险由使用者自行承担。