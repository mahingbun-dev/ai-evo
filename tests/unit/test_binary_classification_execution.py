"""
二分分类评测执行路径单元测试
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.tasks.evaluation_tasks_async import _execute_evaluation, _execute_binary_classification_evaluation

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class MockTask:
    """模拟评测任务对象"""
    def __init__(self, evaluation_type="binary_classification", **kwargs):
        self.evaluation_type = evaluation_type
        self.id = kwargs.get("id", "test-task-id")
        self.name = kwargs.get("name", "test-task")
        self.model_config = kwargs.get("model_config", {})
        self.model_provider = kwargs.get("model_provider", None)
        self.judge_count = kwargs.get("judge_count", 4)
        self.binary_metrics_config = kwargs.get("binary_metrics_config", {})
        self.latency_config = kwargs.get("latency_config", None)
        self.success_rate_config = kwargs.get("success_rate_config", None)
        self.consistency_config = kwargs.get("consistency_config", None)
        self.rag_config = kwargs.get("rag_config", None)


class MockStopEvent:
    """模拟停止事件"""
    def __init__(self):
        self._is_set = False

    def is_set(self):
        return self._is_set

    def set(self):
        self._is_set = True


async def _noop_coro():
    """空协程：验证执行后当前事件循环仍可运行新协程"""
    return True


class TestBinaryClassificationExecution:
    """测试二分分类评测执行路径"""

    def test_binary_classification_branch_hit(self):
        """binary_classification 分支命中"""
        task = MockTask(evaluation_type="binary_classification")
        payloads = [{"question": "test"}]

        with patch("app.tasks.evaluation_tasks_async._execute_binary_classification_evaluation") as mock_exec:
            mock_exec.return_value = [{"idx": 0, "success": True}]
            result = _execute_evaluation(task, payloads, None, "task-id")

            mock_exec.assert_called_once()
            call_args = mock_exec.call_args
            assert call_args[0][0] is task
            assert call_args[0][1] == payloads
            assert call_args[0][2] == "task-id"

    def test_normal_execution_flow(self):
        """正常执行流程"""
        task = MockTask(
            evaluation_type="binary_classification",
            binary_metrics_config={
                "expected_label": {"source": "dataset_field", "source_config": {"field": "is_valid"}},
                "predicted_label": {"source": "model_output", "source_config": {"field": "prediction"}},
                "positive_value": "true",
                "label_type": "boolean",
            },
        )
        payloads = [
            {"idx": 0, "data": {"is_valid": True, "question": "test"}, "prediction": True}
        ]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.question = "test"
        mock_result.expected_label = True
        mock_result.predicted_label = True
        mock_result.is_correct = True
        mock_result.duration = 1.0
        mock_result.error = ""
        mock_result.input_data = None  # 布尔假值：跳过 result_item.update(input_data) 平铺

        # 真实执行链：先待评测模型调用（ModelCallerSync），后 BinaryClassificationEvaluator 比对。
        # 模型调用 mock 为成功（未配置端点会在进入评测器前抛「模型调用失败: 未配置端点」）
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller_class, \
             patch("app.services.binary_classification_evaluator.BinaryClassificationEvaluator") as mock_eval_class:
            mock_caller_class.return_value.call.return_value = {
                "success": True, "content": "true", "raw": {}, "chain_results": [],
            }
            mock_eval = MagicMock()
            mock_eval.evaluate_batch = AsyncMock(return_value=[mock_result])
            mock_eval_class.return_value = mock_eval

            result = _execute_binary_classification_evaluation(task, payloads, "task-id", None)

            assert len(result) == 1
            assert result[0]["success"] is True
            assert "expected_label" in result[0]
            assert "predicted_label" in result[0]
            assert "is_correct" in result[0]

    def test_stop_signal_response(self):
        """停止信号响应"""
        task = MockTask(evaluation_type="binary_classification")
        stop_event = MockStopEvent()
        stop_event.set()

        payloads = [
            {"idx": 0, "user_input": "test1"},
            {"idx": 1, "user_input": "test2"},
        ]

        with patch("app.tasks.evaluation_tasks_async._execute_binary_classification_evaluation") as mock_exec:
            mock_exec.return_value = []

            result = _execute_binary_classification_evaluation(task, payloads, "task-id", None, stop_event)
            assert isinstance(result, list)

    def test_exception_handling(self):
        """异常处理"""
        task = MockTask(evaluation_type="binary_classification")
        payloads = [{"idx": 0, "user_input": "test question"}]

        # 模型调用 mock 为成功，使流程到达评测器；评测器注入异常验证兜底分支
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller_class, \
             patch("app.services.binary_classification_evaluator.BinaryClassificationEvaluator") as mock_evaluator_class:
            mock_caller_class.return_value.call.return_value = {
                "success": True, "content": "pred", "raw": {}, "chain_results": [],
            }
            mock_evaluator = MagicMock()
            mock_evaluator.evaluate_batch = AsyncMock()
            mock_evaluator.evaluate_batch.side_effect = Exception("Test exception")
            mock_evaluator_class.return_value = mock_evaluator

            result = _execute_binary_classification_evaluation(task, payloads, "task-id", None)

            assert len(result) == 1
            assert result[0]["success"] is False
            assert "error" in result[0]
            assert "Test exception" in result[0]["error"]


class TestExecuteBinaryClassificationEvaluation:
    """测试 _execute_binary_classification_evaluation 函数"""

    def test_function_exists(self):
        """验证函数存在"""
        from app.tasks.evaluation_tasks_async import _execute_binary_classification_evaluation
        assert callable(_execute_binary_classification_evaluation)

    def test_config_parsing(self):
        """测试配置解析 - 从 binary_metrics_config 解析新格式配置"""
        task = MockTask(
            evaluation_type="binary_classification",
            binary_metrics_config={
                "expected_label": {"source": "dataset_field", "source_config": {"field": "label"}},
                "predicted_label": {"source": "model_output", "source_config": {"field": "pred"}},
                "positive_value": "yes",
                "label_type": "string",
            },
        )
        payloads = [
            {"idx": 0, "data": {"label": "yes", "question": "test"}, "pred": "yes"}
        ]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.question = "test"
        mock_result.expected_label = True
        mock_result.predicted_label = True
        mock_result.is_correct = True
        mock_result.duration = 1.0
        mock_result.error = ""
        mock_result.input_data = None  # 布尔假值：跳过 result_item.update(input_data) 平铺

        # 模型调用 mock 为成功，使流程到达评测器构造，方可断言构造参数
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller_class, \
             patch("app.services.binary_classification_evaluator.BinaryClassificationEvaluator") as mock_eval_class:
            mock_caller_class.return_value.call.return_value = {
                "success": True, "content": "yes", "raw": {}, "chain_results": [],
            }
            mock_eval = MagicMock()
            mock_eval.evaluate_batch = AsyncMock(return_value=[mock_result])
            mock_eval_class.return_value = mock_eval

            result = _execute_binary_classification_evaluation(task, payloads, "task-id", None)

            call_kwargs = mock_eval_class.call_args[1]
            assert call_kwargs["expected_label_config"] == {"source": "dataset_field", "source_config": {"field": "label"}}
            assert call_kwargs["predicted_label_config"] == {"source": "model_output", "source_config": {"field": "pred"}}
            assert call_kwargs["positive_value"] == "yes"
            assert call_kwargs["label_type"] == "string"

            assert len(result) == 1
            assert result[0]["success"] is True

    def test_event_loop_not_left_closed(self):
        """回归（全量回归验收缺陷）：执行完成后不得把已关闭的临时 loop 残留为 current

        产品 _execute_binary_classification_evaluation 内部 new_event_loop +
        set_event_loop + run + close；若 finally 不恢复先前的 current loop，
        同线程后续 asyncio 调用（含 nest_asyncio.run）全部报「Event loop is closed」
        （该缺陷曾令全量套件 47 个用例连锁失败）。
        """
        import asyncio

        task = MockTask(evaluation_type="binary_classification")
        payloads = [{"idx": 0, "user_input": "test question"}]

        with patch("app.services.model_caller.ModelCallerSync") as mock_caller_class, \
             patch("app.services.binary_classification_evaluator.BinaryClassificationEvaluator") as mock_eval_class:
            mock_caller_class.return_value.call.return_value = {
                "success": True, "content": "pred", "raw": {}, "chain_results": [],
            }
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.expected_label = True
            mock_result.predicted_label = True
            mock_result.is_correct = True
            mock_result.duration = 1.0
            mock_result.error = ""
            mock_result.input_data = None
            mock_eval = MagicMock()
            mock_eval.evaluate_batch = AsyncMock(return_value=[mock_result])
            mock_eval_class.return_value = mock_eval

            result = _execute_binary_classification_evaluation(task, payloads, "task-id", None)
            assert result[0]["success"] is True

            # 执行后不允许残留「已关闭」的 current 事件循环：current 要么不存在
            # （干净状态，asyncio.run 会自建新 loop），要么存在且未关闭
            def _assert_no_closed_loop():
                try:
                    current = asyncio.get_event_loop_policy().get_event_loop()
                except RuntimeError:
                    current = None  # 无 current loop = 无残留，清洁状态
                assert current is None or not current.is_closed(), \
                    "current 事件循环被残留为已关闭状态"

            # 且后续 asyncio.run 可正常执行（nest_asyncio 环境同路径）
            asyncio.run(_noop_coro())
            _assert_no_closed_loop()

            # 连续执行第二次（同线程复现生产连跑两个任务的场景）依旧正常
            result2 = _execute_binary_classification_evaluation(task, payloads, "task-id", None)
            assert result2[0]["success"] is True
            asyncio.run(_noop_coro())
            _assert_no_closed_loop()
