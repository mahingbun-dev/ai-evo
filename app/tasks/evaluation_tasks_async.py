"""
评测任务 - Celery 版本

使用 Celery Worker 执行评测任务。
通过 RedisStopSignalChecker 或 threading.Event 支持任务真正中断。
"""
import asyncio
import logging
import threading
from typing import Dict, Any, List, Optional, Protocol

from app.core.config import get_settings
from app.db.session import engine, SessionLocal
from app.services.evaluation_task_service import EvaluationTaskService, TaskExecutionLogService

logger = logging.getLogger(__name__)


class TaskStoppedException(Exception):
    """任务被主动停止时抛出"""
    pass


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
    5. 并发执行评测
    6. 聚合结果
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

        # ========== 步骤2: 加载数据集 ==========
        log_service.add_log(task_id, "info", "开始加载数据集")
        dataset_items = _load_dataset(task, db)
        log_service.add_log(task_id, "info", f"数据集加载完成，共 {len(dataset_items)} 条数据")

        # 更新题目总数
        task_service.update_progress(task_id, 0, len(dataset_items))

        # ========== 步骤3: 处理媒体文件 ==========
        log_service.add_log(task_id, "info", "开始处理媒体文件")
        processed_items = _process_media_files(task, dataset_items, db)
        log_service.add_log(task_id, "info", "媒体文件处理完成")

        # ========== 步骤4: 生成Payload ==========
        log_service.add_log(task_id, "info", "开始生成请求Payload")
        payloads = _generate_payloads(task, processed_items, db)
        log_service.add_log(task_id, "info", f"Payload生成完成，共 {len(payloads)} 个")

        # ========== 步骤5: 并发执行评测 ==========
        # 执行前检查是否已被停止（可能在排队期间被停止）
        if stop_event.is_set():
            log_service.add_log(task_id, "info", "任务在开始执行前被停止")
            task_service.stop_task(task_id)
            return {"status": "stopped", "task_id": task_id}

        # hub 执行包不可用的数据项直接产出失败结果，不发起评测（阶段B：
        # 数据项级失败，其余数据继续评测、任务不中断，错误信息可定位 file_id）
        hub_failed_results, runnable_payloads = _split_hub_unresolved_payloads(payloads)
        if hub_failed_results:
            log_service.add_log(
                task_id, "warning",
                f"{len(hub_failed_results)} 条数据的 hub 执行包不可用，已标记失败（其余数据继续评测）",
            )

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
        log_service.add_log(task_id, "info", "开始聚合结果")
        aggregated_result = _aggregate_results(results, task)
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
        # hub 拉取缓存随任务结束清理（成功/失败/停止统一覆盖，与 judge_sessions
        # 临时文件同批清理时机一致，设计文档 §七.6）
        _cleanup_task_hub_cache(task_id)
        # 不再主动清理停止信号，让 on_revoke 回调处理
        # 如果是 RedisStopSignalChecker，on_revoke 会调用 stop_task(task_id) 更新状态


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
    """并发执行评测，传递 stop_event 以支持中断"""
    from app.services.evaluation_executor import EvaluationExecutor

    # 获取配置实例
    settings = get_settings()
    langfuse_host = settings.LANGFUSE_HOST if settings.LANGFUSE_ENABLED else ""

    # 设置环境变量供 LangFuse v4 SDK 使用（CallbackHandler 内部读取）
    if settings.LANGFUSE_ENABLED:
        import os
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.LANGFUSE_PUBLIC_KEY)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.LANGFUSE_SECRET_KEY)
        os.environ.setdefault("LANGFUSE_HOST", settings.LANGFUSE_HOST)

    # RAG 评测走专用路径
    if task.evaluation_type == "rag":
        return _execute_rag_evaluation(task, payloads, task_id, stop_event, langfuse_host)

    # 一致性评测走专用路径
    if task.evaluation_type == "consistency":
        return _execute_consistency_evaluation(task, payloads, db, task_id, stop_event)

    # 成功率评测走专用路径（不使用裁判模型）
    if task.evaluation_type == "success_rate":
        return _execute_success_rate_evaluation(task, payloads, task_id, db, stop_event)

    # 耗时评测走专用路径
    if task.evaluation_type == "latency":
        executor = EvaluationExecutor(
            max_workers=min(task.judge_count or 4, 4),
            task_id=task_id,
            stop_event=stop_event,
            langfuse_host=langfuse_host,
        )
        return executor.execute_batch_latency(payloads, task)

    # 二分分类评测走专用路径
    if task.evaluation_type == "binary_classification":
        return _execute_binary_classification_evaluation(task, payloads, task_id, db, stop_event)

    executor = EvaluationExecutor(
        max_workers=min(task.judge_count or 4, 4),
        task_id=task_id,
        stop_event=stop_event,
        langfuse_host=langfuse_host,
    )

    results = executor.execute_batch(payloads, task)
    return results


def _execute_rag_evaluation(task, payloads: List[Dict], task_id: str, stop_event, langfuse_host="") -> List[Dict]:
    """执行 RAG 评测，每条数据：调用 RAG 接口 → 评测各指标"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.services.evaluation_executor import EvaluationExecutor

    executor = EvaluationExecutor(
        max_workers=4,
        task_id=task_id,
        stop_event=stop_event,
        langfuse_host=langfuse_host,
    )

    results = executor.execute_batch_rag(payloads, task)
    return results


def _execute_consistency_evaluation(task, payloads: List[Dict], db, task_id: str, stop_event: Optional[StopEventProtocol] = None) -> List[Dict]:
    """执行一致性评测 - 多次调用模型计算一致性"""
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

    # 获取模型配置
    model_config = task.model_config or {}
    if not model_config and task.model_provider:
        model_config = _load_model_config(task.model_provider, db)

    # 获取裁判配置（与 AI 裁判员一致）
    judge_variable_config = getattr(task, 'judge_variable_config', None)
    # 如果 task 没有 judge_variable_config，尝试从 consistency_config 中获取
    if not judge_variable_config and consistency_config:
        judge_variable_config = consistency_config.get("judge_variable_config")

    # 获取裁判模型配置（优先全局 Claude Code 配置，与 AI 裁判员评测一致）
    judge_config = None
    if use_judge:
        try:
            from app.services.claude_code_config_service import ClaudeCodeConfigService

            judge_config = ClaudeCodeConfigService.get_judge_llm_config()
        except Exception:
            judge_config = None
        if judge_config:
            logger.info("[_execute_consistency_evaluation] 使用全局 Claude Code 配置作为裁判配置")
        # 回退：历史 task.judge_model（软退场，保证历史任务可复跑）
        elif getattr(task, 'judge_model', None):
            judge_config = _load_model_config(task.judge_model, db)
            logger.info(f"[_execute_consistency_evaluation] 从 task.judge_model 加载裁判模型配置: model_id={task.judge_model}")
        # 再回退到 consistency_config 中的 judge_config
        elif consistency_config and consistency_config.get("judge_config"):
            judge_config = consistency_config.get("judge_config")
            logger.info(f"[_execute_consistency_evaluation] 从 consistency_config 加载裁判模型配置")

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

            # 更新进度
            _update_progress(len(results), total, task_id)

        except Exception as e:
            logger.exception(f"一致性评测执行失败 (idx={idx})")
            results.append({
                "idx": idx,
                "success": False,
                "error": str(e),
            })

    # 按原始顺序排序结果
    results.sort(key=lambda x: x.get("idx", 0))
    return results


def _execute_success_rate_evaluation(task, payloads: List[Dict], task_id: str, db, stop_event: Optional[StopEventProtocol] = None) -> List[Dict]:
    """执行成功率评测 - 多次调用模型统计成功率，不使用裁判模型"""
    import asyncio
    import logging
    from app.services.success_rate_evaluator import SuccessRateEvaluator

    logger = logging.getLogger(__name__)

    # 获取配置
    success_rate_config = task.success_rate_config or {}
    repeat_count = success_rate_config.get("repeat_count", 10)  # 默认10次

    logger.info(f"[_execute_success_rate_evaluation] repeat_count={repeat_count}")

    # 获取模型配置
    model_config = task.model_config or {}
    if not model_config and task.model_provider:
        model_config = _load_model_config(task.model_provider, db)

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

            # 更新进度
            _update_progress(len(results), total, task_id)

        except Exception as e:
            logger.exception(f"成功率评测执行失败 (idx={idx})")
            results.append({
                "idx": idx,
                "success": False,
                "error": str(e),
            })

    # 按原始顺序排序结果
    results.sort(key=lambda x: x.get("idx", 0))
    return results


def _execute_binary_classification_evaluation(task, payloads: List[Dict], task_id: str, db, stop_event: Optional[StopEventProtocol] = None) -> List[Dict]:
    """执行二分分类评测"""
    import asyncio
    import logging
    import time
    from app.services.binary_classification_evaluator import BinaryClassificationEvaluator
    from app.services.model_caller import ModelCallerSync

    logger = logging.getLogger(__name__)

    # 获取模型配置
    model_config = task.model_config or {}
    if not model_config and task.model_provider:
        model_config = _load_model_config(task.model_provider, db)
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
                results.append({
                    "idx": idx,
                    "success": False,
                    "error": "No evaluation result returned",
                })

            _update_progress(len(results), total, task_id)

        except Exception as e:
            logger.exception(f"二分分类评测执行失败 (idx={idx})")
            results.append({
                "idx": idx,
                "success": False,
                "error": str(e),
            })

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


def _load_model_config(model_provider_id: str, db) -> Dict[str, Any]:
    """从数据库加载模型配置"""
    from app.models.model_config_models import ModelConfig

    session = SessionLocal()
    try:
        model_config_obj = session.query(ModelConfig).filter(ModelConfig.id == model_provider_id).first()
        if model_config_obj:
            return {
                "provider": model_config_obj.provider,
                "model_type": model_config_obj.model_type,
                "endpoint": model_config_obj.endpoint,
                "api_key": model_config_obj.api_key,
                "auth_type": model_config_obj.auth_type,
                "auth_config": model_config_obj.auth_config,
                "request_config": model_config_obj.request_config,
                "response_parse_config": model_config_obj.response_parse_config,
                "model_id": model_config_obj.id,
            }
    finally:
        session.close()
    return {}


def _update_progress(completed: int, total: int, task_id: str):
    """更新任务进度"""
    try:
        from app.services.evaluation_task_service import EvaluationTaskService

        db = SessionLocal()
        try:
            service = EvaluationTaskService(db)
            service.update_progress(task_id, completed, total)
        finally:
            db.close()
    except Exception:
        pass


def _aggregate_results(results: List[Dict], task) -> Dict[str, Any]:
    """聚合评测结果"""
    from app.services.result_aggregator import ResultAggregator

    if task.evaluation_type == "rag":
        return ResultAggregator.aggregate_rag_results(results)

    # 一致性评测走专用聚合
    if task.evaluation_type == "consistency":
        return ResultAggregator.aggregate_consistency_results(results)

    # 成功率评测走专用聚合
    if task.evaluation_type == "success_rate":
        return ResultAggregator.aggregate_success_rate_results(results)

    # 耗时评测走专用聚合
    if task.evaluation_type == "latency":
        latency_config = task.latency_config or {}
        threshold = latency_config.get("threshold", 10.0)
        return ResultAggregator.calculate_latency_metrics(results, threshold=threshold)

    # 二分分类评测走专用聚合
    if task.evaluation_type == "binary_classification":
        binary_metrics_config = getattr(task, 'binary_metrics_config', None) or []
        return ResultAggregator.aggregate_binary_classification_results(results, binary_metrics_config)

    aggregator = ResultAggregator(task)
    return aggregator.aggregate(results)
