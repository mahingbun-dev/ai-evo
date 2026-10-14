"""W3 行级结果落库 + 断点续跑 集成测试（整改计划 §2 W3「测试设计」）

在本地 PG（127.0.0.1:55434）**临时库**（conftest 供给，w3t_rowlevel_ 前缀，
会话结束 DROP）验证：

1. 全量基线：10 行 mock 评测逐行落库，complete 聚合含全部 10 行，
   result JSON 与旧行为同构；
2. upsert 幂等：同键两次写不重复（单测项，pg_insert 依赖 PG 方言在此验证）；
3. stop 中途：partial_result 为本轮已完成行（计划测试设计场景2）；
4. 软超时注入：任务标 failed 且已完成行保留（计划测试设计场景3）；
5. SIGKILL 子进程：已完成行可查 + 重新触发同 execution_number 只跑缺失行、
   聚合 result 含全部 10 行（计划测试设计场景1，续跑不重复计费）；
6. W3-5：delete_execution_record 联动删行级结果。

模型/裁判全部走固定打分表 mock（w3_mocks，§5.1 fake_judge_model 落地），
零网关依赖。
"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.integration.conftest import (
    fetch_result_rows,
    TEMP_DSN,
)
from tests.integration.w3_mocks import (
    expected_dimensions,
    expected_feedback,
    expected_score,
    install_w3_mocks,
    read_call_log,
)

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
CHILD_RUNNER = WORKTREE_ROOT / "tests" / "integration" / "w3_child_runner.py"

pytestmark = pytest.mark.integration


def _get_task(db, task_id):
    from app.models.task_models import EvaluationTask

    return db.query(EvaluationTask).filter(EvaluationTask.id == task_id).one()


def _get_exec_record(db, task_id, execution_number=1):
    from app.models.task_models import TaskExecutionRecord

    return (
        db.query(TaskExecutionRecord)
        .filter(
            TaskExecutionRecord.task_id == task_id,
            TaskExecutionRecord.execution_number == execution_number,
        )
        .one()
    )


def _run_in_process(task_id, stop_event=None, **hooks):
    """在当前进程以固定打分表 mock 运行 run_evaluation_async，返回 (结果, hooks)"""
    from app.tasks.evaluation_tasks_async import run_evaluation_async

    hook_state = dict(hooks)
    if stop_event is not None:
        hook_state["stop_event"] = stop_event  # 停止钩子需要事件对象
    with install_w3_mocks(hook_state):
        ret = run_evaluation_async(task_id, stop_event=stop_event)
    return ret, hook_state


def _count_rows(task_id: str, execution_number: int = 1) -> int:
    from app.db.session import SessionLocal
    from app.models.task_models import EvaluationResult

    db = SessionLocal()
    try:
        return (
            db.query(EvaluationResult)
            .filter(
                EvaluationResult.task_id == task_id,
                EvaluationResult.execution_number == execution_number,
            )
            .count()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 场景0（基线）：10 行全量跑通，逐行落库 + 聚合含全部行
# ---------------------------------------------------------------------------

class TestFullRunRowLevelPersistence:
    def test_full_run_persists_all_rows_and_aggregates(self, db_session, judge_task_10_rows):
        task_id = judge_task_10_rows
        ret, hooks = _run_in_process(task_id)

        assert ret["status"] == "success", ret
        assert sorted(hooks["model_calls"]) == list(range(10))

        # 行级表：10 行、键为 (task, exec=1, data_index 0..9)、状态 completed
        rows = fetch_result_rows(db_session, task_id)
        assert [r.data_index for r in rows] == list(range(10))
        assert all(r.execution_number == 1 for r in rows)
        assert all(r.status == "completed" for r in rows)

        # 行内容：固定打分表口径
        for row in rows:
            assert row.row_result["success"] is True
            assert row.row_result["average_score"] == expected_score(row.data_index)
            assert row.judge_results[0]["feedback"] == expected_feedback(row.data_index)
            assert row.aggregate["average_score"] == expected_score(row.data_index)
            assert row.aggregate["dimension_scores"] == expected_dimensions(row.data_index)

        # 任务终态与 result JSON（与旧行为同构：前端 Results.vue 依赖的键齐全）
        task = _get_task(db_session, task_id)
        assert task.status == "completed"
        result = task.result
        for key in (
            "total", "success_count", "failed_count", "success_rate",
            "average_score", "max_score", "dimension_scores", "items",
            "failed_items", "completed_at",
        ):
            assert key in result, f"result JSON 缺少旧口径键 {key}"
        assert result["total"] == 10
        assert result["success_count"] == 10
        assert result["failed_count"] == 0
        assert len(result["items"]) == 10
        # 聚合平均分 = 固定打分表均值
        expected_avg = round(sum(expected_score(i) for i in range(10)) / 10, 2)
        assert result["average_score"] == expected_avg
        # 每行 item 均可回放行内容（按位置序断言：V2 数据集 payload 无 idx 键，
        # result["idx"] 恒为 0 是 W3 之前就存在的旧行为——657fc49 与当前一致，
        # 「result JSON 同构」要求保持该怪癖，行身份以行级表 data_index 为准）
        for pos, item in enumerate(result["items"]):
            assert item["success"] is True
            assert item["average_score"] == expected_score(pos)
            assert f"row-{pos:02d}" in item["model_output"]

    def test_progress_reaches_100(self, db_session, judge_task_10_rows):
        task_id = judge_task_10_rows
        _run_in_process(task_id)
        task = _get_task(db_session, task_id)
        assert task.progress == 100
        assert task.completed_questions == task.total_questions == 10


# ---------------------------------------------------------------------------
# 单测项（计划测试设计）：upsert 幂等（同键两次写不重复）
# ---------------------------------------------------------------------------

class TestUpsertIdempotency:
    def test_same_key_twice_no_duplicate(self, db_session, judge_task_10_rows):
        """同 (task_id, execution_number, data_index) 两次写 → 1 行，内容为最新一次"""
        from app.services.evaluation_result_service import upsert_row_result

        task_id = judge_task_10_rows
        first = {"idx": 0, "success": False, "error": "第一次：失败行"}
        second = {
            "idx": 0,
            "success": True,
            "model_output": "第二次：成功行",
            "judge_scores": [{"score": expected_score(0), "feedback": expected_feedback(0)}],
            "average_score": expected_score(0),
            "feedback": expected_feedback(0),
            "dimension_scores": expected_dimensions(0),
        }
        upsert_row_result(db_session, task_id=task_id,
                          execution_number=1, data_index=0, result=first)
        upsert_row_result(db_session, task_id=task_id,
                          execution_number=1, data_index=0, result=second)

        rows = fetch_result_rows(db_session, task_id)
        assert len(rows) == 1, "同键两次写不得产生重复行"
        row = rows[0]
        assert row.status == "completed"  # 第二次写覆盖第一次的 failed
        assert row.error is None
        assert row.model_output == "第二次：成功行"
        assert row.aggregate["average_score"] == expected_score(0)

    def test_persist_row_result_twice_still_one_row(self, db_session, judge_task_10_rows):
        """执行器入口 persist_row_result 重复投递同键 → 仍 1 行（幂等）"""
        from app.services.evaluation_result_service import persist_row_result

        task_id = judge_task_10_rows
        result = {"idx": 1, "success": True, "average_score": 6}
        assert persist_row_result(task_id=task_id, execution_number=1, data_index=1, result=result) is True
        assert persist_row_result(task_id=task_id, execution_number=1, data_index=1, result=result) is True
        rows = fetch_result_rows(db_session, task_id)
        assert len(rows) == 1
        assert rows[0].data_index == 1

    def test_different_keys_separate_rows(self, db_session, judge_task_10_rows):
        """不同 data_index / execution_number 各自成行，互不覆盖"""
        from app.services.evaluation_result_service import upsert_row_result

        task_id = judge_task_10_rows
        for idx in (2, 3):
            upsert_row_result(db_session, task_id=task_id, execution_number=1,
                              data_index=idx, result={"idx": idx, "success": True})
        upsert_row_result(db_session, task_id=task_id, execution_number=2,
                          data_index=2, result={"idx": 2, "success": True})
        db_session.expire_all()

        rows = fetch_result_rows(db_session, task_id, execution_number=1)
        assert [r.data_index for r in rows] == [2, 3]
        rows_exec2 = fetch_result_rows(db_session, task_id, execution_number=2)
        assert [r.data_index for r in rows_exec2] == [2]

    def test_unique_constraint_rejects_plain_duplicate_insert(self, db_session, judge_task_10_rows):
        """绕过 upsert 的裸 INSERT 同键重复 → 唯一约束拒绝（DDL 兜底）"""
        from sqlalchemy.exc import IntegrityError

        from app.models.task_models import EvaluationResult
        from app.services.evaluation_result_service import upsert_row_result

        task_id = judge_task_10_rows
        upsert_row_result(db_session, task_id=task_id, execution_number=1,
                          data_index=4, result={"idx": 4, "success": True})
        db_session.expire_all()

        dup = EvaluationResult(
            task_id=task_id, execution_number=1, data_index=4, status="completed",
        )
        db_session.add(dup)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


# ---------------------------------------------------------------------------
# 场景1：stop 中途 → partial_result 为本轮已完成行
# ---------------------------------------------------------------------------

class TestStopMidRun:
    def test_stop_partial_result_is_current_run_completed_rows(self, db_session, judge_task_10_rows):
        task_id = judge_task_10_rows
        stop_event = threading.Event()
        # 第 6 行裁判打分完成后置位停止：行 0-5 完整落库，行 6-9 不再执行。
        # row_delay_ms 让 mock 行耗时接近真实 LLM 调用，保证主线程的
        # as_completed 落库循环跟得上 worker（见 w3_mocks 模块注释）
        ret, hooks = _run_in_process(
            task_id, stop_event=stop_event, stop_after_rows=6, row_delay_ms=80
        )

        assert ret["status"] == "stopped", ret
        assert sorted(hooks["model_calls"]) == [0, 1, 2, 3, 4, 5]

        # 任务状态 stopped；行级表恰为本轮已完成 6 行
        task = _get_task(db_session, task_id)
        assert task.status == "stopped"
        rows = fetch_result_rows(db_session, task_id)
        assert [r.data_index for r in rows] == [0, 1, 2, 3, 4, 5]
        assert all(r.status == "completed" for r in rows)

        # partial_result（执行记录）= 本轮已完成行聚合（旧语义是上一轮 task.result，
        # 执行期为 None——停止即全丢；这是 P0-E 的核心修正点）
        record = _get_exec_record(db_session, task_id, 1)
        assert record.status == "stopped"
        partial = record.result
        assert partial is not None, "stop 后 partial_result 不得为 None（本轮已有 6 行完成）"
        assert partial["total"] == 6
        assert partial["success_count"] == 6
        assert len(partial["items"]) == 6
        # 按位置序断言（result["idx"] 恒 0 为旧行为怪癖，见全量基线测试注释）
        for pos, item in enumerate(partial["items"]):
            assert item["average_score"] == expected_score(pos)
            assert f"row-{pos:02d}" in item["model_output"]

    def test_stop_before_any_row_partial_result_is_none(self, db_session, judge_task_10_rows):
        """本轮无任何已落库行时 partial_result=None（诚实语义，不回填上一轮结果）"""
        task_id = judge_task_10_rows
        stop_event = threading.Event()
        stop_event.set()  # 执行前即停止
        ret, _ = _run_in_process(task_id, stop_event=stop_event)

        assert ret["status"] == "stopped"
        task = _get_task(db_session, task_id)
        assert task.status == "stopped"
        record = _get_exec_record(db_session, task_id, 1)
        assert record.result is None
        assert fetch_result_rows(db_session, task_id) == []


# ---------------------------------------------------------------------------
# 场景2：软超时注入 → 任务标 failed 且已完成行保留
# ---------------------------------------------------------------------------

class TestSoftTimeLimit:
    def test_soft_limit_marks_failed_and_keeps_rows(self, db_session, judge_task_10_rows):
        task_id = judge_task_10_rows
        # 主线程进度更新点注入（第 5 次进度更新=行 4 评完）：
        # 行 0-3 已落库，行 4 因软超时未落库，任务标 failed
        ret, hooks = _run_in_process(task_id, soft_limit_on_progress=5)

        assert ret["status"] == "failed", ret
        assert ret["error"] == "soft time limit"
        assert hooks.get("soft_limit_raised") is True

        # 任务标 failed，result={"error": "soft time limit"}
        task = _get_task(db_session, task_id)
        assert task.status == "failed"
        assert task.result == {"error": "soft time limit"}

        # 已完成行保留（行 0-3）
        rows = fetch_result_rows(db_session, task_id)
        assert [r.data_index for r in rows] == [0, 1, 2, 3]
        assert all(r.status == "completed" for r in rows)

        # 收尾日志可见（软超时收尾路径有迹可查）
        from app.models.task_models import TaskExecutionLog

        logs = (
            db_session.query(TaskExecutionLog)
            .filter(TaskExecutionLog.task_id == task_id)
            .all()
        )
        assert any("软超时" in (log.message or "") for log in logs)

    def test_soft_limit_failed_task_restartable(self, db_session, judge_task_10_rows):
        """软超时 failed 后可再次 start_task 启动新一轮（execution_number=2 全量重跑）"""
        task_id = judge_task_10_rows
        ret_fail, _ = _run_in_process(task_id, soft_limit_on_progress=5)
        assert ret_fail["status"] == "failed"

        from app.services.evaluation_task_service import EvaluationTaskService

        EvaluationTaskService(db_session).start_task(task_id)
        db_session.expire_all()

        ret2, hooks2 = _run_in_process(task_id)
        assert ret2["status"] == "success", ret2

        # 软超时保留的行属 execution_number=1；第 2 次执行为全新一轮（10 行重跑）
        rows_exec1 = fetch_result_rows(db_session, task_id, execution_number=1)
        rows_exec2 = fetch_result_rows(db_session, task_id, execution_number=2)
        assert [r.data_index for r in rows_exec1] == [0, 1, 2, 3]
        assert [r.data_index for r in rows_exec2] == list(range(10))
        assert sorted(hooks2["model_calls"]) == list(range(10))

        task = _get_task(db_session, task_id)
        assert task.status == "completed"
        assert task.result["total"] == 10


# ---------------------------------------------------------------------------
# 场景3：SIGKILL 子进程 → 已完成行可查 + 同 execution_number 续跑只跑缺失行
# ---------------------------------------------------------------------------

class TestSigkillAndResume:
    def test_sigkill_rows_persisted_and_resume_completes_all(self, db_session, judge_task_10_rows, tmp_path):
        task_id = judge_task_10_rows
        call_log = tmp_path / "child_calls.log"
        child_stdout = tmp_path / "child_stdout.log"

        env = dict(os.environ)
        env["DATABASE_URL"] = TEMP_DSN
        env["W3_TASK_ID"] = task_id
        env["W3_CALL_LOG"] = str(call_log)
        env["W3_ROW_DELAY_MS"] = "250"  # 10 行约 2.5s+，留足 kill 时间窗

        with open(child_stdout, "w") as out_fh:
            child = subprocess.Popen(
                [sys.executable, str(CHILD_RUNNER)],
                cwd=str(WORKTREE_ROOT),
                env=env,
                stdout=out_fh,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                # 轮询行级表：>=4 行落库后 SIGKILL（硬杀，无任何收尾机会）
                deadline = time.time() + 30
                while time.time() < deadline:
                    if _count_rows(task_id) >= 4:
                        break
                    if child.poll() is not None:
                        break  # 子进程提前退出（异常），交由后面断言暴露
                    time.sleep(0.02)
                child.send_signal(signal.SIGKILL)
                child.wait(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)

        assert child.returncode == -signal.SIGKILL, "子进程应被 SIGKILL 杀死"

        rows = fetch_result_rows(db_session, task_id)
        persisted = [r.data_index for r in rows]
        # 已完成行可查（P0-E：中途被杀零结果丢失）；杀在半途，未跑完全部
        assert len(persisted) >= 4, f"SIGKILL 前应有 >=4 行落库，实际 {persisted}"
        assert len(persisted) < 10, "kill 应发生在全部行完成之前（调大 W3_ROW_DELAY_MS 或提前触发）"
        assert all(r.status == "completed" for r in rows)
        # 子进程调用日志与行级表一致（write-through 逐行一致）
        child_calls = read_call_log(str(call_log))
        assert set(child_calls) >= set(persisted)

        # SIGKILL 无收尾路径：任务状态仍为 running（W3 范围内无收尸标记；
        # 计划测试设计中「任务状态 failed」由 celery on_failure/消息重投或
        # W10 reaper 兜底，见验收报告差异记录）
        task = _get_task(db_session, task_id)
        assert task.status == "running"

        # ---- 续跑：同 execution_number（模拟 celery 消息重投/人工重跑）----
        # 注意：不再调用 start_task（execution_number 保持 1，行级表键延续）
        ret, hooks = _run_in_process(task_id)
        assert ret["status"] == "success", ret

        # 续跑只跑缺失行：本轮模型调用与已落库行零交集（不重复计费）
        missing_rows = sorted(set(range(10)) - set(persisted))
        assert sorted(hooks["model_calls"]) == missing_rows, (
            f"续跑应只执行缺失行 {missing_rows}，实际执行了 {sorted(hooks['model_calls'])}"
        )

        # 聚合 result 含全部 10 行（前次已完成行从行级表合并）
        db_session.expire_all()  # 续跑经独立会话提交，清掉本会话缓存的任务对象
        task = _get_task(db_session, task_id)
        assert task.status == "completed"
        result = task.result
        assert result["total"] == 10
        assert result["success_count"] == 10
        assert len(result["items"]) == 10
        # 按位置序断言（result["idx"] 恒 0 为旧行为怪癖，见全量基线测试注释）
        for pos, item in enumerate(result["items"]):
            assert item["average_score"] == expected_score(pos)
            assert f"row-{pos:02d}" in item["model_output"]

        # 行级表：仍 10 行（幂等无重复），全部 completed
        rows_after = fetch_result_rows(db_session, task_id)
        assert [r.data_index for r in rows_after] == list(range(10))
        assert all(r.status == "completed" for r in rows_after)
        assert all(r.execution_number == 1 for r in rows_after)


# ---------------------------------------------------------------------------
# W3-5：delete_execution_record 联动删行
# ---------------------------------------------------------------------------

class TestDeleteExecutionRecord:
    def test_delete_execution_record_cascades_row_results(self, db_session, judge_task_10_rows):
        task_id = judge_task_10_rows
        stop_event = threading.Event()
        _run_in_process(
            task_id, stop_event=stop_event, stop_after_rows=3, row_delay_ms=80
        )  # 3 行落库后停止

        from app.services.evaluation_task_service import TaskExecutionRecordService

        assert len(fetch_result_rows(db_session, task_id)) == 3
        service = TaskExecutionRecordService(db_session)
        assert service.delete_execution_record(task_id, 1) is True
        db_session.expire_all()

        assert fetch_result_rows(db_session, task_id) == []
        from app.models.task_models import TaskExecutionRecord

        assert (
            db_session.query(TaskExecutionRecord)
            .filter(
                TaskExecutionRecord.task_id == task_id,
                TaskExecutionRecord.execution_number == 1,
            )
            .count()
            == 0
        )
