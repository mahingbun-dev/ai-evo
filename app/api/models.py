"""
请求与响应模型
"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Dict, List


# ========== Upload Excel ==========

class UploadExcelResponseModel(BaseModel):
    """上传 Excel 响应模型"""
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"id": 1, "filename": "用例执行结果-20260124.xlsx", "message": "上传成功"}]
        }
    )
    id: int = Field(description="文件 ID")
    filename: str = Field(description="上传的文件名")
    message: str = Field(description="操作结果提示")


# ========== Excel Data ==========

class ExcelDataRequestModel(BaseModel):
    """获取 Excel 数据请求模型（路径参数）"""
    model_config = ConfigDict(json_schema_extra={"examples": [{"file_id": 1}]})
    file_id: int = Field(..., description="Excel 文件 ID")


class ExcelDataResponseModel(BaseModel):
    """获取 Excel 数据响应模型"""
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "file_id": 1,
                    "filename": "用例执行结果-20260124.xlsx",
                    "rows": [
                        {"用例名称": "登录接口测试", "预期结果": "返回200", "实际结果": "通过"},
                        {"用例名称": "查询接口测试", "预期结果": "返回数据", "实际结果": "通过"},
                    ],
                }
            ]
        }
    )
    file_id: int = Field(description="文件 ID")
    filename: str = Field(description="文件名")
    rows: List[Dict[str, Any]] = Field(description="Excel 行数据列表")

