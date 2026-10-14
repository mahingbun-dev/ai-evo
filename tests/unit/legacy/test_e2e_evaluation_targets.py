"""
端到端测试：evaluation_targets chain_result 注入流程

验证从任务配置到最终 system prompt 构建的完整流程
"""
import pytest
from unittest.mock import MagicMock, patch
from app.services.prompt_builder import PromptBuilder
from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget
from app.services.variable_injector import VariableInjector

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）；名称含 e2e 但 LLM/DB 全 mock，按依赖归 unit


class MockTask:
    """模拟任务对象"""

    def __init__(self):
        self.judge_model = "test-model"
        self.judge_count = 1
        self.max_score = 10
        self.evaluation_metrics = [
            {"name": "质量", "weight": "high", "description": "评估内容质量"}
        ]
        self.model_config = {}
        self.model_provider = None
        self.runtime_variables = {}
        self.variable_mappings = {}
        self.variable_sources = {}
        self.variable_types = {}
        self.enable_user_import = False
        self.user_import_template = None
        self.judge_variable_config = {
            "user_input": {
                "name": "用户输入",
                "source": "dataset_field",
                "source_config": {"field": "user_input"},
                "prompt_type": "user"
            }
        }
        self.evaluation_targets = [
            {
                "id": "target_1",
                "name": "评测目标",
                "display_name": "文章",
                "source": "chain_result",
                "source_config": {
                    "var_names": ["content"],
                    "chain_index": 2  # 1-based，第二个请求
                },
                "required": True
            }
        ]
        self.judge_prompt = ""
        self.evaluation_scene = "质量评测"
        self.scene_desc = "评估生成内容的质量"
        self.score_details = "9-10分：优秀"
        self.steps = ["分析内容", "给出评分"]
        self.input_field = "user_input"
        self.reference_field = "answer"


class TestE2EEvaluationTargets:
    """端到端测试"""

    def test_full_flow_chain_result_injection(self):
        """测试完整的 chain_result 注入流程"""
        # 模拟请求链执行结果
        chain_results = [
            {
                "order": 1,
                "name": "预处理",
                "alias": "preprocess",
                "content": "预处理结果",
                "variables": {"result": "预处理结果"}
            },
            {
                "order": 2,
                "name": "生成文章",
                "alias": "generate",
                "content": "这是生成的文章内容，包含详细的信息和分析。",
                "variables": {"content": "这是生成的文章内容，包含详细的信息和分析。"}
            }
        ]

        # 模拟模型输出
        model_output = "模型的最终输出"

        # 模拟 payload
        payload = {
            "user_input": "请生成一篇文章",
            "ref_answer": "参考答案内容",
            "_dataset_item": {}
        }

        # 创建任务对象
        task = MockTask()

        # ========== 步骤1: PromptBuilder 构建 system prompt ==========
        prompt_builder = PromptBuilder(task)

        system_prompt = prompt_builder._build_system_prompt(
            user_input=payload.get("user_input", ""),
            ref_answer=payload.get("ref_answer", ""),
            model_output=model_output,
            dataset_item=payload.get("_dataset_item", {}),
            payload=payload,
            chain_results=chain_results,
        )

        # 验证 system prompt 包含评测目标内容
        assert "待评测目标内容" in system_prompt, "system prompt 应包含 '待评测目标内容'"
        assert "文章" in system_prompt, "system prompt 应包含 '文章'"
        assert "这是生成的文章内容" in system_prompt, "system prompt 应包含实际的文章内容"

        # ========== 步骤2: VariableInjector 构建 judge payload ==========
        variable_injector = VariableInjector(task, chain_results=chain_results)
        judge_payload = variable_injector.build_judge_payload(payload, model_output)

        # 验证 judge_payload 包含必要字段
        assert "user_input" in judge_payload
        assert "model_answer" in judge_payload

        # ========== 步骤3: PromptBuilder 构建 user prompt ==========
        user_prompt = prompt_builder.build_user_prompt_from_judge_vars(
            payload=judge_payload,
            model_output=model_output,
            chain_results=chain_results,
        )

        # 验证 user prompt 包含用户输入
        assert "请生成一篇文章" in user_prompt or "用户输入" in user_prompt

    def test_chain_index_boundary(self):
        """测试 chain_index 边界情况"""
        chain_results = [
            {"order": 1, "name": "请求1", "content": "内容1", "variables": {"content": "内容1"}}
        ]

        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={},
            model_result={"content": "输出"},
            chain_results=chain_results
        )

        # chain_index=1 (1-based) -> chain_results[0] -> 有内容
        target1 = EvaluationTarget(
            id="t1", name="测试1", display_name="文章1",
            source="chain_result",
            source_config={"var_names": ["content"], "chain_index": 1},
            required=True
        )
        result1 = resolver._resolve_single_target(target1)
        assert "内容1" in result1, f"chain_index=1 应该找到内容，实际: {result1}"

        # chain_index=2 (1-based) -> chain_results[1] -> 越界
        target2 = EvaluationTarget(
            id="t2", name="测试2", display_name="文章2",
            source="chain_result",
            source_config={"var_names": ["content"], "chain_index": 2},
            required=True
        )
        result2 = resolver._resolve_single_target(target2)
        assert result2 == "", f"chain_index=2 应该返回空，实际: {result2}"

        # chain_index=0 (1-based，无效) -> chain_results[-1] -> 负数索引
        target3 = EvaluationTarget(
            id="t3", name="测试3", display_name="文章3",
            source="chain_result",
            source_config={"var_names": ["content"], "chain_index": 0},
            required=True
        )
        result3 = resolver._resolve_single_target(target3)
        assert result3 == "", f"chain_index=0 应该返回空，实际: {result3}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
