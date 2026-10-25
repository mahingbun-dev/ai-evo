"""
裁判模型客户端

负责调用裁判模型获取评分和反馈
"""
import json
import re
import logging
from typing import Dict, Any, Optional

from app.services.judge_pool import judge_pool_slot

logger = logging.getLogger(__name__)


def _sanitize_json_string(json_str: str) -> str:
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


def _try_fix_truncated_json(json_str: str) -> str:
    """尝试修复截断的 JSON 字符串。

    当 JSON 被截断时（如 feedback 字段内容过长被截断），
    尝试补全缺失的引号和括号来使 JSON 可解析。
    """
    # 如果 JSON 已经可以解析，不需要修复
    try:
        json.loads(json_str)
        return json_str
    except json.JSONDecodeError:
        pass

    result = json_str

    # 简单策略：找到 feedback 字段值开始的位置
    # 如果字符串在该字段之后被截断，补充缺失的引号和括号
    feedback_match = re.search(r'("feedback":\s*")', result)
    if feedback_match:
        # feedback 值开始位置（引号后）
        value_start = feedback_match.end()
        remaining = result[value_start:]

        # 如果 remaining 中没有结束引号，说明字符串值被截断
        if '"' not in remaining:
            # 直接在末尾添加引号
            result = result + '"'

    # 补全缺失的括号
    open_braces = result.count('{') - result.count('}')
    open_brackets = result.count('[') - result.count(']')
    if open_braces > 0:
        result += '}' * open_braces
    if open_brackets > 0:
        result += ']' * open_brackets

    return result


class JudgeClient:
    """裁判模型客户端"""

    def __init__(self, task):
        """
        初始化裁判客户端

        Args:
            task: 任务对象，包含裁判模型配置
        """
        self.task = task
        self.judge_model = task.judge_model
        self.judge_prompt = task.judge_prompt
        self.max_score = task.max_score or 10
        self.evaluation_metrics = task.evaluation_metrics or []

    def compress(self, system_prompt: str, user_prompt: str) -> str:
        """
        压缩文本
        """
        response = self._call_judge_model(system_prompt, user_prompt)
        return response

    @staticmethod
    def _is_context_overflow_error(error_msg: str) -> bool:
        """判断是否为上下文超限错误"""
        if not error_msg:
            return False
        lower = error_msg.lower()
        return any(kw in lower for kw in [
            "context window exceeds limit",
            "context_length_exceeded",
            "invalid params, context window",
            "maximum context length",
            "token limit",
            "context length",
        ])

    @staticmethod
    def _truncate_for_retry(system_prompt: str, user_prompt: str, safety_ratio: float = 0.75) -> str:
        """对 user_prompt 进行硬截断以用于重试，优先保留末尾部分（通常是 target）。

        Args:
            system_prompt: 系统提示词（用于估算占用）
            user_prompt: 原始 user_prompt
            safety_ratio: user_prompt 保留比例（默认 75%）

        Returns:
            截断后的 user_prompt
        """
        # 简单估算：字符 / 1.5 ≈ token，目标总 token 约 60000
        system_chars = len(system_prompt)
        max_total_chars = int(60000 * 1.5)
        user_budget_chars = int((max_total_chars - system_chars) * safety_ratio)

        if len(user_prompt) <= user_budget_chars:
            return user_prompt

        # 优先保留末尾 40%（通常是 target），截断前 60%（参考内容）
        keep_tail = int(user_budget_chars * 0.4)
        keep_head = user_budget_chars - keep_tail

        head = user_prompt[:keep_head]
        tail = user_prompt[len(user_prompt) - keep_tail:]
        compressed = head + "\n\n[...参考内容已压缩以重试...]\n\n" + tail
        logger.warning(
            f"[重试截断] user_prompt: {len(user_prompt)} -> {len(compressed)} 字符"
            f"（保留头部 {keep_head} + 尾部 {keep_tail} 字符）"
        )
        return compressed

    def _call_judge_model(self, system_prompt: str, user_prompt: str, runtime_variables: Optional[Dict[str, Any]] = None) -> str:
        """调用裁判模型 - 复用 ModelCallerSync"""
        try:
            from app.services.model_caller import ModelCallerSync

            # 从task中获取裁判模型配置
            # 优先使用全局 Claude Code 配置（裁判模型软退场），回落 task.judge_model
            judge_model_id = self.judge_model
            judge_config = getattr(self.task, "judge_model_config", {}) or {}

            if not judge_config:
                try:
                    from app.services.claude_code_config_service import ClaudeCodeConfigService

                    judge_config = ClaudeCodeConfigService.get_judge_llm_config() or {}
                except Exception:
                    pass

            # 如果仍为空，尝试从数据库查询历史裁判模型配置
            if not judge_config and judge_model_id:
                from app.db.session import SessionLocal
                from app.models.model_config_models import ModelConfig

                db = SessionLocal()
                try:
                    model_config_obj = db.query(ModelConfig).filter(ModelConfig.id == judge_model_id).first()
                    if model_config_obj:
                        # 复用 ModelCaller 需要的配置格式
                        judge_config = {
                            "provider": model_config_obj.provider,
                            "model_type": model_config_obj.model_type,
                            "endpoint": model_config_obj.endpoint,
                            "api_key": model_config_obj.api_key,
                            "auth_type": model_config_obj.auth_type,
                            "auth_config": model_config_obj.auth_config,
                            "request_config": model_config_obj.request_config,
                            "response_parse_config": model_config_obj.response_parse_config,
                            "model_id": model_config_obj.id,
                        }
                finally:
                    db.close()

            # 构建消息
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            # 复用 ModelCallerSync 调用模型
            model_caller = ModelCallerSync()
            with judge_pool_slot("graph", meta={"call_site": "judge_client"}):
                result = model_caller.call(messages, judge_config, runtime_variables or {})

            if result.get("success"):
                return result.get("content", "")

            error_msg = result.get("error", "未知错误")

            # 检测上下文超限错误，输出 user_prompt 并触发重试
            if self._is_context_overflow_error(str(error_msg)):
                logger.error(
                    f"[上下文超限] 裁判模型返回上下文超限错误\n"
                    f"  system_prompt: {len(system_prompt)} 字符\n"
                    f"  user_prompt: {len(user_prompt)} 字符\n"
                    f"  原始错误: {error_msg}"
                )
                # 输出完整 user_prompt 供排查（超长时截断显示）
                display_limit = 5000
                if len(user_prompt) > display_limit:
                    logger.error(
                        f"[上下文超限] user_prompt 内容（共 {len(user_prompt)} 字符，仅显示前后各 {display_limit // 2} 字符）:\n"
                        f"--- 开头 ---\n{user_prompt[:display_limit // 2]}\n"
                        f"--- 结尾 ---\n{user_prompt[-(display_limit // 2):]}"
                    )
                else:
                    logger.error(f"[上下文超限] user_prompt 完整内容:\n{user_prompt}")

                # 兜底截断重试
                compressed_user_prompt = self._truncate_for_retry(system_prompt, user_prompt)
                logger.warning(
                    f"[重试] 触发兜底截断重试：user_prompt {len(user_prompt)} -> {len(compressed_user_prompt)} 字符"
                )
                retry_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": compressed_user_prompt},
                ]
                with judge_pool_slot("graph", meta={"call_site": "judge_client_retry"}):
                    retry_result = model_caller.call(retry_messages, judge_config, {})
                if retry_result.get("success"):
                    logger.info("[重试] 兜底重试成功")
                    return retry_result.get("content", "")
                else:
                    retry_error = retry_result.get("error", "未知错误")
                    logger.error(f"[重试] 兜底重试仍失败: {retry_error}")

            raise RuntimeError(error_msg)

        except RuntimeError:
            raise
        except Exception as e:
            logger.exception("裁判模型调用失败")
            raise RuntimeError(f"裁判模型调用失败: {str(e)}")
    def _extract_dimension_scores_from_json(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """从裁判模型返回的 JSON 数据中提取各维度评分。

        支持两种常见的 JSON 结构：
        1. 扁平结构：{"dimension_scores": {"维度名": 分数, ...}}
        2. 嵌套结构：{"dimension_scores": {"维度名": {"score": 分数, ...}, ...}}

        同时使用 evaluation_metrics 的名称列表做过滤，确保只保留已配置的维度。
        """
        if not data or not isinstance(data, dict):
            return None

        raw = data.get("dimension_scores")
        if not raw or not isinstance(raw, dict):
            return None

        # 构建已配置维度名集合（用于过滤）
        configured_names = {m.get("name", "") for m in self.evaluation_metrics if m.get("name")}

        dimension_scores = {}
        for dim_name, dim_value in raw.items():
            if isinstance(dim_value, (int, float)):
                score = float(dim_value)
            elif isinstance(dim_value, dict):
                # 嵌套格式：{"score": 8, "reason": "..."}
                score_val = dim_value.get("score") or dim_value.get("dimension_score")
                if score_val is None:
                    continue
                score = float(score_val)
            else:
                continue

            # 若配置了 evaluation_metrics，只保留匹配的维度；否则全量保留
            if configured_names and dim_name not in configured_names:
                # 尝试前缀匹配（如 "维度1" 匹配 "维度1：需求覆盖度（Coverage）"）
                matched = next((n for n in configured_names if n.startswith(dim_name) or dim_name.startswith(n)), None)
                if not matched:
                    continue
                dim_name = matched

            dimension_scores[dim_name] = score

        return dimension_scores if dimension_scores else None
