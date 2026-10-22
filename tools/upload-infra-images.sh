#!/bin/bash
# ============================================================
# upload-infra-images.sh — 上传基础镜像包到 GitLab Packages
#
# 为什么需要：部署机不能下载外网，但能访问内网 GitLab。所以开发机先把
# infra-images.tar.gz 传到 GitLab Packages，服务器再从那里 curl 下来。
# 与本项目离线快照走的是同一条通道（同理由 tools/upload-offline-snapshot.sh 处理）。
#
# 前置：
#   - 先执行 tools/build-and-export-infra.sh 生成 infra-images.tar.gz
#   - GITLAB_UPLOAD_TOKEN 环境变量（需 write_package_registry 权限）
#
# 用法：
#   GITLAB_UPLOAD_TOKEN=<令牌> ./tools/upload-infra-images.sh
#
# 上传后在服务器上执行（命令见脚本末尾输出）：
#   curl 下载分片 → cat 拼接 → sha256 校验 → tar 解包 → docker load
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

GITLAB_HOST="${GITLAB_HOST:-https://gitlab.example.com}"
GITLAB_PROJECT_ID="${GITLAB_PROJECT_ID:-152361}"
PKG_NAME="${INFRA_PACKAGE:-ai-evo-infra-images}"
FILE="${1:-infra-images.tar.gz}"
CHUNK="${CHUNK:-100m}"

: "${GITLAB_UPLOAD_TOKEN:?需要设置 GITLAB_UPLOAD_TOKEN（需 write_package_registry 权限）}"
# Deploy Token 只认 Basic 鉴权，用 PRIVATE-TOKEN 头会 401（实测）
GITLAB_UPLOAD_USER="${GITLAB_UPLOAD_USER:-gitlab+deploy-token-1}"
AUTH=(-u "${GITLAB_UPLOAD_USER}:${GITLAB_UPLOAD_TOKEN}")

log_info() { echo -e "\033[0;32m[INFO]\033[0m $1"; }
log_warn() { echo -e "\033[0;33m[WARN]\033[0m $1"; }
log_err()  { echo -e "\033[0;31m[ERROR]\033[0m $1"; exit 1; }

[ -f "$FILE" ] || log_err "找不到 $FILE，请先执行 tools/build-and-export-infra.sh"

# 版本号 = 文件内容哈希前 12 位（内容寻址：镜像包变了就是新版本，不会覆盖旧的）
VERSION=$(python3 -c "
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for c in iter(lambda: f.read(1 << 22), b''):
        h.update(c)
print(h.hexdigest()[:12])
" "$FILE")
BASE="$GITLAB_HOST/api/v4/projects/$GITLAB_PROJECT_ID/packages/generic/$PKG_NAME/$VERSION"
log_info "文件: $FILE ($(du -h "$FILE" | cut -f1))"
log_info "版本: $VERSION"
log_info "目标: $PKG_NAME"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log_info "分片并上传..."
cp "$FILE" "$WORK/$FILE"
cd "$WORK"
split -b "$CHUNK" "$FILE" "$FILE.part-"
PARTS=("$FILE.part-"*)
log_info "分片数: ${#PARTS[@]}"

for p in "${PARTS[@]}"; do
    for i in 1 2 3; do
        curl -sS --fail -X PUT "${AUTH[@]}" --upload-file "$p" "$BASE/$p" >/dev/null && break
        if [ "$i" = 3 ]; then
            log_err "上传失败: $p"
        fi
        log_warn "$p 第 $i 次上传失败，重试..."
        sleep 3
    done
    log_info "  $p OK"
done

SHA256=$(python3 -c "
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for c in iter(lambda: f.read(1 << 22), b''):
        h.update(c)
print(h.hexdigest())
" "$FILE")

python3 - "$VERSION" "$SHA256" "${PARTS[@]}" > manifest.json <<'PY'
import json
import sys

version, sha256, *parts = sys.argv[1:]
json.dump({"version": version, "file": "infra-images.tar.gz",
           "sha256": sha256, "parts": parts}, sys.stdout, indent=2)
PY

curl -sS --fail -X PUT "${AUTH[@]}" --upload-file manifest.json "$BASE/manifest.json" >/dev/null
log_info "清单已上传。版本: $VERSION"
echo
echo "========== 服务器侧下载 =========="
echo "部署机不能下载外网、但能访问内网 GitLab，所以在服务器上执行："
echo
echo "  PKG_BASE='${BASE}'"
echo "  curl -sS -u \"\$DEPLOY_USER:\$DEPLOY_TOKEN\" -o manifest.json \"\$PKG_BASE/manifest.json\""
echo "  # 按 manifest 里的 parts 逐片下载、cat 拼接，然后校验并加载："
echo "  echo '${SHA256}  ${FILE}' | sha256sum -c -"
echo "  tar xzf ${FILE} && docker load -i infra-images.tar"
echo
echo "（\$DEPLOY_USER / \$DEPLOY_TOKEN 即 Deploy Token 的用户名与密码）"
echo "注意：拼接顺序必须与 manifest 的 parts 顺序一致。"
echo "=================================="
