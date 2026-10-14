"""旧版配置迁移服务（ConfigMigrationService）单元测试

覆盖契约 v3 §六：
- 规则1：steps 非空 → 合并进 score_details（追加「评分步骤」编号块）后置 None；
  任务 steps 为 JSON 数组、模板 steps 为 Text（按 \n 拆）两种形态
- 规则2：pre_scripts 非空 → 逐条转为 evaluation_targets 的 script_output 行
  （id=target_mig_{i}_{task.id[:8]}、display_name=output_var 或脚本名、
  output_key=output_schema 首键或 None、required=False），pre_scripts 置 None
- 幂等：已迁移任务/模板重复迁移为 no-op（migrated=False、字段不变）
- 批量：migrate_all_tasks / migrate_all_templates 返回 {total, migrated, skipped, errors}
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.task_models import EvaluationTask
from app.models.template_models import EvaluationTemplate
from app.services.config_migration_service import ConfigMigrationService

import pytest
pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _migration_db(script_rows=None, tasks=None, templates=None):
    """构造 mock DB：PreJudgeScript 全量查询返回 script_rows（.all()），
    EvaluationTask/EvaluationTemplate 过滤查询返回 tasks/templates"""
    db = MagicMock()

    def _query_side_effect(model):
        model_name = getattr(model, "__name__", str(model))
        q = MagicMock()
        if model_name == "PreJudgeScript":
            q.all.return_value = script_rows or []
            q.filter.return_value.first.return_value = None  # seed 存在性检查：无既有行
        elif model_name == "EvaluationTask":
            q.filter.return_value.all.return_value = tasks or []
            q.filter.return_value.filter.return_value.all.return_value = tasks or []
        elif model_name == "EvaluationTemplate":
            q.filter.return_value.all.return_value = templates or []
        return q

    db.query.side_effect = _query_side_effect
    return db


def _legacy_task(**overrides):
    """构造带旧版配置的任务 ORM 对象（不入库）"""
    task = EvaluationTask(
        id=overrides.pop("id", "task_migtest01"),
        name=overrides.pop("name", "t"),
        evaluation_type=overrides.pop("evaluation_type", "judge"),
        status="pending",
    )
    task.steps = overrides.pop("steps", None)
    task.pre_scripts = overrides.pop("pre_scripts", None)
    task.score_details = overrides.pop("score_details", None)
    task.evaluation_targets = overrides.pop("evaluation_targets", None)
    return task


# ---------------- 规则1：steps 合并进 score_details ----------------

class TestMigrateTaskRule1:

    def test_steps_merged_into_score_details(self):
        """steps 非空 → score_details 末尾追加编号步骤块，steps 置 None"""
        task = _legacy_task(steps=["阅读材料", "逐项比对"], score_details="基准评分")
        db = _migration_db()
        summary = ConfigMigrationService(db).migrate_task(task)

        assert summary["migrated"] is True
        assert summary["steps_migrated"] == 2
        assert task.steps is None
        assert task.score_details.startswith("基准评分")
        assert "评分步骤：\n1. 阅读材料\n2. 逐项比对" in task.score_details
        assert db.commit.called

    def test_steps_merged_with_empty_score_details(self):
        """score_details 为空 → 直接以「评分步骤：」块开头"""
        task = _legacy_task(steps=["仅一步"], score_details=None)
        ConfigMigrationService(_migration_db()).migrate_task(task)

        assert task.score_details.startswith("评分步骤：\n1. 仅一步")
        assert task.steps is None

    def test_no_steps_noop(self):
        """steps 为空 → 规则1 不生效"""
        task = _legacy_task(steps=None, score_details="既有标准")
        summary = ConfigMigrationService(_migration_db()).migrate_task(task)

        assert summary["steps_migrated"] == 0
        assert task.score_details == "既有标准"
        assert task.steps is None


# ---------------- 规则2：pre_scripts 转为 script_output 行 ----------------

class TestMigrateTaskRule2:

    def test_pre_scripts_converted_with_output_var(self):
        """带 output_var 的脚本步骤 → display_name=output_var，output_key=output_schema 首键"""
        task = _legacy_task(pre_scripts=[{
            "script_id": "builtin:allure_vs_cases",
            "input_mapping": {"cases_file": {"field": "cases_file", "value_type": "local"}},
            "params": {"strict": True},
            "output_var": "allure_compare",
            "on_fail": "abort",
        }])
        allure_row = SimpleNamespace(id="pjs_builtin_allure_vs_cases", name="allure_vs_cases",
                                     exec_type="builtin",
                                     output_schema={"consistency_rate": "number：一致率"}, is_active=True)
        summary = ConfigMigrationService(_migration_db(script_rows=[allure_row])).migrate_task(task)

        assert summary["pre_scripts_migrated"] == 1
        assert summary["target_ids"] == ["target_mig_0_task_mig"]
        assert task.pre_scripts is None

        row = task.evaluation_targets[0]
        assert row["id"] == "target_mig_0_task_mig"
        assert row["display_name"] == "allure_compare"
        assert row["source"] == "script_output"
        assert row["required"] is False
        # 统一落库（规则3）：转换产物的 builtin: 历史引用直接落 DB 记录 id
        assert row["source_config"]["script_id"] == "pjs_builtin_allure_vs_cases"
        # allure_vs_cases 的 output_schema 首键为 consistency_rate
        assert row["source_config"]["output_key"] == "consistency_rate"
        assert row["source_config"]["input_mapping"] == {"cases_file": {"field": "cases_file", "value_type": "local"}}
        assert row["source_config"]["params"] == {"strict": True}
        assert row["source_config"]["on_fail"] == "abort"
        assert row["source_config"]["output_var"] == "allure_compare"

    def test_display_name_falls_back_to_script_name(self):
        """无 output_var → display_name 取脚本名，on_fail 缺省 continue"""
        xml_row = SimpleNamespace(id="pjs_builtin_xml_diff", name="xml_diff",
                                  exec_type="builtin",
                                  output_schema={"identical": "boolean"}, is_active=True)
        task = _legacy_task(pre_scripts=[{"script_id": "builtin:xml_diff"}])
        ConfigMigrationService(_migration_db(script_rows=[xml_row])).migrate_task(task)

        row = task.evaluation_targets[0]
        assert row["display_name"] == "xml_diff"
        assert row["source_config"]["output_var"] is None
        # xml_diff 的 output_schema 首键为 identical
        assert row["source_config"]["output_key"] == "identical"
        assert row["source_config"]["on_fail"] == "continue"

    def test_rule3_rewrites_builtin_refs_in_targets(self):
        """规则3：evaluation_targets script 行的 builtin:<name> 历史引用改写为 DB 记录 id；幂等"""
        xml_row = SimpleNamespace(id="pjs_builtin_xml_diff", name="xml_diff",
                                  exec_type="builtin", output_schema=None, is_active=True)
        task = _legacy_task(evaluation_targets=[
            {"id": "t_model", "source": "model_output", "display_name": "模型输出",
             "source_config": {}, "required": True},
            {"id": "t_script", "source": "script_output", "display_name": "比对结果",
             "required": False,
             "source_config": {"script_id": "builtin:xml_diff", "output_key": None,
                               "input_mapping": {}, "params": {}, "on_fail": "continue",
                               "output_var": "legacy_out"}},
        ])
        summary = ConfigMigrationService(_migration_db(script_rows=[xml_row])).migrate_task(task)
        assert summary["migrated"] is True
        row = task.evaluation_targets[1]
        assert row["source_config"]["script_id"] == "pjs_builtin_xml_diff"
        assert row["source_config"]["output_var"] == "legacy_out"  # 其余配置不动

        # 幂等：再迁移无变化
        again = ConfigMigrationService(_migration_db(script_rows=[xml_row])).migrate_task(task)
        assert again["migrated"] is False

    def test_rule3_keeps_unresolvable_builtin_ref(self):
        """规则3：builtin: 引用解析不到脚本记录时保留原值（由校验层提示）"""
        task = _legacy_task(evaluation_targets=[
            {"id": "t_s", "source": "script_output", "display_name": "比对",
             "required": False,
             "source_config": {"script_id": "builtin:gone_script", "output_key": None,
                               "input_mapping": {}, "params": {}, "on_fail": "continue",
                               "output_var": None}},
        ])
        summary = ConfigMigrationService(_migration_db()).migrate_task(task)
        assert summary["migrated"] is False
        assert task.evaluation_targets[0]["source_config"]["script_id"] == "builtin:gone_script"

    def test_db_script_resolved_by_row(self):
        """DB 脚本记录：按 id 解析脚本名与 output_schema 首键"""
        db_script = SimpleNamespace(id="pjs_db1", name="db脚本", exec_type="script",
                                    output_schema={"result": "string：结论", "rate": "number"})
        task = _legacy_task(pre_scripts=[{"script_id": "pjs_db1", "output_var": None}])
        ConfigMigrationService(_migration_db(script_rows=[db_script])).migrate_task(task)

        row = task.evaluation_targets[0]
        assert row["display_name"] == "db脚本"
        assert row["source_config"]["output_key"] == "result"

    def test_unknown_script_row_still_migrated(self):
        """脚本已不存在（查无记录）→ 行仍迁移，display_name 回退 script_id，output_key=None"""
        task = _legacy_task(pre_scripts=[{"script_id": "pjs_ghost"}])
        summary = ConfigMigrationService(_migration_db(script_rows=[])).migrate_task(task)

        assert summary["pre_scripts_migrated"] == 1
        row = task.evaluation_targets[0]
        assert row["display_name"] == "pjs_ghost"
        assert row["source_config"]["output_key"] is None

    def test_appends_to_existing_targets(self):
        """已有 evaluation_targets → 迁移行追加在末尾，不动既有行"""
        existing = {"id": "t_model", "display_name": "模型输出", "source": "model_output",
                    "source_config": {}, "required": True}
        task = _legacy_task(pre_scripts=[{"script_id": "builtin:log_grep"}],
                            evaluation_targets=[existing])
        ConfigMigrationService(_migration_db()).migrate_task(task)

        assert len(task.evaluation_targets) == 2
        assert task.evaluation_targets[0] == existing
        assert task.evaluation_targets[1]["id"] == "target_mig_0_task_mig"

    def test_both_rules_in_one_pass(self):
        """steps 与 pre_scripts 同时存在 → 两条规则一次迁移全部生效"""
        task = _legacy_task(
            steps=["第一步", "第二步"],
            pre_scripts=[{"script_id": "builtin:log_grep", "output_var": "lg"}],
            score_details="标准",
        )
        summary = ConfigMigrationService(_migration_db()).migrate_task(task)

        assert summary["steps_migrated"] == 2
        assert summary["pre_scripts_migrated"] == 1
        assert task.steps is None and task.pre_scripts is None
        assert "评分步骤：" in task.score_details
        assert task.evaluation_targets[0]["source"] == "script_output"


# ---------------- 幂等 ----------------

class TestMigrateTaskIdempotent:

    def test_second_run_is_noop(self):
        """重复迁移：migrated=False、字段不再变化"""
        task = _legacy_task(steps=["步骤A"], pre_scripts=[{"script_id": "builtin:xml_diff"}],
                            score_details="标准")
        service = ConfigMigrationService(_migration_db())
        first = service.migrate_task(task)
        assert first["migrated"] is True

        snapshot_details = task.score_details
        snapshot_targets = list(task.evaluation_targets)
        second = service.migrate_task(task)

        assert second["migrated"] is False
        assert second["changes"] == []
        assert task.score_details == snapshot_details
        assert task.evaluation_targets == snapshot_targets
        assert task.steps is None and task.pre_scripts is None

    def test_duplicate_target_id_not_readded(self):
        """目标行 id 已存在（如半迁移状态）→ 不重复追加"""
        task = _legacy_task(pre_scripts=[{"script_id": "builtin:xml_diff"}])
        task.evaluation_targets = [
            {"id": "target_mig_0_task_mig", "display_name": "既有行", "source": "script_output",
             "required": False, "source_config": {"script_id": "builtin:xml_diff"}},
        ]
        summary = ConfigMigrationService(_migration_db()).migrate_task(task)

        assert summary["pre_scripts_migrated"] == 0
        assert len(task.evaluation_targets) == 1
        assert task.pre_scripts is None


# ---------------- 批量 ----------------

class TestMigrateAll:

    def test_migrate_all_tasks_summary(self):
        """批量任务：一条需迁移、一条已迁移 → {total:2, migrated:1, skipped:1, errors:[]}"""
        legacy = _legacy_task(steps=["旧步骤"])
        clean = _legacy_task()
        db = _migration_db(tasks=[legacy, clean])

        result = ConfigMigrationService(db).migrate_all_tasks()

        assert result == {"total": 2, "migrated": 1, "skipped": 1, "errors": []}
        assert legacy.steps is None

    def test_migrate_all_tasks_error_recorded(self):
        """单条迁移异常 → 记入 errors 并回滚，其余条目继续"""
        legacy = _legacy_task(steps=["旧步骤"])
        broken = _legacy_task(id="task_broken01")
        db = _migration_db(tasks=[legacy, broken])

        with patch.object(ConfigMigrationService, "migrate_task") as mock_migrate:
            # 仅对 broken 任务抛错：按任务 id 区分
            def _side_effect(task):
                if task.id == "task_broken01":
                    raise ValueError("迁移失败")
                return {"migrated": True}

            mock_migrate.side_effect = _side_effect
            result = ConfigMigrationService(db).migrate_all_tasks()

        assert result["total"] == 2
        assert result["migrated"] == 1
        assert result["errors"] == [{"id": "task_broken01", "error": "迁移失败"}]
        assert db.rollback.called

    def test_migrate_all_templates_text_steps(self):
        """批量模板（仅规则1）：steps 为 Text 按 \\n 拆分合并；无 steps 模板跳过"""
        tmpl_legacy = EvaluationTemplate(id="tmpl_a", name="a")
        tmpl_legacy.steps = "步骤一\n步骤二"
        tmpl_legacy.score_details = "模板标准"
        tmpl_clean = EvaluationTemplate(id="tmpl_b", name="b")
        tmpl_clean.steps = None
        tmpl_clean.score_details = "标准"
        db = _migration_db(templates=[tmpl_legacy, tmpl_clean])

        result = ConfigMigrationService(db).migrate_all_templates()

        assert result == {"total": 2, "migrated": 1, "skipped": 1, "errors": []}
        assert tmpl_legacy.steps is None
        assert "评分步骤：\n1. 步骤一\n2. 步骤二" in tmpl_legacy.score_details
        assert tmpl_legacy.score_details.startswith("模板标准")
        assert tmpl_clean.score_details == "标准"

    def test_template_blank_lines_skipped(self):
        """模板 steps Text 含空行/空白行 → 仅保留非空行"""
        tmpl = EvaluationTemplate(id="tmpl_c", name="c")
        tmpl.steps = "\n  \n步骤A\n\n"
        tmpl.score_details = None
        ConfigMigrationService(_migration_db(templates=[tmpl])).migrate_template(tmpl)

        assert "评分步骤：\n1. 步骤A" in tmpl.score_details
        assert tmpl.steps is None

    def test_migrate_template_noop(self):
        """模板无 steps → migrated=False 不提交"""
        tmpl = EvaluationTemplate(id="tmpl_d", name="d")
        tmpl.steps = None
        tmpl.score_details = "标准"
        db = _migration_db()
        summary = ConfigMigrationService(db).migrate_template(tmpl)

        assert summary["migrated"] is False
        assert db.commit.called is False
