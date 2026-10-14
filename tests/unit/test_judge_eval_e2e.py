"""裁判评测端到端测试：配置传递、prompt 构建、评分解析"""
import pytest
from types import SimpleNamespace
from app.services.prompt_builder import PromptBuilder
from app.services.judge_client import JudgeClient

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestJudgeEvalE2E:
    """裁判评测端到端测试"""

    def test_list_format_config_passes_variables(self):
        """列表格式 judge_variable_config 同时传递 model_output 和 dataset_field"""
        config = {
            "judge_variable_config": [
                {"key": "model_out", "name": "模型输出", "source": "model_output", "prompt_type": "user"},
                {"key": "user_input", "name": "用户输入", "source": "dataset_field", "prompt_type": "user",
                 "source_config": {"field": "user_input"}},
            ],
        }
        pb = PromptBuilder.from_config(config)
        user_prompt = pb.build_user_prompt_from_judge_vars(
            payload={"user_input": "请写一个快排"},
            model_output="def quicksort(arr): ...",
        )
        assert "模型输出" in user_prompt
        assert "def quicksort(arr): ..." in user_prompt
        assert "用户输入" in user_prompt
        assert "请写一个快排" in user_prompt

    def test_dict_format_config_still_works(self):
        """字典格式 judge_variable_config 无回归"""
        config = {
            "judge_variable_config": {
                "model_out": {
                    "name": "模型输出",
                    "source": "model_output",
                    "prompt_type": "user",
                },
            },
        }
        pb = PromptBuilder.from_config(config)
        user_prompt = pb.build_user_prompt_from_judge_vars(
            payload={},
            model_output="字典格式输出",
        )
        assert "模型输出" in user_prompt
        assert "字典格式输出" in user_prompt

    def test_system_prompt_contains_metrics(self):
        """system prompt 包含 evaluation_metrics 维度和权重"""
        config = {
            "scene_desc": "代码质量评测",
            "evaluation_metrics": [
                {"name": "准确性", "weight": 2, "description": "代码逻辑是否正确"},
                {"name": "完整性", "weight": 1, "description": "是否覆盖所有需求"},
            ],
            "max_score": 10,
        }
        pb = PromptBuilder.from_config(config)
        system_prompt = pb._build_system_prompt(payload={}, model_output="", chain_results=[])
        assert "准确性" in system_prompt
        assert "权重: 2" in system_prompt
        assert "完整性" in system_prompt
        assert "权重: 1" in system_prompt
        assert "评测维度" in system_prompt

    def _make_task(self, evaluation_metrics):
        """创建包含 JudgeClient 所需属性的 mock task"""
        return SimpleNamespace(
            judge_model=None,
            judge_prompt="",
            max_score=10,
            evaluation_metrics=evaluation_metrics,
        )

    def test_judge_response_dimension_scores_parsing(self):
        """裁判返回扁平 dimension_scores，正确提取维度评分"""
        task = self._make_task([
            {"name": "准确性", "weight": 2},
            {"name": "完整性", "weight": 1},
        ])
        client = JudgeClient(task)
        data = {"score": 8.5, "dimension_scores": {"准确性": 9, "完整性": 8}}
        result = client._extract_dimension_scores_from_json(data)
        assert result == {"准确性": 9.0, "完整性": 8.0}

    def test_dimension_scores_nested_format(self):
        """裁判返回嵌套格式 dimension_scores，正确提取"""
        task = self._make_task([
            {"name": "准确性", "weight": 2},
            {"name": "完整性", "weight": 1},
        ])
        client = JudgeClient(task)
        data = {
            "dimension_scores": {
                "准确性": {"score": 9, "reason": "逻辑正确"},
                "完整性": {"score": 8, "reason": "基本覆盖"},
            }
        }
        result = client._extract_dimension_scores_from_json(data)
        assert result == {"准确性": 9.0, "完整性": 8.0}

    def test_e2e_payload_to_prompt(self):
        """端到端: payload → judge_variable_config 列表格式 → system/user prompt 完整链路"""
        config = {
            "scene_desc": "测试用例质量评测",
            "evaluation_metrics": [
                {"name": "准确性", "weight": 2, "description": "测试用例是否准确"},
                {"name": "覆盖率", "weight": 1, "description": "是否覆盖关键路径"},
            ],
            "max_score": 10,
            "judge_variable_config": [
                {"key": "model_out", "name": "模型输出", "source": "model_output", "prompt_type": "user"},
                {"key": "user_input", "name": "用户输入", "source": "dataset_field", "prompt_type": "user",
                 "source_config": {"field": "user_input"}},
            ],
        }
        pb = PromptBuilder.from_config(config)

        system_prompt = pb._build_system_prompt(payload={}, model_output="", chain_results=[])
        assert "测试用例质量评测" in system_prompt
        assert "准确性" in system_prompt
        assert "权重: 2" in system_prompt
        assert "覆盖率" in system_prompt
        assert "权重: 1" in system_prompt

        user_prompt = pb.build_user_prompt_from_judge_vars(
            payload={"user_input": "登录功能测试"},
            model_output="test_login_success: PASSED\ntest_login_fail: PASSED",
        )
        assert "模型输出" in user_prompt
        assert "test_login_success: PASSED" in user_prompt
        assert "用户输入" in user_prompt
        assert "登录功能测试" in user_prompt
