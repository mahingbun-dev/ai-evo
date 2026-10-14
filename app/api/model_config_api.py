"""
模型配置API
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState
import json
import re
import asyncio
import logging
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_serializer
from datetime import datetime
from app.db.session import get_db
from app.services.model_config_service import ModelConfigService, PresetModelService
from app.services.ws_auth import require_ws_user
from app.services.access_control import (
    AccessDeniedError,
    check_team_access,
    ensure_model_access,
    user_team_code,
)

# 配置日志
logger = logging.getLogger(__name__)


# ==================== Request Models ====================

class CreateModelRequest(BaseModel):
    name: str = Field(..., description="模型名称")
    provider: str = Field(..., description="模型提供商：openai/anthropic/custom_http")
    model_type: str = Field(..., description="具体模型名称（如 gpt-4、claude-3-opus-20240229）")
    model_role: str = Field("evaluation", description="模型角色：evaluation(待评测模型)/judge(裁判模型)")
    endpoint: Optional[str] = Field(None, description="API端点URL")
    api_key: Optional[str] = Field(None, description="API Key")
    auth_type: str = Field("none", description="鉴权类型：none/api_key/bearer/basic/oauth/custom_header/query")
    auth_config: Optional[Dict[str, Any]] = Field(default_factory=dict, description="鉴权配置")
    request_config: Optional[Dict[str, Any]] = Field(default_factory=dict, description="请求配置")
    response_parse_config: Optional[Dict[str, Any]] = Field(default_factory=dict, description="响应解析配置")
    supports_multimodal: bool = Field(False, description="是否支持多模态")
    multimodal_config: Optional[Dict[str, Any]] = Field(default_factory=dict, description="多模态配置")
    description: Optional[str] = Field(None, description="描述")
    created_by: Optional[str] = Field(None, description="创建人")


class UpdateModelRequest(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model_type: Optional[str] = None
    model_role: Optional[str] = None
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    auth_type: Optional[str] = None
    auth_config: Optional[Dict[str, Any]] = None
    request_config: Optional[Dict[str, Any]] = None
    response_parse_config: Optional[Dict[str, Any]] = None
    supports_multimodal: Optional[bool] = None
    multimodal_config: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class CreateJudgeModelRequest(BaseModel):
    name: str = Field(..., description="裁判模型名称")
    model_type: str = Field(..., description="模型类型")
    endpoint: Optional[str] = Field(None, description="API端点")
    api_key: Optional[str] = Field(None, description="API Key")
    temperature: int = Field(0, description="温度参数")
    max_tokens: int = Field(4096, description="最大token数")
    system_prompt: Optional[str] = Field(None, description="系统提示词")
    default_max_score: int = Field(10, description="默认最大分值")


class ParseCurlRequest(BaseModel):
    curl_command: str = Field(..., description="curl 命令字符串")


class ParseCurlResponse(BaseModel):
    endpoint: str = Field(..., description="API端点URL（已移除查询参数）")
    method: str = Field(..., description="HTTP方法：POST/GET/PUT/DELETE")
    headers: Dict[str, str] = Field(default_factory=dict, description="HTTP请求头")
    params: Dict[str, str] = Field(default_factory=dict, description="URL查询参数")
    body: Optional[Dict[str, Any]] = Field(None, description="请求体（JSON解析后）")
    auth_config: Dict[str, Any] = Field(default_factory=dict, description="鉴权配置")


# ==================== Response Models ====================

class ModelResponse(BaseModel):
    id: str
    name: str
    provider: str
    model_type: str
    model_role: str
    endpoint: Optional[str]
    auth_type: str
    supports_multimodal: bool
    description: Optional[str]
    role_description: Optional[str] = Field(None, description="模型角色说明：evaluation-待评测模型，judge-裁判模型")
    is_active: bool
    is_preset: bool
    created_at: datetime

    @field_serializer('created_at')
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat()

    class Config:
        from_attributes = True


class ModelDetailResponse(ModelResponse):
    api_key: Optional[str]
    auth_config: Dict[str, Any]
    request_config: Dict[str, Any]
    response_parse_config: Dict[str, Any]
    multimodal_config: Dict[str, Any]
    role_description: Optional[str] = Field(None, description="模型角色说明：evaluation-待评测模型，judge-裁判模型")

    @field_serializer('api_key')
    def mask_api_key(self, value: Optional[str]) -> Optional[str]:
        """对 API Key 做脱敏处理，只保留前3位和后3位

        W4 起掩码逻辑单一来源收敛在 config_snapshot.mask_api_key
        （创建任务配置快照复用同一实现），此处仅委托。
        """
        from app.services.config_snapshot import mask_api_key as _mask_api_key
        return _mask_api_key(value)


class JudgeModelResponse(BaseModel):
    id: str
    name: str
    model_type: str
    endpoint: Optional[str]
    temperature: int
    max_tokens: int
    default_max_score: int
    is_active: bool
    created_at: datetime

    @field_serializer('created_at')
    def serialize_created_at(self, value: datetime) -> str:
        return value.isoformat()

    class Config:
        from_attributes = True


class TestConnectionResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None
    response: Optional[str] = None
    request_details: Optional[List[Dict[str, Any]]] = Field(default=None, description="请求链详细执行信息")


class TestConnectionWithVariablesRequest(BaseModel):
    """测试模型连接（带运行时变量）"""
    variables: Optional[Dict[str, Any]] = Field(default_factory=dict, description="运行时变量")


class ModelVariablesResponse(BaseModel):
    """模型变量响应"""
    variables: List[str] = Field(default_factory=list, description="需要用户输入的变量列表")


class PresetModelResponse(BaseModel):
    id: str
    provider: str
    model_name: str
    display_name: str
    is_multimodal: bool
    description: Optional[str]

    class Config:
        from_attributes = True


# ==================== Router ====================

router = APIRouter(prefix="/models", tags=["模型配置"])


@router.post("", response_model=ModelResponse, summary="创建模型配置")
def create_model(
    request: Request,
    body: CreateModelRequest,
    db: Session = Depends(get_db),
):
    """创建新的模型配置（W19：team_code 盖创建人团队，空 org_code 按 DEFAULT）"""
    service = ModelConfigService(db)
    model = service.create_model(
        name=body.name,
        provider=body.provider,
        model_type=body.model_type,
        model_role=body.model_role,
        endpoint=body.endpoint,
        api_key=body.api_key,
        auth_type=body.auth_type,
        auth_config=body.auth_config,
        request_config=body.request_config,
        response_parse_config=body.response_parse_config,
        supports_multimodal=body.supports_multimodal,
        multimodal_config=body.multimodal_config,
        description=body.description,
        created_by=body.created_by,
        team_code=user_team_code(request.state.user),
    )
    return model


@router.get("", response_model=List[ModelResponse], summary="获取模型配置列表")
def get_models(
    provider: Optional[str] = Query(None, description="模型提供商过滤"),
    model_type: Optional[str] = Query(None, description="具体模型名称过滤"),
    model_role: Optional[str] = Query(None, description="模型角色过滤：evaluation/judge"),
    is_active: Optional[bool] = Query(None, description="是否启用"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """获取模型配置列表"""
    service = ModelConfigService(db)
    models = service.get_models(provider=provider, model_type=model_type, model_role=model_role, is_active=is_active, skip=skip, limit=limit)
    return models


@router.get("/{model_id}", response_model=ModelDetailResponse, summary="获取模型详情")
def get_model(
    model_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取模型配置详情（W19：团队收口——详情含 auth_config 等凭据载体）"""
    model = ensure_model_access(db, request.state.user, model_id)
    return model


@router.put("/{model_id}", response_model=ModelResponse, summary="更新模型配置")
def update_model(
    model_id: str,
    request: Request,
    body: UpdateModelRequest,
    db: Session = Depends(get_db),
):
    """更新模型配置（W19：团队收口）"""
    ensure_model_access(db, request.state.user, model_id)
    update_data = body.model_dump(exclude_unset=True)
    # 如果 api_key 为空或包含脱敏字符 '*'，说明用户未修改，不更新
    if 'api_key' in update_data:
        api_key_val = update_data['api_key']
        if not api_key_val or '*' in api_key_val:
            del update_data['api_key']
    service = ModelConfigService(db)
    model = service.update_model(model_id, **update_data)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    return model


@router.delete("/{model_id}", summary="删除模型配置")
def delete_model(
    model_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """删除模型配置（W19：团队收口）"""
    ensure_model_access(db, request.state.user, model_id)
    service = ModelConfigService(db)
    try:
        success = service.delete_model(model_id)
    except ValueError as e:
        # 有关联的评测任务，不能删除
        raise HTTPException(status_code=400, detail=str(e))
    if not success:
        raise HTTPException(status_code=404, detail="模型不存在")
    return {"message": "删除成功"}


@router.get("/{model_id}/variables", summary="获取模型请求链变量")
def get_model_variables(
    model_id: str,
    db: Session = Depends(get_db),
):
    """获取模型配置中需要动态输入的变量列表"""
    from app.services.request_chain import RequestChainExecutor

    service = ModelConfigService(db)
    config = service.get_model(model_id)

    if not config:
        raise HTTPException(status_code=404, detail="模型不存在")

    variables = RequestChainExecutor.extract_variables(
        config.request_config.get("chain_requests") if config.request_config else {},
        config.auth_config  # 传递 auth_config 以排除登录变量
    )
    return {"variables": variables}


@router.post("/{model_id}/test", response_model=TestConnectionResponse, summary="测试模型连接")
async def test_model_connection(
    model_id: str,
    request: Request,
    body: Optional[TestConnectionWithVariablesRequest] = None,
    db: Session = Depends(get_db),
):
    """测试模型连接是否正常，支持传入运行时变量（W19：团队收口，防凭据被探）"""
    ensure_model_access(db, request.state.user, model_id)
    service = ModelConfigService(db)
    # 正确处理可选 body 参数
    runtime_vars = None
    if body and body.variables:
        runtime_vars = body.variables
    result = await service.test_connection(model_id, runtime_vars)
    return result


@router.post("/parse-curl", response_model=ParseCurlResponse, summary="解析curl命令")
def parse_curl(
    body: ParseCurlRequest,
    db: Session = Depends(get_db),
):
    """解析 curl 命令并转换为模型配置结构"""
    service = ModelConfigService(db)
    result = service.parse_curl_command(body.curl_command)
    return result


# ==================== Preset Models ====================

@router.get("/presets", response_model=List[PresetModelResponse], summary="获取预置模型列表")
def get_preset_models(
    provider: Optional[str] = Query(None, description="提供商过滤"),
    db: Session = Depends(get_db),
):
    """获取预置模型列表"""
    service = PresetModelService(db)
    presets = service.get_presets(provider=provider)
    return presets


# ==================== Judge Models ====================

router_judge = APIRouter(prefix="/judge-models", tags=["裁判模型"])


@router_judge.post("", response_model=ModelResponse, summary="创建裁判模型配置")
def create_judge_model(
    request: Request,
    body: CreateModelRequest,
    db: Session = Depends(get_db),
):
    """创建裁判模型配置（使用ModelConfig表，model_role设为judge；W19：盖创建人团队）"""
    service = ModelConfigService(db)
    judge = service.create_model(
        name=body.name,
        provider=body.provider,
        model_type=body.model_type,
        model_role="judge",  # 强制设为裁判模型
        endpoint=body.endpoint,
        api_key=body.api_key,
        auth_type=body.auth_type,
        auth_config=body.auth_config,
        request_config=body.request_config,
        response_parse_config=body.response_parse_config,
        supports_multimodal=body.supports_multimodal,
        multimodal_config=body.multimodal_config,
        description=body.description,
        created_by=body.created_by,
        team_code=user_team_code(request.state.user),
    )
    return judge


@router_judge.get("", response_model=List[ModelResponse], summary="获取裁判模型列表")
def get_judge_models(
    is_active: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
):
    """获取裁判模型列表（从ModelConfig表通过model_role=judge过滤）"""
    service = ModelConfigService(db)
    judges = service.get_models(model_role="judge", is_active=is_active)
    return judges


@router_judge.get("/{judge_id}", response_model=ModelDetailResponse, summary="获取裁判模型详情")
def get_judge_model(
    judge_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """获取裁判模型详情（W19：团队收口）"""
    judge = ensure_model_access(db, request.state.user, judge_id)
    return judge


@router_judge.put("/{judge_id}", response_model=ModelResponse, summary="更新裁判模型配置")
def update_judge_model(
    judge_id: str,
    request: Request,
    body: UpdateModelRequest,
    db: Session = Depends(get_db),
):
    """更新裁判模型配置（W19：团队收口）"""
    ensure_model_access(db, request.state.user, judge_id)
    service = ModelConfigService(db)
    # 强制确保 model_role 为 judge
    update_data = body.model_dump(exclude_unset=True)
    update_data["model_role"] = "judge"
    # 如果 api_key 为空或包含脱敏字符 '*'，说明用户未修改，不更新
    if 'api_key' in update_data:
        api_key_val = update_data['api_key']
        if not api_key_val or '*' in api_key_val:
            del update_data['api_key']
    judge = service.update_model(judge_id, **update_data)
    if not judge:
        raise HTTPException(status_code=404, detail="裁判模型不存在")
    return judge


@router_judge.delete("/{judge_id}", summary="删除裁判模型配置")
def delete_judge_model(
    judge_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """删除裁判模型配置（W19：团队收口）"""
    ensure_model_access(db, request.state.user, judge_id)
    service = ModelConfigService(db)
    try:
        success = service.delete_model(judge_id)
    except ValueError as e:
        # 有关联的评测任务，不能删除
        raise HTTPException(status_code=400, detail=str(e))
    if not success:
        raise HTTPException(status_code=404, detail="裁判模型不存在")
    return {"message": "删除成功"}


# ==================== WebSocket 测试接口 ====================

@router.websocket("/ws/test/{model_id}")
async def websocket_test_model(
    websocket: WebSocket,
    model_id: str,
):
    """
    WebSocket 测试模型连接
    支持流式和非流式输出，通过 WebSocket 实时推送结果

    鉴权：accept 之前完成 SSO Cookie 验证，未认证一律 close(4401)。
    W19：补模型归属校验（4404 不存在 / 4403 跨团队）——测连会用模型
    凭据发起真实请求，不允许跨团队探测。
    """
    # 鉴权（必须在 accept 之前）：失败时 require_ws_user 已发送 close(4401)
    user = await require_ws_user(websocket)
    if user is None:
        return

    # W19：模型归属校验（与 HTTP 侧 ensure_model_access 同一判定内核）
    from app.models.model_config_models import ModelConfig
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        model = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
    if model is None:
        await websocket.close(code=4404)
        return
    try:
        check_team_access(user, model.team_code)
    except AccessDeniedError:
        logger.warning(
            f"[WS] 4403 - 跨团队模型测连被拒: model_id={model_id}, "
            f"model_team={model.team_code}, user_team={user.get('org_code')}"
        )
        await websocket.close(code=4403)
        return

    await websocket.accept()

    try:
        # 接收客户端发送的变量配置
        data = await websocket.receive_text()
        request_data = json.loads(data) if data else {}
        runtime_vars = request_data.get("variables", {})

        # 处理 runtime_variables 中的 { value, type } 格式
        processed_runtime_vars = None
        if runtime_vars:
            processed_runtime_vars = {}
            for key, val in runtime_vars.items():
                if isinstance(val, dict) and 'value' in val and 'type' in val:
                    var_type = val.get('type', 'string')
                    var_value = val.get('value', '')
                    if var_type == 'number':
                        try:
                            processed_runtime_vars[key] = float(var_value) if '.' in var_value else int(var_value)
                        except (ValueError, TypeError):
                            processed_runtime_vars[key] = 0
                    elif var_type == 'boolean':
                        processed_runtime_vars[key] = str(var_value).lower() in ('true', '1', 'yes')
                    else:
                        processed_runtime_vars[key] = str(var_value)
                else:
                    processed_runtime_vars[key] = val

        # 获取数据库会话
        db = next(get_db())
        service = ModelConfigService(db)

        # 获取模型配置
        config = service.get_model(model_id)
        if not config:
            await websocket.send_json({
                "type": "error",
                "content": "模型不存在"
            })
            await websocket.close()
            return

        # 构建配置字典
        config_dict = {
            "provider": config.provider,
            "model_type": config.model_type,
            "api_key": config.api_key,
            "endpoint": config.endpoint,
            "auth_type": config.auth_type,
            "auth_config": config.auth_config,
            "request_config": config.request_config,
            "response_parse_config": config.response_parse_config,
            "model_id": config.id,
        }

        # 发送连接开始消息
        await websocket.send_json({
            "type": "start",
            "content": "开始测试模型连接..."
        })

        # 根据 provider 类型选择不同的调用方式
        provider = (config.provider or config.model_type or "").lower()

        if provider == "openai":
            # OpenAI 调用
            from app.services.model_caller import ModelCaller
            messages = [{"role": "user", "content": "Hello"}]
            result = await ModelCaller.call(messages, config_dict, processed_runtime_vars, debug=True)

            if result.get("success"):
                await websocket.send_json({
                    "type": "success",
                    "content": "连接成功"
                })
                await websocket.send_json({
                    "type": "response",
                    "content": result.get("content", "")[:500]
                })
            else:
                await websocket.send_json({
                    "type": "error",
                    "content": result.get("error", "未知错误")
                })

        elif provider == "anthropic":
            # Anthropic 调用
            from app.services.model_caller import ModelCaller
            messages = [{"role": "user", "content": "Hello"}]
            result = await ModelCaller.call(messages, config_dict, processed_runtime_vars, debug=True)

            if result.get("success"):
                await websocket.send_json({
                    "type": "success",
                    "content": "连接成功"
                })
                await websocket.send_json({
                    "type": "response",
                    "content": result.get("content", "")[:500]
                })
            else:
                await websocket.send_json({
                    "type": "error",
                    "content": result.get("error", "未知错误")
                })

        else:
            # 自定义 HTTP 请求（支持请求链和流式）
            from app.services.model_caller import ModelCaller

            request_config = config.request_config or {}
            chain_config = request_config.get("chain_requests", {})

            if chain_config.get("enabled"):
                # 使用请求链
                from app.services.request_chain import RequestChainExecutor
                executor = RequestChainExecutor(chain_config)

                input_data = {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "endpoint": config.endpoint,
                    "model_type": config.model_type,
                }

                # 处理认证
                from app.services.auth_handler import AuthHandler
                auth_type = config.auth_type or "api_key"
                auth_config = config.auth_config or {}
                response_parse_config = config.response_parse_config or {}

                logger.info(f"auth_config完整配置: {auth_config}")
                logger.info(f"response_parse_config完整配置: {response_parse_config}")

                auth_info = AuthHandler.prepare_auth(auth_type, auth_config, response_parse_config, config.id)
                logger.info(f"鉴权类型: {auth_type}")
                logger.info(f"Auth info: {auth_info}")
                if auth_info.get("headers"):
                    logger.info(f"Auth info headers: {auth_info['headers']}")
                    for key, value in auth_info["headers"].items():
                        logger.info(f"处理 auth header: {key} = {value}")
                        # 将所有 auth 相关的 header 值添加到变量中，供请求链中的 {{token}} 等占位符使用
                        if key.lower() == "authorization":
                            input_data["authorization"] = value
                        elif key.lower() == "cookie":
                            # Cookie header 需要完整值（形如 COOKIE_NAME=value），使用 cookie 作为变量名
                            input_data["cookie"] = value
                        else:
                            # 其他 header 使用原始名称作为变量名（去掉空格和特殊字符）
                            var_name = re.sub(r'[^a-zA-Z0-9_]', '_', key.lower())
                            input_data[var_name] = value
                logger.info(f"添加 auth 变量后的 input_data keys: {list(input_data.keys())}")

                # 定义请求链回调函数，实现实时输出
                async def request_callback(result: Optional[Dict[str, Any]], request_info: Dict[str, Any]) -> bool:
                    """每个请求执行完成后的回调，实时发送WebSocket消息"""
                    try:
                        if result is None:
                            # 请求开始前，发送请求开始信息
                            await websocket.send_json({
                                "type": "request_start",
                                "request_index": request_info.get("order", 0),
                                "name": request_info.get("name", ""),
                                "url": request_info.get("url", ""),
                                "method": request_info.get("method", "POST"),
                                "headers": request_info.get("headers", {}),
                                "vars_before": _serialize_vars(request_info.get("vars_before", {})),
                            })
                        elif isinstance(result, dict) and result.get("type") == "stream":
                            # 流式响应块
                            chunk = result.get("chunk", {})
                            await websocket.send_json({
                                "type": "stream_chunk",
                                "event": chunk.get("event", ""),
                                "data": chunk.get("data"),
                            })
                        else:
                            # 请求完成后，发送响应详情
                            response_data = result if isinstance(result, dict) else {}
                            await websocket.send_json({
                                "type": "request_detail",
                                "request_index": request_info.get("order", 0),
                                "name": request_info.get("name", ""),
                                "url": request_info.get("url", ""),
                                "method": request_info.get("method", "POST"),
                                "headers": request_info.get("headers", {}),
                                "request_params": request_info.get("request_params", {}),
                                "vars_before": _serialize_vars(request_info.get("vars_before", {})),
                                "request_body": request_info.get("request_body", {}),
                                "response": _serialize_response(response_data),
                                "vars_after": _serialize_vars(request_info.get("vars_after", {})),
                                "output_var": request_info.get("output_var", ""),
                                "output_value": str(request_info.get("output_value", ""))[:200] if request_info.get("output_value") else None
                            })
                    except Exception as cb_err:
                        logger.error(f"请求链回调发送失败: {cb_err}")
                    return True  # 继续执行

                # 执行请求链
                logger.info(f"开始执行请求链测试 | model_id={model_id} | runtime_vars={processed_runtime_vars}")
                try:
                    result = await executor.execute(input_data, config_dict, processed_runtime_vars, debug=True, request_callback=request_callback)
                    logger.info(f"请求链执行完成 | model_id={model_id} | success={result.get('success')}")
                except Exception as chain_err:
                    import traceback
                    logger.error(
                        f"请求链执行失败 | model_id={model_id} | "
                        f"错误类型: {type(chain_err).__name__} | "
                        f"错误信息: {str(chain_err)} | "
                        f"堆栈跟踪:\n{traceback.format_exc()}"
                    )
                    raise

                # 请求链执行完成（每个请求的详情已在回调中实时发送）

                if result.get("success"):
                    await websocket.send_json({
                        "type": "success",
                        "content": "连接成功"
                    })
                    await websocket.send_json({
                        "type": "response",
                        "content": result.get("content", "")[:500]
                    })
                else:
                    error_content = result.get("error", "未知错误")
                    logger.error(f"请求链测试失败 | model_id={model_id} | 错误: {error_content}")
                    await websocket.send_json({
                        "type": "error",
                        "content": error_content
                    })
            else:
                # 非请求链模式
                messages = [{"role": "user", "content": "Hello"}]
                result = await ModelCaller.call(messages, config_dict, processed_runtime_vars, debug=True)

                if result.get("success"):
                    await websocket.send_json({
                        "type": "success",
                        "content": "连接成功"
                    })
                    await websocket.send_json({
                        "type": "response",
                        "content": result.get("content", "")[:500]
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "content": result.get("error", "未知错误")
                    })

        # 发送完成消息
        await websocket.send_json({
            "type": "complete",
            "content": "测试完成"
        })

    except WebSocketDisconnect:
        logger.info(f"WebSocket 断开连接: model_id={model_id}")
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_trace = traceback.format_exc()

        # 记录详细的错误信息
        logger.error(
            f"模型测试失败 | model_id={model_id} | "
            f"错误类型: {type(e).__name__} | "
            f"错误信息: {error_msg} | "
            f"堆栈跟踪:\n{error_trace}"
        )

        try:
            await websocket.send_json({
                "type": "error",
                "content": f"测试失败: {error_msg}"
            })
        except Exception as ws_err:
            logger.error(f"发送错误消息到 WebSocket 失败: {str(ws_err)}")
    finally:
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close()
        except Exception as close_err:
            logger.warning(f"关闭 WebSocket 连接失败: {str(close_err)}")


def _serialize_vars(vars_dict: Dict[str, Any]) -> Dict[str, Any]:
    """序列化变量字典，将复杂对象转换为字符串"""
    result = {}
    for k, v in vars_dict.items():
        if isinstance(v, (str, int, float, bool)):
            result[k] = v
        else:
            result[k] = str(v)[:100] if v else ""
    return result


def _serialize_response(response: Any) -> Dict[str, Any]:
    """
    序列化响应对象，避免循环引用

    Args:
        response: 响应对象（可能是 dict 或其他类型）

    Returns:
        可序列化的字典
    """
    if response is None:
        return {}

    # 如果已经是字典，提取关键字段
    if isinstance(response, dict):
        result = {}
        for key in ['success', 'content', 'error']:
            if key in response:
                val = response[key]
                # 处理 content 中的复杂对象
                if key == 'content' and val:
                    if isinstance(val, (dict, list)):
                        try:
                            import json
                            result[key] = json.dumps(val, ensure_ascii=False, default=str)[:1000]
                        except:
                            result[key] = str(val)[:1000]
                    else:
                        result[key] = str(val)[:1000]
                else:
                    result[key] = val
        return result

    # 其他类型直接转为字符串
    return {"content": str(response)[:500] if response else ""}
