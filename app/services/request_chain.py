"""
请求链执行器
支持前置请求、主请求、后置请求的链式调用
支持动态变量生成
"""
import asyncio
import json
import re
import uuid
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
import httpx
from app.services.response_parser import ResponseParser
from app.services.variable_context import VariableContext

# 配置日志
logger = logging.getLogger(__name__)


class DynamicVariableGenerator:
    """动态变量生成器"""

    # 内置函数映射
    BUILTIN_FUNCTIONS = {
        'uuid': lambda: str(uuid.uuid4()),
        'uuid4': lambda: str(uuid.uuid4()),
        'timestamp': lambda: str(int(datetime.now().timestamp())),
        'datetime': lambda: datetime.now().isoformat(),
        'date': lambda: datetime.now().strftime('%Y-%m-%d'),
    }

    @staticmethod
    def generate(expression: str) -> str:
        """
        生成动态变量值

        Args:
            expression: 表达式，如 uuid(), timestamp(), ${uuid}

        Returns:
            生成的值
        """
        if not expression:
            return expression

        expression = expression.strip()

        # 处理 ${function_name()} 格式
        if expression.startswith('${') and expression.endswith('}'):
            func_name = expression[2:-1]
            if func_name in DynamicVariableGenerator.BUILTIN_FUNCTIONS:
                return DynamicVariableGenerator.BUILTIN_FUNCTIONS[func_name]()

        # 处理直接函数名格式 uuid() timestamp()
        for func_name, func in DynamicVariableGenerator.BUILTIN_FUNCTIONS.items():
            if expression == f'{func_name}':
                return func()

        return expression

    @staticmethod
    def extract_variables(text: str) -> List[str]:
        """
        从文本中提取动态变量引用

        Args:
            text: 文本内容

        Returns:
            变量名列表
        """
        variables = set()

        # 匹配 ${uuid}, ${timestamp} 等格式
        pattern = r'\$\{(\w+)\(\)\}'
        matches = re.findall(pattern, text)
        variables.update(matches)

        return list(variables)


class RequestChainExecutor:
    """请求链执行器"""

    # 内置变量，这些不需要用户输入（cookie 从登录配置中自动获取）
    BUILTIN_VARIABLES = {"messages", "token", "endpoint", "model_type", "model_id", "api_key", "cookie"}

    def __init__(self, chain_config: Dict[str, Any]):
        self.chain_config = chain_config
        self.variables: VariableContext = VariableContext()
        # 用于存储已生成的动态变量，确保在整个请求链中复用
        self._generated_dynamic_vars: Dict[str, str] = {}

    @staticmethod
    def extract_variables(chain_config: Dict[str, Any], auth_config: Optional[Dict[str, Any]] = None) -> List[str]:
        """
        从请求链配置中提取所有占位变量

        Args:
            chain_config: 请求链配置
            auth_config: 鉴权配置（包含 login_url 等，登录变量会从结果中排除）

        Returns:
            需要用户输入的变量名列表（排除output_var、output_vars定义的输出变量、以及登录变量）
        """
        variables = set()
        # 显式 [[var]] 外部变量（须用户手动输入）：始终保留，不参与 output_vars 排除
        external_vars = set()
        if not chain_config:
            return []

        # 收集登录变量（通过 auth_config 登录获取的变量）
        auth_vars = set()
        if auth_config and auth_config.get("login_url"):
            # 登录后会自动获取 accessToken 等变量
            auth_vars.add("accessToken")
            # 可以根据需要添加其他已知的登录变量名

        requests = chain_config.get("requests", [])

        # 收集所有输出变量（这些变量的值从响应中获取，不需要用户输入）
        output_vars = set()
        for req in requests:
            # 单变量模式
            output_var = req.get("output_var")
            if output_var:
                output_vars.add(output_var)
            # 多变量模式
            output_vars_list = req.get("output_vars", [])
            for var_config in output_vars_list:
                if isinstance(var_config, dict) and var_config.get("name"):
                    output_vars.add(var_config["name"])
            # SSE 请求的 data_field 也应该被排除（从响应中自动提取）
            response_parse = req.get("response_parse", {})
            data_field = response_parse.get("data_field")
            if data_field:
                output_vars.add(data_field)

        # 需要先按顺序处理请求，收集已定义的变量名
        defined_vars = set()
        all_requests_vars = {}  # alias -> set of var names

        for req in requests:
            alias = req.get("alias") or req.get("name", "")
            req_vars = set()

            # 从 url, headers, body, params 中提取
            for field in ["url", "headers", "body", "params"]:
                content = str(req.get(field, {}))

                # 匹配 [[variable_name]] 格式（外部变量，需要用户输入）
                external_matches = re.findall(r'\[\[(\w+)\]\]', content)
                for var_match in external_matches:
                    variables.add(var_match)
                    external_vars.add(var_match)

                # 匹配 {{alias.var}} 格式（请求链变量）
                alias_var_matches = re.findall(r'\{\{(\w+)\.(\w+)\}\}', content)
                for alias_match, var_match in alias_var_matches:
                    # 如果引用的别名是当前请求之前的某个请求，则需要用户输入
                    if alias_match not in all_requests_vars:
                        variables.add(f"{alias_match}.{var_match}")
                    # 如果是当前请求自己的变量，不需要输入（从响应获取）
                    elif alias_match == alias:
                        req_vars.add(var_match)

            # 记录当前请求的输出变量
            all_requests_vars[alias] = req_vars
            # 记录单变量输出
            if req.get("output_var"):
                all_requests_vars[alias].add(req.get("output_var"))
            # 记录多变量输出
            for var_config in req.get("output_vars", []):
                if isinstance(var_config, dict) and var_config.get("name"):
                    all_requests_vars[alias].add(var_config["name"])

        # 排除内置变量、输出变量和登录变量；但以 [[var]] 显式声明的外部变量始终保留
        return list(((variables - output_vars) | external_vars) - auth_vars)

    async def execute(
        self,
        input_data: Dict[str, Any],
        main_request_config: Dict[str, Any],
        runtime_variables: Optional[Dict[str, Any]] = None,
        debug: bool = False,
        request_callback: Optional[callable] = None,
    ) -> Dict[str, Any]:
        """
        执行请求链

        Args:
            input_data: 输入数据 {messages, ...}
            main_request_config: 主请求配置（用于未启用请求链时的回退）
            runtime_variables: 运行时变量（从任务配置中传入）
            debug: 是否返回详细的请求链执行信息
            request_callback: 每个请求执行完成后的回调函数，签名为 func(result: Dict[str, Any], request_info: Dict[str, Any])，返回True可继续执行

        Returns:
            最终结果 {success, content, ...}
        """
        if not self.chain_config.get("enabled"):
            # 未启用请求链，直接执行主请求
            return await self._execute_single_request(main_request_config, input_data)

        requests = self.chain_config.get("requests", [])
        if not requests:
            return await self._execute_single_request(main_request_config, input_data)

        # 按 order 排序
        requests = sorted(requests, key=lambda x: x.get("order", 0))

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
        auth_config = main_request_config.get("auth_config", {})
        if auth_config and auth_config.get("login_url"):
            from app.services.auth_handler import AuthHandler
            response_parse_config = main_request_config.get("response_parse_config", {})
            login_token = await AuthHandler.get_login_token(auth_config, response_parse_config)
            if login_token:
                # 将 token 设置到 runtime_variables，供 {{accessToken}} 引用
                self.variables.set_runtime_var("accessToken", login_token)

        # 用于存储每个请求的详细信息（调试模式）
        request_details = []
        # 用于存储每个请求的完整结果（chain_results）
        chain_results = []
        # 汇总所有变量，供直接引用
        all_variables = {}

        last_result = None
        for idx, req in enumerate(requests):
            # 获取请求别名
            request_alias = req.get("alias") or req.get("name", f"request_{idx + 1}")

            # 记录请求前的变量状态
            vars_before = self.variables.get_all()

            # 构建请求信息用于回调
            request_info = {
                "order": req.get("order", idx + 1),
                "name": req.get("name", f"请求{idx + 1}"),
                "alias": request_alias,
                "url": req.get("url", ""),
                "method": req.get("method", "POST"),
                "vars_before": vars_before,
                "request_body": req.get("body", {}),
                "headers": req.get("headers", {}),
                "request_params": req.get("params", {})
            }

            # 请求开始前触发回调
            if request_callback:
                should_continue = await request_callback(None, request_info)
                if should_continue is False:
                    return {"success": False, "error": "用户中断执行", "request_details": request_details}

            # 创建流式回调，包装原始回调用于单个请求内的流式输出
            stream_callback = None
            if request_callback:
                stream_callback = lambda chunk, info=request_info: request_callback({"type": "stream", "chunk": chunk}, info)
            result = await self._execute_request(req, stream_callback)

            # 记录请求后的变量状态
            vars_after = self.variables.get_all()
            logger.debug(f"请求 [{request_alias}] 执行后，所有变量: {vars_after}")

            # 更新为替换变量后的真实请求内容
            request_info["url"] = req.get("_processed_url", req.get("url", ""))
            request_info["headers"] = req.get("_processed_headers", req.get("headers", {}))
            request_info["request_body"] = req.get("_processed_body", req.get("body", {}))
            request_info["request_params"] = req.get("_processed_params", req.get("params", {}))

            # 解析多变量或单变量
            request_vars = {}
            output_vars = req.get("output_vars")
            output_var = req.get("output_var")
            is_sse_request = req.get("response_type") == "sse" or req.get("is_sse")

            # 获取原始响应数据
            raw_response = result.get('_raw_response', {})

            if output_vars:
                # 多变量模式：从响应中解析多个变量（支持 SSE 和 JSON）
                response_data = raw_response
                if isinstance(response_data, str):
                    import json
                    try:
                        response_data = json.loads(response_data)
                    except json.JSONDecodeError:
                        response_data = {}

                # SSE events 格式：[{"event": "xxx", "data": {...}}, ...]
                # 需要从 events 中提取 data 进行解析
                if is_sse_request and isinstance(response_data, list) and response_data and isinstance(response_data[0], dict) and "data" in response_data[0]:
                    # SSE 多变量模式：从每个 event 的 data 中提取对应字段
                    # 构建每个变量的提取结果列表
                    var_results = {v.get("name") if isinstance(v, dict) else v: [] for v in output_vars}

                    for event in response_data:
                        if isinstance(event, dict) and "data" in event:
                            event_data = event.get("data", {})
                            if isinstance(event_data, dict):
                                for v in output_vars:
                                    var_name = v.get("name") if isinstance(v, dict) else v
                                    var_results[var_name].append(event_data.get(var_name))

                    # 对每个变量，如果有多个值则拼接
                    for var_name, values in var_results.items():
                        if values:
                            # 过滤 None 值
                            non_none_values = [v for v in values if v is not None]
                            if non_none_values:
                                import json
                                # 如果是字典或列表，转为 JSON 字符串
                                if isinstance(non_none_values[0], (dict, list)):
                                    request_vars[var_name] = json.dumps(non_none_values, ensure_ascii=False)
                                else:
                                    request_vars[var_name] = "\n".join(str(v) for v in non_none_values)
                else:
                    # JSON 多变量模式：保持原有逻辑
                    if isinstance(output_vars, list) and output_vars and isinstance(output_vars[0], str):
                        # 简单格式：["var1", "var2"]，转换为完整格式
                        output_vars = [{"name": v, "parse_type": "json_path", "parse_rule": {"content": f"$.{v}"}} for v in output_vars]
                    request_vars = ResponseParser.parse_multiple(response_data, output_vars)
            elif output_var is not None:
                # 单变量模式（向后兼容）
                request_vars = {output_var: result.get('content')}
            elif is_sse_request and result.get('content') is not None:
                # SSE 请求：自动从 content 中提取 data_field 的值
                # SSE 请求的 content 已经是通过 data_field 提取的值
                response_parse = req.get('response_parse', {})
                data_field = response_parse.get('data_field')
                if data_field:
                    request_vars[data_field] = result.get('content')

            # 将请求变量存入 VariableContext
            for var_name, var_value in request_vars.items():
                self.variables.set_chain_var(request_alias, var_name, var_value)

            # 请求完成后触发回调
            if request_callback:
                request_info["response"] = result
                request_info["vars_after"] = vars_after
                request_info["output_var"] = output_var
                request_info["output_vars"] = output_vars
                request_info["output_value"] = result.get('content')
                request_info["request_vars"] = request_vars
                should_continue = await request_callback(result, request_info)
                if should_continue is False:
                    return {"success": False, "error": "用户中断执行", "request_details": request_details, "chain_results": chain_results}

            if debug:
                request_details.append({
                    "order": req.get("order", idx + 1),
                    "name": req.get("name", f"请求{idx + 1}"),
                    "url": req.get("_processed_url", req.get("url", "")),
                    "method": req.get("method", "POST"),
                    "vars_before": vars_before,
                    "headers": req.get("_processed_headers", req.get("headers", {})),
                    "request_body": req.get("_processed_body", req.get("body", {})),
                    "response": result,
                    "vars_after": vars_after,
                    "output_var": req.get("output_var"),
                    "output_vars": req.get("output_vars"),
                    "output_value": self.variables.get(req.get("output_var")) if req.get("output_var") else None,
                    "request_vars": request_vars
                })

            if not result.get("success"):
                logger.warning(f"[RequestChain] 请求失败 | alias={request_alias} | success={result.get('success')} | error={result.get('error')} | continue_on_error={req.get('continue_on_error', False)}")
                if not req.get("continue_on_error", False):
                    if debug:
                        result["request_details"] = request_details
                    result["chain_results"] = chain_results
                    logger.warning(f"[RequestChain] 请求失败且continue_on_error=False，立即返回 | alias={request_alias}")
                    return result
                # 继续执行下一个请求
                continue

            # 存储输出变量到 chain_results（每个请求都记录，保持索引与请求链顺序一致）
            request_result = {
                "order": req.get("order", idx + 1),
                "name": req.get("name", f"请求{idx + 1}"),
                "alias": request_alias,
                "content": result.get('content'),
                "variables": request_vars
            }
            chain_results.append(request_result)

            last_result = result

        # 返回完整结构
        if last_result:
            response = {
                "success": True,
                "content": last_result.get("content", ""),
                "chain_results": chain_results,
                "all_variables": self.variables.get_all(),
                "all_results": self.variables.get_all()  # 保持向后兼容
            }
            if debug:
                response["request_details"] = request_details
            return response

        logger.warning(f"[RequestChain] execute返回失败 | last_result={last_result} | chain_results={chain_results}")
        return {"success": False, "error": "请求链执行失败", "chain_results": chain_results}

    async def _execute_single_request(
        self,
        request_config: Dict[str, Any],
        input_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """执行单个请求（未启用请求链时使用）"""
        self.variables = {**input_data}

        # 构建请求
        endpoint = request_config.get("endpoint", "")
        method = request_config.get("method", "POST").upper()

        # 请求配置
        req_cfg = request_config.get("request_config", {})
        model_field = req_cfg.get("model_field", "model")
        messages_field = req_cfg.get("messages_field", "messages")

        headers = {}
        body = {
            model_field: request_config.get("model_type", "custom"),
            messages_field: self.variables.get("messages", [])
        }

        # 添加可选参数
        if "temperature" in req_cfg:
            body["temperature"] = req_cfg["temperature"]
        if "max_tokens" in req_cfg:
            body["max_tokens"] = req_cfg["max_tokens"]

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint,
                    json=body,
                    headers=headers,
                    timeout=req_cfg.get("timeout", 60),
                )

                if response.status_code == 200:
                    response_data = response.json()
                    parse_config = request_config.get("response_parse_config", {})
                    content = ResponseParser.parse(response_data, parse_config)
                    return {"success": True, "content": content}
                else:
                    return {"success": False, "error": f"请求失败: {response.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _execute_request(
        self,
        request_config: Dict[str, Any],
        stream_callback: Optional[callable] = None
    ) -> Dict[str, Any]:
        """执行单个请求

        Args:
            request_config: 请求配置
            stream_callback: 流式内容回调函数，签名为 func(chunk: Dict[str, Any])，返回True可继续接收
        """
        # 0. 保存原始 body 用于类型推断（在处理动态变量之前）
        original_body = request_config.get("body", {})

        # 0. 处理动态变量（生成 ${uuid()} 等表达式）
        self._generate_dynamic_variables(request_config)

        # 1. 变量替换
        url = self._interpolate(request_config.get("url", "")).strip()
        method = request_config.get("method", "POST").upper()
        headers = self._interpolate_dict(request_config.get("headers", {}))
        body = self._interpolate_dict(request_config.get("body", {}), original_body)
        params = self._interpolate_dict(request_config.get("params", {}))

        # 保存处理后的值到 request_config 中，供回调使用
        request_config["_processed_url"] = url
        request_config["_processed_headers"] = headers
        request_config["_processed_body"] = body
        request_config["_processed_params"] = params

        # 验证 URL 格式
        if not url:
            return {"success": False, "error": f"请求 {request_config.get('name', 'unknown')} 的 URL 为空，请检查变量配置"}
        if not url.startswith(("http://", "https://")):
            return {
                "success": False,
                "error": f"请求 {request_config.get('name', 'unknown')} 的 URL 无效（缺少 http:// 或 https:// 协议）: '{url}'，请检查 URL 模板中的变量是否已正确赋值"
            }

        # 获取重试配置
        max_retries = request_config.get("max_retries", 0)
        retry_interval = request_config.get("retry_interval", 1000)  # 毫秒

        # 获取超时配置：SSE流式响应需要更长的超时时间
        response_type = request_config.get("response_type", "json")
        if response_type == "sse":
            # SSE流式响应默认5分钟超时
            timeout = request_config.get("timeout", 3000)
        else:
            # 普通请求默认60秒超时
            timeout = request_config.get("timeout", 600)

        # 2. 发送请求（带重试机制）
        response = None
        last_error = None

        retry_count = max_retries + 1  # 初始请求 + 重试次数
        last_error = None

        for attempt in range(retry_count):
            try:
                # SSE类型使用流式请求（在请求过程中完成解析）
                if response_type == "sse":
                    parse_config = request_config.get("response_parse", {})
                    result = await self._send_stream_request(
                        method, url, headers, body, params, timeout,
                        parse_config=parse_config,
                        stream_callback=stream_callback
                    )
                    if result.get("success"):
                        content = result.get("content", "")
                        # SSE 请求也返回原始响应数据（如果可用）
                        return {"success": True, "content": content, "_raw_response": result.get("_raw_response", {})}
                    else:
                        last_error = result.get("error", "流式请求失败")
                        logger.error(f"SSE请求失败: {last_error}")
                else:
                    response = await self._send_request(method, url, headers, body, params, timeout)
                    # 检查响应是否成功
                    if response.status_code == 200:
                        # 3. 解析响应
                        parse_config = request_config.get("response_parse", {})

                        # 普通 JSON 响应解析
                        response_data = response.json()
                        content = ResponseParser.parse(response_data, parse_config)
                        if not content or content == 'None':
                            return {"success": False, "error": f"响应内容为空或解析失败 | parse_config={parse_config}", "content": response.text, "_raw_response": response_data}

                        return {"success": True, "content": content, "_raw_response": response_data}
                    else:
                        last_error = f"请求失败: {response.status_code}. {response.text}\n {body}"
                        logger.error(last_error)

                # 如果还有重试次数，等待后重试
                if attempt < retry_count - 1:
                    await asyncio.sleep(retry_interval / 1000)

            except Exception as e:
                last_error = str(e)
                logger.error(
                    f"请求执行失败 | 请求名称: {request_config.get('name', 'unknown')} | "
                    f"错误类型: {type(e).__name__} | "
                    f"错误信息: {last_error}"
                )
                if attempt < retry_count - 1:
                    await asyncio.sleep(retry_interval / 1000)

        # 所有尝试都失败
        error_detail = f"请求 {request_config.get('name', 'unknown')} 失败: {last_error or '未知错误'}"
        logger.error(f"请求最终失败 | 请求名称: {request_config.get('name', 'unknown')} | 错误: {error_detail}")
        return {
            "success": False,
            "error": error_detail
        }

    async def _parse_sse_response(
        self,
        response: httpx.Response,
        parse_config: Dict[str, Any],
        stream_callback: Optional[callable] = None
    ) -> str:
        """
        解析 SSE 流式响应

        Args:
            response: HTTP 响应对象
            parse_config: 解析配置，包含:
                - event_type: 要匹配的事件类型（如 CodexThreadStarted）
                - data_field: 要提取_id,的数据字段（如 thread test_points）
            stream_callback: 流式内容回调函数，每收到一个事件时调用

        Returns:
            提取的内容
        """
        import json

        event_type = parse_config.get("event_type", "")
        data_field = parse_config.get("data_field", "")

        # 收集所有事件
        events = []
        event_name = ""  # 初始化 event_name，避免 UnboundLocalError
        full_content = ""  # 用于累积完整内容

        # 优先使用流式迭代（如果支持）
        try:
            # 尝试读取原始流式内容
            text_lines = response.text.split('\n')
        except Exception:
            text_lines = []

        for line in text_lines:
            line = line.strip()
            if not line:
                continue

            # 解析 SSE 格式: event:xxx\ndata:xxx
            if line.startswith('event:'):
                event_name = line[6:].strip()
                continue
            elif line.startswith('data:'):
                data_content = line[5:].strip()
                try:
                    data = json.loads(data_content)
                    event_data = {
                        'event': event_name,
                        'data': data
                    }
                    events.append(event_data)

                    # 触发流式回调
                    if stream_callback:
                        should_continue = await stream_callback(event_data)
                        if should_continue is False:
                            break

                    # 提取数据字段内容
                    if data_field and isinstance(data, dict):
                        field_value = data.get(data_field)
                        if field_value is not None:
                            if isinstance(field_value, (dict, list)):
                                full_content += json.dumps(field_value, ensure_ascii=False)
                            else:
                                full_content += str(field_value)
                    elif event_name or not event_type:
                        # 如果没有指定data_field，尝试从整个data中提取
                        if isinstance(data, (dict, list)):
                            full_content += json.dumps(data, ensure_ascii=False)
                        else:
                            full_content += str(data)
                except json.JSONDecodeError:
                    events.append({
                        'event': event_name,
                        'data': data_content
                    })

        # 返回累积的内容（优先使用流式接收的内容）
        if full_content:
            return full_content

        # 如果指定了事件类型，筛选匹配的事件
        if event_type:
            matching_events = [e for e in events if e.get('event') == event_type]
        else:
            matching_events = events

        # 如果指定了数据字段，从事件数据中提取
        if data_field and matching_events:
            for event in matching_events:
                data = event.get('data', {})
                if isinstance(data, dict):
                    value = data.get(data_field)
                    if value is not None:
                        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)

        # 返回最后一个事件的数据
        if matching_events:
            last_data = matching_events[-1].get('data')
            if isinstance(last_data, (dict, list)):
                return json.dumps(last_data, ensure_ascii=False)
            return str(last_data)

        return ""

    async def _send_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Any,
        params: Dict[str, str],
        timeout: float = 60.0
    ) -> httpx.Response:
        """发送HTTP请求

        Args:
            method: HTTP方法
            url: 请求URL
            headers: 请求头
            body: 请求体
            params: URL参数
            timeout: 超时时间（秒），默认60秒
        """
        async with httpx.AsyncClient() as client:
            if method == "GET":
                return await client.get(url, headers=headers, params=params, timeout=timeout)
            elif method == "PUT":
                return await client.put(url, json=body, headers=headers, params=params, timeout=timeout)
            elif method == "DELETE":
                return await client.delete(url, headers=headers, params=params, timeout=timeout)
            elif method == "PATCH":
                return await client.patch(url, json=body, headers=headers, params=params, timeout=timeout)
            else:  # POST
                return await client.post(url, json=body, headers=headers, params=params, timeout=timeout)

    async def _send_stream_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Any,
        params: Dict[str, str],
        timeout: float = 300.0,
        parse_config: Optional[Dict[str, Any]] = None,
        stream_callback: Optional[callable] = None
    ) -> Dict[str, Any]:
        """发送流式HTTP请求（SSE等），并在请求过程中完成解析

        Returns:
            包含解析结果的字典 {success, content, status_code}
        """
        import json

        parse_config = parse_config or {}
        event_type_filter = parse_config.get("event_type", "")
        data_field = parse_config.get("data_field", "")

        events = []
        full_content = ""
        current_event_name = ""
        current_data = {}

        async with httpx.AsyncClient() as client:
            kwargs = {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout
            }
            if method in ["POST", "PATCH", "PUT"]:
                kwargs["json"] = body
            async with client.stream(**kwargs) as response:
                status_code = response.status_code
                # 详细日志：记录 SSE 响应状态
                if status_code >= 400:
                    # 读取错误响应体
                    try:
                        error_body = await response.aread()
                        logger.error(f"[RequestChain] SSE请求失败 | url={url} | status_code={status_code} | response_body={error_body.decode('utf-8', errors='replace')[:500]}")
                    except Exception as read_err:
                        logger.error(f"[RequestChain] 读取错误响应失败 | url={url} | status_code={status_code} | error={read_err}")
                try:
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            if current_data:
                                event_data = {'event': current_event_name, 'data': current_data}
                                events.append(event_data)

                                if stream_callback:
                                    should_continue = await stream_callback(event_data)
                                    if should_continue is False:
                                        break

                                if data_field and isinstance(current_data, dict):
                                    field_value = current_data.get(data_field)
                                    if field_value is not None:
                                        if isinstance(field_value, (dict, list)):
                                            full_content += json.dumps(field_value, ensure_ascii=False)
                                        else:
                                            full_content += str(field_value)
                                elif current_event_name or not event_type_filter:
                                    if isinstance(current_data, (dict, list)):
                                        full_content += json.dumps(current_data, ensure_ascii=False)
                                    else:
                                        full_content += str(current_data)

                                current_event_name = ""
                                current_data = {}
                            continue

                        if line.startswith('event:'):
                            current_event_name = line[6:].strip()
                        elif line.startswith('data:'):
                            data_content = line[5:].strip()
                            try:
                                current_data = json.loads(data_content)
                            except json.JSONDecodeError:
                                current_data = data_content

                except Exception as e:
                    logger.warning(f"SSE流式读取异常: {e}")
                    # 即使出现异常，也检查是否有已读取的内容
                    # 流关闭时可能已经有数据被读取
                    if not (full_content or events):
                        return {"success": False, "content": "", "status_code": status_code, "error": str(e)}

        # 处理结果
        if full_content:
            return {"success": True, "content": full_content, "status_code": status_code, "_raw_response": events}

        matching_events = [e for e in events if e.get('event') == event_type_filter] if event_type_filter else events

        if data_field and matching_events:
            for event in matching_events:
                data = event.get('data', {})
                if isinstance(data, dict):
                    value = data.get(data_field)
                    if value is not None:
                        content = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                        return {"success": True, "content": content, "status_code": status_code, "_raw_response": events}

        if matching_events:
            last_data = matching_events[-1].get('data')
            if isinstance(last_data, (dict, list)):
                return {"success": True, "content": json.dumps(last_data, ensure_ascii=False), "status_code": status_code, "_raw_response": events}
            return {"success": True, "content": str(last_data), "status_code": status_code, "_raw_response": events}

        return {"success": True, "content": "", "status_code": status_code, "_raw_response": events}

    async def _parse_sse_stream_response(
        self,
        response: httpx.Response,
        parse_config: Dict[str, Any],
        stream_callback: Optional[callable] = None
    ) -> str:
        """解析SSE流式响应（真正的流式读取）"""
        import json

        event_type_filter = parse_config.get("event_type", "")
        data_field = parse_config.get("data_field", "")

        events = []
        full_content = ""
        current_event_name = ""
        current_data = {}

        try:
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    if current_data:
                        event_data = {'event': current_event_name, 'data': current_data}
                        events.append(event_data)

                        if stream_callback:
                            should_continue = await stream_callback(event_data)
                            if should_continue is False:
                                break

                        if data_field and isinstance(current_data, dict):
                            field_value = current_data.get(data_field)
                            if field_value is not None:
                                if isinstance(field_value, (dict, list)):
                                    full_content += json.dumps(field_value, ensure_ascii=False)
                                else:
                                    full_content += str(field_value)
                        elif current_event_name or not event_type_filter:
                            if isinstance(current_data, (dict, list)):
                                full_content += json.dumps(current_data, ensure_ascii=False)
                            else:
                                full_content += str(current_data)

                        current_event_name = ""
                        current_data = {}
                    continue

                if line.startswith('event:'):
                    current_event_name = line[6:].strip()
                elif line.startswith('data:'):
                    data_content = line[5:].strip()
                    try:
                        current_data = json.loads(data_content)
                    except json.JSONDecodeError:
                        current_data = data_content

        except Exception as e:
            logger.error(f"SSE流式读取异常: {e}")

        if full_content:
            return full_content

        matching_events = [e for e in events if e.get('event') == event_type_filter] if event_type_filter else events

        if data_field and matching_events:
            for event in matching_events:
                data = event.get('data', {})
                if isinstance(data, dict):
                    value = data.get(data_field)
                    if value is not None:
                        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)

        if matching_events:
            last_data = matching_events[-1].get('data')
            if isinstance(last_data, (dict, list)):
                return json.dumps(last_data, ensure_ascii=False)
            return str(last_data)

        return ""

    def _generate_dynamic_variables(self, request_config: Dict[str, Any]) -> None:
        """
        处理动态变量，生成 ${uuid()} 等表达式的值
        已生成的变量会存储在 _generated_dynamic_vars 中，确保在整个请求链中复用

        Args:
            request_config: 请求配置
        """
        # 处理 body 中的动态变量
        body = request_config.get("body", {})
        if isinstance(body, dict):
            for key, value in body.items():
                if isinstance(value, str):
                    generated_value = self._generate_or_reuse_variable(value, key)
                    request_config["body"][key] = generated_value
                elif isinstance(value, dict):
                    self._generate_dynamic_variables_in_dict(value)
                elif isinstance(value, list):
                    request_config["body"][key] = self._generate_dynamic_variables_in_list(value)

        params = request_config.get("params", {})
        if isinstance(params, dict):
            for key, value in params.items():
                if isinstance(value, str):
                    generated_value = self._generate_or_reuse_variable(value, key)
                    request_config["params"][key] = generated_value
                elif isinstance(value, dict):
                    self._generate_dynamic_variables_in_dict(value)
                elif isinstance(value, list):
                    request_config["params"][key] = self._generate_dynamic_variables_in_list(value)

        # 处理 headers 中的动态变量
        headers = request_config.get("headers", {})
        if isinstance(headers, dict):
            for key, value in headers.items():
                if isinstance(value, str):
                    generated_value = self._generate_or_reuse_variable(value, key)
                    request_config["headers"][key] = generated_value

        # 处理 url 中的动态变量
        url = request_config.get("url", "")
        if isinstance(url, str):
            request_config["url"] = self._generate_or_reuse_variable(url)

    def _generate_or_reuse_variable(self, value: str, field_name: str = None) -> str:
        """
        生成动态变量值或复用已生成的值（按字段名单例）

        Args:
            value: 原始值，可能是 ${uuid()} 等表达式
            field_name: 字段名，用于作为缓存key，实现字段级别的单例

        Returns:
            生成的值或原值
        """
        if not value or not isinstance(value, str):
            return value

        # 检查是否是动态变量表达式 ${func_name()}
        if value.startswith('${') and value.endswith('}'):
            func_expr = value[2:-1]  # 例如 "uuid()"
            # 提取函数名，去除括号
            if func_expr.endswith('()'):
                func_name = func_expr[:-2]
            else:
                func_name = func_expr

            # 使用字段名作为缓存key，实现字段级别的单例
            # 如果没有字段名，则使用函数名作为key（向后兼容）
            cache_key = field_name if field_name else func_name

            # 检查是否已经生成过
            if cache_key in self._generated_dynamic_vars:
                return self._generated_dynamic_vars[cache_key]
            # 生成新值并缓存
            generated = DynamicVariableGenerator.generate(func_expr)
            self._generated_dynamic_vars[cache_key] = generated
            # 同时添加到变量池，供后续请求通过 {{field_name}} 引用
            if field_name:
                self.variables.set_runtime_var(field_name, generated)
            return generated

        # 处理直接函数名格式 uuid() timestamp()
        for func_name in DynamicVariableGenerator.BUILTIN_FUNCTIONS:
            if value == f'{func_name}()':
                cache_key = field_name if field_name else func_name
                if cache_key in self._generated_dynamic_vars:
                    return self._generated_dynamic_vars[cache_key]
                generated = DynamicVariableGenerator.generate(value)
                self._generated_dynamic_vars[cache_key] = generated
                if field_name:
                    self.variables.set_runtime_var(field_name, generated)
                return generated

        return value

    def _generate_dynamic_variables_in_dict(self, data: Dict[str, Any], parent_key: str = '') -> None:
        """递归处理字典中的动态变量"""
        for key, value in data.items():
            # 构建完整的字段名（如 user.id）
            full_key = f"{parent_key}.{key}" if parent_key else key

            if isinstance(value, str):
                generated_value = self._generate_or_reuse_variable(value, full_key)
                data[key] = generated_value
            elif isinstance(value, dict):
                self._generate_dynamic_variables_in_dict(value, full_key)
            elif isinstance(value, list):
                data[key] = self._generate_dynamic_variables_in_list(value, full_key)

    def _generate_dynamic_variables_in_list(self, data: List[Any], parent_key: str = '') -> List[Any]:
        """递归处理列表中的动态变量"""
        result = []
        for idx, item in enumerate(data):
            # 对于列表中的元素，使用索引作为key的一部分
            full_key = f"{parent_key}[{idx}]" if parent_key else f"item_{idx}"

            if isinstance(item, str):
                result.append(self._generate_or_reuse_variable(item, full_key))
            elif isinstance(item, dict):
                self._generate_dynamic_variables_in_dict(item, full_key)
                result.append(item)
            elif isinstance(item, list):
                result.append(self._generate_dynamic_variables_in_list(item, full_key))
            else:
                result.append(item)
        return result

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
            else:
                # 变量值为 None 或不存在，替换为空字符串并记录警告
                logger.warning(f"[RequestChain] 变量 {var_name} 的值为 None 或不存在，替换为空字符串")
                result = result.replace(f'[[{var_name}]]', "")

        # 处理 {{variable}} 格式（内部变量）
        for key, value in self.variables.get_all().items():
            # 替换 {{key}}
            placeholder = f"{{{{{key}}}}}"
            if placeholder in result:
                str_value = str(value)
                result = result.replace(placeholder, str_value)

        return result

    def _interpolate_with_type(self, template: str, key: str = "") -> Any:
        """
        变量替换，尝试保持原始类型。支持两种占位符格式：
          - {{varname}}：从模板提取 varname，在 self.variables 中查值
          - ${...}    ：使用外部传入的 key 在 self.variables 中查值

        Args:
            template: 模板字符串，如 "Bearer {{token}}"、"{{userId}}"、"${value}"
            key: ${...} 格式时用于检索变量的键名

        Returns:
            替换后的值（单一占位符时尽量保留原始类型）
        """
        import re
        if not isinstance(template, str):
            return template

        # --- [[varname]] 格式：外部变量（保持原始类型）---
        external_matches = re.findall(r'\[\[(\w+)\]\]', template)
        if external_matches:
            # 如果模板恰好是单个 [[varname]]，返回原始类型
            if re.fullmatch(r'\[\[\w+\]\]', template):
                var_name = external_matches[0]
                value = self.variables.get(var_name)
                if value is not None:
                    return value
            # 否则在字符串中替换
            for var_name in external_matches:
                value = self.variables.get(var_name)
                if value is not None:
                    template = template.replace(f'[[{var_name}]]', str(value))

        def _lookup(var_name: str) -> Any:
            return (self.variables.get(var_name)
                    or self.variables.get(var_name.lower())
                    or self.variables.get(var_name.upper()))

        # --- ${...} 格式：用外部传入的 key 检索 ---
        dollar_placeholders = re.findall(r'\$\{(\w+)\}', template)
        if dollar_placeholders and key:
            value = _lookup(key)
            if value is None:
                return template
            # 模板恰好等于单个 ${...} → 返回原始类型
            if len(dollar_placeholders) == 1 and re.fullmatch(r'\$\{\w+\}', template):
                return value
            result = template
            for ph in dollar_placeholders:
                result = result.replace(f"${{{ph}}}", str(value))
            return result

        # --- {{alias.var}} 格式：从模板提取 alias.var 查值 ---
        alias_var_pattern = r'\{\{(\w+)\.(\w+)\}\}'

        def _lookup_alias_var(alias: str, var_name: str) -> Any:
            key = f"{alias}.{var_name}"
            return self.variables.get(key)

        # --- {{varname}} 格式：从模板提取 varname 查值 ---
        placeholders = re.findall(r'\{\{(\w+)\}\}', template)
        # 同时提取 {{alias.var}} 格式
        alias_var_placeholders = re.findall(alias_var_pattern, template)

        if not placeholders and not alias_var_placeholders:
            return template

        # 单占位符且模板恰好等于该占位符 → 返回原始类型（int/float/dict/list 等）
        if len(placeholders) == 1:
            var_name = placeholders[0]
            value = _lookup(var_name)
            if value is not None and template == f"{{{{{var_name}}}}}":
                return value

        # 多占位符或有前后缀 → 逐个字符串替换
        result = template
        # 处理 {{alias.var}} 格式
        for alias, var_name in alias_var_placeholders:
            key = f"{alias}.{var_name}"
            value = self.variables.get(key)
            if value is not None:
                result = result.replace(f"{{{{{alias}.{var_name}}}}}", str(value))
        # 处理 {{varname}} 格式
        for var_name in placeholders:
            value = _lookup(var_name)
            if value is not None:
                result = result.replace(f"{{{{{var_name}}}}}", str(value))
        return result

    def _interpolate_dict(self, data: Any, original_data: Any = None) -> Any:
        """
        对数据进行变量替换，尝试保留原始类型

        Args:
            data: 要替换的数据
            original_data: 原始数据，用于获取原始类型信息
        """
        if data is None:
            return None
        elif isinstance(data, dict):
            result = {}
            orig_dict = original_data if isinstance(original_data, dict) else {}
            for key, value in data.items():
                orig_value = orig_dict.get(key)
                if isinstance(value, str):
                    # 检查是否需要变量替换
                    if '{{' in value or '${' in value or '[[' in value:
                        # 纯 [[varname]] 格式单独处理，保持原始类型
                        if re.fullmatch(r'\[\[\w+\]\]', value):
                            var_name = value[2:-2]
                            resolved = self.variables.get(var_name)
                            result[key] = resolved if resolved is not None else value
                        else:
                            result[key] = self._interpolate_with_type(value, key)
                    else:
                        result[key] = value
                elif isinstance(value, dict):
                    result[key] = self._interpolate_dict(value, orig_value if isinstance(orig_value, dict) else None)
                elif isinstance(value, list):
                    orig_list = orig_value if isinstance(orig_value, list) else None
                    result[key] = [
                        self._interpolate(v) if isinstance(v, str) else self._interpolate_dict(v, orig_list[i] if orig_list and i < len(orig_list) else None)
                        for i, v in enumerate(value)
                    ]
                else:
                    result[key] = value
            return result
        elif isinstance(data, list):
            return [
                self._interpolate(item) if isinstance(item, str) else self._interpolate_dict(item)
                for item in data
            ]
        elif isinstance(data, str):
            return self._interpolate(data)
        else:
            return data


class RequestChainExecutorSync:
    """同步版本的请求链执行器"""

    # 内置变量（cookie 从登录配置中自动获取）
    BUILTIN_VARIABLES = {"messages", "token", "endpoint", "model_type", "model_id", "api_key", "cookie"}

    def __init__(self, chain_config: Dict[str, Any]):
        self.chain_config = chain_config
        self.variables: Dict[str, Any] = {}
        self._executor: RequestChainExecutor = None

    @staticmethod
    def extract_variables(chain_config: Dict[str, Any], auth_config: Optional[Dict[str, Any]] = None) -> List[str]:
        """同步版本的提取变量方法"""
        return RequestChainExecutor.extract_variables(chain_config, auth_config)

    def execute(
        self,
        input_data: Dict[str, Any],
        main_request_config: Dict[str, Any],
        runtime_variables: Optional[Dict[str, Any]] = None,
        debug: bool = False,
        request_callback: Optional[callable] = None,
    ) -> Dict[str, Any]:
        """同步执行请求链

        Note: 同步版本不支持回调，回调仅在异步版本中有效
        """
        import asyncio
        import threading

        # 使用线程本地存储，为每个线程创建独立的事件循环和执行器
        thread_local = threading.local()

        # 为当前线程创建独立的事件循环
        if not hasattr(thread_local, 'loop') or thread_local.loop is None or thread_local.loop.is_closed():
            thread_local.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(thread_local.loop)

        # 为当前线程创建独立的执行器实例
        if not hasattr(thread_local, 'executor'):
            thread_local.executor = RequestChainExecutor(self.chain_config)

        loop = thread_local.loop
        executor = thread_local.executor

        try:
            return loop.run_until_complete(
                executor.execute(input_data, main_request_config, runtime_variables, debug, request_callback)
            )
        finally:
            # 不关闭事件循环，保持其可用状态
            pass

    def reset(self) -> None:
        """重置执行器状态，清除已生成的变量"""
        self._executor = None
        self.variables = {}
