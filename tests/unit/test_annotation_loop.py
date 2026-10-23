"""人工标注闭环测试（M8 QE8-1）

链路是真实的：TestClient → 真 AuthMiddleware → results/stats router → sqlite。
覆盖方案 §M8「标注闭环」验收主链路「标注 → 一致性报告」：
1. 标注队列：失败行（执行失败/裁判失败）最优先，其余按裁判分升序
   （低分优先）；annotation 字段空=待标，非空=已标/跳过；
2. 金分提交：一行一条幂等覆盖（修正）、越界分数 400、viewer 403、
   跨团队 403、跨任务 result_row_id 404；
3. 一致性报告：Pearson/Spearman（scipy 对拍手算值）、分带 Cohen's kappa
   （含手算用例）、skipped/无裁判分行不入样本、全任务聚合 by_task；
4. 退化样本（n<2、零方差）返回 null 而非 nan。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.evaluation_task_api import results_router
from app.api.stats_api import router as stats_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.task_models import EvaluationResult, EvaluationTask
from app.models.user_models import User
from app.services.annotation_service import (
    _band,
    _cohen_kappa,
    judge_agreement_report,
    get_annotation_queue,
    submit_annotation,
)
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
        db.add(User(user_code="boss", username="boss", is_active=True,
                    super_admin_flag=1, application_admin=0, org_code="DEFAULT"))
        db.add(User(user_code="other", username="other", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="OTHER"))
        db.commit()

    app = FastAPI()
    app.include_router(results_router, prefix="/api/v1")
    app.include_router(stats_router, prefix="/api/v1")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    app.dependency_overrides[get_db] = lambda: TestSession()
    return type("Stack", (), {
        "app": app, "session": TestSession,
        "jwt_eng": create_token("eng", "eng"),
        "jwt_viewer": create_token("viewer", "viewer"),
        "jwt_boss": create_token("boss", "boss"),
        "jwt_other": create_token("other", "other"),
    })()


@pytest.fixture
def client(stack):
    return TestClient(stack.app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def task_with_rows(stack):
    """任务 t1（max_score=10）第 1 次执行 4 行：

    - data_index 0：completed，裁判分 8.0（高带）
    - data_index 1：failed（执行失败）
    - data_index 2：completed，裁判分 2.0（低带）
    - data_index 3：judge_failed（无裁判分）
    """
    with stack.session() as db:
        db.add(EvaluationTask(id="t1", name="标注任务", evaluation_type="judge",
                              max_score=10, execution_count=1, team_code="DEFAULT",
                              status="completed"))
        rows = [
            dict(data_index=0, status="completed",
                 aggregate={"average_score": 8.0, "feedback": "好"},
                 input_data={"user_input": "q0"}, model_output="a0"),
            dict(data_index=1, status="failed", aggregate=None,
                 input_data={"user_input": "q1"}, model_output=None, error="boom"),
            dict(data_index=2, status="completed",
                 aggregate={"average_score": 2.0, "feedback": "差"},
                 input_data={"user_input": "q2"}, model_output="a2"),
            dict(data_index=3, status="judge_failed", aggregate={},
                 input_data={"user_input": "q3"}, model_output="a3"),
        ]
        for r in rows:
            db.add(EvaluationResult(task_id="t1", execution_number=1, **r))
        db.commit()
    return "t1"


class TestQueueOrdering:
    def test_failed_rows_first_then_low_score(self, stack, task_with_rows):
        with stack.session() as db:
            task = db.get(EvaluationTask, "t1")
            queue = get_annotation_queue(db, task, 1)
        assert queue["total"] == 4
        assert queue["pending"] == 4 and queue["annotated"] == 0
        assert queue["max_score"] == 10
        # 失败行（failed/judge_failed）最优先，组内按数据序；低分行 2.0 在 8.0 前
        assert [i["row_status"] for i in queue["items"]] == [
            "failed", "judge_failed", "completed", "completed"]
        assert [i["judge_score"] for i in queue["items"]] == [None, None, 2.0, 8.0]
        assert all(i["annotation"] is None for i in queue["items"])

    def test_default_execution_number_is_latest(self, client, stack, task_with_rows):
        resp = client.get("/api/v1/evaluation/results/t1/annotations",
                          headers=auth(stack.jwt_eng))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["execution_number"] == 1  # task.execution_count
        assert len(body["items"]) == 4

    def test_annotated_rows_carry_annotation(self, stack, task_with_rows):
        with stack.session() as db:
            task = db.get(EvaluationTask, "t1")
            row_id = (db.query(EvaluationResult)
                      .filter(EvaluationResult.data_index == 0,
                              EvaluationResult.task_id == "t1").first().id)
            submit_annotation(db, task, result_row_id=row_id, gold_score=9.0,
                              annotator="eng")
            queue = get_annotation_queue(db, task, 1)
        assert queue["annotated"] == 1 and queue["pending"] == 3
        item = next(i for i in queue["items"] if i["result_row_id"] == row_id)
        assert item["annotation"]["gold_score"] == 9.0
        assert item["annotation"]["status"] == "completed"


class TestSubmitApi:
    def test_submit_and_upsert_overwrite(self, client, stack, task_with_rows):
        with stack.session() as db:
            row_id = (db.query(EvaluationResult)
                      .filter(EvaluationResult.task_id == "t1",
                              EvaluationResult.data_index == 0).first().id)
        url = "/api/v1/evaluation/results/t1/annotations"
        resp = client.post(url, headers=auth(stack.jwt_eng),
                           json={"result_row_id": row_id, "gold_score": 7.5})
        assert resp.status_code == 200, resp.text
        assert resp.json()["gold_score"] == 7.5
        # 修正：同 row 再次提交为幂等覆盖，不产生第二条
        resp2 = client.post(url, headers=auth(stack.jwt_eng),
                            json={"result_row_id": row_id, "gold_score": 8.5,
                                  "notes": "复看后修正"})
        assert resp2.status_code == 200
        assert resp2.json()["gold_score"] == 8.5
        with stack.session() as db:
            from app.models.annotation_models import ResultAnnotation
            assert db.query(ResultAnnotation).count() == 1

    def test_score_out_of_range_400(self, client, stack, task_with_rows):
        with stack.session() as db:
            row_id = (db.query(EvaluationResult)
                      .filter(EvaluationResult.task_id == "t1",
                              EvaluationResult.data_index == 0).first().id)
        resp = client.post("/api/v1/evaluation/results/t1/annotations",
                           headers=auth(stack.jwt_eng),
                           json={"result_row_id": row_id, "gold_score": 11})
        assert resp.status_code == 400
        assert "0~10" in resp.json()["detail"]

    def test_missing_score_on_completed_400(self, client, stack, task_with_rows):
        with stack.session() as db:
            row_id = (db.query(EvaluationResult)
                      .filter(EvaluationResult.task_id == "t1",
                              EvaluationResult.data_index == 0).first().id)
        resp = client.post("/api/v1/evaluation/results/t1/annotations",
                           headers=auth(stack.jwt_eng),
                           json={"result_row_id": row_id})
        assert resp.status_code == 400

    def test_skip_row_stores_no_score(self, client, stack, task_with_rows):
        with stack.session() as db:
            row_id = (db.query(EvaluationResult)
                      .filter(EvaluationResult.task_id == "t1",
                              EvaluationResult.data_index == 3).first().id)
        resp = client.post("/api/v1/evaluation/results/t1/annotations",
                           headers=auth(stack.jwt_eng),
                           json={"result_row_id": row_id, "status": "skipped"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "skipped" and body["gold_score"] is None

    def test_unknown_row_404(self, client, stack, task_with_rows):
        resp = client.post("/api/v1/evaluation/results/t1/annotations",
                           headers=auth(stack.jwt_eng),
                           json={"result_row_id": 99999, "gold_score": 5})
        assert resp.status_code == 404

    def test_viewer_forbidden(self, client, stack, task_with_rows):
        with stack.session() as db:
            row_id = (db.query(EvaluationResult)
                      .filter(EvaluationResult.task_id == "t1",
                              EvaluationResult.data_index == 0).first().id)
        resp = client.post("/api/v1/evaluation/results/t1/annotations",
                           headers=auth(stack.jwt_viewer),
                           json={"result_row_id": row_id, "gold_score": 5})
        assert resp.status_code == 403

    def test_cross_team_invisible(self, client, stack, task_with_rows):
        """异团队用户连队列都看不到（对象级归属闸，ensure_task_access）"""
        resp = client.get("/api/v1/evaluation/results/t1/annotations",
                          headers=auth(stack.jwt_other))
        assert resp.status_code == 403
        resp = client.post("/api/v1/evaluation/results/t1/annotations",
                           headers=auth(stack.jwt_other),
                           json={"result_row_id": 1, "gold_score": 5})
        assert resp.status_code == 403

    def test_cross_task_row_404(self, client, stack, task_with_rows):
        """行结果属于其他任务：按当前任务提交必须 404（防跨任务越权标注）"""
        with stack.session() as db:
            db.add(EvaluationTask(id="t2", name="任务2", evaluation_type="judge",
                                  max_score=10, execution_count=1,
                                  team_code="DEFAULT", status="completed"))
            db.add(EvaluationResult(task_id="t2", execution_number=1, data_index=0,
                                    status="completed",
                                    aggregate={"average_score": 5.0}))
            db.commit()
            other_row_id = (db.query(EvaluationResult)
                            .filter(EvaluationResult.task_id == "t2").first().id)
        resp = client.post("/api/v1/evaluation/results/t1/annotations",
                           headers=auth(stack.jwt_eng),
                           json={"result_row_id": other_row_id, "gold_score": 5})
        assert resp.status_code == 404


class TestAgreementMath:
    def test_band_edges(self):
        # 得分率分带：<60% 低 / <80% 中 / 其余高（max_score=10）
        assert _band(5.9, 10) == "low"
        assert _band(6.0, 10) == "mid"
        assert _band(7.9, 10) == "mid"
        assert _band(8.0, 10) == "high"
        assert _band(10, 10) == "high"
        # max_score=100 同口径
        assert _band(59, 100) == "low"
        assert _band(60, 100) == "mid"
        assert _band(80, 100) == "high"

    def test_kappa_perfect_agreement(self):
        a = ["low", "mid", "high", "low"]
        assert _cohen_kappa(a, list(a)) == 1.0

    def test_kappa_hand_computed(self):
        # 手算用例：judge=[低,低,中,中,高,高], human=[低,中,中,中,高,高]
        # po=5/6；judge 边际 (2,2,2)/6，human 边际 (1,3,2)/6
        # pe=(2·1+2·3+2·2)/36=12/36=1/3；kappa=(5/6−1/3)/(1−1/3)=0.5/(2/3)=0.75
        judge = ["low", "low", "mid", "mid", "high", "high"]
        human = ["low", "mid", "mid", "mid", "high", "high"]
        assert _cohen_kappa(judge, human) == pytest.approx(0.75, abs=1e-3)

    def test_kappa_degenerate_single_band(self):
        # 双方全落同一带：pe=1，kappa 无定义 → None（不产出误导性 1.0）
        assert _cohen_kappa(["high", "high"], ["high", "high"]) is None

    def test_pearson_matches_manual(self):
        from app.services.annotation_service import _pearson, _spearman
        # 完全线性：judge=[1,2,3], gold=[2,4,6] → r=1
        assert _pearson([1, 2, 3], [2, 4, 6])["r"] == pytest.approx(1.0)
        # 单调非线性：Spearman=1 而 Pearson<1
        p = _pearson([1, 2, 3, 4], [1, 4, 9, 16])
        s = _spearman([1, 2, 3, 4], [1, 4, 9, 16])
        assert p["r"] < 0.999
        assert s["rho"] == pytest.approx(1.0)
        # 退化：n<2 / 零方差 → None（前端显示「样本不足」）
        assert _pearson([3], [3]) is None
        assert _pearson([2, 2, 2], [1, 2, 3]) is None
        assert _spearman([2, 2, 2], [1, 2, 3]) is None


class TestAgreementReport:
    def _seed_and_annotate(self, stack):
        """5 对可比样本 + 1 行 skipped + 1 行 judge_failed（均不入样本）

        完成 5 行的 (judge, gold) 对（按 data_index 升序）：
        (8.0,9.5) (2.0,1.0) (6.5,7.0) (8.5,7.0) (10.0,9.0)——
        仅第 4 对错带（8.5→高 vs 7.0→中），带一致率 4/5。
        """
        judge_scores = [8.0, 2.0, 6.5, 8.5, 10.0]
        gold_scores = [9.5, 1.0, 7.0, 7.0, 9.0]
        with stack.session() as db:
            task = db.get(EvaluationTask, "t1")
            completed = (db.query(EvaluationResult)
                         .filter(EvaluationResult.task_id == "t1",
                                 EvaluationResult.status == "completed")
                         .order_by(EvaluationResult.data_index).all())
            assert len(completed) == 2  # fixture 原有 2 行 completed
            # 扩到 5 行 completed + 1 行额外 judge_failed
            for i in range(3):
                db.add(EvaluationResult(
                    task_id="t1", execution_number=1, data_index=10 + i,
                    status="completed",
                    aggregate={"average_score": judge_scores[2 + i]},
                    input_data={"user_input": f"q{10 + i}"},
                    model_output="a"))
            db.add(EvaluationResult(
                task_id="t1", execution_number=1, data_index=99,
                status="judge_failed", aggregate={}))
            db.commit()
            completed = (db.query(EvaluationResult)
                         .filter(EvaluationResult.task_id == "t1",
                                 EvaluationResult.status == "completed")
                         .order_by(EvaluationResult.data_index).all())
            for row, gold in zip(completed, gold_scores):
                submit_annotation(db, task, result_row_id=row.id, gold_score=gold,
                                  annotator="eng")
            # data_index=99 的 judge_failed 行：skipped（不入样本）
            jf_row = (db.query(EvaluationResult)
                      .filter(EvaluationResult.data_index == 99).first())
            submit_annotation(db, task, result_row_id=jf_row.id, gold_score=None,
                              status="skipped", annotator="eng")
            # fixture 原有 judge_failed 行（data_index=3）打金分：无裁判分被排除
            jf0 = (db.query(EvaluationResult)
                   .filter(EvaluationResult.data_index == 3).first())
            submit_annotation(db, task, result_row_id=jf0.id, gold_score=0.0,
                              annotator="eng")

    def test_report_scopes_and_exclusions(self, stack, task_with_rows):
        self._seed_and_annotate(stack)
        judge = [8.0, 2.0, 6.5, 8.5, 10.0]
        gold = [9.5, 1.0, 7.0, 7.0, 9.0]
        with stack.session() as db:
            report = judge_agreement_report(db, task_id="t1")
        assert report["sample_count"] == 5
        assert report["skipped_count"] == 1
        assert report["excluded_no_judge"] == 1
        # 相关性与 scipy 直算对拍（同一组数，服务只是包了一层口径）
        from scipy.stats import pearsonr, spearmanr
        assert report["pearson"]["r"] == pytest.approx(
            round(float(pearsonr(judge, gold).statistic), 4))
        assert report["spearman"]["rho"] == pytest.approx(
            round(float(spearmanr(judge, gold).statistic), 4))
        # 5 对中 4 对同带（仅 8.5→高 vs 7.0→中 错带）→ 一致率 0.8
        assert report["band_agreement_rate"] == 0.8
        assert report["confusion"]["matrix"][2][1] == 1  # 裁判高 × 人工中
        assert report["mae"] == pytest.approx(
            sum(abs(j - g) for j, g in zip(judge, gold)) / 5, abs=1e-3)
        # 指定任务时无 by_task 全量展开
        assert "by_task" not in report

    def test_report_all_tasks_aggregation(self, stack, task_with_rows):
        self._seed_and_annotate(stack)
        with stack.session() as db:
            report = judge_agreement_report(db)
        assert report["sample_count"] == 5
        assert len(report["by_task"]) == 1
        assert report["by_task"][0]["task_id"] == "t1"
        assert report["by_task"][0]["sample_count"] == 5

    def test_report_empty_state(self, stack, task_with_rows):
        with stack.session() as db:
            report = judge_agreement_report(db, task_id="t1")
        assert report["sample_count"] == 0
        assert report["pearson"] is None and report["cohen_kappa"] is None
        assert report["band_agreement_rate"] is None

    def test_stats_endpoint(self, client, stack, task_with_rows):
        self._seed_and_annotate(stack)
        resp = client.get("/api/v1/stats/judge-agreement",
                          params={"task_id": "t1"}, headers=auth(stack.jwt_boss))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sample_count"] == 5
        assert body["confusion"]["bands"] == ["low", "mid", "high"]

    def test_stats_endpoint_requires_super_admin(self, client, stack,
                                                 task_with_rows):
        resp = client.get("/api/v1/stats/judge-agreement",
                          headers=auth(stack.jwt_eng))
        assert resp.status_code == 403
