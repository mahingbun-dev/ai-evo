"""
规则指标计算器
从 ConsistencyEvaluator 提取的纯静态工具类，无外部依赖
"""
import re
import json
from typing import List, Dict


class RuleMetricsCalculator:
    """规则指标计算器 - 所有方法均为静态方法，不持有实例状态"""

    @staticmethod
    def _extract_json_from_markdown(text: str) -> str:
        """从Markdown中提取JSON内容"""
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            return match.group(1).strip()
        return text

    @staticmethod
    def _extract_json_content(text: str):
        """尝试从文本中提取JSON内容，返回 (data, json_type) 或 (None, 'non_json')"""
        try:
            return json.loads(text), "json"
        except Exception:
            pass
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            try:
                return json.loads(match.group(1).strip()), "markdown_json"
            except Exception:
                pass
        return None, "non_json"

    @staticmethod
    def _extract_keys(obj, prefix=""):
        """递归提取JSON中的所有键路径"""
        keys = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                key_path = f"{prefix}.{k}" if prefix else k
                keys.append(key_path)
                if isinstance(v, (dict, list)) and v:
                    keys.extend(RuleMetricsCalculator._extract_keys(v, key_path))
        elif isinstance(obj, list) and obj:
            keys.extend(RuleMetricsCalculator._extract_keys(obj[0], f"{prefix}[0]") if prefix else ["[0]"])
        return keys

    @staticmethod
    def _get_depth(obj, current_depth=0):
        """计算JSON嵌套深度"""
        if isinstance(obj, dict):
            if not obj:
                return current_depth
            return max(RuleMetricsCalculator._get_depth(v, current_depth + 1) for v in obj.values())
        elif isinstance(obj, list):
            if not obj:
                return current_depth
            return max(RuleMetricsCalculator._get_depth(item, current_depth + 1) for item in obj)
        return current_depth

    @staticmethod
    def calculate_text_similarity(texts: List[str]) -> float:
        """
        计算文本相似度（基于词集合的Jaccard相似度，支持中英文分词）

        Args:
            texts: 文本列表

        Returns:
            相似度分数 (0-1)
        """
        if len(texts) < 2:
            return 0.0

        def get_words(text):
            words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+|\d+', text.lower())
            return set(words)

        similarities = []
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                words1 = get_words(texts[i])
                words2 = get_words(texts[j])
                if not words1 or not words2:
                    continue
                intersection = len(words1 & words2)
                union = len(words1 | words2)
                similarity = intersection / union if union > 0 else 0
                similarities.append(similarity)

        return sum(similarities) / len(similarities) if similarities else 0.0

    @staticmethod
    def calculate_format_consistency(texts: List[str]) -> float:
        """
        计算格式一致性

        Args:
            texts: 文本列表

        Returns:
            格式一致性比例
        """
        if len(texts) < 2:
            return 0.0

        formats = []
        for text in texts:
            try:
                json.loads(text)
                formats.append("json")
                continue
            except Exception:
                pass

            extracted = RuleMetricsCalculator._extract_json_from_markdown(text)
            try:
                json.loads(extracted)
                formats.append("markdown_json")
                continue
            except Exception:
                pass

            if text.strip().startswith("#") or text.strip().startswith("-"):
                formats.append("markdown")
            elif "\n" in text and ":" in text:
                formats.append("key_value")
            else:
                formats.append("plain")

        if not formats:
            return 0.0

        most_common_format = max(set(formats), key=formats.count)
        consistent_count = formats.count(most_common_format)
        return consistent_count / len(formats)

    @staticmethod
    def calculate_structure_consistency(texts: List[str]) -> float:
        """
        计算结构一致性（检查JSON键集合、层级等）

        Args:
            texts: 文本列表

        Returns:
            结构一致性分数 (0-1)
        """
        if len(texts) < 2:
            return 0.0

        parsed = []
        json_count = 0
        for text in texts:
            data, json_type = RuleMetricsCalculator._extract_json_content(text)
            if data is not None:
                json_count += 1
                keys = set(RuleMetricsCalculator._extract_keys(data))
                depth = RuleMetricsCalculator._get_depth(data)
                parsed.append({"keys": keys, "depth": depth, "type": "json"})
            else:
                parsed.append({"type": "non_json"})

        if json_count == len(texts):
            key_similarities = []
            depths = [p["depth"] for p in parsed]

            for i in range(len(parsed)):
                for j in range(i + 1, len(parsed)):
                    keys1 = parsed[i]["keys"]
                    keys2 = parsed[j]["keys"]
                    if keys1 and keys2:
                        intersection = len(keys1 & keys2)
                        union = len(keys1 | keys2)
                        key_sim = intersection / union if union > 0 else 0
                    else:
                        key_sim = 0
                    key_similarities.append(key_sim)

            max_depth = max(depths) if depths else 0
            if max_depth > 0:
                depth_similarities = [1 - abs(d - depths[0]) / max_depth for d in depths[1:]]
                depth_consistency = sum(depth_similarities) / len(depth_similarities) if depth_similarities else 1.0
            else:
                depth_consistency = 1.0

            key_consistency = sum(key_similarities) / len(key_similarities) if key_similarities else 0.0
            return (key_consistency + depth_consistency) / 2

        return json_count / len(texts) if texts else 0.0

    @staticmethod
    def calculate_all(texts: List[str]) -> Dict[str, float]:
        """
        计算所有规则指标

        Args:
            texts: 文本列表

        Returns:
            包含三个指标的字典
        """
        return {
            "text_similarity": round(RuleMetricsCalculator.calculate_text_similarity(texts), 4),
            "format_consistency": round(RuleMetricsCalculator.calculate_format_consistency(texts), 4),
            "structure_consistency": round(RuleMetricsCalculator.calculate_structure_consistency(texts), 4),
        }
