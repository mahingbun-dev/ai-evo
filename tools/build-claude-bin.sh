#!/bin/bash
# ============================================================
# build-claude-bin.sh — 生成离线 claude CLI 产物（开发机执行，需联网）
#
# 背景：部署机无外网，应用镜像 Dockerfile 里不能在线安装 claude。
# 本脚本在 linux/amd64 容器内安装 Claude Code CLI 并打包，上传
# GitLab Packages（包名 ai-evo-claude-bin，版本 = tar 内容哈希前 12 位）。
# 服务器构建时由 build-service 下载解压到工作区 claude-local/ 供
# Dockerfile COPY（版本与 sha256 记录在 tools/claude-bin.json）。
#
# 何时需要重跑：升级 claude CLI 版本时。
#
# 用法：
#   ./tools/build-claude-bin.sh          # 生成 + 写 tools/claude-bin.json
#   GITLAB_UPLOAD_TOKEN=<令牌> ./tools/build-claude-bin.sh --upload
#     （生成、上传、并提示提交 claude-bin.json）
#
# 依赖：Docker（--platform linux/amd64 模拟）、代理可选（PROXY 环境变量，
#   如 PROXY=http://host.docker.internal:7897，容器内访问 claude.ai 用）
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PROXY="${PROXY:-}"
OUT_DIR="$(mktemp -d /tmp/claude-bin.XXXXXX)"
trap 'rm -rf "$OUT_DIR"' EXIT

log_info() { echo -e "\033[0;32m[INFO]\033[0m $1"; }
log_err()  { echo -e "\033[0;31m[ERROR]\033[0m $1"; exit 1; }

PROXY_ARGS=()
[ -n "$PROXY" ] && PROXY_ARGS=(-e "HTTPS_PROXY=$PROXY" -e "HTTP_PROXY=$PROXY")

log_info "在 linux/amd64 容器内安装 claude CLI..."
docker run --rm --platform linux/amd64 -v "$OUT_DIR:/out" "${PROXY_ARGS[@]}" \
    python:3.12-slim sh -c "
set -e
apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates >/dev/null
curl -fsSL https://claude.ai/install.sh | bash >/dev/null
cp -a /root/.local /out/claude-local
cd /out/claude-local
# 绝对路径符号链接会被 docker build 上下文拒绝（'is a link to an absolute
# path'，2026-10-09 实测），统一转为包内相对链接
for l in \$(find . -type l); do
  t=\$(readlink \"\$l\")
  case \"\$t\" in /root/.local/*) ln -sf \"../\${t#/root/.local/}\" \"\$l\";; esac
done
/out/claude-local/bin/claude --version
"

[ -x "$OUT_DIR/claude-local/bin/claude" ] || log_err "安装产物缺少 bin/claude"
CLAUDE_VERSION=$("$OUT_DIR/claude-local/bin/claude" --version 2>/dev/null | awk '{print $1}')
[ -n "$CLAUDE_VERSION" ] || log_err "无法读取 claude 版本号"

log_info "打包 claude-bin.tar.gz..."
COPYFILE_DISABLE=1 tar czf "$OUT_DIR/claude-bin.tar.gz" -C "$OUT_DIR" claude-local

SHA256=$(sha256sum "$OUT_DIR/claude-bin.tar.gz" | awk '{print $1}')
VERSION="${SHA256:0:12}"

log_info "claude ${CLAUDE_VERSION}，快照版本 ${VERSION}，sha256 ${SHA256}"

cat > tools/claude-bin.json <<EOF
{
  "claude_version": "$CLAUDE_VERSION",
  "platform": "linux/amd64",
  "sha256": "$SHA256",
  "version": "$VERSION"
}
EOF
log_info "已更新 tools/claude-bin.json"

if [ "${1:-}" = "--upload" ]; then
    : "${GITLAB_UPLOAD_TOKEN:?--upload 需要设置 GITLAB_UPLOAD_TOKEN}"
    GITLAB_HOST="${GITLAB_HOST:-https://gitlab.example.com}"
    GITLAB_PROJECT_ID="${GITLAB_PROJECT_ID:-152361}"
    GITLAB_UPLOAD_USER="${GITLAB_UPLOAD_USER:-gitlab+deploy-token-173}"
    BASE="$GITLAB_HOST/api/v4/projects/$GITLAB_PROJECT_ID/packages/generic/ai-evo-claude-bin/$VERSION"
    curl -sS --fail -u "$GITLAB_UPLOAD_USER:$GITLAB_UPLOAD_TOKEN" \
        --upload-file "$OUT_DIR/claude-bin.tar.gz" "$BASE/claude-bin.tar.gz" >/dev/null
    curl -sS --fail -u "$GITLAB_UPLOAD_USER:$GITLAB_UPLOAD_TOKEN" \
        --upload-file "$OUT_DIR/claude-bin.tar.gz.sha256" "$BASE/claude-bin.tar.gz.sha256" >/dev/null || true
    log_info "已上传 $BASE/claude-bin.tar.gz"
    log_info "下一步：git add tools/claude-bin.json && git commit（服务器构建将自动下载新版本）"
else
    log_info "上传：GITLAB_UPLOAD_TOKEN=<令牌> ./tools/build-claude-bin.sh --upload"
fi
