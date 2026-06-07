# 项目结构迁移指南

## 当前结构 → 推荐结构映射

### 1. API层重构

#### 当前结构
```
backend/src/api/
├── stocks.py
├── sectors.py
├── market_overview.py
└── watchlist.py
```

#### 推荐结构
```
backend/src/api/
├── v1/
│   ├── endpoints/
│   │   ├── stocks.py          # 股票相关端点
│   │   ├── sectors.py         # 板块相关端点
│   │   ├── market.py         # 市场相关端点
│   │   └── watchlist.py      # 自选股相关端点
│   ├── router.py             # 路由注册
│   └── __init__.py
├── dependencies.py          # 依赖注入（数据库、缓存等）
└── middleware.py           # 中间件（认证、日志等）
```

#### 迁移步骤
1. 创建 `backend/src/api/v1/` 目录
2. 将现有API文件移动到 `endpoints/` 子目录
3. 创建 `router.py` 统一管理路由
4. 提取依赖到 `dependencies.py`
5. 添加中间件到 `middleware.py`

### 2. 核心业务层重构

#### 当前结构
```
backend/src/
├── models/stock.py
├── services/stock_service.py
└── data_sources/ (数据源)
```

#### 推荐结构
```
backend/src/core/
├── domain/
│   ├── entities/             # 领域实体
│   │   ├── stock.py
│   │   ├── kline.py
│   │   └── sector.py
│   ├── value_objects/        # 值对象
│   │   ├── price.py
│   │   └── volume.py
│   └── repositories/        # 仓储接口
│       ├── stock_repository.py
│       └── kline_repository.py
├── use_cases/              # 用例层
│   ├── stock/
│   │   ├── get_stock_info.py
│   │   ├── sync_stock_data.py
│   │   └── get_stock_kline.py
│   ├── market/
│   │   └── get_market_overview.py
│   └── data_sync/
│       ├── full_sync.py
│       └── incremental_sync.py
└── exceptions.py           # 业务异常定义
```

#### 迁移步骤
1. 将 `models/` 重构为 `core/domain/entities/`
2. 提取业务逻辑到 `core/use_cases/`
3. 定义仓储接口到 `core/domain/repositories/`
4. 统一异常处理到 `core/exceptions.py`

### 3. 基础设施层重构

#### 当前结构
```
backend/src/
├── data_sources/           # 数据源
├── utils/database.py       # 数据库工具
├── utils/logger.py         # 日志工具
└── services/data_storage.py # 数据存储
```

#### 推荐结构
```
backend/src/infrastructure/
├── database/
│   ├── models/            # ORM模型
│   │   ├── stock.py
│   │   ├── kline.py
│   │   └── sector.py
│   ├── repositories/      # 仓储实现
│   │   ├── stock_repository.py
│   │   ├── kline_repository.py
│   │   └── sector_repository.py
│   └── migrations/       # 数据库迁移
│       ├── versions/
│       └── env.py
├── cache/
│   ├── redis.py          # Redis缓存实现
│   └── memory.py        # 内存缓存实现
├── messaging/
│   ├── kafka.py         # Kafka消息队列
│   └── rabbitmq.py     # RabbitMQ消息队列
├── external/
│   ├── data_sources/    # 外部数据源
│   │   ├── pytdx/
│   │   │   ├── client.py
│   │   │   └── parser.py
│   │   ├── tushare/
│   │   └── efinance/
│   └── notifications/   # 外部通知服务
│       ├── email.py
│       └── webhook.py
└── config/
    ├── __init__.py
    ├── settings.py
    ├── development.py
    ├── production.py
    └── testing.py
```

#### 迁移步骤
1. 将 `data_sources/` 移动到 `infrastructure/external/data_sources/`
2. 将 `utils/database.py` 重构为 `infrastructure/database/`
3. 创建 `infrastructure/cache/` 目录
4. 创建 `infrastructure/messaging/` 目录
5. 重构配置管理到 `infrastructure/config/`

### 4. 服务层重构

#### 当前结构
```
backend/src/services/
├── stock_service.py
├── data_storage.py
├── cache.py
├── monitoring.py
└── debug_service.py
```

#### 推荐结构
```
backend/src/services/
├── stock_service.py        # 股票服务
├── market_service.py      # 市场服务
├── sync_service.py       # 同步服务
├── notification_service.py # 通知服务
└── monitoring_service.py  # 监控服务
```

#### 迁移步骤
1. 保留核心服务逻辑
2. 将数据存储逻辑移到仓储层
3. 将缓存逻辑移到基础设施层
4. 将监控逻辑移到工具层

### 5. 后台任务重构

#### 当前结构
```
backend/src/scheduler/
├── tasks/
│   ├── full_sync_kline_task.py
│   ├── minute_kline_task.py
│   ├── realtime_quotes_task.py
│   └── intraday_sentiment_task.py
├── scheduler.py
└── trading_calendar.py
```

#### 推荐结构
```
backend/src/workers/
├── tasks/                    # 任务定义
│   ├── data_sync/
│   │   ├── full_sync_kline.py
│   │   ├── minute_kline.py
│   │   └── realtime_quotes.py
│   ├── analysis/
│   │   └── intraday_sentiment.py
│   └── monitoring/
│       └── health_check.py
├── schedulers/               # 调度器
│   ├── data_sync_scheduler.py
│   ├── analysis_scheduler.py
│   └── monitoring_scheduler.py
└── processors/               # 数据处理器
    ├── kline_processor.py
    └── quote_processor.py
```

#### 迁移步骤
1. 将 `scheduler/tasks/` 重命名为 `workers/tasks/`
2. 创建 `workers/schedulers/` 目录
3. 创建 `workers/processors/` 目录
4. 重构任务逻辑，使用用例层

### 6. 工具层重构

#### 当前结构
```
backend/src/utils/
├── config.py
├── database.py
├── logger.py
└── helpers.py
```

#### 推荐结构
```
backend/src/utils/
├── logging/
│   ├── logger.py         # 日志配置
│   ├── handlers.py       # 日志处理器
│   └── formatters.py    # 日志格式化
├── monitoring/
│   ├── metrics.py       # 性能指标
│   ├── tracing.py       # 链路追踪
│   └── health.py       # 健康检查
├── validation/
│   ├── validators.py    # 数据验证
│   └── schemas.py      # 数据模式
└── helpers/
    ├── datetime.py       # 时间工具
    ├── financial.py     # 金融计算
    └── converters.py   # 数据转换
```

#### 迁移步骤
1. 将 `logger.py` 移动到 `utils/logging/`
2. 创建 `utils/monitoring/` 目录
3. 创建 `utils/validation/` 目录
4. 将通用工具函数整理到 `utils/helpers/`

### 7. 测试体系建立

#### 当前结构
```
backend/ (没有专门的测试目录)
```

#### 推荐结构
```
backend/tests/
├── unit/                      # 单元测试
│   ├── test_api/
│   │   ├── test_stocks.py
│   │   ├── test_sectors.py
│   │   └── test_market.py
│   ├── test_services/
│   │   ├── test_stock_service.py
│   │   └── test_sync_service.py
│   ├── test_use_cases/
│   │   ├── test_get_stock_info.py
│   │   └── test_sync_stock_data.py
│   └── test_utils/
│       ├── test_logger.py
│       └── test_helpers.py
├── integration/                # 集成测试
│   ├── test_database/
│   │   ├── test_stock_repository.py
│   │   └── test_kline_repository.py
│   └── test_external/
│       ├── test_pytdx_client.py
│       └── test_tushare_client.py
├── e2e/                       # 端到端测试
│   ├── test_stock_sync_flow.py
│   └── test_realtime_quotes_flow.py
├── fixtures/                  # 测试数据
│   ├── stocks.json
│   └── klines.json
├── conftest.py                # pytest配置
└── __init__.py
```

#### 建立步骤
1. 创建 `backend/tests/` 目录
2. 配置pytest（`conftest.py`）
3. 编写关键模块的单元测试
4. 添加集成测试
5. 配置测试覆盖率

### 8. 部署配置建立

#### 当前结构
```
(没有专门的部署配置)
```

#### 推荐结构
```
backend/deployments/
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── docker-compose.prod.yml
├── kubernetes/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── configmap.yaml
│   └── ingress.yaml
└── terraform/
    ├── main.tf
    ├── variables.tf
    └── outputs.tf
```

#### 建立步骤
1. 创建 `Dockerfile`
2. 配置 `docker-compose.yml`
3. 创建Kubernetes配置文件
4. 配置Terraform（可选）

### 9. CI/CD配置建立

#### 当前结构
```
(没有CI/CD配置)
```

#### 推荐结构
```
.github/workflows/
├── ci.yml              # 持续集成
├── cd.yml              # 持续部署
├── security.yml         # 安全扫描
└── code_quality.yml     # 代码质量检查
```

#### 建立步骤
1. 创建CI工作流
2. 配置代码质量检查
3. 配置安全扫描
4. 配置自动部署

### 10. 文档体系建立

#### 当前结构
```
(文档分散，没有统一管理)
```

#### 推荐结构
```
backend/docs/
├── api/                    # API文档
│   ├── stocks.md
│   ├── sectors.md
│   └── market.md
├── architecture/            # 架构文档
│   ├── design.md
│   ├── database.md
│   └── decisions.md
├── deployment/             # 部署文档
│   ├── docker.md
│   ├── kubernetes.md
│   └── troubleshooting.md
└── development/            # 开发文档
    ├── setup.md
    ├── testing.md
    └── contributing.md
```

#### 建立步骤
1. 创建API文档
2. 编写架构设计文档
3. 编写部署文档
4. 编写开发文档

## 依赖管理重构

### 当前依赖管理
```
(没有明确的依赖管理)
```

### 推荐依赖管理
```
backend/requirements/
├── base.txt              # 基础依赖
│   ├── fastapi==0.104.1
│   ├── uvicorn[standard]==0.24.0
│   ├── sqlalchemy==2.0.23
│   ├── pymysql==1.1.0
│   ├── redis==5.0.1
│   ├── celery==5.3.4
│   └── pydantic==2.5.0
├── development.txt        # 开发依赖
│   ├── pytest==7.4.3
│   ├── pytest-cov==4.1.0
│   ├── black==23.12.1
│   ├── isort==5.13.2
│   ├── flake8==7.0.0
│   ├── mypy==1.8.0
│   └── pre-commit==3.6.0
├── testing.txt           # 测试依赖
│   ├── pytest==7.4.3
│   ├── pytest-asyncio==0.21.1
│   ├── pytest-mock==3.12.0
│   └── faker==20.1.0
└── production.txt        # 生产依赖
    ├── gunicorn==21.2.0
    ├── sentry-sdk==1.40.0
    └── prometheus-client==0.19.0
```

## 配置文件重构

### 当前配置管理
```
backend/config/
├── data_sources.yaml
├── settings.py
└── settings.yaml
```

### 推荐配置管理
```
backend/config/
├── __init__.py
├── settings.py            # 主配置类
├── development.py        # 开发环境配置
├── production.py         # 生产环境配置
└── testing.py           # 测试环境配置
```

### 配置示例
```python
# config/settings.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )
    
    # 应用配置
    APP_NAME: str = "AiStock"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    
    # 数据库配置
    DATABASE_URL: str
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 10
    
    # Redis配置
    REDIS_URL: str
    REDIS_POOL_SIZE: int = 10
    
    # 数据源配置
    PYTDX_ENABLED: bool = True
    TUSHARE_ENABLED: bool = False
    TUSHARE_TOKEN: str = ""
    
    # 任务队列配置
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str
    
    # 监控配置
    SENTRY_DSN: str = ""
    PROMETHEUS_ENABLED: bool = False

@lru_cache()
def get_settings() -> Settings:
    return Settings()

# config/production.py
from .settings import Settings

class ProductionSettings(Settings):
    DEBUG: bool = False
    DATABASE_POOL_SIZE: int = 50
    SENTRY_DSN: str = "your-sentry-dsn"
    PROMETHEUS_ENABLED: bool = True

# config/development.py
from .settings import Settings

class DevelopmentSettings(Settings):
    DEBUG: bool = True
    DATABASE_POOL_SIZE: int = 5
    LOG_LEVEL: str = "DEBUG"
```

## 迁移优先级

### 高优先级（立即执行）
1. 建立测试框架
2. 配置CI/CD流程
3. 添加Docker配置
4. 完善依赖管理

### 中优先级（1-2周内）
1. 重构API层
2. 重构核心业务层
3. 重构基础设施层
4. 建立监控体系

### 低优先级（2-4周内）
1. 完善文档体系
2. 优化部署配置
3. 建立告警机制
4. 性能优化

## 迁移检查清单

### 代码结构
- [ ] API层重构完成
- [ ] 核心业务层重构完成
- [ ] 基础设施层重构完成
- [ ] 服务层重构完成
- [ ] 后台任务重构完成
- [ ] 工具层重构完成

### 测试体系
- [ ] 单元测试框架建立
- [ ] 集成测试框架建立
- [ ] 端到端测试框架建立
- [ ] 测试覆盖率达到80%

### CI/CD
- [ ] CI工作流配置完成
- [ ] CD工作流配置完成
- [ ] 代码质量检查配置完成
- [ ] 安全扫描配置完成

### 部署
- [ ] Docker配置完成
- [ ] Kubernetes配置完成
- [ ] 环境变量配置完成
- [ ] 健康检查配置完成

### 监控
- [ ] 应用监控配置完成
- [ ] 日志聚合配置完成
- [ ] 告警机制配置完成
- [ ] 性能指标收集完成

### 文档
- [ ] API文档完成
- [ ] 架构文档完成
- [ ] 部署文档完成
- [ ] 开发文档完成

## 总结

这个迁移指南提供了详细的步骤，帮助您从当前的项目结构迁移到大厂标准的工程化结构。建议按照优先级逐步实施，避免一次性大规模重构带来的风险。

每个阶段完成后，建议进行充分的测试，确保系统功能正常。同时，保持代码的向后兼容性，确保现有功能不受影响。
