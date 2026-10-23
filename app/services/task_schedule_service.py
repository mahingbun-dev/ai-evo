"""任务定时调度服务（M5 QE5-3）

实现方式（方案 §5.3 决策：不引 celery beat）：

- api 进程 lifespan 周期任务每 60s 调 :func:`scan_due`（app/main.py），
  扫描 ``next_run_at <= now`` 的启用调度；
- 多 api 部署下单写保证双保险：
  1. Redis 锁（SET NX EX，锁住整轮扫描，防两进程同拍扫全表）；
  2. 逐条 CAS 认领——先原子推进 next_run_at（UPDATE ... WHERE
     next_run_at = 旧值，rowcount=1 者独占该次触发），再投递；
- 投递走与 POST /{task_id}/run 完全相同的语义链：start_task 原子互斥
  （任务 running 即跳过）→ scheduler.submit_task（团队配额超限/队列
  不可达返回 False → fail_task 落失败态）→ priority=NORMAL（不插队）；
- 时段限制（window）：触发时刻须落在窗口内，否则记 skipped_window
  并消耗该次触发（next_run_at 已推进，节律不乱）；窗口支持跨零点。
"""
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.schedule_models import TaskSchedule
from app.models.task_models import EvaluationTask
from app.services.cron_service import cron_next, validate_cron
from app.services.stats_service import BEIJING_TZ, beijing_now

logger = logging.getLogger(__name__)

# 历史内嵌上限（新在前；超出裁尾）
HISTORY_LIMIT = 20

# 扫描互斥锁（key 与 task_stop:/team_quota: 同风格）；TTL 略小于扫描周期，
# 进程崩溃后锁自动过期，不会卡死后续扫描
SCAN_LOCK_KEY = "task_schedule_scan_lock"
SCAN_LOCK_TTL_SECONDS = 55

# window 字段格式 HH:MM
_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class TaskScheduleService:
    """task_schedules 的 CRUD 与 due 扫描投递"""

    def __init__(self, db: Session):
        self.db = db

    # ---------------- CRUD ----------------

    def upsert_schedule(
        self,
        task_id: str,
        cron: str,
        enabled: bool = True,
        window_start: Optional[str] = None,
        window_end: Optional[str] = None,
        user_code: Optional[str] = None,
    ) -> TaskSchedule:
        """创建或更新任务定时（一任务一条；cron/window 校验 + 重算 next_run_at）。

        Raises:
            ValueError: cron 非法 / window 格式非法或只配一半 / 任务不存在。
        """
        validate_cron(cron)
        window_start = (window_start or "").strip() or None
        window_end = (window_end or "").strip() or None
        if (window_start is None) != (window_end is None):
            raise ValueError("时段限制须同时配置开始与结束时间（或都留空）")
        for label, value in (("开始", window_start), ("结束", window_end)):
            if value is not None and not _WINDOW_RE.match(value):
                raise ValueError(f"时段限制{label}时间格式非法（应为 HH:MM）：{value}")

        task = self.db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
        if not task or not task.is_active:
            raise ValueError("任务不存在或已删除")

        now = beijing_now()
        schedule = self.get_schedule(task_id)
        if schedule is None:
            schedule = TaskSchedule(
                id=f"sch_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                cron=cron,
                enabled=enabled,
                window_start=window_start,
                window_end=window_end,
                # enabled=False 时置空：扫描天然跳过，重新启用时再从当下重算
                next_run_at=cron_next(cron, now) if enabled else None,
                created_by=user_code,
            )
            self.db.add(schedule)
        else:
            schedule.cron = cron
            schedule.enabled = enabled
            schedule.window_start = window_start
            schedule.window_end = window_end
            schedule.next_run_at = cron_next(cron, now) if enabled else None
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def get_schedule(self, task_id: str) -> Optional[TaskSchedule]:
        return (
            self.db.query(TaskSchedule)
            .filter(TaskSchedule.task_id == task_id)
            .first()
        )

    def delete_schedule(self, task_id: str) -> bool:
        schedule = self.get_schedule(task_id)
        if schedule is None:
            return False
        self.db.delete(schedule)
        self.db.commit()
        return True

    def schedule_payload(self, schedule: TaskSchedule) -> Dict[str, Any]:
        """API 响应序列化（datetime 统一 isoformat，与前端展示口径一致）。"""
        return {
            "task_id": schedule.task_id,
            "cron": schedule.cron,
            "enabled": bool(schedule.enabled),
            "window_start": schedule.window_start,
            "window_end": schedule.window_end,
            "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            "last_status": schedule.last_status,
            "last_message": schedule.last_message,
            "history": schedule.history or [],
            "created_by": schedule.created_by,
            "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
        }

    # ---------------- due 扫描与投递 ----------------

    def scan_due(
        self,
        now: Optional[datetime] = None,
        redis_client: Any = None,
    ) -> Dict[str, int]:
        """扫描并投递到期调度（lifespan 每 60s 调用一次）。

        Redis 锁保证同一时刻只有一个 api 进程在扫；锁内逐条 CAS 认领
        （推进 next_run_at），认领成功才投递——锁与 CAS 任一失效（如锁
        过期竞态）仍有 CAS 兜底，不重复派发。返回各结果计数。

        Args:
            now: 当前时间（测试注入用；缺省北京时间）。
            redis_client: Redis 客户端（测试注入 FakeRedis；缺省真连）。
        """
        now = now or beijing_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=BEIJING_TZ)
        if redis_client is None:
            from app.services.redis_stop_signal import get_redis_client
            redis_client = get_redis_client()

        # 锁：SET NX EX（拿到才扫；拿不到说明兄弟进程正在扫，本轮让位）
        token = uuid.uuid4().hex
        locked = redis_client.set(SCAN_LOCK_KEY, token, nx=True, ex=SCAN_LOCK_TTL_SECONDS)
        if not locked:
            return {"skipped_lock": 1}

        counts: Dict[str, int] = {}
        try:
            due = (
                self.db.query(TaskSchedule)
                .filter(
                    TaskSchedule.enabled.is_(True),
                    TaskSchedule.next_run_at.isnot(None),
                    TaskSchedule.next_run_at <= now,
                )
                .all()
            )
            for schedule in due:
                # CAS 认领：把 next_run_at 推进到「now 之后的下一次」，
                # 仅当旧值未被他人动过（rowcount=1）才由本进程投递该次触发。
                # 错过补跑语义：停机错过多次也只补跑一次，随后对齐到未来节律。
                new_next = cron_next(schedule.cron, now)
                claimed = (
                    self.db.query(TaskSchedule)
                    .filter(
                        TaskSchedule.id == schedule.id,
                        TaskSchedule.next_run_at == schedule.next_run_at,
                    )
                    .update({"next_run_at": new_next}, synchronize_session=False)
                )
                self.db.commit()
                if not claimed:
                    counts["lost_race"] = counts.get("lost_race", 0) + 1
                    continue
                outcome = self._dispatch(schedule, now)
                status = outcome["status"]
                counts[status] = counts.get(status, 0) + 1
                self._record(schedule.id, now, outcome)
            return counts
        finally:
            # 释放锁（比对 token 防误删他人锁；TTL 兜底，失败仅告警）
            try:
                if redis_client.get(SCAN_LOCK_KEY) == token:
                    redis_client.delete(SCAN_LOCK_KEY)
            except Exception as e:  # noqa: BLE001 - 释放失败靠 TTL 兜底
                logger.warning(f"定时扫描锁释放失败（等 TTL 过期）: {e}")

    def _dispatch(self, schedule: TaskSchedule, now: datetime) -> Dict[str, Any]:
        """投递一次触发：走既有 run 入口语义（互斥/配额/priority=NORMAL）。"""
        task = (
            self.db.query(EvaluationTask)
            .filter(EvaluationTask.id == schedule.task_id)
            .first()
        )
        if task is None or not task.is_active:
            return {"status": "task_unavailable", "message": "任务不存在或已删除，跳过触发",
                    "execution_number": None}

        # 时段限制：触发时刻不在窗口内则消耗该次触发（不投递、不报错）
        if (schedule.window_start and schedule.window_end
                and not self._in_window(now, schedule.window_start, schedule.window_end)):
            return {"status": "skipped_window",
                    "message": f"触发时刻不在允许时段（{schedule.window_start}-{schedule.window_end}）",
                    "execution_number": None}

        from app.services.evaluation_task_service import EvaluationTaskService
        from app.services.task_scheduler import get_task_scheduler

        if task.status == "running":
            return {"status": "skipped_running", "message": "任务已在运行中（互斥跳过）",
                    "execution_number": None}

        service = EvaluationTaskService(self.db)
        # 原子互斥：并发触发/人工执行同时到达时只有一个能把状态置 running
        started = service.start_task(task.id)
        if started is None:
            return {"status": "skipped_running", "message": "任务已在运行中（并发互斥）",
                    "execution_number": None}

        # priority=NORMAL（plan §5.3）：定时批量不与人工调试抢插队
        submitted = get_task_scheduler().submit_task(task.id, high_priority=False)
        if not submitted:
            # 与 run 端点同款失败语义：落 failed 态，释放团队额度后下个周期自然重跑
            service.fail_task(task.id, "定时触发：提交失败（团队配额超限或队列不可达）")
            return {"status": "submit_rejected",
                    "message": "提交被拒（团队配额超限或队列不可达），任务已置失败态",
                    "execution_number": started.execution_count}
        return {"status": "dispatched", "message": "已投递执行",
                "execution_number": started.execution_count}

    @staticmethod
    def _in_window(now: datetime, start: str, end: str) -> bool:
        """HH:MM 字符串比较（补零格式天然有序）；start>end 视为跨零点窗口。"""
        t = now.strftime("%H:%M")
        if start <= end:
            return start <= t < end
        return t >= start or t < end

    def _record(self, schedule_id: str, now: datetime, outcome: Dict[str, Any]) -> None:
        """落 last_* 字段并内嵌历史（新在前，裁尾到 HISTORY_LIMIT）。"""
        schedule = (
            self.db.query(TaskSchedule)
            .filter(TaskSchedule.id == schedule_id)
            .first()
        )
        if schedule is None:
            return
        entry = {
            "at": now.isoformat(),
            "status": outcome["status"],
            "message": outcome.get("message") or "",
            "execution_number": outcome.get("execution_number"),
        }
        history = [entry] + list(schedule.history or [])
        schedule.last_run_at = now
        schedule.last_status = outcome["status"]
        schedule.last_message = outcome.get("message")
        schedule.history = history[:HISTORY_LIMIT]
        self.db.commit()
