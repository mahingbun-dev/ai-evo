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
from pathlib import Path
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


from app.services.plugin_eval_market_runner import (
    MAX_CONCURRENCY as _MAX_CONCURRENCY,
    TRACE_MODES as _TRACE_JUDGEMENTS,
)


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
        plugin_config: Optional[Dict] = None,
        safety_config: Optional[Dict] = None,
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
        model_output_source: Optional[str] = None,
        model_output_field: Optional[str] = None,
        gate_config: Optional[Dict] = None,
        verifiers: Optional[List[Dict]] = None,
        trajectory_config: Optional[Dict] = None,
        trials: Optional[int] = None,
        judge_guard_disabled: Optional[bool] = False,
        judge_canary: Optional[bool] = False,
    ) -> EvaluationTask:
        """创建评测任务"""
        # 验证评测类型
        # W12-2：latency 白名单激活（执行/聚合/导出链早已就绪，此前仅因白名单缺席被拒）
        # M3 QE3-1：safety 白名单放行（双通道红队引擎就绪，见 app/services/redteam/）
        valid_evaluation_types = ["judge", "consistency", "success_rate", "rag", "binary_classification", "agent", "latency", "plugin", "safety"]
        if evaluation_type not in valid_evaluation_types:
            raise ValueError(f"不支持的评测类型: {evaluation_type}，可选值: {valid_evaluation_types}")

        # 插件评测配置校验：深度白名单 + 评测对象来源（数据集 / 任务级路径或内联 SKILL.md 至少其一）
        if evaluation_type == "plugin":
            self._validate_plugin_config(plugin_config, dataset_id)
            if str((plugin_config or {}).get("source") or "") == "trace":
                # trace 口径的评测对象来自数据集行，不需要任务级 plugin_path/skill_content
                pass
            elif not dataset_id and not self._plugin_config_has_source(plugin_config):
                raise ValueError(
                    "插件评测需要指定评测对象：请选择包含 plugin_path/skill_content 字段的数据集，"
                    "或在插件评测配置中填写插件路径 / 内联 SKILL.md 内容"
                )

        # 安全评测配置校验（M3 QE3-1）：攻击面白名单/样本来源/样本库存在性/
        # 变异数/被攻击模型必填；通过后补 pack_version 快照（重跑口径可复现）
        if evaluation_type == "safety":
            safety_config = self._validate_safety_config(
                safety_config, dataset_id=dataset_id, model_provider=model_provider
            )

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
        elif evaluation_type == "safety" and str((safety_config or {}).get("sample_source")) == "pack":
            # 安全评测内置样本库直跑：题目总数 = 选中类目的样本数（变异体运行期生成不计入创建期基数）
            from app.services.redteam import load_pack

            pack = load_pack(
                (safety_config or {}).get("pack") or "redteam_pack_v1",
                categories=(safety_config or {}).get("categories"),
            )
            total_questions = len(pack["samples"])

        # 待评测数据配置校验（契约 v3 §五）：targets 必填、script_output 行、@ 引用标记；
        # pre_scripts/steps 旧字段仅兼容接收，写路径一律置 None（targets 为唯一事实源，维度顺序即步骤）
        self._validate_targets_required(evaluation_type, consistency_config, evaluation_targets)
        self._validate_script_targets(evaluation_targets, dataset_id)
        # 终态校验器配置校验（M2 QE2-1）：类型白名单/script 存在性/断言结构
        self._validate_verifiers(verifiers)
        # 轨迹断言配置校验（M2 QE2-2）：模式白名单/条目结构 + 仅 agent 类型
        self._validate_trajectory_config(evaluation_type, trajectory_config)
        # 行级多 trial 校验（M2 QE2-3）：仅 judge 类型、2-5 整数
        self._validate_trials(evaluation_type, trials)
        # 裁判治理校验（M2 QE2-4）：消毒关闭声明与金丝雀仅 judge/agent 类型
        self._validate_judge_governance(evaluation_type, judge_guard_disabled, judge_canary)
        self._validate_reference_marks(
            score_details,
            evaluation_metrics,
            judge_variable_config,
            evaluation_targets,
        )
        # 模型输出来源校验（judge 可跳过被评测模型调用）：来源白名单 +
        # dataset_field 字段存在性 + model_call 必须有被评测模型
        # 归一化后再校验与入库，避免 " dataset_field " 之类带空白的值校验通过、
        # 执行器却按字面量走 model_call 分支
        model_output_source = self._normalize_model_output_source(model_output_source)
        self._validate_model_output_config(
            evaluation_type=evaluation_type,
            dataset_id=dataset_id,
            model_provider=model_provider,
            model_output_source=model_output_source,
            model_output_field=model_output_field,
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
            model_output_source=model_output_source,
            model_output_field=model_output_field,
            dataset_id=dataset_id,
            judge_model=judge_model,
            judge_engine=judge_engine or "claude_agent",
            judge_prompt=judge_prompt,
            consistency_config=consistency_config,
            success_rate_config=success_rate_config,
            rag_config=rag_config,
            latency_config=latency_config,
            binary_metrics_config=binary_metrics_config,
            plugin_config=plugin_config,
            # M3 QE3-1：安全评测配置（校验通过后含 pack_version 快照）
            safety_config=safety_config if evaluation_type == "safety" else None,
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
            # M1 QE1-6：门禁配置（{mode, thresholds}；None=未启用）
            gate_config=gate_config,
            # M2 QE2-1：终态校验器配置（[{type, name, on_fail, config}]；None=未配置）
            verifiers=verifiers,
            # M2 QE2-2：轨迹断言配置（{mode, expected, forbidden}；None=未配置）
            trajectory_config=trajectory_config,
            # M2 QE2-3：行级多 trial 次数（judge 类型 2-5；None/1=不开启）
            trials=trials if trials and trials > 1 else None,
            # M2 QE2-4：裁判输入消毒显式关闭（默认 False=开启消毒，审计可见）
            judge_guard_disabled=bool(judge_guard_disabled),
            # M2 QE2-4：裁判金丝雀自检（默认关闭）
            judge_canary=bool(judge_canary),
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
        "plugin_config", "safety_config",
    )

    def get_tasks(
        self,
        status: Optional[str] = None,
        evaluation_type: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        team_code: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[EvaluationTask]:
        """获取评测任务列表"""
        from sqlalchemy.orm import defer

        query = self.db.query(EvaluationTask).filter(
            EvaluationTask.is_active == True
        ).options(
            *[defer(getattr(EvaluationTask, col)) for col in self._TASK_LIST_DEFERRED_COLUMNS]
        )

        if name:
            query = query.filter(EvaluationTask.name.ilike(f"%{name.strip()}%"))
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
        # 终态校验器校验（M2 QE2-1）：字段缺省时取任务既有值合并成最终态校验
        effective_verifiers = (
            kwargs.get("verifiers") if "verifiers" in kwargs else getattr(task, "verifiers", None)
        )
        self._validate_verifiers(effective_verifiers)
        # 轨迹断言校验（M2 QE2-2）：字段缺省时取任务既有值合并成最终态校验
        effective_trajectory = (
            kwargs.get("trajectory_config") if "trajectory_config" in kwargs
            else getattr(task, "trajectory_config", None)
        )
        self._validate_trajectory_config(effective_type, effective_trajectory)
        # 行级多 trial 校验（M2 QE2-3）：字段缺省时取任务既有值合并成最终态校验
        effective_trials = (
            kwargs["trials"] if "trials" in kwargs else getattr(task, "trials", None)
        )
        self._validate_trials(effective_type, effective_trials)
        # 裁判治理校验（M2 QE2-4）：字段缺省时取任务既有值合并成最终态校验
        effective_guard_disabled = (
            kwargs["judge_guard_disabled"] if "judge_guard_disabled" in kwargs
            else bool(getattr(task, "judge_guard_disabled", False))
        )
        effective_canary = (
            kwargs["judge_canary"] if "judge_canary" in kwargs
            else bool(getattr(task, "judge_canary", False))
        )
        self._validate_judge_governance(effective_type, effective_guard_disabled, effective_canary)
        self._validate_reference_marks(
            kwargs.get("score_details") if "score_details" in kwargs else task.score_details,
            kwargs.get("evaluation_metrics") if "evaluation_metrics" in kwargs else task.evaluation_metrics,
            kwargs.get("judge_variable_config") if "judge_variable_config" in kwargs else task.judge_variable_config,
            effective_targets,
        )
        # 插件评测配置：创建期一直在校验，编辑期此前漏掉——改来源（含新增的 trace 口径）
        # 能绕过深度守卫与数据集要求，这里按合并后的最终态补校验
        if effective_type == "plugin":
            effective_plugin_config = (
                kwargs.get("plugin_config") if "plugin_config" in kwargs else task.plugin_config
            )
            self._validate_plugin_config(
                effective_plugin_config, kwargs.get("dataset_id") or task.dataset_id
            )

        # 模型输出来源校验：kwargs 与任务既有值合并成最终态（避免只改来源/字段
        # 或只改数据集的局部更新绕过校验）；存量任务字段为空按 model_call 处理
        effective_dataset_id = (
            kwargs["dataset_id"] if "dataset_id" in kwargs else getattr(task, "dataset_id", None)
        )
        effective_provider = (
            kwargs["model_provider"] if "model_provider" in kwargs else getattr(task, "model_provider", None)
        )
        # 来源值统一归一化（strip/lower）后回写 kwargs 参与最终态校验与落库
        if "model_output_source" in kwargs:
            kwargs["model_output_source"] = self._normalize_model_output_source(
                kwargs["model_output_source"]
            )
        effective_output_source = (
            kwargs["model_output_source"] if "model_output_source" in kwargs
            else getattr(task, "model_output_source", None)
        )
        effective_output_source = self._normalize_model_output_source(effective_output_source)
        effective_output_field = (
            kwargs["model_output_field"] if "model_output_field" in kwargs
            else getattr(task, "model_output_field", None)
        )
        self._validate_model_output_config(
            evaluation_type=effective_type,
            dataset_id=effective_dataset_id,
            model_provider=effective_provider,
            model_output_source=effective_output_source,
            model_output_field=effective_output_field,
        )

        # 安全评测配置校验（M3 QE3-1）：字段缺省时取任务既有值合并成最终态
        # （避免只改类目/样本来源/数据集的局部更新绕过校验）；校验通过后
        # 的归一化结果（含 pack_version 快照）回写 kwargs 参与落库
        if effective_type == "safety":
            effective_safety = (
                kwargs.get("safety_config") if "safety_config" in kwargs
                else getattr(task, "safety_config", None)
            )
            kwargs["safety_config"] = self._validate_safety_config(
                effective_safety,
                dataset_id=effective_dataset_id,
                model_provider=effective_provider,
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

    def _validate_trajectory_config(
        self,
        evaluation_type: Optional[str],
        trajectory_config: Optional[Dict],
    ) -> None:
        """轨迹断言配置校验（M2 QE2-2），失败抛 ValueError（API 层转 400）

        形态白名单经 trajectory_matcher.validate_trajectory_shape（模式/条目
        结构/args_contains/min_calls）；轨迹断言仅对 agent 类型任务有意义
        （会话 session_messages 为轨迹来源），非 agent 类型配置非空即拒绝
        （防静默不生效的配置）。
        """
        if not trajectory_config:
            return
        if evaluation_type != "agent":
            raise ValueError(
                f"轨迹断言仅支持 agent 类型任务（当前 {evaluation_type!r}）——"
                "轨迹来源为会话数据的工具调用序列"
            )
        from app.services.trajectory_matcher import validate_trajectory_shape

        validate_trajectory_shape(trajectory_config)

    def _validate_verifiers(self, verifiers: Optional[List[Dict]]) -> None:
        """终态校验器配置校验（M2 QE2-1），失败抛 ValueError（API 层转 400）

        形态白名单经 verifier_runner.validate_verifier_shape（类型/on_fail/
        断言结构/http URL 形态）；script 类型追加脚本存在性 + active 校验
        （与 _validate_script_targets 同口径：停用/不存在均报错）。
        """
        if not verifiers:
            return
        from app.services.verifier_runner import validate_verifier_shape

        validate_verifier_shape(verifiers)

        script_ids = [
            str((v.get("config") or {}).get("script_id") or "").strip()
            for v in verifiers
            if isinstance(v, dict) and v.get("type") == "script"
        ]
        if not script_ids:
            return
        from app.models.pre_judge_models import PreJudgeScript

        rows = self.db.query(PreJudgeScript).filter(PreJudgeScript.id.in_(script_ids)).all()
        by_id = {row.id: row for row in rows}
        # builtin:<name> 前缀为历史引用兼容（按名称映射到 DB 记录）
        names = [sid[len("builtin:"):] for sid in script_ids if sid.startswith("builtin:")]
        if names:
            for row in self.db.query(PreJudgeScript).filter(PreJudgeScript.name.in_(names)).all():
                by_id.setdefault(f"builtin:{row.name}", row)
        for script_id in script_ids:
            row = by_id.get(script_id)
            if row is None:
                raise ValueError(f"校验器引用的脚本不存在: {script_id}")
            if not row.is_active:
                raise ValueError(f"校验器引用的脚本已停用: {script_id}（{row.name}）")

    def _validate_trials(
        self,
        evaluation_type: Optional[str],
        trials: Optional[int],
    ) -> None:
        """行级多 trial 配置校验（M2 QE2-3），失败抛 ValueError（API 层转 400）

        仅 judge 类型支持（success_rate/consistency/latency 的多次重复走
        各自 repeat_count 通道，两套 k 并存会互相打架）；取值须为 1-5 的
        整数（1=不开启）。运行时另有 normalize_trials 宽容归一化兜底
        （手写通道的存量任务不因脏值中断执行）。
        """
        if trials is None:
            return
        if isinstance(trials, bool) or not isinstance(trials, int):
            raise ValueError(f"trials 必须为整数（1-5），当前 {trials!r}")
        if not 1 <= trials <= 5:
            raise ValueError(f"trials 须在 1-5 内（2-5 开启行级多 trial），当前 {trials}")
        if trials > 1 and evaluation_type != "judge":
            raise ValueError(
                f"行级多 trial 仅支持 judge 类型任务（当前 {evaluation_type!r}）——"
                "success_rate/consistency/latency 使用各自配置的重复次数"
            )

    def _validate_judge_governance(
        self,
        evaluation_type: Optional[str],
        judge_guard_disabled: Optional[bool],
        judge_canary: Optional[bool],
    ) -> None:
        """裁判治理配置校验（M2 QE2-4），失败抛 ValueError（API 层转 400）

        输入消毒关闭声明与金丝雀自检都是裁判侧治理项，仅对裁判参与计分的
        judge/agent 类型开放（success_rate/latency 等无裁判类型无意义且不
        消费该配置，开启即拒绝——防静默不生效）。消毒默认开启，关闭属
        显式降级动作，随 judge_config_snapshot 落执行记录（审计可见）。
        """
        if not judge_guard_disabled and not judge_canary:
            return
        if evaluation_type not in ("judge", "agent"):
            raise ValueError(
                f"裁判治理配置（输入消毒/金丝雀自检）仅支持 judge/agent 类型任务"
                f"（当前 {evaluation_type!r}）——其余类型无裁判参与计分"
            )

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

    # ---------- 插件评测配置校验 ----------

    _PLUGIN_VALID_DEPTHS = ("quick", "standard")
    # 评测对象来源：trace=数据集行里的会话 trace（历史/真实数据评测，被测插件从线程识别）
    _PLUGIN_VALID_SOURCES = ("manual", "dataset", "marketplace", "trace")

    def _validate_plugin_config(self, plugin_config: Optional[Dict], dataset_id: Optional[str] = None) -> None:
        """插件评测 plugin_config 校验：深度白名单 + 来源字段类型，失败抛 ValueError（API 层转 400）"""
        if plugin_config is None:
            return
        if not isinstance(plugin_config, dict):
            raise ValueError("plugin_config 必须是对象")

        depth = plugin_config.get("depth", "quick")
        if depth not in self._PLUGIN_VALID_DEPTHS:
            raise ValueError(
                f"不支持的插件评测深度: {depth}，可选值: {', '.join(self._PLUGIN_VALID_DEPTHS)}"
            )

        for key in ("plugin_path", "skill_content", "market_plugin"):
            value = plugin_config.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"plugin_config.{key} 必须是字符串")

        source = plugin_config.get("source")
        if source is not None and source not in self._PLUGIN_VALID_SOURCES:
            raise ValueError(f"不支持的插件评测来源: {source}")

        market_id = plugin_config.get("market_id")
        if market_id is not None:
            if isinstance(market_id, bool) or not isinstance(market_id, int):
                raise ValueError("plugin_config.market_id 必须是整数")
            if source == "marketplace" and not str(plugin_config.get("market_plugin") or "").strip():
                raise ValueError("marketplace 来源必须指定 market_plugin（市场内插件路径）")

        if source == "trace":
            # trace 口径的深度由服务端固定（合并口径：模型判定=standard 静态 + LLM 评审，
            # 离线判定=quick 仅静态），与「综合分口径的深度」无关，
            # 故深度守卫（插件级/ skill 级限制）在此不适用，也不接受任务级 depth 覆盖
            judgement = plugin_config.get("trace_judgement")
            if judgement is not None and judgement not in _TRACE_JUDGEMENTS:
                raise ValueError(
                    f"不支持的 trace 判定方式: {judgement}，可选值: {', '.join(_TRACE_JUDGEMENTS)}"
                )
            concurrency = plugin_config.get("trace_concurrency")
            if concurrency is not None:
                if isinstance(concurrency, bool) or not isinstance(concurrency, int):
                    raise ValueError("plugin_config.trace_concurrency 必须是整数")
                if not 1 <= concurrency <= _MAX_CONCURRENCY:
                    raise ValueError(
                        f"plugin_config.trace_concurrency 必须在 1~{_MAX_CONCURRENCY} 之间"
                        f"（plugin-eval 的模型调用并发上限为 {_MAX_CONCURRENCY}）"
                    )
            self._validate_plugin_trace_config(plugin_config, dataset_id)
            return

        self._validate_plugin_depth_target(plugin_config, depth)

    def _validate_plugin_trace_config(self, plugin_config: Dict, dataset_id: Optional[str]) -> None:
        """trace 来源校验：必须选数据集，且数据集要有 anc-hub 文件通道字段

        执行包目录来自行内 hub_file 字段（导入时物化、评测期镜像到任务目录）；没有该
        字段的数据集拿不到原始会话文件，无法做历史评测，故创建期直接拒绝。
        """
        from app.services.hub_file_service import get_hub_file_fields

        if not dataset_id:
            raise ValueError(
                "trace 来源需要选择数据集：数据集每行提供一个会话 trace"
                "（请选择由「从 anc-hub 导入」建的会话/执行包数据集）"
            )
        if not get_hub_file_fields(self.db, str(dataset_id)):
            raise ValueError(
                "所选数据集不含 anc-hub 文件字段（hub_file），行里拿不到原始会话文件；"
                "请用「从 anc-hub 导入」创建数据集（会话数据或执行包数据）"
            )

    def _validate_plugin_depth_target(self, plugin_config: Dict, depth: str) -> None:
        """深度守卫：插件级目标只支持 quick（standard/蒙特卡洛仅 skill 级）

        创建/更新任务时能判定目标类型就前置拒绝（报 400），判不出来（数据集行、市场
        尚未同步）留给执行期按 quick 降级——执行期在行的 warnings 里写明原因。
        """
        if depth == "quick":
            return

        from app.services.plugin_eval.parser import detect_target

        target_type = ""
        market_id = plugin_config.get("market_id")
        market_plugin = str(plugin_config.get("market_plugin") or "").strip()
        if market_id is not None and market_plugin:
            try:
                from app.services.plugin_marketplace_service import list_marketplace_plugins

                items = list_marketplace_plugins(self.db, int(market_id))
            except Exception as e:  # 市场未同步/目录缺失：留给执行期判定
                logger.warning(f"[插件评测] 深度守卫跳过市场目标类型判定: {e}")
                return
            matched = next((i for i in items if i["source"] == market_plugin), None)
            target_type = (matched or {}).get("target_type", "")
        elif str(plugin_config.get("plugin_path") or "").strip():
            path = Path(str(plugin_config["plugin_path"]).strip()).expanduser()
            if path.is_dir():
                target_type = detect_target(path)

        if target_type == "plugin":
            raise ValueError(
                "插件级评测对象只支持 quick 深度（仅静态层）；"
                "standard（及蒙特卡洛）只支持 skill 级对象，请在市场中改选该插件下的 skill"
            )

    @staticmethod
    def _plugin_config_has_source(plugin_config: Optional[Dict]) -> bool:
        """plugin_config 是否带有任务级评测对象来源（插件路径/内联 SKILL.md/市场插件）"""
        if not isinstance(plugin_config, dict):
            return False
        return bool(
            (plugin_config.get("plugin_path") or "").strip()
            or (plugin_config.get("skill_content") or "").strip()
            or (
                plugin_config.get("market_id") is not None
                and str(plugin_config.get("market_plugin") or "").strip()
            )
        )

    # ---------- 安全评测配置校验（M3 QE3-1） ----------

    #: 四类攻击面白名单（M3 首批）
    _SAFETY_CATEGORIES = ("prompt_injection", "jailbreak", "sensitive_content", "factual_assertion")
    #: 样本来源：pack=内置样本库直跑（无数据集）/ dataset=数据集行作为攻击样本
    _SAFETY_SAMPLE_SOURCES = ("pack", "dataset")

    def _validate_safety_config(
        self,
        safety_config: Optional[Dict],
        dataset_id: Optional[str] = None,
        model_provider: Optional[str] = None,
    ) -> Dict:
        """safety 类型配置校验并归一化（失败抛 ValueError→API 400）

        - 攻击面类目：非空子集（缺省全四类）；
        - 样本来源：pack（内置样本库直跑，要求无数据集且样本库存在）/
          dataset（数据集行作攻击样本，要求数据集存在）；
        - 变异增强：count 收敛到 1-3；
        - 安全评测攻击被测模型：必须选择 model_provider；
        - 通过后补 pack_version 快照（库升级后老任务仍按创建时版本口径解释）。
        """
        if safety_config is None:
            safety_config = {}
        if not isinstance(safety_config, dict):
            raise ValueError("safety_config 必须是对象")

        # 攻击面类目
        categories = safety_config.get("categories")
        if categories is None:
            categories = list(self._SAFETY_CATEGORIES)
        if not isinstance(categories, list) or not categories:
            raise ValueError("安全评测至少选择一个攻击面类目")
        unknown = [c for c in categories if c not in self._SAFETY_CATEGORIES]
        if unknown:
            raise ValueError(
                f"不支持的攻击面类目: {unknown}，可选值: {', '.join(self._SAFETY_CATEGORIES)}"
            )
        safety_config = dict(safety_config)
        safety_config["categories"] = categories

        # 被攻击模型（红队攻击目标）
        if not model_provider:
            raise ValueError("安全评测需要选择被评测模型（攻击目标，model_provider）")

        # 样本来源
        sample_source = str(safety_config.get("sample_source") or ("dataset" if dataset_id else "pack")).strip()
        if sample_source not in self._SAFETY_SAMPLE_SOURCES:
            raise ValueError(
                f"不支持的安全评测样本来源: {sample_source}，可选值: {', '.join(self._SAFETY_SAMPLE_SOURCES)}"
            )
        if sample_source == "dataset" and not dataset_id:
            raise ValueError("样本来源为「数据集」时必须先选择数据集（可由内置样本库一键导入后编辑）")
        if sample_source == "pack" and dataset_id:
            raise ValueError("样本来源为「内置样本库」时不需要选择数据集；要编辑样本请改用数据集来源")
        safety_config["sample_source"] = sample_source

        # 内置样本库存在性 + 版本快照
        if sample_source == "pack":
            from app.services.redteam import load_pack
            from app.services.redteam.pack import get_pack_version

            pack_name = str(safety_config.get("pack") or "redteam_pack_v1")
            try:
                load_pack(pack_name, categories=categories)
                safety_config["pack"] = pack_name
                safety_config["pack_version"] = get_pack_version(pack_name)
            except ValueError as e:
                raise ValueError(f"内置样本库不可用: {e}")

        # 变异增强
        mutation = safety_config.get("mutation")
        if mutation is None:
            mutation = {"enabled": False, "count": 1}
        if not isinstance(mutation, dict):
            raise ValueError("safety_config.mutation 必须是对象")
        mutation = dict(mutation)
        if not isinstance(mutation.get("enabled"), bool):
            mutation["enabled"] = bool(mutation.get("enabled", False))
        try:
            count = int(mutation.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        mutation["count"] = max(1, min(count, 3))
        safety_config["mutation"] = mutation

        # 双通道开关结构归一化（未配置=双通道全开）
        channels = safety_config.get("channels")
        if channels is None:
            channels = {}
        if not isinstance(channels, dict):
            raise ValueError("safety_config.channels 必须是对象")
        channels = dict(channels)
        if not isinstance(channels.get("deterministic"), dict):
            channels["deterministic"] = {}
        if not isinstance(channels.get("judge_rubric"), dict):
            channels["judge_rubric"] = {}
        safety_config["channels"] = channels

        # M3 QE3-2 deepeval 收敛：bias/toxicity 等遗留指标并入 safety 类型
        # metric 开关（可选增强通道，超管直调端点已标废弃）
        metrics = safety_config.get("metrics")
        if metrics is None:
            metrics = []
        if not isinstance(metrics, list):
            raise ValueError("safety_config.metrics 必须是数组")
        from app.services.safety_evaluator import _SAFETY_METRICS as _DEEPEVAL_SAFETY_METRICS

        unknown_metrics = [m for m in metrics if m not in _DEEPEVAL_SAFETY_METRICS]
        if unknown_metrics:
            raise ValueError(
                f"不支持的安全评测指标: {unknown_metrics}，可选值: {list(_DEEPEVAL_SAFETY_METRICS)}"
            )
        safety_config["metrics"] = metrics

        return safety_config

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

    # ---------- 模型输出来源校验 ----------

    # 允许的模型输出来源：model_call-调用被评测模型（默认，存量行为）/
    # dataset_field-直接取数据集字段 / none-不使用模型输出
    _MODEL_OUTPUT_SOURCES = ("model_call", "dataset_field", "none")
    # 数据集字段元数据中的平台辅助列（is_media 标记 / hub_file 类型标记），
    # 不是用户数据字段，不能作为模型输出来源
    _DATASET_AUX_FIELD_SUFFIXES = ("_is_media", "_hub_file")

    @staticmethod
    def _normalize_model_output_source(value: Optional[str]) -> Optional[str]:
        """归一化模型输出来源：空值按 model_call，字符串 strip+lower

        校验与执行器分支必须用同一个口径，避免 " dataset_field " 校验通过、
        执行期却按 model_call 去调模型（无端点时整行失败）。
        """
        if value is None:
            return "model_call"
        if not isinstance(value, str):
            # API 层 Pydantic 已把类型错误挡成 422；服务层直调时保持 400 语义
            raise ValueError(
                f"不支持的模型输出来源: {value!r}，可选值: "
                f"{', '.join(EvaluationTaskService._MODEL_OUTPUT_SOURCES)}"
            )
        normalized = value.strip().lower()
        return normalized or "model_call"

    def _get_dataset_data_field_names(self, dataset_id: str) -> set:
        """枚举数据集的真实数据字段名（字段元数据口径）

        - 优先 dataset_field_metadata（数据推断会把 is_media 等辅助键算进来）；
        - 无元数据的存量数据集回退到首条数据（V2 data 键 + V1 question/ref_answer），
          与任务表单字段接口的回退口径保持一致；
        - 排除 *_is_media / *_hub_file 平台辅助列（hub 辅助列/媒体标记列）。
        """
        from app.services.dataset_field_service import DatasetFieldService

        field_metas = DatasetFieldService(self.db).get_field_metadata(dataset_id) or []
        names = set()
        for meta in field_metas:
            name = meta.get("name") if isinstance(meta, dict) else None
            if not name:
                continue
            if name.endswith(self._DATASET_AUX_FIELD_SUFFIXES):
                continue
            names.add(name)

        if not names:
            # 存量数据集无字段元数据：按任务表单字段接口的回退口径枚举，
            # 避免 UI 可选、创建却被拒
            from app.services.dataset_service import DatasetDataService

            data_service = DatasetDataService(self.db)
            try:
                items = data_service.get_data_list_v2(dataset_id, skip=0, limit=1)
                if not items:
                    items = data_service.get_data_list(dataset_id, skip=0, limit=1)
                if items:
                    first = items[0]
                    data = first.get("data") if isinstance(first, dict) else getattr(first, "data", None)
                    if isinstance(data, dict):
                        names.update(str(k) for k in data.keys())
                    for legacy in ("question", "ref_answer"):
                        value = first.get(legacy) if isinstance(first, dict) else getattr(first, legacy, None)
                        if value:
                            names.add(legacy)
            except Exception as e:
                logger.warning(f"枚举数据集 {dataset_id} 数据字段失败: {e}")
            names = {
                n for n in names
                if n and not n.endswith(self._DATASET_AUX_FIELD_SUFFIXES)
            }
        return names

    def _validate_model_output_config(
        self,
        evaluation_type: Optional[str],
        dataset_id: Optional[str],
        model_provider: Optional[str],
        model_output_source: Optional[str],
        model_output_field: Optional[str],
    ) -> None:
        """校验模型输出来源配置（create/update 共用，失败抛 ValueError→API 400）

        - 来源白名单；
        - dataset_field：必须有 dataset_id 与字段名，且字段存在于字段元数据；
        - judge + model_call：必须有被评测模型（model_provider），否则执行期
          必然报「未配置端点」，创建/更新时直接拒绝并给出可选出路。
        """
        source = (model_output_source or "model_call").strip()
        if source not in self._MODEL_OUTPUT_SOURCES:
            raise ValueError(
                f"不支持的模型输出来源: {model_output_source}，可选值: "
                f"{', '.join(self._MODEL_OUTPUT_SOURCES)}"
            )

        if source == "dataset_field":
            if not dataset_id:
                raise ValueError(
                    "模型输出来源选择「数据集字段」时必须先选择数据集（dataset_id）"
                )
            field_name = (model_output_field or "").strip()
            if not field_name:
                raise ValueError(
                    "模型输出来源选择「数据集字段」时必须指定字段名（model_output_field）"
                )
            field_names = self._get_dataset_data_field_names(dataset_id)
            if field_name not in field_names:
                available = "、".join(sorted(field_names)) or "无"
                raise ValueError(
                    f"模型输出字段 {field_name!r} 不存在于数据集 {dataset_id} 的字段元数据中，"
                    f"可选字段: {available}"
                )

        if evaluation_type == "judge" and source == "model_call" and not model_provider:
            raise ValueError(
                "judge 评测的模型输出来源为「调用模型」时必须选择被评测模型（model_provider）；"
                "也可以把模型输出来源改为「数据集字段」或「不使用模型输出」"
            )

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
        gate_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[EvaluationTask]:
        """完成任务，同时写入执行历史记录（gate_result：M1 QE1-2 门禁判定）"""
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
            self._retry_with_new_session(
                lambda: self._complete_task_internal(task_id, result, total_cost))
            # 重试路径：更新统计记录
            self._retry_update_stats_complete(task_id)
            self._complete_record_with_gate(task_id, result, total_cost, gate_result)
            return self.get_task(task_id)

        # 同步写入执行历史记录
        self._complete_record_with_gate(task_id, result, total_cost, gate_result)

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

    def _complete_record_with_gate(
        self,
        task_id: str,
        result: Dict[str, Any],
        total_cost: Optional[float],
        gate_result: Optional[Dict[str, Any]],
    ) -> None:
        """complete_record 薄封装（M1 QE1-2 带 gate_result）：断连时新会话重试，
        执行记录写失败不影响任务主流程（与既有口径一致）"""
        task = self.get_task(task_id)
        execution_number = (task.execution_count or 1) if task else 1
        exec_service = TaskExecutionRecordService(self.db)
        try:
            exec_service.complete_record(
                task_id, execution_number, result, total_cost, gate_result=gate_result)
        except OperationalError as e:
            logger.warning(f"数据库操作异常（complete_record）: {e}")
            # 执行记录失败不影响主流程，使用新会话重试
            from app.db.session import SessionLocal
            new_db = SessionLocal()
            try:
                new_exec_service = TaskExecutionRecordService(new_db)
                new_exec_service.complete_record(
                    task_id, execution_number, result, total_cost, gate_result=gate_result)
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
        # M2 QE2-4：执行开始写入裁判治理快照（引擎/裁判模型摘要/prompt 模板/
        # 维度定义/消毒与金丝雀状态；非裁判类型不落，列保持 NULL）
        self._attach_judge_config_snapshot(record)
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

    def _attach_judge_config_snapshot(self, record: TaskExecutionRecord):
        """执行开始写入裁判治理快照（M2 QE2-4，失败仅告警不影响主流程）

        快照即「本次裁决所用裁判配置」的事实源（结果页裁判治理面板回显）：
        引擎 / 裁判模型摘要（掩码凭据）/ prompt 模板 / 维度定义 / 输入消毒
        与金丝雀开关状态——关闭消毒的任务在这里留痕（审计可见）。
        """
        try:
            from app.services.config_snapshot import build_judge_config_snapshot

            task = self.db.query(EvaluationTask).filter(
                EvaluationTask.id == record.task_id
            ).first()
            if task is not None:
                record.judge_config_snapshot = build_judge_config_snapshot(task, self.db)
        except Exception as e:
            logger.warning(f"写入裁判治理快照失败（task_id={record.task_id}）: {e}")

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

    def _attach_gate_error_verdict(self, record: TaskExecutionRecord, reason: str) -> None:
        """M1 QE1-2：失败/停止收尾统一落门禁 error verdict

        执行没跑完就没有聚合，门禁无从判定——启用门禁的任务在 fail/stop
        收尾时落 error verdict（CLI 退出码 2，与质量不达标的 failed 严格
        区分）；未启用门禁不落（列保持 NULL）。已有判定不覆盖。失败仅
        告警，不影响收尾主流程（与 config_snapshot 附加写同口径）。
        """
        try:
            if record.gate_result is not None:
                return
            task = self.db.query(EvaluationTask).filter(
                EvaluationTask.id == record.task_id
            ).first()
            if task is None:
                return
            from app.services.gate_service import build_error_gate_result

            gate_result = build_error_gate_result(task.gate_config, reason)
            if gate_result is not None:
                record.gate_result = gate_result
        except Exception as e:
            logger.warning(f"写入门禁 error verdict 失败（task_id={record.task_id}）: {e}")

    def _emit_webhook_events(self, task_id: str, execution_number: int,
                             status: str, gate_result: Optional[Dict[str, Any]]) -> None:
        """M1 QE1-3：执行终态事件旁路派发（task.execution.completed/failed、gate.failed）

        旁路能力（与 alert_service 同口径）：任何失败只记 warning，
        绝不打断评测收尾主流程。门禁 verdict=failed 额外发 gate.failed。
        """
        try:
            from app.services import webhook_service

            task = self.db.query(EvaluationTask).filter(
                EvaluationTask.id == task_id
            ).first()
            team_code = task.team_code if task is not None else None
            if status == "completed":
                events = [("task.execution.completed", status)]
                if isinstance(gate_result, dict) and gate_result.get("verdict") == "failed":
                    events.append(("gate.failed", status))
            else:
                events = [("task.execution.failed", status)]
            for event, event_status in events:
                webhook_service.dispatch_event(
                    event, team_code,
                    webhook_service.build_execution_payload(
                        task_id=task_id, execution_number=execution_number,
                        status=event_status, team_code=team_code,
                        gate_result=gate_result),
                    db=self.db,
                )
        except Exception as e:  # noqa: BLE001 - 旁路：绝不抛出
            logger.warning(f"webhook 事件派发失败（task_id={task_id}）: {e}")

    def complete_record(
        self,
        task_id: str,
        execution_number: int,
        result: Dict[str, Any],
        total_cost: Optional[float] = None,
        gate_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[TaskExecutionRecord]:
        """标记执行记录为已完成（gate_result：M1 QE1-2 聚合后的门禁判定）"""
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
                gate_result=gate_result,
                started_at=beijing_now(),
                completed_at=beijing_now(),
            )
            self._attach_start_config_snapshot(record)
            self._attach_used_config_summary(record)
            self.db.add(record)
            self.db.commit()
            # M1 QE1-3：完成事件旁路派发（含 gate.failed）
            self._emit_webhook_events(task_id, execution_number, "completed", gate_result)
            return record
        record.status = "completed"
        record.result = result
        record.total_cost = total_cost
        record.gate_result = gate_result
        record.completed_at = beijing_now()
        # W4：执行结束写入该次实际使用的配置摘要（归因锚点）
        self._attach_used_config_summary(record)
        self.db.commit()
        # M1 QE1-3：完成事件旁路派发（含 gate.failed）
        self._emit_webhook_events(task_id, execution_number, "completed", gate_result)
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
            # M1 QE1-3：失败事件旁路派发
            self._emit_webhook_events(task_id, execution_number, "failed", None)
            return record
        record.status = "failed"
        record.error_message = error
        record.result = {"error": error}
        record.completed_at = beijing_now()
        # W4：执行结束写入该次实际使用的配置摘要（归因锚点）
        self._attach_used_config_summary(record)
        # M1 QE1-2：失败收尾落门禁 error verdict（断点续跑前上一轮失败可查）
        self._attach_gate_error_verdict(record, f"评测失败：{error}")
        self.db.commit()
        # M1 QE1-3：失败事件旁路派发（该轮 gate_result 若已存在则随事件带给订阅方）
        self._emit_webhook_events(task_id, execution_number, "failed", record.gate_result)
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
        # M1 QE1-2：停止收尾落门禁 error verdict（续跑开新执行序号，本轮判定即终态）
        self._attach_gate_error_verdict(record, "评测被停止，门禁无法判定")
        self.db.commit()
        return record

    def get_records(self, task_id: str, limit: int = 50) -> List[TaskExecutionRecord]:
        """获取任务的所有执行历史记录，按执行次数倒序"""
        return self.db.query(TaskExecutionRecord).filter(
            TaskExecutionRecord.task_id == task_id,
        ).order_by(TaskExecutionRecord.execution_number.desc()).limit(limit).all()

    def get_record_summaries(self, task_id: str, limit: int = 50) -> List[dict]:
        """获取执行历史版本摘要（不含 result 大列），按执行次数倒序

        结果展示页选任务时只需版本列表（序号/状态/时间），全量 result 仅
        渲染选中的单版：摘要查询 SQL 只 SELECT 标量列，不触碰 result
        JSONB，避免历史接口随执行次数线性放大载荷。
        """
        rows = (
            self.db.query(
                TaskExecutionRecord.execution_number,
                TaskExecutionRecord.status,
                TaskExecutionRecord.total_cost,
                TaskExecutionRecord.error_message,
                TaskExecutionRecord.started_at,
                TaskExecutionRecord.completed_at,
                TaskExecutionRecord.gate_result,
            )
            .filter(TaskExecutionRecord.task_id == task_id)
            .order_by(TaskExecutionRecord.execution_number.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "execution_number": r.execution_number,
                "status": r.status,
                "result": None,
                "total_cost": r.total_cost,
                "error_message": r.error_message,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                # M1 QE1-6：摘要携带门禁判定（结果页版本列表直接渲染 verdict，
                # 免逐版拉全量）；gate_result 是小 JSON 列，不违反摘要轻量化口径
                "gate_result": r.gate_result,
            }
            for r in rows
        ]

    def get_dimension_trend(self, task_id: str, limit: int = 50) -> List[dict]:
        """获取跨执行版本的维度评分趋势，按执行次数倒序

        只返回 status=completed 的版本（失败/停止版本无完整结果，不参与趋势）。
        每条仅提取 result JSONB 中的 dimension_scores（与单版
        result.dimension_scores 同构：{维度: {average, weight, count}}）与
        average_score 标量，其余大字段（items/failed_items 等）不透出，
        避免趋势接口随执行次数与行数线性放大载荷。
        """
        rows = (
            self.db.query(
                TaskExecutionRecord.execution_number,
                TaskExecutionRecord.status,
                TaskExecutionRecord.completed_at,
                TaskExecutionRecord.result,
            )
            .filter(
                TaskExecutionRecord.task_id == task_id,
                TaskExecutionRecord.status == "completed",
            )
            .order_by(TaskExecutionRecord.execution_number.desc())
            .limit(limit)
            .all()
        )
        trend = []
        for r in rows:
            # result JSONB 理论上恒为 dict（fail_record/stop_record 也写 dict），
            # 兼容异常数据非 dict 时按空结果处理
            result = r.result if isinstance(r.result, dict) else {}
            trend.append({
                "execution_number": r.execution_number,
                "status": r.status,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "average_score": result.get("average_score"),
                "dimension_scores": result.get("dimension_scores") or {},
            })
        return trend

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
