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
  ``network: true`` 时改用默认 bridge 网络池（「脚本要访问被测系统」
  需求的任务级开关；受限内网网段的精细化控制属后续迭代）。
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

执行返回结构与 local 模式完全一致（pre_script_results 标准条目）。
"""
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.core.config import get_settings

logger = logging.getLogger(__name__)

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
# 超时后 SIGTERM docker CLI 的宽限期（秒）：给容器优雅退出与 --rm 收尾的时间
CONTAINER_TERM_GRACE_SECONDS = 5.0

# 容器内显式注入的最小 env（白名单语义：无宿主 env 透传，docker run 不带
# --env 就没有宿主环境）——只给 Python 运行必需项
CONTAINER_ENV_ALLOWLIST: Dict[str, str] = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    "LANG": "C.UTF-8",
    "HOME": CONTAINER_TMPFS_HOME,
}

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

def build_docker_run_args(
    image: str,
    user: str,
    paths: ContainerPaths,
    entry_rel: str,
    container_name: str,
    network: bool = False,
) -> List[str]:
    """构造 docker run 参数列表（不含执行，纯构造）

    隔离清单（整改计划 §4 W15 方案要点逐项落位）：
    - ``--rm``        退出即清理容器
    - ``--name``      唯一容器名（超时兜底 ``docker rm -f`` 依赖）
    - ``--user``      非 root 运行（uid:gid）
    - ``--memory``/``--memory-swap``/``--cpus``/``--pids-limit`` 资源上限
      （memory-swap = memory：禁用 swap 扩展，超限 OOM kill 而非拖垮宿主）
    - ``--read-only`` 根文件系统只读；可写面仅 bind mount 工作区 + tmpfs
    - ``--tmpfs``     /tmp 与容器用户 home（nosuid,noexec，限 64m）
    - ``--network``   默认 none（完全离网）；network=True 时 bridge
    - ``-i``          stdin 载荷经 docker CLI 传入容器（-t 不加：无 TTY）
    - ``-e``          仅注入 CONTAINER_ENV_ALLOWLIST 最小集合（平台 env 剥离）
    - ``-v``          仅挂载本次 script_runs 工作目录（run_dir → /workspace）
    - ``-w``          容器内 cwd（工作区下 script_id 子目录，与 local 一致）

    尾部命令与 local 同构：[python, pre_judge_entry.py, staged_dir, entry_rel]，
    wrapper 在镜像内 /opt/runner/pre_judge_entry.py（构建时 COPY）。
    """
    args: List[str] = [
        "run", "--rm",
        "--name", container_name,
        "--user", user,
        "--memory", CONTAINER_MEMORY,
        "--memory-swap", CONTAINER_MEMORY,
        "--cpus", CONTAINER_CPUS,
        "--pids-limit", str(CONTAINER_PIDS_LIMIT),
        "--read-only",
        "--tmpfs", f"{CONTAINER_TMPFS_TMP}:rw,nosuid,noexec,size={CONTAINER_TMPFS_SIZE}",
        "--tmpfs", f"{CONTAINER_TMPFS_HOME}:rw,nosuid,noexec,size={CONTAINER_TMPFS_SIZE}",
        "--network", ("bridge" if network else "none"),
    ]
    for key, val in CONTAINER_ENV_ALLOWLIST.items():
        args.extend(["-e", f"{key}={val}"])
    args.extend([
        "-i",
        "-v", f"{paths.run_dir_host}:{paths.run_dir_container}",
        "-w", paths.cwd_container,
        image,
        CONTAINER_PYTHON, CONTAINER_ENTRY_WRAPPER, paths.staged_dir_container, entry_rel,
    ])
    return args


def docker_binary() -> str:
    """docker CLI 可执行文件（PATH 解析，可被环境变量覆盖兜底）"""
    return os.environ.get("DOCKER_BINARY") or "docker"


def _new_container_name(script_key: str) -> str:
    """唯一容器名（并发执行不冲突；超时兜底清理按名回收）"""
    tag = uuid.uuid4().hex[:12]
    safe = "".join(c if c.isalnum() else "-" for c in script_key)[:40].strip("-") or "script"
    return f"aievo-script-{safe[:40]}-{tag}"


# ---------------- 容器内执行 ----------------

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
) -> Dict[str, Any]:
    """container 模式执行：docker run --rm 跑脚本，返回结构与 local 完全一致

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
        network: 任务级网络开关（默认关 → --network none）

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

    args = [docker_binary(), *build_docker_run_args(
        image=image, user=user, paths=paths, entry_rel=entry_rel,
        container_name=container_name, network=network,
    )]

    stdout_file = Path(cwd_host) / "stdout.log"
    stderr_file = Path(cwd_host) / "stderr.log"
    timed_out = False
    returncode: Optional[int] = None
    try:
        with open(stdout_file, "wb") as out_f, open(stderr_file, "wb") as err_f:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd_host),
                stdin=subprocess.PIPE,
                stdout=out_f,
                stderr=err_f,
            )
            try:
                proc.stdin.write(stdin_payload)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # 容器未读完 stdin 即退出，按退出码/输出处理
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_docker_run(proc, container_name)
                returncode = proc.wait()
    except OSError as e:
        # 含镜像缺失等启动失败（docker CLI 退出码 125 场景在 returncode 分支）
        return _result(script_id, name, "fail", error=f"容器执行启动失败: {e}",
                       finish_reason="error", started_at=started, static_check=static_check)

    stdout_tail = _read_file_tail(stdout_file, SCRIPT_STDOUT_LIMIT)
    stderr_tail = _read_file_tail(stderr_file, SCRIPT_STDERR_LIMIT)

    if timed_out:
        return _result(script_id, name, "fail",
                       error=f"脚本执行超时被终止（>{timeout:.0f}秒，容器已回收）",
                       finish_reason="timeout", started_at=started, static_check=static_check)

    if returncode != 0:
        hint = _docker_failure_hint(returncode, stderr_tail, image)
        error = f"脚本退出码 {returncode}: {(stderr_tail.strip() or '无 stderr 输出')[:4000]}{hint}"
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
