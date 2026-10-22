#!/bin/bash
# ============================================================
# build-in-container.sh — 在 ai-evo-builder 容器内完成构建
#
# 由 build-service 调用（见 docs/build-pipeline.md）：
#   docker run --rm --name ai-evo-build-<job> \
#     -v <构建工作区>:/workspace \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -e BUILD_SHA=<commit> \
#     ai-evo-builder bash /workspace/tools/build-in-container.sh
#
# 前置：/workspace 已由 build-service 检出到目标 commit，离线快照已同步
# 产出：宿主机 docker daemon 中的 ai-evo:<短sha> 镜像
#       （通过挂载的 docker.sock 直接 build 进宿主 daemon，无需 save/scp）
# ============================================================
set -euo pipefail

cd /workspace

IMAGE_NAME="${IMAGE_NAME:-ai-evo}"
SHA="${BUILD_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
SHORT_SHA="${SHA:0:8}"
IMAGE="${IMAGE_NAME}:${SHORT_SHA}"

# 进度 sidecar：build-service 轮询它更新任务看板
progress() {
    printf '{"stage":"%s","percent":%s,"detail":"%s"}\n' "$1" "$2" "$3" \
        > /workspace/.build-progress.json
}

echo "=== 构建 ${IMAGE} (commit ${SHA}) ==="

# ------------------------------------------------------------
# 1. 前端产物
#    用仓库内的 pnpm store 快照离线安装。
#
#    两个参数都是离线构建的硬性要求，缺一不可：
#
#    --config.trustLockfile=true
#      pnpm 11 默认会在 install 时对锁文件里每个条目向 registry 复核发布时间
#      （minimumReleaseAge 供应链策略）。这一步即使加了 --offline 也会联网，
#      断网环境下直接以 ERR_PNPM_MINIMUM_RELEASE_AGE_VIOLATION 失败。
#      锁文件由团队评审后合入、且快照生成时（开发机、有网）已经过同样的复核，
#      因此构建机这里跳过复核是安全的。用命令行开关而不是写进
#      pnpm-workspace.yaml，是为了不影响开发机上的默认保护。
#
#    依赖的构建脚本策略由 frontend/pnpm-workspace.yaml 的 allowBuilds 显式声明
#      （esbuild / vue-demi 声明为 false）。pnpm 11 默认禁止依赖执行构建脚本且把
#      「有脚本但未表态」当错误，缺了那个声明 install 会以非零码退出、
#      紧接着 `pnpm build` 的依赖状态检查也会失败。
# ------------------------------------------------------------
progress frontend-install 8 "离线安装前端依赖"
echo "--- pnpm install --offline ---"
cd /workspace/frontend
pnpm install --offline --frozen-lockfile \
    --config.trustLockfile=true \
    --store-dir /workspace/frontend/.pnpm-store

progress frontend-build 35 "构建前端 dist"
echo "--- pnpm build ---"
pnpm build

[ -f /workspace/frontend/dist/index.html ] \
    || { echo "!!! 前端构建未产出 dist/index.html"; exit 1; }

# BUILD_FRONTEND_ONLY=1（由 build-service 传入）：变更只涉及 frontend/，
# 跳过应用镜像构建——容器不需要重建，部署阶段只同步 dist 到 nginx 静态根。
BUILD_FRONTEND_ONLY="${BUILD_FRONTEND_ONLY:-0}"
if [ "$BUILD_FRONTEND_ONLY" = "1" ]; then
    progress done 100 "前端构建完成（frontend-only，跳过应用镜像）"
    echo "=== 完成：frontend-only 构建（跳过 docker build）==="
    exit 0
fi

# 前端产物不进 git，但必须随部署包交付给 nginx/静态托管之外的场景，
# docker build 会把它打进镜像（见 Dockerfile 的 COPY frontend/dist/）
progress backend 55 "构建应用镜像"
echo "--- docker build ---"
cd /workspace
docker build --platform linux/amd64 -t "$IMAGE" .

progress done 100 "构建完成"
echo "=== 完成：${IMAGE} ==="

# 供 build-service 收集结果
echo "$IMAGE" > /workspace/.build-image
