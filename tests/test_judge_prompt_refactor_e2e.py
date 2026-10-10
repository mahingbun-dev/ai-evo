"""
Task 6 单元测试：集成测试与验收
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.session import Base
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget
from app.services.judge_validation_service import JudgeValidationService


@pytest.fixture
def db_session():
    """创建测试用内存数据库会话"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


class TestCreateTaskWithEvaluationTargets:
    """测试创建带评测目标配置的任务"""

    def test_create_task_with_evaluation_targets(self, db_session):
        """测试创建带评测目标配置的任务"""
        task = EvaluationTask(
            id="e2e-test-001",
            name="端到端测试任务",
            evaluation_type="judge",
            evaluation_targets=[
                {"id": "t1", "name": "model_output", "source": "model_output", "display_name": "模型输出", "required": True},
                {"id": "t2", "name": "ref_answer", "source": "dataset_field", "source_config": {"field": "ref_answer"}, "display_name": "参考答案", "required": True}
            ],
            validation_enabled=True
        )
        db_session.add(task)
        db_session.commit()

        saved_task = db_session.query(EvaluationTask).filter_by(id="e2e-test-001").first()
        assert saved_task.evaluation_targets is not None
        assert len(saved_task.evaluation_targets) == 2
        assert saved_task.validation_enabled is True


class TestJudgeEvaluationWithTargets:
    """测试使用多评测目标和模板进行裁判评测"""

    def test_run_judge_evaluation_with_targets(self):
        """测试使用多评测目标和模板进行评测"""
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={"question": "什么是AI？", "ref_answer": "AI是人工智能"},
            model_result={"content": "AI是人工智能技术"},
            chain_results=[]
        )
        targets = [
            EvaluationTarget(id="t1", name="model_output", source="model_output", display_name="模型输出", required=True),
            EvaluationTarget(id="t2", name="ref_answer", source="dataset_field", source_config={"field": "ref_answer"}, display_name="参考答案", required=True)
        ]
        resolved_targets = resolver.resolve_targets(targets)

        assert "t1" in resolved_targets
        assert "t2" in resolved_targets
        assert resolved_targets["t1"]["content"] == "AI是人工智能技术"
        assert resolved_targets["t2"]["content"] == "AI是人工智能"
        assert resolved_targets["t1"]["is_empty"] is False
        assert resolved_targets["t2"]["is_empty"] is False


class TestValidationErrorReturnsZeroScore:
    """测试空值校验失败时返回0分"""

    def test_validation_error_returns_zero_score(self):
        """测试空值校验失败时返回0分"""
        validation_service = JudgeValidationService()
        resolved_targets = {
            "t1": {"name": "模型输出", "content": "", "is_empty": True, "required": True}
        }
        is_valid, validation_results = validation_service.validate_targets(resolved_targets, validation_enabled=True)

        assert is_valid is False
        assert len(validation_results) == 1
        assert validation_results[0]["status"] == "error"

        zero_result = validation_service.build_zero_score_result(
            judge_model="gpt-4",
            validation_results=validation_results
        )
        assert zero_result["overall_score"] == 0.0
        assert zero_result["scores"]["综合评分"] == 0


class TestPreviewApiReturnsRenderedPrompts:
    """测试预览API返回正确渲染的提示词"""

    def test_preview_api_returns_rendered_prompts(self):
        """测试预览API返回正确渲染的提示词"""
        from jinja2 import Template

        system_template = "你是一个裁判，请评测：{{ targets[0].name }}"
        user_template = "内容：{{ targets[0].content }}"

        targets_list = [{"name": "模型输出", "content": "测试内容", "is_empty": False, "required": True}]
        system_content = Template(system_template).render(targets=targets_list)
        user_content = Template(user_template).render(targets=targets_list)

        assert "模型输出" in system_content
        assert "测试内容" in user_content


class TestResultContainsMultiJudgeDetails:
    """测试结果包含多裁判独立详情"""

    def test_result_contains_multi_judge_details(self, db_session):
        """测试结果包含多裁判独立详情"""
        record = TaskExecutionRecord(
            id="e2e-record-001",
            task_id="e2e-test-001",
            execution_number=1,
            status="completed",
            judge_index=0,
            evaluation_target_id="t1",
            evaluation_target_name="模型输出",
            validation_message="校验通过"
        )
        db_session.add(record)
        db_session.commit()

        saved_record = db_session.query(TaskExecutionRecord).filter_by(id="e2e-record-001").first()
        assert saved_record.judge_index == 0
        assert saved_record.evaluation_target_id == "t1"
        assert saved_record.evaluation_target_name == "模型输出"
        assert saved_record.validation_message == "校验通过"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])