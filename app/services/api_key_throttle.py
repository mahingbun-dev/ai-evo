"""
API Key 认证失败节流（M1 QE1-1，方案 §M1：「按 IP+prefix 计数，
5 次/10 分钟锁 15 分钟」）

两层计数（均 Redis，key 风格与 team_quota:/task_stop: 一致）：

1. 精确层（方案指定）：``apikey_fail:cnt:{ip}:{prefix}``——同一 IP 对同一
   key 前缀的定向爆破（已知前缀猜随机段）5 次/10 分钟 → 锁
   ``apikey_fail:lock:{ip}:{prefix}`` 15 分钟；
2. IP 层（爆破加固）：``apikey_fail:ipcnt:{ip}``——纯前缀计数对「每次换
   随机前缀的盲扫」天然失效（每个 prefix 计数永远 1），补同一 IP 全局
   30 次/10 分钟 → 锁 ``apikey_fail:iplock:{ip}`` 15 分钟。

- IP 取直连对端地址（request.client.host），**不信任 X-Forwarded-For**
  （可伪造——采信 XFF 等于让攻击者每次换 IP 绕过计数）；
- 故障语义 fail-open：Redis 不可用时记 warning 放行（认证正确性由
  key_hash 比对保证，节流只是反爆破护栏；fail-closed 会让 Redis 抖动
  打挂全部 API Key 认证，代价不成比例）；
- 锁与计数均带 TTL 自动回收，无后台清理任务。
"""
import logging
from typing import Optional

from app.services.redis_stop_signal import get_redis_client

logger = logging.getLogger(__name__)

KEY_PREFIX = "apikey_fail"

# 精确层（IP+prefix）：5 次 / 10 分钟 → 锁 15 分钟（方案指定值）
PREFIX_FAILURE_LIMIT = 5
FAILURE_WINDOW_SECONDS = 600
LOCK_SECONDS = 900

# IP 层（盲扫加固）：30 次 / 10 分钟 → 锁 15 分钟
IP_FAILURE_LIMIT = 30


class ApiKeyAuthThrottle:
    """API Key 认证失败节流器（Redis 计数 + 锁）

    redis_client 可注入（单测传 fakeredis / dict 假体）；缺省复用全局
    Redis 客户端（get_redis_client，与停止信号/互斥锁/团队配额同一连接口径）。
    """

    def __init__(self, redis_client: Optional[object] = None):
        self._client = redis_client if redis_client is not None else get_redis_client()

    # ---- key 构造 ----

    @staticmethod
    def _cnt_key(ip: str, prefix: str) -> str:
        return f"{KEY_PREFIX}:cnt:{ip}:{prefix}"

    @staticmethod
    def _lock_key(ip: str, prefix: str) -> str:
        return f"{KEY_PREFIX}:lock:{ip}:{prefix}"

    @staticmethod
    def _ip_cnt_key(ip: str) -> str:
        return f"{KEY_PREFIX}:ipcnt:{ip}"

    @staticmethod
    def _ip_lock_key(ip: str) -> str:
        return f"{KEY_PREFIX}:iplock:{ip}"

    # ---- 查询 ----

    def is_locked(self, ip: str, prefix: str) -> bool:
        """该 (IP, prefix) 是否处于锁定期（IP 层锁同样拦住——盲扫锁 IP）"""
        try:
            return bool(
                self._client.exists(self._lock_key(ip, prefix))
                or self._client.exists(self._ip_lock_key(ip))
            )
        except Exception as exc:  # fail-open：Redis 故障不阻断认证
            logger.warning("[APIKEY-THROTTLE] Redis 不可用，节流检查放行：%s", exc)
            return False

    # ---- 计数 ----

    def record_failure(self, ip: str, prefix: str) -> None:
        """记一次认证失败；计数触阈即落锁并清计数"""
        try:
            self._record(self._cnt_key(ip, prefix), self._lock_key(ip, prefix),
                         PREFIX_FAILURE_LIMIT)
            self._record(self._ip_cnt_key(ip), self._ip_lock_key(ip),
                         IP_FAILURE_LIMIT)
        except Exception as exc:  # fail-open
            logger.warning("[APIKEY-THROTTLE] Redis 不可用，失败计数跳过：%s", exc)

    def _record(self, cnt_key: str, lock_key: str, limit: int) -> None:
        count = self._client.incr(cnt_key)
        if count == 1:
            # 首次失败起算窗口（固定窗口；重复失败不续期，窗口滚动由新 key 承担）
            self._client.expire(cnt_key, FAILURE_WINDOW_SECONDS)
        if count >= limit:
            # 落锁 + 清计数：锁过期后窗口重新起算
            self._client.set(lock_key, "1", ex=LOCK_SECONDS)
            self._client.delete(cnt_key)
            logger.warning(
                "[APIKEY-THROTTLE] 认证失败达 %d 次，锁定 %s %d 秒", limit, lock_key, LOCK_SECONDS
            )
