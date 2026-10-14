"""exec_type=script（子进程脚本类型）单元测试

覆盖（设计文档 docs/design/pre-judge-scripts.md 第四/六节 script 类型 +
统一子进程执行改造）：
- 真子进程跑 echo 脚本样例：stdin JSON 注入、标准输出最后一行 JSON 解析、
  噪声行容错（只取最后一行）
- 统一 wrapper（app/services/pre_judge_entry.py）执行：脚本目录 + main.py 入口
- cwd 隔离到 uploads/script_runs/{task_id}/{data_id}/{script_id}/
- 超时（timeout_seconds）kill
- 非法输出：最后一行非 JSON / JSON 非对象 / 非法 status / 非零退出码
- stdout 上限截断：只读尾部仍能解析最后一行 JSON
- 运行前静态检查：提示写入结果 static_check 并记日志，不做硬拦截
- 上传链路：service 存 uploads/pre_judge_scripts/{id}/main.py 回写 entrypoint=main.py
  + API multipart 上传（管理员校验 / .py 校验 / 覆盖语义）
- 编排集成：DB 脚本行 exec_type=script 走 run_pre_scripts，串联 upstream

外部依赖边界：脚本子进程为真实进程（echo/timeout 样例要求），数据库与上传目录
均隔离到临时路径（SQLite 内存库 + tmp_path monkeypatch）。
"""
import asyncio
import io
import json
import logging
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.pre_judge_models import PreJudgeScript
from app.services import pre_judge_runner, pre_judge_script_service
from app.services.pre_judge_script_service import (
    MAX_SCRIPT_FILE_SIZE,
    PreJudgeScriptService,
    scan_script_risks,
)
from app.api.pre_judge_script_api import (
    PreJudgeScriptDebugPayload,
    PreJudgeScriptPayload,
    create_script,
    debug_script,
    upload_script_file,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------- 脚本样例 ----------------

# echo 样例：先打印非 JSON 噪声行，最后一行才是标准输出 JSON（验证「取最后一行」契约）
ECHO_SCRIPT = '''
import json, sys

payload = json.load(sys.stdin)
print("loading input ...")
print("[info] data_id =", payload.get("data_id"))
note = ""
msg_path = (payload.get("fields") or {}).get("msg")
if msg_path:
    with open(msg_path, encoding="utf-8") as f:
        note = f.read()
print(json.dumps({
    "status": "ok",
    "output": {"echo": note, "params_strict": (payload.get("params") or {}).get("strict")},
    "files": {},
    "summary": "echoed",
    "finish_reason": "completed",
    "error": None,
}, ensure_ascii=False))
'''

# cwd 隔离样例：相对路径写文件，验证 cwd 落在 script_id 子目录
CWD_MARKER_SCRIPT = '''
import json, sys

payload = json.load(sys.stdin)
with open("cwd_marker.txt", "w", encoding="utf-8") as f:
    f.write("written-in-cwd")
print(json.dumps({"status": "ok", "output": {"done": True}, "files": {},
                  "summary": "", "finish_reason": "completed", "error": None}))
'''

# 超时样例：睡 5 秒不输出
SLEEP_SCRIPT = '''
import time
time.sleep(5)
'''

# 风险声明样例：import subprocess（不实际调用），验证静态检查提示但不硬拦截
RISKY_OK_SCRIPT = '''
import json, sys
import subprocess  # noqa: F401  仅声明
import os          # noqa: F401  仅声明

print(json.dumps({"status": "ok", "output": {"ran": True}, "files": {},
                  "summary": "", "finish_reason": "completed", "error": None}))
'''


# ---------------- fixtures 与辅助 ----------------

@pytest.fixture(autouse=True)
def run_base(tmp_path, monkeypatch):
    """产物根目录与上传目录指向临时目录，避免污染仓库 uploads/"""
    base = tmp_path / "script_runs"
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
    monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE",
                        tmp_path / "uploads_pre_judge_scripts")
    return base


def _write_script(tmp_path, source, name="script_under_test.py") -> str:
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    return str(target)


def _write_pkg_script(source, script_id="pjs_scriptcase", name="main.py") -> str:
    """把脚本源码写进脚本包目录（W1-2：entrypoint 只接受包内相对路径），
    返回包内相对入口路径"""
    base = Path(pre_judge_script_service.SCRIPT_UPLOAD_BASE)
    pkg = base / script_id
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / name).write_text(source, encoding="utf-8")
    return name


def _script_def(entrypoint, timeout_seconds=30, script_id="pjs_scriptcase"):
    return {"script_id": script_id, "name": "script_case", "exec_type": "script",
            "entrypoint": entrypoint, "timeout_seconds": timeout_seconds}


def _stdin(**overrides):
    base = {"task_id": "task_script_x", "data_id": "data_1", "fields": {},
            "model_output": None, "session": None, "params": {}, "upstream": {}}
    base.update(overrides)
    return base


def _task(pre_scripts):
    return SimpleNamespace(id="task_script_x", pre_scripts=pre_scripts)


def _dataset_item(**extra):
    return {"id": "data_1", "data": {"note": "hello", **extra}}


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


def _request(user=None):
    """构造最小 Request，模拟鉴权中间件写入的 request.state.user（仓库既有模式）。

    W1-4 起写操作校验 super_admin_flag == 1：缺省注入超管用户；
    非 403 语义测试用 user 显式传普通用户/无标志用户。
    """
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})
    if user is None:
        user = {"user_code": "u_admin", "super_admin_flag": 1}
    request.state.user = user
    return request


class _InlineUploadFile(UploadFile):
    """绕开 anyio run_in_threadpool 的 UploadFile 子类

    存量测试（如 test_binary_classification_execution）引入 nest_asyncio 全局补丁后，
    真实 UploadFile.read()（anyio 线程池）在端点协程直调场景抛 NoEventLoopError
    （仓库已知跨文件污染，见 test_pre_judge_script_service.py 头注的规避说明）。
    覆写 read() 为同步读底层文件对象，接口与 UploadFile 完全一致；
    multipart 解析属 FastAPI 框架行为，端点逻辑（管理员校验/.py 校验/存储/回写/
    静态检查）不受影响。
    """

    async def read(self, size: int = -1) -> bytes:
        return self.file.read(size)


def _upload_file(source: str, filename: str = "echo.py") -> UploadFile:
    return _InlineUploadFile(file=io.BytesIO(source.encode("utf-8")), filename=filename)


# ---------------- 子进程执行：echo / cwd / 截断 ----------------

class TestScriptSubprocess:
    def test_echo_script_via_real_subprocess(self, tmp_path):
        """真子进程跑 echo 样例：stdin JSON 注入、噪声行容错、最后一行 JSON 解析"""
        entrypoint = _write_pkg_script(ECHO_SCRIPT)
        fields = pre_judge_runner.write_inline_value("msg", "hello", tmp_path)
        result = pre_judge_runner.execute_script(
            _script_def(entrypoint), _stdin(fields={"msg": fields}, params={"strict": True}))

        assert result["status"] == "ok"
        assert result["finish_reason"] == "completed"
        assert result["output"] == {"echo": "hello", "params_strict": True}
        assert result["summary"] == "echoed"
        assert result["error"] is None
        assert isinstance(result["duration_ms"], int)
        assert result["static_check"] == []

    def test_cwd_isolated_to_script_dir(self, tmp_path, run_base):
        """cwd 隔离：脚本相对路径写盘落在 uploads/script_runs/{task}/{data}/{script_id}/"""
        entrypoint = _write_pkg_script(CWD_MARKER_SCRIPT)
        result = pre_judge_runner.execute_script(_script_def(entrypoint), _stdin())

        assert result["status"] == "ok"
        script_cwd = run_base / "task_script_x" / "data_1" / "pjs_scriptcase"
        assert (script_cwd / "cwd_marker.txt").is_file()
        assert (script_cwd / "cwd_marker.txt").read_text(encoding="utf-8") == "written-in-cwd"
        # stdout/stderr 重定向文件也在该目录
        assert (script_cwd / "stdout.log").is_file()

    def test_absolute_entrypoint_rejected(self, tmp_path, monkeypatch):
        """W1-2：绝对路径 entrypoint 一律拒绝执行（历史上传曾可指向服务器任意 .py）"""
        entry_abs = _write_script(tmp_path, CWD_MARKER_SCRIPT)
        result = pre_judge_runner.execute_script(_script_def(entry_abs), _stdin())
        assert result["status"] == "fail"
        assert "绝对路径" in result["error"]
        assert "脚本入口不存在" not in result["error"]

    def test_cwd_relative_entrypoint_rejected(self, tmp_path, monkeypatch):
        """W1-2：相对主进程 cwd 的历史兼容分支已删除——entrypoint 只在脚本包内解析"""
        script_dir = tmp_path / "uploads_pre_judge_scripts"
        script_dir.mkdir(parents=True, exist_ok=True)
        (script_dir / "rel_case.py").write_text(CWD_MARKER_SCRIPT, encoding="utf-8")
        monkeypatch.chdir(tmp_path)  # 历史缺陷①形态：cwd 下该相对路径确实存在

        result = pre_judge_runner.execute_script(
            _script_def("uploads_pre_judge_scripts/rel_case.py"), _stdin())
        assert result["status"] == "fail", "cwd 相对 entrypoint 不应再被执行"
        assert "脚本入口不存在" in (result["error"] or "")
        # 入口校验失败在建运行目录之前发生：隔离目录未被创建（脚本根本没跑）
        assert not (tmp_path / "script_runs" / "task_script_x").exists()

    def test_traversal_entrypoint_rejected(self, tmp_path, monkeypatch):
        """W1-2：含 .. 穿越的 entrypoint 一律拒绝（不得解析到脚本包之外）"""
        base = Path(pre_judge_script_service.SCRIPT_UPLOAD_BASE)
        outside = base.parent / "outside_dir"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "evil.py").write_text(CWD_MARKER_SCRIPT, encoding="utf-8")
        evil_rel = os.path.relpath(outside / "evil.py", base / "pjs_scriptcase")

        result = pre_judge_runner.execute_script(_script_def(evil_rel), _stdin())
        assert result["status"] == "fail"
        assert ".. 穿越" in result["error"] or "绝对路径" in result["error"]

    def test_package_entrypoint_missing_file_fails(self, tmp_path, monkeypatch):
        """包内相对 entrypoint 指向不存在的文件 → fail（入口不存在）"""
        monkeypatch.chdir(tmp_path)
        result = pre_judge_runner.execute_script(
            _script_def("uploads_pre_judge_scripts/ghost.py"), _stdin())
        assert result["status"] == "fail"
        assert "脚本入口不存在" in result["error"]

    def test_stdout_truncated_but_last_line_parsed(self, tmp_path, monkeypatch):
        """stdout 上限截断：大量噪声输出只读尾部，最后一行 JSON 仍可解析"""
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_STDOUT_LIMIT", 4096)
        noisy = ECHO_SCRIPT.replace('print("loading input ...")',
                                    '\n'.join(f'print("noise-line-{i:04d}-' + "x" * 32 + '")'
                                              for i in range(2000)))
        entrypoint = _write_pkg_script(noisy)
        result = pre_judge_runner.execute_script(_script_def(entrypoint), _stdin())
        assert result["status"] == "ok"
        assert result["output"]["echo"] == ""

    def test_missing_entrypoint_fails(self):
        result = pre_judge_runner.execute_script(_script_def(None), _stdin())
        assert result["status"] == "fail"
        assert "未上传脚本文件" in result["error"]

    def test_entrypoint_file_missing_fails(self):
        result = pre_judge_runner.execute_script(
            _script_def(str(Path("_nonexistent_") / "no_such.py")), _stdin())
        assert result["status"] == "fail"
        assert "脚本入口不存在" in result["error"]


# ---------------- 超时与非法输出 ----------------

class TestTimeoutAndIllegalOutput:
    def test_timeout_kills_script(self, tmp_path):
        """超时 kill：timeout_seconds 到点终止脚本，返回 fail/timeout"""
        entrypoint = _write_pkg_script(SLEEP_SCRIPT)
        result = pre_judge_runner.execute_script(
            _script_def(entrypoint, timeout_seconds=0.5), _stdin())

        assert result["status"] == "fail"
        assert result["finish_reason"] == "timeout"
        assert "超时" in result["error"]
        assert result["duration_ms"] < 4000  # 未真等满 5 秒

    def test_illegal_output_not_json(self, tmp_path):
        """非法输出：最后一行不是 JSON → fail"""
        bad = CWD_MARKER_SCRIPT.split("print(json.dumps")[0] + 'print("definitely not json")\n'
        entrypoint = _write_pkg_script(bad)
        result = pre_judge_runner.execute_script(_script_def(entrypoint), _stdin())
        assert result["status"] == "fail"
        assert "最后一行不是合法 JSON" in result["error"]
        assert result["finish_reason"] == "error"

    def test_illegal_output_not_json_object(self, tmp_path):
        """非法输出：最后一行是 JSON 数组（非对象）→ fail"""
        bad = CWD_MARKER_SCRIPT.split("payload = json.load")[0] + 'print(json.dumps([1, 2]))\n'
        entrypoint = _write_pkg_script(bad)
        result = pre_judge_runner.execute_script(_script_def(entrypoint), _stdin())
        assert result["status"] == "fail"
        assert "JSON 对象" in result["error"]

    def test_illegal_status_normalized_to_fail(self, tmp_path):
        """非法输出：status 值不在 ok/skip/fail → 归一为 fail"""
        bad = CWD_MARKER_SCRIPT.replace('"status": "ok"', '"status": "weird"')
        entrypoint = _write_pkg_script(bad)
        result = pre_judge_runner.execute_script(_script_def(entrypoint), _stdin())
        assert result["status"] == "fail"
        assert "非法 status 值" in result["error"]

    def test_nonzero_exit_code_fails_with_stderr(self, tmp_path):
        """非零退出码：fail 且错误信息带退出码与 stderr 片段"""
        bad = 'import sys\nsys.stderr.write("boom-detail")\nsys.exit(3)\n'
        entrypoint = _write_pkg_script(bad)
        result = pre_judge_runner.execute_script(_script_def(entrypoint), _stdin())
        assert result["status"] == "fail"
        assert "3" in result["error"]
        assert "boom-detail" in result["error"]

    def test_empty_stdout_fails(self, tmp_path):
        """无输出：fail（未找到最后一行 JSON）"""
        entrypoint = _write_pkg_script("print()\n")
        result = pre_judge_runner.execute_script(_script_def(entrypoint), _stdin())
        assert result["status"] == "fail"
        assert "标准输出为空" in result["error"]


# ---------------- 运行前静态检查（提示级） ----------------

class TestStaticCheck:
    def test_risky_script_executes_with_warnings(self, tmp_path, caplog):
        """静态检查提示级：风险脚本仍正常执行，提示写入 static_check 并记日志"""
        entrypoint = _write_pkg_script(RISKY_OK_SCRIPT)
        with caplog.at_level(logging.WARNING, logger="app.services.pre_judge_runner"):
            result = pre_judge_runner.execute_script(_script_def(entrypoint), _stdin())

        assert result["status"] == "ok"  # 不硬拦截
        checks = "\n".join(result["static_check"])
        assert "subprocess" in checks
        for record in caplog.records:
            if "静态检查提示" in record.getMessage():
                assert "subprocess" in record.getMessage()
                break
        else:
            pytest.fail("静态检查提示未写入日志")

    def test_scan_script_risks_flags_dangerous_calls(self):
        source = (
            "import subprocess\n"
            "from shutil import rmtree\n"
            "import os\n"
            "os.system('ls')\n"
            "os.remove('x')\n"
            "rmtree('y')\n"
            "eval('1+1')\n"
        )
        warnings = scan_script_risks(source)
        joined = "\n".join(warnings)
        assert "import subprocess" in joined
        assert "os.system()" in joined
        assert "os.remove()" in joined
        assert "rmtree()" in joined
        assert "eval()" in joined

    def test_scan_script_risks_clean_source(self):
        assert scan_script_risks("import json\nprint(json.dumps({'a': 1}))\n") == []

    def test_scan_script_risks_syntax_error_hint_only(self):
        warnings = scan_script_risks("def broken(:\n")
        assert len(warnings) == 1
        assert "语法" in warnings[0]


# ---------------- 上传链路：service + API ----------------

class TestScriptUploadService:
    def test_save_script_file_sets_entrypoint(self, service):
        row = service.create_script({"name": "up_case", "exec_type": "script"})
        saved = service.save_script_file(row["id"], ECHO_SCRIPT.encode("utf-8"))

        # 目录式存储：上传落位 {id}/main.py，entrypoint 回写为目录内相对路径
        # （执行时按 SCRIPT_UPLOAD_BASE/{id}/ 解析，子进程 cwd 隔离不影响）
        assert saved["entrypoint"] == "main.py"
        saved_path = (pre_judge_script_service.SCRIPT_UPLOAD_BASE / row["id"] / "main.py")
        assert saved_path.read_text(encoding="utf-8") == ECHO_SCRIPT
        assert saved["file_size"] == len(ECHO_SCRIPT.encode("utf-8"))
        assert saved["static_check"] == []
        # entrypoint 已回写进记录
        assert service.get_script(row["id"])["entrypoint"] == "main.py"

    def test_save_script_file_with_default_relative_base_runs(
        self, tmp_path, monkeypatch, db_session
    ):
        """真实默认形态回归：SCRIPT_UPLOAD_BASE 为相对路径且主进程 cwd=项目根时，
        上传落位 {cwd}/{base}/{id}/main.py、entrypoint=main.py，执行可解析（目录式
        相对语义取代二期「回写绝对路径」方案）。

        既有 run_base fixture 把上传目录 monkeypatch 成绝对 tmp_path，
        故此处还原真实默认（相对 base）+ chdir 到临时目录验证（不污染仓库 uploads/）。
        """
        monkeypatch.chdir(tmp_path)  # 模拟主进程 cwd
        monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE",
                            Path("uploads/pre_judge_scripts"))
        service = PreJudgeScriptService(db_session)
        row = service.create_script({"name": f"up_rel_{uuid.uuid4().hex[:6]}", "exec_type": "script"})
        saved = service.save_script_file(row["id"], ECHO_SCRIPT.encode("utf-8"))

        assert saved["entrypoint"] == "main.py"
        saved_path = tmp_path / "uploads/pre_judge_scripts" / row["id"] / "main.py"
        assert saved_path.read_text(encoding="utf-8") == ECHO_SCRIPT
        assert service.get_script(row["id"])["entrypoint"] == "main.py"
        # 相对 base 下执行可解析（目录式相对 entrypoint）
        result = pre_judge_runner.execute_script(
            _script_def("main.py", script_id=row["id"]), _stdin())
        assert result["status"] == "ok", f"相对 base 下应可执行: {result}"

    def test_save_script_file_overwrite_semantics(self, service):
        """覆盖语义回归（阶段E起保存走阻断校验链，覆盖内容也必须是契约合法脚本）"""
        content_a = 'import json, sys\nsys.stdin.read()\nprint(json.dumps({"status": "ok", "outputs": {"v": "a"}}))\n'
        content_b = 'import json, sys\nsys.stdin.read()\nprint(json.dumps({"status": "ok", "outputs": {"v": "b"}}))\n'
        row = service.create_script({"name": "up_overwrite", "exec_type": "script"})
        first = service.save_script_file(row["id"], content_a.encode("utf-8"))
        second = service.save_script_file(row["id"], content_b.encode("utf-8"))
        assert first["entrypoint"] == second["entrypoint"] == "main.py"
        saved = pre_judge_script_service.script_dir_for(row["id"]) / "main.py"
        assert saved.read_text(encoding="utf-8") == content_b

    def test_save_script_file_rejects_non_script_type(self, service):
        row = service.create_script({"name": "up_agent", "exec_type": "claude_agent",
                                     "entrypoint": "指令文本"})
        with pytest.raises(ValueError, match="仅 exec_type=script"):
            service.save_script_file(row["id"], b"x = 1\n")

    def test_save_script_file_missing_row(self, service):
        with pytest.raises(ValueError, match="不存在"):
            service.save_script_file("pjs_ghost", b"x = 1\n")

    def test_save_script_file_oversize_rejected(self, service):
        row = service.create_script({"name": "up_big", "exec_type": "script"})
        with pytest.raises(ValueError, match="过大"):
            service.save_script_file(row["id"], b"x" * (MAX_SCRIPT_FILE_SIZE + 1))

    def test_save_script_file_returns_and_logs_static_check(self, service, caplog):
        """风险命中 + 携带风险声明：提示随返回值透出并记日志（阶段E起缺声明会被拒，
        纯拒绝分支见 test_script_upload_validation.py）"""
        source = (
            "import json, sys\n"
            "import socket  # noqa: F401\n"
            'sys.stdin.read()\n'
            'print(json.dumps({"status": "ok", "outputs": {"ran": True}}))\n'
        )
        row = service.create_script({"name": "up_risky", "exec_type": "script"})
        with caplog.at_level(logging.WARNING, logger="app.services.pre_judge_script_service"):
            saved = service.save_script_file(row["id"], source.encode("utf-8"),
                                             risk_acknowledged="需 socket 探测本机端口连通性")
        assert any("socket" in w for w in saved["static_check"])
        assert any("socket" in r.getMessage() for r in caplog.records)


class TestScriptUploadApi:
    """端点协程直调（仓库模式，规避 nest_asyncio × TestClient 冲突）"""

    def _create_script_row(self, db_session, exec_type="script", name=None):
        overrides = {"entrypoint": "指令文本"} if exec_type != "script" else {}
        payload = PreJudgeScriptPayload(name=name or f"api_up_{uuid.uuid4().hex[:6]}",
                                        exec_type=exec_type, timeout_seconds=30, **overrides)
        return asyncio.run(create_script(payload, request=_request(), db=db_session))

    def test_upload_flow_then_debug_run(self, db_session):
        """上传 → 目录式落位 + entrypoint 回写 → debug 试跑真子进程 → 返回含 static_check"""
        created = self._create_script_row(db_session)
        saved = asyncio.run(upload_script_file(
            created["id"], request=_request(), file=_upload_file(ECHO_SCRIPT),
            risk_acknowledged=None, declared_requirements=None, db=db_session))
        assert saved["script_id"] == created["id"]
        assert saved["entrypoint"] == "main.py"
        assert (pre_judge_script_service.SCRIPT_UPLOAD_BASE / created["id"] / "main.py").is_file()

        body = asyncio.run(debug_script(
            created["id"], PreJudgeScriptDebugPayload(fields={"msg": "inline-content"}),
            request=_request(), db=db_session))
        assert body["result"]["status"] == "ok"
        assert body["result"]["output"]["echo"] == "inline-content"
        assert body["static_check"] == []

    def test_upload_rejects_non_py_file(self, db_session):
        created = self._create_script_row(db_session)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_file(
                created["id"], request=_request(), file=_upload_file("x", "evil.sh"), db=db_session))
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("user", [
        {"user_code": "u_normal", "super_admin_flag": 0},
        {"user_code": "u_plain"},  # 无超管标志（deny by default）
        {"user_code": "u_wrong", "super_admin_flag": 2},
    ])
    def test_upload_admin_gate(self, db_session, user):
        """W1-4：非超管（flag=0/缺失/非 1）调写操作一律 403"""
        created = self._create_script_row(db_session)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_file(
                created["id"], request=_request(user=user),
                file=_upload_file(ECHO_SCRIPT), db=db_session))
        assert exc.value.status_code == 403

    def test_upload_unknown_script_400(self, db_session):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_file(
                "pjs_missing", request=_request(), file=_upload_file(ECHO_SCRIPT), db=db_session))
        assert exc.value.status_code == 400

    def test_upload_agent_script_rejected(self, db_session):
        """agent 类型脚本上传被拒（exec_type=script 专属；builtin 类型已在创建层拒绝）"""
        created = self._create_script_row(db_session, exec_type="claude_agent",
                                          name="api_up_agent_reject")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_file(
                created["id"], request=_request(), file=_upload_file(ECHO_SCRIPT), db=db_session))
        assert exc.value.status_code == 400

    def test_upload_with_admin_user_ok(self, db_session):
        created = self._create_script_row(db_session)
        saved = asyncio.run(upload_script_file(
            created["id"], request=_request(user={"user_code": "u_admin", "super_admin_flag": 1}),
            file=_upload_file(ECHO_SCRIPT),
            risk_acknowledged=None, declared_requirements=None, db=db_session))
        assert saved["entrypoint"].endswith(".py")

    def test_debug_requires_admin(self, db_session):
        """W1-4：debug 试跑与写操作同口径，非超管 403"""
        created = self._create_script_row(db_session)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(debug_script(
                created["id"], PreJudgeScriptDebugPayload(fields={}),
                request=_request(user={"user_code": "u_normal", "super_admin_flag": 0}),
                db=db_session))
        assert exc.value.status_code == 403


# ---------------- 编排集成：DB 脚本行 + 脚本串联 ----------------

PROBE_UPSTREAM_SRC = '''
def run(stdin):
    return {"status": "ok",
            "output": {"upstream_names": sorted((stdin.get("upstream") or {}).keys())},
            "files": {}, "summary": "", "finish_reason": "completed", "error": None}
'''


class TestPipelineIntegration:
    @pytest.fixture
    def pg_session_factory(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()

    def _add_probe_script(self, pg_session_factory, tmp_path):
        """上游探测脚本（统一子进程执行：脚本包 + script 记录）"""
        upload_base = tmp_path / "uploads_pre_judge_scripts"
        pkg = upload_base / "pjs_probe_up"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "main.py").write_text(PROBE_UPSTREAM_SRC, encoding="utf-8")
        pg_session_factory.add(PreJudgeScript(id="pjs_probe_up", name="probe_upstream",
                                              exec_type="script", entrypoint="main.py",
                                              timeout_seconds=30))

    def test_db_script_row_runs_in_pipeline(self, pg_session_factory, monkeypatch, tmp_path):
        """DB 记录 exec_type=script：编排走子进程分支，echo 输出进 script_outputs"""
        _write_pkg_script(ECHO_SCRIPT, script_id="pjs_e2e_script")
        pg_session_factory.add(PreJudgeScript(id="pjs_e2e_script", name="echo_script",
                                              exec_type="script", entrypoint="main.py",
                                              timeout_seconds=30))
        pg_session_factory.commit()
        monkeypatch.setattr(pre_judge_runner, "SessionLocal", lambda: pg_session_factory)

        task = _task([{"script_id": "pjs_e2e_script",
                       "input_mapping": {"msg": {"field": "note", "value_type": "inline"}}}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())

        assert out["aborted"] is False
        assert out["script_outputs"]["echo_script"] == {"echo": "hello", "params_strict": None}
        result = out["pre_script_results"][0]
        assert result["status"] == "ok" and result["script_name"] == "echo_script"

    def test_script_then_script_upstream_chain(self, pg_session_factory, monkeypatch, tmp_path):
        """脚本间串联：前序脚本的标准输出注入后序脚本的 stdin.upstream"""
        _write_pkg_script(ECHO_SCRIPT, script_id="pjs_e2e_chain")
        pg_session_factory.add(PreJudgeScript(id="pjs_e2e_chain", name="echo_script",
                                              exec_type="script", entrypoint="main.py",
                                              timeout_seconds=30))
        self._add_probe_script(pg_session_factory, tmp_path)
        pg_session_factory.commit()
        monkeypatch.setattr(pre_judge_runner, "SessionLocal", lambda: pg_session_factory)

        task = _task([
            {"script_id": "pjs_e2e_chain",
             "input_mapping": {"msg": {"field": "note", "value_type": "inline"}}},
            {"script_id": "pjs_probe_up", "output_var": "probe"},
        ])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())
        assert out["script_outputs"]["probe"]["upstream_names"] == ["echo_script"]

    def test_script_fail_continue_keeps_pipeline(self, pg_session_factory, monkeypatch, tmp_path):
        """script 失败（默认 continue）：记录 fail，后续脚本仍执行"""
        _write_pkg_script("import sys\nsys.exit(2)\n", script_id="pjs_e2e_bad")
        pg_session_factory.add(PreJudgeScript(id="pjs_e2e_bad", name="bad_script",
                                              exec_type="script", entrypoint="main.py",
                                              timeout_seconds=30))
        self._add_probe_script(pg_session_factory, tmp_path)
        pg_session_factory.commit()
        monkeypatch.setattr(pre_judge_runner, "SessionLocal", lambda: pg_session_factory)

        task = _task([
            {"script_id": "pjs_e2e_bad"},
            {"script_id": "pjs_probe_up", "output_var": "probe"},
        ])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())

        assert out["aborted"] is False
        first = out["pre_script_results"][0]
        assert first["status"] == "fail" and first["finish_reason"] == "error"
        assert out["script_outputs"]["probe"]["upstream_names"] == ["bad_script"]
