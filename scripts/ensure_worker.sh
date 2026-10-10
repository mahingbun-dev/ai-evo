#!/bin/bash
# 确保 ai-evo-worker 容器运行，缺失时自动拉起（cron 每5分钟执行）
# 放置路径: /apps/qa-code/ai-evo/scripts/ensure_worker.sh
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
LOG=/apps/qa-code/ai-evo/worker_selfheal.log
MARKER=/apps/qa-code/ai-evo/.deploy-in-progress

# 部署期间跳过：CI 部署会让 worker 优雅退出（Celery warm shutdown，最多 900s），
# 这期间容器处于 Stopping、不出现在 docker ps 里，会被下面的判断误认为"挂了"，
# 于是插入一次 docker compose up -d，与外层部署的 compose 撞车。
# build-service 在 compose up 到校验结束之间会写这个标记文件。
if [ -f "$MARKER" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') 检测到部署进行中（$MARKER），跳过自愈" >> "$LOG"
    exit 0
fi

if ! docker ps --format '{{.Names}}' | grep -q '^ai-evo-worker$'; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ai-evo-worker 不在运行，尝试拉起" >> "$LOG"
    cd /apps/qa-code/ai-evo || exit 1
    docker compose up -d worker >> "$LOG" 2>&1
    sleep 5
    if docker ps --format '{{.Names}}' | grep -q '^ai-evo-worker$'; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ai-evo-worker 已拉起" >> "$LOG"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') ai-evo-worker 拉起失败，请人工排查" >> "$LOG"
    fi
fi
