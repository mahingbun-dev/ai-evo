"""
测试 build_judge_payload 修复

验证当 judge_variable_config 为 list 格式时，build_judge_payload 能正确保留原始 payload 字段
"""
import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestBuildJudgePayloadFix:
    """测试 build_judge_payload 修复"""

    def setup_method(self):
        """设置测试环境"""
        from app.services.variable_injector import VariableInjector

        # 创建 mock task
        self.task = MagicMock()
        self.task.runtime_variables = {}
        self.task.variable_mappings = {}
        self.task.variable_sources = {}
        self.task.enable_user_import = False
        self.task.user_import_template = None
        self.task.input_field = "user_input"
        self.task.reference_field = "answer"

        self.variable_injector = VariableInjector(self.task)

    def test_list_format_preserves_original_payload_fields(self):
        """测试 list 格式的 judge_variable_config 保留原始 payload 字段"""
        # 设置 list 格式的 judge_variable_config
        self.task.judge_variable_config = [
            {
                "key": "user_input",
                "name": "用户输入",
                "source": "dataset_field",
                "source_config": {"field": "question"},
                "prompt_type": "user"
            },
            {
                "key": "model_output",
                "name": "模型输出",
                "source": "model_output",
                "prompt_type": "user"
            }
        ]

        # 原始 payload 包含 user_input，数据集中也有 question
        original_payload = {
            "user_input": "这是一个测试问题",
            "ref_answer": "这是一个参考答案",
            "_dataset_item": {"question": "数据集中的问题"}
        }

        model_output = "这是模型输出"

        # 调用 build_judge_payload
        result = self.variable_injector.build_judge_payload(original_payload, model_output)

        # 验证结果
        # user_input 应该从数据集中解析（因为配置了 source=dataset_field, field=question）
        assert "user_input" in result, "user_input 应该在结果中"
        assert result["user_input"] == "数据集中的问题", "user_input 应该从数据集解析"
        # ref_answer 保留原始值（因为没有在 judge_variable_config 中配置）
        assert "ref_answer" in result, "ref_answer 应该在结果中"
        assert result["ref_answer"] == "这是一个参考答案", "ref_answer 应该保留原始值"
        assert result["model_answer"] == model_output, "model_answer 应该是模型输出"

    def test_dict_format_preserves_original_payload_fields(self):
        """测试 dict 格式的 judge_variable_config 保留原始 payload 字段"""
        # 设置 dict 格式的 judge_variable_config
        self.task.judge_variable_config = {
            "user_input": {
                "name": "用户输入",
                "source": "dataset_field",
                "source_config": {"field": "question"},
                "prompt_type": "user"
            },
            "model_output": {
                "name": "模型输出",
                "source": "model_output",
                "prompt_type": "user"
            }
        }

        original_payload = {
            "user_input": "这是一个测试问题",
            "ref_answer": "这是一个参考答案",
            "_dataset_item": {}
        }

        model_output = "这是模型输出"

        result = self.variable_injector.build_judge_payload(original_payload, model_output)

        assert "user_input" in result
        assert result["user_input"] == "这是一个测试问题"
        assert "ref_answer" in result
        assert result["ref_answer"] == "这是一个参考答案"

    def test_empty_config_preserves_original_payload(self):
        """测试空的 judge_variable_config 保留原始 payload"""
        self.task.judge_variable_config = None

        original_payload = {
            "user_input": "测试问题",
            "ref_answer": "参考答案",
            "_dataset_item": {}
        }

        model_output = "模型输出"

        result = self.variable_injector.build_judge_payload(original_payload, model_output)

        assert result["user_input"] == "测试问题"
        assert result["ref_answer"] == "参考答案"
        assert result["model_answer"] == "模型输出"

    def test_dataset_field_resolved_from_dataset_item(self):
        """测试 dataset_field 类型变量从数据集解析"""
        self.task.judge_variable_config = [
            {
                "key": "user_input",
                "name": "用户输入",
                "source": "dataset_field",
                "source_config": {"field": "question"},
                "prompt_type": "user"
            }
        ]

        original_payload = {
            "user_input": "",  # 原始 payload 中为空
            "_dataset_item": {"question": "数据集中的问题"}
        }

        model_output = "模型输出"

        result = self.variable_injector.build_judge_payload(original_payload, model_output)

        # 应该从数据集中解析到值
        assert result["user_input"] == "数据集中的问题"

    def test_chain_result_source_with_empty_chain_results(self):
        """测试 chain_result 类型变量在 chain_results 为空时使用原始 payload 回退"""
        self.variable_injector.chain_results = []

        self.task.judge_variable_config = [
            {
                "key": "user_input",
                "name": "用户输入",
                "source": "chain_result",
                "source_config": {"chain_index": 0, "var_names": ["user_input"]},
                "prompt_type": "user"
            }
        ]

        original_payload = {
            "user_input": "原始 payload 中的用户输入",
            "_dataset_item": {}
        }

        model_output = "模型输出"

        result = self.variable_injector.build_judge_payload(original_payload, model_output)

        # chain_result 解析失败，应该回退到原始 payload
        assert result["user_input"] == "原始 payload 中的用户输入"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
