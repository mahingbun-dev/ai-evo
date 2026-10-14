"""
run_task / batch_run_tasks 并发去重测试

验证 start_task 原子化后，并发提交同一任务时第二个请求被拒绝，
submit_task 不会被重复调用。
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app.api.evaluation_task_api import run_task
from app.api.batch_operations_api import batch_run_tasks, BatchRunRequest
from tests.unit.w9_direct_call import fake_request  # W9: 端点直调适配

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _mock_task(status="pending"):
    task = MagicMock()
    task.id = "task_xxx"
    task.status = status
    return task


class TestRunTaskConcurrency:
    def test_run_task_returns_400_when_already_running(self):
        """任务已在 running 时返回 400，不提交"""
        mock_db = MagicMock()
        with patch('app.api.evaluation_task_api.EvaluationTaskService') as mock_cls, \
                patch('app.services.task_scheduler.get_task_scheduler') as mock_sched:
            mock_service = MagicMock()
            mock_cls.return_value = mock_service
            mock_service.get_task.return_value = _mock_task(status="running")
            # W9：存在性/归属由 ensure_task_access 承担，查询链返回预置任务
            mock_db.query.return_value.filter.return_value.first.return_value = _mock_task(status="running")

            with pytest.raises(HTTPException) as excinfo:
                run_task("task_xxx", fake_request(), db=mock_db)

            assert excinfo.value.status_code == 400
            mock_service.start_task.assert_not_called()
            mock_sched.return_value.submit_task.assert_not_called()

    def test_run_task_rejects_concurrent_start(self):
        """start_task 返回 None（并发已启动）时返回 400，且不调用 submit_task"""
        mock_db = MagicMock()
        with patch('app.api.evaluation_task_api.EvaluationTaskService') as mock_cls, \
                patch('app.services.task_scheduler.get_task_scheduler') as mock_sched:
            mock_service = MagicMock()
            mock_cls.return_value = mock_service
            mock_service.get_task.return_value = _mock_task(status="pending")
            mock_db.query.return_value.filter.return_value.first.return_value = _mock_task(status="pending")
            # 并发场景：另一个请求已把任务置为 running
            mock_service.start_task.return_value = None

            with pytest.raises(HTTPException) as excinfo:
                run_task("task_xxx", fake_request(), db=mock_db)

            assert excinfo.value.status_code == 400
            mock_service.start_task.assert_called_once_with("task_xxx")
            mock_sched.return_value.submit_task.assert_not_called()

    def test_run_task_submits_when_start_ok(self):
        """start_task 成功时正常提交"""
        mock_db = MagicMock()
        with patch('app.api.evaluation_task_api.EvaluationTaskService') as mock_cls, \
                patch('app.services.task_scheduler.get_task_scheduler') as mock_sched:
            mock_service = MagicMock()
            mock_cls.return_value = mock_service
            pending = _mock_task(status="pending")
            running = _mock_task(status="running")
            mock_service.get_task.return_value = pending
            mock_db.query.return_value.filter.return_value.first.return_value = pending
            mock_service.start_task.return_value = running
            mock_sched.return_value.submit_task.return_value = True

            result = run_task("task_xxx", fake_request(), db=mock_db)

            assert result is running
            mock_sched.return_value.submit_task.assert_called_once_with("task_xxx")


class TestBatchRunConcurrency:
    def test_batch_run_skips_when_start_concurrent(self):
        """批量运行中 start_task 返回 None 时标记 skipped，不调用 submit_task"""
        mock_db = MagicMock()
        with patch('app.api.batch_operations_api.EvaluationTaskService') as mock_cls, \
                patch('app.services.task_scheduler.get_task_scheduler') as mock_sched:
            mock_service = MagicMock()
            mock_cls.return_value = mock_service
            mock_service.get_task.return_value = _mock_task(status="pending")
            # 并发场景：另一个请求已把任务置为 running
            mock_service.start_task.return_value = None

            body = BatchRunRequest(task_ids=["task_xxx"])
            result = batch_run_tasks(body, fake_request(), db=mock_db)

            assert result["total"] == 1
            assert result["success"] == 0
            assert result["details"][0]["status"] == "skipped"
            mock_sched.return_value.submit_task.assert_not_called()

    def test_batch_run_submits_when_start_ok(self):
        """批量运行 start_task 成功时正常提交"""
        mock_db = MagicMock()
        with patch('app.api.batch_operations_api.EvaluationTaskService') as mock_cls, \
                patch('app.services.task_scheduler.get_task_scheduler') as mock_sched:
            mock_service = MagicMock()
            mock_cls.return_value = mock_service
            pending = _mock_task(status="pending")
            running = _mock_task(status="running")
            mock_service.get_task.return_value = pending
            mock_db.query.return_value.filter.return_value.first.return_value = pending
            mock_service.start_task.return_value = running
            mock_sched.return_value.submit_task.return_value = True

            body = BatchRunRequest(task_ids=["task_xxx"])
            result = batch_run_tasks(body, fake_request(), db=mock_db)

            assert result["total"] == 1
            assert result["success"] == 1
            assert result["details"][0]["status"] == "success"
            mock_sched.return_value.submit_task.assert_called_once_with("task_xxx")
