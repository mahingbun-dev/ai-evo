"""
Task 1 单元测试：数据模型扩展验证
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.session import Base
from app.models.task_models import EvaluationTask, TaskExecutionRecord


@pytest.fixture
def db_session():
    """创建测试用内存数据库会话"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


class TestEvaluationTaskNewFields:
    """测试 EvaluationTask 新增字段"""

    def test_evaluation_task_has_evaluation_targets_field(self, db_session):
        """测试 evaluation_targets 字段可正常写入 JSON 数据"""
        task = EvaluationTask(
            id="test-task-001",
            name="测试任务",
            evaluation_type="judge",
            evaluation_targets=[
                {"id": "target1", "name": "模型输出", "source": "model_output"}
            ]
        )
        db_session.add(task)
        db_session.commit()

        saved_task = db_session.query(EvaluationTask).filter_by(id="test-task-001").first()
        assert saved_task.evaluation_targets is not None
        assert len(saved_task.evaluation_targets) == 1
        assert saved_task.evaluation_targets[0]["id"] == "target1"

    def test_evaluation_task_has_validation_enabled_field(self, db_session):
        """测试 validation_enabled 字段默认为 True"""
        task = EvaluationTask(
            id="test-task-002",
            name="测试任务",
            evaluation_type="judge"
        )
        db_session.add(task)
        db_session.commit()

        saved_task = db_session.query(EvaluationTask).filter_by(id="test-task-002").first()
        assert saved_task.validation_enabled is True

    def test_validation_enabled_can_be_set_to_false(self, db_session):
        """测试 validation_enabled 可设置为 False"""
        task = EvaluationTask(
            id="test-task-003",
            name="测试任务",
            evaluation_type="judge",
            validation_enabled=False
        )
        db_session.add(task)
        db_session.commit()

        saved_task = db_session.query(EvaluationTask).filter_by(id="test-task-003").first()
        assert saved_task.validation_enabled is False


class TestTaskExecutionRecordNewFields:
    """测试 TaskExecutionRecord 新增字段"""

    def test_task_execution_record_has_judge_index_fields(self, db_session):
        """测试 TaskExecutionRecord 可写入 judge_index、evaluation_target_id、validation_message 字段"""
        record = TaskExecutionRecord(
            id="test-record-001",
            task_id="test-task-001",
            execution_number=1,
            status="running",
            judge_index=0,
            evaluation_target_id="target1",
            evaluation_target_name="模型输出",
            validation_message="验证通过"
        )
        db_session.add(record)
        db_session.commit()

        saved_record = db_session.query(TaskExecutionRecord).filter_by(id="test-record-001").first()
        assert saved_record.judge_index == 0
        assert saved_record.evaluation_target_id == "target1"
        assert saved_record.evaluation_target_name == "模型输出"
        assert saved_record.validation_message == "验证通过"

    def test_backward_compatibility(self, db_session):
        """测试不传新增字段时不影响现有字段读写"""
        task = EvaluationTask(
            id="test-task-010",
            name="测试任务",
            evaluation_type="judge",
            judge_model="gpt-4",
            judge_prompt="请评测这段文本"
        )
        db_session.add(task)
        db_session.commit()

        saved_task = db_session.query(EvaluationTask).filter_by(id="test-task-010").first()
        assert saved_task.judge_model == "gpt-4"
        assert saved_task.judge_prompt == "请评测这段文本"
        assert saved_task.evaluation_targets is None
        assert saved_task.validation_enabled is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])