"""周覆盖度统计服务"""
import uuid
import logging
from datetime import date
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models.stats_models import WeeklyCoverageConfig, WeeklyPlanTask
from app.models.stats_models import TaskExecutionStats

logger = logging.getLogger(__name__)


WEEK_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class CoverageService:
    """周覆盖度服务"""

    def __init__(self, db: Session):
        self.db = db

    def get_config(self) -> Optional[Dict[str, Any]]:
        """获取周配置"""
        config = self.db.query(WeeklyCoverageConfig).first()
        if not config:
            return None
        return {
            "id": config.id,
            "week_start_day": config.week_start_day,
            "week_end_day": config.week_end_day,
        }

    def update_config(self, week_start_day: str, week_end_day: str) -> Dict[str, Any]:
        """更新周配置"""
        if week_start_day not in WEEK_DAYS or week_end_day not in WEEK_DAYS:
            raise ValueError(f"无效的星期值，可选: {', '.join(WEEK_DAYS)}")

        config = self.db.query(WeeklyCoverageConfig).first()
        if config:
            config.week_start_day = week_start_day
            config.week_end_day = week_end_day
        else:
            config = WeeklyCoverageConfig(
                id=str(uuid.uuid4()),
                week_start_day=week_start_day,
                week_end_day=week_end_day,
            )
            self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        return {
            "id": config.id,
            "week_start_day": config.week_start_day,
            "week_end_day": config.week_end_day,
        }

    def get_plans(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """获取周计划列表"""
        query = self.db.query(WeeklyPlanTask).order_by(WeeklyPlanTask.week_start_date.desc())
        total = query.count()
        items = query.offset((page - 1) * page_size).limit(page_size).all()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "id": item.id,
                    "week_start_date": item.week_start_date.isoformat(),
                    "week_end_date": item.week_end_date.isoformat(),
                    "planned_count": item.planned_count,
                    "locked": item.week_end_date < date.today(),
                }
                for item in items
            ],
        }

    def upsert_plan(self, week_start_date: date, week_end_date: date, planned_count: int) -> Dict[str, Any]:
        """录入或修改周计划数"""
        if week_end_date < date.today():
            raise ValueError("已过期的周不可修改计划数")

        existing = self.db.query(WeeklyPlanTask).filter(
            WeeklyPlanTask.week_start_date == week_start_date,
            WeeklyPlanTask.week_end_date == week_end_date,
        ).first()

        if existing:
            existing.planned_count = planned_count
            plan = existing
        else:
            plan = WeeklyPlanTask(
                id=str(uuid.uuid4()),
                week_start_date=week_start_date,
                week_end_date=week_end_date,
                planned_count=planned_count,
            )
            self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)
        return {
            "id": plan.id,
            "week_start_date": plan.week_start_date.isoformat(),
            "week_end_date": plan.week_end_date.isoformat(),
            "planned_count": plan.planned_count,
            "locked": plan.week_end_date < date.today(),
        }

    def delete_plan(self, plan_id: str) -> bool:
        """删除周计划（仅未锁定周）"""
        plan = self.db.query(WeeklyPlanTask).filter(WeeklyPlanTask.id == plan_id).first()
        if not plan:
            return False
        if plan.week_end_date < date.today():
            raise ValueError("已过期的周不可删除")
        self.db.delete(plan)
        self.db.commit()
        return True

    def get_coverage(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """计算时间范围内各周的覆盖率"""
        query = self.db.query(WeeklyPlanTask)

        if start_date:
            query = query.filter(WeeklyPlanTask.week_start_date >= start_date)
        if end_date:
            query = query.filter(WeeklyPlanTask.week_end_date <= end_date)

        plans = query.order_by(WeeklyPlanTask.week_start_date.asc()).all()

        result = []
        for plan in plans:
            # 统计周范围内成功的执行任务数
            actual_count = self.db.query(func.count(TaskExecutionStats.id)).filter(
                TaskExecutionStats.status == "success",
                func.date(TaskExecutionStats.created_at) >= plan.week_start_date,
                func.date(TaskExecutionStats.created_at) <= plan.week_end_date,
            ).scalar() or 0

            coverage_rate = round((actual_count / plan.planned_count) * 100, 2) if plan.planned_count > 0 else 0

            result.append({
                "id": plan.id,
                "week_start_date": plan.week_start_date.isoformat(),
                "week_end_date": plan.week_end_date.isoformat(),
                "planned_count": plan.planned_count,
                "actual_count": actual_count,
                "coverage_rate": coverage_rate,
            })

        return result
