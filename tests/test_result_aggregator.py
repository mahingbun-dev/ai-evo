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

    def test_judge_details_dimension_feedbacks(self):
        """judge_details 投影携带 dimension_feedbacks（新裁判输出才有）"""
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
                        "dimension_scores": {"维度A": 8.0},
                        "dimension_feedbacks": {"维度A": "内容准确"},
                    }
                ],
                "feedback": "综合",
                "dimension_scores": {},
                "user_input": "",
            }
        ]
        items = aggregator._format_items(results)
        assert items[0]["judge_details"][0]["dimension_feedbacks"] == {"维度A": "内容准确"}

    def test_judge_details_dimension_feedbacks_missing_compat(self):
        """旧 judge_scores 无 dimension_feedbacks 时投影为 None，链路不报错"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "model_output": "输出",
                "average_score": 8.0,
                "judge_scores": [
                    {"score": 8.0, "feedback": "好", "dimension_scores": {"维度A": 8.0}}
                ],
                "feedback": "综合",
                "dimension_scores": {},
                "user_input": "",
            }
        ]
        items = aggregator._format_items(results)
        assert items[0]["judge_details"][0]["dimension_feedbacks"] is None


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


class TestFormatFailedItems:
    """测试 _format_failed_items 失败行上下文补齐"""

    def test_failed_item_carries_context(self):
        """失败行平铺输入变量并携带 model_output 与 row_status"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 3,
                "success": False,
                "error": "脚本执行失败",
                "model_output": "部分输出",
                "row_status": "pre_script_failed",
                "question": "什么是 FastAPI？",
                "ref_answer": "一个现代 Web 框架",
                "judge_scores": [{"score": None, "judge_failed": True}],
                "judge_details": [{"judge_index": 0, "score": None}],
            }
        ]
        items = aggregator._format_failed_items(results)
        assert len(items) == 1
        item = items[0]
        assert item["success"] is False
        assert item["error"] == "脚本执行失败"
        assert item["model_output"] == "部分输出"
        assert item["row_status"] == "pre_script_failed"
        # 输入变量平铺（与 _format_items 同口径）
        assert item["question"] == "什么是 FastAPI？"
        assert item["ref_answer"] == "一个现代 Web 框架"
        # 结构化键不平铺
        assert "judge_scores" not in item
        assert "judge_details" not in item

    def test_failed_item_defaults_and_none_skip(self):
        """缺省兜底：model_output 空串、row_status 默认 failed、None 值与下划线键不平铺"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "success": False,
                "error": "boom",
                "model_output": None,
                "internal_note": None,   # None 值 → 不平铺
                "_secret": "x",          # 下划线开头 → 不平铺
                "user_input": "输入",
            }
        ]
        items = aggregator._format_failed_items(results)
        item = items[0]
        assert item["model_output"] == ""
        assert item["row_status"] == "failed"
        assert item["user_input"] == "输入"
        assert "internal_note" not in item
        assert "_secret" not in item

    def test_failed_item_keeps_pre_script_results(self):
        """on_fail=abort 失败行保留 pre_script_results（既有行为不回归）"""
        aggregator = ResultAggregator()
        results = [
            {
                "idx": 0,
                "success": False,
                "error": "abort",
                "pre_script_results": [{"script": "s1", "success": False}],
            }
        ]
        items = aggregator._format_failed_items(results)
        assert items[0]["pre_script_results"] == [{"script": "s1", "success": False}]


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


class TestRowIndexSemantics:
    """行序号取值口径（_row_index / _format_items / _format_failed_items）

    背景：judge/agent 默认执行路径曾有 payload 缺 idx 的回归（执行器
    payload.get("idx", 0) 兜底），历史脏数据全部行 idx=0 入库；行级表
    重建行（load_execution_results）注入的 _orig_idx 是唯一可信行身份。
    """

    def setup_method(self):
        self.aggregator = ResultAggregator()

    def test_row_index_prefers_orig_idx_over_stale_idx(self):
        """行级表重建行：row_result JSON 带历史脏 idx=0，但 _orig_idx 正确——以 _orig_idx 为准"""
        from app.services.result_aggregator import _row_index

        assert _row_index({"_orig_idx": 5, "idx": 0}, 99) == 5

    def test_row_index_falls_back_to_idx(self):
        """无行身份（旧内存直调）回退 result 自带 idx"""
        from app.services.result_aggregator import _row_index

        assert _row_index({"idx": 3}, 99) == 3

    def test_row_index_falls_back_to_enumerate(self):
        """idx 也缺失时回退枚举序号"""
        from app.services.result_aggregator import _row_index

        assert _row_index({}, 7) == 7

    def test_row_index_guards_non_int_values(self):
        """_orig_idx / idx 为非整数（脏值）时逐级回退，不抛错"""
        from app.services.result_aggregator import _row_index

        assert _row_index({"_orig_idx": "3", "idx": 2}, 9) == 2
        assert _row_index({"_orig_idx": None, "idx": "x"}, 9) == 9
        assert _row_index(None, 4) == 4

    def test_format_items_uses_orig_idx_for_rebuilt_rows(self):
        """_format_items：行级表重建行（idx=0 脏数据 + _orig_idx 正确）序号不再全为 0"""
        results = [
            {"_orig_idx": 0, "idx": 0, "success": True, "model_output": "o0"},
            {"_orig_idx": 1, "idx": 0, "success": True, "model_output": "o1"},
            {"_orig_idx": 2, "idx": 0, "success": True, "model_output": "o2"},
        ]
        items = self.aggregator._format_items(results)
        assert [it["idx"] for it in items] == [0, 1, 2]

    def test_format_items_without_row_identity_keeps_legacy_behavior(self):
        """无 _orig_idx 的旧行回退 idx/枚举——既有单测与直调链路零行为变化"""
        results = [
            {"idx": 4, "success": True, "model_output": "o4"},
            {"success": True, "model_output": "o5"},
        ]
        items = self.aggregator._format_items(results)
        assert [it["idx"] for it in items] == [4, 1]

    def test_format_failed_items_uses_orig_idx(self):
        """_format_failed_items 与 _format_items 同口径"""
        results = [
            {"_orig_idx": 6, "idx": 0, "success": False, "error": "boom"},
            {"_orig_idx": 3, "idx": 0, "success": False, "error": "bang"},
        ]
        items = self.aggregator._format_failed_items(results)
        assert [it["idx"] for it in items] == [6, 3]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])