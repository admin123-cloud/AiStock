# COORDINATION：R22 5m 源升级完成，聚合同步被 R19 NULL 不兼容阻塞（待决策）

致：微信端 PM（aistock-pm）；抄送：Owner
自：aistock-engineer（桌面端会话）
时间：2026-11-09 21:00（北京）
任务书：`F:/Aistock/data/data/runtime/task_briefs/20261109_eastmoney_source_eval_r22.md`

## 已完成（详见 `runtime/minute_source_eval_r22/REPORT.md`）

1. **源评估**：东财 push2his 5m 为带 amount（元）最优源 → `minute_collect_auto.py` 默认源已切 east
   （auto→east，B=腾讯降兜底，sina 评估未过不入生产：48 槽不齐/股非整百/额噪声）。
   注意：east 非严格"全过"（10 样本量 18/20，两键仅 15:00 收盘竞价归因差 3.1~3.2%，日合计一致），
   按 Owner"带 amount 的合格源"意图选型，**请复核**；否决则改 EVALUATED_SOURCE 常量一处回退。
2. **缺口补齐**：11-03 300555.SZ、11-02 603828.SH 各 48 行写入成功（B 源兜底、amount=NULL 如实）；
   独立只读回验 48/48 根、日量与日线 0.000% 吻合。
3. **5m 剩余缺口** = 仅 920931/920950.BJ @11-03（东财有覆盖但 open 与日线分歧 1.4~2.0% 被包络拒，
   腾讯无覆盖，新浪槽不齐 → 待 TDX 客户端日）。
4. 顺带修复 R21 两个生产缺陷（均在本任务授权文件内，单测钉死，162 tests 全绿）：
   CH sum(amount) 对全 NULL 组返回 NULL 的 float(None) 崩溃；ch_window 单日窗口含相邻日 +
   HH:MM 键折叠导致读回混日。两次 fail-closed 回滚均精确按 id、残余 0、锁释放。

## 阻塞：15/30/60 聚合同步（任务书 §2 要求，未完成）

`minute_agg_backfill_window.py --formal-write 2026-11-02~11-03` 崩于 `fetch_5m_bars` L159
`float(r["amount"])`：R19 只见过 lc5 带额行，遇本次 B 行（amount=NULL）即崩。
**未取锁、零写入、零损伤**（out-dir 仅 audit 产物；dry-run 复现 log 在证据目录）。
任务边界"不改 R16/R19 文件"与"跑聚合同步"冲突，需裁决：

- 方案 A（建议）：批准对 R19 做最小 NULL 感知修复（≥6 处）+ 定义**聚合额 NULL 口径**
  （建议：成分任一 NULL → 聚合行 amount=NULL，绝不部分求和）+ 补单测；随后一条命令补齐两键聚合。
- 方案 B：两键 5m 改回不写（放弃 NULL 行）等东财数据/其他带额源 —— 不建议（违背缺口补齐目标）。

另请留意：R24 调度器（pipeline_scheduler）今后跑 5m 采集将以 east 为默认源；其 rc=4 处理策略
（是否继续聚合）与本次 R19 修复决策相关，建议一并裁决。
