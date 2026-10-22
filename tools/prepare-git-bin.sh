#!/usr/bin/env bash
# ============================================================
# prepare-git-bin.sh — 生成 git 运行时离线包并上传 GitLab Packages
#
# 插件市场需要 git 命令（plugin_marketplace_service），但运行镜像
# python:3.12-slim 不含 git，构建机（部署机）无外网 apt 源。
# 与 claude-local 同款分发机制：本脚本在有外网的开发机上从与运行镜像
# 同版本的基础镜像提取 git 运行时树（git-local/：bin/git + libexec/git-core
# + 递归 .so 依赖 + templates），打包 git-bin.tar.gz 上传 GitLab Packages；
# 版本与 sha256 记录在 tools/git-bin.json（build-service 构建前按它下载
# 就位，见 build_service.ensure_git_bin）。
#
# 用法：
#   bash tools/prepare-git-bin.sh            # 生成 /tmp/git-bin.tar.gz + tools/git-bin.json
#   bash tools/prepare-git-bin.sh --upload   # 再上传（需 GITLAB_UPLOAD_TOKEN=<有
#                                            # write_package_registry 的令牌>）
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT=$(pwd)
PLATFORM="linux/amd64"
BASE="python:3.12-slim"
PKG="ai-evo-git-bin"
GITLAB_HOST="${GITLAB_HOST:-https://gitlab.example.com}"
GITLAB_PROJECT_ID="${GITLAB_PROJECT_ID:-152361}"
OUT=/tmp/git-bin.tar.gz

echo "=== 1. 在 ${BASE} (${PLATFORM}) 内安装 git 并提取运行时树 ==="
docker rm -f git-prepare >/dev/null 2>&1 || true
docker run -d --platform "$PLATFORM" --name git-prepare "$BASE" sleep 3600 >/dev/null
docker exec git-prepare bash -c '
set -e
apt-get update -qq >/dev/null 2>&1
apt-get install -y --no-install-recommends git >/dev/null 2>&1
STAGE=/tmp/git-local
mkdir -p "$STAGE/bin" "$STAGE/libexec" "$STAGE/lib" "$STAGE/usrshare"
cp -a /usr/bin/git "$STAGE/bin/"
cp -a /usr/lib/git-core "$STAGE/libexec/"
cp -a /usr/share/git-core/templates "$STAGE/usrshare/"
copy_libs() {
  ldd "$1" 2>/dev/null | awk "/=> \//{print \$3} /^\s*\//{print \$1}" | while read -r l; do
    [ -n "$l" ] || continue
    case "$l" in */libc.so*|*/ld-linux*|*/libpthread*|*/libdl*|*/libm.so*) continue ;; esac
    [ -f "$STAGE/lib/$(basename "$l")" ] || cp -aL "$l" "$STAGE/lib/"
  done
}
copy_libs "$STAGE/bin/git"
for f in git-remote-http git-remote-https git-remote-ftps git-http-fetch git-imap-send git-shell; do
  [ -f "$STAGE/libexec/git-core/$f" ] && copy_libs "$STAGE/libexec/git-core/$f"
done
rm -rf "$STAGE"/libexec/git-core/templates/blt 2>/dev/null || true
find "$STAGE" -type f -exec sh -c "file -b \"$1\" 2>/dev/null | grep -q ELF && strip --strip-unneeded \"$1\" 2>/dev/null || true" _ {} \;
GIT_V=$(git --version | awk "{print \$3}")
tar -C /tmp/git-local -czf /tmp/out.tar.gz git-local/
echo "$GIT_V" > /tmp/git-version.txt
'

echo "=== 2. 拷出产物并校验（干净基础镜像内 clone 真实仓库） ==="
docker cp git-prepare:/tmp/out.tar.gz "$OUT"
docker cp git-prepare:/tmp/git-version.txt /tmp/git-version.txt
GIT_VERSION=$(cat /tmp/git-version.txt)
docker rm -f git-prepare >/dev/null

rm -rf git-local && mkdir git-local && tar -C . -xzf "$OUT"
docker run --rm --platform "$PLATFORM" \
    -v "$REPO_ROOT/git-local:/git-runtime:ro" \
    -e PATH=/git-runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    -e GIT_EXEC_PATH=/git-runtime/libexec/git-core \
    -e LD_LIBRARY_PATH=/git-runtime/lib \
    -e GIT_TERMINAL_PROMPT=0 \
    "$BASE" bash -c 'git --version && git init -q /tmp/r && cd /tmp/r && git config user.email t@t && git config user.name t && git commit --allow-empty -qm i && git log --oneline >/dev/null && echo SELFTEST_OK'

echo "=== 3. 写清单 tools/git-bin.json ==="
SHA256=$(shasum -a 256 "$OUT" | cut -d' ' -f1)
VERSION="${SHA256:0:12}"
printf '{\n  "git_version": "%s",\n  "platform": "%s",\n  "sha256": "%s",\n  "version": "%s"\n}\n' \
    "$GIT_VERSION" "$PLATFORM" "$SHA256" "$VERSION" > tools/git-bin.json
cat tools/git-bin.json
echo "产物: $OUT（$(du -sh "$OUT" | cut -f1)）"

if [ "${1:-}" = "--upload" ]; then
    : "${GITLAB_UPLOAD_TOKEN:?--upload 需要设置 GITLAB_UPLOAD_TOKEN（用户:令牌 或 任意有 write_package_registry 的凭据）}"
    echo "=== 4. 上传 GitLab Packages ==="
    BASE="$GITLAB_HOST/api/v4/projects/$GITLAB_PROJECT_ID/packages/generic/$PKG/$VERSION"
    curl -f --user "$GITLAB_UPLOAD_TOKEN" \
        --upload-file "$OUT" "$BASE/git-bin.tar.gz" >/dev/null
    curl -f --user "$GITLAB_UPLOAD_TOKEN" \
        --upload-file tools/git-bin.json "$BASE/git-bin.json" >/dev/null || true
    echo "上传完成: $BASE/git-bin.tar.gz"
else
    echo "上传：GITLAB_UPLOAD_TOKEN=<user:token> ./tools/prepare-git-bin.sh --upload"
fi
