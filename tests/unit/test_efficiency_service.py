"""提效统计服务单元测试"""
import pytest
from unittest.mock import MagicMock
from datetime import datetime

from app.services.efficiency_service import EfficiencyService
from app.models.task_models import TaskExecutionRecord, EvaluationTask
from app.models.stats_models import TaskExecutionStats

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestTotalEfficiency:
    """get_total_efficiency 测试"""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        return EfficiencyService(mock_db)

    def _make_record(self, record_id: str, task_id: str, exec_num: int, manual_hours, completed_at: str):
        r = MagicMock(spec=TaskExecutionRecord)
        r.id = record_id
        r.task_id = task_id
        r.execution_number = exec_num
        r.estimated_manual_hours = manual_hours
        r.completed_at = datetime.fromisoformat(completed_at)
        return r

    def _make_stats(self, task_id: str, duration_ms: int):
        s = MagicMock(spec=TaskExecutionStats)
        s.task_id = task_id
        s.status = "success"
        s.duration_ms = duration_ms
        return s

    def _make_task(self, task_id: str, name: str):
        t = MagicMock(spec=EvaluationTask)
        t.id = task_id
        t.name = name
        return t

    def _setup_db_queries(self, mock_db, records, stats_list, tasks):
        """Helper: configure mock_db.query to return 3 chained query mocks in sequence."""
        mq1, mq2, mq3 = MagicMock(), MagicMock(), MagicMock()
        mock_db.query.side_effect = [mq1, mq2, mq3]

        # query(TaskExecutionRecord).filter(...).all()
        mq1.filter.return_value = mq1
        mq1.all.return_value = records

        # query(TaskExecutionStats).filter(...).all()
        mq2.filter.return_value = mq2
        mq2.all.return_value = stats_list

        # query(EvaluationTask).filter(...).all()
        mq3.filter.return_value = mq3
        mq3.all.return_value = tasks

    def test_without_manual_hours(self, service, mock_db):
        """3条记录均无预计耗时：task_count=3, total_efficiency=0, 每条 efficiency=0"""
        records = [
            self._make_record("r1", "t1", 1, None, "2025-01-01"),
            self._make_record("r2", "t2", 1, None, "2025-01-02"),
            self._make_record("r3", "t3", 1, None, "2025-01-03"),
        ]
        stats_list = [
            self._make_stats("t1", 3600000),  # 1h
            self._make_stats("t2", 1800000),  # 0.5h
            self._make_stats("t3", 7200000),  # 2h
        ]
        tasks = [
            self._make_task("t1", "任务1"),
            self._make_task("t2", "任务2"),
            self._make_task("t3", "任务3"),
        ]
        self._setup_db_queries(mock_db, records, stats_list, tasks)

        result = service.get_total_efficiency()

        assert result["task_count"] == 3, "无预计耗时记录也应计入 task_count"
        assert result["total_efficiency_hours"] == 0.0, "无预计耗时不应累加总提效"
        assert len(result["items"]) == 3
        for item in result["items"]:
            assert item["estimated_manual_hours"] is None, "None 应保持原样返回"
            assert item["efficiency_hours"] == 0.0

    def test_mixed(self, service, mock_db):
        """3条记录中 2条有预计耗时、1条无：task_count=3, 仅含 hours 的记录贡献总提效"""
        records = [
            self._make_record("r1", "t1", 1, 2.0, "2025-01-01"),
            self._make_record("r2", "t2", 1, 3.0, "2025-01-02"),
            self._make_record("r3", "t3", 1, None, "2025-01-03"),
        ]
        stats_list = [
            self._make_stats("t1", 3600000),  # 1h → sys=1.0, eff=2-1=1.0
            self._make_stats("t2", 3600000),  # 1h → sys=1.0, eff=3-1=2.0
            self._make_stats("t3", 7200000),  # 2h → sys=2.0, 但 eff=0 不累加
        ]
        tasks = [
            self._make_task("t1", "任务1"),
            self._make_task("t2", "任务2"),
            self._make_task("t3", "任务3"),
        ]
        self._setup_db_queries(mock_db, records, stats_list, tasks)

        result = service.get_total_efficiency()

        assert result["task_count"] == 3
        assert result["total_efficiency_hours"] == 3.0  # 1.0 + 2.0, 不含无hours的记录
        assert result["avg_efficiency_hours"] == 1.0     # 3.0 / 3
        assert len(result["items"]) == 3

        items_by_id = {i["task_id"]: i for i in result["items"]}
        assert items_by_id["t1"]["efficiency_hours"] == 1.0
        assert items_by_id["t2"]["efficiency_hours"] == 2.0
        assert items_by_id["t3"]["efficiency_hours"] == 0.0
        assert items_by_id["t3"]["estimated_manual_hours"] is None

    def test_empty(self, service, mock_db):
        """日期范围内无记录：task_count=0, items=[]"""
        mq = MagicMock()
        mock_db.query.return_value = mq
        mq.filter.return_value = mq
        mq.all.return_value = []

        result = service.get_total_efficiency(
            start_date="2025-12-01", end_date="2025-12-31"
        )

        assert result["task_count"] == 0
        assert result["total_efficiency_hours"] == 0.0
        assert result["avg_efficiency_hours"] == 0.0
        assert result["items"] == []


class TestTaskEfficiency:
    """get_task_efficiency 测试"""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        return EfficiencyService(mock_db)

    def test_include_without_manual_hours(self, service, mock_db):
        """同一任务有 2 条执行记录，1 条无预计耗时：应返回 2 条，无耗时记录 efficiency=0"""
        records = [
            MagicMock(spec=TaskExecutionRecord, id="r1", task_id="t1",
                      execution_number=1, estimated_manual_hours=2.0,
                      completed_at=datetime(2025, 1, 1)),
            MagicMock(spec=TaskExecutionRecord, id="r2", task_id="t1",
                      execution_number=2, estimated_manual_hours=None,
                      completed_at=datetime(2025, 1, 2)),
        ]

        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.side_effect = [
            records,
            [],
        ]
        mock_query.first.side_effect = [
            MagicMock(spec=TaskExecutionStats, duration_ms=3600000, status="success"),
        ]

        result = service.get_task_efficiency("t1")

        assert len(result) == 2
        assert result[0]["efficiency_hours"] == 1.0    # 2.0 - 1.0
        assert result[1]["efficiency_hours"] == 0.0    # 0 - 1.0 → 0
        assert result[1]["estimated_manual_hours"] == 0  # None → 0
