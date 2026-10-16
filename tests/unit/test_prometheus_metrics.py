"""ARCH-2-3 Prometheus 指标与监控编排单测（L1，无网络/无 DB）

设计依据：plans/2026-10-15-企业级评测系统架构设计.md §2.3 决策 D7 /
§3 Phase 2 表 2-3 行（Prometheus + Grafana + alertmanager 接入）。

覆盖：
  1. /metrics 端点（真实 app.main app，含 AuthMiddleware）：
     - 匿名可达（与 /health 同层的白名单语义：非 /api/ 前缀放行规则），
       返回 prometheus 文本格式且含本平台指标；
     - 与业务 API 的鉴权面对照（/api/v1/* 无 token 401）；
     - 路由注册顺序先于 SPA catch-all（否则被吞成 index.html）。
  2. celery signals 埋点：真实派发 task_prerun/task_success/task_failure，
     断言时长直方图与失败计数按 queue 标签累计。
  3. 队列深度 gauge：kombu priority 子列表 key 求和 + llen 刷新（fake client）。
  4. worker 指标 server：env 门控 + multiproc registry 选择（不真起端口）。
  5. 沙箱启动延迟：execute_script_in_container 对 driver.run() 的打点
     （成功受理观察、受理失败不观察）。
  6. 编排面静态门禁：compose monitoring profile 接线 + alert.rules 与
     代码侧指标名同源（改名必须两边一起动）。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：无网络/无 DB
import yaml
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import celery_app as celery_app_module
from app.services.script_container import (
    SandboxDriverError,
    SandboxHandle,
    SandboxRunStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
MONITORING_DIR = REPO_ROOT / "docker" / "monitoring"


# ======================================================================
# 1. /metrics 端点（真实 app，白名单层）
# ======================================================================

@pytest.fixture(scope="module")
def real_client():
    """真实 app.main app（含 AuthMiddleware 与全部路由；不用 with——不触发
    lifespan 的 DB 收尸/队列深度循环，本文件只测请求面）"""
    from app.main import app
    return TestClient(app)


class TestMetricsEndpoint:
    def test_metrics_anonymous_200_prometheus_format(self, real_client):
        """匿名（无 Authorization）GET /metrics → 200 + prometheus 文本格式 +
        平台指标在列——与 /health 同层的探活级白名单暴露面（ARCH-2-3）"""
        resp = real_client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "# HELP aievo_celery_queue_depth" in resp.text

    def test_metrics_not_shadowed_by_spa_catch_all(self):
        """/metrics 必须注册在 /{path:path} 之前（FastAPI 按注册顺序匹配，
        顺序反了会被 SPA fallback 吞成 index.html）"""
        from app.main import app

        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/metrics" in paths
        assert paths.index("/metrics") < paths.index("/{path:path}")

    def test_business_api_still_requires_token(self, real_client):
        """鉴权对照面：非白名单的业务 API 无 token 仍 401（AuthMiddleware
        生效且未因 /metrics 放宽）——middleware 在路由前拦截，路径无需真实存在"""
        resp = real_client.get("/api/v1/__metrics_whitelist_probe__")
        assert resp.status_code == 401


# ======================================================================
# 2. celery signals 埋点（task_prerun / task_success / task_failure）
# ======================================================================

def _sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


class _FakeTask:
    """信号最小 sender：request.id + delivery_info.routing_key（queue 标签源）"""

    def __init__(self, task_id="unit-task", queue="eval"):
        self.request = SimpleNamespace(
            id=task_id, delivery_info={"routing_key": queue} if queue else {})


class TestTaskSignalMetrics:
    def test_success_observes_duration_by_queue(self):
        """prerun→success：时长直方图按 queue 标签 +1（真实派发 celery 信号）"""
        from celery import signals

        before = _sample(
            "aievo_celery_task_duration_seconds_count", {"queue": "eval"}) or 0
        task = _FakeTask(task_id="unit-ok", queue="eval")
        signals.task_prerun.send(sender=None, task_id=task.request.id, task=task)
        signals.task_success.send(sender=task, result=None)
        after = _sample(
            "aievo_celery_task_duration_seconds_count", {"queue": "eval"})
        assert after == before + 1

    def test_failure_increments_counter_and_duration(self):
        """prerun→failure：失败计数 +1 且时长入直方图（Retry 不走本信号，
        源码核验见 app/celery_app.py 注释）"""
        from celery import signals

        cnt_before = _sample(
            "aievo_celery_task_failures_total", {"queue": "ingest"}) or 0
        dur_before = _sample(
            "aievo_celery_task_duration_seconds_count", {"queue": "ingest"}) or 0
        task = _FakeTask(task_id="unit-bad", queue="ingest")
        signals.task_prerun.send(sender=None, task_id=task.request.id, task=task)
        signals.task_failure.send(
            sender=task, task_id=task.request.id, exception=RuntimeError("boom"))
        assert _sample(
            "aievo_celery_task_failures_total", {"queue": "ingest"}) == cnt_before + 1
        assert _sample(
            "aievo_celery_task_duration_seconds_count", {"queue": "ingest"}
        ) == dur_before + 1

    def test_success_without_prerun_is_noop(self):
        """无 prerun 起点（如 worker 崩溃重投后残留终态）→ 不观察时长、不报错"""
        from celery import signals

        before = _sample(
            "aievo_celery_task_duration_seconds_count", {"queue": "eval"}) or 0
        signals.task_success.send(sender=_FakeTask(task_id="no-such-id"), result=None)
        assert _sample(
            "aievo_celery_task_duration_seconds_count", {"queue": "eval"}) == before

    def test_queue_label_falls_back_to_unknown(self):
        """delivery_info 缺失（如本地直调）→ queue 标签落 unknown，不抛错"""
        from celery import signals

        before = _sample(
            "aievo_celery_task_failures_total", {"queue": "unknown"}) or 0
        signals.task_failure.send(
            sender=_FakeTask(task_id="unit-nq", queue=None),
            task_id="unit-nq", exception=RuntimeError("boom"))
        assert _sample(
            "aievo_celery_task_failures_total", {"queue": "unknown"}) == before + 1


# ======================================================================
# 3. 队列深度 gauge（Redis llen 定时刷新）
# ======================================================================

class _FakeRedis:
    """llen 假 client：按 key 返回深度（缺省 0，与真实 Redis 一致）"""

    def __init__(self, depths):
        self._depths = depths

    def llen(self, key):
        return self._depths.get(key, 0)


class TestQueueDepth:
    def test_keys_cover_priority_sublists(self):
        """队列 key 集 = base + 各非零 priority 档位子列表（ARCH-2-2 拆档后
        llen 单 key 看不到全量深度；分隔符与档位同 kombu 缺省）"""
        keys = celery_app_module.queue_depth_keys("eval")
        assert keys[0] == "eval"
        assert keys[1:] == ["eval\x06\x163", "eval\x06\x166", "eval\x06\x169"]

    def test_update_sums_all_priority_keys_into_gauge(self):
        """llen 求和（base 7 + 档位 3 子列表 2 = 9）写入对应 queue 标签的
        gauge，并返回 {queue: depth}"""
        depths = {"eval": 7, "eval\x06\x163": 2, "maint": 1}
        result = celery_app_module.update_queue_depth_metrics(_FakeRedis(depths))
        assert result == {"eval": 9, "ingest": 0, "maint": 1}
        assert _sample("aievo_celery_queue_depth", {"queue": "eval"}) == 9
        assert _sample("aievo_celery_queue_depth", {"queue": "ingest"}) == 0
        assert _sample("aievo_celery_queue_depth", {"queue": "maint"}) == 1

    def test_redis_error_propagates(self):
        """Redis 异常向上抛（api 侧循环吞掉保留下次值，不吞成假 0）"""

        class _Boom:
            def llen(self, key):
                raise OSError("redis down")

        with pytest.raises(OSError, match="redis down"):
            celery_app_module.update_queue_depth_metrics(_Boom())


# ======================================================================
# 4. worker 指标 server（env 门控；不真起端口）
# ======================================================================

class TestWorkerMetricsServer:
    def test_disabled_without_env(self, monkeypatch):
        """PROMETHEUS_WORKER_METRICS_PORT 未配置 → 不启动（本地开发零开销）"""
        called = []
        monkeypatch.delenv("PROMETHEUS_WORKER_METRICS_PORT", raising=False)
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.setattr(celery_app_module, "start_http_server",
                            lambda *a, **kw: called.append((a, kw)))
        assert celery_app_module.start_worker_metrics_server() is None
        assert called == []

    def test_port_env_starts_server_with_default_registry(self, monkeypatch):
        """配了端口、未配 MULTIPROC_DIR → 默认 registry（单进程口径）"""
        called = []
        monkeypatch.setenv("PROMETHEUS_WORKER_METRICS_PORT", "9108")
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        monkeypatch.setattr(celery_app_module, "start_http_server",
                            lambda *a, **kw: called.append((a, kw)))
        assert celery_app_module.start_worker_metrics_server() == 9108
        (args, kwargs), = called
        assert args == (9108,)
        assert kwargs["registry"] is celery_app_module.REGISTRY

    def test_multiproc_env_uses_aggregating_registry(self, monkeypatch, tmp_path):
        """配了 MULTIPROC_DIR → 独立聚合 registry（MultiProcessCollector；
        prefork 子进程指标经 per-pid mmap 文件聚合，见 celery_app 注释）"""
        from prometheus_client import multiprocess as pc_multiprocess

        called = []
        monkeypatch.setenv("PROMETHEUS_WORKER_METRICS_PORT", "9108")
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        monkeypatch.setattr(celery_app_module, "start_http_server",
                            lambda *a, **kw: called.append((a, kw)))
        assert celery_app_module.start_worker_metrics_server() == 9108
        (_, kwargs), = called
        registry = kwargs["registry"]
        assert registry is not celery_app_module.REGISTRY
        # MultiProcessCollector 不注册静态指标名（collect 时动态扫描目录），
        # 断言其在 registry 的 collector 表里
        assert any(isinstance(c, pc_multiprocess.MultiProcessCollector)
                   for c in registry._collector_to_names)

    def test_worker_ready_hook_never_raises(self, monkeypatch):
        """指标面故障绝不影响 worker 启动：start 抛错被钩子整体兜底"""
        monkeypatch.setenv("PROMETHEUS_WORKER_METRICS_PORT", "9108")

        def boom(*a, **kw):
            raise OSError("port in use")

        monkeypatch.setattr(celery_app_module, "start_http_server", boom)
        celery_app_module._metrics_on_worker_ready()  # 不抛即通过


# ======================================================================
# 5. 沙箱启动（受理）延迟打点（script_container）
# ======================================================================

class _FakeSandboxDriver:
    """受理即返回的假驱动：run 计时由被测函数完成，wait 直接给终态"""

    name = "unit-fake"

    def __init__(self, fail_run=False, exit_code=0):
        self.fail_run = fail_run
        self.exit_code = exit_code

    def run(self, spec):
        if self.fail_run:
            raise SandboxDriverError("受理失败")
        return SandboxHandle(run_id=spec.container_name,
                             container_name=spec.container_name)

    def wait(self, handle, timeout):
        return SandboxRunStatus(state="done", exit_code=self.exit_code)

    def kill(self, handle):
        pass

    def status(self, handle):
        return SandboxRunStatus(state="done", exit_code=self.exit_code)


def _sandbox_latency_count():
    return _sample("aievo_sandbox_start_latency_seconds_count",
                   {"driver": "unit-fake"}) or 0


class TestSandboxStartLatency:
    def _run_once(self, tmp_path, driver):
        from app.services.script_container import execute_script_in_container

        run_dir = tmp_path / "run"
        cwd = tmp_path / "run" / "sid"
        staged = tmp_path / "run" / "sid" / "scripts"
        for d in (cwd, staged):
            d.mkdir(parents=True)
        return execute_script_in_container(
            script={"script_id": "sid", "name": "unit"},
            staged_dir_host=staged, entry_rel="main.py",
            run_dir_host=run_dir, cwd_host=cwd,
            stdin={}, timeout=5, started=0.0, driver=driver,
        )

    def test_successful_run_observed(self, tmp_path):
        """受理成功 → 延迟入直方图（driver 标签；后续 stdout 解析失败属
        另一条结果分支，不影响打点断言——本用例无 stdout.log 必然 fail）"""
        before = _sandbox_latency_count()
        result = self._run_once(tmp_path, _FakeSandboxDriver())
        assert result["status"] == "fail"  # stdout 为空 → JSON 解析失败（预期）
        assert _sandbox_latency_count() == before + 1

    def test_failed_run_not_observed(self, tmp_path):
        """受理失败（SandboxDriverError）→ 不进延迟分布（失败归失败指标/日志）"""
        before = _sandbox_latency_count()
        result = self._run_once(tmp_path, _FakeSandboxDriver(fail_run=True))
        assert result["status"] == "fail"
        assert result.get("finish_reason") == "error"
        assert _sandbox_latency_count() == before


# ======================================================================
# 6. 编排面静态门禁（compose + docker/monitoring/ 与代码同源）
# ======================================================================

@pytest.fixture(scope="module")
def services():
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return data["services"]


def _env_map(svc) -> dict:
    return dict(entry.split("=", 1) for entry in svc.get("environment", []))


class TestMonitoringCompose:
    def test_stack_gated_by_monitoring_profile(self, services):
        """prometheus/grafana/alertmanager 三件套只随 monitoring profile 启动
        （既有部署不设 profile 行为不变，与 sandbox profile 同机制）"""
        for name in ("prometheus", "grafana", "alertmanager"):
            assert services[name]["profiles"] == ["monitoring"], name

    def test_worker_metrics_env_wired(self, services):
        """两个 worker 容器统一注入指标端口 + MULTIPROC_DIR（tmpfs 上的目录，
        prefork 子进程聚合写）；worker 侧代码见 app/celery_app.py"""
        for name in ("worker", "worker-ingest"):
            env = _env_map(services[name])
            assert env.get("PROMETHEUS_WORKER_METRICS_PORT") == "9108", name
            assert env.get("PROMETHEUS_MULTIPROC_DIR") == "/tmp/prometheus_multiproc", name

    def test_prometheus_mounts_config_and_rules(self, services):
        """prometheus.yml/alert.rules 只读挂载自仓库 docker/monitoring/
        （改配置改仓库即生效；alertmanager/grafana 同理挂各自配置）"""
        mounts = services["prometheus"]["volumes"]
        assert "./docker/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro" in mounts
        assert "./docker/monitoring/alert.rules:/etc/prometheus/alert.rules:ro" in mounts
        assert any("/etc/alertmanager/alertmanager.yml" in m
                   for m in services["alertmanager"]["volumes"])
        assert any("/etc/grafana/provisioning/datasources" in m
                   for m in services["grafana"]["volumes"])

    def test_internal_only_except_grafana(self, services):
        """监控栈仅 grafana 发布宿主端口（面板要人看）；prometheus/
        alertmanager 只在 compose 内网（抓取/告警链路不经宿主）"""
        assert "ports" not in services["prometheus"]
        assert "ports" not in services["alertmanager"]
        assert services["grafana"]["ports"] == ["${GRAFANA_PORT:-3000}:3000"]


class TestAlertRulesSyncWithCode:
    """alert.rules 的指标名与代码侧定义同源（改名必须两边一起动）"""

    def test_rule_metrics_match_code_definitions(self):
        rules = (MONITORING_DIR / "alert.rules").read_text(encoding="utf-8")
        assert celery_app_module.QUEUE_DEPTH_METRIC._name in rules
        assert celery_app_module.TASK_FAILURES_METRIC._name in rules
        assert f"{celery_app_module.TASK_DURATION_METRIC._name}_count" in rules

    def test_prometheus_scrape_targets_match_topology(self):
        """抓取目标 = api:8100（/metrics）+ 两 worker:9108（指标 server 端口
        与 compose 注入的 PROMETHEUS_WORKER_METRICS_PORT 同值）"""
        cfg = yaml.safe_load((MONITORING_DIR / "prometheus.yml").read_text(encoding="utf-8"))
        jobs = {c["job_name"]: c for c in cfg["scrape_configs"]}
        assert [t for sc in jobs["ai-evo-api"]["static_configs"]
                for t in sc["targets"]] == ["api:8100"]
        worker_targets = sorted(
            t for sc in jobs["ai-evo-worker"]["static_configs"] for t in sc["targets"])
        assert worker_targets == ["worker-ingest:9108", "worker:9108"]
        assert cfg["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == [
            "alertmanager:9093"]
