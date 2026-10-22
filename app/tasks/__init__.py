# Celery Tasks
from app.tasks.celery_tasks import run_evaluation_task
# M1 QE1-3：webhook 投递任务（app.tasks.maintenance.* 命名空间 → maint 队列，
# glob 路由见 app/celery_app.py TASK_ROUTES）。此处 import 保证 worker 侧
# 任务注册（app.tasks 包被 autodiscover 后本 import 连带注册 maint 任务）
from app.tasks.maintenance.webhook_delivery import deliver_webhook  # noqa: F401
