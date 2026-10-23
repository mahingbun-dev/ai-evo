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
             patch.object(EvaluationTaskService, "_validate_reference_marks"):
            service.update_task("task-1", concurrency=None)

        # None 不写回（避免把已有值清空）；模型属性保持原值
        assert existing.concurrency == 4
