"""
任务调度器 - 使用 Celery 管理评测任务的生命周期

替代 APScheduler，实现 API 与任务执行的完全解耦。
"""
import logging
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.services.redis_stop_signal import RedisStopSignalChecker

logger = logging.getLogger(__name__)


def _get_db_session():
    """获取数据库会话"""
    settings = get_settings()
    engine = create_engine(settings.DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)
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

    def submit_task(self, task_id: str) -> bool:
        """
        提交任务到 Celery Worker。

        Note:
            - 任务状态检查和 start_task() 由调用方负责
            - 此方法仅负责任务入队和存储 celery_task_id

        Returns:
            True 表示提交成功，False 表示提交失败
        """
        from app.tasks.celery_tasks import run_evaluation_task
        from app.models.task_models import EvaluationTask

        db = _get_db_session()
        try:
            task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
            if not task:
                logger.warning(f"任务 {task_id} 不存在")
                return False

            # 提交到 Celery
            result = run_evaluation_task.delay(task_id)

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

        # 清理数据库中的 celery_task_id
        from app.models.task_models import EvaluationTask
        db = _get_db_session()
        try:
            task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
            if task:
                task.celery_task_id = None
                db.commit()
        except Exception as e:
            logger.warning(f"清理 celery_task_id 失败: {e}")
            db.rollback()
        finally:
            db.close()

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
