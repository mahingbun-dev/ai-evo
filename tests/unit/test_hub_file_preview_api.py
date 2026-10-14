"""
数据集迭代阶段D单元测试：执行包文件树 / 预览 / 下载 / 匹配预览 API

覆盖验收清单 D1..D6：
- D1 GET /hub-files/{file_id}/tree：返回登记表 file_index（零文件 IO）；未登记 404
- D2 GET /hub-files/{file_id}/nested-tree：嵌套 zip 内层清单；非嵌套 zip 4xx 中文错误
- D3 GET /hub-files/{file_id}/content：文本 {content(截断2MB), mime, truncated}、
  图片 FileResponse、xlsx/zip 等二进制 4xx 提示走 download
- D4 GET /hub-files/{file_id}/download：包内任意文件下载，中文文件名 URL 编码
- D5 POST /hub-files/match-preview：与执行时同一 match_files 语义；无样本数据空态
- D6 防穿越：../、绝对路径、编码逃逸（%2e%2e）、反斜杠、symlink 逃逸一律 4xx

说明：按仓库既有 API 测试模式（参照 test_hub_import_api.py）直接调用端点函数，
不经过 TestClient——规避存量测试引入 nest_asyncio 全局补丁后对 anyio portal
的跨文件污染（Python 3.14 下 asyncio Runner 兼容问题）；db 用 MagicMock 按模型
分发，不连真实库；解包目录用 tmp_path 真实落盘。
"""
import io
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.api.hub_file_api import (
    HubMatchPreviewRequest,
    download_hub_file,
    get_hub_file_content,
    get_hub_file_nested_tree,
    get_hub_file_tree,
    match_preview,
)
from app.services.hub_file_service import (
    PREVIEW_TEXT_LIMIT,
    HubFileError,
    build_file_index,
    match_files,
    match_files_in_index,
    resolve_path_in_root,
)
from urllib.parse import quote
from tests.unit.w9_direct_call import fake_request  # W9: 端点直调适配

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------------------------------------------------------------------
# mock 工具
# ---------------------------------------------------------------------------

def make_record_db(record):
    """db.query(HubFileRecord).filter().first() 返回 record（None=未登记）"""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = record
    return db


def make_record(unpack_dir, file_index=None, unpack_status="unpacked"):
    return SimpleNamespace(
        id="hub_rec_1",
        file_id="fid-1",
        unpack_dir=str(unpack_dir),
        unpack_status=unpack_status,
        file_index=file_index,
    )


def make_preview_db(rows=None, record=None):
    """按模型类分发查询链：EvaluationDataV2 → 数据行列表，HubFileRecord → 登记记录"""
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "EvaluationDataV2":
            q.filter.return_value.order_by.return_value.all.return_value = rows or []
        elif name == "HubFileRecord":
            q.filter.return_value.first.return_value = record
        return q

    db.query.side_effect = _query
    return db


@pytest.fixture
def unpack_root(tmp_path):
    """真实解包目录：文本/图片/二进制/中文文件名/嵌套 zip 齐备，包外放攻击目标文件"""
    root = tmp_path / "unpack" / "fid-1"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "run.log").write_text("日志内容", encoding="utf-8")
    (root / "脚本.py").write_text("print('你好')", encoding="utf-8")
    (root / "meta.json").write_text('{"a": 1}', encoding="utf-8")
    (root / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n fake png")
    (root / "pic.svg").write_text("<svg></svg>", encoding="utf-8")
    (root / "数据.xlsx").write_bytes(b"fake xlsx bytes")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inner/报告.txt", "内层文件内容")
        zf.writestr("inner/data.json", '{"k": "v"}')
    (root / "nested.zip").write_bytes(buf.getvalue())
    (tmp_path / "secret.txt").write_text("包外秘密", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# D1 文件树（登记表索引，零文件 IO）
# ---------------------------------------------------------------------------

class TestHubFileTreeApi:
    def test_tree_returns_registry_file_index_without_io(self, unpack_root):
        """登记表命中：返回 file_index 原文；unpack_dir 不存在也照常返回（零文件 IO）"""
        index = [{"path": "a.log", "type": "text", "size": 3, "is_nested_zip": False}]
        # 解包目录指向不存在的路径，若端点触碰文件系统则会失败——证明零 IO
        record = make_record("/nonexistent/unpack/fid-1", file_index=index)
        db = make_record_db(record)
        result = get_hub_file_tree("fid-1", request=fake_request(), db=db)
        assert result == {"file_id": "fid-1", "unpack_status": "unpacked", "file_index": index}

    def test_tree_unregistered_404(self):
        db = make_record_db(None)
        with pytest.raises(HTTPException) as exc:
            get_hub_file_tree("fid-gone", request=fake_request(), db=db)
        assert exc.value.status_code == 404
        assert "未登记" in exc.value.detail
        assert "fid-gone" in exc.value.detail

    def test_tree_failed_record_returns_empty_index(self):
        """登记存在但解包失败：返回空索引（file_index 为 None → []）"""
        record = make_record("/nonexistent", file_index=None, unpack_status="failed")
        db = make_record_db(record)
        result = get_hub_file_tree("fid-1", request=fake_request(), db=db)
        assert result["file_index"] == []
        assert result["unpack_status"] == "failed"

    def test_tree_empty_file_id_400(self):
        db = make_record_db(None)
        with pytest.raises(HTTPException) as exc:
            get_hub_file_tree("  ", request=fake_request(), db=db)
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# D2 嵌套 zip 内层清单
# ---------------------------------------------------------------------------

class TestHubFileNestedTreeApi:
    def test_nested_tree_lists_inner_entries(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        result = get_hub_file_nested_tree("fid-1", path="nested.zip", request=fake_request(), db=db)
        assert result["file_id"] == "fid-1"
        assert result["path"] == "nested.zip"
        paths = [e["path"] for e in result["entries"]]
        assert paths == ["inner/报告.txt", "inner/data.json"]
        by_path = {e["path"]: e for e in result["entries"]}
        assert by_path["inner/报告.txt"]["size"] == len("内层文件内容".encode("utf-8"))

    def test_nested_tree_non_zip_400(self, unpack_root):
        """path 非嵌套 zip（普通文本文件）→ 4xx 中文错误"""
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            get_hub_file_nested_tree("fid-1", path="logs/run.log", request=fake_request(), db=db)
        assert exc.value.status_code == 400
        assert "不是 zip" in exc.value.detail

    def test_nested_tree_binary_non_zip_400(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            get_hub_file_nested_tree("fid-1", path="数据.xlsx", request=fake_request(), db=db)
        assert exc.value.status_code == 400

    def test_nested_tree_directory_400(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            get_hub_file_nested_tree("fid-1", path="logs", request=fake_request(), db=db)
        assert exc.value.status_code == 400
        assert "不是文件" in exc.value.detail

    def test_nested_tree_missing_path_404(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            get_hub_file_nested_tree("fid-1", path="no/such.zip", request=fake_request(), db=db)
        assert exc.value.status_code == 404

    def test_nested_tree_unregistered_404(self, unpack_root):
        db = make_record_db(None)
        with pytest.raises(HTTPException) as exc:
            get_hub_file_nested_tree("fid-gone", path="nested.zip", request=fake_request(), db=db)
        assert exc.value.status_code == 404

    def test_nested_tree_not_unpacked_404(self, unpack_root):
        """登记状态 failed：无可用解包区 → 404 提示重新导入"""
        db = make_record_db(make_record(unpack_root, unpack_status="failed"))
        with pytest.raises(HTTPException) as exc:
            get_hub_file_nested_tree("fid-1", path="nested.zip", request=fake_request(), db=db)
        assert exc.value.status_code == 404
        assert "重新导入" in exc.value.detail


# ---------------------------------------------------------------------------
# D3 内容预览
# ---------------------------------------------------------------------------

class TestHubFileContentApi:
    def test_text_py_with_chinese(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        result = get_hub_file_content("fid-1", path="脚本.py", request=fake_request(), db=db)
        assert isinstance(result, dict)
        assert result["content"] == "print('你好')"
        assert result["mime"] == "text/x-python"
        assert result["truncated"] is False
        assert result["path"] == "脚本.py"

    def test_text_json_mime(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        result = get_hub_file_content("fid-1", path="meta.json", request=fake_request(), db=db)
        assert result["mime"] == "application/json"
        assert result["content"] == '{"a": 1}'

    def test_text_log_mime(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        result = get_hub_file_content("fid-1", path="logs/run.log", request=fake_request(), db=db)
        assert result["mime"] == "text/plain"
        assert result["content"] == "日志内容"

    def test_text_truncated_at_2mb(self, unpack_root, tmp_path):
        """超过 2MB 的文本只返回前 2MB，truncated=True"""
        big = unpack_root / "big.log"
        payload = "汉" * 10 + "a" * (PREVIEW_TEXT_LIMIT + 100)
        big.write_text(payload, encoding="utf-8")
        db = make_record_db(make_record(unpack_root))
        result = get_hub_file_content("fid-1", path="big.log", request=fake_request(), db=db)
        assert result["truncated"] is True
        assert result["size"] == len(payload.encode("utf-8"))
        # 截断按 2MB 字节计算（前 2MB 字节按 UTF-8 容错解码）
        assert len(result["content"].encode("utf-8")) <= PREVIEW_TEXT_LIMIT + 2

    def test_image_png_file_response(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        result = get_hub_file_content("fid-1", path="img.png", request=fake_request(), db=db)
        assert isinstance(result, FileResponse)
        assert result.media_type == "image/png"

    def test_image_svg_file_response(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        result = get_hub_file_content("fid-1", path="pic.svg", request=fake_request(), db=db)
        assert isinstance(result, FileResponse)
        assert result.media_type == "image/svg+xml"

    @pytest.mark.parametrize("path", ["数据.xlsx", "nested.zip"])
    def test_binary_4xx_hint_download(self, unpack_root, path):
        """xlsx / zip 等二进制 → 4xx，中文提示走下载"""
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            get_hub_file_content("fid-1", path=path, request=fake_request(), db=db)
        assert 400 <= exc.value.status_code < 500
        assert "下载" in exc.value.detail

    def test_missing_file_404(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            get_hub_file_content("fid-1", path="no/such.txt", request=fake_request(), db=db)
        assert exc.value.status_code == 404

    def test_unregistered_404(self, unpack_root):
        db = make_record_db(None)
        with pytest.raises(HTTPException) as exc:
            get_hub_file_content("fid-gone", path="meta.json", request=fake_request(), db=db)
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# D4 下载
# ---------------------------------------------------------------------------

class TestHubFileDownloadApi:
    def test_download_chinese_filename_url_encoded(self, unpack_root):
        """中文文件名按 dataset_api 模板下载同一模式做 URL 编码"""
        db = make_record_db(make_record(unpack_root))
        resp = download_hub_file("fid-1", path="数据.xlsx", request=fake_request(), db=db)
        assert isinstance(resp, FileResponse)
        encoded = quote("数据.xlsx")
        disposition = resp.headers["content-disposition"]
        assert f"filename={encoded}" in disposition
        assert f"filename*=UTF-8''{encoded}" in disposition
        assert "数据.xlsx" not in disposition  # 原始中文名不裸出现在 header

    def test_download_media_type_by_ext(self, unpack_root):
        """media_type 按扩展名推断（mimetypes 内建表，跨环境稳定）"""
        db = make_record_db(make_record(unpack_root))
        # W8 修复：原断言用 .log → text/plain，该映射来自系统 mime db
        # （/etc/mime.types），python:3.12-slim 离线镜像与生产镜像都没有它
        # （实测 guess_type('run.log') → None → 落 octet-stream）。改用
        # Python mimetypes 内建表覆盖的扩展名断言，CI 与生产环境行为一致。
        resp = download_hub_file("fid-1", path="img.png", request=fake_request(), db=db)
        assert resp.media_type == "image/png"
        resp2 = download_hub_file("fid-1", path="meta.json", request=fake_request(), db=db)
        assert resp2.media_type == "application/json"

    def test_download_nested_zip(self, unpack_root):
        """嵌套 zip 本身可整包下载"""
        db = make_record_db(make_record(unpack_root))
        resp = download_hub_file("fid-1", path="nested.zip", request=fake_request(), db=db)
        assert isinstance(resp, FileResponse)
        assert resp.media_type == "application/zip"

    def test_download_missing_404(self, unpack_root):
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            download_hub_file("fid-1", path="nope.bin", request=fake_request(), db=db)
        assert exc.value.status_code == 404

    def test_download_unregistered_404(self, unpack_root):
        db = make_record_db(None)
        with pytest.raises(HTTPException) as exc:
            download_hub_file("fid-gone", path="meta.json", request=fake_request(), db=db)
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# D5 匹配预览
# ---------------------------------------------------------------------------

class TestHubMatchPreviewApi:
    def _db(self, unpack_root, rows=None, record=None):
        if record is None:
            record = make_record(unpack_root, file_index=build_file_index(str(unpack_root)))
        return make_preview_db(rows=rows, record=record)

    def test_preview_matches_with_execution_semantics(self, unpack_root):
        """首条含字段样本的登记索引上试匹配：计数/样例/样本行号齐备"""
        rows = [
            SimpleNamespace(index=0, data={"note": "无 hub 字段"}),
            SimpleNamespace(index=3, data={"cases_file": "fid-1", "note": "样本行"}),
        ]
        db = self._db(unpack_root, rows=rows)
        body = HubMatchPreviewRequest(dataset_id="ds-1", field_name="cases_file", pattern="**/*.log")
        result = match_preview(body, request=fake_request(), db=db)
        assert result["file_id"] == "fid-1"
        assert result["sample_data_index"] == 3
        assert result["matched_count"] == 1
        assert result["sample_paths"] == ["logs/run.log"]
        assert result["dataset_id"] == "ds-1"
        assert result["field_name"] == "cases_file"
        assert result["pattern"] == "**/*.log"
        assert "message" not in result

    def test_preview_sample_paths_capped_at_20(self, tmp_path):
        """匹配 30 个文件时 sample_paths 截断到 20，matched_count 仍为 30"""
        root = tmp_path / "unpack" / "fid-1"
        (root / "logs").mkdir(parents=True)
        for i in range(30):
            (root / "logs" / f"f{i:02d}.log").write_text("x", encoding="utf-8")
        rows = [SimpleNamespace(index=0, data={"cases_file": "fid-1"})]
        db = self._db(root, rows=rows)
        body = HubMatchPreviewRequest(dataset_id="ds-1", field_name="cases_file", pattern="logs/*.log")
        result = match_preview(body, request=fake_request(), db=db)
        assert result["matched_count"] == 30
        assert len(result["sample_paths"]) == 20
        assert result["sample_paths"][0] == "logs/f00.log"

    def test_preview_no_match_returns_zero(self, unpack_root):
        rows = [SimpleNamespace(index=0, data={"cases_file": "fid-1"})]
        db = self._db(unpack_root, rows=rows)
        body = HubMatchPreviewRequest(dataset_id="ds-1", field_name="cases_file", pattern="**/*.rs")
        result = match_preview(body, request=fake_request(), db=db)
        assert result["matched_count"] == 0
        assert result["sample_paths"] == []
        assert result["sample_data_index"] == 0

    def test_preview_empty_state_without_sample_data(self, unpack_root):
        """无样本数据（字段全空/字段不存在/无数据行）→ 可展示空态"""
        for rows in (
            [SimpleNamespace(index=0, data={"cases_file": "  "})],   # 空白值
            [SimpleNamespace(index=0, data={"note": "x"})],          # 字段不存在
            [],                                                       # 无数据行
        ):
            db = self._db(unpack_root, rows=rows)
            body = HubMatchPreviewRequest(dataset_id="ds-1", field_name="cases_file", pattern="*.log")
            result = match_preview(body, request=fake_request(), db=db)
            assert result["matched_count"] == 0
            assert result["sample_paths"] == []
            assert result["sample_data_index"] is None
            assert result["file_id"] is None
            assert "样本数据" in result["message"]

    def test_preview_unregistered_file_404(self, unpack_root):
        """样本引用的执行包未登记 → 404 提示重新导入"""
        rows = [SimpleNamespace(index=0, data={"cases_file": "fid-gone"})]
        db = make_preview_db(rows=rows, record=None)
        body = HubMatchPreviewRequest(dataset_id="ds-1", field_name="cases_file", pattern="*.log")
        with pytest.raises(HTTPException) as exc:
            match_preview(body, request=fake_request(), db=db)
        assert exc.value.status_code == 404
        assert "fid-gone" in exc.value.detail
        assert "重新导入" in exc.value.detail

    def test_preview_blank_pattern_400(self, unpack_root):
        """空白 pattern（pydantic min_length 放行）→ 服务层中文 400"""
        rows = [SimpleNamespace(index=0, data={"cases_file": "fid-1"})]
        db = self._db(unpack_root, rows=rows)
        body = HubMatchPreviewRequest(dataset_id="ds-1", field_name="cases_file", pattern="   ")
        with pytest.raises(HTTPException) as exc:
            match_preview(body, request=fake_request(), db=db)
        assert exc.value.status_code == 400
        assert "不能为空" in exc.value.detail

    def test_preview_pattern_traversal_400(self, unpack_root):
        """pattern 含 .. 穿越与执行时 match_files 同口径拒绝"""
        rows = [SimpleNamespace(index=0, data={"cases_file": "fid-1"})]
        db = self._db(unpack_root, rows=rows)
        body = HubMatchPreviewRequest(dataset_id="ds-1", field_name="cases_file", pattern="../**/*.log")
        with pytest.raises(HTTPException) as exc:
            match_preview(body, request=fake_request(), db=db)
        assert exc.value.status_code == 400
        assert "穿越" in exc.value.detail

    def test_preview_pydantic_rejects_blank_fields(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            HubMatchPreviewRequest(dataset_id="", field_name="f", pattern="*")
        with pytest.raises(ValidationError):
            HubMatchPreviewRequest(dataset_id="ds", field_name="", pattern="*")


# ---------------------------------------------------------------------------
# D6 防穿越攻击用例（所有带 path 参数的端点）
# ---------------------------------------------------------------------------

ATTACK_PATHS = [
    "../secret.txt",                      # 单层 ..（包外目标真实存在）
    "../../secret.txt",                   # 深层 ..
    "logs/../../secret.txt",              # 混入正常前缀的 ..
    "..\\..\\secret.txt",                 # 反斜杠写法
    "/etc/passwd",                        # 绝对路径
    "//etc/passwd",                       # 双斜杠绝对路径
    "C:/Windows/win.ini",                 # Windows 盘符
    "%2e%2e%2fsecret.txt",                # URL 编码字面量（经 FastAPI 解码后即 ../；未解码时也是包内不存在的文件名）
]

# 服务层必须直接拒绝的穿越写法（字面量 %2e%2e 不在其列：它只是个不存在的文件名，
# 由端点存在性检查兜底 404，服务层不做二次 URL 解码——否则会误伤含 % 的合法文件名）
TRAVERSAL_PATHS = [p for p in ATTACK_PATHS if p != "%2e%2e%2fsecret.txt"]

PATH_ENDPOINTS = [
    ("nested-tree", get_hub_file_nested_tree),
    ("content", get_hub_file_content),
    ("download", download_hub_file),
]


class TestPathTraversalAttacks:
    @pytest.mark.parametrize("endpoint_name,endpoint", PATH_ENDPOINTS, ids=[n for n, _ in PATH_ENDPOINTS])
    @pytest.mark.parametrize("attack", ATTACK_PATHS)
    def test_attack_paths_rejected_4xx(self, unpack_root, endpoint_name, endpoint, attack):
        """.. / 绝对路径 / 编码字面量一律 4xx，绝不读到包外 secret.txt"""
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            endpoint("fid-1", request=fake_request(), path=attack, db=db)
        assert 400 <= exc.value.status_code < 500, (
            f"{endpoint_name} 对攻击路径 {attack!r} 应返回 4xx，实际 {exc.value.status_code}"
        )

    @pytest.mark.parametrize("endpoint_name,endpoint", PATH_ENDPOINTS, ids=[n for n, _ in PATH_ENDPOINTS])
    def test_decoded_traversal_rejected(self, unpack_root, endpoint_name, endpoint):
        """%2e%2e%2f 经 FastAPI 查询参数解码后即 '../'，同样 4xx"""
        db = make_record_db(make_record(unpack_root))
        decoded = "%2e%2e%2fsecret.txt".replace("%2e%2e%2f", "../")
        assert decoded == "../secret.txt"
        with pytest.raises(HTTPException) as exc:
            endpoint("fid-1", request=fake_request(), path=decoded, db=db)
        assert 400 <= exc.value.status_code < 500

    @pytest.mark.parametrize("endpoint_name,endpoint", PATH_ENDPOINTS, ids=[n for n, _ in PATH_ENDPOINTS])
    def test_symlink_escape_rejected(self, tmp_path, unpack_root, endpoint_name, endpoint):
        """包内 symlink 指向包外文件：realpath 越出解包根 → 4xx"""
        link = unpack_root / "leak.txt"
        try:
            link.symlink_to(tmp_path / "secret.txt")
        except OSError:
            pytest.skip("当前环境无法创建 symlink")
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            endpoint("fid-1", request=fake_request(), path="leak.txt", db=db)
        assert 400 <= exc.value.status_code < 500
        assert "越出执行包范围" in exc.value.detail

    @pytest.mark.parametrize("endpoint_name,endpoint", PATH_ENDPOINTS, ids=[n for n, _ in PATH_ENDPOINTS])
    def test_empty_path_rejected(self, unpack_root, endpoint_name, endpoint):
        db = make_record_db(make_record(unpack_root))
        with pytest.raises(HTTPException) as exc:
            endpoint("fid-1", request=fake_request(), path="", db=db)
        assert 400 <= exc.value.status_code < 500


# ---------------------------------------------------------------------------
# 防穿越统一校验函数（服务层）
# ---------------------------------------------------------------------------

class TestResolvePathInRoot:
    def test_valid_rel_path(self, unpack_root):
        real = resolve_path_in_root(str(unpack_root), "logs/run.log")
        assert os.path.realpath(str(unpack_root / "logs" / "run.log")) == real

    def test_dot_path_returns_root(self, unpack_root):
        assert resolve_path_in_root(str(unpack_root), ".") == os.path.realpath(str(unpack_root))

    @pytest.mark.parametrize("attack", TRAVERSAL_PATHS)
    def test_attacks_raise_chinese_error(self, unpack_root, attack):
        with pytest.raises(HubFileError):
            resolve_path_in_root(str(unpack_root), attack)

    def test_symlink_escape_raises(self, tmp_path, unpack_root):
        link = unpack_root / "leak.txt"
        try:
            link.symlink_to(tmp_path / "secret.txt")
        except OSError:
            pytest.skip("当前环境无法创建 symlink")
        with pytest.raises(HubFileError) as exc:
            resolve_path_in_root(str(unpack_root), "leak.txt")
        assert "越出执行包范围" in str(exc.value)

    def test_empty_raises(self, unpack_root):
        with pytest.raises(HubFileError, match="不能为空"):
            resolve_path_in_root(str(unpack_root), "   ")


# ---------------------------------------------------------------------------
# match_files_in_index 与 match_files 同语义（等价性）
# ---------------------------------------------------------------------------

EQUIV_PATTERNS = [
    "*.py", "**/*.py", "**/*.log", "logs/*.log", "**", "a/*/c.txt",
    "a/**/*.txt", "**/c.txt", "./top.py", "nested.zip", "nested*",
    "logs/**", "*.md", "a/*/*.txt", "**/*.txt", "?.py", "*/*.log",
    "logs/", "top.py/", "**/deep.log", ".h*/*.log",
]


class TestMatchFilesIndexEquivalence:
    @pytest.fixture
    def tree(self, tmp_path):
        """含隐藏文件/隐藏目录/嵌套层级的真实文件树"""
        root = tmp_path / "tree"
        for rel in [
            "top.py", ".hidden.py", "readme.md", "nested.zip",
            "logs/a.log", "logs/b.log", ".hdir/deep.log",
            "a/b/c.txt", "a/c/d.txt", "a/one.py",
        ]:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        return root

    @pytest.mark.parametrize("pattern", EQUIV_PATTERNS)
    def test_same_result_as_filesystem_match(self, tree, pattern):
        """纯函数版与文件系统版逐模式等价（含 '**' 递归/隐藏文件/尾随斜杠）"""
        fs = match_files(str(tree), pattern)
        idx = match_files_in_index(build_file_index(str(tree)), pattern)
        assert idx == fs, f"pattern={pattern!r} 不一致: fs={fs} index={idx}"

    def test_both_raise_on_empty_pattern(self, tree):
        with pytest.raises(HubFileError):
            match_files(str(tree), "  ")
        with pytest.raises(HubFileError):
            match_files_in_index(build_file_index(str(tree)), "  ")

    def test_both_raise_on_traversal_pattern(self, tree):
        with pytest.raises(HubFileError):
            match_files(str(tree), "../**/*.log")
        with pytest.raises(HubFileError):
            match_files_in_index(build_file_index(str(tree)), "../**/*.log")

    def test_none_index_returns_empty(self):
        assert match_files_in_index(None, "**/*.log") == []
        assert match_files_in_index([], "*.py") == []


# ---------------------------------------------------------------------------
# 缺陷B2：单顶层目录自动拼接（真实执行包常带 attempt-1/ 顶层目录，
# 用户直觉 pattern 相对 attempt 根，如 reports/allure_results/*.json）
# ---------------------------------------------------------------------------

class TestSingleTopDirAutoPrefix:
    @pytest.fixture
    def attempt_root(self, tmp_path):
        """解包根下仅含一个顶层目录 attempt-1/（真实执行包常见形态）"""
        root = tmp_path / "unpack" / "fid-attempt"
        for rel in [
            "reports/allure_results/r1-result.json",
            "reports/allure_results/r2-result.json",
            "logs/run.log",
            "execution-manifest.json",
        ]:
            target = root / "attempt-1" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        return root

    def test_relative_pattern_auto_prefixed(self, attempt_root):
        """相对 attempt 根的 pattern（用户直觉/前端 placeholder 示例）自动拼接后命中"""
        matched = match_files(str(attempt_root), "reports/allure_results/*.json")
        assert matched == [
            "attempt-1/reports/allure_results/r1-result.json",
            "attempt-1/reports/allure_results/r2-result.json",
        ]

    def test_pattern_already_with_top_dir_unchanged(self, attempt_root):
        """已含顶层目录的 pattern 照常匹配（不拼接、结果不重复）"""
        matched = match_files(str(attempt_root), "attempt-1/reports/allure_results/*.json")
        assert matched == [
            "attempt-1/reports/allure_results/r1-result.json",
            "attempt-1/reports/allure_results/r2-result.json",
        ]

    def test_pattern_with_top_dir_prefix_never_double_prefixed(self, attempt_root):
        """pattern 首段已是顶层目录名但 0 命中：不再二次拼接（恒空，不越层匹配）"""
        assert match_files(str(attempt_root), "attempt-1/reports/*.json") == []

    def test_recursive_pattern_unaffected(self, attempt_root):
        """'**' 递归 pattern 本就能命中，行为不变"""
        matched = match_files(str(attempt_root), "**/*.log")
        assert matched == ["attempt-1/logs/run.log"]

    def test_multi_top_entries_no_auto_prefix(self, tmp_path):
        """解包根下有顶层目录 + 根级散文件（多顶层条目）→ 不自动拼接"""
        root = tmp_path / "multi"
        (root / "attempt-1" / "logs").mkdir(parents=True)
        (root / "attempt-1" / "logs" / "run.log").write_text("x", encoding="utf-8")
        (root / "readme.md").write_text("x", encoding="utf-8")
        assert match_files(str(root), "logs/*.log") == []
        assert match_files(str(root), "*.md") == ["readme.md"]  # 根级匹配不受影响

    def test_index_version_same_as_filesystem(self, attempt_root):
        """预览侧 match_files_in_index 与执行侧 match_files 同源生效（逐模式等价）"""
        index = build_file_index(str(attempt_root))
        for pattern in (
            "reports/allure_results/*.json",   # 相对 attempt 根（自动拼接）
            "attempt-1/reports/*.json",        # 含顶层目录
            "logs/*.log",                      # 相对 attempt 根（自动拼接）
            "**/*.log",                        # 递归
            "*.missing",                       # 两边都空
        ):
            assert match_files_in_index(index, pattern) == match_files(str(attempt_root), pattern), \
                f"pattern={pattern!r} 预览与执行不一致"

    def test_preview_api_endpoint_auto_prefix(self, attempt_root):
        """端到端：match-preview 端点用登记索引预览相对 pattern 也能命中"""
        record = make_record(attempt_root, file_index=build_file_index(str(attempt_root)))
        rows = [SimpleNamespace(index=0, data={"cases_file": "fid-1"})]
        db = make_preview_db(rows=rows, record=record)
        body = HubMatchPreviewRequest(dataset_id="ds-1", field_name="cases_file",
                                      pattern="reports/allure_results/*.json")
        result = match_preview(body, request=fake_request(), db=db)
        assert result["matched_count"] == 2
        assert result["sample_paths"][0].endswith("r1-result.json")
