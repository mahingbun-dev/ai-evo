"""
同步版本的请求链执行器 - 使用 requests 库，无异步依赖
"""
import logging
import json
import re
from typing import Dict, Any, Optional, List

import requests

from app.services.variable_context import VariableContext
from app.services.response_parser import ResponseParser

logger = logging.getLogger(__name__)


class RequestChainSync:
    """完全同步的请求链执行器，使用 requests 库"""

    BUILTIN_VARIABLES = {"messages", "token", "endpoint", "model_type", "model_id", "api_key", "cookie"}

    def __init__(self, chain_config: Dict[str, Any]):
        self.chain_config = chain_config
        self.variables = VariableContext()
        self._request_count = 0

    def execute(
        self,
        input_data: Dict[str, Any],
        config: Dict[str, Any],
        runtime_variables: Optional[Dict[str, Any]] = None,
        debug: bool = False,
    ) -> Dict[str, Any]:
        """同步执行请求链"""
        requests_config = self.chain_config.get("requests", [])

        if not requests_config:
            return {"success": False, "error": "请求链配置为空"}

        # 初始化 VariableContext
        self.variables = VariableContext()
        # 合并内置变量
        self.variables.set_builtin_var("messages", input_data.get("messages"))
        self.variables.set_builtin_var("endpoint", input_data.get("endpoint"))
        self.variables.set_builtin_var("model_type", input_data.get("model_type"))
        self.variables.set_builtin_var("model_id", input_data.get("model_id"))
        self.variables.set_builtin_var("api_key", input_data.get("api_key"))
        # 合并运行时变量
        if runtime_variables:
            for key, value in runtime_variables.items():
                self.variables.set_runtime_var(key, value)

        # 处理登录获取 token（如果配置了 auth_config）
        auth_config = config.get("auth_config", {})
        if auth_config and auth_config.get("login_url"):
            from app.services.auth_handler import AuthHandler
            response_parse_config = config.get("response_parse_config", {})
            login_token = AuthHandler.get_login_token_sync(auth_config, response_parse_config)
            if login_token:
                self.variables.set_runtime_var("accessToken", login_token)

        # 按 order 排序
        requests_config = sorted(requests_config, key=lambda x: x.get("order", 0))

        # 执行每个请求
        for req in requests_config:
            request_alias = req.get("alias") or req.get("name", f"request_{self._request_count + 1}")
            result = self._execute_single_request_sync(req, config, debug)

            if debug:
                logger.info(f"请求 [{request_alias}] 执行后，所有变量: {self.variables.get_all()}")

            # 检查是否继续执行
            if not result.get("success", False):
                return result

            # 解析多变量或单变量
            request_vars = {}
            output_vars = req.get("output_vars")
            output_var = req.get("output_var")
            is_sse_request = req.get("response_type") == "sse" or req.get("is_sse")

            if output_vars:
                # 多变量模式：从响应中解析多个变量
                response_data = result.get('response_data', {})
                request_vars = ResponseParser.parse_multiple(response_data, output_vars)
            elif output_var is not None:
                # 单变量模式
                request_vars = {output_var: result.get('content')}
            elif is_sse_request and result.get('content') is not None:
                # SSE 请求：自动提取 data_field
                response_parse = req.get('response_parse', {})
                data_field = response_parse.get('data_field')
                if data_field:
                    request_vars[data_field] = result.get('content')

            # 将请求变量存入 VariableContext
            for var_name, var_value in request_vars.items():
                self.variables.set_chain_var(request_alias, var_name, var_value)

        # 返回最后一个响应的数据
        final_response = result.get('response_data', {})
        content = None
        if isinstance(final_response, dict):
            for field in ["content", "data", "result", "response", "choices"]:
                if field in final_response:
                    content = final_response[field]
                    break
            if content is None:
                content = final_response

        if isinstance(content, list) and len(content) > 0:
            first_choice = content[0]
            if isinstance(first_choice, dict):
                for msg_field in ["message", "content"]:
                    if msg_field in first_choice:
                        content = first_choice[msg_field]
                        break

        if isinstance(content, dict) and "message" in content:
            content = content.get("message", {}).get("content", str(content))

        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)

        return {
            "success": True,
            "content": content or str(final_response),
            "raw": final_response,
        }

    def _execute_single_request_sync(
        self,
        req_config: Dict[str, Any],
        config: Dict[str, Any],
        debug: bool = False,
    ) -> Dict[str, Any]:
        """执行单个同步请求"""
        # 替换变量
        url = self._interpolate(req_config.get("url", ""))
        method = req_config.get("method", "POST").upper()

        # 获取请求体
        body = req_config.get("body")
        if body:
            body = self._interpolate_dict(body)

        # 获取请求头
        headers = req_config.get("headers", {})
        replaced_headers = {}
        for key, value in headers.items():
            replaced_headers[key] = self._interpolate(str(value))

        # 超时时间
        timeout = req_config.get("timeout", 120)

        try:
            if method == "GET":
                response = requests.get(
                    url,
                    params=body,
                    headers=replaced_headers,
                    timeout=timeout,
                )
            else:
                response = requests.request(
                    method,
                    url,
                    json=body,
                    headers=replaced_headers,
                    timeout=timeout,
                )

            if debug:
                logger.info(f"请求 {self._request_count + 1} 响应状态: {response.status_code}")

            # 检查状态码
            if response.status_code >= 400:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text[:500]}",
                    "status_code": response.status_code,
                }

            # 解析响应
            try:
                response_data = response.json()
            except json.JSONDecodeError:
                response_data = {"text": response.text}

            # 提取 content
            parse_config = req_config.get("response_parse", {})
            content = ResponseParser.parse(response_data, parse_config)

            return {
                "success": True,
                "response_data": response_data,
                "content": content,
                "status_code": response.status_code,
            }

        except requests.Timeout:
            return {"success": False, "error": "请求超时"}
        except requests.RequestException as e:
            return {"success": False, "error": str(e)}

    def _interpolate(self, template: str) -> str:
        """变量替换 - 支持 {{variable_name}} 和 [[variable_name]] 语法"""
        if not isinstance(template, str):
            return str(template)

        result = template

        # 处理 [[variable]] 格式（外部变量）
        external_pattern = r'\[\[(\w+)\]\]'
        for var_name in re.findall(external_pattern, result):
            value = self.variables.get(var_name)
            if value is not None:
                result = result.replace(f'[[{var_name}]]', str(value))

        # 处理 {{variable}} 格式（内部变量）
        for key, value in self.variables.get_all().items():
            placeholder = f"{{{{{key}}}}}"
            if placeholder in result:
                result = result.replace(placeholder, str(value))

        return result

    def _interpolate_dict(self, data: Any) -> Any:
        """对数据进行变量替换"""
        if data is None:
            return None
        elif isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if isinstance(value, str):
                    if '{{' in value or '${' in value or '[[' in value:
                        if re.fullmatch(r'\[\[\w+\]\]', value):
                            var_name = value[2:-2]
                            resolved = self.variables.get(var_name)
                            result[key] = resolved if resolved is not None else value
                        else:
                            result[key] = self._interpolate(value)
                    else:
                        result[key] = value
                elif isinstance(value, dict):
                    result[key] = self._interpolate_dict(value)
                elif isinstance(value, list):
                    result[key] = [self._interpolate(item) if isinstance(item, str) else self._interpolate_dict(item) for item in value]
                else:
                    result[key] = value
            return result
        elif isinstance(data, list):
            return [self._interpolate(item) if isinstance(item, str) else self._interpolate_dict(item) for item in data]
        elif isinstance(data, str):
            return self._interpolate(data)
        return data
