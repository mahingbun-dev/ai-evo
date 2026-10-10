"""
DeepSeek Harness 全局配置 API
提供配置读取/保存（api_key 脱敏）/测试连接接口

测试连接两步（设计：docs/design/pre-judge-scripts.md 第十一节）：
① HTTP 直调按网关协议探测（deepseek/openai → /chat/completions；
   anthropic → /v1/messages）验证 base_url/api_key/model
② DeepSeekHarness 最小 run（cwd/dsh_home 均为一次性临时目录）验证 SDK runtime 链路；
   非 deepseek 协议经 pi-ai provider patch 接入（复用 DeepSeekAdapter 的参数构造）
SDK 未安装或无凭据时 test 返回明确的分步错误，不抛 500。
"""
import asyncio
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.deepseek_adapter import is_native_deepseek_protocol
from app.services.deepseek_harness_config_service import DeepSeekHarnessConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/deepseek-harness-config")

VALID_PROTOCOLS = ("deepseek", "openai", "anthropic")


class DeepSeekHarnessConfigPayload(BaseModel):
    """配置保存请求"""
    name: str = Field(default="默认配置")
    base_url: Optional[str] = Field(default=None, description="网关 API 地址（可空则用 SDK 默认）")
    api_key: Optional[str] = Field(default=None, description="网关 API Key（脱敏回显值原样传回则不更新）")
    protocol: str = Field(default="deepseek", description="网关 API 协议：deepseek|openai|anthropic")
    provider: str = Field(default="deepseek-official", description="DSH provider 标识")
    model: str = Field(default="deepseek-v4-flash", description="模型名")
    profile: str = Field(default="sdk-minimal", description="DSH profile")
    timeout_seconds: int = Field(default=300, ge=10, le=3600)
    max_tokens: int = Field(default=49152, ge=256, le=200000)
    extra_env: Dict[str, Any] = Field(default_factory=dict)
    extra_patches: List[str] = Field(default_factory=list)


class DeepSeekHarnessConfigResponse(BaseModel):
    """配置响应（api_key 脱敏）"""
    configured: bool
    config: Optional[Dict[str, Any]] = None


@router.get("", response_model=DeepSeekHarnessConfigResponse)
async def get_config():
    """读取当前全局配置（api_key 脱敏）"""
    config = DeepSeekHarnessConfigService.masked(DeepSeekHarnessConfigService.get_active_config())
    return DeepSeekHarnessConfigResponse(configured=config is not None, config=config)


@router.put("", response_model=DeepSeekHarnessConfigResponse)
async def update_config(payload: DeepSeekHarnessConfigPayload):
    """保存全局配置（单例 upsert）"""
    if not payload.provider.strip():
        raise HTTPException(status_code=400, detail="provider 不能为空")
    if payload.protocol not in VALID_PROTOCOLS:
        raise HTTPException(status_code=400, detail=f"protocol 必须是 {'/'.join(VALID_PROTOCOLS)} 之一")
    if not payload.model.strip():
        raise HTTPException(status_code=400, detail="model 不能为空")
    saved = DeepSeekHarnessConfigService.save_config(payload.model_dump())
    return DeepSeekHarnessConfigResponse(
        configured=True, config=DeepSeekHarnessConfigService.masked(saved)
    )


@router.post("/test")
async def test_connection():
    """测试连接：① HTTP 直调（OpenAI 兼容端点）② DeepSeekHarness 最小 run

    分步返回：agent 是 DSH 的唯一用途形态，其失败即整体失败；
    HTTP 失败时不再进行 agent 步骤；HTTP 未配置凭据时跳过但仍尝试 agent
    （SDK 可用环境变量 DEEPSEEK_API_KEY 凭据）。
    """
    config = DeepSeekHarnessConfigService.get_active_config()
    if not config:
        raise HTTPException(status_code=400, detail="尚未配置 DeepSeek Harness，请先保存配置")

    result: Dict[str, Any] = {
        "http": {"success": False, "message": "", "skipped": False},
        "agent": {"success": False, "message": "", "skipped": False},
    }

    # 第一步：HTTP 直调（验证 base_url/api_key/model；未配置凭据时跳过）
    if not (config.get("base_url") and config.get("api_key")):
        result["http"] = {
            "success": False,
            "message": "未配置 base_url 或 api_key，跳过 HTTP 直调（DSH 可使用环境变量凭据）",
            "skipped": True,
        }
    else:
        try:
            result["http"] = await _test_http(config)
        except Exception as e:
            # type(e).__name__ 兜底：部分异常（如连接被重置）str() 为空串
            detail = f"{type(e).__name__}: {e}".strip()
            if detail.endswith(":"):
                detail = repr(e)
            result["http"]["message"] = f"HTTP 直调异常: {detail}"
        if not result["http"]["success"]:
            return {"success": False, "steps": result}

    # 第二步：DeepSeekHarness 最小 run（一次性临时 cwd/dsh_home，线程内执行不卡事件循环）
    try:
        result["agent"] = await asyncio.to_thread(_test_agent, config)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}".strip()
        if detail.endswith(":"):
            detail = repr(e)
        result["agent"] = {"success": False, "message": f"Agent 测试异常: {detail}", "skipped": False}

    return {"success": bool(result["agent"]["success"]), "steps": result}


def _build_probe_request(config: Dict[str, Any]) -> Dict[str, Any]:
    """按网关协议构造最小探测请求（url/headers/payload/响应内容提取器）"""
    base = config["base_url"].rstrip("/")
    model = config["model"]
    key = config["api_key"]
    protocol = (config.get("protocol") or "deepseek").strip()
    if protocol == "anthropic":
        # Anthropic Messages：部分网关（如内部 LiteLLM relay）只认 Authorization Bearer，
        # 标准 Anthropic 是 x-api-key——两个头都带，网关各取所需。
        # 内容提取取首个 text 块；推理模型（如 deepseek-flash）可能只返回 thinking 块
        def _extract_anthropic(data: Dict[str, Any]) -> str:
            blocks = data.get("content") or []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text") or "")
            return f"（模型返回 {data.get('stop_reason') or 'ok'}，type={data.get('type') or 'message'}）"

        return {
            "url": f"{base}/v1/messages",
            "headers": {
                "x-api-key": key,
                "Authorization": f"Bearer {key}",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            "payload": {"model": model, "max_tokens": 64,
                        "messages": [{"role": "user", "content": "回复：ok"}]},
            "extract": _extract_anthropic,
        }
    # deepseek / openai 兼容
    def _extract_openai(data: Dict[str, Any]) -> str:
        message = (data.get("choices") or [{}])[0].get("message") or {}
        return str(message.get("content")) if message.get("content") is not None \
            else f"（HTTP 200，模型可用，finish={(data.get('choices') or [{}])[0].get('finish_reason') or 'ok'}）"

    return {
        "url": f"{base}/chat/completions",
        "headers": {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "payload": {"model": model, "max_tokens": 64,
                    "messages": [{"role": "user", "content": "回复：ok"}]},
        "extract": _extract_openai,
    }


async def _test_http(config: Dict[str, Any]) -> Dict[str, Any]:
    """按网关协议发最小请求验证 base_url/api_key/model"""
    probe = _build_probe_request(config)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(probe["url"], json=probe["payload"], headers=probe["headers"])
    if resp.status_code != 200:
        return {
            "success": False,
            "message": f"HTTP 直调失败（HTTP {resp.status_code}）: {resp.text[:200]}",
            "skipped": False,
        }
    try:
        content = probe["extract"](resp.json())
    except Exception:
        content = ""
    return {"success": True, "message": f"HTTP 直调成功（模型返回: {str(content)[:50]}）", "skipped": False}


def _test_agent(config: Dict[str, Any]) -> Dict[str, Any]:
    """用 DeepSeekHarness 跑最小 run 验证 SDK runtime 端到端链路

    cwd 与 dsh_home 均为一次性临时目录（sdk-minimal 为 danger-full-access，
    必须隔离），用完即删；SDK 未安装或无凭据时分步明确返回而不是抛错。
    """
    try:
        from deepseek_harness import DeepSeekHarness
    except ImportError:
        return {
            "success": False,
            "message": "deepseek-harness-sdk 未安装，无法验证 Agent 链路"
                       "（安装：pip install deepseek-harness-sdk，需捆绑平台运行时 wheel）",
            "skipped": True,
        }
    if not (config.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")):
        return {
            "success": False,
            "message": "未配置 api_key，且环境变量 DEEPSEEK_API_KEY 不存在，无法验证 Agent 链路",
            "skipped": True,
        }

    cwd = tempfile.mkdtemp(prefix="dsh_test_ws_")
    dsh_home = tempfile.mkdtemp(prefix="dsh_test_home_")
    try:
        # 参数构造复用 DeepSeekAdapter（协议分派 / pi-ai patch / 隔离目录同源实现）
        from app.services.deepseek_adapter import DeepSeekAdapter

        kwargs: Dict[str, Any] = DeepSeekAdapter(config=config)._build_harness_kwargs(
            dsh_home, cwd, timeout=60.0,
        )
        kwargs["request_timeout_seconds"] = 60.0

        final_text = ""
        with DeepSeekHarness(**kwargs) as harness:
            result = harness.run("请直接回复：ok")
            final_text = str(getattr(result, "final_response", "") or "")[:200]
        if not final_text:
            return {"success": False, "message": "Agent 无返回内容", "skipped": False}
        return {"success": True, "message": f"Agent 链路成功（返回: {final_text[:80]}）", "skipped": False}
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(dsh_home, ignore_errors=True)
