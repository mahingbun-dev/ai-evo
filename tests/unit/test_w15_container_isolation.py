"""W15 脚本执行容器化隔离单元测试（L1，无 docker 依赖）

覆盖（整改计划 §4 W15 方案要点逐项）：
- 灰度开关 SCRIPT_EXEC_MODE=local|container（env 实时读、非法值 fail-safe 回退 local）
- docker run --rm 命令构造：非 root、--memory 1g --cpus 0.5 --pids-limit、
  --read-only + tmpfs、--network none（任务级开关默认关）、-i、挂载仅本次
  script_runs 工作目录、平台 env 剥离（仅显式注入最小集合）、尾部命令同构
- 路径映射：真实任务/ debug 试跑两种目录结构；stdin 宿主路径 → /workspace 重写
- 依赖声明校验（pre_judge_script_service.py:470-514 段）container 分支改
  「runner 镜像内已装集合」静态清单，local 分支保持子进程探测
- 执行分流：container 模式进 docker run 通道，local 模式行为不变
- 冒烟 container 通道：拷贝进临时工作区、路径重写、与生产同构解析
- RUNNER_IMAGE_PACKAGES ↔ docker/script-runner/packages.txt 同步门禁

docker 实机探针（读 env/读宿主路径/网络/资源限制）在
tests/integration/test_w15_container_probe.py（本机 docker 不可用时 skip）。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.services import pre_judge_runner, pre_judge_script_service, script_container

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------- 灰度开关 ----------------

class TestExecModeSwitch:
    def test_default_is_local(self, monkeypatch):
        monkeypatch.delenv("SCRIPT_EXEC_MODE", raising=False)
        assert script_container.get_script_exec_mode() == "local"
        assert script_container.container_mode_enabled() is False

    def test_env_container(self, monkeypatch):
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
        assert script_container.get_script_exec_mode() == "container"
        assert script_container.container_mode_enabled() is True

    def test_env_case_and_space_tolerant(self, monkeypatch):
        monkeypatch.setenv("SCRIPT_EXEC_MODE", " Container ")
        assert script_container.get_script_exec_mode() == "container"

    def test_invalid_env_falls_back_to_local(self, monkeypatch):
        """非法值 fail-safe：绝不进容器通道"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "docker-compose")
        assert script_container.get_script_exec_mode() == "local"
        assert script_container.container_mode_enabled() is False


# ---------------- docker run 命令构造 ----------------

def _make_paths(tmp_path: Path) -> "script_container.ContainerPaths":
    run_dir = tmp_path / "script_runs" / "t1" / "d1"
    staged = run_dir / "scripts" / "s1"
    cwd = run_dir / "s1"
    return script_container.build_container_paths(run_dir, cwd, staged)


class TestDockerRunArgs:
    """W15 隔离清单逐项落位断言"""

    def _args(self, tmp_path, network=False):
        paths = _make_paths(tmp_path)
        return script_container.build_docker_run_args(
            image="ai-evo-script-runner:latest", user="10001:10001",
            paths=paths, entry_rel="main.py",
            container_name="aievo-script-s1-abc", network=network,
        ), paths

    def test_isolation_flags(self, tmp_path):
        args, _ = self._args(tmp_path)
        # docker run --rm（"docker" 前缀由调用方拼，构造函数从 run 起）
        assert args[0:2] == ["run", "--rm"]
        assert "--name" in args and "aievo-script-s1-abc" in args
        # 非 root 用户
        assert args[args.index("--user") + 1] == "10001:10001"
        # 资源限制：memory 1g（swap 同额）、cpus 0.5、pids-limit
        assert args[args.index("--memory") + 1] == "1g"
        assert args[args.index("--memory-swap") + 1] == "1g"
        assert args[args.index("--cpus") + 1] == "0.5"
        assert args[args.index("--pids-limit") + 1] == "128"
        # 只读根文件系统 + tmpfs（/tmp 与容器用户 home）
        assert "--read-only" in args
        tmpfs = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
        assert any(t.startswith("/tmp:rw,nosuid,noexec,size=64m") for t in tmpfs)
        assert any(t.startswith("/home/runner:rw,nosuid,noexec,size=64m") for t in tmpfs)
        # stdin 透传（payload 经 CLI 传给容器）
        assert "-i" in args
        assert "-t" not in args  # 无 TTY
        assert "--privileged" not in args

    def test_network_none_by_default(self, tmp_path):
        """任务级网络开关默认关：--network none（整改计划 §4 W15）"""
        args, _ = self._args(tmp_path, network=False)
        assert args[args.index("--network") + 1] == "none"

    def test_network_switch_opt_in_bridge(self, tmp_path):
        args, _ = self._args(tmp_path, network=True)
        assert args[args.index("--network") + 1] == "bridge"

    def test_mount_only_run_workspace(self, tmp_path):
        """挂载仅本次 script_runs 工作目录（唯一 -v）"""
        args, paths = self._args(tmp_path)
        binds = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        assert binds == [f"{paths.run_dir_host}:{paths.run_dir_container}"]
        assert paths.run_dir_container == script_container.CONTAINER_WORKSPACE
        assert "uploads/script_runs" in paths.run_dir_host or str(tmp_path) in paths.run_dir_host

    def test_platform_env_stripped(self, tmp_path):
        """平台 env 剥离：仅显式注入最小集合，无 --env-file、无宿主变量透传"""
        args, _ = self._args(tmp_path)
        envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert sorted(e.split("=", 1)[0] for e in envs) == sorted(script_container.CONTAINER_ENV_ALLOWLIST)
        assert "--env-file" not in args
        for sensitive in ("DATABASE_URL", "REDIS_URL", "LANGFUSE_PUBLIC_KEY", "SSO_LOGIN_URL"):
            assert not any(e.startswith(f"{sensitive}=") for e in envs)

    def test_tail_command_same_shape_as_local(self, tmp_path):
        """尾部命令与 local 同构：[python, pre_judge_entry.py, staged, entry]，
        wrapper 取镜像内路径，脚本无法 import 平台代码（无 PYTHONPATH 注入）"""
        args, paths = self._args(tmp_path)
        image_idx = args.index("ai-evo-script-runner:latest")
        tail = args[image_idx + 1:]
        assert tail == [
            script_container.CONTAINER_PYTHON,
            script_container.CONTAINER_ENTRY_WRAPPER,
            paths.staged_dir_container, "main.py",
        ]
        assert script_container.CONTAINER_ENTRY_WRAPPER.startswith("/opt/runner/")
        assert not any(a.startswith("PYTHONPATH") for a in args)

    def test_workdir_is_workspace_script_dir(self, tmp_path):
        args, paths = self._args(tmp_path)
        assert args[args.index("-w") + 1] == paths.cwd_container
        assert paths.cwd_container.startswith(script_container.CONTAINER_WORKSPACE)


class TestContainerPaths:
    def test_real_task_layout_mounts_run_dir(self, tmp_path):
        """真实任务：cwd=run_dir/{sid}、staged=run_dir/scripts/{key} → 挂载源=run_dir"""
        run_dir = tmp_path / "script_runs" / "task-1" / "data-9"
        cwd = run_dir / "my-script"
        staged = run_dir / "scripts" / "my-script"
        paths = script_container.build_container_paths(run_dir, cwd, staged)
        assert paths.run_dir_host == str(run_dir)
        assert paths.cwd_container == "/workspace/my-script"
        assert paths.staged_dir_container == "/workspace/scripts/my-script"

    def test_debug_layout_uses_common_ancestor(self, tmp_path):
        """debug 端点：cwd（debug/debug/{sid}）不在 run_dir（debug/{name}_{ts}）下
        → 挂载源兜底公共祖先 uploads/script_runs/debug，绝不抛错且不越出 script_runs"""
        base = tmp_path / "script_runs" / "debug"
        run_dir = base / "my-script_20260101_000000_abc"
        cwd = base / "debug" / "my-script"
        staged = run_dir / "scripts" / "my-script"
        paths = script_container.build_container_paths(run_dir, cwd, staged)
        assert paths.run_dir_host == str(base)
        assert paths.cwd_container == "/workspace/debug/my-script"
        assert paths.staged_dir_container == "/workspace/my-script_20260101_000000_abc/scripts/my-script"


class TestRewriteStdinPaths:
    def test_rewrites_run_dir_prefixes_recursively(self, tmp_path):
        run_dir = str(tmp_path / "script_runs" / "t" / "d")
        stdin = {
            "_run_dir": run_dir,
            "fields": {"note": f"{run_dir}/inputs/note.txt"},
            "inputs": {"pkg": {"root": f"{run_dir}/inputs/pkg",
                               "index_path": f"{run_dir}/inputs/pkg_index.json"}},
            "params": {"keep": "/etc/passwd"},
        }
        out = script_container.rewrite_stdin_paths(stdin, run_dir)
        assert out["_run_dir"] == "/workspace"
        assert out["fields"]["note"] == "/workspace/inputs/note.txt"
        assert out["inputs"]["pkg"]["root"] == "/workspace/inputs/pkg"
        assert out["inputs"]["pkg"]["index_path"] == "/workspace/inputs/pkg_index.json"
        assert out["params"]["keep"] == "/etc/passwd"  # 工作区外路径原样
        assert stdin["_run_dir"] == run_dir  # 原对象不被修改

    def test_no_prefix_collision(self, tmp_path):
        """前缀巧合不误伤：debug 工作区不重写 debug-other 的路径"""
        base = str(tmp_path)
        out = script_container.rewrite_stdin_paths(f"{base}/debug-other/x.txt", f"{base}/debug")
        assert out == f"{base}/debug-other/x.txt"
        out2 = script_container.rewrite_stdin_paths(f"{base}/debug", f"{base}/debug")
        assert out2 == "/workspace"
        out3 = script_container.rewrite_stdin_paths(f"{base}/debug/a.json", f"{base}/debug")
        assert out3 == "/workspace/a.json"

    def test_non_string_values_passthrough(self):
        stdin = {"n": 3, "b": True, "none": None, "l": [1, "x"], "d": {"k": {"deep": 1}}}
        out = script_container.rewrite_stdin_paths(stdin, "/some/run_dir")
        assert out == stdin


# ---------------- runner 镜像静态清单（依赖校验事实源） ----------------

class TestRunnerImagePackages:
    def test_packages_txt_matches_static_list(self):
        """同步门禁：docker/script-runner/packages.txt 逐行 == RUNNER_IMAGE_PACKAGES"""
        packages_file = REPO_ROOT / "docker" / "script-runner" / "packages.txt"
        assert packages_file.is_file(), "docker/script-runner/packages.txt 缺失"
        lines = [ln.strip() for ln in packages_file.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        assert lines == script_container.RUNNER_IMAGE_PACKAGES

    def test_base_names_normalized(self):
        names = script_container.runner_package_base_names()
        assert "requests" in names and "pandas" in names and "pillow" in names
        assert all(n == n.lower() for n in names)
        assert len(names) == len(script_container.RUNNER_IMAGE_PACKAGES)

    def test_dockerfile_installs_packages_txt(self):
        """Dockerfile 以 packages.txt 为安装源（不允许旁路 pip install 漂移）"""
        dockerfile = (REPO_ROOT / "docker" / "script-runner" / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY docker/script-runner/packages.txt" in dockerfile
        assert "pip install" in dockerfile and "/tmp/packages.txt" in dockerfile
        assert "USER runner" in dockerfile  # 非 root 镜像默认态


# ---------------- 依赖声明校验分流 ----------------

class TestCheckDeclaredRequirements:
    def test_container_mode_checks_static_list_without_subprocess(self, monkeypatch):
        """container 模式：纯内存对照静态清单，不再子进程探测执行 venv"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
        calls = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: calls.append(a) or pytest.fail("不应子进程探测"))
        # 镜像内已装（requests）→ 通过
        pre_judge_script_service.check_declared_requirements(["requests>=2.31"])
        assert calls == []
        # 镜像内未装 → ValueError，消息指向镜像扩展而非 pip install
        with pytest.raises(ValueError) as ei:
            pre_judge_script_service.check_declared_requirements(["flask>=3.0", "requests"])
        msg = str(ei.value)
        assert "flask" in msg and "runner 镜像" in msg
        assert "pip install flask" not in msg
        assert "packages.txt" in msg

    def test_container_mode_version_spec_ignored(self, monkeypatch):
        """版本约束不参与清单比对（已装集合按包名）：requests==2.30 虽与镜像
        版本不同仍通过——版本漂移治理属镜像重建流程，不在上传阻断范围"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
        pre_judge_script_service.check_declared_requirements(["requests==2.30"])

    def test_local_mode_still_probes_venv(self, monkeypatch, tmp_path):
        """local 模式保持原行为：SCRIPT_PYTHON 子进程探测，缺失提示 pip install"""
        monkeypatch.delenv("SCRIPT_EXEC_MODE", raising=False)
        fake_py = tmp_path / "python"
        fake_py.write_text("#!/bin/sh\nexit 0\n")
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_PYTHON", str(fake_py))

        def fake_run(cmd, **kwargs):
            class P:
                returncode = 0
                stdout = json.dumps(["some-missing-pkg"]).encode()
                stderr = b""
            assert cmd[0] == str(fake_py)
            return P()

        monkeypatch.setattr(pre_judge_script_service.subprocess, "run", fake_run)
        with pytest.raises(ValueError) as ei:
            pre_judge_script_service.check_declared_requirements(["some-missing-pkg>=1.0"])
        assert "pip install" in str(ei.value)

    def test_empty_requirements_short_circuits_both_modes(self, monkeypatch):
        for mode in ("local", "container"):
            monkeypatch.setenv("SCRIPT_EXEC_MODE", mode)
            pre_judge_script_service.check_declared_requirements([])


# ---------------- 执行分流 ----------------

_NET_SRC = (
    "def run(stdin):\n"
    "    return {\"status\": \"ok\", \"output\": {\"echo\": stdin.get(\"task_id\")}, \"summary\": \"ok\"}\n"
)


def _make_script_pkg(upload_base: Path, script_id: str) -> Path:
    """构造上传脚本包：def run(stdin) 函数式入口"""
    pkg = upload_base / script_id
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "main.py").write_text(_NET_SRC, encoding="utf-8")
    return pkg


def _script_def(script_id: str) -> dict:
    return {"script_id": script_id, "name": script_id, "exec_type": "script",
            "entrypoint": "main.py", "timeout_seconds": 30}


@pytest.fixture
def fake_script_db(tmp_path, monkeypatch):
    """内存库会话工厂 + 上传包落盘（替换 runner.SessionLocal，模式同 test_pre_judge_runner）"""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.orm import sessionmaker
    from app.db.session import Base
    from app.models.pre_judge_models import PreJudgeScript

    upload_base = tmp_path / "uploads" / "pre_judge_scripts"
    monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", upload_base)

    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        for sid in ("pjs-demo", "pjs-net", "pjs-nonet"):
            db.add(PreJudgeScript(id=sid, name=sid, exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
            _make_script_pkg(upload_base, sid)
        db.commit()
    monkeypatch.setattr(pre_judge_runner, "SessionLocal", Session)
    return Session


class TestExecuteDispatch:
    def test_container_mode_routes_to_docker_channel(self, monkeypatch, tmp_path):
        """SCRIPT_EXEC_MODE=container：执行进 docker run 通道，前置准备共用
        （脚本包从工作区副本执行、entry 相对路径不变）"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
        upload_base = tmp_path / "uploads" / "pre_judge_scripts"
        monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", str(upload_base))
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "runs")
        _make_script_pkg(upload_base, "pjs-demo")

        captured = {}
        sentinel = pre_judge_runner._result("pjs-demo", "pjs-demo", "ok", finish_reason="completed")

        def fake_container_exec(**kwargs):
            captured.update(kwargs)
            return sentinel

        monkeypatch.setattr(script_container, "execute_script_in_container", fake_container_exec)
        result = pre_judge_runner.execute_script(_script_def("pjs-demo"), {"task_id": "t1", "data_id": "d1"})

        assert result is sentinel
        assert captured["entry_rel"] == "main.py"
        assert captured["network"] is False
        # 副本在工作区 scripts/ 下（严格拷贝语义）
        assert str(captured["staged_dir_host"]).startswith(str(tmp_path / "runs"))
        assert Path(captured["staged_dir_host"]) / "main.py"

    def test_local_mode_does_not_enter_container_channel(self, monkeypatch, tmp_path):
        """local 模式（默认）：不走容器分支，真实子进程执行成功（回归护栏）"""
        monkeypatch.delenv("SCRIPT_EXEC_MODE", raising=False)
        upload_base = tmp_path / "uploads" / "pre_judge_scripts"
        monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", str(upload_base))
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "runs")
        _make_script_pkg(upload_base, "pjs-demo")

        def fail_exec(**kwargs):  # pragma: no cover - 被调即失败
            pytest.fail("local 模式不应进入容器执行通道")

        monkeypatch.setattr(script_container, "execute_script_in_container", fail_exec)
        result = pre_judge_runner.execute_script(_script_def("pjs-demo"),
                                                 {"task_id": "t1", "data_id": "d1"})
        assert result["status"] == "ok"
        assert result["output"] == {"echo": "t1"}

    def test_network_flag_from_orchestration_step(self, monkeypatch, fake_script_db):
        """任务级网络开关：编排 step 声明 network=true → 透传容器执行参数"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")

        captured = {}
        monkeypatch.setattr(
            script_container, "execute_script_in_container",
            lambda **kw: captured.update(kw)
            or pre_judge_runner._result("pjs-net", "pjs-net", "ok", finish_reason="completed"),
        )
        step = {"script_id": "pjs-net", "network": True}
        result = pre_judge_runner.run_pre_scripts(
            type("T", (), {"id": "task-net", "pre_scripts": [step]})(),
            dataset_item={"id": "d-1", "data": {}},
        )
        assert result["pre_script_results"][0]["status"] == "ok"
        assert captured["network"] is True

    def test_network_flag_defaults_off(self, monkeypatch, fake_script_db):
        """未声明 network → 默认 --network none（任务级开关默认关）"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")

        captured = {}
        monkeypatch.setattr(
            script_container, "execute_script_in_container",
            lambda **kw: captured.update(kw)
            or pre_judge_runner._result("pjs-nonet", "pjs-nonet", "ok", finish_reason="completed"),
        )
        pre_judge_runner.run_pre_scripts(
            type("T", (), {"id": "task-nonet",
                           "pre_scripts": [{"script_id": "pjs-nonet"}]})(),
            dataset_item={"id": "d-1", "data": {}},
        )
        assert captured["network"] is False


# ---------------- 冒烟 container 通道 ----------------

class TestSmokeContainerChannel:
    def test_container_smoke_copies_pkg_rewrites_paths_and_runs_docker(self, monkeypatch, tmp_path):
        """container 冒烟：入口包拷进临时工作区副本、stdin 路径重写为 /workspace、
        经 docker run 参数执行、结果解析与 local 同一断言集"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
        entry = _make_script_pkg(tmp_path / "upload_pkg", "pjs-demo") / "main.py"

        captured = {}

        class FakeProc:
            returncode = 0
            stdout = b'{\"status\": \"ok\", \"output\": {\"smoke\": 1}}\n'
            stderr = b""

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["input"] = kwargs.get("input")
            return FakeProc()

        monkeypatch.setattr(pre_judge_script_service.subprocess, "run", fake_run)
        result = pre_judge_script_service.run_smoke_test(entry)

        assert result == {"status": "ok", "output": {"smoke": 1}}
        args = captured["args"]
        assert args[0] == script_container.docker_binary() or args[0] == "docker"
        assert args[1:3] == ["run", "--rm"]
        assert args[args.index("--network") + 1] == "none"
        # 尾部命令指向镜像内 wrapper 与工作区副本
        assert script_container.CONTAINER_ENTRY_WRAPPER in args
        staged_arg = args[args.index(script_container.CONTAINER_ENTRY_WRAPPER) + 1]
        assert staged_arg.startswith("/workspace/scripts/")
        # stdin 路径重写：payload 内无宿主临时路径
        payload = json.loads(captured["input"].decode("utf-8"))
        assert payload["_run_dir"] == "/workspace"
        assert payload["fields"]["note"].startswith("/workspace/")

    def test_container_smoke_failure_hints_image_preload(self, monkeypatch, tmp_path):
        """镜像不可用（docker run 退出码 125 + 提示镜像名）→ 冒烟错误含预载指引"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
        entry = _make_script_pkg(tmp_path / "upload_pkg", "pjs-demo") / "main.py"

        class FakeProc:
            returncode = 125
            stdout = b""
            stderr = b"No such image: ai-evo-script-runner:latest"

        monkeypatch.setattr(pre_judge_script_service.subprocess, "run", lambda *a, **k: FakeProc())
        with pytest.raises(ValueError) as ei:
            pre_judge_script_service.run_smoke_test(entry)
        assert "docker/script-runner/README.md" in str(ei.value)

    def test_local_smoke_unchanged(self, monkeypatch, tmp_path):
        """local 冒烟回归：仍然 SCRIPT_PYTHON + 白名单 env 直跑（不走 docker）"""
        monkeypatch.delenv("SCRIPT_EXEC_MODE", raising=False)
        entry = _make_script_pkg(tmp_path / "pkg", "pjs-smoke") / "main.py"
        result = pre_judge_script_service.run_smoke_test(entry, timeout=30)
        assert result["status"] == "ok"

    def test_parse_stdout_shared_assertions(self):
        with pytest.raises(ValueError, match="标准输出为空"):
            pre_judge_script_service._smoke_parse_stdout("", "")
        with pytest.raises(ValueError, match="不是合法 JSON"):
            pre_judge_script_service._smoke_parse_stdout("hello\nworld", "")
        with pytest.raises(ValueError, match="必须是对象"):
            pre_judge_script_service._smoke_parse_stdout("[1,2]", "")


# ---------------- 超时兜底与 docker 失败提示 ----------------

class TestTimeoutTerminate:
    def test_graceful_term_lets_cli_exit_no_force_rm(self, monkeypatch):
        """SIGTERM 后 CLI 随容器退出 → 不需要 docker rm -f 兜底"""
        monkeypatch.setattr(script_container, "CONTAINER_TERM_GRACE_SECONDS", 5.0)
        monkeypatch.setattr(script_container, "docker_binary", lambda: "true")
        proc = subprocess.Popen(["sleep", "0.1"])
        script_container._terminate_docker_run(proc, "aievo-script-x")
        assert proc.returncode is not None

    def test_force_kill_then_rm_fallback(self, monkeypatch):
        """CLI 无视 SIGTERM → 宽限期后 SIGKILL + docker rm -f 兜底回收容器"""
        monkeypatch.setattr(script_container, "CONTAINER_TERM_GRACE_SECONDS", 0.2)
        monkeypatch.setattr(script_container, "docker_binary", lambda: "true")
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import signal,time\n"
                                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                 "time.sleep(30)"])
        script_container._terminate_docker_run(proc, "aievo-script-x")
        assert proc.returncode is not None  # 已被 kill 并回收


class TestDockerFailureHint:
    def test_image_missing_hint(self):
        hint = script_container._docker_failure_hint(
            125, "No such image: ai-evo-script-runner:latest", "ai-evo-script-runner:latest")
        assert "README.md" in hint

    def test_other_daemon_rejection_hint(self):
        hint = script_container._docker_failure_hint(125, "daemon not running", "img:1")
        assert "125" in hint

    def test_no_hint_for_non_125(self):
        assert script_container._docker_failure_hint(1, "x", "img:1") == ""


# ---------------- 容器执行返回结构同构（mock docker CLI） ----------------

class TestExecuteScriptInContainerShape:
    def test_success_result_matches_local_shape(self, monkeypatch, tmp_path):
        """真 docker CLI 换成假脚本：验证 execute_script_in_container 的返回
        条目与 local 模式同构（status/output/files/summary/static_check/duration_ms）"""
        fake_cli = tmp_path / "fake_docker.py"
        fake_cli.write_text(
            f"#!{sys.executable}\n"
            "import sys, json\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'status': 'ok', 'output': {'v': 1}, 'summary': 'done'}))\n",
            encoding="utf-8",
        )
        fake_cli.chmod(0o755)
        monkeypatch.setattr(script_container, "docker_binary", lambda: str(fake_cli))
        run_dir = tmp_path / "runs" / "t" / "d"
        staged = run_dir / "scripts" / "s"
        staged.mkdir(parents=True)
        cwd = run_dir / "s"
        cwd.mkdir()
        result = script_container.execute_script_in_container(
            script={"script_id": "s", "name": "s"},
            staged_dir_host=staged, entry_rel="main.py",
            run_dir_host=run_dir, cwd_host=cwd,
            stdin={"task_id": "t", "data_id": "d", "_run_dir": str(run_dir), "fields": {}},
            timeout=30, started=__import__("time").time(), static_check=["w1"],
        )
        assert result["status"] == "ok"
        assert result["output"] == {"v": 1}
        assert result["summary"] == "done"
        assert result["static_check"] == ["w1"]
        assert "duration_ms" in result and "finish_reason" in result
