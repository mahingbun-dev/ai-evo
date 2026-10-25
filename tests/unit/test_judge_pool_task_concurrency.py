"""任务级行并发放开（concurrency）+ 行级并发池上下文/中止语义

覆盖：
1. EvaluationExecutor 接受 concurrency（缺省保持 max_workers，钳制 1~64）；
2. 行工作线程绑定 judge 上下文（eval_task_id/row_idx/stop_checker），
   judge 与 RAG 两条批量链都要绑；
3. 池中止（JudgePoolAborted）不得伪装成行失败——执行器按「任务已停止」
   落行；插件行循环直接上抛（停止链路收尾）；
4. 池等待超时（JudgePoolTimeout）在插件行循环不得按行失败慢速消耗——
   与软超时同等上抛，任务快速失败；
5. API/Service 透传 concurrency。

补充（评审修复，注入点均在真实链路）：
6. 中止从裁判层（CC agent run_parallel / RAGEvaluator）注入后，execute_batch(_rag)
   的行必须是「任务已停止」，而不是被 _evaluate_single_inner 的宽 except 吞成普通失败；
7. 插件 evaluate / evaluate_trace 行内读到的是绑定的 stop_checker（停止可中止）；
8. graph 引擎 run / run_parallel 原样上抛 JudgePoolAborted，不记 judge_failed。

补充（终审 I-QA1：池超时同样不得在 agent 层被洗成 judge_failed）：
9. graph / claude_agent 两个引擎的 run(_single) 与 run_parallel 原样上抛
   JudgePoolTimeout，由行级逻辑落「失败行」（携带额度耗尽的真实原因）；
10. 一致性行循环与插件 / trace 行循环同口径上抛 JudgePoolTimeout。
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor
from app.services.judge_pool import current_judge_context
from app.services.judge_pool.errors import JudgePoolAborted, JudgePoolTimeout

pytestmark = pytest.mark.unit


class TestExecutorConcurrency:
    """执行器行并发参数"""

    def test_executor_accepts_concurrency(self):
        executor = EvaluationExecutor(max_workers=4, concurrency=9)
        assert executor.max_workers == 9
        assert executor.concurrency == 9

    def test_executor_clamps_to_64(self):
        assert EvaluationExecutor(max_workers=4, concurrency=999).max_workers == 64

    def test_executor_defaults_to_max_workers(self):
        assert EvaluationExecutor(max_workers=3).max_workers == 3

    def test_executor_clamps_lower_bound(self):
        assert EvaluationExecutor(max_workers=4, concurrency=0).max_workers == 1


class TestRowJudgeContext:
    """行工作线程必须绑定 judge 上下文，池等待期才能响应停止信号"""

    def test_evaluate_single_binds_context(self):
        executor = EvaluationExecutor(max_workers=1, task_id="task-ctx")
        seen = {}

        def fake_inner(payload, task):
            seen["ctx"] = current_judge_context()
            return {"idx": payload["idx"], "success": True}

        executor._evaluate_single_inner = fake_inner
        executor._evaluate_single({"idx": 7}, MagicMock())

        assert seen["ctx"].eval_task_id == "task-ctx"
        assert seen["ctx"].row_idx == 7
        assert seen["ctx"].stop_checker is executor.stop_event
        # 行结束后上下文不泄漏到线程后续代码
        assert current_judge_context().eval_task_id is None

    def test_evaluate_single_rag_binds_context(self):
        executor = EvaluationExecutor(max_workers=1, task_id="task-rag")
        seen = {}

        def fake_inner(payload, task):
            seen["ctx"] = current_judge_context()
            return {"idx": payload["idx"], "success": True}

        executor._evaluate_single_rag_inner = fake_inner
        executor._evaluate_single_rag({"idx": 3}, MagicMock())

        assert seen["ctx"].eval_task_id == "task-rag"
        assert seen["ctx"].row_idx == 3
        assert seen["ctx"].stop_checker is executor.stop_event


class TestPoolAbortInRowLoops:
    """池中止不得被宽 except 洗成裁判/评测失败"""

    def test_execute_batch_marks_row_stopped(self):
        executor = EvaluationExecutor(max_workers=2)

        def abort(payload, task):
            raise JudgePoolAborted("并发池等待被中止")

        executor._evaluate_single = abort
        results = executor.execute_batch([{"idx": 0}, {"idx": 1}], MagicMock())

        assert [r["error"] for r in results] == ["任务已停止", "任务已停止"]
        assert all(r["success"] is False for r in results)

    def test_execute_batch_rag_marks_row_stopped(self):
        executor = EvaluationExecutor(max_workers=2)

        def abort(payload, task):
            raise JudgePoolAborted("并发池等待被中止")

        executor._evaluate_single_rag = abort
        results = executor.execute_batch_rag([{"idx": 0}], MagicMock())

        assert results[0]["error"] == "任务已停止"
        assert results[0]["success"] is False

    def test_plugin_loop_reraises_pool_timeout(self, monkeypatch):
        """池等待超时不按行失败：插件行循环直接上抛，任务快速失败"""
        from app.tasks.evaluation_tasks_async import _execute_plugin_evaluation

        def timeout(self, **kwargs):
            raise JudgePoolTimeout("并发池等待超时")

        monkeypatch.setattr("app.services.plugin_evaluator.PluginEvaluator.evaluate", timeout)
        task = MagicMock()
        task.plugin_config = {"depth": "quick", "source": "manual"}
        payloads = [{"idx": 0, "_orig_idx": 0, "plugin_path": "/p"}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            with pytest.raises(JudgePoolTimeout):
                _execute_plugin_evaluation(task, payloads, "task-1", None, None, execution_number=1)

    def test_plugin_loop_reraises_pool_abort(self, monkeypatch):
        from app.tasks.evaluation_tasks_async import _execute_plugin_evaluation

        def aborted(self, **kwargs):
            raise JudgePoolAborted("并发池等待被中止")

        monkeypatch.setattr("app.services.plugin_evaluator.PluginEvaluator.evaluate", aborted)
        task = MagicMock()
        task.plugin_config = {"depth": "quick", "source": "manual"}
        payloads = [{"idx": 0, "_orig_idx": 0, "plugin_path": "/p"}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            with pytest.raises(JudgePoolAborted):
                _execute_plugin_evaluation(task, payloads, "task-1", None, None, execution_number=1)

    def test_plugin_trace_loop_reraises_pool_timeout(self, monkeypatch):
        from app.tasks.evaluation_tasks_async import _execute_plugin_trace_evaluation

        def timeout(self, package_dir, on_tick=None, design_cache=None):
            raise JudgePoolTimeout("并发池等待超时")

        monkeypatch.setattr("app.services.plugin_evaluator.PluginEvaluator.evaluate_trace", timeout)
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        task = MagicMock()
        task.plugin_config = {"depth": "quick", "source": "trace"}
        payloads = [{"_orig_idx": 0, "_dataset_item": {"data": {"hub_file_id": "/p"}}}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            with pytest.raises(JudgePoolTimeout):
                _execute_plugin_trace_evaluation(task, payloads, "task-1", None, None, execution_number=1)

    def test_plugin_trace_loop_reraises_pool_abort(self, monkeypatch):
        from app.tasks.evaluation_tasks_async import _execute_plugin_trace_evaluation

        def aborted(self, package_dir, on_tick=None, design_cache=None):
            raise JudgePoolAborted("并发池等待被中止")

        monkeypatch.setattr("app.services.plugin_evaluator.PluginEvaluator.evaluate_trace", aborted)
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        task = MagicMock()
        task.plugin_config = {"depth": "quick", "source": "trace"}
        payloads = [{"_orig_idx": 0, "_dataset_item": {"data": {"hub_file_id": "/p"}}}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            with pytest.raises(JudgePoolAborted):
                _execute_plugin_trace_evaluation(task, payloads, "task-1", None, None, execution_number=1)

    def test_consistency_loop_reraises_pool_timeout(self, monkeypatch):
        """一致性行循环与插件/trace 同口径：池超时上抛，不按行失败慢速消耗

        缺这一条时，每行都要各自排满 wait_timeout（900s）再落一行失败，
        10 行的任务要卡 2.5 小时才结束，且结果里看不出是额度不够。
        """
        from app.tasks.evaluation_tasks_async import _execute_consistency_evaluation

        monkeypatch.setattr(
            "app.services.config_snapshot.resolve_model_call_config",
            lambda task, db: ({"provider": "openai"}, "test"),
        )
        monkeypatch.setattr(
            "app.services.config_snapshot.resolve_judge_call_config",
            lambda task, db: ({"provider": "openai"}, "test"),
        )

        async def timeout(self, *args, **kwargs):
            raise JudgePoolTimeout("并发池等待超时（引擎 graph，等待 900s）")

        monkeypatch.setattr(
            "app.services.consistency_evaluator.ConsistencyEvaluator.evaluate_single", timeout
        )
        task = MagicMock()
        task.consistency_config = {"repeat_count": 2, "use_judge": True}
        task.evaluation_metrics = [{"name": "维度A"}]
        payloads = [{"idx": 0, "_orig_idx": 0, "user_input": "q"}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            with pytest.raises(JudgePoolTimeout):
                _execute_consistency_evaluation(
                    task, payloads, MagicMock(), "task-1", None, execution_number=1
                )

    def test_consistency_loop_reraises_pool_abort(self, monkeypatch):
        from app.tasks.evaluation_tasks_async import _execute_consistency_evaluation

        monkeypatch.setattr(
            "app.services.config_snapshot.resolve_model_call_config",
            lambda task, db: ({"provider": "openai"}, "test"),
        )
        monkeypatch.setattr(
            "app.services.config_snapshot.resolve_judge_call_config",
            lambda task, db: ({"provider": "openai"}, "test"),
        )

        async def aborted(self, *args, **kwargs):
            raise JudgePoolAborted("并发池等待被中止（引擎 graph）")

        monkeypatch.setattr(
            "app.services.consistency_evaluator.ConsistencyEvaluator.evaluate_single", aborted
        )
        task = MagicMock()
        task.consistency_config = {"repeat_count": 2, "use_judge": True}
        task.evaluation_metrics = [{"name": "维度A"}]
        payloads = [{"idx": 0, "_orig_idx": 0, "user_input": "q"}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            with pytest.raises(JudgePoolAborted):
                _execute_consistency_evaluation(
                    task, payloads, MagicMock(), "task-1", None, execution_number=1
                )


class TestApiAndServiceConcurrency:
    """API/Service 透传 concurrency"""

    def test_create_request_default_and_bounds(self):
        from app.api.evaluation_task_api import CreateTaskRequest

        assert CreateTaskRequest(name="t", evaluation_type="judge").concurrency == 4
        assert CreateTaskRequest(name="t", evaluation_type="judge", concurrency=16).concurrency == 16
        with pytest.raises(Exception):
            CreateTaskRequest(name="t", evaluation_type="judge", concurrency=65)

    def test_update_request_accepts_none(self):
        from app.api.evaluation_task_api import UpdateTaskRequest

        assert UpdateTaskRequest().concurrency is None
        assert UpdateTaskRequest(concurrency=8).concurrency == 8

    def test_service_create_task_persists_concurrency(self):
        from app.services.evaluation_task_service import EvaluationTaskService

        service = EvaluationTaskService(MagicMock())
        with patch.object(EvaluationTaskService, "_validate_targets_required"), \
             patch.object(EvaluationTaskService, "_validate_script_targets"), \
             patch.object(EvaluationTaskService, "_validate_reference_marks"), \
             patch("app.services.config_snapshot.build_model_config_snapshot", return_value={}):
            task = service.create_task(
                name="并发任务", evaluation_type="judge", concurrency=9,
                model_output_source="none",
            )
        assert task.concurrency == 9

    def test_service_update_task_ignores_none_concurrency(self):
        from app.services.evaluation_task_service import EvaluationTaskService

        service = EvaluationTaskService(MagicMock())
        existing = MagicMock()
        existing.evaluation_type = "judge"
        existing.consistency_config = None
        existing.evaluation_targets = []
        existing.dataset_id = None
        existing.model_output_source = "none"
        existing.model_output_field = None
        existing.concurrency = 4
        service.get_task = MagicMock(return_value=existing)
        with patch.object(EvaluationTaskService, "_validate_targets_required"), \
             patch.object(EvaluationTaskService, "_validate_script_targets"), \
             patch.object(EvaluationTaskService, "_validate_reference_marks"), \
             patch.object(EvaluationTaskService, "_validate_verifiers"), \
             patch.object(EvaluationTaskService, "_validate_trajectory_config"), \
             patch.object(EvaluationTaskService, "_validate_trials"), \
             patch.object(EvaluationTaskService, "_validate_judge_governance"), \
             patch.object(EvaluationTaskService, "_validate_dataset_version"), \
             patch.object(EvaluationTaskService, "_validate_model_output_config"), \
             patch.object(EvaluationTaskService, "_validate_plugin_config"), \
             patch.object(EvaluationTaskService, "_validate_safety_config"), \
             patch.object(EvaluationTaskService, "_validate_plugin_depth_target"), \
             patch.object(EvaluationTaskService, "_validate_plugin_trace_config"):
            service.update_task("task-1", concurrency=None)

        # None 不写回（避免把已有值清空）；模型属性保持原值
        assert existing.concurrency == 4


# ==================== 评审修复：在真实链路注入中止 ====================


class _StopAfterFirstCheck:
    """第一次 is_set()（行首检查）为 False，之后为 True

    模拟「行已开始执行后用户才点停止」：行首检查放行，等到池里排队时停止已置位。
    """

    def __init__(self):
        self.calls = 0

    def is_set(self):
        self.calls += 1
        return self.calls > 1


def _mock_judge_task(judge_engine="claude_agent"):
    return type("Task", (), {
        "id": "task_real_chain",
        "evaluation_type": "judge",
        "judge_engine": judge_engine,
        "judge_count": 1,
        "model_config": {"provider": "openai"},
        "model_provider": None,
        "max_score": 10,
        "evaluation_metrics": [{"name": "维度A"}],
        "judge_variable_config": {},
        "evaluation_targets": None,
        "runtime_variables": {},
    })()


class TestPoolAbortThroughJudgeChain:
    """中止注入点在裁判层，走真实 _evaluate_single_inner / RAG 内层

    回归点：内层方法体自带宽 except Exception（RuntimeError 子类会被吞成
    「评测执行失败」行），执行器的 except JudgePoolAborted 必须是活的。
    """

    def test_execute_batch_row_stopped_when_cc_judge_aborts(self):
        task = _mock_judge_task("claude_agent")
        executor = EvaluationExecutor(max_workers=1, task_id="task_real_chain")
        with patch("app.services.prompt_builder.PromptBuilder") as mock_pb, \
             patch("app.services.variable_injector.VariableInjector") as mock_vi, \
             patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent") as mock_cc, \
             patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_vi.return_value.build_judge_payload.return_value = {"user_input": "x"}
            mock_vi.return_value.script_output_missing = []
            mock_caller.return_value.call.return_value = {
                "success": True, "content": "模型输出", "chain_results": [],
            }
            mock_cc.return_value.run_parallel.side_effect = JudgePoolAborted(
                "并发池等待被中止（引擎 graph）"
            )

            results = executor.execute_batch(
                [{"idx": 0, "user_input": "问题", "_dataset_item": {}}], task
            )

        assert results[0]["success"] is False
        assert results[0]["error"] == "任务已停止"

    def test_execute_batch_graph_judge_timeout_stays_row_failure(self):
        """池超时保持行失败语义（不像中止那样上抛）"""
        task = _mock_judge_task("claude_agent")
        executor = EvaluationExecutor(max_workers=1, task_id="task_real_chain")
        with patch("app.services.prompt_builder.PromptBuilder") as mock_pb, \
             patch("app.services.variable_injector.VariableInjector") as mock_vi, \
             patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent") as mock_cc, \
             patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_vi.return_value.build_judge_payload.return_value = {"user_input": "x"}
            mock_vi.return_value.script_output_missing = []
            mock_caller.return_value.call.return_value = {
                "success": True, "content": "模型输出", "chain_results": [],
            }
            mock_cc.return_value.run_parallel.side_effect = JudgePoolTimeout(
                "并发池等待超时（引擎 graph，等待 900s）"
            )

            results = executor.execute_batch(
                [{"idx": 0, "user_input": "问题", "_dataset_item": {}}], task
            )

        assert results[0]["success"] is False
        assert "并发池等待超时" in results[0]["error"]

    def test_execute_batch_rag_row_stopped_when_rag_judge_aborts(self):
        task = MagicMock()
        task.rag_config = {"metrics": ["answer_relevancy"]}
        task.judge_variable_config = {}
        task.judge_model = None
        executor = EvaluationExecutor(max_workers=1, task_id="task_rag_chain")
        with patch(
            "app.services.config_snapshot.resolve_model_call_config",
            return_value=({"provider": "openai"}, "test"),
        ), patch("app.services.model_caller.ModelCallerSync") as mock_caller, \
           patch("app.services.rag_evaluator.RAGEvaluator") as mock_rag:
            mock_caller.return_value.call.return_value = {
                "success": True, "content": "答案", "chain_results": [],
            }
            mock_rag.return_value.evaluate.side_effect = JudgePoolAborted(
                "并发池等待被中止（引擎 graph）"
            )

            results = executor.execute_batch_rag([{"idx": 0, "user_input": "问题"}], task)

        assert results[0]["success"] is False
        assert results[0]["error"] == "任务已停止"


class TestPluginRowBindsJudgeContext:
    """插件两条行循环必须把任务/行/停止信号绑进池调用（否则停止要等 900s 超时）"""

    def test_evaluate_row_binds_stop_checker(self, monkeypatch):
        from app.tasks.evaluation_tasks_async import _execute_plugin_evaluation

        stop_event = _StopAfterFirstCheck()
        seen = {}

        def fake_evaluate(self, **kwargs):
            ctx = current_judge_context()
            seen["stop_checker"] = ctx.stop_checker
            seen["eval_task_id"] = ctx.eval_task_id
            seen["row_idx"] = ctx.row_idx
            # 真实 acquirer 的判定：停止已置位 → 池等待中止
            if ctx.stop_checker is not None and ctx.stop_checker.is_set():
                raise JudgePoolAborted("并发池等待被中止（引擎 plugin）")
            return {"success": True, "average_score": 80.0}

        monkeypatch.setattr(
            "app.services.plugin_evaluator.PluginEvaluator.evaluate", fake_evaluate
        )
        task = MagicMock()
        task.plugin_config = {"depth": "quick", "source": "manual"}
        payloads = [{"idx": 0, "_orig_idx": 5, "plugin_path": "/p"}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            with pytest.raises(JudgePoolAborted):
                _execute_plugin_evaluation(
                    task, payloads, "task-plugin", None, stop_event, execution_number=1
                )

        assert seen["stop_checker"] is stop_event
        assert seen["eval_task_id"] == "task-plugin"
        assert seen["row_idx"] == 5

    def test_trace_row_binds_stop_checker(self, monkeypatch):
        from app.tasks.evaluation_tasks_async import _execute_plugin_trace_evaluation

        stop_event = _StopAfterFirstCheck()
        seen = {}

        def fake_evaluate_trace(self, package_dir, on_tick=None, design_cache=None):
            ctx = current_judge_context()
            seen["stop_checker"] = ctx.stop_checker
            seen["eval_task_id"] = ctx.eval_task_id
            seen["row_idx"] = ctx.row_idx
            if ctx.stop_checker is not None and ctx.stop_checker.is_set():
                raise JudgePoolAborted("并发池等待被中止（引擎 plugin）")
            return {"success": True, "evaluation_mode": "trace", "trace": {"mode": "trace"}}

        monkeypatch.setattr(
            "app.services.plugin_evaluator.PluginEvaluator.evaluate_trace",
            fake_evaluate_trace,
        )
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        task = MagicMock()
        task.plugin_config = {"depth": "quick", "source": "trace"}
        payloads = [{"_orig_idx": 7, "_dataset_item": {"data": {"hub_file_id": "/p"}}}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            with pytest.raises(JudgePoolAborted):
                _execute_plugin_trace_evaluation(
                    task, payloads, "task-trace", None, stop_event, execution_number=1
                )

        assert seen["stop_checker"] is stop_event
        assert seen["eval_task_id"] == "task-trace"
        assert seen["row_idx"] == 7


class TestGraphAgentPoolAbort:
    """graph 引擎（存量任务兜底）不得把中止记成裁判失败"""

    def _make_agent(self):
        from app.services.judge_langgraph_agent import JudgeLangGraphAgent

        agent = JudgeLangGraphAgent.__new__(JudgeLangGraphAgent)
        agent.task = MagicMock()
        agent.langfuse_host = ""
        agent.graph = MagicMock()
        return agent

    def test_run_reraises_pool_abort(self):
        agent = self._make_agent()
        agent.graph.invoke.side_effect = JudgePoolAborted("并发池等待被中止（引擎 graph）")

        with pytest.raises(JudgePoolAborted):
            agent.run({"task_id": "t"}, judge_index=0)

    def test_run_parallel_reraises_pool_abort(self, monkeypatch):
        agent = self._make_agent()

        def aborting_run(state_input, judge_index=0):
            raise JudgePoolAborted("并发池等待被中止（引擎 graph）")

        monkeypatch.setattr(agent, "run", aborting_run)

        with pytest.raises(JudgePoolAborted):
            agent.run_parallel({"task_id": "t"}, judge_count=2)

    def test_run_reraises_pool_timeout(self):
        """池等待超时不得被洗成 judge_failed/llm_error

        被 run 的宽 except 吞掉后，上层拿到的是一个「裁判执行失败」结果，
        行仍按普通失败落库：额度耗尽（900s 排队超时）与裁判模型报错在结果里
        无法区分，运维判断不出该扩额度还是该查模型。
        """
        agent = self._make_agent()
        agent.graph.invoke.side_effect = JudgePoolTimeout(
            "并发池等待超时（引擎 graph，等待 900s）"
        )

        with pytest.raises(JudgePoolTimeout):
            agent.run({"task_id": "t"}, judge_index=0)

    def test_run_parallel_reraises_pool_timeout(self, monkeypatch):
        agent = self._make_agent()

        def timing_out_run(state_input, judge_index=0):
            raise JudgePoolTimeout("并发池等待超时（引擎 graph，等待 900s）")

        monkeypatch.setattr(agent, "run", timing_out_run)

        with pytest.raises(JudgePoolTimeout):
            agent.run_parallel({"task_id": "t"}, judge_count=2)


class TestClaudeCodeAgentPoolErrors:
    """claude_agent 引擎：池异常必须原样上抛，不记 judge_failed"""

    def _make_agent(self, monkeypatch, query_raises):
        from app.services.claude_code_judge_agent import ClaudeCodeJudgeAgent

        agent = ClaudeCodeJudgeAgent.__new__(ClaudeCodeJudgeAgent)
        agent.task = MagicMock()
        agent.langfuse_host = ""
        monkeypatch.setattr(agent, "_build_final_prompt", lambda user_prompt, session_file: "final")
        monkeypatch.setattr(agent, "_query_agent", lambda config, system_prompt, prompt: query_raises())
        return agent

    def test_run_single_reraises_pool_abort(self, monkeypatch):
        def aborted():
            raise JudgePoolAborted("并发池等待被中止（引擎 claude_agent）")

        agent = self._make_agent(monkeypatch, aborted)

        with pytest.raises(JudgePoolAborted):
            agent._run_single({"model": "m"}, "sys", "user", None, 0)

    def test_run_single_reraises_pool_timeout(self, monkeypatch):
        """同 graph 引擎口径：超时上抛，由执行器落「失败行」而非 judge_failed"""

        def timed_out():
            raise JudgePoolTimeout("并发池等待超时（引擎 claude_agent，等待 900s）")

        agent = self._make_agent(monkeypatch, timed_out)

        with pytest.raises(JudgePoolTimeout):
            agent._run_single({"model": "m"}, "sys", "user", None, 0)

    def test_run_parallel_reraises_pool_timeout(self, monkeypatch):
        from app.services.claude_code_judge_agent import ClaudeCodeJudgeAgent

        agent = ClaudeCodeJudgeAgent.__new__(ClaudeCodeJudgeAgent)
        agent.task = MagicMock()
        agent.langfuse_host = ""
        monkeypatch.setattr(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_active_config",
            staticmethod(lambda: {"model": "m"}),
        )

        def timing_out_single(config, system_prompt, user_prompt, session_file, judge_index):
            raise JudgePoolTimeout("并发池等待超时（引擎 claude_agent，等待 900s）")

        monkeypatch.setattr(agent, "_run_single", timing_out_single)

        with pytest.raises(JudgePoolTimeout):
            agent.run_parallel("sys", "user", None, 2)

    def test_run_parallel_reraises_pool_abort(self, monkeypatch):
        from app.services.claude_code_judge_agent import ClaudeCodeJudgeAgent

        agent = ClaudeCodeJudgeAgent.__new__(ClaudeCodeJudgeAgent)
        agent.task = MagicMock()
        agent.langfuse_host = ""
        monkeypatch.setattr(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_active_config",
            staticmethod(lambda: {"model": "m"}),
        )

        def aborting_single(config, system_prompt, user_prompt, session_file, judge_index):
            raise JudgePoolAborted("并发池等待被中止（引擎 claude_agent）")

        monkeypatch.setattr(agent, "_run_single", aborting_single)

        with pytest.raises(JudgePoolAborted):
            agent.run_parallel("sys", "user", None, 2)


class TestClampedConcurrencyAttribute:
    """公开属性与实际行为一致：concurrency 存夹取后的值"""

    def test_concurrency_attribute_is_clamped(self):
        executor = EvaluationExecutor(max_workers=4, concurrency=999)
        assert executor.concurrency == 64
        assert executor.max_workers == 64

