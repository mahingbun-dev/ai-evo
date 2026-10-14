"""W10-1 celery broker visibility_timeout 显式配置单元测试

整改计划 §3 W10 第 1 项：Redis broker 可见性超时必须显式 ≥ task_time_limit
（7200s）。缺省 visibility_timeout 为 1 小时，acks_late 长任务执行超过 1 小时
后消息会被 broker 重新投递，同任务在两个 worker 并发执行。
"""
from app.celery_app import celery_app


import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
class TestBrokerVisibilityTimeout:
    def test_visibility_timeout_configured(self):
        """broker_transport_options 显式携带 visibility_timeout=7800"""
        options = celery_app.conf.broker_transport_options
        assert options.get("visibility_timeout") == 7800, (
            f"broker_transport_options 缺少显式 visibility_timeout=7800，实际: {options}"
        )

    def test_visibility_timeout_ge_task_time_limit(self):
        """visibility_timeout ≥ task_time_limit（7200s），避免长任务执行中被重投"""
        visibility = celery_app.conf.broker_transport_options.get("visibility_timeout")
        time_limit = celery_app.conf.task_time_limit
        assert visibility is not None
        assert visibility >= time_limit, (
            f"visibility_timeout({visibility}) 必须 ≥ task_time_limit({time_limit})"
        )

    def test_visibility_timeout_margin_over_soft_limit(self):
        """对 soft_time_limit（6600s）也留有富余，覆盖 ack 抖动窗口"""
        visibility = celery_app.conf.broker_transport_options.get("visibility_timeout")
        assert visibility > celery_app.conf.task_soft_time_limit
        assert visibility - celery_app.conf.task_time_limit >= 300
