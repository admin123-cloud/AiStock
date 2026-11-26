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

This repository is the unified AiStock application source under `F:\Aistock\code`. Keep runtime state, storage, and cold archives in the sibling directories managed by `F:\Aistock`:

- Source & Docker orchestration (Git repo): `F:\Aistock\code`
  - `infra/`: Single source of truth for Docker Compose (`docker-compose.yml`), role-based Dockerfiles (`docker/`), Nginx (`nginx/`), and `deploy.sh`
  - `pipelines/`: Production container scheduler & watcher entrypoints
  - `host_bridges/`: Windows host-only GUI bridge entrypoints (TDX / THS)
  - `research/`: Strategy research, backtests, and archived G2/G3 legacy scripts (excluded from production Docker images)
- Hot runtime & host-container IPC: `F:\Aistock\runtime`
- Persistent storage (warehouse / reports / artifacts / logs): `F:\Aistock\storage`
- Cold archive (historical snapshots & raw packages): `F:\Aistock\archive`
- Build context cache (auto-pruned to latest 3 SHAs): `F:\Aistock\.cache\builds`

## Path Rules

- Use `utils.paths` for runtime/storage/report/log paths.
- Prefer `runtime_path(...)`, `warehouse_path(...)`, `cache_path(...)`, `report_path(...)`, and `data_path(...)`.
- Runtime state lives in `F:\Aistock\runtime`; persistent warehouse/reports/artifacts/logs live in `F:\Aistock\storage`. Keep generated heavy outputs outside `F:\Aistock\code`.
- Do not create junctions or symlinks from this repo to the external heavy directories unless the user explicitly requests it.

## Secrets

- Do not commit `.env`, `.env.local`, or `config/settings.local.yaml`.
- Keep `config/settings.yaml` safe for version control: no real SMTP passwords, tokens, API keys, or account credentials.

## Production Operations & Communication Guardrails

- **故障告警双通道铁律**：所有生产故障、任务失败、桥接/AI Bus 离线超时等 Bug 类告警，必须**同时推送微信（iLink Bridge）与 QQ 邮箱**（双发直达，不以单通道成功跳过另一通道）；日常成功回执维持微信单通道。
- **现役策略防压缩漂移（SSOT Grounding）**：严禁凭历史聊天压缩记忆回答现役策略版本、仓位或买卖参数；所有策略与实盘问答必须实时核对在线接口（`/api/g3/market-gate`、`/api/g3/mining/ar3/radar`）、`live_strategy_constitution` 及源码。
- **微信消息短编号规范（`[idxxxx]`）**：`weixin_ilink_bridge.py` 自动为所有发往微信的正文前置递增短编号 `[idxxxx]`，计数与索引存于 `F:\Aistock\runtime\weixin_ilink\inbox\`（严禁放回根目录以免干扰 `*.json` 账号凭证发现）。收到含 `ref_msg_id` / `referenced_message` 的回复时，必须严格围绕对应原消息应答。
- **Windows 宿主后台与 GUI 自动化铁律**：
  - 严禁在同步命令中用 `Start-Process` 启动常驻守护进程（Windows Job Object 会在命令退出时杀死子进程）；会话内托管守护用 `IsDaemon=True`，系统级常驻桥接用 `schtasks /Run`。
  - 通达信/同花顺 GUI 自动化不得只依赖物理鼠标 `SetCursorPos`（RDP 最小化或锁屏会返回 `ERROR_ACCESS_DENIED=5`）或冷启动单帧截图；必须保留防遮挡校验 + 短重试 + 安全兜底（如 `PostMessageW` 窗口消息点击、渲染轮询、白名单升级弹窗关闭）。
- **容器代码核验铁律**：生产容器（`aistock-backend`、`aistock-pipeline-runner`、`aistock-frontend`）运行镜像内代码而非宿主机 bind mount；排查或修复线上问题时必须通过 `docker exec` 核验容器内实际生效代码，并在热更新后验证健康接口与调度注册日志。

## Migration Notes

- API/runtime modules should be migrated to external paths first.
- Research scripts can be migrated incrementally when they are actively used.
- If a script still assumes `REPO_ROOT / "reports"` or `REPO_ROOT / "data"`, update that script to use `utils.paths` instead of bringing heavy directories back into the core repo.

## AiStock Project Knowledge — mandatory read/write lifecycle

The canonical project knowledge base is `H:\Agent-Memory-System\projects\aistock` (migrated from Codex KB 2026-11-10, R31; GitLab: http://172.28.0.44/zengkun/agent-memory-system). Pull at task start, commit+push at task end.
Before any AiStock startup, investigation, new requirement, fix or refactor, read its `README.md`, `AGENTS.md`, `CURRENT.md`, `LEARNINGS.md`, then the relevant design/requirements/operations pages. Verify the actual checkout and deployment against that record.
At completion update the knowledge base's current state, affected topic, change record and reusable lessons, linking the code commit and validation. Do not declare the task complete before that writeback. If unavailable, report it and continue only independent read-only work.
Keep code, tests, executable configuration and this short pointer here. Do not add project design notes, incident experiences or scattered docs to the code repository. Historical documents are preserved in the knowledge base archives. Machine strategy contracts remain in code; knowledge changes do not automatically change runtime behavior.

## Quant Strategy, ClickHouse & Live Discipline Guardrails (AR4.6)

- **全账户物理验真门禁（反单腿幻觉）**：任何策略参数、止损、轮动频率或卫星仓改动，严禁凭隔离单腿回测上线；必须合入完整物理单账户 `AR4.6`（`simulate_ar45_physical`）通过 `IS / Val / 2026 熊市 OOS` 与滑点/极值压力测试，并获 Owner 批准后方可固化。
- **执行工程与风险预算解耦**：整手资金利用率优化（如 `pooled_flex` 1.25x 弹性分槽、自适应单手门槛）与提高名义风险仓位（如 `50% → 60%~65%`）必须拆分归因、独立决策；严禁借工程优化暗升名义杠杆，名义仓位上调必须先满足实盘滑点门禁。
- **ClickHouse 分钟表查询铁律**：查询 `kline_minute_5` / `kline_minute_15` 必须显式携带 `datetime >= ... AND datetime <= ...` 分区裁剪；禁用无分区裁剪的 `FINAL` 或大表聚合前置 `INNER JOIN stocks`（改用 `IN (SELECT ...)` + `optimize_aggregation_in_order = 1` 或 Pandas `drop_duplicates(keep='last')` 防 HTTP 500/OOM）。
- **多会话脏工作区安全提交**：提交前仅 `git add` 本次精确修改文件，严禁 `git add -A` 混入其他会话改动；生产发布一律走官方 `infra/deploy.sh` 并确认 `DEPLOY_OK`。
- **实盘交易纪律（acct1）**：严格按已验证策略执行、不临时改规则；冰点 30m 触发后严守 `i+6` 冷却锁（期间不续期、不加仓、不补买换股）；微信告警仅在可执行动作或真实风控触发时推送，标题统一 `【acct1·择时看盘】`，正文逐行 `- ` 列表并强制带去重键。
- **核心跨资产 ETF 池 5 层准入门禁（反事后挑赢家偏差）**：严禁仅凭近 4 年涨幅或单腿回测将扩展 ETF 晋级核心池（Top 2 动量池扩容过多必然引发坑位稀释与假突破磨损）；候选标的必须同时通过 ① 宏观象限正交且无长期 QDII 高溢价失真 ② 近 60 日流动性充沛（拒绝千万级以下僵尸盘）③ 完整物理单账户 AR4.6 消融胜出 ④ `rebal=3/5/7/10d` 参数高原全胜 ⑤ 跨反周期远古盲测（含 `2020~2022` 通胀/股债双杀）不拖累，方可获批晋级。
- **正式日线表盘中未收盘拦截铁律**：交易日收盘前（`15:05 Asia/Shanghai` 前）执行任何日线同步脚本（如 `sync_cross_asset_etf_daily.py`），必须显式过滤 `trade_date >= today` 的未收盘半日 bar，严禁写入正式日线表造成核心池日期割裂。
