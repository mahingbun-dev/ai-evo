"""
数据合成 API
"""
from fastapi import APIRouter, HTTPException, Depends
from app.services.access_control import ensure_super_admin
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from app.services.deepeval_synthesizer import (
    synthesize_from_contexts,
    synthesize_from_docs,
    synthesize_from_scratch,
)

router = APIRouter(prefix="/dataset-synthesis", tags=["Dataset Synthesis"], dependencies=[Depends(ensure_super_admin)])


class LLMConfigParam(BaseModel):
    """LLM 配置参数（可选）"""
    provider: str = Field("openai", description="模型提供商：openai/anthropic/custom_http")
    model_type: str = Field("gpt-3.5-turbo", description="模型名称")
    endpoint: Optional[str] = Field(None, description="API 端点 URL")
    api_key: Optional[str] = Field(None, description="API Key")
    temperature: float = Field(0.7, description="温度参数")
    max_tokens: int = Field(2000, description="最大 token 数")


class SynthesizeFromContextsRequest(BaseModel):
    """从上下文生成请求"""
    contexts: List[List[str]] = Field(..., description="上下文列表")
    num_questions: int = Field(5, description="每个上下文生成的问题数量")
    llm_config: Optional[LLMConfigParam] = Field(default=None, description="LLM 配置（可选，不传则使用环境变量配置）")


class SynthesizeFromScratchRequest(BaseModel):
    """从零生成请求"""
    topic: str = Field(..., description="主题")
    num_questions: int = Field(5, description="生成的问题数量")
    llm_config: Optional[LLMConfigParam] = Field(default=None, description="LLM 配置（可选，不传则使用环境变量配置）")


class SynthesisResponse(BaseModel):
    """合成响应"""
    generated_count: int
    items: List[Dict[str, Any]]


@router.post("/from-contexts", response_model=SynthesisResponse)
async def api_synthesize_from_contexts(request: SynthesizeFromContextsRequest):
    """从上下文生成测试数据"""
    try:
        model_config = request.llm_config.model_dump() if request.llm_config else None
        items = await synthesize_from_contexts(
            contexts=request.contexts,
            num_questions=request.num_questions,
            model_config=model_config,
        )
        return SynthesisResponse(
            generated_count=len(items),
            items=items,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/from-scratch", response_model=SynthesisResponse)
async def api_synthesize_from_scratch(request: SynthesizeFromScratchRequest):
    """从零开始生成测试数据"""
    try:
        model_config = request.llm_config.model_dump() if request.llm_config else None
        items = await synthesize_from_scratch(
            topic=request.topic,
            num_questions=request.num_questions,
            model_config=model_config,
        )
        return SynthesisResponse(
            generated_count=len(items),
            items=items,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
