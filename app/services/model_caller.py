"""
通用模型调用服务
根据模型类型自动选择合适的调用方式
"""
from typing import List, Dict, Any, Optional
import httpx


# 动态导入，避免未安装时报错
try:
    import openai
except ImportError:
    openai = None

try:
    import anthropic
except ImportError:
    anthropic = None


class ModelCaller:
    """
    通用模型调用器

    支持的模型类型：
    - openai: 使用 OpenAI 官方库
    - anthropic: 使用 Anthropic 官方库
    - custom_http: 使用自定义 HTTP 请求
    """

    # 各模型的大致上下文窗口大小（tokens）
    CONTEXT_WINDOW_MAP = {
        "claude-3-opus": 200000,
        "claude-3-sonnet": 200000,
        "claude-3-haiku": 200000,
        "claude-2": 100000,
        "claude-instant": 100000,
        "gpt-4": 8192,
        "gpt-4-32k": 32768,
        "gpt-3.5-turbo": 16385,
    }

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """估算文本的 token 数量（粗略估算：中文约1.5字符/token，英文约4字符/token）"""
        if not text:
            return 0
        # 简单估算：按字符数/3 计算
        return len(text) // 3

    @staticmethod
    def _truncate_to_context_limit(
        messages: List[Dict[str, str]],
        max_context_tokens: int,
        reserved_tokens: int = 500,
    ) -> List[Dict[str, str]]:
        """
        截断消息以适应上下文窗口限制

        Args:
            messages: 消息列表
            max_context_tokens: 模型最大上下文 tokens
            reserved_tokens: 保留给输出的 tokens

        Returns:
            截断后的消息列表
        """
        available_tokens = max_context_tokens - reserved_tokens
        if available_tokens <= 0:
            available_tokens = 1500  # 默认最小值

        # 计算当前总 token 数
        total_tokens = 0
        for msg in messages:
            content = msg.get("content", "")
            total_tokens += ModelCaller._estimate_tokens(content)

        if total_tokens <= available_tokens:
            return messages

        # 需要截断，从后往前保留最新的消息
        truncated_messages = []
        current_tokens = 0

        # 倒序遍历，保留最新的消息
        for msg in reversed(messages):
            content = msg.get("content", "")
            msg_tokens = ModelCaller._estimate_tokens(content)

            if current_tokens + msg_tokens <= available_tokens:
                truncated_messages.insert(0, msg)
                current_tokens += msg_tokens
            else:
                # 截断此消息
                remaining_tokens = available_tokens - current_tokens
                if remaining_tokens > 100:  # 至少保留一些内容
                    # 按比例截断字符数
                    chars_to_keep = remaining_tokens * 3
                    truncated_content = content[-chars_to_keep:] if len(content) > chars_to_keep else content
                    truncated_msg = msg.copy()
                    truncated_msg["content"] = "...[已截断]..." + truncated_content
                    truncated_messages.insert(0, truncated_msg)
                break

        return truncated_messages

    @staticmethod
    def _get_model_context_window(model: str) -> int:
        """获取模型的上下文窗口大小"""
        model_lower = model.lower()
        for key, value in ModelCaller.CONTEXT_WINDOW_MAP.items():
            if key in model_lower:
                return value
        # 默认返回较小的值以确保安全
        return 2000

    @staticmethod
    async def call(
        messages: List[Dict[str, str]],
        config: Dict[str, Any],
        runtime_variables: Optional[Dict[str, Any]] = None,
        debug: bool = False,
    ) -> Dict[str, Any]:
        """
        调用模型

        Args:
            messages: 消息列表
            config: 模型配置，包含:
                - provider: 模型提供商 (openai/anthropic/custom_http)
                - model_type: 具体模型名称 (如 gpt-4、claude-3-opus-20240229)
                - api_key: API密钥
                - endpoint: API端点（可选）
                - auth_type: 鉴权类型
                - auth_config: 鉴权配置
                - request_config: 请求配置
                - response_parse_config: 响应解析配置
            runtime_variables: 运行时变量（用于请求链中的动态参数）
            debug: 是否返回详细的请求链执行信息

        Returns:
            调用结果，包含:
                - success: 是否成功
                - content: 响应内容
                - raw: 原始响应
                - error: 错误信息
        """
        # 使用 provider 决定调用方式
        provider = (config.get("provider") or config.get("model_type") or "").lower()

        if provider == "openai":
            return await ModelCaller._call_openai(messages, config)
        elif provider == "anthropic":
            return await ModelCaller._call_anthropic(messages, config)
        else:
            return await ModelCaller._call_http(messages, config, runtime_variables, debug)

    @staticmethod
    async def _call_openai(
        messages: List[Dict[str, str]],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """使用 OpenAI 官方库调用模型"""
        if openai is None:
            return {"success": False, "error": "openai 库未安装"}

        api_key = config.get("api_key")
        if not api_key:
            return {"success": False, "error": "未配置 API Key"}

        model = config.get("model_type", "gpt-4")
        endpoint = config.get("endpoint")
        request_config = config.get("request_config", {})

        try:
            # 设置客户端参数
            client_kwargs = {"api_key": api_key}
            if endpoint:
                client_kwargs["base_url"] = endpoint

            client = openai.OpenAI(**client_kwargs)

            # 构建请求参数
            kwargs = {
                "model": model,
                "messages": messages,
            }

            # 添加可选参数
            if "temperature" in request_config:
                kwargs["temperature"] = request_config["temperature"]
            if "max_tokens" in request_config:
                kwargs["max_tokens"] = request_config["max_tokens"]

            # 设置超时，防止无限等待
            timeout = request_config.get("timeout", 60)
            if isinstance(timeout, (int, float)):
                kwargs["timeout"] = timeout

            response = client.chat.completions.create(**kwargs)

            message = response.choices[0].message
            content = message.content

            # 兼容 OpenAI o1 系列的思考过程
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning:
                content = f"[思考过程]\n{reasoning}\n\n{content}" if content else f"[思考过程]\n{reasoning}"

            return {
                "success": True,
                "content": content,
                "reasoning": reasoning,
                "raw": response.model_dump() if hasattr(response, "model_dump") else str(response),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    async def _call_anthropic(
        messages: List[Dict[str, str]],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """使用 Anthropic 官方库调用模型"""
        if anthropic is None:
            return {"success": False, "error": "anthropic 库未安装"}

        api_key = config.get("api_key")
        if not api_key:
            return {"success": False, "error": "未配置 API Key"}

        model = config.get("model_type", "claude-3-opus-20240229")
        endpoint = config.get("endpoint")
        request_config = config.get("request_config", {})

        try:
            # 设置客户端参数
            client_kwargs = {"api_key": api_key}
            if endpoint:
                client_kwargs["base_url"] = endpoint

            client = anthropic.Anthropic(**client_kwargs)

            # 转换消息格式（OpenAI格式 -> Anthropic格式）
            anthropic_messages = []
            system_prompt = ""
            for msg in messages:
                if msg.get("role") == "system":
                    system_prompt = msg.get("content", "")
                else:
                    anthropic_messages.append({
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", "")
                    })

            # 检查并截断上下文长度，防止超过模型限制
            max_context = ModelCaller._get_model_context_window(model)
            # 合并 system 和最后一条消息进行截断检查
            combined = [{"role": "system", "content": system_prompt}] + anthropic_messages if system_prompt else anthropic_messages
            combined = ModelCaller._truncate_to_context_limit(combined, max_context)
            # 重新分离 system 和 messages
            if combined and combined[0].get("role") == "system":
                system_prompt = combined[0].get("content", "")
                anthropic_messages = combined[1:] if len(combined) > 1 else []

            # 构建请求参数
            kwargs = {
                "model": model,
                "messages": anthropic_messages,
                "max_tokens": request_config.get("max_tokens", 4096),
            }

            if system_prompt:
                kwargs["system"] = system_prompt
            if "temperature" in request_config:
                kwargs["temperature"] = request_config["temperature"]

            # 设置超时，防止无限等待
            timeout = request_config.get("timeout", 60)
            if isinstance(timeout, (int, float)):
                kwargs["timeout"] = timeout

            response = client.messages.create(**kwargs)

            # 处理响应内容，兼容思考过程（ThinkingBlock）
            content = ""
            thinking = ""
            raw_content = []

            if response.content:
                for block in response.content:
                    # 检查块的类型
                    block_type = getattr(block, "type", None)

                    if block_type == "thinking":
                        # 思考块 - 存储思考内容
                        thinking = getattr(block, "thinking", "")
                    elif block_type == "text":
                        # 文本块 - 存储文本内容
                        text = getattr(block, "text", "")
                        content += text
                        raw_content.append({"type": "text", "text": text})
                    else:
                        # 其他类型，尝试获取 text 属性
                        text = getattr(block, "text", str(block))
                        content += text
                        raw_content.append({"type": block_type or "unknown", "text": text})

            # 如果没有文本内容但有思考内容，返回思考内容
            if not content and thinking:
                content = f"[思考过程]\n{thinking}"

            return {
                "success": True,
                "content": content,
                "thinking": thinking if thinking else None,
                "raw": {
                    "content": raw_content,
                    "thinking": thinking,
                    "usage": response.usage.model_dump() if hasattr(response, "usage") else None,
                } if raw_content or thinking else response.model_dump() if hasattr(response, "model_dump") else str(response),
            }
            return {
                "success": True,
                "content": content,
                "raw": response.model_dump() if hasattr(response, "model_dump") else str(response),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    async def _call_http(
        messages: List[Dict[str, str]],
        config: Dict[str, Any],
        runtime_variables: Optional[Dict[str, Any]] = None,
        debug: bool = False,
    ) -> Dict[str, Any]:
        """使用自定义 HTTP 请求调用模型"""
        from app.services.auth_handler import AuthHandler
        from app.services.response_parser import ResponseParser
        from app.services.request_chain import RequestChainExecutor
        import logging
        logger = logging.getLogger(__name__)

        # 检查是否启用请求链
        request_config = config.get("request_config", {})
        chain_config = request_config.get("chain_requests", {})

        # 获取认证信息
        auth_type = config.get("auth_type", "api_key")
        auth_config = config.get("auth_config", {})
        response_parse_config = config.get("response_parse_config", {})
        model_id = config.get("model_id")

        if chain_config.get("enabled"):
            # 使用请求链执行器
            executor = RequestChainExecutor(chain_config)

            # 准备输入数据
            # 从 messages 中提取 user_input 作为 document_url，供请求链中的 {{document_url}} 变量使用
            document_url = ""
            if messages and isinstance(messages, list) and len(messages) > 0:
                first_message = messages[0]
                if isinstance(first_message, dict):
                    document_url = first_message.get("content", "")

            input_data = {
                "messages": messages,
                "endpoint": config.get("endpoint"),
                "model_type": config.get("model_type"),
                "request_config": request_config,
                "document_url": document_url,
            }

            import re as _re
            auth_info = AuthHandler.prepare_auth(auth_type, auth_config, response_parse_config, model_id)

            # 将认证信息注入变量，与测试模型（WebSocket接口）保持一致
            if auth_info.get("headers"):
                for key, value in auth_info["headers"].items():
                    if key.lower() == "authorization":
                        # 存储完整值（含 Bearer 前缀），兼容 {{authorization}}
                        input_data["authorization"] = value
                        # 同时去掉前缀存为 token，兼容 {{token}}
                        input_data["token"] = value[7:] if value.startswith("Bearer ") else value
                    elif key.lower() == "cookie":
                        input_data["cookie"] = value
                    else:
                        # 其他 header 转为合法变量名
                        var_name = _re.sub(r'[^a-zA-Z0-9_]', '_', key.lower())
                        input_data[var_name] = value

            result = await executor.execute(input_data, config, runtime_variables, debug)

            # 如果有request_details，提取每个请求的结果到chain_results格式
            if result.get("success") and result.get("request_details"):
                request_details = result["request_details"]
                chain_results = []
                for idx, detail in enumerate(request_details):
                    vars_after = detail.get("vars_after", {})
                    output_var_name = detail.get("output_var", "")
                    output_vars = detail.get("output_vars")  # 多变量模式
                    request_vars = detail.get("request_vars", {})  # 解析出的变量

                    # 合并变量：vars_after + request_vars（request_vars 优先级更高）
                    merged_vars = {**vars_after, **request_vars}

                    # 将 output_var 也添加到 merged_vars 中
                    output_value = detail.get("output_value", "")
                    if output_var_name and output_value:
                        merged_vars[output_var_name] = output_value

                    # 如果有 output_vars（多变量模式），添加到 merged_vars
                    if output_vars and isinstance(output_vars, list):
                        for var_name, var_value in request_vars.items():
                            merged_vars[var_name] = var_value

                    chain_results.append({
                        "chain_index": idx,
                        "order": detail.get("order", idx + 1),
                        "name": detail.get("name", f"请求{idx + 1}"),
                        "output_var": output_var_name,
                        "output_vars": output_vars,
                        "content": output_value,
                        "variables": merged_vars,  # 包含解析出的变量
                        "all_results": merged_vars
                    })
                result["chain_results"] = chain_results

            return result

        # 原有逻辑（未启用请求链）
        endpoint = config.get("endpoint")
        if not endpoint:
            return {"success": False, "error": "未配置端点"}

        # 准备鉴权
        auth_type = config.get("auth_type", "api_key")
        auth_config = config.get("auth_config", {})
        response_parse_config = config.get("response_parse_config", {})
        model_id = config.get("model_id")

        auth_info = AuthHandler.prepare_auth(auth_type, auth_config, response_parse_config, model_id)
        headers = auth_info.get("headers", {})
        params = auth_info.get("params", {})

        # 添加 API Key
        api_key = config.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 请求配置
        request_config = config.get("request_config", {})
        model_field = request_config.get("model_field", "model")
        messages_field = request_config.get("messages_field", "messages")
        model_type = config.get("model_type", "")

        payload = {
            model_field: model_type,
            messages_field: messages,
        }

        # 添加可选参数
        if "temperature" in request_config:
            payload["temperature"] = request_config["temperature"]
        if "max_tokens" in request_config:
            payload["max_tokens"] = request_config["max_tokens"]

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    params=params,
                    timeout=request_config.get("timeout", 60),
                )

                if response.status_code != 200:
                    return {
                        "success": False,
                        "error": f"请求失败: {response.status_code}",
                        "detail": response.text[:200],
                    }

                response_data = response.json()
                parse_config = config.get("response_parse_config", {})

                if parse_config:
                    content = ResponseParser.parse(response_data, parse_config)
                else:
                    # 默认解析
                    content = response_data.get("choices", [{}])[0].get("message", {}).get("content", "")

                return {
                    "success": True,
                    "content": content,
                    "raw": response_data,
                }

        except Exception as e:
            return {"success": False, "error": str(e)}


class ModelCallerSync:
    """
    同步版本的模型调用器（用于非异步场景）
    """

    @staticmethod
    def call(
        messages: List[Dict[str, str]],
        config: Dict[str, Any],
        runtime_variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """同步调用模型"""
        # 使用 provider 决定调用方式
        provider = (config.get("provider") or config.get("model_type") or "").lower()

        if provider == "openai":
            return ModelCallerSync._call_openai(messages, config)
        elif provider == "anthropic":
            return ModelCallerSync._call_anthropic(messages, config)
        else:
            return ModelCallerSync._call_http(messages, config, runtime_variables)

    @staticmethod
    def _call_openai(
        messages: List[Dict[str, str]],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """同步调用 OpenAI"""
        if openai is None:
            return {"success": False, "error": "openai 库未安装"}

        api_key = config.get("api_key")
        if not api_key:
            return {"success": False, "error": "未配置 API Key"}

        model = config.get("model_type", "gpt-4")
        endpoint = config.get("endpoint")
        request_config = config.get("request_config", {})

        try:
            client_kwargs = {"api_key": api_key}
            if endpoint:
                client_kwargs["base_url"] = endpoint

            client = openai.OpenAI(**client_kwargs)

            kwargs = {
                "model": model,
                "messages": messages,
            }

            if "temperature" in request_config:
                kwargs["temperature"] = request_config["temperature"]
            if "max_tokens" in request_config:
                kwargs["max_tokens"] = request_config["max_tokens"]

            # 设置超时，防止无限等待
            timeout = request_config.get("timeout", 60)
            if isinstance(timeout, (int, float)):
                kwargs["timeout"] = timeout

            response = client.chat.completions.create(**kwargs)

            message = response.choices[0].message
            content = message.content

            # 兼容 OpenAI o1 系列的思考过程
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning:
                content = f"[思考过程]\n{reasoning}\n\n{content}" if content else f"[思考过程]\n{reasoning}"

            return {
                "success": True,
                "content": content,
                "reasoning": reasoning,
                "raw": response.model_dump() if hasattr(response, "model_dump") else str(response),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _call_anthropic(
        messages: List[Dict[str, str]],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """同步调用 Anthropic"""
        if anthropic is None:
            return {"success": False, "error": "anthropic 库未安装"}

        api_key = config.get("api_key")
        if not api_key:
            return {"success": False, "error": "未配置 API Key"}

        model = config.get("model_type", "claude-3-opus-20240229")
        endpoint = config.get("endpoint")
        request_config = config.get("request_config", {})

        try:
            client_kwargs = {"api_key": api_key}
            if endpoint:
                client_kwargs["base_url"] = endpoint

            client = anthropic.Anthropic(**client_kwargs)

            anthropic_messages = []
            system_prompt = ""
            for msg in messages:
                if msg.get("role") == "system":
                    system_prompt = msg.get("content", "")
                else:
                    anthropic_messages.append({
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", "")
                    })

            # 检查并截断上下文长度，防止超过模型限制
            max_context = ModelCaller._get_model_context_window(model)
            combined = [{"role": "system", "content": system_prompt}] + anthropic_messages if system_prompt else anthropic_messages
            combined = ModelCaller._truncate_to_context_limit(combined, max_context)
            if combined and combined[0].get("role") == "system":
                system_prompt = combined[0].get("content", "")
                anthropic_messages = combined[1:] if len(combined) > 1 else []

            kwargs = {
                "model": model,
                "messages": anthropic_messages,
                "max_tokens": request_config.get("max_tokens", 4096),
            }

            if system_prompt:
                kwargs["system"] = system_prompt
            if "temperature" in request_config:
                kwargs["temperature"] = request_config["temperature"]

            # 设置超时，防止无限等待
            timeout = request_config.get("timeout", 60)
            if isinstance(timeout, (int, float)):
                kwargs["timeout"] = timeout

            response = client.messages.create(**kwargs)

            # 处理响应内容，兼容思考过程（ThinkingBlock）
            content = ""
            thinking = ""
            raw_content = []

            if response.content:
                for block in response.content:
                    block_type = getattr(block, "type", None)

                    if block_type == "thinking":
                        thinking = getattr(block, "thinking", "")
                    elif block_type == "text":
                        text = getattr(block, "text", "")
                        content += text
                        raw_content.append({"type": "text", "text": text})
                    else:
                        text = getattr(block, "text", str(block))
                        content += text
                        raw_content.append({"type": block_type or "unknown", "text": text})

            if not content and thinking:
                content = f"[思考过程]\n{thinking}"

            return {
                "success": True,
                "content": content,
                "thinking": thinking if thinking else None,
                "raw": {
                    "content": raw_content,
                    "thinking": thinking,
                    "usage": response.usage.model_dump() if hasattr(response, "usage") else None,
                } if raw_content or thinking else response.model_dump() if hasattr(response, "model_dump") else str(response),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _call_http(
        messages: List[Dict[str, str]],
        config: Dict[str, Any],
        runtime_variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """同步调用自定义 HTTP"""
        import requests
        from app.services.auth_handler import AuthHandler
        from app.services.response_parser import ResponseParser
        from app.services.request_chain import RequestChainExecutorSync

        # 检查是否启用请求链
        request_config = config.get("request_config", {})
        chain_config = request_config.get("chain_requests", {})

        if chain_config.get("enabled"):
            # 使用同步请求链执行器
            executor = RequestChainExecutorSync(chain_config)

            # 准备输入数据
            # 从 messages 中提取 user_input 作为 document_url，供请求链中的 {{document_url}} 变量使用
            document_url = ""
            if messages and isinstance(messages, list) and len(messages) > 0:
                first_message = messages[0]
                if isinstance(first_message, dict):
                    document_url = first_message.get("content", "")

            input_data = {
                "messages": messages,
                "endpoint": config.get("endpoint"),
                "model_type": config.get("model_type"),
                "request_config": request_config,
                "document_url": document_url,
            }

            # 获取认证信息并添加到变量
            auth_type = config.get("auth_type", "api_key")
            auth_config = config.get("auth_config", {})
            response_parse_config = config.get("response_parse_config", {})
            model_id = config.get("model_id")

            import re as _re
            auth_info = AuthHandler.prepare_auth(auth_type, auth_config, response_parse_config, model_id)

            # 将认证信息注入变量，与测试模型（WebSocket接口）保持一致
            if auth_info.get("headers"):
                for key, value in auth_info["headers"].items():
                    if key.lower() == "authorization":
                        # 存储完整值（含 Bearer 前缀），兼容 {{authorization}}
                        input_data["authorization"] = value
                        # 同时去掉前缀存为 token，兼容 {{token}}
                        input_data["token"] = value[7:] if value.startswith("Bearer ") else value
                    elif key.lower() == "cookie":
                        input_data["cookie"] = value
                    else:
                        # 其他 header 转为合法变量名
                        var_name = _re.sub(r'[^a-zA-Z0-9_]', '_', key.lower())
                        input_data[var_name] = value

            # 启用debug模式以获取每个请求的详细信息
            result = executor.execute(input_data, config, runtime_variables, debug=True)

            # 如果有request_details，提取每个请求的结果到chain_results格式
            if result.get("success") and result.get("request_details"):
                request_details = result["request_details"]
                chain_results = []
                for idx, detail in enumerate(request_details):
                    vars_after = detail.get("vars_after", {})
                    output_var_name = detail.get("output_var", "")
                    output_vars = detail.get("output_vars")  # 多变量模式
                    request_vars = detail.get("request_vars", {})  # 解析出的变量

                    # 合并变量：vars_after + request_vars（request_vars 优先级更高）
                    merged_vars = {**vars_after, **request_vars}

                    # 将 output_var 也添加到 merged_vars 中
                    output_value = detail.get("output_value", "")
                    if output_var_name and output_value:
                        merged_vars[output_var_name] = output_value

                    # 如果有 output_vars（多变量模式），添加到 merged_vars
                    if output_vars and isinstance(output_vars, list):
                        for var_name, var_value in request_vars.items():
                            merged_vars[var_name] = var_value

                    chain_results.append({
                        "chain_index": idx,
                        "order": detail.get("order", idx + 1),
                        "name": detail.get("name", f"请求{idx + 1}"),
                        "output_var": output_var_name,
                        "output_vars": output_vars,
                        "content": output_value,
                        "variables": merged_vars,  # 包含解析出的变量
                        "all_results": merged_vars
                    })
                result["chain_results"] = chain_results

            return result

        # 原有逻辑（未启用请求链）
        endpoint = config.get("endpoint")
        if not endpoint:
            return {"success": False, "error": "未配置端点"}

        auth_type = config.get("auth_type", "api_key")
        auth_config = config.get("auth_config", {})
        response_parse_config = config.get("response_parse_config", {})
        model_id = config.get("model_id")

        auth_info = AuthHandler.prepare_auth(auth_type, auth_config, response_parse_config, model_id)
        headers = auth_info.get("headers", {})
        params = auth_info.get("params", {})

        api_key = config.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        request_config = config.get("request_config", {})
        model_field = request_config.get("model_field", "model")
        messages_field = request_config.get("messages_field", "messages")
        model_type = config.get("model_type", "")

        payload = {
            model_field: model_type,
            messages_field: messages,
        }

        if "temperature" in request_config:
            payload["temperature"] = request_config["temperature"]
        if "max_tokens" in request_config:
            payload["max_tokens"] = request_config["max_tokens"]

        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                params=params,
                timeout=request_config.get("timeout", 60),
            )

            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"请求失败: {response.status_code}",
                    "detail": response.text[:200],
                }

            response_data = response.json()
            parse_config = config.get("response_parse_config", {})

            if parse_config:
                content = ResponseParser.parse(response_data, parse_config)
            else:
                content = response_data.get("choices", [{}])[0].get("message", {}).get("content", "")

            return {
                "success": True,
                "content": content,
                "raw": response_data,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
