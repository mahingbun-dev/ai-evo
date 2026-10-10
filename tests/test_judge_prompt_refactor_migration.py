"""
Task 5 单元测试：数据库迁移验证
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect
from app.db.session import Base
from app.models.task_models import EvaluationTask, TaskExecutionRecord


@pytest.fixture
def inspector():
    """创建数据库检查器"""
    from app.core.config import get_settings
    settings = get_settings()
    engine = create_engine(settings.DATABASE_URL)
    inspector = inspect(engine)
    return inspector


class TestMigration:
    """测试数据库迁移"""

    def test_migration_adds_evaluation_targets_column(self, inspector):
        """测试迁移后 evaluation_tasks 表包含 evaluation_targets 列"""
        columns = [col['name'] for col in inspector.get_columns('evaluation_tasks')]
        assert 'evaluation_targets' in columns, f"evaluation_targets 列不存在，现有列: {columns}"

    def test_migration_adds_validation_enabled_column(self, inspector):
        """测试迁移后 evaluation_tasks 表包含 validation_enabled 列"""
        columns = [col['name'] for col in inspector.get_columns('evaluation_tasks')]
        assert 'validation_enabled' in columns, f"validation_enabled 列不存在，现有列: {columns}"

    def test_migration_adds_judge_index_columns(self, inspector):
        """测试迁移后 task_execution_records 表包含新列"""
        columns = [col['name'] for col in inspector.get_columns('task_execution_records')]
        expected_columns = ['judge_index', 'evaluation_target_id', 'evaluation_target_name', 'validation_message']
        for col in expected_columns:
            assert col in columns, f"{col} 列不存在，现有列: {columns}"

    def test_backward_compatibility_after_migration(self, inspector):
        """测试迁移不影响现有字段读写"""
        # 检查 evaluation_tasks 现有字段
        task_columns = [col['name'] for col in inspector.get_columns('evaluation_tasks')]
        assert 'id' in task_columns
        assert 'name' in task_columns
        assert 'evaluation_type' in task_columns
        assert 'judge_model' in task_columns

        # 检查 task_execution_records 现有字段
        record_columns = [col['name'] for col in inspector.get_columns('task_execution_records')]
        assert 'id' in record_columns
        assert 'task_id' in record_columns
        assert 'execution_number' in record_columns


if __name__ == "__main__":
    pytest.main([__file__, "-v"])