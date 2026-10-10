"""
插入Mock数据到数据库

运行方式:
    python scripts/insert_mock_data.py

数据内容:
    - 模型配置（待评测模型 + 裁判模型）
    - 数据集
    - 评测任务（4种类型：一致性评测-有裁判、一致性评测-无裁判、成功率评测、AI裁判员评测）
"""
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uuid
from datetime import datetime
from sqlalchemy import text
from app.db.session import SessionLocal
from app.models.model_config_models import ModelConfig, JudgeModelConfig
from app.models.dataset_models import EvaluationDataset, EvaluationData
from app.models.task_models import EvaluationTask


def generate_id(prefix: str) -> str:
    """生成带前缀的UUID"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def cleanup_mock_data(db):
    """清理已存在的Mock数据"""
    print("\n=== 清理已存在的Mock数据 ===")

    # 清理评测任务
    task_ids = [
        "mock-task-consistency-with-judge-001",
        "mock-task-consistency-no-judge-001",
        "mock-task-success-rate-001",
        "mock-task-judge-001",
    ]
    for task_id in task_ids:
        db.execute(text(f"DELETE FROM evaluation_tasks WHERE id = '{task_id}'"))

    # 清理评测数据
    data_ids = []
    for i in range(1, 4):
        data_ids.append(f"mock-data-consistency-{i:03d}")
    for i in range(1, 6):
        data_ids.append(f"mock-data-success-rate-{i:03d}")
    for i in range(1, 6):
        data_ids.append(f"mock-data-judge-{i:03d}")
    for data_id in data_ids:
        db.execute(text(f"DELETE FROM evaluation_data WHERE id = '{data_id}'"))

    # 清理数据集
    dataset_ids = [
        "mock-dataset-consistency-001",
        "mock-dataset-success-rate-001",
        "mock-dataset-judge-001",
    ]
    for ds_id in dataset_ids:
        db.execute(text(f"DELETE FROM evaluation_datasets WHERE id = '{ds_id}'"))

    # 清理模型配置
    model_ids = ["mock-eval-model-001", "mock-judge-model-001"]
    for model_id in model_ids:
        db.execute(text(f"DELETE FROM model_configs WHERE id = '{model_id}'"))

    db.commit()
    print("  - 已清理所有Mock数据")


def insert_model_configs(db):
    """插入模型配置"""
    print("\n=== 插入模型配置 ===")

    # 待评测模型
    eval_model = ModelConfig(
        id="mock-eval-model-001",
        name="Mock待评测模型",
        provider="custom_http",
        model_type="gpt-4",
        model_role="evaluation",
        endpoint="https://api.test.com/v1/chat/completions",
        api_key=os.environ.get("MOCK_EVAL_API_KEY", ""),
        auth_type="bearer",
        auth_config={"header_name": "Authorization", "header_value_prefix": "Bearer"},
        request_config={
            "temperature": 0.7,
            "max_tokens": 2000,
            "top_p": 1.0,
        },
        response_parse_config={"path": "choices.0.message.content"},
        supports_multimodal=False,
        description="用于测试的Mock待评测模型",
        role_description="待评测模型",
        is_active=True,
        is_preset=False,
        created_by="mock_script",
        team_code="DEFAULT",
    )
    db.add(eval_model)

    # 裁判模型
    judge_model = ModelConfig(
        id="mock-judge-model-001",
        name="Mock裁判模型",
        provider="custom_http",
        model_type="claude-3-opus",
        model_role="judge",
        endpoint="https://api.test.com/v1/chat/completions",
        api_key=os.environ.get("MOCK_JUDGE_API_KEY", ""),
        auth_type="bearer",
        auth_config={"header_name": "Authorization", "header_value_prefix": "Bearer"},
        request_config={
            "temperature": 0,
            "max_tokens": 4096,
        },
        response_parse_config={"path": "choices.0.message.content"},
        supports_multimodal=False,
        description="用于测试的Mock裁判模型",
        role_description="裁判模型",
        is_active=True,
        is_preset=False,
        created_by="mock_script",
        team_code="DEFAULT",
    )
    db.add(judge_model)

    db.commit()
    print(f"  - 待评测模型: mock-eval-model-001")
    print(f"  - 裁判模型: mock-judge-model-001")

    return {"eval_model_id": eval_model.id, "judge_model_id": judge_model.id}


def insert_datasets(db):
    """插入数据集"""
    print("\n=== 插入数据集 ===")

    datasets = []

    # 数据集1：一致性评测数据集
    ds1 = EvaluationDataset(
        id="mock-dataset-consistency-001",
        name="Mock一致性评测数据集",
        description="用于测试一致性评测的数据集",
        question_count=3,
        created_by="mock_script",
        is_active=True,
        team_code="DEFAULT",
    )
    db.add(ds1)
    datasets.append(ds1)

    # 数据集2：成功率评测数据集
    ds2 = EvaluationDataset(
        id="mock-dataset-success-rate-001",
        name="Mock成功率评测数据集",
        description="用于测试成功率评测的数据集",
        question_count=5,
        created_by="mock_script",
        is_active=True,
        team_code="DEFAULT",
    )
    db.add(ds2)
    datasets.append(ds2)

    # 数据集3：AI裁判员评测数据集
    ds3 = EvaluationDataset(
        id="mock-dataset-judge-001",
        name="Mock AI裁判员评测数据集",
        description="用于测试AI裁判员评测的数据集",
        question_count=5,
        created_by="mock_script",
        is_active=True,
        team_code="DEFAULT",
    )
    db.add(ds3)
    datasets.append(ds3)

    db.commit()
    print(f"  - 一致性评测数据集: mock-dataset-consistency-001")
    print(f"  - 成功率评测数据集: mock-dataset-success-rate-001")
    print(f"  - AI裁判员评测数据集: mock-dataset-judge-001")

    return {
        "consistency": ds1.id,
        "success_rate": ds2.id,
        "judge": ds3.id,
    }


def insert_evaluation_data(db, dataset_ids: dict):
    """插入评测数据项"""
    print("\n=== 插入评测数据项 ===")

    # 一致性评测数据集的数据
    consistency_questions = [
        "请介绍一下春天的特点，包括天气、景色和活动。",
        "解释一下什么是人工智能，并举例说明其应用场景。",
        "写一段关于旅行的短文，描述你最喜欢的旅行目的地。",
    ]

    for idx, question in enumerate(consistency_questions, 1):
        data = EvaluationData(
            id=f"mock-data-consistency-{idx:03d}",
            dataset_id=dataset_ids["consistency"],
            index=idx,
            question=question,
            ref_answer="",  # 一致性评测不需要参考答案
            meta_data={"type": "consistency"},
            is_active=True,
        )
        db.add(data)

    # 成功率评测数据集的数据
    success_questions = [
        "今天天气怎么样？",
        "请给我讲一个笑话。",
        "翻译：Hello, how are you?",
        "解释量子计算的基本原理。",
        "推荐一部好看的电影。",
    ]

    for idx, question in enumerate(success_questions, 1):
        data = EvaluationData(
            id=f"mock-data-success-rate-{idx:03d}",
            dataset_id=dataset_ids["success_rate"],
            index=idx,
            question=question,
            ref_answer="",
            meta_data={"type": "success_rate"},
            is_active=True,
        )
        db.add(data)

    # AI裁判员评测数据集的数据
    judge_questions = [
        {
            "question": "解释机器学习和深度学习的区别。",
            "ref_answer": "机器学习是AI的子领域，通过数据训练模型；深度学习是机器学习的子集，使用神经网络。"
        },
        {
            "question": "如何优化SQL查询性能？",
            "ref_answer": "1. 使用索引 2. 避免SELECT * 3. 优化JOIN 4. 使用EXPLAIN分析 5. 限制结果集"
        },
        {
            "question": "描述一下你最喜欢的城市，并说明原因。",
            "ref_answer": "答案应包含：城市名称、特点描述、喜欢的原因等要素。"
        },
        {
            "question": "解释什么是区块链技术。",
            "ref_answer": "区块链是分布式账本技术，具有去中心化、不可篡改、可追溯等特点。"
        },
        {
            "question": "写一首关于秋天的诗。",
            "ref_answer": "诗歌应包含秋天的典型意象，如落叶、秋风、丰收等。"
        },
    ]

    for idx, item in enumerate(judge_questions, 1):
        data = EvaluationData(
            id=f"mock-data-judge-{idx:03d}",
            dataset_id=dataset_ids["judge"],
            index=idx,
            question=item["question"],
            ref_answer=item["ref_answer"],
            meta_data={"type": "judge"},
            is_active=True,
        )
        db.add(data)

    db.commit()
    print(f"  - 一致性评测数据项: {len(consistency_questions)} 条")
    print(f"  - 成功率评测数据项: {len(success_questions)} 条")
    print(f"  - AI裁判员评测数据项: {len(judge_questions)} 条")


def insert_evaluation_tasks(db, model_ids: dict, dataset_ids: dict):
    """插入评测任务"""
    print("\n=== 插入评测任务 ===")

    # 1. 一致性评测任务（需要裁判介入）
    task1 = EvaluationTask(
        id="mock-task-consistency-with-judge-001",
        name="Mock一致性评测（需要裁判）",
        description="测试一致性评测功能，启用裁判评估",
        evaluation_type="consistency",
        status="pending",
        model_config={},
        model_provider=model_ids["eval_model_id"],
        dataset_id=dataset_ids["consistency"],
        judge_model=model_ids["judge_model_id"],
        consistency_config={
            "repeat_count": 5,
            "use_judge": True,
        },
        evaluation_scene="一致性评测",
        scene_desc="评估模型多次回复的一致性",
        evaluation_metrics=[
            {
                "name": "内容一致性",
                "weight": "高",
                "max_score": 10,
                "description": "评估回复内容的一致性",
                "score_criteria": {
                    "优秀": {"min": 9, "max": 10, "desc": "内容高度一致"},
                    "良好": {"min": 6, "max": 8, "desc": "内容基本一致"},
                    "较差": {"min": 0, "max": 5, "desc": "内容差异较大"}
                }
            },
            {
                "name": "格式一致性",
                "weight": "中",
                "max_score": 10,
                "description": "评估回复格式的一致性",
                "score_criteria": {
                    "优秀": {"min": 9, "max": 10, "desc": "格式完全一致"},
                    "良好": {"min": 6, "max": 8, "desc": "格式基本一致"},
                    "较差": {"min": 0, "max": 5, "desc": "格式差异较大"}
                }
            }
        ],
        max_score=10,
        judge_count=3,
        judge_variable_config={
            "user_input": {"source": "dataset_field", "source_config": {"field": "question"}},
            "model_answer": {"source": "model_output"},
        },
        validation_enabled=True,
        total_questions=3,
        created_by="mock_script",
        is_active=True,
        version=0,
        team_code="DEFAULT",
    )
    db.add(task1)
    print(f"  - 一致性评测（需要裁判）: mock-task-consistency-with-judge-001")

    # 2. 一致性评测任务（不需要裁判介入）
    task2 = EvaluationTask(
        id="mock-task-consistency-no-judge-001",
        name="Mock一致性评测（无裁判）",
        description="测试一致性评测功能，不启用裁判评估",
        evaluation_type="consistency",
        status="pending",
        model_config={},
        model_provider=model_ids["eval_model_id"],
        dataset_id=dataset_ids["consistency"],
        consistency_config={
            "repeat_count": 5,
            "use_judge": False,
        },
        evaluation_scene="一致性评测",
        scene_desc="评估模型多次回复的一致性（无裁判）",
        max_score=10,
        validation_enabled=True,
        total_questions=3,
        created_by="mock_script",
        is_active=True,
        version=0,
        team_code="DEFAULT",
    )
    db.add(task2)
    print(f"  - 一致性评测（无裁判）: mock-task-consistency-no-judge-001")

    # 3. 成功率评测任务
    task3 = EvaluationTask(
        id="mock-task-success-rate-001",
        name="Mock成功率评测",
        description="测试成功率评测功能",
        evaluation_type="success_rate",
        status="pending",
        model_config={},
        model_provider=model_ids["eval_model_id"],
        dataset_id=dataset_ids["success_rate"],
        success_rate_config={
            "repeat_count": 10,
            "success_criteria": {
                "non_empty": True,
                "no_error": True,
                "valid_format": True,
            }
        },
        evaluation_scene="成功率评测",
        scene_desc="评估模型调用成功率",
        max_score=10,
        validation_enabled=True,
        total_questions=5,
        created_by="mock_script",
        is_active=True,
        version=0,
        team_code="DEFAULT",
    )
    db.add(task3)
    print(f"  - 成功率评测: mock-task-success-rate-001")

    # 4. AI裁判员评测任务
    task4 = EvaluationTask(
        id="mock-task-judge-001",
        name="Mock AI裁判员评测",
        description="测试AI裁判员评测功能",
        evaluation_type="judge",
        status="pending",
        model_config={},
        model_provider=model_ids["eval_model_id"],
        dataset_id=dataset_ids["judge"],
        judge_model=model_ids["judge_model_id"],
        evaluation_scene="AI裁判员评测",
        scene_desc="使用裁判模型评估被测模型回复质量",
        evaluation_metrics=[
            {
                "name": "内容质量",
                "weight": "高",
                "max_score": 10,
                "description": "评估回复内容的准确性和完整性",
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
                "description": "评估回复格式的规范性",
                "score_criteria": {
                    "优秀": {"min": 9, "max": 10, "desc": "格式规范"},
                    "良好": {"min": 6, "max": 8, "desc": "格式基本规范"},
                    "较差": {"min": 0, "max": 5, "desc": "格式混乱"}
                }
            }
        ],
        max_score=10,
        score_details="评分维度包括内容质量和格式规范",
        judge_count=3,
        judge_variable_config={
            "user_input": {"source": "dataset_field", "source_config": {"field": "question"}},
            "ref_answer": {"source": "dataset_field", "source_config": {"field": "ref_answer"}},
            "model_answer": {"source": "model_output"},
        },
        validation_enabled=True,
        total_questions=5,
        created_by="mock_script",
        is_active=True,
        version=0,
        team_code="DEFAULT",
    )
    db.add(task4)
    print(f"  - AI裁判员评测: mock-task-judge-001")

    db.commit()


def main():
    """主函数"""
    print("=" * 60)
    print("开始插入 Mock 数据到数据库")
    print("=" * 60)

    db = SessionLocal()
    try:
        # 先清理已存在的Mock数据
        cleanup_mock_data(db)

        # 插入模型配置
        model_ids = insert_model_configs(db)

        # 插入数据集
        dataset_ids = insert_datasets(db)

        # 插入评测数据项
        insert_evaluation_data(db, dataset_ids)

        # 插入评测任务
        insert_evaluation_tasks(db, model_ids, dataset_ids)

        print("\n" + "=" * 60)
        print("Mock 数据插入完成！")
        print("=" * 60)

        print("\n插入的数据摘要:")
        print("-" * 40)
        print("模型配置:")
        print(f"  - 待评测模型 ID: {model_ids['eval_model_id']}")
        print(f"  - 裁判模型 ID: {model_ids['judge_model_id']}")
        print("\n数据集:")
        print(f"  - 一致性评测数据集 ID: {dataset_ids['consistency']}")
        print(f"  - 成功率评测数据集 ID: {dataset_ids['success_rate']}")
        print(f"  - AI裁判员评测数据集 ID: {dataset_ids['judge']}")
        print("\n评测任务:")
        print("  - mock-task-consistency-with-judge-001 (一致性评测-需要裁判)")
        print("  - mock-task-consistency-no-judge-001 (一致性评测-无裁判)")
        print("  - mock-task-success-rate-001 (成功率评测)")
        print("  - mock-task-judge-001 (AI裁判员评测)")

    except Exception as e:
        print(f"\n错误: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
