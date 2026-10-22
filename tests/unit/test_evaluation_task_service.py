"""
EvaluationTaskService 单元测试
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime

import app.services.evaluation_task_service as evaluation_task_service_module
from app.services.evaluation_task_service import EvaluationTaskService
from app.models.task_models import EvaluationTask

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestEvaluationTaskServicePublish:
    """EvaluationTaskService 状态发布测试类"""

    @pytest.fixture(autouse=True)
    def reset_redis_publisher(self):
        """
        每用例前后重置模块级 Redis 发布单例。

        W5 起 _publish_status_update 改用模块级连接（进程内复用连接池，
        不再每次 publish 都新建连接），本类用例通过 patch redis.from_url
        断言发布行为，必须每用例重置单例才能让 patch 生效。
        """
        evaluation_task_service_module._redis_publisher = None
        yield
        evaluation_task_service_module._redis_publisher = None

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


class TestTaskExecutionRecordGetDimensionTrend:
    """get_dimension_trend：跨执行维度趋势查询（sqlite 内存库）

    覆盖四条行为：只返回 completed、execution_number 倒序、limit 生效、
    仅投影标量趋势字段（items 等大字段不透出）+ result 非 dict 兜底。
    """

    @staticmethod
    def _make_db():
        # 函数内延迟导入（仓库先例：test_evaluation_pre_scripts_flow._make_db），
        # 避免收集期把 app.db.session 缓存进 sys.modules
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.db.session import Base
        import app.models  # noqa: F401 - 注册全部模型

        engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine)()

    @staticmethod
    def _rec(n, status, result):
        from app.models.task_models import TaskExecutionRecord

        return TaskExecutionRecord(
            id=f"exec-trend-{n}", task_id="t-trend", execution_number=n,
            status=status, result=result,
        )

    def test_completed_only_desc_and_projection(self):
        """只返回 completed、倒序、每条仅含趋势字段，items 大字段不透出"""
        db = self._make_db()
        db.add_all([
            self._rec(1, "completed", {
                "average_score": 6.5,
                "dimension_scores": {"准确性": {"average": 6.5, "weight": 2}},
            }),
            self._rec(2, "failed", {"error": "boom"}),
            self._rec(3, "completed", {
                "average_score": 8.1,
                "items": [{"idx": 0}],  # 大字段：应被投影剔除
                "dimension_scores": {
                    "准确性": {"average": 8.1, "weight": 2},
                    "完整性": {"average": 8.0, "weight": 1},
                },
            }),
            self._rec(4, "stopped", {
                "average_score": 5.0,
                "dimension_scores": {"准确性": {"average": 5.0, "weight": 2}},
            }),
        ])
        db.commit()

        from app.services.evaluation_task_service import TaskExecutionRecordService

        trend = TaskExecutionRecordService(db).get_dimension_trend("t-trend", limit=50)

        assert len(trend) == 2, "failed/stopped 版本不参与趋势"
        assert [t["execution_number"] for t in trend] == [3, 1], "按 execution_number 倒序"
        assert trend[0]["average_score"] == 8.1
        assert set(trend[0]["dimension_scores"]) == {"准确性", "完整性"}
        assert "items" not in trend[0], "items 大字段不应透出"
        assert all(
            set(t) == {"execution_number", "status", "completed_at", "average_score", "dimension_scores"}
            for t in trend
        )

    def test_limit(self):
        """limit 生效：最多返回 N 条（仍倒序取最近的 N 条）"""
        db = self._make_db()
        db.add_all([self._rec(i, "completed", {"average_score": float(i)}) for i in (1, 2, 3)])
        db.commit()

        from app.services.evaluation_task_service import TaskExecutionRecordService

        trend = TaskExecutionRecordService(db).get_dimension_trend("t-trend", limit=2)
        assert [t["execution_number"] for t in trend] == [3, 2]

    def test_no_records_returns_empty(self):
        """无执行记录返回空列表（端点层据此走任务本体兼容分支）"""
        db = self._make_db()

        from app.services.evaluation_task_service import TaskExecutionRecordService

        assert TaskExecutionRecordService(db).get_dimension_trend("t-none") == []

    def test_non_dict_result_tolerated(self):
        """result 非 dict（NULL/异常数据）兜底：average_score=None、dimension_scores={}"""
        db = self._make_db()
        db.add(self._rec(1, "completed", None))
        db.commit()

        from app.services.evaluation_task_service import TaskExecutionRecordService

        trend = TaskExecutionRecordService(db).get_dimension_trend("t-trend")
        assert trend == [{
            "execution_number": 1,
            "status": "completed",
            "completed_at": None,
            "average_score": None,
            "dimension_scores": {},
        }]
