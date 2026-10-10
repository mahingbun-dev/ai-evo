"""
变量注入服务

负责将数据集字段和运行时变量注入到Prompt模板中，生成完整的请求Payload
"""
import json
import re
from typing import Dict, Any, Optional, List


def resolve_script_output_value(
    script_outputs: Dict[str, Any],
    config: Dict[str, Any],
    var_name: str,
    allow_top_level_key: bool = True,
) -> str:
    """从 pre_judge_runner 的脚本输出中按 {script, key} 取变量值（设计文档 §三 输出消费三通道）

    取值位置：优先 source_config {"script": 输出键名, "key": 输出内键名}；
    dict 格式的 judge_variable_config 兼容顶层 {"script": ..., "key": ...} 写法
    （设计文档 §三 示例格式）。list 格式顶层 "key" 是变量名本身，
    不能当作脚本输出键（allow_top_level_key=False）。

    key 缺省时整体取该脚本的 output（整体取，见设计文档 §三）。
    缺值置空并记录警告，不阻断裁判评测。

    Returns:
        变量值（dict/list 序列化为 JSON 字符串，bool 转小写，None 置空）
    """
    import logging
    logger = logging.getLogger(__name__)

    source_config = config.get("source_config") or {}
    script_from_source = source_config.get("script")
    script_name = script_from_source or config.get("script")
    output_key = source_config.get("key")
    if output_key is None and allow_top_level_key and not script_from_source:
        # dict 格式顶层写法（设计文档 §三 示例）：script/key 均位于 config 顶层时，
        # 顶层 key 才作为脚本输出键；script 已在 source_config 配置时不再回落，
        # 避免 list 格式顶层 key（变量名本身）被误当作输出键
        output_key = config.get("key")

    if not script_name:
        logger.warning(f"[script_output] 变量 {var_name} 未配置 script（输出键名），置空")
        return ""
    script_output = (script_outputs or {}).get(script_name)
    if script_output is None:
        logger.warning(f"[script_output] 变量 {var_name} 引用的脚本输出 {script_name} 不存在（脚本失败或未编排），置空")
        return ""
    if output_key in (None, ""):
        value = script_output  # 整体取
    elif isinstance(script_output, dict) and output_key in script_output:
        value = script_output[output_key]
    else:
        logger.warning(f"[script_output] 脚本输出 {script_name} 缺少键 {output_key}，变量 {var_name} 置空")
        return ""

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


class VariableInjector:
    """变量注入器"""

    # Chain 变量引用正则表达式
    # {{alias.var_name}} - 通过请求别名引用，如 {{login.session_id}}
    CHAIN_ALIAS_PATTERN = re.compile(r'\{\{(\w+)\.(\w+)\}\}')

    # {{chain[0].var_name}} - 通过索引引用，如 {{chain[0].session_id}}
    CHAIN_INDEX_PATTERN = re.compile(r'\{\{chain\[(\d+)\]\.(\w+)\}\}')

    def __init__(
        self,
        task,
        chain_results: Optional[List[Dict[str, Any]]] = None,
        all_variables: Optional[Dict[str, Any]] = None,
        script_outputs: Optional[Dict[str, Any]] = None,
    ):
        """
        初始化变量注入器

        Args:
            task: 评测任务对象
            chain_results: 请求链结果列表，每个元素包含请求的 variables
            all_variables: 请求链汇总的所有变量
            script_outputs: 前处理脚本输出 {output_var或脚本名: output dict}（pre_judge_runner 产出）
        """
        self.task = task
        self.runtime_variables = task.runtime_variables or {}
        self.variable_mappings = task.variable_mappings or {}
        self.variable_sources = task.variable_sources or {}
        self.enable_user_import = task.enable_user_import or False
        self.user_import_template = task.user_import_template
        self.chain_results = chain_results or []
        self.all_variables = all_variables or {}
        self.script_outputs = script_outputs or {}

        # V2 字段映射配置（支持自由数据集字段）
        self.input_field = getattr(task, 'input_field', 'user_input') or 'user_input'
        self.reference_field = getattr(task, 'reference_field', 'answer') or 'answer'

    def generate_payload(self, dataset_item: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成完整的请求Payload

        Args:
            dataset_item: 数据集单条数据

        Returns:
            包含所有变量的Payload字典
        """
        import logging
        logger = logging.getLogger(__name__)


        payload = {}

        # 获取 dataset_item 的原始数据（可能是 EvaluationData 对象）
        raw_item = dataset_item if isinstance(dataset_item, dict) else {}

        # 处理用户输入
        user_input = self._get_variable_value("user_input", dataset_item)
        # 如果 user_input_is_media 为 True，读取文件内容
        # 尝试从多个位置获取: dataset_item, meta_data, _dataset_item
        user_input_is_media = (
            dataset_item.get("user_input_is_media") if isinstance(dataset_item, dict) else getattr(dataset_item, "user_input_is_media", None)
        ) or (
            raw_item.get("meta_data", {}).get("user_input_is_media") if raw_item.get("meta_data") else None
        ) or (
            dataset_item.get("_dataset_item", {}).get("user_input_is_media") if isinstance(dataset_item, dict) and dataset_item.get("_dataset_item") else None
        )
        if user_input_is_media and str(user_input_is_media).lower() in ["是", "yes", "true", "1"]:
            user_input = self._process_media_content(user_input, dataset_item, field="user_input")
        payload["user_input"] = user_input

        # 处理参考答案
        ref_answer = self._get_variable_value("ref_answer", dataset_item)
        # 如果 answer_is_media 为 True，读取文件内容
        answer_is_media = (
            dataset_item.get("answer_is_media") if isinstance(dataset_item, dict) else getattr(dataset_item, "answer_is_media", None)
        ) or (
            raw_item.get("meta_data", {}).get("answer_is_media") if raw_item.get("meta_data") else None
        ) or (
            dataset_item.get("_dataset_item", {}).get("answer_is_media") if isinstance(dataset_item, dict) and dataset_item.get("_dataset_item") else None
        )
        if answer_is_media and str(answer_is_media).lower() in ["是", "yes", "true", "1"]:
            ref_answer = self._process_media_content(ref_answer, dataset_item)
        payload["ref_answer"] = ref_answer

        # 处理模型输出（待填充）
        payload["model_answer"] = None

        # 处理其他运行时变量（含数据集字段型变量）
        # 收集所有需要处理的变量名（来自 runtime_variables / variable_sources / variable_mappings）
        all_var_names = set(self.runtime_variables.keys()) | set(self.variable_sources.keys()) | set(self.variable_mappings.keys())
        for var_name in all_var_names:
            payload[var_name] = self._get_variable_value(var_name, dataset_item)

        # 处理多字段的 is_media 标志
        # 遍历 dataset_item 中所有多字段（如 user_input_2, answer_2），检查是否有对应的 is_media 标志
        self._process_multi_field_media(payload, dataset_item)

        # 处理user_import格式
        if self.enable_user_import and self.user_import_template:
            payload["user_import"] = self._format_user_import(
                user_input,
                dataset_item,
            )

        # 添加数据集原始数据（用于后续裁判）
        payload["_dataset_item"] = dataset_item

        # ====== 处理 runtime_variables 中的 { value, type } 格式 ======
        # 与模型配置测试连通性保持一致，正确转换类型
        self._process_runtime_variables(payload)

        return payload

    def _process_multi_field_media(self, payload: Dict[str, Any], dataset_item: Dict[str, Any]) -> None:
        """处理多字段的 is_media 标志

        遍历 dataset_item 中的多字段（如 user_input_2, answer_2），
        检查是否有对应的 xxx_is_media 标志，如果有则处理媒体内容。
        """
        # 收集所有需要检查的字段前缀
        prefixes = ["user_input_", "answer_"]

        for key, value in dataset_item.items():
            if not value or not isinstance(value, str):
                continue

            for prefix in prefixes:
                if key.startswith(prefix) and not key.endswith("_is_media"):
                    # 这是一个多字段（如 user_input_2, answer_2）
                    # 检查对应的 is_media 字段
                    is_media_key = key + "_is_media"
                    is_media = dataset_item.get(is_media_key)

                    if is_media and str(is_media).lower() in ["是", "yes", "true", "1"]:
                        # 处理媒体内容
                        processed_value = self._process_media_content(value, dataset_item, field=key)
                        payload[key] = processed_value

    def _process_runtime_variables(self, payload: Dict[str, Any]) -> None:
        """
        处理 runtime_variables 中的 { value, type } 格式

        将 {'value': '4', 'type': 'number'} 转换为 4（数字）
        将 {'value': 'LCRM', 'type': 'string'} 转换为 'LCRM'（字符串）
        这与模型配置测试连通性的处理逻辑一致
        """
        import logging
        logger = logging.getLogger(__name__)

        # 需要排除的字段
        excluded_keys = {"user_input", "ref_answer", "model_answer", "_dataset_item", "messages", "idx"}

        processed_keys = []
        for key in list(payload.keys()):
            if key in excluded_keys:
                continue

            value = payload[key]
            if isinstance(value, dict) and 'value' in value and 'type' in value:
                # 处理 { value, type } 格式
                var_type = value.get('type', 'string')
                var_value = value.get('value', '')

                if var_type == 'number':
                    try:
                        payload[key] = float(var_value) if '.' in str(var_value) else int(var_value)
                    except (ValueError, TypeError):
                        payload[key] = 0
                elif var_type == 'boolean':
                    payload[key] = str(var_value).lower() in ('true', '1', 'yes')
                else:
                    payload[key] = str(var_value)

                processed_keys.append(key)


    def _process_media_content(self, content: str, dataset_item, field: str = "answer") -> str:
        """处理媒体文件内容，将文件引用转换为文字

        Args:
            content: 文件名、OSS URL 或内容
            dataset_item: 数据集项
            field: 字段名 ('answer' 或 'user_input')
        """
        import logging
        import os
        logger = logging.getLogger(__name__)

        if not content:
            return content

        # 优先检查是否是 OSS URL，如果是则通过 OSSMediaCacheService 处理
        try:
            from app.services.oss_media_cache_service import OSSMediaCacheService
            oss_service = OSSMediaCacheService()
            if oss_service.validate_oss_url(content):
                logger.info(f"检测到 OSS URL，通过缓存服务处理: {content[:80]}...")
                result = oss_service.get_media_content(content)
                if result is not None:
                    logger.info(f"OSS 媒体内容获取成功，长度: {len(result)}")
                    return result
                else:
                    logger.warning(f"OSS 媒体内容获取失败，字段值置空: {content[:80]}")
                    return ""
        except Exception as e:
            logger.error(f"OSS 媒体处理异常: {content[:80]}, 错误: {e}")

        # 检查是否是文件引用（以常见文件扩展名结尾）——向后兼容本地文件
        file_extensions = ('.xlsx', '.xls', '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp')
        if not content.lower().endswith(file_extensions):
            logger.warning(
                f"[_process_media_content] 媒体字段值不符合 OSS URL 格式且不是已知文件引用，"
                f"字段 '{field}' 置空: {content[:80]}"
            )
            return ""

        # 从数据库查询文件路径
        file_path = self._get_file_path_from_db(content, dataset_item, field)
        if not file_path:
            logger.warning(f"未找到文件路径: {content} (field={field})")
            return content

        if not os.path.exists(file_path):
            logger.warning(f"文件不存在: {file_path}")
            return content

        try:
            from app.services.media_processor import MediaProcessor
            # 使用 uploads/datasets 作为 base_path
            base_path = os.path.join(os.getcwd(), "uploads", "datasets")
            media_processor = MediaProcessor(base_path=base_path)
            # 使用 process_content 方法处理文件
            file_path = os.path.normpath(file_path)
            result = media_processor.process_content(file_path)
            logger.info(f"成功处理媒体文件: {file_path}, 转换后长度: {len(result)}")
            return result
        except Exception as e:
            logger.error(f"处理媒体文件失败: {content}, 错误: {e}")
            return content

    def _get_file_path_from_db(self, file_name: str, dataset_item, field: str = "answer") -> Optional[str]:
        """从 dataset_item 的 meta_data 中获取文件路径

        Args:
            file_name: 文件名
            dataset_item: 数据集项
            field: 字段名 ('answer' 或 'user_input')
        """
        import logging
        logger = logging.getLogger(__name__)

        # 获取 meta_data（支持字典和对象）
        if isinstance(dataset_item, dict):
            meta_data = dataset_item.get("meta_data")
        else:
            meta_data = getattr(dataset_item, "meta_data", None)

        if not meta_data:
            logger.warning("未找到 meta_data")
            return None

        # 如果 meta_data 是字符串（JSON字符串），需要解析
        if isinstance(meta_data, str):
            import json
            try:
                meta_data = json.loads(meta_data)
                logger.info(f"解析后的 meta_data: {meta_data}")
            except:
                logger.warning(f"meta_data 解析失败: {meta_data[:100]}")
                return None

        # 获取对应字段的文件列表
        files_key = f"{field}_files"
        files_list = meta_data.get(files_key, [])

        if not files_list:
            # 尝试其他可能的 key
            possible_keys = ["files", "answer_files", "user_input_files", "file_path"]
            for key in possible_keys:
                if key in meta_data:
                    files_list = meta_data.get(key, [])
                    if files_list:
                        break

        if not files_list:
            logger.warning(f"未找到 {files_key} 文件列表, meta_data keys: {list(meta_data.keys()) if isinstance(meta_data, dict) else 'not a dict'}")
            return None

        # 查找匹配的文件名
        for file_info in files_list:
            if file_info.get("file_name") == file_name:
                file_path = file_info.get("file_path")
                return file_path

        logger.warning(f"未在 meta_data 中找到文件: {file_name}")
        return None

    # 数据集字段的别名映射：前端显示名 → dataset_item 实际字段名
    # 注意：flatten_data_item 会将 JSON 中的字段展开
    # - question JSON 中的 "user_input" 展开为 user_input
    # - ref_answer JSON 中的 "answer" 展开为 answer（不是 ref_answer）
    # 所以 ref_answer 需要映射到 answer
    DATASET_FIELD_ALIASES = {
        "user_input": "question",
        "用户输入": "question",
        "ref_answer": "answer",  # ref_answer JSON 展开后是 answer
        "参考答案": "answer",
        "answer_2": "answer_2",  # answer_2 JSON 展开后是 answer_2
    }

    def _resolve_dataset_field(self, source_field: str, dataset_item: Dict[str, Any]):
        """从 dataset_item 中按字段名取值，支持别名映射和 JSON 字段解析"""
        import json
        import logging
        logger = logging.getLogger(__name__)

        # 1. 直接命中
        if source_field in dataset_item:
            value = dataset_item.get(source_field)
            logger.debug(f"[VariableInjector] _resolve_dataset_field 直接命中 | field={source_field} | value={repr(value)[:100]}")
            # 如果直接命中的是 ref_answer（JSON 字符串），解析后返回 answer
            if source_field == "ref_answer" and isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict) and "answer" in parsed:
                        return parsed.get("answer")
                except (json.JSONDecodeError, TypeError):
                    pass
            return value

        # 2. 别名查找
        actual_field = self.DATASET_FIELD_ALIASES.get(source_field)
        if actual_field and actual_field in dataset_item:
            logger.debug(f"[VariableInjector] _resolve_dataset_field 别名命中 | source_field={source_field} | actual_field={actual_field}")
            return dataset_item.get(actual_field)

        # 3. 从 JSON 字段中解析（支持多字段）
        # question JSON 格式: {"user_input": "...", "user_input_xxx": "..."}
        # ref_answer JSON 格式: {"answer": "...", "answer_xxx": "..."}
        question_json = dataset_item.get("question")
        if question_json and isinstance(question_json, str):
            try:
                parsed_question = json.loads(question_json)
                if isinstance(parsed_question, dict) and source_field in parsed_question:
                    return parsed_question.get(source_field)
            except (json.JSONDecodeError, TypeError):
                pass

        ref_answer_json = dataset_item.get("ref_answer")
        if ref_answer_json and isinstance(ref_answer_json, str):
            try:
                parsed_ref_answer = json.loads(ref_answer_json)
                if isinstance(parsed_ref_answer, dict) and source_field in parsed_ref_answer:
                    return parsed_ref_answer.get(source_field)
            except (json.JSONDecodeError, TypeError):
                pass

        # 未找到值，记录警告和数据集字段信息
        logger.warning(f"[VariableInjector] _resolve_dataset_field 未找到值 | source_field={source_field} | "
                      f"dataset_item_keys={list(dataset_item.keys())}")
        return None

    def get_nested_value(self, data: Dict[str, Any], path: str) -> Any:
        """从嵌套字典中按路径取值（支持数组索引）"""
        keys = path.split(".")
        current = data
        for key in keys:
            match = re.match(r'^(\w+)\[(\d+)\]$', key)
            if match:
                array_key = match.group(1)
                index = int(match.group(2))
                if array_key in current and isinstance(current[array_key], list):
                    if index < len(current[array_key]):
                        current = current[array_key][index]
                    else:
                        return None
                else:
                    return None
            else:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return None
        return current

    def _get_from_data_json(self, field_path: str, dataset_item: Dict[str, Any]) -> Optional[str]:
        """从 dataset_item 的 data JSON 中按路径取值

        支持点号分隔的嵌套路径（如 "user.name"）
        和数组索引（如 "answers[0]"）。

        Args:
            field_path: 字段路径
            dataset_item: 数据集项

        Returns:
            字段值，不存在返回 None
        """
        import logging
        logger = logging.getLogger(__name__)

        # 优先从 _dataset_item.data 中取（V2 格式）
        _dataset = dataset_item.get("_dataset_item", {})
        logger.debug(f"[VariableInjector] _get_from_data_json | field_path={field_path} | "
                     f"_dataset_item_exists={bool(_dataset)} | "
                     f"dataset_item_keys={list(dataset_item.keys())}")
        if isinstance(_dataset, dict) and _dataset:
            data = _dataset.get("data", {})
            if isinstance(data, dict):
                result = self.get_nested_value(data, field_path)
                logger.debug(f"[VariableInjector] _get_from_data_json V2 | result={repr(result)[:100]}")
                if result is not None:
                    return result

        # 兼容：直接从 dataset_item.data 取
        if isinstance(dataset_item, dict):
            data = dataset_item.get("data", {})
            logger.debug(f"[VariableInjector] _get_from_data_json fallback | "
                         f"data_keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
            if isinstance(data, dict):
                result = self.get_nested_value(data, field_path)
                logger.debug(f"[VariableInjector] _get_from_data_json fallback result | "
                             f"field_path={field_path} | result={repr(result)[:100]}")
                return result

        return None

    def _get_variable_value(
        self,
        var_name: str,
        dataset_item: Dict[str, Any],
    ) -> Optional[str]:
        """
        获取变量值

        变量来源优先级：
        - 若 variable_sources[var_name] == "dataset"：强制从数据集字段映射获取，忽略 runtime_variables
        - 否则：先取 runtime_variables（手动输入），再取 variable_mappings，再取 dataset_item 原始字段

        Args:
            var_name: 变量名
            dataset_item: 数据集单条数据

        Returns:
            变量值
        """
        import logging
        logger = logging.getLogger(__name__)
        # 仅对非标准字段记录详细日志
        if var_name not in ("user_input", "ref_answer", "model_answer"):
            logger.debug(f"[VariableInjector] 获取变量 | var_name={var_name} | "
                        f"source={self.variable_sources.get(var_name, 'manual')} | "
                        f"mapping={self.variable_mappings.get(var_name, 'N/A')} | "
                        f"runtime_keys={list(self.runtime_variables.keys())}")

        # V2 字段映射：优先使用 input_field/reference_field 从 data JSON 取值
        if var_name == "user_input":
            val = self._get_from_data_json(self.input_field, dataset_item)
            if val is not None:
                return val
        if var_name == "ref_answer":
            val = self._get_from_data_json(self.reference_field, dataset_item)
            if val is not None:
                return val

        # 0. 优先检查 variable_sources：若配置为"数据集字段"，直接走映射，跳过 runtime_variables
        source = self.variable_sources.get(var_name, "manual")
        if source == "dataset":
            # 从 variable_mappings 获取对应的数据集字段名
            mapped_field = self.variable_mappings.get(var_name, var_name)
            import logging
            logger = logging.getLogger(__name__)

            # 尝试从 dataset_item 直接获取
            val = self._resolve_dataset_field(mapped_field, dataset_item)

            # 如果直接获取失败，尝试从 data JSON 中获取
            if val is None:
                val = self._get_from_data_json(mapped_field, dataset_item)

            return val

        # 1. 检查运行时变量（手动输入）
        if var_name in self.runtime_variables:
            value = self.runtime_variables[var_name]
            # 处理 { value, type } 格式（如 {"value": "4", "type": "number"}）
            if isinstance(value, dict) and 'value' in value and 'type' in value:
                var_type = value.get('type', 'string')
                var_value = value.get('value', '')
                if var_type == 'number':
                    try:
                        return float(var_value) if '.' in str(var_value) else int(var_value)
                    except (ValueError, TypeError):
                        return 0
                elif var_type == 'boolean':
                    return str(var_value).lower() in ('true', '1', 'yes')
                else:
                    return str(var_value)
            return str(value)

        # 2. 检查变量映射（支持多字段数组格式）
        if var_name in self.variable_mappings:
            mapping = self.variable_mappings[var_name]
            # 支持数组格式的多字段组合
            if isinstance(mapping, list):
                # 多字段组合：返回多个字段值的拼接
                values = []
                for field_name in mapping:
                    val = self._resolve_dataset_field(field_name, dataset_item)
                    if val:
                        values.append(str(val))
                return "\n".join(values) if values else None
            # 单字段映射
            return self._resolve_dataset_field(mapping, dataset_item)

        # 3. 检查数据集原始字段（含别名）
        val = self._resolve_dataset_field(var_name, dataset_item)
        if val is not None:
            return val

        # 4. Fallback: 对于user_input，尝试从question获取
        if var_name == "user_input" and "question" in dataset_item:
            return dataset_item.get("question")

        # 5. Fallback: 对于ref_answer，尝试从ref_answer获取
        if var_name == "ref_answer" and "ref_answer" in dataset_item:
            return dataset_item.get("ref_answer")

        return None

    def _format_user_import(
        self,
        user_input: str,
        dataset_item: Dict[str, Any],
    ) -> str:
        """
        格式化user_import

        支持的占位符：
        - {user_input}: 用户输入
        - {question}: 问题（user_input的别名）
        - {ref_answer}: 参考答案
        - {dataset_field}: 数据集中的其他字段

        Args:
            user_input: 用户输入
            dataset_item: 数据集数据

        Returns:
            格式化后的user_import字符串
        """
        template = self.user_import_template

        # 替换占位符
        result = template.replace("{user_input}", user_input or "")
        result = result.replace("{question}", user_input or "")

        # 替换参考答案
        ref_answer = dataset_item.get("ref_answer") or ""
        result = result.replace("{ref_answer}", ref_answer)

        # 替换其他数据集字段
        for key, value in dataset_item.items():
            if key.startswith("_"):
                continue
            placeholder = f"{{{key}}}"
            if placeholder in result:
                result = result.replace(placeholder, str(value or ""))

        return result

    def build_judge_payload(
        self,
        original_payload: Dict[str, Any],
        model_output: str,
    ) -> Dict[str, Any]:
        """
        构建裁判请求的Payload

        Args:
            original_payload: 原始Payload（包含user_input等）
            model_output: 模型输出

        Returns:
            裁判Payload - 通过 judge_variable_config 解析变量
        """
        import logging
        logger = logging.getLogger(__name__)

        # 以原始 payload 为基础，保留所有字段作为回退
        # 这确保当 judge_variable_config 中的变量无法从数据集/请求链解析时，
        # 仍然可以从原始 payload 中获取值
        judge_payload = dict(original_payload)
        judge_payload["model_answer"] = model_output

        # 获取裁判变量配置
        judge_variable_config = getattr(self.task, "judge_variable_config", None) or {}

        if judge_variable_config:
            # 使用 JudgeVariableResolver 解析变量
            from app.services.judge_variable_resolver import JudgeVariableResolver

            # 获取 task_config，包含 input_field 和 reference_field 映射
            task_config = {
                "judge_variable_config": judge_variable_config,
                "input_field": getattr(self.task, "input_field", "user_input") or "user_input",
                "reference_field": getattr(self.task, "reference_field", "answer") or "answer",
            }

            resolver = JudgeVariableResolver(
                task_config=task_config,
                dataset_item=original_payload.get("_dataset_item", {}),
                model_result={"content": model_output},
                chain_results=self.chain_results
            )

            # 根据 judge_variable_config 解析各变量
            if isinstance(judge_variable_config, dict):
                for var_name, config in judge_variable_config.items():
                    if not isinstance(config, dict):
                        continue
                    source = config.get("source")
                    # model_output 类型直接从参数获取
                    if source == "model_output":
                        judge_payload[var_name] = model_output
                    # 前处理脚本输出：按 {script, key} 从 script_outputs 取值（设计文档 §三）
                    elif source == "script_output":
                        judge_payload[var_name] = resolve_script_output_value(
                            self.script_outputs, config, var_name,
                        )
                    else:
                        # 使用 resolver 解析其他来源的变量
                        resolved_value = resolver.resolve(var_name)
                        if resolved_value:
                            judge_payload[var_name] = resolved_value
                        # 如果 resolver 无法解析，保留原始 payload 中的值（回退）

            elif isinstance(judge_variable_config, list):
                # 列表格式：[{"key": "user_input", "source": "dataset_field", ...}, ...]
                for item in judge_variable_config:
                    if not isinstance(item, dict):
                        continue
                    var_key = item.get("key", "")
                    source = item.get("source")
                    if not var_key:
                        continue

                    # model_output 类型直接从参数获取
                    if source == "model_output":
                        judge_payload[var_key] = model_output
                    # 前处理脚本输出：按 source_config {script, key} 从 script_outputs 取值
                    # （list 格式顶层 key 是变量名本身，不能当作脚本输出键）
                    elif source == "script_output":
                        judge_payload[var_key] = resolve_script_output_value(
                            self.script_outputs, item, var_key, allow_top_level_key=False,
                        )
                    else:
                        # 使用 resolver 解析其他来源的变量
                        resolved_value = resolver.resolve(var_key)
                        if resolved_value:
                            judge_payload[var_key] = resolved_value
                        # 如果 resolver 无法解析，保留原始 payload 中的值（回退）

            logger.info(
                f"[build_judge_payload] 构建完成 | "
                f"config_type={'list' if isinstance(judge_variable_config, list) else 'dict'} | "
                f"payload_keys={list(judge_payload.keys())}"
            )
        else:
            # 兼容未配置 judge_variable_config 的情况
            # 原始 payload 已经包含了 user_input 和 ref_answer，无需额外处理
            logger.info("[build_judge_payload] 未配置 judge_variable_config，使用原始 payload")

        if self.script_outputs:
            # 附上前处理脚本完整输出，供裁判 Prompt 组装解析 script_output 来源的变量
            judge_payload["script_outputs"] = dict(self.script_outputs)

        return judge_payload

    def resolve_chain_variable(self, ref: str) -> str:
        """解析 chain 变量引用

        支持的语法：
        - {{alias.var_name}} - 通过请求别名引用，如 {{login.session_id}}
        - {{chain[0].var_name}} - 通过索引引用，如 {{chain[0].session_id}}

        Args:
            ref: 变量引用字符串

        Returns:
            变量值
        """
        # 处理 {{alias.var_name}} 格式
        match = self.CHAIN_ALIAS_PATTERN.match(ref)
        if match:
            alias = match.group(1)
            var_name = match.group(2)
            return self._get_chain_var_by_alias(alias, var_name)

        # 处理 {{chain[0].var_name}} 格式
        match = self.CHAIN_INDEX_PATTERN.match(ref)
        if match:
            chain_index = int(match.group(1))
            var_name = match.group(2)
            return self._get_chain_var_by_index(chain_index, var_name)

        return ref  # 不匹配，返回原值

    def _get_chain_var_by_alias(self, alias: str, var_name: str) -> str:
        """通过请求别名获取变量

        Args:
            alias: 请求别名
            var_name: 变量名

        Returns:
            变量值
        """
        for chain_result in self.chain_results:
            if chain_result.get("alias") == alias:
                return self._get_chain_var(chain_result, var_name)
        return ""

    def _get_chain_var_by_index(self, chain_index: int, var_name: str) -> str:
        """通过请求索引获取变量

        Args:
            chain_index: 请求索引（从0开始）
            var_name: 变量名

        Returns:
            变量值
        """
        if chain_index >= len(self.chain_results):
            return ""
        chain_result = self.chain_results[chain_index]
        return self._get_chain_var(chain_result, var_name)

    def _get_chain_var(self, chain_result: Dict[str, Any], var_name: str) -> str:
        """从请求结果中获取指定变量

        Args:
            chain_result: 请求结果
            var_name: 变量名

        Returns:
            变量值
        """
        # 新结构：从 variables 获取（多变量模式）
        if "variables" in chain_result:
            return chain_result["variables"].get(var_name, "")

        # 旧结构兼容：从 all_results 获取
        if "all_results" in chain_result:
            return chain_result["all_results"].get(var_name, "")

        # 单变量模式
        output_var = chain_result.get("output_var")
        if output_var == var_name:
            return chain_result.get("content", "")

        return ""

    def resolve_all_chain_variables(self, text: str) -> str:
        """解析文本中所有 chain 变量引用

        Args:
            text: 包含变量引用的文本

        Returns:
            变量替换后的文本
        """
        if not text:
            return text

        result = text

        # 处理 {{alias.var_name}} 格式
        for match in self.CHAIN_ALIAS_PATTERN.finditer(text):
            full_match = match.group(0)
            alias = match.group(1)
            var_name = match.group(2)
            value = self._get_chain_var_by_alias(alias, var_name)
            result = result.replace(full_match, str(value))

        # 处理 {{chain[0].var_name}} 格式
        for match in self.CHAIN_INDEX_PATTERN.finditer(text):
            full_match = match.group(0)
            chain_index = int(match.group(1))
            var_name = match.group(2)
            value = self._get_chain_var_by_index(chain_index, var_name)
            result = result.replace(full_match, str(value))

        return result

    def get_all_chain_variables(self) -> Dict[str, str]:
        """获取请求链汇总的所有变量

        Returns:
            所有变量 {变量名: 值}
        """
        return self.all_variables.copy()

    def get_chain_results_tree(self) -> List[Dict[str, Any]]:
        """获取请求链结果树形结构，用于前端 UI 展示

        Returns:
            每个请求及其变量的树形结构
        """
        tree = []
        for chain_result in self.chain_results:
            tree.append({
                "order": chain_result.get("order"),
                "name": chain_result.get("name"),
                "alias": chain_result.get("alias"),
                "variables": chain_result.get("variables", {})
            })
        return tree
