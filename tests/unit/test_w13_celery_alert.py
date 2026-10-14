"""celery on_failure 告警接线单元测试（W13）

任务失败 → send_alert（webhook 或 logs/alerts.log 兜底）被调用且携带
评测任务 ID 与错误信息；告警自身失败不影响 on_failure 的状态回写主流程。
"""
import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.tasks.celery_tasks import EvaluationBaseTask


@pytest.fixture
def task():
    return EvaluationBaseTask()


def test_on_failure_sends_alert(task, monkeypatch):
    recorded = {}

    def fake_send_alert(**kw):
        recorded.update(kw)
        return True

    monkeypatch.setattr("app.services.alert_service.send_alert", fake_send_alert)
    # DB 回写部分在单测里直接断开（get_db_session 抛错会走 on_failure 自己的 except）
    monkeypatch.setattr(
        "app.tasks.evaluation_tasks_async.get_db_session",
        lambda: (_ for _ in ()).throw(RuntimeError("unit-test: db disabled")),
    )

    task.on_failure(RuntimeError("boom: 模型网关超时"), "celery-task-1", (42,), {}, None)

    assert recorded["level"] == "error"
    assert recorded["title"] == "评测任务失败"
    assert recorded["source"] == "celery"
    assert recorded["details"]["celery_task_id"] == "celery-task-1"
    assert recorded["details"]["evaluation_task_id"] == 42
    assert "boom: 模型网关超时" in recorded["details"]["error"]


def test_on_failure_alert_failure_does_not_break_db_callback(task, monkeypatch):
    """告警通道炸掉时，on_failure 仍要继续走状态回写（旁路不拖主流程）"""
    def broken_alert(**kw):
        raise RuntimeError("webhook通道炸了")

    monkeypatch.setattr("app.services.alert_service.send_alert", broken_alert)
    monkeypatch.setattr(
        "app.tasks.evaluation_tasks_async.get_db_session",
        lambda: (_ for _ in ()).throw(RuntimeError("unit-test: db disabled")),
    )

    # 不应抛出异常（DB 回写部分同样被自己的 except 兜住）
    task.on_failure(RuntimeError("boom"), "celery-task-2", (43,), {}, None)
