"""结果观察脚本（evaluation/result 扩展点）空实现示例

设计：docs/design/pre-judge-scripts.md §九.3——execute_batch 聚合完成后，
遍历任务配置的 result 观察脚本，对每条评测结果只读观察并产出附加信息
（附加到 result.observations，绝不修改 result 本身）。

本期（二期第一步）只预留扩展点接口与空实现示例：
- 注册表见 app.services.pre_judge_runner.RESULT_OBSERVER_REGISTRY
- 本示例 example_noop 只回读统计摘要，不产出业务观察，作为编写观察脚本的模板
"""
from typing import Any, Dict


def run(stdin: Dict[str, Any]) -> Dict[str, Any]:
    """标准入口：(标准输入 dict) -> 标准输出 dict（与前处理脚本同一契约）

    stdin 结构（观察脚本视角，result 为只读快照）：
        {
          "task_id": "...",
          "result": { ...单条评测结果快照（只读，禁止修改）... },
          "result_index": 0,
          "params": {...}
        }
    """
    result = stdin.get("result") or {}
    summary = (
        f"观察示例：第 {stdin.get('result_index', 0)} 条结果，"
        f"success={result.get('success')}，average_score={result.get('average_score')}"
    )
    return {
        "status": "ok",
        "output": {},
        "files": {},
        "summary": summary,
        "finish_reason": "completed",
        "error": None,
    }
