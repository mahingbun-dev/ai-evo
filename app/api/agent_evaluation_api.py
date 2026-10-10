"""
Agent 评测 API
"""
from dataclasses import asdict
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from app.services.agent_evaluator import AgentEvaluator

router = APIRouter(prefix="/agent-evaluation", tags=["Agent Evaluation"])

_evaluator = AgentEvaluator()


class TaskCompletionRequest(BaseModel):
    """任务完成度评测请求"""
    task: str = Field(..., description="任务描述")
    input_text: str = Field(..., description="用户输入")
    actual_output: str = Field(..., description="模型输出")
    expected_output: Optional[str] = Field(None, description="期望输出")
    threshold: float = Field(0.5, description="评测阈值")


class ToolCorrectnessRequest(BaseModel):
    """工具正确性评测请求"""
    input_text: str = Field(..., description="用户输入")
    tools_called: List[Dict[str, Any]] = Field(..., description="调用的工具列表")
    expected_tools: List[Dict[str, Any]] = Field(..., description="期望的工具列表")
    threshold: float = Field(0.5, description="评测阈值")


class EvaluationResponse(BaseModel):
    """评测响应"""
    score: float
    reason: Optional[str] = None
    success: bool


@router.post("/task-completion", response_model=EvaluationResponse)
async def evaluate_task_completion(request: TaskCompletionRequest):
    """评测任务完成度"""
    try:
        result = _evaluator.evaluate_task_completion(
            task=request.task,
            input_text=request.input_text,
            actual_output=request.actual_output,
            expected_output=request.expected_output,
            threshold=request.threshold,
        )
        return EvaluationResponse(**asdict(result))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tool-correctness", response_model=EvaluationResponse)
async def evaluate_tool_correctness(request: ToolCorrectnessRequest):
    """评测工具调用正确性"""
    try:
        result = _evaluator.evaluate_tool_correctness(
            input_text=request.input_text,
            tools_called=request.tools_called,
            expected_tools=request.expected_tools,
            threshold=request.threshold,
        )
        return EvaluationResponse(**asdict(result))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
