"""W10 独立验证测试（测试角色，与开发交付测试互补）。

覆盖开发测试未触达的实现语义与边界：
- W10-1 visibility_timeout 的生效前提（broker 确为 redis 协议）；
- W10-2 _holder_dead 的锁值解析语义（rpartition 对 IPv6 风格值）、
  本机活/死 pid 真实探测、PermissionError 保守按存活；
- W10-3 drain 提示 running>20 截断、「积压>0 但无 running」组合提示；
- W10-4 reaper 不收非 running 任务；节流表容量清理路径；execution_number=None
  不登记节流表；
- W10-5 _retry_add_log 源码级 AST 护栏（无 dispose / 无 engine 引用）；
- W10-6 脚本子进程真实为进程组长（os.getpgid(0)==os.getpid() 端到端带出）；
- W10-7 json 60 / sse 300 / 显式 timeout 优先（独立行为复验）。
"""
import ast
import inspect
import io
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


# ===========================================================================
# W10-1 visibility_timeout 生效前提
# ===========================================================================

class TestW10T1VisibilityTimeout:
    def test_broker_is_redis_protocol(self):
        """visibility_timeout 仅对 Redis broker 有意义——broker_url 必须是 redis://"""
        from app.celery_app import celery_app

        assert celery_app.conf.broker_url.startswith("redis"), (
            f"broker 非 redis（{celery_app.conf.broker_url}），visibility_timeout 不适用"
        )

    def test_visibility_timeout_value_and_relation(self):
        """7800s：≥ task_time_limit(7200) 且 ≤ 7800（防配置被悄悄改大偏离任务书）"""
        from app.celery_app import celery_app

        opts = celery_app.conf.broker_transport_options
        assert opts.get("visibility_timeout") == 7800
        assert celery_app.conf.task_time_limit == 7200, "前提：task_time_limit=7200s"
        assert opts["visibility_timeout"] >= celery_app.conf.task_time_limit


# ===========================================================================
# W10-2 EvalTaskLock 存活校验语义
# ===========================================================================

def _dead_local_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.kill()
    proc.wait()  # 必须收尸：僵尸进程 os.kill(pid,0) 仍成功
    return proc.pid


class TestW10T2HolderDeadSemantics:
    def test_local_alive_pid_not_dead(self):
        """本机存活 pid（测试进程自己）：判定存活（不可接管）"""
        from app.services.redis_stop_signal import EvalTaskLock

        assert EvalTaskLock._holder_dead(f"{socket.gethostname()}:{os.getpid()}") is False

    def test_local_dead_pid_is_dead(self):
        """本机确定已死的 pid（spawn→SIGKILL→收尸）：判定已死（可接管）"""
        from app.services.redis_stop_signal import EvalTaskLock

        dead_pid = _dead_local_pid()
        assert EvalTaskLock._holder_dead(f"{socket.gethostname()}:{dead_pid}") is True

    def test_rpartition_parses_hostname_with_colons(self, monkeypatch):
        """锁值含多冒号 hostname（IPv6 风格 ::1:123）：rpartition 取尾段 pid，hostname 完整传给异机探测"""
        import app.services.redis_stop_signal as m

        seen = {}
        monkeypatch.setattr(
            m, "_remote_hostname_alive",
            lambda h: (seen.setdefault("hostname", h) or True),
        )
        assert m.EvalTaskLock._holder_dead("::1:1234") is False  # 异机探测按存活
        assert seen["hostname"] == "::1", "hostname 段不得被冒号截断"

    def test_permission_error_treated_alive(self, monkeypatch):
        """os.kill(pid,0) 权限拒绝（进程存在属其它用户）：按存活保守处理"""
        import app.services.redis_stop_signal as m

        def _deny(pid, sig):
            raise PermissionError("not yours")

        monkeypatch.setattr(m.os, "kill", _deny)
        assert m._local_pid_alive(12345) is True
        assert m.EvalTaskLock._holder_dead(f"{socket.gethostname()}:12345") is False

    def test_takeover_ttl_constant(self):
        """接管短 TTL = 15 分钟（任务书：接管并缩短 TTL）"""
        from app.services.redis_stop_signal import EVAL_TASK_LOCK_TAKEOVER_TTL

        assert EVAL_TASK_LOCK_TAKEOVER_TTL == 15 * 60


# ===========================================================================
# W10-3 drain 提示边界
# ===========================================================================

def _import_build_service():
    """导入 build_service（模块级 load_config 需要配置文件前置；与开发测试同手法）"""
    import sys as _sys
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="w10t-build-svc-"))
    (tmp / "ws").mkdir()
    cfg = tmp / "config.toml"
    cfg.write_text(
        f"""
[auth]
token = "t"
[paths]
workspace = "{tmp / 'ws'}"
build_workspace = "{tmp / 'wsb'}"
deploy_dir = "{tmp / 'ws'}"
data_dir = "{tmp / 'data'}"
""",
        encoding="utf-8",
    )
    os.environ.setdefault("BUILD_SERVICE_CONFIG", str(cfg))
    svc_dir = REPO_ROOT / "tools" / "build-service"
    if str(svc_dir) not in _sys.path:
        _sys.path.insert(0, str(svc_dir))
    import build_service
    return build_service


class TestW10T3DrainHintEdges:
    @staticmethod
    def _env_file(tmp_path) -> Path:
        env = tmp_path / ".env"
        env.write_text(
            "DATABASE_URL=postgresql://u:p@127.0.0.1:5432/db\n"
            "REDIS_URL=redis://127.0.0.1:6379/0\n",
            encoding="utf-8",
        )
        return env

    def test_running_tasks_over_20_truncated(self, tmp_path, monkeypatch):
        """running >20 条：日志只列前 20 条并提示「其余 N 条略」"""
        bs = _import_build_service()
        monkeypatch.setattr(
            bs, "collect_running_tasks",
            lambda dsn: [{"id": f"task-{i}", "name": f"任务{i}"} for i in range(25)],
        )
        monkeypatch.setattr(bs, "collect_queue_depth", lambda url: 0)

        logf = io.StringIO()
        bs.queue_drain_hint(logf, env_file=self._env_file(tmp_path))
        out = logf.getvalue()

        assert "running 任务数: 25" in out
        listed = [line for line in out.splitlines() if "- task-" in line]
        assert len(listed) == 20, f"应只列 20 条，实际 {len(listed)}"
        assert "其余 5 条略" in out

    def test_backlog_only_still_hints_wait(self, tmp_path, monkeypatch):
        """无 running 但有积压：仍给等待提示（积压也会被本次发版影响）"""
        bs = _import_build_service()
        monkeypatch.setattr(bs, "collect_running_tasks", lambda dsn: [])
        monkeypatch.setattr(bs, "collect_queue_depth", lambda url: 3)

        logf = io.StringIO()
        bs.queue_drain_hint(logf, env_file=self._env_file(tmp_path))
        assert "积压消息数: 3" in logf.getvalue()
        assert "可等任务完成后再发版" in logf.getvalue()

    def test_compose_stop_grace_period_value(self):
        """worker stop_grace_period == 7200s（独立复验 compose 文件）"""
        compose = yaml.safe_load(
            (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        assert compose["services"]["worker"]["stop_grace_period"] == "7200s"
        # 其它服务不得被误改
        for name, svc in compose["services"].items():
            if name != "worker":
                assert "stop_grace_period" not in svc or svc["stop_grace_period"] == "7200s"


# ===========================================================================
# W10-4 reaper 边界与节流表
# ===========================================================================

class TestW10T4ReaperAndThrottle:
    def test_touch_heartbeat_none_execution_not_registered_in_throttle_table(self):
        """execution_number=None（非任务链路）：直接跳过，不得登记节流表"""
        from app.services import evaluation_task_service as ets

        task_id = "w10t_none_exec"
        ets._heartbeat_last_write.pop(task_id, None)
        try:
            EvaluationTaskServiceNoneDb = ets.EvaluationTaskService(db=None)
            EvaluationTaskServiceNoneDb.touch_heartbeat(task_id, None)
            assert task_id not in ets._heartbeat_last_write, (
                "非任务链路调用不得污染节流表"
            )
        finally:
            ets._heartbeat_last_write.pop(task_id, None)

    def test_throttle_table_cleanup_when_over_capacity(self):
        """节流表超容量：清理过期项后新 key 正常登记（防无界增长路径可用）"""
        from app.services import evaluation_task_service as ets

        saved = dict(ets._heartbeat_last_write)
        try:
            ets._heartbeat_last_write.clear()
            stale_ts = time.monotonic() - 10 * ets.HEARTBEAT_MIN_INTERVAL_SECONDS
            for i in range(ets._HEARTBEAT_TABLE_MAX + 8):
                ets._heartbeat_last_write[f"w10t_cap_{i}"] = stale_ts

            # 新 key 不在表 → 触发容量清理 → 登记 → 返回 False（未节流）
            assert ets._heartbeat_throttled("w10t_cap_new") is False
            assert "w10t_cap_new" in ets._heartbeat_last_write
            assert not any(k.startswith("w10t_cap_") and k != "w10t_cap_new"
                           for k in ets._heartbeat_last_write), "过期项应被清理"
        finally:
            ets._heartbeat_last_write.clear()
            ets._heartbeat_last_write.update(saved)

    def test_throttle_rejects_second_call_within_window(self):
        """同 key 窗口内第二次调用返回 True（节流生效；不依赖 DB）"""
        from app.services import evaluation_task_service as ets

        saved = dict(ets._heartbeat_last_write)
        try:
            ets._heartbeat_last_write.clear()
            assert ets._heartbeat_throttled("w10t_win") is False
            assert ets._heartbeat_throttled("w10t_win") is True
        finally:
            ets._heartbeat_last_write.clear()
            ets._heartbeat_last_write.update(saved)


# ===========================================================================
# W10-5 _retry_add_log 源码级护栏
# ===========================================================================

class TestW10T5RetryAddLogSourceGuard:
    def test_source_has_no_dispose_and_no_engine_import(self):
        """AST 护栏：_retry_add_log 函数体内不得再出现 dispose 调用或 engine 引用
        （开发测试用 mock 计数验证行为；此用例锁源码，防将来手滑加回）"""
        from app.services.evaluation_task_service import TaskExecutionLogService

        src = textwrap.dedent(inspect.getsource(TaskExecutionLogService._retry_add_log))
        tree = ast.parse(src)

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        ]
        dispose_calls = [
            c for c in calls
            if isinstance(c.func, ast.Attribute) and c.func.attr == "dispose"
        ]
        assert not dispose_calls, "重试路径不得调用 engine.dispose（W10-5）"

        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "engine" not in imported_names, (
            f"函数内不得再 import engine（实际: {imported_names}）"
        )
        assert "SessionLocal" in imported_names, "重试应换新会话（SessionLocal）"


# ===========================================================================
# W10-6 脚本子进程为进程组长（端到端直接验证）
# ===========================================================================

_GROUP_LEADER_SRC = '''
import os


def run(stdin):
    """返回本进程是否为进程组长（start_new_session=True 的直接证据）"""
    return {
        "status": "ok",
        "output": {"is_group_leader": os.getpgid(0) == os.getpid()},
        "files": {},
        "summary": "probe",
        "finish_reason": "completed",
        "error": None,
    }
'''


class TestW10T6GroupLeaderEndToEnd:
    def test_child_process_is_group_leader(self, tmp_path, monkeypatch):
        """子进程 os.getpgid(0)==os.getpid()：start_new_session=True 真实生效"""
        from app.services import pre_judge_runner, pre_judge_script_service

        upload_base = tmp_path / "pre_judge_scripts"
        run_base = tmp_path / "script_runs"
        monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", upload_base)
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", run_base)

        pkg_dir = upload_base / "w10t_leader"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "main.py").write_text(_GROUP_LEADER_SRC, encoding="utf-8")

        stdin = {
            "task_id": "w10t_leader_task",
            "data_id": "d1",
            "_run_dir": str(run_base / "workspace" / "w10t_leader" / "item"),
        }
        result = pre_judge_runner._execute_script_subprocess(
            {"script_id": "w10t_leader", "name": "w10t_leader", "entrypoint": "main.py"},
            stdin, 30, time.time(),
        )
        assert result["status"] == "ok", result
        assert result["output"]["is_group_leader"] is True, (
            "子进程应为进程组长（pgid==pid），start_new_session=True 未生效？"
        )


# ===========================================================================
# W10-7 请求链超时默认值（独立行为复验）
# ===========================================================================

class _FakeResp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True}


class TestW10T7RequestChainDefaults:
    def _executor(self):
        from app.services.request_chain import RequestChainExecutor

        return RequestChainExecutor({"requests": []})

    @pytest.mark.asyncio
    async def test_json_default_60(self, monkeypatch):
        captured = {}

        async def _send(method, url, headers, body, params, timeout):
            captured["t"] = timeout
            return _FakeResp()

        ex = self._executor()
        ex._send_request = _send
        r = await ex._execute_request({"name": "n", "url": "http://ok.local/x", "method": "GET"})
        assert r["success"] is True
        assert captured["t"] == 60

    @pytest.mark.asyncio
    async def test_sse_default_300(self):
        captured = {}

        async def _stream(method, url, headers, body, params, timeout, parse_config=None, stream_callback=None):
            captured["t"] = timeout
            return {"success": True, "content": "x"}

        ex = self._executor()
        ex._send_stream_request = _stream
        r = await ex._execute_request(
            {"name": "n", "url": "http://ok.local/s", "method": "GET", "response_type": "sse"}
        )
        assert r["success"] is True
        assert captured["t"] == 300

    @pytest.mark.asyncio
    async def test_explicit_timeout_wins(self):
        captured = {}

        async def _send(method, url, headers, body, params, timeout):
            captured["t"] = timeout
            return _FakeResp()

        ex = self._executor()
        ex._send_request = _send
        await ex._execute_request(
            {"name": "n", "url": "http://ok.local/x", "method": "GET", "timeout": 999}
        )
        assert captured["t"] == 999
