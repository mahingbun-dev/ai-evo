#!/bin/bash
set -e

case "${SERVICE_TYPE}" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port 8100
    ;;
  worker)
    exec celery -A app.celery_app worker --loglevel=info
    ;;
  *)
    echo "Unknown SERVICE_TYPE: ${SERVICE_TYPE}"
    echo "Usage: SERVICE_TYPE=api|worker"
    exit 1
    ;;
esac
