# AiStock 三层拆分说明

本目录是轻量核心代码仓库，只保留日常开发、Codex 编辑、部署和测试需要的源码与配置。

## 目录分层

- 核心代码：`F:\Stock\AiStock-core`
- 数据与运行时文件：`F:\Stock\AiStockData`
- 研究报告与历史实验归档：`F:\Stock\AiStockResearchArchive`
- 原始总目录备份：`F:\Stock\AiStock`

## 迁移原则

- `AiStock-core` 只放源码、轻量配置、文档、部署脚本和必要的研究脚本。
- `data/`、`reports/`、`artifacts/`、`logs/`、`Libs/` 等重量目录不再进入核心仓库。
- 本地敏感配置不进入核心仓库；本次迁移只复制了 `.env.example`，没有复制 `.env.local`。
- 如果代码需要访问数据或报告，应通过配置项指向外部目录，而不是把数据复制回核心仓库。

## 当前状态

旧项目的 Git 目录已被临时隔离在：

`F:\Stock\AiStock\.git.codex-disabled-20260606_175012`

恢复脚本位于：

`D:\Codex\backup\aistock_disable_git_20260606_175012\restore_aistock_git.ps1`

建议后续确认 `AiStock-core` 可正常运行后，再决定是否对旧仓库做正式归档或只读保存。
