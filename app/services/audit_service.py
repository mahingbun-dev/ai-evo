"""
审计日志服务
记录所有写操作的审计信息
"""
import logging
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models.audit_models import OperationLog
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


def log_operation(
    user,
    action: str,
    resource_type: str,
    resource_id: str,
    resource_name: str = "",
    detail: Optional[Dict] = None,
    ip_address: str = ""
) -> Optional[OperationLog]:
    """
    记录操作审计日志
    支持字典和对象两种 user 格式
    使用独立数据库 session，避免影响主业务事务
    """
    # 支持字典和对象两种格式
    if isinstance(user, dict):
        user_code = user.get('user_code', '')
        user_name = user.get('user_name', '')
        team_code = user.get('org_code', '')
    else:
        user_code = user.user_code if hasattr(user, 'user_code') else ""
        user_name = user.user_name if hasattr(user, 'user_name') else ""
        team_code = user.org_code if hasattr(user, 'org_code') else ""

    log = OperationLog(
        user_code=user_code,
        user_name=user_name,
        team_code=team_code,
        action=action.upper(),
        resource_type=resource_type,
        resource_id=str(resource_id),
        resource_name=resource_name or "",
        detail=detail,
        ip_address=ip_address or "",
    )

    # 使用独立 session，避免与主业务共用 session
    db = SessionLocal()
    try:
        db.add(log)
        db.commit()
        db.refresh(log)
        logger.info(f"审计日志: {action} {resource_type} {resource_id} by {user_code or 'unknown'}")
        return log
    except Exception as e:
        logger.warning(f"审计日志记录失败: {e}")
        db.rollback()
        return None
    finally:
        db.close()


def get_operation_logs(
    db: Session,
    skip: int = 0,
    limit: int = 20,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    user_code: Optional[str] = None,
    team_code: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> Tuple[List[OperationLog], int]:
    """
    分页查询审计日志，多条件组合过滤
    返回：(日志列表, 总数)
    """
    query = db.query(OperationLog)

    # 构建过滤条件
    filters = []

    if action:
        filters.append(OperationLog.action == action.upper())

    if resource_type:
        filters.append(OperationLog.resource_type == resource_type)

    if user_code:
        filters.append(OperationLog.user_code == user_code)

    if team_code:
        filters.append(OperationLog.team_code == team_code)

    if start_time:
        filters.append(OperationLog.created_at >= start_time)

    if end_time:
        filters.append(OperationLog.created_at <= end_time)

    if filters:
        query = query.filter(and_(*filters))

    # 获取总数
    total = query.count()

    # 分页查询
    items = query.order_by(OperationLog.created_at.desc()).offset(skip).limit(limit).all()

    return items, total
