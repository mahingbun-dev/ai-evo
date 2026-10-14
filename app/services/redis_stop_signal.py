"""
Redis 停止信号检查器

用于跨进程的任务停止信号，替代 threading.Event。
另含评测任务级互斥锁（EvalTaskLock）：防 celery 孤儿消息重投导致
同一任务并发执行（缺陷B4，挂载于 celery_tasks.run_evaluation_task）。
"""
import logging
import os
import socket
from typing import Optional

import redis
from functools import lru_cache
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# TTL: 1 小时
STOP_SIGNAL_TTL = 3600

# 评测任务互斥锁 TTL：需覆盖单任务最长执行时限（celery task_time_limit=7200s），
# worker 崩溃（SIGKILL，锁无法主动释放）时由 TTL 兜底过期
EVAL_TASK_LOCK_TTL = 3 * 3600

# 评测任务互斥锁 key 前缀（EvalTaskLock 缺省）
EVAL_TASK_LOCK_PREFIX = "eval_task_lock"

# W10-2：接管残留锁的短 TTL。接管发生在「消息重投」时刻——接管方拿到锁后
# 由 run_evaluation_task 在任务真正开跑前 renew 续期到全量 TTL；若接管方拿锁
# 后自己也立刻崩溃，残留锁 15 分钟即可过期，不再叠一层 3 小时不可重跑。
EVAL_TASK_LOCK_TAKEOVER_TTL = 15 * 60

# W10-2 原子接管脚本：仅当锁值仍等于「已判死」的持锁者时删除并写入新锁
# （GET+比较+DEL+SET NX 在 Lua 内原子执行）。不能在客户端分步做
# get→delete→set——若持锁者恰好此刻正常释放、第三个 worker 抢到锁，
# 分步实现会误删他人锁造成同任务并发执行。
EVAL_TASK_LOCK_TAKEOVER_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3], 'NX')
end
return false
"""

# W10-2 续期脚本：仅当锁值仍是本 worker 标识时续期（不覆盖他人锁）
EVAL_TASK_LOCK_RENEW_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""

# hub 导入 job 互斥锁（W11）：key 前缀与 TTL。TTL 语义同评测锁——需覆盖单条
# job 最长执行时限（逐包下载解包落库，条数多时可达小时级；celery
# task_time_limit=7200s），worker 崩溃残留由 TTL 兜底过期
HUB_JOB_LOCK_PREFIX = "hubjob"
HUB_JOB_LOCK_TTL = 3 * 3600


@lru_cache
def get_redis_client() -> redis.Redis:
    """获取 Redis 客户端（带缓存）"""
    settings = get_settings()
    return redis.from_url(settings.REDIS_URL, decode_responses=True)


class RedisStopSignalChecker:
    """
    Redis 停止信号检查器

    接口兼容 threading.Event，使现有代码中的 stop_event.is_set() 调用无需修改。
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._key = f"task_stop:{task_id}"

    def is_set(self) -> bool:
        """检查停止信号是否已设置"""
        client = get_redis_client()
        return client.exists(self._key) > 0

    def set(self):
        """设置停止信号"""
        client = get_redis_client()
        client.set(self._key, "1", ex=STOP_SIGNAL_TTL)

    def cleanup(self):
        """清理停止信号"""
        client = get_redis_client()
        client.delete(self._key)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False


class EvalTaskLock:
    """
    任务级互斥锁（缺陷B4；W11 复用于 hub 导入 job）

    背景：celery 配置 task_acks_late=True + prefork，worker 进程崩溃（OOM/SIGKILL）
    后未 ACK 的消息会重投，同一任务可能在两个 worker 上并发执行——
    评测任务级 hub 镜像目录出现 rmtree vs hardlink 竞态，脚本误报匹配 0 文件；
    hub 导入 job 并发双导入则造成数据行重复、ref_count 双计。

    语义：SET NX EX 抢锁；抢不到说明已有 worker 在执行同一任务，调用方应
    直接拒绝本次执行（不排队重入）。锁值写入持锁 worker 标识（hostname:pid），
    便于排查。任务结束调用 release 释放；worker 崩溃残留的锁由 TTL 兜底过期。

    key_prefix 缺省 eval_task_lock:（评测任务）；hub 导入 job 传
    HUB_JOB_LOCK_PREFIX 得到 hubjob:{job_id}。

    W10-2 存活校验：抢锁失败时检查持锁进程是否存活——
    - 同机持锁者：os.kill(pid, 0) 信号探测（容器内即容器 /proc 视角）
    - 异机持锁者：celery inspect 检查该 worker 是否在线
    持锁进程已死（崩溃残留锁）则原子接管并缩短 TTL，消除「worker 崩溃后
    最长 3 小时不能重跑同一任务」；接管方任务开跑前由 renew 续期到全量 TTL。
    """

    def __init__(
        self,
        task_id: str,
        ttl: int = EVAL_TASK_LOCK_TTL,
        key_prefix: str = EVAL_TASK_LOCK_PREFIX,
    ):
        self.task_id = task_id
        self._key = f"{key_prefix}:{task_id}"
        self._ttl = ttl

    def acquire(self) -> bool:
        """抢锁（SET NX EX）：False 表示已有存活 worker 持有同任务锁

        W10-2：持锁进程已死（崩溃残留）时原子接管并写入短 TTL 锁，返回 True。
        """
        client = get_redis_client()
        holder = self._identity()
        if client.set(self._key, holder, nx=True, ex=self._ttl):
            return True
        return self._takeover_if_holder_dead(client, holder)

    def renew(self, ttl: Optional[int] = None) -> bool:
        """续期自己的锁（W10-2：接管短 TTL 场景，任务开跑前续期到全量 TTL）

        仅当锁值仍等于本 worker 标识时续期，不会覆盖他人锁。
        """
        client = get_redis_client()
        result = client.eval(
            EVAL_TASK_LOCK_RENEW_LUA, 1, self._key,
            self._identity(), str(ttl or self._ttl),
        )
        return bool(result)

    def release(self):
        """释放锁（任务结束调用；worker 崩溃未释放由 TTL 过期兜底）"""
        client = get_redis_client()
        client.delete(self._key)

    def holder(self) -> Optional[str]:
        """查询当前持锁 worker 标识（无锁/异常返回 None，仅排查用）"""
        client = get_redis_client()
        value = client.get(self._key)
        return value if isinstance(value, str) else None

    # ---------- W10-2：持锁者存活校验与接管 ----------

    @staticmethod
    def _identity() -> str:
        """本 worker 锁标识（hostname:pid）"""
        return f"{socket.gethostname()}:{os.getpid()}"

    def _takeover_if_holder_dead(self, client, holder: str) -> bool:
        """持锁进程已死则原子接管（写入短 TTL 新锁）；否则维持拒绝语义"""
        try:
            current = client.get(self._key)
        except Exception as e:  # noqa: BLE001 - 查询失败按「他人持有」保守处理
            logger.warning(f"任务 {self.task_id} 持锁者查询失败（保持拒绝）: {e}")
            return False
        if not current or not self._holder_dead(current):
            return False
        try:
            result = client.eval(
                EVAL_TASK_LOCK_TAKEOVER_LUA, 1, self._key,
                current, holder, str(EVAL_TASK_LOCK_TAKEOVER_TTL),
            )
        except Exception as e:  # noqa: BLE001 - 接管失败保持拒绝（TTL 兜底）
            logger.warning(f"任务 {self.task_id} 锁接管执行失败（保持拒绝）: {e}")
            return False
        if result:
            logger.warning(
                f"任务 {self.task_id} 持锁进程已死（原持锁者: {current}），"
                f"接管互斥锁（短 TTL {EVAL_TASK_LOCK_TAKEOVER_TTL}s，任务开跑前续期）"
            )
            return True
        return False

    @staticmethod
    def _holder_dead(holder_value: str) -> bool:
        """持锁者存活判定。锁值格式 hostname:pid，返回 True 表示已死可接管。

        无法解析的锁值一律按存活处理（不接管，保守）。
        """
        hostname, sep, pid_part = str(holder_value).rpartition(":")
        if not sep or not hostname:
            return False
        try:
            pid = int(pid_part)
        except ValueError:
            return False
        if pid <= 0:
            return False
        try:
            if hostname == socket.gethostname():
                return not _local_pid_alive(pid)
            return not _remote_hostname_alive(hostname)
        except Exception:  # noqa: BLE001 - 探测异常保守按存活（不接管，TTL 兜底）
            return False


def _local_pid_alive(pid: int) -> bool:
    """本机进程存活探测（os.kill(pid, 0)：不杀进程，仅检查存在性/权限）"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属其它用户
    except OSError:
        return True  # 保守：探测异常按存活
    return True


def _remote_hostname_alive(hostname: str) -> bool:
    """异机持锁 worker 在线检查（celery inspect ping）；探测异常按存活保守处理"""
    try:
        from app.celery_app import celery_app  # 局部导入避免循环依赖

        replies = celery_app.control.inspect(timeout=1.0).ping() or {}
        return any(isinstance(reply, dict) and hostname in reply for reply in replies)
    except Exception:  # noqa: BLE001 - inspect 不可用时按存活（TTL 兜底语义不变）
        return True
