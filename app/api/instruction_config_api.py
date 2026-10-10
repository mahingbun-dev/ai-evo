"""
评测指令配置API
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
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
    weight: str = Field("中", description="权重：high/medium/low")
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


@router.post("/config", response_model=InstructionConfigResponse, summary="创建评测指令配置")
def create_instruction_config(
    body: CreateInstructionConfigRequest,
    db: Session = Depends(get_db),
):
    """创建新的评测指令配置"""
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


@router.put("/config/{config_id}", response_model=InstructionConfigResponse, summary="更新配置")
def update_instruction_config(
    config_id: str,
    body: UpdateInstructionConfigRequest,
    db: Session = Depends(get_db),
):
    """更新评测指令配置"""
    template = db.query(EvaluationTemplate).filter(EvaluationTemplate.id == config_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="配置不存在")

    # 更新字段
    update_data = body.model_dump(exclude_unset=True)

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


@router.delete("/config/{config_id}", summary="删除配置")
def delete_instruction_config(
    config_id: str,
    db: Session = Depends(get_db),
):
    """删除评测指令配置"""
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


@router.post("/configs/migrate-legacy-all", response_model=MigrateLegacyAllResponse, summary="批量迁移旧版评测模板")
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
