"""
Claude Code 全局配置服务
提供配置读取（进程级缓存）、裁判 LLM 配置转换、SDK 环境变量构造
"""
import logging
import threading
from typing import Any, Dict, Optional

from app.db.session import SessionLocal
from app.models.claude_code_models import ClaudeCodeConfig

logger = logging.getLogger(__name__)


def mask_token(token: str) -> str:
    """token 脱敏：保留前 4 后 4 位"""
    if not token or len(token) <= 8:
        return "****"
    return f"{token[:4]}****{token[-4:]}"


class ClaudeCodeConfigService:
    """Claude Code 全局配置服务（线程安全缓存）"""

    _lock = threading.Lock()
    _cached_config: Optional[Dict[str, Any]] = None

    @classmethod
    def get_active_config(cls, use_cache: bool = True) -> Optional[Dict[str, Any]]:
        """读取激活配置，返回 dict；未配置时返回 None

        缓存说明：评测链路每次取配置都会调用本方法，进程级缓存避免
        高频查库；保存配置时通过 invalidate_cache() 主动失效。
        """
        if use_cache and cls._cached_config is not None:
            return cls._cached_config
        with cls._lock:
            if use_cache and cls._cached_config is not None:
                return cls._cached_config
            config = None
            try:
                with SessionLocal() as db:
                    row = (
                        db.query(ClaudeCodeConfig)
                        .filter(ClaudeCodeConfig.is_active == True)  # noqa: E712
                        .order_by(ClaudeCodeConfig.created_at.desc())
                        .first()
                    )
                    if row:
                        config = {
                            "id": row.id,
                            "name": row.name,
                            "base_url": row.base_url,
                            "auth_token": row.auth_token,
                            "model": row.model,
                            "timeout_seconds": row.timeout_seconds or 300,
                            "max_turns": row.max_turns or 30,
                            "proxy_url": row.proxy_url,
                            "extra_env": row.extra_env or {},
                            "enable_script_mcp": bool(row.enable_script_mcp),
                        }
            except Exception as e:
                logger.warning(f"读取 Claude Code 配置失败: {e}")
                return None
            if config is not None:
                cls._cached_config = config
            return config

    @classmethod
    def invalidate_cache(cls):
        """配置更新后清除缓存"""
        with cls._lock:
            cls._cached_config = None

    @classmethod
    def get_judge_llm_config(cls) -> Optional[Dict[str, Any]]:
        """转换为现有裁判配置 dict 形态（provider=anthropic，HTTP 直调用）

        消费方：裁判图节点 ModelCallerSync（anthropic 分支）、一致性评测
        JudgeEvaluationEngine、长 prompt 压缩 JudgeClient。
        """
        config = cls.get_active_config()
        if not config:
            return None
        return {
            "provider": "anthropic",
            "model_type": config["model"],
            "endpoint": config["base_url"].rstrip("/"),
            "api_key": config["auth_token"],
            "model_id": config["model"],
            "auth_type": "api_key",
            "auth_config": {},
            "request_config": {},
            "response_parse_config": {},
            # CC 专属扩展字段，调用方按需读取
            "timeout_seconds": config["timeout_seconds"],
        }

    @classmethod
    def get_sdk_env(cls) -> Dict[str, str]:
        """构造 claude-agent-sdk 运行环境变量（覆盖式注入子进程）"""
        config = cls.get_active_config()
        if not config:
            return {}
        env: Dict[str, str] = {
            "ANTHROPIC_BASE_URL": config["base_url"].rstrip("/"),
            "ANTHROPIC_AUTH_TOKEN": config["auth_token"],
            "ANTHROPIC_MODEL": config["model"],
        }
        if config.get("proxy_url"):
            env["HTTPS_PROXY"] = config["proxy_url"]
            env["HTTP_PROXY"] = config["proxy_url"]
        for key, value in (config.get("extra_env") or {}).items():
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                env[key] = str(value)
        return env

    @classmethod
    def get_script_mcp_servers(cls) -> Optional[Dict[str, Dict[str, Any]]]:
        """按 enable_script_mcp 开关返回脚本 MCP server 注入配置

        开启时把 app/services/script_mcp_server.py 以 stdio 形态暴露给
        claude-agent-sdk（ClaudeAgentOptions.mcp_servers，SDK 0.2.163 支持）；
        关闭或未配置时返回 None（调用侧不注入，行为与历史一致）。
        """
        config = cls.get_active_config()
        if not config or not config.get("enable_script_mcp"):
            return None
        # 函数级导入：避免 config service → script_mcp_server 的顶层依赖
        from app.services.script_mcp_server import MCP_SERVER_KEY, get_stdio_server_config

        return {MCP_SERVER_KEY: get_stdio_server_config()}

    @classmethod
    def save_config(cls, payload: Dict[str, Any], created_by: Optional[str] = None) -> Dict[str, Any]:
        """保存全局配置（单例 upsert：有激活记录则更新，否则新建）

        payload 中 auth_token 若与脱敏回显值一致（以 **** 开头）则保留原值。
        """
        import uuid

        with SessionLocal() as db:
            row = (
                db.query(ClaudeCodeConfig)
                .filter(ClaudeCodeConfig.is_active == True)  # noqa: E712
                .order_by(ClaudeCodeConfig.created_at.desc())
                .first()
            )
            if row is None:
                row = ClaudeCodeConfig(
                    id=f"ccc_{uuid.uuid4().hex[:12]}",
                    is_active=True,
                    created_by=created_by,
                )
                db.add(row)
            row.name = payload.get("name") or "默认配置"
            row.base_url = (payload.get("base_url") or "").strip()
            new_token = payload.get("auth_token") or ""
            # 前端回显的脱敏值（前4****后4格式）未修改时保留原 token
            if new_token and "****" not in new_token:
                row.auth_token = new_token.strip()
            row.model = (payload.get("model") or "").strip()
            row.timeout_seconds = int(payload.get("timeout_seconds") or 300)
            row.max_turns = int(payload.get("max_turns") or 30)
            row.proxy_url = (payload.get("proxy_url") or "").strip() or None
            row.extra_env = payload.get("extra_env") or {}
            row.enable_script_mcp = bool(payload.get("enable_script_mcp") or False)
            db.commit()
            db.refresh(row)
        cls.invalidate_cache()
        return cls.get_active_config(use_cache=False)

    @classmethod
    def masked(cls, config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """返回脱敏视图（API 输出用）"""
        if not config:
            return None
        view = dict(config)
        view["auth_token"] = mask_token(config.get("auth_token") or "")
        return view
