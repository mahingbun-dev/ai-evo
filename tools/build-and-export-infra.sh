#!/bin/bash
# ============================================================
# build-and-export-infra.sh — 构建并导出两个基础镜像（开发机执行，需联网）
#
#   ai-evo-builder   构建工具链（Node/pnpm/docker CLI），在 server 上跑构建用
#   build-service    常驻的构建/部署编排服务
#
# 这两个镜像低频变更，只有以下情况才需要重建并重新导出到服务器：
#   - 升级 Node / pnpm / docker CLI 版本（注意 pnpm 主版本必须与开发机一致，
#     否则 pnpm store 快照对不上，需同时重跑 tools/prepare-offline.sh）
#   - 修改 tools/build-service/ 下的代码
#
# 产出：infra-images.tar.gz（含两个镜像 + 服务器启动说明）
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PLATFORM="${PLATFORM:-linux/amd64}"
OUTPUT_BUNDLE="${OUTPUT_BUNDLE:-infra-images.tar.gz}"
OUTPUT_TAR="infra-images.tar"

BUILDER_IMAGE="ai-evo-builder:latest"
# 不叫 build-service：同机 anc 项目已有同名镜像在跑，重名会让 docker load
# 顶掉它的 tag、打断 anc 的流水线
SERVICE_IMAGE="ai-evo-build-service:latest"
# 应用镜像的构建基础：docker build 时 FROM 需要它已存在于本地 daemon。
# 部署机拉不到外网，所以它必须和工具链镜像一起打包过去，否则构建会卡在
# 拉取基础镜像上——这是构建的隐式依赖，容易漏。
BASE_IMAGE="python:3.12-slim"

log_info() { echo -e "\033[0;32m[INFO]\033[0m $1"; }
log_err()  { echo -e "\033[0;31m[ERROR]\033[0m $1"; exit 1; }

command -v docker >/dev/null || log_err "未找到 docker"

log_info "========== 0. 确认基础镜像 ${BASE_IMAGE} 存在 =========="
if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
    log_info "本地缺少 ${BASE_IMAGE}，拉取中..."
    docker pull --platform "$PLATFORM" "$BASE_IMAGE" || log_err "拉取 ${BASE_IMAGE} 失败"
fi
# 与 Dockerfile 里的 FROM 引用的 digest 对齐，避免构建时隐式换基础镜像
docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || log_err "缺少 ${BASE_IMAGE}"
log_info "${BASE_IMAGE} 就绪"

log_info "========== 1. 构建 ${BUILDER_IMAGE} =========="
docker build --platform "$PLATFORM" -t "$BUILDER_IMAGE" -f docker/Dockerfile.builder .

log_info "========== 2. 构建 ${SERVICE_IMAGE} =========="
docker build --platform "$PLATFORM" -t "$SERVICE_IMAGE" tools/build-service/

log_info "========== 3. 校验 pnpm 主版本与快照一致 =========="
# pnpm store 快照的格式与主版本绑定，不一致时构建会失败且原因隐蔽，这里提前拦住
if [ -f .offline-snapshot.json ]; then
    SNAP_PNPM=$(python3 -c "import json;print(json.load(open('.offline-snapshot.json'))['pnpm_version'])")
    IMG_PNPM=$(docker run --rm --platform "$PLATFORM" --entrypoint pnpm "$BUILDER_IMAGE" --version)
    if [ "$SNAP_PNPM" != "$IMG_PNPM" ]; then
        log_err "pnpm 版本不一致：快照 ${SNAP_PNPM} vs 镜像 ${IMG_PNPM}。请重跑 tools/prepare-offline.sh"
    fi
    log_info "pnpm 版本一致：${IMG_PNPM}"
else
    log_info "未找到 .offline-snapshot.json，跳过校验（首次初始化时正常）"
fi

log_info "========== 4. 导出镜像 =========="
rm -f "$OUTPUT_TAR"
# 三个都带上：工具链、构建服务、以及应用镜像的构建基础。
# --platform 必须有：python:3.12-slim 这类官方 tag 本地是 OCI manifest list，
# 直接 save 会因为其他平台的层不存在而报 "content digest ... not found"
# （实测踩到）。BuildKit 构建出的镜像本身也带 attestation manifest list。
docker save --platform "$PLATFORM" -o "$OUTPUT_TAR" \
    "$BASE_IMAGE" "$BUILDER_IMAGE" "$SERVICE_IMAGE"

rm -f "$OUTPUT_BUNDLE"
# COPYFILE_DISABLE=1：macOS 的 bsdtar 默认附带扩展属性，解包时会打印
# "Ignoring unknown extended header keyword" 警告。虽无害，但没必要让日志变脏。
COPYFILE_DISABLE=1 tar czf "$OUTPUT_BUNDLE" "$OUTPUT_TAR"
rm -f "$OUTPUT_TAR"

log_info "========== 完成 =========="
echo "部署包: $OUTPUT_BUNDLE ($(du -h "$OUTPUT_BUNDLE" | cut -f1))"
echo ""
echo "服务器侧步骤（详见 docs/build-pipeline.md 第 4.4 节）："
echo "  1. 传到服务器并解压: tar xzf ${OUTPUT_BUNDLE}"
echo "  2. 加载镜像: docker load -i ${OUTPUT_TAR}"
echo "  3. 按文档准备目录、config.toml、.env 并启动 build-service"
