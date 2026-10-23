"""周覆盖度统计API"""
import logging
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from app.services.access_control import ensure_super_admin
from app.services.audit_service import audited
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from app.db.session import get_db
from app.services.coverage_service import CoverageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/coverage", tags=["周覆盖度"], dependencies=[Depends(ensure_super_admin)])


class ConfigResponse(BaseModel):
    id: str
    week_start_day: str
    week_end_day: str


class UpdateConfigRequest(BaseModel):
    week_start_day: str = Field(..., description="周起始日（如 monday）")
    week_end_day: str = Field(..., description="周结束日（如 sunday）")


class PlanItem(BaseModel):
    id: str
    week_start_date: str
    week_end_date: str
    planned_count: int
    locked: bool


class PlanListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list


class UpsertPlanRequest(BaseModel):
    week_start_date: str = Field(..., description="周起始日期 YYYY-MM-DD")
    week_end_date: str = Field(..., description="周结束日期 YYYY-MM-DD")
    planned_count: int = Field(..., ge=1, description="本周计划任务数")


class CoverageItem(BaseModel):
    id: str
    week_start_date: str
    week_end_date: str
    planned_count: int
    actual_count: int
    coverage_rate: float


@router.get("/config", response_model=ConfigResponse)
def get_config(db: Session = Depends(get_db)):
    """获取周配置"""
    service = CoverageService(db)
    config = service.get_config()
    if not config:
        return ConfigResponse(id="", week_start_day="monday", week_end_day="sunday")
    return config


@router.put("/config", response_model=ConfigResponse,
            dependencies=[Depends(audited("UPDATE", "coverage_config"))])
def update_config(body: UpdateConfigRequest, db: Session = Depends(get_db)):
    """更新周配置"""
    service = CoverageService(db)
    try:
        result = service.update_config(body.week_start_day, body.week_end_day)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/plans", response_model=PlanListResponse)
def get_plans(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """获取周计划列表"""
    service = CoverageService(db)
    return service.get_plans(page=page, page_size=page_size)


@router.post("/plans", response_model=PlanItem,
             dependencies=[Depends(audited("CREATE", "coverage_plan"))])
def upsert_plan(body: UpsertPlanRequest, db: Session = Depends(get_db)):
    """创建/更新周计划"""
    service = CoverageService(db)
    try:
        result = service.upsert_plan(
            week_start_date=date.fromisoformat(body.week_start_date),
            week_end_date=date.fromisoformat(body.week_end_date),
            planned_count=body.planned_count,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/plans/{plan_id}",
               dependencies=[Depends(audited("DELETE", "coverage_plan", id_param="plan_id"))])
def delete_plan(plan_id: str, db: Session = Depends(get_db)):
    """删除周计划（仅未锁定周）"""
    service = CoverageService(db)
    try:
        success = service.delete_plan(plan_id)
        if not success:
            raise HTTPException(status_code=404, detail="周计划不存在")
        return {"message": "删除成功"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stats", response_model=list[CoverageItem])
def get_coverage(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """获取覆盖率数据"""
    service = CoverageService(db)
    return service.get_coverage(start_date=start_date, end_date=end_date)
