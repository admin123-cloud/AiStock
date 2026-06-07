# 项目重构说明

## 概述

本文档说明了 AiStock 项目的重构过程和新的目录结构。重构的目标是创建一个更加清晰、模块化和可维护的项目结构。

## 重构时间

2026-03-28

## 重构目标

1. **模块化设计**：将功能按照业务逻辑分层，提高代码的可维护性
2. **清晰的目录结构**：按照功能模块组织代码，便于查找和扩展
3. **标准化命名**：统一命名规范，提高代码可读性
4. **完整的测试覆盖**：为每个模块提供相应的测试文件
5. **完善的文档**：提供详细的使用说明和开发文档

## 新旧结构对比

### 旧结构（backend 目录）

```
backend/
├── src/
│   ├── api/                    # API接口
│   ├── core/                   # 核心模块
│   ├── data/                   # 数据目录
│   ├── data_sources/           # 数据源
│   ├── models/                 # 数据模型
│   ├── scheduler/              # 调度器
│   ├── services/               # 服务层
│   └── utils/                  # 工具函数
├── config/                     # 配置文件
├── scripts/                    # 脚本文件
└── tests/                      # 测试文件
```

### 新结构（根目录）

```
aistock/
├── data/                       # 数据层
├── models/                     # AI模型层
├── strategies/                 # 策略层
├── backtest/                   # 回测层
├── execution/                  # 执行层
├── data_fetcher/               # 数据获取
├── features/                   # 特征工程
├── config/                     # 配置管理
├── utils/                      # 工具函数
├── tests/                      # 测试
├── notebooks/                  # 研究笔记
├── scripts/                    # 运维脚本
├── logs/                       # 日志文件
├── reports/                    # 报告输出
├── frontend/                   # 前端界面
├── requirements.txt            # Python依赖
├── setup.py                    # 项目安装
├── .env.example               # 环境变量示例
├── .gitignore                 # Git忽略文件
└── README.md                  # 项目说明
```

## 目录结构详解

### 1. data/ - 数据层

```
data/
├── raw/                       # 原始数据（未处理）
├── processed/                 # 处理后数据
└── cache/                     # 缓存数据
```

**用途**：存储各种类型的数据文件

**说明**：
- `raw/`：存储从数据源获取的原始数据
- `processed/`：存储经过清洗和预处理的数据
- `cache/`：存储临时缓存数据，提高数据访问速度

### 2. models/ - AI模型层

```
models/
├── lstm_model.py              # LSTM预测模型
├── transformer_model.py       # Transformer模型
├── reinforcement_model.py     # 强化学习模型
├── ensemble.py                # 模型集成
├── model_utils.py             # 模型工具函数
└── stock_models.py           # 数据模型
```

**用途**：包含所有机器学习和深度学习模型

**说明**：
- 提供多种AI模型用于股票价格预测
- 包含模型训练、预测和评估功能
- 支持模型集成和优化

### 3. strategies/ - 策略层

```
strategies/
├── base_strategy.py           # 策略基类
├── momentum_strategy.py       # 动量策略
├── mean_reversion.py          # 均值回归
├── ml_strategy.py             # 机器学习策略
└── strategy_utils.py          # 策略工具
```

**用途**：包含各种交易策略

**说明**：
- 提供策略基类，便于开发自定义策略
- 包含多种经典和机器学习策略
- 支持策略回测和实盘交易

### 4. backtest/ - 回测层

```
backtest/
├── backtest_engine.py         # 回测引擎核心
├── performance.py             # 绩效评估
├── risk_analysis.py           # 风险分析
└── visualization.py           # 可视化结果
```

**用途**：提供完整的回测系统

**说明**：
- 支持历史数据回测
- 提供详细的绩效评估指标
- 包含风险分析和可视化功能

### 5. execution/ - 执行层

```
execution/
├── broker.py                  # 券商接口
├── order_manager.py           # 订单管理
├── position_manager.py        # 仓位管理
└── risk_controller.py         # 风控模块
```

**用途**：提供实盘交易执行功能

**说明**：
- 支持多种券商接口
- 提供订单和仓位管理
- 包含风险控制模块

### 6. data_fetcher/ - 数据获取

```
data_fetcher/
├── base_fetcher.py            # 数据源基类
├── tdx_fetcher.py             # pytdx数据源
├── ak_fetcher.py              # akshare数据源
├── eastmoney.py               # 东方财富数据源
├── tencent.py                 # 腾讯数据源
├── tickflow.py                # TickFlow数据源
├── websocket_fetcher.py       # 实时行情
└── data_cleaner.py            # 数据清洗
```

**用途**：提供多种数据源的数据获取功能

**说明**：
- 支持多种数据源：通达信、东方财富、腾讯、Tushare等
- 提供实时和历史数据获取
- 包含数据清洗和预处理功能

### 7. features/ - 特征工程

```
features/
├── technical_features.py      # 技术指标特征
├── fundamental_features.py    # 基本面特征
├── alternative_features.py    # 另类数据特征
└── feature_selector.py        # 特征选择
```

**用途**：提供特征提取和工程功能

**说明**：
- 计算各种技术指标特征
- 提取基本面特征
- 支持另类数据特征和特征选择

### 8. config/ - 配置管理

```
config/
├── settings.py                # 全局配置
├── settings.yaml              # YAML配置文件
└── data_sources.yaml          # 数据源配置
```

**用途**：管理项目配置

**说明**：
- 集中管理所有配置项
- 支持多种配置格式
- 便于环境切换和部署

### 9. utils/ - 工具函数

```
utils/
├── logger.py                  # 日志管理
├── config.py                  # 配置管理
├── database.py               # 数据库工具
├── helpers.py                # 通用工具
├── decorators.py             # 装饰器（重试、计时）
└── exceptions.py             # 自定义异常
```

**用途**：提供通用工具函数和类

**说明**：
- 提供日志、配置、数据库等基础功能
- 包含常用装饰器和异常类
- 提供各种辅助函数

### 10. tests/ - 测试

```
tests/
├── test_data.py               # 数据层测试
├── test_models.py             # 模型测试
├── test_strategies.py         # 策略测试
└── test_backtest.py           # 回测测试
```

**用途**：包含所有测试文件

**说明**：
- 为每个模块提供对应的测试文件
- 使用 pytest 测试框架
- 支持测试覆盖率报告

### 11. notebooks/ - 研究笔记

```
notebooks/
├── 01_data_exploration.ipynb  # 数据探索
├── 02_feature_engineering.ipynb # 特征工程
├── 03_model_training.ipynb    # 模型训练
└── 04_backtest_analysis.ipynb  # 回测分析
```

**用途**：Jupyter Notebook 研究笔记

**说明**：
- 用于数据分析和模型研究
- 包含数据探索、特征工程、模型训练等内容
- 便于交互式开发和调试

### 12. scripts/ - 运维脚本

```
scripts/
├── run_backtest.py            # 运行回测
├── live_trading.py            # 实盘交易
├── update_data.py             # 更新数据
└── cron_jobs.py               # 定时任务
```

**用途**：提供各种运维和运行脚本

**说明**：
- 包含数据更新、回测等脚本
- 支持定时任务调度
- 便于自动化运维

### 13. logs/ - 日志文件

```
logs/
├── backtest.log               # 回测日志
├── trading.log                # 交易日志
└── error.log                  # 错误日志
```

**用途**：存储各种日志文件

**说明**：
- 按功能分类存储日志
- 支持日志轮转和压缩
- 便于问题排查和系统监控

### 14. reports/ - 报告输出

```
reports/
├── backtest_results/          # 回测结果
├── performance_reports/       # 绩效报告
└── figures/                   # 图表输出
```

**用途**：存储各种报告和图表

**说明**：
- 存储回测结果和绩效报告
- 保存可视化图表
- 便于结果分析和展示

### 15. frontend/ - 前端界面

```
frontend/
├── src/                       # 源代码
├── public/                    # 静态资源
└── package.json              # 前端依赖
```

**用途**：前端界面代码

**说明**：
- 提供用户界面
- 支持数据可视化和交互
- 独立的前端项目

## 迁移指南

### 从旧结构迁移到新结构

1. **数据源模块**
   - 从 `backend/src/data_sources/` 迁移到 `data_fetcher/`
   - 重命名文件以符合新规范

2. **工具模块**
   - 从 `backend/src/utils/` 迁移到 `utils/`
   - 添加新的装饰器和异常类

3. **配置文件**
   - 从 `backend/config/` 迁移到 `config/`
   - 更新配置路径

4. **数据模型**
   - 从 `backend/src/models/` 迁移到 `models/`
   - 重命名为 `stock_models.py`

5. **脚本文件**
   - 从 `backend/scripts/` 迁移到 `scripts/`
   - 更新导入路径

6. **测试文件**
   - 从 `backend/tests/` 迁移到 `tests/`
   - 创建新的测试文件

## 导入路径更新

### 旧导入路径
```python
from src.data_sources import TdxDataSource
from src.utils import get_logger
from src.models import Stock
```

### 新导入路径
```python
from data_fetcher import TdxDataSource
from utils import get_logger
from models import Stock
```

## 配置文件更新

### 日志配置
- 日志轮转大小从 10 MB 调整为 100 MB
- 日志目录更新为 `logs/`

### 数据源配置
- 更新数据源配置文件路径
- 调整缓存目录为 `data/cache/`

## 兼容性说明

### 向后兼容
- 保留 `backend/` 目录结构，确保现有代码可以继续运行
- 提供过渡期，逐步迁移到新结构

### 弃用警告
- 旧的导入路径将显示弃用警告
- 建议尽快更新到新的导入路径

## 未来规划

1. **完善模块实现**
   - 实现所有计划中的模块
   - 添加更多示例和文档

2. **性能优化**
   - 优化数据获取和处理性能
   - 提高模型训练和预测速度

3. **功能扩展**
   - 添加更多数据源
   - 支持更多交易策略
   - 增强风控功能

4. **测试覆盖**
   - 提高测试覆盖率
   - 添加集成测试

5. **文档完善**
   - 添加API文档
   - 提供更多使用示例

## 联系方式

如有问题或建议，请联系：
- 邮箱：contact@aistock.com
- 项目主页：https://github.com/yourusername/aistock

## 更新日志

### 2026-03-28
- 完成项目结构重构
- 创建新的目录结构
- 迁移现有代码到新结构
- 更新配置文件
- 添加测试文件和脚本
- 创建项目文档