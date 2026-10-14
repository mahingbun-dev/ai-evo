"""
评测任务管理服务
"""
import json
import time
import uuid
import logging
import threading
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError, IntegrityError
from sqlalchemy import func
import redis
from app.core.config import get_settings
from app.models.task_models import EvaluationTask, TaskExecutionLog, TaskExecutionRecord

logger = logging.getLogger(__name__)

# 北京时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now():
    """获取当前北京时间"""
    return datetime.now(BEIJING_TZ)


# 模块级 Redis 发布客户端：状态更新是高频调用（start/stop/complete/fail/progress），
# 复用内建连接池的进程级单例，避免每次 publish 都 redis.from_url 新建连接。
# 惰性创建，celery prefork worker 在 fork 后首次 publish 才建连。
_redis_publisher: Optional["redis.Redis"] = None
_redis_publisher_lock = threading.Lock()


def _get_redis_publisher() -> "redis.Redis":
    """获取模块级 Redis 发布客户端（内建连接池，进程内复用连接）"""
    global _redis_publisher
    with _redis_publisher_lock:
        if _redis_publisher is None:
            _redis_publisher = redis.from_url(get_settings().REDIS_URL)
        return _redis_publisher


# ---------------- W10-4 心跳写库节流 ----------------

# 同任务两次心跳写库的最小间隔（秒）。收尸 reaper 阈值 15 分钟，分钟级心跳
# 足够；行级完成高频时避免每行一次额外 UPDATE/commit（也避免拖慢主落库循环）
HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
# 节流表容量上限：超过即清理过期项（worker 常驻进程防无界增长）
_HEARTBEAT_TABLE_MAX = 4096

_heartbeat_last_write: Dict[str, float] = {}
_heartbeat_lock = threading.Lock()


def _heartbeat_throttled(task_id: str) -> bool:
    """节流判定：距该任务上次心跳写库不足间隔时返回 True（并登记本次意图）"""
    now = time.monotonic()
    with _heartbeat_lock:
        if len(_heartbeat_last_write) > _HEARTBEAT_TABLE_MAX:
            for k in [k for k, t in _heartbeat_last_write.items()
                      if now - t > HEARTBEAT_MIN_INTERVAL_SECONDS]:
                _heartbeat_last_write.pop(k, None)
        last = _heartbeat_last_write.get(task_id)
        if last is not None and (now - last) < HEARTBEAT_MIN_INTERVAL_SECONDS:
            return True
        _heartbeat_last_write[task_id] = now
        return False


class EvaluationTaskService:
    """评测任务服务"""

    def __init__(self, db: Session):
        self.db = db
        self._log_service = None

    @property
    def log_service(self):
        """获取日志服务实例"""
        if self._log_service is None:
            self._log_service = TaskExecutionLogService(self.db)
        return self._log_service

    def add_log(self, task_id: str, level: str, message: str, details: Optional[Dict] = None):
        """添加日志的便捷方法"""
        return self.log_service.add_log(task_id, level, message, details)

    def _publish_status_update(self, task: EvaluationTask):
        """发布任务状态更新到 Redis Pub/Sub（复用模块级连接池）"""
        try:
            r = _get_redis_publisher()
            message = {
                "type": "status_update",
                "task_id": task.id,
                "status": task.status,
                "progress": task.progress or 0,
                "completed_questions": task.completed_questions or 0,
                "total_questions": task.total_questions or 0,
                "timestamp": beijing_now().isoformat()
            }
            r.publish(f"task_status:{task.id}", json.dumps(message))
        except Exception as e:
            logger.warning(f"发布任务状态更新失败: {e}")

    def create_task(
        self,
        name: str,
        evaluation_type: str,
        description: Optional[str] = None,
        model_config: Optional[Dict] = None,
        model_provider: Optional[str] = None,
        model_endpoint: Optional[str] = None,
        dataset_id: Optional[str] = None,
        judge_model: Optional[str] = None,
        judge_engine: Optional[str] = None,
        judge_prompt: Optional[str] = None,
        consistency_config: Optional[Dict] = None,
        success_rate_config: Optional[Dict] = None,
        rag_config: Optional[Dict] = None,
        latency_config: Optional[Dict] = None,
        binary_metrics_config: Optional[Dict] = None,
        multi_judge_config: Optional[Dict] = None,
        parallel_config: Optional[Dict] = None,
        evaluation_scene: Optional[str] = None,
        scene_desc: Optional[str] = None,
        evaluation_metrics: Optional[List[Dict]] = None,
        max_score: Optional[int] = 10,
        score_details: Optional[str] = None,
        steps: Optional[List[str]] = None,
        created_by: Optional[str] = None,
        runtime_variables: Optional[Dict] = None,
        variable_sources: Optional[Dict[str, str]] = None,
        variable_types: Optional[Dict[str, str]] = None,
        variable_mappings: Optional[Dict[str, str]] = None,
        enable_user_import: Optional[bool] = False,
        user_import_template: Optional[str] = None,
        judge_count: Optional[int] = 6,
        team_code: Optional[str] = None,
        judge_variable_config: Optional[Dict] = None,
        evaluation_targets: Optional[List[Dict]] = None,
        validation_enabled: Optional[bool] = True,
        template_id: Optional[str] = None,
        pre_scripts: Optional[List[Dict]] = None,
    ) -> EvaluationTask:
        """创建评测任务"""
        # 验证评测类型
        # W12-2：latency 白名单激活（执行/聚合/导出链早已就绪，此前仅因白名单缺席被拒）
        valid_evaluation_types = ["judge", "consistency", "success_rate", "rag", "binary_classification", "agent", "latency"]
        if evaluation_type not in valid_evaluation_types:
            raise ValueError(f"不支持的评测类型: {evaluation_type}，可选值: {valid_evaluation_types}")

        # 验证裁判引擎
        if judge_engine and judge_engine not in ("claude_agent", "graph"):
            raise ValueError(f"不支持的裁判引擎: {judge_engine}，可选值: claude_agent, graph")

        task_id = f"task_{uuid.uuid4().hex[:12]}"

        # 如果指定了数据集，获取数据集条目数
        total_questions = 0
        if dataset_id:
            from app.services.dataset_service import DatasetDataService
            data_service = DatasetDataService(self.db)
            items = data_service.get_data_list_v2(dataset_id, skip=0, limit=10000)
            total_questions = len(items)
            # 长会话 Agent 评测：数据集必须由会话文件导入（含 session_messages 字段）
            if evaluation_type == "agent":
                if not items:
                    raise ValueError("长会话 Agent 评测的数据集为空，请先通过会话文件导入数据")
                first_raw = items[0]
                if isinstance(first_raw, dict):
                    first_data = first_raw.get("data") or {}
                else:
                    # get_data_list_v2 返回 ORM 对象，data 是其属性
                    first_data = getattr(first_raw, "data", None) or {}
                if not isinstance(first_data, dict):
                    first_data = {}
                if "session_messages" not in first_data:
                    raise ValueError("长会话 Agent 评测要求数据集包含 session_messages 字段，请在数据集管理页通过「会话文件导入」创建")

        # 待评测数据配置校验（契约 v3 §五）：targets 必填、script_output 行、@ 引用标记；
        # pre_scripts/steps 旧字段仅兼容接收，写路径一律置 None（targets 为唯一事实源，维度顺序即步骤）
        self._validate_targets_required(evaluation_type, consistency_config, evaluation_targets)
        self._validate_script_targets(evaluation_targets, dataset_id)
        self._validate_reference_marks(
            score_details,
            evaluation_metrics,
            judge_variable_config,
            evaluation_targets,
        )

        # W4（P0-C）配置快照：创建时对被评测模型（model_provider）与裁判模型
        # （judge_model）各做一次深拷贝快照，写入 task.model_config /
        # task.judge_model_config。快照只含口径字段+掩码凭据（严禁明文
        # api_key/auth_config），执行时「快照优先、实时回退」。
        # model_config 显式传参时透传优先（现网前端不传该字段，快照为主路径）
        from app.services.config_snapshot import build_model_config_snapshot

        effective_model_config = (
            model_config if model_config
            else build_model_config_snapshot(self.db, model_provider)
        )
        judge_model_config_snapshot = build_model_config_snapshot(self.db, judge_model)

        task = EvaluationTask(
            id=task_id,
            name=name,
            description=description,
            evaluation_type=evaluation_type,
            status="pending",
            model_config=effective_model_config,
            model_provider=model_provider,
            model_endpoint=model_endpoint,
            dataset_id=dataset_id,
            judge_model=judge_model,
            judge_engine=judge_engine or "claude_agent",
            judge_prompt=judge_prompt,
            consistency_config=consistency_config,
            success_rate_config=success_rate_config,
            rag_config=rag_config,
            latency_config=latency_config,
            binary_metrics_config=binary_metrics_config,
            multi_judge_config=multi_judge_config,
            judge_count=judge_count,
            # W4：创建时裁判配置快照（judge_model 为空时为 None，
            # claude_agent 引擎裁判走 CC 全局配置不消费该字段）
            judge_model_config=judge_model_config_snapshot,
            parallel_config=parallel_config,
            evaluation_scene=evaluation_scene,
            scene_desc=scene_desc,
            evaluation_metrics=evaluation_metrics,
            max_score=max_score,
            score_details=score_details,
            # 契约 v3 §五：steps 一律写 None（维度顺序即步骤），不再持久化任务级评分步骤
            steps=None,
            created_by=created_by,
            runtime_variables=runtime_variables,
            variable_sources=variable_sources,
            variable_types=variable_types,
            variable_mappings=variable_mappings,
            enable_user_import=enable_user_import,
            user_import_template=user_import_template,
            judge_variable_config=judge_variable_config,
            evaluation_targets=evaluation_targets,
            validation_enabled=validation_enabled,
            total_questions=total_questions,
            team_code=team_code or "DEFAULT",
            template_id=template_id,
            # 契约 v3 §五：pre_scripts 一律写 None（脚本编排迁入 evaluation_targets 的 script_output 行）
            pre_scripts=None,
        )

        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)

        return task

    def get_task(self, task_id: str) -> Optional[EvaluationTask]:
        """获取任务"""
        return self.db.query(EvaluationTask).filter(
            EvaluationTask.id == task_id
        ).first()

    # 任务列表不加载的大 JSON 列：均不在 TaskResponse 中（result 列存量平均
    # 127KB/行），整列加载导致列表接口秒级耗时；详情/编辑接口不受影响
    _TASK_LIST_DEFERRED_COLUMNS = (
        "result", "model_config", "consistency_config", "success_rate_config",
        "rag_config", "latency_config", "multi_judge_config", "parallel_config",
    )

    def get_tasks(
        self,
        status: Optional[str] = None,
        evaluation_type: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        team_code: Optional[str] = None,
    ) -> List[EvaluationTask]:
        """获取评测任务列表"""
        from sqlalchemy.orm import defer

        query = self.db.query(EvaluationTask).filter(
            EvaluationTask.is_active == True
        ).options(
            *[defer(getattr(EvaluationTask, col)) for col in self._TASK_LIST_DEFERRED_COLUMNS]
        )

        if status:
            query = query.filter(EvaluationTask.status == status)
        if evaluation_type:
            query = query.filter(EvaluationTask.evaluation_type == evaluation_type)
        if team_code:
            query = query.filter(EvaluationTask.team_code == team_code)

        return query.order_by(EvaluationTask.created_at.desc()).offset(skip).limit(limit).all()

    def update_task(
        self,
        task_id: str,
        **kwargs,
    ) -> Optional[EvaluationTask]:
        """更新任务"""
        task = self.get_task(task_id)
        if not task:
            return None

        # 更新前校验待评测数据配置（契约 v3 §五）：targets 必填、script_output 行、@ 引用标记
        # 字段缺省时取任务既有值合并成最终态校验（含 targets/dataset_id/评测类型同批更新场景）
        effective_type = kwargs.get("evaluation_type") or task.evaluation_type
        effective_consistency = (
            kwargs.get("consistency_config") if "consistency_config" in kwargs else task.consistency_config
        )
        effective_targets = (
            kwargs.get("evaluation_targets") if "evaluation_targets" in kwargs else task.evaluation_targets
        )
        self._validate_targets_required(effective_type, effective_consistency, effective_targets)
        self._validate_script_targets(effective_targets, kwargs.get("dataset_id") or task.dataset_id)
        self._validate_reference_marks(
            kwargs.get("score_details") if "score_details" in kwargs else task.score_details,
            kwargs.get("evaluation_metrics") if "evaluation_metrics" in kwargs else task.evaluation_metrics,
            kwargs.get("judge_variable_config") if "judge_variable_config" in kwargs else task.judge_variable_config,
            effective_targets,
        )

        # 契约 v3 §五（保守语义）：pre_scripts 与 steps 仅在 payload 显式携带时置 None
        # （targets 为唯一事实源、维度顺序即步骤）；未携带时保留存量值，
        # 避免非前端通道的局部更新误清未迁移任务的 legacy 数据
        if "pre_scripts" in kwargs:
            kwargs["pre_scripts"] = None
        if "steps" in kwargs:
            kwargs["steps"] = None

        for key, value in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, value)

        # W4（P0-C）：显式切换被评测/裁判模型时同步刷新配置快照——任务编辑是
        # 明确的口径变更意图，快照必须跟随，否则「编辑切模型不生效」。
        # 快照已固定同一 model_id 时不重建：表单回传同值不应重置口径锚点
        # （改 ModelConfig 后重跑旧任务口径不变）；存量任务（无快照）首次
        # 显式携带模型字段时按当前配置补建快照（非迁移回填）。
        from app.services.config_snapshot import build_model_config_snapshot

        def _pinned_model_id(snapshot) -> Optional[str]:
            return snapshot.get("model_id") if isinstance(snapshot, dict) else None

        if "model_provider" in kwargs:
            if (task.model_provider or None) != _pinned_model_id(task.model_config):
                task.model_config = build_model_config_snapshot(self.db, task.model_provider)
        if "judge_model" in kwargs:
            if (task.judge_model or None) != _pinned_model_id(task.judge_model_config):
                task.judge_model_config = build_model_config_snapshot(self.db, task.judge_model)

        # W12-1：评分标准口径变更后失效 judge 图缓存。图拓扑由 evaluation_metrics
        # 编译并被 JudgeLangGraphAgent 类级缓存按 task.id 复用（judge_prompt 虽不进
        # 拓扑，但同为评分口径字段，一并失效以覆盖未来缓存扩展）。显式携带即视为
        # 口径变更意图，同值重编译代价可忽略，避免逐字段深比较的误判风险。
        if "evaluation_metrics" in kwargs or "judge_prompt" in kwargs:
            from app.services.judge_langgraph_agent import JudgeLangGraphAgent

            JudgeLangGraphAgent.clear_cache(task.id)

        self.db.commit()
        self.db.refresh(task)
        return task

    # ---------- 待评测数据配置校验（契约 v3 §五） ----------

    def _validate_targets_required(
        self,
        evaluation_type: Optional[str],
        consistency_config: Optional[Dict],
        evaluation_targets: Optional[List[Dict]],
    ) -> None:
        """judge/agent/（一致性评测启用裁判）必须配置待评测数据，否则 ValueError（API 层转 400）"""
        use_judge = bool(
            consistency_config.get("use_judge")
        ) if isinstance(consistency_config, dict) else False
        need_targets = evaluation_type in ("judge", "agent") or (
            evaluation_type == "consistency" and use_judge
        )
        if need_targets and not evaluation_targets:
            raise ValueError(
                "judge/agent 评测（或一致性评测启用裁判）必须配置待评测数据（evaluation_targets），"
                "请先在「待评测数据」区添加评测目标行"
            )

    def _validate_script_targets(
        self,
        evaluation_targets: Optional[List[Dict]],
        dataset_id: Optional[str],
    ) -> None:
        """校验待评测数据中的 script_output 行，失败抛 ValueError（API 层转 400）

        校验项（契约 v3 §五）：
        - source_config 结构合法（script_id 必填、on_fail 枚举、input_mapping 对象）
        - 脚本存在且 is_active（builtin 内置 + pre_judge_scripts 表记录，停用报错）
        - exec_type 合法（builtin/script/claude_agent/deepseek_agent）
        - output_key 为空或在脚本 output_schema 键内（output_schema 空不校验）
        - input_mapping 的字段在数据集中存在（数据集无法确定时跳过该项）
        - hub_file 通道（value_type=hub_file / 配 path_pattern）：path_pattern 形态
          （相对路径/禁 ..）与目标字段必须是数据集 hub_file 类型字段（阶段B，
          传入字段类型视图；无字段元数据时跳过类型校验）
        """
        if not evaluation_targets:
            return
        if not isinstance(evaluation_targets, list):
            raise ValueError("evaluation_targets 必须是数组")

        script_rows = [
            (index, row.get("source_config"))
            for index, row in enumerate(evaluation_targets)
            if isinstance(row, dict) and row.get("source") == "script_output"
        ]
        if not script_rows:
            return

        from app.services.pre_judge_script_service import VALID_EXEC_TYPES, _validate_input_mapping
        from app.models.pre_judge_models import PreJudgeScript

        # 脚本视图：DB 全量记录（含停用，用于区分「不存在/已停用」）；
        # builtin:<name> 前缀为历史引用兼容，按名称映射到 DB 记录
        script_lookup: Dict[str, Dict[str, Any]] = {}
        name_lookup: Dict[str, Dict[str, Any]] = {}
        for row in self.db.query(PreJudgeScript).all():
            entry = {
                "id": row.id,
                "name": row.name,
                "exec_type": row.exec_type,
                "output_schema": row.output_schema,
                "is_active": bool(row.is_active),
            }
            script_lookup[row.id] = entry
            name_lookup[row.name] = entry
        # builtin: 前缀 id 直接指向同名记录（未迁移的存量任务校验兼容）
        for name, entry in name_lookup.items():
            script_lookup.setdefault(f"builtin:{name}", entry)

        errors: List[str] = []
        dataset_fields: Optional[set] = None
        # 阶段B：hub_file 通道目标字段类型视图（无字段元数据时为 None，跳过类型校验）
        dataset_field_types = self._get_dataset_field_types(dataset_id)
        for index, config in script_rows:
            label = f"evaluation_targets[{index}]"
            if not isinstance(config, dict):
                errors.append(f"{label}（script_output）source_config 必须是对象")
                continue
            script_id = config.get("script_id")
            if not script_id:
                errors.append(f"{label}（script_output）缺少 script_id")
                continue
            definition = script_lookup.get(script_id)
            if definition is None:
                errors.append(f"{label}（script_output）引用的脚本不存在: {script_id}")
                continue
            if definition.get("is_active") is False:
                errors.append(f"{label}（script_output）引用的脚本已停用: {script_id}")
            exec_type = definition.get("exec_type")
            if exec_type not in VALID_EXEC_TYPES:
                errors.append(
                    f"{label}（script_output）脚本执行类型非法: {exec_type}，"
                    f"可选值: {', '.join(VALID_EXEC_TYPES)}"
                )
            on_fail = config.get("on_fail") or "continue"
            if on_fail not in ("continue", "abort"):
                errors.append(f"{label}（script_output）on_fail 仅支持 continue/abort，当前: {on_fail}")
            # output_key 校验：为空不校验；脚本 output_schema 为空不校验
            output_key = config.get("output_key")
            output_schema = definition.get("output_schema")
            if output_key and isinstance(output_schema, dict) and output_schema:
                if output_key not in output_schema:
                    errors.append(
                        f"{label}（script_output）output_key {output_key} 不在脚本 {script_id} 的输出键内"
                    )
            mapping = config.get("input_mapping")
            if mapping is not None and not isinstance(mapping, dict):
                errors.append(f"{label}（script_output）input_mapping 必须是对象")
            elif isinstance(mapping, dict) and mapping:
                # 阶段B：hub_file 通道（path_pattern/required/目标字段类型）校验
                errors.extend(_validate_input_mapping(
                    f"{label}（script_output）", mapping, dataset_field_types))
                if dataset_fields is None:
                    dataset_fields = self._get_dataset_field_names(dataset_id)
                if dataset_fields is not None:
                    for field_cfg in mapping.values():
                        field = field_cfg.get("field") if isinstance(field_cfg, dict) else field_cfg
                        if field and field not in dataset_fields:
                            errors.append(
                                f"{label}（script_output）input_mapping 字段 {field} 不在数据集字段中"
                            )

        if errors:
            raise ValueError("；".join(errors))

    def _validate_reference_marks(
        self,
        score_details: Optional[str],
        evaluation_metrics: Optional[List[Dict]],
        judge_variable_config: Optional[Any],
        evaluation_targets: Optional[List[Dict]],
    ) -> None:
        """校验评分标准、维度定义与维度等级描述中的 @ 引用标记，失效引用抛 ValueError（API 层转 400）

        标记出现在全局 score_details、evaluation_metrics[].description（维度定义）
        与 evaluation_metrics[].score_criteria[等级].desc：
        - target 引用须存在于本次 evaluation_targets（按 id 或显示名）
        - var 引用须存在于 judge_variable_config（key/name）
        - 失效时报出标记原文与所在字段
        """
        from app.services.reference_mark_resolver import extract_reference_marks

        # 待评测数据合法标识：行 id 与显示名
        target_identifiers = set()
        for row in (evaluation_targets or []):
            if isinstance(row, dict):
                for key in ("id", "display_name"):
                    value = row.get(key)
                    if value:
                        target_identifiers.add(str(value))
        # 裁判变量合法标识：配置 key 与变量名（dict/list 两种格式）
        var_identifiers = self._collect_judge_var_identifiers(judge_variable_config)

        errors: List[str] = []
        errors.extend(
            self._check_reference_marks(
                extract_reference_marks(score_details), target_identifiers, var_identifiers, "score_details"
            )
        )
        if isinstance(evaluation_metrics, list):
            for metric_index, metric in enumerate(evaluation_metrics):
                if not isinstance(metric, dict):
                    continue
                if isinstance(metric.get("description"), str):
                    field = f"evaluation_metrics[{metric_index}].description"
                    errors.extend(
                        self._check_reference_marks(
                            extract_reference_marks(metric.get("description")),
                            target_identifiers,
                            var_identifiers,
                            field,
                        )
                    )
                score_criteria = metric.get("score_criteria")
                if not isinstance(score_criteria, dict):
                    continue
                for level, level_info in score_criteria.items():
                    if not (isinstance(level_info, dict) and isinstance(level_info.get("desc"), str)):
                        continue
                    field = f"evaluation_metrics[{metric_index}].score_criteria.{level}.desc"
                    errors.extend(
                        self._check_reference_marks(
                            extract_reference_marks(level_info.get("desc")),
                            target_identifiers,
                            var_identifiers,
                            field,
                        )
                    )

        if errors:
            raise ValueError("；".join(errors))

    @staticmethod
    def _check_reference_marks(
        marks: List[Dict[str, str]],
        target_identifiers: set,
        var_identifiers: set,
        field: str,
    ) -> List[str]:
        """逐个标记检查引用是否命中（key 精确或显示名/变量名兜底），未命中报「标记原文 + 所在字段」"""
        errors: List[str] = []
        for mark in marks:
            key = mark.get("key") or ""
            name = mark.get("name") or ""
            if mark.get("kind") == "target":
                if key not in target_identifiers and name not in target_identifiers:
                    errors.append(
                        f"{field} 中的引用标记失效: {mark.get('raw')}"
                        f"（待评测数据中不存在 id/显示名为 {key or name} 的行）"
                    )
            else:
                if key not in var_identifiers and name not in var_identifiers:
                    errors.append(
                        f"{field} 中的引用标记失效: {mark.get('raw')}"
                        f"（裁判变量中不存在 key/名称为 {key or name} 的变量）"
                    )
        return errors

    @staticmethod
    def _collect_judge_var_identifiers(judge_variable_config: Optional[Any]) -> set:
        """收集裁判变量配置的合法标识集合（key 与变量名），支持 dict/list 两种格式

        - dict 格式：{key: {...}} → key 与条目内 name 字段
        - list 格式：[{key/name/variable_name, ...}] → 各字段取值
        """
        identifiers = set()
        if isinstance(judge_variable_config, dict):
            for key, value in judge_variable_config.items():
                if key:
                    identifiers.add(str(key))
                if isinstance(value, dict) and value.get("name"):
                    identifiers.add(str(value["name"]))
        elif isinstance(judge_variable_config, list):
            for item in judge_variable_config:
                if not isinstance(item, dict):
                    continue
                for key in ("key", "variable_name", "name"):
                    if item.get(key):
                        identifiers.add(str(item[key]))
        return identifiers

    def _get_dataset_field_types(self, dataset_id: Optional[str]) -> Optional[Dict[str, str]]:
        """获取数据集字段类型视图 {field_name: field_type}（阶段B：hub_file 通道目标字段校验用）

        仅字段元数据可提供类型（数据推断拿不到）；无 dataset_id / 无字段元数据 /
        读取异常时返回 None（跳过类型校验，与 _get_dataset_field_names 的宽松
        回退语义一致，存量任务配置不受影响）。
        """
        if not dataset_id:
            return None
        try:
            from app.services.dataset_field_service import DatasetFieldService

            field_metas = DatasetFieldService(self.db).get_field_metadata(dataset_id)
            if field_metas:
                return {
                    f["name"]: str(f.get("type") or "string")
                    for f in field_metas if f.get("name")
                }
        except Exception as e:
            logger.warning(f"读取数据集字段元数据失败，跳过 hub_file 字段类型校验: {e}")
        return None

    def _get_dataset_field_names(self, dataset_id: Optional[str]) -> Optional[set]:
        """获取数据集字段名集合；无法确定（无字段元数据且无数据）时返回 None 跳过校验"""
        if not dataset_id:
            return None
        try:
            from app.services.dataset_field_service import DatasetFieldService

            field_metas = DatasetFieldService(self.db).get_field_metadata(dataset_id)
            if field_metas:
                return {f.get("name") for f in field_metas if f.get("name")}
        except Exception as e:
            logger.warning(f"读取数据集字段元数据失败，回退到数据推断: {e}")

        try:
            from app.services.dataset_service import DatasetDataService

            items = DatasetDataService(self.db).get_data_list_v2(dataset_id, skip=0, limit=1)
            for item in items:
                data = item.get("data") if isinstance(item, dict) else getattr(item, "data", None)
                if isinstance(data, dict) and data:
                    return set(data.keys())
        except Exception as e:
            logger.warning(f"从数据项推断字段失败: {e}")
        return None

    def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        task = self.get_task(task_id)
        if not task:
            return False

        task.is_active = False
        self.db.commit()
        return True

    def start_task(self, task_id: str) -> Optional[EvaluationTask]:
        """启动任务（带并发保护），同时创建执行历史记录

        使用原子条件更新（UPDATE ... WHERE status != 'running'）防止并发请求
        重复启动同一任务导致重复入队。返回 None 表示任务不存在或已被其他请求启动。
        """
        task = self.get_task(task_id)
        if not task:
            return None

        # 原子条件更新：仅当任务当前不在 running 状态时才置为 running
        # 并发请求同时通过状态检查时，只有一个请求的 rowcount > 0，其余为 0
        updated = self.db.query(EvaluationTask).filter(
            EvaluationTask.id == task_id,
            EvaluationTask.status != "running",
        ).update(
            {
                "status": "running",
                "execution_count": func.coalesce(EvaluationTask.execution_count, 0) + 1,
            },
            synchronize_session=False,
        )
        if updated == 0:
            self.db.rollback()
            self.add_log(task_id, "warning", "任务已在运行中，忽略重复启动")
            return None
        self.db.commit()

        # 确保会话中所有对象过期，重新读取任务时拿到最新状态/execution_count
        # 不依赖 expire_on_commit 配置，避免读到 identity map 中的脏对象
        self.db.expire_all()
        task = self.get_task(task_id)

        # 创建执行历史记录
        exec_service = TaskExecutionRecordService(self.db)
        exec_service.create_record(task_id, task.execution_count)

        # 创建统计记录（model_config_id：W4 快照统一存 ModelConfig.id 于
        # model_id 键；旧调用 .get("id") 在快照/实时两条路径下均为空，按快照键取值）
        from app.services.stats_service import StatsService
        stats_service = StatsService(self.db)
        stats_service.create_record(
            task_id=task_id,
            evaluation_type=task.evaluation_type,
            model_config_id=(
                task.model_config.get("model_id")
                if isinstance(task.model_config, dict) else None
            ),
            dataset_id=task.dataset_id,
        )
        stats_service.start_record(task_id)

        self.add_log(task_id, "info", f"任务已开始执行（第{task.execution_count}次）")
        self._publish_status_update(task)
        return task

    def stop_task(self, task_id: str) -> Optional[EvaluationTask]:
        """停止任务，同时更新执行历史记录"""
        task = self.get_task(task_id)
        if not task:
            return None

        # 竞态保护：已完成或失败的任务不覆盖
        if task.status in ("completed", "failed"):
            self.add_log(task_id, "warning", f"忽略 stop_task 调用（当前状态: {task.status}）")
            return task

        task.status = "stopped"
        self.db.commit()
        self.db.refresh(task)

        # 同步更新执行历史记录（W3/P0-E：partial_result 改从行级表取本轮已完成行——
        # 旧语义存 task.result，执行期为上一轮结果，本轮停止即全丢）
        exec_service = TaskExecutionRecordService(self.db)
        execution_number = task.execution_count or 1
        exec_service.stop_record(
            task_id,
            execution_number,
            partial_result=self._build_partial_result_from_rows(task, execution_number),
        )

        # 更新统计记录
        from app.services.stats_service import StatsService
        stats_service = StatsService(self.db)
        stats_service.stop_record(task_id)

        self.add_log(task_id, "info", "任务已停止")
        self._publish_status_update(task)
        return task

    def _build_partial_result_from_rows(self, task: EvaluationTask, execution_number: int) -> Optional[Dict[str, Any]]:
        """从行级表聚合本轮已完成行的部分结果（W3 stop_task 专用，P0-E）

        - 行级表只存终态行（write-through），全量即本轮已完成部分；
        - 表无行（本轮尚未评出任何行/表不可用/迁移未执行）时返回 None——
          诚实反映「本轮无部分结果」，不再错误回填上一轮 task.result；
        - 聚合与 complete 同口径（ResultAggregator.aggregate_for_task），
          result JSON 同构，前端无需改动。
        """
        from app.services.evaluation_result_service import load_execution_results

        rows = load_execution_results(self.db, task.id, execution_number)
        if not rows:
            return None
        try:
            from app.services.result_aggregator import ResultAggregator

            return ResultAggregator.aggregate_for_task(task, rows)
        except Exception as e:  # noqa: BLE001 - 部分结果聚合失败不影响停止主流程
            logger.warning(f"[RowPersist] 行级部分结果聚合失败 task={task.id}: {e}")
            return None

    def complete_task(
        self,
        task_id: str,
        result: Dict[str, Any],
        total_cost: Optional[float] = None,
    ) -> Optional[EvaluationTask]:
        """完成任务，同时写入执行历史记录"""
        task = self.get_task(task_id)
        if not task:
            return None

        # 竞态保护：已停止或已完成的任务不覆盖
        if task.status in ("stopped", "completed"):
            self.add_log(task_id, "warning", f"忽略 complete_task 调用（当前状态: {task.status}）")
            return task

        # 确保进度为100%（任务完成时无论是否有数据都设为100%）
        task.status = "completed"
        task.progress = 100
        task.completed_questions = task.total_questions or 0
        task.result = result
        task.total_cost = total_cost
        task.completed_at = beijing_now()

        # 使用重试机制处理可能的连接中断问题
        try:
            self.db.commit()
            self.db.refresh(task)
        except OperationalError as e:
            logger.warning(f"数据库操作异常（complete_task commit）: {e}")
            self.db.rollback()
            # 使用新的数据库会话重试
            self._retry_with_new_session(lambda: self._complete_task_internal(task_id, result, total_cost))
            # 重试路径：更新统计记录
            self._retry_update_stats_complete(task_id)
            return self.get_task(task_id)

        # 同步写入执行历史记录
        exec_service = TaskExecutionRecordService(self.db)
        try:
            exec_service.complete_record(task_id, task.execution_count or 1, result, total_cost)
        except OperationalError as e:
            logger.warning(f"数据库操作异常（complete_record）: {e}")
            # 执行记录失败不影响主流程，使用新会话重试
            from app.db.session import SessionLocal
            new_db = SessionLocal()
            try:
                new_exec_service = TaskExecutionRecordService(new_db)
                new_exec_service.complete_record(task_id, task.execution_count or 1, result, total_cost)
            finally:
                new_db.close()

        # 更新统计记录
        from app.services.stats_service import StatsService
        stats_service = StatsService(self.db)
        try:
            stats_service.complete_record(task_id, success=True)
        except OperationalError as e:
            logger.warning(f"数据库操作异常（complete_record stats）: {e}")
            self._retry_update_stats_complete(task_id)

        self.add_log(task_id, "info", "任务已完成")
        self._publish_status_update(task)
        return task

    def _complete_task_internal(self, task_id: str, result: Dict[str, Any], total_cost: Optional[float]):
        """内部方法：完成任务的实际逻辑（用于重试）"""
        from app.db.session import SessionLocal
        new_db = SessionLocal()
        try:
            service = EvaluationTaskService(new_db)
            task = service.get_task(task_id)
            if task and task.status not in ("stopped", "completed"):
                task.status = "completed"
                if task.total_questions and task.total_questions > 0:
                    task.progress = 100
                    task.completed_questions = task.total_questions
                task.result = result
                task.total_cost = total_cost
                task.completed_at = beijing_now()
                new_db.commit()
        finally:
            new_db.close()

    def _retry_with_new_session(self, func):
        """使用新数据库会话执行操作"""
        from app.db.session import SessionLocal
        new_db = SessionLocal()
        try:
            func()
        finally:
            new_db.close()

    def _retry_update_stats_complete(self, task_id: str):
        """重试更新统计记录（成功）"""
        from app.db.session import SessionLocal
        new_db = SessionLocal()
        try:
            stats_service = StatsService(new_db)
            stats_service.complete_record(task_id, success=True)
        except OperationalError as e:
            logger.warning(f"重试更新统计记录失败（complete）: {e}")
        finally:
            new_db.close()

    def _retry_update_stats_fail(self, task_id: str):
        """重试更新统计记录（失败）"""
        from app.db.session import SessionLocal
        new_db = SessionLocal()
        try:
            stats_service = StatsService(new_db)
            stats_service.fail_record(task_id)
        except OperationalError as e:
            logger.warning(f"重试更新统计记录失败（fail）: {e}")
        finally:
            new_db.close()

    def fail_task(self, task_id: str, error: str) -> Optional[EvaluationTask]:
        """任务失败，同时更新执行历史记录"""
        task = self.get_task(task_id)
        if not task:
            return None

        # 已停止或已完成的任务不覆盖，避免竞态导致 stopped → failed
        if task.status in ("stopped", "completed"):
            self.add_log(task_id, "warning", f"忽略 fail_task 调用（当前状态: {task.status}）: {error}")
            return task

        task.status = "failed"
        task.result = {"error": error}
        task.completed_at = beijing_now()

        # 使用重试机制处理可能的连接中断问题
        try:
            self.db.commit()
            self.db.refresh(task)
        except OperationalError as e:
            logger.warning(f"数据库操作异常（fail_task commit）: {e}")
            self.db.rollback()
            # 使用新的数据库会话重试
            self._retry_with_new_session(lambda: self._fail_task_internal(task_id, error))
            # 重试路径：更新统计记录
            self._retry_update_stats_fail(task_id)
            return self.get_task(task_id)

        # 同步更新执行历史记录
        exec_service = TaskExecutionRecordService(self.db)
        try:
            exec_service.fail_record(task_id, task.execution_count or 1, error)
        except OperationalError as e:
            logger.warning(f"数据库操作异常（fail_record）: {e}")
            from app.db.session import SessionLocal
            new_db = SessionLocal()
            try:
                new_exec_service = TaskExecutionRecordService(new_db)
                new_exec_service.fail_record(task_id, task.execution_count or 1, error)
            finally:
                new_db.close()

        # 更新统计记录
        from app.services.stats_service import StatsService
        stats_service = StatsService(self.db)
        try:
            stats_service.fail_record(task_id)
        except OperationalError as e:
            logger.warning(f"数据库操作异常（fail_record stats）: {e}")
            self._retry_update_stats_fail(task_id)

        self.add_log(task_id, "error", f"任务失败: {error}")
        self._publish_status_update(task)
        return task

    def _fail_task_internal(self, task_id: str, error: str):
        """内部方法：任务失败的实际逻辑（用于重试）"""
        from app.db.session import SessionLocal
        new_db = SessionLocal()
        try:
            service = EvaluationTaskService(new_db)
            task = service.get_task(task_id)
            if task and task.status not in ("stopped", "completed"):
                task.status = "failed"
                task.result = {"error": error}
                task.completed_at = beijing_now()
                new_db.commit()
        finally:
            new_db.close()

    # ---------- W10-4 执行心跳与启动收尸 reaper ----------

    def touch_heartbeat(
        self, task_id: str, execution_number: Optional[int] = None, force: bool = False
    ) -> None:
        """刷新当前执行记录心跳（W10-4：每批评测完成后调用）

        节流：同任务两次心跳写库最小间隔 HEARTBEAT_MIN_INTERVAL_SECONDS——
        收尸 reaper 阈值 15 分钟，分钟级心跳足够；行级完成高频时避免每行
        一次额外 UPDATE/commit。任务执行开始的首跳用 force=True 绕过节流。
        execution_number 缺失（非任务执行链路）时跳过。
        """
        if not execution_number:
            return
        if not force and _heartbeat_throttled(task_id):
            return
        try:
            TaskExecutionRecordService(self.db).touch_heartbeat(task_id, execution_number)
        except Exception as e:  # noqa: BLE001 - 心跳缺失只影响收尸判定，不阻断评测
            logger.warning(f"刷新执行心跳失败（task_id={task_id}）: {e}")

    def fail_stale_running_tasks(self, max_age_seconds: int = 15 * 60) -> List[str]:
        """启动期收尸 reaper（W10-4）：仅将「心跳超时」的 running 任务标 failed

        背景：API lifespan 原实现把所有 running 任务一律标 failed——部署重启
        期间 worker 正在执行的任务会被误标（任务实际还在跑完写回）。

        判定口径：任务 status=running，且其当前执行记录（execution_number 与
        任务 execution_count 对齐）status=running、heartbeat_at 非 NULL 且早于
        cutoff（now - max_age_seconds）。heartbeat_at 为 NULL（排队中尚未开跑、
        或旧版本 worker 写入的记录）一律跳过——保守方向：宁可漏收，不误杀。

        Returns:
            被标 failed 的任务 id 列表
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        # 只对 id 列 distinct（evaluation_tasks 含 JSON 列，整行 DISTINCT 在 PG
        # 无等值算子）；(task_id, execution_number) 业务上唯一，去重仅为防御。
        # 执行序号与执行链路同口径：execution_count or 1（0/NULL 视为 1）。
        stale_ids = [
            row[0] for row in (
                self.db.query(EvaluationTask.id)
                .join(
                    TaskExecutionRecord,
                    (TaskExecutionRecord.task_id == EvaluationTask.id)
                    & (TaskExecutionRecord.execution_number
                       == func.coalesce(func.nullif(EvaluationTask.execution_count, 0), 1)),
                )
                .filter(
                    EvaluationTask.status == "running",
                    TaskExecutionRecord.status == "running",
                    TaskExecutionRecord.heartbeat_at.isnot(None),
                    TaskExecutionRecord.heartbeat_at < cutoff,
                )
                .distinct()
                .all()
            )
        ]
        if not stale_ids:
            return []

        reaped: List[str] = []
        for task_id in stale_ids:
            try:
                self.fail_task(
                    task_id,
                    f"执行心跳超时（>{max_age_seconds // 60} 分钟无进展），"
                    "启动收尸判定任务已死（W10-4 reaper）",
                )
                reaped.append(task_id)
            except Exception as e:  # noqa: BLE001 - 单个任务收尸失败不影响其余
                logger.warning(f"收尸任务 {task_id} 失败: {e}")
        return reaped

    def update_progress(
        self,
        task_id: str,
        completed_questions: int,
        total_questions: int,
    ) -> Optional[EvaluationTask]:
        """更新任务进度"""
        task = self.get_task(task_id)
        if not task:
            return None

        task.completed_questions = completed_questions
        task.total_questions = total_questions
        task.progress = int((completed_questions / total_questions) * 100) if total_questions > 0 else 0

        self.db.commit()
        self.db.refresh(task)
        self._publish_status_update(task)
        return task

    def refresh_dataset_count(self, task_id: str) -> Optional[EvaluationTask]:
        """从数据集刷新题目数"""
        task = self.get_task(task_id)
        if not task:
            return None

        if task.dataset_id:
            from app.services.dataset_service import DatasetDataService
            data_service = DatasetDataService(self.db)
            items = data_service.get_data_list_v2(task.dataset_id, skip=0, limit=10000)
            task.total_questions = len(items)

        self.db.commit()
        self.db.refresh(task)
        return task


class TaskExecutionLogService:
    """任务执行日志服务"""

    def __init__(self, db: Session):
        self.db = db

    def add_log(
        self,
        task_id: str,
        level: str,
        message: str,
        details: Optional[Dict] = None,
    ) -> TaskExecutionLog:
        """添加日志"""
        log_id = f"log_{uuid.uuid4().hex[:12]}"
        log = TaskExecutionLog(
            id=log_id,
            task_id=task_id,
            level=level,
            message=message,
            details=details,
        )

        try:
            self.db.add(log)
            self.db.commit()
        except (OperationalError, IntegrityError) as e:
            logger.warning(f"数据库操作异常，记录日志失败: {e}")
            self.db.rollback()
            # 重试时清空连接池并获取新连接
            self._retry_add_log(log, max_retries=2)

        return log

    def _retry_add_log(self, log: TaskExecutionLog, max_retries: int = 2):
        """重试添加日志（换新会话重试，不再 dispose 全局连接池）

        W10-5：原实现每次重试 engine.dispose()——dispose 会把整个进程的
        连接池清空，其它并发线程正在使用的健康连接一并作废，评测高峰期会
        放大成批量数据库异常。pool_pre_ping 已负责剔除失效连接，重试只需
        换新会话拿到健康连接。
        """
        from app.db.session import SessionLocal

        for attempt in range(max_retries):
            try:
                # 获取新会话重试
                new_db = SessionLocal()
                try:
                    new_db.add(log)
                    new_db.commit()
                    logger.info(f"重试记录日志成功（尝试 {attempt + 1}/{max_retries}）")
                    return
                finally:
                    new_db.close()
            except OperationalError as retry_e:
                logger.warning(f"重试记录日志失败（尝试 {attempt + 1}/{max_retries}）: {retry_e}")
                if attempt == max_retries - 1:
                    logger.error(f"重试日志记录最终失败: {retry_e}")

    def get_logs(
        self,
        task_id: str,
        level: Optional[str] = None,
        limit: int = 100,
    ) -> List[TaskExecutionLog]:
        """获取任务日志"""
        query = self.db.query(TaskExecutionLog).filter(
            TaskExecutionLog.task_id == task_id
        )

        if level:
            query = query.filter(TaskExecutionLog.level == level)

        return query.order_by(TaskExecutionLog.created_at.desc()).limit(limit).all()


class TaskExecutionRecordService:
    """任务执行历史记录服务"""

    def __init__(self, db: Session):
        self.db = db

    def create_record(self, task_id: str, execution_number: int) -> TaskExecutionRecord:
        """创建执行记录（任务开始时调用）"""
        record_id = f"exec_{uuid.uuid4().hex[:12]}"
        record = TaskExecutionRecord(
            id=record_id,
            task_id=task_id,
            execution_number=execution_number,
            status="running",
        )
        # W4：执行开始把创建时双快照写入 config_snapshot（归因锚点「应然」面）
        self._attach_start_config_snapshot(record)
        self.db.add(record)
        self.db.commit()
        return record

    def _get_current_record(self, task_id: str, execution_number: int) -> Optional[TaskExecutionRecord]:
        """获取指定执行次数的记录"""
        return self.db.query(TaskExecutionRecord).filter(
            TaskExecutionRecord.task_id == task_id,
            TaskExecutionRecord.execution_number == execution_number,
        ).first()

    def touch_heartbeat(self, task_id: str, execution_number: int) -> None:
        """刷新当前执行记录心跳（W10-4 收尸 reaper 判定依据：每批评测完成后调用）

        仅更新 status=running 的执行记录（终态记录的心跳无意义）；记录缺失
        静默跳过。失败仅告警回滚——心跳缺失只让启动收尸漏收，不影响评测主流程。
        """
        try:
            record = self._get_current_record(task_id, execution_number)
            if record is None or record.status != "running":
                return
            record.heartbeat_at = datetime.now(timezone.utc)
            self.db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"刷新执行心跳失败（task_id={task_id}, exec={execution_number}）: {e}")
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001
                pass

    # ---------- W4 配置快照写入（失败仅告警，不影响执行记录主流程） ----------

    def _attach_start_config_snapshot(self, record: TaskExecutionRecord):
        """执行开始写入创建时双快照（掩码凭据，严禁明文 api_key/auth_config）"""
        try:
            from app.services.config_snapshot import build_execution_config_snapshot

            task = self.db.query(EvaluationTask).filter(
                EvaluationTask.id == record.task_id
            ).first()
            if task is not None:
                record.config_snapshot = build_execution_config_snapshot(task, self.db)
        except Exception as e:
            logger.warning(f"写入执行配置快照失败（task_id={record.task_id}）: {e}")

    def _attach_used_config_summary(self, record: TaskExecutionRecord):
        """执行结束写入该次实际使用的配置摘要（归因锚点「实然」面）

        与执行链路共用同一解析器（config_snapshot.resolve_*），摘要即可回答
        「这次 X 分当时用的什么模型/温度」；裁判侧按 judge_engine 归因
        （claude_agent 走 CC 全局配置，graph 走快照优先链）。
        """
        try:
            from app.services.config_snapshot import (
                build_execution_config_snapshot,
                build_used_config_summary,
            )

            task = self.db.query(EvaluationTask).filter(
                EvaluationTask.id == record.task_id
            ).first()
            if task is None:
                return
            snapshot = (
                dict(record.config_snapshot)
                if isinstance(record.config_snapshot, dict) and record.config_snapshot
                else build_execution_config_snapshot(task, self.db)
            )
            snapshot["used"] = build_used_config_summary(task, self.db)
            record.config_snapshot = snapshot
        except Exception as e:
            logger.warning(f"写入实际使用配置摘要失败（task_id={record.task_id}）: {e}")

    def complete_record(
        self,
        task_id: str,
        execution_number: int,
        result: Dict[str, Any],
        total_cost: Optional[float] = None,
    ) -> Optional[TaskExecutionRecord]:
        """标记执行记录为已完成"""
        record = self._get_current_record(task_id, execution_number)
        if not record:
            # 如果记录不存在，创建一个新记录（兼容旧任务或create_record失败的情况）
            record = TaskExecutionRecord(
                id=f"exec_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                execution_number=execution_number,
                status="completed",
                result=result,
                total_cost=total_cost,
                started_at=beijing_now(),
                completed_at=beijing_now(),
            )
            self._attach_start_config_snapshot(record)
            self._attach_used_config_summary(record)
            self.db.add(record)
            self.db.commit()
            return record
        record.status = "completed"
        record.result = result
        record.total_cost = total_cost
        record.completed_at = beijing_now()
        # W4：执行结束写入该次实际使用的配置摘要（归因锚点）
        self._attach_used_config_summary(record)
        self.db.commit()
        return record

    def fail_record(self, task_id: str, execution_number: int, error: str) -> Optional[TaskExecutionRecord]:
        """标记执行记录为失败"""
        record = self._get_current_record(task_id, execution_number)
        if not record:
            # 如果记录不存在，创建一个新记录（兼容旧任务或create_record失败的情况）
            record = TaskExecutionRecord(
                id=f"exec_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                execution_number=execution_number,
                status="failed",
                error_message=error,
                result={"error": error},
                started_at=beijing_now(),
                completed_at=beijing_now(),
            )
            self._attach_start_config_snapshot(record)
            self._attach_used_config_summary(record)
            self.db.add(record)
            self.db.commit()
            return record
        record.status = "failed"
        record.error_message = error
        record.result = {"error": error}
        record.completed_at = beijing_now()
        # W4：执行结束写入该次实际使用的配置摘要（归因锚点）
        self._attach_used_config_summary(record)
        self.db.commit()
        return record

    def stop_record(
        self,
        task_id: str,
        execution_number: int,
        partial_result: Optional[Dict] = None,
    ) -> Optional[TaskExecutionRecord]:
        """标记执行记录为已停止，保留停止时的部分结果"""
        record = self._get_current_record(task_id, execution_number)
        if not record:
            # 如果记录不存在，创建一个新记录（兼容旧任务或create_record失败的情况）
            record = TaskExecutionRecord(
                id=f"exec_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                execution_number=execution_number,
                status="stopped",
                result=partial_result,
                started_at=beijing_now(),
                completed_at=beijing_now(),
            )
            self._attach_start_config_snapshot(record)
            self._attach_used_config_summary(record)
            self.db.add(record)
            self.db.commit()
            return record
        record.status = "stopped"
        record.result = partial_result
        record.completed_at = beijing_now()
        # W4：执行结束写入该次实际使用的配置摘要（归因锚点）
        self._attach_used_config_summary(record)
        self.db.commit()
        return record

    def get_records(self, task_id: str, limit: int = 50) -> List[TaskExecutionRecord]:
        """获取任务的所有执行历史记录，按执行次数倒序"""
        return self.db.query(TaskExecutionRecord).filter(
            TaskExecutionRecord.task_id == task_id,
        ).order_by(TaskExecutionRecord.execution_number.desc()).limit(limit).all()

    def get_record_by_number(self, task_id: str, execution_number: int) -> Optional[TaskExecutionRecord]:
        """按执行序号获取单条记录"""
        return self._get_current_record(task_id, execution_number)

    def delete_execution_record(self, task_id: str, execution_number: int) -> bool:
        """删除单条执行历史记录，并联动删除该次执行的全部行级结果（W3-5）

        行级表随 TaskExecutionRecord 生命周期：执行记录删除时其行级结果一并
        清理（批量归档任务为 M2 B-1，届时复用本联动语义）。
        行级表不可用（迁移未执行）时仅告警，不阻断执行记录删除。
        delete_task 软删不动行级表（与现状一致，本方法不参与）。

        Returns: 执行记录是否存在（False = 无记录，行级孤儿行仍被清理）
        """
        record = self._get_current_record(task_id, execution_number)
        if record is not None:
            self.db.delete(record)
        try:
            from app.services.evaluation_result_service import delete_execution_rows

            delete_execution_rows(self.db, task_id, execution_number)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"联动删除行级结果失败（继续删除执行记录）: {e}")
            self.db.commit()
        return record is not None
