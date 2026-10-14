"""
测试 runtime_variables 中 {value, type} 格式的处理

验证评测执行器正确处理前端传入的 {value, type} 格式变量，
确保转换为实际值后传递给模型调用器。
"""
import unittest
from app.services.variable_injector import VariableInjector

import pytest
pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class MockTask:
    """模拟任务对象"""
    def __init__(self, runtime_variables=None, variable_mappings=None, variable_sources=None,
                 enable_user_import=False, user_import_template=None):
        self.runtime_variables = runtime_variables or {}
        self.variable_mappings = variable_mappings or {}
        self.variable_sources = variable_sources or {}
        self.enable_user_import = enable_user_import
        self.user_import_template = user_import_template
        self.input_field = "user_input"
        self.reference_field = "answer"


class TestRuntimeVariablesProcessing(unittest.TestCase):
    """测试 {value, type} 格式的处理"""

    def test_number_type_conversion(self):
        """测试 number 类型转换"""
        task = MockTask(runtime_variables={
            "temperature": {"value": "0.7", "type": "number"},
            "max_tokens": {"value": "100", "type": "number"},
        })
        injector = VariableInjector(task)
        payload = injector.generate_payload({
            "question": "test question",
            "answer": "test answer",
        })

        # 验证转换后的值
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["max_tokens"], 100)
        self.assertIsInstance(payload["temperature"], float)
        self.assertIsInstance(payload["max_tokens"], int)

    def test_boolean_type_conversion(self):
        """测试 boolean 类型转换"""
        task = MockTask(runtime_variables={
            "stream": {"value": "true", "type": "boolean"},
            "verbose": {"value": "false", "type": "boolean"},
        })
        injector = VariableInjector(task)
        payload = injector.generate_payload({
            "question": "test question",
            "answer": "test answer",
        })

        self.assertEqual(payload["stream"], True)
        self.assertEqual(payload["verbose"], False)

    def test_string_type_conversion(self):
        """测试 string 类型转换"""
        task = MockTask(runtime_variables={
            "model": {"value": "gpt-4", "type": "string"},
            "prompt": {"value": "Hello world", "type": "string"},
        })
        injector = VariableInjector(task)
        payload = injector.generate_payload({
            "question": "test question",
            "answer": "test answer",
        })

        self.assertEqual(payload["model"], "gpt-4")
        self.assertEqual(payload["prompt"], "Hello world")

    def test_mixed_types(self):
        """测试混合类型转换"""
        task = MockTask(runtime_variables={
            "temperature": {"value": "0.5", "type": "number"},
            "stream": {"value": "true", "type": "boolean"},
            "model": {"value": "gpt-4", "type": "string"},
            "normal_string": "just a string",  # 非 {value, type} 格式
            "normal_number": 42,  # 非 {value, type} 格式
        })
        injector = VariableInjector(task)
        payload = injector.generate_payload({
            "question": "test question",
            "answer": "test answer",
        })

        self.assertEqual(payload["temperature"], 0.5)
        self.assertEqual(payload["stream"], True)
        self.assertEqual(payload["model"], "gpt-4")
        self.assertEqual(payload["normal_string"], "just a string")
        # 注意：_get_variable_value() 会将非 {value, type} 值转为字符串
        self.assertEqual(payload["normal_number"], "42")

    def test_evaluation_executor_runtime_variables_extraction(self):
        """测试评测执行器提取 runtime_variables 的逻辑"""
        # 模拟 payload（已经过 VariableInjector 处理）
        payload = {
            "user_input": "test question",
            "ref_answer": "test answer",
            "model_answer": None,
            "_dataset_item": {"question": "test"},
            "temperature": 0.7,  # 已转换
            "max_tokens": 100,   # 已转换
            "stream": True,      # 已转换
            "model": "gpt-4",    # 已转换
        }

        # 模拟评测执行器的 runtime_variables 提取逻辑
        runtime_variables = {}
        for key, value in payload.items():
            if key not in ("user_input", "ref_answer", "model_answer", "_dataset_item", "messages", "idx"):
                runtime_variables[key] = value

        # 验证提取的 runtime_variables 不包含 {value, type} 格式
        for key, val in runtime_variables.items():
            self.assertFalse(
                isinstance(val, dict) and 'value' in val and 'type' in val,
                f"变量 {key} 仍为 {{value, type}} 格式: {val}"
            )

    def test_unconverted_value_type_format(self):
        """测试未转换的 {value, type} 格式的处理（模拟边界情况）"""
        # 模拟 runtime_variables 中仍有未转换的 {value, type} 对象
        # （这可能发生在 payload 生成后被修改的情况下）
        runtime_variables = {
            "temperature": {"value": "0.7", "type": "number"},  # 未转换
            "stream": {"value": "true", "type": "boolean"},      # 未转换
            "model": {"value": "gpt-4", "type": "string"},       # 未转换
        }

        # 模拟评测执行器的处理逻辑
        for key, val in list(runtime_variables.items()):
            if isinstance(val, dict) and 'value' in val and 'type' in val:
                var_type = val.get('type', 'string')
                var_value = val.get('value', '')
                if var_type == 'number':
                    try:
                        runtime_variables[key] = float(var_value) if '.' in str(var_value) else int(var_value)
                    except (ValueError, TypeError):
                        runtime_variables[key] = 0
                elif var_type == 'boolean':
                    runtime_variables[key] = str(var_value).lower() in ('true', '1', 'yes')
                else:
                    runtime_variables[key] = str(var_value)

        # 验证转换后的值
        self.assertEqual(runtime_variables["temperature"], 0.7)
        self.assertEqual(runtime_variables["stream"], True)
        self.assertEqual(runtime_variables["model"], "gpt-4")

        # 验证不再包含 {value, type} 格式
        for key, val in runtime_variables.items():
            self.assertFalse(
                isinstance(val, dict) and 'value' in val and 'type' in val,
                f"变量 {key} 仍为 {{value, type}} 格式: {val}"
            )


class TestGetDataJsonFallback(unittest.TestCase):
    """测试 _get_from_data_json 方法的回退逻辑"""

    def test_fallback_when_dataset_item_key_missing(self):
        """当 _dataset_item 键不存在时，应从 dataset_item.data 中获取值"""
        task = MockTask()
        injector = VariableInjector(task)

        # 模拟 V1 格式：没有 _dataset_item 键，数据在 data 字段中
        dataset_item = {
            "data": {
                "user_input": "test question",
                "answer": "test answer",
                "tweetid": "12345"
            }
        }

        # 应该能从 data 中获取到值
        result = injector._get_from_data_json("user_input", dataset_item)
        self.assertEqual(result, "test question")

        result = injector._get_from_data_json("tweetid", dataset_item)
        self.assertEqual(result, "12345")

    def test_v2_format_with_dataset_item(self):
        """当 _dataset_item 键存在且有数据时，应优先从 _dataset_item.data 中获取"""
        task = MockTask()
        injector = VariableInjector(task)

        # 模拟 V2 格式
        dataset_item = {
            "_dataset_item": {
                "data": {
                    "user_input": "v2 question",
                    "answer": "v2 answer"
                }
            },
            "data": {
                "user_input": "v1 question",
                "answer": "v1 answer"
            }
        }

        # 应该优先从 _dataset_item.data 中获取
        result = injector._get_from_data_json("user_input", dataset_item)
        self.assertEqual(result, "v2 question")

    def test_fallback_when_dataset_item_empty(self):
        """当 _dataset_item 存在但为空字典时，应从 dataset_item.data 中获取"""
        task = MockTask()
        injector = VariableInjector(task)

        dataset_item = {
            "_dataset_item": {},  # 空的 _dataset_item
            "data": {
                "user_input": "fallback question",
                "tweetid": "67890"
            }
        }

        result = injector._get_from_data_json("user_input", dataset_item)
        self.assertEqual(result, "fallback question")

        result = injector._get_from_data_json("tweetid", dataset_item)
        self.assertEqual(result, "67890")

    def test_fallback_when_field_not_in_v2_data(self):
        """当字段在 V2 数据中不存在但在 V1 数据中存在时，应回退到 V1 数据"""
        task = MockTask()
        injector = VariableInjector(task)

        dataset_item = {
            "_dataset_item": {
                "data": {
                    "user_input": "v2 question"
                    # tweetid 不在 V2 数据中
                }
            },
            "data": {
                "user_input": "v1 question",
                "tweetid": "12345"
            }
        }

        # tweetid 在 V2 中不存在，应从 V1 的 data 中获取
        result = injector._get_from_data_json("tweetid", dataset_item)
        self.assertEqual(result, "12345")


if __name__ == '__main__':
    unittest.main()
