"""
DeepEval Synthesizer 集成

提供基于 DeepEval Synthesizer 的数据合成能力，支持：
- 从上下文生成测试数据
- 从文档生成测试数据
- 从零开始生成测试数据
- 将生成的 Golden 转换为 AI-EVO 格式

使用平台配置的模型信息进行 LLM 调用，而非硬编码 OpenAI API。
"""

from typing import Any, Dict, List, Optional

from deepeval.dataset import Golden
from deepeval.synthesizer import Synthesizer

from app.services.dataset_converter import batch_golden_to_ai_evo
from app.services.platform_llm_wrapper import PlatformLLMWrapper, create_platform_llm


def _create_synthesizer(
    model_config: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
) -> Synthesizer:
    """
    创建 Synthesizer 实例

    优先使用 model_config 创建平台 LLM 包装器，
    否则使用 model 参数（向后兼容）。

    Args:
        model_config: 平台模型配置字典（优先）
        model: 模型名称字符串（向后兼容）

    Returns:
        Synthesizer 实例
    """
    if model_config:
        # 使用平台配置的模型
        llm_wrapper = create_platform_llm(model_config)
        return Synthesizer(model=llm_wrapper)
    elif model:
        # 向后兼容：使用模型名称字符串
        return Synthesizer(model=model)
    else:
        # 默认使用环境变量中的配置
        return Synthesizer()


def synthesize_from_contexts(
    contexts: List[List[str]],
    num_questions: int,
    model_config: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    include_expected_output: bool = True,
) -> List[Golden]:
    """
    从上下文列表生成测试数据。

    Args:
        contexts: 上下文列表，每个元素是一组上下文字符串列表。
            例如: [["上下文1", "上下文2"], ["上下文3"]]
        num_questions: 每组上下文要生成的最大问题数量。
        model_config: 平台模型配置字典（优先使用）。
        model: 可选的 LLM 模型名称（向后兼容）。
        include_expected_output: 是否在生成结果中包含期望输出，默认 True。

    Returns:
        List[Golden]: 生成的 Golden 对象列表。
    """
    synthesizer = _create_synthesizer(model_config=model_config, model=model)
    return synthesizer.generate_goldens_from_contexts(
        contexts=contexts,
        max_goldens_per_context=num_questions,
        include_expected_output=include_expected_output,
    )


def synthesize_from_docs(
    docs: List[str],
    num_questions: int,
    model_config: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    include_expected_output: bool = True,
) -> List[Golden]:
    """
    从文档生成测试数据。

    Args:
        docs: 文档文件路径列表。
            例如: ["/path/to/doc1.pdf", "/path/to/doc2.txt"]
        num_questions: 每个文档上下文要生成的最大问题数量。
        model_config: 平台模型配置字典（优先使用）。
        model: 可选的 LLM 模型名称（向后兼容）。
        include_expected_output: 是否在生成结果中包含期望输出，默认 True。

    Returns:
        List[Golden]: 生成的 Golden 对象列表。
    """
    synthesizer = _create_synthesizer(model_config=model_config, model=model)
    return synthesizer.generate_goldens_from_docs(
        document_paths=docs,
        max_goldens_per_context=num_questions,
        include_expected_output=include_expected_output,
    )


def synthesize_from_scratch(
    topic: str,
    num_questions: int,
    model_config: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
) -> List[Golden]:
    """
    从零开始生成测试数据（无需上下文或文档）。

    注意: 当前 DeepEval Synthesizer.generate_goldens_from_scratch 不直接
    支持 topic 参数，topic 仅用于语义标识。实际生成数量由 num_questions 控制。

    Args:
        topic: 生成数据的主题标识（用于记录/追踪）。
        num_questions: 要生成的问题数量。
        model_config: 平台模型配置字典（优先使用）。
        model: 可选的 LLM 模型名称（向后兼容）。

    Returns:
        List[Golden]: 生成的 Golden 对象列表。
    """
    synthesizer = _create_synthesizer(model_config=model_config, model=model)
    return synthesizer.generate_goldens_from_scratch(
        num_goldens=num_questions,
    )


def convert_to_ai_evo_format(goldens: List[Golden]) -> List[Dict[str, Any]]:
    """
    将 Golden 列表转换为 AI-EVO 数据格式。

    使用 DatasetConverter.batch_golden_to_ai_evo 进行批量转换。

    Args:
        goldens: DeepEval Golden 对象列表。

    Returns:
        List[Dict[str, Any]]: AI-EVO 格式的数据项列表。
    """
    return batch_golden_to_ai_evo(goldens)
