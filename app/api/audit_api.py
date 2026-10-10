"""
审计日志 API
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from datetime import datetime

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
