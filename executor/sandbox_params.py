"""沙箱 docker run 参数构造与白名单校验——worker/executor 单一事实源（ARCH-1-1）

设计（plans/2026-10-15-企业级评测系统架构设计.md §2.3 D1）：docker.sock 的信任
边界收敛到 executor 小服务，其只接受「参数白名单内」的沙箱请求。本模块把原
``app/services/script_container.py``（W15 worker 侧 docker-CLI 通道）的隔离参数
构造抽成公共模块，worker 通道与 executor 服务（``executor/app.py``）共用同一份
构造逻辑：

- **单一事实源纪律**：两侧约束演进必须改本模块并同步两侧注释（ARCH-1-1），
  严禁在任一侧私增 docker 参数（白名单语义，越界即拒绝）；
- worker 侧经 ``script_container.build_docker_run_args`` 薄封装调用（保持既有
  调用面不变），executor 侧在受理请求时校验白名单后调用；
- 加固位（--user/--memory/--cpus/--pids-limit/--read-only/--tmpfs/--cap-drop/
  no-new-privileges/-e 最小集合）对两侧一致——worker 通道随抽取一并获得
  ``--cap-drop ALL`` 与 ``no-new-privileges``（预对齐 Phase 1-5 门禁）。

本模块刻意只用标准库：worker 镜像（COPY 全仓库）与 executor 镜像（仅 COPY
executor/）两侧都要零依赖可导入。
"""
import os
import re
from typing import Dict, List, Tuple

# ---------------- 沙箱容器固定常量（原 script_container 常量平移，名称不变） ----------------

# 宿主工作区（uploads/script_runs/{task}/{data}/）在容器内的挂载点
CONTAINER_WORKSPACE = "/workspace"
# 容器内 tmpfs：/tmp（Python tempfile 缺省位）与容器用户 home（HOME/.cache 可写面）
CONTAINER_TMPFS_TMP = "/tmp"
CONTAINER_TMPFS_HOME = "/home/runner"
# runner 镜像内 wrapper 路径（构建时 COPY app/services/pre_judge_entry.py）
CONTAINER_ENTRY_WRAPPER = "/opt/runner/pre_judge_entry.py"
# 容器内 python 解释器（镜像 PATH 内）
CONTAINER_PYTHON = "python"

# 资源限制（W15 方案要点：--memory 1g --cpus 0.5；--pids-limit 为
# forkbomb 验收项的资源面强化）
CONTAINER_MEMORY = "1g"
CONTAINER_CPUS = "0.5"
CONTAINER_PIDS_LIMIT = 128
CONTAINER_TMPFS_SIZE = "64m"

# 容器内显式注入的最小 env（白名单语义：无宿主 env 透传，docker run 不带
# --env 就没有宿主环境）——只给 Python 运行必需项
CONTAINER_ENV_ALLOWLIST: Dict[str, str] = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    "LANG": "C.UTF-8",
    "HOME": CONTAINER_TMPFS_HOME,
}

# 沙箱运行用户（executor 白名单硬编码值；worker 侧缺省同值，见
# script_container.runner_user）
SANDBOX_USER = "10001:10001"

# ARCH-1-1 新增加固位（设计 D1：用户/资源上限/read-only/tmpfs/
# no-new-privileges/cap_drop=ALL 固定）——Phase 1-5 的 W15 门禁扩项断言同源
CONTAINER_CAP_DROP = "ALL"
CONTAINER_SECURITY_OPT = "no-new-privileges:true"


class SandboxParamError(ValueError):
    """沙箱参数白名单校验失败（executor 侧统一映射为 400 拒绝并审计）"""


# ---------------- docker CLI 可执行文件 ----------------

def docker_binary() -> str:
    """docker CLI 可执行文件（PATH 解析，可被环境变量覆盖兜底）"""
    return os.environ.get("DOCKER_BINARY") or "docker"


# ---------------- docker 选项位取值防线（防「值即旗标」） ----------------

def _require_flag_safe(kind: str, value: str) -> str:
    """docker 选项位取值防线：非空且不以 "-" 开头

    subprocess 列表参数调用无 shell 注入面，但 docker CLI 仍会把以 "-" 开头的
    独立 argv 项解析成额外旗标——所有拼进 ``docker run`` 的取值统一过这道闸，
    哪怕调用方（executor/worker）上游已校验过（纵深防御）。
    """
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise SandboxParamError(f"{kind} 非法取值: {value!r}")
    return value


# ---------------- 镜像白名单（硬编码，非配置——设计 D1） ----------------

# script-runner 系镜像清单（允许的镜像基名，不带 registry 前缀）。变体镜像
# （D8 的 node / claude-code-runtime 基座）落地时在此扩展并同步两侧注释。
RUNNER_IMAGE_BASES: Tuple[str, ...] = ("ai-evo-script-runner",)

# 合法 tag 字符集（docker tag 规范子集；digest 引用（@sha256:...）钉扎属
# Phase 3-3 配置化，1-1 不开放，一律拒绝）
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_runner_image(image: str) -> str:
    """镜像白名单校验：仅 script-runner 系清单（<base> 或 <base>:<tag>）

    拒绝：未知基名、registry 前缀（含 "/"）、digest 引用（含 "@"）、
    flag 样值、空值。返回原样镜像引用（合法输入无需规范化）。
    """
    _require_flag_safe("image", image)
    if "/" in image or "@" in image:
        raise SandboxParamError(f"镜像 {image!r} 不在白名单：不允许 registry 前缀/digest 引用")
    base, sep, tag = image.partition(":")
    if base not in RUNNER_IMAGE_BASES:
        raise SandboxParamError(
            f"镜像 {image!r} 不在白名单：仅允许 {', '.join(RUNNER_IMAGE_BASES)} 系")
    if sep and not _IMAGE_TAG_RE.match(tag):
        raise SandboxParamError(f"镜像 {image!r} tag 非法")
    return image


# ---------------- bind mount 白名单（硬编码，非配置——设计 D1） ----------------

# executor 视角的工作区根：仅该子树允许作为 bind mount 源（宿主 uploads 卷
# 挂到 executor 容器的 /app/uploads）。生产恒为该常量；单测经 monkeypatch
# 替换到临时目录（无 /app 写权限），不构成配置面。
WORKSPACE_ROOT = "/app/uploads/script_runs"


def resolve_workspace_mount(workspace: str) -> str:
    """bind mount 白名单校验：仅 WORKSPACE_ROOT 子树（realpath 校验）

    - realpath 解析符号链接后必须严格落在 WORKSPACE_ROOT **之下**（不含根本
      身——挂整个 script_runs 面过宽），防 ``../`` 字面穿越与 symlink 逃逸；
    - 必须是已存在的目录（worker 先准备工作区再来请求，不存在即拒绝）；
    - 返回解析后的真实路径作为挂载源（残余 TOCTOU 窗口由「仅 token 内网
      调用方」的信任边界兜底，Phase 1-3 compose 收口监听面）。
    """
    _require_flag_safe("workspace", workspace)
    root_real = os.path.realpath(WORKSPACE_ROOT)
    ws_real = os.path.realpath(workspace)
    try:
        common = os.path.commonpath([root_real, ws_real])
    except ValueError:  # 不同盘符/不可比较路径（macOS/Windows 测试环境兜底）
        raise SandboxParamError(f"工作区 {workspace!r} 不在 {WORKSPACE_ROOT} 子树内")
    if common != root_real:
        raise SandboxParamError(f"工作区 {workspace!r} 不在 {WORKSPACE_ROOT} 子树内（越界 bind mount）")
    if ws_real == root_real:
        raise SandboxParamError(f"工作区不能是 {WORKSPACE_ROOT} 根本身（挂载面过宽）")
    if not os.path.isdir(ws_real):
        raise SandboxParamError(f"工作区 {ws_real!r} 不存在或不是目录（须先准备工作区）")
    return ws_real


# ---------------- 网络档位白名单（设计 D2：executor 仅放行 none|egress） ----------------

# 档位 → docker 网络名。egress 档加入沙箱出网 bridge（白名单域名出网，
# Phase 1-3 compose 建）；open 档（超管审批直出）1-1 不开放，无映射。
NET_PROFILES: Dict[str, str] = {
    "none": "none",
    "egress": "aievo-sandbox-egress",
}


def network_profile_to_docker(net_profile: str) -> str:
    """网络档位白名单：none|egress → docker --network 取值，未知档位拒绝"""
    if net_profile not in NET_PROFILES:
        raise SandboxParamError(
            f"网络档位 {net_profile!r} 不在白名单：仅允许 {'|'.join(NET_PROFILES)}")
    return NET_PROFILES[net_profile]


# ---------------- docker run 参数构造（纯函数，两侧共用——单一事实源） ----------------

def build_docker_run_args(
    *,
    image: str,
    user: str,
    mount_host: str,
    mount_container: str,
    workdir_container: str,
    network_name: str,
    container_name: str,
    tail_command: List[str],
    interactive: bool = False,
    detach: bool = False,
    auto_rm: bool = True,
) -> List[str]:
    """构造 docker run 参数列表（不含执行，纯构造；不含 "docker" 前缀）

    隔离清单（W15 方案要点 + ARCH-1-1 加固位逐项落位，两侧一致）：
    - ``--rm``        退出即清理容器（auto_rm；executor 后台监督模式改显式
                      docker rm -f 回收，见 executor/app.py._supervise_run）
    - ``-d``          后台运行（detach；executor 模式，worker 前台模式不用）
    - ``--name``      唯一容器名（超时兜底 ``docker rm -f`` 依赖）
    - ``--user``      非 root 运行（uid:gid；executor 侧恒为 SANDBOX_USER）
    - ``--memory``/``--memory-swap``/``--cpus``/``--pids-limit`` 资源上限
      （memory-swap = memory：禁用 swap 扩展，超限 OOM kill 而非拖垮宿主）
    - ``--cap-drop ALL`` + ``--security-opt no-new-privileges:true``（ARCH-1-1：
      设计 D1 固定加固位，剥夺全部 capabilities 且禁止提权）
    - ``--read-only`` 根文件系统只读；可写面仅 bind mount 工作区 + tmpfs
    - ``--tmpfs``     /tmp 与容器用户 home（nosuid,noexec，限 64m）
    - ``--network``   由调用方给最终网络名（worker：none|bridge 任务级开关；
      executor：none|egress 档位经 network_profile_to_docker 映射）
    - ``-e``          仅注入 CONTAINER_ENV_ALLOWLIST 最小集合（平台 env 剥离）
    - ``-i``          stdin 载荷经 docker CLI 传入容器（interactive；-t 不加：无 TTY）
    - ``-v``          仅挂载调用方给定的单一工作区（worker：本次 script_runs
      工作目录；executor：白名单 realpath 校验后的 script_runs 子树路径）
    - ``-w``          容器内工作目录

    尾部命令（tail_command）= 镜像后的容器 argv（exec 形式，无 shell 参与）：
    worker 为 [python, pre_judge_entry.py, staged_dir, entry_rel]，executor 为
    请求方提交的 entrypoint（首元素过旗标闸）。**本函数不开放任何额外 docker
    能力参数**（无 privileged/pid/devices/...）——那是白名单之外，不是缺省关。
    """
    for kind, value in (
        ("image", image), ("user", user), ("network_name", network_name),
        ("container_name", container_name), ("mount_host", mount_host),
        ("mount_container", mount_container), ("workdir_container", workdir_container),
    ):
        _require_flag_safe(kind, value)
    if not tail_command:
        raise SandboxParamError("tail_command 不能为空")
    _require_flag_safe("tail_command[0]", tail_command[0])

    args: List[str] = ["run"]
    if auto_rm:
        args.append("--rm")
    if detach:
        args.append("-d")
    args.extend([
        "--name", container_name,
        "--user", user,
        "--memory", CONTAINER_MEMORY,
        "--memory-swap", CONTAINER_MEMORY,
        "--cpus", CONTAINER_CPUS,
        "--pids-limit", str(CONTAINER_PIDS_LIMIT),
        "--cap-drop", CONTAINER_CAP_DROP,
        "--security-opt", CONTAINER_SECURITY_OPT,
        "--read-only",
        "--tmpfs", f"{CONTAINER_TMPFS_TMP}:rw,nosuid,noexec,size={CONTAINER_TMPFS_SIZE}",
        "--tmpfs", f"{CONTAINER_TMPFS_HOME}:rw,nosuid,noexec,size={CONTAINER_TMPFS_SIZE}",
        "--network", network_name,
    ])
    for key, val in CONTAINER_ENV_ALLOWLIST.items():
        args.extend(["-e", f"{key}={val}"])
    if interactive:
        args.append("-i")
    args.extend([
        "-v", f"{mount_host}:{mount_container}",
        "-w", workdir_container,
        image,
        *tail_command,
    ])
    return args
