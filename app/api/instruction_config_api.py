"""
评测指令配置API
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from app.services.access_control import ensure_permission, ensure_super_admin
from app.services.audit_service import audited
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional, Union
from pydantic import BaseModel, Field, field_serializer, field_validator, ConfigDict
from datetime import datetime
from app.db.session import get_db
from app.services.instruction_builder import InstructionBuilder, TemplateService
from app.models.template_models import EvaluationTemplate
import uuid


# ==================== Request Models ====================

class EvaluationMetricModel(BaseModel):
    id: str = Field(default_factory=lambda: f"metric_{uuid.uuid4().hex[:6]}")
    name: str = Field(..., description="维度名称")
    weight: Union[str, float] = Field("中", description="权重：high/medium/low 或数值（如 0.6）")
    max_score: int = Field(10, description="最大分值")
    description: Optional[str] = Field(None, description="维度描述")
    score_criteria: Optional[Dict[str, Any]] = Field(default_factory=dict, description="评分标准")
    dependencies: Optional[List[Dict[str, Any]]] = Field(default_factory=list, description="维度依赖关系")


class CreateInstructionConfigRequest(BaseModel):
    name: str = Field(..., description="配置名称")
    scene: str = Field(..., description="评测场景")
    scene_description: Optional[str] = Field(None, description="场景描述")
    judge_prompt: Optional[str] = Field(None, description="评测指令")
    evaluation_metrics: List[EvaluationMetricModel] = Field(default_factory=list, description="评测维度")
    score_details: Optional[str] = Field(None, description="评分标准")
    max_score: int = Field(10, description="最大分值")
    # 契约 v3：steps 已废弃（维度顺序即步骤）；兼容前端显式传 null，与 Update schema 口径一致
    steps: Optional[List[str]] = Field(default_factory=list, description="评分步骤（已废弃，仅兼容接收）")
    judge_variable_config: Optional[List[Dict[str, Any]]] = Field(default_factory=list, description="裁判变量配置")


class UpdateInstructionConfigRequest(BaseModel):
    name: Optional[str] = None
    scene: Optional[str] = None
    scene_description: Optional[str] = None
    judge_prompt: Optional[str] = None
    evaluation_metrics: Optional[List[EvaluationMetricModel]] = None
    score_details: Optional[str] = None
    max_score: Optional[int] = None
    steps: Optional[List[str]] = None
    judge_variable_config: Optional[List[Dict[str, Any]]] = None
    # M5 QE5-2 编辑自动打版：携带（非 None）即先为改前内容打版再应用编辑
    version_changelog: Optional[str] = Field(
        None, max_length=500, description="编辑留档：携带即先把当前内容打版（改前状态进版本链）"
    )


class CreateTemplateVersionRequest(BaseModel):
    """模板打版请求（M5 QE5-2）"""
    changelog: Optional[str] = Field(None, max_length=500, description="版本变更说明")


class PreviewRequest(BaseModel):
    scene: str = Field(..., description="评测场景")
    scene_description: Optional[str] = Field(None, description="场景描述")
    judge_prompt: Optional[str] = Field(None, description="评测指令")
    evaluation_metrics: List[EvaluationMetricModel] = Field(default_factory=list, description="评测维度")
    max_score: int = Field(10, description="最大分值")
    steps: List[str] = Field(default_factory=list, description="评分步骤")


class ValidateRequest(BaseModel):
    scene: Optional[str] = Field(None, description="评测场景")
    scene_description: Optional[str] = Field(None, description="场景描述")
    evaluation_metrics: List[EvaluationMetricModel] = Field(default_factory=list, description="评测维度")
    max_score: int = Field(10, description="最大分值")


# ==================== Response Models ====================

class InstructionConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    scene: str
    scene_description: Optional[str]
    judge_prompt: Optional[str] = None
    evaluation_metrics: List[Dict[str, Any]]
    max_score: int
    score_details: Optional[str]
    steps: Optional[List[str]]
    is_preset: bool
    created_at: datetime
    judge_variable_config: Optional[List[Dict[str, Any]]] = None

    @field_validator('steps', mode='before')
    @classmethod
    def parse_steps(cls, v):
        if v == '' or v is None:
            return []
        # 如果是字符串，尝试按换行符分割成列表
        if isinstance(v, str):
            return [s.strip() for s in v.split('\n') if s.strip()]
        return v

    @field_serializer('created_at')
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat() if value else ""


class PreviewResponse(BaseModel):
    scene: str
    scene_description: str
    metrics: List[str]
    max_score: int
    steps: List[str]
    full_prompt: str


class ValidateResponse(BaseModel):
    valid: bool
    errors: List[str]
    warnings: List[str]


class PresetSceneResponse(BaseModel):
    id: str
    name: str
    description: str


class MigrateLegacyAllResponse(BaseModel):
    """批量迁移旧版模板配置结果"""
    total: int = Field(..., description="扫描模板总数")
    migrated: int = Field(..., description="发生迁移的模板数")
    skipped: int = Field(..., description="跳过（无需迁移）的模板数")
    errors: List[Dict[str, Any]] = Field(default_factory=list, description="失败明细 [{id, error}]")


# ==================== Router ====================

router = APIRouter(prefix="/instruction", tags=["评测指令配置"])


def _parse_metrics(metrics: List[EvaluationMetricModel] | List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """解析metrics为字典列表"""
    return [m.model_dump() if hasattr(m, 'model_dump') else m for m in metrics]


@router.post("/config", response_model=InstructionConfigResponse, summary="创建评测指令配置",
             dependencies=[Depends(audited("CREATE", "template"))])
def create_instruction_config(
    body: CreateInstructionConfigRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """创建新的评测指令配置"""
    ensure_permission(request.state.user, "templates")  # RBAC（QE4-3）：viewer 只读
    builder = InstructionBuilder()

    # 构建模板
    config = {
        "scene": body.scene,
        "scene_description": body.scene_description or "",
        "evaluation_metrics": _parse_metrics(body.evaluation_metrics),
        "max_score": body.max_score,
        "steps": body.steps or [],
    }

    # 生成指令模板
    instruction_template = builder.build(config)

    # 保存到数据库
    template_id = f"tmpl_{uuid.uuid4().hex[:12]}"
    template = EvaluationTemplate(
        id=template_id,
        name=body.name,
        scene=body.scene,
        scene_description=body.scene_description,
        judge_prompt=body.judge_prompt,
        evaluation_metrics=_parse_metrics(body.evaluation_metrics),
        max_score=body.max_score,
        score_details=body.score_details,
        steps="\n".join(body.steps) if body.steps else None,
        instruction_template=instruction_template,
        judge_variable_config=body.judge_variable_config,
    )

    db.add(template)
    db.commit()
    db.refresh(template)

    # QE4-4：暂存新资源标识供 audited 依赖落审计（直调测试 request 可能缺 state）
    if hasattr(request, "state"):
        request.state.audit_resource = {"id": template.id, "name": template.name}
    return template


@router.get("/config", response_model=List[InstructionConfigResponse], summary="获取评测指令配置列表")
def get_instruction_configs(
    scene: Optional[str] = Query(None, description="场景过滤"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """获取评测指令配置列表"""
    query = db.query(EvaluationTemplate)

    if scene:
        query = query.filter(EvaluationTemplate.scene == scene)

    return query.offset(skip).limit(limit).all()


@router.get("/config/{config_id}", response_model=InstructionConfigResponse, summary="获取配置详情")
def get_instruction_config(
    config_id: str,
    db: Session = Depends(get_db),
):
    """获取评测指令配置详情"""
    template = db.query(EvaluationTemplate).filter(EvaluationTemplate.id == config_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="配置不存在")
    return template


@router.put("/config/{config_id}", response_model=InstructionConfigResponse, summary="更新配置",
            dependencies=[Depends(audited("UPDATE", "template", id_param="config_id"))])
def update_instruction_config(
    config_id: str,
    body: UpdateInstructionConfigRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """更新评测指令配置"""
    ensure_permission(request.state.user, "templates")  # RBAC（QE4-3）：viewer 只读
    template = db.query(EvaluationTemplate).filter(EvaluationTemplate.id == config_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="配置不存在")

    # M5 QE5-2 编辑自动打版：携带 version_changelog（非 None）即先把改前内容
    # 留存进版本链，再应用编辑——显式版本化编辑覆盖的状态都可回溯
    if body.version_changelog is not None:
        from app.services.template_version_service import TemplateVersionService

        user = request.state.user or {}
        TemplateVersionService(db).auto_version_before_update(
            template,
            changelog=body.version_changelog,
            created_by=user.get("user_code"),
        )

    # 更新字段
    update_data = body.model_dump(exclude_unset=True)
    # 打版指令字段不进模板本体
    update_data.pop("version_changelog", None)

    if "evaluation_metrics" in update_data:
        update_data["evaluation_metrics"] = _parse_metrics(update_data["evaluation_metrics"])

    if "steps" in update_data and update_data["steps"]:
        update_data["steps"] = "\n".join(update_data["steps"])

    # 重新生成指令模板
    if any(k in update_data for k in ["scene", "scene_description", "evaluation_metrics", "max_score", "steps"]):
        config = {
            "scene": update_data.get("scene", template.scene),
            "scene_description": update_data.get("scene_description", template.scene_description or ""),
            "evaluation_metrics": update_data.get("evaluation_metrics", template.evaluation_metrics or []),
            "max_score": update_data.get("max_score", template.max_score),
            "steps": (update_data.get("steps", template.steps) or "").split("\n") if template.steps else [],
        }
        builder = InstructionBuilder()
        update_data["instruction_template"] = builder.build(config)

    for key, value in update_data.items():
        setattr(template, key, value)

    db.commit()
    db.refresh(template)
    return template


# ==================== 模板版本（M5 QE5-2：打版/列表/diff/恢复） ====================

def _get_template_or_404(db: Session, config_id: str) -> EvaluationTemplate:
    template = db.query(EvaluationTemplate).filter(EvaluationTemplate.id == config_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="配置不存在")
    return template


@router.post("/config/{config_id}/versions", summary="模板打版（以当前内容创建版本）",
             dependencies=[Depends(audited("CREATE", "template", id_param="config_id"))])
def create_template_version(
    config_id: str,
    body: CreateTemplateVersionRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """以模板当前完整内容打版（content 全量快照，版本不可变，可 diff/恢复/对照）"""
    ensure_permission(request.state.user, "templates")  # RBAC（QE4-3）：viewer 只读
    template = _get_template_or_404(db, config_id)

    from app.services.template_version_service import TemplateVersionService

    user = request.state.user or {}
    version = TemplateVersionService(db).create_version(
        template, changelog=body.changelog, created_by=user.get("user_code")
    )
    request.state.audit_resource = {
        "id": config_id,
        "detail": {"version_no": version["version_no"]},
    }
    return version


@router.get("/config/{config_id}/versions", summary="模板版本列表")
def list_template_versions(
    config_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """版本列表（新→旧，不含 content 大字段）"""
    _get_template_or_404(db, config_id)

    from app.services.template_version_service import TemplateVersionService

    return TemplateVersionService(db).list_versions(config_id)


# 注意路由顺序：diff 是静态段，必须先于 /versions/{version_no} 注册
@router.get("/config/{config_id}/versions/diff", summary="两模板版本字段级 diff")
def diff_template_versions(
    config_id: str,
    request: Request,
    from_version: int = Query(..., alias="from", description="起始版本号"),
    to_version: int = Query(..., alias="to", description="目标版本号"),
    db: Session = Depends(get_db),
):
    """字段级 diff：逐内容字段输出 from/to 对照（展示值超长截断，完整内容以版本为准）"""
    _get_template_or_404(db, config_id)

    from app.services.template_version_service import (
        TemplateVersionNotFound, TemplateVersionService,
    )

    try:
        return TemplateVersionService(db).diff_versions(config_id, from_version, to_version)
    except TemplateVersionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/config/{config_id}/versions/{version_no}", summary="模板版本详情")
def get_template_version(
    config_id: str,
    version_no: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """单版本详情（含完整 content 快照）"""
    _get_template_or_404(db, config_id)

    from app.services.template_version_service import (
        TemplateVersionNotFound, TemplateVersionService,
    )

    try:
        return TemplateVersionService(db).get_version(config_id, version_no)
    except TemplateVersionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/config/{config_id}/versions/{version_no}/restore", summary="恢复模板到指定版本",
             dependencies=[Depends(audited("UPDATE", "template", id_param="config_id"))])
def restore_template_version(
    config_id: str,
    version_no: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """把模板活内容恢复到指定版本（恢复前活内容自动留存进版本链，改前状态不丢）"""
    ensure_permission(request.state.user, "templates")  # RBAC（QE4-3）：viewer 只读
    template = _get_template_or_404(db, config_id)

    from app.services.template_version_service import (
        TemplateVersionNotFound, TemplateVersionService,
    )

    user = request.state.user or {}
    try:
        result = TemplateVersionService(db).restore_version(
            template, version_no, created_by=user.get("user_code")
        )
    except TemplateVersionNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    request.state.audit_resource = {
        "id": config_id,
        "detail": {"restored_version": version_no,
                   "pre_restore_version": result["pre_restore_version"]},
    }
    return result


@router.delete("/config/{config_id}", summary="删除配置",
               dependencies=[Depends(audited("DELETE", "template", id_param="config_id"))])
def delete_instruction_config(
    config_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """删除评测指令配置"""
    ensure_permission(request.state.user, "templates")  # RBAC（QE4-3）：viewer 只读
    template = db.query(EvaluationTemplate).filter(EvaluationTemplate.id == config_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="配置不存在")

    # 检查是否有关联的评测任务
    from app.models.task_models import EvaluationTask
    related_tasks = db.query(EvaluationTask).filter(
        EvaluationTask.template_id == config_id,
        EvaluationTask.is_active == True
    ).all()

    if related_tasks:
        task_names = [task.name for task in related_tasks]
        raise HTTPException(
            status_code=400,
            detail=f"该评测模板被以下评测任务使用，无法删除：{', '.join(task_names)}。请先替换或删除这些评测任务。"
        )

    db.delete(template)
    db.commit()
    return {"message": "删除成功"}


@router.post("/configs/migrate-legacy-all", response_model=MigrateLegacyAllResponse, summary="批量迁移旧版评测模板",
             dependencies=[Depends(ensure_super_admin), Depends(audited("UPDATE", "template"))])
def migrate_legacy_templates(
    db: Session = Depends(get_db),
):
    """批量迁移旧版评测模板（幂等，仅规则1）：steps（Text）合并进 score_details 后置 None"""
    from app.services.config_migration_service import ConfigMigrationService
    try:
        return ConfigMigrationService(db).migrate_all_templates()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/preview", response_model=PreviewResponse, summary="预览指令")
def preview_instruction(
    body: PreviewRequest,
):
    """预览组装后的指令"""
    builder = InstructionBuilder()

    config = {
        "scene": body.scene,
        "scene_description": body.scene_description or "",
        "evaluation_metrics": _parse_metrics(body.evaluation_metrics),
        "max_score": body.max_score,
        "steps": body.steps,
    }

    return builder.preview(config)


@router.post("/validate", response_model=ValidateResponse, summary="验证配置")
def validate_instruction(
    body: ValidateRequest,
):
    """验证配置有效性"""
    builder = InstructionBuilder()

    config = {
        "scene": body.scene,
        "scene_description": body.scene_description or "",
        "evaluation_metrics": _parse_metrics(body.evaluation_metrics),
        "max_score": body.max_score,
    }

    return builder.validate(config)


@router.get("/scenes", response_model=List[PresetSceneResponse], summary="获取预设场景")
def get_preset_scenes():
    """获取预设场景列表"""
    return TemplateService.get_preset_scenes()


@router.get("/default", response_model=PreviewResponse, summary="获取默认模板")
def get_default_template():
    """获取默认模板"""
    builder = InstructionBuilder()
    config = TemplateService.get_default_template()
    return builder.preview(config)
