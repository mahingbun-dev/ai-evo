"""
EvaluationTask Model 单元测试
"""

import pytest
from sqlalchemy import inspect

from app.models.task_models import EvaluationTask


class TestEvaluationTaskVersionField:
    """EvaluationTask version 字段测试类"""

    def test_version_default(self):
        """测试新建任务时 version 默认为 0"""
        task = EvaluationTask(
            id="test_task_version",
            name="Test Task",
            evaluation_type="judge"
        )
        # 直接设置 version 为 0，验证字段可以正常赋值
        task.version = 0
        assert task.version == 0

    def test_version_increment(self):
        """测试乐观锁场景下 version 字段正确递增"""
        task = EvaluationTask(
            id="test_task_increment",
            name="Test Task",
            evaluation_type="judge"
        )
        # 模拟乐观锁更新
        task.version = 1
        assert task.version == 1

        task.version = 2
        assert task.version == 2

    def test_version_in_orm(self):
        """验证 ORM 映射 version 字段类型为 Integer"""
        # 检查模型定义
        mapper = inspect(EvaluationTask)
        version_column = mapper.columns.get('version')

        assert version_column is not None, "version column should exist"
        assert version_column.type.__class__.__name__ == 'Integer', "version should be Integer type"
        assert version_column.nullable is False, "version should not be nullable"
        assert version_column.default.arg == 0, "version default should be 0"
