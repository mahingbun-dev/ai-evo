"""W2-3 LLM 调用退避重试单元测试（P0-B）

对齐《整改开发计划-20261013》§2 W2「测试设计」：
- 超时重试 2 次后成功；
- 4xx 不重试；
- 429/5xx 重试；
- 退避间隔断言（mock time.sleep）。

另覆盖：
- app/utils/llm_retry.is_retryable_llm_error 的错误判定矩阵；
- model_caller 两个分发入口（ModelCallerSync.call / ModelCaller.call）的重试收口
  与 {success, error} 结果契约不变（provider 方法零改动，经由分发入口统一获得重试）。

全部 mock，无真实网络/网关依赖。
"""
import asyncio
import time

import pytest

from app.services.model_caller import ModelCaller, ModelCallerSync
from app.utils.llm_retry import (
    is_retryable_llm_error,
    with_llm_retry,
    with_llm_retry_async,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------------------------------------------------------------------
# is_retryable_llm_error 判定矩阵
# ---------------------------------------------------------------------------
class TestIsRetryableLlmError:
    def test_timeout_exception_is_retryable(self):
        assert is_retryable_llm_error(TimeoutError("read timed out")) is True

    def test_connection_error_is_retryable(self):
        assert is_retryable_llm_error(ConnectionError("connection refused")) is True

    def test_sdk_exception_class_names_are_retryable(self):
        # openai / anthropic SDK 的瞬时异常按类名匹配（SDK 未安装也能识别）
        for exc_type_name in ("APITimeoutError", "APIConnectionError",
                              "RateLimitError", "InternalServerError"):
            exc_cls = type(exc_type_name, (Exception,), {})
            assert is_retryable_llm_error(exc_cls("boom")) is True

    def test_429_text_is_retryable(self):
        assert is_retryable_llm_error("请求失败: 429") is True
        assert is_retryable_llm_error("Error code: 429 - Too Many Requests") is True

    def test_5xx_text_is_retryable(self):
        for status in ("500", "502", "503", "504"):
            assert is_retryable_llm_error(f"Error code: {status} - server error") is True
        assert is_retryable_llm_error("请求失败: 503") is True

    def test_timeout_and_connection_text_is_retryable(self):
        assert is_retryable_llm_error("Read timed out.") is True
        assert is_retryable_llm_error("HTTPSConnectionPool: connection refused") is True
        assert is_retryable_llm_error("service unavailable") is True

    def test_4xx_text_is_not_retryable(self):
        assert is_retryable_llm_error("Error code: 401 - invalid api key") is False
        assert is_retryable_llm_error("Error code: 404 - model not found") is False
        assert is_retryable_llm_error("未配置 API Key") is False

    def test_status_code_edge_not_matched_by_digits(self):
        # 429 后紧跟数字不应误判为 429 状态码
        assert is_retryable_llm_error("request id=4291 failed") is False
        assert is_retryable_llm_error("job 14500 aborted") is False

    def test_business_error_is_not_retryable(self):
        assert is_retryable_llm_error(ValueError("参数不合法")) is False
        assert is_retryable_llm_error("模型返回格式错误") is False

    def test_empty_and_none_like_is_not_retryable(self):
        assert is_retryable_llm_error("") is False
        assert is_retryable_llm_error(Exception()) is False
        assert is_retryable_llm_error(None) is False


# ---------------------------------------------------------------------------
# with_llm_retry 同步语义
# ---------------------------------------------------------------------------
class _CallRecorder:
    """按预设脚本依次返回/抛出的可调用记录器"""

    def __init__(self, script):
        # script: 每项为 ("raise", exc) 或 ("return", value)
        self.script = list(script)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        action, payload = self.script.pop(0) if self.script else ("raise", RuntimeError("脚本耗尽"))
        if action == "raise":
            raise payload
        return payload


class TestWithLlmRetry:
    def test_timeout_retries_twice_then_succeeds(self):
        fn = _CallRecorder([
            ("raise", TimeoutError("timed out")),
            ("raise", TimeoutError("timed out")),
            ("return", "ok"),
        ])
        sleeps = []
        result = with_llm_retry(fn, retries=2, backoff=(2, 8), sleep=sleeps.append)

        assert result == "ok"
        assert fn.calls == 3
        # 退避序列断言（mock sleep）：第 1 次重试前等 2s，第 2 次等 8s
        assert sleeps == [2.0, 8.0]

    def test_4xx_error_does_not_retry(self):
        fn = _CallRecorder([("raise", ValueError("Error code: 401 - invalid api key"))])
        sleeps = []

        with pytest.raises(ValueError):
            with_llm_retry(fn, retries=2, backoff=(2, 8), sleep=sleeps.append)

        assert fn.calls == 1  # 立即抛出，无重试
        assert sleeps == []

    def test_429_error_retries_then_succeeds(self):
        fn = _CallRecorder([
            ("raise", Exception("Error code: 429 - Too Many Requests")),
            ("return", "ok"),
        ])
        sleeps = []

        result = with_llm_retry(fn, retries=2, backoff=(2, 8), sleep=sleeps.append)

        assert result == "ok"
        assert fn.calls == 2
        assert sleeps == [2.0]

    def test_5xx_error_retries(self):
        fn = _CallRecorder([
            ("raise", Exception("Error code: 503 - Service Unavailable")),
            ("return", "ok"),
        ])
        sleeps = []

        result = with_llm_retry(fn, retries=2, backoff=(2, 8), sleep=sleeps.append)

        assert result == "ok"
        assert fn.calls == 2

    def test_connection_refused_text_retries(self):
        fn = _CallRecorder([
            ("raise", Exception("connection refused")),
            ("return", "ok"),
        ])
        sleeps = []

        result = with_llm_retry(fn, retries=2, backoff=(2, 8), sleep=sleeps.append)

        assert result == "ok"
        assert fn.calls == 2

    def test_retry_exhausted_raises_last_error(self):
        fn = _CallRecorder([
            ("raise", TimeoutError("t1")),
            ("raise", TimeoutError("t2")),
            ("raise", TimeoutError("t3")),
        ])
        sleeps = []

        with pytest.raises(TimeoutError, match="t3"):
            with_llm_retry(fn, retries=2, backoff=(2, 8), sleep=sleeps.append)

        assert fn.calls == 3  # 1 次原始 + 2 次重试
        assert sleeps == [2.0, 8.0]

    def test_zero_retries_calls_once(self):
        fn = _CallRecorder([("raise", TimeoutError("t"))])

        with pytest.raises(TimeoutError):
            with_llm_retry(fn, retries=0, backoff=(2, 8), sleep=lambda _: None)

        assert fn.calls == 1

    def test_backoff_sequence_clamps_to_last_value(self):
        # retries=3、backoff=(1, 2)：第 3 次重试越界取序列最后一个值 2
        fn = _CallRecorder([
            ("raise", TimeoutError("t")),
            ("raise", TimeoutError("t")),
            ("raise", TimeoutError("t")),
            ("return", "ok"),
        ])
        sleeps = []

        result = with_llm_retry(fn, retries=3, backoff=(1, 2), sleep=sleeps.append)

        assert result == "ok"
        assert sleeps == [1.0, 2.0, 2.0]


# ---------------------------------------------------------------------------
# with_llm_retry_async 异步语义
# ---------------------------------------------------------------------------
class TestWithLlmRetryAsync:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_async_timeout_retries_then_succeeds(self):
        state = {"calls": 0}
        sleeps = []

        async def fn():
            state["calls"] += 1
            if state["calls"] < 3:
                raise TimeoutError("timed out")
            return "ok"

        async def fake_sleep(delay):
            sleeps.append(delay)

        async def scenario():
            return await with_llm_retry_async(fn, retries=2, backoff=(2, 8), sleep=fake_sleep)

        result = self._run(scenario())

        assert result == "ok"
        assert state["calls"] == 3
        assert sleeps == [2.0, 8.0]

    def test_async_4xx_does_not_retry(self):
        state = {"calls": 0}
        sleeps = []

        async def fn():
            state["calls"] += 1
            raise ValueError("Error code: 404 - not found")

        async def fake_sleep(delay):
            sleeps.append(delay)

        async def scenario():
            return await with_llm_retry_async(fn, retries=2, backoff=(2, 8), sleep=fake_sleep)

        with pytest.raises(ValueError):
            self._run(scenario())

        assert state["calls"] == 1
        assert sleeps == []

    def test_async_429_retries(self):
        state = {"calls": 0}

        async def fn():
            state["calls"] += 1
            if state["calls"] == 1:
                raise Exception("Error code: 429 - rate limited")
            return "ok"

        async def scenario():
            return await with_llm_retry_async(
                fn, retries=2, backoff=(2, 8), sleep=lambda _: asyncio.sleep(0))

        result = self._run(scenario())

        assert result == "ok"
        assert state["calls"] == 2


# ---------------------------------------------------------------------------
# model_caller 分发入口重试收口（W2-3 单点收口验证）
# ---------------------------------------------------------------------------
_CONFIG = {"provider": "http", "model_type": "mock-model", "endpoint": "http://mock", "api_key": "k"}
_MESSAGES = [{"role": "user", "content": "hi"}]


class TestModelCallerSyncRetryIntegration:
    def test_provider_503_result_retries_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        def fake_call_http(messages, config, runtime_variables):
            calls["n"] += 1
            if calls["n"] <= 2:
                return {"success": False, "error": "请求失败: 503"}
            return {"success": True, "content": "ok", "raw": {}}

        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(fake_call_http))
        sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        result = ModelCallerSync.call(_MESSAGES, _CONFIG, {})

        assert result["success"] is True
        assert result["content"] == "ok"
        assert calls["n"] == 3          # 1 次原始 + 2 次重试
        assert sleeps == [2.0, 8.0]     # 退避序列经分发入口生效

    def test_provider_401_result_no_retry(self, monkeypatch):
        calls = {"n": 0}

        def fake_call_http(messages, config, runtime_variables):
            calls["n"] += 1
            return {"success": False, "error": "Error code: 401 - invalid api key"}

        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(fake_call_http))
        sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        result = ModelCallerSync.call(_MESSAGES, _CONFIG, {})

        # 契约不变：失败返回 {success: False, error} dict 而非抛异常
        assert result == {"success": False, "error": "Error code: 401 - invalid api key"}
        assert calls["n"] == 1
        assert sleeps == []

    def test_provider_retry_exhausted_returns_original_failure_dict(self, monkeypatch):
        calls = {"n": 0}
        original_failure = {"success": False, "error": "请求失败: 429"}

        def fake_call_http(messages, config, runtime_variables):
            calls["n"] += 1
            return dict(original_failure)

        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(fake_call_http))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = ModelCallerSync.call(_MESSAGES, _CONFIG, {})

        # 重试耗尽：还原为原始失败 dict（结果契约不变），共 3 次尝试
        assert result == original_failure
        assert calls["n"] == 3

    def test_provider_non_retryable_exception_wrapped_without_retry(self, monkeypatch):
        calls = {"n": 0}

        def fake_call_http(messages, config, runtime_variables):
            calls["n"] += 1
            raise ValueError("参数不合法")

        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(fake_call_http))
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = ModelCallerSync.call(_MESSAGES, _CONFIG, {})

        # 与原 provider 内部 except Exception 包装语义一致
        assert result["success"] is False
        assert "参数不合法" in result["error"]
        assert calls["n"] == 1

    def test_provider_timeout_exception_retries_via_dispatch(self, monkeypatch):
        calls = {"n": 0}

        def fake_call_http(messages, config, runtime_variables):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise TimeoutError("read timeout")
            return {"success": True, "content": "ok", "raw": {}}

        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(fake_call_http))
        sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        result = ModelCallerSync.call(_MESSAGES, _CONFIG, {})

        assert result["success"] is True
        assert calls["n"] == 3
        assert sleeps == [2.0, 8.0]


class TestModelCallerAsyncRetryIntegration:
    def test_async_provider_429_retries_then_succeeds(self, monkeypatch):
        calls = {"n": 0}
        sleeps = []

        async def fake_call_http(messages, config, runtime_variables, debug=False):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"success": False, "error": "Error code: 429 - rate limited"}
            return {"success": True, "content": "ok", "raw": {}}

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(ModelCaller, "_call_http", staticmethod(fake_call_http))
        monkeypatch.setattr("asyncio.sleep", fake_sleep)

        result = asyncio.run(ModelCaller.call(_MESSAGES, _CONFIG))

        assert result["success"] is True
        assert result["content"] == "ok"
        assert calls["n"] == 2
        assert sleeps == [2.0]

    def test_async_provider_401_no_retry(self, monkeypatch):
        calls = {"n": 0}

        async def fake_call_http(messages, config, runtime_variables, debug=False):
            calls["n"] += 1
            return {"success": False, "error": "Error code: 401 - unauthorized"}

        monkeypatch.setattr(ModelCaller, "_call_http", staticmethod(fake_call_http))
        monkeypatch.setattr("asyncio.sleep", self._fail_sleep())

        result = asyncio.run(ModelCaller.call(_MESSAGES, _CONFIG))

        assert result == {"success": False, "error": "Error code: 401 - unauthorized"}
        assert calls["n"] == 1

    @staticmethod
    def _fail_sleep():
        async def fail_sleep(delay):
            raise AssertionError(f"不应发生退避等待，收到 sleep({delay})")
        return fail_sleep
