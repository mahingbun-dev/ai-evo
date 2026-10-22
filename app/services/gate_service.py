"""
门禁服务（M1 QE1-2，评测即门禁）

`evaluate_gate(gate_config, aggregates) -> gate_result`：聚合完成后对阈值
矩阵做判定，结果落 task_execution_records.gate_result，CLI（QE1-5）按
verdict 出退出码（0=passed / 1=failed / 2=error）。

gate_config 结构（plans/2026-10-22-Agent质量工程平台开发方案.md §M1）::

    {
      "enabled": true,
      "mode": "all",            # all=全部阈值须过 / any=任一过
      "thresholds": [
        {"metric": "total_score", "op": ">=", "value": 8.0},
        {"metric": "pass_rate", "op": ">=", "value": 0.95},
        {"metric": "dimension:准确性", "op": ">=", "value": 7.0}
      ]
    }

metric 解析（M1 支持集；verification_* 随 M2 扩展）：
- ``total_score``：任务均分（aggregates.average_score，0..max_score）；
- ``pass_rate``：行通过率（aggregates.success_rate，0..1；别名 success_rate）；
- ``dimension:<维度名>``：维度均分（aggregates.dimension_scores.<名>.average）；
- 其余（含 M2 的 verification_pass_rate 在 M2 接线前）：**metric 不可用**
  → verdict=error（配置引用了本轮拿不到的指标，与质量不达标区分——CI 按
  error 退出码 2 处理，不被误读成测试失败）。

失败/停止收尾：`build_error_gate_result(gate_config, reason)` 统一产
error verdict，由 TaskExecutionRecordService 在 fail_record/stop_record
里对启用门禁的任务自动落库（断点续跑/失败收尾路径不漏）。
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 阈值比较算子（白名单；未知算子按配置错误处理 → error verdict）
_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}

VERDICT_PASSED = "passed"
VERDICT_FAILED = "failed"
VERDICT_ERROR = "error"


def gate_enabled(gate_config: Any) -> bool:
    """门禁是否启用：非空 dict 且 enabled 真值。空 dict / None = 没有配置
    = 未启用（存量任务）；给了配置但没写 enabled 的手写口径按启用算（宁判勿漏）"""
    if not isinstance(gate_config, dict) or not gate_config:
        return False
    return bool(gate_config.get("enabled", True))


def _extract_metric(metric: str, aggregates: Dict[str, Any]) -> Optional[float]:
    """从聚合结果提取指标值；不可提取返回 None（metric 不存在/非数值）"""
    if not isinstance(aggregates, dict):
        return None
    if metric == "total_score":
        value = aggregates.get("average_score")
    elif metric in ("pass_rate", "success_rate"):
        value = aggregates.get("success_rate")
    elif metric.startswith("dimension:"):
        dim_name = metric[len("dimension:"):]
        dims = aggregates.get("dimension_scores") or {}
        dim = dims.get(dim_name)
        value = dim.get("average") if isinstance(dim, dict) else None
    else:
        # M2 verification_* 等尚未接线的指标：聚合里没有即不可用
        value = aggregates.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def evaluate_gate(gate_config: Any, aggregates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按 gate_config 对聚合结果做门禁判定

    Returns:
        gate_result dict（{verdict, checks, evaluated_at}）；未启用门禁
        返回 None（调用方不落库，列保持 NULL）。
    """
    if not gate_enabled(gate_config):
        return None

    mode = gate_config.get("mode") or "all"
    if mode not in ("all", "any"):
        mode = "all"  # 非法 mode 兜底为 all（从严：须全过）
    thresholds = gate_config.get("thresholds")
    if not isinstance(thresholds, list) or not thresholds:
        # 启用了门禁但没配阈值：配置不完整 → error（不是 passed）
        return _result(VERDICT_ERROR, [], note="门禁已启用但未配置阈值")

    checks: List[Dict[str, Any]] = []
    has_error = False
    for threshold in thresholds:
        if not isinstance(threshold, dict):
            has_error = True
            checks.append({"metric": str(threshold), "op": None, "value": None,
                           "actual": None, "passed": False,
                           "note": "阈值项必须是 {metric, op, value} 对象"})
            continue
        metric = threshold.get("metric")
        op = threshold.get("op")
        value = threshold.get("value")
        check = {"metric": metric, "op": op, "value": value,
                 "actual": None, "passed": False}
        if not isinstance(metric, str) or op not in _OPS:
            has_error = True
            check["note"] = f"未知算子或指标名非法：{op!r}"
        else:
            actual = _extract_metric(metric, aggregates)
            if actual is None:
                has_error = True
                check["note"] = "metric 在本次聚合中不可用（未接线或无数据）"
            else:
                check["actual"] = round(actual, 6)
                check["passed"] = bool(_OPS[op](actual, value))
        checks.append(check)

    if has_error:
        verdict = VERDICT_ERROR
    else:
        passed_count = sum(1 for c in checks if c["passed"])
        if mode == "any":
            verdict = VERDICT_PASSED if passed_count >= 1 else VERDICT_FAILED
        else:  # all
            verdict = VERDICT_PASSED if passed_count == len(checks) else VERDICT_FAILED

    result = _result(verdict, checks)
    result["mode"] = mode
    return result


def build_error_gate_result(gate_config: Any, reason: str) -> Optional[Dict[str, Any]]:
    """失败/停止收尾的 error verdict（启用门禁才有；未启用返回 None）

    收尾路径（fail_record/stop_record）调用：执行没跑完聚合就不存在，
    门禁无从判定 → error（CLI 退出码 2），与「质量不达标」（failed，
    退出码 1）严格区分。
    """
    if not gate_enabled(gate_config):
        return None
    result = _result(VERDICT_ERROR, [])
    result["reason"] = reason or "评测未完成（失败/停止），门禁无法判定"
    return result


def _result(verdict: str, checks: List[Dict[str, Any]], **extra) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "verdict": verdict,
        "checks": checks,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    result.update(extra)
    return result
