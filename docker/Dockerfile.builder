# ============================================================
# ai-evo-builder — 构建工具链镜像（在开发机构建，导出到部署机）
#
#   docker build --platform linux/amd64 -t ai-evo-builder:latest -f docker/Dockerfile.builder .
#   docker save ai-evo-builder:latest | gzip > ai-evo-builder.tar.gz
#
# 为什么需要它：部署机是 CentOS 7 且不能下载任何东西，无法在上面装
# Node/pnpm/docker CLI。这里把工具链一次性烘焙进镜像，之后服务器上的
# 每次构建都在这个容器里离线完成（依赖来自仓库内的离线快照）。
#
# 低频变更：只有在需要升级 Node/pnpm/docker CLI 时才重新构建并导出。
# 注意 pnpm 主版本必须与开发机一致——pnpm store 快照的格式与主版本绑定，
# 升级大版本后必须重跑 tools/prepare-offline.sh 重新生成快照。
#
# 运行时目录约定（由 build-service 挂载）：
#   /workspace            —— 构建工作区（含 .wheels/ 与 frontend/.pnpm-store 快照）
#   /cache                —— 跨构建复用缓存
#   /var/run/docker.sock  —— 宿主机 docker daemon（docker build 用）
# ============================================================
FROM --platform=linux/amd64 debian:bookworm-slim

ARG NODE_VERSION=24.18.0
ARG PNPM_VERSION=11.5.2
ARG DOCKER_CLI_VERSION=27.5.1

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils git tar gzip bash tini \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory '*' \
    && git config --system advice.detachedHead false

# Node.js（走 npmmirror 二进制镜像，直接从 nodejs.org 拉取在国内不稳定）
RUN curl -fsSL --retry 5 --retry-all-errors \
        "https://registry.npmmirror.com/-/binary/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        | tar -xJ -C /usr/local --strip-components=1 \
    && node --version

# pnpm 主版本必须与开发机一致（store 快照格式与主版本绑定）
RUN npm install -g "pnpm@${PNPM_VERSION}" --registry=https://registry.npmmirror.com \
    && pnpm --version

# docker CLI（静态二进制）：容器内用它调用宿主 daemon 执行 docker build
RUN curl -fsSL --retry 5 --retry-all-errors \
        "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar xz --strip-components=1 -C /usr/local/bin docker/docker \
    && docker --version

RUN mkdir -p /cache

WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash"]
