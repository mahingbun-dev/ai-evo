"""
EvaluationTaskService 单元测试
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime

from app.services.evaluation_task_service import EvaluationTaskService
from app.models.task_models import EvaluationTask


class TestEvaluationTaskServicePublish:
    """EvaluationTaskService 状态发布测试类"""

    @pytest.fixture
    def mock_db(self):
        """创建模拟数据库会话"""
        db = MagicMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        """创建服务实例"""
        return EvaluationTaskService(mock_db)

    @pytest.fixture
    def mock_task(self):
        """创建模拟任务"""
        task = EvaluationTask(
            id="test_task_1",
            name="Test Task",
            evaluation_type="judge",
            status="pending",
            progress=0,
            completed_questions=0,
            total_questions=10
        )
        return task

    @patch('redis.from_url')
    def test_publish_on_start(self, mock_redis_from_url, service, mock_db, mock_task):
        """测试 start_task 后验证 _publish_status_update 被调用"""
        mock_redis = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        # 模拟原子条件更新成功（rowcount=1）
        mock_db.query.return_value.filter.return_value.update.return_value = 1

        # start_task 原子更新后会重新读取任务，这里返回已启动（running）的任务
        running_task = EvaluationTask(
            id=mock_task.id,
            name=mock_task.name,
            evaluation_type="judge",
            status="running",
            progress=0,
            completed_questions=0,
            total_questions=10,
            execution_count=1,
        )
        service.get_task = MagicMock(return_value=running_task)
        service.add_log = MagicMock()

        # Mock TaskExecutionRecordService
        with patch('app.services.evaluation_task_service.TaskExecutionRecordService'):
            result = service.start_task("test_task_1")

        # 验证 redis.from_url 被调用
        assert mock_redis_from_url.called
        assert mock_redis.publish.called
        assert result.status == "running"

    def test_start_task_concurrent_reject(self, service, mock_db, mock_task):
        """测试并发启动同一任务时被拒绝（原子更新 rowcount=0）"""
        # 模拟原子条件更新失败：任务已被其他并发请求置为 running
        mock_db.query.return_value.filter.return_value.update.return_value = 0
        service.get_task = MagicMock(return_value=mock_task)
        service.add_log = MagicMock()

        with patch('app.services.evaluation_task_service.TaskExecutionRecordService'):
            result = service.start_task("test_task_1")

        assert result is None
        mock_db.rollback.assert_called()

    @patch('redis.from_url')
    def test_publish_on_stop(self, mock_redis_from_url, service, mock_db, mock_task):
        """测试 stop_task 后验证 _publish_status_update 被调用"""
        mock_redis = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        mock_task.status = "running"
        service.get_task = MagicMock(return_value=mock_task)
        service.add_log = MagicMock()

        with patch('app.services.evaluation_task_service.TaskExecutionRecordService'):
            result = service.stop_task("test_task_1")

        assert mock_redis.publish.called
        assert mock_task.status == "stopped"

    @patch('redis.from_url')
    def test_publish_on_complete(self, mock_redis_from_url, service, mock_db, mock_task):
        """测试 complete_task 后验证 _publish_status_update 被调用"""
        mock_redis = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        mock_task.status = "running"
        mock_task.total_questions = 10
        service.get_task = MagicMock(return_value=mock_task)
        service.add_log = MagicMock()

        with patch('app.services.evaluation_task_service.TaskExecutionRecordService'):
            result = service.complete_task("test_task_1", {"result": "success"})

        assert mock_redis.publish.called
        assert mock_task.status == "completed"

    @patch('redis.from_url')
    def test_publish_on_fail(self, mock_redis_from_url, service, mock_db, mock_task):
        """测试 fail_task 后验证 _publish_status_update 被调用"""
        mock_redis = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        mock_task.status = "running"
        service.get_task = MagicMock(return_value=mock_task)
        service.add_log = MagicMock()

        with patch('app.services.evaluation_task_service.TaskExecutionRecordService'):
            result = service.fail_task("test_task_1", "Test error")

        assert mock_redis.publish.called
        assert mock_task.status == "failed"

    @patch('redis.from_url')
    def test_publish_on_progress(self, mock_redis_from_url, service, mock_db, mock_task):
        """测试 update_progress 后验证 _publish_status_update 被调用"""
        mock_redis = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        mock_task.status = "running"
        service.get_task = MagicMock(return_value=mock_task)

        result = service.update_progress("test_task_1", 5, 10)

        assert mock_redis.publish.called
        assert mock_task.completed_questions == 5
        assert mock_task.total_questions == 10
        assert mock_task.progress == 50

    @patch('redis.from_url')
    def test_publish_exception_handling(self, mock_redis_from_url, service, mock_db, mock_task):
        """测试 Redis 发布异常时不应抛出，确保任务主流程不受影响"""
        mock_redis = MagicMock()
        mock_redis.publish = MagicMock(side_effect=Exception("Redis error"))
        mock_redis_from_url.return_value = mock_redis

        # 模拟原子条件更新成功（rowcount=1）
        mock_db.query.return_value.filter.return_value.update.return_value = 1
        # start_task 原子更新后会重新读取任务，这里返回已启动（running）的任务
        running_task = EvaluationTask(
            id=mock_task.id,
            name=mock_task.name,
            evaluation_type="judge",
            status="running",
            progress=0,
            completed_questions=0,
            total_questions=10,
            execution_count=1,
        )
        service.get_task = MagicMock(return_value=running_task)
        service.add_log = MagicMock()

        # 不应该抛出异常
        with patch('app.services.evaluation_task_service.TaskExecutionRecordService'):
            result = service.start_task("test_task_1")

        assert result.status == "running"

    def test_publish_status_update_method_exists(self, service):
        """测试 _publish_status_update 方法存在"""
        assert hasattr(service, '_publish_status_update')
        assert callable(service._publish_status_update)
