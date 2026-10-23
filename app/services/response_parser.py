"""
响应解析处理模块
支持：json_path/json_regex/text_match/custom
"""
import json
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from jsonpath import jsonpath

from app.services.judge_pool import judge_pool_slot

logger = logging.getLogger(__name__)


class ResponseParser:
    """响应解析器"""

    @staticmethod
    def parse_multiple(response: Union[str, Dict], parse_configs: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        批量解析响应，提取多个变量

        Args:
            response: 原始响应（字符串或字典）
            parse_configs: 解析配置列表，每项包含:
                - name: 变量名
                - parse_type: 解析类型 (json_path/json_regex/text_match)
                - parse_rule: 解析规则
                - default: 默认值（可选）

        Returns:
            {变量名: 值} 的字典
        """
        results = {}

        # 转换为字典（如果需要）
        if isinstance(response, str):
            try:
                response_data = json.loads(response)
            except json.JSONDecodeError:
                response_data = response
        else:
            response_data = response

        for config in parse_configs:
            var_name = config.get("name")
            if not var_name:
                continue

            parse_type = config.get("parse_type", "json_path")
            parse_rule = config.get("parse_rule", {})
            default_value = config.get("default", "")

            try:
                value = ResponseParser.parse(response_data, {
                    "parse_type": parse_type,
                    "parse_rule": parse_rule
                })
                # 空值时使用默认值
                if value is None or value == "":
                    value = default_value
                results[var_name] = value
            except Exception:
                # 解析失败使用默认值
                results[var_name] = default_value

        return results

    @staticmethod
    def parse(response: Union[str, Dict], parse_config: Dict[str, Any]) -> str:
        """
        根据配置解析响应

        Args:
            response: 原始响应（字符串或字典）
            parse_config: 解析配置

        Returns:
            解析后的内容
        """
        parse_type = parse_config.get("parse_type", "text_match")
        parse_rule = parse_config.get("parse_rule", {})
        fallback = parse_config.get("fallback_parse", {"type": "text", "trim": True})

        # 转换为字典（如果需要）
        if isinstance(response, str):
            try:
                response_data = json.loads(response)
            except json.JSONDecodeError:
                # 如果不是 JSON，使用文本解析
                return ResponseParser._text_parse(response, fallback)
        else:
            response_data = response

        try:
            if parse_type == "json_path":
                return ResponseParser._json_path_parse(response_data, parse_rule)

            elif parse_type == "json_regex":
                return ResponseParser._json_regex_parse(response, parse_rule)

            elif parse_type == "text_match":
                return ResponseParser._text_parse(response, parse_rule)

            elif parse_type == "custom":
                # 自定义解析需要传入函数名，这里简单处理
                return str(response_data)

            else:
                return str(response_data)

        except Exception as e:
            # 解析失败，使用 fallback
            if fallback.get("type") == "text":
                text = str(response)
                if fallback.get("trim"):
                    text = text.strip()
                return text
            return str(response)

    @staticmethod
    def _json_path_parse(response_data: Dict, parse_rule: Dict) -> str:
        """JSONPath 解析"""
        content_path = parse_rule.get("content")
        if not content_path:
            return str(response_data)

        result = jsonpath(response_data, content_path)
        if result is not False and result is not None:
            # 如果是列表，返回第一个元素
            if isinstance(result, list):
                if not result:
                    return ""
                val = result[0]
                # 布尔值转小写字符串，避免 str(True) → "True"
                if isinstance(val, bool):
                    return "true" if val else "false"
                return str(val)
            # 布尔值转小写字符串，避免 str(True) → "True"
            if isinstance(result, bool):
                return "true" if result else "false"
            return str(result)
        # jsonpath 返回 False 但 response_data 是列表时，直接返回整个响应
        if isinstance(response_data, list):
            return json.dumps(response_data, ensure_ascii=False)
        return ""

    @staticmethod
    def _json_regex_parse(response: Union[str, Dict], parse_rule: Dict) -> str:
        """正则表达式解析"""
        pattern = parse_rule.get("pattern")
        if not pattern:
            return str(response)

        response_str = str(response)
        match = re.search(pattern, response_str, re.DOTALL)
        if match:
            # 如果有捕获组，返回第一个捕获组
            if match.groups():
                return match.group(1)
            return match.group(0)
        return ""

    @staticmethod
    def _text_parse(response: Union[str, Dict], parse_rule: Dict) -> str:
        """文本直接返回"""
        text = str(response)
        if parse_rule.get("trim", True):
            text = text.strip()
        return text


class JudgeResponseParser:
    """裁判模型响应解析器 - 多阶段渐进式解析

    提供稳定可靠的裁判模型输出解析能力，支持：
    1. JSON解析（处理Markdown包裹、截断等问题）
    2. 截断修复（增强的多层嵌套修复）
    3. 正则兜底（多模式匹配）
    4. AI二次校正（调用模型提取结构化结果）
    5. 最终降级（返回部分结果+失败标记）
    """

    # 评分模式（阶段3使用）
    SCORE_PATTERNS = [
        r'[评得]分[：:]\s*(\d+(?:\.\d+)?)',
        r'score[：:]\s*(\d+(?:\.\d+)?)',
        r'评分\s*(\d+(?:\.\d+)?)',
        r'(\d+(?:\.\d+)?)\s*/\s*(\d+)',
    ]

    def __init__(self, evaluation_metrics: Optional[List[Dict]] = None, max_score: float = 10, judge_config: Optional[Dict[str, Any]] = None):
        """
        初始化解析器

        Args:
            evaluation_metrics: 评测维度配置列表
            max_score: 最大评分
            judge_config: 裁判模型配置（用于AI校正阶段）
        """
        self.evaluation_metrics = evaluation_metrics or []
        self.max_score = max_score
        self.judge_config = judge_config
        self._parse_stages_log: List[str] = []

    def log_info(self, message: str):
        """记录解析阶段日志"""
        logger.info(f"[JudgeResponseParser] {message}")
        self._parse_stages_log.append(message)

    def log_warning(self, message: str):
        """记录警告日志"""
        logger.warning(f"[JudgeResponseParser] {message}")
        self._parse_stages_log.append(f"[WARN] {message}")

    def parse(self, raw_response: str, retry_count: int = 0, max_retries: int = 2) -> 'JudgeParseResult':
        """主解析入口 - 多阶段渐进式解析

        Args:
            raw_response: 原始响应
            retry_count: 当前重试次数
            max_retries: 最大重试次数（AI校正阶段）

        Returns:
            JudgeParseResult: 解析结果
        """
        result = JudgeParseResult(raw_response=raw_response)

        # 阶段1: JSON解析
        data = self._stage1_json_parse(raw_response)
        if data is not None:
            self._extract_fields_from_json(data, result)
            if result.score is not None:
                result.parse_success = True
                return result

        # 阶段2: 截断修复
        data = self._stage2_truncate_fix(raw_response)
        if data is not None:
            self._extract_fields_from_json(data, result)
            if result.score is not None:
                result.parse_success = True
                return result

        # 阶段3: 正则兜底
        self._stage3_regex_fallback(raw_response, result)
        if result.score is not None:
            # 正则可能提取了score但feedback还是原始内容，更新一下
            if result.feedback == raw_response:
                result.feedback = self._extract_feedback_by_regex(raw_response) or raw_response
            if result.score is not None:
                result.parse_success = True
                return result

        # 阶段4: AI二次校正
        if retry_count < max_retries:
            corrected = self._stage4_ai_correction(raw_response)
            if corrected is not None:
                self._extract_fields_from_json(corrected, result)
                if result.score is not None:
                    result.parse_success = True
                    return result
                else:
                    # 递归重试
                    return self.parse(raw_response, retry_count + 1, max_retries)

        # 阶段5: 最终降级
        self._stage5_degradation(result, raw_response)
        return result

    def _stage1_json_parse(self, response: str) -> Optional[Dict[str, Any]]:
        """阶段1: JSON解析"""
        try:
            # 提取代码块
            json_str = self._extract_code_block(response)
            if not json_str:
                self.log_warning(f"[阶段1失败] 代码块内容为空，response前500字符: {response[:500]}")
                return None

            # 清洗和解析
            sanitized = _sanitize_json_string_for_judge(json_str)
            if not sanitized:
                self.log_warning(f"[阶段1失败] sanitize后内容为空，response前500字符: {response[:500]}")
                return None

            data = json.loads(sanitized)
            return data

        except json.JSONDecodeError as e:
            self.log_warning(f"[阶段1失败] JSON解析错误: {e}，response前500字符: {response[:500]}")
            return None
        except Exception as e:
            self.log_warning(f"[阶段1失败] 未知错误: {e}，response前500字符: {response[:500]}")
            return None

    def _stage2_truncate_fix(self, response: str) -> Optional[Dict[str, Any]]:
        """阶段2: 截断修复"""
        try:
            json_str = self._extract_code_block(response)
            if not json_str:
                self.log_warning(f"[阶段2失败] 代码块内容为空，response前500字符: {response[:500]}")
                return None

            sanitized = _sanitize_json_string_for_judge(json_str)
            fixed = _try_fix_truncated_json_enhanced(sanitized)

            data = json.loads(fixed)
            return data

        except json.JSONDecodeError as e:
            self.log_warning(f"[阶段2失败] 截断修复后仍然解析失败: {e}，response前500字符: {response[:500]}")
            return None
        except Exception as e:
            self.log_warning(f"[阶段2失败] 未知错误: {e}，response前500字符: {response[:500]}")
            return None

    def _stage3_regex_fallback(self, response: str, result: 'JudgeParseResult'):
        """阶段3: 正则兜底 - 提取score和dimension_scores"""
        matched = False

        # 提取综合评分
        for pattern in self.SCORE_PATTERNS:
            match = re.search(pattern, response)
            if match:
                if len(match.groups()) == 2:
                    score = float(match.group(1))
                    max_score_val = float(match.group(2))
                    result.score = (score / max_score_val) * self.max_score
                else:
                    result.score = float(match.group(1))
                matched = True
                break

        # 确保评分在有效范围内
        if result.score is not None:
            result.score = max(0, min(self.max_score, result.score))

        # 提取维度评分
        dimension_scores = {}
        for metric in self.evaluation_metrics:
            metric_name = metric.get("name", "")
            if not metric_name:
                continue

            # 匹配 "XX评分: Y" 或 "XX得分: Y"
            pattern = rf'{re.escape(metric_name)}[评得]分[：:]\s*(\d+(?:\.\d+)?)'
            match = re.search(pattern, response)

            if match:
                dimension_scores[metric_name] = float(match.group(1))

        if dimension_scores:
            result.dimension_scores = dimension_scores

        # 如果配置了评测维度且有维度评分，用加权平均重新计算综合评分
        self._recalculate_score_from_dimensions(result)

        if not matched and not dimension_scores:
            self.log_warning(f"[阶段3失败] 正则未能匹配到任何评分，response前500字符: {response[:500]}")

    def _stage4_ai_correction(self, raw_response: str) -> Optional[Dict[str, Any]]:
        """阶段4: AI二次校正"""
        try:
            # 检查是否有裁判模型配置
            if not self.judge_config:
                self.log_warning(f"[阶段4失败] 没有提供裁判模型配置，response前500字符: {raw_response[:500]}")
                return None

            # 构建校正Prompt
            correction_prompt = self._build_correction_prompt(raw_response)

            system_prompt = "你是一个JSON提取助手。你的任务是从给定的文本中提取结构化的评分信息。"

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": correction_prompt},
            ]

            # 使用ModelCallerSync进行调用
            from app.services.model_caller import ModelCallerSync

            model_caller = ModelCallerSync()
            with judge_pool_slot("graph", meta={"call_site": "response_parser"}):
                result = model_caller.call(messages, self.judge_config, {})

            if result.get("success"):
                content = result.get("content", "")

                # 解析校正后的响应
                corrected_data = self._stage1_json_parse(content)
                if corrected_data is not None:
                    return corrected_data

                # 如果JSON解析失败，尝试提取代码块
                json_str = self._extract_code_block(content)
                if json_str:
                    try:
                        import json
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        pass

                self.log_warning(f"[阶段4失败] AI校正响应无法解析为JSON，response前500字符: {content[:500]}")
                return None
            else:
                error = result.get("error", "未知错误")
                self.log_warning(f"[阶段4失败] AI校正调用失败: {error}，原始response前500字符: {raw_response[:500]}")
                return None

        except Exception as e:
            self.log_warning(f"[阶段4失败] AI校正异常: {e}，response前500字符: {raw_response[:500]}")
            return None

    def _build_correction_prompt(self, raw_response: str) -> str:
        """构建AI校正Prompt"""
        # 截断过长的响应
        truncated_response = raw_response[:3000] + ('...（已截断）' if len(raw_response) > 3000 else '')

        return f"""你是一个JSON提取助手。给定一个可能包含思考过程的文本，请提取出结构化的评分结果。

需要提取的字段：
- score: 综合评分（0-10的数字）
- dimension_scores: 各维度评分（对象，键为维度名，值为分数）
- feedback: 反馈理由（字符串）

规则：
1. 如果某个字段无法提取或不存在，标记为 null
2. 只输出JSON，不要有其他内容
3. 确保JSON格式正确

原始输出：
---
{truncated_response}
---

提取结果："""

    def _stage5_degradation(self, result: 'JudgeParseResult', raw_response: str):
        """阶段5: 最终降级 - 返回部分结果+失败标记"""
        result.failed_stages = self._parse_stages_log.copy()

        # 标记失败的字段
        if result.score is None:
            result.failed_fields.append("score")
        if result.feedback == result.raw_response:
            result.failed_fields.append("feedback")
        if result.dimension_scores is None:
            result.failed_fields.append("dimension_scores")

        # 设置默认值
        if result.score is None:
            result.score = 0

        if result.feedback == result.raw_response or not result.feedback:
            result.feedback = "[解析失败，请查看raw_response]"

        self.log_warning(f"[最终降级] 解析失败，失败字段: {result.failed_fields}，response前500字符: {raw_response[:500]}")

    def _extract_fields_from_json(self, data: Dict[str, Any], result: 'JudgeParseResult'):
        """从JSON数据中提取字段"""
        if not isinstance(data, dict):
            return

        # 提取score
        if "score" in data:
            score_val = data.get("score")
            if score_val is not None:
                try:
                    result.score = float(score_val)
                    result.score = max(0, min(self.max_score, result.score))
                except (ValueError, TypeError):
                    pass

        # 提取feedback
        if "feedback" in data:
            feedback_val = data.get("feedback")
            if feedback_val is not None:
                result.feedback = str(feedback_val)

        # 提取dimension_scores
        dimension_scores = self._extract_dimension_scores_from_json(data)
        if dimension_scores:
            result.dimension_scores = dimension_scores

        # 如果配置了评测维度且有维度评分，用加权平均重新计算综合评分
        # （因为 prompt 告知 LLM"综合评分将由系统按权重自动计算"，LLM 返回的 score 不可靠）
        self._recalculate_score_from_dimensions(result)

    def _recalculate_score_from_dimensions(self, result: 'JudgeParseResult'):
        """
        如果配置了评测维度且有维度评分，用加权平均重新计算综合评分。

        因为 prompt 告知 LLM "综合评分将由系统按权重自动计算，无需你手动计算"，
        LLM 返回的 score 字段值不可靠（常返回 0 或 null）。
        使用维度评分的加权平均值作为最终得分。
        """
        if not self.evaluation_metrics or not result.dimension_scores:
            return

        from app.services.judge_evaluation_engine import JudgeEvaluationEngine
        calculated_score = JudgeEvaluationEngine.calculate_weighted_score(
            self.evaluation_metrics,
            result.dimension_scores,
            self.max_score
        )
        result.score = calculated_score

    def _extract_dimension_scores_from_json(self, data: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """从JSON中提取维度评分（与JudgeClient兼容）"""
        if not data or not isinstance(data, dict):
            return None

        raw = data.get("dimension_scores")
        if not raw or not isinstance(raw, dict):
            return None

        # 构建已配置维度名集合
        configured_names = {m.get("name", "") for m in self.evaluation_metrics if m.get("name")}

        dimension_scores = {}
        for dim_name, dim_value in raw.items():
            if isinstance(dim_value, (int, float)):
                score = float(dim_value)
            elif isinstance(dim_value, dict):
                score_val = dim_value.get("score") or dim_value.get("dimension_score")
                if score_val is None:
                    continue
                score = float(score_val)
            else:
                continue

            # 若配置了evaluation_metrics，只保留匹配的维度
            if configured_names and dim_name not in configured_names:
                matched = next((n for n in configured_names if n.startswith(dim_name) or dim_name.startswith(n)), None)
                if not matched:
                    continue
                dim_name = matched

            dimension_scores[dim_name] = score

        return dimension_scores if dimension_scores else None

    def _extract_code_block(self, response: str) -> Optional[str]:
        """从响应中提取代码块内容"""
        # 尝试提取 ```json 代码块
        code_block_match = re.search(r'```json\s*\n?(.*?)\n?```', response, re.DOTALL)
        if code_block_match:
            return code_block_match.group(1).strip()

        # 尝试提取 ``` 代码块
        code_block_match = re.search(r'```\s*\n?(.*?)\n?```', response, re.DOTALL)
        if code_block_match:
            return code_block_match.group(1).strip()

        # 没有代码块，返回原始内容
        return response.strip() if response.strip() else None

    def _extract_feedback_by_regex(self, response: str) -> Optional[str]:
        """用正则提取反馈文本（兜底）"""
        patterns = [
            r'反馈[：:]\s*(.{10,500}?)(?=\n|$)',
            r'feedback[：:]\s*(.{10,500}?)(?=\n|$)',
            r'评价[：:]\s*(.{10,500}?)(?=\n|$)',
        ]

        for pattern in patterns:
            match = re.search(pattern, response, re.DOTALL)
            if match:
                return match.group(1).strip()

        return None


@dataclass
class JudgeParseResult:
    """裁判模型解析结果"""
    score: Optional[float] = None
    feedback: str = ""
    dimension_scores: Optional[Dict[str, float]] = None
    parse_success: bool = False
    failed_stages: List[str] = field(default_factory=list)
    failed_fields: List[str] = field(default_factory=list)
    raw_response: str = ""
    parse_stages_log: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式，兼容原有接口"""
        return {
            "score": self.score,
            "feedback": self.feedback,
            "dimension_scores": self.dimension_scores,
            "parse_success": self.parse_success,
            "failed_fields": self.failed_fields,
            "raw_response": self.raw_response,
        }


def _sanitize_json_string_for_judge(json_str: str) -> str:
    """将 JSON 字符串中字段值内的真实控制字符转为合法转义序列。

    解决模型输出的 feedback 字段包含真实换行符（\\x0a）导致 json.loads 报
    'Invalid control character' 的问题。逐字符扫描，只对字符串值内部的控制字符做转义。
    """
    result = []
    in_string = False
    escape_next = False
    for char in json_str:
        if escape_next:
            result.append(char)
            escape_next = False
        elif char == '\\':
            result.append(char)
            escape_next = True
        elif char == '"':
            result.append(char)
            in_string = not in_string
        elif in_string and char == '\n':
            result.append('\\n')
        elif in_string and char == '\r':
            result.append('\\r')
        elif in_string and char == '\t':
            result.append('\\t')
        else:
            result.append(char)
    return ''.join(result)


def _try_fix_truncated_json_enhanced(json_str: str) -> str:
    """增强的截断JSON修复策略。

    支持：
    1. 多层嵌套结构的截断修复
    2. 多个字段同时截断的情况
    3. 字段值内特殊字符的转义
    """
    # 如果 JSON 已经可以解析，不需要修复
    try:
        json.loads(json_str)
        return json_str
    except json.JSONDecodeError:
        pass

    result = json_str

    # 策略1: 修复 feedback 字段截断
    feedback_match = re.search(r'("feedback":\s*")', result)
    if feedback_match:
        value_start = feedback_match.end()
        remaining = result[value_start:]

        # 如果 remaining 中没有结束引号，说明字符串值被截断
        if '"' not in remaining:
            result = result + '"'

    # 策略2: 修复其他常见字符串字段截断（comment, reason, analysis等）
    string_fields = ["comment", "reason", "analysis", "description", "detail"]
    for field_name in string_fields:
        pattern = rf'("{field_name}":\s*")'
        field_match = re.search(pattern, result)
        if field_match:
            value_start = field_match.end()
            remaining = result[value_start:]

            # 检查是否被截断（没有结束引号）
            if remaining and not re.match(r'^[^"]*["\]\},\n]', remaining):
                if '"' not in remaining:
                    result = result + '"'

    # 策略3: 补全缺失的括号
    open_braces = result.count('{') - result.count('}')
    open_brackets = result.count('[') - result.count(']')
    if open_braces > 0:
        result += '}' * open_braces
    if open_brackets > 0:
        result += ']' * open_brackets

    return result


class MultimodalParser:

    @staticmethod
    def prepare_multimodal_content(
        text: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
        input_format: str = "base64"
    ) -> list:
        """
        准备多模态内容

        Args:
            text: 文本内容
            images: 图片列表（base64 或 URL）
            files: 文件列表
            input_format: 输入格式 base64/url

        Returns:
            消息内容列表
        """
        content = []

        # 添加文本
        if text:
            content.append({
                "type": "text",
                "text": text
            })

        # 添加图片
        if images:
            for img in images:
                if input_format == "base64":
                    content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img}"
                        }
                    })
                else:
                    content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": img
                        }
                    })

        # 添加文件
        if files:
            for file in files:
                content.append({
                    "type": "document",
                    "document": file
                })

        return content if content else [{"type": "text", "text": text or ""}]


def extract_json_from_response(content: str) -> Optional[Dict[str, Any]]:
    """从裁判模型返回的内容中提取 JSON

    提取策略（按优先级）:
    1. ```json 代码块
    2. ``` 代码块
    3. 直接 json.loads

    Args:
        content: 原始返回内容

    Returns:
        解析后的字典，如果提取失败返回 None
    """
    if not content or not content.strip():
        return None

    # 尝试提取 ```json 代码块
    code_block_match = re.search(r'```json\s*\n?(.*?)\n?```', content, re.DOTALL)
    if code_block_match:
        json_str = code_block_match.group(1).strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # 尝试提取 ``` 代码块
    code_block_match = re.search(r'```\s*\n?(.*?)\n?```', content, re.DOTALL)
    if code_block_match:
        json_str = code_block_match.group(1).strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # 尝试直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    return None
