#!/bin/bash
# ============================================================
# prepare-offline.sh — 生成离线依赖快照（在开发机执行，需联网 + Docker）
#
# 产出：
#   requirements.lock          全量钉死的依赖清单（= 现网运行的依赖集合）
#   .wheels/                   全部 Python wheel（含由 sdist 预构建的纯 Python 包）
#   frontend/.pnpm-store/      前端依赖的 pnpm 内容寻址存储
#   .offline-snapshot.json     快照元数据（锁文件哈希），供构建服务校验一致性
#
# 何时需要重跑：
#   - requirements.txt 或 frontend/pnpm-lock.yaml 变更后
#   - 有意升级依赖后（先调整 tools/prod-constraints.txt 中对应包的行）
#
# 重跑后必须执行 tools/upload-offline-snapshot.sh 上传到 GitLab Packages，
# 否则服务器构建时会因快照与锁文件哈希不一致而被拒绝。
#
# 除 requirements.lock 与 .offline-snapshot.json 外，其余产物都不入库
# （体积大，走 GitLab Packages 分发，与 anc 项目同构）。
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PY_IMAGE="${PY_IMAGE:-python:3.12-slim}"
NODE_IMAGE="${NODE_IMAGE:-node:24-bookworm-slim}"
PLATFORM="${PLATFORM:-linux/amd64}"
# 必须与 docker/Dockerfile.builder 里的 PNPM_VERSION 一致：
# pnpm store 的格式与主版本绑定，不一致时构建容器无法复用快照
PNPM_VERSION="${PNPM_VERSION:-11.5.2}"

log_info() { echo -e "\033[0;32m[INFO]\033[0m $1"; }
log_err()  { echo -e "\033[0;31m[ERROR]\033[0m $1"; exit 1; }

command -v docker >/dev/null || log_err "未找到 docker"
command -v python3 >/dev/null || log_err "未找到 python3"

# ------------------------------------------------------------
# 1. Python 依赖：生成 requirements.lock 并下载全部 wheel
#
#    必须在目标容器镜像内执行：pip 的 --python-version 只影响 wheel 兼容标签，
#    不影响依赖 marker 的求值环境。在 macOS（Python 3.14）上直接跑
#    `pip download --python-version 3.12` 会因为 marker 仍按 3.14 求值而解析出
#    错误的版本组合（实测：pandas 3.0.0 会要求 numpy>=2.3.3，与 CPU 兼容性
#    所需的 numpy==2.2.6 冲突）。
# ------------------------------------------------------------
log_info "在 ${PY_IMAGE} 内生成 requirements.lock 并下载 wheel..."
rm -rf .wheels
mkdir -p .wheels

docker run --rm --platform "$PLATFORM" \
    -v "$ROOT_DIR/requirements.txt:/src/requirements.txt:ro" \
    -v "$ROOT_DIR/tools/prod-constraints.txt:/src/prod-constraints.txt:ro" \
    -v "$ROOT_DIR/tools/offline-constraints.txt:/src/offline-constraints.txt:ro" \
    -v "$ROOT_DIR/.wheels:/wheels" \
    "$PY_IMAGE" sh -c '
set -e
pip install -q --no-cache-dir pip-tools
pip-compile -q --no-emit-index-url --strip-extras \
    -c /src/prod-constraints.txt -c /src/offline-constraints.txt \
    -o /wheels/requirements.lock /src/requirements.txt
pip download -r /wheels/requirements.lock -d /wheels --no-cache-dir --progress-bar off

# sdist-only 的包（例如 jsonpath==0.82.2）没有 wheel，pip download 只能拿到
# .tar.gz。它们都是纯 Python 包，就地预先构建成 wheel，这样构建镜像时
# 不需要任何编译工具链，也彻底避免了离线环境下的构建隔离失败。
cd /wheels
for tar in *.tar.gz; do
    [ -e "$tar" ] || continue
    echo "预构建 sdist: $tar"
    pip wheel --no-deps --no-cache-dir -w /wheels "$tar" >/dev/null
    rm -f "$tar"
done

# 构建隔离可能用到的工具（离线安装时从 wheelhouse 取）
pip download setuptools wheel -d /wheels --no-cache-dir --progress-bar off >/dev/null
'

mv .wheels/requirements.lock requirements.lock

WHEEL_COUNT=$(find .wheels -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')
[ "$WHEEL_COUNT" -gt 0 ] || log_err "wheelhouse 为空，生成失败"
REMAIN_SDIST=$(find .wheels -maxdepth 1 -name '*.tar.gz' | wc -l | tr -d ' ')
[ "$REMAIN_SDIST" -eq 0 ] || log_err "仍有 ${REMAIN_SDIST} 个 sdist 未转成 wheel"
log_info "wheelhouse 完成：${WHEEL_COUNT} 个 wheel，共 $(du -sh .wheels | cut -f1)"

# ------------------------------------------------------------
# 2. 前端依赖：抓取到 pnpm 内容寻址存储
#
#    必须在 linux/amd64 容器内执行，而不是在开发机（macOS）上直接 fetch：
#    pnpm 默认只抓当前平台的可选依赖，在 macOS 上生成的 store 里只有
#    @esbuild/darwin-arm64，拿到 linux 构建容器里执行 `pnpm install --offline`
#    会缺原生二进制（实测确认）。
#
#    在容器里做还有两个好处：
#      - 不需要给前端加 supportedArchitectures 配置，仓库保持原样；
#      - 开发机本地的 node_modules 与 pnpm 配置完全不受影响
#        （pnpm 11 会因为配置变化要求清理 node_modules，非交互下直接失败）。
#
#    注意 pnpm 主版本必须与 ai-evo-builder 镜像里的一致——store 格式与主版本绑定。
# ------------------------------------------------------------
log_info "在 ${NODE_IMAGE} 内抓取前端依赖到 pnpm store..."
rm -rf frontend/.pnpm-store
mkdir -p frontend/.pnpm-store

docker run --rm --platform "$PLATFORM" \
    -v "$ROOT_DIR/frontend/package.json:/src/package.json:ro" \
    -v "$ROOT_DIR/frontend/pnpm-lock.yaml:/src/pnpm-lock.yaml:ro" \
    -v "$ROOT_DIR/frontend/.pnpm-store:/store" \
    "$NODE_IMAGE" sh -c "
set -e
npm install -g pnpm@${PNPM_VERSION} --registry=https://registry.npmmirror.com >/dev/null 2>&1
mkdir -p /work
cp /src/package.json /src/pnpm-lock.yaml /work/
cd /work
pnpm fetch --store-dir /store
"

# 校验快照里确实含 linux 平台的原生二进制——缺了的话构建容器必然失败，
# 在这里拦住比让服务器上的构建失败好排查
if ! grep -qa "@esbuild/linux-x64" frontend/.pnpm-store/v*/index.db 2>/dev/null; then
    log_err "pnpm store 中未找到 @esbuild/linux-x64，快照无法用于 linux 构建"
fi
if ! grep -qa "@rollup/rollup-linux-x64" frontend/.pnpm-store/v*/index.db 2>/dev/null; then
    log_err "pnpm store 中未找到 @rollup/rollup-linux-x64，快照无法用于 linux 构建"
fi
log_info "pnpm store 完成：$(du -sh frontend/.pnpm-store | cut -f1)（已确认含 linux/x64 原生二进制）"

# ------------------------------------------------------------
# 3. 写快照元数据
#
#    构建服务读取它，与构建工作区里的锁文件哈希逐一比对，不一致就拒绝构建，
#    避免「改了依赖却忘了重跑本脚本 / 忘了上传快照」造成隐蔽的版本不一致。
# ------------------------------------------------------------
log_info "写入 .offline-snapshot.json..."
python3 - "$PNPM_VERSION" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

def sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

meta = {
    "python_version": "3.12",
    "platform": "linux/amd64",
    "pnpm_version": sys.argv[1],
    "requirements_lock_hash": sha256("requirements.lock"),
    "pnpm_lock_hash": sha256("frontend/pnpm-lock.yaml"),
    "wheel_count": len(list(Path(".wheels").glob("*.whl"))),
}
Path(".offline-snapshot.json").write_text(
    json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print(json.dumps(meta, indent=2, sort_keys=True))
PY

log_info "完成。下一步：tools/upload-offline-snapshot.sh 上传快照"
