"""
会话文件数据集导入服务

将 Claude Code jsonl / Langfuse trace 文件解析后导入数据集体系：
一个会话文件（或文件内的一条 trace）= 一条 EvaluationDataV2 数据。
"""
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 会话数据集的标准字段元数据（写入 DatasetFieldMetadata）
# 注意 label 必须与字段语义一致：user_input 是评测输入（首条用户消息摘要），
# session_summary 是展示用的会话摘要——前端表格列头与编辑表单 label 均来自这里
SESSION_FIELD_METADATA = [
    {"name": "user_input", "label": "用户输入（评测输入）", "type": "string", "is_media": False, "is_required": False, "position": 0},
    {"name": "source", "label": "会话来源", "type": "string", "is_media": False, "is_required": False, "position": 1},
    {"name": "session_summary", "label": "会话摘要", "type": "string", "is_media": False, "is_required": False, "position": 2},
    {"name": "session_stats", "label": "会话统计（JSON）", "type": "json", "is_media": False, "is_required": False, "position": 3},
    {"name": "session_messages", "label": "会话内容（JSON，勿手改）", "type": "json", "is_media": False, "is_required": False, "position": 4},
]

# anc-hub 导入页签（from-hub）附加的字段元数据：每条会话数据自动回填原始 jsonl 的
# file_id，且 session_file_id 字段类型标记为 hub_file——评测执行时
# resolve_hub_file_fields 复用持久解包区镜像到任务目录并替换字段值
# （阶段B：缺持久区时该数据项标记失败；设计文档五/决策 #1）
SESSION_HUB_FILE_METADATA = [
    {"name": "session_file_id", "label": "会话原始文件 ID（anc-hub）", "type": "hub_file", "is_media": False, "is_required": False, "position": 5},
    {"name": "session_file_name", "label": "会话原始文件名", "type": "string", "is_media": False, "is_required": False, "position": 6},
]

# hub 通道字段名（type=hub_file）：from-hub 导入时每条会话数据行对其持一次
# 引用（file_id 记在 data 里），import_sessions 据此返回 hub_file_refs——
# 与释放端 release_dataset_hub_refs 的逐行多重释放严格成对
SESSION_HUB_REF_FIELDS = [m["name"] for m in SESSION_HUB_FILE_METADATA if m.get("type") == "hub_file"]

ALLOWED_SESSION_EXTENSIONS = (".jsonl", ".json")


def validate_session_filename(filename: str) -> bool:
    """会话文件扩展名校验"""
    _, ext = os.path.splitext(filename or "")
    return ext.lower() in ALLOWED_SESSION_EXTENSIONS


def save_upload_to_temp(content: bytes, filename: str) -> str:
    """上传内容落盘临时文件（调用方负责删除）"""
    _, ext = os.path.splitext(filename or "")
    fd, tmp_path = tempfile.mkstemp(suffix=ext or ".jsonl")
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return tmp_path


def build_session_data(record: Dict[str, Any]) -> Dict[str, Any]:
    """SessionRecord → EvaluationDataV2.data 扁平结构"""
    from app.services.session_file_parser import summarize_first_user_message

    summary = summarize_first_user_message(record, limit=120)
    return {
        "session_id": record.get("session_id", ""),
        "source": record.get("source", ""),
        "session_summary": summary,
        "session_stats": record.get("stats", {}),
        "session_metadata": record.get("metadata", {}),
        # 完整 SessionRecord（含 messages），评测时由 render_session_markdown 渲染
        "session_messages": record,
        "user_input": summarize_first_user_message(record, limit=80),
    }


def preview_session_files(entries: List[Tuple[str, str]]) -> Dict[str, Any]:
    """解析会话文件生成预览（不落库）

    Args:
        entries: [(file_name, tmp_path), ...]；也兼容三元组
                 [(file_name, tmp_path, entry_key), ...]（第三项仅供
                 import_sessions 做条目唯一键，预览忽略）
    """
    from app.services.session_file_parser import detect_and_parse, summarize_first_user_message

    items: List[Dict[str, Any]] = []
    for entry in entries:
        file_name, tmp_path = entry[0], entry[1]
        if not validate_session_filename(file_name):
            items.append({
                "file_name": file_name, "source": "", "session_id": "",
                "summary": "", "turns": 0, "message_count": 0, "tool_call_count": 0,
                "started_at": None, "ended_at": None,
                "valid": False, "error": "不支持的文件类型，仅支持 .jsonl / .json",
            })
            continue
        try:
            records = detect_and_parse(tmp_path)
        except Exception as e:
            items.append({
                "file_name": file_name, "source": "", "session_id": "",
                "summary": "", "turns": 0, "message_count": 0, "tool_call_count": 0,
                "started_at": None, "ended_at": None,
                "valid": False, "error": f"解析失败: {e}",
            })
            continue
        # 一个文件可能包含多条会话（langfuse 多 trace），逐条展开预览
        for idx, record in enumerate(records):
            stats = record.get("stats", {})
            items.append({
                "file_name": file_name if len(records) == 1 else f"{file_name}#{idx + 1}",
                "source": record.get("source", ""),
                "session_id": record.get("session_id", ""),
                "summary": summarize_first_user_message(record, limit=80),
                "turns": stats.get("turns", 0),
                "message_count": stats.get("message_count", 0),
                "tool_call_count": stats.get("tool_call_count", 0),
                "started_at": record.get("started_at"),
                "ended_at": record.get("ended_at"),
                "valid": True, "error": "",
            })

    valid_count = sum(1 for i in items if i["valid"])
    return {
        "is_valid": valid_count > 0 and valid_count == len(items),
        "total_sessions": len(items),
        "valid_sessions": valid_count,
        "invalid_sessions": len(items) - valid_count,
        "items": items,
    }


def _merge_field_metadata(existing: List[Dict[str, Any]], base: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """以 base 为准合并已有字段元数据，保留 existing 中 base 未覆盖的字段

    追加导入时 save_field_metadata 是全删重建——若不合并，向已有数据集追加普通
    会话会丢掉此前 hub 导入回填的 session_file_id(hub_file) 元数据，导致已导入
    数据的 hub_file 通道失效。
    """
    known = {m.get("name") for m in base}
    merged = list(base)
    for f in existing or []:
        name = f.get("name")
        if name and name not in known:
            merged.append(f)
            known.add(name)
    return merged


def import_sessions(
    db,
    entries: List[Tuple[str, str]],
    name: str = "",
    description: str = "",
    real_dataset_id: Optional[str] = None,
    created_by: Optional[str] = None,
    file_extra: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """会话文件导入数据集（新建或追加）

    Args:
        db: 数据库会话
        entries: [(file_name, tmp_path), ...]；也兼容三元组
                [(file_name, tmp_path, entry_key), ...]，entry_key 为条目唯一键
                （如 anc-hub file_id），file_extra 按该键匹配——同一批导入中出现
                同名文件时（不同 file_id），extra 不会按 file_name 互相覆盖
        name: 新建数据集名称（real_dataset_id 为空时必填）
        description: 数据集描述
        real_dataset_id: 追加模式下的真实数据集 ID
        created_by: 创建人
        file_extra: 可选，按条目唯一键（二元组时即 file_name）附加到每条数据 data
                    的额外字段（如 anc-hub 导入时 {"session_file_id": file_id}）

    Returns:
        {"dataset_id": ..., "count": 导入会话数,
         "hub_file_refs": 本次创建数据行持有的执行包 file_id 列表
         （逐行多重、行内去重，供导入方 acquire_ref 与释放端粒度对齐）}
    """
    from app.services.session_file_parser import detect_and_parse
    from app.services.dataset_service import DatasetService, DatasetDataService
    from app.services.dataset_field_service import DatasetFieldService
    from app.models.dataset_models import EvaluationDataV2, EvaluationDataset
    from sqlalchemy import func

    # 先解析全部会话（任一文件解析失败即中止导入，避免部分成功）
    # 记录 (entry_key, file_name, record) 关联，供 file_extra 按条目唯一键附加
    all_records: List[Tuple[str, str, Dict[str, Any]]] = []
    for entry in entries:
        file_name, tmp_path = entry[0], entry[1]
        entry_key = entry[2] if len(entry) > 2 else file_name
        if not validate_session_filename(file_name):
            raise ValueError(f"文件 {file_name} 类型不支持，仅支持 .jsonl / .json")
        records = detect_and_parse(tmp_path)
        if not records:
            raise ValueError(f"文件 {file_name} 未解析出有效会话")
        for record in records:
            all_records.append((entry_key, file_name, record))

    dataset_service = DatasetService(db)
    is_new_dataset = not real_dataset_id
    if real_dataset_id:
        dataset = dataset_service.get_dataset(real_dataset_id)
        if not dataset:
            raise ValueError(f"目标数据集不存在: {real_dataset_id}")
    else:
        if not name or not name.strip():
            raise ValueError("新建数据集时名称不能为空")
        dataset = dataset_service.create_dataset(
            name=name.strip(),
            description=description or "由 Agent 会话文件导入",
            created_by=created_by,
        )

    data_service = DatasetDataService(db)
    count = 0
    hub_file_refs: List[str] = []
    try:
        for entry_key, file_name, record in all_records:
            data = build_session_data(record)
            if file_extra and entry_key in file_extra:
                extra = file_extra[entry_key]
                data.update(extra)
                # 本次创建的每一行对 hub 通道文件持一次引用（行内去重）：
                # 释放端按数据行逐行释放，acquire 粒度必须与之成对
                row_refs = [
                    extra[k].strip() for k in SESSION_HUB_REF_FIELDS
                    if isinstance(extra.get(k), str) and extra.get(k).strip()
                ]
                hub_file_refs.extend(dict.fromkeys(row_refs))
            data_service.add_data_v2(dataset.id, data)
            count += 1
    except Exception:
        # 新建模式下补偿清理半成品数据集，避免残留空壳
        if is_new_dataset:
            try:
                db.query(EvaluationDataV2).filter(
                    EvaluationDataV2.dataset_id == dataset.id
                ).delete(synchronize_session=False)
                db.query(EvaluationDataset).filter(
                    EvaluationDataset.id == dataset.id
                ).delete(synchronize_session=False)
                db.commit()
                logger.warning(f"会话导入失败，已清理半成品数据集: {dataset.id}")
            except Exception as cleanup_err:
                logger.error(f"会话导入失败且补偿清理失败: {cleanup_err}")
                db.rollback()
        raise

    # 字段元数据（新建/追加均幂等保存）：SESSION 标准字段 + hub 导入回填字段
    # （file_extra 非空即 anc-hub 导入，session_file_id 标记为 hub_file 通道），
    # 并保留数据集已有、本次未覆盖的字段元数据（追加模式防覆盖退化）
    try:
        base_meta = list(SESSION_FIELD_METADATA)
        if file_extra:
            base_meta.extend(SESSION_HUB_FILE_METADATA)
        field_service = DatasetFieldService(db)
        merged_meta = _merge_field_metadata(
            field_service.get_field_metadata(dataset.id), base_meta
        )
        field_service.save_field_metadata(dataset.id, merged_meta)
    except Exception as e:
        logger.warning(f"保存会话数据集字段元数据失败: {e}")

    # 更新条目数（与 import_from_excel 一致的统计口径）
    dataset.question_count = db.query(func.count(EvaluationDataV2.id)).filter(
        EvaluationDataV2.dataset_id == dataset.id,
        EvaluationDataV2.is_active == True,  # noqa: E712
    ).scalar() or 0
    db.commit()

    return {"dataset_id": dataset.id, "count": count, "hub_file_refs": hub_file_refs}
