"""ARCH-3-4 gVisor 可选运行时档单元测试（L1，全 mock，无 docker/网络/DB）

设计：plans/2026-10-15-企业级评测系统架构设计.md §3 Phase 3 表 3-4 行——
「（可选）宿主机安装 gVisor(runsc)，沙箱加 ``--runtime=runsc`` 加固档，
高危脚本专用；验收：沙箱逃逸测试集在 runsc 档复跑通过」。任务口径：
config 增 SANDBOX_RUNTIME（默认 runc，白名单仅 runc|runsc）；script_container
与 executor 白名单透传 --runtime 且非法值拒绝。

覆盖（完成标准逐项）：
- 默认行为不变：SANDBOX_RUNTIME 未配置 / 显式 runc → 两侧构造的 docker run
  参数不含 ``--runtime``（runc 即 docker daemon 缺省运行时，缺省参数与历史
  逐字节一致）
- 透传：runsc → docker run 参数含 ``--runtime runsc``——worker docker-CLI
  通道（SANDBOX_RUNTIME → sandbox_runtime → SandboxRunSpec.runtime →
  DockerCliDriver Popen 参数）与 executor 通道（SANDBOX_RUNTIME → spec →
  HTTP 请求 runtime 字段（M8 QE8-3 客户端接线收口）→ executor 白名单 →
  daemon CLI 参数）两条链
- 拒绝：非法值（kata/空/flag 样值/大写变形）四层闸——Settings 启动
  ValidationError、sandbox_params 构造 SandboxParamError、worker 读取
  SandboxDriverError（转脚本 fail 条目，不炸编排）、executor 400（不触碰
  docker CLI，拒绝即审计 WARNING）
- 单一事实源：CONTAINER_RUNTIMES 与 Settings 校验同源（runc|runsc）

实机 runsc 档逃逸集复跑属 sandbox_e2e 档（部署机 -m sandbox_e2e 显式执行，
见 docs/sandbox-runtime-gvisor.md §三），本文件不触及 docker。
"""
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

pytestmark = pytest.mark.unit  # W8 分类门禁：L1 单元测试（无网络/无 DB）

from app.core.config import Settings
from app.services import script_container
from app.services.script_container import (
    DockerCliDriver,
    SandboxDriver,
    SandboxDriverError,
    SandboxHandle,
    SandboxRunSpec,
    SandboxRunStatus,
)
from executor import app as executor_app
from executor import sandbox_params
from executor.sandbox_params import SandboxParamError

TOKEN = "unit-test-executor-token"


# ---------------- Settings 配置闸（ARCH-3-4） ----------------

class TestSettingsSandboxRuntime:
    def test_field_default_is_runc(self):
        """字段级：缺省值就是 runc（不读 .env/环境变量，跨环境确定）"""
        assert Settings.model_fields["SANDBOX_RUNTIME"].default == "runc"

    def test_clean_settings_resolves_default_runc(self, monkeypatch):
        """解析级：env 未设 + 干净 Settings（_env_file=None 绕开部署 .env）→ runc"""
        monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
        settings = Settings(_env_file=None)
        assert settings.SANDBOX_RUNTIME == "runc"

    def test_runsc_accepted_and_whitespace_stripped(self):
        assert Settings(_env_file=None, SANDBOX_RUNTIME="runsc").SANDBOX_RUNTIME == "runsc"
        assert Settings(_env_file=None, SANDBOX_RUNTIME="  runc ").SANDBOX_RUNTIME == "runc"

    @pytest.mark.parametrize("bad", ["kata", "RUNSC", "", "runc runsc", "runsc;rm"])
    def test_invalid_values_rejected_at_startup(self, bad):
        """白名单仅 runc|runsc：非法值启动即拒（field_validator fail-fast，
        不带病进沙箱通道）——大小写敏感（docker 运行时名即小写）"""
        with pytest.raises(ValidationError):
            Settings(_env_file=None, SANDBOX_RUNTIME=bad)

    def test_whitelist_single_source_with_sandbox_params(self):
        """Settings 校验与沙箱参数构造共用一份白名单（executor/sandbox_params）"""
        assert sandbox_params.CONTAINER_RUNTIMES == ("runc", "runsc")
        assert Settings.model_fields["SANDBOX_RUNTIME"].default \
            == sandbox_params.DEFAULT_RUNTIME


# ---------------- sandbox_params 构造层（单一事实源） ----------------

def _build_args(**overrides):
    """最小合法参数调 build_docker_run_args（runtime 经 overrides 覆盖）"""
    kwargs = dict(
        image="ai-evo-script-runner:latest",
        user="10001:10001",
        mount_host="/srv/uploads/script_runs/t1/d1",
        mount_container="/workspace",
        workdir_container="/workspace",
        network_name="none",
        container_name="aievo-script-s1-abc123",
        tail_command=["python", "/opt/runner/pre_judge_entry.py",
                      "/workspace/scripts/s1", "main.py"],
    )
    kwargs.update(overrides)
    return sandbox_params.build_docker_run_args(**kwargs)


class TestBuildDockerRunArgsRuntime:
    def test_default_no_runtime_flag(self):
        """默认行为不变：缺省不拼 --runtime（runc 即 daemon 缺省，参数与历史
        逐字节一致）"""
        args = _build_args()
        assert "--runtime" not in args
        assert not any(a.startswith("--runtime=") for a in args)

    def test_explicit_runc_still_no_flag(self):
        """显式 runc 同样不拼旗标——避免钉死 daemon 级自定义缺省运行时"""
        args = _build_args(runtime="runc")
        assert "--runtime" not in args

    def test_runsc_appends_runtime_flag(self):
        """加固档透传：runsc → ``--runtime runsc``（gVisor，高危脚本专用）"""
        args = _build_args(runtime="runsc")
        assert args[args.index("--runtime") + 1] == "runsc"

    @pytest.mark.parametrize("bad", ["kata", "", "-v", "--privileged", "runsc extra"])
    def test_invalid_runtime_rejected(self, bad):
        """非法值拒绝：非白名单 / 旗标样值一律 SandboxParamError（两通道共用闸）"""
        with pytest.raises(SandboxParamError):
            _build_args(runtime=bad)

    def test_validate_container_runtime_whitelist(self):
        assert sandbox_params.validate_container_runtime("runc") == "runc"
        assert sandbox_params.validate_container_runtime("runsc") == "runsc"
        with pytest.raises(SandboxParamError, match="不在白名单"):
            sandbox_params.validate_container_runtime("kata")
        with pytest.raises(SandboxParamError):  # 旗标闸（防值即旗标）
            sandbox_params.validate_container_runtime("--runtime")


# ---------------- worker 通道（script_container） ----------------

class TestSandboxRuntimeGetter:
    def test_default_runc(self, monkeypatch):
        monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
        monkeypatch.setattr(script_container, "get_settings",
                            lambda: Settings(_env_file=None))
        assert script_container.sandbox_runtime() == "runc"

    def test_env_runsc_wins_realtime(self, monkeypatch):
        """环境变量实时读优先（改 env 重启 worker 即切换加固档）"""
        monkeypatch.setenv("SANDBOX_RUNTIME", "runsc")
        assert script_container.sandbox_runtime() == "runsc"

    def test_invalid_env_rejected_not_silently_downgraded(self, monkeypatch):
        """非法值 fail-loud（SandboxDriverError）：运行时档回退是放松（加固档
        悄悄降回 runc，高危脚本看似在 gVisor 下实际没有），不得静默降级——
        与 SCRIPT_NET_PROFILE 非法回退 none（收紧）方向相反"""
        monkeypatch.setenv("SANDBOX_RUNTIME", "kata")
        with pytest.raises(SandboxDriverError, match="SANDBOX_RUNTIME"):
            script_container.sandbox_runtime()

    def test_spec_field_defaults_runc(self):
        """SandboxRunSpec.runtime 缺省 runc：既有 spec 构造面零改动兼容"""
        spec = SandboxRunSpec(
            image="ai-evo-script-runner:latest", user="10001:10001",
            workspace_host="/w", workspace_container="/workspace",
            workdir_container="/workspace", cwd_host="/w/s1",
            net_profile="none", container_name="c",
            entrypoint=["python"], stdin_payload=b"{}", timeout=30.0)
        assert spec.runtime == "runc"


def _workspace(tmp_path: Path):
    """真实任务布局的工作区（test_sandbox_driver._workspace 同构）"""
    run_dir = tmp_path / "runs" / "t1" / "d1"
    staged = run_dir / "scripts" / "s1"
    staged.mkdir(parents=True, exist_ok=True)
    cwd = run_dir / "s1"
    cwd.mkdir(parents=True, exist_ok=True)
    return run_dir, staged, cwd


class _FakeScriptDriver(SandboxDriver):
    """假沙箱驱动：记录 spec 并落 ok stdout（模拟容器执行），供编排层断言"""

    name = "fake-script"

    def __init__(self):
        self.spec = None

    def run(self, spec: SandboxRunSpec) -> SandboxHandle:
        self.spec = spec
        Path(spec.cwd_host).mkdir(parents=True, exist_ok=True)
        (Path(spec.cwd_host) / "stdout.log").write_text(
            json.dumps({"status": "ok", "output": {"echo": 1}}) + "\n",
            encoding="utf-8")
        (Path(spec.cwd_host) / "stderr.log").write_text("", encoding="utf-8")
        return SandboxHandle(run_id=spec.container_name, container_name=spec.container_name)

    def wait(self, handle: SandboxHandle, timeout: float) -> SandboxRunStatus:
        return SandboxRunStatus(state="done", exit_code=0)

    def kill(self, handle: SandboxHandle) -> None:  # pragma: no cover - 假驱动不触发
        pass

    def status(self, handle: SandboxHandle) -> SandboxRunStatus:  # pragma: no cover
        return SandboxRunStatus(state="done", exit_code=0)


class _DenyDriver(SandboxDriver):
    """拒绝哨兵驱动：run() 被调即失败（非法配置必须在驱动受理前拒绝）"""

    name = "deny"

    def run(self, spec: SandboxRunSpec) -> SandboxHandle:  # pragma: no cover
        raise AssertionError("非法 SANDBOX_RUNTIME 不应进入驱动受理")

    def wait(self, handle: SandboxHandle, timeout: float) -> SandboxRunStatus:  # pragma: no cover
        raise AssertionError("不应等待")

    def kill(self, handle: SandboxHandle) -> None:  # pragma: no cover
        pass

    def status(self, handle: SandboxHandle) -> SandboxRunStatus:  # pragma: no cover
        raise AssertionError("不应查询")


def _run_in_container(tmp_path, driver, **kwargs):
    run_dir, staged, cwd = _workspace(tmp_path)
    return script_container.execute_script_in_container(
        script={"script_id": "s1", "name": "s1"},
        staged_dir_host=staged, entry_rel="main.py",
        run_dir_host=run_dir, cwd_host=cwd,
        stdin={"task_id": "t1", "data_id": "d1", "_run_dir": str(run_dir), "fields": {}},
        timeout=30, started=time.time(), driver=driver, **kwargs)


class TestWorkerOrchestrationPassThrough:
    def test_default_runc_reaches_driver(self, tmp_path, monkeypatch):
        """默认档：编排层把 runc 透传进 spec（驱动构造不拼旗标由构造层保证）"""
        monkeypatch.delenv("SANDBOX_RUNTIME", raising=False)
        monkeypatch.setattr(script_container, "get_settings",
                            lambda: Settings(_env_file=None))
        driver = _FakeScriptDriver()
        result = _run_in_container(tmp_path, driver)
        assert result["status"] == "ok"
        assert driver.spec.runtime == "runc"

    def test_runsc_reaches_driver_spec(self, tmp_path, monkeypatch):
        """加固档透传：SANDBOX_RUNTIME=runsc → SandboxRunSpec.runtime=runsc"""
        monkeypatch.setenv("SANDBOX_RUNTIME", "runsc")
        driver = _FakeScriptDriver()
        assert _run_in_container(tmp_path, driver)["status"] == "ok"
        assert driver.spec.runtime == "runsc"

    def test_invalid_runtime_becomes_script_fail_entry(self, tmp_path, monkeypatch):
        """非法配置转脚本 fail 条目（finish_reason=error），不炸整条编排，
        且驱动未被触碰（受理前拒绝）"""
        monkeypatch.setenv("SANDBOX_RUNTIME", "kata")
        result = _run_in_container(tmp_path, _DenyDriver())
        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "SANDBOX_RUNTIME" in result["error"] and "kata" in result["error"]


class TestHttpExecutorClientRuntime:
    """HTTP executor 通道客户端环（M8 QE8-3 接线收口）：spec.runtime →
    POST /sandbox/run 请求体 runtime 字段——SANDBOX_RUNTIME=runsc 对
    docker-CLI / executor 两通道同档生效（复用 test_sandbox_driver 的
    mock executor，替换唯一 I/O 出口 _http_open，不经真实网络）"""

    @pytest.fixture
    def mock_executor(self, monkeypatch):
        from tests.unit.test_sandbox_driver import EXECUTOR_URL, MockExecutor

        monkeypatch.setenv("EXECUTOR_URL", EXECUTOR_URL)
        monkeypatch.setenv("EXECUTOR_TOKEN", "unit-test-executor-token")
        mock = MockExecutor()
        from app.services import executor_client
        monkeypatch.setattr(executor_client, "_http_open", mock)
        return mock

    def test_runsc_spec_reaches_request_runtime_field(self, tmp_path, mock_executor):
        """spec.runtime=runsc → 请求体 runtime=runsc（加固档不因走 executor
        通道静默降回 runc）"""
        from app.services.executor_client import HttpExecutorDriver

        run_dir, _, cwd = _workspace(tmp_path)
        spec = SandboxRunSpec(
            image="ai-evo-script-runner:latest", user="10001:10001",
            workspace_host=str(run_dir), workspace_container="/workspace",
            workdir_container="/workspace/s1", cwd_host=str(cwd),
            net_profile="none", container_name="aievo-script-s1-abc123",
            entrypoint=["python"], stdin_payload=b"{}", timeout=30.0,
            runtime="runsc")
        HttpExecutorDriver().run(spec)
        assert mock_executor.posts()[0]["body"]["runtime"] == "runsc"

    def test_default_spec_sends_runc(self, tmp_path, mock_executor):
        """spec 缺省 runc 同样显式携带（executor 同值缺省，行为等价不变）"""
        from app.services.executor_client import HttpExecutorDriver

        run_dir, _, cwd = _workspace(tmp_path)
        spec = SandboxRunSpec(
            image="ai-evo-script-runner:latest", user="10001:10001",
            workspace_host=str(run_dir), workspace_container="/workspace",
            workdir_container="/workspace/s1", cwd_host=str(cwd),
            net_profile="none", container_name="aievo-script-s1-abc123",
            entrypoint=["python"], stdin_payload=b"{}", timeout=30.0)
        HttpExecutorDriver().run(spec)
        assert mock_executor.posts()[0]["body"]["runtime"] == "runc"


class _FakePopen:
    """捕获 Popen 参数的假子进程（DockerCliDriver.run 的 docker CLI 调用）"""

    def __init__(self, args, **kwargs):
        self.args = list(args)
        self.stdin = self

    def write(self, data):  # stdin 载荷写入（丢弃）
        pass

    def close(self):
        pass


class TestDockerCliDriverRuntimeFlag:
    def _capture_popen(self, monkeypatch):
        """替换 Popen 为假子进程（记录 docker CLI 参数，丢弃 stdin 载荷）"""
        captured = {}

        def _fake_popen(args, **kwargs):
            captured["args"] = list(args)
            return _FakePopen(args, **kwargs)

        monkeypatch.setattr(script_container.subprocess, "Popen", _fake_popen)
        return captured

    def test_runsc_reaches_docker_cli_args(self, tmp_path, monkeypatch):
        """CLI 驱动链路：spec.runtime=runsc → docker run 参数含 --runtime runsc"""
        captured = self._capture_popen(monkeypatch)
        run_dir, _, cwd = _workspace(tmp_path)
        spec = SandboxRunSpec(
            image="ai-evo-script-runner:latest", user="10001:10001",
            workspace_host=str(run_dir), workspace_container="/workspace",
            workdir_container="/workspace/s1", cwd_host=str(cwd),
            net_profile="none", container_name="aievo-script-s1-abc123",
            entrypoint=["python", "/opt/runner/pre_judge_entry.py",
                        "/workspace/scripts/s1", "main.py"],
            stdin_payload=b"{}", timeout=30.0, runtime="runsc")
        DockerCliDriver().run(spec)
        args = captured["args"]
        assert args[0] == "docker"
        assert args[args.index("--runtime") + 1] == "runsc"

    def test_default_runc_no_runtime_flag_in_cli_args(self, tmp_path, monkeypatch):
        """CLI 驱动默认档：spec.runtime=runc（缺省）→ 参数无 --runtime（不变）"""
        captured = self._capture_popen(monkeypatch)
        run_dir, _, cwd = _workspace(tmp_path)
        spec = SandboxRunSpec(
            image="ai-evo-script-runner:latest", user="10001:10001",
            workspace_host=str(run_dir), workspace_container="/workspace",
            workdir_container="/workspace/s1", cwd_host=str(cwd),
            net_profile="none", container_name="aievo-script-s1-abc123",
            entrypoint=["python"], stdin_payload=b"", timeout=30.0)
        DockerCliDriver().run(spec)
        assert "--runtime" not in captured["args"]


# ---------------- executor 通道（HTTP 服务） ----------------

def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _valid_request(root: Path, **overrides) -> dict:
    payload = {
        "image": "ai-evo-script-runner:latest",
        "workspace": str(root / "t1" / "d1"),
        "net_profile": "none",
        "timeout": 30.0,
        "entrypoint": ["python", "/opt/runner/pre_judge_entry.py",
                       "/workspace/scripts/s1", "main.py"],
    }
    payload.update(overrides)
    return payload


class _DenyDocker:
    """默认 docker 记录器：被调即失败（拒绝用例不得触碰 docker CLI）"""

    def __call__(self, args, **kwargs):
        raise AssertionError(f"该用例不应调用 docker CLI: {args}")


class FakeDocker:
    """假 docker CLI（run 即成功）；记录调用供 --runtime 断言"""

    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        sub = args[1] if len(args) > 1 else ""
        if sub == "run":
            return subprocess.CompletedProcess(args, 0, stdout=b"f4kec0nta1nerid", stderr=b"")
        if sub == "wait":
            return subprocess.CompletedProcess(args, 0, stdout=b"0", stderr=b"")
        if sub == "logs":
            return subprocess.CompletedProcess(args, 0, stdout=b'{"status": "ok"}\n', stderr=b"")
        if sub == "rm":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"未预期的 docker 调用: {args}")


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv(executor_app.EXECUTOR_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(executor_app.subprocess, "run", _DenyDocker())
    return TestClient(executor_app.app)


@pytest.fixture
def ws_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "uploads" / "script_runs"
    (root / "t1" / "d1").mkdir(parents=True)
    monkeypatch.setattr(sandbox_params, "WORKSPACE_ROOT", str(root))
    return root


@pytest.fixture
def fake_docker(monkeypatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(executor_app.subprocess, "run", fake)
    return fake


class TestExecutorRuntimePassThrough:
    def test_default_request_no_runtime_flag(self, client, ws_root, fake_docker):
        """默认行为不变：请求不带 runtime（既有调用方）→ runc 缺省，
        daemon CLI 参数无 --runtime"""
        resp = client.post("/sandbox/run", json=_valid_request(ws_root), headers=_auth())
        assert resp.status_code == 202
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert "--runtime" not in run_cmd

    def test_runsc_request_appends_runtime_flag(self, client, ws_root, fake_docker):
        """加固档透传：请求 runtime=runsc → docker run 参数含 --runtime runsc"""
        payload = _valid_request(ws_root, runtime="runsc")
        resp = client.post("/sandbox/run", json=payload, headers=_auth())
        assert resp.status_code == 202
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert run_cmd[run_cmd.index("--runtime") + 1] == "runsc"

    def test_explicit_runc_request_no_flag(self, client, ws_root, fake_docker):
        """显式 runc 请求同样不拼旗标（与 worker 通道口径一致）"""
        resp = client.post("/sandbox/run", json=_valid_request(ws_root, runtime="runc"),
                           headers=_auth())
        assert resp.status_code == 202
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert "--runtime" not in run_cmd

    def test_runtime_echoed_in_status_and_audit(self, client, ws_root, fake_docker, caplog):
        """runtime 随全量请求参数落审计（accepted）与状态查询回显（model_dump）"""
        resp = client.post("/sandbox/run", json=_valid_request(ws_root, runtime="runsc"),
                           headers=_auth())
        run_id = resp.json()["run_id"]
        assert client.get(f"/sandbox/runs/{run_id}", headers=_auth()).json() \
            ["request"]["runtime"] == "runsc"
        assert any("accepted" in r.getMessage() and "'runtime': 'runsc'" in r.getMessage()
                   for r in caplog.records)


class TestExecutorRuntimeRejects:
    @pytest.mark.parametrize("bad_runtime", ["kata", "", "-v", "--privileged", "runsc extra"])
    def test_invalid_runtime_rejected_400_without_touching_docker(
            self, client, ws_root, bad_runtime):
        """非法运行时 400（白名单仅 runc|runsc；空/旗标样值同拒），
        client fixture 的 deny docker 保证不触碰 CLI"""
        resp = client.post("/sandbox/run", json=_valid_request(ws_root, runtime=bad_runtime),
                           headers=_auth())
        assert resp.status_code == 400
        assert "容器运行时" in resp.json()["detail"]

    def test_rejection_audited_with_reason(self, client, ws_root, caplog):
        """拒绝即审计 WARNING（含原因与原始参数）——与既有白名单拒绝同口径"""
        resp = client.post("/sandbox/run", json=_valid_request(ws_root, runtime="kata"),
                           headers=_auth())
        assert resp.status_code == 400
        assert any("reject" in r.getMessage() and "kata" in r.getMessage()
                   for r in caplog.records)
