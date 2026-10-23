"""
Celery 任务定义

包装现有的评测流程函数 run_evaluation_async，
支持在 Celery Worker 中执行。
"""
import logging
import signal
from typing import Dict, Any, Optional, Protocol

from app.celery_app import celery_app
from app.services.redis_stop_signal import (
    HUB_JOB_LOCK_PREFIX,
    HUB_JOB_LOCK_TTL,
    EvalTaskLock,
    RedisStopSignalChecker,
)

logger = logging.getLogger(__name__)


class SigtermHandler:
    """SIGTERM 信号处理器，在收到信号时设置停止事件"""

    def __init__(self, stop_checker: RedisStopSignalChecker):
        self.stop_checker = stop_checker
        self._original_handler = None

    def __call__(self, signum, frame):
        """收到 SIGTERM 时立即设置停止信号"""
        logger.info(f"收到 SIGTERM 信号，设置停止标志")
        self.stop_checker.set()


class StopEventProtocol(Protocol):
    """停止事件协议 - 兼容 threading.Event 和 RedisStopSignalChecker"""

    def is_set(self) -> bool: ...
    def set(self) -> None: ...
    def cleanup(self) -> Optional[Any]: ...


class EvaluationBaseTask(celery_app.Task):
    """评测任务基类，提供状态回调"""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """任务失败回调 — 仅当任务状态为 running 时更新为 failed"""
        evaluation_task_id = args[0]

        # W13：任务失败 → 告警（webhook 或 logs/alerts.log 兜底，契约见
        # app/services/alert_service.py）。告警是旁路能力，自身失败只记日志，
        # 不影响下面的状态回写。
        try:
            from app.services.alert_service import send_alert

            send_alert(
                level="error",
                title="评测任务失败",
                details={
                    "celery_task_id": task_id,
                    "evaluation_task_id": evaluation_task_id,
                    "error": str(exc)[:2000],
                },
                source="celery",
            )
        except Exception as alert_err:  # noqa: BLE001
            logger.error(f"任务失败告警发送失败: {alert_err}")

        try:
            from app.tasks.evaluation_tasks_async import get_db_session
            from app.services.evaluation_task_service import EvaluationTaskService
            from app.services.redis_stop_signal import RedisStopSignalChecker

            db = get_db_session()
            try:
                service = EvaluationTaskService(db)
                task = service.get_task(evaluation_task_id)
                # 竞态保护：只有 running 状态才更新为 failed
                # 避免 stop_task 或 complete_task 已经更新状态后又被覆盖
                if task and task.status == "running":
                    service.fail_task(evaluation_task_id, str(exc))
                else:
                    logger.info(f"任务 {evaluation_task_id} 状态已是 {task.status if task else 'None'}，跳过 on_failure 更新")
                # 清理 Redis 停止信号，确保任务可以重新启动
                stop_checker = RedisStopSignalChecker(evaluation_task_id)
                stop_checker.cleanup()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"任务失败回调更新数据库失败: {e}")

    def on_success(self, retval, task_id, args, kwargs):
        """任务成功回调 — 确认状态更新为 completed"""
        # 正常流程中 run_evaluation_async 已经更新了状态
        # 这里作为兜底保障
        pass

    def on_revoke(self, request, expires=None):
        """任务被撤销回调"""
        evaluation_task_id = request.args[0] if request.args else None
        if evaluation_task_id:
            try:
                from app.tasks.evaluation_tasks_async import get_db_session
                from app.services.evaluation_task_service import EvaluationTaskService
                from app.services.redis_stop_signal import RedisStopSignalChecker

                db = get_db_session()
                try:
                    service = EvaluationTaskService(db)
                    service.stop_task(evaluation_task_id)
                    # 清理 Redis 停止信号，确保任务可以重新启动
                    stop_checker = RedisStopSignalChecker(evaluation_task_id)
                    stop_checker.cleanup()
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"任务撤销回调更新数据库失败: {e}")


@celery_app.task(bind=True, base=EvaluationBaseTask, name="app.tasks.celery_tasks.run_evaluation_task")
def run_evaluation_task(self, task_id: str) -> Dict[str, Any]:
    """
    Celery 评测任务入口

    使用 RedisStopSignalChecker 替代 threading.Event 作为停止信号。
    支持 SIGTERM 信号，当收到终止信号时立即设置停止标志。

    任务级互斥锁（缺陷B4）：task_acks_late=True 下 worker 崩溃（OOM/SIGKILL）
    会使未 ACK 消息重投，同一任务在两个 worker 并发执行引发 hub 镜像目录
    rmtree vs hardlink 竞态。抢锁失败说明已有 worker 在执行同一任务，直接
    拒绝本次重投（不排队、不重入），日志说明后返回。
    """
    # 任务级互斥锁：抢不到 = 同任务已在其它 worker 执行，拒绝本次重投执行
    task_lock = EvalTaskLock(task_id)
    lock_acquired = True
    try:
        lock_acquired = task_lock.acquire()
    except Exception as e:  # noqa: BLE001 - redis 抖动不阻断评测（fail-open），互斥为防御性措施
        logger.warning(f"任务 {task_id} 互斥锁获取异常（redis 不可用？），放行执行: {e}")
    if not lock_acquired:
        holder = None
        try:
            holder = task_lock.holder()
        except Exception:  # noqa: BLE001 - 持锁者查询失败不影响拒绝结论
            pass
        logger.error(
            f"任务 {task_id} 已有其它 worker 在执行（eval_task_lock 冲突，持锁: {holder}），"
            "拒绝本次孤儿消息重投执行"
        )
        return {"status": "duplicate_rejected", "task_id": task_id, "lock_holder": holder}

    # W10-2：接管残留锁以短 TTL（15 分钟）拿锁，任务真正开跑前统一续期到全量
    # TTL（覆盖 task_time_limit=2h）。续期失败仅告警不阻断——最坏情况回到
    # 「TTL 过期后可能重复执行」的兜底语义。
    try:
        task_lock.renew()
    except Exception as e:  # noqa: BLE001 - redis 抖动不阻断评测
        logger.warning(f"任务 {task_id} 互斥锁续期失败（不影响执行）: {e}")

    # 使用 Redis 停止信号检查器（替代 threading.Event）
    stop_checker = RedisStopSignalChecker(task_id)

    # 注册 SIGTERM 信号处理器
    sigterm_handler = SigtermHandler(stop_checker)
    sigterm_handler._original_handler = signal.signal(signal.SIGTERM, sigterm_handler)

    try:
        # 调用现有的评测流程
        from app.tasks.evaluation_tasks_async import run_evaluation_async

        # run_evaluation_async 是同步函数，直接调用
        result = run_evaluation_async(task_id, stop_checker)

        # 清理停止信号
        stop_checker.cleanup()

        return result

    except Exception as e:
        logger.exception(f"Celery 任务执行失败: {task_id}")
        stop_checker.cleanup()
        raise
    finally:
        # 释放任务级互斥锁（正常/异常路径统一；未抢到锁时不误删他人锁）
        if lock_acquired:
            try:
                task_lock.release()
            except Exception as e:  # noqa: BLE001 - 释放失败靠 TTL 过期兜底
                logger.warning(f"任务 {task_id} 互斥锁释放失败（将由 TTL 过期）: {e}")
        # 恢复原始 SIGTERM handler
        if sigterm_handler._original_handler:
            signal.signal(signal.SIGTERM, sigterm_handler._original_handler)


def run_evaluation_with_stop_checker(task_id: str, stop_checker: RedisStopSignalChecker) -> Dict[str, Any]:
    """
    使用 RedisStopSignalChecker 运行评测的同步包装函数。

    此函数供 celery_tasks 使用，保持对现有代码的兼容性。
    """
    from app.tasks.evaluation_tasks_async import run_evaluation_async
    from app.services.redis_stop_signal import RedisStopSignalChecker

    # 创建与 stop_checker 兼容的对象
    stop_event = stop_checker

    return run_evaluation_async(task_id, stop_event)


@celery_app.task(bind=True, name="app.tasks.celery_tasks.run_simulation_run")
def run_simulation_run(self, run_id: str) -> Dict[str, Any]:
    """仿真运行入口（M7 QE7-4）

    与 run_evaluation_task 同口径：运行级互斥锁（acks_late 重投防护，
    key 前缀 simrun_lock）+ Redis 停止信号（回合间生效）+ SIGTERM 优雅停。
    执行体在 SimulationService.execute_run（逐 trial 进度发布 + 配额释放）。
    """
    from app.services.simulation.service import SIM_RUN_LOCK_PREFIX, SimulationService
    from app.tasks.evaluation_tasks_async import get_db_session

    run_lock = EvalTaskLock(run_id, key_prefix=SIM_RUN_LOCK_PREFIX)
    lock_acquired = True
    try:
        lock_acquired = run_lock.acquire()
    except Exception as e:  # noqa: BLE001 - redis 抖动不阻断（fail-open），互斥为防御性措施
        logger.warning(f"仿真 run {run_id} 互斥锁获取异常（redis 不可用？），放行执行: {e}")
    if not lock_acquired:
        holder = None
        try:
            holder = run_lock.holder()
        except Exception:  # noqa: BLE001 - 持锁者查询失败不影响拒绝结论
            pass
        logger.error(
            f"仿真 run {run_id} 已有其它 worker 在执行（simrun_lock 冲突，持锁: {holder}），"
            "拒绝本次孤儿消息重投执行"
        )
        return {"status": "duplicate_rejected", "run_id": run_id, "lock_holder": holder}
    try:
        run_lock.renew()
    except Exception as e:  # noqa: BLE001 - redis 抖动不阻断评测
        logger.warning(f"仿真 run {run_id} 互斥锁续期失败（不影响执行）: {e}")

    stop_checker = RedisStopSignalChecker(run_id)
    sigterm_handler = SigtermHandler(stop_checker)
    sigterm_handler._original_handler = signal.signal(signal.SIGTERM, sigterm_handler)

    db = get_db_session()
    try:
        service = SimulationService(db)
        result = service.execute_run(run_id, stop_checker)
        stop_checker.cleanup()
        return result
    except Exception as e:
        logger.exception(f"仿真 run 执行失败: {run_id}")
        stop_checker.cleanup()
        raise
    finally:
        db.close()
        if lock_acquired:
            try:
                run_lock.release()
            except Exception as e:  # noqa: BLE001 - 释放失败靠 TTL 过期兜底
                logger.warning(f"仿真 run {run_id} 互斥锁释放失败（将由 TTL 过期）: {e}")
        if sigterm_handler._original_handler:
            signal.signal(signal.SIGTERM, sigterm_handler._original_handler)


@celery_app.task(name="app.tasks.celery_tasks.run_hub_import_job")
def run_hub_import_job(
    job_id: str,
    datasets_root: Optional[str] = None,
    bearer_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    anc-hub 执行包批量导入任务入口（数据集迭代阶段C）

    逐包流程在 hub_import_job_service.process_hub_import_job：
    ensure_unpacked → acquire_ref → 写数据行（hub_file 字段 + hub_summary），
    每包完成后向 Redis task_status:{job_id} 发布进度；
    单包失败记入 fail_detail 继续处理，重试只重跑失败项（幂等）。

    job 级互斥锁（W11）：task_acks_late=True 下 worker 崩溃（OOM/SIGKILL）会使
    未 ACK 消息重投，同一 job 在两个 worker 并发执行会造成数据行重复导入、
    ref_count 双计。抢锁失败说明已有 worker 在执行同一 job，直接拒绝本次重投
    （不排队、不重入），日志说明后返回 duplicate_rejected。

    datasets_root 仅用于测试注入临时解包根目录，生产调用缺省落
    uploads/hub_datasets/（ensure_unpacked 缺省值）。

    bearer_token：发起导入的 anc-hub 调用凭证（静态服务 token 或平台登录态 Cookie 值），
    由创建/重试端点随任务投递；下载执行包时以该用户身份调用 anc-hub
    （file-store 需要登录态，匿名会被登录闸门拒绝）。
    """
    from app.services.hub_file_service import HubFileService
    from app.services.hub_import_job_service import process_hub_import_job
    from app.tasks.evaluation_tasks_async import get_db_session

    # job 级互斥锁：抢不到 = 同 job 已在其它 worker 执行，拒绝本次孤儿消息重投执行
    job_lock = EvalTaskLock(job_id, ttl=HUB_JOB_LOCK_TTL, key_prefix=HUB_JOB_LOCK_PREFIX)
    lock_acquired = True
    try:
        lock_acquired = job_lock.acquire()
    except Exception as e:  # noqa: BLE001 - redis 抖动不阻断导入（fail-open），互斥为防御性措施
        logger.warning(f"[hub_import] job {job_id} 互斥锁获取异常（redis 不可用？），放行执行: {e}")
    if not lock_acquired:
        holder = None
        try:
            holder = job_lock.holder()
        except Exception:  # noqa: BLE001 - 持锁者查询失败不影响拒绝结论
            pass
        logger.error(
            f"[hub_import] job {job_id} 已有其它 worker 在执行（{HUB_JOB_LOCK_PREFIX} 锁冲突，"
            f"持锁: {holder}），拒绝本次孤儿消息重投执行"
        )
        return {"status": "duplicate_rejected", "job_id": job_id, "lock_holder": holder}

    db = get_db_session()
    try:
        hub_service = HubFileService(bearer_token=bearer_token)
        return process_hub_import_job(
            db, job_id, datasets_root=datasets_root, hub_service=hub_service
        )
    except Exception:
        logger.exception(f"[hub_import] Celery 导入任务执行失败: {job_id}")
        raise
    finally:
        db.close()
        # 释放 job 级互斥锁（正常/异常路径统一；未抢到锁时不误删他人锁）
        if lock_acquired:
            try:
                job_lock.release()
            except Exception as e:  # noqa: BLE001 - 释放失败靠 TTL 过期兜底
                logger.warning(f"[hub_import] job {job_id} 互斥锁释放失败（将由 TTL 过期）: {e}")
