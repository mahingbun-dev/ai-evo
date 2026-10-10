"""评测前处理脚本与执行器/变量注入集成单元测试（设计文档 §二/§三/§七）

覆盖：
1. 执行器分派（_evaluate_single 步骤1.5）：有/无 pre_scripts、agent 与 judge 两种评测类型；
   v3 双路径：targets 含 script_output 行走 derive_script_steps 派生路径（优先），
   否则回退 task.pre_scripts 旧配置路径
2. on_fail 两种策略：continue（裁判可见失败事实继续评测）/ abort（该条数据标记执行失败）
3. script_output 变量注入：build_judge_payload dict 与 list 两种 judge_variable_config 格式
4. result 结构：pre_script_results 字段完整（script_name/status/output/files/summary/
   finish_reason/duration_ms/error），judge 与 agent 类型均生效
5. agent 会话渲染的 model_output 超长临时文件路径进入脚本 stdin.model_output
6. execute_batch 结束清理 uploads/script_runs/{task_id}/（复用 judge_sessions 清理模式）
7. evaluation_task_service create/update 的 script_output 待评测数据行校验（契约 v3 §五，
   ValueError→400 路径；create 一律置 None，update 保守语义=仅 payload 显式携带时置 None）
8. prompt_builder._resolve_judge_variable 的 script_output 分支（裁判 prompt 组装闭环）
9. @ 引用标记 overrides（契约 v3 §三）：executor 解析一次并传给 PromptBuilder
   与 build_state_input；脚本失败 continue → required 目标为空走既有空值0分逻辑
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor
from app.services.pre_judge_runner import cleanup_script_run_files
from app.services.result_aggregator import ResultAggregator
from app.services.variable_injector import (
    VariableInjector,
    resolve_script_output_value,
)


# ---------------- fake 脚本（统一子进程执行：run(stdin) 函数式脚本包） ----------------

_FLOW_OK_SRC = '''
def run(stdin):
    """成功脚本：输出 rate/detail + stdin 组装快照（stdin_echo）"""
    fields = stdin.get("fields") or {}
    return {
        "status": "ok",
        "output": {"rate": 0.87, "mismatched_count": 13, "detail": {"a": 1},
                   "stdin_echo": {"model_output": stdin.get("model_output"),
                                   "task_id": stdin.get("task_id"),
                                   "data_id": stdin.get("data_id"),
                                   "params": stdin.get("params"),
                                   "field_keys": sorted(fields.keys())}},
        "files": {},
        "summary": "86/99 一致",
        "finish_reason": "completed",
        "error": None,
    }
'''

_FLOW_FAIL_SRC = '''
def run(stdin):
    """失败脚本：标准输出 status=fail"""
    return {"status": "fail", "output": None, "files": {}, "summary": "",
            "finish_reason": "error", "error": "boom-flow"}
'''


@pytest.fixture
def fake_flow_builtins(tmp_path, monkeypatch):
    """fake 脚本落库（exec_type=script）+ 脚本包落盘，替换 runner.SessionLocal

    StaticPool 保证多个会话共享同一内存库；SCRIPT_UPLOAD_BASE 指向临时目录；
    同时迁移真实内置脚本供 builtin:xxx 历史引用解析。
    stdin 组装断言改经脚本输出 stdin_echo 回传（跨进程无法共享捕获列表）。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import Base
    from app.models.pre_judge_models import PreJudgeScript
    from app.services import pre_judge_script_service as script_service
    from app.services.pre_judge_script_service import ensure_builtin_scripts_migrated

    upload_base = tmp_path / "pre_judge_scripts"
    monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", upload_base)
    monkeypatch.setattr("app.services.pre_judge_runner.SCRIPT_RUN_BASE", tmp_path / "script_runs")

    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        for name in ("flow_ok", "flow_fail"):
            db.add(PreJudgeScript(id=f"pjs_{name}", name=name, exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
        db.commit()
        for name, source in (("flow_ok", _FLOW_OK_SRC), ("flow_fail", _FLOW_FAIL_SRC)):
            pkg = upload_base / f"pjs_{name}"
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / "main.py").write_text(source, encoding="utf-8")
        ensure_builtin_scripts_migrated(db)
    monkeypatch.setattr("app.services.pre_judge_runner.SessionLocal", Session)
    yield Session


# ---------------- 测试工具 ----------------

def _make_task(pre_scripts=None, evaluation_type="judge", judge_engine="claude_agent",
               judge_variable_config=None, evaluation_targets=None,
               score_details="", evaluation_metrics=None):
    return type("Task", (), {
        "id": "task_flow_x",
        "evaluation_type": evaluation_type,
        "judge_engine": judge_engine,
        "judge_count": 1,
        "model_config": {"provider": "openai"},
        "model_provider": None,
        "max_score": 10,
        "evaluation_metrics": evaluation_metrics if evaluation_metrics is not None else [{"name": "维度A"}],
        "score_details": score_details,
        "judge_variable_config": judge_variable_config if judge_variable_config is not None else {},
        "evaluation_targets": evaluation_targets,
        "runtime_variables": {},
        "variable_mappings": {},
        "variable_sources": {},
        "enable_user_import": False,
        "user_import_template": None,
        "input_field": "user_input",
        "reference_field": "answer",
        "pre_scripts": pre_scripts,
    })()


def _mock_judge_stack():
    """mock 裁判侧外部依赖（PromptBuilder / 双引擎），保留 VariableInjector 真实逻辑"""
    pb = patch("app.services.prompt_builder.PromptBuilder")
    cc = patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent")
    ws = patch("app.services.claude_code_judge_agent.write_session_file",
               return_value="/tmp/fake_session.md")
    lg = patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent")
    return pb, cc, ws, lg


# ==================== 1. 执行器分派：有/无 pre_scripts ====================

class TestExecutorDispatch:
    def _run_single(self, task, payload):
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws as mock_ws, lg as mock_lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            mock_lg.return_value.run_parallel.return_value = [
                {"score": 5, "feedback": "graph", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(payload, task)
        return result, mock_cc, mock_lg, mock_ws

    def test_no_pre_scripts_unchanged(self, fake_flow_builtins):
        """无 pre_scripts：不进入脚本管线，result 不含 pre_script_results（兼容旧结果结构）"""
        task = _make_task(pre_scripts=None)
        result, mock_cc, mock_lg, _ = self._run_single(
            task, {"idx": 0, "user_input": "问题", "_dataset_item": {}})
        assert result["success"] is True
        assert "pre_script_results" not in result
        assert mock_cc.return_value.run_parallel.called

    def test_with_pre_scripts_judge_type(self, fake_flow_builtins):
        """judge 类型：步骤1.5 执行脚本，result 含 pre_script_results 与脚本评分"""
        task = _make_task(pre_scripts=[{"script_id": "flow_ok", "output_var": "flow"}])
        result, mock_cc, mock_lg, _ = self._run_single(
            task, {"idx": 0, "user_input": "问题", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is True
        assert result["average_score"] == 9
        assert mock_cc.return_value.run_parallel.called
        pre = result["pre_script_results"]
        assert len(pre) == 1
        assert pre[0]["status"] == "ok"
        assert pre[0]["script_name"] == "flow_ok"
        # stdin 组装：model_output 为模型输出文本、字段与数据项 id 透传（脚本输出回传快照）
        echo = pre[0]["output"]["stdin_echo"]
        assert echo["model_output"] == "模型输出"
        assert echo["task_id"] == "task_flow_x"
        assert echo["data_id"] == "d1"

    def test_with_pre_scripts_agent_type(self, fake_flow_builtins):
        """agent 类型（会话渲染）同样生效：会话内容作为 model_output 进入脚本 stdin"""
        task = _make_task(pre_scripts=[{"script_id": "flow_ok"}],
                          evaluation_type="agent", judge_engine="claude_agent")
        payload = {
            "idx": 0,
            "user_input": "你好",
            "_dataset_item": {"id": "data2_a", "data": {"session_messages": "# 会话\n用户：你好"}},
        }
        result, mock_cc, _, _ = self._run_single(task, payload)
        assert result["success"] is True
        assert result["model_output"] == "# 会话\n用户：你好"
        assert result["pre_script_results"][0]["status"] == "ok"
        assert result["pre_script_results"][0]["output"]["stdin_echo"]["model_output"] == "# 会话\n用户：你好"
        assert mock_cc.return_value.run_parallel.called


# ==================== 2. on_fail：continue 与 abort ====================

class TestOnFailPolicy:
    def _run_single(self, task, payload):
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws, lg as mock_lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            mock_lg.return_value.run_parallel.return_value = [
                {"score": 5, "feedback": "graph", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(payload, task)
        return result, mock_cc

    def test_fail_continue_keeps_judging(self, fake_flow_builtins):
        """默认 continue：脚本失败后裁判仍执行，失败事实随 pre_script_results 入库"""
        task = _make_task(pre_scripts=[{"script_id": "flow_fail"}])
        result, mock_cc = self._run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is True
        assert result["average_score"] == 9
        assert mock_cc.return_value.run_parallel.called
        assert result["pre_script_results"][0]["status"] == "fail"
        assert "boom-flow" in result["pre_script_results"][0]["error"]

    def test_fail_abort_marks_record_failed(self, fake_flow_builtins):
        """on_fail=abort：该条数据标记执行失败，不再进入裁判评测"""
        task = _make_task(pre_scripts=[{"script_id": "flow_fail", "on_fail": "abort"}])
        result, mock_cc = self._run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is False
        assert "boom-flow" in result["error"]
        assert result["pre_script_results"][0]["status"] == "fail"
        # 裁判从未被调用
        assert not mock_cc.return_value.run_parallel.called

    def test_fail_continue_runs_following_script(self, fake_flow_builtins):
        """continue 策略下后序脚本仍执行"""
        task = _make_task(pre_scripts=[
            {"script_id": "flow_fail"},
            {"script_id": "flow_ok", "output_var": "after"},
        ])
        result, _ = self._run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is True
        statuses = [r["status"] for r in result["pre_script_results"]]
        assert statuses == ["fail", "ok"]


# ==================== 2.5 v3 双路径：targets 派生 vs legacy pre_scripts ====================

def _script_output_target(tid, script_id, display_name="比对结果", output_var=None,
                   output_key=None, required=True, **source_extra):
    """构造 source=script_output 的 evaluation_targets 行（契约 v3 §二）"""
    source_config = {"script_id": script_id}
    if output_key is not None:
        source_config["output_key"] = output_key
    if output_var is not None:
        source_config["output_var"] = output_var
    source_config.update(source_extra)
    return {
        "id": tid,
        "name": display_name,
        "display_name": display_name,
        "source": "script_output",
        "source_config": source_config,
        "required": required,
    }


def _model_target(tid="t_model", display_name="模型输出"):
    """构造 source=model_output 的 evaluation_targets 行（非脚本来源）"""
    return {
        "id": tid,
        "name": display_name,
        "display_name": display_name,
        "source": "model_output",
        "source_config": {},
        "required": True,
    }


class TestExecutorDerivedScriptPath:
    """步骤1.5 双路径分派（共享设计契约 v3 §三）"""

    def _run_single(self, task, payload):
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws, lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(payload, task)
        return result, mock_pb

    def test_targets_derived_path_runs_script(self, fake_flow_builtins):
        """targets 含 script_output 行 → derive_script_steps 派生执行（pre_scripts=None 已迁移形态）"""
        task = _make_task(
            evaluation_targets=[
                _model_target(),
                _script_output_target("t_cmp", "flow_ok", output_var="cmp_out", output_key="rate",
                               input_mapping={"msg": {"field": "note", "value_type": "inline"}},
                               params={"strict": True}, on_fail="continue"),
            ],
        )
        payload = {"idx": 0, "user_input": "q",
                   "_dataset_item": {"id": "d1", "data": {"note": "hello"}}}
        result, mock_pb = self._run_single(task, payload)

        assert result["success"] is True
        pre = result["pre_script_results"]
        assert [r["script_name"] for r in pre] == ["flow_ok"]
        assert pre[0]["status"] == "ok"
        # 派生步骤透传 source_config 的 input_mapping / params
        echo = pre[0]["output"]["stdin_echo"]
        assert echo["params"] == {"strict": True}
        assert "msg" in echo["field_keys"]
        # script_outputs 键 = output_var（judge_payload 携带）
        judge_payload = mock_pb.return_value.build_user_prompt_from_judge_vars.call_args.kwargs["payload"]
        assert judge_payload["script_outputs"]["cmp_out"]["rate"] == 0.87

    def test_derived_path_takes_precedence_over_legacy_pre_scripts(self, fake_flow_builtins):
        """targets 派生步骤优先：与 legacy pre_scripts 并存时后者不再执行"""
        task = _make_task(
            pre_scripts=[{"script_id": "flow_fail"}],
            evaluation_targets=[_script_output_target("t_cmp", "flow_ok", output_var="cmp_out",
                                               required=False)],
        )
        result, _ = self._run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})

        assert result["success"] is True
        # 仅派生的 flow_ok 执行；legacy 的 flow_fail 未执行（若执行会多一条 fail）
        assert [r["script_name"] for r in result["pre_script_results"]] == ["flow_ok"]
        assert all(r["status"] == "ok" for r in result["pre_script_results"])

    def test_non_script_targets_fall_back_to_legacy_pre_scripts(self, fake_flow_builtins):
        """targets 无 script_output 行 → 回退 task.pre_scripts 旧路径（未迁移任务兼容）"""
        task = _make_task(
            pre_scripts=[{"script_id": "flow_ok", "output_var": "legacy_out"}],
            evaluation_targets=[_model_target()],
        )
        result, mock_pb = self._run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})

        assert result["success"] is True
        assert result["pre_script_results"][0]["script_name"] == "flow_ok"
        judge_payload = mock_pb.return_value.build_user_prompt_from_judge_vars.call_args.kwargs["payload"]
        assert judge_payload["script_outputs"]["legacy_out"]["rate"] == 0.87

    def test_derived_on_fail_abort_marks_record_failed(self, fake_flow_builtins):
        """派生路径同样受 on_fail=abort 约束：该条数据标记执行失败，不进裁判"""
        task = _make_task(
            evaluation_targets=[_script_output_target("t_cmp", "flow_fail", on_fail="abort",
                                               required=False)],
        )
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb, cc as mock_cc, ws, lg:
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(
                    {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}}, task)

        assert result["success"] is False
        assert "boom-flow" in result["error"]
        assert result["pre_script_results"][0]["status"] == "fail"
        assert not mock_cc.return_value.run_parallel.called

    def test_derived_fail_continue_required_target_empty_zero_score(self, fake_flow_builtins):
        """契约 v3 §三：脚本失败（on_fail=continue）→ 行内容为空 → required=true
        走既有空值为0分逻辑（不新增校验）"""
        task = _make_task(
            evaluation_targets=[_script_output_target("t_cmp", "flow_fail", required=True)],
        )
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb, cc as mock_cc, ws, lg:
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                # 模型输出为空 + required 目标内容为空（脚本失败置空）→ 待评测内容为空
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "", "chain_results": []}
                result = executor._evaluate_single(
                    {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}}, task)

        assert result["success"] is True
        assert result["average_score"] == 0
        assert "待评测内容为空" in result["feedback"]
        assert result["pre_script_results"][0]["status"] == "fail"
        assert not mock_cc.return_value.run_parallel.called


# ==================== 2.6 @ 引用标记 overrides（契约 v3 §三） ====================

class TestRefMarkOverrides:
    """executor 统一解析 @ 标记，overrides 传给 PromptBuilder 与 build_state_input"""

    def test_overrides_resolved_and_passed_to_both_engines(self, fake_flow_builtins):
        """score_details 的 target 标记与维度等级 desc 的 var 标记均在 executor 解析一次，
        overrides 分别传入 PromptBuilder 与 build_state_input（graph 引擎）"""
        task = _make_task(
            judge_engine="graph",
            score_details="全局标准，需对照 {{ref:target:t_cmp|数据A}} 评分",
            evaluation_metrics=[{"name": "维度A", "score_criteria": {
                "high": {"desc": "达到 {{ref:var:v1|变量一}} 才算高分"}}}],
            judge_variable_config={
                "v1": {"name": "变量一", "source": "script_output",
                       "source_config": {"script": "cmp_out", "key": "rate"}},
            },
            evaluation_targets=[
                _script_output_target("t_cmp", "flow_ok", display_name="数据A",
                               output_var="cmp_out", output_key="rate"),
            ],
        )
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc, ws, lg as mock_lg, \
             patch("app.services.judge_graph_state.build_state_input") as mock_state:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_lg.return_value.run_parallel.return_value = [
                {"score": 5, "feedback": "graph", "dimension_scores": {}}]
            mock_state.return_value = {}
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(
                    {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}}, task)

        assert result["success"] is True
        # PromptBuilder 收到 overrides：target 标记 → resolve_targets 结果（脚本输出）
        pb_overrides = mock_pb.call_args.kwargs["overrides"]
        assert "【数据A】\n0.87" in pb_overrides["score_details"]
        # var 标记 → 裁判变量解析结果（judge_payload 中 v1 的已解析值）
        metric_desc = pb_overrides["evaluation_metrics"][0]["score_criteria"]["high"]["desc"]
        assert "【变量一】\n0.87" in metric_desc
        # build_state_input 收到同一份 overrides
        state_overrides = mock_state.call_args.kwargs["overrides"]
        assert state_overrides["score_details"] == pb_overrides["score_details"]
        assert state_overrides["evaluation_metrics"] == pb_overrides["evaluation_metrics"]
        # 标记不残留
        assert "{{ref:" not in pb_overrides["score_details"]

    def test_overrides_without_marks_returns_copy(self, fake_flow_builtins):
        """无标记任务：overrides 仍产出副本并传递（PromptBuilder 收到 overrides 关键字）"""
        task = _make_task(score_details="普通全局标准，无标记")
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws, lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(
                    {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}}, task)

        assert result["success"] is True
        pb_overrides = mock_pb.call_args.kwargs["overrides"]
        assert pb_overrides["score_details"] == "普通全局标准，无标记"


# ==================== 3. script_output 变量注入 ====================

SCRIPT_OUTPUTS = {"allure_vs_cases": {"consistency_rate": 0.87, "summary_text": "86/99 一致",
                                      "per_case": [{"name": "c1"}]}}


class TestScriptOutputInjection:
    def test_dict_format_top_level_script_key(self):
        """dict 格式：顶层 {script, key} 写法（设计文档 §三 示例）"""
        task = _vi_task(judge_variable_config={
            "allure_rate": {"name": "一致率", "source": "script_output",
                            "script": "allure_vs_cases", "key": "consistency_rate"},
        })
        injector = VariableInjector(task, script_outputs=SCRIPT_OUTPUTS)
        payload = injector.build_judge_payload({"idx": 0}, "模型输出")
        assert payload["allure_rate"] == "0.87"

    def test_dict_format_source_config(self):
        """dict 格式：source_config 携带 {script, key}"""
        task = _vi_task(judge_variable_config={
            "allure_rate": {"name": "一致率", "source": "script_output",
                            "source_config": {"script": "allure_vs_cases", "key": "consistency_rate"}},
        })
        injector = VariableInjector(task, script_outputs=SCRIPT_OUTPUTS)
        payload = injector.build_judge_payload({"idx": 0}, "out")
        assert payload["allure_rate"] == "0.87"

    def test_whole_output_when_key_absent(self):
        """未指定 key 时整体取 output（设计文档 §三「或整体取」）"""
        task = _vi_task(judge_variable_config={
            "allure_compare": {"name": "比对", "source": "script_output",
                               "source_config": {"script": "allure_vs_cases"}},
        })
        injector = VariableInjector(task, script_outputs=SCRIPT_OUTPUTS)
        payload = injector.build_judge_payload({"idx": 0}, "out")
        assert json.loads(payload["allure_compare"]) == SCRIPT_OUTPUTS["allure_vs_cases"]

    def test_list_format_source_config(self):
        """list 格式：{key: 变量名, source_config: {script, key}}"""
        task = _vi_task(judge_variable_config=[
            {"key": "allure_summary", "name": "比对摘要", "source": "script_output",
             "source_config": {"script": "allure_vs_cases", "key": "summary_text"}},
        ])
        injector = VariableInjector(task, script_outputs=SCRIPT_OUTPUTS)
        payload = injector.build_judge_payload({"idx": 0}, "out")
        assert payload["allure_summary"] == "86/99 一致"

    def test_list_format_top_level_key_not_used_as_output_key(self):
        """list 格式顶层 key 是变量名本身，不得被当作脚本输出键（防呆）"""
        task = _vi_task(judge_variable_config=[
            {"key": "allure_compare", "name": "比对", "source": "script_output",
             "source_config": {"script": "allure_vs_cases"}},
        ])
        injector = VariableInjector(task, script_outputs=SCRIPT_OUTPUTS)
        payload = injector.build_judge_payload({"idx": 0}, "out")
        # 整体取 output，而不是按变量名 allure_compare 找键
        assert json.loads(payload["allure_compare"]) == SCRIPT_OUTPUTS["allure_vs_cases"]

    def test_missing_script_output_blank_with_warning(self, caplog):
        """脚本失败/未编排：变量置空并告警，不抛异常"""
        task = _vi_task(judge_variable_config={
            "x": {"name": "X", "source": "script_output",
                  "source_config": {"script": "ghost_script", "key": "k"}},
        })
        injector = VariableInjector(task, script_outputs={})
        with caplog.at_level("WARNING"):
            payload = injector.build_judge_payload({"idx": 0}, "out")
        assert payload["x"] == ""
        assert any("ghost_script" in r.message for r in caplog.records)

    def test_missing_output_key_blank_with_warning(self, caplog):
        """脚本输出缺少指定键：置空并告警"""
        task = _vi_task(judge_variable_config={
            "x": {"name": "X", "source": "script_output",
                  "source_config": {"script": "allure_vs_cases", "key": "no_such_key"}},
        })
        injector = VariableInjector(task, script_outputs=SCRIPT_OUTPUTS)
        with caplog.at_level("WARNING"):
            payload = injector.build_judge_payload({"idx": 0}, "out")
        assert payload["x"] == ""
        assert any("no_such_key" in r.message for r in caplog.records)

    def test_value_type_normalization(self):
        """dict/list 序列化为 JSON；bool 转小写；None 置空"""
        outputs = {"s": {"d": {"k": 1}, "l": [1, 2], "b": True, "n": None}}
        assert resolve_script_output_value(outputs, {"source_config": {"script": "s", "key": "d"}}, "v") == '{"k": 1}'
        assert resolve_script_output_value(outputs, {"source_config": {"script": "s", "key": "l"}}, "v") == "[1, 2]"
        assert resolve_script_output_value(outputs, {"source_config": {"script": "s", "key": "b"}}, "v") == "true"
        assert resolve_script_output_value(outputs, {"source_config": {"script": "s", "key": "n"}}, "v") == ""

    def test_judge_payload_carries_script_outputs_and_files(self):
        """judge_payload 附带完整 script_outputs（供 prompt 组装解析）"""
        task = _vi_task()
        injector = VariableInjector(task, script_outputs={"s": {"k": "v"}})
        payload = injector.build_judge_payload({"idx": 0}, "out")
        assert payload["script_outputs"] == {"s": {"k": "v"}}


def _vi_task(judge_variable_config=None):
    """VariableInjector 所需最小 task 对象"""
    return SimpleNamespace(
        runtime_variables={}, variable_mappings={}, variable_sources={},
        enable_user_import=False, user_import_template=None,
        input_field="user_input", reference_field="answer",
        judge_variable_config=judge_variable_config if judge_variable_config is not None else {},
    )


# ==================== 4. result 结构含 pre_script_results ====================

class TestResultStructure:
    def test_pre_script_results_field_complete(self, fake_flow_builtins):
        """单条 result 的 pre_script_results 条目字段完整（§六 结果结构）"""
        task = _make_task(pre_scripts=[{"script_id": "flow_ok", "output_var": "flow"}])
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws, lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(
                    {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}}, task)
        entry = result["pre_script_results"][0]
        for key in ("script_id", "script_name", "status", "output", "files",
                    "summary", "finish_reason", "error", "duration_ms"):
            assert key in entry
        assert entry["output"]["rate"] == 0.87
        assert entry["output"]["mismatched_count"] == 13
        assert entry["output"]["detail"] == {"a": 1}
        assert isinstance(entry["duration_ms"], int)

    def test_aggregated_items_carry_pre_script_results(self):
        """聚合：_format_items 保留 pre_script_results（进入 task.result 与 record.result）"""
        aggregator = ResultAggregator()
        results = [{
            "idx": 0, "success": True, "model_output": "out",
            "average_score": 8, "judge_scores": [{"score": 8, "feedback": "f"}],
            "pre_script_results": [{
                "script_id": "builtin:allure_vs_cases", "script_name": "allure_vs_cases",
                "status": "ok", "output": {"consistency_rate": 0.87}, "files": {},
                "summary": "86/99 一致", "finish_reason": "completed", "error": None,
                "duration_ms": 12,
            }],
        }]
        aggregated = aggregator.aggregate(results)
        item = aggregated["items"][0]
        assert item["pre_script_results"][0]["script_name"] == "allure_vs_cases"
        assert item["pre_script_results"][0]["status"] == "ok"
        assert item["pre_script_results"][0]["output"] == {"consistency_rate": 0.87}

    def test_aggregated_items_without_pre_scripts_unchanged(self):
        """回归：无 pre_script_results 的旧结果聚合结构不变（不新增空键）"""
        aggregator = ResultAggregator()
        aggregated = aggregator.aggregate([{"idx": 0, "success": True, "model_output": "o",
                                            "average_score": 5, "judge_scores": []}])
        assert "pre_script_results" not in aggregated["items"][0]

    def test_failed_items_keep_pre_script_results_on_abort(self):
        """on_fail=abort 失败条目保留脚本结果，便于定位失败脚本"""
        aggregator = ResultAggregator()
        pre = [{"script_name": "flow_fail", "status": "fail", "error": "boom"}]
        aggregated = aggregator.aggregate([{"idx": 0, "success": False,
                                            "error": "boom", "pre_script_results": pre}])
        assert aggregated["failed_items"][0]["pre_script_results"] == pre


# ==================== 5. agent 超长 model_output 临时文件路径进脚本 stdin ====================

class TestLongSessionModelOutputPath:
    def test_long_session_path_in_script_stdin(self, fake_flow_builtins, tmp_path):
        """agent 会话渲染 >30000 字符时：落盘临时文件，路径写入 payload.model_output
        并经脚本 stdin.model_output 传递；同一文件复用给 CC 裁判，不重复写盘"""
        session_markdown = "# 会话记录\n" + "内容行内容行内容\n" * 3500  # > 30000 字符
        task = _make_task(pre_scripts=[{"script_id": "flow_ok"}],
                          evaluation_type="agent", judge_engine="claude_agent")
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        session_file_path = str(tmp_path / "session.md")

        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, \
             patch("app.services.claude_code_judge_agent.write_session_file",
                   return_value=session_file_path) as mock_ws, lg as mock_lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": session_markdown, "chain_results": []}
                payload = {
                    "idx": 0, "user_input": "你好",
                    "_dataset_item": {"id": "data2_a", "data": {"session_messages": session_markdown}},
                }
                result = executor._evaluate_single(payload, task)

        assert result["success"] is True
        # 严格拷贝语义：超长会话直接落盘到该条工作区 inputs/model_output.md，
        # stdin.model_output 是工作区内路径；同一文件复用给 CC 裁判（不重复落盘）
        ws_session_file = tmp_path / "script_runs" / "task_flow_x" / "data2_a" / "inputs" / "model_output.md"
        stdin_model_output = result["pre_script_results"][0]["output"]["stdin_echo"]["model_output"]
        assert stdin_model_output == str(ws_session_file)
        assert mock_ws.call_count == 0  # 不再写 judge_sessions 任务级临时目录
        assert mock_cc.return_value.run_parallel.call_args.kwargs.get("session_file") == str(ws_session_file)
        # 该条评测完成后工作区（含 model_output.md）随评完即清
        assert not ws_session_file.exists()

    def test_short_session_keeps_text_in_stdin(self, fake_flow_builtins):
        """短会话不落盘：stdin.model_output 仍是会话文本"""
        task = _make_task(pre_scripts=[{"script_id": "flow_ok"}],
                          evaluation_type="agent", judge_engine="claude_agent")
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws as mock_ws, lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "# 会话\n短内容", "chain_results": []}
                payload = {
                    "idx": 0, "user_input": "你好",
                    "_dataset_item": {"id": "data2_a", "data": {"session_messages": "# 会话\n短内容"}},
                }
                result = executor._evaluate_single(payload, task)
        assert result["success"] is True
        assert result["pre_script_results"][0]["output"]["stdin_echo"]["model_output"] == "# 会话\n短内容"
        assert not mock_ws.called


# ==================== 6. execute_batch 结束清理 script_runs ====================

class TestScriptRunCleanup:
    def test_cleanup_script_run_files(self, tmp_path):
        """cleanup_script_run_files 删除任务级产物目录，目录不存在时 no-op"""
        import os

        from app.services import pre_judge_runner

        task_dir = tmp_path / "script_runs" / "task_clean_x"
        (task_dir / "data_1").mkdir(parents=True)
        (task_dir / "data_1" / "artifact.txt").write_text("x", encoding="utf-8")
        with patch.object(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs"):
            cleanup_script_run_files("task_clean_x")
            assert not (tmp_path / "script_runs" / "task_clean_x").exists()
            # 不存在的任务目录 no-op
            cleanup_script_run_files("task_never_ran")
        assert os.path.isdir(tmp_path / "script_runs")

    def test_execute_batch_cleans_script_runs(self, fake_flow_builtins, tmp_path):
        """execute_batch 结束清理 uploads/script_runs/{task_id}/（复用 judge_sessions 清理模式）"""
        from app.services import pre_judge_runner

        task = _make_task(pre_scripts=[{
            "script_id": "flow_ok",
            "input_mapping": {"msg": {"field": "note", "value_type": "inline"}},
        }])
        payload = {"idx": 0, "user_input": "q", "note": "hello",
                   "_dataset_item": {"id": "d1", "data": {"note": "hello"}}}
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")

        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws, lg as mock_lg, \
             patch("app.services.evaluation_task_service.EvaluationTaskService"):
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                with patch.object(pre_judge_runner, "SCRIPT_RUN_BASE",
                                  tmp_path / "script_runs"):
                    results = executor.execute_batch([payload], task)

        assert len(results) == 1 and results[0]["success"] is True
        # 脚本产物目录在批处理结束后被清理
        assert not (tmp_path / "script_runs" / "task_flow_x").exists()


# ==================== 7. graph 引擎 user_variables 的 script_output 分支 ====================

class TestGraphEngineUserVariables:
    def test_graph_user_variables_script_output(self, fake_flow_builtins):
        """graph 引擎：judge_variable_config（dict）中 script_output 变量注入 user_variables"""
        task = _make_task(
            pre_scripts=[{"script_id": "flow_ok", "output_var": "allure_compare"}],
            judge_engine="graph",
            judge_variable_config={
                "sc_var": {"name": "比对一致率", "source": "script_output",
                           "source_config": {"script": "allure_compare", "key": "rate"}},
            },
        )
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        captured = {}
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc, ws, lg as mock_lg, \
             patch("app.services.judge_graph_state.build_state_input") as mock_state:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_lg.return_value.run_parallel.return_value = [
                {"score": 5, "feedback": "graph", "dimension_scores": {}}]

            def _capture_state(task, resolved_targets_content="", user_variables=None, overrides=None):
                captured["user_variables"] = user_variables
                captured["overrides"] = overrides
                return {"user_variables": user_variables or []}

            mock_state.side_effect = _capture_state
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(
                    {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}}, task)

        assert result["success"] is True
        user_vars = {v["name"]: v["value"] for v in captured["user_variables"]}
        assert user_vars["比对一致率"] == "0.87"


# ==================== 8. prompt_builder 的 script_output 分支 ====================

class TestPromptBuilderScriptOutput:
    def _prompt_builder(self, judge_variable_config):
        from app.services.prompt_builder import PromptBuilder

        task = SimpleNamespace(
            judge_prompt="指令", evaluation_scene="", scene_desc="",
            evaluation_metrics=[], max_score=10, score_details="", steps=[],
            judge_variable_config=judge_variable_config, runtime_variables={},
            evaluation_targets=None,
        )
        return PromptBuilder(task)

    def test_list_format_resolves_from_payload_script_outputs(self):
        """build_user_prompt_from_judge_vars 主路径：list 格式 script_output 从 payload 取值"""
        pb = self._prompt_builder([
            {"key": "allure_summary", "name": "比对摘要", "source": "script_output",
             "source_config": {"script": "allure_vs_cases", "key": "summary_text"},
             "prompt_type": "user"},
        ])
        payload = {"script_outputs": SCRIPT_OUTPUTS}
        value, _ = pb._resolve_judge_variable(
            {"key": "allure_summary", "name": "比对摘要", "source": "script_output",
             "source_config": {"script": "allure_vs_cases", "key": "summary_text"}},
            payload, "模型输出", [],
        )
        assert value == "86/99 一致"

    def test_dict_format_resolves_top_level_script_key(self):
        """dict 格式：顶层 {script, key}（设计文档 §三 示例）经 prompt 组装可解析"""
        config = {"allure_rate": {"name": "一致率", "source": "script_output",
                                  "script": "allure_vs_cases", "key": "consistency_rate",
                                  "prompt_type": "user"}}
        pb = self._prompt_builder(config)
        value, _ = pb._resolve_judge_variable(
            config["allure_rate"], {"script_outputs": SCRIPT_OUTPUTS}, "模型输出", [])
        assert value == "0.87"

    def test_user_prompt_includes_script_output_variable(self):
        """user_prompt 组装含脚本输出变量（设计文档 §八：prompt 注入 ${allure_compare}）"""
        pb = self._prompt_builder([
            {"key": "allure_summary", "name": "比对摘要", "source": "script_output",
             "source_config": {"script": "allure_vs_cases", "key": "summary_text"},
             "prompt_type": "user"},
        ])
        prompt = pb.build_user_prompt_from_judge_vars(
            payload={"script_outputs": SCRIPT_OUTPUTS}, model_output="模型输出", chain_results=[])
        assert "比对摘要" in prompt and "86/99 一致" in prompt


# ==================== 9. 任务服务 script_output 待评测数据校验（契约 v3 §五） ====================

def _service_with_db(script_rows=None, field_metas=None, items=None):
    """构造 EvaluationTaskService：mock DB 脚本查询（单次全量）、数据集字段元数据与数据项

    db.query 按 ORM 模型名分组：PreJudgeScript 查询（.all()，含停用记录）返回 script_rows；
    其他模型查询返回空。
    """
    from app.services.evaluation_task_service import EvaluationTaskService

    db = MagicMock()

    def _query_side_effect(model):
        model_name = getattr(model, "__name__", str(model))
        q = MagicMock()
        if model_name == "PreJudgeScript":
            q.all.return_value = script_rows or []
        else:
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query_side_effect
    service = EvaluationTaskService(db)

    patchers = [
        patch("app.services.dataset_service.DatasetDataService.get_data_list_v2",
              return_value=items or []),
        patch("app.services.dataset_field_service.DatasetFieldService.get_field_metadata",
              return_value=field_metas or []),
    ]
    for p in patchers:
        p.start()
    return service, db, patchers


def _stop_patchers(patchers):
    for p in patchers:
        p.stop()



def _seeded_allure_row():
    """模拟补种后的 allure 脚本 DB 行（output_schema 带键，供 output_key/on_fail 校验用例）"""
    return SimpleNamespace(id="pjs_builtin_allure_vs_cases", name="allure_vs_cases",
                           exec_type="script", output_schema={"consistency_rate": "number"},
                           is_active=True)

def _script_target(script_id="builtin:allure_vs_cases", **config_overrides):
    """构造 script_output 来源的待评测数据行（config_overrides 覆盖 source_config）"""
    source_config = {
        "script_id": script_id,
        "output_key": None,
        "input_mapping": {},
        "params": {},
        "on_fail": "continue",
        "output_var": None,
        **config_overrides,
    }
    return {
        "id": f"t-{script_id}",
        "name": "script_output",
        "source": "script_output",
        "source_config": source_config,
        "display_name": "脚本产物",
        "required": True,
    }


class TestTaskServiceScriptTargetsValidation:
    def teardown_method(self):
        pass

    def test_create_task_valid_script_target(self):
        """合法 script_output 行（builtin 脚本 + 数据集字段存在）→ 创建成功；pre_scripts/steps 写 None"""
        row = SimpleNamespace(id="pjs_1", name="allure_vs_cases", exec_type="script",
                              output_schema=None, is_active=True)
        service, db, patchers = _service_with_db(
            script_rows=[row],
            field_metas=[{"name": "cases_file"}, {"name": "allure_results"}],
        )
        try:
            task = service.create_task(
                name="t", evaluation_type="judge", dataset_id="ds_1",
                evaluation_targets=[
                    {"id": "t_model", "name": "model_output", "source": "model_output",
                     "source_config": {}, "display_name": "模型输出", "required": True},
                    _script_target(input_mapping={
                        "cases_file": {"field": "cases_file", "value_type": "local"},
                    }, output_var="allure_compare"),
                ],
                # 旧字段仅兼容接收：即使传入也一律写 None（targets 为唯一事实源）
                pre_scripts=[{"script_id": "builtin:allure_vs_cases"}],
                steps=["旧版步骤"],
            )
            assert db.add.called
            assert task.pre_scripts is None
            assert task.steps is None
            assert task.evaluation_targets[1]["source"] == "script_output"
        finally:
            _stop_patchers(patchers)

    def test_create_task_unknown_script_rejected(self):
        """脚本不存在 → ValueError（API 层 400）"""
        service, db, patchers = _service_with_db(script_rows=[])
        try:
            with pytest.raises(ValueError, match="不存在"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id="ds_1",
                    evaluation_targets=[
                        {"id": "t_model", "name": "model_output", "source": "model_output",
                         "source_config": {}, "display_name": "模型输出", "required": True},
                        _script_target(script_id="pjs_ghost"),
                    ],
                )
        finally:
            _stop_patchers(patchers)

    def test_create_task_illegal_exec_type_rejected(self):
        """exec_type 非法 → ValueError"""
        row = SimpleNamespace(id="pjs_bad", name="bad", exec_type="shell_exec",
                              output_schema=None, is_active=True)
        service, db, patchers = _service_with_db(script_rows=[row])
        try:
            with pytest.raises(ValueError, match="执行类型非法"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id="ds_1",
                    evaluation_targets=[
                        {"id": "t_model", "name": "model_output", "source": "model_output",
                         "source_config": {}, "display_name": "模型输出", "required": True},
                        _script_target(script_id="pjs_bad"),
                    ],
                )
        finally:
            _stop_patchers(patchers)

    def test_create_task_inactive_script_rejected(self):
        """脚本已停用 → ValueError"""
        row = SimpleNamespace(id="pjs_off", name="offline", exec_type="script",
                              output_schema=None, is_active=False)
        service, db, patchers = _service_with_db(script_rows=[row])
        try:
            with pytest.raises(ValueError, match="已停用"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id="ds_1",
                    evaluation_targets=[
                        {"id": "t_model", "name": "model_output", "source": "model_output",
                         "source_config": {}, "display_name": "模型输出", "required": True},
                        _script_target(script_id="pjs_off"),
                    ],
                )
        finally:
            _stop_patchers(patchers)

    def test_create_task_bad_output_key_rejected(self):
        """output_key 不在脚本输出键内 → ValueError（builtin: 历史引用按名称解析到 DB 行）"""
        service, db, patchers = _service_with_db(script_rows=[_seeded_allure_row()])
        try:
            with pytest.raises(ValueError, match="不在脚本"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id="ds_1",
                    evaluation_targets=[
                        {"id": "t_model", "name": "model_output", "source": "model_output",
                         "source_config": {}, "display_name": "模型输出", "required": True},
                        _script_target(output_key="nope_key"),
                    ],
                )
        finally:
            _stop_patchers(patchers)

    def test_create_task_missing_dataset_field_rejected(self):
        """input_mapping 字段不在数据集中 → ValueError"""
        row = SimpleNamespace(id="pjs_1", name="allure_vs_cases", exec_type="script",
                              output_schema=None, is_active=True)
        service, db, patchers = _service_with_db(
            script_rows=[row],
            field_metas=[{"name": "other_field"}],
        )
        try:
            with pytest.raises(ValueError, match="不在数据集字段中"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id="ds_1",
                    evaluation_targets=[
                        {"id": "t_model", "name": "model_output", "source": "model_output",
                         "source_config": {}, "display_name": "模型输出", "required": True},
                        _script_target(input_mapping={"cases_file": {"field": "cases_file"}}),
                    ],
                )
        finally:
            _stop_patchers(patchers)

    def test_create_task_bad_on_fail_rejected(self):
        """on_fail 取值非法 → ValueError（builtin: 历史引用按名称解析到 DB 行）"""
        service, db, patchers = _service_with_db(script_rows=[_seeded_allure_row()])
        try:
            with pytest.raises(ValueError, match="on_fail"):
                service.create_task(
                    name="t", evaluation_type="judge", dataset_id=None,
                    evaluation_targets=[
                        {"id": "t_model", "name": "model_output", "source": "model_output",
                         "source_config": {}, "display_name": "模型输出", "required": True},
                        _script_target(on_fail="ignore"),
                    ],
                )
        finally:
            _stop_patchers(patchers)

    def test_create_task_without_targets_rejected(self):
        """judge 类型未配置待评测数据 → ValueError（契约 v3 §五）"""
        service, db, patchers = _service_with_db()
        try:
            with pytest.raises(ValueError, match="待评测数据"):
                service.create_task(name="普通任务", evaluation_type="judge", dataset_id=None)
        finally:
            _stop_patchers(patchers)

    def test_update_task_validates_script_targets(self):
        """update_task 对 script_output 行同样校验"""
        service, db, patchers = _service_with_db(script_rows=[])
        task = SimpleNamespace(
            id="task_1", evaluation_type="judge", dataset_id=None,
            evaluation_targets=[{"id": "t_model", "display_name": "模型输出"}],
            consistency_config=None, score_details=None, evaluation_metrics=None,
            judge_variable_config=None,
        )
        service.get_task = MagicMock(return_value=task)
        try:
            with pytest.raises(ValueError, match="不存在"):
                service.update_task("task_1", evaluation_targets=[
                    {"id": "t_model", "name": "model_output", "source": "model_output",
                     "source_config": {}, "display_name": "模型输出", "required": True},
                    _script_target(script_id="pjs_ghost"),
                ])
        finally:
            _stop_patchers(patchers)

    def test_update_task_keeps_legacy_fields_when_not_carried(self):
        """update_task 保守语义：payload 未携带 pre_scripts/steps 时保留存量 legacy 值（不误清）"""
        row = SimpleNamespace(id="pjs_1", name="allure_vs_cases", exec_type="script",
                              output_schema=None, is_active=True)
        service, db, patchers = _service_with_db(script_rows=[row])
        legacy_pre_scripts = [{"script_id": "builtin:allure_vs_cases"}]
        legacy_steps = ["旧版步骤"]
        task = SimpleNamespace(
            id="task_1", evaluation_type="judge", dataset_id=None,
            evaluation_targets=[{"id": "t_model", "display_name": "模型输出"}],
            consistency_config=None, score_details=None, evaluation_metrics=None,
            judge_variable_config=None,
            pre_scripts=legacy_pre_scripts,
            steps=legacy_steps,
        )
        service.get_task = MagicMock(return_value=task)
        try:
            service.update_task("task_1", name="新名称")
            assert db.commit.called
            assert task.pre_scripts is legacy_pre_scripts
            assert task.steps is legacy_steps
        finally:
            _stop_patchers(patchers)

    def test_update_task_writes_none_when_legacy_fields_carried(self):
        """update_task 保守语义：payload 显式携带 pre_scripts/steps 时置 None（targets 为唯一事实源）"""
        row = SimpleNamespace(id="pjs_1", name="allure_vs_cases", exec_type="script",
                              output_schema=None, is_active=True)
        service, db, patchers = _service_with_db(script_rows=[row])
        task = SimpleNamespace(
            id="task_1", evaluation_type="judge", dataset_id=None,
            evaluation_targets=[{"id": "t_model", "display_name": "模型输出"}],
            consistency_config=None, score_details=None, evaluation_metrics=None,
            judge_variable_config=None,
            pre_scripts=[{"script_id": "builtin:allure_vs_cases"}],
            steps=["旧版步骤"],
        )
        service.get_task = MagicMock(return_value=task)
        try:
            # 前端 v3 保存链路：归一化进 targets 后显式携带 null 清掉 legacy 字段
            service.update_task("task_1", name="新名称", pre_scripts=None, steps=None)
            assert db.commit.called
            assert task.pre_scripts is None
            assert task.steps is None
        finally:
            _stop_patchers(patchers)


# ==================== 7. 每条数据评测完成即清临时工作区（核心要求） ====================

class TestPerItemWorkspaceCleanup:
    """核心要求：每条数据评测即建临时工作区，该条评测完成（含失败/abort 路径）即清；
    execute_batch 结束的任务级清理保留为兜底。"""

    def _run_single(self, task, payload):
        executor = EvaluationExecutor(max_workers=1, task_id="task_flow_x")
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws as mock_ws, lg as mock_lg:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            mock_lg.return_value.run_parallel.return_value = [
                {"score": 5, "feedback": "graph", "dimension_scores": {}}]
            with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
                mock_caller.return_value.call.return_value = {
                    "success": True, "content": "模型输出", "chain_results": []}
                result = executor._evaluate_single(payload, task)
        return result

    def test_item_workspace_removed_after_eval(self, fake_flow_builtins, tmp_path):
        """正常路径：_evaluate_single 返回后本条工作区 uploads/script_runs/{task_id}/{data_id}/ 已清"""
        task = _make_task(pre_scripts=[{"script_id": "flow_ok", "output_var": "flow"}])
        payload = {"idx": 0, "user_input": "问题", "_dataset_item": {"id": "d1", "data": {}}}
        result = self._run_single(task, payload)
        assert result["success"] is True
        # 评测期间脚本确实建区并写过产物（inline 输入落盘），评完即清
        assert not (tmp_path / "script_runs" / "task_flow_x" / "d1").exists()

    def test_item_workspace_removed_on_abort(self, fake_flow_builtins, tmp_path):
        """on_fail=abort 提前返回路径同样触发 finally 清理"""
        task = _make_task(pre_scripts=[{"script_id": "flow_fail", "on_fail": "abort"}])
        payload = {"idx": 1, "user_input": "问题", "_dataset_item": {"id": "d2", "data": {}}}
        result = self._run_single(task, payload)
        assert result["success"] is False
        assert not (tmp_path / "script_runs" / "task_flow_x" / "d2").exists()

    def test_cleanup_item_run_files_keeps_siblings(self, tmp_path):
        """cleanup_item_run_files 只清指定条目目录，兄弟条目保留；目录不存在 no-op"""
        import os

        from app.services import pre_judge_runner

        base = tmp_path / "script_runs" / "task_unit_x"
        (base / "data_1" / "inputs").mkdir(parents=True)
        (base / "data_1" / "inputs" / "msg.txt").write_text("x", encoding="utf-8")
        (base / "data_2").mkdir(parents=True)
        (base / "data_2" / "artifact.txt").write_text("y", encoding="utf-8")
        with patch.object(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs"):
            pre_judge_runner.cleanup_item_run_files("task_unit_x", "data_1")
            assert not (base / "data_1").exists()
            assert (base / "data_2" / "artifact.txt").is_file()
            # 不存在的条目 no-op
            pre_judge_runner.cleanup_item_run_files("task_unit_x", "data_never")
        assert os.path.isdir(tmp_path / "script_runs" / "task_unit_x")

    def test_cleanup_session_files_for_item(self, tmp_path, monkeypatch):
        """cleanup_session_files_for_item 只清该条会话临时文件，其他条目保留"""
        import os

        from app.services.claude_code_judge_agent import cleanup_session_files_for_item

        monkeypatch.chdir(tmp_path)
        base = tmp_path / "uploads" / "judge_sessions"
        base.mkdir(parents=True)
        (base / "task_s_x_d1_abc123.md").write_text("s1", encoding="utf-8")
        (base / "task_s_x_d2_def456.md").write_text("s2", encoding="utf-8")
        cleanup_session_files_for_item("task_s_x", "d1")
        assert not (base / "task_s_x_d1_abc123.md").exists()
        assert (base / "task_s_x_d2_def456.md").is_file()
        # 目录不存在 no-op
        cleanup_session_files_for_item("task_never", "d1")
