# TDX 依赖审计报告（2026-07-04）

## 2026-07-05 修订结论

用户已明确改变方向：不再直接全部移除 TDX。后续整理目标应改为“QMT/xtquant 为主数据源，TDX/TdxQuant 作为受控兜底手段”，而不是彻底去 TDX 化。

新的边界：

- QMT/xtquant 仍是默认主源，负责日线、5m 原子分钟线、15m/30m/60m 本地派生和常规修复。
- TDX/TdxQuant 保留为 fallback，不参与默认启动健康门槛，不抢主源。
- fallback 必须显式触发、可审计、可回滚，并记录触发原因、覆盖代码、覆盖日期、写入行数和差异校验结果。
- TDX fallback 允许使用 alias 表做代码映射，但行情结果不得长期落入另一套备份行情表；通过缺口审计后必须直接写回 `kline_daily` / `kline_minute_5` / `kline_minute_15` / `kline_minute_30` / `kline_minute_60` 这组 QMT canonical 主表。
- 前端和系统配置页应展示“主源 QMT / 兜底 TDX”的分层状态，避免用户误以为 TDX 已被删除或仍是主源。

## 2026-07-05 代码与板块映射原则

用户进一步明确：股票代码和板块数据全部以 QMT 的结果为准，TDX 只生成 alias 映射。后续 fallback 的身份体系应按以下规则执行：

- 项目内部标准股票、指数代码以 QMT/ClickHouse `stocks.code` 为准。
- 项目内部板块、板块成分、板块层级以 QMT/ClickHouse `sectors` 和 `sector_stocks` 为准。
- TDX 不创建新的主身份，不覆盖 QMT 股票名称、板块名称或板块成分。
- 为 TDX 建 `instrument_alias` / `source_instrument_alias` 映射表，记录 `canonical_code`、`source='tdx'`、`source_code`、`source_name`、市场、匹配置信度和生效状态。
- 当 QMT 获取不到某只股票/指数 K 线时，fallback 流程先用 QMT canonical code 查 alias，再拿 TDX 的 `source_code/source_name` 去 TDX 查询。
- TDX 查询结果只能回写到对应 QMT canonical code，不能用 TDX 名称或 TDX 板块体系反向改写 QMT 主数据。
- 如果 alias 不存在、歧义、退市状态冲突或名称/市场不匹配，TDX fallback 必须停止并进入人工审计。
- TDX 板块数据只可作为辅助参考或 alias 映射来源，不直接参与主升行业板块体系；板块、成分和 canonical code 仍以 QMT 为准。

## 原始审计结论

当前项目的数据源主链路已经切到 QMT/xtquant：

- `data_fetcher.manager.DataSourceManager` 只注册 `qmt_xtquant`，没有把 TDX/TdxQuant 注册为自动 fallback。
- `data_fetcher/sources/tdxquant_pool.py` 顶部有 `AISTOCK_ALLOW_LEGACY_TDX` 保护，未显式开启时导入即阻断。
- `api.main` 已跳过 TdxQuant startup health check，并声明 QMT 是 active market-data source。
- `api.system_config` 的核心数据修复优先调用 `scripts/qmt_xtquant_data_source_task.py`，旧 TDX 修复分支前有 `Legacy TDX data-source repair is disabled` 阻断。

但项目里仍存在真实 TDX 依赖残留，不能只按文档残留处理。风险主要来自直接 import 旧源的 API/脚本、Docker 环境变量/挂载，以及历史调度脚本。

## 需要优先纳入受控兜底

### 1. 直接导入旧源的 API

- `api/sync_full_history.py`
  - 直接 `from data_fetcher.pytdx import PytdxDataSource`。
  - 用途是自动补完整 K 线历史，后续不应删除，但要改成“QMT 优先、TDX fallback 显式触发”。
  - 风险：当前会绕过 `DataSourceManager`，如果被手工运行会直接走 pytdx；应加 source policy、alias 映射和审计报告，最终只写 canonical 主表。

- `api/stocks.py`
  - `/ {code} /industry` 路由仍直接导入 `PytdxDataSource` 获取行业信息。
  - `StockDataSyncer.sync_stock_data` 的主同步逻辑已经通过 `DataSourceManager` 走 QMT，但注释、变量名仍保留 `tdx_period/pytdx_period`。
  - 建议：默认读取本地 `sector_stocks/sectors` 或 QMT 官方市场范围映射；TDX 行业信息只作为缺失字段补全 fallback。

### 2. 直接导入 TdxQuant 的脚本

这些脚本会真正触发 `tdxquant_pool` 或 `TdxQuantDataSource`，不再以“全部退役”为目标，而应改造成显式 fallback 工具：

- `scripts/sync_intraday_minutes_fast.py`
  - 旧的盘中 15m/30m 分钟线同步，从 TdxQuant 写 ClickHouse。
  - 常规盘中同步应由 `scripts/qmt_xtquant_data_source_task.py` 和 `scripts/qmt_xtquant_minute_gap_audit_repair.py` 接管；TDX 版本只用于 QMT 缺口无法修复时的人工/受控兜底。

- `scripts/backfill_tqcenter_daily_to_clickhouse.py`
- `scripts/backfill_stock_share_cap_tdxquant.py`
- `scripts/backfill_index_turnover_rate_tdxquant.py`
- `scripts/fix_stock_bad_kline.py`
- `scripts/fix_index_kline_volume.py`
- `scripts/fetch_sector_change_pct.py`
- `scripts/emergency_backfill_daily.py`
- `scripts/generate_mainline_intraday_diffusion_factor.py`
- `scripts/validate_true_sector_index_quarterly_stock_acceptance_v1.py`

其中 `generate_mainline_intraday_diffusion_factor.py` 已有 ClickHouse fallback，但主函数仍先拉 TdxQuant 板块指数 close；应调整为默认读取 ClickHouse/QMT 落地表，TDX 只在板块指数缺失时显式兜底。

### 3. Docker/环境配置仍暴露 TDX

- `docker-compose.dev.yml`
- `docker-compose.app.yml`

仍包含：

- `AISTOCK_LOCAL_TDX_ROOT=/tdx/vipdoc`
- `AISTOCK_TDX_GATEWAY_URL=http://host.docker.internal:8765`
- `D:/TDX/vipdoc:/tdx/vipdoc:ro`

虽然 `AISTOCK_STARTUP_TDXQUANT_CHECK_ENABLED=0`，但这些配置会让容器形态继续暴露 TDX。新的建议不是删除，而是改成可读性更强的 fallback 配置：默认主源仍为 QMT，TDX Gateway URL 仅用于 fallback/THS bridge，并在系统状态页标注“非主源”。

### 4. requirements 仍安装 pytdx

- `requirements.txt` 仍包含 `pytdx==1.72`。

建议保留 `pytdx`，但注释应改成“TDX fallback/backfill 使用，默认主源不依赖”。如果未来拆分 requirements，可放入 `requirements-tdx-fallback.txt`，由 fallback 部署显式安装。

## 可以保留但需明确 legacy 边界

### TDX Gateway 服务和诊断接口

- `services/tdx_gateway.py`
- `data_fetcher/sources/tdx_gateway_client.py`
- `scripts/start_tdx_gateway.bat`
- `scripts/tdx_gateway_memory_guard.ps1`
- `scripts/tdx_gateway_memory_watchdog.ps1`
- `api/system_config.py` 中 `/tdx-gateway/*`

当前后端路由已经返回 “legacy diagnostics disabled”，前端也未发现直接调用 `tdx-gateway` 的入口。建议改为“TDX fallback diagnostics”：只用于兜底源健康检查、历史排障和同花顺桥，不参与默认主源健康判断。

### G3 同花顺账户桥残留

- `api/gen3_state_alpha.py`

仍有 `AISTOCK_TDX_GATEWAY_URL` 和 `tdx_gateway_ths_bridge`。这部分不是市场行情数据源，而是同花顺账户/成交读取的旧桥。现在 QMT Mini 只读持仓已经存在，建议：

- 默认 `broker_sync_source=qmtmini`。
- TDX gateway THS bridge 只在显式选择 `tdx_gateway` 时启用。
- 文案从 “TDX Gateway” 改成 “legacy THS bridge”，避免误解为市场数据源。

## 文档与历史材料

以下主要是历史文档/技能包，可保留，但要从“旧主源文档”改标为“TDX fallback 文档”：

- `docs/TDXQUANT_SETUP.md`
- `docs/tdxquant_documentation.md`
- `docs/integrations/pytdx-api.md`
- `docs/integrations/pytdx-integration.md`
- `docs/architecture/ENTERPRISE_ARCHITECTURE.md`
- `docs/references/DATA_SOURCES_ANALYSIS.md`
- `skills/tdxquant-core/**`
- `README.md` 中旧 TDX 描述

建议移动或归档到 `docs/fallback/tdx/`，并在 README 顶部写明当前主源是 QMT/xtquant，TDX/TdxQuant 是受控兜底源。

## 建议整理顺序

1. 先改配置形态：README、`.env.example`、系统配置页明确“QMT 主源 + TDX fallback”。
2. 建立统一 fallback policy：只有 QMT 缺口修复失败、QMT 源不可用或人工指定时才调用 TDX。
3. 所有 TDX 兜底写入必须经 alias 映射和审计报告，最终只允许写入 canonical 主 K 线表；不得沉淀为另一套 TDX 备份行情表。
4. 改 API：`api/sync_full_history.py` 和 `api/stocks.py` 不再直接绕过策略，统一走 source policy。
5. 改研究脚本：默认读 ClickHouse/QMT 落地表，TDX 只做缺失数据补洞或差异审计。
6. 最后做验证：以 ClickHouse 覆盖率、分钟缺口审计、G3 页面数据完整性为主验收；TDX Gateway 在线只作为 fallback readiness，不作为主源验收。

## 验收口径

- `rg "from data_fetcher\\.sources\\.tdxquant|from data_fetcher\\.pytdx|import pytdx"` 命中的文件必须通过统一 fallback policy 或明确标注为 fallback 工具。
- 默认主链路不因 TDX Gateway 不在线而失败；只有 fallback 任务才检查 TDX Gateway/TdxQuant。
- `DataSourceManager` 仍只注册 `qmt_xtquant`。
- 每日/分钟修复入口统一为 `scripts/qmt_xtquant_data_source_task.py`。
- G3、系统配置页、核心调度的主源健康判断只看 QMT/ClickHouse 覆盖率与缺口审计；兜底区单独展示 TDX readiness。
