"""W17 评测类型注册表单元测试

覆盖：注册/查取/重复注册拒绝/未声明类型键拒绝/未注册回落 None/注册顺序。
"""
import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标

from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.context import EvaluationContext
from app.services.evaluators.registry import EvaluatorRegistry


class _FakeEvaluator(BaseEvaluator):
    evaluation_type = "fake_type"

    def aggregate_task(self, task, results):
        return {"fake": True}


class _OtherEvaluator(BaseEvaluator):
    evaluation_type = "other_type"


class TestEvaluatorRegistry:
    def test_register_and_get(self):
        registry = EvaluatorRegistry()
        registry.register(_FakeEvaluator)

        adapter = registry.get("fake_type")
        assert isinstance(adapter, _FakeEvaluator)
        # 每次取回同一单例
        assert registry.get("fake_type") is adapter

    def test_get_unregistered_returns_none(self):
        registry = EvaluatorRegistry()
        registry.register(_FakeEvaluator)

        assert registry.get("unknown_type") is None
        assert registry.get(None) is None
        assert registry.get("") is None

    def test_get_default_hooks_are_none(self):
        """未覆写钩子的适配器：执行/聚合/导出钩子均回落 None（默认通道）"""
        registry = EvaluatorRegistry()
        registry.register(_OtherEvaluator)
        adapter = registry.get("other_type")

        ctx = EvaluationContext(db=None, task_id="t1")
        assert adapter.evaluate_batch(ctx, task=None, payloads=[]) is None
        assert adapter.aggregate_task(task=None, results=[]) is None
        assert adapter.export_excel(results=[]) is None
        assert adapter.export_csv(results=[]) is None
        assert adapter.aggregate_row is None

    def test_register_duplicate_rejected(self):
        registry = EvaluatorRegistry()
        registry.register(_FakeEvaluator)
        with pytest.raises(ValueError, match="重复注册"):
            registry.register(_FakeEvaluator)

    def test_register_without_type_key_rejected(self):
        class _NoKeyEvaluator(BaseEvaluator):
            pass

        registry = EvaluatorRegistry()
        with pytest.raises(ValueError, match="未声明 evaluation_type"):
            registry.register(_NoKeyEvaluator)

    def test_registered_types_order_and_contains(self):
        registry = EvaluatorRegistry()
        registry.register(_FakeEvaluator)
        registry.register(_OtherEvaluator)

        assert list(registry.registered_types()) == ["fake_type", "other_type"]
        assert "fake_type" in registry
        assert "unknown" not in registry
        assert None not in registry
        assert "" not in registry

    def test_subclass_hook_override(self):
        class _AggregatingEvaluator(BaseEvaluator):
            evaluation_type = "agg_type"

            def aggregate_task(self, task, results):
                return {"total": len(results)}

        registry = EvaluatorRegistry()
        registry.register(_AggregatingEvaluator)
        adapter = registry.get("agg_type")
        assert adapter.aggregate_task(task=None, results=[1, 2, 3]) == {"total": 3}
