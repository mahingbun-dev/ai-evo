"""
评测任务API
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_serializer, field_validator, computed_field, ConfigDict
from datetime import datetime
from app.db.session import get_db
from app.services.access_control import ensure_task_access
from app.services.evaluation_task_service import EvaluationTaskService, TaskExecutionLogService

logger = logging.getLogger(__name__)


# ==================== Request Models ====================

class CreateTaskRequest(BaseModel):
    name: str = Field(..., description="任务名称")
    description: Optional[str] = Field(None, description="任务描述")
    evaluation_type: str = Field(..., description="评测类型：judge/consistency/success_rate/rag/binary_classification")
    eval_model_config: Optional[Dict[str, Any]] = Field(None, description="模型配置")
    model_provider: Optional[str] = Field(None, description="模型提供商")
    model_endpoint: Optional[str] = Field(None, description="模型端点")
    dataset_id: Optional[str] = Field(None, description="数据集ID")
    judge_model: Optional[str] = Field(None, description="裁判模型")
    judge_engine: Optional[str] = Field("claude_agent", description="裁判引擎：claude_agent(Claude Code Agent)/graph(裁判图 HTTP 直调)")
    judge_prompt: Optional[str] = Field(None, description="评测prompt")
    consistency_config: Optional[Dict[str, Any]] = Field(None, description="一致性评测配置")
    success_rate_config: Optional[Dict[str, Any]] = Field(None, description="成功率评测配置")
    rag_config: Optional[Dict[str, Any]] = Field(None, description="RAG评测配置")
    latency_config: Optional[Dict[str, Any]] = Field(None, description="耗时评测配置")
    binary_metrics_config: Optional[Dict[str, Any]] = Field(None, description="二分分类评测配置")
    multi_judge_config: Optional[Dict[str, Any]] = Field(None, description="多裁判配置")
    judge_count: Optional[int] = Field(6, description="裁判数量")
    parallel_config: Optional[Dict[str, Any]] = Field(None, description="并行执行配置")
    # 评测指令配置
    evaluation_scene: Optional[str] = Field(None, description="评测场景")
    scene_desc: Optional[str] = Field(None, description="场景描述")
    evaluation_metrics: Optional[List[Dict[str, Any]]] = Field(None, description="评测维度列表")
    max_score: Optional[int] = Field(10, description="最大分值")
    score_details: Optional[str] = Field(None, description="分值标准")
    steps: Optional[List[str]] = Field(None, description="评分步骤")
    created_by: Optional[str] = Field(None, description="创建人")
    # 变量注入配置
    runtime_variables: Optional[Dict[str, Any]] = Field(None, description="运行时变量（手动输入）")
    variable_source: Optional[str] = Field("manual", description="变量来源：manual-手动输入，dataset-从数据集选择")
    variable_mappings: Optional[Dict[str, str]] = Field(None, description="变量到数据集字段的映射")
    variable_sources: Optional[Dict[str, str]] = Field(None, description="变量数据来源：manual-手动输入，dataset-数据集字段")
    variable_types: Optional[Dict[str, str]] = Field(None, description="变量类型：string/number/boolean")
    enable_user_import: Optional[bool] = Field(False, description="是否启用user_import注入")
    user_import_template: Optional[str] = Field(None, description="user_import模板格式")
    # 裁判模型变量配置
    judge_variable_config: Optional[Dict[str, Any]] = Field(None, description="裁判模型变量配置：user_input/ref_answer/model_answer的数据来源和prompt类型")
    # 评测目标配置（多评测目标）
    evaluation_targets: Optional[List[Dict[str, Any]]] = Field(None, description="评测目标配置列表")
    # 空值校验开关
    validation_enabled: Optional[bool] = Field(True, description="是否启用空值校验")
    # 关联的评测模板ID
    template_id: Optional[str] = Field(None, description="关联的评测模板ID")
    # 评测前处理脚本编排（pre_judge_scripts，串行执行，输出进入裁判变量体系 script_output）
    pre_scripts: Optional[List[Dict[str, Any]]] = Field(None, description="评测前处理脚本编排列表")


class UpdateTaskRequest(BaseModel):
    name: Optional[str] = None
    evaluation_type: Optional[str] = Field(None, description="评测类型：judge/consistency/success_rate/rag/binary_classification")
    description: Optional[str] = None
    eval_model_config: Optional[Dict[str, Any]] = None
    model_provider: Optional[str] = None
    model_endpoint: Optional[str] = None
    dataset_id: Optional[str] = None
    judge_model: Optional[str] = None
    judge_engine: Optional[str] = None
    judge_prompt: Optional[str] = None
    evaluation_scene: Optional[str] = None
    scene_desc: Optional[str] = None
    evaluation_metrics: Optional[List[Dict[str, Any]]] = None
    max_score: Optional[int] = None
    score_details: Optional[str] = None
    steps: Optional[List[str]] = None
    # 变量注入配置
    runtime_variables: Optional[Dict[str, Any]] = None
    variable_source: Optional[str] = None
    variable_mappings: Optional[Dict[str, str]] = None
    variable_sources: Optional[Dict[str, str]] = None
    variable_types: Optional[Dict[str, str]] = None
    enable_user_import: Optional[bool] = None
    user_import_template: Optional[str] = None
    # 裁判模型变量配置
    judge_variable_config: Optional[Dict[str, Any]] = None
    rag_config: Optional[Dict[str, Any]] = None
    # 裁判数量配置
    judge_count: Optional[int] = None
    # 一致性评测配置
    consistency_config: Optional[Dict[str, Any]] = None
    # 成功率评测配置
    success_rate_config: Optional[Dict[str, Any]] = None
    # 耗时评测配置
    latency_config: Optional[Dict[str, Any]] = None
    # 二分分类评测配置
    binary_metrics_config: Optional[Dict[str, Any]] = None
    # 关联的评测模板ID
    template_id: Optional[str] = None
    # 评测目标配置（多评测目标）
    evaluation_targets: Optional[List[Dict[str, Any]]] = None
    # 空值校验开关
    validation_enabled: Optional[bool] = None
    # 评测前处理脚本编排
    pre_scripts: Optional[List[Dict[str, Any]]] = None


# ==================== Response Models ====================

class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str]
    evaluation_type: str
    status: str
    dataset_id: Optional[str]
    model_provider: Optional[str] = None  # 被评测模型ID
    model_endpoint: Optional[str] = None
    judge_model: Optional[str]
    judge_engine: Optional[str] = None
    judge_prompt: Optional[str] = None
    evaluation_scene: Optional[str] = None
    scene_desc: Optional[str] = None
    evaluation_metrics: Optional[List[Dict[str, Any]]] = None
    max_score: Optional[int] = None
    score_details: Optional[str] = None
    steps: Optional[List[str]] = None
    runtime_variables: Optional[Dict[str, Any]] = None
    variable_source: Optional[str] = None
    variable_mappings: Optional[Dict[str, str]] = None
    variable_sources: Optional[Dict[str, str]] = None
    variable_types: Optional[Dict[str, str]] = None
    enable_user_import: Optional[bool] = None
    user_import_template: Optional[str] = None
    judge_variable_config: Optional[List[Dict[str, Any]]] = None
    judge_count: Optional[int] = None
    # 评测目标配置（多评测目标）
    evaluation_targets: Optional[List[Dict[str, Any]]] = None
    # 空值校验开关
    validation_enabled: Optional[bool] = True
    binary_metrics_config: Optional[Dict[str, Any]] = None
    # 评测前处理脚本编排
    pre_scripts: Optional[List[Dict[str, Any]]] = None
    progress: int
    total_questions: int
    completed_questions: int
    total_cost: Optional[float]
    created_at: datetime
    completed_at: Optional[datetime]

    @field_serializer('judge_variable_config')
    def serialize_judge_variable_config(self, value):
        # 如果是字典格式 {key: {...}} 转换为列表格式 [{key: key, ...}]
        if value is None or value == '':
            return []
        if isinstance(value, dict):
            return [
                {'key': key, **val} if isinstance(val, dict) else {'key': key, 'value': val}
                for key, val in value.items()
            ]
        return value

    @field_validator('judge_variable_config', mode='before')
    @classmethod
    def parse_judge_variable_config(cls, v):
        # 如果是字典格式 {key: {...}} 转换为列表格式 [{key: key, ...}]
        if v is None or v == '':
            return []
        if isinstance(v, dict):
            return [
                {'key': key, **val} if isinstance(val, dict) else {'key': key, 'value': val}
                for key, val in v.items()
            ]
        return v


class TaskConfigResponse(BaseModel):
    """任务配置信息（用于编辑场景，不包含执行结果等大体字段）"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str]
    evaluation_type: str
    status: str
    dataset_id: Optional[str]
    model_provider: Optional[str] = None
    model_endpoint: Optional[str] = None
    judge_model: Optional[str]
    judge_engine: Optional[str] = None
    judge_prompt: Optional[str] = None
    evaluation_scene: Optional[str] = None
    scene_desc: Optional[str] = None
    evaluation_metrics: Optional[List[Dict[str, Any]]] = None
    max_score: Optional[int] = None
    score_details: Optional[str] = None
    steps: Optional[List[str]] = None
    runtime_variables: Optional[Dict[str, Any]] = None
    variable_source: Optional[str] = None
    variable_mappings: Optional[Dict[str, str]] = None
    variable_sources: Optional[Dict[str, str]] = None
    variable_types: Optional[Dict[str, str]] = None
    enable_user_import: Optional[bool] = None
    user_import_template: Optional[str] = None
    judge_variable_config: Optional[List[Dict[str, Any]]] = None
    judge_count: Optional[int] = None
    # 评测目标配置（多评测目标）
    evaluation_targets: Optional[List[Dict[str, Any]]] = None
    # 空值校验开关
    validation_enabled: Optional[bool] = True
    # 评测配置
    eval_model_config: Optional[Dict[str, Any]] = None
    consistency_config: Optional[Dict[str, Any]] = None
    success_rate_config: Optional[Dict[str, Any]] = None
    rag_config: Optional[Dict[str, Any]] = None
    latency_config: Optional[Dict[str, Any]] = None
    multi_judge_config: Optional[Dict[str, Any]] = None
    parallel_config: Optional[Dict[str, Any]] = None
    binary_metrics_config: Optional[Dict[str, Any]] = None
    template_id: Optional[str] = None
    # 评测前处理脚本编排
    pre_scripts: Optional[List[Dict[str, Any]]] = None
    # 不包含执行结果字段：result, total_cost, completed_at, progress, total_questions, completed_questions
    created_at: datetime

    @field_serializer('created_at')
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat() if value else ""

    @field_serializer('judge_variable_config')
    def serialize_judge_variable_config(self, value):
        # 如果是字典格式 {key: {...}} 转换为列表格式 [{key: key, ...}]
        if value is None or value == '':
            return []
        if isinstance(value, dict):
            return [
                {'key': key, **val} if isinstance(val, dict) else {'key': key, 'value': val}
                for key, val in value.items()
            ]
        return value

    @field_validator('judge_variable_config', mode='before')
    @classmethod
    def parse_judge_variable_config(cls, v):
        # 如果是字典格式 {key: {...}} 转换为列表格式 [{key: key, ...}]
        if v is None or v == '':
            return []
        if isinstance(v, dict):
            # 从 {key: config} 转换为 [{key: key, ...config}]
            return [
                {'key': key, **value} if isinstance(value, dict) else {'key': key, 'value': value}
                for key, value in v.items()
            ]
        return v


class TaskDetailResponse(TaskResponse):
    # TaskDetailResponse 继承自 TaskResponse，包含所有基础字段
    # 以下是 TaskDetailResponse 特有的额外字段
    eval_model_config: Optional[Dict[str, Any]] = None
    consistency_config: Optional[Dict[str, Any]] = None
    success_rate_config: Optional[Dict[str, Any]] = None
    rag_config: Optional[Dict[str, Any]] = None
    multi_judge_config: Optional[Dict[str, Any]] = None
    parallel_config: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    template_id: Optional[str] = None


class TaskStatusResponse(BaseModel):
    """任务轻量状态（W14 轮询专用）：只含进度热字段，不查/不序列化大 JSON 列

    面向前端 useTaskPolling 高频轮询：四个字段覆盖状态徽标与进度条渲染
    （status/progress）与「已完成 X / Y 题」文案（completed_questions/total）。
    """
    status: str
    progress: int = 0
    completed_questions: int = 0
    total: int = 0


class EvaluationResultResponse(BaseModel):
    """评测结果响应模型 - 区分成功/失败状态"""
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    status: str
    evaluation_type: Optional[str] = None

    # 成功时返回
    result: Optional[Dict[str, Any]] = None
    total_cost: Optional[float] = None
    completed_at: Optional[datetime] = None

    # 失败时返回
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_log_id: Optional[str] = None

    @field_serializer('completed_at')
    def serialize_completed_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None


class LogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    level: str
    message: str
    details: Optional[Dict[str, Any]]
    created_at: datetime

    @field_serializer('created_at')
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat() if value else ""


class ExecutionRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    execution_number: int
    status: str
    result: Optional[Dict[str, Any]] = None
    total_cost: Optional[float] = None
    error_message: Optional[str] = None
    remark: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    # W4（P0-C）配置归因锚点：执行开始写创建时双快照、结束写实际使用配置摘要
    # （仅掩码凭据+指纹，无明文 api_key）
    config_snapshot: Optional[Dict[str, Any]] = None

    @field_serializer('started_at')
    def serialize_started_at(self, value: datetime) -> str:
        return value.isoformat() if value else ""

    @field_serializer('completed_at')
    def serialize_completed_at(self, value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    @computed_field
    @property
    def duration_ms(self) -> Optional[int]:
        """计算耗时（毫秒），通过 completed_at - started_at 计算"""
        if self.completed_at and self.started_at:
            delta = self.completed_at - self.started_at
            # 处理 timedelta 或 datetime 相减
            if hasattr(delta, 'total_seconds'):
                return int(delta.total_seconds() * 1000)
        return None


class TaskMigrateConfigResponse(BaseModel):
    """单任务旧版配置迁移结果摘要"""
    task_id: str = Field(..., description="任务ID")
    migrated: bool = Field(..., description="是否发生变更（False=已迁移/无需迁移）")
    steps_migrated: int = Field(0, description="合并进 score_details 的评分步骤行数")
    pre_scripts_migrated: int = Field(0, description="转为 script_output 待评测数据行的前处理脚本条数")
    target_ids: List[str] = Field(default_factory=list, description="迁移生成的待评测数据行 id")
    changes: List[str] = Field(default_factory=list, description="变更摘要")


class MigrateLegacyAllResponse(BaseModel):
    """批量迁移旧版配置结果"""
    total: int = Field(..., description="扫描总数")
    migrated: int = Field(..., description="发生迁移的条数")
    skipped: int = Field(..., description="跳过（无需迁移）的条数")
    errors: List[Dict[str, Any]] = Field(default_factory=list, description="失败明细 [{id, error}]")


# ==================== Router ====================

router = APIRouter(prefix="/evaluation/tasks", tags=["评测任务"])


@router.post("", response_model=TaskResponse, summary="创建评测任务")
def create_task(
    body: CreateTaskRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """创建新的评测任务"""
    user = request.state.user
    # 支持字典和对象两种格式（字典是中间件传递的，对象是其他场景使用）
    user_code = user.get('user_code') if isinstance(user, dict) else (user.user_code if hasattr(user, 'user_code') else body.created_by)
    org_code = user.get('org_code') if isinstance(user, dict) else (user.org_code if hasattr(user, 'org_code') else None)
    service = EvaluationTaskService(db)
    try:
        task = service.create_task(
            name=body.name,
            evaluation_type=body.evaluation_type,
            description=body.description,
            model_config=body.eval_model_config,
            model_provider=body.model_provider,
            model_endpoint=body.model_endpoint,
            dataset_id=body.dataset_id,
            judge_model=body.judge_model,
            judge_engine=body.judge_engine,
            judge_prompt=body.judge_prompt,
        consistency_config=body.consistency_config,
        success_rate_config=body.success_rate_config,
        rag_config=body.rag_config,
        latency_config=body.latency_config,
        binary_metrics_config=body.binary_metrics_config,
        multi_judge_config=body.multi_judge_config,
        parallel_config=body.parallel_config,
        evaluation_scene=body.evaluation_scene,
        scene_desc=body.scene_desc,
        evaluation_metrics=body.evaluation_metrics,
        max_score=body.max_score,
        score_details=body.score_details,
        steps=body.steps,
        created_by=user_code,
        runtime_variables=body.runtime_variables,
        variable_sources=body.variable_sources,
        variable_types=body.variable_types,
        variable_mappings=body.variable_mappings,
        enable_user_import=body.enable_user_import,
        user_import_template=body.user_import_template,
        judge_count=body.judge_count,
        team_code=org_code if org_code else "DEFAULT",
        judge_variable_config=body.judge_variable_config,
        evaluation_targets=body.evaluation_targets,
        validation_enabled=body.validation_enabled,
        template_id=body.template_id,
        pre_scripts=body.pre_scripts,
    )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 记录审计日志（V2 新增）- 使用独立 session，不影响主业务
    try:
        from app.services.audit_service import log_operation
        client_host = request.client.host if request.client else ""
        log_operation(user, "CREATE", "task", task.id, task.name, ip_address=client_host)
    except Exception as e:
        logger.warning(f"审计日志记录失败: {e}")
        db.rollback()  # 清理 session 状态

    return task


# ==================== Variable Endpoints ====================
# 注意：这些路由必须放在 /{task_id} 之前，否则会被 /{task_id} 匹配

@router.get("/models/{model_id}/variables", summary="获取模型的变量列表")
def get_model_variables(
    model_id: str,
    db: Session = Depends(get_db),
):
    """获取模型配置中的动态变量列表"""
    from app.services.model_config_service import ModelConfigService

    service = ModelConfigService(db)
    config = service.get_model(model_id)

    if not config:
        raise HTTPException(status_code=404, detail="模型不存在")

    from app.services.request_chain import RequestChainExecutor
    variables = RequestChainExecutor.extract_variables(
        config.request_config.get("chain_requests") if config.request_config else {},
        config.auth_config  # 传递 auth_config 以排除登录变量
    )
    return {"model_id": model_id, "variables": variables}


@router.get("/datasets/{dataset_id}/fields", summary="获取数据集可用字段")
def get_dataset_fields(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取数据集的可用字段（含自定义字段和is_media绑定；W9：数据集归属校验）"""
    from app.services.access_control import ensure_dataset_access
    from app.services.dataset_service import DatasetDataService
    from app.services.dataset_field_service import DatasetFieldService

    # 验证数据集存在 + 团队归属
    ensure_dataset_access(db, request.state.user, dataset_id)

    # 优先从字段元数据表获取字段结构
    field_service = DatasetFieldService(db)
    field_metas = field_service.get_field_metadata(dataset_id)

    fields = []
    if field_metas:
        # 使用字段元数据
        for f in field_metas:
            fields.append({
                "name": f["name"],
                "label": f.get("label", f["name"]),
                "type": f.get("type", "string"),
                "is_media": f.get("is_media", False),
                "is_media_field": f.get("is_media_field")
            })
    else:
        # 兼容旧逻辑：从数据推断字段
        data_service = DatasetDataService(db)
        items = data_service.get_data_list(dataset_id, skip=0, limit=1)

        if items:
            first_item = items[0]
            # 检查 question 字段（对应 user_input）
            if first_item.question:
                fields.append({
                    "name": "question",
                    "label": "user_input（用户输入）",
                    "type": "string",
                    "is_media": False,
                    "is_media_field": "question_is_media"
                })
            # 检查 ref_answer 字段（对应 answer）
            if first_item.ref_answer:
                fields.append({
                    "name": "ref_answer",
                    "label": "answer（参考答案）",
                    "type": "string",
                    "is_media": False,
                    "is_media_field": "ref_answer_is_media"
                })

    return {"dataset_id": dataset_id, "fields": fields}


@router.get("", response_model=List[TaskResponse], summary="获取任务列表")
def get_tasks(
    request: Request,
    status: Optional[str] = Query(None, description="状态过滤"),
    evaluation_type: Optional[str] = Query(None, description="评测类型过滤"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """获取评测任务列表（超管看全部，普通用户看自己团队）"""
    user = request.state.user
    service = EvaluationTaskService(db)

    # 支持字典和对象两种格式
    super_admin_flag = user.get('super_admin_flag') if isinstance(user, dict) else (user.super_admin_flag if hasattr(user, 'super_admin_flag') else 0)
    org_code = user.get('org_code') if isinstance(user, dict) else (user.org_code if hasattr(user, 'org_code') else None)

    # 超管不传 team_code（看全部），普通用户传 team_code（只看自己团队）
    team_code = None
    if super_admin_flag != 1:
        team_code = org_code if org_code else "DEFAULT"

    tasks = service.get_tasks(status=status, evaluation_type=evaluation_type, skip=skip, limit=limit, team_code=team_code)
    return tasks


@router.post("/migrate-legacy-all", response_model=MigrateLegacyAllResponse, summary="批量迁移旧版任务配置")
def migrate_legacy_all_tasks(
    request: Request,
    db: Session = Depends(get_db),
):
    """批量迁移旧版任务配置（幂等）：steps 合并进 score_details、pre_scripts 转为 script_output 待评测数据行

    团队口径与任务列表一致：超管迁移全量，普通用户仅迁移本团队任务。
    """
    user = request.state.user
    super_admin_flag = user.get('super_admin_flag') if isinstance(user, dict) else (user.super_admin_flag if hasattr(user, 'super_admin_flag') else 0)
    org_code = user.get('org_code') if isinstance(user, dict) else (user.org_code if hasattr(user, 'org_code') else None)

    from app.services.config_migration_service import ConfigMigrationService
    service = ConfigMigrationService(db)
    if super_admin_flag == 1:
        return service.migrate_all_tasks()
    return service.migrate_all_tasks(team_code=org_code if org_code else "DEFAULT")


@router.get("/{task_id}", response_model=TaskDetailResponse, summary="获取任务详情")
def get_task(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取评测任务详情（W9：对象级归属校验，跨团队 403）"""
    task = ensure_task_access(db, request.state.user, task_id)
    return task


@router.get("/{task_id}/config", response_model=TaskConfigResponse, summary="获取任务配置（编辑场景）")
def get_task_config(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取任务配置信息，不包含执行结果等大体字段，用于编辑场景（W9：归属校验）"""
    task = ensure_task_access(db, request.state.user, task_id)
    return task


@router.get("/{task_id}/status", response_model=TaskStatusResponse, summary="获取任务轻量状态（轮询专用）")
def get_task_status(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """轮询专用轻端点：仅返回 status/progress/completed_questions/total 四字段

    W14 轮询优化：前端 useTaskPolling 由每 8s 拉全量 detail（GET /{task_id}，
    加载 result/config 等大 JSON 列）切换至本端点——SQL 只 SELECT 标量列
    （id 供过滤），不触碰大 JSON 列，单次轮询开销从全行加载降为标量读取。

    W19b：补对象级归属校验（W14 时漏出 W9 收口，可被跨团队探测任务存在性
    与进度）——team_code 并入同一条标量 SELECT 后用 check_team_access 判定，
    单查询同时完成「查出 + 校验」，不回退 W14 的轻量化。
    """
    from app.models.task_models import EvaluationTask
    from app.services.access_control import AccessDeniedError, check_team_access

    row = (
        db.query(
            EvaluationTask.id,
            EvaluationTask.status,
            EvaluationTask.progress,
            EvaluationTask.completed_questions,
            EvaluationTask.total_questions,
            EvaluationTask.team_code,
        )
        .filter(EvaluationTask.id == task_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        check_team_access(request.state.user, row.team_code)
    except AccessDeniedError:
        raise HTTPException(status_code=403, detail="无权访问该任务（跨团队操作被拒绝）")
    return TaskStatusResponse(
        status=row.status,
        progress=row.progress or 0,
        completed_questions=row.completed_questions or 0,
        total=row.total_questions or 0,
    )


@router.put("/{task_id}", response_model=TaskResponse, summary="更新任务")
def update_task(
    task_id: str,
    body: UpdateTaskRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """更新评测任务（W9：对象级归属校验，查出+校验一步完成防 TOCTOU）"""
    task = ensure_task_access(db, request.state.user, task_id)
    service = EvaluationTaskService(db)

    # 更新任务
    try:
        task = service.update_task(task.id, **body.model_dump(exclude_unset=True))
    except ValueError as e:
        # pre_scripts 等配置校验失败（脚本不存在/已停用、exec_type 非法、input_mapping 字段缺失）
        raise HTTPException(status_code=400, detail=str(e))

    # 总是尝试刷新题目数（如果dataset_id存在）
    if task.dataset_id:
        service.refresh_dataset_count(task_id)
        task = service.get_task(task_id)

    return task


@router.delete("/{task_id}", summary="删除任务")
def delete_task(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """删除评测任务（W9：对象级归属校验）"""
    ensure_task_access(db, request.state.user, task_id)
    service = EvaluationTaskService(db)
    success = service.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"message": "删除成功"}


@router.post("/{task_id}/run", response_model=TaskResponse, summary="执行评测")
def run_task(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """执行评测任务（提交到 APScheduler 线程池；W9：对象级归属校验）"""
    task = ensure_task_access(db, request.state.user, task_id)

    # 允许重新运行已停止或失败的任务
    if task.status == "running":
        raise HTTPException(status_code=400, detail="任务正在执行中")

    from app.services.task_scheduler import get_task_scheduler
    scheduler = get_task_scheduler()

    # 先更新状态为 running（原子操作，防止并发请求重复提交）
    service = EvaluationTaskService(db)
    task = service.start_task(task_id)
    if task is None:
        # start_task 返回 None 说明任务已被其他请求启动
        raise HTTPException(status_code=400, detail="任务正在执行中")

    # 再提交到 Celery Worker 执行
    submitted = scheduler.submit_task(task_id)
    if not submitted:
        service.fail_task(task_id, "任务提交到 Celery Worker 失败")
        raise HTTPException(status_code=400, detail="任务提交失败，请重试")

    return task


@router.post("/{task_id}/stop", response_model=TaskResponse, summary="停止评测")
def stop_task(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """停止评测任务（发送停止信号，真正中断执行中的任务；W9：对象级归属校验）"""
    task = ensure_task_access(db, request.state.user, task_id)

    # 如果任务已完成或已停止，直接返回成功
    if task.status in ["completed", "stopped"]:
        return task

    # running、pending、failed 均可执行停止（failed 时后台线程可能仍在运行）
    if task.status not in ["running", "pending", "failed"]:
        raise HTTPException(status_code=400, detail=f"当前任务状态为{task.status}，无法停止")

    # 向调度器发送停止信号（设置 threading.Event，实际中断执行中的线程）
    from app.services.task_scheduler import get_task_scheduler
    get_task_scheduler().stop_task(task_id)

    # 更新数据库状态
    task = EvaluationTaskService(db).stop_task(task_id)
    return task


@router.post("/{task_id}/refresh-count", response_model=TaskResponse, summary="刷新题目数")
def refresh_task_count(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """从数据集刷新题目数（W9：对象级归属校验）"""
    ensure_task_access(db, request.state.user, task_id)
    service = EvaluationTaskService(db)
    task = service.refresh_dataset_count(task_id)
    return task


@router.post("/{task_id}/migrate-config", response_model=TaskMigrateConfigResponse, summary="迁移旧版任务配置")
def migrate_task_config(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """迁移单个任务的旧版配置（幂等）：steps 合并进 score_details、pre_scripts 转为 script_output 待评测数据行（W9：归属校验）"""
    task = ensure_task_access(db, request.state.user, task_id)

    from app.services.config_migration_service import ConfigMigrationService
    try:
        summary = ConfigMigrationService(db).migrate_task(task)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return summary


@router.get("/{task_id}/logs", response_model=List[LogResponse], summary="获取任务日志")
def get_task_logs(
    task_id: str,
    request: Request,
    level: Optional[str] = Query(None, description="日志级别过滤"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """获取任务执行日志（W9：对象级归属校验）"""
    # 验证任务存在 + 团队归属
    ensure_task_access(db, request.state.user, task_id)

    log_service = TaskExecutionLogService(db)
    logs = log_service.get_logs(task_id, level=level, limit=limit)
    return logs


@router.get("/{task_id}/executions", response_model=List[ExecutionRecordResponse], summary="获取执行历史列表")
def get_task_executions(
    task_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """获取任务的执行历史记录列表（W9：对象级归属校验）"""
    ensure_task_access(db, request.state.user, task_id)

    from app.services.evaluation_task_service import TaskExecutionRecordService
    record_service = TaskExecutionRecordService(db)
    records = record_service.get_records(task_id, limit=limit)
    return records


# ==================== Results Endpoints ====================

results_router = APIRouter(prefix="/evaluation/results", tags=["评测结果"])


@results_router.get("/{task_id}", response_model=EvaluationResultResponse, summary="获取评测结果")
def get_result(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取评测结果（W9：对象级归属校验）"""
    task = ensure_task_access(db, request.state.user, task_id)

    # 失败状态返回错误信息
    if task.status == "failed":
        error_info = {
            "error_code": "API_ERROR",
            "error_message": "任务执行失败",
            "error_log_id": None,
        }
        # 尝试从 task.result 中解析错误信息
        if task.result:
            if isinstance(task.result, dict):
                error_info["error_code"] = task.result.get("error_code", "API_ERROR")
                error_info["error_message"] = task.result.get("error_message", "任务执行失败")
                error_info["error_log_id"] = task.result.get("error_log_id")

        return EvaluationResultResponse(
            task_id=task.id,
            status=task.status,
            evaluation_type=task.evaluation_type,
            error_code=error_info["error_code"],
            error_message=error_info["error_message"],
            error_log_id=error_info["error_log_id"],
        )

    # 完成状态返回结果
    return EvaluationResultResponse(
        task_id=task.id,
        status=task.status,
        evaluation_type=task.evaluation_type,
        result=task.result,
        total_cost=task.total_cost,
        completed_at=task.completed_at,
    )


@results_router.get("/{task_id}/history", summary="获取所有历史版本结果")
def get_result_history(
    task_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """获取任务所有历史版本的评测结果列表（W9：对象级归属校验）"""
    task = ensure_task_access(db, request.state.user, task_id)

    from app.services.evaluation_task_service import TaskExecutionRecordService
    record_service = TaskExecutionRecordService(db)
    records = record_service.get_records(task_id, limit=limit)

    # 兼容处理：如果没有执行记录但任务有结果，从任务结果创建兼容记录
    if not records and task.result:
        # 检查任务结果是否已经是聚合格式（包含items等）
        result_data = task.result
        # 创建兼容记录
        compatibility_record = {
            "execution_number": task.execution_count or 1,
            "status": task.status,
            "result": result_data,
            "total_cost": task.total_cost,
            "error_message": None,
            "started_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }
        return [compatibility_record]

    return [
        {
            "execution_number": r.execution_number,
            "status": r.status,
            "result": r.result,
            "total_cost": r.total_cost,
            "error_message": r.error_message,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in records
    ]
