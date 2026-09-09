# QMT Official Market Universe Migration Runbook

本文档记录 official 行情宇宙从旧 TDX 体系切换到 QMT 体系的执行顺序。

## 目标

- 价格层统一切到 QMT：`kline_daily`、`kline_minute_5`、`kline_minute_15`、`kline_minute_30`、`kline_minute_60`。
- 代码规范化层不做 TDX/QMT 映射，直接采用 QMT 代码写法。
- 板块体系不新增映射字段，删除旧体系结果后按 QMT 板块源重建。
- QMT 分钟源只采 `5m`，`15m/30m/60m` 由本地聚合链路生成。

## 只读预检

先确认 QMT 源侧数量和阈值：

```powershell
F:\python3.10\python.exe scripts\migrate_official_market_universe_to_qmt.py `
  --probe-qmt `
  --skip-backup `
  --skip-daily-rebuild `
  --skip-sector-kline-rebuild `
  --skip-minute-rebuild
```

预期至少满足：

- QMT 股票数 >= 4500
- QMT 指数数 >= 20
- QMT 股票+指数总数 >= 5000
- QMT 板块数 >= 30
- QMT 板块成分关系数 >= 3000

不要在正式迁移中使用 `--no-qmt-sector-include-all`；它只适合调试高层板块过滤，会被源侧阈值拦截。

## 单日链路演练

不落库，仅验证价格重置、日线、板块日线、分钟修复的计划链路：

```powershell
F:\python3.10\python.exe scripts\migrate_official_market_universe_to_qmt.py `
  --skip-backup `
  --skip-stock-rebuild `
  --skip-sector-rebuild `
  --start-date 2026-07-02 `
  --end-date 2026-07-02 `
  --minute-start-date 2026-07-02 `
  --minute-end-date 2026-07-02 `
  --require-sector-daily-validation `
  --no-require-minute-validation
```

报告里应出现：

- `price_range_reset`
- `daily`
- `sector_kline_daily`
- `minute`
- `sector_daily_rows_min=true`

## 正式执行

正式执行必须显式确认：

```powershell
F:\python3.10\python.exe scripts\migrate_official_market_universe_to_qmt.py `
  --execute `
  --confirm-official-qmt-switch `
  --probe-qmt `
  --start-date 2026-01-01 `
  --end-date 2026-07-03 `
  --minute-start-date 2026-01-01 `
  --minute-end-date 2026-07-03 `
  --require-sector-daily-validation `
  --require-minute-validation
```

正式执行会先创建备份表，表名形如：

```text
stocks_backup_qmt_migration_<stamp>
sectors_backup_qmt_migration_<stamp>
kline_daily_backup_qmt_migration_<stamp>
```

执行成功后记录报告里的 `stamp`，回滚必须使用同一个 `stamp`。

## 回滚预检

列出可用备份：

```powershell
F:\python3.10\python.exe scripts\rollback_official_market_universe_qmt_migration.py --list-stamps
```

dry-run 检查指定 `stamp` 是否完整：

```powershell
F:\python3.10\python.exe scripts\rollback_official_market_universe_qmt_migration.py `
  --stamp <stamp>
```

## 正式回滚

正式回滚必须显式确认：

```powershell
F:\python3.10\python.exe scripts\rollback_official_market_universe_qmt_migration.py `
  --execute `
  --confirm-rollback-to-backup `
  --stamp <stamp>
```

回滚脚本不会消耗原始备份表；它会复制备份到临时表，再把当前 official 表保存为 `*_before_qmt_rollback_<run_stamp>` 后切回备份内容。

若 `runtime\official_market_universe_source.json` 的 `migration_stamp` 与回滚 `stamp` 一致，回滚脚本会删除该 marker。

## 完成判定

迁移或回滚完成后，至少检查：

- 对应 summary report 的 `ok=true`
- `thresholds.ok=true`
- 若是迁移：`qmt_probe_thresholds.ok=true`
- 首页或 API 使用的新数据日期符合预期
- `kline_minute_5` 是 QMT 采集源，`15m/30m/60m` 是本地聚合结果
