"""
W16 独立复核测试（测试角色）：deepeval 接入平台模型网关契约（整改计划 §4 W16 / P0-D）

与开发交付的 tests/unit/test_w16_deepeval_gateway.py 互补，覆盖其盲区：
1. 注册表 20 个指标类「真实构造」接受注入的 PlatformLLMWrapper——开发侧仅对
   bias 用真实类验证，其余 18 个经 MagicMock；本文件对全部 20 个指标真实
   实例化（含 ToolCorrectness 等确定性指标），实证 model 注入不破坏构造；
2. misuse/non_advice/role_violation 缺业务必填参数（domain/advice_types/role）
   时 flag 开/关行为一致——TypeError 发生在指标构造，wrapper 注入不改变
   失败模式（存量现状，flag 开启不引入新失败方式）；
3. 真实 httpx 连接拒绝（断网模拟，127.0.0.1:9 discard 端口，非人为 raise）
   → metric_unavailable 而非伪装 0 分；
4. transform_to_evaluation_result.dimension_scores 透传 metric_unavailable
   （开发侧只断言了不崩溃与 score 归 0，未断言透传字段）；
5. 环境变量 → Settings → _platform_llm_enabled 的真实链路（不经 patch flag
   函数），验证 flag 真的从环境读取；
6. evaluate 声明的「调用方显式 model 优先，注入不覆盖」（setdefault 语义）。
"""
import os
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.core.config import Settings, get_settings
from app.services import deepeval_adapter
from app.services.deepeval_adapter import _platform_llm_enabled, create_test_case, evaluate
from app.services.deepeval_metric_registry import DeepEvalMetricRegistry
from app.services.deepeval_result_transformer import (
    DeepEvalResultTransformer,
    TransformedMetricResult,
)
from app.services.platform_llm_wrapper import PlatformLLMWrapper

# 指标构造缺省的业务必填参数（生产路径 safety_evaluator 目前未传，见
# TestMissingBizParamsConsistency；本文件验证的是 model 接受性，故补齐）
_EXTRA_REQUIRED_KWARGS = {
    "misuse": {"domain": "code_generation"},
    "non_advice": {"advice_types": ["medical", "legal"]},
    "role_violation": {"role": "helpful assistant"},
}


def _gateway_config(endpoint: str = "http://127.0.0.1:9/v1/chat/completions") -> dict:
    return {
        "provider": "openai",
        "model_type": "tcheck-gateway-model",
        "endpoint": endpoint,
        "api_key": "tcheck-key",
    }


@pytest.fixture(autouse=True)
def _dummy_openai_key():
    """指标实例化时可能读取 OPENAI_API_KEY，统一给 dummy 值避免环境差异"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-dummy-tcheck"}):
        yield


class TestAllRegistryMetricsAcceptInjectedModel:
    """注册表全部 20 个指标类真实构造接受注入的 PlatformLLMWrapper"""

    @pytest.mark.parametrize(
        "metric_key",
        sorted(DeepEvalMetricRegistry.list_available_metrics().keys()),
    )
    def test_real_metric_constructs_with_wrapper(self, metric_key):
        """真实 create_metric（不 mock registry）：注入 wrapper 后构造成功且 model 即 wrapper"""
        wrapper = PlatformLLMWrapper(model_config=_gateway_config())

        metric = DeepEvalMetricRegistry.create_metric(
            metric_key,
            threshold=0.5,
            model=wrapper,
            **_EXTRA_REQUIRED_KWARGS.get(metric_key, {}),
        )

        assert metric is not None
        assert metric.model is wrapper

    def test_flag_on_evaluate_injects_into_real_metric_construction(self):
        """evaluate 全链路（flag 开、真实 registry 不 mock）：注入发生在真实构造前，
        故 answer_relevancy 真实实例携带 wrapper（measure 前构造不外呼）"""
        captured = {}

        # classmethod 的真实函数（patch 之前取；patch 替换类属性后不再自动绑定 cls）
        original_create = DeepEvalMetricRegistry.__dict__["create_metric"].__func__

        def spy_create(metric_key, threshold=0.5, **kwargs):
            metric = original_create(
                DeepEvalMetricRegistry, metric_key, threshold=threshold, **kwargs
            )
            captured["metric"] = metric
            return metric

        with patch(
            "app.services.deepeval_adapter._platform_llm_enabled", return_value=True
        ), patch(
            "app.services.deepeval_adapter._resolve_platform_model_config",
            return_value=_gateway_config(),
        ), patch(
            "app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric",
            side_effect=spy_create,
        ):
            # answer_relevancy 构造成功后 measure 会经 wrapper 打到不可达端点，
            # 按语义应落 metric_unavailable（连接拒绝场景在 TestRealConnectionRefused
            # 用 bias 覆盖，这里断言注入穿透到真实构造的指标实例）
            result = evaluate(
                test_case=create_test_case(input_text="q", actual_output="a"),
                metric_key="answer_relevancy",
            )

        assert isinstance(captured["metric"].model, PlatformLLMWrapper)
        assert result.metric_unavailable is True  # 不可达端点 → 明确不可用


class TestSafetyBizParamsProductionDefaults:
    """W16 复核第 2 轮：缺陷 3 修复后生产默认参数下的指标可用性契约

    misuse/non_advice：生产默认（domain="general" / advice_types=[medical,legal,
    financial]）构造成功，错误只能来自网关调用阶段（wrapper 的 RuntimeError
    包装「模型调用失败」），不再构造即 TypeError。

    role_violation：RoleViolationMetric 构造函数运行时必填 role（ValueError
    校验，inspect 签名 role: str = None 看不到）——当前实现生产默认路径
    （未传 role）构造即 ValueError，仍恒 metric_unavailable，与交付声明
    「消除三项恒不可用」不符（xfail strict 待修，修复后 XPASS 提示移除标记）。

    区分器：构造阶段失败的 error 是 TypeError/ValueError 原文；走到网关阶段
    的 error 含 wrapper 的「模型调用失败」包装。用正向断言而非排除法——
    排除 TypeError 特征无法区分 ValueError 型构造失败。
    """

    @pytest.mark.parametrize(
        "eval_method,metric_key",
        [
            ("evaluate_misuse", "misuse"),
            ("evaluate_non_advice", "non_advice"),
        ],
    )
    def test_production_default_construction_reaches_gateway_stage(
        self, eval_method, metric_key
    ):
        """修复后 misuse/non_advice 经生产入口（SafetyEvaluator 默认参数）构造成功，
        错误必须来自网关调用阶段（注意：业务参数默认值在生产入口层，直接调
        deepeval_adapter.evaluate 不在此契约内——那是调用方自行传参的路径）"""
        from app.services.safety_evaluator import SafetyEvaluator

        with patch(
            "app.services.deepeval_adapter._platform_llm_enabled", return_value=True
        ), patch(
            "app.services.deepeval_adapter._resolve_platform_model_config",
            return_value=_gateway_config(),
        ):
            result = getattr(SafetyEvaluator, eval_method)(
                input_text="q", actual_output="a"
            )

        assert result.metric_key == metric_key
        assert result.metric_unavailable is True
        assert result.score is None
        # 正向区分断言：错误必须是网关层包装，而非构造缺参（TypeError 或
        # ValueError 原文都不含该包装）
        assert "模型调用失败" in result.error

    def test_misuse_and_non_advice_construct_with_production_defaults(self):
        """生产默认参数真实构造成功（不经过 evaluate，直接的构造可用性证据）"""
        wrapper = PlatformLLMWrapper(model_config=_gateway_config())

        misuse_metric = DeepEvalMetricRegistry.create_metric(
            "misuse", threshold=0.5, model=wrapper, domain="general"
        )
        assert misuse_metric.model is wrapper

        non_advice_metric = DeepEvalMetricRegistry.create_metric(
            "non_advice",
            threshold=0.5,
            model=wrapper,
            advice_types=["medical", "legal", "financial"],
        )
        assert non_advice_metric.model is wrapper

    def test_role_violation_production_default_reaches_gateway_stage(self):
        """role_violation 生产默认路径应与其他安全指标一致：构造成功、错误来自网关

        W16 复核第 2 轮修复后转绿（原 xfail strict 标记已移除）：
        RoleViolationMetric 运行时必填 role（构造函数内部 ValueError 校验），
        生产默认「通用助手」传入构造后走到网关阶段。
        """
        from app.services.safety_evaluator import SafetyEvaluator

        with patch(
            "app.services.deepeval_adapter._platform_llm_enabled", return_value=True
        ), patch(
            "app.services.deepeval_adapter._resolve_platform_model_config",
            return_value=_gateway_config(),
        ):
            result = SafetyEvaluator.evaluate_role_violation(
                input_text="q", actual_output="a"
            )

        assert result.metric_unavailable is True
        assert result.score is None
        # 正向区分断言：构造成功走到网关阶段（error 含「模型调用失败」包装），
        # 而非构造阶段 ValueError("Role parameter is required...")
        assert "模型调用失败" in result.error
        assert "Role parameter" not in result.error


class TestRealConnectionRefused:
    """断网模拟：真实 httpx 连接拒绝 → metric_unavailable 而非伪装 0 分"""

    def test_unreachable_gateway_marks_unavailable(self):
        """G-Eval(bias) 经 wrapper 打到 127.0.0.1:9（discard，连接拒绝）→ 明确不可用"""
        result = evaluate(
            test_case=create_test_case(input_text="测试输入", actual_output="测试输出"),
            metric_key="bias",
            model_config=_gateway_config(),
        )

        assert result.metric_unavailable is True
        assert result.score is None
        assert result.score != 0.0  # 明确不是伪装 0 分
        assert result.success is False
        assert result.error is not None
        assert "模型调用失败" in result.error or "网关" in result.error or "Error" in result.error or "失败" in result.error

    def test_unreachable_gateway_batch_marks_unavailable(self):
        """批量路径同样把连接拒绝标为 metric_unavailable"""
        import asyncio

        results = asyncio.run(
            deepeval_adapter.evaluate_batch(
                test_cases=[create_test_case(input_text="q", actual_output="a")],
                metric_configs=[{"key": "bias", "model_config": _gateway_config()}],
                max_concurrent=1,
            )
        )
        assert len(results) == 1
        assert results[0].metric_unavailable is True
        assert results[0].score is None
        assert results[0].success is False


class TestDimensionScoresCarryUnavailableFlag:
    """transform_to_evaluation_result.dimension_scores 透传 metric_unavailable"""

    def test_unavailable_flag_passthrough_mixed_results(self):
        # 用真实 TransformedMetricResult 走两条路径：正常评测结果（不带标记）
        # 与 evaluate 失败语义结果（score=None + metric_unavailable=True），
        # 再经 transform_to_evaluation_result 的 transform_metric_result 重建
        # 注意不能用裸 MagicMock 当输入：MagicMock 任意属性访问都成功，
        # getattr(x, 'metric_unavailable', False) 的默认值分支永远不会走到，
        # 无法代表「对象没有该属性」的真实 deepeval 指标形态
        ok_real = TransformedMetricResult(
            metric_key="bias",
            metric_name="偏见检测",
            score=0.9,
            reason="ok",
            success=True,
        )
        bad_real = TransformedMetricResult(
            metric_key="toxicity",
            metric_name="毒性检测",
            score=None,
            success=False,
            metric_unavailable=True,
            error="网关连接失败",
        )

        evaluation = DeepEvalResultTransformer.transform_to_evaluation_result(
            metric_results=[ok_real, bad_real],
            metric_configs=[
                {"key": "bias", "name": "偏见检测", "weight": 1},
                {"key": "toxicity", "name": "毒性检测", "weight": 1},
            ],
        )

        assert evaluation.total_metrics == 2
        assert evaluation.passed_metrics == 1
        assert evaluation.failed_metrics == 1
        # 正常指标不带 unavailable 标记
        assert evaluation.dimension_scores["bias"]["metric_unavailable"] is False
        assert evaluation.dimension_scores["bias"]["score"] == 0.9
        # 不可用指标透传标记，分数按 0 参与不崩溃
        assert evaluation.dimension_scores["toxicity"]["metric_unavailable"] is True
        assert evaluation.dimension_scores["toxicity"]["score"] == 0.0
        # 加权平均只按正常指标出分：(0.9*1)/(1+1)=0.45（不可用项按 0 参与）
        assert evaluation.overall_score == pytest.approx(0.45)

    def test_raw_metric_none_score_defaults_zero_keeps_flag_false(self):
        """正常评测路径中指标 score=None 的存量归一行为不被破坏（不误标 unavailable）

        用无 metric_unavailable 属性的普通对象模拟真实 deepeval 指标形态：
        getattr 默认值分支必须生效为 False（MagicMock 不适用——其任意属性
        访问都成功，永远走不到默认值）。
        """

        class _RawMetric:
            """模拟真实 deepeval 指标对象：无 metric_unavailable 属性"""

            def __init__(self):
                self.score = None
                self.reason = None
                self.success = None
                self.error = None
                self.verdicts = None
                self.score_breakdown = None

        raw = _RawMetric()
        assert not hasattr(raw, "metric_unavailable")

        transformed = DeepEvalResultTransformer.transform_metric_result(
            metric_result=raw, metric_key="bias", metric_name="偏见检测"
        )
        assert transformed.score == 0.0
        assert transformed.metric_unavailable is False


class TestFlagEnvChain:
    """环境变量 → Settings → _platform_llm_enabled 真实链路（不 patch flag 函数）"""

    def test_settings_default_false_without_env(self):
        s = Settings(_env_file=None)
        assert s.DEEPEVAL_USE_PLATFORM_LLM is False
        assert s.DEEPEVAL_PLATFORM_MODEL_ID == ""

    def test_settings_parses_env_true(self, monkeypatch):
        monkeypatch.setenv("DEEPEVAL_USE_PLATFORM_LLM", "true")
        s = Settings(_env_file=None)
        assert s.DEEPEVAL_USE_PLATFORM_LLM is True

    def test_platform_llm_enabled_reads_env_via_get_settings(self, monkeypatch):
        monkeypatch.setenv("DEEPEVAL_USE_PLATFORM_LLM", "true")
        get_settings.cache_clear()
        try:
            assert _platform_llm_enabled() is True

            monkeypatch.setenv("DEEPEVAL_USE_PLATFORM_LLM", "false")
            get_settings.cache_clear()
            assert _platform_llm_enabled() is False
        finally:
            monkeypatch.undo()
            get_settings.cache_clear()


class TestExplicitModelWins:
    """evaluate 声明的「调用方显式 model 优先，注入不覆盖」（setdefault 语义）"""

    def test_caller_model_not_overridden_by_platform_wrapper(self):
        caller_model = MagicMock(name="caller_model")

        with patch(
            "app.services.deepeval_adapter._platform_llm_enabled", return_value=True
        ), patch(
            "app.services.deepeval_adapter._resolve_platform_model_config",
            return_value=_gateway_config(),
        ) as mock_resolve, patch(
            "app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric"
        ) as mock_create:
            mock_create.return_value = MagicMock(
                score=0.8, success=True, reason=None, error=None,
                verdicts=None, score_breakdown=None,
            )
            evaluate(
                test_case=create_test_case(input_text="q", actual_output="a"),
                metric_key="bias",
                model=caller_model,
            )

        # 注入不覆盖显式 model，也不再需要解析网关配置
        assert mock_create.call_args.kwargs["model"] is caller_model
        mock_resolve.assert_called()
