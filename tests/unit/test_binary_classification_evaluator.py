"""
二分分类评测引擎单元测试
"""
import pytest
import asyncio
from app.services.binary_classification_evaluator import (
    BinaryClassificationEvaluator,
    BinaryClassificationResult
)


class TestResolveField:
    """测试 _resolve_field 方法"""

    def test_resolve_field_dataset_field(self):
        """dataset_field - 从 item.data 正确解析"""
        evaluator = BinaryClassificationEvaluator()
        item = {"data": {"is_valid": True, "question": "test"}}
        model_output = {}
        source_config = {
            "source": "dataset_field",
            "source_config": {"field": "is_valid"}
        }
        result = evaluator._resolve_field(item, model_output, [], source_config)
        assert result is True

    def test_resolve_field_dataset_field_fallback(self):
        """dataset_field - item.data 无该字段时回退到 item 本身"""
        evaluator = BinaryClassificationEvaluator()
        item = {"expected_label": True}
        model_output = {}
        source_config = {
            "source": "dataset_field",
            "source_config": {"field": "expected_label"}
        }
        result = evaluator._resolve_field(item, model_output, [], source_config)
        assert result is True

    def test_resolve_field_dataset_field_from_dataset_item(self):
        """dataset_field - 从 item._dataset_item 解析（VariableInjector 生成的 payload 结构）"""
        evaluator = BinaryClassificationEvaluator()
        # 模拟 VariableInjector.generate_payload 生成的结构
        # _dataset_item 中包含原始数据集字段（flatten 后在顶层）
        item = {
            "_dataset_item": {
                "id": 1,
                "question": "test question",
                "expected_label": True,  # flatten 后在顶层
                "user_input": "test input"
            }
        }
        model_output = {}
        source_config = {
            "source": "dataset_field",
            "source_config": {"field": "expected_label"}
        }
        result = evaluator._resolve_field(item, model_output, [], source_config)
        assert result is True

    def test_resolve_field_model_output(self):
        """model_output - 从 model_output 正确解析"""
        evaluator = BinaryClassificationEvaluator()
        item = {}
        model_output = {"prediction": "true", "content": "some text"}
        source_config = {
            "source": "model_output",
            "source_config": {"field": "prediction"}
        }
        result = evaluator._resolve_field(item, model_output, [], source_config)
        assert result == "true"

    def test_resolve_field_manual_input(self):
        """manual_input - 从 source_config.value 直接获取"""
        evaluator = BinaryClassificationEvaluator()
        item = {}
        model_output = {}
        source_config = {
            "source": "manual_input",
            "source_config": {"value": True}
        }
        result = evaluator._resolve_field(item, model_output, [], source_config)
        assert result is True

    def test_resolve_field_manual_alias(self):
        """manual 别名 - 与 manual_input 等价"""
        evaluator = BinaryClassificationEvaluator()
        source_config = {
            "source": "manual",
            "source_config": {"manual_value": "yes"}
        }
        result = evaluator._resolve_field({}, {}, [], source_config)
        assert result == "yes"

    def test_resolve_field_chain_result(self):
        """chain_result - 从 chain_results 正确解析（chain_index 1-indexed）"""
        evaluator = BinaryClassificationEvaluator()
        chain_results = [{"variables": {"score": 0.9}}, {"variables": {"is_valid": True}}]
        # chain_index=1 表示第一个请求（1-indexed）
        source_config = {
            "source": "chain_result",
            "source_config": {"chain_index": 1, "var_names": ["score"]}
        }
        result = evaluator._resolve_field({}, {}, chain_results, source_config)
        assert result == 0.9
        # chain_index=2 表示第二个请求
        source_config2 = {
            "source": "chain_result",
            "source_config": {"chain_index": 2, "var_names": ["is_valid"]}
        }
        result2 = evaluator._resolve_field({}, {}, chain_results, source_config2)
        assert result2 is True

    def test_resolve_field_unknown_source(self):
        """未知 source - 返回空字符串"""
        evaluator = BinaryClassificationEvaluator()
        result = evaluator._resolve_field({}, {}, [], {"source": "unknown"})
        assert result == ""


class TestToBool:
    """测试 _to_bool 方法"""

    def test_bool_passthrough(self):
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool(True) is True
        assert evaluator._to_bool(False) is False

    def test_true_strings(self):
        evaluator = BinaryClassificationEvaluator()
        for s in ("true", "True", "TRUE", "1", "yes", "on"):
            assert evaluator._to_bool(s) is True, f"Failed for {s}"

    def test_false_strings(self):
        evaluator = BinaryClassificationEvaluator()
        for s in ("false", "False", "0", "no", "off", ""):
            assert evaluator._to_bool(s) is False, f"Failed for {s}"

    def test_numbers(self):
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool(1) is True
        assert evaluator._to_bool(0) is False
        assert evaluator._to_bool(3.14) is True

    def test_none(self):
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool(None) is False

    def test_json_string_with_result_field(self):
        """JSON 字符串包含 result 字段 - 应提取 result 值"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('{"result": true, "reasoning": "分析"}') is True
        assert evaluator._to_bool('{"result": false, "reasoning": "分析"}') is False

    def test_json_string_with_answer_field(self):
        """JSON 字符串包含 answer 字段 - 应提取 answer 值"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('{"answer": true}') is True
        assert evaluator._to_bool('{"answer": false}') is False

    def test_json_string_with_label_field(self):
        """JSON 字符串包含 label 字段 - 应提取 label 值"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('{"label": true}') is True

    def test_json_string_without_common_fields(self):
        """JSON 字符串没有常见字段 - 应查找第一个布尔值"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('{"status": true, "msg": "ok"}') is True

    def test_json_string_nested_result(self):
        """JSON 字符串 result 字段为字符串 - 应递归解析"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('{"result": "true"}') is True
        assert evaluator._to_bool('{"result": "false"}') is False

    def test_json_string_invalid(self):
        """无效 JSON 字符串 - 应返回 False"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('not json') is False

    def test_json_string_number(self):
        """JSON 字符串为纯数字"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('1') is True
        assert evaluator._to_bool('0') is False

    def test_capital_true(self):
        """大写 True - 应正确识别"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('True') is True
        assert evaluator._to_bool('TRUE') is True
        assert evaluator._to_bool('  True  ') is True

    def test_markdown_code_block(self):
        """markdown 代码块包裹的值 - 应正确提取"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool('```true```') is True
        assert evaluator._to_bool('```json\ntrue\n```') is True
        assert evaluator._to_bool('```\nfalse\n```') is False

    def test_dict_input(self):
        """字典类型输入 - 应提取常见字段"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool({"result": True}) is True
        assert evaluator._to_bool({"result": False}) is False
        assert evaluator._to_bool({"answer": True}) is True
        assert evaluator._to_bool({"status": True}) is True
        assert evaluator._to_bool({"msg": "ok"}) is False

    def test_list_input(self):
        """列表类型输入 - 应取第一个元素"""
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_bool([True]) is True
        assert evaluator._to_bool(["true"]) is True
        assert evaluator._to_bool([]) is False


class TestToLabel:
    """测试 _to_label 方法"""

    def test_boolean_type(self):
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_label(True, "true", "boolean") is True
        assert evaluator._to_label("false", "true", "boolean") is False

    def test_string_type(self):
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_label("yes", "yes", "string") is True
        assert evaluator._to_label("YES", "yes", "string") is True
        assert evaluator._to_label("no", "yes", "string") is False

    def test_number_type(self):
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_label(1, "1", "number") is True
        assert evaluator._to_label("1.0", "1", "number") is True
        assert evaluator._to_label(0, "1", "number") is False

    def test_number_type_invalid(self):
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_label("abc", "1", "number") is False

    def test_unknown_type_fallback(self):
        evaluator = BinaryClassificationEvaluator()
        assert evaluator._to_label(True, "true", "unknown") is True


class TestEvaluateBatch:
    """测试 evaluate_batch 方法"""

    @pytest.mark.asyncio
    async def test_evaluate_batch_basic(self):
        """批量评测 - 3条数据正常评估"""
        evaluator = BinaryClassificationEvaluator(
            expected_label_config={"source": "dataset_field", "source_config": {"field": "is_valid"}},
            predicted_label_config={"source": "model_output", "source_config": {"field": "prediction"}},
            positive_value="true",
            label_type="boolean",
        )
        dataset = [
            {"data": {"question": "Q1", "is_valid": True}},
            {"data": {"question": "Q2", "is_valid": False}},
            {"data": {"question": "Q3", "is_valid": True}},
        ]
        model_outputs = [
            {"prediction": True, "duration": 1.0},
            {"prediction": True, "duration": 1.0},
            {"prediction": True, "duration": 1.0},
        ]
        results = await evaluator.evaluate_batch(dataset, model_outputs)

        assert len(results) == 3
        assert results[0].is_correct is True
        assert results[1].is_correct is False
        assert results[2].is_correct is True
        for r in results:
            assert r.success is True

    @pytest.mark.asyncio
    async def test_evaluate_batch_with_error(self):
        """批量评测 - 某条数据异常时返回 success=False"""
        evaluator = BinaryClassificationEvaluator()
        dataset = [None]
        model_outputs = [{}]
        results = await evaluator.evaluate_batch(dataset, model_outputs)

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error != ""

    @pytest.mark.asyncio
    async def test_evaluate_batch_string_labels(self):
        """批量评测 - string 类型标签"""
        evaluator = BinaryClassificationEvaluator(
            expected_label_config={"source": "dataset_field", "source_config": {"field": "label"}},
            predicted_label_config={"source": "model_output", "source_config": {"field": "pred"}},
            positive_value="valid",
            label_type="string",
        )
        dataset = [
            {"data": {"question": "Q1", "label": "valid"}},
            {"data": {"question": "Q2", "label": "invalid"}},
        ]
        model_outputs = [
            {"pred": "valid"},
            {"pred": "valid"},
        ]
        results = await evaluator.evaluate_batch(dataset, model_outputs)

        assert results[0].is_correct is True
        assert results[1].is_correct is False


class TestBinaryClassificationResult:
    """测试 BinaryClassificationResult dataclass"""

    def test_result_creation(self):
        result = BinaryClassificationResult(
            index=0,
            expected_label=True,
            predicted_label=True,
            is_correct=True,
            duration=1.5,
            success=True,
            error="",
            input_data={"question": "test question"},
        )
        assert result.index == 0
        assert result.input_data == {"question": "test question"}
        assert result.expected_label is True
        assert result.predicted_label is True
        assert result.is_correct is True
        assert result.duration == 1.5
        assert result.success is True
        assert result.error == ""

    def test_result_default_values(self):
        result = BinaryClassificationResult(
            index=0,
            expected_label=False,
            predicted_label=False,
            is_correct=False
        )
        assert result.duration == 0.0
        assert result.success is True
        assert result.error == ""
        assert result.input_data == {}
