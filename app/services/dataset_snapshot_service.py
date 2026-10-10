"""
数据集快照服务
"""
import uuid
import json
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class DatasetSnapshotService:
    """数据集快照服务"""

    def __init__(self, db: Session):
        self.db = db

    def create_snapshot(self, task_id: str, dataset_id: str) -> str:
        """为任务创建数据集快照（V2 only）

        Args:
            task_id: 任务ID
            dataset_id: 数据集ID

        Returns:
            snapshot_id: 快照ID
        """
        from app.models.dataset_models import DatasetSnapshot, EvaluationDataV2, DatasetFieldMetadata
        from app.services.dataset_service import DatasetService, DatasetDataService

        # 获取数据集服务
        dataset_service = DatasetService(self.db)
        data_service = DatasetDataService(self.db)

        # 获取数据集
        dataset = dataset_service.get_dataset(dataset_id)
        if not dataset:
            raise ValueError(f"数据集不存在: {dataset_id}")

        # 获取数据集字段元数据
        field_metas = self.db.query(DatasetFieldMetadata).filter(
            DatasetFieldMetadata.dataset_id == dataset_id
        ).order_by(DatasetFieldMetadata.position).all()

        fields_snapshot = [
            {
                "name": f.field_name,
                "label": f.field_label,
                "type": f.field_type,
                "is_media": f.is_media,
                "is_media_field": f.is_media_field,
                "is_required": f.is_required,
                "position": f.position
            }
            for f in field_metas
        ]

        # 获取 V2 数据
        data_items_v2 = data_service.get_data_list_v2(dataset_id, skip=0, limit=100000)

        # 构建数据快照
        data_snapshot = []
        for item in data_items_v2:
            data_snapshot.append({
                "id": item.id,
                "index": item.index,
                "data": item.data,
            })

        # 创建快照记录
        snapshot_id = f"dss_{uuid.uuid4().hex[:12]}"
        snapshot = DatasetSnapshot(
            id=snapshot_id,
            dataset_id=dataset_id,
            task_id=task_id,
            data={"items": data_snapshot},
            fields={"fields": fields_snapshot}
        )

        self.db.add(snapshot)
        self.db.commit()

        logger.info(f"创建数据集快照成功: snapshot_id={snapshot_id}, task_id={task_id}, dataset_id={dataset_id}")

        return snapshot_id

    def get_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        """获取快照数据

        Args:
            snapshot_id: 快照ID

        Returns:
            {"data": [...], "fields": [...]}
        """
        from app.models.dataset_models import DatasetSnapshot

        snapshot = self.db.query(DatasetSnapshot).filter(
            DatasetSnapshot.id == snapshot_id
        ).first()

        if not snapshot:
            return None

        return {
            "id": snapshot.id,
            "dataset_id": snapshot.dataset_id,
            "task_id": snapshot.task_id,
            "data": snapshot.data,
            "fields": snapshot.fields,
            "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None
        }

    def get_latest_snapshot_for_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务最新的数据集快照

        Args:
            task_id: 任务ID

        Returns:
            最新的快照数据
        """
        from app.models.dataset_models import DatasetSnapshot

        snapshot = self.db.query(DatasetSnapshot).filter(
            DatasetSnapshot.task_id == task_id
        ).order_by(DatasetSnapshot.created_at.desc()).first()

        if not snapshot:
            return None

        return {
            "id": snapshot.id,
            "dataset_id": snapshot.dataset_id,
            "task_id": snapshot.task_id,
            "data": snapshot.data,
            "fields": snapshot.fields,
            "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None
        }

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """删除快照

        Args:
            snapshot_id: 快照ID

        Returns:
            是否成功删除
        """
        from app.models.dataset_models import DatasetSnapshot

        snapshot = self.db.query(DatasetSnapshot).filter(
            DatasetSnapshot.id == snapshot_id
        ).first()

        if not snapshot:
            return False

        self.db.delete(snapshot)
        self.db.commit()
        return True

    def delete_snapshots_for_task(self, task_id: str) -> int:
        """删除任务的所有快照

        Args:
            task_id: 任务ID

        Returns:
            删除的快照数量
        """
        from app.models.dataset_models import DatasetSnapshot

        count = self.db.query(DatasetSnapshot).filter(
            DatasetSnapshot.task_id == task_id
        ).delete()

        self.db.commit()
        return count

    def refresh_snapshot(self, task_id: str, dataset_id: str) -> str:
        """刷新任务的数据集快照

        先删除旧快照，再创建新快照

        Args:
            task_id: 任务ID
            dataset_id: 数据集ID

        Returns:
            新快照ID
        """
        # 删除旧快照
        self.delete_snapshots_for_task(task_id)

        # 创建新快照
        return self.create_snapshot(task_id, dataset_id)
