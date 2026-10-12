"""AievoClient 单元测试：Bearer 鉴权注入、错误翻译、健康探测。

requests.Session 全部 mock，不发真实请求。
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.hub_plugin.client import (
    AievoApiError,
    AievoClient,
)


def _resp(status=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
        resp.text = text
    return resp


def test_bearer_header_carries_platform_token():
    """mas_token 组装成 Authorization: Bearer 头——与 ai-evo 鉴权中间件的契约。"""
    client = AievoClient("http://127.0.0.1:8100")
    session = MagicMock()
    session.request.return_value = _resp(200, {"ok": True})
    client._session = session

    out = client.request("GET", "/api/v1/evaluation/datasets", "tok-abc")

    assert out == {"status": 200, "data": {"ok": True}}
    kwargs = session.request.call_args.kwargs
    assert kwargs["headers"] == {"Authorization": "Bearer tok-abc"}
    assert kwargs["params"] is None
    assert kwargs["json"] is None


def test_timeout_translated_to_api_error():
    client = AievoClient("http://127.0.0.1:8100", timeout=5)
    session = MagicMock()
    session.request.side_effect = requests.exceptions.Timeout("t")
    client._session = session

    with pytest.raises(AievoApiError) as e:
        client.request("GET", "/api/v1/evaluation/tasks", "tok")
    assert e.value.error_code == "timeout"


def test_connection_error_translated_to_api_error():
    client = AievoClient("http://127.0.0.1:9999")
    session = MagicMock()
    session.request.side_effect = requests.exceptions.ConnectionError("c")
    client._session = session

    with pytest.raises(AievoApiError) as e:
        client.request("GET", "/api/v1/evaluation/tasks", "tok")
    assert e.value.error_code == "unreachable"


def test_non_json_body_returned_as_text():
    client = AievoClient("http://127.0.0.1:8100")
    session = MagicMock()
    session.request.return_value = _resp(500, text="Internal Server Error")
    client._session = session

    out = client.request("GET", "/api/v1/evaluation/datasets", "tok")
    assert out == {"status": 500, "data": "Internal Server Error"}


def test_base_url_trailing_slash_stripped():
    client = AievoClient("http://127.0.0.1:8100/")
    session = MagicMock()
    session.request.return_value = _resp(200, {})
    client._session = session
    client.request("GET", "/api/v1/evaluation/datasets", "tok")
    assert (
        session.request.call_args.args[1]
        == "http://127.0.0.1:8100/api/v1/evaluation/datasets"
    )


def test_health_probes_whitelisted_status_endpoint():
    client = AievoClient("http://127.0.0.1:8100")
    session = MagicMock()
    session.get.return_value = _resp(200, {"authenticated": False})
    client._session = session

    assert client.health() is True
    assert session.get.call_args.args[0] == "http://127.0.0.1:8100/api/v1/auth/status"


def test_health_false_on_failure():
    client = AievoClient("http://127.0.0.1:8100")
    session = MagicMock()
    session.get.side_effect = requests.exceptions.ConnectionError("down")
    client._session = session
    assert client.health() is False
