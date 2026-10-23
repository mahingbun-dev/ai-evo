"""评测模板版本化 + A/B 模板版本对照 API 测试（M5 QE5-2）

链路是真实的：TestClient → 真 AuthMiddleware → instruction/ab-test/task
router → sqlite。覆盖方案 §5.2：
1. 模板打版：POST /instruction/config/{id}/versions 以当前内容创建不可变
   版本（content 全量快照、版本号单调递增）；
2. 编辑自动打版：PUT /instruction/config/{id} 携带 version_changelog 即先
   为改前内容打版再应用编辑；
3. 字段级 diff：逐内容字段 from/to 对照，变更字段数与内容正确；
4. 恢复：POST .../versions/{n}/restore——恢复前活内容自动留存进版本链；
5. 权限：viewer 打版/恢复/创建对照 403（读放行）；
6. A/B 模板版本对照（任务化实验）：源任务 × 两版本 → 两个变体任务
   （模板派生字段取版本 content，其余与源任务一致），对照载荷随执行
   记录推导；同版本/未知版本 400；旧入口（模型对比会话）行为不变。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部模型
import app.models.ab_test_models  # noqa: F401  ab_test_sessions 不在 models/__init__
from app.api.ab_test_api import router as ab_router
from app.api.evaluation_task_api import router as task_router
from app.api.instruction_config_api import router as instruction_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.template_models import EvaluationTemplate, TemplateVersion
from app.models.user_models import User
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.jwt_service import create_token
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit


@pytest.fixture
def stack(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", TestSession)

    with TestSession() as db:
        db.add(User(user_code="eng", username="eng", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT"))
        db.add(User(user_code="viewer", username="viewer", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT",
                    role="viewer"))
        db.add(User(user_code="other", username="other", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="OTHER"))
        db.commit()

    app = FastAPI()
    app.include_router(instruction_router, prefix="/api/v1")
    app.include_router(task_router, prefix="/api/v1")
    app.include_router(ab_router, prefix="/api/v1")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    app.dependency_overrides[get_db] = lambda: TestSession()
    return type("Stack", (), {
        "app": app, "session": TestSession,
        "jwt_eng": create_token("eng", "eng"),
        "jwt_viewer": create_token("viewer", "viewer"),
        "jwt_other": create_token("other", "other"),
    })()


@pytest.fixture
def client(stack):
    return TestClient(stack.app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def template(stack):
    """预置模板：v1 内容打版后改为 v2 内容（场景描述/最高分变化）"""
    with stack.session() as db:
        db.add(EvaluationTemplate(
            id="tmpl_1", name="客服评测", scene="customer_service",
            scene_description="场景A", max_score=10,
            evaluation_metrics=[{"name": "准确性", "weight": "medium", "max_score": 10}],
            score_details="9-10分：优秀", judge_prompt="严格评分"))
        db.commit()
    return "tmpl_1"


def _create_version(client, stack, changelog=""):
    resp = client.post("/api/v1/instruction/config/tmpl_1/versions",
                       headers=auth(stack.jwt_eng), json={"changelog": changelog or None})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestTemplateVersioning:
    def test_manual_create_version_snapshots_content(self, client, stack, template):
        v = _create_version(client, stack, changelog="初版")
        assert v["version_no"] == 1
        assert v["changelog"] == "初版"
        content = v["content"]
        assert content["scene_description"] == "场景A"
        assert content["max_score"] == 10
        assert content["evaluation_metrics"][0]["name"] == "准确性"
        assert content["instruction_template"] is None or isinstance(content["instruction_template"], str)

        with stack.session() as db:
            row = db.query(TemplateVersion).filter(
                TemplateVersion.template_id == "tmpl_1").first()
            assert row.version_no == 1
            assert row.created_by == "eng"

    def test_auto_version_on_update_captures_pre_edit(self, client, stack, template):
        _create_version(client, stack, changelog="初版")
        # 编辑携带 version_changelog：改前内容（场景A/10分）自动打版为 v2
        resp = client.put("/api/v1/instruction/config/tmpl_1",
                          headers=auth(stack.jwt_eng),
                          json={"scene_description": "场景B", "max_score": 100,
                                "version_changelog": "收紧评分前留档"})
        assert resp.status_code == 200, resp.text

        versions = client.get("/api/v1/instruction/config/tmpl_1/versions",
                              headers=auth(stack.jwt_eng)).json()
        assert [v["version_no"] for v in versions] == [2, 1]
        assert versions[0]["changelog"] == "收紧评分前留档"

        # v2 content 是改前状态；活模板已是改后状态
        detail = client.get("/api/v1/instruction/config/tmpl_1/versions/2",
                            headers=auth(stack.jwt_eng)).json()
        assert detail["content"]["scene_description"] == "场景A"
        assert detail["content"]["max_score"] == 10
        live = client.get("/api/v1/instruction/config/tmpl_1",
                          headers=auth(stack.jwt_eng)).json()
        assert live["scene_description"] == "场景B"
        assert live["max_score"] == 100

    def test_update_without_changelog_no_auto_version(self, client, stack, template):
        _create_version(client, stack)
        client.put("/api/v1/instruction/config/tmpl_1",
                   headers=auth(stack.jwt_eng), json={"max_score": 50})
        versions = client.get("/api/v1/instruction/config/tmpl_1/versions",
                              headers=auth(stack.jwt_eng)).json()
        assert len(versions) == 1  # 未携带 version_changelog 不打版

    def test_field_level_diff(self, client, stack, template):
        _create_version(client, stack, changelog="v1")
        # 编辑（不自动打版）改变场景描述/最高分——instruction_template 随重新生成
        client.put("/api/v1/instruction/config/tmpl_1",
                   headers=auth(stack.jwt_eng),
                   json={"scene_description": "场景B", "max_score": 100})
        _create_version(client, stack, changelog="v2=收紧后")

        resp = client.get("/api/v1/instruction/config/tmpl_1/versions/diff",
                          headers=auth(stack.jwt_eng), params={"from": 1, "to": 2})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        changed = {f["field"]: f for f in body["fields"]}
        assert set(changed) == {"scene_description", "max_score", "instruction_template"}
        assert changed["scene_description"]["from"] == "场景A"
        assert changed["scene_description"]["to"] == "场景B"
        assert changed["max_score"]["from"] == 10
        assert changed["max_score"]["to"] == 100
        assert body["summary"]["changed"] == 3

    def test_diff_identical_versions(self, client, stack, template):
        _create_version(client, stack)
        _create_version(client, stack)
        body = client.get("/api/v1/instruction/config/tmpl_1/versions/diff",
                          headers=auth(stack.jwt_eng),
                          params={"from": 1, "to": 2}).json()
        assert body["summary"]["changed"] == 0
        assert body["fields"] == []

    def test_restore_keeps_pre_state_in_chain(self, client, stack, template):
        _create_version(client, stack, changelog="v1")
        client.put("/api/v1/instruction/config/tmpl_1",
                   headers=auth(stack.jwt_eng),
                   json={"scene_description": "场景B", "max_score": 100,
                         "version_changelog": "留档"})
        _create_version(client, stack, changelog="收紧后快照")

        # 恢复到 v1：恢复前活内容（场景B/100）自动留存为新版本
        resp = client.post("/api/v1/instruction/config/tmpl_1/versions/1/restore",
                           headers=auth(stack.jwt_eng))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["restored_version"] == 1
        assert body["pre_restore_version"] == 4

        live = client.get("/api/v1/instruction/config/tmpl_1",
                          headers=auth(stack.jwt_eng)).json()
        assert live["scene_description"] == "场景A"
        assert live["max_score"] == 10

        pre = client.get("/api/v1/instruction/config/tmpl_1/versions/4",
                         headers=auth(stack.jwt_eng)).json()
        assert pre["content"]["scene_description"] == "场景B"
        assert "恢复到 v1 前" in pre["changelog"]

    def test_unknown_version_404(self, client, stack, template):
        _create_version(client, stack)
        assert client.get("/api/v1/instruction/config/tmpl_1/versions/99",
                          headers=auth(stack.jwt_eng)).status_code == 404
        assert client.get("/api/v1/instruction/config/tmpl_1/versions/diff",
                          headers=auth(stack.jwt_eng),
                          params={"from": 1, "to": 99}).status_code == 404
        assert client.post("/api/v1/instruction/config/tmpl_1/versions/99/restore",
                           headers=auth(stack.jwt_eng)).status_code == 404

    def test_viewer_readonly(self, client, stack, template):
        assert client.get("/api/v1/instruction/config/tmpl_1/versions",
                          headers=auth(stack.jwt_viewer)).status_code == 200
        assert client.post("/api/v1/instruction/config/tmpl_1/versions",
                           headers=auth(stack.jwt_viewer),
                           json={"changelog": None}).status_code == 403
        assert client.post("/api/v1/instruction/config/tmpl_1/versions/1/restore",
                           headers=auth(stack.jwt_viewer)).status_code == 403
        # 携带 version_changelog 的编辑（含自动打版）同样拒绝
        assert client.put("/api/v1/instruction/config/tmpl_1",
                          headers=auth(stack.jwt_viewer),
                          json={"max_score": 50, "version_changelog": "x"}).status_code == 403


@pytest.fixture
def versioned_template(client, stack, template):
    """打 v1/v2 两版：v1=场景A/10分，v2=场景B/100分（编辑不带自动打版，
    保证版本号即 1/2 便于对照用例引用）"""
    _create_version(client, stack, changelog="宽松版")
    client.put("/api/v1/instruction/config/tmpl_1",
               headers=auth(stack.jwt_eng),
               json={"scene_description": "场景B", "max_score": 100})
    _create_version(client, stack, changelog="收紧版")
    return "tmpl_1"


@pytest.fixture
def source_task(client, stack):
    """源评测任务（success_rate，走任务 API 创建保证全量校验口径）"""
    resp = client.post("/api/v1/evaluation/tasks", headers=auth(stack.jwt_eng), json={
        "name": "源任务",
        "evaluation_type": "success_rate",
        "success_rate_config": {"expected_field": "answer", "mode": "contains"},
        "judge_count": 0,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestABTemplateComparison:
    def _create_session(self, client, stack, source_task, template_id="tmpl_1",
                        version_a=1, version_b=2, name=None):
        return client.post("/api/v1/ab-test/template-sessions",
                           headers=auth(stack.jwt_eng), json={
                               "source_task_id": source_task["id"],
                               "template_id": template_id,
                               "version_a": version_a,
                               "version_b": version_b,
                               "name": name,
                           })

    def test_create_session_spawns_variant_tasks(self, client, stack,
                                                 versioned_template, source_task):
        resp = self._create_session(client, stack, source_task, name="宽松vs收紧")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "template"
        assert body["template_version_a"] == 1
        assert body["template_version_b"] == 2
        assert body["task_a_id"] and body["task_b_id"]
        assert body["task_a_id"] != body["task_b_id"]
        assert body["comparison"]["state"] == "pending"

        # 变体任务：模板派生字段取版本 content，其余与源任务一致
        task_a = client.get(f"/api/v1/evaluation/tasks/{body['task_a_id']}",
                            headers=auth(stack.jwt_eng)).json()
        task_b = client.get(f"/api/v1/evaluation/tasks/{body['task_b_id']}",
                            headers=auth(stack.jwt_eng)).json()
        assert task_a["name"] == "源任务 [模板v1]"
        assert task_a["template_id"] == "tmpl_1"
        assert task_a["scene_desc"] == "场景A"
        assert task_a["max_score"] == 10
        assert task_b["scene_desc"] == "场景B"
        assert task_b["max_score"] == 100
        # 源任务不受影响
        src = client.get(f"/api/v1/evaluation/tasks/{source_task['id']}",
                         headers=auth(stack.jwt_eng)).json()
        assert src["name"] == "源任务"
        assert src["template_id"] in (None, "")

    def test_list_and_detail_with_comparison(self, client, stack,
                                             versioned_template, source_task):
        session_id = self._create_session(client, stack, source_task).json()["id"]

        listing = client.get("/api/v1/ab-test/sessions",
                             headers=auth(stack.jwt_eng),
                             params={"mode": "template"}).json()
        assert listing["total"] == 1
        assert listing["items"][0]["mode"] == "template"
        assert listing["items"][0]["task_a_id"]

        # 模拟两侧执行完成 → comparison 推导 completed + 均分
        from app.models.task_models import TaskExecutionRecord
        with stack.session() as db:
            listing2 = client.get("/api/v1/ab-test/sessions",
                                  headers=auth(stack.jwt_eng),
                                  params={"mode": "template"}).json()
            a_id, b_id = listing2["items"][0]["task_a_id"], listing2["items"][0]["task_b_id"]
            from datetime import datetime, timezone
            for i, tid in enumerate((a_id, b_id)):
                db.add(TaskExecutionRecord(
                    id=f"rec_{tid}", task_id=tid, execution_number=1,
                    status="completed", result={"average_score": 8.0 + i},
                    completed_at=datetime.now(timezone.utc)))
            db.commit()

        detail = client.get(f"/api/v1/ab-test/sessions/{session_id}",
                            headers=auth(stack.jwt_eng)).json()
        assert detail["comparison"]["state"] == "completed"
        assert detail["comparison"]["side_a"]["latest_execution"]["average_score"] == 8.0
        assert detail["comparison"]["side_b"]["latest_execution"]["average_score"] == 9.0

    def test_model_mode_list_untouched(self, client, stack, versioned_template, source_task):
        """旧入口不受影响：默认 mode=model 不返回 template 会话"""
        self._create_session(client, stack, source_task)
        listing = client.get("/api/v1/ab-test/sessions",
                             headers=auth(stack.jwt_eng)).json()
        assert listing["total"] == 0

        # 手工模型会话保存仍走原入口（model 字段照常返回）
        save = client.post("/api/v1/ab-test/sessions", headers=auth(stack.jwt_eng), json={
            "model_a_id": "m1", "model_b_id": "m2",
            "model_a_name": "A模型", "model_b_name": "B模型",
            "status": "completed",
        })
        assert save.status_code == 200, save.text
        assert save.json()["mode"] == "model"
        listing = client.get("/api/v1/ab-test/sessions",
                             headers=auth(stack.jwt_eng)).json()
        assert listing["total"] == 1
        assert listing["items"][0]["mode"] == "model"

    def test_same_version_rejected(self, client, stack, versioned_template, source_task):
        resp = self._create_session(client, stack, source_task, version_a=1, version_b=1)
        assert resp.status_code == 400
        assert "不能相同" in resp.json()["detail"]

    def test_unknown_version_rejected(self, client, stack, versioned_template, source_task):
        resp = self._create_session(client, stack, source_task, version_a=1, version_b=99)
        assert resp.status_code == 400
        assert "不存在版本" in resp.json()["detail"]

    def test_template_without_versions_rejected(self, client, stack, source_task):
        with stack.session() as db:
            db.add(EvaluationTemplate(id="tmpl_nov", name="无版本模板", scene="cs"))
            db.commit()
        resp = self._create_session(client, stack, source_task, template_id="tmpl_nov")
        assert resp.status_code == 400

    def test_viewer_cannot_create(self, client, stack, versioned_template, source_task):
        resp = client.post("/api/v1/ab-test/template-sessions",
                           headers=auth(stack.jwt_viewer), json={
                               "source_task_id": source_task["id"],
                               "template_id": "tmpl_1",
                               "version_a": 1, "version_b": 2})
        assert resp.status_code == 403

    def test_cross_team_source_task_denied(self, client, stack,
                                           versioned_template, source_task):
        # other 团队用户对 DEFAULT 团源任务发起对照 → ensure_task_access 403
        resp = client.post("/api/v1/ab-test/template-sessions",
                           headers=auth(stack.jwt_other), json={
                               "source_task_id": source_task["id"],
                               "template_id": "tmpl_1",
                               "version_a": 1, "version_b": 2})
        assert resp.status_code == 403

    def test_delete_template_session(self, client, stack, versioned_template, source_task):
        session_id = self._create_session(client, stack, source_task).json()["id"]
        resp = client.delete(f"/api/v1/ab-test/sessions/{session_id}",
                             headers=auth(stack.jwt_eng))
        assert resp.status_code == 200
        assert client.get(f"/api/v1/ab-test/sessions/{session_id}",
                          headers=auth(stack.jwt_eng)).status_code == 404
