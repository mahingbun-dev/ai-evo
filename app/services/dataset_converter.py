"""
数据集格式转换器

实现 AI-EVO 数据项与 DeepEval Golden 格式的双向转换。

字段映射关系:
  AI-EVO        ->  DeepEval Golden
  user_input    ->  input
  ref_answer    ->  expected_output
  model_answer  ->  actual_output
  context       ->  context
  retrieval_context -> retrieval_context

DeepEval Golden  ->  AI-EVO
  input             ->  user_input
  expected_output   ->  ref_answer
  actual_output     ->  model_answer
  context           ->  context
  retrieval_context ->  retrieval_context
  additional_metadata -> 展开为顶级字段
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from deepeval.dataset import Golden


# AI-EVO 核心映射字段（不应出现在 additional_metadata / custom_fields 中）
_CORE_FIELD_MAP_TO_GOLDEN = {
    "user_input": "input",
    "ref_answer": "expected_output",
    "model_answer": "actual_output",
    "context": "context",
    "retrieval_context": "retrieval_context",
}


@dataclass
class ValidationResult:
    """数据验证结果"""
    is_valid: bool
    errors: List[str] = field(default_factory=list)


def ai_evo_to_golden(
    item: Dict[str, Any],
    preserve_custom_fields: bool = False,
) -> Golden:
    """
    将 AI-EVO 数据项转换为 DeepEval Golden。

    Args:
        item: AI-EVO 格式的数据字典，支持以下字段：
            - user_input (str): 用户输入/问题
            - ref_answer (str, 可选): 参考答案
            - model_answer (str, 可选): 模型实际输出
            - context (list, 可选): 上下文列表
            - retrieval_context (list, 可选): 检索上下文列表
            - 其他自定义字段（preserve_custom_fields=True 时保留）
        preserve_custom_fields: 是否将非核心字段保留到 additional_metadata

    Returns:
        Golden: DeepEval Golden 对象

    Raises:
        KeyError: 当缺少 user_input 时
        TypeError: 当 item 不是字典时
    """
    if not isinstance(item, dict):
        raise TypeError("item 必须是字典类型")

    user_input = item.get("user_input", "")
    ref_answer = item.get("ref_answer")
    model_answer = item.get("model_answer")
    context = item.get("context")
    retrieval_context = item.get("retrieval_context")

    golden_kwargs: Dict[str, Any] = {
        "input": user_input,
    }

    if ref_answer is not None:
        golden_kwargs["expected_output"] = ref_answer

    if model_answer is not None:
        golden_kwargs["actual_output"] = model_answer

    if context is not None:
        golden_kwargs["context"] = context

    if retrieval_context is not None:
        golden_kwargs["retrieval_context"] = retrieval_context

    # 保留自定义字段
    if preserve_custom_fields:
        custom_fields = {
            k: v
            for k, v in item.items()
            if k not in _CORE_FIELD_MAP_TO_GOLDEN
        }
        if custom_fields:
            golden_kwargs["additional_metadata"] = custom_fields

    return Golden(**golden_kwargs)


def golden_to_ai_evo(golden: Golden) -> Dict[str, Any]:
    """
    将 DeepEval Golden 转换为 AI-EVO 数据项。

    Args:
        golden: DeepEval Golden 对象

    Returns:
        Dict: AI-EVO 格式的数据字典
    """
    item: Dict[str, Any] = {
        "user_input": golden.input,
    }

    if golden.expected_output is not None:
        item["ref_answer"] = golden.expected_output

    if golden.actual_output is not None:
        item["model_answer"] = golden.actual_output

    if golden.context is not None:
        item["context"] = golden.context

    if golden.retrieval_context is not None:
        item["retrieval_context"] = golden.retrieval_context

    # 展开 additional_metadata 为顶级字段
    if golden.additional_metadata:
        item.update(golden.additional_metadata)

    return item


def batch_ai_evo_to_golden(
    items: List[Dict[str, Any]],
    preserve_custom_fields: bool = False,
) -> List[Golden]:
    """
    批量将 AI-EVO 数据项转换为 DeepEval Golden。

    Args:
        items: AI-EVO 数据项列表
        preserve_custom_fields: 是否保留自定义字段

    Returns:
        List[Golden]: Golden 对象列表
    """
    return [
        ai_evo_to_golden(item, preserve_custom_fields=preserve_custom_fields)
        for item in items
    ]


def batch_golden_to_ai_evo(goldens: List[Golden]) -> List[Dict[str, Any]]:
    """
    批量将 DeepEval Golden 转换为 AI-EVO 数据项。

    Args:
        goldens: Golden 对象列表

    Returns:
        List[Dict]: AI-EVO 数据项列表
    """
    return [golden_to_ai_evo(golden) for golden in goldens]


def validate_golden(golden: Golden) -> ValidationResult:
    """
    验证 Golden 数据的有效性。

    规则:
    - input 不能为空字符串
    - expected_output 可以为 None，但不应为空字符串

    Args:
        golden: DeepEval Golden 对象

    Returns:
        ValidationResult: 验证结果
    """
    errors: List[str] = []

    if not golden.input or not golden.input.strip():
        errors.append("input 不能为空")

    if golden.expected_output is not None and golden.expected_output == "":
        errors.append("expected_output 不应为空字符串（应为 None 或有内容）")

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)


def validate_ai_evo_item(item: Any) -> ValidationResult:
    """
    验证 AI-EVO 数据项的有效性。

    规则:
    - item 必须是字典类型
    - user_input 不能为空
    - user_input 必须是非空字符串

    Args:
        item: 待验证的数据项

    Returns:
        ValidationResult: 验证结果
    """
    errors: List[str] = []

    if not isinstance(item, dict):
        errors.append(f"数据项必须是字典类型，实际类型: {type(item).__name__}")
        return ValidationResult(is_valid=False, errors=errors)

    user_input = item.get("user_input")
    if user_input is None:
        errors.append("缺少必填字段: user_input")
    elif not isinstance(user_input, str):
        errors.append(f"user_input 必须是字符串类型，实际类型: {type(user_input).__name__}")
    elif not user_input.strip():
        errors.append("user_input 不能为空字符串")

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)
