"""脚本包导入导出 / 文件树在线编辑 / 脚手架 单元测试（统一子进程执行改造新增面）

覆盖：
1. normalize_package：单 py 拒绝（导入仅收 .zip 脚本包与文件夹）/ zip 解包
   （唯一顶层剥离、路径穿越拒绝、超限拒绝）/ 多文件列表（文件夹直选形态）
2. import_package：无 manifest 自动命名、manifest 元数据、缺 main.py 拒绝、
   model.py 强校验（缺失/语法错/缺契约类拒绝，空壳双声明通过）、
   requirements 检测（忽略注释）、风险扫描并入 warnings、同名覆盖语义
3. export_package → import_package 回环
4. 文件树 API：白名单过滤（二进制隐藏）、读取防穿越、保存乐观锁 409、
   main.py 删除/改名保护、新建冲突 409、agent 类型 400
5. from-template：single/folder 两模板生成、重名拒绝、未知模板拒绝
6. API 层：import-package 端点（multipart 多文件 + relative_paths）、
   export-package 端点（zip 附件头）
"""
import asyncio
import io
import json
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db.session import Base
from app.services import pre_judge_script_service as script_service
from app.services import pre_judge_runner
from app.services.pre_judge_script_service import (
    MAX_PACKAGE_FILE_COUNT,
    PreJudgeFileError,
    PreJudgeScriptService,
    normalize_package,
)
from app.api.pre_judge_script_api import (
    PreJudgeScriptFileCreatePayload,
    PreJudgeScriptFileRenamePayload,
    PreJudgeScriptFileSavePayload,
    create_script_file,
    delete_script_file,
    export_script_package,
    import_script_package,
    list_script_files,
    read_script_file,
    rename_script_file,
    update_script_file,
)
from fastapi import UploadFile

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


MAIN_SRC = "def run(stdin):\n    return {'status': 'ok', 'output': {'v': 1}, 'files': {},\n            'summary': '', 'finish_reason': 'completed', 'error': None}\n"
HELPER_SRC = "def build():\n    return {'v': 1}\n"

# 导入包 fixture 通用的 model.py（含两个契约类，满足导入强校验）
MODEL_SRC = (
    "from pydantic import BaseModel, Field\n\n\n"
    "class PreScriptInput(BaseModel):\n"
    "    src: str = Field(..., description='数据源', "
    "json_schema_extra={'value_type': 'local'})\n\n\n"
    "class PreScriptOutput(BaseModel):\n"
    "    hits: int = Field(..., description='命中数')\n"
).encode()


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def isolated_upload_dirs(tmp_path, monkeypatch):
    """脚本存储与试跑产物指向临时目录（导入/模板/迁移都会写文件）"""
    upload_base = tmp_path / "pre_judge_scripts"
    monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", upload_base)
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")
    return upload_base


@pytest.fixture
def service(db_session):
    return PreJudgeScriptService(db_session)


def _request(user=None) -> Request:
    """构造最小 Request，模拟鉴权中间件写入的 request.state.user（dict）。

    W1-4 起脚本写/管理端点校验 super_admin_flag == 1：缺省注入超管用户。
    """
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    if user is None:
        user = {"user_code": "u_admin", "super_admin_flag": 1}
    request.state.user = user
    return request


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class _InlineUploadFile(UploadFile):
    """绕开 anyio 线程池的 UploadFile 子类（仓库既有模式，见 test_script_exec_type.py）"""

    async def read(self, size: int = -1) -> bytes:
        return self.file.read(size)


def _upload(name: str, content: bytes) -> UploadFile:
    return _InlineUploadFile(file=io.BytesIO(content), filename=name)


# ==================== 1. normalize_package ====================

class TestNormalizePackage:
    def test_single_py_rejected(self):
        """导入收口：单 .py 不再支持（明确报错引导使用 zip 包或文件夹）"""
        files_map, source, error = normalize_package([("echo.py", b"print(1)")])
        assert files_map == {} and source == ""
        assert error and "导入仅支持 .zip 脚本包或文件夹" in error
        assert "main.py" in error and "model.py" in error

    def test_single_zip_strips_solo_top_dir(self):
        """GitHub 式 zip：唯一顶层目录剥离，根级出现 main.py"""
        z = _zip_bytes({"pkg-x/main.py": b"A", "pkg-x/utils.py": b"B"})
        files_map, source, error = normalize_package([("pkg-x.zip", z)])
        assert error is None
        assert files_map == {"main.py": b"A", "utils.py": b"B"}
        assert source == "pkg-x"

    def test_zip_root_files_kept(self):
        z = _zip_bytes({"main.py": b"A", "data/map.json": b"{}"})
        files_map, _, error = normalize_package([("m.zip", z)])
        assert error is None and files_map["main.py"] == b"A"

    def test_zip_traversal_rejected(self):
        z = _zip_bytes({"../evil.py": b"x"})
        _, _, error = normalize_package([("bad.zip", z)])
        assert error and "穿越" in error

    def test_zip_absolute_member_rejected(self):
        z = _zip_bytes({"/etc/passwd": b"x"})
        _, _, error = normalize_package([("bad.zip", z)])
        assert error is not None

    def test_zip_bad_archive_rejected(self):
        _, _, error = normalize_package([("bad.zip", b"not a zip")])
        assert error and "zip 解析失败" in error

    def test_multi_file_list(self):
        """文件夹直选形态：按相对路径组装，source 取公共顶层目录名"""
        items = [("proj/main.py", b"A"), ("proj/sub/util.py", b"B")]
        files_map, source, error = normalize_package(items)
        assert error is None
        assert files_map == {"main.py": b"A", "sub/util.py": b"B"}
        assert source == "proj"

    def test_multi_file_traversal_rejected(self):
        _, _, error = normalize_package([("a.py", b"1"), ("../b.py", b"2")])
        assert error and "穿越" in error

    def test_unsupported_single_type(self):
        _, _, error = normalize_package([("readme.txt", b"x")])
        assert error and "单文件导入仅支持 .zip 脚本包" in error

    def test_empty_package(self):
        _, _, error = normalize_package([])
        assert error and "为空" in error


# ==================== 2. import_package ====================

class TestImportPackage:
    def test_import_without_manifest_auto_named(self, service):
        result = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC}, "my cool script")
        assert result["status"] == "ok"
        assert result["name"] == "my_cool_script"  # 危险字符合法化
        row = service.get_script(result["script_id"])
        assert row["exec_type"] == "script" and row["entrypoint"] == "main.py"
        assert (script_service.SCRIPT_UPLOAD_BASE / result["script_id"] / "main.py").read_bytes() \
            == MAIN_SRC.encode()

    def test_import_requires_main_py(self, service):
        result = service.import_package({"utils.py": HELPER_SRC.encode()}, "no_entry")
        assert result["status"] == "error"
        assert "main.py" in result["error"]

    def test_import_with_manifest_metadata(self, service):
        manifest = json.dumps({
            "export_version": "1.0",
            "script": {"name": "manifested", "description": "来自 manifest",
                       "exec_type": "script", "entrypoint": "main.py",
                       "timeout_seconds": 77, "input_fields": {"f": {"value_type": "inline"}},
                       "output_schema": {"v": "number"}},
        }).encode("utf-8")
        result = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC, "manifest.json": manifest}, "pkg")
        assert result["status"] == "ok" and result["name"] == "manifested"
        row = service.get_script(result["script_id"])
        assert row["timeout_seconds"] == 77 and row["description"] == "来自 manifest"

    def test_import_manifest_builtin_rejected(self, service):
        manifest = json.dumps({"script": {"name": "legacy", "exec_type": "builtin"}}).encode()
        result = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC, "manifest.json": manifest}, "pkg")
        assert result["status"] == "error" and "已废除" in result["error"]

    def test_import_requirements_detection(self, service):
        pkg = {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC,
               "requirements.txt": "# 注释\n\nrequests>=2.0\n".encode("utf-8")}
        result = service.import_package(pkg, "with_reqs")
        assert result["status"] == "ok"
        assert any("预装" in w for w in result["warnings"])

    def test_import_requirements_comments_only_no_warning(self, service):
        pkg = {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC,
               "requirements.txt": "# 只有注释\n\n".encode("utf-8")}
        result = service.import_package(pkg, "no_reqs")
        assert result["status"] == "ok"
        assert not any("预装" in w for w in result["warnings"])

    def test_import_risky_py_in_warnings(self, service):
        risky = b"import subprocess\n" + MAIN_SRC.encode()
        result = service.import_package({"main.py": risky, "model.py": MODEL_SRC}, "risky")
        assert result["status"] == "ok"
        assert any("subprocess" in w for w in result["warnings"])

    def test_import_conflict_overwrite_semantics(self, service):
        first = service.import_package({"main.py": b"# v1\n", "model.py": MODEL_SRC}, "dup_pkg")
        assert first["status"] == "ok"
        conflict = service.import_package({"main.py": b"# v2\n", "model.py": MODEL_SRC}, "dup_pkg")
        assert conflict["status"] == "error" and "已存在" in conflict["error"]
        overwritten = service.import_package(
            {"main.py": b"# v2\n", "model.py": MODEL_SRC}, "dup_pkg", overwrite=True)
        assert overwritten["status"] == "ok" and overwritten["script_id"] == first["script_id"]
        # 覆盖 = 目录文件替换
        assert (script_service.SCRIPT_UPLOAD_BASE / first["script_id"] / "main.py").read_bytes() \
            == b"# v2\n"


# ==================== 3. 导出包 → 导入 回环 ====================

class TestExportImportRoundTrip:
    def test_round_trip(self, service):
        manifest = json.dumps({
            "script": {"name": "rt_script", "description": "回环", "exec_type": "script",
                       "entrypoint": "main.py", "timeout_seconds": 66,
                       "input_fields": {"f": {"value_type": "inline"}},
                       "output_schema": {"v": "number"}},
        }).encode()
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC,
             "sub/helper.py": HELPER_SRC.encode(),
             "manifest.json": manifest, "requirements.txt": b"rich\n"},
            "rt")
        assert imported["status"] == "ok"

        name, zip_bytes = service.export_package(imported["script_id"])
        assert name == "rt_script"
        files_map, source, error = normalize_package([(f"{name}.zip", zip_bytes)])
        assert error is None and source == name
        assert set(files_map) >= {"main.py", "model.py", "sub/helper.py", "requirements.txt"}
        manifest_round = json.loads(files_map["manifest.json"])
        assert manifest_round["script"]["timeout_seconds"] == 66
        # model.py 恒为准：导入时已覆盖 manifest 携带的 input_fields（f → src）
        assert manifest_round["script"]["input_fields"] == {
            "src": {"value_type": "local", "description": "数据源"}}

        # 二次导入到同名覆盖 → 文件与元数据一致
        again = service.import_package(files_map, name, overwrite=True)
        assert again["status"] == "ok"
        row = service.get_script(again["script_id"])
        assert row["timeout_seconds"] == 66 and row["file_count"] == 5

    def test_export_non_script_type_rejected(self, service):
        row = service.create_script({"name": "agent_exp", "exec_type": "claude_agent",
                                     "entrypoint": "指令"})
        with pytest.raises(ValueError, match="仅 exec_type=script"):
            service.export_package(row["id"])

    def test_export_missing_dir_rejected(self, service):
        row = service.create_script({"name": "empty_exp", "exec_type": "script"})
        with pytest.raises(ValueError, match="目录不存在"):
            service.export_package(row["id"])


class TestNoiseFileFiltering:
    """运行噪音/系统文件口径：导出 zip、导入落盘、file_count 三处一致排除"""

    def test_export_excludes_noise_files(self, service, isolated_upload_dirs):
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC}, "noise_exp")
        assert imported["status"] == "ok"
        script_dir = isolated_upload_dirs / imported["script_id"]
        pycache = script_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "main.cpython-311.pyc").write_bytes(b"\x00compiled")
        (script_dir / "sub_pyc" / "__pycache__").mkdir(parents=True)
        (script_dir / "sub_pyc" / "__pycache__" / "h.cpython-311.pyc").write_bytes(b"\x00")
        (script_dir / ".DS_Store").write_bytes(b"\x00ds")

        name, zip_bytes = service.export_package(imported["script_id"])
        files_map, _, error = normalize_package([(f"{name}.zip", zip_bytes)])
        assert error is None
        assert "main.py" in files_map
        rels = set(files_map)
        assert not any(r.endswith(".pyc") or r == ".DS_Store" or "__pycache__" in r for r in rels)

    def test_export_manifest_single_entry(self, service):
        """目录内既有 manifest.json（上次导入落盘）时导出 zip 不产生重复条目"""
        manifest = json.dumps({"script": {"name": "dup_mf", "exec_type": "script"}}).encode()
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC, "manifest.json": manifest}, "dup_mf")
        assert imported["status"] == "ok"
        name, zip_bytes = service.export_package(imported["script_id"])
        names = zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()
        assert names.count("manifest.json") == 1, names

    def test_import_filters_noise_files(self, service, isolated_upload_dirs):
        imported = service.import_package({
            "main.py": MAIN_SRC.encode(),
            "model.py": MODEL_SRC,
            "__pycache__/leak.cpython-311.pyc": b"\x00",
            "sub/__pycache__/nested.pyc": b"\x00",
            ".DS_Store": b"\x00",
            "stale.pyo": b"\x00",
        }, "noise_imp")
        assert imported["status"] == "ok"
        script_dir = isolated_upload_dirs / imported["script_id"]
        assert not (script_dir / "__pycache__").exists()
        assert not (script_dir / ".DS_Store").exists()
        assert not (script_dir / "stale.pyo").exists()

    def test_file_count_excludes_noise(self, service, isolated_upload_dirs):
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC,
             "utils.py": HELPER_SRC.encode()}, "noise_cnt")
        script_dir = isolated_upload_dirs / imported["script_id"]
        (script_dir / "__pycache__").mkdir()
        (script_dir / "__pycache__" / "u.cpython-311.pyc").write_bytes(b"\x00")
        row = service.get_script(imported["script_id"])
        assert row["file_count"] == 3


class TestModelPySync:
    """model.py 声明同步（model.py 恒为准：导入时覆盖 manifest 携带值）

    导入通道强校验：包内必须有根级 model.py 且语法可解析、双契约类齐全
    （字段可为空）；上传通道（save_script_package）不要求 model.py。
    """

    def test_import_model_overrides_manifest(self, service):
        """包内 model.py 与 manifest 声明并存 → 以 model.py 解析结果覆盖"""
        manifest = json.dumps({"script": {
            "name": "mp_sync", "exec_type": "script", "entrypoint": "main.py",
            "input_fields": {"legacy": {"value_type": "inline"}},
            "output_schema": {"legacy": "str：旧"}}}).encode()
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC,
             "manifest.json": manifest}, "mp_sync")
        assert imported["status"] == "ok"
        assert imported["schema_sync"]["input_fields"]["src"]["value_type"] == "local"
        row = service.get_script(imported["script_id"])
        assert set(row["input_fields"]) == {"src"}
        assert row["output_schema"] == {"hits": "int：命中数"}

    def test_import_without_model_rejected(self, service):
        """缺根级 model.py → 导入拒绝（报错引导补齐契约声明后重导）"""
        manifest = json.dumps({"script": {
            "name": "mp_nosync", "exec_type": "script", "entrypoint": "main.py",
            "input_fields": {"legacy": {"value_type": "inline"}},
            "output_schema": {"legacy": "str：旧"}}}).encode()
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "manifest.json": manifest}, "mp_nosync")
        assert imported["status"] == "error"
        assert "缺少根级 model.py" in imported["error"]
        assert "PreScriptInput" in imported["error"] and "PreScriptOutput" in imported["error"]

    def test_import_model_syntax_error_rejected(self, service):
        """model.py 语法错误 → 导入拒绝（错误含文件与行号）"""
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": b"def broken(:"}, "mp_syn")
        assert imported["status"] == "error"
        assert "model.py 校验失败" in imported["error"]
        assert "语法错误" in imported["error"]

    @pytest.mark.parametrize("model_src,missing", [
        ("class PreScriptInput(BaseModel):\n    a: str = 'x'\n", "PreScriptOutput"),
        ("class PreScriptOutput(BaseModel):\n    b: int = 0\n", "PreScriptInput"),
    ])
    def test_import_model_missing_class_rejected(self, service, model_src, missing):
        """缺任一契约类 → 导入拒绝并指明缺哪个类"""
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": model_src.encode()}, "mp_miss")
        assert imported["status"] == "error"
        assert "model.py 校验失败" in imported["error"]
        assert missing in imported["error"]

    def test_import_model_empty_shell_ok(self, service):
        """空壳双声明（仅 docstring，无字段）通过；无字段可解析 → schema 不回写为 None 属预期"""
        empty_shell = (
            "from pydantic import BaseModel\n\n\n"
            "class PreScriptInput(BaseModel):\n"
            "    \"\"\"空壳输入声明\"\"\"\n\n\n"
            "class PreScriptOutput(BaseModel):\n"
            "    \"\"\"空壳输出声明\"\"\"\n"
        ).encode()
        manifest = json.dumps({"script": {
            "name": "mp_shell", "exec_type": "script", "entrypoint": "main.py",
            "input_fields": {"legacy": {"value_type": "inline"}}}}).encode()
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": empty_shell,
             "manifest.json": manifest}, "mp_shell")
        assert imported["status"] == "ok"
        assert imported["schema_sync"] == {"input_fields": None, "output_schema": None}
        row = service.get_script(imported["script_id"])
        assert set(row["input_fields"]) == {"legacy"}  # 空壳不覆盖 manifest 携带值

    def test_folder_template_created_with_schemas(self, service):
        """folder 脚手架创建即解析 model.py 回填初始声明（编辑窗/编排直接可用）"""
        created = service.create_from_template("folder", "mp_tpl")
        assert set(created["files"]) >= {"main.py", "model.py", "utils.py"}
        assert created["input_fields"]["msg"]["value_type"] == "inline"
        assert created["input_fields"]["extra_file"]["value_type"] == "local"
        assert created["output_schema"]["words"] == "int：单词数"
        # 声明同步后的 updated_at 已透出（前端创建后立即保存不 409）
        row = service.get_script(created["id"])
        assert created["updated_at"] == row["updated_at"]

    def test_single_template_untouched(self, service):
        created = service.create_from_template("single", "mp_single")
        assert "model.py" not in created["files"]
        assert created.get("input_fields") is None and created.get("output_schema") is None


class TestImportedAs:
    """导入结果语义权威标志 imported_as（D3：前端不再依赖列表缓存推导文案）"""

    CONTRACT = {"name": "ia_script", "exec_type": "script", "entrypoint": "main.py",
                "description": "d3", "timeout_seconds": 60, "is_active": True}

    def test_created(self, service):
        res = service.import_script(dict(self.CONTRACT))
        assert res["imported_as"] == "created"

    def test_updated_with_overwrite(self, service):
        service.import_script(dict(self.CONTRACT))
        res = service.import_script({**self.CONTRACT, "description": "v2"}, overwrite=True)
        assert res["imported_as"] == "updated"

    def test_revived_from_inactive(self, service):
        created = service.import_script(dict(self.CONTRACT))
        service.update_script(created["id"], {"is_active": False})
        res = service.import_script({**self.CONTRACT, "description": "v3"})
        assert res["imported_as"] == "revived"

    def test_package_result_carries_imported_as(self, service):
        first = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC}, "ia_pkg")
        assert first["status"] == "ok" and first["imported_as"] == "created"
        second = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC}, "ia_pkg", overwrite=True)
        assert second["status"] == "ok" and second["imported_as"] == "updated"

    def test_new_create_unpolluted(self, service):
        """imported_as 只附在 import 返回值上，普通新建/查询不携带"""
        row = service.create_script({"name": "plain_create", "exec_type": "script"})
        assert "imported_as" not in row
        assert "imported_as" not in service.get_script(row["id"])


# ==================== 4. 文件树 / 在线编辑 ====================

class TestFileTreeAndEditing:
    @pytest.fixture
    def script_row(self, service):
        result = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC,
             "sub/helper.py": HELPER_SRC.encode(),
             "notes.md": b"# doc\n", "logo.png": b"\x89PNG\r\n\x1a\n"},
            "file_tree")
        assert result["status"] == "ok"
        return result["script_id"]

    def test_list_filters_non_text(self, service, script_row):
        data = service.list_script_files(script_row)
        paths = {f["path"] for f in data["files"]}
        # 二进制 logo.png 不出树
        assert paths == {"main.py", "model.py", "sub/helper.py", "notes.md"}

    def test_read_file(self, service, script_row):
        data = service.read_script_file(script_row, "sub/helper.py")
        assert data["content"] == HELPER_SRC

    def test_read_traversal_404(self, service, script_row):
        with pytest.raises(PreJudgeFileError) as exc:
            service.read_script_file(script_row, "../other/main.py")
        assert exc.value.status_code == 404

    def test_read_binary_rejected(self, service, script_row):
        with pytest.raises(PreJudgeFileError):
            service.read_script_file(script_row, "logo.png")

    def test_save_with_optimistic_lock(self, service, script_row):
        baseline = service.get_script(script_row)["updated_at"]
        saved = service.update_script_file(script_row, "notes.md", "# doc v2\n", baseline)
        assert saved["path"] == "notes.md"
        assert service.read_script_file(script_row, "notes.md")["content"] == "# doc v2\n"
        # 旧时间戳再保存 → 409
        with pytest.raises(PreJudgeFileError) as exc:
            service.update_script_file(script_row, "notes.md", "# v3\n", baseline)
        assert exc.value.status_code == 409
        # 新基线可再保存
        service.update_script_file(script_row, "notes.md", "# v3\n", saved["updated_at"])

    def test_save_py_runs_risk_scan(self, service, script_row):
        baseline = service.get_script(script_row)["updated_at"]
        saved = service.update_script_file(script_row, "sub/helper.py",
                                           "import socket\n" + HELPER_SRC, baseline)
        assert any("socket" in w for w in saved["warnings"])

    def test_save_missing_file_404(self, service, script_row):
        baseline = service.get_script(script_row)["updated_at"]
        with pytest.raises(PreJudgeFileError) as exc:
            service.update_script_file(script_row, "ghost.py", "x", baseline)
        assert exc.value.status_code == 404

    def test_create_and_conflict(self, service, script_row):
        created = service.create_script_file(script_row, "configs/map.yaml", "k: v\n")
        assert created["created"] is True
        assert service.read_script_file(script_row, "configs/map.yaml")["content"] == "k: v\n"
        with pytest.raises(PreJudgeFileError) as exc:
            service.create_script_file(script_row, "configs/map.yaml", "dup")
        assert exc.value.status_code == 409

    def test_create_non_text_rejected(self, service, script_row):
        with pytest.raises(PreJudgeFileError):
            service.create_script_file(script_row, "evil.exe", "x")

    def test_delete_protections(self, service, script_row):
        with pytest.raises(PreJudgeFileError, match="不可删除"):
            service.delete_script_file(script_row, "main.py")
        service.create_script_file(script_row, "configs/map.yaml", "k: v\n")
        deleted = service.delete_script_file(script_row, "configs/map.yaml")  # 普通文件可删
        assert deleted["deleted"] is True
        # 删除后空父目录被清理
        assert not (script_service.SCRIPT_UPLOAD_BASE / script_row / "configs").exists()
        with pytest.raises(PreJudgeFileError) as exc:
            service.delete_script_file(script_row, "no_such.py")
        assert exc.value.status_code == 404

    def test_rename_protections_and_move(self, service, script_row):
        with pytest.raises(PreJudgeFileError, match="不可改名"):
            service.rename_script_file(script_row, "main.py", "entry.py")
        service.create_script_file(script_row, "tmp/a.md", "x")
        moved = service.rename_script_file(script_row, "tmp/a.md", "docs/a.md")
        assert moved["new_path"] == "docs/a.md"
        assert service.read_script_file(script_row, "docs/a.md")["content"] == "x"
        with pytest.raises(PreJudgeFileError) as exc:
            service.rename_script_file(script_row, "docs/a.md", "notes.md")
        assert exc.value.status_code == 409

    def test_agent_type_rejected(self, service):
        row = service.create_script({"name": "agent_files", "exec_type": "claude_agent"})
        with pytest.raises(PreJudgeFileError, match="仅 exec_type=script"):
            service.list_script_files(row["id"])

    def test_list_response_carries_file_count(self, service, script_row):
        item = next(i for i in service.list_scripts() if i["id"] == script_row)
        # main.py/model.py/sub/helper.py/notes.md/logo.png（目录全量口径）
        assert item["file_count"] == 5
        assert item["has_requirements"] is False


# ==================== 5. 脚手架 ====================

class TestFromTemplate:
    def test_single_template(self, service):
        script = service.create_from_template("single", "scaffold_single", description="单文件模板")
        assert script["exec_type"] == "script" and script["entrypoint"] == "main.py"
        script_dir = script_service.SCRIPT_UPLOAD_BASE / script["id"]
        assert (script_dir / "main.py").is_file()
        assert (script_dir / "README.md").is_file()
        main_src = (script_dir / "main.py").read_text(encoding="utf-8")
        assert "def run(stdin" in main_src  # run(stdin) 函数式契约样例

    def test_folder_template(self, service):
        script = service.create_from_template("folder", "scaffold_folder")
        script_dir = script_service.SCRIPT_UPLOAD_BASE / script["id"]
        for rel in ("main.py", "utils.py", "README.md", "requirements.txt"):
            assert (script_dir / rel).is_file(), rel

    def test_duplicate_active_name_rejected(self, service):
        service.create_from_template("single", "dup_tpl")
        with pytest.raises(ValueError, match="已存在"):
            service.create_from_template("single", "dup_tpl")

    def test_unknown_template_rejected(self, service):
        with pytest.raises(ValueError, match="未知模板"):
            service.create_from_template("enterprise", "bad")

    def test_folder_template_runs_via_subprocess(self, service, tmp_path):
        """folder 模板生成即可跑：run(stdin) 契约由 wrapper 承接（模板会写产物到 _run_dir）"""
        script = service.create_from_template("folder", "runnable_tpl")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        result = pre_judge_runner.execute_script(
            {"script_id": script["id"], "name": script["name"], "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 15},
            {"fields": {}, "params": {}, "upstream": {}, "_run_dir": str(run_dir)},
        )
        assert result["status"] in ("ok", "skip"), f"模板应可执行: {result}"
        # output 键由 PreScriptOutput 声明构造（model.py 声明即使用）
        assert set(result["output"]) == {"text", "chars", "words"}


# ==================== 6. API 层 ====================

class TestPackageApi:
    def test_import_package_endpoint(self, db_session):
        """multipart 多文件（文件夹直选形态）+ relative_paths 表单字段"""
        body = asyncio.run(import_script_package(
            request=_request(user={"user_code": "u1", "super_admin_flag": 1}),
            files=[_upload("proj/main.py", MAIN_SRC.encode()),
                   _upload("proj/model.py", MODEL_SRC),
                   _upload("proj/utils.py", HELPER_SRC.encode())],
            relative_paths=["proj/main.py", "proj/model.py", "proj/utils.py"],
            overwrite=False,
            db=db_session,
        ))
        assert len(body["results"]) == 1
        item = body["results"][0]
        assert item["status"] == "ok" and item["name"] == "proj"

    def test_import_package_endpoint_zip(self, db_session):
        z = _zip_bytes({"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC})
        body = asyncio.run(import_script_package(
            request=_request(),
            files=[_upload("echo_pkg.zip", z)],
            relative_paths=["echo_pkg.zip"],
            overwrite=False,
            db=db_session,
        ))
        assert body["results"][0]["status"] == "ok"
        assert body["results"][0]["name"] == "echo_pkg"

    def test_import_package_endpoint_error_item(self, db_session):
        """缺 main.py：HTTP 200 + 逐项 error 明细"""
        z = _zip_bytes({"utils.py": HELPER_SRC.encode()})
        body = asyncio.run(import_script_package(
            request=_request(),
            files=[_upload("bad.zip", z)],
            relative_paths=["bad.zip"],
            overwrite=False,
            db=db_session,
        ))
        assert body["results"][0]["status"] == "error"
        assert "main.py" in body["results"][0]["error"]

    def test_import_package_endpoint_missing_model_error_item(self, db_session):
        """缺根级 model.py：HTTP 200 + 逐项 error 明细（引导补齐契约声明）"""
        z = _zip_bytes({"main.py": MAIN_SRC.encode()})
        body = asyncio.run(import_script_package(
            request=_request(),
            files=[_upload("no_model.zip", z)],
            relative_paths=["no_model.zip"],
            overwrite=False,
            db=db_session,
        ))
        item = body["results"][0]
        assert item["status"] == "error"
        assert "缺少根级 model.py" in item["error"]

    def test_import_package_endpoint_source_name_hint(self, db_session):
        """平铺多文件（前端本地解压 zip 形态）：显式 source_name 表单字段优先于路径推断命名"""
        body = asyncio.run(import_script_package(
            request=_request(user={"user_code": "u1", "super_admin_flag": 1}),
            files=[_upload("main.py", MAIN_SRC.encode()),
                   _upload("model.py", MODEL_SRC)],
            relative_paths=["main.py", "model.py"],
            overwrite=False,
            source_name="valid-pkg",
            db=db_session,
        ))
        item = body["results"][0]
        assert item["status"] == "ok"
        assert item["name"] == "valid-pkg"

    def test_import_package_endpoint_source_name_blank_falls_back(self, db_session):
        """source_name 缺省时保持原推断行为（平铺多文件回退首个文件名 stem）"""
        body = asyncio.run(import_script_package(
            request=_request(user={"user_code": "u1", "super_admin_flag": 1}),
            files=[_upload("main.py", MAIN_SRC.encode()),
                   _upload("model.py", MODEL_SRC)],
            relative_paths=["main.py", "model.py"],
            overwrite=False,
            db=db_session,
        ))
        item = body["results"][0]
        assert item["status"] == "ok"
        assert item["name"] == "main"

    def test_export_package_endpoint(self, db_session, service):
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC}, "exp_api")
        response = asyncio.run(export_script_package(imported["script_id"], db=db_session))
        assert response.media_type == "application/zip"
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment") and "exp_api.zip" in disposition

    def test_file_endpoints_roundtrip(self, db_session, service):
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC}, "file_api")
        script_id = imported["script_id"]
        listing = asyncio.run(list_script_files(script_id, request=_request(), db=db_session))
        assert [f["path"] for f in listing["files"]] == ["main.py", "model.py"]

        baseline = service.get_script(script_id)["updated_at"]
        saved = asyncio.run(update_script_file(
            script_id,
            PreJudgeScriptFileSavePayload(path="main.py", content="# edited\n", updated_at=baseline),
            request=_request(), db=db_session))
        assert saved["updated_at"]

        with pytest.raises(HTTPException) as exc:
            asyncio.run(update_script_file(
                script_id,
                PreJudgeScriptFileSavePayload(path="main.py", content="# stale\n", updated_at=baseline),
                request=_request(), db=db_session))
        assert exc.value.status_code == 409

        created = asyncio.run(create_script_file(
            script_id, PreJudgeScriptFileCreatePayload(path="docs/readme.md", content="hi"),
            request=_request(), db=db_session))
        assert created["created"] is True

        renamed = asyncio.run(rename_script_file(
            script_id, PreJudgeScriptFileRenamePayload(path="docs/readme.md", new_path="docs/intro.md"),
            request=_request(), db=db_session))
        assert renamed["new_path"] == "docs/intro.md"

        with pytest.raises(HTTPException) as exc:
            asyncio.run(delete_script_file(script_id, path="main.py",
                                           request=_request(), db=db_session))
        assert exc.value.status_code == 400

        deleted = asyncio.run(delete_script_file(script_id, path="docs/intro.md",
                                                 request=_request(), db=db_session))
        assert deleted["deleted"] is True

    def test_read_file_endpoint_traversal_404(self, db_session, service):
        imported = service.import_package(
            {"main.py": MAIN_SRC.encode(), "model.py": MODEL_SRC}, "trav_api")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(read_script_file(imported["script_id"], path="../../etc/passwd",
                                         request=_request(), db=db_session))
        assert exc.value.status_code == 404
