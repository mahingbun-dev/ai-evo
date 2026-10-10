#!/bin/bash
set -e
# 离线打包：网络受限时，以已存在的 ai-evo:latest（含 HEAD 全部代码）为基底，
# 仅叠加改动后的 app/ 与前端 dist/，避开拉取 amd64 基础镜像。
# 适用前提：基础镜像 ai-evo:latest 与 redis:7 已存在于本地，且 ai-evo:latest 为当前 HEAD 代码。
# 若想从零构建（不走增量），改用 build-and-export.sh（需网络可拉 python:3.12-slim）。
#
# 注意：本脚本不重新执行 pip install。当 requirements.txt 新增依赖（如 claude-agent-sdk）
# 或 Dockerfile 新增系统依赖（如 Claude Code CLI）时，基底镜像必须先全量重建，
# 否则镜像内缺少新依赖。脚本会在增量叠加前检测并告警。

BASE_IMAGE="ai-evo:latest"
TMP_IMAGE="ai-evo:new"
IMAGE_NAME="ai-evo"
IMAGE_TAG="latest"
PLATFORM="linux/amd64"
OUTPUT_TAR="ai-evo-image.tar"
OUTPUT_BUNDLE="ai-evo-deploy.tar.gz"

echo "========== 1. 校验基底镜像存在 =========="
docker image inspect ${BASE_IMAGE} >/dev/null 2>&1 || { echo "缺少基底镜像 ${BASE_IMAGE}，请先在线构建或确认镜像已存在"; exit 1; }

echo "========== 1.5 检测基底镜像依赖（claude-agent-sdk / Claude Code CLI） =========="
MISSING_DEPS=""
if ! docker run --rm --entrypoint pip ${BASE_IMAGE} show claude-agent-sdk >/dev/null 2>&1; then
    MISSING_DEPS="${MISSING_DEPS} claude-agent-sdk"
fi
if ! docker run --rm --entrypoint sh ${BASE_IMAGE} -c 'command -v claude' >/dev/null 2>&1; then
    MISSING_DEPS="${MISSING_DEPS} claude-cli"
fi
if [ -n "${MISSING_DEPS}" ]; then
    echo "警告：基底镜像缺少依赖:${MISSING_DEPS}"
    echo "  - claude-agent-sdk 缺失：Claude Code Agent 裁判引擎与新增 API 将无法工作"
    echo "  - claude-cli 缺失：Claude Code Agent 裁判引擎无法启动（裁判图引擎不受影响）"
    echo "请使用 build-and-export.sh 全量重建镜像后再离线打包。"
    if [ "${STRICT_DEPS:-0}" = "1" ]; then
        echo "STRICT_DEPS=1，检测到缺失依赖，退出。"
        exit 1
    fi
    echo "继续增量构建（部署后裁判图引擎仍可用）..."
fi

echo "========== 2. 增量叠加代码 (FROM ${BASE_IMAGE}) =========="
cat > incr-Dockerfile <<'DF'
FROM ai-evo:latest
COPY app/ app/
COPY alembic/ alembic/
COPY frontend/dist/ frontend/dist/
DF
docker build --platform ${PLATFORM} -t ${TMP_IMAGE} -f incr-Dockerfile .
rm -f incr-Dockerfile

echo "========== 3. 打好正式 tag =========="
docker tag ${TMP_IMAGE} ${IMAGE_NAME}:${IMAGE_TAG}
docker rmi ${TMP_IMAGE} >/dev/null 2>&1 || true

echo "========== 4. 确认 redis:7 为 amd64 =========="
docker image inspect redis:7 --format '{{.Architecture}}/{{.Os}}' | grep -q 'amd64/linux' || { echo "redis:7 非 amd64，请先拉取"; exit 1; }

echo "========== 5. 导出镜像 =========="
rm -f ${OUTPUT_TAR}
docker save -o ${OUTPUT_TAR} ${IMAGE_NAME}:${IMAGE_TAG} redis:7

echo "========== 6. 打包部署包 =========="
rm -f ${OUTPUT_BUNDLE}
tar czf ${OUTPUT_BUNDLE} ${OUTPUT_TAR} docker-compose.yml
rm -f ${OUTPUT_TAR}

echo "========== 完成 =========="
echo "部署包: ${OUTPUT_BUNDLE}"
echo "大小: $(du -h ${OUTPUT_BUNDLE} | cut -f1)"
echo "提醒：部署后首次启动请执行数据库迁移 alembic upgrade head（新增 claude_code_configs 表与 judge_engine 列）。"
