"""
anc-hub 文件服务单元测试

MCP 客户端全部 mock（app.services.hub_file_service.requests），覆盖：
- 端点配置缺失中文报错、os.environ 优先、缺失时回退 Settings(.env)
- 内网自签证书跳过校验（verify=False）
- SSE 最后一个非空 data: 行解析、mcp-session-id 透传、DELETE 收尾
- 偶发空响应重试一次
- 分块下载落盘、sha256/size 双校验（含不符场景）、EOF 结束
- 缓存命中跳过下载 / 缓存损坏重新下载
- file_query 透传
- 评测取数路径 value_type=hub_file 字段自动落盘（阶段B重写：复用登记表持久
  解包区 hardlink 镜像替换，缺持久区抛 HubFileNotAvailable——细化用例见
  tests/unit/test_hub_resolve_execution.py）
"""
import base64
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import hub_file_service
from app.services.hub_file_service import (
    HUB_DOWNLOAD_CHUNK,
    HubEndpointNotConfiguredError,
    HubFileError,
    HubFileNotAvailable,
    HubFileService,
    HubMcpClient,
    _extract_tool_payload,
    cleanup_hub_files,
    extract_bearer_token,
    resolve_hub_bearer_token,
    get_hub_endpoint,
    get_hub_file_fields,
    resolve_hub_file_fields,
    sanitize_hub_name,
)

SESSION_ID = "sess-test-1"
ENDPOINT = "http://hub.test/mcp"
# 最小可解析的 Claude Code 会话 jsonl（供 zip 内层文件使用）
SESSION_JSONL = (
    '{"type":"user","sessionId":"s1","timestamp":"2026-01-01T00:00:00.000Z",'
    '"message":{"role":"user","content":"帮我跑测试"}}\n'
)


# ---------------------------------------------------------------------------
# mock 工具
# ---------------------------------------------------------------------------

class FakeResp:
    """模拟 requests.Response：content 是原始字节（UTF-8），text 不再被使用

    真实 requests 在 Content-Type 无 charset 时用 latin-1 解 .text（会把
    UTF-8 中文解成 mojibake），服务实现已改为只读 .content 显式 UTF-8 解码。
    """

    def __init__(self, text="", status_code=200, headers=None):
        if isinstance(text, bytes):
            self.content = text
        else:
            self.content = (text or "").encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}


def sse(obj) -> str:
    """SSE 响应体：含空 data: 心跳行与干扰行，客户端应取最后一个非空 data: 行"""
    return 'data: \ndata: {"ping": 1}\n\ndata: ' + json.dumps(obj) + "\n\n"


def rpc_result(id_, result) -> str:
    return sse({"jsonrpc": "2.0", "id": id_, "result": result})


def make_post(chunks, full_sha, full_size, filename, session_id=SESSION_ID, record=None):
    """构造 requests.post 假实现

    chunks: [(data_bytes, eof, overrides), ...] 按 offset 顺序返回
    每块 payload 均携带整文件的 sha256/size/filename（与真实服务端一致），
    overrides 可覆盖字段以构造校验失败场景。
    record: 可选列表，记录 (method, headers, msg)
    """
    state = {"offset": 0, "idx": 0}

    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        if record is not None:
            record.append((msg.get("method"), headers, msg))
        method = msg.get("method")
        if method == "initialize":
            return FakeResp(rpc_result(msg.get("id"), {"ok": True}),
                            headers={"mcp-session-id": session_id})
        if method == "notifications/initialized":
            return FakeResp("")
        if method == "tools/call":
            assert msg["params"]["name"] == "file-store__file_download"
            args = msg["params"]["arguments"]
            assert args.get("offset") == state["offset"], \
                f"offset 不连续: {args.get('offset')} != {state['offset']}"
            assert args.get("max_bytes") <= HUB_DOWNLOAD_CHUNK
            data_bytes, eof, overrides = chunks[state["idx"]]
            state["idx"] += 1
            state["offset"] += len(data_bytes)
            pl = {
                "ok": True,
                "data_base64": base64.b64encode(data_bytes).decode(),
                "bytes_read": len(data_bytes),
                "eof": eof,
                "sha256": full_sha,
                "size": full_size,
                "filename": filename,
            }
            if overrides:
                pl.update(overrides)
            return FakeResp(rpc_result(msg["id"], {"structuredContent": {"ok": True, "payload": pl}}))
        raise AssertionError(f"未预期的 MCP 方法: {method}")

    return _post


def make_zip_bytes(entries):
    """构造 zip 字节流：entries: [(zip 内路径, 内容 bytes), ...]"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


def retry_empty_once(post_fn, empty_methods=("tools/call",)):
    """对指定方法首次返回空响应（模拟偶发空响应），之后正常"""
    seen = {}

    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        method = msg.get("method")
        if method in empty_methods:
            seen[method] = seen.get(method, 0) + 1
            if seen[method] == 1:
                return FakeResp("")
        return post_fn(url, data=data, headers=headers, timeout=timeout)

    return _post


@pytest.fixture
def cache_root(tmp_path):
    return str(tmp_path / "hub_files")


def make_service(cache_root, **kw):
    return HubFileService(endpoint=ENDPOINT, cache_root=cache_root, **kw)


# ---------------------------------------------------------------------------
# 端点配置
# ---------------------------------------------------------------------------

class TestGetHubEndpoint:
    @staticmethod
    def _patch_settings(monkeypatch, endpoint: str):
        """get_settings 是 lru_cache 且会读到真实 .env，测试必须打桩"""
        monkeypatch.setattr(
            hub_file_service, "get_settings",
            lambda: SimpleNamespace(ANC_HUB_MCP_ENDPOINT=endpoint),
        )

    def test_missing_env_raises_chinese_error(self, monkeypatch):
        """环境变量与 Settings(.env) 均缺失时抛中文错误"""
        monkeypatch.delenv(hub_file_service.HUB_ENDPOINT_ENV, raising=False)
        self._patch_settings(monkeypatch, "")
        with pytest.raises(HubEndpointNotConfiguredError) as exc:
            get_hub_endpoint()
        assert "ANC_HUB_MCP_ENDPOINT" in str(exc.value)
        assert "未配置" in str(exc.value)

    def test_blank_env_raises(self, monkeypatch):
        monkeypatch.setenv(hub_file_service.HUB_ENDPOINT_ENV, "   ")
        self._patch_settings(monkeypatch, "")
        with pytest.raises(HubEndpointNotConfiguredError):
            get_hub_endpoint()

    def test_env_value_returned(self, monkeypatch):
        """os.environ 优先于 Settings(.env)"""
        monkeypatch.setenv(hub_file_service.HUB_ENDPOINT_ENV, "http://10.1.1.1:9999/mcp")
        self._patch_settings(monkeypatch, "http://from-settings:1/mcp")
        assert get_hub_endpoint() == "http://10.1.1.1:9999/mcp"

    def test_settings_fallback_when_env_missing(self, monkeypatch):
        """环境变量缺失时回退 Settings(.env) 中的端点"""
        monkeypatch.delenv(hub_file_service.HUB_ENDPOINT_ENV, raising=False)
        self._patch_settings(monkeypatch, "https://hub.from.env/hub-api/mcp")
        assert get_hub_endpoint() == "https://hub.from.env/hub-api/mcp"

    def test_blank_env_falls_back_to_settings(self, monkeypatch):
        """环境变量为空白时同样回退 Settings"""
        monkeypatch.setenv(hub_file_service.HUB_ENDPOINT_ENV, "   ")
        self._patch_settings(monkeypatch, "https://hub.from.env/hub-api/mcp")
        assert get_hub_endpoint() == "https://hub.from.env/hub-api/mcp"


class TestTlsVerify:
    """TLS 证书校验默认开启；仅显式 AIEVO_HUB_TLS_INSECURE=1 时跳过
    （原实现无条件 verify=False，存在中间人风险，2026-10 起改为 opt-in）"""

    ENDPOINT_HTTPS = "https://hub.test:8081/hub-api/mcp"

    def test_post_verifies_by_default(self, monkeypatch):
        calls = {}

        def fake_post(url, **kwargs):
            calls.update(kwargs)
            calls["url"] = url
            return FakeResp(text=rpc_result(1, {}), headers={})

        monkeypatch.setattr(hub_file_service.requests, "post", fake_post)
        client = HubMcpClient(self.ENDPOINT_HTTPS)
        client._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert calls.get("verify") is True

    def test_close_verifies_by_default(self, monkeypatch):
        calls = {}

        def fake_delete(url, **kwargs):
            calls.update(kwargs)
            calls["url"] = url
            return FakeResp()

        monkeypatch.setattr(hub_file_service.requests, "delete", fake_delete)
        client = HubMcpClient(self.ENDPOINT_HTTPS)
        client.session_id = SESSION_ID
        client.close()
        assert calls.get("verify") is True

    def test_insecure_opt_in_skips_verify(self, monkeypatch):
        calls = {}

        def fake_post(url, **kwargs):
            calls.update(kwargs)
            return FakeResp(text=rpc_result(1, {}), headers={})

        monkeypatch.setattr(hub_file_service.requests, "post", fake_post)
        monkeypatch.setenv("AIEVO_HUB_TLS_INSECURE", "1")
        client = HubMcpClient(self.ENDPOINT_HTTPS)
        client._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert calls.get("verify") is False


# ---------------------------------------------------------------------------
# MCP 客户端回归：UTF-8 解码与 SSE 行界（线上真实事故的固化用例）
# ---------------------------------------------------------------------------

class TestHubMcpClientUtf8Regression:
    """线上事故：登录闸门拒绝卡含中文，requests 的 .text 按 latin-1 解码
    UTF-8 正文，「先」字（E5 85 88）中的 0x85 被解成 U+0085（NEL），又被
    str.splitlines() 当作行界，data 行在 JSON 字符串中途截断、报
    Unterminated string。实现改为 resp.content 显式 UTF-8 解码 +
    split("\\n") 后，在此固化该行为。"""

    @staticmethod
    def _reject_sse_bytes() -> bytes:
        """构造与线上同构的响应：SSE data 行内嵌含中文的登录拒绝卡"""
        card = {
            "decision": "reject",
            "login": {"outcome": "client-unsupported", "required": True},
            "message": "需要先建立登录身份：请调用 login 工具完成登录，然后重试本次调用。",
            "status": "rejected",
        }
        rpc = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "content": [{"type": "text", "text": json.dumps(card, ensure_ascii=False)}],
                "isError": False,
            },
        }
        # content 为真实 UTF-8 字节（「先」等多字节序列内含 0x85 字节）
        return (
            "data: \nid: 0\nretry: 3000\n\ndata: "
            + json.dumps(rpc, ensure_ascii=False)
            + "\nid: 1/0\n\n"
        ).encode("utf-8")

    @patch("app.services.hub_file_service.requests")
    def test_chinese_payload_not_truncated(self, mock_requests):
        """含中文的 SSE data 行完整解析，不再中途截断"""
        mock_requests.post.return_value = FakeResp(text=self._reject_sse_bytes())
        with HubMcpClient(ENDPOINT) as cli:
            result = cli.call("tools/call", {})
        text = result["content"][0]["text"]
        assert "需要先建立登录身份" in text
        assert "client-unsupported" in text

    @patch("app.services.hub_file_service.requests")
    def test_unicode_line_separator_in_json_string(self, mock_requests):
        """JSON 字符串内字面 U+2028/U+2029/U+0085 是合法 JSON，不得被当 SSE 行界拆断"""
        raw = "a\u2028b\u2029c\u0085d"  # splitlines 视作行界、JSON 允许字面出现
        obj = {"jsonrpc": "2.0", "id": 1, "result": {"msg": raw}}
        body = ("data: " + json.dumps(obj, ensure_ascii=False)).encode("utf-8")
        decoded = body.decode("utf-8")
        assert "\u2028" in decoded and "\u2029" in decoded  # 前置确认 fixture 含行界字符
        mock_requests.post.return_value = FakeResp(text=body)
        with HubMcpClient(ENDPOINT) as cli:
            result = cli.call("m")
        assert result["msg"] == raw

    @patch("app.services.hub_file_service.requests")
    def test_crlf_sse_lines(self, mock_requests):
        """SSE 以 \\r\\n 结行时同样取到最后一个非空 data: 行"""
        body = (
            "data: \r\n\r\ndata: "
            + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
            + "\r\n\r\n"
        ).encode("utf-8")
        mock_requests.post.return_value = FakeResp(text=body)
        with HubMcpClient(ENDPOINT) as cli:
            assert cli.call("m") == {"ok": True}


# ---------------------------------------------------------------------------
# Bearer 凭证（同域部署：平台登录态 cookie → Authorization 头；cookie 名可配置）
# ---------------------------------------------------------------------------

class TestBearerToken:
    @patch("app.services.hub_file_service.requests")
    def test_bearer_header_on_all_requests(self, mock_requests):
        """带凭证时 POST 与 DELETE 收尾全部携带 Authorization: Bearer"""
        mock_requests.post.return_value = FakeResp(
            text=rpc_result(1, {"ok": True}),
            headers={"mcp-session-id": SESSION_ID},
        )
        mock_requests.delete.return_value = FakeResp("")
        with HubMcpClient(ENDPOINT, bearer_token="mt_xxx") as cli:
            cli.call("initialize", {})
            cli.call("notifications/initialized", notify=True)
            cli.call("tools/call", {})
        assert mock_requests.post.call_count == 3
        for c in mock_requests.post.call_args_list:
            assert c.kwargs["headers"]["Authorization"] == "Bearer mt_xxx"
        assert (
            mock_requests.delete.call_args.kwargs["headers"]["Authorization"]
            == "Bearer mt_xxx"
        )

    @patch("app.services.hub_file_service.requests")
    def test_no_bearer_no_auth_header(self, mock_requests):
        """无凭证时不产生 Authorization 头（匿名调用）"""
        mock_requests.post.return_value = FakeResp(text=rpc_result(1, {"ok": True}))
        with HubMcpClient(ENDPOINT) as cli:
            cli.call("m")
        headers = mock_requests.post.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    @patch("app.services.hub_file_service.requests")
    def test_service_bearer_passthrough_to_query(self, mock_requests):
        """HubFileService(bearer_token=...) → query_files 全链路带 Bearer 头（含首尾空白清理）"""
        files = {"ok": True, "files": []}
        mock_requests.post.side_effect = [
            FakeResp(rpc_result(1, {"ok": True}), headers={"mcp-session-id": SESSION_ID}),
            FakeResp(""),  # initialized notify
            FakeResp(rpc_result(2, {"structuredContent": files})),
        ]
        svc = HubFileService(endpoint=ENDPOINT, cache_root="/tmp/unused", bearer_token=" mt_cookie ")
        result = svc.query_files(page_size=5)
        assert result["ok"] is True
        auths = [c.kwargs["headers"].get("Authorization") for c in mock_requests.post.call_args_list]
        assert auths == ["Bearer mt_cookie"] * 3

    @patch("app.services.hub_file_service.requests")
    def test_service_bearer_passthrough_to_download(self, mock_requests, tmp_path):
        """download_to_file 同样携带 Bearer 头"""
        payload = b"hello"
        mock_requests.post.side_effect = [
            FakeResp(rpc_result(1, {"ok": True}), headers={"mcp-session-id": SESSION_ID}),
            FakeResp(""),
            FakeResp(rpc_result(2, {"structuredContent": {"ok": True, "payload": {
                "ok": True, "data_base64": base64.b64encode(payload).decode(),
                "bytes_read": len(payload), "eof": True,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload), "filename": "f.bin",
            }}})),
        ]
        svc = HubFileService(endpoint=ENDPOINT, cache_root=str(tmp_path), bearer_token="mt_dl")
        info = svc.download_to_file("fid1")
        assert info["cached"] is False
        auths = [c.kwargs["headers"].get("Authorization") for c in mock_requests.post.call_args_list]
        assert set(auths) == {"Bearer mt_dl"}


class TestExtractBearerToken:
    COOKIE_NAME = "PLATFORM_SSO"

    @pytest.fixture(autouse=True)
    def _enable_cookie_channel(self, monkeypatch):
        """开启 Cookie 凭证通道（中性 Cookie 名）"""
        monkeypatch.setattr(
            "app.services.hub_file_service._hub_sso_cookie_name",
            lambda: self.COOKIE_NAME)

    def test_extracts_sso_cookie(self):
        assert extract_bearer_token("PLATFORM_SSO=abc123; other=1") == "abc123"

    def test_multiple_cookies_keeps_target(self):
        assert extract_bearer_token("a=1; PLATFORM_SSO=tok; b=2") == "tok"

    def test_missing_returns_none(self):
        assert extract_bearer_token("a=1; b=2") is None
        assert extract_bearer_token("") is None
        assert extract_bearer_token(None) is None

    def test_empty_value_returns_none(self):
        assert extract_bearer_token("PLATFORM_SSO=; a=1") is None
        assert extract_bearer_token("PLATFORM_SSO =  ; a=1") is None

    def test_value_may_contain_equals(self):
        assert extract_bearer_token("PLATFORM_SSO=aa=bb") == "aa=bb"

    def test_whitespace_tolerant(self):
        assert extract_bearer_token("  PLATFORM_SSO = tok  ; a=1") == "tok"

    def test_disabled_when_cookie_name_unset(self, monkeypatch):
        """HUB_SSO_COOKIE_NAME 未配置时 Cookie 通道整体禁用"""
        monkeypatch.setattr(
            "app.services.hub_file_service._hub_sso_cookie_name", lambda: "")
        assert extract_bearer_token("PLATFORM_SSO=abc") is None


class TestResolveHubBearerToken:
    def _patch_settings(self, monkeypatch, static_token):
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(
                ANC_HUB_BEARER_TOKEN=static_token,
                HUB_SSO_COOKIE_NAME="PLATFORM_SSO"))

    def test_static_token_priority(self, monkeypatch):
        """静态服务凭证优先于 Cookie 通道"""
        self._patch_settings(monkeypatch, " static-tok ")
        req = SimpleNamespace(headers={"cookie": "PLATFORM_SSO=cookie-tok"})
        assert resolve_hub_bearer_token(req) == "static-tok"

    def test_fallback_to_cookie(self, monkeypatch):
        """未配置静态凭证时回退平台登录态 Cookie"""
        self._patch_settings(monkeypatch, "")
        monkeypatch.setattr(
            "app.services.hub_file_service._hub_sso_cookie_name",
            lambda: "PLATFORM_SSO")
        req = SimpleNamespace(headers={"cookie": "PLATFORM_SSO=cookie-tok"})
        assert resolve_hub_bearer_token(req) == "cookie-tok"

    def test_no_request_no_token(self, monkeypatch):
        """无静态凭证、无请求（Cookie 通道无输入）→ None"""
        self._patch_settings(monkeypatch, "")
        assert resolve_hub_bearer_token(None) is None

    def test_cookie_channel_disabled(self, monkeypatch):
        """静态凭证与 Cookie 通道均未配置 → None"""
        from types import SimpleNamespace
        monkeypatch.setattr(
            "app.core.config.get_settings",
            lambda: SimpleNamespace(ANC_HUB_BEARER_TOKEN="", HUB_SSO_COOKIE_NAME=""))
        req = SimpleNamespace(headers={"cookie": "PLATFORM_SSO=cookie-tok"})
        assert resolve_hub_bearer_token(req) is None


# ---------------------------------------------------------------------------
# MCP 客户端
# ---------------------------------------------------------------------------

class TestHubMcpClient:
    def _client_with(self, posts):
        """posts: [FakeResp 或 异常，按调用顺序]"""
        mock = MagicMock()
        mock.post.side_effect = posts
        mock.delete.return_value = FakeResp("")
        return mock

    @patch("app.services.hub_file_service.requests")
    def test_sse_takes_last_non_empty_data_line(self, mock_requests):
        """SSE 响应取最后一个非空 data: 行（忽略空心跳行与干扰行）"""
        mock_requests.post.return_value = FakeResp(
            text=rpc_result(1, {"echo": "final"}),
            headers={"mcp-session-id": SESSION_ID},
        )
        with HubMcpClient(ENDPOINT) as cli:
            result = cli.call("tools/call", {"name": "t", "arguments": {}})
        assert result == {"echo": "final"}

    @patch("app.services.hub_file_service.requests")
    def test_plain_json_fallback(self, mock_requests):
        """纯 JSON 响应（非 SSE）兜底解析"""
        mock_requests.post.return_value = FakeResp(
            text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"v": 1}})
        )
        with HubMcpClient(ENDPOINT) as cli:
            assert cli.call("m") == {"v": 1}

    @patch("app.services.hub_file_service.requests")
    def test_empty_response_retries_once(self, mock_requests):
        """偶发空响应重试一次后成功"""
        mock_requests.post.side_effect = [
            FakeResp(""),  # 第一次空响应
            FakeResp(rpc_result(1, {"ok": True})),
        ]
        with HubMcpClient(ENDPOINT) as cli:
            result = cli.call("tools/call", {})
        assert result == {"ok": True}
        assert mock_requests.post.call_count == 2

    @patch("app.services.hub_file_service.requests")
    def test_double_empty_response_raises(self, mock_requests):
        """连续两次空响应报错（含中文提示）"""
        mock_requests.post.return_value = FakeResp("")
        with HubMcpClient(ENDPOINT) as cli:
            with pytest.raises(HubFileError) as exc:
                cli.call("tools/call", {})
        assert "已重试一次" in str(exc.value)

    @patch("app.services.hub_file_service.requests")
    def test_jsonrpc_error_raises(self, mock_requests):
        mock_requests.post.return_value = FakeResp(
            text=sse({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}})
        )
        with HubMcpClient(ENDPOINT) as cli:
            with pytest.raises(HubFileError) as exc:
                cli.call("no/such/method")
        assert "JSON-RPC error" in str(exc.value)

    @patch("app.services.hub_file_service.requests")
    def test_http_error_raises(self, mock_requests):
        mock_requests.post.return_value = FakeResp(text="boom", status_code=500)
        with HubMcpClient(ENDPOINT) as cli:
            with pytest.raises(HubFileError) as exc:
                cli.call("initialize", {})
        assert "HTTP 500" in str(exc.value)

    @patch("app.services.hub_file_service.requests")
    def test_session_id_header_passthrough(self, mock_requests):
        """initialize 返回的 mcp-session-id 在后续请求中透传"""
        mock_requests.post.return_value = FakeResp(
            text=rpc_result(1, {}), headers={"mcp-session-id": SESSION_ID}
        )
        mock_requests.delete.return_value = FakeResp("")
        with HubMcpClient(ENDPOINT) as cli:
            cli.call("initialize", {})
            cli.call("tools/call", {})
        second_call = mock_requests.post.call_args_list[1]
        assert second_call.kwargs["headers"]["mcp-session-id"] == SESSION_ID
        # close 发送 DELETE 并携带会话头
        delete_call = mock_requests.delete.call_args
        assert delete_call.kwargs["headers"]["mcp-session-id"] == SESSION_ID

    @patch("app.services.hub_file_service.requests")
    def test_notify_has_no_id(self, mock_requests):
        """通知消息不带 id"""
        mock_requests.post.return_value = FakeResp("")
        with HubMcpClient(ENDPOINT) as cli:
            cli.call("notifications/initialized", notify=True)
        sent = json.loads(mock_requests.post.call_args.kwargs["data"])
        assert "id" not in sent
        assert sent["method"] == "notifications/initialized"

    @patch("app.services.hub_file_service.requests")
    def test_close_without_session_noop(self, mock_requests):
        cli = HubMcpClient(ENDPOINT)
        cli.close()
        mock_requests.delete.assert_not_called()


# ---------------------------------------------------------------------------
# 工具结果 payload 提取
# ---------------------------------------------------------------------------

class TestExtractToolPayload:
    def test_structured_content_with_payload_wrapper(self):
        result = {"structuredContent": {"ok": True, "payload": {"ok": True, "data_base64": "aGk="}}}
        assert _extract_tool_payload(result)["data_base64"] == "aGk="

    def test_structured_content_direct(self):
        result = {"structuredContent": {"ok": True, "data_base64": "aGk="}}
        assert _extract_tool_payload(result)["ok"] is True

    def test_content_text_json(self):
        result = {"content": [{"type": "text", "text": json.dumps({"ok": True, "eof": True})}]}
        assert _extract_tool_payload(result)["eof"] is True

    def test_is_error_raises(self):
        result = {"isError": True, "content": [{"type": "text", "text": "文件不存在"}]}
        with pytest.raises(HubFileError) as exc:
            _extract_tool_payload(result)
        assert "工具返回错误" in str(exc.value)
        assert "文件不存在" in str(exc.value)

    def test_ok_false_raises(self):
        with pytest.raises(HubFileError) as exc:
            _extract_tool_payload({"structuredContent": {"ok": False, "message": "bad"}})
        assert "工具返回失败" in str(exc.value)

    def test_login_gate_reject_card_raises_message(self):
        """登录闸门拒绝卡（decision=reject）直接抛中文 message，不透出整坨 JSON"""
        card = {
            "decision": "reject",
            "status": "rejected",
            "login": {"outcome": "client-unsupported", "required": True},
            "message": "需要先建立登录身份：请调用 login 工具完成登录，然后重试本次调用。",
        }
        result = {"content": [{"type": "text", "text": json.dumps(card, ensure_ascii=False)}]}
        with pytest.raises(HubFileError) as exc:
            _extract_tool_payload(result)
        assert "需要先建立登录身份" in str(exc.value)
        assert "client-unsupported" not in str(exc.value)

    def test_rejected_status_without_decision_also_raises(self):
        """仅 status=rejected（无 decision 字段）同样按拒绝卡处理"""
        card = {"status": "rejected", "message": "登录态已过期，请重新登录后重试"}
        result = {"content": [{"type": "text", "text": json.dumps(card, ensure_ascii=False)}]}
        with pytest.raises(HubFileError) as exc:
            _extract_tool_payload(result)
        assert "登录态已过期" in str(exc.value)

    def test_unparseable_raises(self):
        with pytest.raises(HubFileError) as exc:
            _extract_tool_payload({"content": [{"type": "text", "text": "not-json"}]})
        assert "无法从工具结果解析" in str(exc.value)


# ---------------------------------------------------------------------------
# download_to_file
# ---------------------------------------------------------------------------

def _two_chunk_case():
    """两个分块的下载用例：内容 b"hello-" + b"world"，第二块 eof"""
    part1, part2 = b"hello-", b"world"
    content = part1 + part2
    full_sha = hashlib.sha256(content).hexdigest()
    chunks = [(part1, False, None), (part2, True, None)]
    return chunks, content, full_sha, len(content)


class TestDownloadToFile:
    @patch("app.services.hub_file_service.requests")
    def test_download_multi_chunk_ok(self, mock_requests, cache_root, tmp_path):
        """多分块下载落盘：内容/size/sha256 正确，meta.json 写入，DELETE 收尾"""
        chunks, content, full_sha, size = _two_chunk_case()
        record = []
        mock_requests.post.side_effect = make_post(chunks, full_sha, size, "session-abc.jsonl", record=record)
        mock_requests.delete.return_value = FakeResp("")

        svc = make_service(cache_root)
        info = svc.download_to_file("fid-001")

        assert info["cached"] is False
        assert info["filename"] == "session-abc.jsonl"
        assert info["size"] == size
        assert info["sha256"] == full_sha
        with open(info["path"], "rb") as f:
            assert f.read() == content
        assert os.path.dirname(info["path"]).endswith("fid-001")
        with open(os.path.join(os.path.dirname(info["path"]), "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["sha256"] == full_sha and meta["size"] == size
        # 协议顺序：initialize → notifications/initialized → tools/call×2
        assert [r[0] for r in record] == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
        mock_requests.delete.assert_called_once()

    @patch("app.services.hub_file_service.requests")
    def test_retry_on_sporadic_empty_response(self, mock_requests, cache_root):
        """下载中途偶发空响应：重试一次后继续成功"""
        chunks, content, full_sha, size = _two_chunk_case()
        mock_requests.post.side_effect = retry_empty_once(
            make_post(chunks, full_sha, size, "session-abc.jsonl")
        )
        svc = make_service(cache_root)
        info = svc.download_to_file("fid-retry")
        assert info["size"] == size
        with open(info["path"], "rb") as f:
            assert f.read() == content

    @patch("app.services.hub_file_service.requests")
    def test_cache_hit_skips_download(self, mock_requests, cache_root):
        """缓存命中（sha256 校验通过）时第二次跳过下载"""
        chunks, content, full_sha, size = _two_chunk_case()
        mock_requests.post.side_effect = make_post(chunks, full_sha, size, "s.jsonl")
        mock_requests.delete.return_value = FakeResp("")
        svc = make_service(cache_root)
        first = svc.download_to_file("fid-cache")

        # 第二次：post 一旦被调用即失败，证明走了缓存
        mock_requests.post.side_effect = AssertionError("缓存命中时不应发起 MCP 请求")
        second = svc.download_to_file("fid-cache")
        assert second["cached"] is True
        assert second["path"] == first["path"]
        assert second["sha256"] == full_sha

    @patch("app.services.hub_file_service.requests")
    def test_cache_corrupted_redownloads(self, mock_requests, cache_root):
        """缓存文件被篡改（sha256 不符）时重新下载"""
        chunks, content, full_sha, size = _two_chunk_case()
        mock_requests.post.side_effect = make_post(chunks, full_sha, size, "s.jsonl")
        mock_requests.delete.return_value = FakeResp("")
        svc = make_service(cache_root)
        first = svc.download_to_file("fid-bad")

        with open(first["path"], "wb") as f:
            f.write(b"corrupted!")
        mock_requests.post.side_effect = make_post(chunks, full_sha, size, "s.jsonl")
        second = svc.download_to_file("fid-bad")
        assert second["cached"] is False
        with open(second["path"], "rb") as f:
            assert f.read() == content

    @patch("app.services.hub_file_service.requests")
    def test_sha256_mismatch_fails_and_cleans(self, mock_requests, cache_root):
        """sha256 不符：报中文错误、清理半成品、不写 meta"""
        chunks, content, full_sha, size = _two_chunk_case()
        wrong_sha = "0" * 64
        chunks = [(chunks[0][0], False, None), (chunks[1][0], True, {"sha256": wrong_sha})]
        mock_requests.post.side_effect = make_post(chunks, wrong_sha, size, "s.jsonl")
        mock_requests.delete.return_value = FakeResp("")
        svc = make_service(cache_root)

        with pytest.raises(HubFileError) as exc:
            svc.download_to_file("fid-sha")
        assert "sha256 校验失败" in str(exc.value)
        target_dir = os.path.join(cache_root, "fid-sha")
        assert os.listdir(target_dir) == [], "校验失败后应清理半成品，不留任何文件"

    @patch("app.services.hub_file_service.requests")
    def test_size_mismatch_fails(self, mock_requests, cache_root):
        """size 双校验不符：报中文错误"""
        chunks, content, full_sha, size = _two_chunk_case()
        chunks = [(chunks[0][0], False, None), (chunks[1][0], True, {"size": size + 5})]
        mock_requests.post.side_effect = make_post(chunks, full_sha, size + 5, "s.jsonl")
        svc = make_service(cache_root)
        with pytest.raises(HubFileError) as exc:
            svc.download_to_file("fid-size")
        assert "大小校验失败" in str(exc.value)

    @patch("app.services.hub_file_service.requests")
    def test_bytes_read_mismatch_fails(self, mock_requests, cache_root):
        """bytes_read 与解码长度不符：报错"""
        chunks, content, full_sha, size = _two_chunk_case()
        chunks = [(chunks[0][0], False, None), (chunks[1][0], True, {"bytes_read": 999})]
        mock_requests.post.side_effect = make_post(chunks, full_sha, size, "s.jsonl")
        svc = make_service(cache_root)
        with pytest.raises(HubFileError) as exc:
            svc.download_to_file("fid-br")
        assert "长度不符" in str(exc.value)

    @patch("app.services.hub_file_service.requests")
    def test_tool_error_raises(self, mock_requests, cache_root):
        """工具返回 isError：报错且不落盘"""
        def _post(url, data=None, headers=None, timeout=None, **kwargs):
            msg = json.loads(data)
            if msg.get("method") == "initialize":
                return FakeResp(rpc_result(msg.get("id"), {}), headers={"mcp-session-id": SESSION_ID})
            if msg.get("method") == "tools/call":
                return FakeResp(rpc_result(msg["id"], {"isError": True,
                                                       "content": [{"type": "text", "text": "file not found"}]}))
            return FakeResp("")

        mock_requests.post.side_effect = _post
        svc = make_service(cache_root)
        with pytest.raises(HubFileError) as exc:
            svc.download_to_file("fid-err")
        assert "file not found" in str(exc.value)
        assert not os.path.exists(os.path.join(cache_root, "fid-err", "meta.json"))

    @patch("app.services.hub_file_service.requests")
    def test_eof_never_set_hits_chunk_cap(self, mock_requests, cache_root, monkeypatch):
        """服务端永不返回 eof：达到分块数上限后中止，避免死循环"""
        monkeypatch.setattr(hub_file_service, "MAX_DOWNLOAD_CHUNKS", 3)
        part = b"abc"
        mock_requests.post.side_effect = make_post(
            [(part, False, None)] * 100, "f" * 64, 300, "s.jsonl")
        svc = make_service(cache_root)
        with pytest.raises(HubFileError) as exc:
            svc.download_to_file("fid-loop")
        assert "分块数超过上限" in str(exc.value)

    @patch("app.services.hub_file_service.requests")
    def test_chunk_size_clamped_to_1mb(self, mock_requests, cache_root):
        """单块请求字节数超过服务端上限时收敛到 1MB"""
        captured = []

        def _post(url, data=None, headers=None, timeout=None, **kwargs):
            msg = json.loads(data)
            if msg.get("method") == "initialize":
                return FakeResp(rpc_result(msg.get("id"), {}), headers={"mcp-session-id": SESSION_ID})
            if msg.get("method") == "tools/call":
                captured.append(msg["params"]["arguments"])
                return FakeResp(rpc_result(msg["id"], {"structuredContent": {"ok": True, "payload": {
                    "ok": True, "data_base64": "", "bytes_read": 0, "eof": True,
                    "sha256": hashlib.sha256(b"").hexdigest(), "size": 0, "filename": "empty.jsonl"}}}))
            return FakeResp("")

        mock_requests.post.side_effect = _post
        svc = HubFileService(endpoint=ENDPOINT, cache_root=cache_root, chunk_size=5 * 1024 * 1024)
        svc.download_to_file("fid-clamp")
        assert captured[0]["max_bytes"] == 1024 * 1024

    @patch("app.services.hub_file_service.requests")
    def test_endpoint_missing_raises(self, mock_requests, cache_root, monkeypatch):
        """service 未显式指定端点且环境变量、Settings(.env) 均缺失：中文报错"""
        monkeypatch.delenv(hub_file_service.HUB_ENDPOINT_ENV, raising=False)
        monkeypatch.setattr(
            hub_file_service, "get_settings",
            lambda: SimpleNamespace(ANC_HUB_MCP_ENDPOINT=""),
        )
        svc = HubFileService(cache_root=cache_root)
        with pytest.raises(HubEndpointNotConfiguredError):
            svc.download_to_file("fid-x")
        mock_requests.post.assert_not_called()

    def test_empty_file_id_raises(self, cache_root):
        svc = HubFileService(endpoint=ENDPOINT, cache_root=cache_root)
        with pytest.raises(HubFileError):
            svc.download_to_file("  ")


# ---------------------------------------------------------------------------
# query_files（file_query 透传）
# ---------------------------------------------------------------------------

def make_query_post(expected_args, result_payload, record=None):
    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        if record is not None:
            record.append((msg.get("method"), msg))
        if msg.get("method") == "initialize":
            return FakeResp(rpc_result(msg.get("id"), {}), headers={"mcp-session-id": SESSION_ID})
        if msg.get("method") == "notifications/initialized":
            return FakeResp("")
        if msg.get("method") == "tools/call":
            assert msg["params"]["name"] == hub_file_service.QUERY_TOOL
            assert msg["params"]["arguments"] == expected_args
            return FakeResp(rpc_result(msg["id"], {"structuredContent": {"ok": True, "payload": result_payload}}))
        raise AssertionError("未预期的 MCP 方法")

    return _post


class TestQueryFiles:
    @patch("app.services.hub_file_service.requests")
    def test_query_args_passthrough(self, mock_requests, cache_root):
        """查询条件原样透传 file_query，结果透传返回"""
        record = []
        expected_args = {"name_like": "session", "uploader": "zhang",
                         "uploaded_after": "2026-01-01T00:00:00Z", "page": 2, "page_size": 50}
        payload = {"ok": True, "total": 1, "files": [{"file_id": "f1", "name": "session-x.jsonl"}]}
        mock_requests.post.side_effect = make_query_post(expected_args, payload, record=record)
        mock_requests.delete.return_value = FakeResp("")

        svc = make_service(cache_root)
        result = svc.query_files(
            name_like="session", uploaded_after="2026-01-01T00:00:00Z",
            uploader="zhang", page=2, page_size=50)
        assert result == payload
        assert [r[0] for r in record] == ["initialize", "notifications/initialized", "tools/call"]

    @patch("app.services.hub_file_service.requests")
    def test_query_none_fields_omitted(self, mock_requests, cache_root):
        """未提供的过滤条件不出现在 arguments 中"""
        mock_requests.post.side_effect = make_query_post({"name_like": "a"}, {"ok": True})
        svc = make_service(cache_root)
        result = svc.query_files(name_like="a")
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

class TestSanitizeHubName:
    def test_path_traversal_stripped(self):
        assert sanitize_hub_name("../../etc/passwd") == "passwd"

    def test_separator_replaced(self):
        assert "/" not in sanitize_hub_name("a/b\\c:d")
        assert sanitize_hub_name("  ") == "file"


class TestCleanupHubFiles:
    def test_cleanup_removes_cache(self, tmp_path):
        root = tmp_path / "hub_files"
        sub = root / "fid"
        sub.mkdir(parents=True)
        (sub / "f.jsonl").write_text("x")
        cleanup_hub_files(str(root))
        assert not root.exists()

    def test_cleanup_missing_dir_noop(self, tmp_path):
        cleanup_hub_files(str(tmp_path / "nope"))


# ---------------------------------------------------------------------------
# 评测取数路径：hub_file 字段落盘
# ---------------------------------------------------------------------------

class TestGetHubFileFields:
    def _mock_db(self, field_names):
        db = MagicMock()
        rows = [SimpleNamespace(field_name=n) for n in field_names]
        db.query.return_value.filter.return_value.all.return_value = rows
        return db

    def test_returns_hub_typed_fields(self):
        db = self._mock_db(["cases_file", "allure_results"])
        assert get_hub_file_fields(db, "ds-1") == ["cases_file", "allure_results"]

    def test_none_db_or_dataset(self):
        assert get_hub_file_fields(None, "ds-1") == []
        assert get_hub_file_fields(MagicMock(), None) == []


class TestDownloadAndExtract:
    def _zip_post(self, mock_requests, zip_bytes, zip_name):
        """按单块 eof 返回 zip 字节流的 MCP mock（sha256/size 针对 zip 字节流）"""
        full_sha = hashlib.sha256(zip_bytes).hexdigest()
        mock_requests.post.side_effect = make_post(
            [(zip_bytes, True, None)], full_sha, len(zip_bytes), zip_name)
        mock_requests.delete.return_value = FakeResp("")

    @patch("app.services.hub_file_service.requests")
    def test_single_entry_zip_returns_inner_file(self, mock_requests, cache_root):
        """单条目 zip（claude-threads 形态）：返回内层 jsonl 的 path/filename"""
        inner = SESSION_JSONL.encode()
        zip_bytes = make_zip_bytes([("46471c07-sess.jsonl", inner)])
        self._zip_post(mock_requests, zip_bytes, "46471c07-sess.jsonl__fafc81b0.zip")

        svc = make_service(cache_root)
        info = svc.download_and_extract("fid-zip1")

        assert info["filename"] == "46471c07-sess.jsonl"
        assert info["path"].endswith(os.path.join("unpacked", "46471c07-sess.jsonl"))
        with open(info["path"], "rb") as f:
            assert f.read() == inner
        assert [f["rel_path"] for f in info["files"]] == ["46471c07-sess.jsonl"]
        assert info["files"][0]["size"] == len(inner)
        # size/sha256 记录的是下载原始字节流（zip）的
        assert info["size"] == len(zip_bytes)
        assert info["sha256"] == hashlib.sha256(zip_bytes).hexdigest()
        assert info["cached"] is False

    @patch("app.services.hub_file_service.requests")
    def test_multi_file_zip_returns_unpack_root(self, mock_requests, cache_root):
        """多文件 zip（anc-results 形态）：path 指向解压根目录，目录结构保留，嵌套 zip 不展开"""
        entries = [
            ("README.md", b"readme"),
            ("flow/flow_001.md", b"# flow"),
            ("exec/run.json", b"{}"),
            ("uploads/pic.png", b"\x89PNG"),
            ("nested/inner.zip", b"PK\x03\x04fake"),
        ]
        zip_bytes = make_zip_bytes(entries)
        self._zip_post(mock_requests, zip_bytes, "anc-result__ab62eb1f.zip")

        svc = make_service(cache_root)
        info = svc.download_and_extract("fid-zip2")

        assert info["path"].endswith("unpacked")
        assert os.path.isdir(info["path"])
        assert info["filename"] == "anc-result__ab62eb1f.zip"  # 多文件时展示 zip 名
        assert sorted(f["rel_path"] for f in info["files"]) == sorted(n for n, _ in entries)
        for name, content in entries:
            with open(os.path.join(info["path"], *name.split("/")), "rb") as f:
                assert f.read() == content

    @patch("app.services.hub_file_service.requests")
    def test_non_zip_passthrough(self, mock_requests, cache_root):
        """非 zip 下载：行为与 download_to_file 一致（files 单元素）"""
        content = SESSION_JSONL.encode()
        full_sha = hashlib.sha256(content).hexdigest()
        mock_requests.post.side_effect = make_post(
            [(content, True, None)], full_sha, len(content), "s.jsonl")
        mock_requests.delete.return_value = FakeResp("")

        svc = make_service(cache_root)
        info = svc.download_and_extract("fid-plain")

        assert info["filename"] == "s.jsonl"
        assert info["path"].endswith("s.jsonl")
        assert info["files"] == [{"path": info["path"], "rel_path": "s.jsonl", "size": len(content)}]

    @patch("app.services.hub_file_service.requests")
    def test_zip_slip_rejected(self, mock_requests, cache_root):
        """zip entry 含 .. 穿越：中文报错，不在解压目录外落文件"""
        zip_bytes = make_zip_bytes([("../evil.txt", b"x")])
        self._zip_post(mock_requests, zip_bytes, "evil.zip")

        svc = make_service(cache_root)
        with pytest.raises(HubFileError) as exc:
            svc.download_and_extract("fid-slip")
        assert "不安全" in str(exc.value)
        unpack_root = os.path.join(cache_root, "fid-slip", "unpacked")
        assert not os.path.exists(os.path.join(os.path.dirname(unpack_root), "evil.txt"))

    @patch("app.services.hub_file_service.requests")
    def test_unpack_entry_cap(self, mock_requests, cache_root, monkeypatch):
        """zip 条目数超上限：中止解压（zip bomb 防护）"""
        monkeypatch.setattr(hub_file_service, "MAX_UNPACK_ENTRIES", 2)
        zip_bytes = make_zip_bytes([("a.txt", b"1"), ("b.txt", b"2"), ("c.txt", b"3")])
        self._zip_post(mock_requests, zip_bytes, "bomb-entries.zip")

        svc = make_service(cache_root)
        with pytest.raises(HubFileError) as exc:
            svc.download_and_extract("fid-cap")
        assert "条目数超过上限" in str(exc.value)
        # 解压失败不残留半成品目录
        assert not os.path.exists(os.path.join(cache_root, "fid-cap", "unpacked"))

    @patch("app.services.hub_file_service.requests")
    def test_duplicate_rel_path_deduped(self, mock_requests, cache_root):
        """畸形包重复 rel_path：清单去重（后写覆盖），manifest 可校验、二次调用走缓存"""
        zip_bytes = make_zip_bytes([("a.txt", b"old"), ("a.txt", b"new-longer")])
        self._zip_post(mock_requests, zip_bytes, "dup.zip")

        svc = make_service(cache_root)
        first = svc.download_and_extract("fid-dup")
        assert [f["rel_path"] for f in first["files"]] == ["a.txt"]
        assert first["files"][0]["size"] == len(b"new-longer")
        with open(first["path"], "rb") as f:
            assert f.read() == b"new-longer"

        mock_requests.post.side_effect = AssertionError("缓存命中时不应发起 MCP 请求")
        second = svc.download_and_extract("fid-dup")
        assert second["cached"] is True
        assert second["files"] == first["files"]

    @patch("app.services.hub_file_service.requests")
    def test_unpack_total_size_cap(self, mock_requests, cache_root, monkeypatch):
        """解压实际写出字节超上限：中止解压（按写出量累计，不信任 entry 头）"""
        monkeypatch.setattr(hub_file_service, "MAX_UNPACK_TOTAL_SIZE", 5)
        zip_bytes = make_zip_bytes([("a.txt", b"12345678")])
        self._zip_post(mock_requests, zip_bytes, "bomb-size.zip")

        svc = make_service(cache_root)
        with pytest.raises(HubFileError) as exc:
            svc.download_and_extract("fid-size")
        assert "总大小超过上限" in str(exc.value)

    @patch("app.services.hub_file_service.requests")
    def test_second_call_skips_download_and_unpack(self, mock_requests, cache_root):
        """第二次调用：不再发起 MCP 请求，复用解压产物（cached=True）"""
        zip_bytes = make_zip_bytes([("s.jsonl", SESSION_JSONL.encode())])
        self._zip_post(mock_requests, zip_bytes, "s.jsonl__c2eeca7c.zip")
        svc = make_service(cache_root)
        first = svc.download_and_extract("fid-zip3")

        mock_requests.post.side_effect = AssertionError("缓存命中时不应发起 MCP 请求")
        second = svc.download_and_extract("fid-zip3")
        assert second["cached"] is True
        assert second["path"] == first["path"]
        assert second["files"] == first["files"]

    @patch("app.services.hub_file_service.requests")
    def test_unpacked_artifact_missing_reunpacks(self, mock_requests, cache_root):
        """解压产物被删：zip 缓存仍命中（不重新下载），但重新解压补齐产物"""
        zip_bytes = make_zip_bytes([("s.jsonl", SESSION_JSONL.encode())])
        self._zip_post(mock_requests, zip_bytes, "s.jsonl__024c2863.zip")
        svc = make_service(cache_root)
        first = svc.download_and_extract("fid-zip4")
        assert os.path.isfile(first["path"])

        os.remove(first["path"])
        mock_requests.post.side_effect = AssertionError("zip 缓存命中时不应重新下载")
        second = svc.download_and_extract("fid-zip4")
        assert os.path.isfile(second["path"])  # 产物已补回
        assert second["path"] == first["path"]
        assert second["cached"] is True


# ---------------------------------------------------------------------------
# query_files（file_query 透传）
# ---------------------------------------------------------------------------

def make_query_post(expected_args, result_payload, record=None):
    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        if record is not None:
            record.append((msg.get("method"), msg))
        if msg.get("method") == "initialize":
            return FakeResp(rpc_result(msg.get("id"), {}), headers={"mcp-session-id": SESSION_ID})
        if msg.get("method") == "notifications/initialized":
            return FakeResp("")
        if msg.get("method") == "tools/call":
            assert msg["params"]["name"] == hub_file_service.QUERY_TOOL
            assert msg["params"]["arguments"] == expected_args
            return FakeResp(rpc_result(msg["id"], {"structuredContent": {"ok": True, "payload": result_payload}}))
        raise AssertionError("未预期的 MCP 方法")

    return _post


class TestQueryFiles:
    @patch("app.services.hub_file_service.requests")
    def test_query_args_passthrough(self, mock_requests, cache_root):
        """查询条件原样透传 file_query，结果透传返回"""
        record = []
        expected_args = {"name_like": "session", "uploader": "zhang",
                         "uploaded_after": "2026-01-01T00:00:00Z", "page": 2, "page_size": 50}
        payload = {"ok": True, "total": 1, "files": [{"file_id": "f1", "name": "session-x.jsonl"}]}
        mock_requests.post.side_effect = make_query_post(expected_args, payload, record=record)
        mock_requests.delete.return_value = FakeResp("")

        svc = make_service(cache_root)
        result = svc.query_files(
            name_like="session", uploaded_after="2026-01-01T00:00:00Z",
            uploader="zhang", page=2, page_size=50)
        assert result == payload
        assert [r[0] for r in record] == ["initialize", "notifications/initialized", "tools/call"]

    @patch("app.services.hub_file_service.requests")
    def test_query_none_fields_omitted(self, mock_requests, cache_root):
        """未提供的过滤条件不出现在 arguments 中"""
        mock_requests.post.side_effect = make_query_post({"name_like": "a"}, {"ok": True})
        svc = make_service(cache_root)
        result = svc.query_files(name_like="a")
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

class TestSanitizeHubName:
    def test_path_traversal_stripped(self):
        assert sanitize_hub_name("../../etc/passwd") == "passwd"

    def test_separator_replaced(self):
        assert "/" not in sanitize_hub_name("a/b\\c:d")
        assert sanitize_hub_name("  ") == "file"


class TestCleanupHubFiles:
    def test_cleanup_removes_cache(self, tmp_path):
        root = tmp_path / "hub_files"
        sub = root / "fid"
        sub.mkdir(parents=True)
        (sub / "f.jsonl").write_text("x")
        cleanup_hub_files(str(root))
        assert not root.exists()

    def test_cleanup_missing_dir_noop(self, tmp_path):
        cleanup_hub_files(str(tmp_path / "nope"))


# ---------------------------------------------------------------------------
# 评测取数路径：hub_file 字段落盘
# ---------------------------------------------------------------------------

class TestGetHubFileFields:
    def _mock_db(self, field_names):
        db = MagicMock()
        rows = [SimpleNamespace(field_name=n) for n in field_names]
        db.query.return_value.filter.return_value.all.return_value = rows
        return db

    def test_returns_hub_typed_fields(self):
        db = self._mock_db(["cases_file", "allure_results"])
        assert get_hub_file_fields(db, "ds-1") == ["cases_file", "allure_results"]

    def test_none_db_or_dataset(self):
        assert get_hub_file_fields(None, "ds-1") == []
        assert get_hub_file_fields(MagicMock(), None) == []

class TestResolveHubFileFields:
    """阶段B新契约：复用登记表持久解包区镜像替换（细化用例见 test_hub_resolve_execution.py）"""

    def _db_with_record(self, unpack_dir, status="unpacked"):
        db = MagicMock()
        record = SimpleNamespace(unpack_dir=str(unpack_dir), unpack_status=status)
        db.query.return_value.filter.return_value.first.return_value = record
        return db

    def _db_without_record(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        return db

    def test_replaces_file_id_with_mirror_path(self, tmp_path):
        """file_id 有持久解包区 → 镜像到 cache_root/{file_id} 并替换字段值"""
        unpack = tmp_path / "persist" / "fid-1"
        (unpack / "logs").mkdir(parents=True)
        (unpack / "logs" / "a.log").write_text("x")
        data = {"cases_file": "fid-1", "note": "普通字段"}
        resolved = resolve_hub_file_fields(
            data, ["cases_file"], cache_root=str(tmp_path / "task"), db=self._db_with_record(unpack))
        mirror = str(tmp_path / "task" / "fid-1")
        assert data["cases_file"] == mirror
        assert data["note"] == "普通字段"
        assert resolved == {"cases_file": mirror}
        assert (tmp_path / "task" / "fid-1" / "logs" / "a.log").read_text() == "x"

    def test_missing_persistent_raises_not_available(self):
        """无登记记录 → HubFileNotAvailable（含 file_id 与「请重新导入」）"""
        data = {"cases_file": "fid-gone"}
        with pytest.raises(HubFileNotAvailable) as ei:
            resolve_hub_file_fields(data, ["cases_file"], db=self._db_without_record())
        assert "fid-gone" in str(ei.value)
        assert "请重新导入" in str(ei.value)

    def test_mirror_failure_keeps_original_value(self, tmp_path):
        """镜像落盘异常（非缺持久区）→ 保留原值不抛异常（continue 语义）"""
        unpack = tmp_path / "persist" / "fid-x"
        unpack.mkdir(parents=True)
        data = {"cases_file": "fid-x"}
        with patch("app.services.hub_file_service.hardlink_tree", side_effect=OSError("disk full")):
            resolved = resolve_hub_file_fields(
                data, ["cases_file"], cache_root=str(tmp_path / "task"), db=self._db_with_record(unpack))
        assert data["cases_file"] == "fid-x"
        assert resolved == {}

    def test_empty_or_non_string_skipped(self, tmp_path):
        data = {"a": "", "b": None, "c": 123}
        db = self._db_with_record(tmp_path)
        resolved = resolve_hub_file_fields(data, ["a", "b", "c"], db=db)
        db.query.assert_not_called()
        assert resolved == {}

    def test_no_fields_noop(self):
        data = {"cases_file": "fid-1"}
        resolve_hub_file_fields(data, [], db=MagicMock())
        assert data["cases_file"] == "fid-1"

    def test_missing_db_session_raises(self):
        """db=None：中文配置错误（HubFileError，不属于数据项级失败）"""
        with pytest.raises(HubFileError, match="数据库会话"):
            resolve_hub_file_fields({"a": "fid"}, ["a"])


# ---------------------------------------------------------------------------
# 评测取数路径集成：_process_media_files 中 hub_file 字段的接入
# ---------------------------------------------------------------------------

class TestProcessMediaFilesHubIntegration:
    """验证 evaluation_tasks_async._process_media_files 的 hub_file 接入不影响既有通道"""

    def _mock_db(self, hub_fields):
        db = MagicMock()
        rows = [SimpleNamespace(field_name=n) for n in hub_fields]
        db.query.return_value.filter.return_value.all.return_value = rows
        return db

    def _task(self, dataset_id="ds-1", task_id="task-1"):
        return SimpleNamespace(dataset_id=dataset_id, id=task_id)

    def _run(self, task, items, db):
        from app.tasks.evaluation_tasks_async import _process_media_files
        with patch("app.services.media_processor.MediaProcessor") as mock_mp, \
                patch("app.services.oss_media_cache_service.OSSMediaCacheService") as mock_oss:
            mock_mp.return_value.process_content.side_effect = lambda v: v
            mock_oss.return_value.validate_oss_url.return_value = False
            return _process_media_files(task, items, db)

    def test_no_hub_fields_keeps_existing_channels(self):
        """无 hub_file 字段：既有 is_media/OSS/纯文本通道行为不变，且不调用 hub 解析"""
        db = self._mock_db([])
        items = [{"id": "d1", "index": 0,
                  "data": {"note": "普通文本", "media_field": "a.png", "media_field_is_media": True}}]
        with patch("app.services.hub_file_service.resolve_hub_file_fields",
                   side_effect=AssertionError("无 hub 字段时不应发起解析")) as mock_resolve:
            processed = self._run(self._task(), items, db)
            mock_resolve.assert_not_called()
        data = processed[0]["data"]
        # 既有通道：纯文本字段生成 _processed、媒体字段置空（OSS URL 校验不过）
        assert data["note_processed"] == "普通文本"
        assert data["media_field"] == ""

    def test_hub_field_resolved_and_replaced(self):
        """hub_file 字段：解析成功后字段值替换为镜像目录路径"""
        db = self._mock_db(["cases_file"])
        items = [{"id": "d1", "index": 0, "data": {"cases_file": "fid-9", "note": "x"}}]

        def _fake_resolve(data, fields, cache_root=None, db=None):
            data["cases_file"] = "/abs/mirror/fid-9"

        with patch("app.services.hub_file_service.resolve_hub_file_fields",
                   side_effect=_fake_resolve):
            processed = self._run(self._task(), items, db)
        assert processed[0]["data"]["cases_file"] == "/abs/mirror/fid-9"
        assert "_hub_resolve_error" not in processed[0]

    def test_hub_field_failure_keeps_original(self):
        """hub_file 字段解析异常（非缺持久区）：保留原值，任务不被阻断"""
        db = self._mock_db(["cases_file"])
        items = [{"id": "d1", "index": 0, "data": {"cases_file": "fid-bad", "note": "x"}}]
        with patch("app.services.hub_file_service.resolve_hub_file_fields",
                   side_effect=HubFileError("镜像落盘失败")):
            processed = self._run(self._task(), items, db)
        assert processed[0]["data"]["cases_file"] == "fid-bad"
        assert processed[0]["data"]["note"] == "x"
        assert "_hub_resolve_error" not in processed[0]

    def test_hub_not_available_marks_item_failed(self):
        """缺持久解包区（HubFileNotAvailable）：数据项标记失败（_hub_resolve_error 含 file_id），不抛异常"""
        db = self._mock_db(["cases_file"])
        items = [{"id": "d1", "index": 0, "data": {"cases_file": "fid-gone", "note": "x"}}]
        with patch("app.services.hub_file_service.resolve_hub_file_fields",
                   side_effect=HubFileNotAvailable(
                       "字段 cases_file 的执行包（file_id=fid-gone）无持久解包区，请重新导入")):
            processed = self._run(self._task(), items, db)
        assert "fid-gone" in processed[0]["_hub_resolve_error"]
        assert "请重新导入" in processed[0]["_hub_resolve_error"]
        assert processed[0]["data"]["cases_file"] == "fid-gone"  # 原值保留便于排障

    def test_no_dataset_id_skips_hub_lookup(self):
        """task 无 dataset_id：不查字段元数据、不解析"""
        db = MagicMock()
        items = [{"id": "d1", "index": 0, "data": {"note": "x"}}]
        with patch("app.services.hub_file_service.resolve_hub_file_fields",
                   side_effect=AssertionError("不应发起解析")):
            processed = self._run(self._task(dataset_id=None), items, db)
        db.query.assert_not_called()
        assert processed[0]["data"]["note"] == "x"

    def test_hub_cache_root_is_task_scoped(self):
        """hub 拉取缓存按任务隔离：resolve 收到 uploads/hub_files/{task_id}/ 级 cache_root"""
        db = self._mock_db(["cases_file"])
        items = [{"id": "d1", "index": 0, "data": {"cases_file": "fid-9", "note": "x"}}]
        with patch("app.services.hub_file_service.resolve_hub_file_fields") as mock_resolve:
            self._run(self._task(task_id="task-abc"), items, db)
        assert mock_resolve.call_args.kwargs["cache_root"] == os.path.join(
            os.getcwd(), "uploads", "hub_files", "task-abc")

    def test_no_task_id_no_cache_root(self):
        """task 无 id：cache_root 传 None（走默认缓存根目录）"""
        db = self._mock_db(["cases_file"])
        items = [{"id": "d1", "index": 0, "data": {"cases_file": "fid-9", "note": "x"}}]
        with patch("app.services.hub_file_service.resolve_hub_file_fields") as mock_resolve:
            self._run(SimpleNamespace(dataset_id="ds-1"), items, db)
        assert mock_resolve.call_args.kwargs["cache_root"] is None


# ---------------------------------------------------------------------------
# 任务结束清理：run_evaluation_async finally 清理任务级 hub 缓存
# ---------------------------------------------------------------------------

class TestTaskHubCacheCleanup:
    def test_cleanup_task_hub_cache_uses_task_scoped_dir(self):
        from app.tasks.evaluation_tasks_async import _cleanup_task_hub_cache
        with patch("app.services.hub_file_service.cleanup_hub_files") as mock_cleanup:
            _cleanup_task_hub_cache("task-42")
        assert mock_cleanup.call_args.args[0] == os.path.join(
            os.getcwd(), "uploads", "hub_files", "task-42")

    def test_cleanup_task_hub_cache_empty_task_id_noop(self):
        from app.tasks.evaluation_tasks_async import _cleanup_task_hub_cache
        with patch("app.services.hub_file_service.cleanup_hub_files") as mock_cleanup:
            _cleanup_task_hub_cache(None)
            _cleanup_task_hub_cache("")
        mock_cleanup.assert_not_called()

    def test_cleanup_task_hub_cache_swallows_errors(self):
        """清理失败只告警，不向任务流程抛异常"""
        from app.tasks.evaluation_tasks_async import _cleanup_task_hub_cache
        with patch("app.services.hub_file_service.cleanup_hub_files",
                   side_effect=OSError("disk busy")):
            _cleanup_task_hub_cache("task-42")  # 不应抛出

    def test_run_evaluation_async_finally_cleans_hub_cache(self):
        """run_evaluation_async 的 finally 覆盖异常路径：任务不存在时也清理缓存"""
        from app.tasks import evaluation_tasks_async as eta
        with patch.object(eta, "get_db_session"), \
                patch.object(eta.EvaluationTaskService, "get_task", return_value=None), \
                patch("app.services.hub_file_service.cleanup_hub_files") as mock_cleanup:
            with pytest.raises(ValueError):
                eta.run_evaluation_async("task-fin")
        assert mock_cleanup.call_args.args[0].endswith(os.path.join("hub_files", "task-fin"))
