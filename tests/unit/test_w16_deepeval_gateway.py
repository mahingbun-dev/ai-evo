"""
W16 deepeval 接入平台模型网关 —— mock 网关契约测试（整改计划 §4 W16 / P0-D）

覆盖三块契约：
1. feature flag（DEEPEVAL_USE_PLATFORM_LLM，默认 false）：
   - flag 关：create_metric 不注入 model，行为与现状完全一致；
   - flag 开：evaluate 经 PlatformLLMWrapper 注入 model（safety/agent 全指标路径）；
2. 网关异常语义（不受 flag 控制）：适配失败标 metric_unavailable，
   score 不再伪装 0.0（None 表示无分数），success=False；
3. 配置与结果结构契约：.env.example 声明待 D-6 决策的键、
   TransformedMetricResult 序列化含 metric_unavailable、聚合对 None score 防御。
"""
import asyncio
import dataclasses
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.core.config import Settings
from app.services import deepeval_adapter
from app.services.deepeval_adapter import create_test_case, evaluate, evaluate_batch
from app.services.deepeval_metric_registry import (
    DeepEvalMetricRegistry,
    MetricNotFoundError,
)
from app.services.deepeval_result_transformer import (
    DeepEvalResultTransformer,
    TransformedMetricResult,
)
from app.services.platform_llm_wrapper import PlatformLLMWrapper
from app.services.safety_evaluator import SafetyEvaluator

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# deepeval 指标实例化时可能读取 OpenAI key，统一给 dummy 值避免环境差异
_DUMMY_API_KEY_ENV = {"OPENAI_API_KEY": "sk-test-dummy-key-for-testing"}


@pytest.fixture(autouse=True)
def mock_openai_api_key():
    """为所有测试提供 dummy OPENAI_API_KEY"""
    with patch.dict(os.environ, _DUMMY_API_KEY_ENV):
        yield


class _BadGatewayWrapper(PlatformLLMWrapper):
    """模拟平台网关故障的 wrapper：_generate 阶段连接被拒"""

    def _generate(self, prompt: str) -> str:
        raise RuntimeError("网关连接失败: connection refused")


class _MockGatewayHandler(BaseHTTPRequestHandler):
    """进程内 OpenAI 兼容 mock 网关：按 G-Eval 多轮协议分支应答

    G-Eval（BiasMetric 4.0.4）调用链：
    1. 生成 OPINIONS 列表 -> {"opinions": [...]}
    2. 逐条出 VERDICT -> {"verdicts": [...]}
    3. 打分 -> {"reason", "score"}
    """

    received: list = []
    status_code: int = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        prompt = body["messages"][0]["content"]
        _MockGatewayHandler.received.append(
            {"auth": self.headers.get("Authorization"), "model": body.get("model")}
        )
        if _MockGatewayHandler.status_code != 200:
            self.send_response(_MockGatewayHandler.status_code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if "OPINION" in prompt:
            content = json.dumps({"opinions": ["mock opinion 1", "mock opinion 2"]})
        elif "VERDICT" in prompt or "verdict" in prompt:
            content = json.dumps(
                {
                    "verdicts": [
                        {"verdict": "no", "reason": "mock verdict reason 1"},
                        {"verdict": "no", "reason": "mock verdict reason 2"},
                    ]
                }
            )
        else:
            content = json.dumps({"reason": "mock gateway verdict", "score": 0})

        resp = {"choices": [{"message": {"content": content}}]}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture()
def mock_gateway():
    """进程内 mock 平台网关：返回 (base_url, received_requests)"""
    _MockGatewayHandler.received = []
    _MockGatewayHandler.status_code = 200
    server = HTTPServer(("127.0.0.1", 0), _MockGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
            _MockGatewayHandler.received,
        )
    finally:
        server.shutdown()
        server.server_close()


def _make_metric_mock(score=0.85, success=True, reason="ok"):
    """构造一个行为接近真实 deepeval 指标的 mock

    注意 metric_unavailable 必须显式赋值：MagicMock 任意属性访问都成功，
    transform_metric_result 的 getattr(x, 'metric_unavailable', False)
    默认值分支永远不会走到，会把 MagicMock 对象透传进结果。
    """
    metric = MagicMock()
    metric.measure.return_value = score
    metric.score = score
    metric.reason = reason
    metric.success = success
    metric.score_breakdown = None
    metric.error = None
    metric.verdicts = None
    metric.metric_unavailable = False
    return metric


def _make_gateway_config() -> dict:
    return {
        "provider": "openai",
        "model_type": "platform-gateway-model",
        "endpoint": "http://127.0.0.1:9/v1/chat/completions",
        "api_key": "gateway-key",
    }


# ============================================================
# 一、feature flag 契约
# ============================================================
class TestFeatureFlagContract:
    """flag 关→与现状一致；flag 开→wrapper 被使用"""

    def test_settings_default_flag_false(self):
        """配置 schema：DEEPEVAL_USE_PLATFORM_LLM 默认 false（D-6 未决策前保守关闭）"""
        field = Settings.model_fields["DEEPEVAL_USE_PLATFORM_LLM"]
        assert field.default is False

    def test_env_example_declares_flag_pending_d6(self):
        """.env.example 必须声明该键，并注明「待 D-6 技术决策后启用」"""
        content = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "DEEPEVAL_USE_PLATFORM_LLM" in content
        assert "D-6" in content
        assert "DEEPEVAL_PLATFORM_MODEL_ID" in content

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_flag_off_no_model_injected(self, mock_create_metric):
        """flag 关（默认）：create_metric 收不到 model 参数，行为与现状一致"""
        mock_create_metric.return_value = _make_metric_mock()

        result = evaluate(
            test_case=create_test_case(input_text="q", actual_output="a"),
            metric_key="bias",
            threshold=0.5,
        )

        assert result.score == 0.85
        assert result.success is True
        assert result.metric_unavailable is False
        assert mock_create_metric.call_args.kwargs.get("model") is None
        assert "model" not in mock_create_metric.call_args.kwargs

    @patch("app.services.deepeval_adapter._platform_llm_enabled", return_value=True)
    @patch("app.services.deepeval_adapter._resolve_platform_model_config")
    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_flag_on_injects_platform_wrapper(self, mock_create_metric, mock_resolve, _flag):
        """flag 开：create_metric 收到 PlatformLLMWrapper 实例作为 model"""
        mock_create_metric.return_value = _make_metric_mock()
        mock_resolve.return_value = _make_gateway_config()

        result = evaluate(
            test_case=create_test_case(input_text="q", actual_output="a"),
            metric_key="bias",
            threshold=0.5,
        )

        assert result.score == 0.85
        injected_model = mock_create_metric.call_args.kwargs["model"]
        assert isinstance(injected_model, PlatformLLMWrapper)
        assert injected_model.model_config == _make_gateway_config()
        assert injected_model.get_model_name() == "platform-gateway-model"

    @patch("app.services.deepeval_adapter._platform_llm_enabled", return_value=True)
    @patch("app.services.deepeval_adapter._resolve_platform_model_config")
    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_flag_on_explicit_model_config_skips_resolver(
        self, mock_create_metric, mock_resolve, _flag
    ):
        """flag 开 + 调用方显式传 model_config：直接使用，不再查库解析"""
        mock_create_metric.return_value = _make_metric_mock()

        explicit_cfg = {
            "provider": "anthropic",
            "model_type": "explicit-model",
            "endpoint": "http://explicit.example/v1/messages",
        }
        evaluate(
            test_case=create_test_case(input_text="q", actual_output="a"),
            metric_key="toxicity",
            model_config=explicit_cfg,
        )

        mock_resolve.assert_not_called()
        injected_model = mock_create_metric.call_args.kwargs["model"]
        assert injected_model.model_config == explicit_cfg

    @patch("app.services.deepeval_adapter._platform_llm_enabled", return_value=True)
    @patch("app.services.deepeval_adapter._resolve_platform_model_config")
    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    @pytest.mark.parametrize(
        "metric_key",
        sorted(DeepEvalMetricRegistry.get_category_metrics("safety").keys())
        + sorted(DeepEvalMetricRegistry.get_category_metrics("agent").keys()),
    )
    def test_flag_on_covers_safety_and_agent_paths(
        self, mock_create_metric, mock_resolve, _flag, metric_key
    ):
        """flag 开：safety/agent 每个指标的创建路径都注入 wrapper"""
        mock_create_metric.return_value = _make_metric_mock()
        mock_resolve.return_value = _make_gateway_config()

        evaluate(
            test_case=create_test_case(input_text="q", actual_output="a"),
            metric_key=metric_key,
        )

        assert "model" in mock_create_metric.call_args.kwargs
        assert isinstance(
            mock_create_metric.call_args.kwargs["model"], PlatformLLMWrapper
        )

    @patch("app.services.deepeval_adapter._platform_llm_enabled", return_value=True)
    @patch("app.services.deepeval_adapter._resolve_platform_model_config")
    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_flag_on_resolver_no_config_marks_unavailable(
        self, mock_create_metric, mock_resolve, _flag
    ):
        """flag 开但解析不到网关模型配置：明确 metric_unavailable，不回退伪装"""
        mock_resolve.return_value = None

        result = evaluate(
            test_case=create_test_case(input_text="q", actual_output="a"),
            metric_key="bias",
        )

        mock_create_metric.assert_not_called()
        assert result.metric_unavailable is True
        assert result.score is None
        assert result.success is False
        assert result.error is not None

    @patch("app.services.deepeval_adapter._platform_llm_enabled", return_value=True)
    @patch("app.services.deepeval_adapter._resolve_platform_model_config")
    def test_flag_on_resolver_failure_marks_unavailable(self, mock_resolve, _flag):
        """flag 开且配置解析抛异常（DB 不可用等）：metric_unavailable 而非崩溃/0 分"""
        mock_resolve.side_effect = RuntimeError("database is unavailable")

        result = evaluate(
            test_case=create_test_case(input_text="q", actual_output="a"),
            metric_key="bias",
        )

        assert result.metric_unavailable is True
        assert result.score is None
        assert result.success is False
        assert "database is unavailable" in result.error

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_flag_off_ignores_explicit_model_config(self, mock_create_metric):
        """flag 关：即使调用方传了 model_config 也不注入（flag 是总开关）"""
        mock_create_metric.return_value = _make_metric_mock()

        evaluate(
            test_case=create_test_case(input_text="q", actual_output="a"),
            metric_key="bias",
            model_config=_make_gateway_config(),
        )

        assert "model" not in mock_create_metric.call_args.kwargs


# ============================================================
# 二、网关异常语义（metric_unavailable，不受 flag 控制）
# ============================================================
class TestGatewayErrorSemantics:
    """网关异常 → metric_unavailable 而非伪装 0 分"""

    def test_gateway_error_marks_unavailable_with_real_metric(self):
        """真实指标 + 故障网关 wrapper（flag 开）：measure 抛异常 → metric_unavailable、score 为 None"""
        gateway_config = _make_gateway_config()

        with patch(
            "app.services.deepeval_adapter._platform_llm_enabled", return_value=True
        ), patch(
            "app.services.deepeval_adapter.PlatformLLMWrapper", _BadGatewayWrapper
        ):
            result = evaluate(
                test_case=create_test_case(input_text="q", actual_output="a"),
                metric_key="bias",
                model_config=gateway_config,
            )

        assert result.metric_unavailable is True
        assert result.score is None
        assert result.score != 0.0
        assert result.success is False
        assert "网关连接失败" in result.error

    def test_measure_error_marks_unavailable_flag_off(self):
        """异常语义修正不受 flag 控制：flag 关时指标 measure 抛异常同样标 metric_unavailable"""
        with patch(
            "app.services.deepeval_adapter._platform_llm_enabled", return_value=False
        ), patch(
            "app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric"
        ) as mock_create:
            metric = _make_metric_mock()
            metric.measure.side_effect = RuntimeError("OpenAI API 调用失败")
            mock_create.return_value = metric

            result = evaluate(
                test_case=create_test_case(input_text="q", actual_output="a"),
                metric_key="toxicity",
            )

        assert result.metric_unavailable is True
        assert result.score is None
        assert result.success is False
        assert "OpenAI API 调用失败" in result.error

    def test_metric_not_found_marks_unavailable(self):
        """指标 key 不存在 → metric_unavailable（指标不可用语义的一部分）"""
        result = evaluate(
            test_case=create_test_case(input_text="q", actual_output="a"),
            metric_key="no_such_metric_key",
        )

        assert result.metric_unavailable is True
        assert result.score is None
        assert result.success is False
        assert "no_such_metric_key" in result.error

    def test_evaluate_batch_uncaught_exception_marks_unavailable(self):
        """批量路径兜底：未捕获异常转结果时同样标 metric_unavailable、score None"""
        with patch("app.services.deepeval_adapter.evaluate") as mock_evaluate:
            mock_evaluate.side_effect = RuntimeError("Simulated failure")

            results = asyncio.run(
                evaluate_batch(
                    test_cases=[create_test_case(input_text="q", actual_output="a")],
                    metric_configs=[{"key": "bias"}],
                    max_concurrent=1,
                )
            )

        assert len(results) == 1
        assert results[0].metric_unavailable is True
        assert results[0].score is None
        assert results[0].success is False

    def test_evaluate_batch_success_path_unaffected(self):
        """批量路径正常分支不受语义修正影响"""
        with patch("app.services.deepeval_adapter.evaluate") as mock_evaluate:
            mock_evaluate.return_value = TransformedMetricResult(
                metric_key="bias",
                metric_name="bias",
                score=0.7,
                success=True,
            )

            results = asyncio.run(
                evaluate_batch(
                    test_cases=[create_test_case(input_text="q", actual_output="a")],
                    metric_configs=[{"key": "bias"}],
                    max_concurrent=1,
                )
            )

        assert results[0].score == 0.7
        assert results[0].success is True
        assert results[0].metric_unavailable is False


# ============================================================
# 三、wrapper / registry / transformer 结构契约
# ============================================================
class TestStructureContracts:
    """wrapper 注入、结果结构与聚合防御"""

    def test_registry_create_metric_accepts_model_kwarg(self):
        """registry.create_metric 透传 model 给指标构造函数"""
        wrapper = PlatformLLMWrapper(model_config=_make_gateway_config())
        metric = DeepEvalMetricRegistry.create_metric(
            "bias", threshold=0.5, model=wrapper
        )
        assert metric.model is wrapper

    def test_wrapper_constructed_from_platform_config(self):
        """PlatformLLMWrapper 从平台模型配置构造并暴露模型名"""
        wrapper = PlatformLLMWrapper(model_config=_make_gateway_config())
        assert wrapper.get_model_name() == "platform-gateway-model"

    def test_transformed_result_has_metric_unavailable_field(self):
        """TransformedMetricResult 含 metric_unavailable 字段，asdict 序列化保留"""
        assert "metric_unavailable" in TransformedMetricResult.__dataclass_fields__

        result = TransformedMetricResult(
            metric_key="bias",
            metric_name="偏见检测",
            score=None,
            success=False,
            metric_unavailable=True,
            error="网关异常",
        )
        payload = dataclasses.asdict(result)
        assert payload["metric_unavailable"] is True
        assert payload["score"] is None

    def test_aggregate_treats_unavailable_as_zero_without_crash(self):
        """聚合入口对 metric_unavailable（score=None）防御：按 0 参与加权不崩溃"""
        unavailable_metric = MagicMock()
        unavailable_metric.score = None
        unavailable_metric.reason = None
        unavailable_metric.success = False
        unavailable_metric.error = "网关异常"

        evaluation = DeepEvalResultTransformer.transform_to_evaluation_result(
            metric_results=[unavailable_metric],
            metric_configs=[{"key": "bias", "name": "偏见检测", "weight": 1}],
        )
        assert evaluation.total_metrics == 1
        assert evaluation.failed_metrics == 1
        assert evaluation.dimension_scores["bias"]["score"] == 0.0


# ============================================================
# 四、evaluator 层兜底一致性
# ============================================================
class TestEvaluatorFallbackConsistency:
    """safety/agent evaluator 的异常兜底不再伪装 0 分"""

    def test_safety_evaluate_batch_exception_marks_unavailable(self):
        from app.services.safety_evaluator import SafetyEvaluator

        with patch(
            "app.services.safety_evaluator.deepeval_adapter.evaluate"
        ) as mock_evaluate:
            mock_evaluate.side_effect = RuntimeError("评测执行崩溃")

            results = asyncio.run(
                SafetyEvaluator.evaluate_batch(
                    items=[
                        {"input_text": "q1", "actual_output": "a1"},
                        {"input_text": "q2", "actual_output": "a2"},
                    ],
                    metric_key="bias",
                    max_concurrent=1,
                )
            )

        assert len(results) == 2
        for r in results:
            assert r.metric_unavailable is True
            assert r.score is None
            assert r.success is False

    def test_safety_evaluate_all_missing_result_marks_unavailable(self):
        """gather 中单个指标异常被吞掉后，补全兜底必须是 metric_unavailable 而非伪装 0 分"""
        from app.services.safety_evaluator import SafetyEvaluator

        def _ok(test_case, metric_key, threshold=0.5, metric_name=None, **kwargs):
            return TransformedMetricResult(
                metric_key=metric_key,
                metric_name=metric_name or metric_key,
                score=0.9,
                success=True,
            )

        with patch(
            "app.services.safety_evaluator.SafetyEvaluator.evaluate_bias",
            side_effect=RuntimeError("评测执行崩溃"),
        ), patch(
            "app.services.safety_evaluator.deepeval_adapter.evaluate",
            side_effect=_ok,
        ):
            results = asyncio.run(
                SafetyEvaluator.evaluate_all(input_text="q", actual_output="a")
            )

        assert results["bias"].metric_unavailable is True
        assert results["bias"].score is None
        assert results["bias"].success is False
        # 其余指标正常出分
        assert results["toxicity"].score == 0.9
        assert results["toxicity"].metric_unavailable is False

    def test_agent_evaluate_all_exception_marks_unavailable(self):
        from app.services.agent_evaluator import AgentEvaluator

        ok_tool = TransformedMetricResult(
            metric_key="tool_correctness",
            metric_name="工具正确性",
            score=0.8,
            success=True,
        )
        ok_step = TransformedMetricResult(
            metric_key="step_efficiency",
            metric_name="步骤效率",
            score=0.8,
            success=True,
        )
        with patch(
            "app.services.agent_evaluator.AgentEvaluator.evaluate_task_completion",
            side_effect=RuntimeError("评测执行崩溃"),
        ), patch(
            "app.services.agent_evaluator.AgentEvaluator.evaluate_tool_correctness",
            return_value=ok_tool,
        ), patch(
            "app.services.agent_evaluator.AgentEvaluator.evaluate_step_efficiency",
            return_value=ok_step,
        ):
            results = asyncio.run(
                AgentEvaluator.evaluate_all(task="task", input_text="q", actual_output="a")
            )

        assert results["task_completion"].metric_unavailable is True
        assert results["task_completion"].score is None
        assert results["task_completion"].success is False
        assert results["tool_correctness"].score == 0.8


# ============================================================
# 五、HTTP 层 mock 网关全链路（进程内 OpenAI 兼容服务，无外部依赖）
# ============================================================
class TestMockGatewayEndToEnd:
    """真实 G-Eval 指标 + 进程内 mock 平台网关的 HTTP 全链路契约"""

    def _gateway_config(self, base_url: str) -> dict:
        return {
            "provider": "openai",
            "model_type": "gateway-mock-model",
            "endpoint": base_url,
            "api_key": "gateway-secret",
        }

    @patch("app.services.deepeval_adapter._platform_llm_enabled", return_value=True)
    def test_flag_on_full_chain_scores_via_gateway(self, _flag, mock_gateway):
        """flag 开：G-Eval 多轮协议全部经 PlatformLLMWrapper 打到平台网关并真实出分"""
        base_url, received = mock_gateway

        result = evaluate(
            test_case=create_test_case(input_text="测试输入", actual_output="测试输出"),
            metric_key="bias",
            model_config=self._gateway_config(base_url),
        )

        # 网关确实收到了请求，且带了网关凭证与模型名
        assert len(received) >= 1
        assert all(r["auth"] == "Bearer gateway-secret" for r in received)
        assert all(r["model"] == "gateway-mock-model" for r in received)

        # G-Eval 完整走完 opinions -> verdicts -> score 三轮并出分
        assert result.metric_unavailable is False
        assert result.score == 0.0
        assert result.success is True
        assert result.reason == "mock gateway verdict"

    @patch("app.services.deepeval_adapter._platform_llm_enabled", return_value=True)
    def test_flag_on_gateway_http_error_marks_unavailable(self, _flag, mock_gateway):
        """flag 开：网关返回 HTTP 错误 → metric_unavailable 而非伪装 0 分"""
        base_url, received = mock_gateway
        _MockGatewayHandler.status_code = 500

        result = evaluate(
            test_case=create_test_case(input_text="测试输入", actual_output="测试输出"),
            metric_key="toxicity",
            model_config=self._gateway_config(base_url),
        )

        assert len(received) >= 1
        assert result.metric_unavailable is True
        assert result.score is None
        assert result.success is False
        assert result.error is not None


# ============================================================
# 六、复核缺陷修复：业务必填参数透传与 None 配置归一
# ============================================================
class TestSafetyBizParamsPassthrough:
    """复核缺陷 3：misuse/non_advice/role_violation 生产调用缺业务必填参数

    deepeval 4.0.4 构造签名：MisuseMetric 必填 domain、NonAdviceMetric 必填
    advice_types（inspect 实测）；SafetyEvaluator 生产调用未传 → 构造即
    TypeError → 恒 metric_unavailable。修复：方法签名补业务参数并透传。
    """

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_misuse_domain_passed_to_construction(self, mock_create_metric):
        """evaluate_misuse 必须把 domain 透传给指标构造"""
        mock_create_metric.return_value = _make_metric_mock()

        SafetyEvaluator().evaluate_misuse(input_text="q", actual_output="a")

        assert mock_create_metric.call_args.kwargs.get("domain") == "general"

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_misuse_domain_custom_value(self, mock_create_metric):
        """调用方显式 domain 覆盖默认值"""
        mock_create_metric.return_value = _make_metric_mock()

        SafetyEvaluator().evaluate_misuse(
            input_text="q", actual_output="a", domain="code_generation"
        )

        assert mock_create_metric.call_args.kwargs.get("domain") == "code_generation"

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_non_advice_advice_types_passed_to_construction(
        self, mock_create_metric
    ):
        """evaluate_non_advice 必须把 advice_types 透传给指标构造"""
        mock_create_metric.return_value = _make_metric_mock()

        SafetyEvaluator().evaluate_non_advice(input_text="q", actual_output="a")

        advice_types = mock_create_metric.call_args.kwargs.get("advice_types")
        assert isinstance(advice_types, list) and len(advice_types) > 0

    @patch("app.services.deepeval_adapter.DeepEvalMetricRegistry.create_metric")
    def test_role_violation_role_passthrough_with_production_default(
        self, mock_create_metric
    ):
        """evaluate_role_violation 透传 role；未传时透传生产默认「通用助手」

        W16 复核第 2 轮修复：RoleViolationMetric 构造函数内部运行时必填 role
        （签名默认 None 不可见，None 即抛 ValueError），生产必须无条件透传。
        """
        mock_create_metric.return_value = _make_metric_mock()

        SafetyEvaluator().evaluate_role_violation(input_text="q", actual_output="a")
        assert mock_create_metric.call_args.kwargs.get("role") == "通用助手"

        SafetyEvaluator().evaluate_role_violation(
            input_text="q", actual_output="a", role="helpful assistant"
        )
        assert mock_create_metric.call_args.kwargs.get("role") == "helpful assistant"

    @pytest.mark.parametrize(
        "metric_key",
        ["misuse", "non_advice", "role_violation"],
    )
    def test_real_construction_succeeds_with_production_defaults(self, metric_key):
        """真实构造：生产默认参数下三个指标构造成功并走到网关调用阶段

        flag 开 + 不可达端点（127.0.0.1:9 discard）：构造走真实
        create_metric（本用例的验证目标），measure 经 wrapper 打到
        discard 端点立即连接拒绝，不外呼。

        正向区分断言（教训：排除 TypeError 特征对 ValueError 型构造失败
        假绿——RoleViolationMetric 的 role 运行时校验即此类）：修复后
        error 必须含 wrapper 的「模型调用失败」包装，即构造已成功、失败
        发生在网关调用阶段；构造缺参的 TypeError/ValueError 原文均无该包装。
        """
        method = getattr(SafetyEvaluator, f"evaluate_{metric_key}")
        with patch.object(
            deepeval_adapter, "_platform_llm_enabled", return_value=True
        ), patch.object(
            deepeval_adapter,
            "_resolve_platform_model_config",
            return_value=_make_gateway_config(),
        ):
            result = method(input_text="q", actual_output="a")

        error = result.error or ""
        # 正向断言：走到了真实构造后的网关调用阶段（连接拒绝语义）
        assert result.metric_unavailable is True
        assert "模型调用失败" in error
        # 反向断言：错误不是构造缺参（TypeError/ValueError 原文）
        assert "missing" not in error
        assert "positional argument" not in error
        assert "required positional" not in error
        assert "Role parameter is required" not in error


class TestWrapperNoneConfigTolerance:
    """复核缺陷 4：resolver 透传 DB NULL 的 request_config/auth_config 为 None

    PlatformLLMWrapper._call_custom 对 None 做 .get() 链会 AttributeError
    （dict.get 默认值不覆盖显式 None）。修复：resolver 归一 None→{}，
    wrapper 侧再防御，语义仍落 metric_unavailable（网关失败）而非崩溃。
    """

    def test_call_custom_tolerates_none_request_and_auth_config(self):
        """request_config=None + auth_config=None 不 AttributeError，落模型调用失败"""
        wrapper = PlatformLLMWrapper(
            model_config={
                "provider": "custom_http",
                "model_type": "m",
                "endpoint": "http://127.0.0.1:1/custom",
                "request_config": None,
                "auth_config": None,
            }
        )

        with pytest.raises(RuntimeError) as exc_info:
            wrapper._call_custom("prompt")

        # 必须是模型调用失败（连接层包装），不是 None.get 的 AttributeError 逃逸
        assert "模型调用失败" in str(exc_info.value)
        assert "'NoneType' object has no attribute" not in str(exc_info.value)

