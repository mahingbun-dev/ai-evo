"""任务执行统计服务"""
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, Integer, cast, Date
from app.models.stats_models import TaskExecutionStats
from app.models.task_models import EvaluationTask

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now():
    return datetime.now(BEIJING_TZ)


class StatsService:
    """统计服务"""

    def __init__(self, db: Session):
        self.db = db

    def create_record(self, task_id: str, evaluation_type: str, model_config_id: Optional[str] = None,
                      dataset_id: Optional[str] = None) -> TaskExecutionStats:
        """创建统计记录（任务开始时调用）"""
        record = TaskExecutionStats(
            id=str(uuid.uuid4()),
            task_id=task_id,
            evaluation_type=evaluation_type,
            model_config_id=model_config_id,
            dataset_id=dataset_id,
            status="pending",
            created_at=beijing_now(),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def start_record(self, task_id: str) -> Optional[TaskExecutionStats]:
        """更新记录为运行中（任务实际开始执行时调用）"""
        record = self.db.query(TaskExecutionStats).filter(
            TaskExecutionStats.task_id == task_id,
            TaskExecutionStats.status == "pending"
        ).order_by(TaskExecutionStats.created_at.desc()).first()

        if record:
            record.status = "running"
            record.started_at = beijing_now()
            self.db.commit()
            self.db.refresh(record)
        return record

    def complete_record(self, task_id: str, success: bool = True) -> Optional[TaskExecutionStats]:
        """完成任务统计记录"""
        record = self.db.query(TaskExecutionStats).filter(
            TaskExecutionStats.task_id == task_id,
            TaskExecutionStats.status == "running"
        ).order_by(TaskExecutionStats.started_at.desc()).first()

        if record:
            record.status = "success" if success else "failed"
            record.finished_at = beijing_now()
            if record.started_at:
                record.duration_ms = int((record.finished_at - record.started_at).total_seconds() * 1000)
            self.db.commit()
            self.db.refresh(record)
        return record

    def fail_record(self, task_id: str) -> Optional[TaskExecutionStats]:
        """任务失败统计记录"""
        return self.complete_record(task_id, success=False)

    def stop_record(self, task_id: str) -> Optional[TaskExecutionStats]:
        """任务停止统计记录"""
        record = self.db.query(TaskExecutionStats).filter(
            TaskExecutionStats.task_id == task_id,
            TaskExecutionStats.status == "running"
        ).order_by(TaskExecutionStats.started_at.desc()).first()

        if record:
            record.status = "stopped"
            record.finished_at = beijing_now()
            if record.started_at:
                record.duration_ms = int((record.finished_at - record.started_at).total_seconds() * 1000)
            self.db.commit()
            self.db.refresh(record)
        return record

    def get_records(self, start_date: Optional[str] = None, end_date: Optional[str] = None,
                    evaluation_type: Optional[str] = None, status: Optional[str] = None,
                    page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """获取执行记录列表"""
        query = self.db.query(
            TaskExecutionStats,
            EvaluationTask.name.label("task_name")
        ).outerjoin(
            EvaluationTask,
            TaskExecutionStats.task_id == EvaluationTask.id
        )

        # 筛选
        if start_date:
            query = query.filter(TaskExecutionStats.created_at >= start_date)
        if end_date:
            query = query.filter(cast(TaskExecutionStats.created_at, Date) <= end_date)
        if evaluation_type:
            query = query.filter(TaskExecutionStats.evaluation_type == evaluation_type)
        if status:
            query = query.filter(TaskExecutionStats.status == status)

        # 总数
        total = query.count()

        # 分页
        rows = query.order_by(TaskExecutionStats.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

        items = []
        for row in rows:
            record = row[0]
            task_name = row[1]
            item = {
                "id": record.id,
                "task_id": record.task_id,
                "task_name": task_name,
                "evaluation_type": record.evaluation_type,
                "model_config_id": record.model_config_id,
                "dataset_id": record.dataset_id,
                "status": record.status,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "duration_ms": record.duration_ms,
                "created_at": record.created_at,
            }
            items.append(item)

        return {"total": total, "page": page, "page_size": page_size, "items": items}

    def get_summary(self, start_date: Optional[str] = None, end_date: Optional[str] = None,
                    evaluation_type: Optional[str] = None, status: Optional[str] = None,
                    group_by: Optional[str] = None) -> Dict[str, Any]:
        """获取汇总统计"""
        query = self.db.query(TaskExecutionStats)

        if start_date:
            query = query.filter(TaskExecutionStats.created_at >= start_date)
        if end_date:
            query = query.filter(cast(TaskExecutionStats.created_at, Date) <= end_date)
        if evaluation_type:
            query = query.filter(TaskExecutionStats.evaluation_type == evaluation_type)

        # 基本统计
        # total_count受status参数影响，success_count和failed_count不受影响(各自统计特定状态)
        total_count = query.filter(TaskExecutionStats.status == status).count() if status else query.count()
        success_count = query.filter(TaskExecutionStats.status == "success").count()
        failed_count = query.filter(TaskExecutionStats.status == "failed").count()

        # 平均耗时 - 应用相同的过滤条件
        avg_query = self.db.query(func.avg(TaskExecutionStats.duration_ms))
        if start_date:
            avg_query = avg_query.filter(TaskExecutionStats.created_at >= start_date)
        if end_date:
            avg_query = avg_query.filter(cast(TaskExecutionStats.created_at, Date) <= end_date)
        if evaluation_type:
            avg_query = avg_query.filter(TaskExecutionStats.evaluation_type == evaluation_type)
        if status:
            avg_query = avg_query.filter(TaskExecutionStats.status == status)
        avg_result = avg_query.filter(TaskExecutionStats.duration_ms.isnot(None)).scalar()
        avg_duration_ms = int(avg_result) if avg_result else 0

        result = {
            "total_count": total_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "success_rate": round(success_count / total_count, 4) if total_count > 0 else 0,
            "avg_duration_ms": avg_duration_ms,
        }

        # 分组统计
        if group_by == "evaluation_type":
            types = self.db.query(
                TaskExecutionStats.evaluation_type,
                func.count(TaskExecutionStats.id).label("count"),
                func.sum(func.cast(TaskExecutionStats.status == "success", Integer)).label("success_count"),
                func.avg(TaskExecutionStats.duration_ms).label("avg_duration")
            ).group_by(TaskExecutionStats.evaluation_type).all()

            result["by_type"] = {}
            for t in types:
                result["by_type"][t.evaluation_type] = {
                    "count": t.count,
                    "success_rate": round(t.success_count / t.count, 4) if t.count > 0 else 0,
                    "avg_duration_ms": int(t.avg_duration) if t.avg_duration else 0,
                }

        return result

    def get_trends(self, start_date: Optional[str] = None, end_date: Optional[str] = None,
                   evaluation_type: Optional[str] = None, granularity: str = "day") -> Dict[str, Any]:
        """获取耗时趋势"""
        # 根据粒度确定日期格式和分组
        if granularity == "month":
            date_format = "%Y-%m"
            date_trunc = func.date_trunc("month", TaskExecutionStats.created_at)
        elif granularity == "week":
            date_format = "%Y-W%W"
            date_trunc = func.date_trunc("week", TaskExecutionStats.created_at)
        else:  # day
            date_format = "%Y-%m-%d"
            date_trunc = func.date_trunc("day", TaskExecutionStats.created_at)

        query = self.db.query(
            date_trunc.label("date"),
            func.count(TaskExecutionStats.id).label("count"),
            func.sum(func.cast(TaskExecutionStats.status == "success", Integer)).label("success_count"),
            func.avg(TaskExecutionStats.duration_ms).label("avg_duration")
        )

        if start_date:
            query = query.filter(TaskExecutionStats.created_at >= start_date)
        if end_date:
            query = query.filter(cast(TaskExecutionStats.created_at, Date) <= end_date)
        if evaluation_type:
            query = query.filter(TaskExecutionStats.evaluation_type == evaluation_type)

        query = query.group_by(date_trunc).order_by(date_trunc)

        results = self.db.execute(query).fetchall()

        return {
            "granularity": granularity,
            "data": [
                {
                    "date": r.date.strftime(date_format) if r.date else "",
                    "count": r.count,
                    "success_rate": round(r.success_count / r.count, 4) if r.count > 0 else 0,
                    "avg_duration_ms": int(r.avg_duration) if r.avg_duration else 0,
                }
                for r in results
            ]
        }