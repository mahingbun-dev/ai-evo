"""
start_task 原子条件更新的 SQL 语义测试

使用 SQLite 内存库执行真实的 UPDATE ... WHERE status != 'running'，
验证条件更新的原子语义与 execution_count 的 coalesce 自增。
（MagicMock 单测无法覆盖 SQL 层语义，本测试补足该盲区。）
"""
import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from app.models.task_models import EvaluationTask
from app.db.session import Base


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _insert_task(db, status="pending", execution_count=None):
    task = EvaluationTask(
        id="t1",
        name="test",
        evaluation_type="judge",
        status=status,
        execution_count=execution_count,
    )
    db.add(task)
    db.commit()
    return task


class TestStartTaskAtomicUpdate:
    def test_condition_update_succeeds_from_pending(self, db_session):
        """非 running 状态可被原子置为 running，execution_count 自增"""
        _insert_task(db_session, status="pending", execution_count=None)

        updated = db_session.query(EvaluationTask).filter(
            EvaluationTask.id == "t1",
            EvaluationTask.status != "running",
        ).update(
            {
                "status": "running",
                "execution_count": func.coalesce(EvaluationTask.execution_count, 0) + 1,
            },
            synchronize_session=False,
        )
        db_session.commit()

        assert updated == 1
        db_session.expire_all()
        task = db_session.get(EvaluationTask, "t1")
        assert task.status == "running"
        # NULL execution_count 经 coalesce 后从 1 开始
        assert task.execution_count == 1

    def test_condition_update_increments_existing_count(self, db_session):
        """已有 execution_count 时自增"""
        _insert_task(db_session, status="pending", execution_count=5)

        updated = db_session.query(EvaluationTask).filter(
            EvaluationTask.id == "t1",
            EvaluationTask.status != "running",
        ).update(
            {
                "status": "running",
                "execution_count": func.coalesce(EvaluationTask.execution_count, 0) + 1,
            },
            synchronize_session=False,
        )
        db_session.commit()

        assert updated == 1
        db_session.expire_all()
        task = db_session.get(EvaluationTask, "t1")
        assert task.execution_count == 6

    def test_condition_update_rejected_when_running(self, db_session):
        """已 running 的任务条件更新返回 rowcount=0（模拟并发被拒绝）"""
        _insert_task(db_session, status="running", execution_count=2)

        updated = db_session.query(EvaluationTask).filter(
            EvaluationTask.id == "t1",
            EvaluationTask.status != "running",
        ).update(
            {
                "status": "running",
                "execution_count": func.coalesce(EvaluationTask.execution_count, 0) + 1,
            },
            synchronize_session=False,
        )
        db_session.commit()

        assert updated == 0
        db_session.expire_all()
        task = db_session.get(EvaluationTask, "t1")
        # 状态与计数均不被改变
        assert task.status == "running"
        assert task.execution_count == 2

    def test_condition_update_allows_rerun_after_failed(self, db_session):
        """failed 状态的任务可再次启动（符合允许重新运行已失败任务的业务规则）"""
        _insert_task(db_session, status="failed", execution_count=1)

        updated = db_session.query(EvaluationTask).filter(
            EvaluationTask.id == "t1",
            EvaluationTask.status != "running",
        ).update(
            {
                "status": "running",
                "execution_count": func.coalesce(EvaluationTask.execution_count, 0) + 1,
            },
            synchronize_session=False,
        )
        db_session.commit()

        assert updated == 1
        db_session.expire_all()
        task = db_session.get(EvaluationTask, "t1")
        assert task.status == "running"
        assert task.execution_count == 2
