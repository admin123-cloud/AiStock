# AiStock 容器编排与部署 (`infra/`)

本目录为 AiStock 唯一的 Docker Desktop 编排与发布入口：

- `docker-compose.yml`：主编排文件（`name: aistock`），默认启动 5 个常驻生产容器（`backend` / `frontend` / `pipeline-runner` / `postclose-watcher` / `clickhouse`），并预留按需 Profile：
  - 策略研究/回测沙箱：`docker compose -f F:/Aistock/code/infra/docker-compose.yml --profile research up -d research`
  - 盘中缓存预留：`docker compose -f F:/Aistock/code/infra/docker-compose.yml --profile cache up -d redis`
- `deploy.sh`：唯一 `main`-only 发布脚本，从 `git archive` 提取提交态至 `F:\Aistock\.cache\builds\`（自动仅保留最近 3 份），构建分角色精简镜像（`aistock-backend` / `aistock-worker` / `aistock-frontend`）并执行全链路健康检查。
- `docker/`：分角色 Dockerfile（`Dockerfile.api`、`Dockerfile.worker`、`Dockerfile.research`）。
- `nginx/default.conf`：前端静态站与 `/api/` 动态 DNS 反向代理配置。
