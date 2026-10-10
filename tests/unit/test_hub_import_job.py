"""
anc-hub 执行包批量导入任务单元测试（数据集迭代阶段C）

覆盖验收清单 C1..C6 的任务/服务层分支：
- C1 HubImportJob 模型列与默认值
- C3 eager Celery 逐包处理：ensure_unpacked → 数据行（hub_file 字段 + hub_summary）
  → 字段元数据注册 → acquire_ref；每包完成 publish 到 task_status:{job_id}
- C4 失败包记入 fail_detail；retry 只重跑失败项且幂等（已成功的不重复导入）
- C5 删除数据集/数据行联动 release_ref；末引用清理解包目录与登记记录；
  同 file_id 二次导入复用解包区（ref_count+1，不重复下载）

mock 手法与 tests/unit/test_hub_file_service.py / test_hub_registry.py 同构：
patch requests 提供 MCP file_download 假实现（zip_post）；db 用 MagicMock 分流
（HubImportJob 查询按 id、登记记录查询 patch _query_record 按注册表命中）；
进度发布 patch redis.from_url。
"""
import base64
import hashlib
import json
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.hub_file_models import HubImportJob, HubFileRecord
from app.services.hub_import_job_service import (
    HUB_FILE_ID_FIELD,
    HUB_IMPORT_FIELD_METADATA,
    _normalize_file_ids,
    create_import_job,
    get_job,
    process_hub_import_job,
    retry_job,
)

ENDPOINT = "http://hub.test/mcp"


# ---------------------------------------------------------------------------
# mock 小工具（与 test_hub_registry.py 同手法）
# ---------------------------------------------------------------------------

class FakeResp:
    """模拟 requests.Response：content 是原始字节（UTF-8），与真实契约一致"""

    def __init__(self, text="", status_code=200, headers=None):
        if isinstance(text, bytes):
            self.content = text
        else:
            self.content = (text or "").encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}


def sse(obj) -> str:
    return 'data: \ndata: {"ping": 1}\n\ndata: ' + json.dumps(obj) + "\n\n"


def rpc_result(id_, result) -> str:
    return sse({"jsonrpc": "2.0", "id": id_, "result": result})


ATTEMPT_MANIFEST = {
    "execution_id": "exec-2026-001",
    "case_results": [
        {"case": "c1", "status": "passed"},
        {"case": "c2", "status": "failed"},
    ],
    "scope": ["seg-a", "seg-b"],
}


def make_attempt_zip_bytes(execution_id="exec-2026-001") -> bytes:
    """合成 attempt 执行包 zip（含 execution-manifest.json，可提取摘要）"""
    members = {
        "execution-manifest.json": json.dumps(
            {**ATTEMPT_MANIFEST, "execution_id": execution_id}, ensure_ascii=False),
        "case_list.json": json.dumps([{"case": "c1"}, {"case": "c2"}]),
        "logs/run.log": "line1\n",
    }
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content.encode("utf-8"))
    return buf.getvalue()


def make_plain_zip_bytes() -> bytes:
    """无 manifest 的普通 zip（摘要为 None → hub_summary 落空 dict）"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "普通文件包")
    return buf.getvalue()


def zip_post(zip_bytes: bytes, filename: str):
    """MCP file_download 假实现：按 offset 切片返回 zip 字节（按 file_id 差异化 payload）"""
    full_sha = hashlib.sha256(zip_bytes).hexdigest()

    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        method = msg.get("method")
        if method == "initialize":
            return FakeResp(rpc_result(msg.get("id"), {"ok": True}),
                            headers={"mcp-session-id": "sess-job-1"})
        if method == "notifications/initialized":
            return FakeResp("")
        if method == "tools/call":
            args = msg["params"]["arguments"]
            offset = args.get("offset", 0)
            chunk = zip_bytes[offset:offset + args.get("max_bytes", 1 << 20)]
            pl = {
                "ok": True,
                "data_base64": base64.b64encode(chunk).decode(),
                "bytes_read": len(chunk),
                "eof": offset + len(chunk) >= len(zip_bytes),
                "sha256": full_sha,
                "size": len(zip_bytes),
                "filename": filename,
            }
            return FakeResp(rpc_result(
                msg["id"], {"structuredContent": {"ok": True, "payload": pl}}))
        raise AssertionError(f"未预期的 MCP 方法: {method}")

    return _post


def mcp_router(payloads: dict, default: bytes = None):
    """按 file_id 路由 MCP file_download 假实现（payloads: {file_id: zip 字节}）"""
    fallback = default if default is not None else make_attempt_zip_bytes()

    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        method = msg.get("method")
        if method == "initialize":
            return FakeResp(rpc_result(msg.get("id"), {"ok": True}),
                            headers={"mcp-session-id": "sess-job-1"})
        if method == "notifications/initialized":
            return FakeResp("")
        if method == "tools/call":
            fid = msg["params"]["arguments"]["file_id"]
            payload = payloads.get(fid, fallback)
            return zip_post(payload, f"{fid}.zip")(url, data, headers, timeout)
        raise AssertionError(f"未预期的 MCP 方法: {method}")

    return _post


class FakeJobDb:
    """导入任务用 db 替身：

    - query(HubImportJob) 返回 job（get_job 查询）；其余模型 all() 为空
    - add 捕获对象并把 HubFileRecord 实时注册到 registry（供 _query_record 命中）
    - commit / rollback / delete 记调用
    """

    def __init__(self, job):
        self.job = job
        self.registry = {}   # file_id -> HubFileRecord
        self.added = []
        self.commits = 0
        self.rolls = 0
        self.deleted = []
        self._mock = MagicMock()

    def __getattr__(self, name):
        return getattr(self._mock, name)

    def close(self):
        pass

    def query(self, model):
        q = MagicMock()
        if model is HubImportJob:
            q.filter.return_value.first.return_value = self.job
        else:
            q.filter.return_value.all.return_value = []
        return q

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, HubFileRecord):
            self.registry[obj.file_id] = obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rolls += 1

    def delete(self, obj):
        self.deleted.append(obj)


def make_job(file_ids, dataset_id="ds-job", status="pending", id="hubjob_test0001", **kw):
    return HubImportJob(
        id=id,
        dataset_id=dataset_id,
        file_ids=list(file_ids),
        status=status,
        total_count=kw.pop("total_count", len(file_ids)),
        success_count=kw.pop("success_count", 0),
        fail_count=kw.pop("fail_count", 0),
        fail_detail=kw.pop("fail_detail", {}),
        succeeded_file_ids=kw.pop("succeeded_file_ids", []),
        created_by="tester",
    )


def make_svc(tmp_path):
    """缓存根指到临时目录的 HubFileService（避免下载缓存落进仓库 uploads/）"""
    from app.services.hub_file_service import HubFileService

    return HubFileService(endpoint=ENDPOINT, cache_root=str(tmp_path / "dl"))


def flaky_mcp_router(payloads: dict, fail_first: dict = None, default: bytes = None):
    """按 file_id 路由 MCP file_download；fail_first={file_id: 次数} 模拟下载期瞬时失败

    下载期抛 HubFileError 不落缓存（download_to_file 异常清理半成品），
    重试后同一 file_id 可返回正常 zip —— 与「坏包修复后重跑」的真实语义一致。
    """
    from app.services.hub_file_service import HubFileError

    fallback = default if default is not None else make_attempt_zip_bytes()
    state = dict(fail_first or {})

    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        method = msg.get("method")
        if method == "initialize":
            return FakeResp(rpc_result(msg.get("id"), {"ok": True}),
                            headers={"mcp-session-id": "sess-job-1"})
        if method == "notifications/initialized":
            return FakeResp("")
        if method == "tools/call":
            fid = msg["params"]["arguments"]["file_id"]
            if state.get(fid, 0) > 0:
                state[fid] -= 1
                raise HubFileError(f"文件 {fid} 下载失败: 服务器返回空响应（已重试一次）")
            payload = payloads.get(fid, fallback)
            return zip_post(payload, f"{fid}.zip")(url, data, headers, timeout)
        raise AssertionError(f"未预期的 MCP 方法: {method}")

    return _post


@pytest.fixture
def record_registry():
    """patch hub_file_service._query_record：按 registry dict 命中登记记录"""
    registry = {}

    def _lookup(db, file_id):
        # FakeJobDb.add 已把新增登记记录实时注册（模拟真实 ensure_unpacked 落库后可查）
        if isinstance(db, FakeJobDb) and file_id in db.registry:
            return db.registry[file_id]
        return registry.get(file_id)

    with patch("app.services.hub_file_service._query_record", side_effect=_lookup):
        yield registry


@pytest.fixture
def service_mocks():
    """patch 数据行写入（DatasetDataService）与字段元数据注册（DatasetFieldService）"""
    rows = []
    meta = []
    with patch("app.services.dataset_service.DatasetDataService") as mock_ds_cls, \
            patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs_cls:
        mock_ds_cls.return_value.add_data_v2.side_effect = \
            lambda dataset_id, data: rows.append((dataset_id, dict(data)))
        mock_fs_cls.return_value.get_field_metadata.return_value = []
        mock_fs_cls.return_value.save_field_metadata.side_effect = \
            lambda dataset_id, fields: meta.append((dataset_id, [dict(f) for f in fields]))
        yield SimpleNamespace(rows=rows, meta=meta)


@pytest.fixture
def redis_publish():
    """patch redis.from_url，捕获 publish 调用"""
    r = MagicMock()
    with patch("redis.from_url", return_value=r):
        yield r


# ---------------------------------------------------------------------------
# C1: 模型
# ---------------------------------------------------------------------------

class TestHubImportJobModel:
    EXPECTED_COLUMNS = {
        "id", "dataset_id", "file_ids", "status",
        "total_count", "success_count", "fail_count", "fail_detail",
        "succeeded_file_ids", "created_by", "created_at", "updated_at",
    }

    def test_table_name_and_columns(self):
        """表名 hub_import_jobs，列覆盖验收清单 C1 全部字段（+幂等键/审计列）"""
        assert HubImportJob.__tablename__ == "hub_import_jobs"
        cols = {c.name for c in HubImportJob.__table__.columns}
        assert cols == self.EXPECTED_COLUMNS

    def test_status_defaults_pending(self):
        col = HubImportJob.__table__.columns.status
        assert col.server_default is not None and col.server_default.arg == "pending"
        assert not col.nullable

    def test_counts_default_zero(self):
        for name in ("total_count", "success_count", "fail_count"):
            col = HubImportJob.__table__.columns[name]
            assert col.server_default is not None and col.server_default.arg == "0"

    def test_exported_from_models_init(self):
        import app.models as models_pkg
        assert models_pkg.HubImportJob is HubImportJob
        assert "HubImportJob" in models_pkg.__all__


# ---------------------------------------------------------------------------
# C2 服务侧：create_import_job 参数校验与数据集解析
# ---------------------------------------------------------------------------

class TestCreateImportJob:
    def _db(self, added):
        db = MagicMock()
        db.add.side_effect = lambda obj: added.append(obj)
        return db

    def test_normalize_file_ids_dedupes(self):
        assert _normalize_file_ids([" a ", "a", "", None, "b"]) == ["a", "b"]

    def test_empty_file_ids_raises(self):
        with pytest.raises(ValueError, match="file_id"):
            create_import_job(self._db([]), file_ids=[" ", ""])

    @patch("app.services.dataset_service.DatasetService")
    def test_new_dataset_mode(self, mock_ds_cls):
        """新建模式：create_dataset 传参对齐，job 落 pending 与总数"""
        mock_ds_cls.return_value.create_dataset.return_value = SimpleNamespace(id="ds-new")
        added = []
        job = create_import_job(
            self._db(added), file_ids=["fid-1", "fid-1", "fid-2"],
            name="执行包集", description="d", created_by="u1")
        kwargs = mock_ds_cls.return_value.create_dataset.call_args.kwargs
        assert kwargs["name"] == "执行包集"
        assert kwargs["created_by"] == "u1"
        assert job.dataset_id == "ds-new"
        assert job.file_ids == ["fid-1", "fid-2"]
        assert job.status == "pending"
        assert job.total_count == 2
        assert job.succeeded_file_ids == [] and job.fail_detail == {}
        assert added == [job]

    @patch("app.services.dataset_service.DatasetService")
    def test_new_dataset_without_name_raises(self, mock_ds_cls):
        with pytest.raises(ValueError, match="名称不能为空"):
            create_import_job(self._db([]), file_ids=["fid-1"], name="  ")

    @patch("app.services.dataset_service.DatasetService")
    def test_append_mode_requires_existing_dataset(self, mock_ds_cls):
        mock_ds_cls.return_value.get_dataset.return_value = None
        with pytest.raises(ValueError, match="目标数据集不存在"):
            create_import_job(self._db([]), file_ids=["fid-1"], real_dataset_id="ds-gone")

    @patch("app.services.dataset_service.DatasetService")
    def test_append_mode_reuses_dataset(self, mock_ds_cls):
        mock_ds_cls.return_value.get_dataset.return_value = SimpleNamespace(id="ds-real")
        job = create_import_job(self._db([]), file_ids=["fid-1"], real_dataset_id="ds-real")
        assert job.dataset_id == "ds-real"
        mock_ds_cls.return_value.create_dataset.assert_not_called()


# ---------------------------------------------------------------------------
# C3: 逐包处理（成功 / 数据行结构 / 字段元数据 / 引用计数 / 进度发布）
# ---------------------------------------------------------------------------

class TestProcessHubImportJob:
    @pytest.mark.usefixtures("record_registry", "redis_publish")
    def test_all_success_flow(self, monkeypatch, tmp_path, service_mocks):
        """两包全成功：数据行 data={hub_file_id: file_id, hub_summary}、
        字段元数据注册、ref_count=1、状态 completed"""
        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        job = make_job(["fid-1", "fid-2"])
        db = FakeJobDb(job)
        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = mcp_router(
                {"fid-1": make_attempt_zip_bytes("exec-1"),
                 "fid-2": make_plain_zip_bytes()})
            mock_requests.delete.return_value = FakeResp("")
            result = process_hub_import_job(
                db, job.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))

        assert result["status"] == "completed"
        assert result["success_count"] == 2 and result["fail_count"] == 0
        # 数据行结构：hub_file 字段值 = file_id，顶层 hub_summary（无 manifest 为空 dict）
        assert len(service_mocks.rows) == 2
        ds_id, d1 = service_mocks.rows[0]
        assert ds_id == "ds-job"
        assert d1[HUB_FILE_ID_FIELD] == "fid-1"
        assert d1["hub_summary"]["execution_id"] == "exec-1"
        assert d1["hub_summary"]["schema"] == "anc_attempt_v1"
        _, d2 = service_mocks.rows[1]
        assert d2[HUB_FILE_ID_FIELD] == "fid-2"
        assert d2["hub_summary"] == {}
        # 引用计数各 1
        assert db.registry["fid-1"].ref_count == 1
        assert db.registry["fid-2"].ref_count == 1
        assert job.succeeded_file_ids == ["fid-1", "fid-2"]

    @pytest.mark.usefixtures("record_registry", "redis_publish")
    def test_metadata_registered_with_hub_file_type(self, monkeypatch, tmp_path, service_mocks):
        """字段元数据注册：hub_file_id 类型 hub_file、hub_summary 类型 json（复用既有保存函数）"""
        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        job = make_job(["fid-m"])
        db = FakeJobDb(job)
        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = zip_post(make_attempt_zip_bytes(), "m.zip")
            mock_requests.delete.return_value = FakeResp("")
            process_hub_import_job(
                db, job.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))

        assert service_mocks.meta, "字段元数据应已注册"
        dataset_id, fields = service_mocks.meta[-1]
        assert dataset_id == "ds-job"
        by_name = {f["name"]: f for f in fields}
        assert by_name[HUB_FILE_ID_FIELD]["type"] == "hub_file"
        assert by_name["hub_summary"]["type"] == "json"
        # 注册内容与常量表一致
        assert {m["name"] for m in HUB_IMPORT_FIELD_METADATA} <= set(by_name)

    @pytest.mark.usefixtures("record_registry", "service_mocks")
    def test_publishes_progress_per_package(self, monkeypatch, tmp_path, redis_publish):
        """每包完成后 publish 到 task_status:{job_id}（running + 每包 + 终态）"""
        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        job = make_job(["fid-p1", "fid-p2"])
        db = FakeJobDb(job)
        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = zip_post(make_attempt_zip_bytes(), "p.zip")
            mock_requests.delete.return_value = FakeResp("")
            process_hub_import_job(
                db, job.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))

        calls = redis_publish.publish.call_args_list
        channels = [c.args[0] for c in calls]
        assert channels and all(c == f"task_status:{job.id}" for c in channels)
        # running 1 次 + 每包 1 次 + 终态 1 次 = 4
        assert len(channels) == 4
        first_msg = json.loads(calls[0].args[1])
        assert first_msg["status"] == "running" and first_msg["progress"] == 0
        mid_msg = json.loads(calls[1].args[1])
        assert mid_msg["success_count"] == 1 and mid_msg["progress"] == 50
        final_msg = json.loads(calls[-1].args[1])
        assert final_msg["status"] == "completed"
        assert final_msg["success_count"] == 2 and final_msg["total"] == 2

    @pytest.mark.usefixtures("record_registry", "service_mocks")
    def test_publish_failure_does_not_block(self, monkeypatch, tmp_path):
        """Redis 不可用只记日志，导入主流程不受影响"""
        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        job = make_job(["fid-nr"])
        db = FakeJobDb(job)
        with patch("app.services.hub_file_service.requests") as mock_requests, \
                patch("redis.from_url", side_effect=Exception("redis 不可用")):
            mock_requests.post.side_effect = zip_post(make_attempt_zip_bytes(), "nr.zip")
            mock_requests.delete.return_value = FakeResp("")
            result = process_hub_import_job(
                db, job.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))
        assert result["status"] == "completed"

    def test_job_not_found_raises(self):
        db = FakeJobDb(None)
        with pytest.raises(ValueError, match="导入任务不存在"):
            process_hub_import_job(db, "hubjob_gone")


# ---------------------------------------------------------------------------
# C4: 失败包记入 fail_detail；retry 只重跑失败项且幂等
# ---------------------------------------------------------------------------

class TestFailDetailAndRetry:
    @pytest.mark.usefixtures("record_registry", "service_mocks", "redis_publish")
    def test_failed_package_recorded_and_others_continue(self, monkeypatch, tmp_path):
        """坏 zip 包失败记入 fail_detail（中文错误），其余包继续导入，终态 failed"""
        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        job = make_job(["fid-ok", "fid-bad"])
        db = FakeJobDb(job)
        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = mcp_router(
                {"fid-ok": make_attempt_zip_bytes(),
                 "fid-bad": b"garbage-not-a-zip"})
            mock_requests.delete.return_value = FakeResp("")
            result = process_hub_import_job(
                db, job.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))

        assert result["status"] == "failed"
        assert result["success_count"] == 1 and result["fail_count"] == 1
        assert "不是有效的 zip 文件" in result["fail_detail"]["fid-bad"]
        assert job.succeeded_file_ids == ["fid-ok"]
        # 失败包未持有引用（登记状态 failed，ref_count 未加）
        assert db.registry["fid-bad"].unpack_status == "failed"
        assert int(db.registry["fid-bad"].ref_count or 0) == 0
        assert db.registry["fid-ok"].ref_count == 1
        # 失败包发生了 rollback（清掉半成品会话状态再记失败明细）
        assert db.rolls >= 1

    @pytest.mark.usefixtures("record_registry", "service_mocks", "redis_publish")
    def test_retry_only_failed_and_idempotent(self, monkeypatch, tmp_path, service_mocks):
        """retry 只重跑失败项：已成功的 fid-ok 不再写数据行/引用不变，坏包修复后转 completed"""
        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        job = make_job(["fid-ok", "fid-bad"])
        db = FakeJobDb(job)
        payloads = {"fid-ok": make_attempt_zip_bytes(), "fid-bad": make_attempt_zip_bytes("exec-fixed")}
        # fid-bad 首次下载失败（瞬时故障，不落缓存），重试后返回正常 zip
        router = flaky_mcp_router(payloads, fail_first={"fid-bad": 1})

        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = router
            mock_requests.delete.return_value = FakeResp("")
            first = process_hub_import_job(
                db, job.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))
        assert first["status"] == "failed"
        assert "服务器返回空响应" in first["fail_detail"]["fid-bad"]

        # retry 重置（服务层）+ 瞬时故障恢复后重跑
        retry_job(db, job.id)
        assert job.status == "pending"
        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = router
            mock_requests.delete.return_value = FakeResp("")
            second = process_hub_import_job(
                db, job.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))

        assert second["status"] == "completed"
        assert second["fail_detail"] == {}
        assert set(job.succeeded_file_ids) == {"fid-ok", "fid-bad"}
        # 幂等：fid-ok 只有一条数据行，fid-bad 重试成功补一条
        assert [r[1][HUB_FILE_ID_FIELD] for r in service_mocks.rows] == ["fid-ok", "fid-bad"]
        assert db.registry["fid-ok"].ref_count == 1
        assert db.registry["fid-bad"].ref_count == 1

    def test_retry_state_guards(self):
        job_running = make_job(["fid-1"], status="running")
        with pytest.raises(ValueError, match="进行中"):
            retry_job(FakeJobDb(job_running), job_running.id)

        job_pending = make_job(["fid-1"], status="pending")
        with pytest.raises(ValueError, match="排队中"):
            retry_job(FakeJobDb(job_pending), job_pending.id)

        job_done = make_job(["fid-1"], status="completed")
        with pytest.raises(ValueError, match="无需重试"):
            retry_job(FakeJobDb(job_done), job_done.id)

        job_failed_no_detail = make_job(["fid-1"], status="failed", fail_detail={})
        with pytest.raises(ValueError, match="没有可重试"):
            retry_job(FakeJobDb(job_failed_no_detail), job_failed_no_detail.id)

        assert retry_job(FakeJobDb(None), "hubjob_gone") is None


# ---------------------------------------------------------------------------
# C5: 同 file_id 二次导入复用解包区；数据行写失败补偿释放
# ---------------------------------------------------------------------------

class TestReuseAndCompensation:
    @pytest.mark.usefixtures("record_registry", "service_mocks", "redis_publish")
    def test_second_import_reuses_unpack_region(self, monkeypatch, tmp_path, service_mocks):
        """同 file_id 二次导入：登记表命中零 MCP 请求、ref_count+1 不重复下载"""
        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        job1 = make_job(["fid-re"], id="hubjob_run1")
        job2 = make_job(["fid-re"], id="hubjob_run2", dataset_id="ds-job2")
        db1 = FakeJobDb(job1)
        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = zip_post(make_attempt_zip_bytes(), "re.zip")
            mock_requests.delete.return_value = FakeResp("")
            process_hub_import_job(
                db1, job1.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))
        rec = db1.registry["fid-re"]
        assert rec.ref_count == 1
        unpack_dir = rec.unpack_dir

        # 第二次导入（另一数据集）：登记表命中 → 任何 MCP 请求都视为违规
        db2 = FakeJobDb(job2)
        db2.registry["fid-re"] = rec
        with patch("app.services.hub_file_service.requests") as mock_requests:
            mock_requests.post.side_effect = AssertionError("登记表命中时不应发起 MCP 请求")
            process_hub_import_job(db2, job2.id, datasets_root=str(tmp_path))

        assert rec.ref_count == 2, "二次导入应复用解包区并 ref_count+1"
        assert rec.unpack_dir == unpack_dir
        assert len(service_mocks.rows) == 2
        assert all(r[1][HUB_FILE_ID_FIELD] == "fid-re" for r in service_mocks.rows)
        assert service_mocks.rows[1][0] == "ds-job2"

    @pytest.mark.usefixtures("record_registry", "redis_publish")
    def test_row_add_failure_compensates_ref(self, monkeypatch, tmp_path):
        """数据行写失败：补偿释放本次引用（末引用归零清理登记记录），失败记入 fail_detail"""
        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        job = make_job(["fid-cp"])
        db = FakeJobDb(job)
        with patch("app.services.hub_file_service.requests") as mock_requests, \
                patch("app.services.dataset_service.DatasetDataService") as mock_ds_cls:
            mock_requests.post.side_effect = zip_post(make_attempt_zip_bytes(), "cp.zip")
            mock_requests.delete.return_value = FakeResp("")
            mock_ds_cls.return_value.add_data_v2.side_effect = RuntimeError("数据库写入失败")
            result = process_hub_import_job(
                db, job.id, datasets_root=str(tmp_path), hub_service=make_svc(tmp_path))

        assert result["status"] == "failed"
        assert "数据库写入失败" in result["fail_detail"]["fid-cp"]
        # 补偿释放：末引用归零 → 登记记录被删除
        assert any(isinstance(o, HubFileRecord) and o.file_id == "fid-cp" for o in db.deleted)


# ---------------------------------------------------------------------------
# C5: 删除联动 release_ref（collect / 批量释放 / 两个删除入口）
# ---------------------------------------------------------------------------

class TestReleaseHelpers:
    def test_collect_row_hub_file_ids(self):
        from app.services.hub_file_service import collect_row_hub_file_ids
        data = {"hub_file_id": " f1 ", "other_hub": "f1", "empty": "", "num": 123, "none": None}
        assert collect_row_hub_file_ids(data, ["hub_file_id", "other_hub", "empty", "num"]) == ["f1"]
        assert collect_row_hub_file_ids(None, ["hub_file_id"]) == []
        assert collect_row_hub_file_ids({"hub_file_id": "f2"}, []) == []

    @pytest.mark.usefixtures("record_registry")
    def test_release_hub_refs_last_ref_cleans_dir_and_record(self, tmp_path):
        """末引用（ref_count→0）清理解包目录与登记记录；未登记项跳过不抛"""
        from app.services.hub_file_service import release_hub_refs

        unpack_dir = tmp_path / "fid-last"
        unpack_dir.mkdir()
        (unpack_dir / "case_list.json").write_text("[]")
        rec = HubFileRecord(id="hub_r1", file_id="fid-last", unpack_status="unpacked",
                            unpack_dir=str(unpack_dir), ref_count=1)
        db = FakeJobDb(None)
        db.registry["fid-last"] = rec

        release_hub_refs(db, ["fid-gone", "fid-last"])

        assert not unpack_dir.exists(), "末引用应清理解包目录"
        assert rec in db.deleted, "末引用应删除登记记录"
        assert db.commits >= 1

    @pytest.mark.usefixtures("record_registry")
    def test_release_hub_refs_decrements_when_multiple_refs(self):
        from app.services.hub_file_service import release_hub_refs

        rec = HubFileRecord(id="hub_r2", file_id="fid-multi", ref_count=2)
        db = FakeJobDb(None)
        db.registry["fid-multi"] = rec
        release_hub_refs(db, ["fid-multi"])
        assert rec.ref_count == 1
        assert db.deleted == []


class TestDeleteLinkage:
    """删除数据集/数据行联动 release_ref（dataset_service 挂钩）"""

    def _db_for_delete_data(self, data_obj):
        from app.models.dataset_models import DatasetFieldMetadata, EvaluationDataV2

        db = MagicMock()

        def query(model):
            q = MagicMock()
            if model is EvaluationDataV2:
                q.filter.return_value.first.return_value = data_obj
            elif model is DatasetFieldMetadata:
                q.filter.return_value.all.return_value = [
                    SimpleNamespace(field_name="hub_file_id", field_type="hub_file")]
            return q

        db.query.side_effect = query
        return db

    def test_delete_data_releases_refs(self):
        from app.services.dataset_service import DatasetDataService

        data_obj = SimpleNamespace(
            id="data2_x", dataset_id="ds-1", is_active=True,
            data={"user_input": "q", "hub_file_id": "fid-x"})
        db = self._db_for_delete_data(data_obj)
        with patch("app.services.oss_media_cache_service.OSSMediaCacheService"), \
                patch("app.services.hub_file_service.release_hub_refs") as mock_release:
            assert DatasetDataService(db).delete_data("data2_x") is True
        mock_release.assert_called_once_with(db, ["fid-x"])
        assert data_obj.is_active is False

    def test_delete_data_without_hub_fields_skips_release(self):
        from app.services.dataset_service import DatasetDataService

        data_obj = SimpleNamespace(
            id="data2_y", dataset_id="ds-1", is_active=True, data={"user_input": "q"})
        db = self._db_for_delete_data(data_obj)
        with patch("app.services.oss_media_cache_service.OSSMediaCacheService"), \
                patch("app.services.hub_file_service.release_hub_refs") as mock_release:
            assert DatasetDataService(db).delete_data("data2_y") is True
        mock_release.assert_not_called()

    def test_delete_dataset_releases_all_row_refs_with_multiplicity(self):
        """数据集删除：按活跃数据行逐行收集（跨行保留多重性），软删后统一释放"""
        from app.models.dataset_models import DatasetFieldMetadata, EvaluationDataset, EvaluationDataV2
        from app.models.task_models import EvaluationTask
        from app.services.dataset_service import DatasetService

        dataset = SimpleNamespace(id="ds-1", is_active=True)
        rows = [
            SimpleNamespace(data={"hub_file_id": "fid-a"}),
            SimpleNamespace(data={"hub_file_id": "fid-a"}),
            SimpleNamespace(data={"hub_file_id": "fid-b"}),
            SimpleNamespace(data={"user_input": "无引用"}),
        ]

        db = MagicMock()

        def query(model):
            q = MagicMock()
            if model is EvaluationDataset:
                q.filter.return_value.first.return_value = dataset
            elif model is EvaluationTask:
                q.filter.return_value.all.return_value = []
            elif model is EvaluationDataV2:
                q.filter.return_value.all.return_value = rows
            elif model is DatasetFieldMetadata:
                q.filter.return_value.all.return_value = [
                    SimpleNamespace(field_name="hub_file_id", field_type="hub_file")]
            return q

        db.query.side_effect = query

        with patch("app.services.oss_media_cache_service.OSSMediaCacheService"), \
                patch("app.services.hub_file_service.release_hub_refs") as mock_release:
            assert DatasetService(db).delete_dataset("ds-1") is True
        # fid-a 被两行引用 → 释放两次；fid-b 一次；无引用行不贡献
        mock_release.assert_called_once_with(db, ["fid-a", "fid-a", "fid-b"])
        assert dataset.is_active is False

    def test_cleanup_duplicates_releases_hub_refs(self):
        """清理重复数据集入口（绕过 delete_dataset 的软删路径）同样联动释放执行包引用"""
        from app.api.dataset_api import cleanup_duplicates
        from app.models.dataset_models import DatasetFieldMetadata, EvaluationDataset, EvaluationDataV2

        earliest = SimpleNamespace(id="ds-keep", name="dup", is_active=True)
        dup = SimpleNamespace(id="ds-dup", name="dup", is_active=True)
        db = MagicMock()
        class_calls = {"n": 0}

        def query(arg):
            q = MagicMock()
            if arg is EvaluationDataset:
                class_calls["n"] += 1
                if class_calls["n"] == 1:
                    # 重复名称下最早创建的一条（保留）
                    q.filter.return_value.order_by.return_value.first.return_value = earliest
                else:
                    # 其余重复项（待清理）
                    q.filter.return_value.all.return_value = [dup]
            elif arg is DatasetFieldMetadata:
                q.filter.return_value.all.return_value = [
                    SimpleNamespace(field_name="hub_file_id", field_type="hub_file")]
            elif arg is EvaluationDataV2:
                q.filter.return_value.all.return_value = [
                    SimpleNamespace(data={"hub_file_id": "fid-dup"})]
            else:
                # 名称列查询（arg 为 EvaluationDataset.name 列对象）
                q.filter.return_value.group_by.return_value.having.return_value.all.return_value = [("dup",)]
            return q

        db.query.side_effect = query

        with patch("app.services.hub_file_service.release_hub_refs") as mock_release:
            result = cleanup_duplicates(db=db)
        assert result["deleted_count"] == 1
        assert dup.is_active is False and earliest.is_active is True
        mock_release.assert_called_once_with(db, ["fid-dup"])


# ---------------------------------------------------------------------------
# C6: eager Celery 端到端（任务包装 + 进度查询服务）
# ---------------------------------------------------------------------------

class TestEagerCeleryTask:
    def test_run_hub_import_job_via_delay(self, monkeypatch, tmp_path):
        """eager 模式 .delay 同步执行：get_db_session 注入替身，全流程落 completed"""
        from app.celery_app import celery_app
        from app.tasks.celery_tasks import run_hub_import_job

        monkeypatch.setenv("ANC_HUB_MCP_ENDPOINT", ENDPOINT)
        monkeypatch.chdir(tmp_path)  # 下载缓存/缺省解包根全部落临时目录
        job = make_job(["fid-ek"], id="hubjob_eager01")
        db = FakeJobDb(job)

        old_eager = celery_app.conf.task_always_eager
        celery_app.conf.task_always_eager = True
        try:
            with patch("app.tasks.evaluation_tasks_async.get_db_session", return_value=db), \
                    patch("app.services.hub_file_service.requests") as mock_requests, \
                    patch("app.services.dataset_service.DatasetDataService") as mock_ds_cls, \
                    patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs_cls, \
                    patch("redis.from_url") as mock_redis:
                mock_requests.post.side_effect = zip_post(make_attempt_zip_bytes(), "ek.zip")
                mock_requests.delete.return_value = FakeResp("")
                rows = []
                mock_ds_cls.return_value.add_data_v2.side_effect = \
                    lambda dataset_id, data: rows.append((dataset_id, dict(data)))
                mock_fs_cls.return_value.get_field_metadata.return_value = []
                res = run_hub_import_job.delay(job.id, datasets_root=str(tmp_path))
        finally:
            celery_app.conf.task_always_eager = old_eager

        assert res.result["status"] == "completed"
        assert res.result["job_id"] == job.id
        assert rows and rows[0][1][HUB_FILE_ID_FIELD] == "fid-ek"
        assert job.status == "completed"
        # 进度已发布到 task_status:{job_id} 频道（与评测进度同前缀）
        channels = [c.args[0] for c in mock_redis.return_value.publish.call_args_list]
        assert f"task_status:{job.id}" in channels

    def test_get_job_service(self):
        assert get_job(FakeJobDb(make_job(["fid-1"])), "hubjob_test0001").dataset_id == "ds-job"
        assert get_job(FakeJobDb(None), "hubjob_none") is None
