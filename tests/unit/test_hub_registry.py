"""
anc-hub 执行包登记表单元测试（阶段A：验收 A1 / A3 / A4 / A5 / A7）

- A1 HubFileRecord 模型：表名/列集合/file_id 唯一索引/ref_count 默认0、models/__init__ 导出
- A3 ensure_unpacked 幂等：未命中 下载→安全解包→索引→摘要→写登记表；unpacked 命中零下载
- A4 build_file_index：type 分类、is_nested_zip、嵌套 zip 读 central directory 生成 nested_index（不解压）
- A5 extract_hub_summary：合成 attempt fixture 产出 anc_attempt_v1 摘要；无 manifest 返回 None
- A7 acquire_ref/release_ref：引用计数±1；归零清理解包目录与登记记录

DB 用 MagicMock/SimpleNamespace（不连真实库）；MCP 用 patch requests 手法
（参考 tests/unit/test_hub_file_service.py 的 FakeResp/sse/rpc_result）。
attempt fixture 在测试内用 zipfile 现场合成小样本，不依赖网络。
"""
import base64
import hashlib
import json
import zipfile
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from app.models import HubFileRecord
from app.services import hub_file_service
from app.services.hub_file_service import (
    HubFileError,
    HubFileService,
    acquire_ref,
    build_file_index,
    ensure_unpacked,
    extract_hub_summary,
    release_ref,
    unpack_zip_safely,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

SESSION_ID = "sess-registry-1"
ENDPOINT = "http://hub.test/mcp"


# ---------------------------------------------------------------------------
# mock 工具（与 tests/unit/test_hub_file_service.py 同手法）
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


def zip_post(zip_bytes: bytes, filename: str, calls=None):
    """MCP file_download 假实现：按 offset 切片返回 zip 字节，整包 sha/size/filename 恒定"""
    full_sha = hashlib.sha256(zip_bytes).hexdigest()

    def _post(url, data=None, headers=None, timeout=None, **kwargs):
        msg = json.loads(data)
        method = msg.get("method")
        if method == "initialize":
            return FakeResp(rpc_result(msg.get("id"), {"ok": True}),
                            headers={"mcp-session-id": SESSION_ID})
        if method == "notifications/initialized":
            return FakeResp("")
        if method == "tools/call":
            args = msg["params"]["arguments"]
            offset = args.get("offset", 0)
            chunk = zip_bytes[offset:offset + args.get("max_bytes", 1 << 20)]
            eof = offset + len(chunk) >= len(zip_bytes)
            if calls is not None:
                calls.append(dict(args))
            pl = {
                "ok": True,
                "data_base64": base64.b64encode(chunk).decode(),
                "bytes_read": len(chunk),
                "eof": eof,
                "sha256": full_sha,
                "size": len(zip_bytes),
                "filename": filename,
            }
            return FakeResp(rpc_result(
                msg["id"], {"structuredContent": {"ok": True, "payload": pl}}))
        raise AssertionError(f"未预期的 MCP 方法: {method}")

    return _post


# ---------------------------------------------------------------------------
# attempt 小样本 fixture（现场合成，不依赖网络）
# ---------------------------------------------------------------------------

ATTEMPT_MANIFEST = {
    "execution_id": "exec-2026-001",
    "case_results": [
        {"case_id": "c1", "status": "passed"},
        {"case_id": "c2", "status": "passed"},
        {"case_id": "c3", "status": "failed"},
    ],
    "scope": [{"segment": "S1"}, {"segment": "S2"}, {"segment": "S3"}],
}
ATTEMPT_ACCEPTANCE = {"cases": [{"case_id": "c1"}, {"case_id": "c2"}, {"case_id": "c3"}]}
ATTEMPT_CASE_LIST = [{"case_id": "c1"}, {"case_id": "c2"}, {"case_id": "c3"}]


def make_attempt_zip_bytes() -> bytes:
    """合成 attempt 执行包 zip：manifest / acceptance-map / case_list / 日志与附件"""
    members = {
        "execution-manifest.json": json.dumps(ATTEMPT_MANIFEST, ensure_ascii=False),
        "acceptance-map.json": json.dumps(ATTEMPT_ACCEPTANCE, ensure_ascii=False),
        "case_list.json": json.dumps(ATTEMPT_CASE_LIST, ensure_ascii=False),
        "logs/run.log": "line1\nline2\n",
        "allure/results.json": "[]",
    }
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content.encode("utf-8"))
    return buf.getvalue()


ATTEMPT_FILE_COUNT = 5  # 上述 5 个成员文件


def make_plain_zip_bytes() -> bytes:
    """无 manifest 的普通 zip"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "普通文件包")
        zf.writestr("data/a.csv", "a,b\n1,2\n")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# DB mock：query 链可切换 first 返回值，add 捕获真实 HubFileRecord 实例
# ---------------------------------------------------------------------------

def make_db(first=None, added=None):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = first
    if added is not None:
        db.add.side_effect = lambda obj: added.append(obj)
    _install_w11_ref_stmts(db, first)
    return db


def _install_w11_ref_stmts(db, record):
    """W11 原子 ref 语句模拟：Update ±1（CASE 下限 0）/ 归零条件 Delete / Select 回读

    acquire_ref/release_ref 已改为 SQL 端单语句原子更新（不再读写 ORM 对象），
    替身按编译语句作用于 record，使计数语义在单元层可见；真实 SQL 并发正确性
    由 tests/integration/test_hub_import_concurrency.py 覆盖。归零条件删除以
    db.hub_zero_deleted 标记（替代旧 db.delete(record) 断言）。
    """
    from types import SimpleNamespace

    from sqlalchemy import Delete, Select, Update

    def _execute(stmt, *args, **kwargs):
        if isinstance(stmt, Update) and getattr(stmt.table, "name", None) == "hub_files":
            if record is None:
                return SimpleNamespace(rowcount=0)
            sql = str(stmt.compile())
            count = int(record.ref_count or 0)
            if "CASE" in sql.upper():
                record.ref_count = max(0, count - 1)
            elif "+" in sql:
                record.ref_count = count + 1
            else:
                record.ref_count = max(0, count - 1)
            return SimpleNamespace(rowcount=1)
        if isinstance(stmt, Delete) and getattr(stmt.table, "name", None) == "hub_files":
            if record is not None and int(record.ref_count or 0) == 0:
                db.hub_zero_deleted = True
                return SimpleNamespace(rowcount=1)
            return SimpleNamespace(rowcount=0)
        if isinstance(stmt, Select):
            value = (record.ref_count if record is not None else 0) or 0
            return SimpleNamespace(scalar=lambda: value)
        return MagicMock()

    db.execute.side_effect = _execute


# ---------------------------------------------------------------------------
# A1: 模型与导出
# ---------------------------------------------------------------------------

class TestHubFileRecordModel:
    EXPECTED_COLUMNS = {
        "id", "file_id", "filename", "size", "sha256", "uploader",
        "hub_created_at", "unpack_dir", "unpack_status", "unpack_error",
        "file_count", "total_size", "ref_count", "file_index",
        "supplement_index", "created_at", "updated_at",
        "team_code",  # W9 IDOR 收口：团队归属（导入时从数据集继承）
    }

    def test_table_name_and_columns(self):
        """表名 hub_files，列覆盖验收清单全部字段"""
        assert HubFileRecord.__tablename__ == "hub_files"
        cols = {c.name for c in HubFileRecord.__table__.columns}
        assert cols == self.EXPECTED_COLUMNS

    def test_file_id_unique_index(self):
        """file_id 有唯一索引（幂等导入的命中键）"""
        unique_idx = [
            idx for idx in HubFileRecord.__table__.indexes
            if list(idx.columns) == [HubFileRecord.__table__.columns.file_id]
        ]
        assert unique_idx, "file_id 缺少索引"
        assert all(idx.unique for idx in unique_idx), "file_id 索引必须唯一"

    def test_ref_count_defaults_zero(self):
        """ref_count 默认 0（python default + server_default）"""
        col = HubFileRecord.__table__.columns.ref_count
        assert col.default is not None and col.default.arg == 0
        assert col.server_default is not None and col.server_default.arg == "0"
        assert not col.nullable

    def test_unpack_status_defaults_pending(self):
        col = HubFileRecord.__table__.columns.unpack_status
        assert col.server_default is not None and col.server_default.arg == "pending"

    def test_exported_from_models_init(self):
        """app/models/__init__.py 导出 HubFileRecord"""
        import app.models as models_pkg
        assert models_pkg.HubFileRecord is HubFileRecord
        assert "HubFileRecord" in models_pkg.__all__


# ---------------------------------------------------------------------------
# A3: ensure_unpacked 幂等物化
# ---------------------------------------------------------------------------

class TestEnsureUnpacked:
    def _run(self, db, tmp_path, file_id="fid-a3"):
        svc = HubFileService(endpoint=ENDPOINT, cache_root=str(tmp_path / "dl"))
        return ensure_unpacked(
            db, file_id,
            datasets_root=str(tmp_path / "ds"),
            service=svc,
        )

    @patch("app.services.hub_file_service.requests")
    def test_first_call_full_pipeline(self, mock_requests, tmp_path):
        """未命中：下载→安全解包→建索引→提摘要→写登记表（全链路字段断言）"""
        calls = []
        mock_requests.post.side_effect = zip_post(
            make_attempt_zip_bytes(), "attempt-exec-001.zip", calls=calls)
        mock_requests.delete.return_value = FakeResp("")
        added = []
        db = make_db(first=None, added=added)

        result = self._run(db, tmp_path)

        assert result["cached"] is False
        unpack_dir = tmp_path / "ds" / "fid-a3"
        assert result["unpack_dir"] == str(unpack_dir)
        # 解包产物存在
        assert (unpack_dir / "execution-manifest.json").is_file()
        assert (unpack_dir / "allure" / "results.json").is_file()
        # 确实发生过下载（offset 从 0 递增）
        assert calls and calls[0]["offset"] == 0
        # 登记表写入
        assert len(added) == 1
        rec = added[0]
        assert isinstance(rec, HubFileRecord)
        assert rec.file_id == "fid-a3"
        assert rec.filename == "attempt-exec-001.zip"
        assert rec.size == len(make_attempt_zip_bytes())
        assert rec.sha256 == hashlib.sha256(make_attempt_zip_bytes()).hexdigest()
        assert rec.unpack_status == "unpacked"
        assert rec.unpack_error is None
        assert rec.unpack_dir == str(unpack_dir)
        assert rec.file_count == ATTEMPT_FILE_COUNT
        assert rec.total_size == sum(
            p.stat().st_size for p in unpack_dir.rglob("*") if p.is_file())
        assert isinstance(rec.file_index, list) and len(rec.file_index) == ATTEMPT_FILE_COUNT
        db.commit.assert_called()
        # 摘要
        assert result["summary"]["schema"] == "anc_attempt_v1"
        assert result["summary"]["execution_id"] == "exec-2026-001"

    @patch("app.services.hub_file_service.requests")
    def test_second_call_registry_hit_zero_download(self, mock_requests, tmp_path):
        """unpacked 命中登记表：直接返回，零 MCP 请求"""
        mock_requests.post.side_effect = zip_post(
            make_attempt_zip_bytes(), "attempt-exec-001.zip")
        mock_requests.delete.return_value = FakeResp("")
        added = []
        db = make_db(first=None, added=added)
        first = self._run(db, tmp_path)

        # 第二次：登记表返回第一次写入的记录；任何 MCP 请求都视为违规
        rec = added[0]
        assert rec.unpack_status == "unpacked"
        db2 = make_db(first=rec)
        mock_requests.post.side_effect = AssertionError("登记表命中时不应发起 MCP 请求")
        second = self._run(db2, tmp_path)

        assert second["cached"] is True
        assert second["unpack_dir"] == first["unpack_dir"]
        assert second["file_index"] == first["file_index"]
        assert second["summary"]["execution_id"] == "exec-2026-001"
        assert second["record_id"] == rec.id

    @patch("app.services.hub_file_service.requests")
    def test_pending_record_redownloads(self, mock_requests, tmp_path):
        """记录存在但状态非 unpacked（pending）：重走下载并更新同一条记录"""
        mock_requests.post.side_effect = zip_post(
            make_attempt_zip_bytes(), "attempt-exec-001.zip")
        mock_requests.delete.return_value = FakeResp("")
        rec = HubFileRecord(id="hub_pending_1", file_id="fid-pending", unpack_status="pending")
        added = []
        db = make_db(first=rec, added=added)

        result = self._run(db, tmp_path, file_id="fid-pending")

        assert result["cached"] is False
        assert added == [], "已有记录时应原位更新，不重复 add"
        assert rec.unpack_status == "unpacked"
        assert rec.file_count == ATTEMPT_FILE_COUNT

    @patch("app.services.hub_file_service.requests")
    def test_registry_hit_but_dir_missing_redownloads(self, mock_requests, tmp_path):
        """记录 unpacked 但解包目录被外部删除：视为未命中重走流程"""
        mock_requests.post.side_effect = zip_post(
            make_attempt_zip_bytes(), "attempt-exec-001.zip")
        mock_requests.delete.return_value = FakeResp("")
        rec = HubFileRecord(
            id="hub_gone", file_id="fid-gone", unpack_status="unpacked",
            unpack_dir=str(tmp_path / "ds" / "fid-gone"),
        )
        added = []
        db = make_db(first=rec, added=added)

        result = self._run(db, tmp_path, file_id="fid-gone")

        assert result["cached"] is False
        assert (tmp_path / "ds" / "fid-gone" / "case_list.json").is_file()
        assert rec.unpack_status == "unpacked"

    @patch("app.services.hub_file_service.requests")
    def test_download_ok_but_not_zip_records_failed(self, mock_requests, tmp_path):
        """下载成功但内容不是 zip：抛中文错误，登记表记 failed 与 unpack_error"""
        mock_requests.post.side_effect = zip_post(b"garbage-not-a-zip", "broken.zip")
        mock_requests.delete.return_value = FakeResp("")
        added = []
        db = make_db(first=None, added=added)

        with pytest.raises(HubFileError) as exc:
            self._run(db, tmp_path, file_id="fid-bad")
        assert "不是有效的 zip 文件" in str(exc.value)

        assert len(added) == 1
        rec = added[0]
        assert rec.unpack_status == "failed"
        assert "不是有效的 zip 文件" in (rec.unpack_error or "")

    def test_empty_file_id_raises(self, tmp_path):
        with pytest.raises(HubFileError):
            ensure_unpacked(make_db(), "  ", datasets_root=str(tmp_path))


# ---------------------------------------------------------------------------
# A4: build_file_index
# ---------------------------------------------------------------------------

class TestBuildFileIndex:
    @pytest.fixture
    def unpack_root(self, tmp_path):
        root = tmp_path / "pkg"
        (root / "artifacts").mkdir(parents=True)
        (root / "shots").mkdir(parents=True)
        (root / "execution-manifest.json").write_text("{}", encoding="utf-8")
        (root / "shots" / "screen.png").write_bytes(b"\x89PNG-fake")
        (root / "logs.txt").write_text("hello", encoding="utf-8")
        (root / "data.bin").write_bytes(b"\x00\x01")
        # 嵌套 zip：内含 2 个成员
        nested = BytesIO()
        with zipfile.ZipFile(nested, "w") as zf:
            zf.writestr("results.xml", "<xml/>")
            zf.writestr("sub/attach.txt", "attach")
        (root / "artifacts" / "allure.zip").write_bytes(nested.getvalue())
        return root

    def test_index_items(self, unpack_root):
        """每项含 path/type/size/is_nested_zip，按后缀正确分类"""
        index = build_file_index(str(unpack_root))
        by_path = {item["path"]: item for item in index}

        assert set(by_path) == {
            "execution-manifest.json", "logs.txt", "data.bin",
            "shots/screen.png", "artifacts/allure.zip",
        }
        manifest = by_path["execution-manifest.json"]
        assert manifest["type"] == "text" and manifest["is_nested_zip"] is False
        assert manifest["size"] == 2
        assert by_path["logs.txt"]["type"] == "text"
        assert by_path["shots/screen.png"]["type"] == "image"
        assert by_path["data.bin"]["type"] == "binary"
        nested_item = by_path["artifacts/allure.zip"]
        assert nested_item["type"] == "archive"
        assert nested_item["is_nested_zip"] is True

    def test_nested_zip_index_from_central_directory(self, unpack_root):
        """嵌套 zip 读 central directory 生成 nested_index，且不解压内容"""
        index = build_file_index(str(unpack_root))
        nested_item = next(i for i in index if i["is_nested_zip"])
        nested_index = nested_item["nested_index"]

        assert [e["path"] for e in nested_index] == ["results.xml", "sub/attach.txt"]
        by_entry = {e["path"]: e for e in nested_index}
        assert by_entry["results.xml"]["size"] == len("<xml/>")
        assert by_entry["sub/attach.txt"]["compress_size"] > 0
        # 不解压：包内没有内层成员文件
        assert not (unpack_root / "artifacts" / "results.xml").exists()
        assert not (unpack_root / "artifacts" / "sub").exists()

    def test_bad_nested_zip_index_none(self, tmp_path):
        """损坏的嵌套 zip：nested_index 为 None，不阻断整体索引"""
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "broken.zip").write_bytes(b"definitely not zip")
        index = build_file_index(str(root))
        assert len(index) == 1
        assert index[0]["is_nested_zip"] is True
        assert index[0]["nested_index"] is None

    def test_missing_root_returns_empty(self, tmp_path):
        assert build_file_index(str(tmp_path / "nope")) == []

    def test_unpacked_attempt_index_consistent(self, tmp_path):
        """ensure_unpacked 产物与 build_file_index 语义一致（集成断言）"""
        zip_bytes = make_attempt_zip_bytes()
        dest = tmp_path / "unpacked"
        p = tmp_path / "pkg.zip"
        p.write_bytes(zip_bytes)
        stats = unpack_zip_safely(str(p), str(dest))
        index = build_file_index(str(dest))
        assert stats["file_count"] == len(index) == ATTEMPT_FILE_COUNT
        types = {i["type"] for i in index}
        assert types == {"text"}  # attempt 样本成员均为文本类


# ---------------------------------------------------------------------------
# A5: extract_hub_summary
# ---------------------------------------------------------------------------

class TestExtractHubSummary:
    def _unpack_attempt(self, tmp_path):
        dest = tmp_path / "attempt"
        p = tmp_path / "attempt.zip"
        p.write_bytes(make_attempt_zip_bytes())
        unpack_zip_safely(str(p), str(dest))
        return dest

    def test_attempt_fixture_summary(self, tmp_path):
        """attempt 样本产出 anc_attempt_v1 摘要，各字段正确"""
        dest = self._unpack_attempt(tmp_path)
        summary = extract_hub_summary(str(dest))
        assert summary == {
            "schema": "anc_attempt_v1",
            "execution_id": "exec-2026-001",
            "case_total": 3,
            "case_passed": 2,
            "case_failed": 1,
            "segment_count": 3,
            "file_count": ATTEMPT_FILE_COUNT,
        }

    def test_plain_zip_without_manifest_returns_none(self, tmp_path):
        """无 execution-manifest.json 的普通 zip：返回 None"""
        dest = tmp_path / "plain"
        p = tmp_path / "plain.zip"
        p.write_bytes(make_plain_zip_bytes())
        unpack_zip_safely(str(p), str(dest))
        assert extract_hub_summary(str(dest)) is None

    def test_missing_root_returns_none(self, tmp_path):
        assert extract_hub_summary(str(tmp_path / "nope")) is None

    def test_broken_manifest_returns_none(self, tmp_path):
        """manifest 损坏（非法 JSON）：返回 None 而不抛错"""
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "execution-manifest.json").write_text("{not-json", encoding="utf-8")
        assert extract_hub_summary(str(root)) is None

    def test_case_total_falls_back_when_no_case_results(self, tmp_path):
        """manifest 无 case_results：case_total 回退 acceptance-map.cases"""
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "execution-manifest.json").write_text(json.dumps(
            {"execution_id": "e2", "scope": []}), encoding="utf-8")
        (root / "acceptance-map.json").write_text(json.dumps(
            {"cases": [{"case_id": "c1"}, {"case_id": "c2"}]}), encoding="utf-8")
        summary = extract_hub_summary(str(root))
        assert summary["case_total"] == 2
        assert summary["case_passed"] == 0 and summary["case_failed"] == 0

    # ----- 缺陷B1：dict 形态 case_results（真实执行包实测形态） -----

    def _write_manifest_root(self, tmp_path, manifest):
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "execution-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return root

    def test_dict_case_results_string_status(self, tmp_path):
        """dict 形态 {TC_001: "failed"}：真实包期望 passed=10 / failed=8、total=18"""
        case_results = {}
        for i in range(10):
            case_results[f"TC_{i + 1:03d}"] = "passed"
        for i in range(8):
            case_results[f"TC_{i + 101:03d}"] = "failed"
        root = self._write_manifest_root(tmp_path, {"execution_id": "e-dict", "case_results": case_results})
        summary = extract_hub_summary(str(root))
        assert summary["case_total"] == 18
        assert summary["case_passed"] == 10
        assert summary["case_failed"] == 8

    def test_dict_case_results_dict_value_takes_status(self, tmp_path):
        """dict 形态 value 为 dict：取其 .get("status")（历史形态兼容）"""
        case_results = {
            "TC_001": {"status": "passed", "duration": 1.2},
            "TC_002": {"status": "error"},   # error 归入 failed
            "TC_003": {"status": "broken"},  # broken 不计入 passed/failed
        }
        root = self._write_manifest_root(tmp_path, {"execution_id": "e-dict2", "case_results": case_results})
        summary = extract_hub_summary(str(root))
        assert summary["case_total"] == 3
        assert summary["case_passed"] == 1
        assert summary["case_failed"] == 1

    def test_list_case_results_regression_unchanged(self, tmp_path):
        """list 形态（历史语义）回归：dict 兼容不改变 list 统计结果"""
        dest = self._unpack_attempt(tmp_path)
        summary = extract_hub_summary(str(dest))
        assert summary["case_total"] == 3
        assert summary["case_passed"] == 2
        assert summary["case_failed"] == 1


# ---------------------------------------------------------------------------
# A7: acquire_ref / release_ref 引用计数
# ---------------------------------------------------------------------------

class TestRefCounts:
    def _record(self, tmp_path, ref_count=0, with_dir=True):
        unpack_dir = tmp_path / "ds" / "fid-ref"
        if with_dir:
            unpack_dir.mkdir(parents=True, exist_ok=True)
            (unpack_dir / "case_list.json").write_text("[]", encoding="utf-8")
        return HubFileRecord(
            id="hub_ref_1", file_id="fid-ref", unpack_status="unpacked",
            unpack_dir=str(unpack_dir), ref_count=ref_count,
        )

    def test_acquire_increments(self, tmp_path):
        """引用计数 +1 正确（0→1→2）"""
        rec = self._record(tmp_path, ref_count=0)
        assert acquire_ref(make_db(first=rec), "fid-ref") == 1
        assert rec.ref_count == 1
        assert acquire_ref(make_db(first=rec), "fid-ref") == 2
        assert rec.ref_count == 2

    def test_release_decrements_and_keeps_dir(self, tmp_path):
        """未归零：-1 正确，解包目录保留，登记记录不删"""
        rec = self._record(tmp_path, ref_count=3)
        db = make_db(first=rec)
        assert release_ref(db, "fid-ref") == 2
        assert rec.ref_count == 2
        assert (tmp_path / "ds" / "fid-ref" / "case_list.json").is_file()
        db.delete.assert_not_called()

    def test_release_to_zero_cleans_dir_and_record(self, tmp_path, cache_root):
        """归零：清理解包目录并条件删除登记记录（DELETE ... WHERE ref_count = 0 命中）"""
        rec = self._record(tmp_path, ref_count=1)
        unpack_dir = tmp_path / "ds" / "fid-ref"
        db = make_db(first=rec)

        assert release_ref(db, "fid-ref") == 0
        assert not unpack_dir.exists(), "归零后解包目录应被清理"
        assert getattr(db, "hub_zero_deleted", False) is True, "归零条件删除应命中"

    # ----- 小项1：归零联动清理下载缓存目录 uploads/hub_files/{file_id}/ -----

    @pytest.fixture
    def cache_root(self, tmp_path, monkeypatch):
        """把下载缓存根指到临时目录，避免测试触碰真实 uploads/hub_files"""
        root = tmp_path / "hub_files"
        root.mkdir()
        monkeypatch.setattr(hub_file_service, "_hub_files_cache_root", lambda: str(root))
        return root

    def test_release_to_zero_cleans_download_cache_dir(self, tmp_path, cache_root):
        """归零：联动清理下载缓存目录 uploads/hub_files/{file_id}/（原始 zip 落盘处）"""
        rec = self._record(tmp_path, ref_count=1)
        cache_dir = cache_root / "fid-ref"
        cache_dir.mkdir()
        (cache_dir / "pkg.zip").write_bytes(b"zip-bytes")

        assert release_ref(make_db(first=rec), "fid-ref") == 0
        assert not cache_dir.exists(), "归零后下载缓存目录应被联动清理"

    def test_release_above_zero_keeps_download_cache_dir(self, tmp_path, cache_root):
        """未归零：下载缓存目录保留（仍有引用，下次导入零下载复用）"""
        rec = self._record(tmp_path, ref_count=2)
        cache_dir = cache_root / "fid-ref"
        cache_dir.mkdir()
        (cache_dir / "pkg.zip").write_bytes(b"zip-bytes")

        assert release_ref(make_db(first=rec), "fid-ref") == 1
        assert (cache_dir / "pkg.zip").is_file()

    def test_release_to_zero_cache_dir_missing_is_noop(self, tmp_path, cache_root):
        """归零但缓存目录本就不存在：no-op 不报错（登记记录仍正常条件删除）"""
        rec = self._record(tmp_path, ref_count=1)
        db = make_db(first=rec)
        assert release_ref(db, "fid-ref") == 0
        assert getattr(db, "hub_zero_deleted", False) is True

    def test_acquire_unregistered_raises(self, tmp_path):
        """未登记文件 acquire：中文报错"""
        with pytest.raises(HubFileError) as exc:
            acquire_ref(make_db(first=None), "fid-nope")
        assert "未登记" in str(exc.value)

    def test_release_unregistered_raises(self, tmp_path):
        with pytest.raises(HubFileError) as exc:
            release_ref(make_db(first=None), "fid-nope")
        assert "未登记" in str(exc.value)

    def test_empty_file_id_raises(self, tmp_path):
        with pytest.raises(HubFileError):
            acquire_ref(make_db(), "")
        with pytest.raises(HubFileError):
            release_ref(make_db(), "  ")
