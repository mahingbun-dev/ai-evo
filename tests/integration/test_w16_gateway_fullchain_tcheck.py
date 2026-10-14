"""W16 独立复核集成测试（测试角色）：DB 解析 → PlatformLLMWrapper 真实消费 → 连接拒绝语义

与开发交付的 tests/integration/test_w16_platform_model_resolver.py 互补：
开发侧 flag 开联动用例对 create_metric 打了 mock，未验证「resolver 返回的字段集
能被 PlatformLLMWrapper 真实消费、最终经真实 HTTP 落到正确语义」。本文件补：
- 从真实 DB 解析模型配置（含 nullable JSON 列为 NULL 的记录）→ 构造 wrapper →
  经 evaluator 入口（SafetyEvaluator）真实打到不可达端点（127.0.0.1:9 连接拒绝）
  → metric_unavailable / score=None，全程不 mock create_metric / wrapper；
- 证明 resolver 字段集与 wrapper 消费兼容（openai 路径不因 request_config /
  auth_config 为 NULL 而崩溃）。

数据纪律：w16t_ 前缀自建自清，只删自建数据，不做 DDL/DROP/TRUNCATE；
不触碰共享库既有数据（fixture 均为 conftest 提供的临时库）。
"""
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration  # W8 分类门禁：补打标

from app.services import deepeval_adapter
from app.services.safety_evaluator import SafetyEvaluator

_W16T_MODEL_ID = "w16t_fullchain_eval_model"


@pytest.fixture()
def w16t_gateway_model(db_session):
    """插入启用中的 evaluation 角色模型（endpoint 指向不可达 discard 端口；JSON 列留 NULL）"""
    from app.models.model_config_models import ModelConfig

    record = ModelConfig(
        id=_W16T_MODEL_ID,
        name="W16T 全链路网关模型",
        provider="openai",
        model_type="w16t-gateway-model",
        model_role="evaluation",
        endpoint="http://127.0.0.1:9/v1/chat/completions",
        api_key="w16t-key",
        auth_type="bearer",
        is_active=True,
        # request_config / auth_config 不传 → DB NULL，验证 resolver 透传 None
        # 字段值时 wrapper 的 openai 路径仍可正常消费
    )
    db_session.add(record)
    db_session.commit()

    yield record

    existing = (
        db_session.query(ModelConfig).filter(ModelConfig.id == _W16T_MODEL_ID).first()
    )
    if existing is not None:
        db_session.delete(existing)
        db_session.commit()


class TestDbResolvedConfigFullChain:
    """flag 开 + 真实 DB 解析 + 真实指标 + 真实 HTTP（无人为 raise）"""

    def test_resolver_output_consumed_by_wrapper_via_evaluator(self, w16t_gateway_model):
        """resolver→wrapper→连接拒绝全链路：经 SafetyEvaluator.evaluate_bias 落
        metric_unavailable 而非伪装 0 分，且请求配置来自 DB 记录"""
        # 先确认 resolver 确实解析出 DB 记录（NULL JSON 列已归一为空 dict，
        # W16 复核缺陷 4 修复：原样透传 None 会让 wrapper._call_custom 的
        # .get() 链 AttributeError）
        resolved = deepeval_adapter._resolve_platform_model_config()
        assert resolved is not None
        assert resolved["model_id"] == _W16T_MODEL_ID
        assert resolved["request_config"] == {}  # DB NULL 归一，不透传 None
        assert resolved["auth_config"] == {}

        wrapper_probe = deepeval_adapter.PlatformLLMWrapper(model_config=resolved)
        assert wrapper_probe.get_model_name() == "w16t-gateway-model"
        assert wrapper_probe.load_model() is wrapper_probe  # 配置校验通过

        with patch.object(
            deepeval_adapter, "_platform_llm_enabled", return_value=True
        ):
            result = SafetyEvaluator.evaluate_bias(
                input_text="测试输入",
                actual_output="测试输出",
            )

        assert result.metric_key == "bias"
        assert result.metric_unavailable is True
        assert result.score is None
        assert result.score != 0.0  # 明确不是伪装 0 分
        assert result.success is False
        assert result.error is not None
        assert "模型调用失败" in result.error

    def test_flag_off_does_not_resolve_db(self, w16t_gateway_model):
        """flag 关（默认）：evaluate 不经 DB 解析注入，evaluator 行为与现状一致"""
        assert deepeval_adapter._platform_llm_enabled() is False

        with patch.object(
            deepeval_adapter.DeepEvalMetricRegistry, "create_metric"
        ) as mock_create:
            from unittest.mock import MagicMock

            metric = MagicMock()
            metric.score = 0.6
            metric.success = True
            metric.reason = None
            metric.error = None
            metric.verdicts = None
            metric.score_breakdown = None
            mock_create.return_value = metric

            result = SafetyEvaluator.evaluate_bias(
                input_text="q", actual_output="a"
            )

        assert result.score == 0.6
        assert "model" not in mock_create.call_args.kwargs
