"""
审计日志 API

QE4-4 审计补全：写端点审计由 audited(action, resource_type) 路由依赖统一落库
（覆盖面由 tests/unit/test_audit_coverage.py 静态断言把关）；本文件提供超管
查询面与导出面——导出（xlsx/csv）挂企业功能位 AUDIT_EXPORT_ENABLED（默认关，
关闭时 404，能力不存在而非权限不足）。
"""
import csv
import io
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from datetime import datetime

from app.core.config import get_settings
from app.db.session import get_db
from app.services.audit_service import get_operation_logs


# ==================== Response Models ====================

class OperationLogResponse(BaseModel):
    """审计日志响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_code: str
    user_name: Optional[str] = None
    team_code: Optional[str] = None
    action: str
    resource_type: str
    resource_id: str
    resource_name: Optional[str] = None
    detail: Optional[dict] = None
    ip_address: Optional[str] = None
    created_at: Optional[datetime] = None

    @field_serializer('created_at')
    def serialize_created_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


class AuditLogListResponse(BaseModel):
    """审计日志列表响应"""
    total: int
    items: List[OperationLogResponse]


# ==================== Router ====================

router = APIRouter(prefix="/audit-logs", tags=["审计日志"])


# ==================== Endpoints ====================

@router.get("", response_model=AuditLogListResponse, summary="获取审计日志列表")
def list_audit_logs(
    request: Request,
    skip: int = Query(0, ge=0, description="跳过的记录数"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    action: Optional[str] = Query(None, description="操作类型：CREATE/UPDATE/DELETE"),
    resource_type: Optional[str] = Query(None, description="资源类型：task/dataset/model/template/media"),
    user_code: Optional[str] = Query(None, description="操作者用户代码"),
    team_code: Optional[str] = Query(None, description="团队代码"),
    start_time: Optional[datetime] = Query(None, description="开始时间"),
    end_time: Optional[datetime] = Query(None, description="结束时间"),
    db: Session = Depends(get_db),
):
    """分页查询审计日志（仅超管可访问）"""
    user = request.state.user

    # 仅超管可以访问审计日志
    if user.get("super_admin_flag") != 1:
        raise HTTPException(status_code=403, detail="仅超级管理员可访问审计日志")

    items, total = get_operation_logs(
        db=db,
        skip=skip,
        limit=limit,
        action=action,
        resource_type=resource_type,
        user_code=user_code,
        team_code=team_code,
        start_time=start_time,
        end_time=end_time,
    )

    return AuditLogListResponse(
        total=total,
        items=[OperationLogResponse.model_validate(log) for log in items]
    )


# ==================== 导出（QE4-4，企业功能位） ====================

_EXPORT_COLUMNS = (
    "id", "created_at", "user_code", "user_name", "team_code",
    "action", "resource_type", "resource_id", "resource_name", "ip_address", "detail",
)


def _row_values(log: OperationLogResponse) -> list:
    return [
        log.id,
        log.created_at or "",
        log.user_code,
        log.user_name or "",
        log.team_code or "",
        log.action,
        log.resource_type,
        log.resource_id,
        log.resource_name or "",
        log.ip_address or "",
        # detail 转稳定 JSON 字符串（Excel 单元格吃 JSON 文本，csv 同口径）
        str(log.detail) if log.detail else "",
    ]


@router.get("/export", summary="导出审计日志（xlsx/csv，企业功能位）")
def export_audit_logs(
    request: Request,
    format: str = Query("csv", description="导出格式：csv / xlsx"),
    action: Optional[str] = Query(None, description="操作类型过滤（同列表）"),
    resource_type: Optional[str] = Query(None, description="资源类型过滤（同列表）"),
    user_code: Optional[str] = Query(None, description="操作者过滤（同列表）"),
    team_code: Optional[str] = Query(None, description="团队过滤（同列表）"),
    start_time: Optional[datetime] = Query(None, description="开始时间（同列表）"),
    end_time: Optional[datetime] = Query(None, description="结束时间（同列表）"),
    db: Session = Depends(get_db),
):
    """导出审计日志（仅超管；企业功能位 AUDIT_EXPORT_ENABLED 开启才可用，否则 404）

    单次导出上限 AUDIT_EXPORT_MAX_ROWS（默认 1 万行）：超限返回 400 提示收窄
    过滤条件——审计整表可能远大于常规导出，防一次请求拖垮内存。
    """
    user = request.state.user
    if user.get("super_admin_flag") != 1:
        raise HTTPException(status_code=403, detail="仅超级管理员可导出审计日志")

    settings = get_settings()
    if not settings.AUDIT_EXPORT_ENABLED:
        # 功能位未开：404（能力不存在），与权限不足（403）区分口径
        raise HTTPException(status_code=404, detail="审计导出未启用（企业功能位 AUDIT_EXPORT_ENABLED）")

    if format not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="format 仅支持 csv / xlsx")

    # 复用列表过滤条件；上限行数 +1 探测是否超限
    limit = settings.AUDIT_EXPORT_MAX_ROWS
    items, _total = get_operation_logs(
        db=db, skip=0, limit=limit + 1, action=action, resource_type=resource_type,
        user_code=user_code, team_code=team_code, start_time=start_time, end_time=end_time,
    )
    if len(items) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"导出结果超过单次上限 {limit} 行，请用时间/类型条件收窄后分批导出",
        )
    rows = [OperationLogResponse.model_validate(log) for log in items]

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_EXPORT_COLUMNS)
        for row in rows:
            writer.writerow(_row_values(row))
        # utf-8-sig BOM：Excel 双击打开不乱码
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="audit_logs_{timestamp}.csv"'},
        )

    # xlsx：openpyxl 内存工作簿（与数据集导出同依赖）
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "audit_logs"
    sheet.append(list(_EXPORT_COLUMNS))
    for row in rows:
        sheet.append(_row_values(row))
    output = io.BytesIO()
    workbook.save(output)
    return Response(
        content=output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="audit_logs_{timestamp}.xlsx"'},
    )
