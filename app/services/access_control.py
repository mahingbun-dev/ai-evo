"""对象级归属校验（W9，P0 IDOR 收口）

整改计划 §3 W9：任务/数据集/媒体/hub 文件/导入 job 的详情、更新、删除、
下载、补传、WS 订阅此前按 ID 直取，无对象级归属校验。本模块提供统一
口径的「查出 + 校验」一步完成原语（避免先查对象、再单独查归属造成的
TOCTOU 窗口），供约 30 个端点接入。

口径（与既有团队语义严格对齐）：
- 超管放行：``super_admin_flag == 1``（W7 后 SSO 缺省 0，本地可管理）；
- 团队比较：资源 ``team_code`` 与用户 ``org_code``，任一侧为空按
  ``DEFAULT`` 兜底（与任务/数据集创建 ``org_code or "DEFAULT"``、
  W5 WS 归属校验 ``(task.team_code or "DEFAULT") == user_team`` 同口径）；
- 资源不存在统一 404、跨团队统一 403（中文提示，前端可直接展示）。

异常形态：
- ``AccessDeniedError``：纯异常（HTTP 无关），WS 侧映射 close(4403)、
  批量操作逐项捕获记入 details；
- ``ensure_*`` 系列：直接抛 ``fastapi.HTTPException``（403/404），
  HTTP 端点零样板接入（services 层抛 HTTPException 有先例：excel_service）。
"""
import logging
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_TEAM_CODE = "DEFAULT"

FORBIDDEN_DETAIL = "无权访问该资源（跨团队操作被拒绝）"


class AccessDeniedError(Exception):
    """跨团队访问被拒（消息中文，可直接透出；WS 侧映射 4403）"""


def _field(user: Any, name: str) -> Any:
    """兼容 dict（request.state.user / WS user）与 ORM 对象两种用户形态"""
    if isinstance(user, dict):
        return user.get(name)
    return getattr(user, name, None)


def is_super_admin(user: Any) -> bool:
    """超管判定：super_admin_flag == 1（None/缺失按普通用户）"""
    return int(_field(user, "super_admin_flag") or 0) == 1


def _norm_team(code: Any) -> str:
    """团队代码规范化：空值兜底 DEFAULT（与资源创建口径一致）"""
    code = str(code or "").strip()
    return code or DEFAULT_TEAM_CODE


def user_team_code(user: Any) -> str:
    """用户的团队代码（org_code，空值兜底 DEFAULT）"""
    return _norm_team(_field(user, "org_code"))


def resource_team_code(team_code: Any) -> str:
    """资源的团队代码（列值可能为 NULL 的历史行按 DEFAULT 口径）"""
    return _norm_team(team_code)


def check_team_access(user: Any, team_code: Any) -> None:
    """团队归属判定内核（不抛 HTTP）：超管放行，否则团队不一致抛
    AccessDeniedError。ensure_team_access 与 WS 归属校验共用本函数，
    保证 HTTP 与 WS 同一口径。"""
    if is_super_admin(user):
        return
    if resource_team_code(team_code) != user_team_code(user):
        raise AccessDeniedError(FORBIDDEN_DETAIL)


def ensure_team_access(user: Any, team_code: Any) -> None:
    """团队归属校验（计划 W9 原语一）：超管放行，否则 403。

    适用于：资源已由调用方查出的场景（ensure_task_access 内部亦用它
    的语义完成校验）。"""
    try:
        check_team_access(user, team_code)
    except AccessDeniedError as e:
        logger.warning(f"[ACCESS] 403 - {e}（user_team={user_team_code(user)}, resource_team={resource_team_code(team_code)}）")
        raise HTTPException(status_code=403, detail=str(e))


def _deny_or_404(record: Any, detail_404: str):
    """404 统一映射（资源不存在时不泄漏任何归属信息）"""
    if record is None:
        raise HTTPException(status_code=404, detail=detail_404)
    return record


def _check_record_access(user: Any, record: Any, detail_404: str):
    """查出后的统一校验：404 / 团队归属（供各 ensure_*_access 复用）

    team_code 用 getattr 兜底：历史行 NULL 与无该属性的替身对象一律按
    DEFAULT 口径参与比较（与资源创建/WS 校验口径一致）。"""
    _deny_or_404(record, detail_404)
    try:
        check_team_access(user, getattr(record, "team_code", None))
    except AccessDeniedError as e:
        logger.warning(f"[ACCESS] 403 - {e}")
        raise HTTPException(status_code=403, detail=str(e))
    return record


def ensure_task_access(db: Session, user: Any, task_id: str):
    """评测任务归属校验（计划 W9 原语二）：查出 + 校验一步完成，返回 Task。

    防TOCTOU：对象与归属在同一次查询结果上完成校验，调用方拿到的
    返回对象即是已通过校验的对象，不应再按 task_id 二次查询。
    """
    from app.models.task_models import EvaluationTask

    task = db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
    return _check_record_access(user, task, "任务不存在")


def ensure_dataset_access(db: Session, user: Any, dataset_id: str):
    """评测数据集归属校验：查出 + 校验一步完成，返回 Dataset"""
    from app.models.dataset_models import EvaluationDataset

    dataset = (
        db.query(EvaluationDataset).filter(EvaluationDataset.id == dataset_id).first()
    )
    return _check_record_access(user, dataset, "数据集不存在")


def ensure_media_access(db: Session, user: Any, media_id: str):
    """媒体资源归属校验：查出 + 校验一步完成，返回 MediaResource"""
    from app.models.media_models import MediaResource

    media = db.query(MediaResource).filter(MediaResource.id == media_id).first()
    return _check_record_access(user, media, "文件不存在")


def ensure_hub_file_access(db: Session, user: Any, file_id: str):
    """anc-hub 执行包登记归属校验：按 file_id 查出 + 校验一步完成。

    file_id 空串 / 未登记 → 404（与 hub_file_service.HubFileError 的
    登记语义一致，读取面统一中文提示）。
    """
    from app.models.hub_file_models import HubFileRecord
    from app.services.hub_file_service import HubFileError

    fid = str(file_id or "").strip()
    if not fid:
        raise HTTPException(status_code=404, detail="执行包未登记: （file_id 为空）")
    record = (
        db.query(HubFileRecord).filter(HubFileRecord.file_id == fid).first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"执行包未登记: {fid}（请先从 anc-hub 导入）")
    # 历史行（迁移前导入）team_code 为空 → 按 DEFAULT 口径参与比较
    try:
        check_team_access(user, getattr(record, "team_code", None))
    except AccessDeniedError as e:
        logger.warning(f"[ACCESS] 403 - {e}")
        raise HTTPException(status_code=403, detail=str(e))
    return record


def ensure_hub_import_job_access(db: Session, user: Any, job_id: str):
    """anc-hub 导入任务归属校验：job 无 team_code 列，经 job.dataset_id
    的数据集团队校验（一次 join 查出 + 校验，同防 TOCTOU），返回 (job, dataset)。"""
    from app.models.dataset_models import EvaluationDataset
    from app.models.hub_file_models import HubImportJob

    row = (
        db.query(HubImportJob, EvaluationDataset)
        .outerjoin(EvaluationDataset, HubImportJob.dataset_id == EvaluationDataset.id)
        .filter(HubImportJob.id == job_id)
        .first()
    )
    if row is None or row[0] is None:
        raise HTTPException(status_code=404, detail=f"导入任务不存在: {job_id}")
    job, dataset = row
    team_code = getattr(dataset, "team_code", None) if dataset is not None else None
    try:
        check_team_access(user, team_code)
    except AccessDeniedError as e:
        logger.warning(f"[ACCESS] 403 - {e}")
        raise HTTPException(status_code=403, detail=str(e))
    return job, dataset


def filter_team_code(user: Any) -> Optional[str]:
    """列表过滤参数：超管返回 None（看全部），普通用户返回本团队代码。

    与既有任务列表口径一致（evaluation_task_api.get_tasks）。
    """
    if is_super_admin(user):
        return None
    return user_team_code(user)


def ensure_model_access(db: Session, user: Any, model_id: str):
    """模型配置归属校验（W19 补收口）：查出 + 校验一步完成，返回 ModelConfig。

    模型配置定位为团队共用的评测基础设施：列表保持登录可见（响应模型
    不含凭据字段），但详情（ModelDetailResponse 含 auth_config /
    request_config，可能携带 bearer token、basic 口令等凭据）与一切写
    操作、连接测试、WS 测连按团队收口。
    """
    from app.models.model_config_models import ModelConfig

    model = (
        db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    )
    return _check_record_access(user, model, "模型不存在")


def ensure_super_admin(request: Any) -> None:
    """超管门禁依赖（W19 补收口）：供 router 级 dependencies 与单端点使用。

    与 pre_judge_script_api._ensure_admin 同口径（deny by default：用户
    上下文缺失或非超管一律 403）。request 传 FastAPI Request；经
    Depends 挂在 APIRouter 上时对整组端点生效。
    """
    user = getattr(request, "state", None)
    user = getattr(user, "user", None) if user is not None else None
    if not isinstance(user, dict) or int(user.get("super_admin_flag") or 0) != 1:
        logger.warning("[ACCESS] 403 - 非超管访问受限功能（deny by default）")
        raise HTTPException(status_code=403, detail="仅超级管理员可访问该功能")
