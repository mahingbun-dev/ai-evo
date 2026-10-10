#!/bin/bash
# ⚠️ 已被 GitLab CI 取代，仅应急使用
#
# 正常发版走 GitLab CI（push main 自动构建+部署），见 docs/build-pipeline.md。
# 保留本脚本是为了 CI 或 build-service 不可用、需要从开发机手工出包时的旁路手段。
#
# 注意：这条路径依赖开发机联网拉取基础镜像并 pip install，构建结果与 CI 路径
# （离线 wheelhouse + requirements.lock）不保证完全一致。
set -e

IMAGE_NAME="ai-evo"
IMAGE_TAG="latest"
OUTPUT_TAR="ai-evo-image.tar"
OUTPUT_BUNDLE="ai-evo-deploy.tar.gz"

# 目标架构为 linux/amd64（CentOS 7 x86_64）
PLATFORM="linux/amd64"

echo "========== 1. 拉取基础镜像 =========="
docker pull --platform ${PLATFORM} python:3.12-slim
docker pull --platform ${PLATFORM} redis:7
# 重新构建 redis:7（创建新 manifest），避免 Docker Desktop 本地 manifest 缓存损坏
echo "FROM redis:7" | docker build --platform ${PLATFORM} -t redis:7 -f - .
echo ""

echo "========== 2. 构建应用镜像 =========="
docker build --platform ${PLATFORM} -t ${IMAGE_NAME}:${IMAGE_TAG} .
echo ""

echo "========== 3. 导出镜像为 tar =========="
docker save -o ${OUTPUT_TAR} ${IMAGE_NAME}:${IMAGE_TAG} redis:7
echo ""

echo "========== 4. 打包部署包 =========="
tar czf ${OUTPUT_BUNDLE} ${OUTPUT_TAR} docker-compose.yml
rm -f ${OUTPUT_TAR}
echo ""

echo "========== 完成 =========="
echo "部署包: ${OUTPUT_BUNDLE}"
echo "大小: $(du -h ${OUTPUT_BUNDLE} | cut -f1)"
echo ""
echo "部署步骤 (目标服务器 CentOS 7):"
echo "  1. 将 ${OUTPUT_BUNDLE} 传到目标服务器"
echo "  2. 解压: tar xzf ${OUTPUT_BUNDLE}"
echo "  3. 加载镜像: docker load -i ${OUTPUT_TAR}"
echo "  4. 启动: docker compose up -d"
