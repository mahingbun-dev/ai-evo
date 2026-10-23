"""
审计日志服务
记录所有写操作的审计信息

QE4-4 审计补全：写端点统一走 audited(action, resource_type) 声明式依赖
（plans/2026-10-22-Agent质量工程平台开发方案.md §4.4），替代散点手工
log_operation 调用；覆盖面由 tests/unit/test_audit_coverage.py 扫描
API 注册表静态断言强制（非白名单 POST/PUT/PATCH/DELETE 必须声明审计依赖）。
"""
import logging
from typing import Iterator, List, Optional, Tuple, Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_

from fastapi import Request

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


# ==================== QE4-4：声明式审计依赖 ====================

def _sole_path_param(path_params: Dict) -> str:
    """路径参数里恰好一个候选 ID 时取它；多个/零个返回空（由接线方用 id_param 显式指定）"""
    if len(path_params) == 1:
        return str(next(iter(path_params.values())))
    return ""


def _client_ip(request: Request) -> str:
    """客户端 IP（与既有手工审计同口径：直连地址，不做 XFF 信任）"""
    return request.client.host if request.client else ""


def record_route_audit(
    request: Request,
    action: str,
    resource_type: str,
    id_param: Optional[str] = None,
) -> None:
    """路由级审计落库（audited 依赖的 teardown 调用；失败不阻断业务）

    资源标识解析优先级：
    1. handler 内 ``request.state.audit_resource = {"id":…, "name":…, "detail":…}``
       ——创建类端点无路径 ID 时由 handler 显式暂存（响应体里的新资源 ID）；
    2. ``id_param`` 指定的路径参数（多参数路由必须显式指定）；
    3. 路径里恰好唯一的参数。
    """
    stash = getattr(request.state, "audit_resource", None)
    stash = stash if isinstance(stash, dict) else {}

    resource_id = stash.get("id")
    if not resource_id:
        if id_param:
            resource_id = request.path_params.get(id_param, "")
        else:
            resource_id = _sole_path_param(request.path_params)

    detail: Dict[str, Any] = {"method": request.method, "path": request.url.path}
    if isinstance(stash.get("detail"), dict):
        detail.update(stash["detail"])
    # API Key 通道（CI/CLI）落审计时标注来源，方便区分页面操作与机器调用
    if getattr(request.state, "auth_type", None) == "api_key":
        detail.setdefault("via", "api_key")

    try:
        log_operation(
            user=getattr(request.state, "user", None) or {},
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id or ""),
            resource_name=str(stash.get("name") or ""),
            detail=detail,
            ip_address=_client_ip(request),
        )
    except Exception as e:  # 审计失败不阻断业务（SessionLocal 构造等意外也不外抛）
        logger.warning(f"审计日志记录失败: {e}")


def audited(action: str, resource_type: str, id_param: Optional[str] = None):
    """声明式审计依赖（QE4-4）：包写端点，成功后自动落 operation_logs

    用法（路由装饰器，与 require_scope 等既有依赖并列）::

        @router.delete("/api-keys/{key_id}",
                       dependencies=[Depends(audited("DELETE", "api_key", id_param="key_id"))])

    语义：
    - 依赖是 yield 型生成器——teardown 在 handler **成功返回后**执行，
      端点抛 HTTPException（400/403/404/409…）时审计不落库（只记成功的写）；
    - 创建类端点（POST 集合路径，无资源 ID）由 handler 在返回前暂存：
      ``request.state.audit_resource = {"id": x.id, "name": x.name}``，
      需要变更细节时附 ``"detail": {...}``；
    - 依赖函数上带 ``__audit_action__`` / ``__audit_resource_type__`` 标记，
      tests/unit/test_audit_coverage.py 据此扫描注册表做覆盖面静态断言。
    """
    action = action.upper()

    def dependency(request: Request) -> Iterator[None]:
        yield
        record_route_audit(request, action, resource_type, id_param)

    dependency.__audit_action__ = action
    dependency.__audit_resource_type__ = resource_type
    dependency.__audit_id_param__ = id_param
    return dependency


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
