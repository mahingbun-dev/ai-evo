"""
Task 6 验收测试：功能验收确认
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


class TestAcceptanceMultiTargetEvaluation:
    """验收测试：多评测目标配置→执行→结果完整流程"""

    def test_acceptance_multi_target_evaluation(self, db_session):
        """多评测目标配置→执行→结果完整流程"""
        # 1. 创建带评测目标配置的任务
        task = EvaluationTask(
            id="acceptance-001",
            name="验收测试任务",
            evaluation_type="judge",
            evaluation_targets=[
                {"id": "t1", "name": "model_output", "source": "model_output", "display_name": "模型输出", "required": True},
                {"id": "t2", "name": "ref_answer", "source": "dataset_field", "source_config": {"field": "ref_answer"}, "display_name": "参考答案", "required": True}
            ],
            validation_enabled=True
        )
        db_session.add(task)
        db_session.commit()

        # 2. 解析评测目标
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={"question": "问题", "ref_answer": "参考答案"},
            model_result={"content": "模型输出"},
            chain_results=[]
        )
        targets = [EvaluationTarget(**t) for t in task.evaluation_targets]
        resolved = resolver.resolve_targets(targets)

        # 3. 验证解析结果
        assert "t1" in resolved
        assert "t2" in resolved
        assert resolved["t1"]["content"] == "模型输出"
        assert resolved["t2"]["content"] == "参考答案"

        # 4. 保存执行记录
        record = TaskExecutionRecord(
            id="acc-record-001",
            task_id="acceptance-001",
            execution_number=1,
            status="completed",
            judge_index=0,
            evaluation_target_id="t1",
            evaluation_target_name="模型输出"
        )
        db_session.add(record)
        db_session.commit()

        # 5. 验证结果
        saved_task = db_session.query(EvaluationTask).filter_by(id="acceptance-001").first()
        assert saved_task.evaluation_targets is not None
        assert len(saved_task.evaluation_targets) == 2


class TestAcceptanceValidationAndZeroScore:
    """验收测试：空值校验失败→零分→结果保存完整信息"""

    def test_acceptance_validation_and_zero_score(self):
        """空值校验失败→零分→结果保存完整信息"""
        # 1. 解析目标（空内容）
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={},
            model_result={"content": ""},  # 空内容
            chain_results=[]
        )
        targets = [EvaluationTarget(id="t1", name="model_output", source="model_output", display_name="模型输出", required=True)]
        resolved = resolver.resolve_targets(targets)

        # 2. 空值校验
        validation_service = JudgeValidationService()
        is_valid, validation_results = validation_service.validate_targets(resolved, validation_enabled=True)

        # 3. 验证校验失败
        assert is_valid is False
        assert "为空" in validation_results[0]["message"]

        # 4. 构建零分结果
        zero_result = validation_service.build_zero_score_result(
            judge_model="gpt-4",
            validation_results=validation_results,
            question="问题",
            ref_answer="参考答案",
            model_answer=""
        )

        # 5. 验证零分结果
        assert zero_result["overall_score"] == 0.0
        assert zero_result["scores"]["综合评分"] == 0
        assert "空值校验失败" in zero_result["reasoning"]


class TestAcceptanceTemplatePreview:
    """验收测试：配置模板→预览→渲染结果正确"""

    def test_acceptance_template_preview(self):
        """配置模板→预览→渲染结果正确"""
        from jinja2 import Template

        # 1. 配置模板
        system_template = "你是一个裁判，请根据以下信息评分：{% for t in targets %}\n{{ t.name }}: {{ t.content }}{% endfor %}"
        user_template = "{% for t in targets %}\n{{ t.name }}: {{ t.content }}{% endfor %}\n请返回评分。"

        # 2. 解析目标
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={"question": "问题"},
            model_result={"content": "模型回答内容"},
            chain_results=[]
        )
        targets = [
            EvaluationTarget(id="t1", name="模型输出", source="model_output", display_name="模型输出", required=True),
            EvaluationTarget(id="t2", name="参考答案", source="dataset_field", source_config={"field": "question"}, display_name="用户问题", required=False)
        ]
        resolved_targets = resolver.resolve_targets(targets)

        # 3. 渲染模板
        targets_list = list(resolved_targets.values())
        system_content = Template(system_template).render(targets=targets_list)
        user_content = Template(user_template).render(targets=targets_list)

        # 4. 验证渲染结果
        assert "模型输出" in system_content
        assert "模型回答内容" in system_content
        # 注意：第二个target的source是dataset_field，field是question，所以内容是"问题"而非"参考答案"
        assert "用户问题" in user_content
        assert "问题" in user_content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])