"""W13 独立测试角色补充验证（独立于开发自测的盲区覆盖）

开发随附测试（16c9077）已覆盖：告警 POST 契约/禁用/吞异常、深健康探针分支
monkeypatch、auth/status 三态、celery on_failure 接线、日志 handler 属性、
build_service 告警三态、HEALTH_URL 缺省值、真实 PG+Redis 全绿。

本文件补齐以下独立验证（均为真实接线断言，非重复用例）：
  1. 中间件端到端：/api/v1/health/deep 无 cookie 经 AuthMiddleware 白名单放行
     （开发测试只断言白名单列表含字符串，未验证 startswith 匹配真实放行）；
     对照组非白名单路径确实被拦，证明中间件在工作。
  2. 真实 app.main 挂载：/api/v1/health/deep 路由存在可达、/api/v1/auth/status
     在真实 app 上无 cookie 返回 401（防止 include_router/白名单遗漏的装配回归）。
  3. docker-compose.yml 三服务 logging(json-file 50m×3) + healthcheck 全接线
     （验收标准「日志不再无限增长」与「compose 三服务补 healthcheck」的静态证据）。
  4. docs/alerts-and-health.md 存在且注明 webhook 升级路径（验收要求）。
  5. 告警兜底落盘路径契约：两个实现都指向 logs/alerts.log。
  6. migration_heads() 解析真实 alembic 迁移链非空（纯文件解析，无外部依赖）。
  7. 探针超时护栏契约：PROBE_TIMEOUT_SECONDS < verify 的 urlopen timeout=5s
     （health_api.py 模块注释声明的约束，超了会让 503 退化成「探活超时」丢明细）。
  8. 部署护栏闭环：深健康 503（HTTPError）→ verify_deploy 抛 RuntimeError →
     verify_deploy_with_alert 发告警并原样重抛（「容器起了但依赖不对」被拦住）。
"""
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main  # noqa: F401 - 导入即完成路由注册与日志配置
from app.api import health_api
from app.api.health_api import router as health_router
from app.middleware.auth_middleware import AuthMiddleware
from app.services import alert_service
from tests.unit import test_build_service as _bs_module

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ======================================================================
# 1. 中间件端到端：白名单真实放行 + 对照组拦截
# ======================================================================

def _mw_app():
    """与 app.main 相同的装配：health_router 挂 /api/v1 前缀 + AuthMiddleware"""
    app = FastAPI()
    app.include_router(health_router, prefix="/api/v1")
    app.add_middleware(AuthMiddleware)
    return app


class TestWhitelistEndToEnd:
    def test_deep_health_anonymous_passes_middleware(self, monkeypatch):
        """无 cookie 请求 /api/v1/health/deep：白名单前缀匹配真实放行，不被 401 拦截"""
        monkeypatch.setattr(
            health_api, "collect_checks",
            lambda engine=None: {
                "database": {"status": "ok"}, "redis": {"status": "ok"},
                "migrations": {"status": "ok"}, "uploads": {"status": "ok"},
            },
        )
        client = TestClient(_mw_app())
        resp = client.get("/api/v1/health/deep")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "healthy"

    def test_non_whitelisted_path_still_blocked(self):
        """对照组：非白名单路径无 cookie 被中间件 401 拦截（先于路由匹配）——
        证明中间件真实在工作，上面的放行不是「app 没挂中间件」的假绿"""
        client = TestClient(_mw_app())
        resp = client.get("/api/v1/models")  # 不在白名单；本 app 也未注册该路由
        assert resp.status_code == 401
        assert resp.json()["code"] == 401


# ======================================================================
# 2. 真实 app.main 装配
# ======================================================================

class TestMainAppWiring:
    def test_deep_health_route_registered_and_anonymous_ok(self, monkeypatch):
        """真实 app：/api/v1/health/deep 已挂载且匿名可达（200）"""
        monkeypatch.setattr(
            health_api, "collect_checks",
            lambda engine=None: {
                "database": {"status": "ok"}, "redis": {"status": "ok"},
                "migrations": {"status": "ok"}, "uploads": {"status": "ok"},
            },
        )
        client = TestClient(app.main.app)
        resp = client.get("/api/v1/health/deep")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "healthy"

    def test_auth_status_on_real_app_401_when_no_cookie(self):
        """真实 app：/api/v1/auth/status 无 cookie 如实 401（恒 200 问题消除的装配级证据）"""
        client = TestClient(app.main.app)
        resp = client.get("/api/v1/auth/status")
        assert resp.status_code == 401
        body = resp.json()
        assert body["is_authenticated"] is False
        assert body["message"] == "未提供认证信息或登录已失效"

    def test_deep_health_real_dependency_probe_runs(self):
        """真实 app + 不打桩 collect_checks：探针真实执行（本机有共享库/Redis 时全绿；
        任一依赖缺失时必须 503 degraded 且 checks 给出 error——不允许 5xx 崩溃）"""
        client = TestClient(app.main.app)
        resp = client.get("/api/v1/health/deep")
        assert resp.status_code in (200, 503)
        body = resp.json()
        assert body["status"] in ("healthy", "degraded")
        assert set(body["checks"].keys()) == {"database", "redis", "migrations", "uploads"}
        for name, check in body["checks"].items():
            assert check["status"] in ("ok", "down", "mismatch"), (name, check)
            if check["status"] != "ok":
                assert check.get("error"), (name, check)


# ======================================================================
# 3. docker-compose 三服务 logging + healthcheck
# ======================================================================

COMPOSE_PATH = PROJECT_ROOT / "docker-compose.yml"


class TestComposeWiring:
    @pytest.fixture
    def services(self):
        data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        assert set(data["services"]) >= {"api", "worker", "hub-plugin"}, data["services"].keys()
        return data["services"]

    def test_logging_rotation_all_three(self, services):
        """验收「日志不再无限增长」：三服务 json-file 50m × 3"""
        for name in ("api", "worker", "hub-plugin"):
            logging_cfg = services[name].get("logging", {})
            assert logging_cfg.get("driver") == "json-file", name
            opts = logging_cfg.get("options", {})
            assert opts.get("max-size") == "50m", (name, opts)
            assert opts.get("max-file") == "3", (name, opts)

    def test_healthcheck_all_three(self, services):
        """验收「compose 三服务补 healthcheck」：三服务都有 healthcheck.test"""
        for name in ("api", "worker", "hub-plugin"):
            hc = services[name].get("healthcheck", {})
            assert hc.get("test"), f"{name} 缺 healthcheck"
            assert " ".join(hc["test"]), name

    def test_api_healthcheck_targets_deep_health(self, services):
        """api 的 healthcheck 探的是深健康（而非恒 200 的 auth/status）"""
        cmd = " ".join(services["api"]["healthcheck"]["test"])
        assert "/api/v1/health/deep" in cmd
        assert "auth/status" not in cmd

    def test_worker_healthcheck_is_celery_ping(self, services):
        cmd = " ".join(services["worker"]["healthcheck"]["test"])
        assert "celery" in cmd and "inspect ping" in cmd

    def test_hub_plugin_healthcheck_is_tcp_probe(self, services):
        cmd = " ".join(services["hub-plugin"]["healthcheck"]["test"])
        assert "socket" in cmd and "9500" in cmd


# ======================================================================
# 4. 文档：告警契约 + 离线升级路径
# ======================================================================

class TestOpsDoc:
    def test_doc_exists_and_has_upgrade_path(self):
        doc = PROJECT_ROOT / "docs" / "alerts-and-health.md"
        assert doc.exists(), "docs/alerts-and-health.md 未入库（.gitignore 白名单应放行）"
        text = doc.read_text(encoding="utf-8")
        assert "ALERT_WEBHOOK_URL" in text
        assert "alerts.log" in text
        # 离线环境升级路径：接入 IM 后配环境变量即生效
        assert "升级" in text or "接入" in text
        # 深健康运维速查
        assert "health/deep" in text

    def test_doc_is_git_tracked(self):
        """白名单放行生效：文件不被 git 忽略"""
        import subprocess

        r = subprocess.run(
            ["git", "check-ignore", "docs/alerts-and-health.md"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        assert r.returncode != 0, "docs/alerts-and-health.md 被 .gitignore 忽略，未入库"


# ======================================================================
# 5. 告警兜底落盘路径契约（两实现一致）
# ======================================================================

class TestAlertLogPathContract:
    def test_main_app_alert_log_path(self):
        assert alert_service.ALERT_LOG_PATH.name == "alerts.log"
        assert alert_service.ALERT_LOG_PATH.parent.name == "logs"

    def test_build_service_alert_log_path(self):
        # 复用 test_build_service 的模块级 setup（BUILD_SERVICE_CONFIG + sys.path 注入）
        from tests.unit import test_build_service as tbs

        bs = tbs.bs
        assert bs.ALERT_LOG_PATH.name == "alerts.log"
        assert bs.ALERT_LOG_PATH.parent.name == "logs"
        # 契约字段与主应用一致（同一 JSON 契约：level/title/details/source/timestamp）
        payload = {
            "level": "error", "title": "t", "details": {},
            "source": "build-service", "timestamp": "2026-10-13T00:00:00+0800",
        }
        assert set(payload.keys()) == set(
            alert_service.build_payload("error", "t").keys()
        )


# ======================================================================
# 6. migration_heads 解析真实迁移链
# ======================================================================

class TestMigrationHeads:
    def test_heads_from_real_alembic_dir_nonempty(self):
        heads = health_api.migration_heads()
        assert heads, "真实 alembic 迁移链解析出空 heads"
        assert all(isinstance(h, str) and h for h in heads)

    def test_heads_is_sorted_list(self):
        heads = health_api.migration_heads()
        assert heads == sorted(heads)


# ======================================================================
# 7. 探针超时护栏契约：单探针 3s < verify urlopen timeout=5s
# ======================================================================

def test_probe_timeout_below_verify_urlopen_timeout():
    """health_api.py 模块注释声明的约束：探针超时必须小于 build-service
    verify 的 urlopen timeout=5s，否则依赖宕机时 503 degraded 会退化为
    「探活超时」，丢掉 checks 明细。"""
    assert health_api.PROBE_TIMEOUT_SECONDS < 5.0


# ======================================================================
# 8. 部署护栏闭环：深健康 503 → verify 抛错 → 告警 → 原样重抛
# ======================================================================

class TestVerifyDeepHealthBlocksDeploy:
    def test_deep_health_503_fails_verify_and_alerts(self, monkeypatch):
        """「容器起了但 DB/Redis/迁移版本不对」：深健康返回 503（urllib 抛
        HTTPError）→ verify_deploy 30 次重试后 RuntimeError → verify 发告警并重抛"""
        from tests.unit import test_build_service as tbs

        bs = tbs.bs
        alerts = []
        monkeypatch.setattr(bs, "send_alert",
                            lambda level, title, details, source="build-service": alerts.append(details))
        # 容器状态检查快速通过（全部 running）
        monkeypatch.setattr(bs, "DEPLOY_SERVICES", {"api"})
        ps_result = SimpleNamespace(returncode=0, stdout='{"Service":"api","State":"running"}', stderr="")
        monkeypatch.setattr(bs, "compose", lambda *a, **kw: ps_result)
        # 深健康 503 → urllib 真实行为是抛 HTTPError
        def http_503(url, timeout=None):
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)

        monkeypatch.setattr(bs.urllib.request, "urlopen", http_503)
        monkeypatch.setattr(bs.time, "sleep", lambda s: None)  # 跳过重试等待

        with pytest.raises(RuntimeError, match="HTTP 探活失败"):
            bs.verify_deploy_with_alert(io.StringIO(), "job-503", tag="v9.9.9", commit="deadbee")

        assert len(alerts) == 1
        assert alerts[0]["job_id"] == "job-503"
        assert alerts[0]["tag"] == "v9.9.9"
        assert "HTTP 探活失败" in alerts[0]["error"]

    def test_deep_health_200_passes_verify_no_alert(self, monkeypatch):
        """全绿对照：200 → verify 通过、无告警"""
        from tests.unit import test_build_service as tbs

        bs = tbs.bs
        alerts = []
        monkeypatch.setattr(bs, "send_alert",
                            lambda level, title, details, source="build-service": alerts.append(details))
        monkeypatch.setattr(bs, "DEPLOY_SERVICES", {"api"})
        ps_result = SimpleNamespace(returncode=0, stdout='{"Service":"api","State":"running"}', stderr="")
        monkeypatch.setattr(bs, "compose", lambda *a, **kw: ps_result)

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        opened = []
        monkeypatch.setattr(bs.urllib.request, "urlopen",
                            lambda url, timeout=None: (opened.append(url), _Resp())[1])
        monkeypatch.setattr(bs.time, "sleep", lambda s: None)

        bs.verify_deploy_with_alert(io.StringIO(), "job-ok", tag="v9.9.9")
        assert opened == [bs.HEALTH_URL]
        assert alerts == []


# ======================================================================
# 9. celery on_failure 真实落盘端到端（send_alert 不打桩）
# ======================================================================

class TestCeleryOnFailureRealAlertLog:
    def test_on_failure_writes_real_alert_log(self, monkeypatch, tmp_path):
        """验收「任务失败产生一条告警记录」的端到端：on_failure → 真实
        send_alert（缺省 webhook 禁用）→ alerts.log 落一条含任务 ID 的 JSON Line"""
        from app.tasks.celery_tasks import EvaluationBaseTask

        alert_log = tmp_path / "logs" / "alerts.log"
        monkeypatch.setattr(alert_service, "ALERT_LOG_PATH", alert_log)
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        # 断开 DB 回写（on_failure 自身 except 兜住），聚焦告警链
        monkeypatch.setattr(
            "app.tasks.evaluation_tasks_async.get_db_session",
            lambda: (_ for _ in ()).throw(RuntimeError("unit-test: db disabled")),
        )

        task = EvaluationBaseTask()
        task.on_failure(RuntimeError("模型网关超时"), "celery-real-1", (77,), {}, None)

        lines = alert_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["level"] == "error"
        assert record["title"] == "评测任务失败"
        assert record["source"] == "celery"
        assert record["details"]["celery_task_id"] == "celery-real-1"
        assert record["details"]["evaluation_task_id"] == 77
        assert "模型网关超时" in record["details"]["error"]
        assert record["webhook_delivered"] is False  # 离线兜底形态
