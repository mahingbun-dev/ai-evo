"""ARCH-3-2 HarnessAdapter 被测 agent 沙箱化单元测试（全 mock docker）

设计：plans/2026-10-15-企业级评测系统架构设计.md §2.3 D8 / §3 Phase 3 表 3-2 行
——exec_type=claude_agent/deepseek_agent 的前处理步骤经 SandboxDriver 在沙箱内
执行被测 agent（同一 executor 驱动、同一 script_runs 工作区语义、镜像按
exec_type 选 Dockerfile.harness 变体、出网恒 egress 档）。

覆盖链路（docker 全 mock，FakeHarnessDriver 模拟容器侧 harness_entry 行为）：
- payload 构造：变体解析/指令文本/凭据组装（harness.json 三件套 staged 工作区）
- driver 调用参数：镜像选择（缺省+env 覆盖）、egress 档、隔离常量、entrypoint
  argv 契约、stdin 宿主路径重写
- 产物回收：stdout/stderr 回写工作区、files 容器路径逆映射、artifacts/ 自动
  并入、超时/退出码/驱动错误失败条目
- HTTP executor 通道（生产标准沙箱形态）：真实 HttpExecutorDriver 组包（mock
  传输层 _http_open），请求体 workdir == /workspace/{script_id}（三件套裸相对名
  依赖 -w 指向 per-script cwd），全流程日志归位与结果解析同构
- 评分输入：run_pre_scripts 编排 → script_outputs/script_files（宿主路径）

真容器执行属 sandbox_e2e 档（默认 deselect，pytest.ini），本文件不触及 docker。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.pre_judge_models import PreJudgeScript
from app.services import harness_adapter, pre_judge_runner, script_container
from app.services.claude_code_config_service import ClaudeCodeConfigService
from app.services.deepseek_harness_config_service import DeepSeekHarnessConfigService
from app.services.script_container import (
    CONTAINER_HARNESS_ENTRY,
    CONTAINER_PYTHON,
    CONTAINER_WORKSPACE,
    SandboxDriver,
    SandboxDriverError,
    SandboxHandle,
    SandboxRunStatus,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB/docker 全 mock）

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_HARNESS = REPO_ROOT / "docker" / "script-runner" / "Dockerfile.harness"


# ---------------- FakeHarnessDriver：模拟容器侧 harness_entry（无 docker） ----------------

class FakeHarnessDriver(SandboxDriver):
    """假沙箱驱动：run() 在 spec.cwd_host 落 stdout.log/stderr.log 与 artifacts/
    （模拟 docker run 重定向 + 镜像内 harness_entry 写产物），wait() 返回配置终态。

    report_files：stdout JSON files 里声明的容器路径产物（同时落盘宿主对应位），
    模拟 agent 写工作区文件并在结果 JSON 声明。
    """

    name = "fake-harness"

    def __init__(self, output=None, stdout_lines=None, state="done", exit_code=0,
                 error="", stderr_tail="", report_files=None, artifacts=("tool_calls.jsonl",),
                 fail_run=None):
        self.spec = None
        self.wait_timeout = None
        self.run_count = 0
        self._output = output if output is not None else {"verdict": "pass", "score": 0.9}
        self._stdout_lines = stdout_lines
        self._state = state
        self._exit_code = exit_code
        self._error = error
        self._stderr_tail = stderr_tail
        self._report_files = report_files or {}
        self._artifacts = artifacts
        self._fail_run = fail_run

    def run(self, spec: "script_container.SandboxRunSpec") -> SandboxHandle:
        if self._fail_run:
            raise SandboxDriverError(self._fail_run)
        self.spec = spec
        self.run_count += 1
        cwd = Path(spec.cwd_host)
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / "stderr.log").write_text(self._stderr_tail, encoding="utf-8")
        # 模拟 agent 声明的产物：宿主落盘 + stdout JSON 里给容器路径
        files = {}
        for name, rel in self._report_files.items():
            host_path = cwd / rel
            host_path.parent.mkdir(parents=True, exist_ok=True)
            host_path.write_text(f"content-of-{name}", encoding="utf-8")
            files[name] = f"{spec.workdir_container}/{rel}"
        # 模拟 harness_entry 写工具调用记录（D8：采集回工作区）
        artifacts_dir = cwd / script_container.HARNESS_ARTIFACTS_DIR
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        for item in self._artifacts:
            (artifacts_dir / item).write_text('{"tool": "Read"}\n', encoding="utf-8")
        envelope = {"status": "ok", "output": self._output, "files": files,
                    "summary": "agent done", "finish_reason": "completed", "error": None}
        lines = self._stdout_lines if self._stdout_lines is not None else [
            "claude-agent 启动中…", json.dumps(envelope, ensure_ascii=False)]
        (cwd / "stdout.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return SandboxHandle(run_id=spec.container_name, container_name=spec.container_name)

    def wait(self, handle: SandboxHandle, timeout: float) -> SandboxRunStatus:
        self.wait_timeout = timeout
        return SandboxRunStatus(state=self._state, exit_code=self._exit_code, error=self._error)

    def kill(self, handle: SandboxHandle) -> None:  # pragma: no cover - 假驱动不触发
        pass

    def status(self, handle: SandboxHandle) -> SandboxRunStatus:  # pragma: no cover
        return SandboxRunStatus(state=self._state, exit_code=self._exit_code)


# ---------------- fixtures ----------------

_CC_CONFIG = {
    "base_url": "https://cc-gateway.internal/v1",
    "auth_token": "cc-token-xxx",
    "model": "claude-test-model",
    "max_turns": 12,
}
_CC_ENV = {
    "ANTHROPIC_BASE_URL": "https://cc-gateway.internal/v1",
    "ANTHROPIC_AUTH_TOKEN": "cc-token-xxx",
    "ANTHROPIC_MODEL": "claude-test-model",
}
_DSH_NATIVE_CONFIG = {
    "base_url": "https://api.deepseek.internal",
    "api_key": "dsk-xxx",
    "protocol": "deepseek",
    "provider": "deepseek-official",
    "model": "deepseek-v4-flash",
    "profile": "sdk-minimal",
    "max_tokens": 8192,
    "extra_env": {},
}
_DSH_GATEWAY_CONFIG = {
    "base_url": "https://gw.internal/openai",
    "api_key": "gw-key-xxx",
    "protocol": "openai",
    "provider": "my-openai-gw",
    "model": "gw-model",
    "profile": "sdk-minimal",
    "extra_env": {},
}


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """agent 沙箱链路环境：工作区根指向临时目录 + CC/DSH 全局配置 mock（无 DB）"""
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")
    monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
    for var in ("HARNESS_CLAUDE_IMAGE", "HARNESS_DEEPSEEK_IMAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ClaudeCodeConfigService, "get_active_config",
                        classmethod(lambda cls, use_cache=True: dict(_CC_CONFIG)))
    monkeypatch.setattr(ClaudeCodeConfigService, "get_sdk_env",
                        classmethod(lambda cls: dict(_CC_ENV)))
    monkeypatch.setattr(DeepSeekHarnessConfigService, "get_active_config",
                        classmethod(lambda cls, use_cache=True: None))
    return tmp_path


def _patch_dsh(monkeypatch, config):
    monkeypatch.setattr(DeepSeekHarnessConfigService, "get_active_config",
                        classmethod(lambda cls, use_cache=True: dict(config) if config else None))


def _agent_script(**overrides):
    script = {"script_id": "pjs_cc_agent", "name": "cc_agent_step",
              "exec_type": "claude_agent", "entrypoint": "阅读输入文件并给出结论 JSON",
              "timeout_seconds": 90, "allowed_tools": ["Read", "Grep"],
              "harness_extra": None}
    script.update(overrides)
    return script


def _agent_stdin(run_base: Path, task_id="t-32", data_id="d-1"):
    run_dir = run_base / task_id / data_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return {"task_id": task_id, "data_id": data_id, "fields": {}, "inputs": {},
            "params": {}, "upstream": {}, "_run_dir": str(run_dir)}


@pytest.fixture
def agent_db(tmp_path, monkeypatch):
    """内存库会话工厂 + agent 脚本记录（run_pre_scripts 编排链路用，模式同
    test_pre_judge_runner.fake_script_db）"""
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(PreJudgeScript(id="pjs_cc_agent", name="cc_agent_step",
                              exec_type="claude_agent",
                              entrypoint="阅读输入文件并给出结论 JSON",
                              timeout_seconds=90, is_active=True,
                              allowed_tools=["Read", "Grep"]))
        db.commit()
    monkeypatch.setattr(pre_judge_runner, "SessionLocal", Session)
    return Session


# ---------------- 变体与凭据组装（payload 构造·前置校验） ----------------

class TestHarnessVariantResolution:
    def test_agent_exec_types_map_to_variants(self):
        assert harness_adapter.harness_variant_for("claude_agent") == "claude"
        assert harness_adapter.harness_variant_for("deepseek_agent") == "deepseek"

    def test_unknown_exec_type_rejected(self):
        with pytest.raises(harness_adapter.HarnessConfigError):
            harness_adapter.harness_variant_for("script")

    def test_empty_instruction_rejected(self):
        with pytest.raises(harness_adapter.HarnessConfigError, match="指令文本"):
            harness_adapter.build_harness_instruction({"entrypoint": "  "})


class TestHarnessConfigAssembly:
    def test_claude_config_carries_cc_credentials(self, agent_env):
        config = harness_adapter.build_harness_agent_config(
            "claude", script=_agent_script(), timeout=77.0)
        assert config["variant"] == "claude"
        assert config["env"]["ANTHROPIC_BASE_URL"] == _CC_ENV["ANTHROPIC_BASE_URL"]
        assert config["env"]["ANTHROPIC_AUTH_TOKEN"] == _CC_ENV["ANTHROPIC_AUTH_TOKEN"]
        assert config["model"] == "claude-test-model"
        assert config["max_turns"] == 12
        assert config["allowed_tools"] == ["Read", "Grep"]
        assert config["request_timeout_seconds"] == 77.0

    def test_claude_missing_credentials_fail_fast(self, agent_env, monkeypatch):
        monkeypatch.setattr(ClaudeCodeConfigService, "get_active_config",
                            classmethod(lambda cls, use_cache=True: None))
        monkeypatch.setattr(ClaudeCodeConfigService, "get_sdk_env",
                            classmethod(lambda cls: {}))
        driver = FakeHarnessDriver(fail_run="不应发起沙箱执行")
        monkeypatch.setattr(script_container, "get_sandbox_driver", lambda: driver)
        result = pre_judge_runner.execute_script(
            _agent_script(), _agent_stdin(agent_env / "script_runs"))
        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "Claude Code 全局配置" in result["error"]
        assert driver.run_count == 0  # fail-fast：不发起 docker

    def test_empty_entrypoint_fails_without_driver(self, agent_env, monkeypatch):
        driver = FakeHarnessDriver(fail_run="不应发起沙箱执行")
        monkeypatch.setattr(script_container, "get_sandbox_driver", lambda: driver)
        result = pre_judge_runner.execute_script(
            _agent_script(entrypoint=""), _agent_stdin(agent_env / "script_runs"))
        assert result["status"] == "fail"
        assert "entrypoint 为空" in result["error"]
        assert driver.run_count == 0

    def test_deepseek_native_protocol_direct_credentials(self, agent_env, monkeypatch):
        _patch_dsh(monkeypatch, _DSH_NATIVE_CONFIG)
        config = harness_adapter.build_harness_agent_config(
            "deepseek", script=_agent_script(exec_type="deepseek_agent",
                                             harness_extra={"model": "dsh-custom"}))
        assert config["variant"] == "deepseek"
        assert config["api_key"] == "dsk-xxx"  # 官方协议：key 直传 SDK kwargs
        assert config["base_url"] == "https://api.deepseek.internal"
        assert config["model"] == "dsh-custom"  # harness_extra 覆盖全局默认
        assert "DSH_PROVIDER_API_KEY" not in config["env"]

    def test_deepseek_gateway_protocol_key_via_env(self, agent_env, monkeypatch):
        _patch_dsh(monkeypatch, _DSH_GATEWAY_CONFIG)
        config = harness_adapter.build_harness_agent_config("deepseek", timeout=42.0)
        assert "api_key" not in config  # 网关协议：key 不落 patch 文件，经 env 引用
        assert config["env"]["DSH_PROVIDER_API_KEY"] == "gw-key-xxx"
        assert config["base_url"] == "https://gw.internal/openai"
        assert config["request_timeout_seconds"] == 42.0

    def test_deepseek_missing_config_fail_fast(self, agent_env):
        with pytest.raises(harness_adapter.HarnessConfigError, match="DeepSeek Harness"):
            harness_adapter.build_harness_agent_config("deepseek")

    def test_deepseek_missing_api_key_fail_fast(self, agent_env, monkeypatch):
        _patch_dsh(monkeypatch, {**_DSH_NATIVE_CONFIG, "api_key": ""})
        with pytest.raises(harness_adapter.HarnessConfigError, match="api_key"):
            harness_adapter.build_harness_agent_config("deepseek")

    def test_unknown_variant_rejected(self, agent_env):
        with pytest.raises(harness_adapter.HarnessConfigError):
            harness_adapter.build_harness_agent_config("k8s")


# ---------------- driver 调用参数（镜像/egress/entrypoint/工作区） ----------------

class TestSandboxSpec:
    def _run_once(self, agent_env, driver, script=None, stdin=None):
        script_container.execute_harness_in_container(
            script=script or _agent_script(),
            stdin=stdin or _agent_stdin(agent_env / "script_runs"),
            timeout=66.0, started=0.0,
            variant="claude",
            instruction="指令文本",
            harness_config={"variant": "claude", "env": dict(_CC_ENV)},
            driver=driver,
        )

    def test_spec_image_selected_by_exec_type(self, agent_env):
        for variant, image in script_container.DEFAULT_HARNESS_IMAGES.items():
            driver = FakeHarnessDriver()
            script_container.execute_harness_in_container(
                script=_agent_script(exec_type=f"{variant}_agent", script_id=f"pjs_{variant}"),
                stdin=_agent_stdin(agent_env / "script_runs"),
                timeout=10.0, started=0.0, variant=variant,
                instruction="指令", harness_config={"variant": variant, "env": {}},
                driver=driver)
            assert driver.spec.image == image
            # 基名落在 executor 镜像白名单内（变体=tag，不引入新基名）
            from executor.sandbox_params import RUNNER_IMAGE_BASES
            assert driver.spec.image.split(":")[0] in RUNNER_IMAGE_BASES

    def test_spec_image_env_override(self, agent_env, monkeypatch):
        monkeypatch.setenv("HARNESS_CLAUDE_IMAGE", "ai-evo-script-runner:harness-claude-ci")
        assert script_container.harness_image("claude") == "ai-evo-script-runner:harness-claude-ci"
        monkeypatch.setenv("HARNESS_DEEPSEEK_IMAGE", "ai-evo-script-runner:harness-deepseek-v2")
        assert script_container.harness_image("deepseek") == "ai-evo-script-runner:harness-deepseek-v2"

    def test_unknown_variant_image_rejected(self, agent_env):
        with pytest.raises(script_container.SandboxDriverError):
            script_container.harness_image("k8s")

    def test_spec_forced_egress_and_isolation(self, agent_env):
        driver = FakeHarnessDriver()
        self._run_once(agent_env, driver)
        spec = driver.spec
        # D8：出网恒 egress 档（不受 step.network / SCRIPT_NET_PROFILE 影响）
        assert spec.net_profile == "egress"
        from executor.sandbox_params import NET_PROFILES
        assert NET_PROFILES["egress"] == "aievo-sandbox-egress"
        # argv 契约：python /opt/runner/harness_entry.py <variant> 三件套（相对 workdir）
        assert spec.entrypoint == [
            CONTAINER_PYTHON, CONTAINER_HARNESS_ENTRY, "claude",
            script_container.HARNESS_INSTRUCTION_FILE,
            script_container.HARNESS_STDIN_FILE,
            script_container.HARNESS_CONFIG_FILE,
        ]
        assert spec.user == script_container.runner_user() == "10001:10001"
        assert spec.container_name.startswith("aievo-script-harness-")
        assert spec.stdin_payload == b""  # 载荷三件套落盘，不依赖 -i 管道
        assert spec.timeout == 66.0
        assert driver.wait_timeout == 66.0

    def test_staged_payload_files_in_workspace(self, agent_env):
        driver = FakeHarnessDriver()
        stdin = _agent_stdin(agent_env / "script_runs")
        # fields 里放一个工作区内宿主路径（inputs 落盘产物），验证重写
        marker = Path(stdin["_run_dir"]) / "inputs" / "note.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("hello", encoding="utf-8")
        stdin["fields"]["note"] = str(marker)
        script_container.execute_harness_in_container(
            script=_agent_script(), stdin=stdin, timeout=10.0, started=0.0,
            variant="claude", instruction="指令文本",
            harness_config={"variant": "claude", "env": dict(_CC_ENV)},
            driver=driver)
        cwd = Path(driver.spec.cwd_host)
        assert cwd == agent_env / "script_runs" / "t-32" / "d-1" / "pjs_cc_agent"
        staged_stdin = json.loads((cwd / "stdin.json").read_text(encoding="utf-8"))
        # 宿主路径 → 容器路径（挂载根 /workspace）
        assert staged_stdin["_run_dir"] == CONTAINER_WORKSPACE
        assert staged_stdin["fields"]["note"] == f"{CONTAINER_WORKSPACE}/inputs/note.txt"
        assert (cwd / "instruction.md").read_text(encoding="utf-8") == "指令文本"
        harness_json = json.loads((cwd / "harness.json").read_text(encoding="utf-8"))
        assert harness_json["env"]["ANTHROPIC_AUTH_TOKEN"] == _CC_ENV["ANTHROPIC_AUTH_TOKEN"]
        # 挂载源 = 本次 script_runs 工作目录（W15 约束，agent 通道同口径）
        assert Path(driver.spec.workspace_host) == agent_env / "script_runs" / "t-32" / "d-1"
        assert driver.spec.workspace_container == CONTAINER_WORKSPACE

    def test_full_chain_via_execute_script_stages_harness_json(self, agent_env, monkeypatch):
        """execute_script 调度接线：变体/指令/凭据组装 → 沙箱执行（staged 可观测）"""
        driver = FakeHarnessDriver()
        monkeypatch.setattr(script_container, "get_sandbox_driver", lambda: driver)
        result = pre_judge_runner.execute_script(
            _agent_script(), _agent_stdin(agent_env / "script_runs"), timeout_seconds=90)
        assert result["status"] == "ok"
        cwd = Path(driver.spec.cwd_host)
        harness_json = json.loads((cwd / "harness.json").read_text(encoding="utf-8"))
        assert harness_json["variant"] == "claude"
        assert harness_json["env"]["ANTHROPIC_BASE_URL"] == _CC_ENV["ANTHROPIC_BASE_URL"]
        assert harness_json["max_turns"] == 12
        assert harness_json["allowed_tools"] == ["Read", "Grep"]


# ---------------- HTTP executor 通道（生产标准沙箱形态；mock 传输层，不 mock 组包） ----------------

class TestHarnessViaHttpExecutorDriver:
    """agent 步骤经真实 HttpExecutorDriver.run 组包（评审阻断项回归护栏）

    此前 harness 覆盖只有 FakeHarnessDriver/DockerCliDriver：HTTP 通道请求体无
    workdir 字段、executor 硬编码 -w /workspace，而 entrypoint 以裸相对文件名引用
    三件套 → harness_entry 在挂载根 FileNotFoundError。本类替换客户端唯一 I/O 出口
    executor_client._http_open（mock executor：POST 记录请求体并 202，GET 模拟监督
    线程把 docker logs 落挂载根后 done），组包/路径推导/日志归位/结果解析全走生产代码。
    """

    HARNESS_ENVELOPE = {"status": "ok", "output": {"verdict": "pass"}, "files": {},
                        "summary": "agent done", "finish_reason": "completed", "error": None}

    @pytest.fixture
    def http_executor(self, agent_env, monkeypatch):
        from app.services import executor_client

        monkeypatch.setenv("EXECUTOR_URL", "http://executor:8300")
        monkeypatch.setenv("EXECUTOR_TOKEN", "unit-token")
        posts = []

        def _mock_http_open(req, timeout):
            if req.method == "POST":
                body = json.loads(req.data.decode("utf-8"))
                posts.append(body)
                return 202, json.dumps({"run_id": "run-h1", "container_name": "aievo-sbx-h1",
                                        "state": "running"}).encode("utf-8")
            # GET：模拟 executor 监督线程——终态前 docker logs 写到挂载根（workspace）
            ws = Path(posts[-1]["workspace"])
            (ws / "stdout.log").write_text(
                "claude-agent 启动中…\n" + json.dumps(self.HARNESS_ENVELOPE, ensure_ascii=False) + "\n",
                encoding="utf-8")
            (ws / "stderr.log").write_text("", encoding="utf-8")
            return 200, json.dumps({"run_id": "run-h1", "state": "done", "exit_code": 0,
                                    "error": ""}).encode("utf-8")

        monkeypatch.setattr(executor_client, "_http_open", _mock_http_open)
        return posts

    def test_request_workdir_is_per_script_cwd(self, agent_env, http_executor):
        """请求体 workdir == build_container_paths 推导的 /workspace/{script_id}
        （spec 期望值），不是挂载根 /workspace；三件套在 argv 里仍为裸相对文件名"""
        from app.services.executor_client import HttpExecutorDriver

        run_base = agent_env / "script_runs"
        script_container.execute_harness_in_container(
            script=_agent_script(), stdin=_agent_stdin(run_base),
            timeout=66.0, started=0.0, variant="claude", instruction="指令文本",
            harness_config={"variant": "claude", "env": dict(_CC_ENV)},
            driver=HttpExecutorDriver())

        assert len(http_executor) == 1
        body = http_executor[0]
        run_dir = run_base / "t-32" / "d-1"
        cwd = run_dir / "pjs_cc_agent"
        paths = script_container.build_container_paths(run_dir, cwd, cwd)
        assert paths.cwd_container == f"{CONTAINER_WORKSPACE}/pjs_cc_agent"
        # 关键断言：workdir 字段存在且等于 per-script cwd（评审阻断项）
        assert "workdir" in body
        assert body["workdir"] == paths.cwd_container
        assert body["workspace"] == str(run_dir) == paths.run_dir_host
        assert body["net_profile"] == "egress"
        assert body["image"] == script_container.DEFAULT_HARNESS_IMAGES["claude"]
        # entrypoint 契约不变：python harness_entry.py claude 三件套（裸相对名，依赖 -w）
        assert body["entrypoint"][:2] == ["sh", "-c"]
        command = body["entrypoint"][2]
        assert CONTAINER_HARNESS_ENTRY in command
        argv_part = command.split("<")[0]
        for name in (script_container.HARNESS_INSTRUCTION_FILE,
                     script_container.HARNESS_STDIN_FILE,
                     script_container.HARNESS_CONFIG_FILE):
            assert f" {name}" in argv_part and f"/{name}" not in argv_part
            assert (cwd / name).is_file()  # 三件套确实 staged 在 cwd（而非挂载根）
        # 三件套不在挂载根——若 -w 落挂载根，harness_entry 必 FileNotFound
        assert not (run_dir / script_container.HARNESS_INSTRUCTION_FILE).exists()

    def test_full_flow_relocates_logs_and_parses_result(self, agent_env, http_executor):
        """HTTP 通道全流程：202 → GET done → 挂载根日志归位 cwd → 结果条目与
        FakeHarnessDriver/CLI 通道同构（status/output/harness 元信息）"""
        from app.services.executor_client import HttpExecutorDriver

        run_base = agent_env / "script_runs"
        result = script_container.execute_harness_in_container(
            script=_agent_script(), stdin=_agent_stdin(run_base),
            timeout=30.0, started=0.0, variant="claude", instruction="指令文本",
            harness_config={"variant": "claude", "env": dict(_CC_ENV)},
            driver=HttpExecutorDriver())
        assert result["status"] == "ok", result
        assert result["output"] == {"verdict": "pass"}
        assert result["harness"]["variant"] == "claude"
        cwd = run_base / "t-32" / "d-1" / "pjs_cc_agent"
        assert (cwd / "stdout.log").is_file() and (cwd / "stderr.log").is_file()
        assert not (run_base / "t-32" / "d-1" / "stdout.log").exists()  # 已归位，挂载根无残留


# ---------------- 产物回收与失败语义 ----------------

class TestArtifactRecovery:
    def _exec(self, agent_env, driver, stdin=None):
        return script_container.execute_harness_in_container(
            script=_agent_script(), stdin=stdin or _agent_stdin(agent_env / "script_runs"),
            timeout=30.0, started=0.0, variant="claude", instruction="指令文本",
            harness_config={"variant": "claude", "env": {}}, driver=driver)

    def test_declared_files_recovered_to_host_paths(self, agent_env):
        driver = FakeHarnessDriver(report_files={"report": "outputs/report.md"})
        result = self._exec(agent_env, driver)
        assert result["status"] == "ok"
        expected = Path(driver.spec.cwd_host) / "outputs" / "report.md"
        assert result["files"]["report"] == str(expected)
        assert expected.is_file()

    def test_artifacts_dir_auto_collected(self, agent_env):
        driver = FakeHarnessDriver()
        result = self._exec(agent_env, driver)
        artifacts_dir = Path(driver.spec.cwd_host) / "artifacts"
        assert result["files"]["tool_calls.jsonl"] == str(artifacts_dir / "tool_calls.jsonl")
        assert (artifacts_dir / "tool_calls.jsonl").is_file()
        assert result["harness"]["variant"] == "claude"
        assert result["harness"]["image"] == script_container.DEFAULT_HARNESS_IMAGES["claude"]
        assert result["harness"]["artifacts_dir"] == str(artifacts_dir)

    def test_stdout_stderr_logs_written_back_to_workspace(self, agent_env):
        driver = FakeHarnessDriver(stderr_tail="some agent noise")
        result = self._exec(agent_env, driver)
        cwd = Path(driver.spec.cwd_host)
        assert (cwd / "stdout.log").is_file()
        assert (cwd / "stderr.log").read_text(encoding="utf-8") == "some agent noise"
        assert result["status"] == "ok"
        assert result["output"] == {"verdict": "pass", "score": 0.9}

    def test_nonzero_exit_fails_with_stderr_tail(self, agent_env):
        driver = FakeHarnessDriver(exit_code=3, stderr_tail="TypeError: boom")
        result = self._exec(agent_env, driver)
        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "退出码 3" in result["error"]
        assert "TypeError: boom" in result["error"]

    def test_timeout_state_fails(self, agent_env):
        driver = FakeHarnessDriver(state="timeout", exit_code=137)
        result = self._exec(agent_env, driver)
        assert result["status"] == "fail"
        assert result["finish_reason"] == "timeout"
        assert "超时" in result["error"]

    def test_driver_run_error_fails(self, agent_env):
        driver = FakeHarnessDriver(fail_run="镜像 ai-evo-script-runner:harness-claude 不可用")
        result = self._exec(agent_env, driver)
        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "harness-claude" in result["error"]

    def test_unparseable_stdout_fails(self, agent_env):
        driver = FakeHarnessDriver(stdout_lines=["not json at all"])
        result = self._exec(agent_env, driver)
        assert result["status"] == "fail"
        assert "不是合法 JSON" in (result["error"] or "")

    def test_non_string_file_values_passthrough(self, agent_env):
        driver = FakeHarnessDriver()
        # 直接构造 stdout envelope：files 含非字符串值（防御口径，不崩不误映射）
        envelope = {"status": "ok", "output": {}, "files": {"count": 7, "p": "/workspace/x"},
                    "summary": "", "finish_reason": "completed", "error": None}
        driver._stdout_lines = [json.dumps(envelope)]
        result = self._exec(agent_env, driver)
        assert result["files"]["count"] == 7
        assert result["files"]["p"].startswith(str(agent_env))  # /workspace/x → 宿主前缀


# ---------------- 评分输入链路（run_pre_scripts 编排） ----------------

class TestScoringInputChain:
    def test_agent_step_populates_script_outputs_and_files(self, agent_env, agent_db, monkeypatch):
        driver = FakeHarnessDriver(output={"conclusion": "good"}, report_files={"rpt": "rpt.md"})
        monkeypatch.setattr(script_container, "get_sandbox_driver", lambda: driver)
        task = SimpleNamespace(id="task-3-2", pre_scripts=[{"script_id": "pjs_cc_agent"}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item={"id": "d-9", "data": {}})
        entry = out["pre_script_results"][0]
        assert entry["status"] == "ok"
        assert out["script_outputs"]["cc_agent_step"] == {"conclusion": "good"}
        # 评分输入引用的产物是宿主工作区路径（可直接读文件）
        files = out["script_files"]["cc_agent_step"]
        assert Path(files["rpt"]).is_file()
        assert files["tool_calls.jsonl"].endswith("artifacts/tool_calls.jsonl")
        assert not out["aborted"]

    def test_agent_failure_honors_on_fail_abort(self, agent_env, agent_db, monkeypatch):
        driver = FakeHarnessDriver(stdout_lines=["garbage line"])
        monkeypatch.setattr(script_container, "get_sandbox_driver", lambda: driver)
        task = SimpleNamespace(id="task-3-2b", pre_scripts=[
            {"script_id": "pjs_cc_agent", "on_fail": "abort"}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item={"id": "d-9", "data": {}})
        assert out["pre_script_results"][0]["status"] == "fail"
        assert out["aborted"] is True

    def test_deepseek_agent_step_selects_deepseek_variant(self, agent_env, agent_db, monkeypatch):
        _patch_dsh(monkeypatch, _DSH_NATIVE_CONFIG)
        # 补一条 deepseek_agent 脚本记录（fixture 只建了 claude 记录）
        with agent_db() as db:
            db.add(PreJudgeScript(id="pjs_dsh_agent", name="dsh_agent_step", exec_type="deepseek_agent",
                                  entrypoint="总结输入", timeout_seconds=60, is_active=True,
                                  harness_extra={"model": "dsh-custom"}))
            db.commit()
        driver = FakeHarnessDriver(output={"ok": True})
        monkeypatch.setattr(script_container, "get_sandbox_driver", lambda: driver)
        task = SimpleNamespace(id="task-3-2c", pre_scripts=[{"script_id": "pjs_dsh_agent"}])
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item={"id": "d-9", "data": {}})
        assert out["pre_script_results"][0]["status"] == "ok"
        assert driver.spec.image == script_container.DEFAULT_HARNESS_IMAGES["deepseek"]
        assert driver.spec.entrypoint[2] == "deepseek"
        harness_json = json.loads(
            (Path(driver.spec.cwd_host) / "harness.json").read_text(encoding="utf-8"))
        assert harness_json["model"] == "dsh-custom"  # harness_extra 覆盖透传到沙箱


# ---------------- Dockerfile.harness 静态契约（不 build） ----------------

class TestDockerfileHarnessContract:
    """变体镜像 Dockerfile 的静态断言：真实构建/端到端执行属部署机与 sandbox_e2e 档"""

    def test_file_exists_with_both_variants(self):
        assert DOCKERFILE_HARNESS.is_file()
        text = DOCKERFILE_HARNESS.read_text(encoding="utf-8")
        assert "AS harness-base" in text
        assert "AS harness-claude" in text
        assert "AS harness-deepseek" in text

    def test_claude_variant_copies_offline_cli(self):
        text = DOCKERFILE_HARNESS.read_text(encoding="utf-8")
        assert "COPY claude-local/ /opt/claude/" in text
        # claude-local 目录不入 git（build-service 就位，.gitignore 既有规则覆盖）
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "claude-local/" in gitignore

    def test_deepseek_variant_pins_sdk_version_from_lock(self):
        text = DOCKERFILE_HARNESS.read_text(encoding="utf-8")
        lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
        pinned = next(
            line.split("==")[1].strip()
            for line in lock.splitlines()
            if line.startswith("deepseek-harness-sdk==")
        )
        assert f"deepseek-harness-sdk=={pinned}" in text

    def test_entry_script_matches_worker_contract(self):
        """镜像内 harness_entry.py 的 argv/文件名契约与 script_container 常量一致"""
        lines = DOCKERFILE_HARNESS.read_text(encoding="utf-8").splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("RUN <<PYEOF")) + 1
        end = next(i for i, l in enumerate(lines) if l == "PYEOF")
        body = "\n".join(lines[start:end])
        compile(body, "harness_entry.py", "exec")  # heredoc 内 Python 语法门禁
        for name in (script_container.HARNESS_INSTRUCTION_FILE,
                     script_container.HARNESS_STDIN_FILE,
                     script_container.HARNESS_CONFIG_FILE):
            assert f'"{name}"' in body or name in body
        assert script_container.HARNESS_ARTIFACTS_DIR in body
        # worker 侧入口路径与 Dockerfile 安装位一致
        all_text = "\n".join(lines)
        assert script_container.CONTAINER_HARNESS_ENTRY in all_text

    def test_nonroot_user_and_shared_packages(self):
        text = DOCKERFILE_HARNESS.read_text(encoding="utf-8")
        assert "packages.txt" in text  # 依赖集合与基础 script-runner 同源
        assert "useradd -m -u 10001 -g 10001" in text  # 非 root（与 SANDBOX_USER 一致）
        # USER runner 在 harness-base 落位、早于两个变体 stage（变体继承非 root）
        lines = text.splitlines()
        user_line = next(i for i, l in enumerate(lines) if l == "USER runner")
        variant_lines = [i for i, l in enumerate(lines) if l.startswith("FROM harness-base")]
        assert variant_lines and all(user_line < i for i in variant_lines)

    def test_real_container_execution_left_to_sandbox_e2e(self):
        """完成标准口径：真容器执行标 sandbox_e2e 跳过——本套件恒 mock docker。

        真机档（需 docker daemon + 变体镜像预载）由部署机按 pytest.ini 的
        sandbox_e2e 标记显式 `-m sandbox_e2e` 执行（见下方 TestHarnessSandboxE2E）。
        """
        assert "sandbox_e2e" in (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")


# ---------------- 实机档（sandbox_e2e：默认 deselect，-m sandbox_e2e 显式执行） ----------------

def _harness_sandbox_available(image: str) -> bool:
    """docker CLI/daemon 可用且变体镜像已预载（缺位 → skip 不假绿）"""
    import shutil
    import subprocess

    from executor import sandbox_params

    docker = shutil.which(sandbox_params.docker_binary())
    if not docker:
        return False
    try:
        if subprocess.run([docker, "info"], capture_output=True, timeout=15).returncode != 0:
            return False
        return subprocess.run([docker, "image", "inspect", image],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class TestHarnessSandboxE2E:
    """真容器执行（完成标准：标 sandbox_e2e，默认跳过）

    前置缺位（docker daemon / 变体镜像未预载，变体构建留部署机）一律
    pytest.skip——绝不假绿。容器内被测 agent 无真凭据时也必须收敛为结构化
    fail 条目（harness_entry 输出协议），本档验证的是沙箱管线真实落通：
    staged 三件套进容器、日志/产物回写工作区、结果条目结构完整。
    """

    @pytest.mark.sandbox_e2e
    def test_claude_agent_real_container_plumbing(self, agent_env, monkeypatch):
        image = script_container.DEFAULT_HARNESS_IMAGES["claude"]
        if not _harness_sandbox_available(image):
            pytest.skip(
                f"docker 或变体镜像 {image} 不可用：harness sandbox_e2e 实机档跳过"
                "（变体镜像构建见 docker/script-runner/Dockerfile.harness 文件头，留部署机）")
        from app.services.script_container import DockerCliDriver

        monkeypatch.delenv("EXECUTOR_URL", raising=False)  # 本机形态：CLI 驱动
        result = script_container.execute_harness_in_container(
            script=_agent_script(), stdin=_agent_stdin(agent_env / "script_runs"),
            timeout=90.0, started=0.0, variant="claude", instruction="回复一个 JSON 对象 {\"status\": \"ok\"}",
            harness_config={"variant": "claude", "env": dict(_CC_ENV), "max_turns": 2},
            driver=DockerCliDriver(),
        )
        # 管线断言（agent 真实成败取决于容器内凭据，不假绿）：
        # staged 三件套在工作区、日志已回写、结果条目与 harness 元信息完整
        cwd = agent_env / "script_runs" / "t-32" / "d-1" / "pjs_cc_agent"
        for name in (script_container.HARNESS_INSTRUCTION_FILE,
                     script_container.HARNESS_STDIN_FILE,
                     script_container.HARNESS_CONFIG_FILE):
            assert (cwd / name).is_file(), f"staged 缺失: {name}"
        assert (cwd / "stdout.log").exists() and (cwd / "stderr.log").exists()
        assert result["status"] in ("ok", "fail")
        assert result["finish_reason"] in ("completed", "error", "timeout")
        assert result["harness"]["variant"] == "claude"
        assert result["harness"]["image"] == image
