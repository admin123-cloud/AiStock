# QMT xtquant 统一采集定时策略

目标：稳定替代 TDX 数据源，所有历史补数、盘中同步、盘后修复都收口到 `scripts/qmt_xtquant_data_source_task.py`。

## 分层计划

| 层级 | 时间 | 入口参数 | 范围 | 目的 |
| --- | --- | --- | --- | --- |
| 盘前轻校验 | 08:45-09:15 | 后端 core maintenance | 交易日历、股票/指数/板块列表 | 确认当天交易日和基础资产可用 |
| 盘中轻采集 | 09:35-15:10 每 5 分钟 | `--mode minute-gap-repair --scenario intraday` | 默认指数/重点池，单代码批次 | 给策略盘中判断提供最新 5m/15m/30m/60m |
| 盘后滚动修复 | 18:05-23:50 每 15 分钟 | `--scenario after-close` | `stock,index`，每轮约 80 个 issue code | 分片完成当日全市场分钟数据补齐 |
| 夜间滚动修复 | 00:40-06:30 每 15 分钟 | `--scenario history` | 上一个自然日，每轮约 120 个 issue code | 修复 QMT 下载延迟、超时和漏码 |

## 稳定性规则

- 分钟级 QMT 采集固定使用 `minute_batch_size=1`，避免 `download_history_data2` 多代码批量同步超时。
- 盘中不做全市场高压修复，只采策略必需范围；盘后再做全市场完整性。
- 盘后和夜间使用 offset 状态文件滚动推进，成功才推进 offset，失败不跳过问题批次。
- 所有结果写入统一 ClickHouse 表：`kline_daily`、`kline_minute_5/15/30/60`。
- Docker 后端不直接 import `xtquant`；Windows 主机计划任务调用统一采集入口。
- TDX Gateway 只保留 legacy 诊断，不再作为数据源健康前置条件。

## 安装

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Stock\AiStock-core\scripts\install_qmt_xtquant_collection_schedule.ps1
```

## 手动试跑

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File F:\Stock\AiStock-core\scripts\run_qmt_xtquant_collector.ps1 -Scenario intraday -Universe index -SkipTimeWindowCheck
```
