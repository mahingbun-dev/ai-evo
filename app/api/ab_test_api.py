"""
A/B 测试 API
复用【模型配置】-【测试】的完整调用能力，通过 asyncio.gather 并行执行两个模型调用。
"""
import json
import re
import asyncio
import time
import logging
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query, Depends
from fastapi.websockets import WebSocketState
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.db.session import get_db, SessionLocal
from app.services.ab_test_service import ABTestService
from app.services.model_config_service import ModelConfigService
# 复用现有序列化工具
from app.api.model_config_api import _serialize_vars, _serialize_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ab-test", tags=["A/B测试"])


# ==================== Pydantic 请求/响应模型 ====================

class SaveSessionRequest(BaseModel):
    model_a_id: str
    model_b_id: str
    model_a_name: Optional[str] = None
    model_b_name: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None
    result_a: Optional[Dict[str, Any]] = None
    result_b: Optional[Dict[str, Any]] = None
    status: str = "completed"
    duration_a: Optional[float] = None
    duration_b: Optional[float] = None
    name: Optional[str] = None


# ==================== 工具函数 ====================

def _process_runtime_vars(runtime_vars: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将 {value, type} 格式的变量转换为实际值（复用 model_config_api 中的逻辑）"""
    if not runtime_vars:
        return None
    processed = {}
    for key, val in runtime_vars.items():
        if isinstance(val, dict) and 'value' in val and 'type' in val:
            var_type = val.get('type', 'string')
            var_value = val.get('value', '')
            if var_type == 'number':
                try:
                    processed[key] = float(var_value) if '.' in str(var_value) else int(var_value)
                except (ValueError, TypeError):
                    processed[key] = 0
            elif var_type == 'boolean':
                processed[key] = str(var_value).lower() in ('true', '1', 'yes')
            else:
                processed[key] = str(var_value)
        else:
            processed[key] = val
    return processed


async def _run_model(
    model_id: str,
    side: str,
    websocket: WebSocket,
    processed_vars: Optional[Dict[str, Any]],
    db: Session,
    ws_lock: asyncio.Lock = None,
) -> Dict[str, Any]:
    """
    在单侧（A 或 B）执行模型调用，实时通过 WebSocket 推流。
    完全复用 model_config_api.websocket_test_model 中的代码路径。
    返回 {success, content, request_details, duration}。
    ws_lock: 用于并发 send_json 时的互斥锁，避免 WebSocket 帧交织。
    """
    start_time = time.time()
    request_details = []

    async def send(msg: dict):
        if ws_lock:
            async with ws_lock:
                await websocket.send_json(msg)
        else:
            await websocket.send_json(msg)

    await send({"type": "start", "side": side, "content": f"开始测试模型{side}..."})

    service = ModelConfigService(db)
    config = service.get_model(model_id)

    if not config:
        await send({"type": "error", "side": side, "content": "模型不存在"})
        duration = time.time() - start_time
        await send({"type": "side_complete", "side": side, "duration": round(duration, 2), "success": False})
        return {"success": False, "content": "", "request_details": [], "duration": duration}

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

    provider = (config.provider or config.model_type or "").lower()
    result = {"success": False, "content": "", "error": "未执行"}

    try:
        if provider in ("openai", "anthropic"):
            from app.services.model_caller import ModelCaller
            messages = [{"role": "user", "content": "Hello"}]
            result = await ModelCaller.call(messages, config_dict, processed_vars, debug=True)

        else:
            # custom_http — 支持请求链
            from app.services.model_caller import ModelCaller

            request_config = config.request_config or {}
            chain_config = request_config.get("chain_requests", {})

            if chain_config.get("enabled"):
                from app.services.request_chain import RequestChainExecutor
                from app.services.auth_handler import AuthHandler

                executor = RequestChainExecutor(chain_config)
                input_data: Dict[str, Any] = {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "endpoint": config.endpoint,
                    "model_type": config.model_type,
                }

                # 处理认证（与 model_config_api 中完全相同的逻辑）
                auth_type = config.auth_type or "api_key"
                auth_config = config.auth_config or {}
                response_parse_config = config.response_parse_config or {}
                auth_info = AuthHandler.prepare_auth(auth_type, auth_config, response_parse_config, config.id)

                if auth_info.get("headers"):
                    for key, value in auth_info["headers"].items():
                        if key.lower() == "authorization":
                            input_data["token"] = value[7:] if value.startswith("Bearer ") else value
                        elif key.lower() == "cookie":
                            input_data["cookie"] = value
                        else:
                            var_name = re.sub(r'[^a-zA-Z0-9_]', '_', key.lower())
                            input_data[var_name] = value

                # 定义请求链回调（带 side 标识）
                async def request_callback(
                    cb_result: Optional[Dict[str, Any]],
                    request_info: Dict[str, Any],
                ) -> bool:
                    try:
                        if cb_result is None:
                            msg = {
                                "type": "request_start",
                                "side": side,
                                "request_index": request_info.get("order", 0),
                                "name": request_info.get("name", ""),
                                "url": request_info.get("url", ""),
                                "method": request_info.get("method", "POST"),
                                "headers": request_info.get("headers", {}),
                                "vars_before": _serialize_vars(request_info.get("vars_before", {})),
                            }
                            await send(msg)
                        elif isinstance(cb_result, dict) and cb_result.get("type") == "stream":
                            chunk = cb_result.get("chunk", {})
                            await send({
                                "type": "stream_chunk",
                                "side": side,
                                "event": chunk.get("event", ""),
                                "data": chunk.get("data"),
                            })
                        else:
                            response_data = cb_result if isinstance(cb_result, dict) else {}
                            detail = {
                                "type": "request_detail",
                                "side": side,
                                "request_index": request_info.get("order", 0),
                                "name": request_info.get("name", ""),
                                "url": request_info.get("url", ""),
                                "method": request_info.get("method", "POST"),
                                "headers": request_info.get("headers", {}),
                                "vars_before": _serialize_vars(request_info.get("vars_before", {})),
                                "request_body": request_info.get("request_body", {}),
                                "response": _serialize_response(response_data),
                                "vars_after": _serialize_vars(request_info.get("vars_after", {})),
                                "output_var": request_info.get("output_var", ""),
                                "output_value": str(request_info.get("output_value", ""))[:200]
                                if request_info.get("output_value") else None,
                            }
                            await send(detail)
                            request_details.append(detail)
                    except Exception as cb_err:
                        logger.error(f"[AB-{side}] 请求链回调发送失败: {cb_err}")
                    return True

                result = await executor.execute(
                    input_data, config_dict, processed_vars,
                    debug=True, request_callback=request_callback,
                )
            else:
                # 非请求链模式
                messages = [{"role": "user", "content": "Hello"}]
                result = await ModelCaller.call(messages, config_dict, processed_vars, debug=True)

    except Exception as e:
        import traceback
        logger.error(f"[AB-{side}] 模型调用失败: {e}\n{traceback.format_exc()}")
        result = {"success": False, "content": "", "error": str(e)}

    # 推送结果
    if result.get("success"):
        await send({"type": "success", "side": side, "content": "连接成功"})
        await send({
            "type": "response",
            "side": side,
            "content": result.get("content", "")[:2000],
        })
    else:
        await send({
            "type": "error",
            "side": side,
            "content": result.get("error", "未知错误"),
        })

    duration = round(time.time() - start_time, 2)
    await send({
        "type": "side_complete",
        "side": side,
        "duration": duration,
        "success": result.get("success", False),
    })

    return {
        "success": result.get("success", False),
        "content": result.get("content", ""),
        "request_details": request_details,
        "duration": duration,
    }


# ==================== WebSocket 端点 ====================

@router.websocket("/ws/run")
async def websocket_ab_test(websocket: WebSocket):
    """
    A/B 测试核心 WebSocket 端点。
    并行调用两个模型，通过 side 字段区分 A/B 消息，前端左右分列渲染。
    """
    from app.services.jwt_service import decode_token
    if not decode_token(websocket.query_params.get("token") or ""):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    db = None
    try:
        data_text = await websocket.receive_text()
        data = json.loads(data_text) if data_text else {}

        model_a_id: str = data.get("model_a_id", "")
        model_b_id: str = data.get("model_b_id", "")
        runtime_vars: Dict = data.get("variables", {})

        if not model_a_id or not model_b_id:
            await websocket.send_json({"type": "error", "content": "model_a_id 和 model_b_id 不能为空"})
            await websocket.close()
            return

        processed_vars = _process_runtime_vars(runtime_vars)
        db = SessionLocal()

        # ws_lock 保证并发 send_json 串行化，避免 WebSocket 帧交织
        ws_lock = asyncio.Lock()

        # 并行执行 A 和 B
        results = await asyncio.gather(
            _run_model(model_a_id, "A", websocket, processed_vars, db, ws_lock),
            _run_model(model_b_id, "B", websocket, processed_vars, db, ws_lock),
            return_exceptions=True,
        )

        result_a = results[0] if not isinstance(results[0], Exception) else {"success": False, "duration": 0}
        result_b = results[1] if not isinstance(results[1], Exception) else {"success": False, "duration": 0}

        await websocket.send_json({
            "type": "complete",
            "content": "A/B 测试完成",
            "duration_a": result_a.get("duration", 0),
            "duration_b": result_b.get("duration", 0),
        })

    except WebSocketDisconnect:
        logger.info("A/B Test WebSocket 断开连接")
    except Exception as e:
        import traceback
        logger.error(f"A/B Test WebSocket 异常: {e}\n{traceback.format_exc()}")
        try:
            await websocket.send_json({"type": "error", "content": f"测试异常: {str(e)}"})
        except Exception:
            pass
    finally:
        if db:
            db.close()
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close()
        except Exception:
            pass


# ==================== HTTP 历史记录管理 ====================

@router.post("/sessions", summary="保存 A/B 测试记录")
async def save_session(request: SaveSessionRequest, db: Session = Depends(get_db)):
    """保存一次 A/B 测试历史记录"""
    service = ABTestService(db)
    session = service.create_session(
        model_a_id=request.model_a_id,
        model_b_id=request.model_b_id,
        model_a_name=request.model_a_name,
        model_b_name=request.model_b_name,
        variables=request.variables,
        result_a=request.result_a,
        result_b=request.result_b,
        status=request.status,
        duration_a=request.duration_a,
        duration_b=request.duration_b,
        name=request.name,
    )
    return _session_to_dict(session)


@router.get("/sessions", summary="获取 A/B 测试历史列表")
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str = Query("", description="按备注名模糊搜索"),
    db: Session = Depends(get_db),
):
    """获取历史 A/B 测试记录列表（分页，按创建时间降序）"""
    service = ABTestService(db)
    result = service.list_sessions(page=page, page_size=page_size, keyword=keyword)
    return {
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
        "items": [_session_to_dict(s, include_results=False) for s in result["items"]],
    }


@router.get("/sessions/{session_id}", summary="获取 A/B 测试记录详情")
async def get_session(session_id: str, db: Session = Depends(get_db)):
    """获取单条 A/B 测试记录（含完整结果）"""
    service = ABTestService(db)
    session = service.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="记录不存在")
    return _session_to_dict(session, include_results=True)


@router.delete("/sessions/{session_id}", summary="删除 A/B 测试记录")
async def delete_session(session_id: str, db: Session = Depends(get_db)):
    """删除历史记录"""
    service = ABTestService(db)
    success = service.delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"message": "删除成功"}


# ==================== 工具 ====================

def _session_to_dict(session, include_results: bool = True) -> Dict[str, Any]:
    """将 ORM 对象转为可序列化字典"""
    d: Dict[str, Any] = {
        "id": session.id,
        "name": session.name,
        "model_a_id": session.model_a_id,
        "model_b_id": session.model_b_id,
        "model_a_name": session.model_a_name,
        "model_b_name": session.model_b_name,
        "variables": session.variables,
        "status": session.status,
        "duration_a": session.duration_a,
        "duration_b": session.duration_b,
        "created_at": session.created_at.isoformat() if session.created_at else None,
    }
    if include_results:
        d["result_a"] = session.result_a
        d["result_b"] = session.result_b
    return d
