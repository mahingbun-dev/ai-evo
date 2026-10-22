"""judge「模型输出来自数据集」单元测试

覆盖：
1. create_task：dataset_field 成功落库；缺字段名/字段不存在/hub 辅助列 → ValueError；
   来源白名单；judge + model_call 无 model_provider → ValueError（agent/plugin 不受影响）
2. update_task：kwargs 与任务既有值合并后的最终态校验
3. 执行器 _evaluate_single：dataset_field 不调用 ModelCallerSync，model_output 取字段值
   且裁判收到该值；none 同样跳过模型调用；存量任务（source 为空）仍走模型调用
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor
from app.services.evaluation_task_service import EvaluationTaskService

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------- 测试工具 ----------------

def _model_target():
    """最小可用的待评测数据配置（模型输出行）"""
    return [
        {"id": "t_model", "name": "model_output", "source": "model_output",
         "source_config": {}, "display_name": "模型输出", "required": True},
    ]


def _service(field_metas=None, items=None):
    """构造 Service：mock DB 查询、数据集数据项与字段元数据（与既有测试同模式）"""
    db = MagicMock()

    def _query_side_effect(model):
        q = MagicMock()
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


def _stop(patchers):
    for p in patchers:
        p.stop()


def _judge_task(**overrides):
    """update_task 用最小任务对象（含新字段，存量任务字段为 None）"""
    base = dict(
        id="task_out_1",
        evaluation_type="judge",
        dataset_id=None,
        model_provider=None,
        model_output_source=None,
        model_output_field=None,
        evaluation_targets=_model_target(),
        consistency_config=None,
        score_details=None,
        evaluation_metrics=None,
        judge_variable_config=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------- 一、create_task 校验 ----------------

class TestCreateTaskModelOutputSource:

    def test_judge_dataset_field_success(self):
        """judge + dataset_field（字段存在）→ 创建成功并落库新字段"""
        service, db, patchers = _service(
            field_metas=[
                {"name": "question", "type": "string"},
                {"name": "actual_result", "type": "string"},
            ]
        )
        try:
            service.create_task(
                name="知识库检索评测",
                evaluation_type="judge",
                dataset_id="ds_1",
                model_output_source="dataset_field",
                model_output_field="actual_result",
                evaluation_targets=_model_target(),
            )
        finally:
            _stop(patchers)

        assert db.add.called
        task = db.add.call_args[0][0]
        assert task.model_output_source == "dataset_field"
        assert task.model_output_field == "actual_result"

    def test_source_whitespace_normalized_on_create(self):
        """来源值带空白/大小写时归一化后落库，校验与执行器分支口径一致"""
        service, db, patchers = _service(field_metas=[{"name": "actual_result"}])
        try:
            service.create_task(
                name="t", evaluation_type="judge", dataset_id="ds_1",
                model_output_source=" DATASET_FIELD ",
                model_output_field="actual_result",
                evaluation_targets=_model_target(),
            )
        finally:
            _stop(patchers)

        task = db.add.call_args[0][0]
        assert task.model_output_source == "dataset_field"

    def test_legacy_dataset_without_metadata_accepts_data_field(self):
        """无字段元数据的存量数据集：按首条数据 key 枚举字段（与表单接口回退口径一致）"""
        service, db, patchers = _service(
            field_metas=[],
            items=[SimpleNamespace(data={"actual_result": "x", "answer_is_media": "否"})],
        )
        try:
            service.create_task(
                name="t", evaluation_type="judge", dataset_id="ds_legacy",
                model_output_source="dataset_field",
                model_output_field="actual_result",
                evaluation_targets=_model_target(),
            )
        finally:
            _stop(patchers)

        task = db.add.call_args[0][0]
        assert task.model_output_source == "dataset_field"
        assert task.model_output_field == "actual_result"

    def test_dataset_field_without_field_name_rejected(self):
        service, db, patchers = _service(field_metas=[{"name": "actual_result"}])
        try:
            with pytest.raises(ValueError, match="model_output_field"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id="ds_1",
                    model_output_source="dataset_field",
                    evaluation_targets=_model_target(),
                )
        finally:
            _stop(patchers)

    def test_dataset_field_without_dataset_rejected(self):
        service, db, patchers = _service()
        try:
            with pytest.raises(ValueError, match="选择数据集"):
                service.create_task(
                    name="t", evaluation_type="judge",
                    model_output_source="dataset_field",
                    model_output_field="actual_result",
                    evaluation_targets=_model_target(),
                )
        finally:
            _stop(patchers)

    def test_dataset_field_not_in_metadata_rejected(self):
        service, db, patchers = _service(field_metas=[{"name": "actual_result"}])
        try:
            with pytest.raises(ValueError, match="不存在"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id="ds_1",
                    model_output_source="dataset_field",
                    model_output_field="no_such_field",
                    evaluation_targets=_model_target(),
                )
        finally:
            _stop(patchers)

    def test_hub_auxiliary_field_rejected(self):
        """hub 辅助列（*_hub_file）不是数据字段，不能作为模型输出来源"""
        service, db, patchers = _service(
            field_metas=[{"name": "cases_file_hub_file", "type": "string"}]
        )
        try:
            with pytest.raises(ValueError, match="不存在"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id="ds_1",
                    model_output_source="dataset_field",
                    model_output_field="cases_file_hub_file",
                    evaluation_targets=_model_target(),
                )
        finally:
            _stop(patchers)

    def test_invalid_source_rejected(self):
        service, db, patchers = _service()
        try:
            with pytest.raises(ValueError, match="不支持的模型输出来源"):
                service.create_task(
                    name="t", evaluation_type="judge", model_provider="m1",
                    model_output_source="api",
                    evaluation_targets=_model_target(),
                )
        finally:
            _stop(patchers)

    def test_non_string_source_rejected(self):
        """服务层直调传非字符串来源：给 400 语义的 ValueError，而非 AttributeError"""
        service, db, patchers = _service()
        try:
            with pytest.raises(ValueError, match="不支持的模型输出来源"):
                service.create_task(
                    name="t", evaluation_type="judge", model_provider="m1",
                    model_output_source=123,
                    evaluation_targets=_model_target(),
                )
        finally:
            _stop(patchers)

    def test_judge_model_call_without_provider_rejected(self):
        service, db, patchers = _service()
        try:
            with pytest.raises(ValueError, match="被评测模型"):
                service.create_task(
                    name="t", evaluation_type="judge",
                    evaluation_targets=_model_target(),
                )
        finally:
            _stop(patchers)

    def test_agent_model_call_without_provider_passes(self):
        """agent 不受 judge 的 model_provider 约束（会话文件即模型输出）"""
        service, db, patchers = _service(
            items=[{"data": {"session_messages": {"messages": []}, "user_input": "x"}}]
        )
        try:
            service.create_task(
                name="长会话", evaluation_type="agent", dataset_id="ds_1",
                evaluation_targets=_model_target(),
            )
        finally:
            _stop(patchers)
        assert db.add.called

    def test_plugin_model_call_without_provider_passes(self):
        service, db, patchers = _service()
        try:
            service.create_task(
                name="插件评测", evaluation_type="plugin", dataset_id="ds_1",
                plugin_config={"depth": "quick", "source": "dataset"},
            )
        finally:
            _stop(patchers)
        assert db.add.called


# ---------------- 二、update_task 合并最终态校验 ----------------

class TestUpdateTaskModelOutputSource:

    def test_update_to_dataset_field_without_provider_passes(self):
        """存量 judge 任务无 model_provider：改为 dataset_field 应放行（最终态合法）"""
        service, db, patchers = _service(field_metas=[{"name": "actual_result"}])
        task = _judge_task(dataset_id="ds_1")
        service.get_task = MagicMock(return_value=task)
        try:
            service.update_task(
                "task_out_1",
                model_output_source="dataset_field",
                model_output_field="actual_result",
            )
        finally:
            _stop(patchers)

        assert task.model_output_source == "dataset_field"
        assert task.model_output_field == "actual_result"
        assert db.commit.called

    def test_update_with_existing_provider_passes(self):
        """任务既有 model_provider 参与合并校验：仅改名不触发缺模型报错"""
        service, db, patchers = _service()
        task = _judge_task(model_provider="mc_1")
        service.get_task = MagicMock(return_value=task)
        try:
            service.update_task("task_out_1", name="新名字")
        finally:
            _stop(patchers)
        assert db.commit.called

    def test_update_legacy_task_without_provider_rejected(self):
        """合并最终态：存量 judge 任务 source 为空（=model_call）且无 provider → 拒绝"""
        service, db, patchers = _service()
        task = _judge_task()
        service.get_task = MagicMock(return_value=task)
        try:
            with pytest.raises(ValueError, match="被评测模型"):
                service.update_task("task_out_1", name="新名字")
        finally:
            _stop(patchers)

    def test_update_dataset_field_invalid_field_rejected(self):
        """只改字段名（未重传来源）也按最终态校验字段存在性"""
        service, db, patchers = _service(field_metas=[{"name": "actual_result"}])
        task = _judge_task(
            dataset_id="ds_1",
            model_output_source="dataset_field", model_output_field="actual_result",
        )
        service.get_task = MagicMock(return_value=task)
        try:
            with pytest.raises(ValueError, match="不存在"):
                service.update_task("task_out_1", model_output_field="no_such_field")
        finally:
            _stop(patchers)


# ---------------- 三、执行器模型输出来源 ----------------

def _executor_task(**overrides):
    base = dict(
        id="task_out_x",
        evaluation_type="judge",
        judge_engine="claude_agent",
        judge_count=1,
        model_config=None,
        model_provider=None,
        model_output_source="model_call",
        model_output_field=None,
        max_score=10,
        evaluation_metrics=[{"name": "维度A"}],
        score_details="",
        judge_variable_config={},
        evaluation_targets=None,
        runtime_variables={},
        variable_mappings={},
        variable_sources={},
        enable_user_import=False,
        user_import_template=None,
        input_field="user_input",
        reference_field="answer",
        pre_scripts=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _mock_judge_stack():
    pb = patch("app.services.prompt_builder.PromptBuilder")
    cc = patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent")
    ws = patch("app.services.claude_code_judge_agent.write_session_file",
               return_value="/tmp/fake_session.md")
    lg = patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent")
    return pb, cc, ws, lg


def _run_single(task, payload):
    executor = EvaluationExecutor(max_workers=1, task_id="task_out_x")
    pb, cc, ws, lg = _mock_judge_stack()
    with pb as mock_pb, cc as mock_cc, ws as mock_ws, lg as mock_lg:
        mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
        mock_pb.return_value._build_system_prompt.return_value = "system prompt"
        mock_cc.return_value.run_parallel.return_value = [
            {"score": 9, "feedback": "ok", "dimension_scores": {}}]
        mock_lg.return_value.run_parallel.return_value = [
            {"score": 9, "feedback": "ok", "dimension_scores": {}}]
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            mock_caller.side_effect = AssertionError("dataset_field/none 不应调用模型")
            result = executor._evaluate_single(payload, task)
    return result, mock_caller, mock_pb


class TestExecutorModelOutputSource:

    def test_dataset_field_skips_model_and_judge_receives_value(self):
        task = _executor_task(
            model_output_source="dataset_field",
            model_output_field="actual_result",
            judge_variable_config={"actual": {"source": "model_output"}},
        )
        payload = {
            "idx": 0,
            "user_input": "问题",
            "_dataset_item": {
                "id": "data_1",
                "data": {"user_input": "问题", "actual_result": "知识库检索结果"},
            },
        }
        result, mock_caller, mock_pb = _run_single(task, payload)

        assert not mock_caller.called
        assert result["success"] is True
        assert result["model_output"] == "知识库检索结果"
        # 裁判变量 source=model_output 拿到该字段值
        call_kwargs = mock_pb.return_value.build_user_prompt_from_judge_vars.call_args.kwargs
        assert call_kwargs["model_output"] == "知识库检索结果"
        assert call_kwargs["payload"]["actual"] == "知识库检索结果"

    def test_dataset_field_non_string_serialized_as_json(self):
        task = _executor_task(
            model_output_source="dataset_field",
            model_output_field="actual_result",
        )
        payload = {
            "idx": 0,
            "user_input": "问题",
            "_dataset_item": {"id": "data_1", "data": {"actual_result": {"命中": ["文档A"]}}},
        }
        result, mock_caller, _ = _run_single(task, payload)

        assert not mock_caller.called
        assert result["success"] is True
        assert result["model_output"] == '{"命中": ["文档A"]}'

    def test_dataset_field_supports_orm_dataset_item(self):
        """兼容 get_data_list_v2 直出的 ORM 形态（data 为属性）"""
        task = _executor_task(
            model_output_source="dataset_field",
            model_output_field="actual_result",
        )
        dataset_item = SimpleNamespace(id="data_1", data={"actual_result": "ORM 字段值"})
        result, mock_caller, _ = _run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": dataset_item}
        )

        assert not mock_caller.called
        assert result["model_output"] == "ORM 字段值"

    def test_source_none_skips_model(self):
        task = _executor_task(model_output_source="none")
        result, mock_caller, _ = _run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"data": {}}}
        )

        assert not mock_caller.called
        assert result["success"] is True
        assert result["model_output"] == ""
        # 空值校验按既有口径返回 0 分空输入
        assert result["row_status"] == "empty_input"

    def test_source_with_whitespace_still_skips_model(self):
        """来源值带空白时执行器同样归一化，避免落回 model_call 分支"""
        task = _executor_task(
            model_output_source=" dataset_field ",
            model_output_field="actual_result",
        )
        result, mock_caller, _ = _run_single(
            task,
            {"idx": 0, "user_input": "q", "_dataset_item": {"data": {"actual_result": "值"}}},
        )

        assert not mock_caller.called
        assert result["model_output"] == "值"

    def test_legacy_none_source_still_calls_model(self):
        """存量任务 model_output_source 为 None → 仍走模型调用分支"""
        task = _executor_task(model_output_source=None)
        executor = EvaluationExecutor(max_workers=1, task_id="task_out_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws as mock_ws, lg as mock_lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 8, "feedback": "ok", "dimension_scores": {}}]
            mock_lg.return_value.run_parallel.return_value = [
                {"score": 8, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(
                    {"idx": 0, "user_input": "q", "_dataset_item": {"data": {}}}, task
                )

        assert mock_caller.return_value.call.called
        assert result["success"] is True
        assert result["model_output"] == "模型输出"
