"""沙箱逃逸测试集（ARCH-1-5，设计 plans/2026-10-15-企业级评测系统架构设计.md
§3 Phase 1 表 1-4 验收行「逃逸测试集……全部被沙箱拦截」/ 表 1-5 行 / §6 文件清单）

分层（按「需不需要真 docker」切两档）：

**参数构造层（unit 档，无 docker/网络/DB，CI 常跑）**
- 危险 docker 参数绝不出现：privileged、--pid/--ipc/--network host、任意路径
  挂载、root 用户、额外 capabilities（--cap-add）——旗标全量白名单扫描 +
  逐项反向断言，worker 前台形态与 executor detach 形态共用单一事实源
  executor/sandbox_params，两种形态都钉；
- 网络档正确映射：worker 通道三档（none/egress/open→none/aievo-sandbox-egress/
  bridge）、executor 通道白名单（none/egress，host/open 一律拒）；
- container 路径不注入 PYTHONPATH：env 白名单恰为最小集合，无平台 import 面；
- 超时与资源上限在场：--memory/--memory-swap/--cpus/--pids-limit/--read-only/
  tmpfs 固定值，timeout 在请求模型与执行 spec 两侧强制在场且有界。

**故意反例（unit 档）**——证明校验器抓得住：按攻击者视角构造非法参数
（privileged/pid_host/volumes 注入、镜像冒名、越界 bind mount/符号链接逃逸/
字面 ../ 穿越、host 网络、值即旗标注入、entrypoint 旗标/NUL 注入），逐项
断言被对应校验器拒绝（SandboxParamError/ValidationError），不进 docker CLI。

**实机逃逸集（sandbox_e2e 档，默认 deselect，-m sandbox_e2e 显式跑）**——
经生产通道 execute_script_in_container（docker-CLI 驱动 + script-runner 镜像）
真跑五类逃逸探针：import 平台代码、读 /proc/1/environ、fork 炸弹、写越界
路径、超长运行，外加默认断网出网探测。docker CLI 或 runner 镜像不可用时
明确 skip（不假绿）。
"""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.services import script_container
from executor import sandbox_params
from executor.sandbox_params import SandboxParamError


# ---------------- 公共构造工具（两种通道形态各一份，与生产同构） ----------------

def _worker_shaped_args(tmp_path: Path):
    """worker docker-CLI 前台形态（-i、--rm、挂本次 script_runs 工作目录）"""
    paths = script_container.build_container_paths(
        tmp_path / "script_runs" / "t1" / "d1",
        tmp_path / "script_runs" / "t1" / "d1" / "s1",
        tmp_path / "script_runs" / "t1" / "d1" / "scripts" / "s1",
    )
    args = script_container.build_docker_run_args(
        image="ai-evo-script-runner:latest", user="10001:10001",
        paths=paths, entry_rel="main.py",
        container_name="aievo-script-s1-abc", network=False,
    )
    return args, paths


def _executor_shaped_args(tmp_path: Path, network_name: str = "none"):
    """executor detach 形态（-d、无 --rm、挂白名单校验后的工作区子树）"""
    mount_host = tmp_path / "script_runs" / "t1" / "d1"
    mount_host.mkdir(parents=True, exist_ok=True)
    return sandbox_params.build_docker_run_args(
        image="ai-evo-script-runner:latest",
        user=sandbox_params.SANDBOX_USER,
        mount_host=str(mount_host),
        mount_container=sandbox_params.CONTAINER_WORKSPACE,
        workdir_container=sandbox_params.CONTAINER_WORKSPACE,
        network_name=network_name,
        container_name="aievo-sbx-abc123def456",
        tail_command=["python", sandbox_params.CONTAINER_ENTRY_WRAPPER,
                      "/workspace/scripts/s1", "main.py"],
        detach=True, auto_rm=False,
    )


# 参数构造允许出现的全部 docker 旗标（白名单语义：builder 未来私增任何旗标
# ——privileged/--pid/--device/…——先撞上本门禁，必须连设计一起改）
_ALLOWED_FLAGS = {
    "run", "--rm", "-d", "--name", "--user", "--memory", "--memory-swap",
    "--cpus", "--pids-limit", "--cap-drop", "--security-opt", "--read-only",
    "--tmpfs", "--network", "-e", "-i", "-v", "-w",
}


# ---------------- 参数构造层：危险 docker 参数绝不出现 ----------------

@pytest.mark.unit
class TestNoDangerousDockerArgs:
    """逃逸面 #1：容器本身不得拿到宿主级 docker 能力（设计 §2.3 D1 白名单）"""

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_flag_whitelist_scan(self, tmp_path, shape):
        """旗标全量白名单扫描：args 里一切 "-" 开头的 token 必须在允许清单内
        ——privileged/--pid/--ipc/--device/--dns/… 没有出现通道"""
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        offenders = [tok for tok in args if tok.startswith("-") and tok not in _ALLOWED_FLAGS]
        assert not offenders, f"docker run 出现白名单外旗标: {offenders}"

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_no_privileged(self, tmp_path, shape):
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        assert "--privileged" not in args

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_no_host_namespace_sharing(self, tmp_path, shape):
        """--pid/--ipc/--uts/--cgroupns host 共享 = 宿主进程命名空间可见，一律无；
        --network 的取值面也绝不含 host"""
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        for flag in ("--pid", "--ipc", "--uts", "--cgroupns", "--network-alias",
                     "--dns", "--device", "--device-cgroup-rule", "--gpus",
                     "--sysctl", "--ulimit", "--add-host", "--link", "--cap-add",
                     "--security-opt[privileged]"):
            assert flag not in args
        assert args[args.index("--network") + 1] != "host"

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_single_mount_within_workspace_only(self, tmp_path, shape):
        """逃逸面 #2：挂载仅单一工作区——docker.sock / 宿主根 / 任意路径挂载
        没有通道（-v 恰一个，源即工作区；无 --mount 长形式）"""
        if shape == "worker":
            args, paths = _worker_shaped_args(tmp_path)
            expected_src = paths.run_dir_host
        else:
            args = _executor_shaped_args(tmp_path)
            expected_src = str(tmp_path / "script_runs" / "t1" / "d1")
        binds = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        assert binds == [f"{expected_src}:{sandbox_params.CONTAINER_WORKSPACE}"]
        assert "--mount" not in args
        for bind in binds:
            assert "docker.sock" not in bind
            assert not bind.startswith("/:")

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_non_root_user(self, tmp_path, shape):
        """逃逸面 #3：非 root 运行（root 容器 = 单个内核漏洞即宿主沦陷）"""
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        user = args[args.index("--user") + 1]
        assert user == "10001:10001"
        assert user not in ("root", "0:0", "0")

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_no_extra_capabilities_and_no_privilege_escalation(self, tmp_path, shape):
        """逃逸面 #4：--cap-add 无通道；--cap-drop ALL + no-new-privileges 固定在场"""
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        assert "--cap-add" not in args
        assert args[args.index("--cap-drop") + 1] == "ALL"
        assert args[args.index("--security-opt") + 1] == "no-new-privileges:true"

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_no_host_env_passthrough(self, tmp_path, shape):
        """逃逸面 #5：无 --env-file、无宿主 env 透传，-e 仅最小白名单集合"""
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        assert "--env-file" not in args
        envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
        assert sorted(e.split("=", 1)[0] for e in envs) == \
            sorted(sandbox_params.CONTAINER_ENV_ALLOWLIST)


# ---------------- 参数构造层：网络档正确映射 ----------------

@pytest.mark.unit
class TestNetProfileMapping:
    """逃逸面 #6：网络档位映射面收敛——host 无档位可表达"""

    @pytest.mark.parametrize("profile,network", [
        ("none", "none"),
        ("egress", "aievo-sandbox-egress"),
        ("open", "bridge"),
    ])
    def test_worker_three_profiles(self, tmp_path, profile, network):
        """worker 通道三档：none 离网 / egress 白名单网 / open 默认 bridge"""
        paths = script_container.build_container_paths(
            tmp_path / "script_runs" / "t1" / "d1",
            tmp_path / "script_runs" / "t1" / "d1" / "s1",
            tmp_path / "script_runs" / "t1" / "d1" / "scripts" / "s1",
        )
        args = script_container.build_docker_run_args_for_profile(
            image="ai-evo-script-runner:latest", user="10001:10001",
            paths=paths, entry_rel="main.py", container_name="c",
            net_profile=profile)
        assert args[args.index("--network") + 1] == network

    def test_egress_network_name_single_sourced(self):
        """egress 网络名与 executor 白名单同源（两通道挂同一张网）"""
        assert script_container.WORKER_NET_PROFILE_TO_DOCKER["egress"] == \
            sandbox_params.NET_PROFILES["egress"]

    def test_host_is_never_a_network_value(self):
        """host 绝不在任一通道的档位取值面里（两映射的值域都无 host）"""
        assert "host" not in set(script_container.WORKER_NET_PROFILE_TO_DOCKER.values())
        assert "host" not in set(sandbox_params.NET_PROFILES.values())

    @pytest.mark.parametrize("profile", ["none", "egress"])
    def test_executor_profiles(self, profile):
        assert sandbox_params.network_profile_to_docker(profile) == \
            sandbox_params.NET_PROFILES[profile]

    @pytest.mark.parametrize("profile", ["host", "open", "bridge", "container"])
    def test_executor_rejects_non_whitelisted_profiles(self, profile):
        """executor 白名单只有 none|egress：host（共享宿主网络栈）/open（直出）/
        其它档位一律 SandboxParamError"""
        with pytest.raises(SandboxParamError):
            sandbox_params.network_profile_to_docker(profile)

    def test_worker_rejects_unknown_profile(self, tmp_path):
        paths = script_container.build_container_paths(
            tmp_path / "script_runs" / "t1" / "d1",
            tmp_path / "script_runs" / "t1" / "d1" / "s1",
            tmp_path / "script_runs" / "t1" / "d1" / "scripts" / "s1",
        )
        with pytest.raises(ValueError, match="未知网络档位"):
            script_container.build_docker_run_args_for_profile(
                image="ai-evo-script-runner:latest", user="10001:10001",
                paths=paths, entry_rel="main.py", container_name="c",
                net_profile="host")


# ---------------- 参数构造层：container 路径不注入 PYTHONPATH ----------------

@pytest.mark.unit
class TestNoPlatformImportSurface:
    """逃逸面 #7：脚本拿不到平台代码/凭证——import app.* 无从谈起"""

    def test_env_allowlist_is_exactly_minimal_set(self):
        """env 白名单恰为 Python 运行必需最小集（键级钉扎：多一个键都要过审）"""
        assert set(sandbox_params.CONTAINER_ENV_ALLOWLIST) == {
            "PYTHONIOENCODING", "PYTHONUNBUFFERED", "LANG", "HOME"}

    @pytest.mark.parametrize("secret", [
        "DATABASE_URL", "REDIS_URL", "AUTH_JWT_SECRET", "EXECUTOR_TOKEN",
        "LANGFUSE_SECRET_KEY", "SSO_LOGIN_URL", "ANC_HUB_BEARER_TOKEN",
    ])
    def test_no_platform_secret_in_env_allowlist(self, secret):
        assert secret not in sandbox_params.CONTAINER_ENV_ALLOWLIST
        assert secret != "PYTHONPATH"

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_no_pythonpath_anywhere_in_args(self, tmp_path, shape):
        """container 路径无 PYTHONPATH 注入（local 模式的平台 import 面在容器
        通道不存在）：-e 值、旗标、尾部命令全扫"""
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        assert not any("PYTHONPATH" in tok for tok in args)

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_tail_command_lives_in_image_not_platform_layer(self, tmp_path, shape):
        """尾部命令只引用镜像内 wrapper（/opt/runner）与挂载工作区（/workspace）
        ——无 /app 平台层路径、无宿主绝对路径"""
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        image = "ai-evo-script-runner:latest"
        tail = args[args.index(image) + 1:]
        assert tail[0] == sandbox_params.CONTAINER_PYTHON
        assert tail[1] == sandbox_params.CONTAINER_ENTRY_WRAPPER
        assert tail[1].startswith("/opt/runner/")
        assert not any(tok.startswith("/app") for tok in args)


# ---------------- 参数构造层：超时与资源上限在场 ----------------

@pytest.mark.unit
class TestTimeoutAndResourceCeilings:
    """逃逸面 #8：资源耗尽类攻击（fork 炸弹/内存耗尽/CPU 拖垮/超长运行）的
    参数面——上限必须固定在场且不可豁免"""

    @pytest.mark.parametrize("flag,value", [
        ("--memory", "1g"),          # 内存上限（含 OOM kill 而非拖垮宿主）
        ("--memory-swap", "1g"),     # swap 同额 = 禁 swap 扩展
        ("--cpus", "0.5"),           # CPU 上限
        ("--pids-limit", "128"),     # fork 炸弹天花板
    ])
    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_resource_limits_pinned(self, tmp_path, shape, flag, value):
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        assert args[args.index(flag) + 1] == value

    @pytest.mark.parametrize("shape", ["worker", "executor"])
    def test_readonly_root_and_sized_tmpfs(self, tmp_path, shape):
        """只读根文件系统 + 有界 tmpfs（nosuid,noexec）"""
        args = (_worker_shaped_args(tmp_path)[0] if shape == "worker"
                else _executor_shaped_args(tmp_path))
        assert "--read-only" in args
        tmpfs = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
        assert len(tmpfs) == 2
        for spec_ in tmpfs:
            kind, opts = spec_.split(":", 1)
            assert kind in (sandbox_params.CONTAINER_TMPFS_TMP,
                            sandbox_params.CONTAINER_TMPFS_HOME)
            assert opts == f"rw,nosuid,noexec,size={sandbox_params.CONTAINER_TMPFS_SIZE}"

    def test_resource_constants_pinned(self):
        """上限常量单一事实源钉扎（放松上限必须连本门禁一起改）"""
        assert sandbox_params.CONTAINER_MEMORY == "1g"
        assert sandbox_params.CONTAINER_CPUS == "0.5"
        assert sandbox_params.CONTAINER_PIDS_LIMIT == 128
        assert sandbox_params.CONTAINER_TMPFS_SIZE == "64m"

    def test_run_spec_requires_timeout(self):
        """执行 spec 的 timeout 是必填项——没有「不限时」的沙箱执行"""
        import dataclasses
        from app.services.script_container import SandboxRunSpec
        assert {f.name for f in dataclasses.fields(SandboxRunSpec)} >= {
            "image", "user", "workspace_host", "timeout", "entrypoint"}
        with pytest.raises(TypeError):
            SandboxRunSpec(image="ai-evo-script-runner:latest", user="10001:10001",
                           workspace_host="/x", workspace_container="/workspace",
                           workdir_container="/workspace", cwd_host="/x",
                           net_profile="none", container_name="c",
                           entrypoint=["python"])  # 缺 timeout

    def test_executor_request_timeout_present_and_bounded(self):
        """executor 请求模型：timeout 有缺省（600s）且 (0, 3600] 有界——超长
        运行要么被服务端强杀要么在反序列化层即 422"""
        from executor.app import SandboxRunRequest
        import pydantic
        req = SandboxRunRequest(image="ai-evo-script-runner:latest",
                                workspace="/app/uploads/script_runs/t/d",
                                entrypoint=["python"])
        assert req.timeout == 600.0
        for bad in (0.0, -1.0, 3600.5, 7200.0):
            with pytest.raises(pydantic.ValidationError):
                SandboxRunRequest(image="ai-evo-script-runner:latest",
                                  workspace="/app/uploads/script_runs/t/d",
                                  entrypoint=["python"], timeout=bad)


# ---------------- 故意反例：校验器抓得住（攻击者视角构造非法参数） ----------------

@pytest.mark.unit
class TestDeliberateAttackParameters:
    """故意反例集：每一条都是一次「如果校验器失效就会逃逸」的攻击参数，
    逐项断言在进 docker CLI 之前被 SandboxParamError/ValidationError 拒绝。"""

    # ---- 镜像冒名（validate_runner_image） ----

    @pytest.mark.parametrize("attack_image", [
        "ubuntu:latest",                                    # 白名单外基名（通用攻击镜像）
        "ai-evo-script-runner:latest@sha256:" + "a" * 64,   # 白名单基名 + digest（executor 档未放开）
        "registry.local/evil/ai-evo-script-runner:latest",  # 白名单基名 + registry 前缀伪装
        "-v",                                               # 值即旗标注入
        "",                                                 # 空引用
        "ai-evo-script-runner:-privileged",                 # tag 旗标样
    ])
    def test_image_attacks_rejected(self, attack_image):
        """executor 镜像白名单拒绝冒名/伪装/注入（digest 引用放开属 Phase 3-3，
        届时须同步本参数与两侧注释）"""
        with pytest.raises(SandboxParamError):
            sandbox_params.validate_runner_image(attack_image)

    def test_image_whitelist_accepts_legitimate_base(self):
        """反例的反面：合法引用原样放行（校验器不是一刀切拒绝）"""
        assert sandbox_params.validate_runner_image("ai-evo-script-runner:latest") \
            == "ai-evo-script-runner:latest"
        assert sandbox_params.validate_runner_image("ai-evo-script-runner") \
            == "ai-evo-script-runner"

    # ---- 越界 bind mount（resolve_workspace_mount） ----

    @pytest.fixture
    def ws_root(self, tmp_path, monkeypatch):
        """把 executor 视角工作区根换到临时目录（生产恒为
        /app/uploads/script_runs 常量，单测无 /app 写权限）"""
        root = tmp_path / "script_runs_root"
        (root / "t1" / "d1").mkdir(parents=True)
        monkeypatch.setattr(sandbox_params, "WORKSPACE_ROOT", str(root))
        return root

    def test_mount_outside_root_rejected(self, ws_root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(SandboxParamError):
            sandbox_params.resolve_workspace_mount(str(outside))

    def test_mount_host_root_rejected(self, ws_root, monkeypatch):
        """挂宿主根 = 全机沦陷，绝无通道（包括 workspace 字面值就是 /）"""
        with pytest.raises(SandboxParamError):
            sandbox_params.resolve_workspace_mount("/")
        with pytest.raises(SandboxParamError):
            sandbox_params.resolve_workspace_mount("-v")

    def test_mount_literal_traversal_rejected(self, ws_root):
        """字面 ../ 穿越：realpath 解析后必须严格落在根之下"""
        with pytest.raises(SandboxParamError):
            sandbox_params.resolve_workspace_mount(str(ws_root / "t1" / ".." / "escape"))

    def test_mount_symlink_escape_rejected(self, ws_root, tmp_path):
        """符号链接逃逸：根内 symlink 指向根外目录，realpath 解析后拒绝"""
        outside = tmp_path / "escape-target"
        outside.mkdir()
        link = ws_root / "t1" / "escape-link"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(SandboxParamError):
            sandbox_params.resolve_workspace_mount(str(link))

    def test_mount_docker_sock_rejected(self, ws_root):
        """挂 docker.sock（= 交出宿主 root）在白名单子树外，拒绝"""
        with pytest.raises(SandboxParamError):
            sandbox_params.resolve_workspace_mount("/var/run/docker.sock")

    def test_mount_root_itself_rejected(self, ws_root):
        """挂整个 script_runs 面过宽：根本身拒绝"""
        with pytest.raises(SandboxParamError):
            sandbox_params.resolve_workspace_mount(str(ws_root))

    def test_mount_missing_dir_rejected(self, ws_root):
        with pytest.raises(SandboxParamError):
            sandbox_params.resolve_workspace_mount(str(ws_root / "t1" / "ghost"))

    def test_legitimate_subtree_mount_accepted(self, ws_root):
        """反例的反面：根下已存在子目录原样放行（realpath 解析后返回）"""
        import os
        ws = ws_root / "t1" / "d1"
        assert sandbox_params.resolve_workspace_mount(str(ws)) == str(
            Path(os.path.realpath(ws)))

    # ---- 旗标位值注入（build_docker_run_args 纵深防御闸） ----
    # 本闸只拦「值即旗标」注入（docker CLI 会把 "-" 开头的独立 argv 解析成
    # 旗标）；语义类攻击（user=root / network host / 任意路径挂载）在各自
    # 上游防线拦——executor 请求模型无 user/mem_limit 位（extra=forbid 用例）、
    # network_name 只出自两映射白名单（TestNetProfileMapping）、mount_host
    # 过 resolve_workspace_mount（下列用例），此处不重复设卡。

    @pytest.mark.parametrize("field,attack_value", [
        ("image", "--privileged"),
        ("image", "-v"),
        ("user", "--privileged"),
        ("network_name", "--pid"),
        ("network_name", "-d"),
        ("container_name", "--privileged"),
        ("mount_host", "--privileged"),
        ("mount_container", "-v"),
        ("workdir_container", "-w"),
    ])
    def test_flag_like_values_rejected_in_builder(self, tmp_path, field, attack_value):
        """构造函数自身的纵深防御：任何取值位塞旗标样值（哪怕绕过上游校验）
        一律 SandboxParamError——不进 docker CLI"""
        mount_host = tmp_path / "script_runs" / "t1" / "d1"
        mount_host.mkdir(parents=True)
        kwargs = dict(
            image="ai-evo-script-runner:latest",
            user="10001:10001",
            mount_host=str(mount_host),
            mount_container="/workspace",
            workdir_container="/workspace",
            network_name="none",
            container_name="aievo-sbx-abc",
            tail_command=["python", "wrapper.py"],
        )
        kwargs[field] = attack_value
        with pytest.raises(SandboxParamError):
            sandbox_params.build_docker_run_args(**kwargs)

    def test_empty_tail_command_rejected(self, tmp_path):
        mount_host = tmp_path / "script_runs" / "t1" / "d1"
        mount_host.mkdir(parents=True)
        with pytest.raises(SandboxParamError):
            sandbox_params.build_docker_run_args(
                image="ai-evo-script-runner:latest", user="10001:10001",
                mount_host=str(mount_host), mount_container="/workspace",
                workdir_container="/workspace", network_name="none",
                container_name="aievo-sbx-abc", tail_command=[])

    # ---- entrypoint 载荷注入（executor._validate_entrypoint） ----

    @pytest.mark.parametrize("attack_entrypoint", [
        ["--privileged"],               # 首元素旗标样（docker CLI 只把镜像前
                                        # argv 当旗标解析，首位是唯一注入位）
        ["python", "wra\x00pper.py"],   # NUL 注入（任意位）
        [""],                           # 空元素（任意位）
    ])
    def test_entrypoint_attacks_rejected(self, attack_entrypoint):
        from executor.app import _validate_entrypoint
        with pytest.raises((SandboxParamError, ValueError)):
            _validate_entrypoint(attack_entrypoint)

    # ---- executor 请求模型：额外 docker 能力没有字段可表达（extra=forbid） ----

    @pytest.mark.parametrize("extra_field,attack_value", [
        ("privileged", True),                       # 特权容器
        ("pid_host", True),                         # 共享宿主 PID 命名空间
        ("network_host", True),                     # 共享宿主网络栈
        ("volumes", ["/:/host"]),                   # 任意路径挂载
        ("cap_add", ["SYS_ADMIN", "NET_ADMIN"]),    # 额外 capabilities
        ("devices", ["/dev/sda:/dev/sda"]),         # 宿主设备直通
        ("docker_args", ["--privileged"]),          # 原始旗标直通
        ("security_opt", ["apparmor=unconfined"]),  # 关安全模块
        ("user", "root"),                           # 请求模型无 user 位（固定值）
        ("mem_limit", "16g"),                       # 请求模型无资源位（固定值）
    ])
    def test_executor_request_model_forbids_extra_capabilities(
            self, extra_field, attack_value):
        """最外层防线：请求模型字段集即白名单——攻击字段在反序列化层即 422，
        executor 不是通用 docker 代理（user/mem_limit 固定位同样不可请求覆盖）"""
        import pydantic
        from executor.app import SandboxRunRequest
        payload = {
            "image": "ai-evo-script-runner:latest",
            "workspace": "/app/uploads/script_runs/t1/d1",
            "net_profile": "none",
            "timeout": 60.0,
            "entrypoint": ["python", "/opt/runner/pre_judge_entry.py"],
            extra_field: attack_value,
        }
        with pytest.raises(pydantic.ValidationError):
            SandboxRunRequest(**payload)


# ---------------- 实机逃逸集（sandbox_e2e 档，默认 deselect） ----------------

# 探针脚本源码（容器内经生产 wrapper 执行：def run(stdin) 函数式入口，
# 探测结果全部回填 stdout 末行 JSON——「逃逸成功」也会如实上报，由测试断言拦下）
_PROBE_IMPORT_APP = '''
def run(stdin):
    import importlib, importlib.util, sys
    out = {}
    # 逃逸尝试 1：直接 import 平台代码（local 模式 PYTHONPATH 注入面的核心逃逸）
    try:
        importlib.import_module("app")
        out["import_app"] = "ESCAPED"
    except Exception as e:
        out["import_app"] = "blocked:" + type(e).__name__
    # 逃逸尝试 2：自插常见宿主平台路径后再试
    for p in ("/app", "/opt/app", "/srv/app", "/workspace/../app"):
        sys.path.insert(0, p)
    try:
        importlib.import_module("app.core.config")
        out["import_app_after_path_insert"] = "ESCAPED"
    except Exception as e:
        out["import_app_after_path_insert"] = "blocked:" + type(e).__name__
    # 逃逸尝试 3：全路径搜索——平台代码根本不在镜像层
    out["app_spec_findable"] = importlib.util.find_spec("app") is not None
    return {"status": "ok", "output": out, "summary": "probe"}
'''

_PROBE_PID1_ENVIRON = '''
def run(stdin):
    import os
    out = {}
    secrets = ("DATABASE_URL", "REDIS_URL", "AUTH_JWT_SECRET", "EXECUTOR_TOKEN",
               "LANGFUSE_SECRET_KEY", "SSO_LOGIN_URL", "ANC_HUB_BEARER_TOKEN",
               "DB_ASSERT_READONLY_DSN")
    leaked = []
    # 逃逸尝试 1：读 1 号进程环境（宿主世界里 PID 1 持有平台全部凭证；
    # 沙箱内即使可读，内容也只能是白名单 env）
    try:
        with open("/proc/1/environ", "rb") as f:
            pid1 = f.read().decode("utf-8", "replace")
        out["pid1_readable"] = True
        out["pid1_keys"] = sorted(k.split("=", 1)[0]
                                  for k in pid1.split("\\0") if "=" in k)
        leaked += [s for s in secrets if (s + "=") in pid1]
    except OSError as e:
        out["pid1_readable"] = False
        out["pid1_error"] = type(e).__name__
    # 逃逸尝试 2：自身 environ（docker run 只显式注入白名单 -e）
    leaked += [s for s in secrets if s in os.environ]
    # 逃逸尝试 3：宿主常见凭证文件路径
    for path in ("/run/secrets", "/app/.env", "/opt/runner/.env"):
        try:
            os.listdir(path)
            out.setdefault("secret_paths_found", []).append(path)
        except OSError:
            pass
    out["leaked_secrets"] = sorted(set(leaked))
    return {"status": "ok", "output": out, "summary": "probe"}
'''

_PROBE_FORK_BOMB = '''
def run(stdin):
    import subprocess
    children = []
    # 逃逸尝试：fork 炸弹——不停复制直到被 pids-limit 拦下
    # （exec sleep 每子进程 ~1MB RSS，300 上限 < 1g 内存限额，不触 OOM 干扰判定）
    try:
        for _ in range(300):
            children.append(subprocess.Popen(
                ["/bin/sh", "-c", "exec sleep 30"]))
    except OSError:
        pass  # 资源上限拦截（预期路径）
    for c in children:
        try:
            c.terminate()
        except Exception:
            pass
    for c in children:
        try:
            c.wait(timeout=15)
        except Exception:
            try:
                c.kill()
            except Exception:
                pass
    spawned = len(children)
    return {"status": "ok",
            "output": {"spawned": spawned, "capped": spawned < 300},
            "summary": "probe"}
'''

_PROBE_WRITE_ESCAPE = '''
def run(stdin):
    import os
    out = {}
    # 逃逸尝试：写越界路径（只读根文件系统应全拒）
    for t in ("/etc/escape-probe", "/escape-probe", "/opt/runner/evil.py",
              "/var/tmp/evil", "/usr/local/lib/evil.py", "/home/evil"):
        try:
            with open(t, "a") as f:
                f.write("escape-probe")
            out[t] = "WRITTEN"
        except OSError as e:
            out[t] = "blocked:" + type(e).__name__
    # 逃逸尝试：摸 docker.sock（= 交出宿主 root 的通道）
    try:
        os.stat("/var/run/docker.sock")
        out["docker_sock"] = "FOUND"
    except OSError as e:
        out["docker_sock"] = "blocked:" + type(e).__name__
    # 合法写面（隔离不误伤）：挂载工作区 /tmp、home 三处 tmpfs
    for t in ("/workspace/.probe", "/tmp/.probe", "/home/runner/.probe"):
        try:
            with open(t, "w") as f:
                f.write("ok")
            out[t] = "writable"
        except OSError as e:
            out[t] = "NOT-WRITABLE:" + type(e).__name__
    return {"status": "ok", "output": out, "summary": "probe"}
'''

_PROBE_NETWORK_EGRESS = '''
def run(stdin):
    import socket
    out = {}
    # 逃逸尝试 1：DNS 解析出网
    try:
        socket.getaddrinfo("example.com", 80)
        out["dns"] = "ESCAPED"
    except OSError as e:
        out["dns"] = "blocked:" + type(e).__name__
    # 逃逸尝试 2：直连公网保留地址（绕过 DNS 的出网判定）
    try:
        socket.create_connection(("192.0.2.1", 80), timeout=3)
        out["connect_public"] = "ESCAPED"
    except OSError as e:
        out["connect_public"] = "blocked:" + type(e).__name__
    # 逃逸尝试 3：直连常见内网网段（SSRF 面）
    try:
        socket.create_connection(("10.255.255.1", 80), timeout=3)
        out["connect_private"] = "ESCAPED"
    except OSError as e:
        out["connect_private"] = "blocked:" + type(e).__name__
    return {"status": "ok", "output": out, "summary": "probe"}
'''

_PROBE_LONG_RUN = '''
import time
def run(stdin):
    time.sleep(120)  # 超长运行：必须被超时收口强杀
    return {"status": "ok", "output": {"slept": "full"}, "summary": "probe"}
'''


def _sandbox_available() -> bool:
    """docker CLI 在场且 daemon 可应答（不可用 → e2e 探针明确 skip）"""
    docker = shutil.which(sandbox_params.docker_binary())
    if not docker:
        return False
    try:
        return subprocess.run([docker, "info"], capture_output=True,
                              timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="module")
def sandbox_e2e_env():
    """实机档前置：docker 可用 + runner 镜像已构建，否则 skip 不假绿"""
    if not _sandbox_available():
        pytest.skip("docker CLI/daemon 不可用：sandbox_e2e 实机探针跳过")
    image = script_container.runner_image()
    probe = subprocess.run(
        [sandbox_params.docker_binary(), "image", "inspect", image],
        capture_output=True, timeout=30)
    if probe.returncode != 0:
        pytest.skip(f"runner 镜像 {image} 未构建（见 docker/script-runner/README.md）")
    return {"image": image}


def _run_probe(monkeypatch, tmp_path, source: str, timeout: float = 120.0):
    """经生产通道真跑一个探针脚本（docker-CLI 驱动 + 真镜像 + 真工作区）"""
    monkeypatch.delenv("EXECUTOR_URL", raising=False)  # 本机形态：CLI 驱动
    run_dir = tmp_path / "script_runs" / "escape-e2e" / "probe"
    staged = run_dir / "scripts" / "probe"
    staged.mkdir(parents=True)
    (staged / "main.py").write_text(source, encoding="utf-8")
    cwd = run_dir / "probe"
    cwd.mkdir()
    t0 = time.monotonic()
    result = script_container.execute_script_in_container(
        script={"script_id": "escape-e2e-probe", "name": "escape-e2e-probe"},
        staged_dir_host=staged, entry_rel="main.py",
        run_dir_host=run_dir, cwd_host=cwd,
        stdin={"task_id": "escape-e2e", "data_id": "probe",
               "_run_dir": str(run_dir), "fields": {}},
        timeout=timeout, started=time.time(),
    )
    return result, time.monotonic() - t0


# e2e 实机档双标记：e2e = W8 三分类档位（真外部依赖：docker daemon + 预载
# 镜像，CI 永不跑——三分类闭合门禁 tests/unit/test_w8_marker_gate.py 要求
# 每个用例必须落 unit/integration/e2e 之一）；sandbox_e2e = 显式执行开关
# （pytest.ini addopts 默认 deselect，`-m sandbox_e2e` 才跑）
@pytest.mark.e2e
@pytest.mark.sandbox_e2e
class TestSandboxEscapeE2E:
    """实机逃逸集（设计表 1-4 验收行）：每条探针都「试图逃逸」，断言被拦"""

    def test_import_app_blocked(self, sandbox_e2e_env, monkeypatch, tmp_path):
        """import app / 自插 sys.path / find_spec 全路径——平台代码不在沙箱内"""
        result, _ = _run_probe(monkeypatch, tmp_path, _PROBE_IMPORT_APP)
        assert result["status"] == "ok", result.get("error")
        out = result["output"]
        assert out["import_app"].startswith("blocked"), out
        assert out["import_app_after_path_insert"].startswith("blocked"), out
        assert out["app_spec_findable"] is False

    def test_pid1_environ_and_secret_env_blocked(self, sandbox_e2e_env,
                                                 monkeypatch, tmp_path):
        """/proc/1/environ 与自身 environ 均无平台凭证（env 白名单面）

        PID 1 的 environ 含镜像 ENV 烘焙项（PATH/PYTHON_VERSION/HOSTNAME 等
        python:3.12-slim 基础层）——不是凭证，不设卡；门禁是「平台 secret
        一个都不出现」。"""
        result, _ = _run_probe(monkeypatch, tmp_path, _PROBE_PID1_ENVIRON)
        assert result["status"] == "ok", result.get("error")
        out = result["output"]
        secrets = {"DATABASE_URL", "REDIS_URL", "AUTH_JWT_SECRET", "EXECUTOR_TOKEN",
                   "LANGFUSE_SECRET_KEY", "SSO_LOGIN_URL", "ANC_HUB_BEARER_TOKEN",
                   "DB_ASSERT_READONLY_DSN"}
        assert out["leaked_secrets"] == [], out
        assert not out.get("secret_paths_found"), out
        if out.get("pid1_readable"):
            # 沙箱内 PID 1 是 wrapper 自身——可读，但 environ 与平台 secret 零交集
            assert set(out["pid1_keys"]) & secrets == set(), out

    def test_fork_bomb_capped_by_pids_limit(self, sandbox_e2e_env,
                                            monkeypatch, tmp_path):
        """fork 炸弹：进程复制在 pids-limit（128）处被拦，容器正常收口"""
        result, elapsed = _run_probe(monkeypatch, tmp_path, _PROBE_FORK_BOMB,
                                     timeout=120.0)
        assert result["status"] == "ok", result.get("error")
        out = result["output"]
        assert out["capped"] is True, out
        assert out["spawned"] < 300, out
        assert elapsed < 120, "fork 炸弹探针应在超时前被资源上限收口"

    def test_write_outside_workspace_blocked(self, sandbox_e2e_env,
                                             monkeypatch, tmp_path):
        """写越界路径全拒 + docker.sock 不在场；合法写面（工作区 + /tmp）可用。

        已知限制（2026-10-15 实测，本批越界未修）：/home/runner 的 tmpfs 挂载
        实际 root:root 0700（docker tmpfs 不继承镜像目录属主），非 root 的
        HOME 不可写——对逃逸是更严（绝无写通道），但 W15 设计意图「HOME 可写
        面」未兑现；修复（--tmpfs 加 uid=10001,gid=10001,mode=0700）属
        executor/sandbox_params.py 单一事实源，留给后续条目，本探针只记录。"""
        result, _ = _run_probe(monkeypatch, tmp_path, _PROBE_WRITE_ESCAPE)
        assert result["status"] == "ok", result.get("error")
        out = result["output"]
        for target in ("/etc/escape-probe", "/escape-probe", "/opt/runner/evil.py",
                       "/var/tmp/evil", "/usr/local/lib/evil.py", "/home/evil"):
            assert out[target].startswith("blocked"), (target, out)
        assert out["docker_sock"].startswith("blocked"), out
        # 合法写面（隔离不误伤）：挂载工作区与 /tmp tmpfs（1777）
        for writable in ("/workspace/.probe", "/tmp/.probe"):
            assert out[writable] == "writable", (writable, out)
        # HOME tmpfs：当前实现不可写（见 docstring 已知限制）——绝不允许
        # 出现「WRITTEN」之外的第三态（可写 = 设计兑现，blocked = 现状更严）
        assert out["/home/runner/.probe"] in ("writable", "NOT-WRITABLE:PermissionError"), out

    def test_default_network_none_blocks_egress(self, sandbox_e2e_env,
                                                monkeypatch, tmp_path):
        """默认档 --network none：DNS/公网/内网三类出网探测全部失败"""
        result, _ = _run_probe(monkeypatch, tmp_path, _PROBE_NETWORK_EGRESS)
        assert result["status"] == "ok", result.get("error")
        out = result["output"]
        for key in ("dns", "connect_public", "connect_private"):
            assert out[key].startswith("blocked"), (key, out)

    def test_long_run_killed_by_timeout(self, sandbox_e2e_env, monkeypatch, tmp_path):
        """超长运行（sleep 120s > timeout 4s）：被超时收口强杀，不拖垮任务"""
        result, elapsed = _run_probe(monkeypatch, tmp_path, _PROBE_LONG_RUN,
                                     timeout=4.0)
        assert result["status"] == "fail"
        assert result["finish_reason"] == "timeout"
        assert "超时" in result["error"]
        assert elapsed < 60, "超时收口应分钟内完成（SIGTERM→宽限→rm -f 兜底）"
