"""
数据集字段元数据服务
"""
import uuid
import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

logger = logging.getLogger(__name__)

# 数据集字段元数据中表示 hub_file 通道的字段类型（与 app/services/hub_file_service.py
# 的 HUB_FIELD_TYPE 保持一致）：字段值为 anc-hub file_id，评测执行时自动下载落盘
HUB_FIELD_TYPE = "hub_file"


def merge_field_metadata(existing: List[Dict[str, Any]], base: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """以 base 为准合并已有字段元数据，保留 existing 中 base 未覆盖的字段

    save_field_metadata 是全删重建——向已有数据集追加导入（excel/会话/执行包）
    若直接用本次列全量重建，会丢掉此前其它通道回填的字段元数据
    （如 session_file_id(hub_file)，执行包「文件」按钮与评测 hub 通道依赖它）。
    """
    known = {f.get("name") for f in base}
    merged = list(base)
    for f in existing or []:
        name = f.get("name")
        if name and name not in known:
            merged.append(f)
            known.add(name)
    return merged


class DatasetFieldService:
    """数据集字段服务"""

    # 旧版兼容字段（用于字段名标准化）
    LEGACY_FIELDS = {"question": "user_input", "ref_answer": "answer"}
    # 最大自定义字段数量
    MAX_CUSTOM_FIELDS = 20
    # 字段名合法性正则（支持任意字符作为后缀，支持中文等多语言）
    FIELD_NAME_PATTERN = re.compile(r'^[a-zA-Z_\u4e00-\u9fff][^\s]{0,63}$')

    def __init__(self, db: Session):
        self.db = db

    def parse_fields_from_excel(self, file_path: str) -> Dict[str, Any]:
        """从Excel解析字段结构

        辅助列约定（均不作为数据字段落库）：
        - xxx_is_media：填 是/true 表示 xxx 关联 OSS 附件
        - xxx_hub_file：填 是/true 表示 xxx 的值为 anc-hub file_id，
          字段类型标记为 hub_file，评测执行时自动下载落盘（设计文档五/决策 #1）

        Returns:
            {
                "fields": [
                    {"name": "user_input", "label": "用户输入", "type": "string",
                     "is_media": False, "is_media_field": "user_input_is_media", "is_required": False},
                    ...
                ],
                "custom_fields_count": 5,
                "total_fields_count": 9
            }
        """
        import pandas as pd

        df = pd.read_excel(file_path, sheet_name=0)
        # 清理列名中的前后空格
        columns = [str(col).strip() for col in df.columns.tolist()]
        df.columns = columns

        fields = []
        is_media_bindings = self._auto_bind_is_media_fields(columns)
        hub_file_bindings = self._auto_bind_hub_file_fields(columns)

        for col in columns:
            # 字段名合法性校验
            if not self._validate_field_name(col):
                logger.warning(f"字段名不合法，跳过: {col}")
                continue

            # 标准化字段名
            normalized_name = self._normalize_field_name(col)

            # 判断是否为 is_media 字段
            is_media_field = col.endswith("_is_media")

            # 推断字段类型
            field_type = self._infer_field_type(df[col].dropna().tolist()[:100])  # 最多取100条推断

            # 获取显示名
            label = self._get_field_label(normalized_name, col)

            field_info = {
                "name": col,
                "label": label,
                "type": field_type,
                "is_media": False,  # 主字段默认非媒体
                "is_media_field": None,
                "is_required": False,
                "position": len(fields)
            }

            # 如果是主字段且有对应的 is_media 字段
            if not is_media_field and col in is_media_bindings:
                field_info["is_media"] = self._check_is_media_values(df[col + "_is_media"])
                field_info["is_media_field"] = col + "_is_media"

            # hub_file 辅助列绑定（如 cases_file → cases_file_hub_file 列填 是/true）：
            # 字段值填 anc-hub file_id，评测执行时自动分块下载落盘（设计文档五/决策 #1）。
            # hub_file 字段走本地路径落盘通道，强制关闭 is_media（避免值被当作 OSS URL 置空）
            if not is_media_field and col in hub_file_bindings \
                    and self._check_is_media_values(df[col + "_hub_file"]):
                field_info["type"] = HUB_FIELD_TYPE
                field_info["is_media"] = False
                field_info["is_media_field"] = None

            fields.append(field_info)

        # 分离主字段和 is_media 辅助字段（_hub_file 辅助列仅作为类型标记，不落库）
        aux_suffixes = ("_is_media", "_hub_file")
        main_fields = [f for f in fields if not f["name"].endswith(aux_suffixes)]
        media_fields = [f for f in fields if f["name"].endswith("_is_media")]

        # 统计自定义字段数量（非 is_media 的主字段）
        custom_fields_count = len(main_fields)

        return {
            "fields": main_fields,
            "media_fields": media_fields,
            "custom_fields_count": custom_fields_count,
            "total_fields_count": len(main_fields)
        }

    def _validate_field_name(self, name: str) -> bool:
        """校验字段名合法性"""
        if not name:
            return False
        # 跳过 is_media 辅助字段（它们通过主字段绑定）
        if name.endswith("_is_media"):
            return True
        return bool(self.FIELD_NAME_PATTERN.match(name))

    def _normalize_field_name(self, name: str) -> str:
        """标准化字段名"""
        return self.LEGACY_FIELDS.get(name, name)

    def _auto_bind_is_media_fields(self, columns: List[str]) -> Dict[str, str]:
        """自动绑定 is_media 字段

        例如: custom_field → custom_field_is_media
        Returns: {'custom_field': 'custom_field_is_media', ...}
        """
        bindings = {}
        for col in columns:
            if col.endswith("_is_media"):
                main_field = col[:-9]  # 去掉 _is_media 后缀
                if main_field in columns:
                    bindings[main_field] = col
        return bindings

    def _auto_bind_hub_file_fields(self, columns: List[str]) -> Dict[str, str]:
        """自动绑定 hub_file 辅助列（设计文档五 file_id 配置方式之一：Excel 列）

        例如: cases_file → cases_file_hub_file（该列填 是/true 表示字段值为 anc-hub file_id）
        Returns: {'cases_file': 'cases_file_hub_file', ...}
        """
        bindings = {}
        for col in columns:
            if col.endswith("_hub_file"):
                main_field = col[:-9]  # 去掉 _hub_file 后缀（9 个字符）
                if main_field in columns:
                    bindings[main_field] = col
        return bindings

    def _infer_field_type(self, values: List[Any]) -> str:
        """推断字段类型"""
        if not values:
            return "string"

        # 检查是否全为数值
        numeric_count = 0
        for v in values:
            try:
                float(str(v))
                numeric_count += 1
            except (ValueError, TypeError):
                pass

        if numeric_count == len(values):
            return "number"

        # 检查是否全为布尔值
        bool_values = {"true", "false", "是", "否", "yes", "no", "1", "0"}
        bool_count = 0
        for v in values:
            if str(v).lower().strip() in bool_values:
                bool_count += 1

        if bool_count == len(values):
            return "boolean"

        return "string"

    def _check_is_media_values(self, values: List[Any]) -> bool:
        """检查 is_media 列是否有 true 值"""
        bool_values = {"true", "是", "yes", "1"}
        # 如果是 pandas Series，先转成 list
        values_list = values.tolist() if hasattr(values, 'tolist') else values
        for v in values_list:
            if v is None:
                continue
            if str(v).lower().strip() in bool_values:
                return True
        return False

    def _get_field_label(self, normalized_name: str, original_name: str) -> str:
        """获取字段显示名

        支持动态后缀转换：
        - user_input -> 用户输入
        - user_input_xxx -> 用户输入_xxx
        - answer -> 参考答案
        - answer_xxx -> 参考答案_xxx
        """
        labels = {
            "user_input": "用户输入",
            "answer": "参考答案",
            "question": "用户输入",
            "ref_answer": "参考答案"
        }
        if normalized_name in labels:
            return labels[normalized_name]
        # 动态处理 user_input_xxx -> 用户输入_xxx
        if normalized_name.startswith("user_input_"):
            suffix = normalized_name[11:]  # 去掉 "user_input_" 前缀
            return f"用户输入_{suffix}"
        # 动态处理 answer_xxx -> 参考答案_xxx
        if normalized_name.startswith("answer_"):
            suffix = normalized_name[7:]  # 去掉 "answer_" 前缀
            return f"参考答案_{suffix}"
        return original_name

    def save_field_metadata(self, dataset_id: str, fields: List[Dict[str, Any]]) -> None:
        """保存字段元数据到数据库

        Args:
            dataset_id: 数据集ID
            fields: 字段列表
        """
        from app.models.dataset_models import DatasetFieldMetadata

        # 删除旧的字段元数据
        self.db.query(DatasetFieldMetadata).filter(
            DatasetFieldMetadata.dataset_id == dataset_id
        ).delete()

        # 插入新的字段元数据
        for field in fields:
            field_id = f"dfm_{uuid.uuid4().hex[:12]}"
            meta = DatasetFieldMetadata(
                id=field_id,
                dataset_id=dataset_id,
                field_name=field["name"],
                field_label=field.get("label", field["name"]),
                field_type=field.get("type", "string"),
                is_media=field.get("is_media", False),
                is_media_field=field.get("is_media_field"),
                is_required=field.get("is_required", True),
                position=field.get("position", 0)
            )
            self.db.add(meta)

        self.db.commit()

    def get_field_metadata(self, dataset_id: str) -> List[Dict[str, Any]]:
        """获取数据集字段元数据"""
        from app.models.dataset_models import DatasetFieldMetadata

        fields = self.db.query(DatasetFieldMetadata).filter(
            DatasetFieldMetadata.dataset_id == dataset_id
        ).order_by(DatasetFieldMetadata.position).all()

        return [
            {
                "name": f.field_name,
                "label": f.field_label,
                "type": f.field_type,
                "is_media": f.is_media,
                "is_media_field": f.is_media_field,
                "is_required": f.is_required,
                "position": f.position
            }
            for f in fields
        ]

    def save_field_metadata_merged(self, dataset_id: str, fields: List[Dict[str, Any]]) -> None:
        """合并保存字段元数据：以本次 fields 为准，保留数据集已有中本次未覆盖的字段

        追加导入（excel/会话/执行包）统一走此方法；对全新数据集（无已有元数据）
        等价于 save_field_metadata 全量保存。
        """
        merged = merge_field_metadata(self.get_field_metadata(dataset_id), fields)
        self.save_field_metadata(dataset_id, merged)

    def validate_fields(self, dataset_id: str, file_path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """校验数据，返回有效和无效记录

        Returns:
            (valid_items, invalid_items)
            invalid_items: [{"row": 2, "field": "user_input", "error": "不能为空"}, ...]
        """
        import pandas as pd

        from app.models.dataset_models import DatasetFieldMetadata

        # 获取字段元数据
        field_metas = self.db.query(DatasetFieldMetadata).filter(
            DatasetFieldMetadata.dataset_id == dataset_id
        ).order_by(DatasetFieldMetadata.position).all()

        # 读取Excel
        df = pd.read_excel(file_path, sheet_name=0)

        valid_items = []
        invalid_items = []

        # 获取所有列名
        columns = df.columns.tolist()

        for idx, row in df.iterrows():
            row_num = idx + 2  # Excel行号从2开始（1是表头）
            row_errors = []

            # 获取该行的数据
            item_data = {}
            for col in columns:
                value = row.get(col)
                if pd.notna(value):
                    item_data[col] = str(value).strip()
                else:
                    item_data[col] = None

            # 校验必填字段
            for meta in field_metas:
                if meta.is_required:
                    value = item_data.get(meta.field_name)
                    if not value or str(value).strip() == "":
                        row_errors.append({
                            "row": row_num,
                            "field": meta.field_name,
                            "error": f"{meta.field_label or meta.field_name}不能为空"
                        })

            if row_errors:
                invalid_items.extend(row_errors)
            else:
                valid_items.append({
                    "row_num": row_num,
                    "data": item_data
                })

        return valid_items, invalid_items

    def validate_import_data(self, df, fields: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """校验导入数据

        Args:
            df: pandas DataFrame
            fields: 字段列表（含 is_required 信息）

        Returns:
            (valid_items, errors)
            valid_items: [{"row_num": 2, "data": {...}}, ...]
            errors: [{"row": 2, "field": "xxx", "error": "..."}, ...]
        """
        valid_items = []
        errors = []

        # 构建必填字段映射
        required_fields = {f["name"]: f for f in fields if f.get("is_required", False)}

        for idx, row in df.iterrows():
            row_num = idx + 2
            row_errors = []

            # 获取该行的数据
            item_data = {}
            for col in df.columns:
                value = row.get(col)
                if pd.notna(value):
                    item_data[col] = str(value).strip()
                else:
                    item_data[col] = None

            # 校验必填字段
            for field_name, field_info in required_fields.items():
                value = item_data.get(field_name)
                if not value or str(value).strip() == "":
                    row_errors.append({
                        "row": row_num,
                        "field": field_name,
                        "error": f"{field_info.get('label', field_name)}不能为空"
                    })

            if row_errors:
                errors.extend(row_errors)
            else:
                valid_items.append({
                    "row_num": row_num,
                    "data": item_data
                })

        return valid_items, errors

    def check_custom_fields_limit(self, custom_fields_count: int) -> bool:
        """检查自定义字段数量是否超限

        Returns:
            True if within limit, False if exceeded
        """
        return custom_fields_count <= self.MAX_CUSTOM_FIELDS

    def flatten_data_item(self, item, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
        """将数据项展平为字典格式，支持 JSON 格式字段

        将 question/ref_answer 中的 JSON 对象展开为多个字段，
        确保 VariableInjector 收到的 dataset_item 包含所有字段。

        Args:
            item: EvaluationData 对象
            fields: 字段元数据列表

        Returns:
            展平后的数据字典
        """
        import json
        result = {}

        # 获取字段名映射（原始名 -> 标准化名）
        field_name_map = {}
        for f in fields:
            name = f.get("name", "")
            normalized = self._normalize_field_name(name)
            field_name_map[name] = normalized
            field_name_map[normalized] = normalized

        # 处理 question 字段（支持 JSON 格式）
        question = item.question or ""
        try:
            parsed = json.loads(question)
            if isinstance(parsed, dict):
                # JSON 格式: {"user_input": "值1", "user_input_2": "值2"}
                result.update(parsed)
                result["_question_json"] = parsed
            else:
                # 非字典类型，当作普通字符串处理
                result["user_input"] = str(parsed) if parsed else ""
        except (json.JSONDecodeError, TypeError):
            # 普通字符串或无法解析，直接作为 user_input
            result["user_input"] = question

        # 处理 ref_answer 字段（支持 JSON 格式）
        ref_answer = item.ref_answer or ""
        try:
            parsed = json.loads(ref_answer)
            if isinstance(parsed, dict):
                # JSON 格式: {"answer": "值1", "answer_2": "值2"}
                result.update(parsed)
                result["_ref_answer_json"] = parsed
            else:
                # 非字典类型，当作普通字符串处理
                result["ref_answer"] = str(parsed) if parsed else ""
        except (json.JSONDecodeError, TypeError):
            # 普通字符串或无法解析，直接作为 ref_answer
            result["ref_answer"] = ref_answer

        # 添加 meta_data 中的字段（但避免覆盖已处理的字段）
        if item.meta_data:
            meta = item.meta_data
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if isinstance(meta, dict):
                # 添加 media 相关字段（包括多字段的 is_media 标志）
                for key, value in meta.items():
                    # 跳过已处理的字段
                    if key in result and key not in ("user_input_is_media", "answer_is_media"):
                        continue
                    # 处理固定的 is_media 字段
                    if key in ("user_input_is_media", "answer_is_media"):
                        if key not in result:
                            result[key] = value
                        continue
                    # 处理多字段的 is_media 标志（如 user_input_2_is_media, answer_2_is_media）
                    if key.endswith("_is_media"):
                        # 提取主字段名（如 user_input_2 -> user_input_2）
                        main_field = key[:-9]  # 去掉 _is_media 后缀
                        # 如果主字段存在于结果中，对应的 is_media 也应该加入
                        # 但 is_media 字段本身需要保留，用于后续处理
                        result[key] = value
                    else:
                        # 其他字段
                        if key not in result:
                            result[key] = value

        # 添加 index 和 id
        result["index"] = item.index
        result["id"] = item.id

        return result

    def get_nested_value(self, data: Dict[str, Any], path: str) -> Any:
        """从嵌套字典中按路径取值

        Args:
            data: 字典对象
            path: 点号分隔路径，如 "user.name" 或 "answers[0]"

        Returns:
            路径对应的值，不存在返回 None
        """
        keys = path.split(".")
        current = data
        for key in keys:
            # 处理数组索引，如 "answers[0]"
            match = re.match(r'^(\w+)\[(\d+)\]$', key)
            if match:
                array_key = match.group(1)
                index = int(match.group(2))
                if array_key in current and isinstance(current[array_key], list):
                    if index < len(current[array_key]):
                        current = current[array_key][index]
                    else:
                        return None
                else:
                    return None
            else:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return None
        return current

    def flatten_data_item_v2(self, item, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
        """将 V2 数据项展平为字典格式

        直接返回 item.data 中的所有字段，加上 index 和 id。
        支持嵌套字段路径（如 "user.name"）。

        Args:
            item: EvaluationDataV2 对象
            fields: 字段元数据列表

        Returns:
            展平后的数据字典
        """
        result = {}

        # 从 data JSON 中取所有字段
        data = item.data or {}

        # 遍历字段元数据，按位置顺序加入
        for f in fields:
            name = f.get("name", "")
            if name in data:
                result[name] = data[name]

        # 补充 data 中有但元数据没有的字段（自由字段）
        for key, value in data.items():
            if key not in result:
                result[key] = value

        # 添加 index 和 id
        result["index"] = item.index
        result["id"] = item.id

        return result
