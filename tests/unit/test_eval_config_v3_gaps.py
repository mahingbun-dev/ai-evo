"""共享设计契约 v3 缺口补充测试（独立测试方复核产出）

开发侧测试（test_reference_mark_resolver / test_eval_target_script_source /
test_pre_judge_runner / test_evaluation_pre_scripts_flow / test_task_validation_refs /
test_config_migration / test_instruction_builder_steps / test_prompt_builder_from_config /
tests/test_judge_graph_state / tests/test_judge_graph_nodes）已覆盖契约验收点主干
（标记三类路径、script_output 目标解析、derive_script_steps 去重行序、executor 双路径、
三类 400 校验、迁移规则与幂等、两引擎步骤派生与 overrides 优先），本文件只补复核中
发现的剩余缺口：

1. claude_agent 引擎「## 待评测目标内容」段的 script_output 行注入 —— 缺陷暴露
   （xfail(strict=True)，转修复员，修复后翻转提醒移除标记）
2. executor 步骤1.5 的「targets 无 script_output 行且无 legacy pre_scripts」跳过分支
   （v3 新任务形态：只配 model_output 行不应误入脚本管线），以及 targets 派生路径
   传给 run_pre_scripts 的 steps 参数形态（非 None、与 derive_script_steps 一致）
3. @ 引用标记 vars 上下文对 list 格式 judge_variable_config 的支持
   （EvaluationExecutor._judge_variable_keys 的 list 分支 + executor 集成解析）
4. POST /evaluation/tasks/{task_id}/migrate-config 端点 happy path（含幂等重放）
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor
from app.services.prompt_builder import PromptBuilder

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------- 公共工具 ----------------

def _model_target(tid="t_model", display_name="模型输出"):
    """source=model_output 的待评测数据行"""
    return {
        "id": tid, "name": display_name, "display_name": display_name,
        "source": "model_output", "source_config": {}, "required": True,
    }


def _script_target(tid="t_cmp", script_id="flow_ok", display_name="比对结果",
                   output_var="cmp_out", output_key="rate", required=True):
    """source=script_output 的待评测数据行（契约 v3 §二）"""
    return {
        "id": tid, "name": display_name, "display_name": display_name,
        "source": "script_output", "required": required,
        "source_config": {"script_id": script_id, "output_var": output_var,
                          "output_key": output_key},
    }


_FLOW_OK_SRC = '''
def run(stdin):
    """成功的前处理脚本（统一子进程执行）：输出 rate=0.87"""
    return {
        "status": "ok",
        "output": {"rate": 0.87, "detail": {"a": 1}},
        "files": {}, "summary": "ok", "finish_reason": "completed", "error": None,
    }
'''


@pytest.fixture
def fake_flow_builtins(tmp_path, monkeypatch):
    """fake 脚本落库（exec_type=script）+ 脚本包落盘，替换 runner.SessionLocal"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import Base
    from app.models.pre_judge_models import PreJudgeScript
    from app.services import pre_judge_script_service as script_service

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
        db.add(PreJudgeScript(id="pjs_flow_ok", name="flow_ok", exec_type="script",
                              entrypoint="main.py", timeout_seconds=60, is_active=True))
        db.commit()
        pkg = upload_base / "pjs_flow_ok"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "main.py").write_text(_FLOW_OK_SRC, encoding="utf-8")
    monkeypatch.setattr("app.services.pre_judge_runner.SessionLocal", Session)
    yield Session


def _make_task(**overrides):
    """executor 集成测试用任务对象（字段口径与 test_evaluation_pre_scripts_flow._make_task 一致）"""
    fields = {
        "id": "task_gap_x",
        "evaluation_type": "judge",
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
    }
    fields.update(overrides)
    return type("Task", (), fields)()


def _mock_judge_stack():
    """mock 裁判侧外部依赖（PromptBuilder / 双引擎），保留 VariableInjector 真实逻辑"""
    pb = patch("app.services.prompt_builder.PromptBuilder")
    cc = patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent")
    ws = patch("app.services.claude_code_judge_agent.write_session_file",
               return_value="/tmp/fake_session.md")
    lg = patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent")
    return pb, cc, ws, lg


def _run_single(task, payload):
    """跑一条 _evaluate_single（claude_agent 引擎，裁判栈 mock）"""
    executor = EvaluationExecutor(max_workers=1, task_id="task_gap_x")
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
    return result, mock_pb, mock_cc


# ==================== 1. 待评测目标内容段的 script_output 注入（缺陷暴露） ====================

class TestScriptTargetInjectionIntoSystemPrompt:
    """claude_agent 引擎下 system prompt「## 待评测目标内容」段应含 script_output 行内容

    契约 v3 §二/§三：script_outputs 构造后传入 JudgeVariableResolver 供 targets 解析。
    executor 主链路（空值校验 / @ 标记 targets 上下文）已传，但
    prompt_builder._inject_evaluation_targets（app/services/prompt_builder.py:823-828）
    重新构造 JudgeVariableResolver 时未透传 payload 携带的 script_outputs，
    导致该段中 script_output 行内容恒为空（is_empty=True 被整行跳过）。
    已实测确认缺陷存在（graph 引擎不受影响，resolved_targets_content 来自 executor
    已带 script_outputs 的解析结果）。此处以 xfail(strict=True) 暴露并锁定契约，
    修复后测试翻转 XPASS 提醒移除标记。缺陷转修复员处理。
    """

    @staticmethod
    def _prompt_with_targets(targets):
        task = MagicMock()
        task.judge_prompt = ""
        task.evaluation_scene = ""
        task.scene_desc = "测试场景"
        task.evaluation_metrics = [{"name": "维度A", "weight": 1}]
        task.max_score = 10
        task.score_details = "评分标准"
        task.steps = []
        task.judge_variable_config = {}
        task.runtime_variables = {}
        task.evaluation_targets = targets
        pb = PromptBuilder(task)
        # 模拟 executor 集成：judge_payload 由 VariableInjector 注入 script_outputs 后传入
        judge_payload = {"user_input": "q", "ref_answer": "a",
                         "script_outputs": {"cmp_out": {"rate": 0.87}}}
        return pb._build_system_prompt(
            payload=judge_payload, model_output="真实模型输出内容", chain_results=[])

    @pytest.mark.xfail(strict=True, reason=(
        "缺陷：prompt_builder._inject_evaluation_targets 构造 JudgeVariableResolver 未传 "
        "script_outputs（executor 已注入 judge_payload['script_outputs']），script_output "
        "行内容在「## 待评测目标内容」段丢失——唯一脚本行时整段消失。转修复员。"
    ))
    def test_sole_script_row_content_injected(self):
        """唯一 script_output 行：段应存在且含行显示名与脚本输出值"""
        prompt = self._prompt_with_targets([_script_target()])
        assert "## 待评测目标内容" in prompt
        assert "比对结果" in prompt
        assert "0.87" in prompt

    @pytest.mark.xfail(strict=True, reason=(
        "缺陷：同上——混合目标行时 model_output 行正常注入、script_output 行（显示名与内容）"
        "整行丢失。转修复员。"
    ))
    def test_mixed_rows_keep_script_row_content(self):
        """混合行：model_output 行与 script_output 行内容都应注入"""
        prompt = self._prompt_with_targets([_model_target(), _script_target()])
        assert "## 待评测目标内容" in prompt
        assert "真实模型输出内容" in prompt
        assert "比对结果" in prompt
        assert "0.87" in prompt


# ==================== 2. executor 步骤1.5 跳过分支与 steps 传参形态 ====================

class TestExecutorPipelineDispatch:
    def test_no_script_rows_skips_pipeline(self, fake_flow_builtins):
        """v3 新任务形态：targets 只有 model_output 行且无 legacy pre_scripts →
        derive_script_steps 为空，完全不进入脚本管线（run_pre_scripts 不被调用）"""
        task = _make_task(evaluation_targets=[_model_target()], pre_scripts=None)
        with patch("app.services.pre_judge_runner.run_pre_scripts") as mock_run:
            result, _, mock_cc = _run_single(
                task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})
        assert result["success"] is True
        assert "pre_script_results" not in result
        assert not mock_run.called
        assert mock_cc.return_value.run_parallel.called

    def test_script_rows_invoke_run_pre_scripts_with_derived_steps(self, fake_flow_builtins):
        """targets 含 script_output 行 → run_pre_scripts 以 steps=派生步骤调用（非 None），
        步骤结构与 derive_script_steps 输出一致（首行配置生效）"""
        task = _make_task(
            evaluation_targets=[_model_target(), _script_target()],
            pre_scripts=None,
        )
        with patch("app.services.pre_judge_runner.run_pre_scripts") as mock_run:
            mock_run.return_value = {
                "aborted": False, "error": None, "pre_script_results": [],
                "script_outputs": {"cmp_out": {"rate": 0.87}}, "script_files": {},
            }
            result, _, _ = _run_single(
                task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})

        assert result["success"] is True
        assert mock_run.called
        steps = mock_run.call_args.kwargs["steps"]
        assert steps is not None
        assert steps == [
            {"script_id": "flow_ok", "input_mapping": {},
             "params": {}, "output_var": "cmp_out", "on_fail": "continue"}
        ]


# ==================== 3. list 格式 judge_variable_config 的 @ var 标记解析 ====================

class TestRefMarksWithListJudgeVariableConfig:
    """契约 v3 §三：ctx.vars 用裁判变量解析结果 {key: value}——
    list 格式 judge_variable_config 的 key 提取（_judge_variable_keys list 分支）
    与 executor 集成解析一并覆盖"""

    def test_judge_variable_keys_three_shapes(self):
        """dict / list / 缺省 三种配置形态的 key 提取"""
        assert EvaluationExecutor._judge_variable_keys(
            SimpleNamespace(judge_variable_config={"a": {}, "b": {}})
        ) == ["a", "b"]
        assert EvaluationExecutor._judge_variable_keys(
            SimpleNamespace(judge_variable_config=[
                {"key": "v1", "name": "变量一"}, {"bad": 1}, {"key": "v2"},
            ])
        ) == ["v1", "v2"]
        assert EvaluationExecutor._judge_variable_keys(
            SimpleNamespace(judge_variable_config=None)
        ) == []

    def test_executor_resolves_var_mark_from_list_config(self, fake_flow_builtins):
        """list 格式裁判变量 + @ var 标记：executor 构造的 vars 上下文能按 key 命中
        （脚本输出经 VariableInjector 注入 judge_payload 后进入 overrides 解析结果）"""
        task = _make_task(
            score_details="全局标准，需对照 {{ref:var:v1|变量一}} 评分",
            judge_variable_config=[
                {"key": "v1", "name": "变量一", "source": "script_output",
                 "source_config": {"script": "cmp_out", "key": "rate"}},
            ],
            evaluation_targets=[_script_target()],
        )
        result, mock_pb, _ = _run_single(
            task, {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}})

        assert result["success"] is True
        overrides = mock_pb.call_args.kwargs["overrides"]
        assert overrides["score_details"] == "全局标准，需对照 【变量一】\n0.87 评分"


# ==================== 4. migrate-config 端点 happy path ====================

class TestMigrateConfigEndpointHappyPath:
    def _run_endpoint(self, summary):
        from app.api.evaluation_task_api import migrate_task_config

        # W9：存在性/归属由 ensure_task_access 承担（db 替身返回任务对象），
        # request 直调替身取超管视角
        from tests.unit.w9_direct_call import fake_request

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock(
            id="task_1")
        with patch("app.services.config_migration_service.ConfigMigrationService") as mock_mig:
            mock_mig.return_value.migrate_task.return_value = summary
            return migrate_task_config("task_1", fake_request(), db=db)

    def test_migrated_task_returns_summary(self):
        """发生迁移：端点原样返回迁移摘要（含 target_ids 与 changes）"""
        summary = {
            "task_id": "task_1", "migrated": True, "steps_migrated": 2,
            "pre_scripts_migrated": 1,
            "target_ids": ["target_mig_0_task_1"][:0] or ["target_mig_0_task_1"],
            "changes": ["steps（2 行）已合并进 score_details",
                        "pre_scripts（1 条）已转为 script_output 待评测数据行"],
        }
        assert self._run_endpoint(summary) == summary

    def test_already_migrated_task_returns_200_with_migrated_false(self):
        """幂等重放：已迁移任务（migrated=False）同样 200 返回摘要，不报错"""
        summary = {
            "task_id": "task_1", "migrated": False, "steps_migrated": 0,
            "pre_scripts_migrated": 0, "target_ids": [], "changes": [],
        }
        assert self._run_endpoint(summary) == summary
