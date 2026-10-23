"""数据集版本化 API 测试（M5 QE5-1）

链路是真实的：TestClient → 真 AuthMiddleware → dataset/task router → sqlite。
覆盖方案 §5.1 验收主链路「打版 → diff → 锁定版本」：
1. 打版：POST /datasets/{id}/versions 以当前数据创建不可变快照版本，
   版本号单调递增、快照 task_id 为空（无任务归属）；
2. 列表/详情/diff：行级 diff 按打版时数据行 id 对齐，输出
   新增/删除/变更/未变与字段级 changed_fields；
3. 恢复：POST /versions/{n}/restore 软删当前活行、按版本快照重写，
   question_count 同步刷新；
4. 权限：viewer 只读（打版/恢复 403）、跨团队不可见（403）；
5. 任务绑定：dataset_version_no 锁定/跟随最新/未知版本 400/
   换数据集未切版本 400/执行侧按版本快照加载。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dataset_api import router as dataset_router
from app.api.evaluation_task_api import router as task_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.dataset_models import (
    DatasetSnapshot,
    EvaluationDataV2,
    EvaluationDataset,
    DatasetVersion,
)
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
        # 同团队：engineer（默认角色）与 viewer；异团队：other 团用户
        db.add(User(user_code="eng", username="eng", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT"))
        db.add(User(user_code="viewer", username="viewer", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT",
                    role="viewer"))
        db.add(User(user_code="other", username="other", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="OTHER"))
        db.commit()

    app = FastAPI()
    app.include_router(dataset_router, prefix="/api/v1")
    app.include_router(task_router, prefix="/api/v1")
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
def dataset(stack):
    """预置数据集：3 行（q0/q1/q2），打版 v1 后改 1 行删 1 行加 1 行"""
    with stack.session() as db:
        db.add(EvaluationDataset(id="ds1", name="demo", team_code="DEFAULT"))
        for i in range(3):
            db.add(EvaluationDataV2(
                id=f"data2_{i}", dataset_id="ds1", index=i,
                data={"user_input": f"q{i}", "answer": f"a{i}"}))
        db.commit()
    return "ds1"


def _mutate(stack):
    """打版 v1 后的数据变更：q1 改、q2 删、追加 q3"""
    with stack.session() as db:
        row = db.query(EvaluationDataV2).filter(EvaluationDataV2.id == "data2_1").first()
        row.data = {"user_input": "q1改", "answer": "a1新"}
        db.query(EvaluationDataV2).filter(EvaluationDataV2.id == "data2_2").update(
            {"is_active": False})
        db.add(EvaluationDataV2(
            id="data2_new", dataset_id="ds1", index=3,
            data={"user_input": "q3", "answer": "a3"}))
        db.commit()


def _create_version(client, stack, changelog=""):
    resp = client.post("/api/v1/evaluation/datasets/ds1/versions",
                       headers=auth(stack.jwt_eng), json={"changelog": changelog or None})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestCreateAndListVersions:
    def test_create_version_snapshots_current_data(self, client, stack, dataset):
        v = _create_version(client, stack, changelog="初版")
        assert v["version_no"] == 1
        assert v["row_count"] == 3
        assert v["changelog"] == "初版"
        assert v["snapshot_id"].startswith("dss_")

        # 版本快照复用 dataset_snapshot 机制，task_id 为空（无任务归属）
        with stack.session() as db:
            snap = db.query(DatasetSnapshot).filter(
                DatasetSnapshot.id == v["snapshot_id"]).first()
            assert snap is not None
            assert snap.task_id is None
            assert [it["data"]["user_input"] for it in snap.data["items"]] == ["q0", "q1", "q2"]
            version_row = db.query(DatasetVersion).filter(
                DatasetVersion.dataset_id == "ds1").first()
            assert version_row.version_no == 1

    def test_version_no_monotonic_increase(self, client, stack, dataset):
        _create_version(client, stack)
        _mutate(stack)
        v2 = _create_version(client, stack, changelog="改1删1加1")
        assert v2["version_no"] == 2
        assert v2["row_count"] == 3

    def test_list_versions_newest_first(self, client, stack, dataset):
        _create_version(client, stack, changelog="v1")
        v2 = _create_version(client, stack, changelog="v2")
        resp = client.get("/api/v1/evaluation/datasets/ds1/versions",
                          headers=auth(stack.jwt_eng))
        assert resp.status_code == 200, resp.text
        versions = resp.json()
        assert [v["version_no"] for v in versions] == [2, 1]
        assert versions[0]["changelog"] == "v2"
        assert versions[0]["row_count"] == 3
        assert versions[0]["created_by"] == "eng"

    def test_version_detail_contains_fields(self, client, stack, dataset):
        _create_version(client, stack)
        resp = client.get("/api/v1/evaluation/datasets/ds1/versions/1",
                          headers=auth(stack.jwt_eng))
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert detail["version_no"] == 1
        assert isinstance(detail["fields"], list)


class TestVersionDiff:
    def test_row_level_diff_counts_and_fields(self, client, stack, dataset):
        _create_version(client, stack)
        _mutate(stack)
        _create_version(client, stack)

        resp = client.get("/api/v1/evaluation/datasets/ds1/versions/diff",
                          headers=auth(stack.jwt_eng),
                          params={"from": 1, "to": 2})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["from"]["version_no"] == 1
        assert body["to"]["version_no"] == 2
        assert body["summary"] == {
            "added": 1, "removed": 1, "changed": 1, "unchanged": 1,
            "from_count": 3, "to_count": 3,
        }
        statuses = {r["status"]: r for r in body["rows"]}
        assert set(statuses) == {"added", "removed", "changed"}
        assert statuses["changed"]["key"] == "id:data2_1"
        assert sorted(statuses["changed"]["changed_fields"]) == ["answer", "user_input"]
        assert statuses["added"]["key"] == "id:data2_new"
        assert statuses["removed"]["key"] == "id:data2_2"
        # 变更行双栏值：from=旧值 / to=新值
        assert statuses["changed"]["from_data"]["user_input"] == "q1"
        assert statuses["changed"]["to_data"]["user_input"] == "q1改"

    def test_diff_identical_versions_no_rows(self, client, stack, dataset):
        _create_version(client, stack)
        _create_version(client, stack)
        resp = client.get("/api/v1/evaluation/datasets/ds1/versions/diff",
                          headers=auth(stack.jwt_eng), params={"from": 1, "to": 2})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["summary"]["added"] == 0
        assert body["summary"]["changed"] == 0
        assert body["rows"] == []

    def test_diff_unknown_version_404(self, client, stack, dataset):
        _create_version(client, stack)
        resp = client.get("/api/v1/evaluation/datasets/ds1/versions/diff",
                          headers=auth(stack.jwt_eng), params={"from": 1, "to": 99})
        assert resp.status_code == 404


class TestRestoreVersion:
    def test_restore_rewrites_live_rows_from_snapshot(self, client, stack, dataset):
        _create_version(client, stack)
        _mutate(stack)

        resp = client.post("/api/v1/evaluation/datasets/ds1/versions/1/restore",
                           headers=auth(stack.jwt_eng))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["restored_count"] == 3

        with stack.session() as db:
            active = db.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == "ds1",
                EvaluationDataV2.is_active == True).all()  # noqa: E712
            assert len(active) == 3
            # 恢复的是 v1 内容（q1 原值回来、q2 回来、q3 消失），且行是新写的（新 id）
            assert sorted(row.data["user_input"] for row in active) == ["q0", "q1", "q2"]
            assert all(row.id not in ("data2_0", "data2_1", "data2_2") for row in active)
            dataset_row = db.query(EvaluationDataset).filter(
                EvaluationDataset.id == "ds1").first()
            assert dataset_row.question_count == 3

    def test_restore_unknown_version_404(self, client, stack, dataset):
        resp = client.post("/api/v1/evaluation/datasets/ds1/versions/99/restore",
                           headers=auth(stack.jwt_eng))
        assert resp.status_code == 404


class TestVersionPermissions:
    def test_viewer_readonly(self, client, stack, dataset):
        # 读面放行
        resp = client.get("/api/v1/evaluation/datasets/ds1/versions",
                          headers=auth(stack.jwt_viewer))
        assert resp.status_code == 200
        # 打版/恢复是写操作：viewer 403
        resp = client.post("/api/v1/evaluation/datasets/ds1/versions",
                           headers=auth(stack.jwt_viewer), json={"changelog": None})
        assert resp.status_code == 403
        resp = client.post("/api/v1/evaluation/datasets/ds1/versions/1/restore",
                           headers=auth(stack.jwt_viewer))
        assert resp.status_code == 403

    def test_cross_team_denied(self, client, stack, dataset):
        resp = client.get("/api/v1/evaluation/datasets/ds1/versions",
                          headers=auth(stack.jwt_other))
        assert resp.status_code == 403
        resp = client.post("/api/v1/evaluation/datasets/ds1/versions",
                           headers=auth(stack.jwt_other), json={"changelog": None})
        assert resp.status_code == 403


class TestTaskVersionBinding:
    def _create_task(self, client, stack, dataset_id="ds1", version_no=None):
        payload = {
            "name": "ver-task",
            "evaluation_type": "success_rate",
            "dataset_id": dataset_id,
            "dataset_version_no": version_no,
            "success_rate_config": {"expected_field": "answer", "mode": "contains"},
        }
        return client.post("/api/v1/evaluation/tasks",
                           headers=auth(stack.jwt_eng), json=payload)

    def test_create_task_locked_version(self, client, stack, dataset):
        _create_version(client, stack, changelog="锁定这版")
        # 锁定后活数据再变（q0 内容被改）——任务基数仍按版本行数
        with stack.session() as db:
            row = db.query(EvaluationDataV2).filter(
                EvaluationDataV2.id == "data2_0").first()
            row.data = {"user_input": "q0改", "answer": "a0新"}
            db.commit()

        resp = self._create_task(client, stack, version_no=1)
        assert resp.status_code == 200, resp.text
        task = resp.json()
        assert task["dataset_version_no"] == 1
        assert task["total_questions"] == 3  # 版本行数，不是活行数

    def test_create_task_follow_latest_by_default(self, client, stack, dataset):
        _create_version(client, stack)
        resp = self._create_task(client, stack, version_no=None)
        assert resp.status_code == 200, resp.text
        assert resp.json()["dataset_version_no"] is None

    def test_create_task_unknown_version_400(self, client, stack, dataset):
        _create_version(client, stack)
        resp = self._create_task(client, stack, version_no=99)
        assert resp.status_code == 400
        assert "不存在版本" in resp.json()["detail"]

    def test_create_task_version_without_dataset_400(self, client, stack):
        resp = self._create_task(client, stack, dataset_id=None, version_no=1)
        assert resp.status_code == 400
        assert "先选择数据集" in resp.json()["detail"]

    def test_update_task_unlock_and_relock(self, client, stack, dataset):
        _create_version(client, stack)
        task_id = self._create_task(client, stack, version_no=1).json()["id"]

        # 解锁 → 跟随最新
        resp = client.put(f"/api/v1/evaluation/tasks/{task_id}",
                          headers=auth(stack.jwt_eng),
                          json={"dataset_version_no": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["dataset_version_no"] is None

        # 重新锁定 v1
        resp = client.put(f"/api/v1/evaluation/tasks/{task_id}",
                          headers=auth(stack.jwt_eng),
                          json={"dataset_version_no": 1})
        assert resp.status_code == 200, resp.text
        assert resp.json()["dataset_version_no"] == 1

    def test_update_task_switch_dataset_keeps_stale_version_400(
            self, client, stack, dataset):
        from app.models.dataset_models import EvaluationDataV2 as V2

        _create_version(client, stack)
        with stack.session() as db:
            db.add(EvaluationDataset(id="ds2", name="other-ds", team_code="DEFAULT"))
            db.add(V2(id="d2_0", dataset_id="ds2", index=0,
                      data={"user_input": "x", "answer": "y"}))
            db.commit()

        task_id = self._create_task(client, stack, version_no=1).json()["id"]
        # 只换数据集不带版本号：旧版本号不属于新数据集 → 400（必须同批切换）
        resp = client.put(f"/api/v1/evaluation/tasks/{task_id}",
                          headers=auth(stack.jwt_eng),
                          json={"dataset_id": "ds2"})
        assert resp.status_code == 400
        assert "不存在版本" in resp.json()["detail"]

        # 同批切换（换数据集 + 解锁版本）→ 通过
        resp = client.put(f"/api/v1/evaluation/tasks/{task_id}",
                          headers=auth(stack.jwt_eng),
                          json={"dataset_id": "ds2", "dataset_version_no": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["dataset_id"] == "ds2"
        assert resp.json()["dataset_version_no"] is None


class TestLockedVersionExecutionLoading:
    """执行侧：锁定版本任务经版本快照加载数据（活数据后续变更不影响口径）"""

    def test_load_dataset_uses_version_snapshot(self, client, stack, dataset):
        from types import SimpleNamespace

        from app.tasks.evaluation_tasks_async import _load_dataset

        _create_version(client, stack)
        # 打版后活数据继续漂移：追加 2 行
        with stack.session() as db:
            for i in (4, 5):
                db.add(EvaluationDataV2(
                    id=f"data2_late{i}", dataset_id="ds1", index=i,
                    data={"user_input": f"late{i}", "answer": f"late{i}"}))
            db.commit()

        with stack.session() as db:
            locked = SimpleNamespace(
                dataset_id="ds1", dataset_snapshot_id=None, dataset_version_no=1,
                evaluation_type="judge", safety_config=None)
            items = _load_dataset(locked, db)
            assert [it["data"]["user_input"] for it in items] == ["q0", "q1", "q2"]

            follow = SimpleNamespace(
                dataset_id="ds1", dataset_snapshot_id=None, dataset_version_no=None,
                evaluation_type="judge", safety_config=None)
            assert len(_load_dataset(follow, db)) == 5

    def test_load_dataset_missing_version_raises(self, client, stack, dataset):
        from types import SimpleNamespace

        from app.tasks.evaluation_tasks_async import _load_dataset

        with stack.session() as db:
            locked = SimpleNamespace(
                dataset_id="ds1", dataset_snapshot_id=None, dataset_version_no=99,
                evaluation_type="judge", safety_config=None)
            with pytest.raises(ValueError, match="锁定的数据集版本不存在"):
                _load_dataset(locked, db)
