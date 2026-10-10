"""
Prompt组装器

负责组装裁判模型的系统提示词和用户提示词
"""
import json
from typing import Dict, Any, Optional, List

WEIGHT_MAP = {"高": 3, "中": 2, "低": 1}


class PromptBuilder:
    """Prompt组装器"""

    def __init__(self, task, evaluation_targets: Optional[List[Dict[str, Any]]] = None,
                 overrides: Optional[Dict[str, Any]] = None):
        """
        初始化Prompt组装器

        Args:
            task: 任务对象
            evaluation_targets: 评测目标配置列表（可选）
            overrides: 覆盖件（可选）{score_details, evaluation_metrics}，
                由执行链路解析 @ 引用标记后传入，优先于 task 同名字段
        """
        import logging
        logger = logging.getLogger(__name__)

        self.task = task
        self.judge_prompt = task.judge_prompt or ""
        self.evaluation_scene = task.evaluation_scene or ""
        self.scene_desc = task.scene_desc or ""
        self.evaluation_metrics = task.evaluation_metrics or []
        self.max_score = task.max_score or 10
        self.score_details = task.score_details or ""
        self.steps = task.steps or []
        # overrides 优先：@ 引用标记解析后的 score_details / evaluation_metrics
        # 覆盖 task 同名字段（契约 v3 §三）
        if overrides:
            if overrides.get("score_details") is not None:
                self.score_details = overrides.get("score_details") or ""
            if overrides.get("evaluation_metrics") is not None:
                self.evaluation_metrics = overrides.get("evaluation_metrics") or []
        # 裁判模型变量配置，支持字典和列表两种格式
        judge_var_config = getattr(task, "judge_variable_config", None)
        if isinstance(judge_var_config, dict):
            # 字典格式: {"user_input": {...}, "ref_answer": {...}}
            self.judge_variable_config = judge_var_config
        elif isinstance(judge_var_config, list):
            # 列表格式: [{...}, {...}]
            self.judge_variable_config = judge_var_config
        else:
            self.judge_variable_config = {}
        # runtime_variables（用于 manual 变量解析）
        self.runtime_variables = getattr(task, "runtime_variables", {}) or {}
        # 评测目标配置（可选，用于将评测内容注入到系统提示词）
        # 优先使用参数传入的值，否则从 task 对象中获取
        self.evaluation_targets = evaluation_targets or getattr(task, "evaluation_targets", None)
        logger.info(f"[PromptBuilder.__init__] evaluation_targets={self.evaluation_targets}")

    @classmethod
    def from_config(cls, config: Dict[str, Any],
                    overrides: Optional[Dict[str, Any]] = None) -> "PromptBuilder":
        """从配置字典创建 PromptBuilder 实例（无需 task 对象）

        Args:
            config: 配置字典，包含 judge_prompt、evaluation_metrics 等字段
            overrides: 覆盖件（可选）{score_details, evaluation_metrics}，
                优先于 config 同名字段（@ 引用标记解析结果）

        Returns:
            PromptBuilder 实例
        """
        instance = cls.__new__(cls)
        instance.task = None
        instance.judge_prompt = config.get("judge_prompt", "")
        instance.evaluation_scene = config.get("evaluation_scene", "一致性评测")
        instance.scene_desc = config.get("scene_desc", "评估同一问题多次回复的一致性质量")
        instance.evaluation_metrics = config.get("evaluation_metrics", [])
        instance.max_score = config.get("max_score", 10)
        instance.score_details = config.get("score_details", "")
        instance.steps = config.get("steps", [])
        instance.evaluation_targets = config.get("evaluation_targets")
        instance.runtime_variables = config.get("runtime_variables", {})
        # overrides 优先：@ 引用标记解析后的 score_details / evaluation_metrics
        # 覆盖 config 同名字段（契约 v3 §三）
        if overrides:
            if overrides.get("score_details") is not None:
                instance.score_details = overrides.get("score_details") or ""
            if overrides.get("evaluation_metrics") is not None:
                instance.evaluation_metrics = overrides.get("evaluation_metrics") or []

        judge_var_config = config.get("judge_variable_config", None)
        if isinstance(judge_var_config, dict):
            instance.judge_variable_config = judge_var_config
        elif isinstance(judge_var_config, list):
            instance.judge_variable_config = judge_var_config
        else:
            instance.judge_variable_config = {}

        return instance

    def _resolve_judge_variable(
        self,
        var_config: Dict[str, Any],
        payload: Dict[str, Any],
        model_output: str,
        chain_results: list = None,
    ) -> str:
        """
        解析裁判变量值

        Args:
            var_config: 变量配置 {"name": "...", "source": "...", "source_config": {...}, "prompt_type": "..."}
            payload: 请求Payload
            model_output: 模型输出
            chain_results: 请求链结果列表

        Returns:
            变量值
        """
        import logging
        logger = logging.getLogger(__name__)

        var_name = var_config.get("name", "unknown")
        source = var_config.get("source")
        source_config = var_config.get("source_config", {})


        if source == "chain_result":
            # 从请求链结果获取，支持多变量模式
            chain_index = source_config.get("chain_index")
            chain_order = source_config.get("order")  # 支持通过 order 获取
            chain_node = source_config.get("chain_node")  # 支持通过节点名称获取
            var_names = source_config.get("var_names", [])  # 支持多变量选择


            if not chain_results:
                return "", True

            # 确定要尝试的索引
            target_idx = None

            # 如果配置了节点名称，先通过节点名称找到对应的索引
            if target_idx is None and chain_node:
                all_names = [(idx, cr.get("name", "")) for idx, cr in enumerate(chain_results)]
                for idx, cr in enumerate(chain_results):
                    cr_name = cr.get("name", "")
                    # 支持模糊匹配："预处理" 匹配 "预处理"，"生成测试用例" 匹配 "【生成测试用例】"
                    if chain_node in cr_name or cr_name in chain_node:
                        target_idx = idx
                        logger.info(f"通过chain_node={chain_node}匹配到对应索引: {idx}, name={cr_name}")
                        break

            # 如果配置了 order，通过 order 找到对应的索引
            if target_idx is None and chain_order is not None:
                for idx, cr in enumerate(chain_results):
                    if cr.get("order") == chain_order:
                        target_idx = idx
                        break

            # 如果没有通过 order 找到，使用配置的 chain_index
            if target_idx is None and chain_index is not None:
                # 确保 chain_index 是整数类型（前端可能传字符串）
                try:
                    chain_index = int(chain_index)
                except (ValueError, TypeError):
                    logger.warning(f"chain_index={chain_index}无法转换为整数")
                    chain_index = None
                if chain_index is not None:
                    target_idx = chain_index - 1
            # 如果都没有配置，尝试通过 var_names 找到包含这些变量的请求索引
            if target_idx is None and var_names:
                # 打印所有 chain_result 的信息供调试
                all_info = []
                for idx, cr in enumerate(chain_results):
                    variables = cr.get("variables", {})
                    vars_list = list(variables.keys())
                    all_info.append((idx, cr.get("name", ""), vars_list))

                # 首先尝试通过请求名称匹配（当 var_names 包含请求名称时）
                for idx, cr in enumerate(chain_results):
                    cr_name = cr.get("name", "")
                    for var_name in var_names:
                        if var_name in cr_name or cr_name in var_name:
                            target_idx = idx
                            logger.info(f"通过请求名称匹配找到索引: {idx}, name={cr_name}, var_name={var_name}")
                            break
                    if target_idx is not None:
                        break

                # 如果名称匹配失败，尝试通过变量名查找（var_names 中的名称是否是某个请求的输出变量）
                if target_idx is None:
                    for idx, cr in enumerate(chain_results):
                        variables = cr.get("variables", {})
                        # 检查 var_names 中的任何一个是否在这个请求的 variables 中
                        for var_name in var_names:
                            if var_name in variables:
                                target_idx = idx
                                logger.info(f"通过变量名匹配找到索引: {idx}, name={cr.get('name', '')}, var_name={var_name}")
                                break
                        if target_idx is not None:
                            break

            # 如果都没有配置，默认使用第一个
            if target_idx is None:
                target_idx = 0
                logger.info(f"未配置chain_index或order且name匹配失败，默认使用索引0")

            # 获取目标请求的结果
            if target_idx >= len(chain_results):
                logger.warning(f"chain_index={target_idx}超出范围，有效范围: 0-{len(chain_results)-1}")
                return "", True

            chain_result = chain_results[target_idx]
            # 打印 variables 中的非输入变量（可能是输出）
            variables = chain_result.get("variables", {})
            output_vars = {k: v[:100] + "..." if isinstance(v, str) and len(v) > 100 else v
                          for k, v in variables.items()
                          if k not in ["messages", "endpoint", "model_type", "model_id", "api_key", "cookie",
                                       "document_url", "project_id", "project_name", "accessToken", "session_id"]}

            # 新结构：优先从 variables 获取（多变量模式）
            found_in_variables = False
            if "variables" in chain_result:
                variables = chain_result["variables"]
                # 如果配置了 var_names，返回多个变量拼接
                if var_names:
                    values = []
                    for var_name in var_names:
                        val = variables.get(var_name, "")
                        if val:
                            values.append(f"{var_name}: {val}")
                            found_in_variables = True
                    if values:
                        value = "\n".join(values)
                        logger.info(f"从variables中获取{var_names}: {len(value) if value else 0}字符")
                        return value, True
                # 如果没有配置 var_names，尝试返回 output_var 对应的值
                output_var = chain_result.get("output_var", "")
                if output_var and output_var in variables:
                    value = variables[output_var]
                    return value, True

            # 旧结构：从 all_results 获取
            if "all_results" in chain_result:
                all_results = chain_result["all_results"]
                if isinstance(all_results, dict):
                    # 如果配置了 var_names，返回多个变量拼接
                    if var_names:
                        values = []
                        for var_name in var_names:
                            val = all_results.get(var_name, "")
                            if val:
                                values.append(f"{var_name}: {val}")
                        if values:
                            value = "\n".join(values)
                            return value, True
                    # 如果没有配置 var_names，返回 output_var 对应的值
                    output_var = chain_result.get("output_var", "")
                    if output_var and output_var in all_results:
                        value = all_results[output_var]
                        return value, True

            # 备选：当 var_names 中的名称不是 variables 中的变量时，直接使用 content（节点名称模式）
            if var_names and not found_in_variables:
                content = chain_result.get("content", "")
                if content:
                    return content, True

            # 备选：从 content 获取
            value = chain_result.get("content", "")
            if value:
                return value, True

            return "", True

        elif source == "dataset_field":
            # 从数据集字段获取，支持多字段组合
            # 支持两种格式：
            # 1. source_config.field: 单字段名
            # 2. source_config.fields: 字段数组，多字段值用换行拼接
            fields_list = source_config.get("fields", [])
            field = source_config.get("field")

            # 如果配置了 fields 数组，支持多字段组合
            if fields_list and isinstance(fields_list, list):
                values = []
                for f in fields_list:
                    val = self._get_field_value_from_payload(f, payload)
                    if val:
                        values.append(str(val))
                value = "\n".join(values) if values else ""
                logger.info(f"dataset_field多字段解析: fields={fields_list}, 获取字符数={len(value)}")
                return value, True

            # 单字段模式（原有逻辑）

            # 根据 field 从 payload 获取对应的值
            value = self._get_field_value_from_payload(field, payload, dataset_item=payload.get("_dataset_item", {}))
            return value, True

        elif source == "model_output":
            # 模型输出
            return model_output, False

        elif source == "manual":
            # 从手动输入的变量获取
            var_name_key = var_config.get("name", "")
            runtime_variables = getattr(self, "runtime_variables", None) or (self.task.runtime_variables if self.task else {}) or {}
            logger.info(f"manual解析: var_name={var_name_key}, runtime_variables keys: {list(runtime_variables.keys())}")

            if var_name_key in runtime_variables:
                var_data = runtime_variables[var_name_key]
                # 支持多种格式：直接值或 {value: xxx, type: xxx}
                if isinstance(var_data, dict):
                    value = var_data.get("value", "")
                    logger.info(f"从manual获取{var_name_key}: {len(value) if value else 0}字符")
                    return value, True
                logger.info(f"从manual获取{var_name_key}: {len(str(var_data)) if var_data else 0}字符")
                return str(var_data), True
            logger.warning(f"manual变量 {var_name_key} 在runtime_variables中未找到")
            return "", True

        elif source == "script_output":
            # 前处理脚本输出（设计文档 §三/§七.2）：VariableInjector 已把脚本完整输出
            # 注入 payload["script_outputs"]，此处按 {script, key} 取值。
            # 约定：script/key 优先位于 source_config（list/dict 格式通用）；
            # dict 格式兼容顶层写法（设计文档 §三 示例）。list 格式顶层 key 是变量名，
            # 仅当 script 配置在 source_config 时顶层 key 才不会参与解析（函数内防呆）。
            from app.services.variable_injector import resolve_script_output_value

            script_outputs = payload.get("script_outputs") or {}
            var_key = var_config.get("key") or var_config.get("name") or ""
            value = resolve_script_output_value(script_outputs, var_config, var_key)
            return value, True

        logger.warning(f"未知的source类型: {source}")
        return "", True

    def _get_field_value_from_payload(
        self,
        field: str,
        payload: Dict[str, Any],
        dataset_item: Dict[str, Any] = None,
    ) -> str:
        """从 payload 中获取字段值，支持多字段和别名

        Args:
            field: 字段名
            payload: 请求 payload
            dataset_item: 可选的 dataset_item 字典

        Returns:
            字段值
        """
        if not field:
            return ""

        # 如果 field 是常见的关键字段名，直接从 payload 获取
        field_lower = field.lower() if field else ""

        if field_lower in ["user_input", "用户输入", "question", "问题"]:
            return payload.get("user_input", "") or payload.get("question", "")

        if field_lower in ["answer", "ref_answer", "参考答案"]:
            return payload.get("ref_answer", "") or payload.get("answer", "")

        # 尝试从 payload 获取
        value = payload.get(field, "")
        if value:
            return value

        # 尝试从 _dataset_item 获取
        if dataset_item is None:
            dataset_item = payload.get("_dataset_item", {})

        if isinstance(dataset_item, dict):
            value = dataset_item.get(field, "")
            if value:
                return value

            # V2 数据格式：字段可能嵌套在 data 键下
            data = dataset_item.get("data", {})
            if isinstance(data, dict):
                value = data.get(field, "")
                if value:
                    return str(value)

            # 尝试常见替代字段名（含 V2 data 嵌套）
            alt_fields = ["answer", "refAnswer", "reference_answer", "answer_content", "ref_answer"]
            for alt_field in alt_fields:
                alt_value = dataset_item.get(alt_field, "")
                if not alt_value and isinstance(data, dict):
                    alt_value = data.get(alt_field, "")
                if alt_value:
                    return str(alt_value)

        return ""

    def _resolve_variable_value(
        self,
        var_config: Dict[str, Any],
        payload: Dict[str, Any],
        model_output: str,
    ) -> str:
        """
        解析变量值

        Args:
            var_config: 变量配置
            payload: 请求Payload
            model_output: 模型输出

        Returns:
            变量值
        """
        key = var_config.get("key")
        source = var_config.get("source", "runtime_variable")
        source_config = var_config.get("source_config", {})

        if source == "runtime_variable" or source == "manual":
            # 从 runtime_variables 或直接 payload 中获取
            value = payload.get(key, "")
            # 处理 { value, type } 格式
            if isinstance(value, dict) and "value" in value:
                return value.get("value", "")
            return value

        elif source == "dataset_field":
            # 从数据集字段获取
            field = source_config.get("field", key)
            dataset_item = payload.get("_dataset_item", {})
            if isinstance(dataset_item, dict):
                value = dataset_item.get(field, "")
                if value:
                    return str(value)
                # V2 数据格式：字段可能嵌套在 data 键下
                data = dataset_item.get("data", {})
                if isinstance(data, dict):
                    value = data.get(field, "")
                    if value:
                        return str(value)
            return ""

        elif source == "model_output":
            # 模型输出
            return model_output

        return ""

    def _inject_judge_variables(
        self,
        prompt_type: str,
        payload: Dict[str, Any],
        model_output: str,
    ) -> str:
        """
        注入裁判变量到提示词

        Args:
            prompt_type: 提示词类型 (system 或 user)
            payload: 请求Payload
            model_output: 模型输出

        Returns:
            格式化后的变量字符串
        """
        if not self.judge_variable_config:
            return ""

        injected_parts = []

        for var_config in self.judge_variable_config:
            # 确保 var_config 是字典类型
            if not isinstance(var_config, dict):
                continue

            # 检查变量应该放在哪个提示词中
            if var_config.get("prompt_type") != prompt_type:
                continue

            key = var_config.get("key")
            name = var_config.get("name", key)

            # 跳过内置变量（user_input, ref_answer, model_answer）
            if key in ["user_input", "ref_answer", "model_answer"]:
                continue

            # 解析变量值
            value = self._resolve_variable_value(var_config, payload, model_output)

            if not value:
                continue

            # 检查 prompt_format 类型
            prompt_format = var_config.get("prompt_format", "text")

            if prompt_format == "json":
                # JSON 格式：尝试格式化
                try:
                    if isinstance(value, str):
                        json_obj = json.loads(value)
                    else:
                        json_obj = value
                    formatted_value = json.dumps(json_obj, ensure_ascii=False, indent=2)
                    injected_parts.append(f"### {name}\n```json\n{formatted_value}\n```")
                except (json.JSONDecodeError, TypeError):
                    injected_parts.append(f"### {name}\n{value}")
            else:
                # 文本格式
                injected_parts.append(f"### {name}\n{value}")

        return "\n\n".join(injected_parts)

    def _find_model_output_var_name(self) -> Optional[str]:
        """从 judge_variable_config 中查找 source=model_output 的变量显示名称。

        Returns:
            变量的 name 字段（用于在 user_prompt 中定位），如果未找到则返回 None
        """
        if not self.judge_variable_config:
            return None
        # 字典格式
        if isinstance(self.judge_variable_config, dict):
            for var_name, var_config in self.judge_variable_config.items():
                if isinstance(var_config, dict) and var_config.get("source") == "model_output":
                    return var_config.get("name", var_name)
        # 列表格式
        elif isinstance(self.judge_variable_config, list):
            for var_config in self.judge_variable_config:
                if isinstance(var_config, dict) and var_config.get("source") == "model_output":
                    return var_config.get("name", var_config.get("key", ""))
        return None

    def _llm_compress_text(self, text: str, protect_model_output: bool = True) -> str:
        """
        使用LLM对文本进行摘要压缩，可保护 model_output 部分不被压缩。

        当 protect_model_output=True 时：
        1. 识别 user_prompt 中 model_output 对应的标记段（【变量名】...）
        2. 只压缩参考内容部分，保留 model_output 原文
        3. 重新拼接后返回

        Args:
            text: 待压缩的文本
            protect_model_output: 是否保护 model_output 段不被压缩（默认 True）

        Returns:
            压缩后的文本，如果压缩失败则返回原始文本
        """
        import logging
        logger = logging.getLogger(__name__)

        if not text:
            return text

        # 尝试找到 model_output 变量名，识别并保护对应段落
        model_output_segment = None
        ref_part = text
        suffix_part = ""

        if protect_model_output:
            model_output_var_name = self._find_model_output_var_name()
            if model_output_var_name:
                marker = f"【{model_output_var_name}】"
                marker_pos = text.find(marker)
                if marker_pos != -1:
                    ref_part = text[:marker_pos]
                    suffix_part = text[marker_pos:]
                    model_output_segment = suffix_part
                    logger.info(
                        f"[prompt压缩] 识别到 model_output 段（变量名: {model_output_var_name}）"
                        f" —— 参考部分 {len(ref_part)} 字符，model_output 部分 {len(suffix_part)} 字符（不压缩）"
                    )
                else:
                    logger.info(f"[prompt压缩] 未在 user_prompt 中找到标记【{model_output_var_name}】，对全文压缩")

        # 若参考部分为空或很短，直接返回（无需压缩）
        if not ref_part.strip():
            return text

        try:
            from app.services.judge_client import JudgeClient

            judge_client = JudgeClient(self.task)

            compress_system_prompt = "你是一个文本压缩助手，负责将长文本精简为简短摘要，保留核心信息。"
            compress_user_prompt = f"""请将以下参考内容进行精简压缩，保留核心信息和关键内容，大幅减少字符数量。
压缩后的内容应保持语义完整性，便于理解原意。

待压缩内容：
---
{ref_part}
---

请直接输出压缩后的内容，不要添加任何解释或前缀。"""

            result = judge_client.compress(compress_system_prompt, compress_user_prompt)
            compressed_ref = result.strip() if result else ""

            if not compressed_ref:
                logger.warning("LLM压缩未返回有效内容，回退到原始文本")
                return text

            final_text = compressed_ref + ("\n\n" + suffix_part if suffix_part else "")
            logger.info(
                f"LLM压缩成功: 原始{len(text)}字符 -> 压缩后{len(final_text)}字符"
                + (f"（model_output 部分 {len(suffix_part)} 字符已保护）" if model_output_segment else "")
            )
            return final_text

        except Exception as e:
            logger.warning(f"LLM压缩失败: {e}，回退到原始文本")
            return text

    def build_user_prompt_from_judge_vars(
        self,
        payload: Dict[str, Any],
        model_output: str,
        chain_results: list = None,
        compress_threshold: int = 150000,
    ) -> str:
        """
        根据 judge_variables 配置构建 user_prompt

        步骤：
        1. 根据每个字段的来源配置获取对应值
        2. 对每个值进行上下文压缩（超过阈值时）
        3. 使用每个字段的name和压缩后值进行user_prompt构建
        4. 输出最后的user_prompt

        Args:
            payload: 请求Payload
            model_output: 模型输出
            chain_results: 请求链结果列表
            compress_threshold: 压缩阈值，默认20000字符

        Returns:
            格式化后的 user_prompt
        """
        import logging
        logger = logging.getLogger(__name__)

        # judge_variable_config 可以是列表格式或字典格式
        judge_vars = self.judge_variable_config
        logger.info(f"judge_variable_config: {judge_vars}")

        if not judge_vars:
            logger.warning("judge_variable_config为空")
            return ""

        user_vars = []

        if isinstance(judge_vars, list):
            # 列表格式: [{"key": "model_out", "name": "模型输出", "source": "model_output", "prompt_type": "user"}, ...]
            for item in judge_vars:
                if not isinstance(item, dict):
                    continue

                # 只处理 prompt_type="user" 的字段
                if item.get("prompt_type") != "user":
                    continue

                # 获取变量标识符和显示名
                var_key = item.get("key", "")
                name = item.get("name", var_key)

                # 步骤1: 获取变量值
                value, need_compress = self._resolve_judge_variable(
                    var_config=item,
                    payload=payload,
                    model_output=model_output,
                    chain_results=chain_results,
                )

                if not value:
                    logger.warning(f"变量 {var_key} 获取值为空，跳过")
                    continue

                # 步骤2: 如果超过阈值，进行上下文压缩
                if need_compress and isinstance(value, str) and len(value) > compress_threshold:
                    logger.info(f"变量 {name} 长度({len(value)})超过阈值({compress_threshold})，进行LLM压缩")
                    value = self._llm_compress_text(value)

                user_vars.append({
                    "name": name,
                    "value": value,
                })

        elif isinstance(judge_vars, dict):
            # 字典格式: {"var_name": {"name": "显示名", "source": "model_output", "prompt_type": "user"}, ...}
            for var_name, var_config in judge_vars.items():
                if not isinstance(var_config, dict):
                    continue

                # 只处理 prompt_type="user" 的字段
                if var_config.get("prompt_type") != "user":
                    continue

                # 获取变量名（用于显示）
                name = var_config.get("name", var_name)

                # 步骤1: 获取变量值
                value, need_compress = self._resolve_judge_variable(
                    var_config=var_config,
                    payload=payload,
                    model_output=model_output,
                    chain_results=chain_results,
                )

                if not value:
                    logger.warning(f"变量 {var_name} 获取值为空，跳过") # TODO: 需要处理这种情况
                    continue

                # 步骤2: 如果超过阈值，进行上下文压缩
                if need_compress and isinstance(value, str) and len(value) > compress_threshold:
                    logger.info(f"变量 {name} 长度({len(value)})超过阈值({compress_threshold})，进行LLM压缩")
                    value = self._llm_compress_text(value)

                user_vars.append({
                    "name": name,
                    "value": value,
                })
        else:
            logger.warning("judge_variable_config格式不支持，需为列表或字典")
            return ""

        if not user_vars:
            return ""

        # 步骤3: 构建 user_prompt，格式：【name】value，多个变量之间双换行分隔
        prompt_parts = []
        for var in user_vars:
            prompt_parts.append(f"【{var['name']}】{var['value']}")

        user_prompt = "\n\n".join(prompt_parts)
        logger.info(f"构建 user_prompt 成功: {len(user_vars)} 个变量，总长度 {len(user_prompt)} 字符")

        return user_prompt

    def build_judge_prompts(
        self,
        payload: Dict[str, Any],
        model_output: str,
        chain_results: list = None,
        llm_compress_threshold: int = 150000,
    ) -> tuple[str, str]:
        """
        构建裁判提示词

        Args:
            payload: 原始请求Payload
            model_output: 模型输出
            chain_results: 请求链结果列表
            llm_compress_threshold: LLM压缩触发阈值（默认150000字符）

        Returns:
            (系统提示词, 用户提示词)
        """
        import logging
        logger = logging.getLogger(__name__)

        user_input = payload.get("user_input", "")
        ref_answer = payload.get("ref_answer", "")
        dataset_item = payload.get("_dataset_item", {})
        chain_results = chain_results or []

        # 构建系统提示词
        system_prompt = self._build_system_prompt(
            user_input=user_input,
            ref_answer=ref_answer,
            model_output=model_output,
            dataset_item=dataset_item,
            payload=payload,
            chain_results=chain_results,
        )

        # 构建用户提示词
        user_prompt = self._build_user_prompt(
            user_input=user_input,
            ref_answer=ref_answer,
            model_output=model_output,
            dataset_item=dataset_item,
            payload=payload,
            chain_results=chain_results,
        )

        # 检查是否需要LLM压缩
        if len(user_prompt) > llm_compress_threshold:
            logger.info(f"user_prompt长度({len(user_prompt)})超过阈值({llm_compress_threshold})，触发LLM压缩")
            user_prompt = self._llm_compress_text(user_prompt)

        return system_prompt, user_prompt

    def _inject_evaluation_targets(
        self,
        payload: Dict[str, Any],
        model_output: str,
        chain_results: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """将 evaluation_targets 解析并注入到系统提示词

        Args:
            payload: 请求 payload
            model_output: 模型输出
            chain_results: 请求链结果列表

        Returns:
            解析后的 evaluation_targets 内容，如果无配置则返回空字符串
        """
        import logging
        logger = logging.getLogger(__name__)


        if not self.evaluation_targets:
            logger.warning(f"[_inject_evaluation_targets] evaluation_targets 为空，跳过注入")
            return ""

        try:
            from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget

            resolver = JudgeVariableResolver(
                task_config={},
                dataset_item=payload.get("_dataset_item", {}),
                model_result={"content": model_output},
                chain_results=chain_results or []
            )

            targets = [EvaluationTarget(**t) for t in self.evaluation_targets]

            resolved_targets = resolver.resolve_targets(targets)


            if not resolved_targets:
                logger.info(f"[_inject_evaluation_targets] resolved_targets 为空")
                return ""

            # 构建评测目标内容
            target_parts = []
            for target_info in resolved_targets.values():
                name = target_info.get("name", "")
                content = target_info.get("content", "")
                is_empty = target_info.get("is_empty", False)

                if content and not is_empty:
                    target_parts.append(f"### {name}\n{content}")

            if target_parts:
                result = "\n\n".join(target_parts)
                return result
            else:
                logger.info(f"[_inject_evaluation_targets] 没有有效内容可注入")

        except Exception as e:
            logger.warning(f"[_inject_evaluation_targets] 解析 evaluation_targets 失败: {e}")

        return ""

    def _build_system_prompt(self, **kwargs) -> str:
        """构建系统提示词"""
        import logging
        logger = logging.getLogger(__name__)

        parts = []

        payload = kwargs.get("payload")
        model_output = kwargs.get("model_output")
        chain_results = kwargs.get("chain_results")

        logger.info(f"[_build_system_prompt] 开始构建 | "
                    f"evaluation_targets={self.evaluation_targets} | "
                    f"has_payload={bool(payload)} | "
                    f"chain_results数量={len(chain_results) if chain_results else 0}")

        # 添加场景描述
        if self.scene_desc:
            parts.append(f"## 场景描述\n{self.scene_desc}")

        # 注入评测目标内容（evaluation_targets）
        if self.evaluation_targets:
            targets_content = self._inject_evaluation_targets(
                payload=payload,
                model_output=model_output,
                chain_results=chain_results,
            )
            if targets_content:
                parts.append(f"\n## 待评测目标内容\n{targets_content}")
        else:
            logger.info(f"[_build_system_prompt] evaluation_targets 为空，跳过注入")

        parts.append("\n\n##规范\n请严格根据评测维度要求对待评测目标内容进行评测，并按维度顺序逐步分析并评分。")

        # 添加评测维度（有序步骤形式：维度顺序即评分步骤，契约 v3 §四）
        if self.evaluation_metrics:
            parts.append("\n## 评测维度")
            for i, metric in enumerate(self.evaluation_metrics, 1):
                metric_name = metric.get("name", "")
                metric_desc = metric.get("description", "")
                weight = metric.get("weight", 1)

                if isinstance(weight, str):
                    weight_num = WEIGHT_MAP.get(weight, 1)
                    weight_label = f"{weight}({weight_num})"
                else:
                    weight_num = weight
                    weight_label = str(weight)

                if metric_name:
                    parts.append(f"\n### 第{i}步 {metric_name} (权重: {weight_label})")
                    if metric_desc:
                        parts.append(metric_desc)

        # 添加评分标准
        if self.score_details:
            parts.append(f"\n## 评分标准\n{self.score_details}")

        # 添加输出格式要求
        if self.evaluation_metrics:
            # 有评测维度时，要求返回各维度独立评分
            dim_example_lines = []
            for metric in self.evaluation_metrics:
                metric_name = metric.get("name", "")
                if metric_name:
                    dim_example_lines.append(f'    "{metric_name}": <该维度得分(0-{self.max_score})>')
            dim_block = ",\n".join(dim_example_lines)
            output_format = (
                f"\n## 输出格式要求\n"
                f"请严格按照以下格式输出评分结果：\n\n"
                f"1. 首先在 <think> 标签中写出你的分析过程（可选）\n"
                f"2. 然后在 ```json 代码块中输出结构化结果\n\n"
                f"### 正确格式示例\n\n"
                f"```json\n"
                f"{{\n"
                f'  "score": 8.5,\n'
                f'  "dimension_scores": {{\n'
                f"{dim_block}\n"
                f"  }},\n"
                f'  "feedback": "该测试用例覆盖了核心功能路径，边界条件测试充分，..."\n'
                f"}}\n```\n\n"
                f"### 错误格式示例（请勿模仿）\n\n"
                f"1. 思考过程未分离：\n"
                f"```\n"
                f"我认为这个测试用例质量不错，评分如下：score: 8.5\n"
                f"```\n"
                f"2. Markdown包裹位置错误：\n"
                f"```\n"
                f"以下是评分结果：\n"
                f"```json\n"
                f"{{\"score\": 8.5}}\n"
                f"```\n"
                f"请直接输出 ```json 代码块，不要有其他前缀文字。\n\n"
                f"### 重要提醒\n"
                f"- 只输出 ```json 代码块，不要输出 ``` 包裹的其他内容\n"
                f"- 确保JSON格式正确，可以被 json.loads 解析\n"
                f"- score 必须是 0-{self.max_score} 之间的数字\n"
                f"- dimension_scores 中的每个维度分数必须是 0-{self.max_score} 之间的数字\n"
                f"- 综合评分将由系统按权重自动计算，无需你手动计算\n"
                f"- 请确保每个维度的评分准确，权重越高(数值越大)的维度对综合评分影响更大"
            )
        else:
            output_format = (
                f"\n## 输出格式要求\n"
                f"请严格按照以下格式输出评分结果：\n\n"
                f"1. 首先在 <think> 标签中写出你的分析过程（可选）\n"
                f"2. 然后在 ```json 代码块中输出结构化结果\n\n"
                f"### 正确格式示例\n\n"
                f"```json\n"
                f"{{\n"
                f'  "score": 8.5,\n'
                f'  "feedback": "该测试用例覆盖了核心功能路径，..."\n'
                f"}}\n```\n\n"
                f"### 错误格式示例（请勿模仿）\n\n"
                f"1. 思考过程未分离：\n"
                f"```\n"
                f"我认为这个测试用例质量不错，评分如下：score: 8.5\n"
                f"```\n"
                f"2. 直接输出文本而非JSON：\n"
                f"```\n"
                f"评分：8.5\n"
                f"理由：...\n"
                f"```\n"
                f"请直接输出 ```json 代码块，不要有其他前缀文字。\n\n"
                f"### 重要提醒\n"
                f"- 只输出 ```json 代码块，不要输出 ``` 包裹的其他内容\n"
                f"- 确保JSON格式正确，可以被 json.loads 解析\n"
                f"- score 必须是 0-{self.max_score} 之间的数字"
            )
        parts.append(output_format)

        # 注入裁判变量配置（system类型）
        payload = kwargs.get("payload", {})
        model_output = kwargs.get("model_output", "")
        chain_results = kwargs.get("chain_results", [])
        system_vars = self._inject_judge_variables(
            prompt_type="system",
            payload=payload,
            model_output=model_output,
        )
        if system_vars:
            parts.append(f"\n## 自定义配置\n{system_vars}")
        
        return "\n".join(parts)

    def _compress_text(self, text: str) -> str:
        """
        压缩文本：移除多余空白字符

        - 移除连续空行（多个空行合并为一个）
        - 移除行首行尾多余空格
        - 压缩连续空白字符（多个空格合并为一个）
        """
        if not text:
            return ""

        import re
        # 移除行首行尾空格
        lines = [line.strip() for line in text.split('\n')]
        # 移除空行
        lines = [line for line in lines if line]
        # 合并多空格为单空格
        lines = [re.sub(r'\s+', ' ', line) for line in lines]
        return '\n'.join(lines)

    def _build_user_prompt(self, **kwargs) -> str:
        """构建用户提示词"""
        parts = []

        payload = kwargs.get("payload", {})
        model_output = kwargs.get("model_output", "")
        chain_results = kwargs.get("chain_results", [])

        # 遍历 judge_variable_config 中 prompt_type="user" 的字段
        judge_vars = self.judge_variable_config
        if not judge_vars:
            return ""

        user_vars = []

        if isinstance(judge_vars, list):
            for item in judge_vars:
                if not isinstance(item, dict):
                    continue
                if item.get("prompt_type") != "user":
                    continue

                var_key = item.get("key", "")
                name = item.get("name", var_key)

                # 使用 _resolve_judge_variable 获取变量值
                value, _ = self._resolve_judge_variable(
                    var_config=item,
                    payload=payload,
                    model_output=model_output,
                    chain_results=chain_results,
                )

                if value or value == "":
                    user_vars.append({
                        "name": name,
                        "value": value,
                    })

        elif isinstance(judge_vars, dict):
            for var_name, var_config in judge_vars.items():
                if not isinstance(var_config, dict):
                    continue
                if var_config.get("prompt_type") != "user":
                    continue

                name = var_config.get("name", var_name)

                # 使用 _resolve_judge_variable 获取变量值
                value, _ = self._resolve_judge_variable(
                    var_config=var_config,
                    payload=payload,
                    model_output=model_output,
                    chain_results=chain_results,
                )

                if value:
                    user_vars.append({
                        "name": name,
                        "value": value,
                    })

        # 构建 user prompt，格式：【name】value
        for var in user_vars:
            parts.append(f"【{var['name']}】{var['value']}")

        return "\n".join(parts)

    def build_consistency_prompts(
        self,
        outputs: list[str],
    ) -> tuple[str, str]:
        """
        构建一致性评测提示词

        Args:
            outputs: 待比较的多个模型输出

        Returns:
            (系统提示词, 用户提示词)
        """
        system_prompt = """你是一个专业的AI评测专家，负责评估多个模型输出的一致性。

请分析以下多个模型对同一问题的回答，评估它们的一致性程度。

一致性评估标准：
- 高一致性：回答内容核心观点一致，仅在表达方式上有差异
- 中等一致性：回答有一定相似度，但在关键点上存在差异
- 低一致性：回答存在明显矛盾或完全不同
"""

        user_prompt = f"## 待评测输出\n"
        for i, output in enumerate(outputs, 1):
            user_prompt += f"\n### 输出 {i}\n{output}\n"

        user_prompt += f"\n请对这些输出进行一致性评测，并给出评分(0-{self.max_score})和详细反馈。"

        return system_prompt, user_prompt
