"""
通用模型调用服务
根据模型类型自动选择合适的调用方式
"""
import asyncio
import logging
import threading
import weakref
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
import requests

from app.utils.llm_retry import with_llm_retry, with_llm_retry_async, is_retryable_llm_error

logger = logging.getLogger(__name__)


# ==================== M6 QE6-3：模型调用 token 计量 ====================
#
# 被测模型调用（evaluation_executor）与裁判调用（judge_client 传统裁判图 /
# anthropic 裁判节点）全部经过本模块的 call() 漏斗——在漏斗处按响应 raw.usage
# 尽力提取 token 用量（OpenAI 兼容 prompt/completion_tokens、Anthropic
# input/output_tokens 两种形态统一），累计到进程内收集器；任务链路在
# run_evaluation_async 开始时 reset、收尾（complete/fail/stop record）collect
# 写入 task_execution_records.model_usage（见 evaluation_task_service）。
#
# 进程模型：celery worker 为 prefork 池（app/celery_app.py），一子进程同时
# 只跑一个任务；executor 内部 ThreadPoolExecutor 的行级并发线程共享同一
# 收集器（锁保护累加），计量不丢行。api 进程不经评测链路，收集器恒空。
# Claude Code 裁判走 SDK 子进程不过本漏斗，由 plugin_evaluator._cc_query 的
# usage_sink 就近喂 record_usage_totals（只有 total，无 prompt/completion 拆分）。

_USAGE_LOCK = threading.Lock()
_USAGE_ACCUMULATOR: Dict[str, Dict[str, int]] = {}


def record_usage(model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                 total_tokens: Optional[int] = None) -> None:
    """累计一次模型调用的 token 用量（线程安全；缺失维度按 0 记）"""
    if not model:
        return
    total = int(total_tokens if total_tokens is not None
                else (prompt_tokens + completion_tokens))
    with _USAGE_LOCK:
        slot = _USAGE_ACCUMULATOR.setdefault(model, {"prompt_tokens": 0,
                                                     "completion_tokens": 0,
                                                     "total_tokens": 0})
        slot["prompt_tokens"] += int(prompt_tokens or 0)
        slot["completion_tokens"] += int(completion_tokens or 0)
        slot["total_tokens"] += total


def record_usage_totals(model: str, total_tokens: int) -> None:
    """只有总量的调用（如 Claude Code SDK 上报）记总量，拆分维度留 0"""
    record_usage(model, total_tokens=total_tokens)


def reset_usage_tracking() -> None:
    """任务开始时清零（worker 子进程内一次一任务；跨任务残留即计量串账）"""
    with _USAGE_LOCK:
        _USAGE_ACCUMULATOR.clear()


def collect_usage() -> Dict[str, Dict[str, int]]:
    """收割当前累计（返回深拷贝；空 dict=无计量数据，落库按 NULL 口径）"""
    with _USAGE_LOCK:
        return {m: dict(v) for m, v in _USAGE_ACCUMULATOR.items()}


def _extract_usage(raw: Any) -> Optional[Dict[str, int]]:
    """从响应 raw 里尽力提取 usage（两种形态统一），无则 None（不猜值）"""
    if not isinstance(raw, dict):
        return None
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None

    def _num(v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    # OpenAI 兼容：prompt_tokens/completion_tokens/total_tokens
    if "prompt_tokens" in usage or "completion_tokens" in usage or "total_tokens" in usage:
        prompt = _num(usage.get("prompt_tokens"))
        completion = _num(usage.get("completion_tokens"))
        total = _num(usage.get("total_tokens")) or (prompt + completion)
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": total}
    # Anthropic 形态：input_tokens/output_tokens
    if "input_tokens" in usage or "output_tokens" in usage:
        prompt = _num(usage.get("input_tokens"))
        completion = _num(usage.get("output_tokens"))
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion}
    return None


def _meter_call_result(result: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """call() 漏斗收口：成功结果提取 usage → 进程内累计 + 附到返回值。

    尽力而为口径：raw 无 usage（自定义 HTTP 未返回/解析裁剪）则不记也不报错；
    返回值附 usage 供调用方行级归因（不改变既有 success/content/raw 契约）。
    """
    if not isinstance(result, dict) or not result.get("success"):
        return result
    usage = _extract_usage(result.get("raw"))
    if usage is None:
        return result
    model = str(config.get("model_type") or config.get("provider") or "unknown")
    record_usage(model, usage["prompt_tokens"], usage["completion_tokens"],
                 usage["total_tokens"])
    result["usage"] = {"model": model, **usage}
    return result


# 动态导入，避免未安装时报错
try:
    import openai
except ImportError:
    openai = None

try:
    import anthropic
except ImportError:
    anthropic = None


# ==================== P0-2 SDK 客户端复用 ====================
#
# 原实现每次模型调用 new openai.OpenAI / anthropic.Anthropic / httpx
# AsyncClient——1000 行任务 = 2000+ 次 TCP+TLS 握手（IO 面最大单点浪费）。
# openai/anthropic SDK 客户端官方支持线程安全复用，这里按 (endpoint,
# api_key) 键控进程级缓存；httpx.AsyncClient 按「当前事件循环」弱引用缓存
# （连接池绑定创建它的 loop，跨 loop 复用会报 Event loop is closed——
# 评测行线程各自 new_event_loop 的场景下必须按 loop 隔离，loop 关闭后
# 弱引用条目自动回收、连接随 loop 一起销毁，无需 atexit 显式清理）。

_client_cache_lock = threading.Lock()
_openai_clients: Dict[Tuple[str, str], Any] = {}
_anthropic_clients: Dict[Tuple[str, str], Any] = {}


def _shared_openai_client(endpoint: Optional[str], api_key: str):
    """(endpoint, api_key) 键控的 openai.OpenAI 进程级单例（线程安全）"""
    cache_key = (endpoint or "", api_key or "")
    with _client_cache_lock:
        client = _openai_clients.get(cache_key)
        if client is None:
            client_kwargs = {"api_key": api_key}
            if endpoint:
                client_kwargs["base_url"] = endpoint
            client = openai.OpenAI(**client_kwargs)
            _openai_clients[cache_key] = client
        return client


def _shared_anthropic_client(endpoint: Optional[str], api_key: str):
    """(endpoint, api_key) 键控的 anthropic.Anthropic 进程级单例（线程安全）"""
    cache_key = (endpoint or "", api_key or "")
    with _client_cache_lock:
        client = _anthropic_clients.get(cache_key)
        if client is None:
            client_kwargs = {"api_key": api_key}
            if endpoint:
                client_kwargs["base_url"] = endpoint
            client = anthropic.Anthropic(**client_kwargs)
            _anthropic_clients[cache_key] = client
        return client


# httpx.AsyncClient 按 loop 弱引用缓存：loop -> client（见上方注释）
_async_http_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _get_shared_async_http_client() -> httpx.AsyncClient:
    """当前事件循环共用的 httpx.AsyncClient（连接池限额 64/16）

    请求级超时由各调用点的 client.post(timeout=...) 覆盖，不依赖客户端
    默认值；limit 防行线程并发把对端打满。
    """
    loop = asyncio.get_running_loop()
    with _client_cache_lock:
        client = _async_http_clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=60.0,
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            )
            _async_http_clients[loop] = client
        return client


# 同步散装 HTTP 调用的进程级会话（urllib3 连接池线程安全；进程退出由 OS 回收）
_shared_requests_session: Optional[requests.Session] = None


def _get_shared_requests_session() -> "requests.Session":
    """进程级 requests.Session 单例（P0-2：同步 custom_http 路径免逐次握手）"""
    global _shared_requests_session
    with _client_cache_lock:
        if _shared_requests_session is None:
            _shared_requests_session = requests.Session()
        return _shared_requests_session


class _RetryableProviderFailure(Exception):
    """provider 返回可重试失败结果（如 HTTP 429/5xx）的内部信号

    各 provider 方法把异常统一包装为 {success: False, error} 字典返回（不抛异常），
    该内部异常只在重试包装层流转，用于把「可重试的失败结果」送入退避循环；
    耗尽后还原为原始结果字典，对外契约（失败返回 dict 而非抛异常）保持不变。
    """

    def __init__(self, result: Dict[str, Any]):
        super().__init__(result.get("error", "unknown provider failure"))
        self.result = result


def _execute_with_retry(invoke: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """同步 provider 调用单点收口（W2-3）

    超时 / 连接错误 / HTTP 429/5xx 等瞬时性失败经 with_llm_retry 退避重试；
    4xx 等确定性失败不重试。结果契约与旧实现一致：失败返回 {success: False, error}。
    """
    def _attempt() -> Dict[str, Any]:
        result = invoke()
        if isinstance(result, dict) and not result.get("success"):
            if is_retryable_llm_error(result.get("error", "") or ""):
                raise _RetryableProviderFailure(result)
        return result

    try:
        return with_llm_retry(_attempt)
    except _RetryableProviderFailure as e:
        return e.result
    except Exception as e:  # 与原 provider 内部 except Exception 包装语义一致
        logger.warning(f"[ModelCaller] 调用失败（不重试或重试耗尽）: {type(e).__name__}: {e}")
        return {"success": False, "error": str(e)}


async def _execute_with_retry_async(invoke: Callable[[], Awaitable[Dict[str, Any]]]) -> Dict[str, Any]:
    """异步 provider 调用单点收口（W2-3），语义同 _execute_with_retry"""
    async def _attempt() -> Dict[str, Any]:
        result = await invoke()
        if isinstance(result, dict) and not result.get("success"):
            if is_retryable_llm_error(result.get("error", "") or ""):
                raise _RetryableProviderFailure(result)
        return result

    try:
        return await with_llm_retry_async(_attempt)
    except _RetryableProviderFailure as e:
        return e.result
    except Exception as e:  # 与原 provider 内部 except Exception 包装语义一致
        logger.warning(f"[ModelCaller] 调用失败（不重试或重试耗尽）: {type(e).__name__}: {e}")
        return {"success": False, "error": str(e)}


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
        # 使用 provider 决定调用方式（W2-3：分发经 _execute_with_retry_async 单点收口，
        # 瞬时性失败（超时/连接/429/5xx）退避重试；4xx 确定性失败不重试）
        provider = (config.get("provider") or config.get("model_type") or "").lower()

        async def _dispatch() -> Dict[str, Any]:
            if provider == "openai":
                return await ModelCaller._call_openai(messages, config)
            elif provider == "anthropic":
                return await ModelCaller._call_anthropic(messages, config)
            else:
                return await ModelCaller._call_http(messages, config, runtime_variables, debug)

        # M6 QE6-3：漏斗收口计量（被测与裁判调用统一过这里）
        return _meter_call_result(await _execute_with_retry_async(_dispatch), config)

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
            # P0-2：客户端按 (endpoint, api_key) 进程级复用（SDK 客户端线程安全）
            client = _shared_openai_client(endpoint, api_key)

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
            # P0-2：客户端按 (endpoint, api_key) 进程级复用（SDK 客户端线程安全）
            client = _shared_anthropic_client(endpoint, api_key)

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
        # 使用 provider 决定调用方式（W2-3：分发经 _execute_with_retry 单点收口，
        # 瞬时性失败（超时/连接/429/5xx）退避重试；4xx 确定性失败不重试）
        provider = (config.get("provider") or config.get("model_type") or "").lower()

        def _dispatch() -> Dict[str, Any]:
            if provider == "openai":
                return ModelCallerSync._call_openai(messages, config)
            elif provider == "anthropic":
                return ModelCallerSync._call_anthropic(messages, config)
            else:
                return ModelCallerSync._call_http(messages, config, runtime_variables)

        # M6 QE6-3：漏斗收口计量（被测与裁判调用统一过这里）
        return _meter_call_result(_execute_with_retry(_dispatch), config)

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
            # P0-2：客户端按 (endpoint, api_key) 进程级复用（SDK 客户端线程安全）
            client = _shared_openai_client(endpoint, api_key)

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
