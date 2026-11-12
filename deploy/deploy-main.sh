#!/usr/bin/env bash
# AiStock main 唯一部署管道（Owner 2026-11-09 裁决固化；2026-11-09 归位仓库内）
set -euo pipefail
APP='F:/Aistock/app'
DOCK='/c/Program Files/Docker/Docker/resources/bin/docker.exe'
DC='/c/Program Files/Docker/Docker/resources/cli-plugins/docker-compose.exe'

cur=$(git -C "$APP" symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)
if [ "$cur" != 'main' ]; then echo "REFUSE: 当前处于 '$cur'。裁决：只部署 main。"; exit 1; fi
SHA=$(git -C "$APP" rev-parse HEAD)
echo "AISTOCK_MAIN_VERSION=$SHA"
dirty=$(git -C "$APP" status --porcelain=v1 | wc -l)
if [ "$dirty" -gt 0 ]; then echo "提示：main 有 $dirty 项未提交改动（不参与构建）："; git -C "$APP" status --porcelain=v1 | head -5; fi
SRC="F:/Aistock/data/builds/$SHA-src"; rm -rf "$SRC"; mkdir -p "$SRC"
git -C "$APP" archive --format=tar "$SHA" | tar -x -C "$SRC"
test -f "$SRC/Dockerfile.backend" && test -f "$SRC/frontend-react/Dockerfile.release" && test -f "$SRC/scripts/pipeline_scheduler.py" && test -f "$SRC/services/intraday_price_feed.py" && test -f "$SRC/scripts/alert_local.py"
echo '== 构建后端/前端镜像 =='
"$DOCK" build -t "aistock-core-backend:$SHA" -f "$SRC/Dockerfile.backend" "$SRC" 2>&1 | tail -2
"$DOCK" build -t "aistock-core-frontend:$SHA" -f "$SRC/frontend-react/Dockerfile.release" "$SRC/frontend-react" 2>&1 | tail -2
echo '== 切换容器（preview + frontend + pipeline-runner） =='
export AISTOCK_MAIN_VERSION="$SHA"
"$DC" -f "$APP/deploy/docker-compose.main.yml" up -d 2>&1 | tail -4
sleep 15
fail=0
for p in '/api/health/check' '/api/market/indices?all=true' '/api/operations/tasks'; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "http://127.0.0.1:3000$p" || true); code=${code:0:3}
  echo "  $p -> $code"; [ "$code" = '200' ] || fail=1
done
"$DOCK" ps --format '{{.Names}}\t{{.Status}}' | grep -E 'observation|pipeline'
[ "$fail" = 0 ] || { echo 'DEPLOY VERIFY FAILED'; exit 2; }
echo "DEPLOY_OK main=$SHA"
