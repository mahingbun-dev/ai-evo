"""
DeepSeek Harness 全局配置服务
单例激活配置：进程级缓存读取、SDK 环境变量构造、api_key 脱敏保存
（形态对齐 ClaudeCodeConfigService，见 docs/design/pre-judge-scripts.md 第九节）
"""
import logging
import threading
import uuid
from typing import Any, Dict, Optional

from app.db.session import SessionLocal
from app.models.deepseek_harness_models import DeepSeekHarnessConfig

# 复用既有 token 脱敏（前 4 后 4），与 Claude Code 配置卡回显格式一致
from app.services.claude_code_config_service import mask_token

logger = logging.getLogger(__name__)


class DeepSeekHarnessConfigService:
    """DeepSeek Harness 全局配置服务（线程安全缓存）"""

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
                        db.query(DeepSeekHarnessConfig)
                        .filter(DeepSeekHarnessConfig.is_active == True)  # noqa: E712
                        .order_by(DeepSeekHarnessConfig.created_at.desc())
                        .first()
                    )
                    if row:
                        config = {
                            "id": row.id,
                            "name": row.name,
                            "base_url": row.base_url,
                            "api_key": row.api_key,
                            "protocol": row.protocol or "deepseek",
                            "provider": row.provider or "deepseek-official",
                            "model": row.model,
                            "profile": row.profile or "sdk-minimal",
                            "timeout_seconds": row.timeout_seconds or 300,
                            "max_tokens": row.max_tokens or 49152,
                            "extra_env": row.extra_env or {},
                            "extra_patches": row.extra_patches or [],
                        }
            except Exception as e:
                logger.warning(f"读取 DeepSeek Harness 配置失败: {e}")
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
    def get_sdk_env(cls) -> Dict[str, str]:
        """构造 DeepSeekHarness(env=...) 子进程环境变量

        DSH runtime 默认继承调用者环境，此处仅返回 extra_env 的覆盖项
        （值统一字符串化）。
        """
        config = cls.get_active_config()
        if not config:
            return {}
        env: Dict[str, str] = {}
        for key, value in (config.get("extra_env") or {}).items():
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                env[key] = str(value)
        return env

    @classmethod
    def save_config(cls, payload: Dict[str, Any], created_by: Optional[str] = None) -> Dict[str, Any]:
        """保存全局配置（单例 upsert：有激活记录则更新，否则新建）

        payload 中 api_key 若与脱敏回显值一致（含 ****）则保留原值。
        """
        with SessionLocal() as db:
            row = (
                db.query(DeepSeekHarnessConfig)
                .filter(DeepSeekHarnessConfig.is_active == True)  # noqa: E712
                .order_by(DeepSeekHarnessConfig.created_at.desc())
                .first()
            )
            if row is None:
                row = DeepSeekHarnessConfig(
                    id=f"dsh_{uuid.uuid4().hex[:12]}",
                    is_active=True,
                    created_by=created_by,
                )
                db.add(row)
            row.name = payload.get("name") or "默认配置"
            row.base_url = (payload.get("base_url") or "").strip() or None
            new_key = payload.get("api_key") or ""
            # 前端回显的脱敏值（前4****后4格式）未修改时保留原 api_key
            if new_key and "****" not in new_key:
                row.api_key = new_key.strip()
            row.provider = (payload.get("provider") or "deepseek-official").strip()
            protocol = (payload.get("protocol") or "deepseek").strip()
            if protocol not in ("deepseek", "openai", "anthropic"):
                raise ValueError(f"不支持的网关协议: {protocol!r}（可选 deepseek/openai/anthropic）")
            row.protocol = protocol
            row.model = (payload.get("model") or "").strip()
            row.profile = (payload.get("profile") or "sdk-minimal").strip()
            row.timeout_seconds = int(payload.get("timeout_seconds") or 300)
            row.max_tokens = int(payload.get("max_tokens") or 49152)
            row.extra_env = payload.get("extra_env") or {}
            row.extra_patches = payload.get("extra_patches") or []
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
        view["api_key"] = mask_token(config.get("api_key") or "")
        return view
