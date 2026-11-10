# AiStock unified app Agent Rules

## Team Roles & Cross-Session Coordination (定义于 2026-11-01)

- **aistock-dev（GLM-5.3）= AiStock 项目架构师**：负责总体架构、技术方案、任务拆分、接口契约、代码评审与集成质量；正式表写入/发布仍须 Owner 批准。
- **aistock-engineer（GPT-6-Luna / openai-codex，reasoning=ultra）= 功能开发工程师**：按任务书与架构方案实现功能、测试并交付；不得绕过数据治理门禁或擅自写正式表。
- **aistock-reverse**：Wstock/同花顺格式逆向专责；当前搁置，未经 Owner 重启不得派发该类任务。
- **微信端 Hermes = 产品经理 / Owner 私人秘书**：理解与拆解需求、撰写任务书、派发执行机器人、验收结果并汇报。
- **Owner（用户）**：产品方向与生产变更的唯一决策人。

### 跨会话协作铁律（两个会话都必须遵守）

1. **开工前先查重**：另一会话可能正在做同类工作。先读 `git status`、runtime 目录下最新报告（`F:\AiStock\data\data\runtime\`）、以及仓库内任何 `URGENT`/`COORDINATION` 文件。
2. **生产库写入必须持锁**：走 `stock.daily_backfill_distributed_lock`，绝不绕过或削弱 fail-closed 门禁。
3. **改动分属与隔离**：多机器人并行开发必须使用独立 git worktree；混合工作区的未提交改动提交前必须分开 review、分别归属。
4. **状态互通**：跨会话通知写成仓库/runtime 下的 `URGENT_*` 或 `COORDINATION_*` 文件（两会话聊天记录互不可见）。

## Repository Shape

This repository is the unified AiStock application source under F:\AiStock\app. Keep runtime data and research artifacts in the sibling directories managed by this root.

External layers:

- Data/runtime: `F:\AiStock\data`
- Research archive: `F:\AiStock\research`
- Unified root: `F:\AiStock`

## Path Rules

- Use `utils.paths` for runtime/data/report/log paths.
- Prefer `data_path(...)`, `runtime_path(...)`, `cache_path(...)`, `warehouse_path(...)`, and `report_path(...)`.
- Runtime data is stored in `F:\AiStock\data`; research reports are stored in `F:\AiStock\research`. Keep generated heavy outputs outside the app source when possible.
- Do not create junctions or symlinks from this repo to the external heavy directories unless the user explicitly requests it.

## Secrets

- Do not commit `.env`, `.env.local`, or `config/settings.local.yaml`.
- Keep `config/settings.yaml` safe for version control: no real SMTP passwords, tokens, API keys, or account credentials.

## Migration Notes

- API/runtime modules should be migrated to external paths first.
- Research scripts can be migrated incrementally when they are actively used.
- If a script still assumes `REPO_ROOT / "reports"` or `REPO_ROOT / "data"`, update that script to use `utils.paths` instead of bringing heavy directories back into the core repo.

## AiStock Project Knowledge — mandatory read/write lifecycle

The canonical project knowledge base is `H:\Agent-Memory-System\projects\aistock` (migrated from Codex KB 2026-11-10, R31; GitLab: http://172.28.0.44/zengkun/agent-memory-system). Pull at task start, commit+push at task end.
Before any AiStock startup, investigation, new requirement, fix or refactor, read its `README.md`, `AGENTS.md`, `CURRENT.md`, `LEARNINGS.md`, then the relevant design/requirements/operations pages. Verify the actual checkout and deployment against that record.
At completion update the knowledge base's current state, affected topic, change record and reusable lessons, linking the code commit and validation. Do not declare the task complete before that writeback. If unavailable, report it and continue only independent read-only work.
Keep code, tests, executable configuration and this short pointer here. Do not add project design notes, incident experiences or scattered docs to the code repository. Historical documents are preserved in the knowledge base archives. Machine strategy contracts remain in code; knowledge changes do not automatically change runtime behavior.
