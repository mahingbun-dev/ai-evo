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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.dataset_field_service import HUB_FIELD_TYPE

logger = logging.getLogger(__name__)

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now():
    """获取当前北京时间"""
    return datetime.now(BEIJING_TZ)


# 数据行 data 中承载 file_id 的字段名（字段元数据类型为 hub_file，复用 HUB_FIELD_TYPE 常量语义）
HUB_FILE_ID_FIELD = "hub_file_id"

# 平台注入字段（hub_file 通道）：由导入链路自身写入数据行，与产出方无关。
# type=hub_file 是平台语义——评测执行时 resolve_hub_file_fields 据此把字段值
# （anc-hub file_id）解析为任务目录内的实体文件；这种语义无法从行内值推导，
# 故只能由平台显式声明。
_HUB_FILE_ID_METADATA = {
    "name": HUB_FILE_ID_FIELD, "label": "执行包 file_id（anc-hub）", "type": HUB_FIELD_TYPE,
    "is_media": False, "is_media_field": None, "is_required": False, "position": 0,
}


def _platform_injected_fields() -> List[Dict[str, Any]]:
    """平台注入字段的副本（新 dict，避免调用方就地修改污染模块常量）"""
    return [dict(_HUB_FILE_ID_METADATA)]


# 原样导入（不挂清洗脚本）的字段元数据：该路径的数据行完全由平台组装
# （hub_file_id + hub_summary），两个字段都是平台自产，故由平台声明。
HUB_IMPORT_FIELD_METADATA = [
    dict(_HUB_FILE_ID_METADATA),
    {"name": "hub_summary", "label": "执行包摘要（JSON）", "type": "json",
     "is_media": False, "is_media_field": None, "is_required": False, "position": 1},
]

# 允许重试的状态（failed 才有失败项可重跑）
RETRYABLE_STATUS = "failed"

# running 状态卡死判定阈值（W11）：updated_at 超过该时长未推进即认为 worker 已死。
# process_hub_import_job 逐包 commit（status/计数/幂等键）会持续刷新 updated_at，
# 正常执行中不会超过该阈值；worker 崩溃（OOM/SIGKILL）后 updated_at 停止推进。
HUB_JOB_STUCK_AFTER_SECONDS = 30 * 60


def _job_stale_seconds(job) -> Optional[float]:
    """running job 的 updated_at 距今秒数；缺失/非时间值返回 None（无法判定）。

    时区兼容：Postgres timestamptz 返回带时区值；SQLite CURRENT_TIMESTAMP 等
    返回 naive 值按 UTC 解释（与 server_default func.now() 的 UTC 语义一致）。
    """
    updated_at = getattr(job, "updated_at", None)
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (beijing_now() - updated_at).total_seconds()


def release_job_lock(job_id: str) -> bool:
    """清除 hub 导入 job 互斥锁（retry 接管卡死任务时调用，best-effort）

    卡死任务的 worker 已判定死亡，但其残留锁（hubjob:{job_id}）在 TTL（3h）内
    仍在——不清除的话，重试新投递的 worker 会被 SETNX 拒绝，任务永远停在
    pending。清除失败（redis 不可用等）只记日志，由 TTL 过期兜底。
    """
    try:
        from app.services.redis_stop_signal import HUB_JOB_LOCK_PREFIX, HUB_JOB_LOCK_TTL, EvalTaskLock

        EvalTaskLock(job_id, ttl=HUB_JOB_LOCK_TTL, key_prefix=HUB_JOB_LOCK_PREFIX).release()
        logger.info(f"[hub_import] 已清除 job 互斥锁（卡死接管）: job_id={job_id}")
        return True
    except Exception as e:  # noqa: BLE001 - 锁清除失败不阻断重试主流程
        logger.warning(f"[hub_import] 清除 job 互斥锁失败（将由 TTL 过期兜底）: job_id={job_id} 错误: {e}")
        return False


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
    """以 base 为准合并已有字段元数据（委托 dataset_field_service 的统一实现）

    保留本模块函数名以兼容既有调用点与测试。
    """
    from app.services.dataset_field_service import merge_field_metadata

    return merge_field_metadata(existing, base)


def _field_signature(fields: List[Dict[str, Any]]) -> List[tuple]:
    """字段元数据的可比签名（顺序敏感）：用于判断字段集是否发生变化"""
    return [
        (
            str(f.get("name")),
            str(f.get("label") or f.get("name")),
            str(f.get("type") or "string"),
            bool(f.get("is_media")),
            f.get("is_media_field"),
        )
        for f in fields or []
    ]


def _register_field_metadata(
    db,
    dataset_id: str,
    row: Optional[Dict[str, Any]] = None,
    row_fields: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """注册导入字段元数据（幂等，字段集未变化时跳过写库；失败不阻断导入）

    字段来源分两类：
    - 平台注入字段（hub_file_id，type=hub_file）——由导入链路自身写入数据行，
      语义无法从行内值推导，固定前置；
    - 产出方字段——脚本声明（row_fields，见 pre_judge_runner 的 stdout 契约）
      优先，数据行内容（row）兜底推导，label 取行内 field_descriptions 对照表。
      平台不持有任何行 schema 清单：清洗脚本改了行结构，字段元数据自动跟随。

    row 与 row_fields 均为空 = 原样导入（无清洗脚本），该路径数据行由平台自己
    组装（hub_file_id + hub_summary），回落 HUB_IMPORT_FIELD_METADATA。

    多包导入时逐包调用即为累积注册：merge_field_metadata 保留已有字段，
    故 v1/v2 混合批次的字段并集会被完整登记。
    """
    from app.services.dataset_field_service import (
        DatasetFieldService,
        derive_fields_from_row,
        reindex_field_positions,
    )

    try:
        injected = _platform_injected_fields()
        if row is None and not row_fields:
            base_meta = [dict(m) for m in HUB_IMPORT_FIELD_METADATA]
        else:
            base_meta = injected + derive_fields_from_row(
                row or {},
                declared=row_fields,
                exclude=(HUB_FILE_ID_FIELD,),
                start_position=len(injected),
            )
        field_service = DatasetFieldService(db)
        existing = field_service.get_field_metadata(dataset_id)
        merged = reindex_field_positions(_merge_field_metadata(existing, base_meta))
        # save_field_metadata 是全删重建：字段集没变就不必重写（逐包调用时多数
        # 包命中此分支），避免无谓的 id 抖动与写放大
        if _field_signature(existing) == _field_signature(merged):
            return
        field_service.save_field_metadata(dataset_id, merged)
    except Exception as e:  # noqa: BLE001 - 元数据注册失败只记日志，不阻断数据行导入
        logger.warning(f"[hub_import] 保存执行包字段元数据失败: dataset_id={dataset_id} 错误: {e}")


def validate_clean_script(db, script_id: str) -> str:
    """校验清洗脚本可用并返回规范化的 script_id

    要求：脚本库中存在、启用、exec_type=script（claude_agent/deepseek_agent
    类型由评测 Harness 消费，导入清洗需要确定性的子进程脚本）。

    Raises:
        ValueError: 脚本不存在 / 未启用 / 类型不支持（中文消息）
    """
    from app.models.pre_judge_models import PreJudgeScript

    sid = str(script_id or "").strip()
    if not sid:
        return ""
    row = db.query(PreJudgeScript).filter(PreJudgeScript.id == sid).first()
    if row is None:
        # 与评测编排一致：允许按名称引用（_load_script 的兼容语义）
        row = db.query(PreJudgeScript).filter(PreJudgeScript.name == sid).first()
    if row is None:
        raise ValueError(f"清洗脚本不存在: {script_id}")
    if not row.is_active:
        raise ValueError(f"清洗脚本已停用: {row.name}")
    if (row.exec_type or "script") != "script":
        raise ValueError(f"清洗脚本类型不支持: {row.name}（exec_type={row.exec_type}，仅支持 script）")
    return row.id


def _run_clean_script(
    unpack_dir: str, file_index: list, file_id: str, script_def: dict
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """对单个执行包运行清洗脚本，返回 (数据集行内容, 脚本声明的行字段契约)

    工作区隔离复用评测脚本基建：build_run_dir("hub_import", file_id) 建工作区，
    copy_into_workspace 拷包副本（严格拷贝语义，不触碰持久解包区），stdin.inputs
    用 files 模式（文件对象数组由 file_index 还原；索引缺失时脚本会扫根兜底）。
    脚本产出的行内容从 stdout.files 登记的产物文件读取（避免超过 stdout 上限），
    兜底取 output。失败抛异常，由调用方记入 fail_detail。

    第二项是脚本自描述的行字段契约（stdout.row_fields，已由
    pre_judge_runner._normalize_row_fields 归一化）：脚本不声明时为空列表，
    导入侧改从行内容推导字段元数据，平台不持有行 schema 清单。

    Raises:
        RuntimeError: 脚本执行失败 / 产物不可读（中文消息）
    """
    import json as _json

    from app.services import pre_judge_runner

    run_dir = pre_judge_runner.build_run_dir("hub_import", file_id)
    pkg_copy = pre_judge_runner.copy_into_workspace(unpack_dir, run_dir, "hub_pkg")

    files_array = []
    for entry in file_index or []:
        rel = str(entry.get("path") or "")
        if not rel:
            continue
        files_array.append({
            "path": str(Path(pkg_copy) / rel),
            "rel_path": rel,
            "size": entry.get("size", 0),
            "type": entry.get("type", ""),
        })
    inputs = {"hub_pkg": {"root": pkg_copy}}
    if files_array:
        inputs["hub_pkg"]["files"] = files_array

    stdin = {
        "task_id": "hub_import",
        "data_id": file_id,
        "fields": {},
        "inputs": inputs,
        "model_output": None,
        "session": None,
        "params": {},
        "upstream": {},
        "_run_dir": str(run_dir),
    }
    result = pre_judge_runner.execute_script(script_def, stdin,
                                             timeout_seconds=script_def.get("timeout_seconds"))
    if result.get("status") != "ok":
        error = result.get("error") or result.get("summary") or "未知错误"
        raise RuntimeError(f"清洗脚本执行失败: {error}")

    # 行内容从产物文件读取（stdout 只携带摘要）；兜底 output
    row_fields = result.get("row_fields") or []
    for name, path in (result.get("files") or {}).items():
        if name.endswith(".json") and Path(path).is_file():
            try:
                row = _json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError) as e:
                raise RuntimeError(f"清洗产物不可读: {name}（{e}）")
            if isinstance(row, dict):
                return row, row_fields
    output = result.get("output")
    if isinstance(output, dict) and output:
        return output, row_fields
    raise RuntimeError("清洗脚本未产出可用的行内容（产物文件与 output 均为空）")


def create_import_job(
    db,
    file_ids: List[str],
    name: str = "",
    description: str = "",
    real_dataset_id: str = "",
    created_by: Optional[str] = None,
    script_id: Optional[str] = None,
    team_code: Optional[str] = None,
):
    """创建批量导入任务：解析目标数据集并落 pending 状态的 job 行

    数据集解析与 importSessionsFromHub 对齐：real_dataset_id 非空为追加模式
    （校验存在），否则按 name 新建。新建在 API 侧同步完成，worker 只追加数据行，
    保证 job.dataset_id 恒有值（retry 幂等的落点）。

    script_id 非空时校验清洗脚本可用（存在/启用/exec_type=script）并把规范化
    后的脚本 ID 落到 job 行——worker 逐包执行清洗脚本，以脚本产出的行内容落库。

    team_code：W9 新建数据集时落用户团队（归属锚点，hub_files.team_code 从
    它继承）；追加模式忽略（沿用目标数据集的团队）。

    Raises:
        ValueError: file_ids 为空 / 追加目标不存在 / 新建缺名称 / 清洗脚本不可用（中文消息）
    """
    from app.models.hub_file_models import HubImportJob
    from app.services.dataset_service import DatasetService

    ids = _normalize_file_ids(file_ids)
    if not ids:
        raise ValueError("请至少提供一个 anc-hub file_id")

    normalized_script_id = ""
    if script_id and str(script_id).strip():
        normalized_script_id = validate_clean_script(db, script_id)

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
            team_code=team_code,
        )

    job = HubImportJob(
        id=_new_job_id(),
        dataset_id=dataset.id,
        file_ids=ids,
        script_id=normalized_script_id or None,
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
    logger.info(f"[hub_import] 导入任务已创建: job_id={job.id} dataset_id={job.dataset_id} "
                f"包数={len(ids)} 清洗脚本={normalized_script_id or '无'}")
    return job


def get_job(db, job_id: str):
    """按 ID 查导入任务（不存在返回 None，由 API 层映射 404）"""
    from app.models.hub_file_models import HubImportJob

    return db.query(HubImportJob).filter(HubImportJob.id == job_id).first()


def retry_job(db, job_id: str):
    """重置失败任务为 pending（只重跑失败项由 process 的 succeeded_file_ids 幂等保证）

    卡死自愈（W11）：running 状态且 updated_at 超过 HUB_JOB_STUCK_AFTER_SECONDS
    未推进的任务视为 worker 已死，允许接管重跑——此时不要求存在失败项（可能
    尚未产生任何失败），重跑的幂等由 succeeded_file_ids 保证；接管同时清除
    残留的 job 互斥锁（否则新投递的 worker 会被 SETNX 拒绝）。

    Raises:
        ValueError: 任务仍在进行中（未卡死）/ 排队中 / 已全部完成 /
                    没有可重试的失败项（中文消息）
    """
    job = get_job(db, job_id)
    if job is None:
        return None

    if job.status == "running":
        stale_seconds = _job_stale_seconds(job)
        if stale_seconds is None or stale_seconds < HUB_JOB_STUCK_AFTER_SECONDS:
            raise ValueError("导入任务仍在进行中，无法重试")
        logger.warning(
            f"[hub_import] 接管卡死的导入任务: job_id={job_id} "
            f"updated_at 距今 {int(stale_seconds)} 秒（> {HUB_JOB_STUCK_AFTER_SECONDS}），重置重跑"
        )
        release_job_lock(job_id)
    elif job.status == "pending":
        raise ValueError("导入任务排队中，请等待执行完成后再重试")
    elif job.status == "completed":
        raise ValueError("导入任务已全部完成，无需重试")
    elif not (job.fail_detail or {}):
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

    每包流程（设计文档阶段C；挂载清洗脚本时插入清洗步骤）：
        ensure_unpacked（幂等，登记表命中零下载）→ acquire_ref →
        [script_id 非空] 运行清洗脚本（工作区隔离拷包），以脚本产出的行内容为
        数据行 data → 否则 data = {hub_file_id: file_id, hub_summary: {...}} →
        写 EvaluationDataV2 数据行
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

    # W9：目标数据集团队（hub_files.team_code 继承写入的来源锚点）
    from app.models.dataset_models import EvaluationDataset

    dataset_row = (
        db.query(EvaluationDataset).filter(EvaluationDataset.id == job.dataset_id).first()
    )
    job_team_code = dataset_row.team_code if dataset_row is not None else None

    # 清洗脚本定义（job.script_id 非空时逐包执行清洗；加载失败按整体失败处理）
    script_def = None
    if job.script_id:
        from app.services.pre_judge_runner import _load_script

        script_def = _load_script(job.script_id)
        if script_def is None:
            raise ValueError(f"清洗脚本不可用（不存在或已删除）: {job.script_id}")

    # 字段元数据注册时机分两种：
    # - 无清洗脚本：数据行由平台组装（hub_file_id + hub_summary）、字段固定，
    #   循环前注册一次即可；
    # - 挂清洗脚本：字段来自脚本声明 + 行内容推导，跑完才知道，改为逐包在数据行
    #   落库后累积注册（merge 保留已有字段，故混合批次能登记出字段并集；
    #   字段集未变化时 _register_field_metadata 内部跳过写库）
    if script_def is None:
        _register_field_metadata(db, job.dataset_id)

    succeeded = list(job.succeeded_file_ids or [])
    fail_detail = dict(job.fail_detail or {})
    to_run = [fid for fid in (job.file_ids or []) if fid not in succeeded]
    data_service = DatasetDataService(db)

    for fid in to_run:
        acquired = False
        try:
            # W9：team_code 传入——新建登记时从目标数据集继承团队归属
            info = ensure_unpacked(
                db, fid, datasets_root=datasets_root, service=hub_service,
                team_code=job_team_code,
            )
            # 先持引用再写数据行：数据行写失败时在 except 分支补偿释放
            acquire_ref(db, fid)
            acquired = True
            row_fields: List[Dict[str, Any]] = []
            if script_def is not None:
                row, row_fields = _run_clean_script(
                    info.get("unpack_dir") or "",
                    info.get("file_index") or [],
                    fid,
                    script_def,
                )
                data = {**row, HUB_FILE_ID_FIELD: fid}
            else:
                data = {
                    HUB_FILE_ID_FIELD: fid,
                    "hub_summary": info.get("summary") or {},
                }
            data_service.add_data_v2(job.dataset_id, data)
            if script_def is not None:
                # 数据行落库后再注册字段：字段契约以真实落库的行为准（含平台注入字段）
                _register_field_metadata(db, job.dataset_id, data, row_fields)
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
