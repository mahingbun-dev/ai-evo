"""EvaluationTaskService.get_tasks 的 name 模糊过滤测试（service 层）。

按仓库既有先例（tests/unit/test_instruction_config_weight_api.py）用自建自清的
sqlite 临时库直调 service——真实 SQL 语义（ilike 子串匹配）只有跑真查询链才可信，
mock 查询链只能断言表达式形状，断言不了「过滤真的生效」。

覆盖：
- name=None 不拼 ilike（返回全部）
- name 生效：子串匹配过滤结果集
- 大小写不敏感（SQLAlchemy ilike 在 sqlite 上编译为 lower(...) LIKE lower(...)）
- 首尾空格 strip（"  冒烟  " 等价 "冒烟"）
- 无命中返回空列表
- name 与 status 组合过滤仍叠加
"""
import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.task_models import EvaluationTask
from app.services.evaluation_task_service import EvaluationTaskService

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无外部 DB）

_DB_DIR = tempfile.mkdtemp(prefix="get_tasks_name_test_")
_engine = create_engine(
    f"sqlite:///{os.path.join(_DB_DIR, 'name_filter.sqlite')}",
    connect_args={"check_same_thread": False},
)
_TestingSessionLocal = sessionmaker(bind=_engine, autoflush=False)
Base.metadata.create_all(bind=_engine, tables=[EvaluationTask.__table__])

_SEED = [
    ("t_name_1", "冒烟任务-回归", "completed"),
    ("t_name_2", "Smoke-Test-Alpha", "running"),
    ("t_name_3", "每日巡检报告", "completed"),
    ("t_name_4", "冒烟任务-上线", "failed"),
]


def _seed():
    db = _TestingSessionLocal()
    try:
        for task_id, name, status in _SEED:
            db.add(EvaluationTask(
                id=task_id, name=name, evaluation_type="judge", status=status,
                is_active=True, version=1,
            ))
        db.commit()
    finally:
        db.close()


def _cleanup():
    db = _TestingSessionLocal()
    try:
        for task_id, _, _ in _SEED:
            db.query(EvaluationTask).filter(EvaluationTask.id == task_id).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    _seed()
    yield
    _cleanup()


@pytest.fixture
def db():
    session = _TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


class TestGetTasksNameFilter:
    def test_name_none_returns_all(self, db):
        """name=None：不拼 ilike，返回全部活跃任务。"""
        tasks = EvaluationTaskService(db).get_tasks()
        assert {t.id for t in tasks} >= {row[0] for row in _SEED}

    def test_name_filters_by_substring(self, db):
        tasks = EvaluationTaskService(db).get_tasks(name="冒烟")
        assert {t.id for t in tasks} == {"t_name_1", "t_name_4"}

    def test_name_match_is_case_insensitive(self, db):
        tasks = EvaluationTaskService(db).get_tasks(name="smoke-test")
        assert {t.id for t in tasks} == {"t_name_2"}

    def test_name_stripped(self, db):
        """首尾空格 strip：'  冒烟  ' 与 '冒烟' 等价。"""
        tasks = EvaluationTaskService(db).get_tasks(name="  冒烟  ")
        assert {t.id for t in tasks} == {"t_name_1", "t_name_4"}

    def test_name_no_match_returns_empty(self, db):
        assert EvaluationTaskService(db).get_tasks(name="不存在的任务名") == []

    def test_name_combined_with_status(self, db):
        tasks = EvaluationTaskService(db).get_tasks(name="冒烟", status="failed")
        assert {t.id for t in tasks} == {"t_name_4"}

    def test_blank_name_matches_everything(self, db):
        """纯空白 name：strip 后拼 %% 通配，效果等同不过滤（与 None 观感一致）。"""
        tasks = EvaluationTaskService(db).get_tasks(name="   ")
        assert {t.id for t in tasks} >= {row[0] for row in _SEED}
