"""终态校验器与评测执行器接线测试（M2 QE2-1，executor 步骤1.6）

复用 test_evaluation_pre_scripts_flow 的假体模式（mock 裁判栈/模型调用器）：
1. 任务配置校验器 → result.verification_result 写入（file_assert 真实执行，
   工作区锚定 SCRIPT_RUN_BASE 临时目录）；
2. 校验失败 on_fail=continue → 裁判照常执行（确定性证据不拦截流程）；
3. 校验失败 on_fail=abort → 该行 success=False、row_status=verification_failed、
   裁判未被调用；
4. 未配置校验器 → result 无 verification_result 键（旧结果结构兼容）。
"""
from unittest.mock import patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor

pytestmark = pytest.mark.unit


def _make_task(verifiers=None, evaluation_type="judge"):
    return type("Task", (), {
        "id": "task_vfy_flow",
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
        "verifiers": verifiers,
    })()


def _run_single(task, payload):
    """执行单条评测：mock 裁判栈与被测模型调用，保留校验器真实链路"""
    executor = EvaluationExecutor(max_workers=1, task_id="task_vfy_flow")
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
            "success": True, "content": "模型输出", "chain_results": []}
        result = executor._evaluate_single(payload, task)
    return result, mock_cc


@pytest.fixture(autouse=True)
def _isolated_workspaces(tmp_path, monkeypatch):
    """校验器工作区锚定临时目录（不落仓库 uploads/）"""
    monkeypatch.setattr("app.services.pre_judge_runner.SCRIPT_RUN_BASE",
                        tmp_path / "script_runs")


class TestVerifierExecutorWiring:
    def test_verifiers_recorded_and_judge_continues(self):
        """校验器失败（on_fail 缺省 continue）：证据写入、裁判照常执行"""
        task = _make_task(verifiers=[{
            "type": "file_assert", "name": "终稿校验",
            "config": {"assertions": [{"path": "outputs/final.json", "mode": "exists"}]},
        }])
        result, mock_cc = _run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is True
        assert result["average_score"] == 9
        vr = result["verification_result"]
        assert vr["status"] == "fail"  # outputs/final.json 未生成 → 断言失败
        assert vr["total"] == 1 and vr["failed"] == 1
        assert vr["results"][0]["name"] == "终稿校验"
        assert mock_cc.return_value.run_parallel.called  # 裁判未被拦截

    def test_abort_marks_row_failed_and_skips_judge(self):
        """on_fail=abort：该行标记 verification_failed、不进裁判"""
        task = _make_task(verifiers=[{
            "type": "file_assert", "name": "必过校验", "on_fail": "abort",
            "config": {"assertions": [{"path": "outputs/final.json", "mode": "exists"}]},
        }])
        result, mock_cc = _run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is False
        assert result["row_status"] == "verification_failed"
        assert "必过校验" in result["error"]
        assert not mock_cc.return_value.run_parallel.called
        # 校验证据保留（failed_items 展示定位失败断言）
        assert result["verification_result"]["aborted"] is True

    def test_pass_verifier_keeps_row_success(self, tmp_path):
        """校验通过：行正常完成，verification_result.status=pass"""
        task = _make_task(verifiers=[{
            "type": "file_assert",
            "config": {"assertions": [{"path": "inputs/model_output.txt",
                                       "mode": "regex", "pattern": "模型输出"}]},
        }])
        # 预置断言目标文件：monkeypatch build_run_dir 指向预置目录
        prepared = tmp_path / "ws"
        (prepared / "inputs").mkdir(parents=True)
        (prepared / "inputs" / "model_output.txt").write_text("模型输出 OK", encoding="utf-8")
        with patch("app.services.pre_judge_runner.build_run_dir", return_value=prepared):
            result, _ = _run_single(
                task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is True
        assert result["verification_result"]["status"] == "pass"

    def test_no_verifiers_no_key(self):
        """未配置校验器：result 不写 verification_result（旧结构兼容）"""
        result, _ = _run_single(
            _make_task(verifiers=None),
            {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is True
        assert "verification_result" not in result
