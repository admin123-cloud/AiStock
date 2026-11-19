#!/usr/bin/env bash
# AiStock main 唯一部署管道（分角色精简镜像 + 自动清理 .cache/builds）
set -euo pipefail
APP='F:/Aistock/code'
BUILD_CACHE='F:/Aistock/.cache/builds'
export PATH="/c/Program Files/Docker/Docker/resources/bin:$PATH"
DOCK='/c/Program Files/Docker/Docker/resources/bin/docker.exe'
DC='/c/Program Files/Docker/Docker/resources/cli-plugins/docker-compose.exe'

cur=$(git -C "$APP" symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)
if [ "$cur" != 'main' ]; then
  echo "REFUSE: 当前处于 '$cur'。裁决：只部署 main。"
  exit 1
fi

SHA=$(git -C "$APP" rev-parse HEAD)
echo "AISTOCK_MAIN_VERSION=$SHA"
dirty=$(git -C "$APP" status --porcelain=v1 | wc -l)
if [ "$dirty" -gt 0 ]; then
  echo "提示：main 有 $dirty 项未提交改动（不参与构建）："
  git -C "$APP" status --porcelain=v1 | head -5
fi

mkdir -p "$BUILD_CACHE"
SRC="$BUILD_CACHE/$SHA-src"
rm -rf "$SRC"
mkdir -p "$SRC"
git -C "$APP" archive --format=tar "$SHA" | tar -x -C "$SRC"

test -f "$SRC/infra/docker/Dockerfile.api" \
  && test -f "$SRC/infra/docker/Dockerfile.worker" \
  && test -f "$SRC/frontend/Dockerfile.release" \
  && test -f "$SRC/pipelines/scheduler.py" \
  && test -f "$SRC/pipelines/postclose_watcher.py" \
  && test -f "$SRC/services/market/intraday_price_feed.py"

echo '== 构建分角色精简镜像 (api / worker / frontend) =='
"$DOCK" build -t "aistock-backend:$SHA" -f "$SRC/infra/docker/Dockerfile.api" "$SRC" 2>&1 | tail -3
"$DOCK" build -t "aistock-worker:$SHA" -f "$SRC/infra/docker/Dockerfile.worker" "$SRC" 2>&1 | tail -3
"$DOCK" build -t "aistock-frontend:$SHA" -f "$SRC/frontend/Dockerfile.release" "$SRC/frontend" 2>&1 | tail -3
"$DOCK" tag "aistock-frontend:$SHA" "aistock-frontend:latest"

echo '== 切换容器（backend + frontend + pipeline-runner + postclose-watcher）=='
export AISTOCK_MAIN_VERSION="$SHA"
"$DC" -f "$APP/infra/docker-compose.yml" up -d backend frontend pipeline-runner postclose-watcher 2>&1 | tail -6

echo '== 清理旧构建缓存（仅保留最近 3 份）=='
ls -1dt "$BUILD_CACHE"/*-src 2>/dev/null | tail -n +4 | xargs -r rm -rf || true

sleep 12
fail=0
for p in '/api/health/check' '/api/market/indices?all=true' '/api/operations/tasks' '/api/market/publication-status' '/api/accounts/overview'; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "http://127.0.0.1:3000$p" || true)
  code=${code:0:3}
  echo "  $p -> $code"
  [ "$code" = '200' ] || fail=1
done
"$DOCK" ps --format '{{.Names}}\t{{.Status}}' | grep -E 'aistock-(backend|frontend|pipeline|postclose-watcher|clickhouse)'
[ "$fail" = 0 ] || { echo 'DEPLOY VERIFY FAILED'; exit 2; }
echo "DEPLOY_OK main=$SHA"
