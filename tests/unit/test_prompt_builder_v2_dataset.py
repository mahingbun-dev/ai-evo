"""
PromptBuilder V2 数据集格式兼容性测试
验证 _get_field_value_from_payload 和 _resolve_variable_value 能正确处理 V2 格式的 _dataset_item，
其中字段嵌套在 data 键下。
"""
import pytest
from unittest.mock import MagicMock
from app.services.prompt_builder import PromptBuilder

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _make_pb_with_vars(judge_variable_config=None, runtime_variables=None):
    """创建 PromptBuilder 实例，task 为 mock"""
    task = MagicMock()
    task.judge_prompt = ""
    task.evaluation_scene = ""
    task.scene_desc = ""
    task.evaluation_metrics = []
    task.max_score = 10
    task.score_details = ""
    task.steps = []
    task.judge_variable_config = judge_variable_config or {}
    task.runtime_variables = runtime_variables or {}
    task.evaluation_targets = None
    return PromptBuilder(task)


class TestGetFieldValueFromPayloadV2:
    """_get_field_value_from_payload V2 格式测试"""

    def test_field_in_v2_data_nested(self):
        """字段在 _dataset_item.data 中时应能正确获取"""
        pb = _make_pb_with_vars()
        payload = {
            "user_input": "test question",
            "_dataset_item": {
                "id": 1,
                "index": 0,
                "data": {
                    "参考答案_is_valid": "true",
                    "用户输入_category": "洗衣机",
                }
            }
        }
        value = pb._get_field_value_from_payload(
            field="参考答案_is_valid",
            payload=payload,
            dataset_item=payload.get("_dataset_item", {}),
        )
        assert value == "true", f"Expected 'true', got {value!r}"

    def test_field_in_v2_data_other_field(self):
        """其他字段也能在 V2 data 中正确获取"""
        pb = _make_pb_with_vars()
        payload = {
            "_dataset_item": {
                "id": 1,
                "data": {
                    "用户输入_category": "洗衣机",
                    "用户输入_image_type": "SN码",
                }
            }
        }
        value = pb._get_field_value_from_payload(
            field="用户输入_image_type",
            payload=payload,
            dataset_item=payload.get("_dataset_item", {}),
        )
        assert value == "SN码", f"Expected 'SN码', got {value!r}"

    def test_field_at_top_level_priority(self):
        """字段在 _dataset_item 顶层时优先从顶层获取（兼容非 V2 格式）"""
        pb = _make_pb_with_vars()
        payload = {
            "_dataset_item": {
                "id": 1,
                "index": 0,
                "data": {
                    "参考答案_is_valid": "false",
                },
                "参考答案_is_valid": "true_from_top",
            }
        }
        # 顶层字段优先
        value = pb._get_field_value_from_payload(
            field="参考答案_is_valid",
            payload=payload,
            dataset_item=payload.get("_dataset_item", {}),
        )
        assert value == "true_from_top", f"Expected 'true_from_top', got {value!r}"

    def test_field_not_found_returns_empty(self):
        """不存在的字段返回空字符串"""
        pb = _make_pb_with_vars()
        payload = {
            "_dataset_item": {
                "id": 1,
                "data": {
                    "some_field": "some_value",
                }
            }
        }
        value = pb._get_field_value_from_payload(
            field="non_existent_field",
            payload=payload,
            dataset_item=payload.get("_dataset_item", {}),
        )
        assert value == ""

    def test_no_dataset_item_returns_empty(self):
        """没有 _dataset_item 时返回空字符串"""
        pb = _make_pb_with_vars()
        payload = {"user_input": "test"}
        value = pb._get_field_value_from_payload(
            field="any_field",
            payload=payload,
        )
        assert value == ""

    def test_predefined_fields_still_work(self):
        """预定义字段 (user_input, ref_answer) 仍正常工作"""
        pb = _make_pb_with_vars()
        payload = {
            "user_input": "hello",
            "ref_answer": "world",
        }
        assert pb._get_field_value_from_payload("user_input", payload) == "hello"
        assert pb._get_field_value_from_payload("ref_answer", payload) == "world"

    def test_field_in_payload_directly(self):
        """字段直接在 payload 顶层时优先获取"""
        pb = _make_pb_with_vars()
        payload = {
            "参考答案_is_valid": "true_direct",
            "_dataset_item": {
                "data": {
                    "参考答案_is_valid": "true_from_data",
                }
            }
        }
        value = pb._get_field_value_from_payload(
            field="参考答案_is_valid",
            payload=payload,
        )
        assert value == "true_direct"


class TestResolveVariableValueV2:
    """_resolve_variable_value V2 格式测试"""

    def test_dataset_field_in_v2_data(self):
        """dataset_field 来源能从 V2 data 中正确取值"""
        pb = _make_pb_with_vars()
        payload = {
            "_dataset_item": {
                "id": 1,
                "data": {
                    "参考答案_is_valid": "true",
                }
            }
        }
        var_config = {
            "key": "expected_result",
            "source": "dataset_field",
            "source_config": {"field": "参考答案_is_valid"},
        }
        value = pb._resolve_variable_value(var_config, payload, model_output="")
        assert value == "true", f"Expected 'true', got {value!r}"

    def test_dataset_field_not_found_returns_empty(self):
        """dataset_field 来源找不到字段时返回空"""
        pb = _make_pb_with_vars()
        payload = {"_dataset_item": {"data": {}}}
        var_config = {
            "key": "missing_var",
            "source": "dataset_field",
            "source_config": {"field": "non_existent"},
        }
        value = pb._resolve_variable_value(var_config, payload, model_output="")
        assert value == ""
