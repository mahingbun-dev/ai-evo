"""
长会话 Agent 评测任务创建测试

覆盖：evaluation_type=agent 白名单放行、judge_engine 校验、
agent 类型数据集必须含 session_messages 字段的校验、
judge/agent 类型必须配置待评测数据（契约 v3 §五）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.evaluation_task_service import EvaluationTaskService

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _orm_item(data):
    """模拟 get_data_list_v2 的真实返回形态：EvaluationDataV2 ORM 对象（data 为属性）"""
    return SimpleNamespace(id="data2_a", data=data)


def _targets():
    """最小可用的待评测数据配置（模型输出行）"""
    return [
        {"id": "t_model", "name": "model_output", "source": "model_output",
         "source_config": {}, "display_name": "模型输出", "required": True},
    ]


def _service_with_items(items):
    db = MagicMock()
    service = EvaluationTaskService(db)
    patcher = patch(
        "app.services.dataset_service.DatasetDataService.get_data_list_v2",
        return_value=items,
    )
    patcher.start()
    return service, db, patcher


class TestCreateTaskAgentType:
    def teardown_method(self):
        pass

    def test_agent_type_accepted_with_orm_session_dataset(self):
        """agent 类型 + ORM 形态的会话数据集（真实返回类型）→ 创建成功"""
        service, db, patcher = _service_with_items([
            _orm_item({"session_messages": {}, "user_input": "x"})
        ])
        try:
            task = service.create_task(
                name="长会话评测",
                evaluation_type="agent",
                dataset_id="ds_1",
                judge_engine="claude_agent",
                judge_count=1,
                evaluation_targets=_targets(),
            )
            assert db.add.called
        finally:
            patcher.stop()

    def test_agent_type_accepted_with_dict_session_dataset(self):
        """兼容 dict 形态（防御性）"""
        service, db, patcher = _service_with_items([
            {"id": "data2_a", "data": {"session_messages": {}, "user_input": "x"}}
        ])
        try:
            service.create_task(
                name="长会话评测",
                evaluation_type="agent",
                dataset_id="ds_1",
                evaluation_targets=_targets(),
            )
            assert db.add.called
        finally:
            patcher.stop()

    def test_agent_type_rejected_without_session_field(self):
        """agent 类型 + 普通 Excel 数据集（无 session_messages，ORM 形态）→ 明确报错"""
        service, db, patcher = _service_with_items([
            _orm_item({"user_input": "q", "answer": "a"})
        ])
        try:
            with pytest.raises(ValueError, match="session_messages"):
                service.create_task(
                    name="长会话评测",
                    evaluation_type="agent",
                    dataset_id="ds_1",
                )
        finally:
            patcher.stop()

    def test_agent_type_rejected_with_none_data(self):
        """ORM data 属性为 None 时不崩溃，按缺少字段报错"""
        service, db, patcher = _service_with_items([SimpleNamespace(id="data2_a", data=None)])
        try:
            with pytest.raises(ValueError, match="session_messages"):
                service.create_task(
                    name="长会话评测",
                    evaluation_type="agent",
                    dataset_id="ds_1",
                )
        finally:
            patcher.stop()

    def test_agent_type_rejected_with_empty_dataset(self):
        service, db, patcher = _service_with_items([])
        try:
            with pytest.raises(ValueError, match="数据集为空"):
                service.create_task(
                    name="长会话评测",
                    evaluation_type="agent",
                    dataset_id="ds_1",
                )
        finally:
            patcher.stop()

    def test_invalid_judge_engine_rejected(self):
        service, db, patcher = _service_with_items([])
        try:
            with pytest.raises(ValueError, match="裁判引擎"):
                service.create_task(
                    name="t",
                    evaluation_type="judge",
                    dataset_id=None,
                    judge_engine="unknown_engine",
                )
        finally:
            patcher.stop()

    def test_judge_type_still_valid(self):
        """回归：judge 类型配置待评测数据后创建不受影响"""
        service, db, patcher = _service_with_items([])
        try:
            service.create_task(
                name="普通裁判", evaluation_type="judge", dataset_id=None,
                # judge + 模型调用来源必须有被评测模型（新的模型输出校验）
                model_provider="m1",
                evaluation_targets=_targets(),
            )
            assert db.add.called
        finally:
            patcher.stop()

    def test_agent_type_without_targets_rejected(self):
        """agent 类型未配置待评测数据 → ValueError（契约 v3 §五：targets 必填）"""
        service, db, patcher = _service_with_items([
            _orm_item({"session_messages": {}, "user_input": "x"})
        ])
        try:
            with pytest.raises(ValueError, match="待评测数据"):
                service.create_task(
                    name="长会话评测",
                    evaluation_type="agent",
                    dataset_id="ds_1",
                )
        finally:
            patcher.stop()
