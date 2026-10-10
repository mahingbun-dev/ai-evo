"""脚本上传阻断校验链单元测试（阶段E：E1..E5）

覆盖 upload-package 包上传通道与单 .py 上传通道的同一套校验链：
- E1 安全解包 + 包根 main.py + 语法编译（含 zip 穿越/坏包/超限/嵌套根）
- E2 契约冒烟（样例 stdin 全键实跑入口；非 JSON / 空输出 / 超时 / 非零退出码）
- E3 路径规范扫描（/Users/、/home/、C:\\、uploads/ 等硬编码路径阻断）
- E4 AST 风险扫描命中必须 risk_acknowledged；declared_requirements 逐包校验 venv
- E5 单 .py 上传补齐同一套阻断校验
- API 层：upload-package 端点（.zip 校验/管理员门/400 映射）、templates 端点见
  test_script_templates.py

外部依赖边界：冒烟/依赖校验为真实子进程（评测 venv python）；DB 为 SQLite
内存库；上传目录 monkeypatch 到 tmp_path，不污染仓库 uploads/。
API 用例按仓库既有模式直接调用端点协程函数（规避 nest_asyncio 全局补丁污染）。
"""
import asyncio
import io
import json
import logging
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.services import pre_judge_script_service as pjs_service
from app.services.pre_judge_script_service import (
    MAX_PACKAGE_ZIP_SIZE,
    SMOKE_TIMEOUT_SECONDS,
    PreJudgeScriptService,
    build_smoke_stdin,
    scan_hardcoded_paths,
)
from app.api.pre_judge_script_api import upload_script_package


# ---------------- 契约合法/非法脚本样例 ----------------

# 合法脚本：读 stdin、最后一行输出契约 JSON（冒烟可通过）
GOOD_SCRIPT = '''import json, sys

payload = json.load(sys.stdin)
print("[info] running smoke sample")
outputs = {"data_id": payload.get("data_id"), "file_count": len(((payload.get("inputs") or {}).get("hub_pkg") or {}).get("files") or [])}
print(json.dumps({
    "status": "ok",
    "outputs": outputs,
    "message": "done",
    "output": outputs,
    "summary": "ok",
    "files": {},
    "finish_reason": "completed",
    "error": None,
}, ensure_ascii=False))
'''

# 风险脚本：import subprocess（风险扫描命中）但契约输出合法
RISKY_SCRIPT = '''import json, sys
import subprocess  # noqa: F401  风险扫描命中样例

payload = json.load(sys.stdin)
print(json.dumps({"status": "ok", "outputs": {"ran": True}, "message": "risky",
                  "output": {}, "summary": "", "files": {}, "finish_reason": "completed",
                  "error": None}, ensure_ascii=False))
'''


def make_zip_bytes(members: dict) -> bytes:
    """构造 zip 字节（members: 相对路径 → 文本内容）"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def good_package(lib_extra: str = "") -> bytes:
    """合法脚本包：包根 main.py + lib/ 同目录依赖 + README"""
    return make_zip_bytes({
        "main.py": GOOD_SCRIPT,
        "README.md": "# test package\n",
        "lib/mod.py": f"VALUE = 1\n{lib_extra}",
    })


# ---------------- fixtures ----------------

@pytest.fixture(autouse=True)
def upload_base(tmp_path, monkeypatch):
    """上传目录指向临时目录，避免污染仓库 uploads/"""
    base = tmp_path / "uploads_pre_judge_scripts"
    monkeypatch.setattr(pjs_service, "SCRIPT_UPLOAD_BASE", base)
    return base


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def service(db_session):
    return PreJudgeScriptService(db_session)


def _create_script_row(service, name=None) -> str:
    row = service.create_script({"name": name or f"up_{uuid.uuid4().hex[:6]}", "exec_type": "script"})
    return row["id"]


def _request(user=None):
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})
    if user is not None:
        request.state.user = user
    return request


class _InlineUploadFile(UploadFile):
    """绕开 anyio 线程池的 UploadFile 子类（仓库既有模式，见 test_script_exec_type.py）"""

    async def read(self, size: int = -1) -> bytes:
        return self.file.read(size)


def _upload_zip(content: bytes, filename: str = "pkg.zip") -> UploadFile:
    return _InlineUploadFile(file=io.BytesIO(content), filename=filename)


# ---------------- E2 前置：样例 stdin 契约 ----------------

class TestSmokeStdinSample:
    def test_build_smoke_stdin_full_keys(self, tmp_path):
        """样例 stdin 覆盖契约全键，inputs 两模式的文件均真实存在"""
        stdin = build_smoke_stdin(tmp_path)
        assert set(stdin) == {
            "task_id", "data_id", "fields", "inputs", "model_output",
            "session", "params", "upstream", "_run_dir",
        }
        hub_pkg = stdin["inputs"]["hub_pkg"]
        assert hub_pkg["root"] and Path(hub_pkg["root"]).is_dir()
        for item in hub_pkg["files"]:
            assert Path(item["path"]).is_file()
            assert item["rel_path"] and isinstance(item["size"], int)
        hub_all = stdin["inputs"]["hub_pkg_all"]
        assert Path(hub_all["index_path"]).is_file()
        assert Path(stdin["fields"]["note"]).is_file()
        assert stdin["_run_dir"] == str(tmp_path)


# ---------------- E1：zip 包上传（安全解包 + 包根 main.py + 编译） ----------------

class TestPackageUpload:
    def test_upload_package_ok(self, service):
        """合法包：解包到 {script_id}/ 目录（目录式布局）、entrypoint 为目录内 main.py 相对路径"""
        script_id = _create_script_row(service)
        saved = service.save_script_package(script_id, good_package())

        # 目录式布局：entrypoint 为目录内相对路径 main.py，包目录 = SCRIPT_UPLOAD_BASE/{id}
        assert saved["entrypoint"] == "main.py"
        package_dir = (pjs_service.SCRIPT_UPLOAD_BASE / script_id).resolve()
        assert package_dir.is_dir()
        entry = package_dir / "main.py"
        assert entry.is_file()
        assert (package_dir / "lib" / "mod.py").is_file()
        assert saved["file_count"] == 3
        assert saved["static_check"] == []

        row = service.get_script(script_id)
        assert row["entrypoint"] == saved["entrypoint"]
        assert (pjs_service.script_dir_for(script_id) / "lib" / "mod.py").is_file()

    def test_upload_package_overwrite_semantics(self, service):
        """重复上传覆盖：目录内容整体替换"""
        script_id = _create_script_row(service)
        service.save_script_package(script_id, good_package())
        second = service.save_script_package(script_id, good_package(lib_extra="EXTRA = 2\n"))
        package_dir = pjs_service.SCRIPT_UPLOAD_BASE / script_id
        assert (package_dir / "lib" / "mod.py").read_text(encoding="utf-8").endswith("EXTRA = 2\n")

    def test_upload_package_missing_mainpy_rejected(self, service):
        """包根没有 main.py → 中文报错拒绝，且不落正式目录"""
        script_id = _create_script_row(service)
        bad = make_zip_bytes({"run.py": GOOD_SCRIPT, "README.md": "x"})
        with pytest.raises(ValueError, match="main.py"):
            service.save_script_package(script_id, bad)
        assert not (pjs_service.SCRIPT_UPLOAD_BASE / script_id).exists()

    def test_upload_package_nested_single_top_dir(self, service):
        """zip 只含唯一一级子目录且其内有 main.py → 该子目录视为包根（目录式布局落位）"""
        script_id = _create_script_row(service)
        nested = make_zip_bytes({"basic/main.py": GOOD_SCRIPT, "basic/README.md": "x"})
        saved = service.save_script_package(script_id, nested)
        assert saved["entrypoint"] == "main.py"
        package_dir = pjs_service.SCRIPT_UPLOAD_BASE / script_id
        assert (package_dir / "main.py").is_file()
        assert (package_dir / "README.md").is_file()

    def test_upload_package_syntax_error_rejected(self, service):
        """main.py 语法错误 → 拒绝（含文件与行号），正式目录不受影响"""
        script_id = _create_script_row(service)
        # 先成功上传一次，形成正式目录
        service.save_script_package(script_id, good_package())
        bad = make_zip_bytes({"main.py": "def broken(:\n", "lib/mod.py": "X = 1\n"})
        with pytest.raises(ValueError) as exc:
            service.save_script_package(script_id, bad)
        assert "语法错误" in str(exc.value)
        # 旧包仍在（覆盖失败不影响存量）
        package_dir = pjs_service.SCRIPT_UPLOAD_BASE / script_id
        assert (package_dir / "main.py").read_text(encoding="utf-8") == GOOD_SCRIPT

    def test_upload_package_lib_syntax_error_rejected(self, service):
        """包内非入口 .py 语法错误同样拒绝（校验链覆盖包内全部 .py）"""
        script_id = _create_script_row(service)
        bad = make_zip_bytes({"main.py": GOOD_SCRIPT, "lib/broken.py": "def f(:\n"})
        with pytest.raises(ValueError, match="语法错误"):
            service.save_script_package(script_id, bad)

    def test_upload_package_oversize_rejected(self, service):
        script_id = _create_script_row(service)
        with pytest.raises(ValueError, match="过大"):
            service.save_script_package(script_id, b"x" * (MAX_PACKAGE_ZIP_SIZE + 1))

    def test_upload_package_bad_zip_rejected(self, service):
        """非 zip 字节 → 中文报错拒绝"""
        script_id = _create_script_row(service)
        with pytest.raises(ValueError) as exc:
            service.save_script_package(script_id, b"this is not a zip at all")
        assert "解包失败" in str(exc.value)

    def test_upload_package_traversal_member_rejected(self, service):
        """zip 成员含 .. 穿越 → 安全解包拒绝（一个文件都不落盘）"""
        script_id = _create_script_row(service)
        evil = make_zip_bytes({"main.py": GOOD_SCRIPT, "../evil.py": "HACK = 1\n"})
        with pytest.raises(ValueError) as exc:
            service.save_script_package(script_id, evil)
        assert "不安全" in str(exc.value) or "解包失败" in str(exc.value)

    def test_upload_package_rejects_non_script_type(self, service):
        """agent 类型记录拒绝上传（融合后 builtin 已废除，用 claude_agent 代表非 script 类型）"""
        row = service.create_script({"name": "up_pkg_agent", "exec_type": "claude_agent",
                                     "entrypoint": "指令文本"})
        with pytest.raises(ValueError, match="仅 exec_type=script"):
            service.save_script_package(row["id"], good_package())

    def test_upload_package_missing_row(self, service):
        with pytest.raises(ValueError, match="不存在"):
            service.save_script_package("pjs_ghost_pkg", good_package())

    def test_upload_package_switch_single_file_clears_package_dir(self, service):
        """包形态被单文件覆盖：目录清空重建（仅剩 main.py），entrypoint 保持 main.py"""
        script_id = _create_script_row(service)
        service.save_script_package(script_id, good_package())
        saved = service.save_script_file(script_id, GOOD_SCRIPT.encode("utf-8"))
        row = service.get_script(script_id)
        package_dir = pjs_service.SCRIPT_UPLOAD_BASE / script_id
        assert row["entrypoint"] == "main.py" == saved["entrypoint"]
        # 包内残留文件（lib/、README.md）被清理，目录内仅入口 main.py
        assert sorted(p.name for p in package_dir.iterdir()) == ["main.py"]
        assert (package_dir / "main.py").read_text(encoding="utf-8") == GOOD_SCRIPT

    def test_upload_package_without_model_py_allowed(self, service):
        """上传通道（对已有脚本传新版本包）不要求 model.py——导入强校验不外溢到上传链"""
        script_id = _create_script_row(service)
        no_model = make_zip_bytes({"main.py": GOOD_SCRIPT, "README.md": "# pkg\n"})
        saved = service.save_script_package(script_id, no_model)
        assert saved["entrypoint"] == "main.py"
        package_dir = pjs_service.SCRIPT_UPLOAD_BASE / script_id
        assert (package_dir / "main.py").is_file()
        assert not (package_dir / "model.py").exists()


# ---------------- E2：契约冒烟 ----------------

class TestSmokeValidation:
    def test_smoke_not_json_last_line_rejected(self, service):
        """stdout 最后一行非 JSON → 拒绝并回显尾部输出"""
        script_id = _create_script_row(service)
        bad = make_zip_bytes({"main.py": 'import sys\nsys.stdin.read()\nprint("plain text line")\n'})
        with pytest.raises(ValueError) as exc:
            service.save_script_package(script_id, bad)
        assert "不是合法 JSON" in str(exc.value)
        assert "plain text line" in str(exc.value)  # 回显尾部输出

    def test_smoke_empty_output_rejected(self, service):
        script_id = _create_script_row(service)
        with pytest.raises(ValueError, match="标准输出为空"):
            service.save_script_package(
                script_id, make_zip_bytes({"main.py": "import sys\nsys.stdin.read()\n"}))

    def test_smoke_json_array_rejected(self, service):
        """最后一行是 JSON 数组（非对象）→ 拒绝"""
        script_id = _create_script_row(service)
        bad = make_zip_bytes({"main.py": 'import sys, json\nsys.stdin.read()\nprint(json.dumps([1, 2]))\n'})
        with pytest.raises(ValueError, match="对象"):
            service.save_script_package(script_id, bad)

    def test_smoke_timeout_rejected(self, service, monkeypatch):
        """冒烟超时（monkeypatch 时限 1s，脚本睡 3s）→ 拒绝"""
        monkeypatch.setattr(pjs_service, "SMOKE_TIMEOUT_SECONDS", 1)
        script_id = _create_script_row(service)
        sleeper = make_zip_bytes({"main.py": "import sys, time\nsys.stdin.read()\ntime.sleep(3)\n"})
        with pytest.raises(ValueError, match="超时"):
            service.save_script_package(script_id, sleeper)

    def test_smoke_nonzero_exit_rejected(self, service):
        """入口非零退出码 → 拒绝并附 stderr"""
        script_id = _create_script_row(service)
        bad = make_zip_bytes({"main.py": 'import sys\nsys.stdin.read()\nsys.stderr.write("boom")\nsys.exit(3)\n'})
        with pytest.raises(ValueError) as exc:
            service.save_script_package(script_id, bad)
        assert "退出码 3" in str(exc.value)
        assert "boom" in str(exc.value)

    def test_smoke_reads_full_stdin(self, service):
        """入口不读 stdin 直接输出契约 JSON 也可通过（stdin 写入端有 BrokenPipe 容错）"""
        script_id = _create_script_row(service)
        ok = make_zip_bytes({"main.py": 'import json\nprint(json.dumps({"status": "ok", "outputs": {}, "message": "x"}))\n'})
        saved = service.save_script_package(script_id, ok)
        assert saved["entrypoint"].endswith("main.py")


# ---------------- E3：路径规范扫描 ----------------

class TestPathScan:
    @pytest.mark.parametrize("literal,mark", [
        ("/Users/xxx/data.json", "宿主机绝对路径"),
        ("/home/worker/cases.json", "宿主机绝对路径"),
        ('C:\\Users\\xxx\\report.json', "Windows 盘符绝对路径"),
        ("uploads/pre_judge_scripts/pjs_x.py", "平台保留目录"),
        ("/var/data/uploads/extra.txt", "平台保留目录"),
    ])
    def test_hardcoded_paths_rejected(self, service, literal, mark):
        script_id = _create_script_row(service)
        source = (
            "import json, sys\n"
            f"payload = json.load(sys.stdin)\n"
            f"path = {literal!r}\n"
            'print(json.dumps({"status": "ok", "outputs": {}, "message": "x"}))\n'
        )
        with pytest.raises(ValueError) as exc:
            service.save_script_file(script_id, source.encode("utf-8"))
        assert "路径规范扫描未通过" in str(exc.value)
        assert mark in str(exc.value)
        assert "stdin" in str(exc.value)  # 修复提示

    def test_relative_path_allowed(self, service):
        """相对路径拼 _run_dir 合法（不被误伤）"""
        source = (
            "import json, sys, os\n"
            "payload = json.load(sys.stdin)\n"
            "target = os.path.join(payload['_run_dir'], 'out.txt')\n"
            'print(json.dumps({"status": "ok", "outputs": {"p": target}, "message": "x"}))\n'
        )
        script_id = _create_script_row(service)
        saved = service.save_script_file(script_id, source.encode("utf-8"))
        assert saved["static_check"] == []

    def test_scan_hardcoded_paths_reports_file_and_lineno(self):
        sources = {"main.py": "x = '/Users/a/b.txt'\n", "lib/m.py": "y = 'C:\\\\temp\\\\z'\n"}
        hits = scan_hardcoded_paths(sources)
        joined = "\n".join(hits)
        assert len(hits) == 2
        assert "main.py 第1行" in joined
        assert "lib/m.py" in joined

    def test_scan_hardcoded_paths_clean(self):
        assert scan_hardcoded_paths({"main.py": "import os\np = os.path.join('a', 'b')\n"}) == []

    def test_scan_hardcoded_paths_fstring_literal_part(self):
        """f-string 字面量分片中的路径同样命中"""
        sources = {"main.py": "p = f'/Users/base/{name}.txt'\n"}
        assert scan_hardcoded_paths(sources)


# ---------------- E4：风险声明 + 依赖校验 ----------------

class TestRiskAndRequirements:
    def test_risk_hit_without_ack_rejected(self, service):
        """风险扫描命中且未带声明 → 拒存（错误含命中清单）"""
        script_id = _create_script_row(service)
        with pytest.raises(ValueError) as exc:
            service.save_script_package(script_id, make_zip_bytes({"main.py": RISKY_SCRIPT}))
        assert "风险声明" in str(exc.value)
        assert "subprocess" in str(exc.value)

    def test_risk_hit_with_ack_saved(self, service, caplog):
        """带风险声明 → 保存成功：声明文本落库、提示透出并记日志"""
        script_id = _create_script_row(service)
        ack = "需要 subprocess 调用内部打包工具核对版本，输入路径全部来自 stdin"
        with caplog.at_level(logging.WARNING, logger="app.services.pre_judge_script_service"):
            saved = service.save_script_package(
                script_id, make_zip_bytes({"main.py": RISKY_SCRIPT}), risk_acknowledged=ack)
        assert any("subprocess" in w for w in saved["static_check"])
        assert any("静态检查提示" in r.getMessage() for r in caplog.records)
        assert service.get_script(script_id)["risk_acknowledged"] == ack

    def test_risk_ack_blank_is_rejected(self, service):
        """纯空白声明视同未声明"""
        script_id = _create_script_row(service)
        with pytest.raises(ValueError, match="风险声明"):
            service.save_script_package(
                script_id, make_zip_bytes({"main.py": RISKY_SCRIPT}), risk_acknowledged="   ")

    def test_requirements_missing_pkg_rejected(self, service):
        """依赖缺失：拒绝并附缺失清单与 pip install 命令（不自动安装）"""
        script_id = _create_script_row(service)
        missing = "definitely-not-a-real-pkg-xyz"
        with pytest.raises(ValueError) as exc:
            service.save_script_file(
                script_id, GOOD_SCRIPT.encode("utf-8"),
                declared_requirements=[missing],
            )
        assert missing in str(exc.value)
        assert "pip install" in str(exc.value)

    def test_requirements_installed_pkg_ok(self, service):
        """依赖已安装（评测 venv 内真实存在 pytest）→ 通过并落库"""
        script_id = _create_script_row(service)
        saved = service.save_script_file(
            script_id, GOOD_SCRIPT.encode("utf-8"), declared_requirements=["pytest>=7"])
        assert saved["declared_requirements"] == ["pytest>=7"]
        assert service.get_script(script_id)["declared_requirements"] == ["pytest>=7"]

    def test_requirements_version_spec_base_name(self, service):
        """版本约束剥离包名后校验：requests>=2.31 按 requests 探测"""
        script_id = _create_script_row(service)
        with pytest.raises(ValueError) as exc:
            service.save_script_file(
                script_id, GOOD_SCRIPT.encode("utf-8"),
                declared_requirements=["no-such-pkg-abc>=1.0"],
            )
        assert "no-such-pkg-abc" in str(exc.value)
        assert "no-such-pkg-abc>=1.0" not in str(exc.value).split("pip install")[-1]

    def test_requirements_json_string_and_comma_separated(self, service):
        """依赖声明支持 JSON 数组字符串与逗号分隔字符串（用评测 venv 已装包）"""
        script_id = _create_script_row(service)
        saved = service.save_script_file(
            script_id, GOOD_SCRIPT.encode("utf-8"), declared_requirements='["pytest"]')
        assert saved["declared_requirements"] == ["pytest"]
        saved2 = service.save_script_file(
            script_id, GOOD_SCRIPT.encode("utf-8"), declared_requirements="pytest, sqlalchemy")
        assert saved2["declared_requirements"] == ["pytest", "sqlalchemy"]

    def test_requirements_invalid_type_rejected(self, service):
        script_id = _create_script_row(service)
        with pytest.raises(ValueError, match="declared_requirements"):
            service.save_script_file(
                script_id, GOOD_SCRIPT.encode("utf-8"), declared_requirements=12345)


# ---------------- E5：单 .py 上传同一套阻断校验 ----------------

class TestSingleFileChain:
    def test_syntax_error_rejected(self, service):
        script_id = _create_script_row(service)
        with pytest.raises(ValueError, match="语法错误"):
            service.save_script_file(script_id, b"def broken(:\n")

    def test_smoke_rejected(self, service):
        script_id = _create_script_row(service)
        with pytest.raises(ValueError, match="不是合法 JSON"):
            service.save_script_file(script_id, b'import sys\nsys.stdin.read()\nprint("no json")\n')

    def test_path_scan_rejected(self, service):
        script_id = _create_script_row(service)
        source = "import json, sys\np = '/Users/x/y.json'\nsys.stdin.read()\nprint(json.dumps({'status': 'ok', 'outputs': {}, 'message': 'x'}))\n"
        with pytest.raises(ValueError, match="路径规范扫描未通过"):
            service.save_script_file(script_id, source.encode("utf-8"))

    def test_risk_without_ack_rejected_then_with_ack_ok(self, service):
        script_id = _create_script_row(service)
        with pytest.raises(ValueError, match="风险声明"):
            service.save_script_file(script_id, RISKY_SCRIPT.encode("utf-8"))
        saved = service.save_script_file(
            script_id, RISKY_SCRIPT.encode("utf-8"), risk_acknowledged="需 subprocess 复查打包产物")
        assert saved["risk_acknowledged"].startswith("需 subprocess")
        assert any("subprocess" in w for w in saved["static_check"])

    def test_requirements_missing_rejected(self, service):
        script_id = _create_script_row(service)
        with pytest.raises(ValueError, match="pip install"):
            service.save_script_file(
                script_id, GOOD_SCRIPT.encode("utf-8"),
                declared_requirements=["no-such-pkg-qqq"])

    def test_rejected_upload_not_persisted(self, service):
        """校验失败不落正式文件、不回写 entrypoint（冒烟副本已清理）"""
        script_id = _create_script_row(service)
        with pytest.raises(ValueError):
            service.save_script_file(script_id, b"import sys\nsys.stdin.read()\nprint('bad')\n")
        assert service.get_script(script_id)["entrypoint"] is None
        leftovers = list(pjs_service.SCRIPT_UPLOAD_BASE.glob(f"{script_id}*"))
        assert leftovers == []


# ---------------- API 层：upload-package 端点 ----------------

class TestUploadPackageApi:
    def _create_script_row(self, db_session, name=None) -> str:
        from app.api.pre_judge_script_api import PreJudgeScriptPayload, create_script

        payload = PreJudgeScriptPayload(name=name or f"api_pkg_{uuid.uuid4().hex[:6]}",
                                        exec_type="script", timeout_seconds=30)
        created = asyncio.run(create_script(payload, request=_request(), db=db_session))
        return created["id"]

    def test_api_upload_package_ok(self, db_session):
        script_id = self._create_script_row(db_session)
        saved = asyncio.run(upload_script_package(
            script_id, request=_request(), file=_upload_zip(good_package()),
            risk_acknowledged=None, declared_requirements=None, db=db_session))
        assert saved["script_id"] == script_id
        assert saved["entrypoint"] == "main.py"
        assert saved["file_count"] == 3

    def test_api_rejects_non_zip_extension(self, db_session):
        script_id = self._create_script_row(db_session)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_package(
                script_id, request=_request(), file=_upload_zip(good_package(), "pkg.tar"),
                db=db_session))
        assert exc.value.status_code == 400
        assert "zip" in str(exc.value.detail)

    def test_api_admin_gate(self, db_session):
        script_id = self._create_script_row(db_session)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_package(
                script_id, request=_request(user={"user_code": "u", "is_admin": False}),
                file=_upload_zip(good_package()), db=db_session))
        assert exc.value.status_code == 403

    def test_api_validation_error_maps_400(self, db_session):
        """校验链失败 → 400 + 中文 detail（缺 main.py）"""
        script_id = self._create_script_row(db_session)
        bad = make_zip_bytes({"run.py": GOOD_SCRIPT})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_package(
                script_id, request=_request(), file=_upload_zip(bad),
                risk_acknowledged=None, declared_requirements=None, db=db_session))
        assert exc.value.status_code == 400
        assert "main.py" in str(exc.value.detail)

    def test_api_unknown_script_400(self, db_session):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_package(
                "pjs_missing_pkg", request=_request(), file=_upload_zip(good_package()),
                risk_acknowledged=None, declared_requirements=None, db=db_session))
        assert exc.value.status_code == 400

    def test_api_form_fields_passthrough(self, db_session):
        """表单字段（风险声明/依赖声明）透传到 service 并落库"""
        script_id = self._create_script_row(db_session)
        saved = asyncio.run(upload_script_package(
            script_id, request=_request(), file=_upload_zip(make_zip_bytes({"main.py": RISKY_SCRIPT})),
            risk_acknowledged="声明：需 subprocess", declared_requirements='["pytest"]', db=db_session))
        assert saved["risk_acknowledged"] == "声明：需 subprocess"
        assert saved["declared_requirements"] == ["pytest"]
