"""W12-3 / W12-6 单元测试

W12-3 脚本置空显式化（整改计划 §3 W12 第 3 项）：
- resolve_script_output_value 三个置空分支（未配置 script / 脚本输出不存在 /
  缺少输出键）在 missing_out 收集器上留下明细；不传收集器行为不变；
- evaluation_targets 的 script_output 行被置空（脚本 on_fail=continue 失败 /
  未编排）→ 该行 row_status="pre_script_failed"、跳过裁判、success=False；
- judge_variable_config 裁判变量引用的 script_output 被置空 → 同上；
- pre_script_failed 行不进任务均分（对照旧行为：置空内容走空值校验
  0 分进均分，即「脚本故障=模型 0 分」）；落库派生为 failed（可续跑重跑）。

W12-6 judge_payload["script_files"] 死键（整改计划 §3 W12 第 6 项）：
- 脚本产物文件路径（script_files）非空时，裁判 payload 不再携带该死键
  （裁判全链路只消费显式声明的 script_output，旧赋值注释从未真实接线）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.services.evaluation_executor import EvaluationExecutor
from app.services.evaluation_result_service import derive_row_status
from app.services.judge_variable_resolver import EvaluationTarget, JudgeVariableResolver
from app.services.model_caller import ModelCallerSync
from app.services.result_aggregator import ResultAggregator
from app.services.variable_injector import VariableInjector, resolve_script_output_value


# ---------------- W12-3: resolve_script_output_value 置空收集 ----------------

class TestResolveScriptOutputMissingCollection:
    def test_unconfigured_script_recorded(self):
        """未配置 script（输出键名）→ 置空 + 明细 reason=script_not_configured"""
        missing = []
        value = resolve_script_output_value({}, {"source": "script_output"}, "v1", missing_out=missing)
        assert value == ""
        assert missing == [{"var": "v1", "script": None, "output_key": None, "reason": "script_not_configured"}]

    def test_script_output_missing_recorded(self):
        """引用的脚本输出不存在（脚本失败/未编排）→ 置空 + 明细"""
        missing = []
        cfg = {"source": "script_output", "source_config": {"script": "s1", "key": "x"}}
        value = resolve_script_output_value({}, cfg, "v1", missing_out=missing)
        assert value == ""
        assert missing == [{"var": "v1", "script": "s1", "output_key": "x", "reason": "script_output_missing"}]

    def test_output_key_missing_recorded(self):
        """脚本输出存在但缺少键 → 置空 + 明细"""
        missing = []
        cfg = {"source": "script_output", "source_config": {"script": "s1", "key": "nope"}}
        value = resolve_script_output_value({"s1": {"x": "val"}}, cfg, "v1", missing_out=missing)
        assert value == ""
        assert missing == [{"var": "v1", "script": "s1", "output_key": "nope", "reason": "output_key_missing"}]

    def test_normal_value_not_recorded(self):
        """正常取值 → 收集器为空"""
        missing = []
        cfg = {"source": "script_output", "source_config": {"script": "s1", "key": "x"}}
        value = resolve_script_output_value({"s1": {"x": "val"}}, cfg, "v1", missing_out=missing)
        assert value == "val"
        assert missing == []

    def test_without_collector_behavior_unchanged(self):
        """不传 missing_out（默认 None）→ 置空返回空串，向后兼容"""
        cfg = {"source": "script_output", "source_config": {"script": "s1", "key": "x"}}
        assert resolve_script_output_value({}, cfg, "v1") == ""


class TestResolverTargetsMissingCollection:
    def test_targets_script_output_missing_recorded(self):
        """evaluation_targets 的 script_output 行取不到脚本输出 → resolver 收集明细"""
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={},
            model_result={"content": "模型输出"},
            script_outputs={},  # 脚本失败/未编排：无输出
        )
        targets = [EvaluationTarget(
            name="out1", source="script_output",
            source_config={"script_id": "s1", "output_var": "s1", "output_key": "x"},
            required=True, display_name="脚本输出",
        )]
        results = resolver.resolve_targets(targets)
        assert results[next(iter(results))]["is_empty"] is True
        assert resolver.script_output_missing == [
            {"script": "s1", "output_key": "x", "reason": "script_output_missing"}
        ]


# ---------------- W12-3: 执行链路行级 pre_script_failed ----------------

_METRIC = {"name": "准确率", "weight": 1, "description": "答对程度", "score_criteria": {}}


def _base_task(**extra):
    base = dict(
        id="task_w12script",
        evaluation_type="judge",
        dataset_id=None,
        evaluation_targets=None,
        pre_scripts=None,
        judge_variable_config={},
        runtime_variables={},
        variable_sources={},
        variable_types={},
        variable_mappings={},
        enable_user_import=False,
        user_import_template=None,
        judge_count=1,
        judge_engine="graph",
        judge_prompt=None,
        judge_model_config={"provider": "http", "model_type": "mock", "api_key": "k"},
        model_config={"provider": "http", "model_type": "mock", "api_key": "k"},
        model_provider=None,
        score_details="",
        evaluation_metrics=[dict(_METRIC)],
        max_score=10,
        scene_desc="",
        evaluation_scene="",
        steps=[],
        input_field="user_input",
        reference_field="answer",
        multi_judge_config=None,
        parallel_config=None,
        team_code="T",
        consistency_config=None,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _payload(idx=0, user_input="你好", dataset_item=None):
    return {
        "idx": idx,
        "user_input": user_input,
        "ref_answer": "",
        "_dataset_item": dataset_item or {"id": f"d{idx}", "data": {}},
    }


@pytest.fixture()
def mock_model_call(monkeypatch):
    """mock 被测模型调用：固定成功输出"""
    calls = []

    def _call(self, messages, model_config, runtime_variables):
        calls.append(messages)
        return {"success": True, "content": "模型回答文本", "chain_results": []}

    monkeypatch.setattr(ModelCallerSync, "call", _call)
    return calls


def _executor():
    return EvaluationExecutor(max_workers=1, execution_number=None)


class TestRowPreScriptFailed:
    def test_targets_script_output_missing_marks_row(self, mock_model_call, monkeypatch):
        """targets 的 script_output 行缺失（脚本失败 continue）→ 行级 pre_script_failed、跳过裁判"""
        # 脚本编排派生自 targets（script_id=s1），模拟脚本执行失败 on_fail=continue：无输出
        monkeypatch.setattr(
            "app.services.pre_judge_runner.run_pre_scripts",
            lambda *a, **k: {
                "aborted": False, "error": None,
                "pre_script_results": [{"script_id": "s1", "status": "fail"}],
                "script_outputs": {}, "script_files": {},
            },
        )
        task = _base_task(evaluation_targets=[{
            "id": "t1", "name": "out1", "source": "script_output",
            "source_config": {"script_id": "s1", "output_var": "s1", "output_key": "x"},
            "display_name": "脚本输出", "required": True,
        }])
        row = _executor()._evaluate_single(_payload(), task)

        assert row["row_status"] == "pre_script_failed"
        assert row["success"] is False
        assert "前处理脚本输出缺失" in row["error"]
        assert row["judge_scores"] == []  # 跳过裁判
        assert row["judge_count"] == 0

    def test_judge_variable_script_output_missing_marks_row(self, mock_model_call):
        """裁判变量引用的 script_output 缺失 → 同样 pre_script_failed"""
        task = _base_task(
            evaluation_targets=[{
                "id": "t_model", "name": "model_output", "source": "model_output",
                "source_config": {}, "display_name": "模型输出", "required": True,
            }],
            judge_variable_config={
                "data_var": {
                    "name": "脚本数据", "source": "script_output",
                    "source_config": {"script": "s_missing", "key": "x"},
                    "prompt_type": "user",
                },
            },
        )
        row = _executor()._evaluate_single(_payload(), task)

        assert row["row_status"] == "pre_script_failed"
        assert row["success"] is False
        assert "被裁判变量引用" in row["error"]
        assert row["judge_scores"] == []

    def test_pre_script_failed_row_excluded_from_task_average(self, mock_model_call, monkeypatch):
        """pre_script_failed 行不进任务均分（对照旧行为 0 分拉低均值）"""
        monkeypatch.setattr(
            "app.services.pre_judge_runner.run_pre_scripts",
            lambda *a, **k: {
                "aborted": False, "error": None, "pre_script_results": [],
                "script_outputs": {}, "script_files": {},
            },
        )
        task = _base_task(evaluation_targets=[{
            "id": "t1", "name": "out1", "source": "script_output",
            "source_config": {"script_id": "s1", "output_var": "s1", "output_key": "x"},
            "display_name": "脚本输出", "required": True,
        }])
        bad_row = _executor()._evaluate_single(_payload(0), task)

        good_rows = [
            {"idx": 1, "success": True, "average_score": 8.0, "row_status": "completed",
             "judge_scores": [{"score": 8.0, "feedback": "好", "dimension_scores": {}}]},
            {"idx": 2, "success": True, "average_score": 6.0, "row_status": "completed",
             "judge_scores": [{"score": 6.0, "feedback": "中", "dimension_scores": {}}]},
        ]

        agg = ResultAggregator().aggregate([bad_row] + good_rows)
        # 旧行为：脚本故障 → 置空 → 空值校验 0 分进均分 → (0+8+6)/3≈4.67
        # 新行为：该行不进均分 → (8+6)/2=7.0
        assert agg["average_score"] == 7.0
        assert agg["failed_count"] == 1

    def test_pre_script_failed_row_persists_as_failed_for_resume(self, mock_model_call, monkeypatch):
        """落库口径：pre_script_failed 行派生为 failed（不在续跑跳过集合，脚本修复后可重跑）"""
        monkeypatch.setattr(
            "app.services.pre_judge_runner.run_pre_scripts",
            lambda *a, **k: {
                "aborted": False, "error": None, "pre_script_results": [],
                "script_outputs": {}, "script_files": {},
            },
        )
        task = _base_task(evaluation_targets=[{
            "id": "t1", "name": "out1", "source": "script_output",
            "source_config": {"script_id": "s1", "output_var": "s1", "output_key": "x"},
            "display_name": "脚本输出", "required": True,
        }])
        row = _executor()._evaluate_single(_payload(), task)

        assert derive_row_status(row) == "failed"
        assert row["row_status"] == "pre_script_failed"  # row_result JSON 中保留显式标记

    def test_normal_script_output_does_not_mark_row(self, mock_model_call, monkeypatch):
        """脚本输出正常取到 → 不触发 pre_script_failed，裁判正常执行并聚合"""
        monkeypatch.setattr(
            "app.services.pre_judge_runner.run_pre_scripts",
            lambda *a, **k: {
                "aborted": False, "error": None, "pre_script_results": [],
                "script_outputs": {"s1": {"x": "val"}},
                "script_files": {},
            },
        )
        task = _base_task(
            # legacy 路径触发 _run_pre_scripts（mock 供给 script_outputs）
            pre_scripts=[{"script_id": "s1", "on_fail": "continue", "output_var": "s1"}],
            evaluation_targets=[{
                "id": "t_model", "name": "model_output", "source": "model_output",
                "source_config": {}, "display_name": "模型输出", "required": True,
            }],
            judge_variable_config={
                "data_var": {
                    "name": "脚本数据", "source": "script_output",
                    "source_config": {"script": "s1", "key": "x"},
                    "prompt_type": "user",
                },
            },
        )
        # 裁判执行 mock：run_parallel 返回单裁判固定分
        captured = {}

        def _capture_user_prompt(self, payload, model_output, chain_results=None, **kw):
            captured["judge_payload"] = payload
            return "【脚本数据】<eval_data>val</eval_data>"

        with patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent.__init__",
                   lambda self, *a, **k: None), \
             patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent.run_parallel",
                   lambda self, state_input, judge_count=1: [
                       {"score": 8, "feedback": "好", "dimension_scores": {"准确率": 8}}]), \
             patch("app.services.prompt_builder.PromptBuilder.build_user_prompt_from_judge_vars",
                   _capture_user_prompt):
            row = _executor()._evaluate_single(_payload(), task)

        assert row["row_status"] == "completed"
        assert row["average_score"] == 8
        assert captured["judge_payload"]["data_var"] == "val"


# ---------------- W12-6: judge_payload 不再携带 script_files 死键 ----------------

class TestScriptFilesDeadKeyRemoved:
    def test_judge_payload_excludes_script_files(self, mock_model_call, monkeypatch):
        """script_files 非空时裁判 payload 也不携带该键（死键已删除）"""
        monkeypatch.setattr(
            "app.services.pre_judge_runner.run_pre_scripts",
            lambda *a, **k: {
                "aborted": False, "error": None, "pre_script_results": [],
                "script_outputs": {"s1": {"x": "val"}},
                "script_files": {"s1": {"report.md": "/tmp/report.md"}},
            },
        )
        task = _base_task(
            pre_scripts=[{"script_id": "s1", "on_fail": "continue", "output_var": "s1"}],
            evaluation_targets=[{
                "id": "t_model", "name": "model_output", "source": "model_output",
                "source_config": {}, "display_name": "模型输出", "required": True,
            }],
            judge_variable_config={
                "data_var": {
                    "name": "脚本数据", "source": "script_output",
                    "source_config": {"script": "s1", "key": "x"},
                    "prompt_type": "user",
                },
            },
        )

        captured = {}

        def _capture_user_prompt(self, payload, model_output, chain_results=None, **kw):
            captured["judge_payload"] = payload
            return "【脚本数据】<eval_data>val</eval_data>"

        with patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent.__init__",
                   lambda self, *a, **k: None), \
             patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent.run_parallel",
                   lambda self, state_input, judge_count=1: [
                       {"score": 7, "feedback": "好", "dimension_scores": {"准确率": 7}}]), \
             patch("app.services.prompt_builder.PromptBuilder.build_user_prompt_from_judge_vars",
                   _capture_user_prompt):
            row = _executor()._evaluate_single(_payload(), task)

        assert row["row_status"] == "completed"
        assert "script_files" not in captured["judge_payload"]
        # script_outputs 仍按既有契约附进裁判 payload（PromptBuilder 消费它解析变量）
        assert captured["judge_payload"]["script_outputs"] == {"s1": {"x": "val"}}
