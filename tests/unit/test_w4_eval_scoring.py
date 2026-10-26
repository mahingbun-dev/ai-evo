"""W4 评测平台口径修复单元测试

覆盖两项口径修复（对应整改条目 W4）：
1. 空输入行不进任务均分与维度均分（evaluation_executor.py 空值校验分支
   average_score=None，与 judge_failed 同口径；result_aggregator.aggregate
   对 None 行自动排除，均分分母同步缩小）；
2. 权重文本映射统一收敛到 metric_scoring.WEIGHT_MAP / weight_value：
   「高/中/低」与「high/medium/low」双向支持（大小写不敏感），未知取值
   兜底 1；judge_evaluation_engine / prompt_builder / judge_graph_nodes /
   deepeval_result_transformer 四处调用点复用同一实现。

标尺核对结论（W4 第 3 条，无代码改动）：插件评测（0-100 行分）与裁判评测
（0-10 标尺）经 aggregate_for_task 按 evaluation_type 互斥分发
（evaluators/plugin.py vs evaluators/judge.py），两类分值不进同一聚合——
证据见本文件底部 test_plugin_and_judge_scales_never_mix 的注释。
"""
import pytest

from app.services.metric_scoring import WEIGHT_MAP, weight_value
from app.services.result_aggregator import ResultAggregator
from app.services.judge_evaluation_engine import JudgeEvaluationEngine
from app.services.prompt_builder import PromptBuilder
from app.services import judge_graph_nodes
from app.services.deepeval_result_transformer import DeepEvalResultTransformer

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ===========================================================================
# 1. 空输入行排除（aggregate 均值 / 分母 / 维度聚合 / 展示）
# ===========================================================================
def _task(max_score=10, metrics=None):
    return type("Task", (), {
        "max_score": max_score,
        "evaluation_metrics": metrics or [{"name": "A", "weight": 1}, {"name": "B", "weight": 1}],
    })()


def _completed_row(idx, avg, dims):
    return {
        "idx": idx, "success": True, "average_score": avg,
        "row_status": "completed", "judge_count": 1, "failed_judge_count": 0,
        "judge_scores": [{"score": avg, "feedback": "好", "dimension_scores": dims}],
        "dimension_scores": dims, "model_output": "out",
    }


def _empty_input_row(idx):
    """executor 空值校验分支的输出形态（evaluation_executor.py:680-692，W4 起 None）"""
    return {
        "idx": idx, "success": True, "average_score": None,
        "row_status": "empty_input", "judge_count": 0, "failed_judge_count": 0,
        "judge_scores": [], "dimension_scores": {},
        "feedback": "待评测内容为空，无法进行有效评估", "model_output": "",
    }


def _judge_failed_row(idx):
    return {
        "idx": idx, "success": True, "average_score": None,
        "row_status": "judge_failed", "judge_count": 6, "failed_judge_count": 6,
        "judge_scores": [], "dimension_scores": {}, "model_output": "out",
    }


class TestEmptyInputExcludedFromAverages:
    """空输入行不再当 0 分参与平均（与 judge_failed 同口径）"""

    def test_empty_input_row_not_counted_as_zero(self):
        """1 行 completed(8) + 1 行 empty_input → 任务均分 8.0（旧口径被拉低到 4.0）"""
        rows = [_completed_row(0, 8.0, {"A": 8, "B": 8}), _empty_input_row(1)]

        agg = ResultAggregator(_task()).aggregate(rows)

        # empty_input 行的 None 不进均分：8.0/1 = 8.0（W2 旧口径 (8+0)/2 = 4.0）
        assert agg["average_score"] == 8.0
        assert agg["total"] == 2
        assert agg["success_count"] == 2  # 空输入行 success=True，仍算执行成功
        assert agg["failed_judge_rows"] == 0

    def test_average_denominator_excludes_none_rows(self):
        """均值分母只数有效分行：3 有效 + 2 空输入 + 1 裁判全失败 → 9.0（分母 3 不是 6）"""
        rows = [
            _completed_row(0, 8.0, {"A": 8, "B": 8}),
            _completed_row(1, 9.0, {"A": 9, "B": 9}),
            _completed_row(2, 10.0, {"A": 10, "B": 10}),
            _empty_input_row(3),
            _empty_input_row(4),
            _judge_failed_row(5),
        ]

        agg = ResultAggregator(_task()).aggregate(rows)

        # (8+9+10)/3 = 9.0：空输入与裁判全失败都不占分母（旧口径 (27+0+0)/5 = 5.4）
        assert agg["average_score"] == 9.0
        assert agg["failed_judge_rows"] == 1
        assert agg["total"] == 6

    def test_average_score_key_present_but_none_also_excluded(self):
        """重算路径（行级表重建）：键存在但值为 None 的行同样排除

        load_execution_results 从 row_result 原文重建行（evaluation_result_service.py:
        214-256），None 键保留；aggregate() L200 按「值 is not None」过滤，
        键存在与否不影响排除语义。
        """
        row = _empty_input_row(0)
        assert "average_score" in row and row["average_score"] is None  # 键存在

        agg = ResultAggregator(_task()).aggregate([row, _completed_row(1, 6.0, {"A": 6, "B": 6})])
        assert agg["average_score"] == 6.0  # 6.0/1，None 行未以 0 分计入

    def test_dimension_aggregation_ignores_empty_input_rows(self):
        """维度聚合：空输入行（dimension_scores={}）不贡献样本，count 不含它"""
        rows = [
            _completed_row(0, 8.0, {"A": 8, "B": 6}),
            _completed_row(1, 6.0, {"A": 6, "B": 8}),
            _empty_input_row(2),
            _judge_failed_row(3),
        ]

        agg = ResultAggregator(_task()).aggregate(rows)

        dims = agg["dimension_scores"]
        # 每维度只有 2 个有效样本（空输入/裁判失败行不进维度分母）
        assert dims["A"]["count"] == 2
        assert dims["B"]["count"] == 2
        assert dims["A"]["average"] == 7.0
        assert dims["B"]["average"] == 7.0

    def test_empty_input_item_omits_score_key_and_marks_status(self):
        """列表/详情展示：空输入行 items 无 average_score 键、row_status=empty_input

        前端 Results.vue 评分列对 undefined 不渲染数字（2056 行 v-if），
        据同行 row_status=empty_input 显示「空输入」标签而非 0 分。
        """
        rows = [_completed_row(0, 8.0, {"A": 8, "B": 8}), _empty_input_row(1)]

        agg = ResultAggregator(_task()).aggregate(rows)

        by_idx = {item["idx"]: item for item in agg["items"]}
        assert by_idx[1]["row_status"] == "empty_input"
        assert "average_score" not in by_idx[1]
        assert "待评测内容为空" in by_idx[1]["feedback"]

    def test_all_rows_empty_input_average_falls_back_to_zero(self):
        """全部行都是空输入 → 无有效分，均分回退 0（分母为 0，不是被 None 拉低）"""
        agg = ResultAggregator(_task()).aggregate([_empty_input_row(0), _empty_input_row(1)])
        assert agg["average_score"] == 0


# ===========================================================================
# 2. 权重文本映射统一（metric_scoring.WEIGHT_MAP / weight_value）
# ===========================================================================
class TestUnifiedWeightMap:
    """「高/中/低」与「high/medium/low」双向支持 + 未知兜底"""

    def test_chinese_weights(self):
        """中文权重：高=3 / 中=2 / 低=1（含带空白的容错）"""
        assert weight_value("高") == 3
        assert weight_value("中") == 2
        assert weight_value("低") == 1
        assert weight_value("  高  ") == 3  # strip 容错

    def test_english_weights_case_insensitive(self):
        """英文权重：high/medium/low 双向支持，大小写不敏感"""
        assert weight_value("high") == 3
        assert weight_value("medium") == 2
        assert weight_value("low") == 1
        # judge_graph_nodes 原本地副本支持的首字母大写变体，收敛后行为保持
        assert weight_value("High") == 3
        assert weight_value("Medium") == 2
        assert weight_value("Low") == 1
        assert weight_value("HIGH") == 3
        assert weight_value("MEDIUM") == 2
        assert weight_value("LOW") == 1

    def test_unknown_weight_falls_back_to_one(self):
        """未知取值统一兜底 1（等权）"""
        for unknown in ("超高", "critical", "极高?", " HIGH! "):
            assert weight_value(unknown) == 1, unknown

    def test_numeric_weights_pass_through(self):
        """数值权重原样使用（int 保持 int，避免 prompt 文案出现 3.0）"""
        assert weight_value(3) == 3
        assert isinstance(weight_value(3), int)
        assert weight_value(2.5) == 2.5
        assert weight_value(1) == 1

    def test_weight_map_covers_both_languages(self):
        """统一映射表同时含中英文键（收敛点：metric_scoring.WEIGHT_MAP）"""
        assert WEIGHT_MAP["高"] == WEIGHT_MAP["high"] == 3
        assert WEIGHT_MAP["中"] == WEIGHT_MAP["medium"] == 2
        assert WEIGHT_MAP["低"] == WEIGHT_MAP["low"] == 1


class TestWeightCallSitesReuseUnifiedImpl:
    """四个调用点复用同一实现：同一权重写法（中/英）在各引擎产生等价结果"""

    SCORES = {"A": {"score": 9}, "B": {"score": 5}}

    def test_engine_english_weight_equals_chinese(self):
        """judge_evaluation_engine.calculate_weighted_score：英文与中文权重等价"""
        metrics_cn = [{"name": "A", "weight": "高"}, {"name": "B", "weight": "中"}]
        metrics_en = [{"name": "A", "weight": "high"}, {"name": "B", "weight": "medium"}]

        cn = JudgeEvaluationEngine.calculate_weighted_score(metrics_cn, self.SCORES, 10)
        en = JudgeEvaluationEngine.calculate_weighted_score(metrics_en, self.SCORES, 10)

        # (9*3 + 5*2) / (3+2) = 37/5 = 7.4，两种写法一致
        assert cn == pytest.approx(7.4)
        assert en == pytest.approx(7.4)

    def test_graph_nodes_english_weight_equals_chinese(self):
        """judge_graph_nodes._compute_weighted_scores：英文与中文权重等价

        且本文件不再持有本地 WEIGHT_MAP 副本（W4 收敛到 metric_scoring）。
        """
        assert not hasattr(judge_graph_nodes, "WEIGHT_MAP")

        dim_results = {"A": {"score": 9, "reasoning": "r"}, "B": {"score": 5, "reasoning": "r"}}
        total_cn, weighted_cn, _ = judge_graph_nodes._compute_weighted_scores(
            [{"name": "A", "weight": "高"}, {"name": "B", "weight": "中"}], dim_results, 10)
        total_en, weighted_en, _ = judge_graph_nodes._compute_weighted_scores(
            [{"name": "A", "weight": "high"}, {"name": "B", "weight": "medium"}], dim_results, 10)

        assert (total_cn, weighted_cn) == (total_en, weighted_en)
        assert weighted_cn / total_cn == pytest.approx(7.4)

    def test_deepeval_transformer_english_weight(self):
        """deepeval_result_transformer：英文权重同样映射（本地中文副本已删除）"""
        scores = {"m1": 0.8, "m2": 0.4}
        cn = DeepEvalResultTransformer.calculate_weighted_score(
            scores, {"m1": "高", "m2": "低"}, max_score=1.0)
        en = DeepEvalResultTransformer.calculate_weighted_score(
            scores, {"m1": "high", "m2": "low"}, max_score=1.0)

        # (0.8*3 + 0.4*1) / 4 = 0.7，两种写法一致
        assert cn == pytest.approx(0.7)
        assert en == pytest.approx(0.7)

    def _build_prompt(self, metrics):
        config = {
            "evaluation_metrics": metrics,
            "max_score": 10,
            "scene_desc": "W4 权重映射测试",
        }
        builder = PromptBuilder.from_config(config)
        return builder._build_system_prompt(
            user_input="测试问题", ref_answer="参考答案",
            model_output="模型输出", dataset_item={}, payload={},
        )

    def test_prompt_builder_english_weight_label(self):
        """prompt_builder：英文权重在 prompt 中映射出正确数值 high(3)"""
        prompt = self._build_prompt([{"name": "准确性", "weight": "high", "description": "d"}])
        assert "权重: high(3)" in prompt

    def test_prompt_builder_chinese_and_numeric_labels_unchanged(self):
        """prompt_builder：中文与数值权重的文案口径不变（高(3) / 3 而非 3.0）"""
        prompt_cn = self._build_prompt([{"name": "准确性", "weight": "高", "description": "d"}])
        assert "权重: 高(3)" in prompt_cn

        prompt_num = self._build_prompt([{"name": "准确性", "weight": 3, "description": "d"}])
        assert "权重: 3" in prompt_num
        assert "权重: 3.0" not in prompt_num


# ===========================================================================
# 3. 标尺混算核对（无混算 → 不改代码，证据固化）
# ===========================================================================
class TestScaleSeparation:
    """插件（0-100）与裁判（0-10）不进同一聚合——W4 标尺核对结论的证据固化

    - aggregate_for_task（result_aggregator.py:136-169）按 evaluation_type 查
      适配器互斥分发：plugin → evaluators/plugin.py:179-182（委托
      aggregate_plugin_results，行分 0-100 均分见 result_aggregator.py:681-683）；
      judge/agent → evaluators/judge.py:33-37（委托 ResultAggregator(task).aggregate，
      标尺取 task.max_score，缺省 10）。
    - 两类标尺的分数从不进入同一个聚合函数/同一个任务（evaluation_type 为
      任务级单值字段），因此不存在混算；本用例固化该分发行为。
    - result_aggregator.aggregate_judge_results（result_aggregator.py:414）内含
      0-100 分桶，但全仓（app/ tests/ frontend/）无任何调用方，属遗留死代码，
      不构成实际混算，按 W4 要求未改代码。
    """

    def test_plugin_and_judge_dispatch_to_different_aggregators(self):
        """plugin 与 judge 的任务级聚合互斥分发，标尺各归各"""
        task_plugin = type("Task", (), {"evaluation_type": "plugin", "max_score": 100,
                                        "evaluation_metrics": []})()
        task_judge = type("Task", (), {"evaluation_type": "judge", "max_score": 10,
                                       "evaluation_metrics": []})()

        plugin_rows = [{"idx": 0, "success": True, "average_score": 85.0,
                        "dimension_scores": {}, "badge": "good"}]
        judge_rows = [{"idx": 0, "success": True, "average_score": 8.5,
                       "row_status": "completed", "dimension_scores": {}}]

        agg_plugin = ResultAggregator.aggregate_for_task(task_plugin, plugin_rows)
        agg_judge = ResultAggregator.aggregate_for_task(task_judge, judge_rows)

        # plugin 侧 0-100 标尺（aggregate_plugin_results 输出，无 max_score 键）
        assert agg_plugin["average_score"] == 85.0
        assert "max_score" not in agg_plugin or agg_plugin.get("max_score") != 10
        # judge 侧 0-10 标尺（task.max_score 透传）
        assert agg_judge["average_score"] == 8.5
        assert agg_judge["max_score"] == 10
