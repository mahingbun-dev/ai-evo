"""W10 独立集成验证（测试角色，与开发交付测试互补）。

三组边界，全部复用 tests/integration/conftest.py 现有 fixture：
- fail_stale_running_tasks 收尸口径边界：execution_count=0（防御口径
  coalesce(nullif(0,0),1)=1）应收；非 running 任务（failed/completed）不收；
- 真实 Redis（local_redis）上 EvalTaskLock 接管/续期 Lua 的真实语义：
  开发的 FakeRedis 自实现语义 + kill9 覆盖了接管成功路径，这里补
  「他人锁 renew 不续期」「接管拒绝不破坏他人锁 TTL」在真实 Redis 上的行为；
- reaper 判定与执行记录 heartbeat_at 列的真实读写（TIMESTAMPTZ 回读比较）。

临时库供给与自建自清沿用 conftest（w4t_cfgsnap_ 临时库 + w10t_ 前缀数据）。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.task_models import TaskExecutionRecord
from app.services.evaluation_task_service import EvaluationTaskService

pytestmark = pytest.mark.integration

STALE = datetime.now(timezone.utc) - timedelta(minutes=20)
FRESH = datetime.now(timezone.utc) - timedelta(minutes=5)


def _current_record(db, task_id: str):
    return (
        db.query(TaskExecutionRecord)
        .filter(TaskExecutionRecord.task_id == task_id)
        .order_by(TaskExecutionRecord.execution_number.desc())
        .first()
    )


@pytest.fixture(autouse=True)
def _cleanup_w10t(db_session):
    """自建自清：w10t_ 前缀任务（执行记录随 FK 级联）"""
    from app.models.task_models import EvaluationTask

    def _purge():
        db_session.query(EvaluationTask).filter(
            EvaluationTask.id.like("w10t_%")
        ).delete(synchronize_session=False)
        db_session.commit()

    _purge()
    yield
    _purge()
    db_session.expire_all()


def _make_task(db, task_id: str, status: str, execution_count: int, heartbeat):
    """构造指定状态的任务 + 1 号执行记录（execution_number 固定 1）"""
    from app.models.task_models import EvaluationTask

    task = EvaluationTask(
        id=task_id,
        name=f"W10T独立验证{task_id}",
        evaluation_type="judge",
        status=status,
        judge_count=1,
        execution_count=execution_count,
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


class TestW10TReaperEdges:
    def test_reaps_execution_count_zero_defensive_calibration(self, db_session):
        """execution_count=0（异常残留/旧数据）：防御口径按 1 匹配 1 号记录，超时应收"""
        task = _make_task(db_session, "w10t_zero_count", "running", 0, STALE)

        reaped = EvaluationTaskService(db_session).fail_stale_running_tasks()

        db_session.expire_all()
        assert task.id in reaped
        assert db_session.get(type(task), task.id).status == "failed"

    def test_does_not_reap_failed_task_even_with_stale_record(self, db_session):
        """任务已 failed（记录残留 running+超时心跳）：reaper 不碰非 running 任务"""
        task = _make_task(db_session, "w10t_failed_skip", "failed", 1, STALE)

        reaped = EvaluationTaskService(db_session).fail_stale_running_tasks()

        db_session.expire_all()
        assert task.id not in reaped
        assert db_session.get(type(task), task.id).status == "failed"

    def test_does_not_reap_completed_task_even_with_stale_record(self, db_session):
        """任务已 completed：同样不收（reaper 只作用于 running）"""
        task = _make_task(db_session, "w10t_completed_skip", "completed", 1, STALE)

        reaped = EvaluationTaskService(db_session).fail_stale_running_tasks()

        db_session.expire_all()
        assert task.id not in reaped
        assert db_session.get(type(task), task.id).status == "completed"

    def test_record_status_must_be_running(self, db_session):
        """执行记录已终态（completed）+ 任务 running + 心跳超时：不收
        （记录非 running 说明该次执行已结束，任务态由终态链路负责）"""
        task = _make_task(db_session, "w10t_rec_done", "running", 1, STALE)
        record = _current_record(db_session, task.id)
        record.status = "completed"
        db_session.commit()

        reaped = EvaluationTaskService(db_session).fail_stale_running_tasks()

        db_session.expire_all()
        assert task.id not in reaped

    def test_heartbeat_boundary_just_outside_window_not_reaped(self, db_session):
        """心跳恰在阈值边缘（14 分钟前 < 15 分钟）：不收（超时判定是严格小于）"""
        near = datetime.now(timezone.utc) - timedelta(minutes=14)
        task = _make_task(db_session, "w10t_boundary", "running", 1, near)

        reaped = EvaluationTaskService(db_session).fail_stale_running_tasks()

        db_session.expire_all()
        assert task.id not in reaped
        assert db_session.get(type(task), task.id).status == "running"


class TestW10TRealRedisLockSemantics:
    """真实 Redis（local_redis）上的 Lua 语义：与开发的 FakeRedis 自实现互补"""

    KEY = "eval_task_lock:w10t-real-redis"

    @pytest.fixture(autouse=True)
    def _clean_key(self, local_redis):
        local_redis.delete(self.KEY)
        yield
        local_redis.delete(self.KEY)

    def test_takeover_dead_local_pid_on_real_redis(self, local_redis):
        """真实 Redis：本机死 pid 持锁 → 接管成功 + 短 TTL + renew 续到全量"""
        import socket
        import subprocess
        import sys

        from app.services.redis_stop_signal import (
            EVAL_TASK_LOCK_TAKEOVER_TTL,
            EVAL_TASK_LOCK_TTL,
            EvalTaskLock,
        )

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.kill()
        proc.wait()
        local_redis.set(self.KEY, f"{socket.gethostname()}:{proc.pid}", ex=3600)

        lock = EvalTaskLock("w10t-real-redis")
        assert lock.acquire() is True

        pttl = local_redis.pttl(self.KEY)
        assert 0 < pttl <= EVAL_TASK_LOCK_TAKEOVER_TTL * 1000, f"接管应写短 TTL，pttl={pttl}"

        assert lock.renew() is True
        assert local_redis.pttl(self.KEY) > EVAL_TASK_LOCK_TAKEOVER_TTL * 1000, (
            "接管锁续期后应为全量 TTL"
        )
        assert local_redis.pttl(self.KEY) <= EVAL_TASK_LOCK_TTL * 1000 + 2000

        lock.release()
        assert local_redis.get(self.KEY) is None

    def test_renew_does_not_extend_foreign_lock(self, local_redis):
        """真实 Redis：他人锁 renew 必须失败且 TTL 不动（Lua 比较）"""
        from app.services.redis_stop_signal import EvalTaskLock

        local_redis.set(self.KEY, "other-worker:999", ex=120)
        before = local_redis.pttl(self.KEY)

        assert EvalTaskLock("w10t-real-redis").renew() is False
        after = local_redis.pttl(self.KEY)

        assert after == pytest.approx(before, abs=2000), (
            f"他人锁 TTL 不得被续期（before={before}, after={after}）"
        )
        assert local_redis.get(self.KEY) == "other-worker:999"

    def test_rejected_acquire_leaves_foreign_lock_intact(self, local_redis, monkeypatch):
        """真实 Redis：存活异机持锁者 → acquire 拒绝后锁值与 TTL 原样保留"""
        import app.services.redis_stop_signal as m
        from app.services.redis_stop_signal import EvalTaskLock

        local_redis.set(self.KEY, "remote-live:777", ex=600)
        monkeypatch.setattr(m, "_remote_hostname_alive", lambda h: True)

        assert EvalTaskLock("w10t-real-redis").acquire() is False
        assert local_redis.get(self.KEY) == "remote-live:777"
        pttl = local_redis.pttl(self.KEY)
        assert 590_000 < pttl <= 600_000, f"拒绝路径不得改写他人锁 TTL（pttl={pttl}）"

    def test_holder_roundtrip_after_acquire_on_real_redis(self, local_redis):
        """真实 Redis 正常抢锁：holder() 返回 hostname:pid；release 清锁"""
        import os
        import socket

        from app.services.redis_stop_signal import EvalTaskLock

        lock = EvalTaskLock("w10t-real-redis")
        assert lock.acquire() is True
        assert lock.holder() == f"{socket.gethostname()}:{os.getpid()}"
        lock.release()
        assert local_redis.get(self.KEY) is None
