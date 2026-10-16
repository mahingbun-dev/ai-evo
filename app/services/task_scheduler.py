"""
任务调度器 - 使用 Celery 管理评测任务的生命周期

替代 APScheduler，实现 API 与任务执行的完全解耦。
"""
import logging
from typing import Optional

from app.services.quota import TeamConcurrencyQuota
from app.services.redis_stop_signal import RedisStopSignalChecker

logger = logging.getLogger(__name__)


def _get_db_session():
    """获取数据库会话（W10-5：复用 app.db.session 全局 engine，不再每次新建引擎）

    旧实现每次调用 create_engine：连接池从不复用、连接数随调用次数增长，
    且与全局 engine 双池并存放大连接占用；统一走全局 engine + SessionLocal。
    """
    from app.db.session import SessionLocal
    return SessionLocal()


class TaskScheduler:
    """
    评测任务调度器 - Celery 版本

    使用 Celery Worker 执行评测任务，实现与 API 进程的完全解耦。
    """

    _instance: Optional["TaskScheduler"] = None

    def __new__(cls) -> "TaskScheduler":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        logger.info("TaskScheduler (Celery) 已初始化")

    def submit_task(self, task_id: str, *, high_priority: bool = True) -> bool:
        """
        提交任务到 Celery Worker。

        ARCH-2-2（设计 §2.3 决策 D4 / §3 Phase 2 表 2-2 行）：

        - team 并发配额：提交前校验（app/services/quota.py），超限**拒绝**
          （不排队不等待）——返回 False，调用方既有失败路径（fail_task）把
          任务标记为 failed，团队额度释放后重跑即可。选「拒绝」而非「排队」：
          排队需在任务体/消息重投链路上占位重试（app/tasks 边界外，且占用
          与释放无法成对），批量入口本身也会反复补提交，拒绝语义对调用方
          最诚实、无静默积压。
        - 优先级：人工触发的单任务调试走高优先（celery 消息 priority，eval
          队列内插队于积压消息之前），批量任务走普通档。high_priority 缺省
          True——现有两个调用点（单任务执行 / 批量执行）当前等价拿缺省；
          批量入口（app/api/batch_operations_api.py 的 batch-run）应显式传
          False，该一行接线属 app/api 边界，随其归属批次落地（本批边界
          不含 app/api）。

        Note:
            - 任务状态检查和 start_task() 由调用方负责，且须**先** start_task
              置 running 再提交（配额的 DB 在途对账依赖该顺序）
            - 此方法仅负责任务入队和存储 celery_task_id

        Returns:
            True 表示提交成功，False 表示提交失败（含 team 配额超限拒绝、
            Redis/Celery 不可达等异常）
        """
        from sqlalchemy import func

        from app.celery_app import PRIORITY_HIGH, PRIORITY_NORMAL
        from app.models.task_models import EvaluationTask
        from app.tasks.celery_tasks import run_evaluation_task

        db = _get_db_session()
        try:
            task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
            if not task:
                logger.warning(f"任务 {task_id} 不存在")
                return False

            # ARCH-2-2：team 并发配额——提交前校验，超限拒绝（完整语义见
            # 方法 docstring 与 app/services/quota.py 模块头）。在途真值 =
            # 该团队 status=running 的任务数（含本任务：调用方 start_task
            # 已先行置 running；未经 start_task 的非标准调用防御性 +1，
            # 保证对账不把本次占位误裁）。
            quota = TeamConcurrencyQuota()
            in_flight = (
                db.query(func.count(EvaluationTask.id))
                .filter(
                    EvaluationTask.team_code == task.team_code,
                    EvaluationTask.status == "running",
                )
                .scalar()
                or 0
            )
            if task.status != "running":
                in_flight += 1
            if not quota.acquire(team_code=task.team_code, db_in_flight=in_flight):
                logger.warning(
                    f"任务 {task_id} 提交被拒：team {task.team_code} 并发配额已满"
                    f"（DB 在途 {in_flight}，上限 {quota.limit}，TEAM_MAX_CONCURRENT）"
                )
                return False

            # 提交到 Celery（ARCH-2-2：priority 经 Redis transport 的
            # priority_steps 子队列生效，人工单任务高优先于批量任务）
            try:
                result = run_evaluation_task.apply_async(
                    args=[task_id],
                    priority=PRIORITY_HIGH if high_priority else PRIORITY_NORMAL,
                )
            except Exception:
                # 占用与释放成对：broker 不可达等提交失败时撤销刚占用的
                # 额度（撤销失败仅告警——泄漏由对账/TTL 兜底，见 quota.py）
                try:
                    quota.release(task.team_code)
                except Exception as release_err:  # noqa: BLE001
                    logger.warning(
                        f"任务 {task_id} 提交失败后释放 team 配额额度异常: {release_err}"
                    )
                raise

            # 存储 celery_task_id 到数据库
            task.celery_task_id = result.id
            db.commit()

            logger.info(f"任务 {task_id} 已提交到 Celery，celery_task_id={result.id}")
            return True

        except Exception as e:
            logger.error(f"任务 {task_id} 提交失败: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def stop_task(self, task_id: str) -> bool:
        """
        立即停止正在运行的任务。

        操作步骤：
        1. 设置 Redis 停止信号（冗余保护）
        2. 强制 terminate Celery 任务（发送 SIGTERM，等待超时后 SIGKILL）
        """
        from app.models.task_models import EvaluationTask
        from app.celery_app import celery_app

        # 1. 设置 Redis 停止信号（冗余，作为双保险）
        stop_checker = RedisStopSignalChecker(task_id)
        stop_checker.set()
        logger.info(f"任务 {task_id} 停止信号已发送到 Redis")

        # 2. 强制终止 Celery 任务
        db = _get_db_session()
        try:
            task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
            if task and task.celery_task_id:
                try:
                    # terminate=True: 发送 SIGTERM，等待 3 秒超时后发送 SIGKILL
                    celery_app.control.revoke(
                        task.celery_task_id,
                        terminate=True,
                        timeout=3,  # 等待3秒让任务优雅退出
                    )
                    logger.info(f"任务 {task_id} 已发送 SIGTERM 终止信号")
                except Exception as e:
                    logger.warning(f"Celery terminate 失败: {e}")
            # ARCH-2-2：停止即释放 team 并发额度（占用/释放路径全貌见
            # app/services/quota.py 模块头；释放失败仅告警——停止语义不被
            # 配额故障破坏，泄漏由占用时对账/TTL 兜底）
            if task:
                try:
                    TeamConcurrencyQuota().release(task.team_code)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"任务 {task_id} 停止后释放 team 配额额度失败: {e}")
        finally:
            db.close()

        return True

    def cleanup_task(self, task_id: str):
        """
        清理任务状态（任务完成/失败/停止后调用）。
        """
        # 清理 Redis 停止信号
        stop_checker = RedisStopSignalChecker(task_id)
        stop_checker.cleanup()

        # ARCH-2-2：清理钩子同样释放 team 并发额度（幂等下限保护见
        # quota.release：过量释放清零账本，不出负数；team_code 为 NULL 的
        # 任务按 quota._key 归一落 DEFAULT 账本，同样要释放）
        quota = TeamConcurrencyQuota()
        team_code = None
        task_found = False

        # 清理数据库中的 celery_task_id
        from app.models.task_models import EvaluationTask
        db = _get_db_session()
        try:
            task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
            if task:
                task_found = True
                team_code = task.team_code
                task.celery_task_id = None
                db.commit()
        except Exception as e:
            logger.warning(f"清理 celery_task_id 失败: {e}")
            db.rollback()
        finally:
            db.close()

        # 任务不存在（从未占用）不释放；存在则按其团队释放（含 NULL → DEFAULT）
        if task_found:
            try:
                quota.release(team_code)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"任务 {task_id} 清理时释放 team 配额额度失败: {e}")

        logger.debug(f"任务 {task_id} 调度器状态已清理")

    def is_task_running(self, task_id: str) -> bool:
        """检查任务是否仍在运行"""
        from app.models.task_models import EvaluationTask

        db = _get_db_session()
        try:
            task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
            if not task:
                return False
            return task.status == "running"
        finally:
            db.close()

    def shutdown(self, wait: bool = False):
        """
        关闭调度器（应用退出时调用）。

        Celery 版本不需要特殊处理，Worker 由独立进程管理。
        """
        logger.info("TaskScheduler (Celery) 关闭（无操作）")


def get_task_scheduler() -> TaskScheduler:
    """获取 TaskScheduler 单例"""
    return TaskScheduler()
