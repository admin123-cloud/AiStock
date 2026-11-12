# COORDINATION — R31 知识库统一迁移（aistock-engineer，2026-11-10）

## 状态
R31 按任务书 F:/Aistock/data/data/runtime/task_briefs/20261110_kb_migration_r31.md 执行中/已完成。

## 已完成
1. 审计四清单（H:/Codex/Knowledge 全树 2884 文件）：迁移 624 / 提炼源 4 / 包袱 1187（293.5M）/ 随库 1069；manifest（逐文件 SHA256）落 F:/Aistock/data/data/runtime/kb_migration_r31/。
2. 迁移至 H:/Agent-Memory-System/projects/aistock：624/624 SHA256 全等 + staged blob hash 全等；.gitattributes 冻结该子树 EOL 转换。
3. 新库提交 ed1b487（aistock 迁移）、80f558f（SYNC.md 多机同步节奏条款），已 push origin main（GitLab 已恢复，一次成功）。
4. app 仓引用切换提交 793af94 已 push：README.md + 5 个脚本由废弃路径 F:/AiDevelop 改指 H:/Agent-Memory-System/projects/aistock。
5. 删除 H:/Codex/Knowledge（junction 先删链接再删实体）。

## 遗留（需要 Owner/PM 处理）
- **app 仓 AGENTS.md:48 知识库指针未切换**：本会话对 AGENTS.md 的写入被工具防护拦截（需用户批准、无人应答，规则禁止绕道）。该行仍指向 2026-11-08 起就废弃的 F:\AiDevelop 路径（与本次删除对象 H:/Codex/Knowledge 无关，不阻塞删除）。待 Owner 批准后改为：
  `The canonical project knowledge base is `H:\Agent-Memory-System\projects\aistock`.`
- AiWorkspace/agents、feedback 共 4 文件审阅后无耐久用户确认规则可提炼（均为宋锦视频项目 SOP 模板，feedback 无实际条目），INBOX 零新增，"全局提炼"逻辑提交按空跳过。
- AiWorkspace 根下 registry/workflows/test-cases/knowledge/schemas/scripts/templates/outputs 为宋锦视频项目产物（非 AiStock 知识），按 Owner 裁决 2"删除整个旧库"随库删除。

## 对其他会话的影响
- 此后 AiStock 知识库读写一律走 H:/Agent-Memory-System/projects/aistock（开始必 pull、结束必 commit+push，见新库 SYNC.md）。
- H:/Codex/Knowledge 已不存在；依赖它的脚本/记忆需改指新库。
