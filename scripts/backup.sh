#!/usr/bin/env bash
# 数据库与补传区备份脚本（W6-1，见 docs/build-pipeline.md「备份与恢复」）
#
# 两种模式（对应部署机 crontab 两条目）：
#   backup.sh db       每日 02:00 —— pg_dump 自定义格式全量备份 PostgreSQL
#   backup.sh uploads  每周日 03:00 —— tar 全量归档 uploads/hub_supplements/
#                      （人工补传文件无法从 anc-hub 幂等重建，必须备份）
#
# 环境变量（在部署目录 .env 或 crontab 行内注入）：
#   DATABASE_URL   必填（仅 db 模式）—— PostgreSQL 连接串
#   BACKUP_DIR     备份输出根目录，默认 ./backups（相对部署目录）
#   UPLOADS_DIR    上传产物目录，默认 ./uploads（与 compose 卷挂载一致）
#   RETENTION_DAYS DB 备份保留天数，默认 14（-mtime +N 语义：删除第 N+1 天前的文件）
#   UPLOADS_RETENTION_DAYS  补传区归档保留天数，默认 14
#
# 口令只经 DATABASE_URL 环境变量传入，不落任何脚本/文件。
set -euo pipefail

MODE="${1:-}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
UPLOADS_DIR="${UPLOADS_DIR:-./uploads}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
UPLOADS_RETENTION_DAYS="${UPLOADS_RETENTION_DAYS:-14}"

STAMP="$(date +%Y%m%d_%H%M%S)"
DB_SUBDIR="$BACKUP_DIR/db"
UPLOADS_SUBDIR="$BACKUP_DIR/hub_supplements"

log() { echo "[$(date '+%F %T')] $*"; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || { echo "错误: 缺少命令 $1（部署机请安装 postgresql-client）" >&2; exit 1; }
}

backup_db() {
    if [ -z "${DATABASE_URL:-}" ]; then
        echo "错误: 未设置 DATABASE_URL（db 模式必填，经环境变量注入）" >&2
        exit 1
    fi
    require_cmd pg_dump
    mkdir -p "$DB_SUBDIR"
    local out="$DB_SUBDIR/db_${STAMP}.dump"
    log "开始数据库备份: pg_dump -Fc -> $out"
    pg_dump "$DATABASE_URL" -Fc -f "$out"
    # 备份产物必须非空且是 pg_dump 自定义格式（magic: PGDMP）
    if [ ! -s "$out" ]; then
        echo "错误: 备份文件为空: $out" >&2
        exit 1
    fi
    if ! head -c 5 "$out" | grep -q 'PGDMP'; then
        echo "错误: 备份文件缺少 pg_dump 自定义格式魔数（PGDMP）: $out" >&2
        exit 1
    fi
    log "数据库备份完成: $out ($(du -h "$out" | cut -f1))"
    # 备份成功后才清理过期备份（find -mtime +N = 修改时间在 N+1 天之前）
    find "$DB_SUBDIR" -name 'db_*.dump' -type f -mtime "+$RETENTION_DAYS" -print -delete \
        | sed 's/^/清理过期备份: /' || true
    log "DB 备份保留策略: ${RETENTION_DAYS} 天"
}

backup_uploads() {
    local src="$UPLOADS_DIR/hub_supplements"
    if [ ! -d "$src" ]; then
        # 目录为空/不存在不算失败：首次部署或尚无补传（compose 卷挂载 ./uploads）
        log "目录不存在，跳过补传区备份: $src"
        return 0
    fi
    require_cmd tar
    mkdir -p "$UPLOADS_SUBDIR"
    local out="$UPLOADS_SUBDIR/hub_supplements_${STAMP}.tar.gz"
    log "开始补传区全量归档: $src -> $out"
    tar -czf "$out" -C "$UPLOADS_DIR" hub_supplements
    if [ ! -s "$out" ]; then
        echo "错误: 归档文件为空: $out" >&2
        exit 1
    fi
    log "补传区归档完成: $out ($(du -h "$out" | cut -f1))"
    find "$UPLOADS_SUBDIR" -name 'hub_supplements_*.tar.gz' -type f -mtime "+$UPLOADS_RETENTION_DAYS" -print -delete \
        | sed 's/^/清理过期归档: /' || true
    log "补传区归档保留策略: ${UPLOADS_RETENTION_DAYS} 天"
}

case "$MODE" in
    db)      backup_db ;;
    uploads) backup_uploads ;;
    "")
        echo "用法: $0 {db|uploads}" >&2
        echo "  db      每日 crontab 用：pg_dump -Fc 全量备份（DATABASE_URL 必填）" >&2
        echo "  uploads 每周日 crontab 用：tar 全量归档 uploads/hub_supplements/" >&2
        exit 1
        ;;
    *)
        echo "错误: 未知模式 '$MODE'（仅支持 db / uploads）" >&2
        exit 1
        ;;
esac
