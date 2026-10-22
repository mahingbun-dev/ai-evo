"""
run_evaluation_async payload 行序号标注回归测试

背景（缺陷）：judge/agent 默认执行路径 _evaluate_single 以
payload.get("idx", 0) 取行号，而 VariableInjector.generate_payload 从不
写入 idx——所有行 idx=0 入库，结果页详细结果序号全为 1。修复：payload
标注循环（W3 行身份标注处）同时写入 payload["idx"] = 原始行位。

关键约束：行序号必须与 hub 拆分/断点续跑过滤后的列表序号解耦
（过滤删行后，剩余 payload 的 idx 仍为数据项原始序号）。
"""
import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _make_items(n):
    return [{"id": f"d{i}", "index": i, "data": {"pkg": f"/mirror/f{i}"}} for i in range(n)]


def _run(task_id="t-1", items=None, completed_indexes=None):
    """按 TestRunEvaluationAsyncShortCircuit 同款打桩跑一遍 run_evaluation_async，
    返回 (传入执行器的 payloads, 聚合入口收到的 results)"""
    from app.tasks import evaluation_tasks_async as eta

    items = items if items is not None else _make_items(3)
    payloads = [{"user_input": f"u{i}", "_dataset_item": items[i]} for i in range(len(items))]
    captured = {"payloads": None, "results": None}

    def _fake_execute(task, pls, db, task_id, stop_event=None):
        captured["payloads"] = pls
        return [{"idx": p.get("idx"), "success": True} for p in pls]

    task = SimpleNamespace(
        id=task_id, name="任务1", dataset_id="ds-1", evaluation_type="judge",
        status="running",
    )
    with patch.object(eta, "get_db_session", return_value=MagicMock()), \
            patch("app.tasks.evaluation_tasks_async.EvaluationTaskService") as mock_task_svc_cls, \
            patch("app.tasks.evaluation_tasks_async.TaskExecutionLogService"), \
            patch("app.services.evaluation_result_service.get_completed_data_indexes",
                  return_value=completed_indexes or set()), \
            patch.object(eta, "_load_dataset", return_value=items), \
            patch.object(eta, "_process_media_files", side_effect=lambda t, i, db: i), \
            patch.object(eta, "_generate_payloads", return_value=payloads), \
            patch.object(eta, "_execute_evaluation", side_effect=_fake_execute), \
            patch.object(eta, "_aggregate_results",
                         side_effect=lambda results, t: (
                             captured.update(results=results) or {"results": results})):
        mock_task_svc_cls.return_value.get_task.return_value = task
        eta.run_evaluation_async(task_id)
    return captured


class TestPayloadRowIdxAnnotation:
    def test_payloads_carry_row_idx(self):
        """全量执行：payload 携带 idx == 数据项原始序号（与 _orig_idx 同值）"""
        captured = _run()
        assert captured["payloads"] is not None
        assert [p["idx"] for p in captured["payloads"]] == [0, 1, 2]
        assert [p["_orig_idx"] for p in captured["payloads"]] == [0, 1, 2]

    def test_idx_survives_resume_filtering(self):
        """断点续跑过滤删行后，剩余 payload 的 idx 仍为原始行位（不因列表序号前移而重排）"""
        captured = _run(completed_indexes={0})
        assert [p["idx"] for p in captured["payloads"]] == [1, 2]
        assert [p["_orig_idx"] for p in captured["payloads"]] == [1, 2]

    def test_results_rows_carry_idx(self):
        """执行器产出的行结果带正确 idx（聚合/落库/前端序号的事实源）

        用 _evaluate_single 的停止信号早退分支做真实代码探针：
        不发起模型调用即可断言 result["idx"] 取自 payload["idx"]
        （历史缺陷：payload 无 idx 时全部回退 0，结果页序号全为 1）。
        """
        import threading
        from app.services.evaluation_executor import EvaluationExecutor

        executor = EvaluationExecutor(task_id="t-1", execution_number=1)
        executor.stop_event = threading.Event()
        executor.stop_event.set()
        result = executor._evaluate_single(
            {"idx": 7, "user_input": "u"}, task=SimpleNamespace()
        )
        assert result["idx"] == 7
        assert result["success"] is False
        assert result["error"] == "任务已停止"
