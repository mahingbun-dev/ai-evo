"""
评测任务 - Celery 版本

使用 Celery Worker 执行评测任务。
通过 RedisStopSignalChecker 或 threading.Event 支持任务真正中断。
"""
import asyncio
import logging
import threading
from typing import Dict, Any, List, Optional, Protocol

from celery.exceptions import SoftTimeLimitExceeded

from app.core.config import get_settings
from app.db.session import engine, SessionLocal
from app.services.evaluation_task_service import EvaluationTaskService, TaskExecutionLogService

logger = logging.getLogger(__name__)


class TaskStoppedException(Exception):
    """任务被主动停止时抛出"""
    pass


def _archive_task_workspace(task_id: Optional[str]) -> None:
    """任务级工作区归档（ARCH-2-4，设计 §2.3 D5 / §3 Phase 2 表 2-4 行）

    工作区 uploads/script_runs/{task_id}/ 打包为 uploads/archives/{task_id}.tar.gz
    后清原目录。正常链路工作区已被逐条（评完即清）与 execute_batch 任务级
    兜底清理，此处为 no-op 不产生归档；本归档兜住异常中断等残留（压缩留档
    + 清原目录，磁盘增长曲线平稳），同时作为 Phase 3 工作区整体搬对象存储
    的挂载点。归档失败保留原目录仅告警（走既有兜底清理），不影响任务状态
    ——与 _cleanup_task_hub_cache 同口径。
    """
    if not task_id:
        return
    try:
        from app.services.workspace import archive_task_workspace

        archive_task_workspace(task_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"工作区归档失败（保留原目录待兜底清理）: task_id={task_id} 错误: {e}")


def _cleanup_task_hub_cache(task_id: Optional[str]) -> None:
    """清理任务级 hub 拉取缓存目录 uploads/hub_files/{task_id}/

    任务结束随临时产物清理（设计文档五缓存行与 §七.6「hub 拉取缓存同批清理」）。
    目录不存在时为 no-op；清理失败仅告警，不影响任务状态。
    """
    if not task_id:
        return
    try:
        import os
        from app.services.hub_file_service import cleanup_hub_files, sanitize_hub_name
        cleanup_hub_files(
            os.path.join(os.getcwd(), "uploads", "hub_files", sanitize_hub_name(str(task_id)))
        )
    except Exception as e:
        logger.warning(f"清理 hub 拉取缓存失败: task_id={task_id} 错误: {e}")


class StopEventProtocol(Protocol):
    """停止事件协议 - 兼容 threading.Event 和 RedisStopSignalChecker"""

    def is_set(self) -> bool: ...
    def set(self) -> None: ...
    def cleanup(self) -> Optional[None]: ...


def get_db_session():
    """获取数据库会话 - 直接复用 session.py 中的共享 engine 和 SessionLocal"""
    return SessionLocal()


def run_evaluation_async(task_id: str, stop_event: Optional[StopEventProtocol] = None) -> Dict[str, Any]:
    """
    执行评测任务的主函数 - Celery 版本

    流程：
    1. 读取任务配置
    2. 加载数据集
    3. 处理媒体文件
    4. 生成Payload
    5. 并发执行评测（W3：逐行 write-through 落库；断点续跑跳过已完成行）
    6. 聚合结果（W3：优先从行级表聚合，含续跑前次已完成行）
    7. 存储结果

    Args:
        task_id: 任务ID
        stop_event: 停止事件，支持 threading.Event 或 RedisStopSignalChecker
    """
    if stop_event is None:
        stop_event = threading.Event()  # 兼容直接调用（调试模式）

    db = get_db_session()
    log_service = TaskExecutionLogService(db)
    task_service = EvaluationTaskService(db)

    try:
        # ========== 步骤0: 检查数据库任务状态（防止Celery重启后执行已停止的任务）==========
        # Celery重启时，未ACK的任务会被重新放回队列执行
        # 需要检查数据库状态，避免已停止的任务继续执行
        current_task = task_service.get_task(task_id)
        if current_task and current_task.status == "stopped":
            log_service.add_log(task_id, "info", "任务已停止，跳过执行（Celery重启后恢复）")
            return {"status": "stopped", "task_id": task_id}

        # ========== 步骤1: 读取任务配置 ==========
        # 先获取任务，确认存在后再记录日志（避免外键约束失败）
        task = task_service.get_task(task_id)

        if not task:
            raise ValueError(f"任务 {task_id} 不存在")

        log_service.add_log(task_id, "info", "开始读取任务配置")

        # 注意：start_task 已在 API 层调用，此处不重复调用
        log_service.add_log(task_id, "info", f"任务配置读取完成: {task.name}")

        # W3：本次执行序号（start_task 已原子递增 execution_count 并创建执行记录，
        # 与 stop_task/complete_task 的 execution_count 同口径）
        execution_number = getattr(task, "execution_count", None) or 1

        # ========== 步骤2: 加载数据集 ==========
        log_service.add_log(task_id, "info", "开始加载数据集")
        dataset_items = _load_dataset(task, db)
        log_service.add_log(task_id, "info", f"数据集加载完成，共 {len(dataset_items)} 条数据")

        # W3-3 断点续跑：同 (task_id, execution_number) 已有 completed/judge_failed
        # 行跳过（celery 消息重投/worker 崩溃后重跑不重复计费）。
        # 耗时评测除外：其行级落库键是 repeat_count 展开后的全序序号，
        # 与数据行序号空间不一致，保守不跳过（完整重跑）。
        completed_indexes = set()
        if getattr(task, "evaluation_type", "") != "latency":
            from app.services.evaluation_result_service import get_completed_data_indexes

            completed_indexes = get_completed_data_indexes(db, task_id, execution_number)
            if completed_indexes:
                log_service.add_log(
                    task_id, "info",
                    f"断点续跑：本轮已有 {len(completed_indexes)} 行已完成（含 judge_failed），将跳过",
                )

        # 更新题目总数（进度基数为已跳过的已完成行——续跑时进度为累计值）
        task_service.update_progress(task_id, len(completed_indexes), len(dataset_items))

        # W10-4 执行心跳：任务真正开跑即写入首个心跳（此后每批行完成续跳，
        # 写库按 60s 节流，首跳 force 绕过）。启动收尸 reaper 只认「心跳超时」
        # ——排队中任务（执行记录尚无心跳）与执行中的任务在部署重启时都不会
        # 被误标 failed。
        task_service.touch_heartbeat(task_id, execution_number, force=True)

        # ========== 步骤3: 处理媒体文件 ==========
        log_service.add_log(task_id, "info", "开始处理媒体文件")
        processed_items = _process_media_files(task, dataset_items, db)
        log_service.add_log(task_id, "info", "媒体文件处理完成")

        # ========== 步骤4: 生成Payload ==========
        log_service.add_log(task_id, "info", "开始生成请求Payload")
        payloads = _generate_payloads(task, processed_items, db)
        log_service.add_log(task_id, "info", f"Payload生成完成，共 {len(payloads)} 个")

        # W3 行身份标注：data_index 以数据项在数据集中的原始序号为准
        # （下划线内部键，不进裁判 prompt 变量、不进结果平铺字段），
        # 使行级落库键与 hub 拆分/续跑过滤后的列表序号解耦、多次执行间稳定
        for _orig_pos, _payload in enumerate(payloads):
            _payload["_orig_idx"] = _orig_pos

        # ========== 步骤5: 并发执行评测 ==========
        # 执行前检查是否已被停止（可能在排队期间被停止）
        if stop_event.is_set():
            log_service.add_log(task_id, "info", "任务在开始执行前被停止")
            task_service.stop_task(task_id)
            return {"status": "stopped", "task_id": task_id}

        # hub 执行包不可用的数据项直接产出失败结果，不发起评测（阶段B：
        # 数据项级失败，其余数据继续评测、任务不中断，错误信息可定位 file_id）
        hub_failed_results, runnable_payloads = _split_hub_unresolved_payloads(payloads)

        # W3-3：断点续跑过滤——已完成行不再评测（payloads 与 hub 失败行均按
        # 原始序号过滤；行级 upsert 幂等，重跑失败/缺失行不产生重复数据）
        if completed_indexes:
            runnable_payloads = [
                p for p in runnable_payloads
                if p.get("_orig_idx") not in completed_indexes
            ]
            hub_failed_results = [
                r for r in hub_failed_results
                if r.get("idx") not in completed_indexes
            ]

        if hub_failed_results:
            log_service.add_log(
                task_id, "warning",
                f"{len(hub_failed_results)} 条数据的 hub 执行包不可用，已标记失败（其余数据继续评测）",
            )
            # W3-2：hub 失败行在执行开始前即落库（终态已知），
            # 执行中途被停止/强杀也不丢
            for hub_row in hub_failed_results:
                _persist_row(task_id, execution_number, hub_row["idx"], hub_row)

        log_service.add_log(task_id, "info", "开始执行评测")
        results = _execute_evaluation(task, runnable_payloads, db, task_id, stop_event)
        results.extend(hub_failed_results)

        # 执行完成后检查是否被停止
        if stop_event.is_set():
            log_service.add_log(task_id, "info", "任务已被停止（评测阶段）")
            task_service.stop_task(task_id)
            return {"status": "stopped", "task_id": task_id}

        log_service.add_log(task_id, "info", "评测执行完成")

        # ========== 步骤6: 聚合结果 ==========
        # W3-2：优先从行级表聚合（行级表为本轮结果事实源，含续跑前次已完成行；
        # DB 无行/读取异常回退内存结果——与旧口径一致，零丢失）
        results = _merge_row_table_results(db, task_id, execution_number, results)
        log_service.add_log(task_id, "info", "开始聚合结果")
        aggregated_result = _aggregate_results(results, task)
        # W4（P0-C）：快照使用标记——任务带创建时配置快照为 True（执行用快照
        # 口径），快照为空（存量任务）走实时查询为 False；增量字段，旧消费方不受影响
        from app.services.config_snapshot import snapshot_used

        aggregated_result["config_snapshot"] = snapshot_used(task)
        log_service.add_log(task_id, "info", "结果聚合完成")

        # ========== 步骤7: 存储结果 ==========
        log_service.add_log(task_id, "info", "开始存储结果")
        task_service.complete_task(task_id, aggregated_result)
        log_service.add_log(task_id, "info", "任务完成")

        return {
            "status": "success",
            "task_id": task_id,
            "result": aggregated_result,
        }

    except TaskStoppedException:
        log_service.add_log(task_id, "info", "任务已主动停止")
        task_service.stop_task(task_id)
        return {"status": "stopped", "task_id": task_id}

    except SoftTimeLimitExceeded:
        # W3-4 软超时收尾（P0-E）：软超时不再被行级 except Exception 吞掉，
        # 在硬杀（SIGKILL）前完成收尾——停止后续行、已落库行保留、任务标
        # failed(error="soft time limit")，续跑可从行级表接续
        logger.warning(f"评测任务软超时（soft time limit）: {task_id}")
        try:
            log_service.add_log(
                task_id, "error",
                "任务软超时（soft time limit），已完成行已落库保留，任务标记失败",
            )
        except Exception:  # noqa: BLE001 - 收尾日志失败不阻断状态更新
            pass
        try:
            task_service.fail_task(task_id, "soft time limit")
        except Exception as e:  # noqa: BLE001
            logger.error(f"软超时收尾 fail_task 失败: {e}")
        return {"status": "failed", "task_id": task_id, "error": "soft time limit"}

    except Exception as e:
        logger.exception(f"评测任务执行失败: {task_id}")
        log_service.add_log(task_id, "error", f"执行失败: {str(e)}")
        # 若停止信号已触发，优先按停止处理
        if stop_event is not None and stop_event.is_set():
            task_service.stop_task(task_id)
            return {"status": "stopped", "task_id": task_id}
        # 不要在这里调用 fail_task，而是重新抛出异常
        # 让 Celery 的 on_failure 回调统一处理状态更新
        # 这样可以避免竞态条件（任务还在执行但状态已变成 failed）
        raise

    finally:
        db.close()
        # ARCH-2-4：任务结束工作区归档（tar + 清原目录，失败保留原目录）——
        # 沿用本清理点（成功/失败/停止统一覆盖），见 _archive_task_workspace
        _archive_task_workspace(task_id)
        # hub 拉取缓存随任务结束清理（成功/失败/停止统一覆盖，与 judge_sessions
        # 临时文件同批清理时机一致，设计文档 §七.6）
        _cleanup_task_hub_cache(task_id)
        # 不再主动清理停止信号，让 on_revoke 回调处理
        # 如果是 RedisStopSignalChecker，on_revoke 会调用 stop_task(task_id) 更新状态


def _persist_row(task_id: str, execution_number: Optional[int], data_index: int, result: Dict[str, Any]) -> None:
    """行级 write-through 落库薄封装（W3-2）

    execution_number 为 None（直调/测试链路）时跳过。
    evaluation_result_service.persist_row_result 内部已按进程去重告警且不阻断
    评测主流程；此处仅兜底导入等异常。软超时异常上抛，由收尾逻辑处理。
    """
    if execution_number is None:
        return
    try:
        from app.services.evaluation_result_service import persist_row_result

        persist_row_result(
            task_id=task_id,
            execution_number=execution_number,
            data_index=data_index,
            result=result,
        )
    except SoftTimeLimitExceeded:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[RowPersist] 行级结果落库失败（不影响评测主流程）idx={data_index}: {e}")


def _merge_row_table_results(
    db,
    task_id: str,
    execution_number: int,
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """complete 聚合前的行级表合并（W3-2）：行级表为本轮结果事实源

    - 行级表包含续跑前次已完成行与本次执行全部已落库行——仅靠本次内存
      results（只含本轮新跑的行）聚合会丢前次行，故优先从行级表重建；
    - 同 data_index 冲突时以内存行为准（本轮内容最新）；
    - 行级表无行（迁移未执行/全新链路/落库全失败）→ 原样返回内存结果
      （与旧口径一致，零丢失）；
    - 无行身份的内存行（异常兜底）按原顺序追加在尾部。
    """
    from app.services.evaluation_result_service import load_execution_results

    db_rows = load_execution_results(db, task_id, execution_number)
    if not db_rows:
        return results

    merged: Dict[int, Dict[str, Any]] = {}
    for row in db_rows:
        data_index = row.get("_orig_idx") if isinstance(row, dict) else None
        if isinstance(data_index, int):
            merged[data_index] = row

    unidentified: List[Dict[str, Any]] = []
    for row in results:
        data_index = row.get("_orig_idx") if isinstance(row, dict) else None
        if isinstance(data_index, int):
            merged[data_index] = row
        else:
            unidentified.append(row)

    return [merged[key] for key in sorted(merged)] + unidentified


def _load_dataset(task, db) -> List[Dict[str, Any]]:
    """加载数据集数据（V2 only）"""
    from app.services.dataset_service import DatasetDataService
    from app.services.dataset_snapshot_service import DatasetSnapshotService

    if not task.dataset_id:
        return []

    # 优先从快照加载数据
    if task.dataset_snapshot_id:
        snapshot_service = DatasetSnapshotService(db)
        snapshot = snapshot_service.get_snapshot(task.dataset_snapshot_id)
        if snapshot and snapshot.get("data"):
            snapshot_data = snapshot["data"]
            if isinstance(snapshot_data, dict) and "items" in snapshot_data:
                return snapshot_data["items"]
            return snapshot_data

    # 从 V2 表加载数据
    data_service = DatasetDataService(db)
    items_v2 = data_service.get_data_list_v2(task.dataset_id, skip=0, limit=10000)

    result = []
    for item in items_v2:
        data_json = item.data or {}
        result.append({
            "id": item.id,
            "index": item.index,
            "data": data_json,
        })

    return result


def _process_media_files(task, items: List[Dict], db) -> List[Dict]:
    """处理媒体文件（V2 扁平结构）"""
    import os
    import logging
    from app.services.media_processor import MediaProcessor
    from app.services.oss_media_cache_service import OSSMediaCacheService

    logger = logging.getLogger(__name__)
    base_path = os.path.join(os.getcwd(), "uploads", "datasets")
    processor = MediaProcessor(db, base_path=base_path)
    oss_service = OSSMediaCacheService()
    processed = []

    # 读取 value_type=hub_file 的数据集字段（field_type == "hub_file"，设计文档五）
    # 无该类字段时零行为变化，不影响 inline/local/oss 现有通道
    hub_fields: List[str] = []
    if getattr(task, "dataset_id", None):
        try:
            from app.services.hub_file_service import get_hub_file_fields
            hub_fields = get_hub_file_fields(db, task.dataset_id)
        except Exception as e:
            logger.warning(f"[_process_media_files] 读取 hub_file 字段元数据失败: {e}")

    # hub 拉取缓存使用任务级子目录 uploads/hub_files/{task_id}/：
    # 同 file_id 任务内只拉一次（缓存命中）；任务间目录隔离，避免并发任务互删缓存
    hub_cache_root: Optional[str] = None
    task_id = getattr(task, "id", None)
    if task_id:
        from app.services.hub_file_service import sanitize_hub_name
        hub_cache_root = os.path.join(
            os.getcwd(), "uploads", "hub_files", sanitize_hub_name(str(task_id))
        )

    for item in items:
        processed_item = item.copy()
        # V2 数据在 data JSON 中，所有字段扁平存放
        data_json = item.get("data") or {}
        if not data_json:
            processed.append(processed_item)
            continue

        # 处理 data JSON 中所有 *_is_media 为 true 的字段
        processed_data = dict(data_json)  # 浅拷贝
        for key, value in list(data_json.items()):
            if key.endswith("_is_media"):
                continue  # 跳过 is_media 标志字段本身
            is_media_key = f"{key}_is_media"
            is_media = data_json.get(is_media_key, False)
            # is_media 缺省为 false
            if not is_media:
                # 非媒体字段，如果有对应 is_media 字段且为 false，对纯文本内容做处理
                if isinstance(value, str) and value:
                    processed_data[f"{key}_processed"] = processor.process_content(value)
                continue

            # 媒体字段：获取 OSS 内容
            if isinstance(is_media, bool) and is_media:
                if isinstance(value, str) and value:
                    if oss_service.validate_oss_url(result_val := value):
                        media_content = oss_service.get_media_content(result_val)
                        if media_content is not None:
                            processed_data[key] = media_content
                        else:
                            logger.warning(f"[_process_media_files] OSS 媒体内容获取失败，{key} 置空: {value[:80]}")
                            processed_data[key] = ""
                    else:
                        logger.warning(f"[_process_media_files] {key} 字段值不符合 OSS URL 格式，置空: {value[:80]}")
                        processed_data[key] = ""
                    # 完成媒体转换后清除标志
                    processed_data[is_media_key] = False
            elif isinstance(is_media, str) and is_media.lower() in ["是", "yes", "true", "1"]:
                # 字符串形式的 is_media
                if isinstance(value, str) and value:
                    if oss_service.validate_oss_url(value):
                        media_content = oss_service.get_media_content(value)
                        if media_content is not None:
                            processed_data[key] = media_content
                        else:
                            logger.warning(f"[_process_media_files] OSS 媒体内容获取失败，{key} 置空: {value[:80]}")
                            processed_data[key] = ""
                    else:
                        logger.warning(f"[_process_media_files] {key} 字段值不符合 OSS URL 格式，置空: {value[:80]}")
                        processed_data[key] = ""
                    processed_data[is_media_key] = False

        # 处理 value_type=hub_file 的数据集字段：字段值为 anc-hub file_id 时
        # 复用登记表持久解包区镜像到任务目录并替换为该路径（阶段B）。
        # 缺持久解包区（HubFileNotAvailable，含 file_id）→ 该数据项标记失败
        # （_hub_resolve_error），不阻断其余数据；镜像落盘等其它异常保留原值
        if hub_fields:
            try:
                from app.services.hub_file_service import HubFileNotAvailable, resolve_hub_file_fields
                resolve_hub_file_fields(processed_data, hub_fields, cache_root=hub_cache_root, db=db)
            except HubFileNotAvailable as e:
                processed_item["_hub_resolve_error"] = str(e)
                logger.warning(
                    f"[_process_media_files] 数据项 {processed_item.get('id')} hub 执行包不可用，标记失败: {e}"
                )
            except Exception as e:
                logger.warning(f"[_process_media_files] hub_file 字段解析失败（保留原值）: {e}")

        processed_item["data"] = processed_data
        processed.append(processed_item)

    return processed


def _split_hub_unresolved_payloads(payloads: List[Dict]) -> "tuple[List[Dict], List[Dict]]":
    """把 hub 执行包解析失败的数据项从评测批次中拆出（阶段B）

    _process_media_files 中缺持久解包区的数据项被打上 _hub_resolve_error 标记
    （含 field 与 file_id）；此处拆为：
    - 失败结果列表：{idx, success: False, error}（与执行器异常分支同形态，
      聚合计入 failed_count，不发起模型评测）
    - 可执行 payload 列表：其余数据继续评测（任务不中断）

    idx 取原 payloads 序号，便于失败信息回溯到数据项。
    """
    failed_results: List[Dict] = []
    runnable: List[Dict] = []
    for idx, payload in enumerate(payloads):
        item = payload.get("_dataset_item") if isinstance(payload, dict) else None
        error = item.get("_hub_resolve_error") if isinstance(item, dict) else None
        if error:
            failed_results.append({"idx": idx, "success": False, "error": str(error)})
        else:
            runnable.append(payload)
    return failed_results, runnable


def _generate_payloads(task, items: List[Dict], db) -> List[Dict]:
    """生成请求Payload"""
    from app.services.variable_injector import VariableInjector

    injector = VariableInjector(task)
    payloads = []

    for item in items:
        payload = injector.generate_payload(item)
        payloads.append(payload)

    return payloads


def _execute_evaluation(task, payloads: List[Dict], db, task_id: str, stop_event: Optional[StopEventProtocol] = None) -> List[Dict]:
    """并发执行评测，传递 stop_event 以支持中断

    W3：行级 write-through——execution_number/续跑进度偏移从 task/db 推导
    （保持本函数签名与调用点稳定，兼容既有直调与打桩）。
    """
    from app.services.evaluation_executor import EvaluationExecutor
    from app.services.evaluation_result_service import get_completed_data_indexes

    # 获取配置实例
    settings = get_settings()
    langfuse_host = settings.LANGFUSE_HOST if settings.LANGFUSE_ENABLED else ""

    # 设置环境变量供 LangFuse v4 SDK 使用（CallbackHandler 内部读取）
    if settings.LANGFUSE_ENABLED:
        import os
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.LANGFUSE_PUBLIC_KEY)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.LANGFUSE_SECRET_KEY)
        os.environ.setdefault("LANGFUSE_HOST", settings.LANGFUSE_HOST)

    # W3：行级落库执行序号（start_task 已递增，与 stop/complete 同口径）
    execution_number = getattr(task, "execution_count", None) or 1
    # W3：续跑进度偏移（本轮已跳过的已完成行数；表不可用时为 0）
    progress_offset = len(get_completed_data_indexes(db, task_id, execution_number))

    # W17：类型执行路径收敛注册表——已迁移类型由适配器提供执行钩子；
    # 未迁移类型沿用下方专用路径，未知类型走默认裁判批量执行通道。
    # 函数体内延迟导入：与适配器模块（对执行函数延迟委托）互不构成导入环，
    # 经模块属性取钩子保持既有 patch(..._execute_xxx) 打桩方式生效
    from app.services.evaluators import evaluator_registry
    from app.services.evaluators.context import EvaluationContext

    type_adapter = evaluator_registry.get(task.evaluation_type)
    if type_adapter is not None:
        adapter_result = type_adapter.evaluate_batch(
            EvaluationContext(
                db=db,
                task_id=task_id,
                stop_event=stop_event,
                execution_number=execution_number,
                progress_offset=progress_offset,
                langfuse_host=langfuse_host,
            ),
            task,
            payloads,
        )
        if adapter_result is not None:
            return adapter_result

    # W17：rag 已迁移注册表（见上方类型适配器分发），专用路径保留
    # 供适配器委托与直调使用

    # 成功率评测走专用路径（不使用裁判模型）
    # W17：success_rate 已迁移注册表（见上方类型适配器分发），专用路径保留
    # 供适配器委托与直调使用

    # W17：latency 已迁移注册表（专用 EvaluationExecutor 构造在适配器内，
    # 与本分支逐行等价：max_workers=min(judge_count,4)、progress_offset 恒 0）

    # W17：binary_classification 已迁移注册表（见上方类型适配器分发），
    # 专用路径保留供适配器委托与直调使用

    executor = EvaluationExecutor(
        max_workers=min(task.judge_count or 4, 4),
        task_id=task_id,
        stop_event=stop_event,
        langfuse_host=langfuse_host,
        execution_number=execution_number,
        progress_offset=progress_offset,
    )

    results = executor.execute_batch(payloads, task)
    return results


def _execute_rag_evaluation(
    task,
    payloads: List[Dict],
    task_id: str,
    stop_event,
    langfuse_host="",
    execution_number: Optional[int] = None,
    progress_offset: int = 0,
) -> List[Dict]:
    """执行 RAG 评测，每条数据：调用 RAG 接口 → 评测各指标

    execution_number 为 None（直调/测试链路）时执行器跳过行级落库。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.services.evaluation_executor import EvaluationExecutor

    executor = EvaluationExecutor(
        max_workers=4,
        task_id=task_id,
        stop_event=stop_event,
        langfuse_host=langfuse_host,
        execution_number=execution_number,
        progress_offset=progress_offset,
    )

    results = executor.execute_batch_rag(payloads, task)
    return results


def _execute_consistency_evaluation(task, payloads: List[Dict], db, task_id: str, stop_event: Optional[StopEventProtocol] = None, execution_number: Optional[int] = None) -> List[Dict]:
    """执行一致性评测 - 多次调用模型计算一致性

    execution_number 为 None（直调/测试链路）时跳过行级落库。
    """
    import asyncio
    import logging
    from app.services.consistency_evaluator import ConsistencyEvaluator

    logger = logging.getLogger(__name__)

    # 获取配置
    consistency_config = task.consistency_config or {}
    repeat_count = consistency_config.get("repeat_count", 10)  # 默认10次
    use_judge = consistency_config.get("use_judge", False)     # 默认不使用裁判
    evaluation_metrics = task.evaluation_metrics or []  # 自定义评测维度

    logger.info(f"[_execute_consistency_evaluation] use_judge={use_judge}, evaluation_metrics={evaluation_metrics}, repeat_count={repeat_count}")

    # 获取模型配置：W4 快照优先（口径字段来自创建时快照，凭据实时解析），
    # 快照为空（存量任务）回退实时查询
    from app.services.config_snapshot import resolve_model_call_config

    model_config, _config_source = resolve_model_call_config(task, db)

    # 获取裁判配置（与 AI 裁判员一致）
    judge_variable_config = getattr(task, 'judge_variable_config', None)
    # 如果 task 没有 judge_variable_config，尝试从 consistency_config 中获取
    if not judge_variable_config and consistency_config:
        judge_variable_config = consistency_config.get("judge_variable_config")

    # 获取裁判模型配置：W4 快照优先链（创建时快照 → 全局 Claude Code 配置 →
    # task.judge_model 实时回退 → consistency_config.judge_config 兜底），
    # 与 AI 裁判员评测共用同一解析器
    judge_config = None
    if use_judge:
        from app.services.config_snapshot import resolve_judge_call_config

        judge_config, _judge_source = resolve_judge_call_config(task, db)
        if judge_config:
            logger.info(f"[_execute_consistency_evaluation] 裁判配置来源: {_judge_source}")

    results = []
    total = len(payloads)

    for idx, payload in enumerate(payloads):
        if stop_event and stop_event.is_set():
            logger.info(f"一致性评测任务 {task_id} 收到停止信号，已完成 {len(results)}/{total} 条")
            break

        # 平铺 payload 中的输入变量
        input_variables = {k: v for k, v in payload.items()
                           if k not in ("_dataset_item", "idx") and not k.startswith("_")}

        # 获取 user_input 用于调用模型（管线管道名，内部使用）
        user_input = payload.get("user_input", "") or payload.get("question", "")

        # 获取当前数据项（用于变量解析）
        dataset_item = payload

        # 获取评测目标配置
        evaluation_targets = getattr(task, 'evaluation_targets', None)

        try:
            # 创建一致性评测器，传递裁判配置（与 AI 裁判员一致）
            evaluator = ConsistencyEvaluator(
                model_config=model_config,
                judge_config=judge_config,
                progress_callback=None,
                evaluation_metrics=evaluation_metrics,
                judge_variable_config=judge_variable_config,
                dataset_item=dataset_item,
                chain_results=[],
                evaluation_targets=evaluation_targets,
            )

            # 由于 ConsistencyEvaluator 是异步的，需要用 asyncio 调用
            # 临时 loop 用完须恢复先前的 current 事件循环——否则已关闭 loop 残留为
            # current，同线程后续 asyncio 调用全部报「Event loop is closed」
            prev_loop = _get_current_event_loop()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                consistency_result = loop.run_until_complete(
                    evaluator.evaluate_single(user_input, repeat_count, use_judge)
                )
            finally:
                _restore_event_loop(prev_loop)
                loop.close()

            result_item = {
                "idx": idx,
                "success": True,
                "metrics": consistency_result.metrics,  # 包含 text_similarity, format_consistency, structure_consistency
                "judge_assessment": consistency_result.judge_assessment,
                "repeat_results": consistency_result.repeat_results,
                "evaluation_content": consistency_result.evaluation_content,  # 待评测内容
            }
            # 平铺输入变量
            result_item.update(input_variables)
            results.append(result_item)

            # W3-2：行级 write-through（每行终态即时幂等落库，P0-E）
            _persist_row(task_id, execution_number, payload.get("_orig_idx", idx), result_item)

            # 更新进度
            _update_progress(len(results), total, task_id, execution_number)

        except SoftTimeLimitExceeded:
            # W3-4：软超时不得按行失败吞掉（P0-E），上抛触发收尾
            raise
        except Exception as e:
            logger.exception(f"一致性评测执行失败 (idx={idx})")
            failed_item = {
                "idx": idx,
                "success": False,
                "error": str(e),
            }
            results.append(failed_item)
            _persist_row(task_id, execution_number, payload.get("_orig_idx", idx), failed_item)

    # 按原始顺序排序结果
    results.sort(key=lambda x: x.get("idx", 0))
    return results


def _execute_success_rate_evaluation(task, payloads: List[Dict], task_id: str, db, stop_event: Optional[StopEventProtocol] = None, execution_number: Optional[int] = None) -> List[Dict]:
    """执行成功率评测 - 多次调用模型统计成功率，不使用裁判模型

    execution_number 为 None（直调/测试链路）时跳过行级落库。
    """
    import asyncio
    import logging
    from app.services.success_rate_evaluator import SuccessRateEvaluator

    logger = logging.getLogger(__name__)

    # 获取配置
    success_rate_config = task.success_rate_config or {}
    repeat_count = success_rate_config.get("repeat_count", 10)  # 默认10次

    logger.info(f"[_execute_success_rate_evaluation] repeat_count={repeat_count}")

    # 获取模型配置：W4 快照优先（口径字段来自创建时快照，凭据实时解析），
    # 快照为空（存量任务）回退实时查询
    from app.services.config_snapshot import resolve_model_call_config

    model_config, _config_source = resolve_model_call_config(task, db)

    results = []
    total = len(payloads)

    for idx, payload in enumerate(payloads):
        if stop_event and stop_event.is_set():
            logger.info(f"成功率评测任务 {task_id} 收到停止信号，已完成 {len(results)}/{total} 条")
            break

        # 平铺 payload 中的输入变量
        input_variables = {k: v for k, v in payload.items()
                           if k not in ("_dataset_item", "idx") and not k.startswith("_")}

        # 获取 user_input 用于调用模型（管线管道名，内部使用）
        user_input = payload.get("user_input", "") or payload.get("question", "")

        try:
            # 创建成功率评测器（不使用裁判模型）
            evaluator = SuccessRateEvaluator(
                model_config=model_config,
                success_criteria=success_rate_config.get("success_criteria"),
                progress_callback=None,
            )

            # 由于 SuccessRateEvaluator 是异步的，需要用 asyncio 调用
            prev_loop = _get_current_event_loop()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                success_rate_result = loop.run_until_complete(
                    evaluator.evaluate_single(user_input, repeat_count)
                )
            finally:
                _restore_event_loop(prev_loop)
                loop.close()

            result_item = {
                "idx": idx,
                "success": True,
                "metrics": success_rate_result.metrics,
                "repeat_results": success_rate_result.repeat_results,
                "error_analysis": success_rate_result.error_analysis,
            }
            # 平铺输入变量
            result_item.update(input_variables)
            results.append(result_item)

            # W3-2：行级 write-through（每行终态即时幂等落库，P0-E）
            _persist_row(task_id, execution_number, payload.get("_orig_idx", idx), result_item)

            # 更新进度
            _update_progress(len(results), total, task_id, execution_number)

        except SoftTimeLimitExceeded:
            # W3-4：软超时不得按行失败吞掉（P0-E），上抛触发收尾
            raise
        except Exception as e:
            logger.exception(f"成功率评测执行失败 (idx={idx})")
            failed_item = {
                "idx": idx,
                "success": False,
                "error": str(e),
            }
            results.append(failed_item)
            _persist_row(task_id, execution_number, payload.get("_orig_idx", idx), failed_item)

    # 按原始顺序排序结果
    results.sort(key=lambda x: x.get("idx", 0))
    return results


def _execute_binary_classification_evaluation(task, payloads: List[Dict], task_id: str, db, stop_event: Optional[StopEventProtocol] = None, execution_number: Optional[int] = None) -> List[Dict]:
    """执行二分分类评测

    execution_number 为 None（直调/测试链路）时跳过行级落库。
    """
    import asyncio
    import logging
    import time
    from app.services.binary_classification_evaluator import BinaryClassificationEvaluator
    from app.services.model_caller import ModelCallerSync

    logger = logging.getLogger(__name__)

    # 获取模型配置：W4 快照优先（口径字段来自创建时快照，凭据实时解析），
    # 快照为空（存量任务）回退实时查询
    from app.services.config_snapshot import resolve_model_call_config

    model_config, _config_source = resolve_model_call_config(task, db)
    logger.info(f"[二分分类评测] task_id={task_id}, model_provider={task.model_provider}, model_config_keys={list(model_config.keys()) if model_config else 'EMPTY'}")

    # 获取配置
    binary_config = getattr(task, 'binary_metrics_config', None) or {}
    expected_label_config = binary_config.get("expected_label", {"source": "dataset_field", "source_config": {"field": "expected_label"}})
    predicted_label_config = binary_config.get("predicted_label", {"source": "model_output", "source_config": {"field": "prediction"}})
    positive_value = binary_config.get("positive_value", "true")
    label_type = binary_config.get("label_type", "boolean")

    results = []
    total = len(payloads)
    model_caller = ModelCallerSync()

    for idx, payload in enumerate(payloads):
        if stop_event and stop_event.is_set():
            logger.info(f"二分分类评测任务 {task_id} 收到停止信号，已完成 {len(results)}/{total} 条")
            break

        try:
            # 平铺 payload 中的输入变量
            input_variables = {k: v for k, v in payload.items()
                               if k not in ("_dataset_item", "idx") and not k.startswith("_")}

            # 先调用待评测模型获取预测结果
            user_input = payload.get("user_input") or ""
            messages = [{"role": "user", "content": user_input}]
            runtime_variables = {
                k: v for k, v in payload.items()
                if k not in ("user_input", "ref_answer", "model_answer", "_dataset_item", "messages", "idx")
            }

            # 处理 runtime_variables 中的 { value, type } 格式
            for key, val in list(runtime_variables.items()):
                if isinstance(val, dict) and 'value' in val and 'type' in val:
                    var_type = val.get('type', 'string')
                    var_value = val.get('value', '')
                    if var_type == 'number':
                        try:
                            runtime_variables[key] = float(var_value) if '.' in str(var_value) else int(var_value)
                        except (ValueError, TypeError):
                            runtime_variables[key] = 0
                    elif var_type == 'boolean':
                        runtime_variables[key] = str(var_value).lower() in ('true', '1', 'yes')
                    else:
                        runtime_variables[key] = str(var_value)

            logger.info(f"[二分分类评测] idx={idx}, 开始调用模型, user_input={user_input[:100]}")

            start_time = time.perf_counter()
            call_result = model_caller.call(messages, model_config, runtime_variables)
            duration = time.perf_counter() - start_time

            logger.info(f"[二分分类评测] idx={idx}, 模型调用完成, duration={duration:.3f}s, success={call_result.get('success')}")

            if not call_result.get("success"):
                raise RuntimeError(f"模型调用失败: {call_result.get('error', '未知错误')}")

            # 解析模型输出
            response_text = call_result.get("content", "")
            model_output = {"content": response_text, "prediction": response_text, "duration": duration}
            if call_result.get("raw", {}).get("usage"):
                usage = call_result["raw"]["usage"]
                model_output["input_tokens"] = usage.get("prompt_tokens") or usage.get("input_tokens")
                model_output["output_tokens"] = usage.get("completion_tokens") or usage.get("completion_tokens")

            # 提取请求链结果（用于 chain_result 来源的标签解析）
            chain_results = call_result.get("chain_results", [])
            logger.info(f"[二分分类评测] idx={idx}, chain_results_count={len(chain_results)}, "
                        f"chain_keys={[list(cr.get('variables', {}).keys()) for cr in chain_results]}")

            # 执行评测比较
            evaluator = BinaryClassificationEvaluator(
                expected_label_config=expected_label_config,
                predicted_label_config=predicted_label_config,
                positive_value=positive_value,
                label_type=label_type,
            )

            prev_loop = _get_current_event_loop()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                eval_results = loop.run_until_complete(
                    evaluator.evaluate_batch([payload], [model_output], [chain_results])
                )
            finally:
                _restore_event_loop(prev_loop)
                loop.close()

            if eval_results:
                result = eval_results[0]
                result_item = {
                    "idx": idx,
                    "success": result.success,
                    "expected_label": result.expected_label,
                    "predicted_label": result.predicted_label,
                    "is_correct": result.is_correct,
                    "duration": duration,
                    "error": result.error,
                    "model_output": response_text,
                }
                # 平铺输入变量
                result_item.update(input_variables)
                # 平铺 input_data（来自 evaluator）
                if result.input_data:
                    result_item.update(result.input_data)
                results.append(result_item)
            else:
                result_item = {
                    "idx": idx,
                    "success": False,
                    "error": "No evaluation result returned",
                }
                results.append(result_item)

            # W3-2：行级 write-through（每行终态即时幂等落库，P0-E）
            _persist_row(task_id, execution_number, payload.get("_orig_idx", idx), results[-1])

            _update_progress(len(results), total, task_id, execution_number)

        except SoftTimeLimitExceeded:
            # W3-4：软超时不得按行失败吞掉（P0-E），上抛触发收尾
            raise
        except Exception as e:
            logger.exception(f"二分分类评测执行失败 (idx={idx})")
            failed_item = {
                "idx": idx,
                "success": False,
                "error": str(e),
            }
            results.append(failed_item)
            _persist_row(task_id, execution_number, payload.get("_orig_idx", idx), failed_item)

    results.sort(key=lambda x: x.get("idx", 0))
    return results


def _get_current_event_loop():
    """获取当前线程已设置的 current 事件循环（不新建；无则返回 None）

    供临时 loop 的保存/恢复配对使用：asyncio.set_event_loop(loop) + loop.close()
    后若不恢复，已关闭 loop 会残留为 current，同线程后续 asyncio 调用
    （含 nest_asyncio.run）将全部报「Event loop is closed」。
    """
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        # 非主线程且未设置 current 时的平台行为差异，视为无
        return None


def _restore_event_loop(loop):
    """恢复 current 事件循环（None 表示清空引用，下次按需新建）"""
    asyncio.set_event_loop(loop)


def _update_progress(completed: int, total: int, task_id: str, execution_number: Optional[int] = None):
    """更新任务进度（consistency/success_rate/binary 专用路径）

    W10-4：进度推进即一批行完成，同步刷新执行记录心跳（heartbeat_at）——
    API 启动期收尸 reaper 以心跳区分「正在执行」与「worker 已死」。
    """
    try:
        from app.services.evaluation_task_service import EvaluationTaskService

        db = SessionLocal()
        try:
            service = EvaluationTaskService(db)
            service.update_progress(task_id, completed, total)
            service.touch_heartbeat(task_id, execution_number)
        finally:
            db.close()
    except Exception:
        pass


def _aggregate_results(results: List[Dict], task) -> Dict[str, Any]:
    """聚合评测结果（分发逻辑收敛在 ResultAggregator.aggregate_for_task 单点维护，
    W3 的 stop 部分结果聚合与 complete 聚合共用同一口径）"""
    from app.services.result_aggregator import ResultAggregator

    return ResultAggregator.aggregate_for_task(task, results)
