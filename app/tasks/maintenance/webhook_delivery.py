"""webhook 投递任务（M1 QE1-3）。

自然落 maint 队列：任务名在 ``app.tasks.maintenance.*`` 命名空间，
TASK_ROUTES 的 glob 路由自动派发（ARCH-2-1，无需显式登记）。

重试语义（celery 原生 retry 承载）：任务体委托
``webhook_service.execute_delivery``（留痕状态机 + SSRF 重验 + 签名发送），
返回 retry 时按指数退避（30s/60s/120s）self.retry，重试 3 次后
deliveries 行落 failed 终态。worker 崩溃场景由 celery 重投兜底
（acks_late 全局配置），行内 attempts 留痕不因重投跳变。
"""
import logging

from app.celery_app import celery_app
from app.services import webhook_service

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.tasks.maintenance.webhook_delivery.deliver_webhook",
)
def deliver_webhook(self, delivery_id: int):
    """投递一条 webhook 留痕行（幂等：按行状态机推进，重投安全）"""
    outcome, countdown = webhook_service.execute_delivery(delivery_id)
    if outcome == webhook_service.OUTCOME_RETRY:
        raise self.retry(countdown=countdown, max_retries=webhook_service.MAX_RETRIES)
    return outcome
