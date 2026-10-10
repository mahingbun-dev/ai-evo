#!/bin/bash
# ============================================================
# upload-offline-snapshot.sh — 上传离线依赖快照到 GitLab Packages
#
# 用途：.wheels/ 与 frontend/.pnpm-store 体积大（数百 MB~1GB），不进 git 仓库。
#   本脚本把快照分片上传到 GitLab Generic Packages；build-service 在构建前
#   检测到快照缺失/与锁文件不一致时，会用 Deploy Token 自动下载同步。
#
# 前置：
#   - 先执行 tools/prepare-offline.sh 生成快照
#   - GITLAB_UPLOAD_TOKEN 环境变量：需要 write_package_registry 权限。
#     项目里已有的 Deploy Token「ci/cd」即可（其 scope 含写权限），
#     也可以用带 api 权限的项目访问令牌。
#
# 用法：
#   GITLAB_UPLOAD_TOKEN=<令牌> ./tools/upload-offline-snapshot.sh
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

GITLAB_HOST="${GITLAB_HOST:-https://gitlab.example.com}"
GITLAB_PROJECT="${GITLAB_PROJECT:-group/project}"
# 项目数字 ID 已确认：Deploy Token 没有 read_project 权限，用路径查 API 会
# 返回 404 Project Not Found，因此这里直接给出 ID，不再依赖路径解析。
GITLAB_PROJECT_ID="${GITLAB_PROJECT_ID:-152361}"
PKG_NAME="${SNAPSHOT_PACKAGE:-ai-evo-offline-snapshot}"
: "${GITLAB_UPLOAD_TOKEN:?需要设置 GITLAB_UPLOAD_TOKEN（需 write_package_registry 权限）}"

# 鉴权方式：GitLab 的 Deploy Token 只认 Basic 鉴权，用 PRIVATE-TOKEN 头会返回 401
# （已实测）。用户名就是创建时显示的那个（形如 gitlab+deploy-token-173），
# 不是敏感信息；项目访问令牌用 Basic 鉴权同样可用，所以统一走 Basic。
GITLAB_UPLOAD_USER="${GITLAB_UPLOAD_USER:-gitlab+deploy-token-173}"
AUTH=(-u "${GITLAB_UPLOAD_USER}:${GITLAB_UPLOAD_TOKEN}")

log_info() { echo -e "\033[0;32m[INFO]\033[0m $1"; }
log_warn() { echo -e "\033[0;33m[WARN]\033[0m $1"; }
log_err()  { echo -e "\033[0;31m[ERROR]\033[0m $1"; exit 1; }

[ -f .offline-snapshot.json ] || log_err "缺少 .offline-snapshot.json，请先执行 tools/prepare-offline.sh"
[ -d .wheels ] || log_err "缺少 .wheels/，请先执行 tools/prepare-offline.sh"
[ -d frontend/.pnpm-store ] || log_err "缺少 frontend/.pnpm-store，请先执行 tools/prepare-offline.sh"

log_info "项目: $GITLAB_PROJECT (ID=$GITLAB_PROJECT_ID)"

# 版本号 = .offline-snapshot.json 的内容哈希前 12 位（内容寻址：锁文件变更即产生新版本）
VERSION=$(python3 -c "
import hashlib,sys
print(hashlib.sha256(open('.offline-snapshot.json','rb').read()).hexdigest()[:12])
")
BASE="$GITLAB_HOST/api/v4/projects/$GITLAB_PROJECT_ID/packages/generic/$PKG_NAME/$VERSION"
log_info "快照版本: $VERSION"

# ------------------------------------------------------------
# 打包（按 100M 分片，控制在 GitLab 单文件上限内）
# ------------------------------------------------------------
STAMP=$(date +%Y%m%d-%H%M%S)
WORK="/tmp/ai-evo-snapshot-$STAMP"
mkdir -p "$WORK"

log_info "打包快照（.wheels/ + frontend/.pnpm-store + .offline-snapshot.json）..."
# COPYFILE_DISABLE=1：macOS 的 bsdtar 默认会为每个文件附带 AppleDouble 元数据
# （形如 ._xxx 的侧文件），打进包里会让体积近乎翻倍，解到 Linux 上则全是无用
# 垃圾（实测：162 个 wheel 解出来变成 324 个文件）。这里显式关掉。
COPYFILE_DISABLE=1 tar czf "$WORK/snapshot.tar.gz" \
    --exclude='.DS_Store' --exclude='._*' \
    .wheels frontend/.pnpm-store .offline-snapshot.json \
    || log_err "打包失败"

cd "$WORK"
split -b 100m -d snapshot.tar.gz snapshot.tar.gz.part-
PARTS=(snapshot.tar.gz.part-*)
SIZE=$(du -sh snapshot.tar.gz | cut -f1)
log_info "分片数: ${#PARTS[@]}，解压前大小: $SIZE"

SHA256=$(python3 -c "
import hashlib,sys
h=hashlib.sha256()
with open('snapshot.tar.gz','rb') as f:
    for c in iter(lambda: f.read(1<<22), b''):
        h.update(c)
print(h.hexdigest())
")

# ------------------------------------------------------------
# 上传分片 + 清单
# ------------------------------------------------------------
log_info "上传到 GitLab Packages..."
for p in "${PARTS[@]}"; do
    for i in 1 2 3; do
        curl -sS --fail -X PUT "${AUTH[@]}" \
            --upload-file "$p" "$BASE/$p" >/dev/null && break
        if [ "$i" = 3 ]; then
            log_err "上传失败: $p"
        fi
        log_warn "$p 第 $i 次上传失败，重试..."
        sleep 3
    done
    log_info "  $p OK"
done

python3 - "$VERSION" "$SHA256" "$STAMP" "${PARTS[@]}" > manifest.json <<'PY'
import json
import sys

version, sha256, stamp, *parts = sys.argv[1:]
json.dump(
    {"version": version, "parts": parts, "sha256": sha256, "generated_at": stamp},
    sys.stdout, indent=2,
)
PY

curl -sS --fail -X PUT "${AUTH[@]}" \
    --upload-file manifest.json "$BASE/manifest.json" >/dev/null
log_info "清单已上传。快照版本: $VERSION"

cd "$ROOT_DIR"
rm -rf "$WORK"

log_info "完成。请提交 .offline-snapshot.json 的变更（如有）"
