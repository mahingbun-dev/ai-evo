"""
评测任务数据模型单元测试
"""
import pytest
from app.models.task_models import EvaluationTask


class TestEvaluationTaskLatencyConfig:
    """测试 EvaluationTask 模型的 latency_config 字段"""

    def test_latency_config_field_exists(self):
        """验证 EvaluationTask 模型存在 latency_config 属性且默认值为 None"""
        task = EvaluationTask()
        assert hasattr(task, 'latency_config'), "EvaluationTask should have latency_config attribute"
        assert task.latency_config is None, "latency_config default value should be None"

    def test_latency_config_accepts_json(self):
        """验证 latency_config 字段可接受字典类型值"""
        config_data = {
            "threshold": 10.0,
            "repeat_count": 1,
            "scoring_mode": "threshold"
        }
        task = EvaluationTask(latency_config=config_data)
        assert task.latency_config == config_data
        assert task.latency_config["threshold"] == 10.0
        assert task.latency_config["repeat_count"] == 1
        assert task.latency_config["scoring_mode"] == "threshold"

    def test_latency_config_accepts_empty_dict(self):
        """验证 latency_config 字段可接受空字典"""
        task = EvaluationTask(latency_config={})
        assert task.latency_config == {}

    def test_latency_config_is_json_type(self):
        """验证 latency_config 字段可接受复杂的 JSON 结构"""
        complex_config = {
            "threshold": 5.0,
            "repeat_count": 3,
            "scoring_mode": "average",
            "enable_percentile": True,
            "percentile_values": [50, 90, 95, 99],
            "timeout_seconds": 30
        }
        task = EvaluationTask(latency_config=complex_config)
        assert task.latency_config["enable_percentile"] is True
        assert task.latency_config["percentile_values"] == [50, 90, 95, 99]
