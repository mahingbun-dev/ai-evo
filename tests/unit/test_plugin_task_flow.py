"""
插件评测任务流单元测试：创建校验、执行分发、行执行、聚合与导出
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.evaluation_task_service import EvaluationTaskService
from app.services.result_aggregator import ResultAggregator, ResultExporter
from app.tasks.evaluation_tasks_async import (
    _execute_evaluation,
    _execute_plugin_evaluation,
    _execute_plugin_trace_evaluation,
)
pytestmark = pytest.mark.unit


GOOD_SKILL_MD = """---
name: demo-checker
description: Use when the user asks to check project demo files for broken links, missing references, or stale examples. Use proactively for demo hygiene tasks.
---

# Demo Checker

## Usage

Run the checker.

```bash
./check.sh --all
```
"""


@pytest.fixture
def good_skill_dir(tmp_path):
    skill = tmp_path / "demo-checker"
    skill.mkdir()
    (skill / "SKILL.md").write_text(GOOD_SKILL_MD, encoding="utf-8")
    return str(skill)


@pytest.fixture(autouse=True)
def legacy_engine(monkeypatch):
    """本文件校验任务流与内置移植实现（legacy 引擎，无市场依赖）

    market 引擎（实时加载市场版 plugin-eval）的任务流用例见
    test_plugin_eval_market_engine.py。
    """
    monkeypatch.setenv("PLUGIN_EVAL_ENGINE", "legacy")


def _make_service():
    db = MagicMock()
    return EvaluationTaskService(db), db


class TestCreateTaskValidation:
    """创建任务：插件类型白名单与 plugin_config 校验"""

    def test_whitelist_accepts_plugin(self):
        service, db = _make_service()
        with patch.object(EvaluationTaskService, "_validate_targets_required"), \
             patch.object(EvaluationTaskService, "_validate_script_targets"), \
             patch.object(EvaluationTaskService, "_validate_reference_marks"), \
             patch("app.services.config_snapshot.build_model_config_snapshot", return_value={}):
            task = service.create_task(
                name="插件评测任务",
                evaluation_type="plugin",
                plugin_config={"depth": "quick", "plugin_path": "/some/plugin", "skill_content": ""},
            )
        assert task.evaluation_type == "plugin"
        assert task.plugin_config["depth"] == "quick"

    def test_whitelist_rejects_unknown(self):
        service, _ = _make_service()
        with pytest.raises(ValueError, match="不支持的评测类型"):
            service.create_task(name="x", evaluation_type="no_such_type")

    def test_invalid_depth_rejected(self):
        service, _ = _make_service()
        with pytest.raises(ValueError, match="插件评测深度"):
            service.create_task(
                name="x",
                evaluation_type="plugin",
                plugin_config={"depth": "deep"},
            )

    def test_non_string_path_rejected(self):
        service, _ = _make_service()
        with pytest.raises(ValueError, match="plugin_path"):
            service.create_task(
                name="x",
                evaluation_type="plugin",
                plugin_config={"depth": "quick", "plugin_path": 123},
            )

    def test_source_required_without_dataset(self):
        service, _ = _make_service()
        with pytest.raises(ValueError, match="插件评测需要指定评测对象"):
            service.create_task(name="x", evaluation_type="plugin", plugin_config={"depth": "quick"})

    def test_source_ok_with_inline_content(self):
        service, db = _make_service()
        with patch.object(EvaluationTaskService, "_validate_targets_required"), \
             patch.object(EvaluationTaskService, "_validate_script_targets"), \
             patch.object(EvaluationTaskService, "_validate_reference_marks"), \
             patch("app.services.config_snapshot.build_model_config_snapshot", return_value={}):
            task = service.create_task(
                name="x",
                evaluation_type="plugin",
                plugin_config={"depth": "standard", "skill_content": GOOD_SKILL_MD},
            )
        assert task.plugin_config["skill_content"].startswith("---")


class TestPluginTraceValidation:
    """trace 来源（会话 Trace 口径）的创建/更新校验"""

    def test_trace_source_accepted_with_hub_dataset(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        service, _ = _make_service()
        service._validate_plugin_config({"depth": "quick", "source": "trace"}, "ds_1")

    def test_trace_requires_dataset(self):
        service, _ = _make_service()
        with pytest.raises(ValueError, match="trace 来源需要选择数据集"):
            service._validate_plugin_config({"depth": "quick", "source": "trace"}, None)

    def test_trace_requires_hub_file_field(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields", lambda db, ds: []
        )
        service, _ = _make_service()
        with pytest.raises(ValueError, match="不含 anc-hub 文件字段"):
            service._validate_plugin_config({"depth": "quick", "source": "trace"}, "ds_1")

    def test_unknown_source_rejected(self):
        service, _ = _make_service()
        with pytest.raises(ValueError, match="不支持的插件评测来源"):
            service._validate_plugin_config({"depth": "quick", "source": "no_such_source"})

    def test_trace_rejects_unknown_judgement(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        service, _ = _make_service()
        with pytest.raises(ValueError, match="不支持的 trace 判定方式"):
            service._validate_plugin_config(
                {"depth": "quick", "source": "trace", "trace_judgement": "turbo"}, "ds_1"
            )

    def test_trace_accepts_offline_judgement(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        service, _ = _make_service()
        service._validate_plugin_config(
            {"depth": "quick", "source": "trace", "trace_judgement": "offline"}, "ds_1"
        )

    def test_trace_concurrency_accepted(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        service, _ = _make_service()
        service._validate_plugin_config(
            {"depth": "quick", "source": "trace", "trace_concurrency": 20}, "ds_1"
        )

    @pytest.mark.parametrize("bad", [0, 21, -1])
    def test_trace_concurrency_out_of_range(self, monkeypatch, bad):
        """plugin-eval 的 EvalConfig 是 le=20，越界会让整行判失败，故创建期就拒绝"""
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        service, _ = _make_service()
        with pytest.raises(ValueError, match="trace_concurrency 必须在 1~20 之间"):
            service._validate_plugin_config(
                {"depth": "quick", "source": "trace", "trace_concurrency": bad}, "ds_1"
            )

    @pytest.mark.parametrize("bad", ["4", 4.5, True])
    def test_trace_concurrency_must_be_int(self, monkeypatch, bad):
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        service, _ = _make_service()
        with pytest.raises(ValueError, match="trace_concurrency 必须是整数"):
            service._validate_plugin_config(
                {"depth": "quick", "source": "trace", "trace_concurrency": bad}, "ds_1"
            )

    def test_trace_skips_depth_guard(self, monkeypatch):
        """trace 的深度由服务端固定（不用表单选），综合分口径的深度守卫不适用"""
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        service, _ = _make_service()
        monkeypatch.setattr(
            EvaluationTaskService,
            "_validate_plugin_depth_target",
            lambda self, plugin_config, depth: pytest.fail("trace 不应触发深度守卫"),
        )
        service._validate_plugin_config({"depth": "standard", "source": "trace"}, "ds_1")

    def test_create_task_trace_without_dataset_rejected(self):
        service, _ = _make_service()
        with pytest.raises(ValueError, match="trace 来源需要选择数据集"):
            service.create_task(
                name="trace任务",
                evaluation_type="plugin",
                plugin_config={"depth": "quick", "source": "trace"},
            )

    def test_create_task_trace_with_dataset_ok(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        service, db = _make_service()
        with patch.object(EvaluationTaskService, "_validate_targets_required"), \
             patch.object(EvaluationTaskService, "_validate_script_targets"), \
             patch.object(EvaluationTaskService, "_validate_reference_marks"), \
             patch("app.services.config_snapshot.build_model_config_snapshot", return_value={}):
            task = service.create_task(
                name="trace任务",
                evaluation_type="plugin",
                dataset_id="ds_1",
                plugin_config={"depth": "quick", "source": "trace"},
            )
        assert task.plugin_config["source"] == "trace"

    def test_update_task_validates_plugin_config(self):
        """编辑任务换来源同样受校验（此前 update 路径漏掉插件配置校验）"""
        service, _ = _make_service()
        task = MagicMock()
        task.evaluation_type = "plugin"
        task.plugin_config = {"depth": "quick", "source": "manual", "plugin_path": "/x"}
        task.dataset_id = None
        task.consistency_config = None
        task.evaluation_targets = None
        task.score_details = None
        task.evaluation_metrics = None
        task.judge_variable_config = None
        with patch.object(EvaluationTaskService, "get_task", return_value=task), \
             patch.object(EvaluationTaskService, "_validate_targets_required"), \
             patch.object(EvaluationTaskService, "_validate_script_targets"), \
             patch.object(EvaluationTaskService, "_validate_reference_marks"):
            with pytest.raises(ValueError, match="trace 来源需要选择数据集"):
                service.update_task(
                    "task-1", plugin_config={"depth": "quick", "source": "trace"}
                )


class TestPluginTraceRowExecution:
    """trace 口径行执行：hub 执行包目录取值、未测不失败、异常落失败行"""

    def _make_task(self, plugin_config=None):
        task = MagicMock()
        task.evaluation_type = "plugin"
        task.id = "task-1"
        task.dataset_id = "ds_1"
        task.plugin_config = plugin_config or {"depth": "quick", "source": "trace"}
        return task

    def _patch_trace(self, monkeypatch, seen, row=None):
        def fake_evaluate_trace(self, package_dir, on_tick=None, design_cache=None):
            seen.append(package_dir)
            return row if row is not None else {
                "success": True,
                "evaluation_mode": "trace",
                "target_type": "plugin",
                "target_name": "anc-tms",
                "average_score": 83.33,
                "badge": "",
                "dimension_scores": {},
                "anti_patterns": [],
                "real_data": {"status": "partial", "score": 83.33, "counts": {"passed": 5}},
                "trace": {"mode": "trace"},
                "error": "",
            }

        monkeypatch.setattr(
            "app.services.plugin_evaluator.PluginEvaluator.evaluate_trace", fake_evaluate_trace
        )

    def test_dispatches_to_trace_branch(self):
        """source=trace 时 _execute_plugin_evaluation 转交 trace 分支"""
        task = self._make_task()
        with patch("app.tasks.evaluation_tasks_async._execute_plugin_trace_evaluation") as mock_exec:
            mock_exec.return_value = []
            _execute_plugin_evaluation(task, [], "task-1", None, None, execution_number=1)
        mock_exec.assert_called_once()

    def test_manual_source_not_routed_to_trace(self):
        task = self._make_task({"depth": "quick", "source": "manual", "plugin_path": ""})
        with patch("app.tasks.evaluation_tasks_async._execute_plugin_trace_evaluation") as mock_exec:
            _execute_plugin_evaluation(task, [], "task-1", None, None, execution_number=1)
        mock_exec.assert_not_called()

    def test_row_uses_mirrored_hub_dir(self, monkeypatch):
        seen = []
        self._patch_trace(monkeypatch, seen)
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        task = self._make_task()
        payloads = [{"_orig_idx": 0, "_dataset_item": {"data": {"hub_file_id": "/task/hub/abc"}}}]
        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_trace_evaluation(
                task, payloads, "task-1", None, None, execution_number=1
            )
        assert seen == ["/task/hub/abc"]
        assert results[0]["average_score"] == 83.33
        assert results[0]["idx"] == 0

    def test_flat_payload_variable_preferred(self, monkeypatch):
        seen = []
        self._patch_trace(monkeypatch, seen)
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        task = self._make_task()
        payloads = [{
            "_orig_idx": 1,
            "hub_file_id": "/task/hub/flat",
            "_dataset_item": {"data": {"hub_file_id": "/task/hub/dataset"}},
        }]
        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            _execute_plugin_trace_evaluation(task, payloads, "task-1", None, None, execution_number=1)
        assert seen == ["/task/hub/flat"]

    def test_dataset_without_hub_field_passes_empty(self, monkeypatch):
        """数据集无 hub 字段：行按未测处理（空目录），不中断任务"""
        seen = []
        self._patch_trace(monkeypatch, seen)
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields", lambda db, ds: []
        )
        task = self._make_task()
        payloads = [{"_orig_idx": 0, "_dataset_item": {"data": {"session_summary": "x"}}}]
        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            _execute_plugin_trace_evaluation(task, payloads, "task-1", None, None, execution_number=1)
        assert seen == [""]

    def test_exception_becomes_failure_row(self, monkeypatch):
        def boom(self, package_dir, on_tick=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "app.services.plugin_evaluator.PluginEvaluator.evaluate_trace", boom
        )
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        task = self._make_task()
        payloads = [{"_orig_idx": 3, "_dataset_item": {"data": {"hub_file_id": "/p"}}}]
        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_trace_evaluation(
                task, payloads, "task-1", None, None, execution_number=1
            )
        assert results[0]["success"] is False
        assert results[0]["idx"] == 3
        assert results[0]["evaluation_mode"] == "trace"

    def test_kitchen_sink_logs_and_heartbeat(self, monkeypatch):
        """行内进度日志 + 心跳由 on_tick 驱动（长行不被收尸 reaper 误判）"""
        seen = {}

        class SpyEvaluator:
            def __init__(self, **kwargs):
                seen["init"] = kwargs

            def evaluate_trace(self, package_dir, on_tick=None, design_cache=None):
                seen["package_dir"] = package_dir
                seen["on_tick"] = on_tick
                seen["design_cache"] = design_cache
                if on_tick:
                    on_tick(600)
                return {
                    "success": True, "evaluation_mode": "trace", "target_type": "plugin",
                    "target_name": "anc-tms", "average_score": None, "badge": "",
                    "dimension_scores": {}, "anti_patterns": [],
                    "real_data": {"status": "unmeasured", "score": None, "counts": {}},
                    "trace": {"mode": "trace", "unavailable_reason": "线程里没有可评插件的技能调用"},
                    "error": "",
                }

        monkeypatch.setattr("app.services.plugin_evaluator.PluginEvaluator", SpyEvaluator)
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        beats, logs = [], []
        monkeypatch.setattr(
            EvaluationTaskService, "touch_heartbeat",
            lambda self, task_id, execution_number=None: beats.append((task_id, execution_number)),
        )
        from app.services.evaluation_task_service import TaskExecutionLogService

        monkeypatch.setattr(
            TaskExecutionLogService, "add_log",
            lambda self, task_id, level, message: logs.append((level, message)),
        )

        task = self._make_task({"depth": "quick", "source": "trace", "trace_judgement": "offline"})
        payloads = [{"_orig_idx": 0, "_dataset_item": {"data": {"hub_file_id": "/task/hub/abc"}}}]
        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_trace_evaluation(
                task, payloads, "task-1", None, None, execution_number=1
            )

        assert seen["package_dir"] == "/task/hub/abc"
        assert seen["init"]["trace_judgement"] == "offline"
        assert seen["on_tick"] is not None
        assert beats == [("task-1", 1)]                      # tick 刷新执行心跳
        assert any("开始评测" in m for _, m in logs)           # 行开始可见
        assert any("未测" in m for _, m in logs)               # 未测原因可见
        assert results[0]["trace"]["unavailable_reason"]

    def test_stop_event_breaks_loop(self, monkeypatch):
        seen = []
        self._patch_trace(monkeypatch, seen)
        monkeypatch.setattr(
            "app.services.hub_file_service.get_hub_file_fields",
            lambda db, ds: ["hub_file_id"],
        )
        stop_event = MagicMock()
        stop_event.is_set.return_value = True
        task = self._make_task()
        payloads = [{"_orig_idx": 0, "_dataset_item": {"data": {"hub_file_id": "/p"}}}]
        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_trace_evaluation(
                task, payloads, "task-1", None, stop_event, execution_number=1
            )
        assert results == []
        assert seen == []


class TestExecutionDispatch:
    """执行分发：plugin 类型命中专用路径"""

    def _make_task(self, evaluation_type="plugin", **kwargs):
        task = MagicMock()
        task.evaluation_type = evaluation_type
        task.id = "task-1"
        task.judge_count = 4
        task.plugin_config = kwargs.get("plugin_config", {"depth": "quick"})
        return task

    def test_plugin_branch_hit(self):
        task = self._make_task()
        payloads = [{"user_input": "x"}]
        with patch("app.tasks.evaluation_tasks_async._execute_plugin_evaluation") as mock_exec:
            mock_exec.return_value = [{"idx": 0, "success": True}]
            _execute_evaluation(task, payloads, None, "task-1")
        mock_exec.assert_called_once()
        assert mock_exec.call_args[0][0] is task


class TestPluginRowExecution:
    """行执行：来源解析、任务级兜底、失败行、行级落库"""

    def _make_task(self, plugin_config=None):
        task = MagicMock()
        task.evaluation_type = "plugin"
        task.id = "task-1"
        task.plugin_config = plugin_config or {"depth": "quick"}
        return task

    def test_row_from_payload_flat_variable(self, good_skill_dir):
        task = self._make_task({"depth": "quick"})
        payloads = [{"_orig_idx": 0, "plugin_path": good_skill_dir, "user_input": "ignored"}]

        with patch("app.tasks.evaluation_tasks_async._persist_row") as mock_persist, \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, payloads, "task-1", None, None, execution_number=1)

        assert len(results) == 1
        row = results[0]
        assert row["success"] is True
        assert row["target_name"] == "demo-checker"
        assert row["idx"] == 0
        assert row["user_input"] == "ignored"
        mock_persist.assert_called_once()

    def test_row_value_type_unwrap_and_dataset_fallback(self, good_skill_dir):
        """{value,type} 形态变量解包 + 数据集行字段兜底"""
        task = self._make_task({"depth": "quick"})
        payloads = [{
            "_orig_idx": 2,
            "_dataset_item": {"data": {"plugin_path": good_skill_dir}},
            "skill_content": {"value": "", "type": "string"},
        }]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, payloads, "task-1", None, None, execution_number=1)

        assert results[0]["success"] is True
        assert results[0]["idx"] == 2

    def test_task_level_fallback_single_row(self, good_skill_dir):
        """无数据集行时：任务级 plugin_path 兜底为单行"""
        task = self._make_task({"depth": "quick", "plugin_path": good_skill_dir})

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, [], "task-1", None, None, execution_number=1)

        assert len(results) == 1
        assert results[0]["success"] is True

    def test_bad_path_row_fails_without_breaking_batch(self, good_skill_dir):
        """坏路径行按行失败，其余行继续"""
        task = self._make_task({"depth": "quick"})
        payloads = [
            {"_orig_idx": 0, "plugin_path": "/nonexistent/dir"},
            {"_orig_idx": 1, "plugin_path": good_skill_dir},
        ]

        with patch("app.tasks.evaluation_tasks_async._persist_row") as mock_persist, \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, payloads, "task-1", None, None, execution_number=1)

        assert results[0]["success"] is False
        assert "不存在" in results[0]["error"]
        assert results[1]["success"] is True
        assert mock_persist.call_count == 2

    def test_row_execution_error_isolated(self, good_skill_dir):
        """评测器抛异常：按行失败，不中断批次"""
        task = self._make_task({"depth": "quick"})
        payloads = [{"_orig_idx": 0, "plugin_path": good_skill_dir}]

        with patch("app.services.plugin_evaluator.PluginEvaluator.evaluate",
                   side_effect=RuntimeError("boom")), \
             patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, payloads, "task-1", None, None, execution_number=1)

        assert results[0]["success"] is False
        assert "boom" in results[0]["error"]


class TestMarketplaceSourceRow:
    """任务级 marketplace 来源：解析为市场插件目录单行；失败产失败行"""

    def _make_task(self, plugin_config):
        task = MagicMock()
        task.evaluation_type = "plugin"
        task.id = "task-1"
        task.plugin_config = plugin_config
        return task

    def test_marketplace_source_resolves_to_single_row(self, good_skill_dir):
        task = self._make_task({"depth": "quick", "source": "marketplace", "market_id": 1,
                                "market_plugin": "./public/anc-demo"})
        info = {"path": str(good_skill_dir), "market_id": 1, "market_name": "m",
                "branch": "main", "source": "./public/anc-demo", "commit": "abc123def456",
                "stale": False, "refreshed": True, "refresh_error": "", "last_synced_at": None}
        with patch("app.services.plugin_marketplace_service.prepare_market_plugin_for_eval",
                   return_value=info), \
             patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, [], "task-1", None, None, execution_number=1)

        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0]["target_name"] == "demo-checker"
        # 版本锚点随行结果落库（回答「这个分是哪个 commit 的插件」）
        assert results[0]["market_info"]["commit"] == "abc123def456"

    def test_marketplace_branch_passed_to_resolver(self, good_skill_dir):
        """选了分支：任务级解析按该分支取副本，分支锚点随行结果落库"""
        task = self._make_task({"depth": "quick", "source": "marketplace", "market_id": 1,
                                "market_plugin": "./public/anc-demo",
                                "market_branch": "feature/plugin-x"})
        info = {"path": str(good_skill_dir), "market_id": 1, "market_name": "m",
                "branch": "feature/plugin-x", "branch_selected": "feature/plugin-x",
                "repo_root": "/market/branches/1/feature%2Fplugin-x",
                "source": "./public/anc-demo", "commit": "abc123def456",
                "stale": False, "refreshed": False, "refresh_error": "", "last_synced_at": None}
        with patch("app.services.plugin_marketplace_service.prepare_market_plugin_for_eval",
                   return_value=info) as mock_prepare, \
             patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, [], "task-1", None, None, execution_number=1)

        assert mock_prepare.call_args.kwargs["branch"] == "feature/plugin-x"
        assert results[0]["market_info"]["branch_selected"] == "feature/plugin-x"
        assert results[0]["market_info"]["commit"] == "abc123def456"

    def test_marketplace_resolution_failure_yields_failed_row(self):
        task = self._make_task({"depth": "quick", "source": "marketplace", "market_id": 1,
                                "market_plugin": "./gone"})
        with patch("app.services.plugin_marketplace_service.prepare_market_plugin_for_eval",
                   side_effect=RuntimeError("市场本地目录缺失")), \
             patch("app.tasks.evaluation_tasks_async._persist_row") as mock_persist, \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, [], "task-1", None, None, execution_number=1)

        assert len(results) == 1
        assert results[0]["success"] is False
        assert "市场插件路径解析失败" in results[0]["error"]
        mock_persist.assert_called_once()

    def test_marketplace_ignored_when_rows_present(self, good_skill_dir):
        """有数据集行时任务级市场来源不参与（行字段优先）"""
        task = self._make_task({"depth": "quick", "source": "marketplace", "market_id": 1,
                                "market_plugin": "./public/anc-demo"})
        payloads = [{"_orig_idx": 0, "plugin_path": good_skill_dir}]
        with patch("app.services.plugin_marketplace_service.prepare_market_plugin_for_eval") as mock_prepare, \
             patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, payloads, "task-1", None, None, execution_number=1)

        mock_prepare.assert_not_called()
        assert results[0]["success"] is True

    def test_marketplace_stale_fallback_still_evaluates(self, good_skill_dir):
        """懒加载同步失败降级：用本地副本继续评，行结果标 stale 留痕"""
        task = self._make_task({"depth": "quick", "source": "marketplace", "market_id": 1,
                                "market_plugin": "./public/anc-demo"})
        info = {"path": str(good_skill_dir), "market_id": 1, "market_name": "m",
                "branch": "main", "source": "./public/anc-demo", "commit": "old123",
                "stale": True, "refreshed": False, "refresh_error": "git fetch 失败: net down",
                "last_synced_at": None}
        with patch("app.services.plugin_marketplace_service.prepare_market_plugin_for_eval",
                   return_value=info), \
             patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, [], "task-1", None, None, execution_number=1)

        assert results[0]["success"] is True
        assert results[0]["market_info"]["stale"] is True
        assert "net down" in results[0]["market_info"]["refresh_error"]


class TestPluginAggregation:
    """插件评测聚合"""

    def _row(self, idx=0, score=75.0, badge="silver", dims=None, anti=None):
        return {
            "idx": idx,
            "success": True,
            "target_type": "skill",
            "target_name": f"skill-{idx}",
            "plugin_eval": {
                "depth": "quick",
                "layers": [{"layer": "static", "score": score / 100, "sub_scores": {}}],
                "composite": {
                    "score": score,
                    "anti_pattern_penalty": 1.0,
                    "confidence_label": "Estimated",
                    "dimensions": [],
                    "badge": badge,
                },
                "model_usage": {},
                "duration_ms": 5,
                "warnings": [],
            },
            "average_score": score,
            "badge": badge,
            "dimension_scores": dims or {"triggering_accuracy": 0.8, "token_efficiency": 0.6},
            "anti_patterns": anti or [],
            "error": "",
        }

    def test_empty(self):
        agg = ResultAggregator.aggregate_plugin_results([])
        assert agg["total"] == 0
        assert agg["items"] == []
        assert agg["average_score"] == 0

    def test_aggregate_success_rows(self):
        rows = [
            self._row(0, 80.0, "gold", {"triggering_accuracy": 0.9}),
            self._row(1, 70.0, "silver", {"triggering_accuracy": 0.7}),
            {"idx": 2, "success": False, "error": "路径不存在", "average_score": None,
             "plugin_eval": None, "badge": "", "dimension_scores": {}, "anti_patterns": []},
        ]
        agg = ResultAggregator.aggregate_plugin_results(rows)
        assert agg["total"] == 3
        assert agg["success_count"] == 2
        assert agg["failed_count"] == 1
        assert agg["average_score"] == 75.0
        assert agg["badge_summary"] == {"gold": 1, "silver": 1}
        assert agg["dimension_scores"]["triggering_accuracy"]["average"] == pytest.approx(0.8)
        assert len(agg["items"]) == 2
        assert agg["failed_items"][0]["error"] == "路径不存在"

    def test_anti_pattern_summary(self):
        rows = [
            self._row(0, anti=[
                {"flag": "OVER_CONSTRAINED", "description": "too many MUST", "severity": 0.1},
            ]),
            self._row(1, anti=[
                {"flag": "OVER_CONSTRAINED", "description": "too many MUST", "severity": 0.1},
                {"flag": "MISSING_TRIGGER", "description": "no trigger", "severity": 0.15},
            ]),
        ]
        agg = ResultAggregator.aggregate_plugin_results(rows)
        assert agg["anti_pattern_summary"]["OVER_CONSTRAINED"]["count"] == 2
        assert agg["anti_pattern_summary"]["MISSING_TRIGGER"]["count"] == 1
        assert agg["anti_pattern_summary"]["MISSING_TRIGGER"]["max_severity"] == 0.15

    def test_market_version_anchor_in_items_and_top_level(self):
        """市场来源行：版本锚点进 items，单目标时提到顶层；manual 来源为 None"""
        market = {"market_id": 1, "market_name": "m", "source": "./public/anc-demo",
                  "commit": "abc123def456", "stale": False}
        row = self._row(0, 82.0, "gold")
        row["market_info"] = market

        agg = ResultAggregator.aggregate_plugin_results([row])
        assert agg["items"][0]["market"] == market
        assert agg["market"]["commit"] == "abc123def456"

        manual = ResultAggregator.aggregate_plugin_results([self._row(0, 82.0, "gold")])
        assert manual["items"][0]["market"] is None
        assert manual["market"] is None


class TestPluginExport:
    """插件评测导出（excel/csv 聚合结构）"""

    def _aggregated(self):
        return {
            "total": 1,
            "success_count": 1,
            "failed_count": 0,
            "average_score": 80.0,
            "badge_summary": {"gold": 1},
            "dimension_scores": {"triggering_accuracy": {"average": 0.9, "count": 1}},
            "anti_pattern_summary": {},
            "items": [{
                "idx": 0,
                "target_name": "demo-checker",
                "target_type": "skill",
                "score": 80.0,
                "badge": "gold",
                "confidence_label": "Estimated",
                "depth": "quick",
                "anti_pattern_penalty": 1.0,
                "dimension_scores": {"triggering_accuracy": 0.9},
                "dimensions": [],
                "anti_patterns": [{"flag": "MISSING_TRIGGER", "description": "x", "severity": 0.15}],
                "layers": [],
                "warnings": [],
                "model_usage": {},
            }],
            "failed_items": [],
        }

    def test_export_excel(self):
        data = ResultExporter.export_to_excel([self._aggregated()], "plugin")
        assert len(data) > 0
        assert data[:2] == b"PK"  # xlsx 是 zip 容器

    def test_export_csv(self):
        csv_text = ResultExporter.export_to_csv([self._aggregated()], "plugin")
        assert "demo-checker" in csv_text
        assert "MISSING_TRIGGER" in csv_text
