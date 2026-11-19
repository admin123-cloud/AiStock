# AiStock 生产管道入口 (`pipelines/`)

本目录为 Docker 容器 `aistock-pipeline-runner` 与 `aistock-postclose-watcher` 的唯一标准运行入口：

- `scheduler.py`：`aistock-pipeline-runner` 容器入口（委托 `scripts.pipeline_scheduler` 编排盘中快照、盘后 ETL、做T快慢车道、市场情绪与每周巡检）。
- `postclose_watcher.py`：`aistock-postclose-watcher` 容器入口（委托 `scripts.postclose_deadline_watch` 监控盘后发布死线并发送邮件告警）。
