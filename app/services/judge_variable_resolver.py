"""
裁判模型变量解析器
用于解析裁判模型所需变量的数据来源
"""
import json
import uuid
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from dataclasses import dataclass, field


class EvaluationTarget(BaseModel):
    """评测目标模型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(..., description="字段名称，如 model_output、chain_response_1")
    source: str = Field(..., description="来源类型：model_output / chain_result / dataset_field / manual_input / script_output")
    source_config: Dict[str, Any] = Field(default_factory=dict)
    required: bool = Field(default=True)
    display_name: str = Field(..., description="前端展示名称")


class JudgeVariableConfig(BaseModel):
    """裁判变量配置模型"""
    variable_name: str = Field(..., description="变量名")
    source: str = Field(..., description="来源类型")
    source_config: Dict[str, Any] = Field(default_factory=dict)
    prompt_type: str = Field(default="user", description="放入 system 或 user 消息")
    required: bool = Field(default=False, description="是否必填")


class JudgeVariableResolver:
    """裁判模型变量解析器

    处理裁判模型所需变量的数据来源解析：
    - user_input: 用户指令
    - ref_answer: 参考答案
    - model_answer: AI助手回复
    """

    # 默认配置（向后兼容）
    DEFAULT_CONFIG = {
        "user_input": {
            "source": "dataset_field",
            "source_config": {
                "field": "question"
            },
            "prompt_type": "user"
        },
        "ref_answer": {
            "source": "dataset_field",
            "source_config": {
                "field": "ref_answer"
            },
            "prompt_type": "user"
        },
        "model_answer": {
            "source": "model_output",
            "source_config": {},
            "prompt_type": "user"
        }
    }

    def __init__(
        self,
        task_config: Dict[str, Any],
        dataset_item: Dict[str, Any],
        model_result: Optional[Dict[str, Any]] = None,
        chain_results: Optional[List[Dict[str, Any]]] = None,
        script_outputs: Optional[Dict[str, Any]] = None
    ):
        """
        初始化变量解析器

        Args:
            task_config: 任务配置，包含 judge_variable_config
            dataset_item: 数据集项，包含 question, ref_answer 等字段
            model_result: 模型执行结果，包含 content 字段
            chain_results: 请求链结果列表，每个元素包含请求的输出变量
            script_outputs: 评测前处理脚本输出，键为 output_var 或脚本名
                （与 run_pre_scripts 的收集口径一致，供 script_output 来源目标解析）
        """
        self.task_config = task_config
        self.dataset_item = dataset_item or {}
        self.model_result = model_result or {}
        self.chain_results = chain_results or []
        self.script_outputs = script_outputs or {}

        # 获取配置，不再使用 DEFAULT_CONFIG 作为回退
        # 当 judge_variable_config 未配置时，使用空配置
        # 避免强依赖数据集字段
        raw_config = task_config.get("judge_variable_config")
        if raw_config:
            self.variable_config = raw_config
        else:
            self.variable_config = {}

        # V2 字段映射配置（支持自由数据集字段）
        self.input_field = task_config.get("input_field", "user_input") or "user_input"
        self.reference_field = task_config.get("reference_field", "answer") or "answer"

    def get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return self.DEFAULT_CONFIG.copy()

    def resolve(self, var_name: str) -> str:
        """解析单个变量的值

        Args:
            var_name: 变量名 (user_input/ref_answer/model_answer)

        Returns:
            变量值
        """
        config = self._get_var_config(var_name)
        if not config:
            # 如果没有配置，返回空字符串
            return ""

        source = config.get("source")
        source_config = config.get("source_config", {})

        if source == "dataset_field":
            field = source_config.get("field")
            return self._get_from_dataset(field)
        elif source == "chain_result":
            chain_index = source_config.get("chain_index")
            # 将 1-based chain_index 转换为 0-based 数组索引
            if chain_index is not None:
                chain_index = chain_index - 1
            chain_key = source_config.get("chain_key", "content")
            return self._get_from_chain(chain_index, chain_key)
        elif source == "model_output":
            return self._get_from_model_output()
        elif source == "manual":
            return self._get_from_manual(var_name)
        else:
            return ""

    def _get_var_config(self, var_name: str) -> Optional[Dict[str, Any]]:
        """获取变量配置，支持字典格式和列表格式

        Args:
            var_name: 变量名

        Returns:
            变量配置字典，如果不存在返回 None
        """
        variable_config = self.variable_config

        # 字典格式：{"user_input": {...}, "ref_answer": {...}, ...}
        if isinstance(variable_config, dict):
            return variable_config.get(var_name)

        # 列表格式：[{"key": "user_input", ...}, {"key": "ref_answer", ...}, ...]
        if isinstance(variable_config, list):
            for item in variable_config:
                if isinstance(item, dict) and item.get("key") == var_name:
                    return item

        return None

    def resolve_all(self) -> Dict[str, str]:
        """解析所有变量

        Returns:
            包含所有变量值的字典
        """
        return {
            "user_input": self.resolve("user_input"),
            "ref_answer": self.resolve("ref_answer"),
            "model_answer": self.resolve("model_answer")
        }

    def resolve_targets(
        self,
        targets: List[EvaluationTarget]
    ) -> Dict[str, Dict[str, Any]]:
        """解析所有评测目标

        Args:
            targets: 评测目标列表

        Returns:
            评测目标解析结果字典，格式为 {target_id: {"name": str, "content": str, "is_empty": bool, "source": str, "required": bool}}
        """
        results = {}
        for target in targets:
            content = self._resolve_single_target(target)
            is_empty = not content or content.strip() == ""
            results[target.id] = {
                "name": target.display_name,
                "content": content,
                "is_empty": is_empty,
                "source": target.source,
                "required": target.required
            }
        return results

    def _resolve_single_target(self, target: EvaluationTarget) -> str:
        """解析单个评测目标

        Args:
            target: 评测目标

        Returns:
            解析后的内容
        """
        if target.source == "model_output":
            return self._safe_string(self.model_result.get("content", ""))
        elif target.source == "chain_result":
            chain_index = target.source_config.get("chain_index")
            # 将 1-based chain_index 转换为 0-based 数组索引
            if chain_index is not None:
                chain_index = chain_index - 1
            # 支持 var_names 数组格式（前端生成），使用 display_name 作为标签前缀
            var_names = target.source_config.get("var_names")
            if var_names and isinstance(var_names, list):
                # 多个字段逐行拼接，使用 display_name 作为前缀
                parts = []
                for key in var_names:
                    value = self._get_from_chain(chain_index, key)
                    if value:
                        parts.append(f"{target.display_name}：{value}")
                return "\n".join(parts)
            # 兼容单字段格式
            chain_key = target.source_config.get("chain_key", "content")
            return self._get_from_chain(chain_index, chain_key)
        elif target.source == "dataset_field":
            field = target.source_config.get("field")
            return self._get_from_dataset(field)
        elif target.source == "manual_input":
            return self._safe_string(target.source_config.get("value", ""))
        elif target.source == "script_output":
            return self._get_from_script_output(target.source_config)
        return ""

    def get_prompt_type(self, var_name: str) -> str:
        """获取变量的prompt类型

        Args:
            var_name: 变量名

        Returns:
            "system" 或 "user"
        """
        config = self._get_var_config(var_name)
        if not config:
            return "user"
        return config.get("prompt_type", "user")

    def get_all_prompt_types(self) -> Dict[str, str]:
        """获取所有变量的prompt类型

        Returns:
            变量名到prompt类型的映射
        """
        return {
            "user_input": self.get_prompt_type("user_input"),
            "ref_answer": self.get_prompt_type("ref_answer"),
            "model_answer": self.get_prompt_type("model_answer")
        }

    @staticmethod
    def _safe_string(value: Any) -> str:
        """将值安全转换为字符串，布尔值统一转小写"""
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return ""
        return str(value)

    def _get_from_script_output(self, source_config: Dict[str, Any]) -> str:
        """从评测前处理脚本输出获取值

        行内容 = script_outputs[output_var 或脚本名] 的 output_key：
        - output_key 为空：整体 JSON 序列化为字符串
        - output_key 非空但取不到（输出非对象 / 键不存在 / 值为 None）：
          整体 JSON 序列化兜底
        - 取到：dict/list 序列化为 JSON 文本，其余复用 _safe_string 风格转换
        - 键不存在或输出为 None（脚本失败 on_fail=continue 等场景）：返回空串，
          走既有空值校验逻辑
        """
        script_id = source_config.get("script_id")
        output_var = source_config.get("output_var")
        # script_outputs 键与 run_pre_scripts 收集口径一致：output_var 或脚本名
        output_key_in_ctx = output_var or script_id
        if not output_key_in_ctx or output_key_in_ctx not in self.script_outputs:
            return ""

        output = self.script_outputs.get(output_key_in_ctx)
        if output is None:
            return ""

        output_key = source_config.get("output_key")
        if output_key:
            value = output.get(output_key) if isinstance(output, dict) else None
            if value is not None:
                if isinstance(value, (dict, list)):
                    return self._json_dump_output(value)
                return self._safe_string(value)
        # output_key 为空或取不到：整体 JSON 序列化
        return self._json_dump_output(output)

    @staticmethod
    def _json_dump_output(value: Any) -> str:
        """把脚本输出序列化为 JSON 文本（ensure_ascii=False + default=str 容错）"""
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    def _get_from_dataset(self, field: str) -> str:
        """从数据集字段获取值，支持嵌套路径"""
        if not field:
            return ""
        # 优先从 dataset_item.data 取（V2 格式）
        val = self._get_nested_value(self.dataset_item.get("data", {}), field)
        if val is not None:
            return self._safe_string(val)
        # 回退到原有逻辑
        raw = self.dataset_item.get(field, "")
        return self._safe_string(raw)

    def _get_nested_value(self, data: Dict[str, Any], path: str) -> Any:
        """从嵌套字典中按路径取值（支持数组索引）"""
        import re
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

    def _get_from_chain(self, chain_index: Optional[int], chain_key: str) -> str:
        """从请求链结果获取值

        Args:
            chain_index: 请求索引（从0开始）
            chain_key: 结果key

        Returns:
            请求结果值
        """
        if chain_index is None or chain_index < 0 or chain_index >= len(self.chain_results):
            return ""

        chain_result = self.chain_results[chain_index]

        # 新结构：从 variables 获取（多变量模式）
        if "variables" in chain_result:
            return self._safe_string(chain_result["variables"].get(chain_key, ""))

        # 旧结构：直接从 chain_result 获取（单变量模式兼容）
        if "all_results" in chain_result:
            return self._safe_string(chain_result["all_results"].get(chain_key, ""))

        # 最基础的：从 content 获取
        return self._safe_string(chain_result.get("content", ""))

    def resolve_chain_vars(self, chain_index: int, var_names: List[str]) -> Dict[str, str]:
        """从请求链结果中获取多个变量

        Args:
            chain_index: 请求索引（从0开始）
            var_names: 变量名列表

        Returns:
            {变量名: 值} 字典
        """
        results = {}
        if chain_index is None or chain_index >= len(self.chain_results):
            return results

        chain_result = self.chain_results[chain_index]

        # 新结构：从 variables 获取（多变量模式）
        if "variables" in chain_result:
            variables = chain_result["variables"]
            for var_name in var_names:
                results[var_name] = self._safe_string(variables.get(var_name, ""))

        # 旧结构兼容
        elif "all_results" in chain_result:
            all_results = chain_result["all_results"]
            for var_name in var_names:
                results[var_name] = self._safe_string(all_results.get(var_name, ""))

        return results

    def resolve_chain_var_by_alias(self, alias: str, var_name: str) -> str:
        """通过请求别名获取变量

        Args:
            alias: 请求别名（如 login, getUserInfo）
            var_name: 变量名

        Returns:
            变量值
        """
        for chain_result in self.chain_results:
            if chain_result.get("alias") == alias:
                # 新结构：从 variables 获取
                if "variables" in chain_result:
                    return self._safe_string(chain_result["variables"].get(var_name, ""))
                # 旧结构兼容
                if "all_results" in chain_result:
                    return self._safe_string(chain_result["all_results"].get(var_name, ""))
                # 单变量模式
                output_var = chain_result.get("output_var")
                if output_var == var_name:
                    return self._safe_string(chain_result.get("content", ""))
        return ""

    def _get_from_model_output(self) -> str:
        """从模型输出获取值"""
        return self._safe_string(self.model_result.get("content", ""))

    def _get_from_manual(self, var_name: str) -> str:
        """从手动输入的变量获取值"""
        runtime_variables = self.task_config.get("runtime_variables", {})
        if var_name in runtime_variables:
            var_data = runtime_variables[var_name]
            # 支持多种格式：直接值或 {value: xxx, type: xxx}
            if isinstance(var_data, dict):
                return self._safe_string(var_data.get("value", ""))
            return self._safe_string(var_data)
        return ""

    def build_messages(
        self,
        system_role: str = "你是一名公正的评分裁判。"
    ) -> List[Dict[str, Any]]:
        """构建裁判模型的消息列表

        根据 prompt_type 配置决定变量放在 system 还是 user 消息中

        Args:
            system_role: system 消息的角色定义

        Returns:
            消息列表
        """
        variables = self.resolve_all()
        prompt_types = self.get_all_prompt_types()

        # 构建 system 消息内容
        system_parts = [system_role]

        # 构建 user 消息内容
        user_parts = []

        # 处理 user_input
        if prompt_types.get("user_input") == "system":
            system_parts.append(f"\n\n用户问题：{variables['user_input']}")
        else:
            user_parts.append(f"用户问题：{variables['user_input']}")

        # 处理 ref_answer
        if prompt_types.get("ref_answer") == "system":
            system_parts.append(f"\n\n参考答案：{variables['ref_answer']}")
        else:
            user_parts.append(f"参考答案：{variables['ref_answer']}")

        # 处理 model_answer
        if prompt_types.get("model_answer") == "system":
            system_parts.append(f"\n\n模型回答：{variables['model_answer']}")
        else:
            user_parts.append(f"模型回答：{variables['model_answer']}")

        messages = [
            {"role": "system", "content": "\n".join(system_parts)},
            {"role": "user", "content": "\n\n".join(user_parts)}
        ]

        # 过滤掉空内容
        messages[1]["content"] = messages[1]["content"].strip()

        return messages
