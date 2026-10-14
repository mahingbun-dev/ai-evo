"""W10-4 执行心跳与启动收尸 reaper 集成测试

整改计划 §3 W10 第 4 项：执行器每批更新 TaskExecutionRecord.heartbeat_at；
API lifespan 批量「标 failed」改为「heartbeat 超 15 分钟才标」，消除部署期误标。

「假时钟」手法：不依赖 freezegun——reaper 以 now-timezone 为 cutoff，测试直接
把执行记录的 heartbeat_at 预置为 now-20min（超时）/ now-5min（新鲜）/ NULL，
断言收尸判定只命中超时行。复用 tests/integration/conftest 的 db_session /
make_judge_task fixture（临时库供给，自建自清）。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.task_models import TaskExecutionRecord
from app.services.evaluation_task_service import (
    EvaluationTaskService,
    TaskExecutionRecordService,
)

pytestmark = pytest.mark.integration

STALE = datetime.now(timezone.utc) - timedelta(minutes=20)
FRESH = datetime.now(timezone.utc) - timedelta(minutes=5)


def _current_record(db, task_id: str) -> TaskExecutionRecord:
    return (
        db.query(TaskExecutionRecord)
        .filter(TaskExecutionRecord.task_id == task_id)
        .order_by(TaskExecutionRecord.execution_number.desc())
        .first()
    )


def _set_heartbeat(db, task_id: str, value):
    record = _current_record(db, task_id)
    record.heartbeat_at = value
    db.commit()


@pytest.fixture(autouse=True)
def _cleanup_w10_tasks(db_session):
    """自建自清：用例前后删除本文件创建的 w10it_ 任务（执行记录随 FK 级联）"""
    from app.models.task_models import EvaluationTask

    def _purge():
        db_session.query(EvaluationTask).filter(
            EvaluationTask.id.like("w10it_%")
        ).delete(synchronize_session=False)
        db_session.commit()

    _purge()
    yield
    _purge()
    db_session.expire_all()


# ---------------------------------------------------------------------------
# touch_heartbeat：心跳写入
# ---------------------------------------------------------------------------

class TestTouchHeartbeat:
    def test_updates_running_record(self, db_session, judge_task_10_rows):
        """running 执行记录：touch 写入 heartbeat_at（≈now）"""
        svc = EvaluationTaskService(db_session)
        svc.touch_heartbeat(judge_task_10_rows, 1)
        db_session.expire_all()

        record = _current_record(db_session, judge_task_10_rows)
        assert record.heartbeat_at is not None
        assert abs((datetime.now(timezone.utc) - record.heartbeat_at).total_seconds()) < 60

    def test_skips_terminal_record(self, db_session, judge_task_10_rows):
        """执行记录已终态：touch 不写心跳（终态记录心跳无意义）"""
        record = _current_record(db_session, judge_task_10_rows)
        record.status = "completed"
        db_session.commit()

        EvaluationTaskService(db_session).touch_heartbeat(judge_task_10_rows, 1)
        db_session.expire_all()
        assert _current_record(db_session, judge_task_10_rows).heartbeat_at is None

    def test_skips_missing_record_and_none_execution(self, db_session, judge_task_10_rows):
        """记录缺失 / execution_number 缺失（非任务链路）：静默 no-op"""
        svc = EvaluationTaskService(db_session)
        svc.touch_heartbeat("w10it_no_such_task", 1)  # 记录缺失
        svc.touch_heartbeat(judge_task_10_rows, None)  # 非任务链路

    def test_record_service_touch_direct(self, db_session, judge_task_10_rows):
        """TaskExecutionRecordService.touch_heartbeat 直调同语义"""
        TaskExecutionRecordService(db_session).touch_heartbeat(judge_task_10_rows, 1)
        db_session.expire_all()
        assert _current_record(db_session, judge_task_10_rows).heartbeat_at is not None


# ---------------------------------------------------------------------------
# fail_stale_running_tasks：收尸 reaper（假时钟）
# ---------------------------------------------------------------------------

class TestFailStaleRunningTasks:
    def test_reaps_only_stale_heartbeat(self, db_session):
        """heartbeat 超 15 分钟 → failed；新鲜/NULL/执行序号不匹配 → 保持 running"""
        stale_task = _make_running_task(db_session, "w10it_stale", heartbeat=STALE)
        fresh_task = _make_running_task(db_session, "w10it_fresh", heartbeat=FRESH)
        null_task = _make_running_task(db_session, "w10it_null", heartbeat=None)
        mismatch_task = _make_running_task(db_session, "w10it_mismatch", heartbeat=STALE)
        # 执行序号不匹配：任务 execution_count=2，残留的是 1 号执行记录的旧心跳
        mismatch_task.execution_count = 2
        db_session.commit()

        reaped = EvaluationTaskService(db_session).fail_stale_running_tasks()

        db_session.expire_all()
        assert reaped == [stale_task.id]
        assert db_session.get(type(stale_task), stale_task.id).status == "failed"
        assert db_session.get(type(fresh_task), fresh_task.id).status == "running"
        assert db_session.get(type(null_task), null_task.id).status == "running"
        assert db_session.get(type(mismatch_task), mismatch_task.id).status == "running"

    def test_stale_task_record_also_failed(self, db_session):
        """收尸同时把执行记录标 failed（fail_task 同步链路）"""
        task = _make_running_task(db_session, "w10it_recfail", heartbeat=STALE)

        EvaluationTaskService(db_session).fail_stale_running_tasks()

        db_session.expire_all()
        record = _current_record(db_session, task.id)
        assert record.status == "failed"
        assert record.error_message is not None

    def test_empty_when_nothing_stale(self, db_session, judge_task_10_rows):
        """无超时任务：返回空列表，不触碰任何任务"""
        from app.models.task_models import EvaluationTask

        reaped = EvaluationTaskService(db_session).fail_stale_running_tasks()
        assert reaped == []
        db_session.expire_all()
        assert db_session.get(EvaluationTask, judge_task_10_rows) is not None


def _make_running_task(db, task_id: str, heartbeat):
    """构造 running 任务 + 执行记录（w10it_ 前缀自建自清；收尸判定不依赖数据集）"""
    from app.models.task_models import EvaluationTask

    task = EvaluationTask(
        id=task_id,
        name=f"W10收尸测试{task_id}",
        evaluation_type="judge",
        status="running",
        judge_count=1,
        execution_count=1,
    )
    db.add(task)
    db.flush()
    db.add(TaskExecutionRecord(
        id=f"exec_{task_id}",
        task_id=task_id,
        execution_number=1,
        status="running",
        heartbeat_at=heartbeat,
    ))
    db.commit()
    return task


# ---------------------------------------------------------------------------
# 执行链路接线：每批（行）完成即续跳
# ---------------------------------------------------------------------------

class TestExecutorHeartbeatWiring:
    def test_executor_update_progress_touches_heartbeat(self, db_session, judge_task_10_rows):
        """EvaluationExecutor._update_progress：进度推进同步刷新心跳"""
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(
            max_workers=1, task_id=judge_task_10_rows, execution_number=1,
        )
        executor._update_progress(3, 10)
        db_session.expire_all()

        record = _current_record(db_session, judge_task_10_rows)
        assert record.heartbeat_at is not None

    def test_module_update_progress_touches_heartbeat(self, db_session, judge_task_10_rows):
        """evaluation_tasks_async._update_progress（consistency/success/binary 路径）同语义"""
        from app.tasks.evaluation_tasks_async import _update_progress

        _update_progress(2, 10, judge_task_10_rows, 1)
        db_session.expire_all()

        record = _current_record(db_session, judge_task_10_rows)
        assert record.heartbeat_at is not None

    def test_module_update_progress_without_execution_number_noop_heartbeat(self, db_session, judge_task_10_rows):
        """execution_number 缺失（直调/测试链路）：不写心跳，进度照常更新"""
        from app.models.task_models import EvaluationTask
        from app.tasks.evaluation_tasks_async import _update_progress

        _update_progress(1, 10, judge_task_10_rows, None)
        db_session.expire_all()

        record = _current_record(db_session, judge_task_10_rows)
        assert record.heartbeat_at is None
        task = db_session.query(EvaluationTask).filter(
            EvaluationTask.id == judge_task_10_rows).one()
        assert task.completed_questions == 1


# ---------------------------------------------------------------------------
# 心跳写库节流（60s 最小间隔；首跳 force 绕过）
# ---------------------------------------------------------------------------

class TestHeartbeatThrottle:
    def test_second_touch_within_interval_throttled(self, db_session, judge_task_10_rows):
        """节流窗口内的第二次 touch 不写库（心跳保持原值，无额外 UPDATE）"""
        from app.services import evaluation_task_service as ets

        ets._heartbeat_last_write.pop(judge_task_10_rows, None)
        svc = EvaluationTaskService(db_session)

        svc.touch_heartbeat(judge_task_10_rows, 1)
        db_session.expire_all()
        assert _current_record(db_session, judge_task_10_rows).heartbeat_at is not None

        # 把心跳拨回已知早值；窗口内再 touch 不应覆盖
        _set_heartbeat(db_session, judge_task_10_rows, FRESH)
        svc.touch_heartbeat(judge_task_10_rows, 1)
        db_session.expire_all()
        assert _current_record(db_session, judge_task_10_rows).heartbeat_at == FRESH

    def test_force_bypasses_throttle(self, db_session, judge_task_10_rows):
        """force=True（执行开始首跳）绕过节流立即写库"""
        from app.services import evaluation_task_service as ets

        ets._heartbeat_last_write.pop(judge_task_10_rows, None)
        svc = EvaluationTaskService(db_session)

        svc.touch_heartbeat(judge_task_10_rows, 1)
        _set_heartbeat(db_session, judge_task_10_rows, FRESH)
        svc.touch_heartbeat(judge_task_10_rows, 1, force=True)
        db_session.expire_all()
        assert _current_record(db_session, judge_task_10_rows).heartbeat_at != FRESH

    def test_throttle_interval_constant_is_minutes_scale(self):
        """节流间隔远小于收尸阈值（60s ≪ 15min），不影响 reaper 判定"""
        from app.services import evaluation_task_service as ets

        assert ets.HEARTBEAT_MIN_INTERVAL_SECONDS * 10 < 15 * 60


# ---------------------------------------------------------------------------
# main.py lifespan 接线
# ---------------------------------------------------------------------------

class TestLifespanWiring:
    def test_lifespan_uses_heartbeat_reaper_not_blanket_fail(self):
        """lifespan 启动清理改为 fail_stale_running_tasks（不再无条件标 failed）"""
        import inspect

        from app.main import lifespan

        src = inspect.getsource(lifespan)
        assert "fail_stale_running_tasks()" in src
        assert 'get_tasks(status="running")' not in src, (
            "lifespan 不得再把所有 running 任务一律标 failed（W10-4 部署期误标）"
        )
        assert "服务重启，任务中断" not in src
