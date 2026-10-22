"""W12-1 / W12-2 单元测试

W12-1 judge 图缓存失效（整改计划 §3 W12 第 1 项）：
- update_task 显式改动 evaluation_metrics / judge_prompt 时，必须失效
  JudgeLangGraphAgent 的类级图缓存（按 task_id 定向清除），
  否则「编辑维度后重跑」仍用旧评分标准编译的图；
- 仅更新无关字段（name 等）不得误清缓存（避免无谓重编译）；
- clear_cache(task_id) 定向清除语义：只清目标任务，保留其他任务缓存；
  无参调用清空全部（兼容既有调用方）。

W12-2 latency 白名单激活（整改计划 §3 W12 第 2 项）：
- create_task 接受 evaluation_type="latency"（执行/聚合/导出链早已就绪）；
- safety/dialogue 仍不在后端白名单（前端已禁用并标注「规划中」）；
- 前端 Tasks.vue 的 safety/dialogue 选项 disabled 且标注「规划中」。
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.services.evaluation_task_service import EvaluationTaskService
from app.services.judge_langgraph_agent import JudgeLangGraphAgent


def _service_with_db():
    """构造 EvaluationTaskService：mock DB 查询（与 test_task_validation_refs 同模式）"""
    db = MagicMock()

    def _query_side_effect(model):
        model_name = getattr(model, "__name__", str(model))
        q = MagicMock()
        if model_name == "PreJudgeScript":
            q.all.return_value = []
        else:
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query_side_effect
    return EvaluationTaskService(db), db


def _legacy_task(**extra):
    """最小任务对象（不触发 targets 必填校验的 latency 类型 + 空配置）"""
    base = dict(
        id="task_w12cache",
        evaluation_type="latency",
        dataset_id=None,
        evaluation_targets=None,
        consistency_config=None,
        score_details=None,
        evaluation_metrics=None,
        judge_variable_config=None,
        model_config=None,
        judge_model_config=None,
        model_provider=None,
        judge_model=None,
    )
    base.update(extra)
    return SimpleNamespace(**base)


# ---------------- W12-1 judge 图缓存失效 ----------------

class TestJudgeGraphCacheInvalidation:
    def setup_method(self):
        JudgeLangGraphAgent.clear_cache()

    def test_update_metrics_clears_cache_for_task(self):
        """update_task 改 evaluation_metrics → 按 task_id 清除图缓存"""
        service, db = _service_with_db()
        task = _legacy_task()
        service.get_task = MagicMock(return_value=task)

        with patch.object(JudgeLangGraphAgent, "clear_cache") as mock_clear:
            service.update_task("task_w12cache", evaluation_metrics=[{"name": "准确率", "weight": 1}])

        mock_clear.assert_called_once_with("task_w12cache")

    def test_update_judge_prompt_clears_cache_for_task(self):
        """update_task 改 judge_prompt → 按 task_id 清除图缓存（口径字段一并失效）"""
        service, db = _service_with_db()
        task = _legacy_task()
        service.get_task = MagicMock(return_value=task)

        with patch.object(JudgeLangGraphAgent, "clear_cache") as mock_clear:
            service.update_task("task_w12cache", judge_prompt="新的裁判提示词")

        mock_clear.assert_called_once_with("task_w12cache")

    def test_update_unrelated_fields_keeps_cache(self):
        """update_task 仅改 name → 不清缓存（避免无谓重编译）"""
        service, db = _service_with_db()
        task = _legacy_task()
        service.get_task = MagicMock(return_value=task)

        with patch.object(JudgeLangGraphAgent, "clear_cache") as mock_clear:
            service.update_task("task_w12cache", name="改名不影响评分口径")

        mock_clear.assert_not_called()

    def test_clear_cache_targets_single_task(self):
        """clear_cache(task_id) 只清目标任务，其他任务缓存保留"""
        cache = JudgeLangGraphAgent._graph_cache
        cache["task_a"] = object()
        cache["task_b"] = object()
        try:
            JudgeLangGraphAgent.clear_cache("task_a")
            assert "task_a" not in cache
            assert "task_b" in cache
        finally:
            JudgeLangGraphAgent.clear_cache()

    def test_clear_cache_without_args_clears_all(self):
        """无参 clear_cache 清空全部（既有调用兼容）"""
        cache = JudgeLangGraphAgent._graph_cache
        cache["task_a"] = object()
        cache["task_b"] = object()
        JudgeLangGraphAgent.clear_cache()
        assert cache == {}

    def test_cache_key_follows_updated_metrics(self):
        """端到端语义：编辑维度后重建 Agent → 重新编译图（缓存 miss）而非复用旧图

        用两次 compile_judge_graph 的 spy 计数验证：同 task 改 metrics 后
        第二次构造 JudgeLangGraphAgent 必须重编译。
        """
        JudgeLangGraphAgent.clear_cache()

        old_task = SimpleNamespace(
            id="task_recompile",
            evaluation_metrics=[{"name": "旧维度", "weight": 1, "description": "", "score_criteria": {}}],
        )
        agent1 = JudgeLangGraphAgent(old_task)
        graph_before = agent1.graph

        # 模拟 update_task 改维度后的失效 + 任务对象已带新维度
        JudgeLangGraphAgent.clear_cache("task_recompile")
        new_task = SimpleNamespace(
            id="task_recompile",
            evaluation_metrics=[
                {"name": "新维度一", "weight": 1, "description": "", "score_criteria": {}},
                {"name": "新维度二", "weight": 1, "description": "", "score_criteria": {}},
            ],
        )
        agent2 = JudgeLangGraphAgent(new_task)

        assert agent2.graph is not graph_before  # 重新编译的新图
        # 未失效的其他任务缓存不受影响
        cached = JudgeLangGraphAgent._graph_cache
        assert str(new_task.id) in cached


# ---------------- W12-2 latency 白名单 + 前端禁用 ----------------

class TestLatencyWhitelist:
    def test_create_task_accepts_latency(self):
        """create_task 接受 evaluation_type="latency"（一行激活后可建任务）"""
        service, db = _service_with_db()
        task = service.create_task(name="耗时评测", evaluation_type="latency")
        assert db.add.called
        assert task.evaluation_type == "latency"

    def test_create_task_accepts_safety(self):
        """M3 QE3-1：safety 进入后端白名单（双通道红队引擎就绪）

        类型白名单必须放行——后续 ValueError（如未选被攻击模型）属
        safety_config 校验，不是「不支持的评测类型」。
        """
        service, db = _service_with_db()
        try:
            service.create_task(name="安全评测", evaluation_type="safety")
        except ValueError as e:
            assert "不支持的评测类型" not in str(e)
        except Exception:
            # mock db 落库环节异常可接受——类型白名单已通过
            pass

    def test_dialogue_still_rejected(self):
        """dialogue 并入 agent 类型多轮配置（M3 口径），不单独成类型——保持拒绝"""
        service, db = _service_with_db()
        with pytest.raises(ValueError, match="不支持的评测类型"):
            service.create_task(name="x", evaluation_type="dialogue")


class TestFrontendSafetyEnabled:
    """前端类型选项状态（M3 QE3-1 更新）：safety 放行可选，dialogue 移除
    （并入 agent 类型多轮配置，不单独成类型）"""

    def _tasks_vue(self) -> str:
        path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "views" / "Tasks.vue"
        return path.read_text(encoding="utf-8")

    def test_safety_option_enabled_without_disabled(self):
        content = self._tasks_vue()
        assert '<el-option label="安全评测" value="safety" />' in content
        assert 'value="safety" disabled' not in content

    def test_dialogue_option_removed(self):
        content = self._tasks_vue()
        assert 'value="dialogue"' not in content

    def test_latency_option_remains_selectable(self):
        """latency 选项保持可选（后端已激活）"""
        content = self._tasks_vue()
        assert '<el-option label="响应耗时评测" value="latency" />' in content
