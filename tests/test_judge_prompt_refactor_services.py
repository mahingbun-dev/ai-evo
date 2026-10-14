"""
Task 2 单元测试：服务层重构验证
"""
import pytest
import sys
import os

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.judge_validation_service import JudgeValidationService
from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget


class TestJudgeValidationService:
    """测试 JudgeValidationService"""

    def test_validate_targets_success(self):
        """测试所有必填目标非空时返回 is_valid=True"""
        service = JudgeValidationService()
        resolved_targets = {
            "target1": {"name": "模型输出", "content": "这是模型输出", "is_empty": False, "required": True},
            "target2": {"name": "参考答案", "content": "这是参考答案", "is_empty": False, "required": True}
        }
        is_valid, validation_results = service.validate_targets(resolved_targets, validation_enabled=True)
        assert is_valid is True
        assert len(validation_results) == 2
        assert all(r["status"] == "ok" for r in validation_results)

    def test_validate_targets_empty_required(self):
        """测试必填目标为空时返回 is_valid=False"""
        service = JudgeValidationService()
        resolved_targets = {
            "target1": {"name": "模型输出", "content": "", "is_empty": True, "required": True}
        }
        is_valid, validation_results = service.validate_targets(resolved_targets, validation_enabled=True)
        assert is_valid is False
        assert len(validation_results) == 1
        assert validation_results[0]["status"] == "error"
        assert "为空" in validation_results[0]["message"]

    def test_build_zero_score_result(self):
        """测试校验失败时返回正确的零分结果结构"""
        service = JudgeValidationService()
        validation_results = [
            {"target_id": "t1", "target_name": "模型输出", "status": "error", "message": "内容为空"}
        ]
        result = service.build_zero_score_result(
            judge_model="gpt-4",
            validation_results=validation_results,
            question="问题",
            ref_answer="参考答案",
            model_answer="模型回答"
        )
        assert result["overall_score"] == 0.0
        assert result["scores"]["综合评分"] == 0
        assert result["validation_message"] == "「模型输出」: 内容为空"


class TestJudgeVariableResolver:
    """测试 JudgeVariableResolver.resolve_targets()"""

    def test_resolve_targets(self):
        """测试解析多个评测目标返回正确的结构和空值状态"""
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={"question": "测试问题"},
            model_result={"content": "模型输出内容"},
            chain_results=[]
        )
        targets = [
            EvaluationTarget(id="t1", name="model_output", source="model_output", display_name="模型输出", required=True),
            EvaluationTarget(id="t2", name="user_input", source="dataset_field", source_config={"field": "question"}, display_name="用户问题", required=True)
        ]
        results = resolver.resolve_targets(targets)

        assert "t1" in results
        assert "t2" in results
        assert results["t1"]["content"] == "模型输出内容"
        assert results["t1"]["is_empty"] is False
        assert results["t1"]["required"] is True
        assert results["t2"]["content"] == "测试问题"
        assert results["t2"]["is_empty"] is False

    def test_resolve_targets_empty(self):
        """测试空内容返回 is_empty=True"""
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={},
            model_result={"content": ""},
            chain_results=[]
        )
        targets = [
            EvaluationTarget(id="t1", name="model_output", source="model_output", display_name="模型输出", required=True)
        ]
        results = resolver.resolve_targets(targets)

        assert results["t1"]["content"] == ""
        assert results["t1"]["is_empty"] is True


class TestBuildPromptWithPlaceholders:
    """测试 Jinja2 模板渲染"""

    def test_build_prompt_with_placeholders(self):
        """测试 Jinja2 模板正确渲染包含 targets 列表的提示词"""
        from app.services.judge_evaluation_engine import JudgeEvaluationEngine

        engine = JudgeEvaluationEngine(
            model_config={},
            judge_config={"model_type": "gpt-4"}
        )

        resolved_targets = {
            "t1": {"name": "模型输出", "content": "这是模型回答", "is_empty": False, "required": True},
            "t2": {"name": "参考答案", "content": "这是参考答案", "is_empty": False, "required": False}
        }

        system_template = "你是一个裁判，请评测以下内容：\n{% for target in targets %}- {{ target.name }}: {{ target.content }}\n{% endfor %}"
        user_template = "{% for target in targets %}{{ target.name }}: {{ target.content }}\n{% endfor %}请评分"

        messages = engine._build_prompt_with_placeholders(resolved_targets, system_template, user_template)

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "模型输出" in messages[0]["content"]
        assert "这是模型回答" in messages[0]["content"]
        assert "请评分" in messages[1]["content"]


class TestEvaluateSingleWithValidation:
    """测试 evaluate_single 方法的空值校验"""

    def test_evaluate_single_with_validation_failure(self):
        """测试配置 validation_enabled=True 且目标为空时返回零分"""
        import asyncio
        from app.services.judge_evaluation_engine import JudgeEvaluationEngine

        async def run_test():
            engine = JudgeEvaluationEngine(
                model_config={},
                judge_config={"model_type": "gpt-4"},
                evaluation_targets=[
                    {"id": "t1", "name": "model_output", "source": "model_output", "display_name": "模型输出", "required": True}
                ],
                validation_enabled=True
            )

            result = await engine.evaluate_single(
                question="问题",
                ref_answer="参考答案",
                model_answer=""  # 空内容
            )

            assert result.overall_score == 0.0
            assert result.validation_message != ""

        asyncio.run(run_test())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])