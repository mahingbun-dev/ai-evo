"""P0-2：SDK 客户端进程级复用的单测

同一 (endpoint, api_key) 二次获取必须是同一实例（消除每次模型调用的
TCP+TLS 握手），不同键必须隔离；httpx.AsyncClient 按事件循环弱引用隔离。
"""
import pytest

import asyncio

from app.services import model_caller



pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）
def test_shared_openai_client_same_key_same_instance():
    a = model_caller._shared_openai_client("https://api.example.com/v1", "sk-1")
    b = model_caller._shared_openai_client("https://api.example.com/v1", "sk-1")
    assert a is b


def test_shared_openai_client_different_key_isolated():
    a = model_caller._shared_openai_client("https://api.example.com/v1", "sk-1")
    b = model_caller._shared_openai_client("https://api.example.com/v1", "sk-2")
    c = model_caller._shared_openai_client(None, "sk-1")
    assert a is not b
    assert a is not c


def test_shared_anthropic_client_cache_identity():
    a = model_caller._shared_anthropic_client("https://ant.example.com", "sk-ant-1")
    b = model_caller._shared_anthropic_client("https://ant.example.com", "sk-ant-1")
    other = model_caller._shared_anthropic_client("https://ant.example.com", "sk-ant-2")
    assert a is b
    assert a is not other


def test_shared_async_http_client_per_event_loop():
    """AsyncClient 绑定创建它的事件循环：同 loop 同实例，跨 loop 隔离"""

    async def _get_one():
        return model_caller._get_shared_async_http_client()

    loop1 = asyncio.new_event_loop()
    c1a = loop1.run_until_complete(_get_one())
    c1b = loop1.run_until_complete(_get_one())
    loop2 = asyncio.new_event_loop()
    c2 = loop2.run_until_complete(_get_one())
    try:
        assert c1a is c1b
        assert c1a is not c2
        assert c1a.is_closed is False
    finally:
        loop1.run_until_complete(c1a.aclose())
        loop2.run_until_complete(c2.aclose())
        loop1.close()
        loop2.close()


def test_shared_requests_session_singleton():
    a = model_caller._get_shared_requests_session()
    b = model_caller._get_shared_requests_session()
    assert a is b
    assert isinstance(a, __import__("requests").Session)

