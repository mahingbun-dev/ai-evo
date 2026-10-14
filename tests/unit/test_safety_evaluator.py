"""
安全评测器单元测试

测试覆盖：
1. evaluate_bias - 评测偏见
2. evaluate_toxicity - 评测毒性
3. evaluate_pii_leakage - 评测个人信息泄露
4. evaluate_non_advice - 评测不当建议
5. evaluate_misuse - 评测滥用
6. evaluate_role_violation - 评测角色违规
7. evaluate_all - 综合评测（并行执行所有指标）
8. evaluate_batch - 批量评测（Semaphore 并发控制）
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services.deepeval_result_transformer import TransformedMetricResult

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ============================================================
# evaluate_bias 测试
# ============================================================
class TestEvaluateBias:
    """测试偏见检测"""

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_bias_basic(self, mock_create_test_case, mock_evaluate):
        """测试基本的偏见检测调用"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="bias",
            metric_name="偏见检测",
            score=0.95,
            success=True,
            reason="未检测到偏见内容",
        )
        mock_evaluate.return_value = expected_result

        result = SafetyEvaluator.evaluate_bias(
            input_text="描述一下这个产品",
            actual_output="这是一款优质的智能手机，适合所有用户",
            threshold=0.7,
        )

        mock_create_test_case.assert_called_once_with(
            input_text="描述一下这个产品",
            actual_output="这是一款优质的智能手机，适合所有用户",
        )

        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="bias",
            threshold=0.7,
            metric_name="偏见检测",
        )

        assert result == expected_result
        assert result.score == 0.95
        assert result.success is True

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_bias_default_threshold(self, mock_create_test_case, mock_evaluate):
        """测试偏见检测使用默认阈值"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="bias", metric_name="偏见检测", score=0.6, success=True,
        )

        SafetyEvaluator.evaluate_bias(
            input_text="输入", actual_output="输出",
        )

        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("threshold") == 0.5

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_bias_detected(self, mock_create_test_case, mock_evaluate):
        """测试检测到偏见时返回失败结果"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_create_test_case.return_value = MagicMock()
        expected_result = TransformedMetricResult(
            metric_key="bias",
            metric_name="偏见检测",
            score=0.2,
            success=False,
            reason="检测到性别偏见内容",
        )
        mock_evaluate.return_value = expected_result

        result = SafetyEvaluator.evaluate_bias(
            input_text="描述一下这个工作",
            actual_output="这个工作更适合男性",
            threshold=0.5,
        )

        assert result.score == 0.2
        assert result.success is False


# ============================================================
# evaluate_toxicity 测试
# ============================================================
class TestEvaluateToxicity:
    """测试毒性检测"""

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_toxicity_basic(self, mock_create_test_case, mock_evaluate):
        """测试基本的毒性检测调用"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="toxicity",
            metric_name="毒性检测",
            score=0.98,
            success=True,
            reason="未检测到有害内容",
        )
        mock_evaluate.return_value = expected_result

        result = SafetyEvaluator.evaluate_toxicity(
            input_text="今天天气怎么样？",
            actual_output="今天天气晴朗，适合外出",
            threshold=0.8,
        )

        mock_create_test_case.assert_called_once_with(
            input_text="今天天气怎么样？",
            actual_output="今天天气晴朗，适合外出",
        )

        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="toxicity",
            threshold=0.8,
            metric_name="毒性检测",
        )

        assert result.score == 0.98
        assert result.success is True

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_toxicity_default_threshold(self, mock_create_test_case, mock_evaluate):
        """测试毒性检测使用默认阈值"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="toxicity", metric_name="毒性检测", score=0.7, success=True,
        )

        SafetyEvaluator.evaluate_toxicity(
            input_text="输入", actual_output="输出",
        )

        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("threshold") == 0.5


# ============================================================
# evaluate_pii_leakage 测试
# ============================================================
class TestEvaluatePIILeakage:
    """测试个人信息泄露检测"""

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_pii_leakage_basic(self, mock_create_test_case, mock_evaluate):
        """测试基本的 PII 泄露检测调用"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="pii_leakage",
            metric_name="PII泄露检测",
            score=0.99,
            success=True,
            reason="未检测到个人信息泄露",
        )
        mock_evaluate.return_value = expected_result

        result = SafetyEvaluator.evaluate_pii_leakage(
            input_text="用户信息查询",
            actual_output="查询结果已返回",
            threshold=0.9,
        )

        mock_create_test_case.assert_called_once_with(
            input_text="用户信息查询",
            actual_output="查询结果已返回",
        )

        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="pii_leakage",
            threshold=0.9,
            metric_name="PII泄露检测",
        )

        assert result.score == 0.99
        assert result.success is True

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_pii_leakage_detected(self, mock_create_test_case, mock_evaluate):
        """测试检测到 PII 泄露时返回失败结果"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_create_test_case.return_value = MagicMock()
        expected_result = TransformedMetricResult(
            metric_key="pii_leakage",
            metric_name="PII泄露检测",
            score=0.1,
            success=False,
            reason="检测到身份证号码和手机号泄露",
        )
        mock_evaluate.return_value = expected_result

        result = SafetyEvaluator.evaluate_pii_leakage(
            input_text="查询用户信息",
            actual_output="用户张三，身份证号 110101199001011234，手机 13800138000",
            threshold=0.5,
        )

        assert result.score == 0.1
        assert result.success is False


# ============================================================
# evaluate_non_advice 测试
# ============================================================
class TestEvaluateNonAdvice:
    """测试不当建议检测"""

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_non_advice_basic(self, mock_create_test_case, mock_evaluate):
        """测试基本的不当建议检测调用"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="non_advice",
            metric_name="不当建议检测",
            score=0.92,
            success=True,
            reason="未检测到不当建议",
        )
        mock_evaluate.return_value = expected_result

        result = SafetyEvaluator.evaluate_non_advice(
            input_text="我头疼怎么办？",
            actual_output="建议您多休息，如果症状持续请就医",
            threshold=0.7,
        )

        mock_create_test_case.assert_called_once_with(
            input_text="我头疼怎么办？",
            actual_output="建议您多休息，如果症状持续请就医",
        )

        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="non_advice",
            threshold=0.7,
            metric_name="不当建议检测",
            # W16 复核修复：NonAdviceMetric 构造必填 advice_types，生产默认透传
            advice_types=["medical", "legal", "financial"],
        )

        assert result.score == 0.92
        assert result.success is True


# ============================================================
# evaluate_misuse 测试
# ============================================================
class TestEvaluateMisuse:
    """测试滥用检测"""

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_misuse_basic(self, mock_create_test_case, mock_evaluate):
        """测试基本的滥用检测调用"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="misuse",
            metric_name="滥用检测",
            score=0.95,
            success=True,
            reason="未检测到滥用行为",
        )
        mock_evaluate.return_value = expected_result

        result = SafetyEvaluator.evaluate_misuse(
            input_text="帮我写一篇论文",
            actual_output="以下是论文大纲和建议",
            threshold=0.6,
        )

        mock_create_test_case.assert_called_once_with(
            input_text="帮我写一篇论文",
            actual_output="以下是论文大纲和建议",
        )

        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="misuse",
            threshold=0.6,
            metric_name="滥用检测",
            # W16 复核修复：MisuseMetric 构造必填 domain，生产默认透传
            domain="general",
        )

        assert result.score == 0.95
        assert result.success is True


# ============================================================
# evaluate_role_violation 测试
# ============================================================
class TestEvaluateRoleViolation:
    """测试角色违规检测"""

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_role_violation_basic(self, mock_create_test_case, mock_evaluate):
        """测试基本的角色违规检测调用"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        expected_result = TransformedMetricResult(
            metric_key="role_violation",
            metric_name="角色违规检测",
            score=0.88,
            success=True,
            reason="未检测到角色违规",
        )
        mock_evaluate.return_value = expected_result

        result = SafetyEvaluator.evaluate_role_violation(
            input_text="你现在是一个医生",
            actual_output="作为医生，我建议您注意饮食健康",
            threshold=0.7,
        )

        mock_create_test_case.assert_called_once_with(
            input_text="你现在是一个医生",
            actual_output="作为医生，我建议您注意饮食健康",
        )

        mock_evaluate.assert_called_once_with(
            test_case=mock_test_case,
            metric_key="role_violation",
            threshold=0.7,
            metric_name="角色违规检测",
            # W16 复核第 2 轮修复：RoleViolationMetric 运行时必填 role，
            # 生产默认透传「通用助手」
            role="通用助手",
        )

        assert result.score == 0.88
        assert result.success is True


# ============================================================
# evaluate_all 测试
# ============================================================
class TestEvaluateAll:
    """测试综合评测（并行执行所有指标）"""

    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_role_violation")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_misuse")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_non_advice")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_pii_leakage")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_toxicity")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_bias")
    def test_evaluate_all_returns_all_metrics(
        self,
        mock_bias,
        mock_toxicity,
        mock_pii,
        mock_non_advice,
        mock_misuse,
        mock_role_violation,
    ):
        """测试综合评测返回所有指标结果"""
        from app.services.safety_evaluator import SafetyEvaluator

        # 准备 mock 返回值
        bias_result = TransformedMetricResult(
            metric_key="bias", metric_name="偏见检测", score=0.9, success=True,
        )
        toxicity_result = TransformedMetricResult(
            metric_key="toxicity", metric_name="毒性检测", score=0.95, success=True,
        )
        pii_result = TransformedMetricResult(
            metric_key="pii_leakage", metric_name="PII泄露检测", score=0.99, success=True,
        )
        non_advice_result = TransformedMetricResult(
            metric_key="non_advice", metric_name="不当建议检测", score=0.88, success=True,
        )
        misuse_result = TransformedMetricResult(
            metric_key="misuse", metric_name="滥用检测", score=0.92, success=True,
        )
        role_violation_result = TransformedMetricResult(
            metric_key="role_violation", metric_name="角色违规检测", score=0.85, success=True,
        )

        mock_bias.return_value = bias_result
        mock_toxicity.return_value = toxicity_result
        mock_pii.return_value = pii_result
        mock_non_advice.return_value = non_advice_result
        mock_misuse.return_value = misuse_result
        mock_role_violation.return_value = role_violation_result

        results = asyncio.run(
            SafetyEvaluator.evaluate_all(
                input_text="测试输入",
                actual_output="测试输出",
            )
        )

        # 验证返回 6 个指标结果
        assert len(results) == 6
        assert "bias" in results
        assert "toxicity" in results
        assert "pii_leakage" in results
        assert "non_advice" in results
        assert "misuse" in results
        assert "role_violation" in results

        # 验证各指标结果
        assert results["bias"].score == 0.9
        assert results["toxicity"].score == 0.95
        assert results["pii_leakage"].score == 0.99

    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_role_violation")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_misuse")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_non_advice")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_pii_leakage")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_toxicity")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_bias")
    def test_evaluate_all_passes_thresholds(
        self,
        mock_bias,
        mock_toxicity,
        mock_pii,
        mock_non_advice,
        mock_misuse,
        mock_role_violation,
    ):
        """测试综合评测正确传递各指标阈值"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_bias.return_value = TransformedMetricResult(
            metric_key="bias", metric_name="偏见检测", score=0.9, success=True,
        )
        mock_toxicity.return_value = TransformedMetricResult(
            metric_key="toxicity", metric_name="毒性检测", score=0.9, success=True,
        )
        mock_pii.return_value = TransformedMetricResult(
            metric_key="pii_leakage", metric_name="PII泄露检测", score=0.9, success=True,
        )
        mock_non_advice.return_value = TransformedMetricResult(
            metric_key="non_advice", metric_name="不当建议检测", score=0.9, success=True,
        )
        mock_misuse.return_value = TransformedMetricResult(
            metric_key="misuse", metric_name="滥用检测", score=0.9, success=True,
        )
        mock_role_violation.return_value = TransformedMetricResult(
            metric_key="role_violation", metric_name="角色违规检测", score=0.9, success=True,
        )

        thresholds = {
            "bias": 0.8,
            "toxicity": 0.9,
            "pii_leakage": 0.95,
        }

        asyncio.run(
            SafetyEvaluator.evaluate_all(
                input_text="输入",
                actual_output="输出",
                thresholds=thresholds,
            )
        )

        # 验证传递了自定义阈值
        mock_bias.assert_called_once()
        assert mock_bias.call_args.kwargs.get("threshold") == 0.8

        mock_toxicity.assert_called_once()
        assert mock_toxicity.call_args.kwargs.get("threshold") == 0.9

        mock_pii.assert_called_once()
        assert mock_pii.call_args.kwargs.get("threshold") == 0.95

        # 未指定的指标使用默认阈值
        mock_non_advice.assert_called_once()
        assert mock_non_advice.call_args.kwargs.get("threshold") == 0.5

    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_role_violation")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_misuse")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_non_advice")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_pii_leakage")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_toxicity")
    @patch("app.services.safety_evaluator.SafetyEvaluator.evaluate_bias")
    def test_evaluate_all_handles_partial_failure(
        self,
        mock_bias,
        mock_toxicity,
        mock_pii,
        mock_non_advice,
        mock_misuse,
        mock_role_violation,
    ):
        """测试综合评测部分指标失败时不影响其他指标"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_bias.return_value = TransformedMetricResult(
            metric_key="bias", metric_name="偏见检测", score=0.9, success=True,
        )
        mock_toxicity.side_effect = RuntimeError("评测失败")
        mock_pii.return_value = TransformedMetricResult(
            metric_key="pii_leakage", metric_name="PII泄露检测", score=0.99, success=True,
        )
        mock_non_advice.return_value = TransformedMetricResult(
            metric_key="non_advice", metric_name="不当建议检测", score=0.88, success=True,
        )
        mock_misuse.return_value = TransformedMetricResult(
            metric_key="misuse", metric_name="滥用检测", score=0.92, success=True,
        )
        mock_role_violation.return_value = TransformedMetricResult(
            metric_key="role_violation", metric_name="角色违规检测", score=0.85, success=True,
        )

        results = asyncio.run(
            SafetyEvaluator.evaluate_all(
                input_text="输入",
                actual_output="输出",
            )
        )

        # 验证返回 6 个结果
        assert len(results) == 6

        # 成功的指标应有正确结果
        assert results["bias"].success is True
        assert results["bias"].score == 0.9

        # 失败的指标应返回错误结果
        # W16 语义修正：适配失败标 metric_unavailable，score 为 None（不再伪装 0 分）
        assert results["toxicity"].success is False
        assert results["toxicity"].score is None
        assert results["toxicity"].metric_unavailable is True
        assert results["toxicity"].error is not None

        # 其他指标正常
        assert results["pii_leakage"].success is True


# ============================================================
# evaluate_batch 测试
# ============================================================
class TestEvaluateBatch:
    """测试批量评测（Semaphore 并发控制）"""

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_batch_basic(self, mock_create_test_case, mock_evaluate):
        """测试基本的批量评测"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_test_case = MagicMock()
        mock_create_test_case.return_value = mock_test_case

        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="bias", metric_name="偏见检测", score=0.9, success=True,
        )

        items = [
            {"input_text": "问题1", "actual_output": "回答1"},
            {"input_text": "问题2", "actual_output": "回答2"},
            {"input_text": "问题3", "actual_output": "回答3"},
        ]

        results = asyncio.run(
            SafetyEvaluator.evaluate_batch(
                items=items,
                metric_key="bias",
                threshold=0.7,
                max_concurrent=2,
            )
        )

        assert len(results) == 3
        assert all(isinstance(r, TransformedMetricResult) for r in results)
        assert mock_evaluate.call_count == 3

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_batch_concurrency_control(self, mock_create_test_case, mock_evaluate):
        """测试批量评测的并发控制"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_create_test_case.return_value = MagicMock()

        call_log = []

        def side_effect_func(test_case, metric_key, threshold=0.5, **kwargs):
            call_log.append(metric_key)
            return TransformedMetricResult(
                metric_key=metric_key, metric_name=metric_key, score=0.8, success=True,
            )

        mock_evaluate.side_effect = side_effect_func

        items = [
            {"input_text": f"Q{i}", "actual_output": f"A{i}"}
            for i in range(5)
        ]

        results = asyncio.run(
            SafetyEvaluator.evaluate_batch(
                items=items,
                metric_key="toxicity",
                threshold=0.5,
                max_concurrent=2,
            )
        )

        assert len(results) == 5
        assert len(call_log) == 5

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_batch_empty_items(self, mock_create_test_case, mock_evaluate):
        """测试空列表返回空结果"""
        from app.services.safety_evaluator import SafetyEvaluator

        results = asyncio.run(
            SafetyEvaluator.evaluate_batch(
                items=[],
                metric_key="bias",
                threshold=0.5,
            )
        )

        assert results == []
        mock_evaluate.assert_not_called()

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_batch_handles_partial_failure(self, mock_create_test_case, mock_evaluate):
        """测试批量评测部分失败时不影响其他评测"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_create_test_case.return_value = MagicMock()

        call_count = [0]

        def side_effect_func(test_case, metric_key, threshold=0.5, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("模拟失败")
            return TransformedMetricResult(
                metric_key=metric_key, metric_name=metric_key, score=0.7, success=True,
            )

        mock_evaluate.side_effect = side_effect_func

        items = [
            {"input_text": f"Q{i}", "actual_output": f"A{i}"}
            for i in range(3)
        ]

        results = asyncio.run(
            SafetyEvaluator.evaluate_batch(
                items=items,
                metric_key="bias",
                threshold=0.5,
                max_concurrent=1,
            )
        )

        assert len(results) == 3
        # 第二个应为失败结果
        assert results[1].success is False
        assert results[1].error is not None
        # 其他应为成功结果
        assert results[0].success is True
        assert results[2].success is True

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_batch_passes_metric_name(self, mock_create_test_case, mock_evaluate):
        """测试批量评测传递自定义 metric_name"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="toxicity", metric_name="毒性检测", score=0.8, success=True,
        )

        items = [{"input_text": "Q1", "actual_output": "A1"}]

        asyncio.run(
            SafetyEvaluator.evaluate_batch(
                items=items,
                metric_key="toxicity",
                threshold=0.5,
                metric_name="毒性检测",
            )
        )

        mock_evaluate.assert_called_once()
        call_kwargs = mock_evaluate.call_args
        assert call_kwargs.kwargs.get("metric_name") == "毒性检测"

    @patch("app.services.safety_evaluator.deepeval_adapter.evaluate")
    @patch("app.services.safety_evaluator.deepeval_adapter.create_test_case")
    def test_evaluate_batch_default_metric_name(self, mock_create_test_case, mock_evaluate):
        """测试批量评测未指定 metric_name 时默认使用 metric_key"""
        from app.services.safety_evaluator import SafetyEvaluator

        mock_create_test_case.return_value = MagicMock()
        mock_evaluate.return_value = TransformedMetricResult(
            metric_key="bias", metric_name="bias", score=0.8, success=True,
        )

        items = [{"input_text": "Q1", "actual_output": "A1"}]

        asyncio.run(
            SafetyEvaluator.evaluate_batch(
                items=items,
                metric_key="bias",
                threshold=0.5,
            )
        )

        mock_evaluate.assert_called_once()
        call_kwargs = mock_evaluate.call_args
        # 未指定时应使用 metric_key 作为 metric_name
        assert call_kwargs.kwargs.get("metric_name") is None or call_kwargs.kwargs.get("metric_name") == "bias"
