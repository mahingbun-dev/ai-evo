"""
anc-hub 执行包批量导入任务服务（数据集迭代阶段C）

一次「从 anc-hub 导入执行包」勾选多个 file_id = 一条 HubImportJob：
- 创建（API 侧）：解析目标数据集（新建或追加 real_dataset_id），落 pending 状态并投递 Celery
- 逐包处理（worker 侧）：ensure_unpacked 幂等物化 → 写 EvaluationDataV2 数据行
  （data 含 hub_file 字段值=file_id 与顶层 hub_summary）→ 注册 hub_file 字段元数据
  → acquire_ref 持有引用；每包完成后向 Redis task_status:{job_id} 发布进度
- 失败包记入 fail_detail（{file_id: 中文错误}），retry 只重跑失败项且幂等
  （succeeded_file_ids 为幂等键，已成功的不重复导入）

进度发布复用评测任务的连接方式（evaluation_task_service._publish_status_update）：
redis.from_url(settings.REDIS_URL) → publish task_status:{id}，best-effort 不阻断主流程。
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now():
    """获取当前北京时间"""
    return datetime.now(BEIJING_TZ)


# 数据行 data 中承载 file_id 的字段名（字段元数据类型为 hub_file，复用 HUB_FIELD_TYPE 常量语义）
HUB_FILE_ID_FIELD = "hub_file_id"

# 执行包导入数据集的字段元数据（写入 DatasetFieldMetadata，复用 dataset_field_service）
# - hub_file_id：值为 anc-hub file_id，评测执行时 resolve_hub_file_fields 镜像到任务目录
# - hub_summary：导入时提取的 attempt 执行包摘要（无 manifest 为空 dict）
HUB_IMPORT_FIELD_METADATA = [
    {"name": HUB_FILE_ID_FIELD, "label": "执行包 file_id（anc-hub）", "type": "hub_file",
     "is_media": False, "is_media_field": None, "is_required": False, "position": 0},
    {"name": "hub_summary", "label": "执行包摘要（JSON）", "type": "json",
     "is_media": False, "is_media_field": None, "is_required": False, "position": 1},
]

# 允许重试的状态（failed 才有失败项可重跑）
RETRYABLE_STATUS = "failed"


def _publish_job_progress(job) -> None:
    """发布导入进度到 Redis Pub/Sub（频道与评测进度同前缀 task_status:{id}，best-effort）"""
    try:
        import json

        import redis

        from app.core.config import get_settings

        total = int(job.total_count or 0)
        done = int(job.success_count or 0) + int(job.fail_count or 0)
        progress = int(done * 100 / total) if total > 0 else 100
        message = {
            "type": "hub_import_progress",
            "job_id": job.id,
            "status": job.status,
            "progress": progress,
            "success_count": int(job.success_count or 0),
            "fail_count": int(job.fail_count or 0),
            "total": total,
            "timestamp": beijing_now().isoformat(),
        }
        r = redis.from_url(get_settings().REDIS_URL)
        r.publish(f"task_status:{job.id}", json.dumps(message))
    except Exception as e:  # noqa: BLE001 - 进度发布失败只记日志，不阻断导入
        logger.warning(f"[hub_import] 发布导入进度失败: {e}")


def _normalize_file_ids(file_ids: Optional[List[str]]) -> List[str]:
    """去重保序清理：strip、去空、同 file_id 只保留一个"""
    seen: Dict[str, None] = {}
    for fid in file_ids or []:
        if isinstance(fid, str) and fid.strip():
            seen.setdefault(fid.strip(), None)
    return list(seen.keys())


def _new_job_id() -> str:
    return f"hubjob_{uuid.uuid4().hex[:12]}"


def _merge_field_metadata(existing: List[Dict[str, Any]], base: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """以 base 为准合并已有字段元数据，保留 existing 中 base 未覆盖的字段

    与 session_dataset_service._merge_field_metadata 同语义：save_field_metadata
    是全删重建——若不合并，向已有数据集追加导入会丢掉此前注册的其它字段元数据。
    """
    known = {m.get("name") for m in base}
    merged = list(base)
    for f in existing or []:
        name = f.get("name")
        if name and name not in known:
            merged.append(f)
            known.add(name)
    return merged


def _register_field_metadata(db, dataset_id: str) -> None:
    """注册执行包导入的字段元数据（幂等，保留数据集已有字段；失败不阻断导入）"""
    from app.services.dataset_field_service import DatasetFieldService

    try:
        field_service = DatasetFieldService(db)
        merged = _merge_field_metadata(
            field_service.get_field_metadata(dataset_id), HUB_IMPORT_FIELD_METADATA
        )
        field_service.save_field_metadata(dataset_id, merged)
    except Exception as e:  # noqa: BLE001 - 元数据注册失败只记日志，不阻断数据行导入
        logger.warning(f"[hub_import] 保存执行包字段元数据失败: dataset_id={dataset_id} 错误: {e}")


def create_import_job(
    db,
    file_ids: List[str],
    name: str = "",
    description: str = "",
    real_dataset_id: str = "",
    created_by: Optional[str] = None,
):
    """创建批量导入任务：解析目标数据集并落 pending 状态的 job 行

    数据集解析与 importSessionsFromHub 对齐：real_dataset_id 非空为追加模式
    （校验存在），否则按 name 新建。新建在 API 侧同步完成，worker 只追加数据行，
    保证 job.dataset_id 恒有值（retry 幂等的落点）。

    Raises:
        ValueError: file_ids 为空 / 追加目标不存在 / 新建缺名称（中文消息）
    """
    from app.models.hub_file_models import HubImportJob
    from app.services.dataset_service import DatasetService

    ids = _normalize_file_ids(file_ids)
    if not ids:
        raise ValueError("请至少提供一个 anc-hub file_id")

    dataset_service = DatasetService(db)
    if real_dataset_id:
        dataset = dataset_service.get_dataset(real_dataset_id)
        if not dataset:
            raise ValueError(f"目标数据集不存在: {real_dataset_id}")
    else:
        if not name or not name.strip():
            raise ValueError("新建数据集时名称不能为空")
        dataset = dataset_service.create_dataset(
            name=name.strip(),
            description=description or "由 anc-hub 执行包导入",
            created_by=created_by,
        )

    job = HubImportJob(
        id=_new_job_id(),
        dataset_id=dataset.id,
        file_ids=ids,
        status="pending",
        total_count=len(ids),
        success_count=0,
        fail_count=0,
        fail_detail={},
        succeeded_file_ids=[],
        created_by=created_by,
    )
    db.add(job)
    db.commit()
    logger.info(f"[hub_import] 导入任务已创建: job_id={job.id} dataset_id={job.dataset_id} 包数={len(ids)}")
    return job


def get_job(db, job_id: str):
    """按 ID 查导入任务（不存在返回 None，由 API 层映射 404）"""
    from app.models.hub_file_models import HubImportJob

    return db.query(HubImportJob).filter(HubImportJob.id == job_id).first()


def retry_job(db, job_id: str):
    """重置失败任务为 pending（只重跑失败项由 process 的 succeeded_file_ids 幂等保证）

    Raises:
        ValueError: 任务仍在进行中 / 已全部完成 / 没有可重试的失败项（中文消息）
    """
    job = get_job(db, job_id)
    if job is None:
        return None

    if job.status == "running":
        raise ValueError("导入任务仍在进行中，无法重试")
    if job.status == "pending":
        raise ValueError("导入任务排队中，请等待执行完成后再重试")
    if job.status == "completed":
        raise ValueError("导入任务已全部完成，无需重试")
    if not (job.fail_detail or {}):
        raise ValueError("没有可重试的失败项")

    job.status = "pending"
    db.commit()
    logger.info(f"[hub_import] 导入任务已重置待重试: job_id={job.id} 失败项={list((job.fail_detail or {}).keys())}")
    return job


def process_hub_import_job(
    db,
    job_id: str,
    *,
    datasets_root: Optional[str] = None,
    hub_service: Optional[Any] = None,
) -> Dict[str, Any]:
    """逐包处理导入任务（Celery worker 入口的核心逻辑）

    每包流程（设计文档阶段C）：
        ensure_unpacked（幂等，登记表命中零下载）→ acquire_ref →
        写 EvaluationDataV2 数据行（data = {hub_file_id: file_id, hub_summary: {...}}）
    每包完成后更新计数并 publish 进度；单包失败记入 fail_detail 继续处理其余包，
    全部处理完按 fail_detail 是否为空落 completed / failed。

    已成功的包记录在 succeeded_file_ids（幂等键）：retry / 任务重投时不重复导入。

    Returns:
        {job_id, dataset_id, status, total, success_count, fail_count, fail_detail}
    """
    from app.services.dataset_service import DatasetDataService
    from app.services.hub_file_service import acquire_ref, ensure_unpacked, release_ref

    job = get_job(db, job_id)
    if job is None:
        raise ValueError(f"导入任务不存在: {job_id}")

    job.status = "running"
    db.commit()
    _publish_job_progress(job)

    # 注册字段元数据（幂等；数据集已有字段不会被覆盖删除）
    _register_field_metadata(db, job.dataset_id)

    succeeded = list(job.succeeded_file_ids or [])
    fail_detail = dict(job.fail_detail or {})
    to_run = [fid for fid in (job.file_ids or []) if fid not in succeeded]
    data_service = DatasetDataService(db)

    for fid in to_run:
        acquired = False
        try:
            info = ensure_unpacked(db, fid, datasets_root=datasets_root, service=hub_service)
            # 先持引用再写数据行：数据行写失败时在 except 分支补偿释放
            acquire_ref(db, fid)
            acquired = True
            data = {
                HUB_FILE_ID_FIELD: fid,
                "hub_summary": info.get("summary") or {},
            }
            data_service.add_data_v2(job.dataset_id, data)
            succeeded.append(fid)
            fail_detail.pop(fid, None)
        except Exception as e:  # noqa: BLE001 - 单包失败记明细，继续处理其余包
            db.rollback()
            if acquired:
                # 数据行未落库成功：补偿释放本次持有的引用，避免引用泄漏
                try:
                    release_ref(db, fid)
                except Exception as re:  # noqa: BLE001
                    logger.warning(f"[hub_import] 补偿释放引用失败: file_id={fid} 错误: {re}")
            fail_detail[fid] = str(e)[:2000]
            logger.warning(f"[hub_import] 执行包导入失败: job_id={job_id} file_id={fid} 错误: {e}")

        # 每次挂载新对象（复制），保证 SQLAlchemy JSON 列的变更被追踪
        job.succeeded_file_ids = list(succeeded)
        job.fail_detail = dict(fail_detail)
        job.success_count = len(succeeded)
        job.fail_count = len(fail_detail)
        db.commit()
        _publish_job_progress(job)

    job.status = "completed" if not fail_detail else "failed"
    db.commit()
    _publish_job_progress(job)
    logger.info(
        f"[hub_import] 导入任务结束: job_id={job_id} status={job.status} "
        f"成功={job.success_count} 失败={job.fail_count}"
    )
    return {
        "job_id": job.id,
        "dataset_id": job.dataset_id,
        "status": job.status,
        "total": int(job.total_count or 0),
        "success_count": int(job.success_count or 0),
        "fail_count": int(job.fail_count or 0),
        "fail_detail": dict(fail_detail),
    }
