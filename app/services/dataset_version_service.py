"""数据集版本服务（M5 QE5-1）

打版/列表/行级 diff/恢复。版本的事实源是 dataset_snapshot 快照（复用既有
快照机制：data.items=[{id,index,data}] + fields.fields=[字段元数据]），
dataset_versions 只登记版本号与快照指针，锁定版本执行与任务执行快照走
同一条快照加载链路（evaluation_tasks_async._load_dataset）。
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# diff 响应中单字段值的展示截断长度（会话数据集单字段可达几十 MB，
# diff 面板只需要人可读的摘要；完整数据以版本快照为准）
DIFF_VALUE_TRUNCATE_CHARS = 200
# diff 响应最多返回的差异行数（超出置 truncated=true，前端提示用筛选/导出）
DIFF_MAX_ROWS = 500


class DatasetVersionNotFound(ValueError):
    """版本不存在（或版本不属于该数据集）"""


def _stable_key(item: Dict[str, Any]) -> str:
    """行级 diff 的稳定行键：快照打版时数据行的 id；缺失时回退 index。

    恢复/重导入后数据行 id 会变（恢复是重写行），此时按 index 对齐只能
    给出位置级参考——回退键加 index: 前缀区分两种键空间，避免误对齐。
    """
    row_id = item.get("id")
    if row_id:
        return f"id:{row_id}"
    return f"index:{item.get('index')}"


def _slim_value(value: Any) -> Any:
    """diff 展示用字段值瘦身：长标量/嵌套结构截断为可读摘要"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= DIFF_VALUE_TRUNCATE_CHARS:
            return value
        return value[:DIFF_VALUE_TRUNCATE_CHARS] + f"…（截断，共 {len(value)} 字符）"
    # dict/list：整体 JSON 化后截断（diff 面板不做深度展开）
    try:
        import json

        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= DIFF_VALUE_TRUNCATE_CHARS:
        return value
    return text[:DIFF_VALUE_TRUNCATE_CHARS] + f"…（截断，共 {len(text)} 字符）"


def _slim_row_data(data: Any) -> Any:
    if not isinstance(data, dict):
        return _slim_value(data)
    return {key: _slim_value(val) for key, val in data.items()}


class DatasetVersionService:
    """数据集版本服务"""

    def __init__(self, db: Session):
        self.db = db

    # ---------------- 打版 ----------------

    def create_version(
        self,
        dataset_id: str,
        changelog: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """以数据集当前数据打版（创建快照 + 登记版本号）

        Returns:
            版本信息 dict（version_no/snapshot_id/row_count/...）
        """
        from sqlalchemy import func
        from sqlalchemy.exc import IntegrityError

        from app.models.dataset_models import DatasetSnapshot, DatasetVersion
        from app.services.dataset_snapshot_service import DatasetSnapshotService

        snapshot_id = DatasetSnapshotService(self.db).create_dataset_snapshot(dataset_id)
        # 快照里刚写入的行数（打版行数登记，列表展示免解包快照）
        snapshot = self.db.query(DatasetSnapshot).filter(
            DatasetSnapshot.id == snapshot_id
        ).first()
        row_count = len((snapshot.data or {}).get("items", [])) if snapshot else None

        for attempt in range(2):
            max_no = self.db.query(func.max(DatasetVersion.version_no)).filter(
                DatasetVersion.dataset_id == dataset_id
            ).scalar() or 0
            version = DatasetVersion(
                id=f"dsv_{uuid.uuid4().hex[:12]}",
                dataset_id=dataset_id,
                version_no=max_no + 1,
                snapshot_id=snapshot_id,
                changelog=changelog,
                row_count=row_count,
                created_by=created_by,
            )
            self.db.add(version)
            try:
                self.db.commit()
                break
            except IntegrityError:
                # 并发打版撞唯一约束：回滚后重取 max 再试一次
                self.db.rollback()
                if attempt == 1:
                    raise
        self.db.refresh(version)
        logger.info(
            "数据集打版成功: dataset_id=%s, version_no=%s, rows=%s",
            dataset_id, version.version_no, version.row_count,
        )
        return self._to_dict(version)

    # ---------------- 查询 ----------------

    def list_versions(self, dataset_id: str) -> List[Dict[str, Any]]:
        """版本列表（新→旧）"""
        from app.models.dataset_models import DatasetVersion

        versions = self.db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == dataset_id
        ).order_by(DatasetVersion.version_no.desc()).all()
        return [self._to_dict(v) for v in versions]

    def get_version(self, dataset_id: str, version_no: int) -> Dict[str, Any]:
        """单版本详情（含快照字段结构与行数）"""
        version = self._get_version_or_raise(dataset_id, version_no)
        result = self._to_dict(version)
        snapshot_data = self.load_snapshot_items(version.snapshot_id)
        result["fields"] = self._load_snapshot_fields(version.snapshot_id)
        result["row_count_detail"] = len(snapshot_data)
        return result

    def _get_version_or_raise(self, dataset_id: str, version_no: int):
        from app.models.dataset_models import DatasetVersion

        version = self.db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.version_no == version_no,
        ).first()
        if not version:
            raise DatasetVersionNotFound(
                f"数据集 {dataset_id} 不存在版本 {version_no}"
            )
        return version

    def version_exists(self, dataset_id: str, version_no: int) -> bool:
        """版本存在性（任务绑定校验用）"""
        from app.models.dataset_models import DatasetVersion

        return self.db.query(DatasetVersion.id).filter(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.version_no == version_no,
        ).first() is not None

    def get_version_row_count(self, dataset_id: str, version_no: int) -> Optional[int]:
        """版本行数（任务锁定版本的题目基数）；版本不存在返回 None"""
        from app.models.dataset_models import DatasetVersion

        row = self.db.query(DatasetVersion.row_count).filter(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.version_no == version_no,
        ).first()
        return row[0] if row else None

    def get_version_snapshot_id(self, dataset_id: str, version_no: int) -> Optional[str]:
        """版本快照ID（锁定版本执行时定位快照）；版本不存在返回 None"""
        from app.models.dataset_models import DatasetVersion

        row = self.db.query(DatasetVersion.snapshot_id).filter(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.version_no == version_no,
        ).first()
        return row[0] if row else None

    # ---------------- 行级 diff ----------------

    def diff_versions(self, dataset_id: str, from_no: int, to_no: int) -> Dict[str, Any]:
        """两版本的行级 diff：按稳定行键（打版时数据行 id，缺失回退 index）对齐

        Returns:
            {from/to 版本元信息, summary: {added/removed/changed/unchanged/计数},
             rows: [{key, status, from_index, to_index, from_data, to_data,
                     changed_fields}], truncated}
        """
        from_version = self._get_version_or_raise(dataset_id, from_no)
        to_version = self._get_version_or_raise(dataset_id, to_no)

        from_items = self.load_snapshot_items(from_version.snapshot_id)
        to_items = self.load_snapshot_items(to_version.snapshot_id)

        from_map = {_stable_key(item): item for item in from_items}
        to_map = {_stable_key(item): item for item in to_items}

        rows: List[Dict[str, Any]] = []
        added = removed = changed = unchanged = 0

        # 先按 from 顺序走（removed/changed/unchanged），再补 to 独有（added）
        for key, from_item in from_map.items():
            to_item = to_map.get(key)
            if to_item is None:
                removed += 1
                rows.append({
                    "key": key,
                    "status": "removed",
                    "from_index": from_item.get("index"),
                    "to_index": None,
                    "from_data": _slim_row_data(from_item.get("data")),
                    "to_data": None,
                    "changed_fields": [],
                })
                continue
            changed_fields = self._changed_fields(from_item.get("data"), to_item.get("data"))
            if changed_fields:
                changed += 1
                rows.append({
                    "key": key,
                    "status": "changed",
                    "from_index": from_item.get("index"),
                    "to_index": to_item.get("index"),
                    "from_data": _slim_row_data(from_item.get("data")),
                    "to_data": _slim_row_data(to_item.get("data")),
                    "changed_fields": changed_fields,
                })
            else:
                unchanged += 1

        for key, to_item in to_map.items():
            if key not in from_map:
                added += 1
                rows.append({
                    "key": key,
                    "status": "added",
                    "from_index": None,
                    "to_index": to_item.get("index"),
                    "from_data": None,
                    "to_data": _slim_row_data(to_item.get("data")),
                    "changed_fields": [],
                })

        # 变更行在前、新增/删除随后（diff 面板默认关心变了什么）
        status_order = {"changed": 0, "added": 1, "removed": 2}
        rows.sort(key=lambda r: status_order.get(r["status"], 3))
        truncated = len(rows) > DIFF_MAX_ROWS
        if truncated:
            rows = rows[:DIFF_MAX_ROWS]

        return {
            "dataset_id": dataset_id,
            "from": self._to_dict(from_version),
            "to": self._to_dict(to_version),
            "summary": {
                "added": added,
                "removed": removed,
                "changed": changed,
                "unchanged": unchanged,
                "from_count": len(from_items),
                "to_count": len(to_items),
            },
            "rows": rows,
            "truncated": truncated,
        }

    @staticmethod
    def _changed_fields(from_data: Any, to_data: Any) -> List[str]:
        """行数据字段级对比：返回值不一致的字段名（并集）"""
        from_set = from_data if isinstance(from_data, dict) else {}
        to_set = to_data if isinstance(to_data, dict) else {}
        changed = []
        for key in set(from_set) | set(to_set):
            if from_set.get(key) != to_set.get(key):
                changed.append(key)
        return sorted(changed)

    # ---------------- 恢复 ----------------

    def restore_version(self, dataset_id: str, version_no: int) -> Dict[str, Any]:
        """把数据集活数据恢复到指定版本

        语义：软删当前全部 V2 活行，按版本快照重写为新行（新 id、保留 index
        顺序），字段元数据同步恢复；data JSON 原样拷贝（行内文件引用随行
        保留）。恢复不自动打版——如需留存恢复前状态，先手动打版。
        """
        from app.models.dataset_models import EvaluationDataV2, EvaluationDataset
        from app.services.dataset_field_service import DatasetFieldService

        version = self._get_version_or_raise(dataset_id, version_no)
        items = self.load_snapshot_items(version.snapshot_id)
        fields = self._load_snapshot_fields(version.snapshot_id)

        # 1) 软删当前活行
        deactivated = self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True,  # noqa: E712
        ).update({"is_active": False}, synchronize_session=False)

        # 2) 按快照重写行（新 id；index 保留快照顺序，缺失按位置补齐）
        for position, item in enumerate(items):
            self.db.add(EvaluationDataV2(
                id=f"data2_{uuid.uuid4().hex[:12]}",
                dataset_id=dataset_id,
                data=item.get("data") or {},
                index=item.get("index") if isinstance(item.get("index"), int) else position,
            ))

        # 3) 字段元数据恢复（全量覆盖语义，与 save_field_metadata 一致）
        if fields:
            DatasetFieldService(self.db).save_field_metadata(dataset_id, fields)

        # 4) 数据集行数口径刷新
        dataset = self.db.query(EvaluationDataset).filter(
            EvaluationDataset.id == dataset_id
        ).first()
        if dataset:
            dataset.question_count = len(items)

        self.db.commit()
        logger.info(
            "数据集版本恢复完成: dataset_id=%s, version_no=%s, 恢复 %d 行（软删 %d 行）",
            dataset_id, version_no, len(items), deactivated,
        )
        return {
            "dataset_id": dataset_id,
            "version_no": version_no,
            "restored_count": len(items),
            "deactivated_count": deactivated,
        }

    # ---------------- 内部 ----------------

    def load_snapshot_items(self, snapshot_id: str) -> List[Dict[str, Any]]:
        from app.models.dataset_models import DatasetSnapshot

        snapshot = self.db.query(DatasetSnapshot).filter(
            DatasetSnapshot.id == snapshot_id
        ).first()
        if not snapshot:
            raise DatasetVersionNotFound(f"版本快照不存在: {snapshot_id}")
        data = snapshot.data or {}
        if isinstance(data, dict):
            return list(data.get("items", []))
        return list(data)

    def _load_snapshot_fields(self, snapshot_id: str) -> List[Dict[str, Any]]:
        from app.models.dataset_models import DatasetSnapshot

        snapshot = self.db.query(DatasetSnapshot).filter(
            DatasetSnapshot.id == snapshot_id
        ).first()
        if not snapshot:
            return []
        fields = snapshot.fields or {}
        if isinstance(fields, dict):
            return list(fields.get("fields", []))
        return list(fields)

    @staticmethod
    def _to_dict(version) -> Dict[str, Any]:
        return {
            "id": version.id,
            "dataset_id": version.dataset_id,
            "version_no": version.version_no,
            "snapshot_id": version.snapshot_id,
            "changelog": version.changelog,
            "row_count": version.row_count,
            "created_by": version.created_by,
            "created_at": version.created_at.isoformat() if version.created_at else None,
        }
