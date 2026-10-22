"""维护任务包（ARCH-2-1：app.tasks.maintenance.* 命名空间自动路由 maint 队列）。

M1 QE1-3 首个落地任务：webhook 投递（webhook_delivery.deliver_webhook）。
消费者为 compose 的 worker-maint（CELERY_WORKER_QUEUES=maint）。
"""
