# COORDINATION: R27a 北京钟统一 B 案已执行（数据+元数据完成，读侧 R27b 待做）

- 日期：2026-11-10 00:1x ｜ 执行：aistock-engineer（桌面端）｜ 状态：**完成，全部判据 PASS**
- 任务书：`F:/Aistock/data/data/runtime/task_briefs/20261109_beijing_clock_r27a.md`
- 完整 journal（每批快照/回验/回滚模板）：`F:/Aistock/data/data/runtime/beijing_clock_unify_r26/r27a_journal.md`
- 归档：同目录 `r27a_archive/`（980001 清单 TSV、B24 明细、2012 碰撞抽样、13 个执行脚本）

## 结果摘要
1. Step0 探针四项验收 PASS（MODIFY COLUMN 0.035s 元数据级，值/分区/行数不变，toHour 北京钟）。
2. kline_minute_5 Q 簇 2000-2014 共 22,286,144 行 −16h 归真：15 批全 PASS；碰撞键 792,192（U 新代胜出，Q 代全列归档 _r27_orig_Y）；全表 countIf(toHour 17-23 AND toYear<=2014)=0；894,630,099−792,192−4,400=893,833,507 精确对账。
3. 15m 2023 B 簇 24 行：**按证据执行 −8h**（任务书"+16h"与其数学框架矛盾，同胞行取证：−8h 精确填补 2023-12-21 缺口、+16h 会造 12-22 幽灵行；journal 前置偏差2 有完整证据链）。
4. 980001.SZ 清污 6,737 行（任务书字面 scope；**发现 20 码 98 家族共 111,243+4,340 行同窗污染待 Owner 裁决是否扩清**，证据在 journal 前置补充节，清单随时可出）。
5. Step2：八表（4 正式+3 live_derived+空 1m）datetime → Nullable(DateTime('Asia/Shanghai'))；pause runner 0.2s；8/8 断言过；北京钟抽查 600519.SH/000001.SZ 2026-11-09 09:35~15:00 各 48 根。
6. 零源码改动 / 零 commit / 栈不重启 / 无未完成 mutation。

## 给 aistock-dev（架构师）——DESIGN 缺陷反馈
- **CH 24.8 对排序/分区键列一律拒绝 ALTER UPDATE（CANNOT_UPDATE_COLUMN, Code 420），无绕行设置。** DESIGN §4.1/§4.2 的 UPDATE 机制不可实现。已用等价机制替代：INSERT(平移行+反连接去碰撞)+DELETE(原簇)+OPTIMIZE FINAL，终态键集与原设计逐键等价（碰撞方向一致=U 胜出），且每批全列归档回滚更强。2012 碰撞率 217,232/2,661,296=8.16% 与 DESIGN 预估 8.1% 吻合（等价性独立佐证）。后续设计请勿再假设键列可 UPDATE。
- 另：抽样 JOIN 大表做右表侧会整表填充超 10GiB 内存限制（MEMORY_LIMIT_EXCEEDED），改 500 键点查式比对。

## 给 PM —— 待办与裁决
1. **R27b 读侧切换**（DESIGN §5 清单 S1-S11）未做，属代码切片另派；当前读侧 −8h 旧补丁与新钟面叠加，分钟 K 线详情/覆盖层/预测接口会显示 +8h 偏移时间，盘中功能以 R27b 修复为准。
2. **98 家族扩清裁决**：方案A（已执行，980001 字面）vs 方案B（20 码全家族 111,243+4,340 行，同窗同写方，证据齐全，随时可跑）。
3. _r27_* 48 张回滚表（66M 行，约 2G）保留至验收+R27b 落地，之后需批准 DROP。
4. KB 已回写（changes/2026-11-10-r27a-beijing-clock-unify.md + CURRENT + LEARNINGS）。
