"""evaluation_result_service 单元测试（W3 行级结果落库，P0-E）

覆盖计划 §2 W3「测试设计」的单测项：
- status 状态集合校验：derive_row_status 的推导规则、
  续跑跳过集合 RESUME_SKIP_STATUSES 与计划 DDL status 集合的关系；
- persist_row_result 落库失败不阻断评测（返回 False）、
  软超时异常（SoftTimeLimitExceeded）不得被吞（上抛）、
  行身份 _orig_idx 先行注入；
- input_data / aggregate 投影列的键裁剪规则。

注：upsert 幂等（同键两次写不重复）依赖 postgresql 方言的
ON CONFLICT 语句，SQLite 无法编译，放在集成测试
tests/integration/test_row_level_persistence.py（本地 PG 临时库）中验证。
"""

import pytest
from unittest.mock import MagicMock, patch

from celery.exceptions import SoftTimeLimitExceeded

from app.services import evaluation_result_service as svc

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# 计划 §2 W3-1 DDL 中 status 列的合法状态集合
PLAN_STATUS_SET = {"completed", "judge_failed", "failed", "skipped", "pending"}


class TestDeriveRowStatus:
    """status 状态集合校验（计划：status: completed/judge_failed/failed/skipped/pending）"""

    @pytest.mark.parametrize(
        "result,expected",
        [
            # 成功行 → completed
            ({"idx": 0, "success": True, "average_score": 8.0}, "completed"),
            # 空值校验 0 分行也是已完成评测（success=True）→ completed
            ({"idx": 1, "success": True, "average_score": 0}, "completed"),
            # 模型调用失败 / on_fail=abort / 停止产物行 → failed
            ({"idx": 2, "success": False, "error": "模型调用失败"}, "failed"),
            ({}, "failed"),
            # 非字典（异常兜底产物）→ failed
            (None, "failed"),
            ("not-a-dict", "failed"),
            # W2 前向兼容：裁判全失败行 → judge_failed 透传
            ({"idx": 3, "row_status": "judge_failed", "success": False}, "judge_failed"),
        ],
        ids=[
            "success-true-completed",
            "zero-score-success-completed",
            "failure-failed",
            "empty-dict-failed",
            "none-failed",
            "non-dict-failed",
            "judge-failed-passthrough",
        ],
    )
    def test_derive_row_status_rules(self, result, expected):
        assert svc.derive_row_status(result) == expected

    def test_judge_failed_takes_priority_over_success(self):
        """row_status=judge_failed 优先于 success=True（W2 口径行不再按 completed 跳过）"""
        assert svc.derive_row_status({"row_status": "judge_failed", "success": True}) == "judge_failed"

    def test_derived_statuses_within_plan_set(self):
        """derive_row_status 产出的状态必须落在计划 DDL 的合法集合内"""
        samples = [
            {"success": True},
            {"success": False},
            {},
            None,
            {"row_status": "judge_failed"},
            {"row_status": "skipped"},   # 未识别的 row_status 不透传
            {"row_status": "pending"},
        ]
        for sample in samples:
            assert svc.derive_row_status(sample) in PLAN_STATUS_SET


class TestResumeSkipStatuses:
    """断点续跑跳过集合（W3-3：completed/judge_failed 跳过，failed/pending 重跑）"""

    def test_skip_statuses_exact(self):
        assert set(svc.RESUME_SKIP_STATUSES) == {"completed", "judge_failed"}

    def test_failed_and_pending_not_in_skip_set(self):
        assert "failed" not in svc.RESUME_SKIP_STATUSES
        assert "pending" not in svc.RESUME_SKIP_STATUSES
        assert "skipped" not in svc.RESUME_SKIP_STATUSES


class TestPersistRowResultFailureSemantics:
    """persist_row_result：落库失败不阻断评测、软超时不吞、行身份先行注入

    P0-4 后 persist 为有界缓冲版：单行调用只入缓冲（不触发 DB），flush
    触发时（本组用例显式 flush=True）才走到 SessionLocal / upsert。
    """

    def test_persist_failure_returns_false_not_raise(self):
        """DB 异常（如迁移未执行）→ 返回 False，不向评测主流程抛异常"""
        result = {"idx": 0, "success": True, "model_output": "x"}
        with patch.object(svc, "SessionLocal", side_effect=RuntimeError("relation evaluation_results does not exist")):
            ok = svc.persist_row_result(task_id="t1", execution_number=1, data_index=0, result=result, flush=True)
        assert ok is False

    def test_persist_failure_still_injects_row_identity(self):
        """落库失败时内存合并仍可对齐：_orig_idx 先行注入"""
        result = {"idx": 7, "success": True}
        with patch.object(svc, "SessionLocal", side_effect=RuntimeError("db down")):
            svc.persist_row_result(task_id="t1", execution_number=1, data_index=7, result=result, flush=True)
        assert result["_orig_idx"] == 7

    def test_soft_time_limit_not_swallowed(self):
        """软超时异常必须上抛（W3-4：不得按落库失败吞掉）"""
        result = {"idx": 0, "success": True}
        with patch.object(svc, "SessionLocal", side_effect=SoftTimeLimitExceeded()):
            with pytest.raises(SoftTimeLimitExceeded):
                svc.persist_row_result(task_id="t1", execution_number=1, data_index=0, result=result, flush=True)

    def test_non_dict_result_returns_false(self):
        assert svc.persist_row_result(task_id="t1", execution_number=1, data_index=0, result="bad") is False

    def test_persist_success_returns_true_and_injects_identity(self):
        """正常落库：flush 后批量写入生效，返回 True，注入行身份"""
        result = {"idx": 3, "success": True, "average_score": 7.0}
        with patch.object(svc, "SessionLocal") as session_factory:
            ok = svc.persist_row_result(task_id="t1", execution_number=2, data_index=3, result=result, flush=True)
        assert ok is True
        assert result["_orig_idx"] == 3
        db = session_factory.return_value
        # 批量落库：一次会话内 1 次 execute + 单次 commit
        assert db.execute.call_count == 1
        assert db.commit.call_count == 1
        db.close.assert_called_once()


class TestPersistRowBuffering:
    """P0-4 有界落库缓冲：N=20 行或 500ms 先到批量 upsert + 单次 commit"""

    @pytest.fixture(autouse=True)
    def _drain_buffer(self):
        """每用例前后清空进程级缓冲，隔离串账"""
        with svc._pending_lock:
            svc._pending_rows.clear()
            svc._pending_since.clear()
        yield
        with svc._pending_lock:
            svc._pending_rows.clear()
            svc._pending_since.clear()

    def _row(self, idx):
        return {"idx": idx, "success": True, "average_score": 8.0}

    def test_within_batch_no_db_touch(self):
        """不足 20 行且在窗口内：仅入缓冲，不触发任何 DB 访问"""
        with patch.object(svc, "SessionLocal") as session_factory:
            ok = svc.persist_row_result(task_id="bt", execution_number=1, data_index=0, result=self._row(0))
        assert ok is True
        session_factory.assert_not_called()

    def test_batch_size_triggers_single_commit(self):
        """满 20 行自动触发：一次会话 20 次 execute + 单次 commit"""
        with patch.object(svc, "SessionLocal") as session_factory:
            for idx in range(svc._FLUSH_BATCH_ROWS - 1):
                svc.persist_row_result(task_id="bt", execution_number=1, data_index=idx, result=self._row(idx))
            ok = svc.persist_row_result(
                task_id="bt", execution_number=1, data_index=svc._FLUSH_BATCH_ROWS - 1,
                result=self._row(svc._FLUSH_BATCH_ROWS - 1),
            )
        assert ok is True
        db = session_factory.return_value
        assert db.execute.call_count == svc._FLUSH_BATCH_ROWS
        assert db.commit.call_count == 1  # 批量一次 commit（旧实现为 20 次）

    def test_interval_expiry_triggers_flush_on_next_call(self):
        """首行入缓冲超 500ms：下一次 persist 顺带触发冲刷（含缓冲中的全部行）"""
        with patch.object(svc, "SessionLocal") as session_factory, \
             patch.object(svc.time, "monotonic") as mono:
            mono.return_value = 1000.0
            svc.persist_row_result(task_id="bt", execution_number=1, data_index=0, result=self._row(0))
            mono.return_value = 1000.0 + svc._FLUSH_INTERVAL_SECONDS + 0.01
            svc.persist_row_result(task_id="bt", execution_number=1, data_index=1, result=self._row(1))
        db = session_factory.return_value
        # 窗口超时按首行计时：两行同批冲刷，单次 commit
        assert db.execute.call_count == 2
        assert db.commit.call_count == 1
        with svc._pending_lock:
            assert not svc._pending_rows

    def test_flush_param_forces_immediate_write(self):
        """flush=True 强制落库（停止/软超时/收尾路径）"""
        with patch.object(svc, "SessionLocal") as session_factory:
            svc.persist_row_result(task_id="bt", execution_number=1, data_index=0, result=self._row(0))
            assert session_factory.call_count == 0
            svc.persist_row_result(task_id="bt", execution_number=1, data_index=1, result=self._row(1), flush=True)
        db = session_factory.return_value
        assert db.execute.call_count == 2
        assert db.commit.call_count == 1

    def test_flush_pending_rows_writes_and_drains(self):
        """显式 flush_pending_rows：全部缓冲行一次会话写入并清空缓冲"""
        with patch.object(svc, "SessionLocal") as session_factory:
            for idx in range(3):
                svc.persist_row_result(task_id="bt", execution_number=1, data_index=idx, result=self._row(idx))
            svc.flush_pending_rows()
        db = session_factory.return_value
        assert db.execute.call_count == 3
        assert db.commit.call_count == 1
        with svc._pending_lock:
            assert not svc._pending_rows

    def test_flush_pending_rows_scoped_by_task(self):
        """按 task_id 定向冲刷：只清匹配键，其它任务的缓冲保留"""
        with patch.object(svc, "SessionLocal"):
            svc.persist_row_result(task_id="t-a", execution_number=1, data_index=0, result=self._row(0))
            svc.persist_row_result(task_id="t-b", execution_number=1, data_index=0, result=self._row(0))
            svc.flush_pending_rows(task_id="t-a")
        with svc._pending_lock:
            assert ("t-a", 1) not in svc._pending_rows
            assert ("t-b", 1) in svc._pending_rows

    def test_flush_failure_drops_batch_and_returns_false(self):
        """冲刷失败：该批行从行级表缺失（口径同旧逐行失败），不抛异常"""
        with patch.object(svc, "SessionLocal", side_effect=RuntimeError("db down")):
            ok = svc.persist_row_result(task_id="bt", execution_number=1, data_index=0, result=self._row(0), flush=True)
        assert ok is False
        with svc._pending_lock:
            assert not svc._pending_rows  # 失败批次不回灌（避免 DB 故障时缓冲无界增长）


class TestUpsertRowResultStatement:
    """upsert_row_result 的 upsert 语句构造（幂等语义的 PG 实测见集成测试）"""

    def test_upsert_uses_pg_on_conflict_do_update(self):
        """语句必须是 ON CONFLICT (task_id,execution_number,data_index) DO UPDATE 幂等 upsert"""
        db = MagicMock()
        result = {
            "idx": 0,
            "success": True,
            "model_output": "out",
            "model_raw_response": "raw",
            "judge_scores": [{"score": 8}],
            "average_score": 8.0,
            "feedback": "fb",
            "dimension_scores": {"准确性": 8.0},
            "user_input": "问题",
            "error": None,
        }
        svc.upsert_row_result(db, task_id="t1", execution_number=1, data_index=0, result=result)
        stmt = db.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        assert "ON CONFLICT (task_id, execution_number, data_index) DO UPDATE" in compiled
        # 冲突更新集不含业务键列
        conflict = stmt._post_values_clause
        update_cols = set(dict(conflict.update_values_to_set).keys())
        assert "task_id" not in update_cols
        assert "execution_number" not in update_cols
        assert "data_index" not in update_cols


class TestProjections:
    """input_data / aggregate 投影列裁剪"""

    def test_input_data_excludes_output_and_underscore_keys(self):
        result = {
            "idx": 0,
            "user_input": "问题",
            "ref_answer": "答案",
            "model_output": "输出",
            "judge_scores": [],
            "average_score": 8,
            "success": True,
            "_orig_idx": 0,
            "_dataset_item": {"id": "d1"},
        }
        projection = svc._input_data_projection(result)
        assert projection == {"user_input": "问题", "ref_answer": "答案"}

    def test_aggregate_projection_keeps_only_aggregate_keys(self):
        result = {
            "average_score": 8.0,
            "feedback": "fb",
            "dimension_scores": {"准确性": 8.0},
            "success": True,
            "model_output": "should-not-appear",
            "user_input": "should-not-appear",
        }
        projection = svc._aggregate_projection(result)
        assert projection == {
            "average_score": 8.0,
            "feedback": "fb",
            "dimension_scores": {"准确性": 8.0},
            "success": True,
        }


class TestUpsertRowStatusColumn:
    """upsert_row_result 写入行的 status 列取值必须来自 derive_row_status"""

    @pytest.mark.parametrize(
        "result,expected_status",
        [
            ({"success": True}, "completed"),
            ({"success": False, "error": "e"}, "failed"),
            ({"row_status": "judge_failed"}, "judge_failed"),
        ],
    )
    def test_status_column_value(self, result, expected_status):
        db = MagicMock()
        with patch.object(svc, "derive_row_status", wraps=svc.derive_row_status) as spy:
            svc.upsert_row_result(db, task_id="t", execution_number=1, data_index=0, result=result)
        spy.assert_called_once_with(result)
