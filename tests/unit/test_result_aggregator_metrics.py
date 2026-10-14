"""
整体指标计算单元测试
测试 calculate_overall_metrics 方法
"""

import pytest
from app.services.result_aggregator import ResultAggregator
from app.services.judge_evaluation_engine import JudgeEvaluationResult

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestCalculateOverallMetrics:
    """calculate_overall_metrics 方法测试"""

    def test_empty_results_returns_zero_metrics(self):
        """测试空结果列表返回全零指标"""
        aggregator = ResultAggregator()
        results = []

        metrics = aggregator.calculate_overall_metrics(results)

        assert metrics["accuracy"] == 0.0
        assert metrics["precision"] == 0.0
        assert metrics["recall"] == 0.0
        assert metrics["specificity"] == 0.0
        assert metrics["avg_duration"] == 0.0
        assert metrics["total_count"] == 0
        assert metrics["passed_count"] == 0
        assert metrics["failed_count"] == 0

    def test_all_passed_results(self):
        """测试全部通过场景"""
        aggregator = ResultAggregator()
        results = [
            JudgeEvaluationResult(question_index=1, question="q1", ref_answer="a", model_answer="b", overall_score=0.8, success=True, duration=1.0),
            JudgeEvaluationResult(question_index=2, question="q2", ref_answer="a", model_answer="b", overall_score=0.9, success=True, duration=1.5),
        ]

        metrics = aggregator.calculate_overall_metrics(results, threshold=0.6)

        # 全部通过时：tp=2, fp=0, tn=0, fn=0
        # actual_positive=success=True, predicted_positive=is_passed=True
        # accuracy=(tp+tn)/total=(2+0)/2=1.0
        # precision=tp/(tp+fp)=2/2=1.0
        # recall=tp/(tp+fn)=2/2=1.0
        # specificity=tn/(tn+fp)=0/0=0.0 (因为分母为0)
        assert metrics["accuracy"] == 1.0
        assert metrics["precision"] == 1.0
        assert metrics["recall"] == 1.0
        assert metrics["specificity"] == 0.0  # 实际计算结果
        assert metrics["total_count"] == 2
        assert metrics["passed_count"] == 2
        assert metrics["failed_count"] == 0

    def test_all_failed_results(self):
        """测试全部失败场景（success=True但分数未达标）"""
        aggregator = ResultAggregator()
        results = [
            JudgeEvaluationResult(question_index=1, question="q1", ref_answer="a", model_answer="b", overall_score=0.3, success=True, duration=1.0),
            JudgeEvaluationResult(question_index=2, question="q2", ref_answer="a", model_answer="b", overall_score=0.4, success=True, duration=1.5),
        ]

        metrics = aggregator.calculate_overall_metrics(results, threshold=0.6)

        # actual_positive=success=True, predicted_positive=is_passed=False (score<threshold)
        # tp=0, fp=0, tn=0, fn=2
        # accuracy=(tp+tn)/total=(0+0)/2=0.0
        # precision=tp/(tp+fp)=0/0=0.0
        # recall=tp/(tp+fn)=0/2=0.0
        # specificity=tn/(tn+fp)=0/0=0.0 (因为分母为0)
        assert metrics["accuracy"] == 0.0
        assert metrics["precision"] == 0.0
        assert metrics["recall"] == 0.0
        assert metrics["specificity"] == 0.0
        assert metrics["total_count"] == 2
        assert metrics["passed_count"] == 0
        assert metrics["failed_count"] == 2

    def test_mixed_results_tp_fp_tn_fn(self):
        """测试混合场景（TP/FP/TN/FN）"""
        aggregator = ResultAggregator()
        results = [
            # TP: actual_positive=True, predicted_positive=True (score>=threshold)
            JudgeEvaluationResult(question_index=1, question="q1", ref_answer="a", model_answer="b", overall_score=0.8, success=True, duration=1.0),
            # FN: actual_positive=True, predicted_positive=False (score<threshold)
            JudgeEvaluationResult(question_index=2, question="q2", ref_answer="a", model_answer="b", overall_score=0.4, success=True, duration=1.5),
            # TN: actual_positive=False, predicted_positive=False
            JudgeEvaluationResult(question_index=3, question="q3", ref_answer="a", model_answer="b", overall_score=0.0, success=False, duration=0.0),
            # TN: actual_positive=False, predicted_positive=False
            JudgeEvaluationResult(question_index=4, question="q4", ref_answer="a", model_answer="b", overall_score=0.0, success=False, duration=0.0),
        ]

        metrics = aggregator.calculate_overall_metrics(results, threshold=0.6)

        # actual_positive=success, predicted_positive=is_passed
        # tp=1, fp=0, tn=2, fn=1
        assert metrics["tp"] == 1
        assert metrics["fp"] == 0
        assert metrics["tn"] == 2
        assert metrics["fn"] == 1
        assert metrics["tp"] + metrics["fp"] + metrics["tn"] + metrics["fn"] == metrics["total_count"]
        # accuracy = (tp + tn) / total = (1 + 2) / 4 = 0.75
        assert metrics["accuracy"] == 0.75

    def test_custom_threshold(self):
        """测试自定义阈值"""
        aggregator = ResultAggregator()
        results = [
            JudgeEvaluationResult(question_index=1, question="q1", ref_answer="a", model_answer="b", overall_score=0.7, success=True, duration=1.0),
        ]

        # threshold=0.8 时，0.7 应该失败
        metrics = aggregator.calculate_overall_metrics(results, threshold=0.8)

        assert metrics["passed_count"] == 0
        assert metrics["failed_count"] == 1

    def test_avg_duration_calculation(self):
        """测试 avg_duration 计算"""
        aggregator = ResultAggregator()
        results = [
            JudgeEvaluationResult(question_index=1, question="q1", ref_answer="a", model_answer="b", overall_score=0.8, success=True, duration=1.0),
            JudgeEvaluationResult(question_index=2, question="q2", ref_answer="a", model_answer="b", overall_score=0.8, success=True, duration=3.0),
        ]

        metrics = aggregator.calculate_overall_metrics(results)

        # avg_duration = (1.0 + 3.0) / 2 = 2.0
        assert metrics["avg_duration"] == 2.0

    def test_success_false_results(self):
        """测试 success=False 的结果"""
        aggregator = ResultAggregator()
        results = [
            JudgeEvaluationResult(question_index=1, question="q1", ref_answer="a", model_answer="b", overall_score=0.0, success=False, error="API Error", duration=0.0),
        ]

        metrics = aggregator.calculate_overall_metrics(results)

        # success=False 即使 score 高也应该是 failed
        assert metrics["passed_count"] == 0
        assert metrics["failed_count"] == 1

    def test_confusion_matrix_elements(self):
        """测试混淆矩阵四个元素"""
        aggregator = ResultAggregator()
        # actual_positive=success, predicted_positive=is_passed
        results = [
            # TP: actual_positive=True, predicted_positive=True
            JudgeEvaluationResult(question_index=1, question="q1", ref_answer="a", model_answer="b", overall_score=0.8, success=True, duration=1.0),
            # TN: actual_positive=False, predicted_positive=False
            JudgeEvaluationResult(question_index=2, question="q2", ref_answer="a", model_answer="b", overall_score=0.8, success=False, duration=0.0),
            # TN: actual_positive=False, predicted_positive=False
            JudgeEvaluationResult(question_index=3, question="q3", ref_answer="a", model_answer="b", overall_score=0.3, success=False, duration=0.0),
            # FN: actual_positive=True, predicted_positive=False
            JudgeEvaluationResult(question_index=4, question="q4", ref_answer="a", model_answer="b", overall_score=0.3, success=True, duration=0.0),
        ]

        metrics = aggregator.calculate_overall_metrics(results, threshold=0.6)

        # tp=1, fp=0, tn=2, fn=1
        assert metrics["tp"] == 1
        assert metrics["fp"] == 0
        assert metrics["tn"] == 2
        assert metrics["fn"] == 1
        assert metrics["total_count"] == 4


class TestAggregateConsistencyResults:
    """aggregate_consistency_results 方法测试"""

    def test_empty_results(self):
        """测试空结果列表"""
        result = ResultAggregator.aggregate_consistency_results([])

        assert result["total_questions"] == 0
        assert result["average_text_similarity"] == 0.0
        assert result["custom_dimension_summary"] == {}

    def test_results_without_custom_dimensions(self):
        """测试无自定义维度的结果"""
        results = [
            {
                "idx": 0,
                "question": "问题1",
                "success": True,
                "metrics": {
                    "text_similarity": 0.9,
                    "format_consistency": 0.8,
                    "structure_consistency": 0.85,
                },
                "judge_assessment": {
                    "consistency_score": 8,
                    "content_consistency": {"score": 8, "analysis": "ok"},
                },
            },
            {
                "idx": 1,
                "question": "问题2",
                "success": True,
                "metrics": {
                    "text_similarity": 0.85,
                    "format_consistency": 0.9,
                    "structure_consistency": 0.8,
                },
                "judge_assessment": {},
            },
        ]

        result = ResultAggregator.aggregate_consistency_results(results)

        assert result["total_questions"] == 2
        assert result["custom_dimension_summary"] == {}

    def test_results_with_custom_dimensions(self):
        """测试有自定义维度的结果"""
        results = [
            {
                "idx": 0,
                "question": "问题1",
                "success": True,
                "metrics": {
                    "text_similarity": 0.9,
                    "format_consistency": 0.8,
                    "structure_consistency": 0.85,
                },
                "judge_assessment": {
                    "各维度评分": {
                        "逻辑一致性": {"score": 9, "analysis": "逻辑很一致"},
                        "表达一致性": {"score": 8, "analysis": "表达基本一致"},
                    }
                },
            },
            {
                "idx": 1,
                "question": "问题2",
                "success": True,
                "metrics": {
                    "text_similarity": 0.85,
                    "format_consistency": 0.9,
                    "structure_consistency": 0.8,
                },
                "judge_assessment": {
                    "各维度评分": {
                        "逻辑一致性": {"score": 7, "analysis": "逻辑有些不一致"},
                        "表达一致性": {"score": 9, "analysis": "表达很一致"},
                    }
                },
            },
        ]

        result = ResultAggregator.aggregate_consistency_results(results)

        assert result["total_questions"] == 2
        assert "custom_dimension_summary" in result
        assert "逻辑一致性" in result["custom_dimension_summary"]
        assert "表达一致性" in result["custom_dimension_summary"]
        # 逻辑一致性平均值: (9+7)/2 = 8.0
        assert result["custom_dimension_summary"]["逻辑一致性"]["average"] == 8.0
        assert result["custom_dimension_summary"]["逻辑一致性"]["count"] == 2
        # 表达一致性平均值: (8+9)/2 = 8.5
        assert result["custom_dimension_summary"]["表达一致性"]["average"] == 8.5

    def test_mixed_results_with_and_without_custom_dimensions(self):
        """测试混合数据：部分结果有自定义维度，部分没有"""
        results = [
            {
                "idx": 0,
                "question": "问题1",
                "success": True,
                "metrics": {"text_similarity": 0.9, "format_consistency": 0.8, "structure_consistency": 0.85},
                "judge_assessment": {
                    "各维度评分": {
                        "逻辑一致性": {"score": 9, "analysis": "好"},
                    }
                },
            },
            {
                "idx": 1,
                "question": "问题2",
                "success": True,
                "metrics": {"text_similarity": 0.85, "format_consistency": 0.9, "structure_consistency": 0.8},
                "judge_assessment": {},  # 无自定义维度
            },
            {
                "idx": 2,
                "question": "问题3",
                "success": True,
                "metrics": {"text_similarity": 0.88, "format_consistency": 0.85, "structure_consistency": 0.9},
                "judge_assessment": {
                    "各维度评分": {
                        "逻辑一致性": {"score": 8, "analysis": "不错"},
                    }
                },
            },
        ]

        result = ResultAggregator.aggregate_consistency_results(results)

        assert result["total_questions"] == 3
        assert "逻辑一致性" in result["custom_dimension_summary"]
        # 只有2条结果有"逻辑一致性"维度
        assert result["custom_dimension_summary"]["逻辑一致性"]["count"] == 2
        # 平均值: (9+8)/2 = 8.5
        assert result["custom_dimension_summary"]["逻辑一致性"]["average"] == 8.5

    def test_flat_numeric_dimension_scores(self):
        """测试维度评分是纯数字（非 dict 格式）的聚合"""
        results = [
            {
                "idx": 0,
                "question": "问题1",
                "success": True,
                "metrics": {"text_similarity": 1.0, "format_consistency": 1.0, "structure_consistency": 1.0},
                "judge_assessment": {
                    "综合评分": 10,
                    "各维度评分": {"测试维度": 10}
                },
            },
            {
                "idx": 1,
                "question": "问题2",
                "success": True,
                "metrics": {"text_similarity": 1.0, "format_consistency": 1.0, "structure_consistency": 1.0},
                "judge_assessment": {
                    "综合评分": 8,
                    "各维度评分": {"测试维度": 8}
                },
            },
        ]

        result = ResultAggregator.aggregate_consistency_results(results)

        assert result["total_questions"] == 2
        assert "测试维度" in result["custom_dimension_summary"]
        assert result["custom_dimension_summary"]["测试维度"]["average"] == 9.0
        assert result["custom_dimension_summary"]["测试维度"]["count"] == 2

