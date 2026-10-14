"""executor 沙箱执行服务单元测试（L1，mock subprocess，无真 docker 依赖）

设计条目 1-1（plans/2026-10-15-企业级评测系统架构设计.md §3 Phase 1 表 1-1 行）：
executor 健康/白名单拒绝（privileged/越界 volume/未知镜像/未知网络档/错 token）单测。

覆盖：
- 鉴权：错 token/缺 token → 401；EXECUTOR_TOKEN 未配置 → fail-closed 503；
  /health 无鉴权可达
- 白名单拒绝（400/422，且一律不触碰 docker CLI——默认 deny 记录器被调即失败）：
  * 未知字段注入 privileged/pid_host/volumes/docker_args → 422（extra="forbid"：
    额外 docker 能力没有字段可表达）
  * 越界 workspace：script_runs 之外、../ 穿越、symlink 逃逸、根本身、
    不存在目录 → 400（realpath 校验）
  * 未知镜像：非 script-runner 系、registry 前缀、digest 引用、flag 样值 → 400
  * 未知网络档：bridge/open/host/空 → 400（仅 none|egress）
  * entrypoint 非法：空列表、空元素、NUL、首元素 flag 样值 → 422/400
  * timeout 越界（0/负/超上限）→ 422
  * workdir 越界：挂载根之外、../ 穿越、前缀仿冒、相对路径、flag 样值、空串、
    NUL → 400（仅 /workspace 子树，单一事实源 resolve_container_workdir）
- 受理与生命周期（FakeDocker 按子命令分发）：
  * 202 + run_id；docker run 参数全套加固位（--user 10001:10001、--cap-drop
    ALL、no-new-privileges、--read-only、--network none、唯一 -v 落在
    script_runs 子树、-d 后台、无 --privileged）
  * egress 档位 → --network 映射沙箱出网 bridge
  * workdir 透传 -w（per-script cwd；规范化；缺省挂载根行为不变）
  * run→wait→logs→rm 序列；stdout/stderr 落工作区；GET 状态 done + exit_code
  * 非零退出码仍算 done（脚本失败 ≠ 基础设施失败）
  * 超时路径：wait 超时 → rm -f 强杀、state=timeout
  * docker run 被 daemon 拒绝（125）→ 500
  * GET 未知 run_id → 404
- 审计：受理（全参数）与完成（state+exit_code）落 executor.audit INFO；
  白名单拒绝落 WARNING（含原因与原始参数）
- 单一事实源：worker 通道 script_container.build_docker_run_args 与 executor
  构造共用 sandbox_params（--cap-drop ALL/no-new-privileges 一致，预对齐 1-5）
"""
import io
import json
import logging
import os
import subprocess
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit  # W8 分类门禁：L1 单元测试（无网络/无 DB）

from executor import app as executor_app
from executor import sandbox_params
from executor.sandbox_params import SandboxParamError
from app.services import script_container

TOKEN = "unit-test-executor-token"


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _valid_request(root: Path, **overrides) -> dict:
    """合法请求载荷（workspace 用 fixture 根下的真实目录）"""
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

    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        raise AssertionError(f"该用例不应调用 docker CLI: {args}")


class FakeDocker:
    """按子命令（run/wait/logs/rm）分发的假 docker CLI，记录全调用序列"""

    def __init__(self):
        self.calls = []
        self.run_returncode = 0
        self.run_stderr = b""
        self.run_raise = None
        self.wait_output = "0"
        self.wait_timeout = False
        self.logs_stdout = b'{"status": "ok", "output": {"echo": 1}}\n'
        self.logs_stderr = b""

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        sub = args[1] if len(args) > 1 else ""
        if sub == "run":
            if self.run_raise:
                raise self.run_raise
            return subprocess.CompletedProcess(
                args, self.run_returncode, stdout=b"f4kec0nta1nerid", stderr=self.run_stderr)
        if sub == "wait":
            if self.wait_timeout:
                raise subprocess.TimeoutExpired(args, 1.0)
            return subprocess.CompletedProcess(
                args, 0, stdout=self.wait_output.encode(), stderr=b"")
        if sub == "logs":
            return subprocess.CompletedProcess(
                args, 0, stdout=self.logs_stdout, stderr=self.logs_stderr)
        if sub == "rm":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"未预期的 docker 调用: {args}")


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """已鉴权环境的测试客户端；docker CLI 默认替换为 deny 记录器"""
    monkeypatch.setenv(executor_app.EXECUTOR_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(executor_app.subprocess, "run", _DenyDocker())
    return TestClient(executor_app.app)


@pytest.fixture
def ws_root(tmp_path, monkeypatch) -> Path:
    """测试工作区根：生产硬编码 /app/uploads/script_runs，单测无 /app 写权限，
    monkeypatch 到临时目录（不构成生产配置面——resolve_workspace_mount 读模块
    常量，env 不可覆盖）"""
    root = tmp_path / "uploads" / "script_runs"
    (root / "t1" / "d1").mkdir(parents=True)
    monkeypatch.setattr(sandbox_params, "WORKSPACE_ROOT", str(root))
    return root


@pytest.fixture
def fake_docker(monkeypatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(executor_app.subprocess, "run", fake)
    return fake


# ---------------- 鉴权（EXECUTOR_TOKEN） ----------------

class TestAuth:
    def test_wrong_token_rejected_401(self, client, ws_root):
        resp = client.post("/sandbox/run", json=_valid_request(ws_root),
                           headers=_auth("wrong-token"))
        assert resp.status_code == 401

    def test_missing_token_rejected_401(self, client, ws_root):
        resp = client.post("/sandbox/run", json=_valid_request(ws_root))
        assert resp.status_code == 401

    def test_no_token_configured_fail_closed_503(self, client, monkeypatch, ws_root):
        """EXECUTOR_TOKEN 未配置 = fail-closed：拒绝全部沙箱请求，绝不放行"""
        monkeypatch.delenv(executor_app.EXECUTOR_TOKEN_ENV, raising=False)
        resp = client.post("/sandbox/run", json=_valid_request(ws_root),
                           headers=_auth("anything"))
        assert resp.status_code == 503
        assert "fail-closed" in resp.json()["detail"]

    def test_health_open_without_token(self, client):
        """/health 无鉴权（compose healthcheck 用），非 401"""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------- 白名单拒绝 ----------------

class TestWhitelistRejects:
    def test_extra_field_privileged_rejected_422(self, client, ws_root):
        """privileged 没有字段可表达：注入未知字段在反序列化层即拒（extra=forbid）"""
        payload = _valid_request(ws_root, privileged=True)
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 422

    def test_extra_field_pid_host_and_volumes_rejected_422(self, client, ws_root):
        payload = _valid_request(ws_root, pid_host=True)
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 422
        payload = _valid_request(ws_root, volumes=["/:/host"])
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 422

    def test_extra_field_raw_docker_args_rejected_422(self, client, ws_root):
        payload = _valid_request(ws_root, docker_args=["--privileged", "--pid=host"])
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 422

    def test_workspace_outside_script_runs_rejected_400(self, client, ws_root):
        payload = _valid_request(ws_root, workspace="/etc")
        resp = client.post("/sandbox/run", json=payload, headers=_auth())
        assert resp.status_code == 400
        assert "script_runs" in resp.json()["detail"]

    def test_workspace_parent_traversal_rejected_400(self, client, ws_root):
        """../ 字面穿越：realpath 落到 script_runs 之外 → 拒绝"""
        payload = _valid_request(ws_root, workspace=str(ws_root / "t1" / ".." / ".." / "evil"))
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 400

    def test_workspace_symlink_escape_rejected_400(self, client, ws_root, tmp_path):
        """symlink 逃逸：子树内软链指向子树外 → realpath 解析后拒绝"""
        outside = tmp_path / "outside"
        outside.mkdir()
        link = ws_root / "t1" / "escape"
        link.symlink_to(outside)
        payload = _valid_request(ws_root, workspace=str(link))
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 400

    def test_workspace_root_itself_rejected_400(self, client, ws_root):
        """挂整个 script_runs 根：挂载面过宽 → 拒绝"""
        payload = _valid_request(ws_root, workspace=str(ws_root))
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 400

    def test_workspace_missing_dir_rejected_400(self, client, ws_root):
        """工作区必须先由 worker 准备好：不存在即拒绝"""
        payload = _valid_request(ws_root, workspace=str(ws_root / "t1" / "nope"))
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 400

    @pytest.mark.parametrize("bad_image", [
        "ubuntu:latest",                                        # 非 script-runner 系
        "registry.example.com/ai-evo-script-runner:latest",     # registry 前缀
        "ai-evo-script-runner@sha256:deadbeef",                 # digest 引用（3-3 才开）
        "-v",                                                   # flag 样值（防旗标注入）
        "ai-evo-evil-runner:latest",                            # 前缀仿冒
        "ai-evo-script-runner:latest;rm -rf /",                 # tag 含非法字符
    ])
    def test_unknown_image_rejected_400(self, client, ws_root, bad_image):
        payload = _valid_request(ws_root, image=bad_image)
        resp = client.post("/sandbox/run", json=payload, headers=_auth())
        assert resp.status_code == 400
        assert resp.json()["detail"]  # 拒绝原因非空（白名单/非法取值/tag 非法）

    @pytest.mark.parametrize("bad_profile", ["bridge", "open", "host", "", "none extra"])
    def test_unknown_net_profile_rejected_400(self, client, ws_root, bad_profile):
        """网络档位仅 none|egress（设计 D2：open 档属超管审批流程，1-1 不开放）"""
        payload = _valid_request(ws_root, net_profile=bad_profile)
        resp = client.post("/sandbox/run", json=payload, headers=_auth())
        assert resp.status_code == 400
        assert "网络档位" in resp.json()["detail"]

    def test_empty_entrypoint_rejected_422(self, client, ws_root):
        payload = _valid_request(ws_root, entrypoint=[])
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 422

    def test_blank_entrypoint_element_rejected_400(self, client, ws_root):
        payload = _valid_request(ws_root, entrypoint=[""])
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 400

    def test_entrypoint_nul_rejected_400(self, client, ws_root):
        payload = _valid_request(ws_root, entrypoint=["python", "a\x00b"])
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 400

    def test_entrypoint_flag_like_first_element_rejected_400(self, client, ws_root):
        """首元素 flag 样值：防被 docker CLI 解析成额外旗标（纵深防御）"""
        payload = _valid_request(ws_root, entrypoint=["--privileged", "/x"])
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 400

    @pytest.mark.parametrize("bad_timeout", [0, -1, 3601])
    def test_timeout_out_of_bounds_rejected_422(self, client, ws_root, bad_timeout):
        payload = _valid_request(ws_root, timeout=bad_timeout)
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 422

    @pytest.mark.parametrize("bad_workdir", [
        "/etc",                         # 挂载根之外（镜像只读层任意位置）
        "/workspace/../etc",            # .. 穿越出挂载根
        "/workspace/s1/../../tmp",      # 多级 .. 穿越
        "/workspacefoo",                # 前缀仿冒（非子路径）
        "relative/s1",                  # 相对路径（-w 必须绝对）
        "-w",                           # 旗标样值（防被 docker CLI 解析成额外旗标）
        "",                             # 空串（显式带字段却为空：拒绝，与 net_profile 同口径）
        "/workspace/s1\x00",            # NUL
    ])
    def test_workdir_outside_workspace_rejected_400(self, client, ws_root, bad_workdir):
        """容器内工作目录白名单：仅 /workspace 子树（单一事实源
        sandbox_params.resolve_container_workdir），越界/穿越/相对/旗标/NUL → 400"""
        payload = _valid_request(ws_root, workdir=bad_workdir)
        resp = client.post("/sandbox/run", json=payload, headers=_auth())
        assert resp.status_code == 400
        assert "workdir" in resp.json()["detail"]


# ---------------- 受理与生命周期 ----------------

class TestRunLifecycle:
    def test_accepted_202_with_hardened_docker_args(self, client, ws_root, fake_docker):
        """受理：202 + run_id；docker run 参数全套加固位（设计 D1 白名单落位）"""
        payload = _valid_request(ws_root)
        resp = client.post("/sandbox/run", json=payload, headers=_auth())
        assert resp.status_code == 202
        body = resp.json()
        assert body["run_id"] and body["container_name"].startswith("aievo-sbx-")

        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert run_cmd[0] == "docker"
        args = run_cmd[1:]
        # 后台受理（-d、无 --rm：退出后 logs 取日志再显式回收）
        assert args[0] == "run" and "-d" in args and "--rm" not in args
        # 固定用户/资源上限/加固位
        assert args[args.index("--user") + 1] == "10001:10001"
        assert args[args.index("--memory") + 1] == "1g"
        assert args[args.index("--memory-swap") + 1] == "1g"
        assert args[args.index("--cpus") + 1] == "0.5"
        assert args[args.index("--pids-limit") + 1] == "128"
        assert args[args.index("--cap-drop") + 1] == "ALL"
        assert "no-new-privileges:true" in args
        assert "--read-only" in args
        # 拒绝面：无任何特权旗标（--pid 宿主命名空间旗标不得出现；
        # --pids-limit 是白名单资源上限，二者前缀不同不冲突）
        assert "--privileged" not in args
        assert "--pid" not in args and not any(a.startswith("--pid=") for a in args)
        assert "--pids-limit" in args
        assert not any(a.startswith("--device") for a in args)
        # 默认档 none；env 仅最小集合
        assert args[args.index("--network") + 1] == "none"
        envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert sorted(e.split("=", 1)[0] for e in envs) == \
            sorted(sandbox_params.CONTAINER_ENV_ALLOWLIST)
        # 唯一 -v：realpath 校验后的 script_runs 子树 → /workspace
        binds = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        ws_real = os.path.realpath(str(ws_root / "t1" / "d1"))
        assert binds == [f"{ws_real}:/workspace"]
        # 尾部命令 = 请求 entrypoint（exec 形式，镜像后）
        assert args[args.index(payload["image"]) + 1:] == payload["entrypoint"]

    def test_lifecycle_wait_logs_rm_and_get_done(self, client, ws_root, fake_docker):
        """生命周期：run→wait→logs→rm；日志落工作区；GET 查询 done + exit_code"""
        resp = client.post("/sandbox/run", json=_valid_request(ws_root), headers=_auth())
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]
        # TestClient 同步执行后台任务：post 返回即监督序列已完成
        assert [c[1] for c in fake_docker.calls] == ["run", "wait", "logs", "rm"]
        # stdout/stderr 落工作区（worker 侧按文件口径解析，1-2 接管后不变）
        ws = ws_root / "t1" / "d1"
        assert (ws / "stdout.log").read_bytes() == fake_docker.logs_stdout
        assert (ws / "stderr.log").read_bytes() == fake_docker.logs_stderr

        resp2 = client.get(f"/sandbox/runs/{run_id}", headers=_auth())
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["state"] == "done"
        assert body["exit_code"] == 0
        assert '"status"' in body["stdout_tail"]
        assert body["container_name"] == resp.json()["container_name"]

    def test_egress_profile_maps_to_sandbox_network(self, client, ws_root, fake_docker):
        """egress 档 → --network 沙箱出网 bridge（白名单域名出网，1-3 建网）"""
        payload = _valid_request(ws_root, net_profile="egress")
        resp = client.post("/sandbox/run", json=payload, headers=_auth())
        assert resp.status_code == 202
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert run_cmd[run_cmd.index("--network") + 1] == \
            sandbox_params.NET_PROFILES["egress"]

    def test_workdir_passed_through_to_dash_w(self, client, ws_root, fake_docker):
        """请求 workdir（worker per-script cwd /workspace/{script_id}）→ docker run -w
        同值：harness 三件套/脚本 artifacts 按 cwd 相对寻址，-w 不得硬编码挂载根"""
        payload = _valid_request(ws_root, workdir="/workspace/s1")
        resp = client.post("/sandbox/run", json=payload, headers=_auth())
        assert resp.status_code == 202
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert run_cmd[run_cmd.index("-w") + 1] == "/workspace/s1"
        # 挂载点仍是挂载根（-w 在挂载面内部，不改变 -v）
        binds = [run_cmd[i + 1] for i, a in enumerate(run_cmd) if a == "-v"]
        assert binds == [f"{os.path.realpath(str(ws_root / 't1' / 'd1'))}:/workspace"]

    def test_workdir_normalized_before_dash_w(self, client, ws_root, fake_docker):
        """posix 规范化：尾斜杠/./多斜杠归一后进 -w（仍在子树内即放行）"""
        payload = _valid_request(ws_root, workdir="/workspace/./s1//")
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 202
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert run_cmd[run_cmd.index("-w") + 1] == "/workspace/s1"

    def test_workdir_omitted_defaults_to_workspace_root(self, client, ws_root, fake_docker):
        """未带 workdir 字段（既有调用方）→ -w /workspace（挂载根），行为不变"""
        payload = _valid_request(ws_root)
        assert "workdir" not in payload
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 202
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert run_cmd[run_cmd.index("-w") + 1] == sandbox_params.CONTAINER_WORKSPACE

    def test_nonzero_exit_is_done_with_exit_code(self, client, ws_root, fake_docker):
        """脚本非零退出码仍是 done（脚本失败 ≠ 基础设施失败），exit_code 透传"""
        fake_docker.wait_output = "1"
        resp = client.post("/sandbox/run", json=_valid_request(ws_root), headers=_auth())
        run_id = resp.json()["run_id"]
        body = client.get(f"/sandbox/runs/{run_id}", headers=_auth()).json()
        assert body["state"] == "done" and body["exit_code"] == 1

    def test_timeout_kills_and_marks_timeout(self, client, ws_root, fake_docker):
        """超时路径：docker wait 超时 → rm -f 强杀，state=timeout"""
        fake_docker.wait_timeout = True
        resp = client.post("/sandbox/run", json=_valid_request(ws_root), headers=_auth())
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]
        assert [c[1] for c in fake_docker.calls] == ["run", "wait", "logs", "rm"]
        rm_cmd = fake_docker.calls[-1]
        assert rm_cmd[1:3] == ["rm", "-f"]
        body = client.get(f"/sandbox/runs/{run_id}", headers=_auth()).json()
        assert body["state"] == "timeout"
        assert body["exit_code"] is None
        assert "超时" in body["error"]

    def test_docker_run_rejected_by_daemon_500(self, client, ws_root, fake_docker):
        """daemon 拒绝 run（如镜像缺失，退出码 125）→ 500 + 拒绝原因"""
        fake_docker.run_returncode = 125
        fake_docker.run_stderr = b"docker: Error response from daemon: No such image."
        resp = client.post("/sandbox/run", json=_valid_request(ws_root), headers=_auth())
        assert resp.status_code == 500
        assert "125" in resp.json()["detail"]

    def test_get_unknown_run_404(self, client, ws_root):
        resp = client.get("/sandbox/runs/deadbeef", headers=_auth())
        assert resp.status_code == 404


# ---------------- 审计日志 ----------------

class TestAudit:
    def test_audit_wiring_info_records_have_sink(self):
        """生产 wiring（设计 D7，回归门禁）：executor.audit 显式 INFO 级别 +
        至少一个落点（自身 handler 或 root handler）——裸 uvicorn 下 root 无
        handler 且缺省有效级别 WARNING，缺本 wiring 则 INFO 审计全被丢弃
        （此前用 caplog.set_level 抬级才通过，掩盖了生产缺 wiring）"""
        assert executor_app.audit_logger.level == logging.INFO
        assert executor_app.audit_logger.isEnabledFor(logging.INFO)
        sinks = executor_app.audit_logger.handlers or logging.getLogger().handlers
        assert sinks, "executor.audit 与 root 均无 handler：INFO 审计无落点"

    def test_accept_and_finish_logged_with_full_params_and_exit_code(
            self, client, ws_root, fake_docker, caplog):
        """审计（设计 D7）：受理（全参数+挂载+容器名）与完成（state+exit_code）
        都落 executor.audit（不经 caplog.set_level 抬级——生产 wiring 已保证
        INFO 记录可发，见 test_audit_wiring_info_records_have_sink）"""
        payload = _valid_request(ws_root)
        assert client.post("/sandbox/run", json=payload, headers=_auth()).status_code == 202
        messages = [r.getMessage() for r in caplog.records]
        accepted = [m for m in messages if "accepted" in m]
        assert accepted, messages
        m = accepted[0]
        assert payload["image"] in m and "'net_profile': 'none'" in m
        assert "'timeout': 30.0" in m and payload["entrypoint"][1] in m
        assert os.path.realpath(payload["workspace"]) in m
        finished = [m for m in messages if "finished" in m]
        assert finished and "state=done" in finished[0] and "exit_code=0" in finished[0]

    def test_rejection_logged_with_reason(self, client, ws_root, caplog):
        """白名单拒绝也审计：WARNING 含原因与原始参数"""
        resp = client.post("/sandbox/run", json=_valid_request(ws_root, image="ubuntu:latest"),
                           headers=_auth())
        assert resp.status_code == 400
        assert any("reject" in r.getMessage() and "ubuntu:latest" in r.getMessage()
                   for r in caplog.records)


# ---------------- 单一事实源（worker 通道 ↔ executor 共用 sandbox_params） ----------------

class TestSingleSourceWithWorkerChannel:
    def test_worker_channel_args_share_hardening(self, tmp_path):
        """worker docker-CLI 通道与 executor 同一构造源：W15 断言全保留 +
        --cap-drop ALL / no-new-privileges（预对齐 Phase 1-5 门禁扩项）"""
        run_dir = tmp_path / "script_runs" / "t1" / "d1"
        staged = run_dir / "scripts" / "my-script"
        cwd = run_dir / "my-script"
        paths = script_container.build_container_paths(run_dir, cwd, staged)
        args = script_container.build_docker_run_args(
            image="ai-evo-script-runner:latest", user="10001:10001",
            paths=paths, entry_rel="main.py",
            container_name="aievo-script-s1-abc", network=False)
        # W15 既有断言（test_w15_container_isolation 同口径）
        assert args[0:2] == ["run", "--rm"]
        assert args[args.index("--network") + 1] == "none"
        assert "--read-only" in args and "-i" in args
        # ARCH-1-1 加固位随抽取统一落位
        assert args[args.index("--cap-drop") + 1] == "ALL"
        assert "no-new-privileges:true" in args
        assert "--privileged" not in args

    def test_docker_binary_is_shared_symbol(self):
        """docker_binary 同一函数对象（re-export，非复制粘贴）"""
        assert script_container.docker_binary is sandbox_params.docker_binary

    def test_sandbox_param_error_is_value_error(self):
        """executor 统一捕获口径：SandboxParamError 是 ValueError 子类"""
        assert issubclass(SandboxParamError, ValueError)

    def test_resolve_container_workdir_single_source(self):
        """workdir 白名单单一事实源：None → 挂载根；子树内放行并规范化；越界拒绝"""
        ws = sandbox_params.CONTAINER_WORKSPACE
        assert sandbox_params.resolve_container_workdir(None) == ws
        assert sandbox_params.resolve_container_workdir(ws) == ws
        assert sandbox_params.resolve_container_workdir(f"{ws}/") == ws
        assert sandbox_params.resolve_container_workdir(f"{ws}/pjs_agent") == f"{ws}/pjs_agent"
        assert sandbox_params.resolve_container_workdir(f"{ws}/a/./b//") == f"{ws}/a/b"
        for bad in ("/etc", f"{ws}/../etc", f"{ws}foo", "rel", "-w", "", "//workspace"):
            with pytest.raises(SandboxParamError):
                sandbox_params.resolve_container_workdir(bad)


# ---------------- worker HTTP 驱动 ↔ executor 线上契约（请求体原样喂 executor） ----------------

class TestWorkerHttpDriverWireContract:
    """HttpExecutorDriver 真实构造的请求体 → executor 受理：-w 必须等于 spec.workdir_container

    回归护栏（评审阻断项）：agent 评测步骤（execute_harness_in_container）的
    entrypoint 以裸相对文件名引用 staged 三件套，依赖 -w 指向 per-script cwd
    （/workspace/{script_id}）；此前请求体无 workdir 字段且 executor 硬编码
    -w /workspace，harness_entry 在挂载根找不到三件套 → FileNotFoundError 退出 1。
    本用例不 mock 任何一侧的组包/校验逻辑：客户端唯一 I/O 出口 _http_open 被
    替换为「转发到 executor TestClient」，走的就是生产线上的字节。
    """

    def _wire(self, monkeypatch, client):
        """把 executor_client._http_open 接到 executor TestClient（同进程转发）"""
        from app.services import executor_client

        monkeypatch.setenv("EXECUTOR_URL", "http://executor:8300")
        monkeypatch.setenv("EXECUTOR_TOKEN", TOKEN)
        captured = {}

        def _forward(req, timeout):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            captured["body"] = body
            path = req.full_url[len("http://executor:8300"):]
            resp = client.request(req.method, path, json=body,
                                  headers={"Authorization": req.get_header("Authorization")})
            if resp.status_code >= 400:
                raise urllib.error.HTTPError(req.full_url, resp.status_code, "error", None,
                                             io.BytesIO(resp.content))
            return resp.status_code, resp.content

        monkeypatch.setattr(executor_client, "_http_open", _forward)
        return captured

    def test_harness_spec_workdir_reaches_dash_w(self, client, ws_root, fake_docker, monkeypatch):
        """harness 形状的 spec（三件套裸相对文件名）经真实 HttpExecutorDriver.run →
        executor docker run 的 -w == spec.workdir_container（per-script cwd）"""
        from app.services.executor_client import HttpExecutorDriver

        captured = self._wire(monkeypatch, client)
        run_dir = ws_root / "t1" / "d1"
        cwd = run_dir / "pjs_agent"
        cwd.mkdir()
        paths = script_container.build_container_paths(run_dir, cwd, cwd)
        assert paths.cwd_container == "/workspace/pjs_agent"  # 评审 spec 期望值
        spec = script_container.SandboxRunSpec(
            image="ai-evo-script-runner:harness-claude", user="10001:10001",
            workspace_host=paths.run_dir_host, workspace_container=paths.run_dir_container,
            workdir_container=paths.cwd_container, cwd_host=paths.cwd_host,
            net_profile="egress", container_name="aievo-script-harness-pjs-agent-abc",
            entrypoint=[script_container.CONTAINER_PYTHON, script_container.CONTAINER_HARNESS_ENTRY,
                        "claude", script_container.HARNESS_INSTRUCTION_FILE,
                        script_container.HARNESS_STDIN_FILE, script_container.HARNESS_CONFIG_FILE],
            stdin_payload=b"", timeout=90.0,
        )
        handle = HttpExecutorDriver().run(spec)
        assert handle.run_id

        # 线上请求体带 workdir 且 executor 受理（非 400/422）
        assert captured["body"]["workdir"] == "/workspace/pjs_agent"
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert run_cmd[run_cmd.index("-w") + 1] == spec.workdir_container == "/workspace/pjs_agent"
        # 三件套在 argv 里仍是裸相对文件名（契约不变）——正因如此 -w 必须是 cwd
        shell_cmd = run_cmd[-1]
        for name in (script_container.HARNESS_INSTRUCTION_FILE,
                     script_container.HARNESS_STDIN_FILE,
                     script_container.HARNESS_CONFIG_FILE):
            assert f" {name}" in shell_cmd and f"/{name}" not in shell_cmd.split("<")[0]

    def test_script_spec_workdir_reaches_dash_w(self, client, ws_root, fake_docker, monkeypatch):
        """脚本通道同口径：execute_script_in_container 推导的 cwd_container 到达 -w"""
        from app.services.executor_client import HttpExecutorDriver

        captured = self._wire(monkeypatch, client)
        run_dir = ws_root / "t1" / "d1"
        staged = run_dir / "scripts" / "s1"
        staged.mkdir(parents=True)
        cwd = run_dir / "s1"
        cwd.mkdir()
        paths = script_container.build_container_paths(run_dir, cwd, staged)
        spec = script_container.SandboxRunSpec(
            image="ai-evo-script-runner:latest", user="10001:10001",
            workspace_host=paths.run_dir_host, workspace_container=paths.run_dir_container,
            workdir_container=paths.cwd_container, cwd_host=paths.cwd_host,
            net_profile="none", container_name="aievo-script-s1-abc",
            entrypoint=[script_container.CONTAINER_PYTHON, script_container.CONTAINER_ENTRY_WRAPPER,
                        paths.staged_dir_container, "main.py"],
            stdin_payload=b"{}", timeout=30.0,
        )
        HttpExecutorDriver().run(spec)
        assert captured["body"]["workdir"] == "/workspace/s1"
        run_cmd = next(c for c in fake_docker.calls if c[1] == "run")
        assert run_cmd[run_cmd.index("-w") + 1] == "/workspace/s1"
