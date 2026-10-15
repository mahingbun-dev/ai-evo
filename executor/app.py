"""executor 沙箱执行服务（ARCH-1-1，设计 §2.2 拓扑 / §2.3 D1 / §2.4 时序）

专职沙箱生命周期管理：compose 内唯一挂 docker.sock 的角色——非 root
（镜像 USER 10002）、无凭证、无 DB 依赖的小服务；worker 经 compose 内网 +
token 的 HTTP 调用（1-2 起由 SandboxDriver 的 HTTP 驱动接入）。

接口（设计 §2.4 沙箱单次执行时序）：
- ``POST /sandbox/run``        校验参数白名单 → ``docker run -d`` → 202 {run_id}；
                               后台监督线程 docker wait（超时 rm -f 强杀）→
                               docker logs 落工作区 → docker rm -f 回收
- ``GET /sandbox/runs/{id}``   查询状态/退出码/输出尾部（worker 轮询取果，
                               stdout/stderr 全量在工作区文件）
- ``GET /health``              存活探针（无鉴权，供 compose healthcheck）

参数白名单（硬编码，非配置——设计 D1；校验全部在 executor/sandbox_params.py，
与 worker docker-CLI 通道同一事实源）：
- 镜像仅 script-runner 系清单；``--user 10001:10001``、资源上限/read-only/
  tmpfs/cap-drop=ALL/no-new-privileges 固定；网络档仅 none|egress；
  运行时档仅 runc|runsc（runsc = gVisor 加固档，高危脚本专用，ARCH-3-4）；
  bind mount 仅 /app/uploads/script_runs 子树（realpath 校验）；bind 源
  （-v 左值，docker daemon 宿主路径口径）经可选 mount_source 字段映射
  （SANDBOX_WORKSPACE_HOST_ROOT 命名卷部署，sandbox_params.resolve_mount_source
  ——严格落在宿主根之下，越界/伪造 400；未配置 = workspace 本身，行为不变）；
  容器内工作目录（``workdir`` → ``-w``）仅 /workspace 子树（缺省挂载根；worker
  的 per-script cwd /workspace/{script_id} 经此透传——harness 三件套与脚本
  artifacts 按 cwd 相对寻址，硬编码挂载根会让 agent 通道必然 FileNotFound）；
- 请求模型 ``extra="forbid"``——``privileged``、``--pid=host``、任意 ``-v``
  等一切额外 docker 能性**没有字段可表达**，注入未知字段在反序列化层即 422。

鉴权：``EXECUTOR_TOKEN``（设计 D6：走 .env，不入库），``Authorization:
Bearer`` 头 + hmac.compare_digest 恒时比较；未配置 token 即 fail-closed
拒绝全部沙箱请求（503）。

审计：每次 run 的全量请求参数（含白名单解析后的挂载源/容器名）与最终
退出码/状态都落 ``executor.audit`` 日志（INFO，随容器 stdout 采集）；被白名单
拒绝的请求落 WARNING（含原因与原始参数）。

实现约定：
- docker CLI 经 subprocess 列表参数调用（无 shell），命令构造完全来自
  sandbox_params（单一事实源），本文件不拼任何 docker 旗标；
- run 登记表在进程内存（单进程 uvicorn，不加 --workers——多进程会分裂，
  Dockerfile.executor CMD 已固定单进程；多副本外置属 Phase 2-3）；
- 状态机：running → done（正常退出，exit_code 任意值）| timeout（超时强杀）
  | failed（基础设施错误：daemon 拒绝/wait 失败/日志不可得）。
"""
import hmac
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from executor import sandbox_params
from executor.sandbox_params import SandboxParamError

logger = logging.getLogger(__name__)
# 审计通道：与业务日志分离，采集侧可单独接走（设计 D7 沙箱事件审计）
audit_logger = logging.getLogger("executor.audit")


def _configure_audit_logging(audit: logging.Logger) -> None:
    """审计通道生产 wiring（设计 D7）：INFO 审计记录必须有落点

    部署形态是裸 uvicorn（Dockerfile.executor CMD，无 --log-config）：uvicorn
    默认只配 uvicorn.* 三个 logger，root 无 handler 且有效级别 WARNING——不做
    本 wiring 时 accepted/finished 的 INFO 审计在 logger 层即被丢弃，仅 reject
    的 WARNING 经 lastResort 落 stderr，「每次 run 全参数+退出码落审计」在实机
    不成立。这里：
    - 显式 setLevel(INFO)：不受 root/调用方有效级别门限影响；
    - root 尚无 handler 时挂 stdout handler（容器 stdout 随 compose json-file
      日志采集，与镜像运行约定一致）——进程已自配 root handler（pytest
      caplog、运维 logging 配置）则不重复挂，记录经 propagate 到达既有落点；
    - 幂等：重复 import / 二次调用不叠加 handler。
    """
    audit.setLevel(logging.INFO)
    if audit.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"))
    audit.addHandler(handler)


_configure_audit_logging(audit_logger)

# EXECUTOR_TOKEN 环境变量名（.env 注入；未配置 = fail-closed）
EXECUTOR_TOKEN_ENV = "EXECUTOR_TOKEN"
# docker run -d 本身的 CLI 超时（启动，不含脚本执行）
RUN_START_TIMEOUT_SECONDS = 30.0
# docker wait 的超时宽限（等待期 = 请求 timeout + 本宽限，之后按 rm -f 强杀收口）
WAIT_GRACE_SECONDS = 5.0
# docker logs / docker rm 的 CLI 超时
LOGS_TIMEOUT_SECONDS = 30.0
RM_TIMEOUT_SECONDS = 30.0
# 监督线程写进工作区的日志文件名（worker 侧解析口径不变：stdout/stderr 落盘）
LOG_FILE_STDOUT = "stdout.log"
LOG_FILE_STDERR = "stderr.log"
# GET 查询回显的输出尾部上限（字节；全量在工作区文件，接口只给诊断尾部）
GET_TAIL_BYTES = 64 * 1024
# entrypoint 上限：argv 元素数与单元素长度（exec 形式，无 shell 拼接面）
ENTRYPOINT_MAX_ITEMS = 64
ENTRYPOINT_MAX_ITEM_CHARS = 4096

app = FastAPI(
    title="ai-evo executor",
    version="1.0.0",
    # 内网 token 服务：关文档端点，收口暴露面（compose 内网监听见 1-3）
    docs_url=None, redoc_url=None, openapi_url=None,
)


# ---------------- 请求/登记模型 ----------------

class SandboxRunRequest(BaseModel):
    """沙箱运行请求——字段集即白名单（设计 §2.4：{image, workspace, net_profile, timeout, entrypoint}
    + 后续批次按白名单纪律扩的 runtime（ARCH-3-4）/ workdir（容器内工作目录））

    ``extra="forbid"``：privileged/pid_host/volumes 等任何额外 docker 能力
    没有字段可表达，注入未知字段直接 422——executor 不是通用 docker 代理。
    """
    model_config = ConfigDict(extra="forbid")

    image: str = Field(min_length=1, max_length=256)
    # 宿主（executor 视角）工作区绝对路径，须在 /app/uploads/script_runs 子树内
    workspace: str = Field(min_length=1, max_length=4096)
    # bind 源（docker daemon 宿主路径口径，命名卷部署）：SANDBOX_WORKSPACE_HOST_ROOT
    # 配置时由 worker 按后缀规则派生（script_container.derive_mount_source）；
    # 白名单校验在 sandbox_params.resolve_mount_source（单一事实源）——必须严格
    # 落在宿主根之下，越界 400。缺省 None = 未配置形态由 executor 按工作区
    # 解析结果兜底（同路径 bind 部署行为不变，此时显式携带必须与 workspace 一致）
    mount_source: Optional[str] = Field(default=None, max_length=4096)
    # 容器内工作目录（docker run -w）：worker 侧 per-script cwd = /workspace/{script_id}
    # （script_container.build_container_paths 推导，harness 通道的三件套与脚本通道
    # 的 artifacts 都按 cwd 相对寻址）。白名单校验在 sandbox_params.
    # resolve_container_workdir（单一事实源）：须落在 /workspace 子树内，否则 400；
    # 缺省 None → /workspace（挂载根，既有未带字段的调用方行为不变）
    workdir: Optional[str] = Field(default=None, max_length=4096)
    net_profile: str = "none"  # 档位枚举校验在 sandbox_params（单一事实源），未知档 400
    # 容器运行时档（ARCH-3-4，设计 §3 表 3-4 行）：runc（缺省，daemon 默认，
    # 不拼旗标）| runsc（gVisor 加固档，宿主机须预装注册，见
    # docs/sandbox-runtime-gvisor.md）；枚举校验在 sandbox_params（单一事实源），
    # 未知值 400。缺省 runc 保证既有调用方（未带本字段的 worker 客户端）行为不变
    runtime: str = "runc"
    timeout: float = Field(default=600.0, gt=0.0, le=3600.0)
    # exec 形式容器 argv（镜像后的命令），无 shell 参与拼接
    entrypoint: List[str] = Field(min_length=1, max_length=ENTRYPOINT_MAX_ITEMS)


@dataclass
class RunRecord:
    """单次沙箱 run 的登记项（内存登记表；审计与查询共用）"""
    run_id: str
    container_name: str
    request: Dict[str, Any]   # 全量请求参数（审计/查询回显）
    mount_host: str           # docker daemon 的 bind 源（-v 左值；命名卷部署 = 宿主根映射后路径，审计用）
    workspace_real: str       # 工作区 executor 容器视角真实路径（resolve_workspace_mount 产物；日志落盘/尾部读取寻址）
    started_at: float
    state: str = "running"    # running|done|timeout|failed
    exit_code: Optional[int] = None
    error: str = ""
    finished_at: Optional[float] = None


# run 登记表（单进程内存；见模块 docstring 实现约定）
_runs: Dict[str, RunRecord] = {}
_runs_lock = threading.Lock()


# ---------------- 鉴权（EXECUTOR_TOKEN，fail-closed） ----------------

def _require_token(authorization: Optional[str]) -> None:
    """EXECUTOR_TOKEN 校验：Bearer 头 + 恒时比较；未配置即拒绝全部请求"""
    token = os.environ.get(EXECUTOR_TOKEN_ENV, "")
    if not token:
        raise HTTPException(
            status_code=503,
            detail="EXECUTOR_TOKEN 未配置：fail-closed，拒绝所有沙箱请求")
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[len("Bearer "):]
    if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="invalid executor token")


# ---------------- entrypoint 校验（载荷防线） ----------------

def _validate_entrypoint(entrypoint: List[str]) -> List[str]:
    """exec 形式 argv 校验：非空元素、无 NUL、首元素不得是旗标样值

    载荷在沙箱内以 uid 10001 + cap-drop ALL 运行，威胁模型是宿主而非载荷
    本身；此校验防的是「把 docker 旗标塞进 argv 首位被 CLI 误解析」
    （sandbox_params._require_flag_safe 同闸，纵深防御）。
    """
    for item in entrypoint:
        if not item or len(item) > ENTRYPOINT_MAX_ITEM_CHARS:
            raise SandboxParamError("entrypoint 含空元素或超长元素")
        if "\x00" in item:
            raise SandboxParamError("entrypoint 含 NUL 字符")
    sandbox_params._require_flag_safe("entrypoint[0]", entrypoint[0])
    return entrypoint


# ---------------- 后台监督：wait → logs 落盘 → rm ----------------

def _run_cli(args: List[str], timeout: float) -> "subprocess.CompletedProcess[bytes]":
    """docker CLI 调用统一出口（subprocess 列表参数，无 shell）"""
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False)


def _tail_bytes(path: Path, limit: int = GET_TAIL_BYTES) -> str:
    """读文件尾部（GET 回显诊断用；异常/不存在返回空串）"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _supervise_run(record: RunRecord, timeout: float) -> None:
    """后台监督线程（设计 §2.4）：docker wait（带超时）→ docker logs 落工作区 → docker rm -f

    - 容器不带 --rm（detached 模式下退出即删会丢日志），由本线程统一回收；
    - 超时：docker wait 超时后 docker rm -f 强杀（SIGKILL），仍尽力收集
      已产出的部分日志；
    - 状态：正常退出（含脚本非零退出码）= done；超时 = timeout；
      daemon/CLI 层错误 = failed。
    """
    docker = sandbox_params.docker_binary()
    name = record.container_name
    timed_out = False
    exit_code: Optional[int] = None
    error = ""
    try:
        wait = _run_cli([docker, "wait", name], timeout + WAIT_GRACE_SECONDS)
        out = wait.stdout.decode("utf-8", errors="replace").strip()
        if out.lstrip("-").isdigit():
            exit_code = int(out)
        else:
            error = f"docker wait 输出无法解析: {out[:200]!r}"
    except subprocess.TimeoutExpired:
        timed_out = True
        error = f"沙箱执行超时被终止（>{timeout:.0f}秒，容器已强制回收）"
    except OSError as e:
        error = f"docker wait 调用失败: {e}"

    # 日志收集并落工作区（worker 侧按 stdout.log/stderr.log 文件口径解析）。
    # 落盘点是 executor 容器视角的 workspace_real——命名卷部署下 mount_host
    # 是宿主路径，本容器内不存在，日志必须写进自己挂得到的卷内路径
    try:
        logs = _run_cli([docker, "logs", name], LOGS_TIMEOUT_SECONDS)
        for fname, data in ((LOG_FILE_STDOUT, logs.stdout), (LOG_FILE_STDERR, logs.stderr)):
            try:
                (Path(record.workspace_real) / fname).write_bytes(data)
            except OSError as e:
                logger.warning(f"[sandbox:{record.run_id}] 工作区日志落盘失败 {fname}: {e}")
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"[sandbox:{record.run_id}] docker logs 收集失败: {e}")

    # 回收（幂等：正常退出/超时强杀都 rm -f）
    try:
        _run_cli([docker, "rm", "-f", name], RM_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"[sandbox:{record.run_id}] 容器回收失败（残留需运维清理）: {e}")

    with _runs_lock:
        record.exit_code = exit_code
        record.error = error
        record.finished_at = time.time()
        record.state = "timeout" if timed_out else ("failed" if error else "done")
    audit_logger.info(
        f"[sandbox:{record.run_id}] finished state={record.state} exit_code={exit_code} "
        f"参数={record.request}")


# ---------------- 接口 ----------------

@app.get("/health")
def health() -> Dict[str, str]:
    """存活探针（无鉴权；compose healthcheck 用）"""
    return {"status": "ok", "service": "executor"}


@app.post("/sandbox/run", status_code=202)
def sandbox_run(
    req: SandboxRunRequest,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """受理沙箱 run：白名单校验 → docker run -d → 202 {run_id}，后台监督取果

    白名单拒绝统一 400（字段层非法 422），拒绝即审计 WARNING，不触碰 docker CLI。
    """
    _require_token(authorization)

    # ---- 参数白名单（全部在 sandbox_params——与 worker 通道同一事实源） ----
    try:
        image = sandbox_params.validate_runner_image(req.image)
        # 工作区：executor 容器视角定位（realpath 校验）——日志落盘/尾部读取按它寻址
        ws_real = sandbox_params.resolve_workspace_mount(req.workspace)
        # bind 源：docker daemon 按宿主路径解析 -v 左值（命名卷部署经
        # SANDBOX_WORKSPACE_HOST_ROOT 映射；未配置 = workspace 本身，行为不变）
        mount_host = sandbox_params.resolve_mount_source(ws_real, req.mount_source)
        # 容器内工作目录白名单：须在 /workspace 子树内（缺省挂载根）；worker 的
        # per-script cwd 经此透传到 -w——harness 三件套/脚本 artifacts 按 cwd 寻址
        workdir_container = sandbox_params.resolve_container_workdir(req.workdir)
        network_name = sandbox_params.network_profile_to_docker(req.net_profile)
        # ARCH-3-4：运行时档白名单（runc|runsc），非法值 400 拒绝并审计
        runtime = sandbox_params.validate_container_runtime(req.runtime)
        entrypoint = _validate_entrypoint(req.entrypoint)
    except SandboxParamError as e:
        audit_logger.warning(f"[sandbox:reject] 原因={e} 参数={req.model_dump()}")
        raise HTTPException(status_code=400, detail=str(e))

    run_id = uuid.uuid4().hex
    container_name = f"aievo-sbx-{run_id[:16]}"
    args = [sandbox_params.docker_binary(), *sandbox_params.build_docker_run_args(
        image=image,
        user=sandbox_params.SANDBOX_USER,  # 固定 10001:10001（设计 D1，非请求参数）
        mount_host=mount_host,
        mount_container=sandbox_params.CONTAINER_WORKSPACE,
        workdir_container=workdir_container,  # 请求 workdir（白名单已校验）；缺省挂载根
        network_name=network_name,
        container_name=container_name,
        tail_command=entrypoint,
        runtime=runtime,   # ARCH-3-4：runc|runsc（gVisor 加固档），白名单已校验
        detach=True,       # 202 异步受理；退出码经 GET / docker wait 收口
        auto_rm=False,     # 退出后 docker logs 取日志，再显式 rm -f 回收
        interactive=False, # detached 无 stdin 载荷（payload 在工作区文件）
    )]

    record = RunRecord(
        run_id=run_id, container_name=container_name,
        request=req.model_dump(), mount_host=mount_host,
        workspace_real=ws_real,
        started_at=time.time(),
    )
    with _runs_lock:
        _runs[run_id] = record  # 先登记（含失败态）——500 后仍可 GET 查因

    try:
        proc = _run_cli(args, RUN_START_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        with _runs_lock:
            record.state = "failed"
            record.error = f"docker run 启动失败: {e}"
            record.finished_at = time.time()
        audit_logger.error(f"[sandbox:{run_id}] docker run 启动失败: {e} 参数={record.request}")
        raise HTTPException(status_code=500, detail=f"docker run failed: {e}")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()[:2000]
        with _runs_lock:
            record.state = "failed"
            record.error = f"docker run 退出码 {proc.returncode}: {stderr}"
            record.finished_at = time.time()
        audit_logger.error(
            f"[sandbox:{run_id}] docker run 拒绝 退出码={proc.returncode} "
            f"stderr={stderr} 参数={record.request}")
        raise HTTPException(status_code=500, detail=record.error)

    audit_logger.info(
        f"[sandbox:{run_id}] accepted 参数={record.request} "
        f"挂载={mount_host} 容器={container_name} 镜像={image}")
    background_tasks.add_task(_supervise_run, record, req.timeout)
    return {"run_id": run_id, "container_name": container_name, "state": record.state}


@app.get("/sandbox/runs/{run_id}")
def sandbox_run_status(
    run_id: str,
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """查询 run 状态：state/exit_code/输出尾部（全量日志在工作区文件）"""
    _require_token(authorization)
    with _runs_lock:
        record = _runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "run_id": record.run_id,
            "container_name": record.container_name,
            "state": record.state,
            "exit_code": record.exit_code,
            "error": record.error,
            "request": record.request,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            # 尾部读取同日志落盘：按 executor 容器视角工作区寻址（命名卷部署
            # 下 mount_host 是宿主路径，容器内不可读）
            "stdout_tail": _tail_bytes(Path(record.workspace_real) / LOG_FILE_STDOUT),
            "stderr_tail": _tail_bytes(Path(record.workspace_real) / LOG_FILE_STDERR),
        }


if __name__ == "__main__":  # 本机开发直跑（部署走 uvicorn CLI，见 Dockerfile.executor）
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8300)
