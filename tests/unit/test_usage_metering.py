"""M6 QE6-3 单测：用量计量口径（提取/累计/聚合/超额告警不阻断）

覆盖口径（方案 §M6「计量口径单测」）：
- usage 提取：OpenAI 兼容（prompt/completion/total_tokens）与 Anthropic
  （input/output_tokens）两形态统一；无 usage 不猜值（None）；非法值按 0；
- 进程内收集器：多模型累计、reset 清零、collect 返回拷贝（防收割后继续
  累计串账）、只有总量的 record_usage_totals；
- 漏斗收口 _meter_call_result：成功调用附 usage 进返回值；失败不记；
- 月度窗口：缺省当月/指定月/12 月跨年/非法月；
- monthly_usage_summary（sqlite）：团队聚合、跨月不计、NULL team 按
  DEFAULT、旧形态标量 model_usage 兼容、license 对账块；
- check_run_overage：未设上限/未超额不告警；超额告警且**任何异常都不抛**
  （不阻断口径）；Redis 日节流生效；
- 终态收割：complete_record 把收集器累计写进 record.model_usage。
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.services import license_service, model_caller, usage_service
from app.services.license_service import (
    STATUS_VALID, TIER_ENTERPRISE, LicenseState,
)
from app.services.model_caller import (
    _extract_usage, _meter_call_result, collect_usage, record_usage,
    record_usage_totals, reset_usage_tracking,
)

pytestmark = pytest.mark.unit  # W8 分类：sqlite 替身 + 内存计量态，无网络


@pytest.fixture(autouse=True)
def _isolate():
    """测试间清空 license 与计量态（两处模块级单例）"""
    license_service._state = None
    reset_usage_tracking()
    yield
    license_service._state = None
    reset_usage_tracking()


# ==================== usage 提取 ====================

class TestExtractUsage:
    def test_openai_shape(self):
        got = _extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                        "total_tokens": 15}})
        assert got == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_openai_shape_without_total(self):
        got = _extract_usage({"usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        assert got["total_tokens"] == 10  # total 缺失按和补

    def test_anthropic_shape(self):
        got = _extract_usage({"usage": {"input_tokens": 20, "output_tokens": 8}})
        assert got == {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}

    def test_missing_usage_returns_none(self):
        assert _extract_usage({"choices": []}) is None
        assert _extract_usage({"usage": None}) is None
        assert _extract_usage("not a dict") is None
        assert _extract_usage({"usage": {"unrelated": 1}}) is None

    def test_garbage_values_as_zero(self):
        got = _extract_usage({"usage": {"prompt_tokens": "x", "completion_tokens": 2}})
        assert got == {"prompt_tokens": 0, "completion_tokens": 2, "total_tokens": 2}


# ==================== 进程内收集器 ====================

class TestAccumulator:
    def test_accumulate_across_models(self):
        record_usage("m1", 10, 5, 15)
        record_usage("m1", 1, 1, 2)
        record_usage("m2", 100, 0, 100)
        assert collect_usage() == {
            "m1": {"prompt_tokens": 11, "completion_tokens": 6, "total_tokens": 17},
            "m2": {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100},
        }

    def test_totals_only_records(self):
        record_usage_totals("cc-judge", 42)
        got = collect_usage()["cc-judge"]
        assert got["total_tokens"] == 42
        assert got["prompt_tokens"] == 0 and got["completion_tokens"] == 0

    def test_reset_clears(self):
        record_usage("m1", 1, 1, 2)
        reset_usage_tracking()
        assert collect_usage() == {}

    def test_collect_returns_copy(self):
        record_usage("m1", 1, 1, 2)
        snapshot = collect_usage()
        snapshot["m1"]["total_tokens"] = 999
        assert collect_usage()["m1"]["total_tokens"] == 2

    def test_empty_model_ignored(self):
        record_usage("", 1, 1, 2)
        assert collect_usage() == {}


class TestMeterCallResult:
    def test_success_records_and_attaches(self):
        config = {"provider": "openai", "model_type": "gpt-test"}
        result = _meter_call_result(
            {"success": True, "content": "ok",
             "raw": {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}},
            config)
        assert result["usage"] == {"model": "gpt-test", "prompt_tokens": 3,
                                   "completion_tokens": 4, "total_tokens": 7}
        assert collect_usage()["gpt-test"]["total_tokens"] == 7

    def test_failure_not_recorded(self):
        result = _meter_call_result(
            {"success": False, "error": "x",
             "raw": {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}},
            {"model_type": "m"})
        assert "usage" not in result
        assert collect_usage() == {}

    def test_no_usage_passthrough(self):
        result = _meter_call_result(
            {"success": True, "content": "ok", "raw": {"choices": []}},
            {"model_type": "m"})
        assert "usage" not in result
        assert collect_usage() == {}


# ==================== 月度窗口 ====================

class TestMonthWindow:
    def test_default_current_month(self):
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        start, end, label = usage_service._month_window(None, now)
        assert (start, end, label) == (
            datetime(2026, 10, 1).date(), datetime(2026, 11, 1).date(), "2026-10")

    def test_explicit_month(self):
        start, end, label = usage_service._month_window("2026-01")
        assert label == "2026-01" and end == datetime(2026, 2, 1).date()

    def test_december_crosses_year(self):
        start, end, label = usage_service._month_window("2026-12")
        assert label == "2026-12" and end == datetime(2027, 1, 1).date()

    def test_invalid_month_raises(self):
        for bad in ("2026-13", "bad", "202610", "2026-1"):
            with pytest.raises(ValueError):
                usage_service._month_window(bad)


# ==================== 月度聚合（sqlite） ====================

@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[EvaluationTask.__table__,
                                             TaskExecutionRecord.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


def _seed_run(db, task_id, team, started_at, model_usage=None, status="completed"):
    db.add(EvaluationTask(id=task_id, name=task_id, team_code=team,
                          evaluation_type="ai_judge", status=status,
                          created_at=started_at))
    db.add(TaskExecutionRecord(id=f"exec_{task_id}", task_id=task_id,
                               execution_number=1, status=status,
                               started_at=started_at, model_usage=model_usage))


class TestMonthlyUsageSummary:
    def test_team_aggregation_and_month_scope(self, db):
        october = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        september = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        _seed_run(db, "t1", "QE", october,
                  {"gpt-x": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                   "cc-judge": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 30}})
        _seed_run(db, "t2", "QE", october,
                  {"gpt-x": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        _seed_run(db, "t3", "PLAT", october,
                  {"m": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        _seed_run(db, "t4", "QE", september,
                  {"gpt-x": {"prompt_tokens": 999, "completion_tokens": 999, "total_tokens": 999}})
        _seed_run(db, "t5", None, october, None)  # NULL team → DEFAULT，无 usage
        db.commit()

        summary = usage_service.monthly_usage_summary(db, month="2026-10")
        assert summary["period"] == "2026-10"
        assert summary["totals"]["runs"] == 4  # t1/t2/t3/t5 四笔 10 月运行（9 月 t4 不计入）
        assert summary["totals"]["teams"] == 3  # QE / PLAT / DEFAULT(NULL 团队)
        assert summary["totals"]["prompt_tokens"] == 111
        assert summary["totals"]["completion_tokens"] == 56
        assert summary["totals"]["total_tokens"] == 197

        teams = {t["team_code"]: t for t in summary["teams"]}
        assert teams["QE"]["runs"] == 2
        assert teams["QE"]["total_tokens"] == 195
        assert teams["DEFAULT"]["runs"] == 1
        assert teams["DEFAULT"]["total_tokens"] == 0
        # 排序：token 总量降序（对账视图先看大头）
        assert summary["teams"][0]["team_code"] == "QE"

    def test_legacy_scalar_model_usage_compatible(self, db):
        _seed_run(db, "t1", "QE", datetime(2026, 10, 5, tzinfo=timezone.utc),
                  {"old-model": 77})
        db.commit()
        summary = usage_service.monthly_usage_summary(db, month="2026-10")
        assert summary["totals"]["total_tokens"] == 77

    def test_license_reconciliation_block(self, db):
        summary = usage_service.monthly_usage_summary(db, month="2026-10")
        assert summary["license"]["tier"] == "oss"
        assert summary["license"]["max_monthly_runs"] is None
        assert summary["license"]["runs_over_quota"] is False

        license_service._state = LicenseState(
            status=STATUS_VALID, tier=TIER_ENTERPRISE, effective_tier=TIER_ENTERPRISE,
            customer="ACME", max_users=50, max_monthly_runs=1,
            features=["audit_export"], message="license 有效")
        _seed_run(db, "t1", "QE", datetime(2026, 10, 5, tzinfo=timezone.utc))
        _seed_run(db, "t2", "QE", datetime(2026, 10, 6, tzinfo=timezone.utc))
        db.commit()
        summary = usage_service.monthly_usage_summary(db, month="2026-10")
        assert summary["license"]["max_monthly_runs"] == 1
        assert summary["license"]["runs_over_quota"] is True  # 2 runs > 1


# ==================== 超额告警（不阻断） ====================

class TestCheckRunOverage:
    def _grant(self, max_monthly_runs):
        license_service._state = LicenseState(
            status=STATUS_VALID, tier=TIER_ENTERPRISE, effective_tier=TIER_ENTERPRISE,
            customer="ACME", max_monthly_runs=max_monthly_runs, features=[],
            message="license 有效")

    def _fake_redis(self, monkeypatch, already_alerted=False):
        client = MagicMock()
        client.set.return_value = not already_alerted  # nx=True：False=已存在（已节流）
        monkeypatch.setattr(
            "app.services.redis_stop_signal.get_redis_client", lambda: client)
        return client

    def test_no_cap_means_no_overage(self, db, monkeypatch):
        self._grant(None)
        alerts = []
        monkeypatch.setattr("app.services.alert_service.send_alert",
                            lambda *a, **k: alerts.append(a) or True)
        assert usage_service.check_run_overage(db) is False
        assert alerts == []

    def test_under_cap_no_alert(self, db, monkeypatch):
        self._grant(10)
        _seed_run(db, "t1", "QE", datetime.now(timezone.utc))
        db.commit()
        alerts = []
        monkeypatch.setattr("app.services.alert_service.send_alert",
                            lambda *a, **k: alerts.append(a) or True)
        assert usage_service.check_run_overage(db) is False
        assert alerts == []

    def test_over_cap_alerts_without_raising(self, db, monkeypatch):
        self._grant(1)
        _seed_run(db, "t1", "QE", datetime.now(timezone.utc))
        _seed_run(db, "t2", "QE", datetime.now(timezone.utc))
        db.commit()
        self._fake_redis(monkeypatch)
        alerts = []
        monkeypatch.setattr("app.services.alert_service.send_alert",
                            lambda *a, **k: alerts.append(a) or True)
        assert usage_service.check_run_overage(db) is True
        assert len(alerts) == 1
        assert "超额" in alerts[0][1]

    def test_alert_failure_never_raises(self, db, monkeypatch):
        """告警通道全挂也不抛错（不阻断评测的硬约束）"""
        self._grant(0)  # 上限 0：任何运行都超额
        _seed_run(db, "t1", "QE", datetime.now(timezone.utc))
        db.commit()
        self._fake_redis(monkeypatch)
        monkeypatch.setattr("app.services.alert_service.send_alert",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert usage_service.check_run_overage(db) is True  # 吞掉异常继续返回超额态

    def test_daily_throttle(self, db, monkeypatch):
        """当天已告警 → 节流跳过（不再发告警）"""
        self._grant(1)
        _seed_run(db, "t1", "QE", datetime.now(timezone.utc))
        _seed_run(db, "t2", "QE", datetime.now(timezone.utc))
        db.commit()
        self._fake_redis(monkeypatch, already_alerted=True)
        alerts = []
        monkeypatch.setattr("app.services.alert_service.send_alert",
                            lambda *a, **k: alerts.append(a) or True)
        assert usage_service.check_run_overage(db) is True  # 仍超额（状态如实）
        assert alerts == []  # 但当天已告警过，不重复发

    def test_redis_down_still_alerts(self, db, monkeypatch):
        """Redis 不可用 → 不节流，宁可重复不漏报"""
        self._grant(1)
        _seed_run(db, "t1", "QE", datetime.now(timezone.utc))
        _seed_run(db, "t2", "QE", datetime.now(timezone.utc))
        db.commit()
        def _boom():
            raise ConnectionError("redis down")
        monkeypatch.setattr("app.services.redis_stop_signal.get_redis_client", _boom)
        alerts = []
        monkeypatch.setattr("app.services.alert_service.send_alert",
                            lambda *a, **k: alerts.append(a) or True)
        assert usage_service.check_run_overage(db) is True
        assert len(alerts) == 1


# ==================== 终态收割 ====================

class TestTerminalHarvest:
    def test_complete_record_persists_metered_usage(self, db):
        from app.services.evaluation_task_service import TaskExecutionRecordService

        record_usage("gpt-x", 10, 5, 15)
        record_usage_totals("cc-judge", 30)
        db.add(EvaluationTask(id="t1", name="t1", team_code="QE",
                              evaluation_type="ai_judge", status="running"))
        svc = TaskExecutionRecordService(db)
        svc.create_record("t1", 1)
        svc.complete_record("t1", 1, result={"ok": True})
        record = db.query(TaskExecutionRecord).one()
        assert record.model_usage == {
            "gpt-x": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "cc-judge": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 30},
        }

    def test_empty_tracker_leaves_null(self, db):
        from app.services.evaluation_task_service import TaskExecutionRecordService

        db.add(EvaluationTask(id="t1", name="t1", team_code="QE",
                              evaluation_type="ai_judge", status="running"))
        svc = TaskExecutionRecordService(db)
        svc.create_record("t1", 1)
        svc.complete_record("t1", 1, result={"ok": True})
        assert db.query(TaskExecutionRecord).one().model_usage is None
