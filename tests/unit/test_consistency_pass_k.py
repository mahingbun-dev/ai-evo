"""consistency / success_rate 的 pass^k 升级与聚合测试（M2 QE2-3）

- ConsistencyEvaluator.evaluate_single：mock 被测模型逐次成功/失败，
  行级 metrics 产出 pass@1 / pass^k / 标准差（手算对照）；
- SuccessRateEvaluator.evaluate_single：同口径（mock ModelCaller）；
- ResultAggregator.aggregate_consistency_results：任务级 pass_k 块手算对照
  + 旧版结果（无 pass 字段）聚合结构不变。
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.services.consistency_evaluator import ConsistencyEvaluator
from app.services.result_aggregator import ResultAggregator
from app.services.success_rate_evaluator import SuccessRateEvaluator

pytestmark = pytest.mark.unit


class TestConsistencyPassK:
    @pytest.mark.asyncio
    async def test_row_metrics_hand_computed(self):
        # 手算：5 次重复 [成,成,败,成,成] → pass@1 = 4/5 = 0.8；
        # 有失败 → pass^k = 0；二值分数 std（样本 n-1）= sqrt(0.2) ≈ 0.4472
        evaluator = ConsistencyEvaluator(model_config={"provider": "openai"})
        with patch.object(
            evaluator.engine, "_call_model",
            new=AsyncMock(side_effect=[
                {"success": True, "content": "a", "chain_results": []},
                {"success": True, "content": "b", "chain_results": []},
                {"success": False, "content": "", "chain_results": []},
                {"success": True, "content": "c", "chain_results": []},
                {"success": True, "content": "d", "chain_results": []},
            ]),
        ):
            result = await evaluator.evaluate_single("问题", repeat_count=5, use_judge=False)

        metrics = result.metrics
        assert metrics["trials"] == 5
        assert metrics["pass_count"] == 4
        assert metrics["pass_at_1"] == 0.8
        assert metrics["pass_pow_k"] == 0
        assert metrics["score_std"] == 0.4472
        assert metrics["score_sem"] == round(0.4472135955 / 5 ** 0.5, 4)

    @pytest.mark.asyncio
    async def test_all_pass_row(self):
        evaluator = ConsistencyEvaluator(model_config={"provider": "openai"})
        with patch.object(
            evaluator.engine, "_call_model",
            new=AsyncMock(return_value={"success": True, "content": "x", "chain_results": []}),
        ):
            result = await evaluator.evaluate_single("问题", repeat_count=3, use_judge=False)
        assert result.metrics["pass_at_1"] == 1.0
        assert result.metrics["pass_pow_k"] == 1
        assert result.metrics["score_std"] == 0.0

    def test_aggregate_hand_computed(self):
        # 手算：2 行 pass@1 = [1.0, 0.8]，pass^k = [1, 0]
        # → 任务级平均 pass@1 = 0.9，平均 pass^k = 0.5，全过 1 行
        results = [
            {"idx": 0, "success": True,
             "metrics": {"text_similarity": 1, "pass_at_1": 1.0, "pass_pow_k": 1, "trials": 3}},
            {"idx": 1, "success": True,
             "metrics": {"text_similarity": 0.5, "pass_at_1": 0.8, "pass_pow_k": 0, "trials": 5}},
        ]
        agg = ResultAggregator.aggregate_consistency_results(results)
        assert agg["pass_k"]["rows"] == 2
        assert agg["pass_k"]["average_pass_at_1"] == 0.9
        assert agg["pass_k"]["average_pass_pow_k"] == 0.5
        assert agg["pass_k"]["all_pass_rows"] == 1
        # 顶层便捷口径（结果卡片直读）
        assert agg["average_pass_at_1"] == 0.9
        assert agg["average_pass_pow_k"] == 0.5
        # 行明细透传 pass^k 字段（旧结果无该组键时为 None）
        assert agg["items"][0]["metrics"]["pass_at_1"] == 1.0
        assert agg["items"][1]["metrics"]["pass_pow_k"] == 0

    def test_aggregate_legacy_rows_no_pass_k(self):
        # 旧版一致性结果（无 pass 字段）：聚合不写 pass_k 键（结构不变）
        results = [
            {"idx": 0, "success": True, "metrics": {"text_similarity": 0.9}},
        ]
        agg = ResultAggregator.aggregate_consistency_results(results)
        assert "pass_k" not in agg
        assert "average_pass_at_1" not in agg
        assert agg["items"][0]["metrics"]["pass_at_1"] is None


class TestSuccessRatePassK:
    @pytest.mark.asyncio
    async def test_row_metrics_hand_computed(self):
        # 手算：4 次重复 [成,败,成,败] → success_rate = pass@1 = 0.5；
        # 未全过 → pass^k = 0
        evaluator = SuccessRateEvaluator(model_config={"provider": "openai"})
        with patch(
            "app.services.model_caller.ModelCaller.call",
            new=AsyncMock(side_effect=[
                {"success": True, "content": "ok", "error": ""},
                {"success": False, "content": "", "error": "超时"},
                {"success": True, "content": "ok", "error": ""},
                {"success": False, "content": "", "error": "限流"},
            ]),
        ):
            result = await evaluator.evaluate_single("问题", repeat_count=4)

        assert result.metrics["trials"] == 4
        assert result.metrics["pass_count"] == 2
        assert result.metrics["success_rate"] == 0.5
        assert result.metrics["pass_at_1"] == 0.5
        assert result.metrics["pass_pow_k"] == 0

    def test_aggregate_pass_k_block(self):
        results = [
            {"idx": 0, "success": True,
             "metrics": {"success_rate": 1.0, "pass_at_1": 1.0, "pass_pow_k": 1, "total_calls": 3, "successful_calls": 3}},
            {"idx": 1, "success": True,
             "metrics": {"success_rate": 0.5, "pass_at_1": 0.5, "pass_pow_k": 0, "total_calls": 4, "successful_calls": 2}},
        ]
        agg = ResultAggregator.aggregate_success_rate_results(results)
        assert agg["pass_k"]["rows"] == 2
        assert agg["pass_k"]["average_pass_at_1"] == 0.75
        assert agg["pass_k"]["average_pass_pow_k"] == 0.5
        assert agg["pass_k"]["all_pass_rows"] == 1

    def test_aggregate_legacy_rows_no_pass_k(self):
        results = [
            {"idx": 0, "success": True, "metrics": {"success_rate": 1.0, "total_calls": 2, "successful_calls": 2}},
        ]
        agg = ResultAggregator.aggregate_success_rate_results(results)
        assert "pass_k" not in agg
