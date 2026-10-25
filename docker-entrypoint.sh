#!/bin/bash
set -e

# ARCH-0-1：主镜像以 uid/gid 10002（app）+ cap_drop ALL 运行，进程自身无法 chown。
# uploads/logs 两个写面是运行时挂卷，属主不跟随镜像：bind 挂载目录由宿主决定
# （docker 自动创建的缺失目录是 root:root，历史部署的内容也是 root 写入的），
# 命名卷只在首次创建时按镜像目录初始化。属主迁移因此落在部署链路里而不是只写
# 进部署文档（文档步骤会被跳过，CI 自动部署与群晖经代理工具 compose 的场景也
# 没有"登录宿主 chown"的机会）：
#   - compose 的 volume-init 一次性服务以 root（仅 CHOWN/DAC_OVERRIDE 能力）跑
#     本脚本的 volume-init 分支，把两个卷的属主纠正为 10002 后退出；已是 10002
#     时零写入秒退。api/worker 通过 depends_on 等它完成后再起；
#   - api/worker 分支启动前再做可写预检：卷不可写时立刻退出并打印修复命令，
#     而不是起来之后在第一次写日志/上传时抛 PermissionError。
APP_UID=10002
APP_GID=10002
DATA_DIRS="/app/uploads /app/logs"

migrate_volume_ownership() {
  if [ "${EUID}" != "0" ]; then
    echo "[volume-init] 警告: 当前 uid ${EUID} 非 root，无法迁移属主（compose 需 user: \"0:0\"）" >&2
  fi
  for dir in ${DATA_DIRS}; do
    if [ ! -d "${dir}" ]; then
      echo "[volume-init] 跳过 ${dir}：目录不存在（未挂卷？）"
      continue
    fi
    # 存在任一非 10002 属主条目才递归 chown（find -quit 首个命中即短路；
    # 全部已是 10002 时只读遍历、不产生写入）
    if find "${dir}" ! -user "${APP_UID}" -print -quit 2>/dev/null | grep -q .; then
      echo "[volume-init] ${dir} 存在非 uid ${APP_UID} 属主条目，递归修正属主 ..."
      if chown -R "${APP_UID}:${APP_GID}" "${dir}"; then
        echo "[volume-init] ${dir} 属主已迁移为 ${APP_UID}:${APP_GID}"
      else
        # 不阻断启动：SMB/NFS 等卷可能禁止 chown，但按挂载 uid 映射本就可写；
        # 真实可写性由 api/worker 的预检把关
        echo "[volume-init] 警告: ${dir} chown 失败（文件系统不支持？），交由主服务可写预检判定" >&2
      fi
    else
      echo "[volume-init] ${dir} 属主已是 ${APP_UID}，无需迁移"
    fi
  done
}

require_writable_data_dirs() {
  local failed=""
  for dir in ${DATA_DIRS}; do
    if [ ! -d "${dir}" ] || [ ! -w "${dir}" ]; then
      failed="${failed} ${dir}"
    fi
  done
  if [ -n "${failed}" ]; then
    echo "[entrypoint] 错误: 以 uid ${EUID} 运行，以下挂卷目录不可写:${failed}" >&2
    echo "[entrypoint] 容器内无法自行 chown（非 root + cap_drop ALL）。修复方式：" >&2
    echo "[entrypoint]   a) docker compose up -d —— volume-init 服务会先迁移卷属主再起本服务" >&2
    echo "[entrypoint]   b) 手动：宿主执行 chown -R ${APP_UID}:${APP_GID} <uploads 目录> <logs 目录>" >&2
    echo "[entrypoint]      命名卷：docker run --rm -v <卷名>:/d alpine chown -R ${APP_UID}:${APP_GID} /d" >&2
    exit 1
  fi
}

# 数据库迁移（P0-A，降低使用门槛）：alembic upgrade head，失败则启动失败并给出
# 明确日志——宁可不启动，不静默残废（此前 entrypoint 只起 uvicorn，迁移靠手工
# docker exec，自托管用户首次部署普遍卡在「起来即 503」）。
#
# 并发防护：与 compose 的 migrate 一次性服务（api depends_on 其
# service_completed_successfully）双机制并存——本函数先取 PG 会话级 advisory
# lock（key 0xA1E0D0，十进制 10610000）串行化并发迁移；api 在 compose 形态下
# 由 migrate 服务先行迁移完成，此处实际是幂等 no-op 兜底（多 api 副本 / 不经
# compose 直起容器时仍能自迁移）。bundled-db 与外部数据库两种形态同样适用
# （连接串一律来自 DATABASE_URL 环境变量，alembic/env.py 原生支持）。
run_db_migrations() {
  echo "[entrypoint] 执行数据库迁移 alembic upgrade head ..."
  if python -c "
import os, sys, subprocess
from sqlalchemy import create_engine, text

url = os.environ.get('AIEVO_DATABASE_URL') or os.environ.get('DATABASE_URL') or ''
if not url:
    print('[migrate] 错误: DATABASE_URL 未配置，无法执行迁移', file=sys.stderr)
    sys.exit(2)

engine = create_engine(url)
with engine.connect() as conn:
    conn.execute(text('SELECT pg_advisory_lock(10610000)'))
    print('[migrate] 已获取迁移 advisory lock，执行 alembic upgrade head ...')
    sys.stdout.flush()
    rc = subprocess.call(['alembic', 'upgrade', 'head'])
    conn.execute(text('SELECT pg_advisory_unlock(10610000)'))
sys.exit(rc)
"; then
    echo "[entrypoint] 数据库迁移完成（库已是最新时为 no-op）"
  else
    echo "[entrypoint] 错误: 数据库迁移失败，服务拒绝启动（宁可不启动，不静默残废）" >&2
    echo "[entrypoint] 排查步骤:" >&2
    echo "[entrypoint]   1) 确认 .env 的 DATABASE_URL 指向可达的 PostgreSQL（bundled-db 形态需 COMPOSE_PROFILES=bundled-db）" >&2
    echo "[entrypoint]   2) 查看一次性迁移服务日志: docker compose logs migrate" >&2
    echo "[entrypoint]   3) 手工重试: docker compose run --rm migrate" >&2
    exit 1
  fi
}

case "${SERVICE_TYPE}" in
  volume-init)
    # ARCH-0-1：compose 一次性服务（user: "0:0"，cap 仅 CHOWN/DAC_OVERRIDE），
    # 迁移完退出 0，api/worker depends_on 其 service_completed_successfully
    migrate_volume_ownership
    ;;
  migrate)
    # P0-A：compose 一次性迁移服务（api depends_on 其 service_completed_successfully），
    # 复用与 api 相同的镜像/env；迁移成功退出 0，失败退出非 0（api 不启动）
    run_db_migrations
    ;;
  api)
    require_writable_data_dirs
    run_db_migrations
    exec uvicorn app.main:app --host 0.0.0.0 --port 8100
    ;;
  worker)
    require_writable_data_dirs
    exec celery -A app.celery_app worker --loglevel=info
    ;;
  hub-plugin)
    # anc-hub 插件 sidecar：注册到中台插件面，把 ai-evo 核心能力暴露为 MCP 工具。
    # 配置见 HUB_ADDR / HUB_ADVERTISE_ADDR / HUB_LISTEN_ADDR（hubkit 契约）
    # 不挂 uploads/logs 卷、不落盘，无需可写预检
    exec python -m app.hub_plugin
    ;;
  *)
    echo "Unknown SERVICE_TYPE: ${SERVICE_TYPE}"
    echo "Usage: SERVICE_TYPE=api|worker|hub-plugin|volume-init|migrate"
    exit 1
    ;;
esac
