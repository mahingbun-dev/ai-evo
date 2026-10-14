"""
媒体资源API

W9 对象级归属校验收口：
- 上传写入用户团队（team_code=org_code，归属锚点）；
- 列表超管看全部、普通用户只看本团队；
- 详情/内容/删除按对象做团队归属校验（跨团队 403）。
"""
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from typing import List, Optional, Union
from pydantic import BaseModel, Field, field_serializer
from datetime import datetime
from app.db.session import get_db
from app.services.access_control import ensure_media_access, filter_team_code, user_team_code
from app.services.media_service import MediaService


# ==================== Response Models ====================

class MediaResponse(BaseModel):
    id: str
    file_name: str
    file_type: str
    mime_type: Optional[str]
    file_size: Optional[int]
    description: Optional[str]
    uploaded_by: Optional[str]
    # W9 修复（矩阵测试暴露的既有缺陷）：ORM 返回 datetime，字段注解为 str
    # 会在 pydantic v2 校验期直接失败（field_serializer 不参与校验）；放宽为
    # Union 后仍由 serializer 统一输出 ISO 字符串，前端契约不变
    created_at: Union[str, datetime]

    @field_serializer('created_at')
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat() if value else ""

    class Config:
        from_attributes = True


class MediaListResponse(BaseModel):
    total: int
    items: List[MediaResponse]


# ==================== Router ====================

router = APIRouter(prefix="/media", tags=["媒体资源"])


@router.post("/upload", response_model=MediaResponse, summary="上传媒体文件")
async def upload_media(
    request: Request,
    file: UploadFile = File(..., description="要上传的文件"),
    description: Optional[str] = Query(None, description="文件描述"),
    uploaded_by: Optional[str] = Query(None, description="上传人"),
    db: Session = Depends(get_db),
):
    """上传媒体文件（图片、PDF等；W9：team_code 随用户团队落库）"""
    service = MediaService(db)
    media = await service.upload_file(
        file, description, uploaded_by, team_code=user_team_code(request.state.user)
    )
    return media


@router.get("", response_model=MediaListResponse, summary="获取媒体文件列表")
def get_media_list(
    request: Request,
    file_type: Optional[str] = Query(None, description="文件类型过滤"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """获取媒体文件列表（W9：超管看全部，普通用户只看本团队）"""
    service = MediaService(db)
    items = service.get_media_list(
        file_type=file_type,
        skip=skip,
        limit=limit,
        team_code=filter_team_code(request.state.user),
    )
    total = len(items)
    return {"total": total, "items": items}


@router.get("/{media_id}", response_model=MediaResponse, summary="获取媒体文件信息")
def get_media_info(
    media_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取媒体文件信息（W9：对象级归属校验）"""
    media = ensure_media_access(db, request.state.user, media_id)
    return media


@router.get("/{media_id}/content", summary="获取媒体文件内容")
def get_media_content(
    media_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取媒体文件内容（用于预览；W9：对象级归属校验，跨团队下载拒绝）"""
    media = ensure_media_access(db, request.state.user, media_id)

    service = MediaService(db)
    content = service.get_file_content(media_id)
    if not content:
        raise HTTPException(status_code=404, detail="文件内容不存在")

    return Response(
        content=content,
        media_type=media.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{media.file_name}"'
        }
    )


@router.delete("/{media_id}", summary="删除媒体文件")
def delete_media(
    media_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """删除媒体文件（W9：对象级归属校验）"""
    ensure_media_access(db, request.state.user, media_id)
    service = MediaService(db)
    success = service.delete_media(media_id)
    if not success:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"message": "删除成功"}
