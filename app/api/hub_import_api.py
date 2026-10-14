"""
anc-hub 执行包批量导入 API（数据集迭代阶段C）

- POST /evaluation/datasets/import/hub-files：创建导入 job 并投递 Celery（参数形态
  对齐既有 importSessionsFromHub：file_ids + name/description/real_dataset_id）
- GET /hub-import-jobs/{job_id}：轮询导入进度（配合 Redis task_status:{job_id} 推送）
- POST /hub-import-jobs/{job_id}/retry：只重跑失败项（幂等，已成功的不重复导入）
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.access_control import ensure_dataset_access, ensure_hub_import_job_access, user_team_code
from app.services.hub_import_job_service import create_import_job, get_job, retry_job
from app.services.hub_file_service import resolve_hub_bearer_token

logger = logging.getLogger(__name__)


# ==================== Request / Response Models ====================

class HubFilesImportRequest(BaseModel):
    """从 anc-hub 导入执行包请求（参数形态对齐 HubSessionImportRequest）"""
    file_ids: List[str] = Field(..., description="anc-hub file-store 执行包 file_id 列表")
    name: str = Field("", description="新建数据集名称")
    description: str = Field("", description="数据集描述")
    real_dataset_id: str = Field("", description="追加模式下的真实数据集ID")
    script_id: str = Field("", description="可选：脚本库清洗脚本 ID/名称——非空时逐包执行清洗，脚本产出的行内容作为数据集行数据")


class HubImportJobResponse(BaseModel):
    """导入任务进度响应（GET 轮询 / 创建 / 重试共用）"""
    id: str
    dataset_id: str
    file_ids: List[str] = Field(default_factory=list)
    script_id: Optional[str] = None
    status: str
    total_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    fail_detail: Optional[Dict[str, str]] = None
    created_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator('created_at', mode='before')
    @classmethod
    def datetime_to_str(cls, v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        return v


# ==================== 数据集导入创建端点（挂在数据集前缀下） ====================

dataset_router = APIRouter(prefix="/evaluation/datasets", tags=["评测数据集"])


def _get_user_code(request: Optional[Request]) -> Optional[str]:
    """从请求状态提取当前用户（与 import_sessions_from_hub 同手法）"""
    user = getattr(request, "state", None) and getattr(request.state, "user", None)
    if isinstance(user, dict):
        return user.get("user_code")
    if user is not None and hasattr(user, "user_code"):
        return user.user_code
    return None


@dataset_router.post(
    "/import/hub-files",
    response_model=HubImportJobResponse,
    summary="从 anc-hub 导入执行包（批量异步）",
)
def import_hub_files(
    body: HubFilesImportRequest,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """按勾选的 anc-hub file_id 创建批量导入任务并投递 Celery

    数据集解析同步完成（新建或追加 real_dataset_id），逐包下载解包/写数据行在
    worker 侧执行；进度经 GET /hub-import-jobs/{job_id} 轮询或
    Redis task_status:{job_id} 频道推送。

    下载以配置的凭证进行（静态服务 token 优先，回退同域部署的平台登录态
    Cookie），随 Celery 任务参数投递给 worker（凭证仅存活于本次任务
    消息，任务结束即消费，不落库）。不做入口强拦截：登记表已物化的包
    幂等命中零下载（无需凭证），未命中且缺凭证时由 worker 按包报中文错误。
    """
    from app.tasks.celery_tasks import run_hub_import_job

    bearer_token = resolve_hub_bearer_token(request)

    # W9：追加模式目标数据集归属校验（新建模式无资源可校验，数据集随用户团队落库）
    if body.real_dataset_id:
        ensure_dataset_access(db, request.state.user, body.real_dataset_id)

    try:
        job = create_import_job(
            db,
            file_ids=body.file_ids,
            name=body.name,
            description=body.description,
            real_dataset_id=body.real_dataset_id,
            created_by=_get_user_code(request),
            script_id=body.script_id,
            team_code=user_team_code(request.state.user) if request else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[hub_import] 创建导入任务失败: {e}")
        raise HTTPException(status_code=500, detail=f"创建导入任务失败: {e}")

    try:
        # kwargsrepr 遮蔽凭证：worker 日志的 Received task 行不落真实 token
        run_hub_import_job.apply_async(
            args=[job.id], kwargs={"bearer_token": bearer_token}, kwargsrepr="{<masked>}"
        )
    except Exception as e:
        logger.error(f"[hub_import] 导入任务投递失败: job_id={job.id} 错误: {e}")
        raise HTTPException(status_code=500, detail=f"导入任务投递失败，请稍后重试: {e}")

    return HubImportJobResponse.model_validate(job)


# ==================== 导入任务进度 / 重试端点 ====================

router = APIRouter(prefix="/hub-import-jobs", tags=["anc-hub 执行包导入"])


@router.get("/{job_id}", response_model=HubImportJobResponse, summary="查询导入任务进度")
def get_hub_import_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """轮询导入进度：状态、成功/失败计数与失败明细（{file_id: 中文错误}；W9：经目标数据集团队校验）"""
    # W9：job 无 team_code 列，经 dataset_id 的数据集团队校验（查出+校验一步完成）
    ensure_hub_import_job_access(db, request.state.user, job_id)
    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"导入任务不存在: {job_id}")
    return HubImportJobResponse.model_validate(job)


@router.post("/{job_id}/retry", response_model=HubImportJobResponse, summary="重试导入任务的失败项")
def retry_hub_import_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """只重跑失败项：已成功的包不重复导入（succeeded_file_ids 幂等键；W9：归属校验）

    重试同样以配置的凭证下载（静态服务 token 优先，回退平台登录态 Cookie）
    随任务投递（与创建任务时相互独立，不依赖创建时的凭证仍然有效）。
    """
    from app.tasks.celery_tasks import run_hub_import_job

    bearer_token = resolve_hub_bearer_token(request)

    # W9：导入 job 归属校验（job → dataset → team_code）
    ensure_hub_import_job_access(db, request.state.user, job_id)

    try:
        job = retry_job(db, job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[hub_import] 重置导入任务失败: job_id={job_id} 错误: {e}")
        raise HTTPException(status_code=500, detail=f"重置导入任务失败: {e}")

    if not job:
        raise HTTPException(status_code=404, detail=f"导入任务不存在: {job_id}")

    try:
        # kwargsrepr 遮蔽凭证：worker 日志的 Received task 行不落真实 token
        run_hub_import_job.apply_async(
            args=[job.id], kwargs={"bearer_token": bearer_token}, kwargsrepr="{<masked>}"
        )
    except Exception as e:
        logger.error(f"[hub_import] 重试任务投递失败: job_id={job.id} 错误: {e}")
        raise HTTPException(status_code=500, detail=f"重试任务投递失败，请稍后重试: {e}")

    return HubImportJobResponse.model_validate(job)
