"""
W14 任务轻量状态端点单元测试

覆盖 GET /evaluation/tasks/{task_id}/status（轮询专用轻端点）：
- 只返回 status/progress/completed_questions/total 四字段（progress/计数 None 归零）
- 任务不存在 → 404
- 「轻」的实质：SQL 只 SELECT id/status/progress/completed_questions/total
  加 team_code（W19b 归属校验列）共六个标量列，不触碰 result/config 等
  大 JSON 列（源码断言 + 查询列断言双保险）

说明：按仓库既有 API 测试模式（参照 test_hub_import_api.py / test_task_status_api.py）
直接调用端点函数，不经过 TestClient——规避 nest_asyncio 全局补丁对 anyio portal
的跨文件污染。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from fastapi import HTTPException

from app.api.evaluation_task_api import TaskStatusResponse, get_task_status
from app.models.task_models import EvaluationTask


# W19b：端点要求 request（归属校验用）；本团队普通用户即可走通主流程
def _req():
    return SimpleNamespace(state=SimpleNamespace(
        user={"user_code": "u1", "org_code": "T1", "super_admin_flag": 0}))


def _make_db(row):
    """构造 get_task_status 所需的最小 db mock：db.query(*cols).filter(...).first() → row"""
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.first.return_value = row
    return db


def _make_row(**overrides):
    row = MagicMock()
    row.id = overrides.get("id", "task-1")
    row.status = overrides.get("status", "running")
    row.progress = overrides.get("progress", 40)
    row.completed_questions = overrides.get("completed_questions", 4)
    row.total_questions = overrides.get("total", 10)
    row.team_code = overrides.get("team_code", "T1")
    return row


class TestTaskStatusEndpoint:
    """GET /{task_id}/status 轻端点"""

    def test_returns_four_hot_fields(self):
        db = _make_db(_make_row())
        resp = get_task_status("task-1", _req(), db)
        assert isinstance(resp, TaskStatusResponse)
        assert resp.status == "running"
        assert resp.progress == 40
        assert resp.completed_questions == 4
        assert resp.total == 10
        # 四字段契约：响应里没有其它业务字段
        assert set(resp.model_dump().keys()) == {"status", "progress", "completed_questions", "total"}

    def test_none_scalars_coerced_to_zero(self):
        db = _make_db(_make_row(progress=None, completed_questions=None, total=None))
        resp = get_task_status("task-1", _req(), db)
        assert resp.progress == 0
        assert resp.completed_questions == 0
        assert resp.total == 0

    def test_not_found_raises_404(self):
        db = _make_db(None)
        with pytest.raises(HTTPException) as exc:
            get_task_status("missing", _req(), db)
        assert exc.value.status_code == 404
        assert "任务不存在" in exc.value.detail

    def test_cross_team_403(self):
        """W19b：跨团队探测被拒（存在性 + 进度都不泄漏）"""
        db = _make_db(_make_row(team_code="T2"))
        with pytest.raises(HTTPException) as exc:
            get_task_status("task-1", _req(), db)
        assert exc.value.status_code == 403

    def test_query_only_selects_scalar_columns(self):
        """轻端点的「轻」：查询列限定标量列（含 W19b 归属列 team_code），不含大 JSON 列"""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = _make_row()
        get_task_status("task-1", _req(), db)
        queried_cols = db.query.call_args[0]
        assert set(queried_cols) == {
            EvaluationTask.id,
            EvaluationTask.status,
            EvaluationTask.progress,
            EvaluationTask.completed_questions,
            EvaluationTask.total_questions,
            EvaluationTask.team_code,
        }
        # 大 JSON 列不允许进入查询
        for heavy in ("result", "model_config_json", "evaluation_metrics", "runtime_variables"):
            assert not any(getattr(col, "key", "") == heavy for col in queried_cols)

    def test_source_has_no_full_row_load(self):
        """源码锚点：端点实现是标量列 SELECT，而非复用全量 detail 的 service.get_task"""
        import inspect

        from app.api import evaluation_task_api

        src = inspect.getsource(evaluation_task_api.get_task_status)
        assert "db.query(" in src
        assert "EvaluationTask.total_questions" in src
        assert "service.get_task" not in src
