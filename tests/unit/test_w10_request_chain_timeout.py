"""W10-7 请求链超时默认值单元测试

整改计划 §3 W10 第 7 项：request_chain 请求超时默认值 600→60（json）、
3000→300（sse）——原默认远超实际需要，异常请求会长时间占死执行线程。
显式声明的 timeout 仍优先。
"""
import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.services.request_chain import RequestChainExecutor


class _FakeResponse:
    """最小响应假实现（_execute_request json 分支消费 status_code/json/text）"""

    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True}


def _executor() -> RequestChainExecutor:
    return RequestChainExecutor({"requests": []})


@pytest.mark.asyncio
async def test_json_default_timeout_is_60(monkeypatch):
    """response_type=json（缺省）：不显式声明 timeout 时默认 60 秒"""
    captured = {}

    async def _fake_send(method, url, headers, body, params, timeout):
        captured["timeout"] = timeout
        return _FakeResponse()

    executor = _executor()
    executor._send_request = _fake_send  # 实例属性打桩（绕过方法描述符的 self 绑定）
    result = await executor._execute_request({"name": "n", "url": "http://ok.local/x", "method": "GET"})

    assert result["success"] is True
    assert captured["timeout"] == 60, "json 请求默认超时应为 60s（W10-7：原 600s）"


@pytest.mark.asyncio
async def test_sse_default_timeout_is_300(monkeypatch):
    """response_type=sse：不显式声明 timeout 时默认 300 秒"""
    captured = {}

    async def _fake_stream(method, url, headers, body, params, timeout, parse_config=None, stream_callback=None):
        captured["timeout"] = timeout
        return {"success": True, "content": "sse-content"}

    executor = _executor()
    executor._send_stream_request = _fake_stream
    result = await executor._execute_request(
        {"name": "n", "url": "http://ok.local/stream", "method": "GET", "response_type": "sse"}
    )

    assert result["success"] is True
    assert captured["timeout"] == 300, "SSE 默认超时应为 300s（W10-7：原 3000s）"


@pytest.mark.asyncio
async def test_explicit_timeout_still_wins(monkeypatch):
    """显式声明 timeout：不受默认值调整影响（长耗时请求显式声明即可放宽）"""
    captured = {}

    async def _fake_send(method, url, headers, body, params, timeout):
        captured["timeout"] = timeout
        return _FakeResponse()

    executor = _executor()
    executor._send_request = _fake_send
    await executor._execute_request(
        {"name": "n", "url": "http://ok.local/x", "method": "GET", "timeout": 123}
    )
    assert captured["timeout"] == 123
