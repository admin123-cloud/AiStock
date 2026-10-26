# 脚本分层入口

`scripts/` 保留当前仍可能被测试、任务和运维入口直接调用的运行维护与兼容路径；已迁移的研究脚本以 `research/` 下的 canonical 文件为准。研究脚本的层级登记见同目录 `layers.json`；它记录分类依据和计划归属。

- `runtime_maintenance`：数据采集、质量门禁、修复、迁移、同步、调度和发布检查。
- `research_g2`：G2 历史研究、回测和观察脚本。
- `research_g3`：G3 当前主线、主升、路由和历史对比研究脚本。
- `research_cross_strategy`：跨策略、ETF、通用审计、回测和实验脚本。
- `archived_duplicate`：已迁入 `research/` 且从本目录移除的重复入口，清单见项目知识库 `archives/cleanup/`。

后续物理迁移只针对 `layers.json` 中没有测试/调度引用、且已有 `research/catalog.json` 或独立迁移记录的条目；迁移前先生成包装入口并做 `py_compile` 与结构测试。
