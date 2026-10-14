"""SandboxDriver 抽象与 HTTP executor 驱动单元测试（L1，mock executor，无真实网络/DB）

设计条目 1-2（plans/2026-10-15-企业级评测系统架构设计.md §2.3 D1 / §3 表 1-2 行）：
script_container 抽 SandboxDriver 接口（run/wait/kill/status），实现 docker-CLI
（W15 通道迁移）与 http-executor（executor_client）两个驱动；pre_judge_runner
分流点按驱动选择（container → executor 驱动；local → 原 subprocess 不变）。

覆盖（完成标准逐项）：
- 三档网络参数映射（mock executor）：none/egress → 请求 net_profile 原样进
  executor 白名单；open（step.network=true）→ executor 400 白名单拒绝、
  错误透传到脚本 fail 条目；docker-CLI 侧 WORKER_NET_PROFILE_TO_DOCKER 三档
  → --network 取值（egress 网络名与 sandbox_params.NET_PROFILES 同源钉扎）
- 错误传递：executor 白名单拒绝（400 detail）/ 不可达（URLError）/ 未登记
  run（404）/ EXECUTOR_TOKEN 未配置 fail-closed —— 全部转脚本 fail 条目，
  不静默降级
- 超时：服务端 state=timeout → finish_reason=timeout；executor 卡死时客户端
  轮询死线兜底（WAIT_MARGIN 收紧模拟）
- local 模式行为不变：显式 SCRIPT_EXEC_MODE=local 时即便 EXECUTOR_URL 已配置
  也不解析驱动、走原 subprocess 通道（回归护栏；ARCH-1-4 后 local 须显式
  声明，缺省已是 container）
- 驱动选择：EXECUTOR_URL 已配置 → HttpExecutorDriver；未配置 → DockerCliDriver
  （W15 单机通道回退，既有 79 测零适配全绿的同一机制）
- 生命周期契约：成功流（stdin 落盘工作区 + entrypoint shell 重定向 + 日志
  归位 cwd + 结果解析与 local 同构）；running→done 轮询；非零退出码；
  failed 态；kill 为 1-1 契约下的显式 no-op（无远端端点，不触传输层）
- DockerCliDriver 驱动级生命周期（run/wait/status/kill 用假 docker CLI 走通）

mock executor 说明：executor_client._http_open 是唯一网络 I/O 出口，本文件以
MockExecutor 替换（记录请求 + 脚本化响应），不经真实网络——unit 分层成立。
"""
import io
import json
import sys
import time
import urllib.error
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：L1 单元测试（无网络/无 DB）

from app.services import executor_client, pre_judge_runner, pre_judge_script_service, script_container
from app.services.executor_client import HttpExecutorDriver
from app.services.script_container import (
    DockerCliDriver,
    SandboxDriver,
    SandboxDriverError,
)

EXECUTOR_URL = "http://executor:8300"
TOKEN = "unit-test-executor-token"


# ---------------- mock executor（替换唯一 I/O 出口 _http_open） ----------------

class MockExecutor:
    """mock executor：记录请求，按 method 分队列脚本化响应

    - 记录每次请求 {method, url, auth, body}（断言请求形态/鉴权/三档映射）；
    - ``responses``（GET 侧）/``post_responses``（POST 侧）出队消费：Exception →
      抛出（模拟网络层/HTTP 错误）；callable → 返回 (status, bytes)（可带副
      作用，如模拟 executor 终态前落盘日志）；tuple → 原样返回；
    - 队列空时默认行为：POST → 202 {run_id}；GET → default_state 态（缺省
      done/exit 0，并在响应前把 docker logs 落盘挂载根——模拟服务端监督线程；
      置 "running" 可模拟长跑/卡死）。
    """

    DEFAULT_STDOUT = '{"status": "ok", "output": {"echo": "default"}, "summary": "done"}'

    def __init__(self):
        self.requests = []
        self.responses = []        # GET /sandbox/runs/{id} 侧
        self.post_responses = []   # POST /sandbox/run 侧
        self.default_state = "done"
        self._run_counter = 0

    def __call__(self, req, timeout):
        self.requests.append({
            "method": req.method,
            "url": req.full_url,
            "auth": req.get_header("Authorization") or "",
            "body": json.loads(req.data.decode("utf-8")) if req.data else None,
        })
        queue = self.post_responses if req.method == "POST" else self.responses
        if queue:
            action = queue.pop(0)
            if isinstance(action, Exception):
                raise action
            return action(req) if callable(action) else action
        if req.method == "POST":
            self._run_counter += 1
            run_id = f"run-{self._run_counter:04d}"
            return 202, json.dumps({
                "run_id": run_id, "container_name": f"aievo-sbx-{run_id}",
                "state": "running"}).encode("utf-8")
        if self.default_state == "done":
            self._write_default_logs()
            return 200, json.dumps({
                "run_id": "x", "state": "done", "exit_code": 0,
                "error": ""}).encode("utf-8")
        return 200, json.dumps({
            "run_id": "x", "state": self.default_state,
            "exit_code": None, "error": ""}).encode("utf-8")

    def _write_default_logs(self):
        """默认 done 响应前：模拟 executor 监督线程把 docker logs 写到挂载根"""
        for rec in self.requests:
            if rec["method"] == "POST" and rec.get("body"):
                ws = Path(rec["body"]["workspace"])
                (ws / "stdout.log").write_text(self.DEFAULT_STDOUT + "\n", encoding="utf-8")
                (ws / "stderr.log").write_text("", encoding="utf-8")

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def gets(self):
        return [r for r in self.requests if r["method"] == "GET"]


def _http_error(code: int, detail: str) -> urllib.error.HTTPError:
    """executor 风格的错误响应（FastAPI HTTPException 体 {detail: ...}）"""
    return urllib.error.HTTPError(
        "http://executor:8300/sandbox/run", code, "error", None,
        io.BytesIO(json.dumps({"detail": detail}).encode("utf-8")))


@pytest.fixture
def mock_executor(monkeypatch):
    """已配置 EXECUTOR_URL/TOKEN 的 mock executor 环境（驱动解析为 HTTP 通道）"""
    monkeypatch.setenv("EXECUTOR_URL", EXECUTOR_URL)
    monkeypatch.setenv("EXECUTOR_TOKEN", TOKEN)
    monkeypatch.delenv("SCRIPT_NET_PROFILE", raising=False)
    mock = MockExecutor()
    monkeypatch.setattr(executor_client, "_http_open", mock)
    return mock


def _workspace(tmp_path: Path):
    """真实任务布局的工作区：run_dir 挂载源，cwd=run_dir/{sid}，staged=scripts/ 下"""
    run_dir = tmp_path / "runs" / "t1" / "d1"
    staged = run_dir / "scripts" / "s1"
    staged.mkdir(parents=True, exist_ok=True)
    cwd = run_dir / "s1"
    cwd.mkdir(parents=True, exist_ok=True)
    return run_dir, staged, cwd


def _spec(tmp_path: Path, net_profile: str = "none") -> "script_container.SandboxRunSpec":
    """驱动无关执行参数（真实任务布局；entrypoint 同 CLI 通道尾部命令形状）"""
    run_dir, _, cwd = _workspace(tmp_path)
    return script_container.SandboxRunSpec(
        image="ai-evo-script-runner:latest", user="10001:10001",
        workspace_host=str(run_dir), workspace_container="/workspace",
        workdir_container="/workspace/s1", cwd_host=str(cwd),
        net_profile=net_profile, container_name="aievo-script-s1-abc123",
        entrypoint=["python", "/opt/runner/pre_judge_entry.py",
                    "/workspace/scripts/s1", "main.py"],
        stdin_payload=b"{}", timeout=30.0,
    )


def _run_in_container(tmp_path, **kwargs):
    """驱动无关编排入口（mock_executor fixture 已把驱动解析为 HTTP 通道）"""
    run_dir, staged, cwd = _workspace(tmp_path)
    timeout = kwargs.pop("timeout", 30)
    result = script_container.execute_script_in_container(
        script={"script_id": "s1", "name": "s1"},
        staged_dir_host=staged, entry_rel="main.py",
        run_dir_host=run_dir, cwd_host=cwd,
        stdin={"task_id": "t1", "data_id": "d1", "_run_dir": str(run_dir), "fields": {}},
        timeout=timeout, started=time.time(),
        **kwargs,
    )
    return result, (run_dir, staged, cwd)


# ---------------- 三档网络档位归一（worker 侧） ----------------

class TestNetProfile:
    def test_default_is_none(self, monkeypatch):
        monkeypatch.delenv("SCRIPT_NET_PROFILE", raising=False)
        assert script_container.get_script_net_profile() == "none"

    def test_env_profiles_valid(self, monkeypatch):
        for profile in ("none", "egress", "open"):
            monkeypatch.setenv("SCRIPT_NET_PROFILE", profile)
            assert script_container.get_script_net_profile() == profile

    def test_invalid_env_falls_back_to_none(self, monkeypatch):
        """非法值 fail-safe：不认识的档位绝不出网"""
        monkeypatch.setenv("SCRIPT_NET_PROFILE", "bridge!!")
        assert script_container.get_script_net_profile() == "none"

    def test_step_network_flag_maps_to_open(self, monkeypatch):
        """step.network=true（W15「脚本要访问被测系统」）→ open 档"""
        monkeypatch.delenv("SCRIPT_NET_PROFILE", raising=False)
        assert script_container.resolve_net_profile(True) == "open"

    def test_no_flag_uses_configured_profile(self, monkeypatch):
        monkeypatch.setenv("SCRIPT_NET_PROFILE", "egress")
        assert script_container.resolve_net_profile(False) == "egress"

    def test_worker_map_egress_name_shares_single_source(self):
        """egress 网络名与 executor 白名单 sandbox_params.NET_PROFILES 同源"""
        from executor import sandbox_params
        assert script_container.WORKER_NET_PROFILE_TO_DOCKER == {
            "none": "none",
            "egress": sandbox_params.NET_PROFILES["egress"],
            "open": "bridge",
        }


# ---------------- 驱动选择与接口契约 ----------------

class TestDriverSelection:
    def test_no_executor_url_falls_back_to_docker_cli(self, monkeypatch):
        monkeypatch.delenv("EXECUTOR_URL", raising=False)
        assert isinstance(script_container.get_sandbox_driver(), DockerCliDriver)

    def test_executor_url_selects_http_driver(self, monkeypatch):
        monkeypatch.setenv("EXECUTOR_URL", EXECUTOR_URL)
        assert isinstance(script_container.get_sandbox_driver(), HttpExecutorDriver)

    def test_interface_contract(self):
        """两驱动都实现 SandboxDriver 四方法；抽象基类不可实例化（K8s 预留接口）"""
        for driver in (DockerCliDriver(), HttpExecutorDriver()):
            assert isinstance(driver, SandboxDriver)
            for method in ("run", "wait", "kill", "status"):
                assert callable(getattr(driver, method))
        with pytest.raises(TypeError):
            script_container.SandboxDriver()  # type: ignore[abstract]


# ---------------- docker-CLI 驱动：三档映射（纯构造） ----------------

class TestCliProfileMapping:
    def _args(self, tmp_path, net_profile):
        run_dir = tmp_path / "script_runs" / "t1" / "d1"
        staged = run_dir / "scripts" / "s1"
        cwd = run_dir / "s1"
        paths = script_container.build_container_paths(run_dir, cwd, staged)
        return script_container.build_docker_run_args_for_profile(
            image="ai-evo-script-runner:latest", user="10001:10001",
            paths=paths, entry_rel="main.py",
            container_name="aievo-script-s1-abc", net_profile=net_profile)

    @pytest.mark.parametrize("net_profile,network", [
        ("none", "none"),
        ("egress", "aievo-sandbox-egress"),
        ("open", "bridge"),
    ])
    def test_profile_to_docker_network(self, tmp_path, net_profile, network):
        args = self._args(tmp_path, net_profile)
        assert args[args.index("--network") + 1] == network

    def test_unknown_profile_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="未知网络档位"):
            self._args(tmp_path, "host")

    def test_legacy_bool_wrapper_equivalent(self, tmp_path):
        """W15 布尔开关面：True ≡ open 档、False ≡ none 档（冒烟链路依赖）"""
        run_dir = tmp_path / "script_runs" / "t1" / "d1"
        staged = run_dir / "scripts" / "s1"
        cwd = run_dir / "s1"
        paths = script_container.build_container_paths(run_dir, cwd, staged)
        for network, profile in ((True, "open"), (False, "none")):
            assert script_container.build_docker_run_args(
                image="ai-evo-script-runner:latest", user="10001:10001",
                paths=paths, entry_rel="main.py", container_name="c",
                network=network) == script_container.build_docker_run_args_for_profile(
                image="ai-evo-script-runner:latest", user="10001:10001",
                paths=paths, entry_rel="main.py", container_name="c",
                net_profile=profile)


# ---------------- http-executor 驱动：请求形态与错误传递（mock executor） ----------------

class TestHttpDriverRequests:
    def test_run_posts_whitelist_shaped_payload(self, tmp_path, mock_executor):
        """请求字段集即白名单面：{image, workspace, workdir, net_profile, timeout,
        entrypoint}；stdin 载荷落盘工作区、entrypoint 为 shell 重定向包装（无额外
        字段可表达）；workdir = spec.workdir_container（per-script cwd，与 CLI
        通道 -w 同值——harness 三件套/脚本 artifacts 按 cwd 寻址）"""
        run_dir, _, _ = _workspace(tmp_path)
        spec = script_container.SandboxRunSpec(
            image="ai-evo-script-runner:latest", user="10001:10001",
            workspace_host=str(run_dir), workspace_container="/workspace",
            workdir_container="/workspace/s1", cwd_host=str(run_dir / "s1"),
            net_profile="none", container_name="aievo-script-s1-abc123",
            entrypoint=["python", "/opt/runner/pre_judge_entry.py",
                        "/workspace/scripts/s1", "main.py"],
            stdin_payload=json.dumps({"_run_dir": "/workspace"}).encode("utf-8"),
            timeout=120.0,
        )
        handle = HttpExecutorDriver().run(spec)
        assert handle.run_id

        post = mock_executor.posts()[0]
        # 鉴权与地址（compose 内网 + token，设计 D1/D6）
        assert post["url"] == f"{EXECUTOR_URL}/sandbox/run"
        assert post["auth"] == f"Bearer {TOKEN}"
        # 字段集恰为白名单面（无 privileged/volumes 等可表达位）
        body = post["body"]
        assert sorted(body) == ["entrypoint", "image", "net_profile",
                                "timeout", "workdir", "workspace"]
        assert body["image"] == spec.image
        assert body["workspace"] == str(run_dir)
        # 容器内工作目录透传 per-script cwd（不是挂载根）
        assert body["workdir"] == spec.workdir_container == "/workspace/s1"
        assert body["net_profile"] == "none"
        assert body["timeout"] == 120.0
        # entrypoint = sh -c「原 argv 逐段引号包裹 + stdin 文件重定向」
        assert body["entrypoint"][0:2] == ["sh", "-c"]
        command = body["entrypoint"][2]
        assert "/opt/runner/pre_judge_entry.py" in command
        assert "/workspace/scripts/s1" in command and "main.py" in command
        assert "< /workspace/stdin-aievo-script-s1-abc123.json" in command
        # stdin 载荷已落盘工作区（挂载面内，随工作区清理）
        stdin_file = run_dir / "stdin-aievo-script-s1-abc123.json"
        assert json.loads(stdin_file.read_text("utf-8")) == {"_run_dir": "/workspace"}

    def test_orchestration_sends_per_script_cwd_as_workdir(self, tmp_path, mock_executor):
        """编排层 → 请求 workdir = build_container_paths 推导的 per-script cwd
        （/workspace/{script_id}），与 docker-CLI 通道 -w 同值，不是挂载根"""
        result, (run_dir, staged, cwd) = _run_in_container(tmp_path)
        assert result["status"] == "ok"
        paths = script_container.build_container_paths(run_dir, cwd, staged)
        body = mock_executor.posts()[0]["body"]
        assert body["workdir"] == paths.cwd_container == "/workspace/s1"
        assert body["workspace"] == paths.run_dir_host == str(run_dir)

    def test_missing_token_fails_closed(self, tmp_path, monkeypatch):
        """EXECUTOR_TOKEN 未配置：客户端 fail-closed 不发起任何调用（与 executor 503 对齐）"""
        monkeypatch.setenv("EXECUTOR_URL", EXECUTOR_URL)
        monkeypatch.delenv("EXECUTOR_TOKEN", raising=False)
        mock = MockExecutor()
        monkeypatch.setattr(executor_client, "_http_open", mock)
        with pytest.raises(SandboxDriverError, match="EXECUTOR_TOKEN"):
            HttpExecutorDriver().run(_spec(tmp_path))
        assert mock.requests == []  # 不触传输层

    def test_whitelist_reject_detail_propagates(self, tmp_path, mock_executor):
        """executor 白名单拒绝：HTTP 400 + detail 原样透传（错误传递）"""
        mock_executor.post_responses.append(
            _http_error(400, "镜像 'ubuntu:latest' 不在白名单：仅允许 ai-evo-script-runner 系"))
        with pytest.raises(SandboxDriverError, match="ubuntu:latest") as ei:
            HttpExecutorDriver().run(_spec(tmp_path))
        assert "HTTP 400" in str(ei.value)

    def test_unreachable_executor_raises(self, tmp_path, mock_executor):
        """网络层错误（URLError）：转「不可达」驱动错误（错误传递）"""
        mock_executor.post_responses.append(urllib.error.URLError("connection refused"))
        with pytest.raises(SandboxDriverError, match="不可达"):
            HttpExecutorDriver().run(_spec(tmp_path))


class TestThreeNetProfilesViaOrchestration:
    """三档网络参数映射（完成标准）：编排层 → executor 请求 net_profile 字段"""

    @pytest.mark.parametrize("env_profile,expected", [
        ("none", "none"), ("egress", "egress"),
    ])
    def test_configured_profile_sent_as_is(self, tmp_path, mock_executor, monkeypatch,
                                           env_profile, expected):
        """none/egress：档位原样进请求（白名单校验单一事实源在 executor 侧）"""
        monkeypatch.setenv("SCRIPT_NET_PROFILE", env_profile)
        result, _ = _run_in_container(tmp_path)
        assert result["status"] == "ok"
        assert mock_executor.posts()[0]["body"]["net_profile"] == expected

    def test_open_profile_rejected_and_propagated(self, tmp_path, mock_executor):
        """open 档（step.network=true 归一）：executor 白名单未放开 → 400 拒绝，
        错误原样透传到脚本 fail 条目（不静默降级到 egress/none）"""
        mock_executor.post_responses.append(
            _http_error(400, "网络档位 'open' 不在白名单：仅允许 none|egress"))
        result, _ = _run_in_container(tmp_path, network=True)
        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "HTTP 400" in result["error"]
        assert "open" in result["error"] and "白名单" in result["error"]
        assert mock_executor.posts()[0]["body"]["net_profile"] == "open"


# ---------------- http-executor 驱动：生命周期（mock executor） ----------------

class TestHttpLifecycle:
    def test_success_flow_relocates_logs_and_parses(self, tmp_path, mock_executor):
        """成功流：202 → GET done → 工作区根日志归位 cwd → 结果解析与 local 同构"""
        run_dir, _, cwd = _workspace(tmp_path)
        script_output = '{"status": "ok", "output": {"echo": 1}, "summary": "done"}'

        def _finish_with_logs(req):
            # 模拟 executor 监督线程：终态前把 docker logs 写到挂载根
            (run_dir / "stdout.log").write_text(script_output + "\n", encoding="utf-8")
            (run_dir / "stderr.log").write_text("", encoding="utf-8")
            return 200, json.dumps(
                {"run_id": "run-0001", "state": "done", "exit_code": 0,
                 "error": "", "stdout_tail": script_output}).encode("utf-8")

        mock_executor.responses.append(_finish_with_logs)
        result, _ = _run_in_container(tmp_path)

        assert result["status"] == "ok"
        assert result["output"] == {"echo": 1}
        assert result["summary"] == "done"
        assert "duration_ms" in result and "finish_reason" in result
        # 日志归位：cwd 下可读，工作区根已无残留（编排统一按 cwd 解析）
        assert (cwd / "stdout.log").read_text("utf-8").strip() == script_output
        assert not (run_dir / "stdout.log").exists()
        # 请求序列：POST 受理 + GET 收口；GET 寻址 POST 返回的 run_id
        assert [r["method"] for r in mock_executor.requests] == ["POST", "GET"]
        assert mock_executor.gets()[0]["url"] == f"{EXECUTOR_URL}/sandbox/runs/run-0001"
        # stdin 载荷落盘工作区（挂载面内）
        stdin_files = list(run_dir.glob("stdin-aievo-script-*.json"))
        assert len(stdin_files) == 1
        assert json.loads(stdin_files[0].read_text("utf-8"))["_run_dir"] == "/workspace"

    def test_running_then_done_polling(self, tmp_path, mock_executor, monkeypatch):
        """轮询语义：running → done 两次 GET（POLL_INTERVAL 收紧加速）"""
        monkeypatch.setattr(executor_client, "POLL_INTERVAL_SECONDS", 0.01)
        mock_executor.responses.append(
            (200, json.dumps({"state": "running"}).encode("utf-8")))
        result, _ = _run_in_container(tmp_path)
        # 第一次 GET running，第二次默认 done（mock 落盘默认日志）→ 解析成功
        assert result["status"] == "ok"
        assert len(mock_executor.gets()) == 2

    def test_timeout_state_maps_to_finish_reason_timeout(self, tmp_path, mock_executor):
        """服务端超时收口：state=timeout → finish_reason=timeout + 中文超时文案"""
        run_dir, _, _ = _workspace(tmp_path)
        (run_dir / "stdout.log").write_text("", encoding="utf-8")
        (run_dir / "stderr.log").write_text("", encoding="utf-8")
        mock_executor.responses.append((200, json.dumps({
            "state": "timeout", "exit_code": None,
            "error": "沙箱执行超时被终止（>30秒，容器已强制回收）"}).encode("utf-8")))
        result, _ = _run_in_container(tmp_path)
        assert result["status"] == "fail"
        assert result["finish_reason"] == "timeout"
        assert "超时" in result["error"]

    def test_client_poll_deadline_fallback(self, tmp_path, mock_executor, monkeypatch):
        """executor 卡死（恒 running）：客户端轮询死线兜底按超时返回，不死循环
        （死线 = 脚本 timeout + WAIT_MARGIN，两值都收紧到亚秒级加速）"""
        monkeypatch.setattr(executor_client, "POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(executor_client, "WAIT_MARGIN_SECONDS", 0.05)
        mock_executor.default_state = "running"
        result, _ = _run_in_container(tmp_path, timeout=0.1)
        assert result["finish_reason"] == "timeout"
        assert "客户端死线" in result["error"]
        assert len(mock_executor.gets()) >= 2

    def test_failed_state_propagates_error(self, tmp_path, mock_executor):
        """基础设施失败（state=failed）：服务端 error 透传到脚本 fail 条目"""
        mock_executor.responses.append((200, json.dumps({
            "state": "failed", "exit_code": None,
            "error": "docker run 退出码 125: No such image."}).encode("utf-8")))
        result, _ = _run_in_container(tmp_path)
        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "125" in result["error"] and "No such image" in result["error"]

    def test_nonzero_exit_is_script_failure(self, tmp_path, mock_executor):
        """脚本非零退出码（done + exit 2）：按脚本失败处理，附 stderr 尾部"""
        run_dir, _, _ = _workspace(tmp_path)

        def _finish(req):
            (run_dir / "stdout.log").write_text("", encoding="utf-8")
            (run_dir / "stderr.log").write_text("ValueError: boom\n", encoding="utf-8")
            return 200, json.dumps({"state": "done", "exit_code": 2, "error": ""}).encode()

        mock_executor.responses.append(_finish)
        result, _ = _run_in_container(tmp_path)
        assert result["status"] == "fail"
        assert "脚本退出码 2" in result["error"]
        assert "boom" in result["error"]

    def test_unknown_run_404_propagates(self, tmp_path, mock_executor):
        """GET 404（executor 重启丢登记表）：HTTP 404 错误透传（错误传递）"""
        mock_executor.responses.append(_http_error(404, "run not found"))
        result, _ = _run_in_container(tmp_path)
        assert result["status"] == "fail"
        assert "HTTP 404" in result["error"]

    def test_kill_is_explicit_noop_without_transport(self, tmp_path, mock_executor):
        """kill（1-1 契约无远端端点）：显式 no-op——不抛异常、不触传输层"""
        driver = HttpExecutorDriver()
        handle = driver.run(_spec(tmp_path))
        before = len(mock_executor.requests)
        driver.kill(handle)  # 不抛异常
        assert len(mock_executor.requests) == before  # 不发任何请求


# ---------------- docker-CLI 驱动：驱动级生命周期（假 docker CLI） ----------------

class TestDockerCliDriverLifecycle:
    def test_run_wait_status_done(self, tmp_path, monkeypatch):
        run_dir, _, cwd = _workspace(tmp_path)
        fake_cli = tmp_path / "fake_docker.py"
        fake_cli.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "sys.stdin.read()\n"
            "print('{\"status\": \"ok\"}')\n",
            encoding="utf-8")
        fake_cli.chmod(0o755)
        monkeypatch.setattr(script_container, "docker_binary", lambda: str(fake_cli))
        driver = DockerCliDriver()
        handle = driver.run(_spec(tmp_path))
        st = driver.wait(handle, timeout=10)
        assert st.state == "done" and st.exit_code == 0
        # CLI 通道日志直接落 cwd（Popen 重定向）
        assert (cwd / "stdout.log").read_text("utf-8").strip() == '{"status": "ok"}'

    def test_status_running_then_done(self, tmp_path, monkeypatch):
        _workspace(tmp_path)
        cli = tmp_path / "slow_cli.py"
        cli.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(0.3)\n", encoding="utf-8")
        cli.chmod(0o755)
        monkeypatch.setattr(script_container, "docker_binary", lambda: str(cli))
        driver = DockerCliDriver()
        handle = driver.run(_spec(tmp_path))
        assert driver.status(handle).state == "running"
        assert driver.wait(handle, timeout=10).state == "done"

    def test_wait_timeout_kills_and_reports(self, tmp_path, monkeypatch):
        """超时路径：kill（SIGTERM→SIGKILL+rm -f 兜底）后返回 state=timeout"""
        _workspace(tmp_path)
        cli = tmp_path / "stubborn_cli.py"
        cli.write_text(
            f"#!{sys.executable}\n"
            "import signal, sys, time\n"
            "if 'rm' in sys.argv:\n"       # docker rm -f 兜底调用：立即退出
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdin.read()\n"            # 不消费 stdin 也无妨（payload 小于管道缓冲）
            "time.sleep(30)\n",
            encoding="utf-8")
        cli.chmod(0o755)
        monkeypatch.setattr(script_container, "docker_binary", lambda: str(cli))
        monkeypatch.setattr(script_container, "CONTAINER_TERM_GRACE_SECONDS", 0.2)
        driver = DockerCliDriver()
        handle = driver.run(_spec(tmp_path))
        st = driver.wait(handle, timeout=0.2)
        assert st.state == "timeout"
        assert st.exit_code is not None  # 进程已被 kill 并回收


# ---------------- 分流点：container → executor 驱动 / local 不变 ----------------

_NET_SRC = (
    "def run(stdin):\n"
    "    return {\"status\": \"ok\", \"output\": {\"echo\": stdin.get(\"task_id\")}, \"summary\": \"ok\"}\n"
)


def _stage_local_script(tmp_path, monkeypatch, script_id="pjs-demo"):
    """local/分流测试的脚本包与目录隔离（test_w15_container_isolation 同模式）"""
    upload_base = tmp_path / "uploads" / "pre_judge_scripts"
    (upload_base / script_id).mkdir(parents=True, exist_ok=True)
    (upload_base / script_id / "main.py").write_text(_NET_SRC, encoding="utf-8")
    monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", str(upload_base))
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "runs")


class TestDispatchByDriver:
    def test_container_mode_dispatches_http_driver(self, tmp_path, monkeypatch):
        """分流点按驱动选择：SCRIPT_EXEC_MODE=container + EXECUTOR_URL →
        execute_script_in_container 收到 HttpExecutorDriver 实例（目标通道）"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
        monkeypatch.setenv("EXECUTOR_URL", EXECUTOR_URL)
        _stage_local_script(tmp_path, monkeypatch)
        monkeypatch.setattr(pre_judge_runner, "_load_script",
                            lambda sid: {"script_id": sid, "name": sid,
                                         "exec_type": "script",
                                         "entrypoint": "main.py", "timeout_seconds": 30})
        captured = {}

        def fake_exec(**kwargs):
            captured.update(kwargs)
            return pre_judge_runner._result("pjs-demo", "pjs-demo", "ok",
                                            finish_reason="completed")

        monkeypatch.setattr(script_container, "execute_script_in_container", fake_exec)
        result = pre_judge_runner.execute_script(
            {"script_id": "pjs-demo", "name": "pjs-demo", "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 30},
            {"task_id": "t1", "data_id": "d1"})
        assert result["status"] == "ok"
        assert isinstance(captured["driver"], HttpExecutorDriver)
        assert captured["network"] is False

    def test_local_mode_ignores_executor_url(self, tmp_path, monkeypatch):
        """local 模式行为不变：即便 EXECUTOR_URL 已配置也不解析驱动，
        走原 subprocess 通道真实执行成功（回归护栏；ARCH-1-4 后 local
        须显式声明——缺省已切 container）"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "local")
        monkeypatch.setenv("EXECUTOR_URL", EXECUTOR_URL)

        def _fail_driver():
            raise AssertionError("local 模式不应解析沙箱驱动")

        monkeypatch.setattr(script_container, "get_sandbox_driver", _fail_driver)
        _stage_local_script(tmp_path, monkeypatch)
        result = pre_judge_runner.execute_script(
            {"script_id": "pjs-demo", "name": "pjs-demo", "exec_type": "script",
             "entrypoint": "main.py", "timeout_seconds": 30},
            {"task_id": "t1", "data_id": "d1"})
        assert result["status"] == "ok"
        assert result["output"] == {"echo": "t1"}
