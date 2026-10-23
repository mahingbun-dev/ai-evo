"""
API接口
保留Excel接口，移除老式pytest和直接评测接口
"""
from fastapi import APIRouter, Depends, Request, UploadFile, File, HTTPException, Path
from app.services.access_control import ensure_super_admin
from app.services.audit_service import audited
from sqlalchemy.orm import Session
from typing import Optional
from app.db.session import get_db
from app.services.excel_service import ExcelService
from app.api.models import (
    UploadExcelResponseModel,
    ExcelDataRequestModel,
    ExcelDataResponseModel,
)
from app.core.config import Settings, get_settings


def _get_excel_data_params(file_id: int = Path(..., description="Excel 文件 ID")) -> ExcelDataRequestModel:
    return ExcelDataRequestModel(file_id=file_id)


router = APIRouter(dependencies=[Depends(ensure_super_admin)])


# =============================================================================
# Excel接口
# =============================================================================

@router.post(
    "/upload-excel",
    response_model=UploadExcelResponseModel,
    tags=["Excel"],
    summary="上传 Excel 文件",
    description="上传 Excel 文件并解析后存入数据库，支持 .xlsx 和 .xls 格式。",
    dependencies=[Depends(audited("CREATE", "excel_file"))],
)
async def upload_excel(
    file: UploadFile = File(..., description="要上传的 Excel 文件（.xlsx 或 .xls 格式）"),
    request: Request = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings)
):
    service = ExcelService(db)
    try:
        db_file = await service.upload_and_save(file)
        if request is not None:  # QE4-4：暂存新资源标识供 audited 依赖落审计
            request.state.audit_resource = {"id": db_file.id, "name": db_file.filename}
        return UploadExcelResponseModel(id=db_file.id, filename=db_file.filename, message="上传成功")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析失败: {str(e)}")


@router.get(
    "/excel-data/{file_id}",
    response_model=ExcelDataResponseModel,
    tags=["Excel"],
    summary="读取 Excel 数据",
    description="根据文件 ID 读取已存储的 Excel 数据，返回所有行内容。",
)
async def get_excel_data(
    params: ExcelDataRequestModel = Depends(_get_excel_data_params),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings)
):
    service = ExcelService(db)
    filename, data = service.get_file_data(params.file_id)
    return ExcelDataResponseModel(file_id=params.file_id, filename=filename, rows=[row.data for row in data])
