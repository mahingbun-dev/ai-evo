"""W19b 权限缺口修复单测（对抗审查 C1/C2/C3）

- C1: POST /datasets/cleanup-duplicates 仅超管（跨团队破坏性写）
- C2: GET /evaluation/tasks/{id}/status 补对象级归属校验（W14 漏出 W9 门）
- C3: 会话导入新建数据集随调用者团队落库（不再误入 DEFAULT）
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fastapi import HTTPException

sys.path.insert(0, ".")

import app.api.dataset_api as dataset_api  # noqa: E402
import app.api.evaluation_task_api as evaluation_task_api  # noqa: E402
from app.services.session_dataset_service import import_sessions  # noqa: E402

pytestmark = pytest.mark.unit

SUPER = {"user_code": "root", "org_code": "ROOT", "super_admin_flag": 1}
OWNER = {"user_code": "u1", "org_code": "T1", "super_admin_flag": 0}
OTHER = {"user_code": "u2", "org_code": "T2", "super_admin_flag": 0}


def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


class TestCleanupDuplicatesGate:
    def test_non_super_403_and_db_untouched(self):
        db = MagicMock()
        with pytest.raises(HTTPException) as ei:
            dataset_api.cleanup_duplicates(_req(OWNER), db=db)
        assert ei.value.status_code == 403
        db.query.assert_not_called()

    def test_super_passes_gate_and_runs(self):
        db = MagicMock()
        # 无重复名 → 不进入删除分支，快速返回
        db.query.return_value.filter.return_value.group_by.return_value.having.return_value.all.return_value = []
        out = dataset_api.cleanup_duplicates(_req(SUPER), db=db)
        assert isinstance(out, dict)
        db.query.assert_called()

    def test_normal_user_cannot_even_with_own_team(self):
        db = MagicMock()
        with pytest.raises(HTTPException) as ei:
            dataset_api.cleanup_duplicates(_req(OWNER), db=db)
        assert ei.value.status_code == 403


class TestTaskStatusGate:
    @staticmethod
    def _row(team_code="T1", status="running"):
        return SimpleNamespace(
            team_code=team_code, status=status, progress=50,
            completed_questions=5, total_questions=10,
        )

    @staticmethod
    def _db(row):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = lambda: row
        return db

    def test_cross_team_403(self):
        with pytest.raises(HTTPException) as ei:
            evaluation_task_api.get_task_status("tid", _req(OWNER), db=self._db(self._row("T2")))
        assert ei.value.status_code == 403

    def test_missing_404(self):
        with pytest.raises(HTTPException) as ei:
            evaluation_task_api.get_task_status("ghost", _req(OWNER), db=self._db(None))
        assert ei.value.status_code == 404

    def test_same_team_ok(self):
        out = evaluation_task_api.get_task_status("tid", _req(OWNER), db=self._db(self._row("T1")))
        assert out.status == "running" and out.progress == 50

    def test_super_cross_team_ok(self):
        out = evaluation_task_api.get_task_status("tid", _req(SUPER), db=self._db(self._row("T2")))
        assert out.status == "running"


class TestSessionImportTeamCode:
    def test_new_dataset_stamps_caller_team(self):
        with patch("app.services.dataset_service.DatasetService") as ds_cls, \
             patch("app.services.dataset_service.DatasetDataService") as _ds:
            import_sessions(MagicMock(), entries=[], name="会话集", created_by="u1", team_code="T1")
        assert ds_cls.return_value.create_dataset.call_args.kwargs.get("team_code") == "T1"

    def test_default_none_falls_back_in_create(self):
        with patch("app.services.dataset_service.DatasetService") as ds_cls, \
             patch("app.services.dataset_service.DatasetDataService") as _ds:
            import_sessions(MagicMock(), entries=[], name="会话集")
        assert ds_cls.return_value.create_dataset.call_args.kwargs.get("team_code") is None

    @pytest.mark.asyncio
    async def test_api_confirm_passes_user_team(self):
        """confirm 端点把调用者团队传进 import_sessions"""
        with patch("app.services.session_dataset_service.import_sessions",
                   return_value={"dataset_id": "d1", "count": 0}) as pm, \
             patch.object(dataset_api, "_read_session_uploads", return_value=[]):
            await dataset_api.confirm_session_import(
                name="x", description="", real_dataset_id="",
                files=[MagicMock()], request=_req(OWNER), db=MagicMock())
        assert pm.call_args.kwargs.get("team_code") == "T1"
