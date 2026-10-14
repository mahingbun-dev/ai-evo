"""W10-2 EvalTaskLock 持锁者存活校验与接管单元测试

整改计划 §3 W10 第 2 项：锁值写 hostname:pid；抢锁失败时检查持锁进程存活
（同机 /proc 视角 os.kill(pid,0)、异机 celery inspect），已死则原子接管并
缩短 TTL，消除「worker 崩溃后最长 3 小时不能重跑同一任务」。

redis 全部 fake（get_redis_client 打桩，含接管/续期两个 Lua 脚本的语义实现）；
存活探测对同机 pid 走真实 os.kill(pid, 0)（用「spawn 后立即收尸」的确定性死 pid）。
"""
import os
import socket
import subprocess
import sys
import time
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.services import redis_stop_signal
from app.services.redis_stop_signal import (
    EVAL_TASK_LOCK_TAKEOVER_TTL,
    EVAL_TASK_LOCK_TTL,
    EvalTaskLock,
)
from app.tasks import celery_tasks  # noqa: F401 - 触发 run_evaluation_task 可导入

LOCK_KEY = "eval_task_lock:w10-takeover"


class FakeRedis:
    """最小 redis 假实现：set(nx)/get/delete/expire/exists + W10-2 两个 Lua 语义"""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None  # redis SET NX 未抢到的语义
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)
            self.ttls.pop(key, None)

    def exists(self, key):
        return 1 if key in self.store else 0

    def expire(self, key, ttl):
        if key in self.store:
            self.ttls[key] = int(ttl)
            return 1
        return 0

    def eval(self, script, numkeys, *args):
        """只实现 EvalTaskLock 接管/续期脚本的语义（原子比较在假实现内天然成立）"""
        key = args[0]
        if "EXPIRE" in script:  # RENEW: (key, holder, ttl)
            if self.store.get(key) == args[1]:
                self.ttls[key] = int(args[2])
                return 1
            return 0
        # TAKEOVER: (key, dead_holder, new_holder, ttl)
        if self.store.get(key) == args[1]:
            self.store.pop(key)
            return self.set(key, args[2], nx=True, ex=int(args[3]))
        return None


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_stop_signal, "get_redis_client", lambda: fake)
    return fake


def _spawn_and_reap_dead_pid() -> int:
    """产出一个确定已死的本机 pid：spawn sleep → SIGKILL → wait 收尸。

    必须 wait 收尸：僵尸进程在进程表内 os.kill(pid, 0) 仍成功。
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    proc.kill()
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------------------
# 存活校验与接管
# ---------------------------------------------------------------------------

class TestHolderLivenessTakeover:
    def test_takeover_when_holder_pid_dead(self, fake_redis):
        """持锁 pid 已死（spawn→kill→收尸）：抢锁失败后接管，锁值换成当前进程"""
        dead_pid = _spawn_and_reap_dead_pid()
        fake_redis.store[LOCK_KEY] = f"{socket.gethostname()}:{dead_pid}"

        lock = EvalTaskLock("w10-takeover")
        assert lock.acquire() is True
        assert lock.holder() == f"{socket.gethostname()}:{os.getpid()}"
        assert fake_redis.ttls[LOCK_KEY] == EVAL_TASK_LOCK_TAKEOVER_TTL

    def test_no_takeover_when_holder_alive(self, fake_redis):
        """持锁 pid 存活（真实存活子进程）：维持拒绝语义，不接管"""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            fake_redis.store[LOCK_KEY] = f"{socket.gethostname()}:{proc.pid}"
            assert EvalTaskLock("w10-takeover").acquire() is False
            assert fake_redis.store[LOCK_KEY] == f"{socket.gethostname()}:{proc.pid}"
        finally:
            proc.kill()
            proc.wait()

    def test_unparseable_holder_treated_alive(self, fake_redis):
        """无法解析的锁值（无 pid 段 / pid 非数字 / pid 非法）按存活处理"""
        for bad in ("no-pid-here", f"{socket.gethostname()}:notanumber",
                    f"{socket.gethostname()}:-5", f"{socket.gethostname()}:0"):
            fake_redis.store[LOCK_KEY] = bad
            assert EvalTaskLock("w10-takeover").acquire() is False, bad

    def test_remote_holder_uses_inspect_probe(self, fake_redis, monkeypatch):
        """异机持锁者：走 celery inspect 探测——不在线接管、在线拒绝、异常保守拒绝"""
        import app.services.redis_stop_signal as m

        fake_redis.store[LOCK_KEY] = "remote-worker-abc:1234"
        monkeypatch.setattr(m, "_remote_hostname_alive", lambda h: False)
        assert EvalTaskLock("w10-takeover").acquire() is True  # 异机 worker 已下线 → 接管

        fake_redis.store[LOCK_KEY] = "remote-worker-abc:1234"
        monkeypatch.setattr(m, "_remote_hostname_alive", lambda h: True)
        assert EvalTaskLock("w10-takeover").acquire() is False  # 异机 worker 在线 → 拒绝

        def _boom(hostname):
            raise RuntimeError("inspect 不可用")

        fake_redis.store[LOCK_KEY] = "remote-worker-abc:1234"
        monkeypatch.setattr(m, "_remote_hostname_alive", _boom)
        assert EvalTaskLock("w10-takeover").acquire() is False  # 探测异常保守按存活

    def test_takeover_aborts_when_lock_changed_between_check_and_eval(self, fake_redis, monkeypatch):
        """检查到接管之间锁被他人抢走：Lua 比较失败，他人锁不被误删"""
        dead_pid = _spawn_and_reap_dead_pid()
        dead_holder = f"{socket.gethostname()}:{dead_pid}"
        actual_holder = "other-worker:4242"
        fake_redis.store[LOCK_KEY] = actual_holder
        # get 返回死者值（模拟检查瞬间读到的旧值），store 实际已被他人占用
        monkeypatch.setattr(fake_redis, "get", lambda k: dead_holder, raising=False)

        assert EvalTaskLock("w10-takeover").acquire() is False
        assert fake_redis.store[LOCK_KEY] == actual_holder, "他人的锁不得被误删"

    def test_takeover_eval_failure_keeps_reject(self, fake_redis, monkeypatch):
        """接管脚本执行异常（redis 语法/版本问题）：保持拒绝语义（TTL 兜底）"""
        dead_pid = _spawn_and_reap_dead_pid()
        fake_redis.store[LOCK_KEY] = f"{socket.gethostname()}:{dead_pid}"

        def _boom(*args, **kwargs):
            raise RuntimeError("lua eval failed")

        monkeypatch.setattr(fake_redis, "eval", _boom)
        assert EvalTaskLock("w10-takeover").acquire() is False


# ---------------------------------------------------------------------------
# renew 续期
# ---------------------------------------------------------------------------

class TestRenew:
    def test_renew_extends_own_lock(self, fake_redis):
        fake_redis.store[LOCK_KEY] = f"{socket.gethostname()}:{os.getpid()}"
        fake_redis.ttls[LOCK_KEY] = EVAL_TASK_LOCK_TAKEOVER_TTL

        assert EvalTaskLock("w10-takeover").renew() is True
        assert fake_redis.ttls[LOCK_KEY] == EVAL_TASK_LOCK_TTL

    def test_renew_ignores_foreign_lock(self, fake_redis):
        fake_redis.store[LOCK_KEY] = "other-worker:4242"
        fake_redis.ttls[LOCK_KEY] = 42

        assert EvalTaskLock("w10-takeover").renew() is False
        assert fake_redis.ttls[LOCK_KEY] == 42  # 他人锁 TTL 不变


# ---------------------------------------------------------------------------
# celery 入口 run_evaluation_task 的接管接线
# ---------------------------------------------------------------------------

def _invoke_run_evaluation_task(task_id):
    from app.tasks.celery_tasks import run_evaluation_task
    return run_evaluation_task(task_id)


class TestRunEvaluationTaskTakeoverWiring:
    def test_dead_holder_taken_over_and_evaluation_runs(self, fake_redis):
        """崩溃残留锁：入口接管并正常执行评测（消除 3h 不能重跑）"""
        dead_pid = _spawn_and_reap_dead_pid()
        fake_redis.store[f"eval_task_lock:task-dead"] = f"{socket.gethostname()}:{dead_pid}"

        with patch("app.tasks.evaluation_tasks_async.run_evaluation_async",
                   return_value={"status": "success", "task_id": "task-dead"}) as mock_run:
            result = _invoke_run_evaluation_task("task-dead")
        assert result["status"] == "success"
        mock_run.assert_called_once()
        assert "eval_task_lock:task-dead" not in fake_redis.store, "任务结束后锁应释放"

    def test_live_holder_still_rejected(self, fake_redis):
        """持锁进程存活（当前测试进程自己持锁）：仍拒绝重投"""
        fake_redis.store["eval_task_lock:task-live"] = f"{socket.gethostname()}:{os.getpid()}"
        with patch("app.tasks.evaluation_tasks_async.run_evaluation_async") as mock_run:
            result = _invoke_run_evaluation_task("task-live")
        assert result["status"] == "duplicate_rejected"
        mock_run.assert_not_called()
