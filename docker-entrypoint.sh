#!/bin/bash
set -e

case "${SERVICE_TYPE}" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port 8100
    ;;
  worker)
    exec celery -A app.celery_app worker --loglevel=info
    ;;
  hub-plugin)
    # anc-hub 插件 sidecar：注册到中台插件面，把 ai-evo 核心能力暴露为 MCP 工具。
    # 配置见 HUB_ADDR / HUB_ADVERTISE_ADDR / HUB_LISTEN_ADDR（hubkit 契约）
    exec python -m app.hub_plugin
    ;;
  *)
    echo "Unknown SERVICE_TYPE: ${SERVICE_TYPE}"
    echo "Usage: SERVICE_TYPE=api|worker|hub-plugin"
    exit 1
    ;;
esac
