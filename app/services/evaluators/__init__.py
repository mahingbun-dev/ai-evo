"""评测域抽象包（W17）：BaseEvaluator 协议 + 类型注册表。

三处收敛点的接法：

- 执行分发：``app/tasks/evaluation_tasks_async._execute_evaluation``
- 聚合分发：``app/services/result_aggregator.ResultAggregator.aggregate_for_task``
- 导出三分支：``app/services/result_aggregator.ResultExporter.export_to_excel / export_to_csv``
  （export_to_json 与类型无关，保持通用实现）

存量类型逐一迁移：每迁移一个类型，在本模块追加一行导入（导入即注册）；
未迁移前相应类型走原有 if/elif 通道，行为不变（golden 门禁兜底）。
"""
from app.services.evaluators.base import BaseEvaluator
from app.services.evaluators.context import EvaluationContext
from app.services.evaluators.registry import EvaluatorRegistry, evaluator_registry

# 导入即注册：存量类型逐一迁移（每个类型独立提交）
from app.services.evaluators import judge as _judge  # noqa: F401,E402
from app.services.evaluators import consistency as _consistency  # noqa: F401,E402
from app.services.evaluators import success_rate as _success_rate  # noqa: F401,E402
from app.services.evaluators import rag as _rag  # noqa: F401,E402
from app.services.evaluators import binary_classification as _binary  # noqa: F401,E402
from app.services.evaluators import latency as _latency  # noqa: F401,E402

__all__ = [
    "BaseEvaluator",
    "EvaluationContext",
    "EvaluatorRegistry",
    "evaluator_registry",
]
