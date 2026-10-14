"""W10-5 数据库引擎复用单元测试

整改计划 §3 W10 第 5 项：
- 移除 _retry_add_log 中的 engine.dispose()——dispose 清空整个进程连接池，
  并发线程正在使用的健康连接一并作废，重试只需换新会话；
- task_scheduler._get_db_session 改用 app.db.session 全局 engine/SessionLocal，
  不再每次调用 create_engine 新建引擎。
"""
import inspect
import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app.services import task_scheduler
from app.services.evaluation_task_service import TaskExecutionLogService


class _FakeSession:
    """最小 Session 假实现：commit 可配置异常序列，记录 add/close"""

    def __init__(self, commit_errors=()):
        self.added = []
        self.closed = False
        self._commit_errors = list(commit_errors)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        if self._commit_errors:
            raise self._commit_errors.pop(0)

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self):
        self.dispose_calls = 0

    def dispose(self):
        self.dispose_calls += 1


# ---------------------------------------------------------------------------
# _retry_add_log 不再 dispose 全局引擎
# ---------------------------------------------------------------------------

class TestRetryAddLogNoDispose:
    def test_retry_succeeds_without_disposing_engine(self, monkeypatch):
        """首次 commit 失败 → 换新会话重试成功；全程 engine.dispose 零调用"""
        fake_engine = _FakeEngine()
        sessions = [
            _FakeSession(commit_errors=[OperationalError("stmt", {}, Exception("conn broken"))]),
            _FakeSession(),  # 重试会话
        ]
        created = []
        monkeypatch.setattr("app.db.session.engine", fake_engine)

        def _factory():
            s = sessions.pop(0)
            created.append(s)
            return s

        monkeypatch.setattr("app.db.session.SessionLocal", _factory)

        log = object()  # 仅透传给 session.add，无需真实模型实例
        TaskExecutionLogService(db=None)._retry_add_log(log, max_retries=2)

        assert fake_engine.dispose_calls == 0, "重试路径不得 dispose 全局连接池（W10-5）"
        assert len(created) == 2, "应换新会话重试一次"
        assert created[0].added == [log] and created[0].closed
        assert created[1].added == [log] and created[1].closed

    def test_retry_exhaustion_still_no_dispose(self, monkeypatch):
        """重试次数耗尽：同样不 dispose（只记日志放弃）"""
        fake_engine = _FakeEngine()
        monkeypatch.setattr("app.db.session.engine", fake_engine)
        monkeypatch.setattr(
            "app.db.session.SessionLocal",
            lambda: _FakeSession(commit_errors=[OperationalError("s", {}, Exception("down"))]),
        )

        TaskExecutionLogService(db=None)._retry_add_log(object(), max_retries=2)

        assert fake_engine.dispose_calls == 0


# ---------------------------------------------------------------------------
# task_scheduler 复用全局 SessionLocal
# ---------------------------------------------------------------------------

class TestSchedulerGlobalEngine:
    def test_get_db_session_uses_global_session_local(self, monkeypatch):
        """_get_db_session 走 app.db.session.SessionLocal（返回其产物）"""
        sentinel = object()
        calls = []

        def _factory():
            calls.append(1)
            return sentinel

        monkeypatch.setattr("app.db.session.SessionLocal", _factory)
        assert task_scheduler._get_db_session() is sentinel
        assert calls == [1]

    def test_get_db_session_no_longer_creates_engine(self):
        """回归护栏：_get_db_session 可执行体不再调用 create_engine/sessionmaker/get_settings"""
        import ast

        src = inspect.getsource(task_scheduler._get_db_session)
        called_names = {
            node.func.id
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not called_names & {"create_engine", "sessionmaker", "get_settings"}, called_names
