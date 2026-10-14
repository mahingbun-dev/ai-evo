"""评测类型注册表（W17）

以 ``task.evaluation_type`` 为键的适配器注册表：

- 新类型：实现 :class:`~app.services.evaluators.base.BaseEvaluator` 子类并
  ``@evaluator_registry.register`` 注册，执行分发/聚合/导出三处收敛点即生效；
- 存量类型：逐一迁移（每类型独立提交，golden 逐字节门禁保证零漂移）；
- 未注册类型（含 judge 默认路径、agent 及未来未知类型）：``get`` 返回 ``None``，
  调用方回落默认通道 —— 注册表缺失永不变更既有行为。
"""
from typing import Dict, Iterator, Optional, Type

from app.services.evaluators.base import BaseEvaluator


class EvaluatorRegistry:
    """评测类型适配器注册表"""

    def __init__(self) -> None:
        self._adapters: Dict[str, BaseEvaluator] = {}

    def register(self, evaluator_cls: Type[BaseEvaluator]) -> Type[BaseEvaluator]:
        """类装饰器：按 ``evaluation_type`` 注册适配器（同名重复注册视为配置错误）"""
        key = evaluator_cls.evaluation_type
        if not key:
            raise ValueError(
                f"{evaluator_cls.__name__} 未声明 evaluation_type，无法注册"
            )
        if key in self._adapters:
            raise ValueError(
                f"评测类型 {key!r} 重复注册: "
                f"{type(self._adapters[key]).__name__} -> {evaluator_cls.__name__}"
            )
        self._adapters[key] = evaluator_cls()
        return evaluator_cls

    def get(self, evaluation_type: Optional[str]) -> Optional[BaseEvaluator]:
        """按类型取适配器；未注册（含 None/空串）返回 None，调用方回落默认通道"""
        if not evaluation_type:
            return None
        return self._adapters.get(evaluation_type)

    def registered_types(self) -> Iterator[str]:
        """已注册类型键（按注册顺序）"""
        return iter(self._adapters.keys())

    def __contains__(self, evaluation_type: Optional[str]) -> bool:
        return bool(evaluation_type) and evaluation_type in self._adapters


#: 模块级单例：执行分发 / 聚合 / 导出三处收敛点共用
evaluator_registry = EvaluatorRegistry()
