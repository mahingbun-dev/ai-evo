"""
评测执行器双引擎分派与长会话分支单元测试

覆盖：
1. judge_engine 分派：claude_agent → ClaudeCodeJudgeAgent；graph → JudgeLangGraphAgent；空值回落 graph
2. agent 评测类型：跳过被测模型调用、会话渲染注入 model_output
3. 多裁判聚合方法 _aggregate_judge_scores
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor


def _make_task(evaluation_type="judge", judge_engine="graph", judge_count=2):
    return type("Task", (), {
        "id": "task_test",
        "evaluation_type": evaluation_type,
        "judge_engine": judge_engine,
        "judge_count": judge_count,
        "model_config": {"provider": "openai"},
        "model_provider": None,
        "max_score": 10,
        "evaluation_metrics": [{"name": "维度A"}],
        "judge_variable_config": {},
        "evaluation_targets": None,
        "runtime_variables": {},
    })()


def _session_payload(session_text="# 会话记录\n用户：你好"):
    data = {"session_messages": session_text, "user_input": "你好"}
    return {
        "idx": 0,
        "user_input": "你好",
        "ref_answer": "",
        "_dataset_item": {"id": "data2_abc", "data": data},
    }


class TestAggregateJudgeScores:
    def test_average_and_dimensions(self):
        result = EvaluationExecutor._aggregate_judge_scores(
            {"idx": 0},
            [
                {"score": 8, "feedback": "好", "dimension_scores": {"A": 8, "B": 6}},
                {"score": 6, "feedback": "一般", "dimension_scores": {"A": 4}},
            ],
        )
        assert result["average_score"] == 7
        assert result["dimension_scores"]["A"] == 6
        assert result["dimension_scores"]["B"] == 6
        assert result["feedback"].count("---") == 1

    def test_empty_judge_scores(self):
        result = EvaluationExecutor._aggregate_judge_scores({"idx": 0}, [])
        assert "average_score" not in result


class TestRenderSessionFromPayload:
    def test_plain_markdown_string(self):
        """session_messages 为字符串（已渲染）时直接返回"""
        payload = _session_payload("# 会话\n内容")
        assert EvaluationExecutor._render_session_from_payload(payload) == "# 会话\n内容"

    def test_structured_record(self):
        """session_messages 为结构化 SessionRecord 时渲染 markdown"""
        session = {
            "session_id": "s1",
            "source": "claude_code",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "帮我修复bug"}], "timestamp": None},
                {"role": "assistant", "content": [{"type": "text", "text": "好的"}], "timestamp": None},
            ],
            "stats": {},
            "metadata": {},
        }
        payload = _session_payload(session)
        rendered = EvaluationExecutor._render_session_from_payload(payload)
        assert "帮我修复bug" in rendered
        assert "好的" in rendered

    def test_missing_session(self):
        payload = {"idx": 0, "_dataset_item": {"data": {"user_input": "x"}}}
        assert EvaluationExecutor._render_session_from_payload(payload) == ""


class TestEngineDispatch:
    """_evaluate_single 步骤3 分派验证（mock 两个引擎）"""

    def _run_evaluate(self, task, payload):
        executor = EvaluationExecutor(max_workers=1, task_id="task_test")
        # mock PromptBuilder 与两个引擎，验证分派路径
        with patch("app.services.prompt_builder.PromptBuilder") as mock_pb, \
             patch("app.services.variable_injector.VariableInjector") as mock_vi, \
             patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent") as mock_cc, \
             patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent") as mock_lg:
            pb_instance = mock_pb.return_value
            pb_instance.build_user_prompt_from_judge_vars.return_value = "user prompt"
            pb_instance._build_system_prompt.return_value = "system prompt"
            vi_instance = mock_vi.return_value
            vi_instance.build_judge_payload.return_value = {"user_input": "x", "_dataset_item": {}}

            mock_cc.return_value.run_parallel.return_value = [{"score": 9, "feedback": "ok", "dimension_scores": {}}]
            mock_lg.return_value.run_parallel.return_value = [{"score": 5, "feedback": "graph", "dimension_scores": {}}]
            result = executor._evaluate_single(payload, task)
        return result, mock_cc, mock_lg

    def test_claude_agent_engine(self):
        """judge_engine=claude_agent 时走 CC Agent 且不调用图引擎"""
        task = _make_task(judge_engine="claude_agent")
        # 普通任务需调用被测模型：mock ModelCallerSync
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            mock_caller.return_value.call.return_value = {"success": True, "content": "模型输出", "chain_results": []}
            result, mock_cc, mock_lg = self._run_evaluate(task, {"idx": 0, "user_input": "问题", "_dataset_item": {}})
        assert result["success"] is True
        assert mock_cc.return_value.run_parallel.called
        assert not mock_lg.return_value.run_parallel.called
        assert result["average_score"] == 9

    def test_graph_engine(self):
        """judge_engine=graph 时走图引擎（历史默认行为）"""
        task = _make_task(judge_engine="graph")
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            mock_caller.return_value.call.return_value = {"success": True, "content": "模型输出", "chain_results": []}
            result, mock_cc, mock_lg = self._run_evaluate(task, {"idx": 0, "user_input": "问题", "_dataset_item": {}})
        assert result["success"] is True
        assert mock_lg.return_value.run_parallel.called
        assert not mock_cc.return_value.run_parallel.called

    def test_empty_engine_falls_back_to_graph(self):
        """历史任务 judge_engine 为空时按 graph 处理"""
        task = _make_task(judge_engine=None)
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            mock_caller.return_value.call.return_value = {"success": True, "content": "模型输出", "chain_results": []}
            result, mock_cc, mock_lg = self._run_evaluate(task, {"idx": 0, "user_input": "问题", "_dataset_item": {}})
        assert result["success"] is True
        assert mock_lg.return_value.run_parallel.called

    def test_agent_type_skips_model_call(self):
        """agent 类型：不调用被测模型，会话内容作为 model_output"""
        task = _make_task(evaluation_type="agent", judge_engine="claude_agent")
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            result, mock_cc, mock_lg = self._run_evaluate(task, _session_payload("# 会话记录\n用户：你好"))
        # 被测模型调用器从未实例化
        assert not mock_caller.called
        assert result["success"] is True
        assert result["model_output"] == "# 会话记录\n用户：你好"
        assert mock_cc.return_value.run_parallel.called

    def test_agent_type_missing_session_raises(self):
        """agent 类型但数据缺少会话字段时报错"""
        task = _make_task(evaluation_type="agent", judge_engine="claude_agent")
        executor = EvaluationExecutor(max_workers=1, task_id="task_test")
        result = executor._evaluate_single({"idx": 0, "user_input": "x", "_dataset_item": {"data": {}}}, task)
        assert result["success"] is False
        assert "session_messages" in result["error"]
