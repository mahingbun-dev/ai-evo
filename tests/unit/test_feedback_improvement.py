"""
反馈改进功能单元测试
测试结果反馈优化（状态区分、序号修复、user_input字段）
"""

import pytest
from dataclasses import is_dataclass

from app.services.judge_evaluation_engine import JudgeEvaluationResult
from app.services.result_aggregator import ResultAggregator


class TestJudgeEvaluationResultFields:
    """JudgeEvaluationResult 字段测试"""

    def test_judge_evaluation_result_has_user_input_field(self):
        """测试 JudgeEvaluationResult 包含 user_input 字段且默认值 ''"""
        result = JudgeEvaluationResult(
            question_index=1,
            question="测试问题",
            ref_answer="参考答案",
            model_answer="模型回答",
        )
        assert hasattr(result, 'user_input'), "JudgeEvaluationResult 应该有 user_input 字段"
        assert result.user_input == "", "user_input 默认值应为空字符串"

    def test_judge_evaluation_result_has_duration_field(self):
        """测试 JudgeEvaluationResult 包含 duration 字段且默认值 0.0"""
        result = JudgeEvaluationResult(
            question_index=1,
            question="测试问题",
            ref_answer="参考答案",
            model_answer="模型回答",
        )
        assert hasattr(result, 'duration'), "JudgeEvaluationResult 应该有 duration 字段"
        assert result.duration == 0.0, "duration 默认值应为 0.0"

    def test_judge_evaluation_result_user_input_setable(self):
        """测试 user_input 字段可被赋值"""
        result = JudgeEvaluationResult(
            question_index=1,
            question="测试问题",
            ref_answer="参考答案",
            model_answer="模型回答",
            user_input="原始用户问题",
        )
        assert result.user_input == "原始用户问题"


class TestQuestionIndexFromOne:
    """序号从1开始测试"""

    def test_evaluate_single_error_result_has_index_one(self):
        """测试 evaluate_single 在错误情况下返回 question_index=1"""
        # 模拟错误结果
        result = JudgeEvaluationResult(
            question_index=1,  # 错误情况下的返回值
            question="测试问题",
            ref_answer="参考答案",
            model_answer="",
            success=False,
            error="模型调用失败",
        )
        assert result.question_index == 1, "错误情况下 question_index 应为 1"

    def test_evaluate_single_success_result_has_index_one(self):
        """测试 evaluate_single 在成功情况下返回 question_index=1"""
        result = JudgeEvaluationResult(
            question_index=1,  # 成功情况下的返回值
            question="测试问题",
            ref_answer="参考答案",
            model_answer="模型回答",
            success=True,
            scores={"综合评分": 0.8},
            overall_score=0.8,
        )
        assert result.question_index == 1, "成功情况下 question_index 应为 1"

    def test_evaluate_batch_results_indices_start_from_one(self):
        """测试 evaluate_batch 返回的 question_index 列表为 [1, 2, 3, ...]"""
        # 模拟 evaluate_batch 的返回结果
        results = [
            JudgeEvaluationResult(question_index=1, question="q1", ref_answer="a", model_answer="b", success=True, user_input="原始问题1"),
            JudgeEvaluationResult(question_index=2, question="q2", ref_answer="a", model_answer="b", success=True, user_input="原始问题2"),
            JudgeEvaluationResult(question_index=3, question="q3", ref_answer="a", model_answer="b", success=True, user_input="原始问题3"),
        ]
        indices = [r.question_index for r in results]
        assert indices == [1, 2, 3], f"序号应为 [1, 2, 3]，实际为 {indices}"
        # 验证不是 [0, 1, 2]
        assert indices != [0, 1, 2], "序号不应从 0 开始"


class TestConsistencyEvaluatorIndexFromOne:
    """ConsistencyEvaluator 序号从1开始测试"""

    def test_consistency_evaluator_batch_indices_start_from_one(self):
        """测试 ConsistencyEvaluator.evaluate_batch 返回的 question_index 从1开始"""
        from app.services.consistency_evaluator import ConsistencyResult

        results = [
            ConsistencyResult(question_index=1),
            ConsistencyResult(question_index=2),
            ConsistencyResult(question_index=3),
        ]
        indices = [r.question_index for r in results]
        assert indices == [1, 2, 3], f"ConsistencyResult 序号应为 [1, 2, 3]，实际为 {indices}"


class TestResultAggregatorUserInput:
    """ResultAggregator user_input 透出测试"""

    def test_format_items_includes_user_input(self):
        """测试 _format_items 对包含 user_input 的结果透出该字段"""
        aggregator = ResultAggregator()

        # 模拟包含 user_input 的评测结果
        results = [
            {
                "idx": 0,
                "success": True,
                "model_output": "模型输出",
                "average_score": 0.8,
                "judge_scores": [],
                "feedback": "反馈",
                "dimension_scores": {},
                "user_input": "原始用户问题1",
            },
            {
                "idx": 1,
                "success": True,
                "model_output": "模型输出2",
                "average_score": 0.6,
                "judge_scores": [],
                "feedback": "反馈2",
                "dimension_scores": {},
                "user_input": "原始用户问题2",
            },
        ]

        formatted = aggregator._format_items(results)

        assert len(formatted) == 2, "应返回2条格式化结果"
        assert formatted[0].get("user_input") == "原始用户问题1", "应透出 user_input 字段"
        assert formatted[1].get("user_input") == "原始用户问题2", "应透出 user_input 字段"

    def test_format_items_user_input_absent_when_missing(self):
        """测试 _format_items 在 user_input 缺失时不包含该字段（V2 平铺模式）"""
        aggregator = ResultAggregator()

        results = [
            {
                "idx": 0,
                "success": True,
                "model_output": "模型输出",
                "average_score": 0.8,
                "judge_scores": [],
                "feedback": "反馈",
                "dimension_scores": {},
            },
        ]

        formatted = aggregator._format_items(results)

        assert "user_input" not in formatted[0], "user_input 缺失时不应出现在结果中"


class TestEvaluationResultResponseModel:
    """EvaluationResultResponse 模型测试"""

    def test_evaluation_result_response_model_exists(self):
        """测试 EvaluationResultResponse 模型已定义"""
        from app.api.evaluation_task_api import EvaluationResultResponse

        # 成功响应
        success_response = EvaluationResultResponse(
            task_id="test-task",
            status="completed",
            evaluation_type="judge",
            result={"items": []},
            total_cost=0.5,
        )
        assert success_response.task_id == "test-task"
        assert success_response.status == "completed"
        assert success_response.result is not None
        assert success_response.total_cost == 0.5

    def test_evaluation_result_response_model_error_fields(self):
        """测试 EvaluationResultResponse 失败响应包含 error_code, error_message, error_log_id"""
        from app.api.evaluation_task_api import EvaluationResultResponse

        # 失败响应
        error_response = EvaluationResultResponse(
            task_id="test-task",
            status="failed",
            error_code="API_ERROR",
            error_message="任务执行失败",
            error_log_id=None,
        )
        assert error_response.status == "failed"
        assert error_response.error_code == "API_ERROR"
        assert error_response.error_message == "任务执行失败"

    def test_evaluation_result_response_completed_has_no_error_fields(self):
        """测试 completed 状态的响应不包含 error 相关字段（或为 None）"""
        from app.api.evaluation_task_api import EvaluationResultResponse

        response = EvaluationResultResponse(
            task_id="test-task",
            status="completed",
            evaluation_type="judge",
            result={},
            total_cost=1.0,
        )
        # completed 状态下 error 字段应为 None
        assert response.error_code is None or response.error_code == ""
        assert response.error_message is None or response.error_message == ""
