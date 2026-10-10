"""
裁判配置 API
提供裁判提示词模板获取、预览渲染和变量校验接口
"""
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException

from app.services.judge_validation_service import JudgeValidationService
from app.services.judge_variable_resolver import JudgeVariableResolver, EvaluationTarget
from app.services.prompt_builder import PromptBuilder


router = APIRouter(prefix="/judge-config")


class EvaluationTargetRequest(BaseModel):
    """评测目标请求模型"""
    id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    name: str = Field(..., description="字段名称")
    source: str = Field(..., description="来源类型：model_output / chain_result / dataset_field / manual_input")
    source_config: Dict[str, Any] = Field(default_factory=dict)
    required: bool = Field(default=True)
    display_name: str = Field(..., description="前端展示名称")


class PreviewRequest(BaseModel):
    """预览请求模型"""
    system_prompt_template: str = Field(..., description="System prompt Jinja2 模板")
    user_prompt_template: str = Field(..., description="User prompt Jinja2 模板")
    targets: List[EvaluationTargetRequest] = Field(..., description="评测目标列表")
    dataset_item: Dict[str, Any] = Field(default_factory=dict, description="数据集项")
    model_result: Dict[str, Any] = Field(default_factory=dict, description="模型结果")
    # PromptBuilder 所需配置字段
    scene_desc: str = Field(default="", description="场景描述")
    evaluation_metrics: List[Dict[str, Any]] = Field(default_factory=list, description="评测维度配置")
    max_score: int = Field(default=10, description="最大分值")
    score_details: str = Field(default="", description="评分标准")
    steps: List[str] = Field(default_factory=list, description="评分步骤")
    judge_variable_config: Any = Field(default=None, description="裁判变量配置，支持字典或列表格式")


class ValidateRequest(BaseModel):
    """校验请求模型"""
    targets: List[EvaluationTargetRequest] = Field(..., description="评测目标列表")
    dataset_item: Dict[str, Any] = Field(default_factory=dict, description="数据集项")
    model_result: Dict[str, Any] = Field(default_factory=dict, description="模型结果")


class ValidationResult(BaseModel):
    """校验结果项"""
    target_id: str
    target_name: str
    status: str  # ok / error / warning
    message: str
    content: str = ""


class ValidationResponse(BaseModel):
    """校验响应模型"""
    is_valid: bool
    validation_results: List[ValidationResult]


class PreviewResponse(BaseModel):
    """预览响应模型"""
    rendered_system_prompt: str
    rendered_user_prompt: str
    validation_results: Optional[List[ValidationResult]] = None
    is_valid: bool = True


class TemplateResponse(BaseModel):
    """模板响应模型"""
    system_prompt_template: str = Field(
        default="你是一个公正的评分裁判。请根据以下信息对模型输出进行评分：\n{% for target in targets %}\n{{ target.name }}: {{ target.content }}\n{% endfor %}"
    )
    user_prompt_template: str = Field(
        default="{% for target in targets %}\n{{ target.name }}: {{ target.content }}\n{% endfor %}\n请对以上内容进行评分，返回 JSON 格式的结果。"
    )
    evaluation_targets: List[Dict[str, Any]] = Field(default_factory=list)


@router.get("/template", response_model=TemplateResponse)
async def get_template():
    """
    获取默认裁判提示词模板结构

    Returns:
        包含默认模板的响应
    """
    return TemplateResponse()


@router.post("/preview", response_model=PreviewResponse)
async def preview_prompt(request: PreviewRequest):
    """
    预览渲染后的提示词

    Args:
        request: 预览请求，包含模板和目标数据

    Returns:
        渲染后的提示词和校验结果
    """
    try:
        # 解析评测目标
        targets = [EvaluationTarget(**t.model_dump()) for t in request.targets]

        # 为预览生成模拟的 chain_results（如果有 chain_result 类型的目标）
        preview_chain_results = _build_preview_chain_results(targets)

        # 解析目标内容（用于校验）
        resolver = JudgeVariableResolver(
            task_config={"judge_variable_config": request.judge_variable_config},
            dataset_item=request.dataset_item,
            model_result=request.model_result,
            chain_results=preview_chain_results
        )
        resolved_targets = resolver.resolve_targets(targets)

        # 校验
        validation_service = JudgeValidationService()
        is_valid, validation_results = validation_service.validate_targets(resolved_targets, validation_enabled=True)

        # 使用 PromptBuilder 组装提示词
        # 构建与任务配置结构相同的字典
        task_config = {
            "judge_prompt": request.system_prompt_template,
            "evaluation_scene": "评测",
            "scene_desc": request.scene_desc,
            "evaluation_metrics": request.evaluation_metrics,
            "max_score": request.max_score,
            "score_details": request.score_details,
            "steps": request.steps,
            "judge_variable_config": request.judge_variable_config,
            "runtime_variables": {},
            "evaluation_targets": [t.model_dump() for t in request.targets],
        }

        # 创建 PromptBuilder 实例
        builder = PromptBuilder.from_config(task_config)

        # 构建 payload（包含数据集项和模型输出）
        # PromptBuilder expects payload to have _dataset_item nested
        payload = {"_dataset_item": request.dataset_item, **request.dataset_item}
        model_output = request.model_result.get("content", "") if request.model_result else ""

        # 调用 PromptBuilder 构建提示词，传入预览用的 chain_results
        system_content, user_content = builder.build_judge_prompts(
            payload=payload,
            model_output=model_output,
            chain_results=preview_chain_results,
        )

        # 格式化校验结果
        formatted_validation = [
            ValidationResult(
                target_id=r["target_id"],
                target_name=r["target_name"],
                status=r["status"],
                message=r["message"],
                content=r.get("content", "")
            )
            for r in validation_results
        ]

        return PreviewResponse(
            rendered_system_prompt=system_content.strip(),
            rendered_user_prompt=user_content.strip(),
            validation_results=formatted_validation,
            is_valid=is_valid
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预览渲染失败: {str(e)}")


def _build_preview_chain_results(targets: List[EvaluationTarget]) -> List[Dict[str, Any]]:
    """为预览构建模拟的 chain_results

    当评测目标来源是 chain_result 时，预览需要模拟数据来展示效果。
    注意：chain_index 是数组索引（从0开始），需要创建从0到最大索引的连续数组。

    Args:
        targets: 评测目标列表

    Returns:
        模拟的 chain_results 列表
    """
    # 收集所有需要的 chain_index
    chain_indices = set()
    for target in targets:
        if target.source == "chain_result":
            chain_index = target.source_config.get("chain_index")
            if chain_index is not None:
                chain_indices.add(int(chain_index))

    if not chain_indices:
        return []

    # 创建从 0 到最大索引的连续数组（因为 chain_index 是数组索引）
    max_index = max(chain_indices)
    preview_chain_results = []
    for idx in range(max_index + 1):
        preview_chain_results.append({
            "chain_index": idx,
            "name": f"请求{idx + 1}",
            "content": f"【预览模拟数据】这是请求{idx + 1}的响应内容示例",
            "variables": {
                "content": f"【预览模拟数据】这是请求{idx + 1}的响应内容示例",
                "response": f"【预览模拟数据】这是请求{idx + 1}的响应内容示例",
            }
        })

    return preview_chain_results


@router.post("/validate", response_model=ValidationResponse)
async def validate_config(request: ValidateRequest):
    """
    校验变量配置是否完整

    Args:
        request: 校验请求

    Returns:
        校验结果
    """
    try:
        # 解析评测目标
        targets = [EvaluationTarget(**t.model_dump()) for t in request.targets]

        # 解析目标内容
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item=request.dataset_item,
            model_result=request.model_result,
            chain_results=[]
        )
        resolved_targets = resolver.resolve_targets(targets)

        # 校验
        validation_service = JudgeValidationService()
        is_valid, validation_results = validation_service.validate_targets(resolved_targets, validation_enabled=True)

        # 格式化校验结果
        formatted_results = [
            ValidationResult(
                target_id=r["target_id"],
                target_name=r["target_name"],
                status=r["status"],
                message=r["message"],
                content=r.get("content", "")
            )
            for r in validation_results
        ]

        return ValidationResponse(
            is_valid=is_valid,
            validation_results=formatted_results
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"校验失败: {str(e)}")
