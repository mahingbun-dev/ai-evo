"""agent 任务「轨迹断言 + 终态校验器 + 门禁」全链路组合测试（方案 §2.5 验收）

单测层已分别覆盖三段：轨迹匹配执行接线（test_trajectory_executor_flow）、
校验器执行接线（test_verifier_executor_flow）、门禁指标提取（test_gate_semantics /
test_trajectory_matcher / test_verifier_runner 的手工 aggregates 用例）。本文件把
三段串起来——agent 任务同时配置轨迹断言与终态校验器（+ 门禁阈值行）：

1. **拦截链**：会话 trace 缺失期望工具（轨迹 fail）+ 工作区缺终态文件
   （校验 fail），而裁判打分虚高（average_score=9，「截断 trace 虚高」形态）
   → 聚合产出 trajectory_match_rate=0 与 verification_pass_rate=0 双指标
   → evaluate_gate（all 模式，双指标 ≥1 + 均分 ≥8）判 **failed**——确定性
   证据拦下裁判给高分的回归；
2. **放行链（对照）**：期望工具已调用 + 终态文件预置 → 双 rate=1.0，
   同一阈值下 verdict=**passed**。

链路与生产完全同构：executor._evaluate_single（真实轨迹提取/校验器执行，
仅 mock 裁判栈）→ ResultAggregator.aggregate_for_task（complete/stop 共用
单点）→ gate_service.evaluate_gate（收尾同款调用）。
"""
from unittest.mock import patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor
from app.services.gate_service import evaluate_gate
from app.services.result_aggregator import ResultAggregator

pytestmark = pytest.mark.unit


# 会话 trace：调用了 grep 与 Bash（structured 会话，轨迹可提取）
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


def _make_task(trajectory_config, verifiers):
    return type("Task", (), {
        "id": "task_chain",
        "evaluation_type": "agent",
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
        "verifiers": verifiers,
        "trajectory_config": trajectory_config,
        "gate_config": {
            "enabled": True,
            "mode": "all",
            "thresholds": [
                {"metric": "total_score", "op": ">=", "value": 8},
                {"metric": "verification_pass_rate", "op": ">=", "value": 1},
                {"metric": "trajectory_match_rate", "op": ">=", "value": 1},
            ],
        },
        "trials": None,
        "judge_guard_disabled": False,
    })()


def _run_row(task, payload):
    """执行单条 agent 行：真实轨迹提取/校验器链路，mock 裁判栈（打 9 分）"""
    executor = EvaluationExecutor(max_workers=1, task_id=task.id)
    with patch("app.services.prompt_builder.PromptBuilder") as mock_pb, \
         patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent") as mock_cc, \
         patch("app.services.claude_code_judge_agent.write_session_file",
               return_value="/tmp/fake_session.md"), \
         patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent"), \
         patch("app.services.model_caller.ModelCallerSync"):
        mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
        mock_pb.return_value._build_system_prompt.return_value = "system prompt"
        # 裁判对（截断/劣质）会话虚高打分——正是确定性证据要拦的形态
        mock_cc.return_value.run_parallel.return_value = [
            {"score": 9, "feedback": "ok", "dimension_scores": {}}]
        return executor._evaluate_single(payload, task)


def _agent_payload():
    return {
        "idx": 0,
        "user_input": "查订单",
        "_dataset_item": {"id": "d1", "data": {"session_messages": _SESSION_RECORD}},
    }


@pytest.fixture(autouse=True)
def _isolated_workspaces(tmp_path, monkeypatch):
    """校验器工作区锚定临时目录（不落仓库 uploads/）"""
    monkeypatch.setattr("app.services.pre_judge_runner.SCRIPT_RUN_BASE",
                        tmp_path / "script_runs")


class TestAgentEvidenceGateChain:
    def test_deterministic_evidence_blocks_inflated_judge_score(self):
        """轨迹缺失期望工具 + 终态文件缺失，裁判却打 9 分 → 门禁 failed"""
        task = _make_task(
            # strict：期望 grep + Read，会话只调了 grep+Bash → Read missing=fail
            trajectory_config={"mode": "strict",
                               "expected": [{"tool": "grep"}, {"tool": "Read"}]},
            # 工作区没有 outputs/final.json → file_assert fail（continue 不拦行）
            verifiers=[{
                "type": "file_assert", "name": "终稿校验",
                "config": {"assertions": [{"path": "outputs/final.json", "mode": "exists"}]},
            }],
        )
        row = _run_row(task, _agent_payload())
        # 行级证据：双确定性证据 fail，但行正常完成且裁判虚高
        assert row["success"] is True
        assert row["average_score"] == 9
        assert row["trajectory_match"]["status"] == "fail"
        assert row["verification_result"]["status"] == "fail"

        # 聚合（complete/stop 共用单点）：双 rate 同时产出
        aggregated = ResultAggregator.aggregate_for_task(task, [row])
        assert aggregated["average_score"] == 9
        assert aggregated["verification_pass_rate"] == 0.0
        assert aggregated["trajectory_match_rate"] == 0.0

        # 门禁判定（收尾同款调用）：all 模式下双确定性指标未达标 → failed
        gate = evaluate_gate(task.gate_config, aggregated)
        assert gate["verdict"] == "failed"
        by_metric = {c["metric"]: c for c in gate["checks"]}
        assert by_metric["total_score"]["passed"] is True       # 裁判分达标
        assert by_metric["verification_pass_rate"]["passed"] is False  # 但证据拦下
        assert by_metric["trajectory_match_rate"]["passed"] is False
        assert by_metric["trajectory_match_rate"]["actual"] == 0.0

    def test_passing_evidence_yields_passed_verdict(self, tmp_path):
        """对照：期望工具已调用 + 终态文件预置 → 双 rate=1.0，门禁 passed"""
        task = _make_task(
            trajectory_config={"mode": "unordered", "expected": [{"tool": "grep"}]},
            verifiers=[{
                "type": "file_assert",
                "config": {"assertions": [{"path": "inputs/model_output.txt",
                                           "mode": "regex", "pattern": "查订单"}]},
            }],
        )
        # 预置校验目标文件（锚定 build_run_dir 指向预置工作区）
        prepared = tmp_path / "ws_pass"
        (prepared / "inputs").mkdir(parents=True)
        (prepared / "inputs" / "model_output.txt").write_text("查订单 完成", encoding="utf-8")
        with patch("app.services.pre_judge_runner.build_run_dir", return_value=prepared):
            row = _run_row(task, _agent_payload())
        assert row["success"] is True
        assert row["trajectory_match"]["status"] == "pass"
        assert row["verification_result"]["status"] == "pass"

        aggregated = ResultAggregator.aggregate_for_task(task, [row])
        assert aggregated["verification_pass_rate"] == 1.0
        assert aggregated["trajectory_match_rate"] == 1.0

        gate = evaluate_gate(task.gate_config, aggregated)
        assert gate["verdict"] == "passed"
        assert all(c["passed"] for c in gate["checks"])
