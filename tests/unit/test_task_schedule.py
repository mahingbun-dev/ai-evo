"""定时评测与周报测试（M5 QE5-3）

链路是真实的：TestClient → 真 AuthMiddleware → task router → sqlite。
覆盖方案 §5.3 验收口径：
1. cron 引擎：5 段解析、下次触发计算、非法/永不触发表达式拒绝；
2. 定时 CRUD：PUT 幂等 upsert（校验 cron/window/任务存在）、GET/DELETE、
   viewer 只读 403、跨团队 403；
3. 扫描投递（service 层直测，FakeRedis + monkeypatch submit_task）：
   - due → dispatched：走 start_task 互斥 + submit_task(priority=NORMAL)，
     next_run_at CAS 推进、历史留痕；
   - 任务 running → skipped_running（互斥，不投递）；
   - 错过窗口：next_run_at 落在允许时段外 → skipped_window 消耗该次触发；
   - 错过补跑：停机多个周期只补跑一次，next_run_at 对齐到未来；
   - 提交被拒（团队配额超限路径）→ 任务落 failed + submit_rejected；
   - Redis 锁：他进程持锁 → 本轮让位（skipped_lock）；
4. 周报：build_weekly_report 快照（执行统计/覆盖度/提效/门禁通过率进
   markdown）；generate_if_due 周一 08:00 前不生成、到点生成并推 webhook、
   幂等（重复调用不重复生成）。
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.evaluation_task_api import router as task_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.schedule_models import TaskSchedule, WeeklyReport
from app.models.stats_models import TaskExecutionStats, WeeklyPlanTask
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.models.user_models import User
from app.models.webhook_models import WebhookEndpoint
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.cron_service import cron_next, validate_cron
from app.services.stats_service import BEIJING_TZ
from app.services.task_schedule_service import TaskScheduleService
from app.services.jwt_service import create_token
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit


# ==================== cron 引擎 ====================

class TestCronEngine:
    def test_basic_next_computations(self):
        now = datetime(2026, 10, 23, 10, 30, tzinfo=BEIJING_TZ)  # 周五
        # 每天 08:00 → 明天
        assert cron_next("0 8 * * *", now) == datetime(2026, 10, 24, 8, 0, tzinfo=BEIJING_TZ)
        # 工作日 09:00 → 下周一（周五 10:30 已过当天 09:00）
        assert cron_next("0 9 * * 1-5", now) == datetime(2026, 10, 26, 9, 0, tzinfo=BEIJING_TZ)
        # 步进：*/5 从 10:30 出发 → 10:35（所在分钟不重复触发）
        assert cron_next("*/5 * * * *", now) == datetime(2026, 10, 23, 10, 35, tzinfo=BEIJING_TZ)
        # 月内日：11 月 1 日 08:00
        assert cron_next("0 8 1 * *", now) == datetime(2026, 11, 1, 8, 0, tzinfo=BEIJING_TZ)

    def test_dow_7_is_sunday(self):
        # 周六 22:00，cron "0 10 * * 7"（7=周日）→ 明天周日 10:00
        sat = datetime(2026, 10, 24, 22, 0, tzinfo=BEIJING_TZ)
        assert cron_next("0 10 * * 7", sat) == datetime(2026, 10, 25, 10, 0, tzinfo=BEIJING_TZ)

    def test_dom_dow_or_semantics(self):
        # 日与周都受限取「或」：cron "0 8 1 * 1" 周五 → 下周一（周命中）先于 11/1
        now = datetime(2026, 10, 23, 10, 0, tzinfo=BEIJING_TZ)
        assert cron_next("0 8 1 * 1", now) == datetime(2026, 10, 26, 8, 0, tzinfo=BEIJING_TZ)

    def test_invalid_and_never_firing(self):
        for bad in ["", "0 8 * *", "61 * * * *", "0 8 * * 8", "*/0 * * * *", "a * * * *"]:
            with pytest.raises(ValueError):
                validate_cron(bad)
        # 2 月 30 日不存在 → 一年内无触发
        with pytest.raises(ValueError):
            validate_cron("0 0 30 2 *")

    def test_naive_after_treated_as_beijing(self):
        naive = datetime(2026, 10, 23, 10, 30)
        assert cron_next("0 8 * * *", naive).hour == 8


# ==================== API 栈 ====================

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


@pytest.fixture
def task(stack, client):
    """真实任务（走任务 API 创建，保证校验口径一致）"""
    resp = client.post("/api/v1/evaluation/tasks",
                       headers={"Authorization": f"Bearer {stack.jwt_eng}"},
                       json={"name": "定时被测任务", "evaluation_type": "success_rate",
                             "success_rate_config": {"expected_field": "answer",
                                                     "mode": "contains"},
                             "judge_count": 0})
    assert resp.status_code == 200, resp.text
    return resp.json()


def auth(stack, who="eng"):
    return {"Authorization": f"Bearer {stack.jwt_eng if who == 'eng' else stack.jwt_viewer}"}


# ==================== 定时 CRUD API ====================

class TestScheduleCrud:
    def test_get_unset_returns_404(self, client, stack, task):
        resp = client.get(f"/api/v1/evaluation/tasks/{task['id']}/schedule",
                          headers=auth(stack))
        assert resp.status_code == 404

    def test_upsert_get_delete_roundtrip(self, client, stack, task):
        resp = client.put(f"/api/v1/evaluation/tasks/{task['id']}/schedule",
                          headers=auth(stack),
                          json={"cron": "0 8 * * *", "enabled": True,
                                "window_start": "08:00", "window_end": "20:00"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cron"] == "0 8 * * *"
        assert body["enabled"] is True
        assert body["next_run_at"]  # 下次运行时间已算出
        assert body["history"] == []

        got = client.get(f"/api/v1/evaluation/tasks/{task['id']}/schedule",
                         headers=auth(stack)).json()
        assert got["window_start"] == "08:00"

        # 更新（幂等 upsert：同任务一条）
        resp = client.put(f"/api/v1/evaluation/tasks/{task['id']}/schedule",
                          headers=auth(stack), json={"cron": "*/30 * * * *"})
        assert resp.status_code == 200
        assert resp.json()["cron"] == "*/30 * * * *"
        assert resp.json()["window_start"] is None

        # 删除后再查 404
        resp = client.delete(f"/api/v1/evaluation/tasks/{task['id']}/schedule",
                             headers=auth(stack))
        assert resp.status_code == 200
        assert client.get(f"/api/v1/evaluation/tasks/{task['id']}/schedule",
                          headers=auth(stack)).status_code == 404

    def test_invalid_cron_rejected(self, client, stack, task):
        resp = client.put(f"/api/v1/evaluation/tasks/{task['id']}/schedule",
                          headers=auth(stack), json={"cron": "not a cron"})
        assert resp.status_code == 400
        assert "cron" in resp.json()["detail"]

    def test_half_window_rejected(self, client, stack, task):
        resp = client.put(f"/api/v1/evaluation/tasks/{task['id']}/schedule",
                          headers=auth(stack),
                          json={"cron": "0 8 * * *", "window_start": "08:00"})
        assert resp.status_code == 400

    def test_viewer_readonly(self, client, stack, task):
        assert client.put(
            f"/api/v1/evaluation/tasks/{task['id']}/schedule",
            headers=auth(stack, "viewer"), json={"cron": "0 8 * * *"}).status_code == 403
        assert client.delete(
            f"/api/v1/evaluation/tasks/{task['id']}/schedule",
            headers=auth(stack, "viewer")).status_code == 403

    def test_cross_team_invisible(self, client, stack, task):
        headers = {"Authorization": f"Bearer {stack.jwt_other}"}
        assert client.put(
            f"/api/v1/evaluation/tasks/{task['id']}/schedule",
            headers=headers, json={"cron": "0 8 * * *"}).status_code == 403
        assert client.get(
            f"/api/v1/evaluation/tasks/{task['id']}/schedule",
            headers=headers).status_code == 403


# ==================== 扫描投递（service 层） ====================

class _SubmitRecorder:
    """替身 submit_task：记录调用与 priority；return_value 控制配额拒绝路径"""

    def __init__(self, return_value=True):
        self.calls = []
        self.return_value = return_value

    def __call__(self, task_id, *, high_priority=True):
        self.calls.append({"task_id": task_id, "high_priority": high_priority})
        return self.return_value


@pytest.fixture
def schedule_row(stack, task):
    """直插一条 due 调度（cron 每天 03:00，next_run_at 已过期）"""
    now = datetime.now(BEIJING_TZ)
    with stack.session() as db:
        row = TaskSchedule(
            id="sch_test", task_id=task["id"], cron="0 3 * * *", enabled=True,
            next_run_at=now - timedelta(hours=2), created_by="eng")
        db.add(row)
        db.commit()
        return row.id


def _scan(stack, monkeypatch, submit_recorder, now=None, redis_client=None):
    monkeypatch.setattr(
        "app.services.task_scheduler.TaskScheduler.submit_task", submit_recorder)
    with stack.session() as db:
        counts = TaskScheduleService(db).scan_due(now=now, redis_client=redis_client)
    return counts


def _aware(dt):
    """sqlite 读回 naive datetime（DateTime(timezone=True) 不生效），补北京时区"""
    return dt if dt.tzinfo else dt.replace(tzinfo=BEIJING_TZ)


class TestScanDue:
    def test_due_dispatches_via_run_entry(self, stack, task, schedule_row, monkeypatch):
        rec = _SubmitRecorder()
        now = datetime.now(BEIJING_TZ)
        counts = _scan(stack, monkeypatch, rec, now=now, redis_client=FakeRedis())
        assert counts == {"dispatched": 1}
        # 走既有 run 入口语义：submit 恰好一次、priority=NORMAL（不插队）
        assert len(rec.calls) == 1
        assert rec.calls[0]["task_id"] == task["id"]
        assert rec.calls[0]["high_priority"] is False
        with stack.session() as db:
            task_row = db.query(EvaluationTask).filter_by(id=task["id"]).first()
            assert task_row.status == "running"
            sch = db.query(TaskSchedule).filter_by(id=schedule_row).first()
            # next_run_at 已 CAS 推进到未来的下一次 03:00
            assert _aware(sch.next_run_at) > now
            assert sch.last_status == "dispatched"
            assert sch.last_run_at is not None
            assert sch.history[0]["status"] == "dispatched"

    def test_future_not_due_noop(self, stack, task, monkeypatch):
        with stack.session() as db:
            db.add(TaskSchedule(
                id="sch_future", task_id=task["id"], cron="0 3 * * *", enabled=True,
                next_run_at=datetime.now(BEIJING_TZ) + timedelta(hours=5)))
            db.commit()
        rec = _SubmitRecorder()
        counts = _scan(stack, monkeypatch, rec, redis_client=FakeRedis())
        assert counts == {}
        assert rec.calls == []

    def test_disabled_schedule_skipped(self, stack, task, monkeypatch):
        with stack.session() as db:
            db.add(TaskSchedule(
                id="sch_off", task_id=task["id"], cron="* * * * *", enabled=False,
                next_run_at=datetime.now(BEIJING_TZ) - timedelta(minutes=10)))
            db.commit()
        rec = _SubmitRecorder()
        assert _scan(stack, monkeypatch, rec, redis_client=FakeRedis()) == {}
        assert rec.calls == []

    def test_mutex_running_task_skipped(self, stack, task, schedule_row, monkeypatch):
        with stack.session() as db:
            db.query(EvaluationTask).filter_by(id=task["id"]).update({"status": "running"})
            db.commit()
        rec = _SubmitRecorder()
        counts = _scan(stack, monkeypatch, rec, redis_client=FakeRedis())
        assert counts == {"skipped_running": 1}
        assert rec.calls == []
        with stack.session() as db:
            sch = db.query(TaskSchedule).filter_by(id=schedule_row).first()
            assert sch.last_status == "skipped_running"
            assert _aware(sch.next_run_at) > datetime.now(BEIJING_TZ)

    def test_missed_window_skips_occurrence(self, stack, task, monkeypatch):
        # 时段限制 08:00-20:00，但 cron 每天 03:00：触发时刻永远在窗口外
        now = datetime(2026, 10, 23, 3, 5, tzinfo=BEIJING_TZ)
        with stack.session() as db:
            db.add(TaskSchedule(
                id="sch_win", task_id=task["id"], cron="0 3 * * *", enabled=True,
                window_start="08:00", window_end="20:00",
                next_run_at=datetime(2026, 10, 23, 3, 0, tzinfo=BEIJING_TZ)))
            db.commit()
        rec = _SubmitRecorder()
        counts = _scan(stack, monkeypatch, rec, now=now, redis_client=FakeRedis())
        assert counts == {"skipped_window": 1}
        assert rec.calls == []
        with stack.session() as db:
            sch = db.query(TaskSchedule).filter_by(id="sch_win").first()
            assert sch.last_status == "skipped_window"
            # 该次触发被消耗，next_run_at 推进到下一天 03:00（节律不乱）
            assert _aware(sch.next_run_at) == datetime(2026, 10, 24, 3, 0, tzinfo=BEIJING_TZ)

    def test_catchup_fires_once_after_downtime(self, stack, task, monkeypatch):
        # 停机 3 天：next_run_at 落在 3 天前，只补跑一次并对齐到未来
        now = datetime(2026, 10, 23, 12, 0, tzinfo=BEIJING_TZ)
        with stack.session() as db:
            db.add(TaskSchedule(
                id="sch_late", task_id=task["id"], cron="0 3 * * *", enabled=True,
                next_run_at=datetime(2026, 10, 20, 3, 0, tzinfo=BEIJING_TZ)))
            db.commit()
        rec = _SubmitRecorder()
        counts = _scan(stack, monkeypatch, rec, now=now, redis_client=FakeRedis())
        assert counts == {"dispatched": 1}
        assert len(rec.calls) == 1
        with stack.session() as db:
            sch = db.query(TaskSchedule).filter_by(id="sch_late").first()
            assert _aware(sch.next_run_at) == datetime(2026, 10, 24, 3, 0, tzinfo=BEIJING_TZ)

    def test_quota_rejection_fails_task(self, stack, task, schedule_row, monkeypatch):
        # submit_task 返回 False（团队配额超限/队列不可达）→ 与 run 端点
        # 同款失败语义：任务落 failed，历史记 submit_rejected
        # 注：旁路 StatsService.complete_record——fail_task 顺带的耗时统计在
        # sqlite 上有既存 naive/aware 相减炸点（stats_service.py:75，PG 带时区
        # 不受影响），与本用例断言的配额拒绝语义无关
        from app.services.stats_service import StatsService
        monkeypatch.setattr(StatsService, "complete_record", lambda self, task_id,
                            success=True: None)
        rec = _SubmitRecorder(return_value=False)
        counts = _scan(stack, monkeypatch, rec, redis_client=FakeRedis())
        assert counts == {"submit_rejected": 1}
        with stack.session() as db:
            task_row = db.query(EvaluationTask).filter_by(id=task["id"]).first()
            assert task_row.status == "failed"
            sch = db.query(TaskSchedule).filter_by(id=schedule_row).first()
            assert sch.last_status == "submit_rejected"
            assert "配额" in (sch.last_message or "")

    def test_redis_lock_single_writer(self, stack, task, monkeypatch):
        # 他进程持锁：本轮让位，不扫不投递
        redis = FakeRedis()
        redis.set("task_schedule_scan_lock", "someone-else", nx=True, ex=55)
        rec = _SubmitRecorder()
        with stack.session() as db:
            db.add(TaskSchedule(
                id="sch_lock", task_id=task["id"], cron="* * * * *", enabled=True,
                next_run_at=datetime.now(BEIJING_TZ) - timedelta(minutes=1)))
            db.commit()
        counts = _scan(stack, monkeypatch, rec, redis_client=redis)
        assert counts == {"skipped_lock": 1}
        assert rec.calls == []
        # 锁未被误删（仍是他进程的值）
        assert redis.get("task_schedule_scan_lock") == "someone-else"

    def test_lock_released_after_scan(self, stack, task, schedule_row, monkeypatch):
        redis = FakeRedis()
        _scan(stack, monkeypatch, _SubmitRecorder(), redis_client=redis)
        assert redis.get("task_schedule_scan_lock") is None

    def test_task_deleted_schedule_disables_dispatch(self, stack, task,
                                                     schedule_row, monkeypatch):
        with stack.session() as db:
            db.query(EvaluationTask).filter_by(id=task["id"]).update({"is_active": False})
            db.commit()
        rec = _SubmitRecorder()
        counts = _scan(stack, monkeypatch, rec, redis_client=FakeRedis())
        assert counts == {"task_unavailable": 1}
        assert rec.calls == []


# ==================== 周报 ====================

def _seed_week(stack, week_start):
    """上一周数据：2 成功 1 失败执行、1 条覆盖度计划、1 条带提效与门禁的执行记录"""
    from datetime import date
    ws = datetime.combine(week_start, datetime.min.time(), tzinfo=BEIJING_TZ)
    we = ws + timedelta(days=6, hours=23)
    with stack.session() as db:
        for i in range(3):
            db.add(TaskExecutionStats(
                id=f"stat_{i}", task_id="t1", evaluation_type="success_rate",
                status="success" if i < 2 else "failed",
                created_at=ws + timedelta(days=i), duration_ms=3600_000))
        db.add(WeeklyPlanTask(id="wp1", week_start_date=week_start,
                              week_end_date=week_start + timedelta(days=6),
                              planned_count=4))
        db.add(TaskExecutionRecord(
            id="rec1", task_id="t1", execution_number=1, status="completed",
            started_at=ws + timedelta(hours=1), completed_at=we,
            estimated_manual_hours=5.0,
            gate_result={"verdict": "passed", "checks": []}))
        db.add(TaskExecutionRecord(
            id="rec2", task_id="t1", execution_number=2, status="completed",
            started_at=ws + timedelta(hours=2), completed_at=we,
            gate_result={"verdict": "failed", "checks": []}))
        db.commit()


class TestWeeklyReport:
    def test_build_snapshot_markdown(self, stack):
        from datetime import date
        week_start = date(2026, 10, 12)  # 周一
        _seed_week(stack, week_start)
        from app.services.weekly_report_service import build_weekly_report
        with stack.session() as db:
            result = build_weekly_report(db, week_start)
        assert result["week_start"] == "2026-10-12"
        assert result["week_end"] == "2026-10-18"
        content = result["content"]
        # 快照断言：四类指标都进正文
        assert "| 执行总数 | 3 |" in content
        assert "| 成功 / 失败 | 2 / 1 |" in content
        assert "| 2026-10-12 ~ 2026-10-18 | 4 | 2 | 50.0% |" in content
        assert "总提效" in content
        assert "通过 1、失败 1" in content
        assert "50.0%" in content  # 门禁通过率 1/2

    def test_generate_if_due_gating_and_idempotent(self, stack, monkeypatch):
        from datetime import date
        # 周一 08:00 生成的是「上一个自然周」的报告：10-19（周一）生成 10-12~18
        _seed_week(stack, date(2026, 10, 12))

        # webhook 派发替身：记录调用（不真发）
        dispatched = []

        def fake_dispatch(event, team_code, payload, db=None):
            dispatched.append({"event": event, "team": team_code})
            return 1

        monkeypatch.setattr("app.services.weekly_report_service.dispatch_event",
                            fake_dispatch)
        # 订阅了周报事件的端点（DEFAULT 团队）
        with stack.session() as db:
            db.add(WebhookEndpoint(id=1, team_code="DEFAULT", name="ops",
                                   url="https://example.com/hook", secret="whsec_x",
                                   events=["weekly.report.ready"], active=True))
            db.commit()

        from app.services.weekly_report_service import generate_if_due
        with stack.session() as db:
            # 周一 07:00：未到点不生成
            early = datetime(2026, 10, 19, 7, 0, tzinfo=BEIJING_TZ)
            assert generate_if_due(db, now=early) is None
            # 周一 09:00：生成 + 推送 webhook（订阅团队各一次）
            made = generate_if_due(db, now=datetime(2026, 10, 19, 9, 0, tzinfo=BEIJING_TZ))
        assert made is not None
        assert made["week_start"] == "2026-10-12"
        assert made["pushed_webhooks"] == 1
        assert made["email_status"] == "skipped"  # SMTP 未配置
        assert dispatched == [{"event": "weekly.report.ready", "team": "DEFAULT"}]
        assert "评测周报（2026-10-12 ~ 2026-10-18）" in made["content"]

        # 幂等：同周重复调用（含周三补扫）不再生成
        with stack.session() as db:
            assert generate_if_due(db, now=datetime(2026, 10, 21, 9, 0, tzinfo=BEIJING_TZ)) is None
            rows = db.query(WeeklyReport).all()
        assert len(rows) == 1
        assert rows[0].pushed_webhooks == 1

    def test_generate_catches_up_after_downtime(self, stack, monkeypatch):
        from datetime import date
        _seed_week(stack, date(2026, 10, 12))
        monkeypatch.setattr("app.services.weekly_report_service.dispatch_event",
                            lambda *a, **k: 0)
        from app.services.weekly_report_service import generate_if_due
        # 错过周一 08:00（停机），10:00 恢复：仍补生成上周报告（报告不丢）
        with stack.session() as db:
            made = generate_if_due(db, now=datetime(2026, 10, 19, 10, 0, tzinfo=BEIJING_TZ))
        assert made is not None
        assert made["week_start"] == "2026-10-12"
