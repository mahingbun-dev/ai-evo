"""脚本执行容器化隔离（W15）：docker run --rm 执行通道

整改计划 §4 W15：执行通道改 ``docker run --rm``——非 root 用户、
``--memory 1g --cpus 0.5``、``--read-only`` + tmpfs、剥离平台 env、
``--network none``（任务级开关，默认关）、挂载仅本次 script_runs 工作目录。
灰度开关 ``SCRIPT_EXEC_MODE=local|container``（默认 local，dev 先行）。

设计要点：
- 隔离边界：容器根文件系统只读（镜像层），唯一可写面是 bind mount 的
  本次工作区（uploads/script_runs/{task_id}/{data_id}/ → /workspace）与
  两块 tmpfs（/tmp、容器用户 home）；平台敏感 env（DATABASE_URL/REDIS_URL/
  SSO/LANGFUSE 等）不进容器（docker run 不透传宿主 env，只显式注入运行
  必需的最小集合）；PYTHONPATH 不注入，脚本无法 import 平台代码。
- 路径映射：宿主工作区 run_dir 挂载到容器固定路径 /workspace，stdin
  payload 中所有以 run_dir 为前缀的宿主绝对路径重写为容器路径（脚本拿
  到的 fields/inputs/_run_dir 路径在容器内有效）。
- 网络：默认 ``--network none``（完全离网）；编排 step 声明
  ``network: true`` 时按 ARCH-1-2 三档归一为 open 档（docker-CLI 通道 →
  默认 bridge 直出，「脚本要访问被测系统」需求的任务级开关；受限内网网段
  的精细化控制属后续迭代）。
- 依赖校验：container 模式下依赖声明对照 ``RUNNER_IMAGE_PACKAGES``
  静态清单（runner 镜像内已装集合），缺失即阻断——镜像不可变，无
  运行时 pip install 路径。清单与 docker/script-runner/packages.txt
  由 tests/unit/test_w15_container_isolation.py 一致性用例做同步门禁。
- 超时：先 SIGTERM docker CLI（前台运行时会把信号转发给容器并随容器
  退出），宽限期后 SIGKILL 并 ``docker rm -f`` 兜底清理（CLI 被杀时
  --rm 可能失效，必须按容器名回收）。
- 镜像：ai-evo-script-runner（Dockerfile 见 docker/script-runner/，
  基于平台镜像裁剪：同基 python:3.12-slim，只装脚本常用依赖集合，不
  含平台代码与平台重依赖）。镜像离线预载到部署机是 D-3 运维事项。

ARCH-1-1（设计 plans/2026-10-15-企业级评测系统架构设计.md §2.3 D1）：docker run
参数构造（隔离常量 + 纯构造函数 + docker_binary）已抽到公共模块
executor/sandbox_params.py——worker docker-CLI 通道（本模块）与 executor 沙箱
服务（executor/app.py）共用同一份「单一事实源」，并随抽取获得 --cap-drop ALL
与 no-new-privileges 加固位（预对齐 Phase 1-5 门禁）。两侧约束演进必须改公共
模块并同步两侧注释，严禁任一侧私增 docker 参数（白名单语义）。

ARCH-1-2（设计 §2.3 D1 / §3 表 1-2 行 / 「远期 K8s」预留扩展点）：执行通道抽
``SandboxDriver`` 接口（run/wait/kill/status），两个实现——
- ``DockerCliDriver``（本模块）：W15 既有 docker run --rm 前台通道整体迁移；
- ``HttpExecutorDriver``（app/services/executor_client.py）：POST/GET 走
  EXECUTOR_URL+EXECUTOR_TOKEN 的 executor 沙箱服务（目标通道）。
``get_sandbox_driver()`` 按 EXECUTOR_URL 选择（未配置回退 docker-CLI 单机通道）；
``execute_script_in_container`` 收敛为驱动无关编排（路径映射/stdin 重写/结果
解析共用，差异全部下沉到驱动）。接入 K8s Job 驱动时 worker/api 零改动（§3
远期扩展点 1）。网络三档（设计 D2）：``SCRIPT_NET_PROFILE``（none|egress|open，
默认 none）+ step.network:true → open 档，映射见 WORKER_NET_PROFILE_TO_DOCKER。

执行返回结构与 local 模式完全一致（pre_script_results 标准条目）。
"""
import logging
import os
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from prometheus_client import Histogram

from app.core.config import get_settings

# ARCH-1-1：单一事实源 re-export（保持既有调用面不变；noqa 见下——本模块
# 对外仍暴露这些名字，pre_judge_script_service 与单测直接引用）
from executor.sandbox_params import (  # noqa: F401
    CONTAINER_CPUS,
    CONTAINER_ENTRY_WRAPPER,
    CONTAINER_ENV_ALLOWLIST,
    CONTAINER_MEMORY,
    CONTAINER_PIDS_LIMIT,
    CONTAINER_PYTHON,
    CONTAINER_TMPFS_HOME,
    CONTAINER_TMPFS_SIZE,
    CONTAINER_TMPFS_TMP,
    CONTAINER_WORKSPACE,
    NET_PROFILES as _EXECUTOR_NET_PROFILES,
    build_docker_run_args as _build_sandbox_docker_run_args,
    docker_binary,
)

logger = logging.getLogger(__name__)

# ARCH-2-3：沙箱启动（受理）延迟直方图（设计 §2.3 D7「沙箱启动延迟」/
# §3 Phase 2 表 2-3 行）。口径 = execute_script_in_container 内 driver.run()
# 从发起到受理完成：executor HTTP 通道即 POST /sandbox/run 往返（含 docker
# run detach 冷启动，设计 §2.4 的 0.3-0.8s 量级）；docker-CLI 前台通道为
# 子进程 spawn 耗时（受理与执行一体，无独立启动阶段，亚毫秒档）。driver
# 标签区分两条通道；受理失败（SandboxDriverError）不观察——失败归任务
# 失败指标与日志，不进延迟分布。
SANDBOX_START_LATENCY = Histogram(
    "aievo_sandbox_start_latency_seconds",
    "沙箱启动（受理）延迟：driver.run() 发起→受理完成（秒）",
    ["driver"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

# 超时后 SIGTERM docker CLI 的宽限期（秒）：给容器优雅退出与 --rm 收尾的时间
CONTAINER_TERM_GRACE_SECONDS = 5.0

# 依赖声明校验（W15）：container 模式改为「runner 镜像内已装集合」静态清单。
# 单一事实源是本常量；docker/script-runner/packages.txt 必须逐行一致，
# 由 tests/unit/test_w15_container_isolation.py::TestRunnerImagePackages 断言。
# 版本与平台 requirements.txt/requirements.lock 钉死版本同源（numpy 锁 2.2.6
# 的原因见主 Dockerfile：部署 VM 缺 X86_V2 指令集）。
RUNNER_IMAGE_PACKAGES: List[str] = [
    "requests==2.32.5",
    "httpx==0.28.1",
    "pandas==3.0.0",
    "numpy==2.2.6",
    "openpyxl==3.1.5",
    "xlrd==2.0.2",
    "Pillow==12.1.1",
    "PyYAML==6.0.3",
    "jsonpath-ng==1.7.0",
]


# ---------------- 灰度开关读取 ----------------

_VALID_MODES = ("local", "container")


def get_script_exec_mode() -> str:
    """脚本执行模式（灰度开关）：环境变量 SCRIPT_EXEC_MODE 优先于 Settings

    环境变量实时读取（绕过 get_settings 的 lru_cache）：dev 灰度切换无需
    重启进程，测试 monkeypatch.setenv 可直接生效。非法值告警并回退 local
    （fail-safe：不认识的值绝不进容器通道）。
    """
    raw = (os.environ.get("SCRIPT_EXEC_MODE") or "").strip().lower()
    if raw:
        if raw in _VALID_MODES:
            return raw
        logger.warning(f"SCRIPT_EXEC_MODE 非法值 {raw!r}，回退 local（可选: local/container）")
        return "local"
    mode = (get_settings().SCRIPT_EXEC_MODE or "local").strip().lower()
    return mode if mode in _VALID_MODES else "local"


def container_mode_enabled() -> bool:
    """container 隔离模式是否启用"""
    return get_script_exec_mode() == "container"


def runner_image() -> str:
    """runner 镜像引用（SCRIPT_RUNNER_IMAGE，缺省 ai-evo-script-runner:latest）

    环境变量实时读（绕过 get_settings 的 lru_cache）：镜像 tag 切换/测试
    monkeypatch 与模式开关同语义。
    """
    raw = (os.environ.get("SCRIPT_RUNNER_IMAGE") or "").strip()
    if raw:
        return raw
    return (get_settings().SCRIPT_RUNNER_IMAGE or "ai-evo-script-runner:latest").strip()


def runner_user() -> str:
    """容器内运行用户 uid:gid（非 root；环境变量实时读，优先级同 runner_image）"""
    raw = (os.environ.get("SCRIPT_RUNNER_USER") or "").strip()
    if raw:
        return raw
    return (get_settings().SCRIPT_RUNNER_USER or "10001:10001").strip()


# ---------------- 网络三档（ARCH-1-2，设计 D2） ----------------

_VALID_NET_PROFILES = ("none", "egress", "open")


def get_script_net_profile() -> str:
    """平台级网络档位 SCRIPT_NET_PROFILE（none|egress|open，默认 none）

    环境变量实时读取（与 get_script_exec_mode 同语义）；非法值告警并回退
    none（fail-safe：不认识的档位绝不给出网）。
    """
    raw = (os.environ.get("SCRIPT_NET_PROFILE") or "").strip().lower()
    if raw:
        if raw in _VALID_NET_PROFILES:
            return raw
        logger.warning(f"SCRIPT_NET_PROFILE 非法值 {raw!r}，回退 none（可选: {'/'.join(_VALID_NET_PROFILES)}）")
        return "none"
    profile = (get_settings().SCRIPT_NET_PROFILE or "none").strip().lower()
    return profile if profile in _VALID_NET_PROFILES else "none"


def resolve_net_profile(network_flag: bool = False) -> str:
    """编排步骤网络开关 + 平台档位 → 三档档位（none|egress|open）

    - step.network=true（W15「脚本要访问被测系统」语义）→ open 档（docker-CLI
      通道映射默认 bridge 直出；executor 通道白名单未放开 open 档，请求即拒——
      错误透传给脚本结果，见 HttpExecutorDriver）；
    - 未声明 → 取 SCRIPT_NET_PROFILE（默认 none）。
    """
    return "open" if network_flag else get_script_net_profile()


# worker docker-CLI 通道的「三档档位 → docker --network 取值」映射（ARCH-1-2）。
# egress 网络名与 executor 白名单 sandbox_params.NET_PROFILES 同源（单一事实源：
# 1-3 compose 建同名网络）；open 档 = 默认 bridge 直出（设计 D2：超管审批流程
# 属后续条目）。executor 通道的档位白名单在 sandbox_params.NET_PROFILES（open
# 无映射，请求即拒），与本表是两个不同通道的取值面。
WORKER_NET_PROFILE_TO_DOCKER: Dict[str, str] = {
    "none": "none",
    "egress": _EXECUTOR_NET_PROFILES["egress"],
    "open": "bridge",
}


# ---------------- 路径映射与 stdin 重写 ----------------

@dataclass
class ContainerPaths:
    """本次执行的宿主/容器路径映射（挂载仅本次 script_runs 工作目录）"""
    run_dir_host: str       # 宿主工作区绝对路径（bind mount 源）
    run_dir_container: str  # 容器内工作区（恒为 /workspace）
    cwd_host: str           # 宿主 Popen cwd（stdout/stderr 日志落盘点）
    cwd_container: str      # 容器内工作目录（工作区下 script_id 子目录）
    staged_dir_container: str  # 容器内脚本包副本目录


def build_container_paths(run_dir_host: Path, cwd_host: Path, staged_dir_host: Path) -> ContainerPaths:
    """按 local 模式的目录结构推导容器内路径

    挂载源取 run_dir 与 cwd 的最小公共祖先（os.path.commonpath）：
    - 真实任务链路 cwd = run_dir/{script_id}/，公共祖先即 run_dir 本身，
      挂载面精确到本次 script_runs 工作目录（W15 约束）；
    - debug 试跑端点的 cwd（uploads/script_runs/debug/debug/{sid}/）不在其
      run_dir（uploads/script_runs/debug/{name}_{ts}/）之下，公共祖先兜底为
      uploads/script_runs/debug——仍限定在 script_runs 内、本次调用归属明确
      的目录树，绝无脚本库/uploads 全量挂载。
    容器内路径 = /workspace + 相对挂载源的后缀；stdin 重写前缀与挂载源一致
    （execute_script_in_container 统一按 paths.run_dir_host 重写）。
    """
    import os.path as _osp
    run_dir_abs = Path(run_dir_host).absolute()
    cwd_abs = Path(cwd_host).absolute()
    staged_abs = Path(staged_dir_host).absolute()
    mount_src = Path(_osp.commonpath([str(run_dir_abs), str(cwd_abs)]))
    cwd_rel = cwd_abs.relative_to(mount_src).as_posix()
    staged_rel = staged_abs.relative_to(mount_src).as_posix()
    return ContainerPaths(
        run_dir_host=str(mount_src),
        run_dir_container=CONTAINER_WORKSPACE,
        cwd_host=str(cwd_abs),
        cwd_container=f"{CONTAINER_WORKSPACE}/{cwd_rel}" if cwd_rel != "." else CONTAINER_WORKSPACE,
        staged_dir_container=f"{CONTAINER_WORKSPACE}/{staged_rel}" if staged_rel != "." else CONTAINER_WORKSPACE,
    )


def rewrite_stdin_paths(value: Any, run_dir_host: str, container_root: str = CONTAINER_WORKSPACE) -> Any:
    """把 stdin payload 中所有宿主工作区绝对路径重写为容器内路径（递归）

    local 模式下 fields/inputs/_run_dir 是宿主绝对路径；container 模式下
    工作区挂载在 /workspace，脚本拿到的路径必须指向容器内。所有以
    run_dir_host 为前缀的字符串统一替换前缀（fields/inputs 的文件值、
    hub_file 的 root、_run_dir 一并覆盖）；其他字符串原样保留。
    """
    if not run_dir_host:
        return value
    prefix = run_dir_host.rstrip("/")
    boundary = prefix + "/"  # 仅按路径分量边界匹配，避免 debug 与 debug-other 之类前缀巧合
    if isinstance(value, str):
        if value == prefix:
            return container_root
        if value.startswith(boundary):
            return container_root + value[len(prefix):]
        return value
    if isinstance(value, dict):
        return {k: rewrite_stdin_paths(v, run_dir_host, container_root) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_stdin_paths(v, run_dir_host, container_root) for v in value]
    return value


# ---------------- docker run 命令构造（纯函数，可单测） ----------------

def build_docker_run_args_for_profile(
    image: str,
    user: str,
    paths: ContainerPaths,
    entry_rel: str,
    container_name: str,
    net_profile: str,
) -> List[str]:
    """构造 docker run 参数列表（不含执行，纯构造）——三档网络档位版（ARCH-1-2）

    隔离参数的单一事实源在 executor/sandbox_params.py（与 executor 沙箱服务
    共用，worker 通道随抽取获得 --cap-drop ALL / no-new-privileges 加固位）；
    完整隔离清单与旗标顺序见公共模块 build_docker_run_args 的 docstring。
    本函数只做 worker 通道特有映射：
    - 挂载源/容器路径取自 ContainerPaths（挂载仅本次 script_runs 工作目录）；
    - 尾部命令与 local 同构：[python, pre_judge_entry.py, staged_dir, entry_rel]，
      wrapper 在镜像内 /opt/runner/pre_judge_entry.py（构建时 COPY）；
    - net_profile 三档映射 WORKER_NET_PROFILE_TO_DOCKER（none|egress|open；
      executor 侧档位语义见 sandbox_params.network_profile_to_docker）；
    - 前台运行 ``-i``：stdin 载荷经 docker CLI 传入容器。
    """
    if net_profile not in WORKER_NET_PROFILE_TO_DOCKER:
        raise ValueError(f"未知网络档位 {net_profile!r}（可选: {'/'.join(WORKER_NET_PROFILE_TO_DOCKER)}）")
    return _build_sandbox_docker_run_args(
        image=image,
        user=user,
        mount_host=paths.run_dir_host,
        mount_container=paths.run_dir_container,
        workdir_container=paths.cwd_container,
        network_name=WORKER_NET_PROFILE_TO_DOCKER[net_profile],
        container_name=container_name,
        tail_command=[CONTAINER_PYTHON, CONTAINER_ENTRY_WRAPPER,
                      paths.staged_dir_container, entry_rel],
        interactive=True,
    )


def build_docker_run_args(
    image: str,
    user: str,
    paths: ContainerPaths,
    entry_rel: str,
    container_name: str,
    network: bool = False,
) -> List[str]:
    """构造 docker run 参数列表——W15 既有布尔开关面（ARCH-1-2 起为兼容薄封装）

    冒烟链路（pre_judge_script_service._run_smoke_test_in_container）与既有
    单测仍按布尔开关调用：network=True → open 档（默认 bridge 直出），
    False → none 档。生产执行通道（execute_script_in_container → 驱动）已改
    三档档位版 build_docker_run_args_for_profile。
    """
    return build_docker_run_args_for_profile(
        image=image, user=user, paths=paths, entry_rel=entry_rel,
        container_name=container_name, net_profile=("open" if network else "none"),
    )


def _new_container_name(script_key: str) -> str:
    """唯一容器名（并发执行不冲突；超时兜底清理按名回收）"""
    tag = uuid.uuid4().hex[:12]
    safe = "".join(c if c.isalnum() else "-" for c in script_key)[:40].strip("-") or "script"
    return f"aievo-script-{safe[:40]}-{tag}"


# ---------------- SandboxDriver 抽象（ARCH-1-2，设计 §2.3 D1 / 远期 K8s 预留） ----------------

class SandboxDriverError(RuntimeError):
    """沙箱驱动层错误（受理失败/白名单拒绝/连接失败等）

    execute_script_in_container 统一捕获并转脚本 fail 条目（finish_reason=
    error），错误消息面向运维定位（含驱动名与原因）。
    """


@dataclass
class SandboxRunSpec:
    """一次沙箱执行的驱动无关参数（ARCH-1-2）

    由 execute_script_in_container 统一组装（路径映射与 stdin 重写共用），
    驱动只负责「按 spec 起沙箱、收状态」——差异（前台 -i vs 后台受理+轮询、
    stdin 管道 vs 工作区落盘）全部下沉到驱动实现。
    """
    image: str                     # runner 镜像引用
    user: str                      # 容器内运行用户 uid:gid（非 root）
    workspace_host: str            # 挂载源（宿主工作区，executor 白名单子树）
    workspace_container: str       # 容器内挂载点（恒 /workspace）
    workdir_container: str         # 容器内工作目录
    cwd_host: str                  # 宿主 cwd（stdout/stderr 日志归位目标）
    net_profile: str               # 三档档位（resolve_net_profile 归一）
    container_name: str            # 唯一容器名（超时兜底回收依赖）
    entrypoint: List[str]          # exec 形式容器 argv（[python, wrapper, staged, entry_rel]）
    stdin_payload: bytes           # stdin 载荷（CLI：管道传入；executor：驱动落盘工作区）
    timeout: float                 # 执行超时秒数


@dataclass
class SandboxHandle:
    """run 受理后的执行句柄（wait/kill/status 的寻址凭据）"""
    run_id: str                    # 驱动内标识（CLI=容器名；executor=run_id）
    container_name: str
    extra: Dict[str, Any] = field(default_factory=dict)  # 驱动私有状态（如 Popen）


@dataclass
class SandboxRunStatus:
    """沙箱执行状态（wait 阻塞收口与 status 非阻塞查询共用）"""
    state: str                     # running|done|timeout|failed
    exit_code: Optional[int] = None
    error: str = ""


class SandboxDriver(ABC):
    """沙箱执行驱动接口（ARCH-1-2）

    设计 §3「远期 K8s 驱动」预留的扩展点：现有 docker-CLI / HTTP-executor 两个
    实现之外，接入 K8s 时新增 Job 驱动即可（沙箱 = Pod，egress 档位由
    NetworkPolicy 表达），worker/api 零改动。语义契约：
    - ``run``     受理执行（不阻塞到完成）；失败抛 SandboxDriverError；
    - ``wait``    阻塞等待终态（done/timeout/failed）；超时收口在驱动内部完成
                  （CLI：SIGTERM→SIGKILL+docker rm -f；executor：服务端监督线程
                  docker wait 超时 rm -f，客户端轮询带死线兜底）；
    - ``kill``    尽力终止（异常路径的人工收口；wait 的超时路径内部也会调用）；
    - ``status``  非阻塞查询（纯读，无副作用）。
    """

    name: str = "sandbox"

    @abstractmethod
    def run(self, spec: SandboxRunSpec) -> SandboxHandle:
        """发起沙箱执行，返回句柄"""

    @abstractmethod
    def wait(self, handle: SandboxHandle, timeout: float) -> SandboxRunStatus:
        """阻塞等待执行完成（超时由驱动内部收口并返回 state=timeout）"""

    @abstractmethod
    def kill(self, handle: SandboxHandle) -> None:
        """尽力终止该执行（幂等）"""

    @abstractmethod
    def status(self, handle: SandboxHandle) -> SandboxRunStatus:
        """非阻塞查询当前状态"""


class DockerCliDriver(SandboxDriver):
    """本地 docker-CLI 驱动：W15 既有 ``docker run --rm`` 前台通道整体迁移（ARCH-1-2）

    - run：前台 ``-i`` 起容器，stdout/stderr 重定向到 cwd 下日志文件，stdin
      载荷经 docker CLI 管道传入；
    - wait：proc.wait(timeout)；超时经 kill（SIGTERM CLI → 宽限 → SIGKILL +
      ``docker rm -f`` 按容器名兜底，见 _terminate_docker_run）收口；
    - 本驱动即 SCRIPT_EXEC_MODE=container 在 EXECUTOR_URL 未配置时的回退通道
      （本机灰度/单机部署形态），隔离参数与 executor 通道同一事实源。
    """
    name = "docker-cli"

    def run(self, spec: SandboxRunSpec) -> SandboxHandle:
        stdout_file = Path(spec.cwd_host) / "stdout.log"
        stderr_file = Path(spec.cwd_host) / "stderr.log"
        args = [docker_binary(), *_build_sandbox_docker_run_args(
            image=spec.image,
            user=spec.user,
            mount_host=spec.workspace_host,
            mount_container=spec.workspace_container,
            workdir_container=spec.workdir_container,
            network_name=_worker_network_name(spec.net_profile),
            container_name=spec.container_name,
            tail_command=spec.entrypoint,
            interactive=True,
        )]
        try:
            out_f = open(stdout_file, "wb")
            try:
                err_f = open(stderr_file, "wb")
            except OSError:
                out_f.close()
                raise
            try:
                proc = subprocess.Popen(
                    args,
                    cwd=spec.cwd_host,
                    stdin=subprocess.PIPE,
                    stdout=out_f,
                    stderr=err_f,
                )
            finally:
                # 子进程持有 dup 后的 fd，父侧句柄即可关闭（与原 with 块语义一致）
                out_f.close()
                err_f.close()
        except OSError as e:
            # 含 docker CLI 缺失等启动失败（CLI 退出码 125 场景在 wait 的退出码分支）
            raise SandboxDriverError(f"容器执行启动失败: {e}")
        try:
            proc.stdin.write(spec.stdin_payload)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # 容器未读完 stdin 即退出，按退出码/输出处理
        return SandboxHandle(run_id=spec.container_name, container_name=spec.container_name,
                             extra={"proc": proc})

    def wait(self, handle: SandboxHandle, timeout: float) -> SandboxRunStatus:
        proc: subprocess.Popen = handle.extra["proc"]
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill(handle)
            returncode = proc.wait()
            return SandboxRunStatus(state="timeout", exit_code=returncode)
        except OSError as e:
            raise SandboxDriverError(f"容器执行启动失败: {e}")
        return SandboxRunStatus(state="done", exit_code=returncode)

    def kill(self, handle: SandboxHandle) -> None:
        _terminate_docker_run(handle.extra["proc"], handle.container_name)

    def status(self, handle: SandboxHandle) -> SandboxRunStatus:
        proc: subprocess.Popen = handle.extra["proc"]
        returncode = proc.poll()
        if returncode is None:
            return SandboxRunStatus(state="running")
        return SandboxRunStatus(state="done", exit_code=returncode)


def _worker_network_name(net_profile: str) -> str:
    """三档档位 → docker --network 取值（worker CLI 通道；未知档位拒绝）"""
    try:
        return WORKER_NET_PROFILE_TO_DOCKER[net_profile]
    except KeyError:
        raise SandboxDriverError(
            f"未知网络档位 {net_profile!r}（可选: {'/'.join(WORKER_NET_PROFILE_TO_DOCKER)}）")


def get_sandbox_driver() -> SandboxDriver:
    """container 模式的沙箱驱动选择（ARCH-1-2，设计 §2.3 D1 目标通道）

    - EXECUTOR_URL 已配置（环境变量实时读，优先于 Settings）→ HttpExecutorDriver
      （worker 与 executor 之间是 compose 内网 + token 的 HTTP 调用，实现见
      app/services/executor_client.py）；
    - 未配置 → DockerCliDriver（W15 既有 docker-CLI 单机通道回退：本机灰度、
      冒烟链路同构依赖与无 executor 的部署形态；隔离参数同一事实源，仅执行
      位置不同）。驱动实例无状态，每次调用新建（配置实时读，测试可 env 切换）。
    """
    from app.services.executor_client import executor_url, HttpExecutorDriver  # 懒加载避免顶层互引
    if executor_url():
        return HttpExecutorDriver()
    return DockerCliDriver()


# ---------------- 容器内执行（驱动无关编排，ARCH-1-2） ----------------

def execute_script_in_container(
    script: Dict[str, Any],
    staged_dir_host: Path,
    entry_rel: str,
    run_dir_host: Path,
    cwd_host: Path,
    stdin: Dict[str, Any],
    timeout: float,
    started: float,
    static_check: Optional[List[str]] = None,
    network: bool = False,
    driver: Optional[SandboxDriver] = None,
) -> Dict[str, Any]:
    """container 模式执行：经 SandboxDriver 跑沙箱，返回结构与 local 完全一致

    ARCH-1-2 起为驱动无关编排：路径映射、stdin 宿主路径重写、结果解析归一化
    全部共用（本函数），执行差异（docker-CLI 前台通道 / executor HTTP 受理+
    轮询）下沉到驱动；接入新驱动（如 K8s Job）本函数零改动。

    Args:
        script: 脚本定义 {script_id, name, ...}
        staged_dir_host: 宿主侧脚本包副本目录（工作区 scripts/ 下，严格拷贝语义）
        entry_rel: 入口相对脚本包的路径
        run_dir_host: 本次工作区绝对路径（唯一 bind mount 源）
        cwd_host: 宿主 cwd（stdout/stderr 日志落盘，与 local 同位）
        stdin: stdin payload（宿主路径版；函数内重写为容器路径）
        timeout: 超时秒数（超时终止容器）
        started: 计时起点 time.time()
        static_check: 运行前静态风险扫描结果（提示级透传）
        network: 任务级网络开关（W15 语义；经 resolve_net_profile 归一为三档）
        driver: 显式驱动（分流点注入）；缺省按 get_sandbox_driver() 解析

    返回 pre_script_results 标准条目（_normalize_stdout 组装，复用
    pre_judge_runner 的解析与归一化，保证两条通道语义一致）。
    """
    # 延迟 import：避免模块顶层互引（pre_judge_runner 顶层 import 本模块）
    from app.services.pre_judge_runner import (
        _parse_last_json_line,
        _normalize_stdout,
        _read_file_tail,
        _result,
        SCRIPT_STDERR_LIMIT,
        SCRIPT_STDOUT_LIMIT,
    )

    script_id = str(script.get("script_id") or script.get("id") or "")
    name = script.get("name") or script_id
    script_key = script_id or name or "script"
    static_check = static_check or []

    image = runner_image()
    user = runner_user()
    paths = build_container_paths(run_dir_host, cwd_host, staged_dir_host)
    container_name = _new_container_name(str(script_key))

    # stdin 里的宿主工作区路径全部重写为容器路径（脚本在容器内按 /workspace 取值）
    container_stdin = rewrite_stdin_paths(stdin, paths.run_dir_host, paths.run_dir_container)
    stdin_payload = _dump_stdin(container_stdin)

    spec = SandboxRunSpec(
        image=image,
        user=user,
        workspace_host=paths.run_dir_host,
        workspace_container=paths.run_dir_container,
        workdir_container=paths.cwd_container,
        cwd_host=paths.cwd_host,
        net_profile=resolve_net_profile(network),
        container_name=container_name,
        entrypoint=[CONTAINER_PYTHON, CONTAINER_ENTRY_WRAPPER,
                    paths.staged_dir_container, entry_rel],
        stdin_payload=stdin_payload,
        timeout=timeout,
    )
    sandbox_driver = driver or get_sandbox_driver()

    try:
        # ARCH-2-3：沙箱启动（受理）延迟打点——只观察受理成功（口径见
        # SANDBOX_START_LATENCY 注释）
        _sandbox_start = time.monotonic()
        handle = sandbox_driver.run(spec)
        SANDBOX_START_LATENCY.labels(driver=sandbox_driver.name).observe(
            time.monotonic() - _sandbox_start)
        status = sandbox_driver.wait(handle, timeout)
    except SandboxDriverError as e:
        # 驱动层错误（CLI 启动失败/executor 白名单拒绝/连接失败）：面向运维定位
        return _result(script_id, name, "fail", error=str(e),
                       finish_reason="error", started_at=started, static_check=static_check)

    # 两条驱动的日志统一归位到 cwd 下（CLI：Popen 重定向已在位；executor：
    # wait 收口时把工作区根的 docker logs 落盘文件挪到 cwd，见 _relocate_logs）
    stdout_file = Path(cwd_host) / "stdout.log"
    stderr_file = Path(cwd_host) / "stderr.log"
    stdout_tail = _read_file_tail(stdout_file, SCRIPT_STDOUT_LIMIT)
    stderr_tail = _read_file_tail(stderr_file, SCRIPT_STDERR_LIMIT)

    if status.state == "timeout":
        detail = f"；{status.error}" if status.error else ""
        return _result(script_id, name, "fail",
                       error=f"脚本执行超时被终止（>{timeout:.0f}秒，容器已回收）{detail}",
                       finish_reason="timeout", started_at=started, static_check=static_check)

    if status.state == "failed":
        return _result(script_id, name, "fail",
                       error=(status.error or f"沙箱执行失败（{sandbox_driver.name} 驱动）")[:4000],
                       finish_reason="error", started_at=started, static_check=static_check)

    if status.exit_code != 0:
        hint = _docker_failure_hint(status.exit_code, stderr_tail, image)
        error = f"脚本退出码 {status.exit_code}: {(stderr_tail.strip() or '无 stderr 输出')[:4000]}{hint}"
        return _result(script_id, name, "fail", error=error[:4000],
                       finish_reason="error", started_at=started, static_check=static_check)

    try:
        stdout = _parse_last_json_line(stdout_tail)
    except ValueError as e:
        return _result(script_id, name, "fail", error=str(e),
                       finish_reason="error", started_at=started, static_check=static_check)

    return _normalize_stdout(script_id, name, stdout, started, static_check=static_check)


def _dump_stdin(value: Any) -> bytes:
    """stdin payload 序列化（与 local 同口径：ensure_ascii=False + default=str）"""
    import json
    return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def _terminate_docker_run(proc: subprocess.Popen, container_name: str) -> None:
    """超时终止：SIGTERM docker CLI（转发容器优雅退出）→ 宽限期 → SIGKILL +
    ``docker rm -f`` 按容器名兜底回收（CLI 被杀时 --rm 可能失效）"""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=CONTAINER_TERM_GRACE_SECONDS)
            return  # CLI 随容器退出，--rm 已完成清理
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except OSError:
        pass
    try:
        subprocess.run(
            [docker_binary(), "rm", "-f", container_name],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"[PreScripts] 超时容器 {container_name} 兜底清理失败: {e}")


def _docker_failure_hint(returncode: Optional[int], stderr_tail: str, image: str) -> str:
    """docker CLI 退出码 125（daemon 拒绝 run）时的运维提示（镜像未预载等）"""
    if returncode != 125:
        return ""
    tail = stderr_tail.strip()
    if image in tail or "No such image" in tail or "manifest unknown" in tail:
        return (f"（提示：runner 镜像 {image} 不可用——需在部署机预构建/离线导入，"
                "见 docker/script-runner/README.md）")
    return "（提示：docker run 被拒绝（退出码 125），请检查 runner 镜像与 docker daemon 状态）"


# ---------------- 依赖声明校验（container 分支） ----------------

def runner_package_base_names() -> List[str]:
    """runner 镜像内已装包名集合（小写归一；requests==2.32.5 → requests）"""
    names = []
    for spec in RUNNER_IMAGE_PACKAGES:
        match = __import__("re").match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", spec.strip())
        if match:
            names.append(match.group(0).lower())
    return names


def check_requirements_against_runner(requirements: List[str]) -> List[str]:
    """对照 runner 镜像静态清单校验依赖声明，返回缺失包名列表

    镜像不可变（--read-only + 离线预载）：缺失只能通过扩展 packages.txt
    重建镜像解决，没有运行时 pip install 路径。
    """
    from app.services.pre_judge_script_service import _requirement_base_name
    installed = set(runner_package_base_names())
    missing: List[str] = []
    for item in requirements:
        base = _requirement_base_name(item).lower()
        if base and base not in installed and base not in missing:
            missing.append(base)
    return missing
