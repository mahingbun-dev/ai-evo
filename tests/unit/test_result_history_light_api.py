"""
结果展示页性能优化：历史结果接口轻量模式 + 单版本全量端点单测

文件: tests/unit/test_result_history_light_api.py
背景: 结果展示页选任务时 GET /evaluation/results/{task_id}/history 一次性
传输所有执行版本的完整 result（每版含全部明细/模型输出/裁判 reasoning），
而前端仅渲染其中一版——结果页加载随执行次数线性变慢。本次改动：
- /history 增加 include_result=false 轻量模式（服务层摘要查询只 SELECT
  标量列，不触碰 result JSONB 大列）
- 新增 /history/{execution_number} 单版全量端点（含旧任务无执行记录时
  从任务本体兜底的兼容口径，与 history 兼容记录同构）

按仓库惯例直调端点函数（tests/unit/w9_direct_call.py 模块注释），
归属校验由 mock db 查询链返回预置任务承担。
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app.api.evaluation_task_api import (
    get_dimension_trend,
    get_result_history,
    get_result_history_version,
)
from tests.unit.w9_direct_call import fake_request

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _mock_task(**kwargs):
    task = MagicMock()
    task.id = "task_x"
    task.status = kwargs.get("status", "completed")
    task.result = kwargs.get("result", None)
    task.execution_count = kwargs.get("execution_count", 1)
    task.total_cost = kwargs.get("total_cost", 1.5)
    task.created_at = None
    task.completed_at = None
    return task


def _mock_record(execution_number=2, status="completed", result=None):
    record = MagicMock()
    record.execution_number = execution_number
    record.status = status
    record.result = result if result is not None else {"items": [1], "total": 1}
    record.total_cost = 0.8
    record.error_message = None
    record.started_at = None
    record.completed_at = None
    return record


def _summary(execution_number=2):
    return {
        "execution_number": execution_number,
        "status": "completed",
        "result": None,
        "total_cost": 0.8,
        "error_message": None,
        "started_at": None,
        "completed_at": None,
    }


def _record_service(summaries, records=None, by_number=None):
    svc = MagicMock()
    svc.get_record_summaries.return_value = summaries
    svc.get_records.return_value = records if records is not None else []
    svc.get_record_by_number.return_value = by_number
    return svc


def _db_with_task(task):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = task
    return db


class TestResultHistoryLightMode:
    def test_light_mode_returns_summaries_without_result(self):
        """include_result=false：返回版本摘要（result=None），不触发全量记录查询"""
        summaries = [_summary(3), _summary(2)]
        svc = _record_service(summaries)
        task = _mock_task()
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            res = get_result_history(
                "task_x", fake_request(), limit=50, include_result=False, db=_db_with_task(task)
            )

        assert res == summaries
        assert all(item["result"] is None for item in res)
        svc.get_records.assert_not_called()

    def test_default_keeps_full_result_contract(self):
        """默认 include_result=true：旧契约不变，逐版返回完整 result"""
        records = [_mock_record(2)]
        svc = _record_service([_summary(2)], records=records)
        task = _mock_task()
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            res = get_result_history("task_x", fake_request(), limit=50, db=_db_with_task(task))

        assert len(res) == 1
        assert res[0]["result"] == {"items": [1], "total": 1}
        svc.get_records.assert_called_once_with("task_x", limit=50)

    def test_compat_record_light_mode_hides_result(self):
        """旧任务无执行记录的兼容路径：轻量模式下兼容记录也不携带 result"""
        task = _mock_task(result={"total": 11}, execution_count=3)
        svc = _record_service([])
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            res = get_result_history(
                "task_x", fake_request(), limit=50, include_result=False, db=_db_with_task(task)
            )

        assert len(res) == 1
        assert res[0]["execution_number"] == 3
        assert res[0]["result"] is None

    def test_compat_record_default_carries_result(self):
        """旧任务无执行记录的兼容路径：默认模式兼容记录携带任务本体结果"""
        task = _mock_task(result={"total": 11}, execution_count=3)
        svc = _record_service([])
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            res = get_result_history("task_x", fake_request(), limit=50, db=_db_with_task(task))

        assert res[0]["result"] == {"total": 11}


class TestResultHistoryVersion:
    def test_returns_full_record_by_number(self):
        """按执行序号返回单版完整结果"""
        record = _mock_record(2)
        svc = _record_service([], by_number=record)
        task = _mock_task()
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            res = get_result_history_version("task_x", 2, fake_request(), db=_db_with_task(task))

        assert res["execution_number"] == 2
        assert res["result"] == {"items": [1], "total": 1}
        svc.get_record_by_number.assert_called_once_with("task_x", 2)

    def test_404_when_no_record_and_no_task_result(self):
        """无执行记录且任务无结果：404"""
        svc = _record_service([], by_number=None)
        task = _mock_task(result=None)
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            with pytest.raises(HTTPException) as excinfo:
                get_result_history_version("task_x", 9, fake_request(), db=_db_with_task(task))

        assert excinfo.value.status_code == 404

    def test_compat_from_task_body_when_record_missing(self):
        """旧任务兜底：无执行记录但任务有结果时，按任务本体的 execution_count 匹配版本"""
        task = _mock_task(result={"total": 11}, execution_count=3)
        svc = _record_service([], by_number=None)
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            res = get_result_history_version("task_x", 3, fake_request(), db=_db_with_task(task))

        assert res["execution_number"] == 3
        assert res["result"] == {"total": 11}

    def test_404_when_version_mismatch_compat_path(self):
        """旧任务兜底：版本号与任务本体 execution_count 不匹配 → 404"""
        task = _mock_task(result={"total": 11}, execution_count=3)
        svc = _record_service([], by_number=None)
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            with pytest.raises(HTTPException) as excinfo:
                get_result_history_version("task_x", 2, fake_request(), db=_db_with_task(task))

        assert excinfo.value.status_code == 404

    def test_access_denied_passthrough(self):
        """归属校验失败（403）不被端点吞掉"""
        with patch(
            "app.api.evaluation_task_api.ensure_task_access",
            side_effect=HTTPException(status_code=403, detail="无权访问"),
        ):
            with pytest.raises(HTTPException) as excinfo:
                get_result_history_version("task_x", 2, fake_request(), db=MagicMock())

        assert excinfo.value.status_code == 403


class TestDimensionTrend:
    """GET /evaluation/results/{task_id}/dimension-trend 端点直调（W9 直调惯例）

    覆盖四条行为：service 趋势透传与 limit 转发、旧任务兼容条目、
    无 dimension_scores 不出兼容条目、归属校验 403 直抛。
    """

    def test_passes_limit_and_returns_service_trend(self):
        """service 趋势列表原样透传，limit 转发给 service"""
        trend = [{
            "execution_number": 3,
            "status": "completed",
            "completed_at": None,
            "average_score": 8.1,
            "dimension_scores": {"准确性": {"average": 8.1, "weight": 2}},
        }]
        svc = MagicMock()
        svc.get_dimension_trend.return_value = trend
        task = _mock_task()
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            res = get_dimension_trend("task_x", fake_request(), limit=7, db=_db_with_task(task))

        assert res == trend
        svc.get_dimension_trend.assert_called_once_with("task_x", limit=7)

    def test_compat_entry_when_no_records(self):
        """旧任务无执行记录但任务本体结果含 dimension_scores → 单条兼容条目

        execution_number 用 task.execution_count or 1（与 get_result_history 兼容口径一致）
        """
        task = _mock_task(
            result={"average_score": 9.0, "dimension_scores": {"质量": {"average": 9.0, "weight": 1}}},
            execution_count=3,
        )
        svc = MagicMock()
        svc.get_dimension_trend.return_value = []
        with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
            res = get_dimension_trend("task_x", fake_request(), limit=50, db=_db_with_task(task))

        assert len(res) == 1
        assert res[0] == {
            "execution_number": 3,
            "status": "completed",
            "completed_at": None,
            "average_score": 9.0,
            "dimension_scores": {"质量": {"average": 9.0, "weight": 1}},
        }

    def test_no_compat_without_dimension_scores(self):
        """无执行记录且任务结果不含 dimension_scores（或 result 非 dict）→ 空列表"""
        for result in ({"total": 1}, None):
            task = _mock_task(result=result)
            svc = MagicMock()
            svc.get_dimension_trend.return_value = []
            with patch("app.services.evaluation_task_service.TaskExecutionRecordService", return_value=svc):
                res = get_dimension_trend("task_x", fake_request(), limit=50, db=_db_with_task(task))
            assert res == [], f"result={result!r} 不应出兼容条目"

    def test_access_denied_passthrough(self):
        """归属校验失败（403）不被端点吞掉"""
        with patch(
            "app.api.evaluation_task_api.ensure_task_access",
            side_effect=HTTPException(status_code=403, detail="无权访问"),
        ):
            with pytest.raises(HTTPException) as excinfo:
                get_dimension_trend("task_x", fake_request(), limit=50, db=MagicMock())

        assert excinfo.value.status_code == 403
