"""W17 执行分发收敛注册表的路由测试

逐类型验证 _execute_evaluation 将对应 evaluation_type 分发给
注册表适配器（适配器再委托既有专用路径）；契约与迁移前一致：
patch 模块级 _execute_xxx 函数后必须被命中且位置实参不变。
"""
import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from unittest.mock import patch

from app.tasks.evaluation_tasks_async import _execute_evaluation


class MockTask:
    """最小任务桩：仅携带分发所需字段"""

    def __init__(self, evaluation_type, **kwargs):
        self.evaluation_type = evaluation_type
        self.id = kwargs.get("id", "test-task-id")
        self.name = "test-task"
        self.model_config = {}
        self.model_provider = None
        self.judge_count = 4
        self.max_score = 10
        self.evaluation_metrics = []
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestConsistencyDispatch:
    def test_consistency_routes_through_registry(self):
        """consistency 类型命中注册表适配器并委托既有专用路径"""
        task = MockTask("consistency", consistency_config={"repeat_count": 3})
        payloads = [{"idx": 0, "user_input": "q"}]

        with patch(
            "app.tasks.evaluation_tasks_async._execute_consistency_evaluation"
        ) as mock_exec:
            mock_exec.return_value = [{"idx": 0, "success": True}]
            result = _execute_evaluation(task, payloads, None, "task-id")

            assert result == [{"idx": 0, "success": True}]
            mock_exec.assert_called_once()
            call = mock_exec.call_args
            # 位置实参与迁移前分支完全一致: (task, payloads, db, task_id)
            assert call[0][0] is task
            assert call[0][1] == payloads
            assert call[0][2] is None
            assert call[0][3] == "task-id"
            assert call[1]["execution_number"] == 1


class TestSuccessRateDispatch:
    def test_success_rate_routes_through_registry(self):
        """success_rate 类型命中注册表适配器并委托既有专用路径"""
        task = MockTask("success_rate", success_rate_config={"repeat_count": 10})
        payloads = [{"idx": 0, "user_input": "q"}]

        with patch(
            "app.tasks.evaluation_tasks_async._execute_success_rate_evaluation"
        ) as mock_exec:
            mock_exec.return_value = [{"idx": 0, "success": True}]
            result = _execute_evaluation(task, payloads, None, "task-id")

            assert result == [{"idx": 0, "success": True}]
            mock_exec.assert_called_once()
            call = mock_exec.call_args
            # 位置实参与迁移前分支完全一致: (task, payloads, task_id, db)
            assert call[0][0] is task
            assert call[0][1] == payloads
            assert call[0][2] == "task-id"
            assert call[0][3] is None
            assert call[1]["execution_number"] == 1


class TestRagDispatch:
    def test_rag_routes_through_registry(self):
        """rag 类型命中注册表适配器并委托既有专用路径（含 langfuse_host 与偏移实参）"""
        task = MockTask("rag", rag_config={"top_k": 3})
        payloads = [{"idx": 0, "user_input": "q"}]

        with patch(
            "app.tasks.evaluation_tasks_async._execute_rag_evaluation"
        ) as mock_exec:
            mock_exec.return_value = [{"idx": 0, "success": True}]
            result = _execute_evaluation(task, payloads, None, "task-id")

            assert result == [{"idx": 0, "success": True}]
            mock_exec.assert_called_once()
            call = mock_exec.call_args
            # 位置实参与迁移前分支完全一致: (task, payloads, task_id, stop_event, langfuse_host)
            assert call[0][0] is task
            assert call[0][1] == payloads
            assert call[0][2] == "task-id"
            assert call[0][3] is None
            assert call[0][4] == ""
            assert call[1]["execution_number"] == 1
            assert call[1]["progress_offset"] == 0


class TestBinaryClassificationDispatch:
    def test_binary_classification_routes_through_registry(self):
        """binary_classification 类型命中注册表适配器并委托既有专用路径

        与迁移前 tests/unit/test_binary_classification_execution.py 的
        分支命中契约一致：patch 模块级函数后必须被命中且位置实参不变。
        """
        task = MockTask("binary_classification", binary_metrics_config={})
        payloads = [{"idx": 0, "user_input": "q"}]

        with patch(
            "app.tasks.evaluation_tasks_async._execute_binary_classification_evaluation"
        ) as mock_exec:
            mock_exec.return_value = [{"idx": 0, "success": True}]
            result = _execute_evaluation(task, payloads, None, "task-id")

            assert result == [{"idx": 0, "success": True}]
            mock_exec.assert_called_once()
            call = mock_exec.call_args
            # 位置实参与迁移前分支完全一致: (task, payloads, task_id, db, stop_event)
            assert call[0][0] is task
            assert call[0][1] == payloads
            assert call[0][2] == "task-id"
            assert call[0][3] is None
            assert call[1]["execution_number"] == 1


class TestLatencyDispatch:
    def test_latency_routes_through_registry(self):
        """latency 类型命中注册表适配器（专用 EvaluationExecutor 构造与迁移前分支等价）"""
        task = MockTask("latency", latency_config={"threshold": 2.5}, judge_count=4)
        payloads = [{"idx": 0, "user_input": "q"}]

        with patch(
            "app.services.evaluation_executor.EvaluationExecutor"
        ) as mock_executor_cls:
            mock_executor = mock_executor_cls.return_value
            mock_executor.execute_batch_latency.return_value = [
                {"idx": 0, "success": True, "duration": 1.0}
            ]
            result = _execute_evaluation(task, payloads, None, "task-id")

            assert result == [{"idx": 0, "success": True, "duration": 1.0}]
            mock_executor.execute_batch_latency.assert_called_once()
            # 构造参数与迁移前分支逐项一致
            kwargs = mock_executor_cls.call_args[1]
            assert kwargs["max_workers"] == 4
            assert kwargs["task_id"] == "task-id"
            assert kwargs["execution_number"] == 1
            assert kwargs["progress_offset"] == 0  # 耗时评测不做续跑过滤
            assert kwargs["langfuse_host"] == ""
