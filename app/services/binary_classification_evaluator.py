"""
二分分类评测引擎
用于评估二分类任务中预测标签与期望标签的一致性
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)


@dataclass
class BinaryClassificationResult:
    """单条二分分类评测结果"""
    index: int
    expected_label: bool
    predicted_label: bool
    is_correct: bool
    duration: float = 0.0
    success: bool = True
    error: str = ""
    input_data: Dict[str, Any] = field(default_factory=dict)


class BinaryClassificationEvaluator:
    """二分分类评测器

    简化实现：解析期望标签和预测标签，转换为布尔值后比较
    """

    def __init__(
        self,
        expected_label_config: Optional[Dict[str, Any]] = None,
        predicted_label_config: Optional[Dict[str, Any]] = None,
        positive_value: str = "true",
        label_type: str = "boolean",
        progress_callback: Optional[Callable[[int, int], None]] = None
    ):
        self.expected_label_config = expected_label_config or {
            "source": "dataset_field",
            "source_config": {"field": "expected_label"}
        }
        self.predicted_label_config = predicted_label_config or {
            "source": "model_output",
            "source_config": {"field": "prediction"}
        }
        self.positive_value = positive_value
        self.label_type = label_type
        self.progress_callback = progress_callback

    async def evaluate_batch(
        self,
        dataset: List[Dict[str, Any]],
        model_outputs: List[Dict[str, Any]],
        chain_results_list: Optional[List[List[Dict[str, Any]]]] = None
    ) -> List[BinaryClassificationResult]:
        results = []
        total = len(dataset)

        for idx, item in enumerate(dataset):
            model_output = model_outputs[idx] if idx < len(model_outputs) else {}
            chain_results = chain_results_list[idx] if chain_results_list and idx < len(chain_results_list) else []
            result = await self._evaluate_single(item, model_output, idx, chain_results)
            results.append(result)

            if self.progress_callback:
                self.progress_callback(idx + 1, total)

        return results

    async def _evaluate_single(
        self,
        item: Dict[str, Any],
        model_output: Dict[str, Any],
        index: int,
        chain_results: Optional[List[Dict[str, Any]]] = None
    ) -> BinaryClassificationResult:
        chain_results = chain_results or []
        try:
            expected_raw = self._resolve_field(item, model_output, chain_results, self.expected_label_config)
            predicted_raw = self._resolve_field(item, model_output, chain_results, self.predicted_label_config)

            logger.info(f"[二分分类评测] idx={index}, predicted_raw={predicted_raw!r}, type={type(predicted_raw).__name__}, "
                        f"predicted_label_config={self.predicted_label_config}, "
                        f"chain_results_count={len(chain_results)}")

            expected_label = self._to_label(expected_raw, self.positive_value, self.label_type)
            predicted_label = self._to_label(predicted_raw, self.positive_value, self.label_type)
            is_correct = expected_label == predicted_label

            logger.info(f"[二分分类评测] idx={index}, expected_raw={expected_raw!r} -> {expected_label}, "
                        f"predicted_raw={predicted_raw!r} -> {predicted_label}, is_correct={is_correct}")

            # 收集输入变量（排除内部字段）
            input_vars = {k: v for k, v in item.items()
                          if k not in ("_dataset_item", "idx") and not k.startswith("_")}

            return BinaryClassificationResult(
                index=index,
                expected_label=expected_label,
                predicted_label=predicted_label,
                is_correct=is_correct,
                duration=model_output.get("duration", 0.0),
                success=True,
                error="",
                input_data=input_vars,
            )
        except Exception as e:
            return BinaryClassificationResult(
                index=index,
                expected_label=False,
                predicted_label=False,
                is_correct=False,
                success=False,
                error=str(e),
                input_data={k: v for k, v in (item or {}).items()
                            if k not in ("_dataset_item", "idx") and not k.startswith("_")},
            )

    def _resolve_field(
        self,
        item: Dict[str, Any],
        model_output: Dict[str, Any],
        chain_results: List[Dict[str, Any]],
        source_config: Dict[str, Any]
    ) -> Any:
        source = source_config.get("source", "")
        source_cfg = source_config.get("source_config", {})

        if source == "dataset_field":
            field_name = source_cfg.get("field")
            # payload 结构：
            # - payload["_dataset_item"] = _load_dataset 返回的项，包含 data JSON 对象
            # - payload["data"] 不存在（variable_injector 没有设置 data 字段）
            # 数据实际存储在 _dataset_item["data"] 中
            dataset_item = item.get("_dataset_item", {})
            if isinstance(dataset_item, dict):
                # 优先从 dataset_item.data 取（V2 数据存储在 data JSON 中）
                data = dataset_item.get("data", {})
                if field_name in data:
                    return data[field_name]
                # 兼容：直接从 dataset_item 顶层取（如果字段被 flatten）
                if field_name in dataset_item:
                    return dataset_item[field_name]
            # 最后尝试从 item 本身获取（兼容其他可能的结构）
            data = item.get("data", {})
            if field_name in data:
                return data[field_name]
            return item.get(field_name, "")

        elif source == "model_output":
            field_name = source_cfg.get("field", "content")
            return model_output.get(field_name, "")

        elif source == "chain_result":
            chain_index = source_cfg.get("chain_index")
            var_names = source_cfg.get("var_names")
            if var_names and isinstance(var_names, list) and len(var_names) > 0:
                chain_key = var_names[0]
            else:
                chain_key = source_cfg.get("chain_key", "content")
            if chain_index is None:
                return ""
            # 配置中 chain_index 是 1-indexed，转为 0-indexed
            actual_index = chain_index - 1
            if actual_index < 0 or actual_index >= len(chain_results):
                return ""
            chain_result = chain_results[actual_index]
            if "variables" in chain_result:
                return chain_result["variables"].get(chain_key, "")
            if "all_results" in chain_result:
                return chain_result["all_results"].get(chain_key, "")
            return chain_result.get(chain_key, "")

        elif source in ("manual_input", "manual"):
            return source_cfg.get("manual_value", source_cfg.get("value", ""))

        return ""

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, dict):
            # 字典类型：按优先级检查常见结果字段名
            for key in ("result", "answer", "label", "prediction", "output", "value", "data"):
                if key in value:
                    return self._to_bool(value[key])
            # 没有常见字段，查找第一个布尔值
            for v in value.values():
                if isinstance(v, bool):
                    return v
            return False
        if isinstance(value, (list, tuple)):
            return self._to_bool(value[0]) if value else False
        if isinstance(value, str):
            # 清理字符串：去除 markdown 代码块、多余空白等
            cleaned = value.strip()
            # 去除 ```...``` 包裹：先尝试提取代码块内容，再 fallback 去首尾 ```
            import re
            if cleaned.startswith('```') and cleaned.endswith('```'):
                inner = cleaned[3:-3].strip()
                # 处理 ```json\ncontent\n``` 格式：去掉语言标记行
                if '\n' in inner:
                    lines = inner.split('\n', 1)
                    if len(lines) > 1 and re.match(r'^\w*$', lines[0].strip()):
                        inner = lines[1].strip()
                cleaned = inner
            lower = cleaned.lower()
            if lower in ("true", "1", "yes", "on"):
                return True
            if lower in ("false", "0", "no", "off", ""):
                return False
            # 尝试解析 JSON 字符串，提取结构化响应中的结果值
            # 常见场景：模型返回 {"result": true, "reasoning": "..."} 等 JSON
            try:
                import json
                data = json.loads(cleaned)
                return self._to_bool(data)
            except (json.JSONDecodeError, ValueError):
                pass
        if isinstance(value, (int, float)):
            return bool(value)
        return False

    def _to_label(self, value: Any, positive_value: str, label_type: str) -> bool:
        if label_type == "boolean":
            return self._to_bool(value)
        elif label_type == "string":
            return str(value).strip().lower() == positive_value.strip().lower()
        elif label_type == "number":
            try:
                return float(value) == float(positive_value)
            except (ValueError, TypeError):
                return False
        return self._to_bool(value)
