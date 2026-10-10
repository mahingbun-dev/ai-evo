"""
统一变量上下文

管理请求链中的所有变量：
- chain_variables: 请求链变量（按请求别名组织）
- runtime_variables: 运行时变量
- builtin_variables: 内置变量
"""
import re
from typing import Any, Dict, List, Optional


class VariableContext:
    """统一变量上下文"""

    def __init__(self):
        self.chain_variables: Dict[str, Any] = {}
        self.runtime_variables: Dict[str, Any] = {}
        self.builtin_variables: Dict[str, Any] = {
            "messages": None,
            "endpoint": None,
            "model_type": None,
            "model_id": None,
            "api_key": None,
            "cookie": None,
        }

    def set_chain_var(self, request_alias: str, var_name: str, value: Any):
        """设置请求链变量

        Args:
            request_alias: 请求别名，如 "login"
            var_name: 变量名，如 "token"
            value: 变量值
        """
        # 设置长名称（如 login.token）
        key = f"{request_alias}.{var_name}"
        self.chain_variables[key] = value
        # 也设置短名称，供直接引用
        self.chain_variables[var_name] = value

    def set_runtime_var(self, var_name: str, value: Any):
        """设置运行时变量

        Args:
            var_name: 变量名
            value: 变量值
        """
        self.runtime_variables[var_name] = value

    def set_builtin_var(self, var_name: str, value: Any):
        """设置内置变量

        Args:
            var_name: 变量名
            value: 变量值
        """
        self.builtin_variables[var_name] = value

    def get(self, ref: str) -> Any:
        """获取变量值

        支持多种引用格式：
        - {{alias.var}} - 从请求链变量获取
        - {{var}} - 直接获取（按优先级查找）

        Args:
            ref: 变量引用，如 "login.token" 或 "token"

        Returns:
            变量值，不存在返回 None
        """
        # 先尝试作为请求链变量获取（支持 alias.var 格式）
        if '.' in ref:
            if ref in self.chain_variables:
                return self.chain_variables[ref]

        # 尝试作为请求链变量（短名称）
        if ref in self.chain_variables:
            return self.chain_variables[ref]

        # 尝试运行时变量
        if ref in self.runtime_variables:
            return self.runtime_variables[ref]

        # 尝试内置变量
        if ref in self.builtin_variables:
            return self.builtin_variables[ref]

        return None

    def get_all(self) -> Dict[str, Any]:
        """获取所有变量（合并后的字典）"""
        all_vars = {}
        # 内置变量
        all_vars.update(self.builtin_variables)
        # 请求链变量
        all_vars.update(self.chain_variables)
        # 运行时变量（优先级最高）
        all_vars.update(self.runtime_variables)
        return all_vars

    def resolve_template(self, template: str, runtime_variables: Dict[str, Any] = None) -> Any:
        """解析模板字符串中的变量引用

        支持的语法：
        - {{alias.var}} - 请求链变量
        - {{var}} - 直接引用
        - [[var]] - 外部变量（从 runtime_variables 获取）

        Args:
            template: 模板字符串，如 "/api/user/{{user_id}}" 或 "{{token}}"
            runtime_variables: 外部运行时变量字典

        Returns:
            替换后的值（保持原始类型）
        """
        if not isinstance(template, str):
            return template

        # 检查是否包含变量引用
        has_double_brace = '{{' in template
        has_double_bracket = '[[' in template

        if not has_double_brace and not has_double_bracket:
            return template

        result = template

        # 处理 [[variable]] 格式（外部变量）
        if has_double_bracket:
            external_pattern = r'\[\[(\w+)\]\]'
            external_matches = re.findall(external_pattern, template)
            for var_name in external_matches:
                # 从 runtime_variables 获取值
                value = None
                if runtime_variables and var_name in runtime_variables:
                    value = runtime_variables[var_name]
                if value is not None:
                    # 替换 [[var_name]] 为值
                    result = result.replace(f'[[{var_name}]]', str(value) if not isinstance(value, str) else value)
                else:
                    # 变量值为 None 或不存在，替换为空字符串
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning(f"[VariableContext] 变量 {var_name} 的值为 None 或不存在，替换为空字符串")
                    result = result.replace(f'[[{var_name}]]', "")

        # 处理 {{alias.var}} 和 {{var}} 格式
        if has_double_brace:
            # 纯占位符模板（无前后缀）→ 返回原始类型
            if re.fullmatch(r'\{\{\w+(\.\w+)?\}\}', template):
                # 提取变量名
                var_name = template[2:-2]  # 去掉 {{ 和 }}
                return self.get(var_name)

            # 否则进行字符串替换
            # 匹配 {{alias.var}} 和 {{var}} 格式
            pattern = r'\{\{(\w+(?:\.\w+)?)\}\}'
            matches = re.findall(pattern, template)

            for match in matches:
                value = self.get(match)
                if value is not None:
                    # 对于非字符串类型，转字符串替换
                    if not isinstance(value, str):
                        result = result.replace(f"{{{{{match}}}}}", str(value))
                    else:
                        result = result.replace(f"{{{{{match}}}}}", value)
                else:
                    # 变量值为 None 或不存在，替换为空字符串
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning(f"[VariableContext] 变量 {match} 的值为 None 或不存在，替换为空字符串")
                    result = result.replace(f"{{{{{match}}}}}", "")

        return result

    def resolve_value(self, value: Any, original_value: Any = None) -> Any:
        """解析值中的变量引用，尝试保持原始类型

        支持的语法：
        - {{alias.var}} - 请求链变量
        - {{var}} - 直接引用
        - [[var]] - 外部变量（从 runtime_variables 获取）

        Args:
            value: 要解析的值（可能是字符串模板）
            original_value: 原始值，用于类型推断

        Returns:
            解析后的值
        """
        if value is None:
            return None

        # 如果不是字符串，直接返回
        if not isinstance(value, str):
            return value

        # 如果不包含变量引用，直接返回
        if '{{' not in value and '[[' not in value:
            return value

        # 处理 [[variable]] 格式（外部变量）
        if '[[' in value:
            external_pattern = r'\[\[(\w+)\]\]'
            external_matches = re.findall(external_pattern, value)
            for var_name in external_matches:
                # 从 runtime_variables 获取值
                var_value = self.runtime_variables.get(var_name)
                if var_value is not None:
                    # 替换 [[var_name]] 为值
                    if isinstance(var_value, (int, float, bool)):
                        value = value.replace(f'[[{var_name}]]', str(var_value).lower() if isinstance(var_value, bool) else str(var_value))
                    else:
                        value = value.replace(f'[[{var_name}]]', str(var_value))

        # 如果只剩下 {{}} 格式
        if '{{' not in value:
            return value

        # 检查是否是纯占位符
        if re.fullmatch(r'\{\{\w+(\.\w+)?\}\}', value):
            result = self.get(value[2:-2])
            if result is not None:
                return result
            return value

        # 多占位符或带前后缀，进行字符串替换
        result = value
        pattern = r'\{\{(\w+(?:\.\w+)?)\}\}'

        for match in re.findall(pattern, value):
            var_value = self.get(match)
            if var_value is not None:
                # 如果原始值是字符串类型的 JSON，数字/布尔需要特殊处理
                if isinstance(var_value, (int, float, bool)):
                    result = result.replace(f"{{{{{match}}}}}", str(var_value).lower() if isinstance(var_value, bool) else str(var_value))
                else:
                    result = result.replace(f"{{{{{match}}}}}", str(var_value))

        return result

    def get_preview_tree(self) -> List[Dict[str, Any]]:
        """获取变量预览树形结构，供前端使用

        Returns:
            树形结构列表
        """
        tree = []

        # 按请求别名分组请求链变量
        by_request: Dict[str, List[Dict]] = {}
        for key, value in self.chain_variables.items():
            # 跳过短名称（只处理带点号的完整名称）
            if '.' not in key:
                continue
            alias, var_name = key.split('.', 1)
            if alias not in by_request:
                by_request[alias] = []
            # 检查是否已存在（避免重复添加短名称）
            if not any(v['name'] == var_name for v in by_request[alias]):
                by_request[alias].append({
                    "name": var_name,
                    "value": str(value)[:50] if value else ""  # 截断预览值
                })

        # 转换为树形结构
        for alias in sorted(by_request.keys()):
            tree.append({
                "name": alias,
                "children": by_request[alias]
            })

        return tree

    def get_flat_variables(self) -> Dict[str, Any]:
        """获取扁平化的变量字典（用于调试）

        Returns:
            {变量名: 值} 字典
        """
        result = {}

        # 添加请求链变量
        for key, value in self.chain_variables.items():
            if '.' not in key:  # 只添加短名称
                result[key] = value

        # 添加运行时变量
        result.update(self.runtime_variables)

        return result

    def clear(self):
        """清空所有变量"""
        self.chain_variables.clear()
        self.runtime_variables.clear()
        # 内置变量不清空
        self.builtin_variables = {
            "messages": None,
            "endpoint": None,
            "model_type": None,
            "model_id": None,
            "api_key": None,
            "cookie": None,
        }

    def merge_runtime(self, runtime_vars: Dict[str, Any]):
        """合并运行时变量

        Args:
            runtime_vars: 运行时变量字典
        """
        if runtime_vars:
            self.runtime_variables.update(runtime_vars)
