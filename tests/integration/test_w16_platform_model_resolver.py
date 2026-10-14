"""W16 集成测试：deepeval 平台网关模型配置解析（_resolve_platform_model_config）

覆盖真实 DB 路径：
- 自动选取分支：从启用中的 evaluation 角色模型里取最新的；
- 指定 ID 分支：DEEPEVAL_PLATFORM_MODEL_ID 精确命中；
- 与 flag 的联动：flag 关时 evaluate 不触发解析/注入（行为与现状一致）。

数据纪律：测试用 w16it_ 前缀自建自清，只删自建数据，不做 DDL/DROP/TRUNCATE。
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration  # W8 分类门禁：补打标

from app.services import deepeval_adapter
from app.services.deepeval_adapter import (
    _platform_llm_enabled,
    _resolve_platform_model_config,
    create_test_case,
    evaluate,
)

_W16_MODEL_PREFIX = "w16it_model_"


@pytest.fixture()
def w16_model_records(db_session):
    """在当前库插入 evaluation/judge 各一条模型配置（自建自清）"""
    from app.models.model_config_models import ModelConfig

    eval_id = f"{_W16_MODEL_PREFIX}eval"
    judge_id = f"{_W16_MODEL_PREFIX}judge"
    db_session.add(
        ModelConfig(
            id=eval_id,
            name="W16IT 评测网关模型",
            provider="openai",
            model_type="w16-gateway-model",
            model_role="evaluation",
            endpoint="http://127.0.0.1:9/v1/chat/completions",
            api_key="w16it-key",
            auth_type="bearer",
            is_active=True,
        )
    )
    db_session.add(
        ModelConfig(
            id=judge_id,
            name="W16IT 裁判模型",
            provider="openai",
            model_type="w16-judge-model",
            model_role="judge",
            endpoint="http://127.0.0.1:9/v1/chat/completions",
            is_active=True,
        )
    )
    db_session.commit()

    yield {"eval": eval_id, "judge": judge_id}

    for model_id in (eval_id, judge_id):
        record = db_session.query(ModelConfig).filter(ModelConfig.id == model_id).first()
        if record is not None:
            db_session.delete(record)
    db_session.commit()


class TestResolvePlatformModelConfig:
    """_resolve_platform_model_config 的真实 DB 解析"""

    def test_auto_selects_latest_active_evaluation_model(self, w16_model_records):
        """自动分支：选中启用中的 evaluation 角色模型（不选 judge 角色）"""
        resolved = _resolve_platform_model_config()

        assert resolved is not None
        assert resolved["model_id"] == w16_model_records["eval"]
        assert resolved["model_type"] == "w16-gateway-model"
        assert resolved["provider"] == "openai"
        assert resolved["endpoint"] == "http://127.0.0.1:9/v1/chat/completions"
        assert resolved["api_key"] == "w16it-key"
        # 字段集与 PlatformLLMWrapper 约定的 model_config 结构一致
        assert set(resolved.keys()) == {
            "provider",
            "model_type",
            "api_key",
            "endpoint",
            "auth_type",
            "auth_config",
            "request_config",
            "model_id",
        }

    def test_explicit_model_id_wins(self, w16_model_records, monkeypatch):
        """指定 ID 分支：DEEPEVAL_PLATFORM_MODEL_ID 精确命中（即使是 judge 角色）"""
        monkeypatch.setenv("DEEPEVAL_PLATFORM_MODEL_ID", w16_model_records["judge"])
        deepeval_adapter.get_settings.cache_clear()
        try:
            resolved = _resolve_platform_model_config()
        finally:
            monkeypatch.undo()
            deepeval_adapter.get_settings.cache_clear()

        assert resolved is not None
        assert resolved["model_id"] == w16_model_records["judge"]
        assert resolved["model_type"] == "w16-judge-model"

    def test_missing_model_id_returns_none(self, monkeypatch):
        """指定 ID 不存在时返回 None（不抛异常，由调用方落 metric_unavailable）"""
        monkeypatch.setenv("DEEPEVAL_PLATFORM_MODEL_ID", f"{_W16_MODEL_PREFIX}no_such")
        deepeval_adapter.get_settings.cache_clear()
        try:
            resolved = _resolve_platform_model_config()
        finally:
            monkeypatch.undo()
            deepeval_adapter.get_settings.cache_clear()

        assert resolved is None


class TestFlagIntegrationWithDb:
    """flag 与 DB 解析联动"""

    def test_flag_on_injects_model_resolved_from_db(self, w16_model_records):
        """flag 开：evaluate 经 DB 解析把模型注入 create_metric"""
        with patch.object(
            deepeval_adapter, "_platform_llm_enabled", return_value=True
        ), patch.object(
            deepeval_adapter.DeepEvalMetricRegistry, "create_metric"
        ) as mock_create:
            metric = MagicMock()
            metric.score = 0.5
            metric.success = True
            metric.reason = None
            metric.error = None
            metric.verdicts = None
            metric.score_breakdown = None
            mock_create.return_value = metric

            result = evaluate(
                test_case=create_test_case(input_text="q", actual_output="a"),
                metric_key="bias",
            )

        assert result.score == 0.5
        injected_model = mock_create.call_args.kwargs["model"]
        assert injected_model.model_config["model_id"] == w16_model_records["eval"]

    def test_flag_off_does_not_touch_model_tables(self, w16_model_records):
        """flag 关：行为与现状一致——不注入 model（默认 get_settings 路径）"""
        assert _platform_llm_enabled() is False

        with patch.object(
            deepeval_adapter.DeepEvalMetricRegistry, "create_metric"
        ) as mock_create:
            metric = MagicMock()
            metric.score = 0.5
            metric.success = True
            metric.reason = None
            metric.error = None
            metric.verdicts = None
            metric.score_breakdown = None
            mock_create.return_value = metric

            result = evaluate(
                test_case=create_test_case(input_text="q", actual_output="a"),
                metric_key="bias",
            )

        assert result.score == 0.5
        assert "model" not in mock_create.call_args.kwargs


class TestResolverNormalizesNullJsonColumns:
    """复核缺陷 4：DB NULL 的 JSON 列（auth_config/request_config）归一

    resolver 透传 None 给 PlatformLLMWrapper 会让 _call_custom 的
    .get() 链 AttributeError（dict.get 默认值不覆盖显式 None）。
    修复后 resolver 必须归一为空 dict。
    """

    def test_resolver_normalizes_null_json_columns(self, db_session):
        from app.models.model_config_models import ModelConfig
        from app.services import deepeval_adapter

        model_id = "w16it_model_nullcfg"
        db_session.add(
            ModelConfig(
                id=model_id,
                name="W16IT NULL 配置模型",
                provider="custom_http",
                model_type="w16-nullcfg-model",
                model_role="evaluation",
                endpoint="http://127.0.0.1:9/custom",
                auth_config=None,
                request_config=None,
                is_active=True,
            )
        )
        db_session.commit()
        try:
            from unittest.mock import patch

            with patch(
                "app.services.deepeval_adapter.get_settings"
            ) as mock_settings:
                mock_settings.return_value.DEEPEVAL_PLATFORM_MODEL_ID = model_id
                resolved = deepeval_adapter._resolve_platform_model_config()
        finally:
            record = (
                db_session.query(ModelConfig).filter(ModelConfig.id == model_id).first()
            )
            if record is not None:
                db_session.delete(record)
            db_session.commit()

        assert resolved is not None
        assert resolved["request_config"] == {}
        assert resolved["auth_config"] == {}
        assert resolved["model_id"] == model_id
