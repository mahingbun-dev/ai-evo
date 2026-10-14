"""
W14 WebSocket 归属校验扩展单元测试（hub 导入 job 通道）

背景：前端 useTaskWebSocket 以 job_id（hubjob_ 前缀）连 /ws/task/{id} 订阅
hub_import_progress 进度推送；端点归属校验 _check_task_ownership 原先只认
EvaluationTask 表，hub job 会被 4404 拒掉。W14 扩展为：
- 评测任务命中 → 原团队/超管口径不变
- 评测任务查无 → 回落 HubImportJob：创建人本人或超管放行（4403/4404 语义不变）

手法：mock SessionLocal 的 session.get（按模型类分派返回值）+ AsyncMock WebSocket，
直接调用 _check_task_ownership（参照 test_task_status_api.py 的 mock 风格）。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from fastapi import WebSocket

from app.api.task_status_api import _check_task_ownership
from app.models.hub_file_models import HubImportJob
from app.models.task_models import EvaluationTask
from app.services.ws_auth import WS_CLOSE_FORBIDDEN as CLOSE_FORBIDDEN
from app.services.ws_auth import WS_CLOSE_NOT_FOUND as CLOSE_NOT_FOUND


def _ws():
    ws = AsyncMock(spec=WebSocket)
    return ws


def _patch_session(get_by_model):
    """patch task_status_api.SessionLocal：with SessionLocal() as db →

    - db.get(model, id) 按 get_by_model 分派（_get_hub_import_job 通道）；
    - db.query(EvaluationTask).filter(...).first() 同样按 get_by_model 分派
      （W9 收口后评测任务改走 query 链，team_code 交 check_team_access 判定）
    """
    session = MagicMock()
    session.get.side_effect = lambda model, task_id: get_by_model.get(model)
    session.query.return_value.filter.return_value.first.side_effect = (
        lambda: get_by_model.get(EvaluationTask)
    )
    ctx = MagicMock()
    ctx.__enter__.return_value = session
    ctx.__exit__.return_value = False
    return patch("app.api.task_status_api.SessionLocal", return_value=ctx)


def _hub_job(created_by="u1"):
    job = MagicMock(spec=HubImportJob)
    job.id = "hubjob_abc123"
    job.created_by = created_by
    return job


def _task(team_code="T1"):
    task = MagicMock(spec=EvaluationTask)
    task.team_code = team_code
    return task


CREATOR = {"user_code": "u1", "org_code": "T1", "super_admin_flag": 0}
OTHER = {"user_code": "u2", "org_code": "T2", "super_admin_flag": 0}
SUPER = {"user_code": "root", "org_code": "ROOT", "super_admin_flag": 1}


class TestHubJobOwnership:
    """_check_task_ownership 的 hub 导入 job 通道"""

    @pytest.mark.asyncio
    async def test_creator_allowed_no_close(self):
        ws = _ws()
        with _patch_session({EvaluationTask: None, HubImportJob: _hub_job("u1")}):
            assert await _check_task_ownership("hubjob_abc123", CREATOR, ws) is True
        ws.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_super_admin_allowed(self):
        ws = _ws()
        with _patch_session({EvaluationTask: None, HubImportJob: _hub_job("someone-else")}):
            assert await _check_task_ownership("hubjob_abc123", SUPER, ws) is True
        ws.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_user_rejected_4403(self):
        ws = _ws()
        with _patch_session({EvaluationTask: None, HubImportJob: _hub_job("u1")}):
            assert await _check_task_ownership("hubjob_abc123", OTHER, ws) is False
        ws.close.assert_awaited_once_with(code=CLOSE_FORBIDDEN)

    @pytest.mark.asyncio
    async def test_anonymous_created_job_rejected_for_normal_user(self):
        """API 直建 job（created_by=None）：普通用户不可订阅，仅超管放行"""
        ws = _ws()
        with _patch_session({EvaluationTask: None, HubImportJob: _hub_job(None)}):
            assert await _check_task_ownership("hubjob_x", CREATOR, ws) is False
        ws.close.assert_awaited_once_with(code=CLOSE_FORBIDDEN)

    @pytest.mark.asyncio
    async def test_both_tables_miss_4404(self):
        """评测任务与 hub job 均查无 → 4404（原语义不变）"""
        ws = _ws()
        with _patch_session({EvaluationTask: None, HubImportJob: None}):
            assert await _check_task_ownership("nope", CREATOR, ws) is False
        ws.close.assert_awaited_once_with(code=CLOSE_NOT_FOUND)


class TestTaskOwnershipUnchanged:
    """评测任务原归属口径回归：hub 扩展不得改变既有行为"""

    @pytest.mark.asyncio
    async def test_same_team_allowed(self):
        ws = _ws()
        with _patch_session({EvaluationTask: _task("T1"), HubImportJob: None}):
            assert await _check_task_ownership("task-1", CREATOR, ws) is True
        ws.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_team_rejected_4403(self):
        ws = _ws()
        with _patch_session({EvaluationTask: _task("T9"), HubImportJob: None}):
            assert await _check_task_ownership("task-1", CREATOR, ws) is False
        ws.close.assert_awaited_once_with(code=CLOSE_FORBIDDEN)

    @pytest.mark.asyncio
    async def test_super_admin_sees_all_teams(self):
        ws = _ws()
        with _patch_session({EvaluationTask: _task("T9"), HubImportJob: None}):
            assert await _check_task_ownership("task-1", SUPER, ws) is True
        ws.close.assert_not_called()
