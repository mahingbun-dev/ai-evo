"""评测前处理脚本执行器（pre_judge_runner）单元测试

覆盖：串行编排与 output_var 输出收集、标准输入组装（inline 字段落盘本地路径、
params、upstream 传递）、产物目录 uploads/script_runs/{task_id}/{data_id}/、
on_fail continue/abort 策略、子进程超时 kill、DB 脚本加载、agent 类型凭据缺失
fail-fast（ARCH-3-2 起进沙箱，完整链路见 test_arch_3_2_harness_sandbox.py）、
与真实内置脚本 allure_vs_cases 的集成（统一子进程执行：builtin 进程内类型已
废除，fake 脚本以脚本包形态落盘 SCRIPT_UPLOAD_BASE/{id}/main.py）。
v3 契约：run_pre_scripts 的 steps 显式入参（优先于 task.pre_scripts）与
derive_script_steps（evaluation_targets 的 script_output 行派生编排步骤）。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.pre_judge_models import PreJudgeScript
from app.services import pre_judge_runner
from app.services import pre_judge_script_service as script_service

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------- fake 脚本源码（子进程执行，run(stdin) 函数式契约） ----------------

_ECHO_SRC = '''
import json
from pathlib import Path


def run(stdin):
    """回显 inline 字段内容与 upstream 键（验证标准输入组装）"""
    msg_path = (stdin.get("fields") or {}).get("msg")
    content = open(msg_path, encoding="utf-8").read() if msg_path and Path(msg_path).is_file() else ""
    upstream = stdin.get("upstream") or {}
    return {
        "status": "ok",
        "output": {"echo": content, "upstream_names": sorted(upstream.keys()),
                   "upstream_payload": {k: v.get("output") for k, v in upstream.items()}},
        "files": {},
        "summary": "echoed",
        "finish_reason": "completed",
        "error": None,
    }
'''

_WRITER_SRC = '''
from pathlib import Path


def run(stdin):
    """向 _run_dir 写产物并注册进 files（验证产物目录约定）"""
    target = Path(stdin["_run_dir"]) / "artifact.txt"
    target.write_text("artifact-content", encoding="utf-8")
    return {
        "status": "ok",
        "output": {"a": 1},
        "files": {"artifact.txt": str(target)},
        "summary": "wrote artifact",
        "finish_reason": "completed",
        "error": None,
    }
'''

_FAIL_SRC = '''
def run(stdin):
    return {"status": "fail", "output": None, "files": {}, "summary": "",
            "finish_reason": "error", "error": "boom"}
'''

_SLEEPER_SRC = '''
import time


def run(stdin):
    time.sleep(1.0)
    return {"status": "ok", "output": {"slept": True}, "files": {}, "summary": "",
            "finish_reason": "completed", "error": None}
'''

_CAPTURE_SRC = '''
def run(stdin):
    """把标准输入关键成员透出到 output（验证 params/task_id/data_id/model_output 组装）"""
    return {
        "status": "ok",
        "output": {"params": stdin.get("params"), "task_id": stdin.get("task_id"),
                   "data_id": stdin.get("data_id"), "model_output": stdin.get("model_output")},
        "files": {},
        "summary": "",
        "finish_reason": "completed",
        "error": None,
    }
'''

_BAD_OUTPUT_SRC = '''
def run(stdin):
    return "not-a-dict"
'''

_BAD_STATUS_SRC = '''
def run(stdin):
    return {"status": "weird", "output": {"x": 1}}
'''


def _write_script_pkg(base: Path, script_id: str, source: str) -> None:
    """落盘 fake 脚本包：{base}/{script_id}/main.py"""
    pkg_dir = base / script_id
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "main.py").write_text(source, encoding="utf-8")


FAKE_SCRIPTS = {
    "fake_echo": _ECHO_SRC,
    "fake_writer": _WRITER_SRC,
    "fake_fail": _FAIL_SRC,
    "fake_sleeper": _SLEEPER_SRC,
}


@pytest.fixture
def script_env(tmp_path, monkeypatch):
    """fake 脚本落库（exec_type=script）+ 落盘 + 内置脚本迁移，替换 runner.SessionLocal

    StaticPool 保证多个会话共享同一内存库（默认池下每次连接是独立空库）；
    SCRIPT_UPLOAD_BASE 指向临时目录（迁移内置脚本会拷贝源码文件，避免污染仓库）；
    同时迁移真实内置脚本（allure_vs_cases 等），使 builtin:xxx 历史引用与
    纯名称引用都能按名称解析到 DB 记录（子进程执行）。
    """
    from sqlalchemy.pool import StaticPool

    upload_base = tmp_path / "pre_judge_scripts"
    monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", upload_base)

    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        for name, source in FAKE_SCRIPTS.items():
            db.add(PreJudgeScript(id=f"pjs_{name}", name=name, exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
        db.commit()
        for name, source in FAKE_SCRIPTS.items():
            _write_script_pkg(upload_base, f"pjs_{name}", source)
        # 表非空：显式重建缺失内置，保持「统一落库环境」语义
        script_service.ensure_builtin_scripts_migrated(db, recreate_missing=True)
    monkeypatch.setattr(pre_judge_runner, "SessionLocal", Session)
    return {"session": Session, "upload_base": upload_base}


@pytest.fixture(autouse=True)
def run_base(tmp_path, monkeypatch):
    """产物根目录指向临时目录，避免污染仓库 uploads/"""
    base = tmp_path / "script_runs"
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
    return base


def _task(pre_scripts):
    return SimpleNamespace(id="task_runner_x", pre_scripts=pre_scripts)


def _dataset_item(**extra):
    data = {"note": "hello", **extra}
    return {"id": "data_1", "data": data}


@pytest.fixture
def pg_session_factory():
    """内存库会话工厂（含 pre_judge_scripts 表），供需要隔离模块级 SessionLocal 的用例

    模块级放置：TestDbScriptLoading 与 TestFailurePolicy 的未知脚本用例共用。
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ---------------- 编排与标准输入 ----------------

class TestOrchestration:
    def test_no_pre_scripts(self, script_env):
        out = pre_judge_runner.run_pre_scripts(_task(None), payload={"idx": 0})
        assert out["pre_script_results"] == []
        assert out["script_outputs"] == {} and out["script_files"] == {}
        assert out["aborted"] is False

    def test_single_script_success(self, script_env):
        task = _task([{
            "script_id": "fake_echo",
            "input_mapping": {"msg": {"field": "note", "value_type": "inline"}},
            "params": {"strict": True},
            "output_var": "echo_out",
        }])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())

        assert out["aborted"] is False
        assert out["script_outputs"]["echo_out"]["echo"] == "hello"
        result = out["pre_script_results"][0]
        assert result["status"] == "ok" and result["finish_reason"] == "completed"
        assert result["script_name"] == "fake_echo"
        assert result["error"] is None
        assert isinstance(result["duration_ms"], int)

    def test_inline_field_landed_as_local_file(self, script_env, run_base):
        task = _task([{"script_id": "fake_echo",
                       "input_mapping": {"msg": {"field": "note", "value_type": "inline"}}}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())

        # 标准输入的 fields 为本地绝对路径，内容与 inline 值一致
        assert out["script_outputs"]["fake_echo"]["echo"] == "hello"
        # 产物目录约定 uploads/script_runs/{task_id}/{data_id}/
        run_dir = run_base / "task_runner_x" / "data_1"
        assert (run_dir / "inputs" / "msg.txt").read_text(encoding="utf-8") == "hello"

    def test_params_passed_via_stdin(self, script_env):
        _write_script_pkg(script_env["upload_base"], "pjs_fake_capture", _CAPTURE_SRC)
        with script_env["session"]() as db:
            db.add(PreJudgeScript(id="pjs_fake_capture", name="fake_capture", exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
            db.commit()

        task = _task([{"script_id": "fake_capture", "input_mapping": {},
                       "params": {"fuzzy_threshold": 0.8}}])
        out = pre_judge_runner.run_pre_scripts(task, payload={"model_output": "md-out"},
                                               dataset_item=_dataset_item())
        captured = out["script_outputs"]["fake_capture"]
        assert captured["params"] == {"fuzzy_threshold": 0.8}
        assert captured["task_id"] == "task_runner_x"
        assert captured["data_id"] == "data_1"
        assert captured["model_output"] == "md-out"

    def test_upstream_chaining(self, script_env):
        task = _task([
            {"script_id": "fake_writer", "output_var": "s1"},
            {"script_id": "fake_echo", "output_var": "s2"},
        ])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())

        assert out["script_outputs"]["s1"] == {"a": 1}
        second = out["script_outputs"]["s2"]
        assert second["upstream_names"] == ["fake_writer"]
        assert second["upstream_payload"]["fake_writer"] == {"a": 1}
        # 前序脚本产物文件路径注册并真实存在
        artifact = out["script_files"]["s1"]["artifact.txt"]
        assert Path(artifact).is_file()
        assert "task_runner_x" in artifact and "data_1" in artifact

    def test_local_path_field_used_directly(self, script_env, tmp_path, run_base):
        existing = tmp_path / "cases.json"
        existing.write_text("[]", encoding="utf-8")
        task = _task([{"script_id": "fake_echo",
                       "input_mapping": {"msg": {"field": "cases_file", "value_type": "local"}}}])
        out = pre_judge_runner.run_pre_scripts(
            task, payload={}, dataset_item=_dataset_item(cases_file=str(existing)))
        # 路径直用（local 通道），不生成 inline 副本
        assert not (run_base / "task_runner_x" / "data_1" / "inputs" / "cases_file.txt").exists()

    def test_run_dir_absolute_when_base_relative(self, script_env, tmp_path, monkeypatch):
        """生产形态回归：SCRIPT_RUN_BASE 为相对路径时 _run_dir 必须仍是绝对路径

        子进程 cwd 是隔离目录（build_script_run_dir），若 stdin._run_dir 传相对
        路径，脚本按 _run_dir 写产物会拼到隔离 cwd 下（父目录不存在即失败）——
        验收 D1：folder 模板试跑必现 FileNotFoundError 的根因。
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", Path("uploads/script_runs"))

        out = pre_judge_runner.run_pre_scripts(
            _task([{"script_id": "fake_writer", "output_var": "s1"}]),
            payload={}, dataset_item=_dataset_item())

        assert out["pre_script_results"][0]["status"] == "ok"
        artifact = Path(out["script_files"]["s1"]["artifact.txt"])
        assert artifact.is_absolute()
        assert artifact.is_file()
        # 产物落在进程 cwd 下的真实 script_runs 目录，而非子进程隔离 cwd 里
        assert artifact == (tmp_path / "uploads/script_runs/task_runner_x/data_1/artifact.txt").resolve()


# ---------------- 脚本产物目录 outputs/（平台保证创建，脚本统一写这里） ----------------

_WRITER_OUTPUTS_SRC = '''
from pathlib import Path


def run(stdin):
    """按产物目录规范写 {_run_dir}/outputs/ 并注册进 files（验证平台建区保证）"""
    outputs_dir = Path(stdin["_run_dir"]) / "outputs"
    target = outputs_dir / "artifact.txt"
    target.write_text("artifact-content", encoding="utf-8")
    return {
        "status": "ok",
        "output": {"a": 1},
        "files": {"artifact.txt": str(target)},
        "summary": "wrote artifact",
        "finish_reason": "completed",
        "error": None,
    }
'''


class TestOutputsDir:
    def test_build_run_dir_creates_outputs(self, run_base):
        """工作区组装即创建 outputs/（真实任务与试跑 debug 共用 build_run_dir）"""
        run_dir = pre_judge_runner.build_run_dir("task_x", "data_1")
        assert run_dir.is_absolute()
        assert (run_dir / "outputs").is_dir()
        # 幂等：重复调用不报错
        pre_judge_runner.build_run_dir("task_x", "data_1")
        assert (run_dir / "outputs").is_dir()

    def test_script_writes_to_outputs_dir_end_to_end(self, script_env):
        """按规范写 {_run_dir}/outputs/ 的脚本在真实执行链路下产物真实落盘"""
        _write_script_pkg(script_env["upload_base"], "pjs_fake_writer2", _WRITER_OUTPUTS_SRC)
        with script_env["session"]() as db:
            db.add(PreJudgeScript(id="pjs_fake_writer2", name="fake_writer2", exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
            db.commit()

        out = pre_judge_runner.run_pre_scripts(
            _task([{"script_id": "fake_writer2", "output_var": "s1"}]),
            payload={}, dataset_item=_dataset_item())

        assert out["pre_script_results"][0]["status"] == "ok"
        artifact = Path(out["script_files"]["s1"]["artifact.txt"])
        assert artifact.is_file()
        # 产物落在工作区 outputs/ 下，而非 cwd 隔离目录或工作区根
        assert artifact.parent == pre_judge_runner.build_run_dir("task_runner_x", "data_1") / "outputs"
        assert artifact.read_text(encoding="utf-8") == "artifact-content"


# ---------------- steps 显式入参（共享设计契约 v3 §三） ----------------

class TestExplicitStepsParam:
    def test_explicit_steps_take_precedence(self, script_env):
        """steps 显式传入优先：task.pre_scripts 被忽略，只执行 steps（task 若被误读会执行 fake_fail）"""
        task = _task([{"script_id": "fake_fail"}])
        steps = [{"script_id": "fake_writer", "output_var": "s1"}]
        out = pre_judge_runner.run_pre_scripts(
            task, payload={}, dataset_item=_dataset_item(), steps=steps)
        assert [r["script_name"] for r in out["pre_script_results"]] == ["fake_writer"]
        assert out["script_outputs"]["s1"] == {"a": 1}

    def test_steps_none_reads_task_pre_scripts(self, script_env):
        """steps=None 保持旧行为：读 task.pre_scripts"""
        task = _task([{"script_id": "fake_writer", "output_var": "s1"}])
        out = pre_judge_runner.run_pre_scripts(
            task, payload={}, dataset_item=_dataset_item(), steps=None)
        assert out["script_outputs"]["s1"] == {"a": 1}

    def test_explicit_empty_steps_runs_nothing(self, script_env):
        """显式空列表 = 显式声明无脚本：覆盖 task.pre_scripts，不执行任何脚本"""
        task = _task([{"script_id": "fake_writer"}])
        out = pre_judge_runner.run_pre_scripts(
            task, payload={}, dataset_item=_dataset_item(), steps=[])
        assert out["pre_script_results"] == []
        assert out["script_outputs"] == {} and out["script_files"] == {}


# ---------------- derive_script_steps（共享设计契约 v3 §二） ----------------

class TestDeriveScriptSteps:
    def test_row_order_and_dedup(self):
        """按行序取 script_output 行，script_id 去重且首行决定全部编排参数"""
        targets = [
            {"id": "t0", "source": "model_output"},  # 非 script_output 来源跳过
            {"id": "t1", "source": "script_output", "source_config": {
                "script_id": "s_a",
                "input_mapping": {"msg": {"field": "note"}},
                "params": {"k": 1}, "on_fail": "abort", "output_var": "a_out"}},
            {"id": "t2", "source": "script_output",
             "source_config": {"script_id": "s_b", "output_key": "rate"}},
            # 同脚本第二行：去重，其 output_var 不生效
            {"id": "t3", "source": "script_output",
             "source_config": {"script_id": "s_a", "output_var": "second_row_ignored"}},
        ]
        steps = pre_judge_runner.derive_script_steps(targets)

        assert [s["script_id"] for s in steps] == ["s_a", "s_b"]  # 行序 + 去重
        # 首行决定 input_mapping / params / on_fail / output_var
        assert steps[0] == {
            "script_id": "s_a",
            "input_mapping": {"msg": {"field": "note"}},
            "params": {"k": 1},
            "output_var": "a_out",
            "on_fail": "abort",
        }
        # 缺省值：output_var=脚本名（script_id 口径）、on_fail=continue、空 mapping/params
        assert steps[1] == {
            "script_id": "s_b",
            "input_mapping": {},
            "params": {},
            "output_var": "s_b",
            "on_fail": "continue",
        }

    def test_skips_illegal_rows(self):
        """非 dict 行 / 缺 script_id / source_config 非法的行跳过，不抛错"""
        steps = pre_judge_runner.derive_script_steps([
            "not-a-dict",
            {"id": "t1", "source": "script_output"},  # 无 source_config → 无 script_id
            {"id": "t2", "source": "script_output", "source_config": {"script_id": "   "}},
            {"id": "t3", "source": "script_output", "source_config": "bad"},  # config 非法
        ])
        assert steps == []

    def test_none_or_non_list_input(self):
        """None / 空列表 / 非列表输入返回空步骤"""
        assert pre_judge_runner.derive_script_steps(None) == []
        assert pre_judge_runner.derive_script_steps([]) == []
        assert pre_judge_runner.derive_script_steps("not-a-list") == []

    def test_derived_steps_run_via_run_pre_scripts(self, script_env):
        """派生步骤可直接交 run_pre_scripts(steps=...) 执行（与收集口径闭环）"""
        targets = [{"id": "t1", "source": "script_output", "source_config": {
            "script_id": "fake_echo", "output_var": "echo_out",
            "input_mapping": {"msg": {"field": "note", "value_type": "inline"}}}}]
        steps = pre_judge_runner.derive_script_steps(targets)
        task = _task(None)  # 已迁移任务形态：pre_scripts 为空
        out = pre_judge_runner.run_pre_scripts(
            task, payload={}, dataset_item=_dataset_item(), steps=steps)
        assert out["script_outputs"]["echo_out"]["echo"] == "hello"


# ---------------- 失败策略 ----------------

class TestFailurePolicy:
    def test_missing_field_fail_continue(self, script_env):
        task = _task([
            {"script_id": "fake_echo", "input_mapping": {"msg": {"field": "absent_field"}}},
            {"script_id": "fake_writer", "output_var": "s2"},
        ])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())

        assert out["aborted"] is False
        assert len(out["pre_script_results"]) == 2
        first = out["pre_script_results"][0]
        assert first["status"] == "fail"
        assert "absent_field" in first["error"]
        # continue：后续脚本仍执行
        assert out["script_outputs"]["s2"] == {"a": 1}

    def test_on_fail_abort_stops_pipeline(self, script_env):
        task = _task([
            {"script_id": "fake_fail", "on_fail": "abort"},
            {"script_id": "fake_writer", "output_var": "never"},
        ])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())

        assert out["aborted"] is True
        assert "boom" in out["error"]
        assert len(out["pre_script_results"]) == 1
        assert "never" not in out["script_outputs"]

    def test_script_fail_continue_by_default(self, script_env):
        task = _task([
            {"script_id": "fake_fail"},
            {"script_id": "fake_writer", "output_var": "after"},
        ])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())
        assert out["aborted"] is False
        assert out["pre_script_results"][0]["status"] == "fail"
        assert out["script_outputs"]["after"] == {"a": 1}

    def test_unknown_script_id_fail_continue(self, script_env, pg_session_factory, monkeypatch):
        # DB 隔离（测试资产加固）：未知脚本 id 会落到 DB 查询，真实环境缺
        # pre_judge_scripts 表时抛 ProgrammingError 而非返回「脚本不存在」；
        # 桩成内存空表后走既有语义：查无记录 → fail + 不存在（断言不弱化）
        monkeypatch.setattr(pre_judge_runner, "SessionLocal", lambda: pg_session_factory)
        task = _task([{"script_id": "pjs_ghost", "input_mapping": {}}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())
        assert out["pre_script_results"][0]["status"] == "fail"
        assert "不存在" in out["pre_script_results"][0]["error"]


# ---------------- 超时与类型边界 ----------------

class TestTimeoutAndTypes:
    def test_subprocess_timeout_kill(self, script_env, run_base):
        """子进程超时 kill：返回 fail/timeout 而非抛异常阻塞"""
        result = pre_judge_runner.execute_script(
            {"script_id": "pjs_fake_sleeper", "name": "fake_sleeper", "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 0.3},
            {"fields": {}, "params": {}, "upstream": {}, "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "fail"
        assert result["finish_reason"] == "timeout"
        assert "超时" in result["error"]

    def test_agent_type_fails_clearly(self, script_env, run_base, monkeypatch):
        """ARCH-3-2 起 agent 类型进沙箱执行；凭据未配置时 fail-fast（不发起 docker）"""
        from app.services import claude_code_config_service

        monkeypatch.setattr(claude_code_config_service.ClaudeCodeConfigService,
                            "get_active_config", classmethod(lambda cls: None))
        monkeypatch.setattr(claude_code_config_service.ClaudeCodeConfigService,
                            "get_sdk_env", classmethod(lambda cls: {}))
        from app.services import script_container
        monkeypatch.setattr(script_container, "get_sandbox_driver",
                            lambda: pytest.fail("凭据缺失应 fail-fast，不应发起沙箱执行"))
        result = pre_judge_runner.execute_script(
            {"script_id": "pjs_agent", "name": "agent_script", "exec_type": "claude_agent",
             "entrypoint": "指令文本", "timeout_seconds": 60},
            {"fields": {}, "params": {}, "upstream": {}, "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "Claude Code 全局配置" in result["error"]

    def test_builtin_exec_type_rejected(self, script_env, run_base):
        """历史 builtin 类型已废除：明确 fail 提示而非静默回退"""
        result = pre_judge_runner.execute_script(
            {"script_id": "pjs_legacy", "name": "legacy", "exec_type": "builtin",
             "entrypoint": None, "timeout_seconds": 5},
            {"fields": {}, "params": {}, "upstream": {}, "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "fail"
        assert "已废除" in result["error"]

    def test_illegal_stdout_shape_fails(self, script_env, run_base):
        """run() 返回非 dict：wrapper 以非零码退出并写明原因，stderr 尾部带回结果"""
        _write_script_pkg(script_env["upload_base"], "fake_bad_output", _BAD_OUTPUT_SRC)
        result = pre_judge_runner.execute_script(
            {"script_id": "fake_bad_output", "name": "fake_bad_output", "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 10},
            {"fields": {}, "params": {}, "upstream": {}, "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "dict" in result["error"]

    def test_illegal_status_value_normalized_to_fail(self, script_env, run_base):
        _write_script_pkg(script_env["upload_base"], "fake_bad_status", _BAD_STATUS_SRC)
        result = pre_judge_runner.execute_script(
            {"script_id": "fake_bad_status", "name": "fake_bad_status", "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 10},
            {"fields": {}, "params": {}, "upstream": {}, "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "fail"
        assert "非法 status" in result["error"]

    def test_folder_script_sibling_import(self, script_env, run_base):
        """文件夹包兄弟模块 import：main.py import utils（脚本目录 sys.path[0]）"""
        pkg = script_env["upload_base"] / "fake_pkg"
        (pkg / "sub").mkdir(parents=True, exist_ok=True)
        (pkg / "main.py").write_text(
            "from sub.helper import build\n\n"
            "def run(stdin):\n"
            "    return {'status': 'ok', 'output': build(stdin.get('params') or {}),\n"
            "            'files': {}, 'summary': '', 'finish_reason': 'completed', 'error': None}\n",
            encoding="utf-8")
        (pkg / "sub" / "helper.py").write_text(
            "def build(params):\n    return {'doubled': params.get('x', 0) * 2}\n",
            encoding="utf-8")

        result = pre_judge_runner.execute_script(
            {"script_id": "fake_pkg", "name": "fake_pkg", "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 10},
            {"fields": {}, "params": {"x": 21}, "upstream": {}, "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "ok"
        assert result["output"] == {"doubled": 42}

    def test_platform_import_via_pythonpath(self, script_env, run_base, monkeypatch):
        """PYTHONPATH 注入项目根：子进程可 import 平台侧模块（app 包）

        W8 调整：timeout_seconds 15→60。CI 容器模拟全量套件实测（qemu 模拟
        amd64、套件满载时）该用例两次 status=fail、单独复跑与快速全量均绿
        （2210 passed 全绿轮耗时 335s vs 失败轮 956s）——唯一负载敏感机制是
        子进程 15s 超时（子进程要冷启动 import 整个 app 包）。用例意图是
        「import 可达」而非「15s 内完成」，放宽到 60s 消除负载敏感。
        """
        pkg = script_env["upload_base"] / "fake_platform"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "main.py").write_text(
            "def run(stdin):\n"
            "    from app.services.pre_judge_entry import has_toplevel_run\n"
            "    return {'status': 'ok', 'output': {'imported': has_toplevel_run('def run(x): pass')},\n"
            "            'files': {}, 'summary': '', 'finish_reason': 'completed', 'error': None}\n",
            encoding="utf-8")

        result = pre_judge_runner.execute_script(
            {"script_id": "fake_platform", "name": "fake_platform", "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 60},
            {"fields": {}, "params": {}, "upstream": {}, "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "ok"
        assert result["output"] == {"imported": True}

    def test_legacy_main_style_script_passthrough(self, script_env, run_base):
        """传统 __main__ 风格脚本兼容：无 run() 函数，stdin 直通、自打印末行 JSON"""
        pkg = script_env["upload_base"] / "fake_legacy"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "main.py").write_text(
            "import json, sys\n"
            "stdin = json.load(sys.stdin)\n"
            "print(json.dumps({'status': 'ok', 'output': {'legacy': stdin['params'].get('k')},\n"
            "                  'files': {}, 'summary': '', 'finish_reason': 'completed',\n"
            "                  'error': None}, ensure_ascii=False))\n",
            encoding="utf-8")

        result = pre_judge_runner.execute_script(
            {"script_id": "fake_legacy", "name": "fake_legacy", "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 10},
            {"fields": {}, "params": {"k": "v1"}, "upstream": {}, "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "ok"
        assert result["output"] == {"legacy": "v1"}


# ---------------- DB 脚本加载 ----------------

class TestDbScriptLoading:
    def test_load_db_script_and_run(self, script_env, pg_session_factory, monkeypatch, tmp_path):
        from app.services import pre_judge_script_service as svc
        upload_base = tmp_path / "db_scripts"
        monkeypatch.setattr(svc, "SCRIPT_UPLOAD_BASE", upload_base)
        _write_script_pkg(upload_base, "pjs_db1", _ECHO_SRC)

        row = PreJudgeScript(id="pjs_db1", name="db_echo", exec_type="script",
                             entrypoint="main.py", timeout_seconds=77)
        pg_session_factory.add(row)
        pg_session_factory.commit()
        # runner 内部使用模块级 SessionLocal；测试替换为内存库会话工厂
        monkeypatch.setattr(pre_judge_runner, "SessionLocal", lambda: pg_session_factory)

        task = _task([{"script_id": "pjs_db1", "input_mapping": {"msg": {"field": "note"}}}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())
        assert out["script_outputs"]["db_echo"]["echo"] == "hello"

    def test_db_script_not_found(self, script_env, pg_session_factory, monkeypatch):
        monkeypatch.setattr(pre_judge_runner, "SessionLocal", lambda: pg_session_factory)
        task = _task([{"script_id": "pjs_missing_row", "input_mapping": {}}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())
        assert out["pre_script_results"][0]["status"] == "fail"

    def test_legacy_single_file_layout_auto_migrated(self, script_env, pg_session_factory,
                                                     monkeypatch, tmp_path):
        """旧布局 {id}.py 单文件 → 执行时自动迁移为 {id}/main.py 并回写 entrypoint"""
        from app.services import pre_judge_script_service as svc
        upload_base = tmp_path / "legacy_scripts"
        upload_base.mkdir(parents=True, exist_ok=True)
        (upload_base / "pjs_legacy1.py").write_text(_ECHO_SRC, encoding="utf-8")
        monkeypatch.setattr(svc, "SCRIPT_UPLOAD_BASE", upload_base)

        row = PreJudgeScript(id="pjs_legacy1", name="legacy_echo", exec_type="script",
                             entrypoint=str(upload_base / "pjs_legacy1.py"), timeout_seconds=30)
        pg_session_factory.add(row)
        pg_session_factory.commit()
        monkeypatch.setattr(pre_judge_runner, "SessionLocal", lambda: pg_session_factory)

        task = _task([{"script_id": "pjs_legacy1", "input_mapping": {"msg": {"field": "note"}}}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())
        assert out["script_outputs"]["legacy_echo"]["echo"] == "hello"
        # 布局迁移完成：旧单文件不再存在，目录内 main.py 就位且 entrypoint 已回写
        assert not (upload_base / "pjs_legacy1.py").exists()
        assert (upload_base / "pjs_legacy1" / "main.py").is_file()
        refreshed = pg_session_factory.query(PreJudgeScript).filter_by(id="pjs_legacy1").first()
        assert refreshed.entrypoint == "main.py"


# ---------------- 与真实内置脚本集成（迁移后子进程执行） ----------------

class TestWithRealBuiltin:
    def test_allure_vs_cases_via_pipeline(self, script_env, tmp_path):
        cases_file = tmp_path / "cases.json"
        cases_file.write_text(json.dumps([
            {"name": "case_ok", "expected": "ok", "priority": "P1"},
            {"name": "case_bad", "expected": "bad", "priority": "P1"},
        ], ensure_ascii=False), encoding="utf-8")
        results_dir = tmp_path / "allure-results"
        results_dir.mkdir()
        (results_dir / "0-result.json").write_text(
            json.dumps({"name": "case_ok", "status": "passed"}), encoding="utf-8")
        (results_dir / "1-result.json").write_text(
            json.dumps({"name": "case_bad", "status": "failed", "statusDetails": {"message": "x"}}), encoding="utf-8")

        task = _task([{
            "script_id": "builtin:allure_vs_cases",
            "input_mapping": {
                "cases_file": {"field": "cases_file", "value_type": "local"},
                "allure_results": {"field": "allure_results", "value_type": "local"},
            },
            "params": {},
            "output_var": "allure_compare",
            "on_fail": "continue",
        }])
        item = _dataset_item(cases_file=str(cases_file), allure_results=str(results_dir))
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=item)

        output = out["script_outputs"]["allure_compare"]
        assert output["consistency_rate"] == 0.5
        assert output["mismatched_count"] == 1
        # 内置脚本产物注册进 script_files，落盘在任务/数据项目录
        detail = out["script_files"]["allure_compare"]["per_case_full.json"]
        assert Path(detail).is_file()
        assert "task_runner_x" in detail and "data_1" in detail

    def test_allure_vs_cases_record_is_script_type(self, script_env):
        """迁移后内置脚本记录 exec_type=script、entrypoint=main.py、文件就位"""
        with script_env["session"]() as db:
            row = db.query(PreJudgeScript).filter_by(name="allure_vs_cases").first()
            assert row is not None
            assert row.exec_type == "script"
            assert row.entrypoint == "main.py"
        assert (script_env["upload_base"] / row.id / "main.py").is_file()

    def test_hub_file_channel_not_yet_supported(self, script_env):
        task = _task([{
            "script_id": "builtin:allure_vs_cases",
            "input_mapping": {
                "cases_file": {"field": "cases_file", "value_type": "hub_file"},
                "allure_results": {"field": "allure_results", "value_type": "hub_file"},
            },
        }])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=_dataset_item())
        result = out["pre_script_results"][0]
        assert result["status"] == "fail"
        assert "hub_file" in result["error"]


# ---------------- 严格拷贝语义：脚本包/输入文件/执行包副本隔离 ----------------

_SELF_PATH_ECHO_SRC = '''
import json
from pathlib import Path

def run(stdin):
    pkg_dir = Path(__file__).resolve().parent
    # 对自身包目录写文件：严格拷贝语义下只落在本条工作区副本
    (pkg_dir / "pkg_write.txt").write_text("pkg-write", encoding="utf-8")
    return {"status": "ok",
            "output": {"pkg_dir": str(pkg_dir)},
            "files": {}, "summary": "", "finish_reason": "completed", "error": None}
'''

_HUB_WRITE_SRC = '''
import json
from pathlib import Path

def run(stdin):
    root = Path((stdin.get("inputs") or {}).get("pkg")["root"])
    (root / "script_written_here.txt").write_text("hub-write", encoding="utf-8")
    return {"status": "ok", "output": {"root": str(root)},
            "files": {}, "summary": "", "finish_reason": "completed", "error": None}
'''


class TestStrictCopySemantics:
    """核心要求（严格拷贝）：脚本包、输入文件、hub 执行包都拷进该条工作区，
    脚本对副本的读写不污染脚本库/任务级镜像/上传原件，工作区评完即清"""

    def _register(self, script_env, script_id, source):
        _write_script_pkg(script_env["upload_base"], f"pjs_{script_id}", source)
        with script_env["session"]() as db:
            db.add(PreJudgeScript(id=f"pjs_{script_id}", name=script_id, exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
            db.commit()

    def test_script_package_staged_copy_and_no_source_pollution(self, script_env, run_base):
        self._register(script_env, "pkg_echo", _SELF_PATH_ECHO_SRC)
        out = pre_judge_runner.run_pre_scripts(
            _task([{"script_id": "pkg_echo"}]), payload={}, dataset_item=_dataset_item())
        result = out["pre_script_results"][0]
        assert result["status"] == "ok", result["error"]

        pkg_dir = Path(result["output"]["pkg_dir"])
        # 执行的是工作区 scripts/ 下的副本，不是脚本库源目录
        assert pkg_dir.is_relative_to((run_base / "task_runner_x" / "data_1" / "scripts").resolve())
        assert not pkg_dir.is_relative_to(script_env["upload_base"].resolve())
        # 对包目录的写操作落在副本内，源脚本包无污染
        assert (pkg_dir / "pkg_write.txt").is_file()
        assert not (script_env["upload_base"] / "pjs_pkg_echo" / "pkg_write.txt").exists()

    def test_local_field_copied_into_workspace(self, script_env, tmp_path, run_base):
        src = tmp_path / "cases.json"
        src.write_text('["a"]', encoding="utf-8")
        out = pre_judge_runner.run_pre_scripts(
            _task([{"script_id": "fake_echo",
                    "input_mapping": {"msg": {"field": "cases_file", "value_type": "local"}}}]),
            payload={}, dataset_item=_dataset_item(cases_file=str(src)))
        assert out["pre_script_results"][0]["status"] == "ok"
        # 脚本读到的是工作区副本（内容与源一致），源文件不被触碰
        assert out["script_outputs"]["fake_echo"]["echo"] == '["a"]'
        copy_path = run_base / "task_runner_x" / "data_1" / "inputs" / "msg.json"
        assert copy_path.is_file() and copy_path.read_text(encoding="utf-8") == '["a"]'
        assert src.read_text(encoding="utf-8") == '["a"]'

    def test_hub_package_copy_no_source_pollution(self, script_env, tmp_path, run_base):
        pkg_root = tmp_path / "mirror" / "fid-pkg"
        (pkg_root / "sub").mkdir(parents=True)
        (pkg_root / "sub" / "a.json").write_text("{}", encoding="utf-8")
        self._register(script_env, "hub_write", _HUB_WRITE_SRC)

        out = pre_judge_runner.run_pre_scripts(
            _task([{"script_id": "hub_write",
                    "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file"}}}]),
            payload={}, dataset_item=_dataset_item(pkg=str(pkg_root)))
        result = out["pre_script_results"][0]
        assert result["status"] == "ok", result["error"]

        root = Path(result["output"]["root"])
        assert root.is_relative_to((run_base / "task_runner_x" / "data_1" / "inputs").resolve())
        assert (root / "script_written_here.txt").is_file()          # 写在副本上
        assert not (pkg_root / "script_written_here.txt").exists()   # 源镜像无污染
        assert (pkg_root / "sub" / "a.json").is_file()


_ROW_FIELDS_SRC = '''
def run(stdin):
    """产出行字段契约（stdout.row_fields）：含合法项、重名项、非法项与非白名单键"""
    return {
        "status": "ok",
        "output": {"schema_version": "v1", "thread_events": None},
        "files": {},
        "summary": "row_fields 契约",
        "row_fields": [
            {"name": "schema_version", "type": "string"},
            {"name": "thread_events", "type": "json", "label": "会话事件时间线"},
            {"name": "schema_version", "type": "json"},
            {"label": "缺 name"},
            "not-a-dict",
            {"name": "extra", "type": "string", "position": 3, "unknown": "x"},
        ],
        "finish_reason": "completed",
        "error": None,
    }
'''


class TestRowFieldsContract:
    """脚本自描述的行字段契约（stdout.row_fields）：归一化与透传

    平台不再持有行 schema 清单——字段元数据由产出方声明（本契约）优先、
    行内容推导（dataset_field_service.derive_fields_from_row）兜底。
    """

    def test_normalize_keeps_whitelist_keys_only(self):
        """只保留 name/label/type/is_media/is_media_field 五键，其余剔除"""
        raw = [{"name": "a", "label": "甲", "type": "json", "is_media": True,
                "is_media_field": "image", "position": 3, "unknown": "x"}]
        assert pre_judge_runner._normalize_row_fields(raw) == [
            {"name": "a", "label": "甲", "type": "json",
             "is_media": True, "is_media_field": "image"}]

    def test_normalize_drops_invalid_and_duplicate_items(self):
        """非 dict / 缺 name / 空白 name / 重名 一律丢弃，保留首次出现"""
        raw = ["not-a-dict", None, 42, {"label": "缺 name"}, {"name": "   "},
               {"name": "a"}, {"name": "a", "type": "json"}]
        assert pre_judge_runner._normalize_row_fields(raw) == [{"name": "a"}]

    def test_normalize_non_list_returns_empty(self):
        for bad in (None, "not-a-list", {"name": "a"}, 42):
            assert pre_judge_runner._normalize_row_fields(bad) == []

    def test_normalize_stdout_carries_row_fields(self):
        result = pre_judge_runner._normalize_stdout(
            "pjs_x", "x",
            {"status": "ok", "row_fields": [{"name": "a", "type": "json"}]},
            started_at=0.0)
        assert result["row_fields"] == [{"name": "a", "type": "json"}]

    def test_normalize_stdout_defaults_row_fields_to_empty(self):
        """脚本未声明时为空列表：导入侧改从行内容推导，不报错"""
        result = pre_judge_runner._normalize_stdout(
            "pjs_x", "x", {"status": "ok"}, started_at=0.0)
        assert result["row_fields"] == []

    def test_result_template_has_row_fields(self):
        """_result 默认条目含 row_fields：超时/类型不支持等提前返回路径契约一致"""
        result = pre_judge_runner._result("pjs_x", "x", "fail",
                                          finish_reason="error", started_at=0.0)
        assert result["row_fields"] == []

    def test_output_keys_include_row_fields(self):
        """row_fields 必须在输出白名单里，否则归一化结果会被裁掉"""
        assert "row_fields" in pre_judge_runner._OUTPUT_KEYS

    def test_execute_script_returns_row_fields_end_to_end(self, script_env, run_base):
        """端到端：真实子进程脚本声明的 row_fields 经归一化后出现在执行结果里"""
        _write_script_pkg(script_env["upload_base"], "pjs_row_fields", _ROW_FIELDS_SRC)
        result = pre_judge_runner.execute_script(
            {"script_id": "pjs_row_fields", "name": "fake_row_fields",
             "exec_type": "script", "entrypoint": "main.py", "timeout_seconds": 30},
            {"fields": {}, "params": {}, "upstream": {},
             "_run_dir": str(run_base / "t")},
        )
        assert result["status"] == "ok", result.get("error")
        assert result["row_fields"] == [
            {"name": "schema_version", "type": "string"},
            {"name": "thread_events", "type": "json", "label": "会话事件时间线"},
            {"name": "extra", "type": "string"},
        ]
