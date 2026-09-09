# AiStock

面向A股的行情维护、多策略研究与G3交易观察工作台。QMT为主数据源，TDX保留受控兜底；策略合同和执行安全开关以代码与配置为准。

## 目录职责

```text
api/                    HTTP入口及现有业务编排
services/operations/    运行健康、数据覆盖、异常通知和页面读模型
services/repositories/  数据访问
services/               行情服务、账户桥接及持仓监控
strategies/contracts.py 策略合同读取与验证
strategies/g3/          主升确认和分钟来源证据
research/g2/            G2离线实验与历史复现
research/g3/            G3离线实验与历史复现
research/cross_strategy/跨策略对比及其他历史实验
research/common/        共享报告格式化、序列化
research/catalog.json   旧入口→新入口及迁移前文件哈希
scripts/                生产/运维入口及仍被调用的研究依赖
core/                   通用基础类型与统一异常
utils/                  配置、外部路径、交易时段及存储工具
data_fetcher/          行情数据源适配
scheduler/              交易日历与调度
execution/              账户执行与安全锁
models/                 持久化数据模型
frontend/src/views/     market、g2、g3、operations、shared业务分组
config/                 公共配置、策略合同、唯一生产者清单
tests/                  自动化回归
docs/                   架构、运维与历史说明
```

大型API文件仍包含较多编排逻辑，后续应按能力逐段拆分；不要通过跨模块复制代码继续扩大它们。scripts中仍被生产调用或被其他实验导入的实现暂保留原路径。前端页面保留原URL。

## 数据保留与路径

代码整理不迁移、不删除行情数据库、策略候选、影子/历史成交、研究报告或模型数据。G2、G3及其他历史策略保持原有策略ID、报告子目录和数据库身份。

外部路径由[PATHS.md](PATHS.md)统一规定，使用utils.paths读取。私有凭证不进入版本控制，也不要往代码仓库放回重型数据目录或创建指向它们的链接。

## 运行与研究

正式API入口保持api.main:app，既有Windows任务脚本路径保持不变。切换正式服务请遵循[运行可视化上线说明](docs/运行可视化重构.md)。

```powershell
python -m research --list
python -m research --resolve gen3_audit_execution_shock_by_route_v1
python -m research gen3_audit_execution_shock_by_route_v1
```

前两个命令只列出/定位实验，不导入研究模块。第三个才执行实验，可能查询数据库并写研究报告。也可直接运行迁移后的research/<分组>/<原脚本名>.py，参数与原脚本一致。历史实验不代表当前正式交易合同。

## 验证

```powershell
python -m pytest tests -q
python scripts/check_text_health.py --staged
cd frontend
npm.cmd ci
npm.cmd run build
```

Python按当前运行环境使用3.10及以上。前端依赖和锁文件统一在frontend中，根目录没有独立Node项目。旧版概念性说明保留在[历史README](docs/history/README-v1.md)，不作为当前启动指南。

详细迁移边界、兼容入口及验证方法见[目录与公共能力整理](docs/目录与公共能力整理.md)。
