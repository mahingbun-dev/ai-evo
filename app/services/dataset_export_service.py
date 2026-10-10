"""
数据集导出服务

将数据集导出为可再次导入的 Excel 文件，形成「导出 → 批量修改 → 回灌」闭环。

设计约束（由导入侧逻辑决定）：
- 表头必须使用 field_name（技术字段名），因为导入校验严格按列名匹配
  （见 DatasetFieldService.parse_fields_from_excel）
- 媒体字段原样输出 OSS URL 文本，不嵌图、不转超链接——导入校验要求媒体列值
  匹配 hashDownload URL 正则，嵌图或超链接都会破坏回灌能力
- `xxx_is_media` 辅助列必须紧跟其主字段，导入侧靠列名后缀自动建立绑定
- 不输出 id / index 列：导入侧会把 Excel 中每一列都当作新字段写进字段元数据，
  多余列会污染数据集结构
"""

import io
import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy.orm import Session

from app.services.dataset_field_service import DatasetFieldService
from app.services.dataset_service import DatasetDataService

logger = logging.getLogger(__name__)

# 单次同步导出的行数上限（超过则要求用户拆分，避免打满 uvicorn worker 内存）
MAX_EXPORT_ROWS = 10000

# Excel sheet 名
SHEET_DATA = "数据集数据"
SHEET_FIELDS = "字段说明"

# 旧版数据集（无字段元数据）的降级列定义，与 /download-template 完全一致
LEGACY_FIELDS: List[Dict[str, Any]] = [
    {
        "name": "user_input", "label": "用户输入", "type": "string",
        "is_media": False, "is_media_field": "user_input_is_media", "is_required": False,
    },
    {
        "name": "answer", "label": "参考答案", "type": "string",
        "is_media": False, "is_media_field": "answer_is_media", "is_required": False,
    },
]

IS_MEDIA_COLUMN_SUFFIX = "_is_media"

# 数据 JSON 中的内部记账字段，不是用户可见的业务字段
#   files / *_files —— 媒体附件清单，由导入确认与文件上传流程写入
#                     （见 dataset_api 的 data_json['user_input_files'] 等）
#   multi_*_fields —— 历史版本遗留的复合字段标记，当前代码已无引用
# 仅在字段元数据缺失、需要从数据键推导列结构时用于排除
INTERNAL_DATA_KEYS = {"files", "multi_question_fields", "multi_answer_fields"}
INTERNAL_DATA_KEY_SUFFIXES = ("_files",)


class DatasetExportTooLargeError(ValueError):
    """导出数据量超过上限"""


def to_bool_media(value: Any) -> bool:
    """把数据库/JSON 中各种形态的 is_media 值统一成布尔值"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "yes", "1", "是")


def is_media_text(value: Any) -> str:
    """is_media 列在 Excel 中的取值：是 / 否（导入侧接受「是」，且与模板一致）"""
    return "是" if to_bool_media(value) else "否"


def _stringify(value: Any) -> Any:
    """把 Excel 无法直接写入的复合类型（list/dict）序列化为 JSON 文本

    V2 是自由格式，JSON 导入路径允许字段值为数组/对象。openpyxl 遇到这类值会
    直接抛异常，因此需要显式降级；标量保持不变以保留原始类型。
    """
    if value is None or isinstance(value, (str, int, float, bool, date, datetime)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class DatasetExportService:
    """数据集导出服务"""

    def __init__(self, db: Session):
        self.db = db
        self.field_service = DatasetFieldService(db)
        self.data_service = DatasetDataService(db)

    # ------------------------------------------------------------------
    # 列结构解析
    # ------------------------------------------------------------------
    def _media_column_of(self, field: Dict[str, Any]) -> Optional[str]:
        """返回主字段配套的 is_media 列名；该字段无需 is_media 列时返回 None

        规则（与导入侧的 _auto_bind_is_media_fields 对齐，保证回灌后字段结构不变）：
        - 元数据里已绑定 is_media_field → 沿用绑定名
        - 元数据未绑定但 is_media 为真 → 按约定补一个 <name>_is_media
        - 两者都没有 → 不输出该列，回灌时才会保持 is_media_field 为空
        """
        name = field.get("name") or ""
        if not name or name.endswith(IS_MEDIA_COLUMN_SUFFIX):
            return None
        bound = field.get("is_media_field")
        if bound:
            return bound
        if to_bool_media(field.get("is_media")):
            return f"{name}{IS_MEDIA_COLUMN_SUFFIX}"
        return None

    @staticmethod
    def _version_1_fields() -> List[Dict[str, Any]]:
        return [dict(f) for f in LEGACY_FIELDS]

    def _derive_fields_from_data(self, items: List[Any]) -> List[Dict[str, Any]]:
        """字段元数据缺失时，从数据 JSON 的键推导列结构（保序）

        会跳过 is_media 辅助键与内部记账键；若推导不出任何业务字段，
        回落到四列模板，避免产出零列的 Excel。
        """
        ordered_keys: List[str] = []
        first_row: Dict[str, Any] = {}
        for item in items:
            for key, value in (item.data or {}).items():
                if key not in first_row:
                    first_row[key] = value
                if key not in ordered_keys:
                    ordered_keys.append(key)

        fields: List[Dict[str, Any]] = []
        for key in ordered_keys:
            if self._is_internal_key(key):
                continue
            media_key = f"{key}{IS_MEDIA_COLUMN_SUFFIX}"
            fields.append({
                "name": key,
                "label": key,
                "type": "string",
                "is_media": to_bool_media(first_row.get(media_key)),
                "is_media_field": media_key if media_key in first_row else None,
                "is_required": False,
            })

        if not fields:
            return self._version_1_fields()
        return fields

    @staticmethod
    def _is_internal_key(key: str) -> bool:
        """判断数据键是否为内部记账字段（不作为业务列导出）"""
        if key.endswith(IS_MEDIA_COLUMN_SUFFIX):
            return True
        if key in INTERNAL_DATA_KEYS:
            return True
        return key.endswith(INTERNAL_DATA_KEY_SUFFIXES)

    def _resolve_column_names(self, fields: List[Dict[str, Any]]) -> List[str]:
        """把字段列表摊平成 Excel 列名列表：主字段后紧跟其 is_media 列"""
        columns: List[str] = []
        for field in fields:
            name = field.get("name")
            if not name or name in columns:
                continue
            columns.append(name)
            media_column = self._media_column_of(field)
            if media_column and media_column not in columns:
                columns.append(media_column)
        return columns

    # ------------------------------------------------------------------
    # 数据收集
    # ------------------------------------------------------------------
    def _collect(self, dataset_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """返回 (字段定义, 数据行)

        三条路径，按优先级：
        1. 有字段元数据 → V2 自由格式（正常数据集）
        2. 无元数据但有 V2 数据 → 从数据键推导列结构（元数据丢失的老 V2 数据集）
        3. 无元数据也无 V2 数据 → V1 老数据集或空数据集，降级为四列模板
        """
        fields = self.field_service.get_field_metadata(dataset_id)
        v2_items = self.data_service.get_all_active_v2(dataset_id)

        if not fields and v2_items:
            logger.warning("数据集 %s 缺少字段元数据，从数据键推导列结构", dataset_id)
            fields = self._derive_fields_from_data(v2_items)

        if fields or v2_items:
            if len(v2_items) > MAX_EXPORT_ROWS:
                raise DatasetExportTooLargeError(
                    f"数据集共 {len(v2_items)} 条数据，超过单次导出上限 {MAX_EXPORT_ROWS} 条，"
                    f"请拆分数据集后分批导出"
                )

            # declared 要同时收主字段名与其绑定的 is_media 列名：
            # is_media 列虽然不作为独立字段存在，但它确实会被导出，
            # 只收主字段名会把它们误报成"未声明的字段"。
            declared = set()
            for f in fields:
                if f.get("name"):
                    declared.add(f["name"])
                if f.get("is_media_field"):
                    declared.add(f["is_media_field"])

            records: List[Dict[str, Any]] = []
            undeclared_seen: set = set()
            for item in v2_items:
                flat = self.field_service.flatten_data_item_v2(item, fields)
                # 剔除 flatten_data_item_v2 附加的行标识，避免回灌时被当成新字段
                flat.pop("id", None)
                flat.pop("index", None)

                undeclared_seen.update(k for k in flat if k not in declared)

                for key in list(flat.keys()):
                    if key.endswith(IS_MEDIA_COLUMN_SUFFIX):
                        flat[key] = is_media_text(flat[key])
                    else:
                        flat[key] = _stringify(flat[key])
                records.append(flat)

            # 逐行打会把日志刷爆（226 行就是 226 条），这里按数据集汇总一次。
            # 常见的是导入流程写入的内部记账键（*_files 等），不影响导出结果。
            if undeclared_seen:
                logger.warning(
                    "数据集 %s 有未在字段元数据中声明的字段 %s，导出时已忽略",
                    dataset_id, sorted(undeclared_seen),
                )

            return fields, records

        # V1 老数据集 / 空数据集：降级为四列模板
        v1_items = self.data_service.get_all_active_v1(dataset_id)
        if len(v1_items) > MAX_EXPORT_ROWS:
            raise DatasetExportTooLargeError(
                f"数据集共 {len(v1_items)} 条数据，超过单次导出上限 {MAX_EXPORT_ROWS} 条，"
                f"请拆分数据集后分批导出"
            )

        records = []
        for item in v1_items:
            meta = item.meta_data or {}
            records.append({
                "user_input": item.question or "",
                "user_input_is_media": is_media_text(meta.get("user_input_is_media")),
                "answer": item.ref_answer or "",
                "answer_is_media": is_media_text(meta.get("answer_is_media")),
            })

        return self._version_1_fields(), records

    def count_exportable_rows(self, dataset_id: str) -> int:
        """可导出的数据行数（用于接口提前给出明确的超限提示）"""
        count = self.data_service.count_active_v2(dataset_id)
        if count:
            return count
        if self.field_service.get_field_metadata(dataset_id):
            # 有字段结构但一条数据都没有，属于空数据集
            return 0
        return len(self.data_service.get_all_active_v1(dataset_id))

    # ------------------------------------------------------------------
    # Excel 组装
    # ------------------------------------------------------------------
    def export_to_excel(self, dataset_id: str) -> bytes:
        """生成 Excel 文件内容（主 sheet 数据 + 第二个 sheet 字段说明）"""
        fields, records = self._collect(dataset_id)
        columns = self._resolve_column_names(fields)

        frame = pd.DataFrame(records, columns=columns)
        description = self._build_field_description(fields)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name=SHEET_DATA)
            description.to_excel(writer, index=False, sheet_name=SHEET_FIELDS)
        output.seek(0)
        return output.getvalue()

    @staticmethod
    def _build_field_description(fields: List[Dict[str, Any]]) -> pd.DataFrame:
        """字段说明 sheet：给用户看的 field_name ↔ 显示名 对照，不参与导入"""
        rows = []
        for field in fields:
            name = field.get("name")
            if not name or name.endswith(IS_MEDIA_COLUMN_SUFFIX):
                continue
            rows.append({
                "字段名": name,
                "显示名": field.get("label") or name,
                "类型": field.get("type") or "string",
                "是否媒体": "是" if to_bool_media(field.get("is_media")) else "否",
                "是否必填": "是" if field.get("is_required") else "否",
            })
        return pd.DataFrame(
            rows, columns=["字段名", "显示名", "类型", "是否媒体", "是否必填"]
        )
