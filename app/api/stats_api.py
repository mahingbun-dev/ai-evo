"""任务执行统计API"""
import logging
from fastapi import APIRouter, Depends, Query
from app.services.access_control import ensure_super_admin
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime
from app.db.session import get_db
from app.services.stats_service import StatsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["统计"], dependencies=[Depends(ensure_super_admin)])


class RecordItem(BaseModel):
    id: str
    task_id: str
    task_name: Optional[str] = None
    evaluation_type: str
    model_config_id: Optional[str] = None
    dataset_id: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    created_at: datetime


class RecordListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[RecordItem]


class SummaryResponse(BaseModel):
    total_count: int
    success_count: int
    failed_count: int
    success_rate: float
    avg_duration_ms: int
    by_type: Optional[dict] = None


class TrendItem(BaseModel):
    date: str
    count: int
    success_rate: float
    avg_duration_ms: int


class TrendResponse(BaseModel):
    granularity: str
    data: List[TrendItem]


# 周报归档（M5 QE5-3）：每周一 08:00 聚合上周覆盖度/提效/门禁通过率的
# markdown 报告，归档可回看（生成与推送在 api 进程周期任务内完成）
class WeeklyReportItem(BaseModel):
    id: str
    week_start: str
    week_end: str
    pushed_webhooks: int
    email_status: Optional[str] = None
    created_at: Optional[datetime] = None


class WeeklyReportListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[WeeklyReportItem]


class WeeklyReportDetailResponse(BaseModel):
    id: str
    week_start: str
    week_end: str
    content: str
    payload: Optional[dict] = None
    created_at: Optional[datetime] = None


@router.get("/records", response_model=RecordListResponse)
def get_records(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    evaluation_type: Optional[str] = Query(None, description="任务类型"),
    status: Optional[str] = Query(None, description="状态"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """获取执行记录列表"""
    service = StatsService(db)
    result = service.get_records(
        start_date=start_date,
        end_date=end_date,
        evaluation_type=evaluation_type,
        status=status,
        page=page,
        page_size=page_size,
    )
    return result


@router.get("/summary", response_model=SummaryResponse)
def get_summary(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    evaluation_type: Optional[str] = Query(None, description="任务类型"),
    group_by: Optional[str] = Query(None, description="分组字段"),
    db: Session = Depends(get_db),
):
    """获取汇总统计"""
    service = StatsService(db)
    result = service.get_summary(
        start_date=start_date,
        end_date=end_date,
        evaluation_type=evaluation_type,
        group_by=group_by,
    )
    return result


@router.get("/trends", response_model=TrendResponse)
def get_trends(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    evaluation_type: Optional[str] = Query(None, description="任务类型"),
    granularity: str = Query("day", description="聚合粒度：day/week/month"),
    db: Session = Depends(get_db),
):
    """获取耗时趋势"""
    service = StatsService(db)
    result = service.get_trends(
        start_date=start_date,
        end_date=end_date,
        evaluation_type=evaluation_type,
        granularity=granularity,
    )
    return result


@router.get("/weekly-reports", response_model=WeeklyReportListResponse)
def list_weekly_reports(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """周报归档列表（M5 QE5-3，新在前）"""
    from app.services.weekly_report_service import list_reports
    return list_reports(db, page=page, page_size=page_size)


@router.get("/weekly-reports/{report_id}", response_model=WeeklyReportDetailResponse)
def get_weekly_report(
    report_id: str,
    db: Session = Depends(get_db),
):
    """单份周报正文（markdown 原文，前端查看弹窗渲染）"""
    from app.services.weekly_report_service import get_report_content
    report = get_report_content(db, report_id)
    if report is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="周报不存在")
    return report


# 裁判-人工一致性报告（M8 QE8-1）：人工金分标注（Results 标注模式）与裁判
# 聚合分的相关性度量——Pearson/Spearman（scipy）+ 分带 Cohen's kappa
# （得分率 <60%/80% 分低/中/高带，按各任务 max_score 归一），作为裁判
# 可信度证据（销售物料）。task_id 为空=全任务聚合（附 by_task 摘要）。
@router.get("/judge-agreement", summary="裁判-人工一致性报告")
def get_judge_agreement(
    task_id: Optional[str] = Query(None, description="任务ID（空=全任务聚合）"),
    execution_number: Optional[int] = Query(None, description="执行序号（指定任务时可选锁定单次执行）"),
    db: Session = Depends(get_db),
):
    """裁判-人工一致性报告（M8 QE8-1）

    样本口径：status=completed 且金分非空的标注 × 行裁判聚合分；裁判分缺失
    行（judge_failed）单独计数（excluded_no_judge）不计入样本。
    """
    from app.services.annotation_service import judge_agreement_report
    return judge_agreement_report(db, task_id=task_id, execution_number=execution_number)