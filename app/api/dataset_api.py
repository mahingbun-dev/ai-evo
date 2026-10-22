"""
评测数据集API
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Form, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from app.db.session import get_db
from app.core.config import get_settings
from app.core import scopes
from app.core.scopes import require_scope
from app.services.access_control import (
    ensure_dataset_access,
    ensure_super_admin,
    filter_team_code,
    user_team_code,
)
from app.services.dataset_service import DatasetService, DatasetDataService
from app.services.dataset_field_service import DatasetFieldService
from app.services.dataset_export_service import (
    DatasetExportService,
    DatasetExportTooLargeError,
    MAX_EXPORT_ROWS,
)
from app.models.dataset_models import EvaluationDataset, ImportTempFile
from app.utils.read_limited import read_limited
from app.utils.secure_filename import secure_filename
import pandas as pd
import io
import json
import os
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# M1 QE1-5：数据集创建与导入进 API Key 路由注册表（default-deny 白名单）——
# CLI（aievo datasets upload）持 datasets:write scope 的 Key 建集并导入；
# 列表/详情等读端点暂不开放（保持攻击面最小），页面端点走 JWT 不受影响
scopes.register_api_key_route(
    "POST", "/api/v1/evaluation/datasets", scopes.DATASETS_WRITE)
scopes.register_api_key_route(
    "POST", "/api/v1/evaluation/datasets/{dataset_id}/import/excel",
    scopes.DATASETS_WRITE)
scopes.register_api_key_route(
    "POST", "/api/v1/evaluation/datasets/{dataset_id}/import/json",
    scopes.DATASETS_WRITE)


def _slim_item_data(data):
    """写接口响应瘦身：data 内超大字段替换为占位摘要（与列表接口 slim 口径一致）。

    add/update 返回的完整 data（如会话数据集的 session_messages，单条可达几十 MB）
    前端并不消费，整包返回会拖慢序列化与传输。
    """
    if not isinstance(data, dict):
        return data
    slimmed = {}
    for key, value in data.items():
        try:
            size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            size = 0
        if size > DatasetDataService.OVERSIZE_FIELD_BYTES:
            slimmed[key] = {"__oversize__": True, "size_bytes": size}
        else:
            slimmed[key] = value
    return slimmed


# ==================== Request Models ====================

class CreateDatasetRequest(BaseModel):
    name: str = Field(..., description="数据集名称")
    description: Optional[str] = Field(None, description="数据集描述")
    created_by: Optional[str] = Field(None, description="创建人")


class UpdateDatasetRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class AddDataRequest(BaseModel):
    # V2 扁平格式数据（推荐）
    data: Optional[Dict[str, Any]] = Field(None, description="V2扁平格式数据")
    # V1 兼容格式
    question: Optional[str] = Field(None, description="用户输入")
    ref_answer: Optional[str] = Field(None, description="参考答案")
    meta_data: Optional[Dict[str, Any]] = Field(None, description="元数据")


class ImportJsonRequest(BaseModel):
    data: List[Dict[str, Any]] = Field(..., description="评测数据列表")


# ==================== Response Models ====================

class DatasetResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    question_count: int
    has_media: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True

    @field_validator('created_at', 'updated_at', mode='before')
    @classmethod
    def datetime_to_str(cls, v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        return v

    @field_validator('has_media', mode='before')
    @classmethod
    def calculate_has_media(cls, v, info):
        """自动计算has_media字段"""
        if v is not None:
            return v
        # 如果是从数据库对象获取，检查是否有媒体文件
        if hasattr(info.model, '__dict__'):
            obj = info.model
            if hasattr(obj, 'id'):
                # 这里返回False，实际值会在服务层计算
                return False
        return False


class DataItemUpdateRequest(BaseModel):
    # V2 扁平格式数据（推荐）
    data: Optional[Dict[str, Any]] = Field(None, description="V2扁平格式数据")
    # V1 兼容格式（逐步废弃）
    question: Optional[str] = None
    ref_answer: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = None
    meta_data: Optional[Dict[str, Any]] = Field(None, description="元数据")


class FileResponse(BaseModel):
    id: str
    file_name: str
    file_type: str
    mime_type: Optional[str]
    file_size: int
    created_at: Optional[str] = None

    class Config:
        from_attributes = True

    @field_validator('created_at', mode='before')
    @classmethod
    def datetime_to_str(cls, v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        return v


class DataItemResponse(BaseModel):
    id: str
    index: int
    question: str = Field(..., validation_alias='question', serialization_alias='question')
    ref_answer: str = Field(..., validation_alias='ref_answer', serialization_alias='ref_answer')
    user_input_is_media: bool = Field(False, description="用户输入是否关联文件")
    answer_is_media: bool = Field(False, description="答案是否关联文件")
    meta_data: Optional[Dict[str, Any]] = Field(default=None, validation_alias='meta_data', serialization_alias='meta_data')

    model_config = ConfigDict(from_attributes=True)

    @field_validator('meta_data', mode='before')
    @classmethod
    def json_to_dict(cls, v):
        if v is None:
            return None
        # 如果是SQLAlchemy的JSON类型，需要特殊处理
        if hasattr(v, '__class__') and v.__class__.__name__ in ('InstrumentedList',):
            # InstrumentedList 需要转成普通 list
            return list(v)
        if hasattr(v, '__class__') and v.__class__.__name__ == 'dict':
            # dict 需要递归处理其中的 InstrumentedList
            def convert(obj):
                if obj is None:
                    return None
                if hasattr(obj, '__class__') and obj.__class__.__name__ == 'InstrumentedList':
                    return list(obj)
                if isinstance(obj, dict):
                    return {k: convert(val) for k, val in obj.items()}
                if isinstance(obj, list):
                    return [convert(item) for item in obj]
                return obj
            return convert(v)
        return v

    @model_validator(mode='before')
    @classmethod
    def extract_media_flags(cls, data):
        """从meta_data中提取user_input_is_media和answer_is_media字段"""
        # 如果是字典（Pydantic创建的临时对象），直接返回
        if isinstance(data, dict):
            return data

        # 从ORM对象中提取media flags
        if hasattr(data, 'meta_data'):
            meta = data.meta_data
            if meta is not None:
                # meta_data可能是字典或字符串
                meta_dict = meta
                if isinstance(meta, str):
                    import json
                    try:
                        meta_dict = json.loads(meta)
                    except Exception:
                        meta_dict = {}
                if isinstance(meta_dict, dict):
                    user_input_is_media = meta_dict.get('user_input_is_media', False)
                    answer_is_media = meta_dict.get('answer_is_media', False)
                    # 设置到对象上，这样field_validator可以获取到
                    data.user_input_is_media = user_input_is_media
                    data.answer_is_media = answer_is_media
        return data


class DataListResponse(BaseModel):
    dataset_id: str
    total: int
    items: List[DataItemResponse]


class DataItemResponseV2(BaseModel):
    """数据集数据项响应 V2（自由格式）"""
    id: str
    index: int
    data: Dict[str, Any] = Field(..., description="自由格式的完整数据对象")

    model_config = ConfigDict(from_attributes=True)


class DataListResponseV2(BaseModel):
    dataset_id: str
    total: int
    items: List[DataItemResponseV2]


class ImportResponse(BaseModel):
    count: int
    message: str


class DatasetFieldResponse(BaseModel):
    """数据集字段响应"""
    name: str = Field(..., description="字段名")
    label: str = Field(..., description="显示名")
    type: str = Field(default="string",
                      description="字段类型：string/number/boolean/json/hub_file"
                                  "（hub_file 表示字段值为 anc-hub file_id，评测执行时自动下载落盘）")
    is_media: bool = Field(default=False, description="是否为媒体字段")
    is_media_field: Optional[str] = Field(None, description="关联的is_media字段名")
    is_required: bool = Field(default=True, description="是否必填")
    position: int = Field(default=0, description="字段顺序")


class ImportPreviewResponseV2(BaseModel):
    """导入预览响应V2（支持动态字段）"""
    is_valid: bool
    total_rows: int
    valid_rows: int
    invalid_rows: int
    preview_id: str
    fields: List[DatasetFieldResponse] = Field(default_factory=list, description="字段结构")
    custom_fields_count: int = Field(default=0, description="自定义字段数量")
    total_fields_count: int = Field(default=0, description="总字段数量")
    items: List[Any] = Field(default_factory=list, description="预览数据项")


class ValidationErrorItem(BaseModel):
    """校验错误项"""
    row: int = Field(..., description="行号")
    field: str = Field(..., description="字段名")
    error: str = Field(..., description="错误信息")


# ==================== Router ====================

router = APIRouter(prefix="/evaluation/datasets", tags=["评测数据集"])


# 模板下载需要放在 /{dataset_id} 之前
@router.get("/download-template", summary="下载数据集模板")
def download_template():
    """下载Excel模板文件"""
    from urllib.parse import quote

    # 创建模板DataFrame
    template_data = {
        "user_input": [""],
        "user_input_is_media": ["否"],
        "answer": [""],
        "answer_is_media": ["否"]
    }
    df = pd.DataFrame(template_data)

    # 写入Excel到内存
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='数据集模板')

    output.seek(0)

    # 使用URL编码处理中文文件名
    filename = "数据集模板.xlsx"
    encoded_filename = quote(filename)

    return StreamingResponse(
        io.BytesIO(output.getvalue()),
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={
            'Content-Disposition': f"attachment; filename={encoded_filename}; filename*=UTF-8''{encoded_filename}"
        }
    )


@router.get("/{dataset_id}/export/excel", summary="导出数据集为Excel")
def export_dataset_excel(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """导出数据集全部有效数据为 Excel

    导出文件的表头使用字段名（field_name），媒体列为 OSS URL 原文，
    可直接再次导入，形成「导出 → 批量修改 → 回灌」闭环。
    """
    from urllib.parse import quote

    # W9：导出即数据外流，对象级归属校验（跨团队下载拒绝）
    dataset = ensure_dataset_access(db, request.state.user, dataset_id)

    export_service = DatasetExportService(db)

    # 提前做行数检查，给出比「构建完才发现太大」更明确的提示
    row_count = export_service.count_exportable_rows(dataset_id)
    if row_count > MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"数据集共 {row_count} 条数据，超过单次导出上限 {MAX_EXPORT_ROWS} 条，请拆分数据集后分批导出",
        )

    try:
        content = export_service.export_to_excel(dataset_id)
    except DatasetExportTooLargeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("数据集 %s 导出失败", dataset_id)
        raise HTTPException(status_code=500, detail=f"导出失败: {str(e)}")

    # 记录审计日志（导出即数据外流，需留痕）
    try:
        from app.services.audit_service import log_operation
        user = getattr(request.state, "user", None) or {}
        client_host = request.client.host if request.client else ""
        log_operation(
            user, "EXPORT", "dataset", dataset_id, dataset.name,
            detail={"row_count": row_count, "file_format": "xlsx"},
            ip_address=client_host,
        )
    except Exception as e:
        logger.warning(f"审计日志记录失败: {e}")

    # 文件名：数据集名 + 时间戳（去除文件名非法字符）
    safe_name = "".join(
        c for c in (dataset.name or "dataset") if c not in '\\/:*?"<>|\r\n\t'
    ).strip() or "dataset"
    filename = f"{safe_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    encoded_filename = quote(filename)

    return StreamingResponse(
        io.BytesIO(content),
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={
            'Content-Disposition': f"attachment; filename={encoded_filename}; filename*=UTF-8''{encoded_filename}"
        }
    )


@router.post("", response_model=DatasetResponse, summary="创建数据集",
             dependencies=[Depends(require_scope(scopes.DATASETS_WRITE))])
def create_dataset(
    body: CreateDatasetRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """创建新的评测数据集（W9：team_code 随用户团队落库，列表过滤/归属校验的归属锚点）"""
    service = DatasetService(db)
    try:
        dataset = service.create_dataset(
            name=body.name,
            description=body.description,
            created_by=body.created_by,
            team_code=user_team_code(request.state.user),
        )
        return dataset
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=List[DatasetResponse], summary="获取数据集列表")
def get_datasets(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """获取评测数据集列表（W9：超管看全部，普通用户只看本团队——修复根因
    「数据集列表连 team 过滤都没传」（dataset_api.py:393-402））"""
    service = DatasetService(db)
    datasets = service.get_datasets(
        skip=skip, limit=limit, team_code=filter_team_code(request.state.user)
    )
    return datasets


@router.get("/{dataset_id}", response_model=DatasetResponse, summary="获取数据集详情")
def get_dataset(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取评测数据集详情（W9：对象级归属校验）"""
    dataset = ensure_dataset_access(db, request.state.user, dataset_id)
    return dataset


@router.put("/{dataset_id}", response_model=DatasetResponse, summary="更新数据集")
def update_dataset(
    dataset_id: str,
    body: UpdateDatasetRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """更新数据集信息（W9：对象级归属校验）"""
    ensure_dataset_access(db, request.state.user, dataset_id)
    service = DatasetService(db)
    dataset = service.update_dataset(dataset_id, **body.model_dump(exclude_unset=True))
    if not dataset:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return dataset


@router.delete("/{dataset_id}", summary="删除数据集")
def delete_dataset(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """删除数据集（W9：对象级归属校验）"""
    ensure_dataset_access(db, request.state.user, dataset_id)
    service = DatasetService(db)
    try:
        success = service.delete_dataset(dataset_id)
    except ValueError as e:
        # 有关联的评测任务，不能删除
        raise HTTPException(status_code=400, detail=str(e))
    if not success:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return {"message": "删除成功"}


@router.get("/{dataset_id}/fields", response_model=List[DatasetFieldResponse], summary="获取数据集字段结构")
def get_dataset_fields(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取数据集的字段结构（含自定义字段；W9：对象级归属校验）"""
    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    # 获取字段元数据
    field_service = DatasetFieldService(db)
    fields = field_service.get_field_metadata(dataset_id)

    return fields


@router.put("/{dataset_id}/fields", summary="更新数据集字段元数据")
def update_dataset_fields(
    dataset_id: str,
    fields: List[DatasetFieldResponse],
    request: Request,
    db: Session = Depends(get_db),
):
    """更新数据集的字段元数据（W9：对象级归属校验）"""
    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    # 保存字段元数据
    field_service = DatasetFieldService(db)
    field_service.save_field_metadata(
        dataset_id,
        [f.model_dump() for f in fields]
    )

    return {"message": "更新成功"}


@router.post("/cleanup-duplicates", summary="清理重复数据集")
def cleanup_duplicates(
    request: Request,
    db: Session = Depends(get_db),
):
    """清理重复的数据集（保留最早创建的）

    W19b：仅超管——去重范围跨全部团队且联动释放他团 hub 引用，属破坏性
    批量写，不随调用者团队收窄。
    """
    ensure_super_admin(request)
    from sqlalchemy import func
    from app.models.dataset_models import EvaluationDataset

    # 先查询所有有重复名称的名称列表
    duplicate_names = db.query(EvaluationDataset.name).filter(
        EvaluationDataset.is_active == True
    ).group_by(
        EvaluationDataset.name
    ).having(
        func.count(EvaluationDataset.id) > 1
    ).all()

    if not duplicate_names:
        return {"message": "没有重复的数据集", "deleted_count": 0}

    duplicate_name_set = {item[0] for item in duplicate_names}

    # 对每个重复名称，找出最早创建的那个，保留它
    to_delete = []
    for name in duplicate_name_set:
        # 找出这个名称下最早创建的数据集
        earliest = db.query(EvaluationDataset).filter(
            EvaluationDataset.name == name,
            EvaluationDataset.is_active == True
        ).order_by(EvaluationDataset.created_at.asc()).first()

        if earliest:
            # 找出这个名称下其他的数据集（除了最早的那个）
            others = db.query(EvaluationDataset).filter(
                EvaluationDataset.name == name,
                EvaluationDataset.is_active == True,
                EvaluationDataset.id != earliest.id
            ).all()
            to_delete.extend(others)

    deleted_count = len(to_delete)

    # 软删除重复的数据集
    for dataset in to_delete:
        dataset.is_active = False

    db.commit()

    # 数据集迭代阶段C：被清理的数据集同样联动释放执行包引用（与 delete_dataset 同语义）
    for dataset in to_delete:
        try:
            from app.services.hub_file_service import release_dataset_hub_refs
            release_dataset_hub_refs(db, dataset.id)
        except Exception as e:
            logger.warning(f"清理重复数据集时释放执行包引用失败: {dataset.id}, 错误: {e}")

    return {
        "message": f"清理完成，删除了 {deleted_count} 条重复记录",
        "deleted_count": deleted_count
    }


@router.post("/{dataset_id}/import/excel", response_model=ImportResponse, summary="从Excel导入数据",
             dependencies=[Depends(require_scope(scopes.DATASETS_WRITE))])
async def import_from_excel(
    dataset_id: str,
    request: Request,
    file: UploadFile = File(..., description="Excel文件"),
    db: Session = Depends(get_db),
):
    """从Excel文件导入评测数据（W9：对象级归属校验）"""
    # W9：数据集归属校验
    ensure_dataset_access(db, request.state.user, dataset_id)
    # 保存上传的文件
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        service = DatasetService(db)
        count = service.import_from_excel(dataset_id, tmp_path)
        return {"count": count, "message": f"成功导入 {count} 条数据"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/{dataset_id}/import/json", response_model=ImportResponse, summary="从JSON导入数据",
             dependencies=[Depends(require_scope(scopes.DATASETS_WRITE))])
def import_from_json(
    dataset_id: str,
    body: ImportJsonRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """从JSON导入评测数据（W9：对象级归属校验）"""
    ensure_dataset_access(db, request.state.user, dataset_id)
    service = DatasetService(db)
    count = service.import_from_json(dataset_id, body.data)
    return {"count": count, "message": f"成功导入 {count} 条数据"}


# 数据项文件相关路由 - 需要放在通用路由 /data 之前
@router.post("/{dataset_id}/data/{data_id}/upload-file", response_model=FileResponse, summary="上传数据项关联文件")
async def upload_data_file(
    dataset_id: str,
    data_id: str,
    request: Request,
    field: str = Query("answer", description="字段名：user_input 或 answer"),
    file: UploadFile = File(..., description="上传的文件"),
    db: Session = Depends(get_db),
):
    """上传文件并与具体数据项关联（W9：数据集归属校验）"""
    # 校验field参数
    if field not in ["user_input", "answer"]:
        field = "answer"

    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    try:
        # 上传文件并建立与数据项的关联
        result = DatasetService(db).upload_data_file(dataset_id, data_id, file, field)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{dataset_id}/data/{data_id}/files", summary="获取数据项关联文件列表")
def get_data_files(
    dataset_id: str,
    data_id: str,
    request: Request,
    field: str = Query(None, description="字段名：user_input 或 answer，不传则返回全部"),
    db: Session = Depends(get_db),
):
    """获取与具体数据项关联的文件列表（W9：数据集归属校验）"""
    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    # 验证数据项存在
    data_service = DatasetDataService(db)
    data_item = data_service.get_data(data_id)
    if not data_item:
        raise HTTPException(status_code=404, detail="数据项不存在")

    files = data_service.get_data_files(data_id, field)
    return files


@router.delete("/{dataset_id}/data/{data_id}/files/{file_id}", summary="删除数据项关联文件")
def delete_data_file(
    dataset_id: str,
    data_id: str,
    file_id: str,
    request: Request,
    field: str = Query("answer", description="字段名：user_input 或 answer"),
    db: Session = Depends(get_db),
):
    """删除与具体数据项关联的文件（W9：原实现仅按 data_id 直删、无数据集校验，
    现补齐数据集存在性 + 团队归属校验）"""
    # 校验field参数
    if field not in ["user_input", "answer"]:
        field = "answer"

    # W9：数据集存在 + 团队归属（原实现完全绕过数据集校验）
    ensure_dataset_access(db, request.state.user, dataset_id)

    data_service = DatasetDataService(db)
    success = data_service.delete_data_file(data_id, file_id, field)
    if not success:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"message": "删除成功"}


# 通用数据路由 - 放在具体路由之后
@router.get("/{dataset_id}/data", response_model=DataListResponse, summary="获取数据集题目列表")
def get_dataset_data(
    dataset_id: str,
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """获取评测数据列表（从 V2 表读取，构造 V1 兼容格式；W9：数据集归属校验）"""
    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    data_service = DatasetDataService(db)
    # V1 兼容格式只取 user_input/answer/_is_media/files 等小字段，
    # 用 DB 端瘦身查询避免整行加载（会话数据集单条可达几十 MB）
    v2_items = data_service.get_data_list_v2_slim(dataset_id, skip=skip, limit=limit)

    # 构造 V1 兼容格式
    items = []
    for item in v2_items:
        # get_data_list_v2_slim 返回 dict（mappings()），按下标访问
        data_json = item.get("data") or {}
        meta_data = {}
        # 提取 *_is_media 字段到 meta_data
        for key, value in data_json.items():
            if key.endswith("_is_media"):
                meta_data[key] = value
        # 提取 files 相关字段到 meta_data
        for key in ("user_input_files", "answer_files", "files"):
            if key in data_json:
                meta_data[key] = data_json[key]

        items.append({
            "id": item.get("id"),
            "index": item.get("index"),
            "question": data_json.get("user_input", ""),
            "ref_answer": data_json.get("answer", ""),
            "user_input_is_media": data_json.get("user_input_is_media", False),
            "answer_is_media": data_json.get("answer_is_media", False),
            "meta_data": meta_data if meta_data else None,
        })

    return {
        "dataset_id": dataset_id,
        "total": len(items),
        "items": items
    }


@router.get("/{dataset_id}/data/v2", response_model=DataListResponseV2, summary="获取数据集题目列表 V2（自由格式）")
def get_dataset_data_v2(
    dataset_id: str,
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """获取评测数据列表 V2（返回 data JSON 对象；W9：数据集归属校验）"""
    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    data_service = DatasetDataService(db)
    # 列表展示走 DB 端瘦身：超大字段（如会话数据集的 session_messages，单条可达
    # 几十 MB）替换为占位摘要，避免全量传输/反序列化拖慢预览接口
    items = data_service.get_data_list_v2_slim(dataset_id, skip=skip, limit=limit)

    return {
        "dataset_id": dataset_id,
        "total": len(items),
        "items": items
    }


@router.post("/{dataset_id}/data", summary="添加评测数据")
def add_data(
    dataset_id: str,
    body: AddDataRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """添加单条评测数据（W9：数据集归属校验）"""
    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    data_service = DatasetDataService(db)
    body_dict = body.model_dump(exclude_unset=True)

    # 判断是 V1 还是 V2 格式
    if 'data' in body_dict and body_dict['data'] is not None:
        # V2 格式
        data = data_service.add_data_v2(
            dataset_id=dataset_id,
            data=body_dict['data'],
        )
        return DataItemResponseV2(
            id=data.id,
            index=data.index,
            data=_slim_item_data(data.data)
        )
    else:
        # V1 格式（兼容）
        data = data_service.add_data(
            dataset_id=dataset_id,
            question=body_dict.get('question', ''),
            ref_answer=body_dict.get('ref_answer', ''),
            meta_data=body_dict.get('meta_data'),
        )
        return data


@router.delete("/data/{data_id}", summary="删除评测数据")
def delete_data(
    data_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """删除评测数据（W9：原实现按 data_id 直删无归属校验，现经所属数据集团队校验）"""
    data_service = DatasetDataService(db)
    data_item = data_service.get_data(data_id)
    if not data_item:
        raise HTTPException(status_code=404, detail="数据不存在")
    # W9：经数据项所属数据集做团队归属校验
    ensure_dataset_access(db, request.state.user, data_item.dataset_id)
    success = data_service.delete_data(data_id)
    if not success:
        raise HTTPException(status_code=404, detail="数据不存在")
    return {"message": "删除成功"}


@router.put("/{dataset_id}/data/{data_id}", summary="更新评测数据")
def update_data(
    dataset_id: str,
    data_id: str,
    body: DataItemUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """更新单条评测数据（W9：数据集归属校验）"""
    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    data_service = DatasetDataService(db)

    # 判断是 V1 还是 V2 格式更新
    request_body = body.model_dump(exclude_unset=True)
    is_v2 = 'data' in request_body

    if is_v2:
        # V2 格式更新
        data = data_service.update_data(data_id, **request_body)
        if not data:
            raise HTTPException(status_code=404, detail="数据不存在")
        # 返回 V2 格式响应（超大字段占位摘要，前端不消费完整 data）
        return DataItemResponseV2(
            id=data.id,
            index=data.index,
            data=_slim_item_data(data.data)
        )
    else:
        # V1 格式更新（兼容）
        data = data_service.update_data(data_id, **request_body)
        if not data:
            raise HTTPException(status_code=404, detail="数据不存在")
        return data


# ==================== 模板下载与预览校验 ====================

# 临时文件存储（内存中，生产环境应使用Redis或数据库）
_temp_files: Dict[str, List[Dict[str, Any]]] = {}

# 预览数据缓存（不创建数据集，只缓存预览数据）
_preview_cache: Dict[str, Dict[str, Any]] = {}


class DataPreviewItem(BaseModel):
    """预览数据项"""
    row_num: int  # Excel行号（从1开始，用于错误定位）
    index: int = 0  # 数据索引（用于临时文件关联）
    user_input: str
    user_input_is_media: bool = False
    answer: str
    answer_is_media: bool = False
    is_valid: bool = True
    errors: List[str] = Field(default_factory=list)
    user_input_temp_files: List[Dict[str, Any]] = Field(default_factory=list, description="用户输入关联的临时文件列表")
    answer_temp_files: List[Dict[str, Any]] = Field(default_factory=list, description="答案关联的临时文件列表")


class ImportPreviewResponse(BaseModel):
    """导入预览响应"""
    is_valid: bool
    total_rows: int
    valid_rows: int
    invalid_rows: int
    preview_id: str
    items: List[DataPreviewItem]


@router.post("/{dataset_id}/import/preview", response_model=ImportPreviewResponse, summary="预览并校验导入数据")
async def preview_import(
    dataset_id: str,
    file: UploadFile = File(..., description="Excel文件"),
    db: Session = Depends(get_db),
):
    """预览Excel数据并进行校验，返回校验结果（不创建数据集）"""
    # 不再验证数据集是否存在，使用 dataset_id 作为预览缓存的 key
    # dataset_id 可以是任意值，确认导入时会创建真正的数据集

    # 保存上传的文件
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # 读取Excel（默认只读取第一个sheet）
        df = pd.read_excel(tmp_path, sheet_name=0)

        # 检查必要字段
        required_columns = ["user_input", "answer"]
        missing = [col for col in required_columns if col not in df.columns]
        if missing:
            raise HTTPException(status_code=400, detail=f"Excel缺少必要字段: {missing}")

        # 校验每一行
        items = []
        invalid_count = 0

        for idx, row in df.iterrows():
            row_num = idx + 2  # Excel行号（1是表头，2开始是数据）
            errors = []

            # 必填校验
            user_input = str(row.get("user_input", "")).strip() if pd.notna(row.get("user_input")) else ""
            answer = str(row.get("answer", "")).strip() if pd.notna(row.get("answer")) else ""

            # user_input_is_media字段处理（新增，兼容旧版本默认为否）
            user_input_is_media = False
            if "user_input_is_media" in row:
                val = str(row.get("user_input_is_media", "")).strip().lower()
                if val in ["是", "yes", "true", "1"]:
                    import re as _re
                    from app.services.oss_media_cache_service import get_oss_url_pattern_str
                    _OSS_URL_PATTERN = get_oss_url_pattern_str()
                    user_input_is_media = True
                    # user_input关联文件时，user_input字段不能为空
                    if not user_input:
                        errors.append(f"第{row_num}行：user_input关联文件时，user_input字段不能为空")
                    # 校验 user_input 字段值是否符合 OSS URL 格式
                    elif not _re.match(_OSS_URL_PATTERN, user_input):
                        errors.append(f"第{row_num}行：user_input_is_media为是，请先将文件上传到 OSS（字段值需符合 hashDownload URL 格式）")
                elif val in ["否", "no", "false", "0", ""]:
                    user_input_is_media = False
                    if not user_input:
                        errors.append(f"第{row_num}行：user_input（用户输入）不能为空")
                else:
                    errors.append(f"第{row_num}行：user_input_is_media（用户输入是否关联文件）值无效，请填是或否")
            else:
                # 兼容旧版本：字段不存在时默认为否
                if not user_input:
                    errors.append(f"第{row_num}行：user_input（用户输入）不能为空")

            # answer_is_media字段处理
            answer_is_media = False
            if "answer_is_media" in row:
                val = str(row.get("answer_is_media", "")).strip().lower()
                if val in ["是", "yes", "true", "1"]:
                    import re as _re
                    from app.services.oss_media_cache_service import get_oss_url_pattern_str
                    _OSS_URL_PATTERN = get_oss_url_pattern_str()
                    answer_is_media = True
                    # answer关联文件时，answer字段不能为空
                    if not answer:
                        errors.append(f"第{row_num}行：answer关联文件时，answer字段不能为空")
                    # 校验 answer 字段值是否符合 OSS URL 格式
                    elif not _re.match(_OSS_URL_PATTERN, answer):
                        errors.append(f"第{row_num}行：answer_is_media为是，请先将文件上传到 OSS（字段值需符合 hashDownload URL 格式）")
                elif val in ["否", "no", "false", "0", ""]:
                    answer_is_media = False
                    if not answer:
                        errors.append(f"第{row_num}行：answer（答案）不能为空")
                else:
                    errors.append(f"第{row_num}行：answer_is_media（答案是否关联文件）值无效，请填是或否")
            else:
                # 兼容旧版本：字段不存在时默认为否
                if not answer:
                    errors.append(f"第{row_num}行：answer（答案）不能为空")

            is_valid = len(errors) == 0
            if not is_valid:
                invalid_count += 1

            # 获取该数据对应的临时文件（两个字段分开获取）- 优先从数据库读取
            try:
                user_input_temp_files = db.query(ImportTempFile).filter(
                    ImportTempFile.preview_id == dataset_id,
                    ImportTempFile.data_index == idx,
                    ImportTempFile.field == "user_input",
                    ImportTempFile.is_active == True
                ).all()
                user_input_temp_files = [{
                    "id": f.id,
                    "file_name": f.file_name,
                    "file_type": f.file_type,
                    "file_size": f.file_size,
                    "file_path": f.file_path
                } for f in user_input_temp_files]

                answer_temp_files = db.query(ImportTempFile).filter(
                    ImportTempFile.preview_id == dataset_id,
                    ImportTempFile.data_index == idx,
                    ImportTempFile.field == "answer",
                    ImportTempFile.is_active == True
                ).all()
                answer_temp_files = [{
                    "id": f.id,
                    "file_name": f.file_name,
                    "file_type": f.file_type,
                    "file_size": f.file_size,
                    "file_path": f.file_path
                } for f in answer_temp_files]
            except Exception as e:
                # 兼容旧逻辑：从内存读取
                logger.warning(f"Failed to read temp files from database in preview: {str(e)}")
                user_input_temp_files = _temp_files.get(f"{dataset_id}_{idx}_user_input", [])
                answer_temp_files = _temp_files.get(f"{dataset_id}_{idx}_answer", [])

            items.append(DataPreviewItem(
                row_num=row_num,
                index=idx,  # 数据索引
                user_input=user_input,
                user_input_is_media=user_input_is_media,
                answer=answer,
                answer_is_media=answer_is_media,
                is_valid=is_valid,
                errors=errors,
                user_input_temp_files=user_input_temp_files,
                answer_temp_files=answer_temp_files
            ))

        return ImportPreviewResponse(
            is_valid=invalid_count == 0,
            total_rows=len(items),
            valid_rows=len(items) - invalid_count,
            invalid_rows=invalid_count,
            preview_id=dataset_id,  # 返回 preview_id 供后续使用
            items=items
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取Excel失败: {str(e)}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/{dataset_id}/import/preview/v2", response_model=ImportPreviewResponseV2, summary="预览并校验导入数据V2（支持动态字段）")
async def preview_import_v2(
    dataset_id: str,
    file: UploadFile = File(..., description="Excel文件"),
    db: Session = Depends(get_db),
):
    """预览Excel数据并进行校验，返回校验结果（含动态字段结构）"""
    # 保存上传的文件
    import tempfile
    import os
    import re as _re

    # OSS URL 校验正则
    from app.services.oss_media_cache_service import get_oss_url_pattern_str

    # OSS URL 校验正则（未配置 OSS_BASE_URL 时为空串，校验直接报未配置）
    _OSS_URL_PATTERN = get_oss_url_pattern_str()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # 解析Excel获取字段结构
        field_service = DatasetFieldService(db)
        fields_result = field_service.parse_fields_from_excel(tmp_path)

        main_fields = fields_result.get("fields", [])
        custom_fields_count = fields_result.get("custom_fields_count", 0)

        # 检查自定义字段数量限制
        if custom_fields_count > field_service.MAX_CUSTOM_FIELDS:
            raise HTTPException(
                status_code=400,
                detail=f"自定义字段数量超过限制：{custom_fields_count} > {field_service.MAX_CUSTOM_FIELDS}"
            )

        # 读取Excel
        df = pd.read_excel(tmp_path, sheet_name=0)
        # 清理列名中的前后空格
        df.columns = [str(col).strip() for col in df.columns.tolist()]

        # 构建字段名到字段信息的映射
        field_map = {f["name"]: f for f in main_fields}

        # 校验每一行
        items = []
        invalid_count = 0
        errors_list = []

        for idx, row in df.iterrows():
            row_num = idx + 2  # Excel行号（1是表头，2开始是数据）
            row_errors = []

            # 检查是否为空行（所有主字段值都为空）
            has_any_value = False
            for col in df.columns:
                if col.endswith("_is_media"):
                    continue
                if pd.notna(row.get(col)) and str(row.get(col)).strip():
                    has_any_value = True
                    break
            if not has_any_value:
                continue  # 跳过空行

            # 遍历所有字段进行校验
            for col in df.columns:
                if col.endswith("_is_media"):
                    continue  # 跳过 is_media 辅助字段

                field_info = field_map.get(col)
                if not field_info:
                    continue  # 跳过未知字段

                value = row.get(col)
                value_str = str(value).strip() if pd.notna(value) else ""
                is_media_field = col + "_is_media"

                # 检查 is_media 值
                is_media = False
                if is_media_field in df.columns:
                    media_val = str(row.get(is_media_field, "")).strip().lower()
                    is_media = media_val in ["是", "yes", "true", "1"]

                # 必填校验
                if field_info.get("is_required", False) and not value_str:
                    row_errors.append(f"第{row_num}行{col}：{field_info.get('label', col)}不能为空")
                    continue

                # 如果是 media 字段，校验 URL 格式
                if is_media and value_str:
                    if not _OSS_URL_PATTERN:
                        row_errors.append(f"第{row_num}行{col}：OSS 服务未配置（OSS_BASE_URL），无法使用媒体字段")
                    elif not _re.match(_OSS_URL_PATTERN, value_str):
                        row_errors.append(f"第{row_num}行{col}：{field_info.get('label', col)}为媒体文件，请先上传到OSS（字段值需符合 hashDownload URL 格式）")

            is_valid = len(row_errors) == 0
            if not is_valid:
                invalid_count += 1
                errors_list.extend([{"row": row_num, "field": col, "error": err} for err in row_errors])

            # 构建行的完整数据
            row_data = {"row_num": row_num, "index": idx, "is_valid": is_valid, "errors": row_errors}

            # 添加固定字段
            row_data["user_input"] = str(row.get("user_input", "")).strip() if pd.notna(row.get("user_input")) else ""
            row_data["user_input_is_media"] = field_map.get("user_input", {}).get("is_media", False)
            row_data["answer"] = str(row.get("answer", "")).strip() if pd.notna(row.get("answer")) else ""
            row_data["answer_is_media"] = field_map.get("answer", {}).get("is_media", False)

            # 添加所有非 is_media 的主字段
            for field in main_fields:
                if not field["name"].endswith("_is_media"):
                    field_name = field["name"]
                    row_data[field_name] = str(row.get(field_name, "")).strip() if pd.notna(row.get(field_name)) else ""
                    is_media_f = field_name + "_is_media"
                    if is_media_f in df.columns:
                        row_data[is_media_f] = field.get("is_media", False)
                        logger.info(f"[PREVIEW] field={field_name}, is_media_f={is_media_f}, is_media_value={field.get('is_media', False)}")

            items.append(row_data)
            if idx == 0:
                logger.info(f"[PREVIEW] first row_data keys: {list(row_data.keys())}, answer_带附件_is_media={row_data.get('answer_带附件_is_media')}")

        # 转换为 FieldResponse 格式
        field_responses = [
            DatasetFieldResponse(
                name=f["name"],
                label=f.get("label", f["name"]),
                type=f.get("type", "string"),
                is_media=f.get("is_media", False),
                is_media_field=f.get("is_media_field"),
                is_required=f.get("is_required", True),
                position=f.get("position", 0)
            )
            for f in main_fields
        ]

        # 调试日志
        logger.info(f"[PREVIEW] returning fields count: {len(field_responses)}, items count: {len(items)}")
        if items:
            logger.info(f"[PREVIEW] first item keys: {list(items[0].keys())}")
        logger.info(f"[PREVIEW] field_responses: {[(f.name, f.label, f.is_media) for f in field_responses]}")

        return ImportPreviewResponseV2(
            is_valid=invalid_count == 0,
            total_rows=len(items),
            valid_rows=len(items) - invalid_count,
            invalid_rows=invalid_count,
            preview_id=dataset_id,
            fields=field_responses,
            custom_fields_count=custom_fields_count,
            total_fields_count=len(main_fields),
            items=items
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取Excel失败: {str(e)}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/{dataset_id}/import/confirm", response_model=ImportResponse, summary="确认导入数据")
async def confirm_import(
    dataset_id: str,
    request: Request,
    name: str = Form(''),
    description: str = Form(''),
    real_dataset_id: str = Form('', description="追加模式下的真实数据集ID"),
    is_update: bool = Form(False, description="是否更新已存在的数据"),
    oss_overrides: str = Form('', description="OSS直传URL覆盖，JSON格式：{行索引: {user_input?: url, answer?: url}}"),
    file: UploadFile = File(..., description="Excel文件"),
    db: Session = Depends(get_db),
):
    """确认导入校验后的数据（W9：追加模式目标数据集归属校验；新建数据集随用户团队落库）"""
    import tempfile
    import os

    # W9：归属校验（新建模式无目标数据集可校验；追加模式必须先过团队归属）
    is_preview_id = dataset_id.startswith('preview_') or dataset_id.startswith('import_preview_')
    has_name = bool(name and name.strip())
    if not (is_preview_id and has_name):
        ensure_dataset_access(db, request.state.user, real_dataset_id or dataset_id)

    # 保存上传的文件
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        import json as _json
        oss_overrides_dict = {}
        if oss_overrides:
            try:
                oss_overrides_dict = {int(k): v for k, v in _json.loads(oss_overrides).items()}
            except Exception:
                pass

        dataset_service = DatasetService(db)

        # 判断是新建还是追加模式
        # 新建模式：dataset_id是预览ID且name有值
        # 追加模式：real_dataset_id有值
        is_preview_id = dataset_id.startswith('preview_') or dataset_id.startswith('import_preview_')
        has_name = bool(name and name.strip())

        if is_preview_id and has_name:
            # 创建新数据集（W9：随用户团队落库）
            dataset = dataset_service.create_dataset(
                name=name or file.filename.replace('.xlsx', '').replace('.xls', ''),
                description=description or '从Excel导入',
                team_code=user_team_code(request.state.user),
            )
            real_dataset_id = dataset.id

            # 保存字段元数据（新建数据集时）
            try:
                field_service = DatasetFieldService(db)
                fields_result = field_service.parse_fields_from_excel(tmp_path)
                main_fields = fields_result.get("fields", [])
                if main_fields:
                    field_service.save_field_metadata(real_dataset_id, main_fields)
                    logger.info(f"保存字段元数据成功: dataset_id={real_dataset_id}, fields_count={len(main_fields)}")
            except Exception as e:
                logger.warning(f"保存字段元数据失败: {str(e)}")
        elif real_dataset_id:
            # 追加模式：使用传递的真实数据集ID
            real_dataset_id = real_dataset_id
        else:
            # 追加模式（兼容旧逻辑）：直接使用dataset_id
            real_dataset_id = dataset_id

        # 保存字段元数据（新建或追加模式都应该保存）
        # 合并保存：excel 列只覆盖本次列，保留此前会话/hub 导入回填的字段
        # （session_file_id(hub_file) 等），否则全删重建会导致执行包「文件」
        # 按钮与评测 hub 通道失效（与会话/执行包导入的合并语义一致）
        try:
            field_service = DatasetFieldService(db)
            fields_result = field_service.parse_fields_from_excel(tmp_path)
            main_fields = fields_result.get("fields", [])
            if main_fields:
                field_service.save_field_metadata_merged(real_dataset_id, main_fields)
                logger.info(f"保存字段元数据成功: dataset_id={real_dataset_id}, fields_count={len(main_fields)}")
        except Exception as e:
            logger.warning(f"保存字段元数据失败: {str(e)}")

        # 导入数据
        # 获取导入前的最大index
        from sqlalchemy import func
        from app.models.dataset_models import EvaluationDataV2 as EvaluationDataV2Model
        max_index = db.query(func.max(EvaluationDataV2Model.index)).filter(
            EvaluationDataV2Model.dataset_id == real_dataset_id,
            EvaluationDataV2Model.is_active == True
        ).scalar() or -1

        # 读取Excel（只读取第一个sheet），按行索引与数据库数据项关联
        df = pd.read_excel(tmp_path, sheet_name=0)

        count = dataset_service.import_from_excel(real_dataset_id, tmp_path, is_update=is_update)

        # 获取所有相关的 V2 数据项
        if is_update:
            # 更新模式：获取该数据集所有活跃数据，按index排序
            data_items = db.query(EvaluationDataV2Model).filter(
                EvaluationDataV2Model.dataset_id == real_dataset_id,
                EvaluationDataV2Model.is_active == True
            ).order_by(EvaluationDataV2Model.index).all()
        else:
            # 新建模式：获取新导入的数据（从max_index+1开始）
            data_items = db.query(EvaluationDataV2Model).filter(
                EvaluationDataV2Model.dataset_id == real_dataset_id,
                EvaluationDataV2Model.is_active == True,
                EvaluationDataV2Model.index > max_index
            ).order_by(EvaluationDataV2Model.index).all()

        # 处理临时文件，将临时文件移动到正式存储
        preview_id = dataset_id

        # 建立index到data_item的映射（使用数据库中的index字段，更可靠）
        index_to_item = {item.index: item for item in data_items}

        from sqlalchemy.orm.attributes import flag_modified

        # 迭代Excel行索引，与oss_overrides和data_item进行关联
        for idx in range(len(df)):
            # 找到对应的数据项（按 Excel 行索引匹配数据库中的 index 字段）
            data_item = index_to_item.get(idx)
            logger.info(f"[CONFIRM_IMPORT] idx={idx}, data_item found={data_item is not None}")
            if not data_item:
                continue

            # 获取当前 V2 data JSON
            data_json = dict(data_item.data) if data_item.data else {}
            user_input_files = data_json.get('user_input_files', [])
            answer_files = data_json.get('answer_files', [])

            # 用于收集多字段文件信息的字典
            multi_field_files = {}

            def move_temp_file_to_upload_dir(temp_file, field_prefix):
                """移动临时文件到正式目录，返回文件信息"""
                upload_dir = "./uploads/datasets"
                os.makedirs(upload_dir, exist_ok=True)
                upload_path = os.path.join(upload_dir, f"{data_item.id}_{field_prefix}_{temp_file.file_name if hasattr(temp_file, 'file_name') else temp_file['file_name']}")
                src_path = temp_file.file_path if hasattr(temp_file, 'file_path') else temp_file['file_path']
                if os.path.exists(src_path):
                    os.rename(src_path, upload_path)
                return {
                    "id": temp_file.id if hasattr(temp_file, 'id') else temp_file.get('id', ''),
                    "file_name": temp_file.file_name if hasattr(temp_file, 'file_name') else temp_file['file_name'],
                    "file_path": upload_path,
                    "file_type": temp_file.file_type if hasattr(temp_file, 'file_type') else temp_file['file_type'],
                    "file_size": temp_file.file_size if hasattr(temp_file, 'file_size') else temp_file['file_size']
                }

            # 优先从数据库获取临时文件
            try:
                # 查询所有相关字段的临时文件（包括 user_input, answer 及其多字段扩展）
                all_temp_files = db.query(ImportTempFile).filter(
                    ImportTempFile.preview_id == preview_id,
                    ImportTempFile.data_index == idx,
                    ImportTempFile.is_active == True,
                    (
                        (ImportTempFile.field == "user_input") |
                        (ImportTempFile.field == "answer") |
                        (ImportTempFile.field.like("user_input_%")) |
                        (ImportTempFile.field.like("answer_%"))
                    )
                ).all()

                for temp_file in all_temp_files:
                    file_info = move_temp_file_to_upload_dir(temp_file, temp_file.field)
                    if temp_file.field == "user_input":
                        user_input_files.append(file_info)
                    elif temp_file.field == "answer":
                        answer_files.append(file_info)
                    else:
                        # 多字段文件（如 answer_带附件）
                        if temp_file.field not in multi_field_files:
                            multi_field_files[temp_file.field] = []
                        multi_field_files[temp_file.field].append(file_info)
                    # 标记为已处理（逻辑删除）
                    temp_file.is_active = False

            except Exception as e:
                # 如果数据库读取失败，尝试从内存中读取（兼容旧逻辑）
                logger.warning(f"Failed to read temp files from database: {str(e)}")

                # 从内存中读取所有相关临时文件
                for key, temp_files in list(_temp_files.items()):
                    if not key.startswith(f"{preview_id}_{idx}_"):
                        continue
                    field = key.split("_")[-2] + "_" + key.split("_")[-1]  # 提取 field 名
                    for temp_file in temp_files:
                        try:
                            file_info = move_temp_file_to_upload_dir(temp_file, field)
                            if field == "user_input":
                                user_input_files.append(file_info)
                            elif field == "answer":
                                answer_files.append(file_info)
                            else:
                                if field not in multi_field_files:
                                    multi_field_files[field] = []
                                multi_field_files[field].append(file_info)
                        except Exception:
                            pass
                    # 清理临时文件记录
                    del _temp_files[key]

            # 更新 V2 data JSON 中的 files
            data_json['user_input_files'] = user_input_files
            data_json['answer_files'] = answer_files
            # 添加多字段文件信息
            for field_name, files_list in multi_field_files.items():
                data_json[f'{field_name}_files'] = files_list
            data_json['files'] = answer_files  # 保持向后兼容

            # 应用 OSS 直传 URL 覆盖：用前端上传后的 OSS URL 替换原始字段值
            oss_override = oss_overrides_dict.get(idx, {})
            logger.info(f"[CONFIRM_IMPORT] idx={idx}, oss_override={oss_override}")
            if oss_override.get("user_input"):
                data_json["user_input"] = oss_override["user_input"]
                data_json["user_input_is_media"] = True
            if oss_override.get("answer"):
                data_json["answer"] = oss_override["answer"]
                data_json["answer_is_media"] = True
            # 处理多字段的 OSS URL 覆盖（如 answer_带附件）
            for field_name, field_value in oss_override.items():
                if field_name in ["user_input", "answer"]:
                    continue  # 已处理
                logger.info(f"[CONFIRM_IMPORT] 处理多字段: field_name={field_name}, field_value={field_value}")
                # 设置多字段的 is_media 标志
                data_json[f"{field_name}_is_media"] = True
                # 直接设置多字段值
                data_json[field_name] = field_value

            # 更新 V2 data JSON
            data_item.data = data_json
            flag_modified(data_item, "data")

        db.commit()

        # 清理所有以该preview_id开头的临时文件记录（从数据库）
        try:
            db.query(ImportTempFile).filter(
                ImportTempFile.preview_id == preview_id,
                ImportTempFile.is_active == True
            ).update({"is_active": False})
            db.commit()
        except Exception as e:
            logger.warning(f"Failed to clean temp files from database: {str(e)}")

        # 清理内存中的临时文件记录（兼容旧逻辑）
        keys_to_delete = [k for k in _temp_files.keys() if k.startswith(f"{preview_id}_")]
        for key in keys_to_delete:
            del _temp_files[key]

        return {"count": count, "message": f"成功导入 {count} 条数据"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/{dataset_id}/import/preview/upload-temp-file", response_model=Dict[str, Any], summary="预览阶段上传临时文件")
async def upload_temp_file(
    dataset_id: str,
    request: Request,
    data_index: int = Query(..., description="数据项索引（从0开始）"),
    field: str = Query("answer", description="字段名：user_input 或 answer"),
    file: UploadFile = File(..., description="上传的文件"),
    db: Session = Depends(get_db),
):
    """在预览阶段上传临时文件，与数据项索引和字段关联（持久化存储到数据库）"""
    import uuid
    import logging
    logger = logging.getLogger(__name__)

    # W9：dataset_id 为真实数据集 ID（追加模式预览）时校验团队归属；
    # preview_ 前缀是新建数据集预览的临时缓存 key（confirm_import 同口径），无资源可校验
    if not (dataset_id.startswith("preview_") or dataset_id.startswith("import_preview_")):
        ensure_dataset_access(db, request.state.user, dataset_id)

    # 校验field参数 - 支持多字段名（如 answer_带附件、answer_不带附件）
    _allowed_prefixes = ["user_input", "answer"]
    if not any(field.startswith(prefix) for prefix in _allowed_prefixes):
        field = "answer"  # 默认值

    # 读取文件内容（W1-1：超限即抛 413，媒体与临时上传共用 UPLOAD_MAX_BYTES）
    content = await read_limited(file, get_settings().UPLOAD_MAX_BYTES)
    file_size = len(content)

    # 生成临时文件ID
    temp_file_id = f"temp_{uuid.uuid4().hex[:12]}"

    # 文件名清洗：剥目录成分/控制字符，防路径穿越写（W1-1）
    safe_name = secure_filename(file.filename)

    # 保存文件到临时目录
    temp_dir = "./uploads/datasets/temp"
    os.makedirs(temp_dir, exist_ok=True)
    file_path = os.path.join(temp_dir, f"{temp_file_id}_{safe_name}")

    Path(file_path).write_bytes(content)

    # 获取文件类型（扩展支持音频、图片、表格）
    def get_file_type(filename):
        ext = os.path.splitext(filename)[1].lower()
        type_map = {
            # 表格
            ".xlsx": "excel", ".xls": "excel", ".csv": "csv",
            # 文档
            ".json": "json", ".txt": "text", ".pdf": "pdf",
            ".doc": "doc", ".docx": "docx", ".md": "markdown",
            # 图片
            ".jpg": "image", ".jpeg": "image", ".png": "image",
            ".gif": "image", ".bmp": "image", ".webp": "image",
            # 音频
            ".mp3": "audio", ".wav": "audio", ".aac": "audio",
            ".flac": "audio", ".ogg": "audio", ".m4a": "audio"
        }
        return type_map.get(ext, "other")

    # 保存到数据库（持久化存储）
    try:
        temp_file = ImportTempFile(
            id=temp_file_id,
            preview_id=dataset_id,
            data_index=data_index,
            field=field,
            file_name=safe_name,
            file_path=file_path,
            file_type=get_file_type(safe_name),
            file_size=file_size,
            is_active=True
        )
        db.add(temp_file)
        db.commit()
        logger.info(f"[UPLOAD_TEMP_FILE] Saved to database: temp_file_id={temp_file_id}, preview_id={dataset_id}, data_index={data_index}, field={field}")
    except Exception as e:
        db.rollback()
        logger.error(f"[UPLOAD_TEMP_FILE] Failed to save to database: {str(e)}")
        # 如果数据库保存失败，回退到内存存储（兼容旧逻辑）
        file_info = {
            "id": temp_file_id,
            "file_name": safe_name,
            "file_type": get_file_type(safe_name),
            "file_size": file_size,
            "file_path": file_path,
            "field": field
        }
        key = f"{dataset_id}_{data_index}_{field}"
        if key not in _temp_files:
            _temp_files[key] = []
        _temp_files[key].append(file_info)
        logger.warning(f"[UPLOAD_TEMP_FILE] Fallback to memory storage: key={key}")

    return {
        "id": temp_file_id,
        "file_name": safe_name,
        "file_type": get_file_type(safe_name),
        "file_size": file_size,
    }


@router.delete("/{dataset_id}/import/preview/temp-file/{temp_file_id}", summary="删除预览阶段上传的临时文件")
async def delete_temp_file(
    dataset_id: str,
    temp_file_id: str,
    request: Request,
    data_index: int = Query(..., description="数据项索引（从0开始）"),
    field: str = Query("answer", description="字段名：user_input 或 answer"),
    db: Session = Depends(get_db),
):
    """删除预览阶段上传的临时文件（从数据库中删除；W9：真实数据集 ID 时校验归属）"""
    import logging
    logger = logging.getLogger(__name__)

    # W9：同 upload_temp_file 口径——真实数据集 ID 校验归属，preview_ 前缀为临时 key
    if not (dataset_id.startswith("preview_") or dataset_id.startswith("import_preview_")):
        ensure_dataset_access(db, request.state.user, dataset_id)

    # 校验field参数
    if field not in ["user_input", "answer"]:
        field = "answer"  # 默认值

    logger.info(f"[DELETE_TEMP_FILE] dataset_id={dataset_id}, data_index={data_index}, field={field}, temp_file_id={temp_file_id}")

    # 优先从数据库删除
    try:
        temp_file = db.query(ImportTempFile).filter(
            ImportTempFile.id == temp_file_id,
            ImportTempFile.preview_id == dataset_id,
            ImportTempFile.is_active == True
        ).first()

        if temp_file:
            # 删除物理文件
            if os.path.exists(temp_file.file_path):
                try:
                    os.remove(temp_file.file_path)
                    logger.info(f"[DELETE_TEMP_FILE] file removed from disk: {temp_file.file_path}")
                except Exception as e:
                    logger.error(f"[DELETE_TEMP_FILE] failed to remove file: {e}")

            # 逻辑删除（标记为非活跃）
            temp_file.is_active = False
            db.commit()
            logger.info(f"[DELETE_TEMP_FILE] file deleted from database: {temp_file_id}")
            return {"message": "删除成功"}
    except Exception as e:
        logger.error(f"[DELETE_TEMP_FILE] database error: {str(e)}")
        db.rollback()

    # 如果数据库中没有，尝试从内存中删除（兼容旧逻辑）
    key = f"{dataset_id}_{data_index}_{field}"
    if key in _temp_files:
        files = _temp_files[key]
        for i, f in enumerate(files):
            if f["id"] == temp_file_id:
                # 删除物理文件
                file_path = f.get("file_path", "")
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        logger.error(f"[DELETE_TEMP_FILE] failed to remove file: {e}")
                # 从列表中移除
                files.pop(i)
                return {"message": "删除成功"}

    logger.warning(f"[DELETE_TEMP_FILE] file not found: {temp_file_id}")
    raise HTTPException(status_code=404, detail="临时文件不存在")


# ==================== 会话文件导入（长会话 Agent 评测） ====================

class SessionImportPreviewItem(BaseModel):
    """会话导入预览项"""
    file_name: str = Field(..., description="文件名（多会话文件带 #序号）")
    source: str = Field("", description="会话来源：claude_code / langfuse")
    session_id: str = Field("", description="会话 ID")
    summary: str = Field("", description="首条用户消息摘要")
    turns: int = Field(0, description="对话轮次")
    message_count: int = Field(0, description="消息数")
    tool_call_count: int = Field(0, description="工具调用数")
    started_at: Optional[str] = Field(None, description="会话开始时间")
    ended_at: Optional[str] = Field(None, description="会话结束时间")
    valid: bool = Field(True, description="是否可导入")
    error: str = Field("", description="解析错误信息")


class SessionImportPreviewResponse(BaseModel):
    """会话导入预览响应"""
    is_valid: bool
    total_sessions: int
    valid_sessions: int
    invalid_sessions: int
    items: List[SessionImportPreviewItem] = Field(default_factory=list)


class SessionImportResponse(BaseModel):
    dataset_id: str
    count: int
    message: str


class HubSessionImportRequest(BaseModel):
    """从 anc-hub 拉取会话文件导入请求"""
    file_ids: List[str] = Field(..., description="anc-hub file-store 文件 ID 列表")
    name: str = Field("", description="新建数据集名称")
    description: str = Field("", description="数据集描述")
    real_dataset_id: str = Field("", description="追加模式下的真实数据集ID")


def _read_session_uploads(files: List[UploadFile]) -> List[Tuple[str, str]]:
    """将上传文件落盘临时文件，返回 [(file_name, tmp_path)]"""
    import os as _os

    from app.services.session_dataset_service import save_upload_to_temp

    entries: List[Tuple[str, str]] = []
    try:
        for f in files:
            if not f.filename:
                continue
            content = f.file.read()
            entries.append((f.filename, save_upload_to_temp(content, f.filename)))
    except Exception:
        for _, path in entries:
            try:
                _os.remove(path)
            except OSError:
                pass
        raise
    return entries


def _cleanup_session_uploads(entries: List[Tuple[str, str]]):
    import os as _os

    for _, path in entries:
        try:
            _os.remove(path)
        except OSError:
            pass


@router.post(
    "/import/sessions/preview",
    response_model=SessionImportPreviewResponse,
    summary="会话文件导入预览",
)
async def preview_session_import(
    files: List[UploadFile] = File(..., description="会话文件（.jsonl / Langfuse 导出 .json），支持多文件"),
):
    """解析 Claude Code jsonl / Langfuse trace 文件，返回会话级预览（不落库）"""
    from app.services.session_dataset_service import preview_session_files

    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个会话文件")

    entries = _read_session_uploads(files)
    try:
        result = preview_session_files(entries)
        return SessionImportPreviewResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"会话文件预览失败: {e}")
    finally:
        _cleanup_session_uploads(entries)


@router.post(
    "/import/sessions/confirm",
    response_model=SessionImportResponse,
    summary="确认导入会话数据",
)
async def confirm_session_import(
    name: str = Form('', description="新建数据集名称"),
    description: str = Form(''),
    real_dataset_id: str = Form('', description="追加模式下的真实数据集ID"),
    files: List[UploadFile] = File(..., description="会话文件（与预览相同的多文件）"),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """会话文件落库：一个会话 = 一条数据（含完整结构化 session_messages）"""
    import os as _os

    from app.services.session_dataset_service import import_sessions

    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个会话文件")

    user = getattr(request, "state", None) and getattr(request.state, "user", None)
    user_code = None
    if isinstance(user, dict):
        user_code = user.get("user_code")
    elif user is not None and hasattr(user, "user_code"):
        user_code = user.user_code

    # W9：追加模式目标数据集归属校验（与 confirm_import 同口径；新建无资源可校验）
    if real_dataset_id:
        ensure_dataset_access(db, user, real_dataset_id)

    entries = _read_session_uploads(files)
    try:
        result = import_sessions(
            db,
            entries,
            name=name,
            description=description,
            real_dataset_id=real_dataset_id or None,
            created_by=user_code,
            team_code=user_team_code(user),
        )
        return SessionImportResponse(
            dataset_id=result["dataset_id"],
            count=result["count"],
            message=f"成功导入 {result['count']} 个会话",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"会话文件导入失败: {e}")
        raise HTTPException(status_code=500, detail=f"会话文件导入失败: {e}")
    finally:
        _cleanup_session_uploads(entries)


def _discard_fresh_materializations(db, freshly_materialized, import_completed: bool) -> None:
    """导入校验/执行失败时回滚本次新物化且无引用的执行包登记（best-effort）

    import_completed 之后数据行已落库并持有引用，绝不回滚——否则数据行的
    hub_file 通道引用悬空，评测执行与文件抽屉将报「执行包未登记」。
    """
    if import_completed or not freshly_materialized:
        return
    from app.services.hub_file_service import discard_unpacked_if_unreferenced
    for fid in freshly_materialized:
        try:
            discard_unpacked_if_unreferenced(db, fid)
        except Exception as cleanup_err:
            logger.warning(f"anc-hub 会话导入回滚清理失败: file_id={fid} 错误: {cleanup_err}")


@router.post(
    "/import/sessions/from-hub",
    response_model=SessionImportResponse,
    summary="从 anc-hub 拉取会话文件并导入",
)
async def import_sessions_from_hub(
    body: HubSessionImportRequest,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """按勾选的 anc-hub file_id 拉取会话文件并导入

    先 ensure_unpacked 把执行包物化到持久解包区（幂等：登记表命中零下载），
    再从解包产物中选取会话文件导入——数据行以 session_file_id（hub_file
    通道）引用原始文件，文件抽屉预览与评测执行 resolve_hub_file_fields
    只认登记表 + 持久解包区，导入时必须完成登记并持有引用。

    anc-hub 下载得到的是服务端打包的 zip。会话文件选取规则：
    - 打包内含嵌套 claude_thread.zip（claude-thread.zip，固定规则：会话
      jsonl 必然在其中）→ 只从该嵌套 zip 内的 .jsonl / .json 导入，
      外层其他文件（environment.json 等）全部忽略
    - 无嵌套 thread zip（单条目 jsonl zip / 裸文件）→ 直接按外层
      .jsonl / .json 条目导入
    导入的每条会话数据自动记录原始 file_id（session_file_id /
    session_file_name），供需要原始文件的任务以 hub_file 通道直接引用；
    导入成功后按数据行粒度 acquire_ref（与软删释放严格成对）。

    W9：追加模式目标数据集归属校验（同 confirm_session_import 口径）。
    """
    import logging as _logging
    import os as _os
    import re as _re
    import shutil as _shutil
    import tempfile as _tempfile

    from app.services.hub_file_service import (
        HubEndpointNotConfiguredError,
        HubFileError,
        HubFileService,
        acquire_ref,
        ensure_unpacked,
        resolve_hub_bearer_token,
        release_ref,
        unpack_zip,
    )
    from app.services.session_dataset_service import ALLOWED_SESSION_EXTENSIONS
    from app.services.session_dataset_service import import_sessions as import_sessions_impl

    _logger = _logging.getLogger(__name__)
    # 嵌套会话包命名兼容连字符/下划线两种变体（实测 claude-thread.zip，
    # 业务口径 claude_thread.zip），basename 匹配、不区分大小写
    _THREAD_ZIP_PATTERN = _re.compile(r"claude[-_]thread.*\.zip$")

    def _pick_session_candidates(
        files: List[Dict[str, Any]], nested_work_root: str
    ) -> List[Dict[str, Any]]:
        """从解压产物中选出会话文件条目（嵌套 thread zip 优先）"""
        def _basename(path: str) -> str:
            return _os.path.basename(path.replace("\\", "/")).lower()

        thread_zips = [f for f in files if _THREAD_ZIP_PATTERN.search(_basename(f["rel_path"]))]
        if not thread_zips:
            # 回退：单条目 jsonl zip / 裸文件，直接按扩展名过滤
            return [f for f in files if f["rel_path"].lower().endswith(ALLOWED_SESSION_EXTENSIONS)]
        if len(thread_zips) > 1:
            _logger.warning(
                "发现多个 claude_thread.zip，仅使用第一个: %s",
                [f["rel_path"] for f in thread_zips],
            )
        chosen = thread_zips[0]
        # 解压嵌套 zip 到临时工作目录（随 workdir 一并清理）：源文件在持久解包区，
        # 不得写回污染登记索引与抽屉文件树
        stem = _os.path.splitext(_os.path.basename(chosen["path"]))[0] or "thread"
        nested_dir = _os.path.join(nested_work_root, f"{stem}_unpacked")
        nested_files = unpack_zip(chosen["path"], nested_dir)
        return [
            f for f in nested_files
            if f["rel_path"].lower().endswith(ALLOWED_SESSION_EXTENSIONS)
        ]

    file_ids = list(dict.fromkeys(fid.strip() for fid in (body.file_ids or []) if fid and fid.strip()))
    if not file_ids:
        raise HTTPException(status_code=400, detail="请至少提供一个 anc-hub file_id")

    user = getattr(request, "state", None) and getattr(request.state, "user", None)
    user_code = None
    if isinstance(user, dict):
        user_code = user.get("user_code")
    elif user is not None and hasattr(user, "user_code"):
        user_code = user.user_code

    # W9：追加模式目标数据集归属校验（与 confirm_session_import 同口径）
    if body.real_dataset_id:
        ensure_dataset_access(db, user, body.real_dataset_id)

    workdir = _tempfile.mkdtemp(prefix="hub_session_import_")
    # 本次调用中新物化（cached=False）的 file_id：校验/导入失败时回滚清理，
    # 避免留下无引用的登记记录；导入成功后不得清理（数据行已引用）
    freshly_materialized: List[str] = []
    import_completed = False
    try:
        # 以配置的凭证下载 anc-hub（静态服务 token 优先，回退同域部署的
        # 平台登录态 Cookie；file-store 需要登录态，匿名会被登录闸门拒绝）
        bearer_token = resolve_hub_bearer_token(request)
        hub_service = HubFileService(bearer_token=bearer_token)
        # entries 三元组：(file_name, tmp_path, file_id)——第三项为条目唯一键
        entries: List[tuple] = []
        # file_extra 以 file_id（条目唯一键）为键：同名文件属于不同 file_id 时，
        # import_sessions 按 entries 三元组的第三项归属 extra，不会按文件名互相覆盖
        file_extra: Dict[str, Dict[str, Any]] = {}
        for fid in file_ids:
            # 物化到持久解包区（幂等，登记表命中零下载）：数据行以
            # session_file_id(hub_file 通道) 引用原始文件，抽屉与评测执行
            # 只认登记表 + uploads/hub_datasets/{file_id}/
            info = ensure_unpacked(db, fid, service=hub_service)
            if not info.get("cached"):
                freshly_materialized.append(fid)
            unpack_dir = info["unpack_dir"]
            files = [
                {
                    "path": _os.path.join(unpack_dir, *f["path"].split("/")),
                    "rel_path": f["path"],
                    "size": f.get("size") or 0,
                }
                for f in (info.get("file_index") or [])
            ]
            # 会话文件选取：嵌套 claude_thread.zip 优先（固定规则），外层干扰
            # 文件（environment.json 等）不会被当作会话
            session_files = _pick_session_candidates(files, workdir)
            if not session_files:
                raise ValueError(
                    f"文件 {info.get('filename') or fid} 内未找到会话文件（.jsonl / .json）"
                )
            for f in session_files:
                # entries 三元组：第三项为条目唯一键（file_id）
                entries.append((_os.path.basename(f["rel_path"]), f["path"], fid))
            file_extra[fid] = {
                "session_file_id": fid,
                "session_file_name": info.get("filename") if len(session_files) > 1 else _os.path.basename(session_files[0]["rel_path"]),
            }

        result = import_sessions_impl(
            db,
            entries,
            name=body.name,
            description=body.description,
            real_dataset_id=body.real_dataset_id or None,
            created_by=user_code,
            file_extra=file_extra,
            team_code=user_team_code(request.state.user),
        )
        import_completed = True
        # 引用计数与释放端（release_dataset_hub_refs 逐行多重释放）严格成对：
        # 本次导入创建的每一行对其 session_file_id 持一次引用；中途失败补偿释放。
        # 失败归 500（本地引用计数问题，非 anc-hub 上游错误），故转 RuntimeError
        # 避免被下方 HubFileError 分支误映射为 502
        acquired: List[str] = []
        try:
            for ref_fid in (result.get("hub_file_refs") or []):
                acquire_ref(db, ref_fid)
                acquired.append(ref_fid)
        except Exception as e:
            for done_fid in acquired:
                try:
                    release_ref(db, done_fid)
                except Exception as re_err:
                    logger.warning(f"anc-hub 会话导入补偿释放引用失败: file_id={done_fid} 错误: {re_err}")
            raise RuntimeError(f"会话导入引用计数失败（已补偿释放 {len(acquired)} 个）: {e}") from e
        return SessionImportResponse(
            dataset_id=result["dataset_id"],
            count=result["count"],
            message=f"成功导入 {result['count']} 个会话",
        )
    except HubEndpointNotConfiguredError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except HubFileError as e:
        logger.warning(f"anc-hub 文件拉取失败: {e}")
        _discard_fresh_materializations(db, freshly_materialized, import_completed)
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        _discard_fresh_materializations(db, freshly_materialized, import_completed)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"anc-hub 会话导入失败: {e}")
        _discard_fresh_materializations(db, freshly_materialized, import_completed)
        raise HTTPException(status_code=500, detail=f"anc-hub 会话导入失败: {e}")
    finally:
        _shutil.rmtree(workdir, ignore_errors=True)
