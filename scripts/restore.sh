#!/usr/bin/env bash
# 备份恢复演练脚本（W6-1，见 docs/build-pipeline.md「备份与恢复」）
#
# 用法: restore.sh <backup.dump> <目标库DSN> [源库DSN]
#
#   backup.dump   scripts/backup.sh db 产出的 pg_dump 自定义格式备份
#   目标库DSN     恢复目标（⚠️ 会被 --clean --if-exists 清空重建，必须使用
#                 专用演练临时库，严禁指向生产/共享开发库）
#   源库DSN       可选；提供时自动比对源库与恢复后目标库的 alembic_version，
#                 不一致以非零码退出。不提供时打印恢复后版本供人工核对。
#
# 交互确认：默认要求人工输入 yes 确认（防误恢复）；自动化场景用
#   ASSUME_YES=1 restore.sh ... 跳过。
#
# 口令只经 DSN 环境参数传入，不落任何脚本/文件。
set -euo pipefail

DUMP_FILE="${1:-}"
TARGET_DSN="${2:-}"
SOURCE_DSN="${3:-}"

log() { echo "[$(date '+%F %T')] $*"; }

usage() {
    echo "用法: $0 <backup.dump> <目标库DSN> [源库DSN]" >&2
    echo "  目标库会被清空重建（pg_restore --clean --if-exists），必须使用专用演练库" >&2
    exit 1
}

[ -n "$DUMP_FILE" ] && [ -n "$TARGET_DSN" ] || usage
[ -f "$DUMP_FILE" ] || { echo "错误: 备份文件不存在: $DUMP_FILE" >&2; exit 1; }
if ! head -c 5 "$DUMP_FILE" | grep -q 'PGDMP'; then
    echo "错误: 不是 pg_dump 自定义格式备份（缺 PGDMP 魔数）: $DUMP_FILE" >&2
    exit 1
fi

for cmd in pg_restore psql; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "错误: 缺少命令 $cmd（请安装 postgresql-client）" >&2; exit 1; }
done

# --- 人工确认（目标库将被清空重建）---
if [ "${ASSUME_YES:-0}" != "1" ]; then
    echo "⚠️  即将把备份恢复到目标库，目标库同名对象会被 DROP 后重建："
    echo "    备份文件: $DUMP_FILE"
    # DSN 可能含口令，只回显 host/db 部分
    echo "    目标库  : $(echo "$TARGET_DSN" | sed -E 's#(://[^:/@]+):[^@]*@#\1:***@#')"
    printf "确认继续? (yes/no): "
    read -r answer
    [ "$answer" = "yes" ] || { log "已取消"; exit 1; }
fi

# --- 恢复 ---
log "开始恢复: pg_restore -> 目标库"
# --clean --if-exists: 恢复前 DROP 已有对象（演练库专用语义）
# --no-owner --no-acl: 演练库账号与导出账号不同，跳过 owner/权限还原
# 退出码语义：0=完全成功；1=有被跳过的错误（典型：pg 工具 17+ 对旧版服务器
# 发 SET transaction_timeout 报 unrecognized parameter，数据本身不受影响），
# 继续交给后面的 alembic_version 校验裁决完整性；>=2 才是致命失败。
set +e
pg_restore --clean --if-exists --no-owner --no-acl -d "$TARGET_DSN" "$DUMP_FILE"
restore_rc=$?
set -e
if [ "$restore_rc" -ge 2 ]; then
    echo "错误: pg_restore 致命失败 (exit=$restore_rc)" >&2
    exit 1
fi
[ "$restore_rc" -eq 1 ] && log "警告: pg_restore 存在被跳过的错误（详见上方输出），以 alembic_version 校验结果为准"
log "恢复完成"

# --- 校验 alembic_version ---
restored_version="$(psql "$TARGET_DSN" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || true)"
if [ -z "$restored_version" ]; then
    echo "错误: 恢复后目标库查不到 alembic_version（迁移链不完整?）" >&2
    exit 1
fi
log "恢复后目标库 alembic_version = $restored_version"

if [ -n "$SOURCE_DSN" ]; then
    source_version="$(psql "$SOURCE_DSN" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || true)"
    if [ -z "$source_version" ]; then
        echo "错误: 源库查不到 alembic_version" >&2
        exit 1
    fi
    log "源库当前 alembic_version      = $source_version"
    if [ "$restored_version" != "$source_version" ]; then
        echo "错误: alembic_version 不一致（恢复后=$restored_version, 源库=$source_version）" >&2
        exit 1
    fi
    log "✓ alembic_version 比对一致，恢复演练通过"
else
    log "未提供源库DSN，请人工核对恢复后版本与源库一致"
fi
