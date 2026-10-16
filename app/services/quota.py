"""
Team 级评测并发配额（ARCH-2-2，设计 §2.3 决策 D4 / §3 Phase 2 表 2-2 行）

Redis 计数实现 team 级并发上限：同一 team_code 同时在途（已提交、未到终态）
的评测任务数不得超过 TEAM_MAX_CONCURRENT（默认 8，app/core/config.py），
防止单团队打满全局 worker 并发（验收口径：单团队 100 并发任务不打满全局）。

计数器模型（key: ``team_quota:{team_code}``，值为该团队在途任务数）：

- 占用 ``acquire``：INCR 原子占位 → 与 DB 在途真值对账裁剪 → 判上限；
  超限返回 False 并撤销本次占位（计数器回落到占用前值）。
- 释放 ``release``：DECR，减到 0 删 key 保持 Redis 干净；下限保护不出负数
  （过量 release 只会把账本清零，不会压成负值反向放行超额）。
- 超时回收（TTL 兜底）：每次占用刷新整本计数器的 TTL（TEAM_QUOTA_TTL，
  覆盖单任务最长执行时限 + ack 抖动窗口，与 broker visibility_timeout 同
  口径）。持续无占用的团队计数器整体过期——worker 崩溃等极端场景（无任何
  释放调用）下，泄漏额度最长占用一个 TTL 窗口后自动回满。
- 泄漏对账（``acquire`` 内置）：评测任务自然完成/失败不经过调度器（终态由
  run_evaluation_async 与 celery 回调直写 DB），配额侧没有任务体钩子，故
  每次占用时把计数器与「DB 中该团队 status=running 的在途真值」对账——
  计数只会因丢失 release 虚高、不会虚低，高于真值的部分（自然完成任务的
  遗留额度）在对账时整体裁回，使 TTL 只是兜底而非常规回收路径。

释放路径（调度器侧接线，app/services/task_scheduler.py）：
1. ``TaskScheduler.stop_task``（用户停止）；
2. ``TaskScheduler.cleanup_task``（调度器清理钩子）；
3. 自然完成/失败 → 无钩子，靠占用时对账 + TTL 兜底回收。

竞态说明（诚实起见）：INCR/DECR 各自原子，但「INCR → 对账 SET → 判限」
与并发提交存在交错窗口，最坏放大 ±1 个额度的误差；配额定位是**公平性
护栏**（防单团队打满全局）而非正确性互斥锁（那是 EvalTaskLock 的职责），
±1 误差可接受，故不引入 Lua 整段原子化。
"""
import logging
from typing import Optional

from app.core.config import get_settings
from app.services.redis_stop_signal import get_redis_client

logger = logging.getLogger(__name__)

# 计数器 key 前缀（与 task_stop:/eval_task_lock: 同风格）
TEAM_QUOTA_KEY_PREFIX = "team_quota"

# 整本计数器 TTL：需覆盖单任务最长执行时限（celery task_time_limit=7200s）
# + 重投/ack 抖动窗口，与 celery_app broker visibility_timeout 同口径取
# 7800s（W10-1 同款推导）
TEAM_QUOTA_TTL = 7800


class TeamConcurrencyQuota:
    """team 并发配额计数器（ARCH-2-2）

    redis_client 可注入（单测传 mock）；缺省复用全局 Redis 客户端
    （get_redis_client，与停止信号/互斥锁同一连接口径）。limit 缺省读
    settings.TEAM_MAX_CONCURRENT。
    """

    def __init__(
        self,
        redis_client: Optional[object] = None,
        limit: Optional[int] = None,
        slot_ttl: int = TEAM_QUOTA_TTL,
    ):
        self._client = redis_client if redis_client is not None else get_redis_client()
        if limit is None:
            limit = get_settings().TEAM_MAX_CONCURRENT
        self._limit = limit
        self._ttl = slot_ttl

    @property
    def limit(self) -> int:
        """本配额实例的上限（TEAM_MAX_CONCURRENT）"""
        return self._limit

    @staticmethod
    def _key(team_code: Optional[str]) -> str:
        """计数器 key。team_code 为 NULL 的存量行归一到 DEFAULT（与列
        server_default='DEFAULT' 同语义；SQLAlchemy 对 == None 渲染 IS NULL，
        NULL 团队在 DB 侧对账口径与 'DEFAULT' 字面值团队共用同一本账）"""
        return f"{TEAM_QUOTA_KEY_PREFIX}:{team_code or 'DEFAULT'}"

    def acquire(self, team_code: Optional[str], db_in_flight: int) -> bool:
        """占用一个 team 并发额度，超限拒绝。

        Args:
            team_code: 任务所属团队（EvaluationTask.team_code）
            db_in_flight: DB 中该团队 status=running 的在途任务数，**须包含
                本次待提交任务**（调度器契约：调用方先 start_task 置 running
                再提交；不满足时调用方应自行 +1，见 task_scheduler 接线）。

        Returns:
            True = 占用成功；False = 超限（本次占位已撤销，计数器回落）。
        """
        key = self._key(team_code)
        current = self._client.incr(key)
        # 泄漏对账：只裁不抬——计数高于 DB 真值的部分即丢失 release 的泄漏
        # 额度，整体裁回真值（db_in_flight 已含本次占位语义：真值 + 1）
        if current > db_in_flight:
            self._client.set(key, db_in_flight)
            current = db_in_flight
            logger.info(
                "team %s 配额对账：计数器裁回 DB 在途真值 %d（回收泄漏额度）",
                team_code, db_in_flight,
            )
        # TTL 兜底：每次占用续期整本计数器（持续活跃的团队不会中途过期，
        # 停止活跃后 TTL 到期整本回收）
        self._client.expire(key, self._ttl)
        if current > self._limit:
            # 未获准入：撤销本次 INCR 占位（DECR），保持计数器语义一致
            self.release(team_code)
            logger.warning(
                "team %s 并发配额已满（在途 %d ≥ 上限 %d，TEAM_MAX_CONCURRENT）",
                team_code, db_in_flight, self._limit,
            )
            return False
        return True

    def release(self, team_code: Optional[str]) -> int:
        """释放一个额度（停止/清理钩子调用）。

        返回释放后的占用数。DECR 到 0 及以下直接删 key（下限保护：过量
        release 清零账本，不产生负计数反向放行超额）。
        """
        key = self._key(team_code)
        current = self._client.decr(key)
        if current <= 0:
            self._client.delete(key)
            return 0
        return current

    def current(self, team_code: Optional[str]) -> int:
        """当前占用数（观测/排查用；无计数器视为 0）"""
        value = self._client.get(self._key(team_code))
        return int(value) if value is not None else 0
