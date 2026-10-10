"""提效统计服务"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from app.models.task_models import TaskExecutionRecord
from app.models.stats_models import TaskExecutionStats
from app.models.task_models import EvaluationTask

logger = logging.getLogger(__name__)


def _ms_to_hours(ms: Optional[int]) -> float:
    """毫秒转小时"""
    if ms is None:
        return 0
    return round(ms / 3600000, 2)


class EfficiencyService:
    """提效统计服务"""

    def __init__(self, db: Session):
        self.db = db

    def update_manual_hours(self, record_id: str, hours: float) -> Optional[Dict[str, Any]]:
        """更新单条执行记录的预计耗时"""
        record = self.db.query(TaskExecutionRecord).filter(TaskExecutionRecord.id == record_id).first()
        if not record:
            return None
        record.estimated_manual_hours = hours
        self.db.commit()
        self.db.refresh(record)
        return {
            "id": record.id,
            "task_id": record.task_id,
            "execution_number": record.execution_number,
            "estimated_manual_hours": record.estimated_manual_hours,
        }

    def get_task_efficiency(self, task_id: str) -> List[Dict[str, Any]]:
        """获取单个任务各次执行的提效值"""
        records = self.db.query(TaskExecutionRecord).filter(
            TaskExecutionRecord.task_id == task_id,
        ).order_by(TaskExecutionRecord.execution_number.asc()).all()

        # 获取对应的统计记录（含 duration_ms）
        stats_record = self.db.query(TaskExecutionStats).filter(
            TaskExecutionStats.task_id == task_id,
            TaskExecutionStats.status == "success",
        ).order_by(TaskExecutionStats.created_at.desc()).first()

        system_hours = _ms_to_hours(stats_record.duration_ms if stats_record else None)

        result = []
        for r in records:
            if r.estimated_manual_hours is None:
                manual_hours = 0
                efficiency = 0.0
            else:
                manual_hours = r.estimated_manual_hours
                efficiency = round(manual_hours - system_hours, 2)

            result.append({
                "execution_number": r.execution_number,
                "estimated_manual_hours": manual_hours,
                "system_hours": system_hours,
                "efficiency_hours": efficiency,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            })

        return result

    def get_total_efficiency(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
        """获取时间范围内的总提效统计"""
        query = self.db.query(TaskExecutionRecord)

        if start_date:
            query = query.filter(TaskExecutionRecord.completed_at >= start_date)
        if end_date:
            query = query.filter(TaskExecutionRecord.completed_at <= end_date)

        records = query.all()

        if not records:
            return {"total_efficiency_hours": 0.0, "task_count": 0, "avg_efficiency_hours": 0.0, "items": []}

        # Batch load all TaskExecutionStats for the relevant task_ids
        task_ids = list(set(r.task_id for r in records))
        stats_list = self.db.query(TaskExecutionStats).filter(
            TaskExecutionStats.task_id.in_(task_ids),
            TaskExecutionStats.status == "success",
        ).all()

        stats_map: Dict[str, TaskExecutionStats] = {}
        for s in stats_list:
            if s.task_id not in stats_map:
                stats_map[s.task_id] = s

        # Batch load all task names
        tasks = self.db.query(EvaluationTask).filter(
            EvaluationTask.id.in_(task_ids),
        ).all()
        task_name_map: Dict[str, str] = {t.id: t.name for t in tasks}

        total_efficiency = 0.0
        items = []

        for r in records:
            stats = stats_map.get(r.task_id)
            if not stats:
                continue

            task_name = task_name_map.get(r.task_id, r.task_id)
            system_hours = _ms_to_hours(stats.duration_ms)

            if r.estimated_manual_hours is None:
                # 无预计耗时：efficiency=0，不累加总提效
                efficiency = 0.0
                manual_hours = None
            else:
                manual_hours = r.estimated_manual_hours
                efficiency = round(manual_hours - system_hours, 2)
                total_efficiency += efficiency

            items.append({
                "task_id": r.task_id,
                "task_name": task_name,
                "execution_number": r.execution_number,
                "estimated_manual_hours": manual_hours,
                "system_hours": system_hours,
                "efficiency_hours": efficiency,
            })

        task_count = len(items)
        avg_efficiency = round(total_efficiency / task_count, 2) if task_count > 0 else 0

        return {
            "total_efficiency_hours": round(total_efficiency, 2),
            "task_count": task_count,
            "avg_efficiency_hours": avg_efficiency,
            "items": items,
        }
