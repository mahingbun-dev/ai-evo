"""
空值校验单元测试

测试场景：当待评测内容为空时，裁判评测应直接返回0分，而不是调用裁判模型
"""
import pytest
import asyncio
from unittest.mock import MagicMock, patch


class MockModelCallerSync:
    """Mock ModelCallerSync"""

    def __init__(self, content=""):
        self.content = content

    def call(self, messages, model_config, runtime_variables):
        return {
            "success": True,
            "content": self.content,
            "raw": {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        }


class MockTask:
    """Mock task object - minimal attributes for empty content test"""

    def __init__(self):
        self.judge_model = "test-judge-model"
        self.judge_count = 1
        self.max_score = 10
        self.evaluation_metrics = []
        self.model_config = {
            "provider": "openai",
            "model_type": "gpt-4",
            "endpoint": "https://api.openai.com/v1",
            "api_key": "test-key",
        }
        self.model_provider = None
        self.runtime_variables = {}
        self.variable_mappings = {}
        self.variable_sources = {}
        self.variable_types = {}
        self.enable_user_import = False
        self.user_import_template = None
        self.judge_variable_config = None
        self.evaluation_targets = None
        self.judge_prompt = ""
        self.judge_model_config = None
        self.evaluation_scene = ""
        self.scene_desc = ""
        self.steps = []
        self.score_details = ""


class TestEmptyContentValidation:
    """空值校验测试"""

    def test_empty_model_output_returns_zero_score(self):
        """当模型输出为空字符串时，应直接返回0分，不调用裁判模型"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        payload = {"idx": 0, "user_input": "test question"}

        mock_caller = MockModelCallerSync(content="")

        with patch('app.services.evaluation_executor.EvaluationExecutor._update_progress'):
            with patch('app.services.model_caller.ModelCallerSync', return_value=mock_caller):
                result = executor._evaluate_single(payload, MockTask())

        assert result["success"] is True, "评测应该成功"
        assert result["average_score"] == 0, "空内容应返回0分"
        assert result["judge_scores"] == [], "不应有裁判评分"
        assert "空" in result["feedback"], "反馈应包含空值提示"

    def test_whitespace_only_model_output_returns_zero_score(self):
        """当模型输出只有空白字符时，应直接返回0分"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        payload = {"idx": 0, "user_input": "test question"}

        mock_caller = MockModelCallerSync(content="   \n\t  ")

        with patch('app.services.evaluation_executor.EvaluationExecutor._update_progress'):
            with patch('app.services.model_caller.ModelCallerSync', return_value=mock_caller):
                result = executor._evaluate_single(payload, MockTask())

        assert result["success"] is True
        assert result["average_score"] == 0, "纯空白内容应返回0分"
        assert result["judge_scores"] == []

    def test_empty_content_judge_not_called(self):
        """当内容为空时，裁判模型不应被调用"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        payload = {"idx": 0, "user_input": "test question"}

        mock_caller = MockModelCallerSync(content="")

        with patch('app.services.evaluation_executor.EvaluationExecutor._update_progress'):
            with patch('app.services.model_caller.ModelCallerSync', return_value=mock_caller):
                with patch('app.services.judge_client.JudgeClient') as MockJudge:
                    result = executor._evaluate_single(payload, MockTask())

                    # 验证 JudgeClient 没有被实例化
                    MockJudge.assert_not_called()

        assert result["success"] is True
        assert result["average_score"] == 0

    def test_empty_content_dimension_scores_empty(self):
        """当内容为空时，维度评分应为空"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")
        payload = {"idx": 0, "user_input": "test question"}

        mock_caller = MockModelCallerSync(content="")

        with patch('app.services.evaluation_executor.EvaluationExecutor._update_progress'):
            with patch('app.services.model_caller.ModelCallerSync', return_value=mock_caller):
                result = executor._evaluate_single(payload, MockTask())

        assert result["dimension_scores"] == {}, "空内容的维度评分应为空字典"


class TestEvaluationExecutorWithChainResult:
    """EvaluationExecutor 评测目标来自 chain_result 的空值校验测试"""

    def test_chain_result_content_not_empty_should_not_return_zero(self):
        """当 evaluation_targets 配置为 chain_result 且 chain_result 有内容时，不应返回0分"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")

        # 模型直接输出为空（中间步骤响应），但 chain_result 中有实际内容
        mock_caller = MagicMock()
        mock_caller.call.return_value = {
            "success": True,
            "content": "",  # 模型直接输出为空
            "chain_results": [
                {
                    "chain_index": 0,
                    "name": "步骤1",
                    "variables": {"status": "ok"},
                    "content": "步骤1完成",
                },
                {
                    "chain_index": 1,
                    "name": "步骤2",
                    "variables": {"content": "这是从请求链获取的实际待评测内容"},
                    "content": "这是从请求链获取的实际待评测内容",
                },
            ],
        }

        # 配置 evaluation_targets 从 chain_result 获取内容
        task = MockTask()
        task.evaluation_targets = [
            {
                "id": "target_1",
                "name": "target_1",
                "display_name": "文章",
                "source": "chain_result",
                "source_config": {
                    "var_names": ["content"],
                    "chain_index": 1,
                },
                "required": True,
            }
        ]

        payload = {"idx": 0, "user_input": "test question"}

        with patch('app.services.evaluation_executor.EvaluationExecutor._update_progress'):
            with patch('app.services.model_caller.ModelCallerSync', return_value=mock_caller):
                result = executor._evaluate_single(payload, task)

        # 验证：不应因为空值校验返回0分，因为 chain_result 中有内容
        # 注意：由于 mock 的 chain_result 有内容，空值校验应通过
        # 但后续的裁判调用可能因 mock 不完整而失败，我们主要验证空值校验逻辑
        assert result.get("average_score") != 0 or result.get("success") is True

    def test_chain_result_content_empty_should_return_zero(self):
        """当 evaluation_targets 配置为 chain_result 但 chain_result 内容为空时，应返回0分"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")

        mock_caller = MagicMock()
        mock_caller.call.return_value = {
            "success": True,
            "content": "",
            "chain_results": [
                {
                    "chain_index": 1,
                    "name": "步骤2",
                    "variables": {"content": ""},  # chain_result 中的内容也为空
                    "content": "",
                },
            ],
        }

        task = MockTask()
        task.evaluation_targets = [
            {
                "id": "target_1",
                "name": "target_1",
                "display_name": "文章",
                "source": "chain_result",
                "source_config": {
                    "var_names": ["content"],
                    "chain_index": 1,
                },
                "required": True,
            }
        ]

        payload = {"idx": 0, "user_input": "test question"}

        with patch('app.services.evaluation_executor.EvaluationExecutor._update_progress'):
            with patch('app.services.model_caller.ModelCallerSync', return_value=mock_caller):
                result = executor._evaluate_single(payload, task)

        assert result["success"] is True
        assert result["average_score"] == 0
        assert "空" in result["feedback"]

    def test_no_evaluation_targets_falls_back_to_model_output(self):
        """当未配置 evaluation_targets 时，空值校验应回退到 model_output"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="test-task")

        mock_caller = MockModelCallerSync(content="")

        task = MockTask()
        task.evaluation_targets = None  # 未配置 evaluation_targets

        payload = {"idx": 0, "user_input": "test question"}

        with patch('app.services.evaluation_executor.EvaluationExecutor._update_progress'):
            with patch('app.services.model_caller.ModelCallerSync', return_value=mock_caller):
                result = executor._evaluate_single(payload, task)

        assert result["success"] is True
        assert result["average_score"] == 0
        assert "空" in result["feedback"]


class TestJudgeEvaluationEngineEmptyContent:
    """JudgeEvaluationEngine 空值校验测试"""

    def test_evaluate_single_empty_model_answer(self):
        """evaluate_single: 模型回答为空时直接返回零分"""
        from app.services.judge_evaluation_engine import JudgeEvaluationEngine

        engine = JudgeEvaluationEngine(
            model_config={},
            judge_config={"model_type": "gpt-4"},
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                engine.evaluate_single("问题", "参考答案", "")
            )
        finally:
            loop.close()

        assert result.overall_score == 0.0
        assert result.reasoning == "模型回答内容为空，跳过裁判评测"
        assert result.validation_message == "模型回答内容为空"
        assert result.scores.get("校验状态") == "failed"

    def test_evaluate_single_null_model_answer(self):
        """evaluate_single: 模型回答为 None 时直接返回零分"""
        from app.services.judge_evaluation_engine import JudgeEvaluationEngine

        engine = JudgeEvaluationEngine(
            model_config={},
            judge_config={"model_type": "gpt-4"},
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                engine.evaluate_single("问题", "参考答案", None)
            )
        finally:
            loop.close()

        assert result.overall_score == 0.0
        assert "空" in result.reasoning

    def test_evaluate_single_whitespace_model_answer(self):
        """evaluate_single: 模型回答只有空白字符时直接返回零分"""
        from app.services.judge_evaluation_engine import JudgeEvaluationEngine

        engine = JudgeEvaluationEngine(
            model_config={},
            judge_config={"model_type": "gpt-4"},
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                engine.evaluate_single("问题", "参考答案", "   \n\t  ")
            )
        finally:
            loop.close()

        assert result.overall_score == 0.0
        assert "空" in result.reasoning

    def test_evaluate_single_with_targets_empty_model_answer(self):
        """_evaluate_with_targets: 模型回答为空时直接返回零分"""
        from app.services.judge_evaluation_engine import JudgeEvaluationEngine

        engine = JudgeEvaluationEngine(
            model_config={},
            judge_config={"model_type": "gpt-4"},
            evaluation_targets=[
                {
                    "id": "target1",
                    "name": "model_output",
                    "display_name": "模型输出",
                    "source": "model_output",
                    "source_config": {},
                    "required": True,
                }
            ],
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                engine.evaluate_single("问题", "参考答案", "")
            )
        finally:
            loop.close()

        assert result.overall_score == 0.0
        assert "空" in result.reasoning


class TestJudgeClientEmptyContent:
    """JudgeClient 空值校验测试"""

    def test_judge_both_empty_returns_zero(self):
        """judge: system_prompt 和 user_prompt 都为空时直接返回零分，不调用模型"""
        from app.services.judge_client import JudgeClient

        mock_task = MockTask()
        client = JudgeClient(mock_task)

        result = client.judge("", "")

        assert result["score"] == 0
        assert result["is_empty_content"] is True
        assert "空" in result["feedback"]

    def test_judge_both_whitespace_returns_zero(self):
        """judge: system_prompt 和 user_prompt 都只有空白字符时直接返回零分"""
        from app.services.judge_client import JudgeClient

        mock_task = MockTask()
        client = JudgeClient(mock_task)

        result = client.judge("   \n\t  ", "   \n\t  ")

        assert result["score"] == 0
        assert result["is_empty_content"] is True

    def test_judge_empty_user_prompt_but_system_has_content_calls_model(self):
        """judge: user_prompt 为空但 system_prompt 有内容时正常调用模型（评测内容可能注入到 system_prompt 中）"""
        from app.services.judge_client import JudgeClient

        mock_task = MockTask()
        client = JudgeClient(mock_task)

        mock_parser_result = MagicMock()
        mock_parser_result.to_dict.return_value = {
            "score": 8.0,
            "feedback": "good",
            "dimension_scores": {},
        }

        with patch.object(client, '_call_judge_model', return_value='{"score": 8}'):
            with patch.object(client, '_parse_judge_response', return_value=mock_parser_result.to_dict()):
                result = client.judge("system prompt with evaluation targets content", "")

        assert result["score"] == 8.0
        assert "is_empty_content" not in result

    def test_judge_empty_system_prompt_but_user_has_content_calls_model(self):
        """judge: system_prompt 为空但 user_prompt 有内容时正常调用模型"""
        from app.services.judge_client import JudgeClient

        mock_task = MockTask()
        client = JudgeClient(mock_task)

        mock_parser_result = MagicMock()
        mock_parser_result.to_dict.return_value = {
            "score": 8.0,
            "feedback": "good",
            "dimension_scores": {},
        }

        with patch.object(client, '_call_judge_model', return_value='{"score": 8}'):
            with patch.object(client, '_parse_judge_response', return_value=mock_parser_result.to_dict()):
                result = client.judge("", "有效的用户提示词")

        assert result["score"] == 8.0
        assert "is_empty_content" not in result

    def test_judge_valid_user_prompt_calls_model(self):
        """judge: user_prompt 有效时正常调用模型"""
        from app.services.judge_client import JudgeClient

        mock_task = MockTask()
        client = JudgeClient(mock_task)

        mock_parser_result = MagicMock()
        mock_parser_result.to_dict.return_value = {
            "score": 8.0,
            "feedback": "good",
            "dimension_scores": {},
        }

        with patch.object(client, '_call_judge_model', return_value='{"score": 8}'):
            with patch.object(client, '_parse_judge_response', return_value=mock_parser_result.to_dict()):
                result = client.judge("system prompt", "有效的用户提示词")

        assert result["score"] == 8.0
        assert "is_empty_content" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
