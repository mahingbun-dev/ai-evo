"""
anc-hub 文件 API

- POST /hub-files/query：file_query 透传，驱动「从 anc-hub 导入」页签的远端浏览。
- 数据集迭代阶段D（执行包文件树 / 预览 / 下载 / 匹配预览）：
  - GET /hub-files/{file_id}/tree：返回登记表 file_index + 人工补传叠加视图
    （零文件 IO，补传条目带 is_manual 标记）
  - GET /hub-files/{file_id}/nested-tree?path=：嵌套 zip 内层清单
  - GET /hub-files/{file_id}/content?path=：文本/图片预览（二进制提示走 download）
  - GET /hub-files/{file_id}/download?path=：包内任意文件下载（中文文件名 URL 编码）
  - POST /hub-files/match-preview：pattern 匹配预览（与执行时同一 match_files 语义）
- 人工补传（执行包内容不足时人工补充文件）：
  - POST /hub-files/{file_id}/supplements：补传文件（散文件落指定子目录 /
    zip 自动解压保留结构，同名覆盖并返回覆盖明细）
  - DELETE /hub-files/{file_id}/supplements?path=：删除单个人工补传文件
    （原始执行包文件只读，不可删）
  所有带 path 参数的端点统一经 resolve_path_in_root 防穿越校验（realpath
  必须以解包根为前缀，覆盖 .. / 绝对路径 / 编码逃逸 / symlink 逃逸）；
  补传文件预览/下载时先查解包区、miss 回退人工补传区（_locate_member）。
"""
import logging
import mimetypes
import os
from typing import Annotated, Any, Dict, List, Optional, Union
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.hub_file_service import (
    HubEndpointNotConfiguredError,
    HubFileError,
    HubFileService,
    HubRecordMissingError,
    _supplement_dir_for,
    add_supplements,
    delete_supplement,
    resolve_hub_bearer_token,
    get_hub_record,
    hub_match_preview,
    merged_index,
    preview_kind,
    read_nested_zip_entries,
    resolve_path_in_root,
)
from app.services.hub_file_service import PREVIEW_TEXT_LIMIT

# mimetypes 内建表不含 .log，Linux 上回退查系统 /etc/mime.types（精简镜像/CI 常缺），
# 会把脚本日志下载成 application/octet-stream——显式补齐保证跨平台一致
mimetypes.add_type("text/plain", ".log")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hub-files", tags=["anc-hub 文件"])


class HubFileQueryRequest(BaseModel):
    """anc-hub 文件检索请求（全部条件可选）"""
    name_like: Optional[str] = Field(None, max_length=200, description="文件名子串过滤")
    uploaded_after: Optional[str] = Field(None, max_length=64, description="上传时间下界（ISO8601）")
    uploaded_before: Optional[str] = Field(None, max_length=64, description="上传时间上界（ISO8601）")
    uploader: Optional[str] = Field(None, max_length=128, description="上传者过滤")
    page: Optional[int] = Field(None, ge=1, description="页码，从 1 开始")
    page_size: Optional[int] = Field(None, ge=1, le=200, description="每页条数")


@router.post("/query", summary="查询 anc-hub file-store 文件（远端浏览）")
def query_hub_files(body: HubFileQueryRequest, request: Request) -> Dict[str, Any]:
    """file_query 透传查询，返回服务端原始结果（分页/过滤由 anc-hub 决定）

    以配置的凭证调用 anc-hub（静态服务 token 优先，回退同域部署的平台
    登录态 Cookie），经 authz 验证后即为 file-store 的调用主体。
    """
    bearer_token = resolve_hub_bearer_token(request)
    if not bearer_token:
        raise HTTPException(
            status_code=401,
            detail="缺少 anc-hub 调用凭证（ANC_HUB_BEARER_TOKEN 或平台登录 Cookie），请检查部署配置",
        )
    try:
        return HubFileService(bearer_token=bearer_token).query_files(
            **body.model_dump(exclude_none=True)
        )
    except HubEndpointNotConfiguredError as e:
        logger.warning(f"[hub-files/query] 端点未配置: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except HubFileError as e:
        logger.warning(f"[hub-files/query] 查询失败: {e}")
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# 数据集迭代阶段D：文件树 / 预览 / 下载 / 匹配预览
# ---------------------------------------------------------------------------

def _require_unpacked_record(db: Session, file_id: str):
    """取登记记录并要求已有解包产物（未登记 404 / 无解包区 404，中文提示）"""
    try:
        record = get_hub_record(db, file_id)
    except HubFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if record is None:
        raise HTTPException(status_code=404, detail=f"执行包未登记: {file_id}（请先从 anc-hub 导入）")
    if record.unpack_status != "unpacked" or not record.unpack_dir or not os.path.isdir(record.unpack_dir):
        raise HTTPException(
            status_code=404,
            detail=f"执行包（file_id={file_id}）尚未解包完成或解包目录已被清理，请重新导入后再试",
        )
    return record


def _safe_member_path(unpack_dir: str, path: str) -> str:
    """防穿越统一入口（resolve_path_in_root 包装，中文 4xx）"""
    try:
        return resolve_path_in_root(unpack_dir, path)
    except HubFileError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _locate_member(record, path: str) -> str:
    """包内文件定位：先解包区，miss 时回退人工补传区（补传文件同样可预览/下载）

    两处都没有时返回解包区路径，由调用方按既有语义报 404。
    """
    unpack_path = _safe_member_path(record.unpack_dir, path)
    if os.path.isfile(unpack_path):
        return unpack_path
    supplement_path = _safe_member_path(_supplement_dir_for(record.file_id), path)
    if os.path.isfile(supplement_path):
        return supplement_path
    return unpack_path


@router.get("/{file_id}/tree", summary="获取执行包文件树（登记表索引+补传叠加，零文件 IO）")
def get_hub_file_tree(file_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """返回 file_index + supplement_index 叠加视图（补传条目带 is_manual 标记；未登记 404）"""
    try:
        record = get_hub_record(db, file_id)
    except HubFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if record is None:
        raise HTTPException(status_code=404, detail=f"执行包未登记: {file_id}（请先从 anc-hub 导入）")
    return {
        "file_id": file_id,
        "unpack_status": record.unpack_status,
        "file_index": merged_index(record),
    }


@router.get("/{file_id}/nested-tree", summary="嵌套 zip 内层清单（只读 zip 目录，不解压）")
def get_hub_file_nested_tree(
    file_id: str,
    path: str = Query(..., description="包内嵌套 zip 的相对路径"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """读取嵌套 zip 的 central directory 返回内层清单；path 非嵌套 zip 时 4xx 中文错误"""
    record = _require_unpacked_record(db, file_id)
    abs_path = _locate_member(record, path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail=f"包内不存在该文件: {path}")
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=400, detail=f"路径不是文件（不支持查看内层清单）: {path}")
    if os.path.splitext(abs_path)[1].lower() != ".zip":
        raise HTTPException(status_code=400, detail=f"仅嵌套 zip 支持查看内层清单: {path} 不是 zip 文件")
    try:
        entries = read_nested_zip_entries(abs_path)
    except HubFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"file_id": file_id, "path": path, "entries": entries}


@router.get(
    "/{file_id}/content",
    response_model=None,
    summary="预览包内文件（文本/图片；二进制请走 download）",
)
def get_hub_file_content(
    file_id: str,
    path: str = Query(..., description="包内文件相对路径"),
    db: Session = Depends(get_db),
) -> Union[Dict[str, Any], FileResponse]:
    """文本类返回 {content(截断2MB), mime, truncated}；图片 FileResponse 内联；二进制 4xx 提示走 download"""
    record = _require_unpacked_record(db, file_id)
    abs_path = _locate_member(record, path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"包内不存在该文件: {path}")

    kind, mime = preview_kind(path)
    if kind == "binary":
        raise HTTPException(
            status_code=400,
            detail=f"该文件类型不支持在线预览（{os.path.basename(path)}），请使用下载功能获取原文件",
        )
    if kind == "image":
        return FileResponse(abs_path, media_type=mime, content_disposition_type="inline")

    with open(abs_path, "rb") as f:
        chunk = f.read(PREVIEW_TEXT_LIMIT + 1)
    truncated = len(chunk) > PREVIEW_TEXT_LIMIT
    return {
        "file_id": file_id,
        "path": path,
        "mime": mime,
        "truncated": truncated,
        "size": os.path.getsize(abs_path),
        "content": chunk[:PREVIEW_TEXT_LIMIT].decode("utf-8", errors="replace"),
    }


@router.get("/{file_id}/download", summary="下载包内任意文件（中文文件名 URL 编码）")
def download_hub_file(
    file_id: str,
    path: str = Query(..., description="包内文件相对路径"),
    db: Session = Depends(get_db),
) -> FileResponse:
    """下载包内任意文件；Content-Disposition 与数据集模板下载同一中文编码模式"""
    record = _require_unpacked_record(db, file_id)
    abs_path = _locate_member(record, path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"包内不存在该文件: {path}")

    filename = os.path.basename(abs_path)
    encoded_filename = quote(filename)
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(
        abs_path,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename={encoded_filename}; filename*=UTF-8''{encoded_filename}"
            )
        },
    )


class HubMatchPreviewRequest(BaseModel):
    """匹配预览请求（pattern 与执行时 path_pattern 同语义）"""
    dataset_id: str = Field(..., min_length=1, description="数据集 ID")
    field_name: str = Field(..., min_length=1, description="hub_file 类型字段名")
    pattern: str = Field(..., min_length=1, description="文件匹配模式（pathlib glob 语义）")


@router.post("/match-preview", summary="匹配预览（与执行时同一 match_files 语义）")
def match_preview(body: HubMatchPreviewRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """取数据集首条含该 hub_file 字段样本的登记索引试匹配 pattern

    无样本数据时返回可展示的空态（matched_count=0、sample_data_index=None）。
    """
    try:
        return hub_match_preview(db, body.dataset_id, body.field_name, body.pattern)
    except HubRecordMissingError as e:
        logger.warning(f"[hub-files/match-preview] 执行包不可用: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except HubFileError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# 人工补传（执行包内容不足时人工补充文件；包级生效，同名覆盖）
# ---------------------------------------------------------------------------

def _current_user_code(request: Optional[Request]) -> Optional[str]:
    """从请求状态提取当前用户 user_code（与导入链路 _get_user_code 同手法）"""
    user = getattr(request, "state", None) and getattr(request.state, "user", None)
    if isinstance(user, dict):
        return user.get("user_code")
    if user is not None and hasattr(user, "user_code"):
        return user.user_code
    return None


@router.post("/{file_id}/supplements", summary="人工补传文件（散文件落指定子目录 / zip 自动解压）")
def upload_supplements(
    file_id: str,
    request: Request,
    files: List[UploadFile] = File(..., description="补传文件（.zip 自动解压保留目录结构，其余按散文件落 target_dir）"),
    relative_paths: List[str] = Form(default=[], description="与 files 一一对应的相对路径（仅散文件生效）"),
    # Annotated 形态保证直接调用（单测）时默认值为 ""，而 Form(default="") 的
    # Python 默认是 FieldInfo 对象，直调语义会失真（与 import_script_package 同坑）
    target_dir: Annotated[str, Form(description="散文件目标子目录（缺省包根；zip 按内部结构落盘不受影响）")] = "",
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """向执行包人工补传文件（同名覆盖并返回覆盖明细；未登记/未解包 404）

    同步端点：大文件流式落盘走 FastAPI 线程池，不阻塞事件循环。
    """
    _require_unpacked_record(db, file_id)
    file_items = [(f.filename or f"file_{i}", f.file) for i, f in enumerate(files)]
    try:
        return add_supplements(
            db,
            file_id,
            file_items,
            relative_paths=relative_paths or [],
            target_dir=target_dir,
            uploaded_by=_current_user_code(request),
        )
    except HubFileError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{file_id}/supplements", summary="删除人工补传文件（原始包文件只读）")
def delete_supplement_endpoint(
    file_id: str,
    path: str = Query(..., description="补传文件相对路径"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """删除单个人工补传文件（索引与实体同步清理；原始执行包内容 400 拒绝）"""
    try:
        return delete_supplement(db, file_id, path)
    except HubFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
