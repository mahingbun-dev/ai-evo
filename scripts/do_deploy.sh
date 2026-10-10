#!/bin/bash
# ⚠️ 已被 GitLab CI 取代，仅应急使用
#
# 正常发版走 GitLab CI（push main 自动构建+部署），见 docs/build-pipeline.md。
# 保留本脚本是为了 CI 或 build-service 不可用时的旁路手段。
#
# 注意：本脚本会清空 Redis 里的 Celery 队列（第 4 步），正在执行与排队中的
# 评测任务会丢失；CI 流程已改为依靠 worker 的 stop_grace_period 优雅退出。
set -e
cd /apps/qa-code/ai-evo

echo "=== 1. Copy deploy package ==="
cp /apps/qa-code/anc/deploy/uploads/ai-evo-deploy.tar.gz .

echo "=== 2. Extract ==="
tar xzf ai-evo-deploy.tar.gz

echo "=== 3. Load Docker image ==="
docker load -i ai-evo-image.tar

echo "=== 4. Clear old Redis queues ==="
docker exec ai-evo-redis redis-cli -n 0 DEL celery || true
docker exec ai-evo-redis redis-cli -n 0 EVAL "for _,k in ipairs(redis.call('KEYS','task_stop:*')) do redis.call('DEL',k) end" 0 || true

echo "=== 5. Restart containers ==="
docker compose up -d --force-recreate

echo "=== 6. Verify ==="
sleep 3
docker ps -a | grep ai-evo

echo "=== 7. Check worker alive ==="
if ! docker ps --format '{{.Names}}' | grep -q '^ai-evo-worker$'; then
    echo "!!! ai-evo-worker 未运行，尝试拉起"
    docker compose up -d worker
    sleep 5
fi
if docker ps --format '{{.Names}}' | grep -q '^ai-evo-worker$'; then
    echo "ai-evo-worker: OK"
else
    echo "!!! ai-evo-worker 拉起失败，请人工排查: docker logs ai-evo-worker"
fi
echo "=== Done ==="
