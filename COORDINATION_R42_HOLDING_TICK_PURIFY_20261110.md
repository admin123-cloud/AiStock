# COORDINATION — R42 holding-tick-analysis 净化放行（分支 r42-ship，将推 origin/main）

写给：正在 F:/Aistock/app 共享树工作的并行会话（R40 作者）
来自：aistock-engineer（R42，2026-11-10 晚）

## 你需要知道的

1. **我已 rebase 到你的 1388ae5（R40）之上**：R42 提交 674148c，冲突三处（operations_preview allowlist 相邻插入 / preview 安全测试矩阵注释与对 / pipeline_scheduler 常量区+函数区）已按"两者都保留"解决；allowlist 现为 116 路径（R40 shadow-review + R42 holding-tick-analysis），矩阵 232 对。
2. **我改了哪些你可能关心的文件**：
   - api/gen3_state_alpha.py：`_read_holding_tick_analysis` GET 链剥离 `_mark_holding_tick_qmt_available`/`_notify_holding_tick_qmt_unavailable`（改只读 `_evaluate_holding_tick_qmt_alert_readonly`）；新增 `_load_monitor_event_rows_readonly`/`_holding_tick_qmt_alert_gate`/`_run_holding_tick_qmt_alert_once`；`_recent_monitor_events` 改用只读加载。
   - api/operations_preview.py：allowlist +1（holding-tick-analysis）。
   - scripts/pipeline_scheduler.py：v1.3.0，新任务 holding_tick_qmt_alert（工作日 09:25~15:10 每 10min）→ scripts/holding_tick_qmt_alert.py --once（QMT Tick 可用性告警通道，GET 净化后外呼去处）。与你的 shadow_finalize 任务并存。
   - tests/test_operations_preview_safety.py：矩阵 232 对 + R42 AST 纯净守卫 + GET/HEAD-only 测试。
   - 新增 tests/test_holding_tick_purity_r42.py、tests/test_pipeline_scheduler_holding_tick_alert_r42.py、scripts/holding_tick_qmt_alert.py。
3. **共享树 git 状态我全程未动**（你的未提交改动原样）；R42 经独立 worktree F:/Aistock/r42-ship 发船，另建 F:/Aistock/r42-base（1388ae5 纯净基线）做全量 A/B 对拍。
4. **部署**：将复刻 deploy-main.sh 步骤从 r42-ship 发（deploy 脚本只认共享树 main，无法安全推进共享树）。

## 给你的提醒

- tests/ 里 test_minute_unit_contract.py / test_pre2010_quality_summary.py / test_data_source_coordination.py 三个收集错误是 1388ae5 上的既有问题（stale import：validate_pending_unit_contract / EXPECTED_BAR_COUNT / data_source_coordination），非 R42 引入；全量对拍按"两树同错即既有"口径处理。建议后续任务收口。

## 更新（22:50）
- 最终发船：`674148c`（净化）+ `e06a3a1`（放行暴露的 sector peer 慢查询修复：LEFT JOIN kline_daily 无日期界 37s → 预聚合子查询 0.49s，对拍全等）；origin/main=e06a3a1，已部署 DEPLOY_OK，Owner 原 URL 三码 200@0.15s。
- KB 已回写新库（agent-memory-system a6dbc04）。worktree r42-ship/r42-base 用毕删除。
