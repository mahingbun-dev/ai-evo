"""
批量操作API

W9：批量端点逐任务做对象级归属校验（ensure_task_access）——跨团队任务
不整单 403，而是逐项记入 details（status=failed），其余合法任务继续执行。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel, Field
from datetime import datetime

from app.db.session import get_db
from app.services.access_control import ensure_task_access
from app.services.evaluation_task_service import EvaluationTaskService
from app.services.result_aggregator import ResultExporter


# ==================== Request Models ====================

class BatchRunRequest(BaseModel):
    task_ids: List[str] = Field(..., description="任务ID列表")


class BatchDeleteRequest(BaseModel):
    task_ids: List[str] = Field(..., description="任务ID列表")


class BatchStopRequest(BaseModel):
    task_ids: List[str] = Field(..., description="任务ID列表")


class BatchExportRequest(BaseModel):
    task_ids: List[str] = Field(..., description="任务ID列表")
    format: str = Field("excel", description="导出格式：excel/json/csv")


# ==================== Response Models ====================

class BatchOperationResponse(BaseModel):
    total: int
    success: int
    failed: int
    details: List[dict]


# ==================== Router ====================

router = APIRouter(prefix="/evaluation/tasks", tags=["批量操作"])


@router.post("/batch-run", response_model=BatchOperationResponse, summary="批量执行任务")
def batch_run_tasks(
    body: BatchRunRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """批量执行多个评测任务（W9：逐任务归属校验，跨团队任务逐项失败）"""
    service = EvaluationTaskService(db)

    results = []
    success_count = 0
    failed_count = 0

    for task_id in body.task_ids:
        try:
            try:
                task = ensure_task_access(db, request.state.user, task_id)
            except HTTPException as e:
                # 404/403 均按逐项失败处理（不中断同批其余任务）
                results.append({"task_id": task_id, "status": "failed", "error": e.detail})
                failed_count += 1
                continue

            if task.status == "running":
                results.append({"task_id": task_id, "status": "skipped", "error": "任务正在执行中"})
                failed_count += 1
                continue

            from app.services.task_scheduler import get_task_scheduler
            scheduler = get_task_scheduler()
            # 先设置状态为 running（原子操作，防止并发请求重复提交）
            started = service.start_task(task_id)
            if started is None:
                # 并发下任务可能已被其他请求启动，视为跳过
                results.append({"task_id": task_id, "status": "skipped", "error": "任务正在执行中"})
                failed_count += 1
                continue
            submitted = scheduler.submit_task(task_id)
            if submitted:
                results.append({"task_id": task_id, "status": "success"})
                success_count += 1
            else:
                # 提交失败，回滚状态为 failed
                service.fail_task(task_id, "任务提交到 Celery Worker 失败")
                results.append({"task_id": task_id, "status": "failed", "error": "任务提交失败"})
                failed_count += 1

        except Exception as e:
            results.append({"task_id": task_id, "status": "failed", "error": str(e)})
            failed_count += 1

    return {
        "total": len(body.task_ids),
        "success": success_count,
        "failed": failed_count,
        "details": results,
    }


@router.post("/batch-delete", response_model=BatchOperationResponse, summary="批量删除任务")
def batch_delete_tasks(
    body: BatchDeleteRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """批量删除多个评测任务（W9：逐任务归属校验，跨团队任务逐项失败）"""
    service = EvaluationTaskService(db)

    results = []
    success_count = 0
    failed_count = 0

    for task_id in body.task_ids:
        try:
            try:
                ensure_task_access(db, request.state.user, task_id)
            except HTTPException as e:
                results.append({"task_id": task_id, "status": "failed", "error": e.detail})
                failed_count += 1
                continue

            success = service.delete_task(task_id)
            if success:
                results.append({"task_id": task_id, "status": "success"})
                success_count += 1
            else:
                results.append({"task_id": task_id, "status": "failed", "error": "任务不存在"})
                failed_count += 1

        except Exception as e:
            results.append({"task_id": task_id, "status": "failed", "error": str(e)})
            failed_count += 1

    return {
        "total": len(body.task_ids),
        "success": success_count,
        "failed": failed_count,
        "details": results,
    }


@router.post("/batch-stop", response_model=BatchOperationResponse, summary="批量停止任务")
def batch_stop_tasks(
    body: BatchStopRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """批量停止多个正在执行的评测任务（W9：逐任务归属校验，跨团队任务逐项失败）"""
    service = EvaluationTaskService(db)

    results = []
    success_count = 0
    failed_count = 0

    for task_id in body.task_ids:
        try:
            try:
                task = ensure_task_access(db, request.state.user, task_id)
            except HTTPException as e:
                results.append({"task_id": task_id, "status": "failed", "error": e.detail})
                failed_count += 1
                continue

            if task.status not in ["running", "pending"]:
                results.append({"task_id": task_id, "status": "skipped", "error": f"任务状态为{task.status}，无法停止"})
                failed_count += 1
                continue

            from app.services.task_scheduler import get_task_scheduler
            get_task_scheduler().stop_task(task_id)
            service.stop_task(task_id)
            results.append({"task_id": task_id, "status": "success"})
            success_count += 1

        except Exception as e:
            results.append({"task_id": task_id, "status": "failed", "error": str(e)})
            failed_count += 1

    return {
        "total": len(body.task_ids),
        "success": success_count,
        "failed": failed_count,
        "details": results,
    }


# ==================== Export Router ====================

export_router = APIRouter(prefix="/evaluation/results", tags=["结果导出"])


@export_router.post("/batch-export", summary="批量导出结果")
def batch_export_results(
    body: BatchExportRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """批量导出多个任务的评测结果（W9：逐任务归属校验，跨团队任务逐项失败）"""
    task_service = EvaluationTaskService(db)

    results = []
    success_count = 0
    failed_count = 0

    for task_id in body.task_ids:
        try:
            try:
                task = ensure_task_access(db, request.state.user, task_id)
            except HTTPException as e:
                results.append({"task_id": task_id, "status": "failed", "error": e.detail})
                failed_count += 1
                continue

            if task.status != "completed":
                results.append({"task_id": task_id, "status": "failed", "error": f"任务未完成，当前状态: {task.status}"})
                failed_count += 1
                continue

            results.append({
                "task_id": task_id,
                "task_name": task.name,
                "status": "success",
                "evaluation_type": task.evaluation_type,
                "result": task.result,
            })
            success_count += 1

        except Exception as e:
            results.append({"task_id": task_id, "status": "failed", "error": str(e)})
            failed_count += 1

    # 导出
    if body.format == "excel":
        try:
            # 构建导出数据：合并 result 和 evaluation_type
            export_data = []
            actual_eval_type = "batch"  # 默认
            for r in results:
                if r.get("status") == "success":
                    item = r.get("result", {})
                    item["evaluation_type"] = r.get("evaluation_type", "")
                    export_data.append(item)
                    actual_eval_type = r.get("evaluation_type", "batch")

            # 使用实际评测类型导出
            data = ResultExporter.export_to_excel(export_data, actual_eval_type)
            return Response(
                content=data,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename=evaluation_results_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"}
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"导出失败: {str(e)}")

    elif body.format == "json":
        return {
            "total": len(body.task_ids),
            "success": success_count,
            "failed": failed_count,
            "results": results,
        }

    elif body.format == "csv":
        try:
            # 构建导出数据：合并 result 和 evaluation_type
            export_data = []
            actual_eval_type = "batch"  # 默认
            for r in results:
                if r.get("status") == "success":
                    item = r.get("result", {})
                    item["evaluation_type"] = r.get("evaluation_type", "")
                    export_data.append(item)
                    actual_eval_type = r.get("evaluation_type", "batch")

            data = ResultExporter.export_to_csv(export_data, actual_eval_type)
            return Response(
                content=data.encode('utf-8') if isinstance(data, str) else data,
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=evaluation_results_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"}
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"CSV导出失败: {str(e)}")

    else:
        raise HTTPException(status_code=400, detail=f"不支持的格式: {body.format}")
