"""
评测目标 chain_result 变量注入单元测试

测试场景：evaluation_targets 配置中 source 为 chain_result 的变量
能否正确注入到裁判的系统提示词中
"""
import pytest
from app.services.prompt_builder import PromptBuilder
from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget


class MockTask:
    """Mock task object"""

    def __init__(self):
        self.judge_model = "test-judge-model"
        self.judge_count = 1
        self.max_score = 10
        self.evaluation_metrics = []
        self.model_config = {}
        self.model_provider = None
        self.runtime_variables = {}
        self.variable_mappings = {}
        self.variable_sources = {}
        self.variable_types = {}
        self.enable_user_import = False
        self.user_import_template = None
        self.judge_variable_config = None
        self.evaluation_targets = None
        self.judge_prompt = ""
        self.evaluation_scene = ""
        self.scene_desc = "测试场景"
        self.score_details = ""
        self.steps = []
        self.input_field = "user_input"
        self.reference_field = "answer"


class TestEvaluationTargetsChainResult:
    """评测目标 chain_result 注入测试"""

    def test_resolve_chain_result_target(self):
        """测试 chain_result 类型的评测目标解析

        注意：chain_index 是 1-based，chain_index=1 对应 chain_results[0]
        """
        # 模拟 chain_results
        chain_results = [
            {
                "order": 1,
                "name": "请求1",
                "alias": "req1",
                "content": "请求1的响应内容",
                "variables": {"output": "请求1的输出变量"}
            },
            {
                "order": 2,
                "name": "请求2",
                "alias": "req2",
                "content": "这是一篇生成的文章内容",
                "variables": {"content": "这是一篇生成的文章内容"}
            }
        ]

        # 模拟评测目标配置 - chain_index=2 表示第二个请求（1-based）
        evaluation_targets = [
            {
                "id": "target_1",
                "name": "新评测对象",
                "display_name": "文章",
                "source": "chain_result",
                "source_config": {
                    "var_names": ["content"],
                    "chain_index": 2  # 1-based，对应 chain_results[1]（第二个请求）
                },
                "required": True
            }
        ]

        # 创建 resolver 并解析目标
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={},
            model_result={"content": "模型输出"},
            chain_results=chain_results
        )

        targets = [EvaluationTarget(**t) for t in evaluation_targets]
        resolved = resolver.resolve_targets(targets)

        # 验证解析结果
        assert len(resolved) == 1
        target_id = "target_1"
        assert target_id in resolved
        assert resolved[target_id]["name"] == "文章"
        assert resolved[target_id]["content"] == "文章：这是一篇生成的文章内容"
        assert not resolved[target_id]["is_empty"]

    def test_inject_chain_result_into_system_prompt(self):
        """测试 chain_result 评测目标注入到系统提示词

        注意：chain_index 是 1-based，chain_index=2 对应 chain_results[1]
        """
        # 模拟 chain_results
        chain_results = [
            {
                "order": 1,
                "name": "请求1",
                "alias": "req1",
                "content": "请求1的响应内容",
                "variables": {"output": "请求1的输出变量"}
            },
            {
                "order": 2,
                "name": "请求2",
                "alias": "req2",
                "content": "这是一篇生成的文章内容",
                "variables": {"content": "这是一篇生成的文章内容"}
            }
        ]

        # 创建 task 并设置 evaluation_targets
        task = MockTask()
        task.evaluation_targets = [
            {
                "id": "target_1",
                "name": "新评测对象",
                "display_name": "文章",
                "source": "chain_result",
                "source_config": {
                    "var_names": ["content"],
                    "chain_index": 2  # 1-based，对应 chain_results[1]
                },
                "required": True
            }
        ]

        # 创建 PromptBuilder
        prompt_builder = PromptBuilder(task)

        # 构建 system prompt
        system_prompt = prompt_builder._build_system_prompt(
            user_input="测试问题",
            ref_answer="参考答案",
            model_output="模型输出",
            dataset_item={},
            payload={"_dataset_item": {}},
            chain_results=chain_results,
        )

        # 验证 system_prompt 包含评测目标内容
        assert "待评测目标内容" in system_prompt
        assert "文章" in system_prompt
        assert "这是一篇生成的文章内容" in system_prompt

    def test_chain_result_with_empty_chain_results(self):
        """测试 chain_results 为空时的处理"""
        evaluation_targets = [
            {
                "id": "target_1",
                "name": "新评测对象",
                "display_name": "文章",
                "source": "chain_result",
                "source_config": {
                    "var_names": ["content"],
                    "chain_index": 1
                },
                "required": True
            }
        ]

        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={},
            model_result={"content": "模型输出"},
            chain_results=[]  # 空的 chain_results
        )

        targets = [EvaluationTarget(**t) for t in evaluation_targets]
        resolved = resolver.resolve_targets(targets)

        # 验证空 chain_results 时 content 为空
        assert resolved["target_1"]["is_empty"]

    def test_chain_result_index_out_of_range(self):
        """测试 chain_index 超出范围时的处理"""
        chain_results = [
            {
                "order": 1,
                "name": "请求1",
                "content": "内容",
                "variables": {"output": "输出"}
            }
        ]

        evaluation_targets = [
            {
                "id": "target_1",
                "name": "新评测对象",
                "display_name": "文章",
                "source": "chain_result",
                "source_config": {
                    "var_names": ["content"],
                    "chain_index": 5  # 超出范围
                },
                "required": True
            }
        ]

        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={},
            model_result={"content": "模型输出"},
            chain_results=chain_results
        )

        targets = [EvaluationTarget(**t) for t in evaluation_targets]
        resolved = resolver.resolve_targets(targets)

        # 验证超出范围时 content 为空
        assert resolved["target_1"]["is_empty"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
