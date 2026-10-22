"""
Celery 应用配置

使用 Redis 作为消息代理和结果后端。
"""
import logging
import os
import time
from typing import Dict, List, Optional

from celery import Celery, signals
from kombu import Exchange, Queue
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)
from prometheus_client import multiprocess as _metrics_multiprocess

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# 创建 Celery 实例
celery_app = Celery(
    "ai_eval",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

# ---------------------------------------------------------------------------
# ARCH-2-1：队列拆分（设计 §2.3 决策 D4 / §3 Phase 2 表 2-1 行）
#
# 评测（eval）/ hub 导入（ingest）/ 维护（maint）三队列按任务类型隔离消费，
# 消除单队列互相争抢（hub 导入高峰不再拖慢评测）与单 worker SPOF。
#   - 三队列各自独立同名 exchange/routing_key（direct 直连），不复用缺省
#     exchange——两个 Queue 若绑到同一 routing_key，direct 交换机会把同一条
#     消息投进多个队列（重复消费）；
#   - 路由表（TASK_ROUTES）是队列归属的单一事实源：新增任务必须在此登记；
#   - 维护任务统一约定落在 app.tasks.maintenance.* 命名空间（glob 自动路由
#     maint）。M1 QE1-3 起 webhook 投递任务（webhook_delivery.deliver_webhook）
#     落该命名空间；消费者为 compose 的 worker-maint 服务。
# --------------------------------------------------------------------------
QUEUE_EVAL = "eval"
QUEUE_INGEST = "ingest"
QUEUE_MAINT = "maint"
TASK_QUEUE_NAMES = (QUEUE_EVAL, QUEUE_INGEST, QUEUE_MAINT)

TASK_ROUTES = {
    "app.tasks.celery_tasks.run_evaluation_task": {"queue": QUEUE_EVAL},
    "app.tasks.celery_tasks.run_hub_import_job": {"queue": QUEUE_INGEST},
    "app.tasks.maintenance.*": {"queue": QUEUE_MAINT},
}

# ---------------------------------------------------------------------------
# ARCH-2-2：eval 队列内任务优先级（设计 §2.3 决策 D4「优先级路由：人工触发
# 的单任务调试 > 批量任务」/ §3 Phase 2 表 2-2 行）
#
# 选型：celery 原生消息 priority（同一 eval 队列内分档），不另开独立高优
# 队列——后者要新增队列名 + 改 compose 的 CELERY_WORKER_QUEUES + 多一个
# worker 消费面，且 kombu 多队列消费是公平轮转（_queue_cycle）而非严格
# 优先，语义反而更弱；priority 机制对部署面零改动。
#
# ⚠ 数值方向是「按本栈实测行为反转」的映射，与 celery 文档语义（9=最高）
# 相反，升级 kombu 前必读：kombu 5.6.2（requirements.lock 钉死）的 Redis
# transport 把 priority=N 的消息落进 <queue>\x06\x16<step> 子列表，而消费
# 侧 BRPOP 按 priority_steps 升序下发 keys（MONITOR 实测：BRPOP q q3 q6
# q9），Redis 多 key 从左到右取第一个非空（redis.io 文档）→ 数值小的 step
# 先被消费，即 celery 语义的 priority=9 实际排在最后（kombu#1178 记录的
# BRPOP 多列表方案固有缺陷方向）。2026-10-15 在 dev Redis 7.4 端到端实测
# （发送 priority=0/9 各一条、kombu Consumer 消费）：消费顺序 [0, 9]。
# 因此本平台提交侧取反：人工调试=0（落 base key，先消费）、批量=9。
# 若升级 kombu 后其修正了 BRPOP key 顺序（高 step 先服务），本组常量必须
# 同步翻转——tests/unit/test_team_concurrency_quota.py 钉住了当前取值。
# --------------------------------------------------------------------------
PRIORITY_HIGH = 0    # 人工触发的单任务调试：插队于积压的批量任务之前
PRIORITY_NORMAL = 9  # 批量任务（挂到最低消费档；worker 空闲即消化）


def _task_queues(names):
    """按名字构建队列声明：exchange/routing_key 与队列同名（一队列一交换机）"""
    return [Queue(name, Exchange(name), routing_key=name) for name in names]


def _resolve_task_queues():
    """解析本进程的队列声明全集（task_queues）。

    worker 的消费队列绑定不走 -Q 命令行（docker-entrypoint.sh 的 worker 分支
    固定 celery 命令、不接额外参数），改由配置面表达：celery worker 未指定
    -Q 时消费 task_queues 定义的全集（celery/worker/worker.py setup_queues：
    include 为空则不做 select，amqp.queues 全集即消费集）。因此：

    - CELERY_WORKER_QUEUES（逗号分隔）注入的容器（compose 的 worker /
      worker-ingest）只声明自己的队列 → 只消费自己的队列；
    - 未注入的容器（api 等生产者、本地开发）声明全量三队列——生产者只投递
      不消费，全量表用于按 TASK_ROUTES 正确解析每个队列的绑定；
    - 注入了但值非法（拼错队列名/空串）直接抛错 fail-fast：宁可不启动，
      不静默消费错队列或全量队列。

    返回值只影响本进程：进程间互不传染。
    """
    raw = os.environ.get("CELERY_WORKER_QUEUES")
    if raw is None:
        return _task_queues(TASK_QUEUE_NAMES)
    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in TASK_QUEUE_NAMES]
    if not names or unknown:
        raise RuntimeError(
            f"CELERY_WORKER_QUEUES 配置非法: {raw!r}，"
            f"允许值: {','.join(TASK_QUEUE_NAMES)}（逗号分隔，至少一个）"
        )
    return _task_queues(names)


# Celery 配置
celery_app.conf.update(
    # 序列化配置
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # W10-1：Redis broker 可见性超时必须 ≥ task_time_limit（7200s）。
    # 缺省 visibility_timeout 只有 1 小时——acks_late 长任务执行超过 1 小时后，
    # 消息会被 broker 视为「未消费」重新投递，同任务在两个 worker 并发执行。
    # 显式 7800s（比 task_time_limit 富余 10 分钟，覆盖 ack 抖动窗口）。
    # ARCH-2-2：priority_steps 显式声明（= kombu 缺省值）——Redis transport
    # 按这些档位把队列拆成子列表实现「同队列内」的优先级（跨队列隔离由
    # 2-1 队列拆分保证）。档位消费顺序与常量取值见上方 ARCH-2-2 注释块
    # （本栈实测高 step 后消费，PRIORITY_HIGH=0 / NORMAL=9）。
    broker_transport_options={
        "visibility_timeout": 7800,
        "priority_steps": [0, 3, 6, 9],
    },

    # Worker 配置
    worker_pool="prefork",               # 多进程模式
    worker_concurrency=4,                # 并发 worker 数

    # 任务确认配置
    task_acks_late=True,                 # 任务完成后再确认
    worker_prefetch_multiplier=1,        # 每次只取一个任务

    # 任务跟踪配置
    task_track_started=True,             # 跟踪任务启动状态
    task_time_limit=7200,                # 单任务最长 2 小时
    task_soft_time_limit=6600,           # 软超时 110 分钟

    # ARCH-2-1：队列声明/路由/兜底队列（拆分说明见文件头 ARCH-2-1 注释块）。
    # task_queues 决定本进程的队列绑定全集：worker 未指定 -Q 时消费其全集，
    # 故 worker 容器经 CELERY_WORKER_QUEUES 把全集收敛为自己的队列。
    task_queues=_resolve_task_queues(),
    task_routes=TASK_ROUTES,
    # 兜底队列：未在 TASK_ROUTES 登记的任务名落 eval——与拆分前「单 worker
    # 全消费」的存活语义等价；若落回 celery 缺省队列，拆分后将无任何 worker
    # 消费而静默积压。
    task_default_queue=QUEUE_EVAL,

    # 结果配置
    result_expires=3600,                 # 结果过期时间 1 小时

    # 时区配置
    timezone="Asia/Shanghai",
    enable_utc=True,
)

# 自动发现任务模块
celery_app.autodiscover_tasks(["app.tasks"])

# ---------------------------------------------------------------------------
# ARCH-2-3：Prometheus 指标埋点（设计 §2.3 决策 D7 / §3 Phase 2 表 2-3 行）
#
# 指标暴露拓扑（单机 compose，按 D7 最小可用面落地，不引 pushgateway）：
#   - api 进程：/metrics（app/main.py）暴露本进程默认 registry——队列深度
#     gauge 由 lifespan 周期任务 Redis llen 刷新（api 是该 gauge 的唯一写者，
#     见 update_queue_depth_metrics）；
#   - worker 进程：PROMETHEUS_WORKER_METRICS_PORT 已配置时经 worker_ready
#     信号起独立指标 server（仅 compose 内网抓取，不发布宿主端口）。任务
#     时长/失败指标在 prefork 子进程里埋点（task_prerun/on_success/on_failure
#     都在执行进程内派发），靠 PROMETHEUS_MULTIPROC_DIR 的 per-pid mmap
#     文件聚合到父进程 server——prometheus_client 的 MmapedValue 检测 fork
#     后按子进程自己的 pid 重建文件句柄（0.26.0 values.py __check_for_pid_
#     change，本机源码核验），跨进程无写竞争，即 gunicorn preload 同款官方
#     模式；
#   - 未配 MULTIPROC_DIR 时 worker server 只能看到父进程内存里的指标
#     （prefork 子进程的埋点不可见），仅 --pool=solo 场景有意义——compose
#     的 worker 已统一注入 MULTIPROC_DIR（tmpfs 承载，容器重建清零；Counter
#     从 0 重计不影响 Prometheus rate/increase 语义）。
#
# 指标命名统一 aievo_ 前缀；label 只用低基数维度（queue ∈ 三队列名）。
# docker/monitoring/（prometheus.yml/alert.rules）与 compose monitoring
# profile 是本节的编排面；指标名改动必须同步 alert.rules（单测钉同步）。
# --------------------------------------------------------------------------
TASK_DURATION_METRIC = Histogram(
    "aievo_celery_task_duration_seconds",
    "Celery 任务执行时长（task_prerun→终态，成功与失败都观察；秒）",
    ["queue"],
    # 桶覆盖秒级导入到 task_time_limit=7200 的两小时评测全程
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200),
)
TASK_FAILURES_METRIC = Counter(
    "aievo_celery_task_failures_total",
    "Celery 任务失败数（task_failure 信号；Retry 走 task_retry 不计入）",
    ["queue"],
)
QUEUE_DEPTH_METRIC = Gauge(
    "aievo_celery_queue_depth",
    "Redis 队列深度（llen 求和，含 kombu priority 子列表；api 进程周期刷新）",
    ["queue"],
)


def _ensure_multiproc_dir() -> None:
    """PROMETHEUS_MULTIPROC_DIR 不存在时创建（prometheus_client 只写文件不建目录）

    在 worker 父进程 import 期执行，早于 prefork 子进程 fork 与任何指标写入；
    api 进程不注入该 env（走默认 registry），此处零动作。
    """
    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if directory:
        os.makedirs(directory, exist_ok=True)


_ensure_multiproc_dir()


# task_id -> time.monotonic()：prerun 记起点，终态（成功/失败）弹出观察。
# 子进程被 SIGKILL 强杀（超时兜底）时该条残留，随子进程回收消亡，量级可忽略。
_task_started_at: Dict[str, float] = {}


def _signal_queue(task: object) -> str:
    """从任务请求上下文取队列名（delivery_info.routing_key）；取不到落 unknown"""
    try:
        routing_key = (getattr(task, "request", None).delivery_info or {}).get("routing_key")
    except AttributeError:
        routing_key = None
    return routing_key or "unknown"


@signals.task_prerun.connect
def _metrics_on_task_prerun(task_id=None, task=None, **_kwargs):
    """任务开始执行：记时长起点（信号在执行进程内派发，prefork 即子进程）"""
    _task_started_at[task_id] = time.monotonic()


@signals.task_success.connect
def _metrics_on_task_success(sender=None, result=None, **_kwargs):
    """任务成功：观察执行时长（此时 request 上下文仍存活，sender.request.id 可用）"""
    task_id = getattr(getattr(sender, "request", None), "id", None)
    started = _task_started_at.pop(task_id, None)
    if started is not None:
        TASK_DURATION_METRIC.labels(queue=_signal_queue(sender)).observe(
            time.monotonic() - started)


@signals.task_failure.connect
def _metrics_on_task_failure(sender=None, task_id=None, exception=None, **_kwargs):
    """任务失败：失败计数 + 失败时长（celery/app/trace.py：Retry 走 task_retry
    信号不经此处，重投后按新执行重新计时）"""
    started = _task_started_at.pop(task_id, None)
    queue = _signal_queue(sender)
    if started is not None:
        TASK_DURATION_METRIC.labels(queue=queue).observe(time.monotonic() - started)
    TASK_FAILURES_METRIC.labels(queue=queue).inc()


# kombu Redis transport 的优先级子列表分隔符（kombu/transport/redis.py 的
# Channel.sep 缺省值）：priority 非 0 档的消息 LPUSH 到 "<queue>\x06\x16<pri>"，
# 队列真实深度必须把 base key 与各非零档位子 key 的 llen 求和（ARCH-2-2
# 拆档背景，priority_steps 见上方 broker_transport_options）
_QUEUE_PRIORITY_SEP = "\x06\x16"


def queue_depth_keys(queue: str) -> List[str]:
    """某队列在 Redis broker 里的全部 list key（base key + 非零档位子列表）"""
    steps = celery_app.conf.broker_transport_options.get("priority_steps", [])
    return [queue] + [f"{queue}{_QUEUE_PRIORITY_SEP}{step}" for step in steps if step]


def update_queue_depth_metrics(redis_client=None) -> Dict[str, int]:
    """Redis llen 刷新队列深度 gauge，返回 {queue: depth}（ARCH-2-3）

    由 api 进程周期调用（app/main.py lifespan，gauge 单写者）；Redis 异常
    向上抛、由调用方兜底——指标宁可陈旧不可打断主流程。深度语义 = 等待
    消费的消息数（BRPOP 已弹出未 ACK 的执行中任务不计入，与「队列积压」
    告警口径一致）。
    """
    from app.services.redis_stop_signal import get_redis_client
    client = redis_client or get_redis_client()
    depths: Dict[str, int] = {}
    for queue in TASK_QUEUE_NAMES:
        depth = sum(int(client.llen(key)) for key in queue_depth_keys(queue))
        depths[queue] = depth
        QUEUE_DEPTH_METRIC.labels(queue=queue).set(depth)
    return depths


def start_worker_metrics_server() -> Optional[int]:
    """worker 进程内启动 Prometheus 指标 server（worker_ready 信号调用）

    - PROMETHEUS_WORKER_METRICS_PORT 未配置 → 返回 None 不启动（本地开发
      零开销；compose worker 由 ARCH-2-3 接线统一注入）；
    - 已配 PROMETHEUS_MULTIPROC_DIR → 多进程聚合 registry（拓扑见文件头
      ARCH-2-3 注释块）；未配 → 默认 registry（prefork 下任务指标不可见，
      仅 --pool=solo 形态有意义）。
    指标面故障绝不影响任务执行：worker_ready 钩子整体兜底（见下）。
    """
    port_raw = (os.environ.get("PROMETHEUS_WORKER_METRICS_PORT") or "").strip()
    if not port_raw:
        return None
    port = int(port_raw)
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        _metrics_multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY
    start_http_server(port, registry=registry)
    logger.info(f"Prometheus worker 指标 server 已启动: :{port}")
    return port


@signals.worker_ready.connect
def _metrics_on_worker_ready(**_kwargs):
    """worker 启动完成（父进程；prefork 子进程 fork 先于此，子进程指标经
    MULTIPROC_DIR 文件聚合，见文件头拓扑注释）"""
    try:
        start_worker_metrics_server()
    except Exception:
        logger.exception("Prometheus worker 指标 server 启动失败（不影响任务执行）")
