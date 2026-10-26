"""评测行级结果存储服务（W3，P0-E）

背景：原实现逐行结果全内存、仅任务末尾 complete_task 一次写入——
停止/worker 强杀/软超时窗口内全部结果丢失（P0-E）。本服务提供行级
write-through 的存储面：

- persist_row_result：每行终态（成功/失败/跳过产物）即时幂等 upsert，
  按 UNIQUE(task_id, execution_number, data_index) 幂等，重复投递/
  续跑重写同一行不产生重复数据；
- get_completed_data_indexes：断点续跑查询已完成行（跳过集）；
- load_execution_results：从行级表重建本轮全量行结果列表
  （complete_task 聚合优先用行级表，含续跑前次已完成行）；
- delete_execution_rows：行级表随 TaskExecutionRecord 生命周期联动删除。

落库失败不阻断评测主流程（只告警）：迁移未执行/DB 抖动时评测仍按
旧口径完成，只是中途被杀仍会丢——expand-only 迁移执行后本表即生效。
软超时异常（SoftTimeLimitExceeded）不吞：由上层收尾逻辑处理。
"""
import logging
import threading
from typing import Any, Dict, List, Set

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import SessionLocal
from app.models.task_models import EvaluationResult

logger = logging.getLogger(__name__)

# 断点续跑跳过的行状态：已完成行与裁判全失败行（judge_failed，W2 口径合入后
# 由执行器打标）不重跑，缺失/failed/pending 行重跑
RESUME_SKIP_STATUSES = ("completed", "judge_failed")

# row_result / input_data 投影排除的输出侧结构化键（其余行字段视为输入侧平铺字段）
_ROW_OUTPUT_KEYS = frozenset({
    "idx", "success", "error", "judge_scores", "average_score", "feedback",
    "dimension_scores", "model_output", "model_raw_response", "input_data",
    "pre_script_results", "row_status",
})

# 行级表不可用（迁移未执行/DB 异常）时每进程只告警一次，避免大任务日志刷屏
_persist_failure_lock = threading.Lock()
_persist_failure_noted = False


def _note_persist_failure(reason: str) -> None:
    """行级落库/读取失败告警（每进程一次）；迁移执行后首次成功即重置"""
    global _persist_failure_noted
    with _persist_failure_lock:
        if _persist_failure_noted:
            return
        _persist_failure_noted = True
    logger.warning(f"[RowPersist] 行级结果表不可用或写入失败（后续不再重复告警）: {reason}")


def _reset_persist_failure_note() -> None:
    global _persist_failure_noted
    with _persist_failure_lock:
        _persist_failure_noted = False


def derive_row_status(result: Dict[str, Any]) -> str:
    """行状态推导：completed/judge_failed/failed

    - row_status=="judge_failed" 透传（W2 评分口径合入后由执行器打标，
      本分支先行识别，保证 W3 续跑跳过语义与 W2 对齐）；
    - success=True → completed（含空输入行：W4 起其 average_score=None
      不进任务均分，但行本身已执行完成，续跑重跑结果相同）；
    - 其余 → failed（含模型调用失败、on_fail=abort、停止产物行：续跑重跑）。
      W12-3 的 pre_script_failed 行（脚本输出缺失，success=False）落在此分支：
      落库为 failed 可断点续跑重跑，脚本修复后重跑即可恢复。
    """
    if isinstance(result, dict):
        if result.get("row_status") == "judge_failed":
            return "judge_failed"
        if result.get("success"):
            return "completed"
    return "failed"


def _input_data_projection(result: Dict[str, Any]) -> Dict[str, Any]:
    """input_data 列：行输入侧平铺字段（输出/裁判/结构化键之外的字段）

    仅为查询投影，各评测类型的输出字段可能混入；聚合/续跑重建的事实源
    是 row_result 列，不依赖本投影。
    """
    return {
        key: value
        for key, value in result.items()
        if key not in _ROW_OUTPUT_KEYS and not str(key).startswith("_")
    }


def _aggregate_projection(result: Dict[str, Any]) -> Dict[str, Any]:
    """aggregate 列：行聚合分（W2 新口径扩展点：judge_count/failed_judge_count）"""
    return {
        key: result[key]
        for key in ("average_score", "feedback", "dimension_scores", "success")
        if key in result
    }


def upsert_row_result(
    db: Session,
    *,
    task_id: str,
    execution_number: int,
    data_index: int,
    result: Dict[str, Any],
) -> None:
    """按 UNIQUE(task_id, execution_number, data_index) 幂等 upsert 一行结果

    冲突时更新除业务键与 created_at 外的全部列（同键重写不产生重复行）。
    异常向上抛出（含 SoftTimeLimitExceeded），由调用方决定吞/放。
    """
    row_values = {
        "task_id": task_id,
        "execution_number": execution_number,
        "data_index": data_index,
        "status": derive_row_status(result),
        "input_data": _input_data_projection(result),
        "model_output": result.get("model_output"),
        "model_raw_response": result.get("model_raw_response"),
        "judge_results": result.get("judge_scores"),
        "aggregate": _aggregate_projection(result),
        "row_result": result,
        "error": result.get("error"),
    }
    stmt = pg_insert(EvaluationResult).values(**row_values)
    update_cols = {
        column: stmt.excluded[column]
        for column in row_values
        if column not in ("task_id", "execution_number", "data_index")
    }
    update_cols["updated_at"] = func.now()
    stmt = stmt.on_conflict_do_update(
        index_elements=["task_id", "execution_number", "data_index"],
        set_=update_cols,
    )
    db.execute(stmt)
    db.commit()


def persist_row_result(
    *,
    task_id: str,
    execution_number: int,
    data_index: int,
    result: Dict[str, Any],
) -> bool:
    """行级 write-through 落库入口（评测执行链路调用）

    - 注入行身份 result["_orig_idx"]（下划线内部键，聚合平铺/展示均排除），
      供 complete 合并时对齐行级表与内存结果；
    - 自管短会话（评测执行线程外无长事务），逐行一 commit（写放大预算
      见计划 W3 测试设计：超 15% 再改批量 flush）；
    - 落库失败不阻断评测（只告警一次），软超时异常上抛由收尾处理。

    Returns: 是否落库成功
    """
    if not isinstance(result, dict):
        return False
    # 行身份先行：即便落库失败，内存合并仍可对齐
    result["_orig_idx"] = data_index
    try:
        db = SessionLocal()
        try:
            upsert_row_result(
                db,
                task_id=task_id,
                execution_number=execution_number,
                data_index=data_index,
                result=result,
            )
        finally:
            db.close()
        _reset_persist_failure_note()
        return True
    except SoftTimeLimitExceeded:
        raise
    except Exception as e:
        _note_persist_failure(f"task={task_id} exec={execution_number} idx={data_index}: {e}")
        return False


def get_completed_data_indexes(
    db: Session,
    task_id: str,
    execution_number: int,
) -> Set[int]:
    """断点续跑：查询本轮已完成（含 judge_failed）行的 data_index 集合

    表缺失/查询异常视为无已完成行（返回空集），评测按全量执行——
    续跑是优化，不能因行级表不可用而丢行。
    """
    try:
        rows = (
            db.query(EvaluationResult.data_index)
            .filter(
                EvaluationResult.task_id == task_id,
                EvaluationResult.execution_number == execution_number,
                EvaluationResult.status.in_(RESUME_SKIP_STATUSES),
            )
            .all()
        )
        return {row[0] for row in rows}
    except Exception as e:
        _note_persist_failure(f"查询已完成行失败 task={task_id} exec={execution_number}: {e}")
        return set()


def load_execution_results(
    db: Session,
    task_id: str,
    execution_number: int,
) -> List[Dict[str, Any]]:
    """从行级表重建本轮全量行结果（按 data_index 升序）

    - row_result 为事实源；每行注入行身份 "_orig_idx"（= data_index）；
    - 表缺失/无行返回空列表（调用方回退内存结果聚合）。
    """
    try:
        rows = (
            db.query(EvaluationResult)
            .filter(
                EvaluationResult.task_id == task_id,
                EvaluationResult.execution_number == execution_number,
            )
            .order_by(EvaluationResult.data_index.asc())
            .all()
        )
    except Exception as e:
        _note_persist_failure(f"读取行级结果失败 task={task_id} exec={execution_number}: {e}")
        return []

    results: List[Dict[str, Any]] = []
    for row in rows:
        row_result = row.row_result
        if not isinstance(row_result, dict):
            # 兜底：row_result 异常缺失时用投影列拼最小行结果
            row_result = {
                "idx": row.data_index,
                "success": row.status == "completed",
                "model_output": row.model_output,
                "model_raw_response": row.model_raw_response,
                "judge_scores": row.judge_results,
                "error": row.error,
            }
            aggregate = row.aggregate or {}
            if isinstance(aggregate, dict):
                row_result.update({
                    key: aggregate[key]
                    for key in ("average_score", "feedback", "dimension_scores")
                    if key in aggregate
                })
        row_result["_orig_idx"] = row.data_index
        results.append(row_result)
    return results


def delete_execution_rows(
    db: Session,
    task_id: str,
    execution_number: int,
) -> int:
    """删除某次执行的全部行级结果（随 TaskExecutionRecord 生命周期联动，W3-5）"""
    deleted = (
        db.query(EvaluationResult)
        .filter(
            EvaluationResult.task_id == task_id,
            EvaluationResult.execution_number == execution_number,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted
