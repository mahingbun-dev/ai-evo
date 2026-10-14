"""评测域抽象：BaseEvaluator 协议（W17）

定义评测类型的统一协议（整改计划 §4 W17）：

- ``evaluate_batch``   执行钩子：一批 payload 的评测执行（原执行分发 if/else 的分支体）
- ``aggregate_row``    行级聚合钩子（预留扩展点：现无行级聚合分支，缺省不启用）
- ``aggregate_task``   任务级聚合钩子（原 aggregate_for_task 分发 if/else 的分支体）
- ``export_excel`` / ``export_csv``
                      导出钩子（原 ResultExporter excel/csv 分支体；返回 None 表示
                      该类型无专属导出，回落默认通道）

约束：
- 协议只声明钩子与类型键，不承载业务逻辑；
- 各评测类型适配器放在本包同级模块（judge.py / consistency.py / ...），
  通过 ``@evaluator_registry.register`` 注册；
- 适配器对既有实现做委托，聚合/导出的行为必须与迁移前逐字节一致
  （tests/golden 为零漂移门禁）。
"""
from typing import Any, Dict, List, Optional

from app.services.evaluators.context import EvaluationContext


class BaseEvaluator:
    """评测类型协议基类

    子类必须声明类属性 ``evaluation_type``（与 task.evaluation_type 取值一致），
    并按需覆写钩子；未覆写的钩子保持缺省行为：

    - ``evaluate_batch`` 缺省返回 ``None``：表示该类型无专属执行路径，
      调用方回落默认执行通道（EvaluationExecutor 裁判批量执行）；
    - ``aggregate_task`` 缺省返回 ``None``：调用方回落默认任务级聚合
      （``ResultAggregator(task).aggregate``）；
    - ``export_excel`` / ``export_csv`` 缺省返回 ``None``：调用方回落
      默认导出通道（batch/通用格式）；
    - ``aggregate_row`` 为预留扩展点，缺省 ``None``（不启用）。
    """

    #: 评测类型键（task.evaluation_type 取值）；子类必须覆写
    evaluation_type: str = ""

    #: 执行钩子：返回 None 表示走默认执行通道
    def evaluate_batch(
        self,
        ctx: EvaluationContext,
        task,
        payloads: List[Dict[str, Any]],
    ) -> Optional[List[Dict[str, Any]]]:
        return None

    #: 行级聚合钩子（预留扩展点，缺省不启用）
    aggregate_row: Optional[Any] = None

    #: 任务级聚合钩子：返回 None 表示走默认任务级聚合
    def aggregate_task(self, task, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return None

    #: 导出钩子：返回 None 表示回落默认导出通道
    def export_excel(self, results: List[Dict[str, Any]]) -> Optional[bytes]:
        return None

    def export_csv(self, results: List[Dict[str, Any]]) -> Optional[str]:
        return None
