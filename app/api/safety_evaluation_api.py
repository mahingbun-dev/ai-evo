"""
安全评测 API
"""
from dataclasses import asdict
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from app.services.safety_evaluator import SafetyEvaluator

router = APIRouter(prefix="/safety-evaluation", tags=["Safety Evaluation"])

_evaluator = SafetyEvaluator()


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


@router.post("/evaluate")
async def evaluate_safety(request: SafetyEvaluationRequest):
    """综合评测安全指标"""
    try:
        result = await _evaluator.evaluate_all(
            input_text=request.input_text,
            actual_output=request.actual_output,
            metrics=request.metrics,
            threshold=request.threshold,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bias", response_model=EvaluationResponse)
async def evaluate_bias(request: SafetyEvaluationRequest):
    """评测偏见"""
    try:
        result = _evaluator.evaluate_bias(
            input_text=request.input_text,
            actual_output=request.actual_output,
            threshold=request.threshold,
        )
        return EvaluationResponse(**asdict(result))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/toxicity", response_model=EvaluationResponse)
async def evaluate_toxicity(request: SafetyEvaluationRequest):
    """评测毒性"""
    try:
        result = _evaluator.evaluate_toxicity(
            input_text=request.input_text,
            actual_output=request.actual_output,
            threshold=request.threshold,
        )
        return EvaluationResponse(**asdict(result))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
