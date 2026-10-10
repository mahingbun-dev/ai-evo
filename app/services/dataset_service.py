"""
评测数据集管理服务
"""
import uuid
import json
import os
import tempfile
import logging
import pandas as pd
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql import func
import sqlalchemy as sa
from fastapi import UploadFile
from app.models.dataset_models import (
    EvaluationDataset,
    EvaluationData,
    EvaluationDataV2,
    DatasetFieldMetadata,
)
from app.services.dataset_field_service import DatasetFieldService


class DatasetService:
    """评测数据集服务"""

    def __init__(self, db: Session):
        self.db = db

    def create_dataset(
        self,
        name: str,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        team_code: Optional[str] = None,
    ) -> EvaluationDataset:
        """创建数据集"""
        # 检查名称是否已存在（仅检查未删除的数据集）
        existing = self.db.query(EvaluationDataset).filter(
            EvaluationDataset.name == name,
            EvaluationDataset.is_active == True
        ).first()
        if existing:
            raise ValueError(f"数据集名称 '{name}' 已存在，请使用其他名称")

        dataset_id = f"ds_{uuid.uuid4().hex[:12]}"

        dataset = EvaluationDataset(
            id=dataset_id,
            name=name,
            description=description,
            created_by=created_by,
            team_code=team_code or "DEFAULT",
        )

        self.db.add(dataset)
        self.db.commit()
        self.db.refresh(dataset)

        return dataset

    def get_dataset(self, dataset_id: str) -> Optional[EvaluationDataset]:
        """获取数据集"""
        dataset = self.db.query(EvaluationDataset).filter(
            EvaluationDataset.id == dataset_id
        ).first()
        if dataset:
            dataset.question_count = self.db.query(func.count(EvaluationDataV2.id)).filter(
                EvaluationDataV2.dataset_id == dataset_id,
                EvaluationDataV2.is_active == True
            ).scalar() or 0
        return dataset

    def get_datasets(
        self,
        skip: int = 0,
        limit: int = 100,
        team_code: Optional[str] = None,
    ) -> List[EvaluationDataset]:
        """获取数据集列表"""
        query = self.db.query(EvaluationDataset).filter(
            EvaluationDataset.is_active == True
        )

        if team_code:
            query = query.filter(EvaluationDataset.team_code == team_code)

        datasets = query.order_by(EvaluationDataset.created_at.desc()).offset(skip).limit(limit).all()

        if not datasets:
            return datasets

        # 一次性批量查询所有数据集的题目数量，避免 N+1 查询
        dataset_ids = [d.id for d in datasets]
        counts = self.db.query(
            EvaluationDataV2.dataset_id,
            func.count(EvaluationDataV2.id).label("cnt")
        ).filter(
            EvaluationDataV2.dataset_id.in_(dataset_ids),
            EvaluationDataV2.is_active == True
        ).group_by(EvaluationDataV2.dataset_id).all()
        count_map = {row.dataset_id: row.cnt for row in counts}

        # has_media 优先从字段元数据推导（excel/会话导入都会同步写 is_media 标志，
        # 元数据是小表，毫秒级）；逐数据集加载全部数据行做行级判断会把大会话数据集
        # （单条可达几十 MB 的 session_messages）整体反序列化，列表接口耗时秒级
        meta_dataset_ids = set()
        media_dataset_ids = set()
        for row in self.db.query(DatasetFieldMetadata.dataset_id, DatasetFieldMetadata.is_media).filter(
            DatasetFieldMetadata.dataset_id.in_(dataset_ids)
        ).all():
            meta_dataset_ids.add(row.dataset_id)
            if row.is_media:
                media_dataset_ids.add(row.dataset_id)

        # 动态写回 question_count 和 has_media
        for dataset in datasets:
            dataset.question_count = count_map.get(dataset.id, 0)
            if dataset.id in meta_dataset_ids:
                dataset.has_media = dataset.id in media_dataset_ids
            else:
                # 无字段元数据的数据集（json 导入/单条添加）退回行级判断保语义；
                # 这类数据集不经会话导入，数据量小，行级判断开销可控
                dataset.has_media = self._check_has_media(dataset.id)

        return datasets

    def _check_has_media(self, dataset_id: str) -> bool:
        """检查数据集是否包含媒体文件"""
        # 获取所有 V2 数据，检查 data JSON 中的 *_is_media 字段
        data_items = self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True
        ).all()

        for item in data_items:
            data = item.data or {}
            for key, value in data.items():
                if key.endswith("_is_media") and value in [True, 'true', 'True', '1', 1]:
                    return True

        return False

    def update_dataset(
        self,
        dataset_id: str,
        **kwargs,
    ) -> Optional[EvaluationDataset]:
        """更新数据集"""
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            return None

        for key, value in kwargs.items():
            if hasattr(dataset, key):
                setattr(dataset, key, value)

        self.db.commit()
        self.db.refresh(dataset)
        return dataset

    def delete_dataset(self, dataset_id: str) -> bool:
        """删除数据集"""
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            return False

        # 检查是否有关联的评测任务
        from app.models.task_models import EvaluationTask
        related_tasks = self.db.query(EvaluationTask).filter(
            EvaluationTask.dataset_id == dataset_id,
            EvaluationTask.is_active == True
        ).all()

        if related_tasks:
            task_names = [task.name for task in related_tasks]
            raise ValueError(f"该数据集被以下评测任务使用，无法删除：{', '.join(task_names)}。请先替换或删除这些评测任务。")

        # 清理数据集所有媒体文件的 Redis 缓存
        try:
            from app.services.oss_media_cache_service import OSSMediaCacheService
            oss_service = OSSMediaCacheService()
            oss_service.invalidate_dataset_cache(dataset_id, self.db)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"清理数据集媒体缓存失败: {dataset_id}, 错误: {e}")

        dataset.is_active = False
        self.db.commit()

        # 数据集迭代阶段C：软删后释放活跃数据行持有的执行包引用（best-effort）；
        # 末引用（ref_count→0）由 release_ref 清理解包目录与登记记录
        try:
            from app.services.hub_file_service import release_dataset_hub_refs
            release_dataset_hub_refs(self.db, dataset_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"释放执行包引用失败: {dataset_id}, 错误: {e}")
        return True

    def import_from_excel(self, dataset_id: str, file_path: str, is_update: bool = False, restore_matched: bool = False) -> int:
        """从Excel导入评测数据（V2 格式，写入 evaluation_data_v2 表）
        is_update: 是否更新已存在的数据（根据 user_input 判断）
        restore_matched: 是否恢复与导入数据内容一致的软删除记录（默认True）
        """
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            raise ValueError("数据集不存在")

        # 读取Excel（默认只读取第一个sheet）
        df = pd.read_excel(file_path, sheet_name=0)
        # 清理列名中的前后空格
        df.columns = [str(col).strip() for col in df.columns.tolist()]

        def _convert_to_string(value):
            """将 Excel 单元格值转换为字符串，处理数字精度问题"""
            if isinstance(value, bool):
                return "true" if value else "false"
            if pd.isna(value):
                return ""
            if isinstance(value, (int, float)):
                if isinstance(value, float) and value == int(value):
                    return str(int(value))
                return str(value)
            return str(value)

        def _convert_is_media(value):
            """将 is_media 字符串值转换为布尔值"""
            if pd.isna(value):
                return False
            val = str(value).strip().lower()
            return val in ["是", "yes", "true", "1"]

        # 收集导入数据的内容用于匹配（以 user_input 为 key）
        import_data = {}
        # 确定 user_input 列名（第一个非 _is_media 列）
        non_media_cols = [col for col in df.columns if not col.endswith("_is_media")]
        user_input_col = non_media_cols[0] if non_media_cols else None

        for idx, row in df.iterrows():
            if user_input_col:
                ui = _convert_to_string(row.get(user_input_col, "")).strip()
                if ui:
                    import_data[ui] = True

        # 如果是新增模式且 restore_matched 为 True，检查并恢复软删除的数据
        restored_count = 0
        restored_user_inputs = set()
        if not is_update and restore_matched and import_data:
            deleted_data = self.db.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == dataset_id,
                EvaluationDataV2.is_active == False
            ).all()

            for data in deleted_data:
                data_json = data.data or {}
                ui = data_json.get(user_input_col, "") if user_input_col else ""
                if ui and ui in import_data:
                    data.is_active = True
                    restored_count += 1
                    restored_user_inputs.add(ui)

            if restored_count > 0:
                self.db.commit()
                logger.info(f"Restored {restored_count} matched deleted V2 data records")

        # 导入数据
        count = 0
        update_count = 0

        # 获取当前数据集的最大 index
        max_index = self.db.query(func.max(EvaluationDataV2.index)).filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True
        ).scalar() or -1

        def _try_parse_json_and_merge(value: str, target_dict: dict):
            """尝试解析 JSON 字符串并扁平化合并到目标字典，非 JSON 字符串返回 False"""
            if not value or not isinstance(value, str):
                return False
            value = value.strip()
            if not value.startswith("{"):
                return False
            try:
                import json as _json
                parsed = _json.loads(value)
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        if k not in target_dict:
                            target_dict[k] = v
                    return True
            except (_json.JSONDecodeError, ValueError):
                pass
            return False

        for idx, row in df.iterrows():
            # 构造 V2 扁平 data JSON
            data_dict = {}
            for col in df.columns:
                if col.endswith("_is_media"):
                    # is_media 字段转为布尔值
                    data_dict[col] = _convert_is_media(row.get(col))
                else:
                    value = _convert_to_string(row.get(col, "")).strip()
                    # 如果值是 JSON 字符串，解析并扁平化
                    if not _try_parse_json_and_merge(value, data_dict):
                        data_dict[col] = value

            # 获取 user_input 值用于匹配
            user_input_value = data_dict.get(user_input_col, "") if user_input_col else ""

            # 如果是更新模式，先查找是否存在
            if is_update and user_input_value:
                existing = self.db.query(EvaluationDataV2).filter(
                    EvaluationDataV2.dataset_id == dataset_id,
                    EvaluationDataV2.is_active == True
                ).all()
                existing_data = None
                for item in existing:
                    item_ui = (item.data or {}).get(user_input_col, "")
                    if item_ui == user_input_value:
                        existing_data = item
                        break

                if existing_data:
                    existing_data.data = data_dict
                    flag_modified(existing_data, "data")
                    update_count += 1
                    continue

            # 跳过已恢复的记录
            if user_input_value in restored_user_inputs:
                logger.info(f"Skipping duplicate creation for restored data: {user_input_value[:50]}...")
                continue

            # 创建新 V2 数据
            max_index += 1
            data_id = f"data2_{uuid.uuid4().hex[:12]}"
            data_obj = EvaluationDataV2(
                id=data_id,
                dataset_id=dataset_id,
                data=data_dict,
                index=max_index,
            )
            self.db.add(data_obj)
            count += 1

        # 更新题数
        total_count = self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True
        ).count()
        dataset.question_count = total_count
        self.db.commit()

        return count + update_count

    def import_from_excel_v2(self, dataset_id: str, file_path: str) -> int:
        """从Excel导入评测数据 V2（自由格式）"""
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            raise ValueError("数据集不存在")

        df = pd.read_excel(file_path, sheet_name=0)
        df.columns = [str(col).strip() for col in df.columns.tolist()]

        count = 0
        for idx, row in df.iterrows():
            row_data = {}
            for col in df.columns:
                value = row.get(col)
                if pd.notna(value):
                    row_data[col] = str(value).strip()
                else:
                    row_data[col] = None

            if row_data:
                data_service = DatasetDataService(self.db)
                data_service.add_data_v2(dataset_id, row_data)
                count += 1

        # 更新题数
        total_count = self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True
        ).count()
        dataset.question_count = total_count
        self.db.commit()
        return count

    def import_from_json(self, dataset_id: str, data: List[Dict[str, Any]], restore_matched: bool = False) -> int:
        """从JSON导入评测数据（V2 格式，写入 evaluation_data_v2 表）
        restore_matched: 是否恢复与导入数据内容一致的软删除记录（默认True）
        """
        dataset = self.get_dataset(dataset_id)
        if not dataset:
            raise ValueError("数据集不存在")

        # 收集导入数据的内容用于匹配
        import_data = {}
        for item in data:
            question = str(item.get("question", "")).strip()
            if question:
                import_data[question] = True

        # 如果 restore_matched 为 True，检查并恢复软删除的数据
        restored_count = 0
        if restore_matched and import_data:
            deleted_data = self.db.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == dataset_id,
                EvaluationDataV2.is_active == False
            ).all()

            for item in deleted_data:
                item_ui = (item.data or {}).get("user_input", "")
                if item_ui and item_ui in import_data:
                    item.is_active = True
                    restored_count += 1

            if restored_count > 0:
                self.db.commit()
                logger.info(f"Restored {restored_count} matched deleted V2 data records")

        count = 0
        for idx, item in enumerate(data):
            # 构造 V2 扁平 data JSON
            data_dict = {
                "user_input": str(item.get("question", "")),
                "answer": str(item.get("ref_answer", "")),
            }
            # 合并其他字段（排除 question、ref_answer、meta_data）
            for key, value in item.items():
                if key not in ("question", "ref_answer", "meta_data"):
                    data_dict[key] = value
            # 合并 meta_data 中的字段（如 is_media 标志）
            meta_data = item.get("meta_data")
            if meta_data and isinstance(meta_data, dict):
                for key, value in meta_data.items():
                    if key not in data_dict:
                        data_dict[key] = value

            data_id = f"data2_{uuid.uuid4().hex[:12]}"
            data_obj = EvaluationDataV2(
                id=data_id,
                dataset_id=dataset_id,
                index=idx,
                data=data_dict,
            )
            self.db.add(data_obj)
            count += 1

        # 更新题数
        dataset.question_count = count
        self.db.commit()

        return count

    def upload_data_file(self, dataset_id: str, data_id: str, file: UploadFile, field: str = "answer") -> Dict[str, Any]:
        """上传文件并与具体数据项关联"""
        import os

        # 校验field参数
        if field not in ["user_input", "answer"]:
            field = "answer"

        # 验证数据项存在
        data_service = DatasetDataService(self.db)
        data_item = data_service.get_data(data_id)
        if not data_item:
            raise ValueError("数据项不存在")

        # 创建上传目录
        upload_dir = "./uploads/datasets"
        os.makedirs(upload_dir, exist_ok=True)

        # 生成文件ID和保存路径（包含field信息）
        file_id = f"file_{uuid.uuid4().hex[:12]}"
        file_ext = os.path.splitext(file.filename)[1] if file.filename else ""
        file_path = os.path.join(upload_dir, f"{data_id}_{field}_{file_id}{file_ext}")

        # 保存文件
        content = file.file.read()
        with open(file_path, "wb") as f:
            f.write(content)

        # 获取文件大小
        file_size = os.path.getsize(file_path)

        # 获取文件类型和MIME类型
        file_type = file_ext.lstrip(".") if file_ext else "unknown"

        # 获取MIME类型
        mime_type_map = {
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".json": "application/json",
            ".txt": "text/plain",
            ".csv": "text/csv",
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
        }
        mime_type = mime_type_map.get(file_ext.lower(), "application/octet-stream")

        # 构建文件信息（包含field信息）
        file_info = {
            "id": file_id,
            "file_name": file.filename or "unknown",
            "file_path": file_path,
            "file_type": file_type,
            "mime_type": mime_type,
            "file_size": file_size,
            "field": field  # 记录是哪个字段的文件
        }

        # 调用 DatasetDataService 添加文件到数据项
        data_service.add_file_to_data(data_id, file_info)

        return file_info


class DatasetDataService:
    """评测数据服务"""

    # 列表展示用超大字段替换阈值（字节）：会话数据集的 session_messages 单条
    # 可达几十 MB，超过阈值的字段值在 DB 端替换为占位摘要，避免整行传输/反序列化
    OVERSIZE_FIELD_BYTES = 64 * 1024

    def __init__(self, db: Session):
        self.db = db

    def get_data_list(
        self,
        dataset_id: str,
        skip: int = 0,
        limit: int = 100,
    ) -> List[EvaluationData]:
        """获取评测数据列表"""
        return self.db.query(EvaluationData).filter(
            EvaluationData.dataset_id == dataset_id,
            EvaluationData.is_active == True
        ).order_by(EvaluationData.index).offset(skip).limit(limit).all()

    def get_data(self, data_id: str) -> Optional[EvaluationData]:
        """获取单条评测数据"""
        return self.db.query(EvaluationData).filter(
            EvaluationData.id == data_id
        ).first()

    def get_data_v2(self, data_id: str) -> Optional[EvaluationDataV2]:
        """获取单条评测数据 V2"""
        return self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.id == data_id
        ).first()

    def add_data(
        self,
        dataset_id: str,
        question: str,
        ref_answer: str,
        meta_data: Optional[Dict] = None,
    ) -> EvaluationDataV2:
        """添加评测数据（V2 格式，写入 evaluation_data_v2 表）"""
        # 构造 V2 扁平 data JSON
        data_dict = {
            "user_input": question,
            "answer": ref_answer,
        }
        # 合并 meta_data 中的字段（如 is_media 标志）
        if meta_data and isinstance(meta_data, dict):
            for key, value in meta_data.items():
                if key not in data_dict:
                    data_dict[key] = value

        return self.add_data_v2(dataset_id, data_dict)

    def add_data_v2(
        self,
        dataset_id: str,
        data: Dict[str, Any],
    ) -> EvaluationDataV2:
        """添加评测数据 V2（自由格式）"""
        max_index = self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id
        ).order_by(EvaluationDataV2.index.desc()).first()
        next_index = (max_index.index + 1) if max_index else 0

        data_id = f"data2_{uuid.uuid4().hex[:12]}"
        data_obj = EvaluationDataV2(
            id=data_id,
            dataset_id=dataset_id,
            data=data,
            index=next_index,
        )
        self.db.add(data_obj)
        self.db.commit()
        self.db.refresh(data_obj)
        return data_obj

    def get_data_list_v2(
        self,
        dataset_id: str,
        skip: int = 0,
        limit: int = 100,
    ) -> List[EvaluationDataV2]:
        """获取评测数据列表 V2"""
        return self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True
        ).order_by(EvaluationDataV2.index).offset(skip).limit(limit).all()

    # DB 端瘦身 SQL：data 中序列化后超过 max_bytes 的字段值替换为
    # {"__oversize__": true, "size_bytes": N} 占位摘要，其余字段原样返回
    _SLIM_DATA_SQL = sa.text("""
        SELECT e.id, e."index",
               COALESCE((
                   SELECT jsonb_object_agg(kv.key,
                       CASE WHEN pg_column_size(kv.value) > :max_bytes
                            THEN jsonb_build_object(
                                '__oversize__', true,
                                'size_bytes', pg_column_size(kv.value))
                            ELSE kv.value END)
                   FROM jsonb_each(e.data::jsonb) AS kv
               ), '{}'::jsonb) AS data
        FROM evaluation_data_v2 e
        WHERE e.dataset_id = :ds_id AND e.is_active
        ORDER BY e."index"
        OFFSET :skip LIMIT :lim
    """)

    def get_data_list_v2_slim(
        self,
        dataset_id: str,
        skip: int = 0,
        limit: int = 100,
        max_field_bytes: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取评测数据列表 V2（列表展示用，DB 端瘦身超大字段）

        与 get_data_list_v2 的区别：data 内超过 max_field_bytes 的字段值被
        替换为占位摘要对象。前端编辑保存时会剔除占位字段，update_data_v2
        的合并语义保证库中原值不被覆盖。评测执行/快照需全量数据，
        仍走 get_data_list_v2。
        """
        if max_field_bytes is None:
            max_field_bytes = self.OVERSIZE_FIELD_BYTES
        rows = self.db.execute(
            self._SLIM_DATA_SQL,
            {"ds_id": dataset_id, "skip": skip, "lim": limit, "max_bytes": max_field_bytes},
        ).mappings().all()
        return [dict(row) for row in rows]

    def count_active_v2(self, dataset_id: str) -> int:
        """统计数据集的有效题目数（V2）"""
        return self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True
        ).count()

    def get_all_active_v2(self, dataset_id: str) -> List[EvaluationDataV2]:
        """获取数据集全部有效数据 V2（不分页，供导出使用）"""
        return self.db.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id,
            EvaluationDataV2.is_active == True
        ).order_by(EvaluationDataV2.index).all()

    def get_all_active_v1(self, dataset_id: str) -> List[EvaluationData]:
        """获取数据集全部有效数据 V1（不分页，供导出兼容旧数据集使用）"""
        return self.db.query(EvaluationData).filter(
            EvaluationData.dataset_id == dataset_id,
            EvaluationData.is_active == True
        ).order_by(EvaluationData.index).all()

    def add_file_to_data(self, data_id: str, file_info: Dict[str, Any]) -> EvaluationDataV2:
        """为数据项添加文件关联（V2）"""
        from sqlalchemy.orm.attributes import flag_modified

        data = self.get_data_v2(data_id)
        if not data:
            raise ValueError("数据项不存在")

        # 获取field信息，默认为answer
        field = file_info.get('field', 'answer')
        if field not in ["user_input", "answer"]:
            field = "answer"

        # 获取当前 data JSON
        data_json = dict(data.data) if data.data else {}

        # 根据field添加到对应的文件列表
        files_key = f'{field}_files'
        files_list = data_json.get(files_key, [])

        # 添加文件信息到列表
        files_list.append(file_info)
        data_json[files_key] = files_list

        # 同时更新files字段（保持兼容）
        if field == 'answer':
            data_json['files'] = files_list

        # 更新 V2 data JSON
        data.data = data_json
        # 显式标记 JSON 字段已修改
        flag_modified(data, "data")
        self.db.commit()
        self.db.refresh(data)

        return data

    def update_data(
        self,
        data_id: str,
        **kwargs,
    ) -> Optional[EvaluationDataV2]:
        """更新评测数据（V2 格式）"""
        from sqlalchemy.orm.attributes import flag_modified

        # 如果包含 data 字段，说明是 V2 格式更新
        if 'data' in kwargs:
            return self.update_data_v2(data_id, kwargs.pop('data'), **kwargs)

        # 尝试从 V2 表获取
        data = self.get_data_v2(data_id)
        if data:
            # 将 V1 格式的更新参数转化为 V2 data JSON 更新
            data_dict = dict(data.data) if data.data else {}

            # 清理旧 OSS URL 缓存
            try:
                from app.services.oss_media_cache_service import OSSMediaCacheService
                oss_service = OSSMediaCacheService()
                if 'question' in kwargs:
                    old_val = data_dict.get("user_input", "")
                    if old_val and oss_service.validate_oss_url(old_val):
                        oss_service.invalidate_cache(old_val)
                if 'ref_answer' in kwargs:
                    old_val = data_dict.get("answer", "")
                    if old_val and oss_service.validate_oss_url(old_val):
                        oss_service.invalidate_cache(old_val)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"清理数据行媒体缓存失败: {data_id}, 错误: {e}")

            # V1 字段映射到 V2 data JSON
            if 'question' in kwargs:
                data_dict["user_input"] = kwargs.pop('question')
            if 'ref_answer' in kwargs:
                data_dict["answer"] = kwargs.pop('ref_answer')
            # 合并 meta_data 中的字段到 data JSON
            if 'meta_data' in kwargs and kwargs['meta_data'] is not None:
                new_meta = kwargs.pop('meta_data')
                if isinstance(new_meta, dict):
                    for key, value in new_meta.items():
                        data_dict[key] = value

            data.data = data_dict
            flag_modified(data, "data")

            # 更新数据集的 updated_at
            if data.dataset_id:
                dataset = self.db.query(EvaluationDataset).filter(
                    EvaluationDataset.id == data.dataset_id
                ).first()
                if dataset:
                    dataset.updated_at = func.now()

            self.db.commit()
            self.db.refresh(data)
            return data

        return None

        self.db.commit()
        self.db.refresh(data)
        return data

    def _is_patchable_field(self, key: str, value: Any) -> bool:
        """补丁式更新条件：值为可序列化的小 JSON（大值需整行替换语义，走原路径）"""
        try:
            size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            return False
        return size <= self.OVERSIZE_FIELD_BYTES

    def _update_data_v2_patch(self, data_id: str, data: Dict[str, Any]):
        """顶层合并补丁式更新：只提交小字段时以 data::jsonb || patch 一次合并，
        不把行内大 JSON 字段读到应用层（会话数据集单条可达几十 MB，整行 ORM
        读写使编辑保存耗时秒级）。合并语义与原路径一致（新键创建、已有键覆盖）。
        """
        from types import SimpleNamespace

        row = self.db.execute(sa.text("""
            UPDATE evaluation_data_v2
            SET data = CAST(data::jsonb || CAST(:patch AS jsonb) AS json),
                updated_at = now()
            WHERE id = :data_id
            RETURNING id, "index", dataset_id
        """), {
            "data_id": data_id,
            "patch": json.dumps(data, ensure_ascii=False, default=str),
        }).mappings().first()
        if row is None:
            return None

        # 数据集 updated_at 同步（与整行路径一致）
        dataset = self.db.query(EvaluationDataset).filter(
            EvaluationDataset.id == row["dataset_id"]
        ).first()
        if dataset:
            dataset.updated_at = func.now()

        self.db.commit()
        # 返回轻量结果（API 层只读 id/index/data；响应前端不消费完整 data，
        # 避免为构造返回值回读整行大 JSON）
        return SimpleNamespace(id=row["id"], index=row["index"], data=data)

    def update_data_v2(
        self,
        data_id: str,
        data: Dict[str, Any],
        **kwargs,
    ) -> Optional[EvaluationDataV2]:
        """更新评测数据 V2（自由格式）

        全部为小字段时走 jsonb_set 补丁式更新；提交大值（如替换 session_messages）
        或键名不常规时回退整行合并路径。
        """
        from sqlalchemy.orm.attributes import flag_modified

        if (
            isinstance(data, dict) and data
            and all(self._is_patchable_field(k, v) for k, v in data.items())
        ):
            return self._update_data_v2_patch(data_id, data)

        data_obj = self.get_data_v2(data_id)
        if not data_obj:
            return None

        # 更新 data 字段
        current_data = dict(data_obj.data) if data_obj.data else {}
        merged_data = {**current_data, **data}
        data_obj.data = merged_data

        # 显式标记 JSON 字段已修改
        flag_modified(data_obj, "data")

        # 处理 meta_data（如果有）
        if 'meta_data' in kwargs and kwargs['meta_data'] is not None:
            new_meta = kwargs.pop('meta_data')
            current_meta = dict(data_obj.meta_data) if data_obj.meta_data else {}
            existing_files = current_meta.get('files', [])
            merged_meta = {**current_meta, **new_meta}
            if existing_files and 'files' not in new_meta:
                merged_meta['files'] = existing_files
            data_obj.meta_data = merged_meta
            flag_modified(data_obj, "meta_data")

        # 更新数据集的updated_at
        if data_obj.dataset_id:
            dataset = self.db.query(EvaluationDataset).filter(
                EvaluationDataset.id == data_obj.dataset_id
            ).first()
            if dataset:
                dataset.updated_at = func.now()

        self.db.commit()
        self.db.refresh(data_obj)
        return data_obj

    def delete_data(self, data_id: str) -> bool:
        """删除评测数据（V2）"""
        data = self.get_data_v2(data_id)
        if not data:
            return False

        # 清理媒体文件 Redis 缓存
        try:
            from app.services.oss_media_cache_service import OSSMediaCacheService
            oss_service = OSSMediaCacheService()
            data_json = data.data or {}
            user_input_is_media = data_json.get("user_input_is_media")
            if user_input_is_media in [True, 'true', 'True', '1', 1]:
                user_input = data_json.get("user_input", "")
                if user_input and oss_service.validate_oss_url(user_input):
                    oss_service.invalidate_cache(user_input)
            answer_is_media = data_json.get("answer_is_media")
            if answer_is_media in [True, 'true', 'True', '1', 1]:
                answer = data_json.get("answer", "")
                if answer and oss_service.validate_oss_url(answer):
                    oss_service.invalidate_cache(answer)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"清理数据行媒体缓存失败: {data_id}, 错误: {e}")

        # 数据集迭代阶段C：删除前按 hub_file 字段收集执行包引用，软删成功后释放
        # （末引用 ref_count→0 时清理解包目录与登记记录）
        hub_file_ids: List[str] = []
        try:
            from app.services.hub_file_service import collect_row_hub_file_ids, get_hub_file_fields
            hub_fields = get_hub_file_fields(self.db, data.dataset_id)
            hub_file_ids = collect_row_hub_file_ids(data.data or {}, hub_fields)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"收集数据行执行包引用失败: {data_id}, 错误: {e}")

        data.is_active = False
        self.db.commit()

        if hub_file_ids:
            try:
                from app.services.hub_file_service import release_hub_refs
                release_hub_refs(self.db, hub_file_ids)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"释放数据行执行包引用失败: {data_id}, 错误: {e}")
        return True

    def get_data_files(self, data_id: str, field: str = None) -> List[Dict[str, Any]]:
        """获取数据项关联的文件列表（V2）"""
        data = self.get_data_v2(data_id)
        if not data:
            return []

        data_json = data.data or {}
        answer_is_media = data_json.get('answer_is_media', False)
        user_input_is_media = data_json.get('user_input_is_media', False)

        # 根据 field 参数返回对应字段的文件
        if field:
            files = data_json.get(f'{field}_files', [])
            if not files and 'files' in data_json:
                all_files = data_json.get('files', [])
                if field == 'answer' and answer_is_media:
                    files = all_files
                elif field == 'user_input' and user_input_is_media:
                    files = all_files
                else:
                    files = []
        else:
            user_input_files = data_json.get('user_input_files', [])
            answer_files = data_json.get('answer_files', [])
            all_files = data_json.get('files', [])
            files = user_input_files + answer_files + all_files
        return files

    def delete_data_file(self, data_id: str, file_id: str, field: str = "answer") -> bool:
        """删除数据项关联的文件（V2）"""
        from sqlalchemy.orm.attributes import flag_modified

        # 校验field参数
        if field not in ["user_input", "answer"]:
            field = "answer"

        data = self.get_data_v2(data_id)
        if not data:
            return False

        # 从 V2 data JSON 中获取文件列表
        data_json = dict(data.data) if data.data else {}

        files_key = f'{field}_files'
        files = data_json.get(files_key, [])

        # 如果 field_files 为空，检查旧的 files 字段
        if not files and 'files' in data_json:
            files = data_json.get('files', [])

        # 查找并删除文件
        updated_files = [f for f in files if f.get('id') != file_id]

        if len(updated_files) == len(files):
            return False

        # 更新 field_files
        data_json[files_key] = updated_files
        # 同时更新 files 字段（保持兼容）
        if field == 'answer':
            data_json['files'] = updated_files
        data.data = data_json
        # 显式标记 JSON 字段已修改
        flag_modified(data, "data")
        self.db.commit()
        return True
