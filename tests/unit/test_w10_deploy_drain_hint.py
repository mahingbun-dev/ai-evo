"""W10-3 发版前队列 drain 提示与 stop_grace_period 单元测试

整改计划 §3 W10 第 3 项：
- docker-compose.yml worker 的 stop_grace_period 900s → 7200s（与 celery
  task_time_limit 对齐，长任务不再被 docker 强杀）；
- build-service 部署流程发版前列出 running 任务数与排队深度（仅提示不拦截）。
"""
import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
import yaml

# ---------------------------------------------------------------
# 导入前准备：build_service 模块级会执行 load_config()（与 test_build_service 同手法）
# ---------------------------------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="build-service-test-w10-"))
_WS = _TMP / "ws"
_WS.mkdir(parents=True, exist_ok=True)

_CONFIG = _TMP / "config.toml"
_CONFIG.write_text(
    f"""
[auth]
token = "test-token"

[paths]
workspace = "{_WS}"
build_workspace = "{_TMP / 'ws-build'}"
deploy_dir = "{_WS}"
data_dir = "{_TMP / 'data'}"
""",
    encoding="utf-8",
)
os.environ.setdefault("BUILD_SERVICE_CONFIG", str(_CONFIG))

_SERVICE_DIR = Path(__file__).resolve().parents[2] / "tools" / "build-service"
sys.path.insert(0, str(_SERVICE_DIR))

import build_service as bs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


# ======================================================================
# docker-compose.yml worker 优雅退出窗口
# ======================================================================

class TestStopGracePeriod:
    def test_worker_stop_grace_period_is_7200s(self):
        """worker stop_grace_period 必须为 7200s（W10-3：900s 会强杀长任务）"""
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        worker = compose["services"]["worker"]
        assert worker["stop_grace_period"] == "7200s"

    def test_stop_grace_period_covers_task_time_limit(self):
        """stop_grace_period ≥ celery task_time_limit（7200s）"""
        from app.celery_app import celery_app

        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        grace = int(compose["services"]["worker"]["stop_grace_period"].removesuffix("s"))
        assert grace >= celery_app.conf.task_time_limit


# ======================================================================
# 发版前队列 drain 提示
# ======================================================================

class TestEnvValue:
    def test_reads_plain_and_quoted_values(self, tmp_path):
        text = 'DATABASE_URL=postgresql://u:p@h:5432/db\nREDIS_URL="redis://127.0.0.1:6379/0"\n'
        assert bs._env_value(text, "DATABASE_URL") == "postgresql://u:p@h:5432/db"
        assert bs._env_value(text, "REDIS_URL") == "redis://127.0.0.1:6379/0"
        assert bs._env_value(text, "MISSING_KEY") is None


class TestQueueDrainHint:
    def _env_file(self, tmp_path) -> Path:
        env = tmp_path / ".env"
        env.write_text(
            "DATABASE_URL=postgresql://u:p@127.0.0.1:5432/db\n"
            "REDIS_URL=redis://127.0.0.1:6379/0\n",
            encoding="utf-8",
        )
        return env

    def test_lists_running_tasks_and_queue_depth(self, tmp_path, monkeypatch):
        """有任务在跑/排队：日志列出 running 任务数、任务清单与积压消息数，并给出等待提示"""
        monkeypatch.setattr(
            bs, "collect_running_tasks",
            lambda dsn: [{"id": "task-1", "name": "评测任务一"}, {"id": "task-2", "name": "评测任务二"}],
        )
        monkeypatch.setattr(bs, "collect_queue_depth", lambda url: 5)

        logf = io.StringIO()
        bs.queue_drain_hint(logf, env_file=self._env_file(tmp_path))

        out = logf.getvalue()
        assert "running 任务数: 2" in out
        assert "task-1 评测任务一" in out
        assert "task-2 评测任务二" in out
        assert "积压消息数: 5" in out
        assert "可等任务完成后再发版" in out

    def test_idle_queue_no_wait_hint(self, tmp_path, monkeypatch):
        """空闲（无 running、无积压）：列出计数 0，不给等待提示"""
        monkeypatch.setattr(bs, "collect_running_tasks", lambda dsn: [])
        monkeypatch.setattr(bs, "collect_queue_depth", lambda url: 0)

        logf = io.StringIO()
        bs.queue_drain_hint(logf, env_file=self._env_file(tmp_path))

        out = logf.getvalue()
        assert "running 任务数: 0" in out
        assert "积压消息数: 0" in out
        assert "可等任务完成后再发版" not in out

    def test_query_failure_never_blocks_deploy(self, tmp_path, monkeypatch):
        """查询失败（DB/Redis 不可达）：只记一行告警，绝不抛异常阻断部署"""

        def _boom(dsn):
            raise RuntimeError("db down")

        monkeypatch.setattr(bs, "collect_running_tasks", _boom)
        monkeypatch.setattr(bs, "collect_queue_depth", lambda url: 0)

        logf = io.StringIO()
        bs.queue_drain_hint(logf, env_file=self._env_file(tmp_path))
        assert "队列状态查询失败" in logf.getvalue()

    def test_missing_env_file_non_blocking(self, tmp_path):
        """.env 缺失：不抛异常（部署前置校验另有 validate_env_file 负责）"""
        logf = io.StringIO()
        bs.queue_drain_hint(logf, env_file=tmp_path / "not-exists.env")
        assert "发版前队列 drain 提示" in logf.getvalue()


class TestDeployThreadWiring:
    def test_deploy_thread_calls_drain_hint_before_compose_up(self):
        """部署线程在写日志文件后第一步调用 queue_drain_hint（先于 compose up）"""
        import inspect

        src = inspect.getsource(bs.run_deploy_thread)
        hint_pos = src.index("queue_drain_hint(lf)")
        compose_pos = src.index('compose("up"')
        assert hint_pos < compose_pos, "drain 提示必须发生在 compose up 之前"
