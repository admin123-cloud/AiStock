# G2 二代策略数据血缘与真源治理

## 结论

当前问题的本质不是“某一张快照表偶尔没数据”，而是策略链路里同时存在真源表、派生表、审计产物、页面快照和运行态文件。如果页面或策略在不同位置读取了不同层级的数据，就会出现“数据已经插入某个中转/快照，但真实查询路径仍然无数据”的错位。

后续原则是：每个业务对象只能有一个正式真源；快照只允许用于展示、审计或兜底说明，不能替代策略正式读路径。

## 分层

| 层级 | 角色 | 正式用途 | 不能做什么 |
| --- | --- | --- | --- |
| raw | 原始行情/基础数据 | 日线、分钟线、板块映射等基础读回校验 | 不能只看写入任务成功 |
| truth | 策略业务真源 | 候选池、正式策略源、实盘影子台账 | 不能被页面快照绕过 |
| derived | 研究或训练派生产物 | 因子训练基准、研究矩阵 | 不能误当每日实盘信号 |
| audit | 生成过程审计 | 解释某次更新为什么产出这些结果 | 不能作为最终可买清单 |
| cache/runtime | 展示缓存/运行状态 | 页面加速、调度状态、邮件节流 | 不能参与买点真假判断 |

## G2 当前正式链路

| 阶段 | 真源/产物 | 类型 | 官方读路径 | 写入方 |
| --- | --- | --- | --- | --- |
| data_source | ClickHouse `kline_daily` | raw | `_daily_stock_coverage_status`、候选池上下文读取 | 日线维护任务 |
| data_source | ClickHouse `kline_minute_15` | raw | `_gen2_minute_table_status` | 分钟线维护任务 |
| data_source | ClickHouse `kline_minute_30` | raw | `_gen2_minute_table_status` | 分钟线维护任务 |
| selection | `reports/gen2_event_study_full/v4_event_dataset.parquet` | truth | `_v4_event_dataset_status`、`_load_gen2_v4_pool_context` | `scripts/gen2_refresh_v4_event_dataset.py` |
| selection_context | ClickHouse `sector_stocks` | truth | `_sector_mapping_status` | 板块数据维护 |
| scoring | Alpha191 训练基准文件 | derived | `_alpha191_artifact_status` | Alpha191 研究构建脚本 |
| strategy | `reports/gen2_v2_complete_strategy/sources/g2_v2_complete.parquet` | truth | 回测/策略版本读取器 | `scripts/gen2_build_v2_complete_strategy.py` |
| strategy | `reports/gen2_risk_cool_shadow_ledger/shadow_ledger.csv` | truth | `_load_gen2_risk_cool_shadow_ledger` | `scripts/gen2_update_live_shadow.py` |
| strategy | `reports/gen2_risk_cool_shadow_ledger/live_updates/*` | audit | `_load_gen2_live_update_summary` | `scripts/gen2_update_live_shadow.py` |
| notification | `data/runtime/v4_live_monitor/gen2_shadow_buy_monitor_state.json` | runtime | `_load_gen2_shadow_monitor_state` | 15 分钟监控调度 |

## 监控要求

1. 阻塞检查必须从数据源到通知全链路覆盖：`data_source -> selection -> selection_context -> scoring -> strategy -> intraday_data -> notification`。
2. 每一步不仅检查文件或表是否存在，还要通过官方读路径读回目标日期的行数、股票覆盖数或更新时间。
3. 如果上游真源未通过校验，本轮策略更新必须中止，并按交易时段内的邮件节流规则通知。
4. 页面上的快照、缓存、审计文件必须显示来源和更新时间，避免看起来“有数据”但策略正式读路径实际为空。
5. 新增中转表前必须先说明：它属于 truth、derived、audit、cache 还是 runtime；若不是 truth，不能被正式交易逻辑直接读取。

## 已落地

- `/trading/gen2/workflow/status` 返回 `lineage_items`，页面可直接看到每个关键数据节点的类型、存储位置、官方读路径和读回结果。
- `/trading/gen2/workflow/lineage` 提供独立的血缘清单接口，便于后续单独做数据治理页面。
- 实盘交易页的策略工作流监控中新增关键真源表格，用于排查“快照有数据但正式查询无数据”的口径错位。

