"""LLM 调用退避重试工具（W2-3）

仅对瞬时性错误重试：超时、连接错误、HTTP 429/5xx。
4xx（鉴权失败、参数错误、模型不存在等）属确定性失败，重试无意义，不重试。

注意：超时重试可能造成上游双计费，属可接受代价
（见 docs/user_guide.md「评分口径与重试策略变更说明」）。
"""
import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

# 可重试的异常类名集合（openai / anthropic SDK 与 requests / httpx / 标准库的瞬时异常）。
# 按类名匹配而非硬 import：SDK 未安装时模块不因此失败，与 model_caller 的动态 import 面一致。
_RETRYABLE_EXC_TYPE_NAMES = {
    # openai SDK
    "APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError",
    # anthropic SDK（新旧版本类名带/不带 ErrorException 后缀）
    "APITimeoutErrorException", "APIConnectionErrorException",
    "RateLimitErrorException", "InternalServerErrorException",
    # requests
    "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError", "RetryError",
    # httpx
    "TimeoutException", "PoolTimeout", "ReadError", "RemoteProtocolError", "ConnectError",
    # 标准库
    "socket.timeout", "timeout",
}

# 可重试的 HTTP 状态码（429 限流 + 5xx 服务端瞬时错误）
_RETRYABLE_STATUS_PATTERN = re.compile(r"(?:^|[^0-9])(429|500|502|503|504)(?:[^0-9]|$)")

# 可重试的错误文本特征（大小写不敏感）
_RETRYABLE_TEXT_PATTERNS = re.compile(
    r"rate[ _-]?limit"
    r"|too many requests"
    r"|timed?[ _-]?out"
    r"|timeout"
    r"|connection"
    r"|connect"
    r"|service unavailable"
    r"|temporarily unavailable"
    r"|overloaded",
    re.IGNORECASE,
)


def is_retryable_llm_error(error: Union[BaseException, str]) -> bool:
    """判断一个错误（异常对象或错误文本）是否为可重试的瞬时性错误。

    Args:
        error: 异常对象（SDK/网络库抛出）或错误字符串（model_caller 各 provider
            统一包装后的 result["error"]，如 "请求失败: 429"）

    Returns:
        超时 / 连接错误 / HTTP 429/5xx → True；其余（4xx、业务错误、空值）→ False
    """
    if isinstance(error, BaseException):
        # 异常类型快路径：已知瞬时异常类名 / 标准库连接与超时基类
        if type(error).__name__ in _RETRYABLE_EXC_TYPE_NAMES:
            return True
        if isinstance(error, (ConnectionError, TimeoutError)):
            return True
        text = str(error) or ""
    elif isinstance(error, str):
        text = error
    else:
        return False

    if not text:
        return False
    # HTTP 状态码特征：429 / 5xx（如 "请求失败: 503"、"Error code: 429 - ..."）
    if _RETRYABLE_STATUS_PATTERN.search(text):
        return True
    # 文本特征：超时 / 连接 / 限流等
    return bool(_RETRYABLE_TEXT_PATTERNS.search(text))


def _next_delay(backoff: Optional[Sequence[float]], attempt: int) -> float:
    """第 attempt 次（0 起）重试前的退避秒数：越界取序列最后一个值，空序列不等待"""
    if not backoff:
        return 0.0
    idx = min(attempt, len(backoff) - 1)
    return float(backoff[idx])


def with_llm_retry(
    fn: Callable[[], Any],
    retries: int = 2,
    backoff: Tuple[float, ...] = (2, 8),
    sleep: Optional[Callable[[float], None]] = None,
) -> Any:
    """同步 LLM 调用重试包装：仅对可重试异常（超时/连接/429/5xx）退避重试。

    Args:
        fn: 无参可调用（用 lambda / closure 封装实际调用）
        retries: 额外重试次数（0 表示不重试，即只调用一次）
        backoff: 每次重试前的退避秒数序列，循环取用（缺省 (2, 8)：第 1 次重试等 2s，第 2 次等 8s）
        sleep: 等待函数，缺省 time.sleep；测试注入假时钟用

    Returns:
        fn 的返回值

    Raises:
        重试耗尽或异常不可重试时，抛出最后一次的异常
    """
    _sleep = sleep or time.sleep
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:
            if attempt >= retries or not is_retryable_llm_error(e):
                raise
            delay = _next_delay(backoff, attempt)
            attempt += 1
            logger.warning(
                f"[LLMRetry] 调用失败（{type(e).__name__}: {e}），"
                f"第 {attempt}/{retries} 次重试（退避 {delay}s）"
            )
            _sleep(delay)


async def with_llm_retry_async(
    fn: Callable[[], Awaitable[Any]],
    retries: int = 2,
    backoff: Tuple[float, ...] = (2, 8),
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> Any:
    """异步版重试包装（语义同 with_llm_retry，等待用 asyncio.sleep 不阻塞事件循环）"""
    _sleep = sleep or asyncio.sleep
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as e:
            if attempt >= retries or not is_retryable_llm_error(e):
                raise
            delay = _next_delay(backoff, attempt)
            attempt += 1
            logger.warning(
                f"[LLMRetry] 调用失败（{type(e).__name__}: {e}），"
                f"第 {attempt}/{retries} 次重试（退避 {delay}s）"
            )
            await _sleep(delay)
