"""executor 沙箱服务 HTTP 客户端 + HttpExecutorDriver 驱动（ARCH-1-2）

设计（plans/2026-10-15-企业级评测系统架构设计.md §2.2 拓扑 / §2.3 D1 / §2.4
时序 / §3 表 1-2 行）：worker 与 executor 之间是 compose 内网 + token 的 HTTP
调用。本模块实现 SandboxDriver 接口（定义在 app/services/script_container.py，
设计「远期 K8s 驱动」预留扩展点）的 HTTP 侧：

- ``run``    stdin 载荷落盘工作区 → entrypoint 包一层 shell 重定向（executor
             detached 受理无 stdin 通道，与 CLI 通道 ``-i`` 管道语义等价）→
             请求体带 ``workdir``（spec.workdir_container，per-script cwd——与
             CLI 通道的 ``-w`` 同值，harness 三件套/脚本 artifacts 按 cwd 寻址）→
             ``POST /sandbox/run`` → 202 {run_id}；
- ``wait``   轮询 ``GET /sandbox/runs/{id}`` 至终态（done|timeout|failed），
             客户端死线 = 脚本 timeout + WAIT_MARGIN_SECONDS 兜底（超时强制
             收口本来就在服务端监督线程：docker wait 超时 → rm -f，见
             executor/app.py._supervise_run）；终态后把 executor 落在工作区根的
             docker logs 文件归位到脚本 cwd（编排层统一按 cwd 解析）；
- ``kill``   1-1 executor 契约无远端 kill 端点——显式 no-op 并告警留痕（服务端
             监督是超时收口的执行点；executor 增加 DELETE 端点时补真实终止）；
- ``status`` 单次 GET 非阻塞查询（纯读）。

错误传递（白名单拒绝/网络不可达/未登记 run）统一抛 SandboxDriverError，
由 execute_script_in_container 转脚本 fail 条目——不静默降级、不重试。

配置（ARCH-1-2）：EXECUTOR_URL / EXECUTOR_TOKEN（环境变量实时读优先于
Settings，与 get_script_exec_mode 同语义；token 未配置即 fail-closed 不发起
调用，与 executor 侧 503 语义对齐）。网络档位三档映射：档位值原样进请求
net_profile 字段，白名单校验的单一事实源在 executor/sandbox_params.NET_PROFILES
（none|egress；open 档未放开——请求即拒，错误透传）。

单测替换点：``_http_open`` 是唯一网络 I/O 出口（tests/unit/test_sandbox_driver.py
以 mock executor 替换，覆盖三档映射/错误传递/超时），不经真实网络。
"""
import json
import logging
import os
import shlex
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.config import get_settings
from app.services.script_container import (
    SandboxDriver,
    SandboxDriverError,
    SandboxHandle,
    SandboxRunSpec,
    SandboxRunStatus,
)

logger = logging.getLogger(__name__)

# POST /sandbox/run 客户端超时：executor 侧 docker run -d 启动上限 30s
# （executor/app.py.RUN_START_TIMEOUT_SECONDS）+ 余量
POST_TIMEOUT_SECONDS = 60.0
# GET 状态查询客户端超时（服务端为内存表查询，毫秒级）
GET_TIMEOUT_SECONDS = 10.0
# 终态轮询间隔（单测可 monkeypatch 收紧）
POLL_INTERVAL_SECONDS = 0.5
# 客户端轮询死线 = 脚本 timeout + 本余量：覆盖服务端超时宽限（executor 侧
# WAIT_GRACE_SECONDS=5s）+ docker logs/rm 收集 + 轮询间隔抖动。死线只兜底
# 「executor 卡死/失联」——正常超时收口由服务端监督线程完成
WAIT_MARGIN_SECONDS = 30.0
# stdin 载荷落盘文件名前缀（工作区根 = 挂载面；随工作区评完即清）
STDIN_FILE_PREFIX = "stdin-"
# executor 监督线程写进工作区根的日志文件名（与 executor/app.py 常量同名同源）
LOG_FILE_STDOUT = "stdout.log"
LOG_FILE_STDERR = "stderr.log"


# ---------------- 配置读取（环境变量实时优先） ----------------

def executor_url() -> str:
    """executor 服务地址（EXECUTOR_URL；环境变量实时读优先于 Settings）"""
    raw = (os.environ.get("EXECUTOR_URL") or "").strip()
    if raw:
        return raw
    return (get_settings().EXECUTOR_URL or "").strip()


def executor_token() -> str:
    """executor 调用 token（EXECUTOR_TOKEN；读取优先级同 executor_url）"""
    raw = (os.environ.get("EXECUTOR_TOKEN") or "").strip()
    if raw:
        return raw
    return (get_settings().EXECUTOR_TOKEN or "").strip()


# ---------------- HTTP 传输（唯一 I/O 出口） ----------------

def _http_open(req: urllib.request.Request, timeout: float) -> Tuple[int, bytes]:
    """执行一次 HTTP 请求，返回 (状态码, 响应体)——本模块唯一网络 I/O 出口

    opener 显式挂空 ProxyHandler：executor 是 compose 内网直连，绝不经任何
    http_proxy/https_proxy 环境变量绕行（worker 出网代理属 Phase 1-3 egress
    通道，与沙箱控制面调用无关）。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _executor_request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]],
    timeout: float,
) -> Dict[str, Any]:
    """executor HTTP 调用统一出口（组包 + 经 _http_open 收发 + 错误归一）

    - 2xx → 解析 JSON 返回（空体按 {} 容错）；
    - 4xx/5xx → SandboxDriverError（HTTPError 体里的 executor detail 透传——
      白名单拒绝原因/未登记 run 等运维定位信息不丢失）；
    - URLError/OSError/超时 → SandboxDriverError（不可达）。
    """
    url = f"{executor_url().rstrip('/')}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {executor_token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        _, raw = _http_open(req, timeout)
        return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            parsed = json.loads(e.read().decode("utf-8"))
            detail = str(parsed.get("detail") or "")
        except Exception:  # noqa: BLE001 - detail 解析失败退回 reason
            pass
        raise SandboxDriverError(f"executor 拒绝（HTTP {e.code}）: {detail or e.reason}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise SandboxDriverError(f"executor 不可达（{executor_url()}）: {e}") from e


# ---------------- HTTP executor 驱动（SandboxDriver 实现） ----------------

class HttpExecutorDriver(SandboxDriver):
    """HTTP→executor 驱动：container 模式目标通道（ARCH-1-2，设计 §2.3 D1）

    沙箱生命周期全部委托 executor（compose 内唯一挂 docker.sock 的角色）；
    本驱动不做白名单校验的重复实现——请求字段即白名单面，校验单一事实源在
    executor/sandbox_params（open 档等未放开档位请求即拒，错误原样透传）。
    timeout 上限（3600s）同理由 executor 请求模型约束（422 透传），客户端
    不重复设卡。
    """
    name = "http-executor"

    def run(self, spec: SandboxRunSpec) -> SandboxHandle:
        if not executor_url():
            raise SandboxDriverError(
                "EXECUTOR_URL 未配置：http-executor 驱动不可用（get_sandbox_driver 不应选到本驱动）")
        if not executor_token():
            raise SandboxDriverError(
                "EXECUTOR_TOKEN 未配置：fail-closed，拒绝发起 executor 沙箱请求")
        # stdin 载荷落盘工作区（挂载面内，随工作区清理）；容器内 shell 重定向
        # 进 wrapper——与 CLI 通道经 docker CLI -i 管道传 stdin 语义等价
        stdin_name = f"{STDIN_FILE_PREFIX}{spec.container_name}.json"
        stdin_host = Path(spec.workspace_host) / stdin_name
        stdin_container = f"{spec.workspace_container}/{stdin_name}"
        try:
            stdin_host.write_bytes(spec.stdin_payload)
        except OSError as e:
            raise SandboxDriverError(f"stdin 载荷落盘工作区失败: {e}")
        shell_command = (
            " ".join(shlex.quote(part) for part in spec.entrypoint)
            + f" < {shlex.quote(stdin_container)}"
        )
        body = {
            "image": spec.image,
            "workspace": spec.workspace_host,
            # 容器内工作目录（per-script cwd，/workspace/{script_id}）：harness 通道
            # entrypoint 以裸相对文件名引用 staged 三件套、脚本通道 artifacts 按
            # cwd 落盘，都依赖 -w 指向 cwd——executor 侧按 /workspace 子树白名单
            # 校验（sandbox_params.resolve_container_workdir），越界 400 透传
            "workdir": spec.workdir_container,
            "net_profile": spec.net_profile,
            "timeout": spec.timeout,
            "entrypoint": ["sh", "-c", shell_command],
        }
        # bind 源（命名卷部署，SANDBOX_WORKSPACE_HOST_ROOT）：仅在派生出
        # mount_source 时携带——executor 按宿主根白名单校验；未配置形态不带
        # 字段，缺省行为与既有调用方逐字节一致（workspace 本身即 bind 源）
        if spec.mount_source:
            body["mount_source"] = spec.mount_source
        resp = _executor_request("POST", "/sandbox/run", body, POST_TIMEOUT_SECONDS)
        run_id = str(resp.get("run_id") or "")
        if not run_id:
            raise SandboxDriverError(f"executor 受理响应缺少 run_id: {resp!r}")
        return SandboxHandle(
            run_id=run_id,
            container_name=str(resp.get("container_name") or ""),
            extra={"workspace_host": spec.workspace_host, "cwd_host": spec.cwd_host},
        )

    def wait(self, handle: SandboxHandle, timeout: float) -> SandboxRunStatus:
        deadline = time.monotonic() + timeout + WAIT_MARGIN_SECONDS
        while True:
            status = self.status(handle)
            if status.state != "running":
                self._relocate_logs(handle, status.state)
                return status
            if time.monotonic() >= deadline:
                return SandboxRunStatus(
                    state="timeout",
                    error=(f"executor 状态轮询超过客户端死线"
                           f"（timeout {timeout:.0f}s + 余量 {WAIT_MARGIN_SECONDS:.0f}s），"
                           "按超时处理；服务端监督线程仍会强制回收容器"),
                )
            time.sleep(POLL_INTERVAL_SECONDS)

    def kill(self, handle: SandboxHandle) -> None:
        """远端终止（1-1 executor 契约无 kill 端点）：显式 no-op 并告警留痕

        超时强制收口的执行点是 executor 服务端监督线程（docker wait 超时 →
        docker rm -f，设计 §2.4），客户端无 docker.sock 无从代劳；executor
        增加 DELETE /sandbox/runs/{id} 端点时在此补真实终止。
        """
        logger.warning(
            f"[sandbox:{handle.run_id}] http-executor 驱动无远端 kill 端点："
            "超时收口由 executor 服务端监督线程负责（设计 §2.4）")

    def status(self, handle: SandboxHandle) -> SandboxRunStatus:
        resp = _executor_request(
            "GET", f"/sandbox/runs/{handle.run_id}", None, GET_TIMEOUT_SECONDS)
        return SandboxRunStatus(
            state=str(resp.get("state") or "running"),
            exit_code=resp.get("exit_code"),
            error=str(resp.get("error") or ""),
        )

    def _relocate_logs(self, handle: SandboxHandle, state: str) -> None:
        """executor 落在工作区根的 docker logs 归位到脚本 cwd（编排统一解析点）

        executor 监督线程把 docker logs 写到挂载根 {workspace}/stdout.log|
        stderr.log（executor/app.py._supervise_run），而编排层与 local/CLI
        通道同口径读 {cwd}/stdout.log——wait 收口时统一挪位。编排串行
        （run_pre_scripts 逐脚本执行），同工作区多脚本不会交叉覆盖。
        done 态日志缺失告警（典型成因：worker/executor 卷挂载不一致）。
        """
        workspace = Path(handle.extra["workspace_host"])
        cwd = Path(handle.extra["cwd_host"])
        for fname in (LOG_FILE_STDOUT, LOG_FILE_STDERR):
            src = workspace / fname
            if not src.exists():
                if state == "done":
                    logger.warning(
                        f"[sandbox:{handle.run_id}] 终态 done 但工作区缺 {fname}"
                        f"（检查 worker/executor 的 uploads 卷挂载是否一致）")
                continue
            try:
                os.replace(src, cwd / fname)
            except OSError as e:
                logger.warning(f"[sandbox:{handle.run_id}] 工作区日志 {fname} 归位失败: {e}")
