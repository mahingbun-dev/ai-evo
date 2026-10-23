"""
团队管理 API
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from datetime import datetime

from app.db.session import get_db
from app.models.user_models import User
from app.services.access_control import (
    TEAM_ROLES,
    ROLE_OWNER,
    is_super_admin,
    user_role,
    user_team_code,
)
from app.services.audit_service import audited
from app.services.team_service import get_teams, get_team, get_team_members_all, get_team_stats, update_user_status


# ==================== Response Models ====================

class TeamResponse(BaseModel):
    """团队响应模型"""
    model_config = ConfigDict(from_attributes=True)

    team_code: str
    team_name: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_serializer('created_at')
    def serialize_created_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    @field_serializer('updated_at')
    def serialize_updated_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


class TeamStatsResponse(BaseModel):
    """团队统计响应"""
    team_code: str
    task_count: int
    dataset_count: int
    model_count: int
    member_count: int


class TeamWithStatsResponse(TeamResponse):
    """带统计的团队响应"""
    task_count: int = 0
    dataset_count: int = 0
    model_count: int = 0
    member_count: int = 0


class TeamMemberResponse(BaseModel):
    """团队成员响应"""
    model_config = ConfigDict(from_attributes=True)

    user_code: str
    user_name: Optional[str] = None
    email: Optional[str] = None
    employee_id: Optional[str] = None
    org_code: Optional[str] = None
    org_name: Optional[str] = None
    position_name: Optional[str] = None
    role: Optional[str] = None
    is_active: bool = True
    last_login_at: Optional[datetime] = None

    @field_serializer('last_login_at')
    def serialize_last_login_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


class SetMemberStatusRequest(BaseModel):
    """设置成员状态请求"""
    is_active: bool = Field(..., description="是否启用，True=启用，False=禁用")


class TeamMembersListResponse(BaseModel):
    """团队成员列表响应"""
    total: int
    items: List[TeamMemberResponse]


class TeamListResponse(BaseModel):
    """团队列表响应"""
    total: int
    items: List[TeamWithStatsResponse]


# ==================== Router ====================

router = APIRouter(prefix="/teams", tags=["团队管理"])


# ==================== Endpoints ====================

@router.get("", response_model=TeamListResponse, summary="获取团队列表")
def list_teams(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """获取团队列表（仅超管可访问）"""
    user = request.state.user

    # 仅超管可以访问团队管理
    if user.get("super_admin_flag") != 1:
        raise HTTPException(status_code=403, detail="仅超级管理员可访问团队管理")

    teams = get_teams(db, skip=skip, limit=limit)

    # 构建响应，每个团队带统计
    items = []
    for team in teams:

        stats = get_team_stats(db, team.team_code)
        items.append(TeamWithStatsResponse(
            team_code=team.team_code,
            team_name=team.team_name,
            is_active=team.is_active,
            created_at=team.created_at,
            updated_at=team.updated_at,
            task_count=stats["task_count"],
            dataset_count=stats["dataset_count"],
            model_count=stats["model_count"],
            member_count=stats["member_count"],
        ))

    # 过滤后重新计数
    total = len(items)

    return TeamListResponse(total=total, items=items)


@router.get("/{team_code}", response_model=TeamWithStatsResponse, summary="获取团队详情")
def get_team_detail(
    team_code: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取团队详情（仅超管可访问）"""
    user = request.state.user

    # 仅超管可以访问团队管理
    if user.get("super_admin_flag") != 1:
        raise HTTPException(status_code=403, detail="仅超级管理员可访问团队管理")

    team = get_team(db, team_code)

    if not team:
        raise HTTPException(status_code=404, detail="团队不存在")

    stats = get_team_stats(db, team_code)

    return TeamWithStatsResponse(
        team_code=team.team_code,
        team_name=team.team_name,
        is_active=team.is_active,
        created_at=team.created_at,
        updated_at=team.updated_at,
        task_count=stats["task_count"],
        dataset_count=stats["dataset_count"],
        model_count=stats["model_count"],
        member_count=stats["member_count"],
    )


@router.get("/{team_code}/members", response_model=TeamMembersListResponse, summary="获取团队成员列表")
def get_team_members_list(
    team_code: str,
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """获取团队成员列表（仅超管可访问，包含禁用成员）"""
    user = request.state.user

    # 仅超管可以访问团队管理
    if user.get("super_admin_flag") != 1:
        raise HTTPException(status_code=403, detail="仅超级管理员可访问团队管理")

    # 超管查看所有成员（包括禁用的）
    members, total = get_team_members_all(db, team_code, skip=skip, limit=limit, include_inactive=True)
    items = [TeamMemberResponse.model_validate(m) for m in members]

    return TeamMembersListResponse(total=total, items=items)


@router.patch("/{team_code}/members/{user_code}/status", summary="设置团队成员状态",
              dependencies=[Depends(audited("UPDATE", "team_member_status", id_param="user_code"))])
def set_member_status(
    team_code: str,
    user_code: str,
    request: Request,
    body: SetMemberStatusRequest,
    db: Session = Depends(get_db),
):
    """设置团队成员启用/禁用状态（仅超管可用）"""
    user = request.state.user

    # 仅超管可以操作
    if user.get("super_admin_flag") != 1:
        raise HTTPException(status_code=403, detail="仅超级管理员可执行此操作")

    # 验证目标用户属于该团队
    target_user = db.get(User, user_code)
    if not target_user or target_user.org_code != team_code:
        raise HTTPException(status_code=404, detail="该团队中不存在此成员")

    # 更新用户状态
    updated_user = update_user_status(db, user_code, body.is_active)

    if not updated_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # QE4-4：暂存审计标识（团队/新旧状态进 detail）
    request.state.audit_resource = {
        "id": user_code, "name": updated_user.username or user_code,
        "detail": {"team_code": team_code, "is_active": body.is_active},
    }
    return {
        "success": True,
        "message": f"成员已{'启用' if body.is_active else '禁用'}",
        "user_code": user_code,
        "is_active": body.is_active
    }


class SetMemberRoleRequest(BaseModel):
    """设置团队成员角色请求（M4 QE4-3）"""
    role: str = Field(..., description=f"目标角色，可选：{list(TEAM_ROLES)}")


@router.patch("/{team_code}/members/{user_code}/role", summary="设置团队成员角色",
              dependencies=[Depends(audited("UPDATE", "team_member_role", id_param="user_code"))])
def set_member_role(
    team_code: str,
    user_code: str,
    request: Request,
    body: SetMemberRoleRequest,
    db: Session = Depends(get_db),
):
    """设置团队成员角色（M4 QE4-3）：本团队 owner 或超管可改，落审计。

    保护约束：
    - 末位 owner 不可降级（团队必须始终保有至少一位 owner，否则无人能再
      管理角色——只能靠超管兜底恢复）；
    - 跨团队 owner 无权（owner 权限仅及本团队，跨团队仍 403）。
    """
    operator = request.state.user
    if body.role not in TEAM_ROLES:
        raise HTTPException(status_code=400, detail=f"非法角色，可选：{list(TEAM_ROLES)}")

    target = db.get(User, user_code)
    if not target or (target.org_code or "") != (team_code or ""):
        raise HTTPException(status_code=404, detail="该团队中不存在此成员")

    # 权限闸：超管放行；否则必须是本团队 owner
    if not is_super_admin(operator):
        if user_team_code(operator) != (team_code or ""):
            raise HTTPException(status_code=403, detail="无权管理其他团队的成员角色")
        if user_role(operator) != ROLE_OWNER:
            raise HTTPException(status_code=403, detail="仅团队 owner 可调整成员角色")

    old_role = user_role(target)
    # 末位 owner 保护：目标当前是 owner 且要降级时，团队必须还有其他 owner
    if old_role == ROLE_OWNER and body.role != ROLE_OWNER:
        other_owners = (
            db.query(User)
            .filter(User.org_code == team_code, User.role == ROLE_OWNER,
                    User.user_code != target.user_code, User.is_active == True)  # noqa: E712
            .count()
        )
        if other_owners == 0:
            raise HTTPException(
                status_code=409, detail="团队至少保留一位 owner，请先转移 owner 角色再降级")

    target.role = body.role
    db.commit()
    db.refresh(target)
    # QE4-4：审计改走 audited 路由依赖（team_code/新旧角色进 detail）
    request.state.audit_resource = {
        "id": target.user_code, "name": target.username or target.user_code,
        "detail": {"team_code": team_code, "old_role": old_role, "new_role": body.role},
    }
    return {
        "success": True,
        "message": f"成员角色已调整为 {body.role}",
        "user_code": user_code,
        "role": body.role,
    }
