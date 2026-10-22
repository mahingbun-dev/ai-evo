"""安全评测 API（超管直调，已废弃）

M3 QE3-2 deepeval 端点收敛：bias/toxicity 等指标已并入 safety 评测类型的
``safety_config.metrics`` 开关（双通道红队引擎的逐指标增强通道，见
app/services/redteam/engine.py）；本直调端点保留一个迭代后移除。

调用方迁移指引：
- 安全评测请创建 ``evaluation_type="safety"`` 任务（前端「安全评测」或
  ``POST /api/v1/evaluation/tasks``），在 safety_config.metrics 中选择
  bias/toxicity 等指标；
- 所有响应带 ``Deprecation: true`` 与 ``Link`` 迁移说明头。
"""
from dataclasses import asdict
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from app.services.access_control import ensure_super_admin
from pydantic import BaseModel, Field
from typing import List, Optional
from app.services.safety_evaluator import SafetyEvaluator

router = APIRouter(prefix="/safety-evaluation", tags=["Safety Evaluation"], dependencies=[Depends(ensure_super_admin)])

#: 废弃公告（OpenAPI 描述与 Deprecation 响应头共用）
DEPRECATION_NOTICE = (
    "已废弃（M3 QE3-2）：bias/toxicity 等指标并入 safety 评测类型 "
    "safety_config.metrics 开关（POST /api/v1/evaluation/tasks 创建 "
    "evaluation_type=safety 任务使用），本直调端点将于下个迭代移除"
)

_evaluator = SafetyEvaluator()


def _mark_deprecated(request: Request, response: Response) -> None:
    """为废弃端点补标准告警头（RFC 8594 Deprecation + Sunset 语义）"""
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "deprecated"
    response.headers["Link"] = '</api/v1/evaluation/tasks>; rel="successor-version"'


class SafetyEvaluationRequest(BaseModel):
    """安全评测请求"""
    input_text: str = Field(..., description="用户输入")
    actual_output: str = Field(..., description="模型输出")
    metrics: Optional[List[str]] = Field(None, description="指定评测指标")
    threshold: float = Field(0.5, description="评测阈值")


class EvaluationResponse(BaseModel):
    """评测响应"""
    score: float
    reason: Optional[str] = None
    success: bool


@router.post("/evaluate", deprecated=True,
             summary=f"[{DEPRECATION_NOTICE.split('：')[0]}] 综合评测安全指标（直调，废弃）",
             description=f"{DEPRECATION_NOTICE}。")
async def evaluate_safety(request: SafetyEvaluationRequest, raw_request: Request, response: Response):
    """综合评测安全指标（遗留直调通道，废弃）"""
    _mark_deprecated(raw_request, response)
    try:
        result = await _evaluator.evaluate_all(
            input_text=request.input_text,
            actual_output=request.actual_output,
            threshold=request.threshold,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bias", response_model=EvaluationResponse, deprecated=True,
             summary="[已废弃] 评测偏见（直调）",
             description=f"{DEPRECATION_NOTICE}。")
async def evaluate_bias(request: SafetyEvaluationRequest, raw_request: Request, response: Response):
    """评测偏见（遗留直调通道，废弃）"""
    _mark_deprecated(raw_request, response)
    try:
        result = _evaluator.evaluate_bias(
            input_text=request.input_text,
            actual_output=request.actual_output,
            threshold=request.threshold,
        )
        return EvaluationResponse(**asdict(result))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/toxicity", response_model=EvaluationResponse, deprecated=True,
             summary="[已废弃] 评测毒性（直调）",
             description=f"{DEPRECATION_NOTICE}。")
async def evaluate_toxicity(request: SafetyEvaluationRequest, raw_request: Request, response: Response):
    """评测毒性（遗留直调通道，废弃）"""
    _mark_deprecated(raw_request, response)
    try:
        result = _evaluator.evaluate_toxicity(
            input_text=request.input_text,
            actual_output=request.actual_output,
            threshold=request.threshold,
        )
        return EvaluationResponse(**asdict(result))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
