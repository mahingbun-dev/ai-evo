"""
团队服务
基于部门/组织信息同步团队数据，提供团队管理和成员查询
"""
import logging
from typing import List, Optional, Tuple, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.team_models import Team
from app.models.user_models import User

logger = logging.getLogger(__name__)


def upsert_team(db: Session, org_code: str, org_name: str = "") -> Team:
    """
    查找或创建团队，已存在则更新 team_name
    """
    if not org_code:
        return None

    team = db.get(Team, org_code)

    if team is None:
        # 首次创建团队
        team = Team(
            team_code=org_code,
            team_name=org_name or org_code,
            is_active=True,
        )
        db.add(team)
        logger.info(f"创建新团队: {org_code} - {org_name}")
    else:
        # 更新团队名称
        if org_name and team.team_name != org_name:
            team.team_name = org_name
            logger.info(f"更新团队名称: {org_code} - {org_name}")

    db.commit()
    db.refresh(team)
    return team


def get_teams(db: Session, skip: int = 0, limit: int = 100) -> List[Team]:
    """
    获取团队列表（按 team_code 排序）
    """
    return db.query(Team).filter(
        Team.is_active == True
    ).order_by(Team.team_code).offset(skip).limit(limit).all()


def get_team(db: Session, team_code: str) -> Optional[Team]:
    """
    获取单个团队
    """
    return db.get(Team, team_code)


def get_team_members(
    db: Session,
    team_code: str,
    skip: int = 0,
    limit: int = 100
) -> Tuple[List[User], int]:
    """
    获取团队成员列表
    返回：(成员列表, 总数)
    """
    query = db.query(User).filter(
        User.org_code == team_code,
        User.is_active == True
    )

    total = query.count()
    items = query.offset(skip).limit(limit).all()

    return items, total


def get_team_stats(db: Session, team_code: str) -> Dict[str, int]:
    """
    获取团队资源统计
    返回：任务数、数据集数、模型配置数
    """
    from app.models.task_models import EvaluationTask
    from app.models.dataset_models import EvaluationDataset
    from app.models.model_config_models import ModelConfig

    task_count = db.query(EvaluationTask).filter(
        EvaluationTask.team_code == team_code,
        EvaluationTask.is_active == True
    ).count()

    dataset_count = db.query(EvaluationDataset).filter(
        EvaluationDataset.team_code == team_code,
        EvaluationDataset.is_active == True
    ).count()

    model_count = db.query(ModelConfig).filter(
        ModelConfig.team_code == team_code,
        ModelConfig.is_active == True
    ).count()

    member_count = db.query(User).filter(
        User.org_code == team_code,
        User.is_active == True
    ).count()

    return {
        "task_count": task_count,
        "dataset_count": dataset_count,
        "model_count": model_count,
        "member_count": member_count,
    }


def update_user_status(db: Session, user_code: str, is_active: bool) -> Optional[User]:
    """
    更新用户启用/禁用状态
    """
    user = db.get(User, user_code)
    if not user:
        logger.warning(f"用户不存在: {user_code}")
        return None

    user.is_active = is_active
    db.commit()
    db.refresh(user)
    logger.info(f"更新用户状态: {user_code}, is_active={is_active}")
    return user


def get_team_members_all(
    db: Session,
    team_code: str,
    skip: int = 0,
    limit: int = 100,
    include_inactive: bool = False
) -> Tuple[List[User], int]:
    """
    获取团队成员列表（可选择包含禁用的成员）
    返回：(成员列表, 总数)
    """
    query = db.query(User).filter(User.org_code == team_code)

    if not include_inactive:
        query = query.filter(User.is_active == True)

    total = query.count()
    items = query.offset(skip).limit(limit).all()

    return items, total
