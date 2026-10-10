"""任务配置校验（契约 v3 §五）单元测试：待评测数据必填、@ 引用标记校验、API 400 转换

覆盖：
- create/update：judge/agent/（consistency 且 use_judge）必须 evaluation_targets 非空 →
  ValueError（API 层转 400）
- 标记校验：score_details、metric.description（维度定义）与 metric.score_criteria[].desc
  中的 {{ref:...}}，target 引用须存在于本次 evaluation_targets（按 id 或显示名）、
  var 引用须存在于 judge_variable_config（key/name）；失效报出标记原文与所在字段
- script 行校验的 API 400 路径（脚本不存在/输出键越界等详细用例见
  test_evaluation_pre_scripts_flow.py 第 9 节）
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.evaluation_task_service import EvaluationTaskService


def _service_with_db(script_rows=None, field_metas=None, items=None):
    """构造 EvaluationTaskService：mock DB 脚本查询（.all() 单次全量）、数据集字段元数据与数据项"""
    db = MagicMock()

    def _query_side_effect(model):
        model_name = getattr(model, "__name__", str(model))
        q = MagicMock()
        if model_name == "PreJudgeScript":
            q.all.return_value = script_rows or []
        else:
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query_side_effect
    service = EvaluationTaskService(db)

    patchers = [
        patch("app.services.dataset_service.DatasetDataService.get_data_list_v2",
              return_value=items or []),
        patch("app.services.dataset_field_service.DatasetFieldService.get_field_metadata",
              return_value=field_metas or []),
    ]
    for p in patchers:
        p.start()
    return service, db, patchers


def _stop_patchers(patchers):
    for p in patchers:
        p.stop()


def _model_target():
    """最小可用的待评测数据行（模型输出）"""
    return {"id": "t_model", "name": "model_output", "source": "model_output",
            "source_config": {}, "display_name": "模型输出", "required": True}


def _metric(desc=None, name="维度一", description=None):
    """构造评测维度（score_criteria 单等级，desc 可带引用标记；description 为维度定义）"""
    return {
        "id": "metric_1", "name": name, "weight": "中", "max_score": 10,
        "description": description if description is not None else "维度定义（无标记）",
        "score_criteria": {"high": {"min": 8, "max": 10, "desc": desc or "优秀"}},
    }


# ---------------- 一、targets 必填校验 ----------------

class TestTargetsRequired:

    def test_judge_without_targets_rejected(self):
        """judge 类型未配置 targets → ValueError（API 层转 400）"""
        service, db, patchers = _service_with_db()
        try:
            with pytest.raises(ValueError, match="待评测数据"):
                service.create_task(name="t", evaluation_type="judge")
        finally:
            _stop_patchers(patchers)

    def test_agent_without_targets_rejected(self):
        """agent 类型未配置 targets → ValueError"""
        service, db, patchers = _service_with_db()
        try:
            with pytest.raises(ValueError, match="待评测数据"):
                service.create_task(name="t", evaluation_type="agent")
        finally:
            _stop_patchers(patchers)

    def test_consistency_with_judge_without_targets_rejected(self):
        """consistency 且 use_judge=True 未配置 targets → ValueError"""
        service, db, patchers = _service_with_db()
        try:
            with pytest.raises(ValueError, match="待评测数据"):
                service.create_task(
                    name="t", evaluation_type="consistency",
                    consistency_config={"use_judge": True},
                )
        finally:
            _stop_patchers(patchers)

    def test_consistency_without_judge_passes(self):
        """consistency 未启用裁判 → targets 可为空，创建成功"""
        service, db, patchers = _service_with_db()
        try:
            service.create_task(
                name="t", evaluation_type="consistency",
                consistency_config={"use_judge": False},
            )
            assert db.add.called
        finally:
            _stop_patchers(patchers)

    def test_success_rate_without_targets_passes(self):
        """success_rate 类型不受 targets 必填约束"""
        service, db, patchers = _service_with_db()
        try:
            service.create_task(name="t", evaluation_type="success_rate")
            assert db.add.called
        finally:
            _stop_patchers(patchers)

    def test_judge_with_targets_passes(self):
        """judge 类型配置 targets → 创建成功"""
        service, db, patchers = _service_with_db()
        try:
            service.create_task(
                name="t", evaluation_type="judge",
                evaluation_targets=[_model_target()],
            )
            assert db.add.called
        finally:
            _stop_patchers(patchers)

    def test_update_legacy_judge_task_without_targets_rejected(self):
        """update 合并最终态校验：存量 judge 任务无 targets，仅改名字也拒绝"""
        service, db, patchers = _service_with_db()
        task = SimpleNamespace(
            id="task_1", evaluation_type="judge", dataset_id=None,
            evaluation_targets=None, consistency_config=None,
            score_details=None, evaluation_metrics=None, judge_variable_config=None,
        )
        service.get_task = MagicMock(return_value=task)
        try:
            with pytest.raises(ValueError, match="待评测数据"):
                service.update_task("task_1", name="新名字")
        finally:
            _stop_patchers(patchers)


# ---------------- 二、@ 引用标记校验 ----------------

class TestReferenceMarkValidation:

    def test_stale_target_mark_in_score_details_rejected(self):
        """score_details 中 target 标记引用不存在的待评测数据 → 报标记原文与字段"""
        service, db, patchers = _service_with_db()
        try:
            with pytest.raises(ValueError) as excinfo:
                service.create_task(
                    name="t", evaluation_type="judge",
                    evaluation_targets=[_model_target()],
                    score_details="参考 {{ref:target:t_ghost|幽灵数据}} 评分",
                )
            message = str(excinfo.value)
            assert "{{ref:target:t_ghost|幽灵数据}}" in message
            assert "score_details" in message
        finally:
            _stop_patchers(patchers)

    def test_valid_target_mark_by_id_and_display_name(self):
        """target 标记按 id 命中、按显示名兜底 → 校验通过"""
        service, db, patchers = _service_with_db()
        try:
            service.create_task(
                name="t", evaluation_type="judge",
                evaluation_targets=[_model_target()],
                score_details="按 id：{{ref:target:t_model|模型输出}}；按显示名：{{ref:target:whatever|模型输出}}",
                evaluation_metrics=[_metric(desc="维度内 {{ref:target:t_model|模型输出}}")],
            )
            assert db.add.called
        finally:
            _stop_patchers(patchers)

    def test_stale_var_mark_in_metric_desc_rejected_with_location(self):
        """维度等级 desc 中 var 标记失效 → 报标记原文与 evaluation_metrics[i].score_criteria 定位"""
        service, db, patchers = _service_with_db()
        try:
            with pytest.raises(ValueError) as excinfo:
                service.create_task(
                    name="t", evaluation_type="judge",
                    evaluation_targets=[_model_target()],
                    judge_variable_config=[{"key": "allure_rate", "name": "一致率",
                                            "source": "script_output", "source_config": {}}],
                    evaluation_metrics=[_metric(desc="参考 {{ref:var:missing_var|缺失变量}}")],
                )
            message = str(excinfo.value)
            assert "{{ref:var:missing_var|缺失变量}}" in message
            assert "evaluation_metrics[0]" in message
            assert "score_criteria" in message
        finally:
            _stop_patchers(patchers)

    def test_valid_var_marks_list_and_dict_config(self):
        """var 标记按 key 命中（list/dict 两种 judge_variable_config）与按变量名兜底 → 通过"""
        service, db, patchers = _service_with_db()
        try:
            service.create_task(
                name="t", evaluation_type="judge",
                evaluation_targets=[_model_target()],
                judge_variable_config=[{"key": "allure_rate", "name": "一致率",
                                        "source": "script_output", "source_config": {}}],
                score_details="按 key：{{ref:var:allure_rate|一致率}}；按名称：{{ref:var:xxx|一致率}}",
                evaluation_metrics=[_metric(desc="{{ref:var:allure_rate|一致率}}")],
            )
            assert db.add.called
        finally:
            _stop_patchers(patchers)

        service2, db2, patchers2 = _service_with_db()
        try:
            service2.create_task(
                name="t2", evaluation_type="judge",
                evaluation_targets=[_model_target()],
                judge_variable_config={"user_input": {"name": "用户输入", "source": "dataset_field"}},
                score_details="dict 配置按 key：{{ref:var:user_input|用户输入}}；按名称：{{ref:var:zzz|用户输入}}",
            )
            assert db2.add.called
        finally:
            _stop_patchers(patchers2)

    def test_var_mark_without_judge_variable_config_rejected(self):
        """未配置裁判变量时出现 var 标记 → 失效报错"""
        service, db, patchers = _service_with_db()
        try:
            with pytest.raises(ValueError, match="ref:var"):
                service.create_task(
                    name="t", evaluation_type="judge",
                    evaluation_targets=[_model_target()],
                    score_details="{{ref:var:any_key|任意变量}}",
                )
        finally:
            _stop_patchers(patchers)

    def test_stale_target_mark_in_metric_description_rejected(self):
        """维度定义 description 中的失效 target 标记 → 报错并定位到 description 字段"""
        service, db, patchers = _service_with_db()
        metric = _metric(description="维度定义里出现 {{ref:target:t_ghost|幽灵}} 须校验")
        try:
            with pytest.raises(ValueError, match=r"evaluation_metrics\[0\]\.description.*t_ghost"):
                service.create_task(
                    name="t", evaluation_type="judge",
                    evaluation_targets=[_model_target()],
                    evaluation_metrics=[metric],
                )
        finally:
            _stop_patchers(patchers)

    def test_valid_target_mark_in_metric_description_passes(self):
        """维度定义 description 中的标记引用存在的 targets（按 id / 显示名）→ 通过"""
        service, db, patchers = _service_with_db()
        try:
            service.create_task(
                name="t", evaluation_type="judge",
                evaluation_targets=[_model_target()],
                evaluation_metrics=[
                    _metric(description="对照 {{ref:target:t_model|模型输出}} 评分"),
                    _metric(name="维度二", description="对照 {{ref:target:模型输出|兜底名}} 评分"),
                ],
            )
            assert db.add.called
        finally:
            _stop_patchers(patchers)

    def test_update_validates_marks_with_merged_state(self):
        """update 用任务既有 score_details + 新 targets 合并校验标记"""
        service, db, patchers = _service_with_db()
        task = SimpleNamespace(
            id="task_1", evaluation_type="judge", dataset_id=None,
            evaluation_targets=[_model_target()],
            consistency_config=None,
            score_details="存量标准引用 {{ref:target:t_model|模型输出}}",
            evaluation_metrics=None, judge_variable_config=None,
        )
        service.get_task = MagicMock(return_value=task)
        try:
            # 更新 targets 后，既有 score_details 的标记改引新行 id → 通过
            service.update_task("task_1", evaluation_targets=[
                {**_model_target(), "id": "t_new"},
            ])
            assert db.commit.called
        finally:
            _stop_patchers(patchers)


# ---------------- 三、API 400 转换 ----------------

class TestApiErrorTranslation:

    def test_create_task_endpoint_translates_value_error_to_400(self):
        """create 端点：targets 为空 → HTTPException 400"""
        from app.api.evaluation_task_api import create_task as create_task_endpoint
        from app.api.evaluation_task_api import CreateTaskRequest

        request = MagicMock()
        request.state.user = {"user_code": "u1", "org_code": "ORG"}
        request.client.host = "127.0.0.1"
        body = CreateTaskRequest(name="t", evaluation_type="judge")

        with pytest.raises(HTTPException) as excinfo:
            create_task_endpoint(body, request, MagicMock())

        assert excinfo.value.status_code == 400
        assert "待评测数据" in excinfo.value.detail

    def test_update_task_endpoint_translates_value_error_to_400(self):
        """update 端点：失效标记 → HTTPException 400（沿用既有 ValueError→400 风格）"""
        from app.api.evaluation_task_api import update_task as update_task_endpoint
        from app.api.evaluation_task_api import UpdateTaskRequest

        with patch("app.api.evaluation_task_api.EvaluationTaskService") as mock_cls:
            mock_service = MagicMock()
            mock_cls.return_value = mock_service
            task = MagicMock()
            task.dataset_id = None
            mock_service.get_task.return_value = task
            mock_service.update_task.side_effect = ValueError(
                "score_details 中的引用标记失效: {{ref:target:t_ghost|幽灵数据}}"
            )

            body = UpdateTaskRequest(score_details="标准 {{ref:target:t_ghost|幽灵数据}}")
            with pytest.raises(HTTPException) as excinfo:
                update_task_endpoint("task_1", body, MagicMock())

            assert excinfo.value.status_code == 400
            assert "引用标记失效" in excinfo.value.detail

    def test_migrate_config_endpoint_translates_value_error_to_400(self):
        """migrate-config 端点：迁移抛 ValueError → HTTPException 400；任务不存在 → 404"""
        from app.api.evaluation_task_api import migrate_task_config

        with patch("app.api.evaluation_task_api.EvaluationTaskService") as mock_cls:
            mock_service = MagicMock()
            mock_cls.return_value = mock_service
            mock_service.get_task.return_value = None
            with pytest.raises(HTTPException) as excinfo:
                migrate_task_config("task_missing", MagicMock())
            assert excinfo.value.status_code == 404

        with patch("app.api.evaluation_task_api.EvaluationTaskService") as mock_cls, \
                patch("app.services.config_migration_service.ConfigMigrationService") as mock_mig:
            mock_service = MagicMock()
            mock_cls.return_value = mock_service
            mock_service.get_task.return_value = MagicMock()
            mock_mig.return_value.migrate_task.side_effect = ValueError("迁移失败")

            with pytest.raises(HTTPException) as excinfo:
                migrate_task_config("task_1", MagicMock())
            assert excinfo.value.status_code == 400
            assert "迁移失败" in excinfo.value.detail

    def test_migrate_legacy_all_endpoint_team_scoped(self):
        """migrate-legacy-all 端点：普通用户按团队过滤，超管全量"""
        from app.api.evaluation_task_api import migrate_legacy_all_tasks

        with patch("app.services.config_migration_service.ConfigMigrationService") as mock_mig:
            mock_mig.return_value.migrate_all_tasks.return_value = {
                "total": 3, "migrated": 2, "skipped": 1, "errors": [],
            }
            request = MagicMock()
            request.state.user = {"user_code": "u1", "org_code": "TEAM_A", "super_admin_flag": 0}

            result = migrate_legacy_all_tasks(request, MagicMock())
            assert result["migrated"] == 2
            mock_mig.return_value.migrate_all_tasks.assert_called_once_with(team_code="TEAM_A")

        with patch("app.services.config_migration_service.ConfigMigrationService") as mock_mig:
            mock_mig.return_value.migrate_all_tasks.return_value = {
                "total": 5, "migrated": 4, "skipped": 1, "errors": [],
            }
            request = MagicMock()
            request.state.user = {"user_code": "admin", "org_code": "TEAM_A", "super_admin_flag": 1}

            result = migrate_legacy_all_tasks(request, MagicMock())
            mock_mig.return_value.migrate_all_tasks.assert_called_once_with()

    def test_instruction_migrate_legacy_all_endpoint(self):
        """instruction migrate-legacy-all 端点：返回批量迁移摘要"""
        from app.api.instruction_config_api import migrate_legacy_templates

        with patch("app.services.config_migration_service.ConfigMigrationService") as mock_mig:
            mock_mig.return_value.migrate_all_templates.return_value = {
                "total": 2, "migrated": 1, "skipped": 1, "errors": [],
            }
            result = migrate_legacy_templates(MagicMock())
            assert result == {"total": 2, "migrated": 1, "skipped": 1, "errors": []}

    def test_create_template_accepts_null_steps(self):
        """创建模板端点：steps 显式传 null 不再 422，且模板文本不渲染出 'None'（验收报告 issues#1 回归锁定）"""
        from app.api.instruction_config_api import (
            CreateInstructionConfigRequest,
            create_instruction_config,
        )

        # 前端 confirmSaveTemplate 在 v3 保存链路会显式携带 steps: null
        body = CreateInstructionConfigRequest(name="t", scene="s", steps=None)
        assert body.steps is None

        captured = []
        db = MagicMock()
        db.add.side_effect = lambda obj: captured.append(obj)
        create_instruction_config(body, db)

        assert db.commit.called
        assert captured and captured[0].steps is None
        assert "None" not in (captured[0].instruction_template or "")
