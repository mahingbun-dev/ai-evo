"""
anc-hub 导入 API 单元测试

覆盖：
- POST /hub-files/query（file_query 透传、错误映射）
- POST /datasets/import/sessions/from-hub（拉取 → 复用会话导入 preview/confirm 逻辑）
- 下载 → 落盘 → session_file_parser 解析的复用链路
- import_sessions 附加 file_extra（记录原始 file_id）
- POST /evaluation/datasets/import/hub-files + GET/POST /hub-import-jobs（阶段C 批量导入 job）

说明：按仓库既有 API 测试模式（参照 test_task_status_api.py）直接调用端点函数，
不经过 TestClient —— 规避存量测试引入 nest_asyncio 全局补丁后对
anyio portal 的跨文件污染（Python 3.14 下 asyncio Runner 兼容问题）。
"""
import asyncio
import base64
import hashlib
import io
import json
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.dataset_api import HubSessionImportRequest, import_sessions_from_hub
from app.api.hub_file_api import HubFileQueryRequest, query_hub_files
from app.services.hub_file_service import (
    HubEndpointNotConfiguredError,
    HubFileError,
)
from app.services.session_dataset_service import preview_session_files
from tests.unit.w9_direct_call import fake_request, fake_request_with_cookie  # W9: 端点直调适配

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

# 最小可解析的 Claude Code 会话 jsonl
SESSION_JSONL = (
    '{"type":"user","sessionId":"s1","timestamp":"2026-01-01T00:00:00.000Z",'
    '"message":{"role":"user","content":"帮我跑测试"}}\n'
    '{"type":"assistant","sessionId":"s1","timestamp":"2026-01-01T00:00:10.000Z",'
    '"message":{"role":"assistant","content":"好的，开始执行"}}\n'
)


def make_unpacked_info(directory, file_id, name, content, cached=False):
    """在 directory/file_id/ 下落一个真实文件，返回 ensure_unpacked 的返回值形态"""
    sub = directory / file_id
    sub.mkdir(parents=True, exist_ok=True)
    (sub / name).write_text(content, encoding="utf-8")
    return {
        "file_id": file_id, "unpack_dir": str(sub), "filename": name,
        "file_index": [{"path": name, "type": "text", "size": len(content),
                        "is_nested_zip": False}],
        "summary": None, "cached": cached, "record_id": f"rec-{file_id}",
    }


def make_zip_bytes(entries):
    """构造 zip 字节流：entries: [(zip 内路径, 内容 bytes), ...]"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# POST /hub-files/query
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _enable_hub_cookie_channel(monkeypatch):
    """开启 hub 凭证 Cookie 通道（中性 Cookie 名），并确保静态凭证未配置"""
    from types import SimpleNamespace
    monkeypatch.setattr(
        "app.services.hub_file_service._hub_sso_cookie_name",
        lambda: "PLATFORM_SSO")
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(ANC_HUB_BEARER_TOKEN="", HUB_SSO_COOKIE_NAME="PLATFORM_SSO"))


def fake_request_with_cookie(cookie="PLATFORM_SSO=tok-test"):
    """带平台 Cookie 头 + state.user 的 Request 替身（W9 归属校验 + 凭证提取）"""
    from tests.unit.w9_direct_call import fake_request_with_cookie as _fake
    return _fake(cookie)


class TestHubFilesQueryApi:
    def test_query_passthrough(self):
        """查询条件透传 file_query，服务端结果原样返回"""
        payload = {"ok": True, "total": 1,
                   "files": [{"file_id": "fid-1", "name": "session-x.jsonl", "size": 123}]}
        with patch("app.api.hub_file_api.HubFileService") as mock_cls:
            mock_cls.return_value.query_files.return_value = payload
            body = HubFileQueryRequest(name_like="session", uploader="zhang", page=1, page_size=20)
            result = query_hub_files(body, request=fake_request_with_cookie())
        assert result == payload
        # 以请求 Cookie 提取的凭证构造 service（用户身份透传 anc-hub）
        mock_cls.assert_called_once_with(bearer_token="tok-test")
        mock_cls.return_value.query_files.assert_called_once_with(
            name_like="session", uploader="zhang", page=1, page_size=20)

    def test_query_upstream_error_maps_502(self):
        """anc-hub 查询失败（空响应/工具错误）映射 502，中文 detail"""
        from fastapi import HTTPException

        with patch("app.api.hub_file_api.HubFileService") as mock_cls:
            mock_cls.return_value.query_files.side_effect = HubFileError("服务器返回空响应（已重试一次）")
            with pytest.raises(HTTPException) as exc:
                query_hub_files(HubFileQueryRequest(name_like="x"), request=fake_request_with_cookie())
        assert exc.value.status_code == 502
        assert "已重试一次" in exc.value.detail

    def test_query_endpoint_not_configured_maps_500(self):
        """端点未配置映射 500，中文 detail"""
        from fastapi import HTTPException

        with patch("app.api.hub_file_api.HubFileService") as mock_cls:
            mock_cls.return_value.query_files.side_effect = HubEndpointNotConfiguredError(
                "环境变量 ANC_HUB_MCP_ENDPOINT 未配置，无法访问 anc-hub 文件服务")
            with pytest.raises(HTTPException) as exc:
                query_hub_files(HubFileQueryRequest(), request=fake_request_with_cookie())
        assert exc.value.status_code == 500
        assert "ANC_HUB_MCP_ENDPOINT" in exc.value.detail

    def test_query_missing_platform_cookie_401(self):
        """请求缺平台登录 Cookie 时 401，中文提示检查凭证配置（纯在线查询无本地缓存可兜底）"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            query_hub_files(HubFileQueryRequest(), request=SimpleNamespace(headers={"cookie": "other=1"}))
        assert exc.value.status_code == 401
        assert "ANC_HUB_BEARER_TOKEN" in exc.value.detail

    def test_query_request_all_optional(self):
        """空请求体合法（全部条件可选）"""
        body = HubFileQueryRequest()
        assert body.model_dump(exclude_none=True) == {}

    def test_query_request_page_validation(self):
        """分页参数校验：page ≥ 1、page_size ≤ 200"""
        with pytest.raises(ValidationError):
            HubFileQueryRequest(page=0)
        with pytest.raises(ValidationError):
            HubFileQueryRequest(page_size=500)


# ---------------------------------------------------------------------------
# POST /datasets/import/sessions/from-hub
# ---------------------------------------------------------------------------

def run_from_hub(file_ids=None, name="", description="", real_dataset_id="",
                 unpack_side_effect=None, import_mock=None, import_return=None,
                 acquire_side_effect=None):
    """直接调用 from-hub 端点，返回各协作 mock 的命名空间

    端点现走 ensure_unpacked（物化到持久区）→ 选会话 → import_sessions
    → 按行 acquire_ref；失败路径回滚 discard。全部在 hub_file_service 源
    模块打补丁（端点函数体内 import，调用时才解析绑定）。
    """
    import asyncio
    from types import SimpleNamespace as NS

    body = HubSessionImportRequest(
        file_ids=file_ids or [], name=name, description=description, real_dataset_id=real_dataset_id)
    db = MagicMock()

    with patch("app.services.hub_file_service.HubFileService"), \
            patch("app.services.hub_file_service.ensure_unpacked") as mock_ensure, \
            patch("app.services.hub_file_service.acquire_ref") as mock_acquire, \
            patch("app.services.hub_file_service.release_ref") as mock_release, \
            patch("app.services.hub_file_service.discard_unpacked_if_unreferenced") as mock_discard, \
            patch("app.services.session_dataset_service.import_sessions") as mock_import:
        if unpack_side_effect is not None:
            mock_ensure.side_effect = unpack_side_effect
        if acquire_side_effect is not None:
            mock_acquire.side_effect = acquire_side_effect
        if import_mock is not None:
            mock_import.side_effect = import_mock
        else:
            mock_import.return_value = import_return if import_return is not None else {
                "dataset_id": "ds-new", "count": 1, "hub_file_refs": []}
        result = asyncio.run(import_sessions_from_hub(body, request=fake_request(), db=db))
    return NS(result=result, mock_import=mock_import, mock_ensure=mock_ensure,
              mock_acquire=mock_acquire, mock_release=mock_release,
              mock_discard=mock_discard, db=db)


class TestFromHubImportApi:
    def test_import_ok_and_reuses_session_import(self, tmp_path):
        """物化后复用既有 import_sessions 落库：entries/file_extra/name 正确传递"""
        infos = {
            "fid-1": make_unpacked_info(tmp_path, "fid-1", "s1.jsonl", SESSION_JSONL),
            "fid-2": make_unpacked_info(tmp_path, "fid-2", "s2.jsonl", SESSION_JSONL),
        }
        r = run_from_hub(
            file_ids=["fid-1", "fid-2"], name="hub 会话集", description="d",
            unpack_side_effect=lambda db, fid, **kw: infos[fid])

        assert r.result.dataset_id == "ds-new"
        assert r.result.count == 1  # mock_import 缺省 count=1
        assert "成功导入" in r.result.message

        kwargs = r.mock_import.call_args.kwargs
        assert kwargs["name"] == "hub 会话集"
        assert kwargs["real_dataset_id"] is None
        entries = r.mock_import.call_args.args[1]
        assert [e[0] for e in entries] == ["s1.jsonl", "s2.jsonl"]
        assert [e[1] for e in entries] == [
            str(tmp_path / "fid-1" / "s1.jsonl"), str(tmp_path / "fid-2" / "s2.jsonl")]
        # 条目唯一键 = file_id（三元组第三项），file_extra 按它归属
        assert [e[2] for e in entries] == ["fid-1", "fid-2"]
        # 导入数据自动记录原始 file_id（file_extra 以 file_id 为键）
        assert kwargs["file_extra"] == {
            "fid-1": {"session_file_id": "fid-1", "session_file_name": "s1.jsonl"},
            "fid-2": {"session_file_id": "fid-2", "session_file_name": "s2.jsonl"},
        }
        # db 会话透传给 import_sessions（第一个位置参数）与 ensure_unpacked
        assert r.mock_import.call_args.args[0] is r.db
        assert r.mock_ensure.call_args_list[0].args[0] is r.db
        assert [c.args[1] for c in r.mock_ensure.call_args_list] == ["fid-1", "fid-2"]
        # 成功路径不回滚、不释放
        r.mock_discard.assert_not_called()
        r.mock_release.assert_not_called()

    def test_acquire_refs_per_row_multiplicity(self, tmp_path):
        """导入成功后按数据行粒度 acquire_ref（逐行多重），与释放端粒度成对"""
        infos = {"fid-1": make_unpacked_info(tmp_path, "fid-1", "s1.jsonl", SESSION_JSONL)}
        r = run_from_hub(
            file_ids=["fid-1"],
            unpack_side_effect=lambda db, fid, **kw: infos[fid],
            import_return={"dataset_id": "ds-new", "count": 3,
                           "hub_file_refs": ["fid-1", "fid-1", "fid-2"]})
        assert [c.args[1] for c in r.mock_acquire.call_args_list] == \
            ["fid-1", "fid-1", "fid-2"]
        r.mock_release.assert_not_called()

    def test_acquire_failure_compensates_and_maps_500(self, tmp_path):
        """acquire 中途失败：补偿释放已持引用并 500；数据行已落库，绝不回滚物化"""
        import asyncio
        from fastapi import HTTPException

        infos = {"fid-1": make_unpacked_info(tmp_path, "fid-1", "s1.jsonl", SESSION_JSONL)}
        body = HubSessionImportRequest(file_ids=["fid-1"], name="n")
        db = MagicMock()
        calls = {"n": 0}

        def _acquire(_db, fid):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise HubFileError(f"执行包未登记（file_id={fid}），请先导入解包后再引用")

        with patch("app.services.hub_file_service.HubFileService"), \
                patch("app.services.hub_file_service.ensure_unpacked") as mock_ensure, \
                patch("app.services.hub_file_service.acquire_ref") as mock_acquire, \
                patch("app.services.hub_file_service.release_ref") as mock_release, \
                patch("app.services.hub_file_service.discard_unpacked_if_unreferenced") as mock_discard, \
                patch("app.services.session_dataset_service.import_sessions") as mock_import:
            mock_ensure.side_effect = lambda _db, fid, **kw: infos[fid]
            mock_acquire.side_effect = _acquire
            mock_import.return_value = {"dataset_id": "ds-new", "count": 3,
                                        "hub_file_refs": ["fid-1", "fid-1", "fid-2"]}
            with pytest.raises(HTTPException) as exc:
                asyncio.run(import_sessions_from_hub(body, request=fake_request(), db=db))
            assert exc.value.status_code == 500
            # 已成功持有 fid-1 两次 → 补偿释放两次；失败那次（fid-2）不释放
            assert [c.args[1] for c in mock_release.call_args_list] == ["fid-1", "fid-1"]
            # 数据行已落库：绝不回滚物化（否则数据行引用悬空）
            mock_discard.assert_not_called()

    def test_import_same_name_files_keyed_by_file_id(self, tmp_path):
        """两个不同 file_id 的文件同名：file_extra 按 file_id 为键，不互相覆盖（缺陷回归）"""
        infos = {
            "fid-a": make_unpacked_info(tmp_path, "fid-a", "same.jsonl", SESSION_JSONL),
            "fid-b": make_unpacked_info(tmp_path, "fid-b", "same.jsonl", SESSION_JSONL),
        }
        r = run_from_hub(
            file_ids=["fid-a", "fid-b"], name="同名集",
            unpack_side_effect=lambda db, fid, **kw: infos[fid])
        entries = r.mock_import.call_args.args[1]
        assert [e[0] for e in entries] == ["same.jsonl", "same.jsonl"]
        assert [e[2] for e in entries] == ["fid-a", "fid-b"]
        assert r.mock_import.call_args.kwargs["file_extra"] == {
            "fid-a": {"session_file_id": "fid-a", "session_file_name": "same.jsonl"},
            "fid-b": {"session_file_id": "fid-b", "session_file_name": "same.jsonl"},
        }

    def test_import_dedupes_file_ids(self, tmp_path):
        """重复 file_id 去重，只物化一次"""
        infos = {"fid-1": make_unpacked_info(tmp_path, "fid-1", "s1.jsonl", SESSION_JSONL)}
        r = run_from_hub(
            file_ids=["fid-1", "fid-1", " fid-1 "],
            unpack_side_effect=lambda db, fid, **kw: infos[fid])
        assert r.mock_ensure.call_count == 1
        assert r.mock_import.call_count == 1

    def test_empty_file_ids_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_from_hub(file_ids=[])
        assert exc.value.status_code == 400
        assert "file_id" in exc.value.detail

    def test_blank_file_ids_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_from_hub(file_ids=[" ", ""])
        assert exc.value.status_code == 400

    def test_request_requires_file_ids(self):
        """请求体缺少 file_ids 时 pydantic 校验失败（FastAPI 层为 422）"""
        with pytest.raises(ValidationError):
            HubSessionImportRequest(name="x")

    def test_download_error_maps_502(self):
        """sha256 校验失败等下载错误映射 502"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_from_hub(file_ids=["fid-9"],
                         unpack_side_effect=HubFileError("文件 fid-9 sha256 校验失败: 服务端 a，落盘 b"))
        assert exc.value.status_code == 502
        assert "sha256 校验失败" in exc.value.detail

    def test_endpoint_not_configured_maps_500(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_from_hub(file_ids=["fid-1"],
                         unpack_side_effect=HubEndpointNotConfiguredError(
                             "环境变量 ANC_HUB_MCP_ENDPOINT 未配置，无法访问 anc-hub 文件服务"))
        assert exc.value.status_code == 500
        assert "未配置" in exc.value.detail

    def test_import_value_error_maps_400(self, tmp_path):
        """会话解析/文件类型错误（ValueError）映射 400"""
        from fastapi import HTTPException

        infos = {"fid-1": make_unpacked_info(tmp_path, "fid-1", "s1.jsonl", SESSION_JSONL)}
        with pytest.raises(HTTPException) as exc:
            run_from_hub(file_ids=["fid-1"],
                         unpack_side_effect=lambda db, fid, **kw: infos[fid],
                         import_mock=ValueError("文件 s1.jsonl 未解析出有效会话"))
        assert exc.value.status_code == 400
        assert "未解析出有效会话" in exc.value.detail

    def test_import_append_mode_passes_real_dataset_id(self, tmp_path):
        infos = {"fid-1": make_unpacked_info(tmp_path, "fid-1", "s1.jsonl", SESSION_JSONL)}
        r = run_from_hub(
            file_ids=["fid-1"], real_dataset_id="ds-real",
            unpack_side_effect=lambda db, fid, **kw: infos[fid])
        assert r.mock_import.call_args.kwargs["real_dataset_id"] == "ds-real"

    def test_no_session_file_discards_only_fresh(self, tmp_path):
        """zip 内无会话文件 400：仅回滚本次新物化（cached=False）的登记，
        登记表命中（cached=True，可能被其他数据集引用）的绝不动"""
        import asyncio
        from fastapi import HTTPException

        png_only = {
            "file_id": "fid-b", "unpack_dir": str(tmp_path / "fid-b"),
            "filename": "pic.zip",
            "file_index": [{"path": "pic.png", "type": "image", "size": 3,
                            "is_nested_zip": False}],
            "summary": None, "cached": False, "record_id": "rec-b",
        }
        cached_hit = make_unpacked_info(tmp_path, "fid-a", "s1.jsonl", SESSION_JSONL, cached=True)
        infos = {"fid-a": cached_hit, "fid-b": png_only}
        body = HubSessionImportRequest(file_ids=["fid-a", "fid-b"], name="n")
        db = MagicMock()

        with patch("app.services.hub_file_service.HubFileService"), \
                patch("app.services.hub_file_service.ensure_unpacked") as mock_ensure, \
                patch("app.services.hub_file_service.acquire_ref"), \
                patch("app.services.hub_file_service.release_ref"), \
                patch("app.services.hub_file_service.discard_unpacked_if_unreferenced") as mock_discard, \
                patch("app.services.session_dataset_service.import_sessions") as mock_import:
            mock_ensure.side_effect = lambda _db, fid, **kw: infos[fid]
            with pytest.raises(HTTPException) as exc:
                asyncio.run(import_sessions_from_hub(body, request=fake_request(), db=db))
            assert exc.value.status_code == 400
            assert "未找到会话文件" in exc.value.detail
            mock_import.assert_not_called()
            # 只回滚本次新物化的 fid-b；fid-a 是登记命中，绝不能清理
            assert [c.args[1] for c in mock_discard.call_args_list] == ["fid-b"]


# ---------------------------------------------------------------------------
# 复用链路：下载落盘 → session_file_parser 解析 → import_sessions 落库
# ---------------------------------------------------------------------------

class TestFromHubReuseChain:
    def test_downloaded_file_feeds_session_preview(self, tmp_path):
        """下载落盘的 jsonl 能被既有会话预览逻辑解析（preview 复用验证）"""
        from app.services.hub_file_service import HubFileService

        part = SESSION_JSONL.encode("utf-8")
        full_sha = hashlib.sha256(part).hexdigest()

        def _post(url, data=None, headers=None, timeout=None, **kwargs):
            msg = json.loads(data)
            if msg.get("method") == "initialize":
                return FakeMcpResp(rpc_result(msg.get("id"), {}), headers={"mcp-session-id": "s"})
            if msg.get("method") == "notifications/initialized":
                return FakeMcpResp("")
            if msg.get("method") == "tools/call":
                pl = {"ok": True, "data_base64": base64.b64encode(part).decode(),
                      "bytes_read": len(part), "eof": True, "sha256": full_sha,
                      "size": len(part), "filename": "session-run.jsonl"}
                return FakeMcpResp(rpc_result(msg["id"], {"structuredContent": {"ok": True, "payload": pl}}))
            raise AssertionError("未预期的 MCP 方法")

        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = _post
            mock_requests.delete.return_value = MagicMock()
            svc = HubFileService(endpoint="http://hub.test/mcp", cache_root=str(tmp_path / "cache"))
            info = svc.download_to_file("fid-chain", dest_dir=str(tmp_path / "dl"))

        result = preview_session_files([("session-run.jsonl", info["path"])])
        assert result["valid_sessions"] == 1
        item = result["items"][0]
        assert item["valid"] is True
        assert item["source"] == "claude_code"
        assert "帮我跑测试" in item["summary"]

    def test_import_sessions_attaches_file_extra(self, tmp_path):
        """import_sessions 新增 file_extra 参数：每条数据附加原始 file_id（向后兼容：缺省不附加）"""
        from app.services.session_dataset_service import import_sessions

        src = tmp_path / "s1.jsonl"
        src.write_text(SESSION_JSONL, encoding="utf-8")

        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 1

        added = []
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.services.dataset_service.DatasetDataService") as mock_das, \
                patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs:
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-hub")
            mock_das.return_value.add_data_v2.side_effect = \
                lambda dataset_id, data: added.append(data)

            result = import_sessions(
                db,
                [("s1.jsonl", str(src))],
                name="hub 集合",
                file_extra={"s1.jsonl": {"session_file_id": "fid-1", "session_file_name": "s1.jsonl"}},
            )

        assert result["dataset_id"] == "ds-hub" and result["count"] == 1
        # 本次创建的数据行持有 fid-1 一次（与释放端逐行粒度成对）
        assert result["hub_file_refs"] == ["fid-1"]
        assert len(added) == 1
        assert added[0]["session_file_id"] == "fid-1"
        assert added[0]["session_file_name"] == "s1.jsonl"
        # 原有会话字段保持完整
        assert added[0]["session_id"] == "s1"
        assert added[0]["source"] == "claude_code"
        assert "帮我跑测试" in added[0]["user_input"]
        assert "session_messages" in added[0]
        mock_ds.return_value.create_dataset.assert_called_once()

    def test_import_sessions_without_file_extra_unchanged(self, tmp_path):
        """不传 file_extra（原有 confirm 调用路径）行为不变"""
        from app.services.session_dataset_service import import_sessions

        src = tmp_path / "s1.jsonl"
        src.write_text(SESSION_JSONL, encoding="utf-8")

        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 1

        added = []
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.services.dataset_service.DatasetDataService") as mock_das, \
                patch("app.services.dataset_field_service.DatasetFieldService"):
            mock_ds.return_value.get_dataset.return_value = SimpleNamespace(id="ds-real")
            mock_das.return_value.add_data_v2.side_effect = \
                lambda dataset_id, data: added.append(data)

            result = import_sessions(db, [("s1.jsonl", str(src))], real_dataset_id="ds-real")

        assert result["dataset_id"] == "ds-real" and result["count"] == 1
        # 无 file_extra（本地上传路径）不产生任何执行包引用
        assert result["hub_file_refs"] == []
        assert added[0].get("session_file_id") is None

    def test_import_sessions_same_name_files_correct_attribution(self, tmp_path):
        """同名文件（不同 entry_key=file_id）导入后每条数据归属正确的 session_file_id（缺陷回归）"""
        from app.services.session_dataset_service import import_sessions

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "same.jsonl").write_text(SESSION_JSONL, encoding="utf-8")
        (dir_b / "same.jsonl").write_text(SESSION_JSONL, encoding="utf-8")

        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 1

        added = []
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.services.dataset_service.DatasetDataService") as mock_das, \
                patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs:
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-same")
            mock_das.return_value.add_data_v2.side_effect = \
                lambda dataset_id, data: added.append(data)
            mock_fs.return_value.get_field_metadata.return_value = []

            result = import_sessions(
                db,
                [("same.jsonl", str(dir_a / "same.jsonl"), "fid-a"),
                 ("same.jsonl", str(dir_b / "same.jsonl"), "fid-b")],
                name="同名集",
                file_extra={
                    "fid-a": {"session_file_id": "fid-a", "session_file_name": "same.jsonl"},
                    "fid-b": {"session_file_id": "fid-b", "session_file_name": "same.jsonl"},
                },
            )

        assert result["dataset_id"] == "ds-same" and result["count"] == 2
        # 两行各持一次引用：逐行多重（同一 fid 两行 → 两次）
        assert sorted(result["hub_file_refs"]) == ["fid-a", "fid-b"]
        assert sorted(d["session_file_id"] for d in added) == ["fid-a", "fid-b"]

    def test_hub_import_writes_hub_file_field_metadata(self, tmp_path):
        """hub 导入回填字段元数据：session_file_id 类型为 hub_file（决策 #1 导入页签回填落地）"""
        from app.services.session_dataset_service import import_sessions

        src = tmp_path / "s1.jsonl"
        src.write_text(SESSION_JSONL, encoding="utf-8")

        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 1

        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.services.dataset_service.DatasetDataService"), \
                patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs:
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-hub")
            mock_fs.return_value.get_field_metadata.return_value = []

            import_sessions(
                db,
                [("s1.jsonl", str(src), "fid-1")],
                name="hub 元数据集",
                file_extra={"fid-1": {"session_file_id": "fid-1", "session_file_name": "s1.jsonl"}},
            )

            saved = mock_fs.return_value.save_field_metadata.call_args.args[1]
        by_name = {f["name"]: f for f in saved}
        assert by_name["session_file_id"]["type"] == "hub_file"
        assert by_name["session_file_name"]["type"] == "string"
        # 标准会话字段保持完整
        assert {"user_input", "source", "session_summary", "session_stats", "session_messages"} \
            <= set(by_name)

    def test_plain_import_writes_session_metadata_only(self, tmp_path):
        """普通（本地上传）会话导入不写 hub 回填字段元数据"""
        from app.services.session_dataset_service import import_sessions

        src = tmp_path / "s1.jsonl"
        src.write_text(SESSION_JSONL, encoding="utf-8")

        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 1

        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.services.dataset_service.DatasetDataService"), \
                patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs:
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-plain")
            mock_fs.return_value.get_field_metadata.return_value = []

            import_sessions(db, [("s1.jsonl", str(src))], name="普通会话集")

            saved = mock_fs.return_value.save_field_metadata.call_args.args[1]
        assert "session_file_id" not in {f["name"] for f in saved}

    def test_append_plain_import_keeps_existing_hub_metadata(self, tmp_path):
        """追加普通导入保留数据集已有字段元数据（含此前 hub 回填的 hub_file 字段，防覆盖退化）"""
        from app.services.session_dataset_service import import_sessions

        src = tmp_path / "s1.jsonl"
        src.write_text(SESSION_JSONL, encoding="utf-8")

        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 1

        existing = [{"name": "session_file_id", "label": "会话原始文件 ID（anc-hub）",
                     "type": "hub_file", "is_media": False, "is_media_field": None,
                     "is_required": False, "position": 5}]
        added = []
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.services.dataset_service.DatasetDataService") as mock_das, \
                patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs:
            mock_ds.return_value.get_dataset.return_value = SimpleNamespace(id="ds-real")
            mock_das.return_value.add_data_v2.side_effect = \
                lambda dataset_id, data: added.append(data)
            mock_fs.return_value.get_field_metadata.return_value = existing

            import_sessions(db, [("s1.jsonl", str(src))], real_dataset_id="ds-real")

            saved = mock_fs.return_value.save_field_metadata.call_args.args[1]
        by_name = {f["name"]: f for f in saved}
        # SESSION 标准字段仍在，且既有 hub_file 字段未被覆盖删除
        assert {"user_input", "source", "session_summary", "session_stats", "session_messages"} \
            <= set(by_name)
        assert by_name["session_file_id"]["type"] == "hub_file"

# ---------------------------------------------------------------------------
# from-hub zip 兼容（anc-hub 服务端打包）：MCP 返回 zip 字节流的真实链路
# ---------------------------------------------------------------------------

class TestFromHubZipCompat:
    def _run_with_zip(self, mock_requests, monkeypatch, tmp_path, zip_bytes, zip_name,
                      file_ids=("fid-zip",), import_side_effect=None):
        """mock MCP 返回 zip 字节流，直接调 from-hub 端点走真实下载/解压/物化链路

        ensure_unpacked 现为真实链路：解包根与下载缓存根重定向到 tmp_path，
        避免测试落盘到仓库 uploads/；acquire/release 引用计数打桩（db 为 MagicMock）。
        """
        import asyncio

        from app.services.hub_file_service import HubFileService

        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", "http://hub.test/mcp")
        monkeypatch.setattr(
            "app.services.hub_file_service._hub_datasets_root",
            lambda root=None: str(tmp_path / "hub_datasets"))
        real_init = HubFileService.__init__

        def _patched_init(self, *args, **kwargs):
            kwargs.setdefault("cache_root", str(tmp_path / "cache"))
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(HubFileService, "__init__", _patched_init)
        full_sha = hashlib.sha256(zip_bytes).hexdigest()

        def _post(url, data=None, headers=None, timeout=None, **kwargs):
            msg = json.loads(data)
            if msg.get("method") == "initialize":
                return FakeMcpResp(rpc_result(msg.get("id"), {}), headers={"mcp-session-id": "s"})
            if msg.get("method") == "notifications/initialized":
                return FakeMcpResp("")
            if msg.get("method") == "tools/call":
                pl = {"ok": True, "data_base64": base64.b64encode(zip_bytes).decode(),
                      "bytes_read": len(zip_bytes), "eof": True, "sha256": full_sha,
                      "size": len(zip_bytes), "filename": zip_name}
                return FakeMcpResp(rpc_result(msg["id"], {"structuredContent": {"ok": True, "payload": pl}}))
            raise AssertionError("未预期的 MCP 方法")

        mock_requests.post.side_effect = _post
        mock_requests.delete.return_value = MagicMock()

        body = HubSessionImportRequest(file_ids=list(file_ids), name="hub zip 集")
        db = MagicMock()
        with patch("app.services.session_dataset_service.import_sessions") as mock_import, \
                patch("app.services.hub_file_service.acquire_ref"), \
                patch("app.services.hub_file_service.release_ref"):
            mock_import.return_value = {"dataset_id": "ds-zip", "count": 1, "hub_file_refs": []}
            if import_side_effect is not None:
                mock_import.side_effect = import_side_effect
            result = asyncio.run(import_sessions_from_hub(body, request=fake_request(), db=db))
        return result, mock_import

    @patch("app.services.hub_file_service.requests")
    def test_single_entry_zip_imports_inner_jsonl(self, mock_requests, monkeypatch, tmp_path):
        """单条目 zip（claude-threads 形态）：导入内层 jsonl，session_file_name 不再带 __哈希.zip（缺陷回归）"""
        inner_name = "46471c07-b92a-49cd-8e51-f7e553501242.jsonl"
        zip_bytes = make_zip_bytes([(inner_name, SESSION_JSONL.encode("utf-8"))])
        checked = {}

        def _capture(db, entries, **kwargs):
            # 导入时刻解压产物仍在临时 workdir，验证真实可解析
            checked["preview"] = preview_session_files([(entries[0][0], entries[0][1])])
            return {"dataset_id": "ds-zip", "count": 1}

        result, mock_import = self._run_with_zip(
            mock_requests, monkeypatch, tmp_path, zip_bytes,
            f"{inner_name}__fafc81b0.zip", import_side_effect=_capture)

        assert result.dataset_id == "ds-zip"
        entries = mock_import.call_args.args[1]
        assert len(entries) == 1
        assert entries[0][0] == inner_name
        assert entries[0][1].endswith(inner_name)
        assert entries[0][2] == "fid-zip"
        assert mock_import.call_args.kwargs["file_extra"]["fid-zip"] == {
            "session_file_id": "fid-zip", "session_file_name": inner_name}
        assert checked["preview"]["valid_sessions"] == 1

    @patch("app.services.hub_file_service.requests")
    def test_zip_filters_non_session_files(self, mock_requests, monkeypatch, tmp_path):
        """多文件 zip：仅 .jsonl/.json 进会话导入，图片、md 等被忽略"""
        zip_bytes = make_zip_bytes([
            ("README.md", b"readme"),
            ("uploads/pic.png", b"\x89PNG"),
            ("flow/flow_001.md", b"# flow"),
            ("run-1.jsonl", SESSION_JSONL.encode("utf-8")),
        ])
        result, mock_import = self._run_with_zip(
            mock_requests, monkeypatch, tmp_path, zip_bytes, "anc-result__ab62eb1f.zip")

        entries = mock_import.call_args.args[1]
        assert [e[0] for e in entries] == ["run-1.jsonl"]
        assert mock_import.call_args.kwargs["file_extra"]["fid-zip"]["session_file_name"] == "run-1.jsonl"

    @patch("app.services.hub_file_service.requests")
    def test_zip_without_session_files_maps_400(self, mock_requests, monkeypatch, tmp_path):
        """zip 内没有任何 .jsonl/.json：400 中文错误，不落库"""
        from fastapi import HTTPException

        zip_bytes = make_zip_bytes([("pic.png", b"\x89PNG"), ("a.md", b"md")])
        with pytest.raises(HTTPException) as exc:
            self._run_with_zip(
            mock_requests, monkeypatch, tmp_path, zip_bytes, "only-images.zip")
        assert exc.value.status_code == 400
        assert "未找到会话文件" in exc.value.detail

    @patch("app.services.hub_file_service.requests")
    def test_zip_multiple_jsonl_imports_each(self, mock_requests, monkeypatch, tmp_path):
        """zip 内多个 jsonl：每个都作为会话条目导入（同一 file_id 归属）"""
        zip_bytes = make_zip_bytes([
            ("a.jsonl", SESSION_JSONL.encode("utf-8")),
            ("sub/b.json", SESSION_JSONL.replace("s1", "s2").encode("utf-8")),
        ])
        result, mock_import = self._run_with_zip(
            mock_requests, monkeypatch, tmp_path, zip_bytes, "multi-sess__00000000.zip")

        entries = mock_import.call_args.args[1]
        assert sorted(e[0] for e in entries) == ["a.jsonl", "b.json"]
        assert {e[2] for e in entries} == {"fid-zip"}
        # 多会话文件时 session_file_name 记 zip 名（同一 file_id 无法区分内层文件）
        extra = mock_import.call_args.kwargs["file_extra"]["fid-zip"]
        assert extra == {"session_file_id": "fid-zip", "session_file_name": "multi-sess__00000000.zip"}

    @patch("app.services.hub_file_service.requests")
    def test_nested_thread_zip_wins_over_outer_json(self, mock_requests, monkeypatch, tmp_path):
        """含嵌套 claude-thread.zip：只从嵌套 jsonl 导入，外层干扰 json 全忽略（缺陷回归）

        多文件 zip 混有 environment.json 等非会话 json，此前被当作会话文件
        传给 import_sessions 导致「未解析出有效会话」整体失败。
        """
        inner_name = "claude-thread.jsonl"
        nested = make_zip_bytes([(inner_name, SESSION_JSONL.encode("utf-8"))])
        zip_bytes = make_zip_bytes([
            ("62ed5-attempt-1/environment.json", b'{"Browser": "chrome"}'),
            ("62ed5-attempt-1/execution-manifest.json", b'{"attempts": 1}'),
            ("62ed5-attempt-1/case_list.json", b'[]'),
            ("62ed5-attempt-1/claude-thread.zip", nested),
            ("62ed5-attempt-1/flow/flow_001.md", b"# flow"),
        ])
        checked = {}

        def _capture(db, entries, **kwargs):
            checked["preview"] = preview_session_files([(entries[0][0], entries[0][1])])
            return {"dataset_id": "ds-nested", "count": 1}

        result, mock_import = self._run_with_zip(
            mock_requests, monkeypatch, tmp_path, zip_bytes, "62ed5-attempt-1.zip",
            import_side_effect=_capture)

        entries = mock_import.call_args.args[1]
        assert len(entries) == 1
        assert entries[0][0] == inner_name  # 嵌套内层 jsonl，而非 environment.json
        assert entries[0][1].endswith(inner_name)
        assert checked["preview"]["valid_sessions"] == 1
        assert mock_import.call_args.kwargs["file_extra"]["fid-zip"] == {
            "session_file_id": "fid-zip", "session_file_name": inner_name}

    @patch("app.services.hub_file_service.requests")
    def test_nested_thread_zip_underscore_variant_matched(self, mock_requests, monkeypatch, tmp_path):
        """嵌套会话包下划线命名 claude_thread.zip 同样命中（业务口径变体）"""
        nested = make_zip_bytes([("thread.jsonl", SESSION_JSONL.encode("utf-8"))])
        zip_bytes = make_zip_bytes([
            ("env/environment.json", b'{}'),
            ("env/claude_thread.zip", nested),
        ])
        _, mock_import = self._run_with_zip(
            mock_requests, monkeypatch, tmp_path, zip_bytes, "anc-result__11111111.zip")

        entries = mock_import.call_args.args[1]
        assert [e[0] for e in entries] == ["thread.jsonl"]
        assert mock_import.call_args.kwargs["file_extra"]["fid-zip"]["session_file_name"] == "thread.jsonl"

    @patch("app.services.hub_file_service.requests")
    def test_nested_thread_zip_without_jsonl_maps_400(self, mock_requests, monkeypatch, tmp_path):
        """嵌套 thread zip 内没有 .jsonl/.json：400 中文错误，不落库"""
        from fastapi import HTTPException

        nested = make_zip_bytes([("pic.png", b"\x89PNG")])
        zip_bytes = make_zip_bytes([
            ("environment.json", b'{}'),
            ("claude-thread.zip", nested),
        ])
        with pytest.raises(HTTPException) as exc:
            self._run_with_zip(
            mock_requests, monkeypatch, tmp_path, zip_bytes, "bad-thread.zip")
        assert exc.value.status_code == 400
        assert "未找到会话文件" in exc.value.detail


# ---------------------------------------------------------------------------
# MCP mock 小工具（与 test_hub_file_service.py 同构）
# ---------------------------------------------------------------------------

class FakeMcpResp:
    """模拟 requests.Response：content 是原始字节（UTF-8），与真实契约一致"""

    def __init__(self, text="", status_code=200, headers=None):
        if isinstance(text, bytes):
            self.content = text
        else:
            self.content = (text or "").encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}


def rpc_result(id_, result) -> str:
    return 'data: ' + json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n\n"


# ---------------------------------------------------------------------------
# 阶段C：批量导入 job（POST import/hub-files、GET /hub-import-jobs/{id}、POST retry）
# ---------------------------------------------------------------------------

def hub_job_db(job, dataset_team=None):
    """W9 适配：ensure_hub_import_job_access 的 join 查询链替身
    （返回 (job, dataset) 元组；dataset.team_code 可控）"""
    dataset = SimpleNamespace(id="ds-p", team_code=dataset_team)
    db = MagicMock()
    db.query.return_value.outerjoin.return_value.filter.return_value.first.return_value = (
        job, dataset)
    return db

def run_import_hub_files(file_ids=None, name="", description="", real_dataset_id="",
                         create_dataset_ret=SimpleNamespace(id="ds-api"),
                         get_dataset_ret=None, dispatch_error=None):
    """直接调用创建端点，返回 (response, added_jobs, mock_task, db)"""
    import asyncio

    from app.api.hub_import_api import HubFilesImportRequest, import_hub_files

    body = HubFilesImportRequest(
        file_ids=file_ids or [], name=name, description=description,
        real_dataset_id=real_dataset_id)
    db = MagicMock()
    added = []
    db.add.side_effect = lambda obj: added.append(obj)

    with patch("app.services.dataset_service.DatasetService") as mock_ds, \
            patch("app.tasks.celery_tasks.run_hub_import_job") as mock_task:
        mock_ds.return_value.create_dataset.return_value = create_dataset_ret
        mock_ds.return_value.get_dataset.return_value = get_dataset_ret
        if dispatch_error is not None:
            mock_task.apply_async.side_effect = dispatch_error
        result = import_hub_files(body, request=fake_request(), db=db)
    return result, added, mock_task, db


class TestHubImportJobApi:
    def test_create_job_dispatches_celery(self):
        """创建 job：新建数据集、返回 job_id 并投递 Celery"""
        from app.models.hub_file_models import HubImportJob

        result, added, mock_task, _ = run_import_hub_files(
            file_ids=["fid-1", "fid-1", "fid-2"], name="执行包集", description="d")
        assert result.dataset_id == "ds-api"
        assert result.status == "pending"
        assert result.total_count == 2  # 去重后
        assert result.id.startswith("hubjob_")
        # job 已落库（db.add）并投递 Celery（delay 传 job_id）
        assert len(added) == 1 and isinstance(added[0], HubImportJob)
        mock_task.apply_async.assert_called_once_with(
            args=[result.id], kwargs={"bearer_token": None}, kwargsrepr="{<masked>}")

    def test_create_user_code_passed_through(self):
        """请求态里的当前用户透传为 created_by（与 from-hub 端点同手法）"""
        import asyncio

        from app.api.hub_import_api import HubFilesImportRequest, import_hub_files

        body = HubFilesImportRequest(file_ids=["fid-u"], name="n")
        db = MagicMock()
        request = MagicMock()
        request.state.user = {"user_code": "u001"}
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.tasks.celery_tasks.run_hub_import_job"):
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-u")
            result = import_hub_files(body, request=request, db=db)
        assert mock_ds.return_value.create_dataset.call_args.kwargs["created_by"] == "u001"

    def test_create_empty_file_ids_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_import_hub_files(file_ids=[])
        assert exc.value.status_code == 400
        assert "file_id" in exc.value.detail

    def test_create_blank_file_ids_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_import_hub_files(file_ids=[" ", ""])
        assert exc.value.status_code == 400

    def test_create_append_missing_dataset_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_import_hub_files(file_ids=["fid-1"], real_dataset_id="ds-gone")
        assert exc.value.status_code == 400
        assert "目标数据集不存在" in exc.value.detail

    def test_create_new_without_name_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_import_hub_files(file_ids=["fid-1"], name="  ")
        assert exc.value.status_code == 400
        assert "名称不能为空" in exc.value.detail

    def test_create_append_mode_ok(self):
        result, _, mock_task, _ = run_import_hub_files(
            file_ids=["fid-1"], real_dataset_id="ds-real",
            get_dataset_ret=SimpleNamespace(id="ds-real"))
        assert result.dataset_id == "ds-real"
        mock_task.apply_async.assert_called_once()

    def test_create_dispatch_failure_500(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            run_import_hub_files(file_ids=["fid-1"], name="n",
                                 dispatch_error=ConnectionError("broker 不可达"))
        assert exc.value.status_code == 500
        assert "投递失败" in exc.value.detail

    def test_get_job_progress(self):
        """GET 轮询返回进度：状态/计数/失败明细/时间字符串化"""
        from datetime import datetime

        from app.api.hub_import_api import get_hub_import_job

        job = SimpleNamespace(
            id="hubjob_p1", dataset_id="ds-p", file_ids=["fid-1", "fid-2"],
            status="failed", total_count=2, success_count=1, fail_count=1,
            fail_detail={"fid-2": "不是有效的 zip 文件: bad.zip"},
            created_at=datetime(2026, 10, 8, 12, 0, 0))
        with patch("app.api.hub_import_api.get_job", return_value=job):
            resp = get_hub_import_job("hubjob_p1", request=fake_request(), db=hub_job_db(job))
        assert resp.status == "failed"
        assert resp.success_count == 1 and resp.fail_count == 1
        assert resp.fail_detail == {"fid-2": "不是有效的 zip 文件: bad.zip"}
        assert resp.created_at == "2026-10-08 12:00:00"

    def test_get_job_not_found_404(self):
        from fastapi import HTTPException

        from app.api.hub_import_api import get_hub_import_job

        with patch("app.api.hub_import_api.get_job", return_value=None):
            with pytest.raises(HTTPException) as exc:
                get_hub_import_job("hubjob_gone", request=fake_request(), db=hub_job_db(None))
        assert exc.value.status_code == 404

    def test_retry_failed_job_redispatches(self):
        """retry：重置 pending、只重跑失败项（幂等由服务层保证）并重新投递"""
        from app.api.hub_import_api import retry_hub_import_job

        job = SimpleNamespace(
            id="hubjob_r1", dataset_id="ds-r", file_ids=["fid-1", "fid-2"],
            status="pending", total_count=2, success_count=1, fail_count=1,
            fail_detail={"fid-2": "x"}, created_at=None)

        with patch("app.api.hub_import_api.retry_job", return_value=job), \
                patch("app.tasks.celery_tasks.run_hub_import_job") as mock_task:
            resp = retry_hub_import_job("hubjob_r1", request=fake_request_with_cookie(), db=hub_job_db(job))
        assert resp.id == "hubjob_r1"
        assert resp.fail_detail == {"fid-2": "x"}
        mock_task.apply_async.assert_called_once_with(
            args=["hubjob_r1"], kwargs={"bearer_token": "tok-test"}, kwargsrepr="{<masked>}")

    def test_retry_completed_job_400(self):
        from fastapi import HTTPException

        from app.api.hub_import_api import retry_hub_import_job

        with patch("app.api.hub_import_api.retry_job",
                   side_effect=ValueError("导入任务已全部完成，无需重试")):
            with pytest.raises(HTTPException) as exc:
                retry_hub_import_job("hubjob_done", request=fake_request_with_cookie(), db=hub_job_db(SimpleNamespace(id="hubjob_done", dataset_id="ds-done")))
        assert exc.value.status_code == 400

    def test_retry_not_found_404(self):
        from fastapi import HTTPException

        from app.api.hub_import_api import retry_hub_import_job

        with patch("app.api.hub_import_api.retry_job", return_value=None):
            with pytest.raises(HTTPException) as exc:
                retry_hub_import_job("hubjob_gone", request=fake_request_with_cookie(), db=hub_job_db(None))
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 存量补登记：backfill_refs_for_hub_file_datasets（幂等物化 + 引用差值补齐）
# ---------------------------------------------------------------------------

class TestBackfillHubRefs:
    def _make_db(self, data_rows):
        """构造 db mock：字段元数据查询返回 session_file_id(hub_file)，
        数据行查询返回 data_rows，登记记录查询返回无记录"""
        from app.models.dataset_models import DatasetFieldMetadata, EvaluationDataV2
        from app.models.hub_file_models import HubFileRecord

        db = MagicMock()
        q_meta, q_data, q_rec = MagicMock(), MagicMock(), MagicMock()
        q_meta.filter.return_value.distinct.return_value.all.return_value = [("ds-1",)]
        q_meta.filter.return_value.all.return_value = [
            SimpleNamespace(field_name="session_file_id")]
        q_data.filter.return_value.all.return_value = data_rows
        q_rec.filter.return_value.first.return_value = None
        db.query.side_effect = lambda model, *a, **k: (
            q_data if model is EvaluationDataV2
            else (q_rec if model is HubFileRecord else q_meta))
        return db

    def test_backfill_materializes_and_refills_per_row(self):
        """两行引用同一 fid：ensure_unpacked 物化一次，ref_count 差值补 2"""
        from app.services.hub_file_service import backfill_refs_for_hub_file_datasets

        rows = [SimpleNamespace(data={"session_file_id": "fid-1"}),
                SimpleNamespace(data={"session_file_id": "fid-1"}),
                SimpleNamespace(data={"session_file_id": ""})]
        db = self._make_db(rows)
        with patch("app.services.hub_file_service.ensure_unpacked") as mock_ensure, \
                patch("app.services.hub_file_service.acquire_ref") as mock_acquire:
            mock_ensure.return_value = {"cached": False, "unpack_dir": "/x"}
            result = backfill_refs_for_hub_file_datasets(db)

        assert result["needed"] == {"fid-1": 2}
        assert mock_ensure.call_count == 1
        assert mock_ensure.call_args.args == (db, "fid-1")
        assert [c.args[1] for c in mock_acquire.call_args_list] == ["fid-1", "fid-1"]
        assert result["refilled"] == {"fid-1": 2}
        assert result["errors"] == {}

    def test_backfill_dry_run_writes_nothing(self):
        """dry-run 只统计 needed，不物化不补引用"""
        from app.services.hub_file_service import backfill_refs_for_hub_file_datasets

        rows = [SimpleNamespace(data={"session_file_id": "fid-1"})]
        db = self._make_db(rows)
        with patch("app.services.hub_file_service.ensure_unpacked") as mock_ensure, \
                patch("app.services.hub_file_service.acquire_ref") as mock_acquire:
            result = backfill_refs_for_hub_file_datasets(db, dry_run=True)
        assert result["needed"] == {"fid-1": 1}
        assert result["dry_run"] is True
        mock_ensure.assert_not_called()
        mock_acquire.assert_not_called()

    def test_backfill_skips_refs_already_sufficient(self):
        """ref_count 已达标（如曾登记过）：不重复补引用（幂等）"""
        from app.models.hub_file_models import HubFileRecord
        from app.services.hub_file_service import backfill_refs_for_hub_file_datasets

        rows = [SimpleNamespace(data={"session_file_id": "fid-1"})]
        db = self._make_db(rows)
        with patch("app.services.hub_file_service.ensure_unpacked") as mock_ensure, \
                patch("app.services.hub_file_service.acquire_ref") as mock_acquire:
            mock_ensure.return_value = {"cached": True, "unpack_dir": "/x"}
            # 直接补丁 _query_record 让登记命中已有 ref_count=1 的记录
            with patch("app.services.hub_file_service._query_record") as mock_query:
                mock_query.return_value = SimpleNamespace(ref_count=1)
                result = backfill_refs_for_hub_file_datasets(db)
        assert result["refilled"] == {}
        mock_acquire.assert_not_called()
        assert result["errors"] == {}

    def test_backfill_single_failure_does_not_block_rest(self):
        """单个 file_id 物化失败记入 errors，不中断其余补登记"""
        from app.services.hub_file_service import (
            HubFileError,
            backfill_refs_for_hub_file_datasets,
        )

        rows = [SimpleNamespace(data={"session_file_id": "fid-1"}),
                SimpleNamespace(data={"session_file_id": "fid-2"})]
        db = self._make_db(rows)

        def _ensure(_db, fid, **kw):
            if fid == "fid-1":
                raise HubFileError("下载失败")
            return {"cached": False, "unpack_dir": "/x"}

        with patch("app.services.hub_file_service.ensure_unpacked") as mock_ensure, \
                patch("app.services.hub_file_service.acquire_ref") as mock_acquire:
            mock_ensure.side_effect = _ensure
            result = backfill_refs_for_hub_file_datasets(db)
        assert "fid-1" in result["errors"]
        assert result["refilled"] == {"fid-2": 1}
        assert [c.args[1] for c in mock_acquire.call_args_list] == ["fid-2"]


class TestPreviewKind:
    def test_jsonl_is_previewable_text(self):
        """会话导入的原始 .jsonl 可在线预览（截断保护在 content 端点，2MB）"""
        from app.services.hub_file_service import preview_kind

        kind, mime = preview_kind("d5af326e-263b-4900-a0df-823bc6cec160.jsonl")
        assert kind == "text"
        assert mime == "application/x-ndjson"

    def test_json_unchanged(self):
        from app.services.hub_file_service import preview_kind

        assert preview_kind("a/b.json") == ("text", "application/json")

    def test_unknown_ext_is_binary(self):
        from app.services.hub_file_service import preview_kind

        assert preview_kind("model.bin")[0] == "binary"
        assert preview_kind("pkg.zip")[0] == "binary"
