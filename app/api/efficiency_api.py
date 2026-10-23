"""提效统计API"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from app.services.access_control import ensure_super_admin
from app.services.audit_service import audited
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from app.db.session import get_db
from app.services.efficiency_service import EfficiencyService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/efficiency", tags=["提效统计"])


class UpdateManualHoursRequest(BaseModel):
    record_id: str = Field(..., description="执行记录ID")
    hours: float = Field(..., ge=0, description="人工执行预计耗时（小时）")


class TaskEfficiencyItem(BaseModel):
    execution_number: int
    estimated_manual_hours: Optional[float] = None
    system_hours: float
    efficiency_hours: float
    completed_at: Optional[str] = None


class TotalEfficiencyResponse(BaseModel):
    total_efficiency_hours: float
    task_count: int
    avg_efficiency_hours: float
    items: list


@router.put("/manual-hours",
            dependencies=[Depends(audited("UPDATE", "efficiency_manual_hours"))])
def update_manual_hours(body: UpdateManualHoursRequest, db: Session = Depends(get_db)):
    """更新人工预计耗时"""
    service = EfficiencyService(db)
    result = service.update_manual_hours(body.record_id, body.hours)
    if not result:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    return result


@router.get("/task/{task_id}", response_model=list[TaskEfficiencyItem], dependencies=[Depends(ensure_super_admin)])
def get_task_efficiency(task_id: str, db: Session = Depends(get_db)):
    """获取单任务各次执行提效详情"""
    service = EfficiencyService(db)
    return service.get_task_efficiency(task_id)


@router.get("/summary", response_model=TotalEfficiencyResponse, dependencies=[Depends(ensure_super_admin)])
def get_total_efficiency(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """获取总提效统计"""
    service = EfficiencyService(db)
    return service.get_total_efficiency(start_date=start_date, end_date=end_date)
