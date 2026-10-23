"""仿真场景/运行服务与 API 单测（M7 QE7-4）

链路是真实的：TestClient → 真 AuthMiddleware → simulation_router → sqlite
（场景 CRUD 门禁、run 创建/详情/停止）；run 执行链 mock 在
run_simulation_trials 与配额/celery 边界（不依赖 Redis/broker）。覆盖：

- 类型白名单：simulation 入白名单但直建评测任务被拒（指向仿真入口）；
- 场景 CRUD：无终态校验器 400（发布门禁）、更新 version+1、运行中删除 409；
- run 生命周期：创建冻结场景快照、CAS 启动、配额满拒绝、execute_run
  逐 trial 进度与终态（completed/stopped/failed）、配额释放调用；
- 指标：result 带 pass_k/task_success_rate/average_turns/tool_waste_rate/
  fault_recovery_rate（工具浪费率口径单测）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.simulation_api import router as simulation_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.simulation_models import SimulationRun, SimulationScenario
from app.models.user_models import User
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.evaluation_task_service import EvaluationTaskService
from app.services.jwt_service import create_token
from app.services.simulation.service import SimulationService
from app.services.simulation.turn_controller import tool_waste_rate
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit


def _config(**overrides):
    config = {
        "agent": {"exec_type": "claude_agent", "instruction": "处理报销"},
        "personas": [{"name": "小王", "goal": "拿到受理编号"}],
        "verifiers": [{
            "type": "file_assert", "name": "回执文件",
            "config": {"assertions": [{"name": "回执", "path": "outputs/result.json"}]},
        }],
        "trials": 2,
    }
    config.update(overrides)
    return config


@pytest.fixture
def stack(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", TestSession)

    with TestSession() as db:
        # DEFAULT 团队三角色 + 异团队用户 + 超管（团队隔离/RBAC 用例的当事人）
        db.add(User(user_code="ci-user", username="ci", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT"))
        db.add(User(user_code="viewer-user", username="viewer", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT",
                    role="viewer"))
        db.add(User(user_code="b-user", username="bob", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="TEAM-B"))
        db.add(User(user_code="root", username="root", is_active=True,
                    super_admin_flag=1, application_admin=0, org_code="DEFAULT"))
        db.commit()

    app = FastAPI()
    app.include_router(simulation_router, prefix="/api/v1")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    app.dependency_overrides[get_db] = lambda: TestSession()

    class Stack:
        pass

    stack = Stack()
    stack.app = app
    stack.jwt = create_token("ci-user", "ci")
    stack.viewer_jwt = create_token("viewer-user", "viewer")
    stack.b_jwt = create_token("b-user", "bob")
    stack.root_jwt = create_token("root", "root")
    stack.session = TestSession
    return stack


@pytest.fixture
def client(stack):
    return TestClient(stack.app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 类型白名单
class TestTypeWhitelist:
    def test_simulation_whitelisted_but_direct_task_rejected(self, stack):
        with stack.session() as db:
            service = EvaluationTaskService(db)
            with pytest.raises(ValueError, match="仿真场景"):
                service.create_task(
                    name="直建仿真任务", evaluation_type="simulation",
                    model_provider="test")
            # 非白名单类型仍是「不支持的评测类型」
            with pytest.raises(ValueError, match="不支持的评测类型"):
                service.create_task(
                    name="坏类型", evaluation_type="nope",
                    model_provider="test")


# ---------------------------------------------------------------- 场景 CRUD
class TestScenarioCrud:
    def test_create_publish_gate(self, client, stack):
        # 无终态校验器 → 400（发布门禁）
        resp = client.post("/api/v1/simulation/scenarios", headers=auth(stack.jwt), json={
            "name": "无校验场景", "config": _config(verifiers=[]),
        })
        assert resp.status_code == 400
        assert "终态校验器" in resp.json()["detail"]

        resp = client.post("/api/v1/simulation/scenarios", headers=auth(stack.jwt), json={
            "name": "报销仿真", "config": _config(),
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"].startswith("sim_")
        assert body["config"]["trials"] == 2
        assert body["config"]["max_turns"] == 15  # 归一化缺省

    def test_update_bumps_version_and_gates(self, client, stack):
        resp = client.post("/api/v1/simulation/scenarios", headers=auth(stack.jwt), json={
            "name": "v1", "config": _config()})
        scenario_id = resp.json()["id"]
        resp = client.put(f"/api/v1/simulation/scenarios/{scenario_id}",
                          headers=auth(stack.jwt),
                          json={"name": "v2", "config": _config(trials=3)})
        assert resp.status_code == 200
        assert resp.json()["version"] == 2
        assert resp.json()["config"]["trials"] == 3
        # 更新成无校验器 → 400
        resp = client.put(f"/api/v1/simulation/scenarios/{scenario_id}",
                          headers=auth(stack.jwt), json={"config": _config(verifiers=[])})
        assert resp.status_code == 400

    def test_delete_rejected_while_running(self, client, stack):
        with stack.session() as db:
            service = SimulationService(db)
            scenario = service.create_scenario("运行中", _config(), team_code="DEFAULT")
            scenario_id = scenario.id
            run = service.create_run(scenario.id)
            service.start_run(run.id)
        resp = client.delete(
            f"/api/v1/simulation/scenarios/{scenario_id}", headers=auth(stack.jwt))
        assert resp.status_code == 409
        assert "运行中" in resp.json()["detail"]
        # 完成后可删
        with stack.session() as db:
            db.query(SimulationRun).update({"status": "completed"})
            db.commit()
        resp = client.delete(
            f"/api/v1/simulation/scenarios/{scenario_id}", headers=auth(stack.jwt))
        assert resp.status_code == 200

    def test_get_list_scenarios(self, client, stack):
        client.post("/api/v1/simulation/scenarios", headers=auth(stack.jwt),
                    json={"name": "s1", "config": _config()})
        resp = client.get("/api/v1/simulation/scenarios", headers=auth(stack.jwt))
        assert resp.status_code == 200 and len(resp.json()) == 1
        scenario_id = resp.json()[0]["id"]
        resp = client.get(
            f"/api/v1/simulation/scenarios/{scenario_id}", headers=auth(stack.jwt))
        assert resp.status_code == 200 and resp.json()["name"] == "s1"
        resp = client.get(
            "/api/v1/simulation/scenarios/sim_none", headers=auth(stack.jwt))
        assert resp.status_code == 404


# ---------------------------------------------------------------- 运行生命周期
class TestRunLifecycle:
    def _scenario(self, stack) -> str:
        """建场景并返回 id（对象本身不跨 session 存活）"""
        with stack.session() as db:
            scenario = SimulationService(db).create_scenario(
                "报销仿真", _config(), created_by="ci-user", team_code="DEFAULT")
            return scenario.id

    def test_create_run_freezes_snapshot(self, stack):
        scenario_id = self._scenario(stack)
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id, created_by="ci-user")
            run_id = run.id
            assert run.status == "pending"
            assert run.trials_total == 2
            assert run.scenario_snapshot == service.get_scenario(scenario_id).config
            # 场景修订不影响 run 快照
            service.update_scenario(scenario_id, config=_config(trials=5))
            assert service.get_run(run_id).scenario_snapshot["trials"] == 2

    def test_start_run_cas(self, stack):
        scenario_id = self._scenario(stack)
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id)
            assert service.start_run(run.id) is True
            assert service.start_run(run.id) is False  # 已 running，CAS 拒绝

    def test_submit_run_quota_and_celery(self, stack, monkeypatch):
        scenario_id = self._scenario(stack)
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id)
            service.start_run(run.id)

            # 配额满 → False（调用方标 failed）
            quota = MagicMock()
            quota.acquire.return_value = False
            with patch("app.services.quota.TeamConcurrencyQuota", return_value=quota):
                assert service.submit_run(run.id) is False

            # 配额过 + 入队成功 → celery_task_id 落库
            quota_ok = MagicMock()
            quota_ok.acquire.return_value = True
            fake_async = MagicMock(return_value=SimpleNamespace(id="celery-1"))
            import app.tasks.celery_tasks as ct
            with patch("app.services.quota.TeamConcurrencyQuota", return_value=quota_ok), \
                 patch.object(ct.run_simulation_run, "apply_async", fake_async):
                assert service.submit_run(run.id) is True
            assert service.get_run(run.id).celery_task_id == "celery-1"
            fake_async.assert_called_once()

    def test_execute_run_progress_and_completion(self, stack, monkeypatch):
        scenario_id = self._scenario(stack)
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id)
            service.start_run(run.id)

            progress_calls = []

            def fake_trials(scenario_cfg, task, *, run_id, stop_signal=None,
                            on_trial_done=None, **kw):
                assert scenario_cfg["trials"] == 2
                trials = [{"trial": i + 1, "passed": True,
                           "termination": {"reason": "goal_achieved", "detail": ""},
                           "turns": {"turns": [], "user_turn_count": 2},
                           "agent": {"calls": 2, "tool_count": 1},
                           "tool_waste_rate": 0.0, "faults_fired": [],
                           "verification": {"status": "pass"}, "duration_ms": 5}
                          for i in range(2)]
                if on_trial_done:
                    for i, t in enumerate(trials):
                        on_trial_done(i + 1, t)
                return {
                    "trials": trials,
                    "pass_k": {"trials": 2, "pass_count": 2, "pass_at_1": 1.0, "pass_pow_k": 1},
                    "task_success_rate": 1.0, "average_turns": 2.0,
                    "tool_waste_rate": 0.0, "fault_recovery_rate": None,
                }

            publish = MagicMock()
            release = MagicMock()
            monkeypatch.setattr(
                "app.services.simulation.service.run_simulation_trials", fake_trials)
            monkeypatch.setattr(SimulationService, "_publish_progress", publish)
            monkeypatch.setattr(SimulationService, "_release_quota", release)

            outcome = service.execute_run(run.id, stop_signal=None)
            assert outcome["pass_k"]["pass_pow_k"] == 1
            run_row = service.get_run(run.id)
            assert run_row.status == "completed"
            assert run_row.trials_done == 2
            assert run_row.result["task_success_rate"] == 1.0
            # 每 trial 一次 + 收尾一次进度发布；配额释放成对
            assert publish.call_count == 3
            release.assert_called_once()

    def test_execute_run_stopped_status(self, stack, monkeypatch):
        scenario_id = self._scenario(stack)
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id)
            service.start_run(run.id)
            monkeypatch.setattr(
                "app.services.simulation.service.run_simulation_trials",
                lambda *a, **kw: {"trials": [], "pass_k": {"trials": 0},
                                  "task_success_rate": 0.0})
            monkeypatch.setattr(SimulationService, "_publish_progress", MagicMock())
            monkeypatch.setattr(SimulationService, "_release_quota", MagicMock())
            stopped = SimpleNamespace(is_set=lambda: True)
            service.execute_run(run.id, stop_signal=stopped)
            assert service.get_run(run.id).status == "stopped"

    def test_execute_run_failure_marks_failed(self, stack, monkeypatch):
        scenario_id = self._scenario(stack)
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id)
            service.start_run(run.id)
            release = MagicMock()
            monkeypatch.setattr(SimulationService, "_release_quota", release)
            monkeypatch.setattr(
                "app.services.simulation.service.run_simulation_trials",
                MagicMock(side_effect=RuntimeError("沙箱镜像不可用")))
            with pytest.raises(RuntimeError):
                service.execute_run(run.id)
            failed = service.get_run(run.id)
            assert failed.status == "failed"
            assert "沙箱镜像不可用" in failed.error
            release.assert_called_once()


# ---------------------------------------------------------------- API 运行面
class TestRunApi:
    def test_run_endpoint_happy_path(self, client, stack, monkeypatch):
        resp = client.post("/api/v1/simulation/scenarios", headers=auth(stack.jwt),
                           json={"name": "报销仿真", "config": _config()})
        scenario_id = resp.json()["id"]

        quota_ok = MagicMock()
        quota_ok.acquire.return_value = True
        fake_async = MagicMock(return_value=SimpleNamespace(id="celery-9"))
        import app.tasks.celery_tasks as ct
        with patch("app.services.quota.TeamConcurrencyQuota", return_value=quota_ok), \
             patch.object(ct.run_simulation_run, "apply_async", fake_async):
            resp = client.post(
                f"/api/v1/simulation/scenarios/{scenario_id}/run",
                headers=auth(stack.jwt))
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]
        assert resp.json()["status"] == "running"
        assert resp.json()["trials_total"] == 2

        resp = client.get(f"/api/v1/simulation/runs/{run_id}", headers=auth(stack.jwt))
        assert resp.status_code == 200
        assert resp.json()["id"] == run_id
        assert resp.json()["result"] is None  # 未跑完

        resp = client.get(
            f"/api/v1/simulation/scenarios/{scenario_id}/runs", headers=auth(stack.jwt))
        assert resp.status_code == 200 and len(resp.json()) == 1

    def test_run_endpoint_quota_rejected(self, client, stack, monkeypatch):
        resp = client.post("/api/v1/simulation/scenarios", headers=auth(stack.jwt),
                           json={"name": "配额满", "config": _config()})
        scenario_id = resp.json()["id"]
        quota_full = MagicMock()
        quota_full.acquire.return_value = False
        with patch("app.services.quota.TeamConcurrencyQuota", return_value=quota_full):
            resp = client.post(
                f"/api/v1/simulation/scenarios/{scenario_id}/run",
                headers=auth(stack.jwt))
        assert resp.status_code == 429
        assert "配额" in resp.json()["detail"]
        with stack.session() as db:
            run = db.query(SimulationRun).first()
            assert run.status == "failed"  # 拒绝后 run 标 failed（诚实失败）

    def test_stop_endpoint(self, client, stack, monkeypatch):
        resp = client.post("/api/v1/simulation/scenarios", headers=auth(stack.jwt),
                           json={"name": "停", "config": _config()})
        scenario_id = resp.json()["id"]
        # start/submit 打桩返回成功（DB 状态由下方手动置 running，
        # 与真实 start_run 的持久化效果对齐）
        with patch("app.services.simulation.service.SimulationService.start_run",
                   lambda self, rid: True), \
             patch("app.services.simulation.service.SimulationService.submit_run",
                   lambda self, rid: True):
            resp = client.post(
                f"/api/v1/simulation/scenarios/{scenario_id}/run",
                headers=auth(stack.jwt))
        run_id = resp.json()["run_id"]
        with stack.session() as db:
            db.query(SimulationRun).filter(SimulationRun.id == run_id).update(
                {"status": "running"})
            db.commit()

        stop_signal = MagicMock()
        with patch("app.services.redis_stop_signal.RedisStopSignalChecker", stop_signal), \
             patch("app.celery_app.celery_app.control.revoke", MagicMock()):
            resp = client.post(
                f"/api/v1/simulation/runs/{run_id}/stop", headers=auth(stack.jwt))
        assert resp.status_code == 200
        stop_signal.return_value.set.assert_called_once()
        with stack.session() as db:
            run = db.query(SimulationRun).filter(SimulationRun.id == run_id).first()
            assert run.status == "stopped"

    def test_run_unknown_scenario_404(self, client, stack):
        resp = client.post("/api/v1/simulation/scenarios/sim_none/run",
                           headers=auth(stack.jwt))
        assert resp.status_code == 404


# ---------------------------------------------------------------- 团队隔离与 RBAC
class TestTeamIsolationAndRbac:
    """M7 QE7-4 复审修复：团队隔离（ensure_*_access）+ RBAC（ensure_permission）"""

    def _create_default_scenario(self, client, stack) -> str:
        resp = client.post("/api/v1/simulation/scenarios", headers=auth(stack.jwt),
                           json={"name": "DEFAULT 团队场景", "config": _config()})
        assert resp.status_code == 200
        return resp.json()["id"]

    def test_cross_team_object_access_denied(self, client, stack):
        """异团队用户（TEAM-B）对 DEFAULT 场景：读/写/删/发起 run/历史全部 403"""
        scenario_id = self._create_default_scenario(client, stack)
        b = auth(stack.b_jwt)
        assert client.get(
            f"/api/v1/simulation/scenarios/{scenario_id}", headers=b).status_code == 403
        assert client.put(
            f"/api/v1/simulation/scenarios/{scenario_id}", headers=b,
            json={"name": "偷改"}).status_code == 403
        assert client.delete(
            f"/api/v1/simulation/scenarios/{scenario_id}", headers=b).status_code == 403
        assert client.post(
            f"/api/v1/simulation/scenarios/{scenario_id}/run", headers=b).status_code == 403
        assert client.get(
            f"/api/v1/simulation/scenarios/{scenario_id}/runs", headers=b).status_code == 403
        # run 侧同理：DEFAULT 团队的 run 对 TEAM-B 不可见不可停
        with patch("app.services.simulation.service.SimulationService.start_run",
                   lambda self, rid: True), \
             patch("app.services.simulation.service.SimulationService.submit_run",
                   lambda self, rid: True):
            run_id = client.post(
                f"/api/v1/simulation/scenarios/{scenario_id}/run",
                headers=auth(stack.jwt)).json()["run_id"]
        assert client.get(f"/api/v1/simulation/runs/{run_id}", headers=b).status_code == 403
        assert client.post(
            f"/api/v1/simulation/runs/{run_id}/stop", headers=b).status_code == 403

    def test_list_filters_by_team_and_super_admin_sees_all(self, client, stack):
        scenario_id = self._create_default_scenario(client, stack)
        # 异团队用户建自己的场景
        resp = client.post("/api/v1/simulation/scenarios", headers=auth(stack.b_jwt),
                           json={"name": "TEAM-B 场景", "config": _config()})
        assert resp.status_code == 200

        # DEFAULT 用户只见本团队
        ids = [s["id"] for s in client.get(
            "/api/v1/simulation/scenarios", headers=auth(stack.jwt)).json()]
        assert scenario_id in ids
        assert all(sid != resp.json()["id"] for sid in ids)

        # TEAM-B 用户只见 TEAM-B
        b_ids = [s["id"] for s in client.get(
            "/api/v1/simulation/scenarios", headers=auth(stack.b_jwt)).json()]
        assert b_ids == [resp.json()["id"]]

        # 超管看全部
        root_ids = [s["id"] for s in client.get(
            "/api/v1/simulation/scenarios", headers=auth(stack.root_jwt)).json()]
        assert set(root_ids) >= {scenario_id, resp.json()["id"]}

    def test_super_admin_cross_team_read_allowed(self, client, stack):
        scenario_id = self._create_default_scenario(client, stack)
        assert client.get(
            f"/api/v1/simulation/scenarios/{scenario_id}",
            headers=auth(stack.root_jwt)).status_code == 200

    def test_not_found_leaks_nothing(self, client, stack):
        for token in (stack.jwt, stack.b_jwt, stack.root_jwt):
            assert client.get(
                "/api/v1/simulation/scenarios/sim_none",
                headers=auth(token)).status_code == 404
            assert client.get(
                "/api/v1/simulation/runs/simrun_none",
                headers=auth(token)).status_code == 404

    def test_viewer_read_only(self, client, stack):
        """viewer：本团队可读（列表/详情），写/运行/停止 403"""
        scenario_id = self._create_default_scenario(client, stack)
        v = auth(stack.viewer_jwt)
        assert client.get("/api/v1/simulation/scenarios", headers=v).status_code == 200
        assert client.get(
            f"/api/v1/simulation/scenarios/{scenario_id}", headers=v).status_code == 200
        assert client.get(
            f"/api/v1/simulation/scenarios/{scenario_id}/runs", headers=v).status_code == 200
        # 写面全拒（RBAC 矩阵 tasks 资源 viewer 只读）
        assert client.post("/api/v1/simulation/scenarios", headers=v,
                           json={"name": "x", "config": _config()}).status_code == 403
        assert client.put(f"/api/v1/simulation/scenarios/{scenario_id}",
                          headers=v, json={"name": "x"}).status_code == 403
        assert client.delete(
            f"/api/v1/simulation/scenarios/{scenario_id}", headers=v).status_code == 403
        assert client.post(
            f"/api/v1/simulation/scenarios/{scenario_id}/run", headers=v).status_code == 403
        with stack.session() as db:
            db.query(SimulationRun).delete()
            db.commit()
        assert client.post(
            "/api/v1/simulation/runs/simrun_x/stop", headers=v).status_code in (403, 404)


# ---------------------------------------------------------------- 配额释放口径
class TestQuotaSingleRelease:
    """M7 QE7-4 复审修复：配额释放单一释放点（对齐评测链 fail_task 不释放）

    多释放会使配额账本低于 DB 真值且对账「只裁不抬」永不相救——团队可
    系统性超 TEAM_MAX_CONCURRENT。三条不得释放的路径 + 唯一释放点。
    """

    def _scenario(self, stack) -> str:
        with stack.session() as db:
            return SimulationService(db).create_scenario(
                "配额场景", _config(), team_code="DEFAULT").id

    def test_quota_full_path_never_releases(self, client, stack):
        """配额满：acquire 内部已撤销占位，fail_run 不得再 DECR"""
        scenario_id = self._scenario(stack)
        release = MagicMock()
        with patch.object(SimulationService, "_release_quota", release), \
             patch("app.services.quota.TeamConcurrencyQuota") as quota_cls:
            quota_cls.return_value.acquire.return_value = False
            resp = client.post(
                f"/api/v1/simulation/scenarios/{scenario_id}/run",
                headers=auth(stack.jwt))
        assert resp.status_code == 429
        release.assert_not_called()  # 从未拿到额度 → 无释放义务

    def test_broker_failure_releases_once_inside_submit(self, client, stack):
        """broker 异常：submit_run except 内释放恰一次，API fail_run 不再叠加"""
        scenario_id = self._scenario(stack)
        quota_release = MagicMock()
        service_release = MagicMock()
        import app.tasks.celery_tasks as ct
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id)
            service.start_run(run.id)
            with patch("app.services.quota.TeamConcurrencyQuota") as quota_cls, \
                 patch.object(ct.run_simulation_run, "apply_async",
                              side_effect=RuntimeError("broker 不可达")), \
                 patch.object(SimulationService, "_release_quota", service_release):
                quota_cls.return_value.release = quota_release
                with pytest.raises(RuntimeError):
                    service.submit_run(run.id)
                # 提交失败后 API 侧的 fail_run（不释放，见 fail_run docstring）
                service.fail_run(run.id, "提交失败")
        quota_release.assert_called_once()  # submit_run except 内恰一次
        service_release.assert_not_called()  # fail_run 不叠加

    def test_stop_does_not_release_worker_owns_it(self, stack):
        """停止路径：request_stop 只发信号（在途 worker 收尾释放）——不在 API 侧释放"""
        scenario_id = self._scenario(stack)
        release = MagicMock()
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id)
            service.start_run(run.id)
            with patch("app.services.redis_stop_signal.RedisStopSignalChecker", MagicMock()), \
                 patch("app.celery_app.celery_app.control.revoke", MagicMock()), \
                 patch.object(SimulationService, "_release_quota", release):
                assert service.request_stop(run.id) is True
        release.assert_not_called()

    def test_execute_run_is_single_release_point(self, stack, monkeypatch):
        """execute_run 收尾（成功/停止/失败）各恰好释放一次——唯一释放点"""
        scenario_id = self._scenario(stack)
        monkeypatch.setattr(
            "app.services.simulation.service.run_simulation_trials",
            lambda *a, **kw: {"trials": [], "pass_k": {"trials": 0}})
        for stopped_flag in (False, True):
            release = MagicMock()
            with stack.session() as db:
                service = SimulationService(db)
                run = service.create_run(scenario_id)
                service.start_run(run.id)
                with patch.object(SimulationService, "_release_quota", release), \
                     patch.object(SimulationService, "_publish_progress", MagicMock()):
                    signal = SimpleNamespace(is_set=lambda s=stopped_flag: s)
                    service.execute_run(run.id, stop_signal=signal)
            release.assert_called_once()
        # 失败路径同样恰一次
        release = MagicMock()
        with stack.session() as db:
            service = SimulationService(db)
            run = service.create_run(scenario_id)
            service.start_run(run.id)
            monkeypatch.setattr(
                "app.services.simulation.service.run_simulation_trials",
                MagicMock(side_effect=RuntimeError("boom")))
            with patch.object(SimulationService, "_release_quota", release):
                with pytest.raises(RuntimeError):
                    service.execute_run(run.id)
            release.assert_called_once()
            assert service.get_run(run.id).status == "failed"
class TestToolWasteRate:
    def test_pure_function_semantics(self):
        turns = [
            {"role": "user", "round": 1, "progress": None, "action": "speak",
             "content": "q1"},
            {"role": "agent", "round": 1, "tools": ["a", "b"], "content": "r1"},
            # 第 2 条用户消息判定第 1 回合 agent 无进展 → 该回合 2 次工具浪费
            {"role": "user", "round": 2, "progress": False, "action": "speak",
             "content": "q2"},
            {"role": "agent", "round": 2, "tools": ["c"], "content": "r2"},
            # 第 3 条消息判定第 2 回合有进展 → 该回合 1 次工具有效
            {"role": "user", "round": 3, "progress": True, "action": "speak",
             "content": "q3"},
            {"role": "agent", "round": 3, "tools": ["d"], "content": "r3"},
        ]
        # 总工具 4（2+1+1）：goal_achieved → 末回合有效，浪费 2/4
        assert tool_waste_rate(turns, "goal_achieved") == 0.5
        # give_up → 末回合也浪费，3/4
        assert tool_waste_rate(turns, "give_up") == 0.75

    def test_no_tools_returns_none(self):
        turns = [
            {"role": "user", "round": 1, "progress": None, "content": "q"},
            {"role": "agent", "round": 1, "tools": [], "content": "纯文本回复"},
        ]
        assert tool_waste_rate(turns, "goal_achieved") is None
