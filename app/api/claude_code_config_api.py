"""
Claude Code 全局配置 API
提供配置读取/保存/测试连接接口（裁判引擎共用配置）
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Depends
from app.services.access_control import ensure_super_admin
from pydantic import BaseModel, Field

from app.services.claude_code_config_service import ClaudeCodeConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/claude-code-config", dependencies=[Depends(ensure_super_admin)])


class ClaudeCodeConfigPayload(BaseModel):
    """配置保存请求"""
    name: str = Field(default="默认配置")
    base_url: str = Field(..., description="ANTHROPIC_BASE_URL")
    auth_token: str = Field(..., description="ANTHROPIC_AUTH_TOKEN（脱敏回显值原样传回则不更新）")
    model: str = Field(..., description="模型名")
    timeout_seconds: int = Field(default=300, ge=10, le=3600)
    max_turns: int = Field(default=30, ge=1, le=200)
    proxy_url: Optional[str] = Field(default=None)
    extra_env: Dict[str, Any] = Field(default_factory=dict)
    enable_script_mcp: bool = Field(
        default=False,
        description="启用脚本 MCP 工具：把内置前处理脚本以 stdio MCP server 注入裁判 agent",
    )


class ClaudeCodeConfigResponse(BaseModel):
    """配置响应（token 脱敏）"""
    configured: bool
    config: Optional[Dict[str, Any]] = None


@router.get("", response_model=ClaudeCodeConfigResponse)
async def get_config():
    """读取当前全局配置（token 脱敏）"""
    config = ClaudeCodeConfigService.masked(ClaudeCodeConfigService.get_active_config())
    return ClaudeCodeConfigResponse(configured=config is not None, config=config)


@router.put("", response_model=ClaudeCodeConfigResponse)
async def update_config(payload: ClaudeCodeConfigPayload):
    """保存全局配置（单例 upsert）"""
    if not payload.base_url.strip():
        raise HTTPException(status_code=400, detail="base_url 不能为空")
    if not payload.model.strip():
        raise HTTPException(status_code=400, detail="model 不能为空")
    saved = ClaudeCodeConfigService.save_config(payload.model_dump())
    return ClaudeCodeConfigResponse(
        configured=True, config=ClaudeCodeConfigService.masked(saved)
    )


@router.post("/test")
async def test_connection():
    """测试连接：① HTTP 直调验证网关连通性 ② SDK Agent 验证 CLI 可用性

    agent 层失败不否定配置本身（图模式仅需 HTTP），结果分步返回。
    """
    config = ClaudeCodeConfigService.get_active_config()
    if not config:
        raise HTTPException(status_code=400, detail="尚未配置 Claude Code，请先保存配置")

    result: Dict[str, Any] = {
        "http": {"success": False, "message": ""},
        "agent": {"success": False, "message": "", "skipped": False},
    }

    # 第一步：anthropic SDK 直调（图模式所需的最低能力）
    try:
        result["http"] = await _test_http(config)
    except Exception as e:
        result["http"]["message"] = f"HTTP 直调异常: {e}"
    if not result["http"]["success"]:
        return {"success": False, "steps": result}

    # 第二步：SDK Agent 查询（claude_agent 模式所需，依赖本机/镜像内 CLI）
    try:
        result["agent"] = await _test_agent(config)
    except Exception as e:
        result["agent"]["message"] = f"Agent 测试异常: {e}"

    overall = result["http"]["success"]
    return {"success": overall, "steps": result}


async def _test_http(config: Dict[str, Any]) -> Dict[str, Any]:
    """用 anthropic SDK 发起最小请求验证 base_url/token/model"""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(
        api_key=config["auth_token"],
        base_url=config["base_url"].rstrip("/"),
        timeout=30.0,
    )
    resp = await client.messages.create(
        model=config["model"],
        max_tokens=16,
        messages=[{"role": "user", "content": "回复：ok"}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return {"success": True, "message": f"HTTP 直调成功（模型返回: {text[:50]}）"}


async def _test_agent(config: Dict[str, Any]) -> Dict[str, Any]:
    """用 claude-agent-sdk 跑最小查询验证 CLI + 端到端链路"""
    from claude_agent_sdk import query, ClaudeAgentOptions

    options = ClaudeAgentOptions(
        system_prompt="你是连接测试助手。",
        allowed_tools=[],
        max_turns=1,
        model=config["model"],
        env=ClaudeCodeConfigService.get_sdk_env(),
        setting_sources=[],
    )
    final_text = ""
    async def _run():
        nonlocal final_text
        async for message in query(prompt="请直接回复：ok", options=options):
            if type(message).__name__ == "ResultMessage":
                final_text = str(getattr(message, "result", "") or "")[:200]

    await asyncio.wait_for(_run(), timeout=90)
    if not final_text:
        return {"success": False, "message": "Agent 无返回内容", "skipped": False}
    return {"success": True, "message": f"Agent 链路成功（返回: {final_text[:80]}）", "skipped": False}
