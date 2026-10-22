"""轨迹匹配与评测执行器接线测试（M2 QE2-2，executor 步骤1.7）

复用 test_verifier_executor_flow 的假体模式（mock 裁判栈，agent 类型走
会话渲染路径）：
1. agent 任务配置轨迹断言 → result.trajectory_match 写入（结构化会话真实
   提取工具序列），且不拦截裁判（确定性证据独立展示）；
2. 匹配失败（missing/forbidden）行仍正常完成评测——拦截交给门禁 metric；
3. 手工导入的 markdown 会话 → status=error（轨迹不可提取，证据可见）；
4. 未配置 trajectory_config / judge 类型 → result 无 trajectory_match 键。
"""
from unittest.mock import patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor

pytestmark = pytest.mark.unit


_SESSION_RECORD = {
    "session_id": "s1",
    "source": "claude_code",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "帮我查订单"}], "timestamp": "t0"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "name": "grep", "input": {"pattern": "order_id"}},
        ], "timestamp": "t1"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ], "timestamp": "t2"},
    ],
    "stats": {"tool_call_count": 2},
}


def _make_task(trajectory_config=None, evaluation_type="agent"):
    return type("Task", (), {
        "id": "task_traj_flow",
        "evaluation_type": evaluation_type,
        "judge_engine": "claude_agent",
        "judge_count": 1,
        "model_config": {"provider": "openai"},
        "model_provider": None,
        "max_score": 10,
        "evaluation_metrics": [{"name": "维度A"}],
        "score_details": "",
        "judge_variable_config": {},
        "evaluation_targets": None,
        "runtime_variables": {},
        "variable_mappings": {},
        "variable_sources": {},
        "enable_user_import": False,
        "user_import_template": None,
        "input_field": "user_input",
        "reference_field": "answer",
        "pre_scripts": None,
        "verifiers": None,
        "trajectory_config": trajectory_config,
    })()


def _run_single(task, payload):
    executor = EvaluationExecutor(max_workers=1, task_id="task_traj_flow")
    with patch("app.services.prompt_builder.PromptBuilder") as mock_pb, \
         patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent") as mock_cc, \
         patch("app.services.claude_code_judge_agent.write_session_file",
               return_value="/tmp/fake_session.md"), \
         patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent"), \
         patch("app.services.model_caller.ModelCallerSync") as mock_caller:
        mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
        mock_pb.return_value._build_system_prompt.return_value = "system prompt"
        mock_cc.return_value.run_parallel.return_value = [
            {"score": 9, "feedback": "ok", "dimension_scores": {}}]
        mock_caller.return_value.call.return_value = {
            "success": True, "content": "unused", "chain_results": []}
        result = executor._evaluate_single(payload, task)
    return result, mock_cc


def _agent_payload(session_messages):
    return {
        "idx": 0,
        "user_input": "查订单",
        "_dataset_item": {"id": "d1", "data": {"session_messages": session_messages}},
    }


class TestTrajectoryExecutorWiring:
    def test_pass_match_recorded_and_judge_runs(self):
        """匹配通过：trajectory_match 写入（含时间线），裁判照常执行"""
        task = _make_task({"mode": "unordered", "expected": [{"tool": "grep"}]})
        result, mock_cc = _run_single(task, _agent_payload(_SESSION_RECORD))
        assert result["success"] is True
        assert result["average_score"] == 9  # 裁判均分不受轨迹影响
        tm = result["trajectory_match"]
        assert tm["status"] == "pass"
        assert tm["total_calls"] == 2
        assert [t["tool"] for t in tm["timeline"]] == ["grep", "Bash"]
        # grep 被期望覆盖；Bash 未在期望内 → 时间线标 unexpected（unordered 模式
        # 下仅记录不判失败）
        assert tm["timeline"][0]["kind"] == "covered"
        assert tm["timeline"][1]["kind"] == "unexpected"
        assert mock_cc.return_value.run_parallel.called

    def test_fail_match_does_not_block_row(self):
        """匹配失败：行仍正常完成（success=True、有裁判分），拦截交给门禁 metric"""
        task = _make_task({
            "mode": "strict",
            "expected": [{"tool": "grep"}, {"tool": "Read"}],  # Read 从未调用
        })
        result, mock_cc = _run_single(task, _agent_payload(_SESSION_RECORD))
        assert result["success"] is True
        tm = result["trajectory_match"]
        assert tm["status"] == "fail"
        assert [m["tool"] for m in tm["missing"]] == ["Read"]
        assert mock_cc.return_value.run_parallel.called

    def test_forbidden_hit_marks_fail_without_blocking(self):
        """禁用工具命中：匹配 fail（含 forbidden_hits），行不中断"""
        task = _make_task({
            "mode": "unordered",
            "expected": [{"tool": "grep"}],
            "forbidden": [{"tool": "Bash", "args_contains": ["ls"]}],
        })
        result, _ = _run_single(task, _agent_payload(_SESSION_RECORD))
        tm = result["trajectory_match"]
        assert tm["status"] == "fail"
        assert tm["forbidden_hits"][0]["tool"] == "Bash"
        assert tm["timeline"][1]["kind"] == "forbidden"

    def test_markdown_session_yields_error_status(self):
        """markdown 会话（手工导入）：status=error，证据可见、行不中断"""
        task = _make_task({"mode": "subset", "expected": [{"tool": "grep"}]})
        result, _ = _run_single(task, _agent_payload("# 渲染会话文本"))
        assert result["success"] is True
        assert result["trajectory_match"]["status"] == "error"
        assert "渲染文本" in result["trajectory_match"]["error"]

    def test_no_config_no_key(self):
        """未配置轨迹断言：result 无 trajectory_match 键（旧结构兼容）"""
        result, _ = _run_single(_make_task(None), _agent_payload(_SESSION_RECORD))
        assert result["success"] is True
        assert "trajectory_match" not in result

    def test_judge_type_never_runs_matcher(self):
        """judge 类型即便误配 trajectory_config 也不运行（接线仅 agent 类型）"""
        task = _make_task({"mode": "subset", "expected": [{"tool": "grep"}]},
                          evaluation_type="judge")
        result, _ = _run_single(task, {
            "idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is True
        assert "trajectory_match" not in result
