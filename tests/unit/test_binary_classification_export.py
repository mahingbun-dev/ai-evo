"""
二分分类评测导出单元测试
"""
import pytest
import sys
import os

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.result_aggregator import ResultExporter


class TestBuildBinaryClassificationSummary:
    """测试 _build_binary_classification_summary 方法"""

    def test_summary_with_valid_data(self):
        """有完整数据时，返回正确的统计摘要"""
        result_data = {
            "total_count": 100,
            "accuracy": 0.85,
            "precision": 0.875,
            "sensitivity": 0.8235,
            "specificity": 0.8824,
            "f1_score": 0.8485,
            "f2_score": 0.835,
            "f05_score": 0.861,
            "mcc": 0.7,
            "npv": 0.8235,
            "error_rate": 0.15,
            "prevalence": 0.5,
            "youden_index": 0.71,
            "confusion_matrix": {
                "tp": 42,
                "fp": 6,
                "tn": 43,
                "fn": 9,
            },
        }
        summary = ResultExporter._build_binary_classification_summary(result_data)

        # 验证总行数：15 个指标 + 1 个空行 + 1 个标题 + 4 个混淆矩阵 = 21
        assert len(summary) == 21

        # 验证关键指标
        assert summary[0] == {"指标": "总样本数", "值": 100}
        assert summary[1] == {"指标": "正确数", "值": 85}
        assert summary[2] == {"指标": "错误数", "值": 15}
        assert summary[3] == {"指标": "准确率 (Accuracy)", "值": "85.00%"}
        assert summary[4] == {"指标": "精确率 (Precision)", "值": "87.50%"}

        # 验证混淆矩阵
        assert summary[16] == {"指标": "混淆矩阵", "值": "数量"}
        assert summary[17] == {"指标": "TP (真正例)", "值": 42}
        assert summary[18] == {"指标": "FP (假正例)", "值": 6}
        assert summary[19] == {"指标": "TN (真负例)", "值": 43}

    def test_summary_with_empty_data(self):
        """空数据时，返回提示信息"""
        summary = ResultExporter._build_binary_classification_summary(None)
        assert len(summary) == 1
        assert summary[0] == {"指标": "无数据", "值": "-"}

    def test_summary_with_zero_values(self):
        """所有指标为 0 时，不报错"""
        result_data = {
            "total_count": 0,
            "accuracy": 0,
            "precision": 0,
            "sensitivity": 0,
            "specificity": 0,
            "f1_score": 0,
            "f2_score": 0,
            "f05_score": 0,
            "mcc": 0,
            "npv": 0,
            "error_rate": 0,
            "prevalence": 0,
            "youden_index": 0,
            "confusion_matrix": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
        }
        summary = ResultExporter._build_binary_classification_summary(result_data)
        assert len(summary) == 21
        assert summary[3] == {"指标": "准确率 (Accuracy)", "值": "0.00%"}


class TestExportBinaryClassificationExcel:
    """测试二分分类评测 Excel 导出"""

    def test_export_creates_two_sheets(self):
        """导出应生成两个 Sheet：统计摘要和逐条明细"""
        import pandas as pd
        import io

        results = [{
            "total_count": 4,
            "accuracy": 0.75,
            "precision": 1.0,
            "sensitivity": 0.5,
            "specificity": 1.0,
            "f1_score": 0.6667,
            "f2_score": 0.5556,
            "f05_score": 0.8333,
            "mcc": 0.5774,
            "npv": 0.6667,
            "error_rate": 0.25,
            "prevalence": 0.5,
            "youden_index": 0.5,
            "confusion_matrix": {"tp": 1, "fp": 0, "tn": 2, "fn": 1},
            "items": [
                {"idx": 0, "expected_label": True, "predicted_label": True, "is_correct": True, "duration": 0.1},
                {"idx": 1, "expected_label": True, "predicted_label": False, "is_correct": False, "duration": 0.2},
                {"idx": 2, "expected_label": False, "predicted_label": False, "is_correct": True, "duration": 0.15},
                {"idx": 3, "expected_label": False, "predicted_label": False, "is_correct": True, "duration": 0.12},
            ],
        }]

        excel_bytes = ResultExporter.export_to_excel(results, "binary_classification")

        # 验证返回的是有效的 Excel 文件
        assert isinstance(excel_bytes, bytes)
        assert len(excel_bytes) > 0

        # 读取 Excel 验证 Sheet 结构
        df_dict = pd.read_excel(io.BytesIO(excel_bytes), sheet_name=None)
        assert "统计摘要" in df_dict
        assert "逐条明细" in df_dict

    def test_export_summary_content(self):
        """统计摘要 Sheet 应包含正确的指标"""
        import pandas as pd
        import io

        results = [{
            "total_count": 2,
            "accuracy": 0.5,
            "precision": 0.5,
            "sensitivity": 0.5,
            "specificity": 0.5,
            "f1_score": 0.5,
            "f2_score": 0.5,
            "f05_score": 0.5,
            "mcc": 0.0,
            "npv": 0.5,
            "error_rate": 0.5,
            "prevalence": 0.5,
            "youden_index": 0.0,
            "confusion_matrix": {"tp": 1, "fp": 1, "tn": 0, "fn": 0},
            "items": [],
        }]

        excel_bytes = ResultExporter.export_to_excel(results, "binary_classification")
        df_summary = pd.read_excel(io.BytesIO(excel_bytes), sheet_name="统计摘要")

        # 验证摘要包含关键指标
        assert "总样本数" in df_summary["指标"].values
        assert "准确率 (Accuracy)" in df_summary["指标"].values
        assert "TP (真正例)" in df_summary["指标"].values

    def test_export_detail_content(self):
        """逐条明细 Sheet 应包含正确的数据行"""
        import pandas as pd
        import io

        results = [{
            "total_count": 2,
            "accuracy": 0.5,
            "precision": 0.5,
            "sensitivity": 0.5,
            "specificity": 0.5,
            "f1_score": 0.5,
            "f2_score": 0.5,
            "f05_score": 0.5,
            "mcc": 0.0,
            "npv": 0.5,
            "error_rate": 0.5,
            "prevalence": 0.5,
            "youden_index": 0.0,
            "confusion_matrix": {"tp": 1, "fp": 1, "tn": 0, "fn": 0},
            "items": [
                {"idx": 0, "expected_label": True, "predicted_label": True, "is_correct": True, "duration": 0.1},
                {"idx": 1, "expected_label": False, "predicted_label": True, "is_correct": False, "duration": 0.2},
            ],
        }]

        excel_bytes = ResultExporter.export_to_excel(results, "binary_classification")
        df_detail = pd.read_excel(io.BytesIO(excel_bytes), sheet_name="逐条明细")

        # 验证明细包含 2 行数据
        assert len(df_detail) == 2
        # 验证列名
        assert "序号" in df_detail.columns
        assert "期望标签" in df_detail.columns
        assert "预测标签" in df_detail.columns
        assert "判定结果" in df_detail.columns
