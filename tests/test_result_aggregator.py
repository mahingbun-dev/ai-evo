"""
结果聚合服务单元测试
"""
import pytest
import sys
import os

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.result_aggregator import ResultAggregator


class TestFormatItemsJudgeDetails:
    """测试 _format_items 方法的 judge_details 字段"""

    def test_multi_judge_scenario(self):
        """多裁判场景: judge_scores 包含多个裁判时，judge_details 长度正确，索引从0开始"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "model_output": "模型输出1",
                "average_score": 8.75,
                "judge_scores": [
                    {"score": 8.5, "feedback": "反馈1"},
                    {"score": 9.0, "feedback": "反馈2"},
                ],
                "feedback": "综合反馈",
                "dimension_scores": {},
                "user_input": "输入1",
            }
        ]
        items = aggregator._format_items(results)

        assert len(items) == 1
        assert len(items[0]["judge_details"]) == 2
        assert items[0]["judge_details"][0]["judge_index"] == 0
        assert items[0]["judge_details"][0]["score"] == 8.5
        assert items[0]["judge_details"][1]["judge_index"] == 1
        assert items[0]["judge_details"][1]["score"] == 9.0

    def test_no_judge_scenario(self):
        """无裁判场景: judge_scores 为空数组时，judge_details 也为空数组"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "model_output": "模型输出1",
                "average_score": 0,
                "judge_scores": [],
                "feedback": "无反馈",
                "dimension_scores": {},
                "user_input": "输入1",
            }
        ]
        items = aggregator._format_items(results)

        assert len(items) == 1
        assert items[0]["judge_details"] == []

    def test_single_judge_with_name(self):
        """单裁判场景: judge_scores 包含 evaluation_target_name 时，judge_details 使用该名称"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "model_output": "模型输出1",
                "average_score": 7,
                "judge_scores": [
                    {"score": 7, "evaluation_target_name": "GPT-4", "feedback": "中"}
                ],
                "feedback": "反馈",
                "dimension_scores": {},
                "user_input": "输入1",
            }
        ]
        items = aggregator._format_items(results)

        assert len(items) == 1
        assert len(items[0]["judge_details"]) == 1
        assert items[0]["judge_details"][0]["evaluation_target_name"] == "GPT-4"

    def test_judge_details_structure(self):
        """验证 judge_details 包含所有必要字段"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "model_output": "输出",
                "average_score": 8.0,
                "judge_scores": [
                    {
                        "score": 8.0,
                        "feedback": "好",
                        "dimension_scores": {"维度A": 8.0, "维度B": 7.5},
                        "evaluation_target_name": "Model-X",
                    }
                ],
                "feedback": "综合",
                "dimension_scores": {},
                "user_input": "",
            }
        ]
        items = aggregator._format_items(results)

        jd = items[0]["judge_details"][0]
        assert "judge_index" in jd
        assert "score" in jd
        assert "feedback" in jd
        assert "dimension_scores" in jd
        assert "evaluation_target_name" in jd
        assert jd["dimension_scores"] == {"维度A": 8.0, "维度B": 7.5}


class TestFormatItemsModelRawResponse:
    """测试 _format_items 方法的 model_raw_response 和 input_data 字段"""

    def test_model_raw_response_included(self):
        """验证 model_raw_response 字段被正确包含在格式化结果中"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "model_output": "模型输出内容",
                "model_raw_response": "原始响应内容",
                "input_data": "输入数据内容",
                "average_score": 8.0,
                "judge_scores": [{"score": 8.0, "feedback": "好"}],
                "feedback": "综合反馈",
                "dimension_scores": {},
            }
        ]
        items = aggregator._format_items(results)

        assert len(items) == 1
        assert items[0]["model_raw_response"] == "原始响应内容"
        assert items[0]["input_data"] == "输入数据内容"
        assert items[0]["model_output"] == "模型输出内容"

    def test_model_raw_response_default_empty(self):
        """验证 model_raw_response 缺失时默认为空字符串"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "model_output": "输出",
                "average_score": 5.0,
                "judge_scores": [],
                "feedback": "",
                "dimension_scores": {},
            }
        ]
        items = aggregator._format_items(results)

        assert items[0]["model_raw_response"] == ""
        assert items[0]["input_data"] == ""


class TestExportBatchJudgeExpand:
    """测试 batch 导出时 judge_details 展开逻辑"""

    def test_export_batch_judge_expand(self):
        """judge 类型 2 裁判展开导出: 同一题目展开为2行，题目序号相同，裁判序号不同"""
        import io
        import pandas as pd
        from app.services.result_aggregator import ResultExporter

        results = [
            {
                "items": [
                    {
                        "idx": 0,
                        "model_output": "out",
                        "average_score": 8.75,
                        "judge_count": 2,
                        "judge_details": [
                            {"judge_index": 0, "score": 8.5, "feedback": "好", "dimension_scores": {"维度A": 8.5}},
                            {"judge_index": 1, "score": 9.0, "feedback": "很好", "dimension_scores": {"维度A": 9.0}},
                        ],
                        "feedback": "combined",
                        "dimension_scores": {},
                        "user_input": "",
                    }
                ],
                "evaluation_type": "judge",
            }
        ]
        excel_bytes = ResultExporter.export_to_excel(results, "batch")
        df = pd.read_excel(io.BytesIO(excel_bytes))

        assert len(df) == 2
        assert df["题目序号"].tolist() == [1, 1]
        assert df["裁判序号"].tolist() == [1, 2]
        assert df["裁判评分"].tolist() == [8.5, 9.0]

    def test_export_batch_judge_no_judges(self):
        """无裁判 judge 类型导出: judge_details=[] 时导出行数=1，无裁判序号列"""
        import io
        import pandas as pd
        from app.services.result_aggregator import ResultExporter

        results = [
            {
                "items": [
                    {
                        "idx": 0,
                        "model_output": "out",
                        "average_score": 0,
                        "judge_count": 0,
                        "judge_details": [],
                        "feedback": "no feedback",
                        "dimension_scores": {},
                        "user_input": "",
                    }
                ],
                "evaluation_type": "judge",
            }
        ]
        excel_bytes = ResultExporter.export_to_excel(results, "batch")
        df = pd.read_excel(io.BytesIO(excel_bytes))

        assert len(df) == 1
        # 无裁判时 judge expansion 不触发，通用 row 无裁判序号列
        assert "裁判序号" not in df.columns

    def test_export_batch_consistency_not_affected(self):
        """非 judge 类型（consistency）导出: 不受 judge_details 影响，导出行数与 items 数量一致"""
        import io
        import pandas as pd
        from app.services.result_aggregator import ResultExporter

        results = [
            {
                "items": [
                    {
                        "idx": 0,
                        "model_output": "out1",
                        "average_score": 0,
                        "judge_count": 0,
                        "judge_details": [],
                        "feedback": "fb1",
                        "metrics": {"text_similarity": 0.95, "format_consistency": 0.9, "structure_consistency": 0.88},
                        "dimension_scores": {},
                        "user_input": "",
                    },
                    {
                        "idx": 1,
                        "model_output": "out2",
                        "average_score": 0,
                        "judge_count": 0,
                        "judge_details": [],
                        "feedback": "fb2",
                        "metrics": {"text_similarity": 0.85, "format_consistency": 0.8, "structure_consistency": 0.75},
                        "dimension_scores": {},
                        "user_input": "",
                    },
                ],
                "evaluation_type": "consistency",
            }
        ]
        excel_bytes = ResultExporter.export_to_excel(results, "batch")
        df = pd.read_excel(io.BytesIO(excel_bytes))

        assert len(df) == 2
        assert df["题目序号"].tolist() == [1, 2]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])