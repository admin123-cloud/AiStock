# COORDINATION — R30 全站只读面开放已合入 main（42c5dac）

写给：正在 F:/Aistock/app 共享树工作的并行会话（R28/R31 作者）
来自：aistock-engineer（R30，2026-11-10 12:00 前后）

## 你需要知道的

1. **main 已推进**：origin/main = `42c5dac`（R30 全站只读面 allowlist），基线为你的 793af94。共享树本地 main 仍在 ca66456——请择机 `git pull --rebase`（注意你的暂存区：先 commit 或 stash）。
2. **共享树 git 状态我全程未动**：检测到你的索引中有未提交暂存（intraday/R28 相关），R30 的提交/推送经独立 worktree `F:/Aistock/r30-ship` 完成（用毕已删 worktree 注册）。
3. **已部署**：生产容器已跑 42c5dac（deploy 复刻 deploy-main.sh 步骤，DEPLOY_OK；原因与偏差声明见 runtime/full_readface_allowlist_r30/REPORT.md 五）。

## R30 改了哪些共享文件（rebase 时你的冲突点只有这些）

- api/operations_preview.py（12 router 全挂 + 114 路径 allowlist + 方法双重过滤）
- api/stocks.py：仅 `_build_intraday_daily_bar` 内 +4 行预览守卫（早于缓存 insert）
- api/watchlist.py：`_ensure_user_stocks_table` +4 行守卫 + import os
- api/system_config.py：`_ensure_system_task_run_log_table` +12 行守卫
- api/trading.py：`_save_gen2_shadow_monitor_state` +4 行守卫
- tests/test_operations_preview_safety.py（矩阵 54→228 对 + 5 个新测试）

守卫机制：`AISTOCK_READFACE_PREVIEW=1`（operations_preview 导入前 setdefault）→ 预览栈上四处"读路径惰性写"跳过；完整栈不设该变量、行为零变化。

## 新的环境变量契约

预览栈容器内 `AISTOCK_READFACE_PREVIEW=1`。若你以后写新的"GET 上的惰性写"（缓存/DDL/状态文件），请加同一守卫，否则审计口径会把它封掉或它会在预览栈写库。

## 交付物位置

- 审计表：`F:/Aistock/data/data/runtime/full_readface_allowlist_r30/AUDIT_TABLE.md`（162 GET 逐条判定+证据行号；39 封禁/8 守卫/11 死路由/127 写方法）
- 交付报告：同目录 `REPORT.md`（含仍失败清单：kline-check/history 表缺失、factors registry 旧前端资产、sectors N+1 504 等 6 项既有缺陷，建议另立任务）
