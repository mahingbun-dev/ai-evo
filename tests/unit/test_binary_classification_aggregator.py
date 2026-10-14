"""
二分分类结果聚合单元测试
"""
import pytest
from app.services.result_aggregator import ResultAggregator

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestAggregateBinaryClassificationResults:
    """测试 aggregate_binary_classification_results 方法"""

    def test_aggregate_empty_results(self):
        """空结果聚合"""
        aggregation = ResultAggregator.aggregate_binary_classification_results([])

        assert aggregation["total_count"] == 0
        assert aggregation["confusion_matrix"] == {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        assert aggregation["items"] == []
        assert aggregation["accuracy"] == 0.0
        assert aggregation["specificity"] == 0.0
        assert aggregation["sensitivity"] == 0.0
        assert aggregation["precision"] == 0.0
        assert aggregation["f1_score"] == 0.0
        assert aggregation["f2_score"] == 0.0
        assert aggregation["f05_score"] == 0.0
        assert aggregation["mcc"] == 0.0
        assert aggregation["npv"] == 0.0
        assert aggregation["error_rate"] == 0.0
        assert aggregation["prevalence"] == 0.0
        assert aggregation["youden_index"] == 0.0

    def test_confusion_matrix_calculation(self):
        """混淆矩阵计算"""
        results = [
            {"idx": 0, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 1, "expected_label": True, "predicted_label": False, "is_correct": False},
            {"idx": 2, "expected_label": False, "predicted_label": True, "is_correct": False},
            {"idx": 3, "expected_label": False, "predicted_label": False, "is_correct": True},
        ]
        aggregation = ResultAggregator.aggregate_binary_classification_results(results)

        assert aggregation["confusion_matrix"]["tp"] == 1
        assert aggregation["confusion_matrix"]["fp"] == 1
        assert aggregation["confusion_matrix"]["tn"] == 1
        assert aggregation["confusion_matrix"]["fn"] == 1
        assert aggregation["total_count"] == 4
        assert aggregation["accuracy"] == pytest.approx(0.5, rel=1e-3)
        assert aggregation["specificity"] == pytest.approx(0.5, rel=1e-3)
        assert aggregation["sensitivity"] == pytest.approx(0.5, rel=1e-3)

    def test_items_passthrough(self):
        """items 透传"""
        results = [
            {"idx": 0, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 1, "expected_label": False, "predicted_label": False, "is_correct": True},
        ]
        aggregation = ResultAggregator.aggregate_binary_classification_results(results)

        assert len(aggregation["items"]) == 2
        assert aggregation["items"][0]["idx"] == 0
        assert aggregation["items"][1]["idx"] == 1

    def test_all_correct(self):
        """全部正确"""
        results = [
            {"idx": i, "expected_label": True, "predicted_label": True, "is_correct": True}
            for i in range(5)
        ]
        aggregation = ResultAggregator.aggregate_binary_classification_results(results)

        assert aggregation["total_count"] == 5
        assert aggregation["confusion_matrix"]["tp"] == 5
        assert aggregation["confusion_matrix"]["fp"] == 0
        assert aggregation["confusion_matrix"]["tn"] == 0
        assert aggregation["confusion_matrix"]["fn"] == 0

    def test_all_wrong(self):
        """全部错误"""
        results = [
            {"idx": 0, "expected_label": True, "predicted_label": False, "is_correct": False},
            {"idx": 1, "expected_label": False, "predicted_label": True, "is_correct": False},
        ]
        aggregation = ResultAggregator.aggregate_binary_classification_results(results)

        assert aggregation["confusion_matrix"]["tp"] == 0
        assert aggregation["confusion_matrix"]["fn"] == 1
        assert aggregation["confusion_matrix"]["fp"] == 1
        assert aggregation["confusion_matrix"]["tn"] == 0

    def test_metrics_config_ignored(self):
        """metrics_config 参数被忽略，不影响结果"""
        results = [
            {"idx": 0, "expected_label": True, "predicted_label": True, "is_correct": True},
        ]
        aggregation_with = ResultAggregator.aggregate_binary_classification_results(results, [{"name": "x"}])
        aggregation_without = ResultAggregator.aggregate_binary_classification_results(results, None)

        assert aggregation_with["accuracy"] == aggregation_without["accuracy"]
        assert aggregation_with["confusion_matrix"] == aggregation_without["confusion_matrix"]


class TestBinaryClassificationTopLevelMetrics:
    """测试 accuracy/specificity/sensitivity 顶层指标"""

    def test_typical_scenario(self):
        """典型场景: tp=4,fp=1,tn=3,fn=2"""
        results = [
            {"idx": 0, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 1, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 2, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 3, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 4, "expected_label": False, "predicted_label": True, "is_correct": False},
            {"idx": 5, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 6, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 7, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 8, "expected_label": True, "predicted_label": False, "is_correct": False},
            {"idx": 9, "expected_label": True, "predicted_label": False, "is_correct": False},
        ]
        aggregation = ResultAggregator.aggregate_binary_classification_results(results)
        assert aggregation["accuracy"] == pytest.approx(0.7, rel=1e-3)
        assert aggregation["specificity"] == pytest.approx(0.75, rel=1e-3)
        assert aggregation["sensitivity"] == pytest.approx(0.6667, rel=1e-2)

    def test_all_negative_samples(self):
        """全负样本 - sensitivity=0.0, specificity=1.0"""
        results = [
            {"idx": 0, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 1, "expected_label": False, "predicted_label": False, "is_correct": True},
        ]
        aggregation = ResultAggregator.aggregate_binary_classification_results(results)
        assert aggregation["accuracy"] == 1.0
        assert aggregation["specificity"] == 1.0
        assert aggregation["sensitivity"] == 0.0


class TestBinaryClassificationExtendedMetrics:
    """测试新增的扩展指标"""

    def test_typical_scenario_extended(self):
        """典型场景: tp=4,fp=1,tn=3,fn=2, total=10"""
        results = [
            {"idx": 0, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 1, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 2, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 3, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 4, "expected_label": False, "predicted_label": True, "is_correct": False},
            {"idx": 5, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 6, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 7, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 8, "expected_label": True, "predicted_label": False, "is_correct": False},
            {"idx": 9, "expected_label": True, "predicted_label": False, "is_correct": False},
        ]
        agg = ResultAggregator.aggregate_binary_classification_results(results)

        # tp=4, fp=1, tn=3, fn=2
        # precision = 4/(4+1) = 0.8
        assert agg["precision"] == pytest.approx(0.8, rel=1e-3)
        # recall/sensitivity = 4/(4+2) = 0.6667
        assert agg["sensitivity"] == pytest.approx(0.6667, rel=1e-2)
        # F1 = 2*0.8*0.6667/(0.8+0.6667) ≈ 0.7273
        assert agg["f1_score"] == pytest.approx(0.7273, rel=1e-2)
        # F2 = 5*0.8*0.6667/(4*0.8+0.6667) ≈ 0.6897
        assert agg["f2_score"] == pytest.approx(0.6897, rel=1e-2)
        # F0.5 = 1.25*0.8*0.6667/(0.25*0.8+0.6667) ≈ 0.7692
        assert agg["f05_score"] == pytest.approx(0.7692, rel=1e-2)
        # NPV = 3/(3+2) = 0.6
        assert agg["npv"] == pytest.approx(0.6, rel=1e-3)
        # error_rate = 1 - 0.7 = 0.3
        assert agg["error_rate"] == pytest.approx(0.3, rel=1e-3)
        # prevalence = (4+2)/10 = 0.6
        assert agg["prevalence"] == pytest.approx(0.6, rel=1e-3)
        # youden = 0.6667 + 0.75 - 1 = 0.4167
        assert agg["youden_index"] == pytest.approx(0.4167, rel=1e-2)
        # MCC = (4*3 - 1*2) / sqrt(5*6*4*5) = 10/sqrt(600) ≈ 0.4082
        assert agg["mcc"] == pytest.approx(0.4082, rel=1e-2)

    def test_all_correct_extended(self):
        """全部正确: tp=5, fp=0, tn=0, fn=0"""
        results = [
            {"idx": i, "expected_label": True, "predicted_label": True, "is_correct": True}
            for i in range(5)
        ]
        agg = ResultAggregator.aggregate_binary_classification_results(results)

        assert agg["precision"] == 1.0
        assert agg["f1_score"] == 1.0
        assert agg["error_rate"] == 0.0
        assert agg["prevalence"] == 1.0
        # specificity=0 (no negatives), youden = 1+0-1 = 0
        assert agg["youden_index"] == 0.0

    def test_perfect_classification(self):
        """完美分类: tp=3, fp=0, tn=3, fn=0"""
        results = [
            {"idx": 0, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 1, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 2, "expected_label": True, "predicted_label": True, "is_correct": True},
            {"idx": 3, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 4, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 5, "expected_label": False, "predicted_label": False, "is_correct": True},
        ]
        agg = ResultAggregator.aggregate_binary_classification_results(results)

        assert agg["accuracy"] == 1.0
        assert agg["precision"] == 1.0
        assert agg["sensitivity"] == 1.0
        assert agg["specificity"] == 1.0
        assert agg["f1_score"] == 1.0
        assert agg["npv"] == 1.0
        assert agg["error_rate"] == 0.0
        assert agg["mcc"] == 1.0
        assert agg["youden_index"] == 1.0

    def test_no_positive_predictions(self):
        """无正预测: tp=0, fp=0, tn=3, fn=2"""
        results = [
            {"idx": 0, "expected_label": True, "predicted_label": False, "is_correct": False},
            {"idx": 1, "expected_label": True, "predicted_label": False, "is_correct": False},
            {"idx": 2, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 3, "expected_label": False, "predicted_label": False, "is_correct": True},
            {"idx": 4, "expected_label": False, "predicted_label": False, "is_correct": True},
        ]
        agg = ResultAggregator.aggregate_binary_classification_results(results)

        # precision = 0/(0+0) = 0 (div by zero guard)
        assert agg["precision"] == 0.0
        # F1 = 0 (both P and R are 0 or one is 0)
        assert agg["f1_score"] == 0.0
        # NPV = 3/(3+2) = 0.6
        assert agg["npv"] == pytest.approx(0.6, rel=1e-3)
