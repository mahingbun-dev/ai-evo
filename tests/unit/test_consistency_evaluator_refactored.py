"""ConsistencyEvaluator 重构后单元测试"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.consistency_evaluator import ConsistencyEvaluator, ConsistencyResult

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestExtractRuntimeVariables:
    """_extract_runtime_variables 测试"""

    def test_excludes_special_keys(self):
        dataset_item = {
            "user_input": "q1",
            "ref_answer": "a1",
            "model_answer": "m1",
            "_dataset_item": {},
            "messages": [],
            "idx": 1,
            "custom_var": "hello",
            "another": 42,
        }
        ev = ConsistencyEvaluator(model_config={}, dataset_item=dataset_item)
        result = ev._extract_runtime_variables()
        assert result == {"custom_var": "hello", "another": 42}
        assert "user_input" not in result
        assert "ref_answer" not in result

    def test_empty_dataset_item(self):
        ev = ConsistencyEvaluator(model_config={})
        result = ev._extract_runtime_variables()
        assert result == {}


class TestFormatCombinedOutput:
    """_format_combined_output 测试"""

    def test_json_results(self):
        results = ['{"a": 1}', '{"b": 2}']
        ev = ConsistencyEvaluator(model_config={})
        output = ev._format_combined_output(results)
        assert "回复1:" in output
        assert "回复2:" in output
        assert '"a": 1' in output

    def test_plain_text_results(self):
        results = ['hello world', 'goodbye world']
        ev = ConsistencyEvaluator(model_config={})
        output = ev._format_combined_output(results)
        assert "hello world" in output
        assert "goodbye world" in output

    def test_mixed_format(self):
        results = ['{"json": true}', 'plain text']
        ev = ConsistencyEvaluator(model_config={})
        output = ev._format_combined_output(results)
        assert '"json": true' in output
        assert "plain text" in output


class TestAggregateChainResults:
    """_aggregate_chain_results 测试"""

    def test_basic_aggregation(self):
        all_results = [
            {"success": True, "chain_results": [
                {"content": "c1", "variables": {"v1": "a"}, "name": "step1"},
                {"content": "c2", "variables": {"v2": "x"}, "name": "step2"},
            ]},
            {"success": True, "chain_results": [
                {"content": "c1b", "variables": {"v1": "b"}, "name": "step1"},
                {"content": "c2b", "variables": {"v2": "y"}, "name": "step2"},
            ]},
            {"success": True, "chain_results": [
                {"content": "c1c", "variables": {"v1": "c"}, "name": "step1"},
                {"content": "c2c", "variables": {"v2": "z"}, "name": "step2"},
            ]},
        ]
        ev = ConsistencyEvaluator(model_config={})
        result = ev._aggregate_chain_results(all_results)
        assert len(result) == 2

        # 第一个节点
        node1 = result[0]
        assert node1["_consistency_aggregated"] is True
        assert node1["_repeat_count"] == 3
        assert "第1次: c1" in node1["content"]
        assert "第2次: c1b" in node1["content"]
        assert "第3次: c1c" in node1["content"]
        # variables 按 key 聚合
        v1_val = node1["variables"]["v1"]
        assert "第1次: a" in v1_val
        assert "第2次: b" in v1_val
        assert "第3次: c" in v1_val

    def test_empty_chain_results(self):
        ev = ConsistencyEvaluator(model_config={})
        assert ev._aggregate_chain_results([]) == []
        assert ev._aggregate_chain_results([{"success": False}]) == []

    def test_partial_failure(self):
        all_results = [
            {"success": True, "chain_results": [{"content": "ok", "variables": {}}]},
            {"success": False, "chain_results": [{"content": "fail", "variables": {}}]},
        ]
        ev = ConsistencyEvaluator(model_config={})
        result = ev._aggregate_chain_results(all_results)
        assert len(result) == 1
        assert result[0]["content"] == "ok"
        assert result[0]["_repeat_count"] == 1

    def test_list_variable_merge(self):
        all_results = [
            {"success": True, "chain_results": [{"content": "", "variables": {"tags": ["a", "b"]}}]},
            {"success": True, "chain_results": [{"content": "", "variables": {"tags": ["c"]}}]},
        ]
        ev = ConsistencyEvaluator(model_config={})
        result = ev._aggregate_chain_results(all_results)
        assert result[0]["variables"]["tags"] == ["a", "b", "c"]


class TestGetPromptBuilderConfig:
    """_get_prompt_builder_config 测试"""

    def test_returns_expected_keys(self):
        ev = ConsistencyEvaluator(
            model_config={},
            judge_variable_config={"vars": []},
            evaluation_metrics=[{"name": "准确"}],
            dataset_item={"custom": "val"},
        )
        config = ev._get_prompt_builder_config()
        assert "judge_variable_config" in config
        assert "evaluation_metrics" in config
        assert "runtime_variables" in config
        assert config["runtime_variables"] == {"custom": "val"}
        assert config["max_score"] == 10


class TestJudgeEvaluate:
    """_judge_evaluate 测试 (mocked engine)"""

    @pytest.mark.asyncio
    async def test_successful_judge(self):
        ev = ConsistencyEvaluator(
            model_config={},
            judge_variable_config={"user_input": {"source": "manual", "prompt_type": "user", "key": "user_input"}},
        )
        # Mock engine._call_model to return JSON response
        ev.engine._call_model = AsyncMock(return_value={
            "success": True,
            "content": '{"综合评分": 8.5, "综合评分原因": "good", "各维度评分": {"准确": 9}}',
        })
        assessment, content = await ev._judge_evaluate(["result1", "result2"], [
            {"success": True, "result": "result1", "chain_results": []},
            {"success": True, "result": "result2", "chain_results": []},
        ])
        assert assessment["综合评分"] == 8.5
        assert assessment["综合评分原因"] == "good"
        assert "回复1:" in content

    @pytest.mark.asyncio
    async def test_judge_with_markdown_json(self):
        ev = ConsistencyEvaluator(model_config={}, judge_variable_config={"v": {"source": "manual", "prompt_type": "user", "key": "v"}})
        ev.engine._call_model = AsyncMock(return_value={
            "success": True,
            "content": '```json\n{"综合评分": 7}\n```',
        })
        assessment, _ = await ev._judge_evaluate(["r1", "r2"], [
            {"success": True, "result": "r1", "chain_results": []},
            {"success": True, "result": "r2", "chain_results": []},
        ])
        assert assessment["综合评分"] == 7.0

    @pytest.mark.asyncio
    async def test_judge_call_failure(self):
        ev = ConsistencyEvaluator(model_config={}, judge_variable_config={"v": {"source": "manual", "prompt_type": "user", "key": "v"}})
        ev.engine._call_model = AsyncMock(return_value={"success": False, "error": "timeout"})
        assessment, _ = await ev._judge_evaluate(["r1"], [
            {"success": True, "result": "r1", "chain_results": []},
        ])
        assert assessment == {}


class TestEvaluateSingle:
    """evaluate_single 测试 (mocked engine)"""

    @pytest.mark.asyncio
    async def test_no_judge(self):
        ev = ConsistencyEvaluator(model_config={}, judge_variable_config={"v": {"source": "manual"}})
        ev.engine._call_model = AsyncMock(return_value={"success": True, "content": "test output", "chain_results": []})
        result = await ev.evaluate_single("hello", repeat_count=2, use_judge=False)
        assert result.metrics["total_calls"] == 2
        assert result.metrics["successful_calls"] == 2
        assert result.judge_assessment == {}

    @pytest.mark.asyncio
    async def test_with_judge(self):
        ev = ConsistencyEvaluator(model_config={}, judge_variable_config={"v": {"source": "manual", "prompt_type": "user", "key": "v"}})
        ev.engine._call_model = AsyncMock(return_value={
            "success": True,
            "content": "model output",
            "chain_results": [],
        })
        # For judge call, return JSON
        async def mock_call(messages, config, **kwargs):
            if messages[0].get("role") == "system":
                return {"success": True, "content": '{"综合评分": 9}'}
            return {"success": True, "content": "model output", "chain_results": []}

        ev.engine._call_model = AsyncMock(side_effect=mock_call)
        result = await ev.evaluate_single("hello", repeat_count=2, use_judge=True)
        assert result.metrics["total_calls"] == 2
        # judge assessment should be present (even if empty due to mock simplification)

    @pytest.mark.asyncio
    async def test_all_calls_fail(self):
        ev = ConsistencyEvaluator(model_config={})
        ev.engine._call_model = AsyncMock(return_value={"success": False, "error": "fail"})
        result = await ev.evaluate_single("hello", repeat_count=3, use_judge=False)
        assert result.metrics == {}  # no successful results means no metrics
        assert result.judge_assessment == {}
