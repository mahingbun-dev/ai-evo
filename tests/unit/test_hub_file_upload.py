"""HubFileService.upload_bytes 三段式上传单元测试。

仿 tests/unit/test_hub_file_service.py 的 make_post 模式：mock
app.services.hub_file_service.requests.post，按 JSON-RPC method +
tools/call 的 action 分发，捕获 tools/call 序列断言：
- begin 参数（filename/total_size/sha256）
- chunk 的 upload_id/seq 连续与块大小上限（begin.chunk_max_bytes 缺省 512KB、
  服务端给 2MB 时钳制到 1MB）
- commit 时机与返回值字段
- begin 缺 upload_id / commit 缺 file_id 抛 HubFileError
- chunk 失败时 abort 被调用且原异常上抛；abort 自身失败不掩盖原异常
- 空 data 抛错（不发任何请求）
- 文件名经 sanitize_hub_name 清洗、Bearer 凭证头透传
"""
import base64
import hashlib
import json

import pytest

from app.services import hub_file_service
from app.services.hub_file_service import (
    HUB_DOWNLOAD_CHUNK,
    HUB_UPLOAD_CHUNK,
    HubEndpointNotConfiguredError,
    HubFileError,
    HubFileService,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

ENDPOINT = "http://hub.test/mcp"
UPLOAD_TOOL = "file-store__file_upload"


class FakeResp:
    def __init__(self, text="", status_code=200, headers=None):
        self.content = (text if isinstance(text, bytes) else (text or "").encode("utf-8"))
        self.status_code = status_code
        self.headers = headers or {}


def sse(obj) -> str:
    return 'data: \ndata: {"ping": 1}\n\ndata: ' + json.dumps(obj) + "\n\n"


class UploadBackend:
    """file_upload 工具的假服务端：按 action 返回，可注入故障。"""

    def __init__(self, *, chunk_max_bytes=None, commit_payload=None,
                 begin_payload=None, fail_on_chunk_seq=None, fail_abort=False):
        self.chunk_max_bytes = chunk_max_bytes
        self.commit_payload = commit_payload
        self.begin_payload = begin_payload
        self.fail_on_chunk_seq = fail_on_chunk_seq
        self.fail_abort = fail_abort
        self.actions = []          # [(action, args)] 顺序记录 tools/call
        self.upload_id_counter = 0

    def handle(self, action, args):
        self.actions.append((action, dict(args)))
        if action == "begin":
            self.upload_id_counter += 1
            payload = {"ok": True, "upload_id": f"up-{self.upload_id_counter}"}
            if self.chunk_max_bytes is not None:
                payload["chunk_max_bytes"] = self.chunk_max_bytes
            if self.begin_payload is not None:
                payload = self.begin_payload
            return payload
        if action == "chunk":
            if self.fail_on_chunk_seq is not None and args["seq"] == self.fail_on_chunk_seq:
                return (f"服务端拒绝第 {args['seq']} 块", True)
            return {"ok": True}
        if action == "commit":
            return self.commit_payload if self.commit_payload is not None else {
                "ok": True, "file_id": "file-1",
            }
        if action == "abort":
            if self.fail_abort:
                return ("abort 也失败", True)
            return {"ok": True}
        raise AssertionError(f"未预期的 action: {action}")

    def upload_calls(self):
        return [(a, args) for a, args in self.actions if a in ("begin", "chunk", "commit", "abort")]


def make_post(backend: UploadBackend, record=None):
    """构造 requests.post 假实现（按 JSON-RPC method 分发）。"""

    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        if record is not None:
            record.append((msg.get("method"), headers, msg))
        method = msg.get("method")
        if method == "initialize":
            return FakeResp(
                sse({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": True}}),
                headers={"mcp-session-id": "sess-up-1"},
            )
        if method == "notifications/initialized":
            return FakeResp("")
        if method == "tools/call":
            # 中台收口后：tools/call → call_tool → 插件工具
            assert msg["params"]["name"] == "call_tool", msg["params"]["name"]
            inner = msg["params"]["arguments"]
            assert inner["name"] == UPLOAD_TOOL, inner["name"]
            args = inner["arguments"]
            ret = backend.handle(args.get("action"), args)
            req_id = msg.get("id")
            if isinstance(ret, tuple):  # (message, is_error)：isError 时消息走 content 文本块
                message = ret[0]
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": message if isinstance(message, str) else json.dumps(message)}],
                }
            else:
                result = {"isError": False, "structuredContent": ret}
            return FakeResp(sse({"jsonrpc": "2.0", "id": req_id, "result": result}))
        raise AssertionError(f"未预期的 MCP 方法: {method}")

    return _post


@pytest.fixture
def svc(tmp_path):
    return HubFileService(endpoint=ENDPOINT, cache_root=str(tmp_path / "cache"))


def _patch_mcp(mp, backend, record=None):
    """同时 mock post 与 delete：DELETE 收尾不打真实网络（否则每用例等连接超时）。"""
    mp.setattr(hub_file_service.requests, "post", make_post(backend, record=record))
    mp.setattr(hub_file_service.requests, "delete", lambda *a, **kw: FakeResp(""))


def _upload(svc, backend, filename="报告.md", data=b"x" * 100, **backend_kw):
    with pytest.MonkeyPatch.context() as mp:
        _patch_mcp(mp, backend)
        return svc.upload_bytes(filename, data)


# ── 正常链路 ────────────────────────────────────────────────────────


def test_small_data_single_chunk_flow(svc):
    data = "中文内容".encode("utf-8") * 10
    backend = UploadBackend(commit_payload={
        "ok": True, "file_id": "file-9", "filename": "报告.md",
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    })
    out = _upload(svc, backend, data=data)

    begin_args = backend.actions[0][1]
    assert backend.actions[0][0] == "begin"
    assert begin_args == {
        "action": "begin", "filename": "报告.md",
        "total_size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }
    chunk_args = backend.actions[1][1]
    assert backend.actions[1][0] == "chunk"
    assert chunk_args["seq"] == 0
    assert base64.b64decode(chunk_args["data_base64"]) == data
    assert chunk_args["upload_id"] == "up-1"
    assert backend.actions[2][0] == "commit"
    assert backend.actions[2][1] == {"action": "commit", "upload_id": "up-1"}

    assert out == {
        "file_id": "file-9", "filename": "报告.md",
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }


def test_multi_chunk_seq_continuous_and_reassembly(svc):
    """2MB+ 数据按 512KB 缺省分块：seq 从 0 连续、块序拼回原文。"""
    data = bytes(range(256)) * (2 * 1024 * 1024 // 256) + b"tail"  # 约 2MB
    backend = UploadBackend(commit_payload={"ok": True, "file_id": "file-big"})
    out = _upload(svc, backend, filename="big.json", data=data)

    chunk_calls = [args for action, args in backend.actions if action == "chunk"]
    assert len(chunk_calls) == (len(data) + HUB_UPLOAD_CHUNK - 1) // HUB_UPLOAD_CHUNK
    assert [c["seq"] for c in chunk_calls] == list(range(len(chunk_calls)))
    assert all(len(base64.b64decode(c["data_base64"])) <= HUB_UPLOAD_CHUNK for c in chunk_calls)
    assert b"".join(base64.b64decode(c["data_base64"]) for c in chunk_calls) == data
    assert out["size"] == len(data)  # commit 未回传 size 时用本地长度兜底
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
    assert out["filename"] == "big.json"


def test_chunk_max_bytes_clamped_to_1mb(svc):
    """begin 返回 chunk_max_bytes=2MB：钳制到 HUB_DOWNLOAD_CHUNK=1MB。"""
    data = b"z" * (1024 * 1024 + 7)  # 1MB+7B
    backend = UploadBackend(chunk_max_bytes=2 * 1024 * 1024)
    _upload(svc, backend, data=data)

    chunk_calls = [args for action, args in backend.actions if action == "chunk"]
    assert len(chunk_calls) == 2
    sizes = [len(base64.b64decode(c["data_base64"])) for c in chunk_calls]
    assert sizes == [HUB_DOWNLOAD_CHUNK, 7]


def test_chunk_max_bytes_tiny_value_respected(svc):
    """begin 返回极小 chunk_max_bytes（如 1KB）：以其为准逐块上传。"""
    data = b"y" * 2500
    backend = UploadBackend(chunk_max_bytes=1024)
    _upload(svc, backend, data=data)
    chunk_calls = [args for action, args in backend.actions if action == "chunk"]
    assert len(chunk_calls) == 3
    sizes = [len(base64.b64decode(c["data_base64"])) for c in chunk_calls]
    assert sizes == [1024, 1024, 452]


def test_filename_sanitized_before_begin(svc):
    backend = UploadBackend()
    _upload(svc, backend, filename="../etc/passwd:报告*.md", data=b"d")
    assert backend.actions[0][1]["filename"] == "passwd_报告_.md"


def test_bearer_token_header_forwarded(tmp_path):
    record = []
    svc = HubFileService(
        endpoint=ENDPOINT, cache_root=str(tmp_path / "c"), bearer_token="tok-9"
    )
    backend = UploadBackend()
    with pytest.MonkeyPatch.context() as mp:
        _patch_mcp(mp, backend, record=record)
        svc.upload_bytes("a.md", b"data")
    for method, headers, _msg in record:
        if method == "tools/call":
            assert headers["Authorization"] == "Bearer tok-9"


def test_return_fallbacks_when_commit_sparse(svc):
    """commit 只回 file_id：filename/size/sha256 用本地值兜底。"""
    data = b"fallback"
    backend = UploadBackend(commit_payload={"ok": True, "file_id": "file-min"})
    out = _upload(svc, backend, filename="名.md", data=data)
    assert out == {
        "file_id": "file-min", "filename": "名.md",
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }


# ── 异常链路 ────────────────────────────────────────────────────────


def test_empty_data_raises_without_any_request(svc):
    record = []
    backend = UploadBackend()
    with pytest.MonkeyPatch.context() as mp:
        _patch_mcp(mp, backend, record=record)
        with pytest.raises(HubFileError, match="上传内容为空"):
            svc.upload_bytes("a.md", b"")
    assert record == []
    assert backend.actions == []


def test_begin_missing_upload_id_raises(svc):
    backend = UploadBackend(begin_payload={"ok": True})  # begin 成功但无 upload_id
    with pytest.raises(HubFileError, match="upload_id"):
        _upload(svc, backend)
    actions = [a for a, _ in backend.actions]
    assert actions == ["begin"]  # begin 阶段失败：不 chunk、不 commit、不 abort


def test_commit_missing_file_id_raises(svc):
    backend = UploadBackend(commit_payload={"ok": True})  # commit 成功但无 file_id
    with pytest.raises(HubFileError, match="file_id"):
        _upload(svc, backend)
    actions = [a for a, _ in backend.actions]
    assert actions == ["begin", "chunk", "commit"]


def test_chunk_failure_aborts_and_reraises(svc):
    backend = UploadBackend(fail_on_chunk_seq=0)
    with pytest.raises(HubFileError, match="服务端拒绝第 0 块"):
        _upload(svc, backend)
    actions = [a for a, _ in backend.actions]
    assert actions == ["begin", "chunk", "abort"]
    abort_args = backend.actions[-1][1]
    assert abort_args == {"action": "abort", "upload_id": "up-1"}


def test_mid_chunk_failure_aborts_partial_session(svc):
    """多块中途失败：已传块作废，abort 携带 upload_id。"""
    data = b"k" * (HUB_UPLOAD_CHUNK + 10)
    backend = UploadBackend(fail_on_chunk_seq=1)
    with pytest.raises(HubFileError):
        _upload(svc, backend, data=data)
    actions = [a for a, _ in backend.actions]
    assert actions == ["begin", "chunk", "chunk", "abort"]
    assert "commit" not in actions


def test_abort_failure_does_not_mask_original(svc):
    """abort 自身失败只告警：上抛的仍是 chunk 的原始异常。"""
    backend = UploadBackend(fail_on_chunk_seq=0, fail_abort=True)
    with pytest.raises(HubFileError, match="服务端拒绝第 0 块"):
        _upload(svc, backend)
    assert [a for a, _ in backend.actions] == ["begin", "chunk", "abort"]


def test_commit_failure_aborts(svc):
    """commit 阶段的 tools/call 失败同样触发 abort（会话在 try 块内）。"""
    backend = UploadBackend(commit_payload=("sha256 校验失败", True))
    with pytest.raises(HubFileError, match="sha256 校验失败"):
        _upload(svc, backend)
    assert [a for a, _ in backend.actions] == ["begin", "chunk", "commit", "abort"]


def test_endpoint_not_configured_raises(tmp_path, monkeypatch):
    monkeypatch.delenv(hub_file_service.HUB_ENDPOINT_ENV, raising=False)
    monkeypatch.setattr(
        hub_file_service, "get_settings",
        lambda: type("S", (), {"ANC_HUB_MCP_ENDPOINT": ""})(),
    )
    svc = HubFileService(cache_root=str(tmp_path / "c"))  # 无 endpoint
    with pytest.raises(HubEndpointNotConfiguredError):
        svc.upload_bytes("a.md", b"data")
