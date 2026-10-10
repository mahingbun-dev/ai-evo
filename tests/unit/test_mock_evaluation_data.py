"""
Mock数据验证测试

用于验证四种评测类型的mock数据：
1. 一致性评测（需要裁判介入）
2. 一致性评测（不需要裁判介入）
3. 成功率评测
4. AI裁判员评测

Mock内容：模型配置、数据集、评测任务配置
"""
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass


# ============================================================
# Mock 数据定义
# ============================================================

@dataclass
class MockModelConfig:
    """Mock模型配置"""
    id: str = "model-001"
    name: str = "测试模型"
    provider: str = "custom_http"
    model_type: str = "gpt-4"
    model_role: str = "evaluation"
    endpoint: str = "https://api.test.com/v1/chat/completions"
    api_key: str = "test-api-key-12345"
    auth_type: str = "bearer"
    auth_config: dict = None
    request_config: dict = None
    response_parse_config: dict = None
    supports_multimodal: bool = False
    is_active: bool = True

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "model_type": self.model_type,
            "model_role": self.model_role,
            "endpoint": self.endpoint,
            "api_key": self.api_key,
            "auth_type": self.auth_type,
            "auth_config": self.auth_config or {},
            "request_config": self.request_config or {},
            "response_parse_config": self.response_parse_config or {},
            "supports_multimodal": self.supports_multimodal,
            "is_active": self.is_active,
        }


@dataclass
class MockJudgeModelConfig:
    """Mock裁判模型配置"""
    id: str = "judge-model-001"
    name: str = "裁判模型"
    model_type: str = "claude-3-opus"
    endpoint: str = "https://api.test.com/v1/chat/completions"
    api_key: str = "judge-api-key-12345"
    temperature: int = 0
    max_tokens: int = 4096
    system_prompt: str = "你是一名公正的评分裁判。"
    default_max_score: int = 10
    is_active: bool = True

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "model_type": self.model_type,
            "endpoint": self.endpoint,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "system_prompt": self.system_prompt,
            "default_max_score": self.default_max_score,
            "is_active": self.is_active,
        }


@dataclass
class MockEvaluationDataset:
    """Mock评测数据集"""
    id: str = "dataset-001"
    name: str = "测试数据集"
    description: str = "用于测试的数据集"
    question_count: int = 3
    created_by: str = "test_user"
    is_active: bool = True
    team_code: str = "DEFAULT"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "question_count": self.question_count,
            "created_by": self.created_by,
            "is_active": self.is_active,
            "team_code": self.team_code,
        }


@dataclass
class MockEvaluationData:
    """Mock评测数据项"""
    id: str = "data-001"
    dataset_id: str = "dataset-001"
    index: int = 1
    question: str = "你好，请介绍一下你自己"
    ref_answer: str = "我是一个人工智能助手。"
    meta_data: dict = None
    is_active: bool = True

    def to_dict(self):
        return {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "index": self.index,
            "question": self.question,
            "ref_answer": self.ref_answer,
            "meta_data": self.meta_data or {},
            "is_active": self.is_active,
        }


@dataclass
class MockEvaluationTask:
    """Mock评测任务"""
    id: str = "task-001"
    name: str = "测试评测任务"
    description: str = "用于验证mock数据的测试任务"
    evaluation_type: str = "judge"  # judge/consistency/success_rate/rag
    status: str = "pending"
    model_config: dict = None
    model_provider: str = "model-001"
    model_endpoint: str = "https://api.test.com/v1/chat/completions"
    dataset_id: str = "dataset-001"
    judge_model: str = "judge-model-001"
    judge_prompt: str = None
    evaluation_scene: str = "通用评测"
    scene_desc: str = "通用场景描述"
    evaluation_metrics: list = None
    max_score: int = 10
    score_details: str = None
    steps: list = None
    consistency_config: dict = None
    success_rate_config: dict = None
    judge_count: int = 3
    multi_judge_config: dict = None
    runtime_variables: dict = None
    variable_source: str = "manual"
    judge_variable_config: dict = None
    evaluation_targets: list = None
    validation_enabled: bool = True
    result: dict = None
    total_cost: float = 0.0
    progress: int = 0
    total_questions: int = 3
    completed_questions: int = 0
    created_by: str = "test_user"
    is_active: bool = True
    version: int = 0
    team_code: str = "DEFAULT"

    def __post_init__(self):
        if self.evaluation_metrics is None:
            self.evaluation_metrics = [
                {
                    "name": "内容质量",
                    "weight": "高",
                    "max_score": 10,
                    "description": "评估回复内容质量",
                    "score_criteria": {
                        "优秀": {"min": 9, "max": 10, "desc": "内容完整准确"},
                        "良好": {"min": 6, "max": 8, "desc": "内容基本完整"},
                        "较差": {"min": 0, "max": 5, "desc": "内容有较大问题"}
                    }
                },
                {
                    "name": "格式规范",
                    "weight": "中",
                    "max_score": 10,
                    "description": "评估回复格式",
                    "score_criteria": {
                        "优秀": {"min": 9, "max": 10, "desc": "格式规范"},
                        "良好": {"min": 6, "max": 8, "desc": "格式基本规范"},
                        "较差": {"min": 0, "max": 5, "desc": "格式混乱"}
                    }
                }
            ]
        if self.consistency_config is None:
            self.consistency_config = {
                "repeat_count": 5,
                "use_judge": True,
            }
        if self.success_rate_config is None:
            self.success_rate_config = {
                "repeat_count": 10,
                "success_criteria": {
                    "non_empty": True,
                    "no_error": True,
                    "valid_format": True,
                }
            }

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "evaluation_type": self.evaluation_type,
            "status": self.status,
            "model_config": self.model_config or {},
            "model_provider": self.model_provider,
            "model_endpoint": self.model_endpoint,
            "dataset_id": self.dataset_id,
            "judge_model": self.judge_model,
            "judge_prompt": self.judge_prompt,
            "evaluation_scene": self.evaluation_scene,
            "scene_desc": self.scene_desc,
            "evaluation_metrics": self.evaluation_metrics,
            "max_score": self.max_score,
            "score_details": self.score_details,
            "steps": self.steps,
            "consistency_config": self.consistency_config,
            "success_rate_config": self.success_rate_config,
            "judge_count": self.judge_count,
            "multi_judge_config": self.multi_judge_config,
            "runtime_variables": self.runtime_variables,
            "variable_source": self.variable_source,
            "judge_variable_config": self.judge_variable_config,
            "evaluation_targets": self.evaluation_targets,
            "validation_enabled": self.validation_enabled,
            "result": self.result,
            "total_cost": self.total_cost,
            "progress": self.progress,
            "total_questions": self.total_questions,
            "completed_questions": self.completed_questions,
            "created_by": self.created_by,
            "is_active": self.is_active,
            "version": self.version,
            "team_code": self.team_code,
        }


# ============================================================
# Mock 响应数据
# ============================================================

class MockModelResponse:
    """Mock模型响应"""

    @staticmethod
    def success_response(content: str, usage: dict = None):
        return {
            "success": True,
            "content": content,
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "model": "gpt-4",
        }

    @staticmethod
    def error_response(error: str):
        return {
            "success": False,
            "content": "",
            "error": error,
        }

    @staticmethod
    def judge_success_response():
        """裁判模型成功响应"""
        import json
        return {
            "success": True,
            "content": json.dumps({
                "综合评分": 8.5,
                "综合评分原因": "回复质量良好，内容准确且格式规范",
                "各维度评分": {
                    "内容质量": {"score": 9, "analysis": "内容完整准确"},
                    "格式规范": {"score": 8, "analysis": "格式基本规范"}
                }
            }),
            "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
        }

    @staticmethod
    def consistency_success_response():
        """一致性评测成功响应"""
        import json
        return {
            "success": True,
            "content": json.dumps({
                "consistency_score": 8.0,
                "content_consistency": {"score": 8, "analysis": "内容一致性良好"},
                "format_consistency": {"score": 9, "analysis": "格式一致性很高"},
                "structure_consistency": {"score": 7, "analysis": "结构一致性一般"}
            }),
            "usage": {"prompt_tokens": 150, "completion_tokens": 80, "total_tokens": 230},
        }


# ============================================================
# 测试类
# ============================================================

class TestMockDataStructure:
    """测试Mock数据结构"""

    def test_mock_model_config(self):
        """测试模型配置Mock"""
        config = MockModelConfig()
        config_dict = config.to_dict()

        assert config_dict["id"] == "model-001"
        assert config_dict["name"] == "测试模型"
        assert config_dict["provider"] == "custom_http"
        assert config_dict["model_type"] == "gpt-4"
        assert config_dict["model_role"] == "evaluation"
        assert "api_key" in config_dict

    def test_mock_judge_model_config(self):
        """测试裁判模型配置Mock"""
        config = MockJudgeModelConfig()
        config_dict = config.to_dict()

        assert config_dict["id"] == "judge-model-001"
        assert config_dict["name"] == "裁判模型"
        assert config_dict["temperature"] == 0
        assert config_dict["max_tokens"] == 4096

    def test_mock_evaluation_dataset(self):
        """测试评测数据集Mock"""
        dataset = MockEvaluationDataset()
        dataset_dict = dataset.to_dict()

        assert dataset_dict["id"] == "dataset-001"
        assert dataset_dict["name"] == "测试数据集"
        assert dataset_dict["question_count"] == 3

    def test_mock_evaluation_data(self):
        """测试评测数据项Mock"""
        data = MockEvaluationData()
        data_dict = data.to_dict()

        assert data_dict["id"] == "data-001"
        assert data_dict["dataset_id"] == "dataset-001"
        assert "question" in data_dict
        assert "ref_answer" in data_dict

    def test_mock_evaluation_task_consistency(self):
        """测试一致性评测任务Mock"""
        task = MockEvaluationTask(
            id="task-consistency-001",
            name="一致性评测任务",
            evaluation_type="consistency",
            consistency_config={
                "repeat_count": 5,
                "use_judge": True,
            }
        )
        task_dict = task.to_dict()

        assert task_dict["id"] == "task-consistency-001"
        assert task_dict["evaluation_type"] == "consistency"
        assert task_dict["consistency_config"]["repeat_count"] == 5
        assert task_dict["consistency_config"]["use_judge"] is True

    def test_mock_evaluation_task_consistency_no_judge(self):
        """测试不需要裁判的一致性评测任务Mock"""
        task = MockEvaluationTask(
            id="task-consistency-no-judge-001",
            name="一致性评测任务（无裁判）",
            evaluation_type="consistency",
            consistency_config={
                "repeat_count": 5,
                "use_judge": False,
            }
        )
        task_dict = task.to_dict()

        assert task_dict["evaluation_type"] == "consistency"
        assert task_dict["consistency_config"]["use_judge"] is False

    def test_mock_evaluation_task_success_rate(self):
        """测试成功率评测任务Mock"""
        task = MockEvaluationTask(
            id="task-success-rate-001",
            name="成功率评测任务",
            evaluation_type="success_rate",
            success_rate_config={
                "repeat_count": 10,
                "success_criteria": {
                    "non_empty": True,
                    "no_error": True,
                    "valid_format": True,
                }
            }
        )
        task_dict = task.to_dict()

        assert task_dict["id"] == "task-success-rate-001"
        assert task_dict["evaluation_type"] == "success_rate"
        assert task_dict["success_rate_config"]["repeat_count"] == 10

    def test_mock_evaluation_task_judge(self):
        """测试AI裁判员评测任务Mock"""
        task = MockEvaluationTask(
            id="task-judge-001",
            name="AI裁判员评测任务",
            evaluation_type="judge",
            judge_count=3,
            evaluation_metrics=[
                {
                    "name": "内容质量",
                    "weight": "高",
                    "max_score": 10,
                    "description": "评估回复内容质量",
                    "score_criteria": {
                        "优秀": {"min": 9, "max": 10, "desc": "内容完整准确"},
                        "良好": {"min": 6, "max": 8, "desc": "内容基本完整"},
                        "较差": {"min": 0, "max": 5, "desc": "内容有较大问题"}
                    }
                }
            ]
        )
        task_dict = task.to_dict()

        assert task_dict["id"] == "task-judge-001"
        assert task_dict["evaluation_type"] == "judge"
        assert task_dict["judge_count"] == 3
        assert len(task_dict["evaluation_metrics"]) == 1


class TestMockModelResponse:
    """测试Mock模型响应"""

    def test_success_response(self):
        """测试成功响应"""
        response = MockModelResponse.success_response("测试回复内容")
        assert response["success"] is True
        assert response["content"] == "测试回复内容"
        assert "usage" in response

    def test_error_response(self):
        """测试错误响应"""
        response = MockModelResponse.error_response("API调用失败")
        assert response["success"] is False
        assert response["error"] == "API调用失败"

    def test_judge_success_response(self):
        """测试裁判成功响应"""
        import json
        response = MockModelResponse.judge_success_response()
        assert response["success"] is True

        content = json.loads(response["content"])
        assert "综合评分" in content
        assert "各维度评分" in content
        assert content["综合评分"] == 8.5

    def test_consistency_success_response(self):
        """测试一致性评测成功响应"""
        import json
        response = MockModelResponse.consistency_success_response()
        assert response["success"] is True

        content = json.loads(response["content"])
        assert "consistency_score" in content
        assert "content_consistency" in content


class TestConsistencyEvaluatorWithMock:
    """一致性评测Mock测试（需要裁判介入）"""

    @pytest.fixture
    def mock_model_config(self):
        return MockModelConfig().to_dict()

    @pytest.fixture
    def mock_judge_config(self):
        return MockJudgeModelConfig().to_dict()

    @pytest.fixture
    def mock_evaluator_with_judge(self, mock_model_config, mock_judge_config):
        from app.services.consistency_evaluator import ConsistencyEvaluator
        return ConsistencyEvaluator(
            model_config=mock_model_config,
            judge_config=mock_judge_config,
            progress_callback=None,
            evaluation_metrics=[
                {
                    "name": "逻辑一致性",
                    "weight": "高",
                    "max_score": 10,
                    "description": "评估逻辑是否一致",
                    "score_criteria": {
                        "优秀": {"min": 9, "max": 10, "desc": "逻辑完全一致"},
                        "良好": {"min": 6, "max": 8, "desc": "逻辑基本一致"},
                        "较差": {"min": 0, "max": 5, "desc": "逻辑不一致"}
                    }
                }
            ],
            judge_variable_config={
                "user_input": {
                    "source": "dataset_field",
                    "source_config": {"field": "question"}
                }
            }
        )

    @pytest.mark.asyncio
    async def test_evaluate_single_with_judge(self, mock_evaluator_with_judge):
        """测试需要裁判的一致性评测"""
        with patch.object(
            mock_evaluator_with_judge.engine,
            '_call_model',
            new_callable=AsyncMock,
            return_value=MockModelResponse.judge_success_response()
        ):
            result = await mock_evaluator_with_judge.evaluate_single(
                question="测试问题",
                repeat_count=3,
                use_judge=True
            )

            assert result is not None
            assert len(result.repeat_results) == 3
            assert result.judge_assessment is not None
            assert result.metrics is not None
            assert "text_similarity" in result.metrics


class TestConsistencyEvaluatorWithoutMock:
    """一致性评测Mock测试（不需要裁判介入）"""

    @pytest.fixture
    def mock_model_config(self):
        return MockModelConfig().to_dict()

    @pytest.fixture
    def mock_evaluator_without_judge(self, mock_model_config):
        from app.services.consistency_evaluator import ConsistencyEvaluator
        return ConsistencyEvaluator(
            model_config=mock_model_config,
            judge_config=mock_model_config,
            progress_callback=None,
            evaluation_metrics=[],
            judge_variable_config=None  # 不使用裁判
        )

    @pytest.mark.asyncio
    async def test_evaluate_single_without_judge(self, mock_evaluator_without_judge):
        """测试不需要裁判的一致性评测"""
        with patch.object(
            mock_evaluator_without_judge.engine,
            '_call_model',
            new_callable=AsyncMock,
            return_value=MockModelResponse.success_response("测试回复内容")
        ):
            result = await mock_evaluator_without_judge.evaluate_single(
                question="测试问题",
                repeat_count=3,
                use_judge=False
            )

            assert result is not None
            assert len(result.repeat_results) == 3
            # 不使用裁判时，judge_assessment为空
            assert result.judge_assessment == {}
            assert result.metrics is not None


class TestSuccessRateEvaluatorWithMock:
    """成功率评测Mock测试"""

    @pytest.fixture
    def mock_model_config(self):
        return MockModelConfig().to_dict()

    @pytest.fixture
    def mock_success_rate_evaluator(self, mock_model_config):
        from app.services.success_rate_evaluator import SuccessRateEvaluator
        return SuccessRateEvaluator(
            model_config=mock_model_config,
            success_criteria={
                "non_empty": True,
                "no_error": True,
                "valid_format": True,
            },
            timeout_seconds=30,
            progress_callback=None
        )

    @pytest.mark.asyncio
    async def test_evaluate_single(self, mock_success_rate_evaluator):
        """测试成功率评测"""
        with patch(
            'app.services.success_rate_evaluator.ModelCaller.call',
            new_callable=AsyncMock,
            return_value=MockModelResponse.success_response("测试回复内容")
        ):
            result = await mock_success_rate_evaluator.evaluate_single(
                question="测试问题",
                repeat_count=5
            )

            assert result is not None
            assert len(result.repeat_results) == 5
            assert result.metrics is not None
            assert "success_rate" in result.metrics
            assert result.metrics["total_calls"] == 5


class TestJudgeEvaluationEngineWithMock:
    """AI裁判员评测Mock测试"""

    @pytest.fixture
    def mock_model_config(self):
        return MockModelConfig().to_dict()

    @pytest.fixture
    def mock_judge_config(self):
        return MockJudgeModelConfig().to_dict()

    @pytest.fixture
    def mock_judge_evaluator(self, mock_model_config, mock_judge_config):
        from app.services.judge_evaluation_engine import JudgeEvaluationEngine
        return JudgeEvaluationEngine(
            model_config=mock_model_config,
            judge_config=mock_judge_config,
            progress_callback=None,
            judge_variable_config={
                "user_input": {
                    "source": "dataset_field",
                    "source_config": {"field": "question"}
                },
                "ref_answer": {
                    "source": "dataset_field",
                    "source_config": {"field": "ref_answer"}
                },
                "model_answer": {
                    "source": "model_output"
                }
            },
            dataset_item={
                "question": "测试问题",
                "ref_answer": "参考答案"
            }
        )

    @pytest.mark.asyncio
    async def test_evaluate_single(self, mock_judge_evaluator):
        """测试AI裁判员评测"""
        with patch.object(
            mock_judge_evaluator,
            '_call_model',
            new_callable=AsyncMock,
            return_value=MockModelResponse.judge_success_response()
        ):
            result = await mock_judge_evaluator.evaluate_single(
                question="测试问题",
                ref_answer="参考答案",
                model_answer="模型回复"
            )

            assert result is not None
            assert result.success is True
            assert result.overall_score == 8.5
            assert result.scores is not None
            assert "综合评分" in result.scores or "raw" in result.scores


# ============================================================
# 批量Mock数据生成器
# ============================================================

class MockDataGenerator:
    """批量生成Mock数据"""

    @staticmethod
    def generate_model_configs(count: int = 5):
        """生成多个模型配置"""
        configs = []
        for i in range(count):
            config = MockModelConfig(
                id=f"model-{i+1:03d}",
                name=f"测试模型-{i+1}",
                model_type=f"gpt-{i+1}"
            )
            configs.append(config.to_dict())
        return configs

    @staticmethod
    def generate_judge_model_configs(count: int = 3):
        """生成多个裁判模型配置"""
        configs = []
        for i in range(count):
            config = MockJudgeModelConfig(
                id=f"judge-model-{i+1:03d}",
                name=f"裁判模型-{i+1}",
                model_type=f"claude-{i+1}"
            )
            configs.append(config.to_dict())
        return configs

    @staticmethod
    def generate_datasets(count: int = 5, questions_per_dataset: int = 10):
        """生成多个数据集及其数据项"""
        datasets = []
        for i in range(count):
            dataset_id = f"dataset-{i+1:03d}"
            dataset = MockEvaluationDataset(
                id=dataset_id,
                name=f"测试数据集-{i+1}",
                question_count=questions_per_dataset
            )
            datasets.append(dataset.to_dict())
        return datasets

    @staticmethod
    def generate_evaluation_tasks(
        evaluation_type: str = "judge",
        count: int = 5
    ):
        """生成多个评测任务"""
        tasks = []
        for i in range(count):
            task = MockEvaluationTask(
                id=f"task-{evaluation_type}-{i+1:03d}",
                name=f"{evaluation_type}评测任务-{i+1}",
                evaluation_type=evaluation_type,
                dataset_id=f"dataset-{i+1:03d}"
            )
            tasks.append(task.to_dict())
        return tasks

    @staticmethod
    def generate_full_mock_data():
        """生成完整的Mock数据（包含所有类型）"""
        return {
            "model_configs": MockDataGenerator.generate_model_configs(3),
            "judge_model_configs": MockDataGenerator.generate_judge_model_configs(2),
            "datasets": MockDataGenerator.generate_datasets(3, 5),
            "tasks": {
                "consistency_with_judge": MockDataGenerator.generate_evaluation_tasks("consistency", 2),
                "consistency_without_judge": MockDataGenerator.generate_evaluation_tasks("consistency", 2),
                "success_rate": MockDataGenerator.generate_evaluation_tasks("success_rate", 2),
                "judge": MockDataGenerator.generate_evaluation_tasks("judge", 2),
            }
        }


class TestMockDataGenerator:
    """测试Mock数据生成器"""

    def test_generate_model_configs(self):
        """测试生成多个模型配置"""
        configs = MockDataGenerator.generate_model_configs(5)
        assert len(configs) == 5
        assert all("id" in c for c in configs)
        assert all("name" in c for c in configs)

    def test_generate_datasets(self):
        """测试生成多个数据集"""
        datasets = MockDataGenerator.generate_datasets(3, 10)
        assert len(datasets) == 3
        for ds in datasets:
            assert ds["question_count"] == 10

    def test_generate_evaluation_tasks(self):
        """测试生成多个评测任务"""
        tasks = MockDataGenerator.generate_evaluation_tasks("judge", 3)
        assert len(tasks) == 3
        assert all(t["evaluation_type"] == "judge" for t in tasks)

    def test_generate_full_mock_data(self):
        """测试生成完整Mock数据"""
        data = MockDataGenerator.generate_full_mock_data()

        assert "model_configs" in data
        assert "judge_model_configs" in data
        assert "datasets" in data
        assert "tasks" in data

        assert len(data["model_configs"]) == 3
        assert len(data["judge_model_configs"]) == 2
        assert len(data["datasets"]) == 3

        assert "consistency_with_judge" in data["tasks"]
        assert "consistency_without_judge" in data["tasks"]
        assert "success_rate" in data["tasks"]
        assert "judge" in data["tasks"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
